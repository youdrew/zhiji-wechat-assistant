"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 关系趋势: local message statistics over the whole readable history. No model, no tokens.
// The bridge sends per-message facts once; periods, metrics, zoom and selection are local.
(function () {
  const A = window.FriendApp, M = window.FriendTrendMath;
  const UNITS = window.KyfTimeTrend.UNITS;
  // 总览 shows every card over the message-count chart; each metric shows only its own cards.
  const METRICS = [["overview", "总览"], ["messages", "消息条数"], ["turns", "发言轮次"], ["chars", "文字量"], ["reply", "回复时间"]];
  const BAND_NAMES = ["1 分钟内", "1–10 分钟", "10 分钟–1 小时", "1 小时以上"];
  const KIND_NAMES = { image: "图片", voice: "语音", video: "视频", sticker: "表情", other: "其他" };
  const UNIT_WORDS = { minute: "分钟", hour: "小时", day: "天", week: "周", month: "个月", year: "年" };
  const DAILY = { messages: ["messages", "条"], turns: ["turns", "轮"], chars: ["chars", "字"] };
  // The selection runs from ``anchor`` (where it started) to ``focus`` (the end that moves), both
  // period indexes in the current view; ``range`` keeps its bounds in ms across reloads.
  const T = { data: null, facts: null, request: 0, revision: null, views: new Map(),
    sel: { anchor: 0, focus: 0 }, range: null, drill: null, scroll: new Map(), chart: null,
    settings: { unit: "month", metric: "overview", log: false, zoom: {} }, pending: new Map(),
    settingsRevision: 0, syncRequest: 0 };
  const span = () => [Math.min(T.sel.anchor, T.sel.focus), Math.max(T.sel.anchor, T.sel.focus)];

  // Keep clicks effective immediately; persistence belongs to the host outside this sandbox.
  function save(key, value) {
    ++T.settingsRevision;
    if (key === "zoom") T.settings.zoom[value.unit] = value.slot;
    else T.settings[key] = value;
    const pending = {};
    T.pending.set(key, pending);
    const finish = saved => {
      if (T.pending.get(key) !== pending) return;
      T.pending.delete(key);
      if (saved && !T.pending.size) syncSettings();
    };
    void A.host("trend.settings", { [key]: value }).then(() => finish(true), () => finish(false));
  }
  // Reconcile a context skipped during a save. A late read must not overwrite a newer click/context.
  function syncSettings() {
    const revision = T.settingsRevision, request = ++T.syncRequest;
    void A.host("trend.settings.get").then(settings => {
      if (revision === T.settingsRevision && request === T.syncRequest)
        window.FriendTrend.settingsChanged(settings);
    }).catch(() => { });
  }
  const unit = () => T.drill ? "minute" : T.settings.unit;
  const metric = () => T.settings.metric;
  const chartMetric = () => metric() === "overview" ? "messages" : metric();
  // Reply times use a linear axis unless the user turns the log scale on.
  const logScale = () => T.settings.log;
  let zoomTimer = null;
  function saveZoom(name, slot) {
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(() => save("zoom", { unit: name, slot: Math.round(slot * 10) / 10 }), 300);
  }

  async function load() {
    const request = ++T.request;
    try {
      const data = await A.rpc("page.trend");
      if (request !== T.request) return;
      T.data = data;
      T.facts = M.decode(data);
      T.revision = data.dataRevision;
      T.views.clear();
      render({ selection: T.range ?? "latest" });
    } catch (error) {
      if (request === T.request && !A.masked)
        A.byId("pageTrend").replaceChildren(A.el("div", "fa-empty", error.message || "趋势读取失败"));
    }
  }

  // The periods for the current unit (or the opened day, in minutes), computed once per load.
  const viewKey = () => T.drill ? "minute:" + T.drill.dayStart : unit();
  function currentView() {
    const name = unit(), key = viewKey();
    if (!T.views.has(key)) {
      const nowMs = T.data.cutoffMs;
      const range = T.drill ? { fromMs: T.drill.dayStart, toMs: M.next(T.drill.dayStart, "day"), nowMs } :
        { fromMs: T.data.history.firstMessageMs ?? nowMs, nowMs };
      const view = M.series(T.facts, name, range);
      const last = T.data.history.complete ? Infinity : T.data.history.lastMessageMs ?? -Infinity;
      view.unread = index => view.bounds[index] > last;     // not read yet while the history import runs
      T.views.set(key, view);
    }
    return T.views.get(key);
  }

  // -- the selected period's cards --------------------------------------------------------------
  function duration(value) {
    return value < 90 ? `${Math.round(value)} 秒` : value < 5400 ? `${Math.round(value / 60)} 分钟` : `${(value / 3600).toFixed(1)} 小时`;
  }
  // Mean and median need at least three replies; below that, say whether there were none or only a few.
  function reply(values, summary, side) {
    if (values[side] === null) return summary.replySamples[side] ? `仅 ${summary.replySamples[side]} 次` : "无回复";
    return duration(values[side]);
  }
  const sides = format => `我 ${format(0)} · 对方 ${format(1)}`;
  const pair = (values, suffix = "") => sides(side => values[side] + suffix);
  const ratio = (top, bottom) => bottom ? (top / bottom).toFixed(1) : "—";
  const share = values => values[0] + values[1] ? `我占 ${Math.round(values[0] / (values[0] + values[1]) * 100)}%` : "";
  function stat(label, value, sub) {
    const node = A.el("div", "fa-stat");
    node.append(A.el("div", "fa-stat-label", label), A.el("div", "fa-stat-value", value));
    if (sub) node.append(A.el("div", "fa-stat-sub", sub));
    return node;
  }
  // A full-width card with one row per side: me, then the other person.
  function table(label, columns, cell) {
    const node = A.el("div", "fa-stat wide"), grid = A.el("table", "fa-table"), head = A.el("tr");
    head.append(A.el("th"), ...columns.map(column => A.el("th", "", column)));
    grid.append(head);
    for (const [side, name] of [[0, "我"], [1, "对方"]]) {
      const row = A.el("tr");
      row.append(A.el("th", "", name), ...columns.map((_column, index) => cell(side, index)));
      grid.append(row);
    }
    node.append(A.el("div", "fa-stat-label", label), grid);
    return node;
  }
  function counted(count, total) {
    const node = A.el("td", "", count);
    if (total) node.append(A.el("span", "fa-faint", ` ${Math.round(count / total * 100)}%`));
    return node;
  }
  // Message kinds that occur in the period, text first.
  function kindTable(summary) {
    const used = T.data.kinds.map((kind, index) => [kind, index]).filter(([, index]) => summary.kinds[0][index] + summary.kinds[1][index]);
    if (!used.length) return null;
    return table("消息类型", used.map(([kind]) => kind === "text" ? "文字" : KIND_NAMES[kind]),
      (side, column) => counted(summary.kinds[side][used[column][1]], summary.messages[side]));
  }
  function nonText(summary, side) {
    return T.data.kinds.map((kind, index) => kind !== "text" && summary.kinds[side][index] ?
      `${KIND_NAMES[kind]} ${summary.kinds[side][index]}` : "").filter(Boolean).join("、") || "无";
  }
  const PANELS = {
    overview: summary => [
      stat("消息条数", pair(summary.messages)), stat("发言轮次", pair(summary.turns)),
      stat("文字量", pair(summary.chars, " 字")),
      stat("平均回复时间", sides(side => reply(summary.replyMeanSeconds, summary, side))),
      stat("谁先开聊", pair(summary.starts, " 次")),
      ...(summary.contactDays === null ? [] :
        [stat("联系天数", `${summary.contactDays} 天`), stat("双方都发言", `${summary.twoWayDays} 天`)]),
    ],
    messages: summary => [stat("消息条数", pair(summary.messages), share(summary.messages)), kindTable(summary)],
    turns: summary => [
      stat("发言轮次", pair(summary.turns), share(summary.turns)),
      stat("平均每轮", sides(side => ratio(summary.messages[side], summary.turns[side]) + " 条"), "一方连续发的几条算一轮"),
      stat("谁先开聊", pair(summary.starts, " 次"), "隔 8 小时以上再发消息算开聊"),
    ],
    chars: summary => [
      stat("文字量", pair(summary.chars, " 字"), share(summary.chars)),
      stat("平均每条文字", sides(side => ratio(summary.chars[side], summary.kinds[side][T.data.kinds.indexOf("text")]) + " 字")),
      stat("非文字消息", sides(side => nonText(summary, side)), "图片、语音、表情包等不折算字数"),
    ],
    reply: summary => [
      stat("平均回复时间", sides(side => reply(summary.replyMeanSeconds, summary, side)),
        `回复 我 ${summary.replySamples[0]} 次 · 对方 ${summary.replySamples[1]} 次`),
      stat("中位数", sides(side => reply(summary.replyMedianSeconds, summary, side)), "一半的回复快于此"),
      stat("最久一次", sides(side => summary.replyLongestSeconds[side] === null ? "无回复" : duration(summary.replyLongestSeconds[side]))),
      table("回复快慢", BAND_NAMES, (side, band) => counted(summary.replyBands[side][band], summary.replySamples[side])),
    ],
  };
  function rangeState(view, from, to) {
    let unread = false;
    for (let index = from; index <= to && !unread; index++) unread = view.unread(index);
    if (unread) return from === to ? "这段历史尚未读取，按已读部分统计" : "部分时期尚未读取，按已读部分统计";
    if (to === view.count - 1 && view.inProgress) return from === to ? "进行中" : "含进行中的周期";
    const first = T.data.history.firstMessageMs;
    if (!T.drill && from === 0 && first !== null && view.bounds[0] < first) return "可读历史从这个周期开始";
    return "";
  }
  // Title, length and daily average: cheap enough to follow a drag. Days count only from the start
  // of the readable history to now, and hours and minutes get no daily average.
  function renderHead() {
    const view = currentView(), name = unit(), [from, to] = span();
    const startMs = view.bounds[from], endMs = view.bounds[to + 1];
    T.range = { startMs, endMs };
    const first = T.data.history.firstMessageMs, daily = name !== "hour" && name !== "minute";
    const days = daily ? M.days(first === null ? startMs : Math.max(startMs, M.start(first, "day")), Math.min(endMs, T.data.cutoffMs)) : 0;
    const facts = [];
    if (to > from) facts.push(`共 ${to - from + 1} ${UNIT_WORDS[name]}`);
    if (to > from && name !== "day" && days > 1) facts.push(`${days} 天`);
    if (days > 1) {
      const [key, suffix] = DAILY[metric()] ?? DAILY.messages;
      let total = 0;
      for (let index = from; index <= to; index++) total += view.values[key][0][index] + view.values[key][1][index];
      const average = total / days;
      facts.push(`日均 ${average < 10 ? average.toFixed(1) : Math.round(average)} ${suffix}`);
    }
    const head = T.nodes.head, state = rangeState(view, from, to);
    head.replaceChildren(A.el("span", "fa-trend-title", M.title(startMs, endMs, name)));
    if (facts.length) head.append(A.el("span", "fa-trend-scope", facts.join(" · ")));
    if (state) head.append(A.el("span", "fa-note", state));
    if (from === to && (name === "hour" || name === "day"))
      head.append(A.el("span", "fa-spacer"), A.button("按分钟查看这一天", "fa-plain", () => openMinutes(startMs)));
  }
  function renderSelection() {
    renderHead();
    T.nodes.stats.replaceChildren(...PANELS[metric()](M.summary(T.facts, T.range.startMs, T.range.endMs)).filter(Boolean));
  }
  // Relationship and model usage share the same chart interaction implementation.
  function createChart() {
    const view = currentView(), name = unit(), kind = chartMetric();
    return window.KyfTimeTrend.createChart({ view, unit: name, key: viewKey(), scroll: T.scroll,
      classPrefix: "fa", label: METRICS.find(([value]) => value === kind)[1],
      // Bottom series first, preserving the relationship chart's existing self/other stack.
      series: kind === "reply" ? [{ values: view.values[kind][0], color: "--self" }, { values: view.values[kind][1], color: "--other" }] :
        [{ values: view.values[kind][1], color: "--other" }, { values: view.values[kind][0], color: "--self" }],
      mode: kind === "reply" ? "reply" : "stacked", logScale, zoom: T.settings.zoom[name], onZoom: saveZoom,
      hasData: view.values.messages[0].some(Boolean) || view.values.messages[1].some(Boolean),
      emptyText: kind === "reply" ? "这个粒度下每个周期的回复都不足 3 次" : "暂无消息",
      getSelection: () => T.sel, setSelection(selection, { preview }) {
        T.sel = selection;
        if (preview) renderHead(); else renderSelection();
      } });
  }

  // -- page -------------------------------------------------------------------------------------
  function segment(options, current, choose) {
    const node = A.el("div", "fa-segment");
    for (const [value, label] of options)
      node.append(A.button(label, "", () => choose(value), { "aria-pressed": String(current === value) }));
    return node;
  }
  function setUnit(value) {
    save("unit", value);
    T.drill = null;
    render({ selection: "latest" });
  }
  function setMetric(value) {
    save("metric", value);
    render();
  }
  // A day opens as minutes, starting at its first message (or the chosen hour's first message).
  function openMinutes(startMs) {
    const from = unit(), dayStart = M.start(startMs, "day");
    T.drill = { dayStart, back: T.range };
    const view = currentView();
    const offset = from === "hour" ? Math.round((startMs - dayStart) / 60000) : 0;
    const first = M.firstWithMessages(view, Math.min(offset, view.count - 1), Math.min(offset + (from === "hour" ? 60 : 1440), view.count));
    T.sel = { anchor: first, focus: first };
    T.scroll.delete(viewKey());
    render();
  }
  function closeMinutes() {
    const back = T.drill.back;
    T.drill = null;
    render({ selection: back });
  }
  function legend(kind) {
    const node = A.el("div", "fa-legend");
    const view = currentView();
    const keys = [["var(--self)", "我"], ["var(--other)", "对方"]];
    for (const [fill, label] of keys) {
      const item = A.el("span"), swatch = A.el("i");
      swatch.style.background = fill;
      item.append(swatch, label);
      node.append(item);
    }
    let unread = false;
    for (let index = 0; index < view.count && !unread; index++) unread = view.unread(index);
    if (unread) {
      const item = A.el("span"), swatch = A.el("i");
      swatch.style.border = "1px dashed var(--faint)";
      item.append(swatch, "尚未读取");
      node.append(item);
    }
    if (metric() === "overview") node.append(A.el("span", "", "柱子为消息条数"));
    if (kind === "reply") {
      const toggle = A.el("label", "fa-check"), box = A.el("input");
      box.type = "checkbox";
      box.checked = logScale();
      box.addEventListener("change", () => { save("log", box.checked); T.chart?.draw(); });
      toggle.append(box, "对数刻度");
      toggle.title = "开启后几秒和几小时能同时看清，但柱子高度不再与时长成正比";
      node.append(A.el("span", "", "平均回复时间 · 越高越慢"), toggle);
    }
    return node;
  }
  function details(data) {
    const node = A.el("details", "fa-details");
    node.append(A.el("summary", "", "统计口径"));
    const list = A.el("dl", "fa-kv");
    const add = (term, text) => list.append(A.el("dt", "", term), A.el("dd", "", text));
    add("发言轮次", "同一方连续发的多条算一轮；对方插话或间隔 8 小时以上算新的一轮。");
    add("文字量", "只数文字消息的字数，不计空格，微信表情代码如 [微笑] 算 1 个字；图片、语音、表情包等不折算字数，另列条数。");
    add("开聊", "距上一条已知消息至少 8 小时视为一次开聊；最早一段不判断谁主动。");
    add("回复时间", "对方发消息后我第一次接话计一次（反之亦然），从对方发完第一串消息算起：拆成几条发不加时间，" +
      "对方停顿 5 分钟以上再追问也不重新计时。距对方上一条消息 8 小时以上算新的开聊，不算回复。" +
      "平均值反映让对方等了多久，中位数反映边聊边回的节奏；一个周期内至少 3 次才显示。" +
      "图表纵轴默认按实际时长，可在图表下方开启对数刻度，便于同时看清几秒和几小时。慢回复不代表疏远。");
    add("操作", "点击柱子查看该周期；按住拖动框选一段时间，拖到边缘会自动滚动，Shift+点击或 Shift+方向键扩展范围；" +
      "滚轮缩放，Shift+滚轮、触控板或底部滚动条左右移动，方向键逐个切换。");
    add("时区", `${data.timezone}（UTC${data.utcOffsetMinutes >= 0 ? "+" : "-"}${Math.abs(data.utcOffsetMinutes / 60)}）`);
    add("可读历史", data.history.firstMessageMs ?
      `自 ${A.formatDate(data.history.firstMessageMs)} 起，已读取 ${data.history.readCount} 条` : "暂无可读消息");
    const excluded = data.coverage.unknownDirection + data.coverage.untimed + data.coverage.future;
    add("未计入", `${excluded} 条方向或时间无法确认；${data.coverage.ambiguousSameTime} 组同一秒双方消息不计回复`);
    node.append(list);
    return node;
  }
  // selection: "latest" (newest period with messages), "keep" (same indexes) or {startMs, endMs}.
  function render({ selection = "keep" } = {}) {
    const root = A.byId("pageTrend");
    if (A.masked || !T.data) return;
    if (T.chart) { T.chart.destroy(); T.chart = null; }
    const data = T.data, view = currentView(), name = unit(), kind = chartMetric();
    const clamp = index => Math.max(0, Math.min(index, view.count - 1));
    if (selection === "latest") { const latest = M.latestWithMessages(view); T.sel = { anchor: latest, focus: latest }; }
    else if (selection && typeof selection === "object") {
      const first = M.lower(view.bounds, selection.startMs + 1) - 1, last = M.lower(view.bounds, selection.endMs) - 1;
      T.sel = { anchor: clamp(first), focus: clamp(Math.max(first, last)) };
    }
    T.sel = { anchor: clamp(T.sel.anchor), focus: clamp(T.sel.focus) };
    const bar = A.el("div", "fa-bar");
    if (T.drill) {
      bar.append(A.button("返回", "fa-secondary", closeMinutes),
        A.el("span", "fa-trend-scope", M.title(T.drill.dayStart, M.next(T.drill.dayStart, "day"), "day") + " · 按分钟"));
    } else bar.append(segment(UNITS, name, setUnit));
    bar.append(segment(METRICS, metric(), setMetric), A.el("span", "fa-spacer"),
      A.el("span", "fa-note", `截至 ${A.formatDate(data.cutoffMs, true)}`));
    T.nodes = { head: A.el("div", "fa-trend-head"), stats: A.el("div", "fa-stats") };
    root.replaceChildren(bar, T.nodes.head, T.nodes.stats);
    if (data.insufficient.length) root.append(A.el("p", "fa-note", "数据不足：" + data.insufficient.join("；")));
    T.chart = createChart();
    root.append(T.chart.element, legend(kind), details(data));
    renderSelection();
    T.chart.draw();
  }

  window.FriendTrend = {
    load,
    historyChanged(revision) { if (T.data && revision !== T.revision) void load(); },
    settingsChanged(settings) {
      if (!settings || A.masked) return;
      ++T.settingsRevision;
      const previous = { ...T.settings };
      for (const key of ["unit", "metric", "log", "zoom"])
        if (!T.pending.has(key) && settings[key] !== undefined) T.settings[key] = settings[key];
      if (!T.data) return;
      if (previous.unit !== T.settings.unit) { T.drill = null; render({ selection: "latest" }); }
      else {
        const name = unit(), before = previous.zoom[name], after = T.settings.zoom[name], zoomChanged = before !== after;
        // A remote preference needs a new canvas slot width. Keep the same leftmost
        // period when both widths are known; the chart still reveals the selection focus.
        const key = viewKey(), left = T.scroll.get(key);
        if (zoomChanged && left !== undefined && Number.isFinite(before) && before >= 2 && before <= 60 &&
          Number.isFinite(after) && after >= 2 && after <= 60) T.scroll.set(key, left / before * after);
        if (previous.metric !== T.settings.metric || previous.log !== T.settings.log || zoomChanged) render();
        else T.chart?.draw();
      }
    },
  };
})();
