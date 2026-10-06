"""Library deletion transactions, active voice handover and localhost API."""
import asyncio
import copy
import json
import threading
from http.server import ThreadingHTTPServer
from urllib import error, request

import pytest

from ai_voice.catalog import Catalog
from helpers import TEST_VOICE_ID, write_library
from ai_voice.config import VOICES, VOICE_NAMES
from ai_voice.desktop import DesktopHandler
from ai_voice.desktop_control import DesktopControl


@pytest.fixture
def catalog(tmp_path):
    cat = Catalog(path=write_library(tmp_path / 'desktop.json',
        [{'id': TEST_VOICE_ID, 'name': 'Быков'},
         {'id': 'b' * 32, 'name': 'Second'}, {'id': 'c' * 32, 'name': 'Third'}], TEST_VOICE_ID, language="en"))
    ids = [v['id'] for v in cat.voices()]
    cat.update({'favorite_ids': ids[:3], 'voice_id': ids[0]})
    return cat


def test_remove_many_switches_selected_and_cleans_favorites(catalog):
    ids = [v['id'] for v in catalog.voices()]
    prefs = catalog.remove_many([ids[0], ids[1], ids[0]])
    assert [v['id'] for v in catalog.voices()] == ids[2:]
    assert prefs['favorite_ids'] == [ids[2]]
    assert prefs['voice_id'] == ids[2]
    assert Catalog(path=catalog.path).voices() == catalog.voices()
    assert all('fish_' + v not in VOICES and 'fish_' + v not in VOICE_NAMES for v in ids[:2])


@pytest.mark.parametrize('value', [None, 'bad', [], ['bad'], ['a' * 32] * 501])
def test_remove_many_rejects_invalid_list(catalog, value):
    original = copy.deepcopy(catalog.data)
    with pytest.raises(ValueError, match='Invalid voice list'):
        catalog.remove_many(value)
    assert catalog.data == original


def test_remove_many_unknown_and_all_leave_library_unchanged(catalog):
    ids = [v['id'] for v in catalog.voices()]
    original = copy.deepcopy(catalog.data)
    for remove, message in [([ids[0], 'f' * 32], 'This voice has not been added'),
                            (ids, 'Cannot delete all voices')]:
        with pytest.raises(ValueError, match=message):
            catalog.remove_many(remove)
        assert catalog.data == original


def test_remove_many_save_failure_rolls_back_data_and_registry(catalog, monkeypatch):
    original = copy.deepcopy(catalog.data)
    registry, names, disk = dict(VOICES), dict(VOICE_NAMES), catalog.path.read_text()
    monkeypatch.setattr(catalog, 'save', lambda: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        catalog.remove_many([v['id'] for v in catalog.voices()][:2])
    assert catalog.data == original
    assert VOICES == registry and VOICE_NAMES == names
    assert catalog.path.read_text() == disk


@pytest.mark.asyncio
@pytest.mark.parametrize('save_fails', [False, True])
async def test_control_switches_before_deletion_and_rolls_back(catalog, monkeypatch, save_fails):
    ids = [v['id'] for v in catalog.voices()]
    old_slug, next_slug = catalog.slug(ids[0]), catalog.slug(ids[2])
    calls = []

    class Session:
        async def change_voice(self, slug):
            assert ids[0] in catalog._library_ids()
            calls.append(slug)

    control = DesktopControl(catalog)
    control.session = Session()
    control.state['voice_id'] = ids[0]
    if save_fails:
        monkeypatch.setattr(catalog, 'save', lambda: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            await control.remove_voices(ids[:2])
        assert calls == [next_slug, old_slug]
        assert control.state['voice_id'] == ids[0]
        assert catalog.preferences()['voice_id'] == ids[0]
    else:
        result = await control.remove_voices([ids[0], ids[1], ids[0]])
        assert calls == [next_slug]
        assert result['removed'] == 2
        assert result['preferences']['voice_id'] == ids[2]
        assert control.state['voice_id'] == ids[2]


@pytest.mark.asyncio
async def test_control_switch_failure_keeps_library(catalog):
    original = copy.deepcopy(catalog.data)

    class Session:
        async def change_voice(self, slug):
            raise RuntimeError('switch failed')

    control = DesktopControl(catalog)
    control.session = Session()
    control.state['voice_id'] = catalog.preferences()['voice_id']
    with pytest.raises(ValueError, match='Could not switch the active voice'):
        await control.remove_voices([control.state['voice_id']])
    assert catalog.data == original


def test_http_bulk_remove_contract(catalog, monkeypatch):
    from ai_voice import desktop
    monkeypatch.setattr(desktop, 'list_devices', lambda: [])
    monkeypatch.setattr(catalog, 'start_metadata', lambda: None)
    server = ThreadingHTTPServer(('127.0.0.1', 0), DesktopHandler)
    server.token = 'test-token'
    server.catalog = catalog
    server.control = DesktopControl(catalog)
    server.loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=server.loop.run_forever, daemon=True)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    loop_thread.start()
    http_thread.start()

    def call(path, body=None):
        req = request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                              data=None if body is None else json.dumps(body).encode(),
                              headers={'X-AI-Voice-Token': server.token, 'Content-Type': 'application/json'})
        try:
            with request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        for ids in ['bad', ['bad'], [], ['a' * 32] * 501]:
            assert call('/api/remove_voices', {'ids': ids})[0] == 400
        ids = [v['id'] for v in catalog.voices()]
        code, data = call('/api/remove_voices', {'ids': ids[:2]})
        assert code == 200
        assert set(data) == {'items', 'preferences', 'removed'}
        assert data['removed'] == 2
        assert [v['id'] for v in data['items']] == ids[2:]
        code, data = call('/api/voices')
        assert code == 200 and 'hidden_builtin' not in data
        assert call('/api/restore_builtin', {'all': True})[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)
        server.loop.call_soon_threadsafe(server.loop.stop)
        loop_thread.join(timeout=2)
        server.loop.close()
