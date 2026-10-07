"""Pinned, resumable installer for the optional live-voice runtime."""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from urllib.parse import urljoin, urlsplit

import httpx

from . import paths

LOG = logging.getLogger(__name__)
HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                   "codeload.github.com", "huggingface.co", "cdn-lfs.huggingface.co",
                   "cdn-lfs-us-1.hf.co", "cas-bridge.xethub.hf.co"})
CHUNK = 256 * 1024


def platform_key(sys_platform: str, machine: str) -> str | None:
    if sys_platform == "darwin" and machine.lower() == "arm64":
        return "darwin-arm64"
    if sys_platform == "win32" and machine.lower() in {"amd64", "x86_64"}:
        return "win-x64"
    return None


def _check_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or not (host in HOSTS or host.endswith(".hf.co"))):
        raise RuntimeError("network")


class _Cancelled(Exception):
    pass


class Installer:
    def __init__(self, root=None, manifest=None, client=httpx.Client):
        env = os.environ.get("AI_VOICE_VC_RUNTIME")
        self.root = Path(root if root is not None else env or paths.DATA / "vc-runtime").absolute()
        self.dev = root is None and not env
        self.manifest_path = Path(manifest) if manifest is not None else paths.ROOT / "vc_worker" / "engine-manifest.json"
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        platforms = manifest.get("platforms", {"darwin-arm64": manifest})
        self.platform = platform_key(sys.platform, platform.machine())
        if self.platform not in platforms:
            self.platform = None
        self.manifest = platforms.get(self.platform, platforms.get("darwin-arm64", {}))
        self.client = client
        self.work = self.root.parent / "vc-runtime.partial"
        self.old = self.root.parent / "vc-runtime.old"
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._process = None
        self._state = {"status": "idle", "step": "", "done_bytes": 0,
                       "total_bytes": sum(f["size"] for f in self.manifest["files"]) + self.manifest["uv"]["size"]
                       + self.manifest.get("wheels_estimate_bytes", 0), "error": None}

    def _dev_runtime(self):
        return self.dev or self.root.is_symlink() or (self.root / "venv").is_symlink()

    def state(self):
        with self._lock:
            result = dict(self._state)
        version = None
        try:
            version = json.loads((self.root / "engine.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        python = paths.VC_PYTHON if self.dev else self.root / ("venv/Scripts/python.exe" if self.platform == "win-x64" else "venv/bin/python")
        result.update(installed=python.is_file() and (self._dev_runtime() or version == self.manifest["version"]),
                      version=version, supported=self.platform is not None,
                      free_bytes=self._free_bytes())
        return result

    def _free_bytes(self):
        try:
            return shutil.disk_usage(self.root.parent).free
        except OSError:
            return 0

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _busy(self):
        return self._thread is not None and self._thread.is_alive()

    def _guard(self):
        if self._busy():
            raise RuntimeError("busy")
        if self._dev_runtime():
            raise RuntimeError("dev_runtime")
        if self.root.name != "vc-runtime" or self.root.parent.resolve() != self.root.parent:
            raise RuntimeError("disk")

    def start(self):
        with self._lock:
            self._guard()
            if self.platform is None:
                raise RuntimeError("unsupported")
            self._cancel.clear()
            self._update(status="checking", step="", done_bytes=0, error=None)
            self._thread = threading.Thread(target=self._run, daemon=True, name="vc-engine-install")
            self._thread.start()
        return self.state()

    def cancel(self):
        with self._lock:
            self._cancel.set()
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
        return self.state()

    def remove(self):
        from .storage import dir_size
        with self._lock:
            self._guard()
            freed = dir_size(self.root)
            try:
                self._delete(self.root)
            except OSError as exc:
                LOG.warning("VC removal: %s", str(exc)[:300])
                raise RuntimeError("disk") from None
            self._update(status="idle", step="", error=None, done_bytes=0)
            return {"freed_bytes": freed}

    def _check_cancel(self):
        if self._cancel.is_set():
            raise _Cancelled()

    def _safe(self, base, relative):
        relative = Path(relative)
        target = base / relative
        if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(base.resolve()):
            raise RuntimeError("disk")
        # Do not write through even an internal symlink from a previous attempt.
        if any(p.is_symlink() for p in (target, *target.parents) if p != base.parent):
            raise RuntimeError("disk")
        return target

    def _delete(self, path):
        if (path.parent != self.root.parent or path.name not in {"vc-runtime", "vc-runtime.partial", "vc-runtime.old"}
                or path.is_symlink() or path.parent.resolve() != path.parent):
            raise RuntimeError("disk")
        if path.exists():
            shutil.rmtree(path)

    def _prune(self, path):
        """Remove temporary contents without following links or calling rmtree outside runtime roots."""
        self._safe(self.work, path.relative_to(self.work))
        if path.is_dir():
            for child in path.iterdir():
                if child.is_symlink():
                    child.unlink()
                else:
                    self._prune(child)
            path.rmdir()
        else:
            path.unlink()

    def _relocate_venv(self):
        # uv uses absolute interpreter links and entry-point shebangs.
        before, after = str(self.work), str(self.root)
        venv = self.work / "venv"
        for folder, dirs, files in os.walk(venv, followlinks=False):
            for name in dirs + files:
                path = Path(folder) / name
                if path.is_symlink():
                    target = os.readlink(path)
                    if target == before or target.startswith(before + "/"):
                        path.unlink()
                        path.symlink_to(after + target[len(before):])
        config = venv / "pyvenv.cfg"
        if config.is_file() and not config.is_symlink():
            config.write_text(config.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
        for path in (venv / ("Scripts" if self.platform == "win-x64" else "bin")).iterdir():
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as source:
                    head = source.readline(4096)
                    if head.startswith(b"#!") and before.encode() in head:
                        rest = source.read()
                    else:
                        continue
                path.write_bytes(head.replace(before.encode(), after.encode()) + rest)

    def _download(self, client, entry):
        target = self._safe(self.work, "downloads/" + entry["id"] + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        offset = target.stat().st_size if target.exists() else 0
        if offset > entry["size"]:
            target.unlink()
            offset = 0
        self._update(step=entry["id"])
        if offset != entry["size"]:
            url = entry["url"]
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            hop, restarted = 0, False
            while True:
                self._check_cancel()
                _check_url(url)
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if hop == 10 or "location" not in response.headers:
                            raise RuntimeError("network")
                        url = urljoin(url, response.headers["location"])
                        hop += 1
                        continue
                    if response.status_code == 416:
                        target.unlink(missing_ok=True)
                        if restarted:
                            raise RuntimeError("network")
                        offset = 0
                        url, headers = entry["url"], {}
                        hop, restarted = 0, True
                        continue
                    if response.status_code not in (200, 206):
                        raise RuntimeError("network")
                    if response.status_code == 206:
                        value = response.headers.get("content-range", "")
                        if not value.startswith(f"bytes {offset}-"):
                            raise RuntimeError("network")
                    else:
                        offset = 0
                    self._update(done_bytes=self._state["done_bytes"] + offset)
                    with target.open("ab" if offset else "wb") as output:
                        for chunk in response.iter_bytes(CHUNK):
                            self._check_cancel()
                            output.write(chunk)
                            self._update(done_bytes=self._state["done_bytes"] + len(chunk))
                    break
        else:
            self._update(done_bytes=self._state["done_bytes"] + offset)
        self._check_cancel()
        with target.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != entry["sha256"] or target.stat().st_size != entry["size"]:
            target.unlink()
            raise RuntimeError("checksum")
        return target

    def _extract(self, archive, destination, strip=0, keep=None):
        destination.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    original = PurePosixPath(member.filename)
                    windows = PureWindowsPath(member.filename)
                    if (original.is_absolute() or ".." in original.parts or windows.root
                            or windows.drive or ".." in windows.parts
                            or (member.external_attr >> 16) & 0o170000 == 0o120000):
                        raise RuntimeError("disk")
                    self._safe(destination, member.filename)
                source.extractall(destination)
            return
        members = []
        with tarfile.open(archive, "r:gz") as source:
            for member in source.getmembers():
                original = PurePosixPath(member.name)
                windows = PureWindowsPath(member.name)
                if (original.is_absolute() or ".." in original.parts
                        or sys.platform == 'win32' and (windows.root or windows.drive or ".." in windows.parts)):
                    raise RuntimeError("disk")
                parts = original.parts[strip:]
                if not parts:
                    continue
                relative = Path(*parts)
                self._safe(destination, relative)
                # Upstream source archives need only regular files and directories.
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError("disk")
                if keep is not None and not any(relative == Path(k) or Path(k) in relative.parents for k in keep):
                    continue
                item = copy.copy(member)
                item.name = relative.as_posix()
                members.append(item)
            source.extractall(destination, members=members, filter="data")

    def _subprocess(self, argv, env, code, timeout=None):
        self._check_cancel()
        # A file avoids pipe deadlocks and lets cancel terminate a noisy uv process.
        log = self._safe(self.work, "process.log")
        with log.open("w+", encoding="utf-8") as output, log.open("r", encoding="utf-8") as progress:
            with self._lock:
                self._check_cancel()
                process = subprocess.Popen(argv, env=env, cwd=self.work, stdout=output, stderr=output)
                self._process = process
            try:
                deadline = time.monotonic() + timeout if timeout else None
                while process.poll() is None:
                    self._check_cancel()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError(code)
                    self._cancel.wait(.1)
                    if code == "uv" and progress.readline(4096).strip():
                        self._update(step="Installing packages")
                self._check_cancel()
                if process.returncode:
                    output.seek(0)
                    LOG.warning("VC %s: %s", code, output.read(300))
                    raise RuntimeError(code)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                with self._lock:
                    self._process = None

    def _swap(self):
        with self._lock:
            self._check_cancel()
            if self._dev_runtime():
                raise RuntimeError("dev_runtime")
            if self.old.is_symlink():
                raise RuntimeError("disk")
            # Recover a previous interrupted swap before starting a new one.
            if self.old.exists() and not self.root.exists():
                self.old.rename(self.root)
            self._delete(self.old)
            self._relocate_venv()
            moved = False
            if self.root.exists():
                self.root.rename(self.old)
                moved = True
            try:
                self.work.rename(self.root)
            except OSError:
                if moved:
                    self.old.rename(self.root)
                raise
            self._delete(self.old)

    def _run(self):
        code = "disk"
        try:
            if self._free_bytes() < self._state["total_bytes"] * 2.2:
                raise RuntimeError("disk")
            self._safe(self.root.parent, self.work.name)
            self.work.mkdir(parents=True, exist_ok=True)
            self._update(status="downloading")
            code = "network"
            with self.client(follow_redirects=False, timeout=30) as client:
                uv_entry = dict(self.manifest["uv"], id="uv")
                uv_archive = self._download(client, uv_entry)
                downloads = [(entry, self._download(client, entry)) for entry in self.manifest["files"]]
            code = "disk"
            self._check_cancel()
            self._update(status="installing", step="Extracting components")
            # Clear incomplete extracted content, preserving resumable downloads.
            for folder in ("spike", "venv", "uv", "uv-cache", "python"):
                target = self._safe(self.work, folder)
                if target.exists():
                    self._prune(target)
            uv_dir = self._safe(self.work, "uv")
            self._extract(uv_archive, uv_dir)
            uv = next(p for p in uv_dir.rglob("uv.exe" if self.platform == "win-x64" else "uv") if p.is_file())
            uv.chmod(0o755)
            for entry, downloaded in downloads:
                self._check_cancel()
                destination = self._safe(self.work, entry["dest"])
                if entry.get("extract") == "tar.gz":
                    self._extract(downloaded, destination, entry.get("strip", 0), entry.get("keep"))
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(downloaded, destination)
                parts = Path(entry["dest"]).parts
                for index, part in enumerate(parts):
                    if part.startswith("models--") and parts[index + 1:index + 2] == ("snapshots",):
                        ref = self._safe(self.work, Path(*parts[:index + 1]) / "refs/main")
                        ref.parent.mkdir(parents=True, exist_ok=True)
                        ref.write_text(parts[index + 2], encoding="utf-8")
            env = dict(os.environ, UV_CACHE_DIR=str(self.work / "uv-cache"),
                       UV_PYTHON_INSTALL_DIR=str(self.work / "python"))
            code = "uv"
            self._update(step="Installing packages")
            self._subprocess([str(uv), "venv", "--python", self.manifest["python"], str(self.work / "venv")], env, code)
            requirements = self._safe(self.manifest_path.parent, self.manifest["requirements"])
            python = self.work / ("venv/Scripts/python.exe" if self.platform == "win-x64" else "venv/bin/python")
            install = [str(uv), "pip", "install", "--python", str(python), "-r", str(requirements), "--no-deps"]
            if self.platform == "win-x64":
                # Use PyPI when the CUDA index lacks a package's exact locked version.
                install.extend(["--extra-index-url", self.manifest["torch_index"],
                                "--index-strategy", "unsafe-first-match"])
            self._subprocess(install, env, code)
            code = "smoke"
            self._update(status="verifying", step="Checking engine")
            smoke = "import torch; assert torch.backends.mps.is_available()"
            if self.platform == "win-x64":
                smoke = "import torch; print('device=' + ('cuda' if torch.cuda.is_available() else 'cpu'))"
            self._subprocess([str(python), "-c", smoke], env, code, timeout=60)
            code = "disk"
            (self.work / "engine.json").write_text(json.dumps({"version": self.manifest["version"], "installed_at": time.time()}), encoding="utf-8")
            # Cancellation is accepted until final cleanup and swap begin together.
            with self._lock:
                self._check_cancel()
                for folder in ("downloads", "uv-cache", "uv"):
                    target = self._safe(self.work, folder)
                    if target.exists():
                        self._prune(target)
                log = self._safe(self.work, "process.log")
                log.unlink(missing_ok=True)
                self._swap()
                self._update(status="idle", step="", error=None)
        except _Cancelled:
            self._update(status="cancelled", step="", error=None)
        except Exception as exc:
            LOG.warning("VC installation: %s", str(exc)[:300])
            if isinstance(exc, OSError) and code == "network":
                code = "disk"
            error = str(exc) if isinstance(exc, RuntimeError) and str(exc) in {
                "network", "checksum", "disk", "uv", "smoke", "dev_runtime", "unsupported", "busy"} else code
            self._update(status="error", error=error)


_singleton = None
_singleton_lock = threading.Lock()


def installer():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Installer()
        return _singleton


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install"])
    parser.parse_args()
    engine = installer()
    try:
        engine.start()
    except RuntimeError as exc:
        print(str(exc), flush=True)
        return 1
    previous = None
    try:
        while engine._busy():
            state = engine.state()
            progress = (state["status"], state["step"], state["done_bytes"])
            if progress != previous:
                print(f'{state["status"]}: {state["step"]} {state["done_bytes"]}/{state["total_bytes"]}', flush=True)
                previous = progress
            engine._thread.join(.5)
    except KeyboardInterrupt:
        engine.cancel()
        engine._thread.join()
    state = engine.state()
    print(state["error"] or state["status"], flush=True)
    return 0 if state["installed"] and state["status"] == "idle" else 1


if __name__ == "__main__":
    raise SystemExit(main())
