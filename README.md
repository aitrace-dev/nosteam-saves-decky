# NoSteam Saves · Decky client

Open-source Decky client for cloud saves **only for non-Steam games**, built for Bazzite.
The hosted service is https://nosteam.bluggie.com. The server is maintained separately;
this repository contains only the plugin and its model-free Python agent.

## Alpha status

This is an early developer alpha, not an official Decky Store listing or a finished public
release. The account/device-pairing interface is still being built. The current setup uses
an issued device bearer credential; it does not support the server's retired shared key.
Do not enter a Clerk secret key, GCP credential or Gemini key into the plugin.

The sync engine downloads before launch, uploads after exit, queues offline progress,
backs up local saves before restoring, and preserves conflicting versions for the user
to choose. Only Steam's non-Steam shortcuts are eligible. Save discovery uses local game
definitions and optional server-side AI assistance; no AI model runs on Bazzite.

## Build

Use Node 24 and Python 3.11 or newer:

```bash
npm ci
npm run package
```

The ZIP is written to `out/nosteam-saves-0.1.0.zip`. For developer testing, the Python agent
must still be installed separately on each device:

```bash
bash scripts/install-agent.sh
```

That installs a user-owned environment and an upload-retry service. Finish account pairing
and test with disposable saves before relying on it for game progress. The ZIP alone does
not install the agent yet.

## Client checks

```bash
python3 -m pip install ".[test]"
python3 -m pytest -q
```

The client tests cover non-Steam-only guards, safe save paths and catalog discovery.
Server-backed two-device integration tests remain in the separate development repository.

## Distribution

Direct ZIP distribution is planned first. No Decky Store submission has been made.
The store's current submission checklist includes an AI-authorship declaration and SteamOS
testing requirements; publishing this source does not establish store eligibility.
See https://github.com/SteamDeckHomebrew/decky-plugin-database/blob/main/.github/PULL_REQUEST_TEMPLATE/plugin_addition.md.

MIT licensed. See `LICENSE` and `THIRD_PARTY.md` for notices.
