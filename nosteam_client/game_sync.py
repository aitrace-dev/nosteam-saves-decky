"""Durable, direct-only game jobs. Sources are read-only; installs use new managed folders."""

import concurrent.futures
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import game_files
from .automatic import shortcut_key
from .config import configuration, update_config
from .peer import DirectClient, PeerServer, local_addresses
from .steam import discover
from .storage import atomic_json, checked_root, read_json
from .sync import Remote


class SourceUnavailable(Exception):
    pass


class GameSync:
    def __init__(self, home, stop):
        self.home, self.stop = home, stop
        self.database = home / "game-sync.sqlite3"
        self.wake = threading.Event()
        self.peer = None
        self.peer_thread = None
        self.phase = "off"
        self.catalog = None
        self.local = threading.local()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,owner TEXT NOT NULL,game TEXT NOT NULL,
                title TEXT NOT NULL,version TEXT,root TEXT NOT NULL,device INTEGER NOT NULL,
                inode INTEGER NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,
                total BIGINT NOT NULL DEFAULT 0,error TEXT,retry_at REAL NOT NULL DEFAULT 0,
                destination TEXT,repairs INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS chunks (
                job TEXT NOT NULL,file INTEGER NOT NULL,chunk INTEGER NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY(job,file,chunk)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS sources (
                owner TEXT NOT NULL,version TEXT NOT NULL,game TEXT NOT NULL,title TEXT NOT NULL,
                root TEXT NOT NULL,manifest TEXT NOT NULL,size BIGINT NOT NULL,
                PRIMARY KEY(owner,version)
            )""")
            db.execute("UPDATE jobs SET state='queued' WHERE state IN ('running','verifying')")
        self.database.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enabled(self):
        return configuration(self.home).get("game_sync_enabled", False)

    def owner(self):
        return configuration(self.home).get("account_id")

    def status(self):
        owner = self.owner()
        with self.connect() as db:
            rows = db.execute(
                "SELECT j.id,j.kind,j.game,j.title,j.version,j.state,j.created,j.total,j.error,"
                "COALESCE((SELECT sum(size) FROM chunks WHERE job=j.id),0) AS verified "
                "FROM jobs j WHERE owner=? ORDER BY created DESC LIMIT 100",
                (owner,),
            ).fetchall()
            offered = db.execute("SELECT count(*) FROM sources WHERE owner=?", (owner,)).fetchone()[
                0
            ]
        catalog = self.catalog if self.catalog and self.catalog.get("owner") == owner else None
        return {
            "enabled": self.enabled(),
            "state": self.phase if self.enabled() else "off",
            "jobs": [dict(row) for row in rows],
            "sharing": offered,
            "offers": catalog["offers"] if catalog else [],
            "self_device_id": catalog.get("self_device_id") if catalog else None,
        }

    def set_enabled(self, value):
        if type(value) is not bool:
            raise game_files.GameFileError("Choose whether game sync is enabled")
        update_config(lambda cfg: cfg.update(game_sync_enabled=value), self.home)
        self.wake.set()
        return {"enabled": value}

    def create(self, kind, game, title, root, version=None):
        if not self.enabled() or not self.owner():
            raise game_files.GameFileError("Sign in and enable game transfers first")
        root = checked_root(root, self.home)
        if not root.is_dir():
            raise game_files.GameFileError("Choose an existing game or library folder")
        info = root.stat()
        if kind == "download":
            destinations = [root / ".nosteam-staging", root / "NoSteam" / game / version]
            for definition in configuration(self.home)["games"].values():
                for value in definition.get("roots", []):
                    save = Path(value).expanduser().resolve()
                    if (
                        root == save
                        or save in root.parents
                        or any(
                            path == save or save in path.parents or path in save.parents
                            for path in destinations
                        )
                    ):
                        raise game_files.GameFileError(
                            "Choose a library outside existing save folders"
                        )
        identifier = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind == "download":
                existing = db.execute(
                    "SELECT id,state FROM jobs WHERE owner=? AND root=? AND version=? "
                    "AND kind='download' ORDER BY created DESC LIMIT 1",
                    (self.owner(), str(root), version),
                ).fetchone()
                if existing:
                    if existing["state"] in {"failed", "paused"}:
                        db.execute(
                            "UPDATE jobs SET state='queued',error=NULL,retry_at=0 WHERE id=?",
                            (existing["id"],),
                        )
                    return {"job": existing["id"]}
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE owner=? "
                    "AND state NOT IN ('completed','failed')",
                    (self.owner(),),
                ).fetchone()[0]
                >= 32
            ):
                raise game_files.GameFileError("Too many queued transfers")
            db.execute(
                "INSERT INTO jobs "
                "(id,kind,owner,game,title,version,root,device,inode,state,created) "
                "VALUES (?,?,?,?,?,?,?,?,?,'queued',?)",
                (
                    identifier,
                    kind,
                    self.owner(),
                    game,
                    title,
                    version,
                    str(root),
                    info.st_dev,
                    info.st_ino,
                    time.time(),
                ),
            )
        self.wake.set()
        return {"job": identifier}

    def share(self, key, root):
        shortcuts = {shortcut_key(item): item for item in discover()}
        if key not in shortcuts:
            raise game_files.GameFileError("Only a discovered non-Steam game can be shared")
        cfg = configuration(self.home)
        bound = next(
            (
                (identifier, game)
                for identifier, game in cfg["games"].items()
                if shortcut_key(game) == key
            ),
            None,
        )
        if bound is None:
            raise game_files.GameFileError(
                "Configure this game's save folders before sharing its installation"
            )
        return self.create("index", bound[0], shortcuts[key]["title"], root)

    def refresh(self):
        remote = Remote(configuration(self.home), home=self.home)
        try:
            result = remote.request("GET", "/v1/transfers/offers")
            self.catalog = {**result, "owner": self.owner()}
            return result
        finally:
            remote.close()

    def download(self, game, version, root):
        catalog = self.refresh()
        offer = next(
            (
                item
                for item in catalog["offers"]
                if item["game"] == game
                and item["version"] == version
                and item["device_id"] != catalog["self_device_id"]
            ),
            None,
        )
        if not offer:
            raise game_files.GameFileError("No other device offers this game version")
        result = self.create("download", game, offer["title"], root, version)
        self.update(result["job"], total=offer["size"])
        return result

    def control(self, identifier, paused):
        uuid.UUID(identifier)
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET state=?,error=NULL,retry_at=0 WHERE id=? AND owner=? "
                "AND state!='completed'",
                ("paused" if paused else "queued", identifier, self.owner()),
            )
            if not result.rowcount:
                raise game_files.GameFileError("Transfer is missing or already complete")
        self.wake.set()
        return {"state": "paused" if paused else "queued"}

    def unshare(self, version):
        with self.connect() as db:
            db.execute("DELETE FROM sources WHERE owner=? AND version=?", (self.owner(), version))
        remote = Remote(configuration(self.home), home=self.home)
        try:
            remote.request("DELETE", f"/v1/transfers/offers/{version}")
        finally:
            remote.close()
        return {"status": "stopped"}

    def source(self, version):
        if not self.enabled() or self.stop.is_set():
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE owner=? AND version=?", (self.owner(), version)
            ).fetchone()
        return {**dict(row), "manifest": json.loads(row["manifest"])} if row else None

    def network(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                cfg = configuration(self.home)
                if self.peer and (
                    not cfg.get("token")
                    or cfg.get("auth_required")
                    or self.peer.owner != cfg.get("account_id")
                    or (cfg.get("device_id") and self.peer.device_id != cfg["device_id"])
                ):
                    self.peer.shutdown()
                    self.peer.server_close()
                    self.peer = None
                if not cfg.get("token") or cfg.get("auth_required"):
                    self.phase = "signed_out"
                if cfg.get("token") and not cfg.get("auth_required"):
                    remote = Remote(cfg, home=self.home)
                    try:
                        if self.enabled():
                            contract = remote.request("GET", "/v1/transfers/config")
                            if self.peer:
                                self.peer.issuer = contract["issuer"]
                                self.peer.public_key = contract["public_key"]
                        if self.enabled() and self.peer is None:
                            me = remote.request("GET", "/v1/me")
                            if contract["protocol"] != 1 or not me.get("device_id"):
                                raise game_files.GameFileError(
                                    "Update the server to enable game transfers"
                                )
                            self.peer = PeerServer(
                                self.home,
                                self.source,
                                me["user_id"],
                                me["device_id"],
                                contract["issuer"],
                                contract["public_key"],
                            )
                            self.peer_thread = threading.Thread(
                                target=self.peer.serve_forever, daemon=True
                            )
                            self.peer_thread.start()
                        if self.peer:
                            addresses = local_addresses()
                            remote.request(
                                "POST",
                                "/v1/transfers/announce",
                                json={
                                    "enabled": self.enabled(),
                                    "fingerprint": self.peer.fingerprint,
                                    "addresses": addresses,
                                    "port": self.peer.server_port,
                                },
                            )
                        if not self.enabled() and self.peer:
                            self.peer.shutdown()
                            self.peer.server_close()
                            self.peer = None
                        catalog = remote.request("GET", "/v1/transfers/offers")
                        own = {
                            offer["version"]
                            for offer in catalog["offers"]
                            if offer["device_id"] == catalog["self_device_id"]
                        }
                        if self.enabled():
                            with self.connect() as db:
                                sources = db.execute(
                                    "SELECT * FROM sources WHERE owner=?", (self.owner(),)
                                ).fetchall()
                            missing = [item for item in sources if item["version"] not in own]
                            for source in missing[:10]:
                                remote.request(
                                    "POST",
                                    "/v1/transfers/offers",
                                    json={
                                        "game": source["game"],
                                        "title": source["title"],
                                        "version": source["version"],
                                        "size": source["size"],
                                        "files": len(json.loads(source["manifest"])["files"]),
                                    },
                                )
                        self.catalog = {**catalog, "owner": self.owner()}
                        self.phase = (
                            ("active" if local_addresses() else "no_network_address")
                            if self.enabled()
                            else "off"
                        )
                    finally:
                        remote.close()
            except OSError as error:
                self.phase = (
                    "port_in_use" if error.errno == errno.EADDRINUSE else "waiting_for_server"
                )
            except Exception:
                self.phase = "waiting_for_server"
            self.wake.wait(30)
        if self.peer:
            self.peer.shutdown()
            self.peer.server_close()

    def update(self, identifier, **values):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), identifier),
            )

    def cancelled(self, job):
        if self.stop.is_set() or not self.enabled() or self.owner() != job["owner"]:
            return True
        with self.connect() as db:
            return db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[
                0
            ] not in {"running", "verifying", "activating"}

    def check_root(self, job):
        root = checked_root(job["root"], self.home)
        info = root.stat()
        if info.st_dev != job["device"] or info.st_ino != job["inode"]:
            raise OSError("The selected library was disconnected or replaced")
        return root

    def index(self, job):
        root = self.check_root(job)
        excluded = [
            Path(path).expanduser().resolve()
            for game in configuration(self.home)["games"].values()
            for path in game.get("roots", [])
        ]
        manifest = game_files.index(root, job["game"], excluded, lambda: self.cancelled(job))
        version = game_files.version_id(manifest)
        size = game_files.validate(manifest)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?)",
                (
                    job["owner"],
                    version,
                    job["game"],
                    job["title"],
                    str(root),
                    json.dumps(manifest, separators=(",", ":")),
                    size,
                ),
            )
        self.update(job["id"], state="completed", version=version, total=size)
        self.wake.set()

    def _peers(self, job):
        offers = self.refresh()
        return [
            offer
            for offer in offers["offers"]
            if offer["game"] == job["game"]
            and offer["version"] == job["version"]
            and offer["available"]
            and offer["device_id"] != offers["self_device_id"]
        ]

    def _fetch(self, job, offers, file_index=None, chunk_index=None):
        remote = None
        clients = getattr(self.local, "clients", {})
        self.local.clients = clients
        try:
            start = ((file_index or 0) + (chunk_index or 0)) % len(offers) if offers else 0
            for offer in offers[start:] + offers[:start]:
                for address in offer["addresses"]:
                    if self.cancelled(job):
                        raise InterruptedError("Transfer paused")
                    try:
                        cache_key = (offer["device_id"], address, offer["port"], job["version"])
                        session = clients.get(cache_key)
                        if not session or session["expires"] <= time.time() + 5:
                            remote = remote or Remote(configuration(self.home), home=self.home)
                            grant = remote.request(
                                "POST",
                                "/v1/transfers/grant",
                                json={
                                    "source": offer["device_id"],
                                    "version": job["version"],
                                },
                            )
                            if session:
                                session["client"].close()
                            session = {
                                **grant,
                                "client": DirectClient(
                                    address, offer["port"], grant["fingerprint"]
                                ),
                            }
                            clients[cache_key] = session
                        client, token = session["client"], session["token"]
                        if file_index is None:
                            return client.manifest(job["version"], token)
                        return client.chunk(job["version"], file_index, chunk_index, token)
                    except InterruptedError:
                        raise
                    except Exception:
                        if cache_key in clients:
                            clients.pop(cache_key)["client"].close()
            raise SourceUnavailable("No direct source is reachable. Verified progress is retained.")
        finally:
            if remote:
                remote.close()

    def _stage(self, job):
        root = self.check_root(job)
        descriptor = game_files.open_directory(
            root, f".nosteam-staging/{job['id']}/files", create=True
        )
        os.close(descriptor)
        return root / ".nosteam-staging" / job["id"] / "files"

    def _chunk(self, job, offers, stage, manifest, file_index, chunk_index):
        if self.cancelled(job):
            raise InterruptedError("Transfer paused")
        self.check_root(job)
        entry = manifest["files"][file_index]
        expected_size = min(
            game_files.BLOCK_SIZE, entry["size"] - chunk_index * game_files.BLOCK_SIZE
        )
        start = (file_index + chunk_index) % len(offers) if offers else 0
        for offer in offers[start:] + offers[:start]:
            try:
                data = self._fetch(job, [offer], file_index, chunk_index)
            except SourceUnavailable:
                continue
            if (
                len(data) == expected_size
                and hashlib.sha256(data).hexdigest() == entry["hashes"][chunk_index]
            ):
                self.store_chunk(job, stage, entry, file_index, chunk_index, data)
                return
        raise SourceUnavailable("No verified source is reachable. Verified progress is retained.")

    def store_chunk(self, job, stage, entry, file_index, chunk_index, data):
        self.check_root(job)
        with game_files.open_file(stage, entry["path"], write=True) as stream:
            view = memoryview(data)
            offset = chunk_index * game_files.BLOCK_SIZE
            while view:
                written = os.pwrite(stream.fileno(), view, offset)
                if not written:
                    raise OSError("Could not write game chunk")
                view = view[written:]
                offset += written
            os.fsync(stream.fileno())
        # Only a verified, durable write can advance the resume bitmap.
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?)",
                (job["id"], file_index, chunk_index, len(data)),
            )

    def reuse_local(self, job, stage, manifest, completed):
        wanted = {
            value
            for file, entry in enumerate(manifest["files"])
            for chunk, value in enumerate(entry["hashes"])
            if (file, chunk) not in completed
        }
        if not wanted:
            return
        with self.connect() as db:
            previous = db.execute(
                "SELECT id,version,destination FROM jobs WHERE owner=? AND game=? "
                "AND root=? AND kind='download' AND state='completed' "
                "AND version!=? ORDER BY created DESC LIMIT 3",
                (job["owner"], job["game"], job["root"], job["version"]),
            ).fetchall()
        candidates = {}
        for old in previous:
            old_manifest = read_json(self.home / "game-manifests" / f"{old['id']}.json")
            if old_manifest is None:
                continue
            try:
                game_files.validate(old_manifest, old["version"])
            except ValueError:
                continue
            for entry in old_manifest["files"]:
                for chunk, value in enumerate(entry["hashes"]):
                    if value in wanted:
                        candidates.setdefault(
                            value, (Path(old["destination"]), entry["path"], chunk)
                        )
        for file, entry in enumerate(manifest["files"]):
            for chunk, value in enumerate(entry["hashes"]):
                if (file, chunk) in completed or value not in candidates:
                    continue
                if self.cancelled(job):
                    raise InterruptedError("Transfer paused")
                root, path, source_chunk = candidates[value]
                try:
                    with game_files.open_file(root, path) as stream:
                        stream.seek(source_chunk * game_files.BLOCK_SIZE)
                        data = stream.read(game_files.BLOCK_SIZE)
                except (OSError, ValueError):
                    continue
                if hashlib.sha256(data).hexdigest() == value:
                    self.store_chunk(job, stage, entry, file, chunk, data)
                    completed.add((file, chunk))

    def receive(self, job):
        if job["state"] == "activating":
            return self.activate(job)
        offers = []
        saved = self.home / "game-manifests" / f"{job['id']}.json"
        manifest = read_json(saved)
        if manifest is None:
            offers = self._peers(job)
            if not offers:
                raise SourceUnavailable("Waiting for a device offering this game version")
            manifest = self._fetch(job, offers)
            size = game_files.validate(manifest, job["version"])
            if manifest["game"] != job["game"] or size != offers[0]["size"]:
                raise game_files.GameFileError("Source metadata does not match this game version")
            atomic_json(saved, manifest)
        size = game_files.validate(manifest, job["version"])
        if manifest["game"] != job["game"]:
            raise game_files.GameFileError("The selected game version belongs to another game")
        stage = self._stage(job)
        self.update(job["id"], total=size)
        with self.connect() as db:
            completed = {
                (row[0], row[1])
                for row in db.execute("SELECT file,chunk FROM chunks WHERE job=?", (job["id"],))
            }
        remaining = sum(
            min(game_files.BLOCK_SIZE, entry["size"] - index * game_files.BLOCK_SIZE)
            for file_index, entry in enumerate(manifest["files"])
            for index in range(len(entry["hashes"]))
            if (file_index, index) not in completed
        )
        if shutil.disk_usage(stage).free < remaining + 64 * 1024 * 1024:
            raise game_files.GameFileError("Not enough disk space for this game")
        for directory in manifest["directories"]:
            descriptor = game_files.open_directory(stage, directory, create=True)
            os.close(descriptor)
        self.reuse_local(job, stage, manifest, completed)
        if not offers and any(
            (file, chunk) not in completed
            for file, entry in enumerate(manifest["files"])
            for chunk in range(len(entry["hashes"]))
        ):
            offers = self._peers(job)
            if not offers:
                raise SourceUnavailable("Waiting for a device offering this game version")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pending = set()
            for file_index, entry in enumerate(manifest["files"]):
                with game_files.open_file(stage, entry["path"], write=True) as stream:
                    os.ftruncate(stream.fileno(), entry["size"])
                    os.fsync(stream.fileno())
                for index in range(len(entry["hashes"])):
                    if (file_index, index) in completed:
                        continue
                    if self.cancelled(job):
                        raise InterruptedError("Transfer paused")
                    pending.add(
                        pool.submit(self._chunk, job, offers, stage, manifest, file_index, index)
                    )
                    if len(pending) >= 4:
                        done, pending = concurrent.futures.wait(
                            pending, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        for task in done:
                            task.result()
            for task in concurrent.futures.as_completed(pending):
                task.result()
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET state='verifying' WHERE id=? AND state='running'", (job["id"],)
            )
            if not changed.rowcount:
                raise InterruptedError("Transfer paused")
        bad = []
        for file_index, entry in enumerate(manifest["files"]):
            if self.cancelled(job):
                raise InterruptedError("Transfer paused")
            with game_files.open_file(stage, entry["path"], write=True) as stream:
                for index, expected in enumerate(entry["hashes"]):
                    if hashlib.sha256(stream.read(game_files.BLOCK_SIZE)).hexdigest() != expected:
                        bad.append((file_index, index))
                os.fchmod(stream.fileno(), 0o755 if entry["executable"] else 0o644)
                os.fsync(stream.fileno())
        if bad:
            with self.connect() as db:
                db.executemany(
                    "DELETE FROM chunks WHERE job=? AND file=? AND chunk=?",
                    [(job["id"], file, chunk) for file, chunk in bad],
                )
            if job["repairs"] >= 3:
                raise OSError("Storage verification failed repeatedly")
            self.update(job["id"], state="queued", repairs=job["repairs"] + 1)
            return
        if self.cancelled(job):
            raise InterruptedError("Transfer paused")
        self.update(job["id"], state="activating")
        self.activate({**dict(job), "state": "activating"})

    def activate(self, job):
        root = self.check_root(job)
        parent_name = f"NoSteam/{job['game']}"
        descriptor = game_files.open_directory(root, parent_name, create=True)
        os.close(descriptor)
        destination = root / parent_name / job["version"]
        marker = destination / ".nosteam-install.json"
        if not destination.exists():
            reservation = root / ".nosteam-staging" / job["id"] / "activation"
            descriptor = game_files.open_directory(
                root, f".nosteam-staging/{job['id']}/activation", create=True
            )
            os.close(descriptor)
            atomic_json(reservation / marker.name, {"job": job["id"], "version": job["version"]})
            os.rename(reservation, destination)
            for parent in (reservation.parent, destination.parent):
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        elif destination.is_symlink() or read_json(marker, {}).get("job") != job["id"]:
            raise game_files.GameFileError(
                "This destination already exists; it will not be overwritten"
            )
        target = destination / "files"
        if not target.exists():
            stage = self._stage(job)
            os.rename(stage, target)
            descriptor = os.open(stage.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.update(job["id"], state="completed", destination=str(target), error=None)

    def jobs(self):
        while not self.stop.is_set():
            if not self.enabled():
                self.stop.wait(1)
                continue
            with self.connect() as db:
                row = db.execute(
                    "SELECT * FROM jobs WHERE owner=? "
                    "AND state IN ('queued','waiting_for_source','activating') "
                    "AND retry_at<=? ORDER BY created LIMIT 1",
                    (self.owner(), time.time()),
                ).fetchone()
                if row and row["state"] != "activating":
                    db.execute(
                        "UPDATE jobs SET state='running',error=NULL WHERE id=?", (row["id"],)
                    )
            if row is None:
                self.stop.wait(1)
                continue
            try:
                if row["kind"] == "index":
                    self.index(row)
                else:
                    self.receive(row)
            except InterruptedError:
                with self.connect() as db:
                    db.execute(
                        "UPDATE jobs SET state=? WHERE id=? AND state IN ('running','verifying')",
                        ("queued" if self.stop.is_set() else "paused", row["id"]),
                    )
            except SourceUnavailable as error:
                with self.connect() as db:
                    db.execute(
                        "UPDATE jobs SET state='waiting_for_source',error=?,retry_at=? "
                        "WHERE id=? AND state IN ('running','verifying')",
                        (str(error), time.time() + 10, row["id"]),
                    )
            except game_files.GameFileError as error:
                self.update(row["id"], state="failed", error=str(error))
            except (OSError, ValueError):
                self.update(
                    row["id"],
                    state="failed",
                    error="Storage or content could not be verified. "
                    "Existing games and saves are preserved.",
                )
            except Exception:
                self.update(
                    row["id"],
                    state="failed",
                    error="Could not complete this transfer. Verified progress is retained.",
                )
