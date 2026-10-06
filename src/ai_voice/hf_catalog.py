"""Anonymous HF RVC catalog.

Cards describe file candidates, not verified RVC checkpoints or ZIP contents.
No downloading, torch, paid API, or audio devices are used here.
"""
from copy import deepcopy
from pathlib import PurePosixPath
import re
import threading
import time
import hashlib
import os
from urllib.parse import quote, urljoin, urlsplit
from .i18n import t
from .paths import DATA
from .secrets import load_key
from .hf_readme import parse_readme

import httpx


BASE_URL = 'https://huggingface.co'
PAGE_SIZE = 24
CACHE_SECONDS = 600
TREE_CACHE_SECONDS = 3600
TREE_CACHE_MAX = 512
MAX_BYTES = 1024 ** 3  # VcStore's default import limit.
AUDIO_SUFFIXES = {'.wav','.mp3','.ogg','.flac','.m4a'}
MEDIA_CACHE_LIMIT = 200 * 1024 * 1024
HF_TOKEN_TTL = 60.0
_TOKEN_CACHE = {'value': None, 'at': None}


def _hf_token(clock=time.monotonic):
    now = clock()
    if _TOKEN_CACHE['at'] is None or now - _TOKEN_CACHE['at'] >= HF_TOKEN_TTL:
        _TOKEN_CACHE['value'], _TOKEN_CACHE['at'] = load_key('HF_TOKEN'), now
    return _TOKEN_CACHE['value']


def invalidate_hf_token():
    _TOKEN_CACHE['value'] = _TOKEN_CACHE['at'] = None


def _headers(url):
    """The token goes only to huggingface.co itself, never to CDN redirect targets."""
    headers = {'User-Agent': 'AI-Voice/1.0'}
    token = _hf_token() if urlsplit(url).hostname == 'huggingface.co' else None
    if token:
        headers['Authorization'] = 'Bearer ' + token
    return headers


SORTS = {'downloads': 'downloads', 'likes': 'likes', 'lastModified': 'lastModified',
         'popular': 'downloads', 'new': 'lastModified'}
LANGUAGES = {'ru', 'en', 'all'}


class HfCatalogError(Exception):
    """A user-facing network failure for the eventual HTTP adapter."""
    status_code = 503


def _languages(repo):
    card = repo.get('cardData') or {}
    value = card.get('language')
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        known = list(dict.fromkeys(v.strip().lower() for v in value
                                   if isinstance(v, str) and v.strip()))
        if known:
            return known
    # Reconnaissance found standalone language tags, not language:<code>.
    return list(dict.fromkeys(t for t in repo.get('tags', []) if t in ('ru', 'en')))


def _candidate_file(entry):
    if entry.get('type', 'file') != 'file':
        return None
    path = entry.get('path', entry.get('rfilename'))
    if not isinstance(path, str) or not path or '\\' in path:
        return None
    parts = path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        return None
    lowered = [part.lower() for part in parts]
    if any(part in ('pretrained', 'embedders', 'predictors') or
           part.startswith('dataset') for part in lowered):
        return None
    name = PurePosixPath(path)
    if name.suffix.lower() not in ('.pth', '.index', '.zip'):
        return None
    if name.suffix.lower() == '.pth' and re.match(r'^[GD][_-]', name.name, re.I):
        return None
    lfs = entry.get('lfs') or {}
    size = lfs.get('size', entry.get('size'))
    if type(size) is not int or size <= 0:
        return None
    return {'path': path, 'size': size}


