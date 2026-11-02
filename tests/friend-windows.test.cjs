// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic Electron fakes: multi-contact 知己分析 windows, draft flush on close and locate scope.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createFriendWindows, friendScope } = require("../scripts/friend-windows.cjs");

const CLIENT = "http://127.0.0.1:34567/";
const A = { account: "synthetic-account", user: "wxid_friend_a", name: "朋友甲" };
const B = { account: "synthetic-account", user: "wxid_friend_b", name: "朋友乙" };

function harness({ flush = true, dialogResponse = 0, saved = null } = {}) {
  const made = [];
  const handlers = new Map(), listeners = new Map();
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.bounds = { x: options.x ?? 0, y: options.y ?? 0, width: options.width ?? 1180, height: options.height ?? 780 };
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = handler => { this.popups = handler; };
      this.webContents.isCrashed = () => false;
      this.webContents.mainFrame = { url: "" };
      this.sent = [];
      this.webContents.send = (channel, payload) => {
        this.sent.push([channel, payload]);
        if (channel === "friend:before-close" && flush !== "never")
          setImmediate(() => listeners.get("friend:close-ready")?.({ sender: this.webContents,
            senderFrame: this.webContents.mainFrame }, payload, flush));
      };
      made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    isMaximized() { return false; }
    getBounds() { return { ...this.bounds }; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    destroy() { this.destroyed = true; this.emit("closed"); }
    close() {
      const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      this.emit("close", event);
      if (!event.defaultPrevented) this.destroy();
    }
    loadURL(url) { this.url = url; this.webContents.mainFrame.url = url; return Promise.resolve(); }
    once(name, listener) { return super.once(name, listener); }
  }
  const main = new Window({ x: 100, y: 80 });
  main.webContents.session = { synthetic: true };
  main.webContents.mainFrame.url = CLIENT;
  const ipcMain = {
    handle: (channel, handler) => handlers.set(channel, handler),
    on: (channel, handler) => listeners.set(channel, handler),
    removeHandler: channel => handlers.delete(channel),
    removeAllListeners: channel => listeners.delete(channel),
  };
  const dialogs = [];
  const dialog = { showMessageBox: async (_window, options) => { dialogs.push(options); return { response: dialogResponse }; } };
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "friend-windows-"));
  if (saved) fs.writeFileSync(path.join(userData, "friend-windows.json"), JSON.stringify(saved));
  const trusted = event => event.sender === main.webContents;
  const manager = createFriendWindows({ BrowserWindow: Window, mainWindow: main, clientUrl: CLIENT, ipcMain, dialog,
    userDataPath: userData, trustedMainFrame: trusted, screen: {
      getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }),
      getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }),
      getAllDisplays: () => [{ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }],
    } });
  return { main, manager, made, handlers, listeners, dialogs, userData };
}
const settle = () => new Promise(resolve => setTimeout(resolve, 20));

test("each contact gets its own window; reopening the same contact only focuses it", () => {
  const h = harness();
  assert.equal(h.manager.open(A), true);
  assert.equal(h.manager.open(B), true);
  assert.equal(h.made.length, 3);
  const [first, second] = [h.made[1], h.made[2]];
  assert.notEqual(first, second);
  assert.equal(new URLSearchParams(new URL(first.url).hash.slice(1)).get("user"), A.user);
  assert.equal(new URL(first.url).pathname, "/friend.html");
  assert.equal(first.options.webPreferences.sandbox, true);
  assert.equal(first.options.webPreferences.nodeIntegration, false);
  assert.match(first.options.webPreferences.preload, /friend-preload\.cjs$/);
  assert.equal(first.options.webPreferences.session, h.main.webContents.session);
  assert.equal(first.options.title, "朋友甲 · 知己分析");
  assert.ok(second.options.x > first.options.x || second.options.y > first.options.y, "new windows cascade");
  first.minimized = true;
  assert.equal(h.manager.open(A), true);
  assert.equal(h.made.length, 3);
  assert.equal(first.minimized, false);
  assert.deepEqual(first.sent.at(-1), ["friend:host-event", { type: "focus" }]);
  assert.equal(h.manager.open({ ...A, name: "同名" }), true, "same identity, different display name reuses");
  assert.equal(h.made.length, 3);
  assert.equal(h.manager.open({ ...A, user: "wxid_friend_c", name: "朋友甲" }), true, "same name, other identity");
  assert.equal(h.made.length, 4);
});

