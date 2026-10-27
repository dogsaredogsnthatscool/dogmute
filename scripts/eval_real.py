"""Word catch rate on real call audio: each clip goes through the real pass schedule (stt.Passes on a simulated clock,
a Ring, Listener.check with the chosen model) as one person talking, scored against labels a person gave.

  python scripts/eval_real.py DIR                         # DIR/*.wav + DIR/labels.jsonl {"clip": "x.wav", "said": true}
  python scripts/eval_real.py DIR --model distil-large-v3 # another model (any faster-whisper name)
  python scripts/eval_real.py DIR --silver                # no labels yet: write labels.silver.jsonl from full large-v3
                                                          # (beam 5, whole clip) and score against that. Silver is a
                                                          # stronger model's opinion, not the truth: listen and fix it.
"said" = a word on your list (~/.config/dogmute/words.txt) is spoken anywhere in the clip. Words never print in clear."""
import argparse
import json
import sys
import time
import types
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dogmute import stt  # noqa: E402
from dogmute.audio import RATE, Ring  # noqa: E402
from dogmute.core import CONF, HANG, default_words  # noqa: E402
from dogmute.words import WordList, masked  # noqa: E402


def load(f):
    w = wave.open(str(f))
    assert w.getframerate() == RATE and w.getnchannels() == 1, f"{f}: needs 16 kHz mono"
    return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def silver(clips, words, out):
    from faster_whisper import WhisperModel
    m = WhisperModel("large-v3", device="cuda", compute_type="int8_float16")
    with open(out, "w") as f:
        for c in clips:
            segs, _ = m.transcribe(load(c), language="en", beam_size=5, word_timestamps=True, condition_on_previous_text=False)
            ws = [(w.word, w.start, w.end) for s in segs for w in s.words or []]
            f.write(json.dumps({"clip": c.name, "said": bool(words.find(ws)), "by": "silver:large-v3"}) + "\n")
    del m


def replay(lis, x):
    """Hits [(entry, a, b, p, text)] for clip x heard as one person talking over all of it."""
    T, dur = 1000.0, len(x) / RATE
    ring = Ring(seconds=int(dur) + 8)
    ring.push(x, T + dur)
    lis.votes.clear()
    sched, now, out = stt.Passes(), T, []
    ms = []
    while now < T + dur + 1.0:
        now += 0.1
        talking = T + 0.3 if now < T + dur - 0.3 + HANG else None
        seg = sched.seg_start
        p = sched.step(now, talking)
        if p is None:
            continue
        last = sched.seg_start is None
        a, ts = ring.get(p[0], min(p[1], T + dur))
        if len(a) < RATE * 0.3:
            out += lis.flush() if last else []
            continue
        out += lis.check(a, ts, last, clipped=seg is not None and ts > seg + 0.05)
        ms.append(lis.latency_ms)
        now += lis.latency_ms / 1000
    return out + lis.flush(), ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--silver", action="store_true")
    ap.add_argument("--word-p", type=float, default=0.5, help="below this p a hit is asked about, not muted")
    ap.add_argument("-v", action="store_true", help="list every miss and false hit (masked)")
    a = ap.parse_args()
    d = Path(a.dir)
    lp = CONF / "words.txt"
    words = WordList(lp.read_text() if lp.exists() else default_words())
    clips = sorted(d.glob("*.wav")) or sorted((d / "clips").glob("*.wav"))
    labels_f = d / "labels.jsonl"
    if a.silver:
        labels_f = d / "labels.silver.jsonl"
        if not labels_f.exists():
            silver(clips, words, labels_f)
        print("SILVER labels (large-v3's opinion, not checked by a person)")
    labels = {r["clip"]: r["said"] for r in map(json.loads, labels_f.read_text().splitlines())}
    clips = [c for c in clips if c.name in labels]
    core = types.SimpleNamespace(words=words, setup_test={"on": False},
                                 cfg={"stt": {"model": a.model, "threads": 4, "device": "auto"}})
    lis = stt.Listener(core)
    lis._load()
    print("model:", lis.status, "beam", lis.beam, "| clips:", len(clips), "said:", sum(labels[c.name] for c in clips))
    n = {"caught": 0, "asked": 0, "missed": 0, "false mute": 0, "false ask": 0, "clean": 0}
    ms = []
    for c in clips:
        hits, m = replay(lis, load(c))
        ms += m
        mute = any(h[3] is None or h[3] >= a.word_p for h in hits)
        if labels[c.name]:
            k = "caught" if mute else "asked" if hits else "missed"
        else:
            k = "false mute" if mute else "false ask" if hits else "clean"
        n[k] += 1
        if a.v and k in ("missed", "asked", "false mute", "false ask"):
            print(f"  {k:10} {c.name} {[(masked(h[0]), round(h[3] or 0, 2)) for h in hits]}")
    said = max(1, sum(labels[c.name] for c in clips))
    print(n, f"| catch {n['caught'] / said:.2f} (+asked {n['asked'] / said:.2f})",
          f"| ms/pass p50 {np.percentile(ms, 50):.0f} p90 {np.percentile(ms, 90):.0f}" if ms else "")


if __name__ == "__main__":
    main()
