"""Install and inspect the bundled virtual microphone through the macOS admin prompt."""
import logging
from pathlib import Path
import plistlib
import shlex
import subprocess
import threading
from xml.parsers.expat import ExpatError

from . import paths

NAME = "AIVoiceMic.driver"
DEVICE = "AI Voice Mic"
HAL = Path("/Library/Audio/Plug-Ins/HAL")
logger = logging.getLogger(__name__)
_lock = threading.Lock()
_job = {"state": "idle", "action": None}


def bundled() -> Path | None:
    return next((p for p in (paths.ROOT / "driver" / NAME,
                            paths.ROOT / "build" / "driver" / NAME) if p.is_dir()), None)


def _version(bundle: Path | None) -> str | None:
    if bundle is None:
        return None
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as stream:
            value = plistlib.load(stream).get("CFBundleShortVersionString")
        return value if isinstance(value, str) else None
    except (OSError, ValueError, TypeError, AttributeError, ExpatError):
        return None


def status(names: list[str]) -> dict:
    source = bundled()
    installed = HAL / NAME
    return {"available": source is not None, "installed": installed.is_dir(),
            "device_present": DEVICE in names, "bundled_version": _version(source),
            "installed_version": _version(installed), "job": job()}


def script(action: str, src: Path | None) -> str:
    final = shlex.quote(str(HAL / NAME))
    if action == "uninstall":
        return f"rm -rf {final} && killall coreaudiod"
    if action != "install" or src is None:
        raise ValueError("Invalid driver action or missing source")
    temporary = shlex.quote(str(HAL / (NAME + ".tmp")))
    source = shlex.quote(str(src))
    return (f"rm -rf {temporary} && ditto {source} {temporary} && "
            f"xattr -dr com.apple.quarantine {temporary}; rm -rf {final} && "
            f"mv {temporary} {final} && chown -R root:wheel {final} && killall coreaudiod")


def _run(line: str) -> str:
    escaped = line.replace("\\", "\\\\").replace('"', '\\"')
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'do shell script "{escaped}" with administrator privileges'],
            capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", None) or str(exc)
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        logger.warning("Driver command failed: %s", stderr[:300])
        return "failed"
    if result.returncode == 0:
        return "ok"
    if "-128" in result.stderr:
        return "cancelled"
    logger.warning("Driver command failed: %s", result.stderr[:300])
    return "failed"


def job() -> dict:
    with _lock:
        return dict(_job)


def start(action: str, *, before, after) -> bool:
    with _lock:
        if _job["state"] == "running":
            return False
        _job.update(state="running", action=action)

    def work():
        state = "failed"
        try:
            try:
                before()
            except Exception as exc:
                logger.warning("Driver audio stop failed: %s", str(exc)[:300])
            result = _run(script(action, bundled()))
            if result == "ok":
                after()
                state = "done"
            else:
                state = result
        except Exception as exc:
            logger.warning("Driver job failed: %s", str(exc)[:300])
        finally:
            with _lock:
                _job.update(state=state)

    threading.Thread(target=work, daemon=True).start()
    return True
