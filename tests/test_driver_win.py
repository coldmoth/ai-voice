"""Offline installer tests with byte downloads and native API doubles."""
import ctypes
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from ai_voice import desktop, devices, driver


@pytest.fixture(autouse=True)
def no_real_installer(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Installer network and elevation must be mocked")
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)


FILES = {
    "VBCABLE_Setup_x64.exe": b"installer",
    "VBCABLE_ControlPanel.exe": b"panel",
    "vbMmeCable64_win10.inf": b"inf",
    "vbaudio_cable64_win10.sys": b"sys",
    "vbaudio_cable64_win10.cat": b"cat",
    "pin_in.ico": b"input icon",
    "pin_out.ico": b"output icon",
}


def archive(extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, data in {**FILES, **(extra or {})}.items():
            bundle.writestr(name, data)
    return stream.getvalue()


@pytest.fixture
def cable(monkeypatch):
    payload = archive({"VBCABLE_Setup.exe": b"x86", "nested/unneeded.exe": b"ignored"})
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(driver, "VBCABLE_SHA256", hashlib.sha256(payload).hexdigest(), raising=False)
    events = []
    monkeypatch.setattr(devices.sd, "_terminate", lambda: events.append("terminate"))
    monkeypatch.setattr(devices.sd, "_initialize", lambda: events.append("initialize"))
    monkeypatch.setattr(devices.sd, "query_devices", lambda: [
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "max_output_channels": 2, "hostapi": 0}])
    monkeypatch.setattr(devices.sd, "query_hostapis", lambda: [{"name": "Windows WASAPI"}])

    def download(url):
        assert url == "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
        events.append("download")
        return payload

    return download, events


def test_install_cable_hash_mismatch_never_runs(monkeypatch):
    monkeypatch.setattr(zipfile, "ZipFile", lambda *_: pytest.fail("unverified ZIP opened"))
    result = driver.install_cable(download=lambda _: b"bad bytes",
                                  run_elevated=lambda _: pytest.fail("unverified installer executed"))
    assert result == {"status": "hash_mismatch", "device": None}


@pytest.mark.parametrize("status", [200, 302, 503])
def test_default_download_https_timeout_and_no_redirects(monkeypatch, status):
    requests = []
    def serve(request):
        requests.append(request)
        return httpx.Response(status, content=b"downloaded", headers={"Location": "http://unsafe.test/file"})
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        def get(url, **kwargs):
            assert kwargs["timeout"] == 30
            assert kwargs["follow_redirects"] is False
            return client.get(url, **kwargs)
        monkeypatch.setattr(httpx, "get", get)
        if status == 200:
            assert driver._download_cable(driver.VBCABLE_URL) == b"downloaded"
        else:
            assert driver.install_cable(run_elevated=lambda _: pytest.fail("HTTP error executed")) == {
                "status": "network", "device": None}
    assert len(requests) == 1
    assert str(requests[0].url) == "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"


def test_default_download_rejects_http(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: pytest.fail("insecure request sent"))
    with pytest.raises(ValueError):
        driver._download_cable("http://unsafe.test/file")


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("timeout")])
def test_install_cable_network_error(error):
    def download(_):
        raise error
    assert driver.install_cable(download=download, run_elevated=lambda _: pytest.fail("executed")) == {
        "status": "network", "device": None}


def test_install_cable_cancelled(cable):
    download, events = cable
    directories = []
    def cancel(executable):
        directories.append(executable.parent)
        return 1223
    assert driver.install_cable(download=download, run_elevated=cancel) == {
        "status": "cancelled", "device": None}
    assert events == ["download"]
    assert directories and not directories[0].exists()


def test_install_cable_ok(cable):
    download, events = cable
    directories = []
    def run(executable):
        assert executable.name == "VBCABLE_Setup_x64.exe"
        assert {p.relative_to(executable.parent).as_posix(): p.read_bytes()
                for p in executable.parent.rglob("*") if p.is_file()} == {
            **FILES, "VBCABLE_Setup.exe": b"x86", "nested/unneeded.exe": b"ignored"}
        directories.append(executable.parent)
        events.append("run")
        return 0
    assert driver.install_cable(download=download, run_elevated=run) == {
        "status": "installed", "device": "CABLE Input (VB-Audio Virtual Cable)"}
    assert events == ["download", "run", "terminate", "initialize"]
    assert not directories[0].exists()


