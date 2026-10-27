"""Who said it, by online diarization (DIART-style, no pyannote): every 0.5 s the last 1.5 s of the mix get a speaker
embedding (the WeSpeaker model voice.py already runs), and a cosine clusterer sorts the chunks into voices as they
come: join the nearest voice or start a new one. Then the speaking events name the voices: a cluster belongs to the
Discord user who was on while it was heard, again and again, mostly alone. Unlike voice.py's profiles, which need
moments one person talks alone, a cluster needs no solo moment to exist, only to get its name. who() then scores the
chunks around a word against each named person's clusters.
Memory only, for the current call: forget() clears it all when you switch channel."""
import collections
import threading
import time

import numpy as np

from .audio import RATE

STEP_S = 0.5         # a new chunk this often ...
WIN_S = 1.5          # ... covering this much of the latest audio
SPEECH_MIN = 0.5     # share of the window Silero must call speech, or the chunk is skipped
VAD_P = 0.5          # Silero frame probability that counts as speech
JOIN = 0.35          # cosine similarity to a cluster's centroid that joins it; below: a new cluster
UPDATE = 0.55        # ... and only a chunk this alike moves the centroid (a blend of two voices would drag it)
MOVE_MIN = 0.05      # the centroid keeps moving at least this fast (a voice shifts over a call)
MERGE = 0.50         # two centroids this alike are one voice that got split (people's centroids sit ~0.1-0.3 apart)
MAX_CLUSTERS = 16
KEEP_S = 60.0        # timeline length
ON_MIN_S = 0.05      # a uid counts as speaking in a chunk's centre with at least this much overlap
MAP_SHARE = 0.6      # a cluster is a person when one uid holds this share of its speaking evidence ...
MAP_SECS = 3.0       # ... and at least this many seconds of it
WHO_SIM = 0.65       # who(): the chunks around the word must be this like the person's voice ... (0.60 -> 0.65 on the
                     # eval: wrong picks 16.8 -> 16.2 % on equally loud overlaps, 42.9 -> 39.7 % on quieter ones, for
                     # half a point of right ones; a wrong pick lets the one who said it go. The rule can change both)
WHO_MARGIN = 0.25    # ... and this far ahead of anyone else's (two voices at once sound like the louder one)
WHO_WEIGHT = 0.5     # ... and there must be chunks around the word at all (sum of the triangular weights)

Chunk = collections.namedtuple("Chunk", "t0 t1 cluster sim emb")   # sim: to its cluster when it was filed


def on(core):
    """The "voices told apart" rule is on (rulebook who_diar; a bare core without a rulebook: on)."""
    book = getattr(core, "book", None)
    return book is None or book.get("who_diar") is not None


class _Cluster:
    __slots__ = ("v", "u", "n", "last")

    def __init__(self, e, t):
        self.v, self.u, self.n, self.last = e.copy(), e.copy(), 1, t

    def move(self, e):
        a = max(1.0 / (self.n + 1), MOVE_MIN)
        self.v = (1 - a) * self.v + a * e
        self.u = self.v / (np.linalg.norm(self.v) + 1e-9)
        self.n += 1


