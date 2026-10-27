import numpy as np

from dogmute.spam import DEFAULTS, FPS, FRAME, SpamRules

rng = np.random.default_rng(0)


def run(signal, seconds, cfg=None):
    r = SpamRules(dict(DEFAULTS, **(cfg or {})))
    r.speaking("u", True, 0.0)
    for i in range(int(seconds * FPS)):
        t = i / FPS
        for u, why in r.frame(["u"], signal(t), t):
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
    assert 9 < t < 12 and "nonstop" in why
    t, why = run(tone(-20), 40, {"continuous_s": 7.0})
    assert 6 < t < 9  # fires near continuous_s, not minutes later


def test_hiss():
    t, why = run(lambda t: (rng.standard_normal(FRAME) * 0.1).astype(np.float32), 40)
    assert t < 12 and "nonstop" in why


def test_quiet_background_music_is_fine():
    assert run(tone(-40), 40) is None


def test_screaming_but_not_a_yell():
    loud = lambda t: tone(-9)(t) * (rng.uniform(0.9, 1.1))
    assert run(lambda t: loud(t) if t < 1.2 else speech(-30)(t), 10) is None   # a yell is over in a second
    t, why = run(loud, 10, {"earrape_db": -3.5})   # screaming at -9.5: the -9 tone is clearly over it
    assert t < 3.5 and "screaming" in why


def test_loud_is_relative_to_the_call():
    """Discord plays some calls 10+ dB hotter: there, holding the call's own loud level isn't screaming."""
    held = lambda t: tone(-9)(t) * (rng.uniform(0.9, 1.1))
    quiet = run(lambda t: speech(-22)(t) if t < 30 else held(t), 34)
    assert quiet and quiet[0] >= 30 and "screaming" in quiet[1], quiet
    assert run(lambda t: speech(-6)(t) if t < 30 else held(t), 34) is None
    t, why = run(lambda t: speech(-6)(t) if t < 30 else tone(-1)(t), 34)   # really loud still counts
    assert t >= 30 and any(k in why for k in ("screaming", "earrape", "clipping")), why


def test_screaming_is_one_person():
    """Two people taking turns being loud don't add up to one of them screaming."""
    r = SpamRules(dict(DEFAULTS))
    for i in range(int(10 * FPS)):
        t = i / FPS
        u = "ab"[int(t / 1.2) % 2]
        assert r.frame([u], tone(-9)(t), t) == []


def test_sudden_blast():
    def f(t):
        return speech(-32)(t) if t < 8 else tone(-11)(t)
    res = run(f, 10)
    assert res and res[0] >= 8 and "blast" in res[1], res


def test_mic_flapping():
    r = SpamRules(dict(DEFAULTS))
    out = None
    for i in range(30):
        r.speaking("u", True, i * 0.5)
        r.frame(["u"], speech(-20)(0) + 0.1, i * 0.5)  # something audible in the burst
        out = out or r.flaps("u", i * 0.5)
        r.speaking("u", False, i * 0.5 + 0.2)
    assert out and "mic spam" in out
    r2 = SpamRules(dict(DEFAULTS))
    for i in range(20):  # normal: a start every ~1.5 s
        r2.speaking("u", True, i * 1.5)
        assert r2.flaps("u", i * 1.5) is None


def test_flaps_ignore_voice_gating_quiet_taps_and_repeats():
    r = SpamRules(dict(DEFAULTS))
    for i in range(30):  # long bursts, as a voice gate makes them
        r.speaking("u", True, i * 1.2)
        r.speaking("u", True, i * 1.2 + 0.1)   # Discord repeating itself
        assert r.flaps("u", i * 1.2) is None
        r.speaking("u", False, i * 1.2 + 0.9 + (i % 2) * 0.5)
    r = SpamRules(dict(DEFAULTS))
    for i in range(30):  # keyboard clicks only
        r.speaking("u", True, i * 0.4)
        r.frame(["u"], np.full(FRAME, 0.001, np.float32), i * 0.4)
        assert r.flaps("u", i * 0.4) is None
        r.speaking("u", False, i * 0.4 + 0.2)


def test_three_people_flickering_at_once_is_discord_not_spam():
    r = SpamRules(dict(DEFAULTS, flaps_per_10s=5))
    out = []
    for i in range(12):
        for u in "abc":
            r.speaking(u, True, i * 0.5)
            r.frame([u], np.full(FRAME, 0.1, np.float32), i * 0.5)
            out.append(r.flaps(u, i * 0.5))
            r.speaking(u, False, i * 0.5 + 0.2)
    assert not any(out[6:])


def test_music_while_others_talk_is_blamed_on_the_spammer():
    r = SpamRules(dict(DEFAULTS))
    r.speaking("s", True, 0.0)
    hits = []
    for i in range(40 * FPS):
        t = i / FPS
        other = (t % 8) < 5                       # b talks 5 s of every 8, with word gaps
        if other:
            r.speaking("b", True, t) if (t % 8) < 0.05 else None
        elif (t % 8) - 5 < 0.05:
            r.speaking("b", False, t)
        x = tone(-24)(t) + (speech(-22)(t) if other else 0)
        hits += r.frame(["s", "b"] if other else ["s"], x.astype(np.float32), t)
    assert hits and all(u == "s" and "nonstop" in why for u, why in hits)


def test_loud_sound_with_two_new_mics_is_nobodys_fault():
    r = SpamRules(dict(DEFAULTS))
    r.speaking("a", True, 0.0)
    r.speaking("b", True, 0.1)
    assert not any(r.frame(["a", "b"], np.full(FRAME, 0.7, np.float32), i / FPS) for i in range(20))
