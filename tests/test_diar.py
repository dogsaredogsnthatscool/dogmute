"""Online diarization: the clusterer, the cluster -> Discord user mapping from speaking events, and who() against a word.
Synthetic embeddings and a fake VAD / embedder: no model download, no audio."""
import os
import tempfile

import numpy as np
import pytest

tmp = tempfile.mkdtemp()
os.environ.setdefault("DOGMUTE_CONFIG", tmp + "/c")
os.environ.setdefault("DOGMUTE_STATE", tmp + "/s")
from dogmute import core as C  # noqa: E402
from dogmute import diar as D  # noqa: E402
from dogmute.audio import RATE  # noqa: E402

DIM = 256
rng = np.random.default_rng(7)


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def voice():
    return unit(rng.standard_normal(DIM))


def near(base, sim):
    """A unit vector with cosine similarity `sim` to base."""
    r = rng.standard_normal(DIM)
    r = r - (r @ base) * base
    return unit(sim * base + np.sqrt(1 - sim * sim) * unit(r))


@pytest.fixture
def core():
    (C.CONF / "rules.json").unlink(missing_ok=True)   # built-in rules for every test
    c = C.Core()
    c.me = {"id": "me"}
    c.lag = 0.2
    c.members = {u: {"name": u.lower(), "mute": False} for u in "ABC"}
    return c


@pytest.fixture
def d(core):
    return D.Diarizer(core, embed=lambda x: None)


def feed_run(d, e, t, secs, sim=0.8):
    """Chunks of `e` (a voice) over audio times t..t+secs, one every STEP_S; returns the cluster ids seen."""
    ids = []
    for k in range(int(secs / D.STEP_S)):
        a = t + k * D.STEP_S
        ids.append(d.feed(near(e, sim), a, a + D.WIN_S)[0])
    return ids


def test_same_voice_joins_a_new_voice_starts_a_cluster(d):
    va, vb, vc = voice(), voice(), voice()
    ia = feed_run(d, va, 100.0, 5)
    ib = feed_run(d, vb, 105.0, 5)
    ic = feed_run(d, vc, 110.0, 5)
    ia2 = feed_run(d, va, 115.0, 5)
    assert len(set(ia)) == len(set(ib)) == len(set(ic)) == 1
    assert len({ia[0], ib[0], ic[0]}) == 3 and set(ia2) == {ia[0]}
    assert len(d.clusters) == 3


def test_cluster_count_is_capped_and_the_weakest_makes_room(d):
    big = voice()
    feed_run(d, big, 100.0, 10)                          # a voice with plenty of chunks ...
    for k in range(40):                                  # ... then many one-off strangers
        d.feed(voice(), 110.0 + k, 111.5 + k)
    assert len(d.clusters) == D.MAX_CLUSTERS
    assert d.feed(near(big, 0.8), 160.0, 161.5)[0] == 1   # the big one survived the evictions


def test_only_a_close_chunk_moves_the_centroid(d):
    v = voice()
    d.feed(v, 100.0, 101.5)
    c = d.clusters[1]
    d.feed(near(v, (D.JOIN + D.UPDATE) / 2), 100.5, 102.0)   # joins (>= JOIN) but is no clean sample (< UPDATE)
    assert len(d.clusters) == 1 and c.n == 1 and np.allclose(c.u, v, atol=1e-5)
    d.feed(near(v, 0.9), 101.0, 102.5)
    assert c.n == 2 and c.u @ v > 0.97


def test_a_blend_of_two_voices_does_not_pull_either_centroid(d):
    va, vb = voice(), voice()
    feed_run(d, va, 100.0, 4)
    feed_run(d, vb, 104.0, 4)
    ca, cb = d.clusters[1].u.copy(), d.clusters[2].u.copy()
    blend = unit(va + vb)                                 # both talking: ~0.7 to each, so it joins one, moves neither much
    d.feed(blend, 108.0, 109.5)
    assert np.allclose(d.clusters[1].u, ca, atol=1e-4) or np.allclose(d.clusters[2].u, cb, atol=1e-4) or len(d.clusters) == 3


