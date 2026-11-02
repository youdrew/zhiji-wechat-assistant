// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Both trend surfaces use this interaction path; only synthetic timestamps and values are used.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../plugin-sdk/web/time-trend.js"), "utf8");
const plain = value => JSON.parse(JSON.stringify(value));

function harness({ count = 200, zoom = 8, selection = { anchor: 0, focus: 0 }, scrollLeft = 0,
  unit = "day", series, mode, unknown, unread, hasData, initialIndex, remembered = true } = {}) {
  const frames = new Map(), updates = [], zooms = [], draws = [], observers = [];
  let nextFrame = 0;
  const g = new Proxy({}, { get(target, key) {
    if (key in target) return target[key];
    return (...args) => { draws.push({ method: key, args, fill: target.fillStyle, stroke: target.strokeStyle }); };
  } });
  class Element {
    constructor(tag) {
      Object.assign(this, { tagName: tag, children: [], listeners: {}, style: {}, attributes: {}, clientWidth: 640, _scrollLeft: 0 });
      this.classList = { add() {}, remove() {} };
    }
    append(...nodes) { this.children.push(...nodes); }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    get scrollWidth() { return Math.max(this.clientWidth, parseFloat(this.children[0]?.style.width) || 0); }
    get scrollLeft() { return this._scrollLeft; }
    set scrollLeft(value) { this._scrollLeft = Math.max(0, Math.min(this.scrollWidth - this.clientWidth, value)); }
    getBoundingClientRect() { return { left: 0, top: 0 }; }
    getContext() { return g; }
    focus(options) { this.focused = options; }
    setPointerCapture(pointerId) { this.pointerId = pointerId; }
  }
  class Observer {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  const context = { console, Date, Math, Float64Array, Number, Map, Set,
    document: { createElement: tag => new Element(tag), documentElement: new Element("html"), body: new Element("body") },
    devicePixelRatio: 2, ResizeObserver: Observer, MutationObserver: Observer,
    getComputedStyle: () => ({ fontFamily: "sans-serif", getPropertyValue: name => ({ "--self": "green", "--other": "blue",
      "--select": "selection", "--line": "line", "--faint": "faint" })[name] || "" }),
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id) };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  const calendar = context.KyfTimeTrend.calendar;
  const bounds = [new Date(2026, 0, 1).getTime()];
  for (let index = 0; index < count; index++) bounds.push(calendar.next(bounds.at(-1), unit));
  const chart = context.KyfTimeTrend.createChart({ view: { count, bounds, unit, inProgress: false, unread },
    unit, series: series || [{ values: Array.from({ length: count }, (_, index) => index + 1), color: "--self" }],
    mode, unknown, hasData, initialIndex, label: "合成用量", zoom, key: "synthetic", scroll: new Map(remembered ? [["synthetic", scrollLeft]] : []),
    getSelection: () => selection, setSelection: (next, options) => { selection = next; updates.push({ selection: plain(next), ...options }); },
    onZoom: (name, slot) => zooms.push({ name, slot }) });
  const [canvas, scroller] = chart.element.children, [track] = scroller.children;
  const frame = () => { const pending = [...frames]; for (const [id, callback] of pending) { frames.delete(id); callback(); } };
  const dispatch = (type, extra = {}) => {
    const event = { button: 0, clientX: 100, clientY: 80, pointerId: 1, shiftKey: false,
      deltaX: 0, deltaY: 0, deltaMode: 0, type, prevented: false,
      preventDefault() { this.prevented = true; }, ...extra };
    for (const listener of scroller.listeners[type] || []) listener(event);
    return event;
  };
  chart.draw(); frame();
  return { chart, context, canvas, scroller, track, updates, zooms, draws, observers, frames, frame, dispatch,
    selection: () => plain(selection), slot: () => (parseFloat(track.style.width) - 44 - 12) / count };
}

