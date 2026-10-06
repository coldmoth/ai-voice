"""Bounded PCM ring and nonblocking PortAudio playback callback.

Design notes (do not regress):
- One producer (network TTS push), one callback consumer (primary PortAudio).
- ``PCMBuffer._append`` is the only producer path that takes the ring lock.
- The PortAudio callback NEVER blocks on the producer lock; it only reads
  whatever is already in the buffer (try non-blocking next attempt).
- Attached monitors receive a snapshot tuple captured outside the callback so
  that ``callback`` does not take ``PCMBuffer._monitors_lock``. Monitors are
  fed pre-gain (raw) PCM via ``PCMBuffer.fan_out`` which only enqueues a bounded
  handoff into each monitor's own queue, no resampling on this thread.
- Raw samples are copied BEFORE ``np.multiply`` / ``np.clip`` so the
  monitor does not observe the in-place main modification.
"""
import math
import threading

import numpy as np
import sounddevice as sd
import soxr

from .devices import resolve_output


class PCMBuffer:
    """One producer and one callback consumer; drop oldest audio on overflow.

    push() runs on the producer, read() never waits on the producer lock.
    finish() flushes the continuous resampler once at an utterance boundary.
    """

    def __init__(self, *, input_rate: int = 44100, output_rate: int = 48000,
                 max_frames: int = 12000):
        if any(type(value) is not int or value <= 0 for value in (input_rate, output_rate, max_frames)):
            raise ValueError("PCM rates and capacity must be positive")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.max_frames = max_frames
        self._ring = np.zeros(max_frames, dtype=np.float32)
        self._head = 0
        self._count = 0
        self._lock = threading.Lock()
        self._carry = b""
        self._resampler = self._new_resampler()
        self.dropped_frames = 0
        # Tuple snapshot of attached monitors.  Updated under
        # ``_monitors_lock`` from ``attach_monitor``/``detach_monitor``;
        # readers grab an immutable snapshot without holding the lock.
        self._monitors = ()
        self._monitors_lock = threading.Lock()
        # Monotonic epoch guard.  Cleared whenever the resampler is
        # recreated so any monitor worker processing a stale batch can
        # bail before forwarding it to its output ring.
        self._generation = 0
        self._generation_lock = threading.Lock()

    def _new_resampler(self):
        if self.input_rate == self.output_rate:
            return None
        return soxr.ResampleStream(self.input_rate, self.output_rate, 1,
                                   dtype="float32", quality="LQ")

    @property
    def buffered_frames(self) -> int:
        with self._lock:
            return self._count

    def push(self, data: bytes):
        raw = self._carry + data
        end = len(raw) - len(raw) % 2
        self._carry = raw[end:]
        if not end:
            return
        samples = np.frombuffer(raw[:end], dtype="<i2").astype(np.float32) / 32768
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples, last=False)
        self._append(samples)

    def finish(self):
        if self._carry:
            self._carry = b""
            raise ValueError("PCM ended with an incomplete 16-bit sample")
        if self._resampler is not None:
            self._append(self._resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True))
            self._resampler = self._new_resampler()

    def append_float32(self, samples):
        """Public hook for downstream sinks that already have float32 audio."""
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            return
        self._append(arr)

    def _append(self, samples):
        size = len(samples)
        if not size:
            return
        samples = np.asarray(samples, dtype=np.float32)
        with self._lock:
            excess = max(0, self._count + size - self.max_frames)
            self.dropped_frames += excess
            if size >= self.max_frames:
                self._ring[:] = samples[-self.max_frames:]
                self._head = 0
                self._count = self.max_frames
                return
            if excess:
                self._head = (self._head + excess) % self.max_frames
                self._count -= excess
            tail = (self._head + self._count) % self.max_frames
            first = min(size, self.max_frames - tail)
            self._ring[tail:tail + first] = samples[:first]
            self._ring[:size - first] = samples[first:]
            self._count += size

    # ------------------------------------------------------------------
    # Monitor fan-out (nonblocking, no lock contention on the callback)
    # ------------------------------------------------------------------
    def fan_out(self, samples):
        """Hand a copy of pre-gain (raw) PCM to every attached monitor.

        The PortAudio callback invokes this AFTER it has read the ring;
        the samples are the raw copy taken BEFORE the main gain is applied
        (the monitor has its own gain fader).  ``samples`` is a 1-D
        float32 ndarray.  This method must NOT take any lock the producer
        or the primary callback relies on, otherwise audio glitches appear
        on the main output sink.

        We take only ``self._monitors_lock`` long enough to publish an

        immutable tuple snapshot.  We never call into a monitor under the
        lock, and we never invoke the resampler (resampling happens inside
        the monitor's own dedicated worker).
        """
        if samples is None:
            return
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            return
        targets = self._monitors
        if not targets:
            return
        # Make sure we own the data: the callback hands over a private raw
        # copy of ``samples``, so a fresh view is enough,
        # but defensive copy keeps the contract explicit.
        view = arr if arr.flags.writeable else arr.copy()
        for monitor in targets:
            try:
                monitor.feed(view)
            except Exception:
                # Local error path is best-effort; never let a monitor
                # exception raise into the primary audio callback.
                try:
                    monitor.errors += 1
                except Exception:
                    pass

    def attach_monitor(self, monitor):
        with self._monitors_lock:
            current = list(self._monitors)
            if monitor not in current:
                current.append(monitor)
            self._monitors = tuple(current)

    def detach_monitor(self, monitor):
        with self._monitors_lock:
            current = list(self._monitors)
            if monitor in current:
                current.remove(monitor)
            self._monitors = tuple(current)

    def current_generation(self) -> int:
        with self._generation_lock:
            return self._generation

    def read(self, frames: int) -> np.ndarray:
        if frames < 0:
            raise ValueError("frames must not be negative")
        result = np.zeros(frames, dtype=np.float32)
        if not self._lock.acquire(blocking=False):
            return result
        try:
            count = min(frames, self._count)
            first = min(count, self.max_frames - self._head)
            result[:first] = self._ring[self._head:self._head + first]
            result[first:count] = self._ring[:count - first]
            self._head = (self._head + count) % self.max_frames
            self._count -= count
        finally:
            self._lock.release()
        return result

    def clear(self):
        """Discard queued speech and reset conversion after cancellation.

        Only safe to call from the lifecycle thread.  Resets the raw
        PCM ring, carry buffer, and resampler under the producer lock.
        Monitors are notified outside the lock so a slow monitor cannot
        stall the primary path.
        """
        with self._lock:
            self._head = 0
            self._count = 0
        self._carry = b""
        self._resampler = self._new_resampler()
        # Bump the epoch so any in-flight worker batches for the previous
        # generation are discarded before they reach the output ring.
        self._generation += 1
        # Snapshot the monitor list under the monitors lock, then clear
        # each one outside the lock so a slow monitor cannot stall the
        # primary path.
        with self._monitors_lock:
            monitors = self._monitors
        for monitor in monitors:
            try:
                monitor.clear()
            except Exception:
                pass


