"""Offline storage/import tests; fake model validator and synthetic WAV audio."""
import json
import math
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import wave
import zipfile

import pytest

from ai_voice.vc_store import ROOT, VcStore, VcStoreError


@pytest.fixture
def store(tmp_path):
    fake = tmp_path / 'python-fake'
    fake.write_text(f'#!{sys.executable}\nimport sys\nassert sys.argv[1] == "-c"\nassert "weights_only=True" in sys.argv[2]\nassert \'map_location="cpu"\' in sys.argv[2]\nsys.exit(0)\n')
    fake.chmod(0o755)
    return VcStore(tmp_path / 'voices', python_path=fake)


@pytest.fixture
def model(tmp_path):
    path = tmp_path / 'source.pth'
    path.write_bytes(b'fake checkpoint')
    return path


def archive(tmp_path, entries):
    path = tmp_path / 'model.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name, content in entries:
            output.writestr(name, content)
    return path


def empty(store):
    assert not store.root.exists() or list(store.root.iterdir()) == []


@pytest.mark.parametrize('name', ['../evil.pth', '/evil.pth', 'C:/evil.pth', r'..\evil.pth', 'nested/../../evil.pth'])
def test_zip_traversal(store, tmp_path, name):
    path = archive(tmp_path, [(name, b'x')])
    with pytest.raises(VcStoreError, match='unsafe'):
        store.create('Voice', model_path=path)
    empty(store)
    assert not (tmp_path / 'evil.pth').exists()


def test_zip_symlink(store, tmp_path):
    info = zipfile.ZipInfo('model.pth')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = archive(tmp_path, [(info, b'/outside')])
    with pytest.raises(VcStoreError, match='symlink'):
        store.create('Voice', model_path=path)
    empty(store)


@pytest.mark.parametrize('extension', ['.exe', '.wav', '.py'])
def test_zip_extension(store, tmp_path, extension):
    path = archive(tmp_path, [('model.pth', b'x'), ('file' + extension, b'x')])
    with pytest.raises(VcStoreError, match='Only'):
        store.create('Voice', model_path=path)
    empty(store)


@pytest.mark.parametrize('entries', [[], [('a.pth', b'a'), ('b.pth', b'b')],
                                     [('a.pth', b'a'), ('a.index', b'a'), ('b.index', b'b')]])
def test_zip_ambiguous(store, tmp_path, entries):
    with pytest.raises(VcStoreError, match='one .pth'):
        store.create('Voice', model_path=archive(tmp_path, entries))
    empty(store)


def test_zip_nested_valid(store, tmp_path):
    path = archive(tmp_path, [('folder/', b''), ('folder/a.pth', b'model'),
                              ('folder/a.index', b'index'), ('metadata.json', b'{}')])
    meta = store.create('  Голос  ', model_path=path)
    directory = store.root / meta['id']
    assert meta['name'] == 'Голос'
    assert (directory / 'model.pth').read_bytes() == b'model'
    assert (directory / 'model.index').read_bytes() == b'index'
    assert sorted(p.name for p in directory.iterdir()) == ['meta.json', 'model.index', 'model.pth']


@pytest.mark.parametrize('zip_input', [True, False])
def test_import_limit(store, tmp_path, zip_input):
    store.max_import_bytes = 10
    path = archive(tmp_path, [('a.pth', b'x' * 11)]) if zip_input else tmp_path / 'a.pth'
    if not zip_input:
        path.write_bytes(b'x' * 11)
    with pytest.raises(VcStoreError, match='limit'):
        store.create('Voice', model_path=path)
    empty(store)


def test_json_counts_toward_limit(store, tmp_path):
    store.max_import_bytes = 10
    path = archive(tmp_path, [('a.pth', b'12345'), ('a.json', b'123456')])
    with pytest.raises(VcStoreError, match='limit'):
        store.create('Voice', model_path=path)
    empty(store)


def test_limit_counts_reads_not_header(store, tmp_path, monkeypatch):
    path = archive(tmp_path, [('a.pth', b'x' * 20)])
    store.max_import_bytes = 10
    original = zipfile.ZipFile.infolist
    def understated(archive):
        infos = original(archive)
        for info in infos:
            info.file_size = 1
        return infos
    # Supply decompressed bytes independently of header, as a hostile stream.
    import io
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', understated)
    monkeypatch.setattr(zipfile.ZipFile, 'open', lambda *a, **k: io.BytesIO(b'x' * 20))
    with pytest.raises(VcStoreError, match='limit'):
        store.create('Voice', model_path=path)
    empty(store)


