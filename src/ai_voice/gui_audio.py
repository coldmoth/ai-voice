"""Live voice switching and typed utterances over the existing audio engine."""
import asyncio
from contextlib import aclosing
from dataclasses import replace
import math
import threading
import time

import httpx

from .i18n import t
from .config import Config
from .fish_tts import FishTTS, TTSError
from .asr import ASRError
from .pipeline import SpeechPipeline, log as _pipeline_log
from .transcript import PartialSplitter
from .tts_cache import MAX_ENTRY_BYTES, TTSCache


class AudioSession:
    def __init__(self, config: Config, key: str, *, asr=None, notify=None,
                 tts_factory=FishTTS, asr_listener=None,
                 on_generation_start=None, on_latency=None, on_input_gain=None,
                 on_latency_breakdown=None, on_chars=None, on_commit=None):
        self.engine = SpeechPipeline(config, key, asr,
                                      on_generation_start=on_generation_start,
                                      on_latency=on_latency)
        # Chunks of one utterance must not be dropped by the CLI-sized queue.
        self.engine.queue = asyncio.Queue(maxsize=4)
        self.on_latency_breakdown = on_latency_breakdown
        self.on_chars = on_chars
        self.on_commit = on_commit
        self.cache = TTSCache()
        self._asr_ms = {}
        self._utt_start = {}
        self._vad_at = None
        self.splitter = PartialSplitter(getattr(config, "split_min_words", 12))
        self.notify = notify or (lambda _state, _message="": None)
        self._typed_ts = set()
        self.tts_factory = tts_factory
        self.asr_listener = asr_listener
        self.on_input_gain = on_input_gain or (lambda _db: None)
        self._asr = asr
        self.ready = asyncio.Event()
        self._request = None
        self._can_speak = asyncio.Event()
        self._can_speak.set()
        self._transcript_partial = ""
        self._transcript_final = ""
        self._input_gain = float(getattr(config, "input_gain_db", 0.0))
        self._input_gain_lock = threading.Lock()
        self._input_gain_supported = bool(asr is not None)
        self._input_gain_error = None
        # Initial gain is passed on the helper command line so the capture starts
        # with the correct attenuation. set_input_gain remains for live updates
        # while the session is active.
        self._initial_input_gain_dispatched = bool(asr is None)

    async def _wait_for_initial_input_gain(self):
        # SpeechASR passes this value on the helper command line.  Do not send
        # a control command before the helper has been spawned; this also keeps
        # the ASR interface compatible with older test doubles and adapters.
        with self._input_gain_lock:
            self._initial_input_gain_dispatched = True
            self._input_gain_supported = self._asr is not None
            self._input_gain_error = None
        try:
            self.on_input_gain(self._input_gain)
        except Exception:
            pass

    def _emit_asr_event(self, event):
        listener = self.asr_listener
        if listener is None:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        text = event.get("text")
        if kind == "ready":
            self._transcript_partial = ""
        elif kind == "partial":
            if isinstance(text, str):
                self._transcript_partial = text
        elif kind == "final":
            if isinstance(text, str) and text:
                self._transcript_final = text
                self._transcript_partial = ""
        try:
            listener(dict(event))
        except Exception:
            pass

    def submit(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError(t("errors.speech_text_length"))
        queue = self.engine.queue
        if queue.full():
            dropped, _ = queue.get_nowait()
            self._typed_ts.discard(dropped)
            queue.task_done()
            self.notify("warning", t("status.queue_full"))
        stamp = time.monotonic()
        self._typed_ts.add(stamp)
        queue.put_nowait((stamp, text.strip()))

    async def change_voice(self, voice):
        config = replace(self.engine.config, voice=voice)
        self._can_speak.clear()
        try:
            if self._request is not None and not self._request.done():
                self._request.cancel()
                await asyncio.gather(self._request, return_exceptions=True)
            queue = self.engine.queue
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
            self._typed_ts.clear()
            self._asr_ms.clear()
            self.engine.buffer.clear()
            self.engine.config = config
            if hasattr(self.engine.playback, "set_gain"):
                self.engine.playback.set_gain(config.linear_gain())
            self.notify("listening" if self.engine.asr else "ready")
        finally:
            self._can_speak.set()

    async def set_input_gain(self, db):
        if isinstance(db, bool) or not isinstance(db, (int, float)):
            raise ValueError("input_gain_db must be a finite number")
        value = float(db)
        if value < -24.0 or value > 12.0 or not math.isfinite(value):
            raise ValueError("input_gain_db must be between -24 and 12 dB")
        with self._input_gain_lock:
            self._input_gain = value
            if not self._initial_input_gain_dispatched:
                # Initial dispatch is handled separately so it can be awaited
                # before the session reports ready.
                self._input_gain_supported = self._asr is not None
                self._input_gain_error = None
        if self._asr is None:
            try:
                self.on_input_gain(value)
            except Exception:
                pass
            return
        try:
            await self._asr.set_input_gain(value)
        except ASRError as exc:
            with self._input_gain_lock:
                self._input_gain_supported = False
                self._input_gain_error = str(exc)
            try:
                self.on_input_gain(value)
            except Exception:
                pass
            return
        except Exception as exc:
            with self._input_gain_lock:
                self._input_gain_supported = False
                self._input_gain_error = str(exc)
            try:
                self.on_input_gain(value)
            except Exception:
                pass
            return
        with self._input_gain_lock:
            self._input_gain_supported = True
            self._input_gain_error = None
        try:
            self.on_input_gain(value)
        except Exception:
            pass

    def input_gain_status(self):
        with self._input_gain_lock:
            return {"value": self._input_gain, "supported": self._input_gain_supported,
                    "error": self._input_gain_error}

    async def _apply_initial_input_gain(self):
        await self._wait_for_initial_input_gain()

    def update_settings(self, *, output_gain_db=None, normalize_loudness=None):
        updates = {}
        if output_gain_db is not None:
            updates["output_gain_db"] = output_gain_db
        if normalize_loudness is not None:
            updates["normalize_loudness"] = normalize_loudness
        if not updates:
            return self.engine.config
        config = replace(self.engine.config, **updates)
        self.engine.config = config
        if hasattr(self.engine.playback, "set_gain"):
            self.engine.playback.set_gain(config.linear_gain())
        return config

    async def _events(self):
        await self._apply_initial_input_gain()
        # SpeechASR consumes the initial value when it builds its argv.  Set
        # the attribute for an already-constructed real ASR and call events
        # without kwargs so older duck-typed ASR implementations still work.
        if hasattr(self.engine.asr, "initial_input_gain_db"):
            self.engine.asr.initial_input_gain_db = self._input_gain
        async with aclosing(self.engine.asr.events()) as events:
            async for event in events:
                if event["event"] == "input_gain":
                    continue
                self._emit_asr_event(event)
                if event["event"] == "ready":
                    self.ready.set()
                    self.notify("listening")
                yield event

    def _enqueue(self, text, uid=None):
        queue = self.engine.queue
        if queue.full():
            queue.get_nowait()
            queue.task_done()
            _pipeline_log.debug("Skipped an aging phrase from the queue.")
        stamp = time.monotonic()
        if uid is not None and uid in self._utt_start:
            self._asr_ms[stamp] = (stamp - self._utt_start[uid]) * 1000.0
        queue.put_nowait((stamp, text))

    def _note_utterance(self, kind, uid):
        now = time.monotonic()
        if kind == "vad_start":
            self._vad_at = now
        elif kind in ("partial", "final") and isinstance(uid, str) and uid not in self._utt_start:
            self._utt_start[uid] = self._vad_at if self._vad_at is not None else now
            self._vad_at = None
            while len(self._utt_start) > 16:
                del self._utt_start[next(iter(self._utt_start))]

    async def _listen(self, events):
        """GUI variant of SpeechPipeline._listen with early partial splitting."""
        split = bool(getattr(self.engine.config, "split_long_phrases", True))
        async for event in events:
            kind = event["event"]
            uid = event.get("utterance_id")
            self._note_utterance(kind, uid)
            if kind == "error" and event.get("code") != "no_speech":
                _pipeline_log.warning("Recognition: %s", event.get("message", "error"))
            if (split and kind == "partial" and isinstance(uid, str) and uid
                    and isinstance(event.get("text"), str)):
                for phrase in self.splitter.partial(uid, event["text"]):
                    self._enqueue(phrase, uid)
            text = self.engine.committer.accept(dict(event, type=kind))
            if text is not None:
                if callable(self.on_commit):
                    try:
                        self.on_commit(text)
                    except Exception:
                        pass
                if split and isinstance(uid, str):
                    text = self.splitter.final(uid, text)
                if text:
                    self._enqueue(text, uid)
            elif split and isinstance(uid, str) and kind == "final":
                self.splitter.discard(uid)
        raise RuntimeError(t("errors.recognition_stopped"))

    def _report_latency(self, committed, asr_ms, requested, cached):
        now = time.monotonic()
        if callable(self.engine.on_latency):
            try:
                self.engine.on_latency(now - committed)
            except Exception:
                pass
        if callable(self.on_latency_breakdown):
            try:
                self.on_latency_breakdown({
                    "asr_ms": None if asr_ms is None else round(asr_ms),
                    "tts_first_ms": 0 if cached else round((now - requested) * 1000.0),
                    "total_ms": round((now - committed) * 1000.0),
                    "cached": cached})
            except Exception:
                pass

    async def _synthesize(self, client, text, committed):
        config = self.engine.config
        asr_ms = self._asr_ms.pop(committed, None)
        cache_key = self.cache.key(config.reference_id, config.model, config.tts_sample_rate,
                                   config.normalize_loudness, text)
        pcm = self.cache.get(cache_key)
        if pcm is not None:
            self.notify("playing")
            await self.engine._push(pcm)
            self._report_latency(committed, asr_ms, committed, True)
        else:
            tts = self.tts_factory(config, self.engine.key, client=client)
            self.notify("synthesizing")
            requested = time.monotonic()
            collected = bytearray() if cache_key is not None else None
            async with aclosing(tts.stream(text)) as stream:
                first = await asyncio.wait_for(anext(stream), config.request_timeout_seconds)
                self.notify("playing")
                await self.engine._push(first)
                self._report_latency(committed, asr_ms, requested, False)
                if callable(self.on_chars):
                    try:
                        self.on_chars(len(text))
                    except Exception:
                        pass
                if collected is not None:
                    collected += first
                async for chunk in stream:
                    await self.engine._push(chunk)
                    if collected is not None:
                        collected += chunk
                        if len(collected) > MAX_ENTRY_BYTES:
                            collected = None
            if collected is not None:
                self.cache.put(cache_key, bytes(collected))
        await self.engine._wait_for_space()
        self.engine.buffer.finish()
        await self.engine._wait_for_space(limit=0)

    async def _speak(self, client):
        while True:
            await self._can_speak.wait()
            committed, text = await self.engine.queue.get()
            try:
                typed = committed in self._typed_ts
                self._typed_ts.discard(committed)
                if not typed and time.monotonic() - committed > self.engine.max_age:
                    self._asr_ms.pop(committed, None)
                    self.notify("warning", t("status.phrase_stale"))
                    continue
                if callable(self.engine.on_generation_start):
                    try:
                        self.engine.on_generation_start()
                    except Exception:
                        pass
                self._request = asyncio.create_task(self._synthesize(client, text, committed))
                await self._request
                self.notify("listening" if self.engine.asr else "ready")
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except TTSError as exc:
                self.engine.buffer.clear()
                if exc.status_code in {401, 402, 403, 404, 422}:
                    raise
                self.notify("warning", str(exc))
            except httpx.ConnectError:
                self.engine.buffer.clear()
                self.notify("warning", t("status.fish_retry"))
            except (TimeoutError, StopAsyncIteration, httpx.HTTPError):
                self.engine.buffer.clear()
                self.notify("warning", t("status.fish_timeout"))
            finally:
                self._request = None
                self.engine.queue.task_done()

    async def run(self):
        tasks = []
        events = self._events() if self.engine.asr else None
        try:
            with self.engine.playback:
                async with httpx.AsyncClient(timeout=self.engine.config.request_timeout_seconds) as client:
                    tasks.append(asyncio.create_task(self._speak(client)))
                    if events:
                        tasks.append(asyncio.create_task(self._listen(events)))
                    else:
                        self.ready.set()
                        self.notify("ready")
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
        finally:
            self.cache.clear()
            self.engine.buffer.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if events:
                await events.aclose()
