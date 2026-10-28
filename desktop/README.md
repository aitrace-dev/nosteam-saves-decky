# NoSteam companion alpha

A standalone desktop interface and user background service for non-Steam save
sync. The app does not depend on Decky. The directory name of this repository's
parent checkout is historical; client code stays in the public client repository.

This first slice opens Games, Saves, Transfers, Devices, and Settings. It reads
existing save configuration, pairs accounts, checks configured saves, displays
cloud history, resolves conflicts through the existing save engine, and retries
pending save uploads independently of the window. Game sharing is explicitly off:
full-game downloads and cross-device applied-revision reporting are not yet
implemented. Configuring additional save wrappers also remains a later milestone.

The interface uses a colorful game-library layout, selectable cover tiles, local
Steam artwork, and explicit save-state labels. Covers are read through the private
service from discovered shortcuts' Steam grid directories. The app does not look
up or upload your library to an artwork provider. Missing covers have a title-based
fallback; images are size-limited and read without following symlinks.

## Run from source

Use Python 3.11+ and Node 24. From the client repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cd desktop
npm ci
npm start
```

In the private development workspace, `uv sync --locked` installs the Python
client; the desktop app also recognizes the workspace `.venv`. Set
`NOSTEAM_PYTHON` to an absolute Python executable to use another environment.
The desktop app starts the service if it is not already running.

Closing the app leaves the service running. Reopening reuses the same service.
The service holds a singleton lock and uses a mode-0600 Unix socket in the
mode-0700 app state directory. The renderer has no Node access and talks through
a narrow Electron IPC interface; only approved device-pairing URLs open externally.

For a disposable development session, use an empty temporary directory:

```bash
NOSTEAM_HOME="$(mktemp -d /tmp/nosteam-dev.XXXXXX)" npm start
```

`NOSTEAM_HOME` defaults to `~/.local/share/nosteam-saves`, preserving existing
pairing, game bindings, pending uploads, recovery journals, and backups. Do not
run a development build against real state unless you intend its existing save
sync commands to operate normally. There is no deletion, uninstall, or automatic
migration command in this alpha. Existing launch wrappers and the legacy service
are not rewritten or removed. Per-game locks protect concurrent save operations.

## Background service on Linux

The app starts a detached service for development. To start at login and restart
after failure on Linux, install a systemd user unit using the absolute executable
path from your installed Python environment:

```ini
[Unit]
Description=NoSteam background sync
After=network-online.target

[Service]
ExecStart=/ABSOLUTE/PATH/TO/.venv/bin/nosteam-service
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

Save as `~/.config/systemd/user/nosteam-companion.service`, then use
`systemctl --user daemon-reload` and
`systemctl --user enable --now nosteam-companion.service`. This source setup does
not replace an existing service or provide a migration installer. Reboot/login
startup and Steam Gaming Mode navigation still require Bazzite acceptance testing.
Suspending the device suspends background work too.

## Build and check

```bash
npm test
npm run test:desktop
npm run package:linux
```

`test:desktop` launches the real Electron window and real Python service against
temporary fixtures, verifies navigation and private IPC, closes the window, and
checks that the service and save data remain intact. It needs a graphical session
(use Xvfb in Linux CI). It stops its own service and removes only its temporary
fixture directory. It never contacts a real account or gaming device.

The Linux package under `out/` is the desktop shell, not a complete installer.
It still requires the separately installed Python client and `NOSTEAM_PYTHON`
pointing at that runtime. Bundling the runtime, migrating old installations,
desktop integration, and controller acceptance are later packaging work.

`npm run dev` serves the interface for design inspection. Open `/preview.html`
for clearly labeled example data or `/preview.html?empty` for first run. The
preview performs no real operations and is excluded from the production build.
Normal browser access to `/` cannot access the private service.
