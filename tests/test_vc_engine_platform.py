"""Offline platform selection, installer and CPU warning acceptance tests."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from ai_voice import vc_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('system,machine,expected', [
    ('darwin', 'arm64', 'darwin-arm64'), ('win32', 'AMD64', 'win-x64'),
    ('win32', 'x86_64', 'win-x64'), ('linux', 'x86_64', None),
    ('darwin', 'x86_64', None), ('win32', 'ARM64', None),
])
def test_platform_key(system, machine, expected):
    assert vc_engine.platform_key(system, machine) == expected


@pytest.mark.parametrize('system,machine,key,requirements', [
    ('darwin', 'arm64', 'darwin-arm64', 'engine-requirements.lock'),
    ('win32', 'AMD64', 'win-x64', 'engine-requirements-win.lock'),
])
def test_manifest_has_both_platforms(tmp_path, monkeypatch, system, machine, key, requirements):
    monkeypatch.setattr(vc_engine.sys, 'platform', system)
    monkeypatch.setattr(vc_engine.platform, 'machine', lambda: machine)
    engine = vc_engine.Installer(root=tmp_path / 'vc-runtime')
    assert engine.state()['supported']
    assert engine.manifest['requirements'] == requirements
    assert engine.manifest['python'] == '3.10'
    if key == 'win-x64':
        assert engine.manifest['torch_index'] == 'https://download.pytorch.org/whl/cu128'
        assert engine.manifest['uv']['url'].endswith('uv-x86_64-pc-windows-msvc.zip')
        lock = (ROOT / 'vc_worker' / requirements).read_text()
        assert 'torch==2.8.0+cu128\n' in lock
        assert 'torchaudio==2.8.0+cu128\n' in lock


def legacy_manifest():
    return dict(version=1, python='3.10', requirements='engine-requirements.lock',
                uv=dict(size=1, url='https://github.com/uv', sha256='0' * 64), files=[])


def test_manifest_old_format_still_read(tmp_path, monkeypatch):
    monkeypatch.setattr(vc_engine.sys, 'platform', 'darwin')
    monkeypatch.setattr(vc_engine.platform, 'machine', lambda: 'arm64')
    manifest = legacy_manifest()
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    engine = vc_engine.Installer(root=tmp_path / 'vc-runtime', manifest=path)
    assert engine.manifest == manifest
    assert engine.state()['supported']


@pytest.mark.parametrize('system,machine', [('linux', 'arm64'), ('win32', 'ARM64'), ('win32', 'AMD64')])
def test_unsupported_install_gate_with_legacy_manifest(tmp_path, monkeypatch, system, machine):
    monkeypatch.setattr(vc_engine.sys, 'platform', system)
    monkeypatch.setattr(vc_engine.platform, 'machine', lambda: machine)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(legacy_manifest()))
    engine = vc_engine.Installer(root=tmp_path / 'vc-runtime', manifest=path)
    assert not engine.state()['supported']
    with pytest.raises(RuntimeError, match='unsupported'):
        engine.start()


def zip_bytes(name='uv-win/uv.exe'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(name, b'fake uv')
    return output.getvalue()


@pytest.mark.parametrize('system,machine,python_path,device', [
    ('win32', 'AMD64', 'venv/Scripts/python.exe', 'cpu'),
    ('win32', 'AMD64', 'venv/Scripts/python.exe', 'cuda'),
    ('darwin', 'arm64', 'venv/bin/python', 'mps'),
])
def test_install_uses_platform_python_and_smoke(tmp_path, monkeypatch, system, machine, python_path, device):
    monkeypatch.setattr(vc_engine.sys, 'platform', system)
    monkeypatch.setattr(vc_engine.platform, 'machine', lambda: machine)
    engine = vc_engine.Installer(root=tmp_path / 'vc-runtime')
    calls = []
    def download(client, entry):
        archive = engine.work / 'uv.zip'
        archive.write_bytes(zip_bytes())
        return archive
    monkeypatch.setattr(engine, '_download', download)
    monkeypatch.setattr(engine, '_free_bytes', lambda: 10**13)
    engine.manifest = dict(engine.manifest, files=[])
    if system == 'darwin':
        def extract(archive, destination, *args):
            destination.mkdir()
            (destination / 'uv').write_bytes(b'fake uv')
        monkeypatch.setattr(engine, '_extract', extract)
    def run(argv, env, code, timeout=None):
        calls.append(argv)
        if argv[1] == 'venv':
            python = engine.work / python_path
            python.parent.mkdir(parents=True)
            python.write_bytes(b'fake python')
            (engine.work / 'venv/pyvenv.cfg').write_text('home = ' + str(engine.work / 'python'))
        if code == 'smoke':
            # Execute the actual smoke expression against a fake torch module.
            torch = type('Torch', (), {'cuda': type('Cuda', (), {'is_available': staticmethod(lambda: device == 'cuda')}),
                                      'backends': type('Backends', (), {'mps': type('Mps', (), {'is_available': staticmethod(lambda: device == 'mps')})})})()
            monkeypatch.setitem(sys.modules, 'torch', torch)
            exec(argv[-1])
    monkeypatch.setattr(engine, '_subprocess', run)
    engine._run()
    assert engine.state()['status'] == 'idle', engine.state()
    assert engine.state()['installed']
    assert calls[1][4] == str(engine.work / python_path)
    assert calls[2][0] == str(engine.work / python_path)
    if system == 'win32':
        assert calls[0][0].endswith('uv.exe')
        assert '--extra-index-url' in calls[1]
        assert calls[1][calls[1].index('--extra-index-url') + 1] == 'https://download.pytorch.org/whl/cu128'
        assert calls[1][calls[1].index('--index-strategy') + 1] == 'unsafe-first-match'
        assert '--only-binary' not in calls[1]
        assert '--no-binary' not in calls[1]
        assert 'mps' not in calls[2][-1]
    else:
        assert '--only-binary' not in calls[1]
        assert '--no-binary' not in calls[1]
    assert str(engine.root / 'python') in (engine.root / 'venv/pyvenv.cfg').read_text()


@pytest.mark.parametrize('name', ['../outside.exe', 'C:/outside.exe', '..\\outside.exe'])
def test_windows_uv_zip_rejects_unsafe_paths(tmp_path, monkeypatch, name):
    monkeypatch.setattr(vc_engine.sys, 'platform', 'win32')
    monkeypatch.setattr(vc_engine.platform, 'machine', lambda: 'AMD64')
    engine = vc_engine.Installer(root=tmp_path / 'vc-runtime')
    archive = tmp_path / 'uv.zip'
    archive.write_bytes(zip_bytes(name))
    with pytest.raises(RuntimeError, match='disk'):
        engine._extract(archive, tmp_path / 'unpacked')


def test_windows_uv_hash_mismatch_never_extracts(tmp_path, monkeypatch):
    monkeypatch.setattr(vc_engine.sys, 'platform', 'win32')
    monkeypatch.setattr(vc_engine.platform, 'machine', lambda: 'AMD64')
    engine = vc_engine.Installer(root=tmp_path / 'vc-runtime')
    data = zip_bytes()
    target = engine.work / 'downloads/uv.part'
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    with pytest.raises(RuntimeError, match='checksum'):
        engine._download(None, dict(id='uv', size=len(data), sha256='0' * 64))
    assert not target.exists()


def test_cpu_banner_only_for_cpu_snapshot():
    # Run the real status handler with a minimal DOM; no browser, server or audio.
    source = (ROOT / 'macos/desktop/app.js').read_text()
    creation = next((line.strip() for line in source.splitlines() if 'const cpuWarning=el(' in line), '')
    handler = source.split('  function vcOnStatus(data) {', 1)[1].split('  async function vcAudioAction()', 1)[0]
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
let banner=null;
const el=(tag,cls,textContent)=>({tag,cls,textContent,setAttribute(name,value){this[name]=value;}});
const group={append(node){banner=node;}};
const document = {querySelectorAll: () => []};
const $ = selector => selector === '#vc-cpu-warning' ? banner : null;
let vcLastTrainState=null,vcTraining=null,vcTrainK=0;
const vcUpdateCheck=()=>{},vcSyncMeter=()=>{},vcUpdateText=()=>{},vcNotifyDone=()=>{};
let dictionary;
const t = key => dictionary[key];
const vcOnStatus = new Function('data', 'return null');
'''.replace("const vcOnStatus = new Function('data', 'return null');", 'function vcOnStatus(data) {' + handler)
    script += '\nfunction createBanner() {' + creation + '}\n'
    script += "\nfor (const lang of ['en','ru']) { dictionary=JSON.parse(fs.readFileSync('src/ai_voice/locales/'+lang+'.json','utf8')); createBanner(); assert.ok(banner); assert.equal(banner.hidden,true); assert.equal(banner.id,'vc-cpu-warning'); assert.equal(banner.role,'status'); for (const device of ['cpu','cuda','mps',null,undefined,'cpu',null]) { vcOnStatus({vc:{device}}); assert.equal(banner.hidden,device!=='cpu'); if(device==='cpu') { assert.equal(banner.textContent,dictionary['vc.cpu_warning']); assert.ok(banner.textContent); } } }"
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def manifest_script():
    spec = importlib.util.spec_from_file_location('vc_manifest', ROOT / 'scripts/vc_manifest.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not (ROOT / 'scripts/vc_manifest.py').exists(),
    reason="scripts/vc_manifest.py is absent from the public export",
)
def test_manifest_builder_keeps_both_platforms(tmp_path, monkeypatch):
    module = manifest_script()
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'MANIFEST', tmp_path / 'manifest.json')
    monkeypatch.setattr(module, 'freeze', lambda: None)
    monkeypatch.setattr(module, 'weights', lambda client: [])
    monkeypatch.setattr(module, 'sources', lambda client: [])
    monkeypatch.setattr(module, 'report', lambda *args: None)
    monkeypatch.setattr(module, 'uv', lambda client, key='darwin-arm64': dict(version='0.12.23', size=1, url=key))
    module.build(None)
    manifest = json.loads(module.MANIFEST.read_text())
    assert manifest['platforms']['darwin-arm64']['requirements'] == 'engine-requirements.lock'
    assert manifest['platforms']['win-x64']['requirements'] == 'engine-requirements-win.lock'
    assert manifest['platforms']['win-x64']['uv']['url'] == 'win-x64'


@pytest.mark.skipif(
    not (ROOT / 'scripts/vc_manifest.py').exists(),
    reason="scripts/vc_manifest.py is absent from the public export",
)
@pytest.mark.parametrize('old_format', [True, False])
def test_manifest_verifier_handles_both_formats(tmp_path, monkeypatch, old_format):
    from contextlib import contextmanager
    module = manifest_script()
    path = tmp_path / 'manifest.json'
    manifest = legacy_manifest()
    path.write_text(json.dumps(manifest if old_format else {'platforms': {
        'darwin-arm64': manifest, 'win-x64': dict(manifest, uv=dict(manifest['uv'], url='https://github.com/uv-win'))}}))
    monkeypatch.setattr(module, 'MANIFEST', path)
    requests = []
    @contextmanager
    def request(client, method, url):
        assert method == 'HEAD'
        requests.append(url)
        yield type('Response', (), {'status_code': 200})()
    monkeypatch.setattr(module, 'request', request)
    assert module.verify(None, False) == 0
    assert requests == (['https://github.com/uv'] if old_format else ['https://github.com/uv', 'https://github.com/uv-win'])
