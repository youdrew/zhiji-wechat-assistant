"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Exercise production controllers with synthetic directories; never open a live account.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `production section: ${start}`);
  return source.slice(first, last);
}
const selectionCode = section("async function loadConversationSelection(", "function clearUnselectedConversation(");
const syncCode = section("window.SettingsSync?.listen(", 'window.addEventListener("storage",');
const storageCode = section('window.addEventListener("storage",', "if (window.desktopHost?.isSettingsWindow) {");
function element() {
  const classes = new Set();
  return { hidden: false, open: false, value: "", style: { setProperty(name, value) { this[name] = value; } },
    classList: { contains: name => classes.has(name), add: name => classes.add(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); } },
    close(value) { this.open = false; this.returnValue = value; } };
}
function nodes() {
  const items = new Map();
  return id => { if (!items.has(id)) items.set(id, element()); return items.get(id); };
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function directoryHarness({ oldAccount = null, messagesReady = true } = {}) {
  const requests = [], effects = [], status = [], scopes = [], writes = [];
  const byId = nodes();
  const chatState = { currentAccount: oldAccount, currentUser: null, sessionRequest: 0,
    sessionLoading: false, sessionRefreshQueued: false, selectionLoadedAccount: oldAccount,
    conversationSelectionEpoch: 4, conversationSelectionBusy: !!oldAccount,
    sessions: new Map(), selectedConversations: new Set(), messageSourceReady: false,
    generation: 0, view: "chat" };
  const window = { desktopHost: { isSettingsWindow: true },
    ModelTraces: { setScope: (...scope) => scopes.push(scope) },
    MediaSettings: { refresh: () => effects.push("media") } };
  const context = vm.createContext({ chatState, window, byId, document: { hidden: false },
    accountClearedExiting: false, accountUnavailable: false, startupActive: false,
    api: async route => {
      requests.push(route);
      if (route === "/api/sessions") return { account: "synthetic-account", messagesReady,
        self: { name: "合成账号" }, sessions: [{ username: "synthetic-friend", name: "合成好友", unreadCount: 3 }] };
      if (route === "/api/conversation-selection") return { account: "synthetic-account",
        initialized: true, selectedSessions: ["synthetic-friend"] };
      throw new Error(`unexpected route: ${route}`);
    },
    accountChangedError: () => false, accountUnavailableError: () => false,
    showStartup() {}, accountCheckStatus: message => status.push(message),
    renderConversationManager: () => effects.push("directory"),
    preloadSessionWindows: () => effects.push("preload"), renderSessions: () => effects.push("chat-render"),
    announceFriendAccount: () => effects.push("account-broadcast"),
    localStorage: { getItem: () => null, setItem: (...entry) => writes.push(entry) },
  });
  vm.runInContext(selectionCode +
    section("async function loadSessions(", "function messageNode(") +
    section("function getWatermarks()", "const defaults =") +
    section("async function loadMessages(", "function switchSession(") +
    section("function switchSession(", "let friendWindowRequest ="), context);
  return { context, chatState, byId, requests, effects, status, scopes, writes };
}

test("settings reads only the directory and selection, without opening or marking any conversation", async () => {
  for (const messagesReady of [true, false]) {
    const h = directoryHarness({ messagesReady });
    await h.context.loadSessions();
    assert.deepEqual(h.requests, ["/api/sessions", "/api/conversation-selection"]);
    assert.equal(h.chatState.currentAccount, "synthetic-account");
    assert.deepEqual([...h.chatState.selectedConversations], ["synthetic-friend"]);
    assert.equal(h.chatState.sessions.get("synthetic-friend").name, "合成好友");
    assert.equal(h.chatState.messageSourceReady, messagesReady);
    assert.equal(h.chatState.sessionLoading, false);
    assert.deepEqual(h.effects, ["directory", "media"]);
    assert.deepEqual(h.scopes, [["synthetic-account", null, "settings"]]);
    assert.equal(h.status.at(-1), messagesReady ? "微信账号已就绪" : "聊天记录正在准备中…");
    h.context.markSessionAsRead("synthetic-friend");
    h.context.switchSession("synthetic-friend");
    await h.context.loadMessages();
    assert.equal(h.chatState.currentUser, null);
    assert.deepEqual(h.writes, [], "a settings interaction cannot update read marks or the last chat");
    assert.deepEqual(h.requests, ["/api/sessions", "/api/conversation-selection"]);
  }
});

test("settings account changes cancel the old account confirmation and unblock the new directory", async () => {
  const h = directoryHarness({ oldAccount: "synthetic-old-account" });
  h.chatState.selectedConversations.add("old-friend");
  const dialog = h.byId("conversationActionConfirm"); dialog.open = true;
  await h.context.loadSessions();
  assert.equal(h.chatState.conversationSelectionEpoch, 5, "old account mutations lose their epoch");
  assert.equal(h.chatState.conversationSelectionBusy, false);
  assert.equal(h.chatState.selectionLoadedAccount, "synthetic-account");
  assert.deepEqual([...h.chatState.selectedConversations], ["synthetic-friend"]);
  assert.equal(dialog.open, false);
  assert.equal(dialog.returnValue, "cancel");
  assert.deepEqual(h.effects, ["directory", "media"]);
});

test("a cross-window selection change fences a pending read and invalidates the main sidebar cache", async () => {
  const pending = deferred(), refreshes = [];
  const chatState = { sessionRequest: 7, selectionLoadedAccount: null, sessionSignature: "old-directory",
    selectedConversations: new Set(["current-friend"]) };
  let receive;
  const context = vm.createContext({ chatState, accountClearedExiting: false,
    window: { desktopHost: { isSettingsWindow: false }, SettingsSync: { listen(callback) { receive = callback; } } },
    api: () => pending.promise, byId: () => ({ hidden: true }),
    loadSessions: () => refreshes.push({ request: chatState.sessionRequest,
      selection: chatState.selectionLoadedAccount, signature: chatState.sessionSignature }),
  });
  vm.runInContext(selectionCode + syncCode, context);
  const read = context.loadConversationSelection("synthetic-account", 7);
  chatState.selectionLoadedAccount = "synthetic-account";
  receive("/api/conversation-selection");
  assert.deepEqual(refreshes, [{ request: 8, selection: null, signature: null }]);
  pending.resolve({ account: "synthetic-account", initialized: true, selectedSessions: ["stale-friend"] });
  await read;
  assert.deepEqual([...chatState.selectedConversations], ["current-friend"], "the stale response cannot undo the newer selection");
  assert.equal(chatState.selectionLoadedAccount, null);
});

test("saved theme and zoom apply to another window while invalid storage values are ignored", () => {
  const byId = nodes(), themes = [], listeners = {};
  const document = { body: element(), documentElement: element() };
  const settingsState = { settings: { theme: "dark", zoom: "1.0" } };
  const context = vm.createContext({ byId, document, settingsState,
    window: { desktopHost: { setTheme: theme => themes.push(theme) },
      addEventListener: (name, callback) => { listeners[name] = callback; } },
  });
  vm.runInContext(section("function applySettings()", "function dataRootControlsBusy(") + storageCode, context);
  listeners.storage({ key: "real-ui-settings-1", newValue: JSON.stringify({ theme: "light", zoom: "1.25" }) });
  assert.equal(document.body.classList.contains("theme-light"), true);
  assert.equal(document.documentElement.style.zoom, "1.25");
  assert.equal(document.documentElement.style["--zoom-inverse"], "0.8");
  assert.equal(byId("selectThemeMode").value, "light");
  assert.equal(byId("selectZoomLevel").value, "1.25");
  assert.deepEqual(themes, ["light"]);
  for (const event of [
    { key: "unrelated-setting", newValue: '{"theme":"dark"}' },
    { key: "real-ui-settings-1", newValue: "invalid JSON" },
    { key: "real-ui-settings-1", newValue: '{"theme":"invalid","zoom":"99"}' },
  ]) listeners.storage(event);
  assert.deepEqual(settingsState.settings, { theme: "light", zoom: "1.25" });
  assert.equal(document.documentElement.style.zoom, "1.25");
});

test("main settings requests open the host window and report failures without exposing the overlay", async () => {
  const byId = nodes(), opened = [], notices = [], refreshes = [];
  let outcome = true;
  const context = vm.createContext({ byId, toast: message => notices.push(message),
    window: { desktopHost: { isSettingsWindow: false, openSettingsWindow: async target => {
      opened.push(target); if (outcome instanceof Error) throw outcome; return outcome;
    } } }, loadModelSource: () => refreshes.push("model"), loadDataRoot: () => refreshes.push("data-root"),
  });
  vm.runInContext(section("function showSettingsPanel()", "function applySettingsTarget("), context);
  const target = { tab: "general", section: "conversations" };
  await context.openAppSettings(target);
  assert.deepEqual(opened, [target]);
  assert.equal(byId("settingsModal").classList.contains("show"), false);
  assert.deepEqual(notices, []);
  for (outcome of [false, new Error("synthetic window failure")]) await context.openAppSettings(target);
  assert.deepEqual(notices, ["设置窗口未能打开，请重试", "设置窗口未能打开，请重试"]);
  assert.equal(byId("settingsModal").classList.contains("show"), false);
  assert.deepEqual(refreshes, []);
});
