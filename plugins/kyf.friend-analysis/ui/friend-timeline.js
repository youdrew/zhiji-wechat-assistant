"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 时间线: what we did together. Layout and interactions copied from the 自我剖析 时间线 tab (kyf.self-analysis/ui/page.js
// renderTimeline … renderTimelineAddForm, 2026-11-07); the overview is the shared dot chart of plugin-sdk/web/timeline.js.
// With only “我们” there are no track tabs and the 主线 entry sits in the top row. Ctrl+Z undoes the user's own edits;
// the Agent's edits have their own undo line (doc 4.6). Rows from 关系记忆 are read-only here.
(function () {
  const A = window.FriendApp;
  const Timeline = window.KyfTimeline;
  const COLORS = Timeline.COLORS;
  const OURS = "我们";
  const CATEGORIES = ["见面聚会", "一起出行", "一起做的事", "同学共事", "互相帮忙", "礼物纪念", "情感", "其他"];
  const STATUS = { occurred: "已发生", planned: "计划", cancelled: "已取消", uncertain: "待判断" };
  const LEVELS = { confirmed: "已确认", strong: "较可靠", possible: "可能" };
  const WHEN_HINT = "时间可写成 2024、2024.05 或 2024.05.01";
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const string = (value, limit = 2000) => typeof value === "string" ? value.slice(0, limit) : "";
  const isImportant = Timeline.isImportant, hasMonth = Timeline.hasMonth;
  const parseWhenInput = Timeline.parseWhenInput, whenInput = Timeline.whenInput;
  const fullDate = ms => new Date(ms).toLocaleDateString("zh-CN");
  const yearOf = ms => new Date(ms).getFullYear();
  const monthLabel = event => Number.isFinite(event.startMs) && hasMonth(event) ? `${new Date(event.startMs).getMonth() + 1}月` : "—";
  const valid = value => object(value) && Array.isArray(value.events) && Array.isArray(value.periods) && Array.isArray(value.tracks) &&
    typeof value.version === "number";
  const rowKey = item => (item.fromMemory ? "memory:" : "") + item.id;
  const label = value => typeof value === "string" ? value.slice(0, 80) : string(value?.label, 80);
  const fresh = () => ({ value: null, serial: 0, pending: false, busy: false, error: "", notice: "", progress: null, importantOnly: false,
    track: "", open: "", editing: "", when: null, filter: "", manage: false, adding: null, collapsed: new Set(), expanded: new Set(),
    unknownOpen: false, evidence: {}, undoQueued: 0, agentDismissed: null, rendering: false, drawn: false, menu: null });
  const T = fresh();
  let ui = null;

  // ── data ──
  function tracks() {
    const list = (T.value?.tracks || []).filter(track => object(track) && typeof track.name === "string");
    return list.length ? list : [{ name: OURS, color: COLORS[0], visible: true, builtin: true }];
  }
  const trackColor = name => tracks().find(track => track.name === name)?.color || COLORS[0];
  const trackNames = () => tracks().map(track => track.name);
  function categories() {
    const names = (T.value?.categories || []).filter(name => typeof name === "string" && name);
    return names.length ? names : CATEGORIES;
  }
  const memoryRefs = () => (T.value?.memoryRefs || []).filter(ref => object(ref) && typeof ref.id === "string");
  // 来自关系记忆的只读行（memoryRefs，readOnly）：放在“我们”上，标题就是记忆原文。
  const memoryItems = () => memoryRefs().map(ref => ({ ...ref, fromMemory: true, track: OURS, title: string(ref.title, 80) || string(ref.text, 80),
    status: "occurred", importance: "general", precision: ref.precision || "unknown" }));
  // 事件已关联的关系记忆：memories 是全部，memory 是第一条。
  const linked = event => (Array.isArray(event.memories) ? event.memories : object(event.memory) ? [event.memory] : []).filter(object);
  // 总览图的行：可见且有内容的主线；两行及以上才有主线页签。
  function rows() {
    const value = T.value, used = new Set([...value.periods, ...value.events].map(item => item.track));
    if (memoryRefs().some(ref => Number.isFinite(ref.startMs))) used.add(OURS);
    return tracks().filter(track => track.visible !== false && used.has(track.name));
  }
  const multi = () => !!T.value && rows().length >= 2;
  // 没有颗粒度设置：聊天开始到现在不超过 3 年时一季度一列，否则一年一列。
  function grain() {
    const value = T.value, starts = [value.coverage?.earliestMs, ...value.events.map(event => event.startMs),
      ...value.periods.map(period => period.startMs), ...memoryRefs().map(ref => ref.startMs)].filter(Number.isFinite);
    return starts.length && Date.now() - Math.min(...starts) <= 3 * 365.25 * 86400000 ? 3 : 12;
  }
  function agentUndo() {
    const side = object(T.value?.undo?.agent) ? T.value.undo.agent : {};
    const recent = (Array.isArray(side.items) ? side.items : []).filter(object);
    return { total: count(side.count) ?? 0, recent };
  }
  const agentKey = () => { const top = agentUndo().recent[0]; return top ? String(top.seq ?? top.updatedMs ?? "") : ""; };

  // ── reading and writing ──
  // 事件分页返回（单次回复有大小上限）；续读中版本或总数变了就从头再读。onProgress 收到真实的已读 / 总条数。
  async function fullTimeline(value, onProgress = null) {
    for (let attempt = 0; ; attempt++) {
      if (!valid(value)) throw new Error("时间线暂时无法读取，请稍后重试。");
      const events = [...value.events];
      let next = value.nextOffset, changed = false;
      while (Number.isInteger(next) && !changed) {
        if (onProgress && Number.isInteger(value.eventsTotal) && value.eventsTotal > 0) onProgress(events.length, value.eventsTotal);
        const page = await A.rpc("page.timeline", { offset: next });
        changed = !valid(page) || page.version !== value.version || page.eventsTotal !== value.eventsTotal || !page.events.length;
        if (!changed) { events.push(...page.events); next = page.nextOffset; }
      }
      if (!changed) return { ...value, events };
      if (attempt >= 2) throw new Error("时间线正在更新，请稍后重试。");
      value = await A.rpc("page.timeline", {});
    }
  }
  async function load() {
    if (A.masked) return;
    build();
    if (T.busy) return;
    const serial = ++T.serial;
    T.pending = true; T.progress = null; render();
    // 已有内容时（切回本页、步骤完成、冲突后刷新）静默更新；首次读取才报分页进度。
    const progress = T.value ? null : (done, total) => { if (serial === T.serial) { T.progress = { done, total }; render(); } };
    try {
      const value = await fullTimeline(await A.rpc("page.timeline", {}), progress);
      if (serial !== T.serial) return;
      T.value = value; T.error = "";
    } catch (error) { if (serial === T.serial) T.error = error.message || "时间线读取失败"; }
    finally { if (serial === T.serial) { T.pending = false; T.progress = null; if (!A.masked) render(); } }
  }
  async function write(method, params) {
    if (!T.value || T.busy || A.restricted || A.masked) return null;
    T.serial++; T.pending = false; T.busy = true; T.error = ""; T.notice = ""; render();
    let conflict = false;
    try {
      const result = await A.rpc(method, { ...params, expectedVersion: T.value.version });
      T.value = await fullTimeline(valid(result) ? result : await A.rpc("page.timeline", {}));
      return object(result) ? result : {};
    } catch (error) {
      conflict = ["conflict", "version-conflict"].includes(error?.code);
      T.error = error.message || "操作失败";
      return null;
    } finally {
      T.busy = false;
      if (!A.masked) {
        render();
        // Agent 也会改时间线：版本对不上时读回最新内容，用户重做一次就好。
        if (conflict) void load().then(() => { T.error = "时间线刚被修改过，已刷新，请重试。"; render(); });
      }
    }
  }
  const edit = async params => !!await write("page.timelineEdit", params);
  function undoText(result, agent) {
    if (result.undone) return `${agent ? "已撤销 Agent 的修改" : "已撤销"}：${label(result.undone)}`;
    if (result.skipped) return string(result.skipped.reason, 200) ||
      (agent ? "这处之后被你改过，没有撤销 Agent 的这次修改" : "这处之后被 Agent 改过，没有撤销；需要的话直接修改");
    return agent ? "Agent 没有可撤销的修改" : "没有可撤销的修改";
  }
  // Ctrl+Z 只撤销用户自己的修改；连按时前一次还在写入，排队按顺序撤销。
  async function undo() {
    if (!T.value || A.restricted) return;
    if (T.busy) { T.undoQueued++; return; }
    const result = await write("page.timelineUndo", {});
    if (result) { T.notice = undoText(result, false); render(); }
    if (T.undoQueued) { T.undoQueued--; if (result && (result.undone || result.skipped)) void undo(); else T.undoQueued = 0; }
  }
  async function undoAgent() {
    const result = await write("page.timelineUndoAgent", {});
    if (result) { T.notice = undoText(result, true); render(); }
  }
  // 行内修改先改本地显示，失败再改回来。
  async function patch(event, fields) {
    const before = Object.fromEntries(Object.keys(fields).map(key => [key, event[key]]));
    Object.assign(event, fields);
    if (await edit({ target: "event", action: "edit", id: event.id, fields })) return;
    Object.assign(event, before); render();
  }
  async function loadEvidence(item) {
    const key = rowKey(item);
    try {
      const result = await A.rpc("page.timelineEvidence", { id: item.id });
      T.evidence[key] = object(result) && Array.isArray(result.items) ? result : { total: 0, items: [] };
    } catch { T.evidence[key] = { total: 0, items: [] }; }
    if (T.open === key && !A.masked) render();
  }
  function wantEvidence(item) {
    const key = rowKey(item);
    if (T.evidence[key] || item.manual) return;
    if (item.fromMemory && !count(item.evidenceCount)) return;
    void loadEvidence(item);
  }

  // ── skeleton (built once; render() updates it) ──
  function build() {
    const root = A.byId("pageTimeline");
    if (ui && ui.root === root && root.children.length) return ui;
    const u = { root, forms: new Map(), rows: new Map(), whenInput: null };
    root.className = "fa-page fa-tl";
    u.state = A.el("div", "tl-state"); u.state.setAttribute("role", "status"); u.state.hidden = true;
    u.progress = A.el("div", "tl-progress"); u.progress.setAttribute("aria-hidden", "true");
    u.bar = A.el("span"); u.progress.append(u.bar);
    u.stateText = A.el("p");
    u.retry = A.button("重试", "fa-link", () => { T.error = ""; void load(); });
    u.state.append(u.progress, u.stateText, u.retry);
    u.top = A.el("div", "tl-top"); u.top.hidden = true;
    const filter = A.el("label", "tl-filter");
    u.important = A.el("input"); u.important.type = "checkbox";
    u.important.addEventListener("change", () => { T.importantOnly = u.important.checked; render(); });
    filter.append(u.important, A.el("span", "", "只看重要节点"));
    u.manageTop = A.button("主线", "fa-plain", () => { T.manage = !T.manage; render(); });
    u.add = A.button("添加", "fa-plain", () => { T.adding = T.adding === null ? "event" : null; render(); });
    const actions = A.el("span", "tl-top-actions");
    actions.append(u.manageTop, u.add);
    u.top.append(filter, actions);
    u.addForm = A.el("div", "tl-form"); u.addForm.hidden = true;
    u.manage = A.el("div", "tl-manage-host"); u.manage.hidden = true;
    u.overview = A.el("div", "tl-overview");
    u.tracks = A.el("div", "tl-tracks"); u.tracks.hidden = true;
    u.periodBar = A.el("div", "tl-period-bar"); u.periodBar.hidden = true;
    u.message = A.el("p", "fa-note tl-message"); u.message.setAttribute("role", "status"); u.message.hidden = true;
    u.agent = A.el("div", "tl-agent"); u.agent.hidden = true;
    u.agentText = A.el("span", "tl-agent-text");
    u.agentUndo = A.button("撤销", "fa-link", () => { void undoAgent(); });
    u.agentClose = A.button("×", "fa-plain tl-agent-close", () => { T.agentDismissed = agentKey(); render(); }, { "aria-label": "收起" });
    u.agent.append(u.agentText, u.agentUndo, u.agentClose);
    u.list = A.el("div", "tl-list");
    u.empty = A.el("div", "fa-empty"); u.empty.hidden = true;
    root.replaceChildren(u.state, u.top, u.addForm, u.manage, u.overview, u.tracks, u.periodBar, u.message, u.agent, u.list, u.empty);
    ui = u;
    return u;
  }
  // Forms keep what the user typed across renders; a form is dropped once it is closed.
  function form(key, make) {
    if (!ui.forms.has(key)) ui.forms.set(key, make());
    return ui.forms.get(key);
  }
  function pruneForms() {
    const open = new Set([T.adding && "add:" + T.adding, T.editing && "edit:" + T.editing, T.manage && "track:new"].filter(Boolean));
    for (const key of [...ui.forms.keys()]) if (!open.has(key)) ui.forms.delete(key);
    for (const node of ui.forms.values()) if (node.save) node.save.disabled = T.busy;
  }

  function render() {
    if (!ui || A.masked) return;
    const value = T.value, run = object(value?.run) ? value.run : {}, writable = !A.restricted, many = multi();
    if (!writable) { T.adding = null; T.editing = ""; T.manage = false; T.when = null; }
    ui.top.hidden = !value;
    renderState(!value && (T.pending || !!T.error));
    ui.important.checked = T.importantOnly;
    ui.add.hidden = !writable; ui.add.disabled = T.busy;
    ui.manageTop.hidden = !writable || many;
    ui.manageTop.textContent = T.manage && !many ? "收起" : "主线";
    // ``run`` is the 时间线 step of the latest run; this page is read again when the step finishes.
    const running = run.state === "running" || run.state === "pending" && ["queued", "running"].includes(run.runState);
    let text = value ? T.error : "";
    if (!text && running) text = run.state === "running" ? "正在分析时间线，完成后自动显示" : "等待分析时间线…";
    else if (!text && run.state === "stopped") text = "时间线分析已停止";
    else if (!text && run.state === "failed") text = string(run.error, 200) || "时间线分析未完成，请重试";
    ui.message.textContent = text; ui.message.hidden = !text;
    const nothing = !!value && !value.events.length && !value.periods.length && !memoryItems().length;
    ui.empty.hidden = !nothing || !!text;
    ui.empty.textContent = run.analyzed === true ? `还没有找到我们一起经历的事${writable ? "，可以手动添加" : ""}` :
      "开始分析后，整理我们一起经历过的事";
    pruneForms();
    renderAddForm();
    ui.manage.hidden = !T.manage || many || !value;
    ui.manage.replaceChildren(...(ui.manage.hidden ? [] : [trackManager()]));
    renderOverview(many);
    renderTracks(many);
    renderPeriodBar();
    renderAgent();
    renderList();
  }
  // 首次读取：分页进度是真实的已读 / 总条数；第一页返回前用不定进度条，不编造百分比。
  function renderState(show) {
    ui.state.hidden = !show;
    if (!show) return;
    const failed = !T.pending && !!T.error, progress = T.progress;
    ui.state.className = "tl-state " + (failed ? "failed" : "loading");
    ui.progress.hidden = failed; ui.retry.hidden = !failed;
    ui.progress.className = "tl-progress" + (progress ? "" : " indeterminate");
    ui.bar.style.width = progress ? `${Math.min(100, progress.done / progress.total * 100)}%` : "";
    ui.stateText.textContent = failed ? T.error : progress ? `正在读取… ${progress.done} / ${progress.total} 条` : "正在读取…";
  }
  function renderOverview(many) {
    const value = T.value, shown = value ? rows() : [];
    const drawn = Timeline.renderOverview(ui.overview, { mode: "dots", value, tracks: shown, months: value ? grain() : 12,
      importantOnly: T.importantOnly, selectedTrack: many ? T.track : "", colorOf: trackColor, onTrack: many ? selectTrack : undefined,
      onEvent: reveal, onPeriod: filterPeriod, frequency: value?.frequency, memoryRefs: shown[0]?.name === OURS ? memoryRefs() : [],
      scrollToEnd: !T.drawn });
    // 打开时滚到最右（最近）一次；之后重绘保留用户的滚动位置。
    if (drawn) T.drawn = true;
  }
  function selectTrack(name) { T.track = name; T.open = ""; T.editing = ""; render(); }
  function filterPeriod(period) { T.filter = period.id; T.editing = ""; render(); }
  // 点总览图上的标记：切主线页签、展开年份和“展开全部事件”、展开详情、滚到中间。
  function reveal(mark) {
    const item = mark.memoryRef === true ? memoryItems().find(ref => ref.id === mark.id) || mark :
      T.value.events.find(event => event.id === mark.id) || mark;
    const key = rowKey(item);
    if (multi() && T.track && T.track !== item.track) T.track = item.track;
    T.filter = ""; T.editing = ""; T.open = key;
    if (Number.isFinite(item.startMs)) {
      const year = yearOf(item.startMs);
      T.collapsed.delete(year);
      if (!T.importantOnly && !isImportant(item) && item.status !== "planned") T.expanded.add(year);
    }
    wantEvidence(item);
    render();
    ui.rows.get(key)?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }
  function renderTracks(many) {
    const host = ui.tracks;
    if (!many) { host.replaceChildren(); host.hidden = true; T.track = ""; return; }
    host.hidden = false;
    const shown = rows();
    if (T.track && !shown.some(track => track.name === T.track)) T.track = "";
    const bar = A.el("div", "tl-tabs"); bar.setAttribute("role", "tablist");
    for (const [name, title] of [["", "全部"], ...shown.map(track => [track.name, track.name])])
      bar.append(A.button(title, "", () => selectTrack(name), { role: "tab", "aria-selected": String(T.track === name) }));
    const row = A.el("div", "tl-tabs-row");
    row.append(bar);
    if (!A.restricted) row.append(A.button(T.manage ? "收起" : "管理主线", "fa-plain tl-manage-toggle", () => { T.manage = !T.manage; render(); }));
    host.replaceChildren(row);
    if (T.manage && !A.restricted) host.append(trackManager());
  }
  function trackManager() {
    const box = A.el("div", "tl-manage");
    for (const track of tracks()) {
      const row = A.el("div", "tl-manage-row");
      const visible = A.el("input"); visible.type = "checkbox"; visible.checked = track.visible !== false; visible.disabled = T.busy;
      visible.setAttribute("aria-label", `在总览显示${track.name}`);
      visible.addEventListener("change", () => { void edit({ target: "track", action: "visible", id: track.name, fields: { visible: visible.checked } }); });
      const swatch = A.el("span", "tl-swatch"); swatch.style.background = track.color || COLORS[0];
      row.append(visible, swatch, A.el("span", "tl-manage-name", track.name));
      if (track.description) row.append(A.el("span", "fa-note tl-manage-help", string(track.description, 60)));
      if (!track.builtin && track.name !== OURS) {
        const remove = A.button("删除", "fa-plain fa-danger-text", () => { void edit({ target: "track", action: "delete", id: track.name }); });
        remove.disabled = T.busy;
        row.append(remove);
      }
      box.append(row);
    }
    box.append(form("track:new", newTrackRow));
    return box;
  }
  function newTrackRow() {
    const row = A.el("div", "tl-manage-row tl-manage-new");
    const name = A.el("input"); name.type = "text"; name.maxLength = 8; name.placeholder = "主线名称（最多 8 字）";
    const description = A.el("input"); description.type = "text"; description.maxLength = 60; description.placeholder = "说明：告诉 AI 什么事归到这里（可选）";
    const colors = A.el("div", "tl-colors");
    let color = COLORS[0];
    const swatches = COLORS.map(option => {
      const swatch = A.button("", "tl-swatch-btn" + (option === color ? " picked" : ""), () => {
        color = option;
        for (const node of swatches) node.className = "tl-swatch-btn" + (node === swatch ? " picked" : "");
      }, { "aria-label": "颜色" });
      swatch.style.background = option;
      return swatch;
    });
    colors.append(...swatches);
    row.save = A.button("新建主线", "fa-primary", () => {
      const title = name.value.trim();
      if (!title) { name.focus(); return; }
      void edit({ target: "track", action: "add", fields: { title, description: description.value.trim(), color } })
        .then(ok => { if (ok) { ui.forms.delete("track:new"); render(); } });
    });
    row.append(name, description, colors, row.save);
    return row;
  }
  function renderPeriodBar() {
    const host = ui.periodBar, period = (T.value?.periods || []).find(item => item.id === T.filter);
    if (!period) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    const parts = [period.title, period.whenText, period.ongoing ? "至今" : "", count(period.evidenceCount) ? `${period.evidenceCount} 依据` : ""];
    host.replaceChildren(A.el("span", "tl-period-title", parts.filter(Boolean).join(" · ")));
    if (!A.restricted) host.append(A.button("编辑", "fa-plain", () => { T.editing = T.editing === period.id ? "" : period.id; render(); }));
    host.append(A.button("×", "fa-plain", () => { T.filter = ""; T.editing = ""; render(); }, { "aria-label": "取消筛选" }));
    if (T.editing === period.id) host.append(form("edit:" + period.id, () => periodForm(period)));
  }
  function renderAgent() {
    const { total, recent } = agentUndo();
    ui.agent.hidden = A.restricted || !T.value || !total || agentKey() === T.agentDismissed;
    if (ui.agent.hidden) return;
    ui.agentText.textContent = `Agent 改了 ${total} 处 ·`;
    ui.agent.title = recent.slice(0, 5).map(item => `${label(item)}${item.agent ? " · " + string(item.agent, 40) : ""}`).join("\n");
    ui.agentUndo.disabled = T.busy;
  }

  // ── list: by year, newest first; each year shows important and planned events, the rest behind “展开全部事件” ──
  function renderList() {
    const host = ui.list, value = T.value;
    ui.rows = new Map(); ui.whenInput = null;
    if (!value) { host.replaceChildren(); return; }
    const children = [];
    if (T.notice) children.push(A.el("div", "tl-notice", T.notice));
    const period = value.periods.find(item => item.id === T.filter);
    const items = [...value.events, ...memoryItems()].filter(item => {
      if (T.importantOnly && !isImportant(item)) return false;
      if (T.track && item.track !== T.track) return false;
      if (period && Number.isFinite(period.startMs)) {
        const end = Number.isFinite(period.endMs) ? period.endMs : Date.now();
        return Number.isFinite(item.startMs) && item.startMs >= period.startMs && item.startMs <= end;
      }
      return true;
    });
    const dated = items.filter(item => Number.isFinite(item.startMs)).sort((a, b) => b.startMs - a.startMs);
    const unknown = items.filter(item => !Number.isFinite(item.startMs));
    for (const year of [...new Set(dated.map(item => yearOf(item.startMs)))].sort((a, b) => b - a)) {
      const group = dated.filter(item => yearOf(item.startMs) === year);
      children.push(yearGroup(year, group, group.filter(item => T.importantOnly || isImportant(item) || item.status === "planned")));
    }
    const earliest = value.coverage?.earliestMs;
    if (Number.isFinite(earliest)) {
      const day = new Date(earliest);
      children.push(A.el("div", "tl-list-marker", `聊天记录从 ${day.getFullYear()} 年 ${day.getMonth() + 1} 月开始`));
    }
    if (unknown.length) {
      const group = A.el("section", "tl-year");
      const head = A.button("", "tl-year-head", () => { T.unknownOpen = !T.unknownOpen; render(); }, { "aria-expanded": String(T.unknownOpen) });
      head.append(A.el("span", "", "时间不明"), A.el("span", "tl-count", `${unknown.length} 件`));
      group.append(head, fold(T.unknownOpen, unknown.map(eventRow)));
      children.push(group);
    }
    // 重绘会摘掉正在输入的时间框，这次失焦不算用户离开。
    T.rendering = true;
    host.replaceChildren(...children);
    T.rendering = false;
    const input = ui.whenInput;
    if (input && document.activeElement !== input) { input.focus(); input.select?.(); }
  }
  function fold(open, nodes) {
    const body = A.el("div", "tl-fold" + (open ? " open" : "")), inner = A.el("div", "tl-fold-inner");
    inner.append(...nodes);
    body.append(inner);
    return body;
  }
  function yearGroup(year, items, shown) {
    const group = A.el("section", "tl-year"), collapsed = T.collapsed.has(year), expanded = T.expanded.has(year);
    const head = A.button("", "tl-year-head", () => { collapsed ? T.collapsed.delete(year) : T.collapsed.add(year); render(); },
      { "aria-expanded": String(!collapsed) });
    head.append(A.el("span", "", String(year)), A.el("span", "tl-count", `${items.length} 件`));
    const nodes = (expanded ? items : shown).map(eventRow);
    if (items.length > shown.length)
      nodes.push(A.button(expanded ? "收起" : "展开全部事件", "fa-link tl-folded", () => {
        expanded ? T.expanded.delete(year) : T.expanded.add(year); render();
      }));
    group.append(head, fold(!collapsed, nodes));
    return group;
  }
  // 时间格是输入框的入口：回车或点到别处保存，Esc 放弃，清空表示时间不明。
  function whenCell(event) {
    if (event.fromMemory || A.restricted) return A.el("span", "tl-month", monthLabel(event));
    if (T.when?.id !== event.id)
      return A.button(monthLabel(event), "tl-month", () => { T.when = { id: event.id, text: whenInput(event.startMs, event.precision) }; render(); },
        { title: "修改时间" });
    const input = A.el("input", "tl-when-input");
    input.type = "text"; input.value = T.when.text; input.placeholder = "2024.05"; input.setAttribute("aria-label", "时间");
    input.addEventListener("input", () => { T.when.text = input.value; });
    input.addEventListener("keydown", key => {
      if (key.isComposing) return;
      if (key.key === "Enter") { key.preventDefault(); saveWhen(event, input.value, false); }
      if (key.key === "Escape") { T.when = null; T.error = ""; render(); }
    });
    input.addEventListener("blur", () => { if (!T.rendering && T.when?.id === event.id) saveWhen(event, input.value, true); });
    ui.whenInput = input;
    return input;
  }
  function saveWhen(event, text, leaving) {
    const value = text.trim(), parsed = value ? parseWhenInput(value) : null;
    if (value && !parsed) { if (leaving) T.when = null; T.error = WHEN_HINT; render(); return; }
    T.when = null;
    if (value === whenInput(event.startMs, event.precision)) { render(); return; }
    if (!parsed) { void patch(event, { startMs: null, endMs: null, precision: "unknown" }); return; }
    // 列表只显示开始时间；原本跨月的事件改开始时，结束时间不动。
    const spanning = Number.isFinite(event.endMs) && whenInput(event.endMs, event.precision) !== whenInput(event.startMs, event.precision);
    void patch(event, { ...parsed, endMs: spanning && event.endMs >= parsed.startMs ? event.endMs : parsed.endMs });
  }
  // 重要节点用主线色星标，其余用圆点，计划中的空心，来自关系记忆的用次要色；点一下切换重要程度。
  function mark(event) {
    const important = isImportant(event), planned = event.status === "planned", color = trackColor(event.track);
    const glyph = A.el("span", important ? "tl-star" : "tl-event-dot" + (planned ? " planned" : "") + (event.fromMemory ? " memory" : ""),
      important ? (planned ? "☆" : "★") : "");
    if (!event.fromMemory) { glyph.style.color = color; if (!important) glyph.style.background = color; }
    if (event.fromMemory || A.restricted) { const box = A.el("span", "tl-mark"); box.append(glyph); return box; }
    const title = important ? "改为一般事件" : "标为重要节点";
    const button = A.button("", "tl-mark", () => { void patch(event, { importance: important ? "general" : "important" }); },
      { title, "aria-label": title, "aria-pressed": String(important) });
    button.append(glyph);
    return button;
  }
  function eventRow(event) {
    const key = rowKey(event), open = T.open === key;
    const row = A.el("div", "tl-event" + (open ? " open" : "") + (event.fromMemory ? " memory" : ""));
    ui.rows.set(key, row);
    const head = A.el("div", "tl-event-head" + (T.when?.id === event.id && !event.fromMemory ? " editing" : ""));
    const name = A.el("span", "tl-name");
    name.append(A.el("span", "tl-title" + (isImportant(event) ? " important" : "") + (event.status === "cancelled" ? " cancelled" : "") +
      (event.status === "uncertain" || event.fromMemory ? " uncertain" : ""), string(event.title, 120)));
    if (event.status === "planned") name.append(A.el("span", "tl-hint", "计划"));
    if (event.status === "uncertain") name.append(A.el("span", "tl-hint", "待判断"));
    // 标题右侧写分类（8 类），来自关系记忆的写“关系记忆”。
    const side = event.fromMemory ? "关系记忆" : string(event.category, 12) || "其他";
    const toggle = A.button("", "tl-event-open", () => {
      T.open = open ? "" : key;
      if (!open) wantEvidence(event);
      render();
    }, { "aria-expanded": String(open) });
    toggle.append(name, A.el("span", "tl-category", side));
    head.append(whenCell(event), mark(event), toggle);
    if (!event.fromMemory && !A.restricted) head.addEventListener("contextmenu", menu => {
      menu.preventDefault();
      const rect = head.getBoundingClientRect();
      // 键盘（菜单键、Shift+F10）触发时没有指针坐标，菜单贴在这一行下方。
      openMenu(event, menu.clientX || rect.left + 48, menu.clientY || rect.bottom);
    });
    const detail = A.el("div", "tl-detail" + (open ? " open" : "")), inner = A.el("div", "tl-detail-inner");
    if (open) inner.append(event.fromMemory ? memoryDetail(event) : eventDetail(event));
    detail.append(inner);
    row.append(head, detail);
    return row;
  }

  // 确认、编辑、取消关联、删除放在右键菜单里，详情只用来看依据。
  function closeMenu() { T.menu?.remove(); T.menu = null; }
  function openMenu(event, x, y) {
    closeMenu();
    const menu = A.el("div", "tl-menu"); menu.setAttribute("role", "menu");
    const item = (text, action, danger = false) => {
      const node = A.button(text, "tl-menu-item" + (danger ? " fa-danger-text" : ""), () => { closeMenu(); action(); }, { role: "menuitem" });
      node.disabled = T.busy;
      menu.append(node);
    };
    const memories = linked(event);
    if (!event.manual && !memories.length) {
      const confirmed = event.level === "confirmed";
      item(confirmed ? "取消确认" : "确认", () => { void edit({ target: "event", action: confirmed ? "unconfirm" : "confirm", id: event.id }); });
    }
    item("编辑", () => { T.open = event.id; T.editing = event.id; wantEvidence(event); render(); });
    for (const memory of memories)
      item("取消与关系记忆的关联", () => {
        void edit({ target: "event", action: "unlink", id: event.id, memoryId: memory.id }).then(ok => {
          if (!ok) return;
          delete T.evidence[event.id];
          const fresh = T.value.events.find(item => item.id === event.id);
          if (fresh && T.open === event.id) { wantEvidence(fresh); render(); }
        });
      });
    item("删除", () => {
      void edit({ target: "event", action: "delete", id: event.id }).then(ok => {
        if (!ok) return;
        T.notice = `已删除“${string(event.title, 40)}”${memories.length ? "，只影响时间线，关系记忆里的这条不变" : ""}，按 Ctrl+Z 撤销`;
        T.open = ""; render();
      });
    }, true);
    document.body.append(menu);
    T.menu = menu;
    const { width, height } = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(x, innerWidth - width - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, innerHeight - height - 4))}px`;
    [...menu.children].find(node => !node.disabled)?.focus();
  }
  function evidenceList(box, key, manual, expected) {
    const evidence = T.evidence[key];
    if (Array.isArray(evidence?.items)) {
      for (const item of evidence.items.slice(0, 2)) box.append(evidenceChat(item));
      const total = count(evidence.total) ?? evidence.items.length;
      if (total > 2) {
        const more = A.button(`查看全部 ${total} 条依据`, "fa-link tl-more", () => {
          more.replaceWith(...evidence.items.slice(2).map(evidenceChat), A.el("p", "fa-note", `共 ${total} 条，显示最近 ${Math.min(50, evidence.items.length)} 条`));
        });
        box.append(more);
      }
    } else if (!manual && expected) box.append(A.el("p", "fa-note", "正在读取依据…"));
  }
  function eventDetail(event) {
    const box = A.el("div", "tl-detail-box");
    const parts = [`${string(event.whenText, 40)}${event.status === "planned" ? "（计划）" : ""}${event.inferred ? "（推断）" : ""}`,
      event.manual ? "手动添加" : LEVELS[event.level] || LEVELS.possible];
    box.append(A.el("p", "tl-line", parts.filter(Boolean).join(" · ")));
    const evidence = T.evidence[event.id];
    const phrase = string(event.phrase, 200) || string(evidence?.items?.[0]?.phrase, 200);
    if (phrase) {
      const said = Number.isFinite(event.saidMs) ? event.saidMs : evidence?.items?.[0]?.time;
      box.append(A.el("p", "tl-line", `“${phrase}”${Number.isFinite(said) ? "，" + fullDate(said) + " 说" : ""}`));
    }
    if (string(event.note)) box.append(A.el("p", "tl-line", "备注：" + string(event.note, 200)));
    // 已关联的关系记忆和 Agent 改过的痕迹各一行次要文字（doc 4.2、4.6）。
    for (const memory of linked(event))
      box.append(A.button("也记在关系记忆里：" + string(memory.text, 200), "tl-memory-link", () => A.selectTab("memories")));
    if (Number.isFinite(event.agentEditedMs)) {
      const day = new Date(event.agentEditedMs);
      box.append(A.el("p", "tl-line", `Agent 改过（${day.getMonth() + 1}月${day.getDate()}日）`));
    }
    evidenceList(box, event.id, event.manual, count(event.evidenceCount) || linked(event).length);
    if (string(event.reason)) box.append(A.el("p", "tl-line", string(event.reason, 600)));
    if (T.editing === event.id && !A.restricted) box.append(form("edit:" + event.id, () => eventForm(event)));
    return box;
  }
  // 来自关系记忆的只读行：记忆原文、依据，修改要到关系记忆页。
  function memoryDetail(item) {
    const box = A.el("div", "tl-detail-box");
    box.append(A.el("p", "tl-line", [string(item.whenText, 40), "来自关系记忆"].filter(Boolean).join(" · ")),
      A.el("p", "tl-line tl-memory-text", string(item.text, 600)));
    evidenceList(box, rowKey(item), false, count(item.evidenceCount));
    box.append(A.button(A.restricted ? "在关系记忆里查看" : "到关系记忆里修改", "fa-link tl-more", () => A.selectTab("memories")));
    return box;
  }
  // 依据只来自这一个私聊：不写聊天名称，以对话气泡显示，“我”在右边。
  function evidenceChat(item) {
    const chat = A.el("div", "tl-chat"), time = item.time;
    if (Number.isFinite(time)) chat.append(A.el("p", "tl-chat-head", fullDate(time)));
    const own = item.status === "missing" && !string(item.text) ? "（原消息已不在本机）" : item.text;
    const lines = [...(Array.isArray(item.before) ? item.before : []), { side: item.side, text: own, own: true },
      ...(Array.isArray(item.after) ? item.after : [])];
    for (const line of lines.filter(object)) {
      const row = A.el("div", "tl-msg" + (line.side === "self" ? " self" : ""));
      row.append(A.el("p", "tl-bubble" + (line.own ? " own" : ""), string(line.text, 600)));
      chat.append(row);
    }
    if (item.status === "changed") chat.append(A.el("p", "fa-note", "原消息已变化，这条依据可能过时。"));
    if (item.truncated === true) chat.append(A.el("p", "fa-note", "原文过长，本次仅分析部分文字。"));
    return chat;
  }
  function select(options, chosen, labelText) {
    const node = A.el("select");
    node.setAttribute("aria-label", labelText);
    for (const [key, text] of options) { const option = A.el("option", "", text); option.value = key; option.selected = key === chosen; node.append(option); }
    node.value = options.some(([key]) => key === chosen) ? chosen : options[0]?.[0] ?? "";
    return node;
  }
  function input(value, placeholder, labelText, maxLength = 0) {
    const node = A.el("input"); node.type = "text"; node.value = value || ""; node.placeholder = placeholder;
    node.setAttribute("aria-label", labelText);
    if (maxLength) node.maxLength = maxLength;
    return node;
  }
  // 事件表单：编辑和手动添加共用（自我剖析的添加是克隆编辑表单再换按钮，这里直接分两种模式）。
  function eventForm(event = null) {
    const box = A.el("div", "tl-edit");
    const title = input(event?.title, "标题", "标题", 40);
    const track = select(trackNames().map(name => [name, name]), event?.track || OURS, "主线");
    const category = select(categories().map(name => [name, name]), event?.category || "其他", "分类");
    const status = select(Object.entries(STATUS), event?.status || "occurred", "状态");
    const importance = select([["important", "重要节点"], ["general", "一般事件"]], event?.importance || "general", "重要程度");
    const start = input(event ? whenInput(event.startMs, event.precision) : "", "时间：2024、2024.05 或 2024.05.01", "时间");
    const end = input(event ? whenInput(event.endMs, event.precision) : "", "结束（可选）", "结束时间");
    const note = input(event?.note, "备注（可选）", "备注", 200);
    box.save = A.button(event ? "保存" : "添加", "fa-primary", () => {
      const fields = { title: title.value.trim(), track: track.value, category: category.value, status: status.value, importance: importance.value,
        note: note.value.trim() };
      if (!fields.title) { title.focus(); return; }
      const parsedStart = parseWhenInput(start.value);
      if ((!event || start.value.trim()) && !parsedStart) { T.error = WHEN_HINT; render(); return; }
      fields.precision = parsedStart ? parsedStart.precision : "unknown";
      fields.startMs = parsedStart ? parsedStart.startMs : null;
      const parsedEnd = end.value.trim() ? parseWhenInput(end.value) : null;
      if (end.value.trim() && !parsedEnd) { T.error = `结束${WHEN_HINT}`; render(); return; }
      fields.endMs = parsedEnd ? parsedEnd.endMs : null;
      void edit(event ? { target: "event", action: "edit", id: event.id, fields } : { target: "event", action: "add", fields })
        .then(ok => { if (!ok) return; if (event) T.editing = ""; else T.adding = null; render(); });
    });
    const cancel = A.button("取消", "fa-secondary", () => { if (event) T.editing = ""; else T.adding = null; render(); });
    box.append(title, track, category, status, importance, start, end, note, box.save, cancel);
    return box;
  }
  function periodForm(period = null) {
    const box = A.el("div", "tl-edit");
    const title = input(period?.title, "时期名称", "时期名称", 40);
    const track = select(trackNames().map(name => [name, name]), period?.track || OURS, "主线");
    const start = input(whenInput(period?.startMs, period?.precision), "开始：2022-09", "开始时间");
    const end = input(whenInput(period?.endMs, period?.precision), "结束：留空表示至今", "结束时间");
    box.save = A.button(period ? "保存" : "添加", "fa-primary", () => {
      const fields = { title: title.value.trim(), track: track.value };
      if (!fields.title) { title.focus(); return; }
      const parsedStart = parseWhenInput(start.value);
      if (!parsedStart) { T.error = `开始${WHEN_HINT}`; render(); return; }
      fields.startMs = parsedStart.startMs; fields.precision = parsedStart.precision;
      const parsedEnd = end.value.trim() ? parseWhenInput(end.value) : null;
      if (end.value.trim() && !parsedEnd) { T.error = `结束${WHEN_HINT}`; render(); return; }
      fields.endMs = parsedEnd ? parsedEnd.endMs : null;
      fields.ongoing = !end.value.trim();
      void edit({ target: "period", action: period ? "edit" : "add", id: period?.id, fields })
        .then(ok => { if (!ok) return; if (period) T.editing = ""; else T.adding = null; render(); });
    });
    const cancel = A.button("取消", "fa-secondary", () => { if (period) T.editing = ""; else T.adding = null; render(); });
    box.append(title, track, start, end, box.save, cancel);
    return box;
  }
  function renderAddForm() {
    const host = ui.addForm;
    host.hidden = T.adding === null || !T.value;
    if (host.hidden) { host.replaceChildren(); return; }
    const type = select([["event", "手动添加事件"], ["period", "手动添加时期"]], T.adding, "添加类型");
    type.addEventListener("change", () => { T.adding = type.value; render(); });
    host.replaceChildren(type, form("add:" + T.adding, () => T.adding === "period" ? periodForm() : eventForm()));
  }

  window.addEventListener("keydown", event => {
    if (event.key === "Escape") closeMenu();
    // 输入框里的 Ctrl+Z 留给文字撤销；其余时候撤销用户自己在时间线上的最近一次修改。受限窗口没有撤销。
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && String(event.key).toLowerCase() === "z" && !event.repeat &&
      ui && !ui.root.hidden && T.value && !A.restricted && !A.masked &&
      !event.target?.closest?.("textarea, select, [contenteditable], input:not([type=checkbox])")) {
      event.preventDefault(); void undo();
    }
  });
  window.addEventListener("pointerdown", event => { if (!event.target?.closest?.(".tl-menu")) closeMenu(); });
  window.addEventListener("blur", () => { closeMenu(); Timeline.hideTip(); });
  document.querySelector?.(".fa-main")?.addEventListener("scroll", () => { closeMenu(); Timeline.hideTip(); });

  window.FriendTimeline = { load, undo };
})();
