// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic IPC and window tests; no runtime chats or backend requests.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createExternalImportWindows, externalImportScope } = require("../scripts/external-import-windows.cjs");
const CLIENT = "http://127.0.0.1:34567/";
const A = { account: "synthetic-account", user: "contact-a", name: "联系人甲", theme: "light", friendId: "synthetic-friend-a" };
const B = { ...A, user: "contact-b", name: "联系人乙", friendId: "synthetic-friend-b" };

function harness() {
  const made = [], handlers = new Map();
  const dialog = { choice: 0, calls: [], showMessageBoxSync(...args) { this.calls.push(args); return this.choice; } };
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.sent = [];
      this.webContents = new EventEmitter(); this.webContents.mainFrame = { url: "" };
      this.webContents.setWindowOpenHandler = callback => { this.popups = callback; };
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
    close() {
      if (this.guardUnload) {
        let allowed = false;
        this.webContents.emit("will-prevent-unload", { preventDefault() { allowed = true; } });
        if (!allowed) return;
      }
      this.destroy();
    }
    loadURL(value) { this.url = value; this.webContents.mainFrame.url = value; return Promise.resolve(); }
  }
  const main = new Window({});
  main.webContents.mainFrame.url = CLIENT; main.webContents.session = { synthetic: true };
  const trustedMainFrame = event => event.sender === main.webContents && event.senderFrame === main.webContents.mainFrame &&
    event.senderFrame.url === CLIENT;
  const manager = createExternalImportWindows({ BrowserWindow: Window, mainWindow: main, clientUrl: CLIENT,
    trustedMainFrame, dialog, ipcMain: { handle: (name, callback) => handlers.set(name, callback), removeHandler: name => handlers.delete(name) },
    screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }) } });
  const event = win => ({ sender: win.webContents, senderFrame: win.webContents.mainFrame });
  return { manager, main, made, handlers, event, dialog };
}

test("external chat windows reuse the exact account and friend without changing an existing binding", () => {
  const h = harness();
  assert.equal(h.handlers.get("external-import:open")(h.event(h.main), A, true), true);
  const first = h.made[1], target = new URL(first.url);
  assert.equal(target.origin, new URL(CLIENT).origin);
  assert.equal(target.pathname, "/external-import.html");
  for (const [key, value] of Object.entries(A)) assert.equal(target.searchParams.get(key), value);
  assert.equal(first.options.title, "联系人甲 · 外部聊天");
  assert.equal(first.options.parent, undefined);
  assert.equal(first.options.webPreferences.session, h.main.webContents.session);
  assert.equal(first.options.webPreferences.sandbox, true);
  assert.equal(first.options.webPreferences.contextIsolation, true);
  assert.equal(first.options.webPreferences.nodeIntegration, false);
  assert.equal(first.options.webPreferences.webSecurity, true);
  assert.equal(first.options.webPreferences.devTools, false);
  assert.match(first.options.webPreferences.preload, /external-import-preload\.cjs$/);
  h.manager.open(B);
  assert.deepEqual(h.handlers.get("external-import:context")(h.event(first)), A, "opening another friend never changes the first window");
  first.minimized = true;
  h.manager.open({ ...A, name: "新称呼", friendId: "wrong-friend", theme: "dark" });
  assert.equal(h.made.length, 3);
  assert.equal(first.minimized, false);
  assert.equal(first.focused, true);
  const context = h.handlers.get("external-import:context")(h.event(first));
  assert.equal(context.friendId, A.friendId, "reopening does not replace a canonical external friend binding");
  assert.equal(context.name, "新称呼");
  assert.equal(context.theme, "dark");
  h.manager.open({ ...A, account: "other-account" });
  assert.equal(h.manager.count(), 3);
});

test("only an activated trusted main frame can open validated one-to-one friend windows", () => {
  const h = harness(), open = h.handlers.get("external-import:open");
  for (const value of [null, [], {}, { ...A, account: "" }, { ...A, user: "group@chatroom" },
    { ...A, user: "line\nbreak" }, { ...A, name: "x".repeat(129) }, { ...A, account: "x".repeat(257) },
    { ...A, friendId: "" }, { ...A, friendId: "x".repeat(101) }, { ...A, friendId: "a\nb" }])
    assert.equal(open(h.event(h.main), value, true), false);
  assert.equal(open(h.event(h.main), A, false), false);
  assert.equal(open({ sender: h.main.webContents, senderFrame: { url: CLIENT } }, A, true), false);
  assert.equal(open({ sender: {}, senderFrame: h.main.webContents.mainFrame }, A, true), false);
  assert.deepEqual(externalImportScope({ account: "a", user: "u" }), { account: "a", user: "u", name: "u", theme: "dark" });
  h.manager.open({ ...A, user: "user&account=wrong#part", url: "file:///private" });
  assert.equal(new URL(h.made[1].url).searchParams.get("user"), "user&account=wrong#part");
  assert.equal(new URL(h.made[1].url).searchParams.has("url"), false);
});

