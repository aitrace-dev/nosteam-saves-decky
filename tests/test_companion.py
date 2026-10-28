import base64
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
from nosteam_client import companion
from nosteam_client.config import update_config
from nosteam_client.storage import atomic_json


def saved_device(home, saves):
    saves.mkdir()
    (saves / "slot.sav").write_bytes(b"real progress must survive")
    game = {
        "title": "Celeste",
        "source": "steam-shortcut",
        "shortcut_id": "2147484000",
        "registry": "/fixture/shortcuts.vdf",
        "roots": [str(saves)],
        "original_options": "gamemoderun %command%",
    }
    update_config(
        lambda cfg: cfg.update(
            token="private-token", server="https://example.test", games={"celeste": game}
        ),
        home,
    )
    atomic_json(
        home / "games/celeste/pending.json", {"revision": "pending", "files": {"secret": "data"}}
    )
    atomic_json(home / "games/celeste/backups/old.json", {"preserved": True})
    atomic_json(home / "games/celeste/state.json", {"status": "pending", "base": "revision-1"})


def test_status_projects_existing_progress_without_exposing_tokens_or_changing_saves(
    tmp_path, monkeypatch
):
    home, saves = tmp_path / "state", tmp_path / "saves"
    saved_device(home, saves)
    monkeypatch.setattr(companion, "discover", lambda: [])
    protected = [home / "config.json", *list((home / "games").rglob("*.json")), saves / "slot.sav"]
    before = {path: path.read_bytes() for path in protected}
    service = companion.Companion(home)
    result = service.snapshot()
    assert result["games"][0]["pending"] is True
    assert result["games"][0]["revision"] == "revision-1"
    assert result["workers"]["games"] == {"state": "off", "enabled": False}
    assert "private-token" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert {path: path.read_bytes() for path in protected} == before
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(service.database.stat().st_mode) == 0o600


def test_queue_survives_restart_and_interrupted_resolution_is_not_replayed(tmp_path):
    home, saves = tmp_path / "state", tmp_path / "saves"
    saved_device(home, saves)
    first = companion.Companion(home)
    queued = first.submit("history", {"game": "celeste"})
    interrupted = first.submit("resolve", {"game": "celeste", "choice": "local"})
    with first.connect() as db:
        db.execute("UPDATE tasks SET state='running' WHERE id=?", (interrupted["id"],))
    reopened = companion.Companion(home)
    assert reopened.task(queued["id"])["state"] == "queued"
    task = reopened.task(interrupted["id"])
    assert task["state"] == "failed" and "restarted" in task["error"]
    assert (saves / "slot.sav").read_bytes() == b"real progress must survive"
    assert (home / "games/celeste/pending.json").exists()


@pytest.mark.parametrize(
    "action,args",
    [
        ("launch", {"game": "celeste", "command": ["sh"]}),
        ("sync", {"game": "../celeste"}),
        ("sync", {"game": "other"}),
        ("resolve", {"game": "celeste", "choice": "delete"}),
        ("sign-in", {"server": "https://evil.test"}),
    ],
)
def test_commands_are_bounded_and_cannot_select_paths_or_run_programs(tmp_path, action, args):
    service = companion.Companion(tmp_path / "state")
    with pytest.raises(ValueError):
        service.submit(action, args)


def test_command_worker_redacts_error_details(tmp_path, monkeypatch):
    service = companion.Companion(tmp_path / "state")
    called = []

    def command(action, arguments):
        called.append((action, arguments))
        raise RuntimeError("Bearer do-not-expose-this-secret")

    monkeypatch.setattr(service, "command", command)
    task = service.submit("refresh-cloud", {})
    worker = threading.Thread(target=service.commands)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while service.task(task["id"])["state"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.01)
        result = service.task(task["id"])
        assert called == [("refresh-cloud", {})]
        assert result["state"] == "failed"
        assert "do-not-expose" not in json.dumps(result)
    finally:
        service.stop.set()
        service.wake.set()
        worker.join(3)


@pytest.fixture
def local_server(monkeypatch):
    # Unix sockets have a short pathname limit on macOS. Keep fixtures portable.
    monkeypatch.setattr(companion, "discover", lambda: [])
    with tempfile.TemporaryDirectory(prefix="ns-", dir="/tmp") as directory:
        with companion.Server(Path(directory)) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with httpx.Client(
                    transport=httpx.HTTPTransport(uds=str(companion.socket_path(Path(directory)))),
                    base_url="http://local",
                    timeout=3,
                ) as client:
                    yield client, Path(directory)
            finally:
                server.shutdown()
                thread.join(3)


