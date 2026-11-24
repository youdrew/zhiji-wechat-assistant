// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic tests of the 时间线 tab of the 知己分析 scheme page; page.timeline* RPCs are mocked, no chat data is read.
// The mock shapes follow tests/fixtures/friend-timeline-page.json, real backend output (friend_timeline_view) on a
// synthetic chat, which the last test also renders as it is.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const pluginRoot = path.join(__dirname, "../plugins/kyf.friend-analysis");
const sdk = readFileSync(path.join(__dirname, "../plugin-sdk/web/timeline.js"), "utf8");
const fixture = JSON.parse(readFileSync(path.join(__dirname, "fixtures/friend-timeline-page.json"), "utf8"));

const scrolls = [];
class Element {
  constructor(tagName = "") {
    Object.assign(this, { tagName, className: "", textContent: "", children: [], listeners: {}, attributes: {}, hidden: false,
      disabled: false, value: "", checked: false, parent: null, title: "", scrollLeft: 0, scrollWidth: 900, scrolled: 0 });
    const style = {}; style.setProperty = (name, value) => { style[name] = value; }; this.style = style;
  }
  append(...items) { for (const item of items) { if (typeof item === "string") continue; item.parent = this; this.children.push(item); } }
  appendChild(item) { this.append(item); return item; }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  replaceWith(...items) {
    if (!this.parent) return;
    const list = this.parent.children, at = list.indexOf(this);
    for (const item of items) item.parent = this.parent;
    list.splice(at, 1, ...items);
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); this.removed = true; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === "class") this.className = String(value); }
  getBoundingClientRect() { return { left: 10, top: 10, bottom: 40, width: 100, height: 30 }; }
  scrollIntoView() { this.scrolled++; scrolls.push(this); }
  focus() {} select() {}
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const has = (node, name) => typeof node.className === "string" && node.className.split(" ").includes(name);
const all = (root, name) => descendants(root).filter(node => has(node, name));
const text = node => [node.textContent, ...node.children.map(text)].join("");
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let index = 0; index < 12; index++) await new Promise(resolve => setImmediate(resolve)); };
const hiddenUp = node => { for (let current = node; current; current = current.parent) if (current.hidden) return true; return false; };

const at = (year, month, day = 1) => new Date(year, month - 1, day).getTime();
const NOW = at(2026, 6, 1);
const memory = (id, text) => ({ id, text, eventTime: null });
function event(id, title, startMs, extra = {}) {
  return { id, title, track: "我们", kind: "point", status: "occurred", importance: "general", startMs, endMs: startMs, precision: "month",
    whenText: "合成时间", phrase: "", saidMs: null, place: "", people: "", reason: "", level: "possible", manual: false, inferred: false,
    details: [], edited: [], evidenceCount: 1, conflicts: [], category: "见面聚会", note: null, source: "timeline", memory: null,
    memories: [], agentEditedMs: null, readOnly: false, ...extra };
}
const memoryRef = (memoryId, text, startMs, extra = {}) => ({ id: "memory:" + memoryId, memoryId, title: text.slice(0, 40), text, track: "我们",
  category: "其他", kind: "point", status: "occurred", importance: "general", startMs, endMs: startMs, precision: "month", whenText: "合成时间",
  readOnly: true, evidenceCount: 0, ...extra });
