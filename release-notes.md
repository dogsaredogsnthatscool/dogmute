## Download

| Your computer | File | What to do |
| --- | --- | --- |
| **Windows** 10 (2004+) or 11, 64-bit | **`DogMute-Setup.exe`** (in Assets below) | Run it. |
| **Linux** with PipeWire | **`DogMute.flatpak`** (in Assets below) | Open it, or run `flatpak install --user DogMute.flatpak`. |
| **Mac** with Apple Silicon (M1 or newer), macOS 14+ | **`DogMute-AppleSilicon.dmg`** | Open it, drag DogMute to Applications. |
| **Mac** with Intel, macOS 14+ | **`DogMute-Intel.dmg`** | Same. |

**Mac, first open:** the app isn't signed by Apple yet. If macOS says it can't check DogMute, click **Done**, then **System Settings → Privacy & Security → Open Anyway**. You do this once.

Every computer needs the **Discord desktop app**. New to DogMute? Follow the 5 steps on the page that opens. Step-by-step help for each computer: [Windows](https://github.com/dogsaredogsnthatscool/dogmute#on-windows) · [Linux](https://github.com/dogsaredogsnthatscool/dogmute#on-linux) · [Mac](https://github.com/dogsaredogsnthatscool/dogmute#on-a-mac).

Ignore the two "Source code" files: they are for developers.

## New in 1.3.1

**Mac**

- **DogMute as an app.** `DogMute-AppleSilicon.dmg` and `DogMute-Intel.dmg`: drag DogMute to Applications and open it. No Homebrew, Python or Terminal needed. It starts when you log in, and macOS's Privacy settings list it as DogMute.

**Windows and Mac**

- **Fixed:** while downloading a speech model, the app could start a second copy of itself.

## New in 1.3.0

**Linux**

- **DogMute as one file.** `DogMute.flatpak` installs from GNOME Software or KDE Discover, starts when you log in, and opens the setup page. Speech runs on the CPU in it. To update, open the new `DogMute.flatpak`; your settings stay.
- **Updates install themselves** when you installed with `install.sh`, the same way as on Windows: once you're not in a voice call. To update by hand, run `./install.sh --update`.
- **Fixed:** DogMute hears Discord again when PipeWire is newer than its own tools.

**All computers**

- **Fixed:** a graphics card that can't run the speech model now falls back to the CPU at start, instead of failing on the first word.

## Earlier

**1.2.1 (Windows)**

- **Updating is one double-click.** If DogMute is already installed, opening `DogMute-Setup.exe` updates it without any questions: you see a progress bar, then DogMute opens again. The boxes you ticked last time stay ticked.

**1.2.0**

- **OBS overlay.** Each mute plays the quickscope (with its green screen keyed out) over your OBS scene, plus the hitmarker, "+100" and a killfeed line. The Windows installer adds it to OBS for you. Then click **Start Virtual Camera** in OBS and pick **OBS Virtual Camera** as your camera in Discord.
- **Updates install themselves on Windows.** DogMute checks GitHub every 6 hours and installs a new version silently once you're not in a voice call. Your settings stay. You can turn this off under **Settings → DogMute**. If you're on 1.1.1 or older, install this version by hand once.
- **Lighter default speech model.** `small.en` runs on any computer. With an NVIDIA card (Windows, or Linux with `install.sh`), pick `large-v3-turbo` under **Settings → Words → Speech model** for the best accuracy.

## Good to know

- **Windows:** no admin rights needed. DogMute hears only Discord (Windows' own per-app audio capture, no driver) and never your microphone. Windows may say "Windows protected your PC" because the installer isn't code-signed: click **More info → Run anyway**. Got an NVIDIA card? Tick **Use my NVIDIA graphics card** in the installer (a 1.2 GB download).
- **Linux:** the Flatpak needs Flathub set up (for its runtime). It hears only Discord's voice output, never a microphone.
- **Mac:** macOS asks once whether Python may record the screen and system audio. That is DogMute hearing Discord; it keeps no picture.
