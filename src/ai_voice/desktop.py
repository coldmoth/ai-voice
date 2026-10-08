"""Authenticated localhost bridge for the native macOS AI Voice window."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from .i18n import t
from .preview import PreviewPlayer

from . import __version__, asr, devices, driver, i18n, updates, vc_engine
from .catalog import Catalog, _is_monitor_blacklisted, resolve_speech_language, valid_id
from .paths import DATA, WEB
from .storage import StorageBadRequest, StorageConflict, StorageService, cleanup_old_logs
from .desktop_control import ConflictError, DesktopControl, NotFoundError
from .devices import default_input_name, list_devices, preferred_virtual_output, virtual_outputs
from .vc_control import VcController
from .vc_store import VcStore, VcStoreError
from .hf_catalog import HfCatalogError
from . import keys as api_keys
from .secrets import migrate_legacy


MAX_BODY_BYTES = 65536
KEY_BODY_LIMIT = 1024
logger = logging.getLogger(__name__)


def parent_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _audio_idle(server) -> bool:
    state = server.control.snapshot()
    return (not state.get("active") and not state.get("monitor_active")
            and server.preview.snapshot().get("preview_state") != "playing")


def render_page(folder, token):
    """Assemble the single page: stylesheets and scripts are inlined because plain <script src> cannot send the token header."""
    read = lambda name: (folder / name).read_text(encoding="utf-8")
    script = "const APP_TOKEN = " + json.dumps(token) + ";\n" + read("app.js") + "\n" + read("onboarding.js")
    page = read("index.html").replace("__STYLE__", read("style.css") + "\n" + read("onboarding.css"))
    return page.replace("__SCRIPT__", script).encode()


class DesktopHandler(BaseHTTPRequestHandler):
    upload_timeout = 60

    def log_message(self, *_):
        pass

    def respond(self, status, data, mime="application/json; charset=utf-8", cache=False):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        for name, value in {"Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "private, max-age=3600" if cache else "no-store",
                            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://public-platform.r2.fish.audio; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self):
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        token = self.headers.get("X-AI-Voice-Token", "")
        if urlsplit(self.path).path == "/":
            token = parse_qs(urlsplit(self.path).query).get("token", [token])[0]
        return (self.headers.get("Host") == host and (origin is None or origin == "http://" + host)
                and token.isascii() and secrets.compare_digest(token, self.server.token))

    def do_GET(self):
        if not self.allowed():
            return self.respond(403, {"error": t("errors.window_access_denied")})
        path = urlsplit(self.path)
        try:
            if path.path == "/health":
                return self.respond(200, {"ok": True})
            if path.path == "/api/vc/engine":
                return self.respond(200, vc_engine.installer().state())
            if path.path.startswith("/locales/"):
                name = path.path.removeprefix("/locales/")
                if name not in ("en.json", "ru.json"):
                    return self.respond(404, {"error": "Locale not found"})
                return self.respond(200, i18n.load(name.removesuffix(".json")), cache=True)
            if path.path == "/":
                return self.respond(200, render_page(WEB, self.server.token), "text/html; charset=utf-8")
            if path.path == "/api/status":
                status = self.server.control.snapshot()
                status["version"] = __version__
                status["language"] = self.server.catalog.preferences().get("language", "en")
                preview = self.server.preview.snapshot()
                status.update(preview)
                if "clickedid" in preview:
                    status["clickedid"] = preview["clickedid"]
                if "preview_state" in preview:
                    status["preview_state"] = preview["preview_state"]
                if "preview_error" in preview:
                    status["preview_error"] = preview["preview_error"]
                if "preview_id" in preview:
                    status["preview_id"] = preview["preview_id"]
                return self.respond(200, status)
            if path.path == "/api/voices":
                devices = {"inputs": [], "outputs": [], "monitors": []}
                seen_outputs = set()
                for device in list_devices():
                    name = device.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    if device.get("max_input_channels", 0) >= 1 and name not in devices["inputs"]:
                        devices["inputs"].append(name)
                    if device.get("max_output_channels", 0) >= 2 and name not in devices["outputs"]:
                        devices["outputs"].append(name)
                        seen_outputs.add(name)
                # Monitors may be either an output or duplex hardware sink; we
                # exclude anything that looks like the AI Voice virtual sink,
                # microphones, or other loopback/virtual helpers.
                for device in list_devices():
                    name = device.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    if device.get("max_output_channels", 0) < 2:
                        continue
                    if name in devices["monitors"]:
                        continue
                    if _is_monitor_blacklisted(name):
                        continue
                    devices["monitors"].append(name)
                devices["default_input"] = default_input_name()
                devices["default_output"] = preferred_virtual_output(devices["outputs"])
                devices["virtual"] = virtual_outputs(devices["outputs"])
                self.server.catalog.start_metadata()
                preferences = self.server.catalog.preferences()
                snapshot = asr.locales_snapshot()
                return self.respond(200, {"items": self.server.catalog.voices(), "devices": devices,
                                          "platform": "win" if sys.platform == "win32" else "mac",
                                          "speech_languages": [{"id": x} for x in snapshot["supported"]],
                                          "speech_language": resolve_speech_language(
                                              preferences.get("speech_language"), snapshot["system"], snapshot["supported"]),
                                          "preferences": preferences, "language": preferences.get("language", "en")})
            if path.path == "/api/vc/hf/search":
                query = parse_qs(path.query)
                return self.respond(200, self.server.vc.hf_catalog.search(
                    q=query.get('q', [''])[0], sort=query.get('sort', ['downloads'])[0],
                    lang=query.get('lang', ['all'])[0], page=int(query.get('page', ['1'])[0])))
            if path.path in ("/api/vc/hf/readme", "/api/vc/hf/sample", "/api/vc/hf/image"):
                query=parse_qs(path.query); card_id=query.get('id',[''])[0]
                if not card_id: raise ValueError(t("errors.card_id_invalid"))
                if path.path.endswith('/readme'): return self.respond(200,self.server.vc.hf_catalog.readme(card_id))
                if path.path.endswith('/sample'): body,mime=self.server.vc.hf_catalog.sample(card_id)
                else:
                    try: n=int(query.get('n',['-1'])[0])
                    except ValueError: raise ValueError(t("errors.image_number_invalid"))
                    body,mime=self.server.vc.hf_catalog.image(card_id,n)
                self.respond(200,body,mime,cache=True); return
            if path.path == "/api/vc/catalog/download":
                return self.respond(200, self.server.vc.catalog_download_status())
            if path.path == "/api/vc/voices":
                if not hasattr(self.server, "vc"):
                    return self.respond(404, {"error": t("errors.page_not_found")})
                return self.respond(200, self.server.vc.voices())
            if path.path == "/api/permissions":
                future = asyncio.run_coroutine_threadsafe(asr.permission_status(), self.server.loop)
                try:
                    return self.respond(200, future.result(15))
                except FutureTimeout:
                    future.cancel()
                    return self.respond(200, dict(asr._UNKNOWN_PERMISSIONS))
            if path.path == "/api/update-status":
                return self.respond(200, updates.job())
            if path.path == "/api/driver/status":
                names = [d["name"] for d in list_devices() if d.get("max_output_channels", 0) >= 2]
                return self.respond(200, driver.status(names))
            if path.path == "/api/keys":
                return self.respond(200, api_keys.key_status())
            if path.path == "/api/storage":
                try:
                    return self.respond(200, self.server.storage.get())
                except TimeoutError:
                    return self.respond(503, {"error": t("errors.storage_count_timeout")})
            if path.path == "/api/voice_metadata":
                return self.respond(200, {"items": self.server.catalog.voices(),
                                          "pending": self.server.catalog.metadata_pending})
            if path.path == "/api/search":
                query = parse_qs(path.query)
                args = [query.get("q", [""])[0], int(query.get("page", ["1"])[0])]
                kwargs = {}
                if "gender" in query:
                    kwargs["gender"] = query["gender"][0]
                if "sort_by" in query:
                    kwargs["sort_by"] = query["sort_by"][0]
                return self.respond(200, self.server.catalog.search(*args, **kwargs))
            return self.respond(404, {"error": t("errors.page_not_found")})
        except HfCatalogError as exc:
            status = 502 if path.path in ('/api/vc/hf/readme', '/api/vc/hf/sample', '/api/vc/hf/image') else 503
            return self.respond(status, {"error": str(exc)})
        except (ValueError, RuntimeError, OSError) as exc:
            return self.respond(400, {"error": str(exc)})
        except Exception:
            return self.respond(500, {"error": t("errors.audio_process")})

    def do_POST(self):
        if not self.allowed():
            return self.respond(403, {"error": t("errors.window_access_denied")})
        try:
            path = urlsplit(self.path).path
            if path in ("/api/vc/engine/install", "/api/vc/engine/cancel", "/api/vc/engine/remove"):
                engine = vc_engine.installer()
                try:
                    if path.endswith(("/install", "/remove")):
                        vc = getattr(self.server, "vc", None)
                        training = vc.trainer.status() if vc is not None else None
                        if vc is not None and (vc.busy or (isinstance(training, dict) and
                                (training.get("state") in ("running", "paused") or training.get("queue")))):
                            raise RuntimeError("busy")
                    action = path.rsplit("/", 1)[1]
                    result = engine.start() if action == "install" else getattr(engine, action)()
                    if hasattr(self.server, "storage"):
                        self.server.storage.invalidate()
                    return self.respond(200, result)
                except RuntimeError as exc:
                    code = str(exc)
                    if code not in {"busy", "network", "checksum", "disk", "uv", "smoke", "dev_runtime", "unsupported"}:
                        logger.warning("VC engine: %s", code[:300])
                        code = "disk"
                    return self.respond(409 if code == "busy" else 400, {"error": code})
            if path in ("/api/vc/import", "/api/vc/create"):
                if not hasattr(self.server, "vc"):
                    return self.respond(404, {"error": t("errors.command_not_found")})
                length = int(self.headers.get("Content-Length", "0"))
                ct = self.headers.get("Content-Type", "")
                if not ct.startswith("multipart/form-data"):
                    raise ValueError(t("errors.interface_request_invalid"))
                # A stalled client must not hold the thread and the temp upload forever.
                self.connection.settimeout(self.upload_timeout)
                if path == "/api/vc/import":
                    result = self.server.vc.import_model(ct, self.rfile, length)
                else:
                    result = self.server.vc.create_voice(ct, self.rfile, length)
                return self.respond(200, result)
            length = int(self.headers.get("Content-Length", "0"))
            if path.startswith("/api/keys/") and length > KEY_BODY_LIMIT:
                return self.respond(400, {"error": t("errors.interface_request_invalid")})
            if path == "/api/vc/train/cancel" and length == 0:
                data = {}
            else:
                if not 0 < length <= MAX_BODY_BYTES or not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise ValueError(t("errors.interface_request_invalid"))
                data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError(t("errors.command_invalid"))
            path = urlsplit(self.path).path
            if path in ('/api/vc/speak', '/api/vc/speak/cancel'):
                async def speak_command():
                    if path.endswith('/cancel'):
                        self.server.vc.cancel_text()
                        return {}
                    return await self.server.control.vc_speak(data.get('text'))
                future = asyncio.run_coroutine_threadsafe(speak_command(), self.server.loop)
                try:
                    return self.respond(200, future.result(timeout=30))
                except ConflictError as exc:
                    return self.respond(409, {'error': str(exc)})
                except ValueError as exc:
                    return self.respond(400, {'error': str(exc)})
                except Exception as exc:
                    future.cancel()
                    return self.respond(502, {'error': str(exc)})
            if path == '/api/vc/check':
                future=asyncio.run_coroutine_threadsafe(self.server.control.vc_speak(t("main.voice_test_text"), target='monitor'), self.server.loop)
                try: return self.respond(200, future.result(timeout=30))
                except ConflictError as exc: return self.respond(409, {'error':str(exc)})
                except ValueError as exc: return self.respond(400, {'error':str(exc)})
                except Exception as exc:
                    future.cancel()
                    return self.respond(502, {'error':str(exc)})
            if path == '/api/vc/catalog/download':
                return self.respond(200, self.server.vc.catalog_download(data.get('id')))
            if path == '/api/vc/catalog/download/cancel':
                return self.respond(200, self.server.vc.catalog_download_cancel())
            if path == "/api/permissions/request":
                kind = data.get("kind")
                if kind not in asr.PERMISSION_KINDS:
                    raise ValueError(t("errors.permission_kind_invalid"))
                future = asyncio.run_coroutine_threadsafe(asr.request_permission(kind), self.server.loop)
                try:
                    return self.respond(200, future.result(125))
                except FutureTimeout:
                    future.cancel()
                    return self.respond(200, dict(asr._UNKNOWN_PERMISSIONS))
            if path in ("/api/keys/fish", "/api/keys/hf"):
                kind = path.rsplit("/", 1)[1]
                result = api_keys.save(kind, data.get("key" if kind == "fish" else "token"))
                del data
                return self.respond(200, result)
            if path in ("/api/keys/fish/remove", "/api/keys/hf/remove"):
                return self.respond(200, api_keys.remove(path.split("/")[3]))
            if path == "/api/update-check":
                force = data.get("force", False)
                if not isinstance(force, bool):
                    raise ValueError(t("errors.interface_request_invalid"))
                state = self.server.catalog.update_state()
                result, new_state = updates.check(
                    state, self.server.catalog.preferences()["update_auto"], now=time.time(), force=force)
                if new_state != state:
                    self.server.catalog.set_update_state(new_state)
                return self.respond(200, result)
            if path == "/api/update-install":
                latest = self.server.catalog.update_state()["latest"]
                if latest is None or not updates.is_newer(latest["version"], __version__):
                    return self.respond(400, {"error": t("errors.update_unavailable")})
                if not updates.start_install(latest["version"]):
                    return self.respond(409, {"error": t("errors.update_busy")})
                return self.respond(200, {"ok": True})
            if path == "/api/update-skip":
                version = data.get("version")
                if updates.parse_version(version) is None:
                    raise ValueError(t("errors.update_version_invalid"))
                state = self.server.catalog.update_state()
                state["skipped"] = ".".join(map(str, updates.parse_version(version)))
                self.server.catalog.set_update_state(state)
                return self.respond(200, {"ok": True})
            if path == "/api/devices/rescan":
                if driver.job()["state"] == "running" or not _audio_idle(self.server):
                    return self.respond(409, {"error": t("errors.devices_busy")})
                devices.rescan()
                return self.respond(200, {"ok": True})
            if path in ("/api/driver/install", "/api/driver/uninstall"):
                action = path.rsplit("/", 1)[1]
                if sys.platform == "win32":
                    if action != "install":
                        return self.respond(400, {"error": t("errors.driver_unavailable")})
                    with driver._lock:
                        if driver._job["state"] == "running":
                            return self.respond(409, {"error": t("errors.driver_busy")})
                        if not _audio_idle(self.server):
                            return self.respond(409, {"error": t("errors.devices_busy")})
                        driver._job.update(state="running", action=action)
                    state = "failed"
                    try:
                        result = driver.install_cable(
                            rescan=lambda: devices.rescan() if _audio_idle(self.server) else None)
                        state = "done" if result["status"] in ("installed", "reboot") else result["status"]
                        return self.respond(200, result)
                    finally:
                        with driver._lock:
                            driver._job.update(state=state)
                if driver.job()["state"] == "running":
                    return self.respond(409, {"error": t("errors.driver_busy")})
                if action == "install" and driver.bundled() is None:
                    return self.respond(400, {"error": t("errors.driver_unavailable")})

                def before():
                    for player in (self.server.control, self.server.preview):
                        try:
                            asyncio.run_coroutine_threadsafe(
                                player.stop(), self.server.loop).result(timeout=10)
                        except Exception as exc:
                            logger.warning("Driver audio stop failed: %s", str(exc)[:300])

                def after():
                    for attempt in range(5 if action == "install" else 1):
                        if action == "uninstall" or attempt:
                            time.sleep(2)
                        if _audio_idle(self.server):
                            devices.rescan()
                        if action == "install" and driver.DEVICE in [
                                d["name"] for d in list_devices() if d.get("max_output_channels", 0) >= 2]:
                            break

                if not driver.start(action, before=before, after=after):
                    return self.respond(409, {"error": t("errors.driver_busy")})
                return self.respond(200, {"ok": True})
            if path == "/api/control":
                future = asyncio.run_coroutine_threadsafe(self.server.control.command(data), self.server.loop)
                try:
                    result = future.result(timeout=30)
                except FutureTimeout:
                    future.cancel()
                    raise ValueError(t("errors.command_timeout")) from None
                return self.respond(200, result)
            if hasattr(self.server, "vc") and path == "/api/vc/meter":
                on = data.get("on")
                if not isinstance(on, bool):
                    raise ValueError("on must be a boolean")
                try:
                    result = self.server.vc.meter_heartbeat(
                        on, self.server.catalog.preferences().get("input_device"))
                except RuntimeError as exc:
                    return self.respond(409, {"error": str(exc)})
                return self.respond(200, result)
            if hasattr(self.server, "vc") and path == "/api/vc/gate/calibrate":
                try:
                    input_device = self.server.catalog.preferences().get("input_device")
                    if input_device is None:
                        input_device = default_input_name()
                    result = self.server.vc.calibrate_gate(input_device)
                    future = asyncio.run_coroutine_threadsafe(
                        self.server.control.apply_preferences({"vc_gate_enabled": True,
                                                               "vc_gate_db": result["gate_db"]}),
                        self.server.loop)
                    try:
                        future.result(timeout=30)
                    except FutureTimeout:
                        future.cancel()
                        raise RuntimeError(t("errors.preferences_apply")) from None
                except RuntimeError as exc:
                    return self.respond(409, {"error": str(exc)})
                return self.respond(200, result)
            if hasattr(self.server, "vc") and path == "/api/vc/autotune":
                voice_id = data.get("voice_id")
                if not isinstance(voice_id, str) or not voice_id:
                    raise ValueError(t("errors.voice_id_invalid"))
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.server.control.reap_failed_vc(), self.server.loop).result(timeout=10)
                except FutureTimeout:
                    raise ValueError(t("errors.voice_stopping")) from None
                future = asyncio.run_coroutine_threadsafe(self.server.vc.autotune(voice_id), self.server.loop)
                try:
                    return self.respond(200, future.result(timeout=105))
                except FutureTimeout:
                    future.cancel()
                    raise ValueError(t("errors.autotune_incomplete")) from None
            if hasattr(self.server, "vc") and path in ("/api/vc/train/pause", "/api/vc/train/resume"):
                if path == "/api/vc/train/pause":
                    return self.respond(200, self.server.vc.pause_training())
                return self.respond(200, self.server.vc.resume_training())
            if hasattr(self.server, "vc") and path in ("/api/vc/train", "/api/vc/train/move", "/api/vc/train/cancel", "/api/vc/rename",
                                                       "/api/vc/delete", "/api/vc/params"):
                voice_id = data.get("voice_id")
                if path == "/api/vc/train/cancel":
                    return self.respond(200, self.server.vc.cancel_training(voice_id))
                if not isinstance(voice_id, str) or not voice_id:
                    raise ValueError(t("errors.voice_id_invalid"))
                if path == '/api/vc/train':
                    return self.respond(200, self.server.vc.train(voice_id, data.get('preset')))
                if path == '/api/vc/train/move':
                    return self.respond(200, self.server.vc.move_training(voice_id, data.get('direction')))
                if path == "/api/vc/rename":
                    name = data.get("name")
                    return self.respond(200, self.server.vc.rename(voice_id, name))
                if path == "/api/vc/delete":
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.server.control.reap_failed_vc(), self.server.loop).result(timeout=10)
                    except FutureTimeout:
                        raise ValueError(t("errors.voice_stopping")) from None
                    future = asyncio.run_coroutine_threadsafe(
                        self.server.vc.delete(voice_id), self.server.loop)
                    try:
                        return self.respond(200, future.result(timeout=10))
                    except FutureTimeout:
                        future.cancel()
                        raise ValueError(t("errors.voice_remove")) from None
                kwargs = {k: data[k] for k in ("pitch_shift", "index_rate", "block_ms", "diffusion_steps", "ref_seconds")
                          if data.get(k) is not None}
                if not kwargs:
                    raise ValueError(t("errors.voice_parameters_missing"))
                return self.respond(200, self.server.vc.set_params(voice_id, **kwargs))
            if path == "/api/preferences":
                if self.server.control.snapshot()["active"] and any(k in data for k in ["input_device", "output_device"]):
                    raise ValueError(t("errors.stop_audio_device"))
                future = asyncio.run_coroutine_threadsafe(
                    self.server.control.apply_preferences(data), self.server.loop)
                try:
                    prefs = future.result(timeout=30)
                except FutureTimeout:
                    future.cancel()
                    raise ValueError(t("errors.preferences_apply")) from None
                return self.respond(200, prefs)
            if path == "/api/storage/clear":
                category, item_id = data.get("category"), data.get("item_id")
                if not isinstance(category, str):
                    raise ValueError(t("errors.category_invalid"))
                try:
                    return self.respond(200, self.server.storage.clear(category, item_id))
                except StorageConflict as exc:
                    return self.respond(409, {"error": str(exc)})
                except StorageBadRequest as exc:
                    return self.respond(400, {"error": str(exc)})
                except TimeoutError:
                    return self.respond(503, {"error": t("errors.storage_count_timeout")})
            if path == "/api/add_voice":
                return self.respond(200, self.server.catalog.add(data.get("id")))
            if path == "/api/remove_voice":
                voice_id = data.get("id")
                if not isinstance(voice_id, str) or not valid_id(voice_id):
                    raise ValueError(t("errors.voice_id_invalid"))
                future = asyncio.run_coroutine_threadsafe(
                    self.server.control.remove_voice(voice_id), self.server.loop)
                try:
                    result = future.result(timeout=10)
                except FutureTimeout:
                    future.cancel()
                    raise ValueError(t("errors.voice_remove")) from None
                return self.respond(200, result)
            if path == "/api/remove_voices":
                ids = data.get("ids")
                if not isinstance(ids, list) or any(not valid_id(v) for v in ids):
                    raise ValueError(t("errors.voice_list_invalid"))
                future = asyncio.run_coroutine_threadsafe(
                    self.server.control.remove_voices(ids), self.server.loop)
                try:
                    result = future.result(timeout=10)
                except FutureTimeout:
                    future.cancel()
                    raise ValueError(t("errors.voice_remove")) from None
                return self.respond(200, result)
            if path == "/api/preview":
                action = data.get("action")
                player = self.server.preview
                if action == "start":
                    voice_id = data.get("id")
                    from .preview import _resolve_voice_id
                    resolved = _resolve_voice_id(voice_id)
                    if resolved is None:
                        raise ValueError(t("errors.voice_id_invalid"))
                    monitor_prefs = self.server.catalog.preferences()
                    monitor_device = monitor_prefs.get("monitor_device")
                    future = asyncio.run_coroutine_threadsafe(
                        player.start(resolved, device_name=monitor_device,
                                     gain_db=monitor_prefs.get("monitor_gain_db", 0.0)),
                        self.server.loop)
                    try:
                        await_handle = future
                        snapshot = None
                        try:
                            await_handle.result(timeout=10)
                        except FutureTimeout:
                            await_handle.cancel()
                            raise ValueError(t("errors.preview_start")) from None
                        snapshot = player.snapshot()
                    except ValueError:
                        raise
                    return self.respond(200, snapshot)
                if action == "stop":
                    future = asyncio.run_coroutine_threadsafe(player.stop(), self.server.loop)
                    try:
                        future.result(timeout=5)
                    except FutureTimeout:
                        future.cancel()
                        raise ValueError(t("errors.preview_stop")) from None
                    return self.respond(200, player.snapshot())
                raise ValueError(t("errors.preview_command"))
            return self.respond(404, {"error": t("errors.command_not_found")})
        except NotFoundError:
            return self.respond(404, {"error": "not_found"})
        except ConflictError as exc:
            return self.respond(409, {"error": str(exc)})
        except VcStoreError as exc:
            return self.respond(400, {"error": str(exc)})
        except (ValueError, RuntimeError, OSError) as exc:
            return self.respond(400, {"error": str(exc)})
        except Exception:
            return self.respond(500, {"error": t("errors.audio_process")})


def main():
    migrate_legacy()
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    catalog = Catalog()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), DesktopHandler)
    server.catalog, server.loop = catalog, loop
    server.vc = VcController(VcStore())
    server.control = DesktopControl(catalog, vc=server.vc)
    server.preview = PreviewPlayer()

    def run_async(coro, timeout=10):
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise ValueError(t("errors.storage_delete")) from None
    server.storage = StorageService(vc=server.vc, control=server.control, catalog=catalog, run_async=run_async)
    cleanup_old_logs(DATA / "logs")
    server.token = secrets.token_urlsafe(32)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/?token={server.token}"}), flush=True)
    stopped = threading.Event()

    def shutdown(*_):
        if stopped.is_set():
            return
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in [signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, shutdown)

    def watch_parent():
        while not stopped.wait(1):
            if not parent_alive(args.parent_pid):
                shutdown()
                return
    if args.parent_pid:
        threading.Thread(target=watch_parent, daemon=True).start()
    try:
        threading.Thread(target=lambda: asyncio.run(asr.supported_locales()), daemon=True).start()
        server.serve_forever()
    finally:
        stopped.set()
        try:
            server.vc.close()
        except Exception:
            pass
        asyncio.run_coroutine_threadsafe(server.control.stop(), loop).result(timeout=10)
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
