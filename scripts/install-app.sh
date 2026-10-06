#!/bin/bash
# Copies build/AI Voice.app to ~/Utilities (override: INSTALL_DIR=...) and copies
# existing settings from this checkout's state/ into
# ~/Library/Application Support/AI Voice (never overwrites what is already there).
#   scripts/install-app.sh            refuses while the app is running
#   scripts/install-app.sh --restart  quits the app, replaces it, opens it again
set -euo pipefail
cd "$(dirname "$0")/.."
src="build/AI Voice.app"
dest="${INSTALL_DIR:-$HOME/Utilities}/AI Voice.app"
support="$HOME/Library/Application Support/AI Voice"
[ -d "$src" ] || { echo "Build first: scripts/build-app.sh" >&2; exit 1; }

running() { pgrep -f "$dest/Contents/MacOS/AIVoice" >/dev/null; }
was_running=0
if running; then
  [ "${1:-}" = "--restart" ] || { echo "AI Voice is running — quit it and retry (or --restart)." >&2; exit 1; }
  was_running=1
  osascript -e 'tell application id "local.ai-discord-voice.Launcher" to quit' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do running || break; sleep 0.5; done
  running && pkill -f "$dest/Contents/MacOS/AIVoice" || true
fi

mkdir -p "$(dirname "$dest")"
rm -rf "$dest"
ditto "$src" "$dest"
codesign --verify --deep --strict "$dest"

mkdir -p "$support"
for item in desktop.json voice_metadata.json usage.json config.toml vc-voices; do
  if [ -e "state/$item" ] && [ ! -e "$support/$item" ]; then ditto "state/$item" "$support/$item"; fi
done
if [ -e config.toml ] && [ ! -e "$support/config.toml" ]; then cp config.toml "$support/config.toml"; fi
# The experimental voice-conversion runtime is too big to ship in the app: it is
# linked from this checkout until it gets its own installer.
if [ -d .venv-vc ] && [ -d state/vc-spike ]; then
  mkdir -p "$support/vc-runtime"
  ln -sfn "$PWD/.venv-vc" "$support/vc-runtime/venv"
  ln -sfn "$PWD/state/vc-spike" "$support/vc-runtime/spike"
fi
echo "Installed: $dest"
[ "$was_running" = 1 ] && open "$dest"
exit 0
