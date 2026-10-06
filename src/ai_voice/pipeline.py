"""Ordered final-utterance synthesis and paced playback; no persisted speech."""
import asyncio
from contextlib import aclosing
import logging
import time

import httpx

from .asr import SpeechASR
from .config import Config
from .fish_tts import FishTTS, TTSError
from .playback import PCMBuffer, Playback
from .i18n import t
from .transcript import Committer

log = logging.getLogger(__name__)


class SpeechPipeline:
    def __init__(self, config: Config, key: str, asr: SpeechASR,
                 *, on_generation_start=None, on_latency=None):
        self.config, self.key, self.asr = config, key, asr
        self.on_generation_start = on_generation_start
        self.on_latency = on_latency
        self.queue = asyncio.Queue(maxsize=2)
        self.committer = Committer()
        # 250ms maximum queue, never a forced 250ms startup delay.
        self.buffer = PCMBuffer(input_rate=config.tts_sample_rate,
                                output_rate=config.output_sample_rate,
                                max_frames=max(12000, config.output_sample_rate * config.buffer_ms // 1000))
        self.playback = Playback(self.buffer, device=config.output_device,
                                 sample_rate=config.output_sample_rate,
                                 gain=config.linear_gain())
        self.max_age = 5.0

    async def _listen(self, events):
        async for event in events:
            kind = event["event"]
            if kind == "ready":
                log.info('Microphone %s ready. Speak Russian; stop with Ctrl+C.', self.config.input_device)
            elif kind == "error":
                if event.get("code") == "no_speech":
                    log.debug('No speech detected; continuing to listen.')
                else:
                    log.warning('Recognition: %s', event.get("message", 'error'))
            elif kind == "partial":
                log.debug('Recognition: %s', event.get("text", ""))
            text = self.committer.accept(dict(event, type=kind))
            if text is not None:
                if self.queue.full():
                    self.queue.get_nowait()
                    self.queue.task_done()
                    log.debug('Skipped a stale phrase from the queue.')
                self.queue.put_nowait((time.monotonic(), text))
        raise RuntimeError(t("errors.recognition_stopped"))

    async def _wait_for_space(self, limit=6000):
        deadline = time.monotonic() + 3
        while self.buffer.buffered_frames > limit:
            if time.monotonic() >= deadline:
                raise RuntimeError(t("errors.pipeline_output_stalled"))
            await asyncio.sleep(.01)

    async def _push(self, chunk: bytes):
        for offset in range(0, len(chunk), 4096):
            await self._wait_for_space()
            self.buffer.push(chunk[offset:offset + 4096])

    async def _speak(self, tts):
        while True:
            committed_at, text = await self.queue.get()
            try:
                remaining = self.max_age - (time.monotonic() - committed_at)
                if remaining <= 0:
                    log.debug('Skipped a phrase older than five seconds.')
                    continue
                if callable(self.on_generation_start):
                    try:
                        self.on_generation_start()
                    except Exception:
                        pass
                started = time.monotonic()
                log.info('Sending phrase to Fish Audio, waiting for audio…')
                async with aclosing(tts.stream(text)) as stream:
                    # The five-second queue age applies before a request starts.
                    # Cold voice models can take longer to deliver their first PCM.
                    chunk = await asyncio.wait_for(anext(stream), self.config.request_timeout_seconds)
                    latency = time.monotonic() - committed_at
                    if latency >= 0 and callable(self.on_latency):
                        try:
                            self.on_latency(latency)
                        except Exception:
                            pass
                    log.debug('First PCM %.0f ms after sending, %.0f ms after recognition.',
                              (time.monotonic() - started) * 1000,
                              latency * 1000)
                    await self._push(chunk)
                    async for chunk in stream:
                        await self._push(chunk)
                await self._wait_for_space()
                self.buffer.finish()
                await self._wait_for_space(limit=0)
            except asyncio.TimeoutError:
                self.buffer.clear()
                log.warning('Fish Audio did not start speech within %.0f seconds; skipped phrase, continuing to listen.',
                            self.config.request_timeout_seconds)
            except TTSError as exc:
                self.buffer.clear()
                if exc.status_code in {401, 402, 403, 404, 422}:
                    raise RuntimeError(t("errors.fish_synthesis_stopped", exc=exc)) from None
                log.warning('Phrase skipped: %s', exc)
            except StopAsyncIteration:
                self.buffer.clear()
                log.warning('Fish Audio returned empty audio; continuing to listen.')
            finally:
                self.queue.task_done()

    async def run(self):
        tasks = []
        events = self.asr.events()
        try:
            with self.playback:
                async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                    tts = FishTTS(self.config, self.key, client=client)
                    tasks = [asyncio.create_task(self._listen(events)),
                             asyncio.create_task(self._speak(tts))]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        # Clear pending output before waiting for HTTP/helper cleanup.
                        self.buffer.clear()
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await events.aclose()
