"""In-memory LRU cache of short synthesized phrases (never written to disk)."""
from collections import OrderedDict

MAX_TEXT_CHARS = 40
MAX_ENTRY_BYTES = 512 * 1024


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split()).strip(".,!?… ")


class TTSCache:
    def __init__(self, max_entries: int = 32, max_bytes: int = 8 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple, bytes] = OrderedDict()
        self._bytes = 0

    @staticmethod
    def key(voice_id, model, sample_rate, normalize_loudness, text):
        norm = normalize_text(text)
        if not norm or len(norm) > MAX_TEXT_CHARS:
            return None
        return (voice_id, model, sample_rate, bool(normalize_loudness), norm)

    def get(self, key):
        if key is None or key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key, pcm: bytes) -> None:
        if key is None or not pcm or len(pcm) > min(MAX_ENTRY_BYTES, self.max_bytes):
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= len(old)
        self._items[key] = pcm
        self._bytes += len(pcm)
        while self._items and (len(self._items) > self.max_entries or self._bytes > self.max_bytes):
            _, dropped = self._items.popitem(last=False)
            self._bytes -= len(dropped)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def __len__(self):
        return len(self._items)

    @property
    def size_bytes(self):
        return self._bytes
