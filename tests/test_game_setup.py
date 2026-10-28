import pytest
from nosteam_client import game_setup
from nosteam_client.automatic import shortcut_key
from nosteam_client.config import configuration, update_config
from nosteam_client.steam import discover
from test_nonsteam import steam_home  # noqa: F401


@pytest.fixture
def setup_environment(steam_home, tmp_path, monkeypatch):  # noqa: F811
    _, registry = steam_home
    home = tmp_path / "state"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "progress.sav").write_bytes(b"precious progress")
    update_config(
        lambda cfg: cfg.update(server="https://example.test", token="token", account_id="owner"),
        home,
    )

    class Remote:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args):
            return {"games": []}

        def close(self):
            pass

    monkeypatch.setattr(game_setup, "Remote", Remote)
    monkeypatch.setattr(game_setup, "steam_running", lambda: False)
    return home, registry, saves, shortcut_key(discover()[0])


def test_setup_preserves_saves_and_original_launch_options(setup_environment):
    home, registry, saves, key = setup_environment
    original = registry.read_bytes()
    result = game_setup.setup(home, key, str(saves))
    assert result["status"] == "configured"
    assert "nosteam" in discover()[0]["launch_options"]
    assert discover()[0]["launch_options"].endswith("%command% -windowed")
    assert registry.read_bytes() != original
    assert list((home / "steam-backups").glob("*.json"))
    assert not (home / "setup-journal.json").exists()
    assert (saves / "progress.sav").read_bytes() == b"precious progress"


def test_failed_registry_write_rolls_back_configuration_only(setup_environment, monkeypatch):
    home, registry, saves, key = setup_environment
    original = registry.read_bytes()

    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(game_setup, "write_registry", fail)
    with pytest.raises(OSError):
        game_setup.setup(home, key, str(saves))
    assert registry.read_bytes() == original
    assert configuration(home)["games"] == {}
    assert (saves / "progress.sav").read_bytes() == b"precious progress"


def test_running_steam_refuses_setup_without_writes(setup_environment, monkeypatch):
    home, registry, saves, key = setup_environment
    original = registry.read_bytes()
    monkeypatch.setattr(game_setup, "steam_running", lambda: True)
    with pytest.raises(ValueError, match="Close Steam"):
        game_setup.setup(home, key, str(saves))
    assert registry.read_bytes() == original
    assert not list(home.glob("steam-backups/*"))