def test_pickle_rejection(store, model):
    import pickle
    model.write_bytes(pickle.dumps(os.system))
    Path(store.python_path).write_text(f'#!{sys.executable}\nimport sys\nsys.exit(1)\n')
    with pytest.raises(VcStoreError, match='pickle'):
        store.create('Voice', model_path=model)
    empty(store)


def test_model_timeout(store, model):
    Path(store.python_path).write_text(f'#!{sys.executable}\nimport time\ntime.sleep(5)\n')
    store.timeout = 0.05
    with pytest.raises(VcStoreError, match='Timed out'):
        store.create('Voice', model_path=model)
    empty(store)


def test_missing_validator(store, model):
    store.python_path = '/missing/python'
    with pytest.raises(VcStoreError, match='safely'):
        store.create('Voice', model_path=model)
    empty(store)


@pytest.mark.parametrize('name', ['', '   ', 'x' * 41, None, 42])
def test_invalid_name(store, model, name):
    with pytest.raises(VcStoreError, match='name'):
        store.create(name, model_path=model)
    empty(store)


@pytest.mark.parametrize('params', [{'pitch_shift': 13}, {'pitch_shift': -13}, {'pitch_shift': 1.0},
                                   {'pitch_shift': True}, {'index_rate': -0.1}, {'index_rate': 1.1},
                                   {'index_rate': float('nan')}, {'index_rate': float('inf')},
                                   {'index_rate': True}, {'index_rate': '0.5'}])
def test_invalid_params(store, model, params):
    with pytest.raises(VcStoreError):
        store.create('Voice', model_path=model, **params)
    empty(store)
    meta = store.create('Voice', model_path=model)
    with pytest.raises(VcStoreError):
        store.update_params(meta['id'], **params)
    assert store.get(meta['id']) == meta


@pytest.mark.parametrize('voice_id', ['../outside', '/outside', '..', '.', r'a\b', 'a/b', '', None])
def test_id_traversal(store, voice_id):
    for operation in [lambda: store.get(voice_id), lambda: store.delete(voice_id),
                      lambda: store.rename(voice_id, 'Voice'), lambda: store.update_params(voice_id)]:
        with pytest.raises(VcStoreError, match='ID'):
            operation()


def test_symlink_voice(store, tmp_path):
    store.root.mkdir()
    (store.root / 'link').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(VcStoreError, match='path'):
        store.delete('link')
    assert tmp_path.exists()


def test_crud(store, model):
    assert store.list() == []
    meta = store.create('  ' + 'я' * 40 + '  ', model_path=model)
    assert len(meta['id']) == 32 and meta['id'] != meta['name']
    assert meta['kind'] == 'imported' and meta['status'] == 'ready'
    assert store.list() == [meta]
    assert store.rename(meta['id'], ' New ')['name'] == 'New'
    assert store.update_params(meta['id'], pitch_shift=-12, index_rate=0)['index_rate'] == 0
    assert store.update_params(meta['id'], pitch_shift=12, index_rate=1)['pitch_shift'] == 12
    with pytest.raises(VcStoreError):
        store.rename(meta['id'], ' ')
    store.delete(meta['id'])
    assert store.list() == [] and model.exists()


def test_atomic_meta(store, model, monkeypatch):
    meta = store.create('Original', model_path=model)
    target = store.root / meta['id'] / 'meta.json'
    original_bytes = target.read_bytes()
    real_replace = os.replace
    def inspect(source, destination):
        assert Path(source).parent == target.parent
        assert json.loads(Path(source).read_text())['name'] == 'Changed'
        assert target.read_bytes() == original_bytes
        real_replace(source, destination)
    monkeypatch.setattr(os, 'replace', inspect)
    store.rename(meta['id'], 'Changed')
    assert store.get(meta['id'])['name'] == 'Changed'
    before = target.read_bytes()
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        store.rename(meta['id'], 'Lost')
    assert target.read_bytes() == before
    assert list(target.parent.glob('.meta-*')) == []


