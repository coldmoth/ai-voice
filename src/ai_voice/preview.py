"""Dedicated short-clip preview sink independent from the main output pipeline.

Plays raw Fish 44.1 kHz int16 PCM on a user-chosen physical output device
without ever touching ``PCMBuffer``, the monitor pipeline, or the
``AudioSession`` voice on the main output.  ``PreviewPlayer`` enforces:

* A single ``_PreviewSink`` at a time.  A new ``start`` request cancels
  and replaces the active one; an explicit ``stop`` closes it.
* Latest-wins cancellation: each ``start`` mints a fresh integer
  generation token; stale workers exit without touching shared state.
* Bounded buffers: the raw ring is 4 seconds at 44.1 kHz; overflow
  drops oldest samples and counts them on ``dropped_frames``.
* Bounded work: the playback task reads chunks at most 1024 frames
  per loop and yields between batches.
* Clean shutdown: ``close`` is idempotent and safe to invoke from
  any thread; the OutputStream and worker task are awaited.
* No interaction with the main audio pipeline: this module imports
  neither ``PCMBuffer`` nor ``Playback``.
"""
import asyncio
import dataclasses
import math
import re
import threading
from typing import Optional

import httpx
import numpy as np
import sounddevice as sd
import soxr

from .i18n import t
from .config import Config, VOICES
from .fish_tts import FishTTS
from .secrets import load_key


MAX_PREVIEW_SECONDS = 4.0
SAMPLE_RATE = 44100
RING_CAPACITY = int(SAMPLE_RATE * MAX_PREVIEW_SECONDS)
BLOCK_FRAMES = 1024


def _resolve_voice_id(identifier):
    """Return the canonical 32-hex reference id or ``None``.

    Accepts both registered slugs and raw 32-hex reference identifiers;
    unknown / empty values resolve to ``None``.
    """
    if not isinstance(identifier, str):
        return None
    candidate = identifier.strip()
    if not candidate:
        return None
    if candidate in VOICES:
        return VOICES[candidate]
    if re.fullmatch(r"[0-9a-fA-F]{32}", candidate):
        return candidate.lower()
    for voice_id in VOICES.values():
        if voice_id == candidate:
            return candidate
    return None


def _enumerate_physical_outputs():
    """Return physical output device (index, name) pairs safe to preview on."""
    from .catalog import _is_monitor_blacklisted

    seen = []
    try:
        devices = sd.query_devices()
    except sd.PortAudioError:
        return seen
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            continue
        if device.get("max_output_channels", 0) < 1:
            continue
        name = device.get("name")
        if not isinstance(name, str) or not name:
            continue
        if _is_monitor_blacklisted(name):
            continue
        if any(entry[1] == name for entry in seen):
            continue
        seen.append((index, name))
    return seen


def _resolve_output_index(device_name):
    if not isinstance(device_name, str) or not device_name.strip():
        return None
    target = device_name.strip()
    for index, name in _enumerate_physical_outputs():
        if name == target:
            return index
    return None


class _PreviewConfig(Config):
    """Config subclass that overrides ``reference_id`` without globals.

    ``Config`` is frozen and tied to a slug via ``__post_init__``; this
    subclass lets a caller supply an arbitrary 32-hex reference id
    that the parent class would otherwise reject.
    """

    def __init__(self, *, reference_id, **kwargs):
        object.__setattr__(self, "_override_reference_id", reference_id)
        super().__init__(**kwargs)

    @property
    def reference_id(self) -> str:  # type: ignore[override]
        override = getattr(self, "_override_reference_id", None)
        return override if override is not None else super().reference_id


