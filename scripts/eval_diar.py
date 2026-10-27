"""Who said the word, on simulated calls of real speech: Core.pick scored with timing only, with voice profiles, and with
online diarization (dogmute/diar.py).

  python -I scripts/eval_diar.py                 # 20 calls of 3-4 LibriSpeech dev-clean speakers, 120 s each
  python -I scripts/eval_diar.py --opus          # every speaker through Opus 32 kbps (ffmpeg) before the mix
  python -I scripts/eval_diar.py --busy          # people talk over each other twice as much (~46 % of the speech overlapped)
  python -I scripts/eval_diar.py --calls 6 --set JOIN=0.45,UPDATE=0.7    # try other diar.py constants
  python -I scripts/eval_diar.py --jobs 1 --calls 2                      # one process: honest ms per step

Data: LibriSpeech dev-clean (openslr.org/resources/12) is downloaded to ~/.cache/dogmute-eval/ and extracted into its own
directory there (it is untrusted data: run this with -I, nothing in it is executed). Each call is turn-taking with ~20 %
of the speech overlapped, per-speaker gain, speaking events with +-100 ms jitter, audio 0.22 s behind the events (core.lag
is set to that, as the call would have measured it), and "words": random 0.4 s spans inside one person's speech. Each
word goes through Core.pick 1.5 s after it ended, the way the listener gets there, in four settings:
  timing    diar off, voice off          (what DogMute did before voices)
  voice     voice profiles only          (what DogMute does now)
  diar+voice  diarization, voice as the fallback  (the new default)
  diar      diarization alone
Nothing here touches ~/.config/dogmute, ~/.local/state/dogmute, or any audio device (config and state go to a temp dir)."""
import argparse
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp(prefix="dogmute-eval-")
os.environ["DOGMUTE_CONFIG"], os.environ["DOGMUTE_STATE"] = _tmp + "/c", _tmp + "/s"   # never the real ones
os.environ.setdefault("HF_HUB_OFFLINE", "1")                                           # the voice model is cached

CACHE = Path.home() / ".cache/dogmute-eval"
URL = "https://www.openslr.org/resources/12/dev-clean.tar.gz"
RATE = 16000
LAG = 0.22            # audio runs this far behind the speaking events
JITTER = 0.1          # +- on every speaking event
WORD_S = 0.4
PICK_AFTER = 1.5      # s after the word ends that the listener has it transcribed and asks pick()
T0 = 1000.0
SETTINGS = {"timing": (False, False), "voice": (True, False), "diar+voice": (True, True), "diar": (False, True)}
# the same call is simulated once with everything on; only pick()'s switches differ between the settings


# ---------- data ----------
def ensure_data():
    d = CACHE / "librispeech-dev-clean"
    root = d / "LibriSpeech" / "dev-clean"
    if root.is_dir():
        return root
    CACHE.mkdir(parents=True, exist_ok=True)
    tgz = CACHE / "dev-clean.tar.gz"
    if not tgz.exists():
        print("downloading", URL, flush=True)
        urllib.request.urlretrieve(URL, tgz)
    d.mkdir()
    with tarfile.open(tgz) as t:
        t.extractall(d, filter="data")
    return root


def ffmpeg_decode(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ar", str(RATE), "-ac", "1", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32).copy()


def opus(x, kbps=32):
    enc = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(RATE), "-ac", "1", "-i", "-",
                          "-c:a", "libopus", "-b:a", f"{kbps}k", "-f", "ogg", "-"],
                         input=x.astype(np.float32).tobytes(), capture_output=True, check=True)
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "f32le", "-ar", str(RATE), "-ac", "1", "-"],
                         input=enc.stdout, capture_output=True, check=True)
    y = np.frombuffer(dec.stdout, np.float32)
    return np.pad(y, (0, max(0, len(x) - len(y))))[:len(x)].copy()


def segments(x):
    """Speech stretches of one utterance [(start s, end s)]: 20 ms energy frames over a floor 30 dB under the loud ones."""
    n = len(x) // 320
    db = 10 * np.log10(np.mean(x[:n * 320].reshape(n, 320) ** 2, axis=1) + 1e-10)
    on = db > max(np.percentile(db, 95) - 30, -70)
    out, i = [], 0
    while i < n:
        if on[i]:
            j = i
            while j < n and (on[j] or on[j:j + 15].any()):    # bridge pauses under 0.3 s
                j += 1
            while j > i and not on[j - 1]:
                j -= 1
            if j - i >= 10:
                out.append((i * 0.02, j * 0.02))
            i = j
        i += 1
    return out


