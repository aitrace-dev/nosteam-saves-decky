import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

MAX_BYTES = 32 * 1024 * 1024


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def durable_unlink(path: Path):
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def checked_root(value: str, state_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Choose an absolute save folder without symlinks")
    home_alias = Path.home().absolute()
    home = home_alias.resolve()
    # Atomic desktops such as Bazzite expose /var/home through /home. Normalize that
    # OS-owned alias without accepting symlinks inside a game's save directory.
    if path == home_alias or home_alias in path.parents:
        path = home / path.relative_to(home_alias)
    if path.resolve() != path.absolute():
        raise ValueError("Choose an absolute save folder without symlinks")
    if path == home or path in home.parents or len(path.parts) < 3:
        raise ValueError("Choose the game's save folder, not a home or system folder")
    for protected in (state_dir.resolve(), home / ".ssh", home / ".gnupg"):
        if path == protected or path in protected.parents or protected in path.parents:
            raise ValueError("This folder cannot contain game saves")
    if path.exists() and not path.is_dir():
        raise ValueError("Choose a save folder; individual files are not supported yet")
    return path


def roots_for(game: dict, state_dir: Path) -> list[Path]:
    roots = [checked_root(value, state_dir) for value in game["roots"]]
    if not roots or len(roots) > 8:
        raise ValueError("Select between one and eight save folders")
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                raise ValueError("Save folders must not overlap")
    return roots


def capture(roots: list[Path]) -> dict[str, str]:
    files = {}
    size = 0
    for index, root in enumerate(roots):
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError("Save folders must not be symlinks")
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Symlinks inside save folders are not supported")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Save folders may contain only regular files")
            size += path.stat().st_size
            if size > MAX_BYTES or len(files) >= 10000:
                raise ValueError("Save exceeds the alpha limit (32 MiB / 10,000 files)")
            content = path.read_bytes()
            if len(content) != path.stat().st_size:
                raise ValueError("Save changed while being read; try after the game exits")
            files[f"{index}/{path.relative_to(root).as_posix()}"] = base64.b64encode(
                content
            ).decode()
    validate(files, len(roots))
    return files


def digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def validate(files: dict[str, str], root_count: int):
    if not isinstance(files, dict) or len(files) > 10000:
        raise ValueError("Invalid snapshot file list")
    size = 0
    for name, data in files.items():
        path = PurePosixPath(name)
        if (
            not name
            or len(name) > 1000
            or "\\" in name
            or "\x00" in name
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != name
            or len(path.parts) < 2
            or not path.parts[0].isdigit()
            or not 0 <= int(path.parts[0]) < root_count
        ):
            raise ValueError("Unsafe cloud snapshot path")
        size += len(base64.b64decode(data, validate=True))
        if size > MAX_BYTES:
            raise ValueError("Cloud snapshot exceeds 32 MiB")
    # Reject a file also used as a directory, before changing anything on disk.
    paths = set(files)
    for name in files:
        if any(str(parent) in paths for parent in PurePosixPath(name).parents):
            raise ValueError("Conflicting snapshot paths")


def apply_files(roots: list[Path], files: dict[str, str]):
    """Only called with a durable recovery journal and while holding the game's lock."""
    validate(files, len(roots))
    capture(roots)  # Reject symlinks/special files before modifying the tree.
    for index, root in enumerate(roots):
        root.mkdir(parents=True, exist_ok=True)
        prefix = f"{index}/"
        desired = {
            name[len(prefix) :]: data for name, data in files.items() if name.startswith(prefix)
        }
        # Delete obsolete files first so file-to-directory transitions also work.
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() and path.relative_to(root).as_posix() not in desired:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for name, encoded in desired.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".nosteam-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(base64.b64decode(encoded, validate=True))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
