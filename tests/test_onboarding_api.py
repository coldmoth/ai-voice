"""Onboarding backend: key verification, Keychain endpoints, setup flag, permissions."""
import asyncio
import http.client
import json
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

import httpx
import pytest

from ai_voice import devices, driver, keys
from ai_voice.desktop import DesktopHandler

KEY = "test-key-0123456789abcdef"  # gitleaks:allow (fake test value)
TOKEN = "t" * 20


def client(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        result = handler(request)
        if isinstance(result, BaseException):
            raise result
        return result

    return httpx.Client(transport=httpx.MockTransport(wrapped)), seen


def status_client(code):
    return client(lambda request: httpx.Response(code))


@pytest.mark.parametrize("code,expected", [
    (200, {"ok": True}),
    (401, {"ok": False, "error": "invalid"}),
    (403, {"ok": False, "error": "invalid"}),
    (402, {"ok": True, "warning": "no_credit"}),
    (500, {"ok": False, "error": "network"}),
    (302, {"ok": False, "error": "network"}),
])
def test_verify_fish_mapping(code, expected):
    http_client, seen = status_client(code)
    assert keys.verify_fish_key(KEY, client=http_client) == expected
    assert str(seen[0].url) == keys.FISH_VERIFY_URL
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"


def test_verify_fish_timeout():
    http_client, _ = client(lambda request: httpx.ConnectTimeout("slow"))
    assert keys.verify_fish_key(KEY, client=http_client) == {"ok": False, "error": "network"}


@pytest.mark.parametrize("value", ["", "short", "a" * 201, "abc def ghijklmnopqr", "ключ" * 5, None, 123])
def test_verify_format(value):
    http_client, seen = status_client(200)
    assert keys.verify_fish_key(value, client=http_client) == {"ok": False, "error": "format"}
    assert seen == []


def test_verify_strips_whitespace():
    http_client, seen = status_client(200)
    assert keys.verify_fish_key("  " + KEY + "\n", client=http_client) == {"ok": True}
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("code,expected", [
    (200, {"ok": True}), (401, {"ok": False, "error": "invalid"}), (500, {"ok": False, "error": "network"}),
])
def test_verify_hf_mapping(code, expected):
    http_client, seen = status_client(code)
    assert keys.verify_hf_token(KEY, client=http_client) == expected
    assert str(seen[0].url) == keys.HF_VERIFY_URL


@pytest.fixture
def keychain(monkeypatch):
    calls = {"save": [], "delete": []}
    monkeypatch.setattr(keys, "save_key", lambda name, value: calls["save"].append((name, value)))
    monkeypatch.setattr(keys, "delete_key", lambda name: calls["delete"].append(name))
    return calls


def test_save_only_after_ok(keychain):
    http_client, _ = status_client(401)
    assert keys.save("fish", KEY, client=http_client) == {"ok": False, "error": "invalid"}
    assert keychain["save"] == []
    http_client, _ = status_client(200)
    assert keys.save("fish", " " + KEY + " ", client=http_client) == {"ok": True}
    assert keychain["save"] == [("FISH_API_KEY", KEY)]
    http_client, _ = status_client(402)
    assert keys.save("fish", KEY, client=http_client)["warning"] == "no_credit"
    assert len(keychain["save"]) == 2
    with pytest.raises(ValueError):
        keys.save("other", KEY, client=http_client)


def test_save_keychain_error(monkeypatch):
    def boom(name, value):
        raise RuntimeError("no")
    monkeypatch.setattr(keys, "save_key", boom)
    http_client, _ = status_client(200)
    assert keys.save("fish", KEY, client=http_client) == {"ok": False, "error": "keychain"}


def test_remove(keychain, monkeypatch):
    assert keys.remove("hf") == {"ok": True}
    assert keychain["delete"] == ["HF_TOKEN"]

    def boom(name):
        raise RuntimeError("no")
    monkeypatch.setattr(keys, "delete_key", boom)
    assert keys.remove("hf") == {"ok": False, "error": "keychain"}


@pytest.fixture
def bridge(monkeypatch):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), DesktopHandler)
    server.loop, server.token = loop, TOKEN
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def request(method, path, body=None, *, token=TOKEN, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"X-AI-Voice-Token": token}
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    yield server, request
    server.shutdown()
    server.server_close()
    loop.call_soon_threadsafe(loop.stop)


