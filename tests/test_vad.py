"""The pure VAD and preroll cases from native/tests/VADTests.swift."""
import numpy as np

from ai_voice.vad import VAD


def frame(db, ms):
    return np.full(ms * 16, 10 ** (db / 20), dtype=np.float32)


def test_swift_vad_boundaries_and_reset():
    vad = VAD(100, 450, -45, preroll_ms=200)
    inputs = [(-60, 100), (-30, 50), (-60, 50), (-30, 50), (-30, 50),
              (-60, 400), (-30, 50), (-60, 400), (-60, 50)]
    for index, (db, ms) in enumerate(inputs):
        segments = vad.feed(frame(db, ms))
        assert vad.active == (4 <= index < 8)
        assert len(segments) == (1 if index == 8 else 0)
        if segments:
            np.testing.assert_array_equal(segments[0], np.concatenate(
                [frame(db, ms) for db, ms in inputs]))
    assert vad.feed(frame(-60, 450)) == []
    assert not vad.active
    assert vad.feed(frame(-30, 100)) == []
    assert vad.active
    vad.reset()
    assert vad.feed(frame(-30, 50)) == []
    assert not vad.active


def test_swift_configurable_threshold_and_fast_endpoint():
    vad = VAD(20, 80, -20, preroll_ms=40)
    assert vad.feed(frame(-30, 50)) == []
    assert not vad.active
    assert vad.feed(frame(-10, 20)) == []
    assert vad.active
    segments = vad.feed(frame(-60, 80))
    assert not vad.active
    np.testing.assert_array_equal(segments[0], np.concatenate(
        [frame(-30, 50), frame(-10, 20), frame(-60, 80)]))


def test_swift_preroll_window_and_clear_between_phrases():
    vad = VAD(100, 450, -45, preroll_ms=200)
    # Frame labels stay below threshold, just as Swift's generic Preroll<Int>.
    chunks = [np.full(ms * 16, label / 10000, dtype=np.float32)
              for label, ms in [(1, 100), (2, 100), (3, 100), (4, 100), (5, 50), (6, 50)]]
    for chunk in chunks[:4]:
        vad.feed(chunk)
    np.testing.assert_array_equal(np.concatenate(vad.frames), np.concatenate(chunks[1:4]))
    for chunk in chunks[4:]:
        vad.feed(chunk)
    np.testing.assert_array_equal(np.concatenate(vad.frames), np.concatenate(chunks[2:]))
    vad.reset()
    assert vad.frames == []
    vad.feed(frame(-30, 100))
    segments = vad.feed(frame(-60, 450))
    np.testing.assert_array_equal(segments[0], np.concatenate([frame(-30, 100), frame(-60, 450)]))
    assert vad.frames == []


def test_default_preroll_and_copied_input():
    vad = VAD()
    assert vad.preroll_ms == 40
    chunk = frame(-30, 100)
    original = chunk.copy()
    vad.feed(chunk)
    chunk[:] = 0
    segments = vad.feed(frame(-60, 450))
    np.testing.assert_array_equal(segments[0][:1600], original)
