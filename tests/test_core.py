"""Who counts as talking (members only, people who leave or mute stop), the audio/speaking-event lag, and the ring's
clock across stalls. No Discord, no whisper: events go straight into Core.on_event, audio into Core.on_block."""
import os
import tempfile

import numpy as np
import pytest

tmp = tempfile.mkdtemp()
os.environ.setdefault("DOGMUTE_CONFIG", tmp + "/c")
os.environ.setdefault("DOGMUTE_STATE", tmp + "/s")
from dogmute import core as C  # noqa: E402
from dogmute.audio import RATE, Ring  # noqa: E402
from dogmute.spam import DEFAULTS, FPS, FRAME, SpamRules  # noqa: E402


@pytest.fixture
def core(monkeypatch):
    (C.CONF / "rules.json").unlink(missing_ok=True)   # every test starts from the built-in rules
    c = C.Core()
    c.me = {"id": "me"}
    c.cfg["review"]["enabled"] = False
    c.acts = []
    c.act = lambda uid, why, kind: c.acts.append((uid, why))
    clock = [1000.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    c.clock = clock
    c.members = {"A": {"name": "a", "mute": False}, "B": {"name": "b", "mute": False}}
    return c


def ev(c, evt, t, uid=None, **d):
    c.clock[0] = t
    c.on_event(evt, {"user_id": uid, **d} if uid else d)


def test_someone_not_in_the_channel_never_counts(core):
    ev(core, "SPEAKING_START", 1000.0, "GHOST")          # left before we joined, or a stale STOP never came
    ev(core, "SPEAKING_START", 1001.0, "A")
    assert core.on_now(1002.0) == ["A"] and core.solo(1002.0) == "A"
    assert core.talk_started(1002.0) == 1001.0
    assert core.pick(1001.5, 1001.8)[0] == "A"


def test_leaving_mid_sentence_stops_their_talk(core):
    ev(core, "SPEAKING_START", 1000.0, "A")
    ev(core, "SPEAKING_START", 1000.5, "B")
    ev(core, "VOICE_STATE_DELETE", 1001.0, user={"id": "B"})
    assert core.on_now(1005.0) == ["A"] and "B" not in core.members
    assert core.rules.who["B"].on_since is None


def test_muting_themselves_stops_their_talk(core):
    ev(core, "SPEAKING_START", 1000.0, "B")
    ev(core, "VOICE_STATE_UPDATE", 1001.0, user={"id": "B"}, voice_state={"self_mute": True})
    assert core.on_now(1005.0) == []


def test_lag_is_measured_from_onsets_out_of_silence(core):
    """Speech reaches the mix 0.25 s after Discord says someone started: the lag settles there."""
    t = 1000.0
    core.ring.push(np.zeros(RATE, np.float32), t)       # 1 s of silence anchors the clock
    for k in range(4):
        start = t + 1.0
        ev(core, "SPEAKING_START", start, "A")
        x = np.zeros(int(3.0 * RATE), np.float32)
        on = int((1.0 + 0.25) * RATE)                    # the block covers t..t+3, speech from start + 0.25
        x[on:on + RATE] = 0.3 * np.sin(np.arange(RATE) * 2 * np.pi * 200 / RATE)
        for i in range(0, len(x), 320):
            core.on_block(x[i:i + 320], t + (i + 320) / RATE)
        ev(core, "SPEAKING_STOP", start + 1.3, "A")
        t += 3.0
    assert len(core._lags) >= 3 and abs(core.lag - 0.25) < 0.06


def test_ring_fills_a_stall_with_silence_and_drops_nan():
    r = Ring(seconds=10)
    r.push(np.ones(RATE, np.float32) * 0.1, 101.0)      # 100..101
    x = np.ones(RATE, np.float32) * 0.2
    x[5] = np.nan
    r.push(x, 104.0)                                     # nothing came for 2 s, then 103..104
    a, ts = r.get(103.0, 104.0)
    assert abs(ts - 103.0) < 0.01 and np.isfinite(a).all() and abs(float(np.median(a)) - 0.2) < 1e-6
    gap, _ = r.get(101.2, 102.8)
    assert np.all(gap == 0)


def test_two_open_mics_dont_both_get_the_mix_blamed():
    """A steady tone in the mix with two mics open nonstop: it can't be told whose, so nobody is muted."""
    r = SpamRules(dict(DEFAULTS))
    for u in ("A", "B"):
        r.speaking(u, True, 0.0)
    tone = (0.3 * np.sin(np.arange(FRAME) * 2 * np.pi * 1000 / RATE)).astype(np.float32)
    out = []
    for i in range(FPS * 15):
        out += r.frame(["A", "B"], tone, i / FPS)
    assert out == []
    r = SpamRules(dict(DEFAULTS))
    r.speaking("A", True, 0.0)
    out = []
    for i in range(FPS * 15):
        out += r.frame(["A"], tone, i / FPS)
    assert out and out[0][0] == "A"


def test_several_talking_and_no_telling_who_mutes_them_all_but_friends(core):
    core.members["F"] = {"name": "f", "mute": False}
    core.cfg["friends"] = {"F": "f"}
    core.cfg["slurs"]["voice"] = False
    for u, s in (("A", 1000.0), ("B", 1000.2), ("F", 1000.1)):
        ev(core, "SPEAKING_START", s, u)
    core.clock[0] = 1003.0
    calls = []
    core.act = lambda uid, why, kind: calls.append(uid)
    core.heard("kangaroo", 1001.0, 1001.4, 0.95, "kangaroo")
    assert sorted(calls) == ["A", "B"]
    core.book.update("who_unclear", {"action": "ask"})
    calls.clear()
    core.heard("kangaroo", 1001.0, 1001.4, 0.95, "kangaroo")
    assert calls == [] and core.log[-1]["action"] == "unclear"


def test_only_the_one_on_during_the_word_gets_it(core):
    """B stopped 0.3 s before the word (inside the 0.5 s slack): with the lag known, the word is A's."""
    ev(core, "SPEAKING_START", 1000.0, "A")
    ev(core, "SPEAKING_START", 1000.0, "B")
    ev(core, "SPEAKING_STOP", 1001.2, "B")
    core.clock[0] = 1003.0
    assert core.pick(1001.5, 1001.9)[0] == "A"


def test_every_hit_mutes_even_right_after_an_unmute(core):
    """Two people saying the same word seconds apart both get muted; unmuting someone doesn't buy them 10 s."""
    import asyncio
    core.cfg["test_mode"] = False
    sent = []

    async def set_mute(uid, on):
        sent.append((uid, on))
        (core.muted_ids.add if on else core.muted_ids.discard)(uid)
    core.set_mute = set_mute
    core.review.muted = lambda *a: None

    async def go():
        await core._act("A", 'said "k*******"', "word")
        await core._act("B", 'said "k*******"', "word")   # same word, another person, at once
        await core._act("A", 'said "k*******"', "word")   # already muted: nothing sent
        core.muted_ids.discard("A")                        # you unmuted them
        await core._act("A", 'said "k*******"', "word")
    asyncio.run(go())
    assert sent == [("A", True), ("B", True), ("A", True)]
