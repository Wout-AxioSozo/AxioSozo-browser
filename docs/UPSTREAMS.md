# Source provenance and updates

The authoritative pins and verified archive checksums are in `upstreams.lock.json`.
Revisions were resolved from public upstreams on 2026-09-22 and build inputs were
rechecked on 2026-09-23. Source checkouts are
ignored generated inputs with upstream remotes; the maintainable local work is in
`apps/`, `crates/`, `contracts/`, `packages/`, `native/` and `patches/`.

| Input | Pin | Use |
| --- | --- | --- |
| Zen | 541ee55196395ee8082862b83c84c90ab41d9f6d / 1.23.4b | Application source + small MPL chrome overlay |
| Firefox | 48a735cc74a7e6d730f9bf59c3de58f750287a23 / 158.0.1 candidate build 1 | Zen's actual Gecko source selection; SHA512-verified archive extracted and locally committed as a pristine build baseline |
| Surfer | 1.14.9 | Audited pinned Zen build driver; Zen package-lock retained |
| CEF | 47dbc1df023e99f006fb9e850a8d83cc19b4fdd9 / 156.0.7 | Official macOS ARM64 minimal binary; version and SHA256 pinned |
| Chromium | ab5de11f88f0792d2ad84e22abd460412e915f13 / 156.0.8078.17 | Engine inside that CEF distribution |
| T3 Code | b5a0f810108d42ca8635b5a3d75a6e885bb3a254 | Small audited TypeScript extraction, provenance manifest retained |
| Helium | Not imported | Complete browser fork; no embedding code needed for current candidate |

Reconstruction starts with `./dev setup`. `scripts/upstreams.py` clones missing
pinned checkouts with Git hooks disabled and verifies existing revisions. It never
resets or cleans an existing checkout. The Zen overlay rejects unknown base contents;
the Firefox archive must match its SHA512 before extraction. CEF setup checks SHA256
before loading native code. Dependency installs do not run unreviewed lifecycle hooks.
See component notes for source/build prerequisites. A full arm64 native build and
subsequent incremental rebuilds have completed on the project APFS volume; see
`docs/evidence/zen-native-build-log-index.json` and
`docs/evidence/root-setup-input-flow-20260923.json`.
Cargo.lock pins all Rust transitive checksums and the
provider runtime has no npm install step. Root setup tries its project-scoped
external Cargo cache in offline mode. If unavailable, it fetches the exact Cargo
lock into that cache, then compiles offline. The clean-cache path was exercised on
23 September: the first root check failed on missing `serde_json`, exact locked
crates were fetched to the project APFS cache, and the next root check passed.
See `docs/evidence/root-check-20260923-after-fetch.json`.

The root coordinator builds with Rust 1.99.1 from the root `rust-toolchain.toml`
(rustup); a toolchain change forces a coordinator rebuild. Firefox uses the
pinned Rust 1.95.0 toolchain.
Provider tests use the project-pinned Node 22.23.4; there is no separate global
Node installation requirement. The full Zen toolchain
is now checksum-pinned and installed only on the T9-backed project volume,
including cbindgen 0.29.4 and WASI SDK 34. See `patches/zen/toolchains.json` and
`docs/zen-notes.md`; no global install occurred.

On 22 October 2026 Zen moved from 1.22.3b (Firefox 156.0) to 1.23.4b
(Firefox 158.0.1), which carries Mozilla's fixes through MFSA 2026-111. Every
overlay and Firefox-native record was re-derived from the new upstream bytes:
the anchors were unchanged except for the browser-commands hunk offsets and
Firefox 158's `WebRenderCommandsResult` return type in the engine-view canvas
display item. Zen's package-lock is also moved from follow-redirects 1.16.0 to
1.16.1 (GHSA-q3jh-wrc6-jgq8, GHSA-8r9p-f939-6h3c). The previous checkout is kept
as `upstream/zen-1.22.3b-f0f21cd`. Rerun the full native and security test
matrix before treating this development build as a current daily browser.

Security update owner: Wout/the maintainer of this development fork. Before daily
use and whenever Mozilla, Zen or Chromium announces a relevant fix, review upstream
advisories, resolve a compatible Zen+Firefox and CEF combination, and update pins in
a separate local change. Download official artifacts, verify published integrity,
audit changed install/build hooks, rebase the small overlays with checksum conflicts
visible, regenerate the matching installed Codex schema, and re-run check/test plus
all real E0/E1/E2/TLS/profile/smoke tests. Keep prior source and build stamps for
rollback. Never treat an old component PASS as evidence for a new native build.
The stock Zen updater is disabled in the dev build so it cannot overwrite patches.
A production updater and signing/notarization pipeline are not implemented.

Redistribution review must preserve the actual MPL source and notices for modified
Zen/Firefox files and make corresponding source available when required, retain
T3's MIT notice, and ship CEF/Chromium third-party notices with binaries. CEF is not
Google Chrome: codecs, DRM, Google services and extension compatibility are not
automatically included. There is no imported Helium GPL code. No commercial agreement
was accepted and no binary release was published. The source repository is public
under the root MPL-2.0 license for original AxioSozo code; upstream files retain
their own notices.

Primary references checked: [Zen source](https://github.com/zen-browser/desktop),
[Zen build guide](https://docs.zen-browser.app/contribute/desktop/building),
[chrome structure](https://docs.zen-browser.app/contribute/desktop/code-structure-and-prefs),
[CEF](https://github.com/chromiumembedded/cef),
[CEF native usage](https://chromiumembedded.github.io/cef/general_usage),
[T3](https://github.com/pingdotgg/t3code),
[Helium license boundary](https://github.com/imputnet/helium#license), and
[Mozilla MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).
Provider route/terms references are maintained separately in PROVIDERS.md.
