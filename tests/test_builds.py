"""The Windows, Mac and Flatpak builds leave PyAV out (dogmute/__init__.py): speech must still load and run without it."""
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NO_PYAV = 'import sys\nsys.modules["av"] = None   # as in the builds: PyAV is nowhere\nimport dogmute\n'


def _run(code, prelude=NO_PYAV):
    """Runs code in a fresh Python (this one has imported dogmute already); exit 77 skips the test."""
    pytest.importorskip("faster_whisper")
    p = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)], cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    if p.returncode == 77:
        pytest.skip(p.stdout.strip())
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_faster_whisper_loads_without_pyav():
    out = _run("""
        from faster_whisper import WhisperModel   # imports faster_whisper.audio, which does "import av"
        from faster_whisper.vad import get_vad_model
        import av
        get_vad_model()   # diar.py's voice detector
        assert av.__doc__.startswith("Stand-in"), av
        try:
            av.open
        except AttributeError as e:
            print(e)
        else:
            raise AssertionError("the stand-in has av.open")
    """)
    assert "give it numpy arrays" in out


def test_speech_runs_without_pyav():
    out = _run("""
        import sys
        import numpy as np
        from faster_whisper import WhisperModel
        try:
            m = WhisperModel("tiny.en", device="cpu", compute_type="int8", local_files_only=True)
        except Exception as e:  # noqa: BLE001
            print("no tiny.en model in the Hugging Face cache:", e); sys.exit(77)
        segs, _ = m.transcribe(np.zeros(16000, np.float32), language="en", word_timestamps=True)
        print("passes:", len(list(segs)))
    """)
    assert "passes:" in out


def test_pyav_is_used_where_it_is_installed():
    _run("""
        import importlib.util
        import sys
        if importlib.util.find_spec("av") is None:
            print("PyAV isn't installed here"); sys.exit(77)
        import dogmute
        import av
        assert av.__file__ and hasattr(av, "open"), av   # the real PyAV, not the stand-in
    """, prelude="")


def test_builds_leave_pyav_out():
    for spec in ("win/DogMute.spec", "mac/DogMute.spec"):
        excludes = re.search(r"excludes=\[([^\]]*)\]", (ROOT / spec).read_text()).group(1)
        assert '"av"' in excludes, spec
    wheels = [Path(s["url"]).name for s in json.loads((ROOT / "flatpak/python-deps.json").read_text())["sources"]]
    assert wheels and not [w for w in wheels if w.startswith("av-")]
