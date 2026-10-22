/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. https://mozilla.org/MPL/2.0/ */

// >>> AxioSozo engine UI delegation: schema lives with the UI (contracts/cef-v1.md).
import { validateDelegationEvent, validateDelegationCommand } from "./ChromiumBrowserUI.sys.mjs";
import { validateAXTreeUpdate, validateAXLocation, validateAXCommand } from "./ChromiumAccessibility.sys.mjs";
// <<<

export const CEF_VERSION = "156.0.7+g47dbc1d+chromium-156.0.8078.17";
export const CHROMIUM_VERSION = "156.0.8078.17";
// Inert identity of a target created at about:blank; never resolvable or loaded.
export const BLANK_IDENTITY = "https://axiosozo.invalid";
const MAX_PIXELS = 33554432;
// Engine-surface-v1 bounds every IOSurface to 1..4096 px per dimension; frames
// never cross the pipe in surface mode, so its 32 MiB payload cap does not apply.
export const MAX_SURFACE_DIMENSION = 4096;
// Render paths negotiated in hello/ready (contracts/cef-v1.md "Surface mode").
export const RENDER_PATH_SURFACE = "native-osr-iosurface";
export const RENDER_PATH_PIPE = "native-osr-bgra";
export const SURFACE_SERVICE_CONTRACT = "@axiosozo.nl/engine-surface-service;1";
const MAX_INFLIGHT_REQUESTS = 16;
const MAX_INFLIGHT_INPUT = 8;
const MAX_QUEUED_INPUT = 64;
// Navigation completes on load, cancellation or replacement, however long the
// site (or a first-run Keychain approval) takes. It never times out the engine.
// cef-v1 liveness: a host that announces `heartbeat_interval_ms` must emit a UI-thread
// `heartbeat` event; none for this long (while not closing) ends the host as unresponsive.
export const HEARTBEAT_TIMEOUT_MS = 5000;
// The watchdog wakes this often. A wake later than HEARTBEAT_STALL_MS means Gecko itself
// (or the whole machine, asleep) did not run, so the host could not have been heard.
const HEARTBEAT_CHECK_MS = 1000;
const HEARTBEAT_STALL_MS = 2500;
const UNTIMED_METHODS = new Set(["navigate", "back", "forward", "reload"]);
const CURSORS = new Set(["default", "pointer", "text", "vertical-text", "wait", "progress", "help",
  "crosshair", "move", "not-allowed", "grab", "grabbing", "context-menu", "cell", "alias", "copy",
  "zoom-in", "none", "ew-resize", "ns-resize", "nesw-resize", "nwse-resize"]);
const TARGET_KEYS = ["tab_id", "engine", "engine_instance", "native_target_id", "identity",
  "document_generation", "navigation_generation", "private_mode"];
const PENDING_KEYS = TARGET_KEYS.filter(key => !["engine", "native_target_id"].includes(key));
const isObject = value => value && typeof value === "object" && !Array.isArray(value);
const hasKeys = (value, keys) => isObject(value) && Object.keys(value).length === keys.length
  && Object.keys(value).every(key => keys.includes(key));
const id = value => typeof value === "string" && /^[a-zA-Z0-9_:/.-]{1,128}$/u.test(value);
const uint = value => Number.isSafeInteger(value) && value >= 0;
export const sameCEFTarget = (a, b) => TARGET_KEYS.every(key => a?.[key] === b?.[key]);

export function validateCEFTarget(value, { pending = false, browsingMode = "fixture" } = {}) {
  if (!hasKeys(value, pending ? PENDING_KEYS : TARGET_KEYS)
      || !id(value.tab_id) || !id(value.engine_instance)
      || !uint(value.document_generation) || !uint(value.navigation_generation)
      || value.private_mode !== false || !(browsingMode === "web" ? validWebOrigin(value.identity) : validFixtureOrigin(value.identity))
      || (!pending && (value.engine !== "chromium" || !id(value.native_target_id)))) {
    throw new Error("INVALID_CEF_TARGET");
  }
  return Object.freeze({ ...value });
}

export function validWebOrigin(origin) {
  if (!allowedWebURL(origin) || origin === "about:blank") return false;
  return new URL(origin).origin === origin;
}

/** Explicit browser navigation only; never accepts credentials or external schemes. */
export function allowedWebURL(value) {
  if (typeof value !== "string" || value.length > 4096 || /[\u0000- \u007f]/u.test(value)) return false;
  if (value === "about:blank") return true;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !!url.hostname && !url.username && !url.password;
  } catch { return false; }
}

export function validFixtureOrigin(origin) {
  if (typeof origin !== "string") return false;
  try {
    const url = new URL(origin);
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && !!url.port
      && url.origin === origin && !url.username && !url.password;
  } catch { return false; }
}

export function allowedFixtureURL(value, origin) {
  if (typeof value !== "string" || value.length > 4096 || !validFixtureOrigin(origin)) return false;
  try {
    const url = new URL(value);
    return url.origin === origin && url.pathname === "/engine.html"
      && !url.hash && !url.username && !url.password && ["", "?page=2"].includes(url.search);
  } catch { return false; }
}

/**
 * `maxBytes` is the BGRA pipe's 32 MiB payload bound by default. Surface mode
 * passes Infinity: only the 4096 px per dimension IOSurface bound remains.
 */
export function validateSurface({ width, height, device_scale }, { maxBytes = MAX_PIXELS } = {}) {
  if (![width, height].every(value => Number.isInteger(value) && value > 0 && value <= MAX_SURFACE_DIMENSION)
      || !Number.isFinite(device_scale) || device_scale < 1 || device_scale > 4) throw new Error("INVALID_SURFACE");
  const physicalWidth = Math.ceil(width * device_scale), physicalHeight = Math.ceil(height * device_scale);
  if (physicalWidth > MAX_SURFACE_DIMENSION || physicalHeight > MAX_SURFACE_DIMENSION
      || physicalWidth * physicalHeight * 4 > maxBytes) {
    throw new Error("UNSUPPORTED_SURFACE");
  }
  return { width, height, device_scale };
}

/**
 * Pipe fallback: keep the fixed 32 MiB bound when a Retina window grows,
 * including fullscreen. Surface mode (`maxBytes: Infinity`) steps the scale
 * down only past 4096 physical px per dimension (e.g. a fullscreen 5K window).
 */
export function fitCEFRenderSurface({ width, height, device_scale }, { maxBytes = MAX_PIXELS } = {}) {
  try { return validateSurface({ width, height, device_scale }, { maxBytes }); }
  catch (error) { if (error.message !== "UNSUPPORTED_SURFACE") throw error; }
  // Quarter steps avoid huge, unconstrained full-resolution frames. CEF still
  // receives the full logical viewport and its input coordinates do not change.
  for (let quarter = Math.ceil(device_scale * 4) - 1; quarter >= 4; quarter--) {
    try { return validateSurface({ width, height, device_scale: quarter / 4 }, { maxBytes }); }
    catch (error) { if (error.message !== "UNSUPPORTED_SURFACE") throw error; }
  }
  throw new Error("UNSUPPORTED_SURFACE");
}

// The project volume, or one named build root directly inside it.
const BUILD_ROOT = /^\/Volumes\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)?$/u;
const CEF_ROOT = /^\/Volumes\/[A-Za-z0-9_-]+$/u;

export function validCEFSessionRuntime(root, session, geckoProfile) {
  if (typeof root !== "string" || !BUILD_ROOT.test(root)
      || typeof session !== "string" || typeof geckoProfile !== "string") return false;
  const prefix = `${root}/runtime/`;
  if (!session.startsWith(prefix)) return false;
  const parts = session.slice(prefix.length).split("/");
  return parts.length === 2 && /^[0-9a-f]{16}$/u.test(parts[0])
    && /^[A-Za-z0-9-]{1,64}$/u.test(parts[1])
    && geckoProfile === `${session}/gecko`;
}

/** The CEF root is the build root itself, or the volume that directly contains it. */
export function validCEFRoot(root, cefRoot) {
  return typeof root === "string" && typeof cefRoot === "string" && BUILD_ROOT.test(root) && CEF_ROOT.test(cefRoot)
    && (root === cefRoot || root.slice(0, root.lastIndexOf("/")) === cefRoot);
}

// Trackpad phases as reported by the engine-view component's NSEvent monitor.
export const WHEEL_PHASES = new Set(["none", "may_begin", "began", "changed", "stationary", "ended", "cancelled"]);
const IME_TEXT_LIMIT = 256, IME_UNDERLINE_LIMIT = 16, IME_RANGE_LIMIT = 2147483647, IME_CURSOR_LIMIT = 65536;
const IME_METHODS = new Set(["ime_set_composition", "ime_commit_text", "ime_finish_composing", "ime_cancel_composition"]);
/**
 * Input commands. `wheel` phase fields, `pinch` and the `ime_*` methods are
 * cef-v1 input extensions: they are only valid when the host's ready
 * capabilities announce `wheel_phases`, `pinch` or `ime` (an older host
 * rejects unknown keys and methods as protocol errors).
 */
