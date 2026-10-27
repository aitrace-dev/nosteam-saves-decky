"""Build a self-contained ZIP with a pure-Python agent and its runtime dependencies."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
version = json.loads((root / "package.json").read_text())["version"]
output = root / "out" / f"nosteam-saves-{version}.zip"
output.parent.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as directory:
    vendor = Path(directory) / "vendor"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--target",
            str(vendor),
            "httpx==0.28.1",
            "vdf==3.4",
            "PyYAML==6.0.3",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.copytree(
        root / "nosteam_client",
        vendor / "nosteam_client",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in (
            "plugin.json",
            "package.json",
            "main.py",
            "dist/index.js",
            "LICENSE",
            "THIRD_PARTY.md",
            "README.md",
        ):
            archive.write(root / name, f"nosteam-saves/{name}")
        for path in sorted(vendor.rglob("*")):
            # PyYAML has a pure-Python fallback. Never ship host-specific native modules.
            if path.is_file() and path.suffix not in (".so", ".dylib", ".pyd", ".pyc"):
                archive.write(path, "nosteam-saves/vendor/" + path.relative_to(vendor).as_posix())
print(output)
