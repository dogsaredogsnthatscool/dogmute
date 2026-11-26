"""The DogMute overlay in OBS Studio: a Browser source showing this computer's /obs page (dogmute/static/obs.html) on
top of every scene, so each mute plays the quickscope over whatever OBS shows (your webcam included). Turn on OBS's
Virtual Camera and pick OBS Virtual Camera in Discord.

OBS keeps its scenes in JSON files and writes them back when it closes, so they are only edited while OBS is closed:
the scene collection in use gets the source once (a copy of the file is kept next to it, <name>.json.dogmute-bak).
A collection the overlay was added to is never touched again, so deleting the source in OBS keeps it deleted.

  DogMute.exe --obs-setup       the installer: add it now (or as soon as OBS is closed), and keep doing that
  DogMute.exe --obs-setup off   the installer, box unticked: never add it
"""
import configparser
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

NAME = "DogMute overlay"
PATH = "/obs"


def url(port):
    return f"http://127.0.0.1:{port}{PATH}"


FLATPAK = os.path.exists("/.flatpak-info")


def dirs():
    """OBS's settings folders on this computer (the Flatpak one too). Inside DogMute's own Flatpak, XDG_CONFIG_HOME is
    DogMute's folder, so OBS's is looked for under the real home as well."""
    if sys.platform == "win32":
        c = [Path(os.environ.get("APPDATA", "")) / "obs-studio"]
    elif sys.platform == "darwin":
        c = [Path.home() / "Library/Application Support/obs-studio"]
    else:
        c = [Path(os.environ.get("HOST_XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
             / "obs-studio", Path.home() / ".config/obs-studio",
             Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio"]
    return [d for d in dict.fromkeys(c) if d.is_dir()]


def running():
    if FLATPAK:   # the sandbox can't see other programs: OBS 31+ keeps a file in .sentinel while it runs
        return any(f.is_file() for d in dirs() for f in (d / ".sentinel").glob("run_*"))
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/fi", "imagename eq obs64.exe", "/fo", "csv", "/nh"], capture_output=True,
                                 text=True, timeout=10, creationflags=0x08000000).stdout
            return "obs64.exe" in out.lower()
        return subprocess.run(["pgrep", "-x", "OBS" if sys.platform == "darwin" else "obs"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ini(f, section):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(f.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, configparser.Error):
        return {}
    return dict(cp[section]) if cp.has_section(section) else {}


def _basic(d):
    """[Basic] of OBS's own settings: user.ini since OBS 31, global.ini before (user.ini wins)."""
    return {**_ini(d / "global.ini", "Basic"), **_ini(d / "user.ini", "Basic")}


def canvas(d, basic):
    v = _ini(d / "basic/profiles" / basic.get("profiledir", "") / "basic.ini", "Video")
    try:
        return int(v.get("basecx", 1920)), int(v.get("basecy", 1080))
    except ValueError:
        return 1920, 1080


def collections(d, basic):
    """The scene collection files to add to: the one in use, or all of them when OBS doesn't say."""
    sc = d / "basic/scenes"
    cur = sc / f"{basic.get('scenecollectionfile', '')}.json"
    if basic.get("scenecollectionfile") and cur.is_file():
        return [cur]
    return sorted(sc.glob("*.json")) if sc.is_dir() else []


def present(doc):
    return any(s.get("name") == NAME or (s.get("id") == "browser_source" and
                                         str(s.get("settings", {}).get("url", "")).rstrip("/").endswith(PATH))
               for s in doc.get("sources", []))


def add_to(doc, link, w, h):
    """Adds the overlay to one scene collection (OBS's JSON), on top of every scene. False when there is nothing to do."""
    scenes = [s for s in doc.get("sources", []) if s.get("id") == "scene"]
    if present(doc) or not scenes:
        return False
    sid = str(uuid.uuid4())
    doc["sources"].append({
        "id": "browser_source", "versioned_id": "browser_source", "name": NAME, "uuid": sid, "enabled": True,
        "muted": True, "volume": 1.0, "balance": 0.5, "flags": 0, "sync": 0, "mixers": 0, "monitoring_type": 0,
        "settings": {"url": link, "width": w, "height": h, "reroute_audio": False, "shutdown": False,
                     "restart_when_active": False},
        "private_settings": {}, "hotkeys": {}, "filters": []})
    for s in scenes:
        st = s.setdefault("settings", {})
        items = st.setdefault("items", [])
        n = max([st.get("id_counter", 0)] + [i.get("id", 0) for i in items]) + 1
        st["id_counter"] = n
        items.append({   # last in the list = drawn on top
            "name": NAME, "source_uuid": sid, "visible": True, "locked": True, "rot": 0.0,
            "pos": {"x": 0.0, "y": 0.0}, "scale": {"x": 1.0, "y": 1.0}, "align": 5,
            "bounds_type": 2, "bounds_align": 0, "bounds": {"x": float(w), "y": float(h)},
            "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "id": n,
            "group_item_backup": False, "scale_filter": "disable", "blend_method": "default", "blend_type": "normal",
            "private_settings": {}})
    return True


def setup(port, done=()):
    """Adds the overlay where it's missing. (state, sentence, collections added to now); state is one of
    "added", "there", "running", "no-obs", "no-scenes"."""
    ds = dirs()
    if not ds:
        return "no-obs", "OBS Studio isn't installed (obsproject.com, free).", []
    if running():
        return "running", "Close OBS once: DogMute adds its overlay while OBS is closed.", []
    added, seen = [], 0
    for d in ds:
        basic = _basic(d)
        w, h = canvas(d, basic)
        for f in collections(d, basic):
            seen += 1
            if f.stem in done:
                continue
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not add_to(doc, url(port), w, h):
                continue
            shutil.copy2(f, f.with_name(f.name + ".dogmute-bak"))
            tmp = f.with_name(f.name + ".dogmute-tmp")
            tmp.write_text(json.dumps(doc, indent=4), encoding="utf-8")
            os.replace(tmp, f)
            added.append(f.stem)
    if added:
        return "added", f"Added to OBS ({', '.join(added)}). Open OBS and click Start Virtual Camera.", added
    if not seen:
        return "no-scenes", "OBS has no scenes yet: open OBS once, close it, and DogMute adds its overlay.", []
    return "there", "The overlay is in OBS. In OBS, click Start Virtual Camera.", []


def main(argv):
    """--obs-setup [off], run by the installer: saves the choice in config.json and adds the overlay if OBS is closed."""
    if sys.platform == "win32":
        from .windows import conf_dir
        base = conf_dir()
    else:
        base = Path((os.environ.get("XDG_CONFIG_HOME") if FLATPAK else None) or Path.home() / ".config") / "dogmute"
    conf = Path(os.environ.get("DOGMUTE_CONFIG") or base)   # the same folder as dogmute/core.py's CONF, without its imports
    f = conf / "config.json"
    cfg = json.loads(f.read_text()) if f.exists() else {}
    o = cfg.setdefault("obs", {})
    o["auto"] = "off" not in argv
    state = "off"
    if o["auto"]:
        state, msg, added = setup(cfg.get("port", 8853), o.get("added", []))
        o["added"] = sorted(set(o.get("added", [])) | set(added))
        if added:
            cfg.setdefault("camera", {})["enabled"] = False   # OBS draws the quickscope now; DogMute's own camera would fight it
        print(msg, flush=True)
    conf.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cfg, indent=2) + "\n")
    return state
