"""Word matching. The phrases that must be caught are base64 here (like the shipped list) so the source doesn't display slurs."""
import base64

from dogmute.core import default_words
from dogmute.words import WordList, masked

WL = WordList(default_words())
d = lambda s: base64.b64decode(s).decode()

# (phrase, the list entry it must hit; "" = any entry)
CATCH = [
    ("eW91J3JlIGEgbmlnZ2Vy", "bmlnZ2Vy"),
    ("TklJSUlHR0Egd2hhdA==", "bmlnZ2E="),
    ("bjFnZzRzIGxvbA==", "bmlnZ2E="),
    ("c2h1dCB1cCBmYWdnb3Qu", "ZmFnZ290"),
    ("RmFnZ290cyE=", "ZmFnZ290"),
    ("c2llZyBoZWlsIG1hbg==", "c2llZyBoZWls"),
    ("eW91IGFyZSBhIHNhbmQgbmlnZ2Vy", "c2FuZCBuaWdnZXI="),
    ("bioqKipy", "bmlnZ2Vy"),
    ("c2h1dCB1cCBuaWdh", ""),
    ("bmlnZ3VocyBicm8=", ""),
    # whisper's other ways of writing them: split over tokens, hyphens, spelled out, censored, typos, run together
    ("c2h1dCB1cCB5b3UgZmFnIGdvdA==", "ZmFnZ290"),
    ("ZmFnZ290LWFzcw==", "ZmFnZ290"),
    ("aGUgc2FpZCBOIEkgRyBHIEUgUiB0byBtZQ==", "bmlnZ2Vy"),
    ("YW5kIEkgbi1pLWctZy1hIGFnYWlu", "bmlnZ2E="),
    ("bi0tLS0t", ""),
    ("bioqKipz", ""),
    ("ZioqKip0", ""),
    ("ZmEqb3Q=", ""),
    ("eW91IHRvd2VsIGhlYWQ=", "dG93ZWxoZWFk"),
    ("d2V0IGJhY2s=", "d2V0YmFjaw=="),
    ("cmFnIGhlYWQ=", "cmFnaGVhZA=="),
    ("cG9jaCBtb25rZXk=", "cG9yY2ggbW9ua2V5"),
    ("cG9hY2ggbW9ua2V5", "cG9yY2ggbW9ua2V5"),
    ("c2FuZG5pZ2dlcg==", "c2FuZCBuaWdnZXI="),
    ("aGVpbGhpdGxlcg==", "aGVpbCBoaXRsZXI="),
    ("Y2hpbiBjaG9uZw==", "Y2hpbmcgY2hvbmc="),
    ("amlhYm9v", "amlnYWJvbw=="),
    ("amFnYWJvbw==", "amlnYWJvbw=="),
    ("d2V0YmFr", "d2V0YmFjaw=="),
    ("dG9lbGhlYWQ=", "dG93ZWxoZWFk"),
    ("cG9yY2ggbW9ua2V5", "cG9yY2ggbW9ua2V5"),
    ("eW8gZ2lnYWJvbw==", "Z2lnYWJvbw=="),
    ("bmlnZyBsb2w=", "bmlnZw=="),
    ("aGUgaXMgYSBiZWVuZXI=", "YmVlbmVy"),
    ("c2luZyBoZWls", "c2luZyBoZWls"),
    ("eW91IGhhaWwgaGl0bGVy", "aGFpbCBoaXRsZXI="),
    ("eW91IGhpZ2hsIGhpdGxlcg==", "aGlnaGwgaGl0bGVy"),
    ("eW91IGJlaW5lcg==", "YmVpbmVy"),
]

