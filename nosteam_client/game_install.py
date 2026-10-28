"""Register a verified bundle in Steam with a journal and isolated Proton prefix."""

import base64
import hashlib
import io
import os
import platform
import shlex
import sys
from pathlib import Path

import vdf

from . import game_bundle as bundle
from .config import configuration, lock, update_config
from .game_files import GameFileError, open_file, validate
from .game_setup import steam_running, write_registry
from .steam import launch_options
from .storage import atomic_json, capture, digest, durable_unlink, read_json


def steam_target():
    candidates = []
    for root in [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]:
        for folder in (root / "userdata").glob("*/config"):
            if folder.parent.name.isdigit():
                candidates.append((root.resolve(), folder.resolve() / "shortcuts.vdf"))
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise GameFileError(
            "Select one Steam account on this device before installing a transferred game"
        )
    return candidates[0]


def options(launch, target):
    mappings = sorted(launch["paths"].items(), key=lambda item: len(item[0]), reverse=True)
    words = shlex.split(launch["options"])
    output = []
    for word in words:
        word = launch["commands"].get(word, word)
        if word in {";", "&&", "||", "|", ">", "<"}:
            raise GameFileError("This launch expression needs a portable launcher script")
        for old, new in mappings:
            word = word.replace(old, str(target / new))
        if word == "%command%":
            output.append(word)
        elif "=" in word and word.split("=", 1)[0].isidentifier():
            key, value = word.split("=", 1)
            if not key.isidentifier():
                output.append(shlex.quote(word))
            else:
                output.append(key + "=" + shlex.quote(value))
        else:
            output.append(shlex.quote(word))
    result = " ".join(output)
    if "%command%" not in words:
        result = "%command% " + result
    if launch["proton"]:
        result = "STEAM_COMPAT_DATA_PATH=" + shlex.quote(str(target / "prefix")) + " " + result
    return result.strip()


def prepare(target, manifest):
    launch = manifest["launch"]
    if any(char in str(target) for char in "'\"\\\n"):
        raise GameFileError(
            "Choose a library without quotes or backslashes for portable launch scripts"
        )
    bundle.materialize(target, manifest)
    replacements = sorted(launch["paths"].items(), key=lambda item: len(item[0]), reverse=True)
    # Only newly downloaded files are adapted. Existing destination saves are never opened.
    names = set(launch["helpers"])
    names.update(
        entry["path"]
        for entry in manifest["files"]
        if entry["path"].startswith("prefix/") and entry["path"].endswith(".reg")
    )
    for name in names:
        backup_root = target / ".nosteam-original-config"
        backup_root.mkdir(mode=0o700, exist_ok=True)
        backup = backup_root / hashlib.sha256(name.encode()).hexdigest()
        if not backup.exists():
            with open_file(target, name) as stream:
                header = stream.read(65536)
                if b"\0" in header:
                    continue  # Shared libraries need no text relocation or duplicate backup.
                original = header + stream.read(16 * 1024 * 1024 + 1 - len(header))
            write_registry(backup, original)
        data = backup.read_bytes()
        if len(data) > 16 * 1024 * 1024:
            raise GameFileError("A launch configuration is too large to relocate safely")
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            continue  # Executable/helper libraries remain byte-for-byte intact.
        for old, new in replacements:
            destination = str(target / new)
            text = text.replace(old, destination)
            text = text.replace(old.replace("/", "\\\\"), destination.replace("/", "\\\\"))
        if text.encode() != data:
            path = target / name
            write_registry(path, text.encode(), path.stat().st_mode & 0o777)


