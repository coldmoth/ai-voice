#!/bin/bash
# Builds the pinned BlackHole source as the universal AI Voice Mic HAL plug-in.
# Needs Command Line Tools; upstream is kept in a disposable build cache.
set -euo pipefail
cd "$(dirname "$0")/.."
source driver/UPSTREAM
driver="build/driver/AIVoiceMic.driver"

if [ -d "$driver" ] && [ "$driver" -nt driver/UPSTREAM ] && [ "$driver" -nt scripts/build-driver.sh ]; then
  echo "$driver built."
  exit 0
fi

mkdir -p build/cache
if [ ! -d build/cache/blackhole ]; then
  git clone https://github.com/ExistentialAudio/BlackHole.git build/cache/blackhole
fi
out="$PWD/$driver"
rm -rf "$out"
(
  cd build/cache/blackhole
  git fetch --tags
  git checkout --force --detach "$COMMIT"
  git clean -fdx
  [ "$(git rev-parse HEAD)" = "$COMMIT" ] || { echo "Upstream commit mismatch." >&2; exit 1; }

  # Replace every factory UUID occurrence in the upstream plug-in plist only.
  plist=$(find . -name '*.plist' -exec grep -l CFPlugInFactories {} \;)
  [ -f "$plist" ] || { echo "Expected one plug-in plist." >&2; exit 1; }
  upstream_uuid=$(/usr/libexec/PlistBuddy -c 'Print :CFPlugInFactories' "$plist" | awk '/ = / {print $1}')
  [ -n "$upstream_uuid" ] || { echo "Factory UUID missing." >&2; exit 1; }
  sed -i '' "s/$upstream_uuid/$FACTORY_UUID/g" "$plist"

  mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
  clang -bundle -Os -arch arm64 -arch x86_64 -mmacosx-version-min=10.13 \
    -DDEBUG=0 -DkNumber_Of_Channels=2 \
    '-DkPlugIn_BundleID="io.github.coldmoth.aivoicemic"' \
    '-DkDriver_Name="AI Voice Mic"' '-DkDevice_Name="AI Voice Mic"' \
    '-DkPlugIn_Icon="BlackHole.icns"' -DkHas_Driver_Name_Format=false \
    BlackHole/BlackHole.c -framework CoreAudio -framework CoreFoundation \
    -framework Accelerate -o "$out/Contents/MacOS/AIVoiceMic"
  cp BlackHole/BlackHole.icns "$out/Contents/Resources/BlackHole.icns"
  sed -e 's/${EXECUTABLE_NAME}/AIVoiceMic/g' \
      -e 's/$(PRODUCT_BUNDLE_IDENTIFIER)/io.github.coldmoth.aivoicemic/g' \
      -e 's/${PRODUCT_NAME}/AIVoiceMic/g' \
      -e "s/\$(MARKETING_VERSION)/${TAG#v}/g" \
      "$plist" > "$out/Contents/Info.plist"
  plutil -lint "$out/Contents/Info.plist"
  if grep -q '\$' "$out/Contents/Info.plist"; then
    echo "Unexpanded bundle metadata." >&2
    exit 1
  fi
)
codesign --force --deep --sign - --timestamp=none "$driver"
codesign --verify --deep --strict "$driver"
echo "$driver built."
