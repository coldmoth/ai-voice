"""JSON-lines control service. stdout is exclusively protocol events."""
from contextlib import redirect_stdout
import json
import math
import os
import re
import resource
import sys
import threading
import time
import numpy as np
from .engines import load_engine
from .runtime import Runtime
from .streaming import Config

RSS_LIMIT = 3_000_000_000


def rss_bytes():
    # High-water RSS is conservative: a breached process must be restarted.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == 'darwin' else peak * 1024)


def validate_params(patch, current):
    if not isinstance(patch, dict):
        raise ValueError('params must be an object')
    result = {"diffusion_steps": 4, "bypass": False, "gate_enabled": False, "gate_db": -45, **current}
    bounds = {'pitch_shift': (-12, 12), 'index_rate': (0, 1),
              'gate_db': (-70, -10), 'diffusion_steps': (2, 10), 'output_gain_db': (-24, 12), 'monitor_gain_db': (-24, 12)}
    for key, value in patch.items():
        if key in ('bypass', 'gate_enabled'):
            if not isinstance(value, bool):
                raise ValueError(f'{key} must be a boolean')
            result[key] = value
            continue
        if key not in bounds or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'invalid parameter: {key}')
        low, high = bounds[key]
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} outside {low}..{high}')
        if key == 'diffusion_steps' and type(value) is not int:
            raise ValueError('diffusion_steps must be an integer')
        if key == 'pitch_shift' and int(value) != value:
            raise ValueError('pitch_shift must be an integer')
        result[key] = value
    return result


