"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const { createSettingsWindow } = require("../scripts/settings-window.cjs");
const { createModelTracesWindow } = require("../scripts/model-traces-window.cjs");
const { attachModelTraces } = require("../scripts/model-traces-host.cjs");

const CLIENT = "http://127.0.0.1:43210/";
function fixture({ area = { x: 0, y: 0, width: 1920, height: 1040 }, appUpdatesEnabled = false } = {}) {
  const made = [], handlers = new Map(), ipcMain = new EventEmitter();
  ipcMain.handle = (name, handler) => { assert.equal(handlers.has(name), false); handlers.set(name, handler); };
  ipcMain.removeHandler = name => handlers.delete(name);
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.sent = [];
      this.webContents = new EventEmitter();
      this.webContents.id = made.length + 1;
      this.webContents.mainFrame = { url: "" };
      this.webContents.session = {};
      this.webContents.isDestroyed = () => this.isDestroyed();
      this.webContents.send = (...args) => this.sent.push(args);
      this.webContents.setWindowOpenHandler = handler => { this.popupHandler = handler; };
      made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    getBounds() { return { x: 1800, y: 900, width: 1180, height: 780 }; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    close() {
      let prevented = false;
      this.emit("close", { preventDefault() { prevented = true; } });
      if (!prevented) this.destroy();
    }
    destroy() { this.destroyed = true; this.emit("closed"); }
    loadURL(url) { this.webContents.mainFrame.url = url; return Promise.resolve(); }
  }
  const main = new Window({}); main.webContents.mainFrame.url = CLIENT;
  const mainEvent = { sender: main.webContents, senderFrame: main.webContents.mainFrame };
  const options = { BrowserWindow: Window, ipcMain, mainWindow: main, clientUrl: CLIENT, appUpdatesEnabled,
    trustedMainFrame: event => !main.isDestroyed() && event.sender === main.webContents &&
      event.senderFrame === main.webContents.mainFrame && event.senderFrame.url === CLIENT,
    screen: { getDisplayMatching: () => ({ workArea: area }) },
    getControlToken: () => "synthetic-control", fetchImpl: async (url, opts) => ({ ok: true, status: 200, json: async () =>
      String(url).endsWith("/session") ? { token: "synthetic-session", accountId: "synthetic-account",
        scope: JSON.parse(opts.body).scope, deleteVersion: 0 } : { enabled: false } }) };
  const mainHost = attachModelTraces(options);
  const manager = createSettingsWindow(options);
  const viewer = () => made.at(-1);
  const event = () => ({ sender: viewer().webContents, senderFrame: viewer().webContents.mainFrame });
  return { main, made, handlers, ipcMain, options, manager, mainHost, mainEvent, viewer, event,
    close() { manager.close(); mainHost.close(); } };
}

test("settings opens as a single independent resizable native window and reuses targets", () => {
  const f = fixture({ appUpdatesEnabled: true });
  assert.equal(f.handlers.get("settings-window:open")(f.mainEvent, { tab: "models" }), true);
  const child = f.viewer(); child.emit("ready-to-show");
  assert.equal(child.shown, true);
  assert.equal(child.options.width, 900); assert.equal(child.options.height, 780);
  assert.equal(child.options.minWidth, 680); assert.equal(child.options.minHeight, 500);
  assert.equal(child.options.parent, undefined); assert.equal(child.options.modal, undefined);
  assert.equal(child.options.titleBarOverlay, undefined);
  assert.equal(child.options.resizable, true);
  assert.equal(child.options.x + child.options.width <= 1920, true);
  assert.equal(child.options.y + child.options.height <= 1040, true);
  assert.equal(child.options.webPreferences.session, f.main.webContents.session);
  assert.equal(child.options.webPreferences.sandbox, true);
  assert.equal(child.options.webPreferences.contextIsolation, true);
  assert.equal(child.options.webPreferences.nodeIntegration, false);
  assert.deepEqual(child.options.webPreferences.additionalArguments, ["--kyf-settings-window", "--kyf-app-updates-enabled"]);
  assert.equal(child.webContents.mainFrame.url, CLIENT + "?window=settings");
  assert.deepEqual(f.handlers.get("settings-window:target")(f.event()), { tab: "models" });
  child.minimized = true;
  f.manager.open({ tab: "plugins", plugin: "kyf.synthetic" });
  assert.equal(f.made.length, 2); assert.equal(child.minimized, false);
  assert.deepEqual(child.sent.at(-1), ["settings-window:target", { tab: "plugins", plugin: "kyf.synthetic" }]);
  const notifications = child.sent.length;
  f.manager.open({});
  assert.equal(child.sent.length, notifications);
  assert.deepEqual(f.handlers.get("settings-window:target")(f.event()), { tab: "plugins", plugin: "kyf.synthetic" });
  assert.deepEqual(f.main.sent, []);
  f.close();
});

