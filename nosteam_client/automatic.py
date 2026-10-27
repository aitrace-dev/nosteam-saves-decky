"""Discover every eligible shortcut, negotiate unknown paths, and prepare safe auto-bindings."""

import hashlib
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

import httpx

from .catalog import suggest, update
from .config import configuration, lock
from .discovery import cached, negotiate
from .steam import discover
from .storage import atomic_json, capture, read_json
from .sync import Offline, Remote


def shortcut_key(shortcut):
    return hashlib.sha256(f"{shortcut['registry']}:{shortcut['shortcut_id']}".encode()).hexdigest()


def game_identity(shortcut, matched_title=None):
    title = unicodedata.normalize("NFKC", matched_title or shortcut["title"]).casefold()
    title = " ".join(re.findall(r"\w+", title))
    if not title or title in {"unknown", "game"}:
        return None
    # Canonical catalog names survive shortcut renaming/installation path differences.
    identity = (
        "catalog:" + title
        if matched_title
        else "shortcut:" + title + ":" + Path(shortcut["exe"].strip('"')).name.casefold()
    )
    return "game-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def plan(home, now=None):
    now = time.time() if now is None else now
    config = configuration(home)
    if not config.get("token"):
        return {"ready": [], "pending": []}
    with lock(home / "automation.lock", blocking=False):
        state = read_json(home / "automation.json", {})
        if now >= state.get("catalog_retry", 0):
            try:
                update(home)
                state["catalog_retry"] = now + 86400
            except (httpx.HTTPError, ValueError):
                state["catalog_retry"] = now + 3600
        catalog = read_json(home / "catalog.json", {})
        shortcuts = discover()
        from .library import reconcile

        config = reconcile(home, shortcuts)
        suggestions = {shortcut_key(s): suggest(s, home, catalog) for s in shortcuts}
        identities = {
            shortcut_key(s): game_identity(s, suggestions[shortcut_key(s)].get("matched_title"))
            for s in shortcuts
        }
        counts = Counter(identities.values())
        enabled = {(g["registry"], g["shortcut_id"]) for g in config["games"].values()}
        ready, pending = [], []
        attempts = state.setdefault("attempts", {})
        remote = Remote(config, home=home)
        try:
            for shortcut in shortcuts:
                if (shortcut["registry"], shortcut["shortcut_id"]) in enabled:
                    continue
                key = shortcut_key(shortcut)
                if key in config.get("paused_shortcuts", []):
                    continue
                identity = identities[key]
                if not identity or counts[identity] != 1:
                    pending.append({**shortcut, "status": "identifying"})
                    continue
                paths = suggestions[key]["suggested_roots"] or cached(home, shortcut)
                if (
                    len(paths) != 1
                    and now >= attempts.get(key, 0)
                    and now >= state.get("ai_retry", 0)
                ):
                    # Catalog/cache matches do not consume the server model budget.
                    # Queue fairly across games instead of repeatedly restarting one unknown game.
                    failures = state.setdefault("searches", {}).get(key, 0)
                    attempts[key] = now + min(7, 2 ** min(failures, 3)) * 86400
                    state["searches"][key] = failures + 1
                    state["ai_retry"] = now + 600
                    try:
                        paths = negotiate(home, shortcut, remote)["suggested_roots"]
                    except (Offline, httpx.HTTPError, ValueError):
                        attempts[key] = now + 1800
                        paths = []
                # Multiple possible account/installation folders remain pending for a future
                # server conversation; they are never combined or presented as a mandatory picker.
                if len(paths) != 1:
                    pending.append({**shortcut, "status": "finding_saves"})
                    continue
                try:
                    if not capture([Path(paths[0])]) and not suggestions[key].get("matched_title"):
                        pending.append({**shortcut, "status": "waiting_for_save"})
                        continue
                except (ValueError, OSError):
                    pending.append({**shortcut, "status": "finding_saves"})
                    continue
                ready.append({**shortcut, "game_id": identity, "roots": paths})
        finally:
            remote.close()
            state["pending"] = pending
            atomic_json(home / "automation.json", state)
        return {"ready": ready, "pending": pending}
