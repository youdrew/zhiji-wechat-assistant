"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic checks for the Agent-openable window host (scripts/ui-host.cjs): request validation, the
// restricted hash, the request filter and session header, and the stdin/stdout protocol over fake
// Electron objects. No Electron window or Core is launched.
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const host = require("./ui-host.cjs");

const CORE = "http://127.0.0.1:8805";
const KEY = "a1b2c3d4e5f60718";
const TOKEN = "tok_" + "x".repeat(40);
const scope = { account: "synthetic-account", user: "wxid_friend_a", name: "朋友" };
function openOp(overrides = {}) {
  return { op: "open", key: KEY, reqId: "req-1", surface: "friend", url: CORE + "/friend.html",
    hash: { ...scope }, token: TOKEN, ...overrides };
}

function validation() {
  const request = host.validateOpenRequest(openOp({ tab: "persona", theme: "light" }));
  assert.equal(request.coreOrigin, CORE);
  assert.equal(request.page, CORE + "/friend.html");
  assert.equal(request.title, "朋友 · 知己分析");
  assert.deepEqual([request.tab, request.theme], ["persona", "light"]);
  assert.equal(host.validateOpenRequest(openOp({ url: "http://localhost:9000/friend.html" })).coreOrigin,
    "http://localhost:9000");
  // The hash may also arrive as the same query-string form friend-windows.cjs builds.
  assert.deepEqual(host.validateOpenRequest(openOp({ hash: "account=synthetic-account&user=wxid_friend_a" })).scope,
    { account: "synthetic-account", user: "wxid_friend_a", name: "wxid_friend_a" });
  const rejected = {
    "remote core": { url: "http://192.168.1.2:8805/friend.html" },
    "https core": { url: "https://127.0.0.1:8805/friend.html" },
    "no port": { url: "http://127.0.0.1/friend.html" },
    "other page": { url: CORE + "/index.html" },
    "query": { url: CORE + "/friend.html?x=1" },
    "fragment": { url: CORE + "/friend.html#account=a" },
    "credentials": { url: "http://u:p@127.0.0.1:8805/friend.html" },
    "lookalike host": { url: "http://127.0.0.1.evil.test:8805/friend.html" },
    "upper hex key": { key: "A1B2C3D4E5F60718" },
    "short key": { key: "abc" },
    "path key": { key: "../a1b2c3d4" },
    "missing reqId": { reqId: undefined },
    "surface": { surface: "settings" },
    "short token": { token: "abc" },
    "token with space": { token: "a".repeat(20) + " b" },
    "group chat": { hash: { account: "a", user: "123@chatroom" } },
    "missing user": { hash: { account: "a" } },
    "unknown tab": { tab: "admin" },
    "theme": { theme: "blue" },
    "hash theme": { theme: "SYSTEM" },
    "control title": { title: "a\nb" },
    "bad avatar": { hash: { ...scope, avatar: "javascript:alert(1)" } },
  };
  for (const [name, overrides] of Object.entries(rejected))
    assert.equal(host.validateOpenRequest(openOp(overrides)), null, name);
  assert.equal(host.validateOpenRequest({ ...openOp(), op: "close" }), null);
  assert.equal(host.validateOpenRequest(null), null);
}

function hashFormat() {
  // Same parameter format as friend-windows.cjs, plus restricted=1.
  const plain = new URLSearchParams({ account: scope.account, user: scope.user, name: scope.name }).toString();
  assert.equal(host.buildHash(scope), plain + "&restricted=1");
  const full = new URLSearchParams(host.buildHash({ ...scope, avatar: "https://img.example/a.png" },
    { theme: "light", tab: "memory" }));
  assert.deepEqual([...full.keys()], ["account", "user", "name", "avatar", "restricted", "theme", "tab"]);
  assert.equal(full.get("restricted"), "1");
  assert.equal(new URLSearchParams(host.buildHash(scope, { tab: "evil", theme: "x" })).has("tab"), false);
  const request = host.validateOpenRequest(openOp());
  assert.equal(request.theme, "system", "Core may defer to the system theme");
  assert.equal(host.targetUrl({ ...request, theme: "dark" }), CORE + "/friend.html#" + plain + "&restricted=1&theme=dark");
  assert.equal(host.targetUrl(request), CORE + "/friend.html#" + plain + "&restricted=1", "unresolved system is never written");
  // Core's own shape: a urlencoded hash string, sha256 key, zus_ token and system theme.
  const core = host.validateOpenRequest({ op: "open", key: "f".repeat(64), reqId: "0123456789abcdef", surface: "friend",
    url: CORE + "/friend.html", hash: "account=synthetic-account&user=wxid_friend_a&name=%E6%9C%8B%E5%8F%8B",
    title: "朋友 · 知己分析", tab: "trend", token: "zus_" + "0".repeat(64), theme: "system" });
  assert.equal(core.scope.name, "朋友");
}

