"""Every rule DogMute acts on, as data in ~/.config/dogmute/rules.json: the page lists them, changes their numbers, turns
them off, deletes them and adds new ones. Four sections: words (and safe phrases), noise, who said it, when a word counts.
Word rules hold real list words: the file is private like words.txt was."""
import copy
import json
import re
import threading
import uuid

from .words import WordList

SECTIONS = {"words": "Words", "noise": "Noise", "who": "Who said it", "count": "When a word counts"}
ACTIONS = ("mute", "ask", "log")
NOISE = ("loud", "clip", "blast", "nonstop", "flaps")
F, I, B, T = "float", "int", "bool", "text"

# type -> section, label, help, many (more than one allowed), actions, fields {name: (kind, min, max, unit, label)}
TYPES = {
    "word": {"section": "words", "label": "Word or phrase", "many": True, "actions": ACTIONS,
             "help": "word also catches stretched spellings (wooord), leetspeak (w0rd), plurals and censoring (w**d); "
                     "word* catches anything starting with it; a phrase matches its words in order; /regex/ is a regex.",
             "fields": {"text": (T, 1, 100, "", "Word"), "fuzzy": (B, None, None, "", "Allow a one-letter typo")}},
    "safe": {"section": "words", "label": "Safe phrase", "many": True, "actions": (),
             "help": "A list word inside this phrase never counts (spic and span).",
             "fields": {"text": (T, 1, 100, "", "Phrase")}},
    "loud": {"section": "noise", "label": "Loud", "many": True, "actions": ACTIONS,
             "help": "The call is at least this loud for this long. On a call that is loud itself, the level rises to "
                     "sit this far over the call's own loud talk, up to the ceiling. Leave a box empty to turn that part off.",
             "fields": {"db": (F, -40, 0, "dBFS", "Louder than"), "ms": (I, 50, 20000, "ms", "For"),
                        "voice_db": (F, 0, 20, "dB", "A voice gets this much more room"),
                        "over_call_db": (F, 0, 40, "dB", "Over the call's loud talk"),
                        "ceil_db": (F, -40, 0, "dBFS", "Always counts above"),
                        "steady_db": (F, 0, 30, "dB", "Never dips more than", True)}},
    "clip": {"section": "noise", "label": "Clipping", "many": True, "actions": ACTIONS,
             "help": "This share of samples is clipped (distorted) for this long.",
             "fields": {"frac": (F, 0.001, 1, "%", "Clipped share"), "ms": (I, 100, 5000, "ms", "For"),
                        "min_db": (F, -40, 0, "dBFS", "And louder than")}},
    "blast": {"section": "noise", "label": "Sudden blast", "many": True, "actions": ACTIONS,
              "help": "Someone alone on the mic is suddenly this much louder than their own normal voice.",
              "fields": {"jump_db": (F, 3, 60, "dB", "Louder than their voice by"),
                         "min_db": (F, -40, 0, "dBFS", "And louder than")}},
    "nonstop": {"section": "noise", "label": "Nonstop noise or music", "many": True, "actions": ACTIONS,
                "help": "A mic open this long with hiss, one steady tone, or without the gaps speech has.",
                "fields": {"after_s": (F, 3, 600, "s", "Open for"), "db": (F, -60, 0, "dBFS", "At or above"),
                           "dip_frac": (F, 0, 1, "%", "With gaps under")}},
    "flaps": {"section": "noise", "label": "Mic on/off spam", "many": True, "actions": ACTIONS,
              "help": "The mic flicks on and off this often with short audible bursts (three people at once is Discord).",
              "fields": {"count": (I, 3, 100, "", "Times"), "window_s": (F, 2, 60, "s", "In"),
                         "short_s": (F, 0.2, 3, "s", "Bursts no longer than")}},
    "who_solo": {"section": "who", "label": "Only one person talking", "many": False, "actions": (),
                 "help": "One person's mic was on around the word: it's theirs.",
                 "fields": {"slack_s": (F, 0, 3, "s", "Around the word, give or take")}},
    "who_tight": {"section": "who", "label": "On during the word", "many": False, "actions": (),
                  "help": "Several were on around it, but only one during the word itself (the audio delay is measured).",
                  "fields": {"tight_s": (F, 0, 1, "s", "Give or take")}},
    "who_openmic": {"section": "who", "label": "Open mics are background", "many": False, "actions": (),
                    "help": "Someone whose mic has been on nonstop this long is background, not who just spoke.",
                    "fields": {"open_mic_s": (F, 5, 300, "s", "On for")}},
    "who_diar": {"section": "who", "label": "Voices told apart", "many": False, "actions": (),
                 "help": "Diarization: the call audio is split by voice as it plays, and each voice is matched to "
                         "whoever's mic is on with it. Stricter numbers: fewer wrong picks, more \"can't tell\".",
                 "fields": {"min": (F, 0.3, 0.95, "", "Match at least"), "margin": (F, 0, 0.6, "", "Ahead of the next by")}},
    "who_voice": {"section": "who", "label": "Voice match", "many": False, "actions": (),
                  "help": "The word's voice against voices learned from moments each person talks alone.",
                  "fields": {"min": (F, 0.2, 0.95, "", "Match at least"), "margin": (F, 0, 0.5, "", "Ahead of the next by")}},
    "who_unclear": {"section": "who", "label": "Can't tell who", "many": False, "actions": ACTIONS,
                    "help": "Nothing above decided: mute everyone talking (friends never), ask you, or only log it.",
                    "fields": {"max": (I, 1, 10, "", "At most this many people")}},
    "count_sure": {"section": "count", "label": "One sure pass", "many": False, "actions": (),
                   "help": "Speech is transcribed about once a second; one pass this sure of a list word counts at once.",
                   "fields": {"p": (F, 0.3, 1, "%", "Sure at least")}},
    "count_agree": {"section": "count", "label": "Passes agree", "many": False, "actions": (),
                    "help": "Otherwise it waits for this many passes to hear it (whisper flips on sloppy speech). "
                            "Off: the first pass counts.",
                    "fields": {"votes": (I, 1, 5, "", "Passes")}},
    "count_doubt": {"section": "count", "label": "Doubtful: ask", "many": False, "actions": (),
                    "help": "A list word whisper is less sure of than this is asked about instead of muted.",
                    "fields": {"p": (F, 0, 1, "%", "Below")}},
    "count_near": {"section": "count", "label": "Near-miss: ask", "many": False, "actions": (),
                   "help": "Ask about words that nearly spell a list word (one person talking). Mutes nobody; mostly "
                           "everyday words.", "fields": {"similarity": (F, 0.5, 1, "%", "Similar at least")}},
}

