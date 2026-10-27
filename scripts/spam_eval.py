"""Synthetic mic-spam eval: simulated SPEAKING events + mixed audio through SpamRules.
Usage: [EVAL_LIVE=1] [EVAL_LEGACY=old_spam.py] python scripts/spam_eval.py [seeds=6] [name filter]   (needs espeak-ng)
Spam must be muted (detect rate, time to detect); clean must never be (false mutes)."""
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dogmute.spam import DEFAULTS, FPS, FRAME, SpamRules  # noqa: E402

LIVE = {"continuous_s": 7.0, "continuous_db": -20.0, "flaps_per_10s": 10}   # the config this was tuned against
LEGACY = os.environ.get("EVAL_LEGACY")   # path to an old-API spam.py (git show <old>:dogmute/spam.py) to compare against


class Old:
    """The pre-rewrite rules behind the new interface: frames only count while exactly one person talks."""
    def __init__(self, cfg):
        import importlib.util
        sp = importlib.util.spec_from_file_location("old_spam", LEGACY)
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        self.r = m.SpamRules(dict(m.DEFAULTS, **{k: v for k, v in cfg.items() if k in m.DEFAULTS}))

    def speaking(self, u, on, t):
        self.r.speaking(u, on, t)

    def flaps(self, u, t):
        return self.r.flaps(u, t)

    def frame(self, act, x, t):
        why = self.r.frame(act[0], x, t) if len(act) == 1 else None
        return [(act[0], why)] if why else []

SR = 16000
CACHE = Path(tempfile.gettempdir()) / "dogmute_eval"
TEXT = ["I think we should go to the other side of the map and check the objective before they rotate.",
        "Yeah that was pretty funny, did you see what happened when he tried to jump over the wall?",
        "Hold on, let me grab a drink and then we can start the next round whenever everyone is ready.",
        "Nah I do not think that is right, the last patch changed how the weapons feel completely.",
        "Okay so tomorrow I have work until six, but after that I am free if you guys want to play.",
        "Wait, who is on the left? I can hear footsteps behind us, somebody watch the stairs please.",
        "That is a really good point, honestly I never thought about it that way until you said it.",
        "My internet has been terrible all day, so if I lag out just keep going without me for a bit.",
        "We could try the new character, she looks strong, but the cooldown on her ability is long.",
        "Alright, I am ready now, send me the invite and I will join your party in a second."]
VOICES = ["en-us+m3", "en-us+f3", "en-gb+m1", "en-us+m5", "en-us+f5", "en-gb+f2"]


def dbfs(x):
    return 10 * np.log10(max(float(np.mean(x * x)), 1e-12))


def at(x, db):
    return (x * 10 ** ((db - dbfs(x)) / 20)).astype(np.float32)


def speech_at(x, db):
    """Scale so the speech itself (not the pauses) has this RMS."""
    a = np.concatenate([x[i:i + FRAME] for i in range(0, len(x) - FRAME, FRAME) if dbfs(x[i:i + FRAME]) > -50] or [x])
    return at(x, db + (dbfs(x) - dbfs(a)))


def say(voice, i, speed, pitch):
    CACHE.mkdir(exist_ok=True)
    key = hashlib.md5(f"{voice}{i}{speed}{pitch}".encode()).hexdigest()
    f = CACHE / (key + ".npy")
    if f.exists():
        return np.load(f)
    raw = subprocess.run(["espeak-ng", "--stdout", "-v", voice, "-s", str(speed), "-p", str(pitch), "-g", "4", TEXT[i]],
                         capture_output=True, check=True).stdout
    w = wave.open(io.BytesIO(raw))
    y = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    y = np.interp(np.arange(0, len(y), w.getframerate() / SR), np.arange(len(y)), y).astype(np.float32)
    np.save(f, y)
    return y


def put(buf, sig, t):
    a = int(t * SR)
    if a < len(buf):
        buf[a:a + len(sig)] += sig[:len(buf) - a]


def vad(sig, hang, thr=-50.0, a=0.0):
    """SPEAKING events from a person's own track, like Discord's voice-activity gate: [(t, on)]."""
    n = len(sig) // FRAME
    lv = [dbfs(sig[i * FRAME:(i + 1) * FRAME]) for i in range(n)]
    ev, on, last = [], False, -9
    for i, v in enumerate(lv):
        t = a + i * FRAME / SR
        if v > thr:
            last = t
            if not on:
                ev.append((t, True)); on = True
        elif on and t - last > hang:
            ev.append((t, False)); on = False
    if on:
        ev.append((a + n * FRAME / SR, False))
    return ev


