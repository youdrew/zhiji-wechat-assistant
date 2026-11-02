"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic windows and IPC only; no running service or personal data.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const { createSelfAnalysisWindow, selfContext } = require("../scripts/self-analysis-window.cjs");
const CLIENT = "http://127.0.0.1:34567/";
const A = { account: "synthetic-a", theme: "light" }, B = { account: "synthetic-b", theme: "dark" };

function fixture() {
  const made = [], handlers = new Map(), ipcMain = new EventEmitter(), copies = [];
  ipcMain.handle = (name, handler) => handlers.set(name, handler);
  ipcMain.removeHandler = name => handlers.delete(name);
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.sent = [];
      this.webContents = new EventEmitter(); this.webContents.mainFrame = { url: "" };
      this.webContents.session = {}; this.webContents.isFocused = () => true;
      this.webContents.send = (...args) => this.sent.push(args);
      this.webContents.setWindowOpenHandler = callback => { this.popup = callback; }; made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    restore() { this.minimized = false; }
    getBounds() { return { x: 1900, y: 1000, width: 900, height: 800 }; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    destroy() { this.destroyed = true; this.emit("closed"); }
    loadURL(url) { this.webContents.mainFrame.url = url; return Promise.resolve(); }
  }
  const main = new Window({}); main.webContents.mainFrame.url = CLIENT;
  const event = win => ({ sender: win.webContents, senderFrame: win.webContents.mainFrame });
  const manager = createSelfAnalysisWindow({ BrowserWindow: Window, mainWindow: main, clientUrl: CLIENT, ipcMain,
    trustedMainFrame: value => value.sender === main.webContents && value.senderFrame === main.webContents.mainFrame && value.senderFrame.url === CLIENT,
    clipboard: { writeText: value => copies.push(value) },
    screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 800, height: 600 } }) },
  });
  const child = () => made.at(-1);
  const scope = () => { const value = handlers.get("self-analysis:context")(event(child())); return { account: value.context.account, epoch: value.epoch }; };
  const call = (method, params = {}, bound = scope()) => handlers.get("self-analysis:host-call")(event(child()), method, params, bound);
  const reply = value => ipcMain.emit("self-analysis:host-result", event(main), main.sent.at(-1)[1].id, value);
  return { manager, made, handlers, ipcMain, main, child, event, scope, call, reply, copies };
}

test("one independent native window is reused across contacts and accounts with a fixed sandbox", t => {
  const f = fixture(); t.after(() => f.manager.close());
  assert.equal(f.manager.open(A), true); const child = f.child(); child.emit("ready-to-show");
  assert.equal(child.shown, true); assert.equal(child.options.title, "自我剖析 · 知己微信助手");
  assert.equal(child.options.parent, undefined); assert.equal(child.options.modal, undefined);
  assert.deepEqual([child.options.x, child.options.y, child.options.width, child.options.height], [0, 0, 800, 600]);
  assert.equal(child.options.webPreferences.session, f.main.webContents.session);
  assert.equal(child.options.webPreferences.sandbox, true); assert.equal(child.options.webPreferences.contextIsolation, true);
  assert.equal(child.options.webPreferences.nodeIntegration, false); assert.equal(child.options.webPreferences.devTools, false);
  assert.match(child.options.webPreferences.preload, /self-analysis-preload\.cjs$/);
  assert.equal(child.webContents.mainFrame.url, CLIENT + "self-analysis.html");
  child.minimized = true;
  assert.equal(f.manager.open({ ...B, user: "ignored-contact", url: "file:///ignored" }), true);
  assert.equal(f.made.length, 2); assert.equal(child.minimized, false);
  assert.equal(f.scope().account, B.account);
  assert.deepEqual(selfContext(A), { ...A, user: "", surface: "self-analysis-window" });
  for (const value of [null, [], {}, { account: "line\nbreak" }, { account: "x".repeat(257) }]) assert.equal(f.manager.open(value), false);
});

test("native actions accept exact top frames, bounded methods and activated copy only", async t => {
  const f = fixture(); t.after(() => f.manager.close()); const open = f.handlers.get("self-analysis:open");
  assert.equal(open(f.event(f.main), A, false), false);
  assert.equal(open({ ...f.event(f.main), senderFrame: { url: CLIENT } }, A, true), false);
  assert.equal(open(f.event(f.main), A, true), true);
  const child = f.child(), event = f.event(child), bound = f.scope();
  const handler = f.handlers.get("self-analysis:host-call");
  assert.equal((await handler({ ...event, senderFrame: { url: CLIENT + "self-analysis.html" } }, "model.status", {}, bound)).error.code, "context-changed");
  assert.equal((await f.call("file.pick")).error.code, "context-changed");
  assert.equal((await f.call("self-profile.save", { account: B.account })).error.code, "context-changed");
  const copy = f.handlers.get("self-analysis:copy");
  assert.equal(copy(event, "合成文字", bound, false), false);
  assert.equal(copy(event, "合成文字", bound, true), true);
  assert.deepEqual(f.copies, ["合成文字"]);
  child.webContents.mainFrame.url += "?spoof=1";
  assert.equal(f.handlers.get("self-analysis:context")(event), null);
  assert.equal(copy(event, "not-copied", bound, true), false);
});

