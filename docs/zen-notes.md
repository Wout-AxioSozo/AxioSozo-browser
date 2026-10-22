# Zen workstream — actual implementation and limits

Current checkpoint, 2026-10-22 (security update): Zen moved from 1.22.3b (`f0f21cd`, Firefox 156.0) to 1.23.4b (`541ee55`, Firefox 158.0.1, Mozilla revision `48a735cc`; source SHA512 checked against Mozilla's SHA512SUMS). Every overlay, retired-stock and Firefox-native record was re-derived from the new upstream bytes. Only two needed more than new hashes: the browser-commands hunk offsets, and the engine-view canvas display item, which returns `Ok()` because Firefox 158's `CreateWebRenderCommands` returns `WebRenderCommandsResult`. Zen's own changes required ZEN_ADAPTER_CONTRACT updates: toasts moved to `ZenToasts.mjs`, tab renaming to `ZenTabRenamer.mjs`, sidebar expansion to `ZenVerticalTabsManager.mjs`, and `addTab` route containers now go through `shouldUseRouteContainer()`. Project-container tabs still pass `skipRoute`, which that contract now pins. A full native arm64 build in the `workstation` build root completed and was stamped (`build-stamp.json`); XUL contains the engine-view canvas and accessibility hooks, and a headless launch with a throwaway profile rendered a page. The previous checkout is kept as `upstream/zen-1.22.3b-f0f21cd`. E1/E2 and the GUI were not rerun for this build, so the 2026-09-23 results below describe the old build only.

Previous checkpoint, 2026-09-23: the real custom macOS arm64 Zen build has succeeded (setup exit0). `AxioSozo Dev.app` has bundle-id `nl.axiosozo.browser.dev`; both its executable and XUL are arm64. The build used the authorized T9-backed APFS volume with two jobs. The integration lead then launched and quit the actual app through `./dev`, inspected Gecko fixture/TLS-warning UI, and manually observed real CEF fixture frames and an engine switch in the same Zen window. E1 remains experimental and the E2 result is limited to the local fixture. Earlier blocked checkpoints below are historical evidence. See [verified native build](evidence/zen-native-build-verified.json), [build logs](evidence/zen-native-build-log-index.json), [daily launch](evidence/daily-dev-gui-20260923.json) and [GUI observation](evidence/engine-gui-manual-20260923.json).

## Runtime diagnosis and controlled correction (pending native rebuild)

The actual daily launch log reported `main/search-config-v2 InvalidSignatureError` repeatedly. The pinned Mozilla source Git blob has SHA256 `20b2588cb99492f8eb49f009452205761ade0cbe77c07efb543dd8a12024f2e1`. Zen's `npm run import:dumps` filters records in that signed fallback collection without producing a new signature; the generated stage and built app contained the modified SHA256 `38616d9b61f39ae464b317a5b40fbafe6288a5cc7aeb5f2d0c93a7e3fd6ab155` (7,340-line diff). The build adapter now skips that upstream import and restores only the exact reviewed transformed bytes to Mozilla's pinned Git blob. Any unknown edit fails closed; signature verification remains enabled. `patches/zen/signed-search-dump.json` records both hashes.

The same log reported missing `browser/zen-library.ftl`. All 13 pinned `zen-*.ftl` files were present in the staged Zen locale tree, but none reached generated Firefox locales or the app. The adapter now installs them idempotently before build and rejects unexpected existing content. The source test covers complete, repeated and conflicting locale installs.

One profile-owner launch reported `DefaultBrowserCheck.sys.mjs` line 65 receiving a null Fluent result while constructing the set-default prompt. The default-browser localization and custom brand files were present, so that log alone does not establish the Fluent root cause. The development-only preference now sets `browser.shell.checkDefaultBrowser=false` to avoid prompting Wout to replace the system browser; ordinary manual default-browser settings remain available. No default browser was changed.

`ZenBoostsChild.sys.mjs` and `ZenGlanceChild.sys.mjs` also failed to load in the daily launch. The files exist in the built app and match pinned source byte-for-byte; this ruled out a missing source file. The search/locale correction did not disable either actor.

The fresh `runtime-fix` profile after the first rebuild removed the search-signature and missing-FTL warnings, but both Zen child actor load errors persisted. Firefox's `SyncModuleLoader.cpp` emits this generic error when its source load fails without a JavaScript exception. A built xpcshell could parse/import both modules when given an explicit `resource://axioactor/` substitution to the real files; that diagnostic xpcshell itself exited 139 and is not an actor runtime pass. The built app's actor paths were symlinks through `source/engine/zen` into `source/src/zen`, outside the app bundle. Mozilla's macOS content policy grants file reads inside `appPath`, with no grant for that Zen stage path. This explains why these lazy child modules cannot be read in the content sandbox even though the parent can see them. The second correction materializes exactly nine child actor, lazy helper and CSS resources as real files within the dev bundle after `mach build`. It checks each pinned source and destination first, refuses unknown files or links, and does not widen the sandbox. The source bootstrap suite passes 13/13. A subsequent native setup and clean-profile GUI run completed; its `browser-stdout.log` has no search-signature or child-actor load error. Fresh-profile migration and favicon warnings remain visible. See `evidence/root-setup-input-flow-20260923.json` and `evidence/engine-probe-517a04b3c0d19385/browser-stdout.log`.

The source-only bootstrap suite passed 12/12 after these corrections. This is not runtime proof: the integrator owns the single full native rebuild and GUI retest.

Mozilla [released Firefox 156.0.1 on 22 September 2026](https://www.firefox.com/en-US/firefox/156.0.1/releasenotes/). Its notes list an NVDA accessibility fix, link active-style and CSS anchor hangs, macOS 27 window behavior, and a Windows handle leak. The [Mozilla security advisory index](https://www.mozilla.org/en-US/security/advisories/) lists the 156.0 advisory but no separate 156.0.1 advisory as of 23 September; this is not proof that the point release is unnecessary. The current pinned Zen revision selects 156.0. Updating to 156.0.1 requires a new pinned source stage, exact Mozilla checksum and revision, patch compatibility checks, full native rebuild and GUI regression; the current `zen-import.json` explicitly prevents reusing its old baseline. Do not reinterpret the existing 156.0 build as current.

## Pinned source and inspected toolchain

- Zen `https://github.com/zen-browser/desktop`, commit `f0f21cdade1fd519a660d756942f7032a8c7a518` (local checkout `upstream/zen`, origin retained).
- That checkout's `surfer.json` selects Firefox 156.0 / candidate build1. Mozilla metadata identifies source Mercurial revision `a80bd15ddee3b4bf3679aeba340e9d2db933c467` in `https://hg.mozilla.org/releases/mozilla-release`.
- Source archive: `https://archive.mozilla.org/pub/firefox/candidates/156.0-candidates/build1/source/firefox-156.0.source.tar.xz`. SHA512: `0463304a0898670d248114f66f7c235166ae2397c3989a7c878c96f0c589fbbba1f1c87432daa22633b9fadd94394adf1dc37f0e67d22b066c75efe5eead75ce`. The 802,800,568-byte archive was downloaded, its actual checksum verified, and its source extracted on the project volume. [Download evidence](evidence/zen-firefox-download.log).
- `package-lock.json` pins `@zen-browser/surfer` 1.14.9. The exact 66,646-byte npm tarball was downloaded and its SHA512 integrity was verified against that lockfile before inspecting it. Upstream `.nvmrc` is 22, `.rust-toolchain` is 1.95.0, `.python-version` is 3.11. Node's upstream pin is only a major; `patches/zen/toolchains.json` now selects exact Node22.23.4, Rust1.95.0, Python3.11.17 (python-build-standalone release20261021) and GNUtar1.35 with official-source checksums (Node and Python bumped on 22 October 2026 for the 20 October Node security release; earlier pins were Node22.22.3 and Python3.11.16/20260901).
- Project-local tools now verified (reinstalled 22 October 2026 on the recreated build volume): Node22.23.4, npm10.9.9, Rust1.95.0, Python3.11.17, GNUtar1.35, cbindgen0.29.4 and WASI SDK34. The evidence links below record the earlier Node22.22.3/npm10.9.8/Python3.11.16 install. The pre-existing global Node24/Rust1.94/Python installations are unchanged; system Python3.9.6 can run this orchestration script. macOS SDK26.5 is available. `sccache` is optional and not installed. [Installer evidence](evidence/zen-toolchain-install-retry.log), [native preflight](evidence/zen-doctor-native-preflight.json).

Source evidence: [Mozilla buildhub](evidence/zen-firefox-buildhub.json), [Mozilla metadata](evidence/zen-firefox-metadata.json), [Mozilla checksums](evidence/zen-firefox-SHA512SUMS.txt), [doctor](evidence/zen-doctor.json).

The source metadata's mac build is x86_64; it is used only to establish the release source revision. It is **not** evidence of a local arm64 build or test.

## Original build/preflight result (2026-09-22)

`/usr/bin/python3 scripts/zen.py setup` ran the required `/Users/wout/.local/bin/mount-dev-storage`, then exited20 before any large source download or native build. DevStorage had13GiB free; [Zen's current build documentation](https://docs.zen-browser.app/contribute/desktop/building) requires at least30GB. Node22, Rust1.95.0 and GNU tar also need to be provisioned/selected. No existing storage was deleted, relocated or detached, and no global tool install was attempted. [Actual setup output](evidence/zen-setup.txt).

The implemented setup path stages a separate generated checkout at `/Volumes/AxioSozoBuild/zen/source`, uses `/Users/wout/.local/bin/dev-external` for build commands, pins/downloads/checks the Mozilla archive, uses the locked npm dependency graph with lifecycle scripts disabled, applies Zen's actual ffprefs/import/Surfer build flow, and limits native compilation to two jobs. Native output is `/Volumes/AxioSozoBuild/zen/obj`. This path completed successfully. It does not silently run `mach bootstrap`, install a global Rust/Node toolchain or invoke Zen's release CI hooks.

T9 project storage was then exFAT (the source is now on the APFS `AxioSozo` volume) and created AppleDouble files (`._*`), including spurious Git pack indexes. Git clone returned0 but printed index warnings. The generated native checkout belongs on DevStorage APFS, where Surfer's symlink-based source import can work. The project-owned APFS build volume `AxioSozoBuild` is physically stored inside `/Volumes/T9/Dev.sparsebundle` and mounted at `/Volumes/AxioSozoBuild`; the shared DevStorage volume is unchanged. The adapter filters `._*` from source/test enumeration. No Git object sidecars were deleted.

A stock Firefox artifact build was considered and not represented as a working Zen fork: this Zen revision has native mouse-tracking, split-view and other patches whose ABI cannot be assumed to match stock Firefox artifacts.

## Maintained patch boundary

`patches/zen/overlay.json` records before/after hashes and exact replacements. `0001-axiosozo-development-chrome.patch` is the reviewable diff. Setup/prepare/readiness reject unexported tracked changes, untracked source files and an ignored upstream mozconfig that the build clone would otherwise omit. A generated `.axiosozo-overlay-state.json` records hashes of owned mirror files, allowing legitimate source refresh while preserving unexported edits to those mirrors. `scripts/zen.py prepare` applies the overlay idempotently and rejects unknown local edits. New MPL2.0 module source remains authoritative under `apps/browser/chrome`; it is copied into `upstream/zen/src/zen/common/axiosozo` for Surfer's actual `jar.inc.mn` packaging.

Withdrawn overlay entries move to `patches/zen/retired.json` with their exact patched hashes and the pinned upstream hash. The source guard accepts those exact results, and the next overlay restores the pinned file from Git before applying active entries; any other content is still rejected. The 26 September reset retired every Zen look-and-feel change (minimal UI, pref default flips, hidden settings panes), so the frontend is stock Zen plus the startup hook.

- `ZenPreloadedScripts.js` imports `AxioSozoStartup.mjs` in the trusted browser window.
- Startup waits for Zen's real initialized promise, then installs the Gecko adapter and a small `Gecko · Dev` toolbar indicator. The indicator opens the trusted provider settings dialog only on an explicit user action. Provider discovery is not triggered during browser startup. Original location bar, certificate/permission/download UI, keyboard commands, accessibility, extensions, session restore and development tools remain upstream-owned.
- Zen compact mode starts enabled through the existing preference, hiding the tabbar while retaining the navigation toolbar. The welcome screen is suppressed in the separate development profile.
- `GeckoEngineAdapter.sys.mjs` wraps real `gBrowser` create/navigate/back/forward/reload/close calls and progress/tab listeners. Targets have browser-minted IDs, principal identity and document/navigation generations. Navigation invalidates old target grants synchronously. Private title/URL events are omitted. Privileged/file/javascript target URLs are rejected by this coordinator-facing adapter. Normal user navigation remains the normal browser command path.
- Navigation returns `accepted`; subsequent events are separate. General-site CEF switching and programmatic developer tools return `unsupported`, never a false success. The explicit local-fixture switch works; a failed candidate preserves the original tab.
- `BrowserCoordinator.sys.mjs` is attached asynchronously at startup and packaged in the chrome JAR. The integration lead has launched and interacted with the actual Zen window; fixture tests and pipe IPC tests cover separate boundaries. A full end-to-end privileged action in the actual GUI is still needed before claiming the coordinator is production-ready. Coordinator failure leaves ordinary browsing available. The module is not a security sandbox for providers.

The patch also removes reads of personal `$HOME/.zen-keys` from Zen's build config. Build/test does not read browser profiles, credentials or login state.

## Audited hooks and updater ownership

The npm project scripts, lockfile, ffprefs/import scripts, and exact Surfer download/bootstrap/build/import/branding modules were read before executing any project setup. `npm init`/`npm run init` is deliberately not the entrypoint: it would chain upstream bootstrap. Zen's CI scripts can install Rust globally and access release credentials; none were run.

Surfer eagerly imports `sharp` even when `SURFER_NO_BRANDING_PATCH=true`. The tiny reviewed `0002-surfer-lazy-branding.patch` delays that import when branding generation is disabled, so the native `sharp` install hooks do not need to execute. `surfer-no-branding.json` pins the dependency file's before/after hashes. Development branding is derived from Firefox's existing unofficial resources with a separate display name; no shipping artwork/redistribution claim is made.

The generated bundle must report `nl.axiosozo.browser.dev`. Exact Firefox configure source was inspected: `MOZ_MACBUNDLE_ID` is a suffix added to the distribution ID, so the config sets `nl.axiosozo` + `browser.dev` (not a duplicated full ID). Display name is `AxioSozo Dev`, remoting name is `AxioSozoDev`, and launch always has an explicit development profile and `-no-remote`.

The development build uses `--disable-updater` and an `axiosozo-dev` channel, with an invalid update hostname, so ordinary Zen application updates cannot overwrite this fork. This is **development update ownership**, not a security-maintenance policy for shipping. Before daily use/distribution, maintainers must review Zen and Mozilla security releases, update all pins/checksums together in a branch, reapply/review patches, run native/GUI/engine/security tests, and rebuild. No silent auto-update from an unrelated upstream channel is allowed.

## Entrypoints and lifecycle contract

- `python3 scripts/zen.py doctor`: read-only preflight; exits0 or20.
- `prepare`: applies the owned source overlay; exits0 or20.
- `setup`: mount/preflight then audited source import and visible native build. Successful reconstruction is now verified; repeated setup checks import receipts and reuses native objects. Failures return20 and retain source/object progress.
- `check`: syntax-checks chrome modules and runs 20 deterministic checks: five Gecko fixtures, six engine-control fixtures, and nine bootstrap/security tests.
- `ready` / `describe`: return0 plus the verified executable only when a custom isolated bundle and matching source fingerprint exist; otherwise20.
- `run --profile <absolute profile>`: checks `ready`, verifies the exact root-dev profile namespace and `.axiosozo-dev-profile` marker, then `execv`s the custom native executable. Root `./dev` owns the lock, PID/process group and shutdown. No other app or existing personal profile is a fallback.
- `smoke`: the component command still returns20 until its GUI harness is verified. Root `./dev smoke` owns the actual fixture and locked-profile launch. The command never fabricates a GUI PASS from unit tests.

The root-dev namespace is `/Volumes/AxioSozoBuild/runtime/<sha256(real_project_path)[:16]>/<profile-name>/gecko`. Root daily launch reuses a valid build. A changed owned chrome file or patch invalidates the stamp and requires `./dev setup`, then a controlled restart. No C++ hot reload or upstream watcher has been verified. A faster `surfer build --ui` loop can be added only after a successful native build demonstrates it.

## Verification and remaining acceptance work

Native setup returned0 after two narrow source corrections, retaining all previously built objects. `ready` returned0 with the matching source fingerprint; direct file/Info.plist inspection confirmed the arm64 app identity. Nine packaged chrome modules/resources match their authoritative source. The post-build source check returned0 with 20 passing checks. [Native report](evidence/zen-native-build-verified.json), [source check](evidence/zen-check-built-app.log), [full native output](evidence/zen-native-build-native-share.log.gz).

Still unverified: a second clean reconstruction from an empty output volume, a saved integrated GUI screenshot, physical Retina/window resize, full CEF safety/accessibility matrix and general-site engine switching. Two GUI launches/shutdowns, Gecko fixture input, a certificate warning and a local-fixture CEF switch were observed by the integration lead. The development executable is linker ad-hoc signed; its resources are not sealed and no distribution-signing claim is made.

Handoff2 should enter at `apps/browser/chrome/AxioSozoStartup.mjs`, `GeckoEngineAdapter.sys.mjs`, the maintained patch manifests, and `scripts/zen.py`. The integration lead can launch the verified app through root `./dev` with an owned profile; coordinator and optional fixture-only CEF bridge are packaged for real runtime verification. Keep provider settings and engine experiments in privileged chrome. Daily Chromium use remains unjustified until the required native safety UI and input/accessibility tests pass.

The pinned Surfer CLI/build audit confirms `npm run build -- --jobs 2` is accepted and becomes `mach build -j2`. Surfer consumes stage-root `mozconfig` between common/OS config and its internal branding config. Its `getCurrentBrandName()` uses the custom `brandShortName`, while pinned Firefox derives the native app name from the branding display name; both are `AxioSozo Dev`. These are source-level checks, not a native build result. See [audit](evidence/zen-surfer-build-audit.json).

## Resumed native setup (2026-09-22)

The lead allocated a project-owned 200GiB sparse APFS image on T9 (since 13 October 2026 replaced by the `AxioSozoBuild` volume inside `/Volumes/T9/Dev.sparsebundle`; there is no separate image any more). New setup invokes the required global `dev-external` helper followed by `scripts/storage.py exec -- ...`, which verifies the T9 backing and routes all command temporary/cache/Cargo data into this project volume. `scripts/zen_toolchain.py` extracts checksum-pinned Node and standalone Python and installs only Rust compiler/arm64 standard library/Cargo and GNU tar to this project's `toolchains/zen` prefix. It does not alter global Node/Rust/Homebrew installations or user shell files.

Node, Rust and Python archives were downloaded, rehashed and extracted successfully; see [actual archive verification](evidence/zen-toolchain-archive-verification.json) and [fetch log](evidence/zen-toolchain-fetch.log). At this checkpoint no installer or browser build has run. The Rust installer was inspected: its explicit `--prefix` scopes binaries, manifests and component replacement; `--disable-ldconfig` excludes system linker-cache actions; the selected component names match the archive's `components` file. GNU tar's source installer still requires a recorded audit hash before setup executes it. This checkpoint is not a native build or launch PASS.

`0003-locked-anonymous-bootstrap.patch` retains upstream's locked Rust ffprefs dependency graph and changes a lockfile Git dependency's transport from SSH to anonymous HTTPS while retaining its exact commit. npm lifecycle hooks remain disabled. Interrupted verified archive downloads resume; archive extraction and component installation use scoped receipts and checksums. An unknown pre-existing toolchain destination is rejected rather than overwritten.

Chrome integration checkpoint: `BrowserCoordinator.sys.mjs` and the provider settings module/XHTML/CSS are included in the owned JAR manifest. The startup badge opens provider settings on a user command; this was observed in the actual GUI. Coordinator attachment is asynchronous, uses no web listener and exposes no token; ordinary browsing survives failure. The resumed [12-test check](evidence/zen-check-resumed.txt) includes the unowned toolchain-directory preservation test.

## Explicit Chromium fixture action

`EngineProbeControls.sys.mjs` exposes a toolbar action only when the browser process has `AXIOSOZO_ENGINE_PROBE=1` and a valid `AXIOSOZO_ENGINE_FIXTURE_ORIGIN=http://127.0.0.1:<port>`. No CEF process or presenter is constructed merely by startup or by enabling the probe. The explicit action requires a browser-owned nonprivate Gecko target whose identity matches that origin and URL is `/engine.html` (optionally `?page=2`). It lazy-constructs `CEFPresenter`, which checks again and preserves the original Gecko browser until CEF reports a valid native fixture load and frame. The badge updates its engine identity only from the presenter callback; it continues to open provider settings.

Root launch API: `python3 scripts/zen.py run --profile <owned-runtime>/<profile>/gecko --url http://127.0.0.1:<port>/engine.html`. Root supplies the probe flag, fixture origin, `AXIOSOZO_BUILD_ROOT`, and exact `AXIOSOZO_CEF_BINARY=<build-root>/cef/AxioCEFProbe.app/Contents/MacOS/AxioCEFProbe`. `CEFEngineAdapter.sys.mjs`, `CEFPresenter.sys.mjs` and the controls are packaged under `chrome://browser/content/axiosozo/` in the existing privileged browser JAR.

Privileged GUI verification entrypoints are `window.AxioSozo.engineProbe.switchToChromium()`, `.switchToGecko()` and `.diagnostics()`. The facade exists only in the gated development mode; no content DOM event bridge is installed. Diagnostics include actual presenter frame counters/timing when available, pending/failure status and the logical targets currently owned by CEF. They do not imply a native PASS before that runtime executes. The visible development action and diagnostics list unavailable IME, clipboard, native accessibility, downloads, permissions and automatic Chromium control.

The coordinator still authorizes Gecko targets only. `engineProbe.isGeckoTargetActive(target)` synchronously denies hidden retained Gecko actions before native launch and throughout pending/active CEF ownership, including when a different Gecko tab is selected. Startup passes that predicate to `attachCoordinator`, which checks before authorization and immediately before dispatch. CEF events are not registered into the Gecko registry. Tested transactional registry migration is required before automated Chromium control can be enabled.

Six new synthetic source regression tests cover normal-mode inactivity, untrusted command rejection, private/foreign URL rejection before native hooks, preservation of the Gecko tab on component failure, single-owner switching and retained-target authorization. The complete Zen source check is now18 tests (5 Gecko +6 controls +7 Python); see [actual output](evidence/zen-check-resumed.txt). This work performed no new-volume operations and supplies no E0/E1/E2 runtime evidence.

## Native configure and reproducible import (2026-09-23)

The first real repeated setup exposed a Surfer bug: reversing each patch before
applying it fails with overlapping patches. A deletion-only patch can even reverse
against pristine source and then undo its own insertion, leaving the intended
change unapplied. `scripts/zen_import.py` now reconstructs all 256 patches in their
actual order against pristine Git blobs in a separate temporary directory, verifies
all 266 resulting files, and records their hashes. Repeated setup verifies the
receipt and preserves unexpected edits. The small reviewed
`0004-surfer-forward-import.patch` makes the first upstream import forward-only;
subsequent imports refresh known source symlinks without replaying patches.
The actual missing pristine `aiFeatures.mjs` change was repaired using only its
existing pinned Zen patch, then the full comparison passed. No reset or clean was
used. [Verification](evidence/zen-import-verification.json),
[20 source tests](evidence/zen-check-native-preparation.log).

Xcode 26's linker no longer prints the legacy string used by this Firefox revision
to detect Apple ld. `0005-firefox-xcode26-linker-detection.patch` recognizes its real
`@(#)PROGRAM:ld PROJECT:ld-...` version header. It does not treat a generic failure
as success, alter linker protections, or disable sandboxing. The subsequent actual
native compiler and linker checks pass.

Mozilla cbindgen 0.29.4 is built with its published crate checksum and unchanged
Cargo.lock. Its audited build script only generates test macro files inside
`OUT_DIR`; setup builds its CLI without running its tests or a global installer.
[Pin and provenance](evidence/zen-cbindgen-pin.json).

Xcode omits the wasm target required for Firefox's additional library isolation.
The official [WASI SDK 34 release](https://github.com/WebAssembly/wasi-sdk/releases/tag/wasi-sdk-34)
supplies a checksum-verified Apple Silicon package. Only `WASM_CC`, `WASM_CXX` and
`WASI_SYSROOT` use it; native C/C++ remain on Xcode. All six upstream wasm-sandboxed
libraries remain enabled. Real C/C++ wasm compiler, header and link tests passed
with its Clang 23.1.0. The Rust wasm target reference in this source is generic
triplet resolution; this browser's Rust host and target remain native arm64.
[WASI pin](evidence/zen-wasi-pin.json), [actual configure/build log](evidence/zen-native-build-wasi.log.gz).

Surfer reported a newer upstream Firefox 156.0.1 during this run. The current Zen
revision still selects 156.0; the build never silently changes that pin. Review and
test the corresponding Zen/Mozilla update before treating this development build
as an up-to-date daily release.

## First native compile correction (2026-09-23)

The actual build log reported `missing documentation for a variant` for Zen's
`QueryExpressionValue::String(AtomString)` at 00:07:31. Firefox's style crate
correctly enforces `deny(missing_docs)`. The continue-on-error native Make process
was still compiling independent C++ targets; the owned build group was inspected
(PID=PGID38085), sent SIGINT, and checked to contain no remaining processes.
The command ended130; this was not a successful build. Source and object output
were preserved.

`0006-zen-query-string-documentation.patch` adds exactly one documentation comment.
No lint or security check is disabled. The post-import manifest pins exact before
and after hashes. Import verification recognizes an explicitly maintained
post-import result only when its before hash equals the independently verified
Zen result; unknown edits and mismatched baselines still fail and remain intact.
The existing real overlapping-patch test now exercises these positive and
negative cases. [20 passing source tests](evidence/zen-check-query-documentation.log).
The incremental continuation is recorded in
`docs/evidence/zen-native-build-query-documentation.log.gz`; it is not yet a finished
application or GUI proof.

The corrected Rust build completed its release library phase successfully in
4m31s. The next native failure was in Zen's macOS share function: `rv` from
`GetSpec` was used only by a debug-only assertion and therefore unused in the
normal build. `0007-zen-native-share-uri-error.patch` returns a failed `nsresult`
before constructing the native URL; it preserves success behavior and does not
suppress compiler diagnostics. A full error scan found no other new compiler
failure. The owned second build group72128 was stopped, verified empty, and
session71287 ended130 before the incremental third compile began. The 20 source
checks passed again: [output](evidence/zen-check-native-share.log). The successful
completed log is `docs/evidence/zen-native-build-native-share.log.gz`.

## Native build completion

The final native run (exec session60965) completed with exit0 and upstream's
`Your build was successful!` at 00:13:19. It produced
`/Volumes/AxioSozoBuild/zen/obj/dist/AxioSozo Dev.app/Contents/MacOS/axiosozo-dev`.
`ready` verified bundle identity and the exact current build fingerprint. Both
that executable and `Contents/MacOS/XUL` are arm64 Mach-O binaries. All nine owned
chrome files were checked byte-for-byte against source after bundle assembly.
These checks are recorded in [native verification](evidence/zen-native-build-verified.json).

The three large completed logs are stored losslessly as `.log.gz` files.
[Their index](evidence/zen-native-build-log-index.json) records original and
decompressed SHA256, compressed SHA256, byte counts, session IDs, exact command
exitcodes, and compiler-error/completion line excerpts. Raw files were removed
only after decompression matched their original hash. This is real compile
evidence, separate from the integration lead's subsequent GUI acceptance work.

## Generated JAR manifest (Handoff 3, 27 September 2026)

The three explicit `jar.inc.mn` overlay records were replaced by one
hash-guarded record that inserts `#include axiosozo/jar.inc.mn`; the previous
results moved to `retired.json`. `scripts/zen.py` generates
`src/zen/common/axiosozo/jar.inc.mn` from the packaged file list: every
`.mjs/.js/.xhtml/.html/.css` under `apps/browser/chrome/**` (including
`overview/`) plus `packages/contexts/src/*.mjs` as `contexts/`, limited to files
git lists as tracked or untracked-not-ignored, refusing symlinks. After
`mach build`, `materialize_axiosozo_resources()` copies every packaged file into
the dev bundle as a real file (the `privilegedabout` content process cannot
follow the stage symlinks) and fails if one is missing. The first native build
confirmed the nested `#include` resolves; 40 files were packaged. A chrome-only
rebuild takes about 70 seconds.