function requestFilter() {
  const allowed = [
    [CORE + "/friend.html", "mainFrame"], [CORE + "/api/friend/overview?account=a", "xhr"],
    [CORE + "/plugin-pages/kyf.friend-analysis/ui/index.html", "subFrame"],
    ["data:image/png;base64,AAAA", "image"], ["blob:" + CORE + "/1234", "other"], ["devtools://devtools/x", "other"],
    ["https://wx.qlogo.cn/avatar.png", "image"],
  ];
  for (const [url, type] of allowed) assert.equal(host.isAllowedUrl(url, CORE, type), true, url);
  const blocked = [
    ["https://wx.qlogo.cn/avatar.png", "xhr"], ["https://evil.test/page.html", "mainFrame"],
    ["http://wx.qlogo.cn/avatar.png", "image"], ["http://127.0.0.1:9999/api/x", "xhr"],
    ["http://localhost:8805/api/x", "xhr"], ["file:///C:/Windows/win.ini", "other"],
    ["ws://127.0.0.1:8805/socket", "webSocket"], ["chrome-extension://abc/x", "script"], ["not a url", "other"],
  ];
  for (const [url, type] of blocked) assert.equal(host.isAllowedUrl(url, CORE, type), false, url);

  assert.deepEqual(host.headerFor(CORE + "/api/friend/overview", CORE, TOKEN), { "X-KYF-UI-Session": TOKEN });
  assert.equal(host.headerFor("https://wx.qlogo.cn/a.png", CORE, TOKEN), null);
  assert.equal(host.headerFor("http://127.0.0.1:9999/api", CORE, TOKEN), null);
  assert.equal(host.headerFor("garbage", CORE, TOKEN), null);
  // A page-written header never survives: dropped for other hosts, replaced for Core.
  assert.deepEqual(host.withSessionHeader({ Accept: "*/*", "x-kyf-ui-session": "forged" }, "https://wx.qlogo.cn/a.png",
    CORE, TOKEN), { Accept: "*/*" });
  assert.deepEqual(host.withSessionHeader({ "X-KYF-UI-Session": "forged" }, CORE + "/api", CORE, TOKEN),
    { "X-KYF-UI-Session": TOKEN });
  assert.equal(host.frameNavigationAllowed(CORE + "/plugin-pages/kyf.friend-analysis/ui/index.html", CORE), true);
  assert.equal(host.frameNavigationAllowed("https://evil.test/plugin-pages/x", CORE), false);
}

function argumentsAndLines() {
  assert.equal(host.userDataArgument(["electron", "ui-host.cjs", "--user-data", "D:/x/.local/ui-host"], {}),
    "D:/x/.local/ui-host");
  assert.equal(host.userDataArgument(["electron", "ui-host.cjs", "--user-data=D:/y"], {}), "D:/y");
  assert.equal(host.userDataArgument([], { KYF_UI_HOST_USER_DATA: "D:/z" }), "D:/z");
  assert.equal(host.userDataArgument([], {}), null);
  assert.deepEqual(host.parseLine("[1]"), { error: "bad-json" });
  assert.deepEqual(host.parseLine("{"), { error: "bad-json" });
  assert.deepEqual(host.parseLine(" ".repeat(70000)), { error: "line-too-long" });
  assert.deepEqual(host.parseLine('{"op":"quit"}'), { value: { op: "quit" } });
}