BUILTIN = [
    {"id": "earrape", "type": "loud", "name": "Earrape", "db": -6.0, "ms": 400, "voice_db": 4.0, "over_call_db": 8.0,
     "ceil_db": -2.0, "steady_db": None},
    {"id": "clipping", "type": "clip", "name": "Clipping", "frac": 0.03, "ms": 500, "min_db": -16.0},
    {"id": "screaming", "type": "loud", "name": "Screaming", "db": -12.0, "ms": 2400, "voice_db": 0.0, "over_call_db": 3.0,
     "ceil_db": -4.0, "steady_db": 4.0},
    {"id": "blast", "type": "blast", "name": "Sudden blast", "jump_db": 20.0, "min_db": -16.0},
    {"id": "nonstop", "type": "nonstop", "name": "Nonstop noise or music", "after_s": 10.0, "db": -30.0, "dip_frac": 0.05},
    {"id": "micspam", "type": "flaps", "name": "Mic spam", "count": 12, "window_s": 10.0, "short_s": 1.0},
    {"id": "who_solo", "type": "who_solo", "slack_s": 0.5},
    {"id": "who_tight", "type": "who_tight", "tight_s": 0.1},
    {"id": "who_openmic", "type": "who_openmic", "open_mic_s": 20.0},
    {"id": "who_diar", "type": "who_diar", "min": 0.65, "margin": 0.25},
    {"id": "who_voice", "type": "who_voice", "min": 0.45, "margin": 0.12},
    {"id": "who_unclear", "type": "who_unclear", "max": 3, "action": "mute"},
    {"id": "count_sure", "type": "count_sure", "p": 0.8},
    {"id": "count_agree", "type": "count_agree", "votes": 2},
    {"id": "count_doubt", "type": "count_doubt", "p": 0.25},
    {"id": "count_near", "type": "count_near", "on": False, "similarity": 0.75},
]


def _norm(r):
    """A rule with every field it can have: on, action, name."""
    t = TYPES[r["type"]]
    out = {"id": r["id"], "type": r["type"], "on": r.get("on", True), "name": r.get("name") or t["label"]}
    if t["actions"]:
        out["action"] = r.get("action", "mute")
    for k in t["fields"]:
        out[k] = r.get(k, False if t["fields"][k][0] == B else None)
    return out


def builtin(section=None):
    return [_norm(copy.deepcopy(r)) for r in BUILTIN if section in (None, TYPES[r["type"]]["section"])]