CLEAN = ["Niger", "she began to niggle", "a niggling doubt", "he sniggered", "bigger and bigger", "a digger", "Tigger", "a jigger",
         "figure it out", "his vigor", "a bundle of fagots", "can't", "cant", "spic and span", "spick and span", "fresh spices",
         "the chink in his armor", "royal Troon", "porch money", "the porch light", "a poor monkey", "pork monkey", "sand digger",
         "Pakistani", "pack it up", "a package", "park it", "a chicken", "china", "chinook", "a chip", "kite", "like", "mike", "bike",
         "good book", "cool", "a cocoon", "tranquil", "troop", "wet", "wetter", "a towel", "towels", "ragged", "a rag doll",
         "heil", "Hitler was a dictator", "hail to the chief", "I a m", "U S A", "N B A", "F B I", "a b c d e", "I D K",
         "f****ing", "f*****", "s***", "sh*t", "b*****", "n***", "n**", "ok...", "wait...", "well---", "go--go--go", "no-no-no", "x-ray", "e-mail",
         "retardant", "gooey", "a gawky kid", "fog", "pig", "tag", "a flag", "magnet", "Fagan", "Nigel's bag", "I like bean er soup", "a cr ink"]
CLEAN += ["Niger is a country", "that's way bigger", "a raccoon ate it", "spicy food", "Pakistan won", "he said the n-word",
         "what the f***", "go go go", "the snigger", "a cocoon", "trigger warning", "big digger", "japan", "a chunk of it",
         "Nigeria is big", "a ninja", "benign apple", "Nigel", "night", "signal", "kangaroo"]


def hits(text, wl=WL):
    words = [(w, i, i + 0.5) for i, w in enumerate(text.split())]
    return sorted({e for e, _, _ in wl.find(words)})


def test_catches_variants():
    for p, w in CATCH:
        h = hits(d(p))
        assert h == [d(w)] if w else h, (d(p), h)


def test_leaves_normal_words():
    for s in CLEAN:
        assert hits(s) == [], (s, hits(s))


def test_own_words_stretch_and_plural():
    wl = WordList("kangaroo\nbad phrase\n/x+y+z/\nzap*")
    assert hits("ok, KANGAROOOS!", wl) == ["kangaroo"]
    assert hits("what a bad   phrase", wl) == ["bad phrase"]
    assert hits("xxyyz", wl) == ["/x+y+z/"]
    assert hits("zapping", wl) == ["zap*"]


def test_word_times_span_phrase():
    (e, a, b), = WordList("bad phrase\nphrase").find([("bad", 1.0, 1.3), ("phrase", 1.3, 1.9)])
    assert (e, a, b) == ("bad phrase", 1.0, 1.9)    # the longer entry wins and spans both words


def test_masked():
    assert masked("kangaroo") == "k*******"


def test_every_entry_hits_itself():
    for e in WL.entries:
        if not e.line.startswith("/"):
            assert e.line in hits(e.line.rstrip("*") + " ok"), masked(e.line)


def test_safe_phrases():
    wl = WordList("kangaroo\n!a kangaroo hop")
    assert hits("a kangaroo hop", wl) == [] and hits("a kangaroo", wl) == ["kangaroo"]
    assert WL.find([("the", 0, 1), ("chink", 1, 2), ("in", 2, 3), ("the", 3, 4), ("armor", 4, 5)]) == []


def test_split_hyphen_spelled_censored_fuzzy():
    wl = WordList("kangaroo\nblue koala")
    for t in ["kanga roo", "kang aroo", "kangaroo-ass", "k a n g a r o o", "so k, a, n, g, a, r, o, o", "k-a-n-g-a-r-o-o", "k-k-kangaroo",
              "ka******", "ka------", "kang***o", "k*ngar*o", "kangeroo", "kangroo", "kangarooo", "kangar0o", "kángaroo", "blue koalla", "blue koa1a", "blueKoala", "blue-koala"]:
        assert hits(t, wl), t
    for t in ["kanga", "k a n g", "a kan garo", "kang", "blue", "koala", "blue cola", "blu koala", "k***", "k*******", "kan*****************", "kangar"]:
        assert hits(t, wl) == [], t
