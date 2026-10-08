"""GitHub release checks with an opt-out and a persisted daily cache."""
import hashlib
import io
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__, paths

logger = logging.getLogger(__name__)

REPO = "coldmoth/ai-voice"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_URL_PREFIX = f"https://github.com/{REPO}/releases/"
INTERVAL = 24 * 3600


def parse_version(text: object) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    parts = text.removeprefix("v").split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_newer(remote: str, local: str) -> bool:
    remote_version, local_version = parse_version(remote), parse_version(local)
    return remote_version is not None and local_version is not None and remote_version > local_version


def check(state: dict, auto: bool, *, now: float, force: bool = False,
          client: httpx.Client | None = None) -> tuple[dict, dict]:
    if not force and not auto:
        return {"status": "disabled"}, state
    new_state = state
    if force or not 0 <= now - state["last_check"] < INTERVAL:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"AI-Voice/{__version__}"}
        try:
            if client is not None:
                response = client.get(API, headers=headers, timeout=8, follow_redirects=False)
            else:
                with httpx.Client(timeout=8) as own:
                    response = own.get(API, headers=headers, follow_redirects=False)
        except (httpx.HTTPError, OSError, TimeoutError):
            return {"status": "error", "error": "network"}, state
        latest = None
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                return {"status": "error", "error": "github"}, state
            version = parse_version(body.get("tag_name")) if isinstance(body, dict) else None
            url = body.get("html_url") if isinstance(body, dict) else None
            if (version is None or not isinstance(url, str) or not url.startswith(RELEASE_URL_PREFIX)
                    or body.get("draft") or body.get("prerelease")):
                return {"status": "error", "error": "github"}, state
            latest = {"version": ".".join(str(part) for part in version), "url": url}
        elif response.status_code != 404:
            return {"status": "error", "error": "github"}, state
        new_state = {**state, "last_check": now, "latest": latest}
    latest = new_state["latest"]
    if (latest is not None and is_newer(latest["version"], __version__)
            and (force or latest["version"] != new_state["skipped"])):
        return {"status": "available", **latest}, new_state
    return {"status": "current", "version": __version__}, new_state


