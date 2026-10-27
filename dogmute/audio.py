"""Discord's playback stream (what you hear from the call), captured in memory: through PipeWire on Linux, and on macOS
and Windows through bin/dogmute-listen (macOS: ScreenCaptureKit, or a BlackHole loopback device; Windows: WASAPI's
loopback of one program, or of the speakers). Nothing is written to disk."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

RATE = 16000
BLOCK = 320  # 20 ms
GAP_S = 0.2  # a block this much later than the clock says = the stream stalled (blocks arrive within ~20 ms otherwise)
MAC = sys.platform == "darwin"
WIN = sys.platform == "win32"
HELPER = Path(os.environ.get("DOGMUTE_LISTEN", Path(__file__).resolve().parent.parent / "bin"
                             / ("dogmute-listen.exe" if WIN else "dogmute-listen")))
DISCORD_BUNDLE = "com.hnc.Discord"           # a prefix: also PTB, Canary and Discord's helper processes
DISCORD_EXE = "discord"                      # Windows: an exe name prefix, so Discord.exe, DiscordPTB.exe, DiscordCanary.exe
LOOPBACK = ("blackhole", "loopback", "soundflower")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: no console window flashing up for the helper
WAITING = "waiting for Discord audio"       # the setup page knows this one
PERMISSION = {"screen": "macOS needs your OK: allow Screen & System Audio Recording for Python",
              "microphone": "macOS needs your OK: allow Microphone for Python (to read the loopback device)"}


def _dump():
    try:
        return json.loads(subprocess.run(["pw-dump"], capture_output=True, timeout=5).stdout or b"[]")
    except Exception:
        return []


def _is_discord(p):
    return ("discord" in str(p.get("application.process.binary", "")).lower()
            or "discord" in str(p.get("pipewire.access.portal.app_id", "")).lower()
            or "discord" in str(p.get("application.name", "")).lower())


def _label(p):
    app = p.get("application.name") or p.get("application.process.binary") or "?"
    if _is_discord(p) and "discord" not in app.lower():
        app = f"Discord ({app})"
    return f"{app} · {p.get('media.name') or p.get('node.name') or '?'}"


def _helper_list():
    try:
        return json.loads(subprocess.run([str(HELPER), "list"], capture_output=True, timeout=8,
                                         creationflags=NO_WINDOW).stdout or b"{}")
    except Exception:
        return {}


def mac_streams(info=None):
    """macOS sources: apps with a window (ScreenCaptureKit), every app's sound, and loopback devices such as BlackHole."""
    info = _helper_list() if info is None else info
    out, seen = [], set()
    for a in sorted(info.get("apps", []), key=lambda a: a.get("name", "").lower()):
        b, name = a.get("bundle", ""), a.get("name", "")
        if not b or b in seen or ".helper" in b.lower() or b.startswith("org.python"):
            continue
        seen.add(b)
        out.append({"key": f"app:{b}", "label": name or b, "discord": b.startswith(DISCORD_BUNDLE)})
    out.append({"key": "all", "label": "Every app's sound on this Mac", "discord": False})
    for d in info.get("devices", []):
        if any(w in d.lower() for w in LOOPBACK):
            out.append({"key": f"device:{d}", "label": f"{d} (loopback device)", "discord": False})
    return out


def mac_source(target):
    """dogmute-listen's arguments for an audio.target, or None. "" = Discord through ScreenCaptureKit (macOS 13+)."""
    if not target:
        return ["app", DISCORD_BUNDLE]
    if target == "all":
        return ["all"]
    kind, _, rest = target.partition(":")
    if kind == "app" and rest:
        return ["app", rest]
    if kind == "device" and any(w in rest.lower() for w in LOOPBACK):   # never a microphone
        return ["device", rest]
    return None


