"""Windows capture with fake PortAudio; no microphone or model downloads."""
import asyncio
import base64
from types import SimpleNamespace

import numpy as np
import pytest
from sounddevice import CallbackFlags

from ai_voice import asr, devices


def fake_capture(monkeypatch, chunks, *, error=None, status=False):
    state = SimpleNamespace(closed=False, options=None)

    class InputStream:
        def __init__(self, **kwargs):
            state.options = kwargs

        def __enter__(self):
            if error:
                raise error
            def feed():
                for chunk in chunks:
                    state.options['callback'](chunk.reshape(-1, 1), len(chunk), None, status)
                state.options['finished_callback']()
            asyncio.get_running_loop().call_soon(feed)
            return self

        def __exit__(self, *args):
            state.closed = True

    monkeypatch.setattr(asr, 'sys', SimpleNamespace(platform='win32'), raising=False)
    monkeypatch.setattr(devices, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(devices.sd, 'query_devices',
                        lambda device, *, kind: {'hostapi': 0} if kind == 'input' else pytest.fail(kind))
    monkeypatch.setattr(devices.sd, 'query_hostapis', lambda index: {'name': 'Windows WASAPI'})
    monkeypatch.setattr(devices.sd, 'WasapiSettings', lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setitem(__import__('sys').modules, 'sounddevice', SimpleNamespace(InputStream=InputStream))

    async def forbidden_spawn(*args):
        pytest.fail('Windows capture must not spawn SpeechHelper')

    monkeypatch.setattr(asr.SpeechASR, '_spawn', forbidden_spawn)
    return state


def utterance():
    tone = (0.1 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
    audio = np.concatenate([np.zeros(16000, np.float32), tone, np.zeros(16000, np.float32)])
    return list(np.split(audio, 150))


@pytest.mark.asyncio
async def test_win_capture_emits_helper_events(monkeypatch):
    chunks = utterance()
    state = fake_capture(monkeypatch, chunks)
    capture = asr.SpeechASR(input_device='Test microphone')
    events = [event async for event in capture.events()]
    kinds = [event['event'] for event in events]
    assert kinds[0:2] == ['ready', 'vad_start']
    assert kinds[-1] == 'vad_end'
    assert set(kinds[2:-1]) == {'audio'}
    assert len({e['utterance_id'] for e in events[1:]}) == 1
    for event in events:
        asr.SpeechASR._parse(__import__('json').dumps(event).encode())
    pcm = b''.join(base64.b64decode(e['pcm']) for e in events if e['event'] == 'audio')
    assert 1.4 * 32000 < len(pcm) < 1.7 * 32000
    assert state.closed
    assert state.options['extra_settings'].auto_convert is True
    assert {k: state.options[k] for k in ('samplerate', 'channels', 'dtype', 'device')} == {
        'samplerate': 16000, 'channels': 1, 'dtype': 'float32', 'device': 'Test microphone'}

    fake_capture(monkeypatch, chunks)
    calls = []
    monkeypatch.setattr(asr, 'load_gigaam', lambda model: None)
    def transcribe(self, data):
        calls.append(data)
        return 'recognized'
    monkeypatch.setattr(asr.GigaAMASR, '_transcribe', transcribe)
    events = [event async for event in asr.GigaAMASR().events()]
    assert [e['event'] for e in events] == ['ready', 'vad_start', 'vad_end', 'final']
    assert calls == [pcm]
    assert events[-1]['text'] == 'recognized'


@pytest.mark.asyncio
async def test_win_input_gain_applied_and_acknowledged(monkeypatch):
    state = fake_capture(monkeypatch, [np.full(1600, 0.5, np.float32), np.zeros(8000, np.float32)])
    capture = asr.SpeechASR(initial_input_gain_db=-6)
    stream = capture.events()
    assert (await anext(stream))['input_gain_db'] == -6
    await capture.set_input_gain(12)
    events = [e async for e in stream]
    ack = next(e for e in events if e['event'] == 'input_gain')
    assert ack['db'] == 12
    pcm = np.frombuffer(base64.b64decode(next(e['pcm'] for e in events if e['event'] == 'audio')), '<i2')
    assert np.all(pcm == 32767)
    assert state.closed
    with pytest.raises(asr.ASRError):
        await capture.set_input_gain(0)


@pytest.mark.asyncio
async def test_win_capture_closes_on_cancellation(monkeypatch):
    state = fake_capture(monkeypatch, [])
    stream = asr.SpeechASR().events()
    assert (await anext(stream))['event'] == 'ready'
    await stream.aclose()
    assert state.closed


@pytest.mark.asyncio
async def test_win_capture_open_failure(monkeypatch):
    fake_capture(monkeypatch, [], error=Exception('device unavailable'))
    with pytest.raises(asr.ASRError, match='device unavailable'):
        _ = [e async for e in asr.SpeechASR().events()]


@pytest.mark.asyncio
async def test_win_initial_gain_override_changes_pcm(monkeypatch):
    fake_capture(monkeypatch, [np.full(1600, 0.5, np.float32), np.zeros(8000, np.float32)])
    capture = asr.SpeechASR(initial_input_gain_db=0)
    events = [e async for e in capture.events(initial_input_gain_db=-6)]
    assert events[0]['input_gain_db'] == -6
    pcm = np.frombuffer(base64.b64decode(next(e['pcm'] for e in events if e['event'] == 'audio')), '<i2')
    assert np.all(pcm == int(0.5 * 10 ** (-6 / 20) * 32767))


@pytest.mark.asyncio
async def test_win_two_utterances_have_distinct_ids(monkeypatch):
    audio = np.concatenate(utterance())
    fake_capture(monkeypatch, list(np.split(np.tile(audio, 2), 60)))
    events = [e async for e in asr.SpeechASR().events()]
    starts = [e['utterance_id'] for e in events if e['event'] == 'vad_start']
    ends = [e['utterance_id'] for e in events if e['event'] == 'vad_end']
    assert len(starts) == len(set(starts)) == 2
    assert starts == ends


@pytest.mark.asyncio
async def test_win_capture_overflow_is_error_and_closes(monkeypatch):
    state = fake_capture(monkeypatch, [np.zeros(320, np.float32)] * 300)
    with pytest.raises(asr.ASRError, match='queue overflow'):
        _ = [e async for e in asr.SpeechASR().events()]
    assert state.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('overflow', [False, True])
async def test_win_capture_callback_error_closes(monkeypatch, overflow):
    state = fake_capture(monkeypatch, [])
    stream = asr.SpeechASR().events()
    await anext(stream)
    status = CallbackFlags()
    status.input_underflow = True
    status.input_overflow = overflow
    state.options['callback'](np.zeros((320, 1), np.float32), 320, None, status)
    with pytest.raises(asr.ASRError, match='input underflow'):
        _ = [e async for e in stream]
    assert state.closed


@pytest.mark.asyncio
async def test_win_capture_input_overflow_keeps_frame(monkeypatch):
    status = CallbackFlags()
    status.input_overflow = True
    state = fake_capture(monkeypatch, [np.full(1600, 0.5, np.float32),
                                      np.zeros(8000, np.float32)], status=status)
    events = [e async for e in asr.SpeechASR().events()]
    pcm = np.frombuffer(base64.b64decode(next(e['pcm'] for e in events if e['event'] == 'audio')), '<i2')
    assert np.all(pcm == 16383)
    assert events[-1]['event'] == 'vad_end'
    assert state.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('stream_error', [False, True])
async def test_win_capture_callback_drops_frame_when_loop_closed(monkeypatch, stream_error):
    loop = asyncio.get_running_loop()
    state = fake_capture(monkeypatch, [])
    stream = asr.SpeechASR().events()
    await anext(stream)
    await stream.aclose()
    assert state.closed
    status = CallbackFlags()
    status.input_underflow = stream_error

    def closed(*args):
        raise RuntimeError('Event loop is closed')

    with monkeypatch.context() as patch:
        patch.setattr(loop, 'call_soon_threadsafe', closed)
        state.options['callback'](np.zeros((320, 1), np.float32), 320, None, status)
        state.options['finished_callback']()
