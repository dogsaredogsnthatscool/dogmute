"""Turn the review queue (items.jsonl + clips in the state dir) into a labelled set, or just print what's there.

  python scripts/review_export.py                      # stats
  python scripts/review_export.py --out DIR            # DIR/clips/*.wav + manifest.jsonl + manifest.csv, for scripts/eval_stt.py --real DIR

Labels: "correct" / "wrong" (a "who" case answered with a person is correct, with `who` = their id; "Nobody" is wrong).
Only answers a person gave count (a button on the page or in a notification): a mute nobody undid, or an unmute, says
nothing for sure (test mode never mutes, people unmute friends), so those and unanswered questions are left out.
Entries are word-list lines, so the manifest holds real words: keep it private. Stats only ever print masked words."""
import argparse
import csv
import datetime
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIELDS = ["id", "clip", "kind", "entry", "label", "who", "implicit", "transcript", "p", "event"]


def load(state):
    f = Path(state) / "review" / "items.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def labelled(it, now):
    """(label, who, implicit) or None for a case without an answer."""
    lab = it.get("label")
    if lab is None or it.get("labelled_by") not in ("page", "notify"):
        return None
    if it["kind"] == "who":
        return ("wrong", None, False) if lab == "wrong" else ("correct", lab, False)
    return lab, it.get("picked") if lab == "correct" else None, False


def rows(items, state, now=None):
    now = now or datetime.datetime.now()
    out = []
    for it in items:
        lab = labelled(it, now)
        if lab:
            clip = Path(state) / "review" / it["clip"] if it.get("clip") else None
            out.append({"id": it["id"], "clip": str(clip) if clip and clip.exists() else "", "kind": it["kind"],
                        "entry": it["entry"] if not it.get("reason") and it["kind"] != "spam" else "", "label": lab[0], "who": lab[1] or "",
                        "implicit": lab[2], "transcript": it.get("transcript", ""), "p": it.get("p"), "event": it.get("event"),
                        "rule": it["entry"] if it.get("reason") or it["kind"] == "spam" else ""})
    return out


def stats(items, rs):
    print(f"{len(items)} cases, {sum(1 for r in rs if r['clip'])} labelled with a clip")
    for kind in ("word", "who", "spam", "mute"):
        k = [i for i in items if i["kind"] == kind]
        lab = Counter(r["label"] for r in rs if r["kind"] == kind)
        print(f"  {kind:5} {len(k):4} cases: {lab['correct']} correct, {lab['wrong']} wrong, "
              f"{len(k) - lab['correct'] - lab['wrong']} unlabelled")
    ps = [i["p"] for i in items if i["kind"] == "word" and i.get("p") is not None]
    if ps:
        print(f"  word probability of asked cases: {min(ps):.2f}..{max(ps):.2f}")
    bal = Counter(r["label"] for r in rs)
    if bal:
        print(f"  balance: {bal['correct']} correct / {bal['wrong']} wrong ({100 * bal['wrong'] / sum(bal.values()):.0f}% wrong)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("DOGMUTE_STATE", str(Path.home() / ".local/state/dogmute")))
    ap.add_argument("--out", help="write the dataset here")
    a = ap.parse_args()
    items = load(a.state)
    rs = rows(items, a.state)
    stats(items, rs)
    if not a.out:
        return
    out = Path(a.out)
    (out / "clips").mkdir(parents=True, exist_ok=True)
    for r in rs:
        if r["clip"]:
            dst = out / "clips" / Path(r["clip"]).name
            shutil.copy(r["clip"], dst)
            r["clip"] = f"clips/{dst.name}"
    with open(out / "manifest.jsonl", "w") as f:
        for r in rs:
            f.write(json.dumps(r) + "\n")
    with open(out / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS + ["rule"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rs)
    print(f"wrote {len(rs)} rows to {out}/manifest.jsonl and manifest.csv")


if __name__ == "__main__":
    main()
