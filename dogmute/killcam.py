"""Killfeed camera: your webcam with a quickscope drawn over it each time DogMute mutes someone, passed on as a virtual
camera that Discord (or any app) can pick. Everything is drawn on this computer. The webcam is only open while you're
in a voice channel, or for a few seconds after Try it.

  in:  the default webcam, or the one picked in Settings (virtual cameras are never picked, so it can't film itself)
  out: OBS Studio's virtual camera on Windows and macOS (install OBS once; it doesn't have to run), or on Linux a
       v4l2loopback device labelled "DogMute camera" (never another app's loopback)

Without a clip: scope in (zoom + scope), the shot with recoil, hitmarker, "+100", scope out; the killfeed line (you, a
rifle, their name) holds 4.5 s. With a clip from the clip folder (Settings): it plays once over the picture (green: the
green screen drops out, screen: black drops out, full: it replaces the picture) and the hit lands at hit_at.

Offline: python -m dogmute.killcam sheet <picture> <out.jpg> [clip]   (a contact sheet of the timeline, no webcam)
"""
import glob
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

IN_S, HIT_S, OUT_AT, OUT_S = 0.16, 0.22, 0.55, 0.2     # scope in, the shot, scope out start, scope out length
HITMARK_S, XP_S, FEED_S = 0.32, 1.4, 4.5
NAME_S = 1.8          # the target's name stays this long after the shot (red), fading over the last 0.4 s
ZOOM = 1.6
MAX_CLIP_S = 8.0
TRY_S = 12.0          # Try it keeps the camera on this long outside a call
VIRTUAL = ("obs virtual", "dogmute", "unity video capture")   # cameras we may be writing to: never filmed
V4L = "/sys/class/video4linux"                     # Linux: every video device, and where the loopback ones live
V4L_VIRTUAL = "/sys/devices/virtual/video4linux"
FONTS = [os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "arialbd.ttf"),
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
NEED = {"win32": "no virtual camera yet: install OBS Studio (free, obsproject.com) once, then turn this off and on",
        "darwin": "no virtual camera yet: install OBS Studio (free, obsproject.com), start its virtual camera once, "
                  "then turn this off and on",
        "linux": "no virtual camera yet: sudo modprobe v4l2loopback exclusive_caps=1 card_label=\"DogMute camera\""}


def _ease(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


# ---- the pictures: drawn once per mute, at the camera's size ----

def _font(px):
    from PIL import ImageFont
    for f in FONTS:
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            pass
    return ImageFont.load_default(px)


def _text(d, xy, s, px, fill, squeeze=0.86):
    """Bold text squeezed to a condensed width (game HUDs use condensed faces), with a dark drop shadow."""
    from PIL import Image, ImageDraw
    f = _font(px)
    l, t, r, b = f.getbbox(s)
    im = Image.new("RGBA", (r - l + 6, b - t + 6), (0, 0, 0, 0))
    g = ImageDraw.Draw(im)
    g.text((3 - l + 2, 3 - t + 2), s, font=f, fill=(0, 0, 0, 170))
    g.text((3 - l, 3 - t), s, font=f, fill=fill)
    im = im.resize((max(1, int(im.width * squeeze)), im.height), Image.LANCZOS)
    d.alpha_composite(im, (int(xy[0]), int(xy[1])))
    return im.width


def _rifle(w):
    """A bolt-action sniper rifle seen from the side, white, w px wide (the killfeed's weapon icon)."""
    from PIL import Image, ImageDraw
    h = int(w * 0.28)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = ImageDraw.Draw(im)
    u = w / 100.0
    poly = [(0, 15), (6, 13), (30, 12), (34, 10), (64, 10), (66, 12), (100, 12), (100, 15), (66, 15), (60, 17), (40, 17),
            (36, 26), (31, 26), (32, 18), (20, 18), (14, 25), (2, 25), (0, 20)]
    g.polygon([(x * u, y * u) for x, y in poly], fill=(255, 255, 255, 235))
    g.rounded_rectangle((36 * u, 3 * u, 62 * u, 9 * u), radius=2 * u, fill=(255, 255, 255, 235))   # the scope
    g.rectangle((44 * u, 9 * u, 46 * u, 11 * u), fill=(255, 255, 255, 235))
    g.rectangle((54 * u, 9 * u, 56 * u, 11 * u), fill=(255, 255, 255, 235))
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 160), (2, 2), im.split()[3].point(lambda a: 255 if a else 0))
    sh.alpha_composite(im)
    return sh


