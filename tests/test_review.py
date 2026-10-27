"""The review queue with canned audio and a fake notifier: nothing is muted on a doubtful hit, the answer decides, every
case is kept with its clip. The test word is "kangaroo"."""
import datetime
import os
import tempfile
import time
import types
import wave

import numpy as np
import pytest

tmp = tempfile.mkdtemp()
os.environ.setdefault("DOGMUTE_CONFIG", tmp + "/c")
os.environ.setdefault("DOGMUTE_STATE", tmp + "/s")
from dogmute import stt  # noqa: E402
from dogmute.core import Core  # noqa: E402
from dogmute.review import Review  # noqa: E402
from dogmute.spam import DEFAULTS, FPS, FRAME, SpamRules  # noqa: E402
from dogmute.words import WordList  # noqa: E402

T = 1000.0


class Fake:
    def __init__(self):
        self.asked, self.cancelled = [], []

    def ask(self, key, title, body, choices, timeout, done):
        self.asked.append((key, title, choices, done))

    def cancel(self, key):
        self.cancelled.append(key)

    def press(self, n, key):
        self.asked[n][3](key)


@pytest.fixture
def env(tmp_path):
    from dogmute.core import CONF
    (CONF / "rules.json").unlink(missing_ok=True)
    core = Core()
    core.log_path = tmp_path / "mutes.jsonl"
    core.book.add({"type": "word", "text": "kangaroo"})   # the words come from the rulebook
    core.me = {"id": "me"}
    core.cfg["slurs"]["voice"] = False
    core.members = {"A": {"name": "alice", "mute": False}, "B": {"name": "bob", "mute": False}}
    core.talk = {"A": [(T + 0.9, T + 2.0)]}
    acts = []
    core.act = lambda uid, why, kind: acts.append((uid, why, kind))
    core.book.update("who_unclear", {"action": "ask"})     # these test the question
    x = (np.random.default_rng(1).uniform(-0.3, 0.3, 16000 * 6)).astype(np.float32)
    core.ring.push(x, T + 6)
    fake, timers = Fake(), []
    rv = core.review = Review(core, fake)
    rv.later = lambda s, f: f() if s < 10 else timers.append(f)      # clip cuts now; the timeout and the daily prune wait
    return types.SimpleNamespace(core=core, rv=rv, fake=fake, acts=acts, timers=timers, dir=tmp_path / "review")


def items(env):
    return list(env.rv.items.values())


def test_low_p_hit_asks_instead_of_muting(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2, text="hey kangaroo")
    (it,) = items(env)
    assert env.acts == [] and it["kind"] == "word" and it["action"] == "asked" and it["label"] is None
    assert it["masked"] == "k*******" and it["picked"] == "A" and it["candidates"] == [{"uid": "A", "name": "alice"}]
    assert it["p"] == 0.2 and it["transcript"] == "hey kangaroo"
    assert env.fake.asked[0][1] == 'Did alice say "k*******"?'
    w = wave.open(str(env.dir / it["clip"]))
    assert (w.getframerate(), w.getnchannels()) == (16000, 1) and 2.5 < w.getnframes() / 16000 < 3.7    # ~1.5 s each side of 0.6 s


def test_correct_mutes_and_labels(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2)
    env.fake.press(0, "yes")
    it = items(env)[0]
    assert [a[0] for a in env.acts] == ["A"] and it["label"] == "correct" and it["labelled_by"] == "notify" and it["action"] == "muted"
    env.rv.muted("A", "word", "said it (confirmed by you)")        # core._act reports the mute: no second case
    assert len(items(env)) == 1


def test_wrong_does_not_mute(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2)
    env.fake.press(0, "no")
    it = items(env)[0]
    assert env.acts == [] and it["label"] == "wrong" and it["action"] == "not muted"


def test_timeout_leaves_it_unlabelled_and_a_late_answer_only_labels(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2)
    (it,) = items(env)
    env.timers[0]()                                                  # the ask_timeout_s timer
    assert it["label"] is None and it["action"] == "timed out" and env.rv.view()["count"] == 1
    env.rv.label(it["id"], "correct", "page")
    assert env.acts == [] and it["label"] == "correct" and it["action"] == "label only" and env.rv.view()["count"] == 0


def test_page_answer_in_time_mutes(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2)
    env.rv.label(items(env)[0]["id"], "correct", "page")
    assert [a[0] for a in env.acts] == ["A"]
    with pytest.raises(ValueError):
        env.rv.label(items(env)[0]["id"], "maybe")


def test_confident_hit_mutes_at_once_and_keeps_a_case(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.9, text="kangaroo")
    assert [a[0] for a in env.acts] == ["A"] and env.fake.asked == []
    env.rv.muted("A", "word", env.acts[0][1])                       # what core._act does after the mute
    (it,) = items(env)
    assert it["kind"] == "mute" and it["entry"] == "kangaroo" and it["picked"] == "A" and it["label"] is None and it["clip"]
    assert env.rv.view()["count"] == 0                               # not a question


def test_unmute_within_ten_minutes_says_the_mute_was_wrong(env):
    env.rv.muted("A", "noise", "earrape (-3 dBFS)")
    env.rv.muted("B", "noise", "earrape (-3 dBFS)")
    items(env)[1]["t"] = (datetime.datetime.now() - datetime.timedelta(minutes=11)).isoformat(timespec="seconds")
    env.rv.unmuted("A")
    env.rv.unmuted("B")
    a, b = items(env)
    assert (a["label"], a["labelled_by"]) == ("wrong", "unmute") and b["label"] is None


