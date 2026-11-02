// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Main-window plugin slots (added 2026-10-13): nav entries and chat-toolbar panels from /api/plugins, the
// context pushed to their pages, the host methods answered for them and the app.js wiring. Replaces
// tests/account-clear-advisor.test.cjs and the main-window parts of the former advisor UI tests.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const read = file => readFileSync(path.join(__dirname, "..", file), "utf8");
const slotsSource = read("chatui/plugin-slots.js"), frameSource = read("chatui/plugin-frame.js");
const indexHtml = read("chatui/index.html"), appSource = read("chatui/app.js");
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let index = 0; index < 8; index++) await new Promise(resolve => setImmediate(resolve)); };

class Node {
  constructor(tag, document) {
    Object.assign(this, { tagName: tag, document, children: [], parentNode: null, attributes: {}, dataset: {}, listeners: {},
      textContent: "", className: "", hidden: false, title: "", type: "" });
    const node = this;
    this.classList = {
      contains: name => node.className.split(/\s+/).includes(name),
      add: name => { if (!node.classList.contains(name)) node.className = (node.className + " " + name).trim(); },
      remove: name => { node.className = node.className.split(/\s+/).filter(item => item && item !== name).join(" "); },
      toggle: (name, on) => { if (on === undefined) on = !node.classList.contains(name); on ? node.classList.add(name) : node.classList.remove(name); return on; },
    };
    if (tag === "iframe") this.contentWindow = { messages: [], postMessage(message) { this.messages.push(message); } };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === "class") this.className = String(value); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async emit(name, event = {}) { for (const fn of this.listeners[name] || []) await fn(event); }
  appendChild(child) { child.remove(); child.parentNode = this; this.children.push(child); return child; }
  replaceChildren(...items) { for (const child of [...this.children]) child.remove(); for (const item of items) this.appendChild(item); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; }
  after(node) {
    node.remove(); const siblings = this.parentNode.children; siblings.splice(siblings.indexOf(this) + 1, 0, node); node.parentNode = this.parentNode;
  }
  before(node) {
    node.remove(); const siblings = this.parentNode.children; siblings.splice(siblings.indexOf(this), 0, node); node.parentNode = this.parentNode;
  }
  focus() { this.focused = true; }
  *walk() { for (const child of this.children) { yield child; yield* child.walk(); } }
}

function harness(options = {}) {
  const hooks = {}, body = new Node("body"), ids = new Map(), calls = [], toasts = [], views = [], observers = [], windowListeners = {};
  let hostWindows = [];
  const setHostWindows = list => { hostWindows = list; hooks.panelState?.({ windows: list }); };
  const document = {
    body,
    getElementById: id => ids.get(id) || [...body.walk()].find(node => node.id === id) || null,
    createElement: tag => new Node(tag, document), createElementNS: (_ns, tag) => new Node(tag, document),
    querySelectorAll: selector => [...body.walk()].filter(node => node.classList.contains(selector.replace(/^\./, ""))),
  };
  const add = (id, parent = body, tag = "div") => { const node = new Node(tag, document); node.id = id; parent.appendChild(node); ids.set(id, node); return node; };
  const rail = add("rail"); add("navChat", rail); add("navSettings", rail);
  const header = add("chatHistoryActions"); add("btnReturnLatest", header, "button"); add("btnChatHistory", header, "button");
  add("chatToolbar"); add("pluginNavView"); add("chatInput", body, "textarea");
  const listing = options.listing || { plugins: [plugin("kyf.advisor")] };
  const desktopHost = options.desktopHost === null ? undefined : {
    copyDraft: async text => { calls.push(["copy", text]); return true; },
    openFriendWindow: async scope => { calls.push(["friend", plain(scope)]); return true; },
    choosePluginFile: async (pluginId, kind) => { calls.push(["pick", pluginId, kind]); return { token: "fg_" + "a".repeat(32), label: "包.zip" }; },
    openPluginWindow: async target => {
      calls.push(["open", plain(target)]);
      const result = options.openResult ?? { opened: true };
      if (result.opened) setHostWindows([...hostWindows.filter(item => item.plugin !== target.plugin || item.account !== target.account ||
        item.user !== target.user), { plugin: target.plugin, account: target.account, user: target.user, visible: true }]);
      return result;
    },
    sendPluginWindowEvent: message => calls.push(["event", plain(message)]),
    retainPluginWindows: value => calls.push(["retain", plain(value)]),
    forgetPluginWindows: account => calls.push(["forget", account]),
    onPluginPanelHostCall: fn => { hooks.hostCall = fn; }, onPluginPanelState: fn => { hooks.panelState = fn; },
    replyPluginPanelHostCall: (id, reply) => calls.push(["reply", id, plain(reply)]),
    ...(options.desktopHost || {}),
  };
  const window = {
    desktopHost, document,
    addEventListener: (name, fn) => (windowListeners[name] ||= []).push(fn), removeEventListener() {},
    dispatch: (name, detail) => (windowListeners[name] || []).forEach(fn => fn({ detail })),
    MutationObserver: class { constructor(fn) { observers.push(fn); } observe() {} },
    ModelTraces: options.modelTraces, navigator: {}, alert: message => toasts.push(["alert", message]),
  };
  const chatState = { currentAccount: "account-a", currentUser: "friend-a", messageSourceReady: true, view: "chat",
    sessions: new Map([["friend-a", { name: "朋友甲" }], ["group@chatroom", { isGroup: true }]]) };
  const intervals = [];
  const context = vm.createContext({ window, document, chatState, byId: document.getElementById, toast: message => toasts.push(message),
    setTimeout: () => 0, clearTimeout() {}, setInterval: fn => intervals.push(fn), URLSearchParams, AbortController, Promise, JSON, Object, Error,
    fetch: async (url, init) => {
      calls.push(["fetch", url, ...(init?.body ? [JSON.parse(init.body)] : [])]);
      if (url.startsWith("/api/account/self-profile") && options.selfProfile) return options.selfProfile(url, init);
      if (url === "/api/model-source" && options.modelSource) return options.modelSource(url, init);
      if (url.startsWith("/api/attention?")) return { ok: true, json: async () => ({ items: options.attention ? options.attention() : [] }) };
      if (url === "/api/attention/seen") return options.seen ? options.seen(JSON.parse(init.body)) : { ok: true, json: async () => ({ seenAt: 1 }) };
      return { ok: true, json: async () => options.loadListing ? options.loadListing() : listing };
    } });
  vm.runInContext(frameSource, context);
  vm.runInContext(slotsSource, context);
  vm.runInContext(appSource.slice(appSource.indexOf("let friendWindowRequest ="), appSource.indexOf("\nfunction switchView(")) +
    "\nglobalThis.friendUi = { updateFriendButton, openFriendWindow };", context);
  const slots = window.PluginSlots;
  const attentionRenders = [];
  slots.init({ switchView: view => { views.push(view); slots.onViewChanged(view); }, toast: message => toasts.push(message),
    onAttention: () => attentionRenders.push(1), ...context.friendUi });
  const navItems = () => document.querySelectorAll(".plugin-nav-item");
  const buttons = () => document.querySelectorAll(".plugin-toolbar-btn");
  const headerButtons = () => document.querySelectorAll(".plugin-header-btn");
  const frames = () => [...ids.get("pluginNavView").walk()].filter(node => node.tagName === "iframe");
  async function fromPage(frame, data) {
    for (const fn of windowListeners.message || []) fn({ source: frame.contentWindow, data });
    await settle();
    return frame.contentWindow.messages.at(-1);
  }
  return { hooks, slots, document, body, ids, calls, toasts, views, observers, window, navItems, buttons, headerButtons,
    chatState, friendUi: context.friendUi, frames, fromPage, intervals, attentionRenders, setHostWindows };
}
function plugin(id, overrides = {}) {
  return { id, name: "聊天助手", enabled: true, contributes: {
    nav: [{ id: "repository", label: "助手仓库", page: "ui/repository.html", icon: "assistant" }],
    chatToolbar: [{ id: "advisor", label: "聊天助手", page: "ui/panel.html", kind: "panel" }] }, ...overrides };
}
test("enabled contributions render one nav entry after 聊天 and one toolbar text button, as before", async () => {
  const h = harness({ listing: { plugins: [plugin("kyf.advisor"), plugin("kyf.off", { enabled: false }),
    plugin("kyf.bad", { incompatible: "core api" })] } });
  await settle();
  const rail = h.ids.get("rail");
  assert.deepEqual(rail.children.map(node => node.id || node.dataset.tab), ["navChat", "plugin:kyf.advisor/repository", "navSettings"]);
  const [item] = h.navItems();
  assert.equal(item.title, "助手仓库"); assert.equal(item.attributes["aria-label"], "助手仓库");
  assert.equal(item.className, "nav-item plugin-nav-item");
  const icon = item.children[0];
  assert.equal(icon.attributes.class, "plugin-nav-icon");
  assert.deepEqual(icon.children.map(shape => shape.tagName), ["rect", "path"], "the same robot icon as the former 助手仓库 entry");
  assert.equal(icon.children[1].attributes.d, "M12 3v4M9 12v2m6-2v2M9 17h6M1 12h3m16 0h3");
  const buttons = h.buttons();
  assert.equal(buttons.length, 1); assert.equal(buttons[0].textContent, "聊天助手");
  assert.equal(buttons[0].className, "toolbar-text-btn plugin-toolbar-btn"); assert.equal(buttons[0].parentNode, h.ids.get("chatToolbar"));
});

