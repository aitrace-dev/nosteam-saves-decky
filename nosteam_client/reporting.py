"""Read-only save observations and metadata-only device heartbeats."""

import json
import time

from .config import configuration, lock
from .storage import atomic_json, capture, digest, read_json, roots_for
from .sync import Remote

STATUSES = {
    "synced",
    "ready",
    "pending",
    "conflict",
    "playing",
    "checking",
    "uploading",
    "downloading",
    "error",
    "offline",
    "interrupted",
    "recovering",
    "recovered",
    "saving",
}


def observations(home, budget=5):
    cfg = configuration(home)
    if len(cfg["games"]) > 100:
        raise ValueError("Device reporting currently supports up to 100 configured games")
    deadline = time.monotonic() + budget
    rows = []
    items = list(cfg["games"].items())
    cursor = read_json(home / "report-sequence.json", {"sequence": 0})["sequence"] % max(
        1, len(items)
    )
    for game_id, game in items[cursor:] + items[:cursor]:
        folder = home / "games" / game_id
        state = read_json(folder / "state.json", {})
        pending = (folder / "pending.json").exists()
        row = {
            "game": game_id,
            "title": game["title"][:200],
            "revision": state.get("base"),
            "status": state.get("status") if state.get("status") in STATUSES else "unknown",
            "pending": pending,
            "dirty": None,
            "configured": not game.get("removed", False),
        }
        if (folder / "restore.json").exists():
            row["status"] = "recovering"
        elif row["configured"] and row["status"] in {"synced", "ready", "pending", "conflict"}:
            # Never hold a game-session lock while reading saves: reporting must not
            # prevent the user's launch wrapper from starting. A concurrent wrapper
            # changes the atomic state record, invalidating this observation.
            if time.monotonic() < deadline:
                try:
                    roots = roots_for(game, home)
                    first = digest(capture(roots))
                    stable = first == digest(capture(roots))
                    stable = stable and state == read_json(folder / "state.json", {})
                    stable = stable and pending == (folder / "pending.json").exists()
                    if stable:
                        row["dirty"] = first != state.get("digest")
                except (ValueError, OSError):
                    row["status"] = "error"
        rows.append(row)
    return rows


def cached_status(home):
    cfg = configuration(home)
    cache = read_json(home / "device-status.json", {})
    if cache.get("account_id") != cfg.get("account_id") or cache.get("server") != cfg.get("server"):
        return None
    return cache.get("status")


def fetch_status(home, remote=None):
    cfg = configuration(home)
    own = remote is None
    remote = remote or Remote(cfg, home=home)
    try:
        result = remote.request("GET", "/v1/sync/status")
        atomic_json(
            home / "device-status.json",
            {
                "account_id": cfg.get("account_id"),
                "server": cfg["server"],
                "status": result,
            },
        )
        return result
    finally:
        if own:
            remote.close()


def publish(home):
    cfg = configuration(home)
    if not cfg.get("token") or not cfg.get("server"):
        return None
    games = observations(home)
    # Serialize sequence allocation, not network or save reads. Delayed older
    # requests cannot replace a newer accepted report on the server.
    with lock(home / "report.lock"):
        saved = read_json(home / "report-sequence.json", {"sequence": 0})
        sequence = saved["sequence"] + 1
        atomic_json(home / "report-sequence.json", {"sequence": sequence})
    payload = {"sequence": sequence, "games": games}
    if len(json.dumps(payload).encode()) > 60 * 1024:
        raise ValueError("Device report exceeds metadata limit")
    remote = Remote(cfg, home=home)
    try:
        accepted = remote.request("POST", "/v1/sync/report", json=payload)
        if not accepted["accepted"]:
            with lock(home / "report.lock"):
                current = read_json(home / "report-sequence.json", {"sequence": 0})
                atomic_json(
                    home / "report-sequence.json",
                    {
                        "sequence": max(current["sequence"], accepted["sequence"]),
                    },
                )
        return fetch_status(home, remote)
    finally:
        remote.close()
