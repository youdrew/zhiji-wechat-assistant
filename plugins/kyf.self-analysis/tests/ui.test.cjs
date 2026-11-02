"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = readFileSync(path.join(root, "ui/index.html"), "utf8");
const source = readFileSync(path.join(root, "ui/page.js"), "utf8");
const charts = readFileSync(path.join(root, "../../plugin-sdk/web/persona-charts.js"), "utf8");
const plain = value => JSON.parse(JSON.stringify(value));
const allOutputs = ["profile", "tags", "persona", "values", "library"];
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { await tick(); await tick(); }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
function node(tag = "div", id = "") {
  const value = { tag, id, children: [], listeners: {}, attributes: {}, style: { setProperty() {} }, className: "", textContent: "", value: "", hidden: false, disabled: false };
  value.addEventListener = (name, handler) => { (value.listeners[name] ||= []).push(handler); };
  value.appendChild = child => { value.children.push(child); return child; };
  value.append = (...children) => { value.children.push(...children); };
  value.replaceChildren = (...children) => { value.children = children; };
  value.removeAttribute = name => { delete value.attributes[name]; if (name === "value") delete value.value; };
  value.setAttribute = (name, entry) => { value.attributes[name] = entry; };
  value.focus = () => { value.focused = true; };
  value.select = () => {};
  value.remove = () => { value.removed = true; };
  value.querySelector = () => null;
  value.getBoundingClientRect = () => ({ left: 0, top: 0, bottom: 0, width: 0, height: 0 });
  value.dataset = {};
  Object.defineProperty(value, "firstChild", { get: () => value.children[0] });
  return value;
}
function status(account = "account-a", changes = {}) {
  const defaults = { account, source: { ready: true, message: "本机聊天已连接" },
    build: { state: "idle", phase: "done", processedConversations: 0, totalConversations: 0, sampleCount: 0, messageCount: 0, failedCount: 0 },
    coverage: { complete: false, catalogComplete: false, sampleCount: 0, messageCount: 0, indexedConversations: 0, discoveredConversations: 0, failedConversations: 0 },
    retrieval: { ready: true, mode: "lexical" }, model: { ready: null, message: "回复模型请在核心设置确认" } };
  return { ...defaults, ...changes, build: { ...defaults.build, ...changes.build }, coverage: { ...defaults.coverage, ...changes.coverage } };
}
const analysisStatus = (state = "idle", steps = [], changes = {}) => ({ id: "analysis-1", state, depth: "deep", steps, coverage: {}, ...changes });
const PERSONA_KEYS = ["big5_o", "big5_c", "big5_e", "big5_a", "big5_n", "att_anx", "att_avo", "pol_axis", "pol_ext", "pol_econ", "pol_lib",
  "val_change", "val_self", "moral_care", "moral_fair", "moral_loyal", "moral_auth", "moral_sanct", "moral_liberty"];
const personaStatus = (changes = {}) => ({ version: 1, dimensions: PERSONA_KEYS.map(key => ({ key, selfReport: null, inference: null, manual: null })),
  selfReports: { mbti: null, attachment: null, politics: null }, coverage: {}, ...changes });
const valuesStatus = (changes = {}) => ({ version: 1, dimensions: ["love", "family", "money", "plan", "core"].map(key => ({ key, selfReport: null, inference: null, manual: null })), coverage: {}, ...changes });
function environment(options = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], node("div", match[1])]));
  const timers = new Map(), calls = [], hostCalls = [], events = {};
  let timerId = 0, context = { account: "account-a", user: "friend-a", theme: "light", surface: "self-analysis-window" };
  const env = { elements, timers, calls, hostCalls, events, rpc: options.rpc || ((method, params, scope) => status(scope.account)),
    modelStatus: options.modelStatus || (() => ({ configured: null })),
    host: options.host || ((method) => method === "self-profile.read" ? { background: "", style: "", version: 1 } : { ok: true }) };
  const kyfHost = {
    rpc(method, params, scope) { calls.push({ method, params: plain(params), scope: plain(scope) }); return Promise.resolve().then(() => env.rpc(method, params, scope)).then(value => {
      if (method === "self.analysisStatus" && value?.build) return analysisStatus();
      if (method === "self.analysisSettings" && value?.build) return { outputs: params.outputs || allOutputs };
      if (method === "self.persona" && value?.build) return personaStatus();
      if (method === "self.values" && value?.build) return valuesStatus();
      return value;
    }); },
    host(method, params) { hostCalls.push({ method, params: plain(params) }); return Promise.resolve().then(() => method === "model.status" ? env.modelStatus() : env.host(method, params)); },
    on(listener) { env.listener = listener; return () => {}; }, ready: () => Promise.resolve(), context: () => context,
  };
  const document = { getElementById: id => elements.get(id), createElement: tag => node(tag), createElementNS: (space, tag) => node(tag), querySelector: () => null, body: node("body") };
  env.document = document;
  const window = { document, kyfHost, addEventListener: (name, handler) => {
    const earlier = events[name];
    events[name] = earlier ? event => { earlier(event); return handler(event); } : handler;
  } };
  const Clock = options.now === undefined ? Date : class extends Date { static now() { return options.now; } };
  vm.runInNewContext(charts + "\n" + source, { window, document, Date: Clock, innerWidth: 800, innerHeight: 600, setTimeout: handler => { const id = ++timerId; timers.set(id, handler); return id; }, clearTimeout: id => timers.delete(id) });
  const find = (item, id) => item.id === id ? item : item.children.map(child => find(child, id)).find(Boolean);
  env.get = id => elements.get(id) || [...elements.values()].map(item => find(item, id)).find(Boolean);
  env.click = async id => { const item = env.get(id); assert.equal(item.disabled, false, id + " must be enabled"); for (const callback of item.listeners.click || []) callback({}); await settle(); };
  env.input = (id, text) => { const item = env.get(id); item.value = text; for (const callback of item.listeners.input || []) callback({}); };
  env.change = async (id, value) => { const item = env.get(id); item.value = value; for (const callback of item.listeners.change || []) callback({}); await settle(); };
  env.check = async (key, checked) => { const item = env.get("analysisOutput-" + key); assert.equal(item.disabled, false); item.checked = checked; for (const callback of item.listeners.change || []) callback({}); await settle(); };
  env.context = async next => { context = { ...context, ...next }; env.listener({ type: "context", context }); await settle(); };
  env.shown = async () => { env.listener({ type: "event", event: { type: "shown" } }); await settle(); };
  env.fireTimers = async () => { const batch = [...timers.values()]; timers.clear(); for (const callback of batch) callback(); await settle(); };
  return env;
}
const text = node => node.textContent + node.children.map(text).join("");
const tips = node => [node.attributes.title || "", ...node.children.map(tips)].filter(Boolean).join("\n");
const example = (id, reply = "这次先不去啦，下回再约") => ({ id, name: "合成好友", input: "本人：这周有点累。\n对方：周末来聚会吗？", reply, timeMs: 1000, matchKinds: ["lexical"] });
const excludeButton = card => card.children[0].children.at(-1);
const tagItem = (name, category, level = "strong", extra = {}) => ({ key: name, name, category, level, relation: "interest", manual: false, mentions: 3, chats: 2, days: 3, firstMs: 1000, lastMs: 1000 + 2 * 86400000, ...extra });
const tagStatus = (run = { state: "complete" }, tags = [], account = "account-a") => ({ account, run, tags,
  counts: { conversations: 3, groups: 1, messages: 10, pendingMessages: 0 }, categories: ["运动健身", "游戏棋牌", "性格习惯", "其他"] });
const chips = env => env.get("tagList").children.flatMap(group => group.children[1].children);
const chip = (env, name) => chips(env).find(item => item.textContent === name);
const bubbles = env => env.get("chatLog").children.filter(row => row.className.startsWith("turn")).map(row => [row.className.slice(5), ...row.children[1].children.map(text)]);

const timelineEvent = (id, title, changes = {}) => ({ id, title, track: "学业", status: "occurred", importance: "general", level: "strong",
  startMs: new Date(2024, 2, 1).getTime(), endMs: new Date(2024, 3, 1).getTime() - 1, precision: "month", whenText: "2024年3月", ...changes });
const timelineView = (events, version = 1) => ({ version, events, periods: [], eventsTotal: events.length, nextOffset: null, coverage: {}, run: { state: "done" },
  tracks: [{ name: "学业", color: "#07c160", visible: true, builtin: true, description: "" }] });
const all = (item, match) => [...(match(item) ? [item] : []), ...item.children.flatMap(child => all(child, match))];
const timelineByClass = (env, id, name) => all(env.get(id), item => item.className.split(" ").includes(name));
const timelineRow = (env, title) => timelineByClass(env, "timelineList", "tl-event").find(row => text(row).includes(title));
const within = (row, name) => all(row, item => item.className.split(" ").includes(name))[0];
const press = async (env, target, key = "z", extra = { ctrlKey: true }) => {
  let prevented = false;
  env.events.keydown({ key, ...extra, target, preventDefault() { prevented = true; } }); await settle(); return prevented;
};
async function timelinePage(events, persist = false) {
  const edits = [];
  let view = timelineView(events);
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.timeline") return plain(view);
    if (method === "self.timelineEdit") { edits.push({ method, ...plain(params) }); if (persist) Object.assign(view.events.find(event => event.id === params.id), params.fields); view = timelineView(view.events, view.version + 1); return plain(view); }
    if (method === "self.timelineUndo") { edits.push({ method, ...plain(params) }); view = timelineView(view.events, view.version + 1); return { ...plain(view), undone: "修改“入学”" }; }
    return status(scope.account);
  } });
  await settle(); await env.click("tab-timeline"); await settle();
  return { env, edits };
}
const timeInput = env => timelineByClass(env, "timelineList", "tl-when-input")[0];
async function typeWhen(env, row, value, key = "Enter") {
  for (const callback of within(row, "tl-month").listeners.click) callback({});
  await settle();
  const input = timeInput(env); input.value = value;
  for (const callback of input.listeners.input) callback({});
  for (const callback of input.listeners.keydown) callback({ key, preventDefault() {} });
  await settle();
}

test("the time beside the dot opens in place and accepts the dates people actually type", async () => {
  const { env, edits } = await timelinePage([timelineEvent("e1", "入学", { importance: "important" }), timelineEvent("e2", "未知", { importance: "important", startMs: null, endMs: null, precision: "unknown" })]);
  assert.equal(text(within(timelineRow(env, "未知"), "tl-month")), "—");
  for (const [typed, year, month, day, precision] of [["2024.05", 2024, 4, 1, "month"], ["2024/5", 2024, 4, 1, "month"], ["2024-05", 2024, 4, 1, "month"], ["2024年5月", 2024, 4, 1, "month"],
    ["202405", 2024, 4, 1, "month"], ["2024.5.3", 2024, 4, 3, "day"], ["2024年5月3日", 2024, 4, 3, "day"], ["20240503", 2024, 4, 3, "day"], ["2023", 2023, 0, 1, "year"]]) {
    await typeWhen(env, timelineRow(env, "未知"), typed);
    const fields = edits.at(-1).fields;
    assert.deepEqual([edits.at(-1).id, fields.startMs, fields.precision], ["e2", new Date(year, month, day).getTime(), precision], typed);
    assert.equal(timeInput(env), undefined, typed);
    edits.length = 0;
  }
});

test("a bad date keeps the input open, Escape abandons it and clearing makes the time unknown", async () => {
  const { env, edits } = await timelinePage([timelineEvent("e1", "入学", { importance: "important" })]);
  for (const typed of ["2024.13", "2024.02.30", "去年", "0024.05", "1965", "2100"]) {
    await typeWhen(env, timelineRow(env, "入学"), typed);
    assert.equal(edits.length, 0, typed); assert.match(env.get("timelineMessage").textContent, /2024\.05/); assert.equal(timeInput(env).value, typed);
    for (const callback of timeInput(env).listeners.keydown) callback({ key: "Escape", preventDefault() {} });
    await settle();
  }
  assert.equal(timeInput(env), undefined);
  await typeWhen(env, timelineRow(env, "入学"), "2019.09", "Escape");
  assert.equal(edits.length, 0);
  await typeWhen(env, timelineRow(env, "入学"), "2024-03");
  assert.equal(edits.length, 0, "an unchanged time writes nothing");
  await typeWhen(env, timelineRow(env, "入学"), "");
  assert.deepEqual(edits.at(-1).fields, { startMs: null, endMs: null, precision: "unknown" });
});

test("Enter that only confirms an IME candidate does not save the time", async () => {
  const { env, edits } = await timelinePage([timelineEvent("e1", "入学", { importance: "important" })]);
  for (const callback of within(timelineRow(env, "入学"), "tl-month").listeners.click) callback({});
  await settle();
  for (const callback of timeInput(env).listeners.keydown) callback({ key: "Enter", isComposing: true, preventDefault() {} });
  await settle();
  assert.equal(edits.length, 0); assert.notEqual(timeInput(env), undefined);
});

test("changing the start of a span keeps its end, and leaving the input saves it", async () => {
  const end = new Date(2026, 5, 30).getTime();
  const { env, edits } = await timelinePage([timelineEvent("e1", "读研", { importance: "important", endMs: end })]);
  await typeWhen(env, timelineRow(env, "读研"), "2024.10");
  assert.equal(edits.at(-1).fields.endMs, end);
  for (const callback of within(timelineRow(env, "读研"), "tl-month").listeners.click) callback({});
  await settle();
  const input = timeInput(env); input.value = "2027.01";
  for (const callback of input.listeners.blur) callback({});
  await settle();
  assert.equal(edits.at(-1).fields.endMs, new Date(2027, 1, 1).getTime() - 1, "a start past the old end cannot keep it");
});

test("the dot toggles importance by itself and a confirmed general event shows no star", async () => {
  const { env, edits } = await timelinePage([timelineEvent("e1", "入学", { level: "confirmed", status: "planned" })], true);
  const row = timelineRow(env, "入学");
  assert.equal(within(row, "tl-star"), undefined);
  assert.equal(within(row, "tl-mark").attributes["aria-pressed"], "false");
  assert.equal(within(row, "tl-event-open").tag, "button"); assert.equal(within(row, "tl-event-head").tag, "div");
  for (const callback of within(row, "tl-mark").listeners.click) callback({});
  await settle();
  assert.deepEqual(edits.map(item => [item.target, item.action, item.id, item.fields]), [["event", "edit", "e1", { importance: "important" }]]);
  assert.equal(within(timelineRow(env, "入学"), "tl-star").textContent, "☆");
});

test("Ctrl+Z undoes on the timeline tab only and leaves typing alone", async () => {
  const { env, edits } = await timelinePage([timelineEvent("e1", "入学", { importance: "important" })]);
  assert.equal(await press(env, node("button"), "z", { metaKey: true }), true);
  assert.deepEqual(edits.map(item => [item.method, item.expectedVersion]), [["self.timelineUndo", 1]]);
  assert.equal(timelineByClass(env, "timelineList", "tl-notice")[0].textContent, "已撤销：修改“入学”");
  const typing = node("input"); typing.closest = () => typing;
  assert.equal(await press(env, typing), false);
  assert.equal(await press(env, node("button"), "z", { ctrlKey: true, shiftKey: true }), false);
  assert.equal(await press(env, node("button"), "z", { ctrlKey: true, repeat: true }), false);
  await env.click("tab-profile");
  assert.equal(await press(env, node("button")), false);
  assert.equal(edits.length, 1);
});

test("a stale version reloads the timeline instead of leaving the page stuck", async () => {
  let reads = 0;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.timeline") { reads++; return timelineView([timelineEvent("e1", "入学", { importance: "important" })], reads); }
    if (method === "self.timelineEdit") throw Object.assign(new Error("冲突"), { code: "conflict" });
    return status(scope.account);
  } });
  await settle(); await env.click("tab-timeline"); await settle();
  for (const callback of within(timelineRow(env, "入学"), "tl-mark").listeners.click) callback({});
  await settle(); await settle();
  assert.equal(reads, 2); assert.equal(env.get("timelineMessage").textContent, "时间线刚被修改过，已刷新，请重试。");
  assert.equal(within(timelineRow(env, "入学"), "tl-star").textContent, "★", "the optimistic change is rolled back");
});

test("the timeline shows a reading message until the first read returns, not the empty pane", async () => {
  const gate = deferred();
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.timeline") return gate.promise;
    return status(scope.account);
  } });
  await settle(); await env.click("tab-timeline");
  assert.equal(env.get("timelineMessage").textContent, "正在读取…");
  assert.equal(env.get("timelineEmpty").hidden, true, "the empty pane stays hidden while reading");
  gate.resolve(plain(timelineView([timelineEvent("e1", "入学", { importance: "important" })])));
  await settle();
  assert.equal(env.get("timelineMessage").hidden, true);
  assert.equal(timelineRow(env, "入学") !== undefined, true);
  assert.equal(env.get("timelineEmpty").hidden, true);
});

test("a year folds general events behind one plain button and opens them in date order", async () => {
  const { env } = await timelinePage([timelineEvent("e1", "入学", { importance: "important" }),
    timelineEvent("e2", "更晚的小事", { startMs: new Date(2024, 8, 1).getTime() }),
    timelineEvent("e3", "取消的事", { status: "cancelled", startMs: new Date(2024, 0, 1).getTime() })]);
  const folded = () => timelineByClass(env, "timelineList", "tl-folded")[0];
  assert.equal(folded().textContent, "展开全部事件");
  assert.equal(folded().children.length, 0);
  assert.doesNotMatch(text(env.get("timelineList")), /一般事件|已取消|另 /);
  assert.equal(timelineByClass(env, "timelineList", "tl-event").length, 1);
  for (const callback of folded().listeners.click) callback({});
  assert.equal(folded().textContent, "收起");
  await env.click("tab-tags"); await env.click("tab-timeline"); await settle();
  assert.deepEqual(timelineByClass(env, "timelineList", "tl-title").map(item => item.textContent), ["更晚的小事", "入学", "取消的事"], "an edit or reload keeps the year open");
});