const idle = { state: "idle", runState: null, detail: "", done: null, total: null, error: null, analyzed: false };
const done = { ...idle, state: "done", runState: "done", analyzed: true };
function view(extra = {}) {
  return { account: "synthetic-account", user: "wxid_synthetic", version: 1, eventsTotal: 3, nextOffset: null, run: done,
    categories: ["见面聚会", "一起出行", "一起做的事", "同学共事", "互相帮忙", "礼物纪念", "情感", "其他"],
    tracks: [{ name: "我们", color: "#07c160", visible: true, builtin: true }],
    coverage: { earliestMs: at(2019, 3, 5), latestMs: at(2026, 5, 20) },
    events: [event("e1", "第一次见面", at(2019, 4, 3)), event("e2", "一起去日本", at(2023, 7, 1), { importance: "important", category: "一起出行" }),
      event("e3", "吵架又和好", at(2023, 9, 2), { category: "情感", level: "confirmed", memory: memory("m1", "我们 2023 年吵过一次"),
        memories: [memory("m1", "我们 2023 年吵过一次")] })],
    periods: [], memoryRefs: [], frequency: [{ month: "2023-07", count: 40 }, { month: "2024-01", count: 12 }],
    undo: { user: { count: 0, label: null }, agent: { count: 0, label: null, items: [] } }, ...extra };
}

function harness(handler, { restricted = false } = {}) {
  const nodes = new Map(), calls = [], hosts = [], windowListeners = {};
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element("section")); return nodes.get(id); };
  const body = new Element("body");
  const Clock = class extends Date { static now() { return NOW; } };
  const context = { console, JSON, Promise, Date: Clock, setTimeout: () => 0, clearTimeout() {}, innerWidth: 800, innerHeight: 600,
    addEventListener: (name, listener) => { (windowListeners[name] ||= []).push(listener); },
    document: { getElementById: byId, createElement: tag => new Element(tag), createElementNS: (space, tag) => new Element(tag), body,
      activeElement: null, querySelector: selector => selector === ".tl-tip" ? body.children.find(item => has(item, "tl-tip")) || null : null },
    kyfHost: {
      rpc: async (method, params) => { calls.push({ method, params: params === undefined ? undefined : plain(params) }); return handler(method, params); },
      host: async (method, params) => { hosts.push({ method, params: plain(params) }); return {}; }, on() {},
      context: () => ({ account: "synthetic-account", name: "小林", restricted }) } };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(sdk, context, { filename: "timeline.js" });
  for (const file of ["page-core.js", "friend-timeline.js"])
    vm.runInContext(readFileSync(path.join(pluginRoot, "ui", file), "utf8"), context, { filename: file });
  const root = byId("pageTimeline");
  return { root, body, calls, hosts, timeline: context.FriendTimeline,
    edits: () => calls.filter(call => call.method !== "page.timeline" && call.method !== "page.timelineEvidence"),
    button(scope, label) { return descendants(scope).find(node => node.tagName === "button" && node.textContent === label && !hiddenUp(node)); },
    async click(node) { assert.ok(node, "missing button"); for (const listener of node.listeners.click) await listener({ target: node }); await settle(); },
    async key(init) {
      const event = { key: "z", ctrlKey: true, target: { closest: () => null }, preventDefault() { event.prevented = true; }, ...init };
      for (const listener of windowListeners.keydown || []) listener(event);
      await settle();
      return event;
    },
    async expandAll() { let node; while ((node = this.button(root, "展开全部事件"))) await this.click(node); },
    rows: () => all(root, "tl-event").filter(node => !hiddenUp(node)).map(node => text(all(node, "tl-title")[0])),
    row: title => all(root, "tl-event").find(node => text(all(node, "tl-title")[0]) === title),
    part: name => all(root, name)[0] };
}
const line = (side, text, time = null) => ({ side, speaker: side === "self" ? "我" : "ta", text, time });
const proof = (messageId, side, text, time, extra = {}) => ({ messageId, time, text, side, speaker: side === "self" ? "我" : "ta", phrase: "",
  truncated: false, source: "timeline", status: "ok", before: [], after: [], ...extra });
