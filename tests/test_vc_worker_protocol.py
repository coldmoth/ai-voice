import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vc_worker.protocol import Service, RSS_LIMIT, validate_params
from vc_worker.runtime import Runtime, latest
from vc_worker.streaming import Config


class Engine:
    def convert(self, audio, sr, params):
        return audio, sr


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
    def start(self):
        pass
    def stop(self):
        pass
    def close(self):
        self.closed = True


class Audio:
    def __init__(self):
        self.streams = []
    def create(self, **kwargs):
        stream = FakeStream(**kwargs)
        self.streams.append(stream)
        return stream
    InputStream = OutputStream = create


def service():
    output = io.StringIO()
    audio = Audio()
    def factory(*args):
        return Runtime(*args, sd=audio)
    return Service(output, loader=lambda voice: Engine(), runtime_factory=factory, rss=lambda: 100), audio


def events(s):
    return [json.loads(line) for line in s.output.getvalue().splitlines()]


def load(s):
    s.command({'cmd': 'load', 'voice': {'kind': 'trained', 'model': 'fake'},
               'config': {'sample_rate': 8000}})


def test_gate_parameter_defaults_load_and_live_patch():
    s, _ = service()
    assert s.params['gate_enabled'] is False and s.params['gate_db'] == -45
    assert validate_params({}, {})['gate_enabled'] is False
    for db in (-70, -10):
        assert validate_params({'gate_enabled': True, 'gate_db': db}, {})['gate_db'] == db
    s.command({'cmd': 'load', 'voice': {'kind': 'trained', 'model': 'fake'},
               'params': {'gate_enabled': True, 'gate_db': -50}})
    s.command({'cmd': 'start', 'devices': {}})
    runtime = s.runtime
    try:
        assert runtime.params['gate_enabled'] is True and runtime.params['gate_db'] == -50
        s.command({'cmd': 'params', 'params': {'gate_enabled': False, 'gate_db': -40}})
        assert runtime.params['gate_enabled'] is False and runtime.params['gate_db'] == -40
    finally:
        runtime.close()
        runtime.thread.join(timeout=1)


@pytest.mark.parametrize('patch', [{'gate_enabled': 1}, {'gate_enabled': 'yes'},
    {'gate_db': True}, {'gate_db': float('nan')}, {'gate_db': float('inf')},
    {'gate_db': -71}, {'gate_db': -9}, {'gate_db': '-45'}])
def test_invalid_gate_parameter(patch):
    with pytest.raises(ValueError):
        validate_params(patch, {})


def test_protocol_bad_json_recovery_and_no_torch():
    s, audio = service()
    s.run(io.StringIO('bad\n[]\n{"cmd":"unknown"}\n{"cmd":"status"}\n{"cmd":"quit"}\n'))
    assert len([e for e in events(s) if e['event'] == 'error']) == 3
    assert any(e['event'] == 'status' and e['state'] == 'idle' for e in events(s))
    assert not audio.streams
    result = subprocess.run([sys.executable, '-c',
        "import vc_worker.protocol,sys,os; assert 'torch' not in sys.modules; assert os.environ['SYSTEM_VERSION_COMPAT']=='0'"],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'SYSTEM_VERSION_COMPAT': '1'}, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_entrypoint_stdout_is_json_only():
    result = subprocess.run([sys.executable, '-m', 'vc_worker'], input='{"cmd":"status"}\n{"cmd":"quit"}\n',
                            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=5)
    assert result.returncode == 0
    assert all(json.loads(line)['event'] in ('status', 'stopped') for line in result.stdout.splitlines())


