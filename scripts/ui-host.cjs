"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 可由外部 Agent 打开的知己窗口宿主（外部 Agent 衔接方案 B4）：Core 按需以
// `electron scripts/ui-host.cjs --user-data <dir>` 拉起的常驻 Electron 进程，不要求
// 知己主窗口打开。每个窗口是一个只读（restricted=1）的知己分析页面，跑在独立的
// 内存会话里；本进程只给发往 Core 源站的请求加上 X-KYF-UI-Session 头，令牌从不
// 进入渲染进程。
//
// stdin 行协议（Core → 宿主，每行一个 JSON）：
//   {"op":"open","key":hex,"reqId":str,"surface":"friend"|"chat","url":"http://127.0.0.1:<port>/<page>",
//    "hash":{account,user,name?,avatar?} | "account=…&user=…","title"?:str,"tab"?:str,
//    "token":str,"theme"?:"light"|"dark"|"system"}
//   {"op":"close","key":hex}
//   {"op":"select-tab","key":hex,"tab":str}
//   {"op":"quit"}
// stdout 行协议（宿主 → Core）：
//   {"event":"ready"}                              启动完成、有可用显示器
//   {"event":"opened","key","reqId"}               新窗口已显示
//   {"event":"focused","key","reqId"}              同 key 已有窗口，已前置（令牌已更新）
//   {"event":"closed","key"}                       窗口已关闭（用户关闭、close 指令或页面崩溃）
//   {"event":"error","key"?,"reqId"?,"code"}       code 见 ERROR_CODES；no-display/already-running 后随即退出
// stdin 关闭（EOF）→ 关闭所有窗口并退出；最后一个窗口关闭 60 秒后自行退出。
//
// 不依赖 Electron 的纯函数（validateOpenRequest、buildHash、isAllowedUrl、headerFor 等）
// 与 createUiHost 工厂一并导出，供 scripts/test-ui-host.cjs 在普通 Node 下测试。

const fs = require("node:fs");
const path = require("node:path");
const { friendScope } = require("./friend-windows.cjs");

const SESSION_HEADER = "X-KYF-UI-Session";
const IDLE_EXIT_MS = 60_000;
const LINE_LIMIT = 64 * 1024;
const DEFAULT_SIZE = { width: 960, height: 760 };
const MIN_SIZE = { width: 420, height: 480 };
const CASCADE = 32;
const KEY = /^[0-9a-f]{8,64}$/;
const REQ_ID = /^[A-Za-z0-9_.:-]{1,64}$/;
const TOKEN = /^[A-Za-z0-9_-]{16,256}$/;
// 知己分析（kyf.friend-analysis 默认方案）的栏目；memory 是 memories 的别名。
const TABS = new Set(["overview", "memories", "memory", "persona", "values", "chat", "trend"]);
const THEMES = new Set(["light", "dark"]);
// Core 可以传 system：由宿主按系统深浅色解析成 light/dark 再写进片段。
const REQUEST_THEMES = new Set(["light", "dark", "system"]);
const SURFACES = { friend: "/friend.html", chat: "/history.html" };
const ERROR_CODES = Object.freeze(["no-display", "already-running", "bad-json", "line-too-long", "unknown-op",
  "invalid-request", "unknown-key", "window-failed", "load-failed"]);
const CHANNEL_HOST_EVENT = "friend:host-event";

function plainText(value, maximum) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum &&
    !/[\u0000-\u001f\u007f]/u.test(value) ? value : null;
}

// Core 源站只能是本机回环 http：http://127.0.0.1:<port> 或 http://localhost:<port>。
function coreOrigin(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname) || !url.port ||
      url.username || url.password) return null;
  const port = Number(url.port);
  return Number.isInteger(port) && port > 0 && port <= 65535 ? url.origin : null;
}

// 页面地址 = Core 源站 + 该 surface 的固定页面路径，不带查询和片段。
function pageUrl(value, surface) {
  if (typeof value !== "string" || value.length > 512 || !Object.hasOwn(SURFACES, surface)) return null;
  const origin = coreOrigin(value);
  if (!origin) return null;
  const url = new URL(value);
  if (url.pathname !== SURFACES[surface] || url.search || url.hash || value.includes("#")) return null;
  return { origin, href: origin + url.pathname };
}

