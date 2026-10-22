/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. https://mozilla.org/MPL/2.0/ */

// The single module that touches Zen APIs (contexts-api-v1 §3.1, handoff §2.6).
// Every Zen global, method, event, attribute and element id relied upon is
// listed in ZEN_ADAPTER_CONTRACT; `zen-workspace-adapter.test.mjs` checks each
// entry against the pinned Zen source so an upstream rename fails in one place.
// The adapter never writes Zen's workspace store.

const SPACES = "src/zen/spaces/ZenSpaceManager.mjs";
const TABBROWSER = "src/browser/components/tabbrowser/Tabbrowser-sys-mjs.patch";
const POPUPS = "src/browser/base/content/zen-panels/popups.inc";
const ICONS = "src/browser/base/content/zen-sidebar-icons.inc.xhtml";
const SPACE = "src/zen/spaces/ZenSpace.mjs";
const UI = "src/zen/common/modules/ZenUIManager.mjs";
const ROUTING = "src/zen/space-routing/ZenSpaceRoutingManager.sys.mjs";

export const ZEN_ADAPTER_CONTRACT = Object.freeze([
  { name: "window.gZenWorkspaces", source: SPACES, needle: "window.gZenWorkspaces = new nsZenWorkspaces()" },
  { name: "gZenWorkspaces.promiseInitialized", source: SPACES, needle: "promiseInitialized = new Promise(" },
  { name: "gZenWorkspaces.getWorkspaces", source: SPACES, needle: "getWorkspaces(lieToMe = false) {" },
  { name: "gZenWorkspaces.getWorkspaceFromId", source: SPACES, needle: "getWorkspaceFromId(id) {" },
  { name: "gZenWorkspaces.activeWorkspace", source: SPACES, needle: "get activeWorkspace() {" },
  { name: "gZenWorkspaces.privateWindowOrDisabled", source: SPACES, needle: "get privateWindowOrDisabled() {" },
  { name: "gZenWorkspaces.workspaceEnabled", source: SPACES, needle: "get workspaceEnabled() {" },
  { name: "gZenWorkspaces.workspaceElement", source: SPACES, needle: "workspaceElement(workspaceId) {" },
  { name: "workspace section <zen-workspace>", source: "src/zen/spaces/ZenSpace.mjs", needle: "customElements.define(\"zen-workspace\"" },
  { name: "gZenWorkspaces.changeWorkspaceWithID", source: SPACES, needle: "async changeWorkspaceWithID(workspaceID, ...args) {" },
  { name: "gZenWorkspaces.addChangeListeners", source: SPACES, needle: "addChangeListeners(" },
  { name: "gZenWorkspaces.removeChangeListeners", source: SPACES, needle: "removeChangeListeners(func) {" },
  { name: "change listener payload { workspace }", source: SPACES, needle: "await func({ workspace, onInit });" },
  { name: "workspace.uuid/name/icon/containerTabId", source: SPACES, needle: "containerTabId,\n    };" },
  { name: "event ZenWorkspaceDataChanged", source: SPACES, needle: "new CustomEvent(\"ZenWorkspaceDataChanged\")" },
  { name: "event ZenWorkspacesUIUpdate", source: SPACES, needle: "new CustomEvent(\"ZenWorkspacesUIUpdate\"" },
  { name: "tab attribute zen-workspace-id", source: SPACES, needle: "tab.getAttribute(\"zen-workspace-id\")" },
  { name: "tab attribute zen-essential", source: SPACES, needle: "tab.getAttribute(\"zen-essential\") === \"true\"" },
  { name: "menu target toolbarbutton[zen-workspace-id]", source: SPACES, needle: "event.explicitOriginalTarget?.closest(\"toolbarbutton\")" },
  { name: "popup #zenWorkspaceMoreActions", source: POPUPS, needle: "<menupopup id=\"zenWorkspaceMoreActions\">" },
  { name: "menu #context_zenWorkspacesOpenInContainerTab", source: POPUPS, needle: "<menu id=\"context_zenWorkspacesOpenInContainerTab\"" },
  { name: "window.gZenStartup", source: "src/zen/common/modules/ZenStartup.mjs", needle: "window.gZenStartup = new ZenStartup();" },
  { name: "gZenStartup.promiseInitialized", source: "src/zen/common/modules/ZenStartup.mjs", needle: "get promiseInitialized() {\n    return this.#initialized.promise;" },
  // Sidebar touchpoints used by SpaceSwitcher.sys.mjs and space-switcher.css.
  { name: "gZenWorkspaces.shouldWrapAroundNavigation", source: SPACES, needle: "\"shouldWrapAroundNavigation\",\n      \"zen.workspaces.wrap-around-navigation\"" },
  { name: "gZenWorkspaces.naturalScroll", source: SPACES, needle: "\"naturalScroll\",\n      \"zen.workspaces.natural-scroll\"" },
  { name: "toolbar #zen-sidebar-foot-buttons", source: ICONS, needle: "id=\"zen-sidebar-foot-buttons\"" },
  { name: "space icon strip #zen-workspaces-button", source: ICONS, needle: "<zen-workspace-icons id=\"zen-workspaces-button\"" },
  { name: "foot \"+\" #zen-create-new-button", source: ICONS, needle: "id=\"zen-create-new-button\" context=\"zenCreateNewPopup\"" },
  { name: "CustomizableUI area zen-sidebar-foot-buttons", source: "src/zen/common/sys/ZenCustomizableUI.sys.mjs", needle: "\"zen-sidebar-foot-buttons\",\n      {" },
  { name: "Library widget #zen-library-button", source: "src/zen/library/ZenLibraryWidget.sys.mjs", needle: "id: \"zen-library-button\"," },
  { name: "space header .zen-current-workspace-indicator", source: SPACE, needle: "<vbox class=\"zen-workspace-tabs-section zen-current-workspace-indicator " },
  { name: "space attribute collapsedpinnedtabs", source: SPACE, needle: "setAttribute(\"collapsedpinnedtabs\", \"true\")" },
  { name: "Clear button .zen-workspace-close-unpinned-tabs-button", source: SPACE, needle: "class=\"zen-workspace-close-unpinned-tabs-button\" />" },
  { name: "rename state .tab-label-container-editing", source: "src/zen/tabs/ZenTabRenamer.mjs", needle: "label.classList.add(\"tab-label-container-editing\");" },
  { name: "emoji picker anchor [zen-emoji-open]", source: "src/zen/common/emojis/ZenEmojiPicker.mjs", needle: "this.#anchor.setAttribute(\"zen-emoji-open\", \"true\");" },
  { name: "root attribute zen-sidebar-expanded", source: "src/zen/common/modules/ZenVerticalTabsManager.mjs", needle: "document.documentElement.setAttribute(\"zen-sidebar-expanded\", \"true\");" },
  { name: "menu item create space", source: POPUPS, needle: "<menuitem data-l10n-id=\"zen-panel-ui-workspaces-create\" command=\"cmd_zenOpenWorkspaceCreation\"/>" },
  // Project-container tabs (openTab with an explicit userContextId): gBrowser.addTab
  // takes the workspace and skips space routing, and an explicit container that
  // is not from an external caller is kept instead of the workspace default.
  { name: "gBrowser.addTab option zenWorkspaceId", source: TABBROWSER, needle: "+      zenWorkspaceId,\n+      skipRoute = false," },
  // A route container replaces an explicit one only when Zen found a route,
  // which skipRoute (always passed by openTab) rules out.
  { name: "gBrowser.addTab keeps an explicit non-external userContextId", source: TABBROWSER,
    needle: "+    if (beforeRouteResult.isRouteFound && this.documentGlobal.gZenSpaceRoutingManager.shouldUseRouteContainer(userContextId, fromExternal, this.documentGlobal)) {" },
  { name: "skipRoute tabs never find a route", source: ROUTING,
    needle: "    if (options.skipRoute || options.pinned || options.tabGroup) {\n      return nsZenSpaceRoutingManager.SKIP_TYPE.SKIPPED_TAB;" },
  { name: "gZenWorkspaces.getContextIdIfNeeded keeps an explicit container", source: SPACES,
    needle: "      fromExternal !== true &&\n      typeof userContextId !== \"undefined\" &&\n      userContextId !== activeWorkspaceUserContextId\n    ) {\n      return [userContextId, false, undefined];" },
  // Selecting the new tab can still be refused (openTab reports { selected }).
  { name: "gBrowser.setSelectedTab can be vetoed by Zen", source: TABBROWSER, needle: "+    if (this.documentGlobal.gZenWorkspaces.onBeforeTabSelect(val)) {" },
]);

