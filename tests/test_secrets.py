import importlib
import shlex
import subprocess
import sys

import pytest

from ai_voice import secrets


RUN_OPTIONS = {"capture_output": True, "text": True, "check": False, "timeout": 15}
NEW_LOOKUP = ["security", "find-generic-password", "-a", "default",
              "-s", "ai-voice/FISH_API_KEY", "-w"]


@pytest.fixture
def security_run(monkeypatch):
    calls = []
    responses = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert {key: kwargs[key] for key in RUN_OPTIONS} == RUN_OPTIONS
        assert responses, "Unexpected subprocess call"
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return subprocess.CompletedProcess(args, response[0], stdout=response[1], stderr="")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    return calls, responses


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_load_key_uses_new_service(security_run):
    calls, responses = security_run
    responses.append((0, "test-key\n"))
    assert secrets.load_key() == "test-key"
    assert calls == [(NEW_LOOKUP, RUN_OPTIONS)]
    assert secrets.KEYCHAIN_SERVICE_PREFIX == "ai-voice"
    assert secrets.KEYCHAIN_ACCOUNT == "default"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_load_key_supports_named_keys(security_run):
    calls, responses = security_run
    responses.append((0, "other-key\n"))
    assert secrets.load_key("OTHER_API_KEY") == "other-key"
    assert calls[0][0] == ["security", "find-generic-password", "-a", "default",
                           "-s", "ai-voice/OTHER_API_KEY", "-w"]


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("security", 15)])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_load_key_returns_none_on_subprocess_error(security_run, error):
    _, responses = security_run
    responses.append(error)
    assert secrets.load_key() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_save_key_uses_stdin_not_argv(security_run):
    calls, responses = security_run
    value = 'ab"c\\d'
    responses.extend([(0, ""), (0, value + "\n")])
    assert secrets.save_key("FISH_API_KEY", value) is None
    args, kwargs = calls[0]
    assert args == ["security", "-i"]
    assert all(value not in arg for arg in args)
    assert kwargs == {**RUN_OPTIONS, "input":
                      'add-generic-password -U -a default -s "ai-voice/FISH_API_KEY" -w "ab\\"c\\\\d"\n'}
    assert shlex.split(kwargs["input"])[-1] == value
    assert secrets.load_key() == value


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_save_key_supports_named_keys(security_run):
    calls, responses = security_run
    responses.append((0, ""))
    secrets.save_key("OTHER_API_KEY", "other-key")
    assert calls[0][1]["input"] == (
        'add-generic-password -U -a default -s "ai-voice/OTHER_API_KEY" -w "other-key"\n'
    )


@pytest.mark.parametrize("value", ["one\ntwo", "one\rtwo", "one\r\ntwo"])
def test_save_key_rejects_newline(security_run, value):
    calls, _ = security_run
    with pytest.raises(ValueError):
        secrets.save_key("FISH_API_KEY", value)
    assert calls == []


def test_save_key_rejects_empty_value(security_run):
    calls, _ = security_run
    with pytest.raises(ValueError):
        secrets.save_key("FISH_API_KEY", "")
    assert calls == []


@pytest.mark.parametrize("response", [(1, "private-output"), OSError("private-output"),
                                      subprocess.TimeoutExpired("security", 15, output="private-output")])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_save_key_raises_without_exposing_subprocess_output(security_run, response):
    _, responses = security_run
    responses.append(response)
    with pytest.raises(RuntimeError) as caught:
        secrets.save_key("FISH_API_KEY", "test-key")
    assert "private-output" not in str(caught.value)
    assert "test-key" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("status", [0, 44])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_delete_key_success_or_missing_is_not_an_error(security_run, status):
    calls, responses = security_run
    responses.append((status, ""))
    assert secrets.delete_key("OTHER_API_KEY") is None
    assert calls == [(["security", "delete-generic-password", "-a", "default",
                      "-s", "ai-voice/OTHER_API_KEY"], RUN_OPTIONS)]


