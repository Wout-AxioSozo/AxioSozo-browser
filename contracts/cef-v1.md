# Chromium native transport v1

This contract connects a trusted privileged Gecko adapter to an owned CEF OSR child.
It is never exposed to web content or a provider. This specification alone is not
runtime evidence; dated E0 and manual local-fixture E1/E2 results are recorded under
`docs/evidence/`, with full E1 acceptance still open.

## Process and authentication

In web mode the browser runs one native host per Firefox process and Zen profile,
shared by every browser window; each Chromium tab is one target of that host. The
strict fixture probe keeps one host per target. Both use inherited stdin/stdout.
The first stdin message has keys `version:1`, `method:hello`, a browser-generated
256-bit hex `token`, an `engine_instance` and `fixture_origin`. An explicit
`browsing_mode:"web"` adds normal HTTP(S) browsing; omission retains the strict
fixture policy. In web mode `fixture_origin` is a stable process/session identity
(`https://axiosozo.invalid`), not the current website origin. Current page identity shown to the user comes
from the native navigation URL. No provider browser grants use this session label.
The secret is
delivered only through the private pipe, never argv, environment, files or logs.
All subsequent commands echo the token and carry a unique browser-issued `request_id`.
Their envelope is `{version,request_id,token,method,target,...methodFields}`;
`shutdown` omits target. The native host duplicates control pipes to CLOEXEC
descriptors and redirects inherited helper standard streams before CEF starts,
so sandboxed renderers cannot inherit the authenticated parent channel.
Unknown versions/keys, malformed input, missing hello, wrong tokens and overlong
lines close the channel. No TCP, WebSocket, debugging port or automatic reconnect.
The parent owns the child and drains its stderr. The native host exits on stdin EOF
or `shutdown`, closing every target and its helpers. Closing one web target never
ends the host; a fixture host ends with its only target.

A web host keeps one persistent Chromium profile (`<session>/chromium`, mode 0700)
beside the Zen profile `<session>/gecko`. It holds Chromium's own cookies, cache,
storage and history; nothing is imported from or shared with Firefox. Chromium
encrypts that data with its macOS Keychain item "Chromium Safe Storage", which the
user approves once in the macOS dialog (again after each ad hoc host rebuild). The
host never answers that dialog. Fixture hosts keep all page data in memory, but
still reach that Keychain item; see "Keychain gate" below.

## Input

Commands are UTF-8 JSON lines, at most 16 KiB including the newline. Native admission
and execution are distinct events. Commands are deduplicated; a crash or uncertain
outcome is never automatically replayed. Strict fixtures allow at most 100,000
unique requests. Web mode requires monotonically increasing `cef-<integer>` IDs,
from 1 through JavaScript's maximum safe integer; this rejects replay in constant
memory without expiring animated pages after an hour of frame acknowledgements.

`create` receives a pending browser-owned target: logical `tab_id`, `engine_instance`,
`identity`, `document_generation`, `navigation_generation` and `private_mode:false`.
A web target's `identity` is the origin it was created with (Zen creates every web
target at `about:blank` with the inert session identity, then navigates). A host
serves at most 32 targets, creates one at a time, and rejects a `tab_id` that is
already live. CEF emits its actual `native_target_id` when the browser is created. Subsequent
commands bind the complete BrowserTarget from `ipc-v1.schema.json`, including
`engine:chromium`, and reject any stale field before native dispatch.

Commands: `create`, `navigate`, `back`, `forward`, `reload`, `stop`, `resize`, `focus`, `key`,
`mouse`, `wheel`, `pinch`, `ime_set_composition`, `ime_commit_text`, `ime_finish_composing`,
`ime_cancel_composition`, `frame_rate`, `frame_ack`, `visibility`, `edit`, `close`,
`shutdown`, plus the web-only
browser-UI replies and commands in "Browser UI delegation" below. `close`
ends only its target. A new navigation supersedes one still loading: the earlier
request completes `unsupported` with reason `NAVIGATION_SUPERSEDED`. Navigation
requests have no parent-side deadline; they complete on load, error or replacement. The adapter declares capabilities
and returns `unsupported` for unavailable operations; `devtools` must not silently
succeed. Input coordinates use logical content points plus an explicit device scale.
Keyboard fields are `type:down|up|char`, `native_key_code`, `windows_key_code`,
`modifiers`, and `text`. Mouse fields are `type`, `x`, `y`, `modifiers`, `button`,
`click_count`, and `mouse_leave`. Key verdicts, IME, wheel phases, pinch and frame-rate
renegotiation are specified in "Input extensions" below.