def recover(home):
    journal_path = home / "steam-install-journal.json"
    journal = read_json(journal_path)
    if not journal:
        return
    if steam_running():
        raise GameFileError("Close Steam to finish interrupted game registration")
    for record in journal["files"]:
        path = Path(record["path"])
        current = path.read_bytes() if path.exists() else None
        before = base64.b64decode(record["before"]) if record["before"] is not None else None
        after = base64.b64decode(record["after"])
        if current not in (before, after):
            raise GameFileError(
                "Steam settings changed during registration. Recovery backups were retained."
            )
    cfg = configuration(home)
    current = cfg["games"].get(journal["game"])
    if current not in (journal["old_binding"], journal["binding"]):
        raise GameFileError(
            "Save bindings changed during registration. Recovery backups were retained."
        )
    # Complete a fully written transaction; otherwise restore known original metadata.
    complete = all(
        Path(row["path"]).exists()
        and Path(row["path"]).read_bytes() == base64.b64decode(row["after"])
        for row in journal["files"]
    )
    complete = complete and current == journal["binding"]
    if complete:
        atomic_json(home / "installed-games" / (journal["game"] + ".json"), journal["installation"])
        atomic_json(home / "games" / journal["game"] / "binding.json", journal["binding"])
    else:
        for row in journal["files"]:
            path = Path(row["path"])
            if row["before"] is None:
                if path.exists():
                    durable_unlink(path)
            else:
                write_registry(path, base64.b64decode(row["before"]))

        def restore(cfg):
            if journal["old_binding"] is None:
                cfg["games"].pop(journal["game"], None)
            else:
                cfg["games"][journal["game"]] = journal["old_binding"]

        update_config(restore, home)
        binding = home / "games" / journal["game"] / "binding.json"
        if journal["old_binding"] is not None:
            atomic_json(binding, journal["old_binding"])
        elif binding.exists():
            durable_unlink(binding)
    durable_unlink(journal_path)