# ---------- a call ----------
def build_call(root, idx, seed, secs, p_overlap, overlap_s, use_opus):
    rng = np.random.default_rng([seed, idx])
    spks = sorted(p.name for p in root.iterdir())
    n = int(rng.integers(3, 5))
    ids = [str(s) for s in rng.choice(spks, n, replace=False)]
    files = {s: sorted((root / s).glob("*/*.flac")) for s in ids}
    gain = {s: 10 ** (rng.uniform(-4, 4) / 20) for s in ids}
    tracks = {s: [] for s in ids}
    last, prev_s, prev_e, end = None, 0.0, 0.0, 0.0
    while True:
        s = str(rng.choice([u for u in ids if u != last]))
        for _ in range(20):
            x = ffmpeg_decode(files[s][int(rng.integers(len(files[s])))])
            if 2.0 <= len(x) / RATE <= 12.0:
                break
        dur = len(x) / RATE
        if last is not None and rng.random() < p_overlap:
            start = max(prev_e - rng.uniform(*overlap_s), prev_s + 1.0)
        else:
            start = prev_e + rng.uniform(0.2, 1.2) if last is not None else 0.5
        if start + dur > secs:
            break
        tracks[s].append((start, x))
        last, prev_s, prev_e = s, start, start + dur
        end = max(end, prev_e)
    total = int((end + 1.0) * RATE)
    mix = np.zeros(total, np.float32)
    raw, segs, events = {}, {}, {}
    for s in ids:
        tr = np.zeros(total, np.float32)
        segs[s] = []
        for start, x in tracks[s]:
            a = int(start * RATE)
            tr[a:a + len(x)] += x * gain[s]
            segs[s] += [(start + u, start + v) for u, v in segments(x)]
        raw[s] = tr
        mix += opus(tr) if use_opus else tr
        ev = []
        for u, v in segs[s]:
            ev.append((u + rng.uniform(-JITTER, JITTER), v + rng.uniform(-JITTER, JITTER)))
        events[s] = ev
    peak = float(np.abs(mix).max())
    if peak > 0.9:
        mix *= 0.9 / peak
    # words: 0.4 s spans fully inside one person's speech stretch
    flat = [(s, u, v) for s in ids for u, v in segs[s] if v - u >= WORD_S + 0.1 and v > 1.0]
    wts = np.array([v - u - WORD_S for _, u, v in flat])
    words = []
    for k in rng.choice(len(flat), 80, p=wts / wts.sum()):
        s, u, v = flat[k]
        w = float(rng.uniform(max(u + 0.05, 1.0), v - WORD_S - 0.05)) if v - WORD_S - 0.05 > max(u + 0.05, 1.0) else None
        if w is None:
            continue
        over = any(t != s and any(a < w + WORD_S and b > w for a, b in segs[t]) for t in ids)
        i, j = int(w * RATE), int((w + WORD_S) * RATE)
        mine = float(np.mean(raw[s][i:j] ** 2)) + 1e-9
        rest = float(np.mean(sum(raw[t][i:j] for t in ids if t != s) ** 2)) + 1e-9
        words.append({"u": w, "uid": s, "over": bool(over), "rel_db": round(10 * np.log10(mine / rest), 1)})
    words.sort(key=lambda d: d["u"])
    both = np.zeros(int(total / RATE / 0.02) + 2, np.int8)
    for s in ids:
        for u, v in segs[s]:
            both[int(u / 0.02):int(v / 0.02)] += 1
    return {"audio": mix, "ids": ids, "events": events, "segs": segs, "words": words,
            "overlap_frac": float((both >= 2).sum() / max((both >= 1).sum(), 1))}


def load_call(root, idx, args):
    d = CACHE / f"calls-s{args.seed}-{args.secs}-p{args.p}-o{args.overlap_s.replace(',', '_')}-{'opus' if args.opus else 'raw'}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{idx:02d}.npy"
    if f.exists():
        meta = json.loads((d / f"{idx:02d}.json").read_text())
        meta["audio"] = np.load(f, allow_pickle=False)
        return meta
    c = build_call(root, idx, args.seed, args.secs, args.p, tuple(float(v) for v in args.overlap_s.split(',')), args.opus)
    np.save(f, c["audio"])
    (d / f"{idx:02d}.json").write_text(json.dumps({k: v for k, v in c.items() if k != "audio"}))
    return c


