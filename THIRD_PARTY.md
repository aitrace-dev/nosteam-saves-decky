# Third-party software and data

The standalone companion under `desktop/` uses Electron (MIT), Vite (MIT), and
Inter and Space Grotesk through Fontsource (SIL Open Font License 1.1). Electron includes
Chromium and Node.js with their bundled third-party notices; retain these in
distributed builds. Playwright and Electron Packager are development tools,
licensed Apache-2.0 and BSD-2-Clause respectively. Exact versions and resolved
licenses are recorded in `desktop/package-lock.json`.

The design-only preview references promotional artwork from the Steam product
pages for Celeste (Maddy Makes Games), Hades (Supergiant Games), and Hollow Knight
(Team Cherry). Credits and links appear below that preview. This artwork is not
bundled with the production app. Production artwork is read from the user's own
Steam grid directory and is not uploaded to the hosted service.

This client uses Decky development packages and public interfaces: `@decky/api`, `@decky/ui`
and `@decky/rollup`. Their published licenses remain applicable. Dependency versions are
recorded in `package-lock.json` and the Python project configuration.

Python dependencies include HTTPX, ValvePython's `vdf` and PyYAML. The plugin ZIP bundles
their pure Python runtime dependencies, with license notices retained under `vendor/`.

The cloud icon is Font Awesome's `cloud`, distributed through `react-icons`.
Font Awesome Free icons are CC BY 4.0: https://fontawesome.com/license/free.
`react-icons` code is MIT licensed.

The optional catalog downloads https://github.com/mtkennerly/ludusavi-manifest.
It publishes an MIT license and derives information from PCGamingWiki; preserve its
attribution and applicable terms: https://www.pcgamingwiki.com/wiki/PCGamingWiki:Copyrights.
The full catalog is not bundled in this repository or the plugin ZIP.
