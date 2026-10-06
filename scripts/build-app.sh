#!/bin/bash
# Builds a self-contained build/AI Voice.app (Python, code, UI and the speech
# helper inside the bundle; user data lives in ~/Library/Application Support/AI Voice).
#   scripts/build-app.sh          incremental build (a few seconds for code/UI edits)
#   scripts/build-app.sh --dmg    also write build/AI Voice.dmg
# The heavy parts are cached: the Python runtime + wheels are only reinstalled when
# scripts/requirements-app.txt changes, Swift only when its sources change.
# Needs `uv` (PATH, or $UV) the first time, to fetch a portable Python.
set -euo pipefail
cd "$(dirname "$0")/.."
PYVER=3.13
app="build/AI Voice.app"
res="$app/Contents/Resources"
stamp="$res/python/.build-stamp"
want="$PYVER $(shasum scripts/requirements-app.txt | cut -d' ' -f1)"

mkdir -p "$app/Contents/MacOS" "$app/Contents/Helpers" "$res"

# 1. Launcher
cp macos/Desktop-Info.plist "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $(scripts/version.sh)" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $(git rev-list --count HEAD 2>/dev/null || echo 1)" "$app/Contents/Info.plist"
cp macos/AppIcon.icns "$res/AppIcon.icns"
launcher="$app/Contents/MacOS/AIVoice"
if [ ! -x "$launcher" ] || [ macos/Desktop.swift -nt "$launcher" ]; then
  swiftc -O macos/Desktop.swift -o "$launcher" -framework AppKit -framework WebKit
fi

# 2. Speech helper (own bundle: it holds the microphone / speech permissions)
helper="$app/Contents/Helpers/SpeechHelper.app"
if [ ! -d "$helper" ] || [ -n "$(find native -newer "$helper/Contents/MacOS/SpeechHelper" -type f 2>/dev/null)" ]; then
  scripts/build-helper.sh 2>&1 | grep -v "warning\|^ *[0-9]* |\|^ *|\|^$\|DeprecatedDecl" || true
  rm -rf "$helper"
  ditto build/SpeechHelper.app "$helper"
fi

# 3. Portable Python + dependencies (cached by stamp)
if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
  uv="${UV:-$(command -v uv || true)}"
  [ -x "$uv" ] || { echo "uv is required: brew install uv" >&2; exit 1; }
  export UV_PYTHON_INSTALL_DIR="$PWD/build/cache/python" UV_CACHE_DIR="$PWD/build/cache/uv"
  "$uv" python install "$PYVER" >/dev/null
  py_src=$(find "$UV_PYTHON_INSTALL_DIR" -maxdepth 1 -type d -name "cpython-$PYVER.*-macos-aarch64-none" | sort | tail -1)
  rm -rf "$res/python"
  ditto "$py_src" "$res/python"
  rm -f "$res/python/EXTERNALLY-MANAGED" "$res/python/lib/python$PYVER/EXTERNALLY-MANAGED"
  "$uv" pip install --python "$res/python/bin/python3" --no-config -r scripts/requirements-app.txt
  rm -rf "$res/python/include" "$res/python/share" "$res/python/lib/python$PYVER/test" \
         "$res/python/lib/python$PYVER/idlelib" "$res/python/lib/python$PYVER/turtledemo" \
         "$res/python/lib/python$PYVER/tkinter" "$res/python/lib/libtcl"* "$res/python/lib/libtk"* \
         "$res/python/lib/tcl"* "$res/python/lib/tk"* "$res/python/lib/itcl"* "$res/python/lib/thread"*
  find "$res/python" -name "tests" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$res/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  "$res/python/bin/python3" -m compileall -q -j 0 "$res/python/lib" >/dev/null || true
  find "$res/python" \( -name "*.so" -o -name "*.dylib" \) -exec codesign --force --sign - --timestamp=none {} + 2>/dev/null
  for exe in "$res"/python/bin/python3.*; do
    [ -f "$exe" ] && [ ! -L "$exe" ] && codesign --force --sign - --timestamp=none "$exe"
  done
  echo "$want" > "$stamp"
fi

# 4. Application code and UI (copies, not symlinks: the bundle must stand alone)
rm -rf "$res/app" "$res/web"
mkdir -p "$res/app/scripts"
ditto src/ai_voice "$res/app/ai_voice"
ditto vc_worker "$res/app/vc_worker"
if [ -d build/driver/AIVoiceMic.driver ]; then
  mkdir -p "$res/app/driver"
  ditto build/driver/AIVoiceMic.driver "$res/app/driver/AIVoiceMic.driver"
fi
ditto macos/desktop "$res/web"
cp scripts/save-fish-key.command "$res/app/scripts/"
cp config.example.toml "$res/app/"
find "$res/app" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
"$res/python/bin/python3" -m compileall -q -j 0 "$res/app" >/dev/null || true

# 5. Sign inside-out (ad-hoc). Stable identifiers keep the TCC grants across rebuilds.
codesign --force --sign - --identifier local.ai-discord-voice.SpeechHelper \
  --requirements '=designated => identifier "local.ai-discord-voice.SpeechHelper"' "$helper"
codesign --force --sign - --identifier local.ai-discord-voice.Launcher \
  --requirements '=designated => identifier "local.ai-discord-voice.Launcher"' "$app"
codesign --verify --deep --strict "$app"

if [ "${1:-}" = "--dmg" ]; then
  stage=$(mktemp -d)
  ditto "$app" "$stage/AI Voice.app"
  rm -f "build/AI Voice.dmg"
  if command -v create-dmg >/dev/null; then
    # Styled window with a drag-to-Applications arrow; falls back to a plain DMG if Finder scripting fails.
    create-dmg --volname "AI Voice" --background scripts/dmg-bg.png --window-size 660 400 --icon-size 112 \
      --icon "AI Voice.app" 170 195 --app-drop-link 490 195 --hide-extension "AI Voice.app" \
      "build/AI Voice.dmg" "$stage" >/dev/null || rm -f "build/AI Voice.dmg"
  fi
  if [ ! -f "build/AI Voice.dmg" ]; then
    ln -s /Applications "$stage/Applications"
    hdiutil create -quiet -volname "AI Voice" -srcfolder "$stage" -format UDZO "build/AI Voice.dmg"
  fi
  rm -rf "$stage"
  echo "DMG: build/AI Voice.dmg"
fi
echo "$app built ($(du -sh "$app" | cut -f1))."
