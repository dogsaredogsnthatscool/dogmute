"""Review queue: when the model is unsure, ask on the desktop (and on the page) instead of muting, and keep every case
(text, a short clip, your answer) as a labelled set for training and evals. Confident mutes get an item too, no question."""
import collections
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import wave

import numpy as np

from .audio import RATE
from .words import masked

DEFAULTS = {
    "enabled": True,
    "clips": True,          # save the audio around each case (False: text only)
    "keep_days": 30,        # older cases and clips are deleted on start and daily
    "notify": True,         # ask in a desktop notification (Linux: buttons; Mac: look at the page)
    "ask_timeout_s": 30,    # a "yes" mutes only this long after the question; later it only labels
    "max_per_min": 3,       # at most this many questions popped per minute; the rest wait on the page
}
BEFORE, AFTER = 1.5, 1.5    # clip: this much audio around the event
NEAR = 0.75                 # a near-miss this similar to a list word is worth asking about
NEAR_GAP = 20.0             # ...but at most one per this many seconds (they can be frequent)
SPAM_GAP = 60.0             # at most one mic-spam question per person per this many seconds
UNMUTE_WINDOW = 600.0       # unmuting someone within this long of a mute says the mute was wrong
ID_RX = re.compile(r"^[0-9a-f]{12}$")


