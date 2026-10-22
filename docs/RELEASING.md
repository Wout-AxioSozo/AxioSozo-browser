# Releasing an AxioSozo Preview (F7)

Status: **pipeline prepared, nothing published. BLOCKED_SIGNING_IDENTITY** until
[open decision 3](HANDOFF_3.md#11-open-decisions-for-wout) (the Apple Developer
account) is resolved. Every artifact is labelled **Preview**, never READY.
[AGENTS.md](../AGENTS.md) applies in full.

Nothing is pushed, tagged, uploaded or published without Wout's explicit
authorization **for that specific release**. The pipeline itself never runs
`git push`, `git tag`, `gh release` or any upload. It contacts Apple only when
invoked with both `--notarize` and `--authorized`.

## What the pipeline does

`scripts/release/preview.py` (Python standard library only):

| # | Step | Needs | Verifies / produces |
| --- | --- | --- | --- |
| 1 | Source gate | — | `git status` shows no modified, staged or untracked files; records the exact `HEAD` commit; `--expect-commit SHA` pins it. A real run refuses a dirty tree before writing anything. |
| 2 | Locate app | — | `scripts/zen.py describe` (build fingerprint must match the sources), or an explicit `--app` recorded as `EXPLICIT_PATH_UNVERIFIED`. |
| 3 | Preflight: branding | — | `CFBundleIdentifier` is `nl.axiosozo.*` and has no Mozilla/Firefox/Zen/Chrome/Google token. User-visible `Info.plist` strings, `InfoPlist.strings`, `brand.ftl` and `brand.properties` values have no Firefox, Mozilla, Zen, Chrome, Chromium, Google, Nightly or Twilight mark (comments ignored). `application.ini` profile root must be AxioSozo-owned. The icon must match `apps/browser/branding/firefox.icns`. Other en-US UI strings that mention a mark are reported for review only. |
| 4 | Preflight: packaging | — | No symlink leaves the bundle; no objdir markers (`MozillaDeveloperObjPath`) and no test harness binaries (`gtest`, `xpcshell`, …). Only `mach package` output can pass. |
| 5 | Preflight: updater | — | `AppConstants.MOZ_UPDATER` is false and no `updater.app` / updater launch service exists. Unknown fails closed. |
| 6 | Preflight: Safe Browsing | — | `MOZ_GOOGLE_SAFEBROWSING_API_KEY` present or `no-google-safebrowsing-api-key`. The value is never printed. When absent, a `--notarize` (distribution) run is blocked (`SAFE_BROWSING_KEY_ABSENT`); a local run only warns, and every artifact says the build ships **without** phishing/malware protection. |
| 7 | Preflight: Chromium | — | `LSEnvironment` must not set `AXIOSOZO_ENGINE_SWITCHING=1`. The chrome env gate must exist in source. An embedded CEF host is labelled experimental. |
| 8 | Preflight: notices | — | Gecko `about:license` is present; if CEF is embedded, its `LICENSE.txt` and `CREDITS.html` are present; repository `LICENSE` and `THIRD_PARTY_NOTICES.md` exist. |
| 9 | Stage | — | `ditto` copy to `/Volumes/AxioSozoBuild/release/preview-<version>-<commit12>/dmg-root/`. The stage must be on APFS/HFS+ (exFAT AppleDouble files break codesign). The CEF host (default `/Volumes/AxioSozoBuild/cef/AxioCEFProbe.app`, `--cef-app none` to omit) is embedded at `Contents/Helpers/AxioCEFProbe.app`. `xattr -cr`. An existing stage is never deleted or reused. |
| 10 | Ship notices | — | Adds `Contents/Resources/axiosozo-notices/{LICENSE, THIRD_PARTY_NOTICES.md, PREVIEW-NOTICE.txt}` and a Preview read-me at the DMG root; re-checks notices, branding and packaging on the staged copy. |
| 11 | Release notes | — | `RELEASE_NOTES.md` draft: Preview, manual updates, Safe Browsing status, Chromium limits (no proprietary codecs, no Widevine), license pointers. |
| — | **Stop** | | Without an identity the run ends here with exit 10 `BLOCKED_SIGNING_IDENTITY`. |
| 12 | Codesign | identity | Inside-out: every nested Mach-O and bundle deepest-first. Framework libraries come before their framework, all CEF helper apps before the CEF app, and every helper before the main `.app`, which is signed last. Every item uses `--options runtime`. |
| 13 | Verify signatures | identity | `codesign --verify --deep --strict`; `codesign --display` per item confirms the hardened runtime flag and `TeamIdentifier`; the main app's entitlements include `allow-jit`. |
| 14 | Notarize app | identity + authorized | `ditto -c -k` zip, `xcrun notarytool submit --wait`, `xcrun stapler staple` + `validate` on the `.app`. |
| 15 | DMG | identity | `/Applications` link, `hdiutil create -fs HFS+ -format UDZO`, `codesign` the DMG, `hdiutil verify`, `codesign --verify --strict`. |
| 16 | Notarize DMG | identity + authorized | `notarytool submit --wait`, `stapler staple` + `validate`, then `spctl --assess` for the app (execute) and the DMG (open). |
| 17 | Checksums | identity | `SHA256SUMS.txt` (`shasum -a 256 -c` format); final `RELEASE_NOTES.md`; `release-report.json`. |

