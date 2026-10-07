import base64
import builtins
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from ai_voice import asr, cli


@pytest.mark.parametrize("lang,expected", [
    ("ru", "gigaam"), ("ru-RU", "gigaam"), ("ru_RU", "gigaam"),
    ("RU", "gigaam"), ("en", "whisper"), ("de", "whisper"),
])
@pytest.mark.parametrize("pref", [None, "apple", "gigaam"])
def test_engine_for_language_win(lang, expected, pref):
    assert asr.engine_for_language(lang, pref, platform="win32") == expected


@pytest.mark.parametrize("lang", ["ru-RU", "en", "de"])
@pytest.mark.parametrize("pref,expected", [
    (None, "apple"), ("apple", "apple"), ("gigaam", "gigaam"),
])
def test_engine_for_language_mac_unchanged(lang, pref, expected):
    assert asr.engine_for_language(lang, pref, platform="darwin") == expected


def test_whisper_lazy_import(monkeypatch):
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "onnx_asr":
            pytest.fail("Whisper imported before first use")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.setattr(asr, "load_gigaam", lambda *args: pytest.fail("Model loaded before first use"))
    helper = asr.WhisperASR(language="en")
    assert isinstance(helper, asr.GigaAMASR)
    assert helper.model == "small"


@pytest.mark.parametrize("platform,lang,pref,expected", [
    ("darwin", "en", "apple", asr.SpeechASR),
    ("darwin", "en", "gigaam", asr.GigaAMASR),
    ("win32", "ru-RU", "apple", "GigaAMASR"),
    ("win32", "en", "gigaam", "WhisperASR"),
])
def test_cli_helper_routing(monkeypatch, tmp_path, platform, lang, pref, expected):
    monkeypatch.setattr(sys, "platform", platform)
    binary = tmp_path / "SpeechHelper"
    if platform == "darwin":
        binary.touch()
    monkeypatch.setattr(cli, "HELPER_BINARY", binary)
    config = SimpleNamespace(input_device="default", language=lang, endpoint_ms=700)
    helper = cli._helper(config, pref)
    cls = getattr(asr, expected) if isinstance(expected, str) else expected
    assert type(helper) is cls
    assert (helper.input_device, helper.language, helper.endpoint_ms) == ("default", lang, 700)


@pytest.fixture
def whisper_backend(monkeypatch, tmp_path):
    calls = []
    transcriptions = []

    class Model:
        def recognize(self, audio, *, sample_rate, language=None):
            assert sample_rate == 16000
            transcriptions.append((audio, language))
            return " Hello world "

    def load(name, **kwargs):
        calls.append((name, kwargs))
        return Model()

    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=load))
    monkeypatch.setattr(asr, "_models", {})
    monkeypatch.setenv("AI_VOICE_HOME", str(tmp_path))
    return calls, transcriptions, tmp_path


@pytest.mark.parametrize("language,expected", [("en", "en"), ("en-US", "en"), ("de_DE", "de")])
def test_whisper_transcribes_pcm(whisper_backend, language, expected):
    calls, transcriptions, home = whisper_backend
    helper = asr.WhisperASR(language=language)
    pcm = np.array([-32768, 0, 16384, 32767], dtype="<i2").tobytes()
    assert helper._transcribe(pcm) == "Hello world"
    assert helper._transcribe(pcm) == "Hello world"
    assert calls == [("onnx-community/whisper-small", {"quantization": "int8"})]
    warmup, lang = transcriptions[0]
    np.testing.assert_array_equal(warmup, np.zeros(16000, dtype=np.float32))
    assert lang is None
    audio, lang = transcriptions[1]
    assert audio.dtype == np.float32
    np.testing.assert_array_equal(audio, [-1, 0, 0.5, 32767 / 32768])
    assert lang == expected
    assert len(transcriptions) == 3


async def test_whisper_reuses_events(monkeypatch, whisper_backend):
    pcm = base64.b64encode(np.zeros(3200, dtype="<i2").tobytes()).decode()

    async def capture(self, *, initial_input_gain_db=None):
        assert initial_input_gain_db == 3
        for kind in ("ready", "vad_start", "audio", "vad_end"):
            yield {"event": kind, "utterance_id": "u1", "time_ms": 0, "pcm": pcm}

    monkeypatch.setattr(asr.SpeechASR, "events", capture)
    helper = asr.WhisperASR(language="en")
    output = [event async for event in helper.events(initial_input_gain_db=3)]
    assert [event["event"] for event in output] == ["ready", "vad_start", "vad_end", "final"]
    assert output[-1]["text"] == "Hello world"
    assert output[-1]["utterance_id"] == "u1"
    assert len(whisper_backend[0]) == 1
    assert len(whisper_backend[1]) == 2  # warm-up and utterance


async def test_whisper_load_failure_propagates(monkeypatch, whisper_backend):
    def fail(*args, **kwargs):
        raise RuntimeError("download failed")

    monkeypatch.setattr(sys.modules["onnx_asr"], "load_model", fail)
    helper = asr.WhisperASR(language="en")
    with pytest.raises(asr.ASRError, match="download failed"):
        await anext(helper.events())


async def test_whisper_doctor_loads_model(monkeypatch, whisper_backend):
    async def doctor(self):
        return {"input_found": True}

    monkeypatch.setattr(asr.SpeechASR, "doctor", doctor)
    assert await asr.WhisperASR(language="en").doctor() == {"input_found": True}
    assert len(whisper_backend[0]) == 1


@pytest.mark.parametrize('engine', [asr.SpeechASR, asr.GigaAMASR, asr.WhisperASR])
async def test_win_doctor_and_authorize_without_helper(monkeypatch, whisper_backend, engine):
    monkeypatch.setattr(asr, 'sys', SimpleNamespace(platform='win32'))
    gigaam_loads = []
    monkeypatch.setattr(asr, 'load_gigaam', lambda model: gigaam_loads.append(model))

    async def forbidden_report(*args):
        pytest.fail('Windows doctor/authorize must not invoke SpeechHelper')

    monkeypatch.setattr(asr.SpeechASR, '_report', forbidden_report)
    helper = engine(input_device='Test microphone', language='en')
    report = await helper.doctor()
    assert report['event'] == 'doctor'
    assert report['input'] == 'Test microphone'
    assert report['language'] == 'en'
    assert report['microphone_authorization'] == 'authorized'
    assert report['capture_started'] is False
    cli._permissions(report)
    for kind in ('microphone', 'speech'):
        assert await helper.authorize_only(kind) == report
    assert gigaam_loads == ([asr.GIGAAM_MODEL] if engine is asr.GigaAMASR else
                            ["onnx-community/whisper-small"] if engine is asr.WhisperASR else [])
    assert whisper_backend[0] == []


def test_whisper_models_cached_across_instances(whisper_backend):
    calls, transcriptions, home = whisper_backend
    small = asr.WhisperASR()._load_model()
    assert asr.WhisperASR()._load_model() is small
    assert asr.WhisperASR(model="base")._load_model() is not small
    assert calls == [("onnx-community/whisper-small", {"quantization": "int8"}),
                     ("onnx-community/whisper-base", {"quantization": "int8"})]
    assert len(transcriptions) == 2  # one warm-up per model
