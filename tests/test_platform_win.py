import os
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_app_home_win(monkeypatch):
    from ai_voice import paths
    monkeypatch.delenv("AI_VOICE_HOME", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\X")
    assert paths.app_home() == Path(r"C:\X\AI Voice")


def test_vc_runtime_home_win(monkeypatch):
    from ai_voice import paths
    monkeypatch.delenv("AI_VOICE_VC_RUNTIME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Y")
    assert paths.vc_runtime_home() == Path(r"C:\Y\AI Voice\vc-runtime")


def test_path_overrides_win(monkeypatch):
    from ai_voice import paths
    monkeypatch.setenv("AI_VOICE_HOME", r"C:\Custom\Data")
    monkeypatch.setenv("AI_VOICE_VC_RUNTIME", r"C:\Custom\Runtime")
    assert paths.app_home() == Path(r"C:\Custom\Data")
    assert paths.vc_runtime_home() == Path(r"C:\Custom\Runtime")


def test_audio_lease_second_holder_rejected_win(monkeypatch, tmp_path):
    from ai_voice import instance
    monkeypatch.setattr(instance, "DATA", tmp_path)
    def unexpected(*args, **kwargs):
        pytest.fail("Windows lease must not scan legacy processes")
    monkeypatch.setattr(instance.subprocess, "run", unexpected)
    first, second = instance.AudioLease(), instance.AudioLease()
    try:
        first.acquire()
        with pytest.raises(RuntimeError):
            second.acquire()
        first.close()
        second.acquire()
    finally:
        first.close()
        second.close()


def test_parent_alive_win():
    from ai_voice.desktop import parent_alive
    assert parent_alive(os.getpid()) is True
    assert parent_alive(2**31 - 1) is False


def test_dedupe_libomp_noop_win(tmp_path):
    from ai_voice.vc_omp import dedupe_libomp
    assert dedupe_libomp(tmp_path / "python.exe") == []
    assert list(tmp_path.iterdir()) == []


def test_rss_bytes_win():
    from vc_worker.protocol import rss_bytes
    assert rss_bytes() > 0