test("small display clamps the entire window including minimum size", () => {
  const f = fixture({ area: { x: -640, y: 0, width: 640, height: 480 } });
  f.manager.open(); const { options } = f.viewer();
  assert.deepEqual([options.x, options.y, options.width, options.height, options.minWidth, options.minHeight],
    [-640, 0, 640, 480, 640, 480]); f.close();
});

test("only trusted main can open; settings capabilities require exact child URL and top frame", async () => {
  const f = fixture(), open = f.handlers.get("settings-window:open");
  assert.equal(open({ sender: {}, senderFrame: {} }, {}), false);
  for (const value of [null, [], { tab: "unknown" }, { section: "other" }, { plugin: "../other" }])
    assert.equal(open(f.mainEvent, value), false);
  assert.equal(f.made.length, 1); open(f.mainEvent, { tab: "general", section: "source" });
  const childEvent = f.event();
  assert.equal(open(childEvent, {}), false);
  assert.equal(f.handlers.get("settings-window:target")(f.mainEvent), null);
  assert.equal(f.manager.trusted({ ...childEvent, senderFrame: { url: childEvent.senderFrame.url } }), false);
  f.viewer().webContents.mainFrame.url += "&forged=1";
  assert.equal(f.manager.trusted(childEvent), false);
  assert.equal(f.handlers.get("settings-window:close")(childEvent), false);
  assert.equal((await f.handlers.get("model-traces:request")(childEvent, "state", {})).statusCode, 403);
  f.close();
});

test("all child navigation and popups are blocked and title stays native", () => {
  const f = fixture(); f.manager.open();
  for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview", "page-title-updated"]) {
    let blocked = false;
    f.viewer().webContents.emit(name, { preventDefault() { blocked = true; } });
    assert.equal(blocked, true, name);
  }
  assert.deepEqual(f.viewer().popupHandler(), { action: "deny" }); f.close();
});

test("native close waits for renderer draft guard and approved close releases only child trace access", async () => {
  const f = fixture(); f.manager.open(); const child = f.viewer(), old = f.event();
  child.webContents.emit("did-finish-load"); child.close();
  assert.equal(child.isDestroyed(), false);
  assert.deepEqual(child.sent.at(-1), ["settings-window:request-close"]);
  child.close();
  assert.equal(child.isDestroyed(), false);
  assert.equal(child.sent.filter(([name]) => name === "settings-window:request-close").length, 2,
    "a canceled leave can be retried from the native close button");
  assert.equal((await f.handlers.get("model-traces:request")(old, "state", {})).ok, true);
  assert.equal(f.handlers.get("settings-window:close")(old), true);
  assert.equal(child.isDestroyed(), true);
  assert.equal((await f.handlers.get("model-traces:request")(old, "state", {})).statusCode, 403);
  assert.equal((await f.handlers.get("model-traces:request")(f.mainEvent, "state", {})).ok, true);
  f.manager.open(); assert.equal(f.made.length, 3); f.close();
  assert.equal(f.handlers.size, 0);
});

test("main reload, crash and closure destroy settings and revoke its IPC", () => {
  for (const name of ["did-start-navigation", "render-process-gone", "closed"]) {
    const f = fixture(); f.manager.open(); const child = f.viewer();
    if (name === "closed") f.main.destroy(); else f.main.webContents.emit(name, {}, CLIENT, false, true);
    assert.equal(child.isDestroyed(), true, name);
    if (name === "closed") assert.equal(f.handlers.has("settings-window:open"), false);
    else { f.manager.open(); assert.equal(f.made.length, 3); }
    f.close();
  }
  const f = fixture(); f.manager.open(); const child = f.viewer();
  child.webContents.emit("render-process-gone"); assert.equal(child.isDestroyed(), true);
  f.manager.open(); assert.equal(f.made.length, 3); f.close();
});