`visibility` carries `visible:boolean` and maps to native `was_hidden`. Hidden
tabs/windows stop presenting frames. `edit` accepts only `copy`, `cut`, `paste`,
`select_all`, `undo`, or `redo`, dispatched to the actual focused CEF frame from
trusted user keyboard actions. These commands retain full target validation.

The first integrated experiment allows only the explicitly passed loopback fixture
origin and GET navigations. Deny popups, external schemes, unapproved origins, file
URLs, downloads and permissions. Preserve certificate validation and CEF sandbox.
Private Chromium mode is unavailable until isolation is proven. These restrictions
must be visible in the development UI, not mistaken for general browsing support.

Web mode permits normal HTTP(S) URLs and subresources, plus an empty `about:blank`
start. Forms submitted inside Chromium remain Chromium operations. An explicit
switch carries only the visible address, as if the user retyped it: an HTTP(S)
URL without credentials whose Gecko history entry has no POST data. It never
transfers cookies, storage, history or page state, and never resubmits a form;
anything else starts Chromium blank.
Certificate validation remains native and has no bypass command. Context menus,
JavaScript dialogs, permission requests, file selectors, downloads, HTTP
authentication, blocked pop-ups, find and zoom are delegated to Zen's own UI (see
"Browser UI delegation"); fixture sessions keep failing them closed with explicit
native diagnostics. A user-gesture link that targets a new tab or window
never creates a native window: the host emits `open_url` (`url`, `background`) and
Zen opens a new Chromium tab; other pop-ups are denied. OSR select/autocomplete popup pixels
are composited into the same frame stream; they are not external browser windows.
Web mode remains experimental. Developer tools are unavailable. The Chromium
accessibility tree is exposed to macOS assistive clients (VoiceOver) with the
limits in [engine-accessibility](../docs/design/engine-accessibility.md) (see
"Accessibility" below; EXPERIMENTAL, no VoiceOver E1 yet). Adding HTTP(S)
navigation is not full E1/E2 certification.

## Input extensions

`ready.capabilities` announces each extension (`key_verdict`, `ime`, `wheel_phases`,
`pinch`, `frame_rate_command`, all `true` on the current host); a client sends the
extension fields and methods only when announced. Optional fields are admitted by
exact name; any other key, an out-of-range value or a wrong type is a protocol error.
All offsets are UTF-16 code units.

