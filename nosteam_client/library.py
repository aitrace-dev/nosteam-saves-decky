"""Local shortcut lifecycle and reversible sync exclusions. No network or save writes."""

from .config import configuration, update_config
from .steam import discover, verify_shortcut
from .storage import read_json


def reconcile(home, shortcuts):
    present = {(item["registry"], item["shortcut_id"]) for item in shortcuts}
    config = configuration(home)
    changes = {
        key: (game["registry"], game["shortcut_id"]) not in present
        for key, game in config["games"].items()
        if bool(game.get("removed")) != ((game["registry"], game["shortcut_id"]) not in present)
    }
    if changes:

        def change(current):
            for key, removed in changes.items():
                if key in current["games"]:
                    current["games"][key]["removed"] = removed

        update_config(change, home)
    return configuration(home)


def library(home):
    from .automatic import shortcut_key

    shortcuts = discover()
    config = reconcile(home, shortcuts)
    bindings = {
        (game["registry"], game["shortcut_id"]): (key, game)
        for key, game in config["games"].items()
    }
    pending = {
        shortcut_key(item): item
        for item in read_json(home / "automation.json", {}).get("pending", [])
    }
    rows = []
    for shortcut in shortcuts:
        key = shortcut_key(shortcut)
        bound = bindings.get((shortcut["registry"], shortcut["shortcut_id"]))
        excluded = key in config.get("paused_shortcuts", [])
        state = read_json(home / "games" / bound[0] / "state.json", {}) if bound else {}
        status = (
            "excluded"
            if excluded
            else state.get("status", "ready")
            if bound
            else pending.get(key, {}).get("status", "finding_saves")
        )
        rows.append(
            {
                **shortcut,
                "key": key,
                "game_id": bound[0] if bound else None,
                "excluded": excluded,
                "removed": False,
                "status": status,
            }
        )
    for game_id, game in config["games"].items():
        if game.get("removed"):
            rows.append(
                {
                    **game,
                    "key": shortcut_key(game),
                    "game_id": game_id,
                    "excluded": False,
                    "removed": True,
                    "status": "removed",
                }
            )
    return rows


def set_excluded(home, shortcut_id, registry, excluded):
    from .automatic import shortcut_key

    shortcut = verify_shortcut(
        {"source": "steam-shortcut", "shortcut_id": shortcut_id, "registry": registry}
    )
    key = shortcut_key(shortcut)

    def change(config):
        if excluded and any(
            (game["registry"], game["shortcut_id"]) == (registry, shortcut_id)
            for game in config["games"].values()
        ):
            raise ValueError("Restore this game's Steam launch options before excluding it")
        paused = config.setdefault("paused_shortcuts", [])
        if excluded and key not in paused:
            paused.append(key)
        if not excluded and key in paused:
            paused.remove(key)

    update_config(change, home)
    return {"status": "excluded" if excluded else "included"}
