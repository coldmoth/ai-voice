"""Install virtual microphones through the platform's administrator prompt."""
from collections.abc import Callable
import hashlib
import io
import logging
from pathlib import Path, PureWindowsPath
import plistlib
import shlex
import subprocess
import threading
import tempfile
from xml.parsers.expat import ExpatError
import zipfile

import httpx

from . import paths

NAME = "AIVoiceMic.driver"
DEVICE = "AI Voice Mic"
HAL = Path("/Library/Audio/Plug-Ins/HAL")
logger = logging.getLogger(__name__)
_lock = threading.Lock()
_job = {"state": "idle", "action": None}

VBCABLE_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
VBCABLE_SHA256 = "b950e39f01af1d04ea623c8f6d8eb9b6ea5c477c637295fabf20631c85116bfb"
VBCABLE_EXE = "VBCABLE_Setup_x64.exe"


def _download_cable(url: str) -> bytes:
    if httpx.URL(url).scheme != "https":
        raise ValueError("VB-CABLE download requires HTTPS")
    response = httpx.get(url, timeout=30, follow_redirects=False)
    response.raise_for_status()
    return response.content


def _run_cable_elevated(executable: Path) -> int:
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    executable = executable.resolve()
    info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=0x40,
                            lpVerb="runas", lpFile=str(executable),
                            lpDirectory=str(executable.parent), nShow=1)
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:  # ERROR_CANCELLED: UAC declined.
            return error
        raise OSError(error, "ShellExecuteExW failed")
    if not info.hProcess:
        raise OSError("ShellExecuteExW returned no process handle")
    try:
        if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) != 0:  # INFINITE, WAIT_OBJECT_0
            raise OSError(ctypes.get_last_error(), "Installer wait failed")
    finally:
        kernel.CloseHandle(info.hProcess)
    return 0


def install_cable(download: Callable = _download_cable,
                  run_elevated: Callable = _run_cable_elevated,
                  rescan: Callable[[], None] | None = None) -> dict:
    """Verify the full package; caller guards rescan against audio started during UAC."""
    from . import devices

    try:
        payload = download(VBCABLE_URL)
    except (httpx.HTTPError, OSError):
        return {"status": "network", "device": None}
    if hashlib.sha256(payload).hexdigest() != VBCABLE_SHA256:
        return {"status": "hash_mismatch", "device": None}
    with tempfile.TemporaryDirectory(prefix="ai-voice-cable-") as directory:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
                # Validate every member before writing even the first file.
                for member in bundle.infolist():
                    path = PureWindowsPath(member.filename)
                    if path.drive or path.root or ".." in path.parts or ":" in member.filename:
                        raise ValueError("Unsafe VB-CABLE archive path")
                bundle.getinfo(VBCABLE_EXE)
                bundle.extractall(directory)
        except (zipfile.BadZipFile, KeyError, ValueError):
            return {"status": "hash_mismatch", "device": None}
        result = run_elevated(Path(directory) / VBCABLE_EXE)
        if result == 1223:
            return {"status": "cancelled", "device": None}
        if result != 0:
            raise OSError(result, "VB-CABLE elevation failed")
    (devices.rescan if rescan is None else rescan)()
    names = [device["name"] for device in devices.list_devices()]
    device = devices.preferred_virtual_output(names)
    return {"status": "installed" if device else "reboot", "device": device}


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