def talker(rng, dur, level=None, gap=(0.3, 3.0), start=0.0):
    """One person talking in sentences. Returns the track and utterance intervals."""
    level = rng.uniform(-30, -21) if level is None else level
    v, sp, pi = VOICES[rng.integers(len(VOICES))], int(rng.integers(150, 190)), int(rng.integers(30, 70))
    buf, iv, t = np.zeros(int(dur * SR), np.float32), [], start + rng.uniform(0, 1)
    while t < dur - 3:
        y = say(v, int(rng.integers(len(TEXT))), sp, pi)
        y = speech_at(y, level)
        put(buf, y, t)
        iv.append((t, t + len(y) / SR))
        t += len(y) / SR + rng.uniform(*gap)
    return buf, iv


def gate(sig, ev, tail=0.0):
    """The audio Discord sends: only while SPEAKING is on."""
    g = np.zeros_like(sig)
    on = None
    for t, o in ev + [(len(sig) / SR, False)]:
        if o and on is None:
            on = t
        elif not o and on is not None:
            g[int(on * SR):int((t + tail) * SR)] = 1
            on = None
    return sig * g


def ptt(iv, rng):
    ev = []
    for a, b in iv:
        ev += [(a - 0.05, True), (b + rng.uniform(0.1, 0.5), False)]
    return ev


class Scene:
    def __init__(self, dur):
        self.n = int(dur * SR)
        self.tr = {}  # uid -> that person's audio (muting them drops it from the mix)
        self.ev = []  # (t, uid, on)
        self.spam = set()
        self.t0 = 0.0

    def add(self, uid, sig, ev, t=0.0):
        put(self.tr.setdefault(uid, np.zeros(self.n, np.float32)), np.clip(sig, -1, 1), t)
        self.ev += [(a, uid, on) for a, on in ev]

    def talkers(self, rng, uids, mode="vad", dur=None, **kw):
        for u in uids:
            sig, iv = talker(rng, dur or self.n / SR, **kw)
            ev = vad(sig, rng.uniform(0.15, 0.35)) if mode == "vad" else ptt(iv, rng)
            self.add(u, sig, ev)


# ---------- sounds ----------
def tt(d):
    return np.arange(int(d * SR)) / SR


def music(rng, d, level):
    bpm = rng.uniform(95, 140)
    beat = 60 / bpm
    t = tt(d)
    out = np.zeros_like(t)
    chords = [[220, 261.6, 329.6], [174.6, 220, 261.6], [261.6, 329.6, 392], [196, 246.9, 293.7]]
    for k in range(int(d / beat / 4) + 1):
        a, b = int(k * 4 * beat * SR), int((k + 1) * 4 * beat * SR)
        tc = t[a:b] - t[a] if a < len(t) else None
        if tc is None or len(tc) == 0:
            continue
        for f in chords[k % 4]:
            for h in (1, 2, 3):
                out[a:b] += np.sin(2 * np.pi * f * h * tc)[:len(out[a:b])] / h * 0.3
        out[a:b] += 0.4 * np.sin(2 * np.pi * chords[k % 4][0] / 2 * tc)[:len(out[a:b])]
    for i in range(int(d / (beat / 2))):
        s = int(i * beat / 2 * SR)
        k = tt(0.2)
        if i % 4 == 0:
            put(out, 1.2 * np.sin(2 * np.pi * (45 * k + 80 * (1 - np.exp(-k * 30)) / 30 * 0 + 120 * np.exp(-k * 30) / 30)) * np.exp(-k * 18), s / SR)
        if i % 4 == 2:
            put(out, 0.5 * rng.standard_normal(len(k)) * np.exp(-k * 25), s / SR)
        put(out, 0.2 * np.diff(rng.standard_normal(len(k) + 1)) * np.exp(-k * 90), s / SR)
    return at(out, level)


def noise(rng, d, level, crackle=False):
    x = rng.standard_normal(int(d * SR)).astype(np.float32)
    if crackle:
        x *= 1 + 3 * (rng.random(len(x)) > 0.9995) * rng.standard_normal(len(x))
        x *= 0.6 + 0.4 * np.sin(2 * np.pi * rng.uniform(0.2, 0.8) * tt(d))
    return at(x, level)


