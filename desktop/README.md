# NoSteam companion alpha

A standalone gaming-style desktop app and background service for non-Steam saves
and direct game transfers. Games, Saves, Transfers, Devices and Settings work
without Decky. The server stores game-transfer metadata only; game bytes travel
directly between your approved devices. Cloud save storage remains separate.

## Install on Linux x64

From this client repository, use Python 3.11+ with venv support and Node 24:

```bash
cd desktop
npm ci
npm run package:linux
cd ..
python3 scripts/install-companion.py
```

The installer creates a private Python environment, installs the client, copies the
Electron shell, adds NoSteam to the application menu and enables a systemd user
service for the next login. Dependency installation needs internet access. It
retains previous runtimes and existing `nosteam` commands because installed game
wrappers may reference them. It does not edit Steam, delete saves or stop existing
services. Re-login after replacing a running companion so the new service starts.
`--no-systemd` writes the unit without enabling startup. Linux/Bazzite installation
and controller operation still need physical-device acceptance testing.

## Use

Sign in on each device. Games shows discovered non-Steam shortcuts and local Steam
artwork. To set up saves, close Steam in Desktop Mode, choose **Set up saves**, then
select the actual save folder. Setup journals the change and retains the original
Steam launch settings. Reopen Steam afterward. Save checks, history, conflict
choices and offline upload retries use the existing save engine. Closing the app
leaves the background service running; suspension pauses work.

Saves shows each game across your devices, including applied revisions, pending
changes, conflicts and last report times. Offline or stale reports never claim a
device is currently in sync. A device reports about every 30 seconds; freshness
expires after 90 seconds. Playing or busy games remain uncertain until checked.

Game sharing starts disabled. Enable it on participating devices, then choose
**Share game folder** for a configured game on the source. Select the installation
folder rather than the entire Proton prefix. On another device choose **Download**
and a library folder. Transfers supports pause/resume, restart recovery, four
parallel verified chunks, source failover and reuse of matching blocks from prior
managed versions. Available devices with the same version can supply chunks.

Each download installs into a new `NoSteam/<game>/<version>` folder. Existing game
versions and saves are not overwritten. **Open folder** reveals the completed
installation; adding it to Steam and choosing a compatible runtime remains manual.
Known configured save folders are excluded from sharing. Review the source folder:
unknown game-specific saves inside it cannot be identified automatically. Symlinks,
special files and full Proton prefixes are rejected. Per-game manifests and paths
stay on the devices; the server receives version, title, size and availability.

The first transport is direct LAN IPv4 over TLS 1.3 with pinned device certificates
and server-signed, source/version-scoped 60-second grants. TCP port 8468 must be
reachable between devices. There is no relay fallback or automatic router setup;
unreachable sources wait with durable progress. No game bytes pass through the
backend. Source files must remain unchanged while sharing a version; re-share a
changed installation. Automatic updates and bandwidth caps are not implemented.

## Develop and verify

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q tests
cd desktop
npm ci
npm start
npm test
npm run test:desktop
```

`NOSTEAM_PYTHON` selects an absolute Python executable. In the private workspace,
`uv sync --locked` installs the client and the app recognizes the workspace venv.
`NOSTEAM_HOME` defaults to `~/.local/share/nosteam-saves`; use a disposable empty
folder for development. That directory contains pairing, bindings, pending saves,
recovery journals and backups. Do not delete it during migration.

The renderer is sandboxed, has no Node access, and uses narrow native IPC. The
service exposes a mode-0600 private Unix socket, not a TCP control endpoint.
Filesystem choices use native dialogs. Artwork comes from local Steam grid files;
no production artwork lookup or library upload occurs. Missing covers use titles.

Tests use temporary fixtures, including real pinned TLS transfers, resume after
restart, local block reuse, activation recovery, save setup rollback and a real
Electron lifecycle check. These do not claim real two-device Bazzite acceptance.
`npm run dev` and `/preview.html` provide clearly labelled example data for visual
review; the preview is excluded from production builds.
