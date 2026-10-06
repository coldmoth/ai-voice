"""Tests for the voice-conversion HTTP API."""
import asyncio
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ai_voice.i18n import t
from ai_voice.catalog import Catalog
from ai_voice.desktop_control import ConflictError, DesktopControl
from ai_voice.vc_control import VcController, VcWorker, parse_multipart
from ai_voice.vc_store import VcStore, VcStoreError
from ai_voice.vc_train import VcTrainError
from helpers import TEST_VOICE_ID, write_library


class FakeLease:
    def __init__(self):
        self.acquire_calls = 0
        self.close_calls = 0

    def acquire(self):
        self.acquire_calls += 1

    def close(self):
        self.close_calls += 1


@pytest.fixture(autouse=True)
def fake_audio_lease(monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.AudioLease", FakeLease)


def _build_multipart(fields, files, boundary="X-BOUNDARY"):
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}".encode("utf-8"))
        body.write(b"\r\n")
    for name, filename, content in files:
        body.write(f"--{boundary}\r\n".encode())
        body.write(f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n".encode())
        body.write(b"Content-Type: application/octet-stream\r\n\r\n")
        body.write(content)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue()


def _rfile_from_bytes(data):
    rfile = io.BytesIO(data)

    class _R:
        def read(self, n=-1):
            return rfile.read(n)
    return _R()


def test_parse_multipart_simple(tmp_path):
    body = _build_multipart({"name": "Alice"}, [("file", "a.wav", b"AAAA")])
    rfile = _rfile_from_bytes(body)
    fields, files = parse_multipart("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body), tmp_path)
    assert fields == {"name": "Alice"}
    assert len(files) == 1
    assert files[0].read_bytes() == b"AAAA"


def test_parse_multipart_two_files(tmp_path):
    body = _build_multipart({"name": "X"}, [("f1", "a.wav", b"one"), ("f2", "b.wav", b"two")])
    rfile = _rfile_from_bytes(body)
    fields, files = parse_multipart("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body), tmp_path)
    assert fields == {"name": "X"}
    assert len(files) == 2


def test_parse_multipart_unsafe_filename(tmp_path):
    body = _build_multipart({}, [("file", "../../etc/passwd", b"data")])
    rfile = _rfile_from_bytes(body)
    _, files = parse_multipart("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body), tmp_path)
    assert files[0].name.endswith("passwd")
    assert ".." not in files[0].name


def test_parse_multipart_size_limit(tmp_path):
    class _NoRead:
        def read(self, n):
            raise AssertionError("should not read")
    with pytest.raises(ValueError):
        parse_multipart("multipart/form-data; boundary=X", _NoRead(), 600 * 1024 * 1024, tmp_path)


def test_parse_multipart_bad_boundary(tmp_path):
    rfile = _rfile_from_bytes(b"no boundary here")
    with pytest.raises(ValueError):
        parse_multipart("multipart/form-data; boundary=X", rfile, 17, tmp_path)


def test_parse_multipart_preserves_binary(tmp_path):
    payload = b"before\r\n---inside---\r\nafter"
    body = _build_multipart({}, [("f", "blob.bin", payload)])
    rfile = _rfile_from_bytes(body)
    _, files = parse_multipart("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body), tmp_path)
    assert files[0].read_bytes() == payload


@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_parse_multipart_chunk_boundaries(tmp_path, chunk_size):
    payload = b"x" * 65536 + b"\x00\r\n"
    body = _build_multipart({"name": "Голос"},
                            [("f", "model.pth", payload), ("f", "model.index", b"index")])
    stream = io.BytesIO(body + b"unread")

    class Chunked:
        def read(self, n):
            assert 0 < n <= 65536
            return stream.read(min(n, chunk_size))

    fields, files = parse_multipart("multipart/form-data; boundary=X-BOUNDARY",
                                    Chunked(), len(body), tmp_path)
    assert fields == {"name": "Голос"}
    assert [path.suffix for path in files] == [".pth", ".index"]
    assert [path.read_bytes() for path in files] == [payload, b"index"]
    assert stream.read() == b"unread"


@pytest.mark.parametrize("declared_extra", [0, 100])
def test_parse_multipart_truncated_body(tmp_path, declared_extra):
    body = _build_multipart({}, [("f", "model.pth", b"data")])
    body = body[:-20]
    with pytest.raises(ValueError, match='Invalid file upload'):
        parse_multipart("multipart/form-data; boundary=X-BOUNDARY",
                        io.BytesIO(body), len(body) + declared_extra, tmp_path)


class _FakeProcess:
    def __init__(self, events):
        self._events = list(events)
        class _Stdin(io.StringIO):
            def close(self):
                self.was_closed = True
        self.stdin = _Stdin()
        self.stdout = self._make_stdout()
        self.stderr = io.StringIO()
        self.pid = 12345
        self._returncode = None
        self._sent_commands = []

    def _make_stdout(self):
        data = []
        for event in self._events:
            data.append(json.dumps(event) + "\n")

        class _Stdout:
            def __init__(self, lines):
                self._lines = lines
                self._idx = 0
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                if self._idx >= len(self._lines):
                    self.closed = True
                    raise StopIteration
                line = self._lines[self._idx]
                self._idx += 1
                return line

            def close(self):
                self.closed = True
        return _Stdout(data)

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def terminate(self):
        self._returncode = 0

    def kill(self):
        self._returncode = 0


class _FakePopen:
    def __init__(self, events):
        self._events = events
        self.last = None

    def __call__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.last = _FakeProcess(self._events)
        return self.last


def test_vc_worker_load_and_start(tmp_path):
    events = [
        {"event": "status", "state": "loaded", "rss_mb": 100, "dropped_blocks": 0, "latency_ms": 10},
        {"event": "status", "state": "running", "rss_mb": 110, "dropped_blocks": 1, "latency_ms": 20},
        {"event": "levels", "input_level": 0.1, "output_level": 0.2,
         "input_db": -20.0, "gate_open": True},
    ]
    popen = _FakePopen(events)
    worker = VcWorker(popen=popen, python_path=tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir()
    (directory / "reference.wav").write_bytes(b"RIFF")
    meta = {"id": "v1", "kind": "zeroshot", "pitch_shift": 1, "index_rate": 0.3}
    devices = {"input_device": "in", "output_device": "out", "monitor_enabled": False, "monitor_device": None}
    params = {"output_gain_db": 0.0, "monitor_gain_db": 0.0,
              "gate_enabled": True, "gate_db": -50.0}
    worker.start(meta, directory, devices, params)
    commands = [line for line in popen.last.stdin.getvalue().splitlines() if line]
    payloads = [json.loads(c) for c in commands]
    assert payloads[0]["cmd"] == "load"
    assert payloads[0]["voice"]["kind"] == "zeroshot"
    assert payloads[0]["voice"]["reference"].endswith("reference.wav")
    assert payloads[0]["config"] == {"hop_ms": 256}
    assert payloads[0]["params"]["diffusion_steps"] == 4
    assert payloads[0]["params"]["gate_enabled"] is True
    assert payloads[0]["params"]["gate_db"] == -50.0
    assert payloads[0]["voice"]["ref_seconds"] == 5
    assert payloads[0]["voice"]["pitch_shift"] == meta["pitch_shift"]
    assert payloads[1]["cmd"] == "start"
    assert payloads[1]["devices"]["input_device"] == "in"
    snap = worker.snapshot()
    assert snap["state"] == "running"
    assert snap["input_level"] == 0.1
    assert snap["input_db"] == -20.0 and snap["gate_open"] is True
    worker.stop()
    assert worker.snapshot()["input_db"] is None and worker.snapshot()["gate_open"] is None


def test_vc_worker_imported_index_only_if_exists(tmp_path):
    events = [
        {"event": "status", "state": "loaded"},
        {"event": "status", "state": "running"},
    ]
    popen = _FakePopen(events)
    worker = VcWorker(popen=popen, python_path=tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir()
    (directory / "model.pth").write_bytes(b"pth")
    meta = {"id": "v2", "kind": "imported", "pitch_shift": 0, "index_rate": 0.5}
    worker.start(meta, directory, {"input_device": None, "output_device": None,
                                    "monitor_enabled": False, "monitor_device": None},
                  {"output_gain_db": 0.0, "monitor_gain_db": 0.0})
    payload = json.loads([line for line in popen.last.stdin.getvalue().splitlines() if line][0])
    assert "index" not in payload["voice"]
    (directory / "model.index").write_bytes(b"idx")
    worker2 = VcWorker(popen=_FakePopen(events), python_path=tmp_path / "python")
    worker2.start(meta, directory, {"input_device": None, "output_device": None,
                                     "monitor_enabled": False, "monitor_device": None},
                   {"output_gain_db": 0.0, "monitor_gain_db": 0.0})
    payload2 = json.loads([line for line in worker2._proc.stdin.getvalue().splitlines() if line][0])
    assert payload2["voice"]["index"].endswith("model.index")


def test_vc_worker_timeout(tmp_path):
    popen = _FakePopen([])
    ticks = iter(range(0, 10000, 100))
    worker = VcWorker(popen=popen, python_path=tmp_path / "python",
                      clock=lambda: next(ticks))
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir()
    (directory / "reference.wav").write_bytes(b"x")
    meta = {"id": "v", "kind": "zeroshot", "pitch_shift": 0, "index_rate": 0.0}
    with pytest.raises(RuntimeError):
        worker.start(meta, directory, {"input_device": None, "output_device": None,
                                       "monitor_enabled": False, "monitor_device": None},
                      {"output_gain_db": 0.0, "monitor_gain_db": 0.0})


def test_vc_worker_error_event(tmp_path):
    events = [{"event": "error", "message": "boom"}]
    popen = _FakePopen(events)
    worker = VcWorker(popen=popen, python_path=tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir()
    (directory / "reference.wav").write_bytes(b"x")
    meta = {"id": "v", "kind": "zeroshot", "pitch_shift": 0, "index_rate": 0.0}
    with pytest.raises(RuntimeError):
        worker.start(meta, directory, {"input_device": None, "output_device": None,
                                       "monitor_enabled": False, "monitor_device": None},
                      {"output_gain_db": 0.0, "monitor_gain_db": 0.0})


def test_vc_worker_stop_idempotent(tmp_path):
    popen = _FakePopen([{"event": "status", "state": "running"}])
    worker = VcWorker(popen=popen, python_path=tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir()
    (directory / "reference.wav").write_bytes(b"x")
    meta = {"id": "v", "kind": "zeroshot", "pitch_shift": 0, "index_rate": 0.0}
    worker.start(meta, directory, {"input_device": None, "output_device": None,
                                    "monitor_enabled": False, "monitor_device": None},
                  {"output_gain_db": 0.0, "monitor_gain_db": 0.0})
    worker.stop()
    worker.stop()
    commands = [line for line in popen.last.stdin.getvalue().splitlines() if line]
    payloads = [json.loads(c) for c in commands]
    cmds = [p["cmd"] for p in payloads]
    assert "stop" in cmds and "quit" in cmds


class _FakeStore:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self._lock = threading.RLock()
        self.voices = {}
        self._counter = 0

    def create(self, name, *, kind, model_path=None, index_path=None, audio_paths=None,
               pitch_shift=0, index_rate=0.0):
        self._counter += 1
        voice_id = f"v{self._counter}"
        d = self.tmp_path / voice_id
        d.mkdir(exist_ok=True)
        if audio_paths:
            for ap in audio_paths:
                (d / Path(ap).name).write_bytes(b"audio")
        if model_path:
            (d / "model.pth").write_bytes(b"pth")
            if index_path:
                (d / "model.index").write_bytes(b"idx")
        meta = {"id": voice_id, "name": name, "kind": kind, "status": "ready",
               "pitch_shift": pitch_shift, "index_rate": index_rate,
               "sample_rate": 24000, "speech_seconds": 0.0, "can_train": True, "hint": ""}
        self.voices[voice_id] = meta
        return meta

    def get(self, voice_id):
        return self.voices[voice_id]

    def _write(self, directory, meta):
        self.voices[meta['id']] = dict(meta)

    def create_pending(self, name, audio_paths):
        meta = self.create(name, kind='zeroshot', audio_paths=audio_paths)
        meta.update(status='processing', stage='convert')
        return meta

    def process_audio(self, voice_id):
        meta = dict(self.get(voice_id))
        meta.update(status='ready')
        meta.pop('stage', None)
        self.voices[voice_id] = meta
        return meta

    def list(self):
        return list(self.voices.values())

    def rename(self, voice_id, name):
        self.voices[voice_id]["name"] = name
        return self.voices[voice_id]

    def delete(self, voice_id):
        self.voices.pop(voice_id, None)
        return {"ok": True}

    def update_params(self, voice_id, *, pitch_shift=None, index_rate=None, block_ms=None, diffusion_steps=None, ref_seconds=None):
        meta = dict(self.voices[voice_id])
        for key, value in (("block_ms", block_ms), ("diffusion_steps", diffusion_steps), ("ref_seconds", ref_seconds)):
            if value is not None:
                meta[key] = value
        self.voices[voice_id] = meta
        if pitch_shift is not None and (isinstance(pitch_shift, bool) or not isinstance(pitch_shift, int)):
            raise ValueError("pitch_shift должен быть целым числом от -24 до 24.")
        if index_rate is not None and (isinstance(index_rate, bool) or not isinstance(index_rate, (int, float))):
            raise ValueError("index_rate должен быть числом от 0.0 до 1.0.")
        if pitch_shift is not None:
            if not -24 <= pitch_shift <= 24:
                raise ValueError("pitch_shift должен быть целым числом от -24 до 24.")
            meta["pitch_shift"] = pitch_shift
        if index_rate is not None:
            if not 0.0 <= index_rate <= 1.0:
                raise ValueError("index_rate должен быть числом от 0.0 до 1.0.")
            meta["index_rate"] = float(index_rate)
        return meta

    def _directory(self, voice_id):
        return self.tmp_path / voice_id


class _FakeTrainer:
    def __init__(self, store, on_done=None):
        self.store = store
        self.on_done = on_done
        self.started = []
        self.cancelled = False
        self._status = None
        self.paused = False
        self.pause_count = 0
        self.signals = []

    def pause(self):
        self.pause_count += 1
        if self.pause_count > 1:
            return True
        self.paused = True
        self.signals.append('SIGSTOP')
        return True

    def resume(self):
        if not self.pause_count:
            return
        self.pause_count -= 1
        if self.pause_count:
            return
        self.paused = False
        self.signals.append('SIGCONT')

    def enqueue(self, voice_id, preset):
        return self.start(voice_id)

    def remove_queued(self, voice_id):
        pass

    def close(self):
        self.cancel()

    def start(self, voice_id):
        self.started.append(voice_id)
        return voice_id

    def cancel(self, voice_id=None):
        if voice_id and (self._status or {}).get('voice_id') != voice_id:
            raise VcTrainError('Another voice is currently training.')
        self.cancelled = True
        self._status = None

    def status(self):
        return self._status


def _make_controller(tmp_path):
    store = _FakeStore(tmp_path)
    trainer = _FakeTrainer(store)
    controller = VcController(store, trainer=trainer, worker=_FakeWorker(), uploads=tmp_path / "uploads")
    return controller, store, trainer


class _FakeWorker:
    def __init__(self):
        self.snap = {"state": "idle", "voice_id": None, "latency_ms": None, "cpu": None,
                     "rss_mb": None, "input_level": None, "output_level": None,
                     "dropped_blocks": 0, "error": None}
        self.start_calls = []
        self.stop_calls = 0
        self.gate = None
        self.fail = None
        self.cancel = threading.Event()
        self.params_calls = []
        self.restarts = []
        self._sample_rate = 40000
        self.inject_calls = []
        self.inject_cancel_calls = 0

    def set_inject_state(self, phrase_id, state, **fields):
        self.snap['inject'] = {'id': phrase_id, 'state': state, **fields}

    def inject(self, phrase_id, path, target='both'):
        self.inject_calls.append((phrase_id, Path(path), target))

    def cancel_inject(self):
        self.inject_cancel_calls += 1

    def request_cancel(self):
        self.cancel.set()

    def restart_stream(self, devices):
        self.restarts.append(devices)

    def start(self, meta, directory, devices, params, *, load_only=False):
        self.cancel.clear()
        self.start_calls.append((meta["id"], devices, params))
        if self.gate is not None:
            deadline = time.monotonic() + 5
            while not self.gate.is_set() and not self.cancel.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            if self.cancel.is_set():
                raise RuntimeError("cancelled")
        if self.fail:
            raise RuntimeError(self.fail)
        self.snap = {"state": "running", "voice_id": meta["id"], "latency_ms": 10, "cpu": None,
                     "rss_mb": 200, "input_level": 0.1, "output_level": 0.2,
                     "dropped_blocks": 0, "error": None}

    def stop(self):
        self.stop_calls += 1
        self.snap = {"state": "idle", "voice_id": None, "latency_ms": None, "cpu": None,
                     "rss_mb": None, "input_level": None, "output_level": None,
                     "dropped_blocks": 0, "error": None}

    def set_params(self, params):
        self.params_calls.append(params)

    def snapshot(self):
        return dict(self.snap)

    def worker_failed(self):
        return self.snap.get("state") == "error"


def test_controller_import_model(tmp_path):
    controller, store, trainer = _make_controller(tmp_path)
    body = _build_multipart({"name": "Imported"},
                             [("file", "model.pth", b"pth"), ("file", "model.index", b"idx")])
    rfile = _rfile_from_bytes(body)
    meta = controller.import_model("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body))
    assert meta["kind"] == "imported"
    assert (tmp_path / "uploads").exists()
    uploads = list((tmp_path / "uploads").iterdir())
    assert uploads == []


def test_controller_import_model_exe(tmp_path):
    controller, _, _ = _make_controller(tmp_path)
    body = _build_multipart({"name": "X"}, [("file", "evil.exe", b"data")])
    rfile = _rfile_from_bytes(body)
    with pytest.raises(ValueError):
        controller.import_model("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body))


def test_controller_import_model_no_name(tmp_path):
    controller, _, _ = _make_controller(tmp_path)
    body = _build_multipart({}, [("file", "model.pth", b"pth")])
    rfile = _rfile_from_bytes(body)
    with pytest.raises(ValueError):
        controller.import_model("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body))


def test_controller_create_voice_train(tmp_path):
    controller, store, trainer = _make_controller(tmp_path)
    body = _build_multipart({"name": "Z", "train": "true"},
                             [("file", "a.wav", b"wav"), ("file", "b.wav", b"wav")])
    rfile = _rfile_from_bytes(body)
    result = controller.create_voice("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body))
    assert result["voice"]["kind"] == "zeroshot"
    controller._audio_queue.join()
    assert trainer.started == [result["voice"]["id"]]
    assert result['voice']['status'] == 'processing'
    assert result['voice']['train_after'] == 'normal'


def test_controller_create_voice_no_train_when_disallowed(tmp_path):
    controller, store, trainer = _make_controller(tmp_path)
    store.voices.clear()
    original_create = store.create

    def create_no_train(name, *, kind, model_path=None, index_path=None, audio_paths=None,
                       pitch_shift=0, index_rate=0.0):
        meta = original_create(name, kind=kind, model_path=model_path, index_path=index_path,
                               audio_paths=audio_paths, pitch_shift=pitch_shift, index_rate=index_rate)
        meta["can_train"] = False
        return meta
    store.create = create_no_train
    body = _build_multipart({"name": "Z", "train": "true"},
                             [("file", "a.wav", b"wav")])
    rfile = _rfile_from_bytes(body)
    result = controller.create_voice("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body))
    controller._audio_queue.join()
    assert trainer.started == []


def test_controller_create_voice_train_error(tmp_path):
    controller, store, trainer = _make_controller(tmp_path)

    def fail_start(voice_id):
        raise VcTrainError("обучение недоступно")
    trainer.start = fail_start
    body = _build_multipart({"name": "Z", "train": "true"},
                             [("file", "a.wav", b"wav")])
    rfile = _rfile_from_bytes(body)
    result = controller.create_voice("multipart/form-data; boundary=X-BOUNDARY", rfile, len(body))
    assert result["voice"]["kind"] == "zeroshot"
    controller._audio_queue.join()
    assert store.get(result['voice']['id'])["train_error"] == "обучение недоступно"


def test_controller_snapshot_and_failure(tmp_path):
    controller, store, trainer = _make_controller(tmp_path)
    snap = controller.snapshot()
    assert "vc" in snap and "training" in snap
    assert snap["vc"]["state"] == "idle"
    controller.worker.snap["state"] = "error"
    controller.worker.snap["error"] = "boom"
    assert controller.worker_failed() is True


def test_desktop_control_vc_start_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="zeroshot")
    control = DesktopControl(catalog, vc=controller)

    async def run():
        await control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                                "input_device": "Fake input", "output_device": "Fake output"})
        snap = control.snapshot()
        assert snap["mode"] == "vc"
        assert snap["active"] is True
        assert control.lease.acquire_calls == 1
        stops_before = controller.worker.stop_calls
        await control.command({"action": "stop"})
        snap = control.snapshot()
        assert snap["active"] is False
        assert controller.worker.stop_calls == stops_before + 1
        assert control.lease.close_calls >= 1
    asyncio.run(run())


def test_desktop_control_vc_no_vc_voice_id(tmp_path):
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, _, _ = _make_controller(tmp_path)
    control = DesktopControl(catalog, vc=controller)

    async def run():
        with pytest.raises(ValueError):
            await control.command({"action": "start", "mode": "vc",
                                   "input_device": None, "output_device": None})
        snap = control.snapshot()
        assert snap["active"] is False
    asyncio.run(run())


def test_desktop_control_no_vc_mode(tmp_path):
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    control = DesktopControl(catalog)

    async def run():
        with pytest.raises(ValueError):
            await control.command({"action": "start", "mode": "vc", "vc_voice_id": "v1",
                                   "input_device": None, "output_device": None})
        snap = control.snapshot()
        assert snap["active"] is False
    asyncio.run(run())


def test_http_vc_voices(tmp_path, monkeypatch):
    from ai_voice import desktop as desktop_mod
    monkeypatch.setattr(desktop_mod, "PreviewPlayer", lambda: type("P", (), {"snapshot": lambda self: {}})())
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, _, _ = _make_controller(tmp_path)
    control = DesktopControl(catalog, vc=controller)

    class _Handler(desktop_mod.DesktopHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.token = "secret-token"
    server.control = control
    server.catalog = catalog
    server.preview = type("P", (), {"snapshot": lambda self: {}})()
    server.vc = controller
    server.loop = asyncio.new_event_loop()
    threading.Thread(target=server.loop.run_forever, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/vc/voices",
                                     headers={"X-AI-Voice-Token": "secret-token",
                                              "Origin": f"http://127.0.0.1:{server.server_port}"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            assert "items" in data and "training" in data
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/vc/voices")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req).read()
    finally:
        server.shutdown()
        server.loop.call_soon_threadsafe(server.loop.stop)


def test_http_vc_rename_bad_type(tmp_path, monkeypatch):
    from ai_voice import desktop as desktop_mod
    monkeypatch.setattr(desktop_mod, "PreviewPlayer", lambda: type("P", (), {"snapshot": lambda self: {}})())
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, _, _ = _make_controller(tmp_path)
    controller.import_model("multipart/form-data; boundary=X-BOUNDARY",
                            *_rfile_split(_build_multipart({"name": "X"},
                                                           [("file", "m.pth", b"pth")]),
                                          "multipart/form-data; boundary=X-BOUNDARY"))
    control = DesktopControl(catalog, vc=controller)

    class _Handler(desktop_mod.DesktopHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.token = "tok"
    server.control = control
    server.catalog = catalog
    server.preview = type("P", (), {"snapshot": lambda self: {}})()
    server.vc = controller
    server.loop = asyncio.new_event_loop()
    threading.Thread(target=server.loop.run_forever, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/vc/rename",
                                     data=json.dumps({"voice_id": 123, "name": "Y"}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-AI-Voice-Token": "tok",
                                              "Origin": f"http://127.0.0.1:{server.server_port}"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req).read()
        assert exc.value.code == 400
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/vc/params",
                                     data=json.dumps({"voice_id": "v1", "pitch_shift": True}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-AI-Voice-Token": "tok",
                                              "Origin": f"http://127.0.0.1:{server.server_port}"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req).read()
        assert exc.value.code == 400
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/vc/params",
                                     data=json.dumps({"voice_id": "v1", "pitch_shift": 99}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-AI-Voice-Token": "tok",
                                              "Origin": f"http://127.0.0.1:{server.server_port}"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req).read()
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.loop.call_soon_threadsafe(server.loop.stop)


def _rfile_split(data, content_type):
    return _rfile_from_bytes(data), len(data)


_DEVICES = {"input_device": "in", "output_device": "out", "monitor_enabled": False, "monitor_device": None}


async def _wait_not_loading(controller):
    for _ in range(300):
        if not controller.loading:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("still loading")


def test_vc_async_start_loading_then_running(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.gate = threading.Event()

    async def run():
        await controller.start_worker(meta["id"], _DEVICES, {})
        assert controller.snapshot()["vc"]["state"] == "loading"
        assert controller.snapshot()["vc"]["voice_id"] == meta["id"]
        controller.worker.gate.set()
        await _wait_not_loading(controller)
        assert controller.snapshot()["vc"]["state"] == "running"
    asyncio.run(run())


def test_vc_stop_during_loading_leaves_no_zombie(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.gate = threading.Event()

    async def run():
        await controller.start_worker(meta["id"], _DEVICES, {})
        task = controller._start_task
        await controller.stop_worker()
        assert task.done()
        assert controller.worker.stop_calls == 1
        assert controller.active_voice is None
        assert controller.snapshot()["vc"]["state"] == "idle"
    asyncio.run(run())


def test_vc_start_error_releases_lease_and_sets_error(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.fail = "модель не загрузилась"
    control = DesktopControl(catalog, vc=controller)

    async def run():
        result = await control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                                        "input_device": "i", "output_device": "o"})
        assert result["ok"] is True and result["vc"]["state"] == "loading"
        await _wait_not_loading(controller)
        snap = control.snapshot()
        assert snap["vc"]["state"] == "error"
        assert snap["vc"]["error"] == "модель не загрузилась"
        assert snap["active"] is False and snap["state"] == "error"
        assert control.lease.close_calls >= 1
        assert controller.active_voice is None
    asyncio.run(run())


def test_crashed_worker_allows_autotune(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.bench = lambda candidates: [dict(candidate, ms=1) for candidate in candidates]

    async def run():
        await controller.start_worker(meta["id"], _DEVICES, {})
        await _wait_not_loading(controller)
        controller.worker.snap = {**controller.worker.snap, "state": "error", "error": "движок упал"}
        await controller.autotune(meta["id"])
        assert controller.active_voice is None

    asyncio.run(run())


def test_crashed_worker_allows_start_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    control = DesktopControl(Catalog(path=str(tmp_path / "catalog.json")), vc=controller)
    command = {"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
               "input_device": "i", "output_device": "o"}

    async def run():
        await control.command(command)
        await _wait_not_loading(controller)
        controller.worker.snap = {**controller.worker.snap, "state": "error", "error": "движок упал"}
        starts = len(controller.worker.start_calls)
        await control.command(command)
        await _wait_not_loading(controller)
        assert len(controller.worker.start_calls) == starts + 1
        await control.command({"action": "stop"})
        await control.command(command)
        await _wait_not_loading(controller)
        controller.worker.snap = {**controller.worker.snap, "state": "error", "error": "движок упал"}
        await controller.delete(meta["id"])

    asyncio.run(run())


def test_reap_failed_keeps_error_visible(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    control = DesktopControl(Catalog(path=str(tmp_path / "catalog.json")), vc=controller)

    async def run():
        await control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                               "input_device": "i", "output_device": "o"})
        await _wait_not_loading(controller)
        controller.worker.snap = {**controller.worker.snap, "state": "error", "error": "движок упал"}
        closed = control.lease.close_calls
        await control.reap_failed_vc()
        assert control.state["active"] is False
        assert control.lease.close_calls == closed + 1
        assert control.snapshot()["state"] == "error"
        assert control.snapshot()["message"] == "движок упал"
        assert controller.snapshot()["vc"]["state"] == "error"

    asyncio.run(run())


def test_http_autotune_after_crash_is_not_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.bench = lambda candidates: [dict(candidate, ms=1) for candidate in candidates]
    server = _http_server(tmp_path, monkeypatch, controller)

    async def start_and_crash():
        await server.control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                                      "input_device": "i", "output_device": "o"})
        await _wait_not_loading(controller)
        controller.worker.snap = {**controller.worker.snap, "state": "error", "error": "движок упал"}

    try:
        asyncio.run(start_and_crash())
        code, _ = _post(server, "/api/vc/autotune", {"voice_id": meta["id"]})
        assert code != 409
    finally:
        controller.close()
        server.shutdown()
        server.server_close()
        server.loop.call_soon_threadsafe(server.loop.stop)


def test_vc_second_start_while_loading_conflicts(tmp_path, monkeypatch):
    from ai_voice.desktop_control import ConflictError
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.gate = threading.Event()
    control = DesktopControl(catalog, vc=controller)
    cmd = {"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
           "input_device": "i", "output_device": "o"}

    async def run():
        await control.command(cmd)
        with pytest.raises(ConflictError, match="The voice is already loading"):
            await control.command(cmd)
        assert controller.loading
        await control.command({"action": "stop"})
        assert not controller.loading
    asyncio.run(run())


def test_vc_delete_during_loading_refused(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.gate = threading.Event()

    async def run():
        await controller.start_worker(meta["id"], _DEVICES, {})
        with pytest.raises(VcStoreError, match='Stop Live voice mode first'):
            await controller.delete(meta["id"])
        assert meta["id"] in store.voices and (tmp_path / meta["id"]).exists()
        controller.rename(meta["id"], "Новое имя")
        assert store.voices[meta["id"]]["name"] == "Новое имя"
        await controller.stop_worker()
    asyncio.run(run())


def test_vc_apply_preferences_updates_running_voice(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    monkeypatch.setattr("ai_voice.desktop_control.list_devices", lambda: [
        {"name": "Speakers", "max_output_channels": 2, "max_input_channels": 0}])
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    catalog.update({"vc_gate_enabled": True, "vc_gate_db": -48.0})
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    control = DesktopControl(catalog, vc=controller)

    async def run():
        await control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                               "input_device": "i", "output_device": "o"})
        await _wait_not_loading(controller)
        assert controller.worker.start_calls[-1][2]["gate_enabled"] is True
        assert controller.worker.start_calls[-1][2]["gate_db"] == -48.0
        await control.apply_preferences({"vc_gate_enabled": False, "vc_gate_db": -40})
        assert controller.worker.params_calls[-1] == {"gate_enabled": False, "gate_db": -40.0}
        await control.apply_preferences({"vc_gate_db": -41})
        assert controller.worker.params_calls[-1] == {"gate_db": -41.0}
        await control.apply_preferences({"output_gain_db": 3.0, "monitor_gain_db": -2.0})
        assert controller.worker.params_calls[-1] == {"output_gain_db": 3.0, "monitor_gain_db": -2.0}
        loads = len(controller.worker.start_calls)
        await control.apply_preferences({"monitor_enabled": True, "monitor_device": "Speakers"})
        assert controller.worker.restarts[-1]["monitor_enabled"] is True
        assert controller.worker.restarts[-1]["monitor_device"] == "Speakers"
        assert len(controller.worker.start_calls) == loads
        await control.command({"action": "stop"})
    asyncio.run(run())


def _http_server(tmp_path, monkeypatch, controller):
    from ai_voice import desktop as desktop_mod
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    control = DesktopControl(catalog, vc=controller)

    class _Handler(desktop_mod.DesktopHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.token = "tok"
    server.control = control
    server.catalog = catalog
    server.preview = type("P", (), {"snapshot": lambda self: {}})()
    server.vc = controller
    server.loop = asyncio.new_event_loop()
    threading.Thread(target=server.loop.run_forever, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(server, path, body=None):
    import urllib.request, urllib.error
    data = None if body is None else json.dumps(body).encode()
    headers = {"X-AI-Voice-Token": "tok", "Origin": f"http://127.0.0.1:{server.server_port}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=data,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_train_cancel_contract(tmp_path, monkeypatch):
    controller, _, trainer = _make_controller(tmp_path)
    trainer._status = {"voice_id": "v1", "state": "running"}
    server = _http_server(tmp_path, monkeypatch, controller)
    try:
        status, _ = _post(server, "/api/vc/train/cancel", {})
        assert status == 200 and trainer.cancelled
        trainer.cancelled = False
        trainer._status = {"voice_id": "v1", "state": "running"}
        status, _ = _post(server, "/api/vc/train/cancel")
        assert status == 200 and trainer.cancelled
        trainer.cancelled = False
        trainer._status = {"voice_id": "v1", "state": "running"}
        status, _ = _post(server, "/api/vc/train/cancel", {"voice_id": "other"})
        assert status == 409 and not trainer.cancelled
        status, _ = _post(server, "/api/vc/train/cancel", {"voice_id": "v1"})
        assert status == 200 and trainer.cancelled
    finally:
        server.shutdown()
        server.loop.call_soon_threadsafe(server.loop.stop)


def _worker_with_events(tmp_path, events, interval=0.02):
    popen = _FakePopen(events)
    worker = VcWorker(popen=popen, python_path=tmp_path / "python", status_interval=interval)
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir(exist_ok=True)
    (directory / "reference.wav").write_bytes(b"x")
    meta = {"id": "v", "kind": "zeroshot", "pitch_shift": 0, "index_rate": 0.0}
    worker.start(meta, directory, _DEVICES, {"output_gain_db": 0.0, "monitor_gain_db": 0.0})
    return worker, popen


def test_vc_worker_levels_update_snapshot_and_poller_stops(tmp_path):
    events = [
        {"event": "status", "state": "loaded", "config": {"hop_ms": 100}},
        {"event": "status", "state": "running", "config": {"hop_ms": 100}},
        {"event": "levels", "input_level": 0.1, "output_level": 0.2, "dropped_blocks": 7, "processing_ms": 40},
    ]
    worker, popen = _worker_with_events(tmp_path, events)
    snap = worker.snapshot()
    assert snap["dropped_blocks"] == 7 and snap["processing_ms"] == 40
    assert snap["latency_ms"] == 240
    time.sleep(0.2)
    cmds = [json.loads(c)["cmd"] for c in popen.last.stdin.getvalue().splitlines() if c]
    assert "status" in cmds
    poller = worker._poll_thread
    assert poller.is_alive()
    worker.stop()
    assert poller.is_alive() is False


def test_vc_worker_restart_stream_keeps_model(tmp_path):
    events = [
        {"event": "status", "state": "loaded"},
        {"event": "status", "state": "running"},
    ]
    worker, popen = _worker_with_events(tmp_path, events, interval=60)

    def emit_stopped():
        time.sleep(0.1)
        worker._handle_event({"event": "stopped", "reason": "requested"})
        time.sleep(0.1)
        worker._handle_event({"event": "status", "state": "running"})
    threading.Thread(target=emit_stopped, daemon=True).start()
    worker.restart_stream({**_DEVICES, "monitor_enabled": True, "monitor_device": "Speakers"})
    cmds = [json.loads(c) for c in popen.last.stdin.getvalue().splitlines() if c]
    names = [c["cmd"] for c in cmds]
    assert names.count("load") == 1
    assert names[-2:] == ["stop", "start"]
    assert cmds[-1]["devices"]["monitor_enabled"] is True
    worker.stop()


def test_worker_start_closes_log_fd(tmp_path):
    events = [{"event": "status", "state": "loaded"}, {"event": "status", "state": "running"}]
    popen = _FakePopen(events)
    seen = {}
    original = popen.__call__

    def capture(*args, **kwargs):
        seen["stderr"] = kwargs["stderr"]
        return original(*args, **kwargs)
    worker = VcWorker(popen=capture, python_path=tmp_path / "python", status_interval=60)
    (tmp_path / "python").write_text("#!/bin/sh\n")
    directory = tmp_path / "voice"
    directory.mkdir()
    (directory / "reference.wav").write_bytes(b"x")
    worker.start({"id": "v", "kind": "zeroshot"}, directory, _DEVICES, {})
    assert seen["stderr"].closed
    worker.stop()


def test_multipart_rejects_too_many_fields(tmp_path):
    fields = {f"f{i}": "x" for i in range(21)}
    body = _build_multipart(fields, [])
    with pytest.raises(ValueError, match='Invalid file upload'):
        parse_multipart("multipart/form-data; boundary=X-BOUNDARY", _rfile_from_bytes(body), len(body), tmp_path)
    body = _build_multipart({"n" * 101: "x"}, [])
    with pytest.raises(ValueError, match='Invalid file upload'):
        parse_multipart("multipart/form-data; boundary=X-BOUNDARY", _rfile_from_bytes(body), len(body), tmp_path)
    fields = {f"f{i}": "x" for i in range(20)}
    body = _build_multipart(fields, [])
    parsed, _ = parse_multipart("multipart/form-data; boundary=X-BOUNDARY", _rfile_from_bytes(body), len(body), tmp_path)
    assert len(parsed) == 20


def test_upload_stall_times_out_and_cleans(tmp_path, monkeypatch):
    import socket
    from ai_voice import desktop as desktop_mod
    controller, _, _ = _make_controller(tmp_path)
    monkeypatch.setattr(desktop_mod.DesktopHandler, "upload_timeout", 0.3)
    server = _http_server(tmp_path, monkeypatch, controller)
    try:
        body = _build_multipart({"name": "Stalled"}, [("files", "m.pth", b"x" * 1000)])
        head = (f"POST /api/vc/import HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                f"X-AI-Voice-Token: tok\r\nContent-Type: multipart/form-data; boundary=X-BOUNDARY\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        sock = socket.create_connection(("127.0.0.1", server.server_port))
        sock.settimeout(5)
        sock.sendall(head + body[:len(body) // 2])
        reply = sock.recv(4096)
        sock.close()
        assert reply.startswith(b"HTTP/1.0 400")
        deadline = time.monotonic() + 2
        while any((tmp_path / "uploads").iterdir()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any((tmp_path / "uploads").iterdir())
    finally:
        server.shutdown()
        server.server_close()
        server.loop.call_soon_threadsafe(server.loop.stop)
        deadline = time.monotonic() + 2
        while server.loop.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        server.loop.close()


def test_preferences_after_vc_crash_release_lease(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    control = DesktopControl(catalog, vc=controller)

    async def run():
        await control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                               "input_device": "i", "output_device": "o"})
        await _wait_not_loading(controller)
        controller.worker.snap = {**controller.worker.snap, "state": "error", "error": "движок упал"}
        assert control.snapshot()["active"] is False
        assert controller.trainer.signals[-2:] == ['SIGSTOP', 'SIGCONT']
        closes = control.lease.close_calls
        await control.apply_preferences({"input_device": "other"})
        assert catalog.preferences()["input_device"] == "other"
        assert control.lease.close_calls == closes + 1
        assert control.state["active"] is False
        snap = control.snapshot()
        assert snap["state"] == "error" and snap["message"] == "движок упал"
    asyncio.run(run())


def test_monitor_change_during_loading_applied_after_load(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    monkeypatch.setattr("ai_voice.desktop_control.list_devices", lambda: [
        {"name": "Speakers", "max_output_channels": 2, "max_input_channels": 0}])
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    controller.worker.gate = threading.Event()
    control = DesktopControl(catalog, vc=controller)

    async def run():
        await control.command({"action": "start", "mode": "vc", "vc_voice_id": meta["id"],
                               "input_device": "i", "output_device": "o"})
        await control.apply_preferences({"monitor_enabled": True, "monitor_device": "Speakers",
                                         "output_gain_db": 4.0})
        assert controller.worker.restarts == []
        controller.worker.gate.set()
        await _wait_not_loading(controller)
        await control._vc_resync_task
        assert controller.worker.restarts[-1]["monitor_enabled"] is True
        assert controller.worker.restarts[-1]["monitor_device"] == "Speakers"
        assert controller.worker.params_calls[-1]["output_gain_db"] == 4.0
        await control.command({"action": "stop"})
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['zeroshot', 'imported'])
@pytest.mark.parametrize('fits', [True, False])
def test_autotune_selects_and_saves_first_candidate(tmp_path, kind, fits):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create('Voice', kind=kind)
    candidates_seen = []
    def bench(candidates):
        candidates_seen.extend(candidates)
        return [{**c, 'ms': c['block_ms'] * (.5 if fits and i == 2 else .8)}
                for i, c in enumerate(candidates)]
    controller.worker.bench = bench
    result = asyncio.run(controller.autotune(meta['id']))
    chosen = candidates_seen[2] if fits else candidates_seen[-1]
    assert all(result['chosen'][key] == value for key, value in chosen.items())
    assert all(store.get(meta['id'])[key] == value for key, value in chosen.items())
    assert bool(result['warning']) == (not fits)
    assert controller.worker.stop_calls == 1
    assert controller.active_voice is None
    if kind == 'zeroshot':
        assert candidates_seen[0] == dict(ref_seconds=10, diffusion_steps=6, block_ms=256)
    else:
        assert [c['block_ms'] for c in candidates_seen] == [160, 256, 500, 1000]


@pytest.mark.parametrize('blocked', ['active', 'loading'])
def test_autotune_conflicts(tmp_path, blocked):
    from ai_voice.desktop_control import ConflictError
    controller, store, trainer = _make_controller(tmp_path)
    meta = store.create('Voice', kind='zeroshot')
    async def run():
        if blocked == 'active':
            controller.active_voice = meta['id']
        elif blocked == 'loading':
            controller._start_task = asyncio.create_task(asyncio.sleep(1))
        else:
            trainer._status = {'state': 'running'}
        try:
            with pytest.raises(ConflictError, match='Stop Live voice'):
                await controller.autotune(meta['id'])
            assert not controller.worker.start_calls
        finally:
            if controller._start_task:
                controller._start_task.cancel()
                await asyncio.gather(controller._start_task, return_exceptions=True)
    asyncio.run(run())


def test_autotune_failure_unloads(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create('Voice', kind='zeroshot')
    controller.worker.bench = lambda candidates: (_ for _ in ()).throw(RuntimeError('bench failed'))
    with pytest.raises(RuntimeError, match='bench failed'):
        asyncio.run(controller.autotune(meta['id']))
    assert controller.worker.stop_calls == 1


def test_live_params_and_restart_required(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create('Voice', kind='zeroshot')
    meta.update(block_ms=256, ref_seconds=5, diffusion_steps=4)
    controller.active_voice = meta['id']
    result = controller.set_params(meta['id'], diffusion_steps=6)
    assert controller.worker.params_calls == [{'diffusion_steps': 6}]
    assert 'restart_required' not in result
    result = controller.set_params(meta['id'], block_ms=500, ref_seconds=10)
    assert result['restart_required'] is True
    assert 'restart_required' not in store.get(meta['id'])
    assert controller.worker.params_calls == [{'diffusion_steps': 6}]


def test_vc_runtime_availability(tmp_path):
    controller, _, _ = _make_controller(tmp_path)
    runtime = tmp_path / 'python'
    controller.worker._python_path = runtime
    assert controller.voices()['runtime_ok'] is False
    runtime.write_text('#!/bin/sh')
    assert controller.voices()['runtime_ok'] is True


def test_autotune_timeout_unloads(tmp_path, monkeypatch):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create('Voice', kind='zeroshot')
    controller.worker.bench = lambda candidates: []
    async def expire(task, timeout):
        assert timeout == 90
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()
    monkeypatch.setattr(asyncio, 'wait_for', expire)
    with pytest.raises(VcStoreError, match='Automatic tuning did not finish'):
        asyncio.run(controller.autotune(meta['id']))
    assert controller.worker.stop_calls == 1


def test_create_returns_before_processing_and_serializes_uploads(tmp_path, monkeypatch):
    store = VcStore(tmp_path / 'voices')
    entered, release = threading.Event(), threading.Event()
    running, maximum = [0], [0]
    def prepare(directory, paths, before_slice=None):
        running[0] += 1
        maximum[0] = max(maximum[0], running[0])
        assert paths[0].read_bytes() == b'audio'
        entered.set()
        assert release.wait(3)
        before_slice()
        running[0] -= 1
        return dict(speech_seconds=180, can_train=True, hint='')
    monkeypatch.setattr(store, '_audio', prepare)
    fake = _FakeTrainer(store)
    controller = VcController(store, trainer=fake, worker=_FakeWorker(), uploads=tmp_path / 'uploads')
    try:
        results = []
        for name in ('First', 'Second'):
            body = _build_multipart({'name': name, 'train': 'fast'}, [('files', 'audio.wav', b'audio')])
            results.append(controller.create_voice('multipart/form-data; boundary=X-BOUNDARY', io.BytesIO(body), len(body)))
        assert entered.wait(1)
        assert all(r['voice']['status'] == 'processing' for r in results)
        assert len(controller.voices()['items']) == 2
        assert fake.started == []
        assert not list(controller.uploads.iterdir())
        release.set()
        controller._audio_queue.join()
        assert maximum == [1]
        assert fake.started == [r['voice']['id'] for r in results]
        assert all(store.get(r['voice']['id'])['status'] == 'ready' for r in results)
    finally:
        release.set()
        controller.close()


@pytest.mark.parametrize('failed', [False, True])
def test_autotune_pauses_and_resumes_training(tmp_path, failed):
    controller, store, trainer = _make_controller(tmp_path)
    trainer._status = {'state': 'running'}
    meta = store.create('Voice', kind='zeroshot')
    def bench(candidates):
        assert trainer.paused
        if failed:
            raise RuntimeError('bench failure')
        return [dict(c, ms=1) for c in candidates]
    controller.worker.bench = bench
    if failed:
        with pytest.raises(RuntimeError):
            asyncio.run(controller.autotune(meta['id']))
    else:
        asyncio.run(controller.autotune(meta['id']))
    assert trainer.signals == ['SIGSTOP', 'SIGCONT']
    assert not trainer.paused


def test_vc_training_pause_start_stop_and_load_failure(tmp_path, monkeypatch):
    monkeypatch.setattr('ai_voice.desktop_control.resolve_output', lambda name: 0)
    controller, store, trainer = _make_controller(tmp_path)
    voice = store.create('Voice', kind='zeroshot')
    control = DesktopControl(Catalog(path=str(tmp_path / 'catalog.json')), vc=controller)
    async def run():
        await control.start('vc', None, 'MIC', 'AI Voice', voice['id'])
        await controller._start_task
        assert trainer.signals == ['SIGSTOP']
        await control.stop()
        assert trainer.signals == ['SIGSTOP', 'SIGCONT']
        def fail(*args):
            raise RuntimeError('load failed')
        controller.worker.start = fail
        await control.start('vc', None, 'MIC', 'AI Voice', voice['id'])
        await controller._start_task
        assert trainer.signals[-2:] == ['SIGSTOP', 'SIGCONT']
        await control.stop()
    asyncio.run(run())


def test_autotune_and_vc_hold_independent_training_pauses(tmp_path, monkeypatch):
    from ai_voice.vc_train import VcTrainer
    monkeypatch.setattr('ai_voice.desktop_control.resolve_output', lambda name: 0)
    store = VcStore(tmp_path / 'voices')
    directory = store._directory('voice')
    directory.mkdir(parents=True)
    store._write(directory, dict(id='voice', name='Voice', kind='zeroshot', status='ready',
                                pitch_shift=0, index_rate=0.0))
    trainer = VcTrainer(store)
    controller = VcController(store, trainer=trainer, worker=_FakeWorker(), uploads=tmp_path / 'uploads')
    control = DesktopControl(Catalog(path=str(tmp_path / 'catalog.json')), vc=controller)
    measuring, release = threading.Event(), threading.Event()
    # Force the ownership overlap at the initial idle-stop boundary. Real worker
    # loading still waits on the autotune lifecycle lock; no audio/ML is started.
    stop = control.stop
    async def stop_idle_session():
        if controller.active_voice is not None:
            await stop()
    monkeypatch.setattr(control, 'stop', stop_idle_session)
    def bench(candidates):
        measuring.set()
        assert release.wait(3)
        return [dict(c, ms=1) for c in candidates]
    controller.worker.bench = bench
    async def run():
        task = asyncio.create_task(controller.autotune('voice'))
        try:
            assert await asyncio.to_thread(measuring.wait, 2)
            await control.start('vc', None, 'MIC', 'AI Voice', 'voice')
            assert control._vc_training_paused
            release.set()
            await task
            await controller._start_task
            assert trainer.status()['state'] == 'paused'
            assert not trainer._resumed.is_set()
            await control.stop()
            assert trainer.status() is None
            assert trainer._resumed.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await stop()
    try:
        asyncio.run(run())
    finally:
        controller.close()


@pytest.mark.parametrize('failure', ['conflict', 'unexpected', 'processing', 'write'])
def test_audio_queue_survives_exceptions_and_processes_next_voice(tmp_path, failure):
    controller, store, trainer = _make_controller(tmp_path)
    first = store.create('First', kind='zeroshot')
    second = store.create('Second', kind='zeroshot')
    for meta in (first, second):
        meta.update(status='processing', train_after='fast')
    enqueue = trainer.enqueue
    process = store.process_audio
    write = store._write
    def fail_enqueue(voice_id, preset):
        if voice_id == first['id']:
            if failure == 'conflict':
                raise VcTrainError('The voice is already queued or training.')
            raise RuntimeError('unexpected training error')
        return enqueue(voice_id, preset)
    def fail_process(voice_id):
        if voice_id == first['id'] and failure == 'processing':
            raise RuntimeError('unexpected processing error')
        return process(voice_id)
    def fail_write(directory, meta):
        if meta['id'] == first['id'] and failure == 'write':
            raise RuntimeError('unexpected persistence error')
        return write(directory, meta)
    trainer.enqueue = fail_enqueue
    store.process_audio = fail_process
    store._write = fail_write
    try:
        for meta in (first, second):
            controller._audio_queue.put(meta['id'])
        deadline = time.monotonic() + 2
        while second['id'] not in trainer.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert trainer.started == [second['id']]
        controller._audio_queue.join()
        assert controller._audio_thread.is_alive()
        assert store.get(second['id'])['status'] == 'ready'
        if failure != 'write':
            expected = ('The voice is already queued or training.' if failure == 'conflict'
                        else f'unexpected {"processing" if failure == "processing" else "training"} error')
            assert store.get(first['id'])['train_error'] == expected
    finally:
        controller.close()


def test_delete_removes_queued_voice_before_store_delete(tmp_path):
    from ai_voice.vc_train import VcTrainer
    store = VcStore(tmp_path / 'voices')
    directory = store._directory('voice')
    (directory / 'dataset').mkdir(parents=True)
    store._write(directory, dict(id='voice', name='Voice', kind='zeroshot', status='ready',
                                can_train=True, speech_seconds=180))
    trainer = VcTrainer(store)
    trainer.pause()
    trainer.enqueue('voice', 'fast')
    controller = VcController(store, trainer=trainer, worker=_FakeWorker(), uploads=tmp_path / 'uploads')
    delete = store.delete
    def finish_current_at_delete(voice_id):
        delete(voice_id)
        trainer.resume()  # Deterministically exercise the review's race window.
    store.delete = finish_current_at_delete
    try:
        assert asyncio.run(controller.delete('voice')) == {'ok': True}
        assert trainer.status() is None
        assert controller.last_done is None
        assert not directory.exists()
        assert json.loads(trainer.queue_path.read_text())['queue'] == []
    finally:
        controller.close()


def test_http_training_queue_add_move_cancel_delete(tmp_path, monkeypatch):
    from ai_voice.vc_train import VcTrainer
    store = VcStore(tmp_path / 'voices')
    for voice_id in ('one', 'two', 'three'):
        directory = store._directory(voice_id)
        (directory / 'dataset').mkdir(parents=True)
        store._write(directory, dict(id=voice_id, name=voice_id, kind='zeroshot', status='ready',
                                    can_train=True, speech_seconds=180))
    trainer = VcTrainer(store)
    trainer.pause()  # All requests operate on queued jobs; no ML process.
    controller = VcController(store, trainer=trainer, worker=_FakeWorker(), uploads=tmp_path / 'uploads')
    server = _http_server(tmp_path, monkeypatch, controller)
    try:
        for voice_id, preset in [('one', 'fast'), ('two', 'normal'), ('three', 'max')]:
            code, data = _post(server, '/api/vc/train', dict(voice_id=voice_id, preset=preset))
            assert code == 200
        assert [e['epochs'] for e in data['queue']] == [50, 100, 200]
        assert data['queue'][0]['eta_min'] == 8
        assert _post(server, '/api/vc/train', dict(voice_id='one', preset='fast'))[0] == 409
        assert _post(server, '/api/vc/train', dict(voice_id='one', preset='invalid'))[0] == 400
        code, data = _post(server, '/api/vc/train/move', dict(voice_id='three', direction='up'))
        assert code == 200 and [e['voice_id'] for e in data['queue']] == ['one', 'three', 'two']
        code, data = _post(server, '/api/vc/train/cancel', dict(voice_id='three'))
        assert code == 200 and [e['voice_id'] for e in data['queue']] == ['one', 'two']
        assert _post(server, '/api/vc/delete', dict(voice_id='two'))[0] == 200
        saved = json.loads(trainer.queue_path.read_text())
        assert saved['queue'] == [dict(voice_id='one', epochs=50)]
    finally:
        controller.close()
        server.shutdown()
        server.server_close()
        server.loop.call_soon_threadsafe(server.loop.stop)


def _swap_control(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_voice.desktop_control.resolve_output", lambda name: 0)
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create("VC", kind="imported", model_path="m")
    return DesktopControl(catalog, vc=controller), controller, catalog, meta


_START = {"action": "start", "mode": "vc", "input_device": "i", "output_device": "o"}


def test_vc_bypass_conflict_without_running_vc(tmp_path, monkeypatch):
    control, controller, _, _ = _swap_control(tmp_path, monkeypatch)

    async def run():
        with pytest.raises(ConflictError, match="Live voice is not running"):
            await control.command({"action": "vc_bypass", "bypass": True})
        assert controller.worker.params_calls == []
    asyncio.run(run())


def test_vc_bypass_validates_and_forwards(tmp_path, monkeypatch):
    control, controller, _, meta = _swap_control(tmp_path, monkeypatch)

    async def run():
        await control.command({**_START, "vc_voice_id": meta["id"]})
        await _wait_not_loading(controller)
        with pytest.raises(ValueError):
            await control.command({"action": "vc_bypass", "bypass": "yes"})
        snap = await control.command({"action": "vc_bypass", "bypass": True})
        assert controller.worker.params_calls[-1] == {"bypass": True}
        assert snap["vc"]["bypass"] is True
        await control.command({"action": "stop"})
    asyncio.run(run())


def test_vc_initial_bypass_follows_swap_enabled(tmp_path, monkeypatch):
    control, controller, catalog, meta = _swap_control(tmp_path, monkeypatch)

    async def run():
        for enabled in (False, True):
            catalog.update({"swap_enabled": enabled})
            await control.command({**_START, "vc_voice_id": meta["id"]})
            await _wait_not_loading(controller)
            assert controller.worker.start_calls[-1][2]["bypass"] is enabled
            vc = control.snapshot()["vc"]
            assert vc["bypass"] is enabled and vc["swap_enabled"] is enabled
            await control.command({"action": "stop"})
    asyncio.run(run())


def test_vc_bypass_requested_while_loading_is_applied_after_load(tmp_path, monkeypatch):
    control, controller, catalog, meta = _swap_control(tmp_path, monkeypatch)
    controller.worker.gate = threading.Event()

    async def run():
        catalog.update({"swap_enabled": True})
        await control.command({**_START, "vc_voice_id": meta["id"]})
        controller.set_bypass(False)
        assert controller.worker.params_calls == []
        controller.worker.gate.set()
        await _wait_not_loading(controller)
        assert {"bypass": False} in controller.worker.params_calls
        await control.command({"action": "stop"})
    asyncio.run(run())


def test_swap_preferences_defaults_and_validation(tmp_path):
    catalog = Catalog(path=str(tmp_path / "catalog.json"))
    prefs = catalog.preferences()
    assert prefs["swap_enabled"] is False and prefs["swap_mode"] == "hold"
    assert prefs["swap_hotkey"] == {"key_code": 1, "modifiers": 2304, "label": "⌥⌘S"}
    hotkey = {"key_code": 49, "modifiers": 4096, "label": "⌃Space"}
    prefs = catalog.update({"swap_enabled": True, "swap_mode": "toggle", "swap_hotkey": hotkey})
    assert (prefs["swap_enabled"], prefs["swap_mode"], prefs["swap_hotkey"]) == (True, "toggle", hotkey)
    assert Catalog(path=str(tmp_path / "catalog.json")).preferences()["swap_hotkey"] == hotkey
    bad = [{"swap_enabled": "yes"}, {"swap_mode": "press"},
           {"swap_hotkey": "S"}, {"swap_hotkey": {"key_code": 1, "modifiers": 2304}},
           {"swap_hotkey": {"key_code": True, "modifiers": 2304, "label": "x"}},
           {"swap_hotkey": {"key_code": 200, "modifiers": 2304, "label": "x"}},
           {"swap_hotkey": {"key_code": 1, "modifiers": 1, "label": "x"}},
           {"swap_hotkey": {"key_code": 1, "modifiers": 2304, "label": ""}}]
    for patch in bad:
        with pytest.raises(ValueError):
            catalog.update(patch)
    assert catalog.preferences()["swap_hotkey"] == hotkey


def test_live_zeroshot_pitch_requires_restart_without_live_params(tmp_path):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create('Voice', kind='zeroshot')
    controller.active_voice = meta['id']
    result = controller.set_params(meta['id'], pitch_shift=3)
    assert result['restart_required'] is True
    assert controller.worker.params_calls == []
    assert store.get(meta['id'])['pitch_shift'] == 3
    assert 'restart_required' not in controller.set_params(meta['id'], pitch_shift=3)


@pytest.mark.parametrize('kind', ['trained', 'imported'])
def test_live_rvc_pitch_and_index_forwarded(tmp_path, kind):
    controller, store, _ = _make_controller(tmp_path)
    meta = store.create('Voice', kind=kind)
    controller.active_voice = meta['id']
    result = controller.set_params(meta['id'], pitch_shift=3, index_rate=0.5)
    assert controller.worker.params_calls == [{'pitch_shift': 3, 'index_rate': 0.5}]
    assert 'restart_required' not in result


@pytest.fixture
def hf_api(tmp_path, monkeypatch):
    """Real handler/store/controller with only checkpoint validation and HF mocked."""
    from types import SimpleNamespace
    import httpx
    from ai_voice.desktop import DesktopHandler
    from ai_voice.hf_catalog import HfCatalog

    store = VcStore(tmp_path / 'hf-voices')
    monkeypatch.setattr(store, '_run', lambda *args, **kwargs: None)
    state = {'offline': False, 'zip': False, 'stream': None, 'requests': []}

    def transport(request):
        state['requests'].append(request)
        if state['offline']:
            raise httpx.ConnectError('offline', request=request)
        if request.url.path == '/api/models':
            return httpx.Response(200, json=[dict(id='test/voice', sha='a' * 40)])
        if '/tree/main' in request.url.path:
            files = ([dict(path='nested/voice.zip', size=12)] if state['zip'] else
                     [dict(path='nested/voice.pth', size=6), dict(path='nested/voice.index', size=2)])
            return httpx.Response(200, json=files)
        assert '/resolve/' + 'a' * 40 + '/nested/voice.' in request.url.path
        assert request.headers['User-Agent'] == 'AI-Voice/1.0'
        if state['stream']:
            return httpx.Response(200, stream=state['stream']())
        if state['zip']:
            import zipfile
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('model.pth', b'fake weights')
                archive.writestr('model.index', b'index')
            return httpx.Response(200, content=output.getvalue())
        return httpx.Response(200, content=b'index' if request.url.path.endswith('.index') else b'fake weights')

    client = httpx.Client(transport=httpx.MockTransport(transport))
    controller = VcController(store, trainer=_FakeTrainer(store), worker=_FakeWorker(),
                              uploads=tmp_path / 'hf-uploads', hf_catalog=HfCatalog(client=client),
                              download_client=client)
    handler = object.__new__(DesktopHandler)
    handler.server = SimpleNamespace(vc=controller, server_port=12345, token='test')
    handler.headers = {'Host': '127.0.0.1:12345', 'X-AI-Voice-Token': 'test'}
    handler.respond = lambda status, body: (status, body)

    def request(path, body=None):
        handler.path = path
        if body is None:
            return handler.do_GET()
        data = json.dumps(body).encode()
        handler.rfile = io.BytesIO(data)
        handler.headers.update({'Content-Length': str(len(data)), 'Content-Type': 'application/json'})
        return handler.do_POST()

    yield request, controller, store, state
    controller.close()
    if controller._download_thread:
        controller._download_thread.join(timeout=5)
        assert not controller._download_thread.is_alive()
    client.close()


def _hf_card(request):
    code, result = request('/api/vc/hf/search?q=voice&sort=downloads&lang=all&page=1')
    assert code == 200
    assert result['page'] == 1 and result['has_more'] is False
    return result['items'][0]


def _hf_finished(controller):
    controller._download_thread.join(timeout=5)
    assert not controller._download_thread.is_alive()
    assert list(controller.uploads.iterdir()) == []
    return controller.catalog_download_status()


def test_hf_search_http_success_network_error_and_validation(hf_api):
    request, _, _, state = hf_api
    card = _hf_card(request)
    assert card['revision'] == 'a' * 40
    for query in ('sort=nonsense', 'lang=xx', 'page=0', 'page=abc'):
        assert request('/api/vc/hf/search?' + query)[0] == 400
    state['offline'] = True
    assert request('/api/vc/hf/search?q=offline') == (503, {'error': 'No connection to Hugging Face'})
    # Same cached request survives a network outage (one process instance).
    assert _hf_card(request) == card


@pytest.mark.parametrize('zip_model', [False, True])
def test_hf_download_import_revision_source_and_duplicate(hf_api, zip_model):
    request, controller, store, state = hf_api
    state['zip'] = zip_model
    card = _hf_card(request)
    assert request('/api/vc/catalog/download', {'id': 'unknown'})[0] == 404
    assert request('/api/vc/catalog/download', {'id': card['id']})[0] == 200
    result = _hf_finished(controller)
    assert result['state'] == 'done'
    voice, = request('/api/vc/voices')[1]['items']
    assert voice['name'] == card['title'] and voice['kind'] == 'imported'
    assert voice['source'] == dict(kind='hf', repo='test/voice', revision='a' * 40,
                                   pth='nested/voice.zip' if zip_model else 'nested/voice.pth',
                                   index=None if zip_model else 'nested/voice.index')
    assert store.get(voice['id'])['source'] == voice['source']
    assert (store.root / voice['id'] / 'model.pth').read_bytes() == b'fake weights'
    assert (store.root / voice['id'] / 'model.index').read_bytes() == b'index'
    assert request('/api/vc/catalog/download')[1] == result
    assert request('/api/vc/catalog/download', {'id': card['id']}) == (409, {'error': 'Already in My voices'})


def test_hf_download_concurrent_conflict_progress_and_cancel(hf_api):
    import httpx
    request, controller, store, state = hf_api
    started, release = threading.Event(), threading.Event()

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x' * 65536
            started.set()
            assert release.wait(3)
            yield b'more'

    state['stream'] = Stream
    card = _hf_card(request)
    try:
        assert request('/api/vc/catalog/download', {'id': card['id']})[0] == 200
        assert started.wait(3)
        progress = request('/api/vc/catalog/download')[1]
        assert progress['state'] == 'downloading' and progress['bytes'] == 65536
        assert request('/api/vc/catalog/download', {'id': card['id']}) == (409, {'error': 'Already downloading'})
        assert request('/api/vc/catalog/download/cancel', {})[0] == 200
    finally:
        release.set()
    result = _hf_finished(controller)
    assert result['state'] == 'error' and result['error'] == 'Cancelled'
    assert store.list() == []


def test_hf_download_enforces_actual_combined_bytes(hf_api):
    request, controller, store, _ = hf_api
    # Both files alone fit; metadata total fits; actual combined bytes do not.
    store.max_import_bytes = 16
    card = _hf_card(request)
    assert request('/api/vc/catalog/download', {'id': card['id']})[0] == 200
    result = _hf_finished(controller)
    assert result['state'] == 'error' and 'limit' in result['error']
    assert store.list() == []


def test_hf_download_network_failure_cleans_temporary_files(hf_api):
    request, controller, store, state = hf_api
    card = _hf_card(request)
    state['offline'] = True
    request('/api/vc/catalog/download', {'id': card['id']})
    result = _hf_finished(controller)
    assert result['state'] == 'error' and result['error'] == 'No connection to Hugging Face'
    assert store.list() == []


def test_hf_cancel_during_import_removes_created_voice(hf_api, monkeypatch):
    request, controller, store, _ = hf_api
    started, release = threading.Event(), threading.Event()
    original = store.create

    def slow_create(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'create', slow_create)
    card = _hf_card(request)
    try:
        request('/api/vc/catalog/download', {'id': card['id']})
        assert started.wait(3)
        assert request('/api/vc/catalog/download')[1]['state'] == 'importing'
        request('/api/vc/catalog/download/cancel', {})
    finally:
        release.set()
    result = _hf_finished(controller)
    assert result['state'] == 'error' and result['error'] == 'Cancelled'
    assert store.list() == []


def test_gate_preferences_validation_persistence_and_bad_stored_values(tmp_path):
    path = tmp_path / 'gate-prefs.json'
    catalog = Catalog(path=str(path))
    assert catalog.preferences()['vc_gate_enabled'] is False
    assert catalog.preferences()['vc_gate_db'] == -45.0
    catalog.update({'vc_gate_enabled': True})
    assert catalog.preferences()['vc_gate_db'] == -45.0
    for value in (-70, -10):
        catalog.update({'vc_gate_db': value})
        assert catalog.preferences()['vc_gate_enabled'] is True
    for value in (True, '-45', -71, -9, float('nan'), float('inf')):
        with pytest.raises(ValueError, match='vc_gate_db must be between -70 and -10 dB'):
            catalog.update({'vc_gate_db': value})
    for value in (1, None, 'true'):
        with pytest.raises(ValueError):
            catalog.update({'vc_gate_enabled': value})
    assert Catalog(path=str(path)).preferences()['vc_gate_db'] == -10.0
    stored = json.loads(path.read_text())
    stored.update(vc_gate_enabled='yes', vc_gate_db=-100, swap_enabled=True)
    path.write_text(json.dumps(stored))
    prefs = Catalog(path=str(path)).preferences()
    assert prefs['vc_gate_enabled'] is False and prefs['vc_gate_db'] == -45.0
    assert prefs['swap_enabled'] is True


def _meter_controller(tmp_path):
    from ai_voice.vc_meter import InputMeter
    now = [0.0]
    class Meter(InputMeter):
        def __init__(self):
            super().__init__(clock=lambda: now[0])
            self.starts, self.stops = [], 0
        def start(self, device):
            self.starts.append(device)
            self._stream = True
        def stop(self):
            self.stops += 1
            self._stream = None
    meter = Meter()
    store = _FakeStore(tmp_path)
    controller = VcController(store, trainer=_FakeTrainer(store), worker=_FakeWorker(),
                              uploads=tmp_path / 'uploads', meter=meter, clock=lambda: now[0])
    return controller, meter, now


def test_meter_heartbeat_timeout_and_worker_source(tmp_path):
    controller, meter, now = _meter_controller(tmp_path)
    try:
        assert controller.meter_heartbeat(True, 'mic') == {'source': 'meter'}
        meter.record(-50)
        snap = controller.snapshot()['vc']
        assert snap['meter'] is True and snap['input_db'] == -50 and snap['gate_open'] is None
        now[0] = 4.9
        controller.snapshot()
        assert meter.running
        controller.meter_heartbeat(True, 'mic')
        now[0] = 9.8
        controller.snapshot()
        assert meter.running
        now[0] = 10.0
        assert controller.snapshot()['vc']['meter'] is False and not meter.running
        starts = len(meter.starts)
        controller.active_voice = 'voice'
        assert controller.meter_heartbeat(True, 'mic') == {'source': 'worker'}
        assert len(meter.starts) == starts and not meter.running
        assert controller.snapshot()['vc']['meter'] is False
        controller.active_voice = None
        controller.meter_heartbeat(True, 'mic')
        controller.meter_heartbeat(False, 'mic')
        assert not meter.running
    finally:
        controller.close()


def test_start_stops_meter_and_loading_heartbeat_uses_worker(tmp_path):
    controller, meter, now = _meter_controller(tmp_path)
    meta = controller.store.create('VC', kind='imported', model_path='m')
    controller.worker.gate = threading.Event()
    async def run():
        controller.meter_heartbeat(True, 'mic')
        assert meter.running
        await controller.start_worker(meta['id'], _DEVICES, {})
        assert controller.loading and not meter.running
        starts = len(meter.starts)
        assert controller.meter_heartbeat(True, 'mic') == {'source':'worker'}
        assert len(meter.starts) == starts and not meter.running
        await controller.stop_worker()
    try:
        asyncio.run(run())
    finally:
        controller.close()


@pytest.mark.parametrize('values,expected', [([-62]*10+[-50], (-56.0, -50)),
    ([-120]*10, (-120.0, -70)), ([-11]*10, (-11.0, -10))])
@pytest.mark.parametrize('worker_running', [False, True])
def test_gate_calibration_percentile_limits_and_worker(tmp_path, monkeypatch, values, expected, worker_running):
    controller, meter, now = _meter_controller(tmp_path)
    controller.active_voice = 'voice' if worker_running else None
    def sleep(delay):
        now[0] += delay
        if not meter.samples_since(0):
            for value in values:
                meter.record(value)
    monkeypatch.setattr('ai_voice.vc_control.time.sleep', sleep)
    try:
        assert controller.calibrate_gate('mic') == {'noise_db': expected[0], 'gate_db': expected[1]}
        assert bool(meter.starts) is not worker_running
        assert now[0] >= 3.0
    finally:
        controller.close()


def test_gate_calibration_missing_signal_busy_and_closed(tmp_path, monkeypatch):
    controller, meter, now = _meter_controller(tmp_path)
    monkeypatch.setattr('ai_voice.vc_control.time.sleep', lambda delay: now.__setitem__(0, now[0]+delay))
    try:
        meter.record(-50)  # fewer than five samples
        with pytest.raises(RuntimeError, match='No microphone signal'):
            controller.calibrate_gate('mic')
        controller._calibration_lock.acquire()
        try:
            with pytest.raises(RuntimeError, match='Calibration is already running'):
                controller.calibrate_gate('mic')
        finally:
            controller._calibration_lock.release()
        controller.close()
        with pytest.raises(RuntimeError, match='Controller is closed'):
            controller.calibrate_gate('mic')
    finally:
        controller.close()


def test_worker_levels_record_meter_and_reject_invalid_fields():
    from ai_voice.vc_meter import InputMeter
    worker, meter = VcWorker(), InputMeter(clock=lambda: 0)
    worker.meter = meter
    worker._handle_event({'event':'levels', 'input_db':-52, 'gate_open':False})
    assert meter.samples_since(0) == [-52.0]
    assert worker.snapshot()['gate_open'] is False
    for invalid in (True, float('inf'), float('nan'), 'bad'):
        worker._handle_event({'event':'levels', 'input_db':invalid, 'gate_open':'bad'})
    assert worker.snapshot()['input_db'] == -52 and worker.snapshot()['gate_open'] is False
    assert meter.samples_since(0) == [-52.0]


def test_status_during_vc_start_does_not_reap_or_resume(tmp_path, monkeypatch):
    monkeypatch.setattr('ai_voice.desktop_control.resolve_output', lambda name: 0)
    catalog = Catalog(path=str(tmp_path / 'catalog.json'))
    controller, store, trainer = _make_controller(tmp_path)
    meta = store.create('VC', kind='imported', model_path='m')
    control = DesktopControl(catalog, vc=controller)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        reaped = []
        async def start_worker(*args, on_done=None):
            entered.set()
            await release.wait()
            on_done(RuntimeError('test end'))
        async def reap_failed():
            reaped.append(True)
        monkeypatch.setattr(controller, 'start_worker', start_worker)
        monkeypatch.setattr(controller, 'reap_failed', reap_failed)
        task = asyncio.create_task(control.command({'action':'start', 'mode':'vc',
            'vc_voice_id':meta['id'], 'input_device':'mic', 'output_device':'out'}))
        await entered.wait()
        assert control._vc_starting and not controller.loading
        assert control.snapshot()['active'] is True
        await control._reap_failed_vc()
        assert reaped == [] and trainer.paused and trainer.signals == ['SIGSTOP']
        release.set()
        await task
        assert not control._vc_starting and not trainer.paused
    try:
        asyncio.run(run())
    finally:
        controller.close()


class _TextTTS:
    def __init__(self, config, key, client=None):
        assert config.reference_id == 'a' * 32
        assert config.tts_sample_rate == 44100
        assert config.request_timeout_seconds == 15.0
    async def stream(self, text):
        import numpy as np
        samples = (np.sin(np.arange(22050) * 2 * np.pi * 440 / 44100) * 12000).astype('<i2').tobytes()
        # Split in the middle of an int16 frame to check byte carry.
        yield samples[:101]
        yield samples[101:]


def _text_controller(tmp_path, factory=_TextTTS):
    store = _FakeStore(tmp_path)
    worker = _FakeWorker()
    worker.snap['state'] = 'running'
    controller = VcController(store, trainer=_FakeTrainer(store), worker=worker,
        uploads=tmp_path / 'uploads', inject_dir=tmp_path / 'inject', tts_factory=factory)
    return controller, worker


def test_vc_text_synthesis_resamples_and_atomically_injects(tmp_path):
    import numpy as np
    controller, worker = _text_controller(tmp_path)
    async def run():
        phrase_id = await controller.speak_text('Фраза', 'a' * 32, 'fake', target='monitor')
        sent_id, path, target = worker.inject_calls[0]
        assert sent_id == phrase_id and len(phrase_id) == 12 and target == 'monitor'
        samples = np.fromfile(path, dtype='<f4')
        assert abs(len(samples) - 20000) <= 64 and np.isfinite(samples).all()
        assert .3 < np.max(abs(samples)) < 1
        assert not list(controller.inject_dir.glob('*.tmp'))
        assert controller.snapshot()['vc']['inject'] == {'id':phrase_id, 'state':'synth'}
    try:
        asyncio.run(run())
    finally:
        controller.close()


@pytest.mark.parametrize('failure', ['tts', 'inject'])
def test_vc_text_failure_reports_error_and_removes_files(tmp_path, failure):
    from ai_voice.fish_tts import TTSError
    class FailedTTS(_TextTTS):
        async def stream(self, text):
            yield b'\0\0' * 10
            raise TTSError('Fish unavailable')
    controller, worker = _text_controller(tmp_path, FailedTTS if failure == 'tts' else _TextTTS)
    if failure == 'inject':
        def fail(*args):
            raise RuntimeError('broken worker')
        worker.inject = fail
    try:
        with pytest.raises(RuntimeError, match='Fish unavailable|broken worker'):
            asyncio.run(controller.speak_text('Текст', 'a'*32, 'fake'))
        inject = controller.snapshot()['vc']['inject']
        assert inject['state'] == 'error' and inject['error']
        assert not list(controller.inject_dir.iterdir())
    finally:
        controller.close()


def test_vc_text_parallel_synthesis_conflict_and_cancel(tmp_path):
    entered = asyncio.Event()
    class SlowTTS(_TextTTS):
        async def stream(self, text):
            entered.set()
            await asyncio.Event().wait()
            yield b'\0\0'
    controller, worker = _text_controller(tmp_path, SlowTTS)
    async def run():
        task = asyncio.create_task(controller.speak_text('Первый', 'a'*32, 'fake'))
        await entered.wait()
        assert controller.snapshot()['vc']['inject']['state'] == 'synth'
        with pytest.raises(RuntimeError, match='still being prepared'):
            await controller.speak_text('Второй', 'a'*32, 'fake')
        controller.cancel_text()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.snapshot()['vc']['inject']['state'] == 'cancelled'
        assert worker.inject_cancel_calls == 1 and not worker.inject_calls
        assert not list(controller.inject_dir.iterdir())
    try:
        asyncio.run(run())
    finally:
        controller.close()


@pytest.mark.parametrize('text', [None, '', '  ', 'x'*1001])
def test_vc_text_invalid_text_and_stopped(tmp_path, text):
    controller, worker = _text_controller(tmp_path)
    try:
        with pytest.raises(ValueError, match='Enter 1 to 1000 characters'):
            asyncio.run(controller.speak_text(text, 'a'*32, 'fake'))
        worker.snap['state'] = 'idle'
        with pytest.raises(ConflictError, match='Press Start first'):
            asyncio.run(controller.speak_text('ok', 'a'*32, 'fake'))
    finally:
        controller.close()


def test_vc_text_preferences_defaults_validation_load_and_removal(tmp_path):
    catalog = Catalog(write_library(tmp_path / 'catalog.json',
        [{'id': TEST_VOICE_ID, 'name': 'Test voice'}, {'id': 'a'*32, 'name': 'Other voice'},
         {'id': 'b'*32, 'name': 'Remaining voice'}],
        TEST_VOICE_ID))
    assert catalog.preferences()['vc_text_voice_id'] is None
    voices = catalog.voices()
    chosen = voices[0]['id']
    assert catalog.update({'vc_text_voice_id':chosen})['vc_text_voice_id'] == chosen
    assert Catalog(catalog.path).preferences()['vc_text_voice_id'] == chosen
    for value in ('bad', 'f'*32, 3, False):
        with pytest.raises(ValueError, match=re.escape(t('errors.text_voice_not_found'))):
            catalog.update({'vc_text_voice_id':value})
    catalog.remove(chosen)
    assert catalog.preferences()['vc_text_voice_id'] is None
    chosen = catalog.voices()[0]['id']
    catalog.update({'vc_text_voice_id':chosen})
    catalog.remove_many([chosen])
    assert catalog.preferences()['vc_text_voice_id'] is None
    stored = json.loads(catalog.path.read_text())
    stored['vc_text_voice_id'] = 'f'*32
    catalog.path.write_text(json.dumps(stored))
    assert Catalog(catalog.path).preferences()['vc_text_voice_id'] is None


def test_vc_text_worker_commands_events_and_sample_rate():
    from types import SimpleNamespace
    worker = VcWorker()
    sent = []
    worker._write = sent.append
    worker.inject('abc', Path('/tmp/abc.f32'), 'monitor')
    worker._proc = SimpleNamespace()
    worker.cancel_inject()
    assert sent == [{'cmd':'inject', 'id':'abc', 'path':'/tmp/abc.f32', 'target':'monitor'}, {'cmd':'inject_cancel'}]
    worker._handle_event({'event':'status', 'state':'running', 'config':{'sample_rate':48000, 'hop_ms':256}})
    assert worker._sample_rate == 48000
    for state in ('playing', 'done', 'cancelled'):
        worker._handle_event({'event':'inject', 'id':'abc', 'state':state})
        assert worker._snapshot['inject'] == {'id':'abc', 'state':state}
    def broken(payload):
        raise BrokenPipeError()
    worker._write = broken
    with pytest.raises(RuntimeError, match='The voice process stopped'):
        worker.inject('abc', '/tmp/abc.f32')


def test_desktop_vc_speak_reference_history_and_unlocked_synthesis(tmp_path, monkeypatch):
    catalog = Catalog(write_library(tmp_path / 'catalog.json',
        [{'id': TEST_VOICE_ID, 'name': 'Test voice'}, {'id': 'a'*32, 'name': 'Other voice'}],
        TEST_VOICE_ID))
    controller, worker = _text_controller(tmp_path)
    control = DesktopControl(catalog, vc=controller)
    chosen = catalog.voices()[1]['id']
    catalog.update({'vc_text_voice_id':chosen})
    monkeypatch.setattr('ai_voice.desktop_control.load_key', lambda: 'fake')
    async def speak(text, reference_id, key, target="both"):
        assert not control.lock.locked()
        assert reference_id == chosen and key == 'fake'
        return 'abc'
    controller.speak_text = speak
    try:
        assert asyncio.run(control.vc_speak('Фраза')) == {'id':'abc'}
        assert control._history[-1]['source'] == 'vc'
        assert control._history[-1]['text'] == 'Фраза'
    finally:
        controller.close()
