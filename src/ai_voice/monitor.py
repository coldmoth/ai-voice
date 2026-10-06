"""Optional local monitoring of synthesized output on a separate device.

The monitor never reads from the primary ring; it only receives pre-gain
(raw synthesized) PCM directly from the Playback callback via ``feed``.  Resampling is
performed by a dedicated worker thread so the PortAudio output callback
is strictly nonblocking: it only reads from a bounded ring and applies
the monitor gain.  Any monitor error is counted and isolated; the
primary sink is never affected.
"""
import math
import threading

import numpy as np
import sounddevice as sd
import soxr

from .devices import list_devices, resolve_output


def _is_finite(value):
    return math.isfinite(value)


class MonitorSink:
    """Optional OutputStream fed by the playback callback without re-reading the ring.

    The primary playback callback hands pre-gain (raw) PCM to ``feed`` directly.
    ``feed`` never reads from the primary ring; it only enqueues into the
    monitor's own bounded raw PCM ring.  A dedicated worker thread owns
    the resampler and forwards resampled frames to a second bounded ring
    that the PortAudio output callback drains.

    Lifecycle invariants:
      * ``start`` is idempotent.
      * ``close`` is safe to invoke from any thread and any number of times.
      * ``feed`` is nonblocking; it returns immediately when the monitor
        is not active or when the raw ring is full (oldest samples are
        dropped on overflow).
      * The output callback NEVER blocks on the worker thread.  If the
        output ring is short, the callback emits silence for the missing
        frames and increments ``dropped_frames``; it never resamples.
      * The resampler is owned exclusively by the worker thread.
        ``clear`` increments a generation epoch so the worker rebuilds
        its own resampler and drops any in-flight batch when it next
        observes the epoch change.
      * Errors inside the worker or the output callback are counted on
        ``self.errors`` and never raised.
    """

    # Tunable constants.  Kept as class attributes so tests can patch them.
    _RAW_CAPACITY = 9600   # ~200 ms at 48 kHz of raw incoming PCM
    _OUT_CAPACITY = 9600   # ~200 ms at 48 kHz of resampled output frames
    _WORKER_BATCH = 1024   # frames the worker drains per pass

    def __init__(self, *, device: str, sample_rate: int, gain: float = 1.0,
                 max_frames: int = 4800, channels: int = 2):
        if not isinstance(device, str) or not device.strip():
            raise ValueError("Monitor device must be a non-empty string.")
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("Monitor sample rate must be a positive integer.")
        if (isinstance(gain, bool) or not isinstance(gain, (int, float))
                or not _is_finite(gain) or gain < 0):
            raise ValueError("Monitor gain must be a finite non-negative number.")
        if type(max_frames) is not int or max_frames <= 0:
            raise ValueError("Monitor buffer capacity must be a positive integer.")
        if channels not in (1, 2):
            raise ValueError("Monitor channels must be 1 or 2.")
        self.device = device
        self.requested_rate = sample_rate
        self.channels = channels
        self.max_frames = max_frames
        self._gain = float(gain)
        self.dropped_frames = 0
        self.errors = 0
        self._closing = threading.Event()
        self._stopped = threading.Event()
        self._active = False
        # Raw incoming PCM ring (filled from the primary callback).
        self._raw = np.zeros(self._RAW_CAPACITY, dtype=np.float32)
        self._raw_head = 0
        self._raw_count = 0
        self._raw_lock = threading.Lock()
        # Output ring (filled by worker, drained by PortAudio callback).
        self._out = np.zeros(self._OUT_CAPACITY, dtype=np.float32)
        self._out_head = 0
        self._out_count = 0
        self._out_lock = threading.Lock()
        self._stream = None
        self._worker = None
        # Resampler state is owned by the worker thread.  These fields
        # are read by ``start``/``clear`` only via the worker epoch.
        self._resampler = None
        self._resampler_input = None
        self._resampler_target = None
        # Generation/epoch guards against races between close/start/clear.
        self._generation = 0
        # Lock that serialises the rare cases where the caller wants to
        # wait briefly for the raw ring to be in a known state (e.g.
        # ``feed`` when monitoring starts and the producer side
        # enqueues the first batch).
        self._raw_seq_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public configuration helpers
    # ------------------------------------------------------------------
    def set_gain(self, gain):
        if (isinstance(gain, bool) or not isinstance(gain, (int, float))
                or not _is_finite(gain) or gain < 0):
            raise ValueError("Monitor gain must be a finite non-negative number.")
        self._gain = float(gain)

    def actual_rate(self) -> int:
        if self._stream is None:
            return self.requested_rate
        try:
            return int(self._stream.samplerate)
        except (AttributeError, sd.PortAudioError):
            return self.requested_rate

    @property
    def active(self) -> bool:
        return self._active and self._stream is not None and not self._closing.is_set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        """Open the OutputStream and start the resampler worker thread.

        Idempotent: calling ``start`` twice is a no-op.  If a previous
        stream is still alive (e.g. close-then-start race) this method
        waits briefly for the worker to exit before re-opening.
        """
        if self._stream is not None:
            return
        self._closing.clear()
        self._stopped.clear()
        index = resolve_output(self.device)
        rate = self.requested_rate
        stream = None
        try:
            stream = sd.OutputStream(device=index, samplerate=rate,
                                     channels=self.channels, dtype="float32",
                                     latency="low", blocksize=0, callback=self._callback)
        except sd.PortAudioError:
            stream = None
        if stream is None:
            try:
                defaults = sd.query_devices(index)
                if isinstance(defaults, dict):
                    native = defaults.get("default_samplerate")
                    if isinstance(native, (int, float)) and _is_finite(float(native)) and int(native) > 0:
                        native = int(native)
                        if native != rate:
                            try:
                                stream = sd.OutputStream(device=index, samplerate=native,
                                                         channels=self.channels, dtype="float32",
                                                         latency="low", blocksize=0, callback=self._callback)
                                rate = native
                            except sd.PortAudioError:
                                stream = None
            except sd.PortAudioError:
                pass
        if stream is None:
            raise RuntimeError(f"Cannot open monitor device {self.device!r}; check ai-voice devices")
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        self._stream = stream
        self._resampler_target = rate
        self._resampler_input = self.requested_rate
        # Reset the output ring so leftover frames from a prior session
        # do not appear in the new stream.  Drop any queued raw frames
        # for the same reason.
        self._reset_raw()
        self._reset_out()
        # Bump generation: the worker, when it starts, will build its
        # own resampler based on the current input/target rates.
        self._generation += 1
        generation = self._generation
        self._active = True
        self._stopped.clear()
        worker = threading.Thread(target=self._worker_loop,
                                  name="MonitorSink-Audio",
                                  args=(generation,),
                                  daemon=True)
        self._worker = worker
        worker.start()

    def _build_resampler(self):
        if self._resampler_input == self._resampler_target:
            return None
        try:
            return soxr.ResampleStream(self._resampler_input,
                                       self._resampler_target, 1,
                                       dtype="float32", quality="LQ")
        except Exception:
            return None

    def _worker_loop(self, generation: int):
        """Resample raw PCM into the output ring; sole resampler owner."""
        local_resampler = self._build_resampler()
        try:
            while not self._closing.is_set():
                if generation != self._generation:
                    generation = self._generation
                    local_resampler = self._build_resampler()
                # Observe epoch change: rebuild the resampler and skip
                # the in-flight batch (it may target the old rates).
                if local_resampler is None and self._resampler_input != self._resampler_target:
                    local_resampler = self._build_resampler()
                frames = self._drain_raw(getattr(self, "_WORKER_BATCH", 1024))
                if frames is None or frames.size == 0:
                    # No work; tiny sleep to avoid pegging the CPU.
                    self._closing.wait(0.005)
                    continue
                if generation != self._generation:
                    # Epoch changed mid-drain: drop the in-flight batch
                    # and rebuild the resampler on the next iteration.
                    local_resampler = None
                    continue
                if local_resampler is not None:
                    try:
                        out = local_resampler.resample_chunk(frames, last=False)
                    except Exception:
                        self.errors += 1
                        local_resampler = self._build_resampler()
                        continue
                    if out is None or out.size == 0:
                        continue
                else:
                    out = frames
                # Re-check the epoch before enqueueing the resampled
                # batch so that a concurrent ``clear`` cannot be
                # outrun by stale audio.
                if generation != self._generation:
                    local_resampler = None
                    continue
                with self._raw_seq_lock:
                    if generation == self._generation and not self._closing.is_set():
                        self._enqueue_output(out)
        except Exception:
            self.errors += 1
        finally:
            self._active = False
            self._stopped.set()

    # ------------------------------------------------------------------
    # Output callback (PortAudio)
    # ------------------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status):
        """PortAudio output callback.

        Strictly nonblocking: reads whatever is already in the output
        ring; never resamples; never waits on the worker thread.  A
        single ``_read_output`` call produces the writable buffer that
        is then gain/clip-adjusted and broadcast to the configured
        channel layout.
        """
        try:
            samples = self._read_output(frames)
            if samples.shape != (frames,):
                fixed = np.zeros(frames, dtype=np.float32)
                n = min(frames, samples.shape[0]) if samples.size else 0
                if n:
                    fixed[:n] = samples[:n]
                samples = fixed
            gain = self._gain
            if gain != 1.0:
                np.multiply(samples, gain, out=samples)
                np.clip(samples, -1.0, 1.0, out=samples)
            if self.channels == 2:
                outdata[:] = samples[:, None]
            else:
                outdata[:, 0] = samples
        except Exception:
            self.errors += 1
            try:
                outdata[:] = 0
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Ring helpers (real, used by ``_callback``)
    # ------------------------------------------------------------------
    def _read_output(self, frames):
        """Nonblocking drain of the output ring.

        Returns a fresh ``np.ndarray`` of exactly ``frames`` samples.
        Missing samples are filled with silence and counted as drops.
        """
        result = np.zeros(frames, dtype=np.float32)
        if not self._out_lock.acquire(blocking=False):
            self.dropped_frames += frames
            return result
        try:
            count = min(frames, self._out_count)
            if count:
                first = min(count, self._OUT_CAPACITY - self._out_head)
                result[:first] = self._out[self._out_head:self._out_head + first]
                if count > first:
                    result[first:count] = self._out[:count - first]
                self._out_head = (self._out_head + count) % self._OUT_CAPACITY
                self._out_count -= count
            missing = frames - count
            if missing:
                self.dropped_frames += missing
        finally:
            self._out_lock.release()
        return result

    def _enqueue_output(self, samples):
        size = len(samples)
        if not size:
            return
        if not self._out_lock.acquire(blocking=False):
            self.dropped_frames += size
            return
        try:
            excess = max(0, self._out_count + size - self._OUT_CAPACITY)
            self.dropped_frames += excess
            if size >= self._OUT_CAPACITY:
                self._out[:] = samples[-self._OUT_CAPACITY:]
                self._out_head = 0
                self._out_count = self._OUT_CAPACITY
                return
            if excess:
                self._out_head = (self._out_head + excess) % self._OUT_CAPACITY
                self._out_count -= excess
            tail = (self._out_head + self._out_count) % self._OUT_CAPACITY
            first = min(size, self._OUT_CAPACITY - tail)
            self._out[tail:tail + first] = samples[:first]
            if size > first:
                self._out[:size - first] = samples[first:]
            self._out_count += size
        finally:
            self._out_lock.release()

    # ------------------------------------------------------------------
    # Raw ring (producer side: primary playback callback)
    # ------------------------------------------------------------------
    def _drain_raw(self, frames):
        """Drain up to ``frames`` samples from the raw ring.

        Returns an ``np.ndarray`` whose length equals the number of
        samples actually drained.  When the lock is contended or the
        ring is empty an empty array is returned so the worker can
        ``continue`` without synthesising fake samples.
        """
        if not self._raw_lock.acquire(blocking=False):
            return np.empty(0, dtype=np.float32)
        try:
            count = min(frames, self._raw_count)
            if not count:
                return np.empty(0, dtype=np.float32)
            result = np.empty(count, dtype=np.float32)
            first = min(count, self._RAW_CAPACITY - self._raw_head)
            result[:first] = self._raw[self._raw_head:self._raw_head + first]
            if count > first:
                result[first:count] = self._raw[:count - first]
            self._raw_head = (self._raw_head + count) % self._RAW_CAPACITY
            self._raw_count -= count
            return result
        finally:
            self._raw_lock.release()

    def _enqueue_raw(self, samples):
        size = len(samples)
        if not size:
            return
        if not self._raw_lock.acquire(blocking=False):
            self.dropped_frames += size
            return
        try:
            excess = max(0, self._raw_count + size - self._RAW_CAPACITY)
            self.dropped_frames += excess
            if size >= self._RAW_CAPACITY:
                self._raw[:] = samples[-self._RAW_CAPACITY:]
                self._raw_head = 0
                self._raw_count = self._RAW_CAPACITY
                return
            if excess:
                self._raw_head = (self._raw_head + excess) % self._RAW_CAPACITY
                self._raw_count -= excess
            tail = (self._raw_head + self._raw_count) % self._RAW_CAPACITY
            first = min(size, self._RAW_CAPACITY - tail)
            self._raw[tail:tail + first] = samples[:first]
            if size > first:
                self._raw[:size - first] = samples[first:]
            self._raw_count += size
        finally:
            self._raw_lock.release()

    def _reset_raw(self):
        if not self._raw_lock.acquire(blocking=False):
            return
        try:
            self._raw_head = 0
            self._raw_count = 0
        finally:
            self._raw_lock.release()

    def _reset_out(self):
        if not self._out_lock.acquire(blocking=False):
            return
        try:
            self._out_head = 0
            self._out_count = 0
        finally:
            self._out_lock.release()

    # ------------------------------------------------------------------
    # Producer entry point: called by ``Playback.callback`` / ``PCMBuffer.fan_out``
    # ------------------------------------------------------------------
    def feed(self, samples):
        """Enqueue a copy of the pre-gain PCM without ever touching the primary ring.

        Strictly nonblocking: returns immediately when the monitor is
        inactive or when the raw ring is full (oldest samples are
        dropped on overflow, counted on ``dropped_frames``).
        """
        if self._stream is None or self._closing.is_set() or not self._active:
            return
        if samples is None:
            return
        try:
            arr = np.asarray(samples, dtype=np.float32)
        except Exception:
            self.errors += 1
            return
        if arr.size == 0:
            return
        # Defensive copy: the caller may have reused the underlying
        # buffer for the next callback iteration.
        if not arr.flags.writeable or arr.base is not None:
            arr = arr.copy()
        # Snapshot the current epoch so that an in-flight batch cannot
        # be reordered past a concurrent ``clear``/``close``.
        gen = self._generation
        if not self._raw_seq_lock.acquire(blocking=False):
            self.dropped_frames += arr.size
            return
        try:
            if gen != self._generation or self._stream is None or self._closing.is_set():
                return
            self._enqueue_raw(arr)
        finally:
            self._raw_seq_lock.release()

    # ------------------------------------------------------------------
    # State maintenance
    # ------------------------------------------------------------------
    def clear(self):
        """Drop queued audio and reset the worker-side resampler.

        ``clear`` must never directly replace ``self._resampler``
        because that field is owned by the worker thread.  Instead
        the generation epoch is bumped; the worker observes the
        change, rebuilds its local resampler, and discards any
        in-flight batch.  The bounded rings are cleared under their
        own locks (never from inside the PortAudio callback).
        """
        # Bump the epoch FIRST so any concurrent feed/worker observes
        # the change before new audio enters the rings.
        with self._raw_seq_lock:
            self._generation += 1
            with self._raw_lock:
                self._raw_head = self._raw_count = 0
            with self._out_lock:
                self._out_head = self._out_count = 0

    def close(self):
        """Stop the worker, drain pending audio, close the OutputStream.

        Safe to call from any thread and any number of times.
        """
        if not self._closing.is_set():
            self._closing.set()
        self._active = False
        # Bump the generation so the worker exits even if it is mid-loop.
        self._generation += 1
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.0)
        stream = self._stream
        self._stream = None
        # Resampler is owned by the worker; clearing our reference is
        # safe because the worker has either exited or will exit on
        # its next epoch check.
        self._resampler = None
        if stream is not None:
            try:
                stream.stop()
            except sd.PortAudioError:
                pass
            except Exception:
                pass
            try:
                stream.close()
            except sd.PortAudioError:
                pass
            except Exception:
                pass
        self._reset_raw()
        self._reset_out()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def enumerate_monitor_candidates():
    """Return physical output device names safe to use as a monitor sink."""
    from .catalog import _is_monitor_blacklisted
    seen = []
    for device in list_devices():
        name = device.get("name")
        if not isinstance(name, str) or not name:
            continue
        if device.get("max_output_channels", 0) < 1:
            continue
        if _is_monitor_blacklisted(name):
            continue
        if name in seen:
            continue
        seen.append(name)
    return seen