// A small in-memory stand-in for page.timeline / page.timelineEdit / page.timelineUndo* / page.timelineEvidence.
function backend(data = view()) {
  const state = { data };
  const handler = async (method, params) => {
    if (method === "page.timeline") return plain(state.data);
    if (method === "page.timelineEvidence") {
      if (params.id.startsWith("memory:"))
        return { id: params.id, total: 1, phrase: "", resolvedWhen: "合成时间", memory: memory(params.id.slice(7), "合成记忆"), memories: [],
          items: [proof("msg-9", "other", "杭州那次真好玩", at(2021, 5, 2), { source: "memory" })] };
      return { id: params.id, total: 1, phrase: "", resolvedWhen: "合成时间", memory: null, memories: [],
        items: [proof("msg-1", "other", "好，一起去", at(2023, 7, 1), { before: [line("self", "去日本吗", at(2023, 7, 1))] })] };
    }
    if (method === "page.timelineEdit") {
      assert.equal(params.expectedVersion, state.data.version);
      const target = state.data.events.find(item => item.id === params.id);
      if (params.target === "event" && params.action === "edit") Object.assign(target, params.fields);
      if (params.target === "event" && params.action === "delete") state.data.events = state.data.events.filter(item => item !== target);
      if (params.target === "event" && params.action === "add") state.data.events.push(event("manual-1", params.fields.title, params.fields.startMs, { ...params.fields, manual: true }));
      if (params.target === "track" && params.action === "add") state.data.tracks.push({ name: params.fields.title, color: params.fields.color, visible: true, builtin: false });
      if (params.target === "track" && params.action === "visible") state.data.tracks.find(item => item.name === params.id).visible = params.fields.visible;
      if (params.action === "unlink") Object.assign(target, { memory: null, memories: [] });
      state.data.version++;
      return { ...plain(state.data), editedId: params.id || "manual-1", editLabel: "修改" };
    }
    if (method === "page.timelineUndo") { state.data.version++; return { ...plain(state.data), undone: "修改“一起去日本”", skipped: null }; }
    if (method === "page.timelineUndoAgent") {
      state.data.version++;
      state.data.undo.agent = { count: 1, label: "添加“看展”", items: [{ seq: 3, label: "添加“看展”", agent: "合成 Agent", updatedMs: NOW }] };
      return { ...plain(state.data), undone: "修改“第一次见面”", skipped: null };
    }
    throw new Error("unexpected " + method);
  };
  return { state, handler };
}

test("the first read hides the controls and shows real page progress; a reload does not flash", async () => {
  const first = view({ eventsTotal: 3, nextOffset: 1 });
  const pages = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const h = harness(async (method, params) => {
    pages.push(params?.offset ?? 0);
    if (!params?.offset) { if (pages.length === 1) await gate; return { ...plain(first), events: first.events.slice(0, 1), nextOffset: pages.length > 2 ? null : 1 }; }
    if (pages.length === 2) { const state = h.part("tl-state"); assert.equal(state.hidden, false); assert.equal(text(state).includes("正在读取… 1 / 3 条"), true); }
    return { ...plain(first), events: first.events.slice(1), nextOffset: null };
  });
  const loading = h.timeline.load();
  await settle();
  const state = h.part("tl-state");
  assert.equal(state.hidden, false); assert.equal(h.part("tl-top").hidden, true);
  assert.ok(has(h.part("tl-progress"), "indeterminate"), "no made-up percentage before the first page");
  assert.equal(h.part("tl-state").children[1].textContent, "正在读取…");
  release(); await loading; await settle();
  assert.equal(state.hidden, true); assert.equal(h.part("tl-top").hidden, false);
  assert.deepEqual(pages, [0, 1]);
  assert.deepEqual(h.rows().sort(), ["一起去日本"], "a year shows its important and planned events");
  // Coming back to the tab reads again quietly: no loading state over the content.
  void h.timeline.load();
  assert.equal(state.hidden, true); assert.equal(h.part("tl-top").hidden, false);
  await settle();
});