class Layer:
    """An RGBA picture kept as its non-empty box: BGR and alpha, 0..1 float."""

    def __init__(self, rgba):
        ys, xs = np.nonzero(rgba[..., 3])
        if not len(ys):
            self.box = None
            return
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        crop = rgba[y0:y1, x0:x1].astype(np.float32) / 255
        self.c, self.a, self.box = crop[..., 2::-1].copy(), crop[..., 3:4].copy(), (y0, x0)

    def draw(self, out, k=1.0, scale=1.0, dy=0):
        """Over out (HxWx3 float, BGR) at opacity k; scale grows it around the picture's centre; dy moves it."""
        if self.box is None or k <= 0:
            return
        import cv2
        H, W = out.shape[:2]
        c, a, (y0, x0) = self.c, self.a, self.box
        if scale != 1.0:
            h, w = c.shape[:2]
            nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
            c = cv2.resize(c, (nw, nh), interpolation=cv2.INTER_LINEAR)
            a = cv2.resize(a, (nw, nh), interpolation=cv2.INTER_LINEAR)[..., None]
            y0, x0 = round(H / 2 + (y0 - H / 2) * scale), round(W / 2 + (x0 - W / 2) * scale)
        y0 += dy
        ya, xa = max(0, -y0), max(0, -x0)                       # clipped to the picture
        yb, xb = min(c.shape[0], H - y0), min(c.shape[1], W - x0)
        if yb <= ya or xb <= xa:
            return
        o = out[y0 + ya:y0 + yb, x0 + xa:x0 + xb]
        al = a[ya:yb, xa:xb] * k
        o *= 1 - al
        o += c[ya:yb, xa:xb] * al


def art(W, H, name, by):
    """The mute's still layers at the camera's size: hitmarker, +100, killfeed line, the name (white), the name (red)."""
    from PIL import Image, ImageDraw
    cx, cy = W // 2, H // 2
    hit = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(hit)
    gap, ln, th = H * 0.012, H * 0.034, max(2, round(H * 0.006))
    for col, wide in (((40, 40, 40, 200), th + 2), ((255, 255, 255, 255), th)):
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            d.line([(cx + sx * gap, cy + sy * gap), (cx + sx * (gap + ln), cy + sy * (gap + ln))], fill=col, width=wide)
    names = []
    for col in ((255, 255, 255, 255), (255, 70, 55, 255)):        # the target: white, then red once it's shot
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        px = round(H * 0.11)
        l, t, r, b = _font(px).getbbox(name or "?")
        if (r - l) * 0.86 > W * 0.8:                                  # long names shrink to fit
            px = max(8, int(px * W * 0.8 / ((r - l) * 0.86)))
            l, t, r, b = _font(px).getbbox(name or "?")
        _text(im, (cx - (r - l) * 0.86 / 2, cy - (b - t) / 2 - px * 0.1), name or "?", px, col)
        names.append(im)
    xp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _text(xp, (cx + W * 0.03, cy + H * 0.075), "+100", round(H * 0.06), (255, 246, 170, 255))
    feed = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = round(H * 0.052)
    x, y = W * 0.03, H * 0.82
    if by:
        x += _text(feed, (x, y), by, px, (140, 230, 140, 255)) + px * 0.5
    rifle = _rifle(round(px * 3.4))
    feed.alpha_composite(rifle, (int(x), int(y + px * 0.62 - rifle.height / 2)))
    x += rifle.width + px * 0.5
    _text(feed, (x, y), name or "?", px, (255, 96, 80, 255))
    return [Layer(np.array(im)) for im in (hit, xp, feed, *names)]


