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
from .diar import Diarizer
from .review import DEFAULTS as REVIEW_DEFAULTS, Review
from .rulebook import NOISE, Rulebook
from .spam import FPS, FRAME, SpamRules
from .stt import Listener
from .voice import Voices
from .words import masked

WIN = sys.platform == "win32"
if WIN:
    from . import windows
CONF = Path(os.environ.get("DOGMUTE_CONFIG") or (windows.conf_dir() if WIN else Path.home() / ".config/dogmute"))
STATE = Path(os.environ.get("DOGMUTE_STATE") or (windows.state_dir() if WIN else Path.home() / ".local/state/dogmute"))
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
    "slurs": {"enabled": True},  # listen for words at all (the words and how they count: rules.json, see rulebook.py)
    "spam": {"enabled": True},   # watch for noise at all (the noise rules: rules.json)
    "review": REVIEW_DEFAULTS,   # see dogmute/review.py
    "stt": {"model": "large-v3-turbo", "threads": 4, "device": "auto"},
    "hook": {"enabled": False, "url": "", "clip": "", "key": "green", "hit_at": 0.0,   # see dogmute/hook.py
             "clip_dir": "", "clip_prefix": "", "sync_cmd": ""},
}
HANG = 0.4  # a speaker still counts this long after SPEAKING_STOP
LAG_MAX = 1.0  # the audio runs this far behind Discord's speaking events at most (measured on the call, see _onset)
TEST_WORD = "kangaroo"      # the setup wizard's harmless check word
MODELS = ("tiny.en", "base.en", "small.en", "distil-large-v3", "large-v3-turbo")
# every setting the page can change: path -> (type, min, max) or (type, choices)
SCHEMA = {
    "setup_done": (bool,), "audio.target": (str, None), "test_mode": (bool,), "auto_unmute_min": (int, 0, 1440), "notify": (bool,), "spare_bots": (bool,), "port": (int, 1024, 65535),
    "slurs.enabled": (bool,), "spam.enabled": (bool,),
    "review.enabled": (bool,), "review.clips": (bool,), "review.notify": (bool,), "review.keep_days": (int, 1, 3650),
    "review.ask_timeout_s": (int, 5, 300), "review.max_per_min": (int, 1, 30),
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
        saved = json.loads(self.cfg_path.read_text()) if self.cfg_path.exists() else {}
        words = self.words_path.read_text() if self.words_path.exists() else default_words()
        self.book = Rulebook(CONF / "rules.json", saved, words)   # first start: made from the old settings + words.txt
        self.book.listeners.append(self._rules_changed)
        self.cfg = _merge(DEFAULTS, saved)
        self.cfg = {k: ({kk: vv for kk, vv in v.items() if kk in DEFAULTS[k]} if isinstance(DEFAULTS[k], dict)
                        and k not in ("friends",) else v)
                    for k, v in self.cfg.items() if k in DEFAULTS}   # settings from older versions drop out
        self.creds = json.loads(self.creds_path.read_text()) if self.creds_path.exists() else {}
        if self.creds.get("access_token") and not self.cfg["setup_done"]:
            self.cfg["setup_done"] = True                                      # set up before the wizard existed
        self.save_cfg()
        self.words = self.book.words
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
        self.lag = 0.0                # how far the audio runs behind the speaking events (s), measured from clean onsets
        self._lags = collections.deque(maxlen=25)
        self._starts = collections.deque(maxlen=8)    # SPEAKING_START times with nobody else on, waiting for their onset
        self._lv = collections.deque(maxlen=FPS * 4)  # (audio time, level) of the last frames
        self.ours = {}                # uid -> when we muted them (for auto-unmute / "muted by DogMute")
        self.levels = {}              # uid -> last solo level dBFS (for the page)
        self.ring = Ring()
        self.rules = self._spam()
        self.cut = np.zeros(0, np.float32)
        self.loop = None
        self.wake = None
        self.recent_acts = {}         # (uid, kind) -> t, so test mode doesn't spam the log
        self.muting = set()           # uids whose mute is on its way to Discord
        self.muted_ids = set()        # locally muted (by anyone), whether or not they're in the member list
        self.muted_at = {}            # uid -> monotonic time we muted them (0 = already muted when we saw them)
        self.voices = Voices()        # voice profiles, memory only
        self.diar = Diarizer(self)    # online diarization (dogmute/diar.py), memory only
        self.learn_q = queue.Queue(maxsize=20)
        self._solo = None             # (uid, start, last) of the current talk-alone stretch
        self._learned = {}            # uid -> when we last learned their voice
        self._refresh_at = 0.0
        self.hook = hk.Hook(self)
        self.review = Review(self)

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
        if group not in ("spam", "slurs", "stt", "review"):
            raise ValueError("unknown group")
        self.cfg[group].update(copy.deepcopy(DEFAULTS[group]))
        self.save_cfg()

    def _rules_changed(self):
        self.words = self.book.words

    def _spam(self):
        return SpamRules(self.cfg["spam"], lambda: self.book.of(*NOISE))

    # ---------- who is talking ----------
    def _active(self, uid, now, slack=HANG):
        return [(s, e) for s, e in self.talk.get(uid, []) if e is None or now - e <= slack]

    def _counts(self, uid, t=None):
        """Could their voice be in the mix (at time t)? Only people in the channel: someone who leaves mid-sentence never
        sends SPEAKING_STOP, and would stay "talking" for good. Muted people drop out only from when they were muted."""
        if uid == (self.me or {}).get("id") or uid not in self.members:
            return False
        return uid not in self.muted_ids or (t is not None and self.muted_at.get(uid, 0) > t - 0.3)

    def _open_mic(self, uid, t):
        """Talking nonstop since well before t: an open mic, background rather than whoever just said something."""
        lim = self.book.val("who_openmic", "open_mic_s", 20.0)
        return any(s <= t <= (e if e is not None else t) + HANG and t - s >= lim for s, e in self.talk.get(uid, []))

    def talk_started(self, now):
        with self.lock:
            starts = [s for uid in self.talk if self._counts(uid) for s, e in self._active(uid, now)]
        return min(starts) + self.lag if starts else None   # (in audio time)

    def speakers_between(self, a, b, slack):
        with self.lock:
            return [uid for uid, iv in self.talk.items() if self._counts(uid, a)
                    and any(s - slack <= b and (e is None or e + slack >= a) for s, e in iv)]

    def on_now(self, t):
        with self.lock:
            return [uid for uid in self.talk if self._counts(uid) and self._active(uid, t, 0.15)]

    def solo(self, t):
        on = self.on_now(t)
        return on[0] if len(on) == 1 else None

    # ---------- audio ----------
    def on_block(self, x, now):
        self.ring.push(x, now)
        self.cut = np.concatenate([self.cut, np.nan_to_num(x)])
        while len(self.cut) >= FRAME:
            f, self.cut = self.cut[:FRAME], self.cut[FRAME:]
            t = self.ring.t(self.ring.total - len(self.cut))
            te = t - self.lag                                  # the same moment on the speaking events' clock
            on = self.on_now(te)
            uid = on[0] if len(on) == 1 else None
            self._track_solo(uid, t)
            rules = self.rules
            for u, why in rules.frame(on, f, te):
                self.noise(u, why, "noise", t)
            for u, why in self.rules.pop_close():                # nearly a mic-spam rule: ask
                if self.review.on and self._askable(u):
                    self.review.ask_spam(u, why, t)
            if uid:
                self.levels[uid] = round(rules.who[uid].rows[-1][1], 1)
            self._onset(t, rules.mix[-1][0])

    def _onset(self, t, lv):
        """Lag samples: a SPEAKING_START out of silence (nobody else on) against the moment the mix got loud."""
        self._lv.append((t, lv))
        while self._starts and t > self._starts[0] + LAG_MAX + 0.3:
            e = self._starts.popleft()
            fr = [(ft, v) for ft, v in self._lv if e - 1.0 <= ft <= e + LAG_MAX + 0.3]
            quiet = [v for ft, v in fr if ft < e - 0.3]
            if len(fr) < FPS * 2 or len(quiet) < FPS * 0.5 or max(quiet) > -55:
                continue                                       # no silence before it: no clean onset to time
            on = next((ft for ft, v in fr if ft >= e - 0.3 and v > -40), None)
            if on is not None:
                self._lags.append(on - e)
                if len(self._lags) >= 3:
                    self.lag = min(max(float(np.median(self._lags)), -0.3), LAG_MAX)

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
        """Who said the word heard between monotonic audio times a..b: (uid or None, candidates, how)."""
        bk = self.book
        ea, eb = a - self.lag, b - self.lag                    # on the speaking events' clock
        slack = bk.val("who_solo", "slack_s", 0.5)
        who = self.speakers_between(ea, eb, slack) or self.speakers_between(ea, eb, max(slack, 1.5))
        if len(who) > 1 and bk.get("who_tight"):               # the lag is measured: who was on during the word itself
            tight = self.speakers_between(ea, eb, bk.val("who_tight", "tight_s", 0.1))
            if len(tight) == 1:
                return tight[0], who, "the others weren't on during the word"
            who = tight or who
        if len(who) == 1 and bk.get("who_solo"):
            return who[0], who, "only one talking"
        if len(who) <= 1:
            return None, who, "nobody talking" if not who else "one talking (that rule is off)"
        fresh = [u for u in who if not self._open_mic(u, ea)] if bk.get("who_openmic") else who
        if len(fresh) == 1:
            return fresh[0], who, "the others had open mics"
        cand = fresh or who
        if bk.get("who_diar"):                                 # the voices clustered from the live mix, named by who was on
            r = self.diar.who(a, b, cand)
            if r:
                return r[0], who, f"diarization {r[1]:.2f}"
        vr = bk.get("who_voice")
        if vr and self.voices.sess is not None:
            m = (a + b) / 2
            x, _ = self.ring.get(min(a - 0.3, m - 0.6), max(b + 0.3, m + 0.6))
            sc = self.voices.score(x, cand)
            if sc:
                best = max(sc, key=sc.get)
                rest = [v for u, v in sc.items() if u != best]
                second = max(rest) if rest else 0.0
                all_known = len(sc) == len(cand)
                if sc[best] >= vr["min"] and sc[best] - second >= vr["margin"] and (all_known or sc[best] >= 0.6):
                    return best, who, f"voice match {sc[best]:.2f} vs {second:.2f}"
        return None, cand, "several talking"

    def heard(self, entry, a, b, p=None, text="", near=0.0):
        """A list word was heard between monotonic times a..b. p: whisper's probability for it; near: similarity of a near-miss."""
        if entry == TEST_WORD and self.setup_test["on"]:   # the wizard's check: never mutes anyone
            uid, cand, _ = self.pick(a, b)
            who = self.name(uid) if uid else (", ".join(self.name(u) for u in cand) or "someone")
            self.setup_test.update(on=False, heard=who)
            self.add_log("test", uid, who, f'said the test word "{TEST_WORD}" (setup check, nobody muted)')
            return
        what = f'said "{masked(entry)}"'
        uid, cand, how = self.pick(a, b)
        rv, ev = self.review, dict(entry=entry, a=a, b=b, p=p, text=text, how=how)
        if near:                                         # the plain words nearly spelled a list word: only if one person is talking
            if rv.on and uid and len(cand) == 1 and self._askable(uid) and rv.near_ok(entry, uid):
                rv.ask_word(uid, near=near, **ev)
            return
        action = self.book.action(entry)                 # the word rule's own: mute / ask / log
        if action == "log":
            return self.note(uid or None, f"{what} ({how})", ("word", entry), cand)
        if uid:
            doubt = self.book.val("count_doubt", "p", 0.0)
            if (action == "ask" or (p is not None and p < doubt)) and self._askable(uid):
                if rv.on:
                    return rv.ask_word(uid, **ev)        # whisper wasn't sure of this word (or the rule says ask)
                if action == "ask":                      # nobody to ask: only log it (a doubtful word still mutes)
                    return self.note(uid, f"{what} ({how}; would ask, the review queue is off)", ("word", entry), cand)
            rv.note(uid, "word", **ev)
            self.act(uid, what if how == "only one talking" else f"{what} ({how})", "word")
        else:
            hit = [u for u in cand if self._askable(u)]          # in the call, not a friend, not muted already
            un = self.book.get("who_unclear")
            if un and un["action"] == "mute" and action == "mute" and 0 < len(cand) <= un["max"]:
                which = "all of them" if len(hit) == len(cand) else "all but friends and the already muted"
                for u in hit:                                    # a local mute is one click to undo; hearing it again isn't
                    rv.note(u, "word", **ev)
                    self.act(u, f"{what} (couldn't tell who of {len(cand)} talking: muted {which})", "word")
                return
            names = [self.name(u) for u in cand]
            self.add_log("unclear", None, ", ".join(names) or "nobody talking",
                         f"{what}, couldn't tell who ({how})", candidates=cand)
            ask = [u for u in cand if self._askable(u)]
            if rv.on and ask and (un is None or un["action"] != "log"):
                rv.ask_who(ask, **ev)

    def note(self, uid, reason, key, cand=()):
        """A rule that only logs: one line per rule and person every 10 s."""
        now = time.monotonic()
        if now - self.recent_acts.get((uid, key), -99) < 10:
            return
        self.recent_acts[(uid, key)] = now
        name = self.name(uid) if uid else (", ".join(self.name(u) for u in cand) or "nobody talking")
        self.add_log("noticed", uid, name, reason)

    def noise(self, uid, why, kind, t=None):
        """A noise rule fired: do what it says (spam.Why carries the action)."""
        action = getattr(why, "action", "mute")
        if action == "mute":
            self.act(uid, why, kind)
        elif action == "ask":
            if self.review.on and self._askable(uid):
                self.review.ask_spam(uid, why, time.monotonic() if t is None else t)
        else:
            self.note(uid, why, ("noise", getattr(why, "rule", why)))

    def _askable(self, uid):
        """Worth asking about: someone we would really mute."""
        return (self._counts(uid) and uid not in self.cfg["friends"] and uid not in self.muted_ids
                and not (self.cfg["spare_bots"] and self.members.get(uid, {}).get("bot")))

    # ---------- acting ----------
    def name(self, uid):
        return self.members.get(uid, {}).get("name") or self.cfg["friends"].get(uid) or uid

    def act(self, uid, reason, kind):
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._act(uid, reason, kind)))

    async def _act(self, uid, reason, kind):
        """Mute uid now. Every hit mutes: someone unmuted a second ago gets muted again by their next word (each word
        reaches here once, the passes' votes see to that). Only the log lines that mute nobody are kept to one per 10 s."""
        now = time.monotonic()
        if uid in self.muted_ids or uid in self.muting:
            return                                       # already muted, or being muted right now
        spared = uid in self.cfg["friends"] or (self.cfg["spare_bots"] and self.members.get(uid, {}).get("bot"))
        if spared or self.cfg["test_mode"]:
            if now - self.recent_acts.get((uid, kind), -99) < 10:
                return
            self.recent_acts[(uid, kind)] = now
            if uid in self.cfg["friends"]:
                return self.add_log("friend", uid, self.name(uid), reason + " (friend, not muted)")
            return None if spared else self.add_log("would mute", uid, self.name(uid), reason)
        self.muting.add(uid)
        try:
            await self.set_mute(uid, True)
        except Exception as e:
            return self.add_log("failed", uid, self.name(uid), f"{reason} (mute failed: {e})")
        finally:
            self.muting.discard(uid)
        self.ours[uid] = time.time()
        self.add_log("muted", uid, self.name(uid), reason)
        self.review.muted(uid, kind, reason)
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

    async def toggle_deafen(self):
        """Your own mute + deafen in Discord, both on or both off (for a hardware or stream-deck button). Returns the new state."""
        if not self.rpc:
            raise ConnectionError("not connected to Discord")
        on = not (await self.rpc.cmd("GET_VOICE_SETTINGS")).get("deaf")
        await self.rpc.cmd("SET_VOICE_SETTINGS", {"deaf": on, "mute": on})
        self.add_log("deafened" if on else "undeafened", None, "you", "by the button")
        return on

    async def set_self_mute(self, on):
        """Your own Discord mic muted or not (for another app that needs your mic quiet). Returns what it was."""
        if not self.rpc:
            raise ConnectionError("not connected to Discord")
        was = bool((await self.rpc.cmd("GET_VOICE_SETTINGS")).get("mute"))
        if was != on:
            await self.rpc.cmd("SET_VOICE_SETTINGS", {"mute": on})
        return was

    async def unmute(self, uid, why="you"):
        await self.set_mute(uid, False)
        self.ours.pop(uid, None)
        self.add_log("unmuted", uid, self.name(uid), f"by {why}")
        if why == "you":
            self.review.unmuted(uid)

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
            if WIN:
                return windows.toast(title, reason)
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
            if evt == "SPEAKING_START" and self._counts(uid) and not self.on_now(now):
                self._starts.append(now)                         # out of silence: its onset times the audio's lag
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
                    self.noise(uid, why, "flap", now)
        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
            self._member(d)
        elif evt == "VOICE_STATE_DELETE":
            uid = (d.get("user") or {}).get("id")
            with self.lock:
                self.members.pop(uid, None)
                self._hush(uid, now)
        elif evt == "VOICE_CHANNEL_SELECT":
            asyncio.ensure_future(self.join(d.get("channel_id")))

    async def _refresh(self):
        try:
            ch = await self.rpc.cmd("GET_CHANNEL", {"channel_id": self.channel["id"]})
            here = set()
            for vs in ch.get("voice_states") or []:
                self._member(vs)
                here.add((vs.get("user") or {}).get("id"))
            with self.lock:
                for uid in [u for u in self.members if u not in here]:   # left without a VOICE_STATE_DELETE
                    self.members.pop(uid)
                    self._hush(uid, time.monotonic())
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
            st = vs.get("voice_state") or {}
            if any(st.get(k) for k in ("mute", "self_mute", "deaf", "self_deaf", "suppress")):
                self._hush(u["id"], time.monotonic())             # muted themselves (or the server did): not talking

    def _hush(self, uid, now):
        """They can't be talking (left, or muted): close what is open, as SPEAKING_STOP would have."""
        iv = self.talk.get(uid)
        if iv and iv[-1][1] is None:
            iv[-1] = (iv[-1][0], now)
            self.rules.speaking(uid, False, now)
        if uid in self.members:
            self.members[uid]["talking"] = False

    CH_EVENTS = ("SPEAKING_START", "SPEAKING_STOP", "VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE")

    async def join(self, cid):
        if self.channel:
            for ev in self.CH_EVENTS:
                await self.rpc.unsubscribe(ev, {"channel_id": self.channel["id"]})
        with self.lock:
            self.channel, self.members, self.talk, self.levels = None, {}, {}, {}
            self.muted_ids.clear()
            self.rules = self._spam()                    # per-person noise state is for this call only
        self.voices.forget(set())                        # voice profiles are for this call only
        self.diar.forget()
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
                    self.channel, self.members, self.talk, self.levels = None, {}, {}, {}
                    self.rules = self._spam()
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
                "lag_ms": round(self.lag * 1000), "lag_n": len(self._lags),
                "voices": self.voices.status, "voices_known": sum(self.voices.known(u) for u in self.members),
                "friends": self.cfg["friends"], "has_creds": bool(self.creds.get("client_id")), "setup": self.setup_view(),
                "hook": self.hook.status if self.cfg["hook"]["enabled"] and self.cfg["hook"]["url"] else "off",
                "review": self.review.view(), "review_s": self.cfg["review"]["ask_timeout_s"],
                "platform": "mac" if sys.platform == "darwin" else "windows" if WIN else "linux"}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.capture = Capture(self.on_block, lambda: self.cfg["audio"]["target"])
        self.listener = Listener(self)
        self.capture.start()
        self.listener.start()
        threading.Thread(target=self._learner, daemon=True).start()
        threading.Thread(target=self.diar.run, daemon=True).start()
        self.review.start()
        from .web import serve
        serve(self)
        self.hook.preload()
        await self.discord_loop()
