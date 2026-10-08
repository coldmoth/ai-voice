"""Signed macOS Speech helper process bridge; keeps no recordings or transcripts."""
import asyncio
import base64
from collections.abc import AsyncIterator, Sequence
import json
import logging
import math
from pathlib import Path
import tempfile
import os
import signal
import subprocess
import sys
import time
import uuid
from typing import Literal

import numpy as np

from .i18n import t
from .paths import HELPER_BINARY

log = logging.getLogger(__name__)


LOCALES_FALLBACK = {"system": "en-US", "supported": ["en-US", "ru-RU"]}
_LOCALES: dict | None = None


def locales_snapshot() -> dict:
    """Return cached speech locales immediately, even while discovery is pending."""
    return _LOCALES if _LOCALES is not None else LOCALES_FALLBACK


async def supported_locales(*, helper_path=None, timeout: float = 5.0) -> dict:
    """Discover speech locales without LaunchServices or permission requests."""
    global _LOCALES
    if _LOCALES is not None:
        return _LOCALES
    result = LOCALES_FALLBACK
    process = None
    try:
        process_options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                           if sys.platform == 'win32' else {'start_new_session': True})
        process = await asyncio.create_subprocess_exec(
            str(helper_path or HELPER_BINARY), "--locales",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            **process_options)
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode == 0:
            for line in stdout.splitlines():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (isinstance(value, dict) and value.get("event") == "locales"
                        and isinstance(value.get("system"), str)
                        and isinstance(value.get("supported"), list)
                        and all(isinstance(code, str) for code in value["supported"])):
                    result = value
    except (OSError, asyncio.TimeoutError):
        if process is not None:
            # Reap shell wrappers too: a child can otherwise keep stdout open.
            try:
                if sys.platform == 'win32':
                    await asyncio.to_thread(subprocess.run,
                                            ['taskkill', '/T', '/F', '/PID', str(process.pid)],
                                            capture_output=True, timeout=5)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):  # macOS: EPERM once the group leader is a zombie
                pass
            await process.communicate()
    _LOCALES = result
    return result


class ASRError(RuntimeError):
    """A helper protocol, permission, or process failure."""


