"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// No arbitrary IPC, filesystem, credentials, or plugin administration in this window.
const { contextBridge, ipcRenderer } = require("electron");
const topFrame = () => window.top === window;
const targets = new Set(["chat", "model-settings", "models", "plugins"]);
contextBridge.exposeInMainWorld("externalImportHost", Object.freeze({
  getContext() { return topFrame() ? ipcRenderer.invoke("external-import:context") : Promise.resolve(null); },
  close() { return topFrame() ? ipcRenderer.invoke("external-import:close") : Promise.resolve(false); },
  navigate(target) {
    if (!topFrame() || !navigator.userActivation.isActive || !targets.has(target)) return Promise.resolve(false);
    return ipcRenderer.invoke("external-import:navigate", target, true);
  },
  onHostEvent(listener) {
    if (!topFrame() || typeof listener !== "function") return () => {};
    const handler = (_event, value) => {
      if (value && ["focus", "theme", "service-restored", "plugins-changed"].includes(value.type)) listener(value);
    };
    ipcRenderer.on("external-import:host-event", handler);
    return () => ipcRenderer.removeListener("external-import:host-event", handler);
  },
}));
