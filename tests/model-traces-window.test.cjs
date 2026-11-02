"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createModelTracesWindow } = require("../scripts/model-traces-window.cjs");
const { attachModelTraces } = require("../scripts/model-traces-host.cjs");

const CLIENT = "http://127.0.0.1:43210/";
const A = { account: "account-a", user: "contact-a", view: "chat", theme: "dark" };
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(responder = () => ({ records: [] })) {
  const made = [], handlers = new Map(), calls = [], copies = [];
  const ipcMain = new EventEmitter();
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
      this.webContents.send = (name, value) => this.sent.push([name, value]);
      this.webContents.setWindowOpenHandler = handler => { this.popupHandler = handler; };
      made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    getBounds() { return { x: 100, y: 80, width: 1180, height: 780 }; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    close() { this.destroy(); }
    destroy() { this.destroyed = true; this.emit("closed"); }
    loadURL(url) { this.webContents.mainFrame.url = url; return Promise.resolve(); }
  }
  const main = new Window({}); main.webContents.mainFrame.url = CLIENT;
  const mainEvent = { sender: main.webContents, senderFrame: main.webContents.mainFrame };
  let account = A.account;
  const hostOptions = { ipcMain, mainWindow: main, clientUrl: CLIENT,
    trustedMainFrame: e => !main.isDestroyed() && e.sender === main.webContents && e.senderFrame === main.webContents.mainFrame,
    getControlToken: () => "synthetic-control", fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      const value = String(url).endsWith("/session") ? { token: "synthetic-session", accountId: account,
        scope: JSON.parse(options.body).scope, deleteVersion: 0 } : await responder(url, options);
      return { ok: true, status: 200, json: async () => value };
    } };
  const mainHost = attachModelTraces(hostOptions);
  const manager = createModelTracesWindow({ ...hostOptions, BrowserWindow: Window,
    clipboard: { writeText: value => copies.push(value) },
    screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }) } });
  const viewer = () => made.at(-1);
  const viewerEvent = () => ({ sender: viewer().webContents, senderFrame: viewer().webContents.mainFrame });
  const run = (operation, args) => handlers.get("model-traces:request")(viewerEvent(), operation, args);
  return { main, made, handlers, ipcMain, manager, mainHost, calls, copies, mainEvent, viewer, viewerEvent, run,
    account: value => { account = value; }, close() { manager.close(); mainHost.close(); } };
}

test("trace entry creates one independent sandboxed window and reuses it without touching the main layout", async () => {
  const f = fixture();
  assert.equal(f.handlers.get("model-traces-window:open")(f.mainEvent, A, true), true);
  const viewer = f.viewer(); viewer.emit("ready-to-show");
  assert.equal(viewer.shown, true);
  assert.equal(viewer.options.resizable, true);
  assert.equal(viewer.options.parent, undefined);
  assert.equal(viewer.options.modal, undefined);
  assert.equal(viewer.options.webPreferences.session, f.main.webContents.session);
  assert.equal(viewer.options.webPreferences.sandbox, true);
  assert.equal(viewer.options.webPreferences.contextIsolation, true);
  assert.equal(viewer.options.webPreferences.nodeIntegration, false);
  assert.match(viewer.options.webPreferences.preload, /model-traces-preload\.cjs$/);
  assert.equal(viewer.webContents.mainFrame.url, CLIENT + "model-traces-window.html");
  viewer.minimized = true;
  f.manager.open(A);
  assert.equal(f.made.length, 2); assert.equal(viewer.minimized, false);
  assert.deepEqual(f.main.sent, []);
  const scope = await f.run("scope", { scope: "account" });
  assert.equal(scope.accountId, A.account); assert.equal(scope.token, undefined);
  f.close();
});