def test_install_cable_not_found_after_install_asks_reboot(cable, monkeypatch):
    download, _ = cable
    monkeypatch.setattr(devices.sd, "query_devices", lambda: [])
    assert driver.install_cable(download=download, run_elevated=lambda _: 0) == {
        "status": "reboot", "device": None}


def test_install_cable_removes_temp_dir_on_elevation_failure(cable):
    download, events = cable
    directories = []
    def run(executable):
        directories.append(executable.parent)
        raise OSError("elevation failed")
    with pytest.raises(OSError):
        driver.install_cable(download=download, run_elevated=run)
    assert not directories[0].exists()
    assert events == ["download"]


@pytest.mark.parametrize("name", ["../escape.exe", "/absolute.exe", "C:/escape.exe",
                                 "C:escape.exe", "..\\escape.exe", "\\\\server\\share\\escape.exe",
                                 "nested/../../escape.exe", "file.exe:stream"])
def test_install_cable_rejects_unsafe_members_before_extraction(monkeypatch, name):
    payload = archive({name: b"unsafe"})
    monkeypatch.setattr(driver, "VBCABLE_SHA256", hashlib.sha256(payload).hexdigest(), raising=False)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", lambda *_: pytest.fail("unsafe ZIP extracted"))
    assert driver.install_cable(download=lambda _: payload,
                                run_elevated=lambda _: pytest.fail("unsafe installer executed")) == {
        "status": "hash_mismatch", "device": None}


@pytest.mark.parametrize("payload", [b"not a zip", archive()], ids=["invalid-zip", "bad-crc"])
def test_install_cable_bad_zip_returns_hash_mismatch(monkeypatch, payload):
    # A pinned payload with a damaged member must fail before elevation too.
    if payload.startswith(b"PK"):
        payload = payload.replace(b"installer", b"corrupted", 1)
    monkeypatch.setattr(driver, "VBCABLE_SHA256", hashlib.sha256(payload).hexdigest())
    assert driver.install_cable(download=lambda _: payload,
                                run_elevated=lambda _: pytest.fail("bad ZIP executed")) == {
        "status": "hash_mismatch", "device": None}


def test_install_cable_missing_installer_returns_hash_mismatch(monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("readme.txt", b"no installer")
    payload = stream.getvalue()
    monkeypatch.setattr(driver, "VBCABLE_SHA256", hashlib.sha256(payload).hexdigest())
    assert driver.install_cable(download=lambda _: payload,
                                run_elevated=lambda _: pytest.fail("missing installer executed")) == {
        "status": "hash_mismatch", "device": None}


def test_install_cable_uses_supplied_rescan(cable):
    download, events = cable
    result = driver.install_cable(download=download, run_elevated=lambda _: 0,
                                  rescan=lambda: events.append("guarded rescan"))
    assert result["status"] == "installed"
    assert events == ["download", "guarded rescan"]


def native_apis(monkeypatch, *, cancelled=False, wait=0):
    calls = []
    def execute(pointer):
        info = pointer._obj
        assert info.cbSize == ctypes.sizeof(info)
        assert info.fMask & 0x40
        assert info.lpVerb == "runas"
        assert info.lpFile == str(Path("installer.exe").resolve())
        assert info.lpDirectory == str(Path.cwd())
        if cancelled:
            return False
        info.hProcess = 0x123456789  # Must survive x64 handle marshalling.
        calls.append("execute")
        return True
    def wait_for(handle, timeout):
        calls.append(("wait", handle, timeout))
        return wait
    def close(handle):
        calls.append(("close", handle))
        return True
    def function(fn):
        def wrapper(*args):
            return fn(*args)
        return wrapper
    shell = SimpleNamespace(ShellExecuteExW=function(execute))
    kernel = SimpleNamespace(WaitForSingleObject=function(wait_for), CloseHandle=function(close))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: shell if name == "shell32" else kernel,
                        raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1223 if cancelled else 5, raising=False)
    return calls


def test_elevated_installer_waits_and_closes_handle(monkeypatch):
    calls = native_apis(monkeypatch)
    assert driver._run_cable_elevated(Path("installer.exe")) == 0
    assert calls == ["execute", ("wait", 0x123456789, 0xFFFFFFFF), ("close", 0x123456789)]


def test_elevated_installer_cancelled(monkeypatch):
    calls = native_apis(monkeypatch, cancelled=True)
    assert driver._run_cable_elevated(Path("installer.exe")) == 1223
    assert calls == []


def test_elevated_installer_closes_handle_on_wait_failure(monkeypatch):
    calls = native_apis(monkeypatch, wait=0xFFFFFFFF)
    with pytest.raises(OSError):
        driver._run_cable_elevated(Path("installer.exe"))
    assert calls[-1] == ("close", 0x123456789)


