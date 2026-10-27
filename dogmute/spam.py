"""Mic-spam rules on the call mix: each one a noise rule from the rulebook (loud, clip, blast, nonstop, flaps), so they
can be changed, turned off, deleted and added to. Each 50 ms frame counts for everyone whose SPEAKING is on."""
import collections
import math

import numpy as np

FRAME = 800  # 50 ms at 16 kHz
FPS = 20
AUDIBLE = -45.0   # a short burst counts as a flap if something louder than keyboard clicks came through
CLOSE = 0.8       # a rule that reaches this share of its threshold without firing is "close" (the review queue asks about it)
CLOSE_DB = 20 * math.log10(CLOSE)   # the same share of a loudness threshold: -1.9 dB
# Loudness is relative to the call: how loud Discord plays it differs by 10 dB and more between setups (on a hot one normal
# speech reaches -6 dBFS over 400 ms and clips), so a loud rule's level rises to sit over_call_db above the call's own
# loud speech, up to its ceiling.
REF_S, REF_LAG = 120, 5   # the call's level: 90th percentile of the audible frames of the last 2 min, minus the last 5 s
REF_PRIOR = -14.0         # until 3 s of the call were heard
BLAST_MS = 400            # a sudden blast is judged over this long

DEFAULTS = {   # the old settings; tests and the first start turn them into rules (rulebook.legacy)
    "enabled": True,
    "earrape_db": -6.0,
    "earrape_ms": 400,
    "clip_frac": 0.03,
    "jump_db": 20.0,
    "continuous_s": 10.0,
    "continuous_db": -30.0,
    "continuous_dip_frac": 0.05,
    "flaps_per_10s": 12,
}
WIN = np.hanning(FRAME).astype(np.float32)
BAND = slice(6, 450)          # 100-7000 Hz in the 1024-point spectrum


class Why(str):
    """A reason to act, with the rule's action (mute / ask / log) and id."""
    def __new__(cls, text, action="mute", rule=None):
        s = super().__new__(cls, text)
        s.action, s.rule = action, rule
        return s


def db(x):
    return 10 * math.log10(max(float(np.mean(x * x)), 1e-10))


def pmean(v):
    return 10 * math.log10(max(float(np.mean(10 ** (np.asarray(v) / 10))), 1e-10))


def feats(x):
    """One frame: (level dB, clipped share, spectral flatness, tonal peak share, peak bin, pitch correlation)."""
    lv = db(x)
    clip = float(np.mean(np.abs(x) >= 0.97))
    if lv < -60:
        return lv, clip, 0.0, 0.0, 0, 0.0
    p = np.abs(np.fft.rfft(x * WIN, 1024)) ** 2
    b = p[BAND] + 1e-12
    flat = float(np.exp(np.mean(np.log(b))) / np.mean(b))
    pk = int(np.argmax(b))
    tonal = float(b[max(0, pk - 2):pk + 3].sum() / b.sum())
    ac = np.fft.irfft(p, 1024)
    pitch = float(ac[40:230].max() / max(ac[0], 1e-12))    # 70-400 Hz: voiced speech repeats, noise doesn't
    return lv, clip, flat, tonal, pk, pitch


def what(r, detail):
    return f"{r['name'].lower()} ({detail})"


class Speaker:
    def __init__(self):
        self.rows = collections.deque(maxlen=FPS * 40)   # frame features while their mic was on (alone or not)
        self.voice = collections.deque(maxlen=FPS * 60)  # their normal speech levels (alone), for the blast rule
        self.bursts = collections.deque(maxlen=64)       # (start, length, loudest frame) of finished mic-on bursts
        self.peak = -100.0
        self.on_since = None
        self.off_at = None
        self.talk_since = None                            # start of this stretch; gaps under 0.5 s don't end it


