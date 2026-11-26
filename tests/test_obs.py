"""The overlay in OBS (dogmute/obs.py) on a fake OBS settings folder, and the updater's version logic (dogmute/update.py)."""
import json
import sys

import pytest

from dogmute import obs as O
from dogmute import update as U


def _fake_obs(tmp_path, monkeypatch, scenes=("Scene", "Webcam")):
    d = tmp_path / "obs-studio"
    (d / "basic/scenes").mkdir(parents=True)
    (d / "basic/profiles/Main").mkdir(parents=True)
    (d / "user.ini").write_text("[Basic]\nProfile=Main\nProfileDir=Main\nSceneCollection=Mine\nSceneCollectionFile=Mine\n")
    (d / "basic/profiles/Main/basic.ini").write_text("[Video]\nBaseCX=2560\nBaseCY=1440\n")
    doc = {"name": "Mine", "current_scene": scenes[0], "sources": [
        {"id": "dshow_input", "name": "Video Capture Device", "uuid": "cam", "settings": {}},
        *({"id": "scene", "name": n, "uuid": n, "settings": {"id_counter": 3, "items": [
            {"name": "Video Capture Device", "source_uuid": "cam", "id": 3}]}} for n in scenes)]}
    (d / "basic/scenes/Mine.json").write_text(json.dumps(doc))
    (d / "basic/scenes/Other.json").write_text(json.dumps(doc))
    monkeypatch.setattr(O, "dirs", lambda: [d])
    monkeypatch.setattr(O, "running", lambda: False)
    return d


def test_adds_the_overlay_on_top_of_every_scene_once(tmp_path, monkeypatch):
    d = _fake_obs(tmp_path, monkeypatch)
    state, _, added = O.setup(8853)
    assert (state, added) == ("added", ["Mine"])           # only the collection in use
    doc = json.loads((d / "basic/scenes/Mine.json").read_text())
    src = next(s for s in doc["sources"] if s["name"] == O.NAME)
    assert src["id"] == "browser_source" and src["settings"]["url"] == "http://127.0.0.1:8853/obs"
    assert (src["settings"]["width"], src["settings"]["height"]) == (2560, 1440)
    for sc in (s for s in doc["sources"] if s["id"] == "scene"):
        top = sc["settings"]["items"][-1]                    # last = drawn on top
        assert top["source_uuid"] == src["uuid"] and top["id"] == 4 == sc["settings"]["id_counter"]
        assert top["bounds"] == {"x": 2560.0, "y": 1440.0}
    assert (d / "basic/scenes/Mine.json.dogmute-bak").exists()
    assert "browser_source" not in (d / "basic/scenes/Other.json").read_text()
    assert O.setup(8853)[0] == "there"                       # never twice
    assert O.setup(8853, done=["Mine"])[0] == "there"


def test_deleted_in_obs_stays_deleted(tmp_path, monkeypatch):
    d = _fake_obs(tmp_path, monkeypatch)
    assert O.setup(8853, done=["Mine"])[2] == []
    assert O.NAME not in (d / "basic/scenes/Mine.json").read_text()


def test_obs_open_or_missing(tmp_path, monkeypatch):
    d = _fake_obs(tmp_path, monkeypatch)
    monkeypatch.setattr(O, "running", lambda: True)
    assert O.setup(8853)[0] == "running"
    assert O.NAME not in (d / "basic/scenes/Mine.json").read_text()   # OBS would write over it when it closes
    monkeypatch.setattr(O, "dirs", lambda: [])
    assert O.setup(8853)[0] == "no-obs"


def test_installer_switch_saves_the_choice_and_turns_off_the_own_camera(tmp_path, monkeypatch):
    _fake_obs(tmp_path, monkeypatch)
    monkeypatch.setenv("DOGMUTE_CONFIG", str(tmp_path / "conf"))
    assert O.main(["--obs-setup"]) == "added"
    cfg = json.loads((tmp_path / "conf/config.json").read_text())
    assert cfg["obs"] == {"auto": True, "added": ["Mine"]} and cfg["camera"]["enabled"] is False
    assert O.main(["--obs-setup", "off"]) == "off"
    assert json.loads((tmp_path / "conf/config.json").read_text())["obs"]["auto"] is False


def test_versions():
    assert U.newer("v1.2.0", "1.1.9") and U.newer("1.10.0", "1.9.3")
    assert not U.newer("v1.1.1", "1.1.1") and not U.newer("v1.0.0", "1.1.1")
    assert not U.newer("nightly", "1.0.0") and not U.newer("v2.0.0", None)


def test_release_with_checksum(monkeypatch):
    import io

    body = {"tag_name": "v1.2.0", "html_url": "https://github.com/x/releases/v1.2.0", "assets": [
        {"name": "DogMute-Setup.exe", "browser_download_url": "https://github.com/x/DogMute-Setup.exe",
         "digest": "sha256:" + "ab" * 32}]}
    monkeypatch.setattr(U, "_get", lambda url, timeout=15: io.BytesIO(json.dumps(body).encode()))
    assert U.latest() == {"version": "1.2.0", "page": body["html_url"], "setup": body["assets"][0]["browser_download_url"],
                          "sha256": "ab" * 32}


