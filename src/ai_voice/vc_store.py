"""Local VC assets. No torch, network, microphone, or training in this process.

create(name, kind='imported', model_path=..., index_path=...) imports a model;
create(name, kind='zeroshot', audio_paths=[...]) prepares a reference and dataset.
Returned metadata includes speech_seconds, can_train and hint for audio voices.
Caller-owned input files are never removed; uploaded copies are not retained here.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import wave
import zipfile

from .i18n import t
from .paths import DATA, ROOT, VC_PYTHON


class VcStoreError(Exception):
    """A user-facing Russian validation or preparation error."""


class VcStore:
    def __init__(self, root=None, *, python_path=None, ffmpeg_path=None,
                 max_import_bytes=1024 ** 3, timeout=120):
        self.root = Path(root if root is not None else DATA / 'vc-voices').resolve()
        self.python_path = str(python_path or VC_PYTHON)
        self.ffmpeg_path = str(ffmpeg_path or (
            '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').is_file()
            else shutil.which('ffmpeg') or 'ffmpeg'))
        if type(max_import_bytes) is not int or max_import_bytes <= 0:
            raise VcStoreError(t("errors.vc_import_limit_invalid"))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise VcStoreError(t("errors.vc_timeout_invalid"))
        self.max_import_bytes = max_import_bytes
        self.timeout = timeout
        self._lock = threading.RLock()
        self._deleting = set()
        self._before_delete = None
        for meta in self.list():
            if meta.get('status') == 'processing':
                directory = self._directory(meta['id'])
                meta.update(status='error', error=t("errors.vc_processing_interrupted"))
                meta.pop('stage', None)
                self._write(directory, meta)
                shutil.rmtree(directory / 'incoming', ignore_errors=True)

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise VcStoreError(t("errors.vc_name_invalid"))
        return name.strip()

    @staticmethod
    def _params(pitch_shift, index_rate):
        if type(pitch_shift) is not int or not -12 <= pitch_shift <= 12:
            raise VcStoreError(t("errors.vc_pitch_invalid"))
        if isinstance(index_rate, bool) or not isinstance(index_rate, (int, float)) or not math.isfinite(index_rate) or not 0 <= index_rate <= 1:
            raise VcStoreError(t("errors.vc_similarity_invalid"))

    def _directory(self, voice_id):
        if not isinstance(voice_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', voice_id):
            raise VcStoreError(t("errors.vc_id_invalid"))
        directory = self.root / voice_id
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise VcStoreError(t("errors.vc_path_invalid"))
        return directory

    def _write(self, directory, meta):
        target = directory / 'meta.json'
        fd, temporary = tempfile.mkstemp(prefix='.meta-', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(meta, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _run(self, args, *, model=False):
        try:
            subprocess.run(args, shell=False, check=True, timeout=self.timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            message = (t("errors.vc_model_unsafe")
                       if model else t("errors.vc_audio_prepare"))
            if isinstance(exc, subprocess.TimeoutExpired):
                message += " " + t("errors.vc_timeout")
            raise VcStoreError(message) from exc

    def _copy_limited(self, source, target, total):
        # Count actual decompressed bytes, including ignored JSON archive members.
        with target.open('xb') if target is not None else tempfile.TemporaryFile() as output:
            while True:
                block = source.read(min(65536, self.max_import_bytes - total + 1))
                if not block:
                    return total
                total += len(block)
                if total > self.max_import_bytes:
                    raise VcStoreError(t("errors.vc_unpacked_limit"))
                output.write(block)

    def _import(self, directory, model_path, index_path):
        path = Path(model_path)
        total = 0
        if path.suffix.lower() == '.zip':
            if index_path is not None:
                raise VcStoreError(t("errors.vc_zip_index"))
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                models, indexes = [], []
                for info in members:
                    name = info.filename
                    parts = PurePosixPath(name).parts
                    mode = info.external_attr >> 16
                    if (not name or '\\' in name or name.startswith('/') or '..' in parts
                            or re.match(r'^[A-Za-z]:', name) or stat.S_ISLNK(mode)):
                        raise VcStoreError(t("errors.vc_zip_unsafe"))
                    if info.is_dir():
                        continue
                    extension = PurePosixPath(name).suffix.lower()
                    if extension not in ('.pth', '.index', '.json'):
                        raise VcStoreError(t("errors.vc_zip_extensions"))
                    if extension == '.pth':
                        models.append(info)
                    elif extension == '.index':
                        indexes.append(info)
                if len(models) != 1 or len(indexes) > 1:
                    raise VcStoreError(t("errors.vc_zip_models"))
                for info in members:
                    if info.is_dir():
                        continue
                    target = (directory / 'model.pth' if info is models[0] else
                              directory / 'model.index' if indexes and info is indexes[0] else None)
                    with archive.open(info) as stream:
                        total = self._copy_limited(stream, target, total)
        else:
            if path.suffix.lower() != '.pth':
                raise VcStoreError(t("errors.vc_model_required"))
            files = [(path, directory / 'model.pth')]
            if index_path is not None:
                index = Path(index_path)
                if index.suffix.lower() != '.index':
                    raise VcStoreError(t("errors.vc_index_extension"))
                files.append((index, directory / 'model.index'))
            for source, target in files:
                with source.open('rb') as stream:
                    total = self._copy_limited(stream, target, total)
        self._run([self.python_path, '-c',
                   'import sys, torch; torch.load(sys.argv[1], map_location="cpu", weights_only=True)',
                   str(directory / 'model.pth')], model=True)

    @staticmethod
    def _wav_writer(path):
        output = wave.open(str(path), 'wb')
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(40000)
        return output

    def _audio(self, directory, paths, before_slice=None):
        if not paths:
            raise VcStoreError(t("errors.vc_audio_required"))
        dataset = directory / 'dataset'
        dataset.mkdir()
        with tempfile.TemporaryDirectory(prefix='.audio-', dir=directory) as temporary:
            merged = Path(temporary) / 'speech.wav'
            frames = 0
            with self._wav_writer(merged) as output:
                for number, source in enumerate(paths):
                    prepared = Path(temporary) / f'{number}.wav'
                    self._run([self.ffmpeg_path, '-nostdin', '-hide_banner', '-loglevel', 'error',
                               '-protocol_whitelist', 'file,pipe', '-i', str(Path(source).resolve()),
                               '-vn', '-ac', '1', '-af',
                               'loudnorm=I=-20:TP=-2:LRA=11,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-40dB:stop_periods=-1:stop_duration=0.2:stop_threshold=-40dB',
                               '-ar', '40000', '-c:a', 'pcm_s16le', str(prepared)])
                    with wave.open(str(prepared), 'rb') as audio:
                        frames += audio.getnframes()
                        while block := audio.readframes(400000):
                            output.writeframesraw(block)
                    prepared.unlink()
            seconds = frames / 40000
            if seconds < 10:
                raise VcStoreError(t("errors.vc_speech_minimum"))
            if before_slice is not None:
                before_slice()
            count = math.ceil(frames / 400000)
            base, remainder = divmod(frames, count)
            with wave.open(str(merged), 'rb') as audio:
                for number in range(count):
                    with self._wav_writer(dataset / f'{number:05d}.wav') as chunk:
                        chunk.writeframes(audio.readframes(base + (number < remainder)))
            with wave.open(str(merged), 'rb') as audio, self._wav_writer(directory / 'reference.wav') as reference:
                reference.writeframes(audio.readframes(min(frames, 1200000)))
        return {'speech_seconds': seconds, 'can_train': seconds >= 180,
                'hint': '' if seconds >= 180 else t("status.vc_training_hint")}

    def create_pending(self, name, audio_paths):
        name = self._name(name)
        if not audio_paths:
            raise VcStoreError(t("errors.vc_audio_required"))
        with self._lock:
            directory = self.root / uuid.uuid4().hex
            incoming = directory / 'incoming'
            incoming.mkdir(parents=True)
            try:
                for number, source in enumerate(audio_paths):
                    source = Path(source)
                    shutil.move(str(source), incoming / (str(number) + source.suffix))
                meta = dict(id=directory.name, name=name, kind='zeroshot',
                            status='processing', stage='convert',
                            created_at=datetime.now(timezone.utc).isoformat(),
                            sample_rate=40000, pitch_shift=0, index_rate=.75,
                            block_ms=256, diffusion_steps=4, ref_seconds=5)
                self._write(directory, meta)
                return meta
            except Exception as exc:
                shutil.rmtree(directory, ignore_errors=True)
                raise VcStoreError(t("errors.vc_create_failed")) from exc

    def process_audio(self, voice_id):
        directory = self._directory(voice_id)
        with self._lock:
            meta = self.get(voice_id)
            if meta.get('status') != 'processing':
                return meta
            identity = directory.stat().st_ino

        def update(**values):
            with self._lock:
                if directory.stat().st_ino != identity:
                    raise VcStoreError(t("errors.vc_voice_deleted"))
                current = self.get(voice_id)
                current.update(values)
                if current['status'] != 'processing':
                    current.pop('stage', None)
                self._write(directory, current)
                return current

        try:
            result = self._audio(directory, sorted((directory / 'incoming').iterdir()),
                                 before_slice=lambda: update(stage='slice'))
            return update(status='ready', **result)
        except Exception as exc:
            try:
                return update(status='error', error=str(exc) if isinstance(exc, VcStoreError)
                              else t("errors.vc_audio_process"))
            except (OSError, VcStoreError):
                return None  # Deleted while processing; never recreate the voice.
        finally:
            if directory.exists() and directory.stat().st_ino == identity:
                shutil.rmtree(directory / 'incoming', ignore_errors=True)

    def create(self, name, *, kind='imported', model_path=None, index_path=None,
               audio_paths=None, pitch_shift=0, index_rate=0.75, source=None):
        name = self._name(name)
        self._params(pitch_shift, index_rate)
        if kind not in ('imported', 'zeroshot', 'trained'):
            raise VcStoreError(t("errors.vc_kind_unknown"))
        if kind == 'zeroshot':
            if model_path is not None or index_path is not None:
                raise VcStoreError(t("errors.vc_quick_audio"))
        elif model_path is None or audio_paths is not None:
            raise VcStoreError(t("errors.vc_trained_model"))
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            directory = self.root / uuid.uuid4().hex
            directory.mkdir()
            try:
                meta = {'id': directory.name, 'name': name, 'kind': kind, 'status': 'ready',
                        'created_at': datetime.now(timezone.utc).isoformat(), 'sample_rate': 40000,
                        'pitch_shift': pitch_shift, 'index_rate': index_rate, 'block_ms': 256}
                if source is not None:
                    meta['source'] = dict(source)
                if kind == 'zeroshot':
                    meta.update(diffusion_steps=4, ref_seconds=5)
                if kind == 'zeroshot':
                    meta.update(self._audio(directory, audio_paths))
                else:
                    self._import(directory, model_path, index_path)
                self._write(directory, meta)
                return meta
            except Exception as exc:
                shutil.rmtree(directory)
                if isinstance(exc, VcStoreError):
                    raise
                raise VcStoreError(t("errors.vc_create_failed")) from exc

    def get(self, voice_id):
        with self._lock:
            directory = self._directory(voice_id)
            path = directory / 'meta.json'
            if path.is_symlink():
                raise VcStoreError(t("errors.vc_metadata_path"))
            try:
                meta = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(meta, dict) or meta.get('id') != voice_id:
                    raise ValueError('id')
                meta.setdefault('block_ms', 256)
                if meta.get('kind') == 'zeroshot':
                    meta.setdefault('diffusion_steps', 4)
                    meta.setdefault('ref_seconds', 5)
                return meta
            except (OSError, ValueError) as exc:
                raise VcStoreError(t("errors.vc_metadata_missing")) from exc

    def list(self):
        with self._lock:
            if not self.root.exists():
                return []
            return [self.get(path.name) for path in sorted(self.root.iterdir())
                    if path.is_dir() and not path.name.startswith('.')]

    def rename(self, voice_id, name):
        name = self._name(name)
        with self._lock:
            meta = self.get(voice_id)
            meta['name'] = name
            self._write(self._directory(voice_id), meta)
            return meta

    def delete(self, voice_id):
        with self._lock:
            self.get(voice_id)
            self._deleting.add(voice_id)
        try:
            # Do not hold the store lock while waiting for the training reader.
            if self._before_delete is not None:
                self._before_delete(voice_id)
            with self._lock:
                shutil.rmtree(self._directory(voice_id))
        finally:
            with self._lock:
                self._deleting.discard(voice_id)

    def mark_trained(self, voice_id):
        """Publish metadata only after a nonempty model has been installed."""
        with self._lock:
            meta = self.get(voice_id)
            directory = self._directory(voice_id)
            model = directory / 'model.pth'
            if model.is_symlink() or not model.is_file() or not model.stat().st_size:
                raise VcStoreError(t("errors.vc_trained_missing"))
            meta.update(kind='trained', status='ready', sample_rate=48000)
            self._write(directory, meta)
            return meta

    def update_params(self, voice_id, *, pitch_shift=None, index_rate=None, block_ms=None, diffusion_steps=None, ref_seconds=None):
        with self._lock:
            meta = self.get(voice_id)
            pitch = meta['pitch_shift'] if pitch_shift is None else pitch_shift
            rate = meta['index_rate'] if index_rate is None else index_rate
            self._params(pitch, rate)
            if meta['kind'] != 'zeroshot' and (diffusion_steps is not None or ref_seconds is not None):
                raise VcStoreError(t("errors.vc_quick_parameter"))
            for key, value, allowed in (
                    ('block_ms', block_ms, (160, 256, 384, 500, 750, 1000)),
                    ('diffusion_steps', diffusion_steps, range(2, 11)),
                    ('ref_seconds', ref_seconds, range(3, 16))):
                if value is not None:
                    if type(value) is not int or value not in allowed:
                        raise VcStoreError(t("errors.vc_parameter_invalid", name=key))
                    meta[key] = value
            meta.update(pitch_shift=pitch, index_rate=rate)
            self._write(self._directory(voice_id), meta)
            return meta
