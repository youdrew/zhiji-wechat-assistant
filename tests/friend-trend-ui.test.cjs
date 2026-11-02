// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic statistics exercise the actual sandbox page and trend arithmetic, without chat data.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tagName = "") {
    Object.assign(this, { tagName, className: "", textContent: "", children: [], style: {}, listeners: {},
      attributes: {}, scrollLeft: 0, clientWidth: 640 });
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  append(...items) { this.children.push(...items.map(item => typeof item === "string" ?
    Object.assign(new Element("#text"), { textContent: item }) : item)); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  get scrollWidth() { return Math.max(this.clientWidth, parseFloat(this.children[0]?.style.width) || 0); }
  getContext() { return new Proxy({}, { get: (target, key) => key in target ? target[key] : () => {} }); }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const byClass = (root, name) => descendants(root).find(node => node.className === name);
const findButton = (root, label) => descendants(root).find(node => node.tagName === "button" && node.textContent === label);
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let index = 0; index < 4; index++) await new Promise(resolve => setImmediate(resolve)); };
async function dispatch(node, type, extra = {}) {
  assert.ok(node, `missing ${type} control`);
  await Promise.all((node.listeners[type] || []).map(listener => listener({ target: node, ...extra })));
}

function syntheticTrend() {
  const times = [new Date(2026, 8, 30, 10).getTime(), new Date(2026, 9, 2, 9).getTime(),
    ...Array.from({ length: 8 }, (_, index) => new Date(2026, 9, 14, 10, index).getTime())];
  return { cutoffMs: new Date(2026, 9, 14, 15, 26).getTime(), dataRevision: 10,
    timezone: "Synthetic", utcOffsetMinutes: 480, kinds: ["text", "image"], insufficient: [],
    history: { complete: true, firstMessageMs: times[0], lastMessageMs: times.at(-1), readCount: times.length },
    coverage: { unknownDirection: 0, untimed: 0, future: 0, ambiguousSameTime: 0 },
    messages: { time: times.map((time, index) => time - (times[index - 1] || 0)),
      side: times.map((_, index) => index % 2), chars: times.map((_, index) => index + 1),
      kind: times.map(() => 0), flags: times.map(() => 1) },
    replies: times.slice(3).map((_, index) => [index + 3, 60 * (index + 1)]) };
}

function harness({ save = async () => ({ saved: true }), read = async () => ({}), router = false, paint = false } = {}) {
  const nodes = new Map(), calls = [], data = syntheticTrend(), frames = new Map();
  let nextFrame = 0;
  let receive;
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = { console, Date, setTimeout: () => 1, clearTimeout() {},
    requestAnimationFrame: callback => { const id = ++nextFrame; if (paint) frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id), ResizeObserver: class { observe() {} disconnect() {} }, addEventListener() {},
    getComputedStyle: () => ({ fontFamily: "sans-serif", getPropertyValue: () => "" }),
    document: { getElementById: byId, createElement: tag => new Element(tag), body: new Element("body") },
    kyfHost: { rpc: async method => { assert.equal(method, "page.trend"); return data; },
      host: async (method, params) => {
        calls.push({ method, params: params === undefined ? undefined : plain(params) });
        if (method === "drafts.list") return { drafts: [], closedGenerations: {} };
        if (method === "trend.settings.get") return read();
        assert.equal(method, "trend.settings");
        return save(params);
      }, on: listener => { receive = listener; }, context: () => ({ account: "synthetic-account" }) } };
  Object.defineProperty(context, "localStorage", { get() {
    const error = new Error("Access is denied for this sandboxed document");
    error.name = "SecurityError";
    throw error;
  } });
  context.window = context;
  vm.createContext(context);
  vm.runInContext(readFileSync(path.join(__dirname, "../plugin-sdk/web/time-trend.js"), "utf8"), context, { filename: "time-trend.js" });
  const files = ["page-core.js", "friend-trend-math.js", "friend-trend.js", ...(router ? ["page.js"] : [])];
  for (const file of files) vm.runInContext(readFileSync(path.join(__dirname,
    "../plugins/kyf.friend-analysis/ui", file), "utf8"), context, { filename: file });
  const root = byId("pageTrend");
  return { context, root, calls, data, trend: context.FriendTrend,
    paint: () => { const pending = [...frames]; frames.clear(); for (const [, callback] of pending) callback(); },
    message: message => receive(message), click: label => dispatch(findButton(root, label), "click"),
    selected: label => findButton(root, label)?.getAttribute("aria-pressed") === "true",
    title: () => byClass(root, "fa-trend-title")?.textContent,
    labels: () => descendants(root).filter(node => node.className === "fa-stat-label").map(node => node.textContent),
    checkbox: () => descendants(root).find(node => node.tagName === "input" && node.type === "checkbox") };
}

