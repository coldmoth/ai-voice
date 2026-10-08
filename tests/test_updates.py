"""Offline release checks, version comparison and cache semantics."""
from copy import deepcopy
from pathlib import Path
import re

import httpx
import pytest

from ai_voice import __version__, updates

NOW = 200_000
URL = updates.RELEASE_URL_PREFIX + "tag/v9.0.0"
RELEASE = {"tag_name": "v9.0.0", "html_url": URL, "draft": False, "prerelease": False}


@pytest.fixture
def state():
    return {"last_check": 0, "latest": None, "skipped": None}


def mock_client(response):
    seen = []

    def handle(request):
        seen.append(request)
        if isinstance(response, BaseException):
            raise response
        return response

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


@pytest.mark.parametrize("value,expected", [
    ("0.4.0", (0, 4, 0)), ("v1.2.3", (1, 2, 3)), ("01.2.3", (1, 2, 3)),
    ("1.2.0-rc1", None), ("1.2", None), ("1.2.3.4", None), ("garbage", None),
    (None, None), (123, None), (" 1.2.3", None), ("1.2.3\n", None), ("V1.2.3", None),
    ("1..3", None), ("-1.2.3", None),
])
def test_parse_version(value, expected):
    assert updates.parse_version(value) == expected


@pytest.mark.parametrize("remote,local,expected", [
    ("0.10.0", "0.4.0", True), ("v1.2.3", "1.2.2", True), ("1.2.3", "1.2.3", False),
    ("0.4.0", "0.10.0", False), ("bad", "1.2.3", False), ("1.2.3", "bad", False),
])
def test_is_newer(remote, local, expected):
    assert updates.is_newer(remote, local) is expected


def test_daily_cache_and_clock_rollback(state):
    original = deepcopy(state)
    client, seen = mock_client(httpx.Response(200, json=RELEASE))
    with client:
        result, cached = updates.check(state, True, now=NOW, client=client)
        assert result == {"status": "available", "version": "9.0.0", "url": URL}
        assert state == original and cached["last_check"] == NOW
        assert len(seen) == 1
        result_again, unchanged = updates.check(cached, True, now=NOW + updates.INTERVAL - 1, client=client)
        assert result_again == result and unchanged == cached and len(seen) == 1
        _, refreshed = updates.check(cached, True, now=NOW + updates.INTERVAL, client=client)
        assert refreshed["last_check"] == NOW + updates.INTERVAL and len(seen) == 2
        _, rolled_back = updates.check(refreshed, True, now=NOW, client=client)
        assert rolled_back["last_check"] == NOW and len(seen) == 3


def test_disabled_and_force(state):
    client, seen = mock_client(httpx.Response(200, json=RELEASE))
    with client:
        assert updates.check(state, False, now=NOW, client=client) == ({"status": "disabled"}, state)
        assert seen == []
        result, cached = updates.check(state, False, now=NOW, force=True, client=client)
        assert result["status"] == "available" and len(seen) == 1
        updates.check(cached, False, now=NOW + 1, force=True, client=client)
        assert len(seen) == 2


def test_skip_and_newer_release(state):
    state["skipped"] = "9.0.0"
    client, seen = mock_client(httpx.Response(200, json=RELEASE))
    with client:
        result, cached = updates.check(state, True, now=NOW, client=client)
        assert result == {"status": "current", "version": __version__}
        assert updates.check(cached, True, now=NOW + 1, client=client)[0] == result
        assert len(seen) == 1
        assert updates.check(cached, True, now=NOW + 1, force=True, client=client)[0]["status"] == "available"
    client, _ = mock_client(httpx.Response(200, json={**RELEASE, "tag_name": "v10.0.0"}))
    with client:
        result, _ = updates.check(cached, True, now=NOW + updates.INTERVAL, client=client)
        assert result["status"] == "available" and result["version"] == "10.0.0"


@pytest.mark.parametrize("response", [
    httpx.Response(404), httpx.Response(200, json={**RELEASE, "tag_name": __version__}),
    httpx.Response(200, json={**RELEASE, "tag_name": "0.0.0"}),
])
def test_current_saves_completed_check(state, response):
    state["latest"] = {"version": "9.0.0", "url": URL}
    client, seen = mock_client(response)
    with client:
        result, saved = updates.check(state, True, now=NOW, client=client)
        cached_result, cached_state = updates.check(saved, True, now=NOW + 1, client=client)
    assert result == {"status": "current", "version": __version__}
    assert cached_result == result and cached_state == saved and len(seen) == 1
    assert saved["last_check"] == NOW
    if response.status_code == 404:
        assert saved["latest"] is None
    else:
        assert saved["latest"] is not None