test("important events become stars on the overview with a tooltip of what happened", async () => {
  const { env } = await timelinePage([timelineEvent("e1", "入学", { importance: "important" }),
    timelineEvent("e2", "同月大事", { importance: "important" }),
    timelineEvent("e3", "只知道年份", { importance: "important", precision: "year" }),
    timelineEvent("e4", "小事"), timelineEvent("e5", "取消的大事", { importance: "important", status: "cancelled" }),
    timelineEvent("e6", "时间不明的大事", { importance: "important", startMs: null, endMs: null, precision: "unknown" })]);
  const stars = timelineByClass(env, "timelineOverview", "tl-overview-star");
  assert.deepEqual(stars.map(star => star.attributes["aria-label"]), ["入学、同月大事", "只知道年份"]);
  assert.equal(stars[0].style.left, `${(2.5 / 12) * 100}%`); assert.equal(stars[1].style.left, "50%");
  for (const callback of stars[0].listeners.mouseenter) callback({});
  const tip = env.document.body.children.find(item => item.className.includes("tl-tip"));
  assert.equal(tip.attributes.role, "tooltip");
  assert.deepEqual(tip.children.map(line => text(line)), ["2024年3月入学", "2024年3月同月大事"]);
});

test("seven tabs have one selected panel, roving focus and preserve the current profile draft", async () => {
  const env = environment(); await settle(); const keys = ["profile", "tags", "timeline", "persona", "values", "build", "trial"];
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length); assert.equal(ids.includes("quickBuild"), false);
  assert.equal(env.get("panel-profile").hidden, false); env.input("background", "未保存的合成自述");
  await env.click("tab-trial"); await env.click("tab-profile");
  assert.equal(env.get("background").value, "未保存的合成自述"); assert.equal(env.get("profileMessage").textContent, "未保存");
  for (const [from, key, target] of [["profile", "ArrowRight", "tags"], ["tags", "End", "trial"], ["trial", "ArrowRight", "profile"], ["profile", "ArrowLeft", "trial"], ["trial", "ArrowLeft", "build"], ["build", "Home", "profile"]]) {
    let prevented = false;
    for (const listener of env.get("tab-" + from).listeners.keydown) listener({ key, preventDefault() { prevented = true; } });
    assert.equal(prevented, true); assert.equal(env.get("tab-" + target).focused, true);
    for (const candidate of keys) {
      assert.equal(env.get("panel-" + candidate).hidden, candidate !== target);
      assert.equal(env.get("tab-" + candidate).attributes["aria-selected"], String(candidate === target));
      assert.equal(env.get("tab-" + candidate).tabIndex, candidate === target ? 0 : -1);
    }
  }
  assert.deepEqual(env.calls.map(call => call.method), ["self.status", "self.analysisStatus", "self.analysisSettings", "self.draftStatus", "self.tags", "self.exclusions"], "opening result tabs only reads their saved data");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});

test("Enter searches or sends while Shift + Enter and IME composition keep typing", async () => {
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.search") return { sources: [] };
    if (method === "self.reply") return { id: "r1", state: "done", result: { messages: ["好"] }, sources: [] };
    return status(scope.account, { coverage: { sampleCount: 2 } });
  } }); await settle();
  for (const [id, method] of [["query", "self.search"], ["trialInput", "self.reply"]]) {
    env.get(id).value = "在吗";
    for (const key of [{ key: "Enter", shiftKey: true }, { key: "Enter", isComposing: true }]) {
      let prevented = false;
      for (const listener of env.get(id).listeners.keydown) listener({ ...key, preventDefault() { prevented = true; } });
      await settle(); assert.equal(prevented, false); assert.equal(env.calls.some(call => call.method === method), false);
    }
    let prevented = false;
    for (const listener of env.get(id).listeners.keydown) listener({ key: "Enter", preventDefault() { prevented = true; } });
    await settle(); assert.equal(prevented, true); assert.equal(env.calls.at(-1).method, method);
  }
});
test("global nav page uses the host bridge and keeps private content out of browser storage and HTML injection", () => {
  const manifest = JSON.parse(readFileSync(path.join(root, "plugin.json"), "utf8"));
  assert.equal(manifest.id, "kyf.self-analysis"); assert.equal(manifest.name, "自我剖析");
  assert.deepEqual(manifest.contributes.nav, [{ id: "self-analysis", label: "自我剖析", page: "ui/index.html", icon: "self" }]);
  assert.ok(html.includes("connect-src 'none'")); assert.ok(html.includes("form-action 'none'"));
  assert.deepEqual([...html.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]), ["/plugin-sdk/web/kyf-host.js", "/plugin-sdk/web/persona-charts.js", "page.js"]);
  assert.doesNotMatch(source, /\bfetch\(|localStorage|sessionStorage|indexedDB|\.innerHTML/);
  assert.doesNotMatch(html, /embedding|RAG|API Key|ANTHROPIC/);
  assert.doesNotMatch(source, /models\.generate|message\.send|clipboard\.copy/);
});
test("opening only reads saved results, and one top-level action starts every analysis step without a model", async () => {
  let started = false;
  const env = environment({ modelStatus: () => ({ configured: false }), rpc(method, params, scope) {
    if (method === "self.analyze") { started = true; return analysisStatus("running", [{ key: "library", title: "表达知识库", state: "running", detail: "整理本地样本" }]); }
    return status(scope.account, started ? { build: { state: "running", phase: "indexing" }, coverage: { sampleCount: 3 } } : {});
  } }); await settle();
  assert.deepEqual(env.calls.map(call => call.method), ["self.status", "self.analysisStatus", "self.analysisSettings", "self.draftStatus"]);
  assert.equal(env.get("analysisRun").disabled, false); assert.equal(env.get("analysisModelHint").hidden, false);
  assert.equal(env.get("panel-profile").hidden, false); assert.match(env.get("buildMessage").textContent, /96 MB/);
  await env.click("analysisRun");
  assert.deepEqual(env.calls.find(call => call.method === "self.analyze").params, { outputs: allOutputs, background: "", style: "" });
  assert.equal(env.get("analysisRun").disabled, true); assert.equal(env.get("analysisPause").hidden, false);
  assert.equal(env.get("search").disabled, false); assert.equal(env.get("trialSend").disabled, false);
  assert.equal(env.get("analysisSteps").children[0].children[0].textContent, "表达知识库");
  assert.equal(env.calls.some(call => ["self.build", "self.draft", "self.tagsAnalyze"].includes(call.method)), false);
  for (const id of ["draftProfile", "tagsRun", "tagsStart", "pauseDraft"]) assert.equal(env.get(id).hidden, true, id + " has no duplicate visible entry");
});

test("self-profile can be edited without a contact, source connection, or model", async () => {
  const env = environment({ rpc: (method, params, scope) => status(scope.account, { source: { ready: false, message: "尚未连接" } }),
    host: (method, params) => method === "self-profile.read" ? { background: "合成背景", style: "", version: 4 } : { background: params.background, style: params.style, version: 5 } });
  await settle(); await env.context({ user: "" });
  assert.equal(env.get("background").disabled, false);
  env.input("style", "拒绝时简短说明"); await env.click("saveProfile");
  assert.deepEqual(env.hostCalls.at(-1), { method: "self-profile.save", params: { account: "account-a", background: "合成背景", style: "拒绝时简短说明", expectedVersion: 4 } });
  assert.match(env.get("profileMessage").textContent, /已保存/);
});
test("profile conflicts preserve the draft and explicitly load the new version for comparison", async () => {
  let version = 1, conflict = true;
  const env = environment({ host(method, params) {
    if (method === "self-profile.read") return { background: version === 1 ? "旧背景" : "另一窗口的新背景", style: "简短", version };
    if (conflict) { conflict = false; version = 2; throw Object.assign(new Error("自身资料冲突"), { code: "request-conflict" }); }
    assert.equal(params.expectedVersion, 2); return { background: params.background, style: params.style, version: 3 };
  } });
  await settle(); env.input("background", "我的合并草稿"); await env.click("saveProfile");
  assert.equal(env.get("background").value, "我的合并草稿"); assert.match(env.get("profileError").textContent, /草稿已保留/);
  await env.click("reloadProfile");
  assert.equal(env.get("background").value, "我的合并草稿"); assert.equal(env.get("latestBackground").textContent, "另一窗口的新背景");
  assert.equal(env.get("latestProfile").hidden, false); await env.click("saveProfile");
  assert.equal(env.get("latestProfile").hidden, true); assert.equal(env.get("saveProfile").disabled, true);
});
test("unified analysis keeps unsaved profile edits and delivers an explicit replacement draft once", async () => {
  let ready = false;
  const env = environment({ host: method => method === "self-profile.read" ? { background: "原背景", style: "原表达", version: 1 } : { ok: true }, rpc(method, params, scope) {
    if (method === "self.analyze") { ready = true; return analysisStatus("done", [{ key: "profile", title: "本人资料", state: "done" }]); }
    if (method === "self.draftStatus") return ready ? { id: "draft-1", state: "done", depth: "deep", coverage: { ownMessages: 18, analyzedMessages: 18 }, result: { background: "AI 背景", style: "AI 表达" } } : { state: "idle" };
    return status(scope.account);
  } }); await settle(); env.input("background", "手写但未保存"); env.input("style", "");
  await env.click("analysisRun");
  assert.equal(env.get("background").value, "手写但未保存"); assert.equal(env.get("style").value, "", "even a manually cleared field remains untouched");
  assert.equal(env.get("draftPanel").hidden, false); assert.equal(env.get("draftBackground").textContent, "AI 背景");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
  await env.click("useDraftStyle"); assert.equal(env.get("style").value, "AI 表达");
  await env.click("closeDraft"); await env.shown();
  assert.equal(env.get("draftPanel").hidden, true, "polling the same finished result does not reopen a dismissed draft");
  assert.equal(env.get("background").value, "手写但未保存"); assert.equal(env.get("draftCoverage").hidden, false);
});

test("unified start failures stay readable and a late response cannot populate another account", async () => {
  const pending = deferred(); let delayed = false;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analyze") { if (delayed) return pending.promise; throw Object.assign(new Error("尚未配置回复模型，请先在模型设置中配置"), { code: "api-not-configured" }); }
    return status(scope.account);
  } }); await settle(); await env.click("analysisRun");
  assert.match(env.get("analysisMessage").textContent, /尚未配置/); assert.equal(env.get("analysisModelHint").hidden, false);
  assert.equal(env.get("analysisRun").disabled, false);
  delayed = true; await env.click("analysisRun"); await env.context({ account: "account-b", user: "" });
  pending.resolve(analysisStatus("done", [{ title: "旧账号步骤", state: "done" }])); await settle();
  assert.equal(env.get("analysisSteps").children.length, 0); assert.equal(env.get("draftPanel").hidden, true);
  assert.equal(env.get("analysisRun").textContent, "开始分析");
});

test("the analysis depth lives in the plugin settings: the menu shows the saved one and opens them", async () => {
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisSettings") return { outputs: allOutputs, depth: scope.account === "account-b" ? "basic" : "standard" };
    if (method === "self.analyze") return analysisStatus("done");
    return status(scope.account);
  } }); await settle();
  assert.equal(html.includes('id="draftDepth"'), false, "no depth selector on the page");
  assert.equal(env.get("analysisDepthSettings").textContent, "标准"); assert.match(env.get("analysisDepthSettings").title, /插件设置/);
  await env.click("analysisDepthSettings");
  assert.deepEqual(env.hostCalls.filter(call => call.method === "settings.open").map(call => call.params.section), ["plugin"]);
  await env.click("analysisRun");
  assert.equal("depth" in env.calls.find(call => call.method === "self.analyze").params, false, "the backend reads the saved depth");
  assert.equal(env.calls.some(call => ["self.build", "self.draft", "self.tagsAnalyze"].includes(call.method)), false);
  await env.context({ account: "account-b", user: "" }); assert.equal(env.get("analysisDepthSettings").textContent, "基础");
});

test("reopening follows the unified run, pausing saves its steps and continuing uses the same entry", async () => {
  let run = analysisStatus("running", [{ key: "library", title: "表达知识库", state: "done" }, { key: "profile", title: "本人资料", state: "running" }]);
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analysisPause") { run = { ...run, state: "paused" }; return run; }
    if (method === "self.analyze") { run = { ...run, state: "running" }; return run; }
    return status(scope.account);
  } }); await settle();
  assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
  assert.equal(env.get("analysisProgress").hidden, true); assert.equal(env.get("analysisProgress").value, undefined);
  await env.click("analysisPause"); assert.equal(env.get("analysisRun").textContent, "继续分析");
  await env.click("analysisRun"); assert.equal(env.calls.filter(call => call.method === "self.analyze").length, 1);
  run = { ...run, state: "done", steps: run.steps.map(step => ({ ...step, state: "done" })) }; await env.fireTimers();
  assert.equal(env.get("analysisRun").textContent, "更新分析"); assert.equal(env.get("analysisPause").hidden, true);
});

test("completed profile analysis retains exact gaps and never exposes unknown gap details", async () => {
  const coverage = { conversations: 10, readConversations: 9, failedConversations: 1, ownMessages: 17, privateMessages: 8, groupMessages: 9, analyzedMessages: 17, totalParts: 20, analyzedParts: 20, unreadMessages: 2, excludedMessages: 5, catalogComplete: true, directoryPartial: true, gaps: [{ kind: "failed-shards", count: 2 }, { kind: "PRIVATE/path/person", count: 1 }] };
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.draftStatus") return { id: "complete", state: "done", depth: "deep", batchesDone: 7, coverage, result: { background: "合成背景", style: "合成表达" } };
    return status(scope.account);
  } });
  await settle();
  assert.match(env.get("draftCoverageTitle").textContent, /已完成可读部分/);
  assert.match(env.get("draftModelCoverage").textContent, /17 \/ 17.*20 \/ 20/);
  assert.match(env.get("draftBatchCoverage").textContent, /7 批/);
  assert.match(env.get("draftGaps").textContent, /2.*历史文件|历史文件.*2/);
  assert.match(env.get("draftGaps").textContent, /1 个聊天未能读取/);
  assert.doesNotMatch(env.get("draftGaps").textContent, /PRIVATE|person/);
  env.input("background", ""); await env.shown();
  assert.equal(env.get("background").value, "", "showing a completed job again cannot refill a field the user cleared");
  assert.equal(env.get("draftCoverage").hidden, false);
});

test("profile coverage separates cached analysis from this run and tolerates older status payloads", async () => {
  const base = { conversations: 10, readConversations: 10, messagesRead: 700, ownMessages: 120, privateMessages: 90, groupMessages: 30, analyzedMessages: 120, catalogComplete: true };
  for (const metrics of [{ reusedMessages: 117, newlyAnalyzedMessages: 3 }, { reusedMessages: 120, newlyAnalyzedMessages: 0 }, {}, { reusedMessages: -1, newlyAnalyzedMessages: 2 }]) {
    const env = environment({ rpc(method, params, scope) {
        if (method === "self.draftStatus") return { id: "cached", state: "done", depth: "deep", coverage: { ...base, ...metrics }, result: { background: "合成背景", style: "" } };
      return status(scope.account);
    } });
    await settle();
    assert.equal(env.get("draftCoverageTitle").textContent, "本地可读文字已分析完成");
    assert.match(env.get("draftReadCoverage").textContent, /本地累计已读取 10 \/ 10.*保存 120 条本人文字/);
    assert.match(env.get("draftModelCoverage").textContent, /累计已分析 120 \/ 120/);
    if (metrics.reusedMessages >= 0) assert.match(env.get("draftModelCoverage").textContent, new RegExp(`复用已有分析 ${metrics.reusedMessages} 条，本轮新分析 ${metrics.newlyAnalyzedMessages} 条`));
    else assert.doesNotMatch(env.get("draftModelCoverage").textContent, /复用已有分析|本轮新分析/);
    assert.equal(env.calls.some(call => call.method === "self.draft"), false);
  }
});

test("a late saved depth from the old account cannot replace the new account's", async () => {
  const pending = deferred();
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisSettings") return scope.account === "account-a" ? pending.promise : { outputs: allOutputs, depth: "basic" };
    if (method === "self.draftStatus") return { state: "idle" };
    return status(scope.account);
  } });
  await settle(); await env.context({ account: "account-b" });
  pending.resolve({ outputs: allOutputs, depth: "standard" }); await settle();
  assert.equal(env.get("analysisDepthSettings").textContent, "基础");
});

test("account switches discard unsaved text and ignore old search, status, and save results", async () => {
  const pendingSearch = deferred(), pendingSave = deferred();
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.search") return pendingSearch.promise;
    return status(scope.account, { build: { state: "complete" }, coverage: { complete: true, sampleCount: 2 } });
  }, host(method, params) {
    if (method === "self-profile.save") return pendingSave.promise;
    return { background: params.account === "account-a" ? "账号甲资料" : "账号乙资料", style: "", version: params.account === "account-a" ? 1 : 8 };
  } });
  await settle(); env.input("background", "甲的未保存输入"); env.input("query", "邀约");
  await env.click("search"); await env.click("saveProfile"); await env.context({ account: "account-b", user: "friend-b" });
  assert.equal(env.get("background").value, "账号乙资料"); assert.match(env.get("notice").textContent, /上一账号未保存/);
  pendingSearch.resolve({ sources: [{ reply: "不该出现在乙账号的甲样本", input: "旧前文" }] }); pendingSave.resolve({ version: 2 }); await settle();
  assert.equal(env.get("samples").children.length, 0); assert.equal(env.get("background").value, "账号乙资料");
  assert.equal(env.get("query").value, ""); assert.equal(env.get("saveProfile").disabled, true);
  assert.equal(env.calls.find(call => call.method === "self.search").scope.account, "account-a");
});
test("a late status response cannot populate a newly selected account", async () => {
  const old = deferred();
  const env = environment({ rpc: (method, params, scope) => scope.account === "account-a" ? old.promise : status(scope.account, { coverage: { sampleCount: 1 } }) });
  await settle(); await env.context({ account: "account-b", user: "" });
  old.resolve(status("account-a", { coverage: { sampleCount: 999 } })); await settle();
  assert.equal(env.get("sampleCount").textContent, "1"); assert.match(env.get("account").textContent, /account-b/);
});
test("every hidden compatibility start routes through the unified endpoint", async () => {
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analyze") return analysisStatus("done");
    if (method === "self.analysisPause") return analysisStatus("paused");
    return status(scope.account, { coverage: { sampleCount: 4 } });
  } }); await settle(); await env.click("tab-tags");
  for (const id of ["draftProfile", "tagsRun", "tagsStart"]) {
    for (const callback of env.get(id).listeners.click) callback({}); await settle();
  }
  assert.equal(env.calls.filter(call => call.method === "self.analyze").length, 3);
  assert.equal(env.calls.some(call => ["self.build", "self.resume", "self.draft", "self.tagsAnalyze"].includes(call.method)), false);
});

