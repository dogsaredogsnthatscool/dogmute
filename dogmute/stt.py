"""Offline speech-to-text on the call mix: faster-whisper (NVIDIA GPU or CPU int8), or mlx-whisper on an Apple Silicon
Mac's GPU. Audio stays in memory; text is only checked against the word list and then dropped."""
import importlib.util
from difflib import SequenceMatcher
import platform
import sys
import threading
import time

import numpy as np

from .audio import RATE
from .words import FUZZ_SINGLE, WordList, clean

NEAR_MIN = 0.75   # a near-miss at least this similar to a list word goes to the review queue (see review.py)
NEAR_SHORT = 0.95  # ... for words under FUZZ_SINGLE letters: one letter off them is an everyday word (good, cool, like)
TEST_LIST = WordList("kangaroo")   # the setup wizard's check word (core.TEST_WORD)

WINDOW_S = 6.0   # look back this far each pass
STEP_S = 1.0     # pass every this often while someone talks
EDGE_S = 0.4     # a clipped window's first words are cut off: hits starting this close to its start don't count
TAIL_S = 0.3     # a pass only votes on a hit that ended at least this long before its end (the word may still be going)
# the rest of how a word counts is in the rulebook (count_sure, count_agree, count_doubt, count_near)
MLX_REPOS = {"tiny.en": "mlx-community/whisper-tiny.en-mlx", "base.en": "mlx-community/whisper-base.en-mlx",
             "small.en": "mlx-community/whisper-small.en-mlx", "distil-large-v3": "mlx-community/distil-whisper-large-v3",
             "large-v3-turbo": "mlx-community/whisper-large-v3-turbo"}
# beam search on the GPU: on real call clips greedy turbo missed 1 slur of 25 that beam 5 (and full large-v3) heard, for
# ~10% more time; distil-large-v3 missed 5 (heard "Nicky", "Niggins", or nothing). The CPU stays greedy: it is slow enough.
GPU_BEAM = 5


def made_up(compression, no_speech, logprob):
    """whisper's own signs of text it made up: a repeat loop, or words over what it thinks is no speech."""
    return compression > 2.4 or (no_speech > 0.6 and logprob < -1.0)


def apple_gpu():
    """An Apple Silicon Mac with mlx-whisper installed (speech on its GPU through Metal)."""
    return sys.platform == "darwin" and platform.machine() == "arm64" and importlib.util.find_spec("mlx_whisper") is not None


class Passes:
    """When to transcribe what: the loop's schedule, steppable with any clock (the eval runs it on a simulated one)."""

    def __init__(self):
        self.seg_start, self.last = None, 0.0

    def step(self, now, first):
        """first: earliest start among people talking now, or None -> (t0, t1) of the audio to transcribe, or None."""
        if first is not None and self.seg_start is None:
            self.seg_start = first - 0.3
        if self.seg_start is None:
            return None
        ended = first is None
        if now - self.last < STEP_S and not ended:
            return None
        self.last = now
        t0 = max(self.seg_start, now - WINDOW_S)
        if ended:
            self.seg_start = None
        return t0, now


