#!/bin/bash
# Rebuilds and reinstalls AI Voice.app whenever a source file changes.
# Run it in a Terminal tab and leave it: scripts/watch-app.sh
# If the app is open (e.g. you are in a call), the new build waits in
# build/ and is installed as soon as you quit the app; the running app is never killed.
cd "$(dirname "$0")/.."
dest="$HOME/Utilities/AI Voice.app"
marker=build/.watch-marker
sources=(src vc_worker macos native scripts/requirements-app.txt)
changed() { [ -n "$(find "${sources[@]}" -type f -not -name "*.pyc" -not -path "*/__pycache__/*" -newer "$marker" 2>/dev/null | head -1)" ]; }
mkdir -p build; touch "$marker"; pending=0
echo "Watching ${sources[*]} (Ctrl+C to stop)"
while true; do
  if changed; then
    touch "$marker"; sleep 1  # let a multi-file save settle
    if scripts/build-app.sh; then pending=1; else echo "Build failed."; fi
  fi
  if [ "$pending" = 1 ]; then
    if pgrep -f "$dest/Contents/MacOS/AIVoice" >/dev/null; then
      [ "${notified:-0}" = 1 ] || { echo "New build ready; will install after AI Voice quits."; notified=1; }
    else
      scripts/install-app.sh && pending=0 && notified=0
    fi
  fi
  sleep 2
done
