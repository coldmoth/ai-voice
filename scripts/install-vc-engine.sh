#!/bin/bash
set -euo pipefail
resources="$HOME/Utilities/AI Voice.app/Contents/Resources"
export AI_VOICE_RESOURCES="$resources/app"
export AI_VOICE_VC_RUNTIME="$HOME/Library/Application Support/AI Voice/vc-runtime"
export PYTHONPATH="$resources/app"
exec "$resources/python/bin/python3" -m ai_voice.vc_engine install