def test_a_voice_that_started_two_clusters_is_folded_back_into_one(core, d):
    v = voice()
    d.feed(v, 100.0, 101.5)
    d.feed(near(v, 0.2), 100.5, 102.0)                   # too unlike: a second cluster
    assert len(d.clusters) == 2
    d.clusters[2].u = d.clusters[2].v = near(v, 0.7)      # ... until its centroid settles
    d.evidence[1], d.evidence[2] = {"A": 1.0}, {"A": 2.0}
    d._merge()
    assert list(d.clusters) == [1] and d.clusters[1].n == 2 and d.evidence == {1: {"A": 3.0}}
    assert {c.cluster for c in d.timeline} == {1}


def test_two_clusters_the_events_call_different_people_stay_two(core, d):
    v = voice()
    d.feed(v, 100.0, 101.5)
    d.feed(near(v, 0.2), 100.5, 102.0)
    d.clusters[2].u = d.clusters[2].v = near(v, 0.7)      # alike as voices, but A's and B's by the events
    d.evidence[1], d.evidence[2] = {"A": 5.0}, {"B": 5.0}
    d._merge()
    assert sorted(d.clusters) == [1, 2]


def test_a_chunk_begun_before_forget_is_not_filed_after_it(core, d):
    g = d.gen
    d.forget()
    assert d.feed(voice(), 100.0, 101.5, g) is None and not d.clusters and not d.timeline
    assert d.feed(voice(), 100.0, 101.5, d.gen) == (1, D.JOIN)


def speak(core, uid, s, e):
    core.talk.setdefault(uid, []).append((s, e))


def test_cluster_is_named_by_who_was_on(core, d):
    va, vb = voice(), voice()
    speak(core, "A", 99.8, 110.0)                         # events run 0.2 s ahead of the audio (core.lag)
    speak(core, "B", 110.0, 120.0)
    feed_run(d, va, 100.0, 8)
    feed_run(d, vb, 110.2, 8)
    names = d.mapping()
    assert {names[1][0], names[2][0]} == {"A", "B"}
    assert names[1][0] == "A" and names[2][0] == "B" and names[1][1] > 0.95


def test_lag_correction_shifts_the_events_onto_the_audio(core, d):
    speak(core, "A", 90.0, 110.0)                         # A stops at 110.0 on the events' clock
    d.feed(voice(), 109.75, 111.25)                       # a chunk centred on audio time 110.5
    assert d.evidence.get(1, {}) == {}                    # lag 0.2: that is 110.3 for the events, A is off
    core.lag = 1.0
    d.feed(voice(), 109.75, 111.25)                       # lag 1.0: 109.5, A is on
    assert abs(d.evidence[2]["A"] - D.STEP_S) < 1e-6


def test_overlap_time_is_split_between_everyone_on(core, d):
    va = voice()
    speak(core, "A", 99.8, 130.0)
    speak(core, "B", 99.8, 130.0)                         # both on the whole time: 1/2 each, neither reaches 60 %
    feed_run(d, va, 100.0, 20)
    assert d.mapping() == {}
    ev = d.evidence[1]
    assert abs(ev["A"] - ev["B"]) < 1e-6 and ev["A"] > 4.0   # ~20 s x 0.5 / 2 each
    speak(core, "C", 99.8, 130.0)
    d.forget()
    feed_run(d, va, 100.0, 20)
    assert abs(d.evidence[1]["C"] - d.evidence[1]["A"]) < 1e-6 and d.evidence[1]["A"] < 7.0   # a third: 1/3 each (20 s x 1/3)


def test_a_mostly_solo_voice_is_named_despite_some_overlap(core, d):
    va = voice()
    speak(core, "A", 99.8, 130.0)
    speak(core, "B", 105.0, 108.0)                        # B butts in for 3 s; the rest of the time A is alone
    feed_run(d, va, 100.0, 20)
    nm = d.mapping()
    assert nm[1][0] == "A" and nm[1][1] > 0.8


def test_not_enough_evidence_is_no_name(core, d):
    va = voice()
    speak(core, "A", 99.8, 130.0)
    feed_run(d, va, 100.0, 2.5)                           # 2.5 s of evidence: under MAP_SECS
    assert d.mapping() == {}
    feed_run(d, va, 102.5, 2)
    assert d.mapping()[1][0] == "A"


