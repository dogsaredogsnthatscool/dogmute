"""The Windows paths. The first half runs on any OS; the rest needs Windows: Discord's named pipe, and (with
DOGMUTE_WIN_AUDIO=1 and a sound device, as the GitHub check sets up) speech heard through bin/dogmute-listen.exe."""
import asyncio
import json
import os
import struct
import subprocess
import sys
import time
import xml.dom.minidom

import numpy as np
import pytest

from dogmute import audio, rpc, windows

WIN = sys.platform == "win32"
on_windows = pytest.mark.skipif(not WIN, reason="Windows only")


def test_sources_put_discord_first_and_skip_ourselves():
    info = {"app_capture": True, "apps": [{"exe": "Discord.exe", "name": "Discord", "pid": 4},
                                          {"exe": "python.exe", "name": "Python", "pid": 5},
                                          {"exe": "Spotify.exe", "name": "Spotify", "pid": 6}],
            "devices": ["Speakers (Realtek)"]}
    got = {s["key"]: s["discord"] for s in audio.win_streams(info)}
    assert got == {"app:discord.exe": True, "app:spotify.exe": False, "all": False, "device:Speakers (Realtek)": False}


def test_old_windows_offers_only_whole_device_sound():
    got = [s["key"] for s in audio.win_streams({"app_capture": False, "apps": [{"exe": "Discord.exe"}], "devices": []})]
    assert got == ["all"]


def test_source_arguments():
    assert audio.win_source("") == ["app", "discord"]
    assert audio.win_source("app:spotify.exe") == ["app", "spotify.exe"]
    assert audio.win_source("device:Speakers (Realtek)") == ["device", "Speakers (Realtek)"]
    assert audio.win_source("all") == ["all"]
    assert audio.win_source("pw:stream") is None


def test_too_old_windows_is_said_plainly(monkeypatch):
    monkeypatch.setattr(audio, "WIN", True)
    msg, wait = audio.Capture(lambda x, t: None)._helper_status(4, "", "")
    assert "Windows 10 version 2004" in msg and wait == 10


def test_discord_pipe_is_found(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "listdir", lambda d: ["mojo.123", "discord-ipc-1", "discord-ipc-0", "discord-ipc-x"])
    assert rpc.find_socket() == r"\\.\pipe\discord-ipc-0"
    monkeypatch.setattr(os, "listdir", lambda d: ["mojo.123"])
    assert rpc.find_socket() is None


def test_toast_text_is_escaped():
    x = windows.toast_xml('DogMute muted <b>&"x"', 'said "k*******"', "http://127.0.0.1:8853/")
    doc = xml.dom.minidom.parseString(x)
    assert [t.firstChild.data for t in doc.getElementsByTagName("text")] == ['DogMute muted <b>&"x"', 'said "k*******"']
    assert doc.documentElement.getAttribute("launch") == "http://127.0.0.1:8853/"


def test_cuda_folders_from_pip_and_the_installer(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    for d in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cudnn/include"):
        (site / d).mkdir(parents=True)
    monkeypatch.setattr(sys, "path", [str(site)])
    assert [p.parent.name for p in windows.cuda_dirs()] == ["cublas", "cudnn"]


# ---- on Windows ----

def _frame(op, obj):
    b = json.dumps(obj).encode()
    return struct.pack("<II", op, len(b)) + b


@on_windows
def test_rpc_over_a_named_pipe():
    name = rf"\\.\pipe\dogmute-test-{os.getpid()}"
    seen = []

    class Discord(asyncio.Protocol):   # what the Discord app does: READY after the handshake
        def connection_made(self, t):
            self.t = t

        def data_received(self, data):
            op, n = struct.unpack("<II", data[:8])
            seen.append((op, json.loads(data[8:8 + n])))
            self.t.write(_frame(rpc.OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "data": {"user": {"id": "1", "username": "me"}}}))

    async def go():
        loop = asyncio.get_running_loop()
        [server] = await loop.start_serving_pipe(Discord, name)
        try:
            r = rpc.RPC("123", lambda e, d: None)
            await r.connect(name)
            r.close()
            await asyncio.sleep(0.1)   # let the pipe close before the loop does
            return r.user
        finally:
            server.close()

    assert asyncio.run(go()) == {"id": "1", "username": "me"}
    assert seen[0] == (rpc.OP_HANDSHAKE, {"v": 1, "client_id": "123"})   # then our goodbye (OP_CLOSE)


SAY = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
       "$s.Rate = -2; Start-Sleep -Milliseconds {wait}; $s.Speak('{text}'); Start-Sleep 8")   # stays open, like Discord
needs_sound = pytest.mark.skipif(not (WIN and os.environ.get("DOGMUTE_WIN_AUDIO") and audio.HELPER.exists()),
                                 reason="needs Windows, a sound device and bin/dogmute-listen.exe")


def _speak(text, wait_ms):
    return subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", SAY.format(text=text, wait=wait_ms)])


def _listen(args, seconds):
    p = subprocess.Popen([str(audio.HELPER), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    want = int(seconds * audio.RATE) * 4
    data = b""
    t = time.monotonic()
    while len(data) < want and time.monotonic() - t < seconds + 20:
        chunk = p.stdout.read(min(65536, want - len(data)))
        if not chunk:
            break
        data += chunk
    p.kill()
    err = p.stderr.read().decode(errors="replace")
    p.wait()
    print(" ".join(args), "->", err.strip())
    return np.frombuffer(data[:len(data) // 4 * 4], np.float32), err


def _words(x):
    from faster_whisper import WhisperModel
    segs, _ = WhisperModel("base.en", device="cpu", compute_type="int8").transcribe(x, language="en")
    return " ".join(s.text for s in segs).lower()


@needs_sound
def test_helper_lists_programs_and_devices():
    info = json.loads(subprocess.run([str(audio.HELPER), "list"], capture_output=True, timeout=20).stdout)
    print(info)
    assert info["build"] > 0 and isinstance(info["apps"], list) and info["devices"]


@needs_sound
def test_one_program_is_heard_and_not_the_other():
    """What DogMute relies on: listening to one program (Discord) hears it, and not another one playing too."""
    a = _speak("kangaroo. The word is kangaroo.", 1500)
    b = _speak("elephant. The word is elephant.", 6000)
    try:
        time.sleep(0.5)
        x, err = _listen(["pid", str(a.pid)], 10)
    finally:
        a.wait(30)
        b.wait(30)
    assert len(x) >= 9 * audio.RATE, err
    assert float(np.sqrt(np.mean(x ** 2))) > 1e-3, "silence"
    heard = _words(x)
    print("heard:", heard)
    assert "kangaroo" in heard and "elephant" not in heard


@needs_sound
def test_every_apps_sound_hears_both():
    a = _speak("kangaroo.", 1000)
    b = _speak("elephant.", 3500)
    try:
        x, err = _listen(["all"], 7)
    finally:
        a.wait(30)
        b.wait(30)
    heard = _words(x)
    print("heard:", heard)
    assert "kangaroo" in heard and "elephant" in heard


@needs_sound
def test_a_program_that_isnt_running_is_exit_2():
    r = subprocess.run([str(audio.HELPER), "app", "no-such-program"], capture_output=True, timeout=20)
    assert r.returncode == 2
