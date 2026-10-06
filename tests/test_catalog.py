"""Catalog and preference tests; no network, microphone, or playback."""
import importlib
import json
import threading
from pathlib import Path

import pytest
from helpers import TEST_VOICE_ID, write_library


def test_language_fresh_install_is_en(tmp_path):
    cat = module("catalog").Catalog(tmp_path / "desktop.json")
    for prefs in (cat.preferences(), json.loads(cat.path.read_text()),
                  module("catalog").Catalog(cat.path).preferences()):
        assert prefs["language"] == "en"
        assert prefs["speech_language"] is None
        assert prefs["catalog_language"] == "en"


def test_language_existing_install_is_ru(tmp_path):
    voices = [{"id": TEST_VOICE_ID, "name": "Test"}]
    path = write_library(tmp_path / "desktop.json", voices, TEST_VOICE_ID)
    cat = module("catalog").Catalog(path)
    assert cat.data["voices"] == voices
    assert cat.preferences()["voice_id"] == TEST_VOICE_ID
    for prefs in (cat.preferences(), json.loads(path.read_text())):
        assert prefs["language"] == "ru"
        assert prefs["speech_language"] == "ru-RU"
        assert prefs["catalog_language"] == "ru"


def test_language_corrupt_file_is_ru(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text("{")
    assert module("catalog").Catalog(path).preferences()["language"] == "ru"


def test_language_invalid_stored_value(tmp_path):
    path = write_library(tmp_path / "desktop.json", [], language="de")
    assert module("catalog").Catalog(path).preferences()["language"] == "ru"


@pytest.mark.parametrize("language,speech,catalog_language,expected", [
    ("en", None, "all", ("en", None, "all")),
    ("ru", "fr-FR", "en", ("ru", "fr-FR", "en")),
    ("en", "ru RU", "xx", ("en", None, "en")),
    ("ru", "ru RU", "xx", ("ru", "ru-RU", "ru")),
])
def test_language_stored_preferences(tmp_path, language, speech, catalog_language, expected):
    path = write_library(tmp_path / "desktop.json", [], language=language,
                         speech_language=speech, catalog_language=catalog_language)
    prefs = module("catalog").Catalog(path).preferences()
    assert tuple(prefs[k] for k in ("language", "speech_language", "catalog_language")) == expected
    assert module("i18n").get_language() == language


def test_update_language_validates_and_switches_t(tmp_path, monkeypatch):
    cat = module("catalog").Catalog(tmp_path / "desktop.json")
    cat.update({"language": "ru"})
    assert module("i18n").get_language() == "ru"
    assert module("i18n").t("common.ok") == "ОК"
    before = cat.preferences()
    for patch in ({"language": "de"}, {"catalog_language": "xx"},
                  {"speech_language": "ru RU"}, {"language": []},
                  {"catalog_language": None}, {"speech_language": 42}):
        with pytest.raises(ValueError):
            cat.update(patch)
        assert cat.preferences() == before
    cat.update({"speech_language": None})
    cat.update({"speech_language": "zh-Hant-TW"})
    assert cat.preferences()["speech_language"] == "zh-Hant-TW"
    with monkeypatch.context() as patch:
        patch.setattr(cat, "save", lambda: (_ for _ in ()).throw(OSError("full disk")))
        with pytest.raises(OSError):
            cat.update({"language": "en"})
    assert cat.preferences()["language"] == "ru"
    assert module("i18n").get_language() == "ru"
    cat.update({"language": "en"})
    assert module("i18n").get_language() == "en"


@pytest.mark.parametrize("pref,system,supported,expected", [
    ("ru-RU", "en_US", ["en-US", "ru-RU"], "ru-RU"),
    ("de-DE", "en_US", ["en-US"], "en-US"),
    (None, "fr_FR", ["fr-FR"], "fr-FR"),
    (None, None, [], "en-US"),
    ("ru-RU", None, [], "ru-RU"),
    (None, "de_DE", ["fr-FR"], "en-US"),
])
def test_resolve_speech_language(pref, system, supported, expected):
    assert module("catalog").resolve_speech_language(pref, system, supported) == expected


def test_item_language_filter():
    model = _fish_model("a" * 32)
    model["languages"] = ["en"]
    assert module("catalog").item(model, "ru") is None
    for language in ("en", "all"):
        assert module("catalog").item(model, language)["language"] == "en"
    del model["languages"]
    for language in ("ru", "en"):
        assert module("catalog").item(model, language) is None
    assert module("catalog").item(model, "all")["language"] is None


def test_search_all_omits_language_param(catalog, monkeypatch):
    seen = []
    model = _fish_model("b" * 32)
    model["languages"] = ["en"]
    monkeypatch.setattr(module("catalog"), "fish_get",
                        lambda path, params: seen.append(dict(params)) or {"items": [model]})
    for language in ("all", "en"):
        catalog.update({"catalog_language": language})
        assert catalog.search()["items"][0]["language"] == "en"
    assert "language" not in seen[0]
    assert seen[1]["language"] == "en"


def test_add_uses_catalog_language(catalog, monkeypatch):
    model = _fish_model("b" * 32)
    model["languages"] = ["en"]
    monkeypatch.setattr(module("catalog"), "fish_get", lambda path: model)
    with pytest.raises(ValueError):
        catalog.add(model["_id"])
    catalog.update({"catalog_language": "en"})
    assert catalog.add(model["_id"])["language"] == "en"
    assert next(v for v in catalog.voices() if v["id"] == model["_id"])["language"] == "en"
    assert catalog.voices()[0]["language"] == "ru"
    model = _fish_model("c" * 32)
    del model["languages"]
    catalog.update({"catalog_language": "all"})
    assert catalog.add(model["_id"])["language"] is None


def test_fresh_catalog_is_empty(tmp_path):
    cat = module("catalog").Catalog(tmp_path / "desktop.json")
    assert cat.voices() == []
    assert cat.preferences()["voice_id"] is None
    assert cat.preferences()["favorite_ids"] == []


def test_fresh_catalog_device_preferences_are_null(tmp_path):
    catalog_module = module("catalog")
    cat = catalog_module.Catalog(tmp_path / "desktop.json")
    for field in ("input_device", "output_device"):
        assert catalog_module.Catalog.DEFAULT_PREFS[field] is None
        assert cat.preferences()[field] is None
        assert json.loads(cat.path.read_text())[field] is None
        assert catalog_module.Catalog(cat.path).preferences()[field] is None


def test_existing_device_preferences_are_unchanged(tmp_path):
    path = write_library(tmp_path / "desktop.json", [],
                         input_device="MIC", output_device="AI Voice", language="ru",
                         speech_language="ru-RU", catalog_language="ru", onboarding_completed=True)
    before = path.read_bytes()
    cat = module("catalog").Catalog(path)
    assert cat.preferences()["input_device"] == "MIC"
    assert cat.preferences()["output_device"] == "AI Voice"
    assert path.read_bytes() == before


def test_null_device_preferences_snapshot_round_trips(tmp_path):
    path = write_library(tmp_path / "desktop.json",
                         [{"id": TEST_VOICE_ID, "name": "Test"}], TEST_VOICE_ID)
    cat = module("catalog").Catalog(path)
    snapshot = cat.preferences()
    cat.update({"input_device": "MIC", "output_device": "AI Voice", "output_gain_db": -6})
    assert cat.update(snapshot) == snapshot
    assert module("catalog").Catalog(path).preferences() == snapshot


@pytest.mark.parametrize("version", [1, 2])
def test_existing_library_is_kept(tmp_path, version):
    voices = [{"id": TEST_VOICE_ID, "name": "Быков"},
              {"id": "a" * 32, "name": "First"}, {"id": "b" * 32, "name": "Second"}]
    path = write_library(tmp_path / "desktop.json", voices, TEST_VOICE_ID,
                         favorite_ids=[TEST_VOICE_ID], library_version=version,
                         language="ru", speech_language="ru-RU", catalog_language="ru",
                         onboarding_completed=True)
    before = path.read_bytes()
    cat = module("catalog").Catalog(path)
    assert cat.data["voices"] == voices
    assert [v["id"] for v in cat.voices()] == [v["id"] for v in voices]
    assert cat.preferences()["voice_id"] == TEST_VOICE_ID
    assert cat.preferences()["favorite_ids"] == [TEST_VOICE_ID]
    if version == 2:
        assert path.read_bytes() == before


def test_config_voice_rules(monkeypatch):
    cfg = module("config")
    assert cfg.Config().voice is None
    assert cfg.Config(voice=TEST_VOICE_ID).reference_id == TEST_VOICE_ID
    with pytest.raises(ValueError):
        cfg.Config(voice="nope")
    with pytest.raises(ValueError, match="^Choose a voice first$"):
        cfg.Config().reference_id
    slug = "fish_" + TEST_VOICE_ID
    monkeypatch.setitem(cfg.VOICES, slug, TEST_VOICE_ID)
    assert cfg.Config(voice=slug).reference_id == TEST_VOICE_ID


def module(name):
    return importlib.import_module(f"ai_voice.{name}")


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(module("secrets"), "load_key", lambda: "test-key")
    Cat = module("catalog").Catalog
    cat = Cat(path=write_library(tmp_path / "state" / "desktop.json",
        [{"id": TEST_VOICE_ID, "name": "Быков"}, {"id": "a" * 32, "name": "Other"}], TEST_VOICE_ID))
    return cat


def _fish_model(_id, *, tags=None, task_count=10, cover_image=None, title="Голос"):
    return {"_id": _id, "title": title, "languages": ["ru"], "state": "trained",
            "visibility": "public", "tags": tags or [], "task_count": task_count,
            "cover_image": cover_image or f"coverimage/{_id}"}


def test_search_rejects_invalid_gender_sort_and_page(catalog):
    with pytest.raises(ValueError):
        catalog.search(gender="femaleX")
    with pytest.raises(ValueError):
        catalog.search(sort_by="newest")
    with pytest.raises(ValueError):
        catalog.search(page=True)
    with pytest.raises(ValueError):
        catalog.search(page=0)
    with pytest.raises(ValueError):
        catalog.search(page=11)
    with pytest.raises(ValueError):
        catalog.search(query="x" * 121)


def test_search_builds_correct_query_params_for_female_popular(catalog, monkeypatch):
    seen = {}

    def fake_fish_get(path, params=None):
        seen["path"] = path
        seen["params"] = dict(params or {})
        return {"items": [_fish_model("a" * 32, tags=["female"], task_count=42)],
                "has_more": False}

    monkeypatch.setattr(module("catalog"), "fish_get", fake_fish_get)
    result = catalog.search("Быков", page=2, gender="female", sort_by="task_count")
    assert seen["path"] == "/model"
    assert seen["params"]["language"] == "ru"
    assert seen["params"]["page_size"] == 12
    assert seen["params"]["page_number"] == 2
    assert seen["params"]["title"] == "Быков"
    assert seen["params"]["tag"] == "female"
    assert seen["params"]["sort_by"] == "task_count"
    assert result["page"] == 2
    assert result["gender"] == "female"
    assert result["sort_by"] == "task_count"
    assert result["query"] == "Быков"
    assert result["items"][0]["tags"] == ["female"]


def test_search_skips_title_when_query_empty(catalog, monkeypatch):
    seen = {}

    def fake_fish_get(path, params=None):
        seen["params"] = dict(params or {})
        return {"items": [], "has_more": False}

    monkeypatch.setattr(module("catalog"), "fish_get", fake_fish_get)
    catalog.search(gender="all", sort_by="task_count")
    assert "title" not in seen["params"]
    assert "tag" not in seen["params"]


def test_search_filters_client_side_when_gender_requested(catalog, monkeypatch):
    def fake_fish_get(path, params=None):
        return {"items": [
            _fish_model("a" * 32, tags=["female"], task_count=10),
            _fish_model("b" * 32, tags=["male"], task_count=20),
        ], "has_more": False}
    monkeypatch.setattr(module("catalog"), "fish_get", fake_fish_get)
    result = catalog.search(gender="female")
    assert len(result["items"]) == 1
    assert result["items"][0]["tags"] == ["female"]


def test_search_handles_empty_results_and_429(catalog, monkeypatch):
    def empty(path, params=None):
        return {"items": [], "has_more": False}
    monkeypatch.setattr(module("catalog"), "fish_get", empty)
    result = catalog.search(gender="all")
    assert result["items"] == []
    assert result["has_more"] is False

    def fail(path, params=None):
        raise ValueError("Слишком много запросов. Попробуйте позже.")
    monkeypatch.setattr(module("catalog"), "fish_get", fail)
    with pytest.raises(ValueError, match="Слишком"):
        catalog.search(gender="all")


def test_search_respects_has_more_cap(catalog, monkeypatch):
    def fake(path, params=None):
        return {"items": [_fish_model("a" * 32)], "has_more": True}
    monkeypatch.setattr(module("catalog"), "fish_get", fake)
    result = catalog.search(page=10)
    assert result["has_more"] is False  # capped at 10


def test_safe_avatar_accepts_only_official_cdn_path():
    safe = module("catalog").safe_avatar
    base = "https://public-platform.r2.fish.audio/cdn-cgi/image/width=96,format=webp/coverimage/db89e349112e44dca6820e0cb2d414cc"
    assert safe("coverimage/db89e349112e44dca6820e0cb2d414cc") == base
    assert safe("/coverimage/db89e349112e44dca6820e0cb2d414cc") == base
    assert safe("coverimage/db89e349112e44dca6820e0cb2d414cc/extra") is None
    for bad in ["http://evil.com/x", "https://public-platform.r2.fish.audio.evil/x",
                "//evil/x", "coverimage/../etc/passwd", "coverimage/", "", None,
                "coverimage/with spaces", "javascript:alert(1)"]:
        assert safe(bad) is None


def test_normalize_tags_and_task_count():
    cat = module("catalog")
    assert cat.normalize_tags(["  Female ", "глубокий", "x" * 60, "", 42, "a-b_c"]) == [
        "female", "глубокий", "a-b_c"]
    assert cat.normalize_tags(None) == []
    assert cat.normalize_task_count(5) == 5
    assert cat.normalize_task_count(0) == 0
    assert cat.normalize_task_count(True) is None
    assert cat.normalize_task_count(-1) is None
    assert cat.normalize_task_count("10") is None


def test_normalized_item_includes_avatar_tags_and_count():
    item = module("catalog").item
    model = _fish_model("c" * 32, tags=["Female", "Шёпот"], task_count=99,
                        cover_image="coverimage/" + "c" * 4)
    result = item(model)
    assert result["avatar_url"] == ("https://public-platform.r2.fish.audio/cdn-cgi/image/width=96,format=webp/coverimage/"
                                    + "c" * 4)
    assert result["tags"] == ["female", "шёпот"]
    assert result["task_count"] == 99


def test_stored_voice_keeps_name_and_gets_avatar(tmp_path, monkeypatch):
    monkeypatch.setattr(module("secrets"), "load_key", lambda: "test-key")
    cat = module("catalog").Catalog(path=write_library(tmp_path / "state" / "desktop.json",
        [{"id": TEST_VOICE_ID, "name": "Быков"}], TEST_VOICE_ID))
    seen = []
    def fake(path, params=None):
        seen.append(path)
        return {"_id": "db89e349112e44dca6820e0cb2d414cc", "cover_image": "coverimage/db89e349112e44dca6820e0cb2d414cc",
                "tags": ["male"], "task_count": 1234}
    monkeypatch.setattr(module("catalog"), "fish_get", fake)
    meta = cat.fetch_metadata(["db89e349112e44dca6820e0cb2d414cc"])
    assert meta["db89e349112e44dca6820e0cb2d414cc"]["avatar_url"].endswith(
        "/coverimage/db89e349112e44dca6820e0cb2d414cc")
    assert meta["db89e349112e44dca6820e0cb2d414cc"]["task_count"] == 1234
    voices = cat.voices()
    bykov = next(v for v in voices if v["id"] == "db89e349112e44dca6820e0cb2d414cc")
    assert bykov["name"] == "Быков"
    assert bykov["avatar_url"] == meta[bykov["id"]]["avatar_url"]
    assert cat.fetch_metadata([bykov["id"]]) == meta
    assert len(seen) == 1
    reloaded = module("catalog").Catalog(path=cat.path)
    assert next(v for v in reloaded.voices() if v["id"] == bykov["id"])["avatar_url"] == bykov["avatar_url"]


def test_metadata_fetch_swallows_individual_errors(catalog, monkeypatch):
    calls = []

    def fake(path, params=None):
        calls.append(path)
        if "db89e349" in path:
            raise ValueError("429 от Fish")
        return {"_id": path.split("/")[-1], "cover_image": None, "tags": [], "task_count": 1}

    monkeypatch.setattr(module("catalog"), "fish_get", fake)
    meta = catalog.fetch_metadata(["db89e349112e44dca6820e0cb2d414cc", "a" * 32])
    assert meta == {"a" * 32: {"avatar_url": None, "tags": [], "task_count": 1}}


def test_metadata_fetch_uses_four_workers(catalog, monkeypatch):
    barrier = threading.Barrier(4)
    ids = [str(i) * 32 for i in range(1, 5)]
    def fake(path, params=None):
        barrier.wait(timeout=2)
        return _fish_model(path.split("/")[-1])
    monkeypatch.setattr(module("catalog"), "fish_get", fake)
    assert set(catalog.fetch_metadata(ids)) == set(ids)


def test_metadata_bootstrap_is_nonblocking_and_includes_bykov(catalog, monkeypatch):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    captured = []
    def fake(ids, **kwargs):
        captured.extend(ids)
        entered.set()
        release.wait(timeout=2)
        done.set()
    monkeypatch.setattr(catalog, "fetch_metadata", fake)
    catalog.start_metadata()
    try:
        assert entered.wait(timeout=1)
        assert catalog.metadata_pending is True
        assert "db89e349112e44dca6820e0cb2d414cc" in captured
    finally:
        release.set()
        assert done.wait(timeout=1)


def test_invalid_persisted_normalization_uses_default(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"normalize_loudness": 0, "output_gain_db": -6}))
    cat = module("catalog").Catalog(path=path)
    assert cat.preferences()["normalize_loudness"] is True
    assert cat.preferences()["output_gain_db"] == -6


