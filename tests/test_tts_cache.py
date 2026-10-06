import asyncio

import pytest

from ai_voice.config import Config
from ai_voice.tts_cache import TTSCache
from tests.test_gui_audio import FakePlayback
from helpers import TEST_VOICE_ID


def k(c, voice, text):
    return c.key(voice, "m", 44100, True, text)


def test_lru_by_entries():
    c = TTSCache(max_entries=2)
    c.put(k(c, "v", "a"), b"1")
    c.put(k(c, "v", "b"), b"2")
    assert c.get(k(c, "v", "a")) == b"1"  # a is now most recent
    c.put(k(c, "v", "c"), b"3")
    assert c.get(k(c, "v", "b")) is None
    assert c.get(k(c, "v", "a")) == b"1"


def test_lru_by_bytes():
    c = TTSCache(max_entries=10, max_bytes=10)
    c.put(k(c, "v", "a"), b"123456")
    c.put(k(c, "v", "b"), b"123456")
    assert c.get(k(c, "v", "a")) is None
    assert c.get(k(c, "v", "b")) is not None
    assert c.size_bytes == 6


def test_voice_separates_and_long_text_skipped():
    c = TTSCache()
    c.put(k(c, "v1", "привет"), b"x")
    assert c.get(k(c, "v2", "Привет!")) is None
    assert c.get(k(c, "v1", "Привет!")) == b"x"
    assert k(c, "v1", "я" * 41) is None


@pytest.mark.asyncio
async def test_interrupted_stream_not_cached_and_hit_skips_network(monkeypatch):
    import httpx
    from ai_voice.gui_audio import AudioSession

    monkeypatch.setattr("ai_voice.pipeline.Playback", FakePlayback)
    calls, got = [], []
    fail = [True]

    class TTS:
        def __init__(self, *_a, **_k): pass
        async def stream(self, text):
            calls.append(text)
            yield b"\0\0" * 100
            if fail[0]:
                raise httpx.ReadError("cut")
            yield b"\0\0" * 100

    session = AudioSession(Config(voice=TEST_VOICE_ID), "k", tts_factory=TTS, on_latency_breakdown=got.append)
    run = asyncio.create_task(session.run())

    async def say(text):
        session.submit(text)
        await asyncio.wait_for(session.engine.queue.join(), 2)

    await say("привет")
    assert len(session.cache) == 0
    fail[0] = False
    await say("привет")
    assert len(session.cache) == 1 and len(calls) == 2
    await say("Привет!")
    assert len(calls) == 2 and got[-1]["cached"] is True
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
