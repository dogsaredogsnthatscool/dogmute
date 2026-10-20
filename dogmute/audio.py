"""Discord's playback stream (what you hear from the call), captured in memory through PipeWire.
Nothing is written to disk."""
import json
import subprocess
import threading
import time

import numpy as np

RATE = 16000
BLOCK = 320  # 20 ms


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


def streams(dump=None):
    """Every app playing sound right now: [{"key", "label", "discord"}] (key = what audio.target stores)."""
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

    def push(self, x, now):
        with self.lock:
            n, N = len(x), len(self.buf)
            i = self.total % N
            k = min(n, N - i)
            self.buf[i:i + k] = x[:k]
            self.buf[:n - k] = x[k:]
            self.total += n
            self.last_push = now
            est = now - self.total / RATE
            # take the earliest arrival seen; drift slowly so clock skew can't pin it forever
            if self.offset is None or est < self.offset or est - self.offset > 0.5:  # first block, earlier, or a gap
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

    def run(self):
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
            need = BLOCK * 4
            pending = b""
            try:
                while not self.stop:
                    chunk = self.proc.stdout.read(need - len(pending))
                    if not chunk:
                        break
                    pending += chunk
                    if len(pending) == need:
                        self.on_block(np.frombuffer(pending, np.float32), time.monotonic())
                        pending = b""
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