test("the bundled kyf.advisor manifest keeps the former 助手仓库 robot icon and 聊天助手 button", async () => {
  // Added 2026-10-14 (review): the fake listings above name the icon; the shipped manifest must name it too.
  const manifest = JSON.parse(read("plugins/kyf.advisor/plugin.json"));
  const h = harness({ listing: { plugins: [{ id: manifest.id, name: manifest.name, enabled: true, contributes: manifest.contributes }] } });
  await settle();
  const [item] = h.navItems();
  assert.equal(item.title, "助手仓库");
  assert.equal(item.children[0].children[1].attributes.d, "M12 3v4M9 12v2m6-2v2M9 17h6M1 12h3m16 0h3");
  assert.deepEqual(h.buttons().map(button => button.textContent), ["聊天助手"]);
});

test("自我剖析 uses a personal icon and opens the native panel without switching the main chat", async () => {
  const manifest = JSON.parse(read("plugins/kyf.self-analysis/plugin.json")), opened = [], updates = [];
  const h = harness({ listing: { plugins: [{ ...manifest, enabled: true }] }, desktopHost: {
    openSelfAnalysisWindow: async scope => { opened.push(plain(scope)); return true; },
    syncSelfAnalysisWindow: value => updates.push(plain(value)),
  } });
  await settle(); h.slots.onSessionChanged("account-a", "friend-a", "朋友甲");
  const [item] = h.navItems();
  assert.equal(manifest.contributes.nav[0].icon, "self");
  assert.deepEqual(item.children[0].children.map(shape => shape.tagName), ["path"]);
  await item.emit("click");
  assert.deepEqual(opened, [{ account: "account-a", theme: "dark" }]);
  assert.deepEqual(h.views, []); assert.equal(h.frames().length, 0);
  h.slots.onSessionChanged("account-b", "", "");
  assert.deepEqual(updates.at(-1), { context: { account: "account-b", theme: "dark" } });
  h.slots.onAccountCleared("account-b");
  assert.ok(updates.some(value => value.event?.type === "account-cleared" && value.event.account === "account-b"));
  h.slots.apply({ plugins: [{ ...manifest, enabled: false }] });
  assert.deepEqual(updates.at(-1), { event: { type: "plugins-changed", enabled: false } });
  await h.slots.openNav(manifest.id, "self-analysis"); assert.equal(opened.length, 1);
});

test("a refused native 自我剖析 open is visible to the user and never falls through to an embedded page", async () => {
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] }, desktopHost: {
    openSelfAnalysisWindow: async () => false,
  } });
  await settle(); await h.navItems()[0].emit("click");
  assert.match(h.toasts.at(-1), /自我剖析窗口未能打开/); assert.deepEqual(h.views, []); assert.equal(h.frames().length, 0);
});

