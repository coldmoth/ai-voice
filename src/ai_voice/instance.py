"""Exclusive audio ownership shared by desktop and command-line launches."""
import os
import subprocess
import sys
if sys.platform != "win32":
    import fcntl
from .i18n import t
from .paths import DATA


class AudioLease:
    def __init__(self):
        self.file = None

    def acquire(self):
        # Old command launchers started before this change do not own our lock.
        if sys.platform != "win32":
            try:
                result = subprocess.run(["/bin/ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=2)
                if any("-m ai_voice.cli start" in line and line.split(maxsplit=1)[0] != str(os.getpid()) for line in result.stdout.splitlines()):
                    raise RuntimeError(t("errors.stop_terminal_first"))
            except (OSError, subprocess.TimeoutExpired):
                pass
        directory = DATA
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / "audio.lock").open("a", encoding="utf-8")
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                raise RuntimeError(t("errors.instance_running")) from None
            self.file = handle
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError(t("errors.instance_running")) from None
        self.file = handle

    def close(self):
        if self.file:
            if sys.platform == "win32":
                import msvcrt
                try:
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    self.file.close()
                    self.file = None
                return
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None

