"""Shared offline test defaults."""
import subprocess
from types import SimpleNamespace

import pytest

from ai_voice import asr, i18n, secrets


@pytest.fixture(autouse=True)
def _i18n_en():
    i18n.set_language("en")


@pytest.fixture(autouse=True)
def _offline_speech_locales(monkeypatch):
    monkeypatch.setattr(asr, "_LOCALES", dict(asr.LOCALES_FALLBACK))


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    def run(*_args, **_kwargs):
        raise OSError("keychain disabled in tests")

    monkeypatch.setattr(secrets, "subprocess",
                        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