test("child navigation uses its canonical scope and rejects arbitrary or forged IPC", async () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1], event = h.event(win), navigate = h.handlers.get("external-import:navigate");
  for (const value of ["file:///private", "https://example.test", "unknown", {}, null])
    assert.equal(navigate(event, value, true), false);
  assert.equal(navigate(event, "model-settings", false), false);
  assert.equal(navigate({ ...event, senderFrame: { url: win.url } }, "model-settings", true), false);
  assert.equal(navigate(h.event(h.main), "model-settings", true), false);
  for (const target of ["model-settings", "models", "plugins"]) {
    assert.equal(navigate(event, target, true), true);
    assert.deepEqual(h.main.sent.at(-1), ["external-import:navigate", { target, scope: A }]);
    assert.equal(win.destroyed, undefined);
  }
  assert.equal(h.main.focused, true);
  win.webContents.mainFrame.url = win.url + "unexpected";
  assert.equal(h.handlers.get("external-import:context")(event), null);
  assert.equal(h.handlers.get("external-import:close")(event), false);
  assert.equal(navigate(event, "chat", true), false);
  win.webContents.mainFrame.url = win.url;
  assert.equal(await navigate(event, "chat", true), true);
  assert.equal(win.destroyed, true);
  assert.deepEqual(h.main.sent.at(-1), ["external-import:navigate", { target: "chat", scope: A }]);
});

test("closing an import window only closes its UI and leaves other windows available", async () => {
  const h = harness(); h.manager.open(A); h.manager.open(B);
  const first = h.made[1], other = h.made[2];
  assert.equal(await h.handlers.get("external-import:close")(h.event(first)), true);
  assert.equal(h.manager.count(), 1);
  assert.equal(other.destroyed, undefined);
  assert.equal(h.main.destroyed, undefined);
  assert.equal(h.main.sent.length, 0, "closing makes no backend stop, delete or navigation call");
  h.manager.open(A);
  assert.equal(h.manager.count(), 2);
  assert.equal(h.made.length, 4);
});

test("only a real unsaved-edit unload guard asks whether to discard review changes", () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1];
  assert.equal(h.dialog.calls.length, 0);
  let allowed = false;
  const event = { preventDefault() { allowed = true; } };
  win.webContents.emit("will-prevent-unload", event);
  assert.equal(allowed, false, "keep editing is the default");
  assert.equal(h.dialog.calls[0][0], win);
  h.dialog.choice = 1;
  win.webContents.emit("will-prevent-unload", event);
  assert.equal(allowed, true, "Electron unload is released only after explicit discard");
  assert.equal(h.main.sent.length, 0);
});

test("keeping edits cancels return navigation before the main friend or focus changes", async () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1]; win.guardUnload = true;
  assert.equal(await h.handlers.get("external-import:navigate")(h.event(win), "chat", true), false);
  assert.equal(win.destroyed, undefined);
  assert.equal(h.main.focused, undefined);
  assert.equal(h.main.sent.length, 0);
  h.dialog.choice = 1;
  assert.equal(await h.handlers.get("external-import:navigate")(h.event(win), "chat", true), true);
  assert.equal(win.destroyed, true);
  assert.equal(h.main.focused, true);
  assert.deepEqual(h.main.sent.at(-1), ["external-import:navigate", { target: "chat", scope: A }]);
});

test("app shutdown waits for each real unload guard and stops when a user keeps a window", async () => {
  const h = harness(); h.manager.open(A); h.manager.open(B);
  const clear = h.made[1], dirty = h.made[2]; dirty.guardUnload = true;
  const pending = h.manager.requestCloseAll();
  assert.equal(h.manager.open({ ...B, user: "new-contact" }), false, "opening is paused during close confirmation");
  assert.equal(await pending, false);
  assert.equal(clear.destroyed, true);
  assert.equal(dirty.destroyed, undefined);
  assert.equal(h.manager.count(), 1);
  assert.equal(h.main.sent.length, 0);
  h.dialog.choice = 1;
  assert.equal(await h.manager.requestCloseAll(), true);
  assert.equal(dirty.destroyed, true);
  assert.equal(h.manager.count(), 0);
});

