// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic tests of the 标签 tab of the 知己分析 scheme page; page.tags* RPCs are mocked, no chat data is read.
// The mock shapes follow tests/fixtures/friend-tags-page.json, real backend output (friend_tags) on a synthetic chat,
// which the last test also renders as it is.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const pluginRoot = path.join(__dirname, "../plugins/kyf.friend-analysis");
const fixture = JSON.parse(readFileSync(path.join(__dirname, "fixtures/friend-tags-page.json"), "utf8"));

class Element {
  constructor(tagName = "") {
    Object.assign(this, { tagName, className: "", textContent: "", children: [], style: {}, listeners: {}, attributes: {},
      hidden: false, disabled: false, value: "", parent: null });
  }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() {}
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const has = (node, name) => node.className.split(" ").includes(name);
const all = (root, name) => descendants(root).filter(node => has(node, name));
const text = node => [node.textContent, ...node.children.map(text)].join("");
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let index = 0; index < 8; index++) await new Promise(resolve => setImmediate(resolve)); };

const CATEGORIES = ["运动健身", "游戏棋牌", "影音娱乐", "阅读学习", "美食饮食", "旅行出行", "工作技能", "身份生活",
  "性格习惯", "观点态度", "其他", "说话习惯"];
const idle = { state: "idle", runState: null, detail: "", done: null, total: null, error: null, analyzed: false };
const done = { ...idle, state: "done", runState: "done", detail: "标签分析没有新内容", analyzed: true };
const makeTag = (key, category, level, relation, mentions, extra = {}) => ({ key, name: key, category, level, relation,
  manual: mentions === 0, mentions, days: mentions ? 1 : 0, firstMs: null, lastMs: null, memories: [], ...extra });
const card = (msgId, timeMs, text, extra = {}) => ({ msgId, timeMs, context: "", text, followup: "", relation: null, quote: "",
  reason: "", truncated: false, status: "ok", ...extra });
function syntheticTags(extra = {}) {
  return { account: "synthetic-account", user: "wxid_synthetic", version: 1, categories: CATEGORIES,
    habits: [
      { key: "phrase:哈哈哈", label: "哈哈哈", detail: "23 次", manual: false, hidden: false },
      { key: "never:句号", label: "从不用句号", detail: "", manual: false, hidden: false },
      { key: "manual:从不发语音", label: "从不发语音", detail: "我的补充", manual: true, hidden: false },
      { key: "ending:啦", label: "句末用啦", detail: "12%", manual: false, hidden: true }],
    tags: [
      makeTag("不吃香菜", "美食饮食", "strong", "trait", 3, { days: 2, firstMs: Date.UTC(2024, 0, 2, 4), lastMs: Date.UTC(2024, 5, 3, 4),
        memories: [{ id: "m1", text: "对方不吃香菜，说是过敏" }] }),
      makeTag("篮球", "运动健身", "confirmed", "interest", 12, { days: 9, firstMs: Date.UTC(2023, 3, 1, 4), lastMs: Date.UTC(2026, 8, 2, 4) }),
      makeTag("羽毛球", "运动健身", "possible", "mention", 2),
      makeTag("养猫", "身份生活", "confirmed", "uncertain", 0)],
    undo: { user: { count: 0, label: null }, agent: { count: 0, label: null, items: [] } }, run: done, textMessages: 120, ...extra };
}

