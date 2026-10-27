"""Decky adapter. Filesystem operations run in the bundled model-free agent."""

import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import decky


class Plugin:
    async def _command(self, arguments: list[str], secret: str | None = None):
        home = Path(decky.DECKY_USER_HOME)
        state = home / ".local/share/nosteam-saves"
        # Decky is a frozen executable: sys.executable is PluginLoader, not Python.
        frozen = getattr(sys, "frozen", False)
        python = shutil.which("python3", path=os.defpath) if frozen else sys.executable
        if not python:
            raise RuntimeError("NoSteam Saves needs system Python 3.11 or newer.")
        vendor = Path(__file__).parent / "vendor"
        if not (vendor / "nosteam_client/cli.py").is_file():
            raise RuntimeError(
                "Install the complete NoSteam Saves ZIP, including its bundled agent."
            )
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        executable = state / "nosteam"
        fallback = (
            "#!/bin/sh\nif [ ! -f "
            + shlex.quote(str(vendor / "nosteam_client/cli.py"))
            + ' ]; then\n  if [ "$1" = launch ] && [ "$3" = -- ]; then\n'
            + '    shift 3\n    exec "$@"\n  fi\n  exit 2\nfi\n'
            + 'export NOSTEAM_ORIGINAL_PYTHONPATH="${PYTHONPATH-}"\n'
        )
        body = (
            fallback
            + "\n".join(
                "export " + key + "=" + shlex.quote(value)
                for key, value in {
                    "HOME": str(home),
                    "NOSTEAM_HOME": str(state),
                    "NOSTEAM_EXECUTABLE": str(executable),
                    "PYTHONPATH": str(vendor),
                    "NOSTEAM_BUNDLED_PATH": str(vendor),
                }.items()
            )
            + "\nexec "
            + shlex.quote(python)
            + ' -m nosteam_client.cli "$@"\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
        try:
            temporary.chmod(0o700)
            os.replace(temporary, executable)
        finally:
            temporary.unlink(missing_ok=True)
        environment = {**os.environ, "HOME": str(home), "NOSTEAM_HOME": str(state)}
        if frozen:
            # PyInstaller's private libraries must not leak into the system interpreter.
            original_libraries = environment.pop("LD_LIBRARY_PATH_ORIG", None)
            environment.pop("LD_LIBRARY_PATH", None)
            environment.pop("PYTHONHOME", None)
            if original_libraries is not None:
                environment["LD_LIBRARY_PATH"] = original_libraries
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *arguments,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate((secret + "\n").encode() if secret is not None else None),
                timeout=240 if arguments[0] == "auto-plan" else 60,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("Sync timed out. Local saves are safe.") from None
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise RuntimeError(
                "The save agent could not start. Check the plugin installation."
            ) from None
        if process.returncode:
            raise RuntimeError(result.get("error", "NoSteam Saves operation failed"))
        return result

    async def get_status(self):
        return await self._command(["status"])

    async def discover(self):
        return await self._command(["discover"])

    async def sign_in(self):
        return await self._command(["sign-in"])

    async def poll_sign_in(self):
        return await self._command(["poll-sign-in"])

    async def automatic_plan(self):
        return await self._command(["auto-plan"])

    async def connect(self, server: str, token: str):
        return await self._command(["connect", server, "--stdin"], token)

    async def cloud_games(self):
        return await self._command(["cloud-games"])

    async def update_definitions(self):
        return await self._command(["update-definitions"])

    async def find_saves(self, shortcut_id: str, registry: str):
        return await self._command(["find-saves", shortcut_id, "--registry", registry])

    async def enable(
        self,
        shortcut_id: str,
        registry: str,
        game_id: str,
        save_paths: list[str],
        original_options: str,
    ):
        arguments = [
            "enable",
            shortcut_id,
            "--registry",
            registry,
            "--game",
            game_id,
            f"--original-options={original_options}",
        ]
        for path in save_paths:
            arguments.extend(["--save", path])
        return await self._command(arguments)

    async def cancel_enable(self, game_id: str):
        return await self._command(["disable", game_id, "--retry"])

    async def disable(self, game_id: str):
        return await self._command(["disable", game_id])

    async def sync(self, game_id: str):
        return await self._command(["sync", game_id])

    async def resolve(self, game_id: str, choice: str):
        return await self._command(["resolve", game_id, choice])

    async def history(self, game_id: str):
        return await self._command(["history", game_id])

    async def rollback(self, game_id: str, revision: str):
        return await self._command(["rollback", game_id, revision])

    async def _main(self):
        async def retry_loop():
            while True:
                try:
                    await self._command(["retry"])
                except Exception:
                    pass  # Status is available in the panel; never log credentials or paths.
                await asyncio.sleep(30)

        self.retry_task = asyncio.create_task(retry_loop())
        decky.logger.info("NoSteam Saves loaded")

    async def _unload(self):
        task = getattr(self, "retry_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
