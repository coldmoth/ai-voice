"""HTTP orchestration for the voice-conversion mode."""
import asyncio
import json
import math
import numpy as np
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

import httpx
import soxr

from .i18n import t
from .paths import DATA, ROOT, VC_PYTHON
from .desktop_control import ConflictError, NotFoundError
from .hf_catalog import HfCatalog, BASE_URL, MAX_BYTES
from .vc_store import VcStore, VcStoreError
from .vc_train import VcTrainer, VcTrainError
from .vc_omp import dedupe_libomp
from .vc_meter import InputMeter
from .fish_tts import FishTTS, TTSError
from .preview import _PreviewConfig


_LIMIT = 500 * 1024 * 1024
_BOUNDARY_BUF = 65536
_TEXT_LIMIT = 1000
_MAX_FILES = 50
_MAX_FIELDS = 20
_FIELD_NAME_LIMIT = 100
_BASENAME_LIMIT = 100


def _safe_basename(name):
    if not isinstance(name, str) or not name:
        return "file"
    cleaned = name.replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 0x20 and ch not in {"\x7f"})
    cleaned = cleaned.replace("..", "_").strip().strip(".")
    if not cleaned:
        cleaned = "file"
    return cleaned[:_BASENAME_LIMIT]


def _extract_boundary(content_type):
    if not isinstance(content_type, str):
        return None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            value = part.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if value:
                return value
    return None


def parse_multipart(content_type, rfile, length, directory, limit=_LIMIT):
    """Parse a multipart/form-data body stream into fields and files.

    Reads the body in 64KB chunks without loading it into memory. Returns
    ``(fields, files)`` where ``files`` is a list of ``Path`` objects written
    under ``directory``.
    """
    if not isinstance(length, int) or length < 0:
        raise ValueError(t("errors.upload_invalid"))
    if length > limit:
        raise ValueError(t("errors.upload_size"))
    boundary = _extract_boundary(content_type)
    if not boundary:
        raise ValueError(t("errors.upload_invalid"))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    try:
        delim = ("--" + boundary).encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(t("errors.upload_invalid")) from None
    body_delim = b"\r\n" + delim
    fields, files = {}, []
    field_count = 0
    buffer = b""
    remainder = length
    state = "preamble"
    current_file = None
    current_field = None
    current_data = b""
    finished = False

    def write_data(data):
        nonlocal current_data
        if current_file is not None:
            current_file.write(data)
        elif current_field is not None:
            if len(current_data) + len(data) > _TEXT_LIMIT:
                raise ValueError(t("errors.upload_field_length"))
            current_data += data

    try:
        while remainder > 0:
            chunk = rfile.read(min(_BOUNDARY_BUF, remainder))
            if not chunk:
                raise ValueError(t("errors.upload_invalid"))
            remainder -= len(chunk)
            if finished:
                continue
            buffer += chunk
            while True:
                if state == "preamble":
                    idx = buffer.find(delim)
                    if idx < 0:
                        buffer = buffer[-(len(delim) + 4):]
                        break
                    end = idx + len(delim)
                    if len(buffer) < end + 2:
                        break
                    if buffer[end:end + 2] != b"\r\n":
                        raise ValueError(t("errors.upload_invalid"))
                    buffer = buffer[end + 2:]
                    state = "headers"
                elif state == "headers":
                    idx = buffer.find(b"\r\n\r\n")
                    if idx < 0:
                        if len(buffer) > _BOUNDARY_BUF:
                            raise ValueError(t("errors.upload_invalid"))
                        break
                    header_block = buffer[:idx].decode("utf-8", errors="replace")
                    buffer = buffer[idx + 4:]
                    disposition = ""
                    for line in header_block.split("\r\n"):
                        if line.lower().startswith("content-disposition:"):
                            disposition = line.split(":", 1)[1].strip()
                            break
                    name = filename = None
                    for piece in disposition.split(";"):
                        piece = piece.strip()
                        if piece.startswith("name="):
                            name = piece.split("=", 1)[1].strip().strip('"')
                        elif piece.startswith("filename="):
                            filename = piece.split("=", 1)[1].strip().strip('"')
                    current_field = None
                    current_data = b""
                    if filename is not None:
                        if len(files) >= _MAX_FILES:
                            raise ValueError(t("errors.upload_file_count"))
                        path = directory / f"{len(files) + 1:03d}-{_safe_basename(filename)}"
                        current_file = open(path, "wb")
                        files.append(path)
                    elif name:
                        field_count += 1
                        if field_count > _MAX_FIELDS or len(name) > _FIELD_NAME_LIMIT:
                            raise ValueError(t("errors.upload_invalid"))
                        current_field = name
                    else:
                        raise ValueError(t("errors.upload_invalid"))
                    state = "body"
                else:
                    idx = buffer.find(body_delim)
                    if idx < 0:
                        flush_end = max(0, len(buffer) - len(delim) - 4)
                        write_data(buffer[:flush_end])
                        buffer = buffer[flush_end:]
                        break
                    end = idx + len(body_delim)
                    if len(buffer) < end + 2:
                        break
                    suffix = buffer[end:end + 2]
                    if suffix not in {b"--", b"\r\n"}:
                        raise ValueError(t("errors.upload_invalid"))
                    write_data(buffer[:idx])
                    if current_file is not None:
                        current_file.close()
                        current_file = None
                    elif current_field is not None:
                        try:
                            fields[current_field] = current_data.decode("utf-8")
                        except UnicodeDecodeError:
                            raise ValueError(t("errors.upload_invalid")) from None
                    buffer = buffer[end + 2:]
                    if suffix == b"--":
                        finished = True
                        buffer = b""
                        break
                    state = "headers"
        if not finished:
            raise ValueError(t("errors.upload_invalid"))
        return fields, files
    finally:
        if current_file is not None:
            current_file.close()


