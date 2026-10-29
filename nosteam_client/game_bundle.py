"""Portable installation bundles: game, prefix, exact runtime and launch dependencies."""

import hashlib
import os
import platform
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

import vdf

from . import game_files as files
from .game_files import GameFileError

FIELDS = {"ishidden", "allowdesktopconfig", "allowoverlay", "openvr", "devkit", "tags"}


def folded(value):
    return {key.lower(): item for key, item in value.items()}


def section(value, key):
    if isinstance(value, dict):
        for name, item in value.items():
            if name.lower() == key.lower():
                return item
            result = section(item, key)
            if result is not None:
                return result
    return None


def load_vdf(path):
    if not path.exists():
        return {}
    with path.open() as stream:
        return vdf.load(stream)


def tool_path(steam, name):
    direct = steam / "compatibilitytools.d" / name
    candidates = [direct] if direct.is_dir() else []
    for descriptor in (steam / "compatibilitytools.d").glob("*/compatibilitytool.vdf"):
        tools = section(load_vdf(descriptor), "compat_tools") or {}
        if name in tools:
            candidates.append(descriptor.parent / tools[name].get("install_path", "."))
    aliases = {"proton_experimental": "Proton - Experimental", "proton_hotfix": "Proton Hotfix"}
    if name in aliases:
        candidates.append(steam / "steamapps/common" / aliases[name])
    match = re.fullmatch(r"proton_?(\d)(\d)?", name)
    if match:
        major = match[1] if match[2] in {None, "0"} else match[1] + match[2]
        candidates.append(steam / "steamapps/common" / f"Proton {major}.0")
    found = {path.resolve() for path in candidates if (path / "proton").is_file()}
    if len(found) != 1:
        raise GameFileError("The selected Proton version is missing or ambiguous on the source")
    return found.pop()