def test_start_params_monitor_and_stop():
    s, audio = service()
    load(s)
    s.command({'cmd': 'start', 'devices': {'output_device': 'AI Voice', 'monitor_enabled': True,
                                          'monitor_device': 'Headphones'}})
    runtime = s.runtime
    assert len(audio.streams) == 3
    s.command({'cmd': 'params', 'params': {'pitch_shift': 4, 'monitor_gain_db': -6}})
    assert runtime.params['pitch_shift'] == 4
    c = runtime.config
    block = np.ones((c.hop, 1), np.float32) * .2
    runtime.capture(block, c.hop, None, None)
    deadline = time.monotonic() + 1
    while runtime.output.empty() and time.monotonic() < deadline:
        time.sleep(.005)
    out = np.zeros_like(block)
    monitor = np.zeros_like(block)
    runtime.render('primary', out, c.hop, None, None)
    runtime.render('monitor', monitor, c.hop, None, None)
    assert out.max() == pytest.approx(.2)
    assert monitor.max() == pytest.approx(.2 * 10 ** (-6 / 20))
    # Drive an output callback so stop can drain the faded tail.
    def drain():
        while not runtime.stopping:
            time.sleep(.005)
        runtime.render('primary', out, c.hop, None, None)
        runtime.render('monitor', monitor, c.hop, None, None)
    thread = threading.Thread(target=drain)
    thread.start()
    s.command({'cmd': 'stop'})
    thread.join(timeout=1)
    assert runtime.drained.is_set()
    assert np.allclose(out[:c.fade, 0], .2 * np.linspace(1, 0, c.fade), atol=1e-6)
    assert all(stream.closed for stream in audio.streams)
    assert not runtime.thread.is_alive()
    assert events(s)[-1]['event'] == 'stopped'
    s.command({'cmd': 'stop'})


@pytest.mark.parametrize('patch', [{'pitch_shift': 13}, {'pitch_shift': .5}, {'index_rate': -1},
                                   {'output_gain_db': float('nan')}, {'wrong': 1}, {'index_rate': True}])
def test_params_transactional(patch):
    s, _ = service()
    previous = dict(s.params)
    with pytest.raises(ValueError):
        s.command({'cmd': 'params', 'params': patch})
    assert s.params == previous


def test_load_failure_releases_previous_model_and_stdout_diagnostics():
    s, _ = service()
    load(s)
    def fail(voice):
        print('upstream diagnostic')
        raise ValueError('bad weights')
    s.loader = fail
    s.run(io.StringIO('{"cmd":"load","voice":{}}\n'))
    assert s.engine is None
    assert 'upstream diagnostic' not in s.output.getvalue()
    assert any(e['event'] == 'error' for e in events(s))


def test_budget_load_and_idle_watchdog():
    s, _ = service()
    usage = [100]
    s.rss = lambda: usage[0]
    def loader(voice):
        usage[0] = RSS_LIMIT + 1
        return Engine()
    s.loader = loader
    with pytest.raises(MemoryError):
        load(s)
    assert s.engine is None
    assert [(e['event'], e.get('reason', e.get('code'))) for e in events(s)][-2:] == [
        ('error', 'rss_budget'), ('stopped', 'rss_budget')]
    s, _ = service()
    s.rss = lambda: RSS_LIMIT + 1
    t = threading.Thread(target=s.watch)
    t.start()
    t.join(timeout=1)
    assert not t.is_alive()
    assert s.exceeded.is_set()


def test_budget_releases_streams_and_requests_process_exit():
    s, audio = service()
    load(s)
    s.command({'cmd': 'start'})
    runtime = s.runtime
    exits = []
    s.hard_exit = exits.append
    s.rss = lambda: RSS_LIMIT + 1
    with pytest.raises(MemoryError):
        s.budget()
    assert exits == [3]
    assert runtime.halt.is_set()
    assert all(stream.closed for stream in audio.streams)
    runtime.thread.join(timeout=1)


def test_queues_keep_latest_and_output_underflow_silent():
    runtime = Runtime(Engine(), Config(sample_rate=8000), {}, {}, lambda *a, **k: None, lambda: None)
    c = runtime.config
    for i in range(10):
        runtime.capture(np.full((c.hop, 1), i), c.hop, None, None)
    assert runtime.input.qsize() == 1
    assert runtime.input.get()[0] == 9
    assert runtime.dropped == 9
    output = np.ones((c.hop, 1), np.float32)
    runtime.render('primary', output, c.hop, None, None)
    assert not output.any()
    for i in range(10):
        latest(runtime.output, np.ones(c.hop) * i)
    assert runtime.output.qsize() == 2
    assert runtime.output.get()[0] == 8


