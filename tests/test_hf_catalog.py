"""Offline HF catalog contracts, using the recorded reconnaissance responses."""
import copy
import json
from pathlib import Path
import time

import httpx
import pytest


FIXTURES = Path(__file__).parent / 'fixtures' / 'hf'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def catalog(handler, **kwargs):
    from ai_voice.hf_catalog import HfCatalog
    return HfCatalog(client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def tree_handler(repo, tree):
    def handler(request):
        if request.url.path == '/api/models':
            return httpx.Response(200, json=[repo])
        assert request.url.path == '/api/models/' + repo['id'] + '/tree/main'
        assert request.url.params['recursive'] == 'true'
        assert request.headers['User-Agent'] == 'AI-Voice/1.0'
        return httpx.Response(200, json=tree)
    return handler


def test_recorded_root_pair_and_lfs_size():
    repo = fixture('model-miku-blobs.json')
    # The detail fixture uses siblings, rather than the tree endpoint's keys.
    tree = [dict(type='file', path=f['rfilename'], size=f.get('size'), lfs=f.get('lfs'))
            for f in repo['siblings']]
    result = catalog(tree_handler(repo, tree)).search('', 'popular', 'all', 1)
    card, = result['items']
    assert card['repo'] == 'binant/Hatsune_Miku__RVC_v2_'
    assert card['files'] == [{'path': 'model.pth', 'size': 57587165},
                             {'path': 'model.index', 'size': 223220059}]
    assert card['total_size'] == 280807224
    assert card['revision'] == repo['sha']
    assert card['has_index'] is True
    assert result['page'] == 1 and result['has_more'] is False
    assert not card.get('preview_url')


def test_recorded_ivona_differently_named_index_is_paired():
    repo = dict(id='cotheq/ivona-maxim', cardData=dict(language=['ru'], license='mit'),
                downloads=73, likes=20, lastModified='2026-09-30T12:00:00Z')
    result = catalog(tree_handler(repo, fixture('tree-ivona-maxim.json'))).search('', 'popular', 'ru', 1)
    card, = result['items']
    assert card['has_index'] is True
    assert card['total_size'] == 352174562
    assert [f['path'] for f in card['files']] == ['botMaxim_e16_s8672.pth', 'added_IVF2410_Flat_nprobe_1_botMaxim_v2.index']


def test_two_models_with_unrelated_index_remain_separate():
    tree = [dict(type='file', path='voice.pth', size=50),
            dict(type='file', path='second.pth', size=50),
            dict(type='file', path='unrelated.index', size=100)]
    result = catalog(tree_handler({'id': 'test/unrelated'}, tree)).search('', 'popular', 'all', 1)
    assert len(result['items']) == 2
    assert all(not c['has_index'] and len(c['files']) == 1 for c in result['items'])


def test_recorded_single_zip():
    repo = dict(id='Nuril/rvc-zrch', cardData={'language': 'ru'})
    card, = catalog(tree_handler(repo, fixture('tree-zrch.json'))).search('', 'popular', 'ru', 1)['items']
    assert card['files'] == [{'path': 'zrch.zip', 'size': 287181634}]
    assert card['total_size'] == 287181634


def test_zip_collection_pages_are_cards_and_ids_preserve_versions():
    repo = dict(id='ArkanDash/rvc-genshin-impact', cardData={'license': 'mit'})
    client = catalog(tree_handler(repo, fixture('tree-genshin.json')))
    pages = [client.search('', 'popular', 'all', n) for n in (1, 2, 3)]
    assert [len(p['items']) for p in pages] == [24, 24, 18]
    assert [p['has_more'] for p in pages] == [True, True, False]
    cards = [c for p in pages for c in p['items']]
    assert len({c['id'] for c in cards}) == 66
    assert all(c['repo'] == repo['id'] for c in cards)
    assert any(c['files'][0]['path'] == 'prezipped/v1/ayaka-jp 100 epochs 40k.zip' and
               c['total_size'] == 106872212 for c in cards)
    assert cards[0]['id'] == client.search('', 'popular', 'all', 1)['items'][0]['id']


def test_language_metadata_priority_and_unlabelled_models():
    repos = [dict(id='test/card', cardData={'language': 'ru'}, tags=['en']),
             dict(id='test/tag', tags=['en']), dict(id='test/unknown')]
    def handler(request):
        if request.url.path == '/api/models':
            return httpx.Response(200, json=repos)
        return httpx.Response(200, json=[dict(type='file', path='voice.pth', size=50)])
    client = catalog(handler)
    assert [c['repo'] for c in client.search('', 'popular', 'ru', 1)['items']] == ['test/card']
    assert [c['repo'] for c in client.search('', 'popular', 'en', 1)['items']] == ['test/tag']
    assert len(client.search('', 'popular', 'all', 1)['items']) == 3


def test_size_limit_applies_to_selected_voice_not_repository():
    tree = [dict(type='file', path='one/model.pth', size=1024 ** 3),
            dict(type='file', path='too-large.zip', size=1024 ** 3 + 1),
            dict(type='file', path='two/model.pth', size=1024 ** 3),
            dict(type='file', path='two/model.index', size=1)]
    cards = catalog(tree_handler({'id': 'test/limit'}, tree)).search('', 'popular', 'all', 1)['items']
    assert [c['files'][0]['path'] for c in cards] == ['one/model.pth']


def test_service_files_datasets_and_ambiguous_indexes_are_not_voices():
    paths = ['dataset-raw/data.zip', 'pretrained/G-model.pth', 'D-model.pth',
             'embedders/model.pth', 'predictors/model.pth',
             'ambiguous/a.pth', 'ambiguous/b.pth', 'ambiguous/a.index',
             'voice/only.pth']
    tree = [dict(type='file', path=p, size=10) for p in paths]
    result = catalog(tree_handler({'id': 'test/mixed'}, tree)).search('', 'popular', 'all', 1)
    assert [c['files'][0]['path'] for c in result['items']] == ['ambiguous/a.pth', 'ambiguous/b.pth', 'voice/only.pth']
    assert [c['has_index'] for c in result['items']] == [True, False, False]


def test_recorded_metadata_and_search_query_sort():
    repos = fixture('models-rvc-popular.json')
    def handler(request):
        if request.url.path == '/api/models':
            assert request.url.params['filter'] == 'rvc'
            assert request.url.params['search'] == 'имя & voice'
            assert request.url.params['sort'] == 'lastModified'
            assert request.url.params['direction'] == '-1'
            assert request.url.params['full'] == request.url.params['cardData'] == 'true'
            return httpx.Response(200, json=repos)
        return httpx.Response(200, json=[dict(type='file', path='voice.pth', size=40)])
    cards = catalog(handler).search('имя & voice', 'new', 'all', 1)['items']
    assert cards[0]['downloads'] == repos[0]['downloads']
    assert cards[0]['likes'] == repos[0]['likes']
    assert cards[0]['license'] == repos[0]['cardData']['license']


def test_tree_and_model_cursor_pages_are_followed():
    def handler(request):
        if request.url.path == '/api/models':
            if request.url.params.get('cursor') == 'next-repo':
                return httpx.Response(200, json=[dict(id='test/two')])
            return httpx.Response(200, json=[dict(id='test/one')],
                                 headers={'Link': '<https://huggingface.co/api/models?cursor=next-repo>; rel="next"'})
        if request.url.path.endswith('/one/tree/main'):
            if request.url.params.get('cursor') == 'next-tree':
                return httpx.Response(200, json=[dict(type='file', path='nested/a.zip', size=30)])
            return httpx.Response(200, json=[dict(type='directory', path='nested', size=0)],
                                 headers={'Link': '<https://huggingface.co/api/models/test/one/tree/main?cursor=next-tree>; rel="next"'})
        return httpx.Response(200, json=[dict(type='file', path='voice.pth', size=50)])
    result = catalog(handler).search('', 'popular', 'all', 1)
    assert [c['repo'] for c in result['items']] == ['test/one', 'test/two']
    assert result['has_more'] is False


def test_cache_expires_after_ten_minutes_and_returns_independent_values():
    now = [0]
    tree = [dict(type='file', path='voice.pth', size=50)]
    handler = tree_handler({'id': 'test/cache'}, tree)
    online = [True]
    def transport(request):
        if not online[0]:
            raise httpx.ConnectError('offline', request=request)
        return handler(request)
    client = catalog(transport, clock=lambda: now[0])
    original = client.search('', 'popular', 'all', 1)
    expected = copy.deepcopy(original)
    original['items'][0]['files'].clear()
    online[0] = False
    now[0] = 599
    assert client.search('', 'popular', 'all', 1) == expected
    now[0] = 600
    from ai_voice.hf_catalog import HfCatalogError
    with pytest.raises(HfCatalogError) as exc:
        client.search('', 'popular', 'all', 1)
    assert exc.value.status_code == 503


def test_network_failure_is_user_facing_503_and_not_cached():
    from ai_voice.hf_catalog import HfCatalogError
    online = [False]
    def handler(request):
        if not online[0]:
            raise httpx.ReadTimeout('timeout', request=request)
        return httpx.Response(200, json=[])
    client = catalog(handler)
    with pytest.raises(HfCatalogError, match='No connection to Hugging Face') as exc:
        client.search('', 'popular', 'all', 1)
    assert exc.value.status_code == 503
    online[0] = True
    assert client.search('', 'popular', 'all', 1)['items'] == []


@pytest.mark.parametrize('sort', ['downloads', 'likes', 'lastModified'])
def test_contract_sort_values(sort):
    def handler(request):
        assert request.url.params['sort'] == sort
        assert request.url.params['direction'] == '-1'
        return httpx.Response(200, json=[])
    assert catalog(handler).search(sort=sort)['items'] == []


def test_cached_card_lookup_expires_and_is_isolated():
    now = [0]
    client = catalog(tree_handler({'id': 'test/cache', 'sha': 'a' * 40},
                                 [dict(path='a.pth', size=5)]), clock=lambda: now[0])
    card, = client.search()['items']
    assert client.get_card('unknown') is None
    client.get_card(card['id'])['files'].clear()
    assert client.get_card(card['id']) == card
    now[0] = 600
    assert client.get_card(card['id']) is None


def test_more_does_not_refetch_trees():
    from collections import Counter
    repos = [dict(id=f'test/r{i}', sha=f'{i:040x}') for i in range(30)]
    trees = Counter()

    def handler(request):
        if request.url.path == '/api/models':
            return httpx.Response(200, json=repos)
        trees[request.url.path] += 1
        return httpx.Response(200, json=[dict(type='file', path='voice.pth', size=50)])

    client = catalog(handler)
    assert [c['repo'] for c in client.search(page=1)['items']][-1] == 'test/r23'
    assert [c['repo'] for c in client.search(page=2)['items']] == [f'test/r{i}' for i in range(24, 30)]
    assert all(count == 1 for count in trees.values())


def test_tree_cache_keyed_by_sha():
    now = [0]
    sha = ['a' * 40]
    tree_calls = []

    def handler(request):
        if request.url.path == '/api/models':
            return httpx.Response(200, json=[dict(id='test/sha', sha=sha[0])])
        tree_calls.append(request.url.path)
        return httpx.Response(200, json=[dict(type='file', path='voice.pth', size=50)])

    client = catalog(handler, clock=lambda: now[0])
    client.search(page=1)
    now[0] = 600
    sha[0] = 'b' * 40
    client.search(page=1)
    assert tree_calls == ['/api/models/test/sha/tree/main', '/api/models/test/sha/tree/main']
    now[0] = 700
    client.search(page=1)
    assert len(tree_calls) == 2


def test_tree_cache_expires_and_is_bounded(monkeypatch):
    import ai_voice.hf_catalog as module
    monkeypatch.setattr(module, 'TREE_CACHE_MAX', 2)
    now = [0]
    current = ['test/one']
    calls = []

    def handler(request):
        if request.url.path == '/api/models':
            return httpx.Response(200, json=[dict(id=current[0], sha=current[0] * 40)])
        calls.append(request.url.path)
        return httpx.Response(200, json=[dict(type='file', path='voice.pth', size=50)])

    client = catalog(handler, clock=lambda: now[0])
    for repo in ('test/one', 'test/two', 'test/three'):
        current[0] = repo
        client.search(q=repo, page=1)
    assert len(client._trees) == 2
    now[0] = 3600
    current[0] = 'test/one'
    client.search(page=1)
    assert len(calls) == 4


def test_search_lock_not_held_during_network():
    import threading
    entered = threading.Event()
    release = threading.Event()

    def handler(request):
        entered.set()
        release.wait(2)
        return httpx.Response(200, json=[])

    client = catalog(handler)
    thread = threading.Thread(target=lambda: client.search(page=1))
    thread.start()
    assert entered.wait(1)
    started = time.monotonic()
    assert client.get_card('x') is None
    assert time.monotonic() - started < 1
    release.set()
    thread.join(2)


@pytest.mark.parametrize('audio,models,expected', [
    (['clip.wav'], ['voice.pth'], 'clip.wav'),
    (['clip.mp3', 'EXAMPLE.wav'], ['voice.pth'], 'EXAMPLE.wav'),
    (['clip.mp3', 'sample-1.flac'], ['voice.pth'], 'sample-1.flac'),
    (['one.wav', 'two.wav'], ['voice.pth'], None),
    (['sample.wav', 'demo.wav'], ['voice.pth'], None),
    (['sample.wav'], ['a.pth', 'b.pth'], None),
])
def test_sample_selection_is_unambiguous(audio, models, expected):
    from ai_voice.hf_catalog import _cards
    tree = [dict(type='file', path=p, size=10) for p in models + audio]
    cards = list(_cards({'id': 'test/voice'}, tree))
    assert all(c['sample'] == ({'path': expected, 'size': 10} if expected else None) for c in cards)


@pytest.mark.parametrize('size', [0, -1, True, '10', 26214401])
def test_invalid_sample_size(size):
    from ai_voice.hf_catalog import _cards
    card, = _cards({'id':'test/voice'}, [dict(type='file',path='voice.pth',size=10),
                                      dict(type='file',path='sample.wav',size=size)])
    assert card['sample'] is None


@pytest.mark.parametrize('url', ['http://huggingface.co/a', 'https://outside.test/a',
                                 'https://huggingface.co.evil.test/a'])
def test_media_rejects_foreign_hosts_without_request(url):
    from ai_voice.hf_catalog import HfCatalogError
    def handler(request):
        pytest.fail('Unexpected network request')
    with pytest.raises(HfCatalogError, match='Invalid address'):
        catalog(handler)._fetch_limited(catalog(handler)._client, url, 10, {'audio/wav'})


@pytest.mark.parametrize('host,allowed', [('outside.test',False), ('cas-bridge.xethub.hf.co',True)])
def test_media_redirect_host_checked(host, allowed):
    from ai_voice.hf_catalog import HfCatalogError
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            return httpx.Response(302, headers={'location': f'https://{host}/sample.wav'})
        return httpx.Response(200, content=b'audio', headers={'content-type':'application/octet-stream'})
    c = catalog(handler)
    if allowed:
        assert c._fetch_limited(c._client, 'https://huggingface.co/sample.wav', 10, {'audio/wav'}) == (b'audio', 'audio/wav')
    else:
        with pytest.raises(HfCatalogError, match='Invalid address'):
            c._fetch_limited(c._client, 'https://huggingface.co/sample.wav', 10, {'audio/wav'})
        assert calls == ['huggingface.co']


@pytest.mark.parametrize('declared,reads', [(None,2), ('100',0)])
def test_media_limit_stops_stream_and_closes_response(declared, reads):
    from ai_voice.hf_catalog import HfCatalogError
    class Stream(httpx.SyncByteStream):
        count = 0
        closed = False
        def __iter__(self):
            for _ in range(20):
                self.count += 1
                yield b'1234'
        def close(self):
            self.closed = True
    stream = Stream()
    headers = {'content-type':'audio/wav'}
    if declared is not None:
        headers['content-length'] = declared
    c = catalog(lambda req: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(HfCatalogError, match='File is too large'):
        c._fetch_limited(c._client, 'https://huggingface.co/sample.wav', 5, {'audio/wav'})
    assert stream.count == reads
    assert stream.closed


def test_svg_is_rejected():
    from ai_voice.hf_catalog import HfCatalogError
    c = catalog(lambda req: httpx.Response(200, content=b'<svg/>', headers={'content-type':'image/svg+xml'}))
    with pytest.raises(HfCatalogError, match='Invalid file type'):
        c._fetch_limited(c._client, 'https://huggingface.co/a.svg', 100, {'image/png'})


def test_stale_sample_is_rejected():
    from ai_voice.hf_catalog import HfCatalogError
    with pytest.raises(HfCatalogError, match='The card is outdated'):
        catalog(lambda req: pytest.fail('Unexpected request')).sample('unknown')


def test_media_cache_reuses_file_and_evicts_oldest(tmp_path, monkeypatch):
    from ai_voice import hf_catalog
    monkeypatch.setattr(hf_catalog, 'DATA', tmp_path)
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=b'1234', headers={'content-type':'audio/wav'})
    c = catalog(handler, media_cache_limit=8)
    def media(key):
        return c._media(key, f'https://huggingface.co/{key}.wav', 10, {'audio/wav'}, 'wav')
    assert media('one') == media('one') == (b'1234', 'audio/wav')
    assert len(calls) == 1
    first, = (tmp_path/'hf-media').iterdir()
    import os
    os.utime(first, (1,1))
    media('two'); media('three')
    assert not first.exists()
    assert sum(p.stat().st_size for p in (tmp_path/'hf-media').iterdir()) == 8


@pytest.mark.parametrize('operation', ['readme', 'media'])
@pytest.mark.parametrize('fail', [False, True])
def test_owned_media_client_closed_on_success_and_error(tmp_path, monkeypatch, operation, fail):
    from ai_voice import hf_catalog
    monkeypatch.setattr(hf_catalog, 'DATA', tmp_path)
    real_client = httpx.Client
    clients = []
    def handler(req):
        return httpx.Response(500 if fail else 200, content=b'Description ' * 5,
                              headers={'content-type':'text/plain'})
    def make_client():
        client = real_client(transport=httpx.MockTransport(handler)); clients.append(client); return client
    monkeypatch.setattr(hf_catalog.httpx, 'Client', make_client)
    c = hf_catalog.HfCatalog()
    monkeypatch.setattr(c, 'get_card', lambda id: {'repo':'test/voice','revision':'abc'})
    def request():
        return c.readme('id') if operation == 'readme' else c._media('key','https://huggingface.co/a',100,{'text/plain'})
    if fail:
        with pytest.raises(hf_catalog.HfCatalogError): request()
    else: request()
    assert len(clients) == 1 and clients[0].is_closed


@pytest.fixture
def hf_token(monkeypatch):
    from ai_voice import hf_catalog
    state = {'token': 'hf_testtoken0123456789', 'reads': 0}
    def load(name='FISH_API_KEY'):
        assert name == 'HF_TOKEN'
        state['reads'] += 1
        return state['token']
    monkeypatch.setattr(hf_catalog, 'load_key', load)
    hf_catalog.invalidate_hf_token()
    yield state
    hf_catalog.invalidate_hf_token()


def test_hf_token_header_only_on_huggingface_host(hf_token):
    seen = []
    def handler(request):
        seen.append((request.url.host, request.headers.get('Authorization')))
        if len(seen) == 1:
            return httpx.Response(200, json=[])
        if len(seen) == 2:
            return httpx.Response(302, headers={'location': 'https://cdn-lfs.huggingface.co/sample.wav'})
        return httpx.Response(200, content=b'audio', headers={'content-type': 'audio/wav'})
    c = catalog(handler)
    c._get(c._client, 'https://huggingface.co/api/models')
    c._fetch_limited(c._client, 'https://huggingface.co/sample.wav', 10, {'audio/wav'})
    assert seen == [('huggingface.co', 'Bearer hf_testtoken0123456789'),
                    ('huggingface.co', 'Bearer hf_testtoken0123456789'),
                    ('cdn-lfs.huggingface.co', None)]


def test_hf_no_token_no_header(hf_token):
    hf_token['token'] = None
    seen = []
    def handler(request):
        seen.append(request.headers.get('Authorization'))
        return httpx.Response(200, json=[])
    c = catalog(handler)
    c._get(c._client, 'https://huggingface.co/api/models')
    assert seen == [None]


def test_hf_token_cache_and_invalidate(hf_token):
    from ai_voice import hf_catalog
    now = [10.0]
    clock = lambda: now[0]
    assert hf_catalog._hf_token(clock) == 'hf_testtoken0123456789'
    now[0] = 50.0
    hf_catalog._hf_token(clock)
    assert hf_token['reads'] == 1
    hf_catalog.invalidate_hf_token()
    hf_catalog._hf_token(clock)
    assert hf_token['reads'] == 2
    now[0] = 111.0
    hf_catalog._hf_token(clock)
    assert hf_token['reads'] == 3
