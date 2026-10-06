"""Shared dictionary contracts and the incremental source guard."""
import importlib.util
import json
from pathlib import Path
import re
import warnings

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "src/ai_voice/locales"
CYRILLIC_FREE: list[str] = sorted(
    ["macos/desktop/app.js", "macos/desktop/onboarding.js", "macos/desktop/index.html", "macos/Desktop.swift", "scripts/i18n_extract.py", "scripts/build-driver.sh", "scripts/install-vc-engine.sh"]
    + [path.relative_to(ROOT).as_posix() for path in (ROOT / "src/ai_voice").rglob("*.py")]
)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
PLACEHOLDERS = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
KEY_NAME = re.compile(
    r"^(common|main|catalog|vc|settings|status|errors|menu|onboarding|update|engine|cli|voice)"
    r"\.[a-z0-9_]+(\.[a-z0-9_]+)*$"
)


@pytest.fixture
def dictionaries():
    return {lang: json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
            for lang in ("en", "ru")}


def test_key_parity(dictionaries):
    assert set(dictionaries["en"]) == set(dictionaries["ru"])
    assert {"common.ok", "status.fish_key_missing"} <= dictionaries["en"].keys()


def test_no_empty_values(dictionaries):
    for dictionary in dictionaries.values():
        for key, value in dictionary.items():
            assert isinstance(value, str) and value and value == value.strip(), key


def test_placeholders_match(dictionaries):
    for key, value in dictionaries["en"].items():
        assert set(PLACEHOLDERS.findall(value)) == set(PLACEHOLDERS.findall(dictionaries["ru"][key])), key


def test_key_names(dictionaries):
    for dictionary in dictionaries.values():
        assert all(KEY_NAME.fullmatch(key) for key in dictionary)


def test_files_formatted(dictionaries):
    for lang, dictionary in dictionaries.items():
        assert (LOCALES / f"{lang}.json").read_text(encoding="utf-8") == (
            json.dumps(dictionary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )


def test_plural_keys_complete(dictionaries):
    for dictionary in dictionaries.values():
        for key in dictionary:
            match = re.fullmatch(r"(.+)_(one|few|many|other)", key)
            if match:
                assert {match[1] + "_" + category for category in ("one", "few", "many", "other")} <= dictionary.keys()


def test_used_keys_exist(dictionaries):
    paths = [ROOT / name for name in ("macos/desktop/app.js", "macos/desktop/onboarding.js", "macos/desktop/index.html", "macos/Desktop.swift")]
    paths.extend(sorted((ROOT / "src/ai_voice").glob("*.py")))
    for path in paths:
        source = path.read_text(encoding="utf-8")
        keys = re.findall(r"\bt\(\s*[\"']([a-z0-9_.]+)[\"']", source)
        keys += re.findall(r'data-i18n(?:-title|-placeholder|-aria)?="([a-z0-9_.]+)"', source)
        for key in keys:
            assert key in dictionaries["en"], (path, key)
        for key in re.findall(r"\btp\(\s*[\"']([a-z0-9_.]+)[\"']", source):
            assert key + "_one" in dictionaries["en"], (path, key)
            assert key + "_other" in dictionaries["en"], (path, key)


@pytest.mark.parametrize("path", CYRILLIC_FREE)
def test_no_cyrillic(path):
    for number, line in enumerate((ROOT / path).read_text(encoding="utf-8").splitlines(), 1):
        assert not CYRILLIC.search(line), (path, number)


def test_t_fallbacks(monkeypatch):
    from ai_voice import i18n
    monkeypatch.setattr(i18n, "load", lambda lang: {"a.x": "EN {n}"} if lang == "en" else {})
    i18n.set_language("ru")
    assert i18n.t("a.x", n=2) == "EN 2"
    assert i18n.t("a.missing") == "a.missing"
    assert i18n.t("a.x") == "EN {n}"
    i18n.set_language("de")
    assert i18n.get_language() == "en"


def test_t_current_language_and_literal_replacement(monkeypatch):
    from ai_voice import i18n
    monkeypatch.setattr(i18n, "load", lambda lang: {"common.x": "RU {name} {other}"} if lang == "ru" else {})
    i18n.set_language("ru")
    assert i18n.t("common.x", name=r"\1 {other}") == r"RU \1 {other} {other}"


def test_load_supported_and_unknown_languages():
    from ai_voice import i18n
    assert i18n.LANGUAGES == ("en", "ru")
    assert i18n.LOCALES_DIR == LOCALES
    for lang in i18n.LANGUAGES:
        assert i18n.load(lang) == json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        assert i18n.load(lang) is i18n.load(lang)
    assert i18n.load("../desktop") == {}


@pytest.fixture
def extractor():
    spec = importlib.util.spec_from_file_location("i18n_extract", ROOT / "scripts/i18n_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("suffix,source,expected", [
    (".py", 'value = "Привет"\n# Комментарий\nvalue = f"Голос {name}"\n',
     {"1": "Привет", "2#comment": "# Комментарий", "3": "Голос {name}"}),
    (".js", 'const a = "Привет // строка";\n// Комментарий\nconst b = `Голос ${name}`;\n',
     {"1": "Привет // строка", "2#comment": "// Комментарий", "3": "Голос ${name}"}),
    (".swift", 'let a = "Привет"\n/* Комментарий */\n',
     {"1": "Привет", "2#comment": "/* Комментарий */"}),
    (".html", '<button title="Подсказка">Привет</button>\n<!-- Комментарий -->\n',
     {"1": "Подсказка\nПривет", "2#comment": "<!-- Комментарий -->"}),
])
def test_extractor_scan(extractor, tmp_path, suffix, source, expected):
    path = tmp_path / ("source" + suffix)
    path.write_text(source, encoding="utf-8")
    assert extractor.scan([path]) == {f"{path}:{line}": value for line, value in expected.items()}


