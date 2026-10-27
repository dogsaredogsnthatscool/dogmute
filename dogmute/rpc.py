"""Discord local RPC over the IPC socket (discord-ipc-N): frames, auth, commands, events."""
import asyncio
import glob
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4
SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
REDIRECT = "http://localhost"
TOKEN_URL = "https://discord.com/api/oauth2/token"


class AuthError(Exception):
    """A setup problem, worded for the person setting DogMute up: what went wrong and what to do."""


def _oauth_problem(code, body):
    err, desc = body.get("error", ""), str(body.get("error_description", ""))
    if err == "invalid_client" or code == 401:
        return ("Discord doesn't know that Client ID + secret pair. On your app's OAuth2 page copy the Client ID again, "
                "press Reset Secret for a fresh secret, and paste both.")
    if "redirect" in desc.lower():
        return ("Discord wants a redirect. On your app's OAuth2 page, under Redirects, add http://localhost and press "
                "Save Changes. Then press Try again.")
    if err == "invalid_grant":
        return "Discord refused the login code (they expire quickly). Press Try again."
    return f"Discord said: {err or code} {desc}".strip()


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def socket_dirs():
    """Where the Discord app puts discord-ipc-N: $TMPDIR on macOS, $XDG_RUNTIME_DIR (or its Flatpak/Snap folder) on Linux."""
    if sys.platform == "darwin":
        dirs = [os.environ.get("TMPDIR", "")]
        try:
            dirs.append(os.confstr(65537))   # _CS_DARWIN_USER_TEMP_DIR: the same folder, for when TMPDIR isn't passed on
        except (OSError, ValueError):
            pass
        return [d.rstrip("/") for d in dirs if d] + ["/tmp"]
    run = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return [run, f"{run}/app/com.discordapp.Discord", f"{run}/snap.discord", f"{run}/app/com.discordapp.DiscordCanary", "/tmp"]


def find_socket():
    for d in socket_dirs():
        for p in sorted(glob.glob(f"{d}/discord-ipc-[0-9]")):
            return p
    return None


def _token_request(form):
    req = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "DogMute (local, 0.1)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        raise AuthError(_oauth_problem(e.code, body)) from None
    except OSError:
        raise AuthError("Can't reach discord.com. Check the internet connection, then try again.") from None
    tok["expires_at"] = time.time() + tok.get("expires_in", 0) - 60
    return tok


def exchange_code(creds, code):
    return _token_request({"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                           "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT})


def check_app(creds):
    """Do this Client ID + secret belong together? (an app-only token request; raises AuthError when not)"""
    _token_request({"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                    "grant_type": "client_credentials", "scope": "identify"})


def refresh(creds):
    return _token_request({"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                           "grant_type": "refresh_token", "refresh_token": creds["refresh_token"]})


class RPC:
    """One connection. `on_event(evt, data)` is called for every DISPATCH."""

    def __init__(self, client_id, on_event):
        self.client_id = client_id
        self.on_event = on_event
        self.reader = self.writer = None
        self.pending = {}
        self.user = None
        self.closed = asyncio.Event()
        self._task = None

    async def connect(self, path):
        self.reader, self.writer = await asyncio.open_unix_connection(path)
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        try:
            op, msg = await asyncio.wait_for(self._read(), 10)
        except asyncio.TimeoutError:   # its socket takes the connection and never answers (a hung Discord): try again later
            raise RPCError(0, "Discord isn't answering. Quit and reopen the Discord app.") from None
        if op == OP_CLOSE:
            raise RPCError(msg.get("code"), msg.get("message"))
        if msg.get("evt") != "READY":
            raise RPCError(0, f"unexpected handshake reply {msg}")
        self.user = msg["data"].get("user")
        self._task = asyncio.create_task(self._loop())

    def _send(self, op, obj):
        b = json.dumps(obj).encode()
        self.writer.write(struct.pack("<II", op, len(b)) + b)

    async def _read(self):
        op, n = struct.unpack("<II", await self.reader.readexactly(8))
        return op, json.loads(await self.reader.readexactly(n))

    async def _loop(self):
        try:
            while True:
                op, msg = await self._read()
                if op == OP_PING:
                    self._send(OP_PONG, msg)
                elif op == OP_CLOSE:
                    break
                elif op == OP_FRAME:
                    fut = self.pending.pop(msg.get("nonce"), None)
                    if fut and not fut.done():
                        if msg.get("evt") == "ERROR":
                            fut.set_exception(RPCError(msg["data"].get("code"), msg["data"].get("message")))
                        else:
                            fut.set_result(msg.get("data"))
                    elif msg.get("cmd") == "DISPATCH":
                        try:
                            self.on_event(msg.get("evt"), msg.get("data") or {})
                        except Exception as e:  # never let a handler kill the socket
                            print("event handler error:", evt_repr(msg), e, flush=True)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("discord closed"))
            self.pending.clear()
            self.closed.set()

    async def cmd(self, cmd, args=None, evt=None, timeout=10):
        if self.closed.is_set():
            raise ConnectionError("discord closed")
        nonce = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self.pending[nonce] = fut
        frame = {"cmd": cmd, "args": args or {}, "nonce": nonce}
        if evt:
            frame["evt"] = evt
        self._send(OP_FRAME, frame)
        return await asyncio.wait_for(fut, timeout)

    async def subscribe(self, evt, args=None):
        return await self.cmd("SUBSCRIBE", args, evt)

    async def unsubscribe(self, evt, args=None):
        try:
            return await self.cmd("UNSUBSCRIBE", args, evt)
        except (RPCError, asyncio.TimeoutError):
            return None

    def close(self):
        if self.writer:
            try:
                self._send(OP_CLOSE, {})
                self.writer.close()
            except Exception:
                pass


def evt_repr(msg):
    return f"{msg.get('cmd')}/{msg.get('evt')}"