test("account switches, clears and disabling settle old pending calls and reject late results", async t => {
  const f = fixture(); t.after(() => f.manager.close()); f.manager.open(A);
  const oldScope = f.scope(), oldCall = f.call("self-profile.read", { account: A.account });
  f.manager.sync({ context: B });
  assert.equal((await oldCall).error.code, "context-changed");
  assert.equal((await f.call("self-profile.save", { account: A.account }, oldScope)).error.code, "context-changed");
  f.reply({ result: { style: "stale" } });
  f.manager.sync({ event: { type: "plugins-changed", enabled: true } });
  const good = f.call("model.status"); f.reply({ result: { configured: true } });
  assert.equal((await good).result.configured, true);
  const pending = f.call("self-profile.read", { account: B.account });
  f.manager.sync({ event: { type: "plugins-changed", enabled: false } });
  assert.equal((await pending).error.code, "context-changed");
  assert.equal((await f.call("self-profile.read", { account: B.account })).error.code, "context-changed");
  f.manager.sync({ event: { type: "account-cleared", account: B.account } });
  assert.equal(f.scope().account, "");
  f.manager.sync({ context: B }); assert.equal(f.scope().account, "", "late context cannot revive a cleared account");
});

test("theme and unchanged plugin listings preserve epoch; empty account retains navigation and model status", async t => {
  const f = fixture(); t.after(() => f.manager.close()); f.manager.open({ account: "" });
  const status = f.call("model.status"); f.reply({ result: { configured: false } });
  assert.equal((await status).result.configured, false);
  assert.equal((await f.call("self-profile.read")).error.code, "context-changed");
  f.manager.open(A); f.manager.sync({ event: { type: "plugins-changed", enabled: true } });
  const version = f.scope().epoch;
  f.manager.sync({ event: { type: "plugins-changed", enabled: true } });
  f.manager.sync({ context: { ...A, theme: "dark" } }); assert.equal(f.scope().epoch, version);
  const focus = f.call("view.focusChat"); f.reply({ result: { ok: true } });
  await focus; assert.equal(f.main.focused, true);
});

test("close/crash/reload cancel pending IPC and remove handlers without native navigation escape", async t => {
  const f = fixture(); t.after(() => f.manager.close()); f.manager.open(A); const child = f.child();
  for (const name of ["will-navigate", "will-redirect", "will-attach-webview", "page-title-updated"]) {
    let blocked = false; child.webContents.emit(name, { preventDefault() { blocked = true; } }); assert.equal(blocked, true);
  }
  for (const url of [CLIENT + "plugin-pages/other/ui/index.html", "https://example.test/", CLIENT + "self-analysis.html"]) {
    let blocked = false; child.webContents.emit("will-frame-navigate", { isMainFrame: false, url, preventDefault() { blocked = true; } });
    assert.equal(blocked, true);
  }
  let blocked = false; child.webContents.emit("will-frame-navigate", { isMainFrame: false,
    url: CLIENT + "plugin-pages/kyf.self-analysis/ui/index.html", preventDefault() { blocked = true; } }); assert.equal(blocked, false);
  assert.deepEqual(child.popup(), { action: "deny" });
  const pending = f.call("model.status");
  f.main.webContents.emit("did-start-navigation", {}, CLIENT, false, true);
  assert.equal(child.isDestroyed(), true); assert.equal((await pending).error.code, "context-changed");
  f.manager.open(B); const second = f.child(); f.main.destroy();
  assert.equal(second.isDestroyed(), true); assert.equal(f.handlers.size, 0);
  assert.equal(f.ipcMain.listenerCount("self-analysis:sync"), 0);
  assert.equal(f.ipcMain.listenerCount("self-analysis:host-result"), 0);
});

test("dedicated preload exposes only top-frame scoped capabilities and gesture-gated copy", async () => {
  const code = fs.readFileSync(path.join(__dirname, "../scripts/self-analysis-preload.cjs"), "utf8");
  for (const top of [true, false]) {
    let bridge; const calls = [], window = {}; window.top = top ? window : {};
    const navigator = { userActivation: { isActive: true } };
    vm.runInNewContext(code, { window, navigator, require: () => ({
      contextBridge: { exposeInMainWorld(name, value) { assert.equal(name, "selfAnalysisHost"); bridge = value; } },
      ipcRenderer: { invoke: async (...args) => { calls.push(args); return true; }, on() {}, removeListener() {} },
    }) });
    await bridge.hostCall("file.pick", {}, {}); assert.equal(calls.length, 0);
    await bridge.copy("synthetic", { account: "a", epoch: 1 }); assert.equal(calls.length, top ? 1 : 0);
    navigator.userActivation.isActive = false; await bridge.copy("synthetic", {}); assert.equal(calls.length, top ? 1 : 0);
    assert.equal("send" in bridge, false); assert.equal("invoke" in bridge, false);
  }
});
