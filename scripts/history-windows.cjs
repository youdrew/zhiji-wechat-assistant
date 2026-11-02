"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Independent, read-only chat history windows, fixed to one account and conversation.
const path = require("node:path");
const { imageScope } = require("./image-preview.cjs");
const { locateTarget } = require("./friend-windows.cjs");

function historyScope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { account, user, name } = value;
  const valid = (text, max) => typeof text === "string" && text.length > 0 && text.length <= max &&
    !/[\u0000-\u001f\u007f]/u.test(text);
  if (!valid(account, 256) || !valid(user, 256) ||
      (name !== undefined && name !== "" && !valid(name, 128))) return null;
  return { account, user, name: name || user, theme: value.theme === "light" ? "light" : "dark" };
}

function createHistoryWindows({ BrowserWindow, mainWindow, clientUrl, screen, ipcMain, trustedMainFrame,
  openImagePreview = () => false }) {
  const windows = new Map();
  const mainContents = mainWindow.webContents;
  let closed = false;
  function entryFor(event) {
    for (const entry of windows.values()) {
      const contents = entry.window.webContents;
      if (!entry.window.isDestroyed() && event.sender === contents && event.senderFrame === contents.mainFrame &&
          event.senderFrame.url === entry.url) return entry;
    }
    return null;
  }
  function sameConversation(entry, scope) {
    return entry && scope && scope.account === entry.scope.account && scope.user === entry.scope.user;
  }
  function open(value) {
    const scope = historyScope(value);
    if (closed || !scope || mainWindow.isDestroyed()) return false;
    const key = JSON.stringify([scope.account, scope.user]);
    const existing = windows.get(key);
    if (existing && !existing.window.isDestroyed()) {
      existing.scope = scope;
      existing.window.setTitle(`${scope.name} · 聊天记录`);
      if (existing.window.isMinimized()) existing.window.restore();
      existing.window.show(); existing.window.focus();
      existing.window.webContents.send("history:host-event", { type: "focus", ...scope });
      return true;
    }
    const anchor = mainWindow.getBounds();
    const area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(580, area.width), height = Math.min(760, area.height);
    const offset = (windows.size % 8) * 28;
    const child = new BrowserWindow({
      x: Math.max(area.x, Math.min(anchor.x + 56 + offset, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + 32 + offset, area.y + area.height - height)),
      width, height, minWidth: Math.min(500, width), minHeight: Math.min(480, height),
      title: `${scope.name} · 聊天记录`, backgroundColor: scope.theme === "light" ? "#ffffff" : "#1b1b1b",
      show: false, autoHideMenuBar: true, resizable: true,
      webPreferences: {
        preload: path.join(__dirname, "history-preload.cjs"), contextIsolation: true, sandbox: true,
        nodeIntegration: false, webSecurity: true, devTools: false, session: mainContents.session,
      },
    });
    const target = new URL("history.html", clientUrl);
    target.hash = new URLSearchParams(scope).toString();
    const entry = { window: child, scope, url: target.href };
    windows.set(key, entry);
    const contents = child.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const channel of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
      contents.on(channel, event => event.preventDefault());
    contents.on("render-process-gone", () => { if (!child.isDestroyed()) child.destroy(); });
    child.on("closed", () => { if (windows.get(key) === entry) windows.delete(key); });
    child.once("ready-to-show", () => { if (!closed && !child.isDestroyed()) { child.show(); child.focus(); } });
    void child.loadURL(target.href).catch(() => { if (!child.isDestroyed()) child.destroy(); });
    return true;
  }
  function destroyAll() {
    for (const entry of windows.values()) if (!entry.window.isDestroyed()) entry.window.destroy();
    windows.clear();
  }
  function broadcast(payload) {
    for (const entry of windows.values()) if (!entry.window.isDestroyed()) entry.window.webContents.send("history:host-event", payload);
  }
  function setTheme(theme) {
    if (!["dark", "light"].includes(theme)) return;
    for (const entry of windows.values()) entry.scope.theme = theme;
    broadcast({ type: "theme", theme });
  }
  ipcMain.handle("history:open", (event, scope, activated) =>
    !closed && trustedMainFrame(event) && activated === true ? open(scope) : false);
  ipcMain.handle("history:context", event => entryFor(event)?.scope ?? null);
  ipcMain.handle("history:close", event => {
    const entry = entryFor(event);
    if (!entry) return false;
    entry.window.close(); return true;
  });
  ipcMain.handle("history:locate", (event, value, activated) => {
    const entry = entryFor(event), target = locateTarget(value);
    if (activated !== true || !sameConversation(entry, target) || mainWindow.isDestroyed()) return false;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show(); mainWindow.focus();
    mainContents.send("history:locate", target);
    return true;
  });
  ipcMain.handle("history:image", (event, value, activated) => {
    const entry = entryFor(event), scope = imageScope(value);
    return activated === true && sameConversation(entry, scope) && !mainWindow.isDestroyed() ? openImagePreview(scope) : false;
  });
  function mainNavigation(_event, _url, inPlace, isMainFrame) {
    if (isMainFrame !== false && !inPlace) destroyAll();
  }
  function close() {
    if (closed) return;
    closed = true; destroyAll();
    mainContents.off("did-start-navigation", mainNavigation);
    mainContents.off("render-process-gone", destroyAll);
    mainWindow.off("closed", close);
    for (const action of ["open", "context", "close", "locate", "image"]) ipcMain.removeHandler(`history:${action}`);
  }
  mainContents.on("did-start-navigation", mainNavigation);
  mainContents.on("render-process-gone", destroyAll);
  mainWindow.on("closed", close);
  return { open, close, destroyAll, setTheme, broadcast, count: () => windows.size };
}

module.exports = { createHistoryWindows, historyScope };
