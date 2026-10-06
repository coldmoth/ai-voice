"""Explicit voice libraries for offline tests."""
import json
from pathlib import Path

from ai_voice.catalog import LIBRARY_VERSION

TEST_VOICE_ID = "db89e349112e44dca6820e0cb2d414cc"


def write_library(path, voices: list[dict], voice_id: str | None = None, **prefs) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"library_version": LIBRARY_VERSION, "voices": voices,
                                "voice_id": voice_id, "favorite_ids": [], **prefs},
                               ensure_ascii=False, indent=2))
    return path
