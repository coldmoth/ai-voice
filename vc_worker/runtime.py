"""Worker-owned sounddevice streams and bounded queues; never used by the UI."""
import queue
import sys
import collections
import threading
import time
import numpy as np
from .streaming import Streaming
from .gate import NoiseGate


def stream_extra_settings(sd, device, *, kind="output"):
    # Separate worker Python has no ai_voice; mirror ai_voice.devices.stream_extra_settings.
    if sys.platform == "win32":
        info = sd.query_devices(device, kind=kind)
        if sd.query_hostapis(info["hostapi"])["name"] == "Windows WASAPI":
            return sd.WasapiSettings(auto_convert=True)
    return None


def latest(q, item):
    dropped = 0
    while True:
        try:
            q.put_nowait(item)
            return dropped
        except queue.Full:
            try:
                q.get_nowait()
                dropped += 1
            except queue.Empty:
                pass


def clear(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class Runtime:
    def __init__(self, engine, config, params, devices, emit, budget, sd=None):
        self.processor = Streaming(engine, config, budget=budget)
        self.gate = NoiseGate(config.sample_rate)
        self.config, self.params, self.devices = config, dict(params), devices
        self.emit, self.budget = emit, budget
        self.sd = sd
        self.input = queue.Queue(maxsize=1)
        self.output = queue.Queue(maxsize=2)
        self.monitor = queue.Queue(maxsize=2)
        self.halt = threading.Event()
        self.drained = threading.Event()
        self.monitor_drained = threading.Event()
        self.streams = []
        self.thread = None
        self.dropped = 0
        self.input_level = self.output_level = 0.0
        self.buffers = {'primary': np.empty(0, np.float32), 'monitor': np.empty(0, np.float32)}
        self.stopping = False
        self.inject_queue = collections.deque()
        self.inject_cur = None
        self.inject_lock = threading.Lock()

    def inject(self, phrase_id, samples, target='both'):
        with self.inject_lock:
            if len(self.inject_queue) >= 3:
                raise ValueError('too many pending phrases')
            self.inject_queue.append((phrase_id, samples, target))

    def cancel_inject(self):
        with self.inject_lock:
            phrases = list(self.inject_queue)
            if self.inject_cur is not None:
                phrases.insert(0, self.inject_cur)
            self.inject_queue.clear()
            self.inject_cur = None
            for phrase in phrases:
                self.emit('inject', id=phrase[0], state='cancelled')

    def capture(self, indata, frames, timing, status):
        if self.halt.is_set():
            return
        if status:
            self.dropped += 1
        self.dropped += latest(self.input, np.asarray(indata[:, 0], dtype=np.float32).copy())

    def render(self, which, outdata, frames, timing, status):
        outdata.fill(0)
        q = self.output if which == 'primary' else self.monitor
        buf = self.buffers[which]
        position = 0
        while position < frames:
            if not len(buf):
                try:
                    buf = q.get_nowait()
                except queue.Empty:
                    break
            count = min(len(buf), frames - position)
            outdata[position:position + count, 0] = buf[:count]
            buf = buf[count:]
            position += count
        self.buffers[which] = buf
        gain = self.params.get('output_gain_db' if which == 'primary' else 'monitor_gain_db', 0)
        outdata *= 10 ** (gain / 20)
        np.clip(outdata, -1, 1, out=outdata)
        if self.stopping and not len(buf) and q.empty():
            (self.drained if which == 'primary' else self.monitor_drained).set()

    def start(self):
        if self.sd is None:
            import sounddevice
            self.sd = sounddevice
        c, d = self.config, self.devices
        if d.get('monitor_enabled') and (d.get('monitor_device') is None
                                         or d['monitor_device'] == d.get('output_device')):
            raise ValueError('monitor requires a distinct output device')
        try:
            self.streams.append(self.sd.OutputStream(device=d.get('output_device'), samplerate=c.sample_rate,
                extra_settings=stream_extra_settings(self.sd, d.get('output_device')),
                channels=1, dtype='float32', blocksize=c.hop,
                callback=lambda *args: self.render('primary', *args)))
            if d.get('monitor_enabled'):
                self.streams.append(self.sd.OutputStream(device=d['monitor_device'], samplerate=c.sample_rate,
                    extra_settings=stream_extra_settings(self.sd, d['monitor_device']),
                    channels=1, dtype='float32', blocksize=c.hop,
                    callback=lambda *args: self.render('monitor', *args)))
            self.streams.append(self.sd.InputStream(device=d.get('input_device'), samplerate=c.sample_rate,
                extra_settings=stream_extra_settings(self.sd, d.get('input_device'), kind='input'),
                channels=1, dtype='float32', blocksize=c.hop, callback=self.capture))
            for stream in self.streams:
                stream.start()
            self.thread = threading.Thread(target=self._run, daemon=True, name='vc-convert')
            self.thread.start()
        except Exception:
            self.close()
            raise

    def _run(self):
        try:
            while not self.halt.is_set():
                self.budget()
                try:
                    block = self.input.get(timeout=0.1)
                except queue.Empty:
                    continue
                raw_level = float(np.sqrt(np.mean(block * block)))
                input_db = self.gate._db(block)
                params = dict(self.params)
                injected, target = False, 'both'
                with self.inject_lock:
                    if self.inject_cur is None and self.inject_queue:
                        self.inject_cur = (*self.inject_queue.popleft(), 0)
                        self.emit('inject', id=self.inject_cur[0], state='playing')
                    if self.inject_cur is not None:
                        phrase_id, samples, target, pos = self.inject_cur
                        block = samples[pos:pos + self.config.hop]
                        if len(block) < self.config.hop:
                            block = np.pad(block, (0, self.config.hop - len(block)))
                        pos += self.config.hop
                        injected = True
                        if pos >= len(samples):
                            self.emit('inject', id=phrase_id, state='done')
                            self.inject_cur = None
                        else:
                            self.inject_cur = (phrase_id, samples, target, pos)
                if injected:
                    gate_open = self.gate.open
                else:
                    block, input_db, gate_open = self.gate.process(
                        block, bool(params.get('gate_enabled')), float(params.get('gate_db', -45)))
                out = self.processor.process(block, params)
                if self.halt.is_set():
                    break
                if self.processor.processing_ms > self.config.hop_ms:
                    # Anything captured during a missed deadline is already stale.
                    self.dropped += self.input.qsize()
                    clear(self.input)
                self.dropped += latest(self.output, np.zeros_like(out) if injected and target == 'monitor' else out)
                if self.devices.get('monitor_enabled'):
                    latest(self.monitor, out.copy())
                self.input_level = raw_level
                self.output_level = float(np.sqrt(np.mean(out * out)))
                self.emit('levels', input_level=self.input_level, output_level=self.output_level,
                          input_db=input_db, gate_open=gate_open,
                          injecting=injected,
                          dropped_blocks=self.dropped + self.processor.dropped_blocks,
                          processing_ms=self.processor.processing_ms)
        except MemoryError:
            # The budget controller already emitted error/stopped and terminates the process.
            self.close()
        except Exception as exc:
            self.emit('error', message=str(exc))
            self.halt.set()
            self.close()
            self.emit('stopped', reason='error')

    def stop(self):
        self.halt.set()
        self.cancel_inject()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.close()
                raise RuntimeError('engine did not stop within 2 seconds; quit worker')
        clear(self.input)
        clear(self.output)
        clear(self.monitor)
        self.buffers = {'primary': np.empty(0, np.float32), 'monitor': np.empty(0, np.float32)}
        tail = self.processor.finish()
        latest(self.output, tail)
        if self.devices.get('monitor_enabled'):
            latest(self.monitor, tail.copy())
        self.stopping = True
        if self.streams:
            deadline = time.monotonic() + max(1, self.config.hop_ms / 1000 * 3)
            self.drained.wait(timeout=max(0, deadline - time.monotonic()))
            if self.devices.get('monitor_enabled'):
                self.monitor_drained.wait(timeout=max(0, deadline - time.monotonic()))
        self.close()

    def close(self):
        self.halt.set()
        self.cancel_inject()
        for stream in reversed(self.streams):
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.streams.clear()
