"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Read-only history bridge: no keys, files, plugins, update actions or arbitrary IPC.
const { contextBridge, ipcRenderer } = require("electron");
const topFrame = () => window.top === window;
contextBridge.exposeInMainWorld("historyHost", Object.freeze({
  getContext() { return topFrame() ? ipcRenderer.invoke("history:context") : Promise.resolve(null); },
  close() { return topFrame() ? ipcRenderer.invoke("history:close") : Promise.resolve(false); },
  locate(target) {
    if (!topFrame() || !navigator.userActivation.isActive || !target || typeof target !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("history:locate", target, true);
  },
  onHostEvent(listener) {
    if (!topFrame() || typeof listener !== "function") return () => {};
    const handler = (_event, value) => {
      if (value && ["focus", "theme", "service-restored"].includes(value.type)) listener(value);
    };
    ipcRenderer.on("history:host-event", handler);
    return () => ipcRenderer.removeListener("history:host-event", handler);
  },
}));
contextBridge.exposeInMainWorld("desktopHost", Object.freeze({
  openImagePreview(scope) {
    if (!topFrame() || !navigator.userActivation.isActive || !scope || typeof scope !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("history:image", scope, true);
  },
}));
