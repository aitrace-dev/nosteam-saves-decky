"""Decky adapter. Filesystem operations run in the separately installed user agent."""

import asyncio
import json
import os
from pathlib import Path

import decky


class Plugin:
    async def _command(self, arguments: list[str], secret: str | None = None):
        home = Path(decky.DECKY_USER_HOME)
        executable = home / ".local/share/nosteam-saves/venv/bin/nosteam"
        if not executable.is_file():
            raise RuntimeError("Install the NoSteam Saves agent in Desktop Mode first.")
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *arguments,
            env={
                **os.environ,
                "HOME": str(home),
                "NOSTEAM_HOME": str(home / ".local/share/nosteam-saves"),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate((secret + "\n").encode() if secret is not None else None),
                timeout=60,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("Sync timed out. Local saves are safe.") from None
        result = json.loads(stdout)
        if process.returncode:
            raise RuntimeError(result.get("error", "NoSteam Saves operation failed"))
        return result

    async def get_status(self):
        return await self._command(["status"])

    async def discover(self):
        return await self._command(["discover"])

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
        decky.logger.info("NoSteam Saves loaded")

    async def _unload(self):
        pass