test("one track draws a single-row dot chart, no track tabs, and the 主线 entry in the top row", async () => {
  const h = harness(backend().handler);
  await h.timeline.load();
  const chart = h.part("tl-dots");
  assert.equal(chart.className, "tl-dots single");
  assert.equal(all(h.root, "tl-track-label").length, 0);
  assert.ok(all(h.root, "tl-freq").length === 1, "the chat-frequency line is drawn");
  assert.equal(h.part("tl-tracks").hidden, true);
  assert.ok(h.button(h.part("tl-top"), "主线"));
  assert.ok(h.button(h.part("tl-top"), "添加"));
  assert.equal(h.part("tl-overview").scrollLeft, 900, "opens scrolled to the most recent end");
  assert.equal(text(h.part("tl-list-marker")), "聊天记录从 2019 年 3 月开始");
  // Category on the right of the title.
  assert.equal(text(all(h.row("一起去日本"), "tl-category")[0]), "一起出行");
  await h.click(h.button(h.part("tl-top"), "主线"));
  assert.ok(all(h.part("tl-manage-host"), "tl-manage-row").length >= 2, "the manager opens under the top row");
});

test("a custom track with events gives a multi-row chart and track tabs; hiding it goes back to one row", async () => {
  const data = view();
  data.tracks.push({ name: "工作", color: "#576b95", visible: true, builtin: false, description: "一起上班的事" });
  data.events.push(event("e4", "一起加班", at(2024, 3, 1), { track: "工作", category: "同学共事" }));
  const { handler } = backend(data);
  const h = harness(handler);
  await h.timeline.load();
  assert.equal(h.part("tl-dots").className, "tl-dots");
  assert.deepEqual(all(h.root, "tl-track-label").map(node => node.textContent), ["我们", "工作"]);
  assert.deepEqual(all(h.part("tl-tabs"), "").filter(node => node.tagName === "button").map(node => node.textContent), ["全部", "我们", "工作"]);
  assert.equal(h.button(h.part("tl-top"), "主线"), undefined, "the entry moves next to the tabs");
  await h.click(h.button(h.part("tl-tracks"), "工作"));
  assert.deepEqual(h.rows(), []);
  await h.click(h.button(h.part("tl-tracks"), "管理主线"));
  const box = all(h.part("tl-tracks"), "tl-manage-row").find(node => text(node).includes("工作"));
  const visible = box.children[0];
  visible.checked = false;
  for (const listener of visible.listeners.change) listener({});
  await settle();
  assert.deepEqual(plain(h.edits().at(-1).params), { target: "track", action: "visible", id: "工作", fields: { visible: false }, expectedVersion: 1 });
  assert.equal(h.part("tl-dots").className, "tl-dots single");
  assert.equal(h.part("tl-tracks").hidden, true);
});

test("clicking a dot opens the event in the list, unfolding its year and loading its sources", async () => {
  const h = harness(backend().handler);
  await h.timeline.load();
  assert.equal(h.row("第一次见面"), undefined, "a general event sits behind 展开全部事件");
  const dot = all(h.root, "tl-dot").find(node => node.attributes["aria-label"] === "第一次见面");
  await h.click(dot);
  const row = h.row("第一次见面");
  assert.ok(row && has(row, "open")); assert.ok(scrolls.some(node => text(node).includes("第一次见面")), "scrolled into view");
  assert.ok(h.calls.some(call => call.method === "page.timelineEvidence" && call.params.id === "e1"));
  assert.deepEqual(all(row, "tl-bubble").map(node => node.textContent), ["去日本吗", "好，一起去"]);
});