def test_output_gain_db_validates_and_persists(catalog):
    catalog.update({"output_gain_db": 6.0})
    assert catalog.preferences()["output_gain_db"] == 6.0
    reloaded = module("catalog").Catalog(path=catalog.path)
    assert reloaded.preferences()["output_gain_db"] == 6.0
    for bad in [True, "5", float("inf"), float("nan"), 25, -25]:
        with pytest.raises(ValueError):
            catalog.update({"output_gain_db": bad})


def test_normalize_loudness_validates_and_persists(catalog):
    for bad in ["yes", 1, None, 5]:
        with pytest.raises(ValueError):
            catalog.update({"normalize_loudness": bad})
    catalog.update({"normalize_loudness": False})
    assert catalog.preferences()["normalize_loudness"] is False
    reloaded = module("catalog").Catalog(path=catalog.path)
    assert reloaded.preferences()["normalize_loudness"] is False


def test_old_state_files_get_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(module("secrets"), "load_key", lambda: "test-key")
    state = tmp_path / "state" / "desktop.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"voices": [{"id": TEST_VOICE_ID, "name": "Быков"}], "voice_id": "db89e349112e44dca6820e0cb2d414cc",
                                 "favorite_ids": ["a" * 32],
                                 "input_device": "MIC",
                                 "output_device": "AI Voice"}))
    cat = module("catalog").Catalog(path=state)
    prefs = cat.preferences()
    assert prefs["output_gain_db"] == 0.0
    assert prefs["normalize_loudness"] is True
    assert cat.preferences()["voice_id"] == "db89e349112e44dca6820e0cb2d414cc"
    assert cat.preferences()["favorite_ids"] == []  # migration removes favorites outside the library


