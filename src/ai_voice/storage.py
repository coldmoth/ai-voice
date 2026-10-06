"""Disk usage of everything AI Voice keeps on this Mac, with safe clearing."""
import os
import shutil
import threading
import time
from pathlib import Path

from .i18n import t
from . import paths, vc_engine

CACHE_SECONDS = 30
SCAN_TIMEOUT = 20
LOG_KEEP_DAYS = 7
WORKER_LOG = "vc_worker.log"
UPLOAD_FRESH_SECONDS = 600  # newer upload folders may belong to an import in progress


class StorageConflict(Exception):
    """The category is in use right now and cannot be cleared."""


class StorageBadRequest(ValueError):
    """Unknown or non-clearable category."""


def dir_size(path):
    """Sum of file sizes under path; symlinks inside are never followed or counted twice."""
    path = os.fspath(path)
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return info.st_size
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _by_link(path):
    """Size of a category root that is itself a symlink (target is measured)."""
    try:
        return dir_size(os.path.realpath(path))
    except OSError:
        return 0


def _app_bundle(resources):
    for parent in Path(resources).parents:
        if parent.suffix == ".app":
            return parent
    return None


def cleanup_old_logs(logs_dir, days=LOG_KEEP_DAYS, now=None):
    """Delete log files older than ``days``; returns how many were removed."""
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    try:
        entries = list(Path(logs_dir).iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class StorageService:
    def __init__(self, *, vc=None, control=None, catalog=None, run_async=None, data=None, uploads=None,
                 spike=None, vc_python=None, hf_hub=None, resources="env", clock=time.monotonic):
        self.vc, self.control, self.catalog, self.run_async = vc, control, catalog, run_async
        self.data = Path(data if data is not None else paths.DATA)
        self.uploads = Path(uploads if uploads is not None else (getattr(vc, "uploads", None) or self.data / "vc-uploads"))
        self.spike = Path(spike if spike is not None else paths.VC_SPIKE)
        self.vc_python = Path(vc_python if vc_python is not None else paths.VC_PYTHON)
        self.hf_hub = Path(hf_hub if hf_hub is not None else Path.home() / ".cache" / "huggingface" / "hub")
        self.resources = os.environ.get("AI_VOICE_RESOURCES") if resources == "env" else resources
        self.clock = clock
        self._cond = threading.Condition()
        self._result = None
        self._at = None
        self._running = False
        self._error = None

    # --- scanning -------------------------------------------------------------------------

    def _voice_items(self):
        root = self.data / "vc-voices"
        items = []
        try:
            entries = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return items
        for entry in entries:
            label = entry.name
            try:
                label = str(self.vc.store.get(entry.name).get("name") or label)
            except Exception:
                pass
            items.append({"id": entry.name, "label": label, "bytes": dir_size(entry)})
        return items

    def _gigaam_dirs(self):
        try:
            return sorted(p for p in self.hf_hub.glob("models--*") if "gigaam" in p.name.lower() and p.is_dir())
        except OSError:
            return []

    def scan(self):
        voices = self._voice_items()
        categories = [
            {"id": "vc_voices", "label": t("settings.storage.my_voices"), "bytes": sum(i["bytes"] for i in voices), "clearable": True,
             "note": "", "items": voices},
            {"id": "vc_models", "label": t("settings.storage.live_models"), "bytes": _by_link(self.spike), "clearable": False,
             "note": t("settings.storage.live_models_note")},
            {"id": "vc_env", "label": t("settings.storage.live_environment"), "bytes": _by_link(self.vc_python.parents[1]),
             "clearable": False, "note": t("settings.storage.live_environment_note")},
            {"id": "asr_gigaam", "label": t("settings.storage.gigaam"), "bytes": sum(dir_size(p) for p in self._gigaam_dirs()),
             "clearable": True, "note": t("settings.storage.gigaam_note")},
            {"id": "logs", "label": t("settings.storage.logs"), "bytes": dir_size(self.data / "logs"), "clearable": True, "note": ""},
            {"id": "uploads", "label": t("settings.storage.uploads"), "bytes": dir_size(self.uploads), "clearable": True,
             "note": ""},
            {"id": "cache", "label": t("settings.storage.catalog_cache"), "bytes": dir_size(self.data / "voice_metadata.json"),
             "clearable": True, "note": ""},
        ]
        engine = vc_engine.installer()
        if not engine._dev_runtime() and engine.root.exists():
            categories = [c for c in categories if c["id"] not in ("vc_models", "vc_env")]
            categories.insert(1, {"id": "vc_engine", "label": t("settings.storage.vc_engine"),
                                  "bytes": dir_size(engine.root), "clearable": True})
        bundle = _app_bundle(self.resources) if self.resources else None
        if bundle is not None:
            categories.append({"id": "app", "label": t("settings.storage.app"), "bytes": dir_size(bundle), "clearable": False,
                               "note": ""})
        return {"total": sum(c["bytes"] for c in categories), "categories": categories}

    def _scan_worker(self):
        try:
            result, error = self.scan(), None
        except Exception as exc:  # a failed scan must release waiters
            result, error = None, exc
        with self._cond:
            if result is not None:
                self._result, self._at = result, self.clock()
            self._error = error
            self._running = False
            self._cond.notify_all()

    def get(self, timeout=SCAN_TIMEOUT):
        """Cached scan (30 s) or a fresh one; TimeoutError if it takes longer than ``timeout``."""
        with self._cond:
            if self._result is not None and self._at is not None and self.clock() - self._at < CACHE_SECONDS:
                return self._result
            if not self._running:
                self._running = True
                self._error = None
                threading.Thread(target=self._scan_worker, daemon=True, name="storage-scan").start()
            deadline = time.monotonic() + timeout
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("scan timeout")
                self._cond.wait(remaining)
            if self._error is not None:
                raise RuntimeError(t("errors.storage_count_failed"))
            return self._result

    def invalidate(self):
        with self._cond:
            self._at = None

    # --- clearing -------------------------------------------------------------------------

    def _training_busy(self):
        status = self.vc.trainer.status()
        if not isinstance(status, dict):
            return False
        return status.get("state") in ("running", "paused") or bool(status.get("queue"))

    def _vc_busy(self):
        return self.vc.busy

    def _empty_dir(self, path, keep=(), min_age=0):
        try:
            entries = list(Path(path).iterdir())
        except OSError:
            return
        cutoff = time.time() - min_age
        for entry in entries:
            if entry.name in keep:
                continue
            try:
                if min_age and entry.lstat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    def clear(self, category, item_id=None):
        if category == "vc_engine":
            if self._vc_busy() or self._training_busy():
                raise StorageConflict(t("errors.engine_busy"))
            try:
                vc_engine.installer().remove()
            except RuntimeError as exc:
                if str(exc) == "busy":
                    raise StorageConflict(t("errors.engine_busy")) from None
                raise StorageBadRequest(str(exc)) from None
        elif category == "vc_voices":
            if item_id is not None:
                if not isinstance(item_id, str) or not item_id:
                    raise StorageBadRequest(t("errors.voice_id_invalid"))
                self.run_async(self.vc.delete(item_id))
            else:
                if self._vc_busy() or self._training_busy():
                    raise StorageConflict(t("errors.stop_live_training"))
                try:
                    for item in self._voice_items():
                        self.vc.trainer.remove_queued(item["id"])
                        self.run_async(self.vc.delete(item["id"]))
                finally:
                    self.invalidate()  # a failure midway leaves the cached list stale
        elif category == "asr_gigaam":
            state = self.control.snapshot()
            if (state.get("active") and state.get("mode") == "mic"
                    and self.catalog.preferences().get("asr_engine") == "gigaam"):
                raise StorageConflict(t("errors.stop_gigaam_mic"))
            for found in self._gigaam_dirs():
                shutil.rmtree(found, ignore_errors=True)
        elif category == "logs":
            keep = (WORKER_LOG,) if self.vc is not None and (self._vc_busy() or self._training_busy()) else ()
            self._empty_dir(self.data / "logs", keep=keep)
        elif category == "uploads":
            self._empty_dir(self.uploads, min_age=UPLOAD_FRESH_SECONDS)
        elif category == "cache":
            self.catalog.clear_metadata_cache()
        else:
            raise StorageBadRequest(t("errors.category_not_clearable"))
        self.invalidate()
        return self.get()
