import asyncio
import sys


def main():
    if sys.platform == "win32":
        from .windows import engine_setup
        engine_setup()   # before CTranslate2 loads: where the CUDA files are
    from .core import Core
    asyncio.run(Core().run())


if __name__ == "__main__":
    main()