class SpamRules:
    def __init__(self, cfg, rules=None):
        """cfg: {"enabled": ...}; rules: a callable giving the noise rules that are on (default: cfg's old settings)."""
        self.cfg = cfg
        if rules is None:
            from .rulebook import NOISE, legacy
            fixed = [r for r in legacy({"spam": cfg}, "") if r["type"] in NOISE and r["on"]]
            rules = lambda: fixed   # noqa: E731
        self.rules = rules
        self.who = collections.defaultdict(Speaker)
        self.mix = collections.deque(maxlen=FPS * 30)     # (level, clipped share, voiced?, alone uid) of the whole call
        self.hist = collections.deque(maxlen=FPS * REF_S)  # levels of its audible frames, for the call's level
        self.ref = REF_PRIOR
        self.n = 0
        self.fired = {}                                   # (rule id, uid) -> t, one hit per rule window
        self.close = []                                   # (uid, reason) of rules that nearly fired, until pop_close()

    def pop_close(self):
        out, self.close = self.close, []
        return out

    def speaking(self, uid, on, t):
        s = self.who[uid]
        if on:
            if s.on_since is not None:
                return                                     # Discord repeated itself
            s.on_since, s.peak = t, -100.0
            if s.talk_since is None or s.off_at is None or t - s.off_at > 0.5:
                s.talk_since = t
                s.rows.clear()
        elif s.on_since is not None:
            s.bursts.append((s.on_since, t - s.on_since, s.peak))
            s.on_since, s.off_at = None, t

    # ---------- mic on/off ----------
    def flaps(self, uid, t):
        """Rapid on/off check, run on every SPEAKING_START: many short bursts, not one person's normal voice gating."""
        if not self.cfg["enabled"]:
            return None
        for r in self.rules():
            if r["type"] != "flaps":
                continue
            n = 1 + self._short(uid, t, r)
            detail = f"{n} short on/off in {r['window_s']:g} s"
            if n < r["count"]:
                if n >= CLOSE * r["count"] and r["action"] == "mute":
                    self.close.append((uid, what(r, detail)))
                continue
            if sum(1 for u in self.who if u != uid and self._short(u, t, r) >= 4) >= 2:
                continue                                  # three at once = Discord hiccup, not three spammers
            return Why(what(r, detail), r["action"], r["id"])
        return None

    def _short(self, uid, t, r):
        return sum(1 for a, d, pk in self.who[uid].bursts if t - a <= r["window_s"] and d <= r["short_s"] and pk >= AUDIBLE)

    # ---------- the mix, frame by frame ----------
    def frame(self, active, x, t):
        """One 50 ms frame of the call mix, `active` = who's mic is on. Returns [(uid, Why)] to act on."""
        f = feats(x)
        lv, clip, flat, tonal, pk, pitch = f
        voiced = pitch >= 0.5 and flat < 0.15 and tonal < 0.5
        solo = active[0] if len(active) == 1 else None
        self.mix.append((lv, clip, voiced, solo))
        self.n += 1
        if lv > AUDIBLE:
            self.hist.append(lv)
        if self.n % FPS == 0:
            h = list(self.hist)[:-FPS * REF_LAG]
            self.ref = float(np.percentile(h, 90)) if len(h) >= FPS * 3 else REF_PRIOR
        for u in active:
            self.who[u].rows.append((t,) + f + (u == solo,))
            self.who[u].peak = max(self.who[u].peak, lv)
        out = []
        if self.cfg["enabled"] and active:
            for r in self.rules():
                ev = EVAL.get(r["type"])
                if ev is None or (r["type"] == "nonstop" and self.n % 5):
                    continue
                for hit in ev(self, r, active, solo, t):
                    u, detail = hit
                    if any(u == o[0] for o in out) or t - self.fired.get((r["id"], u), -99) < _hold(r):
                        continue
                    self.fired[(r["id"], u)] = t
                    out.append((u, Why(what(r, detail), r["action"], r["id"])))
            if not out and self.n % 5 == 0:
                self._near(active, solo, t)
        if solo is not None and lv > -50:
            self.who[solo].voice.append(lv)
        return out

    def _near(self, active, solo, t):
        """A muting loud, clip or blast rule at CLOSE of its threshold: the review queue asks."""
        for r in self.rules():
            ev = EVAL.get(r["type"])
            if r["action"] != "mute" or r["type"] not in ("loud", "clip", "blast"):
                continue
            for u, detail in ev(self, r, active, solo, t, near=True):
                self.close.append((u, what(r, detail)))
                return

    # ---------- the rules ----------
    def lim(self, r):
        """A loud rule's level on this call: its own, raised on a call that is itself that loud."""
        if not r.get("over_call_db"):
            return r["db"]
        return max(r["db"], min(self.ref + r["over_call_db"], r["ceil_db"]))

    def _loud(self, r, active, solo, t, near=False):
        n = max(1, int(r["ms"] / 50))
        if len(self.mix) < n:
            return []
        w = list(self.mix)[-n:]
        L = [m[0] for m in w]
        loud = pmean(L)
        lim = self.lim(r) + (r["voice_db"] if sum(m[2] for m in w) >= 0.6 * n else 0)
        if loud < lim + (CLOSE_DB if near else 0):
            return []
        if r.get("steady_db") is not None and np.percentile(L, 25) < lim - r["steady_db"]:
            return []
        u = self._owner(w, active, solo, t, loud)
        held = f" for {n / FPS:.1f} s" if n >= FPS else ""
        return [(u, f"{loud:.0f} dBFS{held}")] if u else []

    def _clip(self, r, active, solo, t, near=False):
        n = max(1, int(r["ms"] / 50))
        if len(self.mix) < n:
            return []
        w = list(self.mix)[-n:]
        share, loud = float(np.mean([m[1] for m in w])), pmean([m[0] for m in w])
        if share < r["frac"] * (CLOSE if near else 1) or loud < r["min_db"]:
            return []
        u = self._owner(w, active, solo, t, loud)
        return [(u, f"{100 * share:.0f}% clipped")] if u else []

    def _blast(self, r, active, solo, t, near=False):
        if solo is None or len(self.who[solo].voice) < FPS * 3:
            return []
        n = BLAST_MS // 50
        loud = pmean([m[0] for m in list(self.mix)[-n:]])
        jump = loud - float(np.median(self.who[solo].voice))
        if loud < r["min_db"] or jump < r["jump_db"] * (CLOSE if near else 1):
            return []
        return [(solo, f"+{jump:.0f} dB over their voice")]

    def _nonstop(self, r, active, solo, t, near=False):
        """Mic open for after_s with no gaps and not shaped like speech = music, noise, tone, soundboard. The whole mix
        counts while theirs is the only mic open that long (others chatting over music don't); with several such mics,
        only frames where they were alone count, or one person's music would get every open mic in the call muted."""
        out = []
        for u in active:
            s = self.who[u]
            if s.talk_since is None or t - s.talk_since < r["after_s"]:
                continue
            n = int(min(r["after_s"], 10) * FPS)
            rows = list(s.rows)[-n:]
            if any(o != u and so.on_since is not None and so.talk_since is not None and t - so.talk_since >= r["after_s"]
                   for o, so in self.who.items()):
                rows = [x for x in rows if x[-1]]
            if len(rows) < 0.8 * n:
                continue
            L = np.array([x[1] for x in rows])
            if pmean(L) < r["db"]:
                continue
            flat, tonal = np.array([x[3] for x in rows]), np.array([x[4] for x in rows])
            pk = np.array([x[5] for x in rows])
            held = f"{t - s.talk_since:.0f} s"
            # shapes speech never has: hiss (flat spectrum) or one steady pitch (whistle, feedback)
            if np.mean(flat > 0.3) > 0.8:
                out.append((u, f"{held}, hiss"))
                continue
            if np.mean(tonal > 0.5) > 0.8 and np.std(pk) < 6:
                out.append((u, f"{held}, steady tone"))
                continue
            # their own frames (nobody else talking): speech keeps dropping out between words, music/soundboard doesn't
            alone = np.array([x[1] for x in s.rows if x[-1]][-FPS * 30:])
            if len(alone) < FPS * 2 or pmean(alone) < r["db"]:
                continue
            dips = float(np.mean(alone < np.percentile(alone, 90) - 10))
            if dips < r["dip_frac"]:
                out.append((u, f"{held}, {100 * dips:.0f}% gaps"))
        return out

    def _owner(self, w, active, solo, t, loud):
        """Whose sound frames w were. Long stretches: the one person alone in them (two people taking turns being loud
        isn't one of them screaming). Otherwise the only one talking, else the one whose mic opened as it started."""
        alone = {m[3] for m in w if m[3] is not None}
        if len(w) >= FPS:
            if len(alone) > 1:
                return None
            if alone and sum(m[3] is not None for m in w) >= 0.5 * len(w):
                return next(iter(alone))
        if solo is not None:
            return solo
        k = 0
        for m in reversed(self.mix):
            if m[0] < loud - 10:
                break
            k += 1
        onset = t - k / FPS
        new = [u for u in active if self.who[u].on_since is not None and abs(self.who[u].on_since - onset) <= 0.5]
        return new[0] if len(new) == 1 else None


def _hold(r):
    """After a rule fires for someone, it stays quiet for them this long (the same sound would fire every frame)."""
    return {"loud": r.get("ms", 0) / 1000, "clip": r.get("ms", 0) / 1000, "blast": 1.0, "nonstop": 10.0}.get(r["type"], 1.0)


EVAL = {"loud": SpamRules._loud, "clip": SpamRules._clip, "blast": SpamRules._blast, "nonstop": SpamRules._nonstop}
