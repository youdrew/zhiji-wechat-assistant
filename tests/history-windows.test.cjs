// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic host tests, with no real account, messages, bridge or native windows.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createHistoryWindows, historyScope } = require("../scripts/history-windows.cjs");
const CLIENT = "http://127.0.0.1:34567/";
const A = { account: "synthetic-account", user: "wxid_a", name: "联系人甲", theme: "light" };
const B = { ...A, user: "group@chatroom", name: "合成群聊" };

function harness() {
  const made = [], handlers = new Map(), images = [];
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.sent = [];
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = { url: "" };
      this.webContents.setWindowOpenHandler = value => { this.popups = value; };
      this.webContents.send = (...value) => this.sent.push(value);
      made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    setTitle(value) { this.title = value; }
    getBounds() { return { x: 100, y: 80, width: 1180, height: 780 }; }
    destroy() { this.destroyed = true; this.emit("closed"); }
    close() { this.destroy(); }
    loadURL(value) { this.url = value; this.webContents.mainFrame.url = value; return Promise.resolve(); }
  }
  const main = new Window({});
  main.webContents.mainFrame.url = CLIENT;
  main.webContents.session = { synthetic: true };
  const trustedMainFrame = event => event.sender === main.webContents && event.senderFrame === main.webContents.mainFrame &&
    event.senderFrame.url === CLIENT;
  const manager = createHistoryWindows({ BrowserWindow: Window, mainWindow: main, clientUrl: CLIENT,
    trustedMainFrame, ipcMain: { handle: (name, callback) => handlers.set(name, callback), removeHandler: name => handlers.delete(name) },
    openImagePreview: scope => { images.push(scope); return true; }, screen: {
      getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }),
    } });
  const event = win => ({ sender: win.webContents, senderFrame: win.webContents.mainFrame });
  return { manager, main, made, handlers, images, event };
}

test("history is a sandboxed independent window reused only for the same account and conversation", () => {
  const h = harness();
  assert.equal(h.handlers.get("history:open")(h.event(h.main), A, true), true);
  const first = h.made[1];
  const target = new URL(first.url), values = new URLSearchParams(target.hash.slice(1));
  assert.equal(target.origin, new URL(CLIENT).origin);
  assert.equal(target.pathname, "/history.html");
  assert.equal(values.get("account"), A.account);
  assert.equal(values.get("user"), A.user);
  assert.equal(values.get("name"), A.name);
  assert.equal(first.options.title, "联系人甲 · 聊天记录");
  assert.equal(first.options.width, 580);
  assert.equal(first.options.height, 760);
  assert.equal(first.options.webPreferences.session, h.main.webContents.session);
  assert.equal(first.options.webPreferences.nodeIntegration, false);
  assert.equal(first.options.webPreferences.sandbox, true);
  assert.equal(first.options.webPreferences.contextIsolation, true);
  assert.match(first.options.webPreferences.preload, /history-preload\.cjs$/);
  assert.equal(first.options.parent, undefined);
  first.minimized = true;
  h.manager.open({ ...A, name: "新称呼" });
  assert.equal(h.made.length, 2);
  assert.equal(first.minimized, false);
  assert.equal(first.focused, true);
  assert.equal(first.title, "新称呼 · 聊天记录");
  h.manager.open(B);
  h.manager.open({ ...A, account: "another-synthetic-account" });
  assert.equal(h.manager.count(), 3);
  assert.ok(h.made[2].options.x > first.options.x);
});

test("opening history requires a trusted activated main frame and validated identifiers", () => {
  const h = harness(), open = h.handlers.get("history:open");
  for (const value of [null, [], {}, { ...A, account: "" }, { ...A, user: "line\nbreak" },
    { ...A, name: "x".repeat(129) }, { ...A, account: "x".repeat(257) }])
    assert.equal(open(h.event(h.main), value, true), false);
  assert.equal(open(h.event(h.main), A, false), false);
  assert.equal(open({ sender: h.main.webContents, senderFrame: { url: CLIENT } }, A, true), false);
  assert.equal(open({ sender: {}, senderFrame: h.main.webContents.mainFrame }, A, true), false);
  assert.equal(historyScope({ account: "a", user: "u" }).name, "u");
  assert.equal(historyScope(B).user, B.user, "group history remains available");
  h.manager.open({ ...A, user: "user&account=wrong#part", url: "file:///private" });
  assert.equal(new URLSearchParams(new URL(h.made[1].url).hash.slice(1)).get("user"), "user&account=wrong#part");
});

