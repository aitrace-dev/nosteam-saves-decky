import pytest
import vdf
from nosteam_client.cli import bind, execute, parser
from nosteam_client.config import configuration, update_config
from nosteam_client.storage import atomic_json
from test_nonsteam import steam_home as steam_home


def run(home, *arguments):
    return execute(parser().parse_args(arguments), home)


def test_exclude_and_include_survive_refresh_without_editing_steam(steam_home, tmp_path):
    _, registry = steam_home
    state = tmp_path / "state"
    original = registry.read_bytes()
    run(state, "exclude", "2147484000", "--registry", str(registry))
    row = run(state, "status")["library"][0]
    assert row["excluded"] is True and row["status"] == "excluded"
    assert registry.read_bytes() == original
    run(state, "include", "2147484000", "--registry", str(registry))
    assert run(state, "status")["library"][0]["excluded"] is False
    assert registry.read_bytes() == original


def test_added_removed_and_readded_shortcuts_keep_backups(steam_home, tmp_path):
    _, registry = steam_home
    state = tmp_path / "state"
    update_config(lambda cfg: cfg.update(server="http://localhost", token="test"), state)
    bind(state, "2147484000", str(registry), "game", [str(tmp_path / "saves")])
    history = state / "games/game/history.json"
    atomic_json(history, {"preserved": True})
    original = registry.read_bytes()
    registry.unlink()
    row = run(state, "status")["library"][0]
    assert row["removed"] is True and row["status"] == "removed"
    assert configuration(state)["games"]["game"]["removed"] is True
    assert history.exists()
    registry.write_bytes(original)
    with registry.open("rb") as stream:
        data = vdf.binary_load(stream)
    data["shortcuts"]["2"] = {
        "appid": 2147484001 - 2**32,
        "appname": "New Non-Steam Game",
        "exe": '"/games/new.exe"',
    }
    with registry.open("wb") as stream:
        vdf.binary_dump(data, stream)
    rows = run(state, "status")["library"]
    assert {row["title"] for row in rows} == {"Non-Steam Game", "New Non-Steam Game"}
    assert not any(row["removed"] for row in rows)
    assert configuration(state)["games"]["game"]["removed"] is False
    assert history.exists()


def test_reinstalled_shortcut_can_reconnect_without_deleting_old_progress(steam_home, tmp_path):
    _, registry = steam_home
    state = tmp_path / "state"
    update_config(lambda cfg: cfg.update(server="http://localhost", token="test"), state)
    old_root = tmp_path / "old-saves"
    old_root.mkdir()
    (old_root / "slot.sav").write_text("old progress")
    bind(state, "2147484000", str(registry), "game", [str(old_root)])
    original = registry.read_bytes()
    registry.unlink()
    run(state, "status")
    registry.write_bytes(original)
    with registry.open("rb") as stream:
        data = vdf.binary_load(stream)
    data["shortcuts"]["0"]["appid"] = 2147484001 - 2**32
    with registry.open("wb") as stream:
        vdf.binary_dump(data, stream)
    new_root = tmp_path / "new-saves"
    result = bind(state, "2147484001", str(registry), "game", [str(new_root)])
    assert result["game_id"] == "game"
    assert configuration(state)["games"]["game"]["shortcut_id"] == "2147484001"
    assert (old_root / "slot.sav").read_text() == "old progress"


def test_late_automatic_binding_cannot_override_an_exclusion(steam_home, tmp_path):
    import pytest

    _, registry = steam_home
    state = tmp_path / "state"
    update_config(lambda cfg: cfg.update(server="http://localhost"), state)
    run(state, "exclude", "2147484000", "--registry", str(registry))
    with pytest.raises(ValueError, match="excluded"):
        bind(state, "2147484000", str(registry), "game", [str(tmp_path / "saves")])
    assert not configuration(state)["games"]


def test_active_session_prevents_exclusion(steam_home, tmp_path):
    import pytest
    from nosteam_client.config import lock

    _, registry = steam_home
    state = tmp_path / "state"
    update_config(lambda cfg: cfg.update(server="http://localhost"), state)
    bind(state, "2147484000", str(registry), "game", [str(tmp_path / "saves")])
    with lock(state / "games/game/session.lock"):
        with pytest.raises(BlockingIOError):
            run(state, "disable", "game")
    assert "game" in configuration(state)["games"]
    assert not configuration(state).get("paused_shortcuts")


def test_ordinary_steam_games_cannot_be_excluded_or_listed(steam_home, tmp_path):
    import pytest

    _, registry = steam_home
    state = tmp_path / "state"
    with pytest.raises(ValueError, match="Non-Steam shortcut"):
        run(state, "exclude", "570", "--registry", str(registry))
    assert all(row["shortcut_id"] != "570" for row in run(state, "status")["library"])


@pytest.mark.parametrize("candidate_count", [1, 2])
def test_first_save_rechecks_previous_candidates_locally_without_another_ai_call(
    steam_home, tmp_path, candidate_count
):
    import uuid

    from nosteam_client.discovery import cached, negotiate
    from nosteam_client.steam import discover

    home, _ = steam_home
    roaming = (
        home
        / ".local/share/Steam/steamapps/compatdata/2147484000/pfx"
        / "drive_c/users/steamuser/AppData/Roaming"
    )
    roaming.mkdir(parents=True)
    shortcut = discover()[0]
    state = tmp_path / "state"
    session = str(uuid.uuid4())

    class Remote:
        def request(self, method, path, **kwargs):
            if path == "/v1/discovery":
                return {
                    "session": session,
                    "round": 1,
                    "status": "searching",
                    "candidates": [
                        {"root": "roaming-0", "path": "Studio/" + name, "operation": "probe"}
                        for name in ["NewGame", "OtherGame"][:candidate_count]
                    ],
                }
            assert kwargs["json"]["observations"][0]["status"] == "missing"
            return {"session": session, "round": 1, "status": "needs_save", "matches": []}

    assert negotiate(state, shortcut, Remote())["suggested_roots"] == []
    save = roaming / "Studio/NewGame"
    save.mkdir(parents=True)
    (save / "slot.sav").write_text("first progress")
    if candidate_count == 2:
        other = roaming / "Studio/OtherGame"
        other.mkdir(parents=True)
        (other / "slot.sav").write_text("different progress")
    assert cached(state, shortcut) == ([str(save)] if candidate_count == 1 else [])
