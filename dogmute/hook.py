"""Overlay hook: each mute is POSTed as JSON to a URL you choose (a stream overlay, a bot, a camera effect...).

hook.url may hold several URLs (space separated): each gets every POST.
Payload: {"cmd": "killfeed", "event": "mute", "name", "by", "reason", "clip", "key", "hit_at"}
clip / key / hit_at are hints for overlays that play a video: the clip's name (clip_prefix + file in clip_dir),
how to key it (green / screen / full) and when the hit lands (seconds, 0 = clip end). Overlays may ignore them.
"""
import json
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".gif"}


def _dir(c):
    return Path(c["clip_dir"]).expanduser() if c.get("clip_dir") else None


def clips(c):
    d = _dir(c)
    return sorted(p.name for p in d.iterdir() if p.suffix.lower() in EXT and p.is_file()) if d and d.is_dir() else []


def save_clip(c, name, data):
    d = _dir(c)
    if not d:
        raise ValueError("set a clip folder first")
    name = Path(name).name
    if Path(name).suffix.lower() not in EXT:
        raise ValueError("a video file: mp4, mov, webm, mkv, m4v or gif")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return name


def sync(c):
    """Run the configured sync command after a clip upload ({dir} = the clip folder). None when there is none."""
    cmd = (c.get("sync_cmd") or "").strip()
    if not cmd:
        return None
    args = [a.replace("{dir}", str(_dir(c))) for a in shlex.split(cmd)]
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


class Hook:
    def __init__(self, core):
        self.core = core
        self.status = "ready"

    @property
    def c(self):
        return self.core.cfg["hook"]

    def _post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                r.read()
            self.status = "ok"
        except urllib.error.HTTPError as e:
            self.status = f"the overlay answered {e.code}"
        except OSError:
            self.status = "overlay not reachable"

    def _send(self, body):
        if self.c["enabled"]:
            for url in self.c["url"].split():
                threading.Thread(target=self._post, args=(url, body), daemon=True).start()

    def clip_name(self):
        return (self.c.get("clip_prefix") or "") + self.c["clip"] if self.c["clip"] else ""

    def hit(self, name, reason=""):
        self._send({"cmd": "killfeed", "event": "mute", "name": name, "by": (self.core.me or {}).get("username", ""),
                    "reason": reason, "clip": self.clip_name(), "key": self.c["key"], "hit_at": self.c["hit_at"]})

    def preload(self):
        if self.clip_name():
            self._send({"cmd": "killfeed", "event": "preload", "clip": self.clip_name(), "preload": True})