function harness(handler, { restricted = false } = {}) {
  const nodes = new Map(), calls = [], hosts = [], windowListeners = {};
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element("section")); return nodes.get(id); };
  const context = { console, Date, JSON, Promise, setTimeout: () => 0, clearTimeout() {},
    addEventListener: (name, listener) => { (windowListeners[name] ||= []).push(listener); },
    document: { getElementById: byId, createElement: tag => new Element(tag), body: new Element("body") },
    kyfHost: {
      rpc: async (method, params) => { calls.push({ method, params: params === undefined ? undefined : plain(params) }); return handler(method, params); },
      host: async (method, params) => { hosts.push({ method, params: plain(params) }); return {}; }, on() {},
      context: () => ({ account: "synthetic-account", name: "小林", restricted }) } };
  context.window = context;
  vm.createContext(context);
  for (const file of ["page-core.js", "friend-tags.js"])
    vm.runInContext(readFileSync(path.join(pluginRoot, "ui", file), "utf8"), context, { filename: file });
  const root = byId("pageTags");
  return { root, calls, hosts, tags: context.FriendTags, left: () => root.children[0], right: () => root.children[1],
    // Writes in order: page.tagEdit as its params (expectedVersion left out), the undo calls by name.
    writes: () => calls.filter(call => /^page\.tag(Edit|Undo)/.test(call.method)),
    edits() {
      return this.writes().map(call => call.method === "page.tagEdit" ?
        Object.fromEntries(Object.entries(call.params).filter(([key]) => key !== "expectedVersion")) : call.method.slice(5));
    },
    async key(init = {}) {
      const event = { key: "z", ctrlKey: true, target: { closest: () => null }, preventDefault() { event.prevented = true; }, ...init };
      for (const listener of windowListeners.keydown || []) listener(event);
      await settle();
      return event;
    },
    button(scope, label) {
      return descendants(scope).find(node => node.tagName === "button" && node.textContent === label && !node.hidden &&
        !hiddenAncestor(node));
    },
    async click(node) { assert.ok(node, "missing button"); for (const listener of node.listeners.click) await listener({ target: node }); await settle(); },
    chip(label) { return all(root, "fa-tg-chip").find(node => node.textContent === label); },
    groups: () => all(root.children[0], "fa-tg-group").map(node => node.children[0].textContent) };
}
function hiddenAncestor(node) { for (let current = node.parent; current; current = current.parent) if (current.hidden) return true; return false; }

// A small in-memory stand-in for page.tags / page.tagEdit / page.tagUndo* / page.tagEvidence: every write checks
// expectedVersion and answers with the whole page; the user's undo puts back the page before their last edit.
function backend(data = syntheticTags()) {
  const state = { data, history: [] };
  const answer = extra => ({ ...plain(state.data), ...extra });
  const handler = async (method, params) => {
    if (method === "page.tags") return plain(state.data);
    if (/^page\.tag(Edit|Undo)/.test(method)) {
      if (params.expectedVersion !== state.data.version) throw Object.assign(new Error("标签已在别处修改"), { code: "conflict" });
    }
    if (method === "page.tagUndo") {
      const before = state.history.pop();
      if (!before) return answer({ undone: null, skipped: null });
      state.data = { ...before.data, version: state.data.version + 1, undo: state.data.undo };
      return answer({ undone: before.label, skipped: null });
    }
    if (method === "page.tagUndoAgent") {
      state.data.version++;
      state.data.undo.agent = { count: 0, label: null, items: [] };
      return answer({ undone: "确认：篮球", skipped: null });
    }
    if (method === "page.tagEdit") {
      const label = `${params.action}：${params.name || params.key}`;
      state.history.push({ data: plain(state.data), label });
      const tags = state.data.tags, habits = state.data.habits;
      const tag = tags.find(item => item.name === params.name);
      if (params.action === "confirm") tag.level = "confirmed";
      else if (params.action === "reset" && tag) tag.level = "strong";
      else if (params.action === "reset") tags.push({ key: params.name, name: params.name, category: "美食饮食", level: "strong",
        relation: "trait", manual: false, mentions: 3, memories: [] });
      else if (params.action === "delete") state.data.tags = tags.filter(item => item !== tag);
      else if (params.action === "hide" || params.action === "unhide")
        habits.find(item => item.key === params.key).hidden = params.action === "hide";
      else if (params.action === "add" && params.category === "说话习惯")
        habits.push({ key: "manual:" + params.name, label: params.name, detail: "我的补充", manual: true, hidden: false });
      else if (params.action === "add") tags.push(makeTag(params.name, params.category, "confirmed", "uncertain", 0));
      else if (params.action === "unlink") tag.memories = tag.memories.filter(item => item.id !== params.memoryId);
      state.data.version++;
      return answer({ editedId: params.key || params.name, editLabel: label });
    }
    if (method === "page.tagEvidence") return params.key ? { key: params.key, total: 1, items: [card("h1", Date.UTC(2024, 1, 1, 4), "哈哈哈好")] } :
      { name: params.name, total: 3, items: [
        card("s1", Date.UTC(2024, 5, 3, 4), "可以，别放香菜", { relation: "trait", context: "我：晚上吃火锅？", followup: "我：好",
          quote: "别放香菜", reason: "明确说不吃" }),
        card("s2", Date.UTC(2024, 0, 2, 4), "香菜……", { relation: "mention", truncated: true })] };
    if (method === "page.message") return { status: "ok", messages: [{ id: "s1", target: true, speaker: "小林", timeMs: 1, text: "可以，别放香菜" }] };
    throw new Error("unexpected " + method);
  };
  return { state, handler };
}

