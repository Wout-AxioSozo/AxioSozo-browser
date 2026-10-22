# CEF component and boundary

This directory is an **E0 component probe**, not an alternative browser application and not proof of Zen embedding. The production shell stays Zen. `probe.py run` reports only what the real native CEF process observed, and always labels E1/E2 separately.

```sh
# Run mount-dev-storage first; prefix build/run commands with:
# /Users/wout/.local/bin/dev-external python3 scripts/storage.py exec --
python3 native/chromium-host/probe.py setup
python3 native/chromium-host/probe.py check
python3 native/chromium-host/probe.py run
python3 native/chromium-host/probe.py test-native
python3 native/chromium-host/stream_test.py
python3 native/chromium-host/stream_security_test.py
# General HTTP(S) capability, still an isolated native component test:
python3 native/chromium-host/web_stream_test.py
```

The root `./dev engine-probe` owns combined acceptance. Native compiler and signing commands have a ten-minute limit and report `BLOCKED_ENV` on timeout; they clean only their owned process groups. Every new run archives previous real logs and frames under `docs/evidence/cef-run-*`, and clears current images so a failed run cannot display stale evidence. The component's first setup downloads a pinned official macOS arm64 minimal archive, verifies its SHA256, and compiles our Objective-C++ C API host with Xcode. It uses the unchanged CEF scoped sandbox helper source from that archive. This avoids installing CMake or compiling the entire C++ wrapper. Build artifacts and profiles live in `$AXIOSOZO_BUILD_ROOT/cef`; the build root must be the mounted project APFS volume, default `/Volumes/AxioSozoBuild`. Compiler temporary files and module caches are overridden after `dev-external` to remain on that fresh volume. The archive is kept under its versioned upstream name, so a previous pin's archive and extraction stay in place and are never reused; each rebuild re-clones the framework from the current pin. The host bundle ID is `dev.axiosozo.cef-probe`; it never sets the default browser or opens a personal profile.

The process uses real CEF off-screen BGRA frames at 2× device scale and native input calls. It renders `tests/fixtures/engine.html`, types synthetic text, resizes, scrolls, navigates to another local GET URL and goes back, then closes through CEF before shutdown. PNGs are encoded directly from CEF `on_paint` bytes; they are actual render output, not screenshots of another browser. The process has no visible product window. This rendering test does not prove native IME, selection, clipboard, accessibility or UI integration in Zen.

The helper initializes CEF's current scoped macOS sandbox before loading the main framework. The main host configures `no_sandbox=0`, no debugging port, and no arbitrary command-line switches. An exact fixture URL allowlist is applied at launch and for stream sessions that omit `browsing_mode`. There is no content-to-privileged bridge. General HTTP(S) mode is explicit and experimental, with the unsupported capabilities listed below.

`setup` reviews/uses no upstream lifecycle hooks, creates local code signatures only (ad hoc, or your own local identity, see "Stable local signing identity"), and preserves the required framework resources and license files. The diagnostic bundle includes English UI resources only, as permitted by the pinned distribution README; the full verified archive retains every upstream locale. ICU remains present for Unicode page content. Binary and source pins are in the root lockfile (`docs/evidence/cef-upstream-manifest.json` keeps the original 154.0.23 index entry as history). A changed local host or toolchain invalidates the build fingerprint, requiring setup. No native hot reload is claimed.

A probe invocation owns its one child process group and unique profile. Timeout/interruption terminates only that group; no `pkill` or global client discovery is used. Successful native shutdown is recorded as `closed` before the result. Profile directories are retained externally for repeatable diagnostics and can be explicitly removed after their process has exited; they contain only synthetic fixture activity.

`AxioCEFProbe --stream PRIVATE_PROFILE EVIDENCE_DIR` implements the native side of `contracts/cef-v1.md`. Authentication arrives through the owned pipe before CEF initializes. `transport.hpp` carries binary BGRA frames on a dedicated bounded writer; `stream.inc` validates target-bound commands and uses the same live native browser for frames and input. Chromium helpers do not inherit these pipes. The stream extension is compiled and signed, and actual private-pipe frames, native text input and Retina resize have passed on the explicitly approved T9-backed project volume. `docs/adr/002-dual-engine.md` tracks component results and remaining acceptance gates. `test-native` tests only transport mechanics, and `stream_test.py` tests the real component; neither proves E1/E2 inside Zen.

