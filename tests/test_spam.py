import numpy as np

from dogmute.spam import DEFAULTS, FPS, FRAME, SpamRules

rng = np.random.default_rng(0)


def run(signal, seconds):
    r = SpamRules(dict(DEFAULTS))
    r.speaking("u", True, 0.0)
    for i in range(int(seconds * FPS)):
        t = i / FPS
        why = r.frame("u", signal(t), t)
        if why:
            return t, why
    return None


def speech(db):
    amp = 10 ** (db / 20) * np.sqrt(3)
    def f(t):
        env = max(0.0, np.sin(2 * np.pi * 3.5 * t)) ** 2 * (0 if (t % 2.3) > 1.9 else 1)  # syllables + breaths
        return (rng.uniform(-1, 1, FRAME) * amp * env).astype(np.float32)
    return f


def tone(db):
    a = 10 ** (db / 20) / np.sqrt(1.5)  # rms of 3 sines = db
    def f(t):
        n = np.arange(FRAME) / 16000 + t
        return (a * (np.sin(2 * np.pi * 220 * n) + np.sin(2 * np.pi * 277 * n) + np.sin(2 * np.pi * 330 * n))).astype(np.float32)
    return f


def test_normal_talking_is_fine():
    assert run(speech(-22), 40) is None
    assert run(speech(-12), 40) is None  # loud talker


def test_earrape():
    t, why = run(lambda t: np.sign(np.sin(np.arange(FRAME) / 16000 * 2 * np.pi * 400 + t)).astype(np.float32) * 0.99, 5)
    assert t < 0.6 and "earrape" in why


def test_music_spam():
    t, why = run(tone(-20), 40)
    assert 19 < t < 22 and "nonstop" in why


def test_quiet_background_music_is_fine():
    assert run(tone(-40), 40) is None


def test_mic_flapping():
    r = SpamRules(dict(DEFAULTS))
    out = None
    for i in range(20):
        r.speaking("u", True, i * 0.5)
        out = out or r.flaps("u", i * 0.5)
        r.speaking("u", False, i * 0.5 + 0.2)
    assert out and "mic spam" in out
    r2 = SpamRules(dict(DEFAULTS))
    for i in range(20):  # normal: a start every ~1.5 s
        r2.speaking("u", True, i * 1.5)
        assert r2.flaps("u", i * 1.5) is None


def test_sudden_blast():
    def f(t):
        return speech(-32)(t) if t < 8 else tone(-11)(t)
    res = run(f, 10)
    assert res and res[0] >= 8 and "blast" in res[1], res
