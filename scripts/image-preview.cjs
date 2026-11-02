"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

function imageScope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { account, user, id } = value;
  if (![account, user].every(item => typeof item === "string" && item.length > 0 &&
      item.length <= 4096 && !/[\u0000-\u001f\u007f]/u.test(item)) ||
      typeof id !== "string" || !/^[a-f0-9]{64}$/.test(id)) return null;
  return { account, user, id };
}

function createImagePreviews({ BrowserWindow, mainWindow, clientUrl, screen }) {
  const windows = new Map();
  function closeAll() {
    for (const preview of windows.values()) if (!preview.isDestroyed()) preview.destroy();
    windows.clear();
  }
  function open(value) {
    const scope = imageScope(value);
    if (!scope || mainWindow.isDestroyed()) return false;
    const key = JSON.stringify(scope);
    const existing = windows.get(key);
    if (existing && !existing.isDestroyed()) {
      if (existing.isMinimized()) existing.restore();
      existing.show();
      existing.focus();
      return true;
    }
    const area = screen.getDisplayMatching(mainWindow.getBounds()).workArea;
    const width = Math.min(960, area.width), height = Math.min(760, area.height);
    const preview = new BrowserWindow({
      width, height, minWidth: Math.min(360, width), minHeight: Math.min(280, height),
      x: Math.round(area.x + (area.width - width) / 2),
      y: Math.round(area.y + (area.height - height) / 2),
      title: "图片 · 知己微信助手", backgroundColor: "#171717", show: false,
      autoHideMenuBar: true, resizable: true,
      webPreferences: {
        contextIsolation: true, sandbox: true, nodeIntegration: false,
        webSecurity: true, devTools: false,
        session: mainWindow.webContents.session,
      },
    });
    windows.set(key, preview);
    const target = new URL("image-viewer.html", clientUrl);
    target.hash = new URLSearchParams(scope).toString();
    const contents = preview.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const eventName of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
      contents.on(eventName, event => event.preventDefault());
    contents.on("before-input-event", (event, input) => {
      if (input.type === "keyDown" && input.key === "Escape") { event.preventDefault(); preview.close(); }
    });
    preview.on("closed", () => { if (windows.get(key) === preview) windows.delete(key); });
    preview.once("ready-to-show", () => {
      if (!preview.isDestroyed()) { preview.show(); preview.focus(); }
    });
    // The page gets identifiers only. The existing media endpoint validates the
    // account, conversation and issued message identity before reading any bytes.
    void preview.loadURL(target.href).catch(() => { if (!preview.isDestroyed()) preview.destroy(); });
    return true;
  }
  mainWindow.on("closed", closeAll);
  mainWindow.webContents.on("render-process-gone", closeAll);
  mainWindow.webContents.on("did-start-navigation", (_event, _url, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) closeAll();
  });
  return { open, closeAll };
}

module.exports = { createImagePreviews, imageScope };
