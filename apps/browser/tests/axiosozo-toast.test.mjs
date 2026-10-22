/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. https://mozilla.org/MPL/2.0/ */

// AxioSozoToast: Zen's toast markup in Zen's container, plain text, polite
// announcement, Zen's timing and motion (none under reduced motion), and no
// throw where Zen's container is missing. Synthetic fake DOM; not evidence of
// how Zen renders it.
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import "./support/chrome-modules.mjs";
import { FakeDocument, createClock, flushMicrotasks } from "./dev-loop-harness.test.mjs";

const { showAxioSozoToast, CONTAINER_ID, DEFAULT_TIMEOUT_MS, MAX_TEXT } = await import("../chrome/AxioSozoToast.sys.mjs");

function fakeWindow({ container = true, reducedMotion = false, motion = null, prefs = {}, frames = false } = {}) {
  const document = new FakeDocument();
  const clock = createClock(0);
  let node = null;
  if (container) {
    node = document.createXULElement("vbox");
    node.id = CONTAINER_ID;
    node.setAttribute("hidden", "true");
    document.documentElement.appendChild(node);
  }
  const queued = [];
  const window = {
    document, ...clock.timersApi,
    Services: { prefs: { getBoolPref: (name, fallback) => (name in prefs ? prefs[name] : fallback) } },
    matchMedia: query => ({ matches: reducedMotion && query === "(prefers-reduced-motion: reduce)" }),
    ...(motion ? { gZenUIManager: { motion } } : {}),
    ...(frames ? { requestAnimationFrame: fn => { queued.push(fn); return queued.length; } } : {}),
  };
  return { window, document, clock, container: node, frame: () => { for (const fn of queued.splice(0)) fn(); } };
}

function recordingMotion() {
  const calls = [];
  return { calls, animate: (node, keyframes, options) => { calls.push({ node, keyframes, options }); return Promise.resolve(); } };
}

const toasts = f => f.container.children;

test("a toast is Zen's own markup in Zen's container: hbox.zen-toast > vbox > label (+ .description) and a primary footer button", async () => {
  const f = fakeWindow();
  let pressed = 0;
  const handle = showAxioSozoToast(f.window, { title: "Removed Webapp from this space", description: "It stays on the Projects page.",
    button: { label: "Undo", onCommand: () => pressed++ } });
  assert.equal(typeof handle.close, "function");
  assert.equal(f.container.hasAttribute("hidden"), false, "Zen's container is shown, as Zen does");
  assert.equal(toasts(f).length, 1);
  const toast = toasts(f)[0];
  assert.deepEqual([toast.localName, toast.className, toast.getAttribute("axiosozo-toast"), toast.getAttribute("button")],
    ["hbox", "zen-toast", "true", "true"]);
  const [labels, button] = toast.children;
  assert.equal(labels.localName, "vbox");
  assert.deepEqual(labels.children.map(label => [label.localName, label.className, label.textContent]),
    [["label", "", "Removed Webapp from this space"], ["label", "description", "It stays on the Projects page."]]);
  assert.deepEqual([button.localName, button.className, button.getAttribute("label")], ["button", "footer-button primary", "Undo"]);
  assert.deepEqual([toast.getAttribute("role"), toast.getAttribute("aria-live"), toast.getAttribute("aria-atomic")], ["status", "polite", "true"]);
  button.dispatch("command");
  await flushMicrotasks();
  assert.equal(pressed, 1, "the button runs its command");
  assert.equal(toasts(f).length, 0, "and closes the toast");
  assert.equal(f.container.getAttribute("hidden"), "true", "the empty container is hidden again");
});