test("click, Shift click and keyboard move the focus while preserving the selection anchor", () => {
  const h = harness();
  assert.equal(h.chart.isSelecting(), false);
  h.dispatch("pointerdown", { clientX: 64 });
  assert.equal(h.chart.isSelecting(), true);
  assert.equal(h.updates.at(-1).preview, true);
  h.dispatch("pointerup", { clientX: 64 });
  assert.equal(h.chart.isSelecting(), false);
  assert.deepEqual(h.selection(), { anchor: 2, focus: 2 });
  assert.equal(h.updates.at(-1).preview, false);
  h.dispatch("pointerdown", { clientX: 88, shiftKey: true });
  h.dispatch("pointerup", { clientX: 88, shiftKey: true });
  assert.deepEqual(h.selection(), { anchor: 2, focus: 5 });
  h.dispatch("keydown", { key: "ArrowRight", shiftKey: true });
  assert.deepEqual(h.selection(), { anchor: 2, focus: 6 });
  h.dispatch("keydown", { key: "ArrowLeft" });
  assert.deepEqual(h.selection(), { anchor: 5, focus: 5 });
  h.dispatch("keydown", { key: "End", shiftKey: true });
  assert.deepEqual(h.selection(), { anchor: 5, focus: 199 });
  assert.ok(h.scroller.scrollLeft > 0, "keyboard selection reveals the far endpoint");
  h.dispatch("keydown", { key: "Home" });
  assert.deepEqual(h.selection(), { anchor: 0, focus: 0 });
  assert.equal(h.scroller.scrollLeft, 0);
});

test("drag selection previews without committing requests and extends while an edge scrolls", () => {
  const h = harness();
  h.dispatch("pointerdown", { clientX: 64 });
  h.dispatch("pointermove", { clientX: 704 });
  assert.equal(h.chart.isSelecting(), true);
  assert.equal(h.scroller.pointerId, 1);
  assert.ok(h.updates.every(update => update.preview), "dragging only produces preview callbacks");
  const before = h.selection().focus;
  h.frame();
  assert.ok(h.scroller.scrollLeft > 0, "the edge scrolls in a frame");
  assert.ok(h.selection().focus > before, "the moving edge includes the newly revealed periods");
  h.dispatch("pointerup", { clientX: 704 });
  assert.equal(h.chart.isSelecting(), false);
  assert.deepEqual(h.selection().anchor, 2);
  assert.equal(h.updates.at(-1).preview, false);
  const selected = h.selection();
  h.dispatch("pointerdown", { clientX: 120 });
  h.dispatch("pointercancel", { clientX: 120 });
  assert.equal(h.chart.isSelecting(), false);
  assert.deepEqual(h.selection(), selected, "a cancelled touch click restores the selection");
  h.chart.destroy();
  assert.equal(h.frames.size, 0, "destroy stops paint and edge-animation frames");
});

test("wheel zoom preserves the cursor's period and selection, and native horizontal gestures remain native", () => {
  const h = harness({ scrollLeft: 300, selection: { anchor: 48, focus: 50 } });
  const cursor = 240;
  const at = (h.scroller.scrollLeft + cursor - 44) / h.slot();
  const event = h.dispatch("wheel", { clientX: cursor, deltaY: -100 });
  assert.equal(event.prevented, true);
  const exactSlot = h.zooms.at(-1).slot;
  assert.ok(Math.abs((h.scroller.scrollLeft + cursor - 44) / exactSlot - at) < 1e-9);
  assert.deepEqual(h.selection(), { anchor: 48, focus: 50 });
  assert.equal(h.zooms.at(-1).name, "day");
  const savedWidth = h.track.style.width;
  assert.equal(h.dispatch("wheel", { shiftKey: true, deltaY: 100 }).prevented, false);
  assert.equal(h.dispatch("wheel", { deltaX: 100, deltaY: 2 }).prevented, false);
  assert.equal(h.track.style.width, savedWidth);
  h.dispatch("wheel", { clientX: cursor, deltaY: 100000 });
  assert.equal(h.zooms.at(-1).slot, 2, "zoom stops at the same minimum slot as relationship trends");
  h.dispatch("wheel", { clientX: cursor, deltaY: -100000 });
  assert.equal(h.zooms.at(-1).slot, 60);
});

