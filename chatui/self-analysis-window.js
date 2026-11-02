"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The account comes only from the native host; the iframe stays opaque/sandboxed.
(() => {
  const bridge = window.selfAnalysisHost, container = document.getElementById("selfAnalysisFrame");
  const failure = () => Object.assign(new Error("当前账号或页面已变化，请重试"), { code: "context-changed" });
  let context = null, epoch = -1, handle = null, serial = 0, enabled = null, listing = null;
  const current = (account, version) => !!context && context.account === account && epoch === version;
  function empty(text, retry = false) {
    handle?.destroy(); handle = null;
    const note = document.createElement("div"), label = document.createElement("p");
    note.className = "self-analysis-empty"; label.textContent = text; note.appendChild(label);
    if (retry) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = "重新载入";
      button.addEventListener("click", () => { void refresh(); }); note.appendChild(button);
    }
    container.replaceChildren(note);
  }
  async function forward(method, params) {
    const account = context?.account, version = epoch;
    if (!context || (method.startsWith("self-profile.") && !account)) throw failure();
    const reply = await bridge.hostCall(method, params, { account, epoch: version });
    if (!current(account, version)) throw failure();
    if (reply?.error) throw Object.assign(new Error(reply.error.message || "请求失败"), { code: reply.error.code || "error" });
    return reply?.result ?? null;
  }
  function hostFor(account, version) {
    const host = Object.fromEntries(["self-profile.read", "self-profile.save", "model.status", "settings.open", "view.focusChat", "toast"]
      .map(method => [method, params => {
        if (!current(account, version)) throw failure();
        return forward(method, params);
      }]));
    host["clipboard.copy"] = async params => {
      if (!current(account, version)) throw failure();
      const copied = !!account && await bridge.copy(params.text, { account, epoch: version }) === true;
      return { copied: copied && current(account, version) };
    };
    return host;
  }
  function stopListing() {
    if (!listing) return;
    clearTimeout(listing.timer); listing.controller.abort(); listing = null;
  }
  async function refresh() {
    const version = epoch, account = context?.account, request = ++serial;
    stopListing();
    handle?.destroy(); handle = null;
    if (enabled === false) { empty("自我剖析已停用，请在主窗口的设置 → 插件中启用。", true); return; }
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    listing = { controller, timer };
    try {
      const response = await fetch("/api/plugins", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error();
      const listing = await response.json();
      if (request !== serial || !current(account, version)) return;
      const plugin = listing?.plugins?.find(value => value?.id === "kyf.self-analysis");
      if (!plugin || plugin.enabled !== true || plugin.incompatible || plugin.dependencyError) { empty("自我剖析已停用或依赖不可用，请在主窗口的设置 → 插件中检查。", true); return; }
      const created = window.KyfFrames.create({ container, plugin: "kyf.self-analysis", page: "ui/index.html",
        title: "自我剖析", className: "self-analysis-frame", context, host: hostFor(account, version),
        async rpc(plugin, method, params, scope) {
          if (!account || !current(account, version) || scope?.account !== account) throw failure();
          const result = await window.KyfFrames.rpc(plugin, method, params, { ...scope, user: "" });
          if (!current(account, version)) throw failure();
          return result;
        },
        onTimeout() { if (handle === created) empty("自我剖析页面没有响应，可以重新载入。", true); },
      });
      handle = created;
    } catch { if (request === serial && current(account, version)) empty("本地服务暂时无法连接，可以重新载入。", true); }
    finally { clearTimeout(timer); if (listing?.controller === controller) listing = null; }
  }
  function receive(message) {
    if (!message || !Number.isSafeInteger(message.epoch) || message.epoch < epoch || !message.context ||
        typeof message.context.account !== "string") return;
    const reset = message.epoch !== epoch || context?.account !== message.context.account;
    epoch = message.epoch;
    enabled = typeof message.enabled === "boolean" ? message.enabled : null;
    context = { account: message.context.account, user: "", theme: message.context.theme === "light" ? "light" : "dark", surface: "self-analysis-window" };
    document.body.dataset.theme = context.theme;
    if (reset) { serial++; handle?.destroy(); handle = null; void refresh(); }
    else { handle?.post(context); if (message.event) handle?.event(message.event); }
  }
  bridge?.onMessage(receive);
  if (bridge) void bridge.getContext().then(receive).catch(() => empty("自我剖析窗口暂不可用，请重新打开。"));
  else empty("请在桌面客户端中打开自我剖析。");
  window.addEventListener("pagehide", () => { serial++; stopListing(); handle?.destroy(); handle = null; });
})();