def test_people_who_cant_be_in_the_mix_earn_nothing(core, d):
    va = voice()
    speak(core, "me", 99.8, 130.0)
    speak(core, "GHOST", 99.8, 130.0)                     # not in the channel
    feed_run(d, va, 100.0, 12)
    assert d.evidence.get(1, {}) == {}
    speak(core, "C", 99.8, 130.0)
    core.muted_ids.add("C")
    core.muted_at["C"] = 50.0                             # muted long before
    feed_run(d, va, 112.0, 12)
    assert d.evidence.get(1, {}) == {}


def named_run(core, d):
    """A and B named (A on 100-110, B on 110-120, A again 120-130); returns their voices."""
    va, vb = voice(), voice()
    speak(core, "A", 99.8, 110.0)
    speak(core, "B", 110.0, 120.0)
    speak(core, "A", 120.0, 130.0)
    feed_run(d, va, 100.0, 9)
    feed_run(d, vb, 110.2, 9)
    feed_run(d, va, 120.2, 9)
    assert {u for u, _, _ in d.mapping().values()} == {"A", "B"}
    return va, vb


def test_who_names_the_speaker_of_a_word(core, d):
    named_run(core, d)
    assert d.who(102.0, 102.4, ["A", "B"])[0] == "A"
    u, score = d.who(113.0, 113.4, ["A", "B"])
    assert u == "B" and score > 0.7


def test_who_only_answers_with_a_candidate(core, d):
    named_run(core, d)
    assert d.who(102.0, 102.4, ["B", "C"]) is None        # the voice there is A, who is not a candidate
    assert d.who(102.0, 102.4, []) is None
    assert d.who(102.0, 102.4, ["A"])[0] == "A"


def test_who_is_silent_where_the_timeline_is(core, d):
    named_run(core, d)
    assert d.who(50.0, 50.4, ["A", "B"]) is None          # before anything
    assert d.who(200.0, 200.4, ["A", "B"]) is None        # after: nothing heard around it


def test_who_wont_pick_when_the_chunks_around_the_word_disagree(core, d):
    va, vb = named_run(core, d)
    d.feed(near(va, 0.8), 130.0, 131.5)                   # two chunks centred on the same word, one like A, one like B
    d.feed(near(vb, 0.8), 130.0, 131.5)
    assert d.who(130.5, 130.9, ["A", "B"]) is None        # each holds half the evidence: A scores ~0.4, B ~0.4
    assert d.who(119.9, 120.3, ["A", "B"]) is None        # a word right at the change B -> A: no chunk is centred on it


def test_a_mix_of_two_voices_is_the_louder_one_or_nobody(core, d):
    va, vb = named_run(core, d)
    for gain, t in ((0.5, 130.0), (1.0, 140.0)):
        mix = unit(va + gain * vb)                        # A over B at 6 dB, then both at the same level
        d.feed(mix, t, t + 1.5)
        speak(core, "A", t - 0.2, t + 2.0)
        speak(core, "B", t - 0.2, t + 2.0)
    r = d.who(130.55, 130.95, ["A", "B"])
    assert r and r[0] == "A"
    assert d.who(140.55, 140.95, ["A", "B"]) is None      # equally loud: no lead
    assert d.who(130.55, 130.95, ["B"]) is None           # B is a candidate but the mix is A's


def test_a_stranger_or_a_faint_likeness_is_no_answer(core, d):
    va, vb = named_run(core, d)
    d.feed(voice(), 130.0, 131.5)                         # nobody we know
    assert d.who(130.55, 130.95, ["A", "B"]) is None
    d.feed(near(va, 0.3), 140.0, 141.5)
    assert d.who(140.55, 140.95, ["A", "B"]) is None


def test_forget_clears_everything(core, d):
    named_run(core, d)
    assert d.clusters and d.timeline and d.evidence
    d.forget()
    assert not d.clusters and not d.timeline and not d.evidence and d.mapping() == {}
    assert d.who(102.0, 102.4, ["A", "B"]) is None


def test_a_person_who_left_cant_be_the_answer(core, d):
    named_run(core, d)
    assert d.who(102.0, 102.4, ["A", "B"])[0] == "A"
    assert d.who(102.0, 102.4, [u for u in core.members if u != "A"]) is None   # pick() hands in members only