def earrape(rng, d, level=-3.0):
    t = tt(d)
    x = np.sin(2 * np.pi * rng.uniform(40, 70) * t) * 3 + 0.5 * np.sign(np.sin(2 * np.pi * rng.uniform(300, 900) * t)) + 0.5 * rng.standard_normal(len(t))
    return np.clip(x * 2.5, -1, 1).astype(np.float32)


def voiced(rng, d, f0, level, vib=5.0, breath=0.05):
    t = tt(d)
    ph = 2 * np.pi * np.cumsum(f0 * (1 + 0.04 * np.sin(2 * np.pi * vib * t))) / SR
    x = sum(np.sin(h * ph) / h ** 0.8 for h in range(1, 18)) + breath * rng.standard_normal(len(t)) * 3
    env = np.minimum(1, t / 0.05) * np.minimum(1, (d - t) / 0.08)
    return at(x * env, level)


def scream(rng, d, level):
    t = tt(d)
    return voiced(rng, d, 650 + 250 * np.sin(2 * np.pi * rng.uniform(0.3, 0.8) * t) + 100 * rng.random(), level, vib=7)


def laugh(rng, level):
    out, t = np.zeros(int(2.5 * SR), np.float32), 0.1
    for i in range(rng.integers(6, 11)):
        out_ = voiced(rng, 0.13, rng.uniform(220, 300), level - 1.5 * i, vib=0, breath=0.3)
        put(out, out_ * np.hanning(len(out_)), t)
        t += rng.uniform(0.17, 0.24)
    return out


def tone(rng, d, level, f=None, wobble=30):
    f = f or rng.uniform(1800, 3200)
    t = tt(d)
    return at(np.sin(2 * np.pi * np.cumsum(f + wobble * np.sin(2 * np.pi * 0.7 * t)) / SR) + 0.1 * np.sin(2 * np.pi * 2 * f * t), level)


def horn(rng, d, level):
    t = tt(d)
    x = sum(np.sign(np.sin(2 * np.pi * f * (1 + 0.01 * np.sin(2 * np.pi * 6 * t)) * t)) for f in (440, 443, 554, 660))
    return at(x * np.minimum(1, (d - t) / 0.2), level)


def boom(rng, level):
    t = tt(1.0)
    return at((np.sin(2 * np.pi * 55 * t * np.exp(-t * 1.5)) * 2 + 0.3 * rng.standard_normal(len(t))) * np.exp(-t * 3), level)


def clicks(rng, d, level, rate=6.0):
    out = np.zeros(int(d * SR), np.float32)
    t = rng.uniform(0, 0.5)
    while t < d - 0.1:
        k = tt(0.03)
        put(out, np.diff(rng.standard_normal(len(k) + 1)).astype(np.float32) * np.exp(-k * 250) * 10 ** (rng.uniform(level - 6, level) / 20), t)
        t += rng.exponential(1 / rate) + 0.04
    return out


# ---------- scenes ----------
def with_others(sc, rng, n, chatty=False):
    if n:
        sc.talkers(rng, [f"p{i}" for i in range(n)], "vad", gap=(0.3, 2.0) if chatty else (2.0, 8.0))