test("canvas draws reported stacks, marks unknown usage, and resizes and repaints with the theme", () => {
  const h = harness({ count: 4, zoom: 60, series: [{ values: [2, null, NaN, 4], color: "var(--self)" },
    { values: [3, null, NaN, 1], color: "--absent-color" }], unknown: index => index === 1 || index === 3 });
  assert.equal(h.canvas.width, 1280);
  assert.equal(h.canvas.height, 360);
  const bars = h.draws.filter(draw => draw.method === "fillRect" && ["green", "#8a8a8a"].includes(draw.fill));
  assert.equal(bars.length, 4, "null and NaN samples have no zero-height bar");
  assert.equal(h.draws.filter(draw => draw.method === "strokeRect").length, 2,
    "unknown totals have a marker even where the reported part is available");
  h.scroller.clientWidth = 700;
  h.observers[0].callback(); h.frame();
  assert.equal(h.canvas.width, 1400);
  const before = h.draws.length;
  h.observers[1].callback(); h.frame();
  assert.ok(h.draws.length > before, "a theme change repaints without rebuilding the chart");
  h.chart.destroy();
  assert.ok(h.observers.every(observer => observer.disconnected));
  h.chart.draw();
  assert.equal(h.frames.size, 0);
});

test("grouped reply bars retain separate sides and nonfinite samples stay absent", () => {
  const h = harness({ count: 3, zoom: 60, mode: "reply", series: [{ values: [60, NaN, 180], color: "--self" },
    { values: [120, NaN, 300], color: "--other" }] });
  const bars = h.draws.filter(draw => draw.method === "fillRect" && ["green", "blue"].includes(draw.fill));
  assert.equal(bars.length, 4);
  assert.equal(bars[0].fill, "green");
  assert.equal(bars[1].fill, "blue");
  assert.ok(bars[1].args[0] > bars[0].args[0], "self remains left and the other person remains right");
});

test("full-day minute and hour range titles distinguish midnight on the following date", () => {
  const M = harness().context.KyfTimeTrend.calendar;
  const start = new Date(2026, 0, 1).getTime(), end = new Date(2026, 0, 2).getTime();
  for (const unit of ["minute", "hour"])
    assert.equal(M.title(start, end, unit), "2026-01-01 00:00 至 01-02 00:00");
  assert.equal(M.title(new Date(2026, 11, 31).getTime(), new Date(2027, 0, 1).getTime(), "minute"),
    "2026-12-31 00:00 至 2027-01-01 00:00");
  assert.equal(M.title(new Date(2026, 0, 1, 12).getTime(), new Date(2026, 0, 1, 14).getTime(), "minute"),
    "2026-01-01 12:00–14:00");
});

test("minute initial viewport can reveal activity without changing the all-day selection or remembered pan", () => {
  const selection = { anchor: 0, focus: 1439 };
  const first = harness({ count: 1440, unit: "minute", selection, initialIndex: 600, remembered: false });
  const at = 44 + 600 * 8 - first.scroller.scrollLeft;
  assert.ok(at >= 44 && at + 8 <= first.scroller.clientWidth, "initial activity is visible");
  assert.deepEqual(first.selection(), selection);
  assert.equal(first.updates.length, 0, "viewport placement never commits a new summary range");
  const kept = harness({ count: 1440, unit: "minute", selection, initialIndex: 600, scrollLeft: 1000 });
  assert.equal(kept.scroller.scrollLeft, 1000, "a user's remembered pan wins over both initial activity and selection focus");
  assert.deepEqual(kept.selection(), selection);
  const legacy = harness({ selection: { anchor: 199, focus: 199 }, scrollLeft: 0 });
  assert.ok(legacy.scroller.scrollLeft > 0, "without initialIndex the existing relationship selection-reveal behavior is preserved");
});
