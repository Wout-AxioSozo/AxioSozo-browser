/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. https://mozilla.org/MPL/2.0/ */

// P6 (Plan 4): the narrow new-tab hook. The one hash-pinned overlay
// replacement of Zen's browser-commands-js.patch is applied here as an
// injected fixture (exact bytes, exact hashes, every other record kept), and
// the patched hunk is applied to the exact pinned native
// BrowserCommands.openTab, which then runs with fake Zen and Firefox globals.
// The window's gate (AxioSozoServices.createStartPageGate) reads every fact on
// each command. No source patch, build or browser run; root validates the
// native menu, Cmd+T and new-tab button in fresh synthetic profiles.
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { contextsCoreAvailable } from "./support/chrome-modules.mjs";
import { fakeZenWindow } from "./support/fake-zen.mjs";

const skip = contextsCoreAvailable ? false : "packages/contexts/src/index.mjs is absent";
const { createStartPageGate, START_PAGE_URL, START_PAGE_PREF, AxioSozoServices } = skip ? {} : await import("../chrome/AxioSozoServices.sys.mjs");
const { ZenWorkspaceAdapter } = await import("../chrome/ZenWorkspaceAdapter.sys.mjs");

const TARGET = "src/browser/base/content/browser-commands-js.patch";
const BEFORE = "16651739183379aca7213be3e7dbebc33f3f1b8f31aba29b790a1bfec1dcc631";
const overlay = JSON.parse(readFileSync(new URL("../../../patches/zen/overlay.json", import.meta.url), "utf8"));
const sha256 = text => createHash("sha256").update(text, "utf8").digest("hex");
const UPSTREAM = new URL(`../../../upstream/zen/${TARGET}`, import.meta.url);

// The pinned upstream file, byte for byte (its sha256 is BEFORE).
const ORIGINAL = [
  "diff --git a/browser/base/content/browser-commands.js b/browser/base/content/browser-commands.js",
  "index 757c5b16612c96f327798f9e47c83144668074f0..1a10a321ee05f2ec84f33f087d8c0e43035fc18b 100644",
  "--- a/browser/base/content/browser-commands.js",
  "+++ b/browser/base/content/browser-commands.js",
  "@@ -13,6 +13,10 @@ var BrowserCommands = {",
  "     const where = BrowserUtils.whereToOpenLink(aEvent, false, true);",
  " ",
  "     if (where == \"current\") {",
  "+      if (!gBrowser.webNavigation.canGoBack && gZenCommonActions.shouldCloseTabOnBack()) {",
  "+        gBrowser.removeTab(gBrowser.selectedTab, { animate: true });",
  "+        return;",
  "+      }",
  "       try {",
  "         gBrowser.goBack();",
  "       } catch (ex) {}",
  "@@ -252,6 +256,10 @@ var BrowserCommands = {",
  "       }",
  "     }",
  " ",
  "+    if (gZenUIManager.handleNewTab(werePassedURL, searchClipboard, where)) {",
  "+      return;",
  "+    }",
  "+",
  "     // A notification intended to be useful for modular peformance tracking",
  "     // starting as close as is reasonably possible to the time when the user",
  "     // expressed the intent to open a new tab.  Since there are a lot of",
  "@@ -374,6 +382,14 @@ var BrowserCommands = {",
  "       return;",
  "     }",
  " ",
  "+    if (gBrowser.selectedTab.hasAttribute(\"zen-empty-tab\")) {",
  "+      if (gBrowser.selectedTab.hasAttribute(\"split-view\")) {",
  "+        return;",
  "+      }",
  "+      gZenWorkspaces.handleTabCloseWindow();",
  "+      return;",
  "+    }",
  "+",
  "     // Keyboard shortcuts that would close a tab that is pinned select the first",
  "     // visible unpinned tab instead.",
  "     if (",
  "",
].join("\n");

