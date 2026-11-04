"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// External chats use independent windows fixed to one account and friend.
const path = require("node:path");
const { friendScope } = require("./friend-windows.cjs");

const NAVIGATION_TARGETS = new Set(["chat", "model-settings", "models", "plugins"]);

function externalImportScope(value) {
  const friend = friendScope(value);
  if (!friend) return null;
  const { friendId } = value;
  if (friendId !== undefined && (typeof friendId !== "string" || !friendId || friendId.length > 100 ||
      /[\u0000-\u001f\u007f]/u.test(friendId))) return null;
  return { account: friend.account, user: friend.user, name: friend.name,
    ...(friendId ? { friendId } : {}), theme: value.theme === "light" ? "light" : "dark" };
}

function pluginEvent(value) {
  if (!value || value.type !== "plugins-changed" || !Array.isArray(value.plugins) || value.plugins.length > 1000) return null;
  const plugins = [];
  for (const plugin of value.plugins) {
    if (!plugin || typeof plugin.id !== "string" || !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(plugin.id) ||
        typeof plugin.enabled !== "boolean") return null;
    plugins.push({ id: plugin.id, enabled: plugin.enabled && !plugin.incompatible });
  }
  return { type: "plugins-changed", plugins };
}

function createExternalImportWindows({ BrowserWindow, mainWindow, clientUrl, screen, ipcMain, dialog, trustedMainFrame,
  openerWindow = () => mainWindow }) {
  // Modified 2026-11-04: independent chat windows open these too; navigation returns to the opener.
  const windows = new Map();
  let closed = false, closeAllPending = null;
  function settleClose(entry, ok) {
    const pending = entry.pendingClose;
    if (!pending) return;
    entry.pendingClose = null; clearTimeout(pending.timer); pending.resolve(ok);
  }
  function requestClose(entry) {
    if (entry.window.isDestroyed()) return Promise.resolve(true);
    if (entry.pendingClose) return entry.pendingClose.promise;
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    const timer = setTimeout(() => {
      settleClose(entry, false);
      if (!entry.window.isDestroyed()) { entry.window.show(); entry.window.focus(); }
    }, 5000);
    entry.pendingClose = { promise, resolve, timer };
    entry.window.close();
    return promise;
  }
  function requestCloseAll() {
    if (!closeAllPending) closeAllPending = (async () => {
      for (const entry of [...windows.values()]) if (!await requestClose(entry)) return false;
      return windows.size === 0;
    })().finally(() => { closeAllPending = null; });
    return closeAllPending;
  }
  function entryFor(event) {
    for (const entry of windows.values()) {
      const contents = entry.window.webContents;
      if (!entry.window.isDestroyed() && event.sender === contents && event.senderFrame === contents.mainFrame &&
          event.senderFrame.url === entry.url) return entry;
    }
    return null;
  }
  function open(value, opener = mainWindow) {
    const scope = externalImportScope(value);
    if (closed || closeAllPending || !scope || mainWindow.isDestroyed()) return false;
    const key = JSON.stringify([scope.account, scope.user]);
    const existing = windows.get(key);
    if (existing && !existing.window.isDestroyed()) {
      existing.opener = opener;
      // A later main-window selection must never replace this window's binding.
      existing.scope.name = scope.name;
      existing.scope.theme = scope.theme;
      existing.window.setTitle(`${scope.name} · 外部聊天`);
      if (existing.window.isMinimized()) existing.window.restore();
      existing.window.show(); existing.window.focus();
      existing.window.webContents.send("external-import:host-event", { type: "focus", ...existing.scope });
      return true;
    }
    const anchor = mainWindow.getBounds(), area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(1020, area.width), height = Math.min(780, area.height), offset = (windows.size % 8) * 28;
    const child = new BrowserWindow({
      x: Math.max(area.x, Math.min(anchor.x + 56 + offset, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + 32 + offset, area.y + area.height - height)),
      width, height, minWidth: Math.min(640, width), minHeight: Math.min(480, height),
      title: `${scope.name} · 外部聊天`, backgroundColor: scope.theme === "light" ? "#ffffff" : "#1b1b1b",
      show: false, autoHideMenuBar: true, resizable: true,
      webPreferences: { preload: path.join(__dirname, "external-import-preload.cjs"), contextIsolation: true,
        sandbox: true, nodeIntegration: false, webSecurity: true, devTools: false, session: mainWindow.webContents.session },
    });
    const target = new URL("external-import.html", clientUrl);
    target.search = new URLSearchParams(scope).toString();
    const entry = { window: child, scope, url: target.href, opener };
    windows.set(key, entry);
    const contents = child.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const channel of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
      contents.on(channel, event => event.preventDefault());
    contents.on("render-process-gone", () => { if (!child.isDestroyed()) child.destroy(); });
    contents.on("will-prevent-unload", event => {
      const choice = dialog.showMessageBoxSync(child, { type: "question", buttons: ["保留窗口", "仍然关闭"],
        defaultId: 0, cancelId: 0, message: "还有未保存的修改或尚未提交的素材",
        detail: "关闭此窗口会放弃未保存的修改，并中断尚未完成的上传。已保存的记录和已上传的素材保留。" });
      if (choice === 1) event.preventDefault();
      else settleClose(entry, false);
    });
    child.on("closed", () => { settleClose(entry, true); if (windows.get(key) === entry) windows.delete(key); });
    child.once("ready-to-show", () => { if (!closed && !child.isDestroyed()) { child.show(); child.focus(); } });
    void child.loadURL(target.href).catch(() => { if (!child.isDestroyed()) child.destroy(); });
    return true;
  }
  function destroyAll() {
    for (const entry of windows.values()) if (!entry.window.isDestroyed()) entry.window.destroy();
    windows.clear();
  }
  function broadcast(payload) {
    for (const entry of windows.values()) if (!entry.window.isDestroyed())
      entry.window.webContents.send("external-import:host-event", payload);
  }
  function setTheme(theme) {
    if (!["dark", "light"].includes(theme)) return;
    for (const entry of windows.values()) entry.scope.theme = theme;
    broadcast({ type: "theme", theme });
  }
  ipcMain.handle("external-import:open", (event, scope, activated) => {
    const opener = !closed && trustedMainFrame(event) && activated === true ? openerWindow(event) : null;
    return opener ? open(scope, opener) : false;
  });
  ipcMain.handle("external-import:context", event => entryFor(event)?.scope ?? null);
  ipcMain.handle("external-import:close", event => {
    const entry = entryFor(event);
    if (!entry) return false;
    return requestClose(entry);
  });
  ipcMain.handle("external-import:navigate", (event, target, activated) => {
    const entry = entryFor(event);
    if (!entry || activated !== true || !NAVIGATION_TARGETS.has(target) || mainWindow.isDestroyed()) return false;
    const navigate = () => {
      if (mainWindow.isDestroyed()) return false;
      // Back to the chat window that opened this one while it is still open; settings, models and plugins
      // live only in the main window.
      const host = target === "chat" && entry.opener && !entry.opener.isDestroyed() ? entry.opener : mainWindow;
      if (host.isMinimized()) host.restore();
      host.show(); host.focus();
      host.webContents.send("external-import:navigate", { target, scope: { ...entry.scope } });
      return true;
    };
    return target === "chat" ? requestClose(entry).then(ok => ok ? navigate() : false) : navigate();
  });
  ipcMain.handle("external-import:notify", (event, value) => {
    const payload = pluginEvent(value);
    if (closed || !trustedMainFrame(event) || !payload) return false;
    broadcast(payload); return true;
  });
  function close() {
    if (closed) return;
    closed = true; destroyAll();
    mainWindow.off("closed", close);
    for (const action of ["open", "context", "close", "navigate", "notify"]) ipcMain.removeHandler(`external-import:${action}`);
  }
  // The child owns its fixed context, so a main-renderer reload or crash must not discard its drafts.
  mainWindow.on("closed", close);
  return { open, close, requestCloseAll, destroyAll, setTheme, broadcast, count: () => windows.size };
}

module.exports = { createExternalImportWindows, externalImportScope };
