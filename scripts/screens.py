#!/usr/bin/env python3
"""README screenshots (docs/*.png) from the real page, fed made-up states: no Discord, no call, no real names.

    python3 scripts/screens.py          (system Python with python3-gi + WebKit2GTK 4.1; it starts the fake server itself)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18870
SCENE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "dogmute-scene"

PEOPLE = [{"id": "1", "name": "Sam", "mute": False, "bot": False, "talking": True, "friend": True, "ours": False, "level": -24.0, "voice": True},
          {"id": "2", "name": "Alex", "mute": False, "bot": False, "talking": False, "friend": False, "ours": False, "level": -31.0, "voice": True},
          {"id": "3", "name": "Rando123", "mute": True, "bot": False, "talking": False, "friend": False, "ours": True, "level": -3.0, "voice": False}]
LOG = [{"t": "2026-10-20T21:14:09", "action": "muted", "user_id": "3", "name": "Rando123", "reason": "earrape (-4 dBFS)"},
       {"t": "2026-10-20T21:02:51", "action": "unclear", "user_id": None, "name": "Sam, Alex", "candidates": ["1", "2"],
        "reason": 'said "k*******", couldn\'t tell who (several talking)'}]


def setup(step, **kw):
    s = {"done": False, "has_creds": step > 2, "authorized": step > 3, "connected": step > 3, "discord_open": True,
         "authorizing": step == 3, "err": "", "audio_ok": step > 4, "test": {"on": False, "heard": None}}
    s.update(kw)
    return s


SCENES = {
    "setup-1": {"setup": setup(1), "h": 700},
    "setup-2": {"setup": setup(2), "h": 760, "js": "localStorage.setItem('dm.step1','1')", "after": (
        "document.getElementById('cid').value='1111111111111111111';document.getElementById('csec').value='xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx';"
        "const e=document.getElementById('wz2Err');e.hidden=false;e.textContent=\"Discord doesn't know that Client ID + secret pair. "
        "On your app's OAuth2 page copy the Client ID again, press Reset Secret for a fresh secret, and paste both.\"")},
    "setup-3": {"setup": setup(3), "h": 560, "js": "localStorage.setItem('dm.step1','1')"},
    "setup-4": {"setup": setup(4), "me": "you", "h": 560},
    "setup-4-mac": {"setup": setup(4), "me": "you", "h": 760, "platform": "mac",
                    "audio": "macOS needs your OK: allow Screen & System Audio Recording for Python"},
    "setup-5": {"setup": setup(6, test={"on": False, "heard": "Sam"}), "me": "you", "h": 520},
    "page": {"setup": setup(6, done=True), "me": "you", "people": PEOPLE, "log": LOG,
             "channel": {"id": "c", "name": "general", "guild_id": "g"}, "h": 520},
}


def serve():
    sys.path.insert(0, str(ROOT))
    import asyncio
    import threading

    from dogmute import audio, core
    from dogmute.web import serve as start

    audio.streams = lambda dump=None: [{"key": "Discord · playStream", "label": "Discord · playStream", "discord": True},
                                       {"key": "Firefox · Playback", "label": "Firefox · Playback", "discord": False}]

    class Fake:
        cfg = core._merge(core.DEFAULTS, {"port": PORT})
        words_path = ROOT / "dogmute" / "data" / "words.default.b64"
        capture = type("C", (), {"status": "listening"})()
        loop = asyncio.new_event_loop()
        wake = asyncio.Event()

        def scene(self):
            return SCENES.get(SCENE.read_text().strip() if SCENE.exists() else "setup-1", SCENES["setup-1"])

        def setup_view(self):
            return self.scene()["setup"]

        def snapshot(self):
            sc = self.scene()
            st = sc["setup"]
            return {"state": "connected" if st["connected"] else "authorize in Discord" if st["authorizing"] else "setup",
                    "me": sc.get("me"), "channel": sc.get("channel"), "people": sc.get("people", []), "log": sc.get("log", []),
                    "test_mode": False, "auto_unmute_min": 0, "audio": sc.get("audio", "listening" if st["audio_ok"] else "waiting for Discord audio"),
                    "stt": "ready (distil-large-v3, GPU)", "stt_ms": 452, "voices": "ready", "voices_known": 2,
                    "friends": {"1": "Sam"}, "has_creds": st["has_creds"], "setup": st, "hook": "off", "platform": sc.get("platform", "linux")}

    threading.Thread(target=Fake.loop.run_forever, daemon=True).start()
    start(Fake())
    while True:
        time.sleep(3600)


def shoot():
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")   # offscreen windows have no GL context
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import GLib, Gtk, WebKit2

    srv = subprocess.Popen([str(ROOT / ".venv/bin/python"), __file__, "--serve"])
    time.sleep(3)
    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    win = Gtk.OffscreenWindow()
    wv = WebKit2.WebView()
    wv.set_size_request(640, 900)
    win.add(wv)
    win.show_all()
    names = list(SCENES)

    def grab(name):
        win.get_pixbuf().savev(str(out / f"{name}.png"), "png", [], [])   # (no cairo bridge needed)
        print("wrote", out / f"{name}.png", flush=True)
        GLib.timeout_add(100, step)
        return False

    def snap(name):
        sc = SCENES[name]
        if sc.get("after"):
            wv.evaluate_javascript(sc["after"], -1, None, None, None, None, None)
        GLib.timeout_add(500, grab, name)
        return False

    def step():
        if not names:
            srv.terminate()
            Gtk.main_quit()
            return False
        name = names.pop(0)
        SCENE.write_text(name)
        wv.set_size_request(640, SCENES[name].get("h", 860))
        js = SCENES[name].get("js", "localStorage.clear()")
        wv.load_uri(f"http://127.0.0.1:{PORT}/")
        def loaded(view, ev):
            if ev == WebKit2.LoadEvent.FINISHED:
                view.disconnect(hid[0])
                view.evaluate_javascript(js + ";document.querySelector('#settings')&&0", -1, None, None, None, None, None)
                view.reload() if "dm.step1" in js and not getattr(view, "_again", False) else None
                GLib.timeout_add(2500, lambda: snap(name))
        hid = [wv.connect("load-changed", loaded)]
        return False

    GLib.timeout_add(100, step)
    Gtk.main()


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else shoot()
