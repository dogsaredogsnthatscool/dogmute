**DogMute for Windows.** Download `DogMute-Setup.exe` below, run it, and follow the 5 steps on the page that opens.

- Windows 10 version 2004 or newer, or Windows 11 (64-bit), and the Discord desktop app.
- No admin rights needed. It hears only Discord (Windows' own per-app audio capture, no driver) and never your microphone.
- Windows may say "Windows protected your PC" because the installer isn't code-signed: click **More info → Run anyway**.
- OBS overlay: each mute plays the quickscope (green screen keyed out) over your OBS scene. The installer adds it to OBS for you. Then click **Start Virtual Camera** in OBS and pick **OBS Virtual Camera** in Discord.
- Updates install themselves: DogMute checks GitHub every 6 hours and installs a new version silently once you're out of the call. Your settings stay.
- The default speech model is now `small.en`, which runs on any computer. With an NVIDIA card you can pick `large-v3-turbo` in Settings.
- The setup steps now say exactly where to paste what you copy.
- Got an NVIDIA graphics card? Tick the box in the installer for the accurate speech model (a 1.2 GB download).
- Fixed in v1.1.1: DogMute could silently stop listening mid-call while the page still said it was. Update if it has gone quiet on you.

See the [README](https://github.com/dogsaredogsnthatscool/dogmute#on-windows) for setup and troubleshooting.
