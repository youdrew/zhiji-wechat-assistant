// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic host tests for independent chat windows, with no real account, messages, bridge or native windows.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { createChatWindows, chatScope } = require("../scripts/chat-windows.cjs");
const CLIENT = "http://127.0.0.1:34567/";
const A = { account: "synthetic-account", user: "wxid_a" };
const G = { account: "synthetic-account", user: "123@chatroom" };
const THEMES = { dark: { color: "#1b1b1b", symbolColor: "#e6e7eb", height: 36 },
  light: { color: "#edf3f7", symbolColor: "#28333d", height: 36 } };

function harness({ userDataPath, displays } = {}) {
  const made = [], handlers = new Map(), loads = [];
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.sent = []; this.overlays = []; this.backgrounds = [];
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = { url: "" };
      this.webContents.setWindowOpenHandler = value => { this.popups = value; };
      this.webContents.send = (...value) => this.sent.push(value);
      this.webContents.isDestroyed = () => !!this.destroyed;
      this.bounds = { x: options.x ?? 100, y: options.y ?? 80, width: options.width ?? 1180, height: options.height ?? 780 };
      made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    isMaximized() { return false; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = (this.focused || 0) + 1; }
    getBounds() { return this.bounds; }
    setTitleBarOverlay(value) { this.overlays.push(value); }
    setBackgroundColor(value) { this.backgrounds.push(value); }
    destroy() { if (this.destroyed) return; this.destroyed = true; this.emit("closed"); }
    close() { this.destroy(); }
    loadURL(value) { this.url = value; this.webContents.mainFrame.url = value; loads.push(value); return Promise.resolve(); }
  }
  const main = new Window({});
  main.webContents.mainFrame.url = CLIENT;
  main.webContents.session = { synthetic: true };
  const trustedMainFrame = event => event.sender === main.webContents && event.senderFrame === main.webContents.mainFrame &&
    event.senderFrame.url === CLIENT;
  const area = { x: 0, y: 0, width: 1920, height: 1040 };
  const manager = createChatWindows({ BrowserWindow: Window, mainWindow: main, clientUrl: CLIENT, trustedMainFrame,
    themes: THEMES, userDataPath, root: "C:\\synthetic-root", instanceId: "a".repeat(64),
    ipcMain: { handle: (name, callback) => handlers.set(name, callback), removeHandler: name => handlers.delete(name) },
    screen: { getDisplayMatching: () => ({ workArea: area }), getAllDisplays: () => displays || [{ workArea: area }],
      getPrimaryDisplay: () => ({ workArea: area }) } });
  const event = win => ({ sender: win.webContents, senderFrame: win.webContents.mainFrame });
  return { manager, main, made, handlers, loads, event };
}

test("a chat window loads the main page with ?window=chat, the main preload and a hidden themed title bar", () => {
  const h = harness();
  assert.equal(h.handlers.get("chat-window:open")(h.event(h.main), A), true);
  const child = h.made[1];
  const target = new URL(child.url);
  assert.equal(target.origin, new URL(CLIENT).origin);
  assert.equal(target.pathname, "/index.html");
  assert.equal(target.searchParams.get("window"), "chat");
  assert.equal(target.searchParams.get("account"), A.account);
  assert.equal(target.searchParams.get("user"), A.user);
  assert.equal([...target.searchParams.keys()].length, 3);
  assert.equal(child.options.width, 640);
  assert.equal(child.options.height, 720);
  assert.equal(child.options.minWidth, 420);
  assert.equal(child.options.minHeight, 480);
  assert.equal(child.options.titleBarStyle, "hidden");
  assert.deepEqual(child.options.titleBarOverlay, THEMES.dark);
  assert.match(child.options.icon, /app-icon\.ico$/);
  const prefs = child.options.webPreferences;
  assert.match(prefs.preload, /real-client-preload\.cjs$/);
  assert.deepEqual(prefs.additionalArguments, ["--kyf-chat-window"]);
  assert.equal(prefs.session, h.main.webContents.session);
  assert.equal(prefs.sandbox, true);
  assert.equal(prefs.contextIsolation, true);
  assert.equal(prefs.nodeIntegration, false);
  assert.equal(prefs.devTools, false);
  assert.equal(child.options.parent, undefined);
  // Cascaded from the main window.
  assert.ok(child.options.x > h.main.bounds.x && child.options.y > h.main.bounds.y);
  // Identifiers that look like other parameters stay one value.
  h.manager.open({ account: A.account, user: "user&window=settings#x" });
  assert.equal(new URL(h.made[2].url).searchParams.get("user"), "user&window=settings#x");
  assert.equal(new URL(h.made[2].url).searchParams.get("window"), "chat");
  assert.ok(h.made[2].options.x > child.options.x);
});

