"""Shared locale dictionaries for backend messages."""
from functools import lru_cache
import json
from pathlib import Path
import re

LANGUAGES = ("en", "ru")
LOCALES_DIR: Path = Path(__file__).with_name("locales")
_language = "en"
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@lru_cache(maxsize=None)
def load(lang: str) -> dict[str, str]:
    if lang not in LANGUAGES:
        return {}
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def set_language(lang: str) -> None:
    global _language
    _language = lang if lang in LANGUAGES else "en"


def get_language() -> str:
    return _language


def t(key: str, **vars) -> str:
    text = load(_language).get(key, load("en").get(key, key))
    return _PLACEHOLDER.sub(
        lambda match: str(vars[match[1]]) if match[1] in vars else match[0], text
    )
