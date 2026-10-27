"""Durable sync operations. Callers hold exclusive(game) through the entire game session."""

import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from .config import lock
from .storage import (
    apply_files,
    atomic_json,
    capture,
    digest,
    durable_unlink,
    read_json,
    roots_for,
    validate,
)


class Conflict(Exception):
    """Both devices changed; preserve snapshots and ask which copy to use."""


class Offline(Exception):
    """Server unavailable; durable local progress must stay untouched."""


class Remote:
    def __init__(self, config: dict, transport=None):
        self.client = httpx.Client(
            base_url=config["server"].rstrip("/"),
            headers={"Authorization": f"Bearer {config['token']}"},
            timeout=15,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise Offline("Cloud unavailable; local saves are safe") from exc
        if response.status_code == 409:
            raise Conflict("Both devices have changed saves. Choose which copy to use.")
        if response.status_code >= 500:
            raise Offline("Cloud unavailable; local saves are safe")
        response.raise_for_status()
        return response.json()

    def head(self, game_id: str):
        return self.request("GET", f"/v1/games/{game_id}/head")

    def commit(self, game_id: str, snapshot: dict):
        return self.request("POST", f"/v1/games/{game_id}/revisions", json=snapshot)


class Sync:
    def __init__(self, home: Path, game_id: str, game: dict, remote: Remote, device: str):
        if (
            game.get("source") != "steam-shortcut"
            or not 0x80000000 <= int(game.get("shortcut_id", 0)) <= 0xFFFFFFFF
        ):
            raise ValueError("Only non-Steam shortcuts are supported")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", game_id):
            raise ValueError("Invalid game identity")
        self.home, self.game_id, self.game = home, game_id, game
        self.remote, self.device = remote, device
        self.folder = home / "games" / game_id
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.roots = roots_for(game, home)

    @contextmanager
    def exclusive(self, blocking: bool = False):
        with lock(self.folder / "session.lock", blocking):
            self.recover()
            yield self

    def state(self):
        return read_json(self.folder / "state.json", {})

    def status(self, status: str, **extra):
        atomic_json(self.folder / "state.json", {**self.state(), "status": status, **extra})

    def adopt(self, revision: str | None, files: dict):
        self.status("synced", base=revision, digest=digest(files), synced_at=time.time())

    def recover(self):
        journal = read_json(self.folder / "restore.json")
        if journal is not None:
            # A power failure during a multi-folder restore must not launch a mixed save.
            self.status("recovering")
            apply_files(self.roots, journal["before"])
            if "state" in journal:
                atomic_json(self.folder / "state.json", journal["state"])
            durable_unlink(self.folder / "restore.json")
            self.status("recovered")

    def restore(self, files: dict, revision: str):
        validate(files, len(self.roots))
        before = capture(self.roots)
        backup = {
            "before": before,
            "base": self.state().get("base"),
            "state": self.state(),
            "created": time.time(),
        }
        atomic_json(self.folder / "backups" / f"{uuid.uuid4()}.json", backup)
        atomic_json(self.folder / "restore.json", backup)
        self.status("downloading")
        apply_files(self.roots, files)
        self.adopt(revision, files)
        durable_unlink(self.folder / "restore.json")

    def enqueue(self, files: dict):
        snapshot = {
            "source": "steam-shortcut",
            "revision": str(uuid.uuid4()),
            "parent": self.state().get("base"),
            "device": self.device,
            "title": self.game["title"],
            "files": files,
        }
        # Preserve every replaced pending snapshot locally, including empty/deleted states.
        pending = read_json(self.folder / "pending.json")
        if pending is not None:
            atomic_json(self.folder / "backups" / f"{pending['revision']}.json", pending)
        atomic_json(self.folder / "pending.json", snapshot)
        self.status("pending")

    def flush(self):
        pending = read_json(self.folder / "pending.json")
        if pending is None:
            return
        self.status("uploading")
        try:
            result = self.remote.commit(self.game_id, pending)
        except Conflict:
            self.status("conflict")
            raise
        except Offline:
            self.status("pending")
            raise
        if result.get("digest") != digest(pending["files"]):
            self.status("error")
            raise ValueError("Server returned an invalid snapshot checksum")
        self.adopt(result["revision"], pending["files"])
        durable_unlink(self.folder / "pending.json")

    def prepare(self):
        """Finish sync before executing the game; never overwrite divergent progress."""
        self.flush()
        self.status("checking")
        try:
            remote = self.remote.head(self.game_id)
        except Offline:
            self.status("offline")
            raise
        local = capture(self.roots)
        state = self.state()
        local_digest = digest(local)
        remote_id = remote["revision"]
        if remote_id is not None:
            validate(remote["files"], len(self.roots))
            if digest(remote["files"]) != remote["digest"]:
                raise ValueError("Cloud snapshot checksum mismatch")
        if remote_id == state.get("base"):
            if local_digest != state.get("digest") and (local or state.get("base")):
                self.enqueue(local)
                self.flush()
            else:
                self.adopt(remote_id, local)
            return
        if local_digest == remote.get("digest"):
            self.adopt(remote_id, local)
            return
        changed = local_digest != state["digest"] if "digest" in state else bool(local)
        if changed:
            self.enqueue(local)
            self.status("conflict")
            raise Conflict("Both local and cloud saves exist. Choose which copy to use.")
        if remote_id is None:
            raise Conflict("The server history changed; choose a copy before continuing")
        self.restore(remote["files"], remote_id)

    def finish(self):
        self.status("saving")
        local = capture(self.roots)
        # Guard against a launcher returning while writes are still settling.
        time.sleep(0.2)
        if local != capture(self.roots):
            raise ValueError("Game saves are still changing; sync again after the game exits")
        if digest(local) == self.state().get("digest"):
            self.status("synced" if not (self.folder / "pending.json").exists() else "pending")
            return
        self.enqueue(local)
        self.flush()

    def resolve(self, choice: str):
        remote = self.remote.head(self.game_id)
        if remote["revision"] is None:
            if choice == "cloud":
                raise ValueError("There is no cloud save to choose")
        else:
            validate(remote["files"], len(self.roots))
            if digest(remote["files"]) != remote["digest"]:
                raise ValueError("Cloud snapshot checksum mismatch")
        if choice == "cloud":
            pending = read_json(self.folder / "pending.json")
            if pending is not None:
                atomic_json(self.folder / "backups" / f"{pending['revision']}.json", pending)
            self.restore(remote["files"], remote["revision"])
            durable_unlink(self.folder / "pending.json")
        elif choice == "local":
            self.status("pending", base=remote["revision"])
            self.enqueue(capture(self.roots))
            self.flush()
        else:
            raise ValueError("Choose local or cloud")

    def history(self):
        return self.remote.request("GET", f"/v1/games/{self.game_id}/revisions")

    def rollback(self, revision: str):
        # An explicit restore becomes a new revision; history remains immutable.
        snapshot = self.remote.request("GET", f"/v1/games/{self.game_id}/revisions/{revision}")
        validate(snapshot["files"], len(self.roots))
        if digest(snapshot["files"]) != snapshot["digest"]:
            raise ValueError("Cloud snapshot checksum mismatch")
        self.prepare()  # Do not discard offline or divergent work.
        parent = self.state().get("base")
        self.restore(snapshot["files"], revision)
        self.status("pending", base=parent)
        self.enqueue(snapshot["files"])
        self.flush()