def test_api_keys_presence_only(bridge, monkeypatch):
    _server, request = bridge
    monkeypatch.setattr(keys, "has_key", lambda name="FISH_API_KEY": name == "FISH_API_KEY")
    status, body = request("GET", "/api/keys")
    assert status == 200
    assert json.loads(body) == {"fish": True, "hf": False}
    assert KEY.encode() not in body


def test_api_keys_post(bridge, monkeypatch):
    _server, request = bridge
    saved = []
    monkeypatch.setattr(keys, "save", lambda kind, value, **kw: saved.append((kind, value)) or {"ok": True})
    status, body = request("POST", "/api/keys/fish", {"key": "x" * 2000})
    assert status == 400 and saved == []
    status, body = request("POST", "/api/keys/fish", {"key": KEY})
    assert status == 200 and json.loads(body) == {"ok": True}
    assert KEY.encode() not in body and saved == [("fish", KEY)]
    status, body = request("POST", "/api/keys/hf", {"token": KEY})
    assert status == 200 and saved[-1] == ("hf", KEY)
    status, _ = request("POST", "/api/keys/fish", {"key": KEY}, token="bad")
    assert status == 403 and len(saved) == 2


def test_api_keys_remove(bridge, monkeypatch):
    _server, request = bridge
    removed = []
    monkeypatch.setattr(keys, "remove", lambda kind: removed.append(kind) or {"ok": True})
    for kind in ("fish", "hf"):
        status, body = request("POST", f"/api/keys/{kind}/remove", {})
        assert status == 200 and json.loads(body) == {"ok": True}
    assert removed == ["fish", "hf"]


# --- onboarding_completed -------------------------------------------------

def make_catalog(path, monkeypatch, *, key):
    from ai_voice import catalog
    monkeypatch.setattr(catalog, "has_key", lambda name="FISH_API_KEY": key)
    return catalog.Catalog(path)


def test_onboarding_default_fresh(tmp_path, monkeypatch):
    cat = make_catalog(tmp_path / "desktop.json", monkeypatch, key=True)
    assert cat.preferences()["onboarding_completed"] is False
    assert json.loads(cat.path.read_text())["onboarding_completed"] is False


def test_onboarding_default_existing_with_key(tmp_path, monkeypatch):
    from helpers import write_library
    path = write_library(tmp_path / "desktop.json", [], None)
    assert make_catalog(path, monkeypatch, key=True).preferences()["onboarding_completed"] is True
    assert json.loads(path.read_text())["onboarding_completed"] is True
    assert make_catalog(path, monkeypatch, key=False).preferences()["onboarding_completed"] is True


def test_onboarding_default_existing_without_key(tmp_path, monkeypatch):
    from helpers import write_library
    path = write_library(tmp_path / "desktop.json", [], None)
    assert make_catalog(path, monkeypatch, key=False).preferences()["onboarding_completed"] is False


def test_onboarding_default_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "desktop.json"
    path.write_text("{")
    assert make_catalog(path, monkeypatch, key=True).preferences()["onboarding_completed"] is True


def test_onboarding_update_validates(tmp_path, monkeypatch):
    cat = make_catalog(tmp_path / "desktop.json", monkeypatch, key=False)
    assert cat.update({"onboarding_completed": True})["onboarding_completed"] is True
    for bad in ("yes", 1):
        with pytest.raises(ValueError):
            cat.update({"onboarding_completed": bad})


