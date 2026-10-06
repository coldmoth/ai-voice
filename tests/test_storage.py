"""Storage scan/clear regressions on a temporary tree with fake controllers."""
import asyncio
import os
import time

import pytest

from ai_voice.storage import (StorageBadRequest, StorageConflict, StorageService, cleanup_old_logs, dir_size)


def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class FakeStore:
    def __init__(self, root):
        self.root = root

    def get(self, voice_id):
        return {"id": voice_id, "name": "Имя " + voice_id}


class FakeTrainer:
    def __init__(self):
        self.state, self.queue, self.removed = None, [], []

    def status(self):
        return None if self.state is None and not self.queue else {"state": self.state or "idle", "queue": self.queue}

    def remove_queued(self, voice_id):
        self.removed.append(voice_id)


class FakeVc:
    def __init__(self, root, uploads):
        self.store, self.trainer = FakeStore(root), FakeTrainer()
        self.active_voice, self.loading, self.uploads = None, False, uploads
        self.deleted = []

    @property
    def busy(self):
        return self.active_voice is not None or self.loading

    async def delete(self, voice_id):
        self.deleted.append(voice_id)
        import shutil
        shutil.rmtree(self.store.root / voice_id)
        return {"ok": True}


class FakeControl:
    def __init__(self):
        self.state = {"active": False, "mode": "mic"}

    def snapshot(self):
        return dict(self.state)


class FakeCatalog:
    def __init__(self):
        self.prefs, self.cleared = {"asr_engine": "apple"}, 0

    def preferences(self):
        return dict(self.prefs)

    def clear_metadata_cache(self):
        self.cleared += 1


@pytest.fixture
def env(tmp_path):
    data, hub = tmp_path / "data", tmp_path / "hub"
    write(data / "vc-voices" / "aaa" / "model.pth", 1000)
    write(data / "vc-voices" / "bbb" / "model.pth", 500)
    write(data / "logs" / "vc_worker.log", 30)
    write(data / "logs" / "old.log", 20)
    write(data / "vc-uploads" / "u1" / "x.wav", 40)
    write(data / "voice_metadata.json", 7)
    write(tmp_path / "spike" / "w.bin", 4000)
    write(tmp_path / "rt" / "venv" / "lib" / "a.py", 300)
    write(hub / "models--istupakov--GigaAM-v2" / "blobs" / "b", 900)
    write(hub / "models--other--thing" / "blobs" / "b", 111)
    vc = FakeVc(data / "vc-voices", data / "vc-uploads")
    control, catalog = FakeControl(), FakeCatalog()
    app = tmp_path / "AI Voice.app" / "Contents" / "Resources"
    write(tmp_path / "AI Voice.app" / "Contents" / "bin", 2000)
    app.mkdir(parents=True, exist_ok=True)
    service = StorageService(vc=vc, control=control, catalog=catalog, run_async=lambda coro: asyncio.run(coro),
                             data=data, spike=tmp_path / "spike", vc_python=tmp_path / "rt" / "venv" / "bin" / "python",
                             hf_hub=hub, resources=str(app))
    return service, vc, control, catalog, tmp_path


def cats(result):
    return {c["id"]: c for c in result["categories"]}


def test_scan_sizes_and_items(env):
    service, *_ = env
    result = service.scan()
    c = cats(result)
    assert c["vc_voices"]["bytes"] == 1500 and c["vc_voices"]["clearable"]
    assert [(i["id"], i["label"], i["bytes"]) for i in c["vc_voices"]["items"]] == [
        ("aaa", "Имя aaa", 1000), ("bbb", "Имя bbb", 500)]
    assert c["vc_models"]["bytes"] == 4000 and not c["vc_models"]["clearable"] and c["vc_models"]["note"]
    assert c["vc_env"]["bytes"] == 300 and not c["vc_env"]["clearable"]
    assert c["asr_gigaam"]["bytes"] == 900 and c["asr_gigaam"]["note"]
    assert c["logs"]["bytes"] == 50 and c["uploads"]["bytes"] == 40 and c["cache"]["bytes"] == 7
    assert c["app"]["bytes"] == 2000 and not c["app"]["clearable"]
    assert result["total"] == sum(x["bytes"] for x in result["categories"])


def test_app_hidden_without_env(env):
    service, *_ = env
    service.resources = None
    assert "app" not in cats(service.scan())