// -- fake Electron -----------------------------------------------------------------------------------------
class FakeContents extends EventEmitter {
  constructor() { super(); this.sent = []; this.openHandler = null; }
  send(channel, payload) { this.sent.push([channel, payload]); }
  setWindowOpenHandler(handler) { this.openHandler = handler; }
}
class FakeSession extends EventEmitter {
  constructor(partition) {
    super();
    this.partition = partition; this.cleared = 0;
    this.webRequest = { onBeforeRequest: fn => { this.beforeRequest = fn; }, onBeforeSendHeaders: fn => { this.beforeHeaders = fn; } };
  }
  setPermissionRequestHandler(fn) { this.permission = fn; }
  setPermissionCheckHandler(fn) { this.permissionCheck = fn; }
  setSpellCheckerEnabled(value) { this.spell = value; }
  clearStorageData() { this.cleared += 1; return Promise.resolve(); }
}
function fakeElectron(load = () => Promise.resolve()) {
  const created = [], sessions = new Map();
  class FakeWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options; this.webContents = new FakeContents(); this.destroyed = false; this.loaded = [];
      this.shown = 0; this.focused = 0; this.minimized = false;
      created.push(this);
    }
    loadURL(url) { this.loaded.push(url); return load(url); }
    isDestroyed() { return this.destroyed; }
    isMinimized() { return this.minimized; }
    restore() { this.minimized = false; }
    show() { this.shown += 1; }
    focus() { this.focused += 1; }
    setTitle(title) { this.title = title; }
    destroy() { if (this.destroyed) return; this.destroyed = true; this.emit("closed"); }
  }
  const session = { fromPartition(name) { if (!sessions.has(name)) sessions.set(name, new FakeSession(name)); return sessions.get(name); } };
  const screen = { getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }) };
  return { BrowserWindow: FakeWindow, session, screen, created, sessions };
}
function fakeHost(load) {
  const electron = fakeElectron(load), events = [], timers = new Map();
  let idle = 0, quit = 0, nextTimer = 1;
  const ui = host.createUiHost({ ...electron, emit: value => events.push(value), idleMs: 60000, systemDark: () => false,
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: id => timers.delete(id),
    onIdle: () => { idle += 1; }, onQuit: () => { quit += 1; } });
  const fire = () => { for (const [id, timer] of [...timers]) { timers.delete(id); timer.fn(); } };
  return { ui, electron, events, timers, fire, get idle() { return idle; }, get quit() { return quit; } };
}
const send = (h, value) => h.ui.handleLine(typeof value === "string" ? value : JSON.stringify(value));

