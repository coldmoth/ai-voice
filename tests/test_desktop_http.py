"""HTTP-level regressions for the localhost desktop bridge (fake control/catalog/preview)."""
import asyncio
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from ai_voice.desktop import DesktopHandler
from helpers import TEST_VOICE_ID, write_library

TOKEN = "t" * 20


@pytest.mark.parametrize("existing,expected", [(False, "en-US"), (True, "ru-RU")])
def test_api_voices_speech_languages(tmp_path, monkeypatch, existing, expected):
    from types import SimpleNamespace
    from ai_voice import asr
    from ai_voice.catalog import Catalog
    monkeypatch.setattr(asr, "_LOCALES",
        {"system": "en-US", "supported": ["en-US", "ru-RU"]}, raising=False)
    monkeypatch.setattr(asr, "supported_locales", lambda: pytest.fail("GET must not await helper"), raising=False)
    monkeypatch.setattr("ai_voice.desktop.list_devices", lambda: [])
    path = tmp_path / "desktop.json"
    if existing:
        write_library(path, [], None)
    catalog = Catalog(path)
    monkeypatch.setattr(catalog, "start_metadata", lambda: None)
    server = SimpleNamespace(catalog=catalog)
    status, _headers, body = _hf_get(server, "/api/voices")
    assert status == 200, body
    result = json.loads(body)
    assert result["speech_languages"] == [{"id": "en-US"}, {"id": "ru-RU"}]
    assert result["speech_language"] == expected


class FakeControl:
    def snapshot(self):
        return {"active": False}

    async def apply_preferences(self, data):
        return dict(data)


class FakeCatalog:
    def __init__(self, prefs=None):
        self.prefs = prefs or {}

    def preferences(self):
        return dict(self.prefs)

    def start_metadata(self):
        raise KeyError("boom")  # unexpected, non-ValueError failure

    def voices(self):
        return []


class FakePreview:
    def __init__(self):
        self.calls = []

    async def start(self, voice_id, device_name=None, **kwargs):
        self.calls.append((voice_id, device_name, kwargs))

    def snapshot(self):
        return {"preview_state": "loading"}


@pytest.fixture
def bridge():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), DesktopHandler)
    server.loop, server.token = loop, TOKEN
    server.control, server.catalog, server.preview = FakeControl(), FakeCatalog(), FakePreview()
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def request(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"X-AI-Voice-Token": TOKEN}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    yield server, request
    server.shutdown()
    server.server_close()
    loop.call_soon_threadsafe(loop.stop)


def test_status_contains_version(bridge):
    _server, request = bridge
    status, body = request("GET", "/api/status")
    assert status == 200, body
    assert json.loads(body)["version"] == __import__("ai_voice").__version__


