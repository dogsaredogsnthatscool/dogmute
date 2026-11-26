#!/usr/bin/env python3
"""Writes flatpak/python-deps.json: every wheel requirements.txt needs on the Flatpak's Python (3.13, x86_64), with its
PyPI URL and SHA-256, so flatpak-builder downloads and checks them and the build itself runs offline.
Run it again after changing requirements.txt:  python3 flatpak/make-deps.py"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLATFORMS = ["manylinux_2_28_x86_64", "manylinux_2_27_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64",
             "linux_x86_64", "any"]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--dry-run", "--ignore-installed",
                        "--report", str(report), "--only-binary=:all:", "--python-version", "3.13",
                        "--implementation", "cp", "--abi", "cp313", *[a for p in PLATFORMS for a in ("--platform", p)],
                        "--target", str(Path(tmp) / "t"), "-r", str(HERE.parent / "requirements.txt")], check=True)
        items = json.loads(report.read_text())["install"]
    sources = []
    for i in sorted(items, key=lambda i: i["metadata"]["name"].lower()):
        d = i["download_info"]
        sources.append({"type": "file", "url": d["url"], "sha256": d["archive_info"]["hashes"]["sha256"]})
    module = {"name": "python-deps", "buildsystem": "simple",
              "build-commands": ["pip3 install --no-index --no-deps --no-build-isolation --ignore-installed --prefix=/app *.whl"],
              "sources": sources}
    (HERE / "python-deps.json").write_text(json.dumps(module, indent=2) + "\n")
    print(f"flatpak/python-deps.json: {len(sources)} wheels")


if __name__ == "__main__":
    main()
