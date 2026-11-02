"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// One account-scoped surface. Plugin frames never receive native IPC capabilities.
const path = require("node:path");
const { randomBytes } = require("node:crypto");
const METHODS = new Set(["self-profile.read", "self-profile.save", "model.status", "settings.open", "view.focusChat", "toast"]);
const object = value => value && typeof value === "object" && !Array.isArray(value);
const accountName = value => typeof value === "string" && value.length <= 256 && !/[\u0000-\u001f\u007f]/u.test(value);
const small = value => { try { return Buffer.byteLength(JSON.stringify(value)) <= 1024 * 1024; } catch { return false; } };
const changed = () => ({ error: { code: "context-changed", message: "当前账号或页面已变化，请重试" } });

function selfContext(value) {
  if (!object(value) || !accountName(value.account)) return null;
  return { account: value.account, user: "", theme: value.theme === "light" ? "light" : "dark", surface: "self-analysis-window" };
}

function createSelfAnalysisWindow(options) {
  const { BrowserWindow, mainWindow, clientUrl, screen, ipcMain, clipboard, trustedMainFrame } = options;
  const address = new URL("self-analysis.html", clientUrl).href;
  const pluginPage = new URL("plugin-pages/kyf.self-analysis/ui/index.html", clientUrl).href;
  const mainContents = mainWindow.webContents;
  const pending = new Map(), cleared = new Set();
  let child = null, closed = false, epoch = 0, enabled = null;
  let context = selfContext({ account: "", theme: "dark" });
  const trustedChild = event => !!child && !child.isDestroyed() && event.sender === child.webContents &&
    event.senderFrame === child.webContents.mainFrame && event.senderFrame.url === address;
  const current = scope => object(scope) && scope.epoch === epoch && scope.account === context.account &&
    (!context.account || !cleared.has(context.account));
  function cancelPending() {
    for (const waiter of pending.values()) { clearTimeout(waiter.timer); waiter.resolve(changed()); }
    pending.clear();
  }
  function send(event) {
    if (child && !child.isDestroyed()) child.webContents.send("self-analysis:message", { context, epoch, enabled, ...(event ? { event } : {}) });
  }
  function replace(next, event) {
    if (next.account !== context.account || event) { epoch++; cancelPending(); }
    context = next;
    send(event);
  }
  function destroy() {
    epoch++; cancelPending();
    const previous = child; child = null;
    if (previous && !previous.isDestroyed()) previous.destroy();
  }
  function sync(message) {
    if (closed || !object(message)) return false;
    if (object(message.context)) {
      const next = selfContext(message.context);
      if (!next) return false;
      if (cleared.has(next.account)) next.account = "";
      replace(next); return true;
    }
    const event = message.event;
    if (!object(event)) return false;
    if (event.type === "account-cleared" && accountName(event.account) && event.account) {
      cleared.add(event.account);
      replace(context.account === event.account ? { ...context, account: "" } : context,
        { type: "account-cleared", account: event.account });
    } else if (event.type === "plugins-changed") {
      if (typeof event.enabled === "boolean" && event.enabled !== enabled) {
        enabled = event.enabled; replace(context, { type: event.type, enabled });
      } else send({ type: event.type, enabled });
    } else if (event.type === "service-restored") replace(context, { type: event.type });
    else return false;
    return true;
  }
  function open(value) {
    const next = selfContext(value);
    if (closed || !next || mainWindow.isDestroyed()) return false;
    if (cleared.has(next.account)) next.account = "";
    replace(next);
    if (child && !child.isDestroyed()) {
      if (child.isMinimized()) child.restore();
      child.show(); child.focus(); send({ type: "shown" }); return true;
    }
    const anchor = mainWindow.getBounds(), area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(1080, area.width), height = Math.min(820, area.height);
    const viewer = new BrowserWindow({
      x: Math.max(area.x, Math.min(anchor.x + 48, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + 48, area.y + area.height - height)),
      width, height, minWidth: Math.min(560, width), minHeight: Math.min(480, height),
      title: "自我剖析 · 知己微信助手", show: false, autoHideMenuBar: true, resizable: true,
      ...(options.root ? { icon: path.join(options.root, "chatui", "assets", "app-icon.ico") } : {}),
      backgroundColor: next.theme === "light" ? "#f7f8fa" : "#171717",
      webPreferences: { preload: path.join(__dirname, "self-analysis-preload.cjs"),
        contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, devTools: false,
        session: mainContents.session },
    });
    child = viewer;
    const contents = viewer.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const name of ["will-navigate", "will-redirect", "will-attach-webview"])
      contents.on(name, event => event.preventDefault());
    contents.on("will-frame-navigate", event => {
      if (event.isMainFrame !== false || event.url !== pluginPage) event.preventDefault();
    });
    contents.on("page-title-updated", event => event.preventDefault());
    contents.on("render-process-gone", () => { if (child === viewer) destroy(); });
    contents.on("did-finish-load", () => { if (child === viewer) send(); });
    viewer.on("closed", () => { if (child === viewer) { child = null; epoch++; cancelPending(); } });
    viewer.once("ready-to-show", () => { if (!closed && !viewer.isDestroyed()) { viewer.show(); viewer.focus(); } });
    void viewer.loadURL(address).catch(() => { if (child === viewer) destroy(); });
    return true;
  }
  const onSync = (event, message) => { if (trustedMainFrame(event)) sync(message); };
  const onResult = (event, id, reply) => {
    if (!trustedMainFrame(event) || typeof id !== "string" || !object(reply) || !small(reply)) return;
    const waiter = pending.get(id);
    if (!waiter) return;
    clearTimeout(waiter.timer); pending.delete(id);
    if (!current(waiter.scope)) { waiter.resolve(changed()); return; }
    if (!reply.error && waiter.method === "view.focusChat" && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show(); mainWindow.focus();
    }
    waiter.resolve(reply);
  };
  ipcMain.handle("self-analysis:open", (event, value, activated) =>
    trustedMainFrame(event) && activated === true ? open(value) : false);
  ipcMain.handle("self-analysis:context", event => trustedChild(event) ? { context, epoch, enabled } : null);
  ipcMain.handle("self-analysis:host-call", (event, method, params, scope) => {
    if (!trustedChild(event) || !METHODS.has(method) || !object(params) || !small(params) || !current(scope) ||
        (method.startsWith("self-profile.") && (!context.account || enabled === false)) ||
        (params.account !== undefined && params.account !== context.account) || pending.size >= 16 || mainWindow.isDestroyed())
      return changed();
    const bound = { ...context }, id = randomBytes(16).toString("hex");
    return new Promise(resolve => {
      const timer = setTimeout(() => { pending.delete(id); resolve({ error: { code: "host-timeout", message: "主窗口没有响应，请重试" } }); }, 30000);
      pending.set(id, { resolve, timer, method, scope: { account: bound.account, epoch } });
      mainContents.send("self-analysis:host-call", { id, method, params, scope: bound });
    });
  });
  ipcMain.handle("self-analysis:copy", (event, text, scope, activated) => {
    if (!trustedChild(event) || !current(scope) || !context.account || enabled === false || activated !== true || !child.webContents.isFocused() ||
        typeof text !== "string" || !text.trim() || text.length > 1_000_000) return false;
    clipboard.writeText(text); return true;
  });
  ipcMain.on("self-analysis:sync", onSync);
  ipcMain.on("self-analysis:host-result", onResult);
  function mainNavigation(_event, _url, inPlace, isMainFrame) {
    if (isMainFrame === false || inPlace) return;
    cleared.clear(); context = selfContext({ account: "", theme: context.theme }); destroy();
  }
  function close() {
    if (closed) return;
    closed = true; destroy();
    mainContents.off("did-start-navigation", mainNavigation);
    mainContents.off("render-process-gone", destroy);
    mainWindow.off("closed", close);
    ipcMain.off("self-analysis:sync", onSync); ipcMain.off("self-analysis:host-result", onResult);
    for (const name of ["open", "context", "host-call", "copy"]) ipcMain.removeHandler(`self-analysis:${name}`);
  }
  mainContents.on("did-start-navigation", mainNavigation);
  mainContents.on("render-process-gone", destroy);
  mainWindow.on("closed", close);
  return { open, openCurrent: () => open(context), sync, close, count: () => child && !child.isDestroyed() ? 1 : 0 };
}

module.exports = { createSelfAnalysisWindow, selfContext };
