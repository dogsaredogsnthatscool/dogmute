"""DogMute.exe's entry point (PyInstaller builds it from win/DogMute.spec)."""
import multiprocessing

if __name__ == "__main__":
    # a library's helper process (multiprocessing) runs this same program again: freeze_support() lets it be that
    # helper, instead of a second DogMute opening the window
    multiprocessing.freeze_support()
    from dogmute.winapp import main
    main()
