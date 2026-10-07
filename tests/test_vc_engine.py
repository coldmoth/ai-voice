"""Installer acceptance tests: local HTTP only; no real uv or runtime writes."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
from contextlib import closing
import json
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import httpx
import pytest

from ai_voice import vc_engine


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if isinstance(content, tuple):
                info.type, info.linkname = content
                tar.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
def local_server():
    state = SimpleNamespace(data={}, requests=[], interrupted=set(), redirect={}, pause=None, range_ignored=False,
                            range_rejections=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state.requests.append((self.path, self.headers.get("Range")))
            if self.path == "/weight" and state.range_rejections:
                state.range_rejections -= 1
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path in state.redirect:
                self.send_response(302)
                self.send_header("Location", state.redirect[self.path])
                self.end_headers()
                return
            data = state.data[self.path]
            offset = int(self.headers["Range"][6:-1]) if self.headers.get("Range") and not state.range_ignored else 0
            self.send_response(206 if offset else 200)
            self.send_header("Content-Length", str(len(data) - offset))
            if offset:
                self.send_header("Content-Range", f"bytes {offset}-{len(data)-1}/{len(data)}")
            self.end_headers()
            if self.path in state.interrupted:
                state.interrupted.remove(self.path)
                self.wfile.write(data[:len(data)//2])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if state.pause and self.path == "/weight":
                started, release = state.pause
                self.wfile.write(data[:vc_engine.CHUNK])
                self.wfile.flush()
                started.set()
                release.wait(5)
                data, offset = data[vc_engine.CHUNK:], 0
            try:
                self.wfile.write(data[offset:])
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        if state.pause:
            state.pause[1].set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def engine(tmp_path, monkeypatch, local_server):
    # Select the tar/macOS fixture without changing the host OS seen by dependencies.
    monkeypatch.setattr(vc_engine, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(vc_engine.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(vc_engine.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))
    original_check = vc_engine._check_url

    def check(url):
        # The only HTTP exception is this fixture's exact loopback server.
        if not url.startswith(local_server.url + "/"):
            original_check(url)

    monkeypatch.setattr(vc_engine, "_check_url", check)
    uv = archive([("uv-test/uv", b"fake uv")])
    source = archive([("repo/infer/a.py", b"ok"), ("repo/checkpoints/excluded", b"no")])
    weight = b"w" * (vc_engine.CHUNK * 4)
    local_server.data.update({"/uv": uv, "/source": source, "/weight": weight})

    def entry(id, content, **extra):
        return dict(id=id, url=local_server.url + "/" + id, size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(), **extra)

    manifest = {"version": 1, "python": "3.10", "requirements": "engine-requirements.lock",
                "wheels_estimate_bytes": 100, "uv": entry("uv", uv),
                "files": [entry("source", source, dest="spike/src/rvc", extract="tar.gz", strip=1, keep=["infer"]),
                          entry("weight", weight, dest="spike/hf/models--org--model/snapshots/abc/weight.bin")]}
    path = tmp_path / "engine-manifest.json"
    path.write_text(json.dumps(manifest))
    (tmp_path / "engine-requirements.lock").write_text("fake==1\n")
    instance = vc_engine.Installer(root=tmp_path / "vc-runtime", manifest=path)
    calls = []

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1] == "venv":
                python = instance.work / "venv/bin/python"
                python.parent.mkdir(parents=True, exist_ok=True)
                interpreter = instance.work / "python/bin/python3.10"
                interpreter.parent.mkdir(parents=True, exist_ok=True)
                interpreter.write_text("fake python")
                if sys.platform == 'darwin':
                    python.symlink_to(interpreter)
                else:
                    # Download/security tests do not exercise macOS uv relocation.
                    python.write_text('fake python', encoding='utf-8')
                (python.parent / "tool").write_text(f"#!{python}\nprint('tool')\n")
                (instance.work / "venv/pyvenv.cfg").write_text(f"home = {interpreter.parent}\n")

        def poll(self):
            return self.returncode

    monkeypatch.setattr(vc_engine.subprocess, "Popen", Process)
    instance.calls = calls
    monkeypatch.setattr(vc_engine, "_singleton", instance)
    return instance


def finish(engine):
    engine._thread.join(5)
    assert not engine._thread.is_alive()
    return engine.state()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_happy_path_and_live_runtime_check(engine):
    from ai_voice.vc_control import VcController
    controller = VcController.__new__(VcController)
    controller.worker = SimpleNamespace(_python_path=engine.root / "venv/bin/python")
    controller.store = SimpleNamespace(list=lambda: [])
    controller.trainer = SimpleNamespace(status=lambda: None)
    controller.last_done = None
    assert not controller.voices()["runtime_ok"]
    assert not engine.state()["installed"]
    engine.start()
    state = finish(engine)
    assert state["status"] == "idle" and state["installed"] and state["version"] == 1
    assert controller.voices()["runtime_ok"]
    assert (engine.root / "spike/src/rvc/infer/a.py").read_bytes() == b"ok"
    assert not (engine.root / "spike/src/rvc/checkpoints").exists()
    assert (engine.root / "spike/hf/models--org--model/refs/main").read_text() == "abc"
    assert not (engine.root / "downloads").exists()
    assert not (engine.root / "uv-cache").exists()
    assert len(engine.calls) == 3
    assert engine.calls[0][0][1:4] == ["venv", "--python", "3.10"]
    assert engine.calls[1][0][-1] == "--no-deps"
    for argv, kwargs in engine.calls:
        assert isinstance(argv, list) and "shell" not in kwargs
        assert kwargs["cwd"] == engine.work
        assert kwargs["env"]["UV_CACHE_DIR"] == str(engine.work / "uv-cache")
        assert kwargs["env"]["UV_PYTHON_INSTALL_DIR"] == str(engine.work / "python")
    assert str(engine.work) not in (engine.root / "venv/pyvenv.cfg").read_text()
    assert (engine.root / "venv/bin/tool").read_text().startswith(f"#!{engine.root}/venv/bin/python\n")
    assert engine.remove()["freed_bytes"] > 0
    assert not engine.root.exists()


def test_interrupted_resume(engine, local_server):
    local_server.interrupted.add("/weight")
    engine.start()
    assert finish(engine)["error"] == "network"
    part = engine.work / "downloads/weight.part"
    offset = part.stat().st_size
    assert 0 < offset < len(local_server.data["/weight"])
    engine.start()
    assert finish(engine)["installed"]
    assert ("/weight", f"bytes={offset}-") in local_server.requests


def test_range_200_restarts(engine, local_server):
    part = engine.work / "downloads/weight.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"wrong-prefix")
    local_server.range_ignored = True
    engine.start()
    assert finish(engine)["installed"]
    assert ("/weight", "bytes=12-") in local_server.requests


def test_oversized_part_restarts(engine, local_server):
    part = engine.work / "downloads/weight.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(local_server.data["/weight"] + b"extra")
    engine.start()
    assert finish(engine)["installed"]
    assert [request for request in local_server.requests if request[0] == "/weight"] == [("/weight", None)]
    assert (engine.root / engine.manifest["files"][1]["dest"]).read_bytes() == local_server.data["/weight"]


@pytest.mark.parametrize("rejections", [1, 2])
def test_range_416_restarts_once(engine, local_server, rejections):
    part = engine.work / "downloads/weight.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"wrong-prefix")
    local_server.range_rejections = rejections
    engine.start()
    state = finish(engine)
    assert [request for request in local_server.requests if request[0] == "/weight"] == [
        ("/weight", "bytes=12-"), ("/weight", None)]
    if rejections == 1:
        assert state["installed"]
        assert (engine.root / engine.manifest["files"][1]["dest"]).read_bytes() == local_server.data["/weight"]
    else:
        assert state["error"] == "network"
        assert not part.exists()
        assert not engine.calls


def test_checksum(engine):
    engine.manifest["files"][1]["sha256"] = "0" * 64
    engine.start()
    assert finish(engine)["error"] == "checksum"
    assert not (engine.work / "downloads/weight.part").exists()
    assert not engine.calls


@pytest.mark.parametrize("location", ["https://evil.invalid/file", "http://huggingface.co/file",
                                      "https://huggingface.co.evil.invalid/file", "https://evil@github.com/file"])
def test_redirect_rejected(engine, local_server, location):
    local_server.redirect["/weight"] = location
    engine.start()
    assert finish(engine)["error"] == "network"
    assert not engine.calls


def test_redirect_checked_every_hop(engine, local_server):
    local_server.redirect["/weight"] = local_server.url + "/next"
    local_server.redirect["/next"] = "https://evil.invalid/file"
    engine.start()
    assert finish(engine)["error"] == "network"
    assert ("/next", None) in local_server.requests


def test_redirect_limit(engine, local_server):
    local_server.redirect["/weight"] = local_server.url + "/weight"
    engine.start()
    assert finish(engine)["error"] == "network"
    assert sum(path == "/weight" for path, _ in local_server.requests) == 11


def test_disk(engine, monkeypatch, local_server):
    monkeypatch.setattr(engine, "_free_bytes", lambda: 0)
    engine.start()
    assert finish(engine)["error"] == "disk"
    assert not local_server.requests


def test_cancel_download_and_busy(engine, local_server):
    started, release = threading.Event(), threading.Event()
    local_server.pause = started, release
    engine.start()
    assert started.wait(5)
    with pytest.raises(RuntimeError, match="^busy$"):
        engine.start()
    with pytest.raises(RuntimeError, match="^busy$"):
        engine.remove()
    engine.cancel()
    release.set()
    assert finish(engine)["status"] == "cancelled"
    assert (engine.work / "downloads/weight.part").exists()
    assert not engine.calls
    local_server.pause = None
    engine.start()
    assert finish(engine)["installed"]


def test_cancel_uv(engine, monkeypatch):
    started = threading.Event()
    processes = []

    class Process:
        returncode = None

        def __init__(self, *args, **kwargs):
            processes.append(self)
            self.terminated = False
            started.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(vc_engine.subprocess, "Popen", Process)
    engine.start()
    assert started.wait(5)
    engine.cancel()
    assert finish(engine)["status"] == "cancelled"
    assert processes[0].terminated
    assert (engine.work / "downloads/weight.part").exists()


@pytest.mark.parametrize("failure", ["uv", "smoke"])
def test_failed_subprocess_preserves_old(engine, monkeypatch, failure):
    engine.root.mkdir()
    (engine.root / "old-data").write_text("original")
    original = vc_engine.subprocess.Popen

    def fail(argv, **kwargs):
        process = original(argv, **kwargs)
        if (failure == "smoke") == (argv[1] == "-c"):
            process.returncode = 1
        return process

    monkeypatch.setattr(vc_engine.subprocess, "Popen", fail)
    engine.start()
    assert finish(engine)["error"] == failure
    assert (engine.root / "old-data").read_text() == "original"


def test_smoke_timeout(engine, monkeypatch):
    original = vc_engine.subprocess.Popen
    clock = [0]

    def tick():
        clock[0] += 61
        return clock[0]

    class Hung:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(vc_engine.subprocess, "Popen", lambda argv, **kw: Hung() if argv[1] == "-c" else original(argv, **kw))
    monkeypatch.setattr(vc_engine.time, "monotonic", tick)
    engine.start()
    assert finish(engine)["error"] == "smoke"


def test_dev_symlink_untouched(engine, tmp_path):
    target = tmp_path / "owner-venv"
    (target / "bin").mkdir(parents=True)
    (target / "bin/python").write_text("original")
    engine.root.mkdir()
    (engine.root / "venv").symlink_to(target, target_is_directory=True)
    assert engine.state()["installed"]
    for action in (engine.start, engine.remove):
        with pytest.raises(RuntimeError, match="^dev_runtime$"):
            action()
    assert (target / "bin/python").read_text() == "original"


def test_dev_unset_env(engine, monkeypatch, tmp_path):
    monkeypatch.delenv("AI_VOICE_VC_RUNTIME", raising=False)
    python = tmp_path / "dev-python"
    python.write_text("dev")
    monkeypatch.setattr(vc_engine.paths, "VC_PYTHON", python)
    dev = vc_engine.Installer(manifest=engine.manifest_path)
    assert dev.state()["installed"]
    for action in (dev.start, dev.remove):
        with pytest.raises(RuntimeError, match="^dev_runtime$"):
            action()


def test_atomic_swap_rollback(engine, monkeypatch):
    engine.root.mkdir()
    (engine.root / "old-data").write_text("original")
    original = Path.rename

    def fail(path, target):
        if path == engine.work:
            raise OSError("swap failed")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", fail)
    engine.start()
    assert finish(engine)["error"] == "disk"
    assert (engine.root / "old-data").read_text() == "original"
    assert not engine.old.exists()


@pytest.mark.parametrize("name,content", [("../escape", b"evil"), ("/absolute", b"evil"),
                                         ("repo/infer/link", (tarfile.SYMTYPE, "../../escape")),
                                         ("repo/infer/link", (tarfile.LNKTYPE, "../../escape"))])
def test_tar_traversal_and_links(engine, local_server, name, content):
    data = archive([(name, content)])
    local_server.data["/source"] = data
    engine.manifest["files"][0].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    engine.start()
    assert finish(engine)["error"] == "disk"
    assert not (engine.root.parent / "escape").exists()
    assert not engine.calls


@pytest.mark.parametrize('name', [r'C:\escape', r'\absolute', r'repo\..\escape'])
def test_tar_windows_paths_rejected(engine, local_server, monkeypatch, name):
    monkeypatch.setattr(vc_engine, 'sys', SimpleNamespace(platform='win32'))
    data = archive([(name, b'evil')])
    local_server.data['/source'] = data
    engine.manifest['files'][0].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    engine.start()
    assert finish(engine)['error'] == 'disk'
    assert not engine.calls


def test_partial_symlink_refused(engine, tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    (target / "keep").write_text("safe")
    engine.work.symlink_to(target, target_is_directory=True)
    engine.start()
    assert finish(engine)["error"] == "disk"
    assert (target / "keep").read_text() == "safe"


@pytest.mark.parametrize("system,machine", [
    ("darwin", "x86_64"), ("linux", "x86_64"),
    ("linux", "arm64"), ("win32", "ARM64"),
])
def test_unsupported(engine, monkeypatch, system, machine):
    monkeypatch.setattr(vc_engine.sys, "platform", system)
    monkeypatch.setattr(vc_engine.platform, "machine", lambda: machine)
    # Task 14 selects the platform and manifest when the installer is created.
    engine = vc_engine.Installer(root=engine.root, manifest=engine.manifest_path)
    assert not engine.state()["supported"]
    with pytest.raises(RuntimeError, match="^unsupported$"):
        engine.start()


@pytest.mark.parametrize("url,allowed", [("https://github.com/x", True), ("https://cdn.hf.co/x", True),
                                         ("https://hf.co/x", False), ("https://evil.invalid/x", False),
                                         ("https://github.com:80/x", False), ("http://github.com/x", False)])
def test_production_allowlist(url, allowed):
    if allowed:
        vc_engine._check_url(url)
    else:
        with pytest.raises(RuntimeError, match="^network$"):
            vc_engine._check_url(url)


def test_endpoints_codes(engine):
    from ai_voice.desktop import DesktopHandler
    server = ThreadingHTTPServer(("127.0.0.1", 0), DesktopHandler)
    server.token = "t" * 20
    server.vc = SimpleNamespace(busy=False, trainer=SimpleNamespace(status=lambda: None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, path):
        with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as conn:
            conn.request(method, path, headers={"X-AI-Voice-Token": server.token})
            response = conn.getresponse()
            return response.status, json.loads(response.read())

    try:
        assert call("GET", "/api/vc/engine")[0] == 200
        server.vc.busy = True
        assert call("POST", "/api/vc/engine/remove") == (409, {"error": "busy"})
        server.vc.busy = False
        engine.dev = True
        assert call("POST", "/api/vc/engine/install") == (400, {"error": "dev_runtime"})
        assert call("POST", "/api/vc/engine/remove") == (400, {"error": "dev_runtime"})
        engine.dev = False
        engine._thread = SimpleNamespace(is_alive=lambda: True)
        assert call("POST", "/api/vc/engine/install") == (409, {"error": "busy"})
        engine._thread = None
        server.vc.trainer = SimpleNamespace(status=lambda: {"state": "paused"})
        assert call("POST", "/api/vc/engine/remove") == (409, {"error": "busy"})
        server.vc.trainer = SimpleNamespace(status=lambda: None)
        assert call("POST", "/api/vc/engine/cancel")[0] == 200
        assert call("POST", "/api/vc/engine/install")[0] == 200
        assert finish(engine)["installed"]
        assert call("POST", "/api/vc/engine/remove")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_allowlisted_relative_redirect(engine, local_server):
    local_server.data["/final"] = local_server.data["/weight"]
    local_server.redirect["/weight"] = "/final"
    engine.start()
    assert finish(engine)["installed"]
    assert ("/final", None) in local_server.requests


def test_successful_swap_replaces_old(engine):
    engine.root.mkdir()
    (engine.root / "old-data").write_text("old")
    engine.start()
    assert finish(engine)["installed"]
    assert not engine.old.exists()
    assert not (engine.root / "old-data").exists()


def test_remove_root_symlink_untouched(engine, tmp_path):
    target = tmp_path / "owner-runtime"
    (target / "venv/bin").mkdir(parents=True)
    (target / "venv/bin/python").write_text("original")
    engine.root.symlink_to(target, target_is_directory=True)
    for action in (engine.start, engine.remove):
        with pytest.raises(RuntimeError, match="^dev_runtime$"):
            action()
    assert (target / "venv/bin/python").read_text() == "original"


def test_old_symlink_not_deleted(engine, tmp_path):
    target = tmp_path / "owner-old"
    target.mkdir()
    (target / "keep").write_text("safe")
    engine.old.symlink_to(target, target_is_directory=True)
    engine.start()
    assert finish(engine)["error"] == "disk"
    assert (target / "keep").read_text() == "safe"


def test_cancel_before_final_cleanup_keeps_parts(engine, monkeypatch):
    original = engine._subprocess

    def cancel_after_smoke(argv, env, code, timeout=None):
        original(argv, env, code, timeout)
        if code == "smoke":
            engine.cancel()

    monkeypatch.setattr(engine, "_subprocess", cancel_after_smoke)
    engine.start()
    assert finish(engine)["status"] == "cancelled"
    assert (engine.work / "downloads/weight.part").exists()
    assert not engine.root.exists()
