"""Exercise bridge protocol and lifecycle with real controlled helper processes."""
import json
import sys

import pytest

from ai_voice.asr import ASRError, SpeechASR
from ai_voice import asr


def locales_helper(tmp_path, body):
    script = tmp_path / "locales-helper"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o700)
    return script


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_supported_locales_parses_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "_LOCALES", None, raising=False)
    report = {"event": "locales", "system": "en-GB", "supported": ["en-GB", "ru-RU"]}
    script = locales_helper(tmp_path, f"printf '%s\\n' garbage '{json.dumps(report)}'")
    assert await asr.supported_locales(helper_path=script) == report
    script.write_text("#!/bin/sh\nexit 1\n")
    assert await asr.supported_locales(helper_path=script) == report
    assert asr.locales_snapshot() == report


@pytest.mark.parametrize("body", [None, "exit 3", "echo garbage", "sleep 10",
    "echo '{\"event\":\"locales\",\"system\":\"en-US\",\"supported\":[]}'; exit 3",
    "echo '{\"event\":\"locales\",\"system\":3,\"supported\":[]}'",
    "echo '{\"event\":\"locales\",\"system\":\"en-US\",\"supported\":[3]}'"])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_supported_locales_fallbacks(tmp_path, monkeypatch, body):
    import time
    monkeypatch.setattr(asr, "_LOCALES", None, raising=False)
    path = tmp_path / "missing" if body is None else locales_helper(tmp_path, body)
    started = time.monotonic()
    assert await asr.supported_locales(helper_path=path, timeout=0.2) == asr.LOCALES_FALLBACK
    assert time.monotonic() - started < 2
    assert asr.locales_snapshot() == asr.LOCALES_FALLBACK


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_supported_locales_uses_last_valid_event(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "_LOCALES", None, raising=False)
    first = {"event": "locales", "system": "en-US", "supported": ["en-US"]}
    last = {"event": "locales", "system": "fr-FR", "supported": ["fr-FR"]}
    script = locales_helper(tmp_path,
        f"printf '%s\\n' '{json.dumps(first)}' '[]' '{json.dumps(last)}' garbage")
    assert await asr.supported_locales(helper_path=script) == last


def test_locales_snapshot_before_discovery(monkeypatch):
    monkeypatch.setattr(asr, "_LOCALES", None)
    assert asr.locales_snapshot() == asr.LOCALES_FALLBACK
    assert asr._LOCALES is None


def helper(tmp_path, lines, exit_code=0, stderr=""):
    script = tmp_path / "helper.py"
    script.write_text("import sys\n" + f"sys.stderr.write({stderr!r})\n" +
                      "\n".join(f"print({line!r}, flush=True)" for line in lines) +
                      f"\nsys.exit({exit_code})\n")
    return SpeechASR(command=[sys.executable, str(script)])


def event(kind, **extra):
    return json.dumps({"event": kind, "utterance_id": "u1", "time_ms": 12.0, **extra})


@pytest.mark.parametrize("line", ["not json", "[]", event("unknown"),
    event("final", text=42), event([], text="bad"),
    event("partial", text="ok", time_ms=10 ** 400), event("final", text="ok", utterance_id=4),
    event("partial", text="ok", time_ms=-1),
    event("partial", text="ok", time_ms=float("nan"))])
