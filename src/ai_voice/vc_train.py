"""Single-job offline training supervisor; ML lives in the child process."""
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import threading
import time
import tempfile

from .i18n import t
from .paths import ROOT, VC_PYTHON, VC_SPIKE
from .vc_store import VcStoreError
from .vc_omp import dedupe_libomp


class VcTrainError(VcStoreError):
    """Safe user-facing training error."""


STAGES = {'prepare': ('status.training_prepare', 0, .05),
          'pitch': ('status.training_pitch', .05, .10),
          'features': ('status.training_features', .15, .10),
          'train': ('status.training_train', .25, .70),
          'index': ('status.training_index', .95, .05)}
PRESETS = {'fast': 50, 'normal': 100, 'max': 200}


def process_rss(pid):
    try:
        import psutil
    except ImportError:
        # On macOS -g selects process groups, on Linux it may select sessions.
        result = subprocess.run(['ps', '-o', 'rss=', '-g', str(pid)],
                                capture_output=True, text=True, timeout=2)
        if not result.stdout.strip():
            result = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)],
                                    capture_output=True, text=True, timeout=2)
        return sum(int(value) * 1024 for value in result.stdout.split())
    try:
        parent = psutil.Process(pid)
        total = 0
        for child in [parent, *parent.children(recursive=True)]:
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        return total
    except psutil.NoSuchProcess:
        return 0


