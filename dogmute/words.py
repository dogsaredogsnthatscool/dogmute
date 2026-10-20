"""Word list matching on whisper output: stretched letters, leetspeak, plurals, prefixes, phrases, regex,
and asterisk-censored tokens ("n****r")."""
import re

LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def clean(word):
    """One whisper word -> lowercase letters (and '*' kept for censored words)."""
    w = re.sub(r"^\W+|[^\w*$@]+$", "", word.lower())  # edge punctuation off first: "Faggots!" isn't leet
    w = w.translate(LEET)
    return re.sub(r"[^a-z*]", "", w)


def _stretch(token):
    # each run of a letter may be stretched: "boom" -> b+o{2,}m+ ; a single letter stays single-or-more
    out = []
    for m in re.finditer(r"(.)\1*", token):
        c, k = re.escape(m.group(1)), len(m.group(0))
        out.append(f"{c}+" if k == 1 else f"{c}{{{k},}}")
    return "".join(out)


class Entry:
    def __init__(self, line):
        self.line = line
        self.single = None  # plain single word, for censored-token matching
        if len(line) > 2 and line.startswith("/") and line.endswith("/"):
            self.rx = re.compile(line[1:-1])
            return
        prefix = line.endswith("*")
        words = [clean(w) for w in line.rstrip("*").split()]
        words = [w for w in words if w]
        body = r"\s+".join(_stretch(w) for w in words)
        tail = "" if prefix else r"(?:s|z|es)?(?![a-z])"
        self.rx = re.compile(r"(?<![a-z])" + body + tail)
        if len(words) == 1 and not prefix:
            self.single = words[0]

    def censored(self, tok):
        s = self.single
        if not s or "*" not in tok or len(tok) != len(s):
            return False
        known = [(a, b) for a, b in zip(tok, s) if a != "*"]
        return len(known) >= 2 and all(a == b for a, b in known)


def masked(entry):
    w = entry.strip("/*")
    return w[0] + "*" * (len(w) - 1) if len(w) > 1 else w


class WordList:
    def __init__(self, text):
        self.entries = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                self.entries.append(Entry(line))

    def find(self, words):
        """words: [(text, start, end)] -> [(entry_line, start, end)] for every hit."""
        toks = [clean(w) for w, _, _ in words]
        # joined string with char offset -> word index
        joined, owner = "", []
        for i, t in enumerate(toks):
            if not t:
                continue
            if joined:
                joined += " "
                owner.append(i)
            joined += t
            owner.extend([i] * len(t))
        hits = []
        for e in self.entries:
            for m in e.rx.finditer(joined):
                if m.end() <= m.start():
                    continue
                a, b = owner[m.start()], owner[m.end() - 1]
                hits.append((e.line, words[a][1], words[b][2]))
            for i, t in enumerate(toks):
                if e.censored(t):
                    hits.append((e.line, words[i][1], words[i][2]))
        # one hit per spot: the longest entry wins ("bad phrase" over "phrase")
        hits.sort(key=lambda h: (h[1], h[1] - h[2]))
        out = []
        for h in hits:
            if not any(k[1] <= h[1] and h[2] <= k[2] for k in out):
                out.append(h)
        return out
