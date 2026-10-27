"""The killfeed camera: the quickscope drawn on a picture, clips keyed over it, which webcam gets picked, and (on
Windows with OBS installed, as the GitHub check sets up) the picture through OBS's virtual camera and back."""
import json
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from dogmute import killcam as K  # noqa: E402

W, H = 640, 360
GREY = np.full((H, W, 3), 128, np.uint8)


def frames(m, until):
    """(seconds in, frame) every 1/30 s while the moment runs."""
    mask, t, out = K.scope_mask(W, H), 100.0, []
    while not m.done and t - 100.0 < until:
        out.append((t - 100.0, m.draw(GREY, t, mask)))
        t += 1 / 30
    return out


def corner(f):
    return float(f[:20, :20].mean())


def test_the_quickscope_timeline():
    m = K.Moment("Rando123", "you")
    got = frames(m, 10)
    at = lambda s: min(got, key=lambda x: abs(x[0] - s))[1]   # noqa: E731
    assert corner(at(0.3)) < 10                                 # in the scope: black around the lens
    assert at(K.HIT_S + 0.05)[H // 2 - 2:H // 2 + 3, W // 2 + 12:W // 2 + 20].max() > 200   # the hitmarker
    assert abs(corner(at(1.5)) - 128) < 2                       # scoped out again
    feed = at(2.0)[int(H * 0.8):int(H * 0.95), :W // 2]
    assert (np.abs(feed.astype(int) - 128).max(axis=2) > 60).sum() > 200   # the killfeed line is on
    assert m.done and K.HIT_S + K.FEED_S - 0.1 < got[-1][0] < K.HIT_S + K.FEED_S + 0.1


def test_a_green_screen_clip_plays_over_the_picture(tmp_path):
    p = str(tmp_path / "clip.mp4")
    w = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 180))
    for _ in range(30):                                         # 1 s: a red square on a green screen
        f = np.zeros((180, 320, 3), np.uint8)
        f[:] = (0, 255, 0)
        f[60:120, 130:190] = (0, 0, 255)
        w.write(f)
    w.release()
    m = K.Moment("Rando123", "you", p, "green", 0.0)
    got = frames(m, 10)
    mid = got[10][1]
    assert abs(corner(mid) - 128) < 12                          # the green dropped out: the picture shows through
    b, g, r = mid[H // 2 - 5:H // 2 + 5, W // 2 - 40:W // 2 - 30].reshape(-1, 3).mean(axis=0)
    assert r > 180 and g < 80                                   # the red square is over it
    assert m.clip_n == 30 and m.done


def test_linux_skips_loopback_devices(tmp_path, monkeypatch):
    real, virtual = tmp_path / "pci" / "video0", tmp_path / "virtual" / "video13"
    for d, name in ((real, "Integrated Camera"), (virtual, "moshcap")):
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "index").write_text("0\n")
    cls = tmp_path / "class"
    cls.mkdir()
    (cls / "video0").symlink_to(real)
    (cls / "video13").symlink_to(virtual)
    monkeypatch.setattr(K, "V4L", str(cls))
    assert K._linux_cameras() == [("/dev/video0", "Integrated Camera")]
    monkeypatch.setattr(K, "V4L_VIRTUAL", str(tmp_path / "virtual"))
    with pytest.raises(RuntimeError, match="v4l2loopback"):     # another app's loopback is never written to
        monkeypatch.setattr(sys, "platform", "linux")
        K._output()
    (virtual / "name").write_text("DogMute camera\n")
    assert K._output() == {"device": "/dev/video13"}


def test_windows_never_films_a_virtual_camera(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(K, "_helper_cameras", lambda: {"mf": ["OBS Virtual Camera", "Integrated Webcam", "Logi C920"]})
    assert [c["key"] for c in K.cameras()] == ["Integrated Webcam", "Logi C920"]


def fake_core(enabled=True, clip=""):
    return types.SimpleNamespace(channel={"id": "1"}, me={"username": "you"},
                                 cfg={"camera": {"enabled": enabled, "device": ""},
                                      "hook": {"clip": clip, "clip_dir": "", "key": "green", "hit_at": 0.0}})


def test_a_mute_fires_only_when_the_camera_is_on():
    kc = K.Killcam(fake_core(enabled=False))
    assert not kc.fire("Rando123") and kc.moment is None
    kc.core.cfg["camera"]["enabled"] = True
    assert kc.fire("Rando123") and kc.moment.name == "Rando123"
    kc.apply(GREY.copy(), 1.0)                                 # the first frame starts the clock
    out = kc.apply(GREY.copy(), 1.2)
    assert out.shape == GREY.shape and not np.array_equal(out, GREY)


def test_the_engine_fires_it_on_a_mute(monkeypatch):
    from dogmute import core as C
    c = C.Core()
    c.cfg["camera"]["enabled"] = True
    c.add_log("muted", "A", "Rando123", 'said "k*******"')
    assert c.killcam.moment is not None and c.killcam.moment.name == "Rando123"
    c.killcam.moment = None
    c.add_log("would mute", "A", "Rando123", "test mode")
    assert c.killcam.moment is None


# ---- on Windows, with OBS Studio's virtual camera ----

class FakeWebcam:
    def __init__(self):
        self.n = 0

    def read(self):
        time.sleep(1 / 30)
        self.n += 1
        return True, GREY.copy()

    def get(self, prop):
        return 30.0

    def release(self):
        pass


def _obs_index():
    from dogmute.audio import HELPER
    import subprocess
    names = json.loads(subprocess.run([str(HELPER), "cameras"], capture_output=True, timeout=20).stdout).get("dshow", [])
    print("DirectShow cameras:", names)
    return next((i for i, n in enumerate(names) if "obs virtual" in n.lower()), None)


@pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("DOGMUTE_WIN_CAMERA"),
                    reason="needs Windows with OBS Studio's virtual camera")
def test_the_quickscope_reaches_obs_virtual_camera(monkeypatch):
    """What Discord sees: the fake webcam's grey picture through OBS Virtual Camera, and the scope on it after a mute."""
    i = _obs_index()
    assert i is not None, "OBS Virtual Camera isn't registered"
    monkeypatch.setattr(K, "open_webcam", lambda device: (FakeWebcam(), "a fake webcam"))
    kc = K.Killcam(fake_core())
    kc.start()
    for _ in range(100):
        if kc.status.startswith("on:"):
            break
        time.sleep(0.1)
    assert kc.status.startswith("on:"), kc.status
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    assert cap.isOpened()
    seen = []
    t = time.monotonic()
    fired = False
    while time.monotonic() - t < 8:
        ok, f = cap.read()
        if not ok:
            continue
        seen.append((time.monotonic() - t, corner(f), float(f[f.shape[0] // 2 - 40:f.shape[0] // 2 + 40].mean())))
        if not fired and time.monotonic() - t > 2:
            kc.fire("Rando123")
            fired = True
    cap.release()
    print("seen:", [(round(a, 2), round(b), round(c)) for a, b, c in seen[::10]])
    assert any(abs(c - 128) < 10 for _, c, _ in seen[:20]), "the webcam's picture didn't come through"
    assert any(c < 15 and m > 40 for _, c, m in seen), "the scope never showed"