// The native BrowserCommands.openTab as Zen's patch leaves it, byte for byte
// (engine/browser/base/content/browser-commands.js lines 232-294 of the pinned
// source whose sha256 is NATIVE.sha256).
const NATIVE = {
  path: "/Volumes/AxioSozoBuild/workstation/zen/source/engine/browser/base/content/browser-commands.js",
  sha256: "eb16be6fbcdc7706edae8697e2e2c71518eef0f604e970ccb055bc9e7b2985f9",
};
const NATIVE_OPEN_TAB = [
  "  openTab({ event, url } = {}) {",
  "    let werePassedURL = !!url;",
  "    url ??= BROWSER_NEW_TAB_URL;",
  "    let searchClipboard =",
  "      event?.button == 1 &&",
  "      Services.prefs.getBoolPref(\"middlemouse.paste\") &&",
  "      gMiddleClickNewTabUsesPasteboard;",
  "",
  "    let relatedToCurrent = false;",
  "    let where = \"tab\";",
  "",
  "    if (event) {",
  "      where = BrowserUtils.whereToOpenLink(event, false, true);",
  "",
  "      switch (where) {",
  "        case \"tab\":",
  "        case \"tabshifted\":",
  "          // When accel-click or middle-click are used, open the new tab as",
  "          // related to the current tab.",
  "          relatedToCurrent = true;",
  "          break;",
  "        case \"current\":",
  "          where = \"tab\";",
  "          break;",
  "      }",
  "    }",
  "",
  "    if (gZenUIManager.handleNewTab(werePassedURL, searchClipboard, where)) {",
  "      return;",
  "    }",
  "",
  "    // A notification intended to be useful for modular peformance tracking",
  "    // starting as close as is reasonably possible to the time when the user",
  "    // expressed the intent to open a new tab.  Since there are a lot of",
  "    // entry points, this won't catch every single tab created, but most",
  "    // initiated by the user should go through here.",
  "    //",
  "    // Note 1: This notification gets notified with a promise that resolves",
  "    //         with the linked browser when the tab gets created",
  "    // Note 2: This is also used to notify a user that an extension has changed",
  "    //         the New Tab page.",
  "    Services.obs.notifyObservers(",
  "      {",
  "        wrappedJSObject: new Promise(resolve => {",
  "          let options = {",
  "            relatedToCurrent,",
  "            resolveOnNewTabCreated: resolve,",
  "          };",
  "          if (!werePassedURL && searchClipboard) {",
  "            let clipboard = readFromClipboard();",
  "            clipboard =",
  "              UrlbarShared.stripUnsafeProtocolOnPaste(clipboard).trim();",
  "            if (clipboard) {",
  "              url = clipboard;",
  "              options.allowThirdPartyFixup = true;",
  "            }",
  "          }",
  "          openTrustedLinkIn(url, where, options);",
  "        }),",
  "      },",
  "      \"browser-open-newtab-start\"",
  "    );",
  "  },",
].join("\n");

/** scripts/zen.py apply_records, for one record: every old text exactly once, in order. */
function apply(text, record) {
  let out = text;
  for (const [old, next] of record.replacements) {
    assert.equal(out.split(old).length - 1, 1, `exactly one: ${old.slice(0, 60)}`);
    out = out.replace(old, () => next);
  }
  return out;
}
/** scripts/zen.py revert_records: every new text exactly once, in reverse. */
function revert(text, record) {
  let out = text;
  for (const [old, next] of [...record.replacements].reverse()) {
    assert.equal(out.split(next).length - 1, 1, `exactly one: ${next.slice(0, 60)}`);
    out = out.replace(next, () => old);
  }
  return out;
}
const record = () => overlay.find(item => item.path === TARGET);
/** The lines the openTab hunk of a patch adds, as they appear in the native file. */
function zenBlock(patch) {
  const lines = patch.split("\n");
  const start = lines.findIndex(line => line.startsWith("@@ -252,6"));
  const end = lines.findIndex((line, index) => index > start && line.startsWith("@@ "));
  return lines.slice(start + 1, end).filter(line => line.startsWith("+")).map(line => line.slice(1)).join("\n");
}

test("overlay: one hash-pinned record replaces the existing Zen patch target; every other record is kept", () => {
  const records = overlay.filter(item => item.path === TARGET);
  assert.equal(records.length, 1);
  assert.equal(records[0].before_sha256, BEFORE);
  assert.equal(records[0].when, undefined, "always applied");
  assert.deepEqual(overlay.map(item => item.path), ["src/zen/common/ZenPreloadedScripts.js", "src/zen/common/jar.inc.mn", "configs/common/mozconfig",
    "surfer.json", "package.json", "package-lock.json", "src/zen/toolkit/common/cocoa/ZenShareInternal.mm", "src/zen/moz.build", TARGET]);
  assert.equal(overlay.find(item => item.path === "src/zen/moz.build").when, "native");
  assert.equal(sha256(ORIGINAL), BEFORE, "the embedded fixture is the pinned upstream file");
  if (existsSync(UPSTREAM)) assert.equal(readFileSync(UPSTREAM, "utf8"), ORIGINAL, "and matches this worktree's upstream copy");
  // The embedded openTab is the pinned native source (read only when that
  // exact pinned file is present on this machine).
  if (existsSync(NATIVE.path)) {
    const native = readFileSync(NATIVE.path, "utf8");
    if (sha256(native) === NATIVE.sha256) assert.equal(native.split(NATIVE_OPEN_TAB).length - 1, 1, "the embedded openTab is the native one");
  }
  assert.equal(NATIVE_OPEN_TAB.split(zenBlock(ORIGINAL)).length - 1, 1, "Zen's block appears once in the native openTab");
});

