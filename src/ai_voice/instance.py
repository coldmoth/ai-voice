"""Exclusive audio ownership shared by desktop and command-line launches."""
import fcntl
import os
import subprocess
from .i18n import t
from .paths import DATA


class AudioLease:
    def __init__(self):
        self.file = None

    def acquire(self):
        # Old command launchers started before this change do not own our lock.
        try:
            result = subprocess.run(["/bin/ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=2)
            if any("-m ai_voice.cli start" in line and line.split(maxsplit=1)[0] != str(os.getpid()) for line in result.stdout.splitlines()):
                raise RuntimeError(t("errors.stop_terminal_first"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        directory = DATA
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / "audio.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError(t("errors.instance_running")) from None
        self.file = handle

    def close(self):
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