test("two panes: 说话习惯 first, then content categories with chips styled by level", async () => {
  const h = harness(backend().handler);
  await h.tags.load();
  assert.equal(h.root.children.length, 2);
  assert.equal(h.left().children[0].children[0].textContent, "标签");
  assert.deepEqual(h.groups(), ["说话习惯", "运动健身", "美食饮食", "身份生活"]);
  const habit = h.chip("哈哈哈");
  assert.equal(text(habit), "哈哈哈23 次");
  assert.equal(habit.attributes.title, "说话习惯 · 统计");
  assert.equal(text(h.chip("从不发语音")), "从不发语音我的补充");
  assert.equal(h.chip("句末常带“啦”"), undefined, "a deleted habit is not shown");
  assert.ok(has(h.chip("篮球"), "confirmed") && has(h.chip("羽毛球"), "possible") && has(h.chip("不吃香菜"), "strong"));
  assert.equal(text(h.chip("篮球")), "篮球兴趣12 条");
  assert.equal(h.chip("篮球").attributes.title, "兴趣 · 已确认");
  assert.equal(text(h.chip("养猫")), "养猫手动添加");
  assert.ok(all(h.root, "fa-tg-guide")[0].textContent.includes("“仅提及”“偶尔参与”不代表兴趣"));
  const empty = all(h.right(), "fa-pane-empty")[0];
  assert.equal([empty.hidden, empty.textContent].join(), "false,选择一个标签，查看它来自哪些消息");
});

test("a tag's sources: stats line, linked 关系记忆, evidence cards with ta's name and context on request", async () => {
  const h = harness(backend().handler);
  await h.tags.load();
  await h.click(h.chip("不吃香菜"));
  assert.equal(h.chip("不吃香菜").attributes["aria-pressed"], "true");
  const right = h.right();
  assert.equal(right.children[0].children[0].textContent, "不吃香菜");
  assert.equal(all(right, "fa-pane-line")[0].textContent, "美食饮食 · 特征 · 较可靠 · 关联消息 3 条 · 2024/1/2–2024/6/3");
  const memory = all(right, "fa-tg-memory-text")[0];
  assert.equal(memory.textContent, "也记在关系记忆里：对方不吃香菜，说是过敏");
  await h.click(memory);
  assert.deepEqual(h.hosts.at(-1), { method: "tab.select", params: { tab: "memories" } });
  assert.deepEqual(h.calls.find(call => call.method === "page.tagEvidence").params, { name: "不吃香菜" });
  const cards = all(right, "fa-tg-sample");
  assert.equal(cards.length, 2);
  assert.equal(all(cards[0], "fa-tg-meta")[0].textContent, "2024/6/3 · 特征");
  assert.deepEqual(all(cards[0], "fa-tg-text").map(node => node.textContent), ["我：晚上吃火锅？", "后续：\n我：好"]);
  assert.equal(all(cards[0], "fa-tg-reply")[0].textContent, "小林：可以，别放香菜");
  assert.deepEqual(all(cards[0], "fa-tg-note").map(node => node.textContent), ["支持原话：别放香菜", "判断理由：明确说不吃"]);
  assert.deepEqual(all(cards[1], "fa-tg-note").map(node => node.textContent), ["原文过长，本次仅分析部分文字，语义待判断。"]);
  assert.ok(descendants(right).some(node => node.textContent === "共 3 条，显示最近 2 条"));
  await h.click(h.button(cards[0], "看上下文"));
  assert.deepEqual(h.calls.at(-1), { method: "page.message", params: { id: "s1" } });
  assert.equal(all(cards[0], "fa-context").length, 1);
});