@pytest.mark.parametrize("language", ["en", "ru"])
def test_api_status_and_voices_return_language(language, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("ai_voice.desktop.list_devices", lambda: [])
    catalog = FakeCatalog({"language": language})
    catalog.start_metadata = lambda: None
    server = SimpleNamespace(control=FakeControl(), catalog=catalog, preview=FakePreview())
    for path in ("/api/status", "/api/voices"):
        status, _headers, body = _hf_get(server, path)
        assert status == 200, body
        assert json.loads(body)["language"] == language


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_locales_route(bridge, lang):
    from ai_voice import i18n
    server, request = bridge
    status, body = request("GET", f"/locales/{lang}.json")
    assert status == 200, body
    assert json.loads(body) == i18n.load(lang)
    for path in ("/locales/../desktop.py", "/locales/de.json", "/locales/en.json/extra"):
        status, body = request("GET", path)
        assert status == 404, body
        assert "error" in json.loads(body)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        conn.request("GET", f"/locales/{lang}.json")
        response = conn.getresponse()
        assert response.status == 403
        assert "error" in json.loads(response.read())
    finally:
        conn.close()


def test_locales_route_headers_and_token_without_socket():
    """Validate real dispatch, auth, and response headers without a listening socket."""
    import io
    from types import SimpleNamespace
    from ai_voice import i18n
    for token, expected in ((TOKEN, 200), ("", 403)):
        handler = object.__new__(DesktopHandler)
        handler.server = SimpleNamespace(server_port=1234, token=TOKEN)
        handler.path = "/locales/en.json"
        handler.headers = {"Host": "127.0.0.1:1234", "X-AI-Voice-Token": token}
        handler.wfile = io.BytesIO()
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda key, value: headers.update({key: value})
        handler.end_headers = lambda: None
        handler.do_GET()
        assert statuses == [expected]
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert "Content-Security-Policy" in headers
        if expected == 200:
            assert headers["Cache-Control"] == "private, max-age=3600"
            assert json.loads(handler.wfile.getvalue()) == i18n.load("en")


def test_large_favorites_list_is_accepted(bridge):
    _server, request = bridge
    ids = [("%032x" % i) for i in range(400)]
    assert len(json.dumps({"favorite_ids": ids})) > 8192
    status, body = request("POST", "/api/preferences", {"favorite_ids": ids})
    assert status == 200, body


def test_preview_start_passes_monitor_gain(bridge):
    server, request = bridge
    server.catalog.prefs = {"monitor_device": "Headphones", "monitor_gain_db": -6.0}
    status, body = request("POST", "/api/preview", {"action": "start", "id": "a" * 32})
    assert status == 200, body
    assert server.preview.calls == [("a" * 32, "Headphones", {"gain_db": -6.0})]


def test_get_unexpected_exception_returns_500(bridge):
    _server, request = bridge
    status, body = request("GET", "/api/voices")
    assert status == 500
    assert "error" in json.loads(body)


def _gate_post(server, path, body):
    """Exercise POST dispatch without opening a sandbox-restricted listening socket."""
    import io
    handler = object.__new__(DesktopHandler)
    payload = json.dumps(body).encode()
    handler.server, handler.path = server, path
    handler.headers = {'Content-Length': str(len(payload)), 'Content-Type': 'application/json'}
    handler.rfile = io.BytesIO(payload)
    handler.allowed = lambda: True
    response = []
    handler.respond = lambda status, data: response.append((status, data))
    handler.do_POST()
    assert len(response) == 1
    return response[0]


def test_vc_meter_post_validation_and_conflict():
    from types import SimpleNamespace
    calls = []
    def heartbeat(on, device):
        calls.append((on, device))
        if on:
            raise RuntimeError('Не удалось открыть микрофон: missing')
        return {'source': 'meter'}
    server = SimpleNamespace(vc=SimpleNamespace(meter_heartbeat=heartbeat),
                             catalog=FakeCatalog({'input_device':'mic'}))
    for value in (1, None, 'true'):
        assert _gate_post(server, '/api/vc/meter', {'on':value})[0] == 400
    assert calls == []
    status, body = _gate_post(server, '/api/vc/meter', {'on':True})
    assert status == 409 and 'Не удалось открыть микрофон' in body['error']
    assert _gate_post(server, '/api/vc/meter', {'on':False}) == (200, {'source':'meter'})
    assert calls == [(True, 'mic'), (False, 'mic')]


@pytest.mark.parametrize('input_device', ['MIC', None])
def test_vc_gate_calibrate_post_saves_and_applies_preferences(tmp_path, monkeypatch, input_device):
    from types import SimpleNamespace
    from ai_voice.catalog import Catalog
    catalog = Catalog(path=str(tmp_path / 'prefs.json'))
    if input_device is not None:
        catalog.update({'input_device': input_device})
    default_inputs = []
    def default_input():
        default_inputs.append(True)
        return 'MacBook Pro Microphone'
    monkeypatch.setattr('ai_voice.desktop.default_input_name', default_input, raising=False)
    calls, devices = [], []
    class Control:
        async def apply_preferences(self, data):
            calls.append(data)
            return catalog.update(data)
    def calibrate(device):
        devices.append(device)
        return {'noise_db':-54, 'gate_db':-48}
    class Future:
        def __init__(self, coro):
            self.value = asyncio.run(coro)
        def result(self, timeout=None):
            return self.value
    monkeypatch.setattr('ai_voice.desktop.asyncio.run_coroutine_threadsafe', lambda coro, loop: Future(coro))
    server = SimpleNamespace(vc=SimpleNamespace(calibrate_gate=calibrate), catalog=catalog,
                             control=Control(), loop=None)
    assert _gate_post(server, '/api/vc/gate/calibrate', {}) == (200, {'noise_db':-54, 'gate_db':-48})
    assert calls == [{'vc_gate_enabled':True, 'vc_gate_db':-48}]
    assert devices == [input_device if input_device is not None else 'MacBook Pro Microphone']
    assert default_inputs == ([] if input_device is not None else [True])
    stored = Catalog(path=str(tmp_path / 'prefs.json')).preferences()
    assert stored['vc_gate_enabled'] is True and stored['vc_gate_db'] == -48
    assert stored['input_device'] == input_device


@pytest.mark.parametrize('message', ['Калибровка уже идёт', 'Нет сигнала микрофона. Проверьте устройство ввода.'])
def test_vc_gate_calibrate_post_runtime_error_is_conflict(message, monkeypatch):
    from types import SimpleNamespace
    def calibrate(device):
        raise RuntimeError(message)
    monkeypatch.setattr('ai_voice.desktop.default_input_name', lambda: 'Test microphone', raising=False)
    server = SimpleNamespace(vc=SimpleNamespace(calibrate_gate=calibrate), catalog=FakeCatalog())
    assert _gate_post(server, '/api/vc/gate/calibrate', {}) == (409, {'error':message})


def test_vc_train_pause_resume_posts():
    from types import SimpleNamespace
    from ai_voice.desktop_control import ConflictError
    calls = []
    class Vc:
        def pause_training(self):
            calls.append('pause')
            return {'state': 'paused', 'user_paused': True, 'queue': []}
        def resume_training(self):
            calls.append('resume')
            return {'state': 'running', 'user_paused': False, 'queue': []}
    server = SimpleNamespace(vc=Vc())
    assert _gate_post(server, '/api/vc/train/pause', {}) == (200, {'state': 'paused', 'user_paused': True, 'queue': []})
    assert _gate_post(server, '/api/vc/train/resume', {}) == (200, {'state': 'running', 'user_paused': False, 'queue': []})
    assert calls == ['pause', 'resume']

    class Empty:
        def pause_training(self):
            raise ConflictError('Нет обучения')
        def resume_training(self):
            raise ConflictError('Нет обучения')
    server = SimpleNamespace(vc=Empty())
    assert _gate_post(server, '/api/vc/train/pause', {}) == (409, {'error': 'Нет обучения'})
    assert _gate_post(server, '/api/vc/train/resume', {}) == (409, {'error': 'Нет обучения'})


@pytest.mark.parametrize('error,status', [(None,200), (ValueError('bad text'),400),
    ('stopped',409), ('pending',409), (RuntimeError('Fish unavailable'),502)])
def test_vc_speak_post_status_codes(error, status):
    from types import SimpleNamespace
    from ai_voice.desktop_control import ConflictError
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    async def speak(text):
        assert text == 'Фраза'
        if error in ('stopped','pending'):
            raise ConflictError('Press Start first.' if error == 'stopped' else 'Предыдущая фраза ещё готовится.')
        if error is not None:
            raise error
        return {'id':'abc'}
    server = SimpleNamespace(loop=loop, control=SimpleNamespace(vc_speak=speak))
    try:
        code, body = _gate_post(server, '/api/vc/speak', {'text':'Фраза'})
        assert code == status
        assert body == {'id':'abc'} if status == 200 else 'error' in body
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


def test_vc_speak_cancel_post_and_unknown_preference(tmp_path):
    from types import SimpleNamespace
    from ai_voice.catalog import Catalog
    from ai_voice.desktop_control import DesktopControl
    calls = []
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    catalog = Catalog(tmp_path / 'catalog.json')
    control = DesktopControl(catalog)
    server = SimpleNamespace(loop=loop, control=control, vc=SimpleNamespace(cancel_text=lambda: calls.append('cancel')))
    try:
        assert _gate_post(server, '/api/vc/speak/cancel', {}) == (200,{})
        assert calls == ['cancel']
        code, body = _gate_post(server, '/api/preferences', {'vc_text_voice_id':'f'*32})
        assert code == 400 and body['error'] == 'Text voice not found.'
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


def _hf_get(server, path):
    """Dispatch and serialize a GET without opening a listening socket."""
    import io
    handler = object.__new__(DesktopHandler)
    handler.server, handler.path = server, path
    handler.allowed = lambda: True
    handler.wfile = io.BytesIO()
    status, headers = [], {}
    handler.send_response = status.append
    handler.send_header = lambda key, value: headers.update({key:value})
    handler.end_headers = lambda: None
    handler.do_GET()
    return status[0], headers, handler.wfile.getvalue()


@pytest.mark.parametrize('path,expected', [('/api/vc/hf/readme?id=card',200),
    ('/api/vc/hf/readme',400), ('/api/vc/hf/image?id=card&n=bad',400),
    ('/api/vc/hf/readme?id=stale',502)])
def test_hf_readme_http_status(path, expected):
    from types import SimpleNamespace
    from ai_voice.hf_catalog import HfCatalogError
    def readme(id):
        if id == 'stale': raise HfCatalogError('The card is outdated')
        return {'blocks':[], 'images':[], 'meaningful':False}
    server = SimpleNamespace(vc=SimpleNamespace(hf_catalog=SimpleNamespace(readme=readme)))
    status, headers, body = _hf_get(server,path)
    assert status == expected
    if expected == 200:
        assert json.loads(body) == {'blocks':[], 'images':[], 'meaningful':False}
        assert headers['Cache-Control'] == 'no-store'
    else: assert 'error' in json.loads(body)


@pytest.mark.parametrize('kind,mime', [('sample','audio/wav'), ('image','image/png')])
def test_hf_media_bytes_cache_headers_and_csp(kind, mime):
    from types import SimpleNamespace
    calls = []
    def media(*args): calls.append(args); return b'\x00\xffdata', mime
    server = SimpleNamespace(vc=SimpleNamespace(hf_catalog=SimpleNamespace(sample=media,image=media)))
    status, headers, body = _hf_get(server, f'/api/vc/hf/{kind}?id=card&n=0')
    assert status == 200 and body == b'\x00\xffdata'
    assert calls == [('card',)] if kind == 'sample' else calls == [('card',0)]
    assert headers['Content-Type'] == mime
    assert headers['Cache-Control'] == 'private, max-age=3600'
    assert "img-src 'self' data: blob:" in headers['Content-Security-Policy']
    assert "media-src 'self' blob:" in headers['Content-Security-Policy']


@pytest.mark.parametrize('error,status', [(None,200), (ValueError('bad'),400),
    ('monitor',409), (RuntimeError('Fish unavailable'),502)])
def test_vc_check_http_target_and_errors(error, status):
    from types import SimpleNamespace
    from ai_voice.desktop_control import ConflictError
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever); thread.start()
    async def speak(text, *, target):
        assert text == 'Hello! This is how this voice sounds. One, two, three — testing.'
        assert target == 'monitor'
        if error == 'monitor': raise ConflictError('Turn on monitoring')
        if error is not None: raise error
        return {'id':'check'}
    try:
        server = SimpleNamespace(loop=loop, control=SimpleNamespace(vc_speak=speak))
        code, body = _gate_post(server, '/api/vc/check', {})
        assert code == status
        assert body == ({'id':'check'} if status == 200 else {'error': 'Turn on monitoring' if error == 'monitor' else str(error)})
    finally:
        loop.call_soon_threadsafe(loop.stop); thread.join(timeout=1); loop.close()