function protocol() {
  const h = fakeHost();
  send(h, openOp({ tab: "trend" }));
  assert.equal(h.electron.created.length, 1);
  const window = h.electron.created[0];
  const prefs = window.options.webPreferences;
  assert.deepEqual([prefs.contextIsolation, prefs.sandbox, prefs.nodeIntegration, prefs.devTools],
    [true, true, false, false]);
  assert.match(prefs.preload, /friend-preload\.cjs$/);
  const ses = h.electron.sessions.get("uihost-" + KEY);
  assert.equal(prefs.session, ses, "in-memory partition uihost-<key>, never persist:");
  assert.equal(window.loaded[0], CORE + "/friend.html#" + host.buildHash(scope, { theme: "light", tab: "trend" }),
    "system theme resolves through the host (light here)");
  assert.match(window.loaded[0], /restricted=1/);
  assert.deepEqual(window.webContents.openHandler(), { action: "deny" });
  const prevented = name => { let stopped = false; window.webContents.emit(name, { preventDefault: () => { stopped = true; },
    isMainFrame: name === "will-frame-navigate" ? false : undefined, url: "https://evil.test/" }); return stopped; };
  for (const name of ["will-navigate", "will-redirect", "will-attach-webview", "will-frame-navigate"])
    assert.equal(prevented(name), true, name);
  // Request filter and session header installed on the window's own session.
  let decision;
  ses.beforeRequest({ url: "https://evil.test/x.js", resourceType: "script" }, value => { decision = value; });
  assert.deepEqual(decision, { cancel: true });
  ses.beforeRequest({ url: CORE + "/api/friend/overview", resourceType: "xhr" }, value => { decision = value; });
  assert.deepEqual(decision, { cancel: false });
  ses.beforeHeaders({ url: CORE + "/api/friend/overview", requestHeaders: { Accept: "*/*" } }, value => { decision = value; });
  assert.deepEqual(decision.requestHeaders, { Accept: "*/*", "X-KYF-UI-Session": TOKEN });
  ses.beforeHeaders({ url: "https://wx.qlogo.cn/a.png", requestHeaders: {} }, value => { decision = value; });
  assert.deepEqual(decision.requestHeaders, {});
  let permission;
  ses.permission(null, "media", value => { permission = value; });
  assert.equal(permission, false);
  let download = false;
  ses.emit("will-download", { preventDefault: () => { download = true; } });
  assert.equal(download, true, "downloads are refused");

  assert.deepEqual(h.events, []);
  window.emit("ready-to-show");
  assert.deepEqual(h.events.pop(), { event: "opened", key: KEY, reqId: "req-1" });
  assert.equal(window.shown, 1);

  // Same key again: focus, new token takes effect, no second window.
  send(h, openOp({ reqId: "req-2", token: "tok_" + "y".repeat(40), tab: "trend" }));
  assert.equal(h.electron.created.length, 1);
  assert.deepEqual(h.events.pop(), { event: "focused", key: KEY, reqId: "req-2" });
  ses.beforeHeaders({ url: CORE + "/api/x", requestHeaders: {} }, value => { decision = value; });
  assert.equal(decision.requestHeaders["X-KYF-UI-Session"], "tok_" + "y".repeat(40));
  assert.deepEqual(window.webContents.sent.slice(-2), [["friend:host-event", { type: "focus" }],
    ["friend:host-event", { type: "select-tab", tab: "trend" }]]);

  send(h, { op: "select-tab", key: KEY, tab: "memories" });
  assert.deepEqual(window.webContents.sent.at(-1), ["friend:host-event", { type: "select-tab", tab: "memories" }]);
  send(h, { op: "select-tab", key: KEY, tab: "settings" });
  assert.deepEqual(h.events.pop(), { event: "error", key: KEY, code: "invalid-request" });
  send(h, { op: "select-tab", key: "ffffffffffffffff", tab: "chat" });
  assert.deepEqual(h.events.pop(), { event: "error", key: "ffffffffffffffff", code: "unknown-key" });

  // Strict validation of every incoming line.
  send(h, "{not json");
  assert.deepEqual(h.events.pop(), { event: "error", code: "bad-json" });
  send(h, { op: "eval", key: KEY });
  assert.deepEqual(h.events.pop(), { event: "error", key: KEY, code: "unknown-op" });
  send(h, openOp({ key: "bbbbbbbbbbbbbbbb", reqId: "req-3", url: "http://evil.test:80/friend.html" }));
  assert.deepEqual(h.events.pop(), { event: "error", key: "bbbbbbbbbbbbbbbb", reqId: "req-3", code: "invalid-request" });
  send(h, { op: "open", key: "<script>", reqId: "x y" });
  assert.deepEqual(h.events.pop(), { event: "error", code: "invalid-request" }, "invalid ids are not echoed");
  assert.equal(h.electron.created.length, 1);

  // Idle exit only after the last window closes.
  assert.equal(h.timers.size, 0);
  send(h, { op: "close", key: KEY });
  assert.equal(window.destroyed, true);
  assert.deepEqual(h.events.pop(), { event: "closed", key: KEY });
  assert.equal(ses.cleared, 1, "the window's in-memory storage is cleared");
  assert.equal(h.ui.count(), 0);
  assert.deepEqual([...h.timers.values()].map(timer => timer.ms), [60000]);
  send(h, { op: "close", key: KEY });
  assert.deepEqual(h.events.pop(), { event: "error", key: KEY, code: "unknown-key" });
  // Reopening cancels the idle timer.
  send(h, openOp({ reqId: "req-4" }));
  assert.equal(h.timers.size, 0);
  const second = h.electron.created[1];
  second.webContents.emit("did-fail-load", {}, -105, "NAME_NOT_RESOLVED", CORE, true);
  assert.deepEqual(h.events.slice(-2), [{ event: "error", key: KEY, reqId: "req-4", code: "load-failed" },
    { event: "closed", key: KEY }]);
  h.fire();
  assert.equal(h.idle, 1);
  send(h, { op: "quit" });
  assert.equal(h.quit, 1);

  // Closing everything (stdin EOF) destroys open windows and ignores further lines.
  const g = fakeHost();
  send(g, openOp());
  send(g, openOp({ key: "cccccccccccccccc", hash: { account: "synthetic-account", user: "wxid_friend_b" } }));
  assert.equal(g.ui.count(), 2);
  g.ui.closeAll();
  assert.equal(g.electron.created.every(item => item.destroyed), true);
  const before = g.events.length;
  send(g, openOp({ key: "dddddddddddddddd" }));
  assert.equal(g.events.length, before);
  assert.equal(g.timers.size, 0);
}