class VcWorker:
    """Manages the vc_worker subprocess and translates its events into a snapshot."""

    def __init__(self, popen=subprocess.Popen, python_path=None, clock=time.monotonic,
                 status_interval=2.0):
        self._popen = popen
        self._python_path = Path(python_path) if python_path else VC_PYTHON
        self._clock = clock
        self._status_interval = status_interval
        self._cancel = threading.Event()
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self._stopped_count = 0
        self._hop_ms = None
        self._sample_rate = 40000
        self._bench_results = None
        self._bench_done = threading.Event()
        self._proc = None
        self._lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._reader_thread = None
        self._crashed = False
        self._snapshot = {
            "voice_id": None,
            "state": "idle",
            "latency_ms": None,
            "cpu": None,
            "rss_mb": None,
            "input_level": None,
            "inject": None,
            "input_db": None,
            "gate_open": None,
            "output_level": None,
            "dropped_blocks": 0,
            "processing_ms": None,
            "error": None,
        }
        self._cpu_pid = None
        self._cpu_cache = {"ts": 0.0, "value": None}
        self._active_voice_id = None

    def _write(self, payload):
        with self._stdin_lock:
            if self._proc is None or self._proc.stdin is None:
                raise BrokenPipeError("worker stdin not available")
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except BrokenPipeError:
                self._mark_crashed()
                raise

    def _mark_crashed(self):
        with self._snapshot_lock:
            self._crashed = True
            if self._snapshot["state"] not in {"error"}:
                self._snapshot["state"] = "error"
            if not self._snapshot.get("error"):
                self._snapshot["error"] = t("status.voice_process_stopped")

    def _read_cpu(self):
        pid = self._cpu_pid
        if pid is None:
            return None
        now = self._clock()
        if now - self._cpu_cache["ts"] < 1.0:
            return self._cpu_cache["value"]
        try:
            out = subprocess.check_output(["ps", "-o", "%cpu=", "-p", str(pid)],
                                           stderr=subprocess.DEVNULL, text=True, timeout=1)
            value = float(out.strip())
        except Exception:
            value = None
        self._cpu_cache = {"ts": now, "value": value}
        return value

    def _reader_loop(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                self._handle_event(event)
        except Exception:
            pass
        if proc.poll() is not None:
            self._poll_stop.set()
            with self._snapshot_lock:
                if not self._crashed and self._snapshot["state"] != "idle":
                    self._snapshot["state"] = "error"
                    self._snapshot["error"] = t("status.voice_process_stopped")

    def _handle_event(self, event):
        kind = event.get("event")
        with self._snapshot_lock:
            if kind == "bench":
                self._bench_results = event.get("results")
                self._bench_done.set()
            elif kind == "status":
                state = event.get("state")
                if isinstance(state, str):
                    self._snapshot["state"] = state
                for key in ("rss_mb", "dropped_blocks", "latency_ms", "processing_ms"):
                    if key in event:
                        self._snapshot[key] = event[key]
                config = event.get("config")
                if isinstance(config, dict) and isinstance(config.get("hop_ms"), (int, float)):
                    self._hop_ms = config["hop_ms"]
                if isinstance(config, dict):
                    self._sample_rate = config.get("sample_rate", 40000)
                if state in {"loaded", "running", "idle"}:
                    self._snapshot["error"] = None
            elif kind == "inject":
                self._snapshot["inject"] = {"id": event.get("id"), "state": event.get("state")}
            elif kind == "levels":
                input_db = event.get("input_db")
                if (isinstance(input_db, (int, float)) and not isinstance(input_db, bool)
                        and math.isfinite(input_db)):
                    self._snapshot["input_db"] = input_db
                    meter = getattr(self, "meter", None)
                    if meter is not None:
                        meter.record(input_db)
                if isinstance(event.get("gate_open"), bool):
                    self._snapshot["gate_open"] = event["gate_open"]
                if "input_level" in event:
                    self._snapshot["input_level"] = event["input_level"]
                if "output_level" in event:
                    self._snapshot["output_level"] = event["output_level"]
                if "dropped_blocks" in event:
                    self._snapshot["dropped_blocks"] = event["dropped_blocks"]
                processing = event.get("processing_ms")
                if isinstance(processing, (int, float)):
                    self._snapshot["processing_ms"] = processing
                    if self._hop_ms is not None:
                        self._snapshot["latency_ms"] = 2 * self._hop_ms + processing
            elif kind == "error":
                message = event.get("message")
                if isinstance(message, str):
                    self._snapshot["error"] = message[:300]
                self._snapshot["state"] = "error"
                self._crashed = True
            elif kind == "stopped":
                reason = event.get("reason")
                if reason == "requested":
                    if self._snapshot["state"] == "running":
                        self._snapshot["state"] = "loaded"
                    self._stopped_count += 1
                else:
                    self._snapshot["state"] = "error"
                    self._crashed = True
                    if not self._snapshot.get("error"):
                        self._snapshot["error"] = t("status.voice_process_stopped")

    def _wait_for_state(self, target_states, timeout, error_label):
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self._cancel.is_set():
                raise RuntimeError(t("errors.voice_start_cancelled"))
            with self._snapshot_lock:
                if self._snapshot["state"] in target_states:
                    return
                if self._snapshot["state"] == "error" or self._crashed:
                    message = self._snapshot.get("error") or error_label
                    raise RuntimeError(message)
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(error_label)
            time.sleep(0.05)
        raise RuntimeError(error_label)

    def request_cancel(self):
        """Ask a running start() to give up; it raises and cleans up by itself."""
        self._cancel.set()

    def _poll_loop(self, stop_event):
        while not stop_event.wait(self._status_interval):
            with self._snapshot_lock:
                running = self._snapshot["state"] == "running"
            if not running:
                continue
            try:
                self._write({"cmd": "status"})
            except (BrokenPipeError, ValueError, OSError):
                return

    def _start_polling(self):
        self._poll_stop = stop_event = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, args=(stop_event,), daemon=True)
        self._poll_thread.start()

    @staticmethod
    def _start_payload(devices):
        return {"cmd": "start",
                "devices": {
                    "input_device": devices.get("input_device"),
                    "output_device": devices.get("output_device"),
                    "monitor_enabled": bool(devices.get("monitor_enabled", False)),
                    "monitor_device": devices.get("monitor_device"),
                }}

    def start(self, voice_meta, directory, devices, params, *, load_only=False):
        self._cancel.clear()
        if not self._python_path.exists():
            raise RuntimeError(t("errors.vc_environment_missing"))
        logs_dir = DATA / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(logs_dir / "vc_worker.log", "a", encoding="utf-8")
        env = os.environ.copy()
        env["SYSTEM_VERSION_COMPAT"] = "0"
        env["AI_VOICE_INJECT_DIR"] = str(DATA / "vc-inject")
        try:
            relinked = dedupe_libomp(self._python_path)
        except OSError as exc:
            # Without links libomp aborts; allowing two runtimes is riskier, so only fall back when relinking fails.
            relinked = []
            env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
            log_file.write(f"libomp dedupe failed: {exc}; KMP_DUPLICATE_LIB_OK=TRUE\n")
        if relinked:
            log_file.write("libomp linked to torch: " + ", ".join(relinked) + "\n")
        log_file.flush()
        try:
            proc = self._popen([str(self._python_path), "-m", "vc_worker"],
                               cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=log_file, text=True, bufsize=1, start_new_session=True,
                               env=env)
        finally:
            # The child holds its own copy of the descriptor.
            log_file.close()
        self._proc = proc
        self._cpu_pid = proc.pid
        self._crashed = False
        with self._snapshot_lock:
            self._snapshot.update({
                "state": "loading",
                "error": None,
                "latency_ms": None,
                "rss_mb": None,
                "input_level": None,
                "inject": None,
                "input_db": None,
                "gate_open": None,
                "output_level": None,
                "dropped_blocks": 0,
            })
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        try:
            voice_payload = {}
            kind = voice_meta.get("kind")
            directory = Path(directory)
            if kind == "zeroshot":
                voice_payload = {"kind": "zeroshot",
                                 "reference": str(directory / "reference.wav"),
                                 "ref_seconds": voice_meta.get("ref_seconds", 5),
                                 "pitch_shift": int(voice_meta.get("pitch_shift", 0))}
            else:
                voice_payload = {"kind": kind, "model": str(directory / "model.pth")}
                index_path = directory / "model.index"
                if index_path.exists():
                    voice_payload["index"] = str(index_path)
            load_params = {
                "pitch_shift": int(voice_meta.get("pitch_shift", 0)),
                "index_rate": float(voice_meta.get("index_rate", 0.0)),
                "output_gain_db": float(params.get("output_gain_db", 0.0)),
                "monitor_gain_db": float(params.get("monitor_gain_db", 0.0)),
                "bypass": bool(params.get("bypass", False)),
                "gate_enabled": bool(params.get("gate_enabled", False)),
                "gate_db": float(params.get("gate_db", -45.0)),
            }
            if kind == "zeroshot":
                load_params["diffusion_steps"] = voice_meta.get("diffusion_steps", 4)
            try:
                self._write({"cmd": "load", "voice": voice_payload, "params": load_params,
                             "config": {"hop_ms": voice_meta.get("block_ms", 256)}})
            except BrokenPipeError:
                raise RuntimeError(t("errors.voice_engine_log")) from None
            self._wait_for_state({"loaded", "running"}, 180.0,
                                 t("errors.voice_engine_log"))
            if load_only:
                return
            try:
                self._write(self._start_payload(devices))
            except BrokenPipeError:
                raise RuntimeError(t("errors.voice_engine_log")) from None
            self._wait_for_state({"running"}, 20.0,
                                 t("errors.voice_engine_log"))
            self._start_polling()
            self._active_voice_id = voice_meta.get("id")
            with self._snapshot_lock:
                self._snapshot["voice_id"] = self._active_voice_id
        except BaseException:
            self.stop()
            raise

    def bench(self, candidates):
        self._bench_results = None
        self._bench_done.clear()
        self._write({"cmd": "bench", "candidates": candidates})
        deadline = self._clock() + 90
        while self._clock() < deadline:
            if self._cancel.is_set():
                raise RuntimeError(t("errors.autotune_incomplete"))
            if self._bench_done.wait(0.05):
                return self._bench_results
            with self._snapshot_lock:
                if self._snapshot["state"] == "error" or self._crashed:
                    raise RuntimeError(self._snapshot.get("error") or t("errors.autotune_incomplete"))
        raise RuntimeError(t("errors.autotune_incomplete"))

    def restart_stream(self, devices):
        """Stop and start audio with new devices; the model stays loaded."""
        if self._proc is None or self._crashed:
            raise RuntimeError(t("errors.voice_process_inactive"))
        before = self._stopped_count
        try:
            self._write({"cmd": "stop"})
            deadline = self._clock() + 10.0
            while self._stopped_count == before:
                if self._clock() >= deadline:
                    raise RuntimeError(t("errors.voice_stop_restart"))
                time.sleep(0.02)
            self._write(self._start_payload(devices))
        except BrokenPipeError:
            raise RuntimeError(t("status.voice_process_stopped")) from None
        self._wait_for_state({"running"}, 20.0, t("errors.voice_audio_restart"))

    def set_params(self, params):
        if self._proc is None or self._crashed:
            return
        with self._snapshot_lock:
            if self._snapshot["state"] != "running":
                return
        try:
            self._write({"cmd": "params", "params": params})
        except BrokenPipeError:
            return

    def set_inject_state(self, phrase_id, state, **fields):
        with self._snapshot_lock:
            self._snapshot["inject"] = {"id": phrase_id, "state": state, **fields}

    def inject(self, phrase_id, path, target='both'):
        try:
            self._write({"cmd": "inject", "id": phrase_id, "path": str(path), "target": target})
        except BrokenPipeError:
            raise RuntimeError(t("status.voice_process_stopped")) from None

    def cancel_inject(self):
        if self._proc is None:
            return
        try:
            self._write({"cmd": "inject_cancel"})
        except BrokenPipeError:
            raise RuntimeError(t("status.voice_process_stopped")) from None

    def stop(self):
        with self._lock:
            proc = self._proc
            self._poll_stop.set()
            if proc is None:
                return
            try:
                self._write({"cmd": "stop"})
            except BrokenPipeError:
                pass
            try:
                self._write({"cmd": "quit"})
            except BrokenPipeError:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            self._proc = None
            self._active_voice_id = None
            poller = self._poll_thread
            if poller is not None and poller is not threading.current_thread():
                poller.join(timeout=3)
            with self._snapshot_lock:
                self._snapshot["state"] = "idle"
                self._snapshot["voice_id"] = None
                self._snapshot["error"] = None
                self._snapshot["input_level"] = None
                self._snapshot["inject"] = None
                self._snapshot["input_db"] = None
                self._snapshot["gate_open"] = None
                self._snapshot["output_level"] = None

    def snapshot(self):
        with self._snapshot_lock:
            data = dict(self._snapshot)
        cpu = self._read_cpu()
        data["cpu"] = cpu
        return data

    @property
    def active_voice_id(self):
        return self._active_voice_id

    def worker_failed(self):
        with self._snapshot_lock:
            return self._snapshot["state"] == "error"


