"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Core owns scheduling and optional search independently of the active API profile.
(function () {
  const fields = {
    searchSource: "selectModelSearchSource",
    searxngUrl: "inputModelSearxngUrl",
    searchOpenPages: "inputModelSearchOpenPages",
  };
  const state = { snapshot: null, dirty: new Set(), ready: false, loading: false, saving: false,
    conflict: false, serial: 0, controller: null, initialized: false };
  const byId = id => document.getElementById(id);
  const status = message => { byId("coreModelSettingsStatus").textContent = message; };
  const read = key => key === "searchOpenPages" ? byId(fields[key]).checked : byId(fields[key]).value;
  const matches = (key, value) => read(key) === value;
  function write(key, value) {
    if (key === "searchOpenPages") byId(fields[key]).checked = value;
    else byId(fields[key]).value = String(value);
  }
  function syncControls() {
    const busy = state.loading || state.saving;
    const disabled = busy || !state.ready;
    for (const id of Object.values(fields)) byId(id).disabled = disabled;
    const searchDisabled = read("searchSource") !== "searxng";
    byId("coreSearchOptions").hidden = searchDisabled;
    byId(fields.searxngUrl).disabled = disabled || searchDisabled;
    byId(fields.searchOpenPages).disabled = disabled || searchDisabled;
    byId("btnSaveCoreModelSettings").disabled = disabled || !state.dirty.size;
    byId("btnSaveCoreModelSettings").textContent = state.conflict ? "确认保存我的更改" : "保存";
    byId("btnReloadCoreModelSettings").disabled = busy;
    byId("btnUseLatestModelSettings").hidden = !state.conflict;
    byId("btnUseLatestModelSettings").disabled = busy || !state.ready;
    byId("coreModelSettings").setAttribute("aria-busy", String(busy));
  }
  function validateSnapshot(value) {
    const settings = value?.values;
    if (!Number.isSafeInteger(value?.revision) || value.revision < 0 || !settings ||
        !["none", "searxng"].includes(settings.searchSource) || typeof settings.searxngUrl !== "string" ||
        settings.searxngUrl.length > 512 || typeof settings.searchOpenPages !== "boolean")
      throw new Error("设置响应无效");
    return { revision: value.revision, values: Object.fromEntries(Object.keys(fields).map(key => [key, settings[key]])) };
  }
  function apply(snapshot, preserveDraft) {
    const changedElsewhere = state.snapshot && state.snapshot.revision !== snapshot.revision;
    if (!preserveDraft) state.dirty.clear();
    for (const key of Object.keys(fields)) {
      if (!state.dirty.has(key)) write(key, snapshot.values[key]);
      else if (matches(key, snapshot.values[key])) state.dirty.delete(key);
    }
    state.snapshot = snapshot;
    state.ready = true;
    state.conflict = !!state.dirty.size && (state.conflict || changedElsewhere);
  }
  function draftStatus() {
    return state.conflict ? "设置已在其他窗口更新，未保存的编辑已保留。请核对后确认保存，或使用最新设置。"
      : state.dirty.size ? "有未保存的更改" : "";
  }
  async function request(body, controller) {
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/model-settings", { cache: "no-store", signal: controller.signal,
        ...(body ? { method: "POST", headers: { "Content-Type": "application/json; charset=utf-8" }, body: JSON.stringify(body) } : {}) });
      if (!response.ok) {
        let message = `HTTP ${response.status}`;
        try {
          const body = await response.json();
          if (typeof body?.message === "string" && body.message.length <= 500) message = body.message;
        } catch { /* A missing error body still leaves the HTTP status available. */ }
        const error = new Error(message); error.status = response.status; throw error;
      }
      return validateSnapshot(await response.json());
    } finally { clearTimeout(timer); }
  }
  const requestError = (error, controller) => controller.signal.aborted ? "请求超时" : error.status ? error.message : "服务暂时不可用";
  async function refresh() {
    if (!state.initialized || state.saving) return;
    state.controller?.abort();
    const serial = ++state.serial, controller = new AbortController();
    state.controller = controller; state.loading = true;
    status("读取中…"); syncControls();
    try {
      const snapshot = await request(null, controller);
      if (serial !== state.serial) return;
      apply(snapshot, true); status(draftStatus());
    } catch (error) {
      if (serial !== state.serial) return;
      state.ready = false;
      status(`读取失败（${requestError(error, controller)}），请重新读取${state.dirty.size ? "；编辑已保留" : ""}`);
    } finally {
      if (serial === state.serial) { state.loading = false; state.controller = null; syncControls(); }
    }
  }
  function collect() {
    const searchSource = read("searchSource"), searxngUrl = read("searxngUrl").trim();
    if (!["none", "searxng"].includes(searchSource)) throw new Error("请选择联网搜索来源");
    if (searxngUrl.length > 512) throw new Error("SearXNG 地址最多 512 个字符");
    if (searchSource === "searxng") {
      let url;
      try { url = new URL(searxngUrl); } catch { throw new Error("请输入有效的 SearXNG HTTP 或 HTTPS 地址"); }
      if (!/^https?:\/\//i.test(searxngUrl) || !["http:", "https:"].includes(url.protocol) || !url.hostname || url.port === "0" || url.username || url.password || url.search || url.hash ||
          /[\s\u0000-\u001f\u007f\\]/.test(searxngUrl))
        throw new Error("SearXNG 地址须为 HTTP 或 HTTPS，不能包含凭据、查询参数、片段或空白字符");
    }
    return { searchSource, searxngUrl, searchOpenPages: read("searchOpenPages") };
  }
  async function save() {
    if (!state.ready || state.loading || state.saving || !state.dirty.size) return;
    let values;
    try { values = collect(); } catch (error) { status(error.message); return; }
    const controller = new AbortController();
    state.saving = true; status("保存中…"); syncControls();
    let conflict = false;
    try {
      apply(await request({ revision: state.snapshot.revision, values }, controller), false);
      status("已保存");
    } catch (error) {
      if (error.status === 409) { state.conflict = true; conflict = true; }
      else {
        if (!error.status) state.ready = false;
        status(error.status ? `保存失败（${requestError(error, controller)}），编辑已保留`
          : `保存结果未确认（${requestError(error, controller)}），编辑已保留，请重新读取`);
      }
    } finally { state.saving = false; syncControls(); }
    if (conflict) await refresh();
  }
  function init() {
    if (state.initialized || !byId("coreModelSettings")) return;
    state.initialized = true;
    for (const [key, id] of Object.entries(fields)) {
      byId(id).addEventListener(key === "searchOpenPages" || key === "searchSource" ? "change" : "input", () => {
        if (!state.snapshot) return;
        if (matches(key, state.snapshot.values[key])) state.dirty.delete(key);
        else state.dirty.add(key);
        if (!state.dirty.size) state.conflict = false;
        status(draftStatus()); syncControls();
      });
    }
    byId("btnSaveCoreModelSettings").addEventListener("click", () => { void save(); });
    byId("btnReloadCoreModelSettings").addEventListener("click", () => { void refresh(); });
    byId("btnUseLatestModelSettings").addEventListener("click", () => {
      if (state.loading || state.saving || !state.ready) return;
      apply(state.snapshot, false); status("已使用最新设置"); syncControls();
    });
    syncControls();
  }
  window.CoreModelSettings = { init, refresh, save,
    // 模型主页的“联网搜索”行显示当前来源。
    searchSource: () => state.snapshot?.values?.searchSource || "none" };
  init();
})();