export function validateCEFInput(method, fields, surface, { wheelPhases = false, pinch = false, ime = false } = {}) {
  if (IME_METHODS.has(method)) {
    const keys = { ime_set_composition: ["text", "selection_start", "selection_end"], ime_commit_text: ["text"],
      ime_finish_composing: [], ime_cancel_composition: [] }[method];
    // Optional cef-v1 fields, each admitted by name (UTF-16 offsets, as the host checks).
    const optional = { ime_set_composition: ["underlines", "replacement_range"],
      ime_commit_text: ["replacement_range", "relative_cursor_pos"], ime_finish_composing: ["keep_selection"] }[method] ?? [];
    if (!ime || !isObject(fields)) throw new Error("INVALID_CEF_IME");
    if (!hasKeys(fields, [...keys, ...optional.filter(key => key in fields)])) throw new Error("INVALID_CEF_IME");
    if ("text" in fields && (typeof fields.text !== "string" || fields.text.length > IME_TEXT_LIMIT)) throw new Error("INVALID_CEF_IME");
    const within = (value, max) => uint(value) && value <= max;
    const span = (value, max) => within(value.start, max) && within(value.end, max) && value.start <= value.end;
    const range = (value, max) => hasKeys(value, ["start", "end"]) && span(value, max);
    if (method === "ime_set_composition" && !(uint(fields.selection_start) && uint(fields.selection_end)
        && fields.selection_start <= fields.selection_end && fields.selection_end <= fields.text.length)) throw new Error("INVALID_CEF_IME");
    if ("underlines" in fields && !(Array.isArray(fields.underlines) && fields.underlines.length <= IME_UNDERLINE_LIMIT
        && fields.underlines.every(line => hasKeys(line, ["start", "end", "thick"]) && span(line, fields.text.length)
          && typeof line.thick === "boolean"))) throw new Error("INVALID_CEF_IME");
    if ("replacement_range" in fields && !range(fields.replacement_range, IME_RANGE_LIMIT)) throw new Error("INVALID_CEF_IME");
    if ("relative_cursor_pos" in fields && !(Number.isInteger(fields.relative_cursor_pos)
        && Math.abs(fields.relative_cursor_pos) <= IME_CURSOR_LIMIT)) throw new Error("INVALID_CEF_IME");
    if ("keep_selection" in fields && typeof fields.keep_selection !== "boolean") throw new Error("INVALID_CEF_IME");
    return structuredClone(fields);
  }
  const required = {
    key: ["type", "native_key_code", "windows_key_code", "modifiers", "text"],
    mouse: ["type", "x", "y", "modifiers", "button", "click_count", "mouse_leave"],
    wheel: ["x", "y", "modifiers", "delta_x", "delta_y", ...(wheelPhases ? ["phase", "momentum_phase", "precise"] : [])],
    pinch: pinch ? ["x", "y", "modifiers", "phase", "magnification"] : null,
  }[method];
  const bounded = (value, max) => uint(value) && value <= max;
  if (!required || !hasKeys(fields, required) || !bounded(fields.modifiers, 131071)) throw new Error("INVALID_CEF_INPUT");
  if (method === "key") {
    if (!["down", "up", "char"].includes(fields.type) || !bounded(fields.native_key_code, 65535)
        || !bounded(fields.windows_key_code, 255) || typeof fields.text !== "string" || fields.text.length > 4) throw new Error("INVALID_CEF_KEY");
  } else {
    if (!surface || !bounded(fields.x, surface.width) || !bounded(fields.y, surface.height)) throw new Error("INVALID_CEF_POINT");
    if (method === "mouse" && (!["move", "down", "up"].includes(fields.type)
        || !["left", "middle", "right"].includes(fields.button) || !bounded(fields.click_count, 3)
        || fields.click_count < 1 || typeof fields.mouse_leave !== "boolean")) throw new Error("INVALID_CEF_MOUSE");
    if (method === "wheel" && ![fields.delta_x, fields.delta_y].every(value => Number.isInteger(value) && Math.abs(value) <= 4096)) throw new Error("INVALID_CEF_WHEEL");
    if (method === "wheel" && wheelPhases && (!WHEEL_PHASES.has(fields.phase) || !WHEEL_PHASES.has(fields.momentum_phase)
        || typeof fields.precise !== "boolean")) throw new Error("INVALID_CEF_WHEEL");
    if (method === "pinch" && (!WHEEL_PHASES.has(fields.phase) || !Number.isFinite(fields.magnification)
        || Math.abs(fields.magnification) > 10)) throw new Error("INVALID_CEF_PINCH");
  }
  return { ...fields };
}

const arrayBufferLength = Object.getOwnPropertyDescriptor(ArrayBuffer.prototype, "byteLength").get;
function bufferLength(value) {
  // Firefox Subprocess can return buffers created in another module/worker realm.
  // The intrinsic getter checks the real ArrayBuffer brand without instanceof
  // or trusting a caller-supplied byteLength/toStringTag property.
  try { return Reflect.apply(arrayBufferLength, value, []); }
  catch { return -1; }
}

/** Subprocess.InputPipe.read(length) is exact, unlike readUint32's native endianness. */
export async function readAXCF(pipe) {
  const header = await pipe.read(16);
  if (bufferLength(header) !== 16) throw new Error("TRUNCATED_AXCF_HEADER");
  const view = new DataView(header);
  if (view.getUint32(0, false) !== 0x41584346 || view.getUint16(4, false) !== 1) throw new Error("INVALID_AXCF_HEADER");
  const kind = view.getUint16(6, false), metadataSize = view.getUint32(8, false), size = view.getUint32(12, false);
  if (![1, 2].includes(kind) || metadataSize < 1 || metadataSize > 8192 || size > MAX_PIXELS || (kind === 1 && size)) {
    throw new Error("INVALID_AXCF_LENGTH");
  }
  const raw = await pipe.read(metadataSize);
  if (bufferLength(raw) !== metadataSize) throw new Error("TRUNCATED_AXCF_METADATA");
  const metadata = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
  if (!isObject(metadata) || metadata.version !== 1) throw new Error("INVALID_AXCF_METADATA");
  if (kind === 2) {
    if (!hasKeys(metadata, ["version", "target", "frame_id", "width", "height", "stride", "device_scale", "format"])
        || !uint(metadata.frame_id) || metadata.frame_id === 0
        || ![metadata.width, metadata.height].every(value => Number.isInteger(value) && value > 0 && value <= 4096)
        || metadata.stride !== metadata.width * 4 || size !== metadata.stride * metadata.height
        || !Number.isFinite(metadata.device_scale) || metadata.device_scale < 1 || metadata.device_scale > 4
        || metadata.format !== "BGRA8") throw new Error("INVALID_AXCF_FRAME");
    validateCEFTarget(metadata.target, { browsingMode: "web" });
  }
  // Allocate/read large pixels only after both framing and metadata are checked.
  const pixels = size ? await pipe.read(size) : new ArrayBuffer(0);
  if (bufferLength(pixels) !== size) throw new Error("TRUNCATED_AXCF_PIXELS");
  return { kind, metadata, pixels };
}