def current_options(value):
    lexer = shlex.shlex(value, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    words = list(lexer)
    for index in range(len(words) - 3, -1, -1):
        if (
            Path(words[index]).name == "nosteam"
            and words[index + 1] == "launch"
            and words[index + 3] == "--"
        ):
            del words[index : index + 4]
    result = []
    for word in words:
        if word in {";", "&&", "||", "|", ">", "<", ">>", "&"}:
            raise GameFileError("Move shell operators into a launcher script before sharing")
        if word == "%command%":
            result.append(word)
        elif "=" in word and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", word.split("=", 1)[0]):
            key, data = word.split("=", 1)
            result.append(key + "=" + shlex.quote(data))
        else:
            result.append(shlex.quote(word))
    return " ".join(result)


def installation_root(shortcut):
    executable = Path(shortcut["exe"].strip('"')).expanduser().resolve()
    if not executable.is_absolute() or not executable.is_file():
        raise GameFileError("The Steam shortcut executable is not available on this device")
    root = executable.parent
    with Path(shortcut["registry"]).open("rb") as stream:
        rows = vdf.binary_load(stream)["shortcuts"]
    for row in rows.values():
        entry = folded(row)
        if int(entry.get("appid", 0)) & 0xFFFFFFFF != int(shortcut["shortcut_id"]):
            continue
        working = entry.get("startdir", "").strip('"')
        if working and Path(working).is_absolute():
            candidate = Path(working).expanduser().resolve()
            if executable.is_relative_to(candidate):
                root = candidate
        break
    # A launcher in a generic folder does not authorize sharing its neighbours.
    if root == Path.home().resolve() or root.name.casefold() in {
        "desktop",
        "downloads",
        "documents",
        "bin",
        "sbin",
        "applications",
        "games",
        "steam",
        "steamapps",
        "common",
        "compatdata",
        "compatibilitytools.d",
        ".local",
        "share",
    }:
        raise GameFileError(
            "This shortcut points to a generic launcher folder. Set its executable and working "
            "directory to the actual game installation in Steam."
        )
    return root


def describe(root, shortcut, binding):
    root = Path(root).resolve()
    registry = Path(shortcut["registry"])
    with registry.open("rb") as stream:
        rows = vdf.binary_load(stream)["shortcuts"]
    entry = next(
        folded(row)
        for row in rows.values()
        if int(folded(row).get("appid", 0)) & 0xFFFFFFFF == int(shortcut["shortcut_id"])
    )
    steam = registry.parents[3]
    mounts = {"game": str(root)}
    paths = {str(root): "game"}
    executable = Path(entry.get("exe", shortcut["exe"]).strip('"')).expanduser().resolve()
    if not executable.is_relative_to(root) or not executable.is_file():
        raise GameFileError("Select the installation folder containing this shortcut's executable")
    mapping = section(load_vdf(steam / "config/config.vdf"), "CompatToolMapping") or {}
    compatibility = mapping.get(shortcut["shortcut_id"], mapping.get("0", {}))
    name = compatibility.get("name", "")
    if executable.suffix.casefold() == ".exe" and not name:
        raise GameFileError("Select a Proton version in Steam before sharing this Windows game")
    options = current_options(entry.get("launchoptions", shortcut["launch_options"]))
    words = shlex.split(options)
    assignments = dict(
        word.split("=", 1) for word in words if "=" in word and word.split("=", 1)[0].isidentifier()
    )
    if name:
        runtime = tool_path(steam, name)
        mounts["runtime"] = str(runtime)
        paths[str(runtime)] = "runtime"
        prefix = (
            Path(assignments["STEAM_COMPAT_DATA_PATH"]).expanduser()
            if "STEAM_COMPAT_DATA_PATH" in assignments
            else steam / "steamapps/compatdata" / shortcut["shortcut_id"]
        )
        mounts["prefix"] = str(prefix.resolve()) if prefix.exists() else None
        paths[str(prefix.resolve())] = "prefix"
    # These constructs need a real shell parser; never silently change their meaning.
    if any(word in {";", "&&", "||", "|", ">", "<"} for word in words):
        raise GameFileError("Move shell pipelines into a launcher script before sharing")
    commands = {}
    for word in words[: words.index("%command%") if "%command%" in words else len(words)]:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", word):
            executable_command = shutil.which(word)
            if executable_command:
                commands[word] = executable_command
    needed = []
    helpers = []
    queue = [
        word
        for word in words
        if word.startswith("/") and not re.fullmatch(r"/[A-Za-z][A-Za-z0-9_-]*:.*", word)
    ] + list(commands.values())
    visited = set()
    while queue:
        original = queue.pop(0)
        if original in visited:
            continue
        visited.add(original)
        path = Path(original).expanduser()
        if any(part in {".ssh", ".gnupg", ".secrets"} for part in path.parts):
            raise GameFileError("Launch dependencies cannot include private credential folders")
        if any(
            path.resolve().is_relative_to(Path(value))
            for key, value in mounts.items()
            if value and key in {"game", "prefix", "runtime"}
        ):
            continue
        if not path.is_file() or not path.is_relative_to(Path.home()):
            needed.append(original)
            continue
        if len(helpers) >= 32 or path.stat().st_size > 64 * 1024 * 1024:
            raise GameFileError("Too many or oversized external launch dependencies")
        virtual = f"helpers/{len(helpers)}"
        actual = path.resolve()
        mounts[virtual] = str(actual.parent)
        paths[original] = virtual + "/" + actual.name
        paths[str(actual)] = virtual + "/" + actual.name
        helpers.append(virtual + "/" + actual.name)
        # Only referenced files are copied, never an entire home/Documents tree.
        if path.stat().st_size < 512 * 1024:
            try:
                text = path.read_text()
            except UnicodeError:
                continue
            queue.extend(re.findall(r"""["'](/[^\n"']+)["']""", text))
    for source, target in list(paths.items()):
        try:
            alias = str(Path.home()) + "/" + str(Path(source).relative_to(Path.home().resolve()))
            paths[alias] = target
        except ValueError:
            pass
    dependency_files = []
    if mounts.get("prefix"):
        for parent, names, file_names in os.walk(mounts["prefix"], followlinks=False):
            for filename in names + file_names:
                path = Path(parent) / filename
                if not path.is_symlink():
                    continue
                resolved = path.resolve()
                virtual = locate(resolved, mounts)
                if virtual and virtual.startswith("dependencies/"):
                    dependency_files.append(virtual)
                    continue
                if virtual is not None:
                    continue
                copied_dependencies = root.parent / "dependencies"
                if (
                    mounts.get("runtime") == str(root.parent / "runtime")
                    and resolved.is_relative_to(copied_dependencies)
                    and len(resolved.relative_to(copied_dependencies).parts) >= 2
                ):
                    key = "dependencies/" + resolved.relative_to(copied_dependencies).parts[0]
                    mounts[key] = str(root.parent / key)
                    paths[str(root.parent / key)] = key
                    dependency_files.append(
                        key + "/" + resolved.relative_to(root.parent / key).as_posix()
                    )
                    continue
                for ancestor in resolved.parents:
                    if (ancestor / "proton").is_file() and ancestor.is_relative_to(steam.resolve()):
                        key = "dependencies/" + str(
                            len([key for key in mounts if key.startswith("dependencies/")])
                        )
                        mounts[key] = str(ancestor)
                        paths[str(ancestor)] = key
                        dependency_files.append(
                            key + "/" + resolved.relative_to(ancestor).as_posix()
                        )
                        break
    artwork = []
    grid = registry.parent / "grid"
    if grid.is_dir():
        for image in sorted(grid.iterdir()):
            if (
                re.fullmatch(
                    re.escape(shortcut["shortcut_id"])
                    + r"(?:p|_hero|_logo|_icon)?\.(?:png|jpg|jpeg|webp)",
                    image.name,
                    re.IGNORECASE,
                )
                and image.is_file()
                and not image.is_symlink()
            ):
                if image.stat().st_size > 16 * 1024 * 1024:
                    raise GameFileError("Steam artwork is too large to transfer")
                artwork.append("artwork/" + image.name)
        if artwork:
            mounts["artwork"] = str(grid.resolve())
    saves = []
    for value in binding.get("roots", []):
        original = Path(value).resolve()
        matched = locate(original, mounts)
        if matched is None:
            raise GameFileError(
                "This game uses external save folders that cannot yet be relocated automatically"
            )
        saves.append(matched)
    start = entry.get("startdir", "").strip('"')
    cwd = locate(Path(start), mounts) if start else "game"
    if cwd is None:
        raise GameFileError("The shortcut working directory is outside this installation")
    return mounts, {
        "title": entry.get("appname", shortcut["title"]),
        "appid": int(shortcut["shortcut_id"]),
        "exe": "game/" + executable.relative_to(root).as_posix(),
        "cwd": cwd,
        "options": options,
        "paths": paths,
        "source_home": str(Path.home()),
        "helpers": helpers,
        "artwork": artwork,
        "dependency_files": sorted(set(dependency_files)),
        "commands": commands,
        "platform": {"os": platform.system(), "arch": platform.machine()},
        "requires": sorted(set(needed)),
        "proton": name,
        "compatibility": {
            "config": str(compatibility.get("config", "")),
            "priority": str(compatibility.get("priority", "250")),
        },
        "shortcut": {key: value for key, value in entry.items() if key in FIELDS},
        "saves": saves,
    }


def locate(path, mounts):
    # Lexical normalization avoids following Wine's Z: link into the host filesystem.
    path = Path(os.path.realpath(path))
    for virtual, source in sorted(
        mounts.items(), key=lambda pair: len(pair[1] or ""), reverse=True
    ):
        if source and path.is_relative_to(Path(source)):
            suffix = path.relative_to(source).as_posix()
            return virtual if suffix == "." else virtual + "/" + suffix
    return None


def external(path, target):
    if re.fullmatch(r"prefix/pfx/dosdevices/[a-z]:{1,2}", path):
        if target == "/" and path.endswith("/z:"):
            return True
        return bool(re.fullmatch(r"/dev/[a-zA-Z0-9_-]+|/run/media/[^\x00\n]+", target))
    return bool(
        re.fullmatch(r"prefix/pfx/dosdevices/(?:com[1-9][0-9]?|lpt[1-9])", path)
        and re.fullmatch(r"/dev/(?:ttyS[0-9]+|lp[0-9]+)", target)
    )


def inventory(mounts, helper_files=()):
    def failed(error):
        raise error

    regular, directories, links = {}, set(), []
    for virtual, source in mounts.items():
        directories.add(virtual)
        if source is None:
            continue
        source = Path(source)
        for parent, names, names_files in os.walk(source, followlinks=False, onerror=failed):
            names.sort()
            for name in names + sorted(names_files):
                path = Path(parent) / name
                relative = path.relative_to(source).as_posix()
                target = virtual + "/" + relative
                if (
                    virtual == "artwork" or virtual.startswith(("helpers/", "dependencies/"))
                ) and target not in helper_files:
                    if path.is_dir() and any(
                        item.startswith(target + "/") for item in helper_files
                    ):
                        directories.add(target)
                    else:
                        if name in names:
                            names.remove(name)
                        continue
                if name in {".ssh", ".gnupg", ".secrets", ".env", ".existing-env"}:
                    raise GameFileError("The selected installation includes private configuration")
                if len(regular) + len(directories) + len(links) > 250000:
                    raise GameFileError("Too many installation files")
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    dest = locate(path.parent / os.readlink(path), mounts)
                    if dest is not None:
                        links.append({"path": target, "target": dest, "external": False})
                    elif external(target, os.readlink(path)):
                        links.append(
                            {"path": target, "target": os.readlink(path), "external": True}
                        )
                    else:
                        raise GameFileError(
                            "A symlink points outside the bundled game, prefix or Proton runtime"
                        )
                elif stat.S_ISDIR(info.st_mode):
                    directories.add(target)
                elif stat.S_ISREG(info.st_mode):
                    regular[target] = (
                        path,
                        (
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                            info.st_mode,
                        ),
                    )
                else:
                    raise GameFileError(
                        "An installation contains a special file that cannot be transferred"
                    )
    return regular, sorted(directories), sorted(links, key=lambda link: link["path"])


def build(
    root, game, shortcut, binding, cancelled=lambda: False, progress=lambda done, total: None
):
    mounts, launch = describe(root, shortcut, binding)
    helper_files = set(launch["helpers"] + launch["dependency_files"] + launch["artwork"])
    before, directories, links = inventory(mounts, helper_files)
    total = sum(info[1][2] for info in before.values())
    if total > files.MAX_GAME:
        raise GameFileError("Installation exceeds the transfer size limit")
    progress(0, total)
    done = 0
    entries = []
    for name, (path, info) in sorted(before.items()):
        hashes = []
        virtual = next(
            key for key in sorted(mounts, key=len, reverse=True) if name.startswith(key + "/")
        )
        with files.open_file(Path(mounts[virtual]), name[len(virtual) + 1 :]) as stream:
            while block := stream.read(files.BLOCK_SIZE):
                if cancelled():
                    raise InterruptedError("Installation indexing paused")
                hashes.append(hashlib.sha256(block).hexdigest())
                done += len(block)
                progress(done, total)
        entries.append(
            {
                "path": name,
                "size": info[2],
                "executable": bool(info[5] & 0o111),
                "hashes": hashes,
                "mtime_ns": info[3],
                "mode": info[5] & 0o777,
            }
        )
    if (before, directories, links) != inventory(mounts, helper_files):
        raise GameFileError(
            "Game, Proton or prefix changed during indexing. Close the game and retry."
        )
    manifest = {
        "schema": 2,
        "game": game,
        "block_size": files.BLOCK_SIZE,
        "files": entries,
        "directories": directories,
        "links": links,
        "launch": launch,
    }
    files.validate(manifest)
    if describe(root, shortcut, binding) != (mounts, launch):
        raise GameFileError("The Steam or Proton configuration changed during indexing")
    return manifest, mounts


def validate_launch(launch):
    expected = {
        "title",
        "appid",
        "exe",
        "cwd",
        "options",
        "paths",
        "source_home",
        "helpers",
        "artwork",
        "requires",
        "proton",
        "compatibility",
        "shortcut",
        "saves",
        "dependency_files",
        "commands",
        "platform",
    }
    if not isinstance(launch, dict) or set(launch) != expected:
        raise GameFileError("Invalid installation launch configuration")
    for key in ("title", "options", "source_home", "proton"):
        if not isinstance(launch[key], str) or len(launch[key]) > 16384 or "\0" in launch[key]:
            raise GameFileError("Invalid installation launch setting")
    if not Path(launch["source_home"]).is_absolute():
        raise GameFileError("Invalid source home directory")
    if type(launch["appid"]) is not int or not 0x80000000 <= launch["appid"] <= 0xFFFFFFFF:
        raise GameFileError("Invalid non-Steam shortcut identity")
    for key in ("exe", "cwd"):
        files.relative(launch[key])
    if not isinstance(launch["paths"], dict) or len(launch["paths"]) > 128:
        raise GameFileError("Too many launch path mappings")
    for key, value in launch["paths"].items():
        if (
            not isinstance(key, str)
            or not key.startswith("/")
            or key == "/"
            or len(key) > 4096
            or "\0" in key
        ):
            raise GameFileError("Invalid source path mapping")
        files.relative(value)
    if (
        not isinstance(launch["platform"], dict)
        or set(launch["platform"]) != {"os", "arch"}
        or any(
            not isinstance(value, str) or len(value) > 100 for value in launch["platform"].values()
        )
    ):
        raise GameFileError("Invalid installation platform")
    if not isinstance(launch["commands"], dict) or len(launch["commands"]) > 32:
        raise GameFileError("Invalid launch command mappings")
    for key, value in launch["commands"].items():
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key)
            or not isinstance(value, str)
            or not value.startswith("/")
            or "\0" in value
        ):
            raise GameFileError("Invalid mapped launch command")
    for key in ("helpers", "saves", "requires", "dependency_files", "artwork"):
        if not isinstance(launch[key], list) or len(launch[key]) > 128:
            raise GameFileError("Invalid installation dependencies")
        for value in launch[key]:
            if key == "requires":
                if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
                    raise GameFileError("Invalid external dependency")
            else:
                files.relative(value)
    for value in launch["artwork"]:
        if not re.fullmatch(
            r"artwork/" + str(launch["appid"]) + r"(?:p|_hero|_logo|_icon)?\.(?:png|jpg|jpeg|webp)",
            value,
            re.IGNORECASE,
        ):
            raise GameFileError("Invalid Steam artwork filename")
    if not isinstance(launch["shortcut"], dict) or set(launch["shortcut"]) - FIELDS:
        raise GameFileError("Invalid Steam shortcut settings")
    for key, value in launch["shortcut"].items():
        if key == "tags":
            if (
                not isinstance(value, dict)
                or len(value) > 100
                or any(
                    not isinstance(k, str) or not isinstance(v, str) or len(v) > 256
                    for k, v in value.items()
                )
            ):
                raise GameFileError("Invalid game tags")
        elif type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise GameFileError("Invalid shortcut flag")
    if not isinstance(launch["compatibility"], dict) or set(launch["compatibility"]) != {
        "config",
        "priority",
    }:
        raise GameFileError("Invalid compatibility settings")
    if any(
        not isinstance(v, str) or len(v) > 4096 or "\0" in v
        for v in launch["compatibility"].values()
    ):
        raise GameFileError("Invalid compatibility value")


