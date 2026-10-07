"""Offline shell tests: real child processes, no audio or native window."""
import importlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def backend(tmp_path):
    package = tmp_path / "ai_voice"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "desktop.py").write_text('''
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        valid = self.path == "/health" and self.headers.get("X-AI-Voice-Token") == "test-token"
        self.send_response(200 if valid else 403)
        self.end_headers()
        self.wfile.write(b'{"ok": true}' if valid else b'{}')

assert sys.argv[1:] == ["--parent-pid", str(os.getppid())]
assert os.environ["PYTHONUTF8"] == "1"
server = HTTPServer(("127.0.0.1", 0), Handler)
print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/?token=test-token"}), flush=True)
server.serve_forever()
''', encoding="utf-8")
    return {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONUTF8": "1"}


@pytest.fixture
def shell():
    return importlib.import_module("ai_voice.winshell")


@pytest.fixture
def launched(shell, backend, monkeypatch):
    processes = []
    start = shell.start_backend

    def launch(python, env):
        assert env["PYTHONUTF8"] == "1"
        process, url = start(python, {**env, "PYTHONPATH": backend["PYTHONPATH"]})
        processes.append(process)
        return process, url

    monkeypatch.setattr(shell, "start_backend", launch)
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def test_start_backend_reads_url(shell, backend):
    process, url = shell.start_backend(Path(sys.executable), backend)
    try:
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/?token=test-token")
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize("override", [None, "", "custom-runtime"])
def test_start_backend_sets_runtime_environment(shell, tmp_path, monkeypatch, override):
    from ai_voice import paths
    runtime = tmp_path / "vc-runtime"
    monkeypatch.setattr(paths, "vc_runtime_home", lambda: runtime)
    environment = {} if override is None else {"AI_VOICE_VC_RUNTIME": override}
    original = environment.copy()
    captured = {}
    process = SimpleNamespace(stdout=io.StringIO('{"url":"http://localhost/"}\n'))

    def popen(argv, **kwargs):
        captured.update(kwargs["env"])
        return process

    monkeypatch.setattr(shell.subprocess, "Popen", popen)
    assert shell.start_backend(Path(sys.executable), environment)[0] is process
    assert captured["AI_VOICE_VC_RUNTIME"] == (override or str(runtime))
    assert captured["AI_VOICE_RESOURCES"] == str(paths.ROOT)
    assert captured["AI_VOICE_WEB"] == str(paths.WEB)
    assert captured["AI_VOICE_HOME"] == str(paths.app_home())
    assert captured["PYTHONDONTWRITEBYTECODE"] == "1"
    assert captured["PYTHONNOUSERSITE"] == "1"
    assert captured["PYTHONUTF8"] == "1"
    assert environment == original


def test_smoke_flag_exits_zero(shell, launched, capsys, monkeypatch):
    # A smoke run must never need the Windows-only dependency.
    monkeypatch.setitem(sys.modules, "webview", None)
    assert shell.main(["--smoke"]) == 0
    assert capsys.readouterr().out == "ok\n"
    assert len(launched) == 1
    assert launched[0].poll() is not None


@pytest.mark.parametrize("fail", [False, True])
def test_backend_killed_on_exit(shell, launched, monkeypatch, fail):
    window = SimpleNamespace()

    def create(title, url, **options):
        assert title == "AI Voice"
        assert url.endswith("/?token=test-token")
        assert options["width"] == 1100 and options["height"] == 720
        assert options["frameless"] is False and options["easy_drag"] is False
        assert isinstance(options["js_api"], shell.Bridge)
        return window

    def start(**options):
        assert options == {"gui": "edgechromium"}
        if fail:
            raise RuntimeError("window failed")

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create, start=start))
    assert shell.main([]) == (1 if fail else 0)
    assert len(launched) == 1
    assert launched[0].poll() is not None


def test_notify_escapes_javascript(shell):
    scripts = []
    bridge = shell.Bridge()
    bridge._window = SimpleNamespace(evaluate_js=scripts.append)
    message = 'quote " newline\n</script>'
    bridge.notify({"title": "AI Voice", "body": message})
    assert len(scripts) == 1
    assert scripts[0].startswith("showToast(")
    assert json.loads(scripts[0][len("showToast("):-1]) == message
    assert bridge.language({"language": "en"}) is None
    assert bridge.drag({}) is None
    assert bridge.hotkey({}) is None  # Task 8 connects the global hotkey.


def request(path, headers=None, server=None):
    from ai_voice.desktop import DesktopHandler
    handler = object.__new__(DesktopHandler)
    handler.server = server or SimpleNamespace(server_port=1234, token="test-token")
    handler.server.server_port, handler.server.token = 1234, "test-token"
    handler.path = path
    handler.headers = {"Host": "127.0.0.1:1234", **(headers or {})}
    handler.wfile = io.BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.do_GET()
    return statuses[0], json.loads(handler.wfile.getvalue())


@pytest.mark.parametrize("path,headers,expected", [
    ("/health", {"X-AI-Voice-Token": "test-token"}, 200),
    ("/health", {}, 403),
    ("/health", {"X-AI-Voice-Token": "wrong"}, 403),
    ("/health?token=test-token", {}, 403),
])
def test_health_auth(path, headers, expected):
    status, body = request(path, headers)
    assert status == expected
    if expected == 200:
        assert body == {"ok": True}
    else:
        assert "error" in body


@pytest.mark.parametrize("platform,expected", [("win32", "win"), ("darwin", "mac")])
def test_initial_state_platform(monkeypatch, platform, expected):
    from ai_voice import desktop
    catalog = SimpleNamespace(start_metadata=lambda: None, preferences=lambda: {}, voices=lambda: [])
    monkeypatch.setattr(desktop, "list_devices", lambda: [])
    monkeypatch.setattr(desktop.asr, "locales_snapshot", lambda: {"system": "en-US", "supported": ["en-US"]})
    monkeypatch.setattr(desktop.sys, "platform", platform)
    status, body = request("/api/voices", {"X-AI-Voice-Token": "test-token"}, SimpleNamespace(catalog=catalog))
    assert status == 200
    assert body["platform"] == expected
