"""Documented Fish HTTP streaming PCM with bounded pre-audio retries."""
import asyncio
from collections.abc import AsyncIterator

import httpx

from .i18n import t
from .config import Config

TTS_URL = "https://api.fish.audio/v1/tts"


class TTSError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retriable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable


class FishTTS:
    def __init__(self, config: Config, key: str, *, client: httpx.AsyncClient | None = None):
        if not key or not key.strip():
            raise ValueError("Fish key is missing; run ai-voice setup-key")
        self.config = config
        self._key = key
        self._client = client

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        if not isinstance(text, str) or not text.strip():
            raise TTSError("Cannot synthesize an empty utterance")
        if self._client is not None:
            async for chunk in self._stream(self._client, text):
                yield chunk
        else:
            async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                async for chunk in self._stream(client, text):
                    yield chunk

    async def _stream(self, client: httpx.AsyncClient, text: str) -> AsyncIterator[bytes]:
        payload = {
            "text": text,
            "reference_id": self.config.reference_id,
            "format": "pcm",
            "sample_rate": self.config.tts_sample_rate,
            "latency": "low",
            "chunk_length": 100,
            "prosody": {"normalize_loudness": bool(self.config.normalize_loudness)},
        }
        headers = {"Authorization": f"Bearer {self._key}", "model": self.config.model}
        emitted = False
        for attempt in range(self.config.max_retries + 1):
            try:
                async with client.stream("POST", TTS_URL, json=payload, headers=headers,
                                         timeout=self.config.request_timeout_seconds) as response:
                    status = response.status_code
                    if status != 200:
                        messages = {
                            401: "Fish authentication failed; check the Keychain key",
                            402: "Fish quota or balance exhausted; check the free model quota",
                            403: "Fish access denied for the selected key or voice",
                            404: "Fish voice or endpoint was not found",
                            422: "Fish rejected the voice or synthesis settings",
                        }
                        raise TTSError(f"{messages.get(status, 'Fish synthesis request failed')} (HTTP {status})",
                                       status_code=status, retriable=status == 429 or 500 <= status <= 504)
                    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if mime not in {"audio/pcm", "audio/raw", "application/octet-stream"}:
                        raise TTSError("Fish returned a non-PCM response")
                    carry = b""
                    async for chunk in response.aiter_bytes():
                        raw = carry + chunk
                        end = len(raw) - len(raw) % 2
                        carry = raw[end:]
                        if end:
                            emitted = True
                            yield raw[:end]
                    if carry:
                        raise TTSError("Fish PCM ended with an incomplete 16-bit sample")
                    if not emitted:
                        raise TTSError("Fish returned empty audio")
                    return
            except httpx.TransportError:
                failure = TTSError(t("status.fish_retry"), retriable=True)
            except TTSError as exc:
                failure = exc
            if emitted or not failure.retriable or attempt == self.config.max_retries:
                # Never include response bodies, request objects, or exception text: they may contain secrets.
                raise TTSError(str(failure), status_code=failure.status_code,
                               retriable=failure.retriable and not emitted) from None
            await asyncio.sleep(min(2.0, self.config.retry_base_seconds * 2.5 ** attempt))  # 0.4, 1.0, 2.0
