"""Private local control for the standalone app. Game/save bytes never cross this socket."""

import argparse
import base64
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer

from . import account
from .automatic import shortcut_key
from .cli import execute, parser, retry
from .config import configuration, lock, state_home
from .steam import discover
from .storage import read_json
from .sync import Conflict, Offline

PROTOCOL = 1
MAX_BODY = 4096
MAX_ARTWORK = 2 * 1024 * 1024
ACTIONS = {"sign-in", "poll-sign-in", "sync", "history", "resolve", "refresh-cloud"}


def socket_path(home: Path) -> Path:
    return home / "companion.sock"


def private_home(home: Path):
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("App state must be a directory owned by the current user")
    home.chmod(0o700)


class Companion:
    """Durable commands and an explicitly public local status projection."""

    def __init__(self, home: Path):
        private_home(home)
        self.home = home
        self.database = home / "companion.sqlite3"
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.started = time.time()
        self.retry_state = "starting"
        self.retry_at = None
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, arguments TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, result TEXT, error TEXT
            )""")
            # Read-only commands can be retried by the caller. Sync's own outbox and
            # restore journal remain authoritative; never blindly replay a resolution.
            db.execute(
                "UPDATE tasks SET state='failed',error=? WHERE state='running'",
                ("Background service restarted. Check save status before retrying.",),
            )
        self.database.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, action, arguments):
        if action not in ACTIONS or not isinstance(arguments, dict):
            raise ValueError("Unknown app command")
        expected = {"game"} if action in {"sync", "history"} else set()
        if action == "resolve":
            expected = {"game", "choice"}
        if set(arguments) != expected:
            raise ValueError("Invalid command arguments")
        if "game" in expected:
            game = arguments["game"]
            if not isinstance(game, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", game):
                raise ValueError("Invalid game identity")
            if game not in configuration(self.home)["games"]:
                raise ValueError("Game is not configured on this device")
        if action == "resolve" and arguments["choice"] not in ("local", "cloud"):
            raise ValueError("Choose local or cloud progress")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT count(*) FROM tasks WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if pending >= 32:
                raise ValueError("Too many queued commands. Wait for current work to finish.")
            identifier = str(uuid.uuid4())
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,'queued',?,NULL,NULL)",
                (identifier, action, json.dumps(arguments), time.time()),
            )
        self.wake.set()
        return {"id": identifier, "state": "queued"}

    def task(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("Command not found")
        return {
            "id": row["id"],
            "action": row["action"],
            "state": row["state"],
            "created": row["created"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
        }

    def artwork(self, key, kind):
        """Read only a discovered shortcut's Steam grid art, never a caller-supplied path."""
        if kind not in {"portrait", "landscape", "hero"}:
            raise ValueError("Unknown artwork shape")
        shortcut = next((item for item in discover() if shortcut_key(item) == key), None)
        if not shortcut:
            return {"image": None}
        grid = Path(shortcut["registry"]).parent / "grid"
        if grid.is_symlink():
            return {"image": None}
        app_id = shortcut["shortcut_id"]
        suffixes = ["p", ""] if kind == "portrait" else ["_hero", ""] if kind == "hero" else [""]
        for suffix in suffixes:
            for extension in ("jpg", "png", "webp"):
                candidate = grid / f"{app_id}{suffix}.{extension}"
                try:
                    descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError:
                    continue
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTWORK:
                        continue
                    data = stream.read(MAX_ARTWORK + 1)
                if len(data) > MAX_ARTWORK:
                    continue
                mime = None
                if data.startswith(b"\xff\xd8\xff"):
                    mime = "image/jpeg"
                elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime = "image/png"
                elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                    mime = "image/webp"
                if mime:
                    return {"image": f"data:{mime};base64,{base64.b64encode(data).decode()}"}
        return {"image": None}

    def snapshot(self):
        cfg = configuration(self.home)
        rows = []
        for game_id, game in cfg["games"].items():
            state = read_json(self.home / "games" / game_id / "state.json", {})
            pending = (self.home / "games" / game_id / "pending.json").exists()
            rows.append(
                {
                    "id": game_id,
                    "key": shortcut_key(game),
                    "title": game["title"],
                    "status": state.get("status", "ready"),
                    "revision": state.get("base"),
                    "synced_at": state.get("synced_at"),
                    "pending": pending,
                    "removed": bool(game.get("removed")),
                }
            )
        shortcuts = discover()
        with self.connect() as db:
            active = db.execute(
                "SELECT id,action,state,created FROM tasks "
                "WHERE state IN ('queued','running') ORDER BY created"
            ).fetchall()
        connected = bool(
            cfg.get("token")
            and cfg.get("server")
            and not cfg.get("auth_required")
            and cfg.get("credential_expires", float("inf")) > time.time()
        )
        return {
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "started": self.started,
            "device": {"name": socket.gethostname(), "connected": connected},
            "sign_in": {
                **account.public_state(self.home),
                "server": cfg.get("server", account.SERVER),
            },
            "games": sorted(rows, key=lambda item: item["title"].casefold()),
            "shortcuts": [
                {"key": shortcut_key(item), "title": item["title"]} for item in shortcuts
            ],
            "workers": {
                "saves": {"state": self.retry_state, "checked_at": self.retry_at},
                "games": {"state": "unavailable", "enabled": False},
            },
            "tasks": [dict(row) for row in active],
        }

    def command(self, action, arguments):
        if action == "sign-in":
            cfg = configuration(self.home)
            return account.begin(self.home, cfg.get("server", account.SERVER))
        if action == "poll-sign-in":
            return account.poll(self.home)
        if action == "refresh-cloud":
            from .sync import Remote

            cfg = configuration(self.home)
            if not cfg.get("token"):
                raise ValueError("Sign in to view your other devices")
            remote = Remote(cfg, home=self.home)
            try:
                return {
                    "games": remote.request("GET", "/v1/games"),
                    "devices": remote.request("GET", "/v1/devices"),
                    "checked_at": time.time(),
                }
            finally:
                remote.close()
        values = [action, arguments["game"]]
        if action == "resolve":
            values.append(arguments["choice"])
        return execute(parser().parse_args(values), self.home)

    def commands(self):
        while not self.stop.is_set():
            self.wake.clear()
            with self.connect() as db:
                row = db.execute(
                    "SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row:
                    db.execute("UPDATE tasks SET state='running' WHERE id=?", (row["id"],))
            if not row:
                self.wake.wait(1)
                continue
            try:
                result = self.command(row["action"], json.loads(row["arguments"]))
                state, error = "completed", None
            except BlockingIOError:
                state, result = "failed", None
                error = "This game is running or syncing. Try after it closes."
            except (Offline, Conflict) as exc:
                state, result, error = "failed", None, str(exc)
            except Exception:
                # Exception details may contain credentials, HTTP bodies, or save paths.
                state, result = "failed", None
                error = "Could not complete the command. Local saves are preserved."
            with self.connect() as db:
                db.execute(
                    "UPDATE tasks SET state=?,result=?,error=? WHERE id=?",
                    (state, json.dumps(result), error, row["id"]),
                )
                db.execute(
                    "DELETE FROM tasks WHERE state IN ('completed','failed') AND id NOT IN "
                    "(SELECT id FROM tasks ORDER BY created DESC LIMIT 100)"
                )

    def saves(self):
        while not self.stop.is_set():
            try:
                cfg = configuration(self.home)
                if (
                    not cfg.get("token")
                    or cfg.get("auth_required")
                    or cfg.get("credential_expires", float("inf")) <= time.time()
                ):
                    self.retry_state = "signed_out"
                else:
                    self.retry_state = "checking"
                    retry(self.home)
                    self.retry_state = (
                        "signed_out" if configuration(self.home).get("auth_required") else "idle"
                    )
            except Exception:
                self.retry_state = "error"
            self.retry_at = time.time()
            self.stop.wait(30)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def respond(self, code, value):
        payload = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            if self.path == "/v1/status":
                return self.respond(200, self.server.companion.snapshot())
            if re.fullmatch(r"/v1/artwork/[a-f0-9]{64}/(portrait|landscape|hero)", self.path):
                _, _, _, key, kind = self.path.split("/")
                return self.respond(200, self.server.companion.artwork(key, kind))
            if re.fullmatch(r"/v1/tasks/[0-9a-f-]{36}", self.path):
                return self.respond(200, self.server.companion.task(self.path.rsplit("/", 1)[1]))
            self.respond(404, {"error": "Unknown local endpoint"})
        except KeyError:
            self.respond(404, {"error": "Command not found"})
        except Exception:
            self.respond(503, {"error": "Could not read local app state"})

    def do_POST(self):
        if self.path != "/v1/tasks" or self.headers.get("Origin"):
            return self.respond(403, {"error": "Private app endpoint"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid command size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"action", "arguments"}:
                raise ValueError("Invalid command")
            result = self.server.companion.submit(payload["action"], payload["arguments"])
            self.respond(202, result)
        except (ValueError, TypeError):
            self.respond(400, {"error": "Invalid or unavailable app command"})
        except Exception:
            self.respond(503, {"error": "Could not queue the command"})


class Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True

    def __init__(self, home):
        self.companion = Companion(home)
        path = socket_path(home)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Refusing to replace a non-socket app path")
            path.unlink()
        super().__init__(str(path), Handler)
        path.chmod(0o600)


def serve(home: Path):
    private_home(home)
    # Singleton ownership is acquired before touching the socket or task journal.
    with lock(home / "companion.lock", blocking=False):
        with Server(home) as server:
            workers = [
                threading.Thread(target=server.companion.commands, daemon=True),
                threading.Thread(target=server.companion.saves, daemon=True),
            ]
            for worker in workers:
                worker.start()

            def shutdown(*_):
                server.companion.stop.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                server.companion.stop.set()
                for worker in workers:
                    worker.join(timeout=20)
                socket_path(home).unlink(missing_ok=True)


def main():
    argparse.ArgumentParser(description="NoSteam companion background service").parse_args()
    os.umask(0o077)
    try:
        serve(state_home())
    except BlockingIOError:
        raise SystemExit("NoSteam background service is already running") from None


if __name__ == "__main__":
    main()