test("clearing requires a page confirmation and does not alter the Core self-profile", async () => {
  let value = status("account-a", { build: { state: "complete" }, coverage: { sampleCount: 3 } });
  const env = environment({ rpc(method) { if (method === "self.reset") value = status(); return value; } }); await settle();
  env.input("background", "未保存的本人资料");
  await env.click("reset"); assert.equal(env.get("resetConfirm").hidden, false); assert.equal(env.calls.some(call => call.method === "self.reset"), false);
  await env.click("cancelReset"); assert.equal(env.get("resetConfirm").hidden, true);
  await env.click("reset"); await env.click("confirmReset");
  assert.equal(env.get("sampleCount").textContent, "0"); assert.equal(env.get("background").value, "未保存的本人资料");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});
test("historical samples render as text with the account's own lines shown as 我", async () => {
  const injected = '<img src=x onerror="send()">';
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.search") return { mode: "lexical", sources: [{ id: "s1", name: "合成好友", timeMs: 1000, input: "本人：最近忙\n对方：" + injected, reply: "谢谢邀请，这次先不去啦" }] };
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 } });
  } }); await settle(); env.input("query", "拒绝邀约"); await env.click("search");
  const card = env.get("samples").children[0];
  assert.equal(env.get("samples").children.length, 1); assert.ok(text(card).includes(injected));
  assert.deepEqual(card.children.slice(1).map(node => [node.tag, node.textContent]), [["p", "我：最近忙\n对方：" + injected], ["p", "我：谢谢邀请，这次先不去啦"]]);
  assert.equal(excludeButton(card).textContent, "排除"); assert.equal(env.get("searchMessage").hidden, true);
  assert.equal(env.hostCalls.some(call => call.method === "view.focusChat"), false);
});
test("the trial chat sends the conversation as the friend, follows the job and shows what the reply drew on", async () => {
  const jobs = [{ id: "r1", state: "running", phase: "reading" }, { id: "r1", state: "running", phase: "writing" },
    { id: "r1", state: "done", result: { messages: ["好呀", "周六中午？", 3] }, sources: [example("a", "好呀，周末见")] }];
  const env = environment({ modelStatus: () => ({ configured: true }), rpc(method, params, scope) {
    if (method === "self.reply" || method === "self.replyStatus") return jobs.shift();
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 } });
  } }); await settle(); await env.click("tab-trial");
  assert.equal(env.get("chatEmpty").hidden, false); assert.equal(env.get("clearTrial").disabled, true);
  env.get("trialInput").value = "  周末一起吃饭吗  "; await env.click("trialSend");
  assert.deepEqual(env.calls.find(call => call.method === "self.reply").params, { messages: [{ from: "friend", text: "周末一起吃饭吗" }] });
  assert.equal(env.get("trialInput").value, ""); assert.equal(env.get("trialSend").disabled, true);
  assert.deepEqual(bubbles(env), [["friend", "周末一起吃饭吗"], ["me", "正在输入…"]]);
  await env.fireTimers(); await env.fireTimers();
  assert.deepEqual(bubbles(env), [["friend", "周末一起吃饭吗"], ["me", "好呀", "周六中午？"]]);
  assert.equal(env.get("chatEmpty").hidden, true); assert.equal(env.get("trialSend").disabled, false);
  assert.equal(env.get("samples").children.length, 1); assert.equal(env.get("searchMessage").textContent, "这条回复参考了 1 条历史回复");
  assert.equal(env.calls.filter(call => call.method === "self.replyStatus").length, 2);
  jobs.push({ id: "r2", state: "done", result: { messages: ["行"] }, sources: [] });
  env.get("trialInput").value = "那就这么定了"; await env.click("trialSend");
  assert.deepEqual(env.calls.at(-1).params.messages.map(turn => turn.from + ":" + turn.text), ["friend:周末一起吃饭吗", "me:好呀", "me:周六中午？", "friend:那就这么定了"]);
  assert.equal(env.get("searchMessage").textContent, "这条回复没有找到相近的历史回复");
  await env.click("clearTrial");
  assert.deepEqual(bubbles(env), []); assert.equal(env.get("chatEmpty").hidden, false); assert.equal(env.get("clearTrial").disabled, true);
  assert.equal(env.calls.some(call => /send|generate/.test(call.method)), false);
  assert.equal(env.hostCalls.some(call => /send|copy/.test(call.method)), false);
});
test("a reply of more than three bubbles shows every bubble", async () => {
  const env = environment({ modelStatus: () => ({ configured: true }), rpc(method, params, scope) {
    if (method === "self.reply") return { id: "r1", state: "done", result: { messages: ["一", "二", "三", "四", "五", "六"] }, sources: [] };
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 } });
  } }); await settle(); await env.click("tab-trial");
  env.get("trialInput").value = "在吗"; await env.click("trialSend");
  assert.deepEqual(bubbles(env), [["friend", "在吗"], ["me", "一", "二", "三", "四", "五", "六"]]);
});
test("a multi-line friend input is sent as several consecutive friend turns", async () => {
  const env = environment({ modelStatus: () => ({ configured: true }), rpc(method, params, scope) {
    if (method === "self.reply") return { id: "r1", state: "done", result: { messages: ["嗯"] }, sources: [] };
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 } });
  } }); await settle(); await env.click("tab-trial");
  env.get("trialInput").value = "在吗\n\n最近怎么样   \n"; await env.click("trialSend");
  assert.deepEqual(env.calls.find(call => call.method === "self.reply").params,
                   { messages: [{ from: "friend", text: "在吗" }, { from: "friend", text: "最近怎么样" }] });
  assert.deepEqual(bubbles(env), [["friend", "在吗", "最近怎么样"], ["me", "嗯"]]);
  assert.equal(env.get("trialInput").value, "");
});
test("trial failures stay in the chat, a missing model shows the settings hint, and an account switch drops a pending reply", async () => {
  let reply = () => { throw Object.assign(new Error("尚未配置回复模型，请先在模型设置中配置"), { code: "api-not-configured" }); };
  const env = environment({ modelStatus: () => ({ configured: null }), rpc(method, params, scope) {
    if (method === "self.reply") return reply();
    if (method === "self.replyStatus") return { id: "r9", state: "done", result: { messages: ["不该出现在乙账号"] } };
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 } });
  } }); await settle(); await env.click("tab-trial");
  assert.equal(env.get("modelHint").hidden, true);
  env.get("trialInput").value = "在吗"; await env.click("trialSend");
  assert.deepEqual(bubbles(env), [["friend", "在吗"], ["me", "尚未配置回复模型，请先在模型设置中配置"]]);
  assert.equal(env.get("modelHint").hidden, false); assert.equal(env.get("trialSend").disabled, false);
  await env.click("modelSettings"); assert.deepEqual(env.hostCalls.at(-1), { method: "settings.open", params: { section: "model", account: "account-a" } });
  reply = () => ({ id: "r3", state: "error", error: "模型服务认证失败，请检查模型设置" });
  env.get("trialInput").value = "还在吗"; await env.click("trialSend");
  assert.deepEqual(bubbles(env).at(-1), ["me", "模型服务认证失败，请检查模型设置"]);
  reply = () => ({ id: "r9", state: "running" });
  env.get("trialInput").value = "最后一条"; await env.click("trialSend");
  await env.context({ account: "account-b", user: "" }); assert.equal(env.timers.size, 0);
  await env.fireTimers(); assert.deepEqual(bubbles(env), []);
  assert.equal(env.calls.filter(call => call.method === "self.replyStatus").length, 0);
});
test("no-sample and disabled-plugin states remain truthful and do not start import loops", async () => {
  const empty = environment({ rpc: (method, params, scope) => status(scope.account, { build: { state: "complete" }, coverage: { complete: true } }) }); await settle();
  assert.match(empty.get("buildMessage").textContent, /没有找到可用的本人文字回复/); assert.equal(empty.get("search").disabled, true); assert.equal(empty.timers.size, 0);
  assert.equal(empty.get("trialInput").disabled, true); assert.equal(empty.get("chatEmpty").textContent, "从顶部开始分析，整理表达样本后即可试用");
  const disabled = environment({ rpc() { throw Object.assign(new Error("plugin disabled"), { code: "plugin-disabled" }); } }); await settle();
  assert.match(disabled.get("error").textContent, /尚未启用/); assert.equal(disabled.get("analysisRun").disabled, true); assert.equal(disabled.timers.size, 0);
  assert.deepEqual(disabled.calls.map(call => call.method), ["self.status", "self.analysisStatus", "self.analysisSettings"]);
});
test("settings navigation reuses the Core model and source entry points", async () => {
  const env = environment(); await settle(); await env.click("modelSettings"); await env.click("sourceSettings");
  assert.deepEqual(env.hostCalls.slice(-2), [{ method: "settings.open", params: { section: "model", account: "account-a" } }, { method: "settings.open", params: { section: "source", account: "account-a" } }]);
});
test("an account is not required to connect chat or open the shared model settings", async () => {
  const env = environment(); await settle(); await env.context({ account: "", user: "" });
  assert.equal(env.get("analysisRun").disabled, true); assert.equal(env.get("background").disabled, true);
  await env.click("sourceSettings"); await env.click("modelSettings");
  assert.deepEqual(env.hostCalls.slice(-2), [{ method: "settings.open", params: { section: "source", account: "" } }, { method: "settings.open", params: { section: "model", account: "" } }]);
});

test("only an explicitly missing model configuration shows a hint, and it never gates building", async () => {
  for (const [configured, hidden] of [[true, true], [false, false], [null, true]]) {
    const env = environment({ modelStatus: () => ({ configured }), rpc: (method, params, scope) => status(scope.account, { model: { ready: configured !== true } }) });
    await settle();
    assert.equal(env.get("modelHint").hidden, hidden);
    assert.equal(env.get("analysisRun").disabled, false, "saved model configuration must not gate building");
    assert.deepEqual(env.hostCalls.filter(call => call.method === "model.status"), [{ method: "model.status", params: {} }]);
    assert.deepEqual(env.calls.map(call => call.method), ["self.status", "self.analysisStatus", "self.analysisSettings", "self.draftStatus"]);
  }
});
test("model configuration errors stay quiet and are read again on showing the window without a chat account", async () => {
  const marker = "private-provider-error";
  const env = environment({ modelStatus() { throw new Error(marker); } }); await settle();
  assert.equal(env.get("modelHint").hidden, true); assert.doesNotMatch(text(env.get("panel-trial")), new RegExp(marker));
  await env.context({ account: "", user: "" }); env.modelStatus = () => ({ configured: false });
  await env.shown();
  assert.equal(env.get("modelHint").hidden, false);
  assert.equal(env.get("analysisRun").disabled, true, "a chat account remains necessary for building");
  assert.equal(env.get("sourceHint").hidden, false, "without an account the chat connection is the first thing to set up");
});
test("model configuration is re-read on showing the window or opening 聊天试用 and ignores an older read", async () => {
  const old = deferred(); let reads = 0;
  const env = environment({ modelStatus: () => ++reads === 1 ? old.promise : { configured: false },
    rpc: (method, params, scope) => status(scope.account, { build: { state: "running" } }) });
  await settle(); assert.equal(env.get("modelHint").hidden, true);
  await env.shown(); assert.equal(env.get("modelHint").hidden, false);
  old.resolve({ configured: true }); await settle(); assert.equal(env.get("modelHint").hidden, false);
  await env.fireTimers(); assert.equal(reads, 2, "build progress polling does not read or test a model connection");
  await env.click("tab-trial"); assert.equal(reads, 3);
});
test("a pending pause shows saved-batch progress before ordinary running text", async () => {
  const env = environment({ rpc: (method, params, scope) => status(scope.account, { build: { state: "running", stopping: true } }) }); await settle();
  assert.match(env.get("buildMessage").textContent, /正在保存本批进度/);
});
test("profile character limits match Core and reject oversized drafts before a save call", async () => {
  assert.match(html, /id="background"[^>]*maxlength="4000"/); assert.match(html, /id="style"[^>]*maxlength="4000"/);
  const env = environment(); await settle(); env.input("background", "字".repeat(4001)); await env.click("saveProfile");
  assert.match(env.get("profileError").textContent, /4000/); assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});
test("unverified source readiness permits an explicit build attempt without claiming connected", async () => {
  const env = environment({ rpc: (method, params, scope) => status(scope.account, { source: { ready: null, canBuild: true, message: "账号已确认，读取范围将在建立时检查" } }) }); await settle();
  assert.equal(env.get("sourceHint").hidden, true); assert.equal(env.get("analysisRun").disabled, false);
  assert.deepEqual(env.calls.map(call => call.method), ["self.status", "self.analysisStatus", "self.analysisSettings", "self.draftStatus"]);
});
test("interrupted and partially completed runs retain distinct status and retry through the top", async () => {
  for (const state of ["paused", "interrupted", "partial"]) {
    const env = environment({ rpc(method, params, scope) {
      if (method === "self.analysisStatus") return analysisStatus(state, [{ title: "本人资料", state: "error", error: "模型服务暂不可用" }]);
      if (method === "self.analyze") return analysisStatus("running", [{ title: "本人资料", state: "running" }]);
      return status(scope.account);
    } }); await settle();
    assert.equal(env.get("analysisRun").textContent, "继续分析"); assert.match(text(env.get("analysisSteps")), /模型服务暂不可用/);
    await env.click("analysisRun"); assert.equal(env.calls.filter(call => call.method === "self.analyze").length, 1);
  }
});

test("unified progress refreshes library counts while leaving existing samples usable", async () => {
  let count = 3, run = analysisStatus("running", [{ title: "表达知识库", state: "running" }]);
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    return status(scope.account, { build: { state: "complete", processedConversations: 7, totalConversations: 7 }, coverage: { sampleCount: count } });
  } }); await settle(); assert.equal(env.get("sampleCount").textContent, "3"); assert.equal(env.get("search").disabled, false);
  count = 8; run = analysisStatus("done", [{ title: "表达知识库", state: "done" }]); await env.fireTimers();
  assert.equal(env.get("sampleCount").textContent, "8"); assert.equal(env.get("processedCount").textContent, "7");
  assert.equal(env.get("analysisRun").textContent, "更新分析");
});

test("model or local-work failures appear per step without presenting successful steps as failed", async () => {
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return analysisStatus("partial", [
      { key: "library", title: "表达知识库", state: "done", detail: "已有样本可用" },
      { key: "profile", title: "本人资料", state: "error", error: "模型尚未配置" },
      { key: "persona", title: "性格分析", state: "skipped", detail: "等待模型配置" }]);
    return status(scope.account);
  } }); await settle();
  assert.deepEqual(env.get("analysisSteps").children.map(item => item.children[1].textContent), ["完成", "未完成"], "a skipped step never runs, so it is not listed");
  assert.equal(env.get("analysisProgress").hidden, true); assert.equal(env.get("analysisProgress").value, undefined);
  assert.match(env.get("analysisMessage").textContent, /部分步骤未完成/);
});

test("users can exclude a historical expression and restore it from the library page", async () => {
  const sample = { id: "sample-a", name: "合成好友", input: "可以帮忙吗？", reply: "好呀" };
  let excluded = false;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.search") return { sources: excluded ? [] : [sample] };
    if (method === "self.exclusions") return { sources: excluded ? [sample] : [] };
    if (method === "self.exclude") excluded = params.excluded;
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: excluded ? 0 : 1 } });
  } }); await settle();
  await env.click("tab-build"); assert.equal(env.get("exclusionsPanel").hidden, true, "nothing excluded takes no room");
  env.input("query", "帮忙"); await env.click("search");
  const exclude = excludeButton(env.get("samples").children[0]);
  await Promise.all(exclude.listeners.click.map(callback => callback({}))); await settle();
  assert.equal(excluded, true); assert.equal(env.get("samples").children.length, 0);
  assert.match(env.get("searchMessage").textContent, /可在“表达知识库”恢复/);
  assert.deepEqual(env.calls.find(call => call.method === "self.exclude").params, { id: "sample-a", excluded: true });
  await env.click("tab-build"); assert.equal(env.get("exclusionsPanel").hidden, false); assert.equal(env.get("excludedSamples").children.length, 1);
  const restore = excludeButton(env.get("excludedSamples").children[0]);
  await Promise.all(restore.listeners.click.map(callback => callback({}))); await settle();
  assert.equal(excluded, false); assert.equal(env.get("excludedSamples").children.length, 0);
  assert.equal(env.get("exclusionsPanel").hidden, true, "restoring the last sample folds the list away");
  assert.equal(env.calls.filter(call => call.method === "self.exclude").at(-1).params.excluded, false);
});
test("a failed excluded-list read says so on the library page, and clearing the library removes the list", async () => {
  let failing = true;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.exclusions") { if (failing) throw new Error("读取失败"); return { sources: [{ id: "sample-a", name: "合成好友", input: "在吗", reply: "在" }] }; }
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 1 } });
  } }); await settle();
  await env.click("tab-build"); assert.equal(env.get("exclusionsPanel").hidden, false); assert.ok(env.get("exclusionMessage").textContent);
  failing = false; await env.click("tab-trial"); await env.click("tab-build");
  assert.equal(env.get("exclusionMessage").textContent, ""); assert.equal(env.get("excludedSamples").children.length, 1);
  await env.click("reset"); await env.click("confirmReset");
  assert.equal(env.get("exclusionsPanel").hidden, true); assert.equal(env.get("excludedSamples").children.length, 0);
});
test("coverage shows only known historical gaps, without private file names", async () => {
  for (const buildState of ["running", "paused", "partial", "failed", "complete"]) {
    const env = environment({ rpc: (method, params, scope) => status(scope.account, { build: { state: buildState }, coverage: { sampleCount: 2, skippedMessages: 4, failedShards: [{ shard: "private-file-name" }], unresolvedTables: 3 } }) });
    await settle(); const summary = env.get("coverageMessage").textContent;
    assert.equal(summary, "跳过 4 条消息；1 份历史文件未能读取；3 处历史记录尚未识别。"); assert.equal(env.get("coverageMessage").hidden, false);
  }
  const clean = environment({ rpc: (method, params, scope) => status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 } }) });
  await settle(); assert.equal(clean.get("coverageMessage").hidden, true);
});
test("one unified action prepares local semantic resources while a missing model leaves search usable", async () => {
  const bytes = 1024 * 1024;
  let value = status("account-a", { embedding: { state: "missing", ready: false, indexState: "idle" } });
  const env = environment({ modelStatus: () => ({ configured: false }), rpc(method) {
    if (method === "self.analyze") {
      value = status("account-a", { build: { state: "running" }, coverage: { sampleCount: 3 },
        embedding: { state: "downloading", ready: false, indexState: "waiting", totalBytes: 96 * bytes, downloadedBytes: 24 * bytes },
        retrieval: { mode: "lexical", semanticReady: false, indexedSamples: 0, pendingSamples: 3, totalSamples: 3 } });
      return analysisStatus("running", [{ title: "表达知识库", state: "running" }]);
    }
    return value;
  } }); await settle(); await env.click("analysisRun");
  assert.equal(env.get("downloadProgress").max, 96 * bytes); assert.equal(env.get("downloadProgress").value, 24 * bytes);
  assert.match(env.get("downloadMessage").textContent, /24\.0 MB \/ 96\.0 MB/);
  assert.equal(env.get("search").disabled, false); assert.equal(env.get("semanticState").textContent, "正在下载资源");
  assert.equal(env.get("analysisModelHint").hidden, false); assert.equal(env.calls.filter(call => call.method === "self.analyze").length, 1);
});

