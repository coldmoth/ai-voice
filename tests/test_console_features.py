"""Offline tests for preview, catalog, and desktop-control console flows.

Covers PreviewPlayer accept/reject paths, physical-device filtering, latest-wins
cancellation, async error status propagation, native-rate fallback when resample
is available, catalog stored-voice deletion, favorite/selected fallbacks,
persistence-failure rollback (including VOICES dict), DesktopControl voice
removal rollback without weakening protections, gain-preference validation
including bool/nan/boundaries, latency callback field plumbing, and DesktopControl
preview callback integration. Uses fake sounddevice + httpx so no real audio,
Keychain, or paid TTS traffic runs.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import math
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from helpers import TEST_VOICE_ID, write_library


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mic", "text"])
async def test_start_without_voice_fails_before_lease(tmp_path, monkeypatch, mode):
    control_module = mod("desktop_control")
    leases, sessions = [], []
    monkeypatch.setattr(control_module, "AudioLease", lambda: leases.append(True))
    monkeypatch.setattr(control_module, "AudioSession", lambda *a, **kw: sessions.append(True))
    control = control_module.DesktopControl(mod("catalog").Catalog(tmp_path / "desktop.json"))
    with pytest.raises(RuntimeError, match="^Choose a voice first$"):
        await control.start(mode, None, "MIC", "AI Voice")
    assert leases == []
    assert sessions == []
    assert control.session is None


def test_cli_voice_selection_and_library_output(tmp_path, monkeypatch, capsys):
    cli = mod("cli")
    monkeypatch.setattr(cli, "migrate_legacy", lambda: False)
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.toml")
    cat = mod("catalog").Catalog(tmp_path / "desktop.json")
    monkeypatch.setattr(cli, "Catalog", lambda: cat)
    assert cli.main(["voices"]) == 0
    assert capsys.readouterr().out.strip() == "No voices yet. Add one in the app."
    assert cli.main(["start"]) == 2
    assert "Pass --voice <fish voice id>" in capsys.readouterr().err
    assert cli._config(TEST_VOICE_ID).reference_id == TEST_VOICE_ID
    write_library(cat.path, [{"id": TEST_VOICE_ID, "name": "Быков"}], TEST_VOICE_ID)
    cat = mod("catalog").Catalog(cat.path)
    assert cli._config(None).reference_id == TEST_VOICE_ID
    assert cli.main(["voices"]) == 0
    assert capsys.readouterr().out.strip() == TEST_VOICE_ID + " — Быков"


@pytest.mark.asyncio
async def test_speak_without_voice_reports_backend_error(tmp_path, monkeypatch):
    control_module = mod("desktop_control")
    leases = []
    monkeypatch.setattr(control_module, "AudioLease", lambda: leases.append(True))
    control = control_module.DesktopControl(mod("catalog").Catalog(tmp_path / "desktop.json"))
    with pytest.raises(RuntimeError, match="^Choose a voice first$"):
        await control.command({"action": "speak", "text": "Hello"})
    assert leases == []
    assert control.session is None
    assert control.snapshot()["message"] == "Choose a voice first"


def mod(name):
    return importlib.import_module(f"ai_voice.{name}")


def test_default_input_name_uses_system_input(monkeypatch):
    devices = mod("devices")
    query = MagicMock(return_value={"name": "MacBook Pro Microphone"})
    monkeypatch.setattr(devices.sd, "query_devices", query)
    assert devices.default_input_name() == "MacBook Pro Microphone"
    query.assert_called_once_with(kind="input")


def test_default_input_name_returns_none_on_error(monkeypatch):
    devices = mod("devices")
    monkeypatch.setattr(devices.sd, "query_devices", MagicMock(side_effect=RuntimeError("No input")))
    assert devices.default_input_name() is None


@pytest.mark.parametrize("names, expected", [
    (["BlackHole 2ch", "AI Voice Mic"], "AI Voice Mic"),
    (["AI Voice", "AI Voice Mic", "BlackHole 2ch"], "AI Voice Mic"),
    (["BlackHole 2ch", "AI Voice"], "AI Voice"),
    (["BlackHole 2ch"], "BlackHole 2ch"),
    ([], None),
    (["Speakers", "ai voice", "AI Voice Mic Extra", "BlackHole 16ch"], None),
])
def test_preferred_virtual_output_exact_priority(names, expected):
    assert mod("devices").preferred_virtual_output(names) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, vc, voice_id, message", [
    ("invalid", None, None, 'Select microphone or text mode.'),
    ("vc", None, "v1", 'Live voice mode is unavailable.'),
    ("vc", SimpleNamespace(loading=False), None, 'Select a voice for Live voice mode.'),
])
async def test_start_validates_mode_before_device_resolution(tmp_path, monkeypatch, mode, vc, voice_id, message):
    control_module = mod("desktop_control")
    default_input, preferred_output, devices, lease = (MagicMock() for _ in range(4))
    monkeypatch.setattr(control_module, "default_input_name", default_input)
    monkeypatch.setattr(control_module, "preferred_virtual_output", preferred_output)
    monkeypatch.setattr(control_module, "list_devices", devices)
    monkeypatch.setattr(control_module, "AudioLease", lease)
    control = control_module.DesktopControl(mod("catalog").Catalog(tmp_path / "desktop.json"), vc=vc)
    with pytest.raises(ValueError) as error:
        await control.start(mode, None, None, None, vc_voice_id=voice_id)
    assert str(error.value) == message
    for call in (default_input, preferred_output, devices, lease):
        call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mic", "text"])
async def test_start_without_output_fails_before_lease(tmp_path, monkeypatch, mode):
    control_module = mod("desktop_control")
    cat = mod("catalog").Catalog(write_library(tmp_path / "desktop.json",
        [{"id": TEST_VOICE_ID, "name": "Test"}], TEST_VOICE_ID))
    default_input = MagicMock(return_value="MacBook Pro Microphone")
    preferred_output = MagicMock(return_value=None)
    monkeypatch.setattr(control_module, "default_input_name", default_input, raising=False)
    monkeypatch.setattr(control_module, "preferred_virtual_output", preferred_output, raising=False)
    monkeypatch.setattr(control_module, "list_devices", lambda: [
        {"name": "Speakers", "max_output_channels": 2},
        {"name": "Mono output", "max_output_channels": 1},
    ])
    lease, session, key = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(control_module, "AudioLease", lease)
    monkeypatch.setattr(control_module, "AudioSession", session)
    monkeypatch.setattr(control_module, "load_key", key)
    control = control_module.DesktopControl(cat)
    with pytest.raises(RuntimeError, match="^Choose an output device in Settings → Audio$"):
        await control.start(mode, TEST_VOICE_ID, None, None)
    default_input.assert_called_once_with()
    preferred_output.assert_called_once_with(["Speakers"])
    lease.assert_not_called()
    session.assert_not_called()
    key.assert_not_called()
    assert control.session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mic", "text"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_start_resolves_devices_without_saving_defaults(tmp_path, monkeypatch, mode, explicit):
    control_module = mod("desktop_control")
    input_name = "MIC" if explicit else "MacBook Pro Microphone"
    output_name = "AI Voice" if explicit else "BlackHole 2ch"
    device_prefs = {"input_device": input_name, "output_device": output_name} if explicit else {}
    cat = mod("catalog").Catalog(write_library(tmp_path / "desktop.json",
        [{"id": TEST_VOICE_ID, "name": "Test"}], TEST_VOICE_ID, **device_prefs))
    device_list = [
        {"name": input_name, "max_input_channels": 1, "max_output_channels": 0},
        {"name": "AI Voice Mic", "max_input_channels": 0, "max_output_channels": 1},
        {"name": output_name, "max_input_channels": 0, "max_output_channels": 2},
    ]
    default_input = MagicMock(return_value=input_name)
    preferred_output = MagicMock(wraps=mod("devices").preferred_virtual_output)
    monkeypatch.setattr(control_module, "default_input_name", default_input, raising=False)
    monkeypatch.setattr(control_module, "preferred_virtual_output", preferred_output, raising=False)
    monkeypatch.setattr(control_module, "list_devices", lambda: device_list)
    monkeypatch.setattr(mod("cli"), "list_devices", lambda: device_list)
    resolve = MagicMock(return_value=2)
    monkeypatch.setattr(control_module, "resolve_output", resolve)
    monkeypatch.setattr(mod("cli"), "resolve_output", resolve)
    monkeypatch.setattr(control_module, "_config", lambda slug: mod("config").Config(voice=slug))
    monkeypatch.setattr(control_module, "load_key", lambda: "test-key")
    monkeypatch.setattr(control_module, "_helper", lambda *a: SimpleNamespace(doctor=AsyncMock(return_value={})))
    monkeypatch.setattr(control_module, "_permissions", lambda *a: None)
    lease = MagicMock()
    monkeypatch.setattr(control_module, "AudioLease", lambda: lease)

    class Session:
        def __init__(self, config, key, **kwargs):
            self.engine = SimpleNamespace(config=config, buffer=SimpleNamespace(clear=lambda: None))
            self.ready = asyncio.Event()
            self.submit = MagicMock()

        def input_gain_status(self):
            return {"supported": True, "error": None}

        async def run(self):
            self.ready.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(control_module, "AudioSession", Session)
    control = control_module.DesktopControl(cat)
    try:
        await control.start(mode, TEST_VOICE_ID,
                            input_name if explicit else None, output_name if explicit else None)
        assert control.session.engine.config.input_device == input_name
        assert control.session.engine.config.output_device == output_name
        resolve.assert_called_once_with(output_name)
        if explicit:
            default_input.assert_not_called()
            preferred_output.assert_not_called()
        else:
            default_input.assert_called_once_with()
            preferred_output.assert_called_once_with([output_name])
        for field, expected in (("input_device", input_name), ("output_device", output_name)):
            assert cat.preferences()[field] == (expected if explicit else None)
            assert json.loads(cat.path.read_text())[field] == (expected if explicit else None)
        session = control.session
        await control.command({"action": "start", "mode": mode, "voice_id": TEST_VOICE_ID})
        assert control.session is session
        lease.acquire.assert_called_once_with()
        resolve.assert_called_once_with(output_name)
    finally:
        await control.stop()


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("supported,expected", [(["en-US", "de-DE"], "de-DE"), (["en-US"], "en-US")])
async def test_start_passes_speech_language(tmp_path, monkeypatch, supported, expected):
    control_module = mod("desktop_control")
    cat = mod("catalog").Catalog(write_library(tmp_path / "desktop.json",
        [{"id": TEST_VOICE_ID, "name": "Test"}], TEST_VOICE_ID,
        speech_language="de-DE"))
    monkeypatch.setattr(mod("asr"), "_LOCALES",
        {"system": "en-US", "supported": supported}, raising=False)
    monkeypatch.setattr(control_module, "_config", lambda slug: mod("config").Config(voice=slug))
    monkeypatch.setattr(control_module, "load_key", lambda: "test-key")
    monkeypatch.setattr(control_module, "_devices", lambda *a: None)
    monkeypatch.setattr(control_module, "_permissions", lambda *a: None)
    monkeypatch.setattr(control_module, "AudioLease", MagicMock)
    helpers = []

    def helper(config, engine):
        instance = mod("asr").SpeechASR(language=config.language)
        instance.doctor = AsyncMock(return_value={})
        helpers.append(instance)
        return instance

    class Session:
        def __init__(self, config, key, **kwargs):
            self.engine = SimpleNamespace(config=config, buffer=SimpleNamespace(clear=lambda: None))
            self.ready = asyncio.Event()

        def input_gain_status(self):
            return {"supported": True, "error": None}

        async def run(self):
            self.ready.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(control_module, "_helper", helper)
    monkeypatch.setattr(control_module, "AudioSession", Session)
    control = control_module.DesktopControl(cat)
    try:
        await control.start("mic", TEST_VOICE_ID, "MIC", "AI Voice")
        assert helpers[0].language == expected
        assert control.session.engine.config.language == expected
        assert cat.preferences()["speech_language"] == "de-DE"
    finally:
        await control.stop()


# Fake sounddevice + httpx so no real audio / network is touched.
# ---------------------------------------------------------------------------
class FakeOutputStream:
    instances = []
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.stopped = False
        self.callback = kwargs.get("callback")
        FakeOutputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakePortAudioError(Exception):
    pass


def install_fake_sounddevice(monkeypatch, *, devices):
    """Install a sounddevice stub that returns a fixed device list."""
    fake_sd = types.SimpleNamespace()
    fake_sd.PortAudioError = FakePortAudioError
    fake_sd.query_devices = lambda *a, **kw: (devices(*a, **kw) if callable(devices) else devices)
    fake_sd.OutputStream = FakeOutputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    FakeOutputStream.instances.clear()
    return fake_sd


@pytest.fixture(autouse=True)
def _isolate_voices():
    """Snapshot/restore the global VOICES / VOICE_NAMES dicts around every test."""
    cfg = mod("config")
    voices_snap = dict(cfg.VOICES)
    names_snap = dict(cfg.VOICE_NAMES)
    yield
    cfg.VOICES.clear()
    cfg.VOICES.update(voices_snap)
    cfg.VOICE_NAMES.clear()
    cfg.VOICE_NAMES.update(names_snap)


@pytest.fixture
def preview_module(monkeypatch, tmp_path):
    """Reload preview module with a stubbed sounddevice + safe load_key."""
    install_fake_sounddevice(monkeypatch, devices=[
        {"name": "Headphones", "max_output_channels": 2, "default_samplerate": 48000},
        {"name": "AI Voice", "max_output_channels": 2, "default_samplerate": 48000},
        {"name": "MIC", "max_output_channels": 2, "default_samplerate": 44100},
        {"name": "BlackHole 2ch", "max_output_channels": 2, "default_samplerate": 44100},
        {"name": "Headphones", "max_output_channels": 2, "default_samplerate": 48000},
    ])
    monkeypatch.setattr(mod("secrets"), "load_key", lambda: "test-key")
    if "ai_voice.preview" in sys.modules:
        del sys.modules["ai_voice.preview"]
    return importlib.import_module("ai_voice.preview")


@pytest.fixture
def catalog(tmp_path):
    return mod("catalog").Catalog(path=write_library(tmp_path / "prefs.json",
        [{"id": TEST_VOICE_ID, "name": "Быков"}, {"id": "a" * 32, "name": "Other"}], TEST_VOICE_ID))


@pytest.fixture
def fake_tts_factory():
    """Return (factory, captured-list). factory(config, key, client) builds a stub."""
    captured = []

    class StubTTS:
        def __init__(self, config, key, *, client):
            self.config = config
            self.key = key
            self.client = client
            captured.append(config)

        async def stream(self, text):
            chunk = np.zeros(1024, dtype="<i2").tobytes()
            yield chunk
            chunk2 = np.zeros(512, dtype="<i2").tobytes()
            yield chunk2
            return

    def factory(config, key, client):
        return StubTTS(config, key, client=client)

    return factory, captured


# ---------------------------------------------------------------------------
# Preview: voice id + device selection
# ---------------------------------------------------------------------------
def test_preview_accepts_canonical_hex_id(preview_module, fake_tts_factory, monkeypatch):
    factory, captured = fake_tts_factory
    player = preview_module.PreviewPlayer(tts_factory=factory)
    valid_hex = "a" * 32  # Fish reference id absent from the configured voice catalog
    asyncio.run(player.start(valid_hex, device_name="Headphones"))
    assert captured, "TTS factory should be invoked"
    assert captured[0].reference_id == valid_hex
    assert player.snapshot()["clickedid"] == valid_hex


def test_preview_rejects_unknown_identifier(preview_module, fake_tts_factory):
    player = preview_module.PreviewPlayer(tts_factory=fake_tts_factory[0])
    with pytest.raises(ValueError):
        asyncio.run(player.start("not-a-real-voice", device_name="Headphones"))
    assert player.snapshot()["preview_error"] == "voice_not_found"


def test_preview_filters_virtual_devices_case_insensitive(preview_module, fake_tts_factory):
    physical = preview_module._enumerate_physical_outputs()
    names = [name for _, name in physical]
    for forbidden in ("AI Voice", "MIC", "BlackHole 2ch"):
        assert forbidden not in names, f"{forbidden} must be excluded"
    assert "Headphones" in names
    # duplicates are collapsed
    assert names.count("Headphones") == 1


def test_preview_explicit_unsafe_device_rejected_safely(preview_module, fake_tts_factory):
    player = preview_module.PreviewPlayer(tts_factory=fake_tts_factory[0])
    with pytest.raises(ValueError):
        asyncio.run(player.start(TEST_VOICE_ID, device_name="ai voice"))
    # virtual device must never become the active sink
    assert player._sink is None


# ---------------------------------------------------------------------------
# Preview: lifecycle, latest-wins, errors, native rate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_preview_latest_wins_and_cleanup(preview_module, fake_tts_factory):
    factory, captured = fake_tts_factory
    player = preview_module.PreviewPlayer(tts_factory=factory)
    token1 = await player.start(TEST_VOICE_ID, device_name="Headphones")
    token2 = await player.start(TEST_VOICE_ID, device_name="Headphones")
    assert token2 != token1
    assert player._generation == token2
    # Give the first (cancelled) task a chance to observe its cancellation.
    for _ in range(20):
        if not any(not t.done() for t in [player._task] if t):
            break
        await asyncio.sleep(0.01)
    await player.stop()
    assert player._sink is None
    assert player._task is None


@pytest.mark.asyncio
async def test_preview_propagates_async_failure_status(preview_module):
    class FailingTTS:
        def __init__(self, config, key, *, client):
            self.config = config
        async def stream(self, text):
            raise RuntimeError("synth down")
            yield b""  # pragma: no cover

    player = preview_module.PreviewPlayer(tts_factory=FailingTTS)
    with pytest.raises(ValueError):
        await player.start(TEST_VOICE_ID, device_name="Headphones")
    # Wait briefly for the background task to record the failure.
    for _ in range(30):
        if player.snapshot()["preview_state"] == "error":
            break
        await asyncio.sleep(0.01)
    snap = player.snapshot()
    assert snap["preview_state"] == "error"
    assert snap["preview_error"] == "preview_failed"


@pytest.mark.asyncio
async def test_preview_native_rate_fallback_when_source_lacks_resample(preview_module):
    """Source.sample_rate() is not implemented → preview falls back to native rate."""
    class NoResample:
        """Minimal fake that intentionally lacks a sample_rate() method."""
        def __init__(self, config, key, *, client):
            self.config = config
        async def stream(self, text):
            yield np.zeros(2048, dtype="<i2").tobytes()

    player = preview_module.PreviewPlayer(tts_factory=NoResample)
    await player.start(TEST_VOICE_ID, device_name="Headphones")
    try:
        sink = player._sink
        # Headphones reports native 48000; without source.sample_rate() the
        # fallback should pick that native rate instead of forcing 44100.
        assert sink.sample_rate == 48000
    finally:
        await player.stop()


# ---------------------------------------------------------------------------
# Catalog: remove stored voice, favorite fallback, rollback
# ---------------------------------------------------------------------------
def test_catalog_remove_seed_id(catalog):
    bykov = "db89e349112e44dca6820e0cb2d414cc"
    catalog.remove(bykov)
    assert bykov not in {v['id'] for v in catalog.voices()}
    assert 'hidden_builtin_ids' not in catalog.data
    with pytest.raises(ValueError):
        catalog.slug(bykov)


def test_catalog_favorite_missing_falls_back_to_selected(catalog):
    # Only the default selected voice should remain selectable.
    prefs = catalog.preferences()
    assert prefs["voice_id"] == TEST_VOICE_ID
    catalog.update({"favorite_ids": ["z" * 32]})  # invalid id gets filtered
    assert catalog.preferences()["favorite_ids"] == []


def test_catalog_remove_unknown_voice_rolls_back_voices_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(mod("secrets"), "load_key", lambda: "test-key")
    cat = mod("catalog").Catalog(path=write_library(tmp_path / "state" / "desktop.json",
        [{"id": TEST_VOICE_ID, "name": "Быков"}], TEST_VOICE_ID))
    target = "f" * 32
    cat.data["voices"].append({"id": target, "name": "User Voice"})
    cat.save()
    mod("config").VOICES["fish_" + target] = target
    mod("config").VOICE_NAMES["fish_" + target] = "User Voice"
    snapshot_voices = dict(mod("config").VOICES)
    snapshot_names = dict(mod("config").VOICE_NAMES)

    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(cat, "save", boom)
    with pytest.raises(OSError):
        cat.remove(target)
    # Global VOICES must be restored to exactly what it was before the failed save.
    assert mod("config").VOICES == snapshot_voices
    assert mod("config").VOICE_NAMES == snapshot_names
    # And the on-disk data must not have lost the user voice.
    reloaded = mod("catalog").Catalog(path=cat.path)
    assert target in {v["id"] for v in reloaded.data["voices"]}


# ---------------------------------------------------------------------------
# DesktopControl: remove voice rollback + gain validation + latency callbacks
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self):
        self.changed_to = []
        self.gain_set = []
        self.closed = False
    async def change_voice(self, slug):
        self.changed_to.append(slug)
    async def set_input_gain(self, db):
        self.gain_set.append(db)
        if math.isnan(db):
            raise ValueError("nan")
    async def update_settings(self, **kw):
        return None


def _build_control(tmp_path, *, active_voice):
    cat = mod("catalog").Catalog(path=tmp_path / "prefs.json")
    cat.update({"voice_id": active_voice})
    control = mod("desktop_control").DesktopControl(cat)
    control.session = _FakeSession()
    control.state["voice_id"] = active_voice
    return control, cat


@pytest.mark.asyncio
async def test_remove_voice_rollback_on_save_failure(tmp_path, monkeypatch):
    voice_id = "d" * 32
    bykov = TEST_VOICE_ID
    cat = mod("catalog").Catalog(path=write_library(tmp_path / "prefs.json",
        [{"id": TEST_VOICE_ID, "name": "Быков"}], TEST_VOICE_ID))
    cat.data["voices"].append({"id": voice_id, "name": "Демо"})
    mod("config").VOICES["fish_" + voice_id] = voice_id
    mod("config").VOICE_NAMES["fish_" + voice_id] = "Демо"
    cat.save()
    cat.update({"voice_id": voice_id})
    control = mod("desktop_control").DesktopControl(cat)
    fake = _FakeSession()
    control.session = fake
    control.state["voice_id"] = voice_id

    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(cat, "save", boom)
    with pytest.raises(OSError):
        await control.remove_voice(voice_id)
    # Protection: active voice must not silently flip to bykov on disk failure.
    assert cat.preferences()["voice_id"] == voice_id
    assert cat.data.get("voices", [])
    assert control.state["voice_id"] == voice_id
    # Removing a seed voice fails the same way and is rolled back.
    with pytest.raises(OSError):
        cat.remove(bykov)
    assert bykov in {v["id"] for v in cat.data["voices"]}


def test_gain_validation_bool_nan_and_boundaries(catalog):
    # bool rejected as number
    with pytest.raises(ValueError):
        catalog.update({"output_gain_db": True})
    with pytest.raises(ValueError):
        catalog.update({"input_gain_db": False})
    # NaN rejected
    with pytest.raises(ValueError):
        catalog.update({"output_gain_db": float("nan")})
    with pytest.raises(ValueError):
        catalog.update({"input_gain_db": float("nan")})
    # boundaries
    catalog.update({"output_gain_db": 12.0})
    catalog.update({"output_gain_db": -24.0})
    catalog.update({"input_gain_db": 12.0})
    catalog.update({"input_gain_db": -24.0})
    assert catalog.preferences()["output_gain_db"] == -24.0
    assert catalog.preferences()["input_gain_db"] == -24.0
    with pytest.raises(ValueError):
        catalog.update({"output_gain_db": 12.5})
    with pytest.raises(ValueError):
        catalog.update({"input_gain_db": -25})


@pytest.mark.asyncio
async def test_apply_preferences_initial_gain_only_when_session_present(tmp_path, monkeypatch):
    cat = mod("catalog").Catalog(path=tmp_path / "prefs.json")
    control = mod("desktop_control").DesktopControl(cat)
    assert control.session is None
    # No session → no propagation required, no error either.
    await control.apply_preferences({"input_gain_db": 3.0})
    assert cat.preferences()["input_gain_db"] == 3.0


@pytest.mark.asyncio
async def test_apply_preferences_unrelated_change_ignores_missing_monitor_device(tmp_path, monkeypatch):
    cat = mod("catalog").Catalog(path=tmp_path / "prefs.json")
    control = mod("desktop_control").DesktopControl(cat)
    monkeypatch.setattr(control, "_monitor_validation_error", lambda prefs: "missing")
    monkeypatch.setattr(cat, "preferences", lambda: {"monitor_enabled": True, "monitor_device": "gone"})
    monkeypatch.setattr(cat, "update", lambda values: dict(values))
    assert await control.apply_preferences({"onboarding_completed": True}) == {"onboarding_completed": True}
    with pytest.raises(ValueError, match="missing"):
        await control.apply_preferences({"monitor_gain_db": 1})


def test_latency_callbacks_start_null_and_increment(tmp_path):
    cat = mod("catalog").Catalog(path=tmp_path / "prefs.json")
    control = mod("desktop_control").DesktopControl(cat)
    snap0 = control.snapshot()
    assert snap0["latency_seconds"] is None
    assert snap0["generation_seq"] == 0
    control.mark_generation_start()
    control.record_latency(0.123)
    snap1 = control.snapshot()
    assert snap1["generation_seq"] == 1
    assert snap1["latency_seconds"] == pytest.approx(0.123)
    control.record_latency(-1)
    assert control.snapshot()["latency_seconds"] == pytest.approx(0.123)
    control.record_latency(float("inf"))
    assert control.snapshot()["latency_seconds"] == pytest.approx(0.123)


def test_preview_callback_branches_visible_in_snapshot(tmp_path):
    cat = mod("catalog").Catalog(path=tmp_path / "prefs.json")
    control = mod("desktop_control").DesktopControl(cat)
    token = control.begin_preview()
    assert token == control.snapshot()["preview_id"]
    assert control.snapshot()["preview_state"] == "playing"
    control.end_preview(token)
    assert control.snapshot()["preview_state"] == "idle"
    control.end_preview(token, error="boom")
    assert control.snapshot()["preview_state"] == "error"
    assert control.snapshot()["preview_error"] == "boom"


@pytest.mark.asyncio
async def test_preview_state_becomes_active_after_first_chunk(preview_module):
    gate = asyncio.Event()

    class SlowTTS:
        def __init__(self, config, key, *, client):
            pass

        async def stream(self, text):
            yield np.zeros(1024, dtype="<i2").tobytes()
            await gate.wait()

    player = preview_module.PreviewPlayer(tts_factory=SlowTTS)
    await player.start(TEST_VOICE_ID, device_name="Headphones")
    for _ in range(50):
        if player.snapshot()["preview_state"] == "active":
            break
        await asyncio.sleep(0.01)
    assert player.snapshot()["preview_state"] == "active"
    gate.set()
    await player.stop()


def test_preview_sink_applies_gain(preview_module):
    sink = preview_module._PreviewSink(0, preview_module.SAMPLE_RATE, gain=0.5)
    sink._stream = object()
    sink.feed(np.full(100, 0.8, dtype=np.float32))
    assert sink._count == 100
    assert float(sink._ring[0]) == pytest.approx(0.4)
    loud = preview_module._PreviewSink(0, preview_module.SAMPLE_RATE, gain=4.0)
    loud._stream = object()
    loud.feed(np.full(10, 0.8, dtype=np.float32))
    assert float(loud._ring[0]) == pytest.approx(1.0)