def win_streams(info=None):
    """Windows sources: programs playing sound (each one on its own, Windows 10 2004+), every app's sound, and each
    output device's sound. Discord is listed even before it plays anything."""
    info = _helper_list() if info is None else info
    out, seen = [], set()
    if info.get("app_capture", True):
        for a in sorted(info.get("apps", []), key=lambda a: a.get("name", "").lower()):
            exe = a.get("exe", "").lower()
            if not exe or exe in seen or exe.startswith(("python", "dogmute")):
                continue
            seen.add(exe)
            out.append({"key": f"app:{exe}", "label": a.get("name") or exe, "discord": exe.startswith(DISCORD_EXE)})
    out.append({"key": "all", "label": "Every app's sound on this PC", "discord": False})
    for d in info.get("devices", []):
        out.append({"key": f"device:{d}", "label": f"Everything playing on {d}", "discord": False})
    return out


def win_source(target):
    """dogmute-listen.exe's arguments for an audio.target, or None. "" = Discord on its own (process loopback).
    Devices are only ever outputs: the helper can't open a microphone."""
    if not target:
        return ["app", DISCORD_EXE]
    if target == "all":
        return ["all"]
    kind, _, rest = target.partition(":")
    return [kind, rest] if kind in ("app", "device") and rest else None


def streams(dump=None):
    """Every app playing sound right now: [{"key", "label", "discord"}] (key = what audio.target stores)."""
    if MAC:
        return mac_streams()
    if WIN:
        return win_streams()
    out = []
    for o in dump if dump is not None else _dump():
        p = (o.get("info") or {}).get("props") or {}
        if o.get("type") == "PipeWire:Interface:Node" and p.get("media.class") == "Stream/Output/Audio":
            out.append({"key": _label(p), "label": _label(p), "id": o["id"], "serial": p.get("object.serial", o["id"]),
                        "discord": _is_discord(p) and p.get("media.name") == "playStream"})
    return out


def find_play_stream(dump=None, target=""):
    """The stream to listen to: (id, serial) or None. target "" = Discord's voice output (its playStream)."""
    for s in streams(dump):
        if (s["key"] == target) if target else s["discord"]:
            return s["id"], s["serial"]
    return None


def link_sources(dump, pid):
    """Node ids feeding our pw-record process (to be sure we never fell back to a mic)."""
    ours = {o["id"] for o in dump if o.get("type") == "PipeWire:Interface:Node"
            and ((o.get("info") or {}).get("props") or {}).get("application.process.id") in (pid, str(pid))}
    return {(o.get("info") or {}).get("output-node-id") for o in dump
            if o.get("type") == "PipeWire:Interface:Link" and (o.get("info") or {}).get("input-node-id") in ours}


class Ring:
    """Last `seconds` of audio with a sample-index -> monotonic-time map."""

    def __init__(self, seconds=30):
        self.buf = np.zeros(seconds * RATE, np.float32)
        self.total = 0
        self.offset = None  # monotonic time of sample 0 (least-delayed estimate)
        self.last_push = -99.0
        self.lock = threading.Lock()

    def _put(self, x):
        n, N = len(x), len(self.buf)
        i = self.total % N
        k = min(n, N - i)
        self.buf[i:i + k] = x[:k]
        self.buf[:n - k] = x[k:]
        self.total += n

    def push(self, x, now):
        # Discord's stream carries the odd NaN; one would blank whisper's whole window and the loudness rules with it
        x = np.nan_to_num(x, nan=0.0, posinf=1.0, neginf=-1.0)
        with self.lock:
            if self.offset is not None:
                gap = now - self.offset - (self.total + len(x)) / RATE
                if gap > GAP_S:       # the stream stalled: silence in its place, so every sample keeps its real time
                    n = int(gap * RATE)
                    if n >= len(self.buf):
                        self.buf[:] = 0
                        self.total += n
                    else:
                        self._put(np.zeros(n, np.float32))
            self._put(x)
            self.last_push = now
            est = now - self.total / RATE
            # take the earliest arrival seen; drift slowly so clock skew can't pin it forever
            if self.offset is None or est < self.offset:
                self.offset = est
            else:
                self.offset += 0.0005 * (est - self.offset)

    def t(self, idx):
        return self.offset + idx / RATE

    def idx(self, t):
        return int(round((t - self.offset) * RATE))

    def get(self, t0, t1):
        """Audio between monotonic times t0..t1 (clipped to what's buffered) and its start time."""
        with self.lock:
            if self.offset is None:
                return np.zeros(0, np.float32), t0
            N = len(self.buf)
            a, b = max(self.idx(t0), self.total - N + 1, 0), min(self.idx(t1), self.total)
            if b <= a:
                return np.zeros(0, np.float32), t0
            ia, ib = a % N, b % N
            x = self.buf[ia:ib].copy() if ia < ib else np.concatenate([self.buf[ia:], self.buf[:ib]])
            return x, self.t(a)


