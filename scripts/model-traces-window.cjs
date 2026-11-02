"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const path = require("node:path");
const { attachModelTraces } = require("./model-traces-host.cjs");

function viewerContext(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result = {};
  for (const key of ["account", "user"]) {
    const name = value[key] ?? "";
    if (typeof name !== "string" || name.length > 256 || /[\u0000-\u001f\u007f]/u.test(name)) return null;
    result[key] = name;
  }
  if (!result.account) result.user = "";
  result.view = typeof value.view === "string" && value.view.length <= 64 ? value.view : "chat";
  result.theme = value.theme === "light" ? "light" : "dark";
  return result;
}

function createModelTracesWindow(options) {
  const { BrowserWindow, mainWindow, clientUrl, screen, ipcMain, clipboard, trustedMainFrame } = options;
  const trustedOpenFrame = options.trustedOpenFrame || trustedMainFrame;
  const mainContents = mainWindow.webContents;
  const pageUrl = new URL("model-traces-window.html", clientUrl).href;
  let child = null, traceHost = null, closed = false;
  let context = { account: "", user: "", view: "chat", theme: "dark", revision: 0 };
  const trustedViewer = event => !!child && !child.isDestroyed() && event.sender === child.webContents &&
    event.senderFrame === child.webContents.mainFrame && event.senderFrame.url === pageUrl;

  function sync(value) {
    const next = viewerContext(value);
    if (closed || !next || mainWindow.isDestroyed()) return false;
    const accountChanged = next.account !== context.account;
    if (["account", "user", "view", "theme"].every(key => next[key] === context[key])) return true;
    context = { ...next, revision: context.revision + 1 };
    if (child && !child.isDestroyed()) {
      // Abort old account reads in the trusted process before notifying the page.
      if (accountChanged) traceHost.invalidate();
      child.webContents.send("model-traces-window:context", context);
    }
    return true;
  }
  function destroyViewer() {
    traceHost?.close(); traceHost = null;
    const previous = child; child = null;
    if (previous && !previous.isDestroyed()) previous.destroy();
  }
  function open(value) {
    if (!sync(value)) return false;
    if (child && !child.isDestroyed()) {
      if (child.isMinimized()) child.restore();
      child.show(); child.focus(); return true;
    }
    const anchor = mainWindow.getBounds();
    const area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(960, area.width), height = Math.min(760, area.height);
    const viewer = new BrowserWindow({
      x: Math.max(area.x, Math.min(anchor.x + 48, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + 48, area.y + area.height - height)),
      width, height, minWidth: Math.min(600, width), minHeight: Math.min(480, height),
      title: "模型调用 · 知己微信助手", show: false, autoHideMenuBar: true, resizable: true,
      backgroundColor: context.theme === "light" ? "#ffffff" : "#1b1b1b",
      webPreferences: {
        preload: path.join(__dirname, "model-traces-preload.cjs"), contextIsolation: true,
        sandbox: true, nodeIntegration: false, webSecurity: true, devTools: false,
        session: mainContents.session,
      },
    });
    child = viewer;
    const contents = viewer.webContents;
    const host = attachModelTraces({ ...options, mainWindow: viewer, trustedMainFrame: trustedViewer });
    traceHost = host;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
      contents.on(name, event => event.preventDefault());
    contents.on("render-process-gone", () => { if (child === viewer) destroyViewer(); });
    viewer.on("closed", () => {
      host.close();
      if (child === viewer) { child = null; traceHost = null; }
    });
    viewer.once("ready-to-show", () => { if (!closed && !viewer.isDestroyed()) { viewer.show(); viewer.focus(); } });
    void viewer.loadURL(pageUrl).catch(() => { if (child === viewer) destroyViewer(); });
    return true;
  }

  const syncFromMain = (event, value) => { if (trustedMainFrame(event)) sync(value); };
  ipcMain.handle("model-traces-window:open", (event, value, activated) =>
    !closed && trustedOpenFrame(event) && activated === true ? open(value) : false);
  ipcMain.on("model-traces-window:sync", syncFromMain);
  ipcMain.handle("model-traces-window:context", event => trustedViewer(event) ? context : null);
  ipcMain.handle("model-traces-window:close", event => {
    if (!trustedViewer(event)) return false;
    child.close(); return true;
  });
  ipcMain.handle("model-traces-window:copy", (event, value, activated) => {
    if (!trustedViewer(event) || activated !== true || typeof value !== "string" || !value || value.length > 1048576) return false;
    clipboard.writeText(value); return true;
  });
  function mainNavigation(_event, _url, inPlace, isMainFrame) {
    if (isMainFrame === false || inPlace) return;
    destroyViewer();
    context = { account: "", user: "", view: "chat", theme: context.theme, revision: context.revision + 1 };
  }
  function close() {
    if (closed) return;
    closed = true; destroyViewer();
    mainContents.off("did-start-navigation", mainNavigation);
    mainContents.off("render-process-gone", destroyViewer);
    mainWindow.off("closed", close);
    ipcMain.off("model-traces-window:sync", syncFromMain);
    for (const action of ["open", "context", "close", "copy"]) ipcMain.removeHandler(`model-traces-window:${action}`);
  }
  mainContents.on("did-start-navigation", mainNavigation);
  mainContents.on("render-process-gone", destroyViewer);
  mainWindow.on("closed", close);
  return { open, sync, close };
}

module.exports = { createModelTracesWindow, viewerContext };