test("a paused unified run still shows the shared resource download separately", async () => {
  let run = analysisStatus("running", [{ title: "表达知识库", state: "running" }]);
  let value = status("account-a", { build: { state: "complete" }, coverage: { sampleCount: 2 },
    embedding: { state: "downloading", ready: false, indexState: "waiting", totalBytes: 96, downloadedBytes: 48 },
    retrieval: { mode: "lexical", semanticReady: false, indexedSamples: 0, pendingSamples: 2, totalSamples: 2 } });
  const env = environment({ rpc(method) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analysisPause") { run = { ...run, state: "paused" }; value = { ...value, embedding: { ...value.embedding, indexState: "paused" } }; return run; }
    return value;
  } }); await settle(); await env.click("analysisPause");
  assert.equal(env.get("analysisRun").textContent, "继续分析"); assert.equal(env.get("semanticState").textContent, "正在下载资源");
  assert.equal(env.get("downloadProgress").value, 48); assert.equal(env.get("progress").hidden, true);
});

test("a semantic failure keeps lexical search and directs retry to the unified entry", async () => {
  let value = status("account-a", { build: { state: "complete" }, coverage: { sampleCount: 2 },
    embedding: { state: "error", ready: false, indexState: "waiting", error: "资源校验未完成" },
    retrieval: { mode: "lexical", semanticReady: false, indexedSamples: 0, pendingSamples: 2, totalSamples: 2 } });
  const env = environment({ rpc(method) {
    if (method === "self.analyze") { value = { ...value, embedding: { ...value.embedding, state: "downloading", error: "" } }; return analysisStatus("running"); }
    return value;
  } }); await settle();
  assert.equal(env.get("search").disabled, false); assert.match(env.get("semanticError").textContent, /从顶部继续分析.*仍可按词语检索/);
  await env.click("analysisRun"); assert.equal(env.get("semanticState").textContent, "正在下载资源");
  assert.equal(env.calls.some(call => call.method === "self.build"), false);
});

test("unknown download totals and inconsistent readiness cannot claim semantic completion", async () => {
  let value = status("account-a", { build: { state: "complete" }, coverage: { sampleCount: 2 },
    embedding: { state: "downloading", ready: false, indexState: "waiting", downloadedBytes: 5 * 1024 * 1024 },
    retrieval: { mode: "lexical", semanticReady: false } });
  const env = environment({ rpc: () => value }); await settle();
  assert.equal(env.get("downloadProgress").value, undefined); assert.match(env.get("downloadMessage").textContent, /5\.0 MB，总量待确认/);
  value = { ...value, embedding: { state: "future-state", ready: true, indexState: "ready" }, retrieval: { mode: "hybrid", semanticReady: true, indexedSamples: 2, pendingSamples: 0, totalSamples: 2 } };
  await env.fireTimers();
  assert.equal(env.get("semanticState").textContent, "待检查"); assert.equal(env.get("buildState").textContent, "历史已整理");
  assert.equal(env.get("downloadProgress").hidden, true);
});
test("semantic match labels come from actual result provenance and limited scans are disclosed", async () => {
  let result = { mode: "lexical", sources: [{ name: "合成好友", input: "要聚餐吗", reply: "这次先不了", matchKinds: ["lexical"] }] };
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.search") return result;
    return status(scope.account, { build: { state: "complete" }, coverage: { sampleCount: 2 },
      embedding: { state: "ready", ready: true, indexState: "ready" }, retrieval: { mode: "hybrid", semanticReady: true, indexedSamples: 2, pendingSamples: 0, totalSamples: 2 } });
  } }); await settle(); env.input("query", "婉拒邀约"); await env.click("search");
  assert.doesNotMatch(text(env.get("samples")), /含义相近/);
  result = { mode: "hybrid", semanticPartial: true, sources: [{ name: "合成好友", input: "要聚餐吗", reply: "这次先不了", matchKinds: ["semantic"] }] };
  await env.click("search"); assert.match(text(env.get("samples")), /含义相近/); assert.match(env.get("searchMessage").textContent, /语义索引尚未完成/);
  result = { mode: "lexical", sources: [{ name: "合成好友", input: "要聚餐吗", reply: "这次先不了", matchKinds: "semantic" }] };
  await env.click("search"); assert.doesNotMatch(text(env.get("samples")), /含义相近/);
});
test("a pending unified pause prevents duplicate starts until the pause completes", async () => {
  const pending = deferred();
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return analysisStatus("running", [{ title: "表达知识库", state: "running" }]);
    if (method === "self.analysisPause") return pending.promise;
    return status(scope.account);
  } }); await settle(); await env.click("analysisPause");
  assert.equal(env.get("analysisRun").disabled, true); assert.equal(env.get("analysisPause").disabled, true);
  assert.equal(env.get("analysisPause").textContent, "正在暂停…");
  pending.resolve(analysisStatus("paused")); await settle();
  assert.equal(env.get("analysisRun").disabled, false); assert.equal(env.get("analysisRun").textContent, "继续分析");
});

test("the 标签 tab reads unified results, follows their progress and keeps category and semantic grouping", async () => {
  let started = false, done = false;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analyze") { started = true; return analysisStatus("running", [{ title: "标签", state: "running" }]); }
    if (method === "self.analysisStatus") return analysisStatus(done ? "done" : started ? "running" : "idle");
    if (method === "self.tags") return !started ? tagStatus({ state: "idle" }) : !done ? tagStatus({ state: "running", phase: "extracting", done: 40, total: 120 }) :
      tagStatus({ state: "complete" }, [tagItem("篮球", "运动健身", "confirmed"), tagItem("跑步", "运动健身", "possible", { relation: "occasional", mentions: 1 }), tagItem("象棋", "游戏棋牌")]);
    return status(scope.account);
  } }); await settle(); await env.click("tab-tags");
  assert.equal(env.get("tagsRun").hidden, true); assert.equal(env.get("tagsStart").hidden, true);
  await env.click("analysisRun"); assert.equal(env.get("tagsMessage").textContent, "正在提取标签 40 / 120");
  done = true; await env.fireTimers();
  assert.deepEqual(env.get("tagList").children.map(group => group.children[0].textContent), ["运动健身", "游戏棋牌"]);
  assert.deepEqual(chips(env).map(item => [item.textContent, item.className, text(item).slice(item.textContent.length)]),
    [["篮球", "chip confirmed", "明确兴趣3 条"], ["跑步", "chip possible", "偶尔参与1 条"], ["象棋", "chip strong", "明确兴趣3 条"]]);
  assert.equal(env.calls.some(call => call.method === "self.tagsAnalyze"), false);
});

test("tag relations remain explicit despite high counts, confirmation or a legacy analysis", async () => {
  const tags = [tagItem("电影票", "其他", "possible", { relation: "mention", mentions: 90, chats: 8, days: 30 }),
    tagItem("看展", "其他", "possible", { relation: "occasional" }), tagItem("篮球", "其他"),
    tagItem("夜猫子", "其他", "strong", { relation: "trait" }),
    tagItem("旧象棋", "其他", "confirmed", { relation: undefined }),
    tagItem("未知类型", "其他", "possible", { relation: "__proto__" }),
    tagItem("自己填写", "其他", "confirmed", { relation: "uncertain", manual: true, mentions: 0 })];
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.tags") return tagStatus({ state: "complete", needsReview: true }, tags);
    if (method === "self.tagEvidence") return { items: [], total: 0 };
    return status(scope.account);
  } }); await settle(); await env.click("tab-tags");
  assert.equal(chip(env, "电影票").title, "仅提及 · 可能");
  assert.equal(text(chip(env, "电影票")), "电影票仅提及90 条", "message totals do not turn a mention into an interest");
  assert.deepEqual(chips(env).map(item => item.children[0].textContent), ["仅提及", "偶尔参与", "明确兴趣", "明确特征", "待判断", "待判断", "手动添加"]);
  assert.equal(chip(env, "旧象棋").className, "chip confirmed");
  assert.equal(chip(env, "旧象棋").title, "待判断 · 已确认", "confirmation keeps its own dimension");
  assert.equal(env.get("tagsReview").hidden, false);
  for (const listener of chip(env, "电影票").listeners.click) listener(); await settle();
  assert.match(env.get("tagStats").textContent, /^其他 · 仅提及 · 可能 · 关联消息 90 条 · 8 个聊天/);
  assert.equal(html.includes("消息数量仅作统计"), true);
  await env.context({ account: "account-b", user: "" });
  assert.equal(env.get("tagsReview").hidden, true, "the review notice belongs to the original account");
});
test("tag evidence exposes its own relation, follow-up, anchored quote, reason and truncation as plain text", async () => {
  const injected = '<img src=x onerror=send()>';
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.tags") return tagStatus(undefined, [tagItem("科幻", "影音娱乐", "possible", { relation: "uncertain" })]);
    if (method === "self.tagEvidence") return { total: 1, items: [{ chat: "合成朋友", timeMs: 1000,
      context: "对方：要不要看电影", text: "偶尔陪朋友去看" + injected, followup: ["对方：你喜欢吗", "我：还不好说"],
      relation: "uncertain", quote: "偶尔陪朋友去看" + injected, reason: "只有单次参与，无法判断兴趣" + injected, truncated: true }] };
    return status(scope.account);
  } }); await settle(); await env.click("tab-tags");
  for (const listener of chip(env, "科幻").listeners.click) listener(); await settle();
  const card = env.get("tagEvidence").children[0];
  assert.match(card.children[0].children[0].textContent, / · 待判断$/);
  assert.deepEqual(card.children.slice(1).map(item => item.textContent), ["对方：要不要看电影", "我：偶尔陪朋友去看" + injected,
    "后续：\n对方：你喜欢吗\n我：还不好说", "支持原话：偶尔陪朋友去看" + injected,
    "判断理由：只有单次参与，无法判断兴趣" + injected, "原文过长，本次仅分析部分文字，语义待判断。"]);
  assert.equal(card.children.slice(1).every(item => item.children.length === 0), true, "chat and model text never becomes markup");
  assert.equal(env.get("tagsReview").hidden, true);
});
test("a selected tag shows its evidence as text, and confirm, delete, undo and add go through one edit call", async () => {
  const injected = '<i onclick=send()>';
  let tags = [tagItem("篮球", "运动健身"), tagItem(injected, "其他", "possible")];
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.tags") return tagStatus(undefined, tags);
    if (method === "self.tagEvidence") return { name: params.name, total: 60, items: [{ id: "g/m1", chat: "篮球群", isGroup: true, timeMs: 1000, context: "张三：周末打球吗", text: "我来" + injected, explicit: true }] };
    if (method === "self.tagEdit") {
      if (params.action === "confirm") tags = [tagItem("篮球", "运动健身", "confirmed"), tags[1]];
      if (params.action === "delete") tags = tags.filter(tag => tag.name !== params.name);
      if (params.action === "reset") tags = [tagItem("篮球", "运动健身"), ...tags];
      if (params.action === "add") tags = [...tags, tagItem("夜猫子", params.category, "confirmed", { manual: true, mentions: 0, chats: 0, days: 0, firstMs: null, lastMs: null })];
      return tagStatus(undefined, tags);
    }
    return status(scope.account);
  } }); await settle(); await env.click("tab-tags");
  assert.ok(chip(env, injected), "tag names render as text"); assert.equal(env.get("tagDetailEmpty").hidden, false);
  for (const listener of chip(env, "篮球").listeners.click) listener(); await settle();
  assert.equal(env.get("tagTitle").textContent, "篮球"); assert.match(env.get("tagStats").textContent, /^运动健身 · 明确兴趣 · 较可靠 · 关联消息 3 条 · 2 个聊天 · /);
  assert.deepEqual(env.calls.find(call => call.method === "self.tagEvidence").params, { name: "篮球" });
  const card = env.get("tagEvidence").children[0];
  assert.deepEqual(card.children.slice(1).map(node => node.textContent), ["张三：周末打球吗", "我：我来" + injected]);
  assert.match(card.children[0].children[0].textContent, /^篮球群（群） · /);
  assert.equal(env.get("tagEvidence").children.at(-1).textContent, "共 60 条，显示最近 1 条");
  assert.equal(chip(env, "篮球").attributes["aria-pressed"], "true");
  await env.click("confirmTag");
  assert.deepEqual(env.calls.at(-1).params, { action: "confirm", name: "篮球" }); assert.equal(env.get("confirmTag").textContent, "取消确认");
  await env.click("deleteTag");
  assert.deepEqual(env.calls.at(-1).params, { action: "delete", name: "篮球" }); assert.equal(chip(env, "篮球"), undefined);
  assert.equal(env.get("tagUndo").hidden, false); assert.equal(env.get("tagUndoText").textContent, "已删除“篮球”"); assert.equal(env.get("tagActions").hidden, true);
  await env.click("undoTag");
  assert.deepEqual(env.calls.filter(call => call.method === "self.tagEdit").at(-1).params, { action: "reset", name: "篮球" });
  assert.equal(env.get("tagUndo").hidden, true); assert.equal(env.get("tagTitle").textContent, "篮球"); assert.equal(env.calls.at(-1).method, "self.tagEvidence");
  await env.click("addTag"); assert.equal(env.get("tagForm").hidden, false);
  assert.deepEqual(env.get("tagCategory").children.map(option => option.value), ["运动健身", "游戏棋牌", "性格习惯", "其他"]); assert.equal(env.get("tagCategory").value, "其他");
  env.get("tagName").value = " 夜猫子 "; env.get("tagCategory").value = "性格习惯"; await env.click("saveTag");
  assert.deepEqual(env.calls.at(-1).params, { action: "add", name: "夜猫子", category: "性格习惯" });
  assert.equal(env.get("tagForm").hidden, true); assert.equal(env.get("tagTitle").textContent, "夜猫子");
  assert.equal(env.get("tagStats").textContent, "性格习惯 · 已确认 · 手动添加"); assert.equal(env.get("confirmTag").hidden, true);
  assert.equal(env.calls.at(-1).method, "self.tagEdit", "a manual tag has no evidence to read");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});
test("tag failures preserve old results, and an account switch rejects another account's tag response", async () => {
  const pending = deferred();
  const env = environment({ modelStatus: () => ({ configured: false }), rpc(method, params, scope) {
    if (method === "self.tags") return scope.account === "account-b" ? pending.promise : tagStatus({ state: "failed", error: "模型服务认证失败，请检查模型设置" }, [tagItem("篮球", "运动健身")]);
    return status(scope.account);
  } }); await settle(); await env.click("tab-tags");
  assert.equal(env.get("tagsMessage").textContent, "模型服务认证失败，请检查模型设置"); assert.ok(chip(env, "篮球"));
  assert.equal(env.get("tagModelHint").hidden, false);
  await env.click("tagModelSettings"); assert.deepEqual(env.hostCalls.at(-1), { method: "settings.open", params: { section: "model", account: "account-a" } });
  await env.context({ account: "account-b", user: "" }); await env.click("tab-tags");
  assert.deepEqual(chips(env), []); pending.resolve(tagStatus(undefined, [tagItem("旧账号标签", "其他")], "account-a")); await settle();
  assert.deepEqual(chips(env), []);
});

