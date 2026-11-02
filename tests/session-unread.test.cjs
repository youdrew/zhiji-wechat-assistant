// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { makeDocument } = require("./helpers/plugin-settings-dom.cjs");
const { seed } = require("./helpers/view-state-harness.cjs");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first);
  return source.slice(first, last);
}
const code = section("function getWatermarks()", "const defaults =") +
  section("function renderSessions()", "async function preloadSessionWindows(") +
  section("function switchSession(", "let friendWindowRequest =");

function harness(storage = new Map()) {
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
  const context = vm.createContext({ document, element, AbortController,
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    byId: id => document.getElementById(id), time: String, avatar: () => element("div", "avatar-frame"),
    text() {}, status() {}, updateFriendButton() {}, cacheCurrentSession() {}, cancelHistoryRequest() {},
    resetHistorySearch() {}, updateHistoryNavigation() {}, loadMessages() {},
    sessionCacheKey: (account, user) => JSON.stringify([account, user]), pluginFriendName: user => user,
  });
  seed(context, { currentAccount: "synthetic-account", selectionLoadedAccount: "synthetic-account",
    currentUser: "other", messageSourceReady: true, selectedConversations: new Set(["friend", "other"]),
    sessions: new Map([
      ["friend", { username: "friend", name: "合成好友", time: 100, sortTimestamp: 1000, preview: "合成消息", unreadCount: 3, lastMessageFromSelf: false }],
      ["other", { username: "other", name: "另一位好友", time: 50, preview: "", unreadCount: 0 }],
    ]) });
  context.window.PluginSlots = {
    attentionFor: () => [{ label: "聊天助手", state: "done" }, { label: "聊天助手", state: "failed" }],
    onSessionChanged() {},
  };
  vm.runInContext(code, context);
  const row = user => document.getElementById("sessionList").children.find(node => node.dataset.id === user);
  const badge = (user = "friend") => row(user)?.querySelector(".session-unread-dot");
  const update = values => Object.assign(context.chatState.sessions.get("friend"), values);
  context.renderSessions();
  return { context, state: context.chatState, document, storage, row, badge, update };
}

test("clicking a friend clears chat unread immediately and survives switching, refresh and reload", async () => {
  const h = harness();
  assert.equal(h.badge().textContent, "3");
  await h.row("friend").click();
  assert.equal(h.badge(), null);
  await h.row("other").click();
  h.context.renderSessions();
  assert.equal(h.badge(), null);
  assert.equal(harness(h.storage).badge(), null);
});

test("a later incoming message restores the badge, and clicking again clears it", async () => {
  const h = harness();
  await h.row("friend").click();
  await h.row("other").click();
  h.update({ time: 101, unreadCount: 4 });
  h.context.renderSessions();
  assert.equal(h.badge().textContent, "1");
  await h.row("friend").click();
  assert.equal(h.badge(), null);
});

test("assistant answers and failures never add a chat-list dot", () => {
  const h = harness();
  h.update({ unreadCount: 0 });
  h.context.renderSessions();
  assert.equal(h.badge(), null);
  assert.equal(h.document.querySelector(".session-attention-dot"), null);
  assert.equal(h.row("friend").querySelector(".session-bottom").children.length, 1);
});

test("visible chat refresh advances read state before switching away, including history browsing", async () => {
  const h = harness();
  await h.row("friend").click();
  h.state.historyState = {};
  h.update({ time: 102, unreadCount: 5 });
  h.context.renderSessions();
  await h.row("other").click();
  assert.equal(h.badge(), null);
});

test("background windows and other pages do not mark the selected conversation read", async () => {
  for (const scope of ["hidden", "plugin:synthetic/page"]) {
    const h = harness();
    await h.row("friend").click();
    if (scope === "hidden") h.document.hidden = true;
    else h.state.view = scope;
    h.update({ time: 102, unreadCount: 5 });
    h.context.renderSessions();
    h.context.markSessionAsRead("friend");
    assert.equal(h.badge().textContent, "2", scope);
    h.state.currentUser = "other";
    h.state.view = "chat";
    h.document.hidden = false;
    h.context.renderSessions();
    assert.equal(h.badge().textContent, "2", scope);
  }
});

test("pin order and outgoing messages cannot revive read messages, but incoming unread growth still can", async () => {
  const h = harness();
  await h.row("friend").click();
  await h.row("other").click();
  h.update({ sortTimestamp: 999999, pinned: true });
  h.context.renderSessions();
  assert.equal(h.badge(), null);
  h.update({ time: 104, preview: "合成自己的消息", lastMessageFromSelf: true });
  h.context.renderSessions();
  assert.equal(h.badge(), null);
  // A reply from self must not hide an incoming message that arrived after the watermark.
  h.update({ unreadCount: 4 });
  h.context.renderSessions();
  assert.equal(h.badge().textContent, "1");
});

test("incoming messages are detected after WeChat resets its unread counter", async () => {
  const h = harness();
  await h.row("friend").click();
  await h.row("other").click();
  h.update({ time: 105, unreadCount: 1 });
  h.context.renderSessions();
  assert.equal(h.badge().textContent, "1");
});

test("legacy read watermarks ignore sorting changes and upgrade on the next click", async () => {
  const storage = new Map([["read-watermark:synthetic-account", JSON.stringify({
    friend: { time: 500, unreadCount: 3, preview: "合成消息" },
  })]]);
  const h = harness(storage);
  assert.equal(h.badge(), null);
  h.update({ unreadCount: 4 });
  h.context.renderSessions();
  assert.equal(h.badge().textContent, "1");
  await h.row("friend").click();
  assert.equal(JSON.parse(storage.get("read-watermark:synthetic-account")).friend.messageTime, 100);
});

test("read records are account scoped and tolerate malformed storage", async () => {
  const h = harness();
  await h.row("friend").click();
  h.state.currentAccount = "synthetic-second-account";
  h.state.currentUser = "other";
  h.context.renderSessions();
  assert.equal(h.badge().textContent, "3");
  for (const value of ["null", "[]", "broken-json"]) {
    h.storage.set("read-watermark:synthetic-second-account", value);
    await h.row("friend").click();
    assert.equal(h.badge(), null);
  }
});
