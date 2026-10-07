import sys
from pathlib import Path
import wave
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vc_worker.streaming import Config, Streaming
from vc_worker.runtime import Runtime


class Identity:
    def __init__(self):
        self.windows = []
    def convert(self, audio, sr, params):
        self.windows.append(audio.copy())
        return audio.copy(), sr


@pytest.mark.parametrize('bypass', [False, True])
def test_runtime_gate_levels_and_quiet_loud_output(bypass):
    import threading
    from types import SimpleNamespace
    events = []
    ready = threading.Event()
    streams = []
    def stream(**kwargs):
        result = SimpleNamespace(kwargs=kwargs, start=lambda: None, stop=lambda: None,
                                 close=lambda: None)
        streams.append(result)
        return result
    sd = SimpleNamespace(InputStream=stream, OutputStream=stream,
                         query_devices=lambda device, **kw: {'hostapi': 0},
                         query_hostapis=lambda index: {'name': 'MME'})
    def emit(kind, **data):
        events.append((kind, data))
        ready.set()
    config = Config(sample_rate=8000, search_ms=0)
    runtime = Runtime(Identity(), config, {'gate_enabled': True, 'gate_db': -45,
                      'bypass': bypass}, {}, emit, lambda: None, sd=sd)
    runtime.start()
    try:
        callback = streams[-1].kwargs['callback']
        def feed(amplitude):
            ready.clear()
            callback(np.full((config.hop, 1), amplitude, np.float32), config.hop, None, None)
            assert ready.wait(1)
            assert events[-1][0] == 'levels', events
            out = runtime.output.get(timeout=1)
            level = events[-1][1]
            assert np.isfinite(level['input_db']) and isinstance(level['gate_open'], bool)
            assert level['input_level'] == pytest.approx(amplitude)
            return out, level
        for _ in range(5):
            out, level = feed(10 ** (-70 / 20))
        assert np.max(abs(out)) < 1e-8 and level['gate_open'] is False
        assert level['input_db'] == pytest.approx(-70, abs=.1)
        for _ in range(3):
            out, level = feed(.1)
        assert np.max(abs(out)) > .05 and level['gate_open'] is True
    finally:
        runtime.close()
        runtime.thread.join(timeout=1)


@pytest.fixture
def wav_audio(tmp_path):
    sr = 8000
    t = np.arange(sr * 2) / sr
    # Chirp avoids correlation ambiguities of constant/periodic fixtures.
    audio = (0.4 * np.sin(2 * np.pi * (120 * t + 37 * t * t))).astype(np.float32)
    path = tmp_path / 'source.wav'
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((audio * 32767).astype('<i2').tobytes())
    with wave.open(str(path), 'rb') as f:
        return np.frombuffer(f.readframes(f.getnframes()), '<i2').astype(np.float32) / 32767, sr


def test_wav_continuity_duration_and_stop(wav_audio):
    audio, sr = wav_audio
    config = Config(sample_rate=sr, search_ms=0)
    engine = Identity()
    stream = Streaming(engine, config)
    count = len(audio) // config.hop
    chunks = [stream.process(audio[i * config.hop:(i + 1) * config.hop]) for i in range(count)]
    tail = stream.finish()
    output = np.concatenate(chunks + [tail])
    assert len(output) == count * config.hop + config.fade
    assert np.allclose(output[config.fade:-config.fade], audio[:count * config.hop - config.fade], atol=1e-6)
    assert np.allclose(output[:config.fade], 0)
    assert output[-1] == 0
    assert np.max(np.abs(np.diff(output))) < 0.3
    assert not stream.finish().any()
    assert len(engine.windows[0]) == config.context + config.hop + config.fade
    assert not engine.windows[0][:config.context + config.fade].any()


def test_sola_finds_positive_and_negative_offsets():
    rng = np.random.default_rng(10)
    c = Config(sample_rate=8000)
    target = rng.normal(0, .1, c.fade).astype(np.float32)
    for offset in (-70, 63):
        window = rng.normal(0, .01, c.context + c.hop + c.fade).astype(np.float32)
        window[c.context + offset:c.context + offset + c.fade] = target
        class Shift:
            def convert(self, audio, sr, params):
                return window, sr
        s = Streaming(Shift(), c)
        s.tail = target.copy()
        s.first = False
        result = s.process(np.zeros(c.hop, np.float32))
        assert s.last_offset == offset
        assert np.allclose(result[:c.fade], target, atol=1e-6)
        assert len(result) == c.hop