Stream `hello` contains `version`, `method`, `token`, `engine_instance`, `fixture_origin`. Later commands contain `version`, `method`, `request_id`, `token`, `target` plus method-specific fields. `shutdown` omits `target`. `create` receives the pending target without `engine`/`native_target_id`; `created` returns those actual runtime fields. `frame_ack` releases the exact outstanding `{frame_id,target}` even if current navigation advanced, and emits no acknowledgement event. Keyboard fields are `type` (`down`, `up`, `char`), `native_key_code`, `windows_key_code`, `modifiers`, `text`. Mouse fields are `type` (`move`, `down`, `up`), `x`, `y`, `modifiers`, `button`, `click_count`, `mouse_leave`; wheel uses `x`, `y`, `modifiers`, `delta_x`, `delta_y`. Coordinates are logical points; resize includes explicit `device_scale`. Oversized physical surfaces return `unsupported` instead of silently reducing Retina scale.

The host owns CEF's official macOS message loop; a bounded timer dispatches private commands on the UI thread. `cef_key_event_t.size` is set according to the pinned API. Accepted navigation revokes old document/navigation generations even for BFCache restores that skip `OnBeforeBrowse`. The `load` event carries the actual `http_status` plus required `restored_from_history:boolean`; HTTP0 alone never authorizes a frame. Only explicit history restoration to a known successful fixture document can enable subsequent actual paints.

## GPU surface transport (Mach + IOSurface)

When `hello` carries `surface_service` + `surface_token` (see
[`contracts/engine-surface-v1.md`](../../contracts/engine-surface-v1.md)), the host
connects to Zen's checked-in Mach port before CEF starts, creates browsers with
`shared_texture_enabled` (and `external_begin_frame_enabled` when
`surface_begin_frames:true`), and handles `OnAcceleratedPaint` instead of `OnPaint`.
CEF's pooled IOSurface is copied by an awaited Metal blit into a host-owned ring of
up to six lazily allocated **global** IOSurfaces per target (the pool buffer returns to
viz when the callback returns, so it is never forwarded; ring surfaces are global so
Zen composites them without a second copy, flag `AXIO_SURFACE_FLAG_GLOBAL_SURFACE`), select popups are composited on the GPU into the
same surface, and the ring surface's Mach send right plus fixed binary metadata goes
to Zen. Zen releases frames with RELEASE; BEGIN_FRAME ticks drive
`send_external_begin_frame`. Control and input stay on the JSON pipe; input now wakes
the UI thread through the main dispatch queue instead of waiting for the 10 ms timer.
Without `surface_service` the AXCF BGRA pipe remains the only frame path. Optional
`frame_rate` (60 default, or 120) sets `windowless_frame_rate` for both paths; the
per-target `frame_rate` command changes it live. Surface mode admits up to 4096 px per
side without the pipe's 32 MiB cap (`surface_test.py` resizes to 3600×2400).

Crash robustness (`crash_guard.hpp`): before CEF starts the host clears the task
exception ports it inherited from Zen (Breakpad ignores a child's fault, which parked a
faulting host instead of letting it die) and, once it decides to end, a watchdog bounds
the exit (5 s; 25 s for a requested shutdown; also on parent death) by SIGKILLing its own
direct children and `_exit`ing. `probe.py test-native` runs `tests/crash_guard_test.cc`.

Files: `engine_surface_v1.h` (wire structs), `surface_transport.hpp/.mm` (Mach +
Metal, main binary only; helpers compile inert stubs and do not link Metal or
IOSurface), `tests/surface_receiver.mm` (standalone Zen stand-in) and
`surface_test.py` (builds the receiver, serves a synthetic animated fixture, runs
internal/external pacing at 60/120 Hz, a pipe baseline and two negative cases).

```sh
/Users/wout/.local/bin/dev-external python3 scripts/storage.py exec -- \
  python3 native/chromium-host/surface_test.py            # --only CASE[,CASE] --seconds N
```

Known environment gate: after every rebuild, Chromium's **teardown** reads the
"Chromium Safe Storage" Keychain item for the new ad hoc cdhash; macOS waits for the
user and Chromium's 10 s teardown watchdog (`chrome/browser/shutdown_watchdog_mac.cc`)
exits 2. The test samples the owned host and reports
`PASS_TEARDOWN_KEYCHAIN_BLOCKED` only when the sample proves that wait. Nothing
approves the dialog. The same stall now also makes `stream_test.py` time out at its
final exit check.

## Heartbeat

