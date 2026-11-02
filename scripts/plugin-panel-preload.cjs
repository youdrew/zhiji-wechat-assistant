"use strict";
// Moved from scripts/advisor-preload.cjs to scripts/plugin-panel-preload.cjs on 2026-10-13 (plugin architecture):
// the docked panel host receives the friend context and host events, asks the main window for host
// methods, copies with the user's gesture and collapses itself.
// Modified 2026-10-22: the same bridge serves each friend's own plugin window (scripts/plugin-windows.cjs); hide closes it.
const { contextBridge, ipcRenderer } = require("electron");
const pending = new Map();
let sequence = 0;
ipcRenderer.on("plugin-panel:host-result", (_event, id, reply) => {
  const waiter = pending.get(id);
  if (!waiter) return;
  pending.delete(id);
  clearTimeout(waiter.timer);
  waiter.resolve(reply && typeof reply === "object" ? reply : { error: { code: "error", message: "请求失败" } });
});
contextBridge.exposeInMainWorld("pluginPanel", Object.freeze({
  onMessage(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, message) => { if (message && typeof message === "object") listener(message); };
    ipcRenderer.on("plugin-panel:to-panel", handler);
    return () => ipcRenderer.removeListener("plugin-panel:to-panel", handler);
  },
  hostCall(method, params) {
    if (window.top !== window || typeof method !== "string") return Promise.resolve({ error: { code: "unknown-method", message: "宿主不支持该操作" } });
    return new Promise(resolve => {
      const id = "p" + (++sequence);
      const timer = setTimeout(() => { pending.delete(id); resolve({ error: { code: "host-timeout", message: "宿主没有响应" } }); }, 30000);
      pending.set(id, { resolve, timer });
      ipcRenderer.send("plugin-panel:host-call", { id, method, params: params && typeof params === "object" ? params : {} });
    });
  },
  copy(text) {
    if (window.top !== window || typeof text !== "string") return Promise.resolve(false);
    return ipcRenderer.invoke("plugin-panel:copy", text, navigator.userActivation.isActive);
  },
  hide() { if (window.top === window) ipcRenderer.send("plugin-panel:hide"); },
}));