class Service:
    def __init__(self, output, loader=load_engine, runtime_factory=Runtime, rss=rss_bytes,
                 hard_exit=None, inject_dir=None):
        self.output, self.loader, self.runtime_factory, self.rss = output, loader, runtime_factory, rss
        self.hard_exit = hard_exit
        self.lock = threading.Lock()
        self.budget_lock = threading.Lock()
        self.engine = self.runtime = None
        self.params = {'pitch_shift': 0, 'index_rate': 0, 'output_gain_db': 0, 'monitor_gain_db': 0, 'diffusion_steps': 4, 'gate_enabled': False, 'gate_db': -45}
        self.config = Config()
        self.voice = None
        self.exceeded = threading.Event()
        self.done = threading.Event()
        self.state = 'idle'
        self.inject_dir = inject_dir or os.environ.get('AI_VOICE_INJECT_DIR')

    def inject(self, message):
        if self.state != 'running' or self.runtime is None:
            raise ValueError('inject requires running worker')
        phrase_id, path = message.get('id'), message.get('path')
        target = message.get('target', 'both')
        if not isinstance(phrase_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', phrase_id):
            raise ValueError('invalid inject id')
        if target not in ('both', 'monitor'):
            raise ValueError('invalid inject target')
        if (not self.inject_dir or not isinstance(path, str) or not os.path.isabs(path)
                or not os.path.realpath(path).startswith(os.path.realpath(self.inject_dir) + os.sep)):
            raise ValueError('inject path must be inside inject_dir')
        try:
            if not os.path.isfile(path):
                raise ValueError('inject file does not exist')
            size = os.path.getsize(path)
            if size % 4 or size > 60 * self.config.sample_rate * 4:
                raise ValueError('invalid inject file size (maximum 60 seconds)')
            if target == 'monitor' and not self.runtime.devices.get('monitor_enabled'):
                raise ValueError('monitor is off')
            samples = np.fromfile(path, dtype='<f4')
            if not np.isfinite(samples).all():
                raise ValueError('inject samples must be finite')
            self.runtime.inject(phrase_id, samples, target)
        except OSError as exc:
            raise ValueError(f'cannot read inject file: {exc}') from exc
        finally:
            if os.path.isfile(path):
                os.remove(path)

    def emit(self, event, **fields):
        if event == 'stopped':
            if fields.get('reason') == 'error':
                self.runtime = None
            self.state = 'loaded' if self.engine else 'idle'
        with self.lock:
            self.output.write(json.dumps({'event': event, **fields}, ensure_ascii=False, allow_nan=False) + '\n')
            self.output.flush()

    def budget(self):
        if self.exceeded.is_set():
            raise MemoryError('RSS budget exceeded (3 GB); restart worker')
        if self.rss() > RSS_LIMIT:
            with self.budget_lock:
                if self.exceeded.is_set():
                    raise MemoryError('RSS budget exceeded')
                self.exceeded.set()
            self.emit('error', code='rss_budget', message='RSS budget exceeded (3 GB); worker stopping')
            if self.runtime:
                self.runtime.halt.set()
                self.runtime.close()
            self.emit('stopped', reason='rss_budget')
            self.state = 'error'
            self.engine = None
            if self.hard_exit:
                self.hard_exit(3)
            raise MemoryError('RSS budget exceeded')

    def watch(self):
        while not self.done.wait(0.1):
            try:
                self.budget()
            except MemoryError:
                return

    def status(self):
        self.budget()
        runtime = self.runtime
        self.emit('status', state=self.state, voice=self.voice, params=self.params,
                  config=self.config.__dict__, rss_mb=self.rss() / 1024 ** 2,
                  dropped_blocks=(runtime.dropped + runtime.processor.dropped_blocks) if runtime else 0,
                  processing_ms=runtime.processor.processing_ms if runtime else 0,
                  # Estimate includes capture and one output callback; not a measured E2E latency.
                  latency_ms=(2 * self.config.hop_ms + runtime.processor.processing_ms) if runtime else None,
                  latency_kind='estimate')

    def stop(self):
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
        self.emit('stopped', reason='requested')

    def bench(self, candidates):
        if self.engine is None or self.runtime is not None:
            raise ValueError('bench requires loaded, stopped worker')
        if not isinstance(candidates, list) or not candidates:
            raise ValueError('candidates must be a nonempty list')
        zeroshot = self.voice.get('kind') == 'zeroshot'
        rate = self.config.sample_rate
        if zeroshot:
            import soundfile as sf
            reference, rate = sf.read(self.voice['reference'], dtype='float32', always_2d=True)
            reference = reference.mean(axis=1)
        else:
            reference = np.random.default_rng(0).normal(0, 0.01, round(rate * 1.5)).astype('float32')
        results = []
        try:
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise ValueError('invalid candidate')
                block = candidate.get('block_ms')
                if type(block) is not int or block not in (160, 256, 384, 500, 750, 1000):
                    raise ValueError('invalid block_ms')
                params = validate_params({k: candidate[k] for k in ('diffusion_steps',) if k in candidate}, self.params)
                if zeroshot:
                    seconds = candidate.get('ref_seconds')
                    if type(seconds) is not int or not 3 <= seconds <= 15:
                        raise ValueError('invalid ref_seconds')
                    self.engine.set_reference(seconds)
                    audio = reference[:round(rate * (block + 500) / 1000)]
                else:
                    audio = reference
                with redirect_stdout(sys.stderr):
                    self.engine.convert(audio, rate, params)
                    timings = []
                    for _ in range(3):
                        self.budget()
                        start = time.perf_counter()
                        self.engine.convert(audio, rate, params)
                        timings.append((time.perf_counter() - start) * 1000)
                results.append({**candidate, 'ms': min(timings)})
        finally:
            if zeroshot:
                with redirect_stdout(sys.stderr):
                    self.engine.set_reference(self.voice.get('ref_seconds', 5))
        self.emit('bench', results=results)

    def command(self, message):
        if not isinstance(message, dict):
            raise ValueError('command must be an object')
        cmd = message.get('cmd')
        if cmd not in ('load', 'start', 'stop', 'params', 'status', 'bench', 'quit', 'inject', 'inject_cancel'):
            raise ValueError('unknown command')
        if cmd == 'quit':
            try:
                self.stop()
            finally:
                self.engine = None
                self.done.set()
            return False
        if cmd == 'stop':
            self.stop()
            return True
        self.budget()
        if cmd == 'load':
            if self.runtime:
                raise ValueError('stop before loading another voice')
            voice = message.get('voice')
            if not isinstance(voice, dict):
                raise ValueError('voice must be an object')
            config = Config(**message.get('config', {}))
            params = validate_params(message.get('params', {}), self.params)
            self.engine = None  # release the old model before loading another one
            self.voice = None
            self.state = 'loading'
            self.emit('status', state='loading')
            with redirect_stdout(sys.stderr):
                engine = self.loader(voice)
            self.budget()
            self.engine, self.voice, self.config, self.params = engine, dict(voice), config, params
            self.state = 'loaded'
        elif cmd == 'start':
            if self.engine is None or self.runtime:
                raise ValueError('start requires loaded, stopped worker')
            devices = message.get('devices', {})
            if not isinstance(devices, dict):
                raise ValueError('devices must be an object')
            runtime = self.runtime_factory(self.engine, self.config, self.params, devices, self.emit, self.budget)
            self.runtime = runtime
            self.state = 'running'
            try:
                runtime.start()
            except Exception:
                self.runtime = None
                self.state = 'loaded'
                raise
        elif cmd == 'bench':
            self.bench(message.get('candidates'))
        elif cmd == 'params':
            self.params = validate_params(message.get('params'), self.params)
            if self.runtime:
                self.runtime.params = dict(self.params)
        elif cmd == 'inject':
            self.inject(message)
        elif cmd == 'inject_cancel':
            if self.runtime:
                self.runtime.cancel_inject()
        self.status()
        return True

    def run(self, source):
        watch = threading.Thread(target=self.watch, daemon=True, name='vc-rss')
        watch.start()
        try:
            self.emit('status', state='idle')
            while not self.done.is_set():
                line = source.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    while line and not line.endswith('\n'):
                        line = source.readline(65537)
                    self.emit('error', message='command exceeds 64 KiB')
                    continue
                try:
                    if not self.command(json.loads(line)):
                        break
                except MemoryError:
                    break
                except Exception as exc:
                    if self.state == 'loading':
                        self.state = 'idle'
                    self.emit('error', message=str(exc))
        finally:
            try:
                if not self.done.is_set():
                    self.stop()
            finally:
                self.done.set()
                watch.join(timeout=0.3)