@pytest.mark.parametrize("response,error", [
    (httpx.ConnectTimeout("slow"), "network"), (OSError("offline"), "network"),
    (TimeoutError("slow"), "network"), (httpx.Response(500), "github"),
    (httpx.Response(302, headers={"Location": URL}), "github"),
    (httpx.Response(200, content=b"not JSON"), "github"),
    (httpx.Response(200, json=[]), "github"),
    (httpx.Response(200, json={"html_url": URL}), "github"),
    (httpx.Response(200, json={**RELEASE, "tag_name": "1.2.0-rc1"}), "github"),
    (httpx.Response(200, json={**RELEASE, "prerelease": True}), "github"),
    (httpx.Response(200, json={**RELEASE, "draft": True}), "github"),
    (httpx.Response(200, json={**RELEASE, "html_url": "https://example.com/releases/1"}), "github"),
    (httpx.Response(200, json={**RELEASE, "html_url": "https://github.com/other/repo/releases/1"}), "github"),
    (httpx.Response(200, json={**RELEASE, "html_url": None}), "github"),
])
def test_errors_preserve_cache(state, response, error):
    state.update(last_check=100, latest={"version": "9.0.0", "url": URL}, skipped="8.0.0")
    before = deepcopy(state)
    client, seen = mock_client(response)
    with client:
        result, returned = updates.check(state, True, now=NOW, client=client)
    assert result == {"status": "error", "error": error}
    assert state == returned == before and len(seen) == 1


def test_request_contract_and_ignored_body(state):
    client, seen = mock_client(httpx.Response(200, json={**RELEASE, "body": "ignored", "assets": []}))
    with client:
        result, saved = updates.check(state, True, now=NOW, client=client)
    request = seen[0]
    assert request.method == "GET" and str(request.url) == updates.API
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["User-Agent"] == f"AI-Voice/{__version__}"
    assert "Authorization" not in request.headers
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 8)
    assert saved["latest"] == {"version": "9.0.0", "url": URL}
    assert result == {"status": "available", **saved["latest"]}


def test_owned_client(state, monkeypatch):
    real_client = httpx.Client
    seen_options = []

    def make_client(**kwargs):
        seen_options.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(404)), **kwargs)

    monkeypatch.setattr(updates.httpx, "Client", make_client)
    assert updates.check(state, True, now=NOW)[0]["status"] == "current"
    assert seen_options == [{"timeout": 8}]


def test_external_hosts():
    source = (Path(__file__).resolve().parents[1] / "macos/Desktop.swift").read_text()
    hosts = re.search(r"externalHosts: Set<String> = \[(.*?)\]", source, re.S)[1]
    assert '"github.com"' in hosts and '"api.github.com"' not in hosts


# ---- In-app install ----
import hashlib
import subprocess
import sys

BASE = updates.RELEASE_URL_PREFIX + "download/v9.0.0/"
CDN = "https://release-assets.githubusercontent.com/blob/1"
PAYLOAD = b"installer-bytes" * 100
GOOD = hashlib.sha256(PAYLOAD).hexdigest()


