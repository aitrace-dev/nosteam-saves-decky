import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .catalog import suggest, update
from .config import configuration, state_home, update_config
from .discovery import cached, negotiate
from .steam import discover, environment_guard, launch_options, verify_shortcut
from .storage import atomic_json, capture, read_json, roots_for
from .sync import Conflict, Offline, Remote, Sync


def configure(server: str, token: str, home: Path):
    url = urlsplit(server)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("Use the server origin, for example https://saves.example.com")
    if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("HTTPS is required outside localhost")
    if len(token) < 32:
        raise ValueError("Use a server token of at least 32 characters")
    new = {"server": server.rstrip("/"), "token": token}
    remote = Remote(new, home=home)
    try:
        account = remote.request("GET", "/v1/me")
    finally:
        remote.close()

    def change(config):
        if config.get("account_id") not in (None, account["user_id"]) and config["games"]:
            raise ValueError("Pause existing games before signing in to a different account")
        if config.get("server") and config["server"] != new["server"] and config["games"]:
            raise ValueError("Disable existing games before switching servers")
        config.update(new)
        config["account_id"] = account["user_id"]
        if account.get("device_id"):
            config["device_id"] = account["device_id"]
        config["auth_required"] = False
        config.setdefault("device", f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}")

    update_config(change, home)


def bind(
    home: Path,
    shortcut_id: str,
    registry: str,
    game_id: str,
    roots: list[str],
    original_options: str | None = None,
):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", game_id):
        raise ValueError("Game identity must contain lowercase letters, numbers, - or _")
    candidate = {"source": "steam-shortcut", "shortcut_id": shortcut_id, "registry": registry}
    shortcut = verify_shortcut(candidate)
    game = {
        **candidate,
        "title": shortcut["title"],
        "roots": roots,
        "original_options": (
            shortcut["launch_options"] if original_options is None else original_options
        ),
    }
    resolved = roots_for(game, home)
    game["roots"] = [str(path) for path in resolved]
    files = capture(resolved)
    executable = os.environ.get("NOSTEAM_EXECUTABLE", str(Path(sys.executable).parent / "nosteam"))
    options = launch_options(game_id, game["original_options"], executable)
    game["wrapped_options"] = options
    can_reconnect = False

    def change(config):
        if "server" not in config:
            raise ValueError("Connect your server first")
        from .automatic import shortcut_key

        if shortcut_key(game) in config.get("paused_shortcuts", []):
            raise ValueError("This game is excluded from cloud sync")
        if game_id in config["games"] and not (
            can_reconnect and config["games"][game_id].get("removed")
        ):
            raise ValueError("This game is already enabled; disable it before reconfiguring")
        for existing_id, existing in config["games"].items():
            if existing_id == game_id and can_reconnect:
                continue
            if (existing["shortcut_id"], existing["registry"]) == (shortcut_id, registry):
                raise ValueError("This shortcut is already enabled")
            for first in resolved:
                for second in roots_for(existing, home):
                    if first == second or first in second.parents or second in first.parents:
                        raise ValueError("Another enabled game already uses this save folder")
        config["games"][game_id] = game

    # Keep configuration edits out of an active session.
    from .config import lock

    with lock(home / "games" / game_id / "session.lock", blocking=False):
        previous = configuration(home)["games"].get(game_id)
        can_reconnect = bool(previous and previous.get("removed")) and not any(
            (item["registry"], item["shortcut_id"])
            == (previous["registry"], previous["shortcut_id"])
            for item in discover()
        )
        old_binding = read_json(home / "games" / game_id / "binding.json")
        if (
            old_binding
            and not can_reconnect
            and any(
                old_binding.get(key) != game.get(key)
                for key in ("source", "shortcut_id", "registry", "roots")
            )
        ):
            raise ValueError("Existing local history belongs to a different installation")
        update_config(change, home)
        atomic_json(home / "games" / game_id / "binding.json", game)
    return {
        "game_id": game_id,
        "launch_options": options,
        "files": len(files),
        "original_options": game["original_options"],
    }


def notify(message: str):
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "NoSteam Saves", message],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(message, file=sys.stderr)


