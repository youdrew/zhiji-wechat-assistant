"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Full-ledger statistics and call records; the calendar and chart interactions are shared with relationship trends.
// A record's detail adds the request, response and errors captured in developer mode (model-traces.js).
(function () {
  const $ = id => document.getElementById(id);
  const STATUS = { ok: "成功", "http-error": "HTTP 错误", "network-error": "网络错误", timeout: "超时", "stream-error": "传输中断", cancelled: "已取消", queued: "排队中", running: "进行中" };
  const FAILED = new Set(["http-error", "network-error", "timeout", "stream-error"]), ACTIVE = new Set(["queued", "running"]);
  const STATUS_FILTERS = [["", "全部状态"], ["active", "进行中"], ...Object.entries(STATUS).filter(([key]) => !ACTIVE.has(key))];
  const DETAIL_TABS = [["overview", "概览"], ["request", "请求"], ["response", "响应"], ["events", "过程与错误"]];
  const RANGES = [["all", "全部时间"], ["today", "今天"], ["7", "最近 7 天"], ["30", "最近 30 天"]];
  const ORDERS = [["total", "按总 token"], ["requests", "按请求数"], ["input", "按输入 token"], ["output", "按输出 token"], ["recent", "按最近调用"], ["failures", "按失败次数"]];
  const METRICS = [["totalTokens", "总量"], ["totalInputTokens", "输入"], ["totalOutputTokens", "输出"], ["requests", "调用次数"]];
  const SCOPES = { self: "本人", none: "未关联好友", multi: "多好友任务" };
  const STEP_NAMES = {
    "kyf.friend-analysis": { "friend-summary": "近期概述", "friend-next": "交流建议", memory: "记忆提取", "memory-dedup": "记忆去重", "friend-chat": "聊天分析", "friend-persona-evidence": "人格证据提取", "friend-persona": "人格综合", "friend-persona-audit": "人格校验" },
    "kyf.message-insights": { analysis: "消息分析", suggestions: "回复建议", vision: "图片理解" },
    "kyf.advisor": { advisor: "助手回答", "wechat-summary": "聊天历史摘要", "discussion-summary": "讨论摘要", "discussion-compaction": "讨论压缩", "media-understanding": "媒体理解" }
  };
  const COLORS = ["--color-green", "--usage-series-blue", "--usage-series-orange", "--usage-series-purple", "--usage-series-grey"];
  const moduleColor = plugin => ({ "kyf.friend-analysis": COLORS[0], "kyf.advisor": COLORS[1], "kyf.message-insights": COLORS[2] }[plugin] ||
    COLORS[3 + ([...plugin].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 2)]);
  const defaults = () => ({ range: "all", friend: "", plugin: "", step: "", model: "", status: "", task: "" });
  const state = { initialized: false, visible: false, account: "", filters: defaults(), order: "total", view: "stats", serial: 0, friendsSerial: 0, loading: false, quiet: false,
    summary: null, baseSummary: null, friends: [], modules: [], records: [], next: null, pages: 0, error: "", friendError: "", friendsLoading: false, names: new Map(), plugins: new Map(), models: new Set(),
    setting: null, traces: null, mount: null, confirming: false, clearing: false, clearSerial: 0, feedback: "", timer: null, ticker: null, fullAt: 0, rendered: "", accountingOpen: false, expandedRecords: new Set(), recordTabs: new Map(), elapsed: [],
    unit: "day", metric: "totalTokens", zoom: {}, scroll: new Map(), selection: null, drill: null, timeline: null, chart: null, chartKey: "", chartSelection: null, expanded: new Set(), dataScope: "" };
  const calendar = () => window.KyfTimeTrend.calendar, unit = () => state.drill ? "minute" : state.unit;
  const host = async (operation, args) => { try { return window.desktopHost?.modelTracesRequest ? await window.desktopHost.modelTracesRequest(operation, args) : { ok: false }; } catch { return { ok: false }; } };
  function node(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
  function button(label, handler, className) { const item = node("button", label, className); item.type = "button"; item.addEventListener("click", handler); return item; }
  const count = value => Number.isFinite(value) ? Number(value).toLocaleString("zh-CN") : "未知";
  const when = value => Number.isFinite(value) ? new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "未知";
  const seconds = ms => `${(Math.max(0, ms) / 1000).toFixed(1)} 秒`, elapsed = record => seconds(Date.now() - record.at);
  const statusLabel = record => `${STATUS[record.status] || record.status || "未知"}${record.status === "http-error" && record.httpStatus ? ` ${record.httpStatus}` : ""}`;
  const friendLabel = (scope, user) => SCOPES[scope] || state.names.get(user) || user || "未关联好友", pluginLabel = id => state.plugins.get(id) || id;
  const stepLabel = (plugin, step) => STEP_NAMES[plugin]?.[step] || (step && step !== "__none__" ? step : "未分类步骤");
  const knownValue = (data, field, known) => data.requests && data[known] === 0 ? null : data[field];
  function since() {
    if (state.filters.range === "all") return undefined;
    const now = new Date(), day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (state.filters.range !== "today") day.setDate(day.getDate() - (Number(state.filters.range) - 1));
    return day.getTime();
  }
  function query(withFriend = true, selected = true) {
    const value = { account: state.account }, start = since(); if (start !== undefined) value.since = String(start);
    for (const key of ["plugin", "step", "model", "status", "task"]) if (state.filters[key]) value[key] = state.filters[key];
    if (withFriend && state.filters.friend) value.friend = state.filters.friend;
    if (selected && state.selection) { value.since = String(Math.max(start ?? 0, state.selection.since)); value.until = String(state.selection.until); }
    return value;
  }
  const current = (serial, account) => serial === state.serial && account === state.account && state.visible;
  // Background refreshes keep the current content in place; only reads the viewer started show progress.
  const busy = () => state.loading && !state.quiet;
  const dataScope = () => JSON.stringify([query(true, false), unit(), state.drill]);
  function resetSelection() { state.selection = null; state.drill = null; state.scroll.clear(); state.chartSelection = state.timeline ? { anchor: 0, focus: state.timeline.count - 1 } : null; }
  function reload({ keepSelection = false, detailsOnly = false } = {}) {
    if (!keepSelection) resetSelection();
    if (state.dataScope && dataScope() !== state.dataScope) {
      state.summary = state.baseSummary = state.timeline = null; state.modules = []; state.friends = []; state.records = [];
    }
    state.next = null; state.pages = 0; void load({ detailsOnly });
  }
  function saveSettings() { try { window.localStorage?.setItem("kyf.model-usage.trend", JSON.stringify({ unit: state.unit, metric: state.metric, zoom: state.zoom })); } catch { /* Optional storage. */ } }
  function readSettings() {
    try {
      const data = JSON.parse(window.localStorage?.getItem("kyf.model-usage.trend") || "null");
      if (window.KyfTimeTrend?.UNITS.some(([name]) => name === data?.unit)) state.unit = data.unit;
      if (METRICS.some(([name]) => name === data?.metric)) state.metric = data.metric;
      if (data?.zoom && typeof data.zoom === "object") for (const [name, size] of Object.entries(data.zoom)) if (Number.isFinite(size) && size > 0) state.zoom[name] = size;
    } catch { /* Defaults remain usable. */ }
  }
  async function timeline(base, serial, account) {
    if (!window.KyfTimeTrend) return { ok: false };
    const M = calendar(), now = base.cutoffMs ?? Date.now(), name = unit();
    const from = state.drill ? state.drill.since : since() ?? Math.min(base.firstAt ?? now, state.selection?.since ?? Infinity);
    const until = state.drill ? M.next(state.drill.since, "day") : now + 1;
    const bounds = [M.start(from, name)];
    while (bounds.at(-1) < until) { const next = M.next(bounds.at(-1), name); if (!(next > bounds.at(-1))) return { ok: false }; bounds.push(next); }
    if (bounds.length === 1) bounds.push(M.next(bounds[0], name));
    const args = query(true, false); if (state.drill) { args.since = String(Math.max(Number(args.since) || 0, from)); args.until = String(until); }
    const buckets = [];
    // Local-calendar boundaries include DST. Bounded chunks preserve the complete history and keep URLs small.
    for (let offset = 0; offset < bounds.length - 1; offset += 4000) {
      const jobs = [];
      for (let position = offset; position < Math.min(bounds.length - 1, offset + 4000); position += 1000)
        jobs.push(host("usage-timeline", { ...args, boundaries: bounds.slice(position, position + 1001).join(",") }));
      const results = await Promise.all(jobs); if (!current(serial, account)) return { ok: false };
      for (const result of results) { if (!result.ok || !Array.isArray(result.buckets)) return { ok: false }; buckets.push(...result.buckets); }
    }
    return buckets.length === bounds.length - 1 ? { ok: true, unit: name, bounds, buckets, count: buckets.length, inProgress: bounds.at(-1) > now, cutoffMs: now } : { ok: false };
  }
  async function load({ more = false, detailsOnly = false, refresh = false } = {}) {
    if (!state.visible) return;
    if (refresh && state.chart?.isSelecting?.()) { schedule(); return; }
    clearTimeout(state.timer); state.timer = null;
    if (!state.account) { state.summary = state.baseSummary = state.timeline = null; state.friends = []; state.modules = []; state.records = []; state.next = null; state.error = "连接微信账号后可查看用量"; render(); return; }
    const scope = dataScope(), reuse = detailsOnly && !state.loading && scope === state.dataScope && state.baseSummary && state.timeline;
    const serial = ++state.serial, account = state.account; state.loading = true; state.quiet = refresh; render();
    if (more && state.next) {
      const result = await host("usage-records", { ...query(), before: state.next.join(","), limit: "50" }); if (!current(serial, account)) return;
      state.loading = false;
      if (result.ok) { state.records = state.records.concat(result.records || []); state.next = result.next || null; state.pages++; state.error = ""; } else state.error = "调用记录读取失败，请重试";
      render(); schedule(); return;
    }
    const base = reuse ? state.baseSummary : await host("usage-summary", query(true, false)); if (!current(serial, account)) return;
    if (refresh && state.chart?.isSelecting?.()) { state.loading = false; render(); schedule(); return; }
    if (!base.ok) { state.loading = false; state.error = "用量读取失败，请重试"; render(); schedule(); return; }
    // A background refresh keeps the previous friend totals until the new ones arrive.
    if (!refresh) { state.friendsLoading = true; state.friendError = ""; }
    const friendsSerial = ++state.friendsSerial;
    const friendRequest = host("usage-friends", { ...query(false), order: state.order });
    // Names and rankings load independently; loading a records page must not
    // revoke a pending response for the same aggregate scope.
    void friendRequest.then(friends => {
      if (friendsSerial !== state.friendsSerial || account !== state.account || !state.visible) return;
      state.friendsLoading = false;
      if (!friends.ok) { state.friendError = "好友汇总读取失败，请重试"; render(); return; }
      state.friendError = ""; state.friends = friends.friends || [];
      for (const item of state.friends) if (item.user && item.name) state.names.set(item.user, item.name);
      render();
    });
    const [summary, modules, records, trend] = await Promise.all([
      state.selection ? host("usage-summary", query()) : Promise.resolve(base), host("usage-modules", query()),
      host("usage-records", { ...query(), limit: "50" }),
      reuse ? Promise.resolve(state.timeline) : timeline(base, serial, account)
    ]);
    if (!current(serial, account)) return; state.loading = false;
    if (refresh && state.chart?.isSelecting?.()) { render(); schedule(); return; }
    if (!summary.ok || !modules.ok || !records.ok || !trend?.ok) { state.error = "用量读取失败，请重试"; render(); schedule(); return; }
    state.error = ""; state.summary = summary; state.baseSummary = base; state.timeline = trend; state.dataScope = scope; state.modules = modules.modules || [];
    for (const item of [...(base.plugins || []), ...state.modules]) if (item.plugin) state.plugins.set(item.plugin, item.name || item.plugin);
    state.models = new Set((summary.models || []).filter(model => typeof model === "string" && model)); state.records = records.records || []; state.next = records.next || null; state.pages = 1;
    state.fullAt = Date.now(); render(); schedule();
  }
  // In-flight attempts refresh the first records page quickly; totals follow at the usual pace and once they finish.
  const live = () => state.view === "records" && state.records.some(record => ACTIVE.has(record.status));
  function schedule() {
    clearTimeout(state.timer); if (!state.visible) { state.timer = null; return; }
    const quick = live();
    state.timer = setTimeout(() => {
      if (!state.visible || state.pages > 1 || state.confirming || document.hidden) schedule();
      else if (quick && Date.now() - state.fullAt < 10000) void refreshRecords(); else void load({ refresh: true });
    }, quick ? 2000 : 10000);
  }
  async function refreshRecords() {
    const serial = state.serial, account = state.account;
    const result = await host("usage-records", { ...query(), limit: "50" });
    if (!current(serial, account) || state.loading || state.pages > 1) { schedule(); return; }
    if (!result.ok) { schedule(); return; }
    const records = result.records || [], before = new Map(state.records.map(record => [record.attemptId, record]));
    const finished = records.some(record => ACTIVE.has(before.get(record.attemptId)?.status) && !ACTIVE.has(record.status)) ||
      state.records.some(record => ACTIVE.has(record.status) && !records.some(item => item.attemptId === record.attemptId));
    state.records = records; state.next = result.next || null;
    for (const record of records) if (record.traceRecordId && state.expandedRecords.has(record.attemptId) && (ACTIVE.has(record.status) || ACTIVE.has(before.get(record.attemptId)?.status)))
      state.traces?.refreshTrace?.(record.traceRecordId);
    if (finished) { void load({ refresh: true }); return; }
    render(); schedule();
  }
  function options(select, values, selected) { select.replaceChildren(...values.map(([value, label]) => { const option = node("option", label); option.value = value; return option; })); select.value = selected; }
  function renderFilters() {
    const friends = [["", "全部好友"], ["self", SCOPES.self], ["none", SCOPES.none], ["multi", SCOPES.multi]];
    for (const item of state.friends) if (item.scope === "friend" && item.user) friends.push([item.user, friendLabel(item.scope, item.user)]);
    if (state.filters.friend && !friends.some(([value]) => value === state.filters.friend)) friends.push([state.filters.friend, friendLabel("friend", state.filters.friend)]);
    options($('modelUsageFriend'), friends, state.filters.friend);
    options($('modelUsagePlugin'), [["", "全部模块"], ...[...state.plugins].map(([id, name]) => [id, name])], state.filters.plugin);
    const models = [["", "全部模型"], ...[...state.models].sort().map(model => [model, model])]; if (state.filters.model && !state.models.has(state.filters.model)) models.push([state.filters.model, state.filters.model]);
    options($('modelUsageModel'), models, state.filters.model); $('modelUsageRange').value = state.filters.range; $('modelUsageStatus').value = state.filters.status; $('modelUsageOrder').value = state.order;
    const step = $('modelUsageStep'); step.replaceChildren(); step.hidden = !state.filters.step && !state.filters.task;
    if (state.filters.step) step.append(node("span", `${pluginLabel(state.filters.plugin)} / ${stepLabel(state.filters.plugin, state.filters.step)}`), button("取消步骤筛选", () => { state.filters.step = ""; reload({ keepSelection: true }); }));
    if (state.filters.task) step.append(node("span", "同一任务的全部步骤与重试"), button("取消任务筛选", () => { state.filters.task = ""; reload(); }));
  }
  function renderSummary() {
    const parent = $('modelUsageSummary'), compact = $('modelUsageCompactSummary'), footer = $('modelUsageAccounting');
    parent.replaceChildren(); compact.replaceChildren(); footer.replaceChildren(); const data = state.summary; if (!data) return;
    const tiles = node("div", undefined, "model-usage-tiles");
    for (const [label, value] of [["已知总 token", count(knownValue(data, "totalTokens", "knownTotalRequests"))], ["输入（含缓存）", count(knownValue(data, "totalInputTokens", "knownInputRequests"))], ["输出（含推理）", count(knownValue(data, "totalOutputTokens", "knownOutputRequests"))], ["调用", `${count(data.requests)} 次`]]) {
      const tile = node("div", undefined, "model-usage-tile"); tile.append(node("span", label), node("strong", value)); tiles.append(tile);
    }
    parent.append(tiles);
    const active = state.records.filter(record => ACTIVE.has(record.status)).length;
    for (const [value, label] of [...(active ? [[`${count(active)} 次`, "进行中"]] : []), [`${count(data.requests)} 次`, "调用"], [count(knownValue(data, "totalTokens", "knownTotalRequests")), "已知 token"], [`${count(data.failures)} 次`, "失败"]]) {
      const item = node("span"); item.append(node("strong", value), node("span", ` ${label}`)); compact.append(item);
    }
    const notes = [`总量完整 ${count(data.knownTotalRequests)} / ${count(data.requests)} 次`];
    if (data.missingTotalUsage) notes.push(`另有 ${count(data.missingTotalUsage)} 次调用总量未知`);
    if (data.missingInputUsage || data.missingOutputUsage) notes.push(`分项不完整：输入 ${count(data.missingInputUsage)} 次 / 输出 ${count(data.missingOutputUsage)} 次，仅计入已知部分`);
    if (data.droppedRecords) notes.push(`全局账本有 ${count(data.droppedRecords)} 次未写入（不属于当前筛选的专有缺口）`);
    const coverage = node("p", notes.join(" · "), "model-traces-note"); coverage.id = "modelUsageCoverage"; footer.append(coverage);
    const accounting = node("details", undefined, "model-usage-accounting"); accounting.open = state.accountingOpen;
    accounting.addEventListener("toggle", () => { state.accountingOpen = accounting.open; });
    accounting.append(node("summary", "统计口径"), node("p", "总量优先采用服务报告，缺失时只按明确协议规则推导。缓存属于输入，推理属于输出，已包含的部分不重复相加。部分服务总量还包含其他计数，因此总量不强制等于可见分项之和。失败、取消及每次重试已报告的消耗均计入。"), node("p", `已报告缓存读 ${count(knownValue(data, "cacheReadTokens", "knownCacheReadRequests"))}（${count(data.knownCacheReadRequests)} 次） · 缓存写 ${count(knownValue(data, "cacheWriteTokens", "knownCacheWriteRequests"))}（${count(data.knownCacheWriteRequests)} 次） · 推理 ${count(knownValue(data, "reasoningTokens", "knownReasoningRequests"))}（${count(data.knownReasoningRequests)} 次）`));
    if (Number.isFinite(data.firstAt)) accounting.append(node("p", `首条可查询记录：${new Date(data.firstAt).toLocaleString("zh-CN")}；更早未记录的调用不补算。这一日期不代表记录连续完整。`));
    if (!state.setting?.enabled) accounting.append(node("p", "请求与响应正文仅在开发者模式开启后采集（设置 → 开发者模式）。")); footer.append(accounting);
  }
  function selectionHead() {
    const head = $('modelUsageSelection'); head.replaceChildren(); const view = state.timeline; if (!view) return;
    const M = calendar(), selection = state.chartSelection || { anchor: 0, focus: view.count - 1 }, from = Math.min(selection.anchor, selection.focus), to = Math.max(selection.anchor, selection.focus);
    head.append(node("strong", state.selection ? M.title(view.bounds[from], view.bounds[to + 1], unit()) : "当前时间范围全部调用"));
    if (view.inProgress && to === view.count - 1) head.append(node("span", "含进行中的周期", "model-traces-note"));
    const fullDay = state.drill && state.selection?.since === state.drill.since && state.selection?.until === M.next(state.drill.since, "day");
    if (state.selection && !fullDay) head.append(button("查看全部范围", () => {
      state.selection = state.drill ? { since: state.drill.since, until: M.next(state.drill.since, "day") } : null;
      state.chartSelection = { anchor: 0, focus: view.count - 1 }; state.chart?.draw(); selectionHead(); reload({ keepSelection: true, detailsOnly: true });
    }));
    if (!state.drill && unit() === "day" && from === to) head.append(button("按分钟查看这一天", () => { state.drill = { since: view.bounds[from] }; state.selection = { since: view.bounds[from], until: view.bounds[from + 1] }; reload({ keepSelection: true }); }));
  }
  function renderTimeline() {
    const root = $('modelUsageTrend'), view = state.timeline, toolbar = $('modelUsageTrendControls'); toolbar.replaceChildren();
    if (state.drill) toolbar.append(button("返回", () => { state.selection = { since: state.drill.since, until: calendar().next(state.drill.since, "day") }; state.drill = null; reload({ keepSelection: true }); }), node("span", "当天 · 按分钟"));
    else for (const [name, label] of window.KyfTimeTrend?.UNITS || []) { const item = button(label, () => {
      if (state.unit === name) return;
      const now = state.baseSummary?.cutoffMs ?? Date.now();
      const at = Math.max(since() ?? 0, Math.min(state.baseSummary?.requests ? state.baseSummary.lastAt ?? now : now, now));
      const start = calendar().start(at, name); state.selection = { since: start, until: calendar().next(start, name) };
      state.unit = name; saveSettings(); reload({ keepSelection: true });
    }); item.setAttribute("aria-pressed", String(name === state.unit)); toolbar.append(item); }
    const metrics = node("div", undefined, "model-usage-metrics");
    for (const [name, label] of METRICS) { const item = button(label, () => { state.metric = name; saveSettings(); render(); }); item.setAttribute("aria-pressed", String(name === state.metric)); metrics.append(item); } toolbar.append(metrics);
    if (!view) { state.chart?.destroy(); state.chart = null; state.chartKey = ""; $('modelUsageSelection').replaceChildren(); root.replaceChildren(node("p", state.loading ? "正在读取调用趋势…" : "暂无调用趋势", "model-traces-note")); return; }
    if (view.unit !== unit()) { state.chart?.destroy(); state.chart = null; state.chartKey = ""; root.replaceChildren(node("p", "正在读取这个粒度的趋势…", "model-traces-note")); return; }
    const key = JSON.stringify([view.unit, view.bounds, view.buckets, state.metric]); if (key === state.chartKey && state.chart) { state.chart.draw(); selectionHead(); return; }
    state.chart?.destroy(); state.chart = null; state.chartKey = key; const M = calendar();
    state.chartSelection = state.selection ? { anchor: Math.max(0, Math.min(view.count - 1, M.lower(view.bounds, state.selection.since + 1) - 1)), focus: Math.max(0, Math.min(view.count - 1, M.lower(view.bounds, state.selection.until) - 1)) } : { anchor: 0, focus: view.count - 1 };
    const plugins = [...new Set(view.buckets.flatMap(bucket => (bucket.modules || []).map(item => item.plugin)))].sort();
    const series = plugins.map(plugin => ({ plugin, color: moduleColor(plugin), values: view.buckets.map(bucket => { const item = (bucket.modules || []).find(item => item.plugin === plugin); return item ? item[state.metric] ?? NaN : 0; }) }));
    const metric = METRICS.find(([name]) => name === state.metric)[1], label = state.metric === "requests" ? "调用次数（次）" : `${metric}（token，仅已知用量）`;
    if (!series.length) series.push({ color: COLORS[0], values: view.buckets.map(bucket => bucket[state.metric] ?? NaN) });
    const missingField = { totalTokens: "missingTotalUsage", totalInputTokens: "missingInputUsage", totalOutputTokens: "missingOutputUsage" }[state.metric];
    state.chart = window.KyfTimeTrend.createChart({ view, series, unit: unit(), label, classPrefix: "usage", key: state.drill ? `minute:${state.drill.since}` : unit(), initialIndex: state.drill ? view.buckets.findIndex(bucket => bucket.requests > 0) : undefined, scroll: state.scroll, zoom: state.zoom[unit()], onZoom: (name, size) => { state.zoom[name] = size; saveSettings(); }, themeElement: $('modelUsagePanel'), palette: { selection: "--usage-selection", line: "--border-color", faint: "--text-secondary", unknown: "--text-secondary" }, unknown: index => !!missingField && view.buckets[index][missingField] > 0, hasData: view.buckets.some(bucket => bucket.requests > 0), emptyText: "暂无调用", getSelection: () => state.chartSelection,
      setSelection: (selection, { preview }) => { state.chartSelection = selection; state.selection = { since: view.bounds[Math.min(selection.anchor, selection.focus)], until: view.bounds[Math.max(selection.anchor, selection.focus) + 1] }; selectionHead(); if (!preview) reload({ keepSelection: true, detailsOnly: true }); }
    });
    const legend = node("div", undefined, "model-usage-legend");
    for (const item of series) if (item.plugin) { const entry = node("span"), swatch = node("i"); swatch.style.background = `var(${item.color})`; entry.append(swatch, node("span", pluginLabel(item.plugin))); legend.append(entry); }
    const missing = missingField ? view.buckets.reduce((sum, bucket) => sum + (bucket[missingField] || 0), 0) : 0;
    if (missing) legend.append(node("span", `虚线周期有 ${count(missing)} 次用量不完整；柱子只显示已知部分`));
    root.replaceChildren(node("p", label, "model-traces-note"), state.chart.element, legend, node("p", "点击查看一个周期；拖动框选，拖到边缘可滚动；Shift 扩展选择；左右方向键、Home / End 移动；滚轮缩放，Shift + 滚轮平移。", "model-traces-note")); state.chart.draw(); selectionHead();
  }
  function renderModules() {
    const parent = $('modelUsageModules'); parent.replaceChildren(); if (!state.modules.length) { parent.append(node("p", "暂无模块调用", "model-traces-note")); return; }
    const total = state.summary?.totalTokens || 0;
    for (const item of state.modules) {
      const expanded = state.expanded.has(item.plugin), row = node("div", undefined, "model-usage-module");
      const disclosure = button(`${expanded ? "▾" : "▸"} ${pluginLabel(item.plugin)}`, () => { if (expanded) state.expanded.delete(item.plugin); else state.expanded.add(item.plugin); render(); }, "model-usage-module-name"); disclosure.setAttribute("aria-expanded", String(expanded));
      const value = knownValue(item, "totalTokens", "knownTotalRequests"), share = total && value !== null ? value / total * 100 : null;
      row.append(disclosure, node("span", `${count(value)} token${share !== null ? ` · ${share.toFixed(1)}% 已知用量` : ""}`, "model-usage-module-total"), button("只看此模块", () => { state.filters.plugin = item.plugin; state.filters.step = ""; reload({ keepSelection: true }); }, "model-usage-link"));
      const track = node("div", undefined, "model-usage-track"), fill = node("i"); fill.style.width = `${Math.min(100, Math.max(0, share || 0))}%`; track.append(fill); row.append(track);
      row.append(node("span", `输入 ${count(knownValue(item, "totalInputTokens", "knownInputRequests"))} / 输出 ${count(knownValue(item, "totalOutputTokens", "knownOutputRequests"))} · ${count(item.requests)} 次 · 平均每次 ${item.knownTotalRequests ? count(Math.round(item.totalTokens / item.knownTotalRequests)) : "未知"} token${item.missingTotalUsage ? ` · ${count(item.missingTotalUsage)} 次总量未知` : ""}`, "model-usage-module-meta"));
      if (expanded) for (const step of item.steps || []) {
        const selected = state.filters.plugin === item.plugin && state.filters.step === (step.step || "__none__");
        const detail = button("", () => { state.filters.plugin = item.plugin; state.filters.step = selected ? "" : step.step || "__none__"; reload({ keepSelection: true }); }, "model-usage-step"); detail.setAttribute("aria-pressed", String(selected));
        detail.append(node("span", stepLabel(item.plugin, step.step)), node("span", `${count(knownValue(step, "totalTokens", "knownTotalRequests"))} token · ${count(step.requests)} 次${step.missingTotalUsage ? ` · ${count(step.missingTotalUsage)} 次未知` : ""}`)); row.append(detail);
      }
      parent.append(row);
    }
  }
  function renderFriends() {
    const parent = $('modelUsageFriends'); parent.replaceChildren();
    if (state.friendsLoading) { parent.append(node("p", "正在更新好友汇总…", "model-traces-note")); return; }
    if (state.friendError) parent.append(node("p", state.friendError, "model-traces-note"));
    if (!state.friends.length) { parent.append(node("p", state.summary ? "暂无好友汇总" : "", "model-traces-note")); return; }
    for (const item of state.friends) {
      const key = item.scope === "friend" ? item.user : item.scope, selected = state.filters.friend === key;
      const row = button("", () => { state.filters.friend = selected ? "" : key; reload({ keepSelection: true }); }, "model-usage-friend"); row.setAttribute("aria-pressed", String(selected));
      row.append(node("span", friendLabel(item.scope, item.user), "model-usage-name"), node("span", `${count(item.requests)} 次 · 总量 ${count(knownValue(item, "totalTokens", "knownTotalRequests"))} · 输入 ${count(knownValue(item, "totalInputTokens", "knownInputRequests"))} · 输出 ${count(knownValue(item, "totalOutputTokens", "knownOutputRequests"))}${item.failures ? ` · 失败 ${count(item.failures)}` : ""} · 最近 ${when(item.lastAt)}`, "model-traces-row-meta")); parent.append(row);
    }
  }
  function field(label, value) { const item = node("div", undefined, "model-usage-record-field"); item.append(node("span", label), node("strong", value)); return item; }
  function fields(pairs) { const list = node("div", undefined, "model-usage-record-fields"); for (const [label, value] of pairs) list.append(field(label, value)); return list; }
  // Without a captured trace the ledger still knows when the attempt started, responded and ended.
  function processFields(record) {
    if (record.status === "queued") return [["进入排队", when(record.at)], ["状态", "等待模型服务空闲"], ["已等待", elapsed(record)]];
    if (record.status === "running") return [["请求发出", when(record.at)], ["状态", "等待服务返回"], ["已用时", elapsed(record)]];
    const offset = ms => Number.isFinite(ms) ? `+${seconds(ms)}` : "未记录";
    const pairs = [["请求发出", when(record.at)], ["首个响应字节", offset(record.firstByteMs)], ["首段可见正文", offset(record.firstTextMs)],
      ["结束", `${Number.isFinite(record.durationMs) ? `${seconds(record.durationMs)}后` : "时间未知"} · ${statusLabel(record)}`]];
    if (FAILED.has(record.status)) pairs.push(["错误", `${statusLabel(record)}${record.status === "http-error" ? "：服务返回错误" : record.status === "timeout" ? "：超过等待时限" : record.status === "stream-error" ? "：返回途中连接中断" : "：未能连接模型服务"}`]);
    return pairs;
  }
  function recordDetail(record, key, index, friend, status, duration) {
    const detail = node("div", undefined, "model-usage-record-detail-body"), tabs = node("div", undefined, "model-usage-record-tabs"), current = state.recordTabs.get(key) || "overview";
    tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "调用详情");
    for (const [name, label] of DETAIL_TABS) {
      const tab = button(label, () => { state.recordTabs.set(key, name); renderRecords(); $(`modelUsageRecordTab${index}${name}`)?.focus(); }, "model-usage-record-tab");
      tab.id = `modelUsageRecordTab${index}${name}`; tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(name === current)); tab.tabIndex = name === current ? 0 : -1;
      tab.addEventListener("keydown", event => {
        if (event.isComposing || event.keyCode === 229 || !["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault();
        const position = DETAIL_TABS.findIndex(([value]) => value === name), next = DETAIL_TABS[(position + (event.key === "ArrowRight" ? 1 : -1) + DETAIL_TABS.length) % DETAIL_TABS.length][0];
        state.recordTabs.set(key, next); renderRecords(); $(`modelUsageRecordTab${index}${next}`)?.focus();
      });
      tabs.append(tab);
    }
    const panel = node("div", undefined, "model-usage-record-panel"); panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", `modelUsageRecordTab${index}${current}`);
    detail.append(tabs, panel);
    const trace = record.traceRecordId && state.traces?.renderTrace ? node("div", undefined, "model-traces-viewer") : null;
    if (current === "overview") {
      const active = ACTIVE.has(record.status), known = value => active ? "—" : count(value);
      panel.append(fields([["好友", friend], ["时间", when(record.at)], ["模块 / 步骤", `${pluginLabel(record.plugin)} / ${stepLabel(record.plugin, record.step)}`], ["模型", record.model || "未知"], ["总 token", known(record.totalTokens)], ["输入", known(record.totalInputTokens)], ["输出", known(record.totalOutputTokens)], ["缓存读 / 写", `${known(record.cacheReadTokens)} / ${known(record.cacheWriteTokens)}`], ["推理", known(record.reasoningTokens)], ["状态 / 耗时", `${status} / ${duration}`], ["计量来源", active ? "调用结束后统计" : record.totalTokensSource === "derived" ? "按协议推导" : record.totalTokensSource === "provider" ? "服务报告" : "服务未提供完整用量"]]));
      if (record.task && !state.filters.task) panel.append(button("查看同一任务的全部步骤与重试", () => { state.filters = { ...defaults(), task: record.task }; reload(); }, "model-usage-link"));
      return { detail };
    }
    if (current === "events") panel.append(fields(processFields(record)));
    if (trace) { panel.append(trace); return { detail, trace: [trace, record.traceRecordId, current, FAILED.has(record.status)] }; }
    if (current !== "events") panel.append(node("p", "这次调用未采集请求与响应。开启开发者模式（设置 → 开发者模式）后，新的调用会保存这些内容。", "model-traces-note"));
    return { detail };
  }
  function renderRecords() {
    const parent = $('modelUsageRecords'); parent.replaceChildren(); state.elapsed = [];
    if (!state.records.length) parent.append(node("p", state.summary ? "暂无匹配的调用记录" : "", "model-traces-note"));
    const table = node("table", undefined, "model-usage-records-table"), head = node("thead"), labels = node("tr"), body = node("tbody");
    for (const [label, name] of [["时间", "time"], ["好友", "friend"], ["步骤", "step"], ["模块", "module"], ["总 token", "total"], ["耗时", "duration"], ["状态", "status"], ["", "action"]]) {
      const cell = node("th", label, `usage-record-${name}`); cell.setAttribute("scope", "col"); labels.append(cell);
    }
    head.append(labels); table.append(head, body); if (state.records.length) parent.append(table);
    const traces = [], shown = [];
    for (const [index, record] of state.records.entries()) {
      const key = record.attemptId || `${record.at}:${index}`, expanded = state.expandedRecords.has(key), detailId = `modelUsageRecordDetail${index}`, toggleId = `modelUsageRecordToggle${index}`;
      const row = node("tr", undefined, "model-usage-record"); row.setAttribute("data-attempt-id", record.attemptId || ""); row.setAttribute("aria-expanded", String(expanded));
      const failed = FAILED.has(record.status), active = ACTIVE.has(record.status), status = statusLabel(record);
      const friend = friendLabel(record.scope, record.user), duration = active ? elapsed(record) : Number.isFinite(record.durationMs) ? seconds(record.durationMs) : "未知";
      const toggle = () => { if (state.expandedRecords.has(key)) state.expandedRecords.delete(key); else state.expandedRecords.add(key); renderRecords(); $(toggleId)?.focus(); };
      const values = [[when(record.at), "time"], [friend, "friend"], [stepLabel(record.plugin, record.step), "step"], [pluginLabel(record.plugin), "module"], [active ? "—" : count(record.totalTokens), "total"], [duration, "duration"], [status, "status"]];
      for (const [value, name] of values) {
        const cell = node("td", undefined, `usage-record-${name}`), text = node(name === "friend" || name === "total" ? "strong" : "span", value); cell.setAttribute("title", value);
        cell.append(text); if (active && name === "duration") state.elapsed.push([text, cell, record]);
        if (name === "status") cell.className += failed ? " model-traces-status-error" : record.status === "ok" ? " model-traces-status-complete" : active ? " model-traces-status-running" : "";
        row.append(cell);
      }
      const action = node("td", undefined, "usage-record-action"), disclosure = button(expanded ? "收起" : "详情", toggle, "model-usage-record-toggle");
      disclosure.id = toggleId; disclosure.setAttribute("aria-label", `${expanded ? "收起" : "展开"}${friend}的${stepLabel(record.plugin, record.step)}详情`); disclosure.setAttribute("aria-expanded", String(expanded)); disclosure.setAttribute("aria-controls", detailId); action.append(disclosure); row.append(action);
      row.addEventListener("click", event => { if (!event.target?.closest?.("button, a")) toggle(); }); body.append(row);
      const detailRow = node("tr", undefined, "model-usage-record-detail"), cell = node("td");
      cell.setAttribute("colspan", "8"); detailRow.hidden = !expanded; detailRow.append(cell); body.append(detailRow);
      if (!expanded) { cell.id = detailId; continue; }
      const { detail, trace } = recordDetail(record, key, index, friend, status, duration);
      detail.id = detailId; cell.append(detail);
      if (trace) { traces.push(trace); shown.push(trace[1]); }
    }
    // Trace content attaches after the row is in place; viewers of collapsed records are dropped.
    state.traces?.retainTraces?.(shown);
    for (const [container, id, tab, failed] of traces) state.traces.renderTrace(container, id, tab, { failed });
    $('modelUsageMore').hidden = !state.next; $('modelUsageMore').disabled = busy();
    if (state.elapsed.length && !state.ticker) state.ticker = setTimeout(tick, 1000);
  }
  // Elapsed time of in-flight rows advances without re-rendering the table.
  function tick() {
    state.ticker = null;
    if (!state.visible || !state.elapsed.length) return;
    for (const [text, cell, record] of state.elapsed) { text.textContent = elapsed(record); cell.setAttribute("title", text.textContent); }
    state.ticker = setTimeout(tick, 1000);
  }
  function render() {
    if (!$('modelUsagePanel')) return;
    $('modelUsagePanel').setAttribute("aria-busy", String(busy())); $('modelUsageLoading').hidden = !busy();
    // Every ledger read carries a fresh cutoff; unchanged usage keeps the existing DOM.
    const stable = value => value && { ...value, cutoffMs: undefined };
    const key = JSON.stringify([state.account, stable(state.summary), state.modules, state.friends, state.records, state.next, state.error, state.friendError, state.friendsLoading, state.filters, state.order, state.setting?.enabled, state.confirming, state.clearing, state.feedback, state.view, state.unit, state.metric, state.drill, [...state.expanded], stable(state.timeline)]);
    if (key === state.rendered) { $('modelUsageMore').disabled = busy(); return; } state.rendered = key;
    renderFilters(); $('modelUsageError').hidden = !state.error; $('modelUsageError').textContent = state.error;
    $('modelUsageStatsView').hidden = state.view !== "stats"; $('modelUsageRecordsView').hidden = state.view !== "records";
    $('modelUsageSummary').hidden = state.view !== "stats"; $('modelUsageCompactSummary').hidden = state.view !== "records";
    renderSummary(); renderTimeline(); renderModules(); renderFriends(); renderRecords();
    $('modelUsageClearConfirm').hidden = !state.confirming;
    $('modelUsageClear').disabled = !state.account || state.clearing; $('modelUsageConfirmClear').disabled = state.clearing; $('modelUsageFeedback').textContent = state.feedback;
  }
  function filter(id, label, values, apply) { const element = node("select"); element.id = id; element.setAttribute("aria-label", label); options(element, values, values[0][0]); element.addEventListener("change", () => { apply(element.value); reload(); }); return element; }
  function buildPanel() {
    const panel = node("main", undefined, "model-traces-panel model-usage-panel"); panel.id = "modelUsagePanel"; panel.hidden = true; panel.setAttribute("aria-label", "模型调用");
    const filters = node("div", undefined, "model-traces-filters model-usage-filters");
    filters.append(filter("modelUsageRange", "时间范围", RANGES, value => { state.filters.range = value; }), filter("modelUsageFriend", "好友", [["", "全部好友"]], value => { state.filters.friend = value; }), filter("modelUsagePlugin", "模块", [["", "全部模块"]], value => { state.filters.plugin = value; state.filters.step = ""; }), filter("modelUsageModel", "模型", [["", "全部模型"]], value => { state.filters.model = value; }), filter("modelUsageStatus", "状态", STATUS_FILTERS, value => { state.filters.status = value; }));
    const step = node("div", undefined, "model-usage-step-filter"); step.id = "modelUsageStep"; step.hidden = true;
    const error = node("p", "", "model-traces-note model-usage-error"); error.id = "modelUsageError"; error.setAttribute("role", "alert"); error.hidden = true;
    const loading = node("p", "正在更新用量统计…", "model-traces-note"); loading.id = "modelUsageLoading"; loading.setAttribute("role", "status"); loading.hidden = true;
    const summary = node("section", undefined, "model-usage-summary"); summary.id = "modelUsageSummary"; summary.setAttribute("aria-label", "汇总"); summary.setAttribute("aria-live", "polite");
    const compact = node("div", undefined, "model-usage-compact-summary"); compact.id = "modelUsageCompactSummary"; compact.hidden = true; compact.setAttribute("aria-live", "polite");
    const stats = node("section"); stats.id = "modelUsageStatsView";
    const controls = node("div", undefined, "model-usage-trend-controls"); controls.id = "modelUsageTrendControls"; controls.setAttribute("aria-label", "时间粒度与用量指标");
    const selection = node("div", undefined, "model-usage-selection"); selection.id = "modelUsageSelection"; selection.setAttribute("aria-live", "polite");
    const trend = node("div"); trend.id = "modelUsageTrend"; const moduleHead = node("div", undefined, "model-usage-section"); moduleHead.append(node("h2", "Token 花在哪里"));
    const modules = node("div", undefined, "model-usage-list"); modules.id = "modelUsageModules";
    const friendHead = node("div", undefined, "model-usage-section"); friendHead.append(node("h2", "按好友"), filter("modelUsageOrder", "排序", ORDERS, value => { state.order = value; }));
    const friends = node("div", undefined, "model-usage-list"); friends.id = "modelUsageFriends"; stats.append(controls, selection, trend, moduleHead, modules, friendHead, friends);
    const recordView = node("section"); recordView.id = "modelUsageRecordsView"; recordView.hidden = true;
    const records = node("div", undefined, "model-usage-list"); records.id = "modelUsageRecords"; const more = button("加载更多", () => load({ more: true }), "model-usage-more"); more.id = "modelUsageMore"; more.hidden = true; recordView.append(records, more);
    const accounting = node("footer", undefined, "model-usage-accounting-footer"); accounting.id = "modelUsageAccounting";
    const footer = node("div", undefined, "model-usage-footer"), clear = button("清除用量", () => { state.confirming = true; state.feedback = ""; render(); $('modelUsageCancelClear').focus(); }, "model-usage-danger"); clear.id = "modelUsageClear"; footer.append(clear);
    const confirm = node("div", undefined, "model-usage-confirm"); confirm.id = "modelUsageClearConfirm"; confirm.hidden = true;
    const cancel = button("取消", () => { state.confirming = false; render(); $('modelUsageClear').focus(); }); cancel.id = "modelUsageCancelClear"; const accept = button("确认清除", () => clearUsage(), "model-usage-danger"); accept.id = "modelUsageConfirmClear";
    confirm.append(node("p", "清除当前账号的全部用量统计和调用记录？聊天和分析结果不受影响。"), cancel, accept); const feedback = node("p", "", "model-traces-note"); feedback.id = "modelUsageFeedback"; feedback.setAttribute("role", "status");
    panel.append(filters, step, error, loading, summary, compact, stats, recordView, footer, confirm, feedback, accounting); (state.mount || document.body).appendChild(panel);
  }
  async function clearUsage() {
    if (!state.account || state.clearing) return; const account = state.account, serial = ++state.clearSerial; state.clearing = true; render();
    const result = await host("usage-clear", { account, confirm: true }); if (serial !== state.clearSerial || account !== state.account) return; state.clearing = false;
    if (result.ok) { state.confirming = false; state.feedback = "已清除当前账号的用量统计"; state.filters.friend = ""; state.expandedRecords.clear(); reload(); } else { state.feedback = "清除失败，请重试"; render(); }
  }
  function setView(view) { if (view !== "stats" && view !== "records") return; state.view = view; if (state.initialized) { render(); schedule(); if (view === "stats") state.chart?.draw(); } }
  function open(view) { if (!state.initialized) return; if (view === "stats" || view === "records") state.view = view; $('modelUsagePanel').hidden = false; if (!state.visible) { state.visible = true; reload({ keepSelection: true }); } else { render(); schedule(); } if (state.view === "stats") state.chart?.draw(); }
  function hide() { state.visible = false; state.serial++; state.friendsSerial++; state.loading = false; clearTimeout(state.timer); state.timer = null; clearTimeout(state.ticker); state.ticker = null; $('modelUsagePanel').hidden = true; }
  async function closeWindow() { try { if (await window.modelTracesWindow?.close?.() === false) throw new Error("close-unavailable"); } catch { state.feedback = "窗口关闭失败，请使用标题栏关闭按钮"; render(); } }
  function setScope(account) {
    account = typeof account === "string" ? account : ""; if (account === state.account) return;
    state.account = account; state.serial++; state.friendsSerial++; state.clearSerial++; state.clearing = false; state.filters = defaults(); state.confirming = false; state.feedback = ""; state.error = ""; state.friendError = ""; state.friendsLoading = false; state.loading = false;
    state.summary = state.baseSummary = state.timeline = null; state.dataScope = ""; state.friends = []; state.modules = []; state.records = []; state.next = null; state.names.clear(); state.plugins.clear(); state.models.clear(); state.expanded.clear(); state.expandedRecords.clear(); state.recordTabs.clear(); resetSelection(); state.chart?.destroy(); state.chart = null; state.chartKey = "";
    render(); if (state.visible) reload();
  }
  function setDeveloperMode(setting) { state.setting = setting || null; if (state.initialized && state.visible) render(); }
  function init(options = {}) {
    if (state.initialized) return; state.initialized = true; state.traces = options.traces || null; state.mount = options.mount || null; state.account = typeof options.account === "string" ? options.account : ""; readSettings(); buildPanel();
    document.addEventListener("keydown", event => { if (event.key !== "Escape" || event.isComposing || event.keyCode === 229 || !state.visible) return; if (event.target?.tagName === "SELECT") return; event.preventDefault(); if (state.confirming) { state.confirming = false; render(); return; } void closeWindow(); });
    window.addEventListener("focus", () => { if (state.visible && state.pages <= 1 && !state.confirming) void load({ refresh: true }); }); window.addEventListener("beforeunload", () => { state.chart?.destroy(); clearTimeout(state.timer); clearTimeout(state.ticker); });
  }
  window.ModelUsage = { init, open, hide, setView, setScope, setDeveloperMode, isOpen: () => state.visible, reload };
})();
