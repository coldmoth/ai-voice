"""Local command line entry point. INFO never prints recognized phrases."""
import argparse
import asyncio
from dataclasses import fields
import logging
from pathlib import Path
import subprocess
import sys
import tomllib

import httpx

from .asr import ASRError, GigaAMASR, SpeechASR, WhisperASR, engine_for_language
from .config import Config
from .catalog import Catalog
from .devices import list_devices, resolve_output
from .fish_tts import FishTTS, TTSError
from .pipeline import SpeechPipeline
from .secrets import load_key, migrate_legacy

from .i18n import t, set_language
from .paths import CONFIG_FILE, HELPER_APP, HELPER_BINARY, ROOT
log = logging.getLogger(__name__)
DOCTOR_SAMPLE = "\u041f\u0440\u0438\u0432\u0435\u0442"  # Privet (Russian TTS diagnostic sample)
AUTHORIZE = f'open -n "{HELPER_APP}" --args --authorize'


def _config(voice=None):
    path = CONFIG_FILE
    settings = {}
    if path.exists():
        with path.open("rb") as source:
            settings = tomllib.load(source)
        unknown = settings.keys() - {field.name for field in fields(Config)}
        if unknown:
            raise ValueError(t("errors.cli_config_unknown", names=", ".join(sorted(unknown))))
    if voice is not None:
        settings["voice"] = voice
    elif settings.get("voice") is None:
        settings["voice"] = Catalog().preferences()["voice_id"]
    if settings["voice"] is None:
        raise ValueError(t("errors.cli_voice_required"))
    return Config(**settings)


def _devices(config):
    devices = list_devices()
    inputs = [d for d in devices if d["name"] == config.input_device and d["max_input_channels"] > 0]
    if len(inputs) != 1:
        raise RuntimeError(t("errors.cli_input_ambiguous", device=repr(config.input_device)))
    resolve_output(config.output_device)


def _helper(config, engine="apple"):
    binary = HELPER_BINARY
    if sys.platform != "win32" and not binary.exists():
        raise RuntimeError(t("errors.cli_helper_missing"))
    selected = engine_for_language(config.language, engine, platform=sys.platform)
    cls = {"apple": SpeechASR, "gigaam": GigaAMASR, "whisper": WhisperASR}[selected]
    return cls(input_device=config.input_device, language=config.language,
               endpoint_ms=config.endpoint_ms)


def _permissions(report, engine="apple", app=False):
    apple = engine != "gigaam"
    names = ("microphone_authorization", "speech_authorization") if apple else ("microphone_authorization",)
    if any(report.get(name) != "authorized" for name in names):
        raise RuntimeError(t("errors.app_permissions") if app else t("errors.cli_permissions", command=AUTHORIZE))
    if apple and (not report.get("supports_on_device") or not report.get("available")):
        raise RuntimeError(t("errors.cli_local_asr"))
    if not report.get("input_found"):
        raise RuntimeError(t("errors.cli_input_missing"))


async def _start(config):
    from .instance import AudioLease
    lease = AudioLease()
    lease.acquire()
    try:
        key = load_key()
        if key is None:
            raise RuntimeError(t("status.fish_key_missing"))
        _devices(config)
        asr = _helper(config)
        report = await asr.doctor()
        _permissions(report)
        log.info(t("cli.start_voice", reference=config.reference_id, input=config.input_device, output=config.output_device))
        log.info(t("cli.start_waiting"))
        await SpeechPipeline(config, key, asr).run()
    finally:
        lease.close()

async def _doctor(config, tts_check=False):
    _devices(config)
    print(t("cli.devices_available", input=config.input_device, output=config.output_device))
    key = load_key()
    print(t("cli.key_status", state=t("cli.key_present") if key else t("cli.key_missing")))
    report = await _helper(config).doctor()
    print(t("cli.microphone_status", state=report.get("microphone_authorization")))
    print(t("cli.recognition_status", state=report.get("speech_authorization")))
    _permissions(report)
    print(t("cli.local_asr_available"))
    if tts_check:
        if not key:
            raise RuntimeError(t("errors.cli_tts_key"))
        async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as client:
            count = 0
            async for chunk in FishTTS(config, key, client=client).stream(DOCTOR_SAMPLE):
                count += len(chunk)
        print(t("cli.pcm_result", count=count))
    else:
        print(t("cli.no_synthesis_check"))


def main(argv=None):
    set_language(Catalog().preferences()["language"])
    migrate_legacy()
    parser = argparse.ArgumentParser(description=t("cli.description"))
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help=t("cli.start_help"))
    start.add_argument("--voice", help=t("cli.voice_id_help"))
    start.add_argument("--debug", action="store_true", help=t("cli.debug_help"))
    doctor = commands.add_parser("doctor", help=t("cli.doctor_help"))
    doctor.add_argument("--voice", help=t("cli.voice_id_help"))
    doctor.add_argument("--tts", action="store_true", help=t("cli.tts_help"))
    commands.add_parser("devices", help=t("cli.devices_help"))
    commands.add_parser("voices", help=t("cli.voices_help"))
    commands.add_parser("setup-key", help=t("cli.setup_key_help"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
                        format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "devices":
            for device in list_devices():
                print(t("cli.device_details", index=device["index"], name=device["name"],
                        inputs=device["max_input_channels"], outputs=device["max_output_channels"],
                        rate=format(device["default_samplerate"], "g")))
        elif args.command == "voices":
            voices = Catalog().voices()
            print("\n".join(f"{v['id']} — {v['name']}" for v in voices)
                  if voices else t("cli.no_voices"))
        elif args.command == "setup-key":
            return subprocess.call(["/bin/zsh", str(ROOT / "scripts" / "save-fish-key.command")])
        elif args.command == "doctor":
            asyncio.run(_doctor(_config(args.voice), args.tts))
        else:
            asyncio.run(_start(_config(args.voice)))
    except KeyboardInterrupt:
        print("\n" + t("cli.stopped"))
        return 0
    except (ASRError, TTSError, RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        print(t("cli.error", exc=exc), file=sys.stderr)
        return 2 if str(exc) == t("errors.cli_voice_required") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
