"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Host side of one plugin page (added 2026-10-13, extracted from friend-scheme.js): a sandboxed frame
// that reaches Core only through the kyf-host.js messages answered here. Calls to target "plugin" go to
// the page's own plugin through /api/plugins/rpc with the host's account and friend; calls to target
// "host" go to the methods the hosting surface offers. Used by the 知己分析 window, the main window's
// nav pages and the chat-toolbar panel.
(function () {
  const READY_TIMEOUT_MS = 10000;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const failure = (code, message, status = 0) => Object.assign(new Error(message), { code, status });

  function pageUrl(plugin, page) {
    return `/plugin-pages/${encodeURIComponent(plugin)}/${String(page).split("/").map(encodeURIComponent).join("/")}`;
  }
  // Core RPC of the page's plugin. The account and friend always come from the host's context.
  async function rpc(plugin, method, params, scope) {
    const body = { plugin, method: String(method).slice(0, 64), params: params && typeof params === "object" ? params : {} };
    if (scope?.account) body.account = scope.account;
    if (scope?.user) body.user = scope.user;
    let response;
    try {
      response = await fetch("/api/plugins/rpc", { method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw failure("network", "本地服务暂时无法连接");
    }
    let data = null;
    try { data = await response.json(); } catch { }
    if (!response.ok) throw failure(typeof data?.error === "string" && data.error ? data.error : "error",
      typeof data?.message === "string" && data.message ? data.message : `请求失败（HTTP ${response.status}）`, response.status);
    return data?.result ?? null;
  }
  const sameScope = (left, right) => !!left && !!right && (left.account || "") === (right.account || "") &&
    (left.user || "") === (right.user || "");

  // options: {container, plugin, page, title, context, host, rpc, className, onTimeout, timeoutMs}
  // -> {frame, post(context), event(value), forget(account), destroy(), ready}
  function create(options) {
    const { container, plugin, page } = options;
    const call = typeof options.rpc === "function" ? options.rpc : rpc;
    let context = { ...(options.context || {}) }, previous = null, ready = false, destroyed = false, timer = null;
    const cleared = new Set();
    const frame = document.createElement("iframe");
    frame.className = options.className || "plugin-frame";
    frame.title = options.title || plugin;
    // No same-origin access: the page cannot read the host document or call Core directly.
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.src = pageUrl(plugin, page);

    function post(message) { if (!destroyed) frame.contentWindow?.postMessage({ kyf: 1, ...message }, "*"); }
    function sendContext() { if (ready) post({ type: "context", context: { ...context, plugin } }); }
    // A call may name the account and friend it was made for. It runs only for the current context or
    // the one just replaced (a page saving its last edit right after a switch), never for another friend.
    function scopeFor(requested) {
      let scope = null;
      if (!requested || typeof requested !== "object" || sameScope(requested, context)) scope = context;
      else if (previous && sameScope(requested, previous)) scope = previous;
      // A cleared account is not served until the host shows it again (the context may lag its clear event).
      if (!scope || (scope.account && cleared.has(scope.account))) throw failure("context-changed", "当前联系人已切换，请重试");
      return scope;
    }
    async function answer(data) {
      const params = data.params && typeof data.params === "object" ? data.params : {};
      if (data.target === "plugin") return call(plugin, data.method, params, scopeFor(data.scope));
      const host = options.host || {};
      const handler = own(host, data.method) ? host[data.method] : null;
      if (typeof handler !== "function") throw failure("unknown-method", "宿主不支持该操作");
      return handler(params, context);
    }
    function onMessage(message) {
      if (destroyed || message.source !== frame.contentWindow) return;
      const data = message.data;
      if (!data || data.kyf !== 1 || typeof data.type !== "string") return;
      if (data.type === "ready") {
        ready = true;
        clearTimeout(timer);
        sendContext();
        options.onReady?.();
      } else if (data.type === "call" && typeof data.id === "string" && data.id.length <= 32 &&
                 (data.target === "plugin" || data.target === "host") && typeof data.method === "string") {
        Promise.resolve().then(() => answer(data)).then(
          result => post({ type: "result", id: data.id, result: result ?? null }),
          error => post({ type: "result", id: data.id,
            error: { code: error?.code || "error", message: error?.message || "请求失败", status: error?.status || 0 } }));
      }
    }
    window.addEventListener("message", onMessage);
    container?.replaceChildren(frame);
    timer = setTimeout(() => { if (!ready && !destroyed) options.onTimeout?.(); }, options.timeoutMs ?? READY_TIMEOUT_MS);
    return {
      frame,
      get ready() { return ready; },
      get context() { return context; },
      post(next) {
        const value = { ...(next || {}) };
        if (!sameScope(value, context)) {
          previous = cleared.has(context.account) ? null : context;
          cleared.delete(value.account);
        }
        context = value;
        sendContext();
      },
      event(value) { if (ready) post({ type: "event", event: value }); },
      // A cleared account is never served again, not even as the context just replaced, unless it is shown anew.
      forget(account) {
        if (typeof account === "string" && account) cleared.add(account);
        if (previous?.account === account) previous = null;
      },
      destroy() {
        if (destroyed) return;
        destroyed = true; ready = false;
        clearTimeout(timer);
        window.removeEventListener?.("message", onMessage);
        frame.remove?.();
      },
    };
  }
  window.KyfFrames = Object.freeze({ create, rpc, pageUrl, READY_TIMEOUT_MS });
})();