# ---- In-app install: download, verify SHA256, hand over to the platform installer ----
# GitHub serves release assets through a redirect to its own CDN; only these hosts are followed.
CDN_HOSTS = {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
MAX_BYTES = 600 * 1024 * 1024
MAX_REDIRECTS = 5
_lock = threading.Lock()
_job = {"state": "idle", "percent": 0, "error": None}

# Arguments: app pid, backend pid, new app, target app, stage dir, work dir.
SWAP_SCRIPT = """#!/bin/bash
n=0
while kill -0 "$1" 2>/dev/null || kill -0 "$2" 2>/dev/null; do
  n=$((n+1))
  if [ "$n" -gt 120 ]; then rm -rf "$5" "$6"; exit 1; fi
  sleep 0.5
done
old="$4.old-update"
rm -rf "$old"
mv "$4" "$old" || { rm -rf "$5" "$6"; open "$4"; exit 1; }
if mv "$3" "$4"; then rm -rf "$old"; else mv "$old" "$4"; fi
rm -rf "$5" "$6"
open "$4"
"""
WINDOWS_ARGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART", "/FORCECLOSEAPPLICATIONS", "/RELAUNCH=1"]


class UpdateError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def asset_name(version: str, platform: str) -> str | None:
    if platform == "win32":
        return f"AI-Voice-Setup-{version}.exe"
    if platform == "darwin":
        return f"AI-Voice-{version}.zip"
    return None


def allowed_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.port not in (None, 443) or ".." in parts.path:
        return False
    if parts.hostname == "github.com":
        return parts.path.startswith(f"/{REPO}/releases/")
    return parts.hostname in CDN_HOSTS


class _Sink:
    def __init__(self, out, digest=None):
        self.out, self.digest = out, digest

    def write(self, chunk: bytes) -> None:
        self.out.write(chunk)
        if self.digest is not None:
            self.digest.update(chunk)


def _fetch(client: httpx.Client, url: str, sink, limit: int, *, missing: str = "network", progress=None) -> None:
    headers = {"User-Agent": f"AI-Voice/{__version__}"}
    for _ in range(MAX_REDIRECTS + 1):
        if not allowed_url(url):
            raise UpdateError("url")
        with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("location", ""))
                continue
            if response.status_code == 404:
                raise UpdateError(missing)
            if response.status_code != 200:
                raise UpdateError("network")
            length = response.headers.get("content-length", "")
            total = int(length) if length.isdecimal() else 0
            if total > limit:
                raise UpdateError("too_large")
            done = 0
            for chunk in response.iter_bytes(65536):
                done += len(chunk)
                if done > limit:
                    raise UpdateError("too_large")
                sink.write(chunk)
                if progress and total:
                    progress(min(99, done * 100 // total))
            return
    raise UpdateError("url")


def job() -> dict:
    with _lock:
        return dict(_job)


def _set(**fields) -> None:
    with _lock:
        _job.update(fields)


def _target_app() -> Path:
    for parent in paths.ROOT.parents:
        if parent.suffix == ".app":
            return parent
    raise UpdateError("unsupported")


def _install_macos(archive: Path, work: Path, target: Path) -> None:
    try:
        stage = Path(tempfile.mkdtemp(prefix=".ai-voice-update-", dir=target.parent))
    except OSError:
        raise UpdateError("not_writable") from None
    try:
        subprocess.run(["ditto", "-x", "-k", str(archive), str(stage)], check=True, timeout=300)
        new = stage / target.name
        if not new.is_dir():
            raise UpdateError("failed")
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], timeout=60)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(new)], check=True, timeout=120)
        script = work / "swap.sh"
        script.write_text(SWAP_SCRIPT, encoding="utf-8")
        subprocess.Popen(
            ["/bin/bash", str(script), str(os.getppid()), str(os.getpid()), str(new), str(target), str(stage), str(work)],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    os.kill(os.getppid(), signal.SIGTERM)  # the shell quits; the script swaps the bundle and reopens it


def _install_windows(installer: Path) -> None:
    detached = getattr(subprocess, "DETACHED_PROCESS", 8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    subprocess.Popen([str(installer), *WINDOWS_ARGS], creationflags=detached, close_fds=True)
    # No /T: the installer is our child and must outlive the shell.
    subprocess.Popen(["taskkill", "/PID", str(os.getppid()), "/F"],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def run_install(version: str, client: httpx.Client | None = None) -> None:
    work = paths.app_home() / "updates"
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        name = asset_name(version, sys.platform)
        if name is None:
            raise UpdateError("unsupported")
        target = _target_app() if sys.platform == "darwin" else None
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        base = f"{RELEASE_URL_PREFIX}download/v{version}/"
        checksum = io.BytesIO()
        _fetch(client, base + name + ".sha256", checksum, 4096, missing="no_checksum")
        words = checksum.getvalue().decode("ascii", "replace").split()
        expected = words[0].lower() if words else ""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise UpdateError("no_checksum")
        file, digest = work / name, hashlib.sha256()
        with file.open("wb") as handle:
            _fetch(client, base + name, _Sink(handle, digest), MAX_BYTES, progress=lambda p: _set(percent=p))
        if digest.hexdigest() != expected:
            raise UpdateError("checksum")
        _set(state="installing", percent=100)
        if sys.platform == "darwin":
            _install_macos(file, work, target)
        else:
            _install_windows(file)
    except UpdateError as exc:
        shutil.rmtree(work, ignore_errors=True)
        _set(state="error", error=exc.code)
    except httpx.HTTPError:
        shutil.rmtree(work, ignore_errors=True)
        _set(state="error", error="network")
    except Exception as exc:
        logger.warning("Update install failed: %s", str(exc)[:300])
        shutil.rmtree(work, ignore_errors=True)
        _set(state="error", error="failed")
    finally:
        if own:
            client.close()


def start_install(version: str) -> bool:
    with _lock:
        if _job["state"] in ("downloading", "installing"):
            return False
        _job.update(state="downloading", percent=0, error=None)
    threading.Thread(target=run_install, args=(version,), daemon=True).start()
    return True
