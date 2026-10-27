"""Model-free device probe. Server-supplied strings are never commands or authority."""

import hashlib
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path, PurePosixPath

from .catalog import prefixes
from .steam import verify_shortcut
from .storage import atomic_json, checked_root, read_json

BLOCKED = {
    ".ssh",
    ".aws",
    ".gnupg",
    "credentials",
    "cookies",
    "login data",
    "tokens",
    "steamapps",
    "compatdata",
    "dosdevices",
    "windows",
    "session storage",
}
SAVE_SUFFIXES = {
    ".sav",
    ".save",
    ".dat",
    ".bin",
    ".srm",
    ".sl2",
    ".rpgsave",
    ".ess",
    ".fos",
    ".lsv",
    ".sav2",
}


def scopes_for(shortcut: dict, home: Path) -> dict[str, tuple[str, Path]]:
    scopes = {}
    # The preferred explicit/default prefix belongs to this verified shortcut only.
    for prefix in prefixes(shortcut)[:1]:
        users = prefix / "drive_c/users"
        for index, user in enumerate(sorted(users.iterdir())):
            if user.name.casefold() in ("public", "all users", "default") or user.is_symlink():
                continue
            for kind, suffix in (
                ("roaming", "AppData/Roaming"),
                ("local", "AppData/Local"),
                ("locallow", "AppData/LocalLow"),
                ("documents", "Documents"),
                ("saved", "Saved Games"),
            ):
                path = user / suffix
                try:
                    checked = checked_root(str(path), home)
                    if checked.is_relative_to(prefix.resolve()) and len(scopes) < 11:
                        scopes[f"{kind}-{index}"] = (kind, checked)
                except ValueError:
                    continue
    exe = Path(shortcut.get("exe", "").strip('"'))
    if exe.is_absolute():
        try:
            install = checked_root(str(exe.parent), home)
            user_home = Path.home().resolve()
            # An executable in Downloads, ~, /usr/bin or ~/.local/bin grants no broad scan.
            within_home = install.is_relative_to(user_home)
            parts = install.relative_to(user_home).parts if within_home else ()
            if (len(parts) >= 2 and not parts[0].startswith(".")) or install.is_relative_to(
                "/run/media"
            ):
                scopes["install-0"] = ("install", install)
        except ValueError:
            pass
    return scopes


def fingerprint(candidate: dict) -> tuple:
    return candidate.get("root"), candidate.get("path"), candidate.get("operation")


def resolve(candidate: dict, scopes: dict) -> Path:
    if set(candidate) != {"root", "path", "operation"}:
        raise ValueError("Invalid probe fields")
    root_id, value, operation = fingerprint(candidate)
    if root_id not in scopes or operation not in ("probe", "list") or not isinstance(value, str):
        raise ValueError("Probe outside permitted scopes")
    path = PurePosixPath(value)
    if not value and operation == "list":
        return scopes[root_id][1]
    if (
        not value
        or len(value) > 240
        or path.is_absolute()
        or str(path) != value
        or len(path.parts) > 10
        or any(
            part.startswith(".")
            or part.casefold() in BLOCKED
            or not re.fullmatch(r"[\w .()'-]+", part)
            for part in path.parts
        )
    ):
        raise ValueError("Unsafe probe path")
    base = scopes[root_id][1]
    current = base
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Probe traverses a symlink")
    if not current.resolve().is_relative_to(base.resolve()):
        raise ValueError("Probe escapes scope")
    return current


