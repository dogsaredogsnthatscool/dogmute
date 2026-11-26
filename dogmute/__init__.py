"""DogMute: automatic local mute for Discord voice calls."""
import importlib.machinery
import importlib.util
import sys
import types


def _pyav_stand_in():
    """faster-whisper imports PyAV (a whole FFmpeg, about 20 MB of each download) at the top of faster_whisper.audio, only
    for decode_audio(), which reads audio files. DogMute always gives it numpy arrays, so the Windows, Mac and Flatpak
    builds leave PyAV out and this empty module stands in for it. Where PyAV is installed (a source checkout), it is used."""
    if sys.modules.get("av") is not None or importlib.util.find_spec("av") is not None:
        return
    av = types.ModuleType("av", "Stand-in for PyAV, which DogMute's builds leave out (see dogmute/__init__.py).")
    av.__spec__ = importlib.machinery.ModuleSpec("av", None)

    def missing(name):
        raise AttributeError(f"av.{name}: this DogMute build has no PyAV, so faster-whisper can't read audio files "
                             "(give it numpy arrays)")
    av.__getattr__ = missing
    sys.modules["av"] = av


_pyav_stand_in()