def asset_client(*, checksum=f"{GOOD}  AI-Voice-Setup-9.0.0.exe\n", payload=PAYLOAD, redirect=CDN, seen=None):
    def handle(request):
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url.startswith(BASE):
            return httpx.Response(302, headers={"location": redirect + "/" + url.rsplit("/", 1)[1]})
        if url.endswith(".sha256"):
            return httpx.Response(404) if checksum is None else httpx.Response(200, content=checksum.encode())
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setattr(updates.paths, "app_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    launched = []
    monkeypatch.setattr(updates, "_install_windows", lambda path: launched.append(path))
    monkeypatch.setattr(updates, "_job", {"state": "downloading", "percent": 0, "error": None})
    return tmp_path / "updates", launched


def test_asset_name_by_platform():
    assert updates.asset_name("1.2.3", "win32") == "AI-Voice-Setup-1.2.3.exe"
    assert updates.asset_name("1.2.3", "darwin") == "AI-Voice-1.2.3.zip"
    assert updates.asset_name("1.2.3", "linux") is None


@pytest.mark.parametrize("url,ok", [
    (BASE + "x.zip", True), (CDN, True), ("https://objects.githubusercontent.com/a", True),
    ("http://github.com/coldmoth/ai-voice/releases/x", False),
    ("https://github.com/other/repo/releases/x", False),
    ("https://github.com/coldmoth/ai-voice/releases/../../evil", False),
    ("https://evil.example/coldmoth/ai-voice/releases/x", False),
    ("https://github.com.evil.example/coldmoth/ai-voice/releases/x", False),
    ("https://release-assets.githubusercontent.com:8443/a", False),
])
def test_allowed_url(url, ok):
    assert updates.allowed_url(url) is ok


def test_install_verifies_hash_and_launches(install):
    work, launched = install
    seen = []
    with asset_client(seen=seen) as client:
        updates.run_install("9.0.0", client)
    assert updates.job()["state"] == "installing"
    assert launched == [work / "AI-Voice-Setup-9.0.0.exe"]
    assert launched[0].read_bytes() == PAYLOAD
    assert seen[0] == BASE + "AI-Voice-Setup-9.0.0.exe.sha256" and any(u.startswith(CDN) for u in seen)


@pytest.mark.parametrize("kwargs,code", [
    ({"checksum": "0" * 64}, "checksum"),
    ({"checksum": None}, "no_checksum"),
    ({"checksum": "not a hash"}, "no_checksum"),
    ({"redirect": "https://evil.example/blob"}, "url"),
    ({"redirect": "http://release-assets.githubusercontent.com/blob"}, "url"),
])
def test_install_refuses_and_cleans_up(install, kwargs, code):
    work, launched = install
    with asset_client(**kwargs) as client:
        updates.run_install("9.0.0", client)
    assert updates.job()["state"] == "error" and updates.job()["error"] == code
    assert launched == [] and not work.exists()


def test_install_too_large(install, monkeypatch):
    work, launched = install
    monkeypatch.setattr(updates, "MAX_BYTES", 10)
    with asset_client() as client:
        updates.run_install("9.0.0", client)
    assert updates.job()["error"] == "too_large" and launched == [] and not work.exists()


def test_install_unsupported_platform(install, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    updates.run_install("9.0.0", asset_client())
    assert updates.job()["error"] == "unsupported"


def test_one_job_at_a_time(monkeypatch):
    monkeypatch.setattr(updates, "_job", {"state": "installing", "percent": 100, "error": None})
    assert updates.start_install("9.0.0") is False


def test_windows_launch_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **kw: calls.append(args))
    updates._install_windows(Path("C:/x/Setup.exe"))
    assert calls[0][0] == str(Path("C:/x/Setup.exe")) and "/SILENT" in calls[0] and "/RELAUNCH=1" in calls[0]
    assert calls[1][0] == "taskkill" and "/T" not in calls[1]


def test_macos_swap_script_launch(tmp_path, monkeypatch):
    target = tmp_path / "apps" / "AI Voice.app"
    target.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()

    def fake_run(args, **kw):
        if args[0] == "ditto":
            (Path(args[-1]) / "AI Voice.app").mkdir()
        return subprocess.CompletedProcess(args, 0)

    popen, killed = [], []
    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **kw: popen.append((args, kw)))
    monkeypatch.setattr(updates.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    updates._install_macos(tmp_path / "a.zip", work, target)
    args, kw = popen[0]
    assert args[:2] == ["/bin/bash", str(work / "swap.sh")] and args[5] == str(target) and kw["start_new_session"]
    assert Path(args[4]).name == "AI Voice.app" and Path(args[6]).parent == target.parent
    assert killed == [(updates.os.getppid(), updates.signal.SIGTERM)]


def test_macos_unwritable_folder(tmp_path, monkeypatch):
    def deny(**kw):
        raise PermissionError
    monkeypatch.setattr(updates.tempfile, "mkdtemp", deny)
    with pytest.raises(updates.UpdateError) as caught:
        updates._install_macos(tmp_path / "a.zip", tmp_path, tmp_path / "AI Voice.app")
    assert caught.value.code == "not_writable"


def test_target_app_from_bundle_path(monkeypatch):
    monkeypatch.setattr(updates.paths, "ROOT", Path("/Applications/AI Voice.app/Contents/Resources/app"))
    assert updates._target_app() == Path("/Applications/AI Voice.app")
    monkeypatch.setattr(updates.paths, "ROOT", Path("/opt/dev/ai-voice"))
    with pytest.raises(updates.UpdateError):
        updates._target_app()
