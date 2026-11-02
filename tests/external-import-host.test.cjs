// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic standalone-window tests. No runtime data or native windows are used.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const read = name => readFileSync(path.join(__dirname, "..", name), "utf8");
const source = read("chatui/external-import-host.js"), indexHtml = read("chatui/index.html");
const PLUGIN = "kyf.external-chat-import";
const plugin = (changes = {}) => ({ id: PLUGIN, enabled: true, ...changes });
const listing = (...plugins) => ({ plugins });
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(setImmediate); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness(options = {}) {
  const calls = [], views = [], popups = [], nativeCalls = [], notices = [], selected = [], listeners = new Map(), ids = new Map();
  const button = () => ({ hidden: false, disabled: false, clicks: 0, listeners: new Map(),
    addEventListener(type, handler) { this.listeners.set(type, handler); },
    click() { if (!this.disabled && !this.hidden) { this.clicks++; this.listeners.get("click")?.(); } },
    dispatch() { this.listeners.get("click")?.(); } });
  for (const id of ["btnExternalImport", "btnSettings", "btnModelTraces"]) ids.set(id, button());
  const document = { getElementById: id => ids.get(id), querySelector: () => null,
    createElement() { throw new Error("external chat must not create an embedded panel"); } };
  const location = { origin: "http://127.0.0.1:48120", search: "" };
  const native = options.native ? {
    openExternalImportWindow(scope) { nativeCalls.push(plain(scope)); return options.nativeOpen?.(scope) ?? Promise.resolve(true); },
    onExternalImportNavigate(callback) { this.navigate = callback; },
    notifyExternalImportWindows(value) { this.notified = plain(value); return Promise.resolve(true); },
  } : undefined;
  const window = { location, document, desktopHost: native,
    addEventListener: (type, callback) => { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(callback); },
    focus() { this.focused = true; },
    open(url, name, features) {
      if (options.blocked) return null;
      const popup = { url, name, features, closed: false, focusCount: 0, messages: [],
        focus() { this.focusCount++; }, close() { this.closed = true; },
        postMessage(message, origin) { this.messages.push({ message: plain(message), origin }); } };
      popups.push(popup); return popup;
    },
  };
  const dispatch = (type, event = {}) => { for (const handler of listeners.get(type) || []) handler(event); };
  const chatState = { view: "chat", currentAccount: "synthetic-account", currentUser: "synthetic-friend",
    sessions: new Map([["synthetic-friend", { name: "合成朋友" }], ["synthetic-second", { name: "另一位合成朋友" }],
      ["synthetic-group@chatroom", { name: "合成群", isGroup: true }]]) };
  let fetchIndex = 0;
  vm.runInNewContext(source, { window, document, location, URLSearchParams, Promise,
    settingsState: { settings: { theme: "light" } }, chatState,
    pluginFriendName: user => chatState.sessions.get(user)?.name || user,
    switchView(target) { views.push(target); chatState.view = target; },
    switchSession(user) { selected.push(user); chatState.currentUser = user; },
    toast: value => notices.push(value),
    fetch: async (url, init = {}) => { calls.push({ url, init });
      return options.fetch ? options.fetch(++fetchIndex, url, init) : { ok: true, json: async () => options.listing || listing(plugin()) }; },
  });
  return { friend: ids.get("btnExternalImport"), calls, views, popups, nativeCalls, notices, selected, window, ids, chatState,
    dispatch, change: value => dispatch("kyf-plugins-changed", { detail: value }),
    message(popup, data, origin = location.origin) { dispatch("message", { origin, source: popup, data }); } };
}
test("settings windows do not initialize external chat", () => {
  const window = { desktopHost: { isSettingsWindow: true } };
  vm.runInNewContext(source, { window });
  assert.equal(window.ExternalImportHost, undefined);
});
test("external chat has only a friend entry and never creates an embedded pane", async () => {
  assert.doesNotMatch(indexHtml, /\bid="navExternalImport"/);
  assert.match(indexHtml.match(/<button\b[^>]*\bid="btnExternalImport"[^>]*>/)?.[0] || "", /\bhidden(?:\s|=|>)/);
  assert.doesNotMatch(source, /createElement|iframe|externalImportView|togglePluginPanel|openPluginPanel/);
  assert.equal((read("chatui/app.js").match(/window\.ExternalImportHost\?\.onSessionChanged\(/g) || []).length, 4);
  const h = harness(); await settle(); h.friend.click(); await settle();
  assert.deepEqual(h.views, []);
  assert.equal(h.chatState.view, "chat");
});
test("discovery keeps entry closed until a compatible enabled plugin is known", async () => {
  const pending = deferred(), h = harness({ fetch: () => pending.promise });
  assert.equal(h.friend.hidden, true); h.friend.dispatch(); assert.equal(h.popups.length, 0);
  pending.resolve({ ok: true, json: async () => listing(plugin()) }); await settle();
  h.friend.click(); assert.equal(h.popups.length, 1);
  const target = new URL(h.popups[0].url, h.window.location.origin);
  assert.equal(target.pathname, "/external-import.html");
  assert.equal(target.searchParams.get("user"), "synthetic-friend");
  assert.equal(target.searchParams.get("theme"), "light");
  assert.match(h.popups[0].features, /popup=yes/);
});
test("missing, disabled, incompatible or dependency-blocked plugins never expose the friend entry", async () => {
  const h = harness({ listing: listing() }); await settle();
  for (const value of [listing(), listing(plugin({ enabled: false })), listing(plugin({ enabled: "true" })),
    listing(plugin({ incompatible: "mismatch" })), listing(plugin({ dependencyError: { code: "plugin-dependency-missing", message: "缺少依赖插件 dev.provider" } })), listing(plugin({ id: "unrelated" }))]) {
    h.change(value); assert.equal(h.friend.hidden, true); h.friend.dispatch();
  }
  assert.equal(h.popups.length, 0);
  h.change(listing(plugin())); assert.equal(h.friend.hidden, false);
});
test("native launch uses current friend scope and leaves main chat untouched", async () => {
  const h = harness({ native: true }); await settle();
  h.chatState.sessions.get("synthetic-friend").externalFriendId = "external-friend";
  h.friend.click(); await settle();
  assert.deepEqual(h.nativeCalls, [{ account: "synthetic-account", user: "synthetic-friend", name: "合成朋友",
    friendId: "external-friend", theme: "light" }]);
  assert.equal(h.popups.length, 0); assert.deepEqual(h.views, []);
  h.chatState.currentUser = "synthetic-second";
  h.window.ExternalImportHost.onSessionChanged("synthetic-account", "synthetic-second");
  assert.equal(h.nativeCalls.length, 1, "switching main friend never rescopes a child window");
  h.friend.click(); await settle();
  assert.equal(h.nativeCalls[1].user, "synthetic-second");
});
test("browser launch reuses only the same friend's open window", async () => {
  const h = harness(); await settle(); h.friend.click(); h.friend.click();
  const first = h.popups[0], initial = first.url;
  assert.equal(h.popups.length, 1); assert.equal(first.focusCount, 2);
  h.chatState.currentUser = "synthetic-second";
  h.window.ExternalImportHost.onSessionChanged("synthetic-account", "synthetic-second");
  assert.equal(first.url, initial); assert.deepEqual(first.messages, []);
  h.friend.click(); assert.equal(h.popups.length, 2);
  h.chatState.currentUser = "synthetic-friend"; h.friend.click();
  assert.equal(h.popups.length, 2); assert.equal(first.focusCount, 3);
  first.closed = true; h.friend.click(); assert.equal(h.popups.length, 3);
});
test("disabled plugin removes access but retains open window and review drafts", async () => {
  const h = harness(); await settle(); h.friend.click();
  const popup = h.popups[0]; popup.draft = "合成未提交编辑";
  h.change(listing(plugin({ enabled: false })));
  assert.equal(h.friend.hidden, true); h.friend.dispatch();
  assert.equal(h.popups.length, 1); assert.equal(popup.closed, false); assert.equal(popup.draft, "合成未提交编辑");
  assert.equal(popup.messages.at(-1).message.type, "plugins-changed"); assert.deepEqual(h.views, []);
  h.change(listing(plugin())); h.friend.click(); assert.equal(h.popups.length, 1);
});

test("dependency loss closes entry access while preserving the existing popup draft and allows recovery", async () => {
  const h = harness(); await settle(); h.friend.click();
  const popup = h.popups[0]; popup.draft = "依赖恢复前的草稿";
  h.change(listing(plugin({ dependencyError: { code: "plugin-dependency-disabled", message: "依赖插件 dev.provider 已停用" } })));
  assert.equal(h.friend.hidden, true); assert.equal(h.friend.disabled, true); h.friend.dispatch();
  assert.equal(h.popups.length, 1); assert.equal(popup.closed, false); assert.equal(popup.draft, "依赖恢复前的草稿");
  assert.deepEqual(popup.messages.at(-1).message, { type: "plugins-changed", plugins: [{ id: PLUGIN, enabled: false }] });
  h.change(listing(plugin())); assert.equal(h.friend.hidden, false); h.friend.click();
  assert.equal(h.popups.length, 1); assert.equal(popup.draft, "依赖恢复前的草稿");
});
test("late discovery cannot undo newer plugin settings", async () => {
  for (const [latest, initial] of [[false, true], [true, false]]) {
    const json = deferred(), h = harness({ fetch: async () => ({ ok: true, json: () => json.promise }) });
    await settle(); h.change(listing(plugin({ enabled: latest })));
    json.resolve(listing(plugin({ enabled: initial }))); await settle();
    assert.equal(h.friend.hidden, !latest);
  }
});
test("discovery failures are closed by default and cannot override newer settings", async () => {
  for (const fetch of [async () => { throw new Error("synthetic failure"); },
    async () => ({ ok: false }), async () => ({ ok: true, json: async () => ({ plugins: null }) }),
    async () => ({ ok: true, json: async () => { throw new Error("bad json"); } })]) {
    const h = harness({ fetch }); await settle(); h.friend.dispatch(); assert.equal(h.popups.length, 0);
  }
  const pending = deferred(), h = harness({ fetch: () => pending.promise });
  h.change(listing(plugin())); pending.reject(new Error("late failure")); await settle();
  assert.equal(h.friend.hidden, false);
});
test("groups, unknown friend or missing identifiers cannot open external chat", async () => {
  const h = harness(); await settle();
  for (const [account, user] of [["synthetic-account", "synthetic-group@chatroom"], [null, "synthetic-friend"],
    ["synthetic-account", null], ["synthetic-account", "unknown"]]) {
    h.chatState.currentAccount = account; h.chatState.currentUser = user;
    h.window.ExternalImportHost.onSessionChanged(account, user);
    assert.equal(h.friend.hidden, true); assert.equal(h.friend.disabled, true); h.friend.dispatch();
  }
  assert.equal(h.popups.length, 0);
});
test("browser navigation trusts only exact registered same-origin popup and uses its original scope", async () => {
  const h = harness(); await settle(); h.friend.click(); const popup = h.popups[0];
  h.chatState.currentUser = "synthetic-second";
  const request = { type: "kyf-external:navigate", target: "chat", scope: { user: "wrong" } };
  h.message(popup, request, "https://example.invalid"); h.message({}, request);
  assert.deepEqual(h.views, []);
  popup.closed = true;
  h.message(popup, request);
  assert.deepEqual(h.views, ["chat"]); assert.deepEqual(h.selected, ["synthetic-friend"]);
  assert.equal(h.window.focused, true);
});
test("settings from child use shared settings route and model calls keep their own route", async () => {
  const h = harness({ native: true }), targets = []; await settle();
  h.window.openAppSettings = value => targets.push(plain(value));
  for (const target of ["plugins", "model-settings", "models", "untrusted"]) h.window.desktopHost.navigate({ target, scope: {} });
  assert.deepEqual(targets, [{ tab: "plugins" }, { tab: "models" }]);
  assert.equal(h.ids.get("btnModelTraces").clicks, 1);
  assert.equal(h.ids.get("btnSettings").clicks, 0);
});
test("theme and recovery reach open popups without changing their fixed scope", async () => {
  const h = harness(); await settle(); h.friend.click(); const popup = h.popups[0];
  h.message(popup, { type: "kyf-external:ready" });
  assert.deepEqual(popup.messages.at(-1), { origin: h.window.location.origin, message: { type: "kyf-external:theme", theme: "light" } });
  h.window.ExternalImportHost.theme("dark");
  assert.equal(popup.messages.at(-1).message.theme, "dark");
  h.dispatch("kyf-service-restored"); await settle();
  assert.ok(popup.messages.some(value => value.message.type === "service-restored"));
  assert.equal(popup.messages.some(value => value.message.type === "kyf-external:scope"), false);
  const before = popup.messages.length; popup.closed = true; h.window.ExternalImportHost.theme("light");
  assert.equal(popup.messages.length, before);
});
test("failed native launch or blocked browser launch reports failure without opening an embedded view", async () => {
  for (const options of [{ blocked: true }, { native: true, nativeOpen: () => Promise.resolve(false) },
    { native: true, nativeOpen: () => Promise.reject(new Error("synthetic failure")) }]) {
    const h = harness(options); await settle(); h.friend.click(); await settle();
    assert.equal(h.notices.length, 1); assert.deepEqual(h.views, []);
  }
});
test("closing main browser session closes its child windows", async () => {
  const h = harness(); await settle(); h.friend.click();
  const popup = h.popups[0]; h.dispatch("pagehide"); assert.equal(popup.closed, true);
});
