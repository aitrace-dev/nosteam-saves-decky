"""Immutable game manifests and descriptor-based writes confined to staging folders."""

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath

BLOCK_SIZE = 4 * 1024 * 1024
MAX_MANIFEST = 32 * 1024 * 1024
MAX_GAME = 8 * 1024**4
HASH = re.compile(r"[a-f0-9]{64}\Z")


class GameFileError(ValueError):
    """A safe, user-facing validation message without file contents or credentials."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def version_id(manifest):
    return hashlib.sha256(canonical(manifest)).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 1024:
        raise GameFileError("Invalid game file path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or len(path.parts) > 64
        or any(char in value for char in "\\:\x00")
        or any(ord(char) < 32 for char in value)
    ):
        raise GameFileError("Game file paths must stay inside the installation")
    return path


def validate(manifest, expected=None):
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "game",
        "block_size",
        "directories",
        "files",
    }:
        raise GameFileError("Invalid game manifest")
    if manifest["schema"] != 1 or manifest["block_size"] != BLOCK_SIZE:
        raise GameFileError("Unsupported game manifest")
    if not isinstance(manifest["game"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,99}", manifest["game"]
    ):
        raise GameFileError("Invalid game identity")
    if not isinstance(manifest["files"], list) or not isinstance(manifest["directories"], list):
        raise GameFileError("Invalid manifest entries")
    if len(manifest["files"]) + len(manifest["directories"]) > 250000:
        raise GameFileError("Too many game files")
    paths = {}
    folded = set()
    total = 0
    for kind, entries in (("directory", manifest["directories"]), ("file", manifest["files"])):
        for entry in entries:
            name = (
                entry
                if kind == "directory"
                else entry.get("path")
                if isinstance(entry, dict)
                else None
            )
            relative(name)
            normalized = unicodedata.normalize("NFC", name).casefold()
            if name in paths or normalized in folded:
                raise GameFileError("Duplicate or case-colliding game paths")
            folded.add(normalized)
            paths[name] = kind
            if kind == "file":
                if set(entry) != {"path", "size", "executable", "hashes"}:
                    raise GameFileError("Invalid game file entry")
                if (
                    type(entry["size"]) is not int
                    or not 0 <= entry["size"] <= MAX_GAME
                    or type(entry["executable"]) is not bool
                ):
                    raise GameFileError("Invalid game file size or permissions")
                hashes = entry["hashes"]
                if not isinstance(hashes, list) or len(hashes) != math.ceil(
                    entry["size"] / BLOCK_SIZE
                ):
                    raise GameFileError("Invalid game chunk map")
                if any(not isinstance(value, str) or not HASH.fullmatch(value) for value in hashes):
                    raise GameFileError("Invalid game chunk hash")
                total += entry["size"]
    if total > MAX_GAME or len(canonical(manifest)) > MAX_MANIFEST:
        raise GameFileError("Game manifest exceeds limits")
    for name in paths:
        if any(paths.get(str(parent)) == "file" for parent in relative(name).parents):
            raise GameFileError("A file cannot contain another game path")
    if expected and version_id(manifest) != expected:
        raise GameFileError("The source manifest does not match the selected game version")
    return total


def inventory(root, excluded):
    result, directories = {}, []

    def failed(error):
        raise error

    for parent, names, files in os.walk(root, followlinks=False, onerror=failed):
        parent = Path(parent)
        names[:] = sorted(
            name
            for name in names
            if name not in {".ssh", ".gnupg", ".secrets"}
            and not any(
                parent / name == item or item in (parent / name).parents for item in excluded
            )
        )
        for name in names + sorted(files):
            path = parent / name
            if path.name in {".env", ".existing-env"} or any(
                path == item or item in path.parents for item in excluded
            ):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise GameFileError("This game contains symlinks; select a portable game folder")
            name = path.relative_to(root).as_posix()
            relative(name)
            if stat.S_ISDIR(info.st_mode):
                directories.append(name)
            elif stat.S_ISREG(info.st_mode):
                result[name] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_mode,
                )
            else:
                raise GameFileError("Game folders can contain only regular files and directories")
            if len(result) + len(directories) > 250000:
                raise GameFileError("Too many game files")
    return result, sorted(directories)


def index(root, game, excluded=(), cancelled=lambda: False):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise GameFileError("Choose a game directory without symlinks")
    if (root / "user.reg").exists() and (root / "drive_c").is_dir():
        raise GameFileError("Select the game folder, not a complete Proton prefix")
    for save in excluded:
        if root == save or save in root.parents:
            raise GameFileError("A save folder cannot be shared as a game")
    before, directories = inventory(root, excluded)
    if sum(info[2] for info in before.values()) > MAX_GAME:
        raise GameFileError("Game exceeds transfer size limit")
    files = []
    for name, info in sorted(before.items()):
        if cancelled():
            raise InterruptedError("Game indexing paused")
        hashes = []
        with open_file(root, name) as stream:
            while data := stream.read(BLOCK_SIZE):
                if cancelled():
                    raise InterruptedError("Game indexing paused")
                hashes.append(hashlib.sha256(data).hexdigest())
        files.append(
            {"path": name, "size": info[2], "executable": bool(info[5] & 0o111), "hashes": hashes}
        )
    after, after_dirs = inventory(root, excluded)
    if before != after or directories != after_dirs:
        raise GameFileError("The game changed while indexing. Close it and retry.")
    manifest = {
        "schema": 1,
        "game": game,
        "block_size": BLOCK_SIZE,
        "directories": directories,
        "files": files,
    }
    validate(manifest)
    return manifest


def open_directory(root, name="", create=False):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative(name).parts if name else ():
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_file(root, name, write=False):
    path = relative(name)
    parent = str(path.parent) if str(path.parent) != "." else ""
    directory = open_directory(root, parent, create=write)
    try:
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        if write:
            try:
                descriptor = os.open(
                    path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
                os.fsync(directory)
            except FileExistsError:
                descriptor = os.open(path.name, flags, dir_fd=directory)
        else:
            descriptor = os.open(path.name, flags, dir_fd=directory)
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise GameFileError("Game entries must be regular files")
    return os.fdopen(descriptor, "r+b" if write else "rb")
