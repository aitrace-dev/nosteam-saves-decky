"""Retire a recognized Decky launcher without changing Steam settings or save files."""

import argparse
import os
import shlex
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

from .config import configuration, lock, state_home
from .game_files import GameFileError
from .game_setup import write_registry
from .storage import read_json


def migrate(home, python):
    python = Path(python).absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise GameFileError("The installed companion Python runtime is unavailable")
    # Verify isolated imports before replacing a launcher used by Steam.
    subprocess.run(
        [str(python), "-I", "-c", "import nosteam_client.cli"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    legacy = home / "nosteam"
    disabled = read_json(Path.home() / "homebrew/settings/loader.json", {})
    if "NoSteam Saves" not in disabled.get("disabled_plugins", []):
        raise GameFileError("Disable NoSteam Saves in Decky before migrating its launcher")
    with ExitStack() as held:
        held.enter_context(lock(home / "launcher-migration.lock", blocking=False))
        held.enter_context(lock(home / "config.lock", blocking=False))
        cfg = configuration(home)
        for game in sorted(cfg["games"]):
            held.enter_context(lock(home / "games" / game / "session.lock", blocking=False))
        if legacy.is_symlink() or not legacy.is_file():
            raise GameFileError("No recognized legacy launcher was found")
        original = legacy.read_bytes()
        vendor = str(Path.home() / "homebrew/plugins/nosteam-saves/vendor")
        if vendor.encode() not in original or b"NOSTEAM_BUNDLED_PATH" not in original:
            raise GameFileError("The existing launcher is not the legacy Decky launcher")
        backup = home / "launcher-backups" / f"{uuid.uuid4()}.sh"
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with backup.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        executable = shlex.quote(str(python))
        updated = (
            "#!/bin/sh\n"
            f"export NOSTEAM_HOME={shlex.quote(str(home))}\n"
            f"export NOSTEAM_EXECUTABLE={shlex.quote(str(legacy))}\n"
            f"if [ ! -x {executable} ]; then\n"
            '  if [ "$1" = launch ] && [ "$3" = -- ]; then shift 3; exec "$@"; fi\n'
            "  printf '%s\\n' 'NoSteam runtime unavailable. Reinstall the companion.' >&2\n"
            "  exit 2\nfi\n"
            f'exec {executable} -I -m nosteam_client.cli "$@"\n'
        ).encode()
        # The executable bit is required before the atomic replacement becomes visible.
        write_registry(legacy, updated, mode=0o755)
    return {"status": "migrated", "steam_settings_changed": False, "save_files_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(migrate(state_home(), args.python)["status"])


if __name__ == "__main__":
    main()
