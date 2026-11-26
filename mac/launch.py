"""PyInstaller's entry point for DogMute.app (see dogmute/macapp.py)."""
import multiprocessing

if __name__ == "__main__":
    # a library's helper process (tqdm's lock starts multiprocessing's resource tracker) runs this same program again:
    # freeze_support() lets it be that helper, instead of a second DogMute
    multiprocessing.freeze_support()
    from dogmute.macapp import main
    main()