test("one window per account and conversation; reopening focuses and restores it", () => {
  const h = harness(), open = h.handlers.get("chat-window:open");
  assert.equal(open(h.event(h.main), A), true);
  const first = h.made[1];
  first.minimized = true;
  assert.equal(open(h.event(h.main), { ...A }), true);
  assert.equal(h.made.length, 2);
  assert.equal(first.minimized, false);
  assert.equal(first.focused, 1);
  assert.equal(open(h.event(h.main), G), true, "groups are conversations too");
  assert.equal(open(h.event(h.main), { ...A, account: "another-account" }), true);
  assert.equal(h.manager.count(), 3);
  first.destroy();
  assert.equal(h.manager.count(), 2);
  assert.equal(open(h.event(h.main), A), true);
  assert.equal(h.made.length, 5);
});

test("opening requires the trusted main frame and validated identifiers", () => {
  const h = harness(), open = h.handlers.get("chat-window:open");
  for (const value of [null, [], {}, "x", { account: "", user: "u" }, { account: "a", user: "" },
    { account: "x".repeat(129), user: "u" }, { account: "a", user: "x".repeat(257) },
    { account: "a\nb", user: "u" }, { account: "a", user: "u\u007f" }, { account: 1, user: "u" }])
    assert.equal(open(h.event(h.main), value), false);
  assert.equal(open({ sender: h.main.webContents, senderFrame: { url: CLIENT } }, A), false);
  assert.equal(open({ sender: {}, senderFrame: h.main.webContents.mainFrame }, A), false);
  h.main.webContents.mainFrame.url = CLIENT + "?window=settings";
  assert.equal(open(h.event(h.main), A), false);
  h.main.webContents.mainFrame.url = CLIENT;
  assert.equal(h.made.length, 1);
  // A chat window cannot pop out further windows.
  assert.equal(open(h.event(h.main), A), true);
  assert.equal(open(h.event(h.made[1]), G), false);
  assert.deepEqual(chatScope({ ...A, url: "file:///x", name: "n" }), A);
  assert.equal(chatScope({ account: "x".repeat(128), user: "y".repeat(256) }).user.length, 256);
});

test("trusted() matches only the exact page in the chat window's main frame", () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1], trusted = h.event(win);
  assert.equal(h.manager.trusted(trusted), true);
  assert.equal(h.manager.senderWindow(trusted), win);
  assert.equal(h.manager.trusted(h.event(h.main)), false);
  assert.equal(h.manager.trusted({ ...trusted, senderFrame: { url: win.url } }), false, "a subframe is not trusted");
  assert.equal(h.manager.trusted({ sender: {}, senderFrame: win.webContents.mainFrame }), false);
  win.webContents.mainFrame.url = win.url + "&user=other";
  assert.equal(h.manager.trusted(trusted), false);
  assert.equal(h.manager.senderWindow(trusted), null);
  win.webContents.mainFrame.url = win.url;
  win.destroy();
  assert.equal(h.manager.trusted(trusted), false);
});

test("popups, navigation and webviews are denied; only plugin-page subframes may load", () => {
  const h = harness(); h.manager.open(A);
  const win = h.made[1];
  assert.deepEqual(win.popups(), { action: "deny" });
  for (const name of ["will-navigate", "will-redirect", "will-attach-webview"]) {
    let prevented = false;
    win.webContents.emit(name, { preventDefault() { prevented = true; } }, CLIENT);
    assert.equal(prevented, true, name);
  }
  const frame = (isMainFrame, url) => {
    let prevented = false;
    win.webContents.emit("will-frame-navigate", { isMainFrame, url, preventDefault() { prevented = true; } });
    return prevented;
  };
  assert.equal(frame(false, CLIENT + "plugin-pages/kyf.example/ui/index.html"), false);
  assert.equal(frame(false, CLIENT + "api/sessions"), true);
  assert.equal(frame(false, "https://example.invalid/"), true);
  assert.equal(frame(true, CLIENT + "plugin-pages/kyf.example/ui/index.html"), true);
  // A crashed renderer closes its window.
  win.webContents.emit("render-process-gone");
  assert.equal(win.destroyed, true);
  assert.equal(h.manager.count(), 0);
});

