"""Read actual PortAudio/CoreAudio devices without altering them."""
import sounddevice as sd


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
    return next((name for name in ("AI Voice Mic", "AI Voice", "BlackHole 2ch")
                 if name in names), None)


VIRTUAL_PATTERNS = ("ai voice mic", "blackhole 2ch", "blackhole 16ch", "loopback", "ai voice")


def virtual_outputs(names: list[str]) -> list[str]:
    return list(dict.fromkeys(n for n in names if any(p in n.lower() for p in VIRTUAL_PATTERNS)))


def resolve_output(name: str) -> int:
    matches = [device for device in list_devices()
               if device["name"] == name and device["max_output_channels"] >= 2]
    if len(matches) != 1:
        raise ValueError(f"Output device {name!r} is missing or ambiguous; run ai-voice devices")
    return matches[0]["index"]