// Public Gecko userContextIds; UINT32_MAX is reserved for extension storage.
const MAX_PUBLIC_USER_CONTEXT_ID = 4294967294;
const adapterError = code => Object.assign(new Error(code), { code });

/** Zen sidebar element ids/selectors, in one place (also used by space-switcher.css). */
export const ZEN_SIDEBAR = Object.freeze({
  footToolbar: "zen-sidebar-foot-buttons",
  spaceIcons: "zen-workspaces-button",
  createNewButton: "zen-create-new-button",
  libraryWidget: "zen-library-button",
  downloadsWidget: "downloads-button",
  spaceHeader: ".zen-current-workspace-indicator",
  clearButton: ".zen-workspace-close-unpinned-tabs-button",
});

const UUID = /^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$/u;

function snapshotOf(workspace) {
  return {
    uuid: workspace.uuid,
    name: typeof workspace.name === "string" ? workspace.name : "",
    icon: typeof workspace.icon === "string" ? workspace.icon : "",
    containerTabId: Number.isSafeInteger(workspace.containerTabId) && workspace.containerTabId > 0
      ? workspace.containerTabId : 0,
  };
}

export class ZenWorkspaceAdapter {
  #window; #listeners = new Set(); #updateListeners = new Set(); #known = null; #disposed = false; #changeListener = null;
  #onData = () => { this.#diff(); this.#notifyUpdate(); };

