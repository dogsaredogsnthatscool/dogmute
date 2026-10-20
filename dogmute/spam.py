"""Mic-spam rules on the call mix, per speaker (a frame counts for someone only while they're the only one talking)."""
import collections
import math

import numpy as np

FRAME = 800  # 50 ms at 16 kHz
FPS = 20

DEFAULTS = {
    "enabled": True,
    "earrape_db": -6.0,        # loudness (RMS dBFS) held for earrape_ms = earrape
    "earrape_ms": 400,
    "clip_frac": 0.03,         # share of clipped samples over 0.5 s (with loudness > -16 dBFS)
    "jump_db": 20.0,           # this much louder than their own normal voice, and above -16 dBFS
    "continuous_s": 20.0,      # talking with no pause this long ...
    "continuous_db": -30.0,    # ... above this level ...
    "continuous_dip_frac": 0.05,  # ... and fewer quiet gaps than speech has = music/noise/soundboard
    "flaps_per_10s": 15,       # mic on/off this many times in 10 s
}


def db(x):
    return 10 * math.log10(max(float(np.mean(x * x)), 1e-10))


class Speaker:
    def __init__(self):
        self.levels = collections.deque(maxlen=FPS * 12)   # recent frame levels while solo
        self.clips = collections.deque(maxlen=FPS // 2)
        self.voice = collections.deque(maxlen=FPS * 60)    # their normal speech levels, for the jump rule
        self.starts = collections.deque(maxlen=64)
        self.talk_since = None


class SpamRules:
    def __init__(self, cfg):
        self.cfg = cfg
        self.who = collections.defaultdict(Speaker)

    def speaking(self, uid, on, t):
        s = self.who[uid]
        if on:
            s.starts.append(t)
            if s.talk_since is None:
                s.talk_since = t
        else:
            s.talk_since = None
            s.levels.clear()
            s.clips.clear()

    def flaps(self, uid, t):
        """Rapid on/off check, run on every SPEAKING_START."""
        c = self.cfg
        if not c["enabled"]:
            return None
        n = sum(1 for x in self.who[uid].starts if t - x <= 10)
        if n >= c["flaps_per_10s"]:
            return f"mic spam ({n} on/off in 10 s)"
        return None

    def frame(self, uid, x, t):
        """One 50 ms frame heard while `uid` was the only one talking. Returns a reason to mute, or None."""
        c = self.cfg
        s = self.who[uid]
        lv = db(x)
        clip = float(np.mean(np.abs(x) >= 0.97))
        s.levels.append(lv)
        s.clips.append(clip)
        if not c["enabled"]:
            return None
        n = max(1, int(c["earrape_ms"] / 50))
        if len(s.levels) >= n:
            recent = list(s.levels)[-n:]
            loud = 10 * math.log10(sum(10 ** (v / 10) for v in recent) / n)
            if loud >= c["earrape_db"]:
                return f"earrape ({loud:.0f} dBFS)"
            if len(s.clips) == s.clips.maxlen and np.mean(s.clips) >= c["clip_frac"] and loud >= -16:
                return f"earrape (clipping {100 * np.mean(s.clips):.0f}%)"
            if len(s.voice) >= FPS * 3 and loud >= -16:
                base = float(np.median(s.voice))
                if loud - base >= c["jump_db"]:
                    return f"sudden blast (+{loud - base:.0f} dB over their voice)"
        if lv > -50:
            s.voice.append(lv)
        if s.talk_since is not None and t - s.talk_since >= c["continuous_s"] and len(s.levels) >= FPS * 10:
            last = np.array(list(s.levels)[-FPS * 10:])
            mean = 10 * math.log10(np.mean(10 ** (last / 10)))
            dips = float(np.mean(last < np.percentile(last, 90) - 15))  # speech keeps dropping out between words
            if mean >= c["continuous_db"] and dips < c["continuous_dip_frac"]:
                return f"nonstop noise/music ({t - s.talk_since:.0f} s, {100 * dips:.0f}% gaps)"
        return None