def test_invalid_preference_does_not_mutate_other_fields(catalog):
    before = catalog.preferences()
    with pytest.raises(ValueError):
        catalog.update({"output_gain_db": True, "favorite_ids": ["a" * 32]})
    after = catalog.preferences()
    assert before == after
    assert before["voice_id"] == after["voice_id"]
    assert before["input_device"] == after["input_device"]
    assert before["output_device"] == after["output_device"]
    assert before["output_gain_db"] == after["output_gain_db"]
    assert before["normalize_loudness"] == after["normalize_loudness"]


def test_unknown_preference_field_is_rejected(catalog):
    with pytest.raises(ValueError):
        catalog.update({"unknown_field": 1})


def test_config_dataclass_rejects_invalid_gain_and_normalize():
    cfg = module("config").Config
    with pytest.raises(ValueError):
        cfg(output_gain_db=True)
    with pytest.raises(ValueError):
        cfg(output_gain_db=float("inf"))
    with pytest.raises(ValueError):
        cfg(output_gain_db=-25)
    with pytest.raises(ValueError):
        cfg(normalize_loudness="yes")


def test_config_linear_gain_matches_expected_curve():
    cfg = module("config").Config
    assert cfg(output_gain_db=0).linear_gain() == pytest.approx(1.0)
    assert cfg(output_gain_db=-6).linear_gain() == pytest.approx(10 ** (-6 / 20))
    assert cfg(output_gain_db=12).linear_gain() == pytest.approx(10 ** (12 / 20))


