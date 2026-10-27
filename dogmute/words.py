"""Word list matching on whisper output: stretched letters, leetspeak, plurals, prefixes, phrases, regex,
asterisk-censored tokens ("n****r", "n-----"), words split over tokens ("fag got"), hyphen compounds and spelled-out letters."""
import re
import unicodedata

FUZZ_SINGLE, FUZZ_PHRASE = 7, 5   # shortest word that may carry a typo, alone / beside exact words of a phrase
LONE_LETTER = "n"   # the one slur that whisper censors down to its first letter ("n*****")
# innocent phrases that contain a listed word; hits inside them are dropped (a list line starting with "!" adds more)
SAFE = ["spic and span", "spick and span", "/chink in (?:the|his|her|my|your|our|their|its) armou?r/", "royal troon", "porch money"]
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def clean(word):
    """One whisper word -> lowercase letters (and '*' kept for censored words)."""
    word = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))   # accents off
    w = re.sub(r"(?<=\w)(?:[-_\u2013\u2014\u2022#]{3,}|[_\u2022#]{2,})", lambda m: "*" * len(m.group()), word.lower())  # "n-----" is censored
    w = re.sub(r"^\W+|[^\w*$@]+$", "", w)  # edge punctuation off first: "Faggots!" isn't leet
    w = w.translate(LEET)
    return re.sub(r"[^a-z*]", "", w)


def _stretch(token):
    # each run of a letter may be stretched: "boom" -> b+o{2,}m+ ; a single letter stays single-or-more
    out = []
    for m in re.finditer(r"(.)\1*", token):
        c, k = re.escape(m.group(1)), len(m.group(0))
        out.append(f"{c}+" if k == 1 else f"{c}{{{k},}}")
    return "".join(out)


def _plural(w):
    return r"(?:s|z|es)?" if re.search(r"[sxz]$|[cs]h$", w) else r"[sz]?"


def _fuzz(w):
    """Regex for w with one letter wrong, missing, extra or swapped (the first letter stays)."""
    # (a doubled letter is never dropped: that spells real words, like the country and the bundle of sticks)
    alts = {w[:i] + "[a-z]" + w[i + 1:] for i in range(1, len(w))} | {w[:i] + w[i + 1:] for i in range(1, len(w)) if w[i - 1:i] != w[i] != w[i + 1:i + 2]}
    alts |= {w[:i] + "[a-z]" + w[i:] for i in range(1, len(w) + 1)}
    alts |= {w[:i] + w[i + 1] + w[i] + w[i + 2:] for i in range(1, len(w) - 1) if w[i] != w[i + 1]}
    return "(?:" + "|".join(sorted(alts)) + ")"


class Entry:
    def __init__(self, line, fuzzy=True):
        self.line = line
        self.single = None  # plain single word, for censored-token matching
        self.srx = None     # the same word as spelled-out letters
        if len(line) > 2 and line.startswith("/") and line.endswith("/"):
            self.rx = re.compile(line[1:-1])
            return
        prefix = line.endswith("*")
        words = [clean(w) for w in line.rstrip("*").split()]
        words = [w for w in words if w]
        tail = "" if prefix else _plural(words[-1] if words else "")
        if len(words) == 1 and not prefix:
            self.single = w = words[0]
            self.srx = re.compile(_stretch(w) + tail)
            # a longer word may come split over two tokens ("fag got", "towel head")
            alts = [_stretch(w)] + [_stretch(w[:i]) + r"\s+" + _stretch(w[i:]) for i in range(3, len(w) - 2)]
            if fuzzy and len(w) >= FUZZ_SINGLE:   # a typo in a long word
                alts.append(_fuzz(w))
            body = "(?:" + "|".join(alts) + ")"
        else:   # the words of a phrase may also come run together; a long one may have a typo if the others are exact
            alts = [r"\s*".join(_stretch(w) for w in words)]
            if fuzzy and len(words) > 1 and not prefix:
                alts += [r"\s*".join(_fuzz(w) if j == i else _stretch(w) for j, w in enumerate(words))
                        for i, v in enumerate(words) if len(v) >= FUZZ_PHRASE]
            body = "(?:" + "|".join(alts) + ")"
        self.rx = re.compile(r"(?<![a-z])" + body + tail + ("" if prefix else r"(?![a-z])"))

    def censored(self, tok):
        s = self.single
        if not s or "*" not in tok:
            return False
        return any(_censored(tok, w) for w in (s, s + "s"))