test("the independent 自我剖析 host reuses the bounded model/profile methods and rejects stale scopes", async () => {
  let receive; const replies = [];
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] }, desktopHost: {
    onSelfAnalysisHostCall: callback => { receive = callback; },
    replySelfAnalysisHostCall: (id, value) => replies.push({ id, ...plain(value) }),
  }, modelSource: async () => ({ ok: true, json: async () => ({ mode: "api", api: { model: "saved", baseUrl: "private" } }) }) });
  await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  receive({ id: "model", method: "model.status", scope: { account: "account-a", user: "" } }); await settle();
  assert.deepEqual(replies.at(-1), { id: "model", result: { configured: true } });
  const reads = h.calls.length;
  receive({ id: "stale", method: "self-profile.read", params: { account: "old-account" }, scope: { account: "old-account" } }); await settle();
  assert.equal(replies.at(-1).error.code, "context-changed"); assert.equal(h.calls.length, reads);
  receive({ id: "denied", method: "file.pick", params: { kind: "file" }, scope: { account: "account-a" } }); await settle();
  assert.equal(replies.at(-1).error.code, "unknown-method");
  receive({ id: "focus", method: "view.focusChat", scope: { account: "account-a" } }); await settle();
  assert.deepEqual(replies.at(-1), { id: "focus", result: { ok: true } }); assert.equal(h.ids.get("chatInput").focused, true);
  const opened = []; h.window.openAppSettings = target => { opened.push(plain(target)); };
  receive({ id: "own", method: "settings.open", params: { section: "plugin", plugin: "kyf.advisor" }, scope: { account: "account-a" } }); await settle();
  receive({ id: "model-settings", method: "settings.open", params: { section: "model" }, scope: { account: "account-a" } }); await settle();
  receive({ id: "bad", method: "settings.open", params: { section: "plugins" }, scope: { account: "account-a" } }); await settle();
  assert.deepEqual(opened, [{ tab: "plugins", plugin: "kyf.self-analysis" }, { tab: "models" }],
    "the page opens only its own plugin settings, whatever plugin it names");
  assert.equal(replies.at(-1).error.code, "invalid-request");
  h.slots.apply({ plugins: [] });
  receive({ id: "disabled", method: "model.status", scope: { account: "account-a" } }); await settle();
  assert.equal(replies.at(-1).error.code, "plugin-disabled");
});

test("an independent panel never receives model status from an account that changed during the read", async () => {
  let receive, release; const replies = [];
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] }, desktopHost: {
    onSelfAnalysisHostCall: callback => { receive = callback; },
    replySelfAnalysisHostCall: (id, value) => replies.push({ id, ...plain(value) }),
  }, modelSource: () => new Promise(resolve => { release = resolve; }) });
  await settle(); h.slots.onSessionChanged("account-a", "", "");
  receive({ id: "late", method: "model.status", scope: { account: "account-a" } }); await settle();
  h.slots.onSessionChanged("account-b", "", "");
  release({ ok: true, json: async () => ({ mode: "none", api: null }) }); await settle();
  assert.equal(replies.at(-1).error.code, "context-changed"); assert.equal(replies.at(-1).result, undefined);
});

test("toggling a plugin in settings adds and removes its entries without a reload", async () => {
  const h = harness({ listing: { plugins: [] } }); await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  assert.equal(h.navItems().length, 0); assert.equal(h.buttons().length, 0);
  h.window.dispatch("kyf-plugins-changed", { plugins: [plugin("kyf.advisor")] });
  assert.equal(h.navItems().length, 1); assert.equal(h.buttons().length, 1);
  await h.navItems()[0].emit("click"); assert.deepEqual(h.views, ["plugin:kyf.advisor/repository"]);
  await h.buttons()[0].emit("click"); await settle(); assert.equal(h.calls.filter(call => call[0] === "open").length, 1);
  h.window.dispatch("kyf-plugins-changed", { plugins: [plugin("kyf.advisor", { enabled: false })] });
  await settle();
  assert.equal(h.navItems().length, 0); assert.equal(h.buttons().length, 0); assert.equal(h.frames().length, 0);
  assert.equal(h.views.at(-1), "chat", "an open page of a disabled plugin returns to the chat");
  assert.deepEqual(h.calls.filter(call => call[0] === "retain").at(-1), ["retain", { targets: [], account: "account-a" }],
    "the disabled plugin's windows close");
});

test("dependency errors withdraw nav, toolbar and contact entries and recovery restores them", async () => {
  const advisor = plugin("kyf.advisor"), analysis = { ...JSON.parse(read("plugins/kyf.friend-analysis/plugin.json")), enabled: true };
  const h = harness({ listing: { plugins: [advisor, analysis] } }); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "甲");
  await h.navItems()[0].emit("click");
  assert.equal(h.frames().length, 1); assert.equal(h.buttons().length, 1); assert.equal(h.headerButtons().length, 1);
  const dependencyError = { code: "plugin-dependency-disabled", message: "依赖插件 dev.provider 已停用" };
  h.window.dispatch("kyf-plugins-changed", { plugins: [advisor, analysis].map(item => ({ ...item, dependencies: ["dev.provider"], dependencyError })) });
  assert.equal(h.navItems().length, 0); assert.equal(h.buttons().length, 0); assert.equal(h.headerButtons().length, 0);
  assert.equal(h.frames().length, 0); assert.equal(h.views.at(-1), "chat");
  assert.deepEqual(h.calls.filter(call => call[0] === "retain").at(-1), ["retain", { targets: [], account: "account-a" }]);
  h.window.dispatch("kyf-plugins-changed", { plugins: [advisor, analysis] });
  assert.equal(h.navItems().length, 1); assert.equal(h.buttons().length, 1); assert.equal(h.headerButtons().length, 1);
});

test("知己分析 contributes its contact-window entry only while enabled and compatible", async () => {
  const h = harness({ listing: { plugins: [] } });
  assert.equal(h.headerButtons().length, 0, "no entry while the initial plugin listing is pending");
  await settle();
  assert.equal(h.headerButtons().length, 0, "all plugins disabled leaves no entry");
  const manifest = JSON.parse(read("plugins/kyf.friend-analysis/plugin.json"));
  const analysis = { ...manifest, enabled: true };
  h.window.dispatch("kyf-plugins-changed", { plugins: [analysis,
    { ...analysis, id: "kyf.off", enabled: false }, { ...analysis, id: "kyf.bad", incompatible: "core api" },
    { id: "kyf.message-insights", enabled: true, contributes: { chatHeader: [{ id: "toggle", label: "分析与建议", kind: "toggle" }] } }] });
  const [button] = h.headerButtons();
  assert.equal(h.headerButtons().length, 1);
  assert.equal(button.textContent, "知己分析"); assert.equal(button.hidden, false); assert.equal(button.disabled, false);
  assert.equal(button.title, "打开当前联系人的知己分析");
  assert.deepEqual(h.ids.get("chatHistoryActions").children.map(node => node.id || node.textContent), ["btnReturnLatest", "知己分析", "btnChatHistory"]);
  assert.equal(h.slots.hasFriendEntry("kyf.friend-analysis", "analysis"), true);
  await button.emit("click");
  assert.deepEqual(h.calls.filter(call => call[0] === "friend"), [["friend", { account: "account-a", user: "friend-a", name: "朋友甲" }]]);
  h.window.dispatch("kyf-plugins-changed", { plugins: [{ ...analysis, enabled: false }] });
  assert.equal(h.headerButtons().length, 0); assert.equal(h.slots.hasFriendEntry("kyf.friend-analysis", "analysis"), false);
  await button.emit("click"); await h.friendUi.openFriendWindow("kyf.friend-analysis", "analysis");
  assert.equal(h.calls.filter(call => call[0] === "friend").length, 1, "removed entries cannot open a window");
  h.window.dispatch("kyf-plugins-changed", { plugins: [analysis] });
  assert.equal(h.headerButtons().length, 1, "re-enabling restores the entry immediately");
  h.window.dispatch("kyf-plugins-changed", { plugins: [{ ...analysis, contributes: {} }] });
  assert.equal(h.headerButtons().length, 0, "removing the contribution removes the entry");
});

