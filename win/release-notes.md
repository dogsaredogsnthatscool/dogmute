**DogMute for Windows.** Download `DogMute-Setup.exe` below and run it. New to DogMute? Follow the 5 steps on the page that opens.

**New in 1.2.1**

- **Updating is one double-click.** If DogMute is already installed, opening `DogMute-Setup.exe` updates it without any questions: you see a progress bar, then DogMute opens again. The boxes you ticked last time stay ticked.

**New in 1.2.0**

- **OBS overlay.** Each mute plays the quickscope (with its green screen keyed out) over your OBS scene, plus the hitmarker, "+100" and a killfeed line. The installer adds it to OBS for you. Then click **Start Virtual Camera** in OBS and pick **OBS Virtual Camera** as your camera in Discord. This also fixes "couldn't open the webcam": OBS keeps the webcam, so DogMute doesn't need it.
- **Updates install themselves.** DogMute checks GitHub every 6 hours. When there's a new version, it downloads it, checks it, and installs it silently once you're not in a voice call. Your settings stay. You can turn this off under **Settings → DogMute**. If you're on 1.1.1 or older, install this version by hand once.
- **Lighter default speech model.** `small.en` runs on any computer. With an NVIDIA card, pick `large-v3-turbo` under **Settings → Words → Speech model** for the best accuracy.
- **Clearer setup.** Every step now says exactly where to paste what you copied.

**Good to know**

- Windows 10 version 2004 or newer, or Windows 11 (64-bit), and the Discord desktop app.
- No admin rights needed. DogMute hears only Discord (using Windows' own per-app audio capture, no driver) and never your microphone.
- Windows may say "Windows protected your PC" because the installer isn't code-signed. Click **More info → Run anyway**.
- Got an NVIDIA graphics card? Tick **Use my NVIDIA graphics card** in the installer (a 1.2 GB download) so speech runs on the card.

See the [README](https://github.com/dogsaredogsnthatscool/dogmute#on-windows) for setup and troubleshooting.