@pytest.mark.parametrize("response", [(1, "private-output"), OSError("private-output"),
                                      subprocess.TimeoutExpired("security", 15, output="private-output")])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_delete_key_raises_on_other_errors(security_run, response):
    _, responses = security_run
    responses.append(response)
    with pytest.raises(RuntimeError) as caught:
        secrets.delete_key("FISH_API_KEY")
    assert "private-output" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_migrate_copies_when_new_missing(monkeypatch, security_run):
    calls, responses = security_run
    responses.extend([(44, ""), (0, "k1\n")])
    saved = []
    monkeypatch.setattr(secrets, "save_key", lambda name, value: saved.append((name, value)), raising=False)
    assert secrets.migrate_legacy() is True
    assert saved == [("FISH_API_KEY", "k1")]
    assert calls[0] == (NEW_LOOKUP, RUN_OPTIONS)
    # Keep the legacy service literal exclusively in the migration implementation.
    assert calls[1] == (["security", "find-generic-password", "-s",
                        "ai-" + "discord-voice/FISH_API_KEY", "-w"], RUN_OPTIONS)
    assert all("delete-generic-password" not in args for args, _ in calls)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_migrate_noop_when_new_present(security_run):
    calls, responses = security_run
    responses.append((0, "new-key\n"))
    assert secrets.migrate_legacy() is False
    assert calls == [(NEW_LOOKUP, RUN_OPTIONS)]


@pytest.mark.parametrize("legacy_response", [(44, ""), (1, "private-output"), (0, "")])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_migrate_noop_when_legacy_missing(security_run, legacy_response):
    calls, responses = security_run
    responses.extend([(44, ""), legacy_response])
    assert secrets.migrate_legacy() is False
    assert len(calls) == 2
    assert all("delete-generic-password" not in args for args, _ in calls)


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("security", 15)])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_migrate_returns_false_on_lookup_error(security_run, error):
    _, responses = security_run
    responses.extend([error, error])
    assert secrets.migrate_legacy() is False


@pytest.mark.parametrize("response", [(1, "private-output"), OSError("unavailable"),
                                      subprocess.TimeoutExpired("security", 15)])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_migrate_returns_false_when_copy_fails(security_run, response):
    calls, responses = security_run
    responses.extend([(44, ""), (0, "k1\n"), response])
    assert secrets.migrate_legacy() is False
    assert len(calls) == 3
    assert calls[2][0] == ["security", "-i"]
    assert all("delete-generic-password" not in args for args, _ in calls)


@pytest.mark.parametrize("entrypoint", ["desktop", "cli"])
def test_main_migrates_once_before_startup(monkeypatch, security_run, entrypoint):
    module = importlib.import_module(f"ai_voice.{entrypoint}")
    migrated = []
    # Catalog() (cli.main reads the language) may ask Keychain for the setup-flag default; not under test here.
    monkeypatch.setattr("ai_voice.catalog.has_key", lambda name="FISH_API_KEY": False)
    monkeypatch.setattr(module, "migrate_legacy", lambda: migrated.append(True), raising=False)

    def stop_before_startup(*args, **kwargs):
        assert migrated == [True]
        raise SystemExit(0)

    monkeypatch.setattr(module.argparse.ArgumentParser, "parse_args", stop_before_startup)
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 0
    assert migrated == [True]
    assert security_run[0] == []


@pytest.mark.parametrize("response,expected", [((0, ""), True), ((44, ""), False), (OSError("x"), False)])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_has_key(security_run, response, expected):
    calls, responses = security_run
    responses.append(response)
    assert secrets.has_key("HF_TOKEN") is expected
    assert calls[0][0] == ["security", "find-generic-password", "-a", "default", "-s", "ai-voice/HF_TOKEN"]


def test_conftest_blocks_keychain():
    assert secrets.load_key() is None
    assert secrets.has_key() is False
