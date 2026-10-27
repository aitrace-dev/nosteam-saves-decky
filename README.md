# NoSteam Saves · Decky client

Open-source Decky client for cloud saves **only for non-Steam games**, built for Bazzite.
The hosted service is https://nosteam.bluggie.com. The server is maintained separately;
this repository contains only the plugin and its model-free Python agent.

## Alpha status

**Do not install 0.2.0:** its launcher incorrectly reused Decky’s frozen executable and
was associated with a Steam webhelper crash loop. Version 0.2.1 uses system Python.
The installed bridge passes repeated frozen-runtime checks. After restarting Decky on
Bazzite, status polls succeeded and Steam remained stable for over a minute of observation.
Full gameplay and two-device acceptance checks are still required.

This is a developer alpha, not an official Decky Store listing. Version 0.4.1 includes
browser sign-in, clearer game/status sections and a bundled Python agent: no separate terminal installation is required.

Open the plugin and select **Sign in**. Choose **Confirm sign-in** in the browser using your
NoSteam Saves account; the device link is carried automatically, with no code to type.
The plugin discovers eligible non-Steam shortcuts and configures cloud saves automatically.
Use Steam's normal Play button afterward.

Known game definitions are checked locally. For unknown locations, the client exchanges
bounded filesystem metadata with the hosted server, where Gemini proposes the next paths
to check. Only locally verified paths can be enabled. Unresolved locations stay pending and
retry automatically; there is no required game selection or folder picker. No AI runs on
Bazzite. Discovery does not guarantee support for every game or save format.

The sync engine downloads before launch, uploads after exit, queues offline progress,
backs up local saves before restoring, and preserves conflicting versions for the user
to choose. AI does not overwrite divergent progress. Only non-Steam shortcuts are eligible.

Browser approval and controller behavior still need a full installed-plugin acceptance
check. Test with disposable saves before relying on this alpha for game progress.

## Build

Use Node 24 and Python 3.11 or newer:

```bash
npm ci
npm run package
```

The self-contained ZIP is written to `out/nosteam-saves-0.4.1.zip`. It includes pure Python
dependencies and their license notices. The bundled runtime has been checked on Bazzite
without installed site-packages, using temporary folders and no real saves.

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

## Library controls and discovery limits

The game dropdown shows one shortcut at a time. Exclude a game from cloud sync without
removing it from Steam, and enable it again later. New shortcuts are discovered automatically;
removed shortcuts stop scheduled sync while history is retained. A recognized reinstallation
can reconnect its existing identity, keeping previous progress. Steam-owned games are never listed.

Previously suggested locations are rechecked locally when a first save appears, without
another AI request. Ambiguous folders remain pending. The selected game is remembered when
the panel closes and reopens.

Unknown save searches back off for 1, 2, 4 and then 7 days. Network failures wait 30 minutes;
known definitions and verified cached paths are checked without model calls. The hosted server
also enforces persistent global/account/game budgets before every paid request.

Run `npm test` for the Decky React interaction suite. It covers game selection, polling,
exclusion/re-enabling, restoring launch options, conflict choices and restore confirmation.
Native controls are mocked in this suite; installed Steam panel layout is verified separately
with actual Bazzite screenshots. A full real two-device gameplay handoff remains outstanding.
