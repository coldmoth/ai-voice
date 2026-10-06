import base64

import numpy as np
import pytest

from ai_voice import asr as asr_mod
from ai_voice.asr import ASRError, GigaAMASR, SpeechASR
from ai_voice.catalog import Catalog


def _pcm(seconds):
    return base64.b64encode(np.zeros(int(16000 * seconds), dtype="<i2").tobytes()).decode()


def _ev(kind, uid="u1", **extra):
    return {"event": kind, "utterance_id": uid, "time_ms": 1.0, **extra}


def _make(monkeypatch, events, text="привет"):
    async def fake_events(self, *, initial_input_gain_db=None):
        for e in events:
            yield e
    monkeypatch.setattr(SpeechASR, "events", fake_events)
    monkeypatch.setattr(asr_mod, "load_gigaam", lambda name=None: None)
    a = GigaAMASR(command=["true"], launch_app=False)
    a._transcribe = lambda pcm: text
    return a


async def _collect(a):
    return [e async for e in a.events()]


async def test_audio_chunks_become_final(monkeypatch):
    a = _make(monkeypatch, [_ev("vad_start"), _ev("audio", pcm=_pcm(0.5)), _ev("audio", pcm=_pcm(0.5)), _ev("vad_end")])
    out = await _collect(a)
    assert [e["event"] for e in out] == ["vad_start", "vad_end", "final"]
    assert out[-1]["text"] == "привет" and out[-1]["utterance_id"] == "u1"


async def test_short_or_empty_utterance_yields_no_final(monkeypatch):
    a = _make(monkeypatch, [_ev("vad_start"), _ev("audio", pcm=_pcm(0.05)), _ev("vad_end")])
    assert [e["event"] for e in await _collect(a)] == ["vad_start", "vad_end"]
    a = _make(monkeypatch, [_ev("vad_start"), _ev("audio", pcm=_pcm(1)), _ev("vad_end")], text="")
    assert [e["event"] for e in await _collect(a)] == ["vad_start", "vad_end"]


async def test_model_error_is_nonfatal(monkeypatch):
    a = _make(monkeypatch, [_ev("audio", pcm=_pcm(1)), _ev("vad_end")])
    def boom(pcm):
        raise RuntimeError("x")
    a._transcribe = boom
    out = await _collect(a)
    assert out[-1]["event"] == "error" and out[-1]["fatal"] is False


async def test_bad_base64_is_fatal(monkeypatch):
    a = _make(monkeypatch, [_ev("audio", pcm="!!!")])
    with pytest.raises(ASRError):
        await _collect(a)


def test_parse_accepts_audio_requires_pcm():
    ok = b'{"event":"audio","utterance_id":"u","time_ms":1,"pcm":"AAAA"}'
    assert SpeechASR._parse(ok)["event"] == "audio"
    with pytest.raises(ASRError):
        SpeechASR._parse(b'{"event":"audio","utterance_id":"u","time_ms":1}')


def test_engine_args():
    assert GigaAMASR._engine_args == ("--engine", "raw")
    assert SpeechASR._engine_args == ()


def test_asr_engine_preference(tmp_path):
    cat = Catalog(tmp_path / "d.json")
    assert cat.preferences()["asr_engine"] == "apple"
    assert cat.update({"asr_engine": "gigaam"})["asr_engine"] == "gigaam"
    assert Catalog(tmp_path / "d.json").preferences()["asr_engine"] == "gigaam"
    with pytest.raises(ValueError):
        cat.update({"asr_engine": "whisper"})
    assert cat.preferences()["asr_engine"] == "gigaam"
