"""Updates: at start and every 6 hours DogMute asks GitHub for the latest release (the only thing sent is that request).

A newer one shows on the page and as a notification. On Windows it then patches itself: DogMute-Setup.exe from that
release is downloaded, checked against the SHA-256 GitHub lists for it, and run silently (your settings, words and
models stay; the graphics card files aren't fetched again), once you're not in a voice call. DogMute restarts by itself.
Turn "updates.auto" off to only be told. Linux and macOS: the page shows the command (git pull, then ./install.sh).
"""
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = "dogsaredogsnthatscool/dogmute"
# DOGMUTE_UPDATE_URL: a stand-in for GitHub's answer when testing the patcher (http(s):// or file:///C:/...)
API = os.environ.get("DOGMUTE_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"
ROOT = Path(__file__).resolve().parent.parent
EVERY_S = 6 * 3600
WIN = sys.platform == "win32"


def current():
    """This copy's version: written by win/build.ps1 into the Windows build, else the checkout's latest tag."""
    try:
        from ._version import VERSION
        return VERSION
    except ImportError:
        pass
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "describe", "--tags", "--abbrev=0"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip().lstrip("v") or None
    except (OSError, subprocess.SubprocessError):
        return None


def parse(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(map(int, m.groups())) if m else None


def newer(latest, cur):
    a, b = parse(latest), parse(cur)
    return bool(a and b and a > b)


def _tls():
    """certifi's list of certificate authorities: a fresh Windows only has the roots it has needed so far, and Python
    can't fetch the missing ones the way Windows does (CERTIFICATE_VERIFY_FAILED on api.github.com)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        return None


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "DogMute-updater", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=_tls() if url.startswith("https:") else None)


def latest():
    """{"version", "page", "setup", "sha256"} of GitHub's latest release (setup/sha256 None without an installer)."""
    with _get(API) as r:
        d = json.load(r)
    a = next((a for a in d.get("assets", []) if a.get("name") == "DogMute-Setup.exe"), {})
    digest = a.get("digest") or ""
    return {"version": str(d.get("tag_name", "")).lstrip("v"), "page": d.get("html_url"),
            "setup": a.get("browser_download_url"), "sha256": digest[7:] if digest.startswith("sha256:") else None}


class Updates(threading.Thread):
    def __init__(self, core):
        super().__init__(daemon=True)
        self.core = core
        self.version = current()
        self.new = None           # latest() when it's newer than this copy
        self.status = "off"       # for the page: what the patcher is doing
        self.wake = threading.Event()
        self._told = None
        self._failed = None       # the version whose patch failed: retried after the next check, not every 30 s

    @property
    def c(self):
        return self.core.cfg["updates"]

    def view(self):
        return {"version": self.version, "new": self.new, "status": self.status,
                "auto": WIN and self.c["auto"], "can_patch": WIN and bool(self.new and self.new["sha256"])}

    def check(self):
        if not (self.c["check"] and parse(self.version) and self.version != "0.0.0"):
            self.status = "off"
            return
        try:
            n = latest()
        except (OSError, ValueError) as e:
            self.status = f"update check couldn't reach GitHub ({e})"[:120]
            return
        self.new = n if newer(n["version"], self.version) else None
        self._failed = None
        self.status = "up to date" if not self.new else "new version"
        if self.new and self._told != n["version"]:
            self._told = n["version"]
            print(f"update: DogMute {n['version']} is out (this is {self.version})", flush=True)
            self.core.notify(f"DogMute {n['version']} is out",
                             "It installs itself when you leave the call." if WIN and self.c["auto"]
                             else "Open DogMute to update.")

    def patch(self):
        """Windows: download the new installer, check it, run it silently. It closes this DogMute and starts the new one."""
        n = self.new
        if not (WIN and n and n["setup"] and n["sha256"]):
            raise RuntimeError("no checked installer for this release: download it from the release page")
        from .core import STATE
        d = STATE / "update"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"DogMute-Setup-{n['version']}.exe"
        self.status = f"downloading {n['version']}"
        h = hashlib.sha256()
        with _get(n["setup"], timeout=60) as r, open(f.with_suffix(".part"), "wb") as out:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != n["sha256"].lower():
            f.with_suffix(".part").unlink(missing_ok=True)
            self.status = "download didn't match GitHub's checksum"
            raise RuntimeError(self.status)
        f.with_suffix(".part").replace(f)
        for old in d.glob("DogMute-Setup-*.exe"):
            if old != f:
                old.unlink(missing_ok=True)
        self.status = f"installing {n['version']}"
        print(f"update: installing {n['version']}", flush=True)
        # through "start": the installer must not be DogMute's child, as it ends DogMute's whole process tree first
        subprocess.Popen(["cmd.exe", "/c", "start", "", str(f), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          f"/LOG={d / 'setup.log'}"], creationflags=0x08000000, close_fds=True)   # CREATE_NO_WINDOW

    def patch_now(self):
        try:
            self.patch()
        except (OSError, RuntimeError) as e:
            self._failed = (self.new or {}).get("version")
            self.status = f"update didn't install ({e})"[:160]
            print("update:", self.status, flush=True)

    def run(self):
        last = 0.0
        while True:
            if time.monotonic() - last >= EVERY_S or self.wake.is_set():
                self.wake.clear()
                last = time.monotonic()
                self.check()
            if WIN and self.c["auto"] and self.new and self.new["sha256"] and self.core.channel is None \
                    and self._failed != self.new["version"] and not self.status.startswith("installing"):
                self.patch_now()
            self.wake.wait(30)