def test_rejects_invalid_wire_event(line):
    with pytest.raises(ASRError):
        SpeechASR._parse(line.encode('utf-8'))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_duplicate_final_is_yielded_once(tmp_path):
    final = event("final", text="Привет")
    events = [e async for e in helper(tmp_path, [event("partial", text="При"), final, final]).events()]
    assert [e["event"] for e in events] == ["partial", "final"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_distinct_ids_preserve_repeated_words(tmp_path):
    events = [e async for e in helper(tmp_path, [event("final", text="Да"),
        event("final", text="Да", utterance_id="u2")]).events()]
    assert [e["text"] for e in events] == ["Да", "Да"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_nonzero_exit_is_readable(tmp_path):
    with pytest.raises(ASRError, match="exited.*7.*device removed"):
        _ = [e async for e in helper(tmp_path, [], 7, "device removed").events()]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_authorization_error_is_fatal_and_readable(tmp_path):
    with pytest.raises(ASRError, match="speech_permission.*denied"):
        _ = [e async for e in helper(tmp_path, [event("error", code="speech_permission", message="denied", fatal=True)]).events()]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_recoverable_error_is_yielded(tmp_path):
    events = [e async for e in helper(tmp_path, [event("error", code="recognition", message="retry", fatal=False)]).events()]
    assert events[0]["fatal"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_doctor_parses_json_without_starting_capture(tmp_path):
    script = tmp_path / "doctor.py"
    script.write_text('import sys, json\nprint(json.dumps({"event":"doctor", "speech_authorization":"notDetermined", "args":sys.argv[1:]}))\n')
    report = await SpeechASR(command=[sys.executable, str(script)]).doctor()
    assert report["speech_authorization"] == "notDetermined"
    assert report["args"] == ["--doctor", "--input", "MIC", "--language", "ru-RU"]

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_app_socket_events_and_cleanup(tmp_path):
    script = tmp_path / "socket_helper.py"
    done = tmp_path / "closed"
    script.write_text('''import sys, socket, json, os
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[sys.argv.index("--event-socket") + 1])
s.sendall((json.dumps({"event":"hello", "pid":os.getpid()}) + "\\n").encode())
s.sendall((json.dumps({"event":"final", "utterance_id":"s1", "time_ms":1, "text":"Привет"}) + "\\n").encode())
s.recv(1)
from pathlib import Path
Path(sys.argv[1]).write_text("closed")
''')
    asr = SpeechASR(command=[sys.executable, str(script), str(done)], launch_app=True)
    stream = asr.events()
    assert (await anext(stream))["text"] == "Привет"
    await stream.aclose()
    assert done.read_text() == "closed"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_app_socket_doctor(tmp_path):
    script = tmp_path / "socket_doctor.py"
    script.write_text('''import sys, socket, json, os
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[sys.argv.index("--event-socket") + 1])
s.sendall((json.dumps({"event":"hello", "pid":os.getpid()}) + "\\n").encode())
s.sendall((json.dumps({"event":"doctor", "capture_started":False}) + "\\n").encode())
s.close()
''')
    report = await SpeechASR(command=[sys.executable, str(script)], launch_app=True).doctor()
    assert report["capture_started"] is False

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_launchservices_zero_exit_still_reports_lost_helper(tmp_path):
    script = tmp_path / "exiting_app.py"
    script.write_text('''import sys, socket, json, os
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[sys.argv.index("--event-socket") + 1])
s.sendall((json.dumps({"event":"hello", "pid":os.getpid()}) + "\\n").encode())
s.sendall((json.dumps({"event":"ready", "utterance_id":"", "time_ms":1}) + "\\n").encode())
s.close()
''')
    with pytest.raises(ASRError, match="stopped"):
        _ = [e async for e in SpeechASR(command=[sys.executable, str(script)], launch_app=True).events()]

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_cancel_pending_app_connection_reaps_launcher(tmp_path):
    import asyncio
    import os
    script = tmp_path / "pending.py"
    pid_file = tmp_path / "pid"
    script.write_text(f'import os, time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(10)\n')
    asr = SpeechASR(command=[sys.executable, str(script)], launch_app=True)
    task = asyncio.create_task(asr.doctor())
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(pid, 9)  # test cleanup even on red
        pytest.fail("cancelled app launcher remains running")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_helper_argv_matches_swift_parser_and_events_flow(tmp_path):
    """Backend must pass exactly the options native/SpeechHelper.swift accepts."""
    script = tmp_path / "argv_helper.py"
    script.write_text(
        "import sys, json\n"
        "known = {'--event-socket', '--input', '--language', '--endpoint-ms', '--threshold-db', '--input-gain-db'}\n"
        "a = sys.argv[1:]\n"
        "keys = a[0::2]\n"
        "bad = [k for k in keys if k not in known]\n"
        "need = {'--input', '--language', '--endpoint-ms', '--threshold-db', '--input-gain-db'}\n"
        "if bad or not need <= set(keys):\n"
        "    print(json.dumps({'event': 'error', 'utterance_id': '', 'time_ms': 1.0, 'code': 'arguments', 'message': 'bad argv', 'fatal': True}), flush=True); sys.exit(1)\n"
        "print(json.dumps({'event': 'ready', 'utterance_id': '', 'time_ms': 1.0}), flush=True)\n"
        "print(json.dumps({'event': 'input_gain', 'utterance_id': '', 'time_ms': 2.0, 'db': 0.0}), flush=True)\n"
        "print(json.dumps({'event': 'final', 'utterance_id': 'u1', 'time_ms': 3.0, 'text': 'privet'}), flush=True)\n")
    asr = SpeechASR(command=[sys.executable, str(script)])
    events = [e async for e in asr.events()]
    assert [e["event"] for e in events] == ["ready", "input_gain", "final"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_authorize_only_args(tmp_path):
    script = tmp_path / "authorize.py"
    script.write_text('import sys, json\nprint(json.dumps({"event":"doctor", "args":sys.argv[1:]}))\n')
    report = await SpeechASR(command=[sys.executable, str(script)], launch_app=False).authorize_only("microphone")
    assert report["args"][:3] == ["--authorize", "--only", "microphone"]
