"""Commit only final utterances, once per recognizer session identity."""
import time
from collections.abc import Callable

FILLERS = frozenset({
    "\u0434\u0430",  # da
    "\u0430\u0433\u0430",  # aga
    "\u0443\u0433\u0443",  # ugu
    "\u044d",  # e
    "\u044d\u043c",  # em
    "\u043c",  # m
})
FILLER_REPEAT_WINDOW = 4.0
_STRIP = ".,!?… \t\n"


def normalize_filler(text: str) -> str:
    return text.lower().strip(_STRIP)


class Committer:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._committed: set[str] = set()
        self._clock = clock
        self._filler_at: dict[str, float] = {}

    def accept(self, event: dict) -> str | None:
        if event.get("type") != "final":
            return None
        identity = event.get("utterance_id")
        text = event.get("text")
        if not isinstance(identity, str) or not identity or not isinstance(text, str):
            return None
        text = text.strip()
        if not text or identity in self._committed:
            return None
        self._committed.add(identity)
        norm = normalize_filler(text)
        if norm in FILLERS:
            now = self._clock()
            last = self._filler_at.get(norm)
            if last is not None and now - last < FILLER_REPEAT_WINDOW:
                return None
            self._filler_at[norm] = now
        return text


_WORD_STRIP = ".,!?;:\u2026\"'\u00ab\u00bb()[]\u2014\u2013-"
_BOUNDARY = ".,!?;:"


def _norm_word(word: str) -> str:
    return word.lower().strip(_WORD_STRIP)


def _tokens(text: str) -> list[str]:
    return [w for w in text.split() if _norm_word(w)]


class PartialSplitter:
    """Emit a stable prefix of a long partial early; the final yields the rest."""

    ANCHOR_SECONDS = 0.6
    TAIL_WORDS = 3
    MAX_UTTERANCES = 8

    def __init__(self, min_words: int = 12, clock: Callable[[], float] = time.monotonic):
        self.min_words = min_words
        self._clock = clock
        self._committed: dict[str, list[str]] = {}
        self._anchor: dict[str, tuple[list[str], float]] = {}

    def _forget_old(self, keep: str) -> None:
        for store in (self._committed, self._anchor):
            while len(store) > self.MAX_UTTERANCES:
                oldest = next(iter(store))
                if oldest == keep:
                    break
                del store[oldest]

    def partial(self, utterance_id: str, text: str) -> list[str]:
        committed = self._committed.get(utterance_id, [])
        words = _tokens(text)
        norm = [_norm_word(w) for w in words]
        if norm[:len(committed)] != committed:
            self._anchor.pop(utterance_id, None)
            return []
        rem = words[len(committed):]
        rem_norm = norm[len(committed):]
        n = len(rem)
        if n < self.min_words:
            self._anchor.pop(utterance_id, None)
            return []
        now = self._clock()
        anchor = self._anchor.get(utterance_id)
        if anchor is not None and rem_norm[:len(anchor[0])] == anchor[0]:
            if now - anchor[1] < self.ANCHOR_SECONDS:
                return []
            k = len(anchor[0])
            prefix = rem[:k]
            cut = k
            for i in range(k - 1, -1, -1):
                if prefix[i][-1] in _BOUNDARY:
                    cut = i + 1
                    break
            phrase = " ".join(prefix[:cut])
            self._committed[utterance_id] = committed + rem_norm[:cut]
            self._anchor.pop(utterance_id, None)
            self._forget_old(utterance_id)
            return [phrase]
        self._anchor[utterance_id] = (rem_norm[:n - self.TAIL_WORDS], now)
        return []

    def discard(self, utterance_id: str) -> None:
        self._committed.pop(utterance_id, None)
        self._anchor.pop(utterance_id, None)

    def final(self, utterance_id: str, text: str) -> str:
        committed = self._committed.pop(utterance_id, [])
        self._anchor.pop(utterance_id, None)
        if not committed:
            return text.strip()
        words = _tokens(text)
        norm = [_norm_word(w) for w in words]
        m = 0
        while m < len(committed) and m < len(norm) and norm[m] == committed[m]:
            m += 1
        rest = words[m:] if m > 0 else words[len(committed):]
        return " ".join(rest)