def sc_spam(kind, n_others, chatty=False):
    def make(rng, d=75.0):
        sc = Scene(d)
        with_others(sc, rng, n_others, chatty)
        t0 = 15.0
        sc.spam, sc.t0 = {"S"}, t0
        L = d - t0
        if kind == "music":
            lv = rng.uniform(-24, -14)
            sc.add("S", music(rng, L, lv), [(t0, True), (d, False)], t0)
        elif kind == "music loud":
            sc.add("S", music(rng, L, rng.uniform(-14, -8)), [(t0, True), (d, False)], t0)
        elif kind == "noise":
            y = noise(rng, L, rng.uniform(-24, -14), crackle=rng.random() < 0.5)
            sc.add("S", y, [(t0, True), (d, False)], t0)
        elif kind == "earrape":
            sc.add("S", at(earrape(rng, L), rng.uniform(-9, -3)), [(t0, True), (d, False)], t0)
        elif kind == "tone/feedback":
            sc.add("S", tone(rng, L, rng.uniform(-22, -10)), [(t0, True), (d, False)], t0)
        elif kind == "scream":
            t = t0
            while t < d - 5:
                dur = rng.uniform(3, 5)
                y = scream(rng, dur, rng.uniform(-11, -7))
                sc.add("S", y, [(t, True), (t + dur + 0.2, False)], t)
                t += dur + rng.uniform(1.5, 3)
        elif kind == "soundboard loud":
            t = t0
            while t < d - 3:
                dur = rng.uniform(1, 2.5)
                y = horn(rng, dur, rng.uniform(-12, -4)) if rng.random() < 0.5 else boom(rng, rng.uniform(-10, -3))
                sc.add("S", y, [(t, True), (t + len(y) / SR + 0.1, False)], t)
                t += len(y) / SR + rng.uniform(1.5, 4)
        elif kind == "soundboard burst":
            t = t0
            while t < d - 2:
                dur = rng.uniform(0.2, 0.5)
                y = horn(rng, dur, rng.uniform(-24, -12)) if rng.random() < 0.5 else voiced(rng, dur, rng.uniform(150, 300), rng.uniform(-24, -14))
                sc.add("S", y, [(t, True), (t + dur + 0.05, False)], t)
                t += dur + rng.uniform(0.15, 0.5)
        elif kind == "mic flapping":
            t = t0
            while t < d - 1:
                dur = rng.uniform(0.15, 0.45)
                y = noise(rng, dur, rng.uniform(-45, -30)) if rng.random() < 0.5 else voiced(rng, dur, 150, -25)
                sc.add("S", y, [(t, True), (t + dur, False)], t)
                t += dur + rng.uniform(0.15, 0.4)
        return sc
    return make


def sc_clean(kind):
    def make(rng, d=75.0):
        sc = Scene(d)
        if kind in ("conversation (vad)", "conversation (ptt)"):
            sc.talkers(rng, [f"p{i}" for i in range(int(rng.integers(2, 5)))], "vad" if "vad" in kind else "ptt")
        elif kind == "talking over each other":
            sc.talkers(rng, ["p0", "p1", "p2", "p3"], gap=(0.0, 0.6), level=-26)
        elif kind == "long monologue":
            sig, iv = talker(rng, d, level=rng.uniform(-26, -18), gap=(0.05, 0.5))
            sc.add("p0", sig, vad(sig, rng.choice([0.5, 1.0, 2.0])))
        elif kind == "monologue + listeners":
            sig, iv = talker(rng, d, level=-22, gap=(0.05, 0.4))
            sc.add("p0", sig, vad(sig, 1.5))
            sc.talkers(rng, ["p1", "p2"], gap=(2, 8), level=-30)
        elif kind == "laughing":
            sc.talkers(rng, ["p0", "p1"], gap=(0.5, 2.5))
            for t in np.arange(8, d - 3, 9) + rng.uniform(0, 3, int((d - 11) / 9) + 1)[:len(np.arange(8, d - 3, 9))]:
                y = laugh(rng, rng.uniform(-16, -10))
                sc.add("p2", y, vad(y, 0.3, a=t), t)
        elif kind == "short yell":
            sc.talkers(rng, ["p0", "p1"])
            for t in (20, 45):
                y = voiced(rng, rng.uniform(0.6, 1.6), rng.uniform(300, 500), rng.uniform(-12, -8), vib=6)
                sc.add("p2", y, [(t, True), (t + len(y) / SR + 0.2, False)], t)
        elif kind == "keyboard clicks":
            y = clicks(rng, d, rng.uniform(-38, -28), rate=rng.uniform(4, 9))
            sc.add("p0", y, vad(y, rng.uniform(0.2, 0.6), thr=-70))
            sc.talkers(rng, ["p1"])
        elif kind == "typing under speech":
            sig, iv = talker(rng, d, gap=(0.2, 1.5))
            sig = sig + clicks(rng, d, -30, rate=7)
            sc.add("p0", sig, vad(sig, 0.4, thr=-70))
        elif kind == "borderline vad":   # gate keeps flicking on a quiet talker
            sig, iv = talker(rng, d, level=-33, gap=(0.1, 1.0))
            ev = vad(sig, 0.2, thr=-38)
            sc.add("p0", gate(sig, ev), ev)
            sc.talkers(rng, ["p1"])
        elif kind == "ptt fidget":       # push-to-talk tapped a few times, plus normal use
            sc.talkers(rng, ["p0"], "ptt")
            for t in (10, 28, 46, 64):
                sc.ev += [(t + 0.5 * i, "p1", i % 2 == 0) for i in range(2 * int(rng.integers(3, 6)))]
        elif kind == "discord hiccup":   # everyone's SPEAKING flickers at once
            sc.talkers(rng, ["p0", "p1", "p2"])
            t = rng.uniform(10, 40)
            for u in ("p3", "p4", "p5"):
                sc.ev += [(t + 0.8 * i + rng.uniform(0, 0.2), u, i % 2 == 0) for i in range(14)]
        elif kind == "noisy-mic talker":
            sig, iv = talker(rng, d, level=-24, gap=(0.1, 1.0))
            sig = sig + noise(rng, d, rng.uniform(-36, -30))
            sc.add("p0", sig, [(0.0, True), (d, False)])  # open mic: always transmitting
        return sc
    return make