Without `--notarize --authorized`, signatures use `--timestamp=none`. Apple's
timestamp service is part of the network path. The DMG is then a **local
verification artifact that must not be distributed** (exit 11).

### Exit codes

| Code | Status | Meaning |
| --- | --- | --- |
| 0 | `PREVIEW_PREPARED_NOTARIZED` | Signed, notarized, stapled and verified. Still a Preview. |
| 2 | usage | Invalid flags (e.g. `--authorized` without `--notarize`). |
| 3 | `SOURCE_REFUSED` | Dirty tree, no commit, or `--expect-commit` mismatch. |
| 4 | `PREFLIGHT_BLOCKED` | Branding, packaging, notices, updater, Safe Browsing state or Chromium default. |
| 5 | `BLOCKED_ENV` | App not found, stage root not external/APFS, stage exists, TMPDIR not external. |
| 10 | `BLOCKED_SIGNING_IDENTITY` | No valid Developer ID Application identity/team (decision 3). |
| 11 | `PREPARED_NOT_NOTARIZED` | Signed locally; notarization not requested or not authorized. |
| 12 | `VERIFY_FAILED` | codesign, notarization, stapling or Gatekeeper failed. |

`--dry-run` prints every command, including each codesign invocation in order,
the entitlement provenance and the release notes template. It writes nothing and
runs no Apple tool. It exits with the code a real run would reach, although a
real run stops at a dirty tree.

## Prerequisites (all by Wout)

1. **Apple Developer Program membership** and a **Developer ID Application**
   certificate in Wout's login keychain ([decision 3](HANDOFF_3.md#11-open-decisions-for-wout)).
   Agents never list or read keychain identities; the identity string is an input:
   `AXIOSOZO_SIGN_IDENTITY="Developer ID Application: NAME (TEAMID)"` and
   `AXIOSOZO_TEAM_ID=TEAMID` (or `--identity`, `--team-id`).
2. **Notary credentials**, stored by Wout himself:
   `xcrun notarytool store-credentials <profile-name> --apple-id <id> --team-id <TEAMID>`
   (prompts for an app-specific password). The pipeline only passes the
   profile name (`AXIOSOZO_NOTARY_PROFILE` or `--notary-profile`) to notarytool.
3. **A packaged build.** The objdir `AxioSozo Dev.app` is not distributable
   (see blockers below). The Zen workstream must add a package step
   (`mach package` via Surfer) with Preview branding.
4. Mounted project volume: run `/Users/wout/.local/bin/mount-dev-storage` (DevStorage
   only), then `python3 scripts/storage.py setup` (mounts the AxioSozoBuild volume
   via `mount-axiosozo` if needed; `storage.py exec` refuses to run without it).