def materialize(root, manifest):
    validate_launch(manifest["launch"])
    for link in manifest["links"]:
        path = root / link["path"]
        descriptor = files.open_directory(root, str(Path(link["path"]).parent), create=True)
        try:
            target = (
                link["target"]
                if link["external"]
                else os.path.relpath(root / link["target"], path.parent)
            )
            if path.is_symlink() and os.readlink(path) == target:
                continue
            os.symlink(target, path.name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def validate_bundle(manifest, expected=None):
    if set(manifest) != {"schema", "game", "block_size", "files", "directories", "links", "launch"}:
        raise GameFileError("Invalid installation bundle")
    base = {key: value for key, value in manifest.items() if key not in {"links", "launch"}}
    base["schema"] = 1
    if not isinstance(base["files"], list) or len(base["files"]) > 250000:
        raise GameFileError("Invalid installation file list")
    normalized = []
    for entry in base["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "size", "executable", "hashes", "mtime_ns", "mode"}
            or type(entry["mtime_ns"]) is not int
            or not -(2**63) < entry["mtime_ns"] < 2**63
            or type(entry["mode"]) is not int
            or not 0 <= entry["mode"] <= 0o777
        ):
            raise GameFileError("Invalid installation file metadata")
        normalized.append(
            {key: value for key, value in entry.items() if key not in {"mtime_ns", "mode"}}
        )
    normalized_base = {**base, "files": normalized}
    validate_launch(manifest["launch"])
    sensitive = manifest["launch"]["platform"]["os"] == "Linux"
    total = files.validate(normalized_base, case_sensitive=sensitive)
    links = manifest["links"]
    if (
        not isinstance(links, list)
        or len(links) + len(base["files"]) + len(base["directories"]) > 250000
    ):
        raise GameFileError("Too many installation links")
    paths = {entry["path"] for entry in base["files"]} | set(base["directories"])
    path_key = (lambda name: name) if sensitive else (lambda name: name.casefold())
    folded_paths = {path_key(name) for name in paths}
    link_paths = set()
    for link in links:
        if not isinstance(link, dict) or set(link) != {"path", "target", "external"}:
            raise GameFileError("Invalid installation symlink")
        files.relative(link["path"])
        if type(link["external"]) is not bool or not isinstance(link["target"], str):
            raise GameFileError("Invalid installation link target")
        if link["external"]:
            if not external(link["path"], link["target"]):
                raise GameFileError(
                    "External symlinks are restricted to Wine drive/device mappings"
                )
        else:
            files.relative(link["target"])
        if path_key(link["path"]) in folded_paths:
            raise GameFileError("Conflicting installation paths")
        folded_paths.add(path_key(link["path"]))
        paths.add(link["path"])
        link_paths.add(link["path"])
    for path in paths:
        if Path(path).parts[0] not in {
            "game",
            "prefix",
            "runtime",
            "helpers",
            "artwork",
            "dependencies",
        }:
            raise GameFileError("Unknown installation bundle namespace")
        if any(str(parent) in link_paths for parent in Path(path).parents):
            raise GameFileError("Installation entries cannot be written through a symlink")
    for link in links:
        if not link["external"] and any(
            str(parent) in link_paths
            for parent in [Path(link["target"]), *Path(link["target"]).parents]
        ):
            raise GameFileError(
                "Installation link chains must resolve to a bundled file or directory"
            )
    for name in [
        manifest["launch"]["exe"],
        manifest["launch"]["cwd"],
        *manifest["launch"]["helpers"],
        *manifest["launch"]["saves"],
    ]:
        if str(Path(name)) in link_paths or any(
            str(parent) in link_paths for parent in Path(name).parents
        ):
            raise GameFileError("Launch and save paths cannot traverse a bundled symlink")
    if manifest["launch"]["proton"] and "runtime/proton" not in {
        entry["path"] for entry in base["files"]
    }:
        raise GameFileError("The selected Proton runtime is absent from the bundle")
    for path in [
        manifest["launch"]["exe"],
        *manifest["launch"]["helpers"],
        *manifest["launch"]["artwork"],
    ]:
        if path not in {entry["path"] for entry in base["files"]}:
            raise GameFileError("Launch executable or helper is absent from the bundle")
    if len(files.canonical(manifest)) > files.MAX_MANIFEST:
        raise GameFileError("Installation bundle exceeds metadata limits")
    if expected and files.version_id(manifest) != expected:
        raise GameFileError("The source bundle does not match this installation version")
    return total


def check_filesystem(root, manifest):
    if manifest["schema"] != 2 or manifest["launch"]["platform"]["os"] != "Linux":
        return
    try:
        with tempfile.TemporaryDirectory(prefix=".nosteam-fs-", dir=root) as folder:
            probe = Path(folder)
            for first, second in [("CaseProbe", "caseprobe"), ("caf\u00e9", "cafe\u0301")]:
                (probe / first).touch()
                if (probe / second).exists():
                    raise GameFileError(
                        "Choose a case-sensitive Linux library for this installation"
                    )
            if manifest["launch"]["proton"] or manifest["links"]:
                (probe / "c:").symlink_to("CaseProbe")
                if not (probe / "c:").is_symlink():
                    raise OSError("Symlinks unavailable")
    except OSError as error:
        raise GameFileError("Choose a Linux library with support for Wine symlinks") from error