test("theme, service and sanitized plugin state reach every independent import window", () => {
  const h = harness(); h.manager.open(A); h.manager.open(B);
  const win = h.made[1], notify = h.handlers.get("external-import:notify");
  h.manager.setTheme("dark");
  assert.equal(h.handlers.get("external-import:context")(h.event(win)).theme, "dark");
  assert.deepEqual(win.sent.at(-1), ["external-import:host-event", { type: "theme", theme: "dark" }]);
  h.manager.broadcast({ type: "service-restored" });
  assert.deepEqual(win.sent.at(-1), ["external-import:host-event", { type: "service-restored" }]);
  const plugins = { type: "plugins-changed", plugins: [{ id: "kyf.external-chat-import", enabled: true, path: "private", token: "private", incompatible: true }] };
  assert.equal(notify(h.event(h.main), plugins), true);
  for (const child of h.made.slice(1)) assert.deepEqual(child.sent.at(-1), ["external-import:host-event",
    { type: "plugins-changed", plugins: [{ id: "kyf.external-chat-import", enabled: false }] }]);
  assert.equal(notify(h.event(win), plugins), false);
  for (const value of [{ type: "theme", theme: "light" }, { type: "plugins-changed", plugins: [{}] },
    { type: "plugins-changed", plugins: [{ id: "../bad", enabled: true }] },
    { type: "plugins-changed", plugins: [{ id: "valid", enabled: "true" }] }])
    assert.equal(notify(h.event(h.main), value), false);
});

test("window navigation and popups are blocked while main reloads preserve fixed child drafts", () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1];
  assert.deepEqual(win.popups(), { action: "deny" });
  for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"]) {
    let prevented = false;
    win.webContents.emit(name, { preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
  }
  h.main.webContents.emit("did-start-navigation", {}, CLIENT, true, true);
  assert.equal(h.manager.count(), 1);
  win.webContents.emit("render-process-gone");
  assert.equal(h.manager.count(), 0);
  h.manager.open(A);
  const preserved = h.made.at(-1);
  h.main.webContents.emit("did-start-navigation", {}, CLIENT, false, true);
  assert.equal(h.manager.count(), 1);
  h.main.webContents.emit("render-process-gone");
  assert.equal(h.manager.count(), 1);
  assert.equal(preserved.destroyed, undefined);
  assert.deepEqual(h.handlers.get("external-import:context")(h.event(preserved)), A);
  const last = h.made.at(-1);
  h.main.destroy(); last.emit("ready-to-show");
  assert.equal(last.destroyed, true);
  assert.equal(last.shown, undefined);
  assert.equal(h.manager.count(), 0);
  assert.equal(h.handlers.size, 0);
  assert.equal(h.manager.open(A), false);
});

function loadPreload(file, { subframe = false, active = true, settings = false } = {}) {
  const hosts = {}, calls = [], listeners = new Map(), window = {};
  window.top = subframe ? {} : window;
  const navigator = { userActivation: { isActive: active } };
  vm.runInNewContext(fs.readFileSync(require.resolve(file), "utf8"), {
    require: () => ({ contextBridge: { exposeInMainWorld: (name, api) => { hosts[name] = api; } },
      ipcRenderer: { on: (name, listener) => listeners.set(name, listener), removeListener: name => listeners.delete(name),
        invoke: (...args) => { calls.push(args); return Promise.resolve(true); }, send() {} } }),
    window, navigator, process: { env: {}, argv: settings ? ["--kyf-settings-window"] : [] }, document: { addEventListener() {} },
  });
  return { hosts, calls, listeners, navigator, window };
}

