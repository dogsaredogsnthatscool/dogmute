"""The overlay in OBS (dogmute/obs.py) on a fake OBS settings folder, and the updater's version logic (dogmute/update.py)."""
import json

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