def scope_mask(W, H):
    """The scope over the picture: HxWx1 float, 0..1 opacity of black (outside the lens, and the reticle)."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy, r = (W - 1) / 2, (H - 1) / 2, H * 0.46
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    m = np.clip((dist - r) / 2.5, 0, 1)                                         # outside the lens
    m = np.maximum(m, np.clip((dist - r * 0.9) / (r * 0.1), 0, 1) * 0.55)       # a dark rim inside it
    thin, thick, post = max(1.0, H / 480), H / 70, r * 0.45
    dx, dy, inside = np.abs(xx - cx), np.abs(yy - cy), dist < r
    for line in ((dy < thin) & inside, (dx < thin) & inside,                  # thin crosshair
                 (dy < thick) & (dx > post) & inside, (dx < thick) & (yy - cy > post) & inside):   # posts left, right, below
        m = np.maximum(m, line.astype(np.float32))
    return m[..., None]


def zoom(img, z, kick=0.0):
    """Zoom z on the centre; kick px moves the picture up (the recoil)."""
    import cv2
    H, W = img.shape[:2]
    h, w = H / z, W / z
    y0 = min(max(0.0, (H - h) / 2 - kick), H - h)
    M = np.float32([[1 / z, 0, (W - w) / 2], [0, 1 / z, y0]])
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)


def key(out, c, how):
    """The clip frame c over out (both HxWx3 float BGR): green screen, black dropping out, or the whole picture."""
    if how == "full":
        return c
    if how == "screen":
        return 1 - (1 - out) * (1 - c)
    b, g, r = c[..., 0:1], c[..., 1:2], c[..., 2:3]
    rb = np.maximum(r, b)
    green = np.clip((g - rb - 0.08) * 6, 0, 1)                   # 1 = the green screen: see through
    c = np.concatenate([b, np.minimum(g, rb + 0.05), r], axis=2)   # green spill off the edges
    return c * (1 - green) + out * green


class Moment:
    """One mute's animation, drawn frame by frame onto the camera picture."""

    def __init__(self, name, by, clip="", how="green", hit_at=0.0):
        self.name, self.by, self.how = str(name)[:32], str(by)[:32], how if how in ("green", "screen", "full") else "green"
        self.clip_path, self.hit_at = clip, max(0.0, min(MAX_CLIP_S, float(hit_at or 0)))
        self.t0 = self.layers = self.clip = None
        self.clip_fps, self.clip_n, self.clip_i, self.clip_frame = 30.0, 0, -1, None
        self.done = False

    def _open_clip(self, W, H):
        import cv2
        cap = cv2.VideoCapture(str(self.clip_path))
        if not cap.isOpened():
            return
        self.clip, self.size = cap, (W, H)
        self.clip_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.clip_n = int(min(cap.get(cv2.CAP_PROP_FRAME_COUNT) or MAX_CLIP_S * self.clip_fps, MAX_CLIP_S * self.clip_fps))

    def _clip_at(self, e):
        """The clip's frame for e seconds in (read in order, never backwards), cover-cropped to the picture, or None."""
        import cv2
        want = int(e * self.clip_fps)
        while self.clip is not None and self.clip_i < want:
            ok, f = self.clip.read()
            if not ok or self.clip_i + 1 >= self.clip_n:
                self.clip.release()
                self.clip, self.clip_frame = None, None
                break
            self.clip_i += 1
            if self.clip_i == want:
                W, H = self.size
                s = max(W / f.shape[1], H / f.shape[0])
                w, h = max(W, round(f.shape[1] * s)), max(H, round(f.shape[0] * s))
                f = cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
                y0, x0 = (h - H) // 2, (w - W) // 2
                self.clip_frame = f[y0:y0 + H, x0:x0 + W].astype(np.float32) / 255
        return self.clip_frame

    def draw(self, frame, t, mask):
        """frame: HxWx3 uint8 BGR; t: seconds on any steady clock. Returns the frame with the moment drawn on it."""
        H, W = frame.shape[:2]
        if self.t0 is None:
            self.t0 = t
            self.layers = art(W, H, self.name, self.by)
            if self.clip_path:
                self._open_clip(W, H)
        e = t - self.t0
        clip = self.clip_path and (self.clip is not None or self.clip_frame is not None)
        hit = (self.hit_at or self.clip_n / self.clip_fps) if self.clip_n else HIT_S
        h = e - hit
        hitmark, xp, feed, white, red = self.layers
        out = frame.astype(np.float32) / 255
        if h < NAME_S:                                         # the target: their name in the middle, white, red once shot
            (white if h < 0 else red).draw(out, min(1.0, e / 0.12) * min(1.0, (NAME_S - h) / 0.4),
                                           1 + 0.2 * (1 - h / 0.12) if 0 <= h < 0.12 else 1.0)
        if self.clip_n:
            k = _ease((h + 0.3) / 0.25) if h < 0.2 else 1 - _ease((h - 0.2) / 0.25)   # aim down sights around the shot
            if k > 0:
                out = zoom(out, 1 + (ZOOM - 1) * k, H * 0.04 * math.exp(-h / 0.06) if h >= 0 else 0.0)
            c = self._clip_at(e) if clip else None
            if c is not None:
                out = key(out, c, self.how)
        elif e < OUT_AT + OUT_S:                               # the built-in scope
            k = _ease(e / IN_S) if e < OUT_AT else 1 - _ease((e - OUT_AT) / OUT_S)
            if k > 0:
                kick = H * 0.04 * math.exp(-(e - HIT_S) / 0.06) if e >= HIT_S else 0.0
                out = zoom(out, 1 + (ZOOM - 1) * k, kick) * (1 - mask * k)
        if 0 <= h < HITMARK_S:                                 # the hitmarker pops in a little big
            hitmark.draw(out, min(1.0, (HITMARK_S - h) / 0.12), 1.0 + 0.25 * (1 - h / 0.1) if h < 0.1 else 1.0)
        if 0 <= h < XP_S:                                      # "+100" drifts up
            xp.draw(out, min(1.0, (XP_S - h) / 0.4), dy=-int(H * 0.03 * h / XP_S))
        if 0 <= h < FEED_S:
            feed.draw(out, min(1.0, (FEED_S - h) / 0.5) * min(1.0, h / 0.08))
        if h >= FEED_S and self.clip is None:
            self.done = True
        return (np.clip(out, 0, 1) * 255).astype(np.uint8)