class _PreviewSink:
    def __init__(self, device_index, sample_rate, gain=1.0):
        if not isinstance(device_index, int):
            raise ValueError("device_index must be an integer")
        if not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.gain = float(gain) if isinstance(gain, (int, float)) and math.isfinite(gain) and gain >= 0 else 1.0
        # Fish always emits 44.1 kHz PCM; sample_rate is the physical sink rate.
        self._source_rate = SAMPLE_RATE
        self._resampler = None
        self._channels = 2
        self._stream = None
        self._ring = np.zeros(RING_CAPACITY, dtype=np.float32)
        self._head = 0
        self._count = 0
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self.dropped_frames = 0
        self.errors = 0

    def _enqueue(self, samples):
        size = len(samples)
        if not size:
            return
        with self._lock:
            excess = max(0, self._count + size - RING_CAPACITY)
            self.dropped_frames += excess
            if size >= RING_CAPACITY:
                self._ring[:] = samples[-RING_CAPACITY:]
                self._head = 0
                self._count = RING_CAPACITY
                return
            if excess:
                self._head = (self._head + excess) % RING_CAPACITY
                self._count -= excess
            tail = (self._head + self._count) % RING_CAPACITY
            first = min(size, RING_CAPACITY - tail)
            self._ring[tail:tail + first] = samples[:first]
            if size > first:
                self._ring[:size - first] = samples[first:]
            self._count += size

    def feed(self, samples):
        if self._closing.is_set() or self._stream is None:
            return
        try:
            arr = np.asarray(samples, dtype=np.float32)
        except Exception:
            self.errors += 1
            return
        if arr.size == 0:
            return
        if not arr.flags.writeable or arr.base is not None:
            arr = arr.copy()
        if self._resampler is not None:
            try:
                arr = self._resampler.resample_chunk(arr, last=False)
            except Exception:
                self.errors += 1
                return
        if self.gain != 1.0:
            arr = np.clip(arr * np.float32(self.gain), -1.0, 1.0)
        self._enqueue(arr)

    def finish(self):
        if self._resampler is not None:
            try:
                tail = self._resampler.resample_chunk(
                    np.empty(0, dtype=np.float32), last=True)
                self._enqueue(tail)
            except Exception:
                self.errors += 1
            # The sink is closing after the final tail; do not create a new
            # stream here, which can fail after a valid short preview.
            self._resampler = None

    def _callback(self, outdata, frames, time_info, status):
        try:
            result = np.zeros(frames, dtype=np.float32)
            with self._lock:
                count = min(frames, self._count)
                if count:
                    first = min(count, RING_CAPACITY - self._head)
                    result[:first] = self._ring[self._head:self._head + first]
                    if count > first:
                        result[first:count] = self._ring[:count - first]
                    self._head = (self._head + count) % RING_CAPACITY
                    self._count -= count
            missing = frames - count
            if missing:
                self.dropped_frames += missing
            if outdata.ndim == 1:
                outdata[:] = result
            else:
                outdata[:] = result[:, None]
        except Exception:
            self.errors += 1
            try:
                outdata[:] = 0
            except Exception:
                pass

    def open(self):
        if self._stream is not None:
            return
        rate = self.sample_rate
        stream = None
        try:
            stream = sd.OutputStream(device=self.device_index, samplerate=rate,
                                     channels=2, dtype="float32", latency=0.1,
                                     blocksize=0, callback=self._callback)
        except sd.PortAudioError:
            stream = None
        if stream is None:
            try:
                defaults = sd.query_devices(self.device_index)
            except sd.PortAudioError:
                defaults = None
            native = None
            if isinstance(defaults, dict):
                native_rate = defaults.get("default_samplerate")
                if (isinstance(native_rate, (int, float))
                        and native_rate > 0):
                    native = int(native_rate)
            if native and native != rate:
                try:
                    info = sd.query_devices(self.device_index)
                    channels = min(2, int(info.get("max_output_channels", 1)))
                    stream = sd.OutputStream(device=self.device_index, samplerate=native,
                                             channels=channels, dtype="float32", latency=0.1,
                                             blocksize=0, callback=self._callback)
                    self._channels = channels
                    rate = native
                except sd.PortAudioError:
                    stream = None
        if stream is None:
            raise RuntimeError("Cannot open preview output device")
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        self._stream = stream
        self.sample_rate = rate
        if rate != self._source_rate:
            self._resampler = soxr.ResampleStream(
                self._source_rate, rate, 1, dtype="float32", quality="LQ")
        self._closing.clear()

    def close(self):
        self._closing.set()
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._head = 0
            self._count = 0