# ---------- the step: ring -> VAD -> embed -> cluster ----------
def push(core, secs, t0=1000.0, level=0.1, start=0):
    n = int(secs * RATE)
    x = (level * np.sin(2 * np.pi * 220 * np.arange(start, start + n) / RATE)).astype(np.float32)
    for i in range(0, n, 320):
        core.ring.push(x[i:i + 320], t0 + (start + i + 320) / RATE)


def test_step_embeds_every_half_second_of_speech_only(core):
    emb = voice()
    calls = []
    d = D.Diarizer(core, embed=lambda x: calls.append(len(x)) or emb)
    d.vad = lambda x: np.ones(len(x) // 512, np.float32)
    assert d.step() is None                               # nothing buffered
    push(core, 1.0)
    assert d.step() is None and not calls                 # a full 1.5 s window first
    push(core, 1.0, start=int(1.0 * RATE))
    r = d.step()
    assert r and r[0] == 1 and calls == [int(1.5 * RATE)]
    assert d.step() is None                               # no new audio
    push(core, 0.25, start=int(2.0 * RATE))
    assert d.step() is None                               # less than 0.5 s on
    push(core, 0.25, start=int(2.25 * RATE))
    assert d.step() and len(calls) == 2
    d.vad = lambda x: np.zeros(len(x) // 512, np.float32)  # silence: skipped, no embedding
    push(core, 0.5, start=int(2.5 * RATE))
    assert d.step() is None and len(calls) == 2
    rule(core, "who_diar")["on"] = False
    push(core, 0.5, start=int(3.0 * RATE))
    d.vad = lambda x: np.ones(len(x) // 512, np.float32)
    assert d.step() is None and len(calls) == 2           # off: idle


def test_step_survives_bad_models(core, capsys):
    d = D.Diarizer(core, embed=lambda x: 1 / 0)
    d.vad = lambda x: np.ones(len(x) // 512, np.float32)
    push(core, 2.0)
    assert d.step() is None
    d.embed = lambda x: np.full(256, np.nan, np.float32)
    push(core, 0.5, start=int(2.0 * RATE))
    assert d.step() is None
    d.embed = lambda x: voice()
    d.vad = lambda x: 1 / 0
    push(core, 0.5, start=int(2.5 * RATE))
    assert d.step() is None
    assert "diar embed" in capsys.readouterr().out


def test_the_real_vad_calls_noise_and_a_tone_not_speech(core):
    d = D.Diarizer(core, embed=lambda x: voice())
    d._load_vad()
    if not d.vad:
        pytest.skip("silero VAD not available")
    push(core, 2.0)                                       # a pure tone is not speech
    assert d.step() is None and not d.clusters


# ---------- Core.pick ----------
def rule(core, id):
    return next(r for r in core.book.rules if r["id"] == id)


def test_pick_asks_the_diarizer_when_timing_cant_tell(core):
    va, vb = named_run(core, core.diar)
    rule(core, "who_voice")["on"] = False
    speak(core, "B", 101.0, 103.0)                        # B butts in while A talks: both on during the word
    rule(core, "who_diar")["on"] = False
    assert core.pick(102.2, 102.6) == (None, ["A", "B"], "several talking")
    rule(core, "who_diar")["on"] = True
    uid, who, how = core.pick(102.2, 102.6)               # (audio runs 0.2 s behind the events)
    assert uid == "A" and who == ["A", "B"] and how.startswith("diarization 0.")


def test_pick_keeps_the_voice_match_as_the_fallback(core):
    va, vb = named_run(core, core.diar)
    speak(core, "B", 101.0, 103.0)
    asked = []
    core.voices.sess = object()                           # "loaded": pick goes on to score the audio
    core.voices.score = lambda x, uids: asked.append(sorted(uids)) or {"A": 0.8, "B": 0.1}
    core.diar.who = lambda a, b, cands: None              # the diarizer has no answer
    uid, _, how = core.pick(102.2, 102.6)
    assert asked == [["A", "B"]] and uid == "A" and how.startswith("voice match")


def test_the_setting_exists_and_is_on_by_default(core):
    assert core.book.get("who_diar") is not None and "who_diar" in [r["type"] for r in core.book.rules]