def _repo(tmp_path):
    """A GitHub stand-in with tags v1.0.0 and v1.1.0, and a checkout of it at v1.0.0."""
    import subprocess

    def g(*a, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)
    up = tmp_path / "up"
    up.mkdir()
    g("init", "-q", "-b", "main", cwd=up)
    for v in ("1.0.0", "1.1.0"):
        (up / "install.sh").write_text(f"echo {v}\n")
        g("add", ".", cwd=up)
        g("commit", "-qm", v, cwd=up)
        g("tag", f"v{v}", cwd=up)
    co = tmp_path / "co"
    g("clone", "-q", str(up), str(co), cwd=tmp_path)
    g("reset", "-q", "--hard", "v1.0.0", cwd=co)
    return co


class _Core:
    channel = None
    cfg = {"updates": {"check": True, "auto": True}}

    def notify(self, *a):
        pass


@pytest.mark.skipif(sys.platform != "linux", reason="Linux only (systemd, the Flatpak)")
def test_linux_patches_itself_through_its_own_systemd_unit(tmp_path, monkeypatch):
    co = _repo(tmp_path)
    monkeypatch.setattr(U, "ROOT", co)
    monkeypatch.setattr(U, "LINUX", True)
    monkeypatch.setattr(U, "WIN", False)
    monkeypatch.setattr(U, "in_service", lambda: True)
    monkeypatch.setenv("DOGMUTE_STATE", str(tmp_path / "state"))
    assert U.git_patchable()
    up = U.Updates(_Core())
    up.new = {"version": "1.1.0", "page": None, "setup": None, "sha256": None}
    assert up.view()["can_patch"] and up.view()["able"]
    ran = []
    monkeypatch.setattr(up, "_launch", ran.append)
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    up.patch_now()                      # the fake unit is never active, so this DogMute is still here: it didn't work
    assert "stopped early" in up.status and up._failed == "1.1.0"
    cmd = ran[0]
    assert cmd[:3] == ["systemd-run", "--user", "--collect"] and cmd[-1] == U._git("rev-parse", "v1.1.0").stdout.strip()
    assert U._git("describe", "--tags").stdout.strip() == "v1.0.0"   # the checkout moves only inside the unit


def test_linux_leaves_a_changed_checkout_alone(tmp_path, monkeypatch):
    co = _repo(tmp_path)
    (co / "install.sh").write_text("echo mine\n")
    monkeypatch.setattr(U, "ROOT", co)
    monkeypatch.setattr(U, "WIN", False)
    up = U.Updates(_Core())
    up.git = True
    up.new = {"version": "1.1.0", "page": None, "setup": None, "sha256": None}
    monkeypatch.setattr(up, "_launch", lambda cmd: (_ for _ in ()).throw(AssertionError("launched")))
    up.patch_now()
    assert "were changed here" in up.status


def test_no_self_update_outside_the_service(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "x")   # other systemd units (terminals, desktop apps) pass this on: not enough
    assert not U.in_service() and not U.git_patchable()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux only (systemd, the Flatpak)")
def test_flatpak_finds_obs_under_the_real_home_and_sees_it_running_by_its_sentinel(tmp_path, monkeypatch):
    home = tmp_path / "home"
    obs = home / ".config/obs-studio"
    (obs / ".sentinel").mkdir(parents=True)
    monkeypatch.setattr(O, "FLATPAK", True)
    monkeypatch.setattr(O.Path, "home", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".var/app/com.dogmosh.DogMute/config"))   # DogMute's own
    monkeypatch.delenv("HOST_XDG_CONFIG_HOME", raising=False)
    assert O.dirs() == [obs] and not O.running()
    (obs / ".sentinel/run_abc").write_text("")
    assert O.running()


def test_flatpak_updates_by_its_file_not_git(monkeypatch):
    monkeypatch.setattr(U, "FLATPAK", True)
    monkeypatch.setattr(U, "LINUX", True)
    monkeypatch.setattr(U, "in_service", lambda: True)
    assert not U.git_patchable()


def test_flatpak_launcher_only_opens_the_page_when_dogmute_runs(monkeypatch):
    from dogmute import flatpak as F
    opened, started = [], []
    monkeypatch.setattr(F, "up", lambda p: True)
    monkeypatch.setattr(F, "open_page", opened.append)
    monkeypatch.setattr(F, "supervise", lambda: started.append(1))
    monkeypatch.setattr(F, "port", lambda: 8853)
    monkeypatch.setattr(F.sys, "argv", ["dogmute"])
    F.main()
    monkeypatch.setattr(F.sys, "argv", ["dogmute", "--background"])
    F.main()
    assert opened == [8853] and not started