  constructor(window) {
    this.#window = window;
    window.addEventListener("ZenWorkspaceDataChanged", this.#onData);
    window.addEventListener("ZenWorkspacesUIUpdate", this.#onData);
    // Zen awaits each change listener; ours only records the switch.
    this.#changeListener = ({ workspace } = {}) => {
      this.#diff();
      if (workspace?.uuid) this.#emit({ kind: "switched", uuid: workspace.uuid });
      this.#notifyUpdate();
    };
    this.#zen()?.addChangeListeners?.(this.#changeListener);
    this.#known = this.#snapshotMap();
  }

  #zen() {
    return this.#disposed ? null : this.#window.gZenWorkspaces ?? null;
  }

  /** True when this window carries the shared, synced workspace list. Private,
   * unsynced and popup windows have local stand-in spaces that are not contexts. */
  isAuthoritative() {
    const zen = this.#zen();
    if (!zen || this.isPrivateWindow()) return false;
    try { return zen.privateWindowOrDisabled === false; } catch { return false; }
  }

  /** Resolves once Zen has restored this window's workspaces. */
  whenReady() {
    return Promise.resolve(this.#zen()?.promiseInitialized).then(() => { this.#diff(); });
  }

  listWorkspaces() {
    const zen = this.#zen();
    if (!zen || this.isPrivateWindow()) return [];
    let spaces;
    try { spaces = zen.getWorkspaces(); } catch { return []; }
    if (!Array.isArray(spaces)) return [];
    return spaces.filter(space => space && typeof space.uuid === "string" && UUID.test(space.uuid))
      .map(snapshotOf);
  }

  activeWorkspaceUuid() {
    const zen = this.#zen();
    if (!zen || this.isPrivateWindow()) return null;
    const uuid = zen.activeWorkspace;
    return typeof uuid === "string" && UUID.test(uuid) ? uuid : null;
  }

  workspaceForTab(tab) {
    if (!tab?.getAttribute || this.isPrivateWindow()) return null;
    const uuid = tab.getAttribute("zen-workspace-id");
    if (uuid && UUID.test(uuid)) return uuid;
    // Essentials are shown in every space; attribute them to the active one.
    if (tab.getAttribute("zen-essential") === "true") return this.activeWorkspaceUuid();
    return null;
  }

  containerForWorkspace(uuid) {
    const found = this.listWorkspaces().find(space => space.uuid === uuid);
    return found ? found.containerTabId : 0;
  }

  /** Firefox container label for a userContextId; null for 0 or unknown. */
  containerLabel(userContextId) {
    if (!userContextId) return null;
    try {
      return this.#window.ContextualIdentityService?.getUserContextLabel(userContextId) || null;
    } catch { return null; }
  }

  /** A public container as Firefox shows it: { userContextId, name, color, icon };
   * null for the default container (0), private or unknown identities. */
  containerIdentity(userContextId) {
    if (!Number.isSafeInteger(userContextId) || userContextId < 1 || userContextId > MAX_PUBLIC_USER_CONTEXT_ID) return null;
    const service = this.#window.ContextualIdentityService;
    let identity = null;
    try { identity = service?.getPublicIdentityFromId?.(userContextId) ?? null; } catch { identity = null; }
    if (!identity || identity.public !== true || identity.userContextId !== userContextId) return null;
    let name = "";
    try { name = service.getUserContextLabel(userContextId) || ""; } catch { name = ""; }
    return { userContextId, name: typeof name === "string" ? name : "",
      color: typeof identity.color === "string" ? identity.color : null, icon: typeof identity.icon === "string" ? identity.icon : null };
  }

  /** The container a tab's document is loaded in (0 = default), only when the
   * tab's userContextId and its live browsing context's origin attributes
   * agree. Null otherwise: a lazy, unattached or destroyed browser has no
   * browsing context (the browser getter returns null without a frame loader),
   * and the tab attribute alone is never trusted. Never changes for a tab. */
  tabUserContextId(tab) {
    const id = tab?.userContextId;
    if (!Number.isSafeInteger(id) || id < 0 || id > MAX_PUBLIC_USER_CONTEXT_ID) return null;
    let live;
    try { live = tab.linkedBrowser?.browsingContext?.originAttributes?.userContextId; } catch { return null; }
    return Number.isSafeInteger(live) && live === id ? id : null;
  }

  isPrivateWindow() {
    const window = this.#window;
    try {
      if (window.PrivateBrowsingUtils) return window.PrivateBrowsingUtils.isWindowPrivate(window);
    } catch {}
    return true; // unknown means private: never record, never treat as a context
  }

  selectedTab() {
    return this.#window.gBrowser?.selectedTab ?? null;
  }

  /** Switches this window to a workspace through Zen's own API. */
  async switchTo(uuid) {
    const zen = this.#zen();
    if (!zen || !this.listWorkspaces().some(space => space.uuid === uuid)) return false;
    await zen.changeWorkspaceWithID(uuid);
    return this.activeWorkspaceUuid() === uuid;
  }

  /** Opens an http(s) URL in a new foreground tab of the given (or active) workspace.
   * The URL comes from a page or a repository manifest, so it loads like an
   * untrusted web link with a null triggering principal (never the system
   * principal). Without `userContextId` the tab takes the workspace's default
   * container through openWebLinkIn. With one (a route resolved by the
   * service's container controller) the tab is created through gBrowser.addTab
   * in exactly that container: matching null principal, fromExternal false,
   * the explicit Zen workspace and no space routing; normal windows only.
   * A window with a modal dialog open is refused before anything happens
   * (WINDOW_BUSY). `verify()` runs synchronously right before the tab exists
   * and throws to cancel. The new tab must report that container through its
   * live browsing context; otherwise this tab (and only this tab) is closed
   * and CONTAINER_MISMATCH is reported. Returns { selected }: whether Firefox
   * actually brought the new tab to the front (gBrowser.setSelectedTab may
   * keep the current tab, e.g. for a shared-screen warning); the tab stays
   * open either way and no other tab is touched. */
  async openTab(url, { workspaceUuid = null, userContextId, verify = null } = {}) {
    const parsed = URL.parse(String(url));
    if (!parsed || !["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password)
      throw adapterError("INVALID_URL");
    if (userContextId === undefined) return this.#openInWorkspaceContainer(parsed.href, workspaceUuid);
    if (!Number.isSafeInteger(userContextId) || userContextId < 0 || userContextId > MAX_PUBLIC_USER_CONTEXT_ID) throw adapterError("INVALID_CONTAINER");
    if (this.isPrivateWindow() !== false) throw adapterError("PRIVATE_WINDOW");
    const gBrowser = this.#window.gBrowser;
    if (typeof gBrowser?.addTab !== "function") throw adapterError("NO_TABBROWSER");
    const idle = () => { if (this.#window.document?.documentElement?.hasAttribute?.("window-modal-open")) throw adapterError("WINDOW_BUSY"); };
    idle();
    if (workspaceUuid && this.activeWorkspaceUuid() !== workspaceUuid) await this.switchTo(workspaceUuid);
    const space = this.activeWorkspaceUuid();
    if (workspaceUuid && space !== workspaceUuid) throw adapterError("WORKSPACE_UNAVAILABLE");
    const triggeringPrincipal = this.#window.Services.scriptSecurityManager.createNullPrincipal({ userContextId });
    if (!triggeringPrincipal || triggeringPrincipal.isSystemPrincipal !== false || triggeringPrincipal.isNullPrincipal !== true
      || triggeringPrincipal.originAttributes?.userContextId !== userContextId) throw adapterError("UNSAFE_PRINCIPAL");
    idle();
    if (typeof verify === "function") verify();
    const tab = gBrowser.addTab(parsed.href, { triggeringPrincipal, userContextId, fromExternal: false, inBackground: false,
      skipRoute: true, ...(space ? { zenWorkspaceId: space } : {}) });
    if (!tab) throw adapterError("TAB_NOT_OPENED");
    if (this.tabUserContextId(tab) !== userContextId) {
      try { gBrowser.removeTab(tab); } catch {}
      throw adapterError("CONTAINER_MISMATCH");
    }
    return { selected: gBrowser.selectedTab === tab };
  }

  async #openInWorkspaceContainer(href, workspaceUuid) {
    if (workspaceUuid && this.activeWorkspaceUuid() !== workspaceUuid) await this.switchTo(workspaceUuid);
    // Zen assigns the workspace and its default container to new tabs itself
    // (gZenWorkspaces.getContextIdIfNeeded); the principal carries the same container.
    const space = this.activeWorkspaceUuid();
    const userContextId = space ? this.containerForWorkspace(space) : 0;
    const triggeringPrincipal = this.#window.Services.scriptSecurityManager.createNullPrincipal({ userContextId });
    if (!triggeringPrincipal || triggeringPrincipal.isSystemPrincipal) throw adapterError("UNSAFE_PRINCIPAL");
    this.#window.openWebLinkIn(href, "tab", { triggeringPrincipal });
    return true;
  }

  /** Zen's <zen-workspace> section for a space (DevLoop anchors its project block there). */
  workspaceElement(uuid) {
    const zen = this.#zen();
    if (!zen || typeof uuid !== "string" || !UUID.test(uuid) || this.isPrivateWindow()) return null;
    try { return zen.workspaceElement(uuid) ?? null; } catch { return null; }
  }

  // F1 menu seam: the menu module itself stays free of Zen names.
  workspaceMenu() {
    return this.#window.document?.getElementById("zenWorkspaceMoreActions") ?? null;
  }

  workspaceMenuAnchor() {
    return this.#window.document?.getElementById("context_zenWorkspacesOpenInContainerTab") ?? null;
  }

  /** Same target rule as Zen's own menu: the clicked space icon, else the active space. */
  workspaceForMenuEvent(event) {
    let target = null;
    try {
      target = event?.explicitOriginalTarget?.closest?.("toolbarbutton")
        ?? event?.target?.triggerNode?.closest?.("toolbarbutton") ?? null;
    } catch {}
    const uuid = target?.getAttribute?.("zen-workspace-id");
    if (uuid && this.listWorkspaces().some(space => space.uuid === uuid)) return uuid;
    return this.activeWorkspaceUuid();
  }

  onChange(callback) {
    if (typeof callback !== "function") throw new TypeError("callback");
    this.#listeners.add(callback);
    return () => this.#listeners.delete(callback);
  }

  /** Called after every Zen workspace data, UI or switch event, without a diff
   * (e.g. reordering or a switch). For views that simply re-render. */
  onUpdate(callback) {
    if (typeof callback !== "function") throw new TypeError("callback");
    this.#updateListeners.add(callback);
    return () => this.#updateListeners.delete(callback);
  }

  /** The space `offset` steps from the active one, honouring Zen's wrap-around
   * pref, and for scroll gestures its natural-scroll pref. Null: nowhere to go. */
  neighbourWorkspace(offset, { scroll = false } = {}) {
    const zen = this.#zen();
    const spaces = this.listWorkspaces();
    const index = spaces.findIndex(space => space.uuid === this.activeWorkspaceUuid());
    if (!zen || index < 0 || spaces.length < 2 || !offset) return null;
    let step = Math.sign(offset);
    if (scroll) { try { if (zen.naturalScroll === true) step = -step; } catch {} }
    let wrap = true;
    try { wrap = zen.shouldWrapAroundNavigation !== false; } catch {}
    let target = index + step;
    if (wrap) target = (target + spaces.length) % spaces.length;
    else if (target < 0 || target >= spaces.length) return null;
    return target === index ? null : spaces[target].uuid;
  }

  /** Makes a chrome element a target of Zen's space menu exactly like Zen's own
   * space icons (a toolbarbutton carrying the space id), so edit, icon, theme,
   * delete and create act on that space through Zen's unchanged menu. */
  markMenuTarget(element, uuid) {
    if (!element?.setAttribute || typeof uuid !== "string" || !UUID.test(uuid)) return false;
    element.setAttribute("zen-workspace-id", uuid);
    element.setAttribute("context", "zenWorkspaceMoreActions");
    return true;
  }

  /** Opens Zen's space menu anchored to an element; without a space target Zen
   * lists every space (switch) plus rename, icon, create and delete. */
  openWorkspaceMenu(anchor, triggerEvent = null) {
    const popup = this.workspaceMenu();
    if (!popup?.openPopup || !anchor) return false;
    popup.openPopup(anchor, "before_start", 0, 0, false, false, triggerEvent);
    return true;
  }

  /** Zen's sidebar foot toolbar (bottom row of the sidebar). */
  sidebarFoot() {
    return this.#window.document?.getElementById(ZEN_SIDEBAR.footToolbar) ?? null;
  }

  /** Stable per-space anchor for chrome blocks (DevLoop): Zen's space header.
   * SpaceSwitcher hides it visually (display: none) but it stays in the DOM, so
   * `header.after(block)` keeps working; null when Zen has none. */
  workspaceHeader(uuid) {
    try { return this.workspaceElement(uuid)?.querySelector?.(ZEN_SIDEBAR.spaceHeader) ?? null; } catch { return null; }
  }

  /** Replaces Zen's Library button in the sidebar foot with Firefox's Downloads
   * button through CustomizableUI (global, persisted like a user customization).
   * Returns true when placements changed. */
  replaceLibraryButton() {
    const cui = this.#window.CustomizableUI;
    const { footToolbar: foot, libraryWidget: library, downloadsWidget: downloads } = ZEN_SIDEBAR;
    try {
      const placement = cui?.getPlacementOfWidget(library);
      if (placement?.area !== foot) return false;
      if (!cui.getPlacementOfWidget(downloads)) cui.addWidgetToArea(downloads, foot, placement.position);
      cui.removeWidgetFromArea(library);
      return true;
    } catch (error) { console.error("AxioSozo: Library button unchanged", error); return false; }
  }

  /** Reverses replaceLibraryButton: Library back where Downloads sits in the foot. */
  restoreLibraryButton() {
    const cui = this.#window.CustomizableUI;
    const { footToolbar: foot, libraryWidget: library, downloadsWidget: downloads } = ZEN_SIDEBAR;
    try {
      if (!cui || cui.getPlacementOfWidget(library)) return false;
      const placement = cui.getPlacementOfWidget(downloads);
      if (placement?.area === foot) {
        cui.addWidgetToArea(library, foot, placement.position);
        cui.removeWidgetFromArea(downloads);
      } else cui.addWidgetToArea(library, foot, 0);
      return true;
    } catch (error) { console.error("AxioSozo: Library button not restored", error); return false; }
  }

  #snapshotMap() {
    return new Map(this.listWorkspaces().map(space => [space.uuid, space]));
  }

  #diff() {
    if (this.#disposed) return;
    const previous = this.#known ?? new Map();
    const next = this.#snapshotMap();
    this.#known = next;
    for (const uuid of previous.keys()) if (!next.has(uuid)) this.#emit({ kind: "deleted", uuid });
    for (const [uuid, space] of next) {
      const before = previous.get(uuid);
      if (!before) this.#emit({ kind: "created", uuid });
      // "renamed" covers every visible workspace field: name, icon and default container.
      else if (before.name !== space.name || before.icon !== space.icon
        || before.containerTabId !== space.containerTabId) this.#emit({ kind: "renamed", uuid });
    }
  }

  #notifyUpdate() {
    if (this.#disposed) return;
    for (const listener of [...this.#updateListeners]) {
      try { listener(); } catch (error) { console.error("AxioSozo workspace view failed", error); }
    }
  }

  #emit(change) {
    for (const listener of [...this.#listeners]) {
      try { listener(change); } catch (error) { console.error("AxioSozo workspace listener failed", error); }
    }
  }

  dispose() {
    if (this.#disposed) return;
    this.#window.removeEventListener("ZenWorkspaceDataChanged", this.#onData);
    this.#window.removeEventListener("ZenWorkspacesUIUpdate", this.#onData);
    try { this.#window.gZenWorkspaces?.removeChangeListeners?.(this.#changeListener); } catch {}
    this.#disposed = true;
    this.#listeners.clear();
    this.#updateListeners.clear();
  }
}