test("the contributed analysis entry preserves group hiding and session readiness across plugin refreshes", async () => {
  const analysis = { ...JSON.parse(read("plugins/kyf.friend-analysis/plugin.json")), enabled: true };
  const h = harness({ listing: { plugins: [analysis] } }); await settle();
  h.chatState.currentUser = "group@chatroom"; h.friendUi.updateFriendButton();
  assert.equal(h.headerButtons()[0].hidden, true); assert.equal(h.headerButtons()[0].disabled, true);
  h.slots.apply({ plugins: [analysis] });
  assert.equal(h.headerButtons()[0].hidden, true, "refreshing entries cannot reveal a group-chat analysis button");
  await h.headerButtons()[0].emit("click");
  assert.equal(h.calls.filter(call => call[0] === "friend").length, 0);
  h.chatState.currentUser = "friend-a"; h.chatState.messageSourceReady = false; h.friendUi.updateFriendButton();
  assert.equal(h.headerButtons()[0].hidden, false); assert.equal(h.headerButtons()[0].disabled, true);
  await h.headerButtons()[0].emit("click");
  assert.equal(h.calls.filter(call => call[0] === "friend").length, 0);
  h.chatState.messageSourceReady = true; h.friendUi.updateFriendButton();
  assert.equal(h.headerButtons()[0].disabled, false);
});

test("settings changes reach contribution slots before asynchronous message annotations refresh", async () => {
  const analysis = { ...JSON.parse(read("plugins/kyf.friend-analysis/plugin.json")), enabled: true };
  const h = harness({ listing: { plugins: [analysis] } }); await settle();
  const pending = [];
  h.window.MessageAnalysis = { pluginsChanged: () => new Promise(resolve => pending.push(resolve)) };
  h.window.dispatchEvent = event => h.window.dispatch(event.type, event.detail);
  const start = appSource.indexOf("async function pluginsChanged(listing)"), end = appSource.indexOf("\nwindow.PluginSettings", start);
  const context = vm.createContext({ window: h.window, chatState: { view: "chat", currentUser: null },
    byId: h.document.getElementById, CustomEvent: class { constructor(type, { detail }) { Object.assign(this, { type, detail }); } } });
  vm.runInContext(appSource.slice(start, end) + "\nglobalThis.changed = pluginsChanged;", context);
  const disabled = context.changed({ plugins: [{ ...analysis, enabled: false }] });
  assert.equal(h.headerButtons().length, 0, "a pending annotation request cannot leave the entry visible");
  const enabled = context.changed({ plugins: [analysis] });
  assert.equal(h.headerButtons().length, 1);
  pending[1](); await enabled;
  pending[0](); await disabled;
  assert.equal(h.headerButtons().length, 1, "an older annotation refresh cannot restore an obsolete plugin listing");
});

test("a late initial listing cannot revive an entry disabled in settings", async () => {
  const analysis = { ...JSON.parse(read("plugins/kyf.friend-analysis/plugin.json")), enabled: true };
  let resolve;
  const h = harness({ loadListing: () => new Promise(done => { resolve = done; }) }); await settle();
  h.window.dispatch("kyf-plugins-changed", { plugins: [{ ...analysis, enabled: false }] });
  resolve({ plugins: [analysis] }); await settle();
  assert.equal(h.headerButtons().length, 0);
});

test("a nav page fills the plugin view in a sandboxed frame with the nav context and is kept between visits", async () => {
  const h = harness(); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "朋友甲");
  h.slots.openNav("kyf.advisor", "repository");
  assert.equal(h.ids.get("pluginNavView").classList.contains("active"), true);
  assert.equal(h.navItems()[0].classList.contains("active"), true);
  const [frame] = h.frames();
  assert.equal(frame.src, "/plugin-pages/kyf.advisor/ui/repository.html"); assert.equal(frame.attributes.sandbox, "allow-scripts");
  await h.fromPage(frame, { kyf: 1, type: "ready" });
  assert.deepEqual(plain(frame.contentWindow.messages[0].context), { account: "account-a", user: "friend-a", name: "朋友甲",
    theme: "dark", surface: "nav", plugin: "kyf.advisor" });
  h.slots.onViewChanged("chat");
  assert.equal(h.ids.get("pluginNavView").classList.contains("active"), false); assert.equal(h.navItems()[0].classList.contains("active"), false);
  h.slots.openNav("kyf.advisor", "repository");
  assert.equal(h.frames().length, 1, "the page keeps its state"); assert.equal(frame.contentWindow.messages.at(-1).event.type, "shown");
  h.body.className = "theme-light"; h.observers[0]();
  assert.equal(frame.contentWindow.messages.filter(item => item.type === "context").at(-1).context.theme, "light");
});

test("nav pages copy, pick files as grants, focus the chat and toast through the host only", async () => {
  const h = harness(); await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  h.slots.openNav("kyf.advisor", "repository"); const [frame] = h.frames();
  await h.fromPage(frame, { kyf: 1, type: "ready" });
  const call = (id, method, params) => h.fromPage(frame, { kyf: 1, type: "call", id, target: "host", method, params });
  assert.deepEqual(plain((await call("c1", "clipboard.copy", { text: "回答" })).result), { copied: true });
  assert.deepEqual(plain((await call("c2", "file.pick", { kind: "file" })).result), { token: "fg_" + "a".repeat(32), label: "包.zip" });
  assert.deepEqual(h.calls.filter(item => item[0] === "copy" || item[0] === "pick"), [["copy", "回答"], ["pick", "kyf.advisor", "file"]]);
  assert.equal((await call("c3", "file.pick", { kind: "device" })).error.code, "invalid-request");
  await call("c4", "toast", { message: "已保存" }); assert.deepEqual(h.toasts, ["已保存"]);
  await call("c5", "view.focusChat"); assert.equal(h.views.at(-1), "chat"); assert.equal(h.ids.get("chatInput").focused, true);
  assert.equal((await call("c6", "panel.close")).error.code, "unknown-method");
  assert.equal((await call("c7", "observations.save", {})).error.code, "unknown-method");
  // Added 2026-10-14 (review): as in the panel, a page copies only right after the user's gesture.
  h.window.navigator.userActivation = { isActive: false };
  assert.deepEqual(plain((await call("c8", "clipboard.copy", { text: "无手势" })).result), { copied: false });
  assert.equal(h.calls.some(item => item[0] === "copy" && item[1] === "无手势"), false);
});

