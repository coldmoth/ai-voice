"""Reference pitch shifting without loading torch, librosa or an audio engine."""
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from vc_worker.seed_adapter import shift_reference


@pytest.fixture
def pitch_calls(monkeypatch):
    calls = []

    def pitch_shift(reference, **kwargs):
        calls.append((reference, kwargs))
        return reference.astype(np.float64) + 1

    monkeypatch.setitem(sys.modules, 'librosa', SimpleNamespace(effects=SimpleNamespace(pitch_shift=pitch_shift)))
    return calls


def test_zero_pitch_returns_same_reference(pitch_calls):
    ref = np.array([0, 1], dtype=np.float32)
    assert shift_reference(ref, 22050, 0) is ref
    assert pitch_calls == []


def test_reference_shift_passes_sample_rate_and_semitones(pitch_calls):
    ref = np.array([0, 1], dtype=np.float32)
    shifted = shift_reference(ref, 22050, -5)
    assert pitch_calls[0][0] is ref
    assert pitch_calls[0][1] == {'sr': 22050, 'n_steps': -5}
    assert shifted.dtype == ref.dtype
    np.testing.assert_array_equal(shifted, ref + 1)


@pytest.mark.parametrize('steps', [13, -13, 1.5, True])
def test_invalid_reference_pitch_is_rejected(steps, pitch_calls):
    with pytest.raises(ValueError, match='pitch_shift must be an integer in -12..12'):
        shift_reference(np.zeros(4), 22050, steps)
    assert pitch_calls == []
