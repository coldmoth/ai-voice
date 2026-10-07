"""Read actual PortAudio/CoreAudio devices without altering them."""
import sys

import sounddevice as sd


def stream_extra_settings(device, *, kind="output") -> object | None:
    """Best-effort shared WASAPI rate conversion; lookup failures need no settings."""
    if sys.platform == "win32":
        try:
            info = sd.query_devices(device, kind=kind)
            if sd.query_hostapis(info["hostapi"])["name"] == "Windows WASAPI":
                return sd.WasapiSettings(auto_convert=True)
        except Exception:
            return None
    return None


WIN_VIRTUAL_OUTPUTS = ("CABLE Input", "VB-Audio Virtual Cabl", "VoiceMeeter Input", "VoiceMeeter Aux Input",
                       "Line 1", "Steam Streaming Microphone")


def rescan() -> None:
    # ponytail: PortAudio reinitialization closes every stream; callers must guarantee none is open.
    sd._terminate()
    sd._initialize()


def list_devices() -> list[dict]:
    return [dict(device, index=index) for index, device in enumerate(sd.query_devices())]


def default_input_name() -> str | None:
    try:
        return sd.query_devices(kind="input")["name"]
    except Exception:
        return None


def preferred_virtual_output(names: list[str]) -> str | None:
    if sys.platform == "win32":
        hostapis = sd.query_hostapis()
        outputs = [device for device in list_devices()
                   if device["name"] in names and device["max_output_channels"] >= 2
                   and "wdm-ks" not in hostapis[device["hostapi"]]["name"].lower()]
        outputs.sort(key=lambda device: "wasapi" not in
                     hostapis[device["hostapi"]]["name"].lower())
        return next((device["name"] for pattern in WIN_VIRTUAL_OUTPUTS
                     for device in outputs if pattern.lower() in device["name"].lower()), None)
    return next((name for name in ("AI Voice Mic", "AI Voice", "BlackHole 2ch")
                 if name in names), None)


VIRTUAL_PATTERNS = ("ai voice mic", "blackhole 2ch", "blackhole 16ch", "loopback", "ai voice")


def virtual_outputs(names: list[str]) -> list[str]:
    if sys.platform == "win32":
        return list(dict.fromkeys(n for n in names
                                  if any(p.lower() in n.lower() for p in WIN_VIRTUAL_OUTPUTS)))
    return list(dict.fromkeys(n for n in names if any(p in n.lower() for p in VIRTUAL_PATTERNS)))


def resolve_output(name: str) -> int:
    matches = [device for device in list_devices()
               if device["name"] == name and device["max_output_channels"] >= 2]
    if sys.platform == "win32" and matches:
        hostapis = sd.query_hostapis()
        matches = [device for device in matches
                   if "wdm-ks" not in hostapis[device["hostapi"]]["name"].lower()]
        wasapi = [device for device in matches
                  if "wasapi" in hostapis[device["hostapi"]]["name"].lower()]
        matches = wasapi or matches
    if len(matches) != 1:
        raise ValueError(f"Output device {name!r} is missing or ambiguous; run ai-voice devices")
    return matches[0]["index"]