class _AppProcess:
    """LaunchServices child with one private socket for events and EOF shutdown."""
    def __init__(self, process, reader, writer, server, directory, helper_pid):
        self.process, self.stdout, self.writer = process, reader, writer
        self.stderr, self.server, self.directory = process.stderr, server, directory
        self.helper_pid = helper_pid

    @property
    def returncode(self):
        return self.process.returncode

    async def wait(self):
        return await self.process.wait()

    async def communicate(self):
        stdout, stderr = await asyncio.gather(self.stdout.read(), self.stderr.read())
        await self.wait()
        return stdout, stderr

    def terminate(self):
        self.writer.close()  # Helper owns capture and exits on peer EOF.

    def kill(self):
        self.writer.close()
        try:
            os.kill(self.helper_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if self.process.returncode is None:
            self.process.kill()

    async def cleanup(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        self.directory.cleanup()


class SpeechASR:
    def __init__(self, *, input_device: str = "MIC", language: str = "ru-RU",
                 endpoint_ms: int = 450, threshold_db: float = -45,
                 helper_path: str | Path | None = None,
                 command: Sequence[str] | None = None, launch_app: bool | None = None,
                 initial_input_gain_db: float = 0.0):
        if not input_device.strip() or not language.strip():
            raise ValueError("ASR input and language must not be empty")
        if type(endpoint_ms) is not int or endpoint_ms <= 0:
            raise ValueError("endpoint_ms must be a positive integer")
        if not math.isfinite(threshold_db):
            raise ValueError("threshold_db must be finite")
        if isinstance(initial_input_gain_db, bool) or not isinstance(initial_input_gain_db, (int, float)):
            raise ValueError("initial_input_gain_db must be a finite number")
        gain = float(initial_input_gain_db)
        if not math.isfinite(gain) or gain < -24.0 or gain > 12.0:
            raise ValueError("initial_input_gain_db must be between -24 and 12 dB")
        default = HELPER_BINARY
        binary = Path(helper_path or default)
        app = binary if binary.suffix == ".app" else binary.parents[2]
        self.launch_app = command is None if launch_app is None else launch_app
        self.command = list(command) if command is not None else ["/usr/bin/open", "-n", "-W", str(app), "--args"]
        if not self.command:
            raise ValueError("helper command must not be empty")
        self.input_device, self.language = input_device, language
        self.endpoint_ms, self.threshold_db = endpoint_ms, threshold_db
        self.initial_input_gain_db = gain

    # Extra helper arguments; the raw-audio engine overrides this.
    _engine_args: tuple = ()

    async def _spawn(self, args):
        directory = server = process = writer = None
        try:
            if not self.launch_app:
                return await asyncio.create_subprocess_exec(
                    *self.command, *args, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            # macOS TCC attributes direct descendants to their responsible GUI app.
            # LaunchServices gives SpeechHelper its own permission identity.
            directory = tempfile.TemporaryDirectory(prefix="aiv-", dir="/tmp")
            path = str(Path(directory.name) / "events.sock")
            connection = asyncio.get_running_loop().create_future()
            def accepted(reader, writer):
                if connection.done():
                    writer.close()
                else:
                    connection.set_result((reader, writer))
            server = await asyncio.start_unix_server(accepted, path=path)
            process = await asyncio.create_subprocess_exec(
                *self.command, "--event-socket", path, *args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            reader, writer = await asyncio.wait_for(connection, 10)
            try:
                hello = json.loads(await asyncio.wait_for(reader.readline(), 10))
            except (ValueError, UnicodeError) as exc:
                raise ASRError("Invalid SpeechHelper IPC handshake") from exc
            if not isinstance(hello, dict) or hello.get("event") != "hello" or type(hello.get("pid")) is not int or hello["pid"] <= 1:
                raise ASRError("Invalid SpeechHelper IPC handshake")
            return _AppProcess(process, reader, writer, server, directory, hello["pid"])
        except BaseException as exc:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if server is not None:
                server.close()
                await server.wait_closed()
            if directory is not None:
                directory.cleanup()
            if isinstance(exc, (OSError, asyncio.TimeoutError)):
                raise ASRError("Cannot connect SpeechHelper; run scripts/build-helper.sh") from exc
            raise

    async def doctor(self) -> dict:
        """Read capability and permission state without capture or authorization dialogs."""
        if sys.platform == "win32":
            # Windows handles microphone access when capture first starts.
            return {"event": "doctor", "input": self.input_device, "language": self.language,
                    "microphone_authorization": "authorized", "speech_authorization": "authorized",
                    "available": True, "supports_on_device": True, "requires_on_device": True,
                    "input_found": True, "capture_started": False}
        return await self._report(["--doctor", "--input", self.input_device, "--language", self.language], 10)

    async def authorize_only(self, kind: str) -> dict:
        """Ask for one permission (system dialog may wait for the user) and return the doctor report."""
        if sys.platform == "win32":
            return await SpeechASR.doctor(self)
        return await self._report(["--authorize", "--only", kind, "--input", self.input_device,
                                   "--language", self.language], 120)

    async def _report(self, args: list[str], timeout: float) -> dict:
        process = await self._spawn(args)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise ASRError(f"SpeechHelper doctor exited {process.returncode}: {stderr.decode(errors='replace')[:2000]}")
            try:
                report = json.loads(stdout)
            except (ValueError, UnicodeError) as exc:
                raise ASRError("SpeechHelper doctor returned malformed JSON") from exc
            if not isinstance(report, dict) or report.get("event") != "doctor":
                raise ASRError("SpeechHelper doctor returned an invalid report")
            return report
        finally:
            await self._stop(process)

    @staticmethod
    async def _stop(process):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if isinstance(process, _AppProcess):
            await process.cleanup()

    @staticmethod
    async def _stderr_tail(stream):
        tail = b""
        while chunk := await stream.read(4096):
            tail = (tail + chunk)[-2000:]
        return tail.decode(errors="replace").strip()

    @staticmethod
    def _parse(line: bytes) -> dict:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ASRError("SpeechHelper emitted malformed JSON") from exc
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise ASRError("SpeechHelper emitted an unknown event")
        kind = event["event"]
        if kind == "input_gain":
            db = event.get("db")
            if not isinstance(db, (int, float)) or isinstance(db, bool) or not math.isfinite(float(db)):
                raise ASRError("SpeechHelper input_gain event requires finite db")
            if float(db) < -24.0 or float(db) > 12.0:
                raise ASRError("SpeechHelper input_gain event db out of range")
            timing = event.get("time_ms")
            if type(timing) not in (int, float) or not 0 <= timing <= 2 ** 53 or not math.isfinite(timing):
                raise ASRError("SpeechHelper input_gain event requires finite monotonic time_ms")
            return event
        if kind not in {"ready", "partial", "final", "error", "vad_start", "vad_end", "audio"}:
            raise ASRError("SpeechHelper emitted an unknown event")
        if not isinstance(event.get("utterance_id"), str):
            raise ASRError("SpeechHelper event requires a string utterance_id")
        timing = event.get("time_ms")
        if type(timing) not in (int, float) or not 0 <= timing <= 2 ** 53 or not math.isfinite(timing):
            raise ASRError("SpeechHelper event requires finite monotonic time_ms")
        if kind == "audio" and not isinstance(event.get("pcm"), str):
            raise ASRError("SpeechHelper audio event requires pcm")
        if event["event"] in {"partial", "final"}:
            if not event["utterance_id"] or not isinstance(event.get("text"), str):
                raise ASRError("SpeechHelper transcript event is invalid")
        if event["event"] == "error":
            if not isinstance(event.get("message"), str) or not isinstance(event.get("code"), str) or type(event.get("fatal")) is not bool:
                raise ASRError("SpeechHelper error event is invalid")
            if event["fatal"]:
                raise ASRError(f"SpeechHelper {event['code']}: {event['message']}")
        return event

    async def set_input_gain(self, db):
        if isinstance(db, bool) or not isinstance(db, (int, float)):
            raise ValueError("input_gain_db must be a finite number")
        value = float(db)
        if value < -24.0 or value > 12.0 or not math.isfinite(value):
            raise ValueError("input_gain_db must be between -24 and 12 dB")
        if sys.platform == "win32":
            queue = getattr(self, "_capture_queue", None)
            if queue is None:
                raise ASRError("Microphone capture is not running; cannot change input gain")
            self._capture_gain_db = value
            await queue.put({"event": "input_gain", "db": value, "requested_db": value,
                             "utterance_id": "", "time_ms": time.monotonic() * 1000})
            return
        app = getattr(self, "_active_app", None)
        if app is None:
            raise ASRError("SpeechHelper is not running; cannot change input gain")
        payload = (json.dumps({"command": "input_gain", "db": value}) + "\n").encode("utf-8")
        try:
            app.writer.write(payload)
            await app.writer.drain()
        except (ConnectionError, BrokenPipeError, OSError) as exc:
            raise ASRError("Cannot send input_gain command to SpeechHelper") from exc

    async def apply_initial_input_gain(self, db):
        """Send the initial input gain to a running helper, with validation.

        Returns True when the helper acknowledged the value (or 0 dB was a no-op).
        Returns False when validation failed or the helper rejected the command.
        """
        if not isinstance(db, (int, float)) or isinstance(db, bool):
            return False
        value = float(db)
        if not math.isfinite(value) or value < -24.0 or value > 12.0:
            return False
        try:
            await self.set_input_gain(value)
            return True
        except ASRError:
            return False

    async def events(self, *, initial_input_gain_db: float | None = None) -> AsyncIterator[dict]:
        if initial_input_gain_db is None:
            initial_input_gain_db = self.initial_input_gain_db
        if not isinstance(initial_input_gain_db, (int, float)) or isinstance(initial_input_gain_db, bool):
            raise ValueError("initial_input_gain_db must be a finite number")
        value = float(initial_input_gain_db)
        if not math.isfinite(value) or value < -24.0 or value > 12.0:
            raise ValueError("initial_input_gain_db must be between -24 and 12 dB")
        if sys.platform == "win32":
            capture = self._capture_events(value)
            try:
                async for event in capture:
                    yield event
            finally:
                await capture.aclose()
            return
        process = await self._spawn(["--input", self.input_device, "--language", self.language,
                                     "--endpoint-ms", str(self.endpoint_ms),
                                     "--threshold-db", str(self.threshold_db),
                                     "--input-gain-db", f"{value:g}", *self._engine_args])
        self._active_app = process
        stderr_task = asyncio.create_task(self._stderr_tail(process.stderr))
        finals = set()
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise ASRError("SpeechHelper event exceeds the protocol limit") from exc
                if not line:
                    break
                event = self._parse(line)
                if event["event"] == "final":
                    if event["utterance_id"] in finals:
                        continue
                    finals.add(event["utterance_id"])
                yield event
            code = await process.wait()
            tail = await stderr_task
            if code:
                raise ASRError(f"SpeechHelper exited {code}: {tail or 'no diagnostic'}")
            if self.launch_app:
                raise ASRError("SpeechHelper stopped unexpectedly (LaunchServices does not forward its exit status)")
        finally:
            self._active_app = None
            await self._stop(process)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _capture_events(self, gain_db: float) -> AsyncIterator[dict]:
        import sounddevice as sd
        from .devices import stream_extra_settings
        from .vad import VAD

        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=256)
        self._capture_queue, self._capture_gain_db = queue, gain_db
        vad = VAD(min_silence_ms=self.endpoint_ms, threshold=self.threshold_db)
        uid = ""
        failed = False

        def enqueue(item):
            nonlocal failed
            if self._capture_queue is not queue or failed:
                return
            if queue.full():
                failed = True
                while not queue.empty():
                    queue.get_nowait()
                item = ASRError("Microphone capture queue overflow")
            queue.put_nowait(item)

        def callback(indata, frames, timing, status):
            if status:
                status.input_overflow = False
            if status:
                item = ASRError(f"Microphone capture: {status}")
            else:
                item = np.clip(indata[:, 0] * 10 ** (self._capture_gain_db / 20), -1, 1).astype(np.float32)
            try:
                loop.call_soon_threadsafe(enqueue, item)
            except RuntimeError:
                pass  # The event loop may already be closed during shutdown.

        def finished():
            try:
                loop.call_soon_threadsafe(enqueue, None)
            except RuntimeError:
                pass

        def event(kind, **fields):
            return {"event": kind, "utterance_id": uid,
                    "time_ms": time.monotonic() * 1000, **fields}

        try:
            device = None if self.input_device == "default" else self.input_device
            with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                device=device,
                                extra_settings=stream_extra_settings(device, kind="input"),
                                callback=callback, finished_callback=finished):
                yield event("ready", input=self.input_device, input_channels=1,
                            recognition_channels=1, recognition_channel=0, sample_rate=16000,
                            recognition_sample_rate=16000, language=self.language,
                            on_device=False, engine="raw", input_gain_db=gain_db)
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    if isinstance(item, dict):
                        yield item
                        continue
                    was_active = vad.active
                    segments = vad.feed(item)
                    if not was_active and vad.active:
                        uid = str(uuid.uuid4())
                        yield event("vad_start", preroll_ms=vad.preroll_ms,
                                    buffer_ms=sum(frame.size for frame in vad.frames) / 16,
                                    threshold_db=self.threshold_db)
                        audio = np.concatenate(vad.frames)
                    elif was_active:
                        audio = item
                    else:
                        continue
                    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                    yield event("audio", pcm=base64.b64encode(pcm).decode("ascii"))
                    if segments:
                        yield event("vad_end")
        except ASRError:
            raise
        except Exception as exc:
            raise ASRError(f"Microphone capture failed: {exc}") from exc
        finally:
            self._capture_queue = None
            vad.reset()


PERMISSION_KINDS = ("microphone", "speech")
PERMISSION_CACHE_SECONDS = 1.5
_UNKNOWN_PERMISSIONS = {"microphone": "unknown", "speech": "unknown"}
_PERM_LOCK: asyncio.Lock | None = None
_PERM_LOCK_LOOP = None
_PERM_CACHE: tuple[float, dict] | None = None


def map_permission(value: object) -> str:
    if value == "notDetermined":
        return "not_determined"
    return value if value in ("authorized", "denied", "restricted") else "unknown"


def _permission_report(report: dict) -> dict:
    return {"microphone": map_permission(report.get("microphone_authorization")),
            "speech": map_permission(report.get("speech_authorization"))}


def _permission_lock() -> asyncio.Lock:
    global _PERM_LOCK, _PERM_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _PERM_LOCK is None or _PERM_LOCK_LOOP is not loop:
        _PERM_LOCK, _PERM_LOCK_LOOP = asyncio.Lock(), loop
    return _PERM_LOCK


async def permission_status(*, helper=None, clock=time.monotonic) -> dict:
    """Microphone/speech permission states; each poll launches the helper, so cache for 1.5 s."""
    global _PERM_CACHE
    async with _permission_lock():
        if _PERM_CACHE is not None and clock() - _PERM_CACHE[0] < PERMISSION_CACHE_SECONDS:
            return dict(_PERM_CACHE[1])
        helper = helper or SpeechASR(input_device="default", language="en-US")
        try:
            result = _permission_report(await helper.doctor())
        except (ASRError, OSError, asyncio.TimeoutError):
            result = dict(_UNKNOWN_PERMISSIONS)
        _PERM_CACHE = (clock(), result)
        return dict(result)


async def request_permission(kind: str, *, helper=None, clock=time.monotonic) -> dict:
    global _PERM_CACHE
    if kind not in PERMISSION_KINDS:
        raise ValueError(t("errors.permission_kind_invalid"))
    async with _permission_lock():
        helper = helper or SpeechASR(input_device="default", language="en-US")
        try:
            result = _permission_report(await helper.authorize_only(kind))
        except (ASRError, OSError, asyncio.TimeoutError):
            result = dict(_UNKNOWN_PERMISSIONS)
        _PERM_CACHE = (clock(), result)
        return dict(result)


GIGAAM_MODEL = "gigaam-v3-e2e-ctc"
MAX_UTTERANCE_SECONDS = 30
_models: dict = {}


def engine_for_language(lang: str, pref: str | None = None,
                        platform: str = sys.platform) -> Literal["apple", "gigaam", "whisper"]:
    if platform == "win32":
        return "gigaam" if lang.lower().startswith("ru") else "whisper"
    return "gigaam" if pref == "gigaam" else "apple"


def load_gigaam(name: str = GIGAAM_MODEL):
    """Load (and on first use download, ~200 MB) the Sber GigaAM ONNX model; cached per process."""
    if name not in _models:
        try:
            import onnx_asr
        except ImportError as exc:
            raise ASRError(t("errors.gigaam_install")) from exc
        try:
            # Windowed app has no valid stderr; HF's tqdm progress bar then fails with [Errno 22].
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            model = onnx_asr.load_model(name, quantization="int8")
            model.recognize(np.zeros(16000, dtype=np.float32), sample_rate=16000)  # warm-up: first call compiles ~4 s
            _models[name] = model
        except Exception as exc:
            log.exception("GigaAM load failed")
            raise ASRError(t("errors.gigaam_load", exc=exc)) from exc
    return _models[name]


class GigaAMASR(SpeechASR):
    """Sber GigaAM running locally; the helper only captures and cuts utterances by VAD."""
    _engine_args = ("--engine", "raw")

    def __init__(self, *args, model: str = GIGAAM_MODEL, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model

    def _load_model(self):
        return load_gigaam(self.model)

    async def doctor(self) -> dict:
        report = await super().doctor()
        await asyncio.to_thread(self._load_model)
        return report

    def _transcribe(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        return str(self._load_model().recognize(audio, sample_rate=16000)).strip()

    async def events(self, *, initial_input_gain_db: float | None = None) -> AsyncIterator[dict]:
        await asyncio.to_thread(self._load_model)
        chunks: dict[str, bytearray] = {}
        async for event in super().events(initial_input_gain_db=initial_input_gain_db):
            kind, uid = event["event"], event.get("utterance_id")
            if kind == "audio":
                try:
                    data = base64.b64decode(event["pcm"], validate=True)
                except ValueError as exc:
                    raise ASRError("SpeechHelper audio event is not valid base64") from exc
                buf = chunks.setdefault(uid, bytearray())
                if len(buf) + len(data) <= MAX_UTTERANCE_SECONDS * 32000:
                    buf += data
                continue
            yield event
            if kind == "vad_end":
                pcm = bytes(chunks.pop(uid, b""))
                if len(pcm) < 3200:  # under 0.1 s: click or noise
                    continue
                try:
                    text = await asyncio.to_thread(self._transcribe, pcm)
                except Exception as exc:
                    yield {"event": "error", "utterance_id": uid, "code": "recognition",
                           "message": f"GigaAM: {exc}", "fatal": False,
                           "time_ms": time.monotonic() * 1000}
                    continue
                if text:
                    yield {"event": "final", "utterance_id": uid, "text": text,
                           "time_ms": time.monotonic() * 1000}


class WhisperASR(GigaAMASR):
    """Local Whisper recognition using the same capture and VAD event stream."""

    def __init__(self, *args, model: str = "small", device: str = "auto", **kwargs):
        super().__init__(*args, model=model, **kwargs)

    def _load_model(self):
        return load_gigaam(f"onnx-community/whisper-{self.model}")

    def _transcribe(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        language = self.language.lower().replace("_", "-").split("-", 1)[0]
        return str(self._load_model().recognize(audio, sample_rate=16000, language=language)).strip()
