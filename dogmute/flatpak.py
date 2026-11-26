"""DogMute's Flatpak launcher (the `dogmute` command in com.dogmosh.DogMute).

Opening DogMute starts the engine (python -m dogmute) and keeps it running, restarting it when it exits (Restart
DogMute, a crash), then opens the page in your browser. While it runs, opening DogMute again only opens the page.
It asks the desktop (the Background portal) to start it when you log in: then it runs with --background, no page.

  dogmute                 start if needed, open the page
  dogmute --background    start if needed, no page (the login start)
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_ID = "com.dogmosh.DogMute"
CONF = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "dogmute"


def port():
    try:
        return int(json.loads((CONF / "config.json").read_text()).get("port", 8853))
    except (OSError, ValueError, TypeError):
        return 8853


def up(p):
    try:
        socket.create_connection(("127.0.0.1", p), 0.3).close()
        return True
    except OSError:
        return False


def open_page(p):
    subprocess.run(["xdg-open", f"http://127.0.0.1:{p}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def autostart():
    """Ask once per start to run at login (the desktop remembers the answer)."""
    opts = ("{'reason': <'DogMute mutes people in your Discord calls, so it runs in the background.'>, "
            "'autostart': <true>, 'commandline': <['dogmute', '--background']>, 'dbus-activatable': <false>}")
    try:
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop", "--object-path",
                        "/org/freedesktop/portal/desktop", "--method", "org.freedesktop.portal.Background.RequestBackground",
                        "", opts], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def open_when_up(p):
    for _ in range(300):
        if up(p):
            return open_page(p)
        time.sleep(0.2)


def supervise(cmd=None):
    """Runs the engine and starts it again when it exits (Restart DogMute, a crash). Also used by dogmute/macapp.py."""
    crashes = []
    while True:
        started = time.monotonic()
        code = subprocess.run(cmd or [sys.executable, "-m", "dogmute"], stdin=subprocess.DEVNULL).returncode
        now = time.monotonic()
        if now - started < 30:
            crashes = [t for t in crashes if t > now - 300] + [now]
        print(f"engine exited ({code}), starting it again", flush=True)
        time.sleep(30 if len(crashes) >= 5 else 1)   # crashing over and over: don't spin


def main():
    p = port()
    background = "--background" in sys.argv
    if up(p):   # already running
        if not background:
            open_page(p)
        return
    threading.Thread(target=autostart, daemon=True).start()
    if not background:
        threading.Thread(target=open_when_up, args=(p,), daemon=True).start()
    supervise()


if __name__ == "__main__":
    main()
