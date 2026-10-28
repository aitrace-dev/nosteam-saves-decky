"""Journaled non-Steam launch setup. Never writes or removes game save files."""

import base64
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import vdf

from .automatic import game_identity, shortcut_key
from .cli import bind
from .config import configuration, lock, update_config
from .game_files import GameFileError
from .steam import discover, launch_options
from .storage import atomic_json, durable_unlink, read_json
from .sync import Remote


def steam_running():
    if Path("/proc").is_dir():
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if process.stat().st_uid == os.getuid() and (
                    process / "comm"
                ).read_text().strip().lower() in {"steam", "steamwebhelper", "steam_osx"}:
                    return True
            except OSError:
                continue
        return False
    try:
        return (
            subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "-ix", "steam|steam_osx|steamwebhelper"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return True


def write_registry(path, data, mode=0o600):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".nosteam-setup-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def recover(home):
    journal_path = home / "setup-journal.json"
    journal = read_json(journal_path)
    if not journal:
        return
    registry = Path(journal["registry"])
    current = registry.read_bytes()
    cfg = configuration(home)
    bound = cfg["games"].get(journal["game"], {})
    if (
        hashlib.sha256(current).hexdigest() == journal["wrapped_hash"]
        and bound.get("wrapped_options") == journal["wrapped_options"]
    ):
        durable_unlink(journal_path)
        return
    if steam_running():
        raise GameFileError("Close Steam in Desktop Mode to finish save setup recovery")
    if (
        bound
        and bound != journal["before"]
        and (
            bound.get("wrapped_options") != journal["wrapped_options"]
            or bound.get("registry") != journal["registry"]
            or bound.get("roots") != journal.get("roots")
        )
    ):
        raise GameFileError("Save settings changed. Recovery backup was preserved for review.")
    original = base64.b64decode(journal["original"])
    if current != original:
        if hashlib.sha256(current).hexdigest() != journal["wrapped_hash"]:
            raise GameFileError(
                "Steam settings changed during setup. "
                "Your settings and recovery backup were preserved."
            )
        write_registry(registry, original)

    def restore(cfg):
        if journal["before"] is None:
            cfg["games"].pop(journal["game"], None)
        else:
            cfg["games"][journal["game"]] = journal["before"]

    update_config(restore, home)
    binding = home / "games" / journal["game"] / "binding.json"
    if journal["binding"] is not None:
        atomic_json(binding, journal["binding"])
    elif binding.exists():
        durable_unlink(binding)
    durable_unlink(journal_path)


def setup(home, key, save_root):
    if steam_running():
        raise GameFileError("Close Steam in Desktop Mode before changing launch options")
    with lock(home / "steam-setup.lock", blocking=False):
        recover(home)
        cfg = configuration(home)
        if not cfg.get("token") or not cfg.get("account_id"):
            raise GameFileError("Sign in before configuring saves")
        shortcut = next((item for item in discover() if shortcut_key(item) == key), None)
        if shortcut is None:
            raise GameFileError("This non-Steam shortcut is no longer installed")
        if any(shortcut_key(game) == key for game in cfg["games"].values()):
            raise GameFileError("Save sync is already configured for this shortcut")
        identifier = game_identity(shortcut)
        remote = Remote(cfg, home=home)
        try:
            existing = remote.request("GET", "/v1/sync/status")["games"]
        finally:
            remote.close()
        matches = [
            game["game"]
            for game in existing
            if game["title"].casefold() == shortcut["title"].casefold()
        ]
        if len(set(matches)) > 1:
            raise GameFileError(
                "More than one game has this name. Give the shortcut a unique title."
            )
        if matches:
            identifier = matches[0]
        if not identifier:
            raise GameFileError("Give this game a recognizable title in Steam first")
        registry = Path(shortcut["registry"])
        if registry.name != "shortcuts.vdf" or registry.parent.name != "config":
            raise GameFileError("This shortcut registry cannot be modified safely")
        original = registry.read_bytes()
        parsed = vdf.binary_load(io.BytesIO(original))
        entry = next(
            (
                item
                for item in parsed["shortcuts"].values()
                if int(next((value for name, value in item.items() if name.lower() == "appid"), 0))
                & 0xFFFFFFFF
                == int(shortcut["shortcut_id"])
            ),
            None,
        )
        if entry is None:
            raise GameFileError("The shortcut changed. Refresh the library and retry.")
        # Compute the wrapper before writing any configuration, then journal both sides.
        executable = os.environ.get(
            "NOSTEAM_EXECUTABLE", str(Path(sys.executable).parent / "nosteam")
        )
        wrapped = launch_options(identifier, shortcut["launch_options"], executable)
        field = next((name for name in entry if name.lower() == "launchoptions"), "LaunchOptions")
        entry[field] = wrapped
        output = io.BytesIO()
        vdf.binary_dump(parsed, output)
        updated = output.getvalue()
        journal = {
            "registry": str(registry),
            "roots": [str(Path(save_root).resolve())],
            "game": identifier,
            "original": base64.b64encode(original).decode(),
            "wrapped_options": wrapped,
            "wrapped_hash": hashlib.sha256(updated).hexdigest(),
            "before": cfg["games"].get(identifier),
            "binding": read_json(home / "games" / identifier / "binding.json"),
        }
        atomic_json(home / "steam-backups" / f"{uuid.uuid4()}.json", journal)
        atomic_json(home / "setup-journal.json", journal)
        try:
            bind(home, shortcut["shortcut_id"], str(registry), identifier, [save_root])
            if steam_running() or registry.read_bytes() != original:
                raise GameFileError("Steam started or its settings changed. Close Steam and retry.")
            write_registry(registry, updated)
            durable_unlink(home / "setup-journal.json")
        except BaseException:
            recover(home)
            raise
        return {"game": identifier, "status": "configured"}