test("confirm, unlink, delete and undo send version-checked writes and re-render from the returned page", async () => {
  const h = harness(backend().handler);
  await h.tags.load();
  await h.click(h.chip("不吃香菜"));
  await h.click(h.button(h.right(), "确认"));
  assert.ok(has(h.chip("不吃香菜"), "confirmed"));
  assert.ok(h.button(h.right(), "取消确认"));
  await h.click(h.button(h.right(), "取消关联"));
  assert.equal(all(h.right(), "fa-tg-memory").length, 0);
  await h.click(h.button(h.right(), "删除"));
  assert.equal(h.chip("不吃香菜"), undefined);
  assert.equal(all(h.right(), "fa-tg-undo")[0].children[0].textContent, "已删除“不吃香菜”，按 Ctrl+Z 撤销");
  await h.click(h.button(h.right(), "撤销"));
  assert.ok(h.chip("不吃香菜"));
  assert.equal(h.chip("不吃香菜").attributes["aria-pressed"], "true", "the restored tag is selected again");
  assert.equal(all(h.right(), "fa-tg-undo")[0].children[0].textContent, "已撤销：delete：不吃香菜");
  assert.deepEqual(h.edits(), [{ action: "confirm", name: "不吃香菜" }, { action: "unlink", name: "不吃香菜", memoryId: "m1" },
    { action: "delete", name: "不吃香菜" }, "tagUndo"]);
  assert.deepEqual(h.writes().map(call => call.params.expectedVersion), [1, 2, 3, 4], "every write names the version it was made on");
});

test("Ctrl+Z undoes the user's own last edit, persistently and not inside a text box", async () => {
  const h = harness(backend().handler);
  await h.tags.load();
  await h.click(h.chip("篮球"));
  await h.click(h.button(h.right(), "取消确认"));
  assert.ok(has(h.chip("篮球"), "strong"));
  await h.key({ target: { closest: () => ({}) } });
  assert.deepEqual(h.edits(), [{ action: "reset", name: "篮球" }], "Ctrl+Z in a text box stays with the text");
  const pressed = await h.key();
  assert.equal(pressed.prevented, true);
  assert.deepEqual(h.edits().at(-1), "tagUndo");
  assert.ok(has(h.chip("篮球"), "confirmed"));
  await h.key();
  assert.equal(all(h.right(), "fa-tg-undo")[0].children[0].textContent, "没有可撤销的修改");
});

test("a version conflict reads the page again and asks to retry", async () => {
  const { state, handler } = backend();
  const h = harness(handler);
  await h.tags.load();
  state.data.version = 7;
  await h.click(h.chip("羽毛球"));
  await h.click(h.button(h.right(), "确认"));
  await settle();
  assert.equal(all(h.left(), "fa-pane-status")[0].children[0].textContent, "标签刚被修改过，已刷新，请重试。");
  assert.equal(h.calls.filter(call => call.method === "page.tags").length, 2);
  await h.click(h.button(h.right(), "确认"));
  assert.equal(h.writes().at(-1).params.expectedVersion, 7, "the retry uses the version read again");
  assert.ok(has(h.chip("羽毛球"), "confirmed"));
});

test("the Agent's edits have their own undo line, shown only when there are some", async () => {
  const agent = { count: 2, label: "确认：篮球", items: [{ seq: 5, label: "确认：篮球", agent: "合成 Agent", updatedMs: 1 },
    { seq: 4, label: "添加：爬山", agent: "合成 Agent", updatedMs: 1 }] };
  const { state, handler } = backend(syntheticTags({ undo: { user: { count: 0, label: null }, agent } }));
  const h = harness(handler);
  await h.tags.load();
  const line = all(h.left(), "fa-tg-agent")[0];
  assert.equal(line.hidden, false);
  assert.equal(text(line), "Agent 改了 2 处 ·撤销×");
  assert.equal(line.title, "确认：篮球 · 合成 Agent\n添加：爬山 · 合成 Agent");
  await h.click(h.button(line, "撤销"));
  assert.deepEqual(h.edits(), ["tagUndoAgent"]);
  assert.equal(line.hidden, true, "nothing left to undo");
  state.data.undo.agent = { ...agent, count: 1, items: [{ ...agent.items[0], seq: 9 }] };
  await h.tags.load();
  assert.equal(line.hidden, false);
  await h.click(h.button(line, "×"));
  assert.equal(line.hidden, true);
  await h.tags.load();
  assert.equal(line.hidden, true, "a dismissed line stays closed until the Agent writes again");
  const plainPage = harness(backend().handler);
  await plainPage.tags.load();
  assert.equal(all(plainPage.left(), "fa-tg-agent")[0].hidden, true);
});