def test_invalid_start_monitor_and_partial_open_cleanup():
    s, audio = service()
    with pytest.raises(ValueError):
        s.command({'cmd': 'start'})
    load(s)
    with pytest.raises(ValueError):
        s.command({'cmd': 'start', 'devices': {'output_device': 'same', 'monitor_enabled': True,
                                             'monitor_device': 'same'}})
    assert not audio.streams
    class Broken(Audio):
        def InputStream(self, **kwargs):
            raise RuntimeError('device missing')
    broken = Broken()
    r = Runtime(Engine(), Config(), {}, {}, lambda *a, **k: None, lambda: None, sd=broken)
    with pytest.raises(RuntimeError):
        r.start()
    assert all(stream.closed for stream in broken.streams)


def test_worker_discards_capture_arriving_during_late_step():
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def convert(self, audio, sr, params):
            entered.set()
            assert release.wait(1)
            return audio, sr
    audio = Audio()
    r = Runtime(Slow(), Config(sample_rate=8000), {}, {}, lambda *a, **k: None, lambda: None, sd=audio)
    ticks = iter([0, .4])
    r.processor.clock = lambda: next(ticks)
    r.start()
    c = r.config
    r.capture(np.ones((c.hop, 1)), c.hop, None, None)
    assert entered.wait(1)
    for _ in range(5):
        r.capture(np.ones((c.hop, 1)), c.hop, None, None)
    release.set()
    limit = time.monotonic() + 1
    while r.output.empty() and time.monotonic() < limit:
        time.sleep(.005)
    assert not r.output.get_nowait().any()
    assert r.input.empty()
    assert r.dropped + r.processor.dropped_blocks == 6
    r.halt.set()
    r.thread.join(timeout=1)
    r.close()


def test_watchdog_catches_budget_during_blocked_load():
    s, _ = service()
    usage = [100]
    s.rss = lambda: usage[0]
    entered, release = threading.Event(), threading.Event()
    def loader(voice):
        entered.set()
        assert release.wait(1)
        return Engine()
    s.loader = loader
    worker = threading.Thread(target=s.run, args=(io.StringIO('{"cmd":"load","voice":{}}\n'),))
    worker.start()
    assert entered.wait(1)
    usage[0] = RSS_LIMIT + 1
    assert s.exceeded.wait(1)
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert s.engine is None
    assert any(e.get('code') == 'rss_budget' for e in events(s))


def test_render_variable_callback_frames_and_gains():
    r = Runtime(Engine(), Config(sample_rate=8000), {'output_gain_db': 12}, {},
                lambda *a, **k: None, lambda: None)
    latest(r.output, np.array([.1, .2, .3, .4], np.float32))
    first, second = np.zeros((2, 1), np.float32), np.ones((4, 1), np.float32)
    r.render('primary', first, 2, None, None)
    r.render('primary', second, 4, None, None)
    assert first[:, 0] == pytest.approx(np.array([.1, .2]) * 10 ** .6)
    assert second[:, 0] == pytest.approx([1, 1, 0, 0])


def test_quit_after_stalled_engine_sets_done():
    s, _ = service()
    class Stalled:
        def stop(self):
            raise RuntimeError('engine did not stop')
    s.runtime = Stalled()
    with pytest.raises(RuntimeError):
        s.command({'cmd': 'quit'})
    assert s.done.is_set()
    assert s.engine is None


def test_oversized_command_does_not_break_next_line():
    s, _ = service()
    s.run(io.StringIO('x' * 100000 + '\n{"cmd":"status"}\n{"cmd":"quit"}\n'))
    assert [e['event'] for e in events(s)] == ['status', 'error', 'status', 'stopped']