def conflict_choice() -> str | None:
    if not shutil.which("zenity"):
        return None
    result = subprocess.run(
        [
            "zenity",
            "--list",
            "--title=Save conflict",
            "--text=Both devices changed this save. "
            "Both copies are backed up. Choose which to play.",
            "--column=Use saves from",
            "This device",
            "Cloud",
            "--width=520",
            "--height=300",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"This device": "local", "Cloud": "cloud"}.get(result.stdout.strip())


def launch(sync: Sync, game: dict, command: list[str]) -> int:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("No launch command supplied")
    environment_guard(game)
    try:
        sync.prepare()
    except Offline:
        # Ordinary offline play needs no extra button. The next successful connection retries.
        notify("Playing offline. Saves will upload when the server is available.")
    except Conflict:
        choice = conflict_choice()
        if choice is None:
            notify("Save conflict: choose a copy in NoSteam Saves before playing.")
            return 2
        sync.resolve(choice)
    sync.status("playing")
    try:
        # No shell: Steam's expanded argv, working directory and environment are inherited.
        environment = dict(os.environ)
        if environment.pop("NOSTEAM_BUNDLED_PATH", None):
            original = environment.pop("NOSTEAM_ORIGINAL_PYTHONPATH", "")
            environment.pop("PYTHONPATH", None)
            if original:
                environment["PYTHONPATH"] = original
        result = subprocess.run(command, check=False, env=environment)
    except BaseException:
        sync.status("interrupted")
        raise
    try:
        sync.finish()
    except (Offline, Conflict) as exc:
        notify(str(exc))
    except Exception:
        sync.status("error")
        notify("Could not finish syncing. Local saves are intact; check NoSteam Saves.")
        raise
    return result.returncode


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Automatic cloud saves for non-Steam shortcuts")
    commands = root.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("connect")
    setup.add_argument("server")
    setup.add_argument("--stdin", action="store_true", help="Read token from stdin")
    commands.add_parser("discover")
    commands.add_parser("status")
    commands.add_parser("cloud-games")
    commands.add_parser("update-definitions")
    commands.add_parser("sign-in")
    commands.add_parser("poll-sign-in")
    commands.add_parser("auto-plan")
    for action in ("exclude", "include"):
        item = commands.add_parser(action)
        item.add_argument("shortcut_id")
        item.add_argument("--registry", required=True)
    find = commands.add_parser("find-saves")
    find.add_argument("shortcut_id")
    find.add_argument("--registry", required=True)
    enable = commands.add_parser("enable")
    enable.add_argument("shortcut_id")
    enable.add_argument("--registry", required=True)
    enable.add_argument("--game", required=True, help="Same identity on both devices")
    enable.add_argument("--save", required=True, action="append", help="Absolute save folder")
    enable.add_argument("--original-options", default=None)
    disable = commands.add_parser("disable")
    disable.add_argument("game")
    disable.add_argument("--retry", action="store_true", help=argparse.SUPPRESS)
    for action in ("sync", "history"):
        item = commands.add_parser(action)
        item.add_argument("game")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("game")
    resolve.add_argument("choice", choices=["local", "cloud"])
    rollback = commands.add_parser("rollback")
    rollback.add_argument("game")
    rollback.add_argument("revision")
    run = commands.add_parser("launch")
    run.add_argument("game")
    run.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("retry")
    commands.add_parser("daemon")
    return root


def retry(home: Path):
    from .library import reconcile

    config = reconcile(home, discover())
    if "server" not in config:
        return
    remote = Remote(config, home=home)
    try:
        for game_id, game in config["games"].items():
            if game.get("removed"):
                continue
            try:
                verify_shortcut(game)
                sync = Sync(home, game_id, game, remote, config["device"])
                with sync.exclusive():
                    sync.flush()  # Retry immutable outbox only; never restore during gameplay.
            except (BlockingIOError, Offline, Conflict):
                continue
            except Exception:
                # Do not log credentials, server responses or absolute save paths.
                print(f"Could not retry {game_id}; check configuration", file=sys.stderr)
    finally:
        remote.close()


def execute(args, home: Path):
    if args.action in ("exclude", "include"):
        from .library import set_excluded

        return set_excluded(home, args.shortcut_id, args.registry, args.action == "exclude")
    if args.action in ("sign-in", "poll-sign-in"):
        from .account import begin, poll

        return begin(home) if args.action == "sign-in" else poll(home)
    if args.action == "auto-plan":
        from .automatic import plan

        return plan(home)
    if args.action == "discover":
        catalog = read_json(home / "catalog.json", {})
        result = []
        for shortcut in discover():
            suggestion = suggest(shortcut, home, catalog)
            if not suggestion["suggested_roots"]:
                suggestion["suggested_roots"] = cached(home, shortcut)
            result.append({**shortcut, **suggestion})
        return result
    if args.action == "update-definitions":
        return update(home)
    if args.action == "connect":
        token = sys.stdin.readline().strip() if args.stdin else getpass.getpass("Server token: ")
        configure(args.server, token, home)
        return {"status": "connected"}
    config = configuration(home)
    if args.action == "status":
        from .account import public_state
        from .library import library

        rows = library(home)
        config = configuration(home)

        return {
            "connected": "server" in config
            and not config.get("auth_required")
            and config.get("credential_expires", float("inf")) > time.time(),
            "server": config.get("server", ""),
            "sign_in": public_state(home),
            "library": rows,
            "pending": read_json(home / "automation.json", {}).get("pending", []),
            "games": [
                {
                    "game_id": key,
                    **game,
                    **read_json(home / "games" / key / "state.json", {"status": "ready"}),
                }
                for key, game in config["games"].items()
            ],
        }
    if args.action == "enable":
        return bind(
            home, args.shortcut_id, args.registry, args.game, args.save, args.original_options
        )
    if args.action == "disable":
        from .config import lock

        with lock(home / "games" / args.game / "session.lock", blocking=False):
            game = config["games"][args.game]
            from .automatic import shortcut_key

            def pause(current):
                current["games"].pop(args.game)
                key = shortcut_key(game)
                if not args.retry and key not in current.setdefault("paused_shortcuts", []):
                    current["paused_shortcuts"].append(key)

            update_config(pause, home)
        return {"original_options": game["original_options"]}
    if args.action in ("retry", "daemon"):
        while True:
            retry(home)
            if args.action == "retry":
                return {"status": "retried"}
            time.sleep(30)
    remote = Remote(config, home=home)
    try:
        if args.action == "cloud-games":
            return remote.request("GET", "/v1/games")
        if args.action == "find-saves":
            shortcut = verify_shortcut(
                {
                    "source": "steam-shortcut",
                    "shortcut_id": args.shortcut_id,
                    "registry": args.registry,
                }
            )
            return negotiate(home, shortcut, remote)
        game = config["games"][args.game]
        verify_shortcut(game)
        sync = Sync(home, args.game, game, remote, config["device"])
        with sync.exclusive():
            if args.action == "launch":
                return launch(sync, game, args.command)
            if args.action == "resolve":
                sync.resolve(args.choice)
            elif args.action == "rollback":
                sync.rollback(args.revision)
            elif args.action == "history":
                return sync.history()
            else:
                sync.prepare()
        return {"status": "synced"}
    finally:
        remote.close()


def main():
    args = parser().parse_args()
    try:
        result = execute(args, state_home())
        if args.action == "launch":
            raise SystemExit(result)
        print(json.dumps(result))
    except BlockingIOError:
        print(json.dumps({"error": "This game is running or syncing. Try after it closes."}))
        raise SystemExit(2)
    except (ValueError, Conflict, Offline) as exc:
        message = str(exc)
        if args.action == "launch":
            notify(message)
        print(json.dumps({"error": message}))
        raise SystemExit(2)
    except (KeyError, OSError, httpx.HTTPError):
        # Avoid dumping HTTP bodies/headers or tracebacks containing credentials.
        message = "Operation failed. Check server connection, game selection and save folders."
        if args.action == "launch":
            notify(message)
        print(json.dumps({"error": message}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