def test_virtual_outputs(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    from ai_voice.devices import virtual_outputs
    names = ["MacBook Speakers", "BlackHole 2ch", "AI Voice Mic", "Loopback Audio", "AI Voice"]
    assert virtual_outputs(names) == ["BlackHole 2ch", "AI Voice Mic", "Loopback Audio", "AI Voice"]
    assert virtual_outputs(["BLACKHOLE 16CH"]) == ["BLACKHOLE 16CH"]
    assert virtual_outputs([]) == []


@pytest.fixture
def driver_bridge(bridge, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ai_voice import desktop
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(driver, "install_cable", lambda **_: pytest.fail("Windows installer in HAL test"))
    server, request = bridge
    calls = []
    state = {"active": False, "monitor_active": False}
    preview = {"preview_state": "idle"}

    async def stop_control():
        calls.append("control.stop")
        state.update(active=False, monitor_active=False)

    async def stop_preview():
        calls.append("preview.stop")
        preview["preview_state"] = "idle"

    server.control = SimpleNamespace(snapshot=lambda: dict(state), stop=stop_control)
    server.preview = SimpleNamespace(snapshot=lambda: dict(preview), stop=stop_preview)
    monkeypatch.setattr(driver, "_job", {"state": "idle", "action": None})
    monkeypatch.setattr(driver.paths, "ROOT", tmp_path)
    monkeypatch.setattr(driver, "HAL", tmp_path / "HAL")
    monkeypatch.setattr(driver, "_run", lambda line: calls.append("run") or "ok")
    monkeypatch.setattr(devices, "rescan", lambda: calls.append("rescan"))
    monkeypatch.setattr(desktop, "list_devices", lambda: [{"name": driver.DEVICE, "max_output_channels": 2}])
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(driver, "threading", SimpleNamespace(Thread=ImmediateThread))
    yield server, request, calls, state, preview


def test_driver_status(driver_bridge):
    _server, request, *_ = driver_bridge
    status, body = request("GET", "/api/driver/status")
    assert status == 200 and json.loads(body) == {
        "available": False, "installed": False, "device_present": True,
        "bundled_version": None, "installed_version": None,
        "job": {"state": "idle", "action": None},
    }


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_driver_busy(driver_bridge, monkeypatch, action):
    _server, request, calls, *_ = driver_bridge
    monkeypatch.setattr(driver, "_job", {"state": "running", "action": "install"})
    status, body = request("POST", f"/api/driver/{action}", {})
    assert status == 409 and json.loads(body)["error"]
    assert calls == []


def test_driver_unavailable(driver_bridge):
    _server, request, calls, *_ = driver_bridge
    status, body = request("POST", "/api/driver/install", {})
    assert status == 400 and json.loads(body)["error"]
    assert calls == []


@pytest.mark.parametrize("busy", ["active", "monitor_active", "preview", "driver", None])
def test_devices_rescan(driver_bridge, monkeypatch, busy):
    _server, request, calls, state, preview = driver_bridge
    if busy in ("active", "monitor_active"):
        state[busy] = True
    elif busy == "preview":
        preview["preview_state"] = "playing"
    elif busy == "driver":
        monkeypatch.setattr(driver, "_job", {"state": "running", "action": "install"})
    status, body = request("POST", "/api/devices/rescan", {})
    assert status == (409 if busy else 200)
    assert calls == ([] if busy else ["rescan"])
    assert ("error" in json.loads(body)) if busy else json.loads(body) == {"ok": True}


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_driver_stops_audio_and_rescans(driver_bridge, action):
    _server, request, calls, state, preview = driver_bridge
    (driver.paths.ROOT / "driver" / driver.NAME).mkdir(parents=True)
    state.update(active=True, monitor_active=True)
    preview["preview_state"] = "playing"
    status, body = request("POST", f"/api/driver/{action}", {})
    assert status == 200 and json.loads(body) == {"ok": True}
    assert calls == ["control.stop", "preview.stop", "run"] + (
        [("sleep", 2)] if action == "uninstall" else []) + ["rescan"]
    assert driver.job() == {"state": "done", "action": action}


@pytest.mark.parametrize("present_on", [1, 3, None])
def test_driver_install_retry_limit(driver_bridge, monkeypatch, present_on):
    from ai_voice import desktop
    _server, request, calls, *_ = driver_bridge
    (driver.paths.ROOT / "driver" / driver.NAME).mkdir(parents=True)
    attempts = []
    def outputs():
        attempts.append(True)
        return ([{"name": driver.DEVICE, "max_output_channels": 2}]
                if present_on and len(attempts) >= present_on else [])
    monkeypatch.setattr(desktop, "list_devices", outputs)
    status, _ = request("POST", "/api/driver/install", {})
    assert status == 200
    count = present_on or 5
    assert len(attempts) == calls.count("rescan") == count
    assert calls.count(("sleep", 2)) == count - 1
    assert driver.job()["state"] == "done"


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_driver_after_idle_guard(driver_bridge, monkeypatch, action):
    _server, request, calls, state, _preview = driver_bridge
    (driver.paths.ROOT / "driver" / driver.NAME).mkdir(parents=True)
    def run(line):
        state["active"] = True
        return "ok"
    monkeypatch.setattr(driver, "_run", run)
    status, _ = request("POST", f"/api/driver/{action}", {})
    assert status == 200 and "rescan" not in calls
    assert driver.job()["state"] == "done"


@pytest.mark.parametrize("result", ["cancelled", "failed"])
def test_driver_failed_or_cancelled_response(driver_bridge, monkeypatch, result):
    _server, request, calls, *_ = driver_bridge
    monkeypatch.setattr(driver, "_run", lambda line: result)
    status, body = request("POST", "/api/driver/uninstall", {})
    assert status == 200 and json.loads(body) == {"ok": True}
    status, body = request("GET", "/api/driver/status")
    assert status == 200
    assert json.loads(body)["job"] == {"state": result, "action": "uninstall"}
    assert calls == ["control.stop", "preview.stop"]


def test_driver_start_race(driver_bridge, monkeypatch):
    _server, request, calls, *_ = driver_bridge
    monkeypatch.setattr(driver, "start", lambda *a, **kw: False)
    status, body = request("POST", "/api/driver/uninstall", {})
    assert status == 409 and json.loads(body)["error"]
    assert calls == []


def test_driver_stop_error_ignored(driver_bridge, monkeypatch):
    server, request, calls, *_ = driver_bridge
    async def stop():
        calls.append("control.stop")
        raise RuntimeError("stop failure")
    server.control.stop = stop
    status, body = request("POST", "/api/driver/uninstall", {})
    assert status == 200 and json.loads(body) == {"ok": True}
    assert calls == ["control.stop", "preview.stop", "run", ("sleep", 2), "rescan"]


def test_driver_output_filter(driver_bridge, monkeypatch):
    from ai_voice import desktop
    _server, request, *_ = driver_bridge
    monkeypatch.setattr(desktop, "list_devices", lambda: [
        {"name": driver.DEVICE, "max_output_channels": 0}, {"name": "Speakers", "max_output_channels": 2}])
    status, body = request("GET", "/api/driver/status")
    assert status == 200 and json.loads(body)["device_present"] is False


# --- permissions ----------------------------------------------------------

class FakeHelper:
    def __init__(self, report=None, error=None):
        self.report, self.error, self.calls = report or {}, error, []

    async def doctor(self):
        self.calls.append("doctor")
        if self.error:
            raise self.error
        return self.report

    async def authorize_only(self, kind):
        self.calls.append(kind)
        if self.error:
            raise self.error
        return self.report


@pytest.fixture(autouse=True)
def _fresh_permission_cache(monkeypatch):
    from ai_voice import asr
    monkeypatch.setattr(asr, "_PERM_CACHE", None)


@pytest.mark.parametrize("value,expected", [
    ("notDetermined", "not_determined"), ("authorized", "authorized"), ("denied", "denied"),
    ("restricted", "restricted"), ("unknown", "unknown"), (None, "unknown"), (5, "unknown"),
])
def test_map_permission(value, expected):
    from ai_voice.asr import map_permission
    assert map_permission(value) == expected


async def test_permission_status_parses_and_caches():
    from ai_voice import asr
    helper = FakeHelper({"microphone_authorization": "authorized", "speech_authorization": "notDetermined"})
    now = [100.0]
    first = await asr.permission_status(helper=helper, clock=lambda: now[0])
    assert first == {"microphone": "authorized", "speech": "not_determined"}
    now[0] = 101.0
    await asr.permission_status(helper=helper, clock=lambda: now[0])
    assert helper.calls == ["doctor"]
    now[0] = 101.6
    await asr.permission_status(helper=helper, clock=lambda: now[0])
    assert helper.calls == ["doctor", "doctor"]


async def test_permission_status_helper_error():
    from ai_voice import asr
    result = await asr.permission_status(helper=FakeHelper(error=asr.ASRError("boom")))
    assert result == {"microphone": "unknown", "speech": "unknown"}


async def test_request_permission_kind_validation():
    from ai_voice import asr
    helper = FakeHelper({"microphone_authorization": "denied", "speech_authorization": "authorized"})
    with pytest.raises(ValueError):
        await asr.request_permission("camera", helper=helper)
    assert helper.calls == []
    result = await asr.request_permission("speech", helper=helper)
    assert helper.calls == ["speech"]
    assert result == {"microphone": "denied", "speech": "authorized"}


def test_permissions_http(bridge, monkeypatch):
    from ai_voice import asr
    _server, request = bridge
    seen = []

    async def status():
        return {"microphone": "authorized", "speech": "denied"}

    async def ask(kind):
        seen.append(kind)
        return {"microphone": "authorized", "speech": "authorized"}

    monkeypatch.setattr(asr, "permission_status", status)
    monkeypatch.setattr(asr, "request_permission", ask)
    status_code, body = request("GET", "/api/permissions")
    assert status_code == 200 and json.loads(body) == {"microphone": "authorized", "speech": "denied"}
    status_code, body = request("POST", "/api/permissions/request", {"kind": "speech"})
    assert status_code == 200 and seen == ["speech"]
    status_code, _ = request("POST", "/api/permissions/request", {"kind": "camera"})
    assert status_code == 400 and seen == ["speech"]


# --- update checker -------------------------------------------------------

@pytest.fixture
def update_bridge(bridge, tmp_path, monkeypatch):
    from ai_voice import updates
    server, request = bridge
    server.catalog = make_catalog(tmp_path / "desktop.json", monkeypatch, key=False)

    class Control:
        def snapshot(self):
            return {"active": False}

        async def apply_preferences(self, data):
            return server.catalog.update(data)

    server.control = Control()
    # Fail before any accidental real HTTP call, even in a failing test.
    def no_network(*args, **kwargs):
        raise AssertionError("update HTTP must be mocked")
    monkeypatch.setattr(updates.httpx, "Client", no_network)
    return server, request


def test_update_check_endpoint(update_bridge, monkeypatch):
    from ai_voice import desktop, updates
    server, request = update_bridge
    calls = []
    writes = []
    saved = server.catalog.set_update_state

    def record_save(state):
        writes.append(state)
        saved(state)

    def check(state, auto, *, now, force):
        # Prove the handler released the catalog lock before checking HTTP.
        acquired = []
        def take_lock():
            with server.catalog.lock:
                acquired.append(True)
        thread = threading.Thread(target=take_lock, daemon=True)
        thread.start()
        thread.join(timeout=1)
        assert acquired == [True]
        calls.append((state, auto, now, force))
        return {"status": "current", "version": "0.4.0"}, {**state, "last_check": now}

    monkeypatch.setattr(updates, "check", check)
    monkeypatch.setattr(desktop.time, "time", lambda: 200_000)
    monkeypatch.setattr(server.catalog, "set_update_state", record_save)
    status, body = request("POST", "/api/update-check", {})
    assert status == 200 and json.loads(body) == {"status": "current", "version": "0.4.0"}
    assert calls[0] == ({"last_check": 0, "latest": None, "skipped": None}, True, 200_000, False)
    assert server.catalog.update_state()["last_check"] == 200_000 and len(writes) == 1
    status, _ = request("POST", "/api/update-check", {})
    assert status == 200 and len(writes) == 1
    server.catalog.update({"update_auto": False})
    status, _ = request("POST", "/api/update-check", {"force": True})
    assert status == 200 and calls[-1][1:] == (False, 200_000, True)


@pytest.mark.parametrize("path,payload", [
    ("/api/update-check", {}), ("/api/update-skip", {"version": "1.2.3"}), ("/api/update-install", {}),
])
def test_update_endpoints_require_token(update_bridge, path, payload):
    server, request = update_bridge
    before = server.catalog.update_state()
    assert request("POST", path, payload, token="")[0] == 403
    assert server.catalog.update_state() == before


@pytest.mark.parametrize("force", [None, 1, "true", [], {}])
def test_update_check_bad_force(update_bridge, force):
    _, request = update_bridge
    status, body = request("POST", "/api/update-check", {"force": force})
    assert status == 400 and "error" in json.loads(body)


def test_update_check_error_does_not_save(update_bridge, monkeypatch):
    from ai_voice import updates
    server, request = update_bridge
    before = server.catalog.update_state()
    monkeypatch.setattr(updates, "check", lambda state, *args, **kwargs:
                        ({"status": "error", "error": "network"}, state))
    def no_save(state):
        raise AssertionError("unchanged state must not be saved")
    monkeypatch.setattr(server.catalog, "set_update_state", no_save)
    status, body = request("POST", "/api/update-check", {})
    assert status == 200 and json.loads(body) == {"status": "error", "error": "network"}
    assert server.catalog.update_state() == before


@pytest.mark.parametrize("version", [None, "bad", "1.2", "1.2.3-rc1", 123])
def test_update_skip_bad_version(update_bridge, version):
    server, request = update_bridge
    before = server.catalog.update_state()
    status, body = request("POST", "/api/update-skip", {"version": version})
    assert status == 400 and json.loads(body) == {"error": "Version is not valid."}
    assert server.catalog.update_state() == before


def test_update_skip_preserves_cache(update_bridge):
    server, request = update_bridge
    from ai_voice.updates import RELEASE_URL_PREFIX
    state = {"last_check": 100, "latest": {"version": "1.2.3", "url": RELEASE_URL_PREFIX + "tag/v1.2.3"},
             "skipped": None}
    server.catalog.set_update_state(state)
    status, body = request("POST", "/api/update-skip", {"version": "1.2.3"})
    assert status == 200 and json.loads(body) == {"ok": True}
    assert server.catalog.update_state() == {**state, "skipped": "1.2.3"}


def test_update_preferences_roundtrip(update_bridge, monkeypatch):
    server, request = update_bridge
    assert server.catalog.preferences()["update_auto"] is True
    for auto in (False, True):
        status, body = request("POST", "/api/preferences", {"update_auto": auto})
        prefs = json.loads(body)
        assert status == 200 and prefs["update_auto"] is auto
        assert "update_state" not in prefs
        reloaded = make_catalog(server.catalog.path, monkeypatch, key=False)
        assert reloaded.preferences()["update_auto"] is auto
    before = server.catalog.preferences()
    for bad in (None, 1, "true"):
        status, body = request("POST", "/api/preferences", {"update_auto": bad})
        assert status == 400 and json.loads(body) == {"error": "Update setting must be true or false."}
        assert server.catalog.preferences() == before
    assert request("POST", "/api/preferences", {"update_state": {}})[0] == 400


@pytest.mark.parametrize("bad", [None, [], "garbage", 42])
def test_update_state_non_dict_defaults(tmp_path, monkeypatch, bad):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"update_state": bad, "update_auto": "bad", "output_gain_db": 3.0}))
    cat = make_catalog(path, monkeypatch, key=False)
    assert cat.update_state() == {"last_check": 0, "latest": None, "skipped": None}
    assert cat.preferences()["update_auto"] is True and cat.preferences()["output_gain_db"] == 3.0


