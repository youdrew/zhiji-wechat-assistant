"use strict";
// Modified by Know Your Friends, 2026-10-10: session-scoped companion replay and history.
// Moved from chatui/advisor-window.js to chatui/plugin-panel.js on 2026-10-13 (plugin architecture): the docked
// panel hosts one chat-toolbar plugin page (named by the query) in a sandboxed frame. RPC goes straight to Core
// with the friend of the newest context; copying and collapsing stay in the desktop host; other host methods
// are answered by the main window. Events wait until the page is ready.
// Modified 2026-10-22: the host fills one friend's own window; "panel.close" closes that window.
(() => {
  const query = new URLSearchParams(window.location?.search || "");
  const plugin = query.get("plugin") || "", page = query.get("page") || "";
  const independent = query.get("independent") === "1";
  if (independent) {
    document.body.dataset.independent = "true";
    document.title = "聊天助手";
  }
  const container = document.getElementById("pluginPanelFrame");
  const queued = [];
  let handle = null, context = null;
  async function forward(method, params) {
    const reply = await window.pluginPanel.hostCall(method, params);
    if (reply?.error) throw Object.assign(new Error(reply.error.message || "请求失败"), { code: reply.error.code || "error" });
    return reply?.result ?? null;
  }
  const host = {
    async "clipboard.copy"(params) {
      if (typeof params.text !== "string" || !params.text) return { copied: false };
      try { return { copied: await window.pluginPanel?.copy(params.text) === true }; } catch { return { copied: false }; }
    },
    "panel.close"() { window.pluginPanel?.hide(); return { ok: true }; },
    "view.focusChat": params => forward("view.focusChat", params),
    "attention.seen": params => forward("attention.seen", params),
    toast: params => forward("toast", params),
  };
  function flush() { if (handle?.ready) for (const event of queued.splice(0)) handle.event(event); }
  function start() {
    if (handle || !window.KyfFrames || !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(plugin) || !page) return;
    handle = window.KyfFrames.create({ container, plugin, page, title: "插件面板", className: "plugin-panel-frame",
      context, host, onReady: flush,
      onTimeout() {
        const own = handle;
        own?.destroy(); handle = null;
        const note = document.createElement("div"), retry = document.createElement("button");
        note.className = "plugin-panel-empty"; note.textContent = "面板页面没有响应。主聊天不受影响，可以重新载入。";
        retry.type = "button"; retry.textContent = "重新载入"; retry.addEventListener("click", start);
        note.appendChild(retry); container.replaceChildren(note);
      } });
  }
  window.pluginPanel?.onMessage(message => {
    if (message.context && typeof message.context === "object") {
      context = { ...message.context, surface: independent ? "assistant-window" : "panel" };
      if (independent) document.title = `${context.name || context.user || ""} · 聊天助手`;
      document.body.dataset.theme = context.theme === "light" ? "light" : "dark";
      if (handle) handle.post(context); else start();
    } else if (message.event && typeof message.event === "object" && typeof message.event.type === "string") {
      if (message.event.type === "account-cleared") handle?.forget(message.event.account);
      queued.push(message.event);
      if (queued.length > 16) queued.shift();
      flush();
    }
  });
})();