def test_one_invalid_persisted_field_does_not_reset_later_fields(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"output_gain_db": 99, "monitor_gain_db": -6, "input_gain_db": 3}))
    prefs = module("catalog").Catalog(path=path).preferences()
    assert prefs["output_gain_db"] == 0.0
    assert prefs["monitor_gain_db"] == -6
    assert prefs["input_gain_db"] == 3


def test_remove_stored_voice_readd_and_last_voice(catalog, monkeypatch):
    ids = [v['id'] for v in catalog.voices()]
    current = catalog.preferences()['voice_id']
    assert catalog.slug(current) == 'fish_' + current
    prefs = catalog.remove(current)
    assert prefs['voice_id'] == next(v for v in ids if v != current)
    assert current not in [v['id'] for v in catalog.voices()]
    assert current not in [v['id'] for v in module('catalog').Catalog(path=catalog.path).voices()]
    with pytest.raises(ValueError, match='Сначала добавьте'):
        catalog.slug(current)
    calls = []
    monkeypatch.setattr(module('catalog'), 'fish_get', lambda path: calls.append(path) or _fish_model(current))
    assert catalog.add(current)['id'] == current
    assert calls == ['/model/' + current]
    catalog.add(current)
    assert len(calls) == 1
    for voice_id in ids[:-1]:
        catalog.remove(voice_id)
    with pytest.raises(ValueError, match='Нельзя удалить последний голос'):
        catalog.remove(ids[-1])
    assert len(catalog.voices()) == 1


