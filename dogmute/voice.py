"""Who said it, by voice: a speaker embedding (WeSpeaker ResNet34, ONNX, CPU) per person, learned from moments they talk
alone in the call. Profiles live in memory only, for the current call: cleared when you leave or switch channel and when
DogMute stops. Nothing about anyone's voice is ever written to disk."""
import threading

import numpy as np

REPO, FILE = "Wespeaker/wespeaker-voxceleb-resnet34-LM", "voxceleb_resnet34_LM.onnx"
RATE = 16000
MIN_S = 1.0          # shortest solo stretch worth learning from
MAX_SHOTS = 30       # embeddings kept per person (newest win)


def _mel_banks(n_mels=80, n_fft=512, low=20.0, high=8000.0):
    """Kaldi-style triangular filters (mel = 1127 ln(1 + f/700)), (n_mels, n_fft//2 + 1)."""
    mel = lambda f: 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)
    lo, hi = mel(low), mel(high)
    pts = lo + np.arange(n_mels + 2) * (hi - lo) / (n_mels + 1)
    fm = mel(np.arange(n_fft // 2 + 1) * RATE / n_fft)
    w = np.zeros((n_mels, n_fft // 2 + 1), np.float32)
    for m in range(n_mels):
        l, c, r = pts[m], pts[m + 1], pts[m + 2]
        up, down = (fm - l) / (c - l), (r - fm) / (r - c)
        w[m] = np.maximum(0, np.minimum(up, down))
    return w


_BANKS = _mel_banks()
_WIN = (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(400) / 399)).astype(np.float32)


def fbank(x):
    """80-dim log mel fbank like torchaudio.compliance.kaldi.fbank(x * 32768, 25 ms / 10 ms, hamming, dither 0), then CMN."""
    x = x.astype(np.float32) * 32768.0
    n = 1 + (len(x) - 400) // 160
    if n < 10:
        return None
    idx = np.arange(400)[None, :] + 160 * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * (1 - 0.97), fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    spec = np.abs(np.fft.rfft(fr * _WIN, 512)) ** 2
    f = np.log(np.maximum(spec @ _BANKS.T, np.finfo(np.float32).eps))
    return (f - f.mean(axis=0, keepdims=True)).astype(np.float32)


class Voices:
    def __init__(self):
        self.sess = None
        self.status = "loading"
        self.lock = threading.Lock()
        self.shots = {}   # uid -> [unit embeddings]

    def load(self):
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            o = ort.SessionOptions()
            o.intra_op_num_threads = 2
            self.sess = ort.InferenceSession(hf_hub_download(REPO, FILE), o, providers=["CPUExecutionProvider"])
            self.status = "ready"
        except Exception as e:  # noqa: BLE001  (no voice matching = the old timing-only behaviour)
            self.status = f"off ({e})"[:120]

    def embed(self, x):
        if self.sess is None:
            return None
        f = fbank(x)
        if f is None:
            return None
        e = self.sess.run(None, {"feats": f[None]})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)

    def learn(self, uid, x):
        e = self.embed(x)
        if e is not None:
            with self.lock:
                s = self.shots.setdefault(uid, [])
                s.append(e)
                del s[:-MAX_SHOTS]

    def known(self, uid):
        return len(self.shots.get(uid, ())) >= 2

    def score(self, x, uids):
        """{uid: cosine similarity of x to that person's voice} for the uids with a profile."""
        e = self.embed(x)
        if e is None:
            return {}
        out = {}
        with self.lock:
            for u in uids:
                s = self.shots.get(u)
                if s and len(s) >= 2:
                    m = np.mean(s, axis=0)
                    out[u] = float(e @ (m / (np.linalg.norm(m) + 1e-9)))
        return out

    def forget(self, keep):
        with self.lock:
            for u in list(self.shots):
                if u not in keep:
                    del self.shots[u]
