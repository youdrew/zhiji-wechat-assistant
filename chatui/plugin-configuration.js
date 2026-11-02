"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Plugin-owned defaults, rendered by the settings host. Editing never starts a plugin or a task.
// Modified 2026-10-16: link shared model configuration to the dedicated settings page.
(function () {
  const S = { options: {}, id: null, serial: 0, controller: null, snapshot: null, draft: {}, controls: new Map(),
    saving: false, pending: null, opener: null, scrollTop: 0, unavailable: false, conflict: false, initialized: false };
  const byId = id => document.getElementById(id);
  const copy = value => JSON.parse(JSON.stringify(value));
  function node(tag, className, text) { const value = document.createElement(tag); if (className) value.className = className; if (text !== undefined) value.textContent = text; return value; }
  function button(label, action, className = "settings-action-btn") {
    const value = node("button", className, label); value.type = "button"; value.addEventListener("click", action); return value;
  }
  function identified(value, id) { value.id = id; return value; }
  const fields = schema => (schema?.groups || []).flatMap(group => group.fields || []);
  const isEditing = () => S.id !== null;
  const isDirty = () => !!S.snapshot && fields(S.snapshot.schema).some(field => S.draft[field.key] !== S.snapshot.values[field.key]);
  const scroller = () => byId("panelPlugins")?.closest(".settings-content");
  const status = message => { if (byId("pluginConfigStatus")) byId("pluginConfigStatus").textContent = message; };

  async function request(body, signal) {
    const response = await fetch(body ? "/api/plugins/settings" : `/api/plugins/settings?id=${encodeURIComponent(S.id)}`,
      { cache: "no-store", signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json; charset=utf-8" }, body: JSON.stringify(body) } : {}) });
    let result;
    try { result = await response.json(); } catch { result = null; }
    if (!response.ok) {
      const error = new Error(typeof result?.message === "string" ? result.message : "插件设置暂时不可用");
      error.status = response.status; error.code = result?.error; error.detail = result?.detail; throw error;
    }
    if (!result || result.plugin?.id !== S.id || !Number.isSafeInteger(result.revision) || result.revision < 0 ||
        !result.values || typeof result.values !== "object" || Array.isArray(result.values) ||
        (result.schema !== null && (!result.schema || result.schema.schema !== 1 || !Array.isArray(result.schema.groups) || typeof result.definition !== "string")))
      throw new Error("插件设置读取结果不正确");
    for (const field of result.plugin.incompatible ? [] : fields(result.schema)) {
      const value = result.values[field.key];
      if ((field.type === "boolean" && typeof value !== "boolean") || (field.type === "integer" && !Number.isSafeInteger(value)) ||
          (["select", "string"].includes(field.type) && typeof value !== "string")) throw new Error("插件设置字段不正确");
    }
    return result;
  }
  function cancelRead() { S.serial++; S.controller?.abort(); S.controller = null; }
  function updateState() {
    const state = byId("pluginConfigState");
    if (!state) return;
    const plugin = S.options.getPlugin?.(S.id) || S.snapshot?.plugin;
    state.textContent = S.unavailable ? "插件已移除或设置声明已变化。当前草稿保留，请返回列表重新打开。" :
      plugin?.incompatible ? `不兼容 · ${plugin.incompatible}` :
      plugin?.dependencyError ? `依赖未满足 · ${plugin.dependencyError.message}；可预先配置，依赖恢复后生效` :
      plugin?.process?.state === "dependency-blocked" ? `依赖不可用 · ${plugin.process.reason || "依赖插件尚未就绪"}；可预先配置` :
      plugin?.enabled === false ? "已停用 · 可预先配置，启用后生效" : "插件默认设置 · 应用于后续任务";
    const dependencies = byId("pluginConfigDependencies");
    if (dependencies) { dependencies.hidden = !plugin?.dependencies?.length; dependencies.textContent =
      plugin?.dependencies?.length ? `依赖插件：${plugin.dependencies.join("、")}` : ""; }
    for (const value of S.controls.values()) value.input.disabled = S.saving || S.unavailable || !!plugin?.incompatible;
    const save = byId("pluginConfigSave"), revert = byId("pluginConfigRevert"), dirty = isDirty();
    if (save) { save.disabled = !dirty || S.saving || S.unavailable || !!plugin?.incompatible; save.textContent = S.saving ? "正在保存…" : "保存更改"; }
    if (revert) revert.disabled = !dirty || S.saving;
    if (byId("pluginConfigDirty")) byId("pluginConfigDirty").textContent = dirty ? "未保存" : "已保存";
    for (const id of ["pluginLeaveSave", "pluginLeaveDiscard", "pluginLeaveStay"]) if (byId(id)) byId(id).disabled = S.saving;
    for (const link of byId("pluginDetail")?.querySelectorAll("[data-plugin-nav]") || []) {
      link.disabled = plugin?.enabled === false || !!plugin?.dependencyError || S.unavailable;
      link.title = plugin?.dependencyError?.message || "";
    }
  }
  function clearErrors() {
    for (const { input, error } of S.controls.values()) { error.textContent = ""; input.removeAttribute("aria-invalid"); }
  }
  function validate() {
    clearErrors(); let first = null;
    for (const field of fields(S.snapshot?.schema)) {
      const value = S.draft[field.key]; let message = "";
      if (field.type === "integer" && (!Number.isSafeInteger(value) ||
          (field.minimum !== undefined && value < field.minimum) || (field.maximum !== undefined && value > field.maximum))) message = "请输入范围内的整数";
      if (field.type === "string" && (typeof value !== "string" || value.length > (field.maxLength ?? 4096))) message = "内容过长，请缩短后保存";
      if (field.type === "select" && !field.options.some(option => option.value === value)) message = "请选择有效选项";
      if (message) { const control = S.controls.get(field.key); control.error.textContent = message; control.input.setAttribute("aria-invalid", "true"); first ||= control.input; }
    }
    first?.focus(); return !first;
  }
  function fieldControl(field) {
    const row = node("div", `plugin-config-field${field.type === "boolean" ? " plugin-config-boolean" : ""}`);
    const input = node(field.type === "select" ? "select" : "input", "plugin-config-input");
    input.id = `pluginConfig-${field.key}`; input.name = field.key;
    const label = node("label", "plugin-config-label", field.title); label.htmlFor = input.id;
    if (field.type === "boolean") { input.type = "checkbox"; input.checked = S.draft[field.key]; }
    else {
      if (field.type === "select") for (const option of field.options) { const value = node("option", "", option.label); value.value = option.value; input.appendChild(value); }
      else { input.type = field.type === "integer" ? "number" : "text"; if (field.minimum !== undefined) input.min = field.minimum; if (field.maximum !== undefined) input.max = field.maximum; if (field.maxLength !== undefined) input.maxLength = field.maxLength; if (field.type === "integer") input.step = "1"; }
      input.value = S.draft[field.key];
    }
    const description = identified(node("p", "plugin-config-description", field.description || ""), `${input.id}-description`);
    const error = identified(node("p", "plugin-config-error"), `${input.id}-error`); error.setAttribute("role", "alert");
    input.setAttribute("aria-describedby", `${description.id} ${error.id}`);
    const changed = () => {
      S.draft[field.key] = field.type === "boolean" ? input.checked : field.type === "integer" && input.value !== "" ? Number(input.value) : input.value;
      error.textContent = ""; input.removeAttribute("aria-invalid"); status(""); updateState();
    };
    input.addEventListener("input", changed); input.addEventListener("change", changed);
    if (field.type === "boolean") { const line = node("div", "plugin-config-check"); line.append(input, label); row.appendChild(line); }
    else row.append(label, input);
    row.append(description, error); S.controls.set(field.key, { input, error }); return row;
  }
  function shell(plugin) {
    const detail = byId("pluginDetail"); S.controls.clear();
    const backButton = identified(button("← 返回插件", back, "plugin-config-back"), "pluginConfigBack");
    const heading = node("div", "settings-title plugin-config-heading");
    heading.append(identified(node("h2", "", `${plugin?.name || "插件"}设置`), "pluginConfigTitle"),
      node("span", "plugin-meta", plugin ? `v${plugin.version} · ${plugin.source === "bundled" ? "内置" : "外部"}` : ""));
    const state = identified(node("p", "plugin-config-state"), "pluginConfigState");
    const dependencies = identified(node("p", "plugin-config-description plugin-dependencies"), "pluginConfigDependencies"); dependencies.hidden = true;
    const feedback = identified(node("p", "plugin-config-feedback"), "pluginConfigStatus"); feedback.setAttribute("role", "status"); feedback.setAttribute("aria-live", "polite");
    detail.replaceChildren(backButton, heading, dependencies, state, feedback); updateState();
    return detail;
  }
  function render() {
    const { plugin, schema } = S.snapshot, detail = shell(plugin);
    if (schema?.description) detail.appendChild(node("p", "plugin-config-description", schema.description));
    if (plugin.incompatible) { status("插件与当前应用不兼容，不能修改设置。"); return; }
    if (!schema) { detail.appendChild(node("p", "plugin-config-description", "这个插件暂无可调整的设置。")); return; }
    {
      const model = node("section", "plugin-config-group"), modelRow = node("div", "plugin-config-model");
      model.appendChild(node("h3", "", "模型"));
      modelRow.append(node("span", "plugin-config-description", "使用应用默认模型"), button("模型设置 ↗", () => requestLeave(() => S.options.onModelSettings?.()), "plugin-config-link"));
      model.appendChild(modelRow); detail.appendChild(model);
    }
    const form = identified(node("form", "plugin-config-form"), "pluginConfigForm");
    form.addEventListener("submit", event => { event.preventDefault(); void save(); });
    for (const group of schema.groups) {
      const section = node("section", "plugin-config-group"); section.appendChild(node("h3", "", group.title));
      if (group.description) section.appendChild(node("p", "plugin-config-description", group.description));
      for (const field of group.fields) section.appendChild(fieldControl(field));
      form.appendChild(section);
    }
    if (schema.links?.length) {
      const links = node("section", "plugin-config-group"); links.appendChild(node("h3", "", "相关管理"));
      for (const item of schema.links) { const link = button(`${item.label} ↗`, () => requestLeave(() => S.options.onOpenNav?.(S.snapshot?.plugin?.id || plugin.id, item.nav)), "plugin-config-link"); link.setAttribute("data-plugin-nav", item.nav); links.appendChild(link); }
      if (!plugin.enabled) links.appendChild(node("p", "plugin-config-description", "启用插件后可打开相关管理页面。"));
      form.appendChild(links);
    }
    const footer = node("div", "plugin-config-footer"), actions = node("div", "plugin-config-buttons");
    actions.append(identified(button("保存更改", () => { void save(); }, "settings-action-btn model-source-primary"), "pluginConfigSave"),
      identified(button("撤销更改", revert), "pluginConfigRevert"));
    footer.append(actions, identified(node("span", "plugin-config-description", "已保存"), "pluginConfigDirty"));
    form.append(footer, node("p", "plugin-config-description plugin-config-effective", "更改用于后续任务，正在运行的任务和已有结果保留。"));
    const reload = identified(button("重新读取设置", () => requestLeave(() => { void open(plugin.id); })), "pluginConfigReload"); reload.hidden = true;
    detail.append(form, reload); updateState();
  }
  function hideConfirmation() { if (byId("pluginLeaveConfirm")) byId("pluginLeaveConfirm").hidden = true; }
  function reset() {
    const previousId = S.id;
    cancelRead(); S.id = null; S.snapshot = null; S.draft = {}; S.controls.clear(); S.pending = null; S.unavailable = false; S.conflict = false;
    hideConfirmation(); byId("pluginDetail").hidden = true; byId("pluginOverview").hidden = false;
    const scroll = scroller(); if (scroll) scroll.scrollTop = S.scrollTop;
    if (S.opener?.isConnected) S.opener.focus({ preventScroll: true });
    else (byId(`pluginSettings-${previousId}`) || byId("pluginList")?.querySelector("button"))?.focus({ preventScroll: true });
    S.opener = null;
  }
  function requestLeave(action) {
    if (!S.id) { action?.(); return true; }
    if (S.saving) { status("正在保存，请稍候再离开。"); return false; }
    if (isDirty()) {
      S.pending = action; byId("pluginLeaveConfirm").hidden = false;
      byId("pluginLeaveStay").focus(); return false;
    }
    reset(); action?.(); return true;
  }
  function back() { return requestLeave(() => {}); }
  async function load(id, opener) {
    cancelRead(); S.id = id; S.snapshot = null; S.draft = {}; S.opener = opener || null; S.unavailable = false; S.conflict = false;
    S.scrollTop = scroller()?.scrollTop || 0;
    byId("pluginOverview").hidden = true; byId("pluginDetail").hidden = false;
    if (scroller()) scroller().scrollTop = 0;
    shell(S.options.getPlugin?.(id)); status("正在读取设置…");
    const serial = S.serial, controller = new AbortController(); S.controller = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const result = await request(null, controller.signal);
      if (serial !== S.serial || S.id !== id) return;
      S.snapshot = result; S.draft = copy(result.values); render(); byId("pluginConfigBack")?.focus({ preventScroll: true });
    } catch (error) {
      if (serial !== S.serial || S.id !== id) return;
      status(controller.signal.aborted ? "设置读取超时，请重试。" : error.message);
      byId("pluginDetail").appendChild(button("重试读取", () => { void load(id, opener); }));
    } finally { clearTimeout(timeout); if (S.controller === controller) S.controller = null; }
  }
  function open(id, opener) {
    if (S.saving) { status("正在保存，请稍候再打开其他插件。"); return Promise.resolve(false); }
    if (isDirty()) { requestLeave(() => { void load(id, opener); }); return Promise.resolve(false); }
    if (S.id) reset();
    return load(id, opener);
  }
  function revert() {
    if (!S.snapshot || S.saving) return;
    S.draft = copy(S.snapshot.values);
    for (const field of fields(S.snapshot.schema)) { const input = S.controls.get(field.key).input; if (field.type === "boolean") input.checked = S.draft[field.key]; else input.value = S.draft[field.key]; }
    clearErrors(); hideConfirmation(); S.pending = null; status(S.conflict ? "草稿已撤销，请重新读取最新设置。" : ""); updateState();
  }
  async function save() {
    if (!S.snapshot || !isDirty() || S.saving || S.unavailable || !validate()) return false;
    const id = S.id, serial = S.serial, values = copy(S.draft);
    S.saving = true; status(""); updateState();
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const result = await request({ id, revision: S.snapshot.revision, definition: S.snapshot.definition, values }, controller.signal);
      if (serial !== S.serial || S.id !== id) return false;
      S.snapshot = result; S.draft = copy(result.values);
      S.conflict = false; byId("pluginConfigReload").hidden = true;
      status(result.plugin.dependencyError ? "已保存 · 依赖恢复后生效。" : result.enabled ? "已保存 · 后续任务使用新设置。" : "已保存 · 将在启用后生效。");
      hideConfirmation(); return true;
    } catch (error) {
      if (serial !== S.serial || S.id !== id) return false;
      status(controller.signal.aborted ? "保存超时，草稿已保留。请重新读取确认保存结果。" : error.message);
      if (error.status === 409 || controller.signal.aborted) { S.conflict = true; byId("pluginConfigReload").hidden = false; }
      const errors = error.detail?.fields || error.detail?.errors;
      if (errors && typeof errors === "object") for (const [key, message] of Object.entries(errors)) { const field = S.controls.get(key); if (field && typeof message === "string") { field.error.textContent = message; field.input.setAttribute("aria-invalid", "true"); } }
      return false;
    } finally { clearTimeout(timeout); S.saving = false; updateState(); }
  }
  function onListing(listing) {
    if (!S.id) return;
    const item = listing.plugins?.find(plugin => plugin.id === S.id);
    if (!item || (S.snapshot && JSON.stringify(item.contributes?.settings || null) !== JSON.stringify(S.snapshot.schema))) S.unavailable = true;
    updateState();
  }
  function init(options = {}) {
    S.options = options;
    if (S.initialized || !byId("pluginDetail")) return;
    S.initialized = true;
    byId("pluginLeaveStay").addEventListener("click", () => { S.pending = null; hideConfirmation(); byId("pluginConfigBack")?.focus(); });
    byId("pluginLeaveDiscard").addEventListener("click", () => { const action = S.pending; reset(); action?.(); });
    byId("pluginLeaveSave").addEventListener("click", async () => { const action = S.pending; if (await save()) { reset(); action?.(); } });
  }
  window.PluginConfiguration = Object.freeze({ init, open, back, requestLeave, onListing, isEditing, isDirty });
})();
