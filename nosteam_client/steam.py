"""Read only Steam's shortcut registry. Installed Steam apps are never enumerated."""

import os
import shlex
from pathlib import Path

import vdf


def discover(home: Path | None = None) -> list[dict]:
    home = home or Path.home()
    bases = [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    found = []
    seen = set()
    for base in bases:
        for registry in sorted(base.glob("userdata/*/config/shortcuts.vdf")):
            registry = registry.resolve()
            if registry in seen:
                continue
            seen.add(registry)
            with registry.open("rb") as stream:
                entries = vdf.binary_load(stream).get("shortcuts", {})
            for entry in entries.values():
                item = {key.lower(): value for key, value in entry.items()}
                app_id = int(item.get("appid", 0)) & 0xFFFFFFFF
                # Shortcut IDs have the high bit set; an ordinary Steam app ID is rejected.
                if not app_id & 0x80000000:
                    continue
                # The companion is a launcher, not a game or save-sync candidate.
                executable = Path(item.get("exe", "").strip('"'))
                if executable == home / ".local/bin/nosteam-companion":
                    continue
                found.append(
                    {
                        "shortcut_id": str(app_id),
                        "title": item.get("appname", "Unknown"),
                        "exe": item.get("exe", ""),
                        "registry": str(registry),
                        "launch_options": item.get("launchoptions", ""),
                    }
                )
    return found


def verify_shortcut(game: dict, home: Path | None = None) -> dict:
    if game.get("source") != "steam-shortcut":
        raise ValueError("Only non-Steam shortcuts are supported")
    for shortcut in discover(home):
        if (shortcut["shortcut_id"], shortcut["registry"]) == (
            game["shortcut_id"],
            game["registry"],
        ):
            return shortcut
    raise ValueError("Non-Steam shortcut no longer exists; reconnect it in NoSteam Saves")


def launch_options(game_id: str, original: str, executable: str) -> str:
    if "nosteam" in original.lower():
        raise ValueError("Shortcut already has a NoSteam wrapper; disable it before reconfiguring")
    command = f"{shlex.quote(executable)} launch {shlex.quote(game_id)} --"
    if "%command%" in original:
        if original.count("%command%") != 1:
            raise ValueError("Launch options must contain at most one %command% placeholder")
        return original.replace("%command%", f"{command} %command%")
    return f"{command} %command% {original}".rstrip()


def environment_guard(game: dict):
    # Steam supplies this when launched via Play. A wrapper pasted onto a Steam game must fail.
    for name in ("SteamAppId", "SteamGameId"):
        raw = os.environ.get(name)
        if not raw or not raw.isdigit():
            continue
        value = int(raw)
        if value == 0:  # Some non-Steam launches use zero alongside the encoded SteamGameId.
            continue
        shortcut_id = value >> 32 if value > 0xFFFFFFFF else value
        if str(shortcut_id) != game["shortcut_id"]:
            raise ValueError("This launch is not the configured non-Steam shortcut")
