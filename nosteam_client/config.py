import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from .storage import atomic_json, read_json


def state_home() -> Path:
    return Path(os.environ.get("NOSTEAM_HOME", Path.home() / ".local/share/nosteam-saves"))


@contextmanager
def lock(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(stream, flags)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def configuration(home: Path | None = None) -> dict:
    home = home or state_home()
    return read_json(home / "config.json", {"games": {}})


def update_config(change, home: Path | None = None):
    home = home or state_home()
    with lock(home / "config.lock"):
        config = configuration(home)
        change(config)
        atomic_json(home / "config.json", config)