def register(home, game, version, target, manifest):
    validate(manifest, version)
    if manifest["schema"] != 2:
        return {"state": "files_only"}
    if steam_running():
        return {
            "state": "awaiting_steam",
            "message": "Close Steam to add the game and its Proton configuration",
        }
    with (
        lock(home / "steam-setup.lock", blocking=False),
        lock(home / "games" / game / "session.lock", blocking=False),
    ):
        recover(home)
        launch = manifest["launch"]
        if launch["platform"] != {"os": platform.system(), "arch": platform.machine()}:
            raise GameFileError(
                "This installation requires the source operating system and CPU architecture"
            )
        steam, registry = steam_target()
        cfg = configuration(home)
        previous = read_json(home / "installed-games" / (game + ".json"))
        if previous and previous.get("owner") != cfg.get("account_id"):
            raise GameFileError("This installation belongs to another paired account")
        old_binding = cfg["games"].get(game)
        if old_binding and not previous:
            state = read_json(home / "games" / game / "state.json", {})
            if (
                old_binding.get("source") != "steam-shortcut"
                or state.get("status") != "synced"
                or not state.get("digest")
                or digest(capture([Path(path) for path in old_binding["roots"]])) != state["digest"]
            ):
                raise GameFileError(
                    "Check existing saves first. This installation and its progress were preserved."
                )
            previous = {
                "appid": int(old_binding["shortcut_id"]),
                "registry": old_binding["registry"],
            }
        if (home / "games" / game / "pending.json").exists():
            raise GameFileError("Upload pending saves before replacing this managed installation")
        missing = [value for value in launch["requires"] if not Path(value).exists()]
        if missing:
            return {
                "state": "requires_dependency",
                "message": "Host setup required: "
                + ", ".join(Path(path).name for path in missing[:16]),
                "requires": missing,
            }
        prepare(target, manifest)
        original = registry.read_bytes() if registry.exists() else None
        shortcuts = vdf.binary_load(io.BytesIO(original)) if original else {"shortcuts": {}}
        rows = shortcuts["shortcuts"]
        appid = previous["appid"] if previous else launch["appid"]
        entry_key = next(
            (
                key
                for key, row in rows.items()
                if int(bundle.folded(row).get("appid", 0)) & 0xFFFFFFFF == appid
            ),
            None,
        )
        if entry_key is not None and not previous:
            raise GameFileError(
                "The source shortcut ID already belongs to another local installation"
            )
        if previous and (str(registry) != previous["registry"] or entry_key is None):
            raise GameFileError(
                "The managed Steam shortcut was moved or removed; existing settings were preserved"
            )
        original_options = options(launch, target)
        wrapped = original_options
        roots = [str(target / name) for name in launch["saves"]]
        definition = None
        if roots:
            # Preserve the existing save engine/session lock contract for transferred games.
            executable = os.environ.get(
                "NOSTEAM_EXECUTABLE", str(Path(sys.executable).parent / "nosteam")
            )
            wrapped = launch_options(game, original_options, executable)
            definition = {
                "source": "steam-shortcut",
                "shortcut_id": str(appid),
                "registry": str(registry),
                "title": launch["title"],
                "roots": roots,
                "original_options": original_options,
                "wrapped_options": wrapped,
            }
            for root in roots:
                if not Path(root).is_dir() or Path(root).is_symlink():
                    raise GameFileError("Transferred save folders are not ready in the new prefix")
        row = {
            "appid": appid if appid < 2**31 else appid - 2**32,
            "appname": launch["title"],
            "exe": '"' + str(target / launch["exe"]) + '"',
            "StartDir": '"' + str(target / launch["cwd"]) + '"',
            "LaunchOptions": wrapped,
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "tags": {},
        }
        row.update(launch["shortcut"])
        key = (
            entry_key if entry_key is not None else str(max([int(key) for key in rows] + [-1]) + 1)
        )
        rows[key] = row
        binary = io.BytesIO()
        vdf.binary_dump(shortcuts, binary)
        writes = [(registry, original, binary.getvalue())]
        for name in launch["artwork"]:
            suffix = Path(name).name[len(str(launch["appid"])) :]
            image = registry.parent / "grid" / (str(appid) + suffix)
            if image.exists():
                continue  # Keep destination artwork, including user customizations.
            with open_file(target, name) as stream:
                content = stream.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise GameFileError("Steam artwork is too large to register")
            writes.append((image, None, content))
        if launch["proton"]:
            tool_name = "nosteam-" + version[:24]
            tool_folder = steam / "compatibilitytools.d" / tool_name
            marker = tool_folder / ".nosteam-tool.json"
            if tool_folder.exists() and read_json(marker, {}).get("version") != version:
                raise GameFileError(
                    "A different compatibility tool already occupies this installation path"
                )
            tool_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = tool_folder / "payload"
            if not payload.exists() and not payload.is_symlink():
                payload.symlink_to(target / "runtime", target_is_directory=True)
            if not payload.is_symlink() or payload.resolve() != (target / "runtime").resolve():
                raise GameFileError("The managed Proton runtime path was changed")
            atomic_json(marker, {"version": version})
            descriptor = {
                "compatibilitytools": {
                    "compat_tools": {
                        tool_name: {
                            "install_path": "payload",
                            "display_name": launch["proton"] + " (NoSteam)",
                            "from_oslist": "windows",
                            "to_oslist": "linux",
                        }
                    }
                }
            }
            write_registry(tool_folder / "compatibilitytool.vdf", vdf.dumps(descriptor).encode())
            config_path = steam / "config/config.vdf"
            config_before = config_path.read_bytes() if config_path.exists() else None
            config = vdf.loads(config_before.decode()) if config_before else {}
            mapping = bundle.section(config, "CompatToolMapping")
            if mapping is None:
                node = config
                for name in [
                    "InstallConfigStore",
                    "Software",
                    "Valve",
                    "Steam",
                    "CompatToolMapping",
                ]:
                    actual = next((key for key in node if key.lower() == name.lower()), name)
                    node = node.setdefault(actual, {})
                mapping = node
            mapping[str(appid)] = {"name": tool_name, **launch["compatibility"]}
            writes.append((config_path, config_before, vdf.dumps(config).encode()))
        installation = {
            "owner": cfg.get("account_id"),
            "version": version,
            "appid": appid,
            "registry": str(registry),
            "path": str(target),
            "proton": launch["proton"],
        }
        journal = {
            "game": game,
            "binding": definition,
            "old_binding": old_binding,
            "installation": installation,
            "files": [
                {
                    "path": str(path),
                    "before": base64.b64encode(before).decode() if before is not None else None,
                    "after": base64.b64encode(after).decode(),
                }
                for path, before, after in writes
            ],
        }
        atomic_json(home / "steam-install-backups" / (version + ".json"), journal)
        atomic_json(home / "steam-install-journal.json", journal)
        try:
            for path, before, after in writes:
                if steam_running() or (path.read_bytes() if path.exists() else None) != before:
                    raise GameFileError(
                        "Steam started or changed its settings. Close it and retry registration."
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                write_registry(path, after)
            if definition:

                def change(cfg):
                    if cfg["games"].get(game) != old_binding:
                        raise GameFileError(
                            "The local save binding changed during game installation"
                        )
                    cfg["games"][game] = definition

                update_config(change, home)
                atomic_json(home / "games" / game / "binding.json", definition)
            atomic_json(home / "installed-games" / (game + ".json"), installation)
            durable_unlink(home / "steam-install-journal.json")
        except BaseException:
            recover(home)
            raise
    return {"state": "ready", "appid": appid, "proton": launch["proton"]}
