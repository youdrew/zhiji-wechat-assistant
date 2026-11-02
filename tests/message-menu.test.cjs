// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Added 2026-10-15: the right-click menu of a chat message copies it or asks the chat panel about it.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/message-menu.js"), "utf8");
const indexHtml = readFileSync(path.join(__dirname, "../chatui/index.html"), "utf8");
const appSource = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const settle = async () => { for (let index = 0; index < 6; index++) await new Promise(resolve => setImmediate(resolve)); };

function harness({ toolbar = [{ plugin: "kyf.advisor", label: "聊天助手" }], panel = true, selection = null, continueResult = true } = {}) {
  const document = { listeners: {}, activeElement: null, documentElement: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); } };
  class Node {
    constructor(id = "") {
      Object.assign(this, { id, hidden: false, textContent: "", style: {}, children: [], parentNode: null, listeners: {} });
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    emit(type, extra = {}) {
      const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
        stopPropagation() {}, ...extra };
      event.done = Promise.all((this.listeners[type] || []).map(fn => fn(event)));
      return event;
    }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    get isConnected() { return this.connected !== false; }
    querySelector(selector) { return selector === ".msg-bubble" ? this.children.find(child => child.id === "bubble") : null; }
    focus() { document.activeElement = this; }
    getBoundingClientRect() { return { width: 160, height: 80 }; }
  }
  const nodes = new Map(["messageContextMenu", "btnCopyMessage", "btnAskAboutMessage", "chatMessages"].map(id => [id, new Node(id)]));
  nodes.get("messageContextMenu").appendChild(nodes.get("btnCopyMessage"));
  nodes.get("messageContextMenu").appendChild(nodes.get("btnAskAboutMessage"));
  const calls = [], toasts = [];
  const window = {
    innerWidth: 800, innerHeight: 600, addEventListener() {},
    getSelection: () => selection, getComputedStyle: () => ({ zoom: "1" }),
    navigator: {},
    desktopHost: { copyDraft: async text => { calls.push(["copy", text]); return true; },
      ...(panel ? { openPluginWindow: async () => ({ opened: true }) } : {}) },
    PluginSlots: { state: { toolbar }, canContinue: () => toolbar.length > 0,
      continueFromMessage: async value => { calls.push(["ask", value]); return continueResult; } },
  };
  const state = { currentAccount: "account-a", currentUser: "friend-a", generation: 3 };
  vm.runInContext(source, vm.createContext({ window, document }));
  window.MessageMenu.init({ state, byId: id => nodes.get(id), toast: message => toasts.push(message) });
  const item = new Node("message"), bubble = item.appendChild(new Node("bubble"));
  return { window, document, nodes, calls, toasts, state, item, bubble, menu: nodes.get("messageContextMenu"),
    copy: nodes.get("btnCopyMessage"), ask: nodes.get("btnAskAboutMessage") };
}

test("the page has the menu, loads its script and attaches it to every message", () => {
  assert.match(indexHtml, /id="messageContextMenu" role="menu"[\s\S]*id="btnCopyMessage"[\s\S]*id="btnAskAboutMessage"/);
  assert.match(indexHtml, /<script src="message-menu\.js"><\/script>\s*\n?\s*<script src="session-resize\.js">/);
  assert.match(appSource, /window\.MessageMenu\?\.attach\(item, message\);/);
  assert.match(appSource, /window\.MessageMenu\?\.init\(\{ state: chatState, byId, toast \}\);/);
});

test("asking about a friend's message opens the panel with a quote and never sends it", async () => {
  const h = harness();
  h.window.MessageMenu.attach(h.item, { id: 42, side: "other", text: "周末有空吗", senderName: "" });
  const event = h.item.emit("contextmenu", { clientX: 10, clientY: 20 });
  assert.equal(event.defaultPrevented, true);
  assert.equal(h.menu.hidden, false);
  assert.deepEqual([h.copy.hidden, h.ask.hidden, h.ask.textContent], [false, false, "问聊天助手"]);
  await h.ask.emit("click").done; await settle();
  assert.equal(h.menu.hidden, true);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [["ask", { account: "account-a", user: "friend-a", messageId: "42",
    text: "周末有空吗", side: "other", sender: "" }]]);
});