# ---------- one simulated call ----------
def pct(xs, q):
    return float(np.percentile(xs, q)) if len(xs) else float("nan")


def simulate(call, voices, fixed):
    from dogmute import core as C
    C.CONF, C.STATE = Path(tempfile.mkdtemp(dir=_tmp, prefix="c")), Path(tempfile.mkdtemp(dir=_tmp, prefix="s"))   # one per call: workers race otherwise
    time_now = [T0]
    real_monotonic, time.monotonic = time.monotonic, lambda: time_now[0]   # the learner's rate limit runs on the call's clock
    core = C.Core()
    core.cfg["review"]["enabled"] = False
    rules = {r["id"]: r for r in core.book.rules}
    def use(vo, di):   # noqa: E306 (the rule dicts directly: no save to disk per word)
        rules["who_voice"]["on"], rules["who_diar"]["on"] = vo, di
    use(True, True)
    core.me = {"id": "me"}
    core.lag = LAG
    core.act = lambda *a, **k: None
    core.voices = voices
    voices.forget(set())
    ids = call["ids"]
    core.members = {u: {"name": u, "mute": False} for u in ids}
    ev = {u: [(T0 + s, T0 + e) for s, e in call["events"][u]] for u in ids}
    nxt_s, nxt_e = {u: 0 for u in ids}, {u: 0 for u in ids}      # next event to show: starts, stops
    core.talk = {u: [] for u in ids}
    chunks = []
    orig = core.diar.feed

    def feed(e, t0, t1, *gen):
        r = orig(e, t0, t1, *gen)
        chunks.append(((t0 + t1) / 2, r[0]))
        return r
    core.diar.feed = feed
    words = call["words"]
    due, results, steps, skips, whos = 0, [], [], [], []
    x = call["audio"]
    x = np.pad(x, (0, int((PICK_AFTER + 1.0) * RATE)))
    for k in range(len(x) // 320):
        now = T0 + LAG + (k + 1) * 0.02
        time_now[0] = now
        for u in ids:                                    # the speaking events that have reached us by now
            tl, e = core.talk[u], ev[u]
            while nxt_s[u] < len(e) and e[nxt_s[u]][0] <= now:
                tl.append((e[nxt_s[u]][0], None))
                nxt_s[u] += 1
            while nxt_e[u] < nxt_s[u] and e[nxt_e[u]][1] <= now:
                tl[nxt_e[u]] = e[nxt_e[u]]
                nxt_e[u] += 1
        core.on_block(x[k * 320:(k + 1) * 320], now)
        if (k + 1) % 25:
            continue
        while not core.learn_q.empty():                  # Core._learner's body, inline (no thread on a simulated clock)
            uid, t0, t1 = core.learn_q.get()
            seg, _ = core.ring.get(t0, t1)
            if len(seg) >= 16000 and float(np.sqrt(np.mean(seg * seg))) > 0.005:
                voices.learn(uid, seg)
        t = time.perf_counter()
        r = core.diar.step()
        (steps if r else skips).append((time.perf_counter() - t) * 1000)
        while due < len(words):
            w = words[due]
            a = T0 + LAG + w["u"]
            if now < a + WORD_S + PICK_AFTER:
                break
            due += 1
            row = dict(w)
            for name, (vo, di) in SETTINGS.items():
                use(vo, di)
                t = time.perf_counter()
                uid, cand, how = core.pick(a, a + WORD_S)
                if name == "diar":
                    whos.append((time.perf_counter() - t) * 1000)
                row[name] = (uid, how.split(" ")[0] if how.startswith(("diar", "voice")) else how,
                             float(how.split(" ")[2] if how.startswith("voice") else how.split(" ")[1]) if how.startswith(("diar", "voice")) else None)
            use(True, True)
            row["tally"] = core.diar.scores(a, a + WORD_S, ids)       # the raw tally, for picking thresholds offline
            results.append(row)
    # chunk purity: of the chunks whose centre has exactly one true speaker, how many sit in a cluster named for them
    names = core.diar.mapping()
    ok = bad = none = 0
    for tc, cid in chunks:
        u = tc - T0 - LAG
        on = [s for s in ids if any(a < u + 0.25 and b > u - 0.25 for a, b in call["segs"][s])]
        if len(on) != 1 or not any(a <= u - 0.25 and u + 0.25 <= b for a, b in call["segs"][on[0]]):
            continue
        nm = names.get(cid)
        ok, bad, none = ok + (nm is not None and nm[0] == on[0]), bad + (nm is not None and nm[0] != on[0]), none + (nm is None)
    time.monotonic = real_monotonic
    return {"words": results, "steps": steps, "skips": skips, "who_ms": whos, "clusters": len(core.diar.clusters),
            "named": len(names), "speakers": len(ids), "purity": (ok, bad, none), "overlap_frac": call["overlap_frac"]}


_W = {}


def work(job):
    i, args, over = job
    from dogmute import diar as D
    from dogmute.voice import Voices
    for k, v in over.items():
        setattr(D, k, v)
    if "v" not in _W:
        _W["v"] = Voices()
        _W["v"].load()
    root = ensure_data()
    call = load_call(root, i, args)
    out = simulate(call, _W["v"], True)
    out["call"] = i
    return out


# ---------- report ----------
def stat(rows, name, pick=lambda r: True):
    rs = [r for r in rows if pick(r)]
    n = len(rs)
    if not n:
        return "     -"
    ok = sum(r[name][0] == r["uid"] for r in rs)
    un = sum(r[name][0] is None for r in rs)
    return f"{100 * ok / n:5.1f}/{100 * (n - ok - un) / n:4.1f}/{100 * un / n:4.1f}"


def report(res, args):
    rows = [dict(w, call=r["call"]) for r in res for w in r["words"]]
    hard = lambda r: r["timing"][0] is None                     # timing alone cannot tell
    louder, equal, quieter = LOUDER, EQUAL, QUIETER
    print(f"\n{len(res)} calls, {len(rows)} words ({sum(r['over'] for r in rows)} overlapped, {sum(hard(r) for r in rows)} that"
          f" timing can't settle), {'Opus 32 kbps' if args.opus else 'raw 16 kHz'}; overlapped speech "
          f"{100 * np.mean([r['overlap_frac'] for r in res]):.0f} % of speech time on average")
    print("cells: correct / wrong / unclear, % of the words in the column\n")
    cols = [("all", lambda r: True), ("solo", lambda r: not r["over"]), ("overlapped", lambda r: r["over"]),
            ("overl. louder>3dB", louder), ("overl. equal+-3dB", equal), ("overl. quieter>3dB", quieter),
            ("timing-can't-tell", hard), ("first 30 s", lambda r: r["u"] < 30), ("after 30 s", lambda r: r["u"] >= 30),
            ("hard, first 30 s", lambda r: hard(r) and r["u"] < 30), ("hard, after 30 s", lambda r: hard(r) and r["u"] >= 30)]
    print(f"{'':12}" + "".join(f"{c:>20}" for c, _ in cols))
    print(f"{'(n)':12}" + "".join(f"{sum(f(r) for r in rows):>20}" for _, f in cols))
    for name in SETTINGS:
        print(f"{name:12}" + "".join(f"{stat(rows, name, f):>20}" for _, f in cols))
    how = {}
    for r in rows:
        k = r["diar+voice"][1]
        how[k] = how.get(k, 0) + 1
    print("\nhow diar+voice answered:", ", ".join(f"{k} {v}" for k, v in sorted(how.items(), key=lambda kv: -kv[1])))
    dv = [r for r in rows if r["diar+voice"][1] == "diarization"]
    print(f"  diarization answers: {sum(r['diar+voice'][0] == r['uid'] for r in dv)}/{len(dv)} right")
    ok, bad, none = (sum(r["purity"][i] for r in res) for i in range(3))
    print(f"chunks with one true speaker: {100 * ok / max(ok + bad + none, 1):.0f} % in a cluster named for them, "
          f"{100 * bad / max(ok + bad + none, 1):.0f} % named for someone else, {100 * none / max(ok + bad + none, 1):.0f} % in an unnamed cluster")
    print(f"clusters per call at the end: {np.mean([r['clusters'] for r in res]):.1f} for {np.mean([r['speakers'] for r in res]):.1f} speakers,"
          f" {np.mean([r['named'] for r in res]):.1f} named")
    st = [m for r in res for m in r["steps"]]
    sk = [m for r in res for m in r["skips"]]
    wm = [m for r in res for m in r["who_ms"]]
    print(f"ms per diar step (VAD + fbank + embedding + cluster): median {pct(st, 50):.0f}, p95 {pct(st, 95):.0f}, max {max(st):.0f}"
          f" over {len(st)} steps; steps VAD skipped (silence): median {pct(sk, 50):.1f}; pick() with diar: median {pct(wm, 50):.1f}"
          f" p95 {pct(wm, 95):.1f} (includes voice scoring when diar can't answer)" if st else "no steps ran")


LOUDER = lambda r: r["over"] and r["rel_db"] >= 3         # the word's speaker 3 dB or more above everyone else on (what whisper hears)
EQUAL = lambda r: r["over"] and -3 <= r["rel_db"] < 3
QUIETER = lambda r: r["over"] and r["rel_db"] < -3        # drowned: whisper would rarely give us this word at all


def brief(res):
    rows = [dict(w, call=r["call"]) for r in res for w in r["words"]]
    hard = lambda r: r["timing"][0] is None
    pick = {"hard": hard, "louder": LOUDER, "equal": EQUAL, "quieter": QUIETER, "<30s": lambda r: r["u"] < 30}
    for name in ("voice", "diar+voice", "diar"):
        print(f"{name:11} all {stat(rows, name)}  " + "  ".join(f"{k} {stat(rows, name, f)}" for k, f in pick.items()))
    dv = [r for r in rows if r["diar"][1] == "diarization"]
    for lo, hi in ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)):
        b = [r for r in dv if lo <= r["diar"][2] < hi]
        print(f"  score {lo:.1f}-{min(hi, 1):.1f}: {len(b):4} answers, {sum(r['diar'][0] == r['uid'] for r in b)} right;"
              f" of the wrong ones {sum(QUIETER(r) for r in b if r['diar'][0] != r['uid'])} drowned, "
              f"{sum(EQUAL(r) for r in b if r['diar'][0] != r['uid'])} equal, {sum(LOUDER(r) for r in b if r['diar'][0] != r['uid'])} louder,"
              f" {sum(not r['over'] for r in b if r['diar'][0] != r['uid'])} not overlapped")
    print(f"diar answers {sum(r['diar'][0] == r['uid'] for r in dv)}/{len(dv)} right; clusters {np.mean([r['clusters'] for r in res]):.1f}"
          f" named {np.mean([r['named'] for r in res]):.1f} of {np.mean([r['speakers'] for r in res]):.1f} speakers")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--calls", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--secs", type=int, default=120)
    ap.add_argument("--p", type=float, default=0.6, help="chance the next utterance starts over the end of the last")
    ap.add_argument("--overlap-s", default="1.5,4.0", help="LO,HI s: how far into the last utterance the next one starts when it talks over it")
    ap.add_argument("--busy", action="store_true", help="a busy call: p 0.9, overlaps of 2-5 s (~46 %% of the speech overlapped)")
    ap.add_argument("--opus", action="store_true")
    ap.add_argument("--build-only", action="store_true", help="make (and cache) the calls, print their overlap, stop")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--first", type=int, default=0, help="first call index")
    ap.add_argument("--dump", help="write every word's rows to this JSON file")
    ap.add_argument("--brief", action="store_true", help="one line per setting, for sweeps")
    ap.add_argument("--set", default="", help="NAME=value,... diar.py constants to override")
    args = ap.parse_args()
    if args.busy:
        args.p, args.overlap_s = 0.9, "2.0,5.0"
    over = {k: float(v) if "." in v else int(v) for k, v in (p.split("=") for p in args.set.split(",") if p)}
    jobs = [(i, args, over) for i in range(args.first, args.first + args.calls)]
    root = ensure_data()
    t = time.time()
    if args.build_only:
        fr = [load_call(root, i, args)["overlap_frac"] for i, _, _ in jobs]
        print(f"{time.time() - t:.0f} s; overlapped share of speech time per call: {np.round(fr, 2).tolist()}, mean {np.mean(fr):.3f}")
        return
    if args.jobs == 1:
        res = [work(j) for j in jobs]
    else:
        with mp.get_context("fork").Pool(args.jobs) as pool:
            res = pool.map(work, jobs, chunksize=1)
    print(f"{time.time() - t:.0f} s, overrides {over or 'none'}")
    if args.dump:
        Path(args.dump).write_text(json.dumps([dict(w, call=r["call"]) for r in res for w in r["words"]]))
    if args.brief:
        brief(res)
    else:
        report(res, args)


if __name__ == "__main__":
    main()