class Notifier:
    """Desktop questions with a button per choice. Never blocks the caller: the wait is in a thread."""

    def __init__(self):
        self.procs = {}
        self._actions = None

    def actions(self):
        if self._actions is None:
            try:
                r = subprocess.run(["notify-send", "--help"], capture_output=True, text=True, timeout=5)
                self._actions = "--action" in r.stdout
            except (OSError, subprocess.SubprocessError):
                self._actions = False
        return self._actions

    def ask(self, key, title, body, choices, timeout, done):
        """choices: [(key, label)]. done(choice key) is called from a thread when one is pressed (never on timeout)."""
        if sys.platform == "darwin":     # osascript can't do buttons here: the page queue is the way to answer
            return self._run(["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                              "-e", "end run", title, body])
        if not self.actions():
            return self._run(["notify-send", "-a", "DogMute", "-u", "critical", title, body + " (answer on the DogMute page)"])
        cmd = ["notify-send", "-a", "DogMute", "-u", "critical", "-t", str(int(timeout * 1000)), "--wait"]
        for k, label in choices:
            cmd += ["-A", f"{k}={label}"]
        threading.Thread(target=self._wait, args=(key, cmd + [title, body], [k for k, _ in choices], timeout, done), daemon=True).start()

    def _run(self, cmd):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _wait(self, key, cmd, keys, timeout, done):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        self.procs[key] = p
        try:
            out, _ = p.communicate(timeout=timeout + 2)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        self.procs.pop(key, None)
        lines = out.split()
        if lines and lines[-1] in keys:
            done(lines[-1])

    def cancel(self, key):
        p = self.procs.pop(key, None)
        if p:
            try:
                p.kill()
            except OSError:
                pass


class Review:
    def __init__(self, core, notifier=None):
        self.core = core
        self.dir = core.log_path.parent / "review"
        self.path = self.dir / "items.jsonl"
        self.notifier = notifier or Notifier()
        self.lock = threading.RLock()
        self.items = collections.OrderedDict()     # id -> item, oldest first
        self.live = {}                             # id -> monotonic deadline until which "yes" still mutes
        self.pending = {}                          # (uid, kind) -> context of a mute that is about to happen
        self.sent = collections.deque()            # monotonic times of the questions popped
        self.last = {}                             # rate limit keys -> monotonic time
        self.later = lambda s, f: _timer(s, f)     # tests run these at once
        self._load()
        self.prune()

    # ---------- store ----------
    @property
    def cfg(self):
        return self.core.cfg["review"]

    @property
    def on(self):
        return self.cfg["enabled"]

    def _load(self):
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            try:
                it = json.loads(line)
                self.items[it["id"]] = it
            except (ValueError, KeyError):
                pass

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i) + "\n" for i in self.items.values()))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def start(self):
        """Daily prune (the start-up one ran in __init__)."""
        def tick():
            self.prune()
            self.later(86400, tick)
        self.later(86400, tick)

    def prune(self):
        cut = datetime.datetime.now() - datetime.timedelta(days=self.cfg["keep_days"])
        with self.lock:
            old = [i for i, it in self.items.items() if _when(it) < cut]
            for i in old:
                self._drop_clip(self.items.pop(i))
            if self.dir.exists():
                for f in self.dir.glob("*.wav"):
                    if f.stem not in self.items:
                        f.unlink(missing_ok=True)       # a clip whose case is gone
            if old:
                self._save()
        return len(old)

    def _drop_clip(self, it):
        if it.get("clip"):
            (self.dir / it["clip"]).unlink(missing_ok=True)

    def _new(self, kind, entry, text, p, uid, cands, how, action, t0, t1, mask=True, **extra):
        """Add a case. t0..t1 (monotonic) is the event; the clip is cut once its tail has been heard."""
        it = {"id": uuid.uuid4().hex[:12], "t": datetime.datetime.now().isoformat(timespec="seconds"), "kind": kind,
              "entry": entry, "masked": masked(entry) if mask else entry, "transcript": text, "p": None if p is None else round(p, 3),
              "candidates": [{"uid": u, "name": self.core.name(u)} for u in cands], "picked": uid, "how": how, "action": action,
              "label": None, "labelled_by": None, "labelled_at": None, "clip": None, **extra}
        with self.lock:
            self.items[it["id"]] = it
            self._save()
        if self.cfg["clips"]:
            self.later(max(0.0, t1 + AFTER - time.monotonic()) + 0.3, lambda: self._cut(it["id"], t0, t1))
        return it

    def _cut(self, id, t0, t1):
        x, ts = self.core.ring.get(t0 - BEFORE, t1 + AFTER)
        if len(x) < RATE * 0.2:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"{id}.wav"
        with wave.open(str(self.dir / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        with self.lock:
            if id in self.items:
                self.items[id]["clip"] = name
                self.items[id]["event"] = [round(t0 - ts, 2), round(t1 - ts, 2)]      # where in the clip it happened (seconds)
                self._save()
            else:
                (self.dir / name).unlink(missing_ok=True)

    def clip_path(self, id):
        it = self.items.get(id) if ID_RX.match(id or "") else None
        f = self.dir / it["clip"] if it and it.get("clip") else None
        return f if f and f.is_file() else None

    # ---------- asking ----------
    def ask_word(self, uid, entry, a, b, p=None, text="", how="", near=0.0, **_):
        who = "someone" if near else self.core.name(uid)
        q = f'Did {who} say "{masked(entry)}"?'
        it = self._new("word", entry, text, p, uid, [uid], how, "asked", a, b, question=q, **({"near": round(near, 2)} if near else {}))
        self._ask(it, q, [("yes", "Correct"), ("no", "Wrong")])

    def ask_who(self, cand, entry, a, b, p=None, text="", how="", **_):
        cand = cand[:3]
        q = f'Who said "{masked(entry)}"?'
        it = self._new("who", entry, text, p, None, cand, how, "asked", a, b, question=q)
        self._ask(it, q, [(f"c{i}", self.core.name(u)) for i, u in enumerate(cand)] + [("no", "Nobody")])

    def ask_spam(self, uid, reason, t):
        if self.last.get(("spam", uid), -999) > time.monotonic() - SPAM_GAP:
            return
        self.last[("spam", uid)] = time.monotonic()
        q = f"Is {self.core.name(uid)} mic-spamming ({reason})?"
        it = self._new("spam", reason.split(" (")[0], "", None, uid, [uid], reason, "asked", t - 2.0, t, mask=False, reason=reason, question=q)
        self._ask(it, q, [("yes", "Correct"), ("no", "Wrong")])

    def near_ok(self, entry, uid):
        """Near-misses are frequent: one per NEAR_GAP overall, and the same one at most once a minute."""
        now = time.monotonic()
        if now - self.last.get("near", -999) < NEAR_GAP or now - self.last.get(("near", entry, uid), -999) < 60:
            return False
        self.last["near"] = self.last[("near", entry, uid)] = now
        return True

    def _ask(self, it, question, choices):
        id = it["id"]
        self.live[id] = time.monotonic() + self.cfg["ask_timeout_s"]
        self.later(self.cfg["ask_timeout_s"] + 0.5, lambda: self.expire(id))
        print(f"review: asked {it['kind']} {id}", flush=True)
        now = time.monotonic()
        while self.sent and now - self.sent[0] > 60:
            self.sent.popleft()
        if not self.cfg["notify"] or len(self.sent) >= self.cfg["max_per_min"]:
            return                                             # waits on the page
        self.sent.append(now)
        body = "Press a button, or answer on the DogMute page" if sys.platform != "darwin" else "Answer on the DogMute page"
        self.notifier.ask(id, question, body, choices, self.cfg["ask_timeout_s"], lambda key: self._answer(id, key))

    def _answer(self, id, key):
        it = self.items.get(id)
        if not it:
            return
        if key == "yes":
            lab = "correct"
        elif key == "no":
            lab = "wrong"
        else:
            lab = it["candidates"][int(key[1:])]["uid"]       # who: c0..c2
        try:
            self.label(id, lab, "notify")
        except ValueError:
            pass

    def expire(self, id):
        """The question's time is up: stays in the queue unlabelled; a late answer only labels."""
        self.live.pop(id, None)
        self.notifier.cancel(id)
        it = self.items.get(id)
        if it and it["label"] is None and it["action"] == "asked":
            with self.lock:
                it["action"] = "timed out"
                self._save()

    # ---------- answering ----------
    def label(self, id, label, by="page"):
        """Label a case; True if it was new. A yes mutes when the question is still live, else it only labels."""
        with self.lock:
            it = self.items.get(id)
            if not it:
                raise ValueError("no such case")
            cands = [c["uid"] for c in it["candidates"]]
            ok = label in ("correct", "wrong") if it["kind"] != "who" else label == "wrong" or label in cands
            if not ok:
                raise ValueError("not an answer for this case")
            if it["label"] is not None:
                return False
            live = time.monotonic() <= self.live.get(id, 0)
            it.update(label=label, labelled_by=by, labelled_at=datetime.datetime.now().isoformat(timespec="seconds"))
            target = (label if it["kind"] == "who" else it["picked"]) if label != "wrong" else None
            if label == "wrong":
                it["action"] = "not muted" if it["action"] in ("asked", "timed out") else it["action"]
            elif it["kind"] != "mute":
                it["action"] = "muted" if live else "label only"
            self.live.pop(id, None)
            self._save()
        self.notifier.cancel(id)
        if target and live and it["kind"] != "mute":
            self.pending[(target, "noise" if it["kind"] == "spam" else "word")] = {"item": id, "ts": time.monotonic()}
            what = f'said "{it["masked"]}"' if it["kind"] != "spam" else it.get("reason", "mic spam")
            self.core.act(target, f"{what} (confirmed by you)", "noise" if it["kind"] == "spam" else "word")
        return True

    # ---------- confident mutes ----------
    def note(self, uid, kind, **ctx):
        """What the word that is about to mute `uid` was (core.heard), so the mute's case can carry it."""
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v["ts"] < 60}
        self.pending[(uid, kind)] = {**ctx, "ts": now}

    def muted(self, uid, kind, reason):
        """A mute really happened (core._act). Confirmed-by-you mutes update their case; the rest get one."""
        ctx = self.pending.pop((uid, kind), {})
        if ctx.get("item"):
            return
        if not self.on:
            return
        now = time.monotonic()
        a, b = ctx.get("a", now - 2.0), ctx.get("b", now)
        entry = ctx.get("entry") or reason.split(" (")[0]
        self._new("mute", entry, ctx.get("text", ""), ctx.get("p"), uid, [uid], ctx.get("how", reason), "muted", a, b,
                  mask=bool(ctx.get("entry")), **({} if ctx.get("entry") else {"reason": reason}))

    def unmuted(self, uid):
        """You unmuted them: a mute in the last 10 minutes was wrong."""
        now = datetime.datetime.now()
        with self.lock:
            for it in reversed(self.items.values()):
                if it["kind"] == "mute" and it["picked"] == uid and it["label"] is None and (now - _when(it)).total_seconds() <= UNMUTE_WINDOW:
                    it.update(label="wrong", labelled_by="unmute", labelled_at=now.isoformat(timespec="seconds"))
                    self._save()
                    return it["id"]

    # ---------- the page ----------
    def view(self, limit=40):
        """Unlabelled questions, newest first, without the transcript or the list line (the page shows masked words)."""
        with self.lock:
            q = [i for i in self.items.values() if i["label"] is None and i["kind"] != "mute"]
            now = time.monotonic()
            return {"count": len(q), "items": [
                {k: i[k] for k in ("id", "t", "kind", "question", "candidates", "action", "clip", "p")}
                | {"live": now <= self.live.get(i["id"], 0)} for i in reversed(q)][:limit]}


def _when(it):
    try:
        return datetime.datetime.fromisoformat(it["t"])
    except (KeyError, ValueError):
        return datetime.datetime.min


def _timer(s, f):
    t = threading.Timer(s, f)
    t.daemon = True
    t.start()
