"""Where code, UI, helper and user data live.

Development checkout: everything sits under the repository (state/ for data).
Packaged `AI Voice.app`: the launcher sets AI_VOICE_RESOURCES, AI_VOICE_WEB,
AI_VOICE_HELPER, AI_VOICE_HOME (~/Library/Application Support/AI Voice) and
AI_VOICE_VC_RUNTIME, so nothing is read from or written into the bundle.
"""
import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _env_path(name, default):
    value = os.environ.get(name)
    return Path(value) if value else default


ROOT = _env_path("AI_VOICE_RESOURCES", _REPO)
DATA = _env_path("AI_VOICE_HOME", ROOT / "state")
WEB = _env_path("AI_VOICE_WEB", ROOT / "macos" / "desktop")
HELPER_APP = _env_path("AI_VOICE_HELPER", ROOT / "build" / "SpeechHelper.app")
HELPER_BINARY = HELPER_APP / "Contents" / "MacOS" / "SpeechHelper"
CONFIG_FILE = DATA / "config.toml" if os.environ.get("AI_VOICE_HOME") else ROOT / "config.toml"

# Experimental voice conversion: a Python 3.10 + torch environment and model
# sources/weights that are too large to ship inside the app.
_VC = os.environ.get("AI_VOICE_VC_RUNTIME")
VC_PYTHON = Path(_VC) / "venv" / "bin" / "python" if _VC else ROOT / ".venv-vc" / "bin" / "python"
VC_SPIKE = Path(_VC) / "spike" if _VC else ROOT / "state" / "vc-spike"


def ensure_data_dir():
    DATA.mkdir(parents=True, exist_ok=True)
    return DATA