def test_extractor_format_is_idempotent(extractor, tmp_path, monkeypatch):
    monkeypatch.setattr(extractor, "LOCALES_DIR", tmp_path)
    for lang in ("en", "ru"):
        (tmp_path / f"{lang}.json").write_text('{"common.z": "Я", "common.a": "A"}', encoding="utf-8")
    extractor.fmt()
    before = [(tmp_path / f"{lang}.json").read_bytes() for lang in ("en", "ru")]
    extractor.fmt()
    assert before == [(tmp_path / f"{lang}.json").read_bytes() for lang in ("en", "ru")]
    assert before[0].decode() == '{\n  "common.a": "A",\n  "common.z": "Я"\n}\n'


def test_extractor_swift_interpolation_has_no_python_warnings(extractor, tmp_path):
    path = tmp_path / "source.swift"
    path.write_text('let message = "Ошибка: \\(reason)"\n', encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert extractor.scan([path]) == {f"{path}:1": r"Ошибка: \(reason)"}
    assert not caught


@pytest.mark.parametrize('language, expected', [('en', 'List audio devices'), ('ru', 'Список звуковых устройств')])
def test_cli_help_uses_desktop_language(monkeypatch, capsys, language, expected):
    from ai_voice import cli, i18n

    class Preferences:
        def preferences(self):
            return {'language': language}

    monkeypatch.setattr(cli, 'Catalog', Preferences)
    monkeypatch.setattr(cli, 'migrate_legacy', lambda: None)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(['--help'])
    assert exit_info.value.code == 0
    assert i18n.get_language() == language
    assert expected in capsys.readouterr().out


def test_training_stage_translated_when_event_arrives(tmp_path):
    from ai_voice import i18n
    from ai_voice.vc_store import VcStore
    from ai_voice.vc_train import VcTrainer

    trainer = VcTrainer(VcStore(tmp_path / 'voices'))
    job = {'status': {'state': 'running', 'progress': 0}, 'index_skipped': False, 'times': []}
    try:
        for language, expected in [('en', 'Pitch analysis'), ('ru', 'Анализ высоты')]:
            i18n.set_language(language)
            trainer._event(job, {'stage': 'pitch', 'progress': .5}, timing=(0, 0))
            assert job['status']['stage_label'] == expected
    finally:
        trainer.close()


@pytest.mark.parametrize('language', ['en', 'ru'])
def test_training_queue_conflict_uses_current_language(language):
    from ai_voice import i18n
    from ai_voice.desktop_control import ConflictError
    from ai_voice.vc_control import VcController
    from ai_voice.vc_train import VcTrainError

    class QueuedTrainer:
        def enqueue(self, voice_id, preset):
            raise VcTrainError(i18n.t('errors.training_queued'))

    controller = VcController.__new__(VcController)
    controller.trainer = QueuedTrainer()
    i18n.set_language(language)
    with pytest.raises(ConflictError) as error:
        controller.train('voice', 'fast')
    assert str(error.value) == i18n.t('errors.training_queued')


@pytest.mark.parametrize('language', ['en', 'ru'])
def test_pipeline_error_uses_current_language(language):
    import asyncio
    from ai_voice import i18n
    from ai_voice.config import Config
    from ai_voice.pipeline import SpeechPipeline

    async def empty_events():
        if False:
            yield {}

    i18n.set_language(language)
    pipeline = SpeechPipeline(Config(voice='a' * 32), '', None)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(pipeline._listen(empty_events()))
    assert str(error.value) == i18n.t('errors.recognition_stopped')