const UNITS = [["hour", "小时"], ["day", "天"], ["week", "周"], ["month", "月"], ["year", "年"]];
const METRICS = [["overview", "总览", "消息条数"], ["messages", "消息条数", "消息类型"],
  ["turns", "发言轮次", "平均每轮"], ["chars", "文字量", "平均每条文字"], ["reply", "回复时间", "中位数"]];

test("all five granularities and metrics change the rendered trend when sandbox storage is unavailable", async () => {
  const h = harness();
  await h.trend.load();
  assert.equal(h.selected("月"), true);
  for (const [unit, unitLabel] of UNITS) {
    await h.click(unitLabel);
    assert.equal(h.selected(unitLabel), true, `${unitLabel} should remain selected after the click`);
    const M = h.context.FriendTrendMath, view = M.series(M.decode(h.data), unit,
      { fromMs: h.data.history.firstMessageMs, nowMs: h.data.cutoffMs });
    const latest = M.latestWithMessages(view);
    assert.equal(h.title(), M.title(view.bounds[latest], view.bounds[latest + 1], unit));
    for (const [metric, metricLabel, cardLabel] of METRICS) {
      await h.click(metricLabel);
      assert.equal(h.selected(unitLabel), true);
      assert.equal(h.selected(metricLabel), true, `${metricLabel} should remain selected after the click`);
      assert.ok(h.labels().includes(cardLabel), `${metricLabel} should render its statistics`);
      const chart = byClass(h.root, "fa-chart-scroll");
      assert.match(chart.getAttribute("aria-label"), new RegExp(`共 ${view.count} 个周期`));
      assert.deepEqual(h.calls.filter(call => call.method === "trend.settings").at(-1),
        { method: "trend.settings", params: { metric } });
    }
    assert.ok(h.calls.some(call => call.params?.unit === unit));
  }
  await h.trend.load();
  assert.equal(h.selected("年"), true, "ordinary data reload retains granularity");
  assert.equal(h.selected("回复时间"), true, "ordinary data reload retains metric");
});

test("router applies host preferences before the first render and syncs later context updates", async () => {
  const h = harness({ router: true });
  h.message({ type: "context", context: { tab: "trend", theme: "dark",
    trendSettings: { unit: "day", metric: "chars", log: true, zoom: { day: 17 } } } });
  await settle();
  assert.equal(h.selected("天"), true);
  assert.equal(h.selected("文字量"), true);
  h.message({ type: "context", context: { tab: "trend", theme: "light",
    trendSettings: { unit: "week", metric: "reply", log: true, zoom: { day: 17 } } } });
  assert.equal(h.selected("周"), true);
  assert.equal(h.selected("回复时间"), true);
  assert.equal(h.checkbox().checked, true);
  h.message({ type: "event", event: { type: "refresh" } });
  await settle();
  assert.equal(h.selected("周"), true);
  assert.equal(h.selected("回复时间"), true);
  assert.equal(h.checkbox().checked, true);
  assert.equal(h.calls.filter(call => call.method === "trend.settings").length, 0,
    "receiving preferences should not write them back");
});

test("minute drill-down returns to the chosen unit, and reply log scale survives rerenders", async () => {
  const h = harness();
  await h.trend.load();
  await h.click("天");
  const dayTitle = h.title();
  await h.click("按分钟查看这一天");
  assert.ok(findButton(h.root, "返回"));
  assert.equal(findButton(h.root, "天"), undefined);
  await h.click("回复时间");
  const box = h.checkbox();
  assert.equal(box.checked, false);
  box.checked = true;
  await dispatch(box, "change");
  assert.deepEqual(h.calls.filter(call => call.method === "trend.settings").at(-1),
    { method: "trend.settings", params: { log: true } });
  await h.click("返回");
  assert.equal(h.selected("天"), true);
  assert.equal(h.title(), dayTitle);
  assert.equal(h.checkbox().checked, true);
  await h.click("总览");
  await h.click("回复时间");
  assert.equal(h.checkbox().checked, true);
  assert.equal(h.calls.some(call => call.params?.unit === "minute"), false,
    "temporary drill-down should not replace the preferred unit");
});

