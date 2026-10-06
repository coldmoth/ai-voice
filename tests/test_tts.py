import importlib
import json

import httpx
import pytest
from helpers import TEST_VOICE_ID


def core():
    try:
        return importlib.import_module("ai_voice.fish_tts"), importlib.import_module("ai_voice.config")
    except ModuleNotFoundError:
        pytest.fail("Missing streaming TTS implementation")


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


async def collect(tts):
    return b"".join([part async for part in tts.stream("Привет")])


async def test_request_is_explicit_pcm_and_first_chunk_is_streamed():
    fish, settings = core()
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=Chunks([b"\x01\x00", b"\x02\x00"]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tts = fish.FishTTS(settings.Config(voice=TEST_VOICE_ID), "test-key", client=client)
        stream = tts.stream("Привет")
        assert await anext(stream) == b"\x01\x00"
        assert await anext(stream) == b"\x02\x00"
        await stream.aclose()
    assert str(seen[0].url) == "https://api.fish.audio/v1/tts"
    body = json.loads(seen[0].content)
    assert body["text"] == "Привет"
    assert body["reference_id"] == TEST_VOICE_ID
    assert body["format"] == "pcm" and body["sample_rate"] == 44100
    assert body["latency"] == "low"
    assert seen[0].headers["model"] == "s2.1-pro-free"


@pytest.mark.parametrize("status", [401, 402, 403, 404, 422])
async def test_terminal_status_does_not_retry_or_expose_response(status):
    fish, settings = core()
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(status, text="test-key secret upstream content")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(fish.TTSError) as failure:
            await collect(fish.FishTTS(settings.Config(voice=TEST_VOICE_ID, retry_base_seconds=0), "test-key", client=client))
    assert len(attempts) == 1
    assert str(status) in str(failure.value)
    assert "test-key" not in str(failure.value)


@pytest.mark.parametrize("failure", [429, 500, 503, "timeout"])
async def test_transient_failure_before_audio_retries(failure):
    fish, settings = core()
    attempts = []
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("network", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, content=b"\x01\x00")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await collect(fish.FishTTS(settings.Config(voice=TEST_VOICE_ID, retry_base_seconds=0), "test-key", client=client)) == b"\x01\x00"
    assert len(attempts) == 2


async def test_network_failure_after_audio_never_replays_emitted_audio():
    fish, settings = core()
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=Chunks([b"\x01\x00", httpx.ReadTimeout("oops")]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = fish.FishTTS(settings.Config(voice=TEST_VOICE_ID, retry_base_seconds=0), "test-key", client=client).stream("Привет")
        assert await anext(stream) == b"\x01\x00"
        with pytest.raises(fish.TTSError):
            await anext(stream)
    assert len(attempts) == 1


@pytest.mark.parametrize("mime,data", [("application/json", b'{"error":1}'), ("audio/pcm", b""), ("audio/pcm", b"\x01"), ("audio/mpeg", b"ID3bad")])
async def test_invalid_or_empty_pcm_is_readable_error(mime, data):
    fish, settings = core()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": mime}, content=data))) as client:
        with pytest.raises(fish.TTSError):
            await collect(fish.FishTTS(settings.Config(voice=TEST_VOICE_ID), "test-key", client=client))


async def test_retries_are_bounded():
    fish, settings = core()
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(fish.TTSError):
            await collect(fish.FishTTS(settings.Config(voice=TEST_VOICE_ID, max_retries=2, retry_base_seconds=0), "test-key", client=client))
    assert len(attempts) == 3


@pytest.mark.parametrize("flag", [True, False])
async def test_normalize_loudness_is_sent_in_prosody(flag):
    fish, settings = core()
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=Chunks([b"\x01\x00"]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tts = fish.FishTTS(settings.Config(voice=TEST_VOICE_ID, normalize_loudness=flag), "test-key", client=client)
        await collect(tts)
    assert seen[0]["prosody"]["normalize_loudness"] is flag


async def test_backoff_delays_and_success_after_two_503(monkeypatch):
    fish, settings = core()
    delays, attempts = [], []

    async def fake_sleep(seconds):
        delays.append(seconds)
    monkeypatch.setattr(fish.asyncio, "sleep", fake_sleep)

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, content=b"\x01\x00")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await collect(fish.FishTTS(settings.Config(voice=TEST_VOICE_ID), "test-key", client=client)) == b"\x01\x00"
    assert delays == [0.4, 1.0] and len(attempts) == 3


async def test_connect_error_retried_then_reports_no_connection(monkeypatch):
    fish, settings = core()
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("down", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(fish.TTSError) as failure:
            await collect(fish.FishTTS(settings.Config(voice=TEST_VOICE_ID, retry_base_seconds=0), "test-key", client=client))
    assert len(attempts) == 3 and "No connection to Fish Audio" in str(failure.value)