test("only trusted activated main entry opens and only the exact viewer frame may read or close", async () => {
  const f = fixture(), open = f.handlers.get("model-traces-window:open");
  assert.equal(open(f.mainEvent, A, false), false);
  assert.equal(open({ sender: {}, senderFrame: {} }, A, true), false);
  for (const value of [null, { ...A, user: "bad\nname" }, { ...A, account: "x".repeat(257) }]) {
    assert.equal(open(f.mainEvent, value, true), false);
  }
  assert.equal(f.made.length, 1); open(f.mainEvent, A, true);
  const event = f.viewerEvent(), get = f.handlers.get("model-traces-window:context");
  assert.equal(get(f.mainEvent), null);
  assert.equal(get({ ...event, senderFrame: { url: event.senderFrame.url } }), null);
  const initial = get(event); assert.equal(initial.account, A.account);
  f.viewer().webContents.mainFrame.url += "?untrusted=1";
  assert.equal(get(event), null);
  assert.equal((await f.run("records", {})).statusCode, 403);
  assert.equal(f.handlers.get("model-traces-window:close")(event), false);
  f.close();
});

test("navigation, redirects, child frames and popups cannot leave the diagnostic page", () => {
  const f = fixture(); f.manager.open(A);
  for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"]) {
    let blocked = false;
    f.viewer().webContents.emit(name, { preventDefault() { blocked = true; } });
    assert.equal(blocked, true, name);
  }
  assert.deepEqual(f.viewer().popupHandler(), { action: "deny" }); f.close();
});

test("switching account cancels a pending old body before syncing the independent viewer", async () => {
  let resolve;
  const f = fixture(() => new Promise(done => { resolve = done; })); f.manager.open(A);
  await f.run("scope", { scope: "account" });
  const pending = f.run("body", { id: "old-account-record", kind: "request" }); await tick();
  const signal = f.calls.at(-1).options.signal;
  f.account("account-b"); f.manager.sync({ ...A, account: "account-b", user: "contact-b" });
  assert.equal(signal.aborted, true);
  assert.deepEqual(f.viewer().sent.slice(-2).map(([name]) => name), ["model-traces:invalidated", "model-traces-window:context"]);
  resolve({ text: "OLD_ACCOUNT_BODY" });
  assert.equal((await pending).error, "trace-stale");
  assert.equal((await f.run("scope", { scope: "account" })).accountId, "account-b"); f.close();
});

test("contact, theme and main view changes synchronize without closing or resizing the viewer", () => {
  const f = fixture(); f.manager.open(A); const viewer = f.viewer();
  f.ipcMain.emit("model-traces-window:sync", f.mainEvent, { ...A, user: "contact-b", view: "advisor", theme: "light" });
  assert.equal(viewer.isDestroyed(), false); assert.equal(f.made.length, 2);
  const updated = f.handlers.get("model-traces-window:context")(f.viewerEvent());
  assert.equal(updated.user, "contact-b"); assert.equal(updated.theme, "light"); assert.equal(updated.view, "advisor");
  assert.ok(updated.revision > 1);
  f.ipcMain.emit("model-traces-window:sync", { sender: {} }, A);
  assert.deepEqual(f.handlers.get("model-traces-window:context")(f.viewerEvent()), updated);
  f.close();
});

test("close and reopen revoke old viewer access but retain main settings IPC", async () => {
  const f = fixture(); f.manager.open(A); const oldEvent = f.viewerEvent();
  assert.equal(f.handlers.get("model-traces-window:close")(oldEvent), true);
  assert.equal((await f.handlers.get("model-traces:request")(oldEvent, "state", {})).statusCode, 403);
  assert.equal((await f.handlers.get("model-traces:request")(f.mainEvent, "state", {})).ok, true);
  f.manager.open(A); assert.equal(f.made.length, 3);
  f.manager.close(); f.manager.close();
  assert.equal(f.viewer().isDestroyed(), true);
  assert.equal(f.handlers.has("model-traces-window:open"), false);
  assert.equal((await f.handlers.get("model-traces:request")(f.mainEvent, "state", {})).ok, true);
  f.mainHost.close(); assert.equal(f.handlers.size, 0);
});

test("main reload or crash closes the viewer; main close also releases IPC", () => {
  for (const name of ["did-start-navigation", "render-process-gone", "closed"]) {
    const f = fixture(); f.manager.open(A); const viewer = f.viewer();
    if (name === "closed") f.main.destroy(); else f.main.webContents.emit(name, {}, CLIENT, false, true);
    assert.equal(viewer.isDestroyed(), true, name);
    if (name === "closed") assert.equal(f.handlers.has("model-traces-window:open"), false);
    else { f.manager.open(A); assert.equal(f.made.length, 3); }
    f.close();
  }
});

