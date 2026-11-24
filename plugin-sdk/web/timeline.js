"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared timeline overview and time input for 自我剖析 and 知己分析 (added 2026-11-07), moved out of
// kyf.self-analysis/ui/page.js. "grid" mode is 自我剖析's track × period grid; "dots" mode draws tracks as rows
// over a time axis with events as dots, important ones as ★, periods as thin lines and a thin chat-frequency line.
// The caller passes data, rows, colours and callbacks; this file keeps no state and makes no requests.
(function (root) {
  const COLORS = ["#07c160", "#576b95", "#c2841c", "#b05a4a", "#7f77dd", "#3d8f8a", "#9a6fb0", "#7b8a4e"];
  const SVG = "http://www.w3.org/2000/svg";
  const hasMonth = event => ["day", "month", "season"].includes(event.precision);
  const isImportant = event => event.importance === "important";
  const fullDate = ms => new Date(ms).toLocaleDateString("zh-CN");
  function element(tag, className, text) { const node = document.createElement(tag); node.className = className; node.textContent = text; return node; }

  // 分隔符不挑：2024.05、2024/5、2024-05、2024年5月、202405 都认；日期要真实存在。
  function parseWhenInput(text) {
    const compact = (text || "").trim().replace(/[./．／。、年月\s]+/g, "-").replace(/[日号]/g, "").replace(/^-+|-+$/g, "");
    const digits = /^(\d{4})(\d{2})(\d{2})?$/.exec(compact);
    const match = digits || /^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$/.exec(compact);
    if (!match) return null;
    const year = +match[1], month = match[2] ? +match[2] : null, day = match[3] ? +match[3] : null;
    if (year < 1970 || year > 2099 || month !== null && (month < 1 || month > 12)) return null;
    if (day !== null) {
      const start = new Date(year, month - 1, day);
      if (start.getMonth() !== month - 1) return null;
      return { startMs: start.getTime(), endMs: new Date(year, month - 1, day + 1).getTime() - 1, precision: "day" };
    }
    if (month !== null) return { startMs: new Date(year, month - 1, 1).getTime(), endMs: new Date(year, month, 1).getTime() - 1, precision: "month" };
    return { startMs: new Date(year, 0, 1).getTime(), endMs: new Date(year + 1, 0, 1).getTime() - 1, precision: "year" };
  }
  function whenInput(ms, precision) {
    if (!Number.isFinite(ms)) return "";
    const day = new Date(ms);
    if (precision === "day") return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    if (["month", "season"].includes(precision)) return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}`;
    return String(day.getFullYear());
  }

  // 时间段号是从公元 0 年算起的第几段（每段 months 个月），相邻两段差 1。
  function bucket(ms, months = 12) { const day = new Date(ms); return Math.floor((day.getFullYear() * 12 + day.getMonth()) / months); }
  function bucketName(value, months = 12, short = false) {
    const start = value * months, year = Math.floor(start / 12), month = start % 12;
    if (months === 12) return String(year);
    if (months > 12) { const last = year + months / 12 - 1; return short ? `${year}–${String(last).slice(2)}` : `${year}–${last}`; }
    if (months === 6) return short ? `${String(year).slice(2)}${month ? "下" : "上"}` : `${year} ${month ? "下" : "上"}半年`;
    return short ? `${String(year).slice(2)}Q${month / 3 + 1}` : `${year} 第${"一二三四"[month / 3]}季度`;
  }
  // 列是时间段；连续三段以上全空的收成一格（null，显示“…”）。
  function columns(value, counted, months = 12) {
    const years = new Set(counted.map(event => bucket(event.startMs, months)));
    for (const period of value.periods || []) if (Number.isFinite(period.startMs)) years.add(bucket(period.startMs, months));
    if (Number.isFinite(value.coverage?.earliestMs)) years.add(bucket(value.coverage.earliestMs, months));
    years.add(bucket(Date.now(), months));
    const first = Math.min(...years), last = Math.max(...years), result = [];
    for (let year = first; year <= last; year++) {
      if (years.has(year)) { result.push(year); continue; }
      let next = year; while (!years.has(next)) next++;
      if (next - year >= 3) { result.push(null); year = next - 1; } else result.push(year);
    }
    return result;
  }

  function hideTip() { document.querySelector(".tl-tip")?.remove(); }
  function showTip(anchor, events) {
    hideTip();
    const tip = element("div", "popover tl-tip", ""); tip.setAttribute("role", "tooltip");
    for (const event of events) {
      const line = element("div", "tl-tip-line", "");
      line.append(element("span", "tl-tip-when", event.whenText), element("span", "", event.title));
      tip.appendChild(line);
    }
    document.body.appendChild(tip);
    const box = anchor.getBoundingClientRect(), { width, height } = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(4, Math.min(box.left + box.width / 2 - width / 2, innerWidth - width - 4))}px`;
    tip.style.top = `${box.top - height - 6 >= 4 ? box.top - height - 6 : box.bottom + 6}px`;
  }
  function withTip(button, items, onEvent) {
    button.setAttribute("aria-label", items.map(event => event.title).join("、"));
    button.addEventListener("mouseenter", () => showTip(button, items));
    button.addEventListener("focus", () => showTip(button, items));
    button.addEventListener("mouseleave", hideTip);
    button.addEventListener("blur", hideTip);
    button.addEventListener("click", () => { hideTip(); onEvent?.(items[0]); });
    return button;
  }

  // ── 格子模式（自我剖析）：主线 × 时间段，深浅表示事件多少，重要事件是叠在格子上的星标 ──
  function gridStars(events, months, onEvent) {
    const slots = new Map();
    for (const event of events) {
      const day = new Date(event.startMs), slot = hasMonth(event) ? (day.getFullYear() * 12 + day.getMonth()) % months : (months - 1) / 2;
      slots.set(slot, [...(slots.get(slot) || []), event]);
    }
    return [...slots].map(([slot, items]) => {
      const star = element("button", "tl-overview-star", items.every(event => event.status === "planned") ? "☆" : "★");
      star.type = "button"; star.style.left = `${(slot + .5) / months * 100}%`;
      return withTip(star, items, onEvent);
    });
  }
  function renderGrid(host, options, value, counted, cols) {
    const { months = 12, selectedTrack = "", colorOf = () => COLORS[0], onTrack, onEvent } = options;
    const cells = new Map();
    for (const event of counted) {
      const key = `${event.track}|${bucket(event.startMs, months)}`;
      const cell = cells.get(key) || { total: 0, important: [] };
      cell.total++; if (isImportant(event)) cell.important.push(event);
      cells.set(key, cell);
    }
    // 深浅分四档，按全图非空格子的分位切，避免一两个大年份把其他格子都压成最浅。
    const sorted = [...cells.values()].map(cell => cell.total).sort((a, b) => a - b);
    const cut = ratio => sorted[Math.floor((sorted.length - 1) * ratio)] || 0;
    const steps = [cut(.25), cut(.5), cut(.75)];
    const level = total => total <= 0 ? 0 : 1 + steps.filter(step => total > step).length;
    const chatYear = Number.isFinite(value.coverage?.earliestMs) ? bucket(value.coverage.earliestMs, months) : null;
    const grid = element("div", "tl-grid", ""), width = months > 12 ? 52 : months < 12 ? 32 : 24;
    grid.style.gridTemplateColumns = `64px ${cols.map(year => year === null ? "16px" : `minmax(${width}px, 1fr)`).join(" ")}`;
    grid.appendChild(element("span", "", ""));
    for (const year of cols) {
      const label = element("span", "tl-year-label" + (year !== null && year === chatYear ? " chat-start" : ""),
        year === null ? "…" : bucketName(year, months, true));
      if (year !== null && year === chatYear) label.title = `聊天记录从 ${fullDate(value.coverage.earliestMs)} 开始`;
      grid.appendChild(label);
    }
    for (const track of options.tracks || []) {
      const dim = selectedTrack && selectedTrack !== track.name ? " dim" : "";
      const label = element("button", "tl-track-label" + dim, track.name); label.type = "button";
      label.title = track.description || track.name;
      label.addEventListener("click", () => onTrack?.(track.name));
      grid.appendChild(label);
      for (const year of cols) {
        if (year === null) { grid.appendChild(element("span", "tl-cell gap" + dim, "")); continue; }
        const cell = cells.get(`${track.name}|${year}`);
        const box = element("button", `tl-cell level-${level(cell?.total || 0)}${dim}`, ""); box.type = "button";
        box.style.setProperty("--track", colorOf(track.name));
        box.title = cell ? `${track.name} · ${bucketName(year, months)}：${cell.total} 件${cell.important.length ? `，重要 ${cell.important.length} 件` : ""}` : `${track.name} · ${bucketName(year, months)}`;
        box.disabled = !cell;
        box.addEventListener("click", () => onTrack?.(track.name, year));
        const slot = element("div", "tl-cell-slot" + dim, "");
        slot.append(box, ...gridStars(cell?.important || [], months, onEvent));
        grid.appendChild(slot);
      }
    }
    host.replaceChildren(grid);
  }

  // ── 圆点模式（知己分析）：横轴按列等分，“…”列窄一些，所有位置都用百分比，宽窗口占满、窄窗口横向滚动 ──
  const GAP_WEIGHT = .4;
  function axis(cols, months) {
    const weights = cols.map(year => year === null ? GAP_WEIGHT : 1), total = weights.reduce((a, b) => a + b, 0) || 1;
    const starts = []; let sum = 0;
    for (const weight of weights) { starts.push(sum / total * 100); sum += weight; }
    const known = cols.map((year, index) => [year, index]).filter(([year]) => year !== null);
    const first = known[0][0], last = known.at(-1)[0];
    // 时间 → 横坐标（%）；落在“…”里的按它收起的段数等分。
    const at = units => {
      const value = units / months;  // in buckets, fractional
      const whole = Math.floor(value), fraction = value - whole;
      if (whole < first) return 0;
      if (whole > last) return 100;
      const index = cols.indexOf(whole);
      if (index >= 0) return starts[index] + fraction * weights[index] / total * 100;
      const after = known.find(([year]) => year > whole), gap = after[1] - 1, before = known[known.indexOf(after) - 1][0];
      return starts[gap] + (value - before - 1) / (after[0] - before - 1) * weights[gap] / total * 100;
    };
    const monthIndex = ms => { const day = new Date(ms); return day.getFullYear() * 12 + day.getMonth(); };
    return {
      starts, widths: weights.map(weight => weight / total * 100), total,
      // 事件：有月份的落在该月中间，只到年的落在那一年中间。
      event: event => hasMonth(event) ? at(monthIndex(event.startMs) + .5) : at(new Date(event.startMs).getFullYear() * 12 + 6),
      time: ms => { const day = new Date(ms); return at(monthIndex(ms) + (day.getDate() - 1) / 31); },
      center: index => starts[index] + weights[index] / total * 50,
    };
  }
  function monthOf(item) {
    if (typeof item?.month === "string") { const match = /^(\d{4})-(\d{1,2})$/.exec(item.month); return match ? new Date(+match[1], +match[2] - 1, 1).getTime() : null; }
    const ms = Number.isFinite(item?.startMs) ? item.startMs : item?.ms;
    return Number.isFinite(ms) ? ms : null;
  }
  function frequencyLine(frequency, cols, ruler, months, value, height) {
    const totals = new Map();
    for (const item of frequency || []) {
      const ms = monthOf(item);
      if (ms !== null && Number.isFinite(item.count) && item.count > 0) totals.set(bucket(ms, months), (totals.get(bucket(ms, months)) || 0) + item.count);
    }
    if (!totals.size) return { svg: null, totals };
    const startBucket = Number.isFinite(value.coverage?.earliestMs) ? bucket(value.coverage.earliestMs, months) : Math.min(...totals.keys());
    const endBucket = Number.isFinite(value.coverage?.latestMs) ? bucket(value.coverage.latestMs, months) : Infinity;
    // 按全图（画出来的列）最大值归一。
    const max = Math.max(0, ...cols.filter(year => year !== null && year >= startBucket && year <= endBucket).map(year => totals.get(year) || 0));
    if (!max) return { svg: null, totals };
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "tl-freq"); svg.setAttribute("viewBox", `0 0 100 ${height}`); svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    let points = [];
    const flush = () => {
      if (points.length) {
        const line = document.createElementNS(SVG, "polyline");
        line.setAttribute("points", (points.length === 1 ? [points[0], points[0]] : points).join(" "));
        line.setAttribute("vector-effect", "non-scaling-stroke");
        svg.appendChild(line);
      }
      points = [];
    };
    cols.forEach((year, index) => {
      // 聊天记录开始之前不画；“…”收起的空段把线断开。
      if (year === null || year < startBucket || year > endBucket) { flush(); return; }
      const count = totals.get(year) || 0;
      points.push(`${ruler.center(index).toFixed(3)},${(1 + (1 - count / max) * (height - 2)).toFixed(3)}`);
    });
    flush();
    return { svg, totals };
  }
  function renderDots(host, options, value, counted, cols) {
    const { months = 12, selectedTrack = "", colorOf = () => COLORS[0], onTrack, onEvent, onPeriod, importantOnly = false } = options;
    const rows = options.tracks || [], single = rows.length <= 1, ruler = axis(cols, months);
    const chatYear = Number.isFinite(value.coverage?.earliestMs) ? bucket(value.coverage.earliestMs, months) : null;
    const rowHeight = 14, rowGap = 3, freqHeight = single ? 26 : rows.length * (rowHeight + rowGap) - rowGap;
    const box = element("div", "tl-dots" + (single ? " single" : ""), "");
    const plot = element("div", "tl-dots-plot", "");
    plot.style.minWidth = `${Math.ceil(ruler.total * (months < 12 ? 30 : 44)) + 20}px`;
    const { svg, totals } = frequencyLine(options.frequency, cols, ruler, months, value, freqHeight);
    const years = element("div", "tl-dots-years", "");
    cols.forEach((year, index) => {
      const label = element("span", "tl-year-label" + (year !== null && year === chatYear ? " chat-start" : ""), year === null ? "…" : bucketName(year, months, true));
      label.style.left = `${ruler.center(index)}%`;
      const tips = [];
      if (year !== null && year === chatYear) tips.push(`聊天记录从 ${fullDate(value.coverage.earliestMs)} 开始`);
      if (year !== null && totals.get(year)) tips.push(`${months === 12 ? "这一年" : bucketName(year, months)}聊了 ${totals.get(year)} 条`);
      if (tips.length) label.title = tips.join("\n");
      years.appendChild(label);
    });
    const body = element("div", "tl-dots-body", "");
    body.style.height = `${single ? freqHeight + rowHeight : freqHeight}px`;
    if (svg) { if (single) svg.style.height = `${freqHeight}px`; body.appendChild(svg); }
    const labels = element("div", "tl-dots-labels", "");
    const memory = (options.memoryRefs || []).filter(ref => Number.isFinite(ref.startMs)).map(ref => ({ ...ref, memoryRef: true, track: rows[0]?.name }));
    rows.forEach((track, index) => {
      const dim = selectedTrack && selectedTrack !== track.name ? " dim" : "";
      if (!single) {
        const label = element("button", "tl-track-label" + dim, track.name); label.type = "button";
        label.title = track.description || track.name;
        label.addEventListener("click", () => onTrack?.(track.name));
        labels.appendChild(label);
      }
      const line = element("div", "tl-dots-row" + dim, "");
      line.style.top = `${single ? freqHeight : index * (rowHeight + rowGap)}px`;
      line.style.setProperty("--tl-color", colorOf(track.name));
      if (single) line.appendChild(element("span", "tl-dots-base", ""));
      for (const period of (value.periods || []).filter(period => period.track === track.name && Number.isFinite(period.startMs))) {
        const endMs = Number.isFinite(period.endMs) ? period.endMs : period.ongoing && Number.isFinite(period.lastMs) ? period.lastMs : period.startMs;
        const left = ruler.time(period.startMs), right = Math.max(left, ruler.time(endMs));
        const bar = element("button", "tl-dots-period", ""); bar.type = "button";
        bar.style.left = `${left}%`; bar.style.width = `${right - left}%`;
        bar.title = `${period.title}${period.whenText ? " · " + period.whenText : ""}`;
        bar.setAttribute("aria-label", `时期：${period.title}`);
        bar.addEventListener("click", () => { hideTip(); onPeriod?.(period); });
        line.appendChild(bar);
      }
      // 同一行同一个月的并成一个标记，有重要的就画 ★；只到年的按年并。
      const slots = new Map();
      for (const event of [...counted.filter(event => event.track === track.name), ...(index === 0 && !importantOnly ? memory : [])]) {
        const day = new Date(event.startMs), key = hasMonth(event) ? `m${day.getFullYear() * 12 + day.getMonth()}` : `y${day.getFullYear()}`;
        slots.set(key, [...(slots.get(key) || []), event]);
      }
      for (const items of slots.values()) {
        const important = items.some(isImportant), own = items.filter(event => !event.memoryRef);
        const classes = ["tl-dot", important ? "important" : "", items.every(event => event.status === "planned") ? "planned" : "",
          own.length && own.every(event => event.status === "uncertain") ? "uncertain" : "", own.length ? "" : "memory"].filter(Boolean);
        const mark = element("button", classes.join(" "), important ? (items.every(event => event.status === "planned") ? "☆" : "★") : "");
        mark.type = "button"; mark.style.left = `${ruler.event(items[0])}%`;
        line.appendChild(withTip(mark, items, onEvent));
      }
      body.appendChild(line);
    });
    if (single) plot.append(body, years); else plot.append(years, body);
    if (!single) box.appendChild(labels);
    box.appendChild(plot);
    host.replaceChildren(box);
    // 打开时滚到最右（最近）。
    if (options.scrollToEnd !== false && typeof host.scrollWidth === "number") host.scrollLeft = host.scrollWidth;
  }

  // options: { mode: "grid" | "dots", value, tracks, months, importantOnly, selectedTrack, colorOf, onTrack, onEvent,
  //   onPeriod, frequency, memoryRefs, scrollToEnd }. Returns true when a chart was drawn. memoryRefs are drawn as
  //   read-only dots on the first row and reach onEvent with ``memoryRef: true``.
  function renderOverview(host, options) {
    hideTip();
    const value = options.value, mode = options.mode || "grid";
    const counted = value ? value.events.filter(event => Number.isFinite(event.startMs) && event.status !== "cancelled" &&
      (!options.importantOnly || isImportant(event))) : [];
    const empty = !value || (mode === "grid" ? !value.periods.length && !value.events.length :
      !value.events.some(event => Number.isFinite(event.startMs) && event.status !== "cancelled") &&
      !value.periods.some(period => Number.isFinite(period.startMs)) && !(options.memoryRefs || []).some(ref => Number.isFinite(ref.startMs)));
    if (empty || mode === "dots" && !(options.tracks || []).length) { host.replaceChildren(); host.hidden = true; return false; }
    host.hidden = false;
    const cols = columns(value, mode === "dots" ? [...counted, ...(options.memoryRefs || []).filter(ref => Number.isFinite(ref.startMs))] : counted, options.months || 12);
    (mode === "dots" ? renderDots : renderGrid)(host, options, value, counted, cols);
    return true;
  }

  root.KyfTimeline = { COLORS, hasMonth, isImportant, parseWhenInput, whenInput, bucket, bucketName, columns, showTip, hideTip, renderOverview };
})(typeof window !== "undefined" ? window : globalThis);