class VcController:
    """Facade that coordinates VcStore, VcTrainer, and VcWorker."""

    def __init__(self, store, trainer=None, worker=None, uploads=None,
                 hf_catalog=None, download_client=None, meter=None, clock=time.monotonic,
                 tts_factory=None, inject_dir=None):
        self.store = store
        self.last_done = None
        if trainer is None:
            self.trainer = VcTrainer(store, on_done=self._on_done)
        else:
            self.trainer = trainer
            self.trainer.on_done = self._on_done
        self.worker = worker if worker is not None else VcWorker()
        self.inject_dir = Path(inject_dir) if inject_dir is not None else DATA / "vc-inject"
        self.inject_dir.mkdir(parents=True, exist_ok=True)
        for path in self.inject_dir.glob('*.f32'):
            try:
                if time.time() - path.stat().st_mtime > 3600:
                    path.unlink()
            except FileNotFoundError:
                pass
        self._tts_factory = tts_factory
        self._text_lock = threading.Lock()
        self._text_task = None
        self._clock = clock
        self.meter = meter if meter is not None else InputMeter(clock=clock)
        self.worker.meter = self.meter
        self._meter_deadline = 0.0
        self._meter_lock = threading.Lock()
        self._calibration_lock = threading.Lock()
        self.uploads = Path(uploads) if uploads is not None else (DATA / "vc-uploads")
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.hf_catalog = hf_catalog if hf_catalog is not None else HfCatalog()
        self._download_client = download_client
        self._download_lock = threading.Lock()
        self._download_cancel = threading.Event()
        self._download_thread = None
        self._download = dict(state='idle', id=None, bytes=0, total=0)
        self.active_voice = None
        self.bypass = False
        self._lifecycle_lock = asyncio.Lock()
        self._start_task = None
        self._start_cancel = None
        self._start_error = None
        self._devices = None
        self._audio_queue = queue.Queue()
        self._audio_closed = threading.Event()
        self._audio_thread = threading.Thread(target=self._process_audio_queue, daemon=True)
        self._audio_thread.start()

    async def speak_text(self, text, reference_id, key, target='both'):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError(t("errors.text_length"))
        if self.worker.snapshot().get('state') != 'running':
            raise ConflictError(t("errors.start_first"))
        with self._text_lock:
            if self._text_task is not None:
                raise ConflictError(t("errors.phrase_preparing"))
            self._text_task = asyncio.current_task()
        phrase_id = uuid.uuid4().hex[:12]
        path = self.inject_dir / (phrase_id + '.f32')
        temporary = self.inject_dir / (phrase_id + '.tmp')
        self.worker.set_inject_state(phrase_id, 'synth')
        try:
            config = _PreviewConfig(reference_id=reference_id, voice=None,
                                    tts_sample_rate=44100, request_timeout_seconds=15.0)
            chunks, frames, remainder = [], 0, b''
            async with httpx.AsyncClient(timeout=15.0) as client:
                tts = (self._tts_factory or FishTTS)(config, key, client=client)
                async for chunk in tts.stream(text):
                    raw = remainder + chunk
                    end = len(raw) - len(raw) % 2
                    remainder = raw[end:]
                    samples = np.frombuffer(raw[:end], dtype='<i2').astype(np.float32) / 32768.0
                    samples = samples[:60 * 44100 - frames]
                    chunks.append(samples)
                    frames += len(samples)
                    if frames >= 60 * 44100:
                        break
            if not frames:
                raise TTSError('Fish returned empty audio')
            samples = soxr.resample(np.concatenate(chunks), 44100, self.worker._sample_rate)
            np.clip(samples, -1, 1).astype('<f4').tofile(temporary)
            os.replace(temporary, path)
            self.worker.inject(phrase_id, path, target)
            return phrase_id
        except asyncio.CancelledError:
            if self.worker.snapshot().get('state') == 'running':
                self.worker.set_inject_state(phrase_id, 'cancelled')
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            path.unlink(missing_ok=True)
            self.worker.set_inject_state(phrase_id, 'error', error=str(exc))
            if isinstance(exc, TTSError):
                raise RuntimeError(str(exc)) from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)
            with self._text_lock:
                self._text_task = None

    def cancel_text(self):
        with self._text_lock:
            if self._text_task is not None:
                self._text_task.cancel()
        self.worker.cancel_inject()

    def catalog_download_status(self):
        with self._download_lock:
            return deepcopy(self._download)

    def catalog_download(self, card_id):
        if not isinstance(card_id, str) or not card_id:
            raise ValueError(t("errors.hf_model_id_invalid"))
        with self._download_lock:
            if self._download['state'] in ('downloading', 'importing'):
                raise ConflictError(t("errors.hf_downloading"))
        card = self.hf_catalog.get_card(card_id)
        if card is None:
            raise NotFoundError(card_id)
        if not card.get('revision'):
            raise ValueError(t("errors.hf_revision_missing"))
        source = dict(kind='hf', repo=card['repo'], revision=card['revision'],
                      pth=card['files'][0]['path'],
                      index=next((f['path'] for f in card['files']
                                  if Path(f['path']).suffix.lower() == '.index'), None))
        with self._download_lock:
            if self._download['state'] in ('downloading', 'importing'):
                raise ConflictError(t("errors.hf_downloading"))
            for voice in self.store.list():
                existing = voice.get('source') or {}
                if (existing.get('kind') == 'hf' and existing.get('repo') == source['repo']
                        and existing.get('pth') == source['pth']):
                    raise ConflictError(t("errors.hf_already_added"))
            self._download_cancel.clear()
            self._download = dict(state='downloading', id=card_id, bytes=0, total=card['total_size'])
            self._download_thread = threading.Thread(
                target=self._run_catalog_download, args=(card, source), daemon=True)
            self._download_thread.start()
            return deepcopy(self._download)

    def catalog_download_cancel(self):
        with self._download_lock:
            if self._download['state'] in ('downloading', 'importing'):
                self._download_cancel.set()
            return deepcopy(self._download)

    def _run_catalog_download(self, card, source):
        temporary = self.uploads / uuid.uuid4().hex
        meta = None
        error = None
        try:
            temporary.mkdir()
            paths = []
            received = 0
            limit = min(MAX_BYTES, self.store.max_import_bytes)
            manager = (nullcontext(self._download_client) if self._download_client is not None
                       else httpx.Client())
            with manager as client:
                for number, file in enumerate(card['files']):
                    if self._download_cancel.is_set():
                        raise ValueError(t("errors.cancelled"))
                    target = temporary / (str(number) + Path(file['path']).suffix.lower())
                    url = (BASE_URL + '/' + quote(card['repo'], safe='/') + '/resolve/' +
                           quote(card['revision'], safe='') + '/' + quote(file['path'], safe='/'))
                    with client.stream('GET', url, timeout=15, follow_redirects=True,
                                       headers={'User-Agent': 'AI-Voice/1.0'}) as response:
                        response.raise_for_status()
                        with target.open('xb') as output:
                            for block in response.iter_bytes(chunk_size=65536):
                                if self._download_cancel.is_set():
                                    raise ValueError(t("errors.cancelled"))
                                received += len(block)
                                if received > limit:
                                    raise ValueError(t("errors.hf_download_limit"))
                                output.write(block)
                                with self._download_lock:
                                    self._download['bytes'] = received
                    paths.append(target)
            with self._download_lock:
                if self._download_cancel.is_set():
                    raise ValueError(t("errors.cancelled"))
                self._download['state'] = 'importing'
            validated = self._validate_upload_files(paths, 'imported')
            meta = self.store.create(card['title'], kind='imported',
                                     model_path=validated.get('zip') or validated['pth'],
                                     index_path=validated.get('index'), source=source)
        except Exception as exc:
            error = (t("errors.hf_connection") if isinstance(exc, httpx.HTTPError) else str(exc))
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
            with self._download_lock:
                # Cancellation and publication are serialized; a cancelled import
                # must not leave an invisible extra voice in the library.
                if self._download_cancel.is_set():
                    error = t("errors.cancelled")
                    if meta is not None:
                        try:
                            self.store.delete(meta['id'])
                        except Exception as exc:
                            error = str(exc)
                if error is not None:
                    self._download.update(state='error', error=error)
                else:
                    self._download.update(state='done', voice=meta)

    def _process_audio_queue(self):
        while True:
            voice_id = self._audio_queue.get()
            try:
                if voice_id is None:
                    return
                if self._audio_closed.is_set():
                    continue
                meta = self.store.process_audio(voice_id)
                if meta and meta.get('status') == 'ready' and meta.get('can_train') and meta.get('train_after'):
                    self.train(voice_id, meta['train_after'])
            except Exception as exc:
                try:
                    with self.store._lock:
                        meta = self.store.get(voice_id)
                        meta['train_error'] = str(exc)
                        self.store._write(self.store._directory(voice_id), meta)
                except Exception:
                    pass  # Deleted voice or persistence failure must not kill the queue.
            finally:
                self._audio_queue.task_done()

    def _on_done(self, voice_id, state):
        name = ""
        try:
            meta = self.store.get(voice_id)
            if isinstance(meta, dict):
                name = meta.get("name", "")
        except Exception:
            name = ""
        self.last_done = {"voice_id": voice_id, "name": name, "state": state, "at": time.time_ns()}

    def voices(self):
        items = []
        training_status = self.trainer.status()
        training_voice_id = training_status.get("voice_id") if isinstance(training_status, dict) else None
        for item in self.store.list():
            entry = dict(item)
            entry["training"] = (isinstance(training_voice_id, str) and entry.get("id") == training_voice_id
                                 and training_status.get('state') in ('running', 'paused'))
            items.append(entry)
        return {"items": items, "training": training_status, "last_done": self.last_done,
                "runtime_ok": Path(getattr(self.worker, "_python_path", VC_PYTHON)).is_file()}

    def _validate_upload_files(self, files, expected):
        accepted = []
        suffixes = {".pth", ".index", ".zip"}
        for path in files:
            suffix = Path(path).suffix.lower()
            if suffix not in suffixes:
                raise ValueError(t("errors.vc_import_files"))
            accepted.append((path, suffix))
        if expected == "imported":
            zips = [item for item in accepted if item[1] == ".zip"]
            pths = [item for item in accepted if item[1] == ".pth"]
            indices = [item for item in accepted if item[1] == ".index"]
            if zips:
                if len(zips) != 1 or len(pths) or len(indices):
                    raise ValueError(t("errors.vc_import_zip_model"))
                return {"zip": zips[0][0]}
            if len(pths) != 1:
                raise ValueError(t("errors.vc_import_model"))
            if len(indices) > 1:
                raise ValueError(t("errors.vc_import_model"))
            return {"pth": pths[0][0], "index": indices[0][0] if indices else None}
        return {}

    def import_model(self, content_type, rfile, length):
        tmp_dir = self.uploads / uuid.uuid4().hex
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            fields, files = parse_multipart(content_type, rfile, length, tmp_dir)
            name = fields.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(t("errors.voice_name_required"))
            validated = self._validate_upload_files(files, "imported")
            if "zip" in validated:
                meta = self.store.create(name.strip(), kind="imported",
                                         model_path=str(validated["zip"]),
                                         index_path=None)
            else:
                meta = self.store.create(name.strip(), kind="imported",
                                         model_path=str(validated["pth"]),
                                         index_path=str(validated["index"]) if validated.get("index") else None)
            return meta
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def create_voice(self, content_type, rfile, length):
        tmp_dir = self.uploads / uuid.uuid4().hex
        tmp_dir.mkdir(parents=True, exist_ok=True)
        meta = None
        try:
            fields, files = parse_multipart(content_type, rfile, length, tmp_dir)
            name = fields.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(t("errors.voice_name_required"))
            if not files:
                raise ValueError(t("errors.audio_file_required"))
            preset = fields.get('train', 'none').lower()
            preset = {'true': 'normal', '1': 'normal', 'false': 'none', '0': 'none'}.get(preset, preset)
            if preset not in ('none', 'fast', 'normal', 'max'):
                raise ValueError(t("errors.training_preset_unknown"))
            meta = self.store.create_pending(name.strip(), [str(path) for path in files])
            if preset != 'none':
                with self.store._lock:
                    meta['train_after'] = preset
                    self.store._write(self.store._directory(meta['id']), meta)
            response = dict(meta)
            self._audio_queue.put(meta['id'])
            return {'voice': response}
        except BaseException:
            if meta is not None:
                try:
                    self.store.delete(meta["id"])
                except Exception:
                    pass
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def cancel_training(self, voice_id=None):
        if voice_id is not None:
            if not isinstance(voice_id, str) or not voice_id:
                raise ValueError(t("errors.voice_id_invalid"))
        try:
            self.trainer.cancel(voice_id)
        except VcTrainError as exc:
            raise ConflictError(str(exc)) from exc
        return self.trainer.status()

    def pause_training(self):
        return self._set_user_pause(True)

    def resume_training(self):
        return self._set_user_pause(False)

    def _set_user_pause(self, paused):
        status = self.trainer.status()
        if not status or (not status.get('voice_id') and not status.get('queue')):
            raise ConflictError(t("errors.training_inactive"))
        return self.trainer.user_pause() if paused else self.trainer.user_resume()

    def train(self, voice_id, preset):
        try:
            return self.trainer.enqueue(voice_id, preset)
        except VcTrainError as exc:
            if t("errors.training_queued") in str(exc):
                raise ConflictError(str(exc)) from exc
            raise

    def move_training(self, voice_id, direction):
        return self.trainer.move(voice_id, direction)

    def rename(self, voice_id, name):
        return self.store.rename(voice_id, name)

    def set_params(self, voice_id, pitch_shift=None, index_rate=None, block_ms=None,
                   diffusion_steps=None, ref_seconds=None):
        previous = self.store.get(voice_id)
        meta = dict(self.store.update_params(voice_id, pitch_shift=pitch_shift, index_rate=index_rate,
                                            block_ms=block_ms, diffusion_steps=diffusion_steps, ref_seconds=ref_seconds))
        zeroshot = previous.get("kind") == "zeroshot"
        if self.active_voice == voice_id:
            params = {key: value for key, value in (
                ("pitch_shift", pitch_shift), ("index_rate", index_rate),
                ("diffusion_steps", diffusion_steps)) if value is not None
                      and (not zeroshot or key == "diffusion_steps")}
            if params:
                self.worker.set_params(params)
            if any(value is not None and previous.get(key) != value for key, value in
                   (("block_ms", block_ms), ("ref_seconds", ref_seconds),
                    ("pitch_shift", pitch_shift if zeroshot else None))):
                meta["restart_required"] = True
        return meta

    async def autotune(self, voice_id):
        await self.reap_failed()
        if self.busy:
            raise ConflictError(t("errors.stop_live_voice"))
        async with self._lifecycle_lock:
            if self.busy:
                raise ConflictError(t("errors.stop_live_voice"))
            meta = self.store.get(voice_id)
            if meta['kind'] == 'zeroshot':
                candidates = [dict(ref_seconds=ref, diffusion_steps=steps, block_ms=block)
                              for ref, steps, block in ((10,6,256), (5,6,256), (5,4,256),
                                                       (10,6,500), (5,4,500), (3,2,1000))]
            else:
                candidates = [dict(block_ms=block) for block in (160, 256, 500, 1000)]

            def measure():
                self.worker.start(meta, self.store._directory(voice_id), {}, {}, load_only=True)
                return self.worker.bench(candidates)

            paused_here = self.trainer.pause()
            task = asyncio.create_task(asyncio.to_thread(measure))
            try:
                results = await asyncio.wait_for(asyncio.shield(task), 90)
                chosen = next((r for r in results if r['ms'] <= 0.6 * r['block_ms']), None)
                warning = None
                if chosen is None:
                    chosen = results[-1]
                    warning = t("status.autotune_slow")
                meta = self.store.update_params(voice_id, **{k: chosen[k] for k in
                    ('block_ms', 'diffusion_steps', 'ref_seconds') if k in chosen})
                return {"voice": meta, "chosen": chosen, "results": results, "warning": warning}
            except asyncio.TimeoutError:
                raise VcStoreError(t("errors.autotune_incomplete")) from None
            finally:
                try:
                    request = getattr(self.worker, 'request_cancel', None)
                    if request is not None:
                        request()
                    await asyncio.to_thread(self.worker.stop)
                    await asyncio.gather(task, return_exceptions=True)
                finally:
                    if paused_here:
                        self.trainer.resume()

    async def delete(self, voice_id):
        await self.reap_failed()
        training_status = self.trainer.status()
        if isinstance(training_status, dict) and training_status.get("state") in ('running', 'paused') \
                and training_status.get("voice_id") == voice_id:
            raise VcStoreError(t("errors.stop_voice_training"))
        if self.active_voice == voice_id:
            raise VcStoreError(t("errors.stop_live_voice_first"))
        async with self._lifecycle_lock:
            if self.active_voice == voice_id:
                raise VcStoreError(t("errors.stop_live_voice_first"))
            self.trainer.remove_queued(voice_id)
            self.store.delete(voice_id)
        return {"ok": True}

    @property
    def loading(self):
        task = self._start_task
        return task is not None and not task.done()

    @property
    def busy(self):
        """Loading, or a voice whose worker is still alive."""
        return self.loading or (self.active_voice is not None and not self.worker.worker_failed())

    async def reap_failed(self):
        """Turn a dead worker into a clean stop; keep its error visible. True if reaped."""
        if self.active_voice is None or self.loading or not self.worker.worker_failed():
            return False
        error = (self.worker.snapshot() or {}).get('error') or t("status.voice_process_stopped")
        await self.stop_worker()
        self._start_error = error
        return True

    async def start_worker(self, voice_id, devices, gains, on_done=None):
        """Begin loading the voice in the background and return at once.

        ``on_done(error)`` runs on the event loop when loading finishes by itself
        (error is None on success); it is not called if stop cancelled the start.
        """
        with self._meter_lock:
            self.meter.stop()
        await self.reap_failed()
        if self.loading:
            raise ConflictError(t("errors.voice_loading"))
        meta = self.store.get(voice_id)
        directory = self.store._directory(voice_id)
        params = {
            "pitch_shift": int(meta.get("pitch_shift", 0)),
            "index_rate": float(meta.get("index_rate", 0.0)),
            "output_gain_db": float(gains.get("output_gain_db", 0.0)),
            "monitor_gain_db": float(gains.get("monitor_gain_db", 0.0)),
            "bypass": bool(gains.get("bypass", False)),
            "gate_enabled": bool(gains.get("gate_enabled", False)),
            "gate_db": float(gains.get("gate_db", -45.0)),
        }
        self.bypass = params["bypass"]
        if meta.get("kind") == "zeroshot":
            params["diffusion_steps"] = meta.get("diffusion_steps", 4)
        with self._meter_lock:
            self.meter.stop()
            self.active_voice = voice_id
        self._start_error = None
        self._devices = dict(devices)
        cancel = self._start_cancel = threading.Event()
        self._start_task = asyncio.create_task(
            self._run_start(voice_id, meta, directory, devices, params, cancel, on_done))

    async def _run_start(self, voice_id, meta, directory, devices, params, cancel, on_done):
        error = None
        async with self._lifecycle_lock:
            if cancel.is_set():
                return
            try:
                await asyncio.to_thread(self.worker.start, meta, directory, devices, params)
            except BaseException as exc:
                if cancel.is_set():
                    return
                error = exc
            if cancel.is_set():
                return
            if error is None and self.bypass != params.get("bypass", False):
                # A swap request that arrived while the model was loading.
                self.worker.set_params({"bypass": self.bypass})
            if error is not None:
                self._start_error = str(error) or t("status.voice_engine_failed")
                if self.active_voice == voice_id:
                    self.active_voice = None
        if on_done is not None:
            try:
                on_done(error)
            except Exception:
                pass

    async def stop_worker(self):
        with self._text_lock:
            text_task = self._text_task
        try:
            self.cancel_text()
        except RuntimeError:
            pass  # A broken command pipe must not prevent stopping the process.
        if text_task is not None:
            await asyncio.gather(text_task, return_exceptions=True)
        cancel = self._start_cancel
        if cancel is not None:
            cancel.set()
            request = getattr(self.worker, "request_cancel", None)
            if request is not None:
                request()
        task = self._start_task
        async with self._lifecycle_lock:
            await asyncio.to_thread(self.worker.stop)
            self.active_voice = None
            self._start_error = None
            self._devices = None
        if task is not None:
            # Cancelled starts return before touching the worker; this never blocks long.
            await asyncio.gather(task, return_exceptions=True)

    async def update_audio(self, gains=None, devices=None):
        """Apply preference changes to a running voice without reloading the model."""
        if self.active_voice is None or self.loading:
            return
        if gains:
            self.worker.set_params(gains)
        if devices is None or self._devices is None:
            return
        merged = {**self._devices, **devices}
        if merged == self._devices:
            return
        async with self._lifecycle_lock:
            if self.active_voice is None:
                return
            await asyncio.to_thread(self.worker.restart_stream, merged)
            self._devices = merged

    def set_bypass(self, bypass):
        """Switch a running voice between own voice (bypass) and the neural voice."""
        if self.active_voice is None:
            raise ConflictError(t("errors.live_voice_inactive"))
        self.bypass = bool(bypass)
        if not self.loading:
            self.worker.set_params({"bypass": self.bypass})

    def meter_heartbeat(self, on, device):
        with self._meter_lock:
            if not on or self._audio_closed.is_set():
                self.meter.stop()
                return {"source": "worker" if self.active_voice is not None or self.loading else "meter"}
            if self.active_voice is not None or self.loading:
                self.meter.stop()
                return {"source": "worker"}
            self._meter_deadline = self._clock() + 5.0
            self.meter.start(device)
            return {"source": "meter"}

    def calibrate_gate(self, device):
        if not self._calibration_lock.acquire(blocking=False):
            raise RuntimeError(t("errors.calibration_running"))
        try:
            if self.active_voice is None and not self.loading:
                self.meter_heartbeat(True, device)
            t0 = self._clock()
            while self._clock() - t0 < 3.0:
                if self._audio_closed.is_set():
                    raise RuntimeError(t("errors.controller_closed"))
                time.sleep(0.1)
            values = self.meter.samples_since(t0)
            if len(values) < 5:
                raise RuntimeError(t("errors.microphone_no_signal"))
            noise = float(np.percentile(values, 95))
            gate = int(round(min(-10, max(-70, noise + 6))))
            return {"noise_db": round(noise, 1), "gate_db": gate}
        finally:
            self._calibration_lock.release()

    def snapshot(self):
        with self._meter_lock:
            if self.meter.running and self._clock() > self._meter_deadline:
                self.meter.stop()
            level = self.meter.level_db()
        worker_snap = self.worker.snapshot()
        worker_snap["meter"] = False
        if self.active_voice is None and not self.loading and level is not None:
            worker_snap.update(input_db=level, gate_open=None, meter=True)
        worker_snap["voice_id"] = self.active_voice
        worker_snap["bypass"] = self.bypass
        if self.loading:
            worker_snap["state"] = "loading"
            worker_snap["error"] = None
        elif self._start_error:
            worker_snap["state"] = "error"
            worker_snap["error"] = self._start_error
        return {"vc": worker_snap, "training": self.trainer.status(), "last_done": self.last_done}

    def worker_failed(self):
        return self.worker.worker_failed()

    def close(self):
        with self._text_lock:
            if self._text_task is not None:
                task = self._text_task
                task.get_loop().call_soon_threadsafe(task.cancel)
        self.catalog_download_cancel()
        self._audio_closed.set()
        with self._meter_lock:
            self.meter.stop()
        self._audio_queue.put(None)
        if self._start_cancel is not None:
            self._start_cancel.set()
        request = getattr(self.worker, "request_cancel", None)
        if request is not None:
            request()
        try:
            self.worker.stop()
        except Exception:
            pass
        try:
            self.trainer.close()
        except Exception:
            pass
