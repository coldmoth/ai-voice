"""Frame-based noise gate; levels are measured before gain is applied."""
import numpy as np


class NoiseGate:
    def __init__(self, sample_rate: int, attack_ms=8.0, hold_ms=250.0,
                 release_ms=120.0, frame_ms=10.0, hysteresis_db=6.0, preroll_frames=2):
        self.frame = round(sample_rate * frame_ms / 1000)
        self.frame_ms, self.hold_ms = frame_ms, hold_ms
        self.attack_samples = sample_rate * attack_ms / 1000
        self.release_samples = sample_rate * release_ms / 1000
        self.hysteresis_db, self.preroll_frames = hysteresis_db, preroll_frames
        self.reset()

    def reset(self):
        self.gain, self.open, self.hold = 1.0, True, self.hold_ms

    @staticmethod
    def _db(block):
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2))) if len(block) else 0.0
        return float(20 * np.log10(max(rms, 1e-6)))

    def process(self, block: np.ndarray, enabled: bool, threshold_db: float):
        if not np.isfinite(block).all():
            return block, -120.0, True
        level_db = self._db(block)
        if not enabled:
            self.gain, self.open, self.hold = 1.0, True, 0.0
            return block, level_db, True
        frames = [block[i:i + self.frame] for i in range(0, len(block), self.frame)]
        flags = []
        for frame in frames:
            db = self._db(frame)
            if db >= threshold_db:
                self.open, self.hold = True, self.hold_ms
            elif self.open and db < threshold_db - self.hysteresis_db:
                self.hold -= self.frame_ms
                if self.hold <= 0:
                    self.open = False
            flags.append(self.open)
        rolled = [any(flags[i:i + self.preroll_frames + 1]) for i in range(len(flags))]
        out = np.empty_like(block)
        offset = 0
        for frame, opened in zip(frames, rolled):
            previous = self.gain
            if opened:
                self.gain = min(1.0, previous + self.frame / self.attack_samples)
            else:
                self.gain = max(0.0, previous - self.frame / self.release_samples)
            n = len(frame)
            out[offset:offset + n] = frame * np.linspace(previous, self.gain, n, endpoint=False)
            offset += n
        return out, level_db, bool(rolled[-1]) if rolled else self.open
