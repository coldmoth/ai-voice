import numpy as np
import pytest

from ai_voice.vc_meter import InputMeter


class Audio:
    def __init__(self):
        self.streams = []
        self.error = None

    def InputStream(self, **kwargs):
        if self.error:
            raise self.error
        stream = Stream(kwargs)
        self.streams.append(stream)
        return stream


class Stream:
    def __init__(self, kwargs):
        self.kwargs, self.closed, self.stopped = kwargs, False, False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def test_level_tone_expiry_and_stream_idempotence():
    sd, now = Audio(), [0.0]
    meter = InputMeter(sd=sd, clock=lambda: now[0])
    meter.start('mic')
    meter.start('mic')
    assert len(sd.streams) == 1 and meter.running
    stream = sd.streams[0]
    assert {k: stream.kwargs[k] for k in ('samplerate', 'channels', 'dtype', 'blocksize')} == {
        'samplerate': 16000, 'channels': 1, 'dtype': 'float32', 'blocksize': 800}
    tone = (.1 * np.sqrt(2) * np.sin(2 * np.pi * 1000 * np.arange(800) / 16000)).astype('float32')
    stream.kwargs['callback'](tone[:, None], 800, None, None)
    assert meter.level_db() == pytest.approx(-20, abs=.5)
    now[0] = 1.0
    assert meter.level_db() is None
    meter.start('other')
    assert stream.closed and stream.stopped and len(sd.streams) == 2
    meter.stop()
    meter.stop()
    assert sd.streams[1].closed and not meter.running


def test_open_failure_stops_old_stream():
    sd, meter = Audio(), None
    meter = InputMeter(sd=sd)
    meter.start('mic')
    sd.error = ValueError('no device')
    with pytest.raises(RuntimeError, match='Could not open the microphone: no device'):
        meter.start('missing')
    assert sd.streams[0].closed and not meter.running


def test_record_filters_nonfinite_and_bounds_history():
    now = [0.0]
    meter = InputMeter(clock=lambda: now[0])
    for value in (np.nan, np.inf, -np.inf):
        meter.record(value)
    assert meter.level_db() is None and meter.samples_since(0) == []
    for i in range(220):
        now[0] = i
        meter.record(-50)
    assert len(meter.samples_since(0)) == 200
    assert len(meter.samples_since(215)) == 5


def test_stop_closes_even_when_stop_raises():
    sd = Audio()
    meter = InputMeter(sd=sd)
    meter.start(None)
    def fail():
        raise ValueError('stop')
    sd.streams[0].stop = fail
    with pytest.raises(ValueError):
        meter.stop()
    assert sd.streams[0].closed and not meter.running
