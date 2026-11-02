"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Chat-toolbar plugin pages in independent windows (2026-10-22, replaces the docked view of scripts/plugin-panel.cjs
// and the single assistant window of scripts/assistant-window.cjs): one window per plugin page, account and friend,
// so several friends' 聊天助手 stay open side by side. Each window hosts chatui/plugin-panel.html with its friend
// pinned; the main window answers the few host methods it offers, only for that friend.

const fs = require("node:fs");
const path = require("node:path");
const { randomBytes } = require("node:crypto");
const { visibleOn } = require("./friend-windows.cjs");

const DEFAULT_SIZE = { width: 460, height: 760 };
const MIN_SIZE = { width: 360, height: 420 };
const CASCADE = 28;
// A closed window is hidden and keeps its page, so reopening it is instant; only the latest few stay.
const HIDDEN_KEEP = 3;
const MAX_MESSAGE_BYTES = 4 * 1024 * 1024;
const MAX_QUEUED_EVENTS = 16;
const MAX_PENDING_CALLS = 16;
const HOST_CALL_TIMEOUT_MS = 30000;
const HOST_METHODS = new Set(["toast", "view.focusChat", "attention.seen"]);
const PLUGIN_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/i;
const THEMES = Object.freeze({ dark: "#1b1b1b", light: "#ffffff" });
const object = value => value && typeof value === "object" && !Array.isArray(value);
const validText = (value, maximum) => typeof value === "string" && !!value.trim() && value.length <= maximum &&
  !/[\u0000-\u001f\u007f]/u.test(value);

function validTarget(value) {
  return object(value) && typeof value.plugin === "string" && PLUGIN_ID.test(value.plugin) &&
    typeof value.page === "string" && value.page.length <= 256 && /^[A-Za-z0-9._/-]+$/.test(value.page) &&
    value.page.split("/").every(part => part && part !== "." && part !== "..");
}
function small(value) {
  try { return Buffer.byteLength(JSON.stringify(value)) <= MAX_MESSAGE_BYTES; } catch { return false; }
}
// {plugin, page, account, user, name?, label?, theme?} of one window; the friend never changes afterwards and
// nothing else a caller passes is kept.
function windowScope(value) {
  if (!validTarget(value)) return null;
  const { account, user, name, label } = value;
  if (!validText(account, 256) || !validText(user, 256) ||
      (name !== undefined && name !== "" && !validText(name, 128)) ||
      (label !== undefined && label !== "" && !validText(label, 64))) return null;
  return { plugin: value.plugin, page: value.page, account, user, name: name || user, label: label || "插件",
    theme: value.theme === "light" ? "light" : "dark" };
}

