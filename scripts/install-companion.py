#!/usr/bin/env python3
"""Install a built Linux companion without deleting saves or old launch runtimes."""

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    with temporary.open("x") as stream:
        os.chmod(temporary, mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def install(source, home, systemd=True):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise SystemExit("This build installs on Linux x64 only.")
    shell = source / "desktop/out/NoSteam-linux-x64"
    if not (shell / "NoSteam").is_file():
        raise SystemExit(
            "Build the desktop shell first: cd desktop && npm ci && npm run package:linux"
        )
    # Never move a venv: generated entry points embed its absolute path.
    release = home / ".local/share/nosteam-companion/releases" / uuid.uuid4().hex
    release.mkdir(parents=True, mode=0o700)
    runtime = release / "runtime"
    subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
    subprocess.run([str(runtime / "bin/python"), "-m", "pip", "install", str(source)], check=True)
    shutil.copytree(shell, release / "app", symlinks=True)
    launcher = home / ".local/bin/nosteam-companion"
    write(
        launcher,
        "#!/bin/sh\n"
        + f"export NOSTEAM_PYTHON={shlex.quote(str(runtime / 'bin/python'))}\n"
        # Steam injects game-overlay libraries that crash Chromium zygotes.
        + "unset LD_PRELOAD\n"
        + f'exec {shlex.quote(str(release / "app/NoSteam"))} "$@"\n',
        0o755,
    )

    # Quote Desktop Entry Exec arguments, which do not use shell escaping.
    def desktop_quote(value):
        return (
            '"'
            + str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
            .replace("%", "%%")
            + '"'
        )

    write(
        home / ".local/share/applications/nosteam-companion.desktop",
        "[Desktop Entry]\nType=Application\nName=NoSteam\n"
        "Comment=Game and save sync across your devices\n"
        + f"Exec={desktop_quote(launcher)}\nTerminal=false\nCategories=Game;Utility;\n",
        0o644,
    )

    def unit_quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    write(
        home / ".config/systemd/user/nosteam-companion.service",
        "[Unit]\nDescription=NoSteam background sync\nAfter=network-online.target\n\n[Service]\n"
        + f"ExecStart={unit_quote(runtime / 'bin/nosteam-service')}\n"
        + "Restart=on-failure\nRestartSec=5\nUMask=0077\n\n[Install]\nWantedBy=default.target\n",
    )
    if systemd:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "nosteam-companion.service"], check=True)
    print("Installed NoSteam. Open it from your application menu.")
    print("Existing services and runtimes were retained.")
    print(
        "Existing save files, pending uploads, backups and Steam launch settings were not modified."
    )
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-systemd", action="store_true", help="Write the unit without enabling it"
    )
    args = parser.parse_args()
    os.umask(0o077)
    install(Path(__file__).resolve().parents[1], Path.home(), not args.no_systemd)


if __name__ == "__main__":
    main()