def _censored(tok, w):
    """tok has asterisks standing in for letters of w: letters that show must line up, length may be one off."""
    m = re.fullmatch(r"([a-z]*)(\*+)([a-z]*)", tok)
    if not m:   # several star runs: same length, letters in place
        known = [(a, b) for a, b in zip(tok, w) if a != "*"]
        return len(tok) == len(w) and len(known) >= 2 and all(a == b for a, b in known)
    pre, post = m.group(1), m.group(3)
    k = len(pre) + len(post)
    return (w.startswith(pre) and w.endswith(post) and len(w) > k and abs(len(tok) - len(w)) <= (k >= 3)
            and (k >= 2 or (len(tok) >= 5 and pre == LONE_LETTER)))


def masked(entry):
    w = entry.strip("/*")
    return w[0] + "*" * (len(w) - 1) if len(w) > 1 else w


class WordList:
    def __init__(self, text, exact=()):
        """exact: lines that may not carry a typo (no fuzzy matching)."""
        self.entries, self.safe = [], [Entry(x, False) for x in SAFE]
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("!"):
                self.safe.append(Entry(line[1:].strip(), False))
            elif line:
                self.entries.append(Entry(line, line not in exact))

    def find(self, words):
        """words: [(text, start, end)] -> [(entry_line, start, end)] for every hit."""
        hits = self._find(words, self.entries)
        if hits and self.safe:
            safe = self._find(words, self.safe)
            hits = [h for h in hits if not any(k[1] <= h[1] and h[2] <= k[2] for k in safe)]
        return hits

    def _find(self, words, entries):
        whole = [(clean(w), i) for i, (w, _, _) in enumerate(words)]
        parts = [(clean(p), i) for i, (w, _, _) in enumerate(words) for p in re.split(r"[-_/\u2013\u2014]+", w)]  # "faggot-ass"
        hits = []
        for toks in (whole, parts):
            hits += self._scan([t for t in toks if t[0]], words, entries)
        hits += self._spelled(parts, words, entries)
        # one hit per spot: the longest entry wins ("bad phrase" over "phrase")
        hits.sort(key=lambda h: (h[1], h[1] - h[2]))
        out = []
        for h in hits:
            if not any(k[1] <= h[1] and h[2] <= k[2] for k in out):
                out.append(h)
        return out

    def _scan(self, toks, words, entries):
        # joined string with char offset -> word index
        joined, owner = "", []
        for t, i in toks:
            if joined:
                joined += " "
                owner.append(i)
            joined += t
            owner.extend([i] * len(t))
        hits = []
        for e in entries:
            for m in e.rx.finditer(joined):
                if m.end() > m.start():
                    hits.append((e.line, words[owner[m.start()]][1], words[owner[m.end() - 1]][2]))
            for t, i in toks:
                if e.censored(t):
                    hits.append((e.line, words[i][1], words[i][2]))
        return hits

    def _spelled(self, toks, words, entries):
        """Runs of single letters ("n i g g e r")."""
        hits, run = [], []
        for t, i in toks + [("", -1)]:
            if len(t) == 1 and t != "*":
                run.append((t, i))
                continue
            if len(run) >= 3:
                s = "".join(c for c, _ in run)
                for e in entries:
                    if e.srx:
                        for m in e.srx.finditer(s):
                            if m.end() > m.start():
                                hits.append((e.line, words[run[m.start()][1]][1], words[run[m.end() - 1][1]][2]))
            run = []
        return hits
