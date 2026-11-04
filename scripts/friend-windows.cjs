"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Independent 知己分析 windows: one per account + contact, never replacing another contact.

const fs = require("node:fs");
const path = require("node:path");
const { randomBytes } = require("node:crypto");

const DEFAULT_SIZE = { width: 960, height: 760 };
const MIN_SIZE = { width: 420, height: 480 };
const CASCADE = 32;
const CLOSE_TIMEOUT_MS = 5000;

function validName(value, maximum = 256) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value);
}

function friendScope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { account, user, name, avatar } = value;
  if (!validName(account) || !validName(user) || user.endsWith("@chatroom") ||
      (name !== undefined && name !== "" && !validName(name, 128)) ||
      (avatar !== undefined && !(typeof avatar === "string" && avatar.length <= 512 && /^https?:\/\/[^\s"'<>]+$/i.test(avatar))))
    return null;
  return { account, user, name: typeof name === "string" && name ? name : user, ...(avatar ? { avatar } : {}) };
}

function locateTarget(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { account, user, messageId, cursor } = value;
  if (![account, user, messageId].every(item => validName(item)) || !validName(cursor, 2048)) return null;
  return { account, user, messageId, cursor };
}

function visibleOn(displays, bounds) {
  return displays.some(({ workArea: area }) => bounds.x < area.x + area.width - 80 && bounds.x + bounds.width > area.x + 80 &&
    bounds.y >= area.y - 8 && bounds.y < area.y + area.height - 80);
}

function createFriendWindows(options) {
  const { BrowserWindow, mainWindow, clientUrl, screen, ipcMain, dialog, userDataPath, trustedMainFrame } = options;
  // Modified 2026-11-04: an independent chat window may open the window too; locate returns to the opener.
  const openerWindow = options.openerWindow || (() => mainWindow);
  const windows = new Map();
  const boundsFile = userDataPath ? path.join(userDataPath, "friend-windows.json") : null;
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
  const keyOf = scope => JSON.stringify([scope.account, scope.user]);
  function initialBounds(key) {
    const displays = screen.getAllDisplays();
    const previous = saved[key];
    if (previous && [previous.x, previous.y, previous.width, previous.height].every(Number.isFinite) &&
        visibleOn(displays, previous)) return previous;
    const anchor = mainWindow.isDestroyed() ? null : mainWindow.getBounds();
    const area = anchor ? screen.getDisplayMatching(anchor).workArea : screen.getPrimaryDisplay().workArea;
    const width = Math.min(DEFAULT_SIZE.width, area.width), height = Math.min(DEFAULT_SIZE.height, area.height);
    const offset = (windows.size % 8) * CASCADE;
    const baseX = anchor ? anchor.x + 48 : area.x + Math.round((area.width - width) / 2);
    const baseY = anchor ? anchor.y + 48 : area.y + Math.round((area.height - height) / 2);
    return {
      x: Math.max(area.x, Math.min(baseX + offset, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(baseY + offset, area.y + area.height - height)), width, height,
    };
  }
  function entryFor(contents) {
    for (const entry of windows.values()) if (!entry.window.isDestroyed() && entry.window.webContents === contents) return entry;
    return null;
  }
  function trustedFriend(event) {
    const entry = entryFor(event.sender);
    return entry && event.senderFrame === entry.window.webContents.mainFrame &&
      event.senderFrame.url.startsWith(new URL("friend.html", clientUrl).href) ? entry : null;
  }
  function requestFlush(entry) {
    if (entry.window.isDestroyed()) return Promise.resolve(true);
    const token = randomBytes(16).toString("hex");
    return new Promise(resolve => {
      const timer = setTimeout(() => { entry.pending.delete(token); resolve(false); }, CLOSE_TIMEOUT_MS);
      entry.pending.set(token, ok => { clearTimeout(timer); resolve(ok === true); });
      entry.window.webContents.send("friend:before-close", token);
    });
  }
  function open(value, opener = mainWindow) {
    const scope = friendScope(value);
    if (!scope || mainWindow.isDestroyed()) return false;
    const key = keyOf(scope);
    const existing = windows.get(key);
    if (existing && !existing.window.isDestroyed()) {
      existing.opener = opener;
      if (existing.window.isMinimized()) existing.window.restore();
      existing.window.show();
      existing.window.focus();
      existing.window.webContents.send("friend:host-event", { type: "focus" });
      return true;
    }
    const bounds = initialBounds(key);
    const friend = new BrowserWindow({
      ...bounds, minWidth: Math.min(MIN_SIZE.width, bounds.width), minHeight: Math.min(MIN_SIZE.height, bounds.height),
      title: `${scope.name} · 知己分析`, show: false, autoHideMenuBar: true, resizable: true,
      backgroundColor: "#ffffff",
      webPreferences: {
        preload: path.join(__dirname, "friend-preload.cjs"), contextIsolation: true, sandbox: true,
        nodeIntegration: false, webSecurity: true, devTools: false, session: mainWindow.webContents.session,
      },
    });
    const entry = { window: friend, scope, key, opener, pending: new Map(), allowClose: false, closing: false };
    windows.set(key, entry);
    const target = new URL("friend.html", clientUrl);
    target.hash = new URLSearchParams({ account: scope.account, user: scope.user, name: scope.name,
      ...(scope.avatar ? { avatar: scope.avatar } : {}) }).toString();
    const contents = friend.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const eventName of ["will-navigate", "will-redirect", "will-attach-webview"])
      contents.on(eventName, event => event.preventDefault());
    // Modified 2026-10-13: the only frame navigation allowed is the scheme page of a plugin.
    const pluginPages = new URL("plugin-pages/", clientUrl).href;
    contents.on("will-frame-navigate", event => {
      if (event.isMainFrame !== false || typeof event.url !== "string" || !event.url.startsWith(pluginPages))
        event.preventDefault();
    });
    const remember = () => {
      if (friend.isDestroyed() || friend.isMinimized() || friend.isMaximized()) return;
      saved[key] = friend.getBounds();
      persist();
    };
    friend.on("resize", remember);
    friend.on("move", remember);
    friend.on("close", event => {
      if (entry.allowClose || contents.isCrashed()) return;
      event.preventDefault();
      if (entry.closing) return;
      entry.closing = true;
      void requestFlush(entry).then(async ok => {
        entry.closing = false;
        if (friend.isDestroyed()) return;
        if (!ok) {
          const choice = await dialog.showMessageBox(friend, { type: "warning", buttons: ["保留窗口", "仍然关闭"],
            defaultId: 0, cancelId: 0, message: "未保存的编辑暂存失败", detail: "保留窗口后可以重试；仍然关闭会丢失尚未暂存的文字。" });
          if (friend.isDestroyed() || choice.response !== 1) return;
        }
        entry.allowClose = true;
        friend.close();
      });
    });
    friend.on("closed", () => { if (windows.get(key) === entry) windows.delete(key); });
    friend.once("ready-to-show", () => { if (!friend.isDestroyed()) { friend.show(); friend.focus(); } });
    void friend.loadURL(target.href).catch(() => { if (!friend.isDestroyed()) friend.destroy(); });
    return true;
  }
  async function flushAll() {
    const entries = [...windows.values()].filter(entry => !entry.window.isDestroyed());
    const results = await Promise.all(entries.map(requestFlush));
    return { ok: results.every(Boolean), count: entries.length };
  }
  function destroyAll() {
    for (const entry of windows.values()) {
      entry.allowClose = true;
      if (!entry.window.isDestroyed()) entry.window.destroy();
    }
    windows.clear();
  }
  function broadcast(payload) {
    for (const entry of windows.values()) if (!entry.window.isDestroyed()) entry.window.webContents.send("friend:host-event", payload);
  }
  ipcMain.handle("friend:open", (event, scope, activated) => {
    if (!trustedMainFrame(event) || activated !== true) return false;
    const opener = openerWindow(event);
    return opener ? open(scope, opener) : false;
  });
  ipcMain.on("friend:close-ready", (event, token, ok) => {
    const entry = trustedFriend(event);
    const resolve = entry && typeof token === "string" ? entry.pending.get(token) : null;
    if (!resolve) return;
    entry.pending.delete(token);
    resolve(ok === true);
  });
  ipcMain.handle("friend:locate", (event, value, activated) => {
    const entry = trustedFriend(event);
    const target = locateTarget(value);
    // A window may only locate messages of its own fixed account and contact.
    if (!entry || activated !== true || !target || target.account !== entry.scope.account ||
        target.user !== entry.scope.user || mainWindow.isDestroyed()) return false;
    // The window that opened this one (a chat window still open), otherwise the main window.
    const host = entry.opener && !entry.opener.isDestroyed() ? entry.opener : mainWindow;
    if (host.isMinimized()) host.restore();
    host.show();
    host.focus();
    host.webContents.send("friend:locate", target);
    return true;
  });
  const closeAllNow = () => destroyAll();
  mainWindow.on("closed", closeAllNow);
  mainWindow.webContents.on("render-process-gone", () => broadcast({ type: "host-gone" }));
  return {
    open, flushAll, destroyAll, broadcast,
    count: () => [...windows.values()].filter(entry => !entry.window.isDestroyed()).length,
    close() {
      clearTimeout(saveTimer);
      for (const channel of ["friend:close-ready"]) ipcMain.removeAllListeners(channel);
      ipcMain.removeHandler("friend:open");
      ipcMain.removeHandler("friend:locate");
    },
  };
}

module.exports = { createFriendWindows, friendScope, locateTarget, visibleOn };
