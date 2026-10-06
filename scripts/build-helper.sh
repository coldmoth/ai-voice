#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
app="build/SpeechHelper.app"
mkdir -p "$app/Contents/MacOS"
cp native/Info.plist "$app/Contents/Info.plist"
swiftc -O native/VAD.swift native/AudioInput.swift native/SpeechHelper.swift -o "$app/Contents/MacOS/SpeechHelper" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker native/Info.plist \
  -framework AVFoundation -framework Speech -framework CoreAudio -framework AudioToolbox
codesign --force --sign - --identifier local.ai-discord-voice.SpeechHelper \
  --requirements '=designated => identifier "local.ai-discord-voice.SpeechHelper"' "$app"
codesign --verify --strict "$app"
