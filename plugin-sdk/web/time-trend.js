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