async function chatProtocol() {
  const chat = { surface: "chat", url: CORE + "/history.html" };
  assert.equal(host.validateOpenRequest(openOp({ ...chat, tab: "chat" })), null);
  assert.equal(host.validateOpenRequest(openOp({ ...chat, url: CORE + "/friend.html" })), null);
  const h = fakeHost();
  send(h, openOp(chat));
  const [window] = h.electron.created;
  const { session } = window.options.webPreferences;
  assert.equal(window.options.webPreferences.preload, undefined);
  assert.equal(session.partition, `uihost-${KEY}-req-1`);
  assert.match(window.loaded[0], /\/history.html#.*restricted=1/);
  let result;
  session.beforeRequest({ url: "https://remote.example/avatar.png", resourceType: "image" }, value => { result = value; });
  assert.deepEqual(result, { cancel: true });
  session.beforeHeaders({ url: CORE + "/api/media", requestHeaders: { Range: "bytes=1-2" } }, value => { result = value; });
  assert.deepEqual(result.requestHeaders, { Range: "bytes=1-2", "X-KYF-UI-Session": TOKEN });
  let blocked = false;
  window.webContents.emit("will-frame-navigate", { url: CORE + "/plugin-pages/a", isMainFrame: false,
    preventDefault: () => { blocked = true; } });
  assert.equal(blocked, true);
  window.webContents.emit("did-navigate", {}, window.loaded[0], 200);
  window.emit("ready-to-show");
  assert.equal(window.shown, 0, "wait for a successful navigation as well as paint readiness");
  await Promise.resolve();
  assert.deepEqual(h.events, [{ event: "opened", key: KEY, reqId: "req-1" }]);
  send(h, openOp({ ...chat, reqId: "req-2" }));
  assert.deepEqual(h.events.at(-1), { event: "focused", key: KEY, reqId: "req-2" });
  const replacementToken = "tok_" + "y".repeat(40);
  send(h, openOp({ ...chat, reqId: "req-3", token: replacementToken }));
  const replacement = h.electron.created[1];
  assert.equal(window.destroyed, true);
  assert.notEqual(replacement.options.webPreferences.session, session);
  send(h, { op: "close", key: KEY, token: TOKEN });
  assert.equal(replacement.destroyed, false, "a delayed close must not close the new generation");
  send(h, { op: "close", key: KEY, token: replacementToken });
  assert.equal(replacement.destroyed, true);
  assert.deepEqual(h.events.at(-1), { event: "closed", key: KEY, token: replacementToken });

  let rejectLoad;
  const failing = fakeHost(() => new Promise((_resolve, reject) => { rejectLoad = reject; }));
  send(failing, openOp(chat));
  const [failed] = failing.electron.created;
  failed.emit("ready-to-show");
  rejectLoad(new Error("synthetic failed navigation"));
  await Promise.resolve();
  assert.equal(failed.shown, 0);
  assert.deepEqual(failing.events, [
    { event: "error", key: KEY, reqId: "req-1", code: "load-failed" },
    { event: "closed", key: KEY, token: TOKEN },
  ]);

  const httpError = fakeHost();
  send(httpError, openOp(chat));
  const [rejected] = httpError.electron.created;
  rejected.webContents.emit("did-navigate", {}, rejected.loaded[0], 403);
  rejected.emit("ready-to-show");
  await Promise.resolve();
  assert.equal(rejected.shown, 0);
  assert.equal(httpError.events[0].code, "load-failed");
}

async function main() {
  validation();
  hashFormat();
  requestFilter();
  argumentsAndLines();
  protocol();
  await chatProtocol();
  console.log("UI_HOST_TESTS_PASSED");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
