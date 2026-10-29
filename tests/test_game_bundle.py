import json
import os
import threading
import time
import uuid
from pathlib import Path

import jwt
import pytest
import vdf
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nosteam_client import game_bundle, game_files, game_install, game_sync
from nosteam_client.config import configuration, update_config
from nosteam_client.peer import PeerServer


def steam_home(home, appid=2147484000, title="Portable game"):
    steam = home / ".local/share/Steam"
    registry = steam / "userdata/123/config/shortcuts.vdf"
    registry.parent.mkdir(parents=True)
    game = home / "Games/Portable game"
    game.mkdir(parents=True)
    (game / "game.exe").write_bytes(b"game binary" * 17)
    helper = home / ".local/bin/graphics-helper"
    helper.parent.mkdir(parents=True)
    library = home / ".local/share/graphics-helper/patch.so"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"unchanged preload library")
    helper.write_text(f'#!/bin/sh\nlib="{library}"\nexport LD_PRELOAD="$lib"\nexec "$@"\n')
    helper.chmod(0o755)
    options = f'WINEDLLOVERRIDES="winmm=n,b" "{helper}" %command% -windowed'
    entry = {
        "appid": appid - 2**32,
        "appname": title,
        "exe": f'"{game}/game.exe"',
        "StartDir": f'"{game}"',
        "LaunchOptions": options,
        "AllowOverlay": 0,
        "tags": {"0": "Favorites"},
    }
    with registry.open("wb") as stream:
        vdf.binary_dump({"shortcuts": {"0": entry}}, stream)
    grid = registry.parent / "grid"
    grid.mkdir()
    (grid / f"{appid}p.png").write_bytes(b"fixture cover")
    config = steam / "config/config.vdf"
    config.parent.mkdir(parents=True)
    config.write_text(
        vdf.dumps(
            {
                "InstallConfigStore": {
                    "Software": {
                        "Valve": {
                            "Steam": {
                                "CompatToolMapping": {
                                    str(appid): {
                                        "name": "GE-Proton-fixture",
                                        "config": "custom-flags",
                                        "priority": "250",
                                    },
                                    "123": {"name": "keep-other-tool"},
                                }
                            }
                        }
                    }
                }
            }
        )
    )
    runtime = steam / "compatibilitytools.d/GE-Proton-fixture"
    (runtime / "files/lib").mkdir(parents=True)
    (runtime / "proton").write_text("#!/bin/sh\nexit 0\n")
    (runtime / "proton").chmod(0o755)
    (runtime / "files/lib/wine.dll").write_bytes(b"actual wine runtime DLL")
    (runtime / "files/lib/alias.dll").symlink_to("wine.dll")
    (runtime / "compatibilitytool.vdf").write_text(
        vdf.dumps(
            {"compatibilitytools": {"compat_tools": {"GE-Proton-fixture": {"install_path": "."}}}}
        )
    )
    prefix = steam / "steamapps/compatdata" / str(appid)
    (prefix / "pfx/dosdevices").mkdir(parents=True)
    (prefix / "pfx/drive_c/windows").mkdir(parents=True)
    (prefix / "pfx/dosdevices/c:").symlink_to("../drive_c")
    (prefix / "pfx/dosdevices/z:").symlink_to("/")
    (prefix / "pfx/drive_c/windows/wine.dll").symlink_to(runtime / "files/lib/wine.dll")
    (prefix / "pfx/user.reg").write_text('WINE REGISTRY Version 2\n"install"="' + str(game) + '"\n')
    saves = prefix / "pfx/drive_c/users/steamuser/AppData/Roaming/Portable"
    saves.mkdir(parents=True)
    (saves / "slot.sav").write_bytes(b"source progress")
    shortcut = {
        "registry": str(registry),
        "shortcut_id": str(appid),
        "title": title,
        "exe": entry["exe"],
        "launch_options": options,
    }
    binding = {
        **shortcut,
        "source": "steam-shortcut",
        "original_options": options,
        "roots": [str(saves)],
    }
    return game, shortcut, binding


def update_launch(shortcut, value):
    path = Path(shortcut["registry"])
    with path.open("rb") as stream:
        data = vdf.binary_load(stream)
    data["shortcuts"]["0"]["LaunchOptions"] = value
    with path.open("wb") as stream:
        vdf.binary_dump(data, stream)