class Playback:
    def __init__(self, buffer: PCMBuffer, *, device: str = "AI Voice", sample_rate: int = 48000,
                 gain: float = 1.0):
        if buffer.output_rate != sample_rate:
            raise ValueError("Playback and buffer sample rates must match")
        self.buffer = buffer
        self.device = device
        self.sample_rate = sample_rate
        self.set_gain(gain)
        self._stream = None
        # Tuple snapshot of attached monitors so the callback never locks.
        self._attached_monitors = ()
        self._monitors_lock = threading.Lock()

    def set_gain(self, gain):
        if (isinstance(gain, bool) or not isinstance(gain, (int, float))
                or not math.isfinite(gain) or gain < 0):
            raise ValueError("gain must be a finite non-negative number")
        # Immutable float reference assignment is atomic under the CPython GIL.
        self._gain = float(gain)

    def attach_monitor(self, monitor):
        with self._monitors_lock:
            current = list(self._attached_monitors)
            if monitor not in current:
                current.append(monitor)
                self._attached_monitors = tuple(current)
        self.buffer.attach_monitor(monitor)

    def detach_monitor(self, monitor):
        with self._monitors_lock:
            current = list(self._attached_monitors)
            if monitor in current:
                current.remove(monitor)
                self._attached_monitors = tuple(current)
        try:
            self.buffer.detach_monitor(monitor)
        except Exception:
            pass

    def callback(self, outdata, frames, time_info, status):
        """PortAudio callback for the primary output sink.

        Steps:
          1. Pull already-prefilled samples from the buffer (nonblocking).
          2. Copy the raw samples BEFORE the main gain/clip so monitors
 see independent audio.
          3. Apply the main gain and clip in place.
          4. Push the raw pre-gain copy from step 2 to attached monitors via
             ``PCMBuffer.fan_out``.  The monitor list is an immutable
             tuple snapshot taken under ``_monitors_lock`` BEFORE the
             audio is mutated, so the callback never blocks on the
             monitor producer.
        """
        try:
            samples = self.buffer.read(frames)
        except Exception:
            outdata[:] = 0
            return
        # Snapshot attached monitors BEFORE in-place modification.  This
        # keeps monitor fan-out independent from the main gain path.
        monitors = self._attached_monitors
        raw_for_monitors = None
        if monitors:
            # Defensive copy of the raw pre-gain PCM so the monitor
            # path never observes the in-place main multiply/clip.
            raw_for_monitors = samples.copy()
        gain = self._gain
        if gain != 1.0:
            np.multiply(samples, gain, out=samples)
            np.clip(samples, -1.0, 1.0, out=samples)
        outdata[:] = samples[:, None]
        if monitors:
            try:
                self.buffer.fan_out(raw_for_monitors if raw_for_monitors is not None else samples)
            except Exception:
                # Fan-out is best-effort; never propagate into the
                # primary callback.
                pass

    def start(self):
        if self._stream is not None:
            return
        index = resolve_output(self.device)
        try:
            stream = sd.OutputStream(device=index, samplerate=self.sample_rate,
                                     channels=2, dtype="float32", latency="low",
                                     blocksize=0, callback=self.callback)
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self._stream = stream
        except sd.PortAudioError:
            raise RuntimeError(f"Cannot open output device {self.device!r}; check ai-voice devices") from None

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None
        with self._monitors_lock:
            self._attached_monitors = ()
        self.buffer.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