Stream mode announces `capabilities.heartbeat_interval_ms:1000` and, once `ready` was sent,
emits `{"event":"heartbeat","sequence":N}` every second from the CEF UI-thread timer in
`host.mm` (strictly increasing, no event while closing, no catch-up after a stall). The
Zen adapter fails a host that is silent for 5 s as `CEF_HOST_UNRESPONSIVE` (see
`contracts/cef-v1.md` "Heartbeat"). `stream_test.py` asserts ~1 s cadence with increasing
sequences, then SIGSTOPs its own host child, confirms no heartbeat is produced, SIGCONTs
it and checks that beats resume before the normal close.

## Input: key verdicts, IME, wheel phases, pinch (`input.inc`)

`ready.capabilities` adds `key_verdict`, `ime`, `wheel_phases`, `pinch` and
`frame_rate_command` (all `true`). The mechanisms and exactly what Chromium receives
are specified in "Input extensions" in `contracts/cef-v1.md`. In short: a keydown's
verdict comes from CEF `OnKeyEvent` plus an inert system-key char probe that Blink
never dispatches; IME uses CEF `ime_*` and a renderer-helper focused-node report for
`text_input`; wheel phases cannot be passed through CEF's C API (Chromium synthesizes
its own); pinch is a synthesized two-point touch sequence (visual-viewport zoom).

```sh
/Users/wout/.local/bin/dev-external python3 scripts/storage.py exec -- \
  python3 native/chromium-host/input_test.py
```

`input_test.py` serves a synthetic page that records keydown/keypress/composition/
wheel/touch/visual-viewport state in its title and checks: verdicts (typing
`not_consumed`, a `preventDefault` key `consumed` with its keypress suppressed, ⌘L
`not_consumed`), probe invisibility, IME set/commit/underlines/finish/cancel,
`text_input` text/password/none and composition caret, phased wheel deltas and
scroll, pinch zoom and `frame_rate`. Functional checks are asserted before teardown.

## Stable local signing identity

Ad hoc signatures have a designated requirement of `cdhash H"..."`, which changes on
every rebuild, so macOS asks about the "Chromium Safe Storage" Keychain item again each
time. With a local code-signing certificate the requirement becomes
`identifier "..." and certificate leaf = H"<cert sha1>"`, which survives rebuilds.
`probe.py` only reads (`security find-identity -v -p codesigning`); it never creates
certificates or touches Keychain entries.

1. Keychain Access, menu Keychain Access, Certificate Assistant, **Create a Certificate...**
   Name `AxioSozo Local Development`, Identity Type **Self Signed Root**, Certificate Type
   **Code Signing**. Create it in the login keychain.
2. Double-click the certificate, expand Trust, set **Code Signing** to **Always Trust**
   (this is what makes `find-identity -v` list it as valid).
3. Verify: `security find-identity -v -p codesigning` shows the name.
4. Rebuild and check:

```sh
export AXIOSOZO_CODESIGN_IDENTITY="AxioSozo Local Development"   # or the 40-character SHA-1
/Users/wout/.local/bin/mount-dev-storage
/Users/wout/.local/bin/dev-external python3 scripts/storage.py exec -- python3 native/chromium-host/probe.py setup
/Users/wout/.local/bin/dev-external python3 scripts/storage.py exec -- python3 native/chromium-host/probe.py check
```

`check` prints the signing mode, runs `codesign --verify --strict --deep` and shows the
designated requirement (it should contain `certificate leaf`, not `cdhash`). Changing or
unsetting the variable changes the build fingerprint, so the next `setup` re-signs. macOS
may ask once whether `codesign` may use the certificate's private key; that is your own key.

**First run.** The Safe Storage item is created and read by this host for its synthetic,
in-memory dev profile, not by your personal Chrome. If the dialog names `AxioCEFProbe`
(the host), choose **Always Allow**; later rebuilds signed with the same certificate will
not ask again. Caveat: the pinned CEF 156 can name the Keychain item per app, but the
host does not set those names yet (see "Explicit web sessions"), so the item is still
the generic **"Chromium Safe Storage"**. A personal Chromium build
using that same item name could be the one the dialog refers to. If the dialog names any
other app or your Chrome, or you are unsure, choose **Deny**; the host tolerates a denial
apart from the teardown stall described above. Nothing in this repo answers the dialog.

## Explicit web sessions

