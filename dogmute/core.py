"""Glue: Discord RPC state, who is talking when, audio, rules -> local mutes, log, auto-unmute."""
import asyncio
import base64
import collections
import copy
import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import hook as hk
from . import rpc as R
from .audio import Capture, Ring
from .spam import DEFAULTS as SPAM_DEFAULTS, FRAME, SpamRules
from .stt import Listener
from .voice import Voices
from .words import WordList, masked

CONF = Path(os.environ.get("DOGMUTE_CONFIG", Path.home() / ".config/dogmute"))
STATE = Path(os.environ.get("DOGMUTE_STATE", Path.home() / ".local/state/dogmute"))
HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "port": 8853,
    "setup_done": False,        # the first-run wizard on the page is finished (or skipped)
    "audio": {"target": ""},    # which output stream to listen to; "" = Discord's voice output, found automatically
    "test_mode": False,         # True: only log "would mute"
    "auto_unmute_min": 0,       # 0 = stay muted
    "notify": True,             # desktop notification on each mute
    "spare_bots": True,         # never auto-mute bots (music bots are on purpose)
    "friends": {},              # user_id -> name; never auto-muted
    "slurs": {"enabled": True, "slack_s": 0.5, "voice": True, "open_mic_s": 20.0},
    "spam": SPAM_DEFAULTS,
    "stt": {"model": "distil-large-v3", "threads": 4, "device": "auto"},
    "hook": {"enabled": False, "url": "", "clip": "", "key": "green", "hit_at": 0.0,   # see dogmute/hook.py
             "clip_dir": "", "clip_prefix": "", "sync_cmd": ""},
}
HANG = 0.4  # a speaker still counts this long after SPEAKING_STOP
TEST_WORD = "kangaroo"      # the setup wizard's harmless check word
MODELS = ("tiny.en", "base.en", "small.en", "distil-large-v3")
# every setting the page can change: path -> (type, min, max) or (type, choices)
SCHEMA = {
    "setup_done": (bool,), "audio.target": (str, None), "test_mode": (bool,), "auto_unmute_min": (int, 0, 1440), "notify": (bool,), "spare_bots": (bool,), "port": (int, 1024, 65535),
    "slurs.voice": (bool,), "slurs.open_mic_s": (float, 5.0, 300.0),
    "slurs.enabled": (bool,), "slurs.slack_s": (float, 0.0, 3.0),
    "spam.enabled": (bool,), "spam.earrape_db": (float, -40.0, 0.0), "spam.earrape_ms": (int, 50, 5000),
    "spam.clip_frac": (float, 0.0, 1.0), "spam.jump_db": (float, 3.0, 60.0),
    "spam.continuous_s": (float, 3.0, 600.0), "spam.continuous_db": (float, -60.0, 0.0),
    "spam.continuous_dip_frac": (float, 0.0, 1.0), "spam.flaps_per_10s": (int, 3, 100),
    "stt.model": (str, MODELS), "stt.threads": (int, 1, os.cpu_count() or 4), "stt.device": (str, ("auto", "gpu", "cpu")),
    "hook.enabled": (bool,), "hook.key": (str, ("green", "screen", "full")), "hook.hit_at": (float, 0.0, 8.0),
    "hook.url": (str, None), "hook.clip": (str, None), "hook.clip_dir": (str, None), "hook.clip_prefix": (str, None),
    "hook.sync_cmd": (str, None),
}


def default_words():
    """The shipped word list. It is base64 in the repo so the source doesn't display slurs; ~/.config/dogmute/words.txt
    (written from it on first run) is the plain list you edit."""
    return base64.b64decode((HERE / "data" / "words.default.b64").read_text()).decode()


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "friends" else v
    return out


def _write(path, text, mode=0o644):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    tmp.replace(path)


