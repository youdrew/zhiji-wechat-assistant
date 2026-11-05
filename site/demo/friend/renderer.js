"use strict";
// Public demo snapshot from 5a077bd: page-core, time-trend, persona-charts, friend-trend-math,
// friend-trend, friend-overview, friend-persona, friend-chat, and friend-memory, in that order.
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared helpers of the 知己分析 scheme page (moved from the window's friend-core.js, 2026-10-13).
// The page runs in a sandboxed frame; data comes from this plugin (A.rpc) or from the host (A.host).
(function () {
  const byId = id => document.getElementById(id);
  let masked = false;

  function el(tag, className = "", text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function button(label, className, onClick, attributes = {}) {
    const node = el("button", className, label);
    node.type = "button";
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }
  const pad = value => String(value).padStart(2, "0");
  function formatDate(ms, withTime = false) {
    if (!Number.isFinite(ms) || ms <= 0) return "时间未知";
    const date = new Date(ms);
    const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    return withTime ? `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}` : day;
  }
  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    if (seconds < 90) return `${Math.round(seconds)} 秒`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟`;
    if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} 小时`;
    return `${(seconds / 86400).toFixed(1)} 天`;
  }
  let toastTimer = null;
  function toast(message) {
    const node = byId("faToast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
  }
  // Sandboxed frames get no window.confirm() (Electron drops it silently); a small page dialog asks
  // instead. Modified 2026-10-14. Escape, the backdrop and 取消 answer no.
  function confirm(message, action = "确定") {
    return new Promise(resolve => {
      const overlay = el("div", "fa-confirm"), card = el("div", "fa-confirm-card"), actions = el("div", "fa-confirm-actions");
      const onKey = event => { if (event.key === "Escape") done(false); };
      const done = value => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
      const cancel = button("取消", "fa-plain", () => done(false));
      actions.append(cancel, button(action, "fa-plain fa-danger-text", () => done(true)));
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.append(el("p", "", message), actions);
      overlay.append(card);
      overlay.addEventListener("click", event => { if (event.target === overlay) done(false); });
      document.addEventListener("keydown", onKey);
      document.body.append(overlay);
      cancel.focus();
    });
  }
  function mask(message) {
    if (masked) return;
    masked = true;
    for (const id of ["pageOverview", "pagePersona", "pageValues", "pageChat", "pageTrend", "faSheetBody"]) byId(id).replaceChildren();
    byId("pageOverview").hidden = false;
    byId("pageOverview").append(el("div", "fa-empty", message));
  }
  function failed(error) {
    if (["account-changed", "account-unavailable", "account-paused", "account-closed"].includes(error?.code))
      mask("微信账号已切换或正在清理，此页面的资料已隐藏。");
    return error;
  }
  const rpc = (method, params) => masked ? Promise.reject(new Error("页面已失效")) :
    window.kyfHost.rpc(method, params).catch(error => { throw failed(error); });
  const host = (method, params) => masked ? Promise.reject(new Error("页面已失效")) :
    window.kyfHost.host(method, params).catch(error => { throw failed(error); });

  // -- sheet (inside this page) ----------------------------------------------------------------------
  let sheetOwner = null;
  function openSheet(title, owner) {
    sheetOwner = owner;
    byId("faSheetTitle").textContent = title;
    byId("faSheetBody").replaceChildren();
    byId("faSheet").hidden = false;
    return byId("faSheetBody");
  }
  function closeSheet() {
    sheetOwner = null;
    byId("faSheet").hidden = true;
    byId("faSheetBody").replaceChildren();
  }
  byId("faSheetClose").addEventListener("click", closeSheet);
  window.addEventListener("keydown", event => { if (event.key === "Escape" && !byId("faSheet").hidden) closeSheet(); });

  // -- drafts of the user's own observations are kept by the host -------------------------------------
  const drafts = { saved: new Map(), closed: {}, active: new Map(), timers: new Map() };
  async function loadDrafts() {
    const data = await host("drafts.list");
    drafts.saved = new Map((data.drafts || []).map(item => [item.entry, item]));
    drafts.closed = data.closedGenerations || {};
    return drafts.saved;
  }
  function draft(entry) {
    let state = drafts.active.get(entry);
    if (state) return state;
    const existing = drafts.saved.get(entry);
    state = { entry, generation: existing ? existing.generation : Number(drafts.closed[entry] || 0) + 1,
      seq: existing ? existing.seq : 0, payload: existing ? existing.payload : null,
      baseVersion: existing ? existing.baseVersion : null, restored: !!existing, dirty: false };
    drafts.active.set(entry, state);
    return state;
  }
  async function flushDraft(state) {
    clearTimeout(drafts.timers.get(state.entry));
    drafts.timers.delete(state.entry);
    if (!state.dirty || masked) return true;
    state.dirty = false;
    try {
      const result = await host("drafts.save", { entry: state.entry, generation: state.generation, seq: ++state.seq,
        payload: state.payload, baseVersion: state.baseVersion });
      return result?.saved === true || drafts.active.get(state.entry) !== state;
    } catch {
      if (drafts.active.get(state.entry) === state) state.dirty = true;
      return masked;
    }
  }
  function updateDraft(state, payload, baseVersion = null) {
    if (masked) return;
    state.payload = payload;
    state.baseVersion = baseVersion;
    state.dirty = true;
    clearTimeout(drafts.timers.get(state.entry));
    drafts.timers.set(state.entry, setTimeout(() => { void flushDraft(state); }, 500));
  }
  async function closeDraft(entry) {
    const state = drafts.active.get(entry) || (drafts.saved.has(entry) ? draft(entry) : null);
    if (!state) return;
    clearTimeout(drafts.timers.get(entry));
    drafts.timers.delete(entry);
    drafts.active.delete(entry);
    drafts.saved.delete(entry);
    drafts.closed[entry] = state.generation;
    try { await host("drafts.close", { entry, generation: state.generation }); } catch { }
  }
  async function flushDrafts() {
    if (masked) return true;
    return (await Promise.all([...drafts.active.values()].map(flushDraft))).every(Boolean);
  }

  // Five messages on each side of a cited one, read by the host from the managed history.
  function contextList(data) {
    const box = el("div", "fa-context");
    if (data.status !== "ok") {
      box.append(el("p", "fa-note", data.status === "unready" ? "聊天历史仍在读取，稍后再看。" : "原文已不在当前可读历史中。"));
      return box;
    }
    for (const message of data.messages) {
      const row = el("div", message.target ? "fa-msg target" : "fa-msg");
      row.append(el("div", "fa-msg-head", `${message.speaker} · ${formatDate(message.timeMs, true)}`),
        el("div", "fa-msg-text", message.text));
      box.append(row);
    }
    return box;
  }

  window.FriendApp = {
    byId, el, button, formatDate, formatDuration, toast, confirm, mask, rpc, host, openSheet, closeSheet, contextList,
    loadDrafts, draft, updateDraft, closeDraft, flushDrafts, savedDrafts: () => drafts.saved,
    selectTab: tab => { void host("tab.select", { tab }).catch(() => { }); },
    get scope() { return window.kyfHost.context() || {}; },
    // An Agent-opened (read-only) 知己分析 window: no edit controls (its host refuses saves anyway).
    get restricted() { return (window.kyfHost.context() || {}).restricted === true; },
    get masked() { return masked; },
    get sheetOwner() { return sheetOwner; },
  };
})();

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared local-time calendar and canvas interactions for relationship and model-usage trends.
// No application state or storage: the caller supplies values, selection and preferences.
(function (root) {
  const UNITS = [["hour", "小时"], ["day", "天"], ["week", "周"], ["month", "月"], ["year", "年"]];
  const pad = value => String(value).padStart(2, "0");
  const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  function start(ms, unit) {
    const date = new Date(ms);
    if (unit === "minute") date.setSeconds(0, 0);
    else if (unit === "hour") date.setMinutes(0, 0, 0);
    else {
      date.setHours(0, 0, 0, 0);
      if (unit === "week") date.setDate(date.getDate() - (date.getDay() + 6) % 7);
      else if (unit === "month") date.setDate(1);
      else if (unit === "year") date.setMonth(0, 1);
    }
    return date.getTime();
  }
  function next(ms, unit) {
    const date = new Date(ms);
    if (unit === "minute") date.setMinutes(date.getMinutes() + 1);
    else if (unit === "hour") date.setHours(date.getHours() + 1);
    else if (unit === "day") date.setDate(date.getDate() + 1);
    else if (unit === "week") date.setDate(date.getDate() + 7);
    else if (unit === "month") date.setMonth(date.getMonth() + 1);
    else date.setFullYear(date.getFullYear() + 1);
    return date.getTime();
  }
  // First index whose value is >= target in an ascending array.
  function lower(values, target, from = 0, to = values.length) {
    while (from < to) {
      const middle = (from + to) >>> 1;
      if (values[middle] < target) from = middle + 1; else to = middle;
    }
    return from;
  }
  // Calendar days touched by [startMs, endMs); a partial day counts as one.
  function days(startMs, endMs) {
    let count = 0;
    for (let at = start(startMs, "day"); at < endMs; at = next(at, "day")) count++;
    return count;
  }

  function parts(ms) {
    const date = new Date(ms);
    return { date, year: date.getFullYear(), month: pad(date.getMonth() + 1), day: pad(date.getDate()),
      hour: pad(date.getHours()), minute: pad(date.getMinutes()) };
  }
  // Short axis text for one bar.
  function axisLabel(ms, unit) {
    const p = parts(ms);
    if (unit === "year") return String(p.year);
    if (unit === "month") return `${p.year}-${p.month}`;
    if (unit === "day" || unit === "week") return `${p.year}-${p.month}-${p.day}`;
    if (unit === "hour") return p.hour === "00" ? `${p.month}-${p.day}` : `${p.hour}:00`;
    return `${p.hour}:${p.minute}`;
  }
  // Full name of a selected period, or of a range when [startMs, endMs) spans several periods.
  function title(startMs, endMs, unit) {
    const p = parts(startMs);
    if (endMs > next(startMs, unit)) {
      const last = parts(endMs - 1), year = last.year === p.year ? "" : last.year;
      if (unit === "year") return `${p.year} 至 ${last.year} 年`;
      if (unit === "month") return `${p.year} 年 ${Number(p.month)} 月至 ${year && year + " 年 "}${Number(last.month)} 月`;
      if (unit === "day" || unit === "week") return `${p.year}-${p.month}-${p.day} 至 ${year && year + "-"}${last.month}-${last.day}`;
      const end = parts(endMs), from = `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
      if (end.day === p.day && end.year === p.year && end.month === p.month) return `${from}–${end.hour}:${end.minute}`;
      return `${from} 至 ${end.year === p.year ? "" : end.year + "-"}${end.month}-${end.day} ${end.hour}:${end.minute}`;
    }
    if (unit === "year") return `${p.year} 年`;
    if (unit === "month") return `${p.year} 年 ${Number(p.month)} 月`;
    if (unit === "week") {
      const end = parts(endMs - 1);
      return `${p.year}-${p.month}-${p.day} 至 ${end.year === p.year ? "" : end.year + "-"}${end.month}-${end.day}`;
    }
    const day = `${p.year}-${p.month}-${p.day}`;
    if (unit === "day") return `${day} ${WEEKDAYS[p.date.getDay()]}`;
    if (unit === "hour") return `${day} ${p.hour}:00–${parts(endMs).hour}:00`;
    return `${day} ${p.hour}:${p.minute}`;
  }
  // Which bars get an axis label. Coarse units count back from the newest bar; hours and
  // minutes snap to round clock times so labels read 06:00, 12:00 or 14:30.
  const CLOCK_STEPS = { minute: [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440], hour: [1, 2, 3, 6, 12, 24] };
  function labelRule(unit, slot, width, count) {
    const every = Math.max(1, Math.ceil(width / slot));
    if (unit !== "minute" && unit !== "hour") return (index, _ms) => (count - 1 - index) % every === 0;
    const steps = CLOCK_STEPS[unit], step = steps.find(value => value >= every);
    if (step) return (_index, ms) => {
      const date = new Date(ms);
      const clock = unit === "minute" ? date.getHours() * 60 + date.getMinutes() : date.getHours();
      return clock % step === 0;
    };
    const days = Math.ceil(every / (unit === "minute" ? 1440 : 24));
    return (_index, ms) => {
      const date = new Date(ms);
      return date.getHours() === 0 && date.getMinutes() === 0 && (date.getDate() - 1) % days === 0;
    };
  }

  const HEIGHT = 180, GUTTER = 44, RIGHT = 12, TOP = 16, BASE = 148, PLOT = BASE - TOP;
  const MIN_SLOT = 2, MAX_SLOT = 60, LOG_TOP = Math.log10(86400);
  const DEFAULT_SLOT = { minute: 8, hour: 6, day: 6, week: 12, month: 24, year: 40 };
  const LABEL_WIDTH = { minute: 40, hour: 44, day: 72, week: 72, month: 52, year: 40 };
  const TICKS = [[60, "1分"], [600, "10分"], [3600, "1时"], [36000, "10时"]];
  const STEPS = [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
  const clock = seconds => seconds < 60 ? `${seconds}秒` : seconds < 3600 ? `${seconds / 60}分` : `${seconds / 3600}时`;
  const compact = value => value >= 10000 ? `${(value / 10000).toFixed(value >= 100000 ? 0 : 1)}万` : String(Math.round(value));

  // Values are drawn bottom-to-top for stacked series; reply series use grouped bars.
  // The caller owns selection and range summaries. Preview updates run while dragging;
  // committed updates run on release, a click, or keyboard selection. Palette and series
  // colors accept CSS custom-property names, var(--name), or literal colors. onZoom is
  // notification only: the mounted chart has already kept the cursor's period in place.
  function createChart(options) {
    const { view, unit: name = view.unit, getSelection, setSelection, onZoom = () => {},
      zoom = null, scroll = new Map(), key = name, classPrefix = "tt", mode = "stacked" } = options;
    const series = options.series, reply = mode === "reply";
    const span = () => { const selection = getSelection(); return [Math.min(selection.anchor, selection.focus), Math.max(selection.anchor, selection.focus)]; };
    const unread = view.unread || (() => false), unknown = options.unknown || (() => false);
    const palette = { selection: "--select", line: "--line", faint: "--faint", unknown: "--faint", ...options.palette };
    let maximum = 1, any = false;
    for (let index = 0; index < view.count; index++) {
      let total = 0;
      for (const item of series) {
        const value = item.values[index];
        if (!Number.isFinite(value)) continue;
        if (reply) { any = true; maximum = Math.max(maximum, value); }
        else { total += value; if (value > 0) any = true; }
      }
      if (!reply) maximum = Math.max(maximum, total);
    }
    if (options.hasData !== undefined && !reply) any = options.hasData;
    const element = (tag, className) => { const node = document.createElement(tag); node.className = className; return node; };
    const wrap = element("div", classPrefix + "-chart"), canvas = document.createElement("canvas");
    const scroller = element("div", classPrefix + "-chart-scroll"), track = element("div", classPrefix + "-chart-track");
    scroller.append(track);
    scroller.tabIndex = 0;
    scroller.setAttribute("role", "group");
    scroller.setAttribute("aria-label", `${options.label}，共 ${view.count} 个周期。` +
      "方向键选择周期，Shift+方向键扩展范围，拖动框选，滚轮缩放，Shift+滚轮平移");
    wrap.append(canvas, scroller);
    let slot = null, placed = false, frame = 0, destroyed = false;

    function layout(width) {
      if (slot === null) {
        const saved = zoom;
        slot = saved >= MIN_SLOT && saved <= MAX_SLOT ? saved :
          Math.min(MAX_SLOT, Math.max(DEFAULT_SLOT[name], (width - GUTTER - RIGHT) / view.count));
      }
      track.style.width = Math.max(width, Math.ceil(GUTTER + view.count * slot + RIGHT)) + "px";
    }
    function paint() {
      frame = 0;
      const width = scroller.clientWidth;
      if (!width) return;
      layout(width);
      if (!placed) { placed = true; place(); }
      const ratio = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(HEIGHT * ratio)) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(HEIGHT * ratio);
        canvas.style.width = width + "px";
        canvas.style.height = HEIGHT + "px";
      }
      const g = canvas.getContext("2d"), css = getComputedStyle(options.themeElement || wrap), log = reply && options.logScale?.();
      const color = (value, fallback = "#8a8a8a") => {
        if (typeof value !== "string" || !value) return fallback;
        const token = value.startsWith("var(") ? value.slice(4, -1).trim() : value;
        return token.startsWith("--") ? css.getPropertyValue(token).trim() || fallback : token;
      };
      g.setTransform(ratio, 0, 0, ratio, 0, 0);
      g.clearRect(0, 0, width, HEIGHT);
      g.font = `10px ${css.fontFamily}`;
      const left = scroller.scrollLeft, gap = slot >= 4 ? slot * 0.3 : slot >= 2.5 ? 0.5 : 0, bar = slot - gap;
      const from = Math.max(0, Math.floor(left / slot) - 1), to = Math.min(view.count, Math.ceil((left + width) / slot) + 1);
      const xOf = index => GUTTER + index * slot - left;
      g.save();
      g.beginPath();
      g.rect(GUTTER, 0, width - GUTTER, HEIGHT);
      g.clip();
      const [first, last] = span();
      if (last >= from && first < to) {
        g.fillStyle = color(palette.selection, "rgba(128,128,128,.15)");
        g.fillRect(xOf(Math.max(first, from)), TOP - 8, (Math.min(last + 1, to) - Math.max(first, from)) * slot, PLOT + 8);
      }
      for (let index = from; index < to; index++) {
        const x = xOf(index) + gap / 2;
        if (unread(index)) {
          g.strokeStyle = color(palette.line);
          g.setLineDash([3, 3]);
          g.strokeRect(x + 0.5, TOP + 0.5, Math.max(1, bar - 1), PLOT - 1);
          g.setLineDash([]);
        } else if (reply) {
          for (let side = 0; side < series.length; side++) {
            const value = series[side].values[index];
            if (!Number.isFinite(value)) continue;
            const height = Math.max(1, log ? Math.log10(Math.max(1, value)) / LOG_TOP * PLOT : value / maximum * PLOT);
            g.fillStyle = color(series[side].color);
            g.fillRect(x + side * bar / series.length, BASE - height, bar / series.length, height);
          }
        } else {
          let bottom = BASE;
          for (const item of series) {
            if (!Number.isFinite(item.values[index])) continue;
            const height = item.values[index] / maximum * PLOT;
            g.fillStyle = color(item.color);
            g.fillRect(x, bottom - height, bar, height);
            bottom -= height;
          }
        }
        // Usage can be partially known: keep the reported bars and mark the period whose
        // total is incomplete, rather than giving the missing portion a zero-height bar.
        if (!unread(index) && unknown(index)) {
          g.strokeStyle = color(palette.unknown);
          g.setLineDash([3, 3]);
          g.strokeRect(x + 0.5, TOP + 0.5, Math.max(1, bar - 1), PLOT - 1);
          g.setLineDash([]);
        }
      }
      const labelled = labelRule(name, slot, LABEL_WIDTH[name], view.count);
      g.fillStyle = color(palette.faint);
      g.textAlign = "center";
      g.textBaseline = "alphabetic";
      for (let index = from; index < to; index++) {
        if (!labelled(index, view.bounds[index])) continue;
        const text = index === view.count - 1 && view.inProgress && name !== "minute" && name !== "hour" ? "进行中" :
          axisLabel(view.bounds[index], name);
        g.fillText(text, xOf(index) + slot / 2, HEIGHT - 14);
      }
      g.restore();
      // Gutter: the scale, fixed while the bars scroll.
      g.strokeStyle = color(palette.line);
      g.fillStyle = color(palette.faint);
      g.textAlign = "right";
      g.textBaseline = "middle";
      const rule = (y, text, dashed) => {
        g.setLineDash(dashed ? [2, 3] : []);
        g.beginPath();
        g.moveTo(GUTTER, Math.round(y) + 0.5);
        g.lineTo(width, Math.round(y) + 0.5);
        g.stroke();
        if (text) g.fillText(text, GUTTER - 6, y);
      };
      rule(BASE, reply ? "" : "0", false);
      if (log) for (const [seconds, text] of TICKS) rule(BASE - Math.log10(seconds) / LOG_TOP * PLOT, text, true);
      else if (reply) {
        // Round clock steps, at most four lines below the slowest period.
        const step = STEPS.find(value => maximum / value <= 4) ?? STEPS[STEPS.length - 1];
        for (let seconds = step; seconds <= maximum; seconds += step) rule(BASE - seconds / maximum * PLOT, clock(seconds), true);
      } else rule(TOP, compact(maximum), true);
      g.setLineDash([]);
      if (!any) {
        g.textAlign = "center";
        g.fillText(options.emptyText || (reply ? "这个粒度下每个周期的回复都不足 3 次" : "暂无数据"), GUTTER + (width - GUTTER) / 2, TOP + PLOT / 2);
      }
    }
    const draw = () => { if (!destroyed && !frame) frame = requestAnimationFrame(paint); };

    function visible(index) {
      const x = GUTTER + index * slot - scroller.scrollLeft;
      return x >= GUTTER && x + slot <= scroller.clientWidth;
    }
    function reveal(index) {
      if (slot === null || visible(index)) return;
      const width = scroller.clientWidth;
      scroller.scrollLeft = GUTTER + index * slot + slot / 2 - (GUTTER + (width - GUTTER) / 2);
    }
    function select(index, { extend = false, reveal: revealSelection = true } = {}) {
      const at = Math.max(0, Math.min(view.count - 1, index));
      setSelection(extend ? { anchor: getSelection().anchor, focus: at } : { anchor: at, focus: at }, { preview: false });
      if (revealSelection) reveal(at);
      draw();
    }
    // Open at the remembered position, else at the newest period; a drill-down can choose
    // the first useful period without changing an all-day selection or a remembered pan.
    function place() {
      const kept = scroll.get(key);
      scroller.scrollLeft = kept ?? scroller.scrollWidth;
      const first = options.initialIndex;
      const initial = Number.isInteger(first) && first >= 0 && first < view.count;
      if (initial && kept !== undefined) return;
      reveal(initial ? first : getSelection().focus);
    }
    scroller.addEventListener("scroll", () => {
      if (placed) {
        const atEnd = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 2;
        if (atEnd) scroll.delete(key); else scroll.set(key, scroller.scrollLeft);
      }
      draw();
    });
    scroller.addEventListener("wheel", event => {
      if (slot === null || event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      event.preventDefault();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1);
      const next = Math.max(MIN_SLOT, Math.min(MAX_SLOT, slot * Math.exp(-delta * 0.002)));
      if (next === slot) return;
      const x = event.clientX - scroller.getBoundingClientRect().left;
      const position = (scroller.scrollLeft + Math.max(x, GUTTER) - GUTTER) / slot;
      slot = next;
      layout(scroller.clientWidth);
      scroller.scrollLeft = position * slot + GUTTER - Math.max(x, GUTTER);
      onZoom(name, slot);
      draw();
    }, { passive: false });
    // Click selects a period, Shift+click extends from the anchor, dragging selects a range.
    // Panning is left to the scrollbar, Shift+wheel and trackpads; near either edge a drag scrolls.
    let press = null;
    const pointerX = event => event.clientX - scroller.getBoundingClientRect().left;
    const indexAt = x => Math.max(0, Math.min(view.count - 1, Math.floor((scroller.scrollLeft + x - GUTTER) / slot)));
    function stretch() {
      const focus = indexAt(press.x);
      if (focus === getSelection().focus) return;
      setSelection({ anchor: getSelection().anchor, focus }, { preview: true });
      draw();
    }
    function edge() {
      if (!press?.moved) return;
      const width = scroller.clientWidth, x = press.x, zone = 24;
      const push = x < GUTTER + zone ? x - GUTTER - zone : x > width - zone ? x - width + zone : 0;
      if (push) {
        scroller.scrollLeft += Math.max(-40, Math.min(40, push / 2));
        stretch();
      }
      press.frame = requestAnimationFrame(edge);
    }
    scroller.addEventListener("pointerdown", event => {
      const x = pointerX(event), y = event.clientY - scroller.getBoundingClientRect().top;
      if (event.button !== 0 || y > HEIGHT || x < GUTTER || slot === null) return;     // leave the scrollbar alone
      press = { start: x, x, id: event.pointerId, moved: false, frame: 0, before: { ...getSelection() }, extending: event.shiftKey };
      if (event.shiftKey) select(indexAt(x), { extend: true, reveal: false });
      else { setSelection({ anchor: indexAt(x), focus: indexAt(x) }, { preview: true }); draw(); }
      event.preventDefault();
      scroller.focus({ preventScroll: true });
    });
    scroller.addEventListener("pointermove", event => {
      if (!press || event.pointerId !== press.id) return;
      press.x = pointerX(event);
      if (!press.moved && Math.abs(press.x - press.start) > 4) {
        press.moved = true;
        scroller.setPointerCapture(press.id);
        scroller.classList.add("selecting");
        press.frame = requestAnimationFrame(edge);
      }
      if (press.moved) stretch();
    });
    const release = event => {
      if (!press || event.pointerId !== press.id) return;
      cancelAnimationFrame(press.frame);
      const { moved, extending, before } = press;
      press = null;
      scroller.classList.remove("selecting");
      if (event.type === "pointercancel" && !moved) setSelection(before, { preview: true });       // e.g. a touch pan took over
      if (moved || !extending) setSelection({ ...getSelection() }, { preview: false });                       // Shift+click rendered already
      draw();
    };
    scroller.addEventListener("pointerup", release);
    scroller.addEventListener("pointercancel", release);
    scroller.addEventListener("keydown", event => {
      const moves = { ArrowLeft: getSelection().focus - 1, ArrowRight: getSelection().focus + 1, Home: 0, End: view.count - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      select(moves[event.key], { extend: event.shiftKey });
    });
    const observer = new ResizeObserver(draw);
    observer.observe(scroller);
    // Changes to inherited palette variables repaint a mounted canvas without rebuilding it.
    const themeObserver = typeof MutationObserver === "undefined" ? null : new MutationObserver(draw);
    for (const node of new Set([document.documentElement, document.body, options.themeElement].filter(Boolean)))
      themeObserver?.observe(node, { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
    return { element: wrap, draw, reveal, select, isSelecting: () => press !== null, destroy() {
      destroyed = true;
      observer.disconnect();
      themeObserver?.disconnect();
      if (frame) cancelAnimationFrame(frame);
      if (press) cancelAnimationFrame(press.frame);
      press = null;
    } };
  }


  root.KyfTimeTrend = { UNITS, calendar: { start, next, lower, days, axisLabel, title, labelRule }, createChart };
})(typeof window !== "undefined" ? window : globalThis);

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared 性格分析 / 三观分析 charts for 自我剖析 and 知己分析 (added 2026-10-25).
// The catalog mirrors plugin-sdk/python/kyf_plugin/persona.py, except that the Big Five and attachment are named
// as the charts show them (能量 for 外向性, 焦虑 for 依恋焦虑). Positions run from -1 (first end) to 1 (second end)
// and come from the plugin; this file only draws them. No storage, no requests.
(function (root) {
  const DIMENSIONS = {
    big5_o: ["心智", "低", "高"], big5_c: ["应对方式", "低", "高"], big5_e: ["能量", "低", "高"],
    big5_a: ["天性", "低", "高"], big5_n: ["身份特征", "低", "高"],
    att_anx: ["焦虑", "低", "高"], att_avo: ["回避", "低", "高"],
    pol_axis: ["政治倾向", "进步", "保守"], pol_ext: ["立场强度", "温和", "极端"],
    pol_econ: ["经济立场", "左派", "右派"], pol_lib: ["自由—威权", "自由", "威权"],
    val_change: ["求变—守成", "求变", "守成"], val_self: ["利己—利他", "利己", "利他"],
    moral_care: ["关爱", "轻视", "重视"], moral_fair: ["公平", "轻视", "重视"], moral_loyal: ["忠诚", "轻视", "重视"],
    moral_auth: ["权威", "轻视", "重视"], moral_sanct: ["圣洁", "轻视", "重视"], moral_liberty: ["自由", "轻视", "重视"],
    // 三观时间线 scores the pooled 道德基础 axis directly, so it has a key of its own.
    moral_bind: ["道德基础", "个人", "集体"],
    // 知己分析 extension (2026-10-27): the friend-only two-ended dimensions. The type dimensions
    // (humour, conflict) carry a fixed vocabulary instead of poles and live in TYPE_VOCAB.
    cog_need: ["认知需求", "低", "高"], cog_close: ["认知闭合", "低", "高"], locus: ["控制点", "内控", "外控"],
    optimism: ["气质性乐观", "悲观", "乐观"], time_focus: ["时间焦点", "当下", "未来"], time_past: ["过去态度", "消极", "积极"],
    self_constr: ["自我建构", "独立", "互依"], dialect: ["辩证思维", "低", "高"], direct: ["沟通直接性", "高语境", "低语境"],
    self_monitor: ["自我监控", "低", "高"], reject_sens: ["拒绝敏感性", "低", "高"], moral_scope: ["道德关怀范围", "窄", "宽"],
    val_enh: ["人生目标", "自我提升", "自我超越"],
  };
  const TYPE_VOCAB = { humor: ["亲和型", "自强型", "攻击型", "自嘲型"], conflict: ["竞争", "合作", "妥协", "回避", "迁就"] };
  // The diverging humour bars grow from the midpoint: the adaptive styles to the right, the others to the left.
  const DIVERGE_RIGHT = new Set(["亲和型", "自强型"]);
  // The Big Five drawn in MBTI order (16Personalities' five scales, McCrae & Costa 1989): each scale names its
  // letters at both ends and the share toward the nearer end. sign -1: a high Big Five score is the left letter.
  const MBTI = [
    { key: "big5_e", big5: "外向性", left: ["E", "外向"], right: ["I", "内向"], sign: -1 },
    { key: "big5_o", big5: "开放性", left: ["N", "直觉"], right: ["S", "实感"], sign: -1 },
    { key: "big5_a", big5: "宜人性", left: ["T", "理性"], right: ["F", "情感"], sign: 1 },
    { key: "big5_c", big5: "尽责性", left: ["J", "计划"], right: ["P", "随性"], sign: -1 },
    { key: "big5_n", big5: "情绪性", left: ["A", "自信"], right: ["T", "易波动"], sign: 1 },
  ];
  const CARDS = [
    { id: "big5", tab: "personality", title: "MBTI", kind: "mbti", dims: MBTI.map(scale => scale.key) },
    // Bartholomew's four styles on the anxiety × avoidance plane, drawn with low avoidance on top so 安全型 sits top-left.
    // ends: left, right, top, bottom; quadrants: top-left, top-right, bottom-left, bottom-right.
    // `extra` rows are friend-only additions drawn beneath the chart (here 拒绝敏感性).
    { id: "attachment", tab: "personality", title: "依恋类型", kind: "plane", dims: ["att_anx", "att_avo"], flipY: true,
      ends: ["低焦虑", "高焦虑", "低回避", "高回避"], quadrants: ["安全型", "焦虑型", "回避型", "恐惧型"], tinted: true,
      extra: ["reject_sens"] },
    // The politics card draws the classic compass (经济左—右 × 自由—威权); the cultural stance and the
    // extremity stay extracted and are listed in the card's detail rows. Region names are tendencies,
    // not verdicts: a point near a corner merely leans toward that side's values. The plane draws the
    // second dimension's high end at the top (no flipY), so the top end is 威权 and the quadrants follow
    // the compass's own corners (fixed 2026-10-29: the labels had been swapped against the coordinates,
    // drawing a libertarian-leaning point onto the 权威 end).
    { id: "politics", tab: "values", title: "政治光谱", kind: "plane", dims: ["pol_econ", "pol_lib", "pol_axis", "pol_ext"],
      ends: ["左派", "右派", "威权", "自由"], tinted: true, center: "中间派",
      quadrants: ["社会主义", "保守主义", "社会民主主义", "保守自由主义"] },
    { id: "values", tab: "values", title: "价值观", kind: "plane", dims: ["val_change", "val_self"], tinted: true,
      quadrants: ["理想派", "守护派", "进取派", "务实派"], extra: ["val_enh"] },
    // One pooled axis instead of six bars: 个人 (关爱、公平、自由) vs 集体 (忠诚、权威、圣洁); the six
    // foundations stay listed in the card's detail with their own evidence. side +1: the high end is 集体.
    // The friend-only 道德关怀范围 (对谁适用, beside the 理由 axes) joins as a second row.
    { id: "moral", tab: "values", title: "道德基础", kind: "axis", friend: true, ends: ["个人", "集体"], whole: "moral_bind",
      dims: ["moral_care", "moral_fair", "moral_liberty", "moral_loyal", "moral_auth", "moral_sanct"],
      pooled: { moral_care: -1, moral_fair: -1, moral_liberty: -1, moral_loyal: 1, moral_auth: 1, moral_sanct: 1 },
      extra: ["moral_scope"] },
    // 知己分析 extension (2026-10-27). `friend: true` keeps these cards out of 自我剖析, which still
    // uses the original catalog. Slider rows reuse the MBTI row style without the letter columns.
    { id: "mind", tab: "personality", title: "认知与动机", kind: "rows", friend: true,
      dims: ["cog_need", "cog_close", "locus", "optimism"] },
    { id: "expression", tab: "personality", title: "表达与思维", kind: "rows", friend: true,
      dims: ["self_constr", "dialect", "direct", "self_monitor"] },
    { id: "time", tab: "personality", title: "时间观", kind: "rows", friend: true, dims: ["time_focus", "time_past"] },
    { id: "humor", tab: "personality", title: "幽默风格", kind: "diverge", friend: true, dims: ["humor"] },
    { id: "conflict", tab: "personality", title: "冲突处理", kind: "heat", friend: true, dims: ["conflict"] },
  ];
  // Where an explicit self-report sits on its plane: [x, y] in the dimensions' own direction.
  const ATTACHMENT_SELF = { "安全型": [-0.5, -0.5], "焦虑型": [0.5, -0.5], "痴迷型": [0.5, -0.5], "回避型": [-0.5, 0.5],
    "疏离型": [-0.5, 0.5], "恐惧型": [0.5, 0.5] };
  const POLITICS_SELF = { "进步": [-0.5, 0], "中间": [0, 0], "保守": [0.5, 0] };
  const STRENGTH = { "较有把握": "firm", "有一定依据": "some", "把握较低": "weak" };
  const ORDER = ["firm", "some", "weak"];

  function el(tag, className, text) {
    const node = root.document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function add(parent, ...children) {
    for (const child of children) parent.appendChild ? parent.appendChild(child) : parent.append(child);
    return parent;
  }
  function place(node, x, y) {
    if (!node.style) return;
    if (x !== null) node.style.left = (x + 1) * 50 + "%";
    if (y !== null) node.style.top = (1 - y) * 50 + "%";
  }
  const known = item => item && typeof item.position === "number" && STRENGTH[item.confidence];
  const label = key => DIMENSIONS[key][0];

  // -1 (left letter) … 1 (right letter), or null without evidence.
  const lean = (scale, dims) => known(dims[scale.key]) ? scale.sign * dims[scale.key].position : null;
  const letter = (scale, dims) => { const x = lean(scale, dims); return x ? (x < 0 ? scale.left : scale.right)[0] : "?"; };
  // "93% 外向": the share toward the nearer letter, as the scale and its detail row both show it.
  function share(scale, dims) {
    const x = lean(scale, dims);
    if (x === null) return null;
    return Math.round(50 + Math.abs(x) * 50) + "%" + (x ? " " + (x < 0 ? scale.left : scale.right)[1] : "");
  }
  function mbti(dims, self) {
    if (self) return { text: self, title: "本人自述" };
    const text = MBTI.slice(0, 4).map(scale => letter(scale, dims)).join("");
    // A partly known scale is not a type: "?N??" must not stand in for the self-report.
    if (text.includes("?")) return null;
    const identity = letter(MBTI[4], dims);
    return { text: identity === "?" ? text : text + "-" + identity, title: "由大五人格推算（McCrae & Costa 1989）" };
  }

  function dot(item, key) {
    const node = el("i", "kpc-dot " + STRENGTH[item.confidence]);
    node.setAttribute("title", `${label(key)} · ${item.confidence}`);
    return node;
  }
  function scales(dims) {
    const list = el("div", "kpc-rows kpc-mbti");
    for (const scale of MBTI) {
      const item = dims[scale.key], x = lean(scale, dims), row = el("div", "kpc-row");
      const track = el("span", x === null ? "kpc-track unknown" : "kpc-track");
      const side = x === null || !x ? null : x < 0 ? scale.left : scale.right;
      if (x !== null) {
        const mark = dot(item, scale.key);
        place(mark, x, null);
        add(track, mark);
      }
      const name = el("span", "kpc-label", label(scale.key));
      name.setAttribute("title", "大五人格 · " + scale.big5);
      add(list, add(row, name, el("span", side === scale.left ? "kpc-letter on" : "kpc-letter", scale.left[0]), track,
        el("span", side === scale.right ? "kpc-letter on" : "kpc-letter", scale.right[0]),
        el("span", x === null ? "kpc-value unknown" : "kpc-value", share(scale, dims) || "证据不足")));
    }
    return list;
  }
  // One pooled -1 (left end) … 1 (right end) axis from several dimensions' lean counts; each key's
  // `pooled` side (+1/-1) says whether its high end counts toward the right end. Confidence is the
  // weakest of the contributing dimensions, like a plane point showing the weaker of its two axes.
  // A score for the axis as a whole (`whole`) stands in for the pooled counts.
  function axis(card, dims) {
    const [left, right] = card.ends, row = el("div", "kpc-rows kpc-axis"), whole = dims[card.whole];
    let toward = 0, total = 0, strength = -1;
    for (const [key, side] of Object.entries(card.pooled)) {
      const item = dims[key];
      if (!known(item)) continue;
      strength = Math.max(strength, ORDER.indexOf(STRENGTH[item.confidence]));
      const counts = item.counts || {};
      toward += side * ((counts.high || 0) - (counts.low || 0));
      total += (counts.low || 0) + (counts.high || 0) + (counts.mixed || 0);
    }
    if (known(whole)) strength = ORDER.indexOf(STRENGTH[whole.confidence]);
    const x = known(whole) ? whole.position : total ? toward / (total + 2) : null;
    const track = el("span", x !== null ? "kpc-track" : "kpc-track unknown");
    if (x !== null) {
      const mark = el("i", "kpc-dot " + ORDER[strength]);
      mark.setAttribute("title", `${card.title} · ${x < 0 ? left : right}侧 · ${ORDER[strength]}；` +
        `${left}＝关爱、公平、自由；${right}＝忠诚、权威、圣洁`);
      place(mark, x, null);
      add(track, mark);
    }
    const text = x !== null ? Math.round(50 + Math.abs(x) * 50) + "% " + (x < 0 ? left : right) : null;
    return add(row, add(el("div", "kpc-row"), el("span", "kpc-label", left), track, el("span", "kpc-label", right),
      el("span", text ? "kpc-value" : "kpc-value unknown", text || "证据不足")));
  }
  // The extended friend dimensions reuse the MBTI row style without the letter columns: name, track, share.
  const SLIDERS = new Set(["cog_need", "cog_close", "locus", "optimism", "time_focus", "time_past",
    "self_constr", "dialect", "direct", "self_monitor", "reject_sens", "moral_scope", "val_enh"]);
  function poleShare(key, item) {
    const x = item && typeof item.position === "number" ? item.position : null;
    if (x === null) return null;
    return Math.round(50 + Math.abs(x) * 50) + "% " + (x < 0 ? DIMENSIONS[key][1] : DIMENSIONS[key][2]);
  }
  function slider(item, key) {
    const x = known(item) ? item.position : null, row = el("div", "kpc-row kpc-slide-row");
    const track = el("span", x === null ? "kpc-track unknown" : "kpc-track");
    if (x !== null) {
      const mark = dot(item, key);
      place(mark, x, null);
      add(track, mark);
    }
    const name = el("span", "kpc-label", label(key));
    name.setAttribute("title", `${label(key)}（${DIMENSIONS[key][1]}—${DIMENSIONS[key][2]}）`);
    return add(row, name, track, el("span", x === null ? "kpc-value unknown" : "kpc-value", poleShare(key, item) || "证据不足"));
  }
  function rowsBody(card, dims) {
    const body = el("div", "kpc-rows kpc-slide");
    for (const key of card.dims) body.append(slider(dims[key], key));
    return body;
  }
  // Humour styles as four bars growing from the midpoint: adaptive styles right, the others left.
  function diverge(card, dims) {
    const counts = (dims[card.dims[0]] || {}).types || {};
    const total = TYPE_VOCAB.humor.reduce((sum, type) => sum + (counts[type] || 0), 0);
    const body = el("div", "kpc-rows kpc-diverge");
    for (const type of TYPE_VOCAB.humor) {
      const count = counts[type] || 0, row = el("div", "kpc-row");
      const track = el("span", total ? "kpc-track kpc-diverge-track" : "kpc-track unknown");
      if (count) {
        const bar = el("i", "kpc-bar");
        bar.style[DIVERGE_RIGHT.has(type) ? "left" : "right"] = "50%";
        bar.style.width = count / total * 50 + "%";
        bar.setAttribute("title", `${type} · ${count} 条 · ${Math.round(count / total * 100)}%`);
        track.append(bar);
      }
      add(body, add(row, el("span", "kpc-label", type), track,
        el("span", total ? "kpc-value" : "kpc-value unknown", total ? Math.round(count / total * 100) + "%" : "证据不足")));
    }
    return body;
  }
  // Conflict handling as a five-cell heat strip; without any recorded conflict the card stays dashed.
  function heat(card, dims) {
    const counts = (dims[card.dims[0]] || {}).types || {};
    const total = TYPE_VOCAB.conflict.reduce((sum, type) => sum + (counts[type] || 0), 0);
    const box = el("div", "kpc-heat");
    if (!total) {
      box.className += " empty";
      box.append(el("span", "kpc-heat-empty", "暂无冲突情境"));
      return box;
    }
    const top = Math.max(...TYPE_VOCAB.conflict.map(type => counts[type] || 0));
    for (const type of TYPE_VOCAB.conflict) {
      const count = counts[type] || 0, cell = el("span", "kpc-heat-cell" + (count ? "" : " none") + (count === top ? " on" : ""));
      if (count) cell.style.flexGrow = String(count);
      cell.setAttribute("title", `${type} · ${count} 条 · ${Math.round(count / total * 100)}%`);
      cell.textContent = type;
      box.append(cell);
    }
    return box;
  }
  // The distribution line of a type dimension ("亲和型 5 · 自嘲型 2"), or null for pole dimensions.
  function typeText(key, item) {
    const vocab = TYPE_VOCAB[key];
    if (!vocab || !item) return null;
    const counts = item.types || {};
    const parts = vocab.filter(type => counts[type]).map(type => `${type} ${counts[type]}`);
    return parts.length ? parts.join(" · ") : null;
  }
  function cardSources(card, sources) {
    const seen = new Set(), list = [];
    for (const key of [...card.dims, ...(card.extra || [])])
      for (const cite of sources[key] || [])
        if (!seen.has(cite)) { seen.add(cite); list.push(cite); }
    return list;
  }
  function plane(card, dims, self) {
    const [xKey, yKey] = card.dims, box = el("div", card.tinted ? "kpc-plane tinted" : "kpc-plane");
    // flipY draws the second dimension's high end at the bottom.
    const at = (node, px, py) => place(node, px, card.flipY ? -py : py);
    if (card.quadrants) for (const [index, name] of card.quadrants.entries())
      add(box, el("span", "kpc-quadrant q" + index, name));
    if (card.center) add(box, el("span", "kpc-plane-center", card.center));
    const [left, right, top, bottom] = card.ends || [DIMENSIONS[xKey][1], DIMENSIONS[xKey][2], DIMENSIONS[yKey][2], DIMENSIONS[yKey][1]];
    const frame = add(el("div", "kpc-planebox"), el("span", "kpc-end top", top), el("span", "kpc-end left", left), box,
      el("span", "kpc-end right", right), el("span", "kpc-end bottom", bottom));
    const x = dims[xKey], y = dims[yKey];
    if (known(x) && known(y)) {
      const strength = ORDER[Math.max(...[x, y].map(item => ORDER.indexOf(STRENGTH[item.confidence])))];
      const mark = el("i", "kpc-dot " + strength);
      mark.setAttribute("title", [xKey, yKey].map(key => `${label(key)} · ${known(dims[key]) ? dims[key].confidence : "证据不足"}`).join("；"));
      at(mark, x.position, y.position);
      add(box, mark);
    } else {
      box.className += " unknown";
      const available = known(x) ? xKey : known(y) ? yKey : null;
      const missing = available === xKey ? yKey : xKey;
      add(frame, el("span", "kpc-plane-status", available
        ? `仅${label(available)}轴有依据；${label(missing)}轴证据不足`
        : "两轴证据不足，暂无二维结论"));
    }
    if (self) {
      const mark = el("i", "kpc-self");
      mark.setAttribute("title", "本人自述：" + self.value);
      at(mark, self.at[0], self.at[1]);
      add(box, mark);
    }
    return frame;
  }
  function selfMark(card, reports) {
    if (card.id === "attachment" && reports.attachment)
      return { value: reports.attachment, at: ATTACHMENT_SELF[reports.attachment] || [0.5, 0.5] };
    if (card.id === "politics" && POLITICS_SELF[reports.politics])
      return { value: reports.politics, at: POLITICS_SELF[reports.politics] };
    return null;
  }

  // data: { tab, dims: { key: { position, confidence } }, selfReports: { mbti, attachment, politics }, selected,
  //   friend (draws the 知己-only cards and extra rows), sources: { key: [citation] } for the title hover,
  //   type: { text, title } when the caller already knows the letters the MBTI card should name }
  function render(container, data, onSelect) {
    const dims = data.dims || {}, reports = data.selfReports || {}, grid = el("div", "kpc-grid");
    const friend = !!data.friend;
    for (const card of CARDS.filter(item => item.tab === data.tab && (!item.friend || friend))) {
      const node = el("button", "kpc-card");
      node.type = "button";
      node.setAttribute("aria-pressed", String(data.selected === card.id));
      node.addEventListener("click", () => onSelect(card.id));
      const title = el("span", "kpc-title", card.title);
      if (data.sources) {
        const cites = cardSources(card, data.sources);
        if (cites.length) title.setAttribute("title", cites.join("；"));
      }
      const head = add(el("span", "kpc-head"), title);
      if (card.id === "big5") {
        // The type follows the scales drawn below it; a self-report differing from them is listed in the details.
        const type = data.type || mbti(dims, null) || mbti(dims, reports.mbti);
        if (type) {
          const aside = el("span", "kpc-aside", type.text);
          aside.setAttribute("title", type.title);
          add(head, aside);
        }
      }
      if ((card.kind === "diverge" || card.kind === "heat") && dims[card.dims[0]]?.label) {
        // The dominant type is decided by the local rule; the distribution itself is always drawn.
        const aside = el("span", "kpc-aside", dims[card.dims[0]].label);
        aside.setAttribute("title", dims[card.dims[0]].confidence || "");
        add(head, aside);
      }
      let body;
      if (card.kind === "mbti") body = scales(dims);
      else if (card.kind === "rows") body = rowsBody(card, dims);
      else if (card.kind === "diverge") body = diverge(card, dims);
      else if (card.kind === "heat") body = heat(card, dims);
      else if (card.kind === "axis") {
        body = axis(card, dims);
        if (friend) for (const key of card.extra || []) body.append(slider(dims[key], key));
      } else {
        const extras = friend ? card.extra || [] : [];
        if (extras.length) {
          body = el("div", "kpc-rows kpc-slide");
          add(body, plane(card, dims, selfMark(card, reports)));
          for (const key of extras) body.append(slider(dims[key], key));
        } else body = plane(card, dims, selfMark(card, reports));
      }
      add(node, head, body);
      add(grid, node);
    }
    container.replaceChildren(grid);
  }

  // 性格时间线 (added 2026-11-01): five small plots on one time axis, each scored 1–5 around a midline of 3.
  // The line is the score and the band its lowest to highest repeat; a period without a score breaks both.
  // Each scale keeps one of the eight tones in persona-charts.css (2026-11-01).
  const TONES = 8;
  const SVG_NS = "http://www.w3.org/2000/svg";
  function shape(tag, attributes) {
    const node = root.document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    node.setAttribute("vector-effect", "non-scaling-stroke");
    return node;
  }
  const plotX = (index, count) => (index + .5) / count * 100;
  const plotY = value => (5 - Math.min(5, Math.max(1, value))) / 4 * 100;
  function stretches(periods, ready) {
    const groups = [];
    let group = null;
    periods.forEach((period, index) => {
      if (!ready(period)) { group = null; return; }
      if (!group) groups.push(group = []);
      group.push(index);
    });
    return groups;
  }
  const score = (period, key) => period.factors?.[key] || {};
  const scoreText = value => typeof value.score === "number" ? value.score.toFixed(1) : "—";
  const rangeText = value => typeof value.low === "number" ? `${value.low.toFixed(1)}–${value.high.toFixed(1)}` : "";
  function periodPlot(periods, key, selected, tone) {
    const count = periods.length, plot = el("div", `kpc-p-plot kpc-tone-${tone}`);
    const chart = shape("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" });
    chart.append(shape("line", { class: "kpc-p-mid", x1: 0, x2: 100, y1: 50, y2: 50 }));
    for (const group of stretches(periods, period => typeof score(period, key).low === "number")) {
      if (group.length < 2) {
        // A lone period has no neighbour to form a band with, so its range is a short bar.
        const x = plotX(group[0], count), value = score(periods[group[0]], key);
        chart.append(shape("line", { class: "kpc-p-whisker", x1: x, x2: x, y1: plotY(value.high), y2: plotY(value.low) }));
        continue;
      }
      const top = group.map(index => `${plotX(index, count)},${plotY(score(periods[index], key).high)}`);
      const bottom = group.map(index => `${plotX(index, count)},${plotY(score(periods[index], key).low)}`).reverse();
      chart.append(shape("polygon", { class: "kpc-p-band", points: [...top, ...bottom].join(" ") }));
    }
    for (const group of stretches(periods, period => typeof score(period, key).score === "number")) {
      if (group.length > 1) chart.append(shape("polyline", { class: "kpc-p-line",
        points: group.map(index => `${plotX(index, count)},${plotY(score(periods[index], key).score)}`).join(" ") }));
    }
    plot.append(chart);
    periods.forEach((period, index) => {
      const value = score(period, key);
      if (typeof value.score !== "number") return;
      const mark = el("i", period.id === selected ? "kpc-p-dot on" : "kpc-p-dot");
      mark.style.left = plotX(index, count) + "%";
      mark.style.top = plotY(value.score) + "%";
      plot.append(mark);
    });
    return plot;
  }
  // Equal values share a tone across strips, spaced or not, so the same name keeps its color everywhere.
  // Past the eighth value a segment stays neutral rather than repeat a tone; its text still names it.
  function stripTone(tones, value) {
    const key = value.replace(/\s/g, "");
    if (!tones.seen.has(key)) tones.seen.set(key, tones.seen.size);
    const order = tones.seen.get(key);
    return order < TONES ? ` kpc-toned kpc-tone-${(tones.first + order) % TONES}` : "";
  }
  // Runs of equal text become one segment, so a long stretch of one type reads as one bar. A text
  // that is an array of names — several partners of one period — renders one chip per name, stacked;
  // each chip takes its own tone, so the same person keeps their color in every period. With
  // edit: { edited(period), ai(period), open(periodId) } every period opens, blank ones too, and
  // a click inside a run opens the period under the pointer. What the user changed never merges with the model's.
  function periodStrip(periods, text, name = "kpc-p-strip", tones = null, edit = null) {
    const strip = el("div", name), count = periods.length;
    strip.style.gridTemplateColumns = `repeat(${count}, minmax(0, 1fr))`;
    const mine = period => Boolean(edit?.edited(period)), key = value => Array.isArray(value) ? value.filter(Boolean).join("\n") : value;
    let stacked = false, start = 0;
    while (start < count) {
      const raw = text(periods[start]), value = key(raw), edited = mine(periods[start]), first = start;
      let end = start + 1;
      while (end < count && key(text(periods[end])) === value && mine(periods[end]) === edited) end++;
      if (value || edit) {
        const span = end - first, names = Array.isArray(raw) ? raw.filter(Boolean) : null;
        const classes = "kpc-p-seg" + (value ? "" : " empty") + (names?.length ? " stack" : value && tones ? stripTone(tones, value) : "") + (edited ? " edited" : "");
        const segment = el(edit ? "button" : "span", classes, names?.length ? "" : value), range = `${periods[first].label}${span > 1 ? " 至 " + periods[end - 1].label : ""}`;
        segment.style.gridColumn = `${first + 1} / ${end + 1}`;
        const record = edited ? `\n你改过，AI 判断：${edit.ai(periods[first]) || "无"}` : "";
        segment.setAttribute("title", `${range}：${names?.length ? names.join("、") : value || "无"}${record}`);
        if (names?.length) {
          stacked = true;
          for (const item of names) segment.append(el("span", `kpc-p-chip${tones ? stripTone(tones, item) : ""}`, item));
        }
        if (edit) {
          segment.type = "button";
          segment.addEventListener("click", event => {
            const offset = Math.floor(event.offsetX / segment.clientWidth * span) || 0;
            edit.open(periods[first + Math.min(span - 1, Math.max(0, offset))].id);
          });
        }
        strip.append(segment);
      }
      start = end;
    }
    if (stacked) strip.className += " stacked";
    return strip;
  }
  function periodTip(period, scales) {
    const rows = scales.map(scale => {
      const value = score(period, scale.key), range = rangeText(value);
      return `${label(scale.key)} ${scoreText(value)}${range ? `（${range}）` : ""}`;
    });
    return [period.label, ...rows, (period.letters || []).join("")].join("\n");
  }
  // The reading note opens from a ? in the corner, stays open through a redraw and closes on any click outside it.
  let helpWatched = false;
  function closeHelp(keep) {
    for (const node of root.document.querySelectorAll(".kpc-help[open]")) if (node !== keep) node.open = false;
  }
  function periodHelp(lines, title, open) {
    if (!helpWatched) {
      helpWatched = true;
      root.addEventListener("click", event => closeHelp(event.target.closest?.(".kpc-help")));
      root.addEventListener("keydown", event => { if (event.key === "Escape") closeHelp(null); });
    }
    const help = el("details", "kpc-help"), mark = el("summary", "", "?"), note = el("div", "kpc-help-note");
    mark.setAttribute("aria-label", `怎么读${title}`);
    mark.setAttribute("title", `怎么读${title}`);
    for (const line of lines) note.append(el("p", "", line));
    help.open = open;
    return add(help, mark, note);
  }
  // data: { periods: [{ id, label, factors, letters, report, ai, edited }], selected, detail, help: [line] }; a timeline of
  // other scales adds title, instrument, scales: [{ key, tone }] and strips: [[name, period => text, field, edited?]].
  // handlers: onSelect(periodId), onDetail(dimensionKey), onEdit(field, periodId). The caller owns the selection.
  function periods(container, data, handlers) {
    const list = data.periods || [], count = list.length, box = el("section", "kpc-periods"), scales = data.scales || MBTI;
    const passes = Math.max(...list.map(period => period.passes || 1)), items = Math.max(...list.map(period => period.items || 0));
    // The self-reported 你说过 row went away on 2026-11-01: the user corrects values in place, and the
    // self-report still shows on the MBTI card. A timeline that wants more rows passes its own strips.
    const strips = data.strips || [["读聊天", period => (period.letters || []).join(""), "type"]];
    const edited = (period, field) => (period.edited || []).includes(field);
    const title = data.title || "性格时间线", tone = (scale, row) => scale.tone ?? row;
    const head = add(el("div", "kpc-head"), el("span", "kpc-title", title),
      el("span", "kpc-aside", `${data.instrument || (items > 20 ? "IPIP-50" : "Mini-IPIP")} · 每期 ${passes} 次调研`));
    if (data.help) add(head, periodHelp(data.help, title, Boolean(container.querySelector(".kpc-help[open]"))));
    const body = el("div", "kpc-p-body"), chosen = list.find(period => period.id === data.selected) || list[count - 1];
    scales.forEach((scale, row) => {
      const name = el("button", `kpc-p-name kpc-toned kpc-tone-${tone(scale, row)}`, label(scale.key));
      name.type = "button";
      name.setAttribute("title", scale.big5 ? `大五人格 · ${scale.big5}（查看每道题）` : "查看每道题");
      name.setAttribute("aria-pressed", String(data.detail === scale.key));
      name.addEventListener("click", () => handlers.onDetail(scale.key));
      const value = score(chosen, scale.key), field = "score:" + scale.key, mine = edited(chosen, field), range = rangeText(value);
      const shown = el("button", (typeof value.score === "number" ? "kpc-p-value" : "kpc-p-value unknown") + (mine ? " edited" : ""), scoreText(value));
      shown.type = "button";
      shown.setAttribute("title", [range ? `${passes} 次调研 ${range}` : "", mine ? `你改过，AI 判断：${scoreText({ score: chosen.ai?.[field] })}` : "修改这个分数"].filter(Boolean).join("\n"));
      shown.addEventListener("click", () => handlers.onEdit(field, chosen.id));
      const plot = periodPlot(list, scale.key, chosen.id, tone(scale, row));
      plot.style.gridRow = String(row + 1);
      add(body, name, plot, shown);
    });
    const columns = el("div", "kpc-p-cols");
    columns.style.gridTemplateColumns = `repeat(${count}, minmax(0, 1fr))`;
    columns.style.gridRow = `1 / span ${scales.length}`;
    for (const period of list) {
      const column = el("button", period.id === chosen.id ? "kpc-p-col on" : "kpc-p-col");
      column.type = "button";
      column.setAttribute("title", periodTip(period, scales));
      column.setAttribute("aria-label", period.label);
      column.addEventListener("click", () => handlers.onSelect(period.id));
      columns.append(column);
    }
    const years = periodStrip(list, period => period.id.slice(0, period.id.includes("-") ? 9 : 4), count > 12 ? "kpc-p-strip kpc-p-years long" : "kpc-p-strip kpc-p-years");
    add(body, columns, el("span", ""), years, el("span", ""));
    // Strip values start on the tones the scales left free, so a value does not read as one of the scales.
    const tones = { seen: new Map(), first: Math.max(...scales.map(tone)) + 1 };
    for (const [name, text, field, mine] of strips) add(body, el("span", "kpc-p-name plain", name), periodStrip(list, text, undefined, tones,
      { edited: mine || (period => edited(period, field)), ai: period => period.ai?.[field], open: id => handlers.onEdit(field, id) }), el("span", ""));
    container.replaceChildren(add(box, head, body));
  }

  root.KyfPersonaCharts = {
    DIMENSIONS, TYPE_VOCAB, CARDS, MBTI, render, mbti, typeText, poleShare, periods,
    cards: tab => CARDS.filter(card => card.tab === tab),
    cardOf: key => (CARDS.find(card => card.dims.includes(key) || (card.extra || []).includes(key)) || {}).id || null,
    tabOf: key => (CARDS.find(card => card.dims.includes(key) || (card.extra || []).includes(key)) || {}).tab || null,
    label, ends: key => DIMENSIONS[key].slice(1),
    share: (key, item) => {
      const scale = MBTI.find(entry => entry.key === key);
      if (scale) return share(scale, { [key]: item });
      return SLIDERS.has(key) ? poleShare(key, item) : null;
    },
  };
})(typeof window !== "undefined" ? window : globalThis);

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 关系趋势 arithmetic: buckets the bridge's per-message facts into local-time periods.
// Pure functions only, so the same code runs in the window and in Node tests.
(function (root) {
  const UNITS = ["minute", "hour", "day", "week", "month", "year"];
  const METRICS = ["messages", "turns", "chars", "reply"];
  const TURN = 1, START = 2, MIN_REPLY_SAMPLES = 3;
  // Reply speed bands: within a minute, within 10 minutes, within an hour, longer (up to 8 hours).
  const REPLY_BANDS = [60, 600, 3600];
  const { start, next, lower, days, axisLabel, title, labelRule } = root.KyfTimeTrend.calendar;

  function decode(data) {
    const columns = data.messages, count = columns.time.length;
    const time = new Float64Array(count);
    let total = 0;
    for (let index = 0; index < count; index++) time[index] = total += columns.time[index];
    const replyIndex = new Int32Array(data.replies.length), replySeconds = new Float64Array(data.replies.length);
    data.replies.forEach(([index, seconds], position) => { replyIndex[position] = index; replySeconds[position] = seconds; });
    return { time, side: columns.side, chars: columns.chars, kind: columns.kind, flags: columns.flags,
      replyIndex, replySeconds, kinds: data.kinds, count };
  }

  const tenth = value => Math.round(value * 10) / 10;
  function median(values) {
    if (values.length < MIN_REPLY_SAMPLES) return null;
    const sorted = [...values].sort((left, right) => left - right), middle = sorted.length >> 1;
    return tenth(sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2);
  }
  function mean(values) {
    if (values.length < MIN_REPLY_SAMPLES) return null;
    let total = 0;
    for (const value of values) total += value;
    return tenth(total / values.length);
  }

  // Every period of ``unit`` from the one holding fromMs up to the in-progress one (or toMs).
  // ``bounds`` holds each period start plus the last period's end. ``reply`` is each side's mean
  // wait, NaN below three replies: the mean, unlike the median, shows who keeps the other waiting.
  function series(facts, unit, { fromMs, toMs = Infinity, nowMs }) {
    const starts = [];
    for (let at = start(fromMs, unit); at < toMs && at <= nowMs; at = next(at, unit)) starts.push(at);
    if (!starts.length) starts.push(start(Math.min(fromMs, nowMs), unit));
    const count = starts.length, bounds = new Float64Array(count + 1);
    bounds.set(starts);
    bounds[count] = next(starts[count - 1], unit);
    const make = () => [new Float64Array(count), new Float64Array(count)];
    const values = { messages: make(), turns: make(), chars: make(), reply: make() };
    const replies = make();
    let period = 0;
    const first = lower(facts.time, bounds[0]), last = lower(facts.time, bounds[count]);
    for (let index = first; index < last; index++) {
      const at = facts.time[index];
      while (at >= bounds[period + 1]) period++;
      const side = facts.side[index];
      values.messages[side][period]++;
      if (facts.flags[index] & TURN) values.turns[side][period]++;
      values.chars[side][period] += facts.chars[index];
    }
    period = 0;
    for (let position = lower(facts.replyIndex, first); position < facts.replyIndex.length; position++) {
      const index = facts.replyIndex[position];
      if (index >= last) break;
      while (facts.time[index] >= bounds[period + 1]) period++;
      const side = facts.side[index];
      values.reply[side][period] += facts.replySeconds[position];
      replies[side][period]++;
    }
    for (const side of [0, 1])
      for (let index = 0; index < count; index++)
        values.reply[side][index] = replies[side][index] >= MIN_REPLY_SAMPLES ?
          tenth(values.reply[side][index] / replies[side][index]) : NaN;
    return { unit, count, bounds, values, inProgress: bounds[count] > nowMs };
  }

  // Everything the cards show for [startMs, endMs): one period or a selected range of periods.
  // Contact days are reported whenever the span reaches past its first day.
  function summary(facts, startMs, endMs) {
    const first = lower(facts.time, startMs), last = lower(facts.time, endMs);
    const pair = () => [0, 0];
    const result = { messages: pair(), turns: pair(), chars: pair(), starts: pair(),
      kinds: [facts.kinds.map(() => 0), facts.kinds.map(() => 0)], replySamples: pair(),
      replyMeanSeconds: [null, null], replyMedianSeconds: [null, null], replyLongestSeconds: [null, null],
      replyBands: [REPLY_BANDS.map(() => 0).concat(0), REPLY_BANDS.map(() => 0).concat(0)],
      contactDays: null, twoWayDays: null };
    let day = null, mask = 0, contact = 0, twoWay = 0;
    const closeDay = () => { if (mask) { contact++; if (mask === 3) twoWay++; } };
    for (let index = first; index < last; index++) {
      const side = facts.side[index];
      result.messages[side]++;
      if (facts.flags[index] & TURN) result.turns[side]++;
      if (facts.flags[index] & START) result.starts[side]++;
      result.chars[side] += facts.chars[index];
      result.kinds[side][facts.kind[index]]++;
      const today = start(facts.time[index], "day");
      if (today !== day) { closeDay(); day = today; mask = 0; }
      mask |= side ? 2 : 1;
    }
    closeDay();
    if (endMs > next(start(startMs, "day"), "day")) Object.assign(result, { contactDays: contact, twoWayDays: twoWay });
    const lists = [[], []];
    for (let position = lower(facts.replyIndex, first); position < facts.replyIndex.length && facts.replyIndex[position] < last; position++)
      lists[facts.side[facts.replyIndex[position]]].push(facts.replySeconds[position]);
    for (const side of [0, 1]) {
      result.replySamples[side] = lists[side].length;
      result.replyMeanSeconds[side] = mean(lists[side]);
      result.replyMedianSeconds[side] = median(lists[side]);
      for (const value of lists[side]) {
        result.replyLongestSeconds[side] = Math.max(result.replyLongestSeconds[side] ?? 0, value);
        const band = REPLY_BANDS.findIndex(limit => value < limit);
        result.replyBands[side][band < 0 ? REPLY_BANDS.length : band]++;
      }
    }
    return result;
  }

  // Default selection: the newest period that has messages, else the newest period.
  function latestWithMessages(view) {
    for (let index = view.count - 1; index >= 0; index--)
      if (view.values.messages[0][index] + view.values.messages[1][index] > 0) return index;
    return view.count - 1;
  }
  function firstWithMessages(view, from = 0, to = view.count) {
    for (let index = from; index < to; index++)
      if (view.values.messages[0][index] + view.values.messages[1][index] > 0) return index;
    return from;
  }

  root.FriendTrendMath = { UNITS, METRICS, TURN, START, REPLY_BANDS, decode, start, next, lower, median, mean, days, series, summary,
    latestWithMessages, firstWithMessages, axisLabel, title, labelRule };
})(typeof window !== "undefined" ? window : globalThis);

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

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 好友概述: local facts, the recent-conversation summary and next step, and pointers to the other pages.
(function () {
  const A = window.FriendApp;
  const O = { data: null, request: 0 };

  function ago(ms) {
    if (!Number.isFinite(ms)) return "—";
    const days = Math.floor((Date.now() - ms) / 86400000);
    return days <= 0 ? "今天" : days === 1 ? "昨天" : days < 30 ? `${days} 天前` : A.formatDate(ms);
  }
  function range(from, to) {
    const left = A.formatDate(from), right = A.formatDate(to);
    return left === right ? left : `${left} 至 ${right}`;
  }

  async function load() {
    const request = ++O.request;
    try {
      const data = await A.rpc("page.overview");
      if (request !== O.request) return;
      O.data = data;
      render();
    } catch (error) {
      if (request === O.request && !A.masked)
        A.byId("pageOverview").replaceChildren(A.el("div", "fa-empty", error.message || "概述读取失败"));
    }
  }

  function section(title, content, aside) {
    const node = A.el("section", "fa-card");
    const head = A.el("div", "fa-card-head");
    head.append(A.el("h3", "", title));
    if (aside) head.append(A.el("span", "fa-spacer"), aside);
    node.append(head, content);
    return node;
  }
  function render() {
    const root = A.byId("pageOverview");
    if (A.masked || !O.data) return;
    const data = O.data, facts = data.facts;
    root.replaceChildren();
    const reading = facts.historyState !== "ready";
    const line = [facts.firstMs ? "可读记录最早 " + A.formatDate(facts.firstMs) : null,
      `共 ${facts.messages} 条消息${reading ? "（统计中）" : ""}`,
      facts.lastMs ? "最近联系 " + ago(facts.lastMs) : null,
      "联系目标：" + facts.goal + (facts.goalIsDefault ? "（默认）" : "")].filter(Boolean).join(" · ");
    root.append(A.el("p", "fa-facts", line));

    if (data.summary) {
      const body = A.el("div");
      body.append(A.el("p", "fa-hero-text", data.summary.summary));
      const meta = A.el("div", "fa-item-meta");
      meta.append(A.el("span", "", `依据 ${range(data.summary.fromMs, data.summary.toMs)} 的 ${data.summary.messages} 条消息` +
        (data.summary.complete ? "" : "（较长，只含最近部分）")));
      if (data.stale) meta.append(A.el("span", "fa-mark warn", "有新消息，下次分析时更新"));
      body.append(meta);
      root.append(section("最近聊到了这里", body,
        A.button("查看原文", "fa-plain", () => showEvidence(data.summary.evidence))));
    } else root.append(section("最近聊到了这里", A.el("p", "fa-note", "开始分析后显示。")));

    root.append(section("下一步", data.next ? A.el("p", "fa-hero-text", data.next.next) :
      A.el("p", "fa-note", "开始分析后显示。")));

    if (data.about.length) {
      const list = A.el("div", "fa-list");
      for (const item of data.about) {
        const row = A.button("", "fa-row-link", () => A.selectTab(item.kind === "persona" ? "persona" : "memories"));
        row.append(A.el("span", "fa-row-label", item.label || ""), A.el("span", "fa-row-text", item.text));
        if (item.confidence) row.append(A.el("span", "fa-mark", item.confidence + " · 推测"));
        list.append(row);
      }
      root.append(section("关于 TA", list));
    } else root.append(section("关于 TA", A.el("p", "fa-note", "开始分析后显示。")));

    if (data.remember.length) {
      const list = A.el("div", "fa-list");
      for (const item of data.remember) {
        const row = A.el("div", "fa-item");
        row.append(A.el("div", "fa-item-text", item.text));
        list.append(row);
      }
      root.append(section("值得记住", list, A.button("全部", "fa-plain", () => A.selectTab("memories"))));
    }
    if (data.change) {
      const difference = data.change.days - data.change.previousDays;
      const trend = difference === 0 ? "与前一个月相同" : `比前一个月${difference > 0 ? "多" : "少"} ${Math.abs(difference)} 天`;
      root.append(section("关系变化", A.el("p", "fa-hero-text", `${data.change.label} 联系 ${data.change.days} 天，${trend}。`)));
    }
    if (data.summary) {
      const source = [`依据 ${range(data.summary.fromMs, data.summary.toMs)} 的对话`];
      if (data.summary.model) source.push(data.summary.model);
      source.push(A.formatDate(data.summary.createdAtMs, true));
      root.append(A.el("p", "fa-note", source.join(" · ")));
    }
  }

  function showEvidence(items) {
    const body = A.openSheet("摘要依据的原文", "overview-evidence");
    const list = A.el("div");
    for (const item of items || []) {
      const row = A.el("div", "fa-msg");
      row.append(A.el("div", "fa-msg-head", `${item.speaker} · ${A.formatDate(item.timeMs, true)}`),
        A.el("div", "fa-msg-text", item.text));
      const more = A.button("看上下文", "fa-plain", () => showContext(item.msgId, row, more));
      row.append(more);
      list.append(row);
    }
    body.append(list, A.el("p", "fa-note", "摘要是对这些原文的概括，不代表对方的内心。"));
  }
  async function showContext(msgId, container, trigger) {
    trigger.disabled = true;
    try {
      const data = await A.host("message.context", { id: msgId });
      container.append(A.contextList(data));
      trigger.remove();
    } catch (error) { trigger.disabled = false; A.toast(error.message || "读取失败"); }
  }

  window.FriendOverview = { load };
})();

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 性格分析 and 三观分析 (split from 人格分析, 2026-10-25): the shared charts on top; choosing a chart opens its
// dimensions below with confidence, evidence and 我的观察. AI results and the user's own observations are
// separate records; neither overwrites the other.
(function () {
  const A = window.FriendApp, C = window.KyfPersonaCharts;
  const VALUE_ITEMS = [["love", "感情观"], ["family", "家庭观"], ["money", "金钱观"],
    ["plan", "人生规划"], ["core", "价值底色"]];
  const VALUE_KEYS = new Set(VALUE_ITEMS.map(([id]) => id));
  const shared = { data: null, request: 0, pages: [] };

  async function fetchData() {
    const request = ++shared.request;
    const data = await A.rpc("page.persona");
    if (request === shared.request) shared.data = data;
    return request === shared.request;
  }

  function make(tab, pageId) {
    const P = { open: new Set(), observing: null, selected: null };
    const root = () => A.byId(pageId);
    const owns = dimension => VALUE_KEYS.has(dimension) ? tab === "values" :
      dimension === "mbti" ? tab === "personality" : C.tabOf(dimension) === tab;

    async function load() {
      try {
        if (await fetchData()) render();
      } catch (error) {
        if (!A.masked) root().replaceChildren(A.el("div", "fa-empty", error.message || "读取失败"));
      }
    }

    // The charts' names (能量, 焦虑) rather than the analysis catalog's (外向性, 依恋焦虑).
    function label(id) { return C.DIMENSIONS[id] ? C.label(id) : shared.data.labels[id] ||
      VALUE_ITEMS.find(([key]) => key === id)?.[1] || id; }
    function observationsFor(id) { return shared.data.observations.filter(item => item.dimension === id); }
    function newObservationFor(id) { return P.observing && !P.observing.item && P.observing.dimension === id; }
    function result() { return shared.data.result; }
    function dimension(id) { return result().dimensions.find(item => item.id === id); }

    function render() {
      const page = root();
      if (A.masked || !shared.data) return;
      const focusKey = page.contains(document.activeElement) ? document.activeElement?.dataset?.key : null;
      page.replaceChildren();
      if (result()) {
        if (shared.data.stale) page.append(A.el("p", "fa-facts", "有新证据，下次分析时更新"));
        const charts = A.el("div", "fa-charts");
        C.render(charts, { tab, selected: P.selected, friend: true, sources: shared.data.sources,
          dims: Object.fromEntries(result().dimensions.map(item => [item.id, item])),
          selfReports: Object.fromEntries(Object.entries(result().selfReports).map(([key, value]) => [key, value?.value || null])) },
        id => { P.selected = P.selected === id ? null : id; render(); });
        page.append(charts);
        const drift = driftBlock(result());
        if (drift) page.append(drift);
        if (P.selected) page.append(cardDetail(C.CARDS.find(card => card.id === P.selected)));
        if (tab === "values") {
          if (!Array.isArray(result().values))
            page.append(A.el("p", "fa-note", "这份旧版结果尚未包含感情观等文字分析，下次分析后更新。"));
          page.append(valuesBlock());
        }
        page.append(detailsBlock(result()));
        const sources = sourcesBlock();
        if (sources) page.append(sources);
      } else {
        page.append(A.el("div", "fa-empty", shared.data.stale ? "旧版分析待更新，重新分析后显示" : "开始分析后显示"));
        if (tab === "values") page.append(valuesBlock());
      }
      if (tab === "personality") page.append(otherObservations());
      if (focusKey) page.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
    }

    function cardDetail(card) {
      const list = A.el("div", "fa-list fa-chart-detail");
      const reports = result().selfReports;
      const ids = [...card.dims, ...(card.extra || [])].map(dimension).filter(Boolean);
      // 把握较低 folds into a separate 参考 group instead of mixing with the firmer rows.
      for (const item of ids.filter(item => item.tier !== "reference")) list.append(dimensionRow(item));
      const reference = ids.filter(item => item.tier === "reference");
      if (reference.length) {
        const details = A.el("details", "fa-details");
        details.append(A.el("summary", "", `参考 · 把握较低（${reference.length}）`));
        for (const item of reference) details.append(dimensionRow(item));
        list.append(details);
      }
      if (card.id === "big5") list.append(reportRow("mbti", reports.mbti));
      if (card.id === "attachment" && reports.attachment) list.append(reportRow(null, reports.attachment, "依恋类型自述"));
      if (card.id === "politics" && reports.politics) list.append(reportRow(null, reports.politics, "政治自我定位"));
      return list;
    }

    function head(id, text, confidence) {
      const main = A.el("div", "fa-item-main");
      const body = A.el("div", "fa-item-text");
      const top = A.el("div", "fa-persona-head");
      top.append(A.el("span", "fa-row-label", label(id)), A.el("span", "fa-row-text", text));
      if (confidence) top.append(A.el("span", "fa-mark", confidence));
      body.append(top);
      for (const item of observationsFor(id)) body.append(observationLine(item));
      const actions = A.el("div", "fa-item-actions");
      if (!A.restricted) actions.append(A.button("修正", "fa-plain", () => startObservation(null, id)));
      main.append(body, actions);
      return { main, body };
    }
    function dimensionRow(item) {
      const row = A.el("div", "fa-item");
      if (newObservationFor(item.id)) { row.append(observationForm(P.observing)); return row; }
      // A Big Five row reads like its scale ("93% 外向"); other rows keep the analysis' own wording,
      // except the extended sliders (same share wording) and the type dims' distribution line.
      const text = item.label ? C.share(item.id, item) || item.label : C.typeText(item.id, item) || "证据不足";
      const { main, body } = head(item.id, text, item.label ? item.confidence : null);
      if (item.label) {
        main.lastChild.prepend(A.button(P.open.has(item.id) ? "收起" : "依据", "fa-plain", () => {
          P.open.has(item.id) ? P.open.delete(item.id) : P.open.add(item.id);
          render();
        }));
        if (P.open.has(item.id)) body.append(detail(item));
      }
      row.append(main);
      return row;
    }
    function detail(item) {
      const box = A.el("div", "fa-persona-detail");
      // 防漂移: a dimension that moved keeps its previous conclusion beside the new one.
      if (item.previous) box.append(A.el("p", "fa-note",
        `上次判断：${item.previous.label}（${item.previous.confidence || "把握未知"}）`));
      if (item.detail) box.append(A.el("p", "fa-text", item.detail));
      if (item.periods) box.append(A.el("p", "fa-note", "不同时期：" + item.periods));
      const links = A.el("div", "fa-choice");
      if (item.support.length) links.append(A.button(`支持的原文 ${item.support.length} 条`, "fa-secondary",
        () => showEvidence(item.support, label(item.id) + " · 支持")));
      if (item.counter.length) links.append(A.button(`相反的原文 ${item.counter.length} 条`, "fa-secondary",
        () => showEvidence(item.counter, label(item.id) + " · 相反")));
      box.append(links);
      const cites = (shared.data.sources || {})[item.id];
      if (cites && cites.length) {
        box.append(A.el("p", "fa-note", `理论依据：${cites[0]}${cites.length > 1 ? ` 等 ${cites.length} 篇` : ""}`),
          A.button("查看文献", "fa-plain", () => showSources(item.id)));
      }
      return box;
    }
    function valuesBlock() {
      const list = A.el("div", "fa-values");
      const entries = new Map((result()?.values || []).map(item => [item.id, item]));
      for (const [id, title] of VALUE_ITEMS) list.append(valueCard(id, title, entries.get(id)));
      return list;
    }
    function valueCard(id, title, item) {
      const card = A.el("section", "fa-value-card");
      card.append(A.el("h3", "", title));
      const report = item?.selfReport, inference = item?.inference;
      const self = A.el("div", "fa-value-row");
      self.append(A.el("span", "fa-row-label", "对方自述"));
      const statements = Array.isArray(report?.statements) ? report.statements : [];
      if (statements.length) {
        const lines = A.el("div", "fa-value-statements");
        for (const statement of statements) {
          const line = A.el("div", "fa-value-statement");
          line.append(A.el("span", "", `${A.formatDate(statement.timeMs)} · ${statement.text}`));
          if (statement.evidence?.length) line.append(A.button("原文", "fa-plain",
            () => showEvidence(statement.evidence, title + " · 对方自述")));
          lines.append(line);
        }
        if (report.moreCount) lines.append(A.el("div", "fa-note", `另有 ${report.moreCount} 条不同自述，可查看全部原文。`));
        self.append(lines);
      } else self.append(A.el("span", "fa-row-text", report?.text || (result() ? "尚无明确自述" : "分析后显示")));
      if (report?.evidence?.length) self.append(A.button(`原文 ${report.evidence.length} 条`, "fa-plain",
        () => showEvidence(report.evidence, title + " · 对方自述")));
      card.append(self);
      const ai = A.el("div", "fa-value-row");
      ai.append(A.el("span", "fa-row-label", "AI 推测"),
        A.el("span", "fa-row-text", inference?.label ?
          [inference.label, inference.detail].filter(Boolean).join(" · ") : result() ? "证据不足，暂不推测" : "分析后显示"));
      if (inference?.label) {
        ai.append(A.el("span", "fa-mark", inference.confidence));
        card.append(ai, detail(inference));
      } else card.append(ai);
      const observe = A.el("div", "fa-value-observation");
      if (newObservationFor(id)) observe.append(observationForm(P.observing));
      else {
        for (const saved of observationsFor(id)) observe.append(observationLine(saved));
        if (!A.restricted) observe.append(A.button("记录我的观察", "fa-plain", () => startObservation(null, id)));
      }
      card.append(observe);
      return card;
    }
    // MBTI keeps 我的观察 beside it; its type itself comes from the chart (自述 first, else the Big Five).
    function reportRow(id, report, title) {
      const row = A.el("div", "fa-item");
      if (id && newObservationFor(id)) { row.append(observationForm(P.observing)); return row; }
      const dims = Object.fromEntries(result().dimensions.map(item => [item.id, item]));
      // MBTI shows the type the chart draws; the friend's own words stay beside it, even when they differ.
      const type = id === "mbti" ? C.mbti(dims, null) : null;
      const text = type ? type.text : report ? report.value : "证据不足";
      let main, body;
      if (id) ({ main, body } = head(id, text, type ? "由图表推算" : report ? "本人自述" : null));
      else {
        main = A.el("div", "fa-item-main");
        body = A.el("div", "fa-item-text");
        const top = A.el("div", "fa-persona-head");
        top.append(A.el("span", "fa-row-label", title), A.el("span", "fa-row-text", text), A.el("span", "fa-mark", "本人自述"));
        body.append(top);
        main.append(body);
      }
      if (report) body.append(A.el("div", "fa-note", (type ? `本人自述 ${report.value} · ` : "") +
        `原话（${A.formatDate(report.timeMs)}）：${report.quote}` +
        (report.differs ? " · 不同时期的说法不一致，显示最近一次" : "")));
      row.append(main);
      return row;
    }
    // The dims of this tab across all its cards, including the friend-only extra rows.
    function tabDims() {
      return new Set(C.cards(tab).flatMap(card => [...card.dims, ...(card.extra || [])]));
    }
    function familyOf() {
      return Object.fromEntries((shared.data.catalog || []).map(item => [item.id, item.group]).filter(([, group]) => group));
    }
    function detailsBlock(data) {
      const details = A.el("details", "fa-details");
      details.append(A.el("summary", "", "分析依据与说明"));
      const list = A.el("dl", "fa-kv");
      const coverage = data.coverage;
      const rows = [["覆盖时间", coverage.fromMs ? `${A.formatDate(coverage.fromMs)} 至 ${A.formatDate(coverage.toMs)}` : "—"],
        ["已处理原文", `${coverage.processed} 条对方文字消息`], ["未计入", `${coverage.excluded} 条媒体或非文字消息`],
        ["证据", `共 ${coverage.evidence} 条，引用 ${coverage.cited} 条`],
        ["核查", data.removed ? `${data.removed} 项未通过逐项核查，已改为证据不足` : "全部通过逐项核查"],
        ["历史", data.historyComplete ? "全部可读历史" : "历史分析未完成"],
        ["模型", data.model || "—"], ["生成时间", A.formatDate(shared.data.createdAtMs, true)]];
      for (const [key, value] of rows) list.append(A.el("dt", "", key), A.el("dd", "", value));
      // The method note follows the catalog: one sentence per family of this tab, so new dimensions
      // describe themselves instead of the wording drifting out of date.
      const families = shared.data.families || {}, groups = familyOf(), dims = tabDims();
      const notes = { personality: "大五人格按 MBTI 五条轴显示、类型由轴推算，依恋是焦虑 × 回避平面" };
      const parts = [];
      for (const [family, name] of Object.entries(families)) {
        const names = Object.keys(groups).filter(key => groups[key] === family && dims.has(key))
          .map(key => C.TYPE_VOCAB[key] ? C.TYPE_VOCAB[key].join("、") : label(key));
        if (names.length) parts.push(`${name}（${names.join("、")}${notes[family] ? "；" + notes[family] : ""}）`);
      }
      details.append(list, A.el("p", "fa-note", "图上的点按证据偏向两端的条数计算，证据少时靠近中间；只有通过核查的维度才画点。" +
        (parts.length ? "维度来自" + parts.join("；") + "。" : "") +
        "全部是根据聊天表现的推测，不是心理测评或诊断；“我的观察”单独保存，不会作为分析的输入。"));
      return details;
    }
    // 防漂移: the page names the dimensions whose position moved by 0.3 or more instead of
    // silently overwriting the previous result.
    function driftBlock(data) {
      const moves = data.drift || [];
      if (!moves.length) return null;
      const details = A.el("details", "fa-details");
      details.append(A.el("summary", "", `与上次相比 ${moves.length} 个维度位置移动 ≥0.3`));
      for (const move of moves) details.append(A.el("p", "fa-note",
        `${label(move.id)}：上次 ${C.poleShare(move.id, { position: move.from })} → 本次 ${C.poleShare(move.id, { position: move.to })}`));
      return details;
    }
    function sourcesBlock() {
      const sources = shared.data.sources || {};
      if (!Object.keys(sources).length) return null;
      const groups = familyOf(), dims = tabDims();
      const details = A.el("details", "fa-details");
      details.append(A.el("summary", "", "参考量表来源"));
      const box = A.el("div", "fa-sources");
      for (const [family, name] of Object.entries(shared.data.families || {})) {
        const cites = [];
        const seen = new Set();
        for (const key of Object.keys(groups).filter(key => groups[key] === family && dims.has(key)))
          for (const cite of sources[key] || []) if (!seen.has(cite)) { seen.add(cite); cites.push(cite); }
        if (!cites.length) continue;
        box.append(A.el("h4", "", name));
        for (const cite of cites) box.append(A.el("p", "", cite));
      }
      if (!box.childNodes.length) return null;
      details.append(box);
      return details;
    }
    function showSources(id) {
      const body = A.openSheet(label(id) + " · 文献", "persona-sources");
      const cites = (shared.data.sources || {})[id] || [];
      if (!cites.length) body.append(A.el("p", "fa-note", "暂无文献记录"));
      for (const cite of cites) body.append(A.el("p", "fa-msg-text", cite));
    }

    async function showEvidence(ids, title) {
      const body = A.openSheet(title, "persona-evidence");
      let offset = 0;
      const more = A.button("加载更多原文", "fa-secondary", () => { void loadMore(); });
      async function loadMore() {
        more.remove();
        more.disabled = true;
        const loading = A.el("p", "fa-note", "读取中…");
        body.append(loading);
        try {
          const batch = ids.slice(offset, offset + 40);
          const data = await A.rpc("page.personaEvidence", { ids: batch });
          if (A.sheetOwner !== "persona-evidence") return;
          loading.remove();
          offset += batch.length;
          const byId = new Map(data.items.map(item => [item.id, item]));
          for (const id of batch) {
            const item = byId.get(id);
            if (!item) continue;
            const row = A.el("div", "fa-msg");
            row.append(A.el("div", "fa-msg-head", `对方 · ${A.formatDate(item.timeMs, true)}` +
              (item.status === "ok" ? "" : item.status === "changed" ? " · 原文已变化" : " · 原文已不在可读历史中")),
              A.el("div", "fa-msg-text", item.excerpt), A.el("div", "fa-note", item.note));
            const context = A.button("看上下文", "fa-plain", async () => {
              context.disabled = true;
              try { row.append(A.contextList(await A.host("message.context", { id: item.messageId }))); context.remove(); }
              catch (error) { context.disabled = false; A.toast(error.message || "读取失败"); }
            });
            row.append(context);
            body.append(row);
          }
          if (offset < ids.length) { more.disabled = false; body.append(more); }
        } catch (error) {
          loading.textContent = error.message || "读取失败";
          more.disabled = false;
          body.append(more);
        }
      }
      await loadMore();
    }

    // -- 我的观察 ---------------------------------------------------------------------------------
    function observationLine(item) {
      const line = A.el("div", "fa-observation");
      if (P.observing?.item?.id === item.id) { line.append(observationForm(P.observing)); return line; }
      line.append(A.el("span", "fa-row-label", "我的观察"), A.el("span", "", item.judgement));
      if (item.note) line.append(A.el("span", "fa-note", " · " + item.note));
      if (!A.restricted) line.append(A.button("编辑", "fa-plain", () => startObservation(item)),
        A.button("删除", "fa-plain", () => deleteObservation(item)));
      return line;
    }
    function shownDimensions() {
      const keys = new Set((shared.data.catalog || []).map(item => item.id));
      keys.add("mbti");
      for (const [id] of VALUE_ITEMS) keys.add(id);
      return keys;
    }
    function otherObservations() {
      const known = result() ? shownDimensions() : new Set(VALUE_KEYS);
      const items = shared.data.observations.filter(item => !item.dimension || !known.has(item.dimension));
      const block = A.el("section", "fa-section");
      const top = A.el("div", "fa-group");
      top.append(A.el("span", "", "其他观察"), A.el("span", "fa-spacer"));
      if (!A.restricted) top.append(A.button("记录观察", "fa-plain", () => startObservation(null, "")));
      block.append(top);
      const list = A.el("div", "fa-list");
      if (P.observing && !P.observing.item && !known.has(P.observing.dimension)) {
        const row = A.el("div", "fa-item");
        row.append(observationForm(P.observing));
        list.append(row);
      }
      for (const item of items) {
        const row = A.el("div", "fa-item");
        if (item.legacyLabel) row.append(A.el("div", "fa-note", "原关联：" + item.legacyLabel));
        row.append(observationLine(item));
        list.append(row);
      }
      if (list.childNodes.length) block.append(list);
      return block;
    }
    function startObservation(item, dimension = "") {
      if (A.restricted) return;
      const entry = item ? "observation:" + item.id : "observation:new";
      const draft = A.draft(entry);
      const base = draft.payload || { judgement: item?.judgement || "", note: item?.note || "",
        dimension: item ? item.dimension || "" : dimension };
      const baseVersion = draft.payload && Number.isInteger(draft.baseVersion) ? draft.baseVersion : item?.version ?? null;
      P.observing = { entry, item, draft, ...base, baseVersion, restored: draft.restored };
      // The editor opens inside the chart that owns its dimension.
      if (result() && base.dimension && owns(base.dimension) && !VALUE_KEYS.has(base.dimension))
        P.selected = C.cardOf(base.dimension) || "big5";
      render();
    }
    function observationForm(state) {
      const form = A.el("div", "fa-form");
      const judgement = A.el("input");
      judgement.type = "text";
      judgement.maxLength = 200;
      judgement.placeholder = "你的判断，例如：熟了以后说话很直接";
      judgement.value = state.judgement;
      judgement.dataset.key = state.entry + ":judgement";
      const note = A.el("textarea");
      note.placeholder = "依据备注（可选）";
      note.value = state.note;
      note.dataset.key = state.entry + ":note";
      const dimension = A.el("select");
      dimension.setAttribute("aria-label", "关联的维度");
      const options = [["", "不关联维度"], ["mbti", label("mbti")],
        ...(shared.data.catalog || []).map(item => [item.id, label(item.id)]), ...VALUE_ITEMS];
      for (const [value, text] of options) {
        const option = A.el("option", "", text);
        option.value = value;
        dimension.append(option);
      }
      if (state.dimension && !options.some(([value]) => value === state.dimension)) {
        const option = A.el("option", "", "原关联维度");
        option.value = state.dimension;
        dimension.append(option);
      }
      dimension.value = state.dimension || "";
      const unsaved = A.el("span", "fa-unsaved", state.restored ? "未保存（已恢复上次编辑）" : "");
      const changed = () => {
        Object.assign(state, { judgement: judgement.value, note: note.value, dimension: dimension.value });
        unsaved.textContent = "未保存";
        A.updateDraft(state.draft, { judgement: state.judgement, note: state.note, dimension: state.dimension },
          state.baseVersion);
      };
      for (const input of [judgement, note, dimension]) input.addEventListener("input", changed);
      const finish = () => { P.observing = null; void load(); };
      const save = A.button("保存", "fa-primary", async () => {
        save.disabled = true;
        try {
          await A.host("observations.save", { id: state.item?.id ?? null,
            judgement: state.judgement, note: state.note, dimension: state.dimension || null,
            expectedVersion: state.baseVersion });
          await A.closeDraft(state.entry);
          finish();
        } catch (error) { save.disabled = false; A.toast(error.message || "保存失败"); }
      });
      const cancel = A.button("放弃", "fa-plain", async () => { await A.closeDraft(state.entry); finish(); });
      const actions = A.el("div", "fa-form-actions");
      actions.append(save, cancel, unsaved);
      form.append(judgement, note, dimension, actions);
      return form;
    }
    async function deleteObservation(item) {
      if (!await A.confirm("删除这条观察？", "删除")) return;
      try {
        await A.host("observations.save", { action: "delete", id: item.id });
        void load();
      } catch (error) { A.toast(error.message || "删除失败"); }
    }

    const page = {
      load, render,
      // Restores the drafts whose dimension belongs to this tab; 不关联维度 drafts live on 性格分析.
      async restoreDrafts(drafts) {
        if (!shared.data) await fetchData();
        let restored = false;
        for (const [entry, saved] of drafts) if (entry.startsWith("observation:")) {
          const id = entry.slice(12);
          const item = id === "new" ? null : shared.data?.observations.find(candidate => candidate.id === id);
          const dimension = item ? item.dimension : saved?.payload?.dimension || "";
          if ((id === "new" || item) && (dimension && owns(dimension) || !dimension && tab === "personality" ||
              dimension && !C.tabOf(dimension) && dimension !== "mbti" && !VALUE_KEYS.has(dimension) && tab === "personality")) {
            startObservation(item, id === "new" ? dimension : "");
            restored = true;
          }
        }
        return restored;
      },
    };
    shared.pages.push(page);
    return page;
  }

  window.FriendPersona = make("personality", "pagePersona");
  window.FriendValues = make("values", "pageValues");
})();

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 聊天分析: how the friend responds to me, from block labels of both sides, plus local reply
// statistics. The folded 旧版画像 is the earlier one-sided portrait, read only when it is opened.
(function () {
  const A = window.FriendApp;
  const C = { data: null, request: 0, side: "other", legacy: null, legacyOpen: false, legacyRequest: 0 };
  const ASK = { 提问: "你提问后", 分享: "你分享后", 邀请: "你邀请后", 关心: "你关心对方后", 玩笑: "你开玩笑后",
    请求帮助: "你请求帮助后", 回应: "你回应对方后", 混合: "意图混合时", 其他: "其他情况", 未判定: "意图未判定时" };
  const percent = value => `${Math.max(1, Math.round(value * 100))}%`;
  function range(from, to) {
    if (!from) return "";
    const left = A.formatDate(from), right = A.formatDate(to);
    return left === right ? left : `${left} 至 ${right}`;
  }

  async function load() {
    const request = ++C.request;
    try {
      const data = await A.rpc("page.chat");
      if (request !== C.request) return;
      C.data = data;
      render();
      if (!data.configured && !data.statistics && !C.legacy) void loadLegacy();
    } catch (error) {
      if (request === C.request && !A.masked)
        A.byId("pageChat").replaceChildren(A.el("div", "fa-empty", error.message || "聊天分析读取失败"));
    }
  }

  function section(title, content, aside) {
    const node = A.el("section", "fa-card");
    const head = A.el("div", "fa-card-head");
    head.append(A.el("h3", "", title));
    if (aside) head.append(A.el("span", "fa-spacer"), typeof aside === "string" ? A.el("span", "fa-card-aside", aside) : aside);
    node.append(head, content);
    return node;
  }
  // One labelled bar. A null fraction has no ratio: a dashed empty track, never a filled 0.
  function meter(label, fraction, value, onClick) {
    const row = onClick ? A.button("", "fa-meter fa-meter-button", onClick) : A.el("div", "fa-meter");
    const track = A.el("span", fraction === null ? "fa-track unknown" : "fa-track");
    track.setAttribute("aria-hidden", "true");
    const fill = A.el("i");
    if (fraction !== null) fill.style.width = `${Math.round(Math.max(0, Math.min(1, fraction)) * 1000) / 10}%`;
    track.append(fill);
    row.append(A.el("span", "fa-meter-label", label), track, A.el("span", "fa-meter-value", value));
    return row;
  }

  function render() {
    const root = A.byId("pageChat");
    if (A.masked || !C.data) return;
    const data = C.data, coverage = data.coverage;
    const open = root.querySelector("details[data-key='legacy']")?.open ?? C.legacyOpen;
    root.replaceChildren();
    if (coverage) {
      const line = [coverage.fromMs ? `已分析 ${range(coverage.fromMs, coverage.toMs)} 的聊天，共 ${coverage.blocks} 段连续消息` :
        "还没有可分析的完整互动"];
      if (coverage.pending) line.push(`另有 ${coverage.pending} 条待下次分析`);
      if (coverage.missingTextBlocks) line.push(`${coverage.missingTextBlocks} 个文本块的标注尚未完成，需补齐`);
      if (coverage.outdated) line.push("旧版结果，需重新分析");
      root.append(A.el("p", "fa-facts", line.join(" · ")));
    }
    root.append(replyCard(data.reply, data.historyReady));
    if (data.statistics) {
      root.append(responseCard(data.statistics.responses));
      const invites = data.statistics.invites;
      if (invites.total) root.append(inviteCard(invites));
      root.append(signalCards(data.statistics.signals));
      root.append(keywordCard(data.statistics.keywords));
    } else if (!data.configured) {
      root.append(section("对方如何回应我", A.el("p", "fa-note", "回应方式需要配置 API 模型。")));
      if (C.legacy) root.append(...legacyCards(C.legacy));
    } else root.append(A.el("p", "fa-note", "开始分析后显示双方的意图与情绪，以及对方如何回应你。"));
    if (data.configured || data.statistics) root.append(legacyDetails(open));
    root.append(methodDetails());
  }

  function replyCard(reply, ready) {
    const body = A.el("div");
    if (!reply.eligible && !reply.observing) body.append(A.el("p", "fa-note", "还没有可统计的互动。"));
    else {
      body.append(A.el("p", "fa-hero-text", reply.rate === null ?
        `仅 ${reply.eligible} 次可判定，暂不给比例（对方 24 小时内回应 ${reply.replied} 次）` :
        `对方在 24 小时内回应了 ${percent(reply.rate)}（${reply.replied} / ${reply.eligible} 次）`));
      const meta = A.el("div", "fa-item-meta");
      if (reply.medianSeconds !== null) meta.append(A.el("span", "", "回复时间中位数 " + A.formatDuration(reply.medianSeconds)));
      if (reply.observing) meta.append(A.el("span", "", `观察中 ${reply.observing} 次（不足 24 小时）`));
      if (reply.excluded) meta.append(A.el("span", "", `${reply.excluded} 次顺序或时间无法确认，未计入`));
      if (!ready) meta.append(A.el("span", "fa-mark warn", "聊天历史仍在读取，按已读部分统计"));
      if (meta.childNodes.length) body.append(meta);
    }
    return section("回复率", body, "本地统计");
  }

  function responseCard(groups) {
    const list = A.el("div", "fa-list");
    for (const group of groups) {
      const row = A.el("div", "fa-item");
      const head = A.el("div", "fa-persona-head");
      head.append(A.el("span", "fa-row-label", ASK[group.ask] || group.ask), A.el("span", "fa-row-text", `${group.rounds} 次互动`));
      row.append(head);
      const meters = A.el("div", "fa-meters");
      for (const item of group.items)
        meters.append(meter(item.label, item.share, item.share === null ? `${item.count} 次` : percent(item.share),
          () => showExamples(`${ASK[group.ask] || group.ask} · ${item.label}`, item.examples)));
      if (group.items.length) row.append(meters);
      const meta = A.el("div", "fa-item-meta");
      if (group.determinable < 5) meta.append(A.el("span", "", `仅 ${group.determinable} 次可判定，暂不给比例`));
      if (group.undetermined) meta.append(A.el("span", "", `${group.undetermined} 次无法判断`));
      if (group.fromMs) meta.append(A.el("span", "", range(group.fromMs, group.toMs)));
      if (meta.childNodes.length) row.append(meta);
      list.append(row);
    }
    if (!groups.length) list.append(A.el("p", "fa-note", "还没有你发言后对方在 24 小时内接话的互动。"));
    return section("对方如何回应我", list, "点一行看原文");
  }

  function inviteCard(invites) {
    const counts = invites.counts;
    const body = A.el("div");
    body.append(A.el("p", "fa-hero-text", ["明确接受", "明确婉拒", "未明确答复", "无法判断"]
      .filter(label => counts[label]).map(label => `${label} ${counts[label]} 次`).join(" · ")));
    body.append(A.el("p", "fa-note", (invites.withoutReply ? `未明确答复含 ${invites.withoutReply} 次对方没有接话。` : "") +
      "没有明确答复不当作拒绝。"));
    return section("邀请", body, `共 ${invites.total} 次`);
  }

  // Shares of labelled blocks; the long tail becomes 其他 so each side stays a short list.
  function shareList(group, limit = 5) {
    const list = A.el("div", "fa-meters");
    const items = [...group.items].sort((left, right) => right.count - left.count);
    const top = items.slice(0, limit), rest = items.slice(limit).reduce((sum, item) => sum + item.count, 0);
    for (const item of top) list.append(meter(item.label, item.share, percent(item.share)));
    if (rest) list.append(meter("其他", rest / group.total, percent(rest / group.total)));
    if (!group.total) list.append(A.el("p", "fa-note", "暂无"));
    return list;
  }
  function compare(signals, key) {
    const grid = A.el("div", "fa-compare");
    for (const [side, name] of [["self", "我"], ["other", "对方"]]) {
      const column = A.el("div");
      column.append(A.el("div", "fa-compare-head", `${name} · ${signals[side].labelled} 段`), shareList(signals[side][key]));
      grid.append(column);
    }
    return grid;
  }
  function signalCards(signals) {
    const pair = A.el("div", "fa-cards");
    pair.append(section("交流意图", compare(signals, "intent")), section("情绪信号", compare(signals, "emotion")));
    return pair;
  }
  function keywordCard(keywords) {
    const segment = A.el("div", "fa-segment");
    for (const [value, label] of [["other", "对方"], ["self", "我"]])
      segment.append(A.button(label, "", () => { C.side = value; render(); }, { "aria-pressed": String(C.side === value) }));
    const chips = A.el("div", "fa-chips");
    for (const item of keywords[C.side] || []) {
      const chip = A.el("span", "fa-chip", item.word);
      chip.append(A.el("small", "", `${item.count} 次`));
      chips.append(chip);
    }
    if (!chips.childNodes.length) chips.append(A.el("span", "fa-note", "暂无出现两次以上的词"));
    return section("高频词", chips, segment);
  }

  // -- examples: my block and the friend's answer, read from the managed history ----------------
  let sheetToken = 0;
  async function showExamples(title, ids) {
    const token = ++sheetToken;
    const body = A.openSheet(title, "chat-round");
    if (!ids.length) { body.append(A.el("p", "fa-note", "没有可显示的例子。")); return; }
    const loading = A.el("p", "fa-note", "读取中…");
    body.append(loading);
    try {
      const results = await Promise.all(ids.map(id => A.rpc("page.round", { id })));
      if (token !== sheetToken || A.sheetOwner !== "chat-round") return;
      loading.remove();
      for (const result of results) {
        const box = A.el("div", "fa-round");
        for (const block of result.blocks) for (const message of block.messages) {
          const row = A.el("div", "fa-msg");
          row.append(A.el("div", "fa-msg-head", `${message.speaker} · ${A.formatDate(message.timeMs, true)}`),
            A.el("div", "fa-msg-text", message.text));
          box.append(row);
        }
        if (!result.blocks.length) box.append(A.el("p", "fa-note", "原文已不在当前可读历史中。"));
        body.append(box);
      }
      body.append(A.el("p", "fa-note", "每个例子是你的一段消息和对方随后的回应，最多显示 3 个。"));
    } catch (error) { if (token === sheetToken) loading.textContent = error.message || "读取失败"; }
  }

  // -- 旧版画像: the earlier one-sided portrait, kept for reference and never updated here --------
  const TRAITS = { socialEnergy: "表达活力", humor: "幽默表达", composure: "情绪平和", initiative: "话题主动",
    care: "关怀支持", affection: "亲近表达" };
  const score = value => Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
  // Model probabilities averaged over messages need not add up to 1; the tail becomes 其他.
  function shares(entries, limit = 4) {
    const valid = (Array.isArray(entries) ? entries : []).filter(item => typeof item?.label === "string" &&
      Number.isFinite(item.probability) && item.probability > 0).sort((left, right) => right.probability - left.probability);
    const top = valid.slice(0, limit).filter(item => item.probability >= 0.005);
    const rest = valid.slice(top.length).reduce((sum, item) => sum + item.probability, 0);
    return [...top.map(item => ({ label: item.label, share: Math.min(1, item.probability) })),
      ...(rest >= 0.005 ? [{ label: "其他", share: Math.min(1, rest) }] : [])];
  }
  function statisticsModel(profile, source, analyzed, total, local = false) {
    return { local, source, emotions: shares(profile.emotion), intents: shares(profile.intent),
      words: (profile.keywords || []).filter(item => typeof item?.word === "string"),
      traits: (profile.traits || []).map(item => ({ label: item.label, value: score(Math.round(item.val)) })),
      affinity: score(profile.affinity), analyzed, total };
  }
  // One saved portrait of the retired analysis: local Laya statistics or an API source.
  function legacyModel(item) {
    if (!item) return { empty: true };
    if (item.kind === "statistics") return statisticsModel(item, item.label, item.analyzed, item.total, item.source === "local");
    const portrait = item.portrait || {};
    return { source: item.label, summary: portrait.summary, communication: portrait.communication,
      emotion: portrait.emotionExpression, preferences: portrait.interactionPreferences,
      patterns: portrait.patterns || [], boundaries: portrait.boundaries || [], uncertain: portrait.uncertain || [],
      topics: portrait.topics || [], traits: Object.entries(TRAITS).map(([key, label]) => ({ label, value: score(portrait.traits?.[key]) })),
      affinity: score(portrait.affinity), analyzed: item.analyzed, total: item.total };
  }
  async function loadLegacy() {
    const request = ++C.legacyRequest;
    try {
      const data = await A.host("legacy.portrait");
      if (request !== C.legacyRequest) return;
      C.legacy = data.error ? { error: data.error } : legacyModel(Array.isArray(data.items) ? data.items[0] : null);
    } catch (error) {
      if (request !== C.legacyRequest) return;
      C.legacy = { error: error.message || "旧版画像读取失败" };
    }
    render();
  }
  function legacyDetails(open) {
    const details = A.el("details", "fa-details");
    details.dataset.key = "legacy";
    details.open = open;
    details.append(A.el("summary", "", "已有分析详情（旧版画像）"));
    details.addEventListener("toggle", () => {
      C.legacyOpen = details.open;
      if (details.open && !C.legacy) void loadLegacy();
    });
    if (C.legacy) details.append(...legacyCards(C.legacy));
    else if (open) details.append(A.el("p", "fa-note", "读取中…"));
    return details;
  }
  function legacyCards(model) {
    if (model.error) return [A.el("p", "fa-note", model.error)];
    if (model.empty) return [A.el("p", "fa-note", "没有旧版画像。")];
    const nodes = [];
    if (model.summary) nodes.push(section("旧版摘要", A.el("p", "fa-text", model.summary)));
    const style = A.el("dl", "fa-kv");
    for (const [label, value] of [["交流方式", model.communication], ["情绪表达", model.emotion], ["互动偏好", model.preferences]])
      if (value) style.append(A.el("dt", "", label), A.el("dd", "", value));
    if (style.childNodes.length) nodes.push(section("沟通方式", style));
    const known = model.traits.some(item => item.value !== null);
    const behaviour = A.el("div");
    if (known) {
      const meters = A.el("div", "fa-meters fa-meters-two");
      for (const item of model.traits) meters.append(meter(item.label, item.value === null ? null : item.value / 100,
        item.value === null ? "未知" : String(item.value)));
      behaviour.append(meters);
    }
    for (const pattern of model.patterns || []) behaviour.append(A.el("div", "fa-text fa-pattern", "· " + pattern));
    if (behaviour.childNodes.length) nodes.push(section("互动表现", behaviour, known ? "按聊天表现估计，满分 100" : ""));
    const pair = A.el("div", "fa-cards");
    for (const [title, items] of [["对方的情绪信号", model.emotions], ["对方的交流意图", model.intents]]) if ((items || []).length) {
      const list = A.el("div", "fa-meters");
      for (const item of items) list.append(meter(item.label, item.share, percent(item.share)));
      pair.append(section(title, list, "约占对方消息"));
    }
    if (pair.childNodes.length) nodes.push(pair);
    if ((model.words || []).length || (model.topics || []).length) {
      const chips = A.el("div", "fa-chips");
      for (const item of model.words || []) {
        const chip = A.el("span", "fa-chip", item.word);
        if (Number.isInteger(item.count)) chip.append(A.el("small", "", `${item.count} 次`));
        chips.append(chip);
      }
      for (const topic of model.topics || []) chips.append(A.el("span", "fa-chip", topic));
      nodes.push(section(model.words?.length ? "对方高频词" : "常聊话题", chips));
    }
    const rating = A.el("div");
    const affinity = A.el("div", "fa-meters");
    affinity.append(meter("互动评分", model.affinity === null ? null : model.affinity / 100,
      model.affinity === null ? "待分析" : String(model.affinity)));
    rating.append(affinity, A.el("p", "fa-note", "原“好感度”。评分方法已知存在问题，后续会重新设计，仅供参考。"));
    nodes.push(section("互动评分", rating));
    const limits = A.el("dl", "fa-kv");
    if ((model.boundaries || []).length) limits.append(A.el("dt", "", "明确边界"), A.el("dd", "", model.boundaries.join("；")));
    if ((model.uncertain || []).length) limits.append(A.el("dt", "", "证据不足"), A.el("dd", "", model.uncertain.join("；")));
    if (limits.childNodes.length) nodes.push(section("其他", limits));
    const coverage = Number.isFinite(model.total) && model.total > 0 ?
      ` · 已分析 ${Math.min(model.total, Number(model.analyzed) || 0)} / ${model.total} 条对方文本` : "";
    nodes.push(A.el("p", "fa-note", `${model.source}${coverage} · 推测。旧版只分析对方消息，此处不再更新；MBTI 已移到性格分析。`));
    return nodes;
  }

  function methodDetails() {
    const details = A.el("details", "fa-details");
    details.append(A.el("summary", "", "统计口径"));
    const list = A.el("dl", "fa-kv");
    for (const [key, value] of [
      ["一段消息", "同一方连续发出的消息算一段；你连发三条、对方回一条只算一次互动。"],
      ["回复率", "你的一段消息之后 24 小时内对方接话算回应，只回表情、图片或语音也算；不足 24 小时的单列“观察中”。"],
      ["回应方式", "由模型按原文逐段标注，只描述可见的表达；慢回复或简短回复不代表冷淡。可判定少于 5 次时不给比例。"],
      ["邀请", "只有对方明确答复才算接受或婉拒，没有明确答复不当作拒绝。"],
      ["未计入", "系统消息、同一秒的双方消息与时间未知的消息不参与配对；引用的内容不算发言人本人的表达。"]])
      list.append(A.el("dt", "", key), A.el("dd", "", value));
    details.append(list);
    return details;
  }

  window.FriendChat = { load };
})();

"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 关系记忆: candidates, confirmed records, tags/timeline views, editing and evidence.
(function () {
  const A = window.FriendApp;
  const TAGS = ["基本信息", "经历", "性格", "爱好", "偏好与忌口", "边界", "共同事件"];
  const PAGE = 50;
  const M = { view: "tags", pending: [], confirmed: [], total: 0, version: 0, loading: false, request: 0,
    editing: new Map(), adding: false, ignored: null, overview: null, pendingShown: PAGE };
  const page = () => A.byId("pageMemory");
  const evidenceWords = { changed: "原文已变化", missing: "原文当前缺失", recalled: "原文已撤回",
    unready: "原文待核对", "source-changed": "来源已变化" };

  async function loadList(status, offset, limit) {
    return A.api("/api/friend/memories?" + A.query({ view: M.view, status, offset: String(offset), limit: String(limit) }));
  }
  async function load(reset = true) {
    const request = ++M.request;
    M.loading = true;
    try {
      const pending = [];
      for (let offset = 0; offset < 1000; offset += 200) {
        const part = await loadList("pending", offset, 200);
        pending.push(...part.items);
        if (offset + 200 >= part.total) break;
      }
      const confirmed = await loadList("confirmed", reset ? 0 : M.confirmed.length, PAGE);
      if (request !== M.request) return;
      M.pending = pending;
      M.confirmed = reset ? confirmed.items : M.confirmed.concat(confirmed.items);
      M.total = confirmed.total;
      M.version = confirmed.memoryVersion;
    } catch (error) {
      if (request === M.request && !A.masked) A.toast(error.message || "记忆读取失败");
    } finally {
      if (request === M.request) {
        M.loading = false;
        M.loaded = true;
        render();
        if (M.restore) applyDrafts();
      }
    }
  }

  function timeLabel(item) {
    if (item.eventTime) return "事件 " + item.eventTime;
    if (item.firstMentionMs) return "首次提及 " + A.formatDate(item.firstMentionMs);
    return "";
  }
  function marks(item) {
    const result = [];
    if (item.status === "pending") result.push(["待确认", "pending"]);
    if (item.relation === "supplement") result.push(["补充", "warn"]);
    if (item.relation === "conflict") result.push(["与原记录矛盾", "warn"]);
    if (item.relation === "uncertain") result.push(["待核对是否重复", "warn"]);
    if (item.origin === "user") result.push(["用户记录" + (item.evidenceCount ? "" : " · 未关联原文"), ""]);
    if (item.userCorrected) result.push(["用户修正", ""]);
    for (const state of item.evidenceStates || []) if (evidenceWords[state]) result.push([evidenceWords[state], "warn"]);
    return result;
  }
  function itemRow(item) {
    const row = A.el("div", "fa-item");
    row.dataset.id = item.id;
    const main = A.el("div", "fa-item-main");
    const body = A.el("div", "fa-item-text");
    body.append(A.el("div", "", item.text));
    const meta = A.el("div", "fa-item-meta");
    meta.append(A.el("span", "", item.tags.join("、")));
    if (timeLabel(item)) meta.append(A.el("span", "", timeLabel(item)));
    for (const [label, kind] of marks(item)) meta.append(A.el("span", "fa-mark" + (kind ? " " + kind : ""), label));
    body.append(meta);
    const actions = A.el("div", "fa-item-actions");
    if (item.evidenceCount) actions.append(A.button("原文", "fa-plain", () => openEvidence(item)));
    // A restricted (Agent-opened, read-only) window shows records without review or edit actions.
    if (!A.restricted) {
      if (item.status === "pending" && item.relation !== "conflict") {
        actions.append(A.button(item.relation === "supplement" ? "确认合并" : "确认", "fa-plain", () => decide(item, "confirm")),
          A.button("忽略", "fa-plain", () => decide(item, "ignore")));
      } else if (item.status === "pending") {
        actions.append(A.button("忽略", "fa-plain", () => decide(item, "ignore")));
      } else if (item.status === "confirmed") {
        actions.append(A.button("编辑", "fa-plain", () => startEdit(item)), A.button("删除", "fa-plain", () => remove(item)));
      }
    }
    main.append(body, actions);
    row.append(main);
    if (item.relatedId && item.status === "pending") row.append(relatedBlock(item));
    const editing = M.editing.get(item.id);
    if (editing) row.append(editor(editing));
    return row;
  }
  function relatedBlock(item) {
    const block = A.el("div", "fa-related", "相关原记录读取中…");
    void A.api("/api/friend/memory?" + A.query({ id: item.relatedId })).then(data => {
      block.textContent = (item.relation === "supplement" ? "将合并到：" : "原记录：") + data.memory.text;
      if (item.relation === "conflict" && !A.restricted) {
        const choice = A.el("div", "fa-choice");
        choice.append(A.button("保留原记录", "fa-secondary", () => decide(item, "confirm", "keep")),
          A.button("采用新说法", "fa-secondary", () => decide(item, "confirm", "adopt")),
          A.button("分别注明时期", "fa-secondary", () => decide(item, "confirm", "both")));
        block.append(choice);
      }
    }).catch(() => { block.textContent = "原记录已不存在，可直接确认或忽略。"; });
    return block;
  }

  function group(title, items, batch) {
    const fragment = document.createDocumentFragment();
    const head = A.el("div", "fa-group");
    head.append(A.el("span", "", title), A.el("span", "fa-spacer"));
    if (batch && !A.restricted) for (const [label, action] of batch) head.append(A.button(label, "fa-plain", action));
    const list = A.el("div", "fa-list");
    for (const item of items) list.append(itemRow(item));
    fragment.append(head, list);
    return fragment;
  }
  function firstTag(item) { return TAGS.find(tag => item.tags.includes(tag)) || TAGS[0]; }
  function render() {
    const root = page();
    if (A.masked) return;
    // Keep typing position when background progress re-renders the list.
    const active = document.activeElement;
    const focusKey = root.contains(active) ? active?.dataset?.key : null;
    const selection = focusKey ? [active.selectionStart, active.selectionEnd] : null;
    const scroll = root.parentElement.scrollTop;
    renderInto(root);
    root.parentElement.scrollTop = scroll;
    if (focusKey) {
      const target = root.querySelector(`[data-key="${CSS.escape(focusKey)}"]`);
      if (target) { target.focus(); try { target.setSelectionRange(...selection); } catch { } }
    }
  }
  function renderInto(root) {
    root.replaceChildren();
    if (M.ignored) { renderIgnored(root); return; }
    const bar = A.el("div", "fa-bar");
    const segment = A.el("div", "fa-segment");
    for (const [view, label] of [["tags", "按标签"], ["timeline", "按时间线"]])
      segment.append(A.button(label, "", () => { if (M.view !== view) { M.view = view; void load(); } },
        { "aria-pressed": String(M.view === view) }));
    // Analysis schemes propose candidates; this host page reviews and edits the user's memories.
    bar.append(segment, A.el("span", "fa-spacer"));
    if (!A.restricted) bar.append(A.button("新增", "fa-plain", () => startAdd()));
    root.append(bar);
    if (M.adding) {
      const block = A.el("div", "fa-list");
      block.append(editor(M.adding));
      root.append(block);
    }
    if (M.pending.length) {
      const all = [["全部确认", () => batch("batch-confirm", { all: true })], ["全部忽略", () => batch("batch-ignore", { all: true })]];
      // Long candidate lists render in pages; group batches act only on what is shown.
      const shown = M.pending.slice(0, M.pendingShown);
      if (M.view === "tags") {
        root.append(A.el("div", "fa-group", `待确认 ${M.pending.length} 条`));
        const head = root.lastChild;
        head.append(A.el("span", "fa-spacer"));
        if (!A.restricted) for (const [label, action] of all) head.append(A.button(label, "fa-plain", action));
        for (const tag of TAGS) {
          const items = shown.filter(item => firstTag(item) === tag);
          if (items.length) root.append(group(`${tag} · ${items.length}`, items, [
            ["确认本组", () => batch("batch-confirm", null, items)], ["忽略本组", () => batch("batch-ignore", null, items)]]));
        }
      } else root.append(group(`待确认 ${M.pending.length} 条`, shown, all));
      if (shown.length < M.pending.length)
        root.append(A.button(`显示更多待确认（还有 ${M.pending.length - shown.length} 条）`, "fa-plain", () => {
          M.pendingShown += PAGE;
          render();
        }));
    }
    if (M.confirmed.length) {
      if (M.pending.length) root.append(A.el("div", "fa-group", `已确认 ${M.total} 条`));
      if (M.view === "tags") {
        for (const tag of TAGS) {
          const items = M.confirmed.filter(item => firstTag(item) === tag);
          if (items.length) root.append(group(tag, items));
        }
      } else root.append(group("已确认 · 按事件或首次提及时间", M.confirmed));
      if (M.confirmed.length < M.total)
        root.append(A.button(`加载更多（还有 ${M.total - M.confirmed.length} 条）`, "fa-plain", () => load(false)));
    }
    if (!M.pending.length && !M.confirmed.length && !M.adding && !M.loading) {
      const empty = A.el("div", "fa-empty");
      const built = M.overview?.memory?.built;
      empty.append(A.el("p", "", built ? "还没有记忆。可以手动新增，或等有新聊天时再分析。" :
        "还没有关系记忆。开始分析后，这里会出现待你确认的候选，也可以手动新增。"));
      root.append(empty);
    }
  }
  function renderIgnored(root) {
    const bar = A.el("div", "fa-bar");
    bar.append(A.button("返回", "fa-plain", () => { M.ignored = null; render(); }),
      A.el("span", "fa-note", "已忽略的记录不会再次自动提出，也不会用于建议。"));
    root.append(bar);
    if (!M.ignored.length) root.append(A.el("div", "fa-empty", "没有已忽略的记录。"));
    else root.append(group(`已忽略 ${M.ignored.length} 条`, M.ignored));
  }
  async function showIgnored() {
    try {
      const data = await loadList("rejected", 0, 200);
      M.ignored = data.items;
      render();
    } catch (error) { A.toast(error.message || "读取失败"); }
  }

  // -- decisions ---------------------------------------------------------------------------
  async function mutate(body, message) {
    try {
      const result = await A.api("/api/friend/memory", { method: "POST", body });
      if (message) A.toast(message);
      A.emit({ type: "memory-changed" });
      return result;
    } catch (error) {
      A.toast(error.message || "操作失败");
      if (error.code === "request-conflict") void load();
      return null;
    } finally { void load(); }
  }
  function decide(item, action, choice) {
    return mutate({ action, id: item.id, expectedVersion: item.version, ...(choice ? { choice } : {}) });
  }
  async function batch(action, scope, items) {
    const verb = action === "batch-confirm" ? "确认" : "忽略";
    const count = items ? items.length : M.pending.length;
    if (!await A.confirm(`${verb}所选范围内的 ${count} 条候选？${action === "batch-confirm" ? "需要单独选择的矛盾候选会跳过。" : ""}`)) return;
    return mutate(items ? { action, ids: items.map(item => item.id) } :
      { action, scope, expectedMemoryVersion: M.version }, `已${verb}`);
  }
  async function remove(item) {
    if (!await A.confirm("删除这条记忆？刷新页面可还原演示。", "删除")) return;
    return mutate({ action: "delete", id: item.id, expectedVersion: item.version }, "已删除");
  }
  async function clearAll() {
    if (!await A.confirm("清空这位联系人的演示记忆？刷新页面可还原。", "清空")) return;
    return mutate({ action: "clear" }, "已清空");
  }

  // -- editor with autosaved drafts ---------------------------------------------------------------
  function editorState(entry, item) {
    const draft = A.draft(entry);
    const base = draft.payload || { text: item?.text || "", tags: item?.tags || [], eventTime: item?.eventTime || "" };
    return { entry, item, draft, text: base.text, tags: [...base.tags], eventTime: base.eventTime || "",
      restored: draft.restored, baseVersion: item?.version ?? null };
  }
  function startEdit(item) { M.editing.set(item.id, editorState("memory:" + item.id, item)); render(); }
  function startAdd() { M.adding = M.adding || editorState("memory:new", null); render(); }
  function editor(state) {
    const form = A.el("div", "fa-form");
    const text = A.el("textarea");
    text.value = state.text;
    text.maxLength = 300;
    text.placeholder = "例如：对方不吃香菜";
    text.setAttribute("aria-label", "记忆内容");
    text.dataset.key = state.entry + ":text";
    const tags = A.el("div", "fa-tags");
    for (const tag of TAGS) {
      const chip = A.button(tag, "fa-tag", () => {
        state.tags = state.tags.includes(tag) ? state.tags.filter(value => value !== tag) : [...state.tags, tag];
        chip.setAttribute("aria-pressed", String(state.tags.includes(tag)));
        changed();
      }, { "aria-pressed": String(state.tags.includes(tag)) });
      tags.append(chip);
    }
    const when = A.el("input");
    when.type = "text";
    when.value = state.eventTime;
    when.placeholder = "事件时间（可选）：YYYY、YYYY-MM 或 YYYY-MM-DD";
    when.setAttribute("aria-label", "事件时间");
    when.dataset.key = state.entry + ":when";
    const actions = A.el("div", "fa-form-actions");
    const unsaved = A.el("span", "fa-unsaved", state.restored ? "未保存（已恢复上次编辑）" : "");
    const save = A.button("保存", "fa-primary", async () => {
      save.disabled = true;
      const body = state.item ? { action: "edit", id: state.item.id, expectedVersion: state.baseVersion } : { action: "create" };
      const result = await mutate({ ...body, text: state.text, tags: state.tags, eventTime: state.eventTime || null },
        "已保存");
      save.disabled = false;
      if (result) { await A.closeDraft(state.entry); finish(); }
    });
    const cancel = A.button("放弃", "fa-plain", async () => { await A.closeDraft(state.entry); finish(); });
    actions.append(save, cancel, unsaved);
    function changed() {
      state.text = text.value;
      state.eventTime = when.value.trim();
      unsaved.textContent = "未保存";
      A.updateDraft(state.draft, { text: state.text, tags: state.tags, eventTime: state.eventTime }, state.baseVersion);
    }
    function finish() {
      if (state.item) M.editing.delete(state.item.id); else M.adding = false;
      render();
    }
    text.addEventListener("input", changed);
    when.addEventListener("input", changed);
    form.append(text, tags, when, actions);
    if (!state.item) form.prepend(A.el("div", "fa-note", "手动记录默认已确认，标为“用户记录”。"));
    return form;
  }
  function restoreDrafts(drafts) {
    // Restore after the list has loaded, so edits attach to their records.
    M.restore = drafts;
    if (M.loaded && !M.loading) applyDrafts();
  }
  function applyDrafts() {
    const drafts = M.restore;
    M.restore = null;
    if (!drafts) return;
    for (const [entry] of drafts) {
      if (entry === "memory:new") M.adding = editorState(entry, null);
      else if (entry.startsWith("memory:")) {
        const id = entry.slice(7);
        const item = M.confirmed.find(candidate => candidate.id === id);
        if (item) M.editing.set(id, editorState(entry, item));
      }
    }
    render();
  }

  // -- building -------------------------------------------------------------------------------------
  async function openEvidence(item) {
    const body = A.openSheet("原文与上下文", "evidence:" + item.id);
    try {
      const detail = await A.api("/api/friend/memory?" + A.query({ id: item.id }));
      if (A.sheetOwner !== "evidence:" + item.id) return;
      body.append(A.el("p", "fa-text", detail.memory.text));
      if (detail.memory.userCorrected) body.append(A.el("p", "fa-note", "用户修正过内容，以下原文仅供核对。"));
      detail.evidence.forEach((evidence, index) => {
        const block = A.el("div", "fa-section");
        block.append(A.el("h3", "", `${roleWords[evidence.role] || "发言"} · ${A.formatDate(evidence.timeMs, true)}` +
          (evidenceWords[evidence.status] ? " · " + evidenceWords[evidence.status] : "")));
        const context = A.el("div");
        context.append(A.el("div", "fa-msg-text", evidence.excerpt));
        block.append(context);
        // The first source opens with its context; others load on request.
        if (index === 0) void showContext(item, evidence, context);
        else {
          const more = A.button("查看上下文", "fa-plain", () => { more.remove(); void showContext(item, evidence, context); });
          block.append(more);
        }
        body.append(block);
      });
    } catch (error) {
      if (A.sheetOwner === "evidence:" + item.id) body.append(A.el("p", "", error.message || "原文读取失败"));
    }
  }
  async function showContext(item, evidence, container) {
    try {
      const data = await A.api("/api/friend/evidence?" + A.query({ id: item.id, ordinal: String(evidence.ordinal) }));
      if (A.sheetOwner !== "evidence:" + item.id || !data.messages.length) return;
      container.replaceChildren();
      for (const message of data.messages) {
        const row = A.el("div", "fa-msg" + (message.target ? " target" : ""));
        row.append(A.el("div", "fa-msg-head", `${message.speaker} · ${A.formatDate(message.timeMs, true)}` +
          (message.role === "forward" ? " · 转发" : "") + (message.quote ? " · 含引用" : "")));
        row.append(A.el("div", "fa-msg-text", message.media ? `[${message.media.kind === "voice" ? "语音" : "媒体"}，内容未在此加载]` : message.text));
        container.append(row);
      }
      if (data.locatable && window.friendHost?.locate && !A.restricted) {
        container.append(A.button("在聊天中定位", "fa-secondary", async () => {
          try {
            const target = await A.api("/api/friend/locate", { method: "POST", body: { id: item.id, ordinal: evidence.ordinal } });
            const ok = await window.friendHost.locate(target);
            if (!ok) A.toast("未能定位，请确认主窗口仍是同一账号");
          } catch (error) { A.toast(error.message || "未能定位"); }
        }));
      }
    } catch { }
  }

  window.FriendMemory = {
    load, render, showIgnored, startAdd, clear: clearAll, restoreDrafts,
    setOverview(overview) {
      const previous = M.overview;
      M.overview = overview;
      const signature = value => JSON.stringify([value.history.state, value.memory]);
      if (!previous || previous.memoryVersion !== overview.memoryVersion) void load();
      else if (signature(previous) !== signature(overview)) render();
    },
  };
})();
