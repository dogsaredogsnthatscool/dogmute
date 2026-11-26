#!/usr/bin/env bash
# Builds dist/DogMute-<AppleSilicon|Intel>.dmg on a Mac (macOS 14+, Xcode Command Line Tools, Python 3.13):
#   mac/build.sh 1.3.0 [python3.13]
# The app isn't signed by a developer (only ad hoc), so macOS asks once: System Settings > Privacy & Security > Open Anyway.
set -euo pipefail
VERSION="${1:?usage: mac/build.sh VERSION [python]}"
PY="${2:-python3.13}"
cd "$(dirname "$0")/.."
if [ "$(uname -m)" = arm64 ]; then KIND=AppleSilicon; else KIND=Intel; fi

mkdir -p bin build dist
xcrun swiftc -O -swift-version 5 -o bin/dogmute-listen mac/listen.swift

ICONSET=build/DogMute.iconset; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for s in 16 32 128 256; do
  sips -z $s $s dogmute/static/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) dogmute/static/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o build/DogMute.icns

[ -x .venv-build/bin/python ] || "$PY" -m venv .venv-build
.venv-build/bin/pip install -q --upgrade pip
.venv-build/bin/pip install -q -r requirements.txt pyinstaller==6.22.3
[ $KIND = AppleSilicon ] && .venv-build/bin/pip install -q -r requirements-mac.txt

printf 'VERSION = "%s"   # written by mac/build.sh; the updater compares it\n' "$VERSION" > dogmute/_version.py
trap 'rm -f dogmute/_version.py' EXIT
rm -rf build/DogMute dist/DogMute dist/DogMute.app
DOGMUTE_VERSION="$VERSION" .venv-build/bin/pyinstaller --noconfirm --distpath dist --workpath build mac/DogMute.spec
codesign --force --deep --sign - dist/DogMute.app   # ad hoc: Apple Silicon runs nothing unsigned

STAGE=build/dmg; rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R dist/DogMute.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "dist/DogMute-$KIND.dmg"
hdiutil create -quiet -volname DogMute -srcfolder "$STAGE" -ov -format UDZO "dist/DogMute-$KIND.dmg"
echo "dist/DogMute-$KIND.dmg"
