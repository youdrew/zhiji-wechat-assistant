// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// A conversation popped out of the main window into its own chat window (2026-11-04).
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { makeDocument, settle } = require("./helpers/plugin-settings-dom.cjs");
const { seed } = require("./helpers/view-state-harness.cjs");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const settingsWindowSource = readFileSync(path.join(__dirname, "../chatui/settings-window.js"), "utf8");
const html = readFileSync(path.join(__dirname, "../chatui/index.html"), "utf8");
const css = readFileSync(path.join(__dirname, "../chatui/style.css"), "utf8");
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing app.js section: ${start}`);
  return source.slice(first, last);
}
const ACCOUNT = "synthetic-account", FRIEND = "synthetic-friend", OTHER = "synthetic-other";

// -- the window's own target -------------------------------------------------------------------------------------
function loadWindow(desktopHost, search) {
  const classes = new Set(), history = [];
  const window = { desktopHost, location: { search }, addEventListener() {},
    history: { replaceState: (...args) => history.push(args), pushState: (...args) => history.push(args) } };
  const document = { title: "知己微信助手", documentElement: { classList: { add: name => classes.add(name) } },
    getElementById: () => ({ setAttribute() {} }), body: { append() {} } };
  vm.runInNewContext(settingsWindowSource, { window, document, URLSearchParams });
  return { window, classes, history };
}

test("only a chat-window host reads account and user from the query, and never rewrites the URL", () => {
  const query = `?window=chat&account=${encodeURIComponent("账号 a&b")}&user=${encodeURIComponent("123@chatroom")}`;
  const chat = loadWindow({ isChatWindow: true }, query);
  assert.deepEqual({ ...chat.window.ChatWindow }, { account: "账号 a&b", user: "123@chatroom" });
  assert.equal(Object.isFrozen(chat.window.ChatWindow), true);
  assert.equal(chat.classes.has("chat-window"), true);
  assert.deepEqual(chat.history, []);
  assert.equal(chat.window.location.search, query);
  for (const host of [undefined, {}, { isChatWindow: false }, { isChatWindow: "true" }, { isChatWindow: true, isSettingsWindow: true }]) {
    const other = loadWindow(host, query);
    assert.equal(other.window.ChatWindow, undefined);
    assert.equal(other.classes.has("chat-window"), false);
  }
});

test("an unusable chat-window query still yields a chat window, with empty fields", () => {
  const parse = loadWindow(undefined, "").window.parseChatWindowTarget;
  for (const search of ["", "?account=a&user=b", "?window=settings&account=a&user=b", "?window=chat&account=&user=b",
    `?window=chat&account=${"a".repeat(129)}&user=b`, `?window=chat&account=a&user=${"b".repeat(257)}`]) {
    const target = parse({ isChatWindow: true }, search);
    assert.ok(target, search);
    assert.ok(!target.account || !target.user, search);
  }
  assert.deepEqual({ ...parse({ isChatWindow: true }, "?window=chat&account=a&user=b") }, { account: "a", user: "b" });
});

test("the page hides the nav rail, conversation list and startup gates only in chat-window mode", () => {
  for (const selector of [".nav-rail", "#sessionPane", "#databaseLoading", "#startupOverlay"])
    assert.match(css, new RegExp(`html\\.chat-window ${selector.replace(".", "\\.")}[,\\s{]`));
  assert.match(html, /<script src="settings-window\.js"><\/script>[\s\S]*<script src="app\.js"><\/script>/);
  assert.match(html, /id="btnOpenConversationWindow"[^>]*hidden[^>]*>在独立窗口中打开</);
});

// -- main window: double-click opens the conversation in its own window ------------------------------------------------
function sessionHarness({ desktopHost, chatWindow, ready = true } = {}) {
  const { document, add, Node } = makeDocument();
  document.hidden = false;
  Node.prototype.prepend = function (child) { child.remove(); child.parentElement = this; this.children.unshift(child); };
  Node.prototype.insertBefore = function (child, before) {
    child.remove(); child.parentElement = this;
    const index = this.children.indexOf(before);
    this.children.splice(index < 0 ? this.children.length : index, 0, child);
  };
  Object.defineProperty(Node.prototype, "lastElementChild", { get() { return this.children.at(-1); } });
  for (const id of ["sessionList", "searchInput", "btnAddConversation", "chatMessages", "chatTitle", "btnChatHistory"]) add(id);
  const element = (tag, className = "", text) => Object.assign(document.createElement(tag),
    { className, ...(text === undefined ? {} : { textContent: String(text) }) });
  const storage = new Map(), switched = [], toasts = [];
  const context = vm.createContext({ document, element, AbortController, Promise,
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    byId: id => document.getElementById(id), time: String, avatar: () => element("div", "avatar-frame"),
    toast: message => toasts.push(message), status() {}, text() {},
    switchSession: user => switched.push(user),
  });
  seed(context, { currentAccount: ACCOUNT, selectionLoadedAccount: ACCOUNT, currentUser: OTHER, messageSourceReady: ready,
    selectedConversations: new Set([FRIEND, OTHER]),
    sessions: new Map([
      [FRIEND, { username: FRIEND, name: "合成好友", time: 100, preview: "合成消息", unreadCount: 2 }],
      [OTHER, { username: OTHER, name: "另一位", time: 50, preview: "", unreadCount: 0 }],
    ]) });
  context.window.desktopHost = desktopHost;
  if (chatWindow) context.window.ChatWindow = chatWindow;
  vm.runInContext(section("function getWatermarks()", "const defaults =") +
    section("function renderSessions()", "async function preloadSessionWindows("), context);
  context.renderSessions();
  const row = user => document.getElementById("sessionList").children.find(node => node.dataset.id === user);
  return { context, document, storage, switched, toasts, row };
}

test("double-clicking a conversation opens it in a chat window for the current account", async () => {
  const opened = [];
  const h = sessionHarness({ desktopHost: { openChatWindow: async (account, user) => { opened.push([account, user]); return true; } } });
  await h.row(FRIEND).emit("click");
  await h.row(FRIEND).emit("click");
  await h.row(FRIEND).emit("dblclick");
  assert.deepEqual(opened, [[ACCOUNT, FRIEND]]);
  assert.deepEqual(h.switched, [FRIEND, FRIEND], "The main window still follows the clicks, as in WeChat");
  assert.deepEqual(h.toasts, []);
});

test("a refused or failed pop-out is reported, and nothing opens without the desktop bridge or a ready source", async () => {
  const refused = sessionHarness({ desktopHost: { openChatWindow: async () => false } });
  await refused.row(FRIEND).emit("dblclick"); await settle();
  assert.deepEqual(refused.toasts, ["聊天窗口未能打开"]);
  const failing = sessionHarness({ desktopHost: { openChatWindow: () => { throw new Error("ipc"); } } });
  await failing.row(FRIEND).emit("dblclick"); await settle();
  assert.deepEqual(failing.toasts, ["聊天窗口未能打开"]);
  for (const options of [{ desktopHost: undefined }, { desktopHost: {} }, { desktopHost: { isSettingsWindow: true, openChatWindow() { throw new Error("no"); } } },
    { desktopHost: { openChatWindow() { throw new Error("no"); } }, ready: false }]) {
    const h = sessionHarness(options);
    await h.row(FRIEND).emit("dblclick"); await settle();
    assert.deepEqual(h.toasts, []);
  }
});

test("read marks are rewritten only when they change, and another window's mark re-renders without writing back", () => {
  const h = sessionHarness({ desktopHost: {} });
  const key = `read-watermark:${ACCOUNT}`;
  h.context.markSessionAsRead(FRIEND);
  const first = h.storage.get(key);
  assert.equal(JSON.parse(first)[FRIEND].unreadCount, 2);
  h.context.markSessionAsRead(FRIEND);
  assert.equal(h.storage.get(key), first, "An unchanged mark is not rewritten");
  h.context.chatState.sessions.get(FRIEND).time = 120;
  h.context.markSessionAsRead(FRIEND);
  assert.equal(JSON.parse(h.storage.get(key))[FRIEND].messageTime, 120);

  // The storage listener: only the current account's marks, never a write in reply.
  const listeners = {}, renders = [];
  const listener = vm.createContext({ chatState: { currentAccount: ACCOUNT }, renderSessions: (...args) => renders.push(args),
    settingsState: { settings: {} }, applySettings() {}, window: { addEventListener: (name, callback) => { listeners[name] = callback; } } });
  vm.runInContext(section('window.addEventListener("storage",', "if (window.desktopHost?.isSettingsWindow) {"), listener);
  listeners.storage({ key, newValue: "{}" });
  listeners.storage({ key: "read-watermark:synthetic-other-account", newValue: "{}" });
  assert.equal(renders.length, 1);
  assert.equal(renders[0][0].markRead, false);
  // A chat window has no list to render: renderSessions only keeps its conversation read.
  const chat = sessionHarness({ desktopHost: {}, chatWindow: { account: ACCOUNT, user: OTHER } });
  assert.equal(chat.document.getElementById("sessionList").children.length, 0);
});

// -- chat window: fixed to its own conversation ---------------------------------------------------------------------
function chatHarness({ target = { account: ACCOUNT, user: FRIEND } } = {}) {
  const { document, add } = makeDocument();
  document.title = "知己微信助手";
  document.hidden = false;
  for (const id of ["chatMessages", "chatTitle", "btnChatHistory", "sessionList"]) add(id);
  const storage = new Map(), log = [], statuses = [];
  const composer = [];
  const context = vm.createContext({ document, AbortController, setTimeout, clearTimeout,
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    byId: id => document.getElementById(id),
    text: (id, value) => { document.getElementById(id).textContent = String(value ?? ""); },
    status: (container, message) => { statuses.push(message); container.textContent = message; },
    cacheCurrentSession() {}, cancelHistoryRequest() {}, resetHistorySearch() {}, updateHistoryNavigation() {},
    updateFriendButton: user => log.push(["friend-button", user]), renderMessages() {},
    loadMessages: () => log.push(["load-messages"]), markSessionAsRead: user => log.push(["read", user]),
    renderSessions: () => log.push(["render-sessions"]),
    sessionCacheKey: (account, user) => JSON.stringify([account, user]), pluginFriendName: user => user,
    accountUnavailable: true,
  });
  seed(context, { sessions: new Map(), sessionCache: new Map(), messages: [] });
  context.window.ChatWindow = target;
  context.window.ChatComposer = { setScope: (...args) => composer.push(args) };
  context.window.PluginSlots = { onSessionChanged: (...args) => log.push(["plugins", ...args]) };
  context.window.ExternalImportHost = { onSessionChanged() {} };
  context.window.ModelTraces = { setScope() {} };
  vm.runInContext(section("let chatWindowRetired =", "function messageNode(") +
    section("function switchSession(", "let friendWindowRequest =") +
    "globalThis.ui = { applyChatWindowSessions, invalidateChatWindow, switchSession, retire() { chatWindowRetired = true; } };", context);
  const sessions = (extra = []) => ({ account: ACCOUNT, messagesReady: true, self: { name: "我" },
    sessions: [{ username: FRIEND, name: "合成好友" }, { username: OTHER, name: "另一位" }, ...extra] });
  return { context, ui: context.ui, state: context.chatState, document, storage, log, statuses, composer, sessions };
}

test("the chat window opens its own conversation without remembering it as the main window's last chat", () => {
  const h = chatHarness();
  h.ui.applyChatWindowSessions(h.sessions());
  assert.equal(h.state.currentAccount, ACCOUNT);
  assert.equal(h.state.currentUser, FRIEND);
  assert.equal(h.document.title, "合成好友");
  assert.equal(h.document.getElementById("chatTitle").textContent, "合成好友");
  assert.equal(h.storage.size, 0, "No last-conversation entry is written");
  assert.deepEqual(h.composer.at(-1).slice(0, 2), [ACCOUNT, FRIEND]);
  assert.equal(h.composer.at(-1)[2].available, true);
  assert.ok(h.log.some(item => item[0] === "load-messages"));
  // A refresh keeps the conversation; nothing else can take its place.
  const generation = h.state.generation;
  h.ui.applyChatWindowSessions(h.sessions());
  assert.equal(h.state.generation, generation, "A directory refresh does not reopen the conversation");
  h.ui.switchSession(OTHER);
  assert.equal(h.state.currentUser, FRIEND);
});

test("an account switch disables the window inline, and the same account coming back restores it", () => {
  const h = chatHarness();
  h.ui.applyChatWindowSessions(h.sessions());
  h.ui.applyChatWindowSessions({ ...h.sessions(), account: "synthetic-account-2" });
  assert.equal(h.statuses.at(-1), "微信账号已切换，此窗口已失效");
  assert.equal(h.state.currentUser, null);
  assert.equal(h.state.currentAccount, null);
  assert.equal(h.state.sessions.size, 0, "Nothing of the other account is kept");
  assert.deepEqual(h.composer.at(-1), [null, null], "The composer is disabled");
  assert.equal(h.document.getElementById("btnChatHistory").disabled, true);
  assert.equal(h.storage.size, 0);
  h.ui.applyChatWindowSessions(h.sessions());
  assert.equal(h.state.currentUser, FRIEND);
  assert.deepEqual(h.composer.at(-1).slice(0, 2), [ACCOUNT, FRIEND]);
  h.ui.retire();
  h.ui.invalidateChatWindow("微信账号数据已清除，此窗口已失效");
  h.ui.applyChatWindowSessions(h.sessions());
  assert.equal(h.state.currentUser, null, "A retired window never shows its account again");
});

test("a missing conversation or unready source shows an inline state instead of the startup gate", () => {
  const missing = chatHarness({ target: { account: ACCOUNT, user: "synthetic-absent" } });
  missing.ui.applyChatWindowSessions(missing.sessions());
  assert.equal(missing.statuses.at(-1), "暂无和 TA 的聊天记录");
  assert.equal(missing.state.currentUser, null);
  const preparing = chatHarness();
  preparing.ui.applyChatWindowSessions({ ...preparing.sessions(), messagesReady: false });
  assert.equal(preparing.statuses.at(-1), "聊天记录尚未就绪，正在重试…");
  assert.equal(preparing.state.currentUser, null);
  const invalid = chatHarness({ target: { account: "", user: "" } });
  invalid.ui.applyChatWindowSessions(invalid.sessions());
  assert.equal(invalid.statuses.at(-1), "此聊天窗口已失效");
});

test("the chat-window branch skips selection, following, preloading and update checks", () => {
  const load = section("async function loadSessions(", "// -- chat window (added 2026-11-04)");
  const branch = load.indexOf("if (window.ChatWindow) { applyChatWindowSessions(data); return; }");
  assert.ok(branch > 0);
  // Only the settings window's own branch, which returns, precedes it.
  const settingsBranch = load.slice(0, branch);
  assert.match(settingsBranch, /if \(window\.desktopHost\?\.isSettingsWindow\) \{[\s\S]*return;\s+\}\s+$/);
  for (const main of ["trackNewConversations(", "preloadSessionWindows(", "last-conversation:", "announceFriendAccount(", "showStartup(\"sessions\""])
    assert.ok(load.indexOf(main) > branch, `${main} runs only after the chat-window branch`);
  assert.ok(load.indexOf("loadConversationSelection(data.account", branch) > branch);
  assert.match(section("function scheduleBackgroundUpdateChecks()", "async function refreshUpdateState()"),
    /if \(window\.desktopHost\?\.isSettingsWindow \|\| window\.ChatWindow\) return;/);
  assert.match(section("function resetAccountView(", "function accountUnavailableError("),
    /^function resetAccountView\([^)]*\) \{\s+if \(window\.ChatWindow\) \{ invalidateChatWindow\(message\); return; \}/);
  assert.match(source, /\} else if \(window\.ChatWindow\) \{[\s\S]{0,300}void loadSessions\(\);\s+\} else if \(!window\.desktopHost\?\.isSettingsWindow\) \{\s+void startInitialLoad\(\);/);
  assert.match(source, /let startupActive = !window\.desktopHost\?\.isSettingsWindow && !window\.ChatWindow;/);
  assert.match(source, /if \(!window\.ChatWindow\) window\.SessionResize\?\.init\(/);
  assert.match(source, /navigation: !window\.ChatWindow,/);
  // Drafts sync only between desktop windows, never in the settings window or a plain browser tab.
  assert.match(section("function composerDraftChannel()", "const composerDrafts ="),
    /if \(!window\.desktopHost \|\| window\.desktopHost\.isSettingsWindow \|\| typeof BroadcastChannel !== "function"\) return null;/);
});

test("clearing account data also matches an invalidated chat window's own account", async () => {
  const crypto = require("node:crypto");
  const accountId = crypto.createHash("sha256").update(ACCOUNT).digest("hex");
  const cleared = [];
  const run = async chatWindow => {
    const context = vm.createContext({ TextEncoder, crypto: globalThis.crypto,
      localStorage: { length: 0, key: () => null, removeItem() {} }, sessionStorage: undefined,
      window: { ChatWindow: chatWindow, ChatComposer: { accounts: () => [], clearAccount: account => cleared.push(account) } } });
    seed(context, { currentAccount: null, sessionCache: new Map() });
    vm.runInContext(section("async function clearAccountUiData(", "async function deleteManagedAccount("), context);
    return [...await context.clearAccountUiData(accountId)];
  };
  assert.deepEqual(await run(undefined), [], "Without a chat window nothing names the account");
  assert.deepEqual(await run({ account: ACCOUNT, user: FRIEND }), [ACCOUNT]);
  assert.deepEqual(cleared, [ACCOUNT], "The composer clears (and broadcasts) that account's drafts");
  assert.match(source, /if \(window\.ChatWindow && accounts\.has\(window\.ChatWindow\.account\)\) \{\s+chatWindowRetired = true;/);
});