test("the main theme recolors every chat window; a chat page recolors only itself", () => {
  const h = harness(); h.manager.open(A); h.manager.open(G);
  const [, first, second] = h.made;
  h.manager.setTheme("light");
  for (const win of [first, second]) {
    assert.deepEqual(win.overlays.at(-1), THEMES.light);
    assert.equal(win.backgrounds.at(-1), THEMES.light.color);
  }
  h.manager.open({ ...A, user: "wxid_c" });
  assert.deepEqual(h.made[3].options.titleBarOverlay, THEMES.light, "new windows start with the current theme");
  assert.equal(h.manager.applyTheme(h.event(first), "dark"), true);
  assert.deepEqual(first.overlays.at(-1), THEMES.dark);
  assert.deepEqual(second.overlays.at(-1), THEMES.light);
  assert.equal(h.manager.applyTheme(h.event(first), "neon"), false);
  assert.equal(h.manager.applyTheme(h.event(h.main), "dark"), false);
  h.manager.setTheme("neon");
  assert.deepEqual(second.overlays.at(-1), THEMES.light);
});

test("broadcast reaches each chat window; the windows close with the main window", () => {
  const h = harness(); h.manager.open(A); h.manager.open(G);
  h.manager.broadcast("real-client:bridge-restored");
  assert.deepEqual(h.made[1].sent.at(-1), ["real-client:bridge-restored", undefined]);
  assert.deepEqual(h.made[2].sent.at(-1), ["real-client:bridge-restored", undefined]);
  assert.deepEqual(h.manager.contents(), [h.made[1].webContents, h.made[2].webContents]);
  h.main.destroy();
  assert.equal(h.made[1].destroyed && h.made[2].destroyed, true);
  assert.equal(h.manager.count(), 0);
  assert.equal(h.handlers.has("chat-window:open"), false);
  assert.equal(h.manager.open(A), false);
});

test("a main crash or full navigation closes chat windows; an in-place navigation does not", () => {
  for (const trigger of [h => h.main.webContents.emit("render-process-gone"),
    h => h.main.webContents.emit("did-start-navigation", {}, CLIENT, false, true)]) {
    const h = harness(); h.manager.open(A);
    h.main.webContents.emit("did-start-navigation", {}, CLIENT, true, true);
    h.main.webContents.emit("did-start-navigation", {}, CLIENT + "plugin-pages/x", false, false);
    assert.equal(h.manager.count(), 1);
    const win = h.made[1]; trigger(h); win.emit("ready-to-show");
    assert.equal(win.destroyed, true);
    assert.equal(win.shown, undefined);
    assert.equal(h.manager.count(), 0);
    h.manager.close();
  }
});