def word_rules(text):
    """words.txt lines -> word / safe rules (comments dropped)."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("!") and line[1:].strip():
            out.append({"type": "safe", "text": line[1:].strip()})
        elif line:
            out.append({"type": "word", "text": line, "fuzzy": True, "action": "mute"})
    return [_norm({**r, "id": _id()}) for r in out]


def legacy(cfg, words_text):
    """The rules the old settings meant (config.json's spam/slurs/review numbers and the word list), for the first start."""
    sp, sl, rv = cfg.get("spam", {}), cfg.get("slurs", {}), cfg.get("review", {})
    rules = builtin()
    by = {r["id"]: r for r in rules}
    ear = sp.get("earrape_db", -6.0)
    by["earrape"].update(db=ear, ms=sp.get("earrape_ms", 400))
    by["screaming"].update(db=ear - 6, ms=6 * sp.get("earrape_ms", 400))
    by["clipping"]["frac"] = sp.get("clip_frac", 0.03)
    by["blast"]["jump_db"] = sp.get("jump_db", 20.0)
    by["nonstop"].update(after_s=sp.get("continuous_s", 10.0), db=sp.get("continuous_db", -30.0),
                         dip_frac=sp.get("continuous_dip_frac", 0.05))
    by["micspam"]["count"] = sp.get("flaps_per_10s", 12)
    by["who_solo"]["slack_s"] = sl.get("slack_s", 0.5)
    by["who_openmic"]["open_mic_s"] = sl.get("open_mic_s", 20.0)
    by["who_voice"]["on"] = sl.get("voice", True)
    by["who_unclear"]["action"] = {"mute": "mute", "ask": "ask"}.get(sl.get("unclear"), "mute")
    by["count_doubt"]["p"] = rv.get("word_p", 0.25)
    by["count_near"]["on"] = bool(rv.get("near", False))
    return word_rules(words_text) + rules


def _id():
    return "r" + uuid.uuid4().hex[:8]


def check(r):
    """Raise ValueError (plain words) for a rule that can't work; returns it with numbers as numbers."""
    t = TYPES.get(r.get("type"))
    if not t:
        raise ValueError("unknown kind of rule")
    if t["actions"] and r.get("action") not in t["actions"]:
        raise ValueError(f"action is one of {', '.join(t['actions'])}")
    name = str(r.get("name") or "").strip()
    if not name or len(name) > 40:
        raise ValueError("a name of 1 to 40 letters")
    r["name"] = name
    r["on"] = bool(r.get("on"))
    for k, (kind, lo, hi, _, label, *nullable) in t["fields"].items():
        v = r.get(k)
        if kind == B:
            r[k] = bool(v)
        elif kind == T:
            v = str(v or "").strip()
            if not v or len(v) > hi or "\n" in v or v[0] in "!#" or "#" in v:
                raise ValueError(f"{label}: 1 to {hi} letters on one line, no # and not starting with !")
            if v.startswith("/") and v.endswith("/") and len(v) > 2:
                try:
                    re.compile(v[1:-1])
                except re.error as e:
                    raise ValueError(f"{label}: that regex doesn't work ({e})") from None
            r[k] = v
        elif v in (None, "") and nullable:
            r[k] = None
        else:
            try:
                v = (int if kind == I else float)(float(v))
            except (TypeError, ValueError):
                raise ValueError(f"{label}: a number") from None
            if not lo <= v <= hi:
                raise ValueError(f"{label}: {lo:g} to {hi:g}")
            r[k] = v
    return r


class Rulebook:
    def __init__(self, path, cfg=None, words_text=""):
        self.path = path
        self.lock = threading.RLock()
        self.listeners = []
        try:
            saved = json.loads(path.read_text())["rules"]
            self.rules = []
            for r in saved:
                try:   # a field added since it was saved gets the built-in's number
                    base = next((b for b in BUILTIN if b["id"] == r.get("id")), None) or next(
                        (b for b in BUILTIN if b["type"] == r.get("type")), {})
                    r = {**{k: v for k, v in base.items() if k not in ("id", "name", "on")}, **r}   # (a saved None stays: "off")
                    self.rules.append(check(_norm(r)))
                except (KeyError, ValueError) as e:   # a hand edit gone wrong: drop that rule, keep the rest
                    print("rules: dropped", r.get("id"), e, flush=True)
        except (OSError, ValueError, KeyError):
            self.rules = legacy(cfg or {}, words_text)
            self._save()
        self._compile()

    # ---------- reading (live, from any thread) ----------
    def get(self, type):
        """The rule of a one-only type if it is on, else None."""
        return next((r for r in self.rules if r["type"] == type and r["on"]), None)

    def val(self, type, field, default):
        """A one-only rule's number, or `default` when it is off or deleted."""
        r = self.get(type)
        return default if r is None or r.get(field) is None else r[field]

    def of(self, *types):
        return [r for r in self.rules if r["type"] in types and r["on"]]

    def action(self, line):
        """What the word rule `line` does: mute / ask / log."""
        return self._actions.get(line, "mute")

    # ---------- changing (from the page) ----------
    def add(self, d):
        t = d.get("type")
        if t not in TYPES:
            raise ValueError("unknown kind of rule")
        with self.lock:
            if not TYPES[t]["many"] and any(r["type"] == t for r in self.rules):
                raise ValueError("there is one of these already")
            base = next((copy.deepcopy(r) for r in BUILTIN if r["type"] == t), {"type": t})
            base.pop("id", None)
            if TYPES[t]["many"] and TYPES[t]["section"] != "words":    # a new one of a kind: its own name, not the built-in's
                base.pop("name", None)
            r = _norm({**base, **{k: v for k, v in d.items() if k != "id"}, "id": _id(), "type": t})
            names, k = {x["name"] for x in self.rules}, 2
            while TYPES[t]["many"] and r["name"] in names and not d.get("name"):
                r["name"] = f"{TYPES[t]['label']} {k}"
                k += 1
            new = self._place(r)
            self._commit(new)
        return r

    def update(self, id, d):
        with self.lock:
            new, found = [], None
            for r in self.rules:
                if r["id"] == id:
                    found = check({**r, **{k: v for k, v in d.items() if k not in ("id", "type")}})
                    r = found
                new.append(r)
            if not found:
                raise ValueError("no such rule")
            self._commit(new)
        return found

    def delete(self, id):
        with self.lock:
            new = [r for r in self.rules if r["id"] != id]
            if len(new) == len(self.rules):
                raise ValueError("no such rule")
            self._commit(new)

    def reset(self, section):
        """Put back the built-in rules of a section as they ship (words: the shipped list). Others of it stay."""
        if section not in SECTIONS:
            raise ValueError("unknown section")
        with self.lock:
            if section == "words":
                from .core import default_words
                have = {(r["type"], r.get("text")) for r in self.rules}
                add = [r for r in word_rules(default_words()) if (r["type"], r["text"]) not in have]
                self._commit(add + self.rules)
                return
            ids = {b["id"] for b in BUILTIN}
            mine = [r for r in self.rules if TYPES[r["type"]]["section"] == section and r["id"] not in ids]
            new = [r for r in self.rules if TYPES[r["type"]]["section"] != section]
            for r in builtin(section) + mine:            # built-ins first, then yours, each in its type's place
                new = self._place(r, new)
            self._commit(new)

    def view(self):
        types = {k: {"section": t["section"], "label": t["label"], "help": t["help"], "many": t["many"],
                     "actions": list(t["actions"]),
                     "fields": {f: {"kind": s[0], "min": s[1], "max": s[2], "unit": s[3], "label": s[4],
                                    "blank": bool(s[5:])} for f, s in t["fields"].items()}}
                 for k, t in TYPES.items()}
        with self.lock:
            missing = [b["type"] for b in BUILTIN if not TYPES[b["type"]]["many"]
                       and not any(r["type"] == b["type"] for r in self.rules)]
            return {"sections": SECTIONS, "types": types, "rules": copy.deepcopy(self.rules), "missing": missing}

    # ---------- inside ----------
    def _place(self, r, rules=None):
        """rules + r: words first (newest on top), the rest where the built-ins put that type."""
        rules = list(self.rules if rules is None else rules)
        check(r)
        if TYPES[r["type"]]["section"] == "words":
            return [r] + rules
        order = [b["type"] for b in BUILTIN]
        k = order.index(r["type"]) if r["type"] in order else len(order)
        i = next((j for j, x in enumerate(rules) if x["type"] in order and order.index(x["type"]) > k), len(rules))
        return rules[:i] + [r] + rules[i:]

    def _commit(self, new):
        old = self.rules
        self.rules = new
        try:
            self._compile()
        except Exception:
            self.rules = old
            raise
        self._save()
        for f in self.listeners:
            f()

    def _compile(self):
        words = [r for r in self.rules if r["type"] in ("word", "safe") and r["on"]]
        text = "\n".join(("!" if r["type"] == "safe" else "") + r["text"] for r in words)
        self.words = WordList(text, exact={r["text"] for r in words if r["type"] == "word" and not r["fuzzy"]})
        self._actions = {r["text"]: r["action"] for r in words if r["type"] == "word"}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rules": self.rules}, indent=1) + "\n")
        tmp.chmod(0o600)
        tmp.replace(self.path)