# ---- webcams in, the virtual camera out ----

def _helper_cameras():
    from .audio import HELPER, NO_WINDOW
    try:
        return json.loads(subprocess.run([str(HELPER), "cameras"], capture_output=True, timeout=10,
                                         creationflags=NO_WINDOW).stdout or b"{}")
    except Exception:
        return {}


def _linux_cameras():
    """Real capture devices: [(path, name)]. v4l2loopback devices (virtual, maybe our own output) are left out."""
    out = []
    for d in sorted(glob.glob(f"{V4L}/video*"), key=lambda p: int(p.rsplit("video", 1)[1])):
        try:
            if "/virtual/" in os.path.realpath(d) or Path(d, "index").read_text().strip() != "0":
                continue
            out.append((f"/dev/{os.path.basename(d)}", Path(d, "name").read_text().strip()))
        except (OSError, ValueError):
            pass
    return out


def _virtual(name):
    return any(v in name.lower() for v in VIRTUAL)


def cameras():
    """Webcams that can be picked: [{"key", "label"}]; key is what camera.device stores ("" = the default one)."""
    if sys.platform == "win32":
        return [{"key": n, "label": n} for n in _helper_cameras().get("mf", []) if not _virtual(n)]
    if sys.platform == "linux":
        return [{"key": p, "label": f"{n} ({p})"} for p, n in _linux_cameras() if not _virtual(n)]
    return []


def open_webcam(device):
    """(cv2.VideoCapture, label) for camera.device; raises RuntimeError worded for the page."""
    import cv2
    if sys.platform == "win32":
        names = _helper_cameras().get("mf", [])
        pick = [i for i, n in enumerate(names) if (n == device if device else not _virtual(n))]
        if names and not pick:
            raise RuntimeError("that webcam is gone" if device else "no webcam found")
        i = pick[0] if pick else 0
        args, label = (i, cv2.CAP_MSMF), names[i] if names else "the webcam"
    elif sys.platform == "linux":
        cams = [(p, n) for p, n in _linux_cameras() if (p == device if device else not _virtual(n))]
        if not cams:
            raise RuntimeError("that webcam is gone" if device else "no webcam found")
        args, label = (cams[0][0], cv2.CAP_V4L2), cams[0][1]
    else:
        args, label = (0, cv2.CAP_AVFOUNDATION), "the camera"
    cap = cv2.VideoCapture(*args)
    if not cap.isOpened():
        raise RuntimeError(f"couldn't open {label} (another app may be using it)")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 30)
    return cap, label


def _output():
    """pyvirtualcam's device argument. On Linux only a loopback labelled DogMute: other loopback devices belong to
    other apps (OBS, a VJ tool), and two programs writing one device would fight."""
    if sys.platform != "linux":
        return {}
    for d in sorted(glob.glob(f"{V4L_VIRTUAL}/video*")):
        try:
            if "dogmute" in Path(d, "name").read_text().lower():
                return {"device": f"/dev/{os.path.basename(d)}"}
        except OSError:
            pass
    raise RuntimeError(NEED["linux"])