class Capture(threading.Thread):
    """Runs pw-record on Discord's playStream; restarts when Discord or the stream comes and goes."""

    def __init__(self, on_block, target=lambda: ""):
        super().__init__(daemon=True)
        self.on_block = on_block
        self.target = target      # callable: the audio.target setting, read live
        self.status = "starting"
        self.proc = None
        self.stop = False
        self.asked = False        # macOS: show the permission prompt once per run, not every retry

    def _read(self, first=None):
        need = BLOCK * 4
        pending = b""
        while not self.stop:
            chunk = self.proc.stdout.read(need - len(pending))
            if not chunk:
                break
            pending += chunk
            if len(pending) == need:
                if first:
                    self.status, first = first, None
                self.on_block(np.frombuffer(pending, np.float32), time.monotonic())
                pending = b""

    def run(self):
        if MAC or WIN:
            return self._run_helper()
        while not self.stop:
            node = find_play_stream(target=self.target())
            if not node:
                self.status = "waiting for Discord audio" if not self.target() else "that sound source is gone"
                time.sleep(3)
                continue
            nid, serial = node
            self.proc = subprocess.Popen(
                ["pw-record", "--target", str(serial), "--rate", str(RATE), "--channels", "1", "--format", "f32",
                 "-P", "{ node.dont-reconnect = true node.dont-fallback = true node.name = dogmute_listen }", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.status = "listening"
            checker = threading.Thread(target=self._watch, args=(nid, self.proc), daemon=True)
            checker.start()
            try:
                self._read()
            finally:
                self.proc.kill()
                self.proc.wait()
            self.status = "reconnecting"
            time.sleep(1)

    def _watch(self, nid, proc):
        time.sleep(1.5)
        while proc.poll() is None:
            d = _dump()
            node = find_play_stream(d, self.target())
            src = link_sources(d, proc.pid)
            if not node or node[0] != nid or (src and src != {nid}):
                # Discord's stream changed, or we got linked to something that isn't Discord (a mic): drop it
                self.status = "re-finding Discord audio"
                proc.kill()
                return
            time.sleep(5)

    def _run_helper(self):
        """bin/dogmute-listen does the listening; it exits when its source goes away and tells why in its exit code."""
        while not self.stop:
            target = self.target()
            args = (win_source if WIN else mac_source)(target)
            if not HELPER.exists():
                self.status = ("the sound helper is missing: install DogMute again" if WIN
                               else "the macOS sound helper is missing: run ./install.sh again")
                time.sleep(5)
                continue
            if not args:
                self.status = "that sound source is gone"
                time.sleep(3)
                continue
            if MAC and not self.asked:
                args, self.asked = args + ["--ask"], True
            self.status = WAITING if not target else f"waiting for sound from {target.partition(':')[2] or 'every app'}"
            self.proc = subprocess.Popen([str(HELPER), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                         creationflags=NO_WINDOW)
            try:
                self._read(first="listening")
            finally:
                self.proc.kill()
                code = self.proc.wait()
                err = self.proc.stderr.read().decode(errors="replace").strip().splitlines()
            self.status, wait = self._helper_status(code, err[-1] if err else "", target)
            time.sleep(wait)

    def _helper_status(self, code, err, target):
        if code == 3:
            return PERMISSION["microphone" if "microphone" in err else "screen"], 5
        if code == 2:
            return (WAITING if not target else "that sound source is gone"), 3
        if code == 4:
            if WIN:
                return "this PC needs Windows 10 version 2004 or newer to hear Discord on its own: pick Every app's sound", 10
            return "this Mac needs macOS 13 or newer to hear Discord: pick a BlackHole device instead", 10
        if code in (0, -9):
            return "reconnecting", 1
        return f"sound helper stopped: {err or code}", 3