def test_vc_check_timeout_cancels_future(monkeypatch):
    from types import SimpleNamespace
    from ai_voice import desktop
    cancelled = []
    class Future:
        def result(self, timeout):
            assert timeout == 30
            raise TimeoutError('timeout')
        def cancel(self): cancelled.append(True)
    def schedule(coro, loop): coro.close(); return Future()
    monkeypatch.setattr(desktop.asyncio, 'run_coroutine_threadsafe', schedule)
    async def speak(*args, **kwargs): pass
    server = SimpleNamespace(loop=None, control=SimpleNamespace(vc_speak=speak))
    assert _gate_post(server,'/api/vc/check',{}) == (502, {'error':'timeout'})
    assert cancelled == [True]


@pytest.mark.parametrize('enabled,device,expected', [(False,None,409), (True,None,409),
    (True,'Headphones',200), (True,'AI Voice',409)])
def test_check_uses_vc_worker_monitor_preferences(tmp_path, monkeypatch, enabled, device, expected):
    from types import SimpleNamespace
    from ai_voice import desktop_control
    from ai_voice.catalog import Catalog
    catalog = Catalog(write_library(tmp_path/'catalog.json',
        [{'id': TEST_VOICE_ID, 'name': 'Test voice'}], TEST_VOICE_ID, language="en"))
    catalog.update({'monitor_enabled':enabled,'monitor_device':device,'output_device':'AI Voice'})
    control = desktop_control.DesktopControl(catalog)
    assert control._monitor is None
    monkeypatch.setattr(desktop_control, 'resolve_output', lambda name: 1)
    monkeypatch.setattr(desktop_control, 'load_key', lambda: 'offline-test-key')
    calls = []
    async def speak(text, reference_id, key, *, target):
        calls.append((text,target)); return 'check'
    control.vc = SimpleNamespace(worker=SimpleNamespace(snapshot=lambda: {'state':'running'}),speak_text=speak)
    if expected == 200:
        assert asyncio.run(control.vc_speak('Проверка',target='monitor')) == {'id':'check'}
        assert calls == [('Проверка','monitor')]
    else:
        with pytest.raises(desktop_control.ConflictError, match='Turn on monitoring'):
            asyncio.run(control.vc_speak('Проверка',target='monitor'))
        assert calls == []


@pytest.mark.parametrize("outputs,expected_default", [(["Speakers", "BlackHole 2ch"], "BlackHole 2ch"), (["Speakers"], None)])
def test_api_voices_device_defaults(monkeypatch, outputs, expected_default):
    from types import SimpleNamespace
    devices = [{"name": "USB Mic", "max_input_channels": 1, "max_output_channels": 0}]
    devices += [{"name": n, "max_input_channels": 0, "max_output_channels": 2} for n in outputs]
    monkeypatch.setattr("ai_voice.desktop.list_devices", lambda: devices)
    monkeypatch.setattr("ai_voice.desktop.default_input_name", lambda: "USB Mic")
    catalog = FakeCatalog({})
    catalog.start_metadata = lambda: None
    server = SimpleNamespace(control=FakeControl(), catalog=catalog, preview=FakePreview())
    status, _headers, body = _hf_get(server, "/api/voices")
    assert status == 200, body
    result = json.loads(body)["devices"]
    assert result["default_input"] == "USB Mic"
    assert result["default_output"] == expected_default
    assert result["virtual"] == (["BlackHole 2ch"] if expected_default else [])