const cardTitle = card => card.children[0].children[0].textContent;
const byClass = (item, name) => item.className.split(" ").includes(name) ? item : item.children.map(child => byClass(child, name)).find(Boolean);
async function openEditor(env, kind, key) { await env.click(kind + "-edit-" + key); }
function editor(env, kind, key) { const manual = byClass(env.get(kind + "-" + key), "persona-manual"); return { input: manual.children[0], save: manual.children[1].children[0], cancel: manual.children[1].children[1] }; }
const chartTitles = (env, id) => env.get(id).children[0].children.map(card => card.children[0].children[0].textContent);
async function openChart(env, id, title) {
  const card = env.get(id).children[0].children.find(item => item.children[0].children[0].textContent === title);
  for (const callback of card.listeners.click) callback({}); await settle();
}
const dimension = (value, key) => value.dimensions.find(item => item.key === key);
test("性格分析 charts the Big Five as MBTI scales and opens self-report, inference, support and contrary context separately and safely", async () => {
  const injected = '<img src=x onerror=send()>', evidence = { user: "合成朋友", id: "m1", time: 1000, quote: "我自述 INFP" + injected,
    reason: "只是本人自述" + injected, truncated: true,
    contextBefore: [{ side: "other", speaker: "合成群友", text: "这是谁的性格", truncated: false }],
    contextAfter: [{ side: "self", speaker: "本人", text: "我说的是自己", truncated: false }] };
  const persona = personaStatus();
  persona.selfReports.mbti = { text: "INFP", confidence: "本人自述", scope: "明确自述", evidence: [evidence], counter: [], conflicting: false };
  dimension(persona, "big5_e").inference = { text: "偏内向", confidence: "把握较低", scope: "仅限所见聊天表现", position: -0.3,
    evidence: [evidence], counter: [{ ...evidence, quote: "也常主动组织活动" }] };
  const env = environment({ rpc(method, params, scope) { if (method === "self.persona") return persona; return status(scope.account); } });
  await settle(); await env.click("tab-persona");
  assert.deepEqual(chartTitles(env, "personaCharts"), ["MBTI", "依恋类型"]);
  assert.equal(env.get("personaList").children.length, 0, "dimensions open from their chart");
  assert.doesNotMatch(text(env.get("personaCharts")), /I\?\?\?/,
    "a partly known scale is not a type; the differing self-report stays in its own card");
  await openChart(env, "personaCharts", "MBTI");
  const cards = env.get("personaList").children;
  assert.deepEqual(cards.map(cardTitle), ["能量", "心智", "天性", "应对方式", "身份特征", "MBTI"]);
  assert.match(text(cards[0]), /偏内向.*把握较低 · 1 条依据/s);
  assert.doesNotMatch(text(cards[0]), /AI 推测|仅限所见聊天/, "a single conclusion needs no label, boilerplate scope is hidden");
  assert.match(text(cards[0]), /原话 1.*前文：.*合成群友：这是谁的性格.*本人原话：我自述 INFP.*后文：.*本人：我说的是自己/s);
  assert.match(text(cards[0]), /反例 1.*也常主动组织活动/s);
  assert.ok(text(cards[0]).includes(injected)); assert.equal(text(cards[1]), "心智暂无依据修正", "an empty dimension is one line");
  assert.match(text(cards[5]), /自述.*INFP/s);
  assert.doesNotMatch(text(cards[5]), /I\?\?\?/, "no derived type column without all four letters");
  await env.click("tab-values");
  assert.deepEqual(chartTitles(env, "valuesCharts"), ["政治光谱", "价值观"]);
  assert.equal(env.get("valuesChartList").children.length, 0, "chart dimensions open on demand");
  assert.deepEqual(env.get("valuesList").children.map(cardTitle), ["感情观", "家庭观", "金钱观", "人生规划", "价值底色"],
    "the five 三观 text items stay below the charts");
  assert.equal(env.calls.some(call => ["self.analyze", "self.draft", "self.tagsAnalyze"].includes(call.method)), false);
});
test("persona edits survive refresh and conflicts, save with the current version and allow clearing a correction", async () => {
  let version = 1, manual = "原修正", conflict = true;
  const value = () => { const result = personaStatus({ version }); dimension(result, "big5_e").manual = manual ? { text: manual, updatedAt: 1000 } : null; return result; };
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.persona") return value();
    if (method === "self.personaEdit") {
      assert.equal(params.key, "big5_e"); assert.equal(params.expectedVersion, version);
      if (conflict) { conflict = false; version++; throw Object.assign(new Error("版本冲突"), { code: "conflict" }); }
      manual = params.text; version++; return value();
    }
    return status(scope.account);
  } }); await settle(); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  assert.match(text(env.get("persona-big5_e")), /编辑修正.*我的理解：原修正/s, "a saved correction is shown, its editor stays closed");
  await openEditor(env, "persona", "big5_e");
  const controls = () => editor(env, "persona", "big5_e");
  assert.equal(controls().input.value, "原修正");
  const input = text => { const node = controls().input; node.value = text; for (const callback of node.listeners.input) callback({}); };
  const save = async () => { for (const callback of controls().save.listeners.click) callback({}); await settle(); };
  input("我的未保存修正"); manual = "外部更新的修正"; await env.shown();
  assert.equal(controls().input.value, "我的未保存修正");
  await save(); assert.match(env.get("personaMessage").textContent, /修正已保留/); assert.equal(env.get("personaReload").hidden, false);
  assert.equal(controls().input.value, "我的未保存修正");
  await env.click("personaReload"); assert.equal(controls().input.value, "我的未保存修正");
  await save(); assert.equal(manual, "我的未保存修正"); assert.equal(env.get("personaMessage").hidden, true);
  assert.match(text(env.get("persona-big5_e")), /我的理解：我的未保存修正/, "saving closes the editor and shows the correction");
  await openEditor(env, "persona", "big5_e"); input(""); await save(); assert.equal(manual, "");
  assert.doesNotMatch(text(env.get("persona-big5_e")), /我的理解/);
  await openEditor(env, "persona", "big5_e"); input("取消的草稿");
  for (const callback of controls().cancel.listeners.click) callback({}); await settle();
  assert.equal(env.get("persona-manual-big5_e"), undefined, "cancel discards the draft and closes the editor");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});
test("persona manual drafts and late responses are isolated across accounts", async () => {
  const old = deferred(); let waiting = false;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.persona") {
      if (scope.account === "account-a" && waiting) return old.promise;
      const value = personaStatus(); dimension(value, "big5_e").manual = { text: scope.account + "的修正", updatedAt: 1000 }; return value;
    }
    return status(scope.account);
  } }); await settle(); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  await openEditor(env, "persona", "big5_e");
  const input = env.get("persona-manual-big5_e"); input.value = "旧账号未保存";
  for (const callback of input.listeners.input) callback({}); waiting = true; await env.click("personaReload");
  await env.context({ account: "account-b", user: "" }); await env.click("tab-persona");
  assert.equal(env.get("personaList").children.length, 0, "the chosen chart does not carry over");
  await openChart(env, "personaCharts", "MBTI");
  const late = personaStatus(); dimension(late, "big5_e").manual = { text: "旧账号迟到内容" };
  old.resolve(late); await settle();
  assert.equal(env.get("persona-manual-big5_e"), undefined, "the other account's editor is closed");
  assert.match(text(env.get("persona-big5_e")), /我的理解：account-b的修正/);
  assert.doesNotMatch(text(env.get("personaList")), /旧账号/);
});
test("persona evidence disclosure states survive refresh independently and reset across accounts", async () => {
  const value = personaStatus();
  const quote = { chat: "合成好友", time: 1000, quote: "我先想想", reason: "合成", contextBefore: [], contextAfter: [] };
  dimension(value, "big5_e").inference = { text: "仅限聊天表现", confidence: "把握较低", position: 0.2, evidence: [quote], counter: [quote] };
  const env = environment({ rpc(method, params, scope) { if (method === "self.persona") return value; return status(scope.account); } });
  await settle(); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  const details = () => env.get("personaList").children[0].children[1].children.flatMap(column => column.children.filter(child => child.tag === "details"));
  const old = details();
  for (const [index, item] of old.entries()) {
    item.open = index !== 1;
    for (const callback of item.listeners.toggle) callback({});
  }
  await env.click("personaReload");
  assert.deepEqual(details().map(item => [item.id, item.open]), [
    ["persona-evidence-big5_e-inference-support", true], ["persona-evidence-big5_e-inference-counter", false]]);
  await env.context({ account: "account-b", user: "" }); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  for (const callback of old[0].listeners.toggle) callback({});
  await env.click("personaReload");
  assert.equal(details().every(item => item.open === false), true, "neither old DOM nor late toggle events leak the previous account's UI state");
});
test("stale persona results ask for an update on both tabs and keep unsaved corrections", async () => {
  const value = personaStatus({ coverage: { ownMessages: 100, analyzedMessages: 100, evidenceCount: 5 } });
  const env = environment({ rpc(method, params, scope) { if (method === "self.persona") return value; return status(scope.account); } });
  await settle(); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  assert.equal(env.get("personaMessage").hidden, true, "no coverage note while results are current");
  await openEditor(env, "persona", "big5_e");
  const input = env.get("persona-manual-big5_e"); input.value = "我自己的修正";
  for (const callback of input.listeners.input) callback({});
  value.coverage = { ...value.coverage, ownMessages: 101, stale: true };
  await env.click("personaReload");
  for (const id of ["personaMessage", "valuesMessage"])
    assert.equal(env.get(id).textContent, "聊天记录已变化，AI 结论待更新；你的修正已保留。");
  assert.equal(env.get("persona-manual-big5_e").value, "我自己的修正");
});
test("an old analysis status cannot restore steps or timers after switching accounts", async () => {
  const old = deferred();
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return scope.account === "account-a" ? old.promise : analysisStatus();
    return status(scope.account);
  } }); await settle(); await env.context({ account: "account-b", user: "" });
  old.resolve(analysisStatus("running", [{ title: "旧账号分析", state: "running" }])); await settle();
  assert.equal(env.get("analysisSteps").children.length, 0); assert.equal(env.get("analysisRun").disabled, false); assert.equal(env.timers.size, 0);
});
test("starting before the profile read omits unknown fields, while a loaded user's cleared fields stay explicit", async () => {
  const profile = deferred();
  const env = environment({ modelStatus: () => ({ configured: false }),
    host: method => method === "self-profile.read" ? profile.promise : { ok: true },
    rpc(method, params, scope) { if (method === "self.analyze") return analysisStatus("done"); return status(scope.account); }
  }); await settle();
  assert.equal(env.get("background").disabled, true); assert.equal(env.get("analysisRun").disabled, false);
  await env.click("analysisRun");
  assert.deepEqual(env.calls.find(call => call.method === "self.analyze").params, { outputs: allOutputs },
    "an unread empty input cannot replace the saved Core profile supplied by the service");
  profile.resolve({ background: "已经保存的本人自述", style: "已经保存的表达偏好", version: 4 }); await settle();
  assert.equal(env.get("background").value, "已经保存的本人自述");
  env.input("background", ""); env.input("style", ""); await env.click("analysisRun");
  assert.deepEqual(env.calls.filter(call => call.method === "self.analyze").at(-1).params, { outputs: allOutputs, background: "", style: "" },
    "after loading, explicitly cleared edits remain an intentional input");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});
test("analysis shows actual per-stage counts and uses only the current known denominator for its progress bar", async () => {
  let run = analysisStatus("running", [
    { key: "history", title: "同步本人文字", state: "done", done: 12, total: 12, unit: "conversations" },
    { key: "profile", title: "本人资料", state: "running", done: 24, total: 120, unit: "messages" },
    { key: "persona", title: "性格分析", state: "pending" }
  ], { phase: "profile" });
  const env = environment({ rpc(method, params, scope) { if (method === "self.analysisStatus") return run; return status(scope.account); } }); await settle();
  assert.doesNotMatch(text(env.get("analysisSteps").children[0]), /12 \/ 12/, "finished steps keep their counts in the tooltip");
  assert.match(tips(env.get("analysisSteps").children[0]), /12 \/ 12 个聊天/);
  assert.match(text(env.get("analysisSteps").children[1]), /24 \/ 120 条/);
  assert.equal(env.get("analysisProgress").hidden, false); assert.equal(env.get("analysisProgress").max, 120); assert.equal(env.get("analysisProgress").value, 24);
  assert.equal(env.get("analysisProgress").attributes["aria-label"], "本人资料进度");
  run = { ...run, steps: [{ key: "profile", title: "本人资料", state: "running", done: 30, unit: "messages" }] };
  await env.fireTimers();
  assert.match(text(env.get("analysisSteps")), /已处理 30 条/); assert.equal(env.get("analysisProgress").hidden, true); assert.equal(env.get("analysisProgress").value, undefined);
  run = { ...run, steps: [{ key: "profile", title: "本人资料", state: "running", done: 50, total: 40, unit: "messages" }] };
  await env.fireTimers();
  assert.match(text(env.get("analysisSteps")), /已处理 50 条/); assert.doesNotMatch(text(env.get("analysisSteps")), /50 \/ 40/); assert.equal(env.get("analysisProgress").hidden, true);
});
test("analysis timing shows observed phase ETA and actual model requests without exposing internal stage names", async () => {
  const now = 1792742400000;
  let run = analysisStatus("running", [
    { key: "history", title: "同步本人文字", state: "done", done: 10, total: 10 },
    { key: "profile", title: "本人资料", state: "running", done: 80, total: 320, modelCalls: 4,
      activeCallStartedAt: now - 32000, activeCallStep: "profile-draft-review", phase: "extract" }
  ], { phase: "profile", elapsedMs: 125000, eta: { scope: "phase", label: "本人资料提取与复核", lowMs: 180000, highMs: 420000, basis: "observed" } });
  const env = environment({ now, rpc(method, params, scope) { if (method === "self.analysisStatus") return run; return status(scope.account); } }); await settle();
  assert.equal(env.get("analysisMessage").textContent, "本人资料 · 已用时 2分5秒 · 约还需 3分–7分");
  assert.match(text(env.get("analysisSteps")), /80 \/ 320 条/);
  assert.match(tips(env.get("analysisSteps")), /10 \/ 10 个聊天.*80 \/ 320 条.*已调用 4 次 · 当前请求已用 32秒/s);
  assert.doesNotMatch(text(env.get("analysisSteps")) + tips(env.get("analysisSteps")), /profile-draft-review|extract/);
  for (const eta of [undefined, { scope: "phase", lowMs: 1000, highMs: 2000, basis: "guessed" },
    { scope: "whole-run", lowMs: 1000, highMs: 2000, basis: "observed" },
    { scope: "phase", lowMs: 2000, highMs: 1000, basis: "observed" }]) {
    run = { ...run, eta }; await env.fireTimers();
    assert.equal(env.get("analysisMessage").textContent, "本人资料 · 已用时 2分5秒", "unmeasured or invalid remaining time stays out of the UI");
  }
});
test("run timing shows only while running and never extends a finished run to the current time", async () => {
  const now = 1792742400000;
  let run = analysisStatus("done", [], { startedAt: now - 90000, finishedAt: now - 10000 });
  const env = environment({ now, rpc(method, params, scope) { if (method === "self.analysisStatus") return run; return status(scope.account); } }); await settle();
  assert.equal(env.get("analysisMessage").textContent, "分析完成", "a finished run does not keep showing its duration");
  run = { ...run, finishedAt: undefined }; await env.shown();
  run = { ...run, state: "running" }; await env.shown();
  assert.equal(env.get("analysisMessage").textContent, "正在分析 · 已用时 1分30秒");
});
test("history progress distinguishes this reader's new messages from the current chat page position", async () => {
  let run = analysisStatus("running", [{ key: "history", title: "同步本人文字", state: "running", done: 15, total: 3473,
    sourceMessagesRead: 9200, currentChatDone: 4800, currentChatTotal: 10000 }], { phase: "history" });
  const env = environment({ rpc(method, params, scope) { if (method === "self.analysisStatus") return run; return status(scope.account); } }); await settle();
  assert.match(text(env.get("analysisSteps")), /15 \/ 3473 个聊天/);
  assert.match(tips(env.get("analysisSteps")), /15 \/ 3473 个聊天.*本轮读取 9200 条 · 当前聊天 4800 \/ 10000 条/s);
  assert.doesNotMatch(text(env.get("analysisSteps")) + tips(env.get("analysisSteps")), /累计/);
  run = { ...run, steps: [{ ...run.steps[0], sourceMessagesRead: 12500, currentChatDone: 8100 }] }; await env.fireTimers();
  assert.match(tips(env.get("analysisSteps")), /本轮读取 12500 条 · 当前聊天 8100 \/ 10000 条/);
  run = { ...run, steps: [{ ...run.steps[0], sourceMessagesRead: "12500", currentChatDone: 50, currentChatTotal: 40 }] }; await env.fireTimers();
  assert.doesNotMatch(tips(env.get("analysisSteps")), /本轮读取|当前聊天/);
  run = { ...run, steps: [{ ...run.steps[0], sourceMessagesRead: 0, currentChatDone: 0, currentChatTotal: 20 }] }; await env.fireTimers();
  assert.match(tips(env.get("analysisSteps")), /本轮读取 0 条 · 当前聊天 0 \/ 20 条/);
  run = { ...run, steps: [{ ...run.steps[0], sourceMessagesVerified: 500, currentChatDone: 0, currentChatTotal: 0 }] }; await env.fireTimers();
  assert.match(tips(env.get("analysisSteps")), /本轮读取 0 条 · 已核对 500 条旧记录/);
  assert.doesNotMatch(tips(env.get("analysisSteps")), /当前聊天/);
});
test("a run-level error shows once in the headline and finished-step details stay out of the toolbar", async () => {
  const error = "已有资料的聊天尚未完成核对，原有资料库已保留，请恢复读取后继续";
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return analysisStatus("partial", [
      { key: "history", title: "同步聊天记录", state: "error", error, done: 314, total: 3473 },
      { key: "library", title: "整理表达知识库", state: "pending" }], { error, elapsedMs: 2072000, coverage: { ownMessages: 304848 } });
    return status(scope.account);
  } }); await settle();
  assert.equal(env.get("analysisMessage").textContent, error); assert.match(env.get("analysisMessage").className, /error-text/);
  assert.equal(text(env.get("analysisSteps")).replace(/继续/g, ""), "同步聊天记录未完成整理表达知识库等待", "only name and state remain besides hover actions");
  assert.match(tips(env.get("analysisSteps").children[0]), /314 \/ 3473 个聊天/);
  assert.equal(html.includes("analysisCoverage"), false);
});
test("parallel semantic indexing keeps its real sample counts while the foreground stage owns the progress bar", async () => {
  const run = analysisStatus("running", [
    { key: "semantic", title: "本地语义索引", state: "running", done: 15, total: 100, unit: "条样本" },
    { key: "profile", title: "本人资料", state: "running", done: 60, total: 120, unit: "条" }
  ], { phase: "profile" });
  const env = environment({ rpc(method, params, scope) { if (method === "self.analysisStatus") return run; return status(scope.account); } }); await settle();
  assert.equal(env.get("analysisSteps").children.filter(item => item.className === "analysis-step running").length, 2);
  assert.match(text(env.get("analysisSteps").children[0]), /15 \/ 100 条样本/);
  assert.equal(env.get("analysisProgress").attributes["aria-label"], "本人资料进度");
  assert.equal(env.get("analysisProgress").value, 60); assert.equal(env.get("analysisProgress").max, 120);
});

test("saved output choices restore without starting work and a library-only run needs no model", async () => {
  const env = environment({ modelStatus: () => ({ configured: false }), rpc(method, params, scope) {
    if (method === "self.analysisSettings") return { outputs: ["library"] };
    if (method === "self.analyze") return analysisStatus("running", [{ key: "library", title: "表达知识库", state: "running", done: 1, total: 8 }], { outputs: params.outputs });
    return status(scope.account);
  } }); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["library"]);
  assert.equal(env.calls.some(call => ["self.analyze", "self.analysisResume", "self.analysisPause"].includes(call.method)), false);
  await env.shown(); assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
  await env.click("analysisRun");
  assert.deepEqual(env.calls.find(call => call.method === "self.analyze").params.outputs, ["library"]);
  assert.equal(env.get("analysisModelHint").hidden, false); assert.equal(env.get("analysisRun").disabled, true);
  for (const key of allOutputs) assert.equal(env.get("analysisOutput-" + key).disabled, true);
});

test("output preferences save canonical choices, including empty selection, and empty cannot start", async () => {
  const env = environment(); await settle();
  for (const key of allOutputs) await env.check(key, false);
  assert.deepEqual(env.calls.filter(call => call.method === "self.analysisSettings").at(-1).params, { outputs: [] });
  assert.equal(env.get("analysisRun").disabled, true); assert.match(env.get("analysisMessage").textContent, /请选择分析内容/);
  assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
  await env.check("library", true); await env.check("profile", true);
  assert.deepEqual(env.calls.filter(call => call.method === "self.analysisSettings").at(-1).params, { outputs: ["profile", "library"] });
  assert.equal(env.get("analysisRun").disabled, false);
});

