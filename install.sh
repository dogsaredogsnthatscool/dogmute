#!/usr/bin/env bash
# DogMute installer for Linux and macOS: checks what's needed, installs, downloads the models, starts a background
# service (systemd on Linux, launchd on macOS), opens setup.
#
#   ./install.sh                 install or update
#   ./install.sh --desktop       also put a launcher on the Desktop
#   ./install.sh --no-service    install only (run it yourself: .venv/bin/python -m dogmute)
#   ./install.sh --remove        stop and remove the service and launchers (keeps your settings)
#
# Without a checkout:  curl -fsSL https://raw.githubusercontent.com/dogsaredogsnthatscool/dogmute/main/install.sh | bash
set -euo pipefail
REPO_URL="${DOGMUTE_REPO:-https://github.com/dogsaredogsnthatscool/dogmute.git}"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")" 2>/dev/null && pwd || pwd)"

if [ ! -f "$HERE/dogmute/__main__.py" ]; then          # piped from curl: get the code first
  DEST="${DOGMUTE_DIR:-$HOME/.local/share/dogmute}"
  if [ "$(uname -s)" = Darwin ]; then GITFIX="xcode-select --install"; else GITFIX="sudo apt install git"; fi
  command -v git >/dev/null || { echo "DogMute needs git to download itself. Fix: $GITFIX"; exit 1; }
  if [ -d "$DEST/.git" ]; then git -C "$DEST" pull -q --ff-only; else git clone -q "$REPO_URL" "$DEST"; fi
  exec bash "$DEST/install.sh" "$@"
fi
DIR="$HERE"
MAC=0; [ "$(uname -s)" = Darwin ] && MAC=1
UNIT=~/.config/systemd/user/dogmute.service
APP=~/.local/share/applications/dogmute.desktop
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")/DogMute.desktop"
LABEL=local.dogmute
PLIST=~/Library/LaunchAgents/$LABEL.plist
MACDESK="$HOME/Desktop/DogMute.webloc"
SERVICE=1 DESKTOP=0
for a in "$@"; do
  case "$a" in
    --no-service) SERVICE=0 ;;
    --desktop) DESKTOP=1 ;;
    --remove)
      if [ $MAC = 1 ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$PLIST" "$MACDESK"
        echo "DogMute removed. Your settings are still in ~/.config/dogmute (delete that folder to forget them)."; exit 0
      fi
      systemctl --user disable --now dogmute.service 2>/dev/null || true
      rm -f "$UNIT" "$APP" "$DESK"; systemctl --user daemon-reload 2>/dev/null || true
      echo "DogMute removed. Your settings are still in ~/.config/dogmute (delete that folder to forget them)."; exit 0 ;;
    *) echo "unknown option $a (see the top of install.sh)"; exit 1 ;;
  esac
done

if [ -t 1 ]; then G=$'\e[32m' R=$'\e[31m' Y=$'\e[33m' B=$'\e[1m' N=$'\e[0m'; else G= R= Y= B= N=; fi
ok()   { echo "  ${G}ok${N}   $1"; }
bad()  { echo "  ${R}no${N}   $1"; echo "       fix: $2"; FAIL=1; }
warn() { echo "  ${Y}note${N} $1"; if [ -n "${2:-}" ]; then echo "       $2"; fi; }
FAIL=0