test("copy requires a viewer gesture and remains bounded", () => {
  const f = fixture(); f.manager.open(A); const copy = f.handlers.get("model-traces-window:copy");
  assert.equal(copy(f.mainEvent, "synthetic", true), false);
  assert.equal(copy(f.viewerEvent(), "synthetic", false), false);
  assert.equal(copy(f.viewerEvent(), "x".repeat(1048577), true), false);
  assert.equal(copy(f.viewerEvent(), "synthetic", true), true);
  assert.deepEqual(f.copies, ["synthetic"]); f.close();
});

test("minimal viewer preload gates subframes and gestures without exposing arbitrary IPC or secrets", async () => {
  const api = {}, calls = [], ipcRenderer = new EventEmitter();
  ipcRenderer.invoke = async (...args) => { calls.push(args); return true; };
  const window = {}; window.top = window;
  const navigator = { userActivation: { isActive: false } };
  vm.runInNewContext(readFileSync(path.join(__dirname, "../scripts/model-traces-preload.cjs"), "utf8"), {
    window, navigator, require: name => {
      assert.equal(name, "electron");
      return { ipcRenderer, contextBridge: { exposeInMainWorld: (name, value) => { api[name] = value; } } };
    },
  });
  assert.equal(await api.desktopHost.copyDraft("synthetic"), false);
  navigator.userActivation.isActive = true;
  assert.equal(await api.desktopHost.copyDraft("synthetic"), true);
  assert.deepEqual(calls[0], ["model-traces-window:copy", "synthetic", true]);
  assert.equal(Object.isFrozen(api.desktopHost), true);
  assert.equal(api.desktopHost.openFriendWindow, undefined);
  window.top = {};
  assert.equal(await api.modelTracesWindow.getContext(), null);
  assert.equal(await api.modelTracesWindow.close(), false);
  assert.equal((await api.desktopHost.modelTracesRequest("state")).ok, false);
  assert.equal(calls.length, 1);
});

function navigationFixture({ delayedContext = false } = {}) {
  const nodes = new Map(), calls = [], keydown = [], windowEvents = new Map();
  let contextListener, usageOptions, traceOptions, finishContext, disposed = false;
  const document = { activeElement: null, body: { classList: { toggle: (name, enabled) => calls.push(["theme", name, enabled]) } },
    addEventListener: (name, handler) => { if (name === "keydown") keydown.push(handler); },
    getElementById(id) {
      if (!nodes.has(id)) {
        const value = { id, hidden: true, attributes: {}, listeners: {}, textContent: "", tabIndex: -1,
          setAttribute(name, text) { this.attributes[name] = String(text); },
          getAttribute(name) { return this.attributes[name] ?? null; },
          addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); },
          focus() { document.activeElement = this; } };
        nodes.set(id, value);
      }
      return nodes.get(id);
    } };
  const window = { ModelUsage: {
    init(options) { usageOptions = options; calls.push(["usage-init", options.account]); },
    open(view) { calls.push(["usage-open", view]); }, hide() { calls.push(["usage-hide"]); },
    setScope(account) { calls.push(["usage-scope", account]); },
    setDeveloperMode(setting) { calls.push(["developer-mode", setting]); },
  }, ModelTraces: {
    init(options) { traceOptions = options; calls.push(["traces-init", options.account]); },
    setScope(...scope) { calls.push(["trace-scope", ...scope]); },
  }, modelTracesWindow: {
    getContext: () => delayedContext ? new Promise(resolve => { finishContext = resolve; }) : Promise.resolve({ ...A, revision: 1 }),
    onContext(handler) { contextListener = handler; return () => { disposed = true; }; },
    close: async () => { calls.push(["close-window"]); },
  }, addEventListener: (name, handler) => windowEvents.set(name, handler) };
  vm.runInNewContext(readFileSync(path.join(__dirname, "../chatui/model-traces-window.js"), "utf8"), { window, document });
  const sendKey = (id, key, extra = {}) => {
    let prevented = false;
    const target = document.getElementById(id);
    for (const handler of target.listeners.keydown || []) handler({ key, target, preventDefault() { prevented = true; }, ...extra });
    return prevented;
  };
  return { window, document, nodes, calls, keydown, windowEvents, sendKey,
    get usageOptions() { return usageOptions; }, get traceOptions() { return traceOptions; },
    context: context => contextListener(context), finishContext: context => finishContext(context), disposed: () => disposed };
}