class VcTrainer:
    def __init__(self, store, python_path=None, runner_path=None, rvc_src=None,
                 pretrained_dir=None, epochs=200, batch=4,
                 rss_limit_bytes=6 * 1024 ** 3, nice=15, *,
                 clock=time.monotonic, popen=subprocess.Popen, sleep=time.sleep,
                 rss_measure=process_rss, on_done=None):
        self.store = store
        self.python_path = str(python_path or VC_PYTHON)
        self.runner_path = str(runner_path or ROOT / 'vc_worker/train_runner.py')
        self.rvc_src = str(rvc_src or VC_SPIKE / 'src/rvc')
        self.pretrained_dir = str(pretrained_dir or VC_SPIKE / 'weights')
        if type(epochs) is not int or epochs < 1 or type(batch) is not int or batch < 1:
            raise VcTrainError(t("errors.training_parameters"))
        self.epochs, self.batch = epochs, batch
        self.rss_limit_bytes, self.nice = rss_limit_bytes, nice
        self.clock, self.popen, self.sleep = clock, popen, sleep
        self.rss_measure, self.on_done = rss_measure, on_done
        self._lock = threading.RLock()
        self._job = None
        self._queue = []
        self._pause_count = 0
        self._user_paused = False
        self._closed = False
        self._resumed = threading.Event()
        self._resumed.set()
        self.data = self.store.root.parent
        self.data.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.data / 'vc-train-queue.json'
        self.stats_path = self.data / 'vc-train-stats.json'
        self.pid_path = self.data / 'vc-train.pid'
        self._kill_orphan()
        try:
            saved = json.loads(self.queue_path.read_text())
            if isinstance(saved, dict):
                entries = saved.get('queue') if isinstance(saved.get('queue'), list) else []
                if saved.get('user_paused') is True:
                    self._user_paused = True
                    self._pause_count += 1
                    self._resumed.clear()
            elif isinstance(saved, list):
                entries = saved
            else:
                entries = None
            if entries is not None:
                seen = set()
                for entry in entries:
                    if (isinstance(entry, dict) and isinstance(entry.get('voice_id'), str)
                            and type(entry.get('epochs')) is int and entry['epochs'] > 0
                            and entry['voice_id'] not in seen):
                        try:
                            self._validate(entry['voice_id'])
                        except VcStoreError:
                            continue
                        self._queue.append(entry)
                        seen.add(entry['voice_id'])
        except (OSError, ValueError):
            pass
        self._start_next()

    @staticmethod
    def _save_json(path, value):
        fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _persist(self):
        active = []
        if self._job and not self._job['finished'].is_set():
            active = [dict(voice_id=self._job['status']['voice_id'],
                           epochs=self._job['status']['total_epochs'])]
        self._save_json(self.queue_path, dict(user_paused=self._user_paused,
                                              queue=active + self._queue))

    def _pause_label(self):
        return t("status.training_paused") if self._user_paused else t("status.training_paused_live")

    def _kill_orphan(self):
        try:
            pgid = int(self.pid_path.read_text())
            if pgid <= 1 or pgid == os.getpgrp():
                return
            os.kill(pgid, 0)
            result = subprocess.run(['ps', '-p', str(pgid), '-o', 'command='],
                                    capture_output=True, text=True, timeout=2)
            if 'train_runner' in result.stdout:
                os.killpg(pgid, signal.SIGTERM)
                os.killpg(pgid, signal.SIGCONT)
                self.sleep(3)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        finally:
            self.pid_path.unlink(missing_ok=True)

    def _stats(self):
        try:
            values = json.loads(self.stats_path.read_text())
            return [float(k) for k in values[-3:] if type(k) in (int, float) and math.isfinite(k) and k > 0]
        except (OSError, ValueError, TypeError):
            return []

    def estimate(self, speech_seconds, epochs):
        values = self._stats()
        k = statistics.mean(values) if values else 2.3
        return math.ceil(epochs * k * speech_seconds / 60 / 60) + 2

    def _validate(self, voice_id):
        meta = self.store.get(voice_id)
        directory = self.store._directory(voice_id)
        if meta['kind'] != 'zeroshot' or meta.get('status') != 'ready':
            raise VcTrainError(t("errors.training_quick_ready"))
        if not (directory / 'dataset').is_dir() or (directory / 'dataset').is_symlink():
            raise VcTrainError(t("errors.training_dataset_missing"))
        if not meta.get('can_train') or meta.get('speech_seconds', 0) < 180:
            raise VcTrainError(t("errors.training_speech_minimum"))
        return meta

    def status(self):
        with self._lock:
            values = self._stats()
            if not self._job and not self._queue and not self._pause_count and not values:
                return None
            if self._pause_count and (not self._job or self._job['finished'].is_set()):
                status = dict(state='paused', voice_id=None)
            else:
                status = dict(self._job['status']) if self._job else dict(state='idle', voice_id=None)
            if status.get('state') == 'paused':
                status['stage_label'] = self._pause_label()
            status['user_paused'] = self._user_paused
            items = []
            for entry in self._queue:
                try:
                    meta = self.store.get(entry['voice_id'])
                except VcStoreError:
                    continue
                items.append(dict(entry, name=meta['name'], eta_min=self.estimate(meta.get('speech_seconds', 0), entry['epochs'])))
            status['queue'] = items
            status['k'] = statistics.mean(values) if values else 2.3
            return status

    def enqueue(self, voice_id, preset):
        if not isinstance(preset, str) or preset not in PRESETS:
            raise VcTrainError(t("errors.training_preset_unknown"))
        with self._lock:
            if (self._job and not self._job['finished'].is_set() and self._job['status']['voice_id'] == voice_id
                    or any(e['voice_id'] == voice_id for e in self._queue)):
                raise VcTrainError(t("errors.training_queued"))
            self._validate(voice_id)
            self._queue.append(dict(voice_id=voice_id, epochs=PRESETS[preset]))
            self._persist()
            self._start_next()
            return self.status()

    def move(self, voice_id, direction):
        if direction not in ('up', 'down'):
            raise VcTrainError(t("errors.training_direction"))
        with self._lock:
            for i, entry in enumerate(self._queue):
                if entry['voice_id'] == voice_id:
                    j = i + (-1 if direction == 'up' else 1)
                    if 0 <= j < len(self._queue):
                        self._queue[i], self._queue[j] = self._queue[j], self._queue[i]
                    self._persist()
                    return self.status()
            raise VcTrainError(t("errors.training_queue_missing"))

    def remove_queued(self, voice_id):
        with self._lock:
            self._queue = [e for e in self._queue if e['voice_id'] != voice_id]
            self._persist()

    def _start_next(self):
        with self._lock:
            if self._closed or self._pause_count or self._job and not self._job['finished'].is_set():
                return
            while self._queue:
                entry = self._queue.pop(0)
                try:
                    self.start(entry['voice_id'], epochs=entry['epochs'])
                    return
                except VcStoreError:
                    self._job = None
                    if self.on_done:
                        self.on_done(entry['voice_id'], 'failed')
            self._persist()

    def start(self, voice_id, *, epochs=None):
        with self._lock, self.store._lock:
            if self._job and not self._job['finished'].is_set():
                raise VcTrainError(t("errors.training_running"))
            meta = self._validate(voice_id)
            directory = self.store._directory(voice_id)
            total_epochs = self.epochs if epochs is None else epochs
            self._cleanup(directory, preserve_checkpoint=True)
            log = (directory / 'train.log').open('a', encoding='utf-8')
            args = ['/usr/sbin/taskpolicy', '-c', 'utility', 'nice', '-n', str(self.nice), self.python_path, self.runner_path,
                    '--voice-dir', str(directory), '--epochs', str(total_epochs),
                    '--batch', str(self.batch), '--rvc-src', self.rvc_src,
                    '--pretrained-dir', self.pretrained_dir]
            try:
                env = {**os.environ, 'SYSTEM_VERSION_COMPAT': '0', 'OMP_NUM_THREADS': '4'}
                try:
                    relinked = dedupe_libomp(self.python_path)
                except OSError as exc:
                    # Without links libomp aborts; allowing two runtimes is riskier, so only fall back when relinking fails.
                    relinked = []
                    env['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
                    log.write(f'libomp dedupe failed: {exc}; KMP_DUPLICATE_LIB_OK=TRUE\n')
                if relinked:
                    log.write('libomp linked to torch: ' + ', '.join(relinked) + '\n')
                log.flush()
                process = self.popen(args, shell=False, start_new_session=True,
                                     env=env,
                                     stdout=subprocess.PIPE, stderr=log, text=True,
                                     bufsize=1)
            except Exception as exc:
                log.close()
                raise VcTrainError(t("errors.training_start")) from exc
            job = {'process': process, 'directory': directory, 'name': meta['name'],
                   'identity': directory.stat().st_ino, 'log': log,
                   'stop': threading.Event(), 'finished': threading.Event(),
                   'terminate_lock': threading.Lock(), 'times': [], 'last_epoch': 0,
                   'last_time': self.clock(), 'index_skipped': False,
                   'paused_at': None, 'paused_seconds': 0.,
                   'train_seconds': 0., 'train_epochs': 0,
                   'epoch_pause_seconds': 0.,
                   'status': dict(voice_id=voice_id, stage='prepare',
                                  stage_label=t("status.training_prepare"), epoch=0,
                                  total_epochs=total_epochs, progress=0., eta_s=None,
                                  state='running', error=None)}
            self._job = job
            try:
                self.pid_path.write_text(str(process.pid))
                self._persist()
            except Exception as exc:
                self._terminate(job)
                process.stdout.close()
                log.close()
                self.pid_path.unlink(missing_ok=True)
                self._job = None
                raise VcTrainError(t("errors.training_save")) from exc
            threading.Thread(target=self._read, args=(job,), daemon=True).start()
            threading.Thread(target=self._watch, args=(job,), daemon=True).start()
            return dict(job['status'])

    @staticmethod
    def _cleanup(directory, preserve_checkpoint=False):
        for name in ('train-work', 'train-out'):
            path = directory / name
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                if (name == 'train-work' and preserve_checkpoint
                        and list(path.glob('G_*.pth')) and list(path.glob('D_*.pth'))):
                    continue
                shutil.rmtree(path)

    def _exists(self, job):
        try:
            self.store.get(job['status']['voice_id'])
            return job['directory'].stat().st_ino == job['identity']
        except (OSError, VcStoreError):
            return False

    def _terminate(self, job):
        with job['terminate_lock']:
            process = job['process']
            deadline = self.clock() + 3
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            # Reap/kill descendants too, even if the direct child exited on TERM.
            try:
                os.killpg(process.pid, 0)
                self.sleep(max(0, deadline - self.clock()))
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def _stop(self, job, state, error=None):
        with self._lock:
            if job['status']['state'] not in ('running', 'paused'):
                return
            job['status'].update(state=state, error=error, eta_s=None)
            job['stop'].set()
            self._resumed.set()
            if job['paused_at'] is not None:
                try:
                    os.killpg(job['process'].pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        self._terminate(job)

    def cancel(self, voice_id=None):
        with self._lock:
            job = self._job
            if voice_id is not None and (not job or job['status']['voice_id'] != voice_id or job['finished'].is_set()):
                if any(e['voice_id'] == voice_id for e in self._queue):
                    self.remove_queued(voice_id)
                    self._clear_user_pause_if_idle()
                    return
                raise VcTrainError(t("errors.training_other_voice"))
            if not job or job['finished'].is_set():
                self._clear_user_pause_if_idle()
                return
        self._stop(job, 'cancelled')
        job['finished'].wait()
        with self._lock:
            self._clear_user_pause_if_idle()

    def _clear_user_pause_if_idle(self):
        if (self._user_paused and not self._queue
                and (not self._job or self._job['finished'].is_set())):
            self._user_paused = False
            if self._pause_count:
                self._pause_count -= 1
                if not self._pause_count:
                    self._resumed.set()
            self._persist()

    def user_pause(self):
        with self._lock:
            if self._user_paused:
                return self.status()
            self._user_paused = True
            self._persist()
            self.pause()
            return self.status()

    def user_resume(self):
        with self._lock:
            if not self._user_paused:
                return self.status()
            self._user_paused = False
            self._persist()
            self.resume()
            return self.status()

    def pause(self):
        with self._lock:
            self._pause_count += 1
            if self._pause_count > 1:
                return True
            self._resumed.clear()
            job = self._job
            if job and job['status']['state'] == 'running' and not job['finished'].is_set():
                try:
                    os.killpg(job['process'].pid, signal.SIGSTOP)
                except ProcessLookupError:
                    return True
                job['paused_at'] = self.clock()
                job['status'].update(state='paused', stage_label=self._pause_label())
            return True

    def resume(self):
        with self._lock:
            if not self._pause_count:
                return
            self._pause_count -= 1
            if self._pause_count:
                return
            self._resumed.set()
            job = self._job
            if job and job['status']['state'] == 'paused':
                now = self.clock()
                duration = now - job['paused_at']
                job['paused_seconds'] += duration
                job['paused_at'] = None
                try:
                    os.killpg(job['process'].pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                label = t(STAGES[job['status']['stage']][0])
                job['status'].update(state='running', stage_label=label)
            self._start_next()

    def close(self):
        with self._lock:
            self._closed = True
            self._persist()  # Keep current first so shutdown resumes it next time.
            job = self._job
        if job and not job['finished'].is_set():
            self._stop(job, 'cancelled')
            job['finished'].wait()

    def _watch(self, job):
        while not job['stop'].is_set():
            try:
                if not self._exists(job):
                    self._stop(job, 'cancelled')
                    return
                if job['status']['state'] != 'paused' and self.rss_measure(job['process'].pid) > self.rss_limit_bytes:
                    self._stop(job, 'failed', t("errors.training_memory_limit"))
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.sleep(2)

    def _timing(self, job):
        with self._lock:
            now = self.clock()
            paused = job['paused_seconds']
            if job['paused_at'] is not None:
                paused += now - job['paused_at']
            return now, paused

    def _event(self, job, event, timing=None):
        stage = event.get('stage')
        if stage not in STAGES:
            return
        with self._lock:
            if job['status']['state'] != 'running':
                return
            now, paused = timing if timing is not None else self._timing(job)
            status = job['status']
            label_key, base, weight = STAGES[stage]
            label = t(label_key)
            fraction = float(event.get('progress', 0))
            if not math.isfinite(fraction):
                return
            if stage == 'train':
                total = status['total_epochs']
                epoch = max(0, min(total, int(event.get('epoch', 0))))
                fraction = epoch / total
                label = t("status.training_epoch", epoch=epoch, total=total)
                if event.get('resumed'):
                    job['last_epoch'], job['last_time'] = epoch, now
                    job['epoch_pause_seconds'] = paused
                if epoch > job['last_epoch']:
                    seconds = float(event.get('seconds', now - job['last_time']))
                    seconds -= paused - job['epoch_pause_seconds']
                    job['epoch_pause_seconds'] = paused
                    if epoch > 1 and seconds > 0 and math.isfinite(seconds):
                        job['times'].append(seconds)
                    if seconds > 0 and math.isfinite(seconds):
                        job['train_seconds'] += seconds
                        job['train_epochs'] += epoch - job['last_epoch']
                    job['last_epoch'], job['last_time'] = epoch, now
                status['epoch'] = epoch
            if stage == 'index' and event.get('skipped') == 'faiss_unavailable':
                job['index_skipped'] = True
            status.update(stage=stage, stage_label=label,
                          progress=max(status['progress'], base + weight * max(0, min(1, fraction))))
            status['eta_s'] = (statistics.median(job['times']) *
                               (status['total_epochs'] - status['epoch']) if job['times'] else None)

    def _publish(self, job):
        directory = job['directory']
        with self._lock, self.store._lock:
            if job['status']['state'] != 'running':
                return
            if not self._exists(job):
                job['status'].update(state='cancelled', eta_s=None)
                return
            names = ['model.pth'] + ([] if job['index_skipped'] else ['model.index'])
            for name in names:
                path = directory / 'train-out' / name
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise VcTrainError(t("errors.training_output_missing"))
            moved = []
            try:
                for name in names:
                    os.replace(directory / 'train-out' / name, directory / name)
                    moved.append(directory / name)
                self.store.mark_trained(job['status']['voice_id'])
            except Exception:
                for path in moved:
                    path.unlink(missing_ok=True)
                raise
            shutil.rmtree(directory / 'dataset')
            meta = self.store.get(job['status']['voice_id'])
            if job['train_epochs'] and meta.get('speech_seconds', 0) > 0:
                k = job['train_seconds'] / job['train_epochs'] / (meta['speech_seconds'] / 60)
                self._save_json(self.stats_path, (self._stats() + [k])[-3:])
            job['status'].update(state='done', progress=1., eta_s=0.)

    def _read(self, job):
        try:
            for line in job['process'].stdout:
                timing = self._timing(job)
                self._resumed.wait()
                job['log'].write(line)
                job['log'].flush()
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        self._event(job, event, timing)
                except (ValueError, TypeError, OverflowError):
                    pass
            code = job['process'].wait()
            self._resumed.wait()
            if not self._exists(job):
                self._stop(job, 'cancelled')
            elif code:
                self._stop(job, 'failed', t("errors.training_log"))
            else:
                self._publish(job)
        except Exception:
            self._stop(job, 'failed', t("errors.training_finish"))
        finally:
            job['stop'].set()
            # Never remove files of a replacement directory with the same ID.
            try:
                if job['directory'].stat().st_ino == job['identity']:
                    self._cleanup(job['directory'], preserve_checkpoint=job['status']['state'] == 'cancelled')
            except OSError:
                pass
            job['process'].stdout.close()
            job['log'].close()
            with self._lock:
                self.pid_path.unlink(missing_ok=True)
                job['finished'].set()
                if not self._closed:
                    self._persist()
            if self.on_done:
                try:
                    self.on_done(job['status']['voice_id'], job['status']['state'])
                except Exception:
                    pass
            self._start_next()