def test_who_picker(env):
    env.core.talk = {"A": [(T + 0.9, T + 2.0)], "B": [(T + 0.5, T + 2.0)]}
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.9)
    (it,) = items(env)
    assert env.acts == [] and it["kind"] == "who" and [c["uid"] for c in it["candidates"]] == ["A", "B"]
    assert env.fake.asked[0][1] == 'Who said "k*******"?' and [c[1] for c in env.fake.asked[0][2]] == ["alice", "bob", "Nobody"]
    env.fake.press(0, "c1")
    assert [a[0] for a in env.acts] == ["B"] and it["label"] == "B"


def test_who_nobody_labels_wrong(env):
    env.core.talk = {"A": [(T + 0.9, T + 2.0)], "B": [(T + 0.5, T + 2.0)]}
    env.core.heard("kangaroo", T + 1, T + 1.6)
    env.fake.press(0, "no")
    assert env.acts == [] and items(env)[0]["label"] == "wrong"
    with pytest.raises(ValueError):
        env.rv.label(items(env)[0]["id"], "correct")                # a who case is answered with a person


def test_prune_by_keep_days(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2)
    env.core.heard("kangaroo", T + 3, T + 3.6, p=0.2)
    old, new = items(env)
    old["t"] = (datetime.datetime.now() - datetime.timedelta(days=31)).isoformat(timespec="seconds")
    env.rv._save()
    (env.dir / "stray.wav").write_bytes(b"x")
    assert env.rv.prune() == 1
    assert [i["id"] for i in items(env)] == [new["id"]] and not (env.dir / old["clip"]).exists() and (env.dir / new["clip"]).exists()
    assert not (env.dir / "stray.wav").exists()
    assert list(Review(env.core, env.fake).items) == [new["id"]]     # and it survives a restart


def test_clips_off_writes_no_audio(env):
    env.core.cfg["review"]["clips"] = False
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.2)
    assert items(env)[0]["clip"] is None and not list(env.dir.glob("*.wav")) and items(env)[0]["transcript"] == ""


def test_questions_are_rate_limited_but_still_queue(env):
    for i in range(5):
        env.core.heard("kangaroo", T + 1 + i * 0.1, T + 1.6, p=0.2)
    assert len(env.fake.asked) == 3 and len(items(env)) == 5 and env.rv.view()["count"] == 5


def test_near_miss_one_speaker_only_and_rare(env):
    env.core.heard("kangaroo", T + 1, T + 1.6, text="kangeroo", near=0.88)
    (it,) = items(env)
    assert it["kind"] == "word" and env.fake.asked[0][1] == 'Did someone say "k*******"?'
    env.fake.press(0, "yes")
    assert [a[0] for a in env.acts] == ["A"]
    env.core.heard("kangaroo", T + 3, T + 3.6, near=0.88)            # right after: dropped
    env.core.talk["B"] = [(T + 0.5, T + 2.0)]
    env.rv.last.clear()
    env.core.heard("kangaroo", T + 1, T + 1.6, near=0.88)            # two talking: not asked
    assert len(items(env)) == 1


def test_off_means_nothing_is_kept(env):
    env.core.cfg["review"]["enabled"] = False
    env.core.heard("kangaroo", T + 1, T + 1.6, p=0.1)
    env.rv.muted("A", "word", "x")
    assert [a[0] for a in env.acts] == ["A"] and items(env) == []


def test_spam_close_asks_and_a_yes_mutes(env):
    r = SpamRules(dict(DEFAULTS))
    r.speaking("u", True, 0.0)
    rng, fired = np.random.default_rng(0), []
    for i in range(40):
        fired += r.frame(["u"], (rng.uniform(-1, 1, FRAME) * 10 ** (-7.5 / 20) * np.sqrt(3)).astype(np.float32), i / FPS)
    close = r.pop_close()
    assert fired == [] and close and close[0][0] == "u" and close[0][1].startswith("earrape") and r.pop_close() == []
    env.rv.ask_spam("A", close[0][1], T + 2)
    env.rv.ask_spam("A", close[0][1], T + 2.1)                        # same person within a minute: once
    assert len(items(env)) == 1 and env.fake.asked[0][1].startswith("Is alice mic-spamming (earrape")
    env.fake.press(0, "yes")
    assert env.acts[0][0] == "A" and env.acts[0][2] == "noise" and items(env)[0]["label"] == "correct"


def test_listener_reports_probability_and_near_misses():
    core = types.SimpleNamespace(words=WordList("kangaroo"), setup_test={"on": False})
    lis = stt.Listener(core)
    n = time.monotonic()
    lis.plain = [("so", n, n + 0.3), ("kangeroo", n + 0.4, n + 1.0), ("yeah", n + 1.1, n + 1.4)]
    lis.plain_probs = [(n, n + 0.3, 0.99), (n + 0.4, n + 1.0, 0.31), (n + 1.1, n + 1.4, 0.9)]
    assert lis.p_over(n + 0.4, n + 1.0) == 0.31 and lis.p_over(n + 5, n + 6) is None
    lis.found = []
    (m,) = lis.misses()
    assert m[0] == "kangaroo" and (m[1], m[2]) == (n + 0.4, n + 1.0) and m[3] >= 0.75
    assert lis.misses() == []                                         # the same one isn't reported again
    lis.found = [("kangaroo", n + 0.4, n + 1.0)]
    lis.recent_near.clear()
    assert lis.misses() == []                                         # it was a hit


def test_short_words_one_letter_off_are_not_near_misses():
    core = types.SimpleNamespace(words=WordList("gook\ncoon\nkangaroo"), setup_test={"on": False})
    lis = stt.Listener(core)
    n = time.monotonic()
    lis.plain = [("good", n, n + 0.3), ("cool", n + 0.4, n + 0.7), ("kangeroo", n + 0.8, n + 1.4)]
    lis.found = []
    assert [m[0] for m in lis.misses()] == ["kangaroo"]