def test_create_disk_failure_cleans(store, model, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(VcStoreError):
        store.create('Voice', model_path=model)
    empty(store)


def synthetic(path, seconds, *, silence=0):
    rate = 16000
    tone = b''.join(struct.pack('<h', int(8000 * math.sin(2 * math.pi * 180 * i / rate))) for i in range(rate))
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b'\0\0' * int(silence * rate))
        for _ in range(seconds):
            output.writeframesraw(tone)
        output.writeframes(b'\0\0' * int(silence * rate))
    return path


@pytest.fixture
def audio_store(store):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg and Path('/opt/homebrew/bin/ffmpeg').exists():
        ffmpeg = '/opt/homebrew/bin/ffmpeg'
    if not ffmpeg:
        pytest.skip('ffmpeg не установлен')
    store.ffmpeg_path = ffmpeg
    return store


def test_short_audio(audio_store, tmp_path):
    source = synthetic(tmp_path / 'short.wav', 5, silence=2)
    with pytest.raises(VcStoreError, match='10 seconds'):
        audio_store.create('Voice', kind='zeroshot', audio_paths=[source])
    empty(audio_store)
    assert source.exists()


@pytest.mark.parametrize('seconds', [11, 35, 181])
def test_audio_preparation(audio_store, tmp_path, seconds):
    source = synthetic(tmp_path / 'source.wav', seconds, silence=2)
    meta = audio_store.create('Voice', kind='zeroshot', audio_paths=[source])
    assert seconds - 0.1 <= meta['speech_seconds'] <= seconds + 0.25
    assert meta['can_train'] is (seconds >= 180)
    assert bool(meta['hint']) is (seconds < 180)
    directory = audio_store.root / meta['id']
    durations = []
    for path in (directory / 'dataset').glob('*.wav'):
        with wave.open(str(path)) as audio:
            assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 40000, 2)
            duration = audio.getnframes() / 40000
            assert 3 <= duration <= 10
            durations.append(duration)
    assert sum(durations) == pytest.approx(meta['speech_seconds'])
    with wave.open(str(directory / 'reference.wav')) as audio:
        assert audio.getnframes() / 40000 == pytest.approx(min(30, meta['speech_seconds']))
    assert sorted(p.name for p in directory.iterdir()) == ['dataset', 'meta.json', 'reference.wav']
    assert source.exists()


def test_audio_multiple_sources(audio_store, tmp_path):
    sources = [synthetic(tmp_path / f'{i}.wav', 6) for i in range(2)]
    meta = audio_store.create('Voice', kind='zeroshot', audio_paths=sources)
    assert 11.8 < meta['speech_seconds'] <= 12.2


def test_audio_ffmpeg_failure(store, tmp_path):
    source = tmp_path / 'invalid.wav'
    source.write_bytes(b'bad')
    store.ffmpeg_path = '/missing/ffmpeg'
    with pytest.raises(VcStoreError, match='ffmpeg'):
        store.create('Voice', kind='zeroshot', audio_paths=[source])
    empty(store)


def test_no_torch_import():
    code = 'import sys; import ai_voice.vc_store; assert "torch" not in sys.modules'
    subprocess.run([sys.executable, '-c', code], check=True, timeout=10,
                   env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})


def test_interior_silence_removed(audio_store, tmp_path):
    tone = synthetic(tmp_path / 'tone.wav', 6)
    source = tmp_path / 'gaps.wav'
    with wave.open(str(tone)) as audio:
        pcm = audio.readframes(audio.getnframes())
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm + b'\0\0' * 16000 * 4 + pcm)
    meta = audio_store.create('Voice', kind='zeroshot', audio_paths=[source])
    assert 11.8 < meta['speech_seconds'] < 12.3  # four-second gap excluded


def test_all_silence_rejected(audio_store, tmp_path):
    source = tmp_path / 'silence.wav'
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0' * (8000 * 4 * 12))
    with pytest.raises(VcStoreError, match='10 seconds'):
        audio_store.create('Voice', kind='zeroshot', audio_paths=[source])
    empty(audio_store)


def test_ffmpeg_timeout(store, tmp_path):
    fake = tmp_path / 'ffmpeg-fake'
    fake.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(5)\n')
    fake.chmod(0o755)
    store.ffmpeg_path = str(fake)
    store.timeout = 0.05
    with pytest.raises(VcStoreError, match='Timed out'):
        store.create('Voice', kind='zeroshot', audio_paths=[tmp_path / 'any.wav'])
    empty(store)