const EVENT_FIELDS = {
  heartbeat: ["sequence"],
  ready: ["cef", "chromium", "runtime_cef", "runtime_chromium", "platform", "sandbox_configured", "engine_instance", "render_path", "capabilities"],
  created: ["request_id"], accepted: ["request_id"], completed: ["request_id", "status", "reason"],
  navigation: [], loading: ["loading", "can_go_back", "can_go_forward"],
  title: ["title"], url: ["url"], closed: [], error: ["request_id", "code", "native_code"], load: ["http_status", "restored_from_history", "same_document"],
  cursor: ["cursor"], open_url: ["url", "background"],
  // The page icon as a small PNG data URL Chromium downloaded, or "" for none.
  favicon: ["icon"],
  // Proposed with `ime`: the focused editable's kind and caret, in logical points.
  text_input: ["mode", "caret_x", "caret_y", "caret_width", "caret_height"],
  // >>> AxioSozo engine UI delegation (validated by validateDelegationEvent)
  prompt: ["prompt_id", "kind", "details", "timeout_ms"], prompt_closed: ["prompt_id", "reason"],
  download_updated: ["download_id", "state", "received_bytes", "total_bytes", "speed", "paused"],
  find_result: ["identifier", "count", "active", "final"], popup_blocked: ["url"],
  // <<<
  // Accessibility (docs/design/engine-accessibility.md; validated by ChromiumAccessibility).
  ax_tree_update: ["seq", "batch", "reset", "final", "root", "focus", "px", "events", "truncated", "nodes"],
  ax_location: ["seq", "nodes"],
};
// Only a PNG the host encoded itself: never a remote URL Zen would fetch, never SVG.
export const MAX_FAVICON_LENGTH = 7400;
const FAVICON = /^data:image\/png;base64,(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u;
export const validFavicon = value => value === "" || (typeof value === "string" && value.length <= MAX_FAVICON_LENGTH
  && value.length > 22 && FAVICON.test(value));
const AX_EVENTS = new Set(["ax_tree_update", "ax_location"]);
const DELEGATION_EVENTS = new Set(["prompt", "prompt_closed", "download_updated", "find_result", "popup_blocked"]);

// Preserve only our fixed diagnostic codes. Never display native payloads,
// arbitrary exception messages, pipe contents or authentication material.
const READ_FAILURE_CODES = new Set([
  "TRUNCATED_AXCF_HEADER", "TRUNCATED_AXCF_METADATA", "TRUNCATED_AXCF_PIXELS",
  "INVALID_AXCF_HEADER", "INVALID_AXCF_LENGTH", "INVALID_AXCF_METADATA", "INVALID_AXCF_FRAME",
  "INVALID_CEF_TARGET", "FOREIGN_CEF_TARGET", "INVALID_CEF_CREATION", "UNKNOWN_CEF_TARGET",
  "INVALID_CEF_GENERATION", "STALE_CEF_EVENT", "UNEXPECTED_CEF_FRAME", "FOREIGN_CEF_FRAME",
  "FUTURE_CEF_FRAME", "INVALID_CEF_EVENT", "UNVERIFIED_CEF_RUNTIME", "UNREQUESTED_CEF_CREATION",
  "MISSING_CEF_TARGET", "UNKNOWN_CEF_RESPONSE", "INVALID_CEF_COMPLETION", "INVALID_CEF_TITLE",
  "INVALID_CEF_LOADING", "INVALID_CEF_URL", "INVALID_CEF_LOAD", "INVALID_CEF_HISTORY_RESTORE",
  "INVALID_CEF_CURSOR", "INVALID_CEF_OPEN_URL", "INVALID_CEF_PROMPT", "INVALID_CEF_TEXT_INPUT",
  "CEF_FRAME_PRESENTATION_FAILED", "CEF_EVENT_CALLBACK_FAILED",
  "CEF_SURFACE_BIND_FAILED", "CEF_SURFACE_CLOSED", "INVALID_CEF_HEARTBEAT", "CEF_HOST_UNRESPONSIVE",
  "INVALID_CEF_ACCESSIBILITY", "INVALID_CEF_FAVICON",
]);
// A violation that concerns one target's own document. On a shared host it ends
// only that tab; framing, identity and response errors end the whole host.
class TargetError extends Error {}
// Window scripts and the process-wide module are separate realms; a registry
// symbol identifies a host connection from either one.
const HOST_BRAND = Symbol.for("axiosozo.cef.host-connection");
const targetError = code => new TargetError(code);
/** cef-v1 `native_target_id` as the engine-surface-v1 u64 (a safe JS integer here). */
export function surfaceTargetId(target) {
  const value = target?.native_target_id;
  if (typeof value !== "string" || !/^[1-9][0-9]{0,15}$/u.test(value) || !Number.isSafeInteger(Number(value))) {
    throw targetError("CEF_SURFACE_BIND_FAILED");
  }
  return Number(value);
}

/**
 * The Gecko engine-view component (apps/browser/native/engine-view), or null when
 * this build or test has none. Null selects the labelled BGRA pipe fallback.
 */
export function engineSurfaceService() {
  try {
    const factory = globalThis.Cc?.[SURFACE_SERVICE_CONTRACT];
    const iface = globalThis.Ci?.nsIAxioEngineSurfaceService;
    return factory && iface ? factory.getService(iface) : null;
  } catch { return null; }
}
/** Hello `frame_rate` from Gecko's vsync source: 120 on ProMotion, otherwise 60. */
export function surfaceFrameRate(service) {
  let rate = 60;
  try { rate = Number(service?.displayRefreshRate); } catch {}
  return rate >= 100 ? 120 : 60;
}
/** cef-v1 `frame_rate` command values. */
export const FRAME_RATES = Object.freeze([60, 120]);

/**
 * One authenticated native process. A web host serves every Chromium tab of a
 * Zen profile; each tab is a CEFEngineAdapter registered by its tab_id.
 */
export class CEFHostConnection {
  #process; #token; #timers; #deadline; #requests = new Map(); #sequence = 0;
  #targets = new Map(); #readyWait; #ended = false; #closing = false; #ready = false;
  // Surface mode: one nsIAxioEngineEndpoint per host process; frames never reach JS.
  #endpoint = null; #frameRate = null; #surfaceTargets = new Map(); #surfaceClosed = false;
  // Native creates one target at a time; a tab_id is reused only after its old target closed.
  #createQueue = Promise.resolve(); #retiring = new Map();
  // Heartbeat watchdog (armed by a ready capability): last accepted sequence, the time anything
  // was last heard or the watchdog last reset, the pending check timer and the wake observer.
  #heartbeatSequence = 0; #heartbeatTimeout; #heartbeatInterval = null; #heartbeatSeen = 0;
  #heartbeatCheckedAt = 0; #heartbeatTimer = null; #wakeObserver = null; #now; #observers;
  constructor(process, { token, instance, identity, timers, deadline = 15000, browsingMode = "fixture",
    shared = false, onFailure = () => {}, surface = null, heartbeatTimeout = HEARTBEAT_TIMEOUT_MS,
    now = () => Date.now(), observers = globalThis.Services?.obs ?? null }) {
    if (!/^[0-9a-f]{64}$/u.test(token)) throw new Error("INVALID_CEF_BOOTSTRAP");
    if (surface && (!surface.endpoint || ![60, 120].includes(surface.frameRate))) throw new Error("INVALID_CEF_BOOTSTRAP");
    if (!["fixture", "web"].includes(browsingMode)) throw new Error("INVALID_CEF_MODE");
    if (!id(instance) || !(browsingMode === "web" ? validWebOrigin(identity) : validFixtureOrigin(identity))) throw new Error("INVALID_CEF_BOOTSTRAP");
    this.browsingMode = browsingMode; this.shared = shared; this.instance = instance; this.identity = identity;
    this.#process = process; this.#token = token; this.#timers = timers; this.#deadline = deadline;
    this.#heartbeatTimeout = heartbeatTimeout; this.#now = now; this.#observers = observers;
    this.onFailure = onFailure;
    this.status = "starting"; this.readPhase = "before_connect"; this.lastFrameBytes = 0; this.lastFrame = 0;
    this.nativeExitCode = null; this.capabilities = null;
    this.#endpoint = surface?.endpoint ?? null; this.#frameRate = surface?.frameRate ?? null;
    // Native input helpers (holdKeyEvent, describeNativeEvent, setCursor) of the same component.
    this.nativeInput = surface ? surface.service ?? null : null;
    this.renderPath = surface ? RENDER_PATH_SURFACE : RENDER_PATH_PIPE;
    this.surfaceConnected = false; this.surfaceClosedReason = null;
    this.exited = process.wait().then(() => {}, () => {});
  }
  get [HOST_BRAND]() { return true; }
  get surfaceMode() { return !!this.#endpoint; }
  get frameRate() { return this.#frameRate; }
  /**
   * The display refresh class now, from the same vsync source as the hello
   * `frame_rate`; null without the engine-view component (no rate source).
   */
  get displayFrameRate() { return this.nativeInput ? surfaceFrameRate(this.nativeInput) : null; }
  get ended() { return this.#ended; }
  get targetCount() { return this.#targets.size; }
  waiter(label, onTimeout) {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    const timer = onTimeout && this.#timers.setTimeout(() => onTimeout(`${label}_TIMEOUT_UNCERTAIN`), this.#deadline);
    // Attach a rejection observer immediately; callers still receive the rejection.
    promise.catch(() => {});
    const clear = () => { if (timer) this.#timers.clearTimeout(timer); };
    return { promise, resolve: value => { clear(); resolve(value); }, reject: error => { clear(); reject(error); } };
  }
  async connect() {
    this.#readyWait = this.waiter("CEF_READY", code => this.fail(code));
    this.#read().catch(error => {
      if (!this.#closing) this.fail(READ_FAILURE_CODES.has(error?.message) ? error.message : "CEF_PROTOCOL_OR_EOF");
    });
    this.#drainErrors().catch(() => this.fail("CEF_STDERR_FAILURE"));
    this.#process.wait().then(result => {
      this.nativeExitCode = result.exitCode;
      if (result.exitCode !== 0) this.fail("CEF_CRASH_OUTCOME_UNCERTAIN");
      else if (!this.#closing) this.fail("CEF_UNEXPECTED_EXIT");
      if (this.status === "failed") console.error("AXIOSOZO_CEF_EXIT", JSON.stringify({ exitCode:result.exitCode }));
    }, () => this.fail("CEF_PROCESS_FAILURE"));
    const hello = { version: 1, method: "hello", token: this.#token,
      engine_instance: this.instance, fixture_origin: this.identity,
      ...(this.browsingMode === "web" ? { browsing_mode: "web" } : {}) };
    if (this.#endpoint) {
      const endpoint = this.#endpoint;
      try {
        endpoint.listener = this.#surfaceListener();
        // CONNECT is accepted only from this direct child; register it before its token leaves.
        endpoint.expectHostPid(this.#process.pid);
        // The service name and single-use token travel only in this authenticated hello.
        Object.assign(hello, { surface_service: endpoint.serviceName, surface_token: endpoint.takeToken(),
          ...(endpoint.externalBeginFrames ? { surface_begin_frames: true } : {}), frame_rate: this.#frameRate });
      } catch (error) { this.fail("CEF_SURFACE_SETUP_FAILED"); throw new Error("CEF_SURFACE_SETUP_FAILED"); }
    }
    await this.#write(hello);
    await this.#readyWait.promise;
    this.status = "connected";
    return this;
  }
  #surfaceListener() {
    const listener = {
      onConnected: pid => { this.surfaceConnected = pid === this.#process.pid; },
      onTargetGeometry: (targetId, width, height, logicalWidth, logicalHeight, scale) => {
        const adapter = this.#surfaceTargets.get(String(targetId));
        if (!adapter) return;
        try { adapter.surfaceGeometry({ width, height, logicalWidth, logicalHeight, scale }); }
        catch (error) {
          // Never throw back into the native caller: a host-wide violation ends the host here.
          try { this.#targetFailed(adapter, error); } catch (fatal) { this.fail(fatal.message); }
        }
      },
      onClosed: reason => {
        this.surfaceClosedReason = String(reason);
        this.#surfaceClosed = true; this.#surfaceTargets.clear();
        // host-died, host-unresponsive or protocol:<detail>: every Chromium tab of this host ends.
        if (!this.#ended && !this.#closing) this.fail("CEF_SURFACE_CLOSED");
      },
    };
    const qi = globalThis.ChromeUtils?.generateQI?.(["nsIAxioEngineEndpointListener"]);
    if (qi) listener.QueryInterface = qi;
    return listener;
  }
  /** Present `target` in `element` (a context-less chrome <canvas>); frames bypass JS. */
  bindSurface(adapter, element, target) {
    const id = surfaceTargetId(target);
    if (!this.#endpoint || this.#surfaceClosed || this.#ended || !element) throw targetError("CEF_SURFACE_BIND_FAILED");
    try { this.#endpoint.bindElement(element, id, target.document_generation, target.navigation_generation); }
    catch { throw targetError("CEF_SURFACE_BIND_FAILED"); }
    this.#surfaceTargets.set(String(id), adapter);
    return id;
  }
  /** Chrome processed this generation: the endpoint may now show its held frames. */
  confirmSurface(id, target) {
    if (!this.#endpoint || this.#surfaceClosed || this.#ended) return;
    try { this.#endpoint.setTargetGenerations(id, target.document_generation, target.navigation_generation); }
    catch { throw targetError("CEF_SURFACE_BIND_FAILED"); }
  }
  unbindSurface(id, adapter) {
    if (this.#surfaceTargets.get(String(id)) !== adapter) return;
    this.#surfaceTargets.delete(String(id));
    if (!this.#surfaceClosed && !this.#ended) try { this.#endpoint.unbindTarget(id); } catch {}
  }
  /** Begin-frame gating: hidden targets receive no BEGIN_FRAME and unpin old surfaces. */
  surfaceVisible(id, visible) {
    if (this.#surfaceClosed || this.#ended) return;
    try { this.#endpoint?.setTargetVisible(id, visible); } catch {}
  }
  surfaceStats(id) {
    if (this.#surfaceClosed || this.#ended) return null;
    try { return this.#endpoint?.getTargetStats(id) ?? null; } catch { return null; }
  }
  #closeSurface() {
    if (!this.#endpoint || this.#surfaceClosed) return;
    this.#surfaceClosed = true; this.#surfaceTargets.clear();
    try { this.#endpoint.close(); } catch {}
  }
  register(adapter) {
    if (this.#ended || this.#closing) throw new Error("CEF_UNAVAILABLE");
    if (this.#targets.has(adapter.tabId)) throw new Error("DUPLICATE_CEF_TARGET");
    this.#targets.set(adapter.tabId, adapter);
  }
  unregister(adapter) { if (this.#targets.get(adapter.tabId) === adapter) this.#targets.delete(adapter.tabId); }
  /** Until native confirms `close`, the old target keeps its tab_id. */
  retire(tabId, closing) {
    const done = closing.then(() => {}, () => {}).finally(() => { if (this.#retiring.get(tabId) === done) this.#retiring.delete(tabId); });
    this.#retiring.set(tabId, done);
  }
  serializeCreate(tabId, start) {
    const turn = this.#createQueue.then(() => this.#retiring.get(tabId)).then(start);
    this.#createQueue = turn.then(() => {}, () => {});
    return turn;
  }
  async #drainErrors() {
    if (!this.#process.stderr) return;
    let bytes = 0;
    for (;;) {
      const chunk = await this.#process.stderr.read();
      if (!chunk.byteLength) return;
      bytes += chunk.byteLength;
      // A long-lived host logs occasional Chromium warnings; bound each window.
      if (bytes > 65536 && !this.shared) { this.fail("CEF_DIAGNOSTIC_LIMIT"); return; }
    }
  }
  async #write(value) {
    const text = JSON.stringify(value) + "\n";
    if (new TextEncoder().encode(text).length > 16384) throw new Error("CEF_REQUEST_TOO_LARGE");
    try { await this.#process.stdin.write(text); }
    catch (error) { this.fail("CEF_WRITE_OUTCOME_UNCERTAIN"); throw error; }
  }
  request(method, fields, target, { acknowledge = false, owner = null } = {}) {
    // Surface frames are released over Mach; a JSON frame_ack is a host protocol error.
    if (method === "frame_ack" && this.#endpoint) return Promise.reject(new Error("CEF_SURFACE_FRAME_ACK"));
    if (this.#ended || (this.#closing && !["frame_ack", "shutdown", "close"].includes(method))) return Promise.reject(new Error("CEF_UNAVAILABLE"));
    if (++this.#sequence > (this.browsingMode === "web" ? Number.MAX_SAFE_INTEGER : 100000)
        || (!acknowledge && this.#requests.size >= MAX_INFLIGHT_REQUESTS * Math.max(1, this.#targets.size))) return Promise.reject(new Error("CEF_REQUEST_LIMIT"));
    const request_id = `cef-${this.#sequence}`;
    const value = { version: 1, method, request_id, token: this.#token, ...fields };
    if (method !== "shutdown") value.target = target;
    if (acknowledge) return this.#write(value);
    const onTimeout = UNTIMED_METHODS.has(method) ? null
      : code => (owner && this.shared ? owner.fail(code) : this.fail(code));
    const waiter = this.waiter("CEF_ACTION", onTimeout);
    Object.assign(waiter, { method, owner, request_id });
    this.#requests.set(request_id, waiter);
    owner?.trackRequest(request_id);
    this.#write(value).catch(error => waiter.reject(error));
    return waiter.promise;
  }
  #forget(request_id) {
    const waiter = this.#requests.get(request_id);
    this.#requests.delete(request_id);
    waiter?.owner?.untrackRequest(request_id);
    return waiter;
  }
  rejectRequestsOf(owner, error) {
    // Native still answers these; keep each ID so its late response is recognised.
    for (const [request_id, waiter] of this.#requests) if (waiter.owner === owner && !waiter.abandoned) {
      waiter.abandoned = true; owner.untrackRequest(request_id); waiter.reject(error);
    }
  }
  #adapterFor(target, created = false) {
    // A legacy single-target connection owns exactly one adapter. It validates
    // foreign identity itself and fails closed instead of ignoring the packet.
    if (!this.shared) return this.#targets.values().next().value ?? null;
    if (target?.engine_instance !== this.instance) throw new Error("FOREIGN_CEF_TARGET");
    const adapter = this.#targets.get(target.tab_id) ?? null;
    // A closing previous target of the same tab still reports until native closes it.
    if (!adapter || (created ? adapter.target : adapter.target?.native_target_id !== target.native_target_id)) return null;
    return adapter;
  }
  #targetFailed(adapter, error) {
    const code = READ_FAILURE_CODES.has(error?.message) ? error.message : "CEF_PROTOCOL_OR_EOF";
    if (!(error instanceof TargetError) || !this.shared || !adapter) throw new Error(code);
    adapter.fail(code);
  }
  async #read() {
    while (!this.#ended) {
      this.readPhase = "packet";
      const { kind, metadata: value, pixels } = await readAXCF(this.#process.stdout);
      if (kind === 2) {
        this.readPhase = "frame_validate";
        // Surface mode: the pipe carries JSON events only; frames travel over Mach.
        if (this.#endpoint) throw new Error("UNEXPECTED_CEF_FRAME");
        if (value.frame_id <= this.lastFrame) throw new Error("UNEXPECTED_CEF_FRAME");
        this.lastFrame = value.frame_id;
        const adapter = this.#adapterFor(value.target);
        let presentationError = null;
        try {
          if (adapter) {
            try { await adapter.acceptFrame(value, pixels); }
            catch (error) { presentationError = error; }
          } else if (!this.shared) throw new Error("UNEXPECTED_CEF_FRAME");
        } finally {
          // Stale/old-size frames must release exact outstanding transport credit.
          // This never executes a native browser action against their old target.
          this.readPhase = "frame_ack";
          await this.request("frame_ack", { frame_id: value.frame_id }, value.target, { acknowledge: true });
        }
        if (presentationError) {
          this.readPhase = presentationError.message === "CEF_FRAME_PRESENTATION_FAILED" ? "frame_present" : "frame_validate";
          this.#targetFailed(adapter, presentationError);
        }
        continue;
      }
      this.readPhase = "event_validate";
      const fields = EVENT_FIELDS[value.event];
      if (!fields || Object.keys(value).some(key => !["version", "event", "target", ...fields].includes(key))) throw new Error("INVALID_CEF_EVENT");
      if (value.event === "ready") { this.#acceptReady(value); continue; }
      if (value.event === "heartbeat") { this.#acceptHeartbeat(value); continue; }
      let adapter = null;
      if (value.target) {
        const creation = value.event === "created" && this.#requests.get(value.request_id);
        if (value.event === "created" && creation?.method !== "create") throw new Error("UNREQUESTED_CEF_CREATION");
        adapter = creation?.abandoned ? null : this.#adapterFor(value.target, value.event === "created");
        if (!adapter && !this.shared) throw new Error("UNKNOWN_CEF_TARGET");
        // A target whose tab went away while it was being created is closed at once.
        if (creation?.abandoned) this.retire(value.target.tab_id, this.request("close", {}, value.target));
      } else if (["created", "navigation", "load", "loading", "title", "url", "closed", "cursor", "open_url", "text_input", "favicon"].includes(value.event)
          || DELEGATION_EVENTS.has(value.event) || AX_EVENTS.has(value.event)) {
        throw new Error("MISSING_CEF_TARGET");
      }
      if (["accepted", "completed"].includes(value.event) || (value.event === "error" && value.request_id)) {
        const waiter = this.#requests.get(value.request_id);
        if (!waiter) throw new Error("UNKNOWN_CEF_RESPONSE");
        if (value.event !== "accepted") {
          this.#forget(value.request_id);
          if (waiter.abandoned) continue;
          // A native generation check can reject input already in flight when
          // history navigation commits. Match the local resolve() error so the
          // presenter discards that input without closing the healthy engine.
          if (value.event === "error") waiter.reject(new Error(value.code === "stale_target" ? "STALE_CEF_TARGET" : "CEF_ACTION_FAILED"));
          else if (!["success", "unsupported", "failed", "uncertain"].includes(value.status)) throw new Error("INVALID_CEF_COMPLETION");
          else waiter.resolve({ status: value.status, reason: value.reason, target: waiter.owner?.target ?? null });
          waiter.owner?.pumpInput();
        }
      }
      if (adapter) {
        this.readPhase = "event_callback";
        try { adapter.acceptEvent(value); }
        catch (error) { this.#targetFailed(adapter, error); }
      }
    }
  }
  #acceptReady(value) {
    const web = this.browsingMode === "web", capabilities = value.capabilities, surface = !!this.#endpoint;
    if (this.#ready || this.status !== "starting" || value.cef !== CEF_VERSION || value.chromium !== CHROMIUM_VERSION
        || value.runtime_cef !== CEF_VERSION.split("+")[0] || value.runtime_chromium !== CHROMIUM_VERSION
        || value.engine_instance !== this.instance || value.platform !== "macosarm64"
        || value.render_path !== (surface ? RENDER_PATH_SURFACE : RENDER_PATH_PIPE) || value.sandbox_configured !== true
        // Surface mode must be exactly what the hello negotiated; the pipe never claims it.
        || (surface ? capabilities?.surface !== true || capabilities?.external_begin_frame !== this.#endpoint.externalBeginFrames
          || capabilities?.frame_rate !== this.#frameRate : capabilities?.surface === true)
        || capabilities?.fixture_only !== !web || capabilities?.devtools !== false
        || (web && (capabilities?.edit !== true || capabilities?.visibility !== true
          // Permissions, downloads, dialogs, file pickers: false = denied natively,
          // true = delegated to Zen's own UI. Native pop-up windows never exist.
          || typeof capabilities?.permissions !== "boolean" || typeof capabilities?.downloads !== "boolean"
          || capabilities?.popups !== false))
        // Accessibility (design doc §6): announced either way; when true, with its action list.
        || typeof capabilities?.accessibility !== "boolean"
        || (capabilities.accessibility === true && !(Array.isArray(capabilities.ax_actions)
          && capabilities.ax_actions.every(action => typeof action === "string" && action.length <= 16)))
        // One shared host needs native multi-target isolation and its profile model.
        || (this.shared && (capabilities?.multi_target !== true || capabilities?.stop !== true
          || capabilities?.cursor !== true || capabilities?.persistent_profile !== web || capabilities?.open_in_tab !== web))
        || capabilities?.private_mode !== false || typeof (capabilities?.ime ?? false) !== "boolean"
        // Optional liveness announcement; a value that could never fit the 5 s rule is not trusted.
        || (capabilities?.heartbeat_interval_ms !== undefined && !(Number.isInteger(capabilities.heartbeat_interval_ms)
          && capabilities.heartbeat_interval_ms >= 100 && capabilities.heartbeat_interval_ms <= 2000))) throw new Error("UNVERIFIED_CEF_RUNTIME");
    this.#ready = true; this.capabilities = Object.freeze({ ...capabilities });
    this.#readyWait.resolve(value);
    this.#startHeartbeatWatchdog(capabilities.heartbeat_interval_ms);
  }
  #acceptHeartbeat(value) {
    // Host-level only: never targeted, only after ready, only when announced, strictly increasing.
    if (value.target || !this.#ready || this.#heartbeatInterval === null || !uint(value.sequence)
        || value.sequence <= this.#heartbeatSequence) throw new Error("INVALID_CEF_HEARTBEAT");
    this.#heartbeatSequence = value.sequence;
    this.#heartbeatSeen = this.#now();
  }
  #startHeartbeatWatchdog(interval) {
    if (interval === undefined || this.#ended || this.#closing) return;
    this.#heartbeatInterval = interval;
    this.#heartbeatSeen = this.#heartbeatCheckedAt = this.#now();
    // System wake: the host was frozen with the machine, not by itself. Start the window anew.
    if (this.#observers?.addObserver) {
      this.#wakeObserver = { observe: () => { this.#heartbeatSeen = this.#heartbeatCheckedAt = this.#now(); } };
      try { this.#observers.addObserver(this.#wakeObserver, "wake_notification"); } catch { this.#wakeObserver = null; }
    }
    this.#scheduleHeartbeatCheck();
  }
  #scheduleHeartbeatCheck() {
    const timer = this.#heartbeatTimer = this.#timers.setTimeout(() => {
      this.#heartbeatTimer = null;
      if (this.#ended || this.#closing) return;
      const now = this.#now();
      // This check itself ran late (sleep, or a stalled Gecko main thread that could not
      // read the pipe): unread heartbeats are not evidence of a frozen host.
      if (now - this.#heartbeatCheckedAt > HEARTBEAT_STALL_MS) this.#heartbeatSeen = now;
      this.#heartbeatCheckedAt = now;
      if (now - this.#heartbeatSeen >= this.#heartbeatTimeout) { this.fail("CEF_HOST_UNRESPONSIVE"); return; }
      this.#scheduleHeartbeatCheck();
    }, HEARTBEAT_CHECK_MS);
    // A watchdog must never keep a test runner (or shutdown) alive; Gecko timer IDs are numbers.
    if (typeof timer?.unref === "function") timer.unref();
  }
  #stopHeartbeatWatchdog() {
    if (this.#heartbeatTimer !== null) { this.#timers.clearTimeout(this.#heartbeatTimer); this.#heartbeatTimer = null; }
    if (this.#wakeObserver) {
      try { this.#observers.removeObserver(this.#wakeObserver, "wake_notification"); } catch {}
      this.#wakeObserver = null;
    }
  }
  fail(reason) {
    if (this.#ended) return;
    this.#ended = true; this.status = "failed";
    this.#stopHeartbeatWatchdog();
    // Fixed metadata only: no URLs, input, target IDs, profile paths or tokens.
    console.error("AXIOSOZO_CEF_DIAGNOSTIC", JSON.stringify({ code:reason, phase:this.readPhase,
      lastFrameId:this.lastFrame, lastFrameBytes:this.lastFrameBytes, targets:this.#targets.size,
      pendingRequests:this.#requests.size, nativeExitCode:this.nativeExitCode }));
    const error = new Error(reason);
    this.#readyWait?.reject(error);
    for (const waiter of this.#requests.values()) waiter.reject(error);
    this.#requests.clear();
    for (const adapter of [...this.#targets.values()]) adapter.hostFailed(error);
    this.#targets.clear();
    // Crash, protocol error or exit: the Mach endpoint and every binding end too.
    this.#closeSurface();
    this.#process.stdin.close().catch(() => {});
    this.#process.kill(500).catch(() => {});
    this.onFailure(error);
  }
  async shutdown() {
    if (this.#ended) return;
    try {
      const done = this.request("shutdown", {}, null);
      this.#closing = true;
      this.#stopHeartbeatWatchdog();
      await done;
      const result = await this.#process.wait();
      if (result.exitCode !== 0) throw new Error("CEF_SHUTDOWN_FAILED");
      this.#ended = true; this.status = "closed";
      this.#stopHeartbeatWatchdog();
      const unavailable = new Error("CEF_UNAVAILABLE");
      for (const waiter of this.#requests.values()) waiter.reject(unavailable);
      this.#requests.clear();
      for (const adapter of [...this.#targets.values()]) adapter.hostFailed(unavailable);
      this.#targets.clear();
      this.#closeSurface();
      await this.#process.stdin.close();
    } catch (error) { this.#closing = false; this.fail("CEF_CLOSE_OUTCOME_UNCERTAIN"); throw error; }
  }
}

/**
 * One Chromium target. Constructed with a process it owns a private
 * single-target connection (fixture probes); constructed with a shared
 * CEFHostConnection it is one tab of that host.
 */
export class CEFEngineAdapter {
  #host; #owned; #pending; #requests = new Set(); #inputQueue = [];
  #committedURLs = new Set(); #currentURL = null;
  #previousURL = null; #previousLoaded = false;
  #frameWait; #creating = null; #lastFrame = 0; #ended = false; #closing = false; #created = false; #loaded = false;
  // Native windowless frame rate of this target's browser: the hello rate (host
  // default 60) until a `frame_rate` command succeeds. It survives navigation.
  #frameRate = null;
  // Surface mode: the bound canvas, its endpoint target id, the generations chrome
  // confirmed to the endpoint, and whether the endpoint presented a first frame.
  #surfaceElement; #surfaceId = null; #confirmed = null; #surfacePresented = false;
  constructor(processOrHost, { token, pendingTarget, timers, deadline = 15000, browsingMode = "fixture",
    onEvent = () => {}, onFrame = () => {}, onFailure = () => {}, element = null, onGeometry = () => {} }) {
    this.#owned = processOrHost?.[HOST_BRAND] !== true;
    const mode = this.#owned ? browsingMode : processOrHost.browsingMode;
    if (!["fixture", "web"].includes(mode)) throw new Error("INVALID_CEF_MODE");
    this.browsingMode = mode;
    this.#pending = validateCEFTarget(pendingTarget, { pending: true, browsingMode: mode });
    this.#host = this.#owned ? new CEFHostConnection(processOrHost, { token, timers, deadline, browsingMode: mode,
      instance: this.#pending.engine_instance, identity: this.#pending.identity }) : processOrHost;
    if (!this.#owned && this.#pending.engine_instance !== this.#host.instance) throw new Error("FOREIGN_CEF_TARGET");
    this.onEvent = onEvent; this.onFrame = onFrame; this.onFailure = onFailure; this.onGeometry = onGeometry;
    this.#surfaceElement = element;
    this.target = null; this.surface = null; this.nativeClosed = false; this.geometry = null;
    this.status = this.#owned ? "starting" : "connected";
    this.#host.register(this);
  }
  get tabId() { return this.#pending.tab_id; }
  get instance() { return this.#pending.engine_instance; }
  get host() { return this.#host; }
  get readPhase() { return this.#host.readPhase; }
  get lastFrameBytes() { return this.#host.lastFrameBytes; }
  get nativeExitCode() { return this.#host.nativeExitCode; }
  /** GPU surface presentation (engine-surface-v1). False only in the labelled BGRA pipe fallback. */
  get surfaceMode() { return this.#host.surfaceMode === true; }
  get renderPath() { return this.#host.renderPath ?? RENDER_PATH_PIPE; }
  /** nsIAxioEngineSurfaceService input helpers; null in the pipe fallback. */
  get nativeInput() { return this.surfaceMode ? this.#host.nativeInput ?? null : null; }
  get maxSurfaceBytes() { return this.surfaceMode ? Infinity : MAX_PIXELS; }
  /**
   * Host input capabilities (cef-v1 "Input"; absent on older hosts): `key_verdict`
   * (key `down` completes with reason key_consumed/key_not_consumed), `wheel_phases`,
   * `pinch` and `ime` (text_input events plus ime_* commands).
   */
  get inputFeatures() {
    const capabilities = this.#host.capabilities ?? {};
    return Object.freeze({ keyVerdict: capabilities.key_verdict === true, wheelPhases: capabilities.wheel_phases === true,
      pinch: capabilities.pinch === true, ime: capabilities.ime === true });
  }
  surfaceStats() { return this.#surfaceId === null ? null : this.#host.surfaceStats(this.#surfaceId); }
  capabilities() {
    return Object.freeze({ version: 1, engine: "chromium", navigation: true,
      observation: ["url", "title", "loading", "favicon"], content_capture: false, developer_tools: false,
      fixture_only: this.browsingMode === "fixture", private_mode: false, ime: this.inputFeatures.ime,
      render_path: this.renderPath, experimental: true });
  }
  async connect() {
    if (this.#owned) await this.#host.connect();
    this.status = "connected";
    return this;
  }
  trackRequest(request_id) { this.#requests.add(request_id); }
  untrackRequest(request_id) { this.#requests.delete(request_id); }
  #request(method, fields = {}, target = this.target) {
    if (this.#ended || this.#closing) return Promise.reject(new Error("CEF_UNAVAILABLE"));
    if (method !== "close" && this.#requests.size >= MAX_INFLIGHT_REQUESTS) return Promise.reject(new Error("CEF_REQUEST_LIMIT"));
    return this.#host.request(method, fields, target, { owner: this });
  }
  pumpInput() {
    while (!this.#ended && !this.#closing && this.#requests.size < MAX_INFLIGHT_INPUT && this.#inputQueue.length) {
      const item = this.#inputQueue.shift();
      try { this.resolve(item.target); }
      catch (error) { item.reject(error); continue; }
      // This is the first and only dispatch of the validated input. A crash,
      // uncertain write, or reconnect rejects it; no mutating replay occurs.
      this.#request(item.method, item.fields, item.target).then(item.resolve, item.reject);
    }
  }
  #rejectQueuedInput(error) {
    for (const item of this.#inputQueue.splice(0)) item.reject(error);
  }
  #adopt(target, type) {
    const next = validateCEFTarget(target, { browsingMode: this.browsingMode });
    for (const key of ["tab_id", "engine_instance", "private_mode"]) {
      if (next[key] !== this.#pending[key]) throw new Error("FOREIGN_CEF_TARGET");
    }
    // A fixture host serves exactly one origin. A web target keeps the origin it
    // was created with as its identity; native revokes by generation instead.
    if (next.identity !== this.#pending.identity) throw new Error("FOREIGN_CEF_TARGET");
    if (type === "created") {
      if (this.#created || this.target || next.document_generation !== this.#pending.document_generation
          || next.navigation_generation !== this.#pending.navigation_generation) throw targetError("INVALID_CEF_CREATION");
      this.#created = true;
    } else {
      if (!this.target || next.native_target_id !== this.target.native_target_id) throw targetError("UNKNOWN_CEF_TARGET");
      if (type === "navigation") {
        if (next.document_generation !== this.target.document_generation + 1
            || next.navigation_generation !== this.target.navigation_generation + 1) throw targetError("INVALID_CEF_GENERATION");
        this.#previousURL = this.#currentURL; this.#previousLoaded = this.#loaded;
        this.#loaded = false;
      } else if (!sameCEFTarget(next, this.target)) throw targetError("STALE_CEF_EVENT");
    }
    this.target = next;
  }
  async acceptFrame(value, pixels) {
    if (!this.#created || !this.target) throw targetError("UNEXPECTED_CEF_FRAME");
    for (const field of ["tab_id", "engine_instance", "native_target_id", "identity", "private_mode"]) {
      if (value.target[field] !== this.target[field]) throw targetError("FOREIGN_CEF_FRAME");
    }
    if (value.target.document_generation > this.target.document_generation
        || value.target.navigation_generation > this.target.navigation_generation) throw targetError("FUTURE_CEF_FRAME");
    this.#lastFrame = value.frame_id;
    const same = sameCEFTarget(value.target, this.target);
    const sizeMatches = this.surface && value.width === Math.ceil(this.surface.width * this.surface.device_scale)
      && value.height === Math.ceil(this.surface.height * this.surface.device_scale)
      && value.device_scale === this.surface.device_scale;
    if (!same || !sizeMatches || !this.#loaded || this.#closing || this.#ended) return;
    this.#host.readPhase = "frame_present"; this.#host.lastFrameBytes = pixels.byteLength;
    try { await this.onFrame(value, pixels); }
    catch { throw targetError("CEF_FRAME_PRESENTATION_FAILED"); }
    this.#frameWait?.resolve(this.target);
  }
  /** onTargetGeometry from the endpoint: the first frame arrived, or its size/scale changed. */
  surfaceGeometry(geometry) {
    if (this.#ended || this.#surfaceId === null) return;
    const { width, height, logicalWidth, logicalHeight, scale } = geometry;
    if (![width, height, logicalWidth, logicalHeight].every(value => Number.isInteger(value) && value > 0 && value <= MAX_SURFACE_DIMENSION)
        || !Number.isFinite(scale) || scale <= 0 || scale > 4) throw targetError("CEF_SURFACE_BIND_FAILED");
    this.geometry = Object.freeze({ width, height, logicalWidth, logicalHeight, scale });
    this.#surfacePresented = true;
    try { this.onGeometry(this.geometry); }
    catch { throw targetError("CEF_FRAME_PRESENTATION_FAILED"); }
    this.#surfaceFirstFrame();
  }
  #surfaceFirstFrame() {
    // Parity with acceptFrame(): a live presented frame of the loaded, current target.
    if (this.#surfacePresented && this.#loaded && this.target && !this.#closing) this.#frameWait?.resolve(this.target);
  }
  /**
   * Frames of a newer generation stay held in the endpoint until chrome has
   * processed that navigation (its url and load events), so page pixels never
   * precede the address Zen shows. Mirrors acceptFrame()'s `same && loaded` rule.
   */
  #confirmSurface() {
    if (this.#surfaceId === null || !this.target || !this.#loaded || this.#closing || this.#ended) return;
    const { document_generation, navigation_generation } = this.target;
    if (this.#confirmed?.document_generation === document_generation
        && this.#confirmed?.navigation_generation === navigation_generation) return;
    this.#host.confirmSurface(this.#surfaceId, this.target);
    this.#confirmed = { document_generation, navigation_generation };
  }
  acceptEvent(value) {
    if (value.target) {
      this.#adopt(value.target, value.event);
      if (value.event === "navigation") this.pumpInput();
      if (value.event === "created" && this.surfaceMode) {
        // Bound before the host may paint (it paints only after load end), with the
        // created target's generations; later generations wait for #confirmSurface.
        this.#surfaceId = this.#host.bindSurface(this, this.#surfaceElement, this.target);
        this.#confirmed = { document_generation: this.target.document_generation,
          navigation_generation: this.target.navigation_generation };
      }
    }
    if (value.event === "title" && (typeof value.title !== "string" || value.title.length > 1024)) throw targetError("INVALID_CEF_TITLE");
    if (value.event === "loading" && ![value.loading, value.can_go_back, value.can_go_forward].every(item => typeof item === "boolean")) throw targetError("INVALID_CEF_LOADING");
    if (value.event === "cursor" && !CURSORS.has(value.cursor)) throw targetError("INVALID_CEF_CURSOR");
    if (value.event === "favicon" && !validFavicon(value.icon)) throw targetError("INVALID_CEF_FAVICON");
    if (value.event === "open_url" && (this.browsingMode !== "web" || !allowedWebURL(value.url)
        || value.url === "about:blank" || typeof value.background !== "boolean")) throw targetError("INVALID_CEF_OPEN_URL");
    if (value.event === "text_input" && (!this.inputFeatures.ime || !["none", "text", "password"].includes(value.mode)
        || ![value.caret_x, value.caret_y, value.caret_width, value.caret_height]
          .every(number => Number.isFinite(number) && number >= 0 && number <= 16384))) throw targetError("INVALID_CEF_TEXT_INPUT");
    if (AX_EVENTS.has(value.event)) {
      if (this.#host.capabilities?.accessibility !== true) throw targetError("INVALID_CEF_ACCESSIBILITY");
      try { (value.event === "ax_tree_update" ? validateAXTreeUpdate : validateAXLocation)(value); }
      catch { throw targetError("INVALID_CEF_ACCESSIBILITY"); }
    }
    // >>> AxioSozo engine UI delegation: web sessions only, strict schema
    if (DELEGATION_EVENTS.has(value.event)) {
      if (this.browsingMode !== "web") throw targetError("INVALID_CEF_PROMPT");
      try { validateDelegationEvent(value); } catch { throw targetError("INVALID_CEF_PROMPT"); }
    }
    // <<<
    if (value.event === "url") {
      if (!this.allowedURL(value.url)) throw targetError("INVALID_CEF_URL");
      this.#currentURL = value.url;
    }
    if (value.event === "load") {
      if (!Number.isInteger(value.http_status) || typeof value.restored_from_history !== "boolean") throw targetError("INVALID_CEF_LOAD");
      const pendingHistory = this.#pendingHistory;
      const restored = value.http_status === 0 && value.restored_from_history && pendingHistory && this.#committedURLs.has(this.#currentURL);
      if (value.restored_from_history && !restored) throw targetError("INVALID_CEF_HISTORY_RESTORE");
      const sameDocument = value.same_document === true && this.browsingMode === "web"
        && value.http_status === 0 && !value.restored_from_history && this.#previousLoaded
        && allowedWebURL(this.#previousURL) && allowedWebURL(this.#currentURL)
        && new URL(this.#previousURL).origin === new URL(this.#currentURL).origin;
      if (value.same_document !== undefined && !sameDocument) throw targetError("INVALID_CEF_LOAD");
      const loaded = this.browsingMode === "web"
        ? (value.http_status >= 200 && value.http_status <= 599)
          || (value.http_status === 0 && this.#currentURL === "about:blank" && !value.restored_from_history)
        : value.http_status === 200;
      this.#loaded = loaded || restored || sameDocument;
      if ((loaded || sameDocument) && this.#currentURL) {
        this.#committedURLs.add(this.#currentURL);
        // Bound history verification memory; old HTTP0 restores fail closed.
        if (this.#committedURLs.size > 1024) this.#committedURLs.delete(this.#committedURLs.values().next().value);
      }
      if (!this.#loaded) this.#frameWait?.reject(new Error("CEF_FIXTURE_LOAD_FAILED"));
    }
    if (value.event === "closed") this.nativeClosed = true;
    try { this.onEvent(value); }
    catch { throw targetError("CEF_EVENT_CALLBACK_FAILED"); }
    // After chrome handled the event (address bar, title): release held frames.
    if (value.event === "load") { this.#confirmSurface(); this.#surfaceFirstFrame(); }
  }
  #historyMethods = new Set();
  get #pendingHistory() { return this.#historyMethods.size > 0; }
  resolve(target) {
    if (!this.target || !sameCEFTarget(validateCEFTarget(target, { browsingMode: this.browsingMode }), this.target)) throw new Error("STALE_CEF_TARGET");
    if (this.#ended || this.#closing) throw new Error("CEF_UNAVAILABLE");
    return this.target;
  }
  async create(url, surface) {
    if (this.#created || this.status !== "connected" || !this.allowedURL(url)) throw new Error("INVALID_CEF_CREATE");
    this.surface = validateSurface(surface, { maxBytes: this.maxSurfaceBytes });
    this.#frameWait = this.#host.waiter("CEF_FIRST_FRAME", code => this.fail(code));
    this.#creating = this.#host.serializeCreate(this.tabId, () => this.#request("create", { url, ...this.surface }, this.#pending));
    const result = await this.#creating;
    if (result.status !== "success") {
      // A host that still applies the pipe's byte bound refuses large surfaces
      // before creating anything; the caller may retry once with a bounded scale.
      const error = new Error(result.status === "unsupported" && result.reason === "surface_limit"
        ? "CEF_SURFACE_LIMIT" : "CEF_CREATE_UNSUPPORTED_OR_FAILED");
      if (!this.#created) { this.#frameWait.reject(error); this.#creating = null; }
      throw error;
    }
    // Acceptance/creation alone cannot replace the original Gecko presentation.
    await this.#frameWait.promise;
    this.status = "active";
    return this.target;
  }
  navigate(target, url) {
    this.resolve(target);
    if (!this.allowedURL(url)) return Promise.resolve({ status: "unsupported", reason: "UNSUPPORTED_URL" });
    return this.#request("navigate", { url });
  }
  allowedURL(url) { return this.browsingMode === "web" ? allowedWebURL(url) : allowedFixtureURL(url, this.#pending.identity); }
  #history(method) {
    // An HTTP0 history restore is legitimate only while this explicit request
    // is outstanding; the marker is cleared however the request settles.
    const marker = Symbol(method);
    this.#historyMethods.add(marker);
    const settle = () => this.#historyMethods.delete(marker);
    const result = this.#request(method);
    result.then(settle, settle);
    return result;
  }
  back(target) { this.resolve(target); return this.#history("back"); }
  forward(target) { this.resolve(target); return this.#history("forward"); }
  reload(target) { this.resolve(target); return this.#request("reload"); }
  stop(target) {
    this.resolve(target);
    if (this.#host.capabilities?.stop !== true) return Promise.resolve({ status: "unsupported", reason: "STOP_UNSUPPORTED" });
    return this.#request("stop");
  }
  developerTools() { return Promise.resolve({ status: "unsupported", reason: "DEVTOOLS_NOT_INTEGRATED" }); }
  // >>> AxioSozo engine UI delegation
  /**
   * One Zen UI answer or UI command (prompt replies, find, zoom, download control)
   * for exactly `target`: a navigation since the prompt makes it STALE_CEF_TARGET.
   */
  reply(target, method, fields) {
    this.resolve(target);
    if (this.browsingMode !== "web") return Promise.resolve({ status: "unsupported", reason: "FIXTURE_ONLY" });
    return this.#request(method, validateDelegationCommand(method, fields), target);
  }
  // <<<
  resize(target, surface) {
    this.resolve(target);
    const next = validateSurface(surface, { maxBytes: this.maxSurfaceBytes }), previous = this.surface;
    this.surface = next;
    return this.#request("resize", next).then(result => {
      if (result.status !== "success") this.surface = previous;
      return result;
    }, error => { this.surface = previous; throw error; });
  }
  /** Rate this target's native browser runs at (hello rate until changed). */
  get appliedFrameRate() { return this.#frameRate ?? this.#host.frameRate ?? 60; }
  /** Display refresh class for this target's window now; null when unknown. */
  get displayFrameRate() { return this.#host.displayFrameRate ?? null; }
  /**
   * cef-v1 `frame_rate` (capability `frame_rate_command`): the window's display
   * refresh class changed. Live `set_windowless_frame_rate` for this target only.
   */
  frameRate(target, rate) {
    this.resolve(target);
    if (!FRAME_RATES.includes(rate)) throw new Error("INVALID_FRAME_RATE");
    if (this.#host.capabilities?.frame_rate_command !== true) return Promise.resolve({ status: "unsupported", reason: "FRAME_RATE_UNSUPPORTED" });
    return this.#request("frame_rate", { frame_rate: rate }).then(result => {
      if (result?.status === "success") this.#frameRate = rate;
      return result;
    });
  }
  focus(target, focused) {
    this.resolve(target);
    if (typeof focused !== "boolean") throw new Error("INVALID_FOCUS");
    return this.#request("focus", { focused });
  }
  visibility(target, visible) {
    this.resolve(target);
    if (typeof visible !== "boolean") throw new Error("INVALID_VISIBILITY");
    // Begin-frame gating first, so a shown target gets ticks as native unhides it.
    if (this.#surfaceId !== null) this.#host.surfaceVisible(this.#surfaceId, visible);
    if (this.browsingMode !== "web") {
      return Promise.resolve(this.#surfaceId !== null ? { status: "success", reason: "SURFACE_ONLY" }
        : { status: "unsupported", reason: "FIXTURE_ONLY" });
    }
    return this.#request("visibility", { visible });
  }
  // >>> Accessibility (docs/design/engine-accessibility.md §6)
  /** `accessibility {enabled}` for the current target (TreeOnly renderer accessibility). */
  accessibility(enabled) {
    const fields = validateAXCommand("accessibility", { enabled });
    if (this.#host.capabilities?.accessibility !== true) return Promise.resolve({ status: "unsupported", reason: "ACCESSIBILITY_UNSUPPORTED" });
    return this.#request("accessibility", fields);
  }
  /** One assistive action on a host wire node id of the current target. */
  axAction(node_id, action, value) {
    const fields = validateAXCommand("ax_action", value === undefined ? { node_id, action } : { node_id, action, value });
    if (this.#host.capabilities?.accessibility !== true) return Promise.resolve({ status: "unsupported", reason: "ACCESSIBILITY_UNSUPPORTED" });
    return this.#request("ax_action", fields);
  }
  /** Credit for one applied ax event; like frame_ack it names that event's exact target. */
  axAck(target, seq) {
    if (!Number.isSafeInteger(seq) || seq < 1) return Promise.reject(new Error("INVALID_AX_ACK"));
    if (this.#ended) return Promise.resolve();
    return this.#host.request("ax_ack", { seq }, target, { acknowledge: true });
  }
  // <<<
  edit(target, action) {
    this.resolve(target);
    if (!["copy", "cut", "paste", "select_all", "undo", "redo"].includes(action)) throw new Error("INVALID_EDIT_ACTION");
    if (this.browsingMode !== "web") return Promise.resolve({ status: "unsupported", reason: "FIXTURE_ONLY" });
    return this.#request("edit", { action });
  }
  input(target, method, fields) {
    this.resolve(target);
    const validated = validateCEFInput(method, fields, this.surface, this.inputFeatures);
    if (this.#inputQueue.length >= MAX_QUEUED_INPUT) {
      // Further input could strand key-up or pointer-up after an earlier down.
      // Stop this experimental engine rather than drop a mutating command and
      // keep presenting a seemingly healthy Chromium surface.
      this.fail("CEF_INPUT_BACKPRESSURE");
      return Promise.reject(new Error("CEF_INPUT_BACKPRESSURE"));
    }
    return new Promise((resolve, reject) => {
      this.#inputQueue.push({ target, method, fields:validated, resolve, reject });
      this.pumpInput();
    });
  }
  /** Ends this target. A private connection ends with it; a shared host continues. */
  fail(reason) {
    if (this.#ended) return;
    if (this.#owned) { this.#host.fail(reason); return; }
    const target = this.#created ? this.target : null;
    const error = this.#end(reason, "failed");
    // Native revokes the target; a later target of this tab waits for that close.
    if (target && !this.#host.ended) this.#host.retire(this.tabId, this.#host.request("close", {}, target));
    this.onFailure(error);
  }
  hostFailed(error) {
    if (this.#ended) return;
    this.#end(error.message, error.message === "CEF_UNAVAILABLE" ? "closed" : "failed");
    if (this.status === "failed") this.onFailure(error);
  }
  #end(reason, status) {
    this.#ended = true; this.status = status;
    const error = new Error(reason);
    if (this.#surfaceId !== null) { this.#host.unbindSurface(this.#surfaceId, this); this.#surfaceId = null; }
    this.#frameWait?.reject(error);
    this.#host.rejectRequestsOf(this, error);
    this.#rejectQueuedInput(error);
    this.#host.unregister(this);
    return error;
  }
  async close() {
    if (this.#ended) return;
    if (this.#owned) {
      try { await this.#host.shutdown(); }
      finally { if (!this.#ended) this.#end("CEF_UNAVAILABLE", this.#host.status === "closed" ? "closed" : "failed"); }
      return;
    }
    try {
      // A create already sent completes first; its target is then closed too.
      if (!this.#created && this.#creating) await this.#creating.catch(() => {});
      if (this.#created && this.target && !this.#ended) {
        const done = this.#request("close", {}, this.target);
        this.#closing = true;
        this.#host.retire(this.tabId, done);
        await done;
      }
    } finally { if (!this.#ended) this.#end("CEF_UNAVAILABLE", "closed"); }
  }
}

let sharedHost = null;
/**
 * The one web host of this Firefox process and profile. Import this module with
 * ChromeUtils.importESModule so every browser window shares it. Its Chromium
 * profile is persistent beside the Zen profile and never contains Firefox data.
 */
export function chromiumHost() {
  if (!sharedHost || sharedHost.failed) {
    // A persistent Chromium profile has one owner: wait for a failed host to exit.
    const previous = sharedHost?.connection?.exited ?? Promise.resolve();
    const connecting = previous.then(() => launchHost("web")).then(host => {
      entry.connection = host;
      host.onFailure = () => { entry.failed = true; };
      return host;
    }, error => { entry.failed = true; throw error; });
    const entry = { connecting, connection: null, failed: false };
    sharedHost = entry;
  }
  return sharedHost.connecting;
}

async function launchHost(browsingMode, origin = BLANK_IDENTITY) {
  const { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
  const timers = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");
  const root = Services.env.get("AXIOSOZO_BUILD_ROOT");
  const session = Services.env.get("AXIOSOZO_SESSION_RUNTIME");
  const command = Services.env.get("AXIOSOZO_CEF_BINARY");
  // The CEF host is built once at the volume root; a named sub build root
  // (e.g. /Volumes/AxioSozoBuild/workstation) shares it.
  const cefRoot = Services.env.get("AXIOSOZO_CEF_ROOT") || root;
  if (!validCEFRoot(root, cefRoot) || command !== `${cefRoot}/cef/AxioCEFProbe.app/Contents/MacOS/AxioCEFProbe`
      || !validCEFSessionRuntime(root, session, Services.dirsvc.get("ProfD", Ci.nsIFile).path)
      || !(browsingMode === "web"
        ? Services.env.get("AXIOSOZO_ENGINE_SWITCHING") === "1" && origin === BLANK_IDENTITY
        : browsingMode === "fixture" && origin === Services.env.get("AXIOSOZO_ENGINE_FIXTURE_ORIGIN") && validFixtureOrigin(origin))) {
    throw new Error("CEF_PROJECT_RUNTIME_UNAVAILABLE");
  }
  const instance = Services.uuid.generateUUID().toString().replace(/[{}]/gu, "");
  // Web: one persistent Chromium profile per Zen session profile. Fixture
  // probes: a private profile removed when its process exits.
  const profile = browsingMode === "web" ? `${session}/chromium` : `${session}/cef-${instance}`;
  await IOUtils.makeDirectory(profile, { permissions: 0o700, createAncestors: false, ignoreExisting: browsingMode === "web" });
  const random = Cc["@mozilla.org/security/random-generator;1"].getService(Ci.nsIRandomGenerator).generateRandomBytes(32);
  const token = [...random].map(value => value.toString(16).padStart(2, "0")).join("");
  // GPU surface path (engine-surface-v1): the endpoint exists before the host is
  // spawned, so the host can connect before CEF starts. Without the engine-view
  // component this build uses the BGRA pipe fallback, and says so.
  const service = engineSurfaceService();
  let endpoint = null;
  try { endpoint = service?.createEndpoint(true) ?? null; } catch { endpoint = null; }
  if (!endpoint) {
    console.warn("AXIOSOZO_CEF_RENDER_PATH", JSON.stringify({ renderPath: RENDER_PATH_PIPE,
      reason: service ? "surface_endpoint_failed" : "surface_service_unavailable" }));
  }
  let process;
  try {
    process = await Subprocess.call({ command, arguments: ["--stream", profile, profile],
      environmentAppend: false, environment: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", TMPDIR: profile }, stderr: "pipe" });
  } catch (error) {
    try { endpoint?.close(); } catch {}
    if (browsingMode !== "web") await IOUtils.remove(profile, { recursive: true });
    throw error;
  }
  if (browsingMode !== "web") {
    // CEF owns this directory until its process exits. Remove only the generated
    // path, on normal close or crash; never remove another session's profile.
    const cleanup = () => IOUtils.remove(profile, { recursive: true });
    process.wait().then(cleanup, cleanup).catch(() => {});
  }
  const host = new CEFHostConnection(process, { token, instance, identity: origin, timers, browsingMode,
    shared: browsingMode === "web", surface: endpoint ? { endpoint, service, frameRate: surfaceFrameRate(service) } : null });
  try { return await host.connect(); }
  catch (error) { await host.shutdown().catch(() => {}); throw error; }
}

/**
 * Attach one Zen tab as a Chromium target. Web tabs share the profile's host;
 * the fixture probe keeps its own strict single-origin host per tab.
 */
export async function launchCEF(win, { tabId, origin, browsingMode = "fixture", onEvent, onFrame, onFailure,
  element = null, onGeometry }) {
  if (!id(tabId)) throw new Error("CEF_PROJECT_RUNTIME_UNAVAILABLE");
  if (browsingMode === "web") {
    if (!validWebOrigin(origin)) throw new Error("CEF_PROJECT_RUNTIME_UNAVAILABLE");
    // Every window shares the process-wide module instance and its one host.
    const shared = ChromeUtils.importESModule("chrome://browser/content/axiosozo/CEFEngineAdapter.sys.mjs");
    const host = await shared.chromiumHost();
    return new shared.CEFEngineAdapter(host, { pendingTarget: { tab_id: tabId, engine_instance: host.instance, identity: origin,
      document_generation: 1, navigation_generation: 1, private_mode: false }, onEvent, onFrame, onFailure, element, onGeometry });
  }
  const host = await launchHost("fixture", origin);
  const adapter = new CEFEngineAdapter(host, { pendingTarget: { tab_id: tabId, engine_instance: host.instance,
    identity: origin, document_generation: 1, navigation_generation: 1, private_mode: false }, onEvent, onFrame, onFailure,
    element, onGeometry });
  // A fixture host exists for exactly this one tab.
  const close = adapter.close.bind(adapter);
  adapter.close = async () => { try { await close(); } finally { await host.shutdown().catch(() => {}); } };
  return adapter;
}
