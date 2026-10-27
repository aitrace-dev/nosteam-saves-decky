"""Exercise the real bridge under Decky's frozen executable contract."""

import asyncio
import importlib.util
import shlex
import shutil
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    shutil.copy(root / "main.py", plugin / "main.py")
    shutil.copytree(root / "nosteam_client", plugin / "vendor/nosteam_client")
    fake_decky = types.ModuleType("decky")
    fake_decky.DECKY_USER_HOME = str(tmp_path / "user")
    monkeypatch.setitem(sys.modules, "decky", fake_decky)
    spec = importlib.util.spec_from_file_location("bridge_under_test", plugin / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_decky_does_not_reexecute_pluginloader(bridge, tmp_path, monkeypatch):
    python = sys.executable
    executable = tmp_path / "PluginLoader"
    marker = tmp_path / "decky-was-restarted"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'Decky is not Python'\n")
    executable.chmod(0o700)
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    interpreter = bin_folder / "python3"
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(python)} "$@"\n')
    interpreter.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(bridge.os, "defpath", str(bin_folder))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/decky-private-libraries")
    result = asyncio.run(bridge.Plugin().get_status())
    assert result["connected"] is False
    assert not marker.exists(), "A status poll must never restart Decky"
    launcher = Path(bridge.decky.DECKY_USER_HOME) / ".local/share/nosteam-saves/nosteam"
    assert "PluginLoader" not in launcher.read_text()
