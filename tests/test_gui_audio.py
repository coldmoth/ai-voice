import asyncio
import pytest

from ai_voice.config import Config
from helpers import TEST_VOICE_ID


class FakePlayback:
    opened = 0

    def __init__(self, buffer, **_):
        self.buffer = buffer

    def __enter__(self):
        type(self).opened += 1
        self.task = asyncio.create_task(self.drain())
        return self

    async def drain(self):
        while True:
            self.buffer.read(4096)
            await asyncio.sleep(.001)

    def __exit__(self, *_):
        self.task.cancel()
        type(self).opened -= 1
        self.buffer.clear()


@pytest.mark.asyncio
async def test_switch_cancels_synthesis_clears_queue_without_restarting_asr(monkeypatch):
    from ai_voice.gui_audio import AudioSession
    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    class ASR:
        starts = 0
        async def events(self):
            self.starts += 1
            yield {"event": "ready", "utterance_id": "", "time_ms": 0}
            await asyncio.Event().wait()

    class TTS:
        def __init__(self, config, *_args, **_kwargs):
            self.voice = config.voice
        async def stream(self, text):
            calls.append((self.voice, text))
            if self.voice == TEST_VOICE_ID:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            yield b"\0\0" * 100

    asr = ASR()
    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key", asr=asr, tts_factory=TTS)
    run = asyncio.create_task(session.run())
    await session.ready.wait()
    session.submit("первая")
    await entered.wait()
    session.submit("старая очередь")
    await session.change_voice("a" * 32)
    assert cancelled.is_set()
    assert session.engine.queue.empty()
    assert session.engine.buffer.buffered_frames == 0
    assert asr.starts == 1
    session.submit("новая")
    await asyncio.wait_for(session.engine.queue.join(), 1)
    assert calls == [(TEST_VOICE_ID, "первая"), ("a" * 32, "новая")]
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert FakePlayback.opened == 0


@pytest.mark.asyncio
async def test_text_mode_never_starts_asr_and_stops_cleanly(monkeypatch):
    from ai_voice.gui_audio import AudioSession
    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    calls = []

    class TTS:
        def __init__(self, *_args, **_kwargs): pass
        async def stream(self, text):
            calls.append(text)
            yield b"\0\0" * 100

    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key", tts_factory=TTS)
    run = asyncio.create_task(session.run())
    await session.ready.wait()
    session.submit("Мяу")
    await asyncio.wait_for(session.engine.queue.join(), 1)
    assert calls == ["Мяу"]
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert FakePlayback.opened == 0


@pytest.mark.asyncio
async def test_gui_latency_is_reported_once_after_first_pcm_and_not_on_error(monkeypatch):
    from ai_voice.fish_tts import TTSError
    from ai_voice.gui_audio import AudioSession

    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    clock = [100.0]
    import time as _time
    import types
    fake_time = types.SimpleNamespace(**{n: getattr(_time, n) for n in dir(_time) if not n.startswith("_")})
    fake_time.monotonic = lambda: clock[0]
    monkeypatch.setattr("ai_voice.gui_audio.time", fake_time)
    latencies = []
    generations = []

    class TTS:
        def __init__(self, *_args, **_kwargs): pass
        async def stream(self, text):
            if text == "ошибка":
                raise TTSError("временная ошибка", status_code=500)
            clock[0] += 0.25
            yield b"\0\0" * 100

    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key", tts_factory=TTS,
                           on_generation_start=lambda: generations.append(True),
                           on_latency=latencies.append)
    assert latencies == []
    run = asyncio.create_task(session.run())
    await session.ready.wait()

    session.submit("первая")
    await asyncio.wait_for(session.engine.queue.join(), 1)
    assert latencies == [pytest.approx(0.25)]
    assert len(generations) == 1

    session.submit("ошибка")
    await asyncio.wait_for(session.engine.queue.join(), 1)
    assert len(latencies) == 1
    assert len(generations) == 2

    session.submit("вторая")
    await asyncio.wait_for(session.engine.queue.join(), 1)
    assert len(latencies) == 2
    assert len(generations) == 3

    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert FakePlayback.opened == 0


def test_text_and_voice_validation_do_not_open_devices():
    from ai_voice.gui_audio import AudioSession
    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key")
    for text in ["", " ", "x" * 1001, None]:
        with pytest.raises(ValueError): session.submit(text)
    assert session.engine.queue.empty()


@pytest.mark.asyncio
async def test_auth_error_stops_session_and_clears_audio(monkeypatch):
    from ai_voice.gui_audio import AudioSession
    from ai_voice.fish_tts import TTSError
    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    class TTS:
        def __init__(self, *_args, **_kwargs): pass
        async def stream(self, text):
            raise TTSError("Ключ не принят", status_code=401)
            yield b""
    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key", tts_factory=TTS)
    run = asyncio.create_task(session.run())
    await session.ready.wait()
    session.submit("Привет")
    with pytest.raises(TTSError): await run
    assert session.engine.buffer.buffered_frames == 0
    assert FakePlayback.opened == 0


