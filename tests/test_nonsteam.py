import shlex
from pathlib import Path

import pytest
import vdf
from nosteam_client.cli import bind
from nosteam_client.config import configuration, update_config
from nosteam_client.steam import discover, environment_guard, launch_options, verify_shortcut
from nosteam_client.storage import capture, checked_root, validate


@pytest.fixture
def steam_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    registry = home / ".local/share/Steam/userdata/123/config/shortcuts.vdf"
    registry.parent.mkdir(parents=True)
    data = {
        "shortcuts": {
            "0": {
                "appid": 2147484000 - 2**32,
                "appname": "Non-Steam Game",
                "exe": '"/games/game.exe"',
                "LaunchOptions": "gamemoderun %command% -windowed",
            },
            "1": {"appid": 570, "appname": "Steam Game", "exe": "dota2"},
        }
    }
    with registry.open("wb") as stream:
        vdf.binary_dump(data, stream)
    # A real installed Steam game is present but must never be discovered.
    manifest = home / ".local/share/Steam/steamapps/appmanifest_570.acf"
    manifest.parent.mkdir()
    manifest.write_text('"AppState" { "appid" "570" "name" "Steam Game" }')
    return home, registry


def test_discovery_only_lists_verified_nonsteam_shortcuts(steam_home):
    home, _ = steam_home
    games = discover(home)
    assert [game["title"] for game in games] == ["Non-Steam Game"]


def test_normal_steam_app_cannot_be_bound(steam_home, tmp_path):
    _, registry = steam_home
    with pytest.raises(ValueError, match="Non-Steam shortcut"):
        bind(tmp_path / "state", "570", str(registry), "game", [str(tmp_path / "saves")])


def test_binding_preview_preserves_original_launch_settings(steam_home, tmp_path):
    _, registry = steam_home
    state = tmp_path / "state"
    update_config(lambda current: current.update(server="http://localhost", token="test"), state)
    result = bind(state, "2147484000", str(registry), "game", [str(tmp_path / "saves")])
    assert result["launch_options"].startswith("gamemoderun ")
    assert result["launch_options"].endswith("%command% -windowed")
    assert configuration(state)["games"]["game"]["source"] == "steam-shortcut"
    # Binding never writes to Steam's registry.
    assert discover()[0]["launch_options"] == "gamemoderun %command% -windowed"


def test_same_shortcut_cannot_be_enabled_twice(steam_home, tmp_path):
    _, registry = steam_home
    state = tmp_path / "state"
    update_config(lambda current: current.update(server="http://localhost"), state)
    bind(state, "2147484000", str(registry), "first", [str(tmp_path / "saves")])
    with pytest.raises(ValueError, match="already enabled"):
        bind(state, "2147484000", str(registry), "second", [str(tmp_path / "saves-2")])


def test_removed_shortcut_cannot_be_used(steam_home):
    _, registry = steam_home
    game = {"source": "steam-shortcut", "shortcut_id": "2147484000", "registry": str(registry)}
    verify_shortcut(game)
    registry.unlink()
    with pytest.raises(ValueError, match="no longer exists"):
        verify_shortcut(game)


def test_pasting_wrapper_on_steam_game_is_rejected(monkeypatch):
    monkeypatch.setenv("SteamAppId", "570")
    with pytest.raises(ValueError, match="non-Steam shortcut"):
        environment_guard({"shortcut_id": "2147484000"})


def test_encoded_shortcut_game_id_is_accepted(monkeypatch):
    monkeypatch.setenv("SteamAppId", "0")
    monkeypatch.setenv("SteamGameId", str((2147484000 << 32) | 0x02000000))
    environment_guard({"shortcut_id": "2147484000"})


def test_launch_expression_preserves_environment_and_quoted_arguments():
    original = 'MANGOHUD=1 gamemoderun %command% "argument with spaces"'
    result = launch_options("game-id", original, "/home/a user/nosteam")
    assert result == (
        "MANGOHUD=1 gamemoderun '/home/a user/nosteam' launch game-id -- "
        '%command% "argument with spaces"'
    )
    assert shlex.split(launch_options("game", "--fullscreen", "/bin/nosteam"))[-1] == "--fullscreen"


def test_symlinks_and_sensitive_roots_are_rejected(tmp_path):
    root = tmp_path / "saves"
    root.mkdir()
    target = tmp_path / "private"
    target.write_text("private")
    (root / "slot.sav").symlink_to(target)
    with pytest.raises(ValueError, match="Symlinks"):
        capture([root])
    with pytest.raises(ValueError):
        checked_root(str(Path.home()), tmp_path / "state")
    with pytest.raises(ValueError):
        checked_root(str(tmp_path), tmp_path / "state")


def test_unsafe_remote_file_and_directory_collision_rejected():
    with pytest.raises(ValueError, match="Conflicting"):
        validate({"0/save": "eA==", "0/save/child": "eA=="}, 1)
    with pytest.raises(ValueError, match="Unsafe"):
        validate({"1/save": "eA=="}, 1)


def test_bazzite_home_alias_is_normalized_without_allowing_save_symlinks(tmp_path, monkeypatch):
    real_home = tmp_path / "var/home/player"
    real_home.mkdir(parents=True)
    home_alias = tmp_path / "home-player"
    home_alias.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home_alias)
    save = real_home / "game/saves"
    save.mkdir(parents=True)
    assert checked_root(str(home_alias / "game/saves"), real_home / "state") == save
    linked_save = real_home / "linked-save"
    linked_save.symlink_to(save, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        checked_root(str(home_alias / "linked-save"), real_home / "state")
    with pytest.raises(ValueError):
        checked_root(str(home_alias), real_home / "state")


def test_companion_shortcut_is_not_offered_as_a_game(steam_home):
    home, registry = steam_home
    with registry.open("rb") as stream:
        data = vdf.binary_load(stream)
    data["shortcuts"]["2"] = {
        "appid": 2147484999 - 2**32,
        "appname": "NoSteam",
        "exe": f'"{home}/.local/bin/nosteam-companion"',
    }
    with registry.open("wb") as stream:
        vdf.binary_dump(data, stream)
    assert [game["title"] for game in discover(home)] == ["Non-Steam Game"]
