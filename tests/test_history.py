import asyncio

import pytest

from ai_voice.catalog import Catalog
from ai_voice.config import Config
from ai_voice.desktop_control import ConflictError, DesktopControl, NotFoundError
from ai_voice.gui_audio import AudioSession


def make(tmp_path):
    return DesktopControl(Catalog(tmp_path / "desktop.json"))


def test_history_limit_and_order(tmp_path):
    c = make(tmp_path)
    for i in range(20):
        c.add_history(f"фраза {i}", "mic")
    h = c.snapshot()["history"]
    assert len(h) == 15 and h[0]["text"] == "фраза 19" and h[-1]["text"] == "фраза 5"
    assert set(h[0]) == {"id", "text", "source", "at"}


def test_history_text_bounded(tmp_path):
    c = make(tmp_path)
    c.add_history("я" * 500, "text")
    assert len(c.snapshot()["history"][0]["text"]) <= 240


@pytest.mark.asyncio
async def test_repeat_unknown_conflict_and_ok(tmp_path):
    c = make(tmp_path)
    with pytest.raises(NotFoundError):
        await c.command({"action": "repeat", "id": 999})
    c.add_history("привет", "mic")
    hid = c.snapshot()["history"][0]["id"]
    with pytest.raises(ConflictError):
        await c.command({"action": "repeat", "id": hid})
    c.session = AudioSession(Config(), "k")
    await c.command({"action": "repeat", "id": hid})
    assert c.session.engine.queue.get_nowait()[1] == "привет"


@pytest.mark.asyncio
async def test_history_cleared_on_stop(tmp_path):
    c = make(tmp_path)
    c.add_history("привет", "mic")
    await c.stop()
    assert c.snapshot()["history"] == []


def test_limit_pref_validation(tmp_path):
    cat = Catalog(tmp_path / "d.json")
    assert cat.update({"monthly_char_limit": 5000})["monthly_char_limit"] == 5000
    assert cat.update({"monthly_char_limit": None})["monthly_char_limit"] is None
    for bad in (0, -1, "5", True, 1.5):
        with pytest.raises(ValueError):
            cat.update({"monthly_char_limit": bad})
    cat.update({"monthly_char_limit": 7})
    assert Catalog(tmp_path / "d.json").preferences()["monthly_char_limit"] == 7