class Diarizer:
    def __init__(self, core, embed=None):
        self.core = core
        self._shared = embed is None                # True: the voice model voice.py loads, which may still be loading
        self.embed = embed or (lambda x: core.voices.embed(x))
        self.status = "idle"
        self.lock = threading.Lock()
        self.vad = None
        self._warned = {}
        self.gen = 0                                # forget() counts up: a chunk begun before it is not filed after it
        self.forget()

    def forget(self):
        with self.lock:
            self.gen += 1
            self.clusters = {}                      # id -> _Cluster
            self.evidence = {}                      # id -> {uid: seconds on while this voice was heard, 1/k each}
            self.timeline = collections.deque()     # Chunk, newest last
            self.next_id = 1
            self._total = -1                        # ring.total at the last step
            self._end = None                        # audio time the last chunk ended

    def _err(self, where, e):
        now = time.monotonic()
        if now - self._warned.get(where, -99) > 10:
            self._warned[where] = now
            print(f"diar {where}: {e!r}", flush=True)

    # ---------- the chunk loop ----------
    def run(self):
        while True:
            time.sleep(STEP_S)
            try:
                self.step()
            except Exception as e:  # noqa: BLE001  (bad audio must never end the thread)
                self._err("step", e)

    def _load_vad(self):
        try:
            from faster_whisper.vad import get_vad_model
            self.vad = get_vad_model()
        except Exception as e:  # noqa: BLE001
            self.status = f"off ({e})"[:120]
            self.vad = False
            self._err("vad model", e)

    def step(self):
        """One chunk: the latest WIN_S of the ring -> VAD -> embedding -> cluster. Returns (cluster, sim) or None."""
        core = self.core
        if not on(core) or (self._shared and core.voices.sess is None):
            return None
        if self.vad is None:
            self._load_vad()
        if not self.vad:
            return None
        ring = core.ring
        total, off = ring.total, ring.offset
        if off is None or total == self._total:
            return None                                       # no audio, or none since the last step
        t1 = ring.t(total)
        if self._end is not None and t1 - self._end < STEP_S - 0.05:
            return None
        x, t0 = ring.get(t1 - WIN_S, t1)
        if len(x) < WIN_S * RATE * 0.95:
            return None
        self._total, self._end = total, t1
        gen = self.gen
        try:
            n = len(x) // 512 * 512
            p = np.asarray(self.vad(x[:n])).reshape(-1)
            if float(np.mean(p > VAD_P)) < SPEECH_MIN:
                self.status = "listening"
                return None
        except Exception as e:  # noqa: BLE001
            self._err("vad", e)
            return None
        try:
            e = self.embed(x)
        except Exception as ex:  # noqa: BLE001
            self._err("embed", ex)
            return None
        if e is None or not np.all(np.isfinite(e)):
            return None
        self.status = "on"
        try:
            return self.feed(e, t0, t0 + len(x) / RATE, gen)
        except Exception as ex:  # noqa: BLE001
            self._err("cluster", ex)
            return None

    # ---------- clustering ----------
    def feed(self, e, t0, t1, gen=None):
        """A chunk's unit embedding, heard between audio times t0..t1: cluster it, credit the speakers on then."""
        ov = self._on_during((t0 + t1) / 2)                  # (core lock) before ours: never hold both
        with self.lock:
            if gen is not None and gen != self.gen:
                return None                                  # forget() ran meanwhile: that was another call
            cid, sim = self._assign(e, t1)
            self.timeline.append(Chunk(t0, t1, cid, sim, e))
            while self.timeline and self.timeline[0].t1 < t1 - KEEP_S:
                self.timeline.popleft()
            if ov:
                ev = self.evidence.setdefault(cid, {})
                for uid, s in ov.items():
                    ev[uid] = ev.get(uid, 0.0) + s / len(ov)
            self._merge()
            cid = self.timeline[-1].cluster
        return cid, sim

    def _assign(self, e, t):
        cs = self.clusters
        best, sim = None, -1.0
        if cs:
            ids = list(cs)
            sims = np.stack([cs[i].u for i in ids]) @ e
            k = int(np.argmax(sims))
            best, sim = ids[k], float(sims[k])
        if best is not None and sim >= JOIN:
            c = cs[best]
            if sim >= UPDATE:
                c.move(e)
            c.last = t
            return best, sim
        if len(cs) >= MAX_CLUSTERS:                          # full: the weakest (small and long unheard) makes room
            gone = min(cs, key=lambda i: cs[i].n * 0.5 ** ((t - cs[i].last) / 30.0))
            del cs[gone]
            self.evidence.pop(gone, None)
        cid, self.next_id = self.next_id, self.next_id + 1
        cs[cid] = _Cluster(e, t)
        return cid, JOIN                                     # the founding chunk: no better than a borderline one

    def _merge(self):
        """Fold together clusters whose centroids have grown alike (a voice that started two), unless the speaking events
        say they are two different people."""
        cs = self.clusters
        while len(cs) > 1:
            ids = sorted(cs)
            u = np.stack([cs[i].u for i in ids])
            sims = np.triu(u @ u.T, 1)
            x, y = np.unravel_index(int(np.argmax(sims)), sims.shape)
            if sims[x, y] < MERGE:
                return
            keep, gone = ids[x], ids[y]
            names = self._mapping()
            if keep in names and gone in names and names[keep][0] != names[gone][0]:
                return
            a, b = cs[keep], cs.pop(gone)
            a.v = (a.n * a.v + b.n * b.v) / (a.n + b.n)
            a.u = a.v / (np.linalg.norm(a.v) + 1e-9)
            a.n, a.last = a.n + b.n, max(a.last, b.last)
            ev = self.evidence.setdefault(keep, {})
            for uid, sec in self.evidence.pop(gone, {}).items():
                ev[uid] = ev.get(uid, 0.0) + sec
            self.timeline = collections.deque(c._replace(cluster=keep) if c.cluster == gone else c for c in self.timeline)

    # ---------- who was on ----------
    def _on_during(self, tc):
        """{uid: seconds on} in the 0.5 s around audio time tc (lag-corrected), people who could be in the mix only."""
        core = self.core
        half = STEP_S / 2
        ea, eb = tc - core.lag - half, tc - core.lag + half
        out = {}
        with core.lock:
            for uid, iv in core.talk.items():
                if not core._counts(uid, ea):
                    continue
                s = sum(max(0.0, min(eb if e is None else e, eb) - max(st, ea)) for st, e in iv)
                if s >= ON_MIN_S:
                    out[uid] = s
        return out

    def _mapping(self):
        """cluster id -> (uid, share, seconds) for the clusters that have a name. Call with self.lock held."""
        out = {}
        for cid, d in self.evidence.items():
            if cid not in self.clusters or not d:
                continue
            uid = max(d, key=d.get)
            tot = sum(d.values())
            if d[uid] >= MAP_SECS and d[uid] >= MAP_SHARE * tot:
                out[cid] = (uid, d[uid] / tot, d[uid])
        return out

    def mapping(self):
        with self.lock:
            return self._mapping()

    # ---------- the answer ----------
    def who(self, a, b, cands):
        """(uid, score) for the word heard between audio times a..b, if the voices around it are one of cands; else None.
        score: how like that person's voice(s) the chunks around the word are (cosine, like voice.py's match)."""
        book = getattr(self.core, "book", None)
        sim = book.val("who_diar", "min", WHO_SIM) if book else WHO_SIM
        margin = book.val("who_diar", "margin", WHO_MARGIN) if book else WHO_MARGIN
        try:
            r = self.scores(a, b, cands)
            if r and r["weight"] >= WHO_WEIGHT and r["score"] >= sim and r["margin"] >= margin:
                return r["uid"], r["score"]
        except Exception as e:  # noqa: BLE001
            self._err("who", e)
        return None

    def scores(self, a, b, cands):
        """The tally behind who(): {uid, score, margin, weight} for the best of cands, or None if no named voice is there.
        Every chunk overlapping the word counts by how near its middle is to the word's (a chunk centred on the word:
        1, one just touching it: 0). It is scored against each named person's clusters (the best of them), always against
        the centroids as they are now, so a chunk that was filed under a stray cluster still counts for the right person.
        score: the best candidate's weighted mean; margin: its lead over the best of anyone else named."""
        cands = set(cands)
        if not cands:
            return None
        m = (a + b) / 2
        with self.lock:
            rows = [c for c in self.timeline if c.t1 >= a and c.t0 <= b]
            names = self._mapping()
            cent = {cid: self.clusters[cid].u for cid in names}
        tot, weight = {}, 0.0
        for c in rows:
            w = max(0.0, 1.0 - abs(m - (c.t0 + c.t1) / 2) / (WIN_S / 2))
            if w <= 0:
                continue
            weight += w
            per = {}
            for cid, (uid, _, _) in names.items():
                per[uid] = max(per.get(uid, -1.0), float(c.emb @ cent[cid]))
            for uid, v in per.items():
                tot[uid] = tot.get(uid, 0.0) + w * v
        mine = [u for u in tot if u in cands]
        if not mine:
            return None
        best = max(mine, key=tot.get)
        rest = [v for u, v in tot.items() if u != best]
        return {"uid": best, "score": tot[best] / weight, "margin": (tot[best] - (max(rest) if rest else 0.0)) / weight,
                "weight": weight}
