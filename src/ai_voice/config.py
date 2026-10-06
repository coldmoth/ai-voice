"""Validated settings; no secrets or live device changes."""
from dataclasses import dataclass, field
import math
import re

VOICES: dict[str, str] = {}
VOICE_NAMES: dict[str, str] = {}

def _coerce_gain(value):
    if isinstance(value, bool):
        raise ValueError("output_gain_db must be a number")
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("output_gain_db must be a finite number")
    if not -24.0 <= float(value) <= 12.0:
        raise ValueError("output_gain_db must be between -24 and 12 dB")
    return float(value)


def _coerce_monitor_gain(value):
    if isinstance(value, bool):
        raise ValueError("monitor_gain_db must be a number")
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("monitor_gain_db must be a finite number")
    if not -24.0 <= float(value) <= 12.0:
        raise ValueError("monitor_gain_db must be between -24 and 12 dB")
    return float(value)


def _coerce_monitor_enabled(value):
    if not isinstance(value, bool):
        raise ValueError("monitor_enabled must be a boolean")
    return value


def _coerce_monitor_device(value):
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or len(stripped) > 200:
            raise ValueError("monitor_device must be a non-empty string up to 200 characters")
        return stripped
    raise ValueError("monitor_device must be a string or null")


@dataclass(frozen=True)
class Config:
    input_device: str = "MIC"
    output_device: str = "AI Voice"
    voice: str | None = None
    language: str = "ru-RU"
    model: str = "s2.1-pro-free"
    tts_sample_rate: int = 44100
    output_sample_rate: int = 48000
    buffer_ms: int = 250  # Maximum audio queue budget, not an imposed startup delay.
    endpoint_ms: int = 450
    max_retries: int = 2
    retry_base_seconds: float = 0.4
    request_timeout_seconds: float = 15.0
    output_gain_db: float = 0.0
    normalize_loudness: bool = True
    monitor_enabled: bool = False
    monitor_device: object = None
    monitor_gain_db: float = 0.0
    input_gain_db: float = 0.0
    split_long_phrases: bool = True
    split_min_words: int = 12

    def __post_init__(self):
        if self.voice is not None and (not isinstance(self.voice, str) or
                (self.voice not in VOICES and not re.fullmatch(r"[a-f0-9]{32}", self.voice))):
            raise ValueError("Choose a registered voice or a 32-hex Fish ID")
        for name in ("input_device", "output_device", "language", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.model != "s2.1-pro-free":
            raise ValueError("This delivery uses only the free Fish model s2.1-pro-free")
        if type(self.tts_sample_rate) is not int or self.tts_sample_rate != 44100:
            raise ValueError("Fish PCM input must use 44100 Hz")
        for name in ("output_sample_rate", "buffer_ms", "endpoint_ms"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be an integer between 0 and 10")
        for name in ("retry_base_seconds", "request_timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.request_timeout_seconds == 0:
            raise ValueError("request_timeout_seconds must be positive")
        object.__setattr__(self, "output_gain_db", _coerce_gain(self.output_gain_db))
        if not isinstance(self.normalize_loudness, bool):
            raise ValueError("normalize_loudness must be a boolean")
        object.__setattr__(self, "monitor_enabled", _coerce_monitor_enabled(self.monitor_enabled))
        object.__setattr__(self, "monitor_device", _coerce_monitor_device(self.monitor_device))
        object.__setattr__(self, "monitor_gain_db", _coerce_monitor_gain(self.monitor_gain_db))
        object.__setattr__(self, "input_gain_db", _coerce_gain(self.input_gain_db))

    @property
    def reference_id(self) -> str:
        if self.voice is None:
            raise ValueError("Choose a voice first")
        return VOICES.get(self.voice, self.voice)

    def linear_gain(self) -> float:
        return 10 ** (self.output_gain_db / 20.0)

    def monitor_linear_gain(self) -> float:
        return 10 ** (self.monitor_gain_db / 20.0)