function scopeFrom(hash) {
  if (typeof hash === "string") {
    if (hash.length > 2048) return null;
    const parameters = new URLSearchParams(hash.replace(/^#/, ""));
    const value = {};
    for (const key of ["account", "user", "name", "avatar"]) if (parameters.has(key)) value[key] = parameters.get(key);
    return friendScope(value);
  }
  if (!hash || typeof hash !== "object" || Array.isArray(hash)) return null;
  return friendScope({ account: hash.account, user: hash.user, name: hash.name, avatar: hash.avatar });
}

// -> 规范化后的打开请求；任何字段不合格都返回 null（宿主回 invalid-request）。
function validateOpenRequest(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.op !== "open") return null;
  const { key, reqId, surface, token } = value;
  if (typeof key !== "string" || !KEY.test(key) || typeof reqId !== "string" || !REQ_ID.test(reqId)) return null;
  if (typeof surface !== "string" || !Object.hasOwn(SURFACES, surface) ||
      typeof token !== "string" || !TOKEN.test(token)) return null;
  const page = pageUrl(value.url, surface);
  const scope = scopeFrom(value.hash);
  if (!page || !scope) return null;
  if (surface === "chat" && (value.tab != null || scope.avatar)) return null;
  if (value.tab !== undefined && value.tab !== null && !(typeof value.tab === "string" && TABS.has(value.tab))) return null;
  if (value.theme !== undefined && value.theme !== null && !REQUEST_THEMES.has(value.theme)) return null;
  if (value.title !== undefined && value.title !== null && !plainText(value.title, 128)) return null;
  return {
    key, reqId, surface, token, scope, coreOrigin: page.origin, page: page.href,
    tab: value.tab || null, theme: value.theme || "system",
    title: value.title || `${scope.name} · ${surface === "chat" ? "聊天记录" : "知己分析"}`,
  };
}

// 与 friend-windows.cjs 相同的片段格式，再加 restricted=1（及可选的主题、初始栏目）。
function buildHash(scope, options = {}) {
  const parameters = new URLSearchParams({ account: scope.account, user: scope.user, name: scope.name,
    ...(scope.avatar ? { avatar: scope.avatar } : {}) });
  parameters.set("restricted", "1");
  if (options.theme && THEMES.has(options.theme)) parameters.set("theme", options.theme);
  if (options.tab && TABS.has(options.tab)) parameters.set("tab", options.tab);
  return parameters.toString();
}

function targetUrl(request) {
  return request.page + "#" + buildHash(request.scope, request);
}

// 只放行：Core 源站、页面自身需要的 data:/blob:/devtools:，以及 https 图片（头像）。
function isAllowedUrl(url, origin, resourceType, surface = "friend") {
  let parsed;
  try { parsed = new URL(url); } catch { return false; }
  if (["data:", "blob:", "devtools:"].includes(parsed.protocol)) return true;
  if (origin && parsed.protocol === "http:" && parsed.origin === origin) return true;
  return surface === "friend" && parsed.protocol === "https:" && resourceType === "image";
}

// 只有发往 Core 源站的请求带会话令牌。
function headerFor(url, origin, token) {
  let parsed;
  try { parsed = new URL(url); } catch { return null; }
  return origin && token && parsed.origin === origin ? { [SESSION_HEADER]: token } : null;
}

function withSessionHeader(requestHeaders, url, origin, token) {
  const headers = {};
  // 页面自己写的同名头一律丢弃，令牌只由宿主附加。
  for (const [name, value] of Object.entries(requestHeaders || {}))
    if (name.toLowerCase() !== SESSION_HEADER.toLowerCase()) headers[name] = value;
  return { ...headers, ...(headerFor(url, origin, token) || {}) };
}

function frameNavigationAllowed(url, origin) {
  return typeof url === "string" && !!origin && url.startsWith(origin + "/plugin-pages/");
}

function parseLine(line) {
  if (typeof line !== "string") return { error: "bad-json" };
  if (line.length > LINE_LIMIT) return { error: "line-too-long" };
  try {
    const value = JSON.parse(line);
    return value && typeof value === "object" && !Array.isArray(value) ? { value } : { error: "bad-json" };
  } catch { return { error: "bad-json" }; }
}

// key/reqId 合格时才回显，避免把任意输入写回 Core。
function echo(value) {
  const result = {};
  if (value && typeof value.key === "string" && KEY.test(value.key)) result.key = value.key;
  if (value && typeof value.reqId === "string" && REQ_ID.test(value.reqId)) result.reqId = value.reqId;
  return result;
}

function userDataArgument(argv, env) {
  const args = Array.isArray(argv) ? argv : [];
  for (let index = 0; index < args.length; index++) {
    const item = args[index];
    if (item === "--user-data" && typeof args[index + 1] === "string") return args[index + 1];
    if (typeof item === "string" && item.startsWith("--user-data=")) return item.slice("--user-data=".length);
  }
  return env && typeof env.KYF_UI_HOST_USER_DATA === "string" && env.KYF_UI_HOST_USER_DATA ?
    env.KYF_UI_HOST_USER_DATA : null;
}

// options: {BrowserWindow, session, screen, emit, root?, systemDark?, onIdle?, onQuit?, idleMs?,
//           setTimeout?, clearTimeout?}
function createUiHost(options) {
  const { BrowserWindow, session, screen, emit } = options;
  const resolveTheme = theme => THEMES.has(theme) ? theme : (options.systemDark?.() === false ? "light" : "dark");
  const schedule = options.setTimeout || setTimeout;
  const unschedule = options.clearTimeout || clearTimeout;
  const idleMs = options.idleMs ?? IDLE_EXIT_MS;
  const windows = new Map();
  let idleTimer = null, stopped = false;

  function armIdle() {
    unschedule(idleTimer);
    idleTimer = null;
    if (stopped || windows.size) return;
    idleTimer = schedule(() => { idleTimer = null; if (!windows.size && !stopped) options.onIdle?.(); }, idleMs);
  }
  function initialBounds() {
    const area = screen.getPrimaryDisplay().workArea;
    const width = Math.min(DEFAULT_SIZE.width, area.width), height = Math.min(DEFAULT_SIZE.height, area.height);
    const offset = (windows.size % 8) * CASCADE;
    return {
      x: Math.max(area.x, Math.min(area.x + Math.round((area.width - width) / 2) + offset, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(area.y + Math.round((area.height - height) / 2) + offset, area.y + area.height - height)),
      width, height,
    };
  }
  // 每个 key 一个内存分区（无 persist: 前缀）；每次打开都按最新令牌与源站重装拦截器。
  function guardSession(ses, entry) {
    ses.setPermissionRequestHandler?.((_contents, _permission, callback) => callback(false));
    ses.setPermissionCheckHandler?.(() => false);
    ses.setSpellCheckerEnabled?.(false);
    ses.webRequest.onBeforeRequest((details, callback) => {
      callback({ cancel: !isAllowedUrl(details.url, entry.request.coreOrigin, details.resourceType, entry.request.surface) });
    });
    ses.webRequest.onBeforeSendHeaders((details, callback) => {
      callback({ requestHeaders: withSessionHeader(details.requestHeaders, details.url, entry.request.coreOrigin,
        entry.request.token) });
    });
    if (!entry.downloadGuard) {
      entry.downloadGuard = event => event.preventDefault();
      ses.on?.("will-download", entry.downloadGuard);
    }
  }
  function hostEvent(entry, payload) {
    if (!entry.window.isDestroyed()) entry.window.webContents.send(CHANNEL_HOST_EVENT, payload);
  }

  function open(validated) {
    const request = { ...validated, theme: resolveTheme(validated.theme) };
    unschedule(idleTimer);
    idleTimer = null;
    const existing = windows.get(request.key);
    const chat = request.surface === "chat";
    if (existing && !existing.window.isDestroyed() && chat && existing.request.token !== request.token) {
      existing.window.destroy();
    }
    if (existing && !existing.window.isDestroyed()) {
      if (chat && !existing.opened) {
        emit({ event: "error", key: request.key, reqId: request.reqId, code: "window-failed" });
        return;
      }
      const previous = targetUrl(existing.request);
      existing.request = request;
      guardSession(existing.session, existing);
      existing.window.setTitle?.(request.title);
      if (!chat && targetUrl(request) !== previous) void existing.window.loadURL(targetUrl(request)).catch(() => { });
      else {
        hostEvent(existing, { type: "focus" });
        if (request.tab) hostEvent(existing, { type: "select-tab", tab: request.tab });
      }
      if (existing.window.isMinimized()) existing.window.restore();
      existing.window.show();
      existing.window.focus();
      emit({ event: "focused", key: request.key, reqId: request.reqId });
      return;
    }
    let window, ses;
    const entry = { key: request.key, request, window: null, session: null, opened: false, downloadGuard: null };
    try {
      ses = session.fromPartition(`uihost-${request.key}${chat ? `-${request.reqId}` : ""}`);
      entry.session = ses;
      guardSession(ses, entry);
      const bounds = initialBounds();
      window = new BrowserWindow({
        ...bounds, minWidth: Math.min(MIN_SIZE.width, bounds.width), minHeight: Math.min(MIN_SIZE.height, bounds.height),
        title: request.title, show: false, autoHideMenuBar: true, resizable: true,
        backgroundColor: request.theme === "light" ? "#ffffff" : "#1b1b1b",
        ...(options.root ? { icon: path.join(options.root, "chatui", "assets", "app-icon.ico") } : {}),
        webPreferences: {
          ...(chat ? {} : { preload: path.join(__dirname, "friend-preload.cjs") }), session: ses,
          contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true,
          devTools: false, spellcheck: false, webviewTag: false,
        },
      });
    } catch {
      emit({ event: "error", key: request.key, reqId: request.reqId, code: "window-failed" });
      armIdle();
      return;
    }
    entry.window = window;
    windows.set(request.key, entry);
    const contents = window.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const name of ["will-navigate", "will-redirect", "will-attach-webview"])
      contents.on(name, event => event.preventDefault());
    // 唯一允许的框架导航：插件方案页（与 friend-windows.cjs 相同）。
    contents.on("will-frame-navigate", event => {
      if (chat || event.isMainFrame !== false || !frameNavigationAllowed(event.url, entry.request.coreOrigin)) event.preventDefault();
    });
    let readyToShow = false, loaded = false, failed = false, navigationOk = !chat;
    function reportFailure(code) {
      if (entry.opened || failed) return;
      failed = true;
      emit({ event: "error", key: entry.key, reqId: entry.request.reqId, code });
    }
    function loadFailed() {
      if (window.isDestroyed() || entry.opened) return;
      reportFailure("load-failed");
      window.destroy();
    }
    contents.on("did-fail-load", (_event, code, _description, _url, isMainFrame) => {
      if (isMainFrame !== false && code !== -3) loadFailed();
    });
    if (chat) contents.once("did-navigate", (_event, _url, status) => {
      if (!(status >= 200 && status < 300)) { loadFailed(); return; }
      navigationOk = true;
      showWhenReady();
    });
    contents.on("render-process-gone", () => { if (!window.isDestroyed()) window.destroy(); });
    function showWhenReady() {
      if (window.isDestroyed() || entry.opened || !readyToShow || (chat && (!loaded || !navigationOk))) return;
      entry.opened = true;
      window.show();
      window.focus();
      emit({ event: "opened", key: entry.key, reqId: entry.request.reqId });
    }
    window.once("ready-to-show", () => {
      readyToShow = true;
      showWhenReady();
    });
    window.on("closed", () => {
      if (chat) reportFailure("window-failed");
      if (windows.get(entry.key) === entry) windows.delete(entry.key);
      if (entry.downloadGuard) ses.removeListener?.("will-download", entry.downloadGuard);
      void Promise.resolve(ses.clearStorageData?.()).catch(() => { });
      emit({ event: "closed", key: entry.key, ...(chat ? { token: entry.request.token } : {}) });
      armIdle();
    });
    void window.loadURL(targetUrl(request)).then(() => {
      loaded = true;
      showWhenReady();
    }, loadFailed);
  }

  function handle(value) {
    const op = value?.op;
    if (op === "open") {
      const request = validateOpenRequest(value);
      if (!request) { emit({ event: "error", ...echo(value), code: "invalid-request" }); return; }
      open(request);
    } else if (op === "close" || op === "select-tab") {
      if (typeof value.key !== "string" || !KEY.test(value.key) ||
          (op === "select-tab" && !(typeof value.tab === "string" && TABS.has(value.tab)))) {
        emit({ event: "error", ...echo(value), code: "invalid-request" });
        return;
      }
      const entry = windows.get(value.key);
      if (!entry || entry.window.isDestroyed()) { emit({ event: "error", key: value.key, code: "unknown-key" }); return; }
      if (op === "close") {
        if (entry.request.surface !== "chat" || entry.request.token === value.token) entry.window.destroy();
      }
      else hostEvent(entry, { type: "select-tab", tab: value.tab });
    } else if (op === "quit") {
      options.onQuit?.();
    } else emit({ event: "error", ...echo(value), code: "unknown-op" });
  }
  function handleLine(line) {
    if (stopped || !line.trim()) return;
    const parsed = parseLine(line);
    if (parsed.error) { emit({ event: "error", code: parsed.error }); return; }
    handle(parsed.value);
  }
  function closeAll() {
    stopped = true;
    unschedule(idleTimer);
    idleTimer = null;
    for (const entry of [...windows.values()]) if (!entry.window.isDestroyed()) entry.window.destroy();
    windows.clear();
  }
  return { handle, handleLine, closeAll, armIdle, count: () => windows.size, has: key => windows.has(key) };
}

module.exports = {
  SESSION_HEADER, IDLE_EXIT_MS, TABS, ERROR_CODES, CHANNEL_HOST_EVENT,
  validateOpenRequest, buildHash, targetUrl, isAllowedUrl, headerFor, withSessionHeader, frameNavigationAllowed,
  coreOrigin, pageUrl, parseLine, userDataArgument, createUiHost,
};

// Electron 不把入口脚本设为 require.main（那是 electron 模块），按 argv 判断直启。
const INVOKED_DIRECTLY = process.argv.length >= 2 && typeof process.argv[1] === "string" &&
  path.resolve(process.argv[1]) === path.resolve(__filename);

if (INVOKED_DIRECTLY && process.versions.electron) main();

function main() {
  const { app, BrowserWindow, ipcMain, nativeTheme, screen, session } = require("electron");
  const readline = require("node:readline");
  const root = path.resolve(__dirname, "..");
  const emit = value => { try { process.stdout.write(JSON.stringify(value) + "\n"); } catch { /* 退出中 */ } };
  app.setAppUserModelId("com.local.knowyourfriends.real-client.ui-host");
  try {
    const userData = path.resolve(userDataArgument(process.argv, process.env) || path.join(root, ".local", "ui-host"));
    fs.mkdirSync(userData, { recursive: true });
    app.setPath("userData", userData);
  } catch { /* 默认 userData 也可用 */ }
  if (!app.requestSingleInstanceLock()) {
    emit({ event: "error", code: "already-running" });
    app.exit(2);
    return;
  }
  let host = null, exiting = false;
  const shutdown = code => {
    if (exiting) return;
    exiting = true;
    host?.closeAll();
    setImmediate(() => app.exit(code));
  };
  // 窗口全部关闭不等于退出：由空闲计时器或 stdin EOF 决定。
  app.on("window-all-closed", () => { });
  app.on("web-contents-created", (_event, contents) => {
    contents.on("will-attach-webview", event => event.preventDefault());
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
  });
  // friend-preload 的特权调用在只读窗口里一律无效：定位返回 false，关窗暂存直接忽略。
  ipcMain.handle("friend:locate", () => false);
  ipcMain.on("friend:close-ready", () => { });
  app.whenReady().then(() => {
    let displays = [];
    try { displays = screen.getAllDisplays(); } catch { displays = []; }
    if (!displays.length) {
      emit({ event: "error", code: "no-display" });
      app.exit(2);
      return;
    }
    host = createUiHost({ BrowserWindow, session, screen, emit, root,
      systemDark: () => nativeTheme.shouldUseDarkColors,
      onIdle: () => shutdown(0), onQuit: () => shutdown(0) });
    // Electron 主进程的 process.stdin 在 Windows 管道上读不到数据并立即结束；直接读 fd 0。
    const input = fs.createReadStream(null, { fd: 0, autoClose: false });
    input.on("error", () => shutdown(0));
    const lines = readline.createInterface({ input, crlfDelay: Infinity });
    lines.on("line", line => { try { host.handleLine(line); } catch { emit({ event: "error", code: "invalid-request" }); } });
    lines.on("close", () => shutdown(0));
    emit({ event: "ready" });
    host.armIdle();
  });
}