def materialize_files(root, manifest, mounts):
    for entry in manifest["files"]:
        key = next(
            key
            for key in sorted(mounts, key=len, reverse=True)
            if entry["path"].startswith(key + "/")
        )
        source = Path(mounts[key]) / entry["path"][len(key) + 1 :]
        with game_files.open_file(root, entry["path"], write=True) as stream:
            stream.write(source.read_bytes())
            os.fchmod(stream.fileno(), 0o755 if entry["executable"] else 0o644)
    for directory in manifest["directories"]:
        os.close(game_files.open_directory(root, directory, create=True))


def test_bundle_copies_prefix_runtime_helpers_and_registers_without_touching_existing_saves(
    tmp_path, monkeypatch
):
    source = tmp_path / "source-user"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    version = game_files.version_id(manifest)
    assert manifest["schema"] == 2
    assert any(entry["path"].startswith("runtime/") for entry in manifest["files"])
    destination = tmp_path / "destination-user"
    _, _, _ = steam_home(destination, appid=2147484999, title="Keep existing game")
    monkeypatch.setattr(Path, "home", lambda: destination)
    state = destination / "state"
    update_config(lambda cfg: cfg.update(account_id="owner"), state)
    installed = destination / "new library/files"
    installed.mkdir(parents=True)
    existing_save = next(
        destination.glob(
            ".local/share/Steam/steamapps/compatdata/*/pfx/drive_c/users/steamuser/AppData/Roaming/Portable/slot.sav"
        )
    )
    before = existing_save.read_bytes()
    materialize_files(installed, manifest, mounts)
    monkeypatch.setattr(game_install, "steam_running", lambda: True)
    assert (
        game_install.register(state, "portable", version, installed, manifest)["state"]
        == "awaiting_steam"
    )
    monkeypatch.setattr(game_install, "steam_running", lambda: False)
    assert (
        game_install.register(state, "portable", version, installed, manifest)["state"] == "ready"
    )
    assert existing_save.read_bytes() == before
    assert (installed / "game/game.exe").read_bytes() == (root / "game.exe").read_bytes()
    assert (
        installed / "prefix/pfx/drive_c/windows/wine.dll"
    ).resolve() == installed / "runtime/files/lib/wine.dll"
    assert os.readlink(installed / "prefix/pfx/dosdevices/z:") == "/"
    assert str(source) not in (installed / "helpers/0/graphics-helper").read_text()
    assert b"unchanged preload library" == (installed / "helpers/1/patch.so").read_bytes()
    cfg = configuration(state)
    assert cfg["games"]["portable"]["roots"][0].startswith(str(installed))
    registry = destination / ".local/share/Steam/userdata/123/config/shortcuts.vdf"
    with registry.open("rb") as stream:
        rows = vdf.binary_load(stream)["shortcuts"]
    assert len(rows) == 2
    assert (
        registry.parent / "grid" / (shortcut["shortcut_id"] + "p.png")
    ).read_bytes() == b"fixture cover"
    row = next(row for row in rows.values() if row.get("appname") == "Portable game")
    assert "STEAM_COMPAT_DATA_PATH=" in row["LaunchOptions"]
    assert "WINEDLLOVERRIDES=winmm=n,b" in row["LaunchOptions"]
    assert "-windowed" in row["LaunchOptions"]
    assert row["allowoverlay"] == 0
    config = vdf.loads((destination / ".local/share/Steam/config/config.vdf").read_text())
    mapping = game_bundle.section(config, "CompatToolMapping")
    assert mapping["123"]["name"] == "keep-other-tool"
    assert mapping[shortcut["shortcut_id"]]["config"] == "custom-flags"
    assert mapping[shortcut["shortcut_id"]]["name"].startswith("nosteam-")
    # Retrying cannot nest path replacements or create duplicate Steam entries.
    assert (
        game_install.register(state, "portable", version, installed, manifest)["state"] == "ready"
    )
    with registry.open("rb") as stream:
        assert len(vdf.binary_load(stream)["shortcuts"]) == 2
    assert existing_save.read_bytes() == before