test("bounds are saved per conversation and reused only while visible", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "kyf-chat-windows-"));
  try {
    const h = harness({ userDataPath: directory }); h.manager.open(A);
    const win = h.made[1];
    win.bounds = { x: 300, y: 200, width: 700, height: 650 };
    win.emit("move");
    await new Promise(resolve => setTimeout(resolve, 350));
    const file = path.join(directory, "chat-windows.json");
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8"))[JSON.stringify([A.account, A.user])], win.bounds);
    h.manager.close();
    const next = harness({ userDataPath: directory }); next.manager.open(A); next.manager.open(G);
    assert.equal(next.made[1].options.x, 300);
    assert.equal(next.made[1].options.width, 700);
    assert.equal(next.made[2].options.width, 640, "another conversation keeps its own (default) bounds");
    next.manager.close();
    const offscreen = harness({ userDataPath: directory, displays: [{ workArea: { x: 5000, y: 5000, width: 800, height: 600 } }] });
    offscreen.manager.open(A);
    assert.notEqual(offscreen.made[1].options.x, 300);
    offscreen.manager.close();
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("normal popouts leave privileged trace operations to the separate viewer", async () => {
  const h = harness(); h.manager.open(A);
  assert.equal(h.handlers.has("model-traces:request"), false);
  const chat = loadPreload(["--kyf-chat-window"]);
  for (const operation of ["records", "usage-clear", "clear"]) {
    assert.equal((await chat.host.modelTracesRequest(operation, {})).error, "trace-forbidden");
  }
  assert.equal(chat.calls.length, 0);
  assert.equal(await chat.host.openModelTracesWindow({ ...A, view: "chat" }), true);
  assert.equal(chat.calls[0][0], "model-traces-window:open");
});

function loadPreload(argv = []) {
  const hosts = {}, calls = [];
  const window = {}; window.top = window;
  vm.runInNewContext(fs.readFileSync(require.resolve("../scripts/real-client-preload.cjs"), "utf8"), {
    require: () => ({ contextBridge: { exposeInMainWorld: (name, api) => { hosts[name] = api; } },
      ipcRenderer: { on() {}, removeListener() {}, invoke: (...args) => { calls.push(args); return Promise.resolve(true); },
        send() {} } }),
    window, navigator: { userActivation: { isActive: true } },
    process: { argv, env: { KYF_UPDATE_VALIDATE: "1" } }, document: { addEventListener() {} },
  });
  return { host: hosts.desktopHost, calls, window };
}

test("the main preload pops a conversation out; chat and settings windows cannot", async () => {
  const main = loadPreload();
  assert.equal(main.host.isChatWindow, false);
  assert.equal(await main.host.openChatWindow(A.account, A.user), true);
  assert.equal(JSON.stringify(main.calls.at(-1)), JSON.stringify(["chat-window:open", A]));
  assert.equal(await main.host.openChatWindow(A.account), false);
  main.window.top = {};
  assert.equal(await main.host.openChatWindow(A.account, A.user), false);
  assert.equal(main.calls.length, 1);
  const chat = loadPreload(["--kyf-chat-window", "--kyf-app-updates-enabled"]);
  assert.equal(chat.host.isChatWindow, true);
  assert.equal(chat.host.isSettingsWindow, false);
  assert.equal(chat.host.updateValidationMode, false, "a chat window never runs the update UI check");
  assert.equal(chat.host.updateFinalReadyMode, false);
  assert.equal(await chat.host.openChatWindow(A.account, A.user), false);
  const settings = loadPreload(["--kyf-settings-window"]);
  assert.equal(settings.host.isChatWindow, false);
  assert.equal(await settings.host.openChatWindow(A.account, A.user), false);
  assert.equal(chat.calls.length + settings.calls.length, 0);
});

test("portable and update packages include the chat window host", () => {
  const projectRoot = path.resolve(__dirname, "..");
  const builder = JSON.parse(fs.readFileSync(path.join(projectRoot, "electron-builder.real-client.json"), "utf8"));
  const stageSource = fs.readFileSync(path.join(projectRoot, "scripts/stage-real-client.py"), "utf8");
  const releaseSource = fs.readFileSync(path.join(projectRoot, "scripts/build-windows-release.py"), "utf8");
  const tuple = (source, name) => {
    const body = source.match(new RegExp(`^${name} = \\(([\\s\\S]*?)^\\)`, "m"))?.[1];
    assert.ok(body, `${name} runtime allowlist exists`);
    return [...body.matchAll(/"([^"\n]+)"/g)].map(match => match[1]);
  };
  assert.ok(builder.files.includes("scripts/chat-windows.cjs"));
  assert.ok(tuple(stageSource, "SCRIPTS").includes("chat-windows.cjs"));
  assert.ok(tuple(releaseSource, "ASAR_SCRIPTS").includes("scripts/chat-windows.cjs"));
});

test("at most MAX_WINDOWS chat windows are open; a further conversation is refused, an open one still focuses", () => {
  const { MAX_WINDOWS } = require("../scripts/chat-windows.cjs");
  const h = harness(), open = h.handlers.get("chat-window:open");
  for (let index = 0; index < MAX_WINDOWS; index++)
    assert.equal(open(h.event(h.main), { account: A.account, user: `wxid_${index}` }), true);
  assert.equal(h.manager.count(), MAX_WINDOWS);
  const focused = h.made.map(win => win.focused || 0);
  assert.equal(open(h.event(h.main), { account: A.account, user: "wxid_extra" }), false);
  assert.equal(h.made.length, MAX_WINDOWS + 1);
  assert.deepEqual(h.made.map(win => win.focused || 0), focused, "a refused open focuses nothing");
  assert.equal(open(h.event(h.main), { account: A.account, user: "wxid_0" }), true);
  h.made[1].destroy();
  assert.equal(open(h.event(h.main), { account: A.account, user: "wxid_extra" }), true);
});
