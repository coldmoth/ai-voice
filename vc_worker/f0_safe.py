"""F0 post-processing for RVC that tolerates fully unvoiced blocks."""
import numpy as np


F0_MIN, F0_MAX = 50.0, 1100.0
MEL_MIN = 1127 * np.log(1 + F0_MIN / 700)
MEL_MAX = 1127 * np.log(1 + F0_MAX / 700)


def f0_features(f0, f0_up_key):
    """Return (coarse int32, f0 Hz) exactly like upstream Pipeline.get_f0 for rmvpe."""
    f0 = np.array(f0, dtype=np.float32, copy=True)
    if len(f0) == 0:
        return np.array([], dtype=np.int32), f0
    uv = f0 == 0
    if uv.any() and (~uv).any():
        f0[uv] = np.interp(np.where(uv)[0], np.where(~uv)[0], f0[~uv])
    f0 *= pow(2, f0_up_key / 12)
    f0bak = f0.copy()
    f0_mel = 1127 * np.log(1 + f0 / 700)
    positive = f0_mel > 0
    f0_mel[positive] = (f0_mel[positive] - MEL_MIN) * 254 / (MEL_MAX - MEL_MIN) + 1
    f0_mel[f0_mel <= 1] = 1
    f0_mel[f0_mel > 255] = 255
    return np.rint(f0_mel).astype(np.int32), f0bak
