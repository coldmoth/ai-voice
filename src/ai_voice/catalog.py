"""Public Fish catalog and non-secret desktop preferences."""
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import math
from concurrent.futures import ThreadPoolExecutor

import httpx

from .i18n import t
from .config import VOICES, VOICE_NAMES
from . import i18n
from .secrets import has_key, load_key
from .updates import RELEASE_URL_PREFIX, parse_version

from .paths import DATA, ROOT  # noqa: F401  (ROOT re-exported)
AVATAR_CDN = "https://public-platform.r2.fish.audio"
GENDERS = {"all", "female", "male"}
SORT_OPTIONS = {"task_count", "created_at", "score"}
UI_LANGUAGES = {"en", "ru"}
CATALOG_LANGUAGES = {"ru", "en", "all"}
SPEECH_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")
OUTPUT_GAIN_MIN, OUTPUT_GAIN_MAX = -24.0, 12.0
MONITOR_GAIN_MIN, MONITOR_GAIN_MAX = -24.0, 12.0
MONITOR_BLACKLIST = ("AI Voice", "MIC", "BlackHole", "Loopback Audio", "Soundflower", "Aggregate")
ALLOWED_PREF_KEYS = {"voice_id", "input_device", "output_device", "favorite_ids",
                     "output_gain_db", "normalize_loudness", "language", "speech_language", "catalog_language",
                     "monitor_enabled", "monitor_device", "monitor_gain_db",
                     "input_gain_db", "monthly_char_limit", "asr_engine",
                     "swap_enabled", "swap_mode", "swap_hotkey", "vc_gate_enabled", "vc_gate_db", "vc_text_voice_id",
                     "onboarding_completed", "update_auto"}
SWAP_MODES = {"hold", "toggle"}
SWAP_MODIFIER_MASK = 256 | 512 | 2048 | 4096  # Carbon cmdKey|shiftKey|optionKey|controlKey
DEFAULT_SWAP_HOTKEY = {"key_code": 1, "modifiers": 256 | 2048, "label": "\u2325\u2318S"}
INPUT_GAIN_MIN, INPUT_GAIN_MAX = -24.0, 12.0
USER_VOICES_KEY = "fish_"
LIBRARY_VERSION = 2


META_CACHE_TTL = 24 * 60 * 60
META_MAX_ENTRIES = 500


def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{32}", value))


def safe_avatar(cover_image):
    """Build a CDN avatar URL from Fish cover_image path or return None.

    Only the official r2 CDN is allowed; arbitrary schemes, hosts and traversal are rejected.
    """
    if not isinstance(cover_image, str):
        return None
    path = cover_image.strip()
    if not path:
        return None
    if "://" in path or path.startswith("//"):
        return None
    if ".." in path.split("/"):
        return None
    if path.startswith("/"):
        path = path[1:]
    if path.startswith("coverimage/"):
        relative = path[len("coverimage/"):]
    else:
        return None
    identifier = relative
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", identifier):
        return None
    return f"{AVATAR_CDN}/cdn-cgi/image/width=96,format=webp/coverimage/{identifier}"


def normalize_tags(tags):
    if not isinstance(tags, list):
        return []
    cleaned = []
    for tag in tags:
        if isinstance(tag, str):
            stripped = tag.strip().lower()
            if stripped and len(stripped) <= 40 and re.fullmatch(r"[\w\- ]+", stripped):
                cleaned.append(stripped[:40])
    return cleaned[:16]


