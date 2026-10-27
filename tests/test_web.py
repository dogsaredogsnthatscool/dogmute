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
    assert post("/api/config", {"path": "spam.enabled", "value": False})[1]["value"] is False
    assert CORE.rules.cfg["enabled"] is False                       # live, no restart
    post("/api/config", {"path": "spam.enabled", "value": True})
    assert post("/api/config", {"path": "hook.url", "value": "ftp://x"})[0] == 400
    assert post("/api/config", {"path": "nope", "value": 1})[0] == 400
    c = json.load(urllib.request.urlopen(URL + "/api/config"))
    assert c["cfg"]["spam"] == {"enabled": True} and "distil-large-v3" in c["models"]


def test_rules_list_change_delete_add_and_restore():
    v = json.load(urllib.request.urlopen(URL + "/api/rules"))
    assert {"words", "noise", "who", "count"} <= set(v["sections"]) and any(r["type"] == "word" for r in v["rules"])
    ear = next(r for r in v["rules"] if r["id"] == "earrape")
    s, v = post("/api/rules", {"op": "update", "id": "earrape", "db": -5})
    assert s == 200 and CORE.book.get("loud")["db"] == -5.0 and next(r for r in CORE.rules.rules() if r["id"] == "earrape")["db"] == -5.0
    assert post("/api/rules", {"op": "update", "id": "earrape", "db": 5})[0] == 400        # out of range: kept
    assert CORE.book.get("loud")["db"] == -5.0
    s, v = post("/api/rules", {"op": "add", "type": "loud", "name": "Long and loud", "db": -15, "ms": 6000})
    assert s == 200 and [r["name"] for r in CORE.book.of("loud")][:2] == ["Earrape", "Long and loud"]
    s, v = post("/api/rules", {"op": "add", "type": "word", "text": "koala\nwombat", "fuzzy": False, "action": "ask"})
    assert CORE.words.find([("wombat", 0, 1)]) and CORE.book.action("koala") == "ask"
    assert post("/api/rules", {"op": "add", "type": "word", "text": "/(/"})[0] == 400       # broken regex
    assert post("/api/rules", {"op": "delete", "id": "who_voice"})[0] == 200 and CORE.book.get("who_voice") is None
    assert "who_voice" in json.load(urllib.request.urlopen(URL + "/api/rules"))["missing"]
    assert post("/api/rules", {"op": "add", "type": "who_voice"})[0] == 200 and CORE.book.get("who_voice")
    assert post("/api/rules", {"op": "add", "type": "who_voice"})[0] == 400                 # one of those only
    assert post("/api/rules", {"op": "reset", "section": "noise"})[0] == 200
    assert CORE.book.get("loud")["db"] == ear["db"]


def test_other_sites_cant_drive_it():
    assert post("/api/config", {"path": "test_mode", "value": True}, ctype="text/plain")[0] == 403
    assert post("/api/config", {"path": "test_mode", "value": True}, origin="https://evil.example")[0] == 403
    assert CORE.cfg["test_mode"] is False


def test_setup_checks_the_credentials_shape():
    assert post("/api/setup", {"client_id": "abc", "client_secret": "x"})[0] == 400


def test_review_queue_on_the_page():
    rv, acts = CORE.review, []
    rv.notifier = type("N", (), {"ask": lambda *a: None, "cancel": lambda *a: None})()
    rv.later = lambda s, f: None
    CORE.act = lambda uid, why, kind: acts.append(uid)
    CORE.members = {"A": {"name": "alice", "mute": False}}
    rv.ask_word("A", "kangaroo", 1.0, 1.5, p=0.2, text="kangaroo")
    s = json.load(urllib.request.urlopen(URL + "/api/state"))["review"]
    assert s["count"] == 1 and s["items"][0]["question"] == 'Did alice say "k*******"?' and "transcript" not in s["items"][0]
    id = s["items"][0]["id"]
    assert post("/api/review/label", {"id": id, "label": "nope"})[0] == 400
    assert post("/api/review/label", {"id": "../x", "label": "wrong"})[0] == 400
    assert post("/api/review/label", {"id": id, "label": "correct"})[1] == {"new": True} and acts == ["A"]
    assert json.load(urllib.request.urlopen(URL + "/api/state"))["review"]["count"] == 0
    try:
        urllib.request.urlopen(URL + "/api/review/clip?id=../../etc/passwd")
    except urllib.error.HTTPError as e:
        assert e.code == 404
