"""Offline virtual microphone status, quoting and job tests."""
from pathlib import Path
import plistlib
import shlex
import subprocess
import threading
from types import SimpleNamespace

import pytest

from ai_voice import devices, driver


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(driver.paths, "ROOT", tmp_path)
    monkeypatch.setattr(driver, "HAL", tmp_path / "HAL")
    monkeypatch.setattr(driver, "_job", {"state": "idle", "action": None})


def bundle(path, version="0.7.1"):
    contents = path / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    return path


@pytest.mark.parametrize("available,installed,present", [
    (False, False, False), (True, False, False), (True, True, True),
    (True, True, False), (False, True, True),
])
def test_status(available, installed, present):
    if available:
        bundle(driver.paths.ROOT / "driver" / driver.NAME)
    if installed:
        bundle(driver.HAL / driver.NAME, "0.7.0")
    assert driver.status([driver.DEVICE] if present else []) == {
        "available": available, "installed": installed, "device_present": present,
        "bundled_version": "0.7.1" if available else None,
        "installed_version": "0.7.0" if installed else None,
        "job": {"state": "idle", "action": None},
    }


def test_bundled_fallback_and_priority():
    fallback = bundle(driver.paths.ROOT / "build" / "driver" / driver.NAME)
    assert driver.bundled() == fallback
    primary = bundle(driver.paths.ROOT / "driver" / driver.NAME)
    assert driver.bundled() == primary


@pytest.mark.parametrize("payload", [b"bad plist", b"<?xml version='1.0'?><plist><bad", plistlib.dumps([])])
def test_unreadable_versions(payload):
    for path in (driver.paths.ROOT / "driver" / driver.NAME, driver.HAL / driver.NAME):
        bundle(path)
        (path / "Contents" / "Info.plist").write_bytes(payload)
    state = driver.status([])
    assert state["bundled_version"] is None and state["installed_version"] is None


def test_missing_versions():
    (driver.paths.ROOT / "driver" / driver.NAME).mkdir(parents=True)
    assert driver.status([])["bundled_version"] is None


def test_script_and_applescript_quoting(monkeypatch):
    source = Path('/tmp/a b"c/AIVoiceMic.driver')
    monkeypatch.setattr(driver, "HAL", Path('/tmp/hal a"b\\c'))
    line = driver.script("install", source)
    captured = []
    monkeypatch.setattr(driver.subprocess, "run", lambda args, **kw: captured.append((args, kw)) or
                        SimpleNamespace(returncode=0, stderr=""))
    assert driver._run(line) == "ok"
    args, options = captured[0]
    assert args[:2] == ["/usr/bin/osascript", "-e"]
    assert options == {"capture_output": True, "text": True, "timeout": 300}
    escaped = args[2].removeprefix('do shell script "').removesuffix('" with administrator privileges')
    # Undo one AppleScript escape layer, including backslashes inside quoted paths.
    import re
    unescaped = re.sub(r'\\([\\"])', r'\1', escaped)
    assert unescaped == line
    tokens = shlex.split(unescaped, posix=True)
    assert tokens.count(str(source)) == 1
    commands = shlex.shlex(unescaped, posix=True, punctuation_chars=";&")
    commands.whitespace_split = True
    tokens = list(commands)
    assert tokens.count(";") == 1
    separator = tokens.index(";")
    assert tokens[separator - 4:separator] == ["xattr", "-dr", "com.apple.quarantine", str(driver.HAL / (driver.NAME + ".tmp"))]
    assert tokens[separator + 1:separator + 4] == ["rm", "-rf", str(driver.HAL / driver.NAME)]
    assert shlex.split(driver.script("uninstall", None)) == [
        "rm", "-rf", str(driver.HAL / driver.NAME), "&&", "killall", "coreaudiod"]


@pytest.mark.parametrize("code,stderr,expected", [(0, "", "ok"), (1, "cancel (-128)", "cancelled"),
                                                 (1, "secret error" * 50, "failed")])
def test_runner_outcomes(monkeypatch, caplog, code, stderr, expected):
    monkeypatch.setattr(driver.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=code, stderr=stderr))
    assert driver._run("test") == expected
    if expected == "failed":
        assert caplog.records[-1].args == (stderr[:300],)
    else:
        assert not caplog.records


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("test", 300, stderr=b"timed out"), OSError("no runner")])
def test_runner_exceptions(monkeypatch, error, caplog):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(driver.subprocess, "run", run)
    assert driver._run("test") == "failed"
    assert caplog.records


@pytest.mark.parametrize("outcome,expected", [("ok", "done"), ("cancelled", "cancelled"), ("failed", "failed")])
def test_job_order_and_reset(monkeypatch, outcome, expected):
    calls = []
    threads = []
    real_thread = threading.Thread
    def spawn(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(driver.threading, "Thread", spawn)
    monkeypatch.setattr(driver, "_run", lambda line: calls.append("run") or outcome)
    assert driver.start("uninstall", before=lambda: calls.append("before"), after=lambda: calls.append("after"))
    threads[-1].join(2)
    assert not threads[-1].is_alive() and threads[-1].daemon
    assert calls == ["before", "run"] + (["after"] if outcome == "ok" else [])
    assert driver.job() == {"state": expected, "action": "uninstall"}
    copied = driver.job()
    copied["state"] = "running"
    assert driver.job()["state"] == expected
    assert driver.start("uninstall", before=lambda: None, after=lambda: None)
    threads[-1].join(2)
    assert driver.job()["state"] == expected


def test_busy_job(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def run(line):
        entered.set()
        assert release.wait(2)
        return "ok"
    threads = []
    real_thread = threading.Thread
    def spawn(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(driver.threading, "Thread", spawn)
    monkeypatch.setattr(driver, "_run", run)
    try:
        assert driver.start("uninstall", before=lambda: None, after=lambda: None)
        assert entered.wait(2)
        assert driver.job() == {"state": "running", "action": "uninstall"}
        assert driver.start("uninstall", before=lambda: pytest.fail("second before"), after=lambda: None) is False
    finally:
        release.set()
        for thread in threads:
            thread.join(2)


def test_before_failure_still_runs(monkeypatch):
    calls = []
    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(driver.threading, "Thread", ImmediateThread)
    def before():
        calls.append("before")
        raise RuntimeError("stop failed")
    monkeypatch.setattr(driver, "_run", lambda line: calls.append("run") or "ok")
    assert driver.start("uninstall", before=before, after=lambda: calls.append("after"))
    assert calls == ["before", "run", "after"]
    assert driver.job()["state"] == "done"


def test_rescan(monkeypatch):
    calls = []
    monkeypatch.setattr(devices.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(devices.sd, "_initialize", lambda: calls.append("initialize"))
    devices.rescan()
    assert calls == ["terminate", "initialize"]
