"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Minimal host bridge for 知己分析 windows: no keys, files, network or arbitrary IPC.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("friendHost", Object.freeze({
  onBeforeClose(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, token) => { if (typeof token === "string" && /^[a-f0-9]{32}$/.test(token)) listener(token); };
    ipcRenderer.on("friend:before-close", handler);
    return () => ipcRenderer.removeListener("friend:before-close", handler);
  },
  closeReady(token, ok) {
    if (window.top === window && typeof token === "string") ipcRenderer.send("friend:close-ready", token, ok === true);
  },
  onHostEvent(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, payload) => { if (payload && typeof payload.type === "string") listener(payload); };
    ipcRenderer.on("friend:host-event", handler);
    return () => ipcRenderer.removeListener("friend:host-event", handler);
  },
  locate(target) {
    if (window.top !== window || !navigator.userActivation.isActive || !target || typeof target !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("friend:locate", target, true);
  },
}));