test("text is plain: markup stays literal, whitespace collapses and long text is cut", () => {
  const f = fakeWindow();
  showAxioSozoToast(f.window, { title: "<img src=x onerror=alert(1)>\n  Stopped", description: "x".repeat(400) });
  const [title, description] = toasts(f)[0].children[0].children;
  assert.equal(title.textContent, "<img src=x onerror=alert(1)> Stopped");
  assert.equal(title.children.length, 0, "no element was parsed from it");
  assert.equal(description.textContent.length, MAX_TEXT);
  assert.ok(description.textContent.endsWith("…"));
  const source = readFileSync(new URL("../chrome/AxioSozoToast.sys.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|createContextualFragment|DOMParser/u);
});

test("a new toast is filled a frame after it is added, so it is announced as a live-region change", () => {
  const f = fakeWindow({ frames: true });
  showAxioSozoToast(f.window, { title: "Stopped 3 servers" });
  const title = toasts(f)[0].children[0].children[0];
  assert.equal(title.textContent, "", "added empty");
  f.frame();
  assert.equal(title.textContent, "Stopped 3 servers");
});

test("it closes after its timeout; hover and keyboard focus keep it; ui.popup.disable_autohide keeps it", async () => {
  const f = fakeWindow();
  showAxioSozoToast(f.window, { title: "Stopped 1 server" });
  await f.clock.advance(DEFAULT_TIMEOUT_MS - 1);
  assert.equal(toasts(f).length, 1);
  await f.clock.advance(1);
  assert.equal(toasts(f).length, 0, "Zen-like default: a short confirmation");

  showAxioSozoToast(f.window, { title: "Removed Webapp from this space", timeoutMs: 6000 });
  const toast = toasts(f)[0];
  toast.dispatch("mouseenter");
  await f.clock.advance(10000);
  assert.equal(toasts(f).length, 1, "hovering keeps it");
  toast.dispatch("focusin");
  toast.dispatch("mouseleave");
  await f.clock.advance(10000);
  assert.equal(toasts(f).length, 1, "keyboard focus inside keeps it");
  toast.dispatch("focusout");
  await f.clock.advance(5999);
  assert.equal(toasts(f).length, 1, "the full time again once left");
  await f.clock.advance(1);
  assert.equal(toasts(f).length, 0);

  const kept = fakeWindow({ prefs: { "ui.popup.disable_autohide": true } });
  showAxioSozoToast(kept.window, { title: "Stopped 1 server" });
  await kept.clock.advance(60000);
  assert.equal(toasts(kept).length, 1, "like Zen, the debugging pref keeps toasts up");
});

test("the same key updates the shown toast in place instead of stacking another", async () => {
  const f = fakeWindow();
  const first = showAxioSozoToast(f.window, { title: "Stopped 2 servers", key: "stop-all" });
  const node = toasts(f)[0];
  await f.clock.advance(DEFAULT_TIMEOUT_MS - 100);
  showAxioSozoToast(f.window, { title: "Stopped 3 servers", key: "stop-all" });
  assert.equal(toasts(f).length, 1);
  assert.equal(toasts(f)[0], node);
  assert.equal(node.children[0].children[0].textContent, "Stopped 3 servers");
  await f.clock.advance(200);
  assert.equal(toasts(f).length, 1, "its timer started again");
  first.close();
  await flushMicrotasks();
  assert.equal(toasts(f).length, 0, "either handle closes it");
  first.close();
  showAxioSozoToast(f.window, { title: "One" });
  showAxioSozoToast(f.window, { title: "Two" });
  assert.equal(toasts(f).length, 2, "different messages are separate toasts, as in Zen");
});

test("motion: Zen's spring in and quick fade out through gZenUIManager.motion; none at all under reduced motion", async () => {
  const motion = recordingMotion();
  const f = fakeWindow({ motion });
  const handle = showAxioSozoToast(f.window, { title: "Stopped 1 server" });
  await flushMicrotasks();
  const toast = toasts(f)[0];
  assert.equal(toast.style.transform, "scale(0)", "starts closed, as Zen's does");
  assert.deepEqual(motion.calls.map(call => [call.keyframes, call.options]),
    [[{ scale: 1 }, { type: "spring", bounce: 0.2, duration: 0.5 }]]);
  handle.close();
  await flushMicrotasks();
  assert.deepEqual(motion.calls.at(-1).keyframes, { opacity: [1, 0], scale: [1, 0.5] });
  assert.equal(motion.calls.at(-1).options.duration, 0.2);
  assert.equal(toasts(f).length, 0, "removed once the exit finished");

  const calm = recordingMotion();
  const reduced = fakeWindow({ motion: calm, reducedMotion: true });
  const shown = showAxioSozoToast(reduced.window, { title: "Stopped 1 server" });
  assert.equal(toasts(reduced)[0].style.transform, undefined, "never set to scale(0)");
  shown.close();
  await flushMicrotasks();
  assert.equal(calm.calls.length, 0, "no animation at all");
  assert.equal(toasts(reduced).length, 0);

  const broken = fakeWindow({ motion: { animate: () => Promise.reject(new Error("no motion")) } });
  showAxioSozoToast(broken.window, { title: "Stopped 1 server" });
  await flushMicrotasks();
  assert.equal(toasts(broken)[0].style.transform, "", "a failed animation never leaves it invisible");
});

test("no container, no window or no text: nothing is shown and nothing throws", () => {
  for (const window of [null, undefined, {}, { document: null }, fakeWindow({ container: false }).window]) {
    const handle = showAxioSozoToast(window, { title: "Stopped 1 server" });
    assert.equal(typeof handle.close, "function");
    handle.close();
  }
  const f = fakeWindow();
  showAxioSozoToast(f.window, { title: "   " });
  showAxioSozoToast(f.window);
  assert.equal(toasts(f).length, 0);
  showAxioSozoToast(f.window, { title: "Stopped", button: { label: "Undo" } });
  assert.equal(toasts(f)[0].children.length, 1, "a button without a command is left out");
  const throwing = fakeWindow();
  throwing.document.createXULElement = () => { throw new Error("broken document"); };
  assert.doesNotThrow(() => showAxioSozoToast(throwing.window, { title: "Stopped" }));
});

// The markup and classes above are Zen's: if the pinned Zen source changes
// them, this module must follow (ZenToasts.mjs, zen-popup.css).
const ZEN = new URL("../../../upstream/zen/src/", import.meta.url);
test("Zen's toast container, markup and styles still exist in the pinned Zen source", { skip: !existsSync(ZEN) && "pinned Zen source absent" }, () => {
  const read = path => readFileSync(new URL(path, ZEN), "utf8");
  assert.match(read("browser/base/content/zen-tabbrowser-elements.inc.xhtml"), /<vbox id="zen-toast-container" \/>/u);
  const manager = read("zen/common/modules/ZenToasts.mjs");
  for (const needle of ['document.createXULElement("hbox")', 'document.createXULElement("vbox")', 'document.createXULElement("label")',
    'description.classList.add("description")', 'button.classList.add("footer-button", "primary")',
    'wrapper.classList.add("zen-toast")', 'wrapper.setAttribute("button", true)', '{ type: "spring", bounce: 0.2, duration: 0.5,',
    '{ opacity: [1, 0], scale: [1, 0.5] }', 'getBoolPref("ui.popup.disable_autohide")']) {
    assert.ok(manager.includes(needle), needle);
  }
  const css = read("zen/common/styles/zen-popup.css");
  assert.match(css, /#zen-toast-container \{/u);
  assert.match(css, /& \.zen-toast \{/u);
  assert.match(css, /& \.button-text \{\s*display: none;/u, "Zen's toast buttons are icon-only; axiosozo-runtime.css shows our word");
});

test("the runtime stylesheet lets an AxioSozo toast button show its text, scoped to AxioSozo toasts", () => {
  const css = readFileSync(new URL("../chrome/axiosozo-runtime.css", import.meta.url), "utf8");
  assert.match(css, /#zen-toast-container \.zen-toast\[axiosozo-toast\] \{/u);
  assert.match(css, /& \.button-text \{ display: revert; \}/u);
  assert.match(css, /& \.button-icon \{ display: none; \}/u);
});