def test_update_settings_returns_config_without_restart():
    from ai_voice.gui_audio import AudioSession
    from ai_voice.playback import PCMBuffer
    from dataclasses import replace
    cfg = Config(voice=TEST_VOICE_ID)
    session = AudioSession(replace(cfg), "test-key")
    # initialize playback manually since AudioSession.run is not called here
    session.engine.buffer = PCMBuffer(input_rate=cfg.tts_sample_rate, output_rate=cfg.output_sample_rate)
    session.engine.playback = type(session.engine.playback)(session.engine.buffer, device="AI Voice",
                                                             sample_rate=cfg.output_sample_rate,
                                                             gain=cfg.linear_gain())
    cfg2 = session.update_settings(output_gain_db=-3)
    assert cfg2.output_gain_db == -3
    assert session.engine.playback._gain == pytest.approx(10 ** (-3 / 20))


@pytest.mark.asyncio
async def test_apply_preferences_to_active_session_without_microphone(monkeypatch):
    """apply_preferences must update live gain and config but not touch ASR / devices."""
    from ai_voice.desktop_control import DesktopControl
    from ai_voice.gui_audio import AudioSession
    from ai_voice.playback import PCMBuffer

    catalog_data = {"voices": [], "favorite_ids": [],
                    "voice_id": "db89e349112e44dca6820e0cb2d414cc",
                    "input_device": "MIC", "output_device": "AI Voice",
                    "output_gain_db": 0.0, "normalize_loudness": True}

    class FakeCatalog:
        def __init__(self):
            self.data = dict(catalog_data)
        def preferences(self):
            return {k: v for k, v in self.data.items() if k != "voices"}
        def update(self, values):
            self.data.update(values)
            return self.preferences()
        def slug(self, voice_id):
            return TEST_VOICE_ID

    cat = FakeCatalog()
    control = DesktopControl(cat)
    buf = PCMBuffer(input_rate=44100, output_rate=48000)
    from ai_voice.playback import Playback
    playback = Playback(buf, device="AI Voice", sample_rate=48000, gain=1.0)
    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key")
    session.engine.buffer = buf
    session.engine.playback = playback
    control.session = session
    control.task = type("T", (), {"cancel": lambda self: None})()
    await control.apply_preferences({"output_gain_db": -6, "normalize_loudness": False})
    assert playback._gain == pytest.approx(10 ** (-6 / 20))
    assert session.engine.config.normalize_loudness is False
    assert session.engine.config.output_gain_db == -6


@pytest.mark.asyncio
async def test_apply_preferences_persist_when_no_session():
    from ai_voice.desktop_control import DesktopControl

    class FakeCatalog:
        def __init__(self):
            self.data = {"voice_id": "db89e349112e44dca6820e0cb2d414cc",
                         "input_device": "MIC", "output_device": "AI Voice",
                         "output_gain_db": 0.0, "normalize_loudness": True,
                         "voices": [], "favorite_ids": []}
        def preferences(self):
            return {k: v for k, v in self.data.items() if k != "voices"}
        def slug(self, _):
            return TEST_VOICE_ID
        def update(self, values):
            self.data.update(values)
            return self.preferences()

    control = DesktopControl(FakeCatalog())
    assert control.session is None
    prefs = await control.apply_preferences({"output_gain_db": 5})
    assert prefs["output_gain_db"] == 5


@pytest.mark.asyncio
async def test_typed_text_is_not_dropped_by_age_and_evictions_warn(monkeypatch):
    from ai_voice.gui_audio import AudioSession

    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    calls, notes = [], []

    class TTS:
        def __init__(self, *_args, **_kwargs): pass
        async def stream(self, text):
            calls.append(text)
            yield b"\0\0" * 100

    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key", tts_factory=TTS,
                           notify=lambda state, message="": notes.append((state, message)))
    session.submit("один")
    session.submit("два")
    session.submit("три")
    session.submit("четыре")
    session.submit("пять")  # queue (maxsize 4) is full: "один" is evicted, user is told
    assert any(state == "warning" for state, _ in notes)
    notes.clear()
    session.engine.queue._queue.appendleft((0.0, "устаревшая речь"))  # untyped, older than max_age
    session.engine.queue._unfinished_tasks += 1
    session.engine.max_age = -1.0  # everything is "too old"; typed text must still be spoken
    run = asyncio.create_task(session.run())
    await asyncio.wait_for(session.engine.queue.join(), 2)
    assert calls == ["два", "три", "четыре", "пять"]
    assert any(state == "warning" for state, _ in notes)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_latency_breakdown_reported(monkeypatch):
    from ai_voice.gui_audio import AudioSession

    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    got = []

    class TTS:
        def __init__(self, *_args, **_kwargs): pass
        async def stream(self, text):
            yield b"\0\0" * 100

    session = AudioSession(Config(voice=TEST_VOICE_ID), "test-key", tts_factory=TTS,
                           on_latency_breakdown=got.append)
    session.submit("привет")
    run = asyncio.create_task(session.run())
    await asyncio.wait_for(session.engine.queue.join(), 2)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert got and got[0]["asr_ms"] is None and got[0]["cached"] is False
    assert got[0]["total_ms"] >= got[0]["tts_first_ms"] >= 0