test("a shared evidence stage shows real progress while only selected output steps have individual controls", async () => {
  const run = analysisStatus("running", [
    { key: "history", title: "同步本人文字", state: "done" },
    { key: "evidence", title: "提取共同证据", state: "running", done: 20, total: 100 },
    { key: "profile", title: "本人资料", state: "pending" }, { key: "tags", title: "标签", state: "pending" },
    { key: "persona", title: "性格", state: "skipped", detail: "未选择" }, { key: "library", title: "表达知识库", state: "skipped" },
  ], { outputs: ["profile", "tags"], phase: "evidence" });
  const env = environment({ rpc(method, params, scope) { if (method === "self.analysisStatus") return run; return status(scope.account); } }); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["profile", "tags"]);
  assert.equal(env.get("analysisProgress").max, 100); assert.equal(env.get("analysisProgress").value, 20);
  assert.match(text(env.get("analysisSteps")), /提取共同证据进行中20 \/ 100 条/);
  assert.equal(env.get("analysisStepPause-profile").disabled, false); assert.equal(env.get("analysisStepPause-tags").disabled, false);
  for (const key of ["history", "evidence", "persona", "library"]) assert.equal(env.get("analysisStepPause-" + key), undefined);
  assert.deepEqual(env.get("analysisSteps").children.map(item => item.children[0].textContent), ["同步本人文字", "提取共同证据", "本人资料", "标签"]);
  assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
});

test("one output pauses and resumes within a running analysis, with in-flight stopping disabled", async () => {
  let run = analysisStatus("running", [{ key: "profile", title: "本人资料", state: "running" }, { key: "tags", title: "标签", state: "pending" }], { outputs: ["profile", "tags"] });
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analysisPause") { run = { ...run, pausedOutputs: [params.output], steps: run.steps.map(step => step.key === params.output ? { ...step, stopping: true } : step) }; return run; }
    if (method === "self.analysisResume") { run = { ...run, pausedOutputs: [], steps: run.steps.map(step => step.key === params.output ? { ...step, state: "running", stopping: false } : step) }; return run; }
    return status(scope.account);
  } }); await settle(); env.input("background", "未保存的本人资料");
  await env.click("analysisStepPause-profile");
  assert.deepEqual(env.calls.find(call => call.method === "self.analysisPause").params, { output: "profile" });
  assert.equal(env.get("analysisStepResume-profile").disabled, true); assert.equal(env.get("analysisStepResume-profile").textContent, "正在暂停…");
  assert.equal(env.get("analysisStepPause-tags").disabled, false);
  run = { ...run, steps: run.steps.map(step => step.key === "profile" ? { ...step, state: "paused", stopping: false } : step) }; await env.fireTimers();
  assert.match(text(env.get("analysisSteps").children[0]), /已暂停/);
  await env.click("analysisStepResume-profile");
  assert.deepEqual(env.calls.find(call => call.method === "self.analysisResume").params, { output: "profile" });
  assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
  assert.equal(env.get("background").value, "未保存的本人资料");
});

test("a paused run can continue only one output with the current unsaved profile input", async () => {
  let run = analysisStatus("paused", [{ key: "profile", title: "本人资料", state: "paused" }, { key: "tags", title: "标签", state: "paused" }], { outputs: ["profile", "tags"], pausedOutputs: ["profile", "tags"] });
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analysisResume") { run = { ...run, state: "running", outputs: [params.output], steps: [{ key: "tags", title: "标签", state: "running" }] }; return run; }
    return status(scope.account);
  } }); await settle(); env.input("background", "保留未保存的自述");
  await env.click("analysisStepResume-tags");
  assert.deepEqual(env.calls.find(call => call.method === "self.analysisResume").params, { output: "tags", background: "保留未保存的自述", style: "" });
  assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
  assert.equal(env.get("background").value, "保留未保存的自述");
  assert.equal(env.hostCalls.some(call => call.method === "self-profile.save"), false);
});

test("continuing one item shows only that item while it runs and the saved choices again once it ends", async () => {
  let run = analysisStatus("partial", [{ key: "profile", title: "本人资料", state: "error" }, { key: "persona", title: "性格分析", state: "error" }], { outputs: ["profile", "persona"] });
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analysisSettings") return { outputs: params.outputs || ["profile", "persona"] };
    if (method === "self.analysisResume") { run = analysisStatus("running", [{ key: "profile", title: "本人资料", state: "running" }], { outputs: [params.output] }); return run; }
    return status(scope.account);
  } }); await settle();
  await env.click("analysisStepResume-profile");
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["profile"]);
  run = analysisStatus("done", [{ key: "profile", title: "本人资料", state: "done" }], { outputs: ["profile"] });
  await env.fireTimers();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["profile", "persona"]);
  await env.click("analysisRun");
  assert.deepEqual(env.calls.find(call => call.method === "self.analyze").params.outputs, ["profile", "persona"]);
});

test("semantic indexing controls the library output after expression samples are complete", async () => {
  let run = analysisStatus("running", [{ key: "library", title: "表达知识库", state: "done" }, { key: "semantic", title: "本地语义索引", state: "running", done: 4, total: 10, unit: "条样本" }], { outputs: ["library"] });
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analysisPause") { run = { ...run, state: "paused", pausedOutputs: ["library"], steps: run.steps.map(step => step.key === "semantic" ? { ...step, state: "paused" } : step) }; return run; }
    if (method === "self.analysisResume") { run = { ...run, state: "running", pausedOutputs: [], steps: run.steps.map(step => step.key === "semantic" ? { ...step, state: "running" } : step) }; return run; }
    return status(scope.account);
  } }); await settle();
  assert.equal(env.get("analysisStepPause-library"), undefined);
  await env.click("analysisStepPause-semantic"); assert.deepEqual(env.calls.find(call => call.method === "self.analysisPause").params, { output: "library" });
  await env.click("analysisStepResume-semantic"); assert.equal(env.calls.find(call => call.method === "self.analysisResume").params.output, "library");
  assert.equal(env.get("analysisProgress").max, 10); assert.equal(env.get("analysisProgress").value, 4);
});

test("late output settings cannot overwrite a user choice, a running selection, or another account", async () => {
  const old = deferred(), running = deferred(); let settingsReads = 0;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisSettings") { if (params.outputs) return { outputs: params.outputs }; return scope.account === "account-a" && settingsReads++ === 0 ? old.promise : { outputs: ["library"] }; }
    if (method === "self.analysisStatus") return scope.account === "account-b" ? running.promise : analysisStatus();
    return status(scope.account);
  } }); await settle(); await env.check("persona", false);
  old.resolve({ outputs: ["persona"] }); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["profile", "tags", "values", "library"]);
  await env.context({ account: "account-b" });
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["library"]);
  running.resolve(analysisStatus("running", [{ key: "profile", title: "本人资料", state: "running" }], { outputs: ["profile"] })); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["profile"]);
  for (const key of allOutputs) assert.equal(env.get("analysisOutput-" + key).disabled, true);
  const late = deferred(); env.rpc = (method, params, scope) => method === "self.analysisSettings" ? scope.account === "account-c" ? late.promise : { outputs: ["library"] } : status(scope.account);
  await env.context({ account: "account-c" }); await env.context({ account: "account-d" });
  late.resolve({ outputs: ["tags"] }); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["library"]);
  assert.equal(env.calls.some(call => call.method === "self.analyze"), false);
});

test("completed run outputs cannot reset newer saved choices and late settings cannot unlock a running run", async () => {
  const later = deferred(); let waiting = false;
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return analysisStatus(waiting ? "running" : "done", [], { outputs: ["profile"] });
    if (method === "self.analysisSettings") return waiting ? later.promise : { outputs: params.outputs || ["library"] };
    return status(scope.account);
  } }); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["library"]);
  await env.check("tags", true); await env.shown();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["tags", "library"]);
  waiting = true; await env.context({ account: "account-b" });
  later.resolve({ outputs: ["library"] }); await settle();
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["profile"]);
  assert.equal(env.get("analysisOutput-library").disabled, true);
});

test("the first start waits for saved output choices, while unavailable settings permit an explicit default start", async () => {
  const saved = deferred();
  const env = environment({ rpc(method, params, scope) { if (method === "self.analysisSettings") return saved.promise; return status(scope.account); } }); await settle();
  assert.equal(env.get("analysisRun").disabled, true);
  saved.resolve({ outputs: ["tags"] }); await settle();
  assert.equal(env.get("analysisRun").disabled, false);
  assert.deepEqual(allOutputs.filter(key => env.get("analysisOutput-" + key).checked), ["tags"]);
  const unavailable = environment({ rpc(method, params, scope) { if (method === "self.analysisSettings") throw new Error("synthetic unavailable"); return status(scope.account); } }); await settle();
  assert.equal(unavailable.get("analysisRun").disabled, false);
  assert.deepEqual(allOutputs.filter(key => unavailable.get("analysisOutput-" + key).checked), allOutputs);
  assert.equal(unavailable.calls.some(call => call.method === "self.analyze"), false);
});

test("the title row holds one split button whose menu chooses outputs, and closes while a run is active", async () => {
  let run = analysisStatus("idle");
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analyze") { run = analysisStatus("running", [{ key: "profile", title: "本人资料", state: "running" }], { outputs: params.outputs }); return run; }
    return status(scope.account);
  } }); await settle();
  assert.ok(html.indexOf('id="analysisRun"') < html.indexOf('id="tabContent"') && html.indexOf('id="analysisMenu"') > html.indexOf('class="page-header"'));
  assert.equal(env.get("analysisMenu").hidden, true); assert.equal(env.get("analysisStatus").hidden, true, "an idle account shows no status text");
  await env.click("analysisMenuToggle");
  assert.equal(env.get("analysisMenu").hidden, false); assert.equal(env.get("analysisMenuToggle").attributes["aria-expanded"], "true");
  await env.check("values", false);
  assert.equal(env.get("analysisMenu").hidden, false, "choosing keeps the menu open");
  env.events.keydown({ key: "Escape" }); await settle();
  assert.equal(env.get("analysisMenu").hidden, true); assert.equal(env.get("analysisMenuToggle").focused, true);
  await env.click("analysisMenuToggle"); await env.click("analysisRun");
  assert.deepEqual(env.calls.find(call => call.method === "self.analyze").params.outputs, ["profile", "tags", "persona", "library"]);
  assert.equal(env.get("analysisMenu").hidden, true); assert.equal(env.get("analysisSplit").hidden, true); assert.equal(env.get("analysisPause").hidden, false);
});

test("the run progress overlay appears only while work runs, reopens from the status text and closes after a clean finish", async () => {
  let run = analysisStatus("idle");
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.analysisStatus") return run;
    if (method === "self.analyze") { run = analysisStatus("running", [{ key: "history", title: "同步聊天记录", state: "running" }, { key: "tags", title: "标签分析", state: "skipped" }]); return run; }
    return status(scope.account);
  } }); await settle();
  assert.equal(env.get("analysisPanel").hidden, true); assert.equal(env.get("analysisSteps").children.length, 0, "no fixed chain sits on the page before a run");
  await env.click("analysisRun");
  assert.equal(env.get("analysisPanel").hidden, false); assert.equal(env.get("analysisStatus").attributes["aria-expanded"], "true");
  assert.deepEqual(env.get("analysisSteps").children.map(item => item.children[0].textContent), ["同步聊天记录"]);
  await env.click("analysisPanelClose"); assert.equal(env.get("analysisPanel").hidden, true); assert.equal(env.get("analysisStatus").focused, true);
  await env.fireTimers(); assert.equal(env.get("analysisPanel").hidden, true, "polling does not reopen a dismissed overlay");
  await env.click("analysisStatus"); assert.equal(env.get("analysisPanel").hidden, false);
  run = { ...run, state: "done", steps: run.steps.map(step => step.state === "running" ? { ...step, state: "done" } : step) }; await env.fireTimers();
  assert.equal(env.get("analysisPanel").hidden, true); assert.equal(env.get("analysisMessage").textContent, "分析完成");
  await env.click("analysisStatus"); assert.equal(env.get("analysisPanel").hidden, false, "a finished run can still be reviewed");
});

test("三观 shows the charts above five text dimensions and saves corrections through its own store", async () => {
  let version = 1, saved = null;
  const value = () => { const result = valuesStatus({ version }); if (saved) result.dimensions[0].manual = { text: saved, updatedAt: 1000 }; return result; };
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.values") return value();
    if (method === "self.valuesEdit") { assert.equal(params.expectedVersion, version); saved = params.text; version++; return value(); }
    if (method === "self.personaEdit") throw new Error("三观 corrections must not reach the persona store");
    return status(scope.account);
  } }); await settle(); await env.click("tab-values");
  assert.deepEqual(chartTitles(env, "valuesCharts"), ["政治光谱", "价值观"], "the charts come from self.persona");
  assert.deepEqual(env.get("valuesList").children.map(cardTitle), ["感情观", "家庭观", "金钱观", "人生规划", "价值底色"]);
  await openEditor(env, "values", "love");
  env.input("values-manual-love", "我更看重长期稳定");
  const save = editor(env, "values", "love").save; assert.equal(save.disabled, false);
  for (const callback of save.listeners.click) callback({}); await settle();
  assert.equal(saved, "我更看重长期稳定"); assert.equal(env.get("valuesMessage").hidden, true);
  assert.match(text(env.get("valuesList").children[0]), /我的理解：我更看重长期稳定/);
  assert.equal(env.calls.find(call => call.method === "self.valuesEdit").params.key, "love");
});

const PERIOD_FACTORS = (value, spread) => Object.fromEntries(["big5_e", "big5_a", "big5_c", "big5_n", "big5_o"].map(key => [key,
  { score: value, low: spread ? value - spread : null, high: spread ? value + spread : null, items: 10, total: 10, chats: 6 }]));
const periodsValue = () => ({ answers: 300, changes: [{ key: "big5_e", from: "2026H1", to: "2026H2", delta: -.4, confirmed: false }], periods: [
  { id: "2026H1", label: "2026 上半年", lines: 900, current: false, passes: 3, items: 50, factors: PERIOD_FACTORS(3.6, .1), letters: ["E", "N", "F", "J"], report: { value: "ENFJ", count: 2, others: [] } },
  { id: "2026H2", label: "2026 下半年", lines: 800, current: true, passes: 3, items: 50, factors: PERIOD_FACTORS(3.2, .1), letters: ["I", "N", "F", "J"], report: { value: "INFJ", count: 1, others: [] } }] });
function walk(root, visit) { visit(root); for (const child of root.children || []) walk(child, visit); }
function findAll(root, className) { const found = []; walk(root, item => { if (String(item.className).split(" ").includes(className)) found.push(item); }); return found; }

test("性格时间线 draws every half-year, picks the newest and opens one question list per dimension", async () => {
  const env = environment({ rpc: (method, params, scope) => {
    if (method === "self.periods") return periodsValue();
    if (method === "self.periodDetail") return { period: params.period, key: params.key, items: [
      { item: 1, text: "我会主动和人搭话", reverse: false, inner: false, answers: [4, null, 3], why: ["常主动开口"], situational: 2,
        evidence: [{ s: 2, k: "行为", name: "合成朋友", time: 1000, text: "我先去打个招呼" }] }] };
    return status(scope.account);
  } });
  await settle();
  env.elements.get("tab-persona").listeners.click[0]({});
  await settle(); await settle();
  const host = env.elements.get("personaPeriods");
  const columns = findAll(host, "kpc-p-col");
  assert.equal(columns.length, 2);
  assert.equal(findAll(host, "on").filter(item => String(item.className).includes("kpc-p-col")).length, 1);
  assert.ok(columns[1].className.includes("on"), "the newest half-year starts selected");
  assert.equal(findAll(host, "kpc-p-seg").map(item => item.textContent).join("|"), "2026|ENFJ|INFJ", "the self-reported 你说过 row is gone; only the year and 读聊天 strips remain");
  assert.equal(env.elements.get("personaPeriodLine").hidden, true, "the chart carries the changes, not a line under it");
  const help = findAll(host, "kpc-help")[0];
  assert.equal(help.open, false, "the reading note stays closed until the ? is pressed");
  assert.match(findAll(help, "kpc-help-note")[0].children[0].textContent, /点一列看那一期/);
  const tones = name => findAll(host, name).map(item => String(item.className).match(/kpc-tone-\d/)?.[0]);
  assert.deepEqual(tones("kpc-p-plot"), ["kpc-tone-0", "kpc-tone-1", "kpc-tone-2", "kpc-tone-3", "kpc-tone-4"], "every scale has its own color");
  assert.deepEqual(tones("kpc-p-name").slice(0, 5), tones("kpc-p-plot"), "a name wears the color of its plot");
  assert.deepEqual(tones("kpc-p-seg"), [undefined, "kpc-tone-5", "kpc-tone-6"], "strip values start on the tones the scales left free");
  findAll(host, "kpc-p-name")[0].listeners.click[0]({});
  await settle(); await settle();
  assert.deepEqual(plain(env.calls.find(call => call.method === "self.periodDetail").params), { period: "2026H2", key: "big5_e" });
  const detail = env.elements.get("personaPeriodDetail");
  assert.equal(detail.children.length, 1);
  assert.ok(JSON.stringify(detail.children[0], (key, value) => typeof value === "function" ? undefined : value).includes("4 · 无依据 · 3"));
  findAll(env.elements.get("personaPeriods"), "kpc-p-col")[0].listeners.click[0]({});
  await settle();
  assert.equal(env.elements.get("personaPeriodDetail").children.length, 0, "another half-year closes the list of the last one");
  assert.ok(findAll(env.elements.get("personaPeriods"), "kpc-p-col")[0].className.includes("on"));
});