def test_library_remove_save_failure_rolls_back(catalog, monkeypatch):
    voice_id = catalog.preferences()['voice_id']
    original = catalog.preferences()
    monkeypatch.setattr(catalog, 'save', lambda: (_ for _ in ()).throw(OSError('full disk')))
    with pytest.raises(OSError):
        catalog.remove(voice_id)
    assert catalog.preferences() == original


def test_hidden_builtin_pref_is_rejected(catalog):
    with pytest.raises(ValueError, match='Неизвестные настройки: hidden_builtin_ids'):
        catalog.update({'hidden_builtin_ids': []})


def test_library_migration_preserves_users_and_order(tmp_path):
    catmod = module('catalog')
    hidden = ['c' * 32, 'd' * 32]
    bykov = TEST_VOICE_ID
    user = [{'id': 'a' * 32, 'name': 'Первый', 'avatar_url': 'https://example.test/avatar', 'tags': ['male'], 'task_count': 9},
            {'id': 'b' * 32, 'name': 'Второй'}]
    path = tmp_path / 'desktop.json'
    path.write_text(json.dumps({'hidden_builtin_ids': hidden, 'voices': user,
                               'favorite_ids': [bykov, hidden[0], user[0]['id']], 'voice_id': user[0]['id']}))
    cat = catmod.Catalog(path=path)
    expected = [v['id'] for v in user]
    assert [v['id'] for v in cat.voices()] == expected
    assert cat.data['voices'][-2:] == user
    assert cat.voices()[-2]['avatar_url'] == user[0]['avatar_url']
    assert all('removable' not in v for v in cat.voices())
    assert cat.preferences()['favorite_ids'] == [user[0]['id']]
    assert cat.preferences()['voice_id'] == user[0]['id']
    disk = json.loads(path.read_text())
    assert disk['library_version'] == 2
    assert 'hidden_builtin_ids' not in disk
    assert catmod.Catalog(path=path).voices() == cat.voices()