test("说话习惯: every item, the user's own too, hides and unhides by key; manual items show 我的补充 and have no 确认", async () => {
  const h = harness(backend().handler);
  await h.tags.load();
  await h.click(h.chip("哈哈哈"));
  assert.equal(all(h.right(), "fa-pane-line")[0].textContent, "说话习惯 · 统计");
  assert.equal(h.button(h.right(), "确认"), undefined);
  assert.deepEqual(h.calls.find(call => call.method === "page.tagEvidence").params, { key: "phrase:哈哈哈" });
  assert.equal(all(h.right(), "fa-tg-reply")[0].textContent, "小林：哈哈哈好");
  await h.click(h.button(h.right(), "删除"));
  assert.equal(h.chip("哈哈哈"), undefined);
  await h.click(h.button(h.right(), "撤销"));
  assert.ok(h.chip("哈哈哈"));
  const before = h.calls.filter(call => call.method === "page.tagEvidence").length;
  await h.click(h.chip("从不用句号"));
  await h.click(h.chip("从不发语音"));
  assert.equal(h.calls.filter(call => call.method === "page.tagEvidence").length, before, "从不 and manual habits have no sources");
  assert.equal(all(h.right(), "fa-pane-line")[0].textContent, "说话习惯 · 我的补充");
  await h.click(h.button(h.right(), "删除"));
  await h.click(h.button(h.right(), "撤销"));
  assert.ok(h.chip("从不发语音"));
  assert.deepEqual(h.edits(), [{ action: "hide", key: "phrase:哈哈哈" }, "tagUndo", { action: "hide", key: "manual:从不发语音" }, "tagUndo"]);
});

test("添加 opens a form with the categories, adds a tag or habit and selects it", async () => {
  const h = harness(backend().handler);
  await h.tags.load();
  const form = all(h.left(), "fa-tg-form")[0];
  assert.equal(form.hidden, true);
  await h.click(h.button(h.left().children[0], "添加"));
  assert.equal(form.hidden, false);
  const [input, select] = form.children;
  assert.equal(input.maxLength, 20);
  assert.deepEqual(select.children.map(node => node.value), CATEGORIES);
  assert.equal(select.value, "其他");
  input.value = "＃ 爱旅行";
  select.value = "旅行出行";
  await h.click(h.button(form, "添加"));
  assert.deepEqual(h.edits().at(-1), { action: "add", name: "爱旅行", category: "旅行出行" });
  assert.equal(form.hidden, true);
  assert.equal(h.chip("爱旅行").attributes["aria-pressed"], "true");
  assert.equal(all(h.right(), "fa-pane-line")[0].textContent, "旅行出行 · 已确认");
  await h.click(h.button(h.left().children[0], "添加"));
  input.value = "爱用倒装句";
  select.value = "说话习惯";
  for (const listener of input.listeners.keydown) listener({ key: "Enter", isComposing: false });
  await settle();
  assert.equal(h.chip("爱用倒装句").attributes["aria-pressed"], "true");
  assert.deepEqual(h.groups()[0], "说话习惯");
});