test("child IPC is restricted to its exact page, account and conversation", () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1], trusted = h.event(win);
  const locate = h.handlers.get("history:locate"), image = h.handlers.get("history:image");
  const target = { account: A.account, user: A.user, cursor: "synthetic-cursor", messageId: "synthetic-message" };
  assert.deepEqual(h.handlers.get("history:context")(trusted), A);
  assert.equal(locate(trusted, target, true), true);
  assert.deepEqual(h.main.sent.at(-1), ["history:locate", target]);
  const scope = { account: A.account, user: A.user, id: "a".repeat(64) };
  assert.equal(image(trusted, scope, true), true);
  assert.deepEqual(h.images, [scope]);
  for (const other of [{ account: "other" }, { user: B.user }]) {
    assert.equal(locate(trusted, { ...target, ...other }, true), false);
    assert.equal(image(trusted, { ...scope, ...other }, true), false);
  }
  assert.equal(locate(trusted, target, false), false);
  assert.equal(image(trusted, scope, false), false);
  assert.equal(image(trusted, { ...scope, id: "file:///private" }, true), false);
  assert.equal(locate({ ...trusted, senderFrame: { url: win.url } }, target, true), false);
  win.webContents.mainFrame.url = win.url + "unexpected";
  assert.equal(locate(trusted, target, true), false);
  assert.equal(image(trusted, scope, true), false);
  assert.equal(h.handlers.get("history:context")(trusted), null);
});