function createPluginWindows(options) {
  const { BrowserWindow, ipcMain, screen, clipboard, mainWindow, clientUrl, trustedMainFrame, userDataPath } = options;
  const address = new URL("plugin-panel.html", clientUrl).href;
  const windows = new Map(), pending = new Map();
  const prefix = "w" + randomBytes(6).toString("hex");
  let serial = 0, sequence = 0, closed = false, theme = "dark";
  const boundsFile = userDataPath ? path.join(userDataPath, "plugin-windows.json") : null;
  let saved = {};
  try { if (boundsFile) saved = JSON.parse(fs.readFileSync(boundsFile, "utf8")) || {}; } catch { saved = {}; }
  let saveTimer = null;
  function persist() {
    if (!boundsFile) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const entries = Object.entries(saved).slice(-200);
      try { fs.writeFileSync(boundsFile, JSON.stringify(Object.fromEntries(entries)), { mode: 0o600 }); } catch { }
    }, 300);
  }
  const keyOf = scope => JSON.stringify([scope.plugin, scope.page, scope.account, scope.user]);
  const pageOf = url => typeof url === "string" ? url.split(/[?#]/)[0] : "";
  const title = scope => `${scope.name} · ${scope.label}`;
  const live = entry => !entry.window.isDestroyed() && !entry.hidden;
  const shown = entry => live(entry) && entry.window.isVisible() && !entry.window.isMinimized();
  const context = entry => ({ account: entry.scope.account, user: entry.scope.user, name: entry.scope.name, theme });

  function entryFor(event) {
    for (const entry of windows.values()) {
      if (entry.window.isDestroyed()) continue;
      const contents = entry.window.webContents;
      if (event?.sender === contents && event.senderFrame === contents.mainFrame && pageOf(event.senderFrame.url) === address)
        return entry;
    }
    return null;
  }
  function toMain(channel, value) {
    if (!closed && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, value);
  }
  function toPage(entry, message) {
    if (entry.loaded && !entry.window.isDestroyed()) entry.window.webContents.send("plugin-panel:to-panel", message);
  }
  // Which friends have a window, and whether it is on screen: the main window's toolbar and reminders follow it.
  function publish() {
    toMain("plugin-panel:state", { windows: [...windows.values()].filter(live).map(entry => ({
      plugin: entry.scope.plugin, account: entry.scope.account, user: entry.scope.user, visible: shown(entry) })) });
  }
  function visibility(entry) {
    const visible = shown(entry);
    if (visible !== entry.visible) {
      entry.visible = visible;
      toPage(entry, { event: { type: "visibility", visible } });
    }
    publish();
  }
  // Answers still owed to a page that can no longer get them from the main window it asked.
  function failPending(entry, code, message) {
    for (const [id, waiter] of pending) {
      if (waiter.entry !== entry) continue;
      pending.delete(id); clearTimeout(waiter.timer);
      if (entry.loaded && !entry.window.isDestroyed())
        entry.window.webContents.send("plugin-panel:host-result", waiter.original, { error: { code, message } });
    }
  }
  function initialBounds(key) {
    const previous = saved[key];
    if (previous && [previous.x, previous.y, previous.width, previous.height].every(Number.isFinite) &&
        visibleOn(screen.getAllDisplays(), previous)) return previous;
    const anchor = mainWindow.getBounds(), area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(DEFAULT_SIZE.width, area.width), height = Math.min(DEFAULT_SIZE.height, area.height);
    const offset = (windows.size % 8) * CASCADE;
    // Beside the main window where the docked panel used to be, or over its right edge on a narrow screen.
    const right = anchor.x + anchor.width;
    const baseX = right + width <= area.x + area.width ? right : right - width - 24;
    return {
      x: Math.max(area.x, Math.min(baseX + offset, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + offset, area.y + area.height - height)), width, height,
    };
  }
  function destroy(entry) {
    failPending(entry, "context-changed", "聊天助手窗口已关闭");
    entry.allowClose = true;
    if (!entry.window.isDestroyed()) entry.window.destroy();
  }
  // Keep the most recently closed windows for an instant reopen; release the others.
  function trimHidden() {
    const hidden = [...windows.values()].filter(entry => entry.hidden && !entry.window.isDestroyed())
      .sort((left, right) => right.hiddenAt - left.hiddenAt);
    for (const entry of hidden.slice(HIDDEN_KEEP)) destroy(entry);
  }
  function open(value) {
    const scope = windowScope(value);
    if (closed || !scope || mainWindow.isDestroyed()) return { opened: false };
    if (scope.theme !== theme) setTheme(scope.theme);
    const key = keyOf(scope);
    const existing = windows.get(key);
    if (existing && !existing.window.isDestroyed()) {
      // A closed window keeps its page and comes back as it was.
      existing.hidden = false;
      existing.scope = { ...existing.scope, name: scope.name, label: scope.label };
      existing.window.setTitle(title(existing.scope));
      toPage(existing, { context: context(existing) });
      if (existing.window.isMinimized()) existing.window.restore();
      existing.window.show(); existing.window.focus();
      visibility(existing);
      return { opened: true };
    }
    const bounds = initialBounds(key);
    const child = new BrowserWindow({
      ...bounds, minWidth: Math.min(MIN_SIZE.width, bounds.width), minHeight: Math.min(MIN_SIZE.height, bounds.height),
      title: title(scope), show: false, autoHideMenuBar: true, resizable: true, backgroundColor: THEMES[theme],
      webPreferences: {
        preload: path.join(__dirname, "plugin-panel-preload.cjs"), contextIsolation: true, sandbox: true,
        nodeIntegration: false, webSecurity: true, devTools: false, session: mainWindow.webContents.session,
      },
    });
    const entry = { window: child, scope, key, serial: ++serial, loaded: false, queued: [], visible: true,
      hidden: false, hiddenAt: 0, allowClose: false };
    windows.set(key, entry);
    const contents = child.webContents;
    const pluginPage = new URL(`plugin-pages/${scope.plugin}/${scope.page}`, clientUrl).href;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    contents.on("will-navigate", (event, url) => { if (pageOf(url) !== address) event.preventDefault(); });
    contents.on("will-redirect", (event, url) => { if (pageOf(url) !== address) event.preventDefault(); });
    contents.on("will-attach-webview", event => event.preventDefault());
    // The only frame navigation allowed is this window's own plugin page inside the host page.
    contents.on("will-frame-navigate", event => {
      if (event.isMainFrame !== false || pageOf(event.url) !== pluginPage) event.preventDefault();
    });
    contents.on("did-finish-load", () => {
      if (child.isDestroyed()) return;
      entry.loaded = true;
      toPage(entry, { context: context(entry) });
      for (const event of entry.queued.splice(0)) toPage(entry, { event });
    });
    contents.on("render-process-gone", () => destroy(entry));
    // The window keeps the friend's name, not the host page's document title.
    child.on("page-title-updated", event => event.preventDefault());
    const remember = () => {
      if (child.isDestroyed() || child.isMinimized() || child.isMaximized()) return;
      saved[key] = child.getBounds();
      persist();
    };
    child.on("resize", remember);
    child.on("move", remember);
    for (const name of ["show", "hide", "minimize", "restore"]) child.on(name, () => visibility(entry));
    child.on("close", event => {
      if (entry.allowClose || closed) return;
      event.preventDefault();
      // Hidden, not destroyed: the page saves its last draft edit and stays ready for the friend.
      entry.hidden = true; entry.hiddenAt = ++sequence;
      failPending(entry, "context-changed", "聊天助手窗口已关闭");
      child.hide();
      visibility(entry);
      trimHidden();
    });
    child.on("closed", () => {
      failPending(entry, "context-changed", "聊天助手窗口已关闭");
      if (windows.get(key) === entry) windows.delete(key);
      publish();
    });
    child.once("ready-to-show", () => { if (!closed && live(entry)) { child.show(); child.focus(); } });
    const url = new URL(address);
    // The query names the page; the friend arrives as the context once the host page has loaded.
    url.search = new URLSearchParams({ plugin: scope.plugin, page: scope.page, independent: "1" }).toString();
    void child.loadURL(url.href).catch(() => destroy(entry));
    publish();
    return { opened: true };
  }
  function setTheme(value) {
    if (!Object.hasOwn(THEMES, value)) return;
    theme = value;
    for (const entry of windows.values()) {
      if (entry.window.isDestroyed()) continue;
      entry.window.setBackgroundColor(THEMES[value]);
      toPage(entry, { context: context(entry) });
    }
  }
  function destroyAll() {
    for (const entry of [...windows.values()]) destroy(entry);
    windows.clear();
  }
  // A reloaded or replaced main renderer will not answer earlier calls; it learns which windows are open.
  function mainNavigation(_event, _url, inPlace, isMainFrame) {
    if (isMainFrame === false || inPlace) return;
    for (const entry of windows.values()) failPending(entry, "host-timeout", "主窗口已重新载入，请重试");
  }

  ipcMain.handle("plugin-panel:open", (event, value) =>
    !closed && trustedMainFrame(event) ? open(value) : { opened: false });
  // Events for the windows of one friend (继续聊这句 quotes, completed-answer checks); they wait for the page.
  ipcMain.on("plugin-panel:event", (event, message) => {
    if (closed || !trustedMainFrame(event) || !object(message) || !small(message) || !object(message.target) ||
        !object(message.event) || typeof message.event.type !== "string") return;
    const { plugin, account, user } = message.target;
    for (const entry of windows.values()) {
      if (!live(entry) || entry.scope.plugin !== plugin || entry.scope.account !== account || entry.scope.user !== user) continue;
      if (entry.loaded) toPage(entry, { event: message.event });
      else if (entry.queued.push(message.event) > MAX_QUEUED_EVENTS) entry.queued.shift();
    }
  });
  // Windows of a removed contribution, or of an account other than the current one, close.
  ipcMain.on("plugin-panel:retain", (event, value) => {
    if (closed || !trustedMainFrame(event) || !object(value) || !Array.isArray(value.targets) || value.targets.length > 256 ||
        typeof value.account !== "string") return;
    const keep = new Set(value.targets.filter(validTarget).map(item => JSON.stringify([item.plugin, item.page])));
    for (const entry of [...windows.values()])
      if (!keep.has(JSON.stringify([entry.scope.plugin, entry.scope.page])) || (value.account && entry.scope.account !== value.account))
        destroy(entry);
  });
  // A cleared account's windows close; Core has already stopped its plugin work.
  ipcMain.on("plugin-panel:forget", (event, account) => {
    if (closed || !trustedMainFrame(event) || !validText(account, 256)) return;
    for (const entry of [...windows.values()]) if (entry.scope.account === account) destroy(entry);
  });
  // Host methods a page asks of the main window, pinned to the window's friend; answers return to that page only.
  ipcMain.on("plugin-panel:host-call", (event, call) => {
    const entry = entryFor(event);
    if (closed || !entry || !shown(entry) || !object(call) || !validText(call.id, 48) || !HOST_METHODS.has(call.method) ||
        !small(call) || mainWindow.isDestroyed() || [...pending.values()].filter(item => item.entry === entry).length >= MAX_PENDING_CALLS)
      return;
    const params = object(call.params) ? call.params : {};
    if ((params.account !== undefined && params.account !== entry.scope.account) ||
        (params.user !== undefined && params.user !== entry.scope.user)) return;
    const id = `${prefix}-${entry.serial}-${++sequence}`, original = call.id;
    const timer = setTimeout(() => {
      if (!pending.delete(id) || !entry.loaded || entry.window.isDestroyed()) return;
      entry.window.webContents.send("plugin-panel:host-result", original, { error: { code: "host-timeout", message: "主窗口没有响应，请重试" } });
    }, HOST_CALL_TIMEOUT_MS);
    timer.unref?.();
    pending.set(id, { entry, original, method: call.method, timer });
    toMain("plugin-panel:host-call", { id, plugin: entry.scope.plugin, method: call.method, params, independent: true,
      scope: { account: entry.scope.account, user: entry.scope.user, name: entry.scope.name } });
  });
  ipcMain.on("plugin-panel:host-result", (event, id, reply) => {
    if (!trustedMainFrame(event) || typeof id !== "string" || id.length > 64 || !object(reply) || !small(reply)) return;
    const waiter = pending.get(id);
    if (!waiter) return;
    pending.delete(id); clearTimeout(waiter.timer);
    const { entry } = waiter;
    if (!entry.loaded || entry.window.isDestroyed()) return;
    // The main window comes forward only once it has switched to the friend's chat.
    if (waiter.method === "view.focusChat" && !reply.error && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show(); mainWindow.focus();
    }
    entry.window.webContents.send("plugin-panel:host-result", waiter.original, reply);
  });
  ipcMain.handle("plugin-panel:copy", (event, text, activated) => {
    const entry = entryFor(event);
    const copied = !!entry && activated === true && shown(entry) && entry.window.webContents.isFocused() &&
      typeof text === "string" && !!text.trim() && text.length <= 1_000_000;
    if (copied) clipboard.writeText(text);
    return copied;
  });
  ipcMain.on("plugin-panel:hide", event => { entryFor(event)?.window.close(); });
  // A reloaded main renderer learns which windows are still open.
  mainWindow.webContents.on("did-finish-load", publish);
  mainWindow.webContents.on("did-start-navigation", mainNavigation);
  return {
    open, setTheme, destroyAll,
    count: () => [...windows.values()].filter(live).length,
    close() {
      if (closed) return;
      closed = true;
      clearTimeout(saveTimer);
      if (!mainWindow.isDestroyed()) {
        mainWindow.webContents.off("did-finish-load", publish);
        mainWindow.webContents.off("did-start-navigation", mainNavigation);
      }
      for (const channel of ["plugin-panel:event", "plugin-panel:retain", "plugin-panel:forget", "plugin-panel:host-call",
        "plugin-panel:host-result", "plugin-panel:hide"]) ipcMain.removeAllListeners(channel);
      for (const channel of ["plugin-panel:open", "plugin-panel:copy"]) ipcMain.removeHandler(channel);
      destroyAll();
    },
  };
}

module.exports = { createPluginWindows, validTarget, windowScope };