test("copy takes the selected part of the message, or the whole text, through the desktop host", async () => {
  const h = harness();
  h.window.MessageMenu.attach(h.item, { id: 7, side: "self", text: "我明天下午有空，一起吃饭吧" });
  h.item.emit("contextmenu", { clientX: 1, clientY: 1 });
  await h.copy.emit("click").done; await settle();
  assert.deepEqual(h.calls, [["copy", "我明天下午有空，一起吃饭吧"]]);
  assert.deepEqual(h.toasts, ["已复制"]);
  const picked = harness({ selection: { isCollapsed: false, toString: () => " 一起吃饭 ", anchorNode: null, focusNode: null } });
  picked.window.MessageMenu.attach(picked.item, { id: 7, side: "self", text: "我明天下午有空，一起吃饭吧" });
  picked.window.getSelection = () => ({ isCollapsed: false, toString: () => " 一起吃饭 ", anchorNode: picked.bubble, focusNode: picked.bubble });
  picked.item.emit("contextmenu", { clientX: 1, clientY: 1 });
  await picked.ask.emit("click").done; await settle();
  assert.equal(picked.calls[0][1].text, "一起吃饭");
  assert.equal(picked.calls[0][1].side, "self");
});

test("without a chat panel only copy is offered; a media message without text offers only asking", () => {
  const noPanel = harness({ toolbar: [] });
  noPanel.window.MessageMenu.attach(noPanel.item, { id: 1, side: "other", text: "你好" });
  noPanel.item.emit("contextmenu", { clientX: 1, clientY: 1 });
  assert.deepEqual([noPanel.copy.hidden, noPanel.ask.hidden], [false, true]);
  const media = harness();
  media.window.MessageMenu.attach(media.item, { id: 2, side: "other", kind: "image", text: "" });
  media.item.emit("contextmenu", { clientX: 1, clientY: 1 });
  assert.deepEqual([media.copy.hidden, media.ask.hidden], [true, false]);
  // Nothing to offer: the default menu is left alone.
  const empty = harness({ toolbar: [] });
  empty.window.MessageMenu.attach(empty.item, { id: 3, side: "other", text: "" });
  assert.equal(empty.item.emit("contextmenu", { clientX: 1, clientY: 1 }).defaultPrevented, false);
  assert.equal(empty.menu.hidden, true);
});

test("a menu opened for another chat does nothing after the chat changed, and a refusal is reported", async () => {
  const h = harness();
  h.window.MessageMenu.attach(h.item, { id: 5, side: "other", text: "收到" });
  h.item.emit("contextmenu", { clientX: 1, clientY: 1 });
  h.state.currentUser = "friend-b";
  await h.ask.emit("click").done; await settle();
  assert.deepEqual(h.calls, []);
  const refused = harness({ continueResult: false });
  refused.window.MessageMenu.attach(refused.item, { id: 6, side: "other", text: "好的", senderName: "小李" });
  refused.item.emit("contextmenu", { clientX: 1, clientY: 1 });
  await refused.ask.emit("click").done; await settle();
  assert.equal(refused.calls[0][1].sender, "小李");
  assert.deepEqual(refused.toasts, ["暂无法带入聊天助手"]);
});

test("selection outside the bubble never substitutes a sender name or unrelated text", async () => {
  const h = harness(); h.window.MessageMenu.attach(h.item, { id: 9, side: "other", text: "完整消息" });
  h.window.getSelection = () => ({ isCollapsed: false, toString: () => "发送人名字", anchorNode: h.item, focusNode: h.item });
  h.item.emit("contextmenu", { clientX: 1, clientY: 1 }); await h.copy.emit("click").done; await settle();
  assert.deepEqual(h.calls, [["copy", "完整消息"]]);
});

test("removing the original message or changing account invalidates an open message menu", async () => {
  for (const mutate of [h => { h.item.connected = false; }, h => { h.state.currentAccount = "account-b"; }]) {
    const h = harness(); h.window.MessageMenu.attach(h.item, { id: 10, side: "other", text: "旧账号消息" });
    h.item.emit("contextmenu", { clientX: 1, clientY: 1 }); mutate(h);
    await h.copy.emit("click").done; await settle(); assert.deepEqual(h.calls, []);
  }
});
