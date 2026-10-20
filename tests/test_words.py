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
]

CLEAN = ["Niger is a country", "that's way bigger", "a raccoon ate it", "spicy food", "Pakistan won", "he said the n-word",
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
