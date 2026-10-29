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
`--no-systemd` writes the unit without enabling startup. Installation, startup, account reporting and all five packaged app screens have
been exercised on Bazzite. Launching the app through a Steam shortcut has also been exercised; the installed
launcher clears overlay preloads that otherwise crash Chromium zygotes. Physical
controller navigation and real-game handoffs still need acceptance testing.

## Controller interface

The app uses a fixed navigation rail and controller hint bar, with one scrolling
content area. Games is split into This device, Other devices and Installed rather
than a long dashboard. Each game is one focusable tile; A opens its actions and B
returns to the same tile. D-pad/left stick moves focus, LB/RB switches main screens,
and the right stick scrolls. Keyboard arrows, Enter, Escape and Page Up/Down work
with Steam Input keyboard mappings. Settings includes Close app; sync stays running.

Steam/Gamescope launches open fullscreen and scale the interface for the display,
including 2560×1600 and 4K. Desktop sessions retain a normal window. Controller mode
hides the thin scroll thumb and shows a More below cue when content continues.
Focus is restored after background updates, and dialogs contain navigation.
Virtual gamepad tests cover these actions in the actual Electron renderer; physical
Legion Go controls still need hardware acceptance.

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
**Make available on my devices** for a configured game on the source. No folder
picker opens: the service uses the Steam shortcut's executable and working directory,
queues preparation and reports bytes checked in Transfers. Repeated requests reuse
the same queued job. Pause/Resume stays available while preparation runs. On another device choose **Download**
and a library folder. Transfers supports pause/resume, restart recovery, four
parallel verified chunks, source failover and reuse of matching blocks from prior
managed versions. Available devices with the same version can supply chunks.

Each new share bundles the installation, its complete Proton prefix, the exact
selected Proton runtime, launch flags, referenced user launchers/libraries and
Steam artwork. Existing prefix links to older Proton DLLs are included too.
Regular game files keep their bytes, permissions and modification times. Internal
symlinks are relocated inside the bundle; Wine drive/device mappings are retained.
Only paths in copied registry files and text launch helpers are adapted to the
new library. NoSteam finds the installation, prefix and runtime from current Steam
settings. Generic launcher directories such as bin or Downloads are rejected
rather than sharing unrelated files.

Downloads install into a new `NoSteam/<game>/<version>/files` folder. Existing game
versions and saves are retained. With Steam closed, NoSteam journals registration
of the non-Steam shortcut, copied compatibility tool and save binding. If Steam is
open, Transfers says **Close Steam to finish** and completes registration when it
closes; an interrupted registration recovers without downloading again. **Play in
Steam** appears only after registration succeeds. Destination Steam artwork is
preserved. Re-sharing an imported game includes its relocated prefix and helpers.
Older files-only offers must be re-shared by an updated source to gain Steam setup.

The first supported bundle target is the same operating system and CPU architecture
as the source, with a case-sensitive Linux library supporting symlinks. This includes
case-distinct Wine prefix paths. External save folders outside the installation or
prefix, ambiguous Steam accounts and unsupported symlinks stop setup with a clear
message. Privileged/system launch helpers are reported as **Host setup required**
on devices where they are missing; the app does not grant privileges. Existing
save bindings need a clean save check and no pending upload before switching to a
copied installation. Per-game manifests, prefix contents and local paths stay on
the devices; the server receives version, title, size and availability only.

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
Electron lifecycle check. A disposable complete Linux bundle has also been transferred from macOS to Bazzite
over LAN, resumed after restart and registered in an isolated Steam library. Real-game
playability and a second physical Bazzite remain acceptance work.
`npm run dev` and `/preview.html` provide clearly labelled example data for visual
review; the preview is excluded from production builds.

## Migrating an existing Decky installation

Disable **NoSteam Saves** in Decky's plugin settings before migrating its stable
launcher. Keep the plugin directory until launcher migration succeeds: old wrappers
load the Python agent from that directory. With the companion installed, run its
Python executable with `-m nosteam_client.migration`. This refuses migration while
a game session holds a save lock, backs up the recognized old launcher, verifies
the new runtime, and atomically replaces only the launcher. Steam launch options,
save files, pending uploads and history remain in place. Unknown/custom launchers
are left unchanged. The previous script is retained under `launcher-backups/` in
the existing state directory. A missing runtime still allows an existing wrapped
game command to launch without sync.

In Bazzite Desktop Mode, add the application menu entry to Steam to open NoSteam
from Gaming Mode. The native `steamos-add-to-steam` utility accepts
`~/.local/share/applications/nosteam-companion.desktop`; let Steam update its own
shortcuts rather than editing `shortcuts.vdf` while Steam is running.