test("the user edits importance, time, category and deletion; Ctrl+Z undoes only the user's own edits", async () => {
  const { handler } = backend();
  const h = harness(handler);
  await h.timeline.load();
  await h.click(all(h.row("一起去日本"), "tl-mark")[0]);
  assert.deepEqual(plain(h.edits()[0].params), { target: "event", action: "edit", id: "e2", fields: { importance: "general" }, expectedVersion: 1 });
  await h.expandAll();
  await h.click(all(h.row("第一次见面"), "tl-month")[0]);
  const input = all(h.root, "tl-when-input")[0];
  input.value = "2019.05.02";
  for (const listener of input.listeners.keydown) listener({ key: "Enter", preventDefault() {} });
  await settle();
  const when = h.edits()[1].params;
  assert.equal(when.fields.precision, "day"); assert.equal(when.fields.startMs, at(2019, 5, 2));
  // Right-click menu: delete a linked event — the notice says the memory is untouched.
  for (const listener of all(h.row("吵架又和好"), "tl-event-head")[0].listeners.contextmenu) listener({ preventDefault() {}, clientX: 20, clientY: 30 });
  const menu = h.body.children.find(node => has(node, "tl-menu"));
  assert.deepEqual(menu.children.map(node => node.textContent), ["编辑", "取消与关系记忆的关联", "删除"], "a linked memory already confirms it");
  await h.click(menu.children[2]);
  assert.deepEqual(plain(h.edits()[2].params), { target: "event", action: "delete", id: "e3", expectedVersion: 3 });
  assert.match(text(h.part("tl-notice")), /只影响时间线，关系记忆里的这条不变，按 Ctrl\+Z 撤销/);
  // Ctrl+Z in a text box stays with the text; elsewhere it undoes the user's last edit.
  await h.key({ target: { closest: () => ({}) } });
  assert.equal(h.edits().length, 3);
  const pressed = await h.key({});
  assert.equal(pressed.prevented, true);
  assert.equal(h.edits()[3].method, "page.timelineUndo");
  assert.equal(text(h.part("tl-notice")), "已撤销：修改“一起去日本”");
});

test("manual add, the unlink item and a new track send the plan's edit shapes", async () => {
  const h = harness(backend().handler);
  await h.timeline.load();
  await h.click(h.button(h.part("tl-top"), "添加"));
  const form = all(h.part("tl-form"), "tl-edit")[0];
  const [title, track, category, , , start] = form.children;
  title.value = "一起看展"; category.value = "一起做的事"; start.value = "2025.10";
  assert.equal(track.value, "我们", "manual events default to 我们");
  await h.click(h.button(form, "添加"));
  assert.deepEqual(plain(h.edits()[0].params.fields), { title: "一起看展", track: "我们", category: "一起做的事", status: "occurred",
    importance: "general", note: "", precision: "month", startMs: at(2025, 10, 1), endMs: null });
  assert.equal(h.part("tl-form").hidden, true);
  await h.expandAll();
  for (const listener of all(h.row("吵架又和好"), "tl-event-head")[0].listeners.contextmenu) listener({ preventDefault() {}, clientX: 1, clientY: 1 });
  await h.click(h.body.children.find(node => has(node, "tl-menu")).children[1]);
  assert.deepEqual(plain(h.edits()[1].params), { target: "event", action: "unlink", id: "e3", memoryId: "m1", expectedVersion: 2 });
  await h.click(h.button(h.part("tl-top"), "主线"));
  const row = all(h.root, "tl-manage-new")[0];
  row.children[0].value = "工作"; row.children[1].value = "一起上班的事";
  await h.click(h.button(row, "新建主线"));
  assert.deepEqual(plain(h.edits()[2].params), { target: "track", action: "add", fields: { title: "工作", description: "一起上班的事", color: "#07c160" }, expectedVersion: 3 });
});

