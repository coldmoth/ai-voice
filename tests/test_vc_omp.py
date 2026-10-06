"""Offline libomp relinking and worker launch coverage."""
import os

import pytest

from ai_voice.vc_control import VcWorker
from ai_voice.vc_omp import dedupe_libomp
from test_vc_api import _FakePopen


def _venv(tmp_path, torch=True):
    python = tmp_path / 'venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    site = tmp_path / 'venv/lib/python3.10/site-packages'
    files = {'faiss/.dylibs/libomp.dylib': b'faiss',
             'sklearn/.dylibs/libomp.dylib': b'sk'}
    if torch:
        files['torch/lib/libomp.dylib'] = b'torch'
    for name, content in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return python, site


def test_dedupe_libomp_relative_links_and_idempotence(tmp_path):
    python, site = _venv(tmp_path)
    paths = ['faiss/.dylibs/libomp.dylib', 'sklearn/.dylibs/libomp.dylib']
    assert dedupe_libomp(python) == paths
    for name in paths:
        path = site / name
        assert path.is_symlink()
        assert path.read_bytes() == b'torch'
        assert not os.path.isabs(os.readlink(path))
    assert dedupe_libomp(python) == []


def test_dedupe_libomp_without_torch_leaves_files(tmp_path):
    python, site = _venv(tmp_path, torch=False)
    assert dedupe_libomp(python) == []
    path = site / 'faiss/.dylibs/libomp.dylib'
    assert not path.is_symlink()
    assert path.read_bytes() == b'faiss'


@pytest.mark.parametrize('fails', [False, True])
def test_worker_dedupes_libomp_with_fallback_only_on_error(tmp_path, monkeypatch, fails):
    python, site = _venv(tmp_path)
    monkeypatch.setattr('ai_voice.vc_control.DATA', tmp_path)
    monkeypatch.delenv('KMP_DUPLICATE_LIB_OK', raising=False)
    if fails:
        def fail(_):
            raise OSError('read-only venv')
        monkeypatch.setattr('ai_voice.vc_control.dedupe_libomp', fail)
    popen = _FakePopen([{'event': 'status', 'state': 'loaded'},
                        {'event': 'status', 'state': 'running'}])
    worker = VcWorker(popen=popen, python_path=python)
    directory = tmp_path / 'voice'
    directory.mkdir()
    (directory / 'reference.wav').write_bytes(b'RIFF')
    try:
        worker.start({'id': 'v1', 'kind': 'zeroshot'}, directory, {}, {})
        log = (tmp_path / 'logs/vc_worker.log').read_text()
        if fails:
            assert popen.kwargs['env']['KMP_DUPLICATE_LIB_OK'] == 'TRUE'
            assert 'libomp dedupe failed: read-only venv; KMP_DUPLICATE_LIB_OK=TRUE' in log
        else:
            assert 'KMP_DUPLICATE_LIB_OK' not in popen.kwargs['env']
            assert (site / 'faiss/.dylibs/libomp.dylib').is_symlink()
            assert (site / 'sklearn/.dylibs/libomp.dylib').is_symlink()
            assert 'libomp linked to torch: faiss/.dylibs/libomp.dylib, sklearn/.dylibs/libomp.dylib' in log
    finally:
        worker.stop()
