import importlib

import numpy as np
import pytest
import soxr


def core():
    try:
        return importlib.import_module("ai_voice.playback")
    except ModuleNotFoundError:
        pytest.fail("Missing PCM buffering and playback implementation")


def test_odd_pcm_chunks_reassemble_and_underrun_is_silence():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000, max_frames=8)
    pcm.push(b"\x00")
    assert np.array_equal(pcm.read(2), [0, 0])
    pcm.push(b"\x40\x00\xc0")
    assert np.array_equal(pcm.read(4), [0.5, -0.5, 0, 0])


def test_buffer_retains_recent_audio_and_never_exceeds_capacity():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000, max_frames=3)
    pcm.push(np.array([1000, 2000, 3000, 4000, 5000], dtype="<i2").tobytes())
    assert pcm.buffered_frames == 3
    np.testing.assert_allclose(pcm.read(3), [3000/32768, 4000/32768, 5000/32768])
    assert pcm.dropped_frames == 2


def test_chunked_44100_resampling_matches_one_continuous_stream():
    pcm = core().PCMBuffer(input_rate=44100, output_rate=48000, max_frames=20000)
    source = np.round(np.sin(np.arange(8820) * 2 * np.pi * 440/44100) * 10000).astype("<i2")
    raw = source.tobytes()
    for start in range(0, len(raw), 173):
        pcm.push(raw[start:start+173])
    pcm.finish()
    assert pcm.buffered_frames == 9600
    expected = soxr.resample(source.astype(np.float32) / 32768, 44100, 48000, quality="LQ")
    np.testing.assert_allclose(pcm.read(9600), expected, atol=2e-6)


def test_callback_outputs_stereo_silence_without_waiting_for_data():
    playback = core().Playback(core().PCMBuffer(input_rate=48000, output_rate=48000), device="AI Voice", sample_rate=48000)
    output = np.ones((128, 2), dtype=np.float32)
    playback.callback(output, 128, None, None)
    assert np.count_nonzero(output) == 0
    playback.buffer.push(np.array([16384, -16384], dtype="<i2").tobytes())
    playback.callback(output, 128, None, None)
    np.testing.assert_equal(output[:2], [[0.5, 0.5], [-0.5, -0.5]])
    assert np.count_nonzero(output[2:]) == 0


def test_callback_does_not_block_on_producer_lock():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000)
    pcm._lock.acquire()
    try:
        np.testing.assert_equal(pcm.read(16), np.zeros(16))
    finally:
        pcm._lock.release()


@pytest.mark.parametrize("settings", [{"input_rate": 44100.5}, {"output_rate": True}, {"max_frames": 1.5}])
def test_pcm_settings_reject_noninteger_audio_sizes(settings):
    with pytest.raises(ValueError):
        core().PCMBuffer(**settings)


def test_callback_applies_gain_and_clamps_to_safe_range():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000)
    playback = core().Playback(pcm, device="AI Voice", sample_rate=48000, gain=1.0)
    pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
    output = np.ones((2, 2), dtype=np.float32)
    playback.callback(output, 2, None, None)
    np.testing.assert_allclose(output[0], [0.5, 0.5])
    np.testing.assert_allclose(output[1], [-0.5, -0.5])

    pcm.push(np.array([16384, 16384], dtype="<i2").tobytes())
    playback.set_gain(0.5)  # -6 dB: ~0.5 amplitude
    output = np.zeros((2, 2), dtype=np.float32)
    playback.callback(output, 2, None, None)
    np.testing.assert_allclose(output[0], [0.25, 0.25], atol=1e-6)
    np.testing.assert_allclose(output[1], [0.25, 0.25], atol=1e-6)

    pcm.push(np.array([30000, 30000], dtype="<i2").tobytes())
    playback.set_gain(10 ** (12 / 20))  # +12 dB; would exceed 1.0 and must clip
    output = np.zeros((2, 2), dtype=np.float32)
    playback.callback(output, 2, None, None)
    assert np.max(np.abs(output)) <= 1.0

    # Silence must remain silence regardless of gain
    pcm.push(np.array([0, 0], dtype="<i2").tobytes())
    playback.set_gain(2.5)
    output = np.ones((2, 2), dtype=np.float32)
    playback.callback(output, 2, None, None)
    assert np.count_nonzero(output) == 0


def test_callback_unity_gain_keeps_amplitude_unchanged():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000)
    playback = core().Playback(pcm, device="AI Voice", sample_rate=48000, gain=1.0)
    pcm.push(np.array([8192, 8192], dtype="<i2").tobytes())
    output = np.zeros((2, 2), dtype=np.float32)
    playback.callback(output, 2, None, None)
    np.testing.assert_allclose(output, [[0.25, 0.25]] * 2, atol=1e-6)


def test_stereo_channels_track_invariantly():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000)
    playback = core().Playback(pcm, device="AI Voice", sample_rate=48000, gain=0.7)
    pcm.push(np.array([10000, -10000, 20000, -20000], dtype="<i2").tobytes())
    output = np.zeros((4, 2), dtype=np.float32)
    playback.callback(output, 4, None, None)
    left = output[:, 0]
    np.testing.assert_allclose(left, np.array([10000, -10000, 20000, -20000]) / 32768 * 0.7, atol=1e-6)
    np.testing.assert_allclose(output[:, 0], output[:, 1])


@pytest.mark.parametrize("gain", [True, float("inf"), float("nan"), -1, "1"])
def test_playback_rejects_invalid_gain_before_callback(gain):
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000)
    with pytest.raises(ValueError):
        core().Playback(pcm, sample_rate=48000, gain=gain)
    playback = core().Playback(pcm, sample_rate=48000)
    with pytest.raises(ValueError):
        playback.set_gain(gain)


def test_monitor_receives_pre_gain_audio():
    pcm = core().PCMBuffer(input_rate=48000, output_rate=48000)
    playback = core().Playback(pcm, device="AI Voice", sample_rate=48000, gain=0.5)
    fed = []

    class Monitor:
        errors = 0

        def feed(self, samples):
            fed.append(np.array(samples))

    playback.attach_monitor(Monitor())
    pcm.push(np.array([16384, 16384], dtype="<i2").tobytes())
    output = np.zeros((2, 2), dtype=np.float32)
    playback.callback(output, 2, None, None)
    np.testing.assert_allclose(output, [[0.25, 0.25]] * 2, atol=1e-6)
    np.testing.assert_allclose(fed[0], [0.5, 0.5], atol=1e-6)
