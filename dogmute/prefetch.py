"""Download the models ahead of the first call (the installer runs this): the speech model that fits this machine and
the voice-matching model. python -m dogmute.prefetch [--cpu]"""
import sys


def main():
    import ctranslate2
    from faster_whisper.utils import download_model
    from huggingface_hub import hf_hub_download

    from .voice import FILE, REPO

    gpu = "--cpu" not in sys.argv and ctranslate2.get_cuda_device_count() > 0
    name = "distil-large-v3" if gpu else "base.en"
    print(f"  speech model {name} ({'graphics card' if gpu else 'CPU'}) ...", flush=True)
    download_model(name)
    print("  voice-matching model ...", flush=True)
    hf_hub_download(REPO, FILE)
    if gpu:
        print("  small CPU fallback model ...", flush=True)
        download_model("base.en")


if __name__ == "__main__":
    main()