def test_symlinks_not_double_counted(tmp_path):
    write(tmp_path / "d" / "real" / "f", 100)
    os.symlink(tmp_path / "d" / "real", tmp_path / "d" / "link")
    os.symlink(tmp_path / "d" / "real" / "f", tmp_path / "d" / "flink")
    assert dir_size(tmp_path / "d") < 100 + 200 and dir_size(tmp_path / "d") >= 100
    assert dir_size(tmp_path / "missing") == 0


def test_by_link_category_measures_target(env):
    service, _, _, _, tmp = env
    os.symlink(tmp / "spike", tmp / "spike-link")
    service.spike = tmp / "spike-link"
    assert cats(service.scan())["vc_models"]["bytes"] == 4000


def test_get_caches_for_30_seconds(env):
    service, *_ = env
    now = [0.0]
    service.clock = lambda: now[0]
    first = service.get()
    write(env[4] / "data" / "logs" / "new.log", 999)
    assert service.get() is first
    now[0] = 31
    assert cats(service.get())["logs"]["bytes"] == 1049


def test_get_times_out(env):
    service, *_ = env
    import threading
    gate = threading.Event()
    service.scan = lambda: gate.wait(5) and {}
    with pytest.raises(TimeoutError):
        service.get(timeout=0.1)
    gate.set()


def test_clear_single_voice_uses_delete_path(env):
    service, vc, *_ = env
    result = service.clear("vc_voices", "aaa")
    assert vc.deleted == ["aaa"]
    assert [i["id"] for i in cats(result)["vc_voices"]["items"]] == ["bbb"]


def test_clear_all_voices(env):
    service, vc, *_ = env
    result = service.clear("vc_voices")
    assert sorted(vc.deleted) == ["aaa", "bbb"] and sorted(vc.trainer.removed) == ["aaa", "bbb"]
    assert cats(result)["vc_voices"]["bytes"] == 0


@pytest.mark.parametrize("setup", ["active", "loading", "running", "paused", "queued"])
def test_clear_all_voices_conflicts(env, setup):
    service, vc, *_ = env
    if setup == "active":
        vc.active_voice = "aaa"
    elif setup == "loading":
        vc.loading = True
    elif setup == "queued":
        vc.trainer.queue = [{"voice_id": "bbb"}]
    else:
        vc.trainer.state = setup
    with pytest.raises(StorageConflict):
        service.clear("vc_voices")
    assert vc.deleted == []


def test_clear_gigaam(env):
    service, _, control, catalog, tmp = env
    control.state = {"active": True, "mode": "mic"}
    catalog.prefs["asr_engine"] = "gigaam"
    with pytest.raises(StorageConflict):
        service.clear("asr_gigaam")
    assert (tmp / "hub" / "models--istupakov--GigaAM-v2").exists()
    control.state = {"active": True, "mode": "mic"}
    catalog.prefs["asr_engine"] = "apple"
    result = service.clear("asr_gigaam")
    assert not (tmp / "hub" / "models--istupakov--GigaAM-v2").exists()
    assert (tmp / "hub" / "models--other--thing").exists()
    assert cats(result)["asr_gigaam"]["bytes"] == 0


def test_clear_logs_keeps_open_worker_log(env):
    service, vc, _, _, tmp = env
    vc.active_voice = "aaa"
    service.clear("logs")
    assert [p.name for p in (tmp / "data" / "logs").iterdir()] == ["vc_worker.log"]
    vc.active_voice = None
    service.clear("logs")
    assert list((tmp / "data" / "logs").iterdir()) == []


def test_clear_uploads_and_cache(env):
    service, _, _, catalog, tmp = env
    old = time.time() - 3600
    os.utime(tmp / "data" / "vc-uploads" / "u1" / "x.wav", (old, old))
    os.utime(tmp / "data" / "vc-uploads" / "u1", (old, old))
    result = service.clear("uploads")
    assert (tmp / "data" / "vc-uploads").is_dir() and list((tmp / "data" / "vc-uploads").iterdir()) == []
    assert cats(result)["uploads"]["bytes"] == 0
    service.clear("cache")
    assert catalog.cleared == 1


@pytest.mark.parametrize("category", ["vc_models", "vc_env", "app", "nope"])
def test_non_clearable_is_bad_request(env, category):
    service, *_ = env
    with pytest.raises(StorageBadRequest):
        service.clear(category)