5. A clean tree at the commit being released.

## Commands

Run through the global wrapper so Apple tools use the build volume's TMPDIR:

```sh
/Users/wout/.local/bin/mount-dev-storage
python3 scripts/storage.py setup   # mounts the AxioSozoBuild volume if needed
W="/Users/wout/.local/bin/dev-external python3 scripts/storage.py exec --"

# Plan only; modifies nothing, contacts nothing.
$W python3 scripts/release/preview.py --dry-run [--app PATH] [--json]

# Everything that needs no identity (stage, notices, notes); exits 10 today.
$W python3 scripts/release/preview.py --app PATH --expect-commit SHA

# Local signed DMG, not distributable (exit 11). Never contacts Apple.
$W python3 scripts/release/preview.py --app PATH --expect-commit SHA

# Only with Wout's explicit authorization for this release: timestamp + notarize.
$W python3 scripts/release/preview.py --app PATH --expect-commit SHA --notarize --authorized
```

The two signed runs need `AXIOSOZO_SIGN_IDENTITY` and `AXIOSOZO_TEAM_ID` exported
by Wout; the notarized run also needs `AXIOSOZO_NOTARY_PROFILE`.

Tests: `python3 -m unittest tests/test_release_preview.py` (also collected by
`./dev test`). Fixtures live in `.local/release-tests/` and, for staging,
`/Volumes/AxioSozoBuild/tmp/release-tests/`; both are removed after each test.

## Entitlements

Mirrored from the pinned Firefox 156.0 source in the build stage
(`/Volumes/AxioSozoBuild/zen/source/engine`). The mapping follows
`taskcluster/config.yml` → `mac-signing` → `hardened-sign-config` → `production`.
The files are in `scripts/release/entitlements/`.

