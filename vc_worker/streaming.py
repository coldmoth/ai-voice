"""Causal fixed-hop conversion and normalized-correlation SOLA (no audio devices)."""
from dataclasses import dataclass
import math
import time
import numpy as np


@dataclass(frozen=True)
class Config:
    sample_rate: int = 40000
    hop_ms: int = 256
    context_ms: int = 500
    crossfade_ms: int = 64
    search_ms: int = 16

    def __post_init__(self):
        for value in self.__dict__.values():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError('stream config values must be integers')
        if not (8000 <= self.sample_rate <= 96000 and 160 <= self.hop_ms <= 1000
                and 0 <= self.context_ms <= 1000 and 40 <= self.crossfade_ms <= 80
                and 0 <= self.search_ms <= self.crossfade_ms):
            raise ValueError('invalid streaming configuration')

    def frames(self, ms):
        return round(self.sample_rate * ms / 1000)

    @property
    def hop(self):
        return self.frames(self.hop_ms)

    @property
    def fade(self):
        return self.frames(self.crossfade_ms)

    @property
    def context(self):
        return self.frames(self.context_ms)

    @property
    def search(self):
        return self.frames(self.search_ms)


def resample(audio, source_rate, target_rate):
    """Polyphase resampling, imported only when a real engine changes sample rate."""
    if source_rate == target_rate:
        return audio
    from scipy.signal import resample_poly
    divisor = math.gcd(int(source_rate), int(target_rate))
    return resample_poly(audio, target_rate // divisor, source_rate // divisor).astype(np.float32)


class Streaming:
    def __init__(self, engine, config=Config(), clock=time.perf_counter, budget=lambda: None):
        self.engine, self.config, self.clock, self.budget = engine, config, clock, budget
        self.history = np.zeros(config.context + config.fade, np.float32)
        self.tail = np.zeros(config.fade, np.float32)
        self.first = True
        self.dropped_blocks = 0
        self.processing_ms = 0.0
        self.last_offset = 0

    def process(self, block, params=None):
        started = self.clock()
        c = self.config
        block = np.asarray(block, dtype=np.float32)
        if block.shape != (c.hop,) or not np.isfinite(block).all():
            raise ValueError('expected one finite mono hop')
        self.budget()
        # Overlap is previous captured audio, never future input/lookahead.
        window = np.concatenate((self.history, block))
        self.history = window[-len(self.history):].copy()
        params = params or {}
        if params.get('bypass'):
            # Own voice: the same input window goes through the shared resample/SOLA path,
            # so switching to and from the model crossfades like any other block.
            # The engine's output rate is not exposed and the result is resampled back to
            # c.sample_rate anyway, so the round trip is skipped.
            audio, rate = window.copy(), c.sample_rate
        else:
            audio, rate = self.engine.convert(window, c.sample_rate, params)
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all() or rate <= 0:
            raise ValueError('engine returned invalid waveform')
        audio = resample(audio, rate, c.sample_rate)
        # Engines may round the final vocoder frame; pad/crop to the input duration.
        audio = np.pad(audio, (0, max(0, len(window) - len(audio))))[:len(window)]
        self.budget()
        padded = np.pad(audio, (c.search, c.search))
        base = c.context + c.search
        offset = 0
        if not self.first and c.search and np.linalg.norm(self.tail) > 1e-8:
            region = padded[base - c.search:base + c.search + c.fade]
            numerator = np.correlate(region, self.tail, mode='valid')
            energy = np.convolve(region * region, np.ones(c.fade), mode='valid')
            scores = numerator / np.sqrt(np.maximum(energy, 1e-20))
            offset = int(np.argmax(scores)) - c.search
        candidate = padded[base + offset:base + offset + c.hop + c.fade].copy()
        weight = np.linspace(0, 1, c.fade, dtype=np.float32)
        candidate[:c.fade] = self.tail * (1 - weight) + candidate[:c.fade] * weight
        self.processing_ms = (self.clock() - started) * 1000
        if self.processing_ms > c.hop_ms:
            self.dropped_blocks += 1
            self.tail.fill(0)
            self.first = True
            return np.zeros(c.hop, np.float32)
        self.tail = candidate[c.hop:].copy()
        self.first = False
        self.last_offset = offset
        return candidate[:c.hop]

    def finish(self):
        tail = self.tail * np.linspace(1, 0, self.config.fade, dtype=np.float32)
        self.tail.fill(0)
        self.first = True
        return tail
