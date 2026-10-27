"""Ring + whisper + word list + who-said-it, with espeak standing in for people in a call. No Discord needed: the speaking
events are written straight into Core.talk, and Core.act is replaced by a list."""
import os
import subprocess
import tempfile
import time
import wave

import shutil

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(not shutil.which("espeak-ng"), reason="needs espeak-ng (it stands in for someone talking)")

tmp = tempfile.mkdtemp()
os.environ["DOGMUTE_CONFIG"], os.environ["DOGMUTE_STATE"] = tmp + "/c", tmp + "/s"
from dogmute.core import Core  # noqa: E402
from dogmute.stt import Listener  # noqa: E402
from dogmute.words import WordList  # noqa: E402


def say(text, voice="en"):
    p = tmp + "/say.wav"
    subprocess.run(["espeak-ng", "-v", voice, "-s", "140", "-w", p, text], check=True)
    w = wave.open(p)
    x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768 * 0.5
    n = int(len(x) * 16000 / w.getframerate())
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


@pytest.fixture(scope="module")
def setup():
    from dogmute.core import CONF
    (CONF / "rules.json").unlink(missing_ok=True)
    core = Core()
    core.book.add({"type": "word", "text": "kangaroo"})   # the words come from the rulebook
    core.me = {"id": "me"}
    core.cfg["review"]["enabled"] = False       # the review queue has its own tests (and would pop notifications)
    acts = []
    core.act = lambda uid, why, kind: acts.append((uid, why))
    lis = Listener(core)
    lis._load()
    speech = say("hey guys, did you see that? kangaroo. Okay, let's go.")
    audio = np.concatenate([np.zeros(16000, np.float32), speech, np.zeros(16000, np.float32)])
    T = 1000.0
    for i in range(0, len(audio), 320):
        core.ring.push(audio[i:i + 320], T + (i + 320) / 16000)
    return core, lis, acts, T, len(speech) / 16000


def pass_over(core, lis, T, dur):
    lis.votes.clear()
    t = time.monotonic()
    for e, a, b, _, _ in lis.check(*core.ring.get(T, T + dur + 2), last=True):
        core.heard(e, a, b)
    return time.monotonic() - t


def test_one_speaker_gets_muted(setup):
    core, lis, acts, T, dur = setup
    core.members = {"A": {"name": "troll", "mute": False}, "B": {"name": "friend", "mute": False}}
    core.talk = {"A": [(T + 0.9, T + 1 + dur + 0.3)], "B": [(T - 5, T + 0.2)], "me": [(T + 1, T + 3)]}
    acts.clear()
    dt = pass_over(core, lis, T, dur)
    assert acts == [("A", 'said "k*******"')]
    print(f"pass took {dt * 1000:.0f} ms")


def test_two_talking_and_no_telling_mutes_both(setup):
    core, lis, acts, T, dur = setup
    core.talk = {"A": [(T + 0.9, T + 1 + dur)], "B": [(T + 0.5, T + 1 + dur)]}
    core.cfg["slurs"]["voice"] = False
    acts.clear()
    pass_over(core, lis, T, dur)
    assert sorted(u for u, _ in acts) == ["A", "B"]
    core.book.update("who_unclear", {"action": "ask"})
    acts.clear()
    pass_over(core, lis, T, dur)
    core.book.update("who_unclear", {"action": "mute"})
    assert acts == []
    assert core.log[-1]["action"] == "unclear" and set(core.log[-1]["candidates"]) == {"A", "B"}


def test_muted_people_dont_count(setup):
    core, lis, acts, T, dur = setup
    core.muted_ids.add("B")  # already muted: their audio isn't in the mix
    acts.clear()
    pass_over(core, lis, T, dur)
    assert acts == [("A", 'said "k*******"')]


def test_open_mic_is_background(setup):
    core, lis, acts, T, dur = setup
    core.muted_ids.clear()
    core.talk = {"A": [(T + 0.9, T + 1 + dur)], "B": [(T - 60, None)]}  # B: open mic for a minute
    acts.clear()
    pass_over(core, lis, T, dur)
    assert acts == [("A", 'said "k*******" (the others had open mics)')]


def test_voice_picks_the_speaker(setup):
    core, lis, acts, T, dur = setup
    core.voices.load()
    lines = ["we should push on the left side now", "did anyone see where he went", "I need ammo give me a second"]
    for line in lines:   # A sounds like the espeak default voice, B like a different one
        core.voices.learn("A", say(line))
        core.voices.learn("B", say(line, "en+f2"))
    core.cfg["slurs"]["voice"] = True
    core.talk = {"A": [(T + 0.9, T + 1 + dur)], "B": [(T + 0.5, T + 1 + dur)]}
    acts.clear()
    pass_over(core, lis, T, dur)
    assert len(acts) == 1 and acts[0][0] == "A" and "voice match" in acts[0][1], acts


def test_same_word_isnt_pinned_on_someone_else_after_the_mute(setup):
    core, lis, acts, T, dur = setup
    core.muted_ids.clear()
    core.cfg["slurs"]["voice"] = False
    core.talk = {"A": [(T + 0.9, T + 1 + dur)], "B": [(T - 60, None)]}
    core.muted_ids.add("A")
    core.muted_at["A"] = T + 1 + dur + 0.5     # muted after saying it: the word is still theirs
    acts.clear()
    pass_over(core, lis, T, dur)
    assert acts and acts[0][0] == "A", acts
