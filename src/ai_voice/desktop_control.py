"""One audio owner and serialized desktop commands; no capture on startup."""
import asyncio
import collections
import time
from dataclasses import replace
import math
import threading
from pathlib import Path

from .i18n import t
from .config import VOICES
from . import asr as speech_asr
from .instance import AudioLease
from .cli import _config, _devices, _helper, _permissions
from .devices import default_input_name, list_devices, preferred_virtual_output, resolve_output
from .gui_audio import AudioSession
from .monitor import MonitorSink
from .catalog import _is_monitor_blacklisted, resolve_speech_language
from .secrets import load_key
from .usage import UsageTracker

MAX_TRANSCRIPT_CHARS = 240


def _bounded(value, limit=MAX_TRANSCRIPT_CHARS):
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _sanitize_transcript(value):
    if not isinstance(value, str):
        return ""
    clean = "".join(ch for ch in value if ch == "\n" or ord(ch) >= 0x20)
    if len(clean) > MAX_TRANSCRIPT_CHARS:
        clean = clean[:MAX_TRANSCRIPT_CHARS - 1].rstrip() + "…"
    return clean


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class DesktopControl:
    def __init__(self, catalog, vc=None):
        self.catalog = catalog
        self.vc = vc
        self.session = self.task = None
        self._lease = None
        self.lock = asyncio.Lock()
        self.state_lock = threading.RLock()
        self.state = {"state": "stopped", "message": t("status.stopped"), "active": False, "mode": "mic",
                      "voice_id": catalog.data["voice_id"],
                      "vc_voice_id": None,
                      "transcript_partial": "", "transcript_final": "",
                      "monitor_active": False,
                      "generation_seq": 0, "latency_seconds": None, "latency": None,
                      "preview_id": None, "preview_state": "idle", "preview_error": None,
                      "input_gain_supported": True, "input_gain_db": 0.0, "input_gain_error": None}
        self.usage = UsageTracker(Path(catalog.path).parent / "usage.json" if getattr(catalog, "path", None) else None)
        self._usage_level = 0
        self._history = collections.deque(maxlen=15)
        self._history_seq = 0
        self._monitor = None
        self._monitor_error = None
        self._vc_resync_task = None
        self._vc_training_paused = False
        self._vc_starting = False
        self._generation_seq = 0
        self._last_latency = None
        self._preview_lock = threading.Lock()
        self._preview_id_seq = 0
        self._preview_active = None
        self._preview_last = None
        self._preview_error = None

    @property
    def lease(self):
        if self._lease is None:
            self._lease = AudioLease()
        return self._lease

    def notify(self, state, message=""):
        labels = {"stopped": t("status.stopped"), "starting": t("status.starting"), "listening": t("status.listening"),
                  "ready": t("status.ready"), "synthesizing": t("status.synthesizing"),
                  "playing": t("status.playing"), "warning": t("status.warning"), "error": t("status.error")}
        with self.state_lock:
            self.state.update(state=state, message=message or labels.get(state, state))

    def update_transcript(self, *, partial=None, final=None, reset=False):
        with self.state_lock:
            if reset:
                self.state["transcript_partial"] = ""
                self.state["transcript_final"] = ""
                return
            if final is not None:
                self.state["transcript_final"] = _bounded(_sanitize_transcript(final))
                self.state["transcript_partial"] = ""
            elif partial is not None:
                clean = _sanitize_transcript(partial)
                self.state["transcript_partial"] = _bounded(clean)

    def snapshot(self):
        with self.state_lock:
            data = dict(self.state)
            data["generation_seq"] = self._generation_seq
            data["latency_seconds"] = self._last_latency
            data["history"] = list(reversed(self._history))
            data["usage"] = self.usage.totals(self.catalog.preferences().get("monthly_char_limit"))
            preview_id = self._preview_id_seq
            data["preview_id"] = preview_id if self._preview_active is not None else self.state.get("preview_id")
            if self._preview_active is not None:
                data["preview_state"] = "playing"
            elif self._preview_error is not None:
                data["preview_state"] = "error"
            else:
                data["preview_state"] = "idle"
            data["preview_error"] = self._preview_error
            monitor_active = (self._monitor is not None
                              and getattr(self._monitor, "_active", True)
                              and not self._monitor_error)
            data["monitor_enabled"] = monitor_active
            data["monitor_active"] = monitor_active
            if self.vc is not None:
                data.update(self.vc.snapshot())
                if isinstance(data.get("vc"), dict):
                    data["vc"]["swap_enabled"] = bool(self.catalog.preferences().get("swap_enabled", False))
                if (data.get("mode") == "vc" and data.get("active") and not self._vc_starting
                        and (self.vc.worker_failed()
                             or (self.vc.active_voice is None and not self.vc.loading))):
                    self._resume_training()
                    data['training'] = self.vc.trainer.status()
                    vc_snap = data.get("vc", {}) if isinstance(data.get("vc"), dict) else {}
                    err = vc_snap.get("error") if isinstance(vc_snap, dict) else None
                    data["state"] = "error"
                    data["message"] = err or t("status.voice_process_stopped")
                    data["active"] = False
            if self._monitor is not None:
                data["monitor_actual_rate"] = self._monitor.actual_rate()
                data["monitor_dropped_frames"] = self._monitor.dropped_frames
                data["monitor_errors"] = self._monitor.errors
                if not monitor_active and self._monitor.errors and not self._monitor_error:
                    data["monitor_error"] = t("status.monitor_stopped")
            if self._monitor_error:
                data["monitor_error"] = self._monitor_error
            return data

    def _monitor_validation_error(self, prefs):
        monitor_enabled = bool(prefs.get("monitor_enabled", False))
        monitor_device = prefs.get("monitor_device")
        if not monitor_enabled:
            return None
        if not monitor_device or not isinstance(monitor_device, str):
            return t("errors.monitor_device_required")
        if monitor_device == prefs.get("output_device"):
            return t("errors.monitor_discord_device")
        if _is_monitor_blacklisted(monitor_device):
            return t("errors.monitor_unavailable")
        try:
            resolve_output(monitor_device)
        except ValueError as exc:
            return t("errors.monitor_device_unavailable", exc=exc)
        return None

    def _start_monitor_for_session(self, prefs):
        if not prefs.get("monitor_enabled", False):
            return None
        device = prefs.get("monitor_device")
        if not device or not isinstance(device, str):
            return None
        gain_db = float(prefs.get("monitor_gain_db", 0.0))
        monitor = MonitorSink(device=device,
                              sample_rate=self.session.engine.config.output_sample_rate,
                              gain=10 ** (gain_db / 20.0))
        try:
            monitor.start()
        except Exception as exc:
            self._monitor_error = t("errors.monitor_open", exc=exc)
            with self.state_lock:
                self.state["monitor_active"] = False
            return None
        self._monitor_error = None
        with self.state_lock:
            self.state["monitor_active"] = True
        self._monitor = monitor
        self.session.engine.playback.attach_monitor(monitor)
        return monitor

    def _stop_monitor(self):
        monitor = self._monitor
        self._monitor = None
        if monitor is not None:
            if self.session is not None:
                try:
                    self.session.engine.playback.detach_monitor(monitor)
                except Exception:
                    pass
            try:
                monitor.close()
            except Exception:
                pass
        self._monitor_error = None
        with self.state_lock:
            self.state["monitor_active"] = False

    def _open_replacement_monitor(self, prefs):
        """Build+start a new monitor without disturbing the current one.

        Returns the new monitor on success. On failure the current monitor
        (if any) is left untouched and the exception is re-raised.
        """
        device = prefs.get("monitor_device")
        gain_db = float(prefs.get("monitor_gain_db", 0.0))
        candidate = MonitorSink(device=device,
                                sample_rate=self.session.engine.config.output_sample_rate,
                                gain=10 ** (gain_db / 20.0))
        candidate.start()
        return candidate

    def _install_candidate_monitor(self, candidate):
        previous = self._monitor
        self._monitor = candidate
        if previous is not None:
            try:
                self.session.engine.playback.detach_monitor(previous)
            except Exception:
                pass
            try:
                previous.close()
            except Exception:
                pass
        try:
            self.session.engine.playback.attach_monitor(candidate)
        except Exception:
            self._monitor = previous
            try:
                candidate.close()
            except Exception:
                pass
            raise

    def _swap_monitor(self, prefs):
        """Atomically replace the current monitor with a new one."""
        previous = self._monitor
        try:
            replacement = self._open_replacement_monitor(prefs)
        except Exception as exc:
            self._monitor_error = t("errors.monitor_open", exc=exc)
            with self.state_lock:
                self.state["monitor_active"] = previous is not None and self._monitor_error is None
            raise
        self._install_candidate_monitor(replacement)
        self._monitor_error = None
        with self.state_lock:
            self.state["monitor_active"] = True

    async def stop(self):
        task, self.task = self.task, None
        if self.session:
            self.session.engine.buffer.clear()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._stop_monitor()
        if self.vc is not None:
            try:
                await self.vc.stop_worker()
            except Exception:
                pass
            finally:
                self._vc_starting = False
                self._resume_training()
        self.session = None
        self.usage.flush()
        with self.state_lock:
            self._history.clear()
        if self._lease is not None:
            self._lease.close()
        with self.state_lock:
            self.state["active"] = False
            self.state["transcript_partial"] = ""
            self.state["transcript_final"] = ""
            self.state["monitor_active"] = False
            self.state["vc_voice_id"] = None
        self.notify("stopped")

    def _resume_training(self):
        if self._vc_training_paused:
            self._vc_training_paused = False
            self.vc.trainer.resume()

    def _vc_start_finished(self, error, input_device, output_device):
        self._vc_starting = False
        if error is not None:
            self._resume_training()
            self.lease.close()
            with self.state_lock:
                self.state["active"] = False
                self.state["vc_voice_id"] = None
            self.notify("error", str(error) or t("status.voice_engine_failed"))
            return
        try:
            device_prefs = {key: value for key, value in
                            (("input_device", input_device), ("output_device", output_device))
                            if value is not None}
            if device_prefs:
                self.catalog.update(device_prefs)
        except Exception:
            pass
        self.notify("listening", t("status.live_voice_running"))
        self._vc_resync_task = asyncio.get_running_loop().create_task(self._vc_resync())

    async def _vc_resync(self):
        """Apply gain/monitor preferences changed while the voice was loading."""
        async with self.lock:
            if not (self.vc is not None and self.state.get("mode") == "vc" and self.state.get("active")):
                return
            prefs = self.catalog.preferences()
            enabled = bool(prefs.get("monitor_enabled", False)
                           and self._monitor_validation_error(prefs) is None)
            gains = {key: float(prefs.get(key, 0.0)) for key in ("output_gain_db", "monitor_gain_db")}
            gains.update(gate_enabled=bool(prefs.get("vc_gate_enabled", False)),
                         gate_db=float(prefs.get("vc_gate_db", -45.0)))
            devices = {"monitor_enabled": enabled,
                       "monitor_device": prefs.get("monitor_device") if enabled else None}
            try:
                await self.vc.update_audio(gains=gains, devices=devices)
            except Exception as exc:
                self.notify("error", t("errors.voice_settings_apply", exc=exc))

    async def _reap_failed_vc(self):
        """Release audio after the VC process died while running; keep its error visible."""
        if not (self.vc is not None and self.state.get("mode") == "vc" and self.state.get("active")
                and not self.vc.loading and not self._vc_starting
                and (self.vc.worker_failed() or self.vc.active_voice is None)):
            return
        error = (self.vc.snapshot().get("vc") or {}).get("error")
        try:
            await self.vc.reap_failed()
        except Exception:
            pass
        finally:
            self._resume_training()
        self.lease.close()
        with self.state_lock:
            self.state["active"] = False
            self.state["vc_voice_id"] = None
        self.notify("error", error or t("status.voice_process_stopped"))

    async def reap_failed_vc(self):
        async with self.lock:
            await self._reap_failed_vc()

    def finished(self, task):
        if task is not self.task:
            return
        self.task = self.session = None
        self._stop_monitor()
        self.lease.close()
        with self.state_lock:
            self.state["active"] = False
            self.state["monitor_active"] = False
        if task.cancelled():
            self.notify("stopped")
        else:
            exc = task.exception()
            self.notify("error", str(exc) if exc else t("status.audio_process_ended"))

    def _on_input_gain_changed(self, db):
        with self.state_lock:
            self.state["input_gain_db"] = float(db)
        if self.session is None:
            return
        status = self.session.input_gain_status()
        with self.state_lock:
            self.state["input_gain_supported"] = status["supported"]
            self.state["input_gain_error"] = status["error"]

    def _on_asr_event(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        if kind == "partial":
            self.update_transcript(partial=event.get("text", ""))
        elif kind == "final":
            self.update_transcript(final=event.get("text", ""))
        elif kind == "ready":
            self.update_transcript(reset=True)

    def mark_generation_start(self):
        with self.state_lock:
            self._generation_seq += 1
            self.state["generation_seq"] = self._generation_seq

    def record_latency(self, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            return
        with self.state_lock:
            self._last_latency = float(seconds)
            self.state["latency_seconds"] = self._last_latency

    async def vc_speak(self, text, target='both'):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError(t("errors.text_length"))
        if self.vc is None or self.vc.worker.snapshot().get('state') != 'running':
            raise ConflictError(t("errors.start_first"))
        prefs = self.catalog.preferences()
        if target == 'monitor' and (not prefs.get('monitor_enabled') or self._monitor_validation_error(prefs) is not None):
            raise ConflictError(t("errors.enable_monitor_first"))
        voice_id = prefs.get('vc_text_voice_id') or prefs['voice_id']
        if voice_id is None:
            raise RuntimeError("Choose a voice first")
        reference_id = VOICES[self.catalog.slug(voice_id)]
        key = await asyncio.to_thread(load_key)
        if not key:
            raise RuntimeError(t("status.fish_key_missing"))
        phrase_id = await self.vc.speak_text(text, reference_id, key, target=target)
        self.add_history(text, 'vc')
        return {'id': phrase_id}

    def add_history(self, text, source):
        if not isinstance(text, str) or not text.strip():
            return
        with self.state_lock:
            self._history_seq += 1
            self._history.append({"id": self._history_seq, "text": _bounded(_sanitize_transcript(text)),
                                  "source": source, "at": time.time()})

    def record_chars(self, chars):
        self.usage.add(chars)
        totals = self.usage.totals(self.catalog.preferences().get("monthly_char_limit"))
        limit = totals["limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            self._usage_level = 0
            return
        ratio = totals["month"] / limit
        level = 2 if ratio >= 1 else 1 if ratio >= 0.8 else 0
        if level > self._usage_level:
            self.notify("warning", t("status.char_limit_exhausted") if level == 2
                        else t("status.char_limit_warning"))
        self._usage_level = level

    def record_latency_breakdown(self, data):
        if not isinstance(data, dict):
            return
        clean = {}
        for key in ("asr_ms", "tts_first_ms", "total_ms"):
            value = data.get(key)
            if value is None and key == "asr_ms":
                clean[key] = None
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                clean[key] = int(value)
            else:
                return
        clean["cached"] = bool(data.get("cached"))
        with self.state_lock:
            self.state["latency"] = clean

    def begin_preview(self):
        with self._preview_lock:
            self._preview_id_seq += 1
            token = self._preview_id_seq
            self._preview_active = token
            self._preview_error = None
            with self.state_lock:
                self.state["preview_id"] = token
                self.state["preview_state"] = "playing"
                self.state["preview_error"] = None
            return token

    def end_preview(self, token, error=None):
        with self._preview_lock:
            if self._preview_active == token:
                self._preview_active = None
                self._preview_last = token
                self._preview_error = error
            elif self._preview_active is None and token == self._preview_last and error is not None:
                self._preview_error = error
            else:
                return
            with self.state_lock:
                self.state["preview_state"] = "idle" if error is None else "error"
                self.state["preview_error"] = error

    async def start(self, mode, voice_id, input_device, output_device, vc_voice_id=None):
        if mode in {"mic", "text"} and voice_id is None:
            raise RuntimeError("Choose a voice first")
        if mode not in {"mic", "text", "vc"}:
            raise ValueError(t("errors.mode_invalid"))
        if mode == "vc" and self.vc is not None and self.vc.loading:
            raise ConflictError(t("errors.voice_loading"))
        if mode == "vc":
            if self.vc is None:
                raise ValueError(t("errors.live_voice_unavailable"))
            if not isinstance(vc_voice_id, str) or not vc_voice_id:
                raise ValueError(t("errors.live_voice_required"))
        device_prefs = {key: value for key, value in
                        (("input_device", input_device), ("output_device", output_device))
                        if value is not None}
        if input_device is None:
            input_device = default_input_name()
        if output_device is None:
            output_device = preferred_virtual_output(
                [d["name"] for d in list_devices() if d["max_output_channels"] >= 2])
        if output_device is None:
            raise RuntimeError("Choose an output device in Settings → Audio")
        await self.stop()
        self.lease.acquire()
        self.notify("starting")
        monitor_enabled_pref = False
        try:
            if mode == "vc":
                prefs = self.catalog.preferences()
                monitor_validation_error = self._monitor_validation_error(prefs)
                monitor_enabled_pref = bool(prefs.get("monitor_enabled", False)
                                            and monitor_validation_error is None)
                if prefs.get("monitor_enabled") and monitor_validation_error is not None:
                    self._monitor_error = monitor_validation_error
                else:
                    self._monitor_error = None
                devices = {"input_device": input_device,
                           "output_device": output_device,
                           "monitor_enabled": monitor_enabled_pref,
                           "monitor_device": prefs.get("monitor_device") if monitor_enabled_pref else None}
                gains = {"output_gain_db": float(prefs.get("output_gain_db", 0.0)),
                         "monitor_gain_db": float(prefs.get("monitor_gain_db", 0.0)),
                         "bypass": bool(prefs.get("swap_enabled", False)),
                         "gate_enabled": bool(prefs.get("vc_gate_enabled", False)),
                         "gate_db": float(prefs.get("vc_gate_db", -45.0))}
                try:
                    resolve_output(output_device)
                except ValueError:
                    raise
                self._vc_starting = True
                with self.state_lock:
                    self.state.update(active=True, mode="vc", voice_id=voice_id)
                    self.state["vc_voice_id"] = vc_voice_id
                    self.state["transcript_partial"] = ""
                    self.state["transcript_final"] = ""
                    self.state["monitor_active"] = False
                self.notify("loading", t("status.voice_loading"))

                def finished(error):
                    self._vc_start_finished(error, device_prefs.get("input_device"),
                                            device_prefs.get("output_device"))
                self._vc_training_paused = self.vc.trainer.pause()
                await self.vc.start_worker(vc_voice_id, devices, gains, on_done=finished)
                return
            prefs = self.catalog.preferences()
            monitor_validation_error = self._monitor_validation_error(prefs)
            monitor_enabled_pref = bool(prefs.get("monitor_enabled", False)
                                        and monitor_validation_error is None)
            if prefs.get("monitor_enabled") and monitor_validation_error is not None:
                self._monitor_error = monitor_validation_error
            else:
                self._monitor_error = None
            config = replace(_config(self.catalog.slug(voice_id)),
                             input_device=input_device, output_device=output_device,
                             output_gain_db=prefs.get("output_gain_db", 0.0),
                             normalize_loudness=prefs.get("normalize_loudness", True),
                             monitor_enabled=monitor_enabled_pref,
                             monitor_device=prefs.get("monitor_device") if monitor_enabled_pref else None,
                             monitor_gain_db=prefs.get("monitor_gain_db", 0.0),
                             input_gain_db=float(prefs.get("input_gain_db", 0.0)))
            key = await asyncio.to_thread(load_key)
            if not key:
                raise RuntimeError(t("status.fish_key_missing"))
            asr = None
            if mode == "mic":
                locales = await speech_asr.supported_locales()
                language = resolve_speech_language(
                    prefs.get("speech_language"), locales["system"], locales["supported"])
                config = replace(config, language=language)
                _devices(config)
                engine = prefs.get("asr_engine", "apple")
                asr = _helper(config, engine)
                _permissions(await asr.doctor(), engine)
            else:
                resolve_output(config.output_device)

            self.session = AudioSession(config, key, asr=asr, notify=self.notify,
                                        asr_listener=self._on_asr_event,
                                        on_generation_start=self.mark_generation_start,
                                        on_latency=self.record_latency,
                                        on_latency_breakdown=self.record_latency_breakdown,
                                        on_chars=self.record_chars,
                                        on_commit=lambda text: self.add_history(text, "mic"),
                                        on_input_gain=self._on_input_gain_changed)
            status = self.session.input_gain_status()
            with self.state_lock:
                self.state["input_gain_supported"] = status["supported"]
                self.state["input_gain_db"] = float(prefs.get("input_gain_db", 0.0))
                self.state["input_gain_error"] = status["error"]
            self.task = asyncio.create_task(self.session.run())
            self.task.add_done_callback(self.finished)
            with self.state_lock:
                self.state.update(active=True, mode=mode, voice_id=voice_id)
                self.state["transcript_partial"] = ""
                self.state["transcript_final"] = ""
                self.state["monitor_active"] = False
            try:
                if monitor_enabled_pref:
                    self._start_monitor_for_session(prefs)
            except Exception:
                pass
            ready = asyncio.create_task(self.session.ready.wait())
            try:
                done, _ = await asyncio.wait([ready, self.task], timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if self.task is None:
                    raise RuntimeError(self.snapshot()["message"])
                if self.task in done:
                    self.task.result()
                    raise RuntimeError(t("errors.audio_start_ended"))
                if ready not in done:
                    raise RuntimeError(t("errors.microphone_timeout"))
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
            self.catalog.update({"voice_id": voice_id, **device_prefs})
        except BaseException:
            self._vc_starting = False
            drainer = getattr(self, "_asr_drainer", None)
            if drainer is not None:
                drainer.cancel()
                try:
                    await asyncio.gather(drainer, return_exceptions=True)
                except Exception:
                    pass
            with self.state_lock:
                self.state["monitor_active"] = False
            await self.stop()
            raise

    async def command(self, data):
        async with self.lock:
            await self._reap_failed_vc()
            action = data.get("action")
            prefs = self.catalog.preferences()
            voice_id = data.get("voice_id", prefs["voice_id"])
            try:
                if action == "stop":
                    await self.stop()
                elif action == "vc_bypass":
                    bypass = data.get("bypass")
                    if not isinstance(bypass, bool):
                        raise ValueError(t("errors.swap_request_invalid"))
                    if not (self.vc is not None and self.state.get("mode") == "vc"
                            and self.state.get("active")):
                        raise ConflictError(t("errors.live_voice_inactive"))
                    self.vc.set_bypass(bypass)
                elif action == "voice":
                    slug = self.catalog.slug(voice_id)
                    if self.session:
                        await self.session.change_voice(slug)
                    self.catalog.update({"voice_id": voice_id})
                    with self.state_lock:
                        self.state["voice_id"] = voice_id
                elif action == "repeat":
                    with self.state_lock:
                        entry = next((h for h in self._history if h["id"] == data.get("id")), None)
                    if entry is None:
                        raise NotFoundError("not_found")
                    if not self.session:
                        raise ConflictError(t("errors.start_speech_first"))
                    self.session.submit(entry["text"])
                elif action in {"start", "speak"}:
                    mode = data.get("mode", "mic") if action == "start" else "text"
                    text = data.get("text", "")
                    if action == "speak" and (not isinstance(text, str) or not text.strip() or len(text) > 1000):
                        raise ValueError(t("errors.text_length"))
                    if mode != "vc" and voice_id is None:
                        raise RuntimeError("Choose a voice first")
                    input_device = data.get("input_device", prefs["input_device"])
                    output_device = data.get("output_device", prefs["output_device"])
                    vc_voice_id = data.get("vc_voice_id")
                    if mode == "vc":
                        if not isinstance(vc_voice_id, str) or not vc_voice_id:
                            raise ValueError(t("errors.live_voice_required"))
                    if (not self.session or self.state["mode"] != mode
                            or (input_device is not None and self.session.engine.config.input_device != input_device)
                            or (output_device is not None and self.session.engine.config.output_device != output_device)
                            or mode == "vc"):
                        await self.start(mode, voice_id, input_device, output_device, vc_voice_id=vc_voice_id)
                        if mode == "vc" and action == "start":
                            return {**self.snapshot(), "ok": True}
                    elif self.state["voice_id"] != voice_id and mode != "vc":
                        await self.session.change_voice(self.catalog.slug(voice_id))
                        self.catalog.update({"voice_id": voice_id})
                        with self.state_lock:
                            self.state["voice_id"] = voice_id
                    if action == "speak":
                        self.session.submit(text)
                        self.add_history(text, "text")
                else:
                    raise ValueError(t("errors.interface_command_unknown"))
            except Exception as exc:
                # A validation error must not flip a healthy running session to "error".
                if not (isinstance(exc, (NotFoundError, ConflictError))
                        or (isinstance(exc, ValueError) and self.session is not None)):
                    self.notify("error", str(exc))
                raise
            return self.snapshot()

    async def apply_preferences(self, values):
        """Validate/save preferences and propagate to an active AudioSession if any."""
        async with self.lock:
            await self._reap_failed_vc()
            active_vc = self.state.get("mode") == "vc" and self.state.get("active")
            if (self.session or active_vc) and any(k in values for k in ("input_device", "output_device")):
                raise ValueError(t("errors.stop_audio_device"))
            prefs_snapshot = self.catalog.preferences()
            monitor_requested = {key: value for key, value in values.items()
                                 if key in {"monitor_enabled", "monitor_device", "monitor_gain_db"}}
            prospective = dict(prefs_snapshot)
            prospective.update(values)
            monitor_validation_error = self._monitor_validation_error(prospective)
            if ((monitor_requested or "output_device" in values)
                    and prospective.get("monitor_enabled") and monitor_validation_error is not None):
                raise ValueError(monitor_validation_error)
            previous_monitor_enabled = bool(prefs_snapshot.get("monitor_enabled", False))
            previous_monitor_device = prefs_snapshot.get("monitor_device")
            candidate_monitor = None
            candidate_monitor_error = None
            if monitor_requested and self.session is not None:
                enabled_now = bool(prospective.get("monitor_enabled", False))
                if enabled_now and (not previous_monitor_enabled
                                    or prospective.get("monitor_device") != previous_monitor_device
                                    or self._monitor is None):
                    try:
                        candidate_monitor = self._open_replacement_monitor(prospective)
                    except Exception as exc:
                        candidate_monitor_error = str(exc)
            try:
                prefs = self.catalog.update(values)
            except Exception:
                if candidate_monitor is not None:
                    try:
                        candidate_monitor.close()
                    except Exception:
                        pass
                raise
            if candidate_monitor_error is not None:
                try:
                    prefs = self.catalog.update(prefs_snapshot)
                except Exception:
                    pass
                raise ValueError(candidate_monitor_error)
            vc_active = (self.vc is not None and self.state.get("mode") == "vc"
                         and self.state.get("active"))
            if vc_active:
                gains = {key: float(prefs[key]) for key in ("output_gain_db", "monitor_gain_db")
                         if key in values and key in prefs}
                if "vc_gate_enabled" in values:
                    gains["gate_enabled"] = prefs["vc_gate_enabled"]
                if "vc_gate_db" in values:
                    gains["gate_db"] = prefs["vc_gate_db"]
                devices = None
                if any(k in values for k in ("monitor_enabled", "monitor_device")):
                    enabled = bool(prefs.get("monitor_enabled", False))
                    devices = {"monitor_enabled": enabled,
                               "monitor_device": prefs.get("monitor_device") if enabled else None}
                try:
                    await self.vc.update_audio(gains=gains or None, devices=devices)
                except Exception as exc:
                    raise ValueError(t("errors.voice_settings_apply", exc=exc)) from exc
            audio_fields = {}
            if "output_gain_db" in values and "output_gain_db" in prefs:
                audio_fields["output_gain_db"] = prefs["output_gain_db"]
            if "normalize_loudness" in values and "normalize_loudness" in prefs:
                audio_fields["normalize_loudness"] = prefs["normalize_loudness"]
            if self.session and audio_fields:
                self.session.update_settings(**audio_fields)
            if "input_gain_db" in values:
                try:
                    await self._apply_input_gain(prefs["input_gain_db"])
                except Exception as exc:
                    try:
                        self.catalog.update(prefs_snapshot)
                    except Exception:
                        pass
                    if self.session is not None and audio_fields:
                        try:
                            self.session.update_settings(
                                **{k: prefs_snapshot[k] for k in audio_fields if k in prefs_snapshot})
                        except Exception:
                            pass
                    raise ValueError(t("errors.input_gain_apply", exc=exc))
            with self.state_lock:
                self.state["input_gain_db"] = float(prefs.get("input_gain_db", 0.0))
            if candidate_monitor is not None:
                if self.session is None:
                    try:
                        candidate_monitor.close()
                    except Exception:
                        pass
                else:
                    self._monitor_error = None
                    with self.state_lock:
                        self.state["monitor_active"] = True
                    self._install_candidate_monitor(candidate_monitor)
            if self.session and monitor_requested and candidate_monitor is None:
                enabled_now = bool(prefs.get("monitor_enabled", False))
                if enabled_now:
                    device_changed = (prefs.get("monitor_device") != previous_monitor_device
                                      or not previous_monitor_enabled)
                    if device_changed or self._monitor is None:
                        try:
                            self._swap_monitor(prefs)
                        except Exception as exc:
                            try:
                                self.catalog.update(prefs_snapshot)
                            except Exception:
                                pass
                            raise ValueError(
                                t("errors.monitor_open", exc=exc))
                    else:
                        try:
                            self._monitor.set_gain(10 ** (float(prefs.get("monitor_gain_db", 0.0)) / 20.0))
                        except Exception:
                            pass
                        with self.state_lock:
                            self.state["monitor_active"] = True
                        self._monitor_error = None
                else:
                    self._stop_monitor()
            return prefs

    async def _apply_input_gain(self, db):
        if not isinstance(db, (int, float)) or not math.isfinite(float(db)):
            return
        if self.session is not None:
            setter = getattr(self.session, "set_input_gain", None)
            if setter is not None:
                await setter(float(db))

    async def remove_voice(self, voice_id):
        async with self.lock:
            active_id = self.state.get("voice_id")
            is_active = self.session is not None and active_id == voice_id
            original_voice_id = active_id
            remaining = [v for v in self.catalog.voices() if v["id"] != voice_id]
            if not remaining:
                raise ValueError(t("errors.last_voice"))
            next_id = remaining[0]["id"]
            switched = False
            if is_active:
                try:
                    await self.session.change_voice(self.catalog.slug(next_id))
                    switched = True
                except Exception as exc:
                    raise ValueError(t("errors.active_voice_switch", exc=exc))
            try:
                prefs = self.catalog.remove(voice_id)
            except Exception:
                if switched:
                    try:
                        await self.session.change_voice(self.catalog.slug(original_voice_id))
                        with self.state_lock:
                            self.state["voice_id"] = original_voice_id
                    except Exception:
                        pass
                raise
            with self.state_lock:
                if active_id == voice_id:
                    self.state["voice_id"] = prefs["voice_id"]
            return {"items": self.catalog.voices(), "preferences": prefs}

    async def remove_voices(self, voice_ids):
        async with self.lock:
            removed = set(voice_ids)
            active_id = self.state.get("voice_id")
            remaining = [v for v in self.catalog.voices() if v["id"] not in removed]
            if not remaining:
                raise ValueError(t("errors.all_voices"))
            switched = False
            if self.session is not None and active_id in removed:
                try:
                    await self.session.change_voice(self.catalog.slug(remaining[0]["id"]))
                    switched = True
                except Exception as exc:
                    raise ValueError(t("errors.active_voice_switch", exc=exc))
            try:
                prefs = self.catalog.remove_many(voice_ids)
            except Exception:
                if switched:
                    try:
                        await self.session.change_voice(self.catalog.slug(active_id))
                        with self.state_lock:
                            self.state["voice_id"] = active_id
                    except Exception:
                        pass
                raise
            with self.state_lock:
                if active_id in removed:
                    self.state["voice_id"] = prefs["voice_id"]
            return {"items": self.catalog.voices(), "preferences": prefs, "removed": len(removed)}