@pytest.mark.parametrize("status", ["installed", "reboot", "cancelled", "hash_mismatch", "network"])
def test_windows_install_route_returns_status(monkeypatch, status):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(driver, "_job", {"state": "idle", "action": None})
    monkeypatch.setattr(desktop, "_audio_idle", lambda _: True)
    result = {"status": status, "device": "CABLE Input" if status == "installed" else None}
    monkeypatch.setattr(driver, "install_cable", lambda **_: result)
    monkeypatch.setattr(driver, "bundled", lambda: pytest.fail("macOS bundle check"))
    handler = object.__new__(desktop.DesktopHandler)
    handler.server = SimpleNamespace(server_port=1234, token="test-token")
    handler.path = "/api/driver/install"
    handler.headers = {"Host": "127.0.0.1:1234", "X-AI-Voice-Token": "test-token",
                       "Content-Length": "2", "Content-Type": "application/json"}
    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
    codes = []
    handler.send_response = codes.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.do_POST()
    assert codes == [200]
    assert json.loads(handler.wfile.getvalue()) == result


@pytest.mark.parametrize("running,audio_idle", [(True, True), (False, False)])
def test_windows_install_route_rejects_busy(monkeypatch, running, audio_idle):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(driver, "_job", {"state": "running" if running else "idle", "action": None})
    monkeypatch.setattr(desktop, "_audio_idle", lambda _: audio_idle)
    monkeypatch.setattr(driver, "install_cable", lambda **_: pytest.fail("busy installation started"))
    handler = object.__new__(desktop.DesktopHandler)
    handler.server = SimpleNamespace(server_port=1234, token="test-token")
    handler.path = "/api/driver/install"
    handler.headers = {"Host": "127.0.0.1:1234", "X-AI-Voice-Token": "test-token",
                       "Content-Length": "2", "Content-Type": "application/json"}
    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
    codes = []
    handler.send_response = codes.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.do_POST()
    assert codes == [409]
    assert "error" in json.loads(handler.wfile.getvalue())


@pytest.mark.parametrize("busy_after_install", [False, True])
def test_windows_install_route_rechecks_idle_before_rescan(cable, monkeypatch, busy_after_install):
    download, events = cable
    monkeypatch.setattr(driver, "_job", {"state": "idle", "action": None})
    state = {"active": False}
    installer = driver.install_cable

    def run(executable):
        events.append("run")
        state["active"] = busy_after_install
        return 0

    def install(**kwargs):
        return installer(download=download, run_elevated=run, **kwargs)

    monkeypatch.setattr(driver, "install_cable", install)
    handler = object.__new__(desktop.DesktopHandler)
    handler.server = SimpleNamespace(
        server_port=1234, token="test-token",
        control=SimpleNamespace(snapshot=lambda: dict(state)),
        preview=SimpleNamespace(snapshot=lambda: {"preview_state": "idle"}))
    handler.path = "/api/driver/install"
    handler.headers = {"Host": "127.0.0.1:1234", "X-AI-Voice-Token": "test-token",
                       "Content-Length": "2", "Content-Type": "application/json"}
    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
    codes = []
    handler.send_response = codes.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.do_POST()
    assert codes == [200]
    assert json.loads(handler.wfile.getvalue())["status"] == "installed"
    assert events == ["download", "run"] + ([] if busy_after_install else ["terminate", "initialize"])
    assert driver.job() == {"state": "done", "action": "install"}


def test_macos_install_route_keeps_async_job(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(driver, "_job", {"state": "idle", "action": None})
    monkeypatch.setattr(driver, "bundled", lambda: Path("AIVoiceMic.driver"))
    monkeypatch.setattr(driver, "install_cable", lambda: pytest.fail("Windows installer called on macOS"))
    actions = []
    monkeypatch.setattr(driver, "start", lambda action, **_: actions.append(action) or True)
    handler = object.__new__(desktop.DesktopHandler)
    handler.server = SimpleNamespace(server_port=1234, token="test-token")
    handler.path = "/api/driver/install"
    handler.headers = {"Host": "127.0.0.1:1234", "X-AI-Voice-Token": "test-token",
                       "Content-Length": "2", "Content-Type": "application/json"}
    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
    codes = []
    handler.send_response = codes.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.do_POST()
    assert codes == [200]
    assert json.loads(handler.wfile.getvalue()) == {"ok": True}
    assert actions == ["install"]
