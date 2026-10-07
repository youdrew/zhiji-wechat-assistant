"use strict";
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("advisorWindow", Object.freeze({
  send(envelope) {
    if (window.top !== window || !envelope || typeof envelope !== "object") return;
    ipcRenderer.send("advisor:from-window", envelope, navigator.userActivation.isActive);
  },
  receive(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, envelope) => listener(envelope);
    ipcRenderer.on("advisor:to-window", handler);
    return () => ipcRenderer.removeListener("advisor:to-window", handler);
  },
  hide() { if (window.top === window) ipcRenderer.send("advisor:hide"); },
}));
