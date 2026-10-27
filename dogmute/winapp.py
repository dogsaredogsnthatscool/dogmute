"""DogMute on Windows: the tray icon, with the engine kept running behind it (Restart on the page is the engine exiting;
this starts it again, as systemd and launchd do elsewhere).

  DogMute.exe                 tray icon + engine, and the page in its own window
  DogMute.exe --background    the same without the window (what runs when you sign in)
  DogMute.exe --engine        the engine alone (the tray runs this)
"""
import ctypes
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import windows as W

MUTEX = "Local\\DogMute"   # one DogMute per signed-in user
ALREADY_EXISTS = 183


def up(p):
    try:
        socket.create_connection(("127.0.0.1", p), 0.3).close()
        return True
    except OSError:
        return False


def engine_cmd():
    return [sys.executable, "--engine"] if W.FROZEN else [sys.executable, "-m", "dogmute"]


class Tray:
    def __init__(self):
        self.proc = None
        self.quitting = False

    def supervise(self):
        crashes = []
        while not self.quitting:
            started = time.monotonic()
            self.proc = subprocess.Popen(engine_cmd(), cwd=W.app_dir(), creationflags=W.NO_WINDOW,
                                         stdin=subprocess.DEVNULL)
            code = self.proc.wait()
            if self.quitting:
                return
            now = time.monotonic()
            if now - started < 30:
                crashes = [t for t in crashes if t > now - 300] + [now]
            print(f"engine exited ({code}), starting it again", flush=True)
            time.sleep(30 if len(crashes) >= 5 else 1)   # crashing over and over: don't spin

    def restart(self, *_):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def open(self, *_):
        W.open_window(W.port())

    def quit(self, icon, *_):
        self.quitting = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        icon.stop()


def open_when_up():
    p = W.port()
    for _ in range(150):
        if up(p):
            break
        time.sleep(0.2)
    W.open_window(p)


def main():
    if "--engine" in sys.argv:
        from .__main__ import main as engine
        return engine()
    W.state_dir().mkdir(parents=True, exist_ok=True)
    if W.FROZEN:
        sys.stdout = sys.stderr = open(W.state_dir() / "tray.log", "w", buffering=1, encoding="utf-8", errors="replace")
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, MUTEX)   # noqa: F841  (held until DogMute quits)
    if kernel32.GetLastError() == ALREADY_EXISTS:      # already running: just show it
        if "--background" not in sys.argv:
            W.open_window(W.port())
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(W.APP_ID)

    import pystray
    from PIL import Image

    tray = Tray()
    threading.Thread(target=tray.supervise, daemon=True).start()
    if "--background" not in sys.argv:
        threading.Thread(target=open_when_up, daemon=True).start()
    image = Image.open(Path(__file__).resolve().parent / "static" / "icon.png")
    icon = pystray.Icon("DogMute", image, "DogMute", menu=pystray.Menu(
        pystray.MenuItem("Open DogMute", tray.open, default=True),
        pystray.MenuItem("Restart DogMute", tray.restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit DogMute", tray.quit)))
    icon.run()


if __name__ == "__main__":
    main()