@pytest.mark.parametrize("field,bad", [
    ("last_check", -1), ("last_check", float("inf")), ("last_check", float("nan")),
    ("last_check", True), ("last_check", "bad"), ("latest", []),
    ("latest", {"version": "bad", "url": "https://github.com/coldmoth/ai-voice/releases/1"}),
    ("latest", {"version": "1.2.3", "url": "https://example.com"}),
    ("latest", {"version": "1.2.3", "url": None}), ("skipped", 123), ("skipped", "1.2.3-rc1"),
])
def test_update_state_resets_only_bad_part(tmp_path, monkeypatch, field, bad):
    from ai_voice.updates import RELEASE_URL_PREFIX
    valid = {"last_check": 100.5, "latest": {"version": "1.2.3", "url": RELEASE_URL_PREFIX + "tag/v1.2.3"},
             "skipped": "1.1.0"}
    defaults = {"last_check": 0, "latest": None, "skipped": None}
    expected = {**valid, field: defaults[field]}
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"update_state": {**valid, field: bad}, "update_auto": False}))
    cat = make_catalog(path, monkeypatch, key=False)
    assert cat.update_state() == expected
    assert cat.preferences()["update_auto"] is False
    cat.set_update_state({**valid, field: bad})
    assert cat.update_state() == expected
    assert make_catalog(path, monkeypatch, key=False).update_state() == expected