def test_silence_sola_stays_finite():
    c = Config(sample_rate=8000)
    s = Streaming(Identity(), c)
    for _ in range(3):
        assert not s.process(np.zeros(c.hop, np.float32)).any()
    assert s.last_offset == 0


def test_deadline_emits_silence_and_counts_drop(wav_audio):
    audio, sr = wav_audio
    ticks = iter([0, .3])
    c = Config(sample_rate=sr)
    s = Streaming(Identity(), c, clock=lambda: next(ticks))
    assert not s.process(audio[:c.hop]).any()
    assert s.dropped_blocks == 1
    assert s.processing_ms == 300
    assert not s.finish().any()


@pytest.mark.parametrize('kwargs', [{'context_ms': 1001}, {'hop_ms': 0}, {'crossfade_ms': 90},
                                    {'search_ms': 65}, {'sample_rate': True}])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


@pytest.mark.parametrize('bad', [np.zeros(2), np.full(2048, np.nan)])
def test_rejects_invalid_input(bad):
    with pytest.raises(ValueError):
        Streaming(Identity(), Config(sample_rate=8000)).process(bad)


def test_engine_invalid_output_and_budget():
    c = Config(sample_rate=8000)
    class Bad:
        def convert(self, audio, sr, params):
            return np.full(len(audio), np.inf), sr
    with pytest.raises(ValueError):
        Streaming(Bad(), c).process(np.zeros(c.hop))
    def exceeded():
        raise MemoryError('budget')
    with pytest.raises(MemoryError):
        Streaming(Identity(), c, budget=exceeded).process(np.zeros(c.hop))


def test_configurable_context_and_parameter_snapshot():
    c = Config(sample_rate=8000, hop_ms=160, context_ms=1000, crossfade_ms=80, search_ms=8)
    calls = []
    class Engine:
        def convert(self, audio, sr, params):
            calls.append((len(audio), params.copy()))
            return audio, sr
    s = Streaming(Engine(), c)
    result = s.process(np.ones(c.hop), {'pitch_shift': -12, 'index_rate': 1})
    assert calls == [(9920, {'pitch_shift': -12, 'index_rate': 1})]
    assert len(result) == 1280
    assert len(s.finish()) == 640


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / 'scripts/vc_bench.py').exists(),
    reason="scripts/vc_bench.py is absent from the public export",
)
def test_streaming_bench_uses_wav_hops_and_sola(wav_audio, tmp_path, monkeypatch, capsys):
    import importlib.util
    import json
    from types import SimpleNamespace
    from vc_worker import engines
    path = Path(__file__).resolve().parents[1] / 'scripts/vc_bench.py'
    spec = importlib.util.spec_from_file_location('streaming_bench_test', path)
    bench = importlib.util.module_from_spec(spec)
    # The portable streaming benchmark must import without POSIX resource.
    import builtins
    original_import = builtins.__import__
    def without_resource(name, *args, **kwargs):
        if name == 'resource':
            raise ModuleNotFoundError("No module named 'resource'")
        return original_import(name, *args, **kwargs)
    with monkeypatch.context() as imports:
        imports.setattr(builtins, '__import__', without_resource)
        spec.loader.exec_module(bench)
    monkeypatch.setattr(bench, '__file__', str(tmp_path / 'scripts/vc_bench.py'))
    directory = tmp_path / 'state/vc-voices/test'
    directory.mkdir(parents=True)
    (directory / 'meta.json').write_text(json.dumps({'kind': 'trained'}))
    audio, sr = wav_audio
    wav = tmp_path / 'source.wav'
    with wave.open(str(wav), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((audio * 32767).astype('<i2').tobytes())
    written = []
    def read(path, **kwargs):
        with wave.open(str(path), 'rb') as f:
            return np.frombuffer(f.readframes(f.getnframes()), '<i2').astype(np.float32)[:, None] / 32767, f.getframerate()
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(read=read, write=lambda *args: written.append(args)))
    identity = Identity()
    monkeypatch.setattr(engines, 'load_engine', lambda voice: identity)
    result = bench.streaming_benchmark(SimpleNamespace(voice_id='test', input_wav=wav, output=tmp_path / 'out.wav'))
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report['kind'] == 'streaming_wav_benchmark'
    assert report['end_to_end_latency_measured'] is False
    assert len(identity.windows) == 8
    assert report['output_frames'] == 8 * 2048 + 512
    assert len(written[0][1]) == report['output_frames']