class PreviewPlayer:
    def __init__(self, *, tts_factory=None):
        self._lock = threading.Lock()
        self._sink: Optional[_PreviewSink] = None
        self._task: Optional[asyncio.Task] = None
        self._generation = 0
        self._state = "idle"
        self._error: Optional[str] = None
        self._clicked_id: Optional[str] = None
        self._tts_factory = tts_factory

    @property
    def is_active(self):
        with self._lock:
            return self._sink is not None and not self._sink._closing.is_set()

    def _cancel_locked(self):
        previous = self._sink
        self._sink = None
        if previous is not None:
            try:
                previous.close()
            except Exception:
                pass
        task = self._task
        self._task = None
        self._generation += 1
        if task is not None and not task.done():
            task.cancel()

    async def stop(self):
        with self._lock:
            task = self._task
            self._cancel_locked()
        if task is not None and not task.done():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def aclose(self):
        await self.stop()

    def snapshot(self):
        with self._lock:
            sink = self._sink
            state = self._state
            error = self._error
            clicked = self._clicked_id
            active = sink is not None and not sink._closing.is_set()
        if active:
            public_state = state if state in ("loading", "active", "error") else "active"
        elif state == "error":
            public_state = "error"
        else:
            public_state = "idle"
        return {
            "preview_id": clicked if active else None,
            "clickedid": clicked,
            "preview_state": public_state,
            "preview_error": error,
        }

    def _set_state(self, state, *, error=None):
        with self._lock:
            self._state = state
            self._error = error

    async def start(self, voice_id, device_name=None, text=None, gain_db=0.0):
        if not isinstance(voice_id, str) or not voice_id:
            self._set_state("error", error="voice_unselected")
            raise ValueError(t("errors.preview_voice_required"))
        fixed_text = t("main.preview_text")
        resolved_voice = _resolve_voice_id(voice_id)
        if resolved_voice is None:
            self._set_state("error", error="voice_not_found")
            raise ValueError(t("errors.library_voice_missing"))
        key = load_key()
        if not key:
            self._set_state("error", error="key_missing")
            raise ValueError(t("status.fish_key_missing"))
        if device_name:
            device_index = _resolve_output_index(device_name)
            if device_index is None:
                self._set_state("error", error="unsafe_device")
                raise ValueError(t("errors.preview_device_unsafe"))
        else:
            physical = _enumerate_physical_outputs()
            device_index = physical[0][0] if physical else None
        native_rate = None
        if device_index is not None:
            try:
                all_devices = sd.query_devices()
                device_info = (all_devices[device_index] if isinstance(all_devices, list)
                               and 0 <= device_index < len(all_devices) else None)
                candidate_rate = device_info.get("default_samplerate") if isinstance(device_info, dict) else None
                if isinstance(candidate_rate, (int, float)) and candidate_rate > 0:
                    native_rate = int(candidate_rate)
            except sd.PortAudioError:
                native_rate = None
        if device_index is None:
            physical = _enumerate_physical_outputs()
            if not physical:
                self._set_state("error", error="no_device")
                raise ValueError(t("errors.preview_device_missing"))
            device_index = physical[0][0]
        loop = asyncio.get_running_loop()
        with self._lock:
            self._cancel_locked()
            self._generation += 1
            self._clicked_id = resolved_voice
            self._state = "loading"
            self._error = None
            gain = 1.0
            if isinstance(gain_db, (int, float)) and not isinstance(gain_db, bool) and math.isfinite(gain_db):
                gain = 10 ** (max(-24.0, min(12.0, float(gain_db))) / 20)
            sink = _PreviewSink(device_index, native_rate or SAMPLE_RATE, gain=gain)
            try:
                sink.open()
            except Exception as exc:
                sink.close()
                self._state = "error"
                self._error = "device_open_failed"
                raise ValueError(t("errors.monitor_open", exc=exc)) from None
            self._sink = sink
            token = self._generation
            task = loop.create_task(self._run(token, sink, resolved_voice, fixed_text, key))
            self._task = task
        await asyncio.sleep(0)
        if task.done() and not task.cancelled():
            failure = task.exception()
            if failure is not None:
                raise ValueError(t("errors.preview_failed")) from failure
            if self.snapshot()["preview_state"] == "error":
                raise ValueError(t("errors.preview_failed"))
        return token

    async def _run(self, token, sink, reference_id, text, key):
        config = _PreviewConfig(reference_id=reference_id, voice=None,
                                tts_sample_rate=SAMPLE_RATE,
                                output_sample_rate=sink.sample_rate,
                                request_timeout_seconds=15.0)
        emit_total = 0
        max_frames = int(SAMPLE_RATE * MAX_PREVIEW_SECONDS)
        try:
            factory = self._tts_factory or self._default_factory
            async with httpx.AsyncClient(timeout=15.0) as client:
                tts = factory(config, key, client=client)
                async for chunk in tts.stream(text):
                    with self._lock:
                        current = self._generation
                    if token != current:
                        break
                    if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                        continue
                    raw = bytes(chunk)
                    end = len(raw) - len(raw) % 2
                    if not end:
                        continue
                    samples = np.frombuffer(raw[:end], dtype="<i2").astype(np.float32) / 32768.0
                    if samples.size == 0:
                        continue
                    if emit_total + samples.size > max_frames:
                        samples = samples[:max_frames - emit_total]
                    if samples.size == 0:
                        break
                    sink.feed(samples)
                    if emit_total == 0:
                        with self._lock:
                            if token == self._generation and self._state == "loading":
                                self._state = "active"
                    emit_total += samples.size
                    if emit_total >= max_frames:
                        break
            sink.finish()
            drain_budget = 300
            while drain_budget > 0:
                with self._lock:
                    current = self._generation
                if token != current:
                    break
                with sink._lock:
                    remaining = sink._count
                if remaining <= 0:
                    break
                drain_budget -= 1
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass
        except Exception:
            sink.errors += 1
            with self._lock:
                if token == self._generation:
                    self._state = "error"
                    self._error = "preview_failed"
        finally:
            sink.close()
            with self._lock:
                if token == self._generation:
                    if self._state not in ("error",):
                        self._state = "idle"
                    self._sink = None
                    if self._task is asyncio.current_task():
                        self._task = None

    def _default_factory(self, config, key, client):
        return FishTTS(config, key, client=client)
