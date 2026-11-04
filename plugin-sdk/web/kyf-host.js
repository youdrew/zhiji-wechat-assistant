"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Host bridge for plugin pages. A page runs in a sandboxed frame of a host window and
// reaches Core only through these messages; the host fixes the plugin, account and friend.
//
//   kyfHost.rpc(method, params, scope?) -> calls this page's own plugin (Core RPC). The optional
//                                       scope {account, user} names the friend the call was made for;
//                                       the host runs it only for its current friend or the one it just
//                                       switched away from, otherwise it fails with "context-changed".
//   kyfHost.host(method, params)     -> host capabilities. Every surface: "toast" {message}.
//                                       知己分析 window (surface "scheme"): "message.context", "observations.save",
//                                       "drafts.list", "drafts.save", "drafts.close", "drafts.flushed",
//                                       "legacy.portrait", "tab.select", "trend.settings" {unit | metric | log | zoom},
//                                       "trend.settings.get" -> {unit, metric, log, zoom}.
//                                       Trend display preferences arrive in context.trendSettings; the host
//                                       persists them and synchronizes other windows without exposing storage.
//                                       Main window nav page (surface "nav") and chat-toolbar panel (surface
//                                       "panel"), added 2026-10-13: "clipboard.copy" {text} -> {copied},
//                                       "view.focusChat" -> {ok}; nav only: "file.pick" {kind: "file" |
//                                       "directory"} -> {token, label} | null (token = one-time Core file
//                                       grant for this plugin); panel only: "panel.close" -> {ok}.
//   kyfHost.context()                -> {account, user, name, plugin, theme, surface: "nav" | "panel" | "scheme",
//                                       ...surface fields, e.g. scheme and tab in the 知己分析 window;
//                                       restricted: true in an Agent-opened read-only window}
//   kyfHost.on(listener)             -> context changes and host events ({type: "context" | "event", ...}).
//                                       Events: "account-cleared" {account}; panel: "visibility" {visible},
//                                       "quote" {account, user, messageId, text, side?: "self" | "other",
//                                       sender?}; "attention-check" {account, user, tasks:[{id,runId,threadId}]}
//                                       Panel acknowledges displayed completed tasks with host("attention.seen", {account,user,taskIds}).
//                                       nav: "shown".
//                                       Modified 2026-10-14: messages that arrive while the page's later scripts
//                                       still load (no listener yet) are kept, up to 32, and delivered in order.
(function () {
  const pending = new Map();
  const listeners = new Set();
  const early = [];
  let sequence = 0, current = null, readyResolve;
  const ready = new Promise(resolve => { readyResolve = resolve; });
  const TIMEOUT_MS = 60000;

  function post(message) { window.parent.postMessage({ kyf: 1, ...message }, "*"); }
  function call(target, method, params, scope) {
    return ready.then(() => new Promise((resolve, reject) => {
      const id = "c" + (++sequence);
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(Object.assign(new Error("宿主没有响应"), { code: "host-timeout" }));
      }, TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
      post({ type: "call", id, target, method: String(method), params: params || {},
        ...(scope && typeof scope === "object" ? { scope: { account: String(scope.account || ""), user: String(scope.user || "") } } : {}) });
    }));
  }
  window.addEventListener("message", event => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.kyf !== 1 || typeof message.type !== "string") return;
    if (message.type === "result") {
      const waiter = pending.get(message.id);
      if (!waiter) return;
      pending.delete(message.id);
      clearTimeout(waiter.timer);
      if (message.error) waiter.reject(Object.assign(new Error(message.error.message || "请求失败"),
        { code: message.error.code, status: message.error.status }));
      else waiter.resolve(message.result);
      return;
    }
    if (message.type === "context" && message.context && typeof message.context === "object") {
      current = Object.freeze({ ...message.context });
      document.documentElement.dataset.theme = current.theme === "light" ? "light" : "dark";
      readyResolve();
    }
    if (!listeners.size) { if (early.push(message) > 32) early.shift(); return; }
    drain();
    deliver(message);
  });
  function deliver(message) {
    for (const listener of listeners) {
      try { listener(message); } catch { /* one listener cannot break another */ }
    }
  }
  function drain() { for (const message of early.splice(0)) deliver(message); }
  window.kyfHost = Object.freeze({
    rpc: (method, params, scope) => call("plugin", method, params, scope),
    host: (method, params) => call("host", method, params),
    context: () => current,
    ready: () => ready,
    on(listener) {
      listeners.add(listener);
      if (early.length) setTimeout(drain, 0);
      return () => listeners.delete(listener);
    },
  });
  post({ type: "ready" });
})();
