"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const { contextBridge, ipcRenderer } = require("electron");
const methods = new Set(["self-profile.read", "self-profile.save", "model.status", "settings.open", "view.focusChat", "toast"]);
const denied = () => Promise.resolve({ error: { code: "context-changed", message: "当前页面不可用" } });
contextBridge.exposeInMainWorld("selfAnalysisHost", Object.freeze({
  getContext() {
    return window.top === window ? ipcRenderer.invoke("self-analysis:context") : Promise.resolve(null);
  },
  onMessage(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, value) => { if (value && typeof value === "object") listener(value); };
    ipcRenderer.on("self-analysis:message", handler);
    return () => ipcRenderer.removeListener("self-analysis:message", handler);
  },
  hostCall(method, params, scope) {
    if (window.top !== window || !methods.has(method) || !params || typeof params !== "object") return denied();
    return ipcRenderer.invoke("self-analysis:host-call", method, params, scope);
  },
  copy(text, scope) {
    if (window.top !== window || !navigator.userActivation.isActive || typeof text !== "string") return Promise.resolve(false);
    return ipcRenderer.invoke("self-analysis:copy", text, scope, true);
  },
}));