def probe(candidate: dict, scopes: dict) -> dict:
    report = {
        "candidate": candidate,
        "status": "blocked",
        "entries": [],
        "save_files": 0,
        "total_bytes": 0,
        "truncated": False,
    }
    try:
        root = resolve(candidate, scopes)
        base = scopes[candidate["root"]][1]
        if base.is_symlink() or base.resolve() != base.absolute():
            return report
        if not root.is_dir():
            report["status"] = "missing"
            return report
        queue = deque([(root, 0)])
        visited = 0
        deadline = time.monotonic() + 2
        while queue:
            folder, depth = queue.popleft()
            if folder.is_symlink() or not folder.resolve().is_relative_to(base):
                return {**report, "status": "blocked", "save_files": 0}
            with os.scandir(folder) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 64 or time.monotonic() > deadline:
                        report.update(status="limit", truncated=True)
                        return report
                    if (
                        entry.is_symlink()
                        or entry.name.startswith(".")
                        or entry.name.casefold() in BLOCKED
                        or not re.fullmatch(r"[\w .()'-]{1,160}", entry.name)
                    ):
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    if candidate["operation"] == "list":
                        if is_dir:
                            if len(report["entries"]) == 24:
                                report["truncated"] = True
                                return {**report, "status": "inspected"}
                            report["entries"].append({"name": entry.name, "kind": "directory"})
                        continue
                    if is_dir:
                        if depth < 2:
                            queue.append((Path(entry.path), depth + 1))
                        else:
                            report["truncated"] = True
                    elif is_file:
                        metadata = entry.stat(follow_symlinks=False)
                        report["total_bytes"] += metadata.st_size
                        suffix = Path(entry.name).suffix.casefold()
                        save_name = re.search(r"save|slot|progress", entry.name, re.I)
                        if suffix in SAVE_SUFFIXES or (suffix == ".json" and save_name):
                            report["save_files"] += 1
                        if len(report["entries"]) < 24:
                            report["entries"].append(
                                {
                                    "name": entry.name,
                                    "kind": "file",
                                    "size": metadata.st_size,
                                    "modified": max(0, int(metadata.st_mtime)),
                                }
                            )
        report["status"] = "inspected" if visited else "empty"
        return report
    except (ValueError, OSError, TypeError):
        return report


def cache_path(home: Path, shortcut: dict) -> Path:
    identity = f"{shortcut['registry']}:{shortcut['shortcut_id']}"
    return home / "discovery" / f"{hashlib.sha256(identity.encode()).hexdigest()}.json"


def cached(home: Path, shortcut: dict) -> list[str]:
    result = read_json(cache_path(home, shortcut), {})
    scopes = scopes_for(shortcut, home)
    paths = []
    for candidate in result.get("matches", []):
        report = probe(candidate, scopes)
        if report["save_files"] and not report["truncated"]:
            paths.append(str(resolve(candidate, scopes)))
    # Never silently combine ambiguous saves from multiple accounts/installations.
    return paths if len(paths) == 1 else []


def negotiate(home: Path, shortcut: dict, remote) -> dict:
    verify_shortcut({**shortcut, "source": "steam-shortcut"})
    scopes = scopes_for(shortcut, home)
    if not scopes:
        return {
            "status": "needs_save",
            "suggested_roots": [],
            "rounds": 0,
            "message": "Waiting for the game's save location. Discovery will retry automatically.",
        }
    response = remote.request(
        "POST",
        "/v1/discovery",
        json={
            "source": "steam-shortcut",
            "title": shortcut["title"],
            "executable": Path(shortcut["exe"].strip('"')).name,
            "scopes": [{"id": name, "kind": kind} for name, (kind, _) in scopes.items()],
        },
    )
    session = str(uuid.UUID(response["session"]))
    verified = {}
    for round_number in range(1, 7):
        if response.get("session") != session:
            raise ValueError("Discovery session changed")
        if response.get("status") != "searching":
            break
        candidates = response.get("candidates", [])
        if response.get("round") != round_number or not 1 <= len(candidates) <= 4:
            raise ValueError("Invalid discovery round")
        observations = []
        for candidate in candidates:
            observation = probe(candidate, scopes)
            observations.append(observation)
            if observation["save_files"] and not observation["truncated"]:
                verified[fingerprint(candidate)] = str(resolve(candidate, scopes))
        response = remote.request(
            "POST",
            f"/v1/discovery/{session}/observations",
            json={
                "round": round_number,
                "observations": observations,
            },
        )
    if response.get("session") != session or response.get("status") not in ("review", "needs_save"):
        raise ValueError("Discovery exceeded negotiation budget")
    matches = response.get("matches", [])
    if len(matches) > 4 or any(fingerprint(candidate) not in verified for candidate in matches):
        raise ValueError("Server claimed an unverified save directory")
    paths = [verified[fingerprint(candidate)] for candidate in matches]
    atomic_json(cache_path(home, shortcut), {"matches": matches, "session": session})
    return {
        "status": "review" if paths else "needs_save",
        "suggested_roots": paths,
        "rounds": response.get("round", 0),
        "ai": response.get("ai"),
        "message": (
            "Save locations verified locally."
            if paths
            else "No save found yet. Discovery will retry automatically."
        ),
    }
