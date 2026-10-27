# Third-party software and data

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