test("empty states: before analysis, after analysis, and too little chat", async () => {
  const empty = h => all(h.left(), "fa-pane-empty")[0];
  let h = harness(backend(syntheticTags({ tags: [], run: idle })).handler);
  await h.tags.load();
  assert.deepEqual(h.groups(), ["说话习惯"], "说话习惯 still shows before analysis");
  assert.equal(empty(h).textContent, "开始分析后，从 ta 的聊天里整理 ta 的兴趣、习惯和偏好");
  h = harness(backend(syntheticTags({ tags: [] })).handler);
  await h.tags.load();
  assert.equal(empty(h).textContent, "没有找到标签，可以手动添加");
  h = harness(backend(syntheticTags({ tags: [], habits: [], run: idle })).handler);
  await h.tags.load();
  assert.equal([empty(h).hidden, empty(h).textContent].join(), "false,聊天太少，暂时看不出");
  assert.equal(all(h.left(), "fa-tg-guide")[0].hidden, true);
});

test("the 标签分析 step's state shows above the list; a failure shows the reason", async () => {
  const message = h => all(h.left(), "fa-pane-status")[0].children[0].textContent;
  const states = [[{ ...idle, state: "running", runState: "running", detail: "标签分析 · 40 / 160 条", done: 40, total: 160 }, "正在分析标签，完成后自动显示"],
    [{ ...idle, state: "pending", runState: "running" }, "等待分析标签…"], [{ ...idle, state: "stopped", runState: "stopped" }, "标签分析已停止"],
    [{ ...idle, state: "failed", runState: "partial", error: "模型暂时不可用" }, "模型暂时不可用"], [{ ...idle, state: "pending", runState: "stopped" }, ""]];
  for (const [run, expected] of states) {
    const h = harness(backend(syntheticTags({ run })).handler);
    await h.tags.load();
    assert.equal(message(h), expected, run.state + "/" + run.runState);
    assert.equal(all(h.left(), "fa-pane-status")[0].hidden, !expected);
  }
  const broken = harness(async () => { throw new Error("标签读取失败了"); });
  await broken.tags.load();
  assert.equal(message(broken), "标签读取失败了");
});

test("a source whose message is gone or changed says so", async () => {
  const h = harness(async (method, params) => method === "page.tags" ? syntheticTags() : { name: params.name, total: 2, items: [
    card("s1", null, "", { status: "missing", relation: "trait", quote: "别放香菜" }), card("s2", Date.UTC(2024, 0, 2, 4), "香菜", { status: "changed" })] });
  await h.tags.load();
  await h.click(h.chip("不吃香菜"));
  const cards = all(h.right(), "fa-tg-sample");
  assert.equal(all(cards[0], "fa-tg-reply")[0].textContent, "小林：（原消息已不在本机）");
  assert.equal(h.button(cards[0], "看上下文"), undefined);
  assert.ok(all(cards[1], "fa-tg-note").some(node => node.textContent === "原消息已变化，这条依据可能过时。"));
});

test("a restricted window shows tags and sources without any edit control", async () => {
  const agent = { count: 1, label: "x", items: [{ seq: 1, label: "x", agent: "", updatedMs: 1 }] };
  const h = harness(backend(syntheticTags({ undo: { user: { count: 1, label: "x" }, agent } })).handler, { restricted: true });
  await h.tags.load();
  await h.click(h.chip("不吃香菜"));
  for (const label of ["添加", "确认", "删除", "撤销", "取消关联"]) assert.equal(h.button(h.root, label), undefined, label);
  assert.equal(all(h.right(), "fa-tg-memory-text").length, 1, "the linked memory is still shown");
  assert.equal(all(h.right(), "fa-tg-sample").length, 2);
  assert.equal(all(h.left(), "fa-tg-agent")[0].hidden, true);
  await h.key();
  assert.deepEqual(h.edits(), []);
});