A trusted browser can add `browsing_mode:"web"` to `hello`. `fixture_origin` then
names the validated HTTP(S) session identity; it is stable for that owned process,
not a page-origin permission grant. Existing fixture sessions retain their exact
origin/path/GET restrictions. A web session permits credential-free HTTP(S), normal
web subresources and form submission, and the inert initial `about:blank` page.
External protocols, file navigation, native popup windows, client-certificate
selection and camera/microphone/screen capture remain unavailable. Context menus,
JavaScript dialogs (including beforeunload), permission prompts, file dialogs,
downloads, HTTP authentication, blocked pop-ups, find and zoom are delegated to Zen,
which draws them with Firefox's own UI; each decision is bound to a host-issued
`prompt_id`, the exact target and its generations, is single use, and times out or
is cancelled on navigation/close with the safe default (see "Browser UI delegation"
in `contracts/cef-v1.md`). TLS errors are denied by CEF and reported without any
certificate exception path. Fixture sessions keep suppressing all of these. Native HTML select dropdowns
are separate from popup windows and their bounded OSR overlay is composited into
the same tab surface.

`ready.capabilities` reports these limits, including
`accessibility:false`, `private_mode:false`, `devtools:false`, `popups:false` and
`media_capture:false`; web sessions report `context_menu`, `javascript_dialogs`,
`permissions`, `file_dialogs`, `downloads`, `http_auth`, `find`, `zoom` and
`popup_blocked` as `true` (all `false` for fixture sessions). Gecko remains the
fully featured engine for those workflows. `edit` dispatches an explicit trusted
`copy`, `cut`, `paste`, `select_all`, `undo` or `redo` to CEF's focused frame; it never
returns clipboard content across the pipe. `visibility` with a boolean `visible`
calls CEF `was_hidden` so hidden tabs stop OSR painting. No CEF process is needed
for tabs that stay in Gecko. Web request IDs are strictly increasing `cef-N` safe
integers, giving constant-memory replay rejection without the fixture session's
100,000-request lifetime limit.

HTTP200–599 documents can render in web mode, so a real HTTP404 page is visible.
HTTP0 requires known history restoration, exact `about:blank`, or the explicit
`same_document:true` marker for a retained same-origin document. A `load` is only
reported for the main-frame RenderFrameHost whose `OnLoadStart` (commit) followed the
current navigation; a previous document finishing late, such as the committed TLS
interstitial of a load `OnLoadError` already failed, never completes, authorizes or
revokes the current request (it could otherwise fail the next navigation). SPA/fragment
navigation revokes target generations before its URL/frame is exposed. A renderer
crash revokes the target and requires explicit user recovery; unsafe form requests
are never automatically replayed.

Each target now uses an explicit non-global CEF request context with empty
`cache_path` and a unique owned `root_cache_path`: page cookies, cache and storage
are in memory and are not imported from Firefox or another Chromium profile.
**This does not avoid Chromium's process-global OSCrypt initialization.** On
2026-09-26 the newly signed host reached a real blank OSR frame, then stalled before
the first HTTP request waiting inside macOS `SecItemCopyMatching` for the default
Chromium Safe Storage item. The default and explicit in-memory contexts both hit
this boundary. See `docs/evidence/cef-web-stream-cwqgbeis/result.json` and the owned
process sample `docs/evidence/cef-owned-stall-sample.txt`. Those runs are failures,
not passing web or E1/E2 evidence. Do not approve a Keychain prompt automatically.

Official CEF added per-app Keychain service/account names in
[commit fa874ac](https://github.com/chromiumembedded/cef/commit/fa874acb9dd4a0ebf607245543b2f20d27d04916).
The pinned 156.0.7 exposes them as `cef_settings_t.keychain_service_name` and
`keychain_account_name` (API version 15600; empty keeps Chromium's default item).
The host deliberately leaves them empty for now: a different name creates or reads a
different Keychain item, so data encrypted under the current item (the persistent web
profile) would become unreadable. A product-specific name would stop the host from
ever touching a personal Chromium's "Chromium Safe Storage" item, but with ad hoc
signing each rebuild would still be a new client of its own item and still prompt;
production isolation needs both a planned name migration and stable signing. No mock Keychain, basic password store, TLS
bypass, personal-profile access, or system-Keychain mutation is implemented here.
An explicitly authorized user may respond to macOS themselves during an isolated
fixture run. `web_stream_test.py --interaction-timeout 60` waits longer for that
response; the flag itself is **not** authorization to access Keychain.
