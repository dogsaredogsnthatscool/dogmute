"""Tiny local page + JSON API on 127.0.0.1."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".png": "image/png", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}


def serve(core):
    loop = core.loop

    def run(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(10)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/state":
                return self._send(200, core.snapshot())
            if path == "/api/words":
                return self._send(200, {"text": core.words_path.read_text()})
            if path == "/api/audio":
                from .audio import streams
                return self._send(200, {"streams": [{k: s[k] for k in ("key", "label", "discord")} for s in streams()],
                                        "target": core.cfg["audio"]["target"], "status": core.capture.status,
                                        "flowing": core.setup_view()["audio_ok"]})
            if path == "/api/config":
                from .core import DEFAULTS, MODELS, SCHEMA
                from .hook import clips
                return self._send(200, {"cfg": core.cfg, "defaults": DEFAULTS, "models": MODELS, "clips": clips(core.cfg["hook"]),
                                        "limits": {k: v[1:] for k, v in SCHEMA.items() if v[0] in (int, float)}})
            f = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if f.resolve().parent != STATIC or not f.is_file():
                return self._send(404, {"error": "not found"})
            self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))

        def do_POST(self):
            # JSON-only + same-origin: a web page elsewhere can't drive this
            origin = self.headers.get("Origin")
            ctype = self.headers.get("Content-Type", "").split(";")[0]
            if origin and origin.split("://")[-1] not in (f"127.0.0.1:{core.cfg['port']}", f"localhost:{core.cfg['port']}"):
                return self._send(403, {"error": "forbidden"})
            if self.path.startswith("/api/clip?") and ctype == "application/octet-stream":   # a video for the overlay hook
                return self._clip()
            if ctype != "application/json":
                return self._send(403, {"error": "forbidden"})
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                self._send(200, self.route(self.path, d) or {"ok": True})
            except Exception as e:
                self._send(400, {"error": str(e)})

        def _clip(self):
            from urllib.parse import parse_qs, urlparse
            from . import hook as hk
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 200 * 1024 * 1024:
                return self._send(413, {"error": "up to 200 MB"})
            try:
                name = hk.save_clip(core.cfg["hook"], parse_qs(urlparse(self.path).query).get("name", [""])[0], self.rfile.read(n))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            r = hk.sync(core.cfg["hook"])
            core.set_cfg("hook.clip", name)
            self._send(200, {"ok": True, "clip": name, "synced": None if r is None else r.returncode == 0,
                             "sync_err": "" if r is None else r.stderr[-300:]})

        def route(self, path, d):
            uid = d.get("user_id")
            if path == "/api/unmute":
                run(core.unmute(uid))
            elif path == "/api/mute":
                run(core.set_mute(uid, True))
                core.add_log("muted", uid, core.name(uid), "by you")
            elif path == "/api/friend":
                if d.get("on"):
                    core.cfg["friends"][uid] = core.name(uid)
                else:
                    core.cfg["friends"].pop(uid, None)
                core.save_cfg()
            elif path == "/api/config":
                return {"value": core.set_cfg(d["path"], d["value"])}
            elif path == "/api/reset":
                core.reset_cfg(d["group"])
            elif path == "/api/hook/test":
                core.hook.hit(d.get("name") or "Test", "test")
            elif path == "/api/restart":
                threading.Timer(0.3, lambda: os._exit(0)).start()  # systemd (Linux) or launchd (macOS) starts us again
            elif path == "/api/privacy" and sys.platform == "darwin":   # macOS: the Privacy & Security page to allow capture
                pane = "Privacy_Microphone" if d.get("pane") == "microphone" else "Privacy_ScreenCapture"
                subprocess.Popen(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])
            elif path == "/api/words":
                core.set_words(d["text"])
            elif path == "/api/setup":
                cid, sec = str(d.get("client_id", "")).strip(), str(d.get("client_secret", "")).strip()
                if not cid.isdigit() or not 15 <= len(cid) <= 22:
                    raise ValueError("The Client ID is a long number (about 19 digits) from your app's OAuth2 page.")
                if len(sec) < 20 or " " in sec:
                    raise ValueError("The secret is the long code you get from Reset Secret on the OAuth2 page (about 32 characters).")
                from .rpc import AuthError
                try:
                    run(core.check_creds(cid, sec))
                except AuthError as e:
                    raise ValueError(str(e)) from None
            elif path == "/api/setup/retry":
                core.setup_err = ""
                loop.call_soon_threadsafe(core.wake.set)
            elif path == "/api/setup/test":
                core.setup_test.update(on=bool(d.get("on")), heard=None)
            elif path == "/api/setup/done":
                core.set_cfg("setup_done", bool(d.get("done", True)))
                core.setup_test["on"] = False
            else:
                raise ValueError("unknown endpoint")

    srv = ThreadingHTTPServer(("127.0.0.1", core.cfg["port"]), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"DogMute page: http://127.0.0.1:{core.cfg['port']}/", flush=True)
    return srv