| Item | File | Source |
| --- | --- | --- |
| `.app` root | `browser.plist`: `allow-jit`, `disable-library-validation` (pkcs11), audio-input, camera, usb, location, smartcard | `security/mac/hardenedruntime/production/firefox.browser.xml` (pristine Firefox commit; Zen's copy only changes the application identifier) |
| `plugin-container.app` | `allow-jit` | `production/plugin-container.xml` |
| `media-plugin-helper.app` | `disable-library-validation` | `production/media-plugin-helper.xml` |
| `security-module-helper.app` (if present) | `disable-library-validation`, smartcard | `production/security-module-helper.xml` |
| XUL, dylibs, crashhelper, crashreporter.app, gpu-helper.app, pingsender, nmhproxy, ChannelPrefs.framework, dmgInstallHelper | none (hardened runtime only) | same config |
| CEF Helper (Renderer), Helper (GPU) | `cef-helper-jit.plist`: `allow-jit` | **UNVERIFIED ASSUMPTION**: the pinned CEF minimal archive has no entitlements file |
| other CEF items | none | **UNVERIFIED ASSUMPTION** |

The restricted keys `com.apple.application-identifier` and
`com.apple.developer.web-browser.public-key-credential` are omitted. Firefox's own
`mach macos-sign -e production-without-restricted` does the same
(`tools/signing/macos/mach_commands.py`), because they need a provisioning
profile. As a result, platform passkeys are unavailable in the Preview.
`allow-unsigned-executable-memory` is not used; Firefox production does not use it.
The CEF workstream must prove E0 under this exact hardened-runtime signature before
a release embeds CEF.

## Update channel

The stock Zen updater stays disabled: `--disable-updater` and `MOZ_UPDATER: false`.
Zen's update server must never update this app. Firefox's MAR updater needs our
own MAR signing keys and our own update host, and **neither exists**. Until they
do, every Preview states **updates are manual**, both in the in-app
`PREVIEW-NOTICE.txt` and in the release notes. Security releases follow Firefox's
cadence (see [handoff §9](HANDOFF_3.md#9-keeping-up-with-zen)) and require a new
Preview.

## Safe Browsing key

The overlay deliberately builds without personal Zen key files
(`patches/zen/overlay.json`, `configs/common/mozconfig`). A distribution build needs
AxioSozo's own Google Safe Browsing key supplied through a build-time key file
outside the repository. Until then, the pipeline detects
`no-google-safebrowsing-api-key` and refuses any `--notarize` run with the
`SAFE_BROWSING_KEY_ABSENT` preflight blocker (exit 4), so no build without
phishing/malware protection can be distributed. Local runs (dry-run, unsigned
stage, locally signed DMG) only warn, and every such artifact states that it ships
**without phishing/malware protection**. Key procurement and storage are Wout's
decision; agents never read key files.

## Branding and licensing checklist

- [ ] Bundle ID `nl.axiosozo.*`, not the `.dev` identity; display name without "Dev".
- [ ] `MOZ_APP_PROFILE` and `MOZ_APP_VENDOR` are AxioSozo-owned (profile root must not be `zen`).
- [ ] No Firefox/Mozilla/Zen/Chrome marks in `Info.plist`, `InfoPlist.strings`, `brand.ftl`, `brand.properties`.
- [ ] Review the `UI_STRING_MARKS` report (Zen/Firefox UI strings outside the brand files).
- [ ] AxioSozo icon (`apps/browser/branding`) in the bundle.
- [ ] `about:license` present; `axiosozo-notices/` contains the MPL-2.0 text, `THIRD_PARTY_NOTICES.md` and the Preview notice.
- [ ] CEF `LICENSE.txt` and `CREDITS.html` present if CEF is embedded.
- [ ] If the coordinator or provider host is ever bundled: Rust crate notices (Cargo.lock) and the T3 MIT notice ship too. Neither is bundled today.
- [ ] Crash reporting: decide whether crash reports may go to Mozilla's server (currently `Enabled=1` with a mozilla.com URL) or disable it.
- [ ] MPL source availability: the release notes link the exact public commit.

## Hosting

GitHub Releases hosts the DMG, `SHA256SUMS.txt` and the notes; the release
notes also carry the checksum. The download website and domain are
[decision 2](HANDOFF_3.md#11-open-decisions-for-wout); it links to the release.
Whether Jev key entry ships in the first Preview is
[decision 4](HANDOFF_3.md#11-open-decisions-for-wout); the notes say Jev live is
not included. Creating the release, uploading and tagging are done by Wout, or by
an agent only under his explicit authorization for that one release, never by
this pipeline.

## Current blockers (dry-run against the dev bundle, 27 September 2026)

| Blocker | Owner |
| --- | --- |
| `BLOCKED_SIGNING_IDENTITY`: no Apple Developer account (decision 3) | Wout |
| `OBJDIR_DEVELOPMENT_BUNDLE`, `SYMLINKS_LEAVE_BUNDLE` (7774), `TEST_ARTIFACTS_PRESENT`: only the objdir `AxioSozo Dev.app` exists; no `mach package` output | Zen workstream (`scripts/zen.py` package step) |
| `PROFILE_ROOT_NOT_ISOLATED`: `application.ini` `Profile=zen`, from Zen's `src/toolkit/moz-configure.patch` default `MOZ_APP_PROFILE="zen"`. An installed build would use `~/Library/Application Support/zen`, the stock Zen profile root. | Zen workstream (mozconfig `MOZ_APP_PROFILE`/`MOZ_APP_VENDOR`) |
| Warnings: `.dev` bundle ID and "AxioSozo Dev" name; `Vendor=Mozilla`; `SourceRepository` zen-browser; crash reports to Mozilla; Zen team requirement in `SMPrivilegedExecutables`; Safe Browsing key absent (a blocker for any `--notarize` run) | Zen workstream / Wout |
| CEF under hardened runtime unproven; the adapter only launches CEF from a `./dev` session, so embedded CEF is unusable in a Finder launch | CEF workstream |