def normalize_task_count(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def _output_gain_db(value):
    if isinstance(value, bool):
        raise ValueError(t("errors.output_gain_invalid"))
    if not isinstance(value, (int, float)):
        raise ValueError(t("errors.output_gain_number"))
    if not math.isfinite(float(value)):
        raise ValueError(t("errors.output_gain_number"))
    if not OUTPUT_GAIN_MIN <= float(value) <= OUTPUT_GAIN_MAX:
        raise ValueError(t("errors.output_gain_range", min=format(OUTPUT_GAIN_MIN, ".0f"), max=format(OUTPUT_GAIN_MAX, ".0f")))
    return float(value)


def _monthly_char_limit(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000_000:
        raise ValueError(t("errors.char_limit_invalid"))
    return value


ASR_ENGINES = ("apple", "gigaam")


def _asr_engine(value):
    if value not in ASR_ENGINES:
        raise ValueError(t("errors.asr_engine_invalid"))
    return value


def _swap_enabled(value):
    if not isinstance(value, bool):
        raise ValueError(t("errors.swap_enabled_invalid"))
    return value


def _vc_gate_enabled(value):
    if not isinstance(value, bool):
        raise ValueError("vc_gate_enabled must be a boolean")
    return value


def _vc_gate_db(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not -70 <= value <= -10):
        raise ValueError("vc_gate_db must be between -70 and -10 dB")
    return float(value)


def _swap_mode(value):
    if value not in SWAP_MODES:
        raise ValueError(t("errors.swap_mode_invalid"))
    return value


def _swap_hotkey(value):
    if not isinstance(value, dict) or set(value) != {"key_code", "modifiers", "label"}:
        raise ValueError(t("errors.hotkey_invalid"))
    code, mods, label = value["key_code"], value["modifiers"], value["label"]
    if (isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 127
            or isinstance(mods, bool) or not isinstance(mods, int)
            or mods < 0 or mods & ~SWAP_MODIFIER_MASK
            or not isinstance(label, str) or not label.strip() or len(label) > 16):
        raise ValueError(t("errors.hotkey_invalid"))
    return {"key_code": code, "modifiers": mods, "label": label}


def _input_gain_db(value):
    if isinstance(value, bool):
        raise ValueError(t("errors.input_gain_invalid"))
    if not isinstance(value, (int, float)):
        raise ValueError(t("errors.input_gain_number"))
    if not math.isfinite(float(value)):
        raise ValueError(t("errors.input_gain_number"))
    if not INPUT_GAIN_MIN <= float(value) <= INPUT_GAIN_MAX:
        raise ValueError(t("errors.input_gain_range", min=format(INPUT_GAIN_MIN, ".0f"), max=format(INPUT_GAIN_MAX, ".0f")))
    return float(value)


def _monitor_gain_db(value):
    if isinstance(value, bool):
        raise ValueError(t("errors.monitor_gain_invalid"))
    if not isinstance(value, (int, float)):
        raise ValueError(t("errors.monitor_gain_number"))
    if not math.isfinite(float(value)):
        raise ValueError(t("errors.monitor_gain_number"))
    if not MONITOR_GAIN_MIN <= float(value) <= MONITOR_GAIN_MAX:
        raise ValueError(t("errors.monitor_gain_range", min=format(MONITOR_GAIN_MIN, ".0f"), max=format(MONITOR_GAIN_MAX, ".0f")))
    return float(value)


def _monitor_enabled(value):
    if not isinstance(value, bool):
        raise ValueError(t("errors.monitor_enabled_invalid"))
    return value


def _monitor_device(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or len(stripped) > 200:
            raise ValueError(t("errors.monitor_device_invalid"))
        return stripped
    raise ValueError(t("errors.monitor_device_invalid"))


def _is_monitor_blacklisted(name):
    if not isinstance(name, str):
        return True
    upper = name.upper()
    if sys.platform == "win32":
        from .devices import WIN_VIRTUAL_OUTPUTS

        if any(token.upper() in upper for token in WIN_VIRTUAL_OUTPUTS):
            return True
    return any(token.upper() in upper for token in MONITOR_BLACKLIST)


def _language(v) -> str:
    if not isinstance(v, str) or v not in UI_LANGUAGES:
        raise ValueError(t("errors.ui_language_invalid"))
    return v


def _catalog_language(v) -> str:
    if not isinstance(v, str) or v not in CATALOG_LANGUAGES:
        raise ValueError(t("errors.catalog_language_invalid"))
    return v


def _speech_language(v) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not SPEECH_LANGUAGE_RE.fullmatch(v):
        raise ValueError(t("errors.speech_language_invalid"))
    return v


def resolve_speech_language(pref: str | None, system: str | None, supported: list[str]) -> str:
    if pref and (pref in supported or not supported):
        return pref
    system = system.replace("_", "-") if system else None
    return system if system in supported else "en-US"


def item(model, language="ru"):
    if (not isinstance(model, dict) or not valid_id(model.get("_id"))
            or not isinstance(model.get("title"), str) or not model["title"].strip()
            or (language != "all" and language not in (model.get("languages") or []))
            or model.get("state") != "trained" or model.get("visibility") != "public"):
        return None
    languages = model.get("languages") or []
    return {"id": model["_id"], "name": model["title"].strip()[:160],
            "language": (languages[0] if languages else None) if language == "all" else language,
            "source": "fish", "url": f"https://fish.audio/m/{model['_id']}/",
            "avatar_url": safe_avatar(model.get("cover_image")),
            "tags": normalize_tags(model.get("tags")),
            "task_count": normalize_task_count(model.get("task_count"))}


def fish_get(path, params=None):
    key = load_key()
    if not key:
        raise ValueError(t("status.fish_key_missing"))
    try:
        response = httpx.get("https://api.fish.audio" + path, params=params,
                             headers={"Authorization": "Bearer " + key}, timeout=20)
        if response.status_code != 200:
            message = {401: t("errors.fish_key_rejected"), 403: t("errors.voice_access_denied"),
                       404: t("errors.voice_not_found"), 429: t("errors.too_many_requests")}
            raise ValueError(message.get(response.status_code, t("errors.fish_catalog_unavailable")))
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(t("errors.fish_response_invalid"))
        return data
    except (httpx.HTTPError, json.JSONDecodeError):
        raise ValueError(t("errors.fish_connection")) from None


class _VoiceMetaStore:
    """Bounded official metadata cache; disk and in-memory entries share a TTL."""
    def __init__(self, path):
        self.path, self.lock, self._data = path, threading.RLock(), {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for voice_id, payload in list(raw.items())[:META_MAX_ENTRIES]:
            if not valid_id(voice_id) or not isinstance(payload, dict):
                continue
            ts = payload.get("ts")
            if (isinstance(ts, bool) or not isinstance(ts, (int, float))
                    or not math.isfinite(ts) or not 0 <= now - ts <= META_CACHE_TTL):
                continue
            entry = payload.get("entry")
            if not isinstance(entry, dict):
                continue
            cover = entry.get("cover_path")
            clean = {"cover_path": cover if safe_avatar(cover) else None,
                     "tags": normalize_tags(entry.get("tags")),
                     "task_count": normalize_task_count(entry.get("task_count"))}
            self._data[voice_id] = {"ts": ts, "entry": clean}

    def get(self, voice_id):
        with self.lock:
            payload = self._data.get(voice_id)
            if not payload or time.time() - payload["ts"] > META_CACHE_TTL:
                return None
            entry = payload["entry"]
            return {"avatar_url": safe_avatar(entry.get("cover_path")),
                    "tags": list(entry["tags"]), "task_count": entry["task_count"]}

    def clear(self):
        with self.lock:
            self._data = {}
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def put(self, voice_id, model):
        cover = model.get("cover_image")
        entry = {"cover_path": cover if safe_avatar(cover) else None,
                 "tags": normalize_tags(model.get("tags")),
                 "task_count": normalize_task_count(model.get("task_count"))}
        with self.lock:
            self._data[voice_id] = {"ts": time.time(), "entry": entry}
            if len(self._data) > META_MAX_ENTRIES:
                oldest = min(self._data, key=lambda key: self._data[key]["ts"])
                del self._data[oldest]
            try:
                self.path.parent.mkdir(exist_ok=True, parents=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
                os.chmod(temporary, 0o600)
                temporary.replace(self.path)
            except OSError:
                pass  # Read-only/full disk must not break the voice library.


def _update_state(value):
    value = value if isinstance(value, dict) else {}
    last_check = value.get("last_check", 0)
    if (isinstance(last_check, bool) or not isinstance(last_check, (int, float))
            or not 0 <= last_check < float("inf")):
        last_check = 0
    latest = value.get("latest")
    if (isinstance(latest, dict) and parse_version(latest.get("version")) is not None
            and isinstance(latest.get("url"), str) and latest["url"].startswith(RELEASE_URL_PREFIX)):
        latest = {"version": latest["version"], "url": latest["url"]}
    else:
        latest = None
    skipped = value.get("skipped")
    if parse_version(skipped) is None:
        skipped = None
    return {"last_check": last_check, "latest": latest, "skipped": skipped}


class Catalog:
    DEFAULT_PREFS = {"voice_id": None, "input_device": None, "output_device": None,
                     "language": "en", "speech_language": None, "catalog_language": "en",
                     "output_gain_db": 0.0, "normalize_loudness": True,
                     "monitor_enabled": False, "monitor_device": None, "monitor_gain_db": 0.0,
                     "input_gain_db": 0.0, "monthly_char_limit": None, "asr_engine": "apple",
                     "vc_gate_enabled": False, "vc_gate_db": -45.0, "vc_text_voice_id": None,
                     "swap_enabled": False, "swap_mode": "hold", "swap_hotkey": dict(DEFAULT_SWAP_HOTKEY),
                     "onboarding_completed": False, "update_auto": True,
                     "update_state": {"last_check": 0, "latest": None, "skipped": None}}

    def __init__(self, path=None):
        self.path = Path(path or DATA / "desktop.json")
        existed = self.path.exists()
        self.lock = threading.RLock()
        self.data = {"voices": [], "favorite_ids": [], "library_version": LIBRARY_VERSION, "voice_id": None,
                     "input_device": None, "output_device": None,
                     "language": "en", "speech_language": None, "catalog_language": "en",
                     "output_gain_db": 0.0, "normalize_loudness": True,
                     "monitor_enabled": False, "monitor_device": None, "monitor_gain_db": 0.0,
                     "input_gain_db": 0.0, "monthly_char_limit": None, "asr_engine": "apple",
                     "vc_gate_enabled": False, "vc_gate_db": -45.0, "vc_text_voice_id": None,
                     "swap_enabled": False, "swap_mode": "hold", "swap_hotkey": dict(DEFAULT_SWAP_HOTKEY),
                     "onboarding_completed": False, "update_auto": True,
                     "update_state": {"last_check": 0, "latest": None, "skipped": None}}
        stored_version, file_ok = None, False
        stored = {}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                file_ok = True
                stored_version = stored.get("library_version")
                for field in ["voice_id", "input_device", "output_device"]:
                    if isinstance(stored.get(field), str) and stored[field].strip():
                        self.data[field] = stored[field][:200]
                if isinstance(stored.get("voices"), list):
                    self.data["voices"] = [v for v in stored["voices"] if isinstance(v, dict)
                                           and valid_id(v.get("id")) and isinstance(v.get("name"), str)]
                if isinstance(stored.get("favorite_ids"), list):
                    self.data["favorite_ids"] = [v for v in stored["favorite_ids"] if valid_id(v)]
                validators = {
                    "update_auto": lambda v: v if isinstance(v, bool) else True,
                    "update_state": _update_state,
                    "output_gain_db": _output_gain_db,
                    "normalize_loudness": lambda v: v if isinstance(v, bool) else True,
                    "monitor_enabled": _monitor_enabled,
                    "monitor_device": _monitor_device,
                    "monitor_gain_db": _monitor_gain_db,
                    "input_gain_db": _input_gain_db,
                    "monthly_char_limit": _monthly_char_limit,
                    "asr_engine": _asr_engine,
                    "vc_gate_enabled": _vc_gate_enabled,
                    "vc_gate_db": _vc_gate_db,
                    "swap_enabled": _swap_enabled,
                    "swap_mode": _swap_mode,
                    "swap_hotkey": _swap_hotkey,
                    "vc_text_voice_id": self._vc_text_voice_id,
                }
                for field, validate in validators.items():
                    if field in stored:
                        try:
                            self.data[field] = validate(stored[field])
                        except ValueError:
                            pass  # one bad stored value must not reset the others
        except (OSError, ValueError):
            pass
        language_prefs = stored if file_ok else {}
        validators = {"language": _language, "speech_language": _speech_language,
                      "catalog_language": _catalog_language}
        for field, validate in validators.items():
            if field == "language":
                fallback = "ru" if existed else "en"
            elif field == "speech_language":
                fallback = "ru-RU" if self.data["language"] == "ru" else None
            else:
                fallback = self.data["language"]
            try:
                self.data[field] = validate(language_prefs[field])
            except (KeyError, ValueError):
                self.data[field] = fallback
        missing_language_prefs = any(field not in language_prefs for field in validators)
        stored_flag = language_prefs.get("onboarding_completed")
        missing_onboarding = not isinstance(stored_flag, bool)
        # Existing installs that already hold a Fish key skip onboarding.
        self.data["onboarding_completed"] = (
            stored_flag if not missing_onboarding else (existed and has_key("FISH_API_KEY")))
        migrate = not (file_ok and stored_version == LIBRARY_VERSION)
        if migrate:
            self.data["library_version"] = LIBRARY_VERSION
            ids = self._library_ids()
            self.data["favorite_ids"] = [v for v in self.data["favorite_ids"] if v in ids]
        if self.data["voice_id"] not in self._library_ids():
            self.data["voice_id"] = self.data["voices"][0]["id"] if self.data["voices"] else None
        if self.data['vc_text_voice_id'] not in self._library_ids():
            self.data['vc_text_voice_id'] = None
        if migrate or missing_language_prefs or missing_onboarding:
            try:
                self.save()
            except OSError:
                pass
        i18n.set_language(self.data["language"])
        for v in self.data["voices"]:
            VOICES["fish_" + v["id"]], VOICE_NAMES["fish_" + v["id"]] = v["id"], v["name"]
        self._meta_store = _VoiceMetaStore(self.path.parent / "voice_metadata.json")
        self.metadata_pending = False

    def save(self):
        with self.lock:
            self.path.parent.mkdir(exist_ok=True, parents=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)

    def _library_ids(self):
        return {v["id"] for v in self.data["voices"]}

    def slug(self, voice_id):
        with self.lock:
            if voice_id not in self._library_ids():
                raise ValueError(t("errors.add_voice_first"))
            for slug, candidate in VOICES.items():
                if candidate == voice_id:
                    return slug
        raise ValueError(t("errors.add_voice_first"))

    def _vc_text_voice_id(self, value):
        if value is None:
            return None
        if not valid_id(value) or value not in self._library_ids():
            raise ValueError(t("errors.text_voice_not_found"))
        return value

    def voices(self):
        with self.lock:
            items = [{"id": v["id"], "slug": "fish_" + v["id"], "name": v["name"],
                      "language": v.get("language") or "ru", "source": "fish", "url": f"https://fish.audio/m/{v['id']}/",
                      "avatar_url": v.get("avatar_url"), "tags": v.get("tags", []),
                      "task_count": v.get("task_count")} for v in self.data["voices"]]
            by_id = {entry["id"]: entry for entry in items}
            for voice_id, entry in by_id.items():
                meta = self._meta_store.get(voice_id)
                if not meta:
                    continue
                if entry["avatar_url"] is None and meta.get("avatar_url"):
                    entry["avatar_url"] = meta["avatar_url"]
                if not entry["tags"] and meta.get("tags"):
                    entry["tags"] = meta["tags"]
                if entry["task_count"] is None and meta.get("task_count") is not None:
                    entry["task_count"] = meta["task_count"]
            return items

    def preferences(self):
        with self.lock:
            return {key: value for key, value in self.data.items()
                    if key not in ("voices", "library_version", "update_state")}

    def update_state(self) -> dict:
        with self.lock:
            return _update_state(self.data["update_state"])

    def set_update_state(self, state: dict) -> None:
        with self.lock:
            old_state = self.data["update_state"]
            self.data["update_state"] = _update_state(state)
            try:
                self.save()
            except OSError:
                self.data["update_state"] = old_state
                raise

    def update(self, values):
        with self.lock:
            if not isinstance(values, dict):
                raise ValueError(t("errors.preferences_invalid"))
            unknown = [k for k in values.keys() if k not in ALLOWED_PREF_KEYS]
            if unknown:
                raise ValueError(t("errors.unknown_prefs", names=', '.join(unknown)))
            if not values:
                raise ValueError(t("errors.preferences_empty"))

            new_data = dict(self.data)
            if "update_auto" in values:
                if not isinstance(values["update_auto"], bool):
                    raise ValueError(t("errors.update_auto_invalid"))
                new_data["update_auto"] = values["update_auto"]
            for field, validate in (("language", _language), ("speech_language", _speech_language),
                                    ("catalog_language", _catalog_language)):
                if field in values:
                    new_data[field] = validate(values[field])
            new_favorites = new_data["favorite_ids"]

            if "favorite_ids" in values:
                favs = values["favorite_ids"]
                if not isinstance(favs, list):
                    raise ValueError(t("errors.favorites_invalid"))
                new_favorites = list(dict.fromkeys(v for v in favs if valid_id(v)))[:500]

            for field in ["input_device", "output_device"]:
                if field in values:
                    if values[field] is not None and (not isinstance(values[field], str)
                                                     or not values[field].strip() or len(values[field]) > 200):
                        raise ValueError(t("errors.device_name_invalid"))
                    new_data[field] = values[field]

            if "onboarding_completed" in values:
                if not isinstance(values["onboarding_completed"], bool):
                    raise ValueError(t("errors.onboarding_flag_invalid"))
                new_data["onboarding_completed"] = values["onboarding_completed"]

            if "voice_id" in values:
                self.slug(values["voice_id"])
                new_data["voice_id"] = values["voice_id"]

            gain_value = None
            normalize_value = None
            invalid_gain = False
            if "output_gain_db" in values:
                try:
                    gain_value = _output_gain_db(values["output_gain_db"])
                except ValueError:
                    invalid_gain = True
            if "normalize_loudness" in values:
                if not isinstance(values["normalize_loudness"], bool):
                    invalid_gain = True
                else:
                    normalize_value = values["normalize_loudness"]

            monitor_enabled_value = None
            monitor_device_value = None
            monitor_gain_value = None
            invalid_monitor = False
            if "monitor_enabled" in values:
                try:
                    monitor_enabled_value = _monitor_enabled(values["monitor_enabled"])
                except ValueError:
                    invalid_monitor = True
            if "monitor_device" in values:
                try:
                    monitor_device_value = _monitor_device(values["monitor_device"])
                except ValueError:
                    invalid_monitor = True
            if "monitor_gain_db" in values:
                try:
                    monitor_gain_value = _monitor_gain_db(values["monitor_gain_db"])
                except ValueError:
                    invalid_monitor = True

            input_gain_value = None
            invalid_input_gain = False
            if "input_gain_db" in values:
                try:
                    input_gain_value = _input_gain_db(values["input_gain_db"])
                except ValueError:
                    invalid_input_gain = True

            limit_value, limit_set = None, "monthly_char_limit" in values
            if limit_set:
                limit_value = _monthly_char_limit(values["monthly_char_limit"])

            for field, validate in (("vc_gate_enabled", _vc_gate_enabled), ("vc_gate_db", _vc_gate_db),
                                    ("swap_enabled", _swap_enabled), ("swap_mode", _swap_mode),
                                    ("swap_hotkey", _swap_hotkey), ("vc_text_voice_id", self._vc_text_voice_id)):
                if field in values:
                    new_data[field] = validate(values[field])

            engine_set = "asr_engine" in values
            engine_value = _asr_engine(values["asr_engine"]) if engine_set else None

            if invalid_gain:
                raise ValueError(t("errors.output_preferences_invalid"))
            if invalid_monitor:
                raise ValueError(t("errors.monitor_preferences_invalid"))
            if invalid_input_gain:
                raise ValueError(t("errors.input_gain_bounds"))

            new_data["favorite_ids"] = new_favorites
            if gain_value is not None:
                new_data["output_gain_db"] = gain_value
            if normalize_value is not None:
                new_data["normalize_loudness"] = normalize_value
            if monitor_enabled_value is not None:
                new_data["monitor_enabled"] = monitor_enabled_value
            if "monitor_device" in values:
                new_data["monitor_device"] = monitor_device_value
            if monitor_gain_value is not None:
                new_data["monitor_gain_db"] = monitor_gain_value
            if input_gain_value is not None:
                new_data["input_gain_db"] = input_gain_value
            if limit_set:
                new_data["monthly_char_limit"] = limit_value
            if engine_set:
                new_data["asr_engine"] = engine_value

            old_data = self.data
            self.data = new_data
            try:
                self.save()
            except OSError:
                self.data = old_data
                raise
            if "language" in values:
                i18n.set_language(new_data["language"])
            return self.preferences()

    def add(self, voice_id):
        if not valid_id(voice_id):
            raise ValueError(t("errors.voice_id_invalid"))
        with self.lock:
            if voice_id in self._library_ids():
                return next(v for v in self.voices() if v["id"] == voice_id)
        voice = item(fish_get("/model/" + voice_id), self.preferences()["catalog_language"])
        if not voice:
            raise ValueError(t("errors.catalog_model_required"))
        with self.lock:
            if voice_id not in self._library_ids():
                self.data["voices"].append(voice)
                VOICES["fish_" + voice_id], VOICE_NAMES["fish_" + voice_id] = voice_id, voice["name"]
                self.save()
        return voice

    def remove(self, voice_id):
        if not valid_id(voice_id):
            raise ValueError(t("errors.voice_id_invalid"))
        with self.lock:
            if voice_id not in self._library_ids():
                raise ValueError(t("errors.voice_not_added"))
            remaining = [v for v in self.voices() if v["id"] != voice_id]
            if not remaining:
                raise ValueError(t("errors.last_voice"))
            old_data = self.data
            new_data = dict(self.data)
            new_data["voices"] = [v for v in self.data["voices"] if v["id"] != voice_id]
            new_data["favorite_ids"] = [v for v in self.data["favorite_ids"] if v != voice_id]
            if self.data.get("voice_id") == voice_id:
                new_data["voice_id"] = remaining[0]["id"]
            if self.data.get('vc_text_voice_id') == voice_id:
                new_data['vc_text_voice_id'] = None
            self.data = new_data
            try:
                self.save()
            except OSError:
                self.data = old_data
                raise
            VOICES.pop(USER_VOICES_KEY + voice_id, None)
            VOICE_NAMES.pop(USER_VOICES_KEY + voice_id, None)
        return self.preferences()

    def remove_many(self, voice_ids):
        if (not isinstance(voice_ids, list) or not 1 <= len(voice_ids) <= 500
                or any(not valid_id(v) for v in voice_ids)):
            raise ValueError(t("errors.voice_list_invalid"))
        ids = list(dict.fromkeys(voice_ids))
        with self.lock:
            if any(v not in self._library_ids() for v in ids):
                raise ValueError(t("errors.voice_not_added"))
            removed = set(ids)
            remaining = [v for v in self.data["voices"] if v["id"] not in removed]
            if not remaining:
                raise ValueError(t("errors.all_voices"))
            old_data = self.data
            self.data = dict(old_data)
            self.data["voices"] = remaining
            self.data["favorite_ids"] = [v for v in old_data["favorite_ids"] if v not in removed]
            if old_data["voice_id"] in removed:
                self.data["voice_id"] = remaining[0]["id"]
            if old_data.get('vc_text_voice_id') in removed:
                self.data['vc_text_voice_id'] = None
            try:
                self.save()
            except OSError:
                self.data = old_data
                raise
            for voice_id in ids:
                VOICES.pop(USER_VOICES_KEY + voice_id, None)
                VOICE_NAMES.pop(USER_VOICES_KEY + voice_id, None)
            return self.preferences()

    def lookup_voice_id(self, value):
        if not isinstance(value, str) or not value.strip():
            return None
        target = value.strip()
        if target in VOICES:
            return VOICES[target]
        if valid_id(target):
            for slug, vid in VOICES.items():
                if vid == target:
                    return target
        return None

    def search(self, query="", page=1, *, gender="all", sort_by="task_count"):
        if not isinstance(query, str) or len(query) > 120:
            raise ValueError(t("errors.search_name_invalid"))
        if not isinstance(gender, str) or gender not in GENDERS:
            raise ValueError(t("errors.search_gender_invalid"))
        if not isinstance(sort_by, str) or sort_by not in SORT_OPTIONS:
            raise ValueError(t("errors.search_sort_invalid"))
        if isinstance(page, bool) or not isinstance(page, int):
            raise ValueError(t("errors.search_page_integer"))
        if not 1 <= page <= 10:
            raise ValueError(t("errors.search_page_limit"))
        language = self.preferences()["catalog_language"]
        params = {"page_size": 12, "page_number": page, "sort_by": sort_by}
        if language != "all":
            params["language"] = language
        title = query.strip()
        if title:
            params["title"] = title
        if gender != "all":
            params["tag"] = gender
        data = fish_get("/model", params)
        if not isinstance(data.get("items"), list):
            raise ValueError(t("errors.fish_catalog_format"))
        items = []
        for model in data["items"]:
            normalized = item(model, language)
            if not normalized:
                continue
            if gender != "all" and gender not in normalized["tags"]:
                continue
            items.append(normalized)
        return {"items": items, "has_more": bool(data.get("has_more")) and page < 10, "page": page,
                "gender": gender, "sort_by": sort_by, "query": title}

    def fetch_metadata(self, voice_ids, *, max_workers=4):
        """Fetch uncached metadata in at most four workers; isolate model failures."""
        if not isinstance(voice_ids, (list, tuple)):
            return {}
        ids = list(dict.fromkeys(v for v in voice_ids if valid_id(v)))[:META_MAX_ENTRIES]
        workers = min(4, max(1, max_workers)) if type(max_workers) is int else 4
        result, missing = {}, []
        for voice_id in ids:
            cached = self._meta_store.get(voice_id)
            if cached is not None:
                result[voice_id] = cached
            else:
                missing.append(voice_id)
        if not missing:
            return result

        def fetch(voice_id):
            try:
                model = fish_get("/model/" + voice_id)
                if not isinstance(model, dict) or model.get("_id") != voice_id:
                    return voice_id, None
                self._meta_store.put(voice_id, model)
                return voice_id, self._meta_store.get(voice_id)
            except (ValueError, OSError, httpx.HTTPError):
                return voice_id, None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for voice_id, meta in pool.map(fetch, missing):
                if meta is not None:
                    result[voice_id] = meta
        return result

    def clear_metadata_cache(self):
        self._meta_store.clear()

    def start_metadata(self):
        """Return immediately while a bounded background batch warms the library."""
        with self.lock:
            if self.metadata_pending:
                return
            ids = [v["id"] for v in self.data["voices"]
                   if self._meta_store.get(v["id"]) is None][:META_MAX_ENTRIES]
            if not ids:
                return
            self.metadata_pending = True

        def run():
            try:
                self.fetch_metadata(ids)
            finally:
                with self.lock:
                    self.metadata_pending = False

        threading.Thread(target=run, daemon=True, name="voice-metadata").start()