test("继续聊这句 is offered only while a chat-toolbar panel is contributed", async () => {
  // Added 2026-10-14 (review).
  const h = harness({ listing: { plugins: [] } }); await settle();
  assert.equal(h.slots.canContinue(), false);
  h.window.dispatch("kyf-plugins-changed", { plugins: [plugin("kyf.advisor")] });
  assert.equal(h.slots.canContinue(), true);
  h.window.dispatch("kyf-plugins-changed", { plugins: [plugin("kyf.advisor", { enabled: false })] });
  assert.equal(h.slots.canContinue(), false);
});

// Modified 2026-10-22: each friend's 聊天助手 is an own window; several stay open side by side.
test("the toolbar button opens the current friend's own window, and other friends' windows stay open", async () => {
  const h = harness(); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "甲");
  await h.buttons()[0].emit("click"); await settle();
  assert.deepEqual(h.calls.filter(call => call[0] === "open").at(-1), ["open", { plugin: "kyf.advisor", page: "ui/panel.html",
    account: "account-a", user: "friend-a", name: "甲", label: "聊天助手", theme: "dark" }]);
  assert.equal(h.buttons()[0].classList.contains("active"), true);
  const before = h.calls.length;
  h.slots.onSessionChanged("account-a", "friend-b", "乙");
  assert.equal(h.buttons()[0].classList.contains("active"), false, "乙 has no window yet");
  assert.equal(h.calls.slice(before).some(call => ["open", "event", "retain", "forget"].includes(call[0])), false,
    "switching the chat neither moves nor closes 甲's window");
  await h.buttons()[0].emit("click"); await settle();
  assert.equal(h.calls.filter(call => call[0] === "open").at(-1)[1].user, "friend-b");
  assert.equal(h.buttons()[0].classList.contains("active"), true);
  h.slots.onSessionChanged("account-a", "friend-a", "甲");
  assert.equal(h.buttons()[0].classList.contains("active"), true, "both windows stay open");
  h.slots.onViewChanged("plugin:kyf.advisor/repository"); h.slots.onViewChanged("chat"); await settle();
  assert.equal(h.buttons()[0].classList.contains("active"), true, "other views leave the windows alone");
  h.setHostWindows([]); assert.equal(h.buttons()[0].classList.contains("active"), false);
  h.slots.onSessionChanged("account-b", "friend-x", "X");
  assert.deepEqual(h.calls.filter(call => call[0] === "retain").at(-1),
    ["retain", { targets: [{ plugin: "kyf.advisor", page: "ui/panel.html" }], account: "account-b" }], "another account's windows close");
});

test("window refusals are shown and never mark the button active; no chat or a browser has no window", async () => {
  const h = harness({ openResult: { opened: false, error: "窗口未能打开" } }); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "甲");
  assert.equal(await h.slots.openWindow("kyf.advisor", "advisor"), false);
  assert.deepEqual(h.toasts, ["窗口未能打开"]); assert.equal(h.buttons()[0].classList.contains("active"), false);
  const none = harness(); await settle(); none.slots.onSessionChanged("account-a", "", "");
  assert.equal(await none.slots.openWindow("kyf.advisor", "advisor"), false);
  assert.deepEqual(none.toasts, ["请先打开一个聊天，再使用聊天助手"]); assert.equal(none.calls.filter(call => call[0] === "open").length, 0);
  const browser = harness({ desktopHost: null }); await settle(); browser.slots.onSessionChanged("account-a", "friend-a", "甲");
  assert.equal(await browser.slots.openWindow("kyf.advisor", "advisor"), false);
  assert.deepEqual(browser.toasts, ["聊天助手需要在桌面客户端中使用"]);
});

test("the panel's host calls are answered here: focus and toast only", async () => {
  const h = harness(); await settle();
  await h.hooks.hostCall({ id: "p1", method: "view.focusChat", params: {} }); await settle();
  await h.hooks.hostCall({ id: "p2", method: "toast", params: { message: "x".repeat(200) } }); await settle();
  await h.hooks.hostCall({ id: "p3", method: "file.pick", params: { kind: "file" } }); await settle();
  const replies = h.calls.filter(call => call[0] === "reply");
  assert.deepEqual(replies[0], ["reply", "p1", { result: { ok: true } }]);
  assert.deepEqual(replies[1], ["reply", "p2", { result: { ok: true } }]); assert.equal(h.toasts[0].length, 120);
  assert.equal(replies[2][2].error.code, "unknown-method");
  assert.equal(h.calls.filter(call => call[0] === "pick").length, 0);
});

test("clearing an account tells every page and resets the friend context", async () => {
  const h = harness(); await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  h.slots.openNav("kyf.advisor", "repository"); const [frame] = h.frames(); await h.fromPage(frame, { kyf: 1, type: "ready" });
  h.slots.onAccountCleared("account-b");
  h.slots.onAccountCleared("account-a");
  const events = frame.contentWindow.messages.filter(item => item.type === "event").map(item => plain(item.event));
  assert.deepEqual(events, [{ type: "account-cleared", account: "account-b" }, { type: "account-cleared", account: "account-a" }]);
  assert.deepEqual(plain(frame.contentWindow.messages.at(-1).context), { account: "", user: "", name: "", theme: "dark", surface: "nav", plugin: "kyf.advisor" });
  assert.deepEqual(h.calls.filter(call => call[0] === "forget").map(call => call[1]), ["account-b", "account-a"],
    "a cleared account's windows close");
});

test("继续聊这句 opens the friend's window and hands it only the source quote of the current friend", async () => {
  const h = harness(); await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  assert.equal(await h.slots.continueFromMessage({ account: "account-a", user: "friend-b", messageId: "m", text: "x" }), false);
  assert.equal(h.calls.filter(call => call[0] === "open").length, 0);
  assert.equal(await h.slots.continueFromMessage({ account: "account-a", user: "friend-a", messageId: "m1", text: "合成原话", kind: "text" }), true);
  assert.equal(h.calls.filter(call => call[0] === "open").at(-1)[1].user, "friend-a");
  assert.deepEqual(h.calls.at(-1), ["event", { target: { plugin: "kyf.advisor", account: "account-a", user: "friend-a" },
    event: { type: "quote", account: "account-a", user: "friend-a", messageId: "m1", text: "合成原话" } }]);
});

