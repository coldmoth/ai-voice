import json
from datetime import date, timedelta

from ai_voice.usage import UsageTracker
from helpers import TEST_VOICE_ID


def make(tmp_path, day=date(2026, 10, 2)):
    d = [day]
    t = [0.0]
    return UsageTracker(tmp_path / "usage.json", today=lambda: d[0], clock=lambda: t[0]), d, t


def test_day_and_month_counts(tmp_path):
    u, d, t = make(tmp_path)
    u.add(100)
    d[0] = date(2026, 10, 3)
    u.add(50)
    d[0] = date(2026, 9, 30)
    u.add(7)
    d[0] = date(2026, 10, 3)
    assert u.totals() == {"today": 50, "month": 150, "limit": None}


def test_prunes_to_62_days_and_stores_only_numbers(tmp_path):
    u, d, t = make(tmp_path, date(2026, 1, 1))
    for i in range(70):
        d[0] = date(2026, 1, 1) + timedelta(days=i)
        u.add(1)
    assert len(u.days) == 62
    u.flush()
    data = json.loads((tmp_path / "usage.json").read_text())
    assert set(data) == {"days"} and all(isinstance(v, int) for v in data["days"].values())


def test_corrupt_json_starts_empty(tmp_path):
    (tmp_path / "usage.json").write_text("{not json")
    u, _, _ = make(tmp_path)
    assert u.days == {}
    u.add(5)
    assert u.totals()["today"] == 5


def test_writes_coalesced_and_flushed_on_demand(tmp_path):
    u, d, t = make(tmp_path)
    u.add(1)  # first write immediately
    u.add(2)  # within 5 s: held back
    assert json.loads((tmp_path / "usage.json").read_text())["days"]["2026-10-02"] == 1
    t[0] = 6.0
    u.add(3)
    assert json.loads((tmp_path / "usage.json").read_text())["days"]["2026-10-02"] == 6
    u.add(4)
    u.flush()
    assert json.loads((tmp_path / "usage.json").read_text())["days"]["2026-10-02"] == 10


def test_cache_hit_not_counted(tmp_path):
    import asyncio
    from ai_voice.config import Config
    from ai_voice.gui_audio import AudioSession
    from tests.test_gui_audio import FakePlayback
    import ai_voice.pipeline as pl

    pl.Playback = FakePlayback
    counted = []

    class TTS:
        def __init__(self, *_a, **_k): pass
        async def stream(self, text):
            yield b"\0\0" * 100

    async def go():
        s = AudioSession(Config(voice=TEST_VOICE_ID), "k", tts_factory=TTS, on_chars=counted.append)
        run = asyncio.create_task(s.run())
        for _ in range(2):
            s.submit("привет")
            await asyncio.wait_for(s.engine.queue.join(), 2)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    asyncio.run(go())
    assert counted == [6]
