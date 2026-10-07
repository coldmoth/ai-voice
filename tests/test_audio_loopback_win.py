"""Exercise the VB-CABLE playback-to-recording path on Windows."""

import sys
from contextlib import ExitStack

import pytest

from ai_voice import devices as audio_devices


def _find_cable_pairs(devices):
    names = audio_devices.virtual_outputs([device["name"] for device in devices])
    hostapis = audio_devices.sd.query_hostapis()
    pairs = []
    for api_name in ("MME", "Windows DirectSound", "Windows WASAPI"):
        for api, hostapi in enumerate(hostapis):
            if hostapi["name"] != api_name:
                continue
            inputs = [index for index, device in enumerate(devices)
                      if device["hostapi"] == api
                      and "cable output" in device["name"].lower()
                      and device["max_input_channels"] > 0]
            outputs = [index for index, device in enumerate(devices)
                       if device["hostapi"] == api and device["name"] in names
                       and device["max_output_channels"] > 0]
            pairs.extend((recording, output) for recording in inputs for output in outputs)
    return pairs


@pytest.fixture
def cable_devices(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(audio_devices.sd, "query_hostapis", lambda: [
        {"name": "MME"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"},
        {"name": "Windows DirectSound"}])

    def populate(*entries):
        rows = [dict(name=name, hostapi=api, max_input_channels=inputs,
                     max_output_channels=outputs)
                for name, api, inputs, outputs in entries]
        monkeypatch.setattr(audio_devices.sd, "query_devices", lambda: rows)
        return rows
    return populate


@pytest.mark.parametrize("mme,wasapi", [
    ("Speakers (VB-Audio Virtual Cabl", "Speakers (VB-Audio Virtual Cable)"),
    ("CABLE Input (VB-Audio Virtual C", "cable INPUT (VB-Audio Virtual Cable)"),
])
def test_cable_pairs_host_order(cable_devices, mme, wasapi):
    rows = cable_devices((mme, 0, 0, 2), ("CABLE Output", 0, 2, 0),
                         (wasapi, 1, 0, 2), ("cable OUTPUT", 1, 2, 0),
                         (wasapi, 2, 0, 2), ("CABLE Output", 2, 2, 0),
                         (wasapi, 3, 0, 2), ("CABLE Output", 3, 2, 0))
    assert _find_cable_pairs(rows) == [(1, 0), (7, 6), (3, 2)]


def test_cable_pair_mme_fallback(cable_devices):
    rows = cable_devices(("Speakers (VB-Audio Virtual Cabl", 0, 0, 2),
                         ("CABLE Output (VB-Audio Virtual C", 0, 2, 0))
    assert _find_cable_pairs(rows) == [(1, 0)]


@pytest.mark.parametrize("playback,api,input_api,input_channels", [
    ("Speakers (VB-Audio Point)", 2, 2, 2),
    ("Speakers (VB-Audio Virtual Cable)", 2, 2, 2),
    ("Speakers (Realtek High Definition Audio)", 1, 1, 2),
    ("Speakers (VB-Audio Virtual Cable)", 1, 0, 2),
    ("Speakers (VB-Audio Virtual Cable)", 1, 1, 0),
])
def test_cable_pair_missing(cable_devices, playback, api, input_api, input_channels):
    rows = cable_devices((playback, api, 0, 2),
                         ("CABLE Output", input_api, input_channels, 0))
    assert _find_cable_pairs(rows) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_cable_loopback_tone():
    import numpy as np
    import sounddevice as sd

    devices = sd.query_devices()
    names = [device["name"] for device in devices]

    pairs = _find_cable_pairs(devices)
    if not pairs:
        pytest.skip(f"No virtual playback/CABLE Output recording pair on the same host API; devices: {names!r}")
    frames = []

    def capture(indata, frame_count, time_info, status):
        frames.append(indata.copy())

    errors = []
    for recording, output in pairs:
        input_samplerate = int(devices[recording]["default_samplerate"])
        frames.clear()
        with ExitStack() as streams:
            try:
                input_stream = sd.InputStream(
                    device=recording, samplerate=input_samplerate, channels=1,
                    dtype="float32", callback=capture,
                    extra_settings=audio_devices.stream_extra_settings(recording, kind="input"),
                )
                streams.callback(input_stream.close)
                output_stream = sd.OutputStream(
                    device=output, samplerate=input_samplerate, channels=1, dtype="float32",
                    extra_settings=audio_devices.stream_extra_settings(output),
                )
                streams.callback(output_stream.close)
            except sd.PortAudioError as error:
                errors.append(f"input={recording}, output={output}: {error}")
                continue

            input_stream.start()
            output_stream.start()
            margin = int(0.3 * input_samplerate)
            tone = (10 ** (-12 / 20)) * np.sin(
                2 * np.pi * 1000 * np.arange(input_samplerate) / input_samplerate
            )
            playback = np.zeros((input_samplerate + 2 * margin, 1), dtype="float32")
            playback[margin : margin + input_samplerate, 0] = tone
            output_stream.write(playback)
            output_stream.stop()  # Drain queued output while capture is still running.
            sd.sleep(300)
        break
    else:
        pytest.skip("No CABLE pair could open: " + "; ".join(errors))

    assert frames, "Loopback captured no frames"
    recorded = np.concatenate(frames)
    assert np.isfinite(recorded).all(), "Recording contains non-finite samples"

    # Ignore startup and drain time; inspect the middle half-second of the tone.
    audible = np.flatnonzero(np.abs(recorded[:, 0]) > 10 ** (-30 / 20))
    assert audible.size, "Loopback RMS too low: no audible tone captured"
    start = audible[0] + input_samplerate // 4
    middle = recorded[start : start + input_samplerate // 2, 0].astype("float64")
    assert len(middle) == input_samplerate // 2, "Loopback recording ended before the tone"
    rms = np.sqrt(np.mean(middle**2))
    assert rms > 10 ** (-30 / 20), f"Loopback RMS too low: {rms:.6f}"
    spectrum = np.abs(np.fft.rfft((middle - middle.mean()) * np.hanning(len(middle))))
    frequencies = np.fft.rfftfreq(len(middle), d=1 / input_samplerate)
    peak = frequencies[np.argmax(spectrum)]
    assert abs(peak - 1000) <= 20, f"Loopback peak is {peak:.1f} Hz"