echo "${B}DogMute: checking this computer${N}"
GPU=0 PY=python3
if [ $MAC = 1 ]; then
  V="$(sw_vers -productVersion)"; MAJ="${V%%.*}"
  [ "$MAJ" -ge 13 ] && ok "macOS $V" \
    || bad "macOS $V is too old" "DogMute needs macOS 13 Ventura or newer: Apple menu > System Settings > General > Software Update"
  command -v brew >/dev/null && ok "Homebrew" \
    || bad "Homebrew is missing" "install it from https://brew.sh, then open a new Terminal window"
  xcrun --find swiftc >/dev/null 2>&1 && ok "Xcode Command Line Tools (builds the sound helper)" \
    || bad "Xcode Command Line Tools are missing" "xcode-select --install"
  for p in python3.13 python3.12 python3.14 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do   # 3.13 first: every package has Mac wheels for it
    command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null && { PY="$(command -v "$p")"; break; }
  done
  if [ -n "${PY:-}" ]; then ok "Python $("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
  else bad "Python 3.12 or newer is missing (the one macOS comes with is too old)" "brew install python@3.13"; fi
  if ls -d /Applications/Discord*.app ~/Applications/Discord*.app >/dev/null 2>&1; then ok "Discord desktop app"
  else warn "no Discord desktop app found" "install it from https://discord.com/download or: brew install --cask discord (Discord in a browser can't be used)"; fi
  if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] && [ "$MAJ" -ge 14 ]; then
    GPU=1; ok "Apple Silicon: speech runs on its GPU (the accurate speech model)"
  elif [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then
    warn "macOS 13: speech runs on the CPU with a smaller model (the GPU speech package needs macOS 14)"
  else
    warn "Intel Mac: speech runs on the CPU with a smaller model (works, less accurate)"
  fi
  system_profiler SPAudioDataType 2>/dev/null | grep -i blackhole >/dev/null && ok "BlackHole (a fallback sound source; not needed)"
  if [ $SERVICE = 1 ]; then launchctl print "gui/$(id -u)" >/dev/null 2>&1 && ok "launchd (starts DogMute at login)" \
    || bad "no logged-in desktop session for launchd" "run ./install.sh from Terminal on the Mac itself, or use --no-service"; fi
else
  if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    ok "Python $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
    python3 -c 'import venv, ensurepip' 2>/dev/null && ok "Python venv" \
      || bad "Python can't make a virtual environment" "sudo apt install python3-venv   (Fedora: it's built in; Arch: it's built in)"
  else
    bad "Python 3.12 or newer is missing" "sudo apt install python3 python3-venv   (Fedora: sudo dnf install python3; Arch: sudo pacman -S python)"
  fi
  if command -v pw-record >/dev/null && command -v pw-dump >/dev/null; then
    pw-dump >/dev/null 2>&1 && ok "PipeWire" || bad "PipeWire isn't running" "systemctl --user start pipewire pipewire-pulse wireplumber   (or log out and back in)"
  else
    bad "PipeWire tools (pw-record, pw-dump) are missing" "sudo apt install pipewire-bin   (Fedora: pipewire-utils; Arch: pipewire)"
  fi
  if [ $SERVICE = 1 ]; then
    systemctl --user show-environment >/dev/null 2>&1 && ok "systemd user services" \
      || bad "systemd user services aren't available" "run ./install.sh --no-service and start it with: .venv/bin/python -m dogmute"
  fi
  if command -v discord >/dev/null || [ -d /usr/share/discord ] || [ -d /opt/discord ] || \
     (command -v flatpak >/dev/null && flatpak info com.discordapp.Discord >/dev/null 2>&1) || \
     (command -v snap >/dev/null && snap list discord >/dev/null 2>&1); then
    ok "Discord desktop app"
  else
    warn "no Discord desktop app found" "install it from https://discord.com/download or: flatpak install flathub com.discordapp.Discord (Discord in a browser can't be used)"
  fi
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    if ldconfig -p 2>/dev/null | grep -q 'libcublas.so.12'; then
      GPU=1; ok "graphics card: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1) (the accurate speech model)"
    else
      warn "NVIDIA card found but CUDA 12 cuBLAS (libcublas.so.12) isn't, so speech runs on the CPU (works, less accurate)" \
           "for the card: sudo apt install libcublas12   (or the CUDA 12 toolkit), then run ./install.sh again"
    fi
  else
    warn "no NVIDIA graphics card: speech runs on the CPU with a smaller model (works, less accurate)"
  fi
  command -v notify-send >/dev/null || warn "no notify-send: no pop-up when someone is muted" "sudo apt install libnotify-bin"
  python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null \
    || warn "no GTK WebKit: the DogMute launcher opens your browser instead of its own window" "sudo apt install python3-gi gir1.2-webkit2-4.1"
fi
if [ $FAIL = 1 ]; then echo; echo "${R}Fix the lines marked 'no' above, then run ./install.sh again.${N}"; exit 1; fi

echo; echo "${B}Installing${N} (into $DIR/.venv)"
cd "$DIR"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "Python packages"
if [ $MAC = 1 ] && [ $GPU = 1 ]; then
  if .venv/bin/pip install -q -r requirements-mac.txt; then ok "speech on the Apple GPU (mlx-whisper)"
  else GPU=0; warn "couldn't install mlx-whisper, so speech runs on the CPU (works, less accurate)"; fi
fi
if [ $MAC = 1 ]; then   # the sound helper: rebuilt only when its source changes (macOS ties your OK to the build)
  mkdir -p bin
  SUM="$(shasum -a 256 mac/listen.swift | cut -c1-64)"
  if [ ! -x bin/dogmute-listen ] || [ "$(cat bin/.listen.sha 2>/dev/null)" != "$SUM" ]; then
    xcrun swiftc -O -swift-version 5 -o bin/dogmute-listen mac/listen.swift 2>bin/build.log \
      || { bad "the sound helper didn't build (bin/build.log has why)" "xcode-select --install, or update Xcode Command Line Tools, then run ./install.sh again"; exit 1; }
    echo "$SUM" > bin/.listen.sha
  fi
  ok "sound helper (bin/dogmute-listen)"
fi
echo "  downloading models (once, into ~/.cache/huggingface; up to ~1.6 GB with a graphics card, ~200 MB without)"
export HF_HUB_VERBOSITY=error HF_HUB_DISABLE_TELEMETRY=1
if [ $GPU = 1 ]; then .venv/bin/python -m dogmute.prefetch; else .venv/bin/python -m dogmute.prefetch --cpu; fi
ok "models"

PORT=$("$PY" -c 'import json,os;print(json.load(open(os.path.expanduser("~/.config/dogmute/config.json"))).get("port",8853))' 2>/dev/null || echo 8853)
URL="http://127.0.0.1:$PORT/"
if [ $MAC = 1 ]; then
  if [ $DESKTOP = 1 ]; then
    printf '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>URL</key><string>%s</string></dict></plist>\n' "$URL" > "$MACDESK"
    ok "DogMute link on your Desktop"
  fi
  if [ $SERVICE = 1 ]; then
    mkdir -p "$(dirname "$PLIST")" ~/Library/Logs
    cat > "$PLIST" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>-m</string><string>dogmute</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>3</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/dogmute.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/dogmute.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>HF_HUB_DISABLE_TELEMETRY</key><string>1</string>
    <key>HF_HUB_VERBOSITY</key><string>error</string>
    <key>TMPDIR</key><string>$(getconf DARWIN_USER_TEMP_DIR)</string>
  </dict>
</dict>
</plist>
P
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5; do launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && break; sleep 1; done
    for _ in $(seq 1 60); do "$PY" -c "import urllib.request; urllib.request.urlopen('$URL', timeout=1)" 2>/dev/null && break; sleep 0.5; done
    ok "running in the background (starts by itself when you log in)"
    echo; echo "${B}Next:${N} open ${B}$URL${N} and follow the 5 setup steps."
    echo "  macOS will ask whether ${B}Python${N} may record the screen and system audio. That's DogMute hearing Discord:"
    echo "  turn it on in System Settings > Privacy & Security > Screen & System Audio Recording, then press Restart DogMute."
    [ -z "${DOGMUTE_NO_OPEN:-}" ] && open "$URL" 2>/dev/null || true
  else
    echo; echo "${B}Next:${N} start it with  $DIR/.venv/bin/python -m dogmute  then open $URL and follow the 5 setup steps."
  fi
  exit 0
fi

mkdir -p "$(dirname "$APP")"
if [ $SERVICE = 1 ]; then
  mkdir -p "$(dirname "$UNIT")"; rm -f "$UNIT"
  cat > "$UNIT" <<U
[Unit]
Description=DogMute: auto local-mute for Discord calls
After=pipewire.service graphical-session.target
Wants=pipewire.service

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m dogmute
Restart=always
RestartSec=3
Nice=5
Environment=PYTHONUNBUFFERED=1
Environment=HF_HUB_DISABLE_TELEMETRY=1
Environment=HF_HUB_VERBOSITY=error

[Install]
WantedBy=default.target
U
fi
cat > "$APP" <<D
[Desktop Entry]
Type=Application
Name=DogMute
Comment=Auto local-mute for slurs and mic spam in your Discord calls
Exec=/usr/bin/python3 $DIR/window.py
Icon=$DIR/dogmute/static/icon.png
Terminal=false
Categories=Network;Chat;AudioVideo;
StartupWMClass=dogmute
D
if [ $DESKTOP = 1 ] && [ -d "$(dirname "$DESK")" ]; then
  cp "$APP" "$DESK" && chmod +x "$DESK"; gio set "$DESK" metadata::trusted true 2>/dev/null || true
fi
update-desktop-database ~/.local/share/applications 2>/dev/null || true
ok "DogMute in your app menu"

if [ $SERVICE = 1 ]; then
  systemctl --user daemon-reload
  systemctl --user enable dogmute.service >/dev/null 2>&1
  systemctl --user restart dogmute.service
  for _ in $(seq 1 40); do python3 -c "import urllib.request; urllib.request.urlopen('$URL', timeout=1)" 2>/dev/null && break; sleep 0.5; done
  ok "running in the background (starts by itself when you log in)"
  echo; echo "${B}Next:${N} open ${B}$URL${N} (or DogMute in your app menu) and follow the 5 setup steps."
  if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ -z "${DOGMUTE_NO_OPEN:-}" ]; then xdg-open "$URL" >/dev/null 2>&1 || true; fi
else
  echo; echo "${B}Next:${N} start it with  $DIR/.venv/bin/python -m dogmute  then open $URL and follow the 5 setup steps."
fi