test("history blocks popups and navigation, broadcasts changes, and closes with its host", () => {
  const h = harness(); h.manager.open(A); h.manager.open(B);
  const win = h.made[1];
  assert.deepEqual(win.popups(), { action: "deny" });
  for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"]) {
    let prevented = false;
    win.webContents.emit(name, { preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
  }
  h.manager.setTheme("dark");
  assert.deepEqual(win.sent.at(-1), ["history:host-event", { type: "theme", theme: "dark" }]);
  assert.equal(h.handlers.get("history:context")(h.event(win)).theme, "dark");
  h.manager.broadcast({ type: "service-restored" });
  assert.deepEqual(win.sent.at(-1), ["history:host-event", { type: "service-restored" }]);
  h.main.destroy();
  assert.equal(h.manager.count(), 0);
  assert.equal(h.handlers.size, 0);
  assert.equal(h.manager.open(A), false);
});

test("a host crash or full navigation destroys history without affecting an in-place navigation", () => {
  for (const trigger of [h => h.main.webContents.emit("render-process-gone"),
    h => h.main.webContents.emit("did-start-navigation", {}, CLIENT, false, true)]) {
    const h = harness(); h.manager.open(A);
    h.main.webContents.emit("did-start-navigation", {}, CLIENT, true, true);
    assert.equal(h.manager.count(), 1);
    const win = h.made[1]; trigger(h); win.emit("ready-to-show");
    assert.equal(win.destroyed, true);
    assert.equal(win.shown, undefined);
    assert.equal(h.manager.count(), 0);
    h.manager.close();
  }
});

function loadPreload(file, { subframe = false, active = true } = {}) {
  const hosts = {}, calls = [], listeners = new Map();
  const window = {}; window.top = subframe ? {} : window;
  const navigator = { userActivation: { isActive: active } };
  vm.runInNewContext(fs.readFileSync(require.resolve(file), "utf8"), {
    require: () => ({ contextBridge: { exposeInMainWorld: (name, api) => { hosts[name] = api; } },
      ipcRenderer: { on: (name, listener) => listeners.set(name, listener), removeListener: name => listeners.delete(name),
        invoke: (...args) => { calls.push(args); return Promise.resolve(true); }, send() {} } }),
    window, navigator, process: { env: {} }, document: { addEventListener() {} },
  });
  return { hosts, calls, listeners, navigator, window };
}

test("main preload opens history only on an active main-frame gesture", async () => {
  const h = loadPreload("../scripts/real-client-preload.cjs");
  assert.equal(await h.hosts.desktopHost.openHistoryWindow(A), true);
  assert.deepEqual(h.calls.at(-1), ["history:open", A, true]);
  h.navigator.userActivation.isActive = false;
  assert.equal(await h.hosts.desktopHost.openHistoryWindow(A), false);
  h.navigator.userActivation.isActive = true; h.window.top = {};
  assert.equal(await h.hosts.desktopHost.openHistoryWindow(A), false);
  assert.equal(h.calls.length, 1);
});

test("history preload exposes only minimal scoped navigation and image actions", async () => {
  const h = loadPreload("../scripts/history-preload.cjs");
  assert.deepEqual(Object.keys(h.hosts.desktopHost), ["openImagePreview"]);
  assert.deepEqual(Object.keys(h.hosts.historyHost), ["getContext", "close", "locate", "onHostEvent"]);
  assert.equal(Object.isFrozen(h.hosts.desktopHost), true);
  const target = { ...A, messageId: "synthetic-message", cursor: "synthetic-cursor" };
  await h.hosts.historyHost.getContext();
  assert.equal(await h.hosts.historyHost.locate(target), true);
  assert.equal(await h.hosts.desktopHost.openImagePreview({ ...A, id: "a".repeat(64) }), true);
  assert.deepEqual(h.calls.map(value => value[0]), ["history:context", "history:locate", "history:image"]);
  h.navigator.userActivation.isActive = false;
  assert.equal(await h.hosts.historyHost.locate(target), false);
  assert.equal(await h.hosts.desktopHost.openImagePreview(A), false);
  let received = null;
  const unsubscribe = h.hosts.historyHost.onHostEvent(value => { received = value; });
  h.listeners.get("history:host-event")({}, { type: "unexpected" });
  assert.equal(received, null);
  h.listeners.get("history:host-event")({}, { type: "theme", theme: "light" });
  assert.equal(received.theme, "light");
  unsubscribe();
  assert.equal(h.listeners.size, 0);
  h.window.top = {};
  assert.equal(await h.hosts.historyHost.getContext(), null);
  assert.equal(await h.hosts.historyHost.close(), false);
  assert.equal(h.calls.length, 3);
});

test("portable and update packages include the complete history window runtime", () => {
  const projectRoot = path.resolve(__dirname, "..");
  const builder = JSON.parse(fs.readFileSync(path.join(projectRoot, "electron-builder.real-client.json"), "utf8"));
  const stageSource = fs.readFileSync(path.join(projectRoot, "scripts/stage-real-client.py"), "utf8");
  const releaseSource = fs.readFileSync(path.join(projectRoot, "scripts/build-windows-release.py"), "utf8");
  const tuple = (source, name) => {
    const body = source.match(new RegExp(`^${name} = \\(([\\s\\S]*?)^\\)`, "m"))?.[1];
    assert.ok(body, `${name} runtime allowlist exists`);
    return [...body.matchAll(/"([^"\n]+)"/g)].map(match => match[1]);
  };
  const stagedScripts = tuple(stageSource, "SCRIPTS");
  const stagedUi = tuple(stageSource, "PUBLIC_FILES");
  const releaseScripts = tuple(releaseSource, "ASAR_SCRIPTS");
  for (const name of ["history-windows.cjs", "history-preload.cjs"]) {
    const relative = `scripts/${name}`;
    assert.ok(builder.files.includes(relative), `desktop ASAR includes ${relative}`);
    assert.ok(stagedScripts.includes(name), `client stage includes ${relative}`);
    assert.ok(releaseScripts.includes(relative), `update ASAR verification includes ${relative}`);
    assert.equal(fs.existsSync(path.join(projectRoot, relative)), true);
  }
  for (const name of ["history.html", "history-panel.js", "history-panel.css"]) {
    const relative = `chatui/${name}`;
    assert.ok(stagedUi.includes(relative), `client resources include ${relative}`);
    assert.equal(fs.existsSync(path.join(projectRoot, relative)), true);
  }
});
