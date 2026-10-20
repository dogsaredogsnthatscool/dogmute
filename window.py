#!/usr/bin/env python3
"""The DogMute page in its own window. Starts the service if it isn't running; one window at a time."""
import json
import socket
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent
try:
    PORT = json.loads((Path.home() / ".config/dogmute/config.json").read_text()).get("port", 8853)
except (OSError, ValueError):
    PORT = 8853


def up():
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.3).close()
        return True
    except OSError:
        return False


def activate(app):
    if app.get_windows():
        return app.get_windows()[0].present()
    w = Gtk.ApplicationWindow(application=app, title="DogMute")
    w.set_default_size(560, 860)
    w.set_icon_from_file(str(HERE / "dogmute" / "static" / "icon.png"))
    wv = WebKit2.WebView()
    w.add(wv)
    w.show_all()
    if not up():
        subprocess.run(["systemctl", "--user", "start", "dogmute"])
        for _ in range(50):
            if up():
                break
            time.sleep(0.2)
    wv.load_uri(f"http://127.0.0.1:{PORT}/")


GLib.set_prgname("dogmute")
app = Gtk.Application(application_id="com.dogmosh.DogMute")
app.connect("activate", activate)
app.run()