def test_private_socket_protocol_rejects_browser_requests_and_oversized_commands(local_server):
    client, home = local_server
    assert stat.S_IMODE(companion.socket_path(home).stat().st_mode) == 0o600
    assert client.get("/v1/status").json()["protocol"] == 1
    payload = {"action": "sign-in", "arguments": {}}
    assert (
        client.post("/v1/tasks", json=payload, headers={"Origin": "https://evil.test"}).status_code
        == 403
    )
    assert client.post("/v1/tasks", content=b"x" * 5000).status_code == 400
    assert client.post("/v1/tasks", json={"action": "launch", "arguments": {}}).status_code == 400
    result = client.post("/v1/tasks", json=payload)
    assert result.status_code == 202
    identifier = result.json()["id"]
    assert client.get(f"/v1/tasks/{identifier}").json()["state"] == "queued"
    assert client.get("/v1/tasks/../../config.json").status_code == 404


def test_service_runs_without_decky_survives_disconnect_and_enforces_singleton():
    with tempfile.TemporaryDirectory(prefix="ns-", dir="/tmp") as directory:
        home = Path(directory)
        env = {**os.environ, "NOSTEAM_HOME": str(home)}
        process = subprocess.Popen(
            [sys.executable, "-m", "nosteam_client.companion"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not companion.socket_path(home).exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.02)
            transport = httpx.HTTPTransport(uds=str(companion.socket_path(home)))
            with httpx.Client(transport=transport, base_url="http://local") as client:
                started = client.get("/v1/status").json()["started"]
            assert process.poll() is None
            second = subprocess.run(
                [sys.executable, "-m", "nosteam_client.companion"],
                env=env,
                capture_output=True,
                timeout=5,
            )
            assert second.returncode != 0
            assert b"already running" in second.stderr
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(companion.socket_path(home))),
                base_url="http://local",
            ) as client:
                assert client.get("/v1/status").json()["started"] == started
        finally:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        assert not companion.socket_path(home).exists()
        assert (home / "companion.sqlite3").exists()


def test_refuses_to_replace_non_socket_or_follow_state_symlink(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    sentinel = companion.socket_path(home)
    sentinel.write_text("preserve")
    with pytest.raises(ValueError, match="non-socket"):
        companion.Server(home)
    assert sentinel.read_text() == "preserve"
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(ValueError, match="owned"):
        companion.Companion(alias)


def test_artwork_only_reads_discovered_local_steam_covers_and_rejects_symlinks(
    tmp_path, monkeypatch
):
    registry = tmp_path / "steam/config/shortcuts.vdf"
    grid = registry.parent / "grid"
    grid.mkdir(parents=True)
    shortcut = {"registry": str(registry), "shortcut_id": "2147484000", "title": "Celeste"}
    monkeypatch.setattr(companion, "discover", lambda: [shortcut])
    service = companion.Companion(tmp_path / "state")
    key = companion.shortcut_key(shortcut)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFpcAAAAASUVORK5CYII="
    )
    cover = grid / "2147484000p.png"
    cover.write_bytes(png)
    result = service.artwork(key, "portrait")
    assert result["image"] == "data:image/png;base64," + base64.b64encode(png).decode()
    assert service.artwork("f" * 64, "portrait") == {"image": None}
    assert service.artwork(key, "hero") == {"image": None}
    sentinel = tmp_path / "slot.sav"
    sentinel.write_bytes(png)
    cover.unlink()
    cover.symlink_to(sentinel)
    assert service.artwork(key, "portrait") == {"image": None}
    assert sentinel.read_bytes() == png
    with pytest.raises(ValueError):
        service.artwork(key, "../../slot.sav")


def test_artwork_rejects_oversized_files_and_non_images(tmp_path, monkeypatch):
    registry = tmp_path / "steam/config/shortcuts.vdf"
    grid = registry.parent / "grid"
    grid.mkdir(parents=True)
    shortcut = {"registry": str(registry), "shortcut_id": "2147484000", "title": "Celeste"}
    monkeypatch.setattr(companion, "discover", lambda: [shortcut])
    service = companion.Companion(tmp_path / "state")
    key = companion.shortcut_key(shortcut)
    cover = grid / "2147484000.jpg"
    cover.write_bytes(b"not an image")
    assert service.artwork(key, "landscape") == {"image": None}
    monkeypatch.setattr(companion, "MAX_ARTWORK", 32)
    cover.write_bytes(b"\xff\xd8\xff" + b"x" * 40)
    assert service.artwork(key, "landscape") == {"image": None}
    cover.unlink()
    grid.rmdir()
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    (elsewhere / "2147484000.jpg").write_bytes(b"\xff\xd8\xff")
    grid.symlink_to(elsewhere, target_is_directory=True)
    assert service.artwork(key, "landscape") == {"image": None}