test("pending saves resist stale host context and failed persistence leaves controls usable", async () => {
  let resolveSave, fail = false;
  const pending = new Promise(resolve => { resolveSave = resolve; });
  let latestHost = { unit: "month", metric: "overview", log: false, zoom: {} };
  const h = harness({ save: () => fail ? Promise.reject(new Error("synthetic persistence failure")) : pending,
    read: async () => latestHost });
  await h.trend.load();
  await h.click("小时");
  await h.click("发言轮次");
  h.trend.settingsChanged({ unit: "month", metric: "overview", log: false, zoom: {} });
  assert.equal(h.selected("小时"), true, "stale context must not overwrite a pending unit save");
  assert.equal(h.selected("发言轮次"), true, "stale context must not overwrite a pending metric save");
  latestHost = { unit: "week", metric: "chars", log: false, zoom: {} };
  h.trend.settingsChanged(latestHost);
  assert.equal(h.selected("小时"), true, "another window's changes wait for this save to finish");
  assert.equal(h.selected("发言轮次"), true);
  resolveSave({ saved: true });
  await settle();
  assert.ok(h.calls.some(call => call.method === "trend.settings.get"));
  assert.equal(h.selected("周"), true, "fresh host state syncs after saves settle");
  assert.equal(h.selected("文字量"), true);
  fail = true;
  await h.click("天");
  await h.click("回复时间");
  await settle();
  await h.trend.load();
  assert.equal(h.selected("天"), true);
  assert.equal(h.selected("回复时间"), true);
  await h.click("小时");
  await h.click("消息条数");
  await settle();
  assert.equal(h.selected("小时"), true);
  assert.equal(h.selected("消息条数"), true);
});

test("an older host read cannot replace a newer context or click", async () => {
  for (const change of ["context", "click"]) {
    const reads = [];
    const h = harness({ read: () => new Promise(resolve => reads.push(resolve)) });
    await h.trend.load();
    await h.click("天");
    await settle();
    assert.equal(reads.length, 1, "the completed save starts a host reconciliation read");
    if (change === "context") {
      h.trend.settingsChanged({ unit: "week", metric: "chars", log: false, zoom: {} });
    } else {
      await h.click("小时");
      await settle();
    }
    reads[0]({ unit: "day", metric: "overview", log: false, zoom: {} });
    await settle();
    assert.equal(h.selected(change === "context" ? "周" : "小时"), true,
      `the old response must preserve the newer ${change}`);
    if (change === "context") assert.equal(h.selected("文字量"), true);
    else {
      assert.equal(reads.length, 2);
      reads[1]({ unit: "hour", metric: "overview", log: false, zoom: {} });
      await settle();
    }
  }
});

test("another window's current-unit zoom context changes actual canvas width without losing the selected range", async () => {
  const h = harness({ router: true, paint: true }), charts = [];
  const original = h.context.KyfTimeTrend.createChart;
  h.context.KyfTimeTrend.createChart = options => { const chart = original(options); charts.push(chart); return chart; };
  const context = zoom => ({ type: "context", context: { tab: "trend", theme: "dark",
    trendSettings: { unit: "hour", metric: "messages", log: false, zoom } } });
  h.message(context({ hour: 10 })); await settle(); h.paint();
  charts[0].select(60); charts[0].select(65, { extend: true }); h.paint();
  const title = h.title(), oldScroller = byClass(h.root, "fa-chart-scroll");
  oldScroller.scrollLeft = 600;
  await dispatch(oldScroller, "scroll");
  const M = h.context.FriendTrendMath, view = M.series(M.decode(h.data), "hour",
    { fromMs: h.data.history.firstMessageMs, nowMs: h.data.cutoffMs });
  assert.equal(parseFloat(byClass(h.root, "fa-chart-track").style.width), 56 + view.count * 10);
  h.message(context({ hour: 20 })); h.paint();
  const scroller = byClass(h.root, "fa-chart-scroll"), track = byClass(h.root, "fa-chart-track");
  assert.notEqual(scroller, oldScroller, "the active canvas receives the changed slot width");
  assert.equal(parseFloat(track.style.width), 56 + view.count * 20);
  assert.equal(scroller.scrollLeft, 1200, "the remembered pan retains the same leftmost period");
  assert.equal(h.title(), title, "the anchor and focus survive the zoom update");
  assert.equal(h.calls.filter(call => call.method === "trend.settings").length, 0,
    "received preferences do not write back or start a synchronization loop");
  h.message(context({ hour: 20, day: 40 })); h.paint();
  assert.equal(byClass(h.root, "fa-chart-scroll"), scroller, "a different unit's zoom does not rebuild the active chart");
  assert.equal(h.title(), title);
});
