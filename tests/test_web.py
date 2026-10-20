"""The page's API with no Discord, no audio and no models: settings round-trip, validation, CSRF guard."""
import asyncio
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request

tmp = tempfile.mkdtemp()
os.environ["DOGMUTE_CONFIG"], os.environ["DOGMUTE_STATE"] = tmp + "/c", tmp + "/s"
from dogmute.core import Core  # noqa: E402
from dogmute.web import serve  # noqa: E402


class _Stub:
    status, latency_ms = "off", None


def _start():
    core = Core()
    core.cfg["port"] = 18853
    core.capture = core.listener = _Stub()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    core.loop, core.wake = loop, asyncio.Event()
    serve(core)
    return core


CORE = _start()
URL = "http://127.0.0.1:18853"


def post(path, body, ctype="application/json", origin=None):
    h = {"Content-Type": ctype}
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_page_and_state():
    assert b"DogMute" in urllib.request.urlopen(URL + "/").read()
    s = json.load(urllib.request.urlopen(URL + "/api/state"))
    assert s["state"] == "starting" and s["test_mode"] is False


def test_settings_roundtrip_and_validation():
    assert post("/api/config", {"path": "spam.earrape_db", "value": -5})[1]["value"] == -5.0
    assert CORE.rules.cfg["earrape_db"] == -5.0                     # live, no restart
    assert post("/api/config", {"path": "spam.earrape_db", "value": 5})[0] == 400
    assert post("/api/config", {"path": "hook.url", "value": "ftp://x"})[0] == 400
    assert post("/api/config", {"path": "nope", "value": 1})[0] == 400
    c = json.load(urllib.request.urlopen(URL + "/api/config"))
    assert c["cfg"]["spam"]["earrape_db"] == -5.0 and "distil-large-v3" in c["models"]


def test_other_sites_cant_drive_it():
    assert post("/api/config", {"path": "test_mode", "value": True}, ctype="text/plain")[0] == 403
    assert post("/api/config", {"path": "test_mode", "value": True}, origin="https://evil.example")[0] == 403
    assert CORE.cfg["test_mode"] is False


def test_setup_checks_the_credentials_shape():
    assert post("/api/setup", {"client_id": "abc", "client_secret": "x"})[0] == 400