def test_live_hop_bounds():
    assert Config(hop_ms=1000).hop_ms == 1000
    with pytest.raises(ValueError):
        Config(hop_ms=1001)


def test_bypass_skips_engine_and_returns_input():
    c = Config(sample_rate=8000)
    class Counting:
        calls = 0
        def convert(self, audio, sr, params):
            Counting.calls += 1
            return audio * 0, sr
    s = Streaming(Counting(), c)
    t = np.arange(c.hop * 4) / c.sample_rate
    signal = (0.4 * np.sin(2 * np.pi * (120 * t + 37 * t * t))).astype(np.float32)
    out = []
    for i in range(4):
        out.append(s.process(signal[i * c.hop:(i + 1) * c.hop], {'bypass': True}))
    assert Counting.calls == 0
    result = np.concatenate(out)
    # Output lags the input by the crossfade overlap; compare past the first fade.
    lag = c.fade
    expected = np.concatenate((np.zeros(lag, np.float32), signal))[:len(result)]
    assert np.allclose(result[c.hop:], expected[c.hop:], atol=1e-3)


def test_bypass_switch_has_no_jump():
    # Disable offset search so SOLA cannot align an inverted periodic signal back
    # onto the input: the shared crossfade must smooth a real change of waveform.
    c = Config(sample_rate=8000, search_ms=0)
    class PhaseInverted:
        def __init__(self):
            self.calls = 0
        def convert(self, audio, sr, params):
            self.calls += 1
            return -audio, sr
    engine = PhaseInverted()
    s = Streaming(engine, c)
    t = np.arange(c.hop * 6) / c.sample_rate
    signal = (0.4 * np.cos(2 * np.pi * 220 * t)).astype(np.float32)
    flags = [True, True, True, False, False, True]
    out = np.concatenate([s.process(signal[i * c.hop:(i + 1) * c.hop], {'bypass': flag})
                          for i, flag in enumerate(flags)])
    assert engine.calls == 2
    delayed = np.concatenate((np.zeros(c.fade, np.float32), signal))[:len(out)]
    assert np.allclose(out[c.hop:3 * c.hop], delayed[c.hop:3 * c.hop], atol=1e-6)
    assert np.allclose(out[3 * c.hop + c.fade:5 * c.hop],
                       -delayed[3 * c.hop + c.fade:5 * c.hop], atol=1e-6)
    assert np.allclose(out[5 * c.hop + c.fade:], delayed[5 * c.hop + c.fade:], atol=1e-6)
    for index in (3, 5):
        boundary = index * c.hop
        # An abrupt switch between these distinct signals would exceed the
        # threshold at this very boundary, making the fixture regression-sensitive.
        assert abs(delayed[boundary] + delayed[boundary - 1]) > 0.1
        assert abs(out[boundary] - out[boundary - 1]) < 0.1
    # Skip the initial zero-history block; this test covers mode switches in an
    # established stream, not the onset of a cosine fixture at nonzero amplitude.
    assert np.abs(np.diff(out[c.hop:])).max() < 0.1


