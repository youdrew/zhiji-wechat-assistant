"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Only diagnostic operations are exposed; credentials stay in the main process.
const { contextBridge, ipcRenderer } = require("electron");
function listen(channel, listener) {
  if (window.top !== window || typeof listener !== "function") return () => {};
  const handler = (_event, payload) => listener(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}
contextBridge.exposeInMainWorld("desktopHost", Object.freeze({
  modelTracesRequest(operation, args = {}) {
    if (window.top !== window || typeof operation !== "string") return Promise.resolve({ ok: false, error: "trace-forbidden" });
    return ipcRenderer.invoke("model-traces:request", operation, args);
  },
  onModelTracesInvalidated(listener) { return listen("model-traces:invalidated", listener); },
  copyDraft(value) {
    if (window.top !== window || !navigator.userActivation.isActive || typeof value !== "string" || !value || value.length > 1048576)
      return Promise.resolve(false);
    return ipcRenderer.invoke("model-traces-window:copy", value, true);
  },
}));
contextBridge.exposeInMainWorld("modelTracesWindow", Object.freeze({
  getContext() {
    return window.top === window ? ipcRenderer.invoke("model-traces-window:context") : Promise.resolve(null);
  },
  onContext(listener) { return listen("model-traces-window:context", listener); },
  close() { return window.top === window ? ipcRenderer.invoke("model-traces-window:close") : Promise.resolve(false); },
}));
