import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def test_installer_retains_saves_wrappers_and_previous_runtime(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "installer", Path(__file__).parents[1] / "scripts/install-companion.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **kw: calls.append(command))
    source = tmp_path / "source"
    shell = source / "desktop/out/NoSteam-linux-x64"
    shell.mkdir(parents=True)
    (shell / "NoSteam").write_bytes(b"fake executable")
    home = tmp_path / "player home"
    state = home / ".local/share/nosteam-saves"
    state.mkdir(parents=True)
    (state / "pending.json").write_bytes(b"keep pending")
    saves = home / "game/saves"
    saves.mkdir(parents=True)
    (saves / "slot.sav").write_bytes(b"keep progress")
    wrapper = home / ".local/bin/nosteam"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b"keep old wrapper")
    previous = home / ".local/share/nosteam-companion/releases/previous"
    previous.mkdir(parents=True)
    (previous / "runtime").write_bytes(b"keep runtime")
    installed = installer.install(source, home, systemd=False)
    assert (installed / "app/NoSteam").read_bytes() == b"fake executable"
    assert wrapper.read_bytes() == b"keep old wrapper"
    assert (previous / "runtime").read_bytes() == b"keep runtime"
    assert (state / "pending.json").read_bytes() == b"keep pending"
    assert (saves / "slot.sav").read_bytes() == b"keep progress"
    assert len(calls) == 2
    assert str(installed / "runtime") in calls[0]
    launcher = (home / ".local/bin/nosteam-companion").read_text()
    assert "NOSTEAM_PYTHON=" in launcher
    assert '"$@"' in launcher
    desktop = (home / ".local/share/applications/nosteam-companion.desktop").read_text()
    assert f'Exec="{home}/.local/bin/nosteam-companion"' in desktop

    # Exercise the generated launcher with the Steam overlay environment that
    # crashes Chromium's zygote when the app is opened from Gaming Mode.
    executable = installed / "app/NoSteam"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'preload': os.environ.get('LD_PRELOAD'), 'args': sys.argv[1:]}))\n"
    )
    executable.chmod(0o755)
    process = subprocess.Popen(
        [str(home / ".local/bin/nosteam-companion"), "argument with spaces"],
        env={**os.environ, "LD_PRELOAD": "/fixture/gameoverlayrenderer.so"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    assert json.loads(output) == {"preload": None, "args": ["argument with spaces"]}