test("the main window hosts plugin pages only through the generic slots", () => {
  for (const removed of ['id="navAdvisor"', 'id="btnToolbarAdvisor"', 'id="advisorRepoView"', "advisor.css", "advisor.js", "advisor-nav-icon"])
    assert.ok(!indexHtml.includes(removed), removed);
  assert.match(indexHtml, /<div class="pane-view" id="pluginNavView"><\/div>/);
  assert.match(indexHtml, /class="toolbar-left" id="chatToolbar"/);
  assert.match(indexHtml, /<link rel="stylesheet" href="plugin-host.css">/);
  const scripts = [...indexHtml.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);
  assert.ok(scripts.indexOf("plugin-frame.js") < scripts.indexOf("plugin-slots.js"));
  assert.ok(scripts.indexOf("plugin-slots.js") < scripts.indexOf("app.js"));
  assert.ok(!/window\.Advisor\b/.test(appSource));
  assert.match(appSource, /window\.PluginSlots\?\.init\(\{ switchView, toast, openFriendWindow, updateFriendButton,/);
  assert.doesNotMatch(appSource, /onAttention\s*:\s*renderSessions/);
  const sessionRenderer = appSource.slice(appSource.indexOf("function renderSessions()"), appSource.indexOf("async function preloadSessionWindows("));
  assert.doesNotMatch(sessionRenderer, /attentionFor|session-attention-dot/);
  const emojiHandler = appSource.split('byId("btnEmoji").addEventListener')[1].split("\n")[0];
  assert.ok(emojiHandler.includes('byId("emojiPopover").classList.toggle("show")'));
});

test("switchView shows plugin views through the slots and keeps the model-trace scope", () => {
  const start = appSource.indexOf("function switchView(target)"), end = appSource.indexOf("\nfunction applySettings(", start);
  const nodes = new Map(), seen = [];
  const context = vm.createContext({ friendWindowRequest: 0, updateFriendButton() {}, scrollToLatest() {},
    chatState: { view: "chat", historyState: null, currentAccount: "a", currentUser: "u", sessions: new Map([["u", { name: "好友" }]]) },
    byId(id) { if (!nodes.has(id)) nodes.set(id, { classList: { toggle(name, on) { seen.push([id, name, on]); } } }); return nodes.get(id); },
    window: { PluginSlots: { onViewChanged: view => seen.push(["slots", view]) }, ModelTraces: { setScope: (...args) => seen.push(["traces", ...args]) } } });
  vm.runInContext(appSource.slice(start, end) + "\nglobalThis.switchView = switchView; globalThis.name = pluginFriendName;", context);
  context.switchView("plugin:kyf.advisor/repository");
  assert.deepEqual(seen, [["chatView", "active", false], ["navChat", "active", false], ["slots", "plugin:kyf.advisor/repository"],
    ["traces", "a", "u", "plugin:kyf.advisor/repository"]]);
  assert.equal(context.chatState.view, "plugin:kyf.advisor/repository");
  assert.equal(context.name("u"), "好友"); assert.equal(context.name("x"), "x"); assert.equal(context.name(null), "");
});

test("current-account deletion clears plugin pages using resolved account identity, not its public hash", async () => {
  const start = appSource.indexOf("async function deleteManagedAccount()");
  const end = appSource.indexOf("\nasync function copyDraft(", start);
  const account = "synthetic-current-account";
  const accountId = crypto.createHash("sha256").update(account).digest("hex");
  const cleared = [], announced = [], nodes = new Map();
  const context = vm.createContext({
    announceFriendAccount: (type, value) => announced.push([type, value]),
    managedAccounts: [{ accountId }], pendingDeleteAccountId: accountId, accountDeleteBusy: false,
    accountClearedExiting: false, selectedManagedAccountId: accountId,
    byId(id) { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    renderManagedAccounts() {}, text() {}, resetAccountView() {}, clearTimeout() {}, startupWatchdog: null,
    chatState: { sessionRequest: 1, advance() {}, currentUser: "synthetic-friend", controller: null },
    fetch: async () => ({ ok: true, json: async () => ({ deleted: accountId, current: true, exitApp: true }) }),
    clearAccountUiData: async id => { assert.equal(id, accountId); return new Set([account]); },
    window: { PluginSlots: { onAccountCleared: id => cleared.push(id) }, desktopHost: { exitApp: async () => true } },
  });
  vm.runInContext(appSource.slice(start, end) + "\nglobalThis.clear = deleteManagedAccount;", context);
  await context.clear();
  assert.deepEqual(cleared, [account]);
  assert.equal(context.accountClearedExiting, true);
  assert.deepEqual(announced, [["account-cleared", accountId]]);
});

test("a call naming another friend than the frame's current or previous context is refused", async () => {
  const h = harness(); await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  h.slots.openNav("kyf.advisor", "repository"); const [frame] = h.frames(); await h.fromPage(frame, { kyf: 1, type: "ready" });
  const result = await h.fromPage(frame, { kyf: 1, type: "call", id: "c1", target: "plugin", method: "catalog.get", scope: { account: "x", user: "y" } });
  assert.equal(result.error.code, "context-changed");
  assert.equal(h.calls.filter(call => call[0] === "fetch" && call[1] === "/api/plugins/rpc").length, 0);
});

test("自我剖析 edits the Core account profile without requiring a selected friend", async () => {
  const profile = { background: "合成资料", style: "简短", version: 2 };
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] },
    selfProfile: async () => ({ ok: true, json: async () => profile }) });
  await settle(); h.slots.onSessionChanged("account-a", null);
  h.slots.openNav("kyf.self-analysis", "repository");
  const [frame] = h.frames(); await h.fromPage(frame, { kyf: 1, type: "ready" });
  const result = await h.fromPage(frame, { kyf: 1, type: "call", id: "profile", target: "host", method: "self-profile.save",
    params: { account: "account-a", background: "合成资料", style: "简短", expectedVersion: 1 } });
  assert.deepEqual(plain(result.result), profile);
  assert.deepEqual(h.calls.find(call => call[1] === "/api/account/self-profile")[2],
    { account: "account-a", background: "合成资料", style: "简短", expectedVersion: 1 });
  h.slots.onSessionChanged("account-b", null);
  const stale = await h.fromPage(frame, { kyf: 1, type: "call", id: "stale", target: "host", method: "self-profile.save",
    params: { account: "account-a", background: "旧稿", style: "旧稿", expectedVersion: 2 } });
  assert.equal(stale.error.code, "context-changed");
  assert.equal(h.calls.filter(call => call[1] === "/api/account/self-profile").length, 1);
});