test("the Agent's edits have their own undo line, and the dismissed line returns on the next Agent write", async () => {
  const data = view({ undo: { user: { count: 0, label: null }, agent: { count: 2, label: "修改“第一次见面”", items: [
    { seq: 5, label: "修改“第一次见面”", agent: "合成 Agent", updatedMs: NOW }, { seq: 4, label: "添加“看展”", agent: "合成 Agent", updatedMs: NOW }] } } });
  data.events[0].agentEditedMs = at(2026, 5, 3);
  const { state, handler } = backend(data);
  const h = harness(handler);
  await h.timeline.load();
  const line = h.part("tl-agent");
  assert.equal(line.hidden, false); assert.equal(text(line).startsWith("Agent 改了 2 处 ·撤销"), true);
  assert.match(line.title, /修改“第一次见面” · 合成 Agent/);
  await h.click(h.button(line, "撤销"));
  assert.equal(h.edits()[0].method, "page.timelineUndoAgent");
  assert.equal(text(h.part("tl-notice")), "已撤销 Agent 的修改：修改“第一次见面”");
  await h.click(h.button(line, "×"));
  assert.equal(line.hidden, true);
  state.data.undo.agent = { count: 2, label: "删除“聚餐”", items: [{ seq: 9, label: "删除“聚餐”", agent: "合成 Agent", updatedMs: NOW }] };
  await h.timeline.load();
  assert.equal(line.hidden, false);
  await h.expandAll();
  await h.click(all(h.row("第一次见面"), "tl-event-open")[0]);
  assert.ok(all(h.row("第一次见面"), "tl-line").some(node => node.textContent === "Agent 改过（5月3日）"));
});

test("a 关系记忆 row is read-only and leads to the memory page; a linked event names its memory", async () => {
  const data = view({ memoryRefs: [memoryRef("m9", "我们 2021 年一起去了杭州", at(2021, 5, 1), { whenText: "2021年5月", evidenceCount: 1 })] });
  const h = harness(backend(data).handler);
  await h.timeline.load();
  assert.ok(all(h.root, "tl-dot").some(node => has(node, "memory")), "drawn as a muted dot on 我们");
  assert.ok(!all(h.root, "tl-dot").some(node => has(node, "memory") && node.attributes["aria-label"] === "吵架又和好"),
    "an event linked to a memory is drawn as an event, not as a memory row");
  await h.expandAll();
  const row = all(h.root, "tl-event").find(node => has(node, "memory"));
  assert.equal(text(all(row, "tl-category")[0]), "关系记忆");
  assert.equal(all(row, "tl-month")[0].tagName, "span", "time cannot be edited");
  assert.equal(all(row, "tl-mark")[0].tagName, "span", "importance cannot be toggled");
  assert.equal(all(row, "tl-event-head")[0].listeners.contextmenu, undefined, "no edit menu");
  await h.click(all(row, "tl-event-open")[0]);
  const reopened = all(h.root, "tl-event").find(node => has(node, "memory"));
  assert.deepEqual(all(reopened, "tl-bubble").map(node => node.textContent), ["杭州那次真好玩"]);
  assert.ok(h.calls.some(call => call.method === "page.timelineEvidence" && call.params.id === "memory:m9"));
  await h.click(h.button(reopened, "到关系记忆里修改"));
  assert.deepEqual(h.hosts.at(-1), { method: "tab.select", params: { tab: "memories" } });
  await h.click(all(h.row("吵架又和好"), "tl-event-open")[0]);
  assert.ok(h.button(h.row("吵架又和好"), "也记在关系记忆里：我们 2023 年吵过一次"));
  assert.ok(h.edits().every(call => call.method !== "page.timelineEdit"), "reading never writes");
});

test("a restricted window shows the timeline without any edit or undo entry", async () => {
  const data = view({ undo: { user: { count: 1, label: "x" }, agent: { count: 1, label: "x", items: [{ seq: 1, label: "x", agent: "", updatedMs: NOW }] } } });
  const h = harness(backend(data).handler, { restricted: true });
  await h.timeline.load();
  for (const label of ["添加", "主线", "管理主线", "撤销"]) assert.equal(h.button(h.root, label), undefined, label);
  assert.equal(h.part("tl-agent").hidden, true);
  assert.ok(all(h.root, "tl-mark").every(node => node.tagName === "span"));
  assert.ok(all(h.root, "tl-event-head").every(node => !node.listeners.contextmenu));
  await h.key({});
  assert.deepEqual(h.edits(), []);
  assert.ok(all(h.root, "tl-dot").length > 0, "the chart is still drawn");
});

