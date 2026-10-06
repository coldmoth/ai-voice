"""Targeted catalog/preferences regression tests for monitor fields; offline-only."""
import importlib
import json

import pytest


def module(name):
    return importlib.import_module(f"ai_voice.{name}")


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(module("secrets"), "load_key", lambda: "test-key")
    Cat = module("catalog").Catalog
    return Cat(path=tmp_path / "state" / "desktop.json")


def test_monitor_blacklist_substrings(monkeypatch):
    is_blacklisted = module("catalog")._is_monitor_blacklisted
    for name in ["AI Voice", "AI Voice Mic", "ai voice aux", "Voicemeeter AI Voice Output",
                 "MIC", "Mac Microphone", "Microphone (USB)", "USB MIC Pro",
                 "Internal Mic Array", "blackhole 2ch", "BlackHole 2ch",
                 "ai voice", "aI vOiCe"]:
        assert is_blacklisted(name) is True, name
    for name in ["MacBook Pro Speakers", "External Headphones",
                 "Studio Monitors", "Output 1-2 (MOTU)"]:
        assert is_blacklisted(name) is False, name


def test_default_preferences_include_monitor_fields(catalog):
    prefs = catalog.preferences()
    assert prefs["monitor_enabled"] is False
    assert prefs["monitor_device"] is None
    assert prefs["monitor_gain_db"] == 0.0
    # Existing defaults preserved.
    assert prefs["output_gain_db"] == 0.0
    assert prefs["normalize_loudness"] is True


def test_monitor_enabled_validates(catalog):
    for bad in ["yes", 1, None, "false"]:
        with pytest.raises(ValueError):
            catalog.update({"monitor_enabled": bad})
    catalog.update({"monitor_enabled": True})
    assert catalog.preferences()["monitor_enabled"] is True
    catalog.update({"monitor_enabled": False})
    assert catalog.preferences()["monitor_enabled"] is False


def test_monitor_device_validates(catalog):
    # Empty / whitespace normalize to None (user choice "none"), not an error.
    for blank in ["", " "]:
        catalog.update({"monitor_device": blank})
        assert catalog.preferences()["monitor_device"] is None
    # Invalid types / too long still raise.
    for bad in ["x" * 201, 42, [], {}]:
        with pytest.raises(ValueError):
            catalog.update({"monitor_device": bad})
    catalog.update({"monitor_device": None})
    assert catalog.preferences()["monitor_device"] is None
    catalog.update({"monitor_device": "External Headphones"})
    assert catalog.preferences()["monitor_device"] == "External Headphones"


def test_monitor_gain_validates(catalog):
    for bad in [True, "5", float("inf"), float("nan"), 13, -25, None]:
        with pytest.raises(ValueError):
            catalog.update({"monitor_gain_db": bad})
    catalog.update({"monitor_gain_db": 6})
    assert catalog.preferences()["monitor_gain_db"] == 6.0
    catalog.update({"monitor_gain_db": -12.5})
    assert catalog.preferences()["monitor_gain_db"] == pytest.approx(-12.5)


def test_monitor_update_is_atomic_on_validation_failure(catalog):
    before = catalog.preferences()
    with pytest.raises(ValueError):
        catalog.update({"monitor_enabled": True, "monitor_gain_db": 100})
    after = catalog.preferences()
    # Atomic rollback: nothing about the partial update leaked through.
    assert after == before
    assert after["monitor_enabled"] is False
    assert after["monitor_gain_db"] == 0.0


def test_monitor_fields_persist_across_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(module("secrets"), "load_key", lambda: "test-key")
    state = tmp_path / "state" / "desktop.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"voice_id": "db89e349112e44dca6820e0cb2d414cc",
                                 "favorite_ids": [], "input_device": "MIC",
                                 "output_device": "AI Voice", "output_gain_db": 0.0,
                                 "normalize_loudness": True,
                                 "monitor_enabled": True,
                                 "monitor_device": "External Headphones",
                                 "monitor_gain_db": -3.5}))
    cat = module("catalog").Catalog(path=state)
    prefs = cat.preferences()
    assert prefs["monitor_enabled"] is True
    assert prefs["monitor_device"] == "External Headphones"
    assert prefs["monitor_gain_db"] == pytest.approx(-3.5)


def test_old_state_files_fill_monitor_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(module("secrets"), "load_key", lambda: "test-key")
    state = tmp_path / "state" / "desktop.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    # Legacy state without monitor fields: defaults must be filled in safely.
    state.write_text(json.dumps({"voice_id": "db89e349112e44dca6820e0cb2d414cc",
                                 "favorite_ids": ["a" * 32],
                                 "input_device": "MIC",
                                 "output_device": "AI Voice",
                                 "output_gain_db": -6.0,
                                 "normalize_loudness": False}))
    cat = module("catalog").Catalog(path=state)
    prefs = cat.preferences()
    assert prefs["monitor_enabled"] is False
    assert prefs["monitor_device"] is None
    assert prefs["monitor_gain_db"] == 0.0


def test_unknown_preference_field_is_rejected_after_monitor_introduction(catalog):
    for key in ["unknown_field", "mon_gain", "monitor"]:
        with pytest.raises(ValueError):
            catalog.update({key: 1})


def test_invalid_monitor_preference_does_not_mutate_other_fields(catalog):
    before = catalog.preferences()
    with pytest.raises(ValueError):
        catalog.update({"monitor_enabled": "yes", "favorite_ids": ["b" * 32]})
    after = catalog.preferences()
    assert before == after
    assert "b" * 32 not in after["favorite_ids"]


def test_config_dataclass_accepts_and_rejects_monitor_fields():
    cfg = module("config").Config
    # Defaults match catalog defaults.
    cfg0 = cfg()
    assert cfg0.monitor_enabled is False
    assert cfg0.monitor_device is None
    assert cfg0.monitor_gain_db == 0.0
    # Custom monitor configuration is accepted.
    cfg1 = cfg(monitor_enabled=True, monitor_device="External Headphones", monitor_gain_db=-6)
    assert cfg1.monitor_enabled is True
    assert cfg1.monitor_device == "External Headphones"
    assert cfg1.monitor_gain_db == pytest.approx(-6)
    # Invalid monitor_gain_db rejected with the same -24..12 dB range as output_gain_db.
    for bad in [13, -25, float("nan"), True]:
        with pytest.raises(ValueError):
            cfg(monitor_gain_db=bad)
    for bad in ["yes", 1, None, "true"]:
        with pytest.raises(ValueError):
            cfg(monitor_enabled=bad)
    with pytest.raises(ValueError):
        cfg(monitor_device=42)


def test_config_monitor_linear_gain_matches_expected_curve():
    cfg = module("config").Config
    assert cfg(monitor_gain_db=0).monitor_linear_gain() == pytest.approx(1.0)
    assert cfg(monitor_gain_db=-6).monitor_linear_gain() == pytest.approx(10 ** (-6 / 20))
    assert cfg(monitor_gain_db=12).monitor_linear_gain() == pytest.approx(10 ** (12 / 20))