def test_update_state_copies_and_save_failure(tmp_path, monkeypatch):
    from ai_voice.updates import RELEASE_URL_PREFIX
    cat = make_catalog(tmp_path / "desktop.json", monkeypatch, key=False)
    state = {"last_check": 1, "latest": {"version": "v1.2.3", "url": RELEASE_URL_PREFIX + "tag/v1.2.3"},
             "skipped": "v1.1.0"}
    cat.set_update_state(state)
    before = cat.update_state()
    state["latest"]["version"] = "9.9.9"
    snapshot = cat.update_state()
    snapshot["latest"]["url"] = "changed"
    assert cat.update_state() == before
    assert make_catalog(cat.path, monkeypatch, key=False).update_state() == before
    def fail():
        raise OSError("disk full")
    monkeypatch.setattr(cat, "save", fail)
    with pytest.raises(OSError):
        cat.set_update_state({"last_check": 2})
    assert cat.update_state() == before


def test_window_drag_covers_onboarding():
    swift = (Path(__file__).resolve().parents[1] / "macos" / "Desktop.swift").read_text()
    assert "'.n-toolbar,.n-traffic-spacer,.ob'" in swift


def test_update_install_route(update_bridge, monkeypatch):
    from ai_voice import updates
    server, request = update_bridge
    started = []
    monkeypatch.setattr(updates, "start_install", lambda version: started.append(version) or len(started) == 1)
    assert request("POST", "/api/update-install", {})[0] == 400 and started == []  # nothing newer known
    url = updates.RELEASE_URL_PREFIX + "tag/v9.0.0"
    server.catalog.set_update_state({**server.catalog.update_state(), "latest": {"version": "9.0.0", "url": url}})
    assert request("POST", "/api/update-install", {})[0] == 200 and started == ["9.0.0"]
    assert request("POST", "/api/update-install", {})[0] == 409
    assert request("GET", "/api/update-status")[0] == 200