test("a version conflict reloads the timeline and asks to retry; empty states follow the run", async () => {
  let version = 1;
  const h = harness(async method => {
    if (method === "page.timeline") return { ...view(), version };
    version = 2;
    throw Object.assign(new Error("冲突"), { code: "conflict" });
  });
  await h.timeline.load();
  await h.click(all(h.row("一起去日本"), "tl-mark")[0]);
  await settle();
  assert.equal(h.part("tl-message").textContent, "时间线刚被修改过，已刷新，请重试。");
  assert.equal(h.calls.filter(call => call.method === "page.timeline").length, 2);

  const before = harness(async () => view({ events: [], memoryRefs: [], run: idle }));
  await before.timeline.load();
  assert.equal(before.part("fa-empty").textContent, "开始分析后，整理我们一起经历过的事");
  assert.equal(before.part("tl-overview").hidden, true, "no chart without events");
  const after = harness(async () => view({ events: [], run: done }));
  await after.timeline.load();
  assert.equal(after.part("fa-empty").textContent, "还没有找到我们一起经历的事，可以手动添加");
});

test("the mock shapes match the real backend output, which renders as it is", async () => {
  const real = fixture.page, mock = view();
  const keys = value => Object.keys(value).sort();
  assert.deepEqual(keys(mock), keys(real));
  assert.deepEqual(keys(mock.events[0]), keys(real.events[0]));
  assert.deepEqual(keys(mock.run), keys(real.run));
  assert.deepEqual(keys(memoryRef("m", "x", 0)), keys(real.memoryRefs[0]));
  assert.deepEqual(keys(mock.undo.agent), keys(real.undo.agent));
  assert.deepEqual(keys(proof("m", "other", "x", 0)), keys(fixture.evidence.items[0]));
  const h = harness(async (method, params) => {
    if (method === "page.timeline") return plain(real);
    if (method === "page.timelineEvidence") return plain(params.id.startsWith("memory:") ? fixture.memoryEvidence : fixture.evidence);
    if (method === "page.timelineUndo") return plain(fixture.undo);
    throw new Error("unexpected " + method);
  });
  await h.timeline.load();
  const hike = real.events.find(item => item.memory);
  const dots = all(h.root, "tl-dot");
  // The unlinked 关系记忆 (2022) shares its year with 一起去日本, so it joins that event's dot.
  assert.ok(dots.some(node => (node.attributes["aria-label"] || "").includes(real.memoryRefs[0].title)));
  assert.ok(dots.filter(node => (node.attributes["aria-label"] || "").includes(hike.title)).every(node => !has(node, "memory")),
    "a linked event stays an event dot");
  assert.match(text(h.part("tl-agent")), /^Agent 改了 1 处/);
  assert.match(h.part("tl-agent").title, /合成 Agent/);
  await h.expandAll();
  await h.click(all(h.root, "tl-year-head").find(node => text(node).startsWith("时间不明")));
  assert.ok(h.rows().includes("我们一起打过羽毛球"));
  const row = all(h.root, "tl-event").find(node => all(node, "tl-title").some(title => text(title) === hike.title) &&
    !all(node, "tl-hint").length);
  await h.click(all(row, "tl-event-open")[0]);
  const opened = all(h.root, "tl-event").find(node => has(node, "open"));
  assert.ok(h.button(opened, "也记在关系记忆里：" + hike.memory.text));
  assert.deepEqual(all(opened, "tl-bubble").map(node => node.textContent).slice(0, 1), [fixture.evidence.items[0].text]);
  await h.click(all(all(h.root, "tl-event").find(node => has(node, "memory")), "tl-event-open")[0]);
  const memoryRow = all(h.root, "tl-event").find(node => has(node, "memory"));
  assert.ok(has(memoryRow, "open") && h.button(memoryRow, "到关系记忆里修改"));
  assert.ok(h.calls.every(call => call.method !== "page.timelineEvidence" || !call.params.id.startsWith("memory:")),
    "a memory with no evidence is not read");
  await h.key({});
  assert.equal(text(h.part("tl-notice")), "已撤销：" + fixture.undo.undone);
});
