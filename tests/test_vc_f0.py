import numpy as np

from vc_worker.f0_safe import f0_features


def test_fully_unvoiced_is_silent_without_output(capsys):
    coarse, f0 = f0_features([0, 0, 0], 0)
    assert coarse.tolist() == [1, 1, 1]
    assert f0.tolist() == [0, 0, 0]
    assert capsys.readouterr().out == ''
    assert capsys.readouterr().err == ''


def test_interpolates_and_applies_pitch_shift():
    source = np.array([0, 100, 0, 200, 0], np.float32)
    before = source.copy()
    coarse, f0 = f0_features(source, 0)
    assert coarse.tolist() == [20, 20, 37, 54, 54]
    assert f0.tolist() == [100, 100, 150, 200, 200]
    assert np.array_equal(source, before)
    coarse, f0 = f0_features(source, 12)
    assert coarse.tolist() == [54, 54, 84, 112, 112]
    assert f0.tolist() == [200, 200, 300, 400, 400]


def test_empty_input_returns_empty_arrays():
    coarse, f0 = f0_features(np.array([], dtype=np.float32), 0)
    assert coarse.dtype == np.int32 and coarse.size == 0
    assert f0.size == 0