test("settings navigation activates the main window with validated plugin identifiers", () => {
  const f = fixture(); f.manager.open(); const nav = f.handlers.get("settings-window:main-nav");
  assert.equal(nav(f.mainEvent, "kyf.synthetic", "home"), false);
  assert.equal(nav(f.event(), "../bad", "home"), false);
  assert.equal(nav(f.event(), "kyf.synthetic", "bad\nnav"), false);
  f.main.minimized = true;
  assert.equal(nav(f.event(), "kyf.synthetic", "home"), true);
  assert.equal(f.main.minimized, false); assert.equal(f.main.focused, true);
  assert.deepEqual(f.main.sent.at(-1), ["settings-window:main-nav", { plugin: "kyf.synthetic", nav: "home" }]);
  f.manager.broadcast("real-client:bridge-restored");
  assert.deepEqual(f.viewer().sent.at(-1), ["real-client:bridge-restored", undefined]); f.close();
});

test("validated settings navigation opens the native self-analysis surface using main's current context", () => {
  const f = fixture(); let opened = 0;
  f.options.openSelfAnalysis = () => { opened++; return true; };
  f.manager.open(); const nav = f.handlers.get("settings-window:main-nav");
  assert.equal(nav(f.mainEvent, "kyf.self-analysis", "self-analysis"), false);
  assert.equal(opened, 0);
  assert.equal(nav({ ...f.event(), senderFrame: { url: f.viewer().webContents.mainFrame.url } }, "kyf.self-analysis", "self-analysis"), false);
  assert.equal(opened, 0);
  assert.equal(nav(f.event(), "kyf.self-analysis", "self-analysis"), true);
  assert.equal(opened, 1); assert.equal(f.main.sent.length, 0, "no renderer IPC replay needs a second user activation");
  assert.equal(nav(f.event(), "kyf.other", "self-analysis"), true); assert.equal(opened, 1);
  f.close();
});

test("settings can open model calls without gaining permission to overwrite main context", () => {
  const f = fixture();
  const modelWindow = createModelTracesWindow({ ...f.options,
    trustedOpenFrame: event => f.options.trustedMainFrame(event) || f.manager.trusted(event) });
  f.manager.open(); const settingsEvent = f.event();
  const context = { account: "synthetic", user: "", view: "chat", theme: "dark" };
  assert.equal(f.handlers.get("model-traces-window:open")(settingsEvent, context, true), true);
  const initial = f.handlers.get("model-traces-window:context")(f.event());
  f.ipcMain.emit("model-traces-window:sync", settingsEvent, { ...context, account: "other" });
  assert.deepEqual(f.handlers.get("model-traces-window:context")(f.event()), initial);
  modelWindow.close(); f.close();
});

function preload({ settings = false, subframe = false } = {}) {
  const calls = [], sent = [], ipcRenderer = new EventEmitter(); let host;
  ipcRenderer.invoke = async (...args) => { calls.push(args); return true; };
  ipcRenderer.send = (...args) => sent.push(args);
  const window = {}; window.top = subframe ? {} : window;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../scripts/real-client-preload.cjs"), "utf8"), {
    require: () => ({ ipcRenderer, contextBridge: { exposeInMainWorld: (_name, value) => { host = value; } } }),
    process: { argv: settings ? ["--kyf-settings-window"] : [], env: {} },
    document: { addEventListener() {} }, window, navigator: { userActivation: { isActive: false } },
  });
  return { host, calls, sent, ipcRenderer };
}

test("preload settings identity is host-owned; child cannot synchronize main model context", async () => {
  const main = preload(), child = preload({ settings: true });
  assert.equal(main.host.isSettingsWindow, false); assert.equal(child.host.isSettingsWindow, true);
  assert.equal(Object.isFrozen(child.host), true);
  await main.host.openSettingsWindow({ tab: "models" });
  assert.deepEqual(main.calls[0], ["settings-window:open", { tab: "models" }]);
  assert.equal(await main.host.closeSettingsWindow(), false);
  assert.equal(await child.host.openSettingsWindow({}), false);
  child.host.syncModelTracesWindow({ account: "forbidden" });
  assert.deepEqual(child.sent, []);
  let closes = 0;
  const off = child.host.onSettingsCloseRequested(() => closes++);
  child.ipcRenderer.emit("settings-window:request-close"); off();
  child.ipcRenderer.emit("settings-window:request-close"); assert.equal(closes, 1);
  await child.host.closeSettingsWindow();
  assert.deepEqual(child.calls[0], ["settings-window:close"]);
  const subframe = preload({ settings: true, subframe: true });
  assert.equal(await subframe.host.getSettingsTarget(), null);
  assert.equal(await subframe.host.closeSettingsWindow(), false);
  assert.equal(await subframe.host.openSettingsMainNav("kyf.synthetic", "home"), false);
  assert.deepEqual(subframe.calls, []);
});
