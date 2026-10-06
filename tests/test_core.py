import importlib
import subprocess

import pytest


def module(name):
    try:
        return importlib.import_module(f"ai_voice.{name}")
    except ModuleNotFoundError:
        pytest.fail(f"Missing speech core implementation: {name}")


@pytest.mark.parametrize("settings", [
    {"voice": "missing"}, {"input_device": ""}, {"output_device": ""},
    {"tts_sample_rate": 48000}, {"output_sample_rate": 0},
    {"buffer_ms": 0}, {"endpoint_ms": -1}, {"max_retries": -1},
    {"retry_base_seconds": -1}, {"request_timeout_seconds": 0},
])
def test_invalid_configuration_fails_early(settings):
    with pytest.raises(ValueError):
        module("config").Config(**settings)


def test_explicit_voice_resolves_to_reference_id():
    config = module("config").Config(voice="db89e349112e44dca6820e0cb2d414cc")
    assert config.reference_id == "db89e349112e44dca6820e0cb2d414cc"
    assert config.input_device == "MIC"
    assert config.output_device == "AI Voice"


def test_partials_are_revised_without_speaking_and_final_replay_is_suppressed():
    commit = module("transcript").Committer()
    assert commit.accept({"type": "partial", "utterance_id": "a", "text": "не"}) is None
    assert commit.accept({"type": "partial", "utterance_id": "a", "text": "нет"}) is None
    assert commit.accept({"type": "final", "utterance_id": "a", "text": " нет "}) == "нет"
    assert commit.accept({"type": "final", "utterance_id": "a", "text": "нет"}) is None
    assert commit.accept({"type": "final", "utterance_id": "b", "text": "нет"}) == "нет"


@pytest.mark.parametrize("event", [
    {"type": "final", "text": "да"},
    {"type": "final", "utterance_id": "a", "text": " "},
    {"type": "final", "utterance_id": "a", "text": 42},
    {"type": "ready"},
])
def test_invalid_or_control_event_never_generates_speech(event):
    assert module("transcript").Committer().accept(event) is None


@pytest.mark.parametrize("status,stdout,want", [(0, "hidden\n", "hidden"), (44, "", None), (1, "secret on failure", None), (0, "", None)])
def test_keychain_requires_success_and_nonempty_value(monkeypatch, status, stdout, want):
    secrets = module("secrets")
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, status, stdout=stdout, stderr="")
    monkeypatch.setattr(secrets.subprocess, "run", run)
    assert secrets.load_key() == want
    assert calls[0][0] == ["security", "find-generic-password", "-a", secrets.KEYCHAIN_ACCOUNT,
                           "-s", f"{secrets.KEYCHAIN_SERVICE_PREFIX}/FISH_API_KEY", "-w"]
    assert calls[0][1]["capture_output"] is True


def test_devices_preserve_actual_names_and_capabilities(monkeypatch):
    devices = module("devices")
    monkeypatch.setattr(devices.sd, "query_devices", lambda: [
        {"name": "MIC", "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 48000.0},
        {"name": "AI Voice", "max_input_channels": 2, "max_output_channels": 2, "default_samplerate": 48000.0},
    ])
    result = devices.list_devices()
    assert [(d["index"], d["name"], d["max_output_channels"]) for d in result] == [(0, "MIC", 0), (1, "AI Voice", 2)]


def test_unknown_model_is_rejected_before_api_can_fall_back_to_paid_model():
    with pytest.raises(ValueError, match="model"):
        module("config").Config(model="s2.1-pro-fre")


def test_paid_model_cannot_be_selected_in_free_only_delivery():
    with pytest.raises(ValueError, match="free"):
        module("config").Config(model="s2.1-pro")
