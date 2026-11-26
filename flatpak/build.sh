#!/usr/bin/env bash
# Builds DogMute.flatpak:  flatpak/build.sh 1.3.0
# Needs: flatpak install --user flathub org.flatpak.Builder org.freedesktop.Sdk//25.08
set -euo pipefail
VERSION="${1:?usage: flatpak/build.sh VERSION}"
cd "$(dirname "$0")/.."
printf 'VERSION = "%s"   # written by flatpak/build.sh; the updater compares it\n' "$VERSION" > dogmute/_version.py
trap 'rm -f dogmute/_version.py' EXIT
mkdir -p dist
flatpak run org.flatpak.Builder --user --force-clean --disable-rofiles-fuse --repo=flatpak/repo --state-dir=.flatpak-builder \
  flatpak/build-dir flatpak/com.dogmosh.DogMute.yml
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo flatpak/repo dist/DogMute.flatpak \
  com.dogmosh.DogMute
echo "dist/DogMute.flatpak"
