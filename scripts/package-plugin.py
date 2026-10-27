"""Build an installable Decky ZIP. Agent is installed separately in this alpha."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
output = root / "out" / "nosteam-saves-0.1.0.zip"
output.parent.mkdir(exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for name in ("plugin.json", "package.json", "main.py", "dist/index.js"):
        archive.write(root / name, f"nosteam-saves/{name}")
    for name in ("LICENSE", "THIRD_PARTY.md"):
        archive.write(root / name, f"nosteam-saves/{name}")
print(output)
