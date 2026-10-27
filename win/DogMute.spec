# PyInstaller recipe for DogMute.exe: one folder with the engine, the tray icon, the page and the sound helper.
# win\build.ps1 runs it (pyinstaller --noconfirm win\DogMute.spec) after building bin\dogmute-listen.exe.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    binaries=[(os.path.join(ROOT, "bin", "dogmute-listen.exe"), "bin")]
             + collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime"),
    datas=[(os.path.join(ROOT, "dogmute", "static"), os.path.join("dogmute", "static")),
           (os.path.join(ROOT, "dogmute", "data"), os.path.join("dogmute", "data"))]
          + collect_data_files("faster_whisper"),
    hiddenimports=["dogmute.__main__", "dogmute.prefetch", "pystray._win32"],
    excludes=["tkinter", "matplotlib", "torch", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DogMute", icon=os.path.join(ROOT, "build", "icon.ico"),
          console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="DogMute", upx=False)
