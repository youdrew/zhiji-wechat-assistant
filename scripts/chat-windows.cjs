"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Added 2026-11-04: WeChat-style independent chat windows. One conversation of one account is popped
// out of the main window into its own window (one per account + conversation); it loads the main page
// with ?window=chat and the same preload, and closes with the main window.
const fs = require("node:fs");
const path = require("node:path");
const { visibleOn } = require("./friend-windows.cjs");

const DEFAULT_SIZE = { width: 640, height: 720 };
const MIN_SIZE = { width: 420, height: 480 };
const CASCADE = 28;
// At most this many chat windows at once; another conversation is refused until one closes.
const MAX_WINDOWS = 12;
const THEMES = Object.freeze({
  dark: { color: "#1b1b1b", symbolColor: "#e6e7eb", height: 36 },
  light: { color: "#edf3f7", symbolColor: "#28333d", height: 36 },
});
const valid = (value, maximum) => typeof value === "string" && value.length > 0 && value.length <= maximum &&
  !/[\u0000-\u001f\u007f]/u.test(value);

// {account, user} of one conversation; groups (…@chatroom) are conversations too. Nothing else is kept.
function chatScope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { account, user } = value;
  return valid(account, 128) && valid(user, 256) ? { account, user } : null;
}

function createChatWindows(options) {
  const { BrowserWindow, mainWindow, clientUrl, screen, ipcMain, userDataPath, trustedMainFrame } = options;
  const themes = options.themes || THEMES;
  const mainContents = mainWindow.webContents;
  const windows = new Map();
  const boundsFile = userDataPath ? path.join(userDataPath, "chat-windows.json") : null;
  let saved = {};
  try { if (boundsFile) saved = JSON.parse(fs.readFileSync(boundsFile, "utf8")) || {}; } catch { saved = {}; }
  let saveTimer = null, closed = false, theme = "dark";
  function persist() {
    if (!boundsFile) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const entries = Object.entries(saved).slice(-200);
      try { fs.writeFileSync(boundsFile, JSON.stringify(Object.fromEntries(entries)), { mode: 0o600 }); } catch { }
    }, 300);
  }
  const keyOf = scope => JSON.stringify([scope.account, scope.user]);
  function initialBounds(key) {
    const previous = saved[key];
    if (previous && [previous.x, previous.y, previous.width, previous.height].every(Number.isFinite) &&
        visibleOn(screen.getAllDisplays(), previous)) return previous;
    const anchor = mainWindow.getBounds(), area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(DEFAULT_SIZE.width, area.width), height = Math.min(DEFAULT_SIZE.height, area.height);
    const offset = (windows.size % 8) * CASCADE;
    return {
      x: Math.max(area.x, Math.min(anchor.x + 56 + offset, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + 32 + offset, area.y + area.height - height)), width, height,
    };
  }
  // Only the exact page this window was opened with, in its main frame.
  function entryFor(event) {
    for (const entry of windows.values()) {
      if (entry.window.isDestroyed()) continue;
      const contents = entry.window.webContents;
      if (event?.sender === contents && event.senderFrame === contents.mainFrame && event.senderFrame.url === entry.url)
        return entry;
    }
    return null;
  }
  function paint(child, value) {
    if (child.isDestroyed()) return;
    child.setTitleBarOverlay?.(themes[value]);
    child.setBackgroundColor?.(themes[value].color);
  }
  function open(value) {
    const scope = chatScope(value);
    if (closed || !scope || mainWindow.isDestroyed()) return false;
    const key = keyOf(scope);
    const existing = windows.get(key);
    if (existing && !existing.window.isDestroyed()) {
      if (existing.window.isMinimized()) existing.window.restore();
      existing.window.show(); existing.window.focus();
      return true;
    }
    if ([...windows.values()].filter(entry => !entry.window.isDestroyed()).length >= MAX_WINDOWS) return false;
    const bounds = initialBounds(key);
    const child = new BrowserWindow({
      ...bounds, minWidth: Math.min(MIN_SIZE.width, bounds.width), minHeight: Math.min(MIN_SIZE.height, bounds.height),
      title: "知己微信助手", show: false, resizable: true,
      ...(options.root ? { icon: path.join(options.root, "chatui", "assets", "app-icon.ico") } : {}),
      titleBarStyle: "hidden", titleBarOverlay: themes[theme], backgroundColor: themes[theme].color,
      webPreferences: {
        preload: path.join(__dirname, "real-client-preload.cjs"),
        additionalArguments: ["--kyf-chat-window", ...(options.appUpdatesEnabled ? ["--kyf-app-updates-enabled"] : [])],
        contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, devTools: false,
        session: mainContents.session,
      },
    });
    const target = new URL("index.html", clientUrl);
    target.search = new URLSearchParams({ window: "chat", account: scope.account, user: scope.user }).toString();
    const entry = { window: child, scope, key, url: target.href };
    windows.set(key, entry);
    const contents = child.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    // The page never changes its own URL; plugin pages are the only frames it embeds (as in the main window).
    for (const name of ["will-navigate", "will-redirect", "will-attach-webview"])
      contents.on(name, event => event.preventDefault());
    const pluginPages = new URL("plugin-pages/", clientUrl).href;
    contents.on("will-frame-navigate", event => {
      if (event.isMainFrame !== false || typeof event.url !== "string" || !event.url.startsWith(pluginPages))
        event.preventDefault();
    });
    contents.on("did-finish-load", () => { if (!child.isDestroyed()) options.onLoad?.(contents); });
    contents.on("render-process-gone", () => { if (!child.isDestroyed()) child.destroy(); });
    const remember = () => {
      if (child.isDestroyed() || child.isMinimized() || child.isMaximized()) return;
      saved[key] = child.getBounds();
      persist();
    };
    child.on("resize", remember);
    child.on("move", remember);
    child.on("closed", () => {
      if (windows.get(key) === entry) windows.delete(key);
    });
    child.once("ready-to-show", () => { if (!closed && !child.isDestroyed()) { child.show(); child.focus(); } });
    void child.loadURL(target.href).catch(() => { if (!child.isDestroyed()) child.destroy(); });
    return true;
  }
  function destroyAll() {
    for (const entry of [...windows.values()]) if (!entry.window.isDestroyed()) entry.window.destroy();
    windows.clear();
  }
  function broadcast(channel, value) {
    for (const entry of windows.values()) {
      const contents = entry.window.isDestroyed() ? null : entry.window.webContents;
      if (contents && !contents.isDestroyed?.()) contents.send(channel, value);
    }
  }
  // The main window's theme recolors every chat window; a chat page's own setTheme recolors only itself.
  function setTheme(value) {
    if (!Object.hasOwn(themes, value)) return;
    theme = value;
    for (const entry of windows.values()) paint(entry.window, value);
  }
  function applyTheme(event, value) {
    const entry = entryFor(event);
    if (!entry || !Object.hasOwn(themes, value)) return false;
    paint(entry.window, value);
    return true;
  }
  ipcMain.handle("chat-window:open", (event, value) => !closed && trustedMainFrame(event) ? open(value) : false);
  function mainNavigation(_event, _url, inPlace, isMainFrame) {
    if (isMainFrame !== false && !inPlace) destroyAll();
  }
  function close() {
    if (closed) return;
    closed = true; clearTimeout(saveTimer); destroyAll();
    mainContents.off("did-start-navigation", mainNavigation);
    mainContents.off("render-process-gone", destroyAll);
    mainWindow.off("closed", close);
    ipcMain.removeHandler("chat-window:open");
  }
  mainContents.on("did-start-navigation", mainNavigation);
  mainContents.on("render-process-gone", destroyAll);
  mainWindow.on("closed", close);
  return {
    open, close, destroyAll, broadcast, setTheme, applyTheme,
    trusted: event => !!entryFor(event),
    senderWindow: event => entryFor(event)?.window ?? null,
    contents: () => [...windows.values()].filter(entry => !entry.window.isDestroyed()).map(entry => entry.window.webContents),
    count: () => [...windows.values()].filter(entry => !entry.window.isDestroyed()).length,
  };
}

module.exports = { createChatWindows, chatScope, MAX_WINDOWS };
