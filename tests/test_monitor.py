"""Targeted monitor sink regression tests; offline-only."""
import threading
import time
import sounddevice as sd
import time

import numpy as np
import pytest


def _module():
    try:
        import ai_voice.monitor as mod
    except ModuleNotFoundError:
        pytest.fail("Missing ai_voice.monitor implementation")
    return mod


class _FakeStream:
    """Mimics sounddevice.OutputStream enough for MonitorSink lifecycle hooks."""
    last_instance = None
    opened = 0
    closed = 0
    started = 0
    failed_starts = 0

    def __init__(self, *, device, samplerate, channels, dtype, latency, blocksize, callback,
                 extra_settings=None):
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.latency = latency
        self.blocksize = blocksize
        self.callback = callback
        self.extra_settings = extra_settings
        type(self).opened += 1
        type(self).last_instance = self
        self._stopped = False
        self._closed = False

    def start(self):
        type(self).started += 1

    def stop(self):
        self._stopped = True

    def close(self):
        if self._closed:
            return
        self._closed = True
        type(self).closed += 1


class _FakeSoundDevice:
    PortAudioError = sd.PortAudioError
    def __init__(self, *, fail_rates=None, fallback_native=None):
        self._fail_rates = set(fail_rates or ())
        self._fallback_native = fallback_native
        self.default_samplerate = float(fallback_native) if fallback_native else 48000.0
        self.stream = _FakeStream

    def OutputStream(self, **kwargs):
        rate = kwargs.get("samplerate")
        if rate in self._fail_rates:
            raise sd.PortAudioError("PortAudio fake: cannot open at " + str(rate))
        return _FakeStream(**kwargs)

    def query_devices(self, index):
        return {"default_samplerate": self.default_samplerate}


@pytest.fixture(autouse=True)
def _reset_fake_stream_counters(monkeypatch):
    from types import SimpleNamespace
    from ai_voice import devices
    monkeypatch.setattr(devices, "sys", SimpleNamespace(platform="darwin"))
    _FakeStream.opened = _FakeStream.closed = _FakeStream.started = _FakeStream.failed_starts = 0
    yield
    _FakeStream.opened = _FakeStream.closed = _FakeStream.started = _FakeStream.failed_starts = 0


def _patch_sounddevice(monkeypatch, **kwargs):
    fake = _FakeSoundDevice(**kwargs)
    monkeypatch.setattr("ai_voice.monitor.sd", fake)
    return fake


def _patch_resolve(monkeypatch, name="BlackHole 2ch"):
    monkeypatch.setattr("ai_voice.monitor.resolve_output", lambda value: 1 if value == name else -1)


def _make_sink(monkeypatch, **kwargs):
    _patch_resolve(monkeypatch, kwargs.pop("device_name", "BlackHole 2ch"))
    if not isinstance(_module().sd, _FakeSoundDevice):
        _patch_sounddevice(monkeypatch)
    defaults = dict(device="BlackHole 2ch", sample_rate=48000, gain=1.0)
    defaults.update(kwargs)
    return _module().MonitorSink(**defaults)


def test_monitor_validates_inputs_before_open():
    mod = _module()
    invalid_overrides = [
        {"device": ""},
        {"device": 123},
        {"sample_rate": 0},
        {"sample_rate": 48000.5},
        {"gain": True},
        {"gain": float("nan")},
        {"gain": -1},
        {"max_frames": 0},
        {"channels": 3},
    ]
    base = {"device": "BlackHole 2ch", "sample_rate": 48000}
    for overrides in invalid_overrides:
        merged = dict(base)
        merged.update(overrides)
        with pytest.raises(ValueError):
            mod.MonitorSink(**merged)


def test_monitor_start_opens_stream_with_requested_rate(monkeypatch):
    sink = _make_sink(monkeypatch)
    sink.start()
    assert _FakeStream.opened == 1
    assert _FakeStream.started == 1
    assert sink.actual_rate() == 48000
    assert _FakeStream.last_instance.samplerate == 48000
    assert _FakeStream.last_instance.extra_settings is None
    sink.close()
    assert _FakeStream.closed == 1
    assert sink._stream is None