def test_live_defaults_do_not_rewrite_old_meta(store, model):
    voice = store.create('Imported', model_path=model)
    path = store._directory(voice['id']) / 'meta.json'
    meta = json.loads(path.read_text())
    meta.pop('block_ms')
    path.write_text(json.dumps(meta))
    before = path.read_bytes()
    assert store.get(voice['id'])['block_ms'] == 256
    assert path.read_bytes() == before
    meta['kind'] = 'zeroshot'
    path.write_text(json.dumps(meta))
    before = path.read_bytes()
    result = store.get(voice['id'])
    assert (result['block_ms'], result['diffusion_steps'], result['ref_seconds']) == (256, 4, 5)
    assert path.read_bytes() == before
    result = store.update_params(voice['id'], block_ms=1000, diffusion_steps=2, ref_seconds=15)
    assert (result['block_ms'], result['diffusion_steps'], result['ref_seconds']) == (1000, 2, 15)


@pytest.mark.parametrize('patch', [dict(block_ms=257), dict(block_ms=True), dict(block_ms=256.0),
    dict(diffusion_steps=1), dict(diffusion_steps=11), dict(diffusion_steps=4.0),
    dict(ref_seconds=2), dict(ref_seconds=16), dict(ref_seconds=True)])
def test_live_param_validation(store, model, patch):
    meta = store.create('Imported', model_path=model)
    path = store._directory(meta['id']) / 'meta.json'
    meta['kind'] = 'zeroshot'
    path.write_text(json.dumps(meta))
    with pytest.raises(VcStoreError):
        store.update_params(meta['id'], **patch)


@pytest.mark.parametrize('key', ['diffusion_steps', 'ref_seconds'])
def test_live_zeroshot_only(store, model, key):
    meta = store.create('Imported', model_path=model)
    with pytest.raises(VcStoreError, match='This parameter is only available for a quick voice'):
        store.update_params(meta['id'], **{key: 5})


def test_pending_moves_uploads_and_processing_stages(audio_store, tmp_path, monkeypatch):
    source = synthetic(tmp_path / 'upload.wav', 12)
    meta = audio_store.create_pending('Pending', [source])
    directory = audio_store._directory(meta['id'])
    assert not source.exists()
    assert meta['status'] == 'processing' and meta['stage'] == 'convert'
    assert list((directory / 'incoming').iterdir())
    observed = []
    original = audio_store._write
    def write(path, value):
        observed.append((value['status'], value.get('stage')))
        return original(path, value)
    monkeypatch.setattr(audio_store, '_write', write)
    ready = audio_store.process_audio(meta['id'])
    assert ('processing', 'slice') in observed
    assert ready['status'] == 'ready' and 'stage' not in ready
    assert ready['speech_seconds'] >= 10 and not ready['can_train']
    assert (directory / 'reference.wav').is_file()
    assert not (directory / 'incoming').exists()


@pytest.mark.parametrize('safe', [True, False])
def test_pending_error_cleanup(store, tmp_path, monkeypatch, safe):
    source = tmp_path / 'upload.wav'
    source.write_bytes(b'invalid')
    meta = store.create_pending('Pending', [source])
    def fail(*args, **kwargs):
        raise VcStoreError('Ошибка ffmpeg') if safe else RuntimeError('/private/secret')
    monkeypatch.setattr(store, '_audio', fail)
    result = store.process_audio(meta['id'])
    assert result['status'] == 'error' and 'stage' not in result
    assert result['error'] == 'Ошибка ffmpeg' if safe else '/private' not in result['error']
    assert not (store._directory(meta['id']) / 'incoming').exists()
    store.delete(meta['id'])


def test_pending_interrupted_on_restart(store, tmp_path):
    source = tmp_path / 'upload.wav'
    source.write_bytes(b'audio')
    meta = store.create_pending('Pending', [source])
    restarted = VcStore(store.root)
    result = restarted.get(meta['id'])
    assert result['status'] == 'error'
    assert result['error'] == 'Processing was interrupted. Delete the voice and add it again.'
    assert not (store._directory(meta['id']) / 'incoming').exists()