test("overlay: applying gives exactly the pinned result, reverting gives the original; only the openTab hunk and two later headers change", () => {
  const after = apply(ORIGINAL, record());
  assert.equal(sha256(after), record().after_sha256, `after_sha256 must be ${sha256(after)}`);
  assert.equal(revert(after, record()), ORIGINAL);
  const before = ORIGINAL.split("\n"), lines = after.split("\n");
  assert.equal(lines.length - before.length, 13, "13 added lines, all inside the openTab hunk");
  assert.deepEqual(lines.filter(line => line.startsWith("@@")), ["@@ -13,6 +13,10 @@ var BrowserCommands = {", "@@ -252,6 +256,23 @@ var BrowserCommands = {",
    "@@ -374,6 +395,14 @@ var BrowserCommands = {"]);
  // Each hunk's counts still match its body (git apply checks them).
  const hunks = after.split(/^(?=@@ )/mu).slice(1);
  for (const hunk of hunks) {
    const [, oldCount, newCount] = /^@@ -\d+,(\d+) \+\d+,(\d+) @@/u.exec(hunk);
    const body = hunk.split("\n").slice(1).filter(line => line !== "");
    assert.equal(body.filter(line => !line.startsWith("+")).length, Number(oldCount), hunk.split("\n")[0]);
    assert.equal(body.filter(line => !line.startsWith("-")).length, Number(newCount), hunk.split("\n")[0]);
  }
  const added = lines.filter(line => line.startsWith("+") && !before.includes(line));
  const code = added.join("\n");
  assert.match(code, /window\.AxioSozo\?\.startPageForNewTab\?\.\(\)/u);
  assert.ok(lines.includes("+    } else if (gZenUIManager.handleNewTab(werePassedURL, searchClipboard, where)) {"), "Zen's own branch, unchanged");
  assert.doesNotMatch(code, /BrowserCommands\.openTab|openTab\(|AboutNewTab|BROWSER_NEW_TAB_URL\s*=|Services\.prefs|newtab\.url|homepage|BrowserExperience/u,
    "no recursion, global new-tab override or preference write");
});

// The exact native openTab with a patch's openTab hunk applied (Zen's own
// block for the pinned patch, the AxioSozo guard for the overlaid one), run
// with fake Firefox and Zen globals.
function patchedOpenTab(patch) {
  const zen = zenBlock(ORIGINAL);
  assert.equal(NATIVE_OPEN_TAB.split(zen).length - 1, 1);
  const source = NATIVE_OPEN_TAB.replace(zen, () => zenBlock(patch));
  // eslint-disable-next-line no-new-func -- test-only evaluation of the pinned native method and the overlay's own lines
  return new Function("env", `with (env) { return ({\n${source}\n}).openTab; }`);
}

function browser({ answer = () => "about:axiosozo#home", consume = false, newTab = "about:newtab", clipboard = "https://pasted.example/",
  middlePaste = true, pasteboard = true, patch = apply(ORIGINAL, record()) } = {}) {
  const log = { asked: 0, zen: [], opened: [], notified: [], prefs: [] };
  const prefs = new Map([["middlemouse.paste", middlePaste]]);
  const env = {
    window: { AxioSozo: { startPageForNewTab: () => { log.asked++; return answer(); } } },
    BROWSER_NEW_TAB_URL: newTab, gMiddleClickNewTabUsesPasteboard: pasteboard,
    BrowserUtils: { whereToOpenLink: event => event.where ?? "tab" },
    gZenUIManager: { handleNewTab: (...args) => { log.zen.push(args); return consume; } },
    Services: {
      obs: { notifyObservers: (_subject, topic) => log.notified.push(topic) },
      // A missing pref without a default throws, as natively.
      prefs: { getBoolPref: name => { log.prefs.push(name); if (!prefs.has(name)) throw new Error("NS_ERROR_UNEXPECTED"); return prefs.get(name); } },
    },
    readFromClipboard: () => clipboard, UrlbarShared: { stripUnsafeProtocolOnPaste: text => text.replace(/^javascript:/iu, "") },
    openTrustedLinkIn: (url, where, options) => log.opened.push({ url, where, related: options.relatedToCurrent, fixup: options.allowThirdPartyFixup ?? false }),
  };
  return { env, log, openTab: patchedOpenTab(patch)(env) };
}

test("hook: an accepted ordinary command opens only the fixed start page once and skips only Zen's consume branch", () => {
  const b = browser();
  b.openTab();
  assert.deepEqual(b.log.opened, [{ url: "about:axiosozo#home", where: "tab", related: false, fixup: false }]);
  assert.deepEqual([b.log.asked, b.log.zen.length, b.log.notified], [1, 0, ["browser-open-newtab-start"]], "the native notification still fires");
  const accel = browser();
  accel.openTab({ event: { where: "current" } });
  assert.deepEqual(accel.log.opened, [{ url: "about:axiosozo#home", where: "tab", related: false, fixup: false }], "current becomes tab, as natively");
  const middle = browser({ middlePaste: false });
  middle.openTab({ event: { where: "tab", button: 1 } });
  assert.deepEqual([middle.log.asked, middle.log.opened], [1, [{ url: "about:axiosozo#home", where: "tab", related: true, fixup: false }]],
    "a middle click without paste is an ordinary command, related to the current tab as natively");
});

test("hook: the unpatched Zen file, run through the same native method, behaves as stock Zen", () => {
  const stock = browser({ patch: ORIGINAL });
  stock.openTab();
  assert.deepEqual([stock.log.asked, stock.log.zen, stock.log.opened], [0, [[false, false, "tab"]], [{ url: "about:newtab", where: "tab", related: false, fixup: false }]]);
  const consumed = browser({ patch: ORIGINAL, consume: true });
  consumed.openTab();
  assert.deepEqual([consumed.log.opened, consumed.log.notified], [[], []]);
});

test("hook: a null, other, throwing or missing answer leaves the native branch; Zen may still consume it into its URL bar", () => {
  for (const [label, setup] of [["null", b => { b.env.window.AxioSozo.startPageForNewTab = () => null; }],
    ["another URL", b => { b.env.window.AxioSozo.startPageForNewTab = () => "https://elsewhere.example/"; }],
    ["throws", b => { b.env.window.AxioSozo.startPageForNewTab = () => { throw new Error("x"); }; }],
    ["no AxioSozo", b => { b.env.window.AxioSozo = undefined; }],
    ["no callback", b => { b.env.window.AxioSozo = {}; }]]) {
    const consumed = browser({ consume: true });
    setup(consumed);
    consumed.openTab();
    assert.deepEqual([consumed.log.zen.length, consumed.log.opened], [1, []], `${label}: stock Zen's URL bar takes it`);
    const native = browser({ consume: false });
    setup(native);
    native.openTab();
    assert.deepEqual(native.log.opened, [{ url: "about:newtab", where: "tab", related: false, fixup: false }], `${label}: the native default`);
  }
});

test("hook: explicit URLs, clipboard paste and other event destinations are never asked and stay native", () => {
  const explicit = browser();
  explicit.openTab({ url: "https://explicit.example/" });
  assert.deepEqual([explicit.log.asked, explicit.log.opened[0].url, explicit.log.zen], [0, "https://explicit.example/", [[true, false, "tab"]]]);
  const explicitDefault = browser();
  explicitDefault.openTab({ url: "about:newtab" });
  assert.deepEqual([explicitDefault.log.asked, explicitDefault.log.opened[0].url], [0, "about:newtab"], "a passed about:newtab is a passed URL");
  // Native werePassedURL is !!url, so "" counts as not passed, yet ??= keeps
  // it: the resolved URL is "", not Firefox's default, and stays native.
  for (const consume of [false, true]) {
    const empty = browser({ consume });
    empty.openTab({ url: "" });
    assert.deepEqual([empty.log.asked, empty.log.zen], [0, [[false, false, "tab"]]], "the empty URL reaches Zen's branch unchanged");
    assert.deepEqual(empty.log.opened, consume ? [] : [{ url: "", where: "tab", related: false, fixup: false }], "and the native open, never the start page");
  }
  for (const consume of [false, true]) {
    const paste = browser({ consume });
    paste.openTab({ event: { where: "tab", button: 1 } });
    assert.deepEqual([paste.log.asked, paste.log.zen, paste.log.prefs], [0, [[false, true, "tab"]], ["middlemouse.paste"]], "a pasted middle click is never asked");
    assert.deepEqual(paste.log.opened, consume ? [] : [{ url: "https://pasted.example/", where: "tab", related: true, fixup: true }], "the native paste");
  }
  const unsafe = browser({ clipboard: "javascript:alert(1)" });
  unsafe.openTab({ event: { where: "tab", button: 1 } });
  assert.deepEqual([unsafe.log.asked, unsafe.log.opened[0].url], [0, "alert(1)"], "Firefox's own paste sanitizer still runs");
  for (const where of ["tabshifted", "window"]) {
    const other = browser();
    other.openTab({ event: { where } });
    assert.deepEqual([other.log.asked, other.log.opened[0].url, other.log.opened[0].where], [0, "about:newtab", where], where);
  }
});

test("hook: an extension's new-tab page overrides the native default and is never asked, even when the gate would say yes", () => {
  for (const newTab of ["moz-extension://fixture/newtab.html", "about:home", "https://custom.example/", "about:blank"]) {
    for (const consume of [false, true]) {
      const custom = browser({ newTab, consume });
      custom.openTab();
      assert.deepEqual([custom.log.asked, custom.log.zen], [0, [[false, false, "tab"]]], `${newTab}: Zen's branch exactly as stock`);
      assert.deepEqual(custom.log.opened, consume ? [] : [{ url: newTab, where: "tab", related: false, fixup: false }], `${newTab}: the native page`);
    }
  }
});

// ---------------------------------------------------------------- the window gate

function gateHarness(overrides = {}) {
  const facts = { flag: true, registered: true, closed: false, isPrivate: false, ai: false, authority: true, defaultNewTab: true, ...overrides };
  const reads = [];
  const window = { get closed() { return facts.closed; } };
  const gate = createStartPageGate({ window, aboutRegistered: overrides.aboutRegistered ?? true,
    services: { isNormalWindow: target => target === window && facts.registered },
    prefs: { getBoolPref: (name, fallback) => { reads.push([name, fallback]); if (facts.flag instanceof Error) throw facts.flag; return facts.flag; } },
    authority: () => facts.authority, isPrivate: () => facts.isPrivate, isAIWindow: () => facts.ai, defaultNewTab: () => facts.defaultNewTab });
  return { gate, facts, reads };
}

test("gate: only an alive, registered, authoritative normal non-AI window with Firefox's own new-tab page and the flag on gets the start page", { skip }, () => {
  assert.deepEqual([START_PAGE_URL, START_PAGE_PREF], ["about:axiosozo#home", "axiosozo.home.enabled"]);
  const h = gateHarness();
  assert.equal(h.gate.check(), START_PAGE_URL);
  assert.deepEqual(h.reads, [[START_PAGE_PREF, false]], "the real flag, default off");
  for (const [label, patch] of [["flag off", { flag: false }], ["unreadable flag", { flag: new Error("x") }], ["private", { isPrivate: true }],
    ["unknown privacy", { isPrivate: null }], ["AI window", { ai: true }], ["unknown AI state", { ai: null }], ["closed", { closed: true }],
    ["unknown closed state", { closed: undefined }], ["unregistered", { registered: false }], ["contexts not authoritative", { authority: false }],
    ["custom or extension new tab", { defaultNewTab: false }], ["unknown new-tab page", { defaultNewTab: undefined }]]) {
    assert.equal(gateHarness(patch).gate.check(), null, label);
  }
  assert.equal(gateHarness({ aboutRegistered: false }).gate.check(), null, "about:axiosozo not registered");
  const throwing = createStartPageGate({ window: {}, aboutRegistered: true, services: {}, prefs: {}, authority: () => true,
    isPrivate: () => false, isAIWindow: () => false, defaultNewTab: () => true });
  assert.equal(throwing.check(), null, "missing facts are null, never a throw");
});

test("gate: every command reads again: the flag off or an extension installed during enablement take effect at the next command", { skip }, () => {
  const h = gateHarness();
  assert.equal(h.gate.check(), START_PAGE_URL);
  h.facts.flag = false;
  assert.equal(h.gate.check(), null, "no observer: the next command");
  h.facts.flag = true;
  h.facts.defaultNewTab = false;
  assert.equal(h.gate.check(), null, "an extension's new-tab override now");
  h.facts.defaultNewTab = true;
  assert.equal(h.gate.check(), START_PAGE_URL);
  assert.equal(h.reads.length, 4);
});

test("gate: disposing ends only that window's gate; a replacement window keeps its own", { skip }, () => {
  const old = gateHarness(), replacement = gateHarness();
  old.gate.dispose();
  old.gate.dispose();
  assert.equal(old.gate.check(), null);
  assert.equal(replacement.gate.check(), START_PAGE_URL);
});

const STARTUP = readFileSync(new URL("../chrome/AxioSozoStartup.mjs", import.meta.url), "utf8");
/** One top-level function of AxioSozoStartup.mjs, evaluated with injected globals. */
function startupFunction(name, globals) {
  const start = STARTUP.indexOf(`\nfunction ${name}(`);
  const end = STARTUP.indexOf("\n}\n", start);
  assert.ok(start >= 0 && end > start, name);
  const constants = STARTUP.match(/^const NEW_TAB_EXTENSION_CONTROLLED = .*;$/mu)[0];
  // eslint-disable-next-line no-new-func -- test-only evaluation of one startup function
  return new Function(...Object.keys(globals), `${constants}\n${STARTUP.slice(start, end + 2)}\nreturn ${name};`)(...Object.values(globals));
}

test("startup: Firefox's own new-tab page only: the native default URL, AboutNewTab and the extension-controlled pref all agree", () => {
  const harness = (overrides = {}) => {
    const facts = { browserNewTab: "about:newtab", newTabURL: "about:newtab", overridden: false, extension: undefined, ...overrides };
    const reads = [];
    const read = startupFunction("defaultNewTabPage", {
      window: { get BROWSER_NEW_TAB_URL() { return facts.browserNewTab; } },
      ChromeUtils: { importESModule: url => { assert.equal(url, "resource:///modules/AboutNewTab.sys.mjs");
        return { AboutNewTab: { get newTabURL() { return facts.newTabURL; }, get newTabURLOverridden() { return facts.overridden; } } }; } },
      Services: { prefs: { getBoolPref: (name, fallback) => {
        reads.push([name, fallback]);
        if (facts.extension instanceof Error) throw facts.extension;
        return facts.extension ?? fallback;
      } } },
    });
    return { read, reads };
  };
  const stock = harness();
  assert.equal(stock.read(), true);
  assert.deepEqual(stock.reads, [["browser.newtab.extensionControlled", false]], "an absent pref is the native false");
  for (const [label, patch] of [["an extension controls new tabs", { extension: true }], ["the pref has the wrong type", { extension: new Error("NS_ERROR_UNEXPECTED") }],
    ["a custom window default", { browserNewTab: "moz-extension://fixture/newtab.html" }], ["an empty window default", { browserNewTab: "" }],
    ["AboutNewTab points elsewhere", { newTabURL: "https://custom.example/" }], ["AboutNewTab is overridden", { overridden: true }],
    ["unknown override state", { overridden: undefined }]]) {
    let answer;
    try { answer = harness(patch).read(); } catch { answer = null; }
    assert.notEqual(answer, true, label);
  }
});

test("startup: the window's lifetime starts before its first await; readiness must be Zen's actual promise; the scheduler and gate need a live ready window", () => {
  const source = STARTUP;
  const init = source.slice(source.indexOf("async function initialize()"));
  const firstAwait = init.search(/^[^/\n]*\bawait /mu);
  assert.ok(firstAwait > 0 && firstAwait === init.indexOf("  await window.gZenStartup.promiseInitialized;"), "Zen's startup is the first await");
  assert.ok(init.indexOf('window.addEventListener("unload", () => { disposed = true; }, { once: true });') < firstAwait, "unload is observed before the first await");
  assert.match(init, /await window\.gZenStartup\.promiseInitialized;\n\s*if \(disposed\) return;/u, "a window closed during Zen's startup installs nothing");
  assert.equal((init.match(/let disposed\b/gu) ?? []).length, 1, "one lifetime flag");
  assert.match(source, /const knownReadiness = typeof readiness\?\.then === "function";/u);
  assert.match(source, /if \(knownReadiness\) \{ await readiness; workspacesReady = true; \}/u, "only an actual fulfilled promise counts; a missing one is unknown");
  assert.match(source, /if \(registered && normalWindow && workspacesReady && alive\(\) && typeof services\.startWatchScheduler === "function"\n\s*&& guarded\("watch schedule admission", \(\) => services\.isNormalWindow\(window\)\) === true\)/u);
  assert.match(source, /const startPage = aboutRegistered && normalWindow && workspacesReady && alive\(\) &&/u);
  assert.match(source, /installContexts\(\{ engineProbe, aboutRegistered, alive: \(\) => !disposed \}\)/u);
  assert.doesNotMatch(source, /stopWatchScheduler|watchScheduler\.stop|getWatchDiagnostics\(\)\.dispose/u, "a window closing never stops the process schedule");
});

/** guarded() and installContexts() of AxioSozoStartup.mjs, run with injected modules and globals. */
function installContextsWith(globals) {
  const pick = head => {
    const start = STARTUP.indexOf(`\n${head}`);
    const end = STARTUP.indexOf("\n}\n", start);
    assert.ok(start >= 0 && end > start, head);
    return STARTUP.slice(start, end + 2);
  };
  // eslint-disable-next-line no-new-func -- test-only evaluation of the window wiring
  return new Function(...Object.keys(globals), `${pick("function guarded(")}${pick("async function installContexts(")}\nreturn installContexts;`)(...Object.values(globals));
}

test("startup: the watch schedule starts only from a window whose own registration succeeded and that the services hold as registered and normal", { skip }, async () => {
  const shutdown = [];
  const services = new AxioSozoServices({ storageFor: () => ({ read: async () => null, write: async () => {} }), clock: () => Date.UTC(2026, 9, 3, 12),
    timers: { setTimeout: () => 0, clearTimeout: () => {} }, onShutdown: (fn, label) => shutdown.push(label) });
  // Per window: how its registration goes (default: the real one).
  const registration = new Map();
  const register = services.registerWindow.bind(services);
  services.registerWindow = (win, adapter) => (registration.get(win) ?? register)(win, adapter);
  let starts = 0;
  const start = services.startWatchScheduler.bind(services);
  services.startWatchScheduler = () => { starts++; return start(); };
  const servicesModule = { AxioSozoServices: { get: () => services }, processSingleton: (_key, create) => create(), createStartPageGate };
  const quiet = { error: () => {} };
  const install = async (win, { alive = () => true } = {}) => {
    const run = installContextsWith({ Services: { prefs: { getBoolPref: (_name, fallback) => fallback } }, console: quiet, window: win,
      optionalModule: file => ({ "ZenWorkspaceAdapter.sys.mjs": { ZenWorkspaceAdapter }, "AxioSozoServices.sys.mjs": servicesModule })[file] ?? null,
      reloadFailedOverviewTabs: () => {}, installToolsEntry: () => null, introduceOnce: () => {}, aiWindowActive: () => false, defaultNewTabPage: () => true });
    return run({ engineProbe: null, aboutRegistered: false, alive });
  };
  const space = { uuid: "11111111-1111-4111-8111-111111111111", name: "Home", containerTabId: 0 };
  const scheduled = () => services.getWatchDiagnostics().scheduled;

  const throwing = fakeZenWindow({ spaces: [space] }).window;
  registration.set(throwing, () => { throw new Error("registration refused"); });
  await install(throwing);
  const refusing = fakeZenWindow({ spaces: [space] }).window;
  registration.set(refusing, () => null);
  await install(refusing);
  // "Registered" by a stand-in that the services never took: not admitted either.
  const unheld = fakeZenWindow({ spaces: [space] }).window;
  registration.set(unheld, () => () => {});
  await install(unheld);
  const closed = fakeZenWindow({ spaces: [space] }).window;
  await install(closed, { alive: () => false });
  assert.deepEqual([starts, scheduled(), services.getWatchDiagnostics().created], [0, false, false], "no schedule from any of them");
  assert.ok(!shutdown.includes("AxioSozo: stop watches"));

  // A correctly registered normal window starts it, once; a later one reuses it.
  const good = fakeZenWindow({ spaces: [space] }).window;
  const first = await install(good);
  assert.deepEqual([starts, scheduled()], [1, true]);
  const second = fakeZenWindow({ spaces: [space] }).window;
  await install(second);
  assert.deepEqual([starts, scheduled(), shutdown.filter(label => label === "AxioSozo: stop watches").length], [2, true, 1],
    "asked again, but one process schedule and one shutdown registration");
  for (const dispose of first.disposers.reverse()) dispose();
  assert.equal(scheduled(), true, "closing a window stops nothing");
});

test("startup: the callback answers only for the exact current window.AxioSozo owner, a live window and an installed gate", () => {
  const from = STARTUP.indexOf("  let startPage = null;\n  let owner = null;");
  const to = STARTUP.indexOf("  window.AxioSozo = owner;\n");
  assert.ok(from > 0 && to > from);
  // eslint-disable-next-line no-new-func -- test-only evaluation of the owner block of initialize()
  const install = new Function("window", `let disposed = false; let coordinator = null; let contexts = { runtime: {} };
    const adapter = {}, engineProbe = null, ensureCoordinator = () => null;
    ${STARTUP.slice(from, to + "  window.AxioSozo = owner;\n".length)}
    return { owner, setGate: gate => { startPage = gate; }, unload: () => { disposed = true; } };`);
  const window = {};
  const first = install(window);
  assert.equal(window.AxioSozo, first.owner);
  assert.ok(Object.isFrozen(first.owner));
  assert.equal(first.owner.startPageForNewTab(), null, "no gate installed yet");
  let checks = 0;
  first.setGate({ check: () => { checks++; return "about:axiosozo#home"; } });
  assert.equal(first.owner.startPageForNewTab(), "about:axiosozo#home");
  // A newer owner on the same window: the older callback is null, and neither
  // removes nor replaces it.
  const newer = Object.freeze({ startPageForNewTab: () => "newer" });
  window.AxioSozo = newer;
  assert.equal(first.owner.startPageForNewTab(), null);
  assert.equal(window.AxioSozo, newer);
  window.AxioSozo = first.owner;
  first.setGate({ check: () => "https://elsewhere.example/" });
  assert.equal(first.owner.startPageForNewTab(), null, "only the fixed URL");
  first.setGate({ check: () => { throw new Error("x"); } });
  assert.equal(first.owner.startPageForNewTab(), null, "a throwing gate is null");
  first.setGate({ check: () => { checks++; return "about:axiosozo#home"; } });
  first.unload();
  assert.equal(first.owner.startPageForNewTab(), null, "after unload, at once");
  assert.equal(checks, 1, "a closed window or a stale owner never reads its gate");
});

test("startup: the callback is part of the window's first frozen object; one gate per window; late installation never publishes", () => {
  const source = STARTUP;
  assert.match(source, /owner = Object\.freeze\(\{[^}]*startPageForNewTab,/u, "constructed with the object, never appended");
  assert.match(source, /window\.AxioSozo = owner;/u);
  assert.doesNotMatch(source, /AxioSozo\.startPageForNewTab\s*=/u);
  assert.match(source, /const gate = disposed \|\| window\.AxioSozo !== owner \? null : startPage;/u, "unload or a newer owner makes it null at once");
  assert.doesNotMatch(source, /delete window\.AxioSozo|window\.AxioSozo = (?!owner;)/u, "never removes or replaces a newer owner");
  assert.match(source, /if \(disposed\) disposeContexts\(\);\n\s*else startPage = contexts\.startPage \?\? null;/u, "a late gate is disposed, never published");
  assert.match(source, /startPage = null;\n\s*disposeContexts\(\);/u);
  assert.match(source, /disposers\.push\(\(\) => startPage\.dispose\(\)\)/u, "the window's own gate ends with its contexts");
  assert.match(source, /isPrivate: \(\) => \{\n\s*if \(window\.PrivateBrowsingUtils\?\.permanentPrivateBrowsing !== false\) return true;/u, "permanent private browsing is private");
  assert.match(source, /AboutNewTab\.newTabURLOverridden === false/u);
  assert.match(source, /moz-src:\/\/\/browser\/components\/aiwindow\/ui\/modules\/AIWindow\.sys\.mjs/u);
  // Plan 4 step 9: the window wiring builds no host or budget of its own.
  assert.doesNotMatch(source, /processSingleton\("decide"|createDecide\(|createDecisionBudget|onDecisionSending/u);
  assert.match(source, /services\.getDecisionRuntime\(\)/u);
  assert.match(source, /"installSiteRuleRuntime", \{ services, adapter: zen, decisions \}/u);
  assert.doesNotMatch(source, /home\.enabled["'],\s*true|setBoolPref\("axiosozo\.home/u, "the start page flag is never written");
  const defaults = readFileSync(new URL("../chrome/defaults.yaml", import.meta.url), "utf8");
  assert.match(defaults, /- name: axiosozo\.home\.enabled\n {2}value: false/u, "root's product default stays off");
});