test("the general relationship tendency and self-report remain independent of the selected personality period", async () => {
  const persona = personaStatus();
  dimension(persona, "att_anx").inference = { position: .3, text: "在这段关系中会反复寻求确认", scope: "某段关系与时期", confidence: "把握较低", evidence: [], counter: [] };
  dimension(persona, "att_avo").inference = { position: -.2, text: "愿意表达亲近需要", scope: "某段关系与时期", confidence: "把握较低", evidence: [], counter: [] };
  persona.selfReports.attachment = { text: "焦虑型", evidence: [], counter: [] };
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.persona") return persona;
    if (method === "self.periods") return periodsValue();
    return status(scope.account);
  } });
  await settle(); await env.click("tab-persona");
  const chart = () => findAll(env.get("personaCharts"), "kpc-card").find(card => findAll(card, "kpc-title").some(title => title.textContent === "依恋类型"));
  assert.equal(findAll(chart(), "kpc-dot").length, 1);
  assert.match(env.get("personaChartContext").textContent, /依恋倾向使用全历史资料/);
  await openChart(env, "personaCharts", "依恋类型");
  assert.match(text(env.get("personaList")), /反复寻求确认.*愿意表达亲近需要.*焦虑型/s);
  findAll(env.get("personaPeriods"), "kpc-p-col")[0].listeners.click[0]({}); await settle();
  assert.equal(findAll(chart(), "kpc-dot").length, 1);
  assert.match(text(env.get("personaList")), /反复寻求确认.*愿意表达亲近需要.*焦虑型/s);
  assert.equal(env.calls.some(call => call.method.startsWith("self.attachment")), false);
});

const WORLDVIEW_FACTORS = value => Object.fromEntries(["pol_econ", "pol_lib", "val_change", "val_self", "moral_bind"].map(key => [key, { score: value, low: value - .1, high: value + .1, items: 6, total: 6, chats: 4 }]));
const worldviewValue = () => ({ answers: 90, changes: [], others: false, periods: [
  { id: "2026H1", label: "2026 上半年", passes: 3, items: 30, factors: WORLDVIEW_FACTORS(2.4), letters: ["社会民主主义"], style: "进取派", edited: [],
    ai: { type: "社会民主主义", style: "进取派", "view:money": "想先攒钱" }, views: { money: { text: "想先攒钱", evidence: [{ s: 1, k: "自述", name: "合成朋友", time: 1, text: "先把钱存下来" }] } } },
  { id: "2026H2", label: "2026 下半年", passes: 3, items: 30, factors: WORLDVIEW_FACTORS(3.6), letters: ["保守主义"], style: "守护派", edited: [],
    ai: { type: "保守主义", style: "守护派", "view:money": "愿意花在体验上" }, views: { money: { text: "愿意花在体验上", evidence: [] } } }] });

test("三观时间线 draws the four axes, follows the chosen period and keeps the model's sentence after an edit", async () => {
  const value = worldviewValue();
  const env = environment({ rpc: (method, params) => {
    if (method === "self.persona") return personaStatus();
    if (method === "self.worldview") return structuredClone(value);
    if (method === "self.worldviewEdit") {
      const period = value.periods.find(item => item.id === params.period);
      period.edited = params.value === null ? [] : [params.field]; period.views.money.text = params.value ?? period.ai["view:money"];
      return structuredClone(value);
    }
    return undefined;
  } });
  await settle();
  env.elements.get("tab-values").listeners.click[0]({});
  await settle(); await settle();
  const host = env.elements.get("valuesPeriods"), views = () => env.elements.get("valuesPeriodViews");
  assert.equal(findAll(host, "kpc-p-col").length, 2);
  assert.equal(findAll(host, "kpc-p-name").map(item => item.textContent).join("|"), "经济立场|自由—威权|求变—守成|利己—利他|政治|价值观");
  assert.ok(text(views()).includes("愿意花在体验上"), "the newest period shows first");
  assert.ok(text(env.elements.get("valuesCharts")).includes("保守主义"), "politics follows the newest period");
  assert.deepEqual(chartTitles(env, "valuesCharts"), ["政治光谱", "价值观"], "legacy moral scores never recreate the removed chart");
  findAll(host, "kpc-p-col")[0].listeners.click[0]({});
  await settle();
  assert.ok(text(views()).includes("想先攒钱"));
  assert.ok(text(env.elements.get("valuesCharts")).includes("社会民主主义"), "politics follows the selected earlier period");
  assert.equal(findAll(views(), "persona-evidence").length, 1, "the sentence opens its lines");
  const card = views().children.find(item => text(item).includes("金钱观"));
  findAll(card, "persona-edit")[0].listeners.click[0]({});
  await settle();
  const box = views().children.find(item => text(item).includes("金钱观"));
  const area = findAll(box, "period-edit-control")[0];
  assert.equal(area.tagName?.toLowerCase?.() ?? "textarea", "textarea");
  area.value = "我其实没存下来";
  findAll(box, "primary")[0].listeners.click[0]({});
  await settle(); await settle();
  assert.deepEqual(plain(env.calls.find(call => call.method === "self.worldviewEdit").params), { period: "2026H1", field: "view:money", value: "我其实没存下来" });
  const after = views().children.find(item => text(item).includes("金钱观"));
  assert.ok(text(after).includes("我其实没存下来"));
  assert.ok(text(after).includes("AI 判断：想先攒钱"));
});

test("8values shows the original question and edits answers on its agreement scale", async () => {
  const env = environment({ rpc: (method, params) => {
    if (method === "self.persona") return personaStatus();
    if (method === "self.worldview") return worldviewValue();
    if (method === "self.worldviewDetail") return { period: params.period, label: "2026 下半年", key: params.key, items: [
      { item: 1001, text: "合成政治题", source: "8values 第 1 题", english: "Synthetic original statement.", answers: [4], why: [], evidence: [] }] };
    if (method === "self.worldviewEdit") return worldviewValue();
    return undefined;
  } });
  await settle(); await env.click("tab-values"); await settle();
  findAll(env.elements.get("valuesPeriods"), "kpc-p-name")[0].listeners.click[0]({});
  await settle(); await settle();
  const detail = env.elements.get("valuesPeriodDetail");
  assert.match(text(detail), /8values 第 1 题/);
  assert.match(text(detail), /Synthetic original statement/);
  findAll(detail, "persona-edit")[0].listeners.click[0]({}); await settle();
  const control = findAll(detail, "period-edit-control")[0];
  assert.deepEqual(control.children.map(item => item.textContent), ["强烈反对", "反对", "中立", "同意", "强烈同意"]);
  control.value = "5";
  findAll(detail, "primary")[0].listeners.click[0]({}); await settle(); await settle();
  assert.deepEqual(plain(env.calls.find(call => call.method === "self.worldviewEdit").params), { period: "2026H2", field: "item:1001", value: 5 });
});

test("the new political instrument asks to rerun instead of displaying legacy scores", async () => {
  const env = environment({ rpc: method => method === "self.worldview" ? { periods: [], legacy: true } : undefined });
  await settle(); await env.click("tab-values"); await settle();
  assert.match(env.elements.get("valuesPeriodLine").textContent, /8values.*重新|8values.*开始分析/);
  assert.equal(env.elements.get("valuesPeriods").children.length, 0);
});

test("换了时间颗粒度还没分析时提示去勾选，不留空白", async () => {
  const env = environment({ rpc: method => {
    if (method === "self.worldview") return { answers: 90, changes: [], periods: [], others: true };
    return undefined;
  } });
  await settle();
  env.elements.get("tab-values").listeners.click[0]({});
  await settle(); await settle();
  assert.match(env.elements.get("valuesPeriodLine").textContent, /勾选“三观时间线”/);
  assert.equal(env.elements.get("valuesPeriodLine").hidden, false);
});

for (const [months, heads, labels] of [
  [6, ["2024 下半年", "2024 上半年"], ["24上", "24下"]],
  [3, ["2024 第三季度", "2024 第一季度"], ["24Q1", "24Q2", "24Q3"]],
  [24, ["2024–2025"], ["2024–25"]],
  [60, ["2020–2024"], ["2020–24"]],
]) test(`时间线按 ${months} 个月分段`, async () => {
  const events = [timelineEvent("e1", "入学", { importance: "important" }), timelineEvent("e2", "搬家", { importance: "important", startMs: new Date(2024, 7, 1).getTime(), endMs: new Date(2024, 8, 1).getTime() - 1 })];
  const env = environment({ rpc(method, params, scope) {
    if (method === "self.timeline") return { ...timelineView(events), months };
    return status(scope.account);
  } });
  await settle(); await env.click("tab-timeline"); await settle();
  assert.deepEqual(timelineByClass(env, "timelineList", "tl-year-head").map(head => head.children[0].textContent), heads);
  const shown = timelineByClass(env, "timelineOverview", "tl-year-label").map(label => label.textContent);
  for (const label of labels) assert.ok(shown.includes(label), `${label} in ${shown}`);
  assert.ok(text(env.get("timelineList")).includes("入学"));
});

test("时间线的字母、自述、分数和题目都能改，改过后显示用户的值并留着 AI 的判断", async () => {
  const value = periodsValue();
  const live = () => structuredClone(value);
  value.periods[1].ai = { type: "INFJ", report: "INFJ", "score:big5_e": 3.2 };
  value.periods[1].edited = [];
  const env = environment({ rpc: (method, params) => {
    if (method === "self.periods") return live();
    if (method === "self.periodDetail") return { period: params.period, label: "2026 下半年", key: params.key, items: [
      { item: 1, text: "我会主动和人搭话", reverse: false, inner: false, answers: [4, 4, 3], mine: value.periods[1].edited.includes("item:1") ? 2 : null, why: [], situational: 0, evidence: [] }] };
    if (method === "self.periodEdit") {
      const period = value.periods[1];
      period.edited = params.value === null ? [] : [params.field];
      if (params.field === "type") period.letters = params.value === null ? ["I", "N", "F", "J"] : [params.value];
      return live();
    }
    return undefined;
  } });
  await settle();
  env.elements.get("tab-persona").listeners.click[0]({});
  await settle(); await settle();
  const host = () => env.elements.get("personaPeriods"), editor = () => env.elements.get("personaPeriodEdit");
  const strip = text => findAll(host(), "kpc-p-seg").find(item => item.textContent === text);
  strip("INFJ").listeners.click[0]({ offsetX: 1 });
  await settle();
  assert.match(editor().children[0].children[0].textContent, /2026 下半年 · 读聊天。AI 判断：INFJ/);
  assert.equal(findAll(editor(), "primary").length, 1);
  assert.equal(editor().children[0].children[2].children.length, 2, "nothing to give back before the user changed it");
  editor().children[0].children[1].value = "ENTJ";
  findAll(editor(), "primary")[0].listeners.click[0]({});
  await settle(); await settle();
  assert.deepEqual(plain(env.calls.find(call => call.method === "self.periodEdit").params), { period: "2026H2", field: "type", value: "ENTJ" });
  assert.equal(editor().children.length, 0, "saving closes the editor");
  const changed = strip("ENTJ");
  assert.ok(String(changed.className).includes("edited"));
  assert.match(changed.attributes.title, /你改过，AI 判断：INFJ/);
  changed.listeners.click[0]({});
  await settle();
  assert.match(editor().children[0].children[0].textContent, /AI 判断：INFJ/, "the model's value stays on record");
  assert.equal(editor().children[0].children[2].children[1].textContent, "恢复 AI 判断");
  editor().children[0].children[2].children[1].listeners.click[0]({});
  await settle(); await settle();
  assert.equal(env.calls.filter(call => call.method === "self.periodEdit").at(-1).params.value, null);
  findAll(host(), "kpc-p-value")[0].listeners.click[0]({});
  await settle();
  assert.match(editor().children[0].children[0].textContent, /AI 判断：3\.2/);
  editor().children[0].children[1].value = "9";
  findAll(editor(), "primary")[0].listeners.click[0]({});
  await settle();
  assert.equal(env.calls.filter(call => call.method === "self.periodEdit").length, 2, "a score outside 1 to 5 is not sent");
  assert.equal(env.elements.get("personaPeriodLine").textContent, "分数要在 1 到 5 之间");
  findAll(host(), "kpc-p-name")[0].listeners.click[0]({});
  await settle(); await settle();
  const detail = env.elements.get("personaPeriodDetail");
  findAll(detail, "persona-edit")[0].listeners.click[0]({});
  await settle();
  const card = env.elements.get("personaPeriodDetail").children[0];
  const box = findAll(card, "period-picker")[0];
  box.children[1].value = "2";
  findAll(box, "primary")[0].listeners.click[0]({});
  await settle(); await settle();
  assert.deepEqual(plain(env.calls.filter(call => call.method === "self.periodEdit").at(-1).params), { period: "2026H2", field: "item:1", value: 2 });
  assert.ok(JSON.stringify(env.elements.get("personaPeriodDetail").children[0], (key, value) => typeof value === "function" ? undefined : value).includes("你选 2（AI：4 · 4 · 3）"));
});

test("时间线上的一段连续色条按点击位置打开对应的半年", async () => {
  const value = periodsValue();
  value.periods[1].letters = ["E", "N", "F", "J"];
  const env = environment({ rpc: method => method === "self.periods" ? value : undefined });
  await settle();
  env.elements.get("tab-persona").listeners.click[0]({});
  await settle(); await settle();
  const run = findAll(env.elements.get("personaPeriods"), "kpc-p-seg").find(item => item.textContent === "ENFJ");
  run.clientWidth = 200;
  run.listeners.click[0]({ offsetX: 20 });
  await settle();
  assert.match(env.elements.get("personaPeriodEdit").children[0].children[0].textContent, /2026 上半年/);
});

test("性格时间线 names every year under its own half-years", async () => {
  const value = periodsValue();
  value.periods.unshift({ ...value.periods[0], id: "2025H2", label: "2025 下半年" });
  const env = environment({ rpc: method => method === "self.periods" ? value : undefined });
  await settle();
  env.elements.get("tab-persona").listeners.click[0]({});
  await settle(); await settle();
  const years = findAll(findAll(env.elements.get("personaPeriods"), "kpc-p-years")[0], "kpc-p-seg");
  assert.deepEqual(years.map(item => [item.textContent, item.style.gridColumn]), [["2025", "1 / 2"], ["2026", "2 / 4"]]);
});

test("judge evaluation is read on request and keeps the diagnostic denominator visible", async () => {
  const env = environment({rpc: (method, params, scope) => method === "self.judgeShadow" ? {
    mode: "shadow", diagnosticThreshold: .5, threshold: null, counts: {total: 8, done: 7, error: 1},
    engines: [{kind: "timeline", engine: "systemone", positiveWindows: 4, falseNegativeWindows: 1}],
  } : status(scope.account)});
  await settle();
  assert.ok(!env.calls.some(call => call.method === "self.judgeShadow"));
  await env.click("judgeShadowRefresh");
  assert.match(env.get("judgeShadowSummary").textContent, /7 \/ 8/);
  assert.match(env.get("judgeShadowSummary").textContent, /漏判 1 \/ 4/);
  assert.match(env.get("judgeShadowSummary").textContent, /诊断分界 0.5/);
  assert.match(env.get("judgeShadowSummary").textContent, /尚未启用跳过/);
});

test("changing account discards a pending judge evaluation and permits a fresh read", async () => {
  const reply = deferred();
  const env = environment({rpc: (method, params, scope) => method === "self.judgeShadow" ? reply.promise : status(scope.account)});
  await settle();
  await env.click("judgeShadowRefresh");
  assert.equal(env.get("judgeShadowRefresh").disabled, true);
  await env.context({account: "account-b"});
  assert.equal(env.get("judgeShadowRefresh").disabled, false);
  reply.resolve({mode: "shadow", counts: {total: 999, done: 999}, engines: []});
  await settle();
  assert.equal(env.get("judgeShadowSummary").textContent, "");
});

test("a pending latest period stays selected and never borrows a historical score or self-report", async () => {
  const periods = periodsValue(), persona = personaStatus();
  periods.months = 6; periods.coverage = { total: 2, completed: 1, pending: 1 };
  Object.assign(periods.periods[1], { factors: Object.fromEntries(Object.keys(PERIOD_FACTORS(3, 0)).map(key => [key, { score: null }])), letters: [], report: null, status: "pending", passes: 0, items: 0 });
  dimension(persona, "big5_e").inference = { text: "历史上较外向", position: .8 };
  persona.selfReports.mbti = { text: "ENFJ", evidence: [] };
  const env = environment({ rpc: method => method === "self.periods" ? periods : method === "self.persona" ? persona : undefined });
  await settle(); await env.click("tab-persona");
  assert.ok(findAll(env.get("personaPeriods"), "kpc-p-col")[1].className.includes("on"));
  assert.equal(findAll(env.get("personaPeriods"), "kpc-p-value")[0].textContent, "—");
  assert.match(text(env.get("personaPeriodContext")), /每 6 个月.*2026 下半年.*尚未分析.*1 \/ 2/);
  assert.match(text(env.get("personaCharts")), /尚无类型/);
  assert.doesNotMatch(text(env.get("personaCharts")), /ENFJ/);
  await openChart(env, "personaCharts", "MBTI");
  assert.match(text(env.get("personaList").children[0]), /2026 下半年.*本期尚未分析/);
  assert.doesNotMatch(text(env.get("personaList").children[0]), /历史上较外向/);
  assert.equal(env.get("personaList").children.at(-1).tag, "details");
  await env.click("personaPeriodSettings");
  assert.deepEqual(env.hostCalls.at(-1).params, { section: "plugin", account: "account-a" });
});

test("worldview keeps its own period and renders pending views without falling back to all-history text", async () => {
  const worldview = worldviewValue(), values = valuesStatus();
  Object.assign(worldview.periods[1], { status: "partial", factors: {}, letters: [], style: "", views: { money: { text: "", status: "pending", evidence: [] } } });
  worldview.months = 6; worldview.coverage = { total: 2, completed: 1, pending: 1 };
  dimension(values, "money").inference = { text: "全历史金钱观", evidence: [] };
  const env = environment({ rpc: method => method === "self.worldview" ? worldview : method === "self.periods" ? periodsValue() : method === "self.values" ? values : method === "self.persona" ? personaStatus() : undefined });
  await settle(); await env.click("tab-persona");
  findAll(env.get("personaPeriods"), "kpc-p-col")[0].listeners.click[0]({}); await settle();
  await env.click("tab-values");
  assert.ok(findAll(env.get("valuesPeriods"), "kpc-p-col")[1].className.includes("on"));
  assert.match(text(env.get("valuesPeriodContext")), /2026 下半年.*调研未完成/);
  assert.match(text(env.get("valuesPeriodViews")), /本期尚未分析/);
  assert.doesNotMatch(text(env.get("valuesPeriodViews")), /全历史金钱观/);
  findAll(env.get("valuesPeriods"), "kpc-p-col")[0].listeners.click[0]({}); await settle();
  await env.click("tab-persona");
  findAll(env.get("personaPeriods"), "kpc-p-col")[1].listeners.click[0]({}); await settle();
  await env.click("tab-values");
  assert.ok(findAll(env.get("valuesPeriods"), "kpc-p-col")[0].className.includes("on"));
  await openChart(env, "valuesCharts", "政治光谱");
  assert.match(text(env.get("valuesChartList").children[0]), /2026 上半年/);
  assert.match(html, /<details id="valuesHistory"[^>]*><summary>全历史三观分析与依据/);
});