class Core:
    def __init__(self):
        CONF.mkdir(parents=True, exist_ok=True)
        STATE.mkdir(parents=True, exist_ok=True)
        self.cfg_path, self.creds_path = CONF / "config.json", CONF / "discord.json"
        self.words_path, self.log_path = CONF / "words.txt", STATE / "mutes.jsonl"
        self.cfg = _merge(DEFAULTS, json.loads(self.cfg_path.read_text()) if self.cfg_path.exists() else {})
        self.cfg = {k: v for k, v in self.cfg.items() if k in DEFAULTS}   # settings from older versions drop out
        self.creds = json.loads(self.creds_path.read_text()) if self.creds_path.exists() else {}
        if self.creds.get("access_token") and not self.cfg["setup_done"]:
            self.cfg["setup_done"] = True                                      # set up before the wizard existed
        self.save_cfg()
        if not self.words_path.exists():
            self.words_path.write_text(default_words())
        self.words = WordList(self.words_path.read_text())
        self.log = collections.deque(maxlen=200)
        if self.log_path.exists():
            for line in self.log_path.read_text().splitlines()[-200:]:
                try:
                    self.log.append(json.loads(line))
                except ValueError:
                    pass
        self.lock = threading.RLock()
        self.rpc = None
        self.me = None
        self.state = "starting"       # setup | waiting for Discord | authorize in Discord | connected | error: ...
        self.setup_err = ""           # the last setup problem, in plain words with what to do
        self.setup_test = {"on": False, "heard": None}   # the wizard's "say the test word" check
        self.channel = None           # {"id", "name", "guild_id"}
        self.members = {}             # uid -> {"name", "mute", "bot", "talking"}
        self.talk = {}                # uid -> [(start, stop|None)] recent
        self.ours = {}                # uid -> when we muted them (for auto-unmute / "muted by DogMute")
        self.levels = {}              # uid -> last solo level dBFS (for the page)
        self.ring = Ring()
        self.rules = SpamRules(self.cfg["spam"])
        self.cut = np.zeros(0, np.float32)
        self.loop = None
        self.wake = None
        self.recent_acts = {}         # (uid, kind) -> t, so test mode doesn't spam the log
        self.muted_ids = set()        # locally muted (by anyone), whether or not they're in the member list
        self.muted_at = {}            # uid -> monotonic time we muted them (0 = already muted when we saw them)
        self.voices = Voices()        # voice profiles, memory only
        self.learn_q = queue.Queue(maxsize=20)
        self._solo = None             # (uid, start, last) of the current talk-alone stretch
        self._learned = {}            # uid -> when we last learned their voice
        self._refresh_at = 0.0
        self.hook = hk.Hook(self)

    # ---------- config ----------
    def save_cfg(self):
        _write(self.cfg_path, json.dumps(self.cfg, indent=2) + "\n")

    def save_creds(self):
        _write(self.creds_path, json.dumps(self.creds, indent=2) + "\n", 0o600)

    def set_cfg(self, path, value):
        if path not in SCHEMA:
            raise ValueError(f"unknown setting {path}")
        kind, *lim = SCHEMA[path]
        if kind is bool:
            v = bool(value)
        elif kind is str and lim[0] is None:
            v = str(value).strip()
            if path == "hook.clip" and v and v not in hk.clips(self.cfg["hook"]):
                raise ValueError("no such clip")
            if path == "hook.url" and v and not v.startswith(("http://", "https://")):
                raise ValueError("starts with http:// or https://")
        elif kind is str:
            if value not in lim[0]:
                raise ValueError(f"pick one of {', '.join(lim[0])}")
            v = value
        else:
            v = kind(float(value))
            if not lim[0] <= v <= lim[1]:
                raise ValueError(f"use {lim[0]:g} to {lim[1]:g}")
        *parents, key = path.split(".")
        d = self.cfg
        for k in parents:
            d = d[k]
        d[key] = v  # in place: the rules and listener read these dicts live
        self.save_cfg()
        if path in ("hook.clip", "hook.enabled", "hook.url"):
            self.hook.preload()
        return v

    def reset_cfg(self, group):
        if group not in ("spam", "slurs", "stt"):
            raise ValueError("unknown group")
        self.cfg[group].update(copy.deepcopy(DEFAULTS[group]))
        self.save_cfg()

    def set_words(self, text):
        wl = WordList(text)  # raises on a bad /regex/
        _write(self.words_path, text)
        self.words = wl

    # ---------- who is talking ----------
    def _active(self, uid, now, slack=HANG):
        return [(s, e) for s, e in self.talk.get(uid, []) if e is None or now - e <= slack]

    def _counts(self, uid, t=None):
        """Could their voice be in the mix (at time t)? Muted people drop out only from when they were muted."""
        if uid == (self.me or {}).get("id"):
            return False
        return uid not in self.muted_ids or (t is not None and self.muted_at.get(uid, 0) > t - 0.3)

    def _open_mic(self, uid, t):
        """Talking nonstop since well before t: an open mic, background rather than whoever just said something."""
        lim = self.cfg["slurs"]["open_mic_s"]
        return any(s <= t <= (e if e is not None else t) + HANG and t - s >= lim for s, e in self.talk.get(uid, []))

    def talk_started(self, now):
        with self.lock:
            starts = [s for uid in self.talk if self._counts(uid) for s, e in self._active(uid, now)]
        return min(starts) if starts else None

    def speakers_between(self, a, b, slack):
        with self.lock:
            return [uid for uid, iv in self.talk.items() if self._counts(uid, a)
                    and any(s - slack <= b and (e is None or e + slack >= a) for s, e in iv)]

    def solo(self, t):
        with self.lock:
            on = [uid for uid in self.talk if self._counts(uid) and self._active(uid, t, 0.15)]
        return on[0] if len(on) == 1 else None

    # ---------- audio ----------
    def on_block(self, x, now):
        self.ring.push(x, now)
        self.cut = np.concatenate([self.cut, x])
        while len(self.cut) >= FRAME:
            f, self.cut = self.cut[:FRAME], self.cut[FRAME:]
            t = self.ring.t(self.ring.total - len(self.cut))
            uid = self.solo(t)
            self._track_solo(uid, t)
            if uid:
                why = self.rules.frame(uid, f, t)
                self.levels[uid] = round(self.rules.who[uid].levels[-1], 1)
                if why:
                    self.act(uid, why, "noise")

    def _track_solo(self, uid, t):
        """Stretches where one person talks alone (1-4 s) go to the voice learner."""
        s = self._solo
        if s and (uid != s[0] or t - s[2] > 0.2 or t - s[1] >= 4.0):
            if t - s[1] >= 1.0 and time.monotonic() - self._learned.get(s[0], -99) > 3.0:
                self._learned[s[0]] = time.monotonic()
                try:
                    self.learn_q.put_nowait((s[0], s[1], s[2]))
                except queue.Full:
                    pass
            s = None
        self._solo = (uid, s[1] if s else t, t) if uid else None

    def _learner(self):
        self.voices.load()
        while True:
            uid, t0, t1 = self.learn_q.get()
            x, _ = self.ring.get(t0, t1)
            if len(x) >= 16000 and float(np.sqrt(np.mean(x * x))) > 0.005:   # actual voice in it, not near-silence
                try:
                    self.voices.learn(uid, x)
                except Exception as e:  # noqa: BLE001
                    print("voice learn:", e, flush=True)

    def pick(self, a, b):
        """Who said the word between monotonic times a..b: (uid or None, candidates, how)."""
        sl = self.cfg["slurs"]
        who = self.speakers_between(a, b, sl["slack_s"]) or self.speakers_between(a, b, 1.5)
        if len(who) <= 1:
            return (who[0] if who else None), who, "only one talking"
        fresh = [u for u in who if not self._open_mic(u, a)]
        if len(fresh) == 1:
            return fresh[0], who, "the others had open mics"
        cand = fresh or who
        if sl["voice"] and self.voices.sess is not None:
            m = (a + b) / 2
            x, _ = self.ring.get(min(a - 0.3, m - 0.6), max(b + 0.3, m + 0.6))
            sc = self.voices.score(x, cand)
            if sc:
                best = max(sc, key=sc.get)
                rest = [v for u, v in sc.items() if u != best]
                second = max(rest) if rest else 0.0
                all_known = len(sc) == len(cand)
                if sc[best] >= 0.45 and sc[best] - second >= 0.12 and (all_known or sc[best] >= 0.6):
                    return best, who, f"voice match {sc[best]:.2f} vs {second:.2f}"
        return None, cand, "several talking"

    def heard(self, entry, a, b):
        if entry == TEST_WORD and self.setup_test["on"]:   # the wizard's check: never mutes anyone
            uid, cand, _ = self.pick(a, b)
            who = self.name(uid) if uid else (", ".join(self.name(u) for u in cand) or "someone")
            self.setup_test.update(on=False, heard=who)
            self.add_log("test", uid, who, f'said the test word "{TEST_WORD}" (setup check, nobody muted)')
            return
        what = f'said "{masked(entry)}"'
        uid, cand, how = self.pick(a, b)
        if uid:
            self.act(uid, what if how == "only one talking" else f"{what} ({how})", "word")
        else:
            names = [self.name(u) for u in cand]
            self.add_log("unclear", None, ", ".join(names) or "nobody talking",
                         f"{what}, couldn't tell who ({how})", candidates=cand)

    # ---------- acting ----------
    def name(self, uid):
        return self.members.get(uid, {}).get("name") or self.cfg["friends"].get(uid) or uid

    def act(self, uid, reason, kind):
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._act(uid, reason, kind)))

    async def _act(self, uid, reason, kind):
        now = time.monotonic()
        if now - self.recent_acts.get((uid, kind), -99) < 10:
            return
        self.recent_acts[(uid, kind)] = now
        if uid in self.cfg["friends"]:
            return self.add_log("friend", uid, self.name(uid), reason + " (friend, not muted)")
        what = reason.split(" (")[0]
        if uid in self.muted_ids or now - self.recent_acts.get(("any", what), -99) < 4:
            return                                       # already muted / this very word already got someone
        if self.cfg["spare_bots"] and self.members.get(uid, {}).get("bot"):
            return
        if self.cfg["test_mode"]:
            return self.add_log("would mute", uid, self.name(uid), reason)
        try:
            await self.set_mute(uid, True)
        except Exception as e:
            return self.add_log("failed", uid, self.name(uid), f"{reason} (mute failed: {e})")
        self.ours[uid] = time.time()
        self.recent_acts[("any", what)] = now
        self.add_log("muted", uid, self.name(uid), reason)
        mins = self.cfg["auto_unmute_min"]
        if mins:
            self.loop.call_later(mins * 60, lambda: asyncio.ensure_future(self._auto_unmute(uid, self.ours[uid])))

    async def _auto_unmute(self, uid, stamp):
        if self.ours.get(uid) == stamp:
            await self.unmute(uid, "auto-unmute")

    async def set_mute(self, uid, on):
        await self.rpc.cmd("SET_USER_VOICE_SETTINGS", {"user_id": uid, "mute": on})
        with self.lock:
            (self.muted_ids.add if on else self.muted_ids.discard)(uid)
            self.muted_at[uid] = time.monotonic() if on else 0
            if uid in self.members:
                self.members[uid]["mute"] = on

    async def unmute(self, uid, why="you"):
        await self.set_mute(uid, False)
        self.ours.pop(uid, None)
        self.add_log("unmuted", uid, self.name(uid), f"by {why}")

    def add_log(self, action, uid, name, reason, **extra):
        e = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "action": action,
             "user_id": uid, "name": name, "reason": reason, **extra}
        self.log.append(e)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(e) + "\n")
        print(f"{e['t']} {action} {name}: {reason}", flush=True)
        if action == "muted":
            self.hook.hit(name, reason)
        if self.cfg["notify"] and action in ("muted", "would mute"):
            title = f"DogMute {'muted' if action == 'muted' else 'would mute'} {name}"
            cmd = (["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run", title, reason] if sys.platform == "darwin"
                   else ["notify-send", "-a", "DogMute", "-i", "audio-volume-muted", title, reason])
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    # ---------- Discord ----------
    def on_event(self, evt, d):
        now = time.monotonic()
        if evt in ("SPEAKING_START", "SPEAKING_STOP"):
            uid = d.get("user_id")
            if not uid:
                return
            with self.lock:
                iv = self.talk.setdefault(uid, [])
                if evt == "SPEAKING_START":
                    if not iv or iv[-1][1] is not None:
                        iv.append((now, None))
                else:
                    if iv and iv[-1][1] is None:
                        iv[-1] = (iv[-1][0], now)
                self.talk[uid] = [p for p in iv if p[1] is None or now - p[1] < 30]
                if uid in self.members:
                    self.members[uid]["talking"] = evt == "SPEAKING_START"
            self.rules.speaking(uid, evt == "SPEAKING_START", now)
            if uid not in self.members and self.channel and now - self._refresh_at > 3:
                self._refresh_at = now                   # someone we don't know: ask Discord who's here
                asyncio.ensure_future(self._refresh())
            if evt == "SPEAKING_START" and self._counts(uid):
                why = self.rules.flaps(uid, now)
                if why:
                    self.act(uid, why, "flap")
        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
            self._member(d)
        elif evt == "VOICE_STATE_DELETE":
            with self.lock:
                self.members.pop((d.get("user") or {}).get("id"), None)
        elif evt == "VOICE_CHANNEL_SELECT":
            asyncio.ensure_future(self.join(d.get("channel_id")))

    async def _refresh(self):
        try:
            ch = await self.rpc.cmd("GET_CHANNEL", {"channel_id": self.channel["id"]})
            for vs in ch.get("voice_states") or []:
                self._member(vs)
        except Exception as e:  # noqa: BLE001
            print("refresh:", e, flush=True)

    def _member(self, vs):
        u = vs.get("user") or {}
        if not u.get("id"):
            return
        with self.lock:
            (self.muted_ids.add if vs.get("mute") else self.muted_ids.discard)(u["id"])
            old = self.members.get(u["id"], {})
            self.members[u["id"]] = {"name": vs.get("nick") or u.get("global_name") or u.get("username"),
                                     "mute": bool(vs.get("mute")), "bot": bool(u.get("bot")),
                                     "talking": old.get("talking", False)}

    CH_EVENTS = ("SPEAKING_START", "SPEAKING_STOP", "VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE")

    async def join(self, cid):
        if self.channel:
            for ev in self.CH_EVENTS:
                await self.rpc.unsubscribe(ev, {"channel_id": self.channel["id"]})
        with self.lock:
            self.channel, self.members, self.talk = None, {}, {}
            self.muted_ids.clear()
        self.voices.forget(set())                        # voice profiles are for this call only
        if not cid:
            return
        ch = await self.rpc.cmd("GET_CHANNEL", {"channel_id": cid})
        await self._enter(ch)

    async def _enter(self, ch):
        for ev in self.CH_EVENTS:
            await self.rpc.subscribe(ev, {"channel_id": ch["id"]})
        with self.lock:
            self.channel = {"id": ch["id"], "name": ch.get("name"), "guild_id": ch.get("guild_id")}
        for vs in ch.get("voice_states") or []:
            self._member(vs)

    async def _auth(self):
        c = self.creds
        loop = asyncio.get_running_loop()
        if c.get("access_token"):
            if c.get("expires_at", 0) < time.time() and c.get("refresh_token"):
                try:
                    c.update(await loop.run_in_executor(None, R.refresh, c))
                    self.save_creds()
                except Exception as e:
                    print("refresh failed:", e, flush=True)
            try:
                return await self.rpc.cmd("AUTHENTICATE", {"access_token": c["access_token"]})
            except R.RPCError as e:
                print("authenticate failed:", e, flush=True)
        self.state = "authorize in Discord"
        try:
            code = (await self.rpc.cmd("AUTHORIZE", {"client_id": c["client_id"], "scopes": R.SCOPES},
                                       timeout=300))["code"]
        except asyncio.TimeoutError:
            raise R.AuthError("Nobody answered the Authorize popup in Discord. Press Try again; the popup opens inside "
                              "the Discord app, so bring its window to the front.") from None
        except R.RPCError as e:
            if "cancel" in str(e).lower() or "denied" in str(e).lower() or e.code == 5000:
                raise R.AuthError("The Authorize popup was cancelled. Press Try again and click Authorize.") from None
            raise R.AuthError(f"Discord refused: {e}. Press Try again.") from None
        c.update(await loop.run_in_executor(None, R.exchange_code, c, code))
        self.save_creds()
        try:
            return await self.rpc.cmd("AUTHENTICATE", {"access_token": c["access_token"]})
        except R.RPCError as e:
            raise R.AuthError(f"Discord didn't accept the login ({e}). Press Try again.") from None

    async def discord_loop(self):
        while True:
            if not (self.creds.get("client_id") and self.creds.get("client_secret")):
                self.state = "setup"
                await self.wake.wait()
                self.wake.clear()
                continue
            path = R.find_socket()
            if not path:
                self.state = "waiting for Discord"
                try:
                    await asyncio.wait_for(self.wake.wait(), 5)
                    self.wake.clear()
                except asyncio.TimeoutError:
                    pass
                continue
            self.rpc = R.RPC(self.creds["client_id"], self.on_event)
            needs_you = False
            try:
                try:
                    await self.rpc.connect(path)
                except R.RPCError as e:
                    if e.code == 4000:
                        raise R.AuthError("Discord says that Client ID doesn't exist. Copy it again from your app's "
                                          "OAuth2 page and save it in step 2.") from None
                    raise
                auth = await self._auth()
                self.setup_err = ""
                self.me = auth.get("user") or self.rpc.user
                await self.rpc.subscribe("VOICE_CHANNEL_SELECT")
                ch = await self.rpc.cmd("GET_SELECTED_VOICE_CHANNEL")
                if ch:
                    await self._enter(ch)
                self.state = "connected"
                await self.rpc.closed.wait()
                self.state = "Discord closed"
            except R.AuthError as e:                      # needs the person: wait for Try again, never re-pop the popup by itself
                self.state, self.setup_err, needs_you = "error: setup", str(e), True
                print("setup:", e, flush=True)
            except R.RPCError as e:
                self.state = f"error: {e}"
                print("rpc:", e, flush=True)
            except Exception as e:
                self.state = f"error: {type(e).__name__}: {e}"
                print("rpc:", self.state, flush=True)
            finally:
                self.rpc.close()
                with self.lock:
                    self.channel, self.members, self.talk = None, {}, {}
            try:
                await asyncio.wait_for(self.wake.wait(), None if needs_you else 10)
                self.wake.clear()
            except asyncio.TimeoutError:
                pass

    async def check_creds(self, cid, secret):
        """Step 2 of the setup: is this a real app + secret? Saves them and connects when it is."""
        c = {"client_id": cid, "client_secret": secret}
        await asyncio.get_running_loop().run_in_executor(None, R.check_app, c)
        self.creds = c
        self.save_creds()
        self.setup_err = ""
        self.wake.set()

    def setup_view(self):
        st = self.state
        return {"done": self.cfg["setup_done"], "has_creds": bool(self.creds.get("client_id")),
                "authorized": bool(self.creds.get("access_token")), "connected": st == "connected",
                "discord_open": R.find_socket() is not None, "authorizing": st == "authorize in Discord",
                "err": self.setup_err, "audio_ok": time.monotonic() - self.ring.last_push < 2.0,
                "test": dict(self.setup_test)}

    def snapshot(self):
        with self.lock:
            people = [{"id": uid, **m, "mute": uid in self.muted_ids, "friend": uid in self.cfg["friends"],
                       "ours": uid in self.ours, "level": self.levels.get(uid), "voice": self.voices.known(uid)}
                      for uid, m in self.members.items() if uid != (self.me or {}).get("id")]
        return {"state": self.state, "me": (self.me or {}).get("username"), "channel": self.channel,
                "people": people, "log": list(self.log)[-60:][::-1],
                "test_mode": self.cfg["test_mode"], "auto_unmute_min": self.cfg["auto_unmute_min"],
                "audio": self.capture.status, "stt": self.listener.status, "stt_ms": self.listener.latency_ms,
                "voices": self.voices.status, "voices_known": sum(self.voices.known(u) for u in self.members),
                "friends": self.cfg["friends"], "has_creds": bool(self.creds.get("client_id")), "setup": self.setup_view(),
                "hook": self.hook.status if self.cfg["hook"]["enabled"] and self.cfg["hook"]["url"] else "off",
                "platform": "mac" if sys.platform == "darwin" else "linux"}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.capture = Capture(self.on_block, lambda: self.cfg["audio"]["target"])
        self.listener = Listener(self)
        self.capture.start()
        self.listener.start()
        threading.Thread(target=self._learner, daemon=True).start()
        from .web import serve
        serve(self)
        self.hook.preload()
        await self.discord_loop()
