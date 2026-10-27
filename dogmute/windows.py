"""Windows glue: where settings live, pop-ups (Windows toasts), the graphics card's CUDA files, the app window."""
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

FROZEN = getattr(sys, "frozen", False)    # the installed DogMute.exe (PyInstaller), not a source checkout
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# toasts show under the Start menu shortcut with this AppUserModelID (the installer makes it); from a source checkout
# there is none, so they show under PowerShell's
APP_ID = "DogMute" if FROZEN else "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"


def conf_dir():
    """Settings and Discord credentials: %APPDATA%\\DogMute (the folder only you can read)."""
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "DogMute"


def state_dir():
    """The mute log, the review queue and DogMute's own log: %LOCALAPPDATA%\\DogMute."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "DogMute"


def port():
    try:
        return int(json.loads((conf_dir() / "config.json").read_text()).get("port", 8853))
    except (OSError, ValueError, TypeError, AttributeError):
        return 8853


def app_dir():
    return Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent


TOAST = """$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:DOGMUTE_TOAST)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:DOGMUTE_APPID).Show(
  [Windows.UI.Notifications.ToastNotification]::new($x))"""


def toast_xml(title, body, url):
    return (f"<toast activationType=\"protocol\" launch={quoteattr(url)}><visual><binding template=\"ToastGeneric\">"
            f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")


def toast(title, body):
    """A Windows pop-up; clicking it opens DogMute's page. Never blocks (PowerShell shows it)."""
    env = {**os.environ, "DOGMUTE_TOAST": toast_xml(title, body, f"http://127.0.0.1:{port()}/"), "DOGMUTE_APPID": APP_ID}
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", TOAST],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError:
        pass


def cuda_dirs():
    """Folders holding the CUDA 12 cuBLAS and cuDNN 9 DLLs the big speech model needs on an NVIDIA card: the installer
    puts them in <DogMute>\\cuda; from a source checkout they come from pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
    roots = [app_dir() / "cuda"] + [Path(p) for p in sys.path if p.endswith("site-packages")]
    return [b for r in roots for b in sorted((r / "nvidia").glob("*/bin")) if b.is_dir()]


def use_cuda():
    dirs = cuda_dirs()
    for d in dirs:
        os.add_dll_directory(str(d))
    if dirs:   # CTranslate2 loads cuBLAS by name when it first needs it, which searches PATH
        os.environ["PATH"] = os.pathsep.join([*map(str, dirs), os.environ.get("PATH", "")])
    return dirs


def engine_setup():
    """Before the engine imports anything heavy: the CUDA folders, and (installed) its output into dogmute.log."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")   # Windows without developer mode: copies, fine
    if FROZEN or sys.stdout is None:
        log = state_dir() / "dogmute.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 5_000_000:
            log.replace(log.with_suffix(".log.1"))
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8", errors="replace")
    dirs = use_cuda()
    if dirs:
        print("CUDA files:", ", ".join(map(str, dirs)), flush=True)


def _edge():
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                path = winreg.QueryValue(k, None)
                if path and Path(path).exists():
                    return path
        except OSError:
            pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        p = Path(base or "C:/Program Files") / "Microsoft/Edge/Application/msedge.exe"
        if p.exists():
            return str(p)
    return None


def open_window(port):
    """The page in a window of its own (Edge's app mode, which every Windows 10 and 11 has), else the browser."""
    url = f"http://127.0.0.1:{port}/"
    edge = _edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=580,900"])
    else:
        os.startfile(url)