test("main preload gates opening to the activated main UI and offers a narrow event relay", async () => {
  const h = loadPreload("../scripts/real-client-preload.cjs");
  assert.equal(await h.hosts.desktopHost.openExternalImportWindow(A), true);
  assert.deepEqual(h.calls.at(-1), ["external-import:open", A, true]);
  h.navigator.userActivation.isActive = false;
  assert.equal(await h.hosts.desktopHost.openExternalImportWindow(A), false);
  h.navigator.userActivation.isActive = true; h.window.top = {};
  assert.equal(await h.hosts.desktopHost.openExternalImportWindow(A), false);
  assert.equal(await h.hosts.desktopHost.notifyExternalImportWindows({ type: "plugins-changed", plugins: [] }), false);
  h.window.top = h.window;
  assert.equal(await h.hosts.desktopHost.notifyExternalImportWindows({ type: "arbitrary" }), false);
  await h.hosts.desktopHost.notifyExternalImportWindows({ type: "plugins-changed", plugins: [] });
  assert.equal(h.calls.length, 2);
  let received;
  const unsubscribe = h.hosts.desktopHost.onExternalImportNavigate(value => { received = value; });
  h.listeners.get("external-import:navigate")({}, { target: "arbitrary", scope: A });
  assert.equal(received, undefined);
  h.listeners.get("external-import:navigate")({}, { target: "chat", scope: A });
  assert.equal(received.scope, A);
  unsubscribe();
  assert.equal(h.listeners.has("external-import:navigate"), false);
  const settings = loadPreload("../scripts/real-client-preload.cjs", { settings: true });
  assert.equal(await settings.hosts.desktopHost.openExternalImportWindow(A), false);
  assert.equal(await settings.hosts.desktopHost.notifyExternalImportWindows({ type: "plugins-changed", plugins: [] }), false);
});

test("child preload exposes only scoped context, close, explicit navigation and host events", async () => {
  const h = loadPreload("../scripts/external-import-preload.cjs"), api = h.hosts.externalImportHost;
  assert.deepEqual(Object.keys(h.hosts), ["externalImportHost"]);
  assert.deepEqual(Object.keys(api), ["getContext", "close", "navigate", "onHostEvent"]);
  assert.equal(Object.isFrozen(api), true);
  await api.getContext(); await api.navigate("plugins");
  assert.deepEqual(h.calls.map(value => value[0]), ["external-import:context", "external-import:navigate"]);
  assert.equal(await api.navigate("https://example.test"), false);
  h.navigator.userActivation.isActive = false;
  assert.equal(await api.navigate("chat"), false);
  let received;
  const unsubscribe = api.onHostEvent(value => { received = value; });
  h.listeners.get("external-import:host-event")({}, { type: "arbitrary" });
  assert.equal(received, undefined);
  h.listeners.get("external-import:host-event")({}, { type: "plugins-changed", plugins: [] });
  assert.equal(received.type, "plugins-changed");
  unsubscribe();
  assert.equal(h.listeners.size, 0);
  await api.close();
  h.window.top = {};
  assert.equal(await api.getContext(), null);
  assert.equal(await api.close(), false);
  assert.equal(h.calls.length, 3);
});

test("portable staging, desktop packaging and update verification include both window scripts", () => {
  const root = path.resolve(__dirname, ".."), read = file => fs.readFileSync(path.join(root, file), "utf8");
  const builder = JSON.parse(read("electron-builder.real-client.json"));
  const tuple = (source, name) => {
    const body = source.match(new RegExp(`^${name} = \\(([\\s\\S]*?)^\\)`, "m"))?.[1];
    assert.ok(body, `${name} allowlist exists`);
    return [...body.matchAll(/"([^"\n]+)"/g)].map(match => match[1]);
  };
  const stage = tuple(read("scripts/stage-real-client.py"), "SCRIPTS"), release = tuple(read("scripts/build-windows-release.py"), "ASAR_SCRIPTS");
  for (const name of ["external-import-windows.cjs", "external-import-preload.cjs"]) {
    const relative = `scripts/${name}`;
    assert.equal(fs.existsSync(path.join(root, relative)), true);
    assert.ok(builder.files.includes(relative));
    assert.ok(stage.includes(name));
    assert.ok(release.includes(relative));
  }
});