test("navigation switches between statistics and call records in one shell", async () => {
  const f = navigationFixture(); await tick();
  assert.equal(f.nodes.get("modelCallsNavigation").hidden, false);
  assert.equal(f.nodes.get("modelCallsContent").hidden, false);
  assert.equal(f.nodes.get("modelCallsStatsTab").getAttribute("aria-selected"), "true");
  assert.equal(f.usageOptions.mount, f.nodes.get("modelCallsContent"));
  assert.equal(f.usageOptions.traces, f.window.ModelTraces, "call records show captured bodies through the trace viewer");
  assert.equal(f.traceOptions.standalone, true);
  assert.equal(f.window.ModelCalls.setView("records"), true);
  assert.equal(f.nodes.get("modelCallsStatsTab").tabIndex, -1);
  assert.equal(f.nodes.get("modelCallsRecordsTab").tabIndex, 0);
  assert.equal(f.nodes.get("modelUsagePanel").getAttribute("aria-labelledby"), "modelCallsRecordsTab");
  assert.deepEqual(f.calls.at(-1), ["usage-open", "records"]);
  const settings = { enabled: true }; f.traceOptions.onSettings(settings);
  assert.deepEqual(f.calls.at(-1), ["developer-mode", settings]);
  f.window.ModelCalls.setView("stats");
  assert.deepEqual(f.calls.at(-1), ["usage-open", "stats"]);
  const calls = f.calls.length;
  assert.equal(f.window.ModelCalls.setView("diagnostics"), false);
  assert.equal(f.calls.length, calls);
  f.windowEvents.get("beforeunload")(); assert.equal(f.disposed(), true);
});

test("top page tabs support roving keyboard focus and respect composition; usage owns Escape", async () => {
  const f = navigationFixture(); await tick();
  assert.equal(f.sendKey("modelCallsStatsTab", "ArrowRight"), true);
  assert.equal(f.document.activeElement.id, "modelCallsRecordsTab");
  assert.equal(f.nodes.get("modelCallsRecordsTab").getAttribute("aria-selected"), "true");
  assert.equal(f.sendKey("modelCallsRecordsTab", "ArrowRight"), true);
  assert.equal(f.document.activeElement.id, "modelCallsStatsTab");
  assert.equal(f.sendKey("modelCallsStatsTab", "End"), true);
  assert.equal(f.document.activeElement.id, "modelCallsRecordsTab");
  assert.equal(f.sendKey("modelCallsRecordsTab", "ArrowLeft", { isComposing: true }), false);
  assert.equal(f.document.activeElement.id, "modelCallsRecordsTab");
  assert.equal(f.sendKey("modelCallsRecordsTab", "Home"), true);
  assert.equal(f.document.activeElement.id, "modelCallsStatsTab");
  assert.equal(f.keydown.length, 0, "the usage page handles its clear confirmation and closing itself");
});

test("a newer context notification wins a late initial reply without losing the active page", async () => {
  const f = navigationFixture({ delayedContext: true });
  f.context({ ...A, account: "account-b", revision: 2 });
  f.window.ModelCalls.setView("records");
  f.finishContext({ ...A, revision: 1 }); await tick();
  assert.deepEqual(f.calls.filter(([name]) => name === "usage-init"), [["usage-init", "account-b"]]);
  assert.equal(f.calls.some(([name, account]) => name === "usage-scope" && account === "account-a"), false);
  f.context({ ...A, account: "account-c", revision: 3 });
  assert.deepEqual(f.calls.slice(-2), [["trace-scope", "account-c", A.user, A.view], ["usage-scope", "account-c"]]);
  assert.equal(f.nodes.get("modelCallsRecordsTab").getAttribute("aria-selected"), "true");
});