def _cards(repo, entries):
    """Separate ZIPs and unambiguous .pth/index groups; do not pick an index."""
    groups = {}
    candidates = []
    seen = set()
    for entry in entries:
        file = _candidate_file(entry)
        if file is None or file['path'] in seen:
            continue
        seen.add(file['path'])
        path = PurePosixPath(file['path'])
        if path.suffix.lower() == '.zip':
            candidates.append([file])
        else:
            group = groups.setdefault(str(path.parent), {'pth': [], 'index': []})
            group[path.suffix.lower()[1:]].append(file)
    for group in groups.values():
        models, indexes = group['pth'], group['index']
        for model in models:
            matches = [index for index in indexes if PurePosixPath(index['path']).stem ==
                       PurePosixPath(model['path']).stem]
            if len(matches) == 1:
                candidates.append([model, matches[0]])
            elif len(models) == len(indexes) == 1:
                candidates.append([model, indexes[0]])
            else:
                candidates.append([model])

    metadata = repo.get('cardData') or {}
    audios = []
    for entry in entries:
        if entry.get('type', 'file') != 'file': continue
        path = entry.get('path', entry.get('rfilename'))
        size = (entry.get('lfs') or {}).get('size', entry.get('size'))
        if isinstance(path, str) and PurePosixPath(path).suffix.lower() in AUDIO_SUFFIXES and type(size) is int and 0 < size <= 26214400:
            audios.append({'path': path, 'size': size})
    sample = None
    if len(candidates) == 1 and audios:
        if len(audios) == 1:
            sample = audios[0]
        else:
            named = [a for a in audios if PurePosixPath(a['path']).stem.lower() in {'example','sample','demo','preview'} or PurePosixPath(a['path']).stem.lower().startswith(('demo','sample'))]
            if len(named) == 1: sample = named[0]
    license_id = metadata.get('license')
    if not license_id:
        license_id = next((t[8:] for t in repo.get('tags', [])
                           if isinstance(t, str) and t.startswith('license:')), 'unknown')
    for files in candidates:
        total = sum(file['size'] for file in files)
        if total > MAX_BYTES:
            continue
        path = files[0]['path']
        title = PurePosixPath(path).stem
        if len(candidates) == 1:
            title = metadata.get('pretty_name') or title
        yield dict(id=repo['id'] + ':' + path, repo=repo['id'], title=title,
                   files=files, total_size=total, revision=repo.get('sha'),
                   has_index=any(PurePosixPath(f['path']).suffix.lower() == '.index' for f in files),
                   downloads=repo.get('downloads', 0), likes=repo.get('likes', 0),
                   updated=repo.get('lastModified'), lang=_languages(repo),
                   license=license_id, sample=deepcopy(sample))
                   


