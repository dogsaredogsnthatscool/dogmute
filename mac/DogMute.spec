# PyInstaller recipe for DogMute.app: the engine, the launcher, the page and the sound helper (bin/dogmute-listen).
# mac/build.sh runs it after building the helper. Apple Silicon also bundles mlx-whisper (speech on the Apple GPU).
import os
import platform

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.dirname(SPECPATH)
VERSION = os.environ.get("DOGMUTE_VERSION", "0.0.0")
ARM = platform.machine() == "arm64"

datas = [(os.path.join(ROOT, "dogmute", "static"), os.path.join("dogmute", "static")),
         (os.path.join(ROOT, "dogmute", "data"), os.path.join("dogmute", "data"))] + collect_data_files("faster_whisper")
binaries = [(os.path.join(ROOT, "bin", "dogmute-listen"), "bin")] \
    + collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")
hidden = ["dogmute.__main__", "dogmute.prefetch"] + collect_submodules("pyvirtualcam")
if ARM:
    datas += collect_data_files("mlx") + collect_data_files("mlx_whisper") + collect_data_files("tiktoken_ext")
    binaries += collect_dynamic_libs("mlx")
    hidden += collect_submodules("mlx") + collect_submodules("mlx_whisper", filter=lambda n: "torch" not in n) \
        + ["tiktoken_ext", "tiktoken_ext.openai_public"]

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "torch", "IPython", "pytest", "mlx_whisper.torch_whisper"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DogMute", console=False, upx=False,
          target_arch=platform.machine())
coll = COLLECT(exe, a.binaries, a.datas, name="DogMute", upx=False)
app = BUNDLE(
    coll, name="DogMute.app", icon=os.path.join(ROOT, "build", "DogMute.icns"), bundle_identifier="com.dogmosh.DogMute",
    version=VERSION,
    info_plist={
        "CFBundleName": "DogMute", "CFBundleDisplayName": "DogMute", "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION, "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True,   # no Dock icon: DogMute lives on its page (open DogMute again to show it)
        "NSMicrophoneUsageDescription": "Only to read a loopback device such as BlackHole, when you pick one. "
                                        "DogMute never listens to your microphone.",
    },
)
