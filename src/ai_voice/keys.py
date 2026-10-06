"""Format check, network verification and Keychain storage for the Fish Audio and Hugging Face keys.

A key value is never logged, returned or kept after the call.
"""
import httpx

from . import __version__
from .hf_catalog import invalidate_hf_token
from .secrets import delete_key, has_key, save_key

KINDS = {"fish": "FISH_API_KEY", "hf": "HF_TOKEN"}
FISH_VERIFY_URL = "https://api.fish.audio/wallet/self/api-credit"
HF_VERIFY_URL = "https://huggingface.co/api/whoami-v2"


def check_format(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if 16 <= len(value) <= 200 and all(0x21 <= ord(c) <= 0x7e for c in value):
        return value
    return None


def _get(url: str, key: str, client: httpx.Client | None) -> int | None:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": f"AI-Voice/{__version__}"}
    try:
        if client is not None:
            return client.get(url, headers=headers, follow_redirects=False).status_code
        with httpx.Client(timeout=10) as own:
            return own.get(url, headers=headers, follow_redirects=False).status_code
    except (httpx.HTTPError, OSError):
        return None


def verify_fish_key(key: str, *, client: httpx.Client | None = None) -> dict:
    value = check_format(key)
    if value is None:
        return {"ok": False, "error": "format"}
    status = _get(FISH_VERIFY_URL, value, client)
    if status == 200:
        return {"ok": True}
    if status in (401, 403):
        return {"ok": False, "error": "invalid"}
    if status == 402:
        return {"ok": True, "warning": "no_credit"}
    return {"ok": False, "error": "network"}


def verify_hf_token(token: str, *, client: httpx.Client | None = None) -> dict:
    value = check_format(token)
    if value is None:
        return {"ok": False, "error": "format"}
    status = _get(HF_VERIFY_URL, value, client)
    if status == 200:
        return {"ok": True}
    if status in (401, 403):
        return {"ok": False, "error": "invalid"}
    return {"ok": False, "error": "network"}


def key_status() -> dict:
    return {"fish": has_key("FISH_API_KEY"), "hf": has_key("HF_TOKEN")}


def save(kind: str, value: object, *, client: httpx.Client | None = None) -> dict:
    name = KINDS[kind] if kind in KINDS else None
    if name is None:
        raise ValueError("Unknown key kind.")
    stripped = check_format(value)
    verify = verify_fish_key if kind == "fish" else verify_hf_token
    result = verify(stripped if stripped is not None else "", client=client)
    if not result["ok"]:
        return result
    try:
        save_key(name, stripped)
    except RuntimeError:
        return {"ok": False, "error": "keychain"}
    if kind == "hf":
        invalidate_hf_token()
    return result


def remove(kind: str) -> dict:
    if kind not in KINDS:
        raise ValueError("Unknown key kind.")
    try:
        delete_key(KINDS[kind])
    except RuntimeError:
        return {"ok": False, "error": "keychain"}
    if kind == "hf":
        invalidate_hf_token()
    return {"ok": True}
