"""STT scheduling and the votes of overlapping passes, with whisper replaced by canned transcripts (no model, no GPU)."""
import types

from dogmute import stt
from dogmute.words import WordList


def listener(*passes):
    """A Listener whose whisper returns the given transcripts in turn: [(word, start, end, p)] relative to the window."""
    core = types.SimpleNamespace(words=WordList("kangaroo"), setup_test={"on": False})
    lis = stt.Listener(core)
    left = list(passes)

    def fake(x, t0):
        w = left.pop(0) if len(left) > 1 else left[0]
        lis.probs = [(t0 + a, t0 + b, p) for _, a, b, p in w]
        return [(t, t0 + a, t0 + b) for t, a, b, _ in w]
    lis.transcribe = fake
    return lis


X = [0.0] * 32000   # 2 s
HIT = [("kangaroo", 0.1, 0.6, 0.6)]          # unsure: waits for another pass
SURE = [("kangaroo", 0.1, 0.6, 0.95)]
SAID_ELSE = [("can", 0.1, 0.3, 0.9), ("go", 0.35, 0.6, 0.9)]


def test_passes_step_and_end():
    p = stt.Passes()
    assert p.step(100.0, None) is None
    assert p.step(101.0, 100.5) == (100.2, 101.0)       # talk starts: a pass from 0.3 s before it
    assert p.step(101.5, 100.5) is None                 # not yet a step later
    assert p.step(102.0, 100.5) == (100.2, 102.0)
    assert p.step(110.0, 100.5) == (104.0, 110.0)       # window is 6 s
    assert p.step(110.2, None) == (104.2, 110.2)        # talk ended: one last pass right away
    assert p.step(110.4, None) is None


def test_the_last_pass_over_finished_talk_counts_alone():
    lis = listener(HIT)
    assert [h[:4] for h in lis.check(X, 0.0, last=True)] == [("kangaroo", 0.1, 0.6, 0.6)]


def test_one_sure_pass_counts_at_once():
    lis = listener(SURE)
    assert [h[:4] for h in lis.check(X, 0.0)] == [("kangaroo", 0.1, 0.6, 0.95)]
    assert lis.check(X, 0.0) == []                                   # decided once


def test_two_passes_that_agree_count():
    lis = listener(HIT)
    assert lis.check(X, 0.0) == []                                   # one vote
    hits = lis.check(X, 0.0)
    assert [h[:4] for h in hits] == [("kangaroo", 0.1, 0.6, 0.6)] and hits[0][4] == "kangaroo"
    assert lis.check(X, 0.0, last=True) == []                        # decided once


def test_two_passes_that_heard_something_else_drop_it():
    lis = listener(HIT, SAID_ELSE, SAID_ELSE)
    assert lis.check(X, 0.0) == [] and lis.check(X, 0.0) == [] and lis.check(X, 0.0) == []
    assert lis.check(X, 0.0, last=True) == [] and lis.flush() == []


def test_a_tie_when_the_talk_ends_is_only_asked():
    """whisper flips on sloppy speech between windows: one yes and one no is p 0 (asked about, never muted)."""
    lis = listener(HIT, SAID_ELSE)
    assert lis.check(X, 0.0) == []
    assert [h[:4] for h in lis.check(X, 0.0, last=True)] == [("kangaroo", 0.1, 0.6, 0.0)]


def test_a_different_list_word_on_the_same_spot_agrees():
    lis = stt.Listener(types.SimpleNamespace(words=WordList("kangaroo\nwallaby"), setup_test={"on": False}))
    left = [[("kangaroo", 0.1, 0.6, 0.5)], [("wallaby", 0.12, 0.62, 0.8)]]

    def fake(x, t0):
        w = left.pop(0)
        lis.probs = [(t0 + a, t0 + b, p) for _, a, b, p in w]
        return [(t, t0 + a, t0 + b) for t, a, b, _ in w]
    lis.transcribe = fake
    assert lis.check(X, 0.0) == []
    assert [h[0] for h in lis.check(X, 0.0)] == ["wallaby"]          # its surer reading


def test_a_word_still_being_said_gets_no_vote():
    lis = listener([("kangaroo", 1.5, 1.9, 0.9)])                    # ends 0.1 s before the window does
    assert lis.check(X, 0.0) == [] and lis.votes[0]["yes"] == 0
    assert [h[0] for h in lis.check(X, 0.0, last=True)] == ["kangaroo"]


def test_a_clipped_window_ignores_its_cut_off_first_word():
    lis = listener([("kangaroo", 0.1, 0.6, 0.9)])
    assert lis.check(X, 0.0, last=True, clipped=True) == [] and lis.votes == []


def test_flush_reports_what_the_votes_say():
    lis = listener(HIT)
    assert lis.check(X, 0.0) == [] and [h[:4] for h in lis.flush()] == [("kangaroo", 0.1, 0.6, 0.6)]


def test_made_up_segments():
    assert stt.made_up(2.6, 0.0, -0.2) and stt.made_up(1.2, 0.8, -1.4)
    assert not stt.made_up(1.2, 0.8, -0.3) and not stt.made_up(1.5, 0.1, -1.4)
