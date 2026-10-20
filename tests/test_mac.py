"""The macOS paths, run on any OS: Discord's socket in $TMPDIR, sound sources, the helper's exit codes, MLX speech."""
import sys
import textwrap
import time
import types

import numpy as np

from dogmute import audio, rpc, stt


def test_socket_in_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("TMPDIR", str(tmp_path) + "/")
    (tmp_path / "discord-ipc-0").touch()   # a plain file: macOS's long pytest paths don't fit a socket address
    assert rpc.find_socket() == str(tmp_path / "discord-ipc-0")


def test_sources_never_a_microphone():
    info = {"apps": [{"bundle": "com.hnc.Discord", "name": "Discord"}, {"bundle": "com.hnc.Discord.helper", "name": "Discord Helper"},
                     {"bundle": "com.spotify.client", "name": "Spotify"}, {"bundle": "org.python.python", "name": "Python"}],
            "devices": ["BlackHole 2ch", "MacBook Pro Microphone"]}
    got = {s["key"]: s["discord"] for s in audio.mac_streams(info)}
    assert got == {"app:com.hnc.Discord": True, "app:com.spotify.client": False, "all": False, "device:BlackHole 2ch": False}
    assert audio.mac_source("") == ["app", "com.hnc.Discord"]
    assert audio.mac_source("device:BlackHole 2ch") == ["device", "BlackHole 2ch"]
    assert audio.mac_source("device:MacBook Pro Microphone") is None
    assert audio.mac_source("all") == ["all"]


def _helper(tmp_path, body):
    p = tmp_path / "dogmute-listen"
    p.write_text(f"#!{sys.executable}\nimport sys\n" + textwrap.dedent(body))
    p.chmod(0o755)
    return p


def _capture(monkeypatch, helper, target=""):
    monkeypatch.setattr(audio, "MAC", True)
    monkeypatch.setattr(audio, "HELPER", helper)
    got = []
    cap = audio.Capture(lambda x, t: got.append(x), lambda: target)
    cap.start()
    return cap, got


def test_helper_sound_reaches_the_ring(tmp_path, monkeypatch):
    h = _helper(tmp_path, """
        import array, time
        assert sys.argv[1:3] == ["app", "com.hnc.Discord"], sys.argv
        sys.stdout.buffer.write(array.array("f", [0.25] * 3200).tobytes()); sys.stdout.flush()
        time.sleep(30)
    """)
    cap, got = _capture(monkeypatch, h)
    for _ in range(50):
        if len(got) >= 10:
            break
        time.sleep(0.1)
    cap.stop = True
    cap.proc.kill()
    assert len(got) == 10 and all(len(b) == audio.BLOCK and np.allclose(b, 0.25) for b in got)
    assert cap.status == "listening"


def test_helper_exit_codes_become_plain_status(tmp_path, monkeypatch):
    h = _helper(tmp_path, """
        sys.stderr.write("permission: screen\\n"); sys.exit(3)
    """)
    cap, _ = _capture(monkeypatch, h)
    for _ in range(50):
        if cap.status.startswith("macOS needs your OK"):
            break
        time.sleep(0.1)
    cap.stop = True
    assert cap.status == audio.PERMISSION["screen"]
    assert cap.asked   # the prompt is asked for once, not on every retry
    assert cap._mac_status(2, "", "")[0] == audio.WAITING
    assert cap._mac_status(3, "permission: microphone", "device:BlackHole 2ch")[0] == audio.PERMISSION["microphone"]


def test_apple_gpu_speech(monkeypatch):
    calls = []

    def transcribe(x, **kw):
        calls.append(kw)
        return {"segments": [{"words": [{"word": " kangaroo", "start": 0.5, "end": 1.0}]}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    monkeypatch.setattr(stt, "apple_gpu", lambda: True)
    core = types.SimpleNamespace(cfg={"stt": {"model": "distil-large-v3", "threads": 4, "device": "auto"}})
    li = stt.Listener(core)
    li._load()
    assert li.status == "ready (distil-large-v3, Apple GPU)"
    assert li.transcribe(np.zeros(16000, np.float32), 10.0) == [(" kangaroo", 10.5, 11.0)]
    assert all(c["path_or_hf_repo"] == "mlx-community/distil-whisper-large-v3" and c["verbose"] is None for c in calls)

