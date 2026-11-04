// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// G13: Core opens the main window for an Agent with `--open-module settings`, both at startup
// and through the single-instance `second-instance` event. Synthetic, no Electron.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const CLIENT = "http://127.0.0.1:34567/";

async function shell(argv) {
  const app = new EventEmitter(), main = new EventEmitter(), contents = new EventEmitter();
  const state = { opened: [], chats: [], focused: 0, restored: 0, startupErrors: [] };
  contents.mainFrame = { url: CLIENT };
  contents.setWindowOpenHandler = () => {}; contents.loadURL = () => Promise.resolve(); contents.send = () => {};
  Object.assign(main, { webContents: contents, isDestroyed: () => false, close() {}, setTitleBarOverlay() {},
    isMinimized: () => true, restore: () => { state.restored++; }, focus: () => { state.focused++; } });
  Object.assign(app, { isPackaged: false, setAppUserModelId() {}, setPath() {}, getVersion: () => "1.0.0",
    requestSingleInstanceLock: () => true, commandLine: { appendSwitch() {} }, whenReady: () => Promise.resolve(),
    quit() {}, exit: value => { state.startupErrors.push(value); } });
  const disposable = { close() {}, broadcast() {}, setTheme() {}, count: () => 0, destroyAll() {}, trusted: () => false };
  const settings = { ...disposable, open: value => { state.opened.push(value); return true; } };
  const chats = { ...disposable, open: value => { state.chats.push(value); return true; } };
  const electron = { app, BrowserWindow: class { constructor() { return main; } }, WebContentsView: class {},
    ipcMain: { on() {}, handle() {} }, screen: {}, clipboard: {}, shell: {},
    dialog: { showErrorBox: (_title, value) => state.startupErrors.push(value) }, session: { defaultSession: {
      setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {}, webRequest: { onBeforeRequest() {} },
    } } };
  const mocks = {
    "./wechat-send-host.cjs": { attachWechatSend: () => ({ close() {}, reset() {} }) },
    "./wechat-sdk-host.cjs": { attachWechatSdk: () => ({ close() {} }) },
    "./chat-link-host.cjs": require("../scripts/chat-link-host.cjs"),
    "./real-client-recovery.cjs": { monitorBridge: () => () => {} },
    "./real-client-update.cjs": { RELEASES_URL: "https://example.invalid/releases" },
    "./real-client-update-proxy.cjs": { createUpdateProxyFetch: () => ({}) },
    "./model-traces-host.cjs": { readControlToken: () => null, attachModelTraces: () => disposable },
    "./external-import-windows.cjs": { createExternalImportWindows: () => disposable },
    "./history-windows.cjs": { createHistoryWindows: () => disposable },
    "./friend-windows.cjs": { createFriendWindows: () => disposable },
    "./settings-window.cjs": { createSettingsWindow: () => settings },
    "./self-analysis-window.cjs": { createSelfAnalysisWindow: () => disposable },
    "./image-preview.cjs": { createImagePreviews: () => disposable },
    "./model-traces-window.cjs": { createModelTracesWindow: () => disposable },
    "./plugin-windows.cjs": { createPluginWindows: () => disposable },
    "./chat-windows.cjs": { createChatWindows: () => chats },
    "./real-client-update-controller.cjs": { createUpdateController: () => ({}) },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../scripts/real-client-shell.cjs"), "utf8"), {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:fs") return { mkdirSync() {}, readFileSync() { return "{}"; } };
      if (name === "node:path") return path;
      if (name === "node:child_process") return { execFile() {} };
      if (mocks[name]) return mocks[name];
      if (name.endsWith(path.join("node_modules", "undici"))) return { ProxyAgent: class {} };
      throw new Error(`Unexpected require: ${name}`);
    }, __dirname: path.dirname(require.resolve("../scripts/real-client-shell.cjs")), URL, setImmediate,
    setTimeout, clearTimeout,
    process: { platform: "win32", env: { KYF_INSTANCE_ID: "a".repeat(64) },
      argv: ["electron", "shell", "--client-url", CLIENT, ...argv], stderr: { write() {} } },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(state.startupErrors, []);
  return { state, app, contents };
}

test("--open-module settings at startup opens settings once the main page loaded", async () => {
  const h = await shell(["--open-module", "settings"]);
  assert.deepEqual(h.state.opened, []);
  h.contents.emit("did-finish-load");
  h.contents.emit("did-finish-load");
  assert.equal(JSON.stringify(h.state.opened), JSON.stringify([{ tab: "general" }]));
});

test("a second instance focuses the main window and opens settings only when asked", async () => {
  const h = await shell([]);
  h.contents.emit("did-finish-load");
  assert.deepEqual(h.state.opened, []);
  h.app.emit("second-instance", {}, ["electron", "shell", "--client-url", CLIENT]);
  assert.equal(h.state.focused, 1);
  assert.deepEqual(h.state.opened, []);
  h.app.emit("second-instance", {}, ["electron", "shell", "--client-url", CLIENT, "--open-module", "settings"]);
  assert.equal(h.state.focused, 2);
  assert.equal(h.state.restored, 2);
  assert.equal(JSON.stringify(h.state.opened), JSON.stringify([{ tab: "general" }]));
  // Unknown modules (including the never-openable model settings) are ignored.
  h.app.emit("second-instance", {}, ["electron", "shell", "--open-module", "models"]);
  assert.equal(h.state.opened.length, 1);
});

test("the legacy chat arguments cannot open a normal popout at startup or in a second instance", async () => {
  const chat = ["--open-module", "chat", "--chat-account=synthetic-account", "--chat-user=wxid_synthetic"];
  const h = await shell(chat);
  h.contents.emit("did-finish-load");
  h.app.emit("second-instance", {}, ["electron", "shell", "--client-url", CLIENT, ...chat]);
  assert.deepEqual(h.state.chats, []);
  assert.deepEqual(h.state.opened, []);
});