def test_cleanup_old_logs(tmp_path):
    now = time.time()
    old, fresh = write(tmp_path / "old.log", 1), write(tmp_path / "fresh.log", 1)
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(fresh, (now - 6 * 86400, now - 6 * 86400))
    assert cleanup_old_logs(tmp_path, now=now) == 1
    assert not old.exists() and fresh.exists()
    assert cleanup_old_logs(tmp_path / "missing") == 0


def test_http_routes(env):
    import http.client, json, threading
    from http.server import ThreadingHTTPServer
    from ai_voice.desktop import DesktopHandler
    service = env[0]
    server = ThreadingHTTPServer(("127.0.0.1", 0), DesktopHandler)
    server.token, server.storage = "t" * 20, service
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"X-AI-Voice-Token": server.token}
        payload = None
        if body is not None:
            payload, headers["Content-Type"] = json.dumps(body), "application/json"
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    try:
        status, data = call("GET", "/api/storage")
        assert status == 200 and data["total"] > 0
        assert call("POST", "/api/storage/clear", {"category": "app"})[0] == 400
        env[1].active_voice = "aaa"
        assert call("POST", "/api/storage/clear", {"category": "vc_voices"})[0] == 409
        service.get = lambda: (_ for _ in ()).throw(TimeoutError())
        status, data = call("GET", "/api/storage")
        assert status == 503 and 'took too long' in data["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_clear_uploads_keeps_fresh_folder(env):
    service, _, _, _, tmp = env
    result = service.clear("uploads")
    assert [p.name for p in (tmp / "data" / "vc-uploads").iterdir()] == ["u1"]
    assert cats(result)["uploads"]["bytes"] > 0


def test_clear_all_voices_failure_invalidates_cache(env):
    service, vc, *_ = env
    service.get()
    original = vc.delete

    async def failing(voice_id):
        if voice_id == "bbb":
            raise ValueError("boom")
        return await original(voice_id)

    vc.delete = failing
    with pytest.raises(ValueError):
        service.clear("vc_voices")
    assert [i["id"] for i in cats(service.get())["vc_voices"]["items"]] == ["bbb"]


@pytest.mark.parametrize("manifest_version", [1, 2])
def test_engine_row_and_removal(env, tmp_path, monkeypatch, manifest_version):
    from ai_voice import vc_engine
    import json
    root = tmp_path / "vc-runtime"
    write(root / "venv/bin/python", 10)
    (root / "engine.json").write_text('{"version": 1}')
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": manifest_version, "uv": {"size": 0}, "files": []}))
    engine = vc_engine.Installer(root=root, manifest=manifest)
    monkeypatch.setattr(vc_engine, "_singleton", engine)
    assert not engine._dev_runtime()
    assert engine.state()["installed"] == (manifest_version == 1)
    service, vc, *_ = env
    categories = cats(service.scan())
    assert "vc_models" not in categories and "vc_env" not in categories
    assert categories["vc_engine"]["bytes"] == dir_size(root)
    assert categories["vc_engine"]["clearable"]
    vc.active_voice = "aaa"
    with pytest.raises(StorageConflict):
        service.clear("vc_engine")
    assert root.exists()
    vc.active_voice = None
    vc.trainer.state = "paused"
    with pytest.raises(StorageConflict):
        service.clear("vc_engine")
    vc.trainer.state = None
    result = service.clear("vc_engine")
    assert not root.exists() and "vc_engine" not in cats(result)
    assert {"vc_models", "vc_env"} <= cats(result).keys()


def test_engine_dev_row_unchanged(env, tmp_path, monkeypatch):
    from ai_voice import vc_engine
    import json
    root = tmp_path / "vc-runtime"
    target = tmp_path / "owner-venv"
    write(target / "bin/python", 10)
    root.mkdir()
    (root / "venv").symlink_to(target, target_is_directory=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "uv": {"size": 0}, "files": []}))
    engine = vc_engine.Installer(root=root, manifest=manifest)
    monkeypatch.setattr(vc_engine, "_singleton", engine)
    service = env[0]
    categories = cats(service.scan())
    assert "vc_engine" not in categories
    assert {"vc_models", "vc_env"} <= categories.keys()
    with pytest.raises(StorageBadRequest, match="dev_runtime"):
        service.clear("vc_engine")
    assert (target / "bin/python").read_bytes() == b"x" * 10
