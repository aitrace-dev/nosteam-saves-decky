import json
import subprocess
import sys
from pathlib import Path

import pytest
from nosteam_client.config import lock, update_config
from nosteam_client.migration import migrate


def environment(tmp_path, monkeypatch):
    user = tmp_path / "player home"
    monkeypatch.setattr(Path, "home", lambda: user)
    settings = user / "homebrew/settings/loader.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"disabled_plugins": ["NoSteam Saves"]}))
    home = user / ".local/share/nosteam-saves"
    update_config(lambda cfg: cfg.update(games={"game": {"title": "Game"}}), home)
    launcher = home / "nosteam"
    launcher.write_text(
        "#!/bin/sh\nexport NOSTEAM_BUNDLED_PATH="
        + str(user / "homebrew/plugins/nosteam-saves/vendor")
    )
    saves = user / "actual-saves"
    saves.mkdir()
    (saves / "slot.sav").write_bytes(b"precious progress")
    return home, launcher, settings, saves


def test_migration_preserves_state_and_boots_installed_runtime(tmp_path, monkeypatch):
    home, launcher, settings, saves = environment(tmp_path, monkeypatch)
    original = launcher.read_bytes()
    config = (home / "config.json").read_bytes()
    assert migrate(home, sys.executable)["status"] == "migrated"
    assert list((home / "launcher-backups").glob("*.sh"))[0].read_bytes() == original
    assert (home / "config.json").read_bytes() == config
    assert (saves / "slot.sav").read_bytes() == b"precious progress"
    result = subprocess.run([str(launcher), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "launch" in result.stdout
    assert launcher.stat().st_mode & 0o777 == 0o755


def test_migration_refuses_active_decky_or_active_game(tmp_path, monkeypatch):
    home, launcher, settings, saves = environment(tmp_path, monkeypatch)
    original = launcher.read_bytes()
    settings.write_text("{}")
    with pytest.raises(ValueError, match="Disable"):
        migrate(home, sys.executable)
    assert launcher.read_bytes() == original
    settings.write_text(json.dumps({"disabled_plugins": ["NoSteam Saves"]}))
    with lock(home / "games/game/session.lock"):
        with pytest.raises(BlockingIOError):
            migrate(home, sys.executable)
    assert launcher.read_bytes() == original
    assert (saves / "slot.sav").read_bytes() == b"precious progress"