SPAM = [(k, o) for k in ("music", "music loud", "noise", "earrape", "tone/feedback", "scream", "soundboard loud", "soundboard burst", "mic flapping") for o in (0, 2)]
CLEAN = ["conversation (vad)", "conversation (ptt)", "talking over each other", "long monologue", "monologue + listeners",
         "laughing", "short yell", "borderline vad", "ptt fidget", "discord hiccup", "keyboard clicks", "typing under speech", "noisy-mic talker"]
SCENES = [(f"{k}{' +2 talking' if o else ''}", "spam", sc_spam(k, o)) for k, o in SPAM] + [
    (f"{k} +3 chatty", "spam", sc_spam(k, 3, True)) for k in ("music", "noise", "earrape", "scream")] + [(k, "clean", sc_clean(k)) for k in CLEAN]


def run(sc, cfg=None):
    """Feed the scene through SpamRules as core.on_block / on_event / act would. Returns [(t, uid, reason)] of mutes."""
    r = Old(cfg or {}) if LEGACY else SpamRules(dict(DEFAULTS, **(cfg or {})))
    ev = sorted(sc.ev, key=lambda e: e[0])
    state, off, muted, hits, kinds, i = {}, {}, {}, [], {}, 0

    def mute(t, u, why):
        kind = why.split(" (")[0]
        if u in muted or t - kinds.get(kind, -99) < 4:      # core._act: already muted / this very kind just got someone
            return
        muted[u] = t
        kinds[kind] = t
        hits.append((t, u, why))

    for k in range(sc.n // FRAME):
        t = k * FRAME / SR
        while i < len(ev) and ev[i][0] <= t:
            te, u, on = ev[i]
            i += 1
            if u in muted:
                continue
            state[u] = on
            if not on:
                off[u] = te
            r.speaking(u, on, te)
            if on:
                why = r.flaps(u, te)
                if why:
                    mute(t, u, why)
        act = [u for u in state if (u not in muted or t < muted[u] + 0.3) and (state[u] or t - off.get(u, -9) <= 0.15)]
        x = sum((a[k * FRAME:(k + 1) * FRAME] for u, a in sc.tr.items() if u not in muted or t < muted[u] + 0.2), np.zeros(FRAME, np.float32))
        for u, why in r.frame(act, x, t):
            mute(t, u, why)
    return hits


def main():
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    flt = sys.argv[2] if len(sys.argv) > 2 else ""
    cfg = LIVE if os.environ.get("EVAL_LIVE") else None
    print(f"{'scenario':32} {'detect':>7} {'t_detect s':>11} {'false':>6}  reasons")
    for name, kind, make in SCENES:
        if flt not in name:
            continue
        det, ts, fa, why = 0, [], 0, {}
        for s in range(seeds):
            sc = make(np.random.default_rng(1000 + s))
            hits = run(sc, cfg)
            good = [h for h in hits if h[1] in sc.spam]
            bad = [h for h in hits if h[1] not in sc.spam]
            fa += len(bad)
            for h in good + bad:
                why[h[2].split(" (")[0]] = why.get(h[2].split(" (")[0], 0) + 1
            if good:
                det += 1; ts.append(good[0][0] - sc.t0)
        if kind == "spam":
            print(f"{name:32} {det}/{seeds:<5} {np.median(ts) if ts else float('nan'):11.1f} {fa:6}  {why}")
        else:
            print(f"{name:32} {'-':>7} {'-':>11} {fa:6}  {why}")


if __name__ == "__main__":
    main()
