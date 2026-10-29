import hashlib
import json
import os
import threading
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nosteam_client import game_files, game_sync
from nosteam_client.config import update_config
from nosteam_client.peer import DirectClient, PeerServer


def test_manifest_is_content_addressed_excludes_saves_and_rejects_escape(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    (root / "game.exe").write_bytes(b"game binary")
    (root / "empty").mkdir()
    saves = root / "saves"
    saves.mkdir()
    (saves / "slot.sav").write_bytes(b"actual progress")
    manifest = game_files.index(root, "celeste", [saves])
    version = game_files.version_id(manifest)
    assert game_files.validate(manifest, version) == len(b"game binary")
    assert manifest["directories"] == ["empty"]
    assert "actual progress" not in json.dumps(manifest)
    assert "slot.sav" not in json.dumps(manifest)
    assert game_files.index(root, "celeste", [saves]) == manifest
    (root / "game.exe").write_bytes(b"updated binary")
    assert game_files.version_id(game_files.index(root, "celeste", [saves])) != version
    assert (saves / "slot.sav").read_bytes() == b"actual progress"
    bad = {**manifest, "files": [{**manifest["files"][0], "path": "../slot.sav"}]}
    with pytest.raises(ValueError):
        game_files.validate(bad)
    (root / "linked").symlink_to(saves, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        game_files.index(root, "celeste", [saves])


def test_staging_cannot_follow_a_symlink_to_actual_saves(tmp_path):
    stage, saves = tmp_path / "stage", tmp_path / "saves"
    stage.mkdir()
    saves.mkdir()
    slot = saves / "slot.sav"
    slot.write_bytes(b"preserve")
    (stage / "other").symlink_to(saves, target_is_directory=True)
    with pytest.raises(OSError):
        game_files.open_file(stage, "other/slot.sav", write=True)
    assert slot.read_bytes() == b"preserve"


@pytest.fixture
def peers(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "game.exe").write_bytes(os.urandom(game_files.BLOCK_SIZE * 8 + 17))
    (source / "game.exe").chmod(0o755)
    (source / "empty.dat").write_bytes(b"")
    (source / "empty-folder").mkdir()
    manifest = game_files.index(source, "celeste")
    version = game_files.version_id(manifest)
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    source_id, receiver_id = str(uuid.uuid4()), str(uuid.uuid4())
    server = PeerServer(
        tmp_path / "source-state",
        lambda requested: (
            {"root": str(source), "manifest": manifest} if requested == version else None
        ),
        "user_fixture",
        source_id,
        "https://example.test",
        public,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def grant(**overrides):
        now = int(time.time())
        return jwt.encode(
            {
                "iss": "https://example.test",
                "aud": source_id,
                "sub": receiver_id,
                "owner": "user_fixture",
                "version": version,
                "scope": "game-read",
                "iat": now,
                "nbf": now,
                "exp": now + 60,
                **overrides,
            },
            signer,
            algorithm="EdDSA",
        )

    requests = []

    class Remote:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, **kwargs):
            requests.append((method, path, kwargs))
            if path == "/v1/transfers/offers":
                return {
                    "self_device_id": receiver_id,
                    "offers": [
                        {
                            "game": "celeste",
                            "title": "Celeste",
                            "version": version,
                            "size": game_files.validate(manifest),
                            "files": len(manifest["files"]),
                            "device_id": source_id,
                            "available": True,
                            "addresses": ["127.0.0.1"],
                            "port": server.server_port,
                            "fingerprint": server.fingerprint,
                        }
                    ],
                }
            assert path == "/v1/transfers/grant"
            return {
                "token": grant(),
                "expires": time.time() + 60,
                "fingerprint": server.fingerprint,
            }

        def close(self):
            pass

    monkeypatch.setattr(game_sync, "Remote", Remote)
    try:
        yield source, manifest, version, server, grant, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def receiver(tmp_path, version):
    home, library = tmp_path / "receiver-state", tmp_path / "library"
    library.mkdir()
    update_config(lambda cfg: cfg.update(account_id="user_fixture", game_sync_enabled=True), home)
    manager = game_sync.GameSync(home, threading.Event())
    job = manager.create("download", "celeste", "Celeste", str(library), version)["job"]
    manager.update(job, state="running")
    with manager.connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
    return manager, row, library


def test_real_tls_transfer_resumes_after_restart_and_never_redownloads_durable_chunks(
    tmp_path, peers, monkeypatch
):
    source, manifest, version, server, grant, requests = peers
    manager, job, library = receiver(tmp_path, version)
    original = manager._chunk
    paused = threading.Event()
    pause_lock = threading.Lock()

    def pause_after_chunk(*args):
        original(*args)
        with pause_lock:
            if not paused.is_set():
                manager.control(job["id"], True)
                paused.set()

    monkeypatch.setattr(manager, "_chunk", pause_after_chunk)
    with pytest.raises(InterruptedError):
        manager.receive(job)
    with manager.connect() as db:
        done = {
            (row[0], row[1])
            for row in db.execute("SELECT file,chunk FROM chunks WHERE job=?", (job["id"],))
        }
    assert 0 < len(done) < 9
    restarted = game_sync.GameSync(manager.home, threading.Event())
    restarted.control(job["id"], False)
    restarted.update(job["id"], state="running")
    with restarted.connect() as db:
        resumed = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    requested = []
    original_chunk = DirectClient.chunk

    def record(self, version, file, chunk, token):
        requested.append((file, chunk))
        return original_chunk(self, version, file, chunk, token)

    monkeypatch.setattr(DirectClient, "chunk", record)
    restarted.receive(resumed)
    assert not done.intersection(requested)
    result = restarted.status()["jobs"][0]
    assert result["state"] == "completed"
    assert result["verified"] == result["total"] == game_files.validate(manifest)
    installed = library / "NoSteam/celeste" / version / "files"
    assert (
        hashlib.sha256((installed / "game.exe").read_bytes()).digest()
        == hashlib.sha256((source / "game.exe").read_bytes()).digest()
    )
    assert os.stat(installed / "game.exe").st_mode & 0o111
    assert (installed / "empty.dat").stat().st_size == 0
    assert (installed / "empty-folder").is_dir()
    assert all(path in {"/v1/transfers/offers", "/v1/transfers/grant"} for _, path, _ in requests)
    assert all("files" not in details.get("json", {}) for _, _, details in requests)


def test_direct_peer_rejects_wrong_certificate_owner_expired_grants_and_changed_content(peers):
    source, manifest, version, server, grant, _ = peers
    wrong = DirectClient("127.0.0.1", server.server_port, "0" * 64)
    with pytest.raises(OSError, match="identity"):
        wrong.manifest(version, grant())
    client = DirectClient("127.0.0.1", server.server_port, server.fingerprint)
    try:
        for token in (grant(owner="user_other"), grant(exp=int(time.time()) - 1)):
            with pytest.raises(OSError):
                client.manifest(version, token)
        assert client.manifest(version, grant()) == manifest
        index = next(i for i, entry in enumerate(manifest["files"]) if entry["path"] == "game.exe")
        (source / "game.exe").write_bytes(b"changed installation")
        with pytest.raises(OSError):
            client.chunk(version, index, 0, grant())
    finally:
        client.close()


def test_game_update_reuses_verified_local_blocks_and_preserves_previous_install_saves(
    tmp_path, peers, monkeypatch
):
    source, original_manifest, old_version, server, grant, _ = peers
    manager, job, library = receiver(tmp_path, old_version)
    manager.receive(job)
    old_install = library / "NoSteam/celeste" / old_version / "files"
    save = old_install / "saves/slot.sav"
    save.parent.mkdir()
    save.write_bytes(b"progress from playing the previous version")
    with (source / "game.exe").open("ab") as stream:
        stream.write(b"!")
    manifest = game_files.index(source, "celeste")
    version = game_files.version_id(manifest)
    server.provider = lambda requested: (
        {"root": str(source), "manifest": manifest} if requested == version else None
    )
    parent_remote = game_sync.Remote

    class UpdatedRemote(parent_remote):
        def request(self, method, path, **kwargs):
            result = super().request(method, path, **kwargs)
            if path.endswith("offers"):
                result["offers"][0].update(version=version, size=game_files.validate(manifest))
            else:
                result["token"] = grant(version=version)
            return result

    monkeypatch.setattr(game_sync, "Remote", UpdatedRemote)
    identifier = manager.create("download", "celeste", "Celeste", str(library), version)["job"]
    manager.update(identifier, state="running")
    with manager.connect() as db:
        updated = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
    requests = []
    original_chunk = DirectClient.chunk

    def record(self, version, file, chunk, token):
        requests.append((file, chunk))
        return original_chunk(self, version, file, chunk, token)

    monkeypatch.setattr(DirectClient, "chunk", record)
    manager.receive(updated)
    assert len(requests) == 1  # Only the changed final block crosses the network.
    installed = library / "NoSteam/celeste" / version / "files"
    assert (installed / "game.exe").read_bytes() == (source / "game.exe").read_bytes()
    assert save.read_bytes() == b"progress from playing the previous version"
    assert (old_install / "game.exe").stat().st_size + 1 == (installed / "game.exe").stat().st_size


def test_activation_restart_finishes_offline_without_touching_existing_game_saves(
    tmp_path, peers, monkeypatch
):
    _, _, version, _, _, _ = peers
    manager, job, library = receiver(tmp_path, version)
    save = library / "ExistingGame/saves/slot.sav"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"existing game progress")
    original_update = manager.update

    def interrupted(identifier, **values):
        if values.get("state") == "completed":
            raise OSError("simulated restart after atomic activation")
        return original_update(identifier, **values)

    monkeypatch.setattr(manager, "update", interrupted)
    with pytest.raises(OSError, match="simulated restart"):
        manager.receive(job)
    restarted = game_sync.GameSync(manager.home, threading.Event())
    monkeypatch.setattr(
        game_sync,
        "Remote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not need network")),
    )
    with restarted.connect() as db:
        resumed = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert resumed["state"] == "activating"
    restarted.receive(resumed)
    assert restarted.status()["jobs"][0]["state"] == "completed"
    assert save.read_bytes() == b"existing game progress"