test("profile conflicts preserve Core's error and other plugins cannot use this write capability", async () => {
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis"), plugin("kyf.advisor")] },
    selfProfile: async () => ({ ok: false, json: async () => ({ error: "profile-conflict", message: "资料已变化" }) }) });
  await settle(); h.slots.onSessionChanged("account-a", null);
  h.slots.openNav("kyf.self-analysis", "repository"); const [self] = h.frames();
  await h.fromPage(self, { kyf: 1, type: "ready" });
  const conflict = await h.fromPage(self, { kyf: 1, type: "call", id: "conflict", target: "host", method: "self-profile.save",
    params: { account: "account-a", background: "合成", style: "合成", expectedVersion: 0 } });
  assert.equal(conflict.error.code, "profile-conflict");
  h.slots.openNav("kyf.advisor", "repository"); const other = h.frames().at(-1);
  await h.fromPage(other, { kyf: 1, type: "ready" });
  const denied = await h.fromPage(other, { kyf: 1, type: "call", id: "denied", target: "host", method: "self-profile.save",
    params: { account: "account-a" } });
  assert.equal(denied.error.code, "unknown-method");
});

test("a late profile read cannot populate a switched account", async () => {
  let release;
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] },
    selfProfile: () => new Promise(resolve => { release = resolve; }) });
  await settle(); h.slots.onSessionChanged("account-a", null);
  h.slots.openNav("kyf.self-analysis", "repository"); const [frame] = h.frames();
  await h.fromPage(frame, { kyf: 1, type: "ready" });
  await h.fromPage(frame, { kyf: 1, type: "call", id: "read", target: "host", method: "self-profile.read", params: { account: "account-a" } });
  h.slots.onSessionChanged("account-b", null);
  release({ ok: true, json: async () => ({ background: "旧账号", style: "旧风格", version: 1 }) }); await settle();
  assert.equal(frame.contentWindow.messages.at(-1).error.code, "context-changed");
});

test("自我剖析 reads only saved model configuration and never exposes the source profile", async () => {
  const marker = "private-profile-value";
  const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] }, modelSource: async (url, init) => {
    assert.equal(url, "/api/model-source"); assert.equal(init.method || "GET", "GET");
    assert.equal(init.cache, "no-store"); assert.equal(init.body, undefined);
    return { ok: true, json: async () => ({ mode: "api", api: { protocol: "chat-completions", model: "saved-model",
      hasKey: false, apiKey: marker, encryptedKey: marker, baseUrl: marker }, modelList: marker }) };
  } });
  await settle(); h.slots.onSessionChanged("", null);
  h.slots.openNav("kyf.self-analysis", "repository"); const [frame] = h.frames(); await h.fromPage(frame, { kyf: 1, type: "ready" });
  const result = await h.fromPage(frame, { kyf: 1, type: "call", id: "model", target: "host", method: "model.status", params: {} });
  assert.deepEqual(plain(result.result), { configured: true }, "a saved selection does not require an API key");
  assert.doesNotMatch(JSON.stringify(result), new RegExp(marker));
  assert.deepEqual(h.calls.filter(call => call[0] === "fetch" && call[1].startsWith("/api/model-source")).map(call => call[1]), ["/api/model-source"]);
});

test("saved model status distinguishes no selection from unknown provider structures", async () => {
  for (const [source, configured] of [[{ mode: "none", api: null }, false], [{ mode: "managed", connection: { saved: true } }, null],
    [{ mode: "api", api: { model: 123 } }, null]]) {
    const h = harness({ listing: { plugins: [plugin("kyf.self-analysis")] }, modelSource: async () => ({ ok: true, json: async () => source }) });
    await settle(); h.slots.openNav("kyf.self-analysis", "repository"); const [frame] = h.frames(); await h.fromPage(frame, { kyf: 1, type: "ready" });
    const result = await h.fromPage(frame, { kyf: 1, type: "call", id: "model", target: "host", method: "model.status", params: {} });
    assert.deepEqual(plain(result.result), { configured });
  }
});

test("model configuration reads return a bounded error and remain private to the 自我剖析 nav", async () => {
  const marker = "private-error-and-endpoint";
  for (const modelSource of [async () => { throw new Error(marker); }, async () => ({ ok: false, json: async () => ({ message: marker }) })]) {
    const h = harness({ listing: { plugins: [plugin("kyf.self-analysis"), plugin("kyf.advisor")] }, modelSource });
    await settle(); h.slots.openNav("kyf.self-analysis", "repository"); const [self] = h.frames(); await h.fromPage(self, { kyf: 1, type: "ready" });
    const failed = await h.fromPage(self, { kyf: 1, type: "call", id: "model", target: "host", method: "model.status", params: {} });
    assert.equal(failed.error.code, "model-status-unavailable"); assert.doesNotMatch(JSON.stringify(failed), new RegExp(marker));
    h.slots.openNav("kyf.advisor", "repository"); const other = h.frames().at(-1); await h.fromPage(other, { kyf: 1, type: "ready" });
    const denied = await h.fromPage(other, { kyf: 1, type: "call", id: "denied", target: "host", method: "model.status", params: {} });
    assert.equal(denied.error.code, "unknown-method");
    assert.equal(h.calls.filter(call => call[1] === "/api/model-source").length, 1);
  }
});

test("clearing the current account never serves it as the context just replaced, until it is shown again", async () => {
  // Added 2026-10-13 (review): onAccountCleared forgets the account before it resets the friend context.
  const h = harness(); await settle(); h.slots.onSessionChanged("account-a", "friend-a", "甲");
  h.slots.openNav("kyf.advisor", "repository"); const [frame] = h.frames(); await h.fromPage(frame, { kyf: 1, type: "ready" });
  const rpcs = () => h.calls.filter(call => call[0] === "fetch" && call[1] === "/api/plugins/rpc").length;
  const save = (id, user) => h.fromPage(frame, { kyf: 1, type: "call", id, target: "plugin", method: "draft.save", params: {},
    scope: { account: "account-a", user } });
  h.slots.onAccountCleared("account-a");
  assert.equal((await save("c1", "friend-a")).error.code, "context-changed");
  assert.equal(rpcs(), 0);
  h.slots.onSessionChanged("account-a", "friend-b", "乙");
  assert.equal((await save("c2", "friend-b")).error, undefined, "an account shown again is served");
  assert.equal(rpcs(), 1);
});


