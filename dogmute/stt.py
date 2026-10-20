"""Offline speech-to-text on the call mix: faster-whisper (NVIDIA GPU or CPU int8), or mlx-whisper on an Apple Silicon
Mac's GPU. Audio stays in memory; text is only checked against the word list and then dropped."""
import importlib.util
import platform
import sys
import threading
import time

import numpy as np

from .audio import RATE
from .words import WordList

TEST_LIST = WordList("kangaroo")   # the setup wizard's check word (core.TEST_WORD)

WINDOW_S = 6.0   # look back this far each pass
STEP_S = 1.0     # pass every this often while someone talks
MLX_REPOS = {"tiny.en": "mlx-community/whisper-tiny.en-mlx", "base.en": "mlx-community/whisper-base.en-mlx",
             "small.en": "mlx-community/whisper-small.en-mlx", "distil-large-v3": "mlx-community/distil-whisper-large-v3"}


def apple_gpu():
    """An Apple Silicon Mac with mlx-whisper installed (speech on its GPU through Metal)."""
    return sys.platform == "darwin" and platform.machine() == "arm64" and importlib.util.find_spec("mlx_whisper") is not None


class Listener(threading.Thread):
    def __init__(self, core):
        super().__init__(daemon=True)
        self.core = core
        self.model = None
        self.mlx = None          # the mlx-whisper model repo when speech runs on an Apple GPU
        self.loaded = None
        self.latency_ms = None
        self.status = "loading speech model"
        self.recent = []  # (entry, t) already reported

    def _load(self):
        import ctranslate2
        from faster_whisper import WhisperModel
        c = self.core.cfg["stt"]
        self.loaded = (c["model"], c["threads"], c["device"])
        self.mlx = None
        self.status = f"loading {c['model']}"
        if c["device"] != "cpu" and apple_gpu():
            try:
                self.mlx = MLX_REPOS[c["model"]]
                self._mlx(np.zeros(RATE, np.float32))   # loads the model and warms up Metal, in this thread (MLX wants that)
                self.status = f"ready ({c['model']}, Apple GPU)"
                return
            except Exception as e:  # noqa: BLE001
                self.mlx = None
                print("stt: Apple GPU failed, using the CPU:", e, flush=True)
        gpu = c["device"] == "gpu" or (c["device"] == "auto" and ctranslate2.get_cuda_device_count() > 0)
        try:
            if gpu:
                self.model = WhisperModel(c["model"], device="cuda", compute_type="int8_float16")
                self.status = f"ready ({c['model']}, GPU)"
                return
        except Exception as e:  # noqa: BLE001  (no GPU / out of memory: the CPU instead)
            print("stt: GPU failed, using the CPU:", e, flush=True)
        name = c["model"] if c["model"].endswith(".en") else "base.en"   # the big model is too slow on a CPU
        self.model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=c["threads"])
        self.status = f"ready ({name}, CPU)"

    def _mlx(self, x):
        import mlx_whisper
        # verbose=None: mlx-whisper prints nothing (True would print what people said)
        return mlx_whisper.transcribe(x, path_or_hf_repo=self.mlx, language="en", word_timestamps=True, temperature=0.0,
                                      condition_on_previous_text=False, verbose=None)

    def transcribe(self, x, t0):
        """[(word, abs_start, abs_end)] for audio x that starts at monotonic time t0."""
        if self.mlx:
            return [(w["word"], t0 + w["start"], t0 + w["end"])
                    for s in self._mlx(x)["segments"] for w in s.get("words") or []]
        segs, _ = self.model.transcribe(x, language="en", beam_size=1, word_timestamps=True,
                                        condition_on_previous_text=False, vad_filter=False)
        return [(w.word, t0 + w.start, t0 + w.end) for s in segs for w in (s.words or [])]

    def check(self, x, t0):
        t = time.monotonic()
        words = self.transcribe(x, t0)
        self.latency_ms = int((time.monotonic() - t) * 1000)
        hits = []
        found = self.core.words.find(words)
        if self.core.setup_test["on"]:
            found += TEST_LIST.find(words)
        for entry, a, b in found:
            if any(e == entry and abs(a - ta) < 3.0 for e, ta in self.recent):
                continue  # same word seen in an overlapping window
            self.recent.append((entry, a))
            hits.append((entry, a, b))
        self.recent = [(e, ta) for e, ta in self.recent if t - ta < 30]
        return hits

    def run(self):
        try:
            self._load()
        except Exception as e:
            self.status = f"speech model failed: {e}"
            return
        seg_start, last = None, 0.0
        while True:
            time.sleep(0.1)
            c = self.core.cfg["stt"]
            if (c["model"], c["threads"], c["device"]) != self.loaded:
                try:
                    self._load()
                except Exception as e:
                    self.status = f"speech model failed: {e}"
                    time.sleep(5)
                    continue
            if not self.core.cfg["slurs"]["enabled"]:
                seg_start = None
                continue
            now = time.monotonic()
            first = self.core.talk_started(now)  # earliest start among people talking now, or None
            if first is not None and seg_start is None:
                seg_start = first - 0.3
            if seg_start is None:
                continue
            ended = first is None
            if now - last < STEP_S and not ended:
                continue
            last = now
            x, ts = self.core.ring.get(max(seg_start, now - WINDOW_S), now)
            if ended:
                seg_start = None
            if len(x) < RATE * 0.3:
                continue
            try:
                for entry, a, b in self.check(x, ts):
                    self.core.heard(entry, a, b)
            except Exception as e:
                print("stt error:", e, flush=True)