test("the page router shows 标签 and 时间线 and reloads each after its step", async () => {
  const nodes = new Map(), loads = [];
  let listener = null;
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element("section")); return nodes.get(id); };
  const context = { console, Date, JSON, Promise, setTimeout: () => 0, clearTimeout() {}, addEventListener() {},
    document: { getElementById: byId, createElement: tag => new Element(tag), body: { classList: { toggle() {} } } },
    kyfHost: { rpc: async () => ({}), host: async () => ({ drafts: [] }), on: fn => { listener = fn; }, context: () => ({}) } };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(readFileSync(path.join(pluginRoot, "ui", "page-core.js"), "utf8"), context);
  const module = name => ({ load: async () => { loads.push(name); } });
  Object.assign(context, { FriendTags: module("tags"), FriendTimeline: module("timeline"), FriendOverview: module("overview"), FriendPersona: module("persona"),
    FriendValues: module("values"), FriendChat: module("chat"), FriendSimulation: module("simulation"),
    FriendTrend: { ...module("trend"), settingsChanged() {}, historyChanged() {} } });
  vm.runInContext(readFileSync(path.join(pluginRoot, "ui", "page.js"), "utf8"), context);
  listener({ type: "context", context: { tab: "tags" } });
  await settle();
  assert.equal(byId("pageTags").hidden, false);
  assert.equal(byId("pageOverview").hidden, true);
  listener({ type: "event", event: { type: "run-step", key: "tags" } });
  listener({ type: "event", event: { type: "run-step", key: "history" } });
  listener({ type: "event", event: { type: "run-step", key: "persona" } });
  assert.deepEqual(loads, ["tags", "tags", "tags"]);
  listener({ type: "context", context: { tab: "timeline" } });
  assert.equal(byId("pageTimeline").hidden, false);
  assert.equal(byId("pageTags").hidden, true);
  listener({ type: "event", event: { type: "run-step", key: "timeline" } });
  listener({ type: "event", event: { type: "run-step", key: "history" } });
  listener({ type: "event", event: { type: "run-step", key: "tags" } });
  assert.deepEqual(loads, ["tags", "tags", "tags", "timeline", "timeline", "timeline"]);
});

test("the mock shapes match the real backend output, which renders as it is", async () => {
  const keys = value => Object.keys(value).sort(), real = fixture.page, mock = syntheticTags();
  assert.deepEqual(keys(mock), keys(real));
  assert.deepEqual(keys(mock.tags[0]), keys(real.tags[0]));
  assert.deepEqual(keys(mock.habits[0]), keys(real.habits[0]));
  assert.deepEqual(keys(mock.run), keys(real.run));
  assert.deepEqual(mock.categories, real.categories);
  assert.deepEqual(keys(card("x", 0, "")), keys(fixture.tagEvidence.items[0]));
  assert.deepEqual(keys(card("x", 0, "")), keys(fixture.habitEvidence.items[0]));
  assert.deepEqual(keys(mock.undo.agent), keys(real.undo.agent));
  assert.deepEqual(keys(mock.undo.user), keys(real.undo.user));
  const { handler } = backend();
  assert.deepEqual(keys(await handler("page.tagEdit", { action: "hide", key: "phrase:哈哈哈", expectedVersion: 1 })), keys(fixture.editHide),
    "page.tagEdit answers with the whole page plus editedId / editLabel");
  const h = harness(async (method, params) => {
    if (method === "page.tags") return plain(real);
    if (method === "page.tagEvidence") return plain(params.key ? fixture.habitEvidence : fixture.tagEvidence);
    if (method === "page.tagEdit") return plain(fixture.editHide);
    throw new Error("unexpected " + method);
  });
  await h.tags.load();
  assert.equal(h.groups()[0], "说话习惯");
  assert.equal(all(h.left(), "fa-pane-status")[0].hidden, true, "a finished run shows no status");
  const linked = real.tags.find(item => item.memories.length);
  await h.click(h.chip(linked.name));
  assert.equal(all(h.right(), "fa-tg-memory-text")[0].textContent, "也记在关系记忆里：" + linked.memories[0].text);
  const sample = all(h.right(), "fa-tg-sample")[0];
  assert.equal(all(sample, "fa-tg-reply")[0].textContent, "小林：" + fixture.tagEvidence.items[0].text);
  assert.deepEqual(all(sample, "fa-tg-text").map(node => node.textContent),
    [fixture.tagEvidence.items[0].context, "后续：\n" + fixture.tagEvidence.items[0].followup]);
  await h.click(h.chip("从不发语音"));
  await h.click(h.button(h.right(), "删除"));
  assert.deepEqual(h.edits().at(-1), { action: "hide", key: "manual:从不发语音" });
  assert.equal(h.writes().at(-1).params.expectedVersion, real.version);
  assert.equal(h.chip("从不发语音"), undefined, "a hidden habit of the user's own is gone from the list");
});
