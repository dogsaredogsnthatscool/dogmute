import asyncio

from .core import Core


def main():
    asyncio.run(Core().run())


if __name__ == "__main__":
    main()