@pytest.mark.parametrize('target', ['both', 'monitor'])
def test_runtime_inject_replaces_three_microphone_hops_and_skips_gate(target):
    import threading
    from types import SimpleNamespace
    config = Config(sample_rate=8000)
    events, inputs = [], []
    ready = threading.Event()
    def emit(kind, **fields):
        events.append((kind, fields))
        if kind == 'levels':
            ready.set()
    runtime = Runtime(Identity(), config, {'gate_enabled':True}, {'monitor_enabled':True}, emit, lambda: None)
    def process(block, params):
        inputs.append(block.copy())
        return block.copy()
    runtime.processor = SimpleNamespace(process=process, processing_ms=0, dropped_blocks=0)
    def gate(*args):
        raise AssertionError('gate must not process injected blocks')
    runtime.gate.process = gate
    phrase = np.arange(config.hop * 5 // 2, dtype=np.float32) / (config.hop * 5)
    runtime.inject('one', phrase, target)
    runtime.thread = threading.Thread(target=runtime._run)
    runtime.thread.start()
    try:
        for _ in range(3):
            ready.clear()
            runtime.capture(np.full((config.hop, 1), .9, np.float32), config.hop, None, None)
            assert ready.wait(1)
            primary = runtime.output.get(timeout=1)
            monitor = runtime.monitor.get(timeout=1)
            np.testing.assert_array_equal(monitor, inputs[-1])
            np.testing.assert_array_equal(primary, inputs[-1] if target == 'both' else np.zeros(config.hop))
            assert events[-1][1]['input_level'] == pytest.approx(.9)
            assert events[-1][1]['injecting'] is True
        np.testing.assert_array_equal(np.concatenate(inputs)[:len(phrase)], phrase)
        assert not inputs[-1][config.hop//2:].any()
        assert [(fields['id'], fields['state']) for kind, fields in events if kind == 'inject'] == [('one','playing'), ('one','done')]
    finally:
        runtime.close()
        runtime.thread.join(timeout=1)


def test_runtime_inject_queue_limit_cancel_and_close():
    config = Config(sample_rate=8000)
    events = []
    runtime = Runtime(Identity(), config, {}, {}, lambda kind, **fields: events.append((kind, fields)), lambda: None)
    phrase = np.ones(config.hop * 4, np.float32)
    for phrase_id in ('a', 'b', 'c'):
        runtime.inject(phrase_id, phrase)
    with pytest.raises(ValueError, match='too many pending phrases'):
        runtime.inject('d', phrase)
    runtime.inject_cur = (*runtime.inject_queue.popleft(), config.hop)
    runtime.cancel_inject()
    assert runtime.inject_cur is None and not runtime.inject_queue
    assert [(data['id'], data['state']) for _, data in events] == [('a','cancelled'), ('b','cancelled'), ('c','cancelled')]
    runtime.inject('e', phrase)
    runtime.close()
    assert events[-1][1] == {'id':'e', 'state':'cancelled'}



def test_runtime_cancel_active_phrase_resumes_microphone_and_stop_cancels_queue():
    import threading
    from types import SimpleNamespace
    config = Config(sample_rate=8000)
    events, inputs = [], []
    ready = threading.Event()
    def emit(kind, **fields):
        events.append((kind, fields))
        if kind == 'levels':
            ready.set()
    runtime = Runtime(Identity(), config, {}, {}, emit, lambda: None)
    def process(block, params):
        inputs.append(block.copy())
        return block.copy()
    runtime.processor = SimpleNamespace(process=process, processing_ms=0, dropped_blocks=0,
                                        finish=lambda: np.zeros(config.fade, np.float32))
    runtime.inject('active', np.full(config.hop * 4, .2, np.float32))
    runtime.thread = threading.Thread(target=runtime._run)
    runtime.thread.start()
    try:
        for cancel in (False, True):
            if cancel:
                runtime.cancel_inject()
            ready.clear()
            runtime.capture(np.full((config.hop, 1), .7, np.float32), config.hop, None, None)
            assert ready.wait(1)
            runtime.output.get(timeout=1)
        np.testing.assert_allclose(inputs[0], .2)
        np.testing.assert_allclose(inputs[1], .7)
        assert events[-1][1]['injecting'] is False
        assert ('inject', {'id':'active', 'state':'cancelled'}) in events
        assert not any(data.get('state') == 'done' for kind, data in events if kind == 'inject')
        runtime.inject('queued', np.ones(config.hop, np.float32))
        runtime.stop()
        assert ('inject', {'id':'queued', 'state':'cancelled'}) in events
    finally:
        runtime.close()
        runtime.thread.join(timeout=1)