def test_bundle_rejects_arbitrary_external_links_and_traversal(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    manifest, _ = game_bundle.build(root, "portable", shortcut, binding)
    for link in [
        {"path": "prefix/pfx/dosdevices/z:", "target": "/etc/shadow", "external": True},
        {"path": "game/link", "target": "../actual-saves", "external": False},
    ]:
        invalid = json.loads(json.dumps(manifest))
        invalid["links"] = [link]
        with pytest.raises(ValueError):
            game_files.validate(invalid)


def test_real_tls_bundle_resumes_into_pending_steam_registration(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    version = game_files.version_id(manifest)
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    source_id = str(uuid.uuid4())
    server = PeerServer(
        tmp_path / "peer",
        lambda key: {"manifest": manifest, "mounts": mounts} if key == version else None,
        "owner",
        source_id,
        "https://test.invalid",
        public,
        host="127.0.0.1",
        port=0,
    )
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    requests = []

    class Remote:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, **kwargs):
            requests.append(path)
            if path.endswith("/offers"):
                return {
                    "self_device_id": "receiver",
                    "offers": [
                        {
                            "game": "portable",
                            "title": "Portable game",
                            "version": version,
                            "size": game_files.validate(manifest),
                            "device_id": source_id,
                            "available": True,
                            "addresses": ["127.0.0.1"],
                            "port": server.server_port,
                        }
                    ],
                }
            now = int(time.time())
            return {
                "token": jwt.encode(
                    {
                        "iss": "https://test.invalid",
                        "aud": source_id,
                        "sub": "receiver",
                        "owner": "owner",
                        "version": version,
                        "scope": "game-read",
                        "iat": now,
                        "nbf": now,
                        "exp": now + 60,
                    },
                    signer,
                    algorithm="EdDSA",
                ),
                "expires": now + 60,
                "fingerprint": server.fingerprint,
            }

        def close(self):
            pass

    monkeypatch.setattr(game_sync, "Remote", Remote)
    destination = tmp_path / "destination"
    steam_home(destination, appid=2147484999)
    monkeypatch.setattr(Path, "home", lambda: destination)
    state, library = destination / "state", destination / "library"
    library.mkdir()
    update_config(lambda cfg: cfg.update(account_id="owner", game_sync_enabled=True), state)
    manager = game_sync.GameSync(state, threading.Event())
    identifier = manager.create("download", "portable", "Portable game", str(library), version)[
        "job"
    ]

    def job():
        with manager.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()

    try:
        monkeypatch.setattr(game_install, "steam_running", lambda: True)
        manager.update(identifier, state="running")
        manager.receive(job())
        assert job()["state"] == "awaiting_steam"
        count = len(requests)
        manager = game_sync.GameSync(state, threading.Event())
        monkeypatch.setattr(game_install, "steam_running", lambda: False)
        manager.receive(job())
        assert job()["state"] == "completed"
        assert len(requests) == count
        assert (
            Path(job()["destination"]) / "prefix/pfx/drive_c/windows/wine.dll"
        ).read_bytes() == b"actual wine runtime DLL"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_partial_steam_write_rolls_back_and_retry_does_not_redownload(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    version = game_files.version_id(manifest)
    destination = tmp_path / "destination"
    steam_home(destination, appid=2147484999)
    monkeypatch.setattr(Path, "home", lambda: destination)
    monkeypatch.setattr(game_install, "steam_running", lambda: False)
    state = destination / "state"
    update_config(lambda cfg: cfg.update(account_id="owner"), state)
    installed = destination / "new-install"
    installed.mkdir()
    materialize_files(installed, manifest, mounts)
    steam = destination / ".local/share/Steam"
    registry = steam / "userdata/123/config/shortcuts.vdf"
    config = steam / "config/config.vdf"
    originals = registry.read_bytes(), config.read_bytes()
    write = game_install.write_registry
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == config and not failed:
            failed = True
            raise OSError("simulated disk failure")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(game_install, "write_registry", fail_once)
    with pytest.raises(OSError):
        game_install.register(state, "portable", version, installed, manifest)
    assert (registry.read_bytes(), config.read_bytes()) == originals
    assert "portable" not in configuration(state)["games"]
    assert not (state / "steam-install-journal.json").exists()
    assert (
        game_install.register(state, "portable", version, installed, manifest)["state"] == "ready"
    )
    assert "portable" in configuration(state)["games"]


def test_prefix_retains_dependencies_from_an_older_proton_runtime(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    steam = source / ".local/share/Steam"
    older = steam / "steamapps/common/Proton Older"
    older.mkdir(parents=True)
    (older / "proton").write_text("old proton")
    (older / "legacy.dll").write_bytes(b"legacy prefix dependency")
    prefix = steam / "steamapps/compatdata" / shortcut["shortcut_id"] / "pfx/drive_c/windows"
    (prefix / "legacy.dll").symlink_to(older / "legacy.dll")
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    assert manifest["launch"]["dependency_files"] == ["dependencies/0/legacy.dll"]
    assert any(entry["path"] == "dependencies/0/legacy.dll" for entry in manifest["files"])
    assert not any(entry["path"] == "dependencies/0/proton" for entry in manifest["files"])
    target = tmp_path / "new bundle"
    target.mkdir()
    materialize_files(target, manifest, mounts)
    game_bundle.materialize(target, manifest)
    assert (
        target / "prefix/pfx/drive_c/windows/legacy.dll"
    ).read_bytes() == b"legacy prefix dependency"


def test_helpers_in_the_same_folder_are_all_included(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    first = source / ".local/bin/bootstrap"
    first.write_text(f'#!/bin/sh\nexec "{source}/.local/bin/graphics-helper" "$@"\n')
    binding["original_options"] = f'"{first}" %command% /WineDetectionEnabled:False'
    update_launch(shortcut, binding["original_options"])
    manifest, _ = game_bundle.build(root, "portable", shortcut, binding)
    names = {Path(path).name for path in manifest["launch"]["helpers"]}
    assert names == {"bootstrap", "graphics-helper", "patch.so"}
    assert "/WineDetectionEnabled:False" not in manifest["launch"]["requires"]


def test_install_adopts_only_clean_existing_saves_and_keeps_the_old_files(tmp_path, monkeypatch):
    from nosteam_client.storage import atomic_json, capture, digest

    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    version = game_files.version_id(manifest)
    destination = tmp_path / "destination"
    _, old_shortcut, old_binding = steam_home(destination, appid=2147484999)
    monkeypatch.setattr(Path, "home", lambda: destination)
    monkeypatch.setattr(game_install, "steam_running", lambda: False)
    state = destination / "state"
    update_config(
        lambda cfg: cfg.update(account_id="owner", games={"portable": old_binding}), state
    )
    target = destination / "new-install"
    target.mkdir()
    materialize_files(target, manifest, mounts)
    with pytest.raises(ValueError, match="existing saves"):
        game_install.register(state, "portable", version, target, manifest)
    old_saves = [Path(path) for path in old_binding["roots"]]
    old_bytes = (old_saves[0] / "slot.sav").read_bytes()
    atomic_json(
        state / "games/portable/state.json",
        {"status": "synced", "digest": digest(capture(old_saves))},
    )
    result = game_install.register(state, "portable", version, target, manifest)
    assert result["state"] == "ready"
    assert result["appid"] == int(old_shortcut["shortcut_id"])
    assert (old_saves[0] / "slot.sav").read_bytes() == old_bytes
    assert configuration(state)["games"]["portable"]["roots"] != old_binding["roots"]


def test_resharing_an_installation_uses_its_explicit_prefix(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    original = source / ".local/share/Steam/steamapps/compatdata" / shortcut["shortcut_id"]
    relocated = source / "managed installs/copied-prefix"
    relocated.parent.mkdir()
    original.rename(relocated)
    binding["roots"] = [
        str(relocated / Path(path).relative_to(original)) for path in binding["roots"]
    ]
    binding["original_options"] = (
        f'STEAM_COMPAT_DATA_PATH="{relocated}" ' + binding["original_options"]
    )
    update_launch(shortcut, binding["original_options"])
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    assert mounts["prefix"] == str(relocated)
    assert manifest["launch"]["saves"][0].startswith("prefix/")


def test_current_steam_launch_settings_override_stale_binding_and_keep_flags(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    update_launch(
        shortcut, 'DXVK_ASYNC=1 "/usr/local/bin/nosteam" launch portable -- %command% -updated'
    )
    manifest, _ = game_bundle.build(root, "portable", shortcut, binding)
    assert manifest["launch"]["options"] == "DXVK_ASYNC=1 %command% -updated"
    assert not manifest["launch"]["helpers"]


def test_missing_host_setup_never_modifies_steam_or_existing_saves(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    update_launch(shortcut, "/usr/local/libexec/missing-privileged-helper %command%")
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    destination = tmp_path / "destination"
    steam_home(destination, appid=2147484999)
    monkeypatch.setattr(Path, "home", lambda: destination)
    monkeypatch.setattr(game_install, "steam_running", lambda: False)
    state = destination / "state"
    update_config(lambda cfg: cfg.update(account_id="owner"), state)
    target = destination / "install"
    target.mkdir()
    materialize_files(target, manifest, mounts)
    registry = destination / ".local/share/Steam/userdata/123/config/shortcuts.vdf"
    original = registry.read_bytes()
    result = game_install.register(
        state, "portable", game_files.version_id(manifest), target, manifest
    )
    assert result["state"] == "requires_dependency"
    assert "missing-privileged-helper" in result["message"]
    assert registry.read_bytes() == original
    assert not configuration(state)["games"]


def test_imported_prefix_with_legacy_dlls_can_be_shared_to_a_third_device(tmp_path, monkeypatch):
    from nosteam_client.steam import discover

    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    older = source / ".local/share/Steam/steamapps/common/Proton Legacy"
    older.mkdir(parents=True)
    (older / "proton").write_text("legacy proton")
    (older / "legacy.dll").write_bytes(b"required legacy DLL")
    prefix = source / ".local/share/Steam/steamapps/compatdata" / shortcut["shortcut_id"]
    (prefix / "pfx/drive_c/windows/legacy.dll").symlink_to(older / "legacy.dll")
    manifest, mounts = game_bundle.build(root, "portable", shortcut, binding)
    destination = tmp_path / "destination"
    steam_home(destination, appid=2147484999)
    monkeypatch.setattr(Path, "home", lambda: destination)
    monkeypatch.setattr(game_install, "steam_running", lambda: False)
    state = destination / "state"
    update_config(lambda cfg: cfg.update(account_id="owner"), state)
    target = destination / "library/files"
    target.mkdir(parents=True)
    materialize_files(target, manifest, mounts)
    game_install.register(state, "portable", game_files.version_id(manifest), target, manifest)
    installed_binding = configuration(state)["games"]["portable"]
    installed_shortcut = next(
        item for item in discover() if item["shortcut_id"] == shortcut["shortcut_id"]
    )
    new_manifest, new_mounts = game_bundle.build(
        target / "game", "portable", installed_shortcut, installed_binding
    )
    assert new_mounts["prefix"] == str(target / "prefix")
    assert new_manifest["launch"]["dependency_files"] == ["dependencies/0/legacy.dll"]
    assert len(new_manifest["launch"]["helpers"]) == 2
    assert len(new_manifest["launch"]["artwork"]) == 1
    assert "nosteam launch" not in new_manifest["launch"]["options"]


def test_linux_prefix_preserves_case_distinct_wine_paths(tmp_path, monkeypatch):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    manifest, _ = game_bundle.build(root, "portable", shortcut, binding)
    manifest["launch"]["platform"] = {"os": "Linux", "arch": "x86_64"}
    manifest["directories"] += [
        "prefix/pfx/drive_c/windows/DriverStore",
        "prefix/pfx/drive_c/windows/driverstore",
    ]
    assert game_files.validate(manifest) > 0
    manifest["directories"].append("prefix/pfx/drive_c/windows/DriverStore")
    with pytest.raises(ValueError, match="Duplicate"):
        game_files.validate(manifest)


def test_make_available_uses_steam_installation_and_queues_only_one_background_job(
    tmp_path, monkeypatch
):
    from nosteam_client.automatic import shortcut_key

    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    state = source / "state"
    update_config(
        lambda cfg: cfg.update(
            account_id="owner", game_sync_enabled=True, games={"portable": binding}
        ),
        state,
    )
    manager = game_sync.GameSync(state, threading.Event())
    monkeypatch.setattr(game_sync, "discover", lambda: [shortcut])
    result = manager.share(shortcut_key(shortcut))
    assert manager.share(shortcut_key(shortcut)) == result
    with manager.connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (result["job"],)).fetchone()
    assert job["root"] == str(root)
    assert job["state"] == "queued"
    assert not manager.status()["shared_games"]
    manager.update(result["job"], state="running")
    manager.index(job)
    status = manager.status()
    assert status["shared_games"] == ["portable"]
    assert status["jobs"][0]["verified"] == status["jobs"][0]["total"] > 0


def test_installation_preparation_reports_bytes_and_rejects_generic_launcher_folders(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    root, shortcut, binding = steam_home(source)
    monkeypatch.setattr(Path, "home", lambda: source)
    progress = []
    manifest, _ = game_bundle.build(
        root,
        "portable",
        shortcut,
        binding,
        progress=lambda done, total: progress.append((done, total)),
    )
    total = sum(entry["size"] for entry in manifest["files"])
    assert progress[0] == (0, total)
    assert progress[-1] == (total, total)
    assert [done for done, _ in progress] == sorted(done for done, _ in progress)
    shortcut["exe"] = str(source / ".local/bin/graphics-helper")
    with pytest.raises(ValueError, match="launcher"):
        game_bundle.installation_root(shortcut)
