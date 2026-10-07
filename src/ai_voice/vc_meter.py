"""Lightweight input level meter, with no conversion or output stream."""
from collections import deque
import math
import time

import numpy as np

from .i18n import t


class InputMeter:
    def __init__(self, sd=None, clock=time.monotonic, sample_rate=16000, block=800):
        self.sd, self.clock = sd, clock
        self.sample_rate, self.block = sample_rate, block
        self._samples = deque(maxlen=200)
        self._stream = None
        self._device = None

    @property
    def running(self):
        return self._stream is not None

    def start(self, device):
        if self.running and device == self._device:
            return
        self.stop()
        stream = None
        try:
            from .devices import stream_extra_settings
            if self.sd is None:
                import sounddevice
                self.sd = sounddevice
            stream = self.sd.InputStream(device=device, samplerate=self.sample_rate,
                extra_settings=stream_extra_settings(device, kind="input"),
                channels=1, dtype='float32', blocksize=self.block, callback=self._callback)
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise RuntimeError(t("errors.microphone_open", exc=exc)) from exc
        self._stream, self._device = stream, device

    def stop(self):
        stream, self._stream = self._stream, None
        self._device = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def _callback(self, indata, frames, timing, status):
        rms = float(np.sqrt(np.mean(indata[:, 0].astype(np.float64) ** 2)))
        self.record(20 * math.log10(max(rms, 1e-6)))

    def record(self, level_db):
        if math.isfinite(level_db):
            self._samples.append((self.clock(), float(level_db)))

    def level_db(self):
        if not self._samples:
            return None
        timestamp, level = self._samples[-1]
        return level if self.clock() - timestamp < 1.0 else None

    def samples_since(self, t):
        return [level for timestamp, level in list(self._samples) if timestamp >= t]
