"""Run from project root: .venv-vc/bin/python -m vc_worker"""
from contextlib import redirect_stdout
import os
import sys
from .protocol import Service

os.environ['SYSTEM_VERSION_COMPAT'] = '0'
if __name__ == '__main__':
    service = Service(sys.stdout, hard_exit=os._exit)
    with redirect_stdout(sys.stderr):
        service.run(sys.stdin)
