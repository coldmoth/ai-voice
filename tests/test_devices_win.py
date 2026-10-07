"""Offline cable discovery checks with both platform branches patched."""
import sys

import pytest

from ai_voice import catalog, devices


@pytest.mark.parametrize("stage", ["query_devices", "query_hostapis", "WasapiSettings"])
@pytest.mark.parametrize("error", [
    ValueError("Unknown device name"),
    ValueError("Not an output device: 'CABLE Output (VB-Audio Virtual '"),
    devices.sd.PortAudioError("Device lookup failed"),
])
@pytest.mark.parametrize("kind", ["output", "input"])
def test_stream_extra_settings_lookup_failure_returns_none(monkeypatch, stage, error, kind):
    from types import SimpleNamespace

    monkeypatch.setattr(devices, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(devices.sd, "query_devices", lambda device, **kw: {"hostapi": 0})
    monkeypatch.setattr(devices.sd, "query_hostapis", lambda index: {"name": "Windows WASAPI"})

    def fail(*args, **kwargs):
        raise type(error)(*error.args)

    monkeypatch.setattr(devices.sd, stage, fail)
    assert devices.stream_extra_settings("CABLE Output", kind=kind) is None


def test_stream_extra_settings_defaults_to_output(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(devices, "sys", SimpleNamespace(platform="win32"))

    def query_device(device, *, kind):
        assert device == "CABLE Output"
        assert kind == "output"
        raise ValueError("Not an output device")

    monkeypatch.setattr(devices.sd, "query_devices", query_device)
    assert devices.stream_extra_settings("CABLE Output") is None


@pytest.mark.parametrize("platform,hostapi,enabled", [
    ("win32", "Windows WASAPI", True),
    ("win32", "MME", False),
    ("darwin", "Windows WASAPI", False),
])
@pytest.mark.parametrize("device,kind", [(7, "output"), ("mic", "input"),
                                         (None, "input"), (None, "output")])
def test_stream_extra_settings(monkeypatch, platform, hostapi, enabled, device, kind):
    from types import SimpleNamespace

    monkeypatch.setattr(devices, "sys", SimpleNamespace(platform=platform))

    def query_device(selected, *, kind):
        assert platform == "win32", "macOS must not query devices for extra settings"
        assert selected == device
        assert kind == expected_kind
        return {"hostapi": 2}

    def query_hostapi(index):
        assert index == 2
        return {"name": hostapi}

    expected_kind = kind
    monkeypatch.setattr(devices.sd, "query_devices", query_device)
    monkeypatch.setattr(devices.sd, "query_hostapis", query_hostapi)
    monkeypatch.setattr(devices.sd, "WasapiSettings", lambda **kw: SimpleNamespace(**kw))
    settings = devices.stream_extra_settings(device, kind=kind)
    if enabled:
        assert settings.auto_convert is True
    else:
        assert settings is None


@pytest.mark.parametrize("platform,hostapi,enabled", [
    ("win32", "Windows WASAPI", True), ("win32", "MME", False),
    ("darwin", "Windows WASAPI", False),
])
@pytest.mark.parametrize("sink_name,fallback", [
    ("playback", False), ("monitor", False), ("monitor", True),
    ("preview", False), ("preview", True),
])
def test_output_stream_settings(monkeypatch, platform, hostapi, enabled, sink_name, fallback):
    from types import SimpleNamespace
    from ai_voice import monitor, playback, preview

    monkeypatch.setattr(devices, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(devices.sd, "query_devices", lambda device, **kw: {
        "hostapi": 0, "default_samplerate": 48000, "max_output_channels": 2})
    monkeypatch.setattr(devices.sd, "query_hostapis", lambda index: {"name": hostapi})
    monkeypatch.setattr(devices.sd, "WasapiSettings", lambda **kw: SimpleNamespace(**kw))
    attempts = []

    def open_stream(**kwargs):
        assert kwargs["device"] == 7
        settings = kwargs["extra_settings"]
        if enabled:
            assert settings.auto_convert is True
        else:
            assert settings is None
        attempts.append(kwargs["samplerate"])
        if fallback and len(attempts) == 1:
            raise devices.sd.PortAudioError("fake rate rejection")
        return SimpleNamespace(start=lambda: None, stop=lambda: None, close=lambda: None)

    monkeypatch.setattr(devices.sd, "OutputStream", open_stream)
    monkeypatch.setattr(playback, "resolve_output", lambda name: 7)
    monkeypatch.setattr(monitor, "resolve_output", lambda name: 7)
    # These modules may have independent sounddevice doubles from other offline tests.
    for module in (playback, monitor, preview):
        monkeypatch.setattr(module, "sd", devices.sd)
    if sink_name == "playback":
        sink = playback.Playback(playback.PCMBuffer(output_rate=16000),
                                 device="cable", sample_rate=16000)
    elif sink_name == "monitor":
        sink = monitor.MonitorSink(device="speakers", sample_rate=16000)
    else:
        sink = preview._PreviewSink(7, 16000)
    try:
        (sink.open if sink_name == "preview" else sink.start)()
        assert attempts == ([16000, 48000] if fallback else [16000])
    finally:
        sink.close()


WIN_OUTPUTS = ("CABLE Input", "VB-Audio Virtual Cabl", "VoiceMeeter Input", "VoiceMeeter Aux Input",
               "Line 1", "Steam Streaming Microphone")


@pytest.fixture(params=["win32", "darwin"])
def platform(monkeypatch, request):
    monkeypatch.setattr(sys, "platform", request.param)
    return request.param


@pytest.fixture
def device_list(monkeypatch):
    rows = []
    monkeypatch.setattr(devices, "list_devices", lambda: rows)
    monkeypatch.setattr(devices.sd, "query_hostapis", lambda: [
        {"name": "MME"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}])

    def populate(*entries):
        rows[:] = [dict(index=index, name=name, hostapi=hostapi,
                       max_output_channels=channels)
                   for index, (name, hostapi, channels) in enumerate(entries)]
        return [row["name"] for row in rows if row["max_output_channels"] > 0]

    return populate


@pytest.mark.parametrize("prefix", WIN_OUTPUTS)
def test_preferred_virtual_output_substring_match(platform, device_list, prefix):
    name = f"Localized {prefix.swapcase()} (VB-Audio Virtual Cable)"
    names = device_list((name, 0, 2))
    assert devices.preferred_virtual_output(names) == (name if platform == "win32" else None)


def test_preference_order(platform, device_list):
    cable = "CABLE Input (VB-Audio Virtual Cable)"
    names = device_list(("VoiceMeeter Input", 1, 2), (cable, 0, 2),
                        ("AI Voice Mic", 0, 2), ("AI Voice", 0, 2))
    expected = cable if platform == "win32" else "AI Voice Mic"
    assert devices.preferred_virtual_output(names) == expected


def test_wasapi_preferred(platform, device_list):
    name = "CABLE Input (VB-Audio Virtual Cable)"
    names = device_list((name, 0, 2), (name, 1, 2))
    if platform == "win32":
        assert devices.preferred_virtual_output(names) == name
        assert devices.resolve_output(name) == 1
    else:
        assert devices.preferred_virtual_output(names) is None
        with pytest.raises(ValueError, match="ambiguous"):
            devices.resolve_output(name)


def test_wasapi_preferred_different_names(platform, device_list):
    mme = "CABLE Input"
    wasapi = "CABLE Input (VB-Audio Virtual Cable)"
    names = device_list((mme, 0, 2), (wasapi, 1, 2))
    assert devices.preferred_virtual_output(names) == (wasapi if platform == "win32" else None)


def test_none_when_no_cable(platform, device_list):
    names = device_list(("Speakers", 1, 2),
                        ("CABLE Output (VB-Audio Virtual Cable)", 0, 0),
                        ("CABLE Input (recording only)", 1, 0))
    assert devices.preferred_virtual_output(names) is None


def test_mme_fallback(platform, device_list):
    name = "CABLE Input (VB-Audio Virtual Cable)"
    names = device_list((name, 0, 2), (name, 1, 0))
    assert devices.preferred_virtual_output(names) == (name if platform == "win32" else None)
    assert devices.resolve_output(name) == 0


@pytest.mark.parametrize("name,hostapi", [
    ("Speakers (VB-Audio Virtual Cable)", 1),
    ("Speakers (VB-Audio Virtual Cabl", 0),
])
def test_speakers_cable_names(platform, device_list, name, hostapi):
    names = device_list((name, hostapi, 2))
    assert devices.preferred_virtual_output(names) == (name if platform == "win32" else None)
    assert devices.virtual_outputs(names) == (names if platform == "win32" else [])
    assert catalog._is_monitor_blacklisted(name) is (platform == "win32")


def test_speakers_wasapi_preferred(platform, device_list):
    mme = "Speakers (VB-Audio Virtual Cabl"
    wasapi = "Speakers (VB-Audio Virtual Cable)"
    names = device_list((mme, 0, 2), (wasapi, 2, 2), (wasapi, 1, 2))
    assert devices.preferred_virtual_output(names) == (wasapi if platform == "win32" else None)
    if platform == "win32":
        assert devices.resolve_output(wasapi) == 2


@pytest.mark.parametrize("name,hostapi", [
    ("Speakers (VB-Audio Point)", 2),
    ("Speakers (VB-Audio Virtual Cable)", 2),
    ("CABLE Input", 2),
    ("Speakers (Realtek High Definition Audio)", 1),
])
def test_unsupported_cable_outputs(platform, device_list, name, hostapi):
    names = device_list((name, hostapi, 2))
    assert devices.preferred_virtual_output(names) is None
    if platform == "win32" and hostapi == 2:
        with pytest.raises(ValueError, match="missing or ambiguous"):
            devices.resolve_output(name)


@pytest.mark.parametrize("prefix", WIN_OUTPUTS)
def test_monitor_blacklist(platform, prefix):
    name = f"Localized {prefix.swapcase()} (virtual cable)"
    expected = platform == "win32" or prefix == "Steam Streaming Microphone"
    assert catalog._is_monitor_blacklisted(name) is expected
    assert catalog._is_monitor_blacklisted("Headphones") is False
    assert catalog._is_monitor_blacklisted("BlackHole 2ch") is True
    assert catalog._is_monitor_blacklisted(None) is True


def test_virtual_outputs(platform):
    names = ["Speakers", *(f"{prefix} (virtual cable)" for prefix in WIN_OUTPUTS),
             "AI Voice Mic", "BlackHole 2ch"]
    expected = names[1:1 + len(WIN_OUTPUTS)] if platform == "win32" else names[1 + len(WIN_OUTPUTS):]
    assert devices.virtual_outputs(names + names) == expected


def test_mac_exact_matching(platform, device_list):
    names = device_list(("BlackHole 2ch", 0, 2), ("AI Voice", 0, 2),
                        ("AI Voice Mic", 0, 2), ("AI Voice Mic extra", 0, 2))
    assert devices.preferred_virtual_output(names) == (None if platform == "win32" else "AI Voice Mic")
    assert devices.resolve_output("AI Voice Mic") == 2


def test_windows_output_preference_constant():
    assert devices.WIN_VIRTUAL_OUTPUTS == WIN_OUTPUTS