async function shellLifecycle() {
  const app = new EventEmitter(), main = new EventEmitter(), contents = new EventEmitter(), handlers = new Map();
  const state = { windows: 1, allow: false, guarded: 0, mainClose: 0, quit: 0, monitorStopped: 0, startupErrors: [] };
  contents.mainFrame = { url: CLIENT };
  contents.setWindowOpenHandler = () => {}; contents.loadURL = () => Promise.resolve(); contents.send = () => {};
  Object.assign(main, { webContents: contents, isDestroyed: () => false,
    close: () => { state.mainClose++; }, setTitleBarOverlay() {} });
  Object.assign(app, { isPackaged: true, setAppUserModelId() {}, setPath() {}, getVersion: () => "1.0.1",
    requestSingleInstanceLock: () => true, commandLine: { appendSwitch() {} }, whenReady: () => Promise.resolve(),
    quit: () => { state.quit++; }, exit: value => { state.startupErrors.push(value); } });
  const external = { count: () => state.windows, close() {}, broadcast() {}, setTheme() {}, requestCloseAll: async () => {
    state.guarded++; if (state.allow) state.windows = 0; return state.allow;
  } };
  const disposable = { close() {}, broadcast() {}, setTheme() {}, count: () => 0, destroyAll() {}, trusted: () => false };
  const electron = { app, BrowserWindow: class { constructor() { return main; } }, WebContentsView: class {},
    ipcMain: { on() {}, handle: (name, handler) => handlers.set(name, handler) }, screen: {}, clipboard: {}, shell: {},
    dialog: { showErrorBox: (_title, value) => state.startupErrors.push(value) }, session: { defaultSession: {
      setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {}, webRequest: { onBeforeRequest() {} },
    } } };
  const moduleMocks = {
    // Keep sender lifecycle independent from these external-window close guards.
    "./chat-link-host.cjs": require("../scripts/chat-link-host.cjs"),
    "./real-client-recovery.cjs": { monitorBridge: () => () => { state.monitorStopped++; } },
    "./real-client-update.cjs": { RELEASES_URL: "https://example.invalid/releases" },
    "./real-client-update-proxy.cjs": { createUpdateProxyFetch: () => ({}) },
    "./model-traces-host.cjs": { readControlToken: () => null, attachModelTraces: () => disposable },
    "./external-import-windows.cjs": { createExternalImportWindows: () => external },
    "./history-windows.cjs": { createHistoryWindows: () => disposable },
    "./friend-windows.cjs": { createFriendWindows: () => disposable },
    "./settings-window.cjs": { createSettingsWindow: () => disposable },
    "./self-analysis-window.cjs": { createSelfAnalysisWindow: () => disposable },
    "./image-preview.cjs": { createImagePreviews: () => disposable },
    "./model-traces-window.cjs": { createModelTracesWindow: () => disposable },
    "./plugin-windows.cjs": { createPluginWindows: () => disposable },
    "./real-client-update-controller.cjs": { createUpdateController: options => { state.updater = options; return {}; } },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../scripts/real-client-shell.cjs"), "utf8"), {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:fs") return { mkdirSync() {}, readFileSync(file) {
        assert.equal(path.resolve(file), path.resolve(__dirname, "..", "package.json"));
        return '{"kyfUpdatesEnabled":true}';
      } };
      if (name === "node:path") return path;
      if (name === "node:child_process") return { execFile() { throw new Error("a cancelled close must not stop the bridge"); } };
      if (moduleMocks[name]) return moduleMocks[name];
      if (name.endsWith(path.join("node_modules", "undici"))) return { ProxyAgent: class {} };
      throw new Error(`Unexpected require: ${name}`);
    }, __dirname: path.dirname(require.resolve("../scripts/real-client-shell.cjs")), URL, setImmediate,
    process: { platform: "win32", env: { KYF_INSTANCE_ID: "a".repeat(64) },
      argv: ["electron", "shell", "--client-url", CLIENT], stderr: { write() {} } },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(state.startupErrors, []);
  const trusted = { sender: contents, senderFrame: contents.mainFrame };
  return { state, app, main, handlers, trusted };
}

test("main close, exit and app quit remain open when an external window keeps unsaved work", async () => {
  for (const action of ["main-close", "exit-ipc", "before-quit"]) {
    const h = await shellLifecycle();
    if (action === "exit-ipc") assert.equal(await h.handlers.get("real-client:exit-app")(h.trusted), false);
    else {
      let prevented = false;
      (action === "main-close" ? h.main : h.app).emit(action === "main-close" ? "close" : "before-quit",
        { preventDefault() { prevented = true; } });
      assert.equal(prevented, true);
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal(h.state.guarded, 1, action);
    assert.equal(h.state.windows, 1, action);
    assert.equal(h.state.mainClose, 0, action);
    assert.equal(h.state.quit, 0, action);
    assert.equal(h.state.monitorStopped, 0, action);
  }
});

test("updater cannot stop the service until external windows accept closing", async () => {
  const h = await shellLifecycle();
  await assert.rejects(h.state.updater.pauseRecovery(), /更新已取消/);
  assert.equal(h.state.windows, 1);
  assert.equal(h.state.monitorStopped, 0);
  h.state.updater.resumeRecovery();
  h.state.allow = true;
  await h.state.updater.pauseRecovery();
  assert.equal(h.state.windows, 0);
  assert.equal(h.state.monitorStopped, 1);
});