class HfCatalog:
    def __init__(self, *, client=None, clock=time.monotonic, media_cache_limit=MEDIA_CACHE_LIMIT):
        self._client = client
        self._clock = clock
        self._cache = {}
        self._trees = {}
        self._lock = threading.Lock()
        self._readmes = {}
        self._media_cache_limit = media_cache_limit

    @staticmethod
    def _allowed_host(host):
        return host == 'huggingface.co' or host.endswith('.huggingface.co') or host == 'hf.co' or host.endswith('.hf.co')

    def _fetch_limited(self, client, url, max_bytes, allowed_types):
        for _ in range(5):
            p=urlsplit(url)
            if p.scheme!='https' or not self._allowed_host(p.hostname or ''): raise HfCatalogError(t("errors.hf_address_invalid"))
            try:
                with client.stream('GET', url, timeout=15, follow_redirects=False,
                                   headers=_headers(url)) as response:
                    if response.status_code in (301,302,303,307,308):
                        url=urljoin(url,response.headers.get('location','')); continue
                    if response.status_code==404: raise HfCatalogError(t("errors.hf_file_missing"))
                    if response.status_code>=400: raise HfCatalogError(t("errors.hf_connection"))
                    ctype=response.headers.get('content-type','').split(';')[0].lower()
                    ext=PurePosixPath(p.path).suffix.lower()
                    if ctype=='application/octet-stream' and ext in AUDIO_SUFFIXES:
                        ctype={'.wav':'audio/wav','.mp3':'audio/mpeg','.ogg':'audio/ogg','.flac':'audio/flac','.m4a':'audio/mp4'}[ext]
                    if ctype not in allowed_types: raise HfCatalogError(t("errors.hf_file_type"))
                    length=response.headers.get('content-length')
                    if length and length.isdigit() and int(length)>max_bytes:
                        raise HfCatalogError(t("errors.hf_file_size"))
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        if len(body)+len(chunk)>max_bytes:
                            raise HfCatalogError(t("errors.hf_file_size"))
                        body.extend(chunk)
                    return bytes(body),ctype
            except httpx.HTTPError as exc:
                raise HfCatalogError(t("errors.hf_connection")) from exc
        raise HfCatalogError(t("errors.hf_redirects"))

    def _media(self, key, url, limit, types, suffix='bin'):
        folder=DATA/'hf-media'; folder.mkdir(parents=True,exist_ok=True); path=folder/(hashlib.sha256(key.encode()).hexdigest()+'.'+suffix)
        if path.exists():
            suffix=path.suffix.lower(); mime={'wav':'audio/wav','mp3':'audio/mpeg','ogg':'audio/ogg','flac':'audio/flac','m4a':'audio/mp4','png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp','gif':'image/gif'}.get(suffix.lstrip('.'),'application/octet-stream')
            return path.read_bytes(), mime
        if self._client is not None:
            body,mime=self._fetch_limited(self._client,url,limit,types)
        else:
            with httpx.Client() as client:
                body,mime=self._fetch_limited(client,url,limit,types)
        path.write_bytes(body)
        files=sorted(folder.iterdir(),key=lambda p:p.stat().st_mtime)
        total=sum(p.stat().st_size for p in files)
        for old in files:
            if total<=self._media_cache_limit: break
            total-=old.stat().st_size; old.unlink(missing_ok=True)
        return body,mime

    def readme(self, card_id):
        card=self.get_card(card_id)
        if not card: raise HfCatalogError(t("errors.hf_card_stale"))
        key=(card['repo'],card.get('revision') or 'main'); now=self._clock(); cached=self._readmes.get(key)
        if cached and now-cached[0]<3600:return deepcopy(cached[1])
        url=f"{BASE_URL}/{card['repo']}/raw/{key[1]}/README.md"
        try:
            if self._client is not None:
                body,_=self._fetch_limited(self._client,url,65536,{'text/plain','text/markdown'})
            else:
                with httpx.Client() as client:
                    body,_=self._fetch_limited(client,url,65536,{'text/plain','text/markdown'})
        except HfCatalogError as exc:
            if str(exc)==t("errors.hf_file_missing"): return {'blocks':[],'images':[],'meaningful':False}
            raise
        result=parse_readme(body.decode('utf-8','replace'),*key); self._readmes[key]=(now,result); return deepcopy(result)

    def sample(self, card_id):
        card=self.get_card(card_id)
        if not card: raise HfCatalogError(t("errors.hf_card_stale"))
        sample=card.get('sample')
        if not sample: raise HfCatalogError(t("errors.hf_sample_missing"))
        rev=card.get('revision') or 'main'; path=sample['path']; url=f"{BASE_URL}/{card['repo']}/resolve/{rev}/{quote(path)}"
        return self._media(card['repo']+rev+path,url,26214400,{'audio/wav','audio/x-wav','audio/wave','audio/mpeg','audio/ogg','audio/flac','audio/x-flac','audio/mp4','audio/x-m4a'},PurePosixPath(path).suffix.lower().lstrip('.') or 'bin')

    def image(self, card_id, n):
        data=self.readme(card_id); card=self.get_card(card_id)
        if card is None or type(n) is not int or n<0 or n>=len(data['images']): raise HfCatalogError(t("errors.hf_image_missing"))
        url=data['images'][n]; return self._media(card['repo']+(card.get('revision') or 'main')+url,url,3145728,{'image/png','image/jpeg','image/webp','image/gif'},PurePosixPath(urlsplit(url).path).suffix.lower().lstrip('.') or 'bin')

    @staticmethod
    def _next(response):
        link = response.links.get('next', {}).get('url')
        if not link:
            return None
        url = urljoin(str(response.url), link)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'huggingface.co' or not parsed.path.startswith('/api/models'):
            raise HfCatalogError(t("errors.hf_connection"))
        return url

    @staticmethod
    def _get(client, url, params=None):
        try:
            response = client.get(url, params=params, timeout=15,
                                  headers=_headers(url), follow_redirects=True)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('HF list response expected')
            return data, HfCatalog._next(response)
        except (httpx.HTTPError, ValueError) as exc:
            raise HfCatalogError(t("errors.hf_connection")) from exc

    def _tree(self, client, repo):
        url = BASE_URL + '/api/models/' + quote(repo, safe='/') + '/tree/main'
        params = {'recursive': 'true', 'limit': 100, 'expand': 'false'}
        entries, visited = [], set()
        while url:
            if url in visited:
                raise HfCatalogError(t("errors.hf_connection"))
            visited.add(url)
            page, url = self._get(client, url, params)
            entries.extend(page)
            params = None
        return entries

    def _search(self, client, q, sort, lang, page):
        url = BASE_URL + '/api/models'
        params = dict(filter='rvc', sort=SORTS[sort], direction=-1,
                      limit=PAGE_SIZE, full='true', cardData='true')
        if q:
            params['search'] = q
        cards, seen_repos, visited = [], set(), set()
        end = page * PAGE_SIZE
        # Look ahead by one card; HF pages count repos, not compatible voices.
        while url and len(cards) <= end:
            if url in visited:
                raise HfCatalogError(t("errors.hf_connection"))
            visited.add(url)
            repos, url = self._get(client, url, params)
            params = None
            for repo in repos:
                repo_id = repo.get('id')
                if (not isinstance(repo_id, str) or repo_id in seen_repos or
                        not re.fullmatch(r'[\w.-]+/[\w.-]+', repo_id)):
                    continue
                seen_repos.add(repo_id)
                if lang != 'all' and lang not in _languages(repo):
                    continue
                siblings = repo.get('siblings')
                if isinstance(siblings, list) and not any(
                        str(f.get('rfilename', '')).lower().endswith(('.pth', '.zip'))
                        for f in siblings):
                    continue
                cards.extend(_cards(repo, self._tree_cached(client, repo_id, repo.get('sha'))))
                if len(cards) > end:
                    break
        return {'items': cards[(page - 1) * PAGE_SIZE:end],
                'page': page, 'has_more': len(cards) > end}

    def get_card(self, card_id):
        """Resolve only a still-cached search card, never a caller-supplied URL."""
        with self._lock:
            now = self._clock()
            for saved, result in self._cache.values():
                if now - saved < CACHE_SECONDS:
                    for card in result['items']:
                        if card['id'] == card_id:
                            return deepcopy(card)
        return None

    def _tree_cached(self, client, repo_id, sha):
        if not isinstance(sha, str) or not sha:
            return self._tree(client, repo_id)
        key = (repo_id, sha)
        with self._lock:
            now = self._clock()
            self._trees = {k: v for k, v in self._trees.items()
                           if now - v[0] < TREE_CACHE_SECONDS}
            cached = self._trees.get(key)
            if cached is not None:
                return deepcopy(cached[1])
        entries = self._tree(client, repo_id)
        with self._lock:
            self._trees[key] = (self._clock(), deepcopy(entries))
            while len(self._trees) > TREE_CACHE_MAX:
                oldest = min(self._trees, key=lambda k: self._trees[k][0])
                del self._trees[oldest]
        return entries

    def search(self, q='', sort='downloads', lang='all', page=1):
        if not isinstance(q, str) or sort not in SORTS or lang not in LANGUAGES:
            raise ValueError(t("errors.hf_parameters_invalid"))
        if type(page) is not int or page < 1:
            raise ValueError(t("errors.hf_page_invalid"))
        key = (q, sort, lang, page)
        with self._lock:
            now = self._clock()
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}
            if key in self._cache:
                return deepcopy(self._cache[key][1])
        if self._client is None:
            with httpx.Client() as client:
                result = self._search(client, q, sort, lang, page)
        else:
            result = self._search(self._client, q, sort, lang, page)
        with self._lock:
            self._cache[key] = (self._clock(), deepcopy(result))
        return result
