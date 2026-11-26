"""DogMute.app on macOS (mac/build.sh makes it): the engine kept running, the page in your browser, and a start at login.

Opening DogMute starts the engine and keeps it running (Restart DogMute on the page is the engine exiting; this starts
it again, as launchd does for install.sh's agent), then opens the page. While it runs, opening DogMute again only opens
the page. It adds a login item (~/Library/LaunchAgents/com.dogmosh.DogMute.plist) that starts it with --background.

  DogMute                 start if needed, open the page
  DogMute --background    start if needed, no page (the login start)
  DogMute --engine        the engine alone (what this runs)
  DogMute --selfcheck     load every library the app bundles, print what failed (the build checks this)
"""
import os
import plistlib
import subprocess
import sys
import threading
from pathlib import Path

from .flatpak import port, supervise, up

LABEL = "com.dogmosh.DogMute"
FROZEN = bool(getattr(sys, "frozen", False))
LOG = Path.home() / "Library/Logs/DogMute.log"


def open_page(p):
    subprocess.run(["open", f"http://127.0.0.1:{p}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_when_up(p):
    import time
    for _ in range(300):
        if up(p):
            return open_page(p)
        time.sleep(0.2)


def login_item():
    """Start at login: written each start, so it follows the app when it is moved. Not loaded now (that would start a
    second DogMute): launchd reads it at the next login."""
    f = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    want = {"Label": LABEL, "ProgramArguments": [sys.executable, "--background"], "RunAtLoad": True,
            "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}
    try:
        if f.exists() and plistlib.loads(f.read_bytes()) == want:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(plistlib.dumps(want))
    except (OSError, ValueError):
        pass


def selfcheck():
    import importlib
    import platform
    names = ["numpy", "ctranslate2", "faster_whisper", "onnxruntime", "huggingface_hub", "cv2", "pyvirtualcam",
             "PIL", "certifi", "dogmute.core"]   # no av: the app leaves PyAV out (dogmute/__init__.py)
    if platform.machine() == "arm64":
        names += ["mlx.core", "mlx_whisper", "mlx_whisper.transcribe", "tiktoken_ext.openai_public"]
    bad = []
    for n in names:
        try:
            importlib.import_module(n)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{n}: {e}")
    from . import audio
    print("helper:", audio.HELPER, "found" if audio.HELPER.exists() else "MISSING")
    print("\n".join(bad) or f"selfcheck ok ({len(names)} libraries)")
    return 1 if bad or not audio.HELPER.exists() else 0


def main():
    if "--engine" in sys.argv:
        from .__main__ import main as engine
        return engine()
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    if FROZEN and not os.environ.get("DOGMUTE_LOG_STDOUT"):   # a Finder start has nowhere to print: the log file
        LOG.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(sys.stdout.fileno(), 1)
        os.dup2(sys.stdout.fileno(), 2)
    p = port()
    background = "--background" in sys.argv
    if up(p):
        if not background:
            open_page(p)
        return
    if FROZEN and not os.environ.get("DOGMUTE_NO_AUTOSTART"):
        login_item()
    if not background:
        threading.Thread(target=open_when_up, args=(p,), daemon=True).start()
    supervise([sys.executable, "--engine"] if FROZEN else [sys.executable, "-m", "dogmute"])


if __name__ == "__main__":
    main()
