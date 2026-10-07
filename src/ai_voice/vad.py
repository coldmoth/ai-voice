"""Duration-based VAD with the native helper's whole-frame preroll policy."""
import math

import numpy as np


class VAD:
    def __init__(self, min_speech_ms: int = 100, min_silence_ms: int = 450,
                 threshold: float = -45, preroll_ms: int = 40):
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self.threshold = threshold
        self.preroll_ms = preroll_ms
        self.reset()

    def reset(self):
        self.active = False
        self._candidate_ms = self._silent_ms = self._buffer_ms = 0.0
        self.frames: list[np.ndarray] = []

    def feed(self, frame: np.ndarray) -> list[np.ndarray]:
        frame = np.asarray(frame, dtype=np.float32).reshape(-1).copy()
        if not frame.size:
            return []
        duration = frame.size / 16.0
        db = 10 * math.log10(max(float(np.mean(frame.astype(np.float64) ** 2)), 1e-12))
        self.frames.append(frame)
        if not self.active:
            self._buffer_ms += duration
            capacity = self.preroll_ms + self.min_speech_ms
            while len(self.frames) > 1 and self._buffer_ms - self.frames[0].size / 16.0 >= capacity:
                self._buffer_ms -= self.frames.pop(0).size / 16.0
        if db >= self.threshold:
            self._silent_ms = 0
            if not self.active:
                self._candidate_ms += duration
                if self._candidate_ms >= self.min_speech_ms:
                    self.active = True
        elif self.active:
            self._silent_ms += duration
            if self._silent_ms >= self.min_silence_ms:
                utterance = np.concatenate(self.frames)
                self.reset()
                return [utterance]
        else:
            self._candidate_ms = 0
        return []