class Killcam(threading.Thread):
    def __init__(self, core):
        super().__init__(daemon=True)
        self.core = core
        self.status = "off"
        self.moment = None
        self.until = 0.0
        self.fps = 0.0
        self._mask = None

    @property
    def c(self):
        return self.core.cfg["camera"]

    def wanted(self):
        return self.c["enabled"] and (self.core.channel is not None or time.monotonic() < self.until)

    def fire(self, name, by="", test=False):
        """A mute: the moment starts on the next camera frame. test also wakes the camera for a while outside a call."""
        if not self.c["enabled"]:
            return False
        if test:
            self.until = time.monotonic() + TRY_S
        from . import hook as hk
        h = self.core.cfg["hook"]
        clip = ""
        if h.get("clip") and h["clip"] in hk.clips(h):
            clip = str(Path(h["clip_dir"]).expanduser() / h["clip"])
        self.moment = Moment(name, by, clip, h.get("key", "green"), h.get("hit_at", 0.0))
        return True

    def apply(self, frame, t):
        m = self.moment
        if m is None:
            return frame
        H, W = frame.shape[:2]
        if self._mask is None or self._mask.shape[:2] != (H, W):
            self._mask = scope_mask(W, H)
        try:                                                   # a drawing problem must never stop the picture
            out = m.draw(frame, t, self._mask)
        except Exception as e:  # noqa: BLE001
            print("killcam:", repr(e)[:200], flush=True)
            self.moment = None
            return frame
        if m.done:
            self.moment = None
        return out

    def run(self):
        while True:
            if not self.wanted():
                self.status = "on: waits for a voice call" if self.c["enabled"] else "off"
                time.sleep(0.5)
                continue
            try:
                self._stream()
            except RuntimeError as e:
                self.status = str(e)
                time.sleep(3)
            except Exception as e:  # noqa: BLE001
                self.status = f"camera stopped: {e!r}"[:160]
                time.sleep(3)

    def _stream(self):
        import cv2
        try:
            import pyvirtualcam
        except ImportError:
            raise RuntimeError("the virtual camera part is missing: install DogMute again") from None
        device = self.c["device"]
        out = _output()                                        # no virtual camera: say so before the webcam light comes on
        cap, label = open_webcam(device)
        try:
            for _ in range(30):                                # webcams can take a moment to hand over the first frame
                ok, frame = cap.read()
                if ok:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"{label} sends no picture")
            H, W = frame.shape[:2]
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            try:
                vc = pyvirtualcam.Camera(W, H, min(60.0, max(10.0, fps)), fmt=pyvirtualcam.PixelFormat.BGR, **out)
            except RuntimeError as e:
                print("killcam: virtual camera:", e, flush=True)
                raise RuntimeError(NEED.get(sys.platform, NEED["linux"])) from None
            with vc:
                self.status = f"on: {label} → {vc.device}"
                print("killcam:", self.status, flush=True)
                n, t1 = 0, time.monotonic()
                while self.wanted() and self.c["device"] == device:
                    vc.send(self.apply(frame, time.monotonic()))
                    ok, frame = cap.read()
                    if not ok:
                        raise RuntimeError(f"{label} stopped sending pictures")
                    n += 1
                    if n % 60 == 0:
                        now = time.monotonic()
                        self.fps, t1 = 60 / (now - t1), now
        finally:
            cap.release()


def _sheet(argv):
    """python -m dogmute.killcam sheet <picture> <out.jpg> [clip] [green|screen|full] [hit_at]"""
    import cv2
    pic = cv2.resize(cv2.imread(argv[0]), (1280, 720))
    m = Moment("Rando123", "you", argv[2] if len(argv) > 2 else "", argv[3] if len(argv) > 3 else "green",
               float(argv[4]) if len(argv) > 4 else 0.0)
    mask = scope_mask(1280, 720)
    m.draw(pic, 100.0, mask)
    hit = (m.hit_at or m.clip_n / m.clip_fps) if m.clip_n else HIT_S
    tiles = []
    for e in [0.0] + [max(0.0, hit + d) for d in (-0.3, -0.15, -0.05, 0.0, 0.05, 0.15, 0.3, 0.6, 1.2, 2.0, 4.0)]:
        tile = cv2.resize(m.draw(pic, 100.0 + e, mask), (480, 270))
        cv2.putText(tile, f"{e:.2f}s", (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        tiles.append(tile)
    rows = [np.hstack(tiles[i:i + 4]) for i in range(0, 12, 4)]
    cv2.imwrite(argv[1], np.vstack(rows))
    print("wrote", argv[1])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sheet":
        _sheet(sys.argv[2:])
    else:
        print(__doc__)
