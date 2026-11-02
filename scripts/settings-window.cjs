"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const path = require("node:path");
const { attachModelTraces } = require("./model-traces-host.cjs");

const identifier = value => typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,63}$/i.test(value);
function settingsTarget(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const tab = value.tab ?? "general";
  if (!["general", "models", "plugins", "about"].includes(tab)) return null;
  const result = { tab };
  if (value.section !== undefined) {
    if (!["conversations", "source", "update"].includes(value.section)) return null;
    result.section = value.section;
  }
  if (value.plugin !== undefined) {
    if (!identifier(value.plugin)) return null;
    result.plugin = value.plugin;
  }
  return result;
}

function createSettingsWindow(options) {
  const { BrowserWindow, mainWindow, clientUrl, screen, ipcMain, trustedMainFrame } = options;
  const mainContents = mainWindow.webContents;
  const address = new URL(clientUrl);
  address.searchParams.set("window", "settings");
  const pageUrl = address.href;
  let child = null, traceHost = null, closed = false;
  let target = { tab: "general" };
  const trusted = event => !!child && !child.isDestroyed() && event.sender === child.webContents &&
    event.senderFrame === child.webContents.mainFrame && event.senderFrame.url === pageUrl;

  function destroy() {
    traceHost?.close(); traceHost = null;
    const previous = child; child = null;
    if (previous && !previous.isDestroyed()) previous.destroy();
  }
  function broadcast(channel, value) {
    if (child && !child.isDestroyed() && !child.webContents.isDestroyed()) child.webContents.send(channel, value);
  }
  function open(value) {
    const next = settingsTarget(value);
    if (closed || !next || mainWindow.isDestroyed()) return false;
    if (child && !child.isDestroyed()) {
      // Reopening the gear button only focuses the current panel and its drafts.
      if (value && ["tab", "section", "plugin"].some(key => value[key] !== undefined)) {
        target = next;
        broadcast("settings-window:target", target);
      }
      if (child.isMinimized()) child.restore();
      child.show(); child.focus(); return true;
    }
    target = next;
    const anchor = mainWindow.getBounds();
    const area = screen.getDisplayMatching(anchor).workArea;
    const width = Math.min(900, area.width), height = Math.min(780, area.height);
    const viewer = new BrowserWindow({
      x: Math.max(area.x, Math.min(anchor.x + 48, area.x + area.width - width)),
      y: Math.max(area.y, Math.min(anchor.y + 48, area.y + area.height - height)),
      width, height, minWidth: Math.min(680, width), minHeight: Math.min(500, height),
      title: "设置 · 知己微信助手", show: false, autoHideMenuBar: true, resizable: true,
      ...(options.root ? { icon: path.join(options.root, "chatui", "assets", "app-icon.ico") } : {}),
      backgroundColor: "#1b1b1b",
      webPreferences: {
        preload: path.join(__dirname, "real-client-preload.cjs"),
        additionalArguments: ["--kyf-settings-window", ...(options.appUpdatesEnabled ? ["--kyf-app-updates-enabled"] : [])],
        contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, devTools: false,
        session: mainContents.session,
      },
    });
    child = viewer;
    let rendererReady = false;
    const contents = viewer.webContents;
    const host = attachModelTraces({ ...options, mainWindow: viewer, trustedMainFrame: trusted });
    traceHost = host;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
      contents.on(name, event => event.preventDefault());
    contents.on("page-title-updated", event => event.preventDefault());
    contents.on("did-finish-load", () => { rendererReady = true; });
    contents.on("render-process-gone", () => { if (child === viewer) destroy(); });
    viewer.on("close", event => {
      if (!rendererReady || child !== viewer) return;
      event.preventDefault();
      contents.send("settings-window:request-close");
    });
    viewer.on("closed", () => {
      host.close();
      if (child === viewer) { child = null; traceHost = null; }
    });
    viewer.once("ready-to-show", () => { if (!closed && !viewer.isDestroyed()) { viewer.show(); viewer.focus(); } });
    void viewer.loadURL(pageUrl).catch(() => { if (child === viewer) destroy(); });
    return true;
  }

  ipcMain.handle("settings-window:open", (event, value) => trustedMainFrame(event) ? open(value) : false);
  ipcMain.handle("settings-window:target", event => trusted(event) ? target : null);
  ipcMain.handle("settings-window:close", event => {
    if (!trusted(event)) return false;
    destroy(); return true;
  });
  ipcMain.handle("settings-window:main-nav", (event, plugin, nav) => {
    if (!trusted(event) || mainWindow.isDestroyed() || mainContents.mainFrame.url !== clientUrl ||
        !identifier(plugin) || !identifier(nav)) return false;
    // Preserve the validated settings gesture across IPC without accepting a caller-supplied account.
    if (plugin === "kyf.self-analysis" && nav === "self-analysis" && typeof options.openSelfAnalysis === "function")
      return options.openSelfAnalysis() === true;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show(); mainWindow.focus();
    mainContents.send("settings-window:main-nav", { plugin, nav });
    return true;
  });
  function mainNavigation(_event, _url, inPlace, isMainFrame) {
    if (isMainFrame === false || inPlace) return;
    destroy();
  }
  function close() {
    if (closed) return;
    closed = true; destroy();
    mainContents.off("did-start-navigation", mainNavigation);
    mainContents.off("render-process-gone", destroy);
    mainWindow.off("closed", close);
    for (const action of ["open", "target", "close", "main-nav"]) ipcMain.removeHandler(`settings-window:${action}`);
  }
  mainContents.on("did-start-navigation", mainNavigation);
  mainContents.on("render-process-gone", destroy);
  mainWindow.on("closed", close);
  return { open, trusted, broadcast, close, senderWindow: event => trusted(event) ? child : null };
}

module.exports = { createSettingsWindow, settingsTarget };