test("changing periods invalidates an in-flight question response", async () => {
  const pending = deferred();
  const env = environment({ rpc: method => method === "self.periods" ? periodsValue() : method === "self.periodDetail" ? pending.promise : undefined });
  await settle(); await env.click("tab-persona");
  findAll(env.get("personaPeriods"), "kpc-p-name")[0].listeners.click[0]({}); await settle();
  findAll(env.get("personaPeriods"), "kpc-p-col")[0].listeners.click[0]({}); await settle();
  pending.resolve({ period: "2026H2", key: "big5_e", items: [{ item: 1, text: "不应串到上半年", answers: [4], evidence: [] }] });
  await settle();
  assert.equal(env.get("personaPeriodDetail").children.length, 0);
});

test("period evidence loads beyond the preview, preserves every reason and marks uncounted context", async () => {
  const env = environment({ rpc: (method, params) => {
    if (method === "self.periods") return periodsValue();
    if (method === "self.periodDetail") return { period: params.period, key: params.key, items: [{ item: 1, text: "合成题", answers: [4, 3, 4], why: ["第一轮理由", "第二轮理由", "第一轮理由"],
      evidence: [{ id: "one", s: 1, k: "自述", text: "甲" }], evidenceTotal: 3, evidenceNextOffset: 1 }] };
    if (method === "self.periodEvidence") return { items: [{ id: "two", s: -1, k: "行为", text: "乙" }, { id: "three", s: 1, k: "处境", counted: false, text: "长".repeat(500) }], total: 3, nextOffset: null };
    return undefined;
  } });
  await settle(); await env.click("tab-persona");
  findAll(env.get("personaPeriods"), "kpc-p-name")[0].listeners.click[0]({}); await settle();
  assert.match(text(env.get("personaPeriodDetail")), /第一轮理由.*第二轮理由/s);
  findAll(env.get("personaPeriodDetail"), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.deepEqual(env.calls.find(call => call.method === "self.periodEvidence").params, { period: "2026H2", item: 1, offset: 1, limit: 50 });
  const detail = env.get("personaPeriodDetail");
  assert.equal(findAll(detail, "sample").length, 3);
  assert.equal(findAll(detail, "evidence-more").length, 0);
  assert.match(text(detail), /未计入/);
  assert.ok(text(detail).includes("长".repeat(500)), "the UI does not shorten a returned quote to 400 characters");
});

for (const kind of ["persona", "values"]) test(`${kind} conclusion evidence paginates its own source and ignores a late page after account switch`, async () => {
  const pending = deferred(), value = kind === "persona" ? personaStatus() : valuesStatus(), key = kind === "persona" ? "big5_e" : "money";
  dimension(value, key).inference = { text: "合成结论", evidence: [{ id: "one", quote: "首条依据" }], evidenceTotal: 2 };
  const env = environment({ rpc: method => method === `self.${kind}` ? value : method === `self.${kind}Evidence` ? pending.promise : undefined });
  await settle(); await env.click(`tab-${kind}`);
  if (kind === "persona") await openChart(env, "personaCharts", "MBTI");
  const id = `${kind}-evidence-${key}-inference-support`;
  findAll(env.get(id), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.deepEqual(env.calls.find(call => call.method === `self.${kind}Evidence`).params, { key, kind: "inference", side: "evidence", offset: 1, limit: 50, expectedVersion: 1 });
  await env.context({ account: "account-b" });
  pending.resolve({ items: [{ id: "two", quote: "旧账号的后续依据" }], total: 2, nextOffset: null }); await settle();
  assert.doesNotMatch(text(env.get(kind === "persona" ? "personaList" : "valuesList")), /旧账号的后续依据/);
});

test("showing the worldview page again refreshes its timeline and coverage", async () => {
  let calls = 0;
  const env = environment({ rpc: method => {
    if (method !== "self.worldview") return undefined;
    calls++;
    return { ...worldviewValue(), coverage: { total: 2, completed: calls, pending: 2 - calls }, months: 6 };
  } });
  await settle(); await env.click("tab-values");
  assert.match(text(env.get("valuesPeriodContext")), /1 \/ 2/);
  await env.shown();
  assert.match(text(env.get("valuesPeriodContext")), /2 \/ 2/);
  assert.equal(calls, 2);
});

for (const [side, suffix, totalKey] of [["evidence", "support", "evidenceTotal"], ["counter", "counter", "counterTotal"]])
test(`additional ${side} records are distinct from direct citations in both the preview and later pages`, async () => {
  const value = personaStatus();
  dimension(value, "big5_e").inference = { text: "合成结论", [side]: [
    { id: "one", quote: "本段引用", cited: true }, { id: "two", quote: "首屏的同维度记录", cited: false }, { id: "legacy", quote: "旧记录没有引用标记" }], [totalKey]: 4 };
  const env = environment({ rpc: method => method === "self.persona" ? value : method === "self.personaEvidence" ? {
    items: [{ id: "later", quote: "后页的同维度观察", cited: false }], total: 4, nextOffset: null, version: 1,
  } : undefined });
  await settle(); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  const id = `persona-evidence-big5_e-inference-${suffix}`;
  const preview = findAll(env.get(id), "sample");
  assert.doesNotMatch(text(preview[0]), /未被本段结论直接引用/);
  assert.match(text(preview[1]), /同维度相关记录，未被本段结论直接引用/);
  assert.doesNotMatch(text(preview[2]), /未被本段结论直接引用/, "legacy records retain their earlier display");
  findAll(env.get(id), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.deepEqual(env.calls.find(call => call.method === "self.personaEvidence").params,
    { key: "big5_e", kind: "inference", side, offset: 3, limit: 50, expectedVersion: 1 });
  const cards = findAll(env.get(id), "sample");
  assert.equal(cards.length, 4);
  assert.doesNotMatch(text(cards[0]), /未被本段结论直接引用/);
  assert.match(text(cards[1]), /同维度相关记录，未被本段结论直接引用/);
  assert.doesNotMatch(text(cards[2]), /未被本段结论直接引用/);
  assert.match(text(cards[3]), /同维度相关记录，未被本段结论直接引用/);
});

test("a changed period evidence snapshot is rejected and can reload the current question", async () => {
  let version = "first";
  const env = environment({ rpc: (method, params) => {
    if (method === "self.periods") return periodsValue();
    if (method === "self.periodDetail") return { period: params.period, key: params.key, items: [{ item: 1, text: "合成题", answers: [4],
      evidence: [{ id: version, text: version === "first" ? "旧快照首条" : "新快照首条" }], evidenceTotal: 2, evidenceNextOffset: 1, evidenceVersion: version }] };
    if (method === "self.periodEvidence") { version = "second"; return { items: [{ id: "newer", text: "不可混入的后页" }], total: 2, nextOffset: null, version }; }
    return undefined;
  } });
  await settle(); await env.click("tab-persona");
  findAll(env.get("personaPeriods"), "kpc-p-name")[0].listeners.click[0]({}); await settle();
  findAll(env.get("personaPeriodDetail"), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.equal(env.calls.find(call => call.method === "self.periodEvidence").params.expectedVersion, "first");
  assert.doesNotMatch(text(env.get("personaPeriodDetail")), /不可混入的后页/);
  assert.equal(findAll(env.get("personaPeriodDetail"), "evidence-more")[0].textContent, "读取最新依据");
  findAll(env.get("personaPeriodDetail"), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.equal(env.calls.filter(call => call.method === "self.periodDetail").length, 2);
  assert.match(text(env.get("personaPeriodDetail")), /新快照首条/);
  assert.doesNotMatch(text(env.get("personaPeriodDetail")), /旧快照首条/);
});

test("a stale conclusion page reloads the versioned analysis instead of appending records", async () => {
  let version = 1;
  const env = environment({ rpc: method => {
    if (method === "self.persona") {
      const value = personaStatus({ version });
      dimension(value, "big5_e").inference = { text: "合成结论", evidence: [{ id: String(version), quote: `第${version}版依据` }], evidenceTotal: 2 };
      return value;
    }
    if (method === "self.personaEvidence") { version++; throw Object.assign(new Error("依据已更新，请读取最新依据后继续。"), { code: "stale-evidence" }); }
    return undefined;
  } });
  await settle(); await env.click("tab-persona"); await openChart(env, "personaCharts", "MBTI");
  const id = "persona-evidence-big5_e-inference-support";
  findAll(env.get(id), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.equal(findAll(env.get(id), "evidence-more")[0].textContent, "读取最新依据");
  findAll(env.get(id), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.match(text(env.get(id)), /第2版依据/);
  assert.doesNotMatch(text(env.get(id)), /第1版依据/);
});

test("period politics opens only supported questionnaire axes, with no legacy empty cards", async () => {
  const env = environment({ rpc: method => method === "self.worldview" ? worldviewValue() : method === "self.persona" ? personaStatus() : undefined });
  await settle(); await env.click("tab-values"); await openChart(env, "valuesCharts", "政治光谱");
  const cards = env.get("valuesChartList").children.filter(item => item.className === "persona-card");
  assert.deepEqual(cards.map(card => card.children[0].textContent), ["经济立场", "自由—威权"]);
  assert.equal(cards.length, 2);
  assert.equal(cards.reduce((count, card) => count + all(card, item => item.tag === "button").length, 0), 2);
});

test("loading question evidence preserves a concurrent worldview text correction until cancel", async () => {
  const env = environment({ rpc: (method, params) => {
    if (method === "self.worldview") return worldviewValue();
    if (method === "self.worldviewDetail") return { period: params.period, key: params.key, items: [{ item: 1001, text: "合成题", answers: [4],
      evidence: [{ id: "one", text: "首条" }], evidenceTotal: 2, evidenceNextOffset: 1, evidenceVersion: "v1" }] };
    if (method === "self.worldviewEvidence") return { items: [{ id: "two", text: "后页" }], total: 2, nextOffset: null, version: "v1" };
    return undefined;
  } });
  await settle(); await env.click("tab-values");
  findAll(env.get("valuesPeriods"), "kpc-p-name")[0].listeners.click[0]({}); await settle();
  const moneyCard = () => env.get("valuesPeriodViews").children.find(item => text(item).includes("金钱观"));
  findAll(moneyCard(), "persona-edit")[0].listeners.click[0]({}); await settle();
  const id = "period-draft-worldview-2026H2-view:money";
  env.input(id, "尚未保存的本期修正");
  findAll(env.get("valuesPeriodDetail"), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.equal(env.get(id).value, "尚未保存的本期修正");
  assert.equal(findAll(env.get("valuesPeriodDetail"), "sample").length, 2);
  const cancel = all(moneyCard(), item => item.tag === "button" && item.textContent === "取消")[0];
  cancel.listeners.click[0]({}); await settle();
  findAll(moneyCard(), "persona-edit")[0].listeners.click[0]({}); await settle();
  assert.equal(env.get(id).value, "愿意花在体验上", "cancel explicitly discards that field's draft");
});

test("worldview summary evidence uses its own period, item and snapshot while retaining an edit draft", async () => {
  const value = worldviewValue(), pending = deferred();
  value.periods[1].views.money = { item: 103, text: "合成观点", evidence: [{ id: "one", text: "摘要首条" }], evidenceTotal: 3, evidenceNextOffset: 1, evidenceVersion: "view-v1" };
  const env = environment({ rpc: (method, params) => {
    if (method === "self.worldview") return value;
    if (method === "self.worldviewDetail") return { period: params.period, key: params.key, items: [{ item: 1001, text: "另一道问卷题", answers: [4], evidence: [] }] };
    if (method === "self.worldviewEvidence") return pending.promise;
    return undefined;
  } });
  await settle(); await env.click("tab-values");
  findAll(env.get("valuesPeriods"), "kpc-p-name")[0].listeners.click[0]({}); await settle();
  const card = () => env.get("valuesPeriodViews").children.find(item => text(item).includes("金钱观"));
  findAll(card(), "persona-edit")[0].listeners.click[0]({}); await settle();
  const draftId = "period-draft-worldview-2026H2-view:money";
  env.input(draftId, "摘要加载期间的未保存修正");
  findAll(card(), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.deepEqual(env.calls.find(call => call.method === "self.worldviewEvidence").params,
    { period: "2026H2", item: 103, offset: 1, limit: 50, expectedVersion: "view-v1" });
  pending.resolve({ items: [{ id: "two", text: "摘要后页一" }, { id: "three", text: "摘要后页二" }], total: 3, nextOffset: null, version: "view-v1" }); await settle();
  assert.equal(findAll(card(), "sample").length, 3);
  assert.equal(findAll(card(), "evidence-more").length, 0);
  assert.equal(env.get(draftId).value, "摘要加载期间的未保存修正");
  assert.match(text(env.get("valuesPeriodDetail")), /另一道问卷题/);
});

test("a worldview summary that already returns all four citations does not invent another page", async () => {
  const value = worldviewValue();
  value.periods[1].views.money = { item: 103, text: "合成观点", evidence: Array.from({ length: 4 }, (_, index) => ({ text: `合成引用 ${index}` })),
    evidenceTotal: 4, evidenceNextOffset: null, evidenceVersion: "view-v1" };
  const env = environment({ rpc: method => method === "self.worldview" ? value : undefined });
  await settle(); await env.click("tab-values");
  assert.equal(findAll(env.get("valuesPeriodViews"), "sample").length, 4);
  assert.equal(findAll(env.get("valuesPeriodViews"), "evidence-more").length, 0);
  assert.equal(env.calls.some(call => call.method === "self.worldviewEvidence"), false);
});

test("a late worldview summary page cannot enter another selected period", async () => {
  const value = worldviewValue(), pending = deferred();
  value.periods[1].views.money = { item: 103, text: "下半年观点", evidence: [{ text: "下半年首条" }], evidenceTotal: 2, evidenceVersion: "view-v1" };
  const env = environment({ rpc: method => method === "self.worldview" ? value : method === "self.worldviewEvidence" ? pending.promise : undefined });
  await settle(); await env.click("tab-values");
  findAll(env.get("valuesPeriodViews"), "evidence-more")[0].listeners.click[0]({}); await settle();
  findAll(env.get("valuesPeriods"), "kpc-p-col")[0].listeners.click[0]({}); await settle();
  pending.resolve({ items: [{ text: "不应进入上半年的后页" }], total: 2, nextOffset: null, version: "view-v1" }); await settle();
  assert.match(text(env.get("valuesPeriodViews")), /想先攒钱/);
  assert.doesNotMatch(text(env.get("valuesPeriodViews")), /不应进入上半年|下半年首条/);
});

test("a stale worldview summary page reloads the views and preserves the field draft", async () => {
  let reads = 0;
  const env = environment({ rpc: method => {
    if (method === "self.worldview") {
      const value = worldviewValue(), version = ++reads;
      value.periods[1].views.money = { item: 103, text: "合成观点", evidence: [{ text: version > 1 ? "新版本首条" : "旧版本首条" }], evidenceTotal: 2, evidenceVersion: `view-v${version}` };
      return value;
    }
    if (method === "self.worldviewEvidence") return { items: [{ text: "不应混入的新版本后页" }], total: 2, nextOffset: null, version: "view-v2" };
    return undefined;
  } });
  await settle(); await env.click("tab-values");
  const card = () => env.get("valuesPeriodViews").children.find(item => text(item).includes("金钱观"));
  findAll(card(), "persona-edit")[0].listeners.click[0]({}); await settle();
  const draftId = "period-draft-worldview-2026H2-view:money";
  env.input(draftId, "版本冲突也保留此稿");
  findAll(card(), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.doesNotMatch(text(card()), /不应混入/);
  assert.equal(findAll(card(), "evidence-more")[0].textContent, "读取最新依据");
  findAll(card(), "evidence-more")[0].listeners.click[0]({}); await settle();
  assert.match(text(card()), /新版本首条/);
  assert.doesNotMatch(text(card()), /旧版本首条/);
  assert.equal(env.get(draftId).value, "版本冲突也保留此稿");
  assert.equal(reads, 2);
});

test("an empty worldview timeline reports the selected run's queue and progress instead of a blank page", async () => {
  let stepState = "pending", runState = "running";
  const env = environment({ rpc: method => {
    if (method === "self.analysisStatus") return analysisStatus(runState, [{ key: "worldview", title: "三观时间线", state: stepState }], { outputs: ["worldview"] });
    if (method === "self.worldview") return { months: 6, periods: [] };
    return undefined;
  } });
  await settle(); await env.click("tab-values");
  assert.match(text(env.get("valuesPeriodLine")), /三观时间线排队中.*自动开始/);
  assert.equal(env.get("valuesPeriodLine").hidden, false);
  stepState = "running"; await env.shown();
  assert.match(text(env.get("valuesPeriodLine")), /正在分析三观时间线/);
  stepState = "paused"; await env.shown();
  assert.match(text(env.get("valuesPeriodLine")), /已暂停/);
  stepState = "error"; runState = "partial"; await env.shown();
  assert.match(text(env.get("valuesPeriodLine")), /分析失败.*重试/);
  stepState = "done"; runState = "done"; await env.shown();
  assert.equal(env.get("valuesPeriodLine").hidden, true);
});

test("timeline errors and legacy view evidence without a snapshot remain explicit", async () => {
  const value = worldviewValue();
  value.periods[1].status = "error";
  value.coverage = { total: 2, completed: 1, pending: 1, failed: 1 };
  value.periods[1].views.money = { text: "合成观点", evidence: [{ text: "首条" }], evidenceTotal: 3 };
  const env = environment({ rpc: method => method === "self.worldview" ? value : undefined });
  await settle(); await env.click("tab-values");
  assert.match(text(env.get("valuesPeriodContext")), /分析失败，待重试.*1 期需重试/);
  assert.match(text(env.get("valuesPeriodViews")), /原话 3（已显示 1 条）/);
  assert.equal(findAll(env.get("valuesPeriodViews"), "evidence-more").length, 0, "legacy payloads cannot invent an item or evidence snapshot");
});

test("judge diagnostics use the returned decision boundary", async () => {
  const env = environment({ rpc: method => method === "self.judgeShadow" ? {
    mode: "shadow", diagnosticThreshold: .7, counts: { total: 1, done: 1 }, engines: [],
  } : undefined });
  await settle(); await env.click("judgeShadowRefresh");
  assert.match(text(env.get("judgeShadowSummary")), /诊断分界 0\.7/);
  assert.doesNotMatch(text(env.get("judgeShadowSummary")), /0\.5/);
});