**Key verdicts.** A `key` `down` completes `success` with `reason:"key_consumed"` or
`reason:"key_not_consumed"`; `up` and `char` complete without a reason. Chromium acks
keyboard events in order per widget and passes only unconsumed ones to
`CefKeyboardHandler::OnKeyEvent`. After each keydown (and after its own `char`, or
before the next command of that target, or after 30 ms) the host sends one inert
probe: a `KEYEVENT_CHAR` with `is_system_key` set, which Blink answers "not handled"
before any DOM dispatch (`WebFrameWidgetImpl::HandleCharEvent`, Chromium 154). A
keydown whose unhandled report arrives before its probe is `key_not_consumed`; one
whose probe arrives first was consumed (page `preventDefault`, editing command, focus
move). The probe waits for the keydown's `char` because it also clears Blink's
suppress-next-keypress flag. No answer within 500 ms, a closed target, or an open
select/date popup (no probe is sent while a popup is shown) completes
`key_not_consumed`. Nothing blocks the UI thread. For non-text keys with empty
`text` (arrows, Escape, Return, F-keys) the host supplies AppKit's `[NSEvent
characters]` value, because CEF's macOS translator turns an event with no characters
into a flags-changed (modifier) event.

**IME** (`ime:true`), bound to the focused target:

| method | fields (optional in brackets) |
| --- | --- |
| `ime_set_composition` | `text` (≤ 1024), `selection_start`, `selection_end` (0 ≤ start ≤ end ≤ text length), [`underlines`: ≤ 16 × `{start, end, thick}` within `text`], [`replacement_range` `{start, end}`] |
| `ime_commit_text` | `text`, [`replacement_range`], [`relative_cursor_pos` (\|n\| ≤ 65536)] |
| `ime_finish_composing` | [`keep_selection` boolean, default false] |
| `ime_cancel_composition` | — |

An omitted `replacement_range` is CEF's invalid range (current composition or
selection). Omitted `underlines` means one thin solid underline in the text colour.
The host emits `text_input {mode, caret_x, caret_y, caret_width, caret_height}` in
logical points of the target view, `mode` ∈ `none`, `text`, `password`. The renderer
helper reports focused-node changes (editable, password input, element bounds) with
one bounded process message; the host validates and clamps it and, until composition
reports real character bounds, uses a one-line caret at the start of the field.
During composition `OnImeCompositionRangeChanged` character bounds give the caret.
Events are sent only on change and at most about 60 per second per target. A new
document resets the mode to `none`. Focus inside out-of-process iframes is not
reported (CEF's renderer focus callback runs only for the main frame tree).

**Wheel phases** (`wheel_phases:true`): `wheel` may add the group `phase`,
`momentum_phase` (each `none`, `may_begin`, `began`, `changed`, `stationary`, `ended`,
`cancelled`) and `precise` (boolean). What Chromium receives: the pinned C API has no
phase fields, and on macOS CEF ignores `EVENTFLAG_PRECISION_SCROLLING_DELTA` and
`EVENTFLAG_SCROLL_BY_PAGE`: every wheel event is `kScrollByPrecisePixel` with
`kPhaseNone` (`TranslateWebWheelEvent`, `browser_platform_delegate_native_mac.mm`).
The OSR view's `MouseWheelPhaseHandler` then synthesizes phases: Began, Changed (or
Stationary for zero deltas), and Ended 500 ms after the last event; latching also
breaks after 10 px of pointer travel, a modifier change or a direction change. macOS
momentum events therefore reach Chromium as more non-momentum scroll updates: the
page scrolls with the system's inertia curve, but Chromium sees no fling and no
momentum phase, and a new gesture within 500 ms continues the previous latched
scroll. Elastic overscroll and scroll-snap behaviour at the end of momentum were not
verified. The host uses the phases only to drop zero-delta boundary events, which
would otherwise extend Chromium's latch by 500 ms; `precise` is validated and has no
effect.

**Pinch** (`pinch:true`): `pinch {x, y, modifiers, phase, magnification}`
(`|magnification|` ≤ 10, macOS increment). CEF's OSR view has no pinch API, so the
host synthesizes a two-point touch sequence (`send_touch_event`) centred on `x, y`:
`began` presses two points 160–400 points apart, each update multiplies the span by
`1 + magnification` (bounded to at most 12× the start span and never below 140
points, above Chromium's minimum scaling span), and `ended`/`cancelled` releases or cancels them. Chromium's
gesture provider turns this into GesturePinchBegin/Update/End: visual-viewport
pinch-zoom as in Chrome's trackpad pinch; layout and the Chromium zoom level are
unchanged. The page sees touch and pointer events (`pointerType:"touch"`) instead of
Chrome's synthetic ctrl+wheel, and can cancel them. A pinch with no update for 1 s,
a hidden target or a new document cancels the sequence.

**Frame rate** (`frame_rate_command:true`): `frame_rate {frame_rate: 60|120}` on a
target calls `set_windowless_frame_rate`, which CEF applies live to the compositor
v-sync interval and the capture period. Send it when the window moves to a display
with another refresh rate; new targets keep the `hello` rate, which `ready`
continues to report.

## Output

Each output item starts with exactly 16 bytes, all integers big-endian:

| Bytes | Field |
| --- | --- |
| 0–3 | ASCII `AXCF` |
| 4–5 | version = 1 (u16) |
| 6–7 | kind: 1 JSON event, 2 BGRA frame (u16) |
| 8–11 | UTF-8 JSON metadata length, 1..8192 (u32) |
| 12–15 | binary payload length, 0..33554432 (u32) |

The reader validates lengths before allocation, then reads exactly metadata and
payload bytes. A JSON event has zero binary payload. Frame metadata contains the
complete target, a monotonically increasing `frame_id`, physical `width`, `height`,
`stride`, `device_scale`, and `format:BGRA8`. Each dimension is 1..4096;
`stride == width*4` and `payload_length == stride*height <= 32 MiB`.
Oversized Retina surfaces use an explicitly reported bounded render scale while
retaining logical input coordinates, or return `unsupported` when no safe scale
fits. The UI exposes a scale cap; it is never mistaken for native Retina fidelity.
All generations are safe JSON integers (0..9007199254740991).

Events include `ready` with actual CEF/Chromium/platform/capabilities, `created`,
`accepted`, `completed`, `navigation`, `loading`, `title`, `url`, `closed`, `error`,
`cursor` (a CSS cursor keyword), `open_url`, `favicon` and the host-level `heartbeat` (see
"Heartbeat"); web sessions add `prompt`,
`prompt_closed`, `download_updated`, `find_result` and `popup_blocked`. A shared host must report
`multi_target`, `stop`, `cursor`, `persistent_profile` and `open_in_tab`. A malformed
event about one target's document ends that target; framing, authentication,
identity and response errors end the host and every target.
`favicon {icon}` carries the page icon: Chromium downloads the first declared icon
(else the default `/favicon.ico`) through its own network context, without cookies,
at most 32 px, and the host sends it as `data:image/png;base64,…` of at most
5400 PNG bytes, or `""` when the page has none or the download fails. Zen never
fetches an icon URL itself; any other value ends that target
(`INVALID_CEF_FAVICON`). Zen shows it with its own `setIcon`, keeps it when the
hidden Firefox browser loads `about:blank`, and uses Firefox's error-page icons
while a network or certificate error shows.
Generation changes precede observable navigation events. Frames/events for a stale
generation are discarded by the presenter and never applied to another target.
CEF engine identity comes from the pinned native runtime, not a spoofed user agent.

Every `load` event carries an integer `http_status` and a boolean
`restored_from_history`. A normal fixture load is successful only for HTTP 200.
Web mode accepts HTTP 200–599 so that normal server error pages can render; an
explicit `about:blank` start is the only new-page HTTP-0 exception. CEF can report
HTTP 0 when a previously committed page returns from its back-forward
cache. The native host may set `restored_from_history:true` only for a pending
back/forward command whose load-start and load-end URLs exactly match a fixture URL
previously committed with HTTP 200, with no intervening load error. The privileged
adapter checks that command and URL independently and waits for a subsequent frame
before considering the restored page ready. Other unsolicited HTTP 0 or a new
network URL never counts as a successful load.

## Browser UI delegation

Chromium never draws browser UI, exactly as Gecko content never does. In web
sessions every CEF UI callback becomes one targeted `prompt` event
`{prompt_id, kind, details, timeout_ms}`; Zen renders it with Firefox's own UI
(`apps/browser/chrome/ChromiumBrowserUI.sys.mjs`) and answers with one reply
command. `prompt_id` is host-issued (`prompt-<n>`, strictly increasing, never
reused). A reply is executed only when its `prompt_id` is still open, its method
matches the prompt kind, its `target` equals the tab's current target and, for page
prompts (all kinds except `download`), that target still equals the one the prompt
was issued for. Otherwise native answers `error {code:"stale_prompt"}` and does
nothing; this is not fatal because timeouts race with the user. A malformed reply
field is a protocol error. Every prompt is single use. Timeout (`timeout_ms`),
navigation (before generations advance), `on_reset_dialog_state`, tab close and
shutdown answer with the safe default and emit `prompt_closed {prompt_id, reason}`
(`timeout`, `navigation`, `reset`, `withdrawn`, `closed`, or `answered` after a
reply). The safe defaults: menu cancelled, dialog cancelled, beforeunload stays
(leaves only while the tab or host is closing), permission dismissed, file dialog
cancelled, download not started, auth cancelled. At most 8 open prompts per tab and
64 per host; a prompt whose event would exceed the metadata bound is denied, never
truncated (URLs are sent whole or as `""`). Details are strictly schema-checked by
`validateDelegationEvent`; a violation ends that tab (`INVALID_CEF_PROMPT`).

| kind | CEF callback | details | reply (`prompt_id` +) | timeout |
| --- | --- | --- | --- | --- |
| `context_menu` | `run_context_menu` | `x, y, type_flags, link_url, source_url, frame_url, selection_text, link_text, editable, edit_flags, media_type, media_flags, misspelled_word, suggestions, spellcheck` | `context_menu_command {command, index}`; `command` ∈ `dismiss, copy, cut, paste, select_all, undo, redo, copy_image, save_image, save_link, spelling, add_to_dictionary` | 120 s |
| `dialog` | `on_jsdialog` | `dialog_type` (`alert/confirm/prompt`), `origin_url, message, default_text` | `dialog_reply {accept, text}` | 300 s |
| `before_unload` | `on_before_unload_dialog` | `dialog_type:"beforeunload", is_reload` (no page text) | `dialog_reply {accept, text:""}` (accept = leave) | 300 s |
| `permission` | `on_show_permission_prompt` | `origin, permissions[]` | `permission_reply {decision}` (`allow/deny/dismiss`) | 300 s |
| `file_dialog` | `on_file_dialog` | `mode` (`open/open_multiple/open_folder/save`), `title, default_name` (file name only), `filters[] {filter, extensions, description}` | `file_dialog_reply {paths}` (`[]` = cancel) | 600 s |
| `download` | `on_before_download` | `download_id, suggested_name, url, mime_type, total_bytes` | `download_reply {path}` (`""` = cancel) | 600 s |
| `auth` | `get_auth_credentials` (IO thread, hopped to UI) | `origin, host, port, is_proxy, realm, scheme` | `auth_reply {accept, username, password}` | 300 s |

Context-menu actions run on the host-held link/source URL and frame of that prompt,
never a URL supplied in the reply; "Open link in new tab", "Copy link", search,
back/forward/reload and "Open page in Firefox" are Zen-local and answer `dismiss`.
File replies must be absolute paths without `..`; open modes require existing files
(or a directory for `open_folder`), save and download paths need an existing parent
directory and must not name a directory. Zen sends only paths the user chose in
`nsIFilePicker`, or a download path derived from Firefox's download-directory prefs.
Camera, microphone and screen capture are denied natively with
`error {code:"media_capture_unavailable"}`: the ad hoc signed OSR host has no macOS
privacy usage descriptions, so TCC cannot grant capture (`capabilities.media_capture:false`).
Unknown permission kinds are denied (`permission_denied`). Permission decisions are
remembered only in Zen's Chromium-scoped store (`<profile>/axiosozo/chromium/site-permissions.json`),
never in Firefox's permission manager. Credentials are never logged or saved.

Other web-only commands, bound to the current target: `find {text, forward,
match_case, find_next}` → `find_result {identifier, count, active, final}`;
`stop_finding {clear_selection}`; `zoom {level}` (Chromium level, |level| ≤ 10);
`download_control {download_id, action}` (`cancel/pause/resume`, only for a download
Zen accepted in the same tab; otherwise `error {code:"stale_download"}`). Accepted
downloads report `download_updated {download_id, state, received_bytes, total_bytes,
speed, paused}` (`state`: `in_progress/complete/canceled/interrupted`); they survive
navigation and are cancelled when their tab closes. A pop-up without a user gesture
never opens: native emits `popup_blocked {url}` (HTTP(S) only) and Zen shows
Firefox's pop-up-blocked bar; only an explicit user choice (or a remembered
per-origin allowance) opens it, as a new Chromium tab. `ready.capabilities` reports
`context_menu`, `javascript_dialogs`, `permissions`, `file_dialogs`, `downloads`,
`http_auth`, `find`, `zoom` and `popup_blocked` as `true` for web sessions and
`false` for fixtures; `popups` (native windows) and `media_capture` stay `false`.

## Surface mode (Mach IOSurface frames)

`hello` may add `surface_service` + `surface_token` (together), optional
`surface_begin_frames:true` and optional `frame_rate` (`60` default or `120`; the
latter is also valid without a surface). The frame channel, its handshake,
release/backpressure and begin-frame ticks are specified engine-neutrally in
[engine-surface-v1](engine-surface-v1.md). Surface mode has no 32 MiB payload cap:
`create`/`resize` need only 1..4096 physical px per side (at most 64 MiB per BGRA
IOSurface); the pipe keeps its cap. In surface mode `ready.render_path` is
`native-osr-iosurface` and capabilities add `surface`, `external_begin_frame` and
`frame_rate`; the pipe then carries only JSON events (no kind-2 frames), and a
JSON `frame_ack` is a protocol error because frames are released over Mach.
Channel failures emit `error {code:"surface_failed", reason}` and end the host
(exit 64). Everything else in this contract (commands, targets, generations,
events, security) is unchanged. Without `surface_service` the AXCF frames below
remain the fallback.

## Heartbeat (host liveness)

Process exit and Mach dead-name detection only cover a *dead* host. A host that is alive
but frozen (SIGSTOP, a deadlocked UI thread) is detected by a heartbeat:

- `ready.capabilities.heartbeat_interval_ms` (integer, 100..2000; the host sends `1000`)
  announces the feature. Absent = an older host: the adapter runs no watchdog and a
  `heartbeat` event from it is a protocol error.
- `{"event":"heartbeat","sequence":N}` is host-level (no `target`, no other fields).
  `sequence` is a uint, strictly increasing from the first beat (first >= 1). It is emitted
  every `heartbeat_interval_ms` from the CEF **UI thread** timer, so it proves the UI thread
  runs, not just the transport threads. Never before `ready`, never once the host is closing
  (shutdown, EOF, protocol or surface failure), and with no catch-up burst after a stall.
- Adapter rule: if the capability was announced, the host is not closing and no heartbeat
  has been accepted for 5 s (`HEARTBEAT_TIMEOUT_MS`), the host fails with the fixed code
  `CEF_HOST_UNRESPONSIVE`. That runs the normal failure path: the owned process is killed
  (SIGTERM, then SIGKILL after 500 ms, so a stopped process dies too), the surface endpoint
  closes and every Chromium tab of that host gets the crash panel and reload recovery.
- A malformed heartbeat (target present, extra field, non-uint, equal or lower sequence,
  before `ready`, or without the capability) is a host protocol error
  (`INVALID_CEF_HEARTBEAT`) and fails the host like any framing error.
- System sleep: the 5 s window restarts on the `wake_notification` observer topic, and
  also whenever the watchdog's own 1 s check fires more than 2.5 s late (sleep, or a Gecko
  main thread that could not read the pipe). A host is then judged on 5 s of Gecko-side
  running time, never on time the machine or Gecko was not running.
- The watchdog timer and wake observer are removed on close, failure and shutdown, and
  the timer is unref'd where the runtime supports it.

## Accessibility

`ready.capabilities.accessibility` is a boolean (`true` on the current host, web and
fixture sessions alike); when `true`, `ax_actions` lists the supported actions. The
host never enables renderer accessibility on its own and never globally (no
`--force-renderer-accessibility`). Semantics, privacy rules and limits are in
[docs/design/engine-accessibility.md](../docs/design/engine-accessibility.md) §5.

Commands, all bound to the current target unless noted:

- `accessibility {enabled}` maps to `set_accessibility_state` for that browser only
  (TreeOnly for windowless browsers) and completes `success`. Zen sends `true` only
  while a macOS assistive client is active and has queried that tab's canvas, and
  `false` when the tab is hidden or closed, or the client goes away.
- `ax_action {node_id, action, [value]}`:
  - `action` is one of `press`, `focus`, `scroll_to`, `set_value`, `show_menu`,
    `increment` or `decrement`.
  - `value` (<= 4096 UTF-16) is required for, and only allowed with, `set_value`.
  - It completes `success`, or `unsupported` with `stale_node`, `offscreen`,
    `focus_unavailable`, `focus_required`, `not_editable`, `accessibility_disabled`,
    `no_focused_frame` or `rate_limited` (20 actions per second per target).
  - CEF's C API (154 and the pinned 156) has no accessibility action entry point for OSR browsers, so
    actions are synthesized as trusted mouse, wheel, key, `select_all` and IME-commit
    input at the node's visible rect (design doc §5.5).
- `ax_ack {seq}` names the exact target of the event it answers (it may predate a
  navigation), like `frame_ack`. It gets no `accepted` or `completed`. An unknown
  `seq` is a protocol error; a `seq` from before a reset is ignored; an ack for a
  closed or replaced target is ignored.

Events, at most 4 unacknowledged per target (each <= 8 KiB):

- `ax_tree_update {seq, batch, reset, final, root, focus, px, events, truncated, nodes}`.
  - Chunks with the same `batch` form one atomic update, applied when `final:true`
    arrives. `reset:true` replaces the whole tree (first batch after enabling, and
    after a main-frame document change).
  - `root` and `focus` are wire ids (0 = none). `px` is 1 or the device scale.
  - `events` holds at most 32 `{type, id}` from the renderer's allowlist.
  - `nodes` holds records `{id, role, b:[x,y,w,h], oc, kids, [states], [actions],
    [scroll], [tf], [name], [value], [desc], [placeholder], [url], [roledesc],
    [shortcuts], [lang], [level], [checked], [invalid], [restriction], [popup],
    [setsize], [posinset], [sel], [range], [table], [live], [relevant], [atomic],
    [busy], [selected], [modal], [current], [input], [action], [tag], [activedesc],
    [linktarget], [redacted]}`, or continuations `{id, append:"name"|"value", text}` /
    `{id, append:"kids", kids}` of a record earlier in the same batch.
- `ax_location {seq, nodes:[{id, b, oc, [tf]}]}`: geometry only, applied immediately.

Further rules:

- Wire ids are per target and never reused; Chromium tree ids never leave the host.
- Password fields (state `protected` or input type `password`) carry
  `redacted:true` and never a value, selection or children; Zen redacts again.
- Every key is exact-checked. A malformed accessibility event, or one from a host
  that did not announce the capability, ends that tab (`INVALID_CEF_ACCESSIBILITY`).

## Backpressure and lifecycle

At most one frame is outstanding across the host. Each target keeps only its newest
undelivered frame, and delivery alternates between targets. The parent sends
`frame_ack` only after consuming or deliberately discarding that exact frame. An ACK matches the exact outstanding
`{frame_id,target}`, even when its generations have since become stale. This releases
transport credit only; it grants no native action. Unknown or duplicate ACKs fail.
The native UI thread never blocks on
pipe writes: an owned writer sends the current frame, with at most one pending newest
frame replacing older pending frames. Event queues and metadata are bounded too.
No frame is a screenshot of another application: payloads come directly from CEF's
native `OnPaint` callback and input returns to the same live CEF browser.

Resize invalidates stale-size frames. Close rejects further input, closes the CEF
browser and waits for the native closed callback. Engine switching keeps the Gecko
tab until the Chromium candidate emits an admitted load and actual frame; failure
restores it with no cookie transfer. After the switch commits, the Gecko browser
loads `about:blank`, so no Firefox document keeps running hidden behind Chromium.
Switching back loads Chromium's current address in Gecko. Each tab's engine and
Chromium address are saved as Zen session values and restore lazily when the tab is
shown. Background Chromium tabs are hidden natively and at most 24 are live per window.

Every ref-counted CEF struct passed into a C callback carries one reference the
callback releases; every struct the host passes into CEF carries one reference CEF
releases. The host follows both rules so `cef_shutdown` finds no live objects.

## Engine-preference hook (F6)

`installEngineProbeControls` returns `applyEnginePreference(tab, engine, { reason })`
for `EnginePreference.sys.mjs` ([contexts-api-v1](contexts-api-v1.md) §5). It
resolves to a frozen `{ applied, engine, error? }` and never rejects or throws.
`engine` is `"firefox"` or `"chromium"`; `reason` is a diagnostic token
(`/^[a-z][a-z0-9_]{0,31}$/`, otherwise recorded as `null`) and never reaches the UI.
The whole object is `null` when the engine switch is not enabled for the window
(`AXIOSOZO_ENGINE_SWITCHING` unset); callers treat that as `UNAVAILABLE`.

Checks run in this order; the first refusal returns `applied:false` with:

| `error` | When |
| --- | --- |
| `INVALID_ENGINE` | engine is not exactly `firefox`/`chromium` (`engine` is then `null`) |
| `DISABLED` | pref `axiosozo.engine.preferences.enabled` is not `true` (default false; read errors count as false) |
| `UNAVAILABLE` | disposed, fixture probe mode, no per-tab switch, or any unexpected exception |
| `PRIVATE` | private window or private target |
| `UNKNOWN_TAB` | the tab is not tracked by this window's Gecko adapter |
| `PENDING` | an engine switch (manual, restore or preference) is in progress |
| — | tab already uses the requested engine: `{ applied:false, engine }`, nothing happens |
| `UNSUPPORTED_URL` | `chromium` only: the page is not a credential-free HTTP(S) URL whose session entry has no POST data (`about:`, `chrome:`, `file:`, extension and view-source pages always stay in Firefox) |
| `CANCELLED` | the launch was cancelled (tab closed, deselected or switched back meanwhile) |
| `SWITCH_FAILED` | the per-tab switch failed, or finished without the tab using the requested engine |

A passing request calls the existing per-tab switch (`CEFPresenter.setTabEngine`),
so every presenter and native check still applies and failure restores the Firefox
tab without cookie transfer. A selected tab starts Chromium immediately; a
background tab is only marked and starts when shown, like the tab menu. `firefox`
on a Chromium tab returns it to Gecko with its current address. The native failure
code is kept in `diagnostics().failure`; `diagnostics().enginePreferences` holds
the gate state and the last request's result and reason. The hook adds nothing
to installation (web mode already constructs the presenter, which starts no CEF
process); a disabled gate and every refusal call no switch and start no CEF process.

## Keychain gate

Why live runs stop: the pinned CEF runtime (154, unchanged in 156) runs Chromium's OSCrypt, which on
macOS keeps the at-rest encryption password as generic-password item service
`Chromium Safe Storage`, account `Chromium` in the user's login keychain (both
strings, the `saltysalt` KDF salt and the `use-mock-keychain`/`mock_password` test
path are present in the pinned framework binary). The key is fetched process-wide
on the first network/cookie use, independent of the request context's `cache_path`,
so the in-memory fixture context and the persistent web context both reach it. The
recorded stall (`docs/evidence/cef-owned-stall-sample.txt`) shows a worker blocked in
`SecItemCopyMatching` → `SecKeychainItemCopyContent`: an item exists and reading its
secret needs an ACL approval, i.e. the macOS dialog. The host is ad hoc signed, so
its code identity is its cdhash and every rebuild is a new application to Keychain.
The name is Chromium's default, so the item can be shared with other Chromium/CEF
apps on the Mac. The host sets `command_line_args_disabled=1` and appends no
switches; pinned `cef_settings_t` has no Keychain name fields (upstream per-app
names, cef `fa874ac`, are `CEF_NEXT` only).

| Option | Security effect | Status |
| --- | --- | --- |
| Wout approves "Allow" (not "Always Allow") during a synthetic-profile run, ideally in a dedicated synthetic macOS user | Encryption unchanged. Grants this ad hoc binary the default shared key; in Wout's own login keychain that may be another Chromium app's key. Repeat after each rebuild | Wout's decision; never answered by an agent |
| Per-app Keychain service/account via a CEF release exposing fa874ac, plus stable Developer ID signing | Best: own item, ACL survives updates, no sharing | Blocked on upstream release and signing (open decision 3) |
| Patched self-built CEF with an own service name | Same as above | Rejected: full Chromium build and maintenance |
| `--use-mock-keychain` | Fixed, public key: at-rest data is only obfuscated | Never for the web profile. Acceptable only for strict fixture sessions (in-memory, synthetic, profile deleted on exit) behind an explicit flag the web host refuses; not implemented (native rebuild; unverifiable without risking the dialog) |
| `--password-store=basic` | — | Linux-only switch; absent from the macOS framework, no effect |
| Keep cookies in memory | — | Already true for fixtures and does not avoid the lookup |
| Change default keychain / edit item ACLs | Mutates user Keychain | Rejected |