@pytest.mark.parametrize('kind,adapter', [('imported', 'rvc_adapter'), ('trained', 'rvc_adapter'),
                                        ('zeroshot', 'seed_adapter')])
def test_engine_selection_safe_load_and_environment(kind, adapter, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import patch
    from vc_worker import engines, rvc_adapter, seed_adapter
    calls = []
    def torch_load(*args, **kwargs):
        calls.append(kwargs)
        return {}
    torch = SimpleNamespace(load=torch_load, backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setitem(sys.modules, 'torch', torch)
    def adapter_load(voice):
        assert os.environ['SYSTEM_VERSION_COMPAT'] == '0'
        torch.load('local-checkpoint', weights_only=False)
        return Engine()
    monkeypatch.setattr(rvc_adapter if adapter == 'rvc_adapter' else seed_adapter, 'load', adapter_load)
    file = tmp_path / 'local'
    file.touch()
    with patch.dict(os.environ):
        engine = engines.load_engine({'kind': kind, 'model': str(file), 'reference': str(file)})
        assert os.environ['HF_HUB_OFFLINE'] == '1'
    assert isinstance(engine, Engine)
    assert calls == [{'weights_only': True}]
    assert torch.load is torch_load


@pytest.mark.parametrize('value', [1, 11, True, 4.0])
def test_live_diffusion_bounds(value):
    from vc_worker.protocol import validate_params
    with pytest.raises(ValueError):
        validate_params({'diffusion_steps': value}, {})
    assert validate_params({}, {})['diffusion_steps'] == 4


def test_bench_requires_loaded_stopped_worker():
    s, _ = service()
    with pytest.raises(ValueError, match='loaded, stopped'):
        s.command({'cmd': 'bench', 'candidates': [{'block_ms': 256}]})
    load(s)
    s.command({'cmd': 'start'})
    try:
        with pytest.raises(ValueError, match='loaded, stopped'):
            s.command({'cmd': 'bench', 'candidates': [{'block_ms': 256}]})
    finally:
        s.stop()


def test_bench_minimum_timings_no_devices(monkeypatch):
    import vc_worker.protocol as protocol
    s, audio = service()
    load(s)
    ticks = iter([0, .020, 1, 1.030, 2, 2.010, 3, 3.040, 4, 4.050, 5, 5.030])
    monkeypatch.setattr(protocol.time, 'perf_counter', lambda: next(ticks))
    s.command({'cmd': 'bench', 'candidates': [{'block_ms': 256}, {'block_ms': 500}]})
    results = next(e['results'] for e in events(s) if e['event'] == 'bench')
    assert [r['ms'] for r in results] == pytest.approx([10, 30])
    assert [r['block_ms'] for r in results] == [256, 500]
    assert not audio.streams


def test_bench_zeroshot_restores_reference(monkeypatch, tmp_path):
    import types
    calls = []
    engine = Engine()
    engine.set_reference = calls.append
    monkeypatch.setitem(sys.modules, 'soundfile', types.SimpleNamespace(
        read=lambda *a, **kw: (np.zeros((160000, 1), dtype='float32'), 16000)))
    s = Service(io.StringIO(), loader=lambda v: engine, rss=lambda: 100)
    s.command({'cmd': 'load', 'voice': {'kind': 'zeroshot', 'reference': str(tmp_path / 'ref.wav'), 'ref_seconds': 5}})
    s.command({'cmd': 'bench', 'candidates': [{'ref_seconds': 3, 'diffusion_steps': 2, 'block_ms': 1000}]})
    assert calls == [3, 5]
    def fail(*args):
        raise RuntimeError('conversion failed')
    engine.convert = fail
    with pytest.raises(RuntimeError):
        s.command({'cmd': 'bench', 'candidates': [{'ref_seconds': 10, 'diffusion_steps': 6, 'block_ms': 256}]})
    assert calls[-2:] == [10, 5]


def test_validate_params_bypass():
    from vc_worker.protocol import validate_params
    assert validate_params({}, {})['bypass'] is False
    assert validate_params({'bypass': True}, {})['bypass'] is True
    for value in (1, 'true', None):
        with pytest.raises(ValueError):
            validate_params({'bypass': value}, {})


@pytest.fixture
def inject_service(tmp_path):
    from types import SimpleNamespace
    s = Service(io.StringIO(), rss=lambda: 100, inject_dir=tmp_path / 'inject')
    Path(s.inject_dir).mkdir()
    calls = []
    s.state = 'running'
    s.runtime = SimpleNamespace(devices={'monitor_enabled': True},
        inject=lambda *args: calls.append(args), cancel_inject=lambda: calls.append('cancel'))
    return s, calls


def test_inject_protocol_valid_file_and_cancel(inject_service):
    s, calls = inject_service
    path = Path(s.inject_dir) / 'phrase.f32'
    samples = np.array([.1, -.2], dtype='<f4')
    samples.tofile(path)
    s.inject({'id': 'abc_-12', 'path': str(path)})
    assert not path.exists()
    assert calls[0][0] == 'abc_-12' and calls[0][2] == 'both'
    np.testing.assert_array_equal(calls[0][1], samples)
    s.runtime = None
    s.command({'cmd': 'inject_cancel'})


@pytest.mark.parametrize('bad', ['size', 'long', 'nan', 'monitor'])
def test_inject_protocol_rejected_pcm_is_removed(inject_service, bad):
    s, calls = inject_service
    path = Path(s.inject_dir) / 'phrase.f32'
    if bad == 'size':
        path.write_bytes(b'123')
    elif bad == 'long':
        with path.open('wb') as f:
            f.truncate(60 * s.config.sample_rate * 4 + 4)
    elif bad == 'nan':
        np.array([np.nan], dtype='<f4').tofile(path)
    else:
        np.zeros(4, dtype='<f4').tofile(path)
        s.runtime.devices['monitor_enabled'] = False
    with pytest.raises(ValueError):
        s.inject({'id': 'abc', 'path': str(path), 'target': 'monitor' if bad == 'monitor' else 'both'})
    assert not path.exists() and not calls


@pytest.mark.parametrize('kind', ['outside', 'parent', 'symlink', 'relative', 'unset'])
def test_inject_protocol_rejects_untrusted_path(inject_service, tmp_path, kind):
    s, calls = inject_service
    outside = tmp_path / 'outside.f32'
    np.ones(4, dtype='<f4').tofile(outside)
    path = outside
    if kind == 'parent':
        path = Path(s.inject_dir) / '..' / outside.name
    elif kind == 'symlink':
        path = Path(s.inject_dir) / 'link.f32'
        path.symlink_to(outside)
    elif kind == 'relative':
        path = Path('outside.f32')
    elif kind == 'unset':
        s.inject_dir = None
    with pytest.raises(ValueError, match='inside inject_dir'):
        s.inject({'id': 'abc', 'path': str(path)})
    assert outside.exists() and not calls


@pytest.mark.parametrize('state', ['idle', 'loaded', 'loading'])
def test_inject_requires_running(inject_service, state):
    s, _ = inject_service
    s.state = state
    with pytest.raises(ValueError, match='inject requires running worker'):
        s.inject({'id': 'abc', 'path': '/unused'})


@pytest.mark.parametrize('patch', [{'id':''}, {'id':'a'*65}, {'id':'a.b'}, {'target':'other'}])
def test_inject_protocol_id_and_target(inject_service, patch):
    s, _ = inject_service
    with pytest.raises(ValueError):
        s.inject({'id': 'abc', 'path': '/unused', **patch})


def test_inject_protocol_missing_file(inject_service):
    s, calls = inject_service
    with pytest.raises(ValueError, match='inject file does not exist'):
        s.inject({'id': 'abc', 'path': str(Path(s.inject_dir) / 'missing.f32')})
    assert not calls