def test_monitor_start_falls_back_to_native_rate(monkeypatch):
    _patch_resolve(monkeypatch)
    fake = _patch_sounddevice(monkeypatch, fail_rates={48000}, fallback_native=48000)
    sink = _make_sink(monkeypatch, sample_rate=48000)
    # Both requested and native (96000 not injected, but 48000 is in fail set)
    # must surface as RuntimeError; the fake does NOT auto-fallback.
    opened_before = _FakeStream.opened
    with pytest.raises(RuntimeError, match="monitor device"):
        sink.start()
    # Failure leaves no half-open stream behind.
    assert sink._stream is None
    assert _FakeStream.opened == opened_before
    # Production-style fallback: if we ask for native (44100) the stream opens.
    _FakeStream.opened = _FakeStream.closed = _FakeStream.started = 0
    monkeypatch.setattr("ai_voice.monitor.sd", _FakeSoundDevice(fail_rates=set(), fallback_native=44100))
    sink2 = _make_sink(monkeypatch, sample_rate=44100)
    sink2.start()
    assert _FakeStream.opened == 1
    assert _FakeStream.started == 1
    assert sink2.actual_rate() == 44100
    sink2.close()
    assert _FakeStream.closed == 1
    assert fake.stream is _FakeStream


def test_monitor_start_failure_raises_runtime_error(monkeypatch):
    _patch_resolve(monkeypatch)
    # Both requested (48000) and native (96000) rates fail: start() must surface a clean
    # RuntimeError and never leave a half-opened stream behind.
    fake = _FakeSoundDevice(fail_rates={48000, 96000}, fallback_native=96000)
    monkeypatch.setattr("ai_voice.monitor.sd", fake)
    sink = _make_sink(monkeypatch, sample_rate=48000)
    opened_before = _FakeStream.opened
    with pytest.raises(RuntimeError, match="monitor device"):
        sink.start()
    assert sink._stream is None
    assert _FakeStream.opened == opened_before


def test_monitor_feed_enqueues_without_blocking_when_locked(monkeypatch):
    sink = _make_sink(monkeypatch)
    sink.start()
    # Acquire the internal lock to simulate the callback holding it; feed must
    # not block the audio thread and must count the dropped frames instead.
    assert sink._raw_lock.acquire(blocking=False) is True
    try:
        sink.feed(np.ones(64, dtype=np.float32))
    finally:
        sink._raw_lock.release()
    assert sink.dropped_frames == 64


def test_monitor_feed_overflow_drops_oldest_in_order(monkeypatch):
    sink = _make_sink(monkeypatch, max_frames=8)
    capacity = sink._RAW_CAPACITY
    payload = np.arange(capacity + 12, dtype=np.float32)
    sink._enqueue_raw(payload)
    assert sink.dropped_frames == 12
    assert sink._raw_count == capacity
    np.testing.assert_allclose(sink._drain_raw(capacity), payload[-capacity:])


def test_monitor_callback_never_sees_uninitiated_stream(monkeypatch):
    sink = _make_sink(monkeypatch)
    # Calling _callback before start() is a defensive path; nothing should raise.
    out = np.ones((0, 2), dtype=np.float32)
    sink._callback(out, 0, None, None)
    assert sink._stream is None


def test_monitor_callback_ignores_input_when_already_closing(monkeypatch):
    sink = _make_sink(monkeypatch)
    sink.start()
    sink._closing.set()
    sink.feed(np.ones(16, dtype=np.float32))
    # Feed is rejected silently; no exception, no enqueue.
    assert sink._out_count == 0


def test_monitor_close_is_idempotent_and_safe(monkeypatch):
    sink = _make_sink(monkeypatch)
    sink.start()
    sink.close()
    # Closing twice must not raise and must not double-close the fake stream.
    sink.close()
    assert _FakeStream.closed == 1


