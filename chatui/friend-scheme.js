"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The analysis scheme shown in this window (added 2026-10-13, plugin architecture): the scheme
// picker, tabs from the scheme's manifest, the host 关系记忆 tab, and the sandboxed scheme page.
// The page reaches Core only through the calls answered here, always for this window's friend.
// Modified 2026-10-13: the frame and its message bridge are the shared plugin-frame.js.
(function () {
  const A = window.FriendApp;
  const HOST_TABS = { memories: "关系记忆" };
  const FLUSH_TIMEOUT_MS = 2000;
  const S = { schemes: [], scheme: null, tabs: [], tab: null, frame: null, flushWaiters: [], serial: 0 };
  // Sandboxed pages cannot use localStorage. The host owns these four display preferences only.
  const TREND_UNITS = ["hour", "day", "week", "month", "year"];
  const TREND_METRICS = ["overview", "messages", "turns", "chars", "reply"];
  const TREND_KEYS = { unit: "kyf-friend-trend-granularity", metric: "kyf-friend-trend-metric",
    log: "kyf-friend-trend-log", zoom: "kyf-friend-trend-zoom" };
  function trendSettings() {
    const read = key => { try { return localStorage.getItem(TREND_KEYS[key]); } catch { return null; } };
    const unit = read("unit"), metric = read("metric"), zoom = {};
    let saved = {};
    try { saved = JSON.parse(read("zoom") || "{}"); } catch { }
    for (const name of ["minute", ...TREND_UNITS]) {
      const slot = saved?.[name];
      if (typeof slot === "number" && slot >= 2 && slot <= 60) zoom[name] = slot;
    }
    return { unit: TREND_UNITS.includes(unit) ? unit : "month",
      metric: TREND_METRICS.includes(metric) ? metric : "overview", log: read("log") === "1", zoom };
  }
  function saveTrendSetting(params) {
    const keys = Object.keys(params), key = keys[0], value = params[key];
    const valid = keys.length === 1 && (key === "unit" ? TREND_UNITS.includes(value) :
      key === "metric" ? TREND_METRICS.includes(value) : key === "log" ? typeof value === "boolean" :
      key === "zoom" && value && Object.keys(value).length === 2 &&
        ["minute", ...TREND_UNITS].includes(value.unit) && typeof value.slot === "number" && value.slot >= 2 && value.slot <= 60);
    if (!valid) throw Object.assign(new Error("趋势显示设置不正确"), { code: "invalid-request" });
    const stored = key === "zoom" ? JSON.stringify({ ...trendSettings().zoom, [value.unit]: value.slot }) :
      key === "log" ? (value ? "1" : "0") : value;
    localStorage.setItem(TREND_KEYS[key], stored);
    return { ok: true };
  }
  const sameScheme = (left, right) => !!left && !!right && left.plugin === right.plugin &&
    (left.id ?? left.scheme) === (right.id ?? right.scheme);

  // -- the page's calls -------------------------------------------------------------------------------
  const pick = (value, keys) => Object.fromEntries(keys.filter(key => value?.[key] !== undefined).map(key => [key, value[key]]));
  const ownDraft = entry => typeof entry === "string" && entry.startsWith("observation:") && entry.length <= 200;
  const HOST = {
    "message.context": params => A.api("/api/friend/message?" + A.query({ id: String(params.id || "") })),
    "observations.save": params => A.api("/api/friend/observation", { method: "POST",
      body: pick(params, ["action", "id", "judgement", "note", "dimension", "expectedVersion"]) }),
    "drafts.list": async () => {
      const data = await A.api("/api/friend/drafts?" + A.query());
      return { drafts: (data.drafts || []).filter(item => ownDraft(item.entry)),
        closedGenerations: Object.fromEntries(Object.entries(data.closedGenerations || {}).filter(([entry]) => ownDraft(entry))) };
    },
    "drafts.save": params => {
      if (!ownDraft(params.entry)) throw Object.assign(new Error("暂存项不正确"), { code: "invalid-request" });
      return A.api("/api/friend/draft", { method: "POST",
        body: pick(params, ["entry", "generation", "seq", "payload", "baseVersion"]) });
    },
    "drafts.close": params => {
      if (!ownDraft(params.entry)) throw Object.assign(new Error("暂存项不正确"), { code: "invalid-request" });
      return A.api("/api/friend/draft", { method: "POST", body: { entry: params.entry, generation: params.generation, close: true } });
    },
    "drafts.flushed": () => { for (const resolve of S.flushWaiters.splice(0)) resolve(true); return { ok: true }; },
    "legacy.portrait": () => A.api("/api/legacy-portrait?" + A.query()),
    "tab.select": params => { if (S.tabs.some(tab => tab.key === params.tab)) selectTab(params.tab); return { ok: true }; },
    "trend.settings": saveTrendSetting,
    "trend.settings.get": trendSettings,
    "toast": params => { A.toast(String(params.message || "").slice(0, 120)); return { ok: true }; },
  };
  // The window's own api() adds this window's account and friend and masks on account changes.
  const rpc = async (plugin, method, params) => (await A.api("/api/plugins/rpc", { method: "POST",
    body: { plugin, method: String(method).slice(0, 64), params } })).result;
  function context() {
    return { account: A.scope.account, user: A.scope.user, name: A.scope.name, scheme: S.scheme?.id, tab: S.tab,
      theme: document.body.classList.contains("theme-light") ? "light" : "dark", surface: "scheme",
      trendSettings: trendSettings() };
  }
  function postContext() { if (S.frame && S.scheme) S.frame.post(context()); }
  function event(value) { S.frame?.event(value); }

  // -- frame ------------------------------------------------------------------------------------------------
  function loadFrame() {
    const container = A.byId("faSchemeFrame");
    S.frame?.destroy();
    const handle = window.KyfFrames.create({ container, plugin: S.scheme.plugin, page: S.scheme.page, title: S.scheme.name,
      className: "fa-scheme-frame", context: context(), host: HOST, rpc,
      onTimeout() {
        if (S.frame !== handle || A.masked) return;
        handle.destroy();
        const note = A.el("div", "fa-empty");
        note.append(A.el("p", "", "方案页面没有响应。主聊天不受影响，可以重新载入或切换方案。"),
          A.button("重新载入", "fa-plain", () => loadFrame()));
        container.replaceChildren(note);
        S.frame = null;
      } });
    S.frame = handle;
  }

  // -- tabs --------------------------------------------------------------------------------------------------
  function renderTabs() {
    const nav = A.byId("faTabs");
    nav.replaceChildren();
    for (const tab of S.tabs) {
      const button = A.button(tab.title, "", () => selectTab(tab.key), { role: "tab", "data-tab": tab.key,
        "aria-selected": String(tab.key === S.tab), "aria-controls": tab.host ? "pageMemory" : "faSchemeFrame" });
      button.tabIndex = tab.key === S.tab ? 0 : -1;
      button.addEventListener("keydown", keyEvent => {
        if (keyEvent.key !== "ArrowRight" && keyEvent.key !== "ArrowLeft") return;
        const index = S.tabs.findIndex(item => item.key === S.tab);
        const next = S.tabs[(index + (keyEvent.key === "ArrowRight" ? 1 : S.tabs.length - 1)) % S.tabs.length];
        selectTab(next.key);
        nav.querySelector(`[data-tab="${CSS.escape(next.key)}"]`)?.focus();
      });
      nav.append(button);
    }
  }
  function selectTab(key) {
    const tab = S.tabs.find(item => item.key === key) || S.tabs[0];
    if (!tab) return;
    S.tab = tab.key;
    for (const button of A.byId("faTabs").children) {
      const selected = button.dataset.tab === S.tab;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    A.byId("pageMemory").hidden = tab.host !== "memories";
    A.byId("faSchemeFrame").hidden = !!tab.host;
    if (tab.host === "memories") void window.FriendMemory.load();
    else postContext();
  }

  // -- schemes -----------------------------------------------------------------------------------------------
  function renderPicker() {
    const select = A.byId("faScheme");
    select.replaceChildren();
    for (const [index, scheme] of S.schemes.entries()) {
      const option = A.el("option", "", scheme.schemes > 1 ? `${scheme.pluginName} · ${scheme.name}` : scheme.name);
      option.value = String(index);
      option.title = scheme.summary || "";
      option.selected = sameScheme(scheme, S.scheme);
      select.append(option);
    }
    // One scheme needs no picker.
    A.byId("faSchemeLabel").hidden = S.schemes.length < 2;
  }
  function apply(scheme) {
    const previousTab = S.tab;
    S.scheme = scheme;
    S.tabs = scheme.tabs.map(tab => tab.host ? { key: tab.host, title: HOST_TABS[tab.host] || tab.host, host: tab.host } :
      { key: tab.id, title: tab.title });
    if (!S.tabs.some(tab => tab.host === "memories")) S.tabs.push({ key: "memories", title: HOST_TABS.memories, host: "memories" });
    S.tab = S.tabs.some(tab => tab.key === previousTab) ? previousTab : S.tabs[0].key;
    renderTabs();
    renderPicker();
    loadFrame();
    selectTab(S.tab);
    window.FriendRun.setScheme({ plugin: scheme.plugin, scheme: scheme.id, name: scheme.name });
  }
  async function load() {
    const serial = ++S.serial;
    const data = await A.api("/api/schemes?" + A.query());
    if (serial !== S.serial) return;
    const counts = {};
    for (const scheme of data.schemes || []) counts[scheme.plugin] = (counts[scheme.plugin] || 0) + 1;
    S.schemes = (data.schemes || []).map(scheme => ({ ...scheme, schemes: counts[scheme.plugin] }));
    const selected = S.schemes.find(scheme => sameScheme(scheme, data.selected));
    if (!selected) {
      S.frame?.destroy();
      S.frame = null;
      S.scheme = null;
      S.tabs = []; S.tab = null;
      window.FriendRun.setScheme(null);
      renderPicker();
      A.byId("faTabs").replaceChildren();
      A.byId("faSchemeFrame").hidden = false;
      A.byId("faSchemeFrame").replaceChildren(A.el("div", "fa-empty", "没有可用的分析方案。可在“设置 → 插件”中启用。"));
      A.byId("pageMemory").hidden = true;
      A.byId("faRunInfo").textContent = "";
      A.byId("faRunButton").hidden = true;
      return;
    }
    if (!sameScheme(selected, S.scheme)) apply(selected);
    else renderPicker();
  }
  // Switching only changes what this window shows: no analysis starts or stops, no result is deleted.
  A.byId("faScheme").addEventListener("change", async () => {
    const scheme = S.schemes[Number(A.byId("faScheme").value)];
    if (!scheme || sameScheme(scheme, S.scheme)) return;
    try {
      await A.api("/api/schemes/select", { method: "POST", body: { plugin: scheme.plugin, scheme: scheme.id } });
      apply(scheme);
    } catch (error) {
      A.toast(error.message || "切换失败");
      renderPicker();
    }
  });

  A.on(message => {
    if (message.type === "analysis-step") event({ type: "run-step", key: message.key });
    else if (message.type === "analysis-finished") event({ type: "run-finished", state: message.state });
    else if (message.type === "storage") postContext();
    else if (message.type === "masked") {
      S.frame?.destroy();
      S.frame = null;
    }
  });

  window.FriendScheme = {
    load, selectTab,
    get tab() { return S.tab; },
    get scheme() { return S.scheme; },
    schemeName(plugin, scheme) { return S.schemes.find(item => item.plugin === plugin && item.id === scheme)?.name; },
    refresh() { event({ type: "refresh" }); },
    historyChanged(readCount) { event({ type: "history", readCount }); },
    // Before the window closes: let the page save its unsaved observation text.
    flush() {
      if (!S.frame?.ready) return Promise.resolve(true);
      return new Promise(resolve => {
        const timer = setTimeout(() => resolve(false), FLUSH_TIMEOUT_MS);
        S.flushWaiters.push(ok => { clearTimeout(timer); resolve(ok); });
        event({ type: "before-close" });
      });
    },
  };
})();
