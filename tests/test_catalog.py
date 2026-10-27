from contextlib import contextmanager
from pathlib import Path

import httpx
from nosteam_client.catalog import suggest, update
from nosteam_client.storage import read_json


def test_definition_download_is_validated_before_replacing_cache(tmp_path, monkeypatch):
    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(
            200,
            content=b'Example Game:\n  files:\n    "<home>/saves":\n      tags: [save]\n',
            request=httpx.Request("GET", "https://test"),
        )

    monkeypatch.setattr(httpx, "stream", stream)
    result = update(tmp_path)
    assert result["games"] == 1
    assert read_json(tmp_path / "catalog.json")["Example Game"]["files"]


def shortcut(home, title="Example Game"):
    return {
        "title": title,
        "shortcut_id": "2147484000",
        "exe": '"/games/example.exe"',
        "launch_options": "",
        "registry": str(home / ".local/share/Steam/userdata/123/config/shortcuts.vdf"),
    }


def test_native_save_folder_suggestion_is_read_only(tmp_path, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: home)
    saves = home / ".local/share/example/saves"
    saves.mkdir(parents=True)
    (saves / "slot.sav").write_text("untouched")
    entries = {
        "Example Game": {
            "files": {
                "<xdgData>/example/saves": {"tags": ["save"]},
                "<xdgConfig>/example": {"tags": ["config"]},
            }
        }
    }
    result = suggest(shortcut(home), tmp_path / "state", entries)
    assert result["suggested_roots"] == [str(saves)]
    assert (saves / "slot.sav").read_text() == "untouched"


def test_proton_suggestion_only_scans_selected_shortcuts_prefix(tmp_path, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: home)
    base = home / ".local/share/Steam/steamapps/compatdata"
    selected = base / "2147484000/pfx/drive_c/users/steamuser/AppData/Roaming/Example/Saves"
    unrelated = base / "570/pfx/drive_c/users/steamuser/AppData/Roaming/Example/Saves"
    selected.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    entries = {
        "Example Game": {
            "files": {
                "<winAppData>/Example/Saves": {"tags": ["save"]},
            }
        }
    }
    assert suggest(shortcut(home), tmp_path / "state", entries)["suggested_roots"] == [
        str(selected)
    ]


def test_unknown_and_ambiguous_titles_require_manual_selection(tmp_path):
    entries = {"Example Game": {"files": {}}, "Example-Game": {"files": {}}}
    assert (
        suggest(shortcut(tmp_path, "Other"), tmp_path / "state", entries)["matched_title"] is None
    )
    assert suggest(shortcut(tmp_path), tmp_path / "state", entries)["suggested_roots"] == []


def test_globs_and_file_only_rules_do_not_expand_to_broad_parent_folders(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / "save.dat").write_text("progress")
    entries = {
        "Example Game": {
            "files": {
                "<home>/save.dat": {"tags": ["save"]},
                "<home>/*": {"tags": ["save"]},
            }
        }
    }
    assert suggest(shortcut(home), tmp_path / "state", entries)["suggested_roots"] == []


def test_account_placeholder_resolves_only_inside_selected_shortcut_prefix(tmp_path, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: home)
    prefix = home / ".local/share/Steam/steamapps/compatdata/2147484000/pfx"
    accounts = prefix / "drive_c/users/steamuser/AppData/Local/Game/Steam"
    save = accounts / "123456789"
    save.mkdir(parents=True)
    entries = {
        "Example Game": {
            "files": {
                "<winLocalAppData>/Game/Steam/<storeUserId>": {"tags": ["save"]},
                "<root>/userdata/<storeUserId>/570/remote": {"tags": ["save"]},
            }
        }
    }
    assert suggest(shortcut(home), tmp_path / "state", entries)["suggested_roots"] == [str(save)]
    (accounts / "987654321").mkdir()
    assert suggest(shortcut(home), tmp_path / "state", entries)["suggested_roots"] == []


def test_onimusha_nonsteam_save_layout_is_found_without_steam_userdata(tmp_path, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: home)
    prefix = home / ".local/share/Steam/steamapps/compatdata/2147484000/pfx"
    saves = prefix / "drive_c/users/steamuser/AppData/Roaming/GSE Saves/2638890/remote/win64_save"
    saves.mkdir(parents=True)
    (saves / "data001Slot.bin").write_bytes(b"disposable save")
    result = suggest(shortcut(home, "Onimusha - Way of the Sword"), tmp_path / "state", {})
    assert result["matched_title"] == "Onimusha: Way of the Sword"
    assert result["suggested_roots"] == [str(saves)]
    assert (saves / "data001Slot.bin").read_bytes() == b"disposable save"
