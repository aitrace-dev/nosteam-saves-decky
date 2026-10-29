#!/usr/bin/env python3
"""Package only the public companion and its prebuilt Linux shell."""

import hashlib
import tarfile
from pathlib import Path

source = Path(__file__).resolve().parents[1]
shell = source / "desktop/out/NoSteam-linux-x64"
if not (shell / "NoSteam").is_file():
    raise SystemExit("Build first: cd desktop && npm ci && npm run package:linux")
output = source / "out/NoSteam-linux-x64.tar.gz"
output.parent.mkdir(exist_ok=True)


def public_member(info):
    if any(
        part in {"__pycache__", ".git", ".secrets", "node_modules"}
        for part in Path(info.name).parts
    ):
        return None
    if info.name.endswith((".pyc", ".DS_Store")):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


with tarfile.open(output, "w:gz", compresslevel=6) as archive:
    for name in [
        "nosteam_client",
        "pyproject.toml",
        "LICENSE",
        "scripts/install-companion.py",
        "desktop/out/NoSteam-linux-x64",
    ]:
        archive.add(source / name, arcname="NoSteam/" + name, filter=public_member)
    archive.add(source / "desktop/INSTALL.md", arcname="NoSteam/INSTALL.md", filter=public_member)
checksum = hashlib.file_digest(output.open("rb"), "sha256").hexdigest()
output.with_name(output.name + ".sha256").write_text(checksum + "  " + output.name + "\n")
print(f"Packaged {output.name}: {output.stat().st_size:,} bytes")