function attention(user, ids = ["task-one"], state = "done") {
  return { plugin: "kyf.advisor", user, state, finishedAt: 5, taskIds: ids,
    tasks: ids.map(id => ({ id, runId: "run-" + id, threadId: "thread-" + user })) };
}
async function acknowledge(h, user, taskIds, plugin = "kyf.advisor", account = "account-a", windowUser = user) {
  h.hooks.hostCall({ id: "seen-call", plugin, method: "attention.seen", params: { account, user, taskIds },
    scope: { account: "account-a", user: windowUser } });
  await settle();
}
test("completed and failed answers stay internal until acknowledged, without toolbar reminders", async () => {
  let items = [attention("friend-b"), attention("friend-c", ["task-failed"], "failed")];
  const h = harness({ attention: () => items }); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "甲"); await settle();
  assert.equal(h.slots.attentionFor("friend-b")[0].state, "done");
  h.slots.onSessionChanged("account-a", "friend-b", "乙");
  assert.equal(h.buttons()[0].classList.contains("has-attention"), false);
  assert.equal(h.buttons()[0].title, "");
  await h.buttons()[0].emit("click"); await settle();
  assert.equal(h.buttons()[0].classList.contains("has-attention"), false);
  assert.equal(h.slots.attentionFor("friend-b").length, 1, "opening is not a viewed acknowledgement");
  assert.equal(h.calls.filter(call => call[1] === "/api/attention/seen").length, 0);
  const check = h.calls.findLast(call => call[0] === "event" && call[1].event?.type === "attention-check" && call[1].event.user === "friend-b")[1].event;
  assert.deepEqual(check.tasks, items[0].tasks);
  await acknowledge(h, "friend-b", ["task-one"]);
  assert.deepEqual(h.calls.find(call => call[1] === "/api/attention/seen")[2],
    { plugin: "kyf.advisor", account: "account-a", user: "friend-b", taskIds: ["task-one"] });
  assert.deepEqual(plain(h.slots.attentionFor("friend-b")), []);
  h.slots.onSessionChanged("account-a", "friend-c", "丙");
  assert.equal(h.slots.attentionFor("friend-c")[0].state, "failed");
  assert.equal(h.buttons()[0].classList.contains("has-attention"), false);
  assert.equal(h.buttons()[0].title, "");
  // 丙's own window, open beside 乙's, is asked too, though the main window shows another chat.
  h.setHostWindows([{ plugin: "kyf.advisor", account: "account-a", user: "friend-b", visible: true },
    { plugin: "kyf.advisor", account: "account-a", user: "friend-c", visible: true }]);
  assert.equal(h.calls.findLast(call => call[0] === "event" && call[1].event?.type === "attention-check")[1].target.user, "friend-c");
});

test("a pinned assistant receives and acknowledges its completed answer after the main chat changes", async () => {
  const h = harness({ attention: () => [attention("friend-b")] }); await settle();
  h.slots.onSessionChanged("account-a", "friend-b", "乙");
  await h.buttons()[0].emit("click"); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "甲");
  assert.ok(h.calls.some(call => call[0] === "event" && call[1].event?.type === "attention-check" && call[1].event.user === "friend-b"));
  const call = { id: "independent-seen", plugin: "kyf.advisor", independent: true, method: "attention.seen",
    scope: { account: "account-a", user: "friend-b" }, params: { account: "account-a", user: "friend-b", taskIds: ["task-one"] } };
  h.hooks.hostCall({ ...call, scope: { ...call.scope, user: "friend-c" } }); await settle();
  assert.equal(h.calls.filter(item => item[1] === "/api/attention/seen").length, 0);
  h.hooks.hostCall(call); await settle();
  assert.deepEqual(plain(h.slots.attentionFor("friend-b")), []);
});
test("failed acknowledgements retry, and hidden windows or other scopes cannot mark completions seen", async () => {
  let fail = true;
  const h = harness({ attention: () => [attention("friend-b")], seen: () => ({ ok: !fail, json: async () => ({ seenAt: 1 }) }) });
  await settle(); h.slots.onSessionChanged("account-a", "friend-b", "乙"); await settle();
  await h.buttons()[0].emit("click"); await settle();
  h.setHostWindows([{ plugin: "kyf.advisor", account: "account-a", user: "friend-b", visible: false }]);
  await acknowledge(h, "friend-b", ["task-one"]);
  assert.equal(h.calls.filter(call => call[1] === "/api/attention/seen").length, 0, "a minimized window has seen nothing");
  h.setHostWindows([{ plugin: "kyf.advisor", account: "account-a", user: "friend-b", visible: true }]);
  await acknowledge(h, "friend-b", ["task-one"], "other.plugin");
  await acknowledge(h, "friend-b", ["task-one"], "kyf.advisor", "account-old");
  await acknowledge(h, "friend-b", ["task-one"], "kyf.advisor", "account-a", "friend-c");
  assert.equal(h.calls.filter(call => call[1] === "/api/attention/seen").length, 0);
  await acknowledge(h, "friend-b", ["task-one"]);
  assert.equal(h.slots.attentionFor("friend-b").length, 1);
  const checks = h.calls.filter(call => call[0] === "event" && call[1].event?.type === "attention-check").length;
  await h.intervals[0](); await settle();
  assert.ok(h.calls.filter(call => call[0] === "event" && call[1].event?.type === "attention-check").length > checks);
  fail = false; await acknowledge(h, "friend-b", ["task-one"]);
  assert.equal(h.slots.attentionFor("friend-b").length, 0);
});
test("acknowledging an observed completion cannot swallow a new task completed in the same millisecond", async () => {
  let items = [attention("friend-b")], complete;
  const h = harness({ attention: () => items, seen: () => new Promise(resolve => { complete = resolve; }) });
  await settle(); h.slots.onSessionChanged("account-a", "friend-b", "乙"); await settle();
  await h.buttons()[0].emit("click"); await settle();
  const ack = acknowledge(h, "friend-b", ["task-one"]); await settle();
  items = [attention("friend-b", ["task-one", "task-two"])];
  await h.slots.loadAttention(); await settle();
  complete({ ok: true, json: async () => ({ seenAt: 5 }) }); await ack; await settle();
  assert.deepEqual(plain(h.slots.attentionFor("friend-b")[0].taskIds), ["task-two"]);
  assert.equal(h.buttons()[0].classList.contains("has-attention"), false);
});
test("attention follows the account and is dropped for a cleared one", async () => {
  const h = harness({ attention: () => [attention("friend-b")] }); await settle();
  h.slots.onSessionChanged("account-a", "friend-a", "甲"); await settle();
  assert.equal(h.slots.attentionFor("friend-b").length, 1);
  h.slots.onAccountCleared("account-a"); await settle();
  assert.deepEqual(plain(h.slots.attentionFor("friend-b")), []);
  await h.intervals[0](); await settle();
  assert.deepEqual(plain(h.slots.attentionFor("friend-b")), []);
});