class Listener(threading.Thread):
    def __init__(self, core):
        super().__init__(daemon=True)
        self.core = core
        self.model = None
        self.name = None
        self.mlx = None          # the mlx-whisper model repo when speech runs on an Apple GPU
        self.loaded = None
        self.latency_ms = None
        self.beam = 1
        self.status = "loading speech model"
        self.votes = []   # list hits being voted on by the passes over them (see check)
        self.probs = []
        self.plain, self.plain_probs, self.text = [], [], ""   # the last plain pass: words, their probabilities, as text
        self.recent_near, self.found = [], []

    def _load(self):
        import ctranslate2
        from faster_whisper import WhisperModel
        c = self.core.cfg["stt"]
        self.loaded = (c["model"], c["threads"], c["device"])
        self.name = c["model"]   # what actually runs (the CPU swaps the big model for base.en)
        self.mlx = None
        self.status = f"loading {c['model']}"
        if c["device"] != "cpu" and apple_gpu():
            try:
                self.mlx = MLX_REPOS[c["model"]]
                self._mlx(np.zeros(RATE, np.float32))   # loads the model and warms up Metal, in this thread (MLX wants that)
                self.status = f"ready ({c['model']}, Apple GPU)"
                return
            except Exception as e:  # noqa: BLE001
                self.mlx = None
                print("stt: Apple GPU failed, using the CPU:", e, flush=True)
        gpu = c["device"] == "gpu" or (c["device"] == "auto" and ctranslate2.get_cuda_device_count() > 0)
        try:
            if gpu:
                self.model = WhisperModel(c["model"], device="cuda", compute_type="int8_float16")
                self.beam = GPU_BEAM
                self.status = f"ready ({c['model']}, GPU)"
                return
        except Exception as e:  # noqa: BLE001  (no GPU / out of memory: the CPU instead)
            print("stt: GPU failed, using the CPU:", e, flush=True)
        name = self.name = c["model"] if c["model"].endswith(".en") else "base.en"   # the big model is too slow on a CPU
        self.model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=c["threads"])
        self.beam = 1
        self.status = f"ready ({name}, CPU)"

    def _mlx(self, x):
        import mlx_whisper
        # verbose=None: mlx-whisper prints nothing (True would print what people said)
        return mlx_whisper.transcribe(x, path_or_hf_repo=self.mlx, language="en", word_timestamps=True, temperature=0.0,
                                      condition_on_previous_text=False, verbose=None)

    def transcribe(self, x, t0):
        """[(word, abs_start, abs_end)] for audio x that starts at monotonic time t0."""
        if self.mlx:
            return [(w["word"], t0 + w["start"], t0 + w["end"])
                    for s in self._mlx(x)["segments"]
                    if not made_up(s.get("compression_ratio", 0), s.get("no_speech_prob", 0), s.get("avg_logprob", 0))
                    for w in s.get("words") or []]
        segs, _ = self.model.transcribe(x, language="en", beam_size=self.beam, word_timestamps=True,
                                        condition_on_previous_text=False, vad_filter=False)
        self.probs = []   # (start, end, probability) of the words just returned
        out = []
        for s in segs:
            if made_up(s.compression_ratio, s.no_speech_prob, s.avg_logprob):
                continue
            for w in s.words or []:
                out.append((w.word, t0 + w.start, t0 + w.end))
                self.probs.append((t0 + w.start, t0 + w.end, w.probability))
        return out

    def rule(self, type, field, default):
        """A number from the rulebook (default when that rule is off, deleted, or there is no rulebook)."""
        book = getattr(self.core, "book", None)
        if book is None:
            return {"count_sure": 0.8, "count_agree": 2, "count_near": NEAR_MIN}.get(type, default)
        return book.val(type, field, default)

    def p_over(self, a, b):
        """The least sure whisper was of any word over a..b in the plain pass (None: it didn't say, e.g. on the Apple GPU)."""
        qs = [q for pa, pb, q in self.plain_probs if pa < b + 0.01 and a - 0.01 < pb]
        return min(qs) if qs else None

    def misses(self):
        """[(list line, start, end, similarity)] for runs of plain words that nearly spell a list word but weren't a hit."""
        found, near = self.found, self.rule("count_near", "similarity", NEAR_MIN)
        wl, heard, out = self.core.words, [clean(w) for w, _, _ in self.plain], []
        t = time.monotonic()
        for e in wl.entries:
            w = clean(e.line.strip("/*"))
            if e.line.startswith("/") or not w:
                continue
            m = SequenceMatcher(None)
            m.set_seq2(w)
            best = (0.0, 0, 0)
            for k in range(1, len(e.line.split()) + 2):
                for i in range(len(heard) - k + 1):
                    m.set_seq1("".join(heard[i:i + k]))
                    if m.real_quick_ratio() >= near and m.quick_ratio() >= near:
                        r = m.ratio()
                        if r > best[0]:
                            best = (r, i, i + k - 1)
            r, i, j = best
            if r < (near if len(w) >= FUZZ_SINGLE else max(near, NEAR_SHORT)):
                continue
            a, b = self.plain[i][1], self.plain[j][2]
            if any(pa < b and a < pb for _, pa, pb in found) or (wl.safe and wl._find(self.plain[i:j + 1], wl.safe)):
                continue
            if any(le == e.line and abs(a - ta) < 3.0 for le, ta in self.recent_near):
                continue
            self.recent_near.append((e.line, a))
            out.append((e.line, a, b, r))
        self.recent_near = [(le, ta) for le, ta in self.recent_near if t - ta < 30]
        return out

    def check(self, x, t0, last=False, clipped=False):
        """[(entry, a, b, p, text)] to act on. Each stretch of talk is transcribed by several overlapping passes, and whisper
        flips between them on sloppy speech ("Nicky, you literally said" in one, the slur in the next), so every pass that
        heard a hit's whole stretch votes on it: some list word there or not (any list word: one pass hears "n****r", the
        next "n***a"). count_agree's yes votes ahead of the no votes, or one at count_sure's p: it counts, with its surest
        p. Two no votes ahead: dropped.
        When no pass is left to vote (the talk ended, or the window moved past it), a tie or a lone yes after a no is
        asked about (p 0, never muted), a lone yes counts with its own p. last: the talk ended, this pass has all of it.
        clipped: the window starts mid-talk, so its first words are cut off (earlier passes heard them whole)."""
        t = time.monotonic()
        words = self.transcribe(x, t0)
        self.plain, self.plain_probs = words, list(self.probs)
        self.text = " ".join(w.strip() for w, _, _ in words)
        found = self.core.words.find(words)
        if self.core.setup_test["on"]:
            found += TEST_LIST.find(words)
        self.found = list(found)
        self.latency_ms = int((time.monotonic() - t) * 1000)
        t1, edge = t0 + len(x) / RATE, t0 + (EDGE_S if clipped else 0.0)
        sure, need = self.rule("count_sure", "p", None), self.rule("count_agree", "votes", 1)
        nxt = t1 + STEP_S - WINDOW_S                                         # where the next pass will start
        nxt_edge = nxt + EDGE_S if clipped or nxt > t0 else t0
        over = lambda c, a, b: a < c["b"] + 0.3 and c["a"] - 0.3 < b   # noqa: E731
        for e, a, b in found:
            if a >= edge and not any(over(c, a, b) for c in self.votes):
                self.votes.append({"entry": e, "a": a, "b": b, "yes": 0, "no": 0, "p": None, "text": "", "done": False, "t": t})
        hits = []
        for c in self.votes:
            if c["done"]:
                continue
            if edge <= c["a"] - 0.1 and c["b"] + (0.05 if last else TAIL_S) <= t1:   # this pass heard all of it
                same = [(e, a, b) for e, a, b in found if over(c, a, b)]
                if same:
                    e, a, b = same[0]
                    q = self.p_over(a, b)
                    if c["yes"] == 0 or (q is not None and (c["p"] is None or q > c["p"])):
                        c.update(entry=e, a=a, b=b, p=q, text=self.text)
                    c["yes"] += 1
                else:
                    c["no"] += 1
            y, n = c["yes"], c["no"]
            if (y >= need and y > n) or (y == 1 and n == 0 and c["p"] is not None and sure is not None and c["p"] >= sure):
                c["done"] = True
                hits.append((c["entry"], c["a"], c["b"], c["p"], c["text"]))
            elif n >= 2 and n > y:
                c["done"] = True
            elif last or c["a"] - 0.1 < nxt_edge:                               # the next pass starts past it
                c["done"] = True
                if y and y >= n:
                    hits.append((c["entry"], c["a"], c["b"], 0.0 if n else c["p"], c["text"]))
        self.votes = [c for c in self.votes if not c["done"] or t - c["t"] < 30]
        return hits

    def flush(self):
        """The talk ended without another pass: what the votes say so far."""
        out = [(c["entry"], c["a"], c["b"], 0.0 if c["no"] else c["p"], c["text"]) for c in self.votes
               if not c["done"] and c["yes"] and c["yes"] >= c["no"]]
        self.votes = [c for c in self.votes if c["done"]]
        return out

    def run(self):
        try:
            self._load()
        except Exception as e:
            self.status = f"speech model failed: {e}"
            return
        sched = Passes()
        while True:
            time.sleep(0.1)
            c = self.core.cfg["stt"]
            if (c["model"], c["threads"], c["device"]) != self.loaded:
                try:
                    self._load()
                except Exception as e:
                    self.status = f"speech model failed: {e}"
                    time.sleep(5)
                    continue
            if not self.core.cfg["slurs"]["enabled"]:
                sched.seg_start = None
                continue
            now = time.monotonic()
            seg = sched.seg_start
            p = sched.step(now, self.core.talk_started(now))  # earliest start among people talking now, or None
            if p is None:
                continue
            last = sched.seg_start is None
            x, ts = self.core.ring.get(p[0], p[1])
            if len(x) < RATE * 0.3:
                for entry, a, b, q, text in self.flush() if last else []:
                    self.core.heard(entry, a, b, q, text)
                continue
            try:
                for entry, a, b, q, text in self.check(x, ts, last, clipped=seg is not None and ts > seg + 0.05):
                    self.core.heard(entry, a, b, q, text)
                if self.core.review.on and self.core.book.get("count_near"):
                    for entry, a, b, r in self.misses():
                        self.core.heard(entry, a, b, None, self.text, near=r)
            except Exception as e:
                print("stt error:", e, flush=True)
