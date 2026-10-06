import numpy as np
import pytest

from vc_worker.gate import NoiseGate

SR = 40000
FRAME = 400


def constant(db, frames=1):
    return np.full(FRAME * frames, 10 ** (db / 20), dtype=np.float32)


def close_gate(gate):
    gate.process(constant(-120, 50), True, -45)


def test_disabled_is_identical_and_resets_state():
    gate = NoiseGate(SR)
    close_gate(gate)
    block = constant(-20, 10)
    out, db, opened = gate.process(block, False, -45)
    assert np.array_equal(out, block)
    assert opened is True and db == pytest.approx(-20, abs=.1)
    assert (gate.gain, gate.open, gate.hold) == (1.0, True, 0.0)
    gate.reset()
    assert (gate.gain, gate.open, gate.hold) == (1.0, True, 250.0)


@pytest.mark.parametrize('db', [-120, -70])
def test_quiet_closes_after_hold_release(db):
    gate = NoiseGate(SR)
    for _ in range(5):
        out, measured, opened = gate.process(constant(db, 20), True, -45)
    assert max(abs(out)) < 1e-4 and opened is False
    assert measured == pytest.approx(db, abs=.1)


def test_tone_passes_and_hysteresis_keeps_open():
    gate = NoiseGate(SR)
    close_gate(gate)
    tone = (np.sqrt(2) * .1 * np.sin(2 * np.pi * 1000 * np.arange(FRAME * 10) / SR)).astype('float32')
    out, db, opened = gate.process(tone, True, -45)
    assert db == pytest.approx(-20, abs=.1) and opened
    assert np.array_equal(out[FRAME:], tone[FRAME:])
    out, _, opened = gate.process(constant(-48, 100), True, -45)
    assert opened and np.array_equal(out, constant(-48, 100))


def test_hold_release_timing():
    gate = NoiseGate(SR)
    gate.process(constant(-20), True, -45)
    for _ in range(24):
        _, _, opened = gate.process(constant(-70), True, -45)
        assert opened and gate.gain == 1
    out, _, opened = gate.process(constant(-70), True, -45)
    assert not opened and gate.gain == pytest.approx(1 - 1 / 12)
    gate.process(constant(-70, 11), True, -45)
    out, _, _ = gate.process(constant(-70), True, -45)
    assert max(abs(out)) < 1e-10


@pytest.mark.parametrize('attack_ms', [8, 20])
def test_attack_is_linear_without_clicks(attack_ms):
    gate = NoiseGate(SR, attack_ms=attack_ms)
    close_gate(gate)
    out, _, _ = gate.process(np.ones(FRAME * 4, np.float32), True, -45)
    assert np.max(abs(np.diff(out))) <= 1 / gate.attack_samples + 1e-7
    assert out[2 * FRAME] == 1


def test_preroll_uses_original_flags_and_stays_within_block():
    gate = NoiseGate(SR)
    close_gate(gate)
    block = np.concatenate([constant(-70, 7), constant(-20, 3)])
    out, _, opened = gate.process(block, True, -45)
    assert opened
    assert not out[:5 * FRAME].any()
    assert out[5 * FRAME + 1] > 0  # opens two frames before speech
    assert np.array_equal(out[6 * FRAME:7 * FRAME], block[6 * FRAME:7 * FRAME])


@pytest.mark.parametrize('length', [0, 1, 399, 401, 1793])
def test_partial_frames_preserve_length_and_finite_level(length):
    out, db, opened = NoiseGate(SR).process(np.zeros(length, np.float32), True, -45)
    assert len(out) == length and isinstance(db, float) and np.isfinite(db)


@pytest.mark.parametrize('bad', [np.nan, np.inf, -np.inf])
def test_nonfinite_passthrough_does_not_change_state(bad):
    gate = NoiseGate(SR)
    close_gate(gate)
    state = (gate.gain, gate.open, gate.hold)
    block = np.array([bad, .1], np.float32)
    out, db, opened = gate.process(block, True, -45)
    assert out is block and db == -120.0 and opened is True
    assert (gate.gain, gate.open, gate.hold) == state