test("group chats and malformed scopes never open", () => {
  const h = harness();
  for (const value of [null, {}, { ...A, user: "123@chatroom" }, { ...A, account: "" }, { ...A, user: "a\nb" },
    { ...A, name: "x".repeat(129) }]) assert.equal(h.manager.open(value), false);
  assert.equal(h.made.length, 1);
  assert.equal(friendScope({ account: "a", user: "b" }).name, "b");
});

test("closing flushes drafts first and keeps the window when saving fails", async () => {
  const ok = harness();
  ok.manager.open(A);
  const win = ok.made[1];
  win.close();
  assert.equal(win.isDestroyed(), false, "waits for the renderer to persist drafts");
  await settle();
  assert.equal(win.isDestroyed(), true);
  assert.deepEqual(ok.dialogs, []);
  const failing = harness({ flush: false, dialogResponse: 0 });
  failing.manager.open(A);
  failing.made[1].close();
  await settle();
  assert.equal(failing.made[1].isDestroyed(), false);
  assert.equal(failing.dialogs.length, 1);
  assert.match(failing.dialogs[0].message, /暂存失败/);
});

test("app exit flushes every window together and reports failures once", async () => {
  const h = harness();
  h.manager.open(A);
  h.manager.open(B);
  assert.deepEqual(await h.manager.flushAll(), { ok: true, count: 2 });
  const failing = harness({ flush: false });
  failing.manager.open(A);
  assert.deepEqual(await failing.manager.flushAll(), { ok: false, count: 1 });
  failing.manager.destroyAll();
  assert.equal(failing.manager.count(), 0);
});

test("only the main frame opens windows and a window can locate only its own contact", async () => {
  const h = harness();
  const open = h.handlers.get("friend:open");
  assert.equal(open({ sender: {} }, A, true), false);
  assert.equal(open({ sender: h.main.webContents }, A, false), false);
  assert.equal(open({ sender: h.main.webContents }, A, true), true);
  const win = h.made[1];
  const locate = h.handlers.get("friend:locate");
  const event = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
  const target = { account: A.account, user: A.user, messageId: "m-1", cursor: "c-1" };
  assert.equal(locate(event, { ...target, user: B.user }, true), false);
  assert.equal(locate(event, target, false), false);
  assert.equal(locate({ sender: {}, senderFrame: {} }, target, true), false);
  assert.equal(locate(event, target, true), true);
  assert.deepEqual(h.main.sent.at(-1), ["friend:locate", target]);
});

test("window bounds are remembered per contact and restored only when still visible", async () => {
  const h = harness();
  h.manager.open(A);
  const win = h.made[1];
  win.bounds = { x: 300, y: 200, width: 900, height: 700 };
  win.emit("move");
  await new Promise(resolve => setTimeout(resolve, 350));
  const saved = JSON.parse(fs.readFileSync(path.join(h.userData, "friend-windows.json"), "utf8"));
  assert.deepEqual(Object.values(saved)[0], { x: 300, y: 200, width: 900, height: 700 });
  win.destroy();
  h.manager.open(A);
  assert.equal(h.made[2].options.x, 300);
  const again = harness({ saved: { [JSON.stringify([B.account, B.user])]: { x: 5000, y: 5000, width: 900, height: 700 } } });
  again.manager.open(B);
  assert.ok(again.made[1].options.x < 1920, "an off-screen saved position falls back to the visible work area");
});
