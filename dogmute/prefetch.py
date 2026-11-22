"""Download the models ahead of the first call (the installer runs this): the speech model that fits this machine and
the voice-matching model. python -m dogmute.prefetch [--cpu]"""
import sys

from .stt import MLX_REPOS, apple_gpu


def main():
    import ctranslate2
    from faster_whisper.utils import download_model
    from huggingface_hub import hf_hub_download

    from .voice import FILE, REPO

    mac = "--cpu" not in sys.argv and apple_gpu()
    gpu = "--cpu" not in sys.argv and ctranslate2.get_cuda_device_count() > 0
    name = "small.en"   # the default everywhere (Settings can pick large-v3-turbo for an NVIDIA or Apple GPU)
    if mac:
        from huggingface_hub import snapshot_download
        print(f"  speech model {name} (Apple GPU) ...", flush=True)
        snapshot_download(MLX_REPOS[name])
    else:
        print(f"  speech model {name} ({'graphics card' if gpu else 'CPU'}) ...", flush=True)
        download_model(name)
    print("  voice-matching model ...", flush=True)
    hf_hub_download(REPO, FILE)
    if mac:
        print("  CPU fallback model ...", flush=True)
        download_model(name)


if __name__ == "__main__":
    main()
