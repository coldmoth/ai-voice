"""Integration regressions for the real ASR event consumer and monitor lifecycle."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from ai_voice.catalog import Catalog
from ai_voice.config import Config
from ai_voice.desktop_control import DesktopControl, MAX_TRANSCRIPT_CHARS
from ai_voice.gui_audio import AudioSession
from helpers import TEST_VOICE_ID, write_library


@pytest.mark.asyncio
async def test_real_asr_event_consumer_delivers_partial_and_final(tmp_path):
    control = DesktopControl(Catalog(tmp_path / "preferences.json"))

    class ASR:
        async def events(self):
            yield {"event": "ready"}
            yield {"event": "partial", "text": "Первая фра"}
            yield {"event": "final", "text": "Первая фраза."}
            yield {"event": "partial", "text": "Следующая"}

    session = AudioSession(Config(voice=TEST_VOICE_ID), "unused", asr=ASR(), asr_listener=control._on_asr_event)
    observed = []
    async for event in session._events():
        observed.append(control.snapshot())
    assert session.ready.is_set()
    assert observed[1]["transcript_partial"] == "Первая фра"
    assert observed[2]["transcript_final"] == "Первая фраза."
    assert observed[2]["transcript_partial"] == ""
    assert observed[3]["transcript_final"] == "Первая фраза."
    assert observed[3]["transcript_partial"] == "Следующая"
    control._on_asr_event({"event": "partial", "text": "x" * 5000})
    assert len(control.snapshot()["transcript_partial"]) <= MAX_TRANSCRIPT_CHARS
    assert "transcript_final" not in control.catalog.preferences()


class Monitor:
    def __init__(self):
        self.closed = False
        self.gains = []
        self._active = True
        self.errors = self.dropped_frames = 0

    def close(self):
        self.closed = True

    def set_gain(self, gain):
        self.gains.append(gain)

    def actual_rate(self):
        return 48000


def active_control(tmp_path, monkeypatch):
    catalog = Catalog(write_library(tmp_path / "preferences.json",
        [{"id": TEST_VOICE_ID, "name": "Test voice"}], TEST_VOICE_ID))
    catalog.update({"monitor_enabled": True, "monitor_device": "Headphones"})
    control = DesktopControl(catalog)
    control._monitor = Monitor()
    attached = []
    playback = SimpleNamespace(attach_monitor=attached.append,
                               detach_monitor=lambda m: attached.remove(m) if m in attached else None)
    control.session = SimpleNamespace(engine=SimpleNamespace(config=Config(voice=TEST_VOICE_ID), playback=playback),
                                      update_settings=lambda **values: None)
    monkeypatch.setattr(control, "_monitor_validation_error", lambda prefs: None)
    return control


@pytest.mark.asyncio
async def test_monitor_gain_update_keeps_existing_stream(tmp_path, monkeypatch):
    control = active_control(tmp_path, monkeypatch)
    previous = control._monitor
    monkeypatch.setattr(control, "_open_replacement_monitor",
                        lambda prefs: pytest.fail("A gain adjustment must not open another stream"))
    prefs = await control.apply_preferences({"monitor_gain_db": -6})
    assert prefs["monitor_gain_db"] == -6
    assert control._monitor is previous and not previous.closed
    assert previous.gains == [pytest.approx(10 ** (-6 / 20))]


@pytest.mark.asyncio
async def test_failed_monitor_switch_keeps_saved_and_active_device(tmp_path, monkeypatch):
    control = active_control(tmp_path, monkeypatch)
    previous, saved = control._monitor, control.catalog.preferences()

    def unavailable(prefs):
        raise RuntimeError("Output is unavailable")

    monkeypatch.setattr(control, "_open_replacement_monitor", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        await control.apply_preferences({"monitor_device": "Other headphones"})
    assert control.catalog.preferences() == saved
    assert control._monitor is previous and not previous.closed


@pytest.mark.asyncio
async def test_disk_failure_closes_candidate_without_replacing_monitor(tmp_path, monkeypatch):
    control = active_control(tmp_path, monkeypatch)
    previous, candidate = control._monitor, Monitor()
    saved = control.catalog.preferences()
    monkeypatch.setattr(control, "_open_replacement_monitor", lambda prefs: candidate)
    monkeypatch.setattr(control.catalog, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        await control.apply_preferences({"monitor_device": "Other headphones"})
    assert control.catalog.preferences() == saved
    assert control._monitor is previous and not previous.closed
    assert candidate.closed


def test_monitor_continues_after_repeated_clear(monkeypatch):
    import time
    from test_monitor import _make_sink

    sink = _make_sink(monkeypatch)
    sink.start()
    try:
        for value in [0.25, -0.5, 0.75]:
            sink.clear()
            sink.feed(np.full(256, value, dtype=np.float32))
            deadline = time.monotonic() + 1
            while sink._out_count < 256 and time.monotonic() < deadline:
                time.sleep(.005)
            assert sink._out_count >= 256
            out = np.zeros((256, 2), dtype=np.float32)
            sink._callback(out, 256, None, None)
            np.testing.assert_allclose(out, value)
            assert sink._worker.is_alive()
    finally:
        sink.close()


@pytest.mark.asyncio
async def test_validation_error_does_not_flip_running_session_to_error(tmp_path):
    control = DesktopControl(Catalog(tmp_path / "preferences.json"))
    control.session = SimpleNamespace()
    control.notify("listening")
    with pytest.raises(ValueError):
        await control.command({"action": "speak", "text": "   "})
    assert control.snapshot()["state"] == "listening"
    control.session = None
    with pytest.raises(ValueError):
        await control.command({"action": "speak", "text": "   "})
    assert control.snapshot()["state"] == "error"


@pytest.mark.asyncio
async def test_unrelated_save_does_not_reapply_input_gain_and_rollback_restores_output(tmp_path):
    control = DesktopControl(Catalog(write_library(tmp_path / "preferences.json",
        [{"id": TEST_VOICE_ID, "name": "Test voice"}], TEST_VOICE_ID, language="en")))
    calls = {"input": 0, "settings": []}

    async def set_input_gain(db):
        calls["input"] += 1
        raise RuntimeError("device gone")

    control.session = SimpleNamespace(set_input_gain=set_input_gain,
                                      update_settings=lambda **kw: calls["settings"].append(kw))
    control.catalog.update({"output_gain_db": 0.0})
    await control.apply_preferences({"favorite_ids": []})
    assert calls["input"] == 0
    with pytest.raises(ValueError) as err:
        await control.apply_preferences({"output_gain_db": 6.0, "input_gain_db": 3.0})
    assert 'input gain' in str(err.value)
    assert control.catalog.preferences()["output_gain_db"] == 0.0
    assert calls["settings"][-1] == {"output_gain_db": 0.0}


def test_long_transcript_is_truncated_with_ellipsis(tmp_path):
    control = DesktopControl(Catalog(tmp_path / "preferences.json"))
    control.update_transcript(final="слово " * 100)
    final = control.snapshot()["transcript_final"]
    assert len(final) <= MAX_TRANSCRIPT_CHARS
    assert final.endswith("…")
    control.update_transcript(final="коротко")
    assert control.snapshot()["transcript_final"] == "коротко"