def test_library_migration_preserves_hidden_user_override(tmp_path):
    catmod = module('catalog')
    bykov = TEST_VOICE_ID
    user = {'id': bykov, 'name': 'Моё имя', 'avatar_url': 'https://example.test/avatar', 'tags': ['male']}
    path = tmp_path / 'desktop.json'
    path.write_text(json.dumps({'voices': [user], 'hidden_builtin_ids': [bykov]}))
    cat = catmod.Catalog(path=path)
    assert next(v for v in cat.data['voices'] if v['id'] == bykov) == user


def test_v2_library_is_not_reseeded(tmp_path):
    path = tmp_path / 'desktop.json'
    voice = {'id': 'a' * 32, 'name': 'Один'}
    path.write_text(json.dumps({'library_version': 2, 'voices': [voice]}))
    cat = module('catalog').Catalog(path=path)
    assert cat.data['voices'] == [voice]
    assert cat.preferences()['voice_id'] == voice['id']


def test_migration_save_failure_is_retryable(tmp_path, monkeypatch):
    catmod = module('catalog')
    path = tmp_path / 'desktop.json'
    path.write_text(json.dumps({'voices': [{'id': 'a' * 32, 'name': 'User'}]}))
    old = path.read_text()
    with monkeypatch.context() as patch:
        patch.setattr(catmod.Catalog, 'save', lambda self: (_ for _ in ()).throw(OSError('full disk')))
        first = catmod.Catalog(path=path)
    assert path.read_text() == old
    assert catmod.Catalog(path=path).voices() == first.voices()
