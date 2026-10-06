#!/bin/bash
# Build + install in one go. Use after any change to src/, vc_worker/, macos/, native/.
#   scripts/rebuild.sh            build, install (fails politely if the app is open)
#   scripts/rebuild.sh --restart  also quit and reopen a running app
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/build-app.sh
scripts/install-app.sh "$@"
