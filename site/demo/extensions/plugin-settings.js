"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 设置 → 插件 (2026-10-13, plugin architecture): bundled plugins and auto-discovered .local/plugins with their
// enable switch and actionable problems. Enabling never starts analysis by itself.
(function () {
  const PROCESS_PROBLEMS = { unresponsive: "无响应", exited: "已退出", blocked: "已阻止", "dependency-blocked": "依赖不可用" };
  const REFRESH_MS = 3000;
  const S = { listing: null, signature: null, serial: 0, busy: new Set(), message: "", timer: null, onChange: null, initialized: false, rendered: "" };
  const byId = id => document.getElementById(id);
  function node(tag, className, text) { const value = document.createElement(tag); if (className) value.className = className; if (text !== undefined) value.textContent = text; return value; }
  function button(label, className, handler) { const value = node("button", className, label); value.type = "button"; value.addEventListener("click", handler); return value; }
  const visible = () => !!byId("settingsModal")?.classList.contains("show") && !!byId("panelPlugins")?.classList.contains("active");
  const basename = path => String(path || "").split(/[\\/]/).filter(Boolean).pop() || String(path || "");
  // What the main window shows from plugins: a change here must reach every contribution slot.
  const signature = listing => JSON.stringify((listing?.plugins || []).filter(item => item.enabled && !item.incompatible && !item.dependencyError).map(item => [item.id, item.version, item.contributes]));

  async function call(path, body) {
    const response = await fetch(path, body ? { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json; charset=utf-8" }, body: JSON.stringify(body) }
      : { cache: "no-store" });
    let value = null;
    try { value = await response.json(); } catch { value = null; }
    if (!response.ok || !value || !Array.isArray(value.plugins)) {
      const error = new Error(typeof value?.message === "string" ? value.message : "插件服务暂时不可用"); error.status = response.status; throw error;
    }
    return value;
  }
  function apply(listing) {
    const previous = S.signature;
    S.listing = listing; S.signature = signature(listing);
    render();
    window.PluginConfiguration?.onListing(listing);
    if (previous !== null && previous !== S.signature) S.onChange?.(listing);
  }
  function schedule() {
    clearTimeout(S.timer);
    S.timer = visible() ? setTimeout(() => { if (visible()) void refresh(); }, REFRESH_MS) : null;
  }
  async function refresh() {
    const serial = ++S.serial;
    try {
      const listing = await call("/api/plugins");
      if (serial !== S.serial) return;
      if (S.message === "插件列表读取失败，请重试") S.message = "";
      apply(listing);
    } catch {
      if (serial !== S.serial) return;
      S.message = "插件列表读取失败，请重试"; render();
    } finally { if (serial === S.serial) schedule(); }
  }
  async function act(id, path, body, done) {
    if (S.busy.has(id)) return;
    S.busy.add(id); S.serial++; S.message = ""; render();
    try { apply(await call(path, body)); S.message = done || ""; }
    catch (error) { S.message = error.message || "操作失败，请重试"; }
    finally { S.busy.delete(id); render(); schedule(); }
  }
  const toggle = item => act(item.id, "/api/plugins/enable", { id: item.id, enabled: !item.enabled }, `${item.name} 已${item.enabled ? "停用" : "启用"}`);
  const rescan = () => act("", "/api/plugins/refresh", {}, "已重新扫描插件目录");

  function pluginRow(item) {
    const row = node("div", "plugin-row"), main = node("div", "plugin-main"), actions = node("div", "plugin-actions");
    const busy = S.busy.has(item.id);
    const problemState = item.enabled && !item.incompatible && !item.dependencyError ? item.process?.state : null;
    const problem = PROCESS_PROBLEMS[problemState];
    const title = node("div", "plugin-name-line");
    title.append(node("strong", "", item.name), node("span", "plugin-meta", `v${item.version}`), node("span", "plugin-source", item.source === "bundled" ? "内置" : "外部"));
    main.appendChild(title);
    if (item.description) main.appendChild(node("p", "plugin-desc", item.description));
    if (item.dependencies?.length) main.appendChild(node("p", "plugin-desc plugin-dependencies", `依赖插件：${item.dependencies.join("、")}`));
    if (problem) {
      const line = node("p", "plugin-state");
      line.append(node("span", `plugin-state-label plugin-state-${problemState}`, problem));
      if (item.process.reason) line.append(node("span", "", ` · ${item.process.reason}`));
      main.appendChild(line);
    }
    if (item.incompatible) main.appendChild(node("p", "plugin-problem", `不兼容 · ${item.incompatible}；不能启用`));
    if (item.dependencyError) main.appendChild(node("p", "plugin-problem plugin-dependency-error", `依赖未满足 · ${item.dependencyError.message}`));
    const log = item.process?.log || [];
    if (log.length && problem) {
      const details = node("details", "plugin-log"); details.append(node("summary", "", "最近输出"), node("pre", "", log.join("\n"))); main.appendChild(details);
    }
    const settings = button("设置", "settings-action-btn plugin-settings-entry", () => { void window.PluginConfiguration?.open(item.id, settings); });
    settings.id = `pluginSettings-${item.id}`;
    settings.setAttribute("aria-label", `${item.name}设置`); settings.disabled = busy; actions.appendChild(settings);
    const toggleButton = node("button", "plugin-switch"); toggleButton.type = "button";
    toggleButton.setAttribute("role", "switch"); toggleButton.setAttribute("aria-checked", String(!!item.enabled));
    toggleButton.setAttribute("aria-label", `${item.enabled ? "停用" : "启用"}${item.name}`);
    toggleButton.title = item.enabled ? "停用" : item.incompatible ? "插件与当前版本不兼容" :
      item.dependencyError ? `依赖未满足 · ${item.dependencyError.message}` : "启用";
    toggleButton.disabled = busy || (!item.enabled && (!!item.incompatible || !!item.dependencyError));
    toggleButton.addEventListener("click", () => toggle(item));
    actions.appendChild(toggleButton);
    row.append(main, actions);
    return row;
  }
  function render() {
    const list = byId("pluginList"), status = byId("pluginStatus");
    if (!list || !status) return;
    // Polling must not rebuild unchanged rows: that would drop focus and swallow a click in progress.
    const key = JSON.stringify([S.listing, S.message, [...S.busy]]);
    if (key === S.rendered) return;
    S.rendered = key;
    status.textContent = S.message;
    if (byId("btnRescanPlugins")) byId("btnRescanPlugins").disabled = S.busy.has("");
    if (!S.listing) { list.replaceChildren(node("p", "model-source-status", S.message ? "" : "正在读取插件…")); return; }
    const rows = S.listing.plugins.map(pluginRow);
    if (!rows.length) rows.push(node("p", "model-source-status", "没有发现插件"));
    const problems = S.listing.problems || [];
    if (problems.length) {
      rows.push(node("div", "plugin-group-title", "未加载"));
      for (const problem of problems) {
        const row = node("div", "plugin-row plugin-row-problem"), main = node("div", "plugin-main"), title = node("div", "plugin-name-line");
        title.append(node("strong", "", basename(problem.directory)), node("span", "plugin-source", problem.source === "bundled" ? "内置" : "外部"));
        main.append(title, node("p", "plugin-problem", problem.error)); row.appendChild(main); rows.push(row);
      }
    }
    list.replaceChildren(...rows);
  }
  function init(options = {}) {
    if (S.initialized || !byId("pluginList")) return;
    S.initialized = true; S.onChange = typeof options.onChange === "function" ? options.onChange : null;
    window.PluginConfiguration?.init({ getPlugin: id => S.listing?.plugins?.find(item => item.id === id),
      onModelSettings: options.onModelSettings, onOpenNav: options.onOpenNav });
    byId("btnRescanPlugins")?.addEventListener("click", rescan);
    render();
    void refresh();
  }
  const requestLeave = action => window.PluginConfiguration ? window.PluginConfiguration.requestLeave(action) : (action?.(), true);
  const openSettings = id => window.PluginConfiguration?.open(id);
  window.PluginSettings = Object.freeze({ init, refresh, listing: () => S.listing, requestLeave, openSettings });
})();