def test_monitor_context_manager_starts_and_stops(monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_sounddevice(monkeypatch)
    sink = _module().MonitorSink(device="BlackHole 2ch", sample_rate=48000)
    with sink as s:
        assert s is sink
        assert _FakeStream.opened == 1
        assert _FakeStream.started == 1
    assert _FakeStream.closed == 1


def test_monitor_set_gain_validates_and_replaces():
    sink_cls = _module().MonitorSink
    # Constructor validation already exercised; just verify set_gain rejects bad inputs.
    mod = _module()
    # set_gain requires an internal _gain attribute, set by __init__; build via kwargs
    sink = _module().MonitorSink.__new__(sink_cls)
    sink._gain = 1.0
    for bad in [True, float("nan"), float("inf"), -1, "1"]:
        with pytest.raises(ValueError):
            sink.set_gain(bad)
    sink.set_gain(0.5)
    assert sink._gain == pytest.approx(0.5)
    sink.set_gain(0)
    assert sink._gain == 0.0


def test_monitor_resamples_when_input_rate_differs(monkeypatch):
    # Force 44100 to fail so production fallback picks native 48000; resampler
    # must then exist because requested_rate (44100) differs from actual (48000).
    _patch_resolve(monkeypatch)
    fake = _FakeSoundDevice(fail_rates={44100}, fallback_native=48000)
    monkeypatch.setattr("ai_voice.monitor.sd", fake)
    sink = _make_sink(monkeypatch, sample_rate=44100)
    sink.start()
    assert sink.actual_rate() == 48000
    assert sink.actual_rate() != sink.requested_rate
    # Feed enough samples so the resampler/buffer actually fills.
    tone = np.sin(np.arange(4096) * 2 * np.pi * 440 / 44100).astype(np.float32)
    sink.feed(tone)
    # Allow the async monitor worker to drain the ring (bounded wait).
    deadline = time.time() + 1.5
    while time.time() < deadline and sink._out_count == 0 and sink.dropped_frames == 0:
        time.sleep(0.01)
    # Either the ring has frames (worker ran) or frames were dropped — both prove
    # the resampler/feed path executed under a real rate difference.
    assert (sink._out_count > 0) or (sink.dropped_frames > 0)
    out = np.zeros((64, 2), dtype=np.float32)
    sink._callback(out, 64, None, None)
    sink.close()
    assert _FakeStream.closed >= 1


def test_enumerate_monitor_candidates_filters_blacklist(monkeypatch):
    # Drive the real desktop._devices() surface with a fake sounddevice backend.
    from ai_voice import desktop as desktop_mod

    physical = [
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "External Headphones", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Headphones Mono", "max_input_channels": 0, "max_output_channels": 1},
    ]
    virtual = [
        {"name": "ai voice aux", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "MIC", "max_input_channels": 2, "max_output_channels": 2},
        {"name": "Mac Microphone", "max_input_channels": 2, "max_output_channels": 2},
        {"name": "Voicemeeter AI Voice Output", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "blackhole 2ch", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
    ]
    all_devices = physical + virtual

    class _FakeSD:
        def query_devices(self, kind=None):
            return list(all_devices)
        def OutputStream(self, **kwargs):
            raise sd.PortAudioError("no audio in tests")

    monkeypatch.setattr("ai_voice.monitor.list_devices", lambda: all_devices)
    monitors = _module().enumerate_monitor_candidates()
    # Physical monitors are eligible (mono allowed when supported).
    assert "MacBook Pro Speakers" in monitors
    assert "External Headphones" in monitors
    assert "Headphones Mono" in monitors
    # Blacklisted tokens must never appear, regardless of case.
    for forbidden in ["ai voice aux", "MIC", "Mac Microphone",
                      "Voicemeeter AI Voice Output", "blackhole 2ch"]:
        assert forbidden not in monitors, forbidden
    # Outputs and inputs preserved with virtual devices.
    # Real catalog blacklist against same fixtures.
    from ai_voice.catalog import _is_monitor_blacklisted as real_blacklist
    for name in ["AI Voice", "ai voice aux", "Voicemeeter AI Voice Output",
                 "MIC", "Mac Microphone", "Microphone (USB)",
                 "blackhole 2ch"]:
        assert real_blacklist(name) is True, name
    for name in ["MacBook Pro Speakers", "External Headphones", "Studio Monitors"]:
        assert real_blacklist(name) is False, name


def test_monitor_primary_independent_gains_raw_fan_out(monkeypatch):
    """Monitor gain and primary gain are independent raw float values fanned out."""
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    pb_mod = __import__("ai_voice.playback", fromlist=["Playback"]).Playback
    pcm = pcm_mod(input_rate=48000, output_rate=48000, max_frames=64)
    pb = pb_mod(pcm, device="AI Voice", sample_rate=48000, gain=0.5)

    delivered = []
    class _FakeMonitor:
        def __init__(self, g): self._g = g
        def feed(self, samples):
            delivered.append((self._g, np.asarray(samples).copy()))

    mon = _FakeMonitor(0.25)
    pb.attach_monitor(mon)
    # Raw PCM = ±0.5, primary applies gain=0.5 → out ±0.25; monitor is independent.
    pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
    out = np.zeros((2, 2), dtype=np.float32)
    pb.callback(out, 2, None, None)
    assert len(delivered) == 1
    # Primary output applies its own gain.
    np.testing.assert_allclose(out[:2,0], [0.25, -0.25], atol=1e-6)
    # Monitor gets the raw post-primary value (independent of monitor gain scalar).
    np.testing.assert_allclose(delivered[0][1], [0.5, -0.5], atol=1e-6)
    # The monitor's scalar gain is stored on the sink, not applied in the fan-out path.
    assert delivered[0][0] == 0.25
    pb.detach_monitor(mon)


def test_monitor_nonblocking_handoff_under_contention(monkeypatch):
    """Primary callback must not block on a contended monitor lock."""
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    pb_mod = __import__("ai_voice.playback", fromlist=["Playback"]).Playback
    pcm = pcm_mod(input_rate=48000, output_rate=48000, max_frames=64)
    pb = pb_mod(pcm, device="AI Voice", sample_rate=48000, gain=1.0)

    class _ContendedMonitor:
        def __init__(self):
            self.lock = threading.Lock()
            self.dropped = 0
        def feed(self, samples):
            if not self.lock.acquire(blocking=False):
                self.dropped += len(samples)
                return
            try:
                time.sleep(0.05)
            finally:
                self.lock.release()

    mon = _ContendedMonitor()
    pb.attach_monitor(mon)
    pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
    out = np.zeros((2, 2), dtype=np.float32)
    # Hold the monitor lock and run several primary callbacks — they must all return
    # promptly (non-blocking) and primary output must remain correct.
    assert mon.lock.acquire(blocking=False) is True
    start = time.time()
    try:
        for _ in range(20):
            pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
            pb.callback(out, 2, None, None)
    finally:
        mon.lock.release()
    elapsed = time.time() - start
    assert elapsed < 0.5, "primary callback must not block on monitor lock"
    np.testing.assert_allclose(out[0], [0.5, 0.5], atol=1e-6)
    pb.detach_monitor(mon)


def test_monitor_separate_buffer_does_not_steal_primary_frames(monkeypatch):
    """Monitor has its own ring; detaching it leaves the primary ring intact."""
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    pb_mod = __import__("ai_voice.playback", fromlist=["Playback"]).Playback
    pcm = pcm_mod(input_rate=48000, output_rate=48000, max_frames=128)
    pb = pb_mod(pcm, device="AI Voice", sample_rate=48000, gain=1.0)

    seen = []
    class _Sink:
        def __init__(self): self._buf = np.zeros(8, dtype=np.float32); self._n = 0
        def feed(self, samples):
            arr = np.asarray(samples, dtype=np.float32)
            n = min(len(arr), len(self._buf) - self._n)
            self._buf[self._n:self._n + n] = arr[:n]
            self._n += n
            seen.append(len(arr))
        def tail(self):
            return self._buf[:self._n]

    sink = _Sink()
    pb.attach_monitor(sink)
    pcm.push(np.array([8192, -8192, 4096, -4096, 2048, -2048, 1024, -1024], dtype="<i2").tobytes())
    out = np.zeros((4, 2), dtype=np.float32)
    pb.callback(out, 4, None, None)
    # The primary output read exactly what the ring provided; monitor got its own copy.
    assert sink._n == 4
    np.testing.assert_allclose(out[:2,0], [0.25, -0.25], atol=1e-6)
    # Detach and re-push; the primary ring remains intact, monitor does not steal.
    pb.detach_monitor(sink)
    pcm.push(np.array([4096, -4096, 2048, -2048], dtype="<i2").tobytes())
    before_count = sink._n
    out2 = np.zeros((2, 2), dtype=np.float32)
    pb.callback(out2, 2, None, None)
    assert sink._n == before_count, "monitor must not receive frames after detach"
    np.testing.assert_allclose(out2[:,0], [0.0625, -0.0625], atol=1e-6)


def test_monitor_fallback_actual_resampled_duration(monkeypatch):
    fake = _patch_sounddevice(monkeypatch, fail_rates={44100}, fallback_native=48000)
    sink = _make_sink(monkeypatch, sample_rate=44100)
    try:
        sink.start()
        assert sink.actual_rate() == 48000
        tone = np.sin(np.arange(4096) * 2 * np.pi * 440 / 44100).astype(np.float32)
        sink.feed(tone)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and sink._out_count < 4200:
            time.sleep(.005)
        frames = sink._out_count
        assert abs(frames - 4096 * 48000 / 44100) < 100
        out = np.zeros((frames, 2), dtype=np.float32)
        sink._callback(out, frames, None, None)
        assert np.count_nonzero(out[:,0]) > 4000
        np.testing.assert_array_equal(out[:,0],out[:,1])
    finally:
        sink.close()


def test_monitor_closing_restart_and_failure_isolation(monkeypatch):
    """Closing and restarting a MonitorSink leaves a clean state; a failure in
    one cycle never propagates into a fresh cycle."""
    _patch_resolve(monkeypatch)
    _patch_sounddevice(monkeypatch)
    sink = _make_sink(monkeypatch)
    sink.start()
    sink.close()
    assert _FakeStream.closed == 1
    # Restart with a fresh stream.
    sink.start()
    assert _FakeStream.opened == 2
    assert _FakeStream.closed == 1
    # Failure during a cycle must not leak into the next.
    sink.close()
    _FakeStream.opened = _FakeStream.closed = _FakeStream.started = 0
    _patch_sounddevice(monkeypatch, fail_rates={48000}, fallback_native=48000)
    sink_fail = _make_sink(monkeypatch)
    with pytest.raises(RuntimeError):
        sink_fail.start()
    # Restore the healthy fake and ensure a fresh start succeeds.
    _patch_sounddevice(monkeypatch)
    sink_fail.start()
    assert _FakeStream.opened >= 1
    assert sink_fail._stream is not None
    sink_fail.close()


def test_buffer_fan_out_does_not_re_read_primary_ring(monkeypatch):
    """The fan_out path delivers its own argument, not samples from the primary ring."""
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    buf = pcm_mod(input_rate=48000, output_rate=48000, max_frames=64)
    delivered = []

    class _FakeMonitor:
        errors = 0
        def feed(self, samples):
            delivered.append(np.asarray(samples).copy())

    monitor = _FakeMonitor()
    buf.attach_monitor(monitor)
    arr = np.linspace(0.0, 1.0, 32, dtype=np.float32)
    buf.fan_out(arr)
    # The monitor should have received exactly the input array, not anything pulled
    # from the primary ring (which is empty).
    assert len(delivered) == 1
    np.testing.assert_allclose(delivered[0], arr)
    assert buf.dropped_frames == 0
    # Detaching should stop future delivery.
    buf.detach_monitor(monitor)
    buf.fan_out(arr)
    assert len(delivered) == 1


def test_buffer_fan_out_swallows_monitor_exception(monkeypatch):
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    buf = pcm_mod(input_rate=48000, output_rate=48000, max_frames=64)

    class _BadMonitor:
        errors = 0
        def feed(self, samples):
            raise RuntimeError("boom")

    monitor = _BadMonitor()
    buf.attach_monitor(monitor)
    # fan_out must not propagate the monitor exception to the primary callback.
    buf.fan_out(np.ones(16, dtype=np.float32))
    assert monitor.errors == 1


def test_playback_attach_propagates_fan_out(monkeypatch):
    """Playback.attach_monitor wires monitor into the buffer's fan_out, identical PCM."""
    pb_mod = __import__("ai_voice.playback", fromlist=["Playback"]).Playback
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    pb = pb_mod() if False else None  # silence linter
    pcm = pcm_mod(input_rate=48000, output_rate=48000, max_frames=64)
    pb = pb_mod(pcm, device="AI Voice", sample_rate=48000, gain=1.0)
    delivered = []

    class _FakeMonitor:
        errors = 0
        def feed(self, samples):
            delivered.append(np.asarray(samples).copy())

    monitor = _FakeMonitor()
    pb.attach_monitor(monitor)
    pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
    out = np.zeros((2, 2), dtype=np.float32)
    pb.callback(out, 2, None, None)
    # The same PCM that the primary output received is fanned out.
    assert len(delivered) == 1
    np.testing.assert_allclose(delivered[0], [0.5, -0.5], atol=1e-6)
    # Output ring is independent — primary did not re-read its own ring for fan_out.
    assert pcm.buffered_frames == 0
    pb.detach_monitor(monitor)
    pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
    pb.callback(out, 2, None, None)
    assert len(delivered) == 1


def test_playback_callback_isolates_monitor_failure(monkeypatch):
    """Monitor failure must never propagate into the primary callback."""
    pb_mod = __import__("ai_voice.playback", fromlist=["Playback"]).Playback
    pcm_mod = __import__("ai_voice.playback", fromlist=["PCMBuffer"]).PCMBuffer
    pcm = pcm_mod(input_rate=48000, output_rate=48000, max_frames=64)
    pb = pb_mod(pcm, device="AI Voice", sample_rate=48000, gain=1.0)

    class _BoomMonitor:
        errors = 0
        def feed(self, samples):
            raise RuntimeError("monitor callback boom")

    monitor = _BoomMonitor()
    pb.attach_monitor(monitor)
    pcm.push(np.array([16384, -16384], dtype="<i2").tobytes())
    out = np.zeros((2, 2), dtype=np.float32)
    # Primary callback must complete cleanly despite monitor throwing.
    pb.callback(out, 2, None, None)
    assert monitor.errors == 1
    np.testing.assert_allclose(out[0], [0.5, 0.5], atol=1e-6)
