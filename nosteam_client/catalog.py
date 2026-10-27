"""Conservative suggestions from Ludusavi and reviewed JSON recipes. Never enables a game."""

import json
import re
import shlex
from itertools import islice
from pathlib import Path

import httpx
import yaml

from .storage import atomic_json, checked_root, read_json

MANIFEST_URL = (
    "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml"
)


def account_paths(template: str, prefix: str | None) -> list[str]:
    if "<storeUserId>" not in template:
        return [template]
    if prefix is None or template.count("<storeUserId>") != 1:
        return []
    before, after = template.split("<storeUserId>")
    parent = Path(before)
    # Resolve account subfolders only inside this non-Steam shortcut's prefix.
    # Never expand <root>/userdata into installed Steam games' save trees.
    if (
        any(character in before + after for character in ("<", ">", "*", "?", "["))
        or not parent.is_absolute()
        or Path(prefix).resolve() not in parent.resolve().parents
        or not parent.is_dir()
    ):
        return []
    children = list(islice(parent.iterdir(), 33))
    if len(children) > 32:
        return []
    matches = [
        child / after.lstrip("/")
        for child in children
        if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", child.name)
        and child.is_dir()
        and not child.is_symlink()
        and (child / after.lstrip("/")).is_dir()
    ]
    # Multiple accounts require explicit selection, never combined progress.
    return [str(matches[0])] if len(matches) == 1 else []


def normalized(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.casefold())


def update(home: Path):
    content = bytearray()
    with httpx.stream("GET", MANIFEST_URL, timeout=30, follow_redirects=True) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 40 * 1024 * 1024:
                raise ValueError("Game database exceeds the download limit")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    manifest = yaml.load(bytes(content), Loader=loader)
    if not isinstance(manifest, dict):
        raise ValueError("Invalid game database")
    entries = {}
    for title, definition in manifest.items():
        if isinstance(title, str) and isinstance(definition, dict):
            entries[title] = {
                key: definition[key] for key in ("files", "alias") if key in definition
            }
    atomic_json(home / "catalog.json", entries)
    return {"games": len(entries), "source": MANIFEST_URL}


def prefixes(shortcut: dict) -> list[Path]:
    registry = Path(shortcut["registry"])
    base = registry.parents[3]
    candidates = [base / "steamapps/compatdata" / shortcut["shortcut_id"] / "pfx"]
    # Explicit prefix settings take precedence; never execute a shell to expand them.
    for part in shlex.split(shortcut.get("launch_options", "")):
        if part.startswith("STEAM_COMPAT_DATA_PATH="):
            value = part.partition("=")[2].replace("$HOME", str(Path.home()))
            candidates.insert(0, Path(value).expanduser() / "pfx")
    exe = Path(shortcut.get("exe", "").strip('"'))
    for parent in exe.parents:
        if parent.name == "drive_c":
            candidates.insert(0, parent.parent)
            break
    return list(dict.fromkeys(path for path in candidates if (path / "drive_c").is_dir()))


def suggest(shortcut: dict, home: Path, entries: dict | None = None) -> dict:
    entries = entries if entries is not None else read_json(home / "catalog.json", {})
    overrides = json.loads(Path(__file__).with_name("recipes.json").read_text())
    names = [shortcut["title"]]
    recipe = next(
        (
            item
            for item in overrides
            if normalized(shortcut["title"])
            in {normalized(name) for name in item.get("aliases", [])}
        ),
        None,
    )
    if recipe:
        names.insert(0, recipe["title"])
    matches = [title for title in entries if normalized(title) in {normalized(n) for n in names}]
    if len(matches) != 1 and not recipe:
        return {"suggested_roots": [], "matched_title": None}
    title = recipe["title"] if recipe else matches[0]
    definition = entries.get(title, {})
    # Resolve aliases with a bound, and never trust a recursive definition.
    for _ in range(8):
        if not definition.get("alias"):
            break
        definition = entries.get(definition["alias"], {})
    templates = [
        path
        for path, rule in definition.get("files", {}).items()
        if isinstance(rule, dict) and "save" in rule.get("tags", [])
    ]
    if recipe:
        templates = recipe.get("paths", []) or templates
    home_path = Path.home()
    environments = [
        {
            "<home>": str(home_path),
            "<xdgData>": str(home_path / ".local/share"),
            "<xdgConfig>": str(home_path / ".config"),
        }
    ]
    for prefix in prefixes(shortcut):
        for user in (prefix / "drive_c/users").glob("*"):
            if user.name.lower() in ("public", "all users", "default") or not user.is_dir():
                continue
            environments.append(
                {
                    "_prefix": str(prefix),
                    "<home>": str(user),
                    "<winAppData>": str(user / "AppData/Roaming"),
                    "<winLocalAppData>": str(user / "AppData/Local"),
                    "<winLocalAppDataLow>": str(user / "AppData/LocalLow"),
                    "<winDocuments>": str(user / "Documents"),
                }
            )
    candidates = set()
    for template in templates:
        if not isinstance(template, str):
            continue
        for environment in environments:
            path = template
            for variable, value in environment.items():
                if variable.startswith("<"):
                    path = path.replace(variable, value)
            # Unsupported/glob/file-only rules require manual review in this alpha.
            for resolved in account_paths(path, environment.get("_prefix")):
                if any(character in resolved for character in ("<", ">", "*", "?", "[")):
                    continue
                try:
                    root = checked_root(resolved, home)
                    if root.is_dir():
                        candidates.add(str(root))
                except ValueError:
                    continue
    # Nested definitions collapse to the containing save directory.
    roots = sorted(
        path
        for path in candidates
        if not any(other != path and Path(other) in Path(path).parents for other in candidates)
    )
    return {"suggested_roots": roots, "matched_title": title}
