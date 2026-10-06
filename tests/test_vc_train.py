"""Offline supervisor tests with real lightweight subprocesses, no ML imports."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from ai_voice.vc_store import ROOT, VcStore, VcStoreError
from ai_voice.i18n import t
from ai_voice.vc_train import STAGES, VcTrainer, VcTrainError


@pytest.fixture
def voice(tmp_path):
    store = VcStore(tmp_path / 'voices')
    directory = store.root / 'voice'
    (directory / 'dataset').mkdir(parents=True)
    (directory / 'dataset/clip.wav').write_bytes(b'audio')
    (directory / 'reference.wav').write_bytes(b'reference')
    store._write(directory, dict(id='voice', name='Voice', kind='zeroshot',
                                status='ready', can_train=True, speech_seconds=180))
    return store, directory


def runner(tmp_path, body):
    path = tmp_path / 'runner.py'
    path.write_text('import sys, json, time, os\nfrom pathlib import Path\n'
                    'directory = Path(sys.argv[sys.argv.index("--voice-dir") + 1])\n'
                    'def emit(**event):\n    print(json.dumps(event), flush=True)\n' + body)
    return path


def trainer(voice, tmp_path, body='time.sleep(60)\n', **kwargs):
    store, _ = voice
    real_popen = kwargs.pop('popen', subprocess.Popen)
    def offline_popen(args, **options):
        # Exercise the supervisor's exact command contract without macOS policy
        # changing the QA runner's scheduling or requiring its sandbox entitlement.
        assert args[:3] == ['/usr/sbin/taskpolicy', '-c', 'utility']
        return real_popen(args[3:], **options)
    return VcTrainer(store, python_path=sys.executable,
                     runner_path=runner(tmp_path, body), epochs=4, popen=offline_popen, **kwargs)


def finished(train):
    assert train._job['finished'].wait(8), train.status()
    return train.status()


SUCCESS = '''
for stage in ('prepare', 'pitch', 'features'):
    emit(stage=stage, progress=1)
for epoch in range(1, 5):
    emit(stage='train', epoch=epoch, seconds=100 if epoch == 1 else 2)
output = directory / 'train-out'
output.mkdir()
(output / 'model.pth').write_bytes(b'model')
(output / 'model.index').write_bytes(b'index')
emit(stage='index', progress=1)
print('diagnostic', file=sys.stderr, flush=True)
'''


def test_success(voice, tmp_path):
    done = []
    train = trainer(voice, tmp_path, SUCCESS, on_done=lambda *args: done.append(args))
    assert train.status() is None
    train.start('voice')
    assert finished(train)['state'] == 'done'
    store, directory = voice
    assert store.get('voice')['kind'] == 'trained'
    assert store.get('voice')['status'] == 'ready'
    assert store.get('voice')['sample_rate'] == 48000
    assert (directory / 'model.pth').read_bytes() == b'model'
    assert (directory / 'model.index').read_bytes() == b'index'
    assert (directory / 'reference.wav').read_bytes() == b'reference'
    assert not (directory / 'dataset').exists()
    assert not (directory / 'train-out').exists()
    assert 'diagnostic' in (directory / 'train.log').read_text()
    for _ in range(100):
        if done:
            break
        time.sleep(.001)
    assert done == [('voice', 'done')]
    train.cancel()


def test_events_progress_eta(voice, tmp_path):
    train = trainer(voice, tmp_path)
    train.start('voice')
    job = train._job
    try:
        for stage, (_, base, weight) in STAGES.items():
            if stage == 'train':
                continue
            if stage == 'index':
                break
            train._event(job, dict(stage=stage, progress=.5))
            status = train.status()
            assert status['stage_label'] == t(STAGES[stage][0])
            assert status['progress'] == pytest.approx(base + weight * .5)
        for epoch, seconds in [(1, 100), (2, 2), (3, 4)]:
            train._event(job, dict(stage='train', epoch=epoch, seconds=seconds))
            assert train.status()['progress'] == pytest.approx(.25 + .7 * epoch / 4)
            assert train.status()['stage_label'] == f'Training (epoch {epoch}/4)'
            if epoch == 1:
                assert train.status()['eta_s'] is None
        assert train.status()['eta_s'] == 3
        train._event(job, dict(stage='index', progress=.5))
        assert train.status()['progress'] == pytest.approx(.975)
        status = train.status()
        status['state'] = 'corrupted'
        assert train.status()['state'] == 'running'
    finally:
        train.cancel()


def test_already_running(voice, tmp_path):
    train = trainer(voice, tmp_path)
    train.start('voice')
    try:
        with pytest.raises(VcTrainError, match='Training is already running'):
            train.start('voice')
    finally:
        train.cancel()


@pytest.mark.parametrize('change, message', [('kind', 'quick'), ('dataset', 'dataset'),
                                           ('speech', '3 minutes'), ('can_train', '3 minutes')])
def test_refusals(voice, tmp_path, change, message):
    store, directory = voice
    meta = store.get('voice')
    if change == 'kind':
        meta['kind'] = 'imported'
    elif change == 'dataset':
        import shutil
        shutil.rmtree(directory / 'dataset')
    elif change == 'speech':
        meta['speech_seconds'] = 179
    else:
        meta['can_train'] = False
    store._write(directory, meta)
    train = trainer(voice, tmp_path)
    with pytest.raises(VcStoreError, match=message):
        train.start('voice')
    assert train.status() is None


def test_failed(voice, tmp_path):
    train = trainer(voice, tmp_path, 'print("/secret/path", file=sys.stderr)\nsys.exit(3)\n')
    train.start('voice')
    status = finished(train)
    assert status['state'] == 'failed'
    assert '/secret' not in status['error']
    assert voice[0].get('voice')['kind'] == 'zeroshot'
    assert (voice[1] / 'dataset').exists()
    assert '/secret/path' in (voice[1] / 'train.log').read_text()


def test_memory_limit(voice, tmp_path):
    train = trainer(voice, tmp_path, rss_measure=lambda pid: 7 * 1024 ** 3)
    train.start('voice')
    status = finished(train)
    assert status['state'] == 'failed'
    assert status['error'] == 'The 6 GB memory limit was exceeded'
    assert train._job['process'].poll() is not None
    assert voice[0].get('voice')['kind'] == 'zeroshot'


def test_cancel_reaped_and_restart(voice, tmp_path):
    train = trainer(voice, tmp_path, '''
(directory / 'train-work').mkdir()
(directory / 'train-work/temp').write_bytes(b'x')
emit(stage='prepare', progress=.5)
time.sleep(60)
''')
    train.cancel()
    train.start('voice')
    pid = train._job['process'].pid
    for _ in range(100):
        if (voice[1] / 'train-work/temp').exists():
            break
        time.sleep(.01)
    train.cancel()
    train.cancel()
    assert train.status()['state'] == 'cancelled'
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not (voice[1] / 'train-work').exists()
    assert (voice[1] / 'dataset').exists()
    assert voice[0].get('voice')['kind'] == 'zeroshot'
    train.start('voice')
    train.cancel()


def test_deleted_voice_cancelled(voice, tmp_path):
    train = trainer(voice, tmp_path, SUCCESS.replace("for stage", "time.sleep(.2)\nfor stage", 1))
    train.start('voice')
    voice[0].delete('voice')
    assert finished(train)['state'] == 'cancelled'
    assert not voice[1].exists()


def test_rename_during_training_keeps_going(voice, tmp_path):
    train = trainer(voice, tmp_path, SUCCESS.replace("for stage", "time.sleep(.2)\nfor stage", 1))
    train.start('voice')
    voice[0].rename('voice', 'Renamed')
    assert finished(train)['state'] == 'done'
    meta = voice[0].get('voice')
    assert meta['kind'] == 'trained'
    assert meta['name'] == 'Renamed'


def test_popen_contract(voice, tmp_path):
    calls = []
    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.Popen(args, **kwargs)
    train = trainer(voice, tmp_path, SUCCESS, popen=popen)
    train.start('voice')
    assert finished(train)['state'] == 'done'
    args, kwargs = calls[0]
    assert args[:4] == ['nice', '-n', '15', sys.executable]
    assert kwargs['start_new_session'] is True
    assert kwargs['shell'] is False
    assert kwargs['env']['SYSTEM_VERSION_COMPAT'] == '0'
    assert kwargs['env']['OMP_NUM_THREADS'] == '4'
    assert kwargs['stdout'] == subprocess.PIPE
    assert kwargs['stderr'].name == str(voice[1] / 'train.log')


@pytest.mark.parametrize('skip, expected', [(False, 'failed'), (True, 'done')])
def test_missing_index_requires_explicit_skip(voice, tmp_path, skip, expected):
    body = SUCCESS.replace("(output / 'model.index').write_bytes(b'index')", '')
    if skip:
        body = body.replace("emit(stage='index', progress=1)",
                            "emit(stage='index', progress=1, skipped='faiss_unavailable')")
    train = trainer(voice, tmp_path, body)
    train.start('voice')
    assert finished(train)['state'] == expected


@pytest.mark.parametrize('content', [None, b''])
def test_mark_trained_rejects_missing_empty_model(voice, content):
    store, directory = voice
    if content is not None:
        (directory / 'model.pth').write_bytes(content)
    with pytest.raises(VcStoreError, match='not found'):
        store.mark_trained('voice')
    assert store.get('voice')['kind'] == 'zeroshot'


def test_mark_trained_atomic_failure(voice, monkeypatch):
    store, directory = voice
    (directory / 'model.pth').write_bytes(b'model')
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        store.mark_trained('voice')
    assert store.get('voice')['kind'] == 'zeroshot'
    assert not list(directory.glob('.meta-*'))


def test_no_torch_import():
    subprocess.run([sys.executable, '-c',
                    'import sys; from ai_voice.vc_train import VcTrainer; '
                    'assert "torch" not in sys.modules'], check=True,
                   env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, timeout=10)


def test_injected_clock_eta(voice, tmp_path):
    now = [0.]
    train = trainer(voice, tmp_path, clock=lambda: now[0])
    train.start('voice')
    try:
        for epoch, timestamp in [(1, 100), (2, 102), (3, 106)]:
            now[0] = timestamp
            train._event(train._job, dict(stage='train', epoch=epoch))
        assert train.status()['eta_s'] == 3
    finally:
        train.cancel()


def test_popen_failure(voice, tmp_path):
    def fail(*args, **kwargs):
        raise OSError('/private/secret')
    train = trainer(voice, tmp_path, popen=fail)
    with pytest.raises(VcTrainError, match='start') as error:
        train.start('voice')
    assert '/private' not in str(error.value)
    assert train.status() is None


def test_publish_meta_failure_rolls_back(voice, tmp_path, monkeypatch):
    train = trainer(voice, tmp_path, SUCCESS)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(voice[0], 'mark_trained', fail)
    train.start('voice')
    assert finished(train)['state'] == 'failed'
    assert voice[0].get('voice')['kind'] == 'zeroshot'
    assert not (voice[1] / 'model.pth').exists()
    assert not (voice[1] / 'model.index').exists()
    assert (voice[1] / 'dataset').exists()


def test_cancel_kills_descendant(voice, tmp_path):
    train = trainer(voice, tmp_path, '''
import subprocess, signal
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def terminate(signum, frame):
    child.wait()
    sys.exit(0)
signal.signal(signal.SIGTERM, terminate)
(directory / 'child-pid').write_text(str(child.pid))
try:
    time.sleep(60)
finally:
    child.wait()
''')
    train.start('voice')
    for _ in range(100):
        if (voice[1] / 'child-pid').exists():
            break
        time.sleep(.01)
    child_pid = int((voice[1] / 'child-pid').read_text())
    train.cancel()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert train._job['process'].poll() is not None


@pytest.mark.parametrize('model_content', [None, b''])
def test_empty_missing_model_fails(voice, tmp_path, model_content):
    body = SUCCESS
    body = body.replace("(output / 'model.pth').write_bytes(b'model')",
                        '' if model_content is None else "(output / 'model.pth').write_bytes(b'')")
    train = trainer(voice, tmp_path, body)
    train.start('voice')
    assert finished(train)['state'] == 'failed'
    assert voice[0].get('voice')['kind'] == 'zeroshot'


def test_pause_uses_process_group_and_excludes_time(voice, tmp_path, monkeypatch):
    import signal
    now = [0.]
    train = trainer(voice, tmp_path, clock=lambda: now[0])
    train.start('voice')
    actual = os.killpg
    calls = []
    def killpg(pid, sig):
        calls.append(sig)
        actual(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    try:
        now[0] = 1
        train._event(train._job, dict(stage='train', epoch=1))
        assert train.pause() is True
        assert train.status()['state'] == 'paused'
        assert train.status()['stage_label'] == 'Paused: Live voice is running'
        now[0] = 101
        train.resume()
        now[0] = 103
        train._event(train._job, dict(stage='train', epoch=2))
        assert train.status()['eta_s'] == 4
        assert calls[:2] == [signal.SIGSTOP, signal.SIGCONT]
    finally:
        train.cancel()


def second_voice(voice, voice_id='second'):
    store, directory = voice
    target = store._directory(voice_id)
    (target / 'dataset').mkdir(parents=True)
    (target / 'dataset/clip.wav').write_bytes(b'audio')
    meta = dict(store.get(directory.name), id=voice_id, name=voice_id)
    store._write(target, meta)
    return voice_id


@pytest.mark.parametrize('outcome', ['done', 'failed', 'cancelled'])
def test_queue_starts_next_on_all_outcomes(voice, tmp_path, outcome):
    other = second_voice(voice)
    if outcome == 'done':
        body = 'time.sleep(.15)\n' + SUCCESS
    elif outcome == 'failed':
        body = "time.sleep(.15)\nif directory.name == 'voice': sys.exit(3)\n" + SUCCESS
    else:
        body = "if directory.name == 'voice': time.sleep(60)\n" + SUCCESS
    done = []
    train = trainer(voice, tmp_path, body, on_done=lambda *args: done.append(args))
    try:
        train.start('voice')
        train.enqueue(other, 'fast')
        assert train.status()['queue'][0]['epochs'] == 50
        with pytest.raises(VcTrainError, match='already queued'):
            train.enqueue(other, 'normal')
        if outcome == 'cancelled':
            train.cancel('voice')
        deadline = time.monotonic() + 4
        while len(done) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert done == [('voice', outcome), (other, 'done')]
        assert train.status()['queue'] == []
        assert json.loads(train.queue_path.read_text())['queue'] == []
    finally:
        train.close()


def test_shutdown_preserves_current_first_and_checkpoint(voice, tmp_path):
    other = second_voice(voice)
    train = trainer(voice, tmp_path)
    train.start('voice')
    work = voice[1] / 'train-work'
    work.mkdir()
    (work / 'G_2333333.pth').write_bytes(b'g')
    (work / 'D_2333333.pth').write_bytes(b'd')
    train.enqueue(other, 'fast')
    train.close()
    saved = json.loads(train.queue_path.read_text())
    assert saved['queue'] == [dict(voice_id='voice', epochs=4), dict(voice_id=other, epochs=50)]
    assert saved['user_paused'] is False
    assert (work / 'G_2333333.pth').read_bytes() == b'g'
    assert not train.pid_path.exists()
    resumed = trainer(voice, tmp_path)
    try:
        assert resumed.status()['voice_id'] == 'voice'
        assert resumed.status()['queue'][0]['voice_id'] == other
        assert (work / 'G_2333333.pth').is_file()
        resumed._event(resumed._job, dict(stage='train', epoch=2, resumed=True))
        assert resumed.status()['epoch'] == 2
        assert resumed.status()['progress'] == pytest.approx(.6)
        assert resumed._job['train_epochs'] == 0
    finally:
        resumed.close()


def test_training_stats_last_three_and_prediction(voice, tmp_path):
    train = trainer(voice, tmp_path, SUCCESS)
    train.stats_path.write_text('[1, 2, 3]')
    assert train.estimate(180, 50) == 7
    train.start('voice')
    assert finished(train)['state'] == 'done'
    values = json.loads(train.stats_path.read_text())
    assert values[:2] == [2, 3] and len(values) == 3
    assert values[2] == pytest.approx((100 + 2 + 2 + 2) / 4 / 3)
    assert train.status()['k'] == pytest.approx(sum(values) / 3)


@pytest.mark.parametrize('matching', [True, False])
def test_orphan_pid_checked_and_group_killed(voice, tmp_path, monkeypatch, matching):
    import signal
    pid_path = voice[0].root.parent / 'vc-train.pid'
    pid_path.write_text('34567')
    calls = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: calls.append(('probe', pid, sig)))
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: calls.append(('group', pid, sig)))
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout='python train_runner.py' if matching else 'other_app'))
    train = trainer(voice, tmp_path, sleep=lambda seconds: None)
    groups = [call[2] for call in calls if call[0] == 'group']
    assert groups == ([signal.SIGTERM, signal.SIGCONT, signal.SIGKILL] if matching else [])
    assert not pid_path.exists()
    train.close()


def test_pid_write_failure_reaps_launched_process(voice, tmp_path, monkeypatch):
    processes = []
    def popen(args, **kwargs):
        process = subprocess.Popen(args, **kwargs)
        processes.append(process)
        return process
    train = trainer(voice, tmp_path, popen=popen)
    original = Path.write_text
    def fail(path, *args, **kwargs):
        if path == train.pid_path:
            raise OSError('disk full')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail)
    with pytest.raises(VcTrainError, match='save training state'):
        train.start('voice')
    assert processes[0].poll() is not None
    assert train.status() is None
    assert not train.pid_path.exists()


def test_paused_queue_does_not_show_completed_job_as_current(voice, tmp_path):
    other = second_voice(voice)
    train = trainer(voice, tmp_path, SUCCESS)
    train.start('voice')
    assert finished(train)['state'] == 'done'
    train.pause()
    train.enqueue(other, 'normal')
    try:
        assert train.status()['state'] == 'paused'
        assert train.status()['voice_id'] is None
        assert train.status()['queue'][0]['voice_id'] == other
    finally:
        train.close()


@pytest.mark.parametrize('iteration', [49, 50, 90])
def test_resume_checkpoint_with_smaller_preset(tmp_path, monkeypatch, iteration):
    import runpy
    from types import SimpleNamespace
    # Loading the runner's definitions must not change the test process env.
    with monkeypatch.context() as env:
        for key in ('SYSTEM_VERSION_COMPAT', 'CUDA_VISIBLE_DEVICES', 'HF_HUB_OFFLINE',
                    'TRANSFORMERS_OFFLINE', 'PYTORCH_ENABLE_MPS_FALLBACK'):
            env.setenv(key, os.environ.get(key, ''))
        resume_epoch = runpy.run_path(str(ROOT / 'vc_worker/train_runner.py'))['resume_epoch']
    work = tmp_path / 'train-work'
    work.mkdir()
    for name in ('G_1.pth', 'G_2333333.pth', 'D_2333333.pth'):
        (work / name).write_bytes(b'checkpoint')
    (work / 'filelist.txt').write_text('dataset')
    loads = []
    def load(path, **options):
        loads.append((Path(path).name, options))
        return {'iteration': iteration}
    torch = SimpleNamespace(load=load)
    utils = SimpleNamespace(latest_checkpoint_path=lambda *args: str(work / 'G_2333333.pth'))
    assert resume_epoch(work, 50, torch, utils) == (48 if iteration == 49 else 0)
    assert bool(list(work.glob('[GD]_*.pth'))) == (iteration < 50)
    assert (work / 'filelist.txt').read_text() == 'dataset'
    assert loads == [('G_2333333.pth', dict(map_location='cpu', weights_only=True))]
    if iteration >= 50:
        assert resume_epoch(work, 50, torch, utils) == 0
        assert len(loads) == 1


@pytest.mark.parametrize('outcome', ['failed', 'cancelled'])
def test_checkpoint_cleanup_depends_on_outcome(voice, tmp_path, outcome):
    body = '''
work = directory / 'train-work'
work.mkdir()
(work / 'G_2333333.pth').write_bytes(b'broken-g')
(work / 'D_2333333.pth').write_bytes(b'broken-d')
emit(stage='prepare', progress=1)
'''
    body += 'sys.exit(3)\n' if outcome == 'failed' else 'time.sleep(60)\n'
    train = trainer(voice, tmp_path, body)
    try:
        train.start('voice')
        if outcome == 'cancelled':
            deadline = time.monotonic() + 2
            while not (voice[1] / 'train-work/D_2333333.pth').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (voice[1] / 'train-work/D_2333333.pth').exists()
            train.cancel()
        assert finished(train)['state'] == outcome
        assert (voice[1] / 'train-work').exists() == (outcome == 'cancelled')
        assert (voice[1] / 'dataset').exists()
        if outcome == 'failed':
            runner(tmp_path, "assert not (directory / 'train-work').exists()\n" + SUCCESS)
            train.start('voice')
            assert finished(train)['state'] == 'done'
    finally:
        train.close()


def test_overlapping_pause_owners_keep_process_and_queue_paused(voice, tmp_path, monkeypatch):
    import signal
    now = [0.]
    train = trainer(voice, tmp_path, clock=lambda: now[0])
    train.start('voice')
    other = second_voice(voice)
    train.enqueue(other, 'fast')
    actual = os.killpg
    calls = []
    def killpg(pid, sig):
        calls.append(sig)
        actual(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    try:
        now[0] = 1
        train._event(train._job, dict(stage='train', epoch=1))
        assert train.pause() is True  # Autotune owns the first pause.
        now[0] = 2
        assert train.pause() is True  # VC acquires an independent pause.
        now[0] = 10
        train.resume()  # Autotune completes; VC still owns its pause.
        assert train.status()['state'] == 'paused'
        assert not train._resumed.is_set()
        assert calls == [signal.SIGSTOP]
        now[0] = 101
        train.resume()
        now[0] = 103
        train._event(train._job, dict(stage='train', epoch=2, seconds=102))
        assert train.status()['eta_s'] == 4
        assert calls == [signal.SIGSTOP, signal.SIGCONT]
        train.resume()  # Unmatched release must not corrupt the next acquisition.
        assert train.pause() is True
        train.cancel()
        assert train.status()['state'] == 'paused'
        assert train.status()['voice_id'] is None
        assert train.status()['queue'][0]['voice_id'] == other
    finally:
        train.close()


def test_reader_counts_pause_at_line_receipt(voice, tmp_path, monkeypatch):
    now = [0.]
    received, release = threading.Event(), threading.Event()
    train = trainer(voice, tmp_path, '''
while not (directory / 'emit-now').exists(): time.sleep(.01)
emit(stage='train', epoch=2, seconds=2)
time.sleep(60)
''', clock=lambda: now[0])
    wait = train._resumed.wait
    def delay_reader(*args, **kwargs):
        received.set()
        assert release.wait(3)
        return wait(*args, **kwargs)
    monkeypatch.setattr(train._resumed, 'wait', delay_reader)
    train.start('voice')
    try:
        now[0] = 1
        train._event(train._job, dict(stage='train', epoch=1, seconds=1))
        now[0] = 3
        (voice[1] / 'emit-now').touch()
        assert received.wait(2)
        train.pause()
        now[0] = 103
        train.resume()
        release.set()
        deadline = time.monotonic() + 2
        while train.status()['epoch'] != 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert train.status()['epoch'] == 2
        assert train.status()['eta_s'] == 4
        assert train._job['train_seconds'] == 3
        assert train._job['last_time'] == 3
        now[0] = 105
        train._event(train._job, dict(stage='train', epoch=3, seconds=102))
        assert train.status()['eta_s'] == 2
        assert train._job['train_seconds'] == 5
        assert train._job['train_epochs'] == 3
    finally:
        release.set()
        train.close()


def test_user_pause_resume_signals_and_label(voice, tmp_path, monkeypatch):
    import signal
    now = [0.]
    train = trainer(voice, tmp_path, clock=lambda: now[0])
    train.start('voice')
    actual = os.killpg
    calls = []
    def killpg(pid, sig):
        calls.append(sig)
        actual(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    try:
        now[0] = 1
        train._event(train._job, dict(stage='train', epoch=1))
        status = train.user_pause()
        assert status['state'] == 'paused'
        assert status['stage_label'] == 'Paused'
        assert status['user_paused'] is True
        assert train.user_pause()['user_paused'] is True  # Идемпотентно.
        assert calls == [signal.SIGSTOP]
        status = train.user_resume()
        assert status['state'] == 'running'
        assert status['user_paused'] is False
        assert calls == [signal.SIGSTOP, signal.SIGCONT]
        assert train.user_resume()['user_paused'] is False
    finally:
        train.cancel()


def test_user_pause_survives_vc_resume(voice, tmp_path, monkeypatch):
    import signal
    train = trainer(voice, tmp_path)
    train.start('voice')
    actual = os.killpg
    calls = []
    def killpg(pid, sig):
        calls.append(sig)
        actual(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    try:
        train.user_pause()   # Пользователь ставит паузу.
        train.pause()        # Живой голос добавляет свою паузу.
        assert train.status()['state'] == 'paused'
        train.resume()       # Живой голос остановлен: обучение остаётся на паузе.
        assert train.status()['state'] == 'paused'
        assert train.status()['stage_label'] == 'Paused'
        assert calls == [signal.SIGSTOP]
        train.user_resume()
        assert train.status()['state'] == 'running'
        assert calls == [signal.SIGSTOP, signal.SIGCONT]
    finally:
        train.cancel()


def test_user_pause_persisted_and_restored(voice, tmp_path):
    other = second_voice(voice)
    train = trainer(voice, tmp_path)
    train.user_pause()
    train.enqueue(other, 'fast')
    saved = json.loads(train.queue_path.read_text())
    assert saved['user_paused'] is True
    train.close()
    restored = trainer(voice, tmp_path)
    try:
        status = restored.status()
        assert status['user_paused'] is True
        assert status['state'] == 'paused'
        assert status['voice_id'] is None  # Обучение не стартует, пока пауза не снята.
        assert status['queue'][0]['voice_id'] == other
        status = restored.user_resume()
        assert status['voice_id'] == other
        assert status['state'] == 'running'
    finally:
        restored.cancel()


def test_legacy_queue_file_without_user_paused(voice, tmp_path):
    other = second_voice(voice)
    train = trainer(voice, tmp_path)
    train.close()
    train.queue_path.write_text(json.dumps([dict(voice_id=other, epochs=50)]))
    restored = trainer(voice, tmp_path)
    try:
        status = restored.status()
        assert status['user_paused'] is False
        assert status['voice_id'] == other
    finally:
        restored.cancel()


def test_cancel_clears_user_pause_only_when_idle(voice, tmp_path):
    other = second_voice(voice)
    train = trainer(voice, tmp_path)
    train.start('voice')
    try:
        train.enqueue(other, 'fast')
        train.user_pause()
        train.cancel()  # Очередь ещё есть: пауза пользователя сохраняется.
        assert train.status()['user_paused'] is True
        assert train.status()['state'] == 'paused'
        train.cancel(other)  # Очередь пуста и задачи нет: пауза сброшена.
        assert train.status() is None or train.status()['user_paused'] is False
        assert json.loads(train.queue_path.read_text())['user_paused'] is False
    finally:
        train.close()


def test_dataset_scale_check_rejects_integer_clips(tmp_path, monkeypatch):
    import runpy
    import types
    import numpy as np
    with monkeypatch.context() as env:
        for key in ('SYSTEM_VERSION_COMPAT', 'CUDA_VISIBLE_DEVICES', 'HF_HUB_OFFLINE',
                    'TRANSFORMERS_OFFLINE', 'PYTORCH_ENABLE_MPS_FALLBACK'):
            env.setenv(key, os.environ.get(key, ''))
        check = runpy.run_path(str(ROOT / 'vc_worker/train_runner.py'))['check_dataset_scale']
    clips = [tmp_path / '0']
    for data in (np.array([0.5, -0.9], dtype=np.float32), np.array([16384, -3], dtype=np.int16),
                 np.array([2.0], dtype=np.float32)):
        wavfile = types.ModuleType('scipy.io.wavfile')
        wavfile.read = lambda path, data=data: (48000, data)
        monkeypatch.setitem(sys.modules, 'scipy', types.ModuleType('scipy'))
        monkeypatch.setitem(sys.modules, 'scipy.io', types.ModuleType('scipy.io'))
        monkeypatch.setitem(sys.modules, 'scipy.io.wavfile', wavfile)
        if data.dtype == np.float32 and data.max() <= 1:
            check(tmp_path, clips)
        else:
            with pytest.raises(ValueError):
                check(tmp_path, clips)


def test_divergence_guard_aborts_blown_up_gan():
    import ast
    source = (ROOT / 'vc_worker/train_mps.py').read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == 'check_divergence')
    scope = {}
    exec(compile(ast.Module([node], []), 'train_mps', 'exec'), scope)
    check = scope['check_divergence']
    check(4.0, 40.0, 30.0)
    for args in ((919720192.0, 1.0, 30.0), (4.0, float('nan'), 30.0), (4.0, 40.0, 1000.0)):
        with pytest.raises(RuntimeError, match='diverged'):
            check(*args)