def test_invalid_chunk_fails_over_without_marking_corrupt_bytes_durable(tmp_path, monkeypatch):
    data = b"valid content"
    manifest = {
        "files": [
            {"path": "game.bin", "size": len(data), "hashes": [hashlib.sha256(data).hexdigest()]}
        ]
    }
    manager, job, library = receiver(tmp_path, "a" * 64)
    stage = manager._stage(job)
    calls = []

    def fetch(job, offers, file, chunk):
        calls.append(offers[0]["device_id"])
        with manager.connect() as db:
            assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        return b"bad content!!" if offers[0]["device_id"] == "bad" else data

    monkeypatch.setattr(manager, "_fetch", fetch)
    manager._chunk(job, [{"device_id": "bad"}, {"device_id": "good"}], stage, manifest, 0, 0)
    assert calls == ["bad", "good"]
    assert (stage / "game.bin").read_bytes() == data
    with manager.connect() as db:
        assert db.execute("SELECT size FROM chunks").fetchone()[0] == len(data)


def test_website_reports_publish_only_progress_and_never_paths_or_error_details(tmp_path):
    import threading

    from nosteam_client.config import update_config
    from nosteam_client.game_sync import GameSync

    update_config(
        lambda cfg: cfg.update(account_id="owner", game_sync_enabled=True), tmp_path / "state"
    )
    manager = GameSync(tmp_path / "state", threading.Event())
    library = tmp_path / "library"
    library.mkdir()
    job = manager.create("download", "celeste", "Celeste", str(library), "a" * 64)["job"]
    manager.update(job, state="failed", total=100, error="private /home/alice/path details")
    calls = []

    class Remote:
        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs["json"]))
            return {"accepted": len(calls) > 1, "sequence": 50}

    manager.publish_status(Remote(), "owner")
    payload = calls[0][2]
    assert payload["jobs"][0]["state"] == "failed"
    assert not {"root", "destination", "error", "manifest"} & payload["jobs"][0].keys()
    assert "alice" not in str(payload)
    manager.publish_status(Remote(), "owner")
    assert calls[1][2]["sequence"] == 51
    manager.publish_status(Remote(), "other-account")
    assert len(calls) == 2
