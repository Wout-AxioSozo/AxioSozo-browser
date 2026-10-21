# AxioSozo browser

Zen/Gecko remains the application; no Electron, external Chromium window or iframe counts as dual-engine integration. Never claim READY unless real macOS E1/E2 pass.

- Preserve TLS, sandboxing, permissions, profiles, accessibility, extensions and dev tools.
- Never read personal profile data or credentials, and make no live provider calls without explicit authorization.
- Build with `dev-external python3 scripts/storage.py exec -- ...` (uses the /Volumes/AxioSozoBuild volume).
- Workstreams (Zen, CEF, providers, integration/security) own separate paths; only the integration lead touches root dev, shared contracts, the lockfile and final status.
