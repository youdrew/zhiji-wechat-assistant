// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Synthetic coverage for the “知己分析” entry: per-contact windows, group hiding and
// verified chat location requests coming back from an analysis window.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const html = readFileSync(path.join(__dirname, "../chatui/index.html"), "utf8");
const manifest = JSON.parse(readFileSync(path.join(__dirname, "../plugins/kyf.friend-analysis/plugin.json"), "utf8"));
const ENTRY = ["kyf.friend-analysis", "analysis"];
function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing section: ${start}`);
  return source.slice(first, last);
}
const code = section("let friendWindowRequest =", "\nfunction switchView(") +
  "globalThis.ui = { openFriendWindow, updateFriendButton, locateFromFriend };";

function element() {
  return { hidden: false, disabled: false, title: "", value: "", textContent: "", attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; }, focus() { this.focused = true; } };
}
function harness({ host = true, opened = true, confirm = true, enabled = true } = {}) {
  const nodes = new Map();
  const byId = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const button = Object.assign(element(), { textContent: "知己分析" });
  const env = { opens: [], toasts: [], switches: [], jumps: [], confirms: [], views: [], enabled, button };
  const state = {
    currentAccount: "synthetic-account", currentUser: "wxid_friend_a", messageSourceReady: true, view: "chat",
    sessions: new Map([["wxid_friend_a", { name: "朋友甲" }], ["wxid_friend_b", { name: "朋友乙" }],
      ["123@chatroom", { name: "合成群", isGroup: true }]]),
  };
  const desktopHost = host ? { openFriendWindow: async scope => { env.opens.push(scope); return opened; } } : {};
  const window = { desktopHost, confirm: message => { env.confirms.push(message); return confirm; },
    PluginSlots: { hasFriendEntry: (plugin, id) => env.enabled && plugin === ENTRY[0] && id === ENTRY[1] } };
  const context = vm.createContext({
    window, document: { querySelectorAll: selector => selector === ".plugin-header-btn" && env.enabled ? [button] : [] },
    byId, chatState: state, toast: message => env.toasts.push(message),
    switchView: view => { env.views.push(view); state.view = view; },
    switchSession: user => { env.switches.push(user); state.currentUser = user; },
    jumpToHistory: async (cursor, id) => env.jumps.push([cursor, id]),
  });
  vm.runInContext(code, context);
  return Object.assign(env, { state, byId, window, ui: context.ui });
}

test("the 知己分析 plugin owns its entry instead of a fixed main-window button", () => {
  assert.doesNotMatch(html, /id="btnToolbarPersona"/);
  assert.deepEqual(manifest.contributes.chatHeader, [{ id: "analysis", label: "知己分析", kind: "friend-window" }]);
  assert.doesNotMatch(html, />人物画像</);
});

test("opens a fixed window for the current contact without changing the chat view", async () => {
  const env = harness();
  await env.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(JSON.parse(JSON.stringify(env.opens)), [{ account: "synthetic-account", user: "wxid_friend_a", name: "朋友甲" }]);
  assert.equal(env.state.view, "chat");
  env.state.currentUser = "wxid_friend_b";
  await env.ui.openFriendWindow(...ENTRY);
  assert.equal(env.opens[1].user, "wxid_friend_b", "another contact gets its own window request");
});

test("group chats hide the entry and never open a window", async () => {
  const env = harness();
  env.state.currentUser = "123@chatroom";
  env.ui.updateFriendButton();
  assert.equal(env.button.hidden, true);
  assert.equal(env.button.disabled, true);
  await env.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(env.opens, []);
  env.state.currentUser = "wxid_friend_a";
  env.ui.updateFriendButton();
  assert.equal(env.button.hidden, false);
  assert.equal(env.button.disabled, false);
});

test("a disabled or unavailable plugin entry cannot open a window", async () => {
  const env = harness({ enabled: false });
  await env.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(env.opens, []);
  env.enabled = true;
  await env.ui.openFriendWindow("kyf.other", "analysis");
  await env.ui.openFriendWindow(ENTRY[0], "missing");
  assert.deepEqual(env.opens, []);
  await env.ui.openFriendWindow(...ENTRY);
  assert.equal(env.opens.length, 1);
  env.enabled = false;
  await env.ui.openFriendWindow(...ENTRY);
  assert.equal(env.opens.length, 1, "disabling blocks a previously available entry");
  delete env.window.PluginSlots;
  await env.ui.openFriendWindow(...ENTRY);
  assert.equal(env.opens.length, 1);
});

test("the plugin entry follows message-source readiness and selected-contact state", async () => {
  const env = harness();
  env.state.messageSourceReady = false;
  env.ui.updateFriendButton();
  assert.equal(env.button.disabled, true);
  await env.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(env.opens, []);
  env.state.messageSourceReady = true;
  env.state.currentUser = null;
  env.ui.updateFriendButton();
  assert.equal(env.button.disabled, true);
  await env.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(env.opens, []);
  env.state.currentUser = "wxid_friend_a";
  env.ui.updateFriendButton();
  assert.equal(env.button.disabled, false);
  assert.equal(env.button.title, "打开当前联系人的知己分析");
});

test("a browser without the desktop host explains instead of failing silently", async () => {
  const env = harness({ host: false });
  env.ui.updateFriendButton();
  assert.match(env.button.title, /桌面客户端/);
  await env.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(env.toasts, ["知己分析需要在桌面客户端中打开"]);
  const refused = harness({ opened: false });
  await refused.ui.openFriendWindow(...ENTRY);
  assert.deepEqual(refused.toasts, ["知己分析窗口未能打开"]);
});

test("locating evidence stays within the same account and lets the composer preserve drafts per friend", async () => {
  const target = { account: "synthetic-account", user: "wxid_friend_b", messageId: "m-1", cursor: "c-1" };
  const env = harness();
  await env.ui.locateFromFriend({ ...target, account: "other-account" });
  assert.deepEqual(env.switches, []);
  assert.match(env.toasts[0], /账号已变化/);
  await env.ui.locateFromFriend(target);
  assert.deepEqual(env.confirms, [], "no draft means no confirmation");
  assert.deepEqual(env.switches, ["wxid_friend_b"]);
  assert.deepEqual(env.jumps, [["c-1", "m-1"]]);
  const drafted = harness({ confirm: false });
  drafted.byId("chatInput").value = "还没发出去的话";
  await drafted.ui.locateFromFriend(target);
  assert.equal(drafted.confirms.length, 0);
  assert.deepEqual(drafted.switches, ["wxid_friend_b"]);
  assert.deepEqual(drafted.jumps, [["c-1", "m-1"]]);
  const same = harness();
  same.byId("chatInput").value = "草稿";
  await same.ui.locateFromFriend({ ...target, user: "wxid_friend_a" });
  assert.deepEqual(same.confirms, [], "staying in the same chat keeps the draft without asking");
  assert.deepEqual(same.jumps, [["c-1", "m-1"]]);
});
