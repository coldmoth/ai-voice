"""Per-day count of synthesized characters (numbers only, never phrase text)."""
import json
import os
import threading
import time
from datetime import date
from pathlib import Path

KEEP_DAYS = 62
FLUSH_SECONDS = 5.0


class UsageTracker:
    def __init__(self, path, *, today=date.today, clock=time.monotonic):
        self.path = Path(path) if path else None
        self._today = today
        self._clock = clock
        self._lock = threading.Lock()
        self._dirty = False
        self._last_flush = None
        self.days: dict[str, int] = {}
        try:
            if self.path is None:
                raise ValueError
            raw = json.loads(self.path.read_text(encoding="utf-8")).get("days", {})
            for key, value in raw.items():
                date.fromisoformat(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.days[key] = value
        except (OSError, ValueError, AttributeError, TypeError):
            self.days = {}

    def _prune(self):
        for key in sorted(self.days)[:-KEEP_DAYS]:
            del self.days[key]

    def add(self, chars: int) -> None:
        if not isinstance(chars, int) or chars <= 0:
            return
        with self._lock:
            key = self._today().isoformat()
            self.days[key] = self.days.get(key, 0) + chars
            self._prune()
            self._dirty = True
            now = self._clock()
            if self._last_flush is None or now - self._last_flush >= FLUSH_SECONDS:
                self._write(now)

    def _write(self, now):
        if self.path is None:
            self._dirty = False
            self._last_flush = now
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"days": self.days}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            return
        self._dirty = False
        self._last_flush = now

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._write(self._clock())

    def totals(self, limit=None) -> dict:
        with self._lock:
            today = self._today()
            month = today.strftime("%Y-%m")
            return {"today": self.days.get(today.isoformat(), 0),
                    "month": sum(v for k, v in self.days.items() if k.startswith(month)),
                    "limit": limit}
