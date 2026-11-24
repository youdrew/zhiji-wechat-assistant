// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic tests of the 模拟对话 tab of the 知己分析 scheme page; RPC is mocked, no chat data is read.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const pluginRoot = path.join(__dirname, "../plugins/kyf.friend-analysis");

class Element {
  constructor(tagName = "") {
    Object.assign(this, { tagName, className: "", textContent: "", children: [], style: {}, listeners: {}, attributes: {},
      hidden: false, disabled: false, value: "", open: false, scrollTop: 0, scrollHeight: 100, parent: null });
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  append(...items) {
    for (const item of items) {
      const child = typeof item === "string" ? Object.assign(new Element("#text"), { textContent: item }) : item;
      child.parent = this;
      this.children.push(child);
    }
  }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  focus() {}
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const classes = node => node.className.split(" ");
const all = (root, name) => descendants(root).filter(node => node.className === name || classes(node).includes(name));
const findButton = (root, label) => descendants(root).find(node => node.tagName === "button" && node.textContent === label);
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let index = 0; index < 8; index++) await new Promise(resolve => setImmediate(resolve)); };

function syntheticSession(extra = {}) {
  return { account: "synthetic-account", user: "wxid_synthetic", analysed: true, warning: null,
    profile: { layers: [{ key: "rules", title: "硬规则", lines: ["不用句号"] }, { key: "style", title: "表达", lines: ["常用“哈哈”"] }] },
    context: [{ side: "me", text: "周末有空吗", timeMs: 1 }, { side: "me", text: "去爬山", timeMs: 2 },
      { side: "friend", text: "可以呀", timeMs: 3 }],
    turns: [], pending: null, ...extra };
}

function harness(handler) {
  const nodes = new Map(), calls = [], timers = [];
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element("section")); return nodes.get(id); };
  const context = { console, Date, JSON, Promise,
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {},
    addEventListener() {},
    document: { getElementById: byId, createElement: tag => new Element(tag), body: new Element("body") },
    kyfHost: {
      rpc: async (method, params) => { calls.push({ method, params: params === undefined ? undefined : plain(params) }); return handler(method, params); },
      host: async () => ({}), on() {}, context: () => ({ account: "synthetic-account", name: "小林" }) } };
  context.window = context;
  vm.createContext(context);
  for (const file of ["page-core.js", "friend-simulation.js"])
    vm.runInContext(readFileSync(path.join(pluginRoot, "ui", file), "utf8"), context, { filename: file });
  const root = byId("pageSimulation");
  return { root, calls, sim: context.FriendSimulation,
    // Fire the pending one-second poll timers.
    async tick() { const due = timers.splice(0); for (const callback of due) callback(); await settle(); },
    input: () => descendants(root).find(node => node.tagName === "textarea"),
    async type(text) {
      const input = descendants(root).find(node => node.tagName === "textarea");
      input.value = text;
      let prevented = false;
      for (const listener of input.listeners.keydown) listener({ key: "Enter", shiftKey: false, isComposing: false, preventDefault() { prevented = true; } });
      await settle();
      return prevented;
    },
    async click(label) {
      const node = findButton(root, label);
      assert.ok(node, `missing ${label}`);
      for (const listener of node.listeners.click) listener({ target: node });
      await settle();
    },
    rows: side => all(root, "fa-sim-row").filter(node => classes(node).includes(side)),
    texts: name => all(root, name).map(node => node.textContent) };
}

test("the 模拟对话 tab is declared and its module is loaded by the page", () => {
  const manifest = JSON.parse(readFileSync(path.join(pluginRoot, "plugin.json"), "utf8"));
  const tabs = manifest.contributes.schemes[0].tabs;
  assert.deepEqual(plain(tabs.at(-1)), { id: "simulation", title: "模拟对话" });
  const html = readFileSync(path.join(pluginRoot, "ui/index.html"), "utf8");
  assert.match(html, /<section id="pageSimulation" class="fa-page fa-sim" hidden><\/section>/);
  assert.match(readFileSync(path.join(pluginRoot, "ui/page.js"), "utf8"), /simulation: \(\) => window\.FriendSimulation/);
});

test("the restored session shows faded real messages, the divider, both sides and no reply", async () => {
  const session = syntheticSession({ warning: "还没有做过知己分析，模拟可能不准。", turns: [
    { id: "t1", side: "me", messages: ["你在干嘛"] },
    { id: "t2", side: "friend", messages: ["在家", "躺着呢"], noReply: false, stance: "secret",
      samples: [{ input: ["吃了吗"], reply: ["吃了"], timeMs: Date.UTC(2026, 0, 2) }] },
    { id: "t3", side: "me", messages: ["出来玩", "走"] },
    { id: "t4", side: "friend", messages: [], noReply: true, samples: [] }] });
  const h = harness(method => { assert.equal(method, "page.simulation"); return session; });
  await h.sim.load();
  await settle();
  const real = all(h.root, "fa-sim-real")[0];
  assert.ok(real, "real messages are shown");
  // Consecutive real messages of one side share a row; the friend's bubbles are on the left.
  assert.equal(all(real, "fa-sim-row").length, 2);
  assert.deepEqual(all(real, "fa-sim-bubble").map(node => node.textContent), ["周末有空吗", "去爬山", "可以呀"]);
  assert.deepEqual(h.texts("fa-sim-divider").length, 1);
  assert.equal(descendants(all(h.root, "fa-sim-divider")[0]).at(-1).textContent, "以下为模拟");
  const simulated = h.rows("friend").filter(row => !descendants(real).includes(row));
  assert.deepEqual(all(simulated[0], "fa-sim-bubble").map(node => node.textContent), ["在家", "躺着呢"]);
  assert.equal(h.rows("me").filter(row => !descendants(real).includes(row)).length, 2);
  assert.deepEqual(h.texts("fa-sim-system"), ["对方没有回复"]);
  assert.deepEqual(h.texts("fa-sim-notice"), ["还没有做过知己分析，模拟可能不准。"]);
  // One 换一个, below the last friend turn; no stance or other label anywhere.
  assert.equal(descendants(h.root).filter(node => node.textContent === "换一个").length, 1);
  assert.equal(all(h.root, "fa-sim-again")[0], all(h.root, "fa-sim-log")[0].children.at(-1));
  assert.ok(!descendants(h.root).some(node => node.textContent.includes("secret")));
  // Like a WeChat window: one notice line, the chat and the input; the profile and samples are not shown.
  assert.deepEqual(h.root.children.map(node => node.className), ["fa-sim-notice", "fa-sim-log", "fa-sim-composer"]);
  assert.equal(descendants(h.root).filter(node => node.tagName === "details").length, 0);
  for (const hidden of ["不用句号", "吃了", "看上下文", "ta 的档案", "参考的真实回复"])
    assert.ok(!descendants(h.root).some(node => node.textContent.includes(hidden)), hidden);
  // 重新开始 sits in the input bar beside 发送.
  const bar = all(h.root, "fa-sim-bar")[0];
  assert.deepEqual(bar.children.filter(node => node.tagName === "button").map(node => node.textContent), ["重新开始", "发送"]);
  assert.equal(findButton(h.root, "重新开始").hidden, false);
});

test("with nothing to say or reset, there is no notice row and no 重新开始", async () => {
  const h = harness(() => syntheticSession({ model: "ready", configured: true, historyReady: true }));
  await h.sim.load();
  assert.equal(all(h.root, "fa-sim-notice")[0].hidden, true);
  assert.equal(findButton(h.root, "重新开始").hidden, true);
});

test("a turn of several lines is sent once, polled each second, then read back from the session", async () => {
  let session = syntheticSession(), statuses = 0;
  const h = harness((method, params) => {
    if (method === "page.simulation") return session;
    if (method === "page.simulationSend") {
      assert.deepEqual(plain(params), { messages: ["哈喽", "在吗"] });
      session = { ...session, turns: [{ id: "t1", side: "me", messages: ["哈喽", "在吗"] }], pending: "r1" };
      return { requestId: "r1" };
    }
    if (method === "page.simulationStatus") {
      assert.deepEqual(plain(params), { requestId: "r1" });
      if (++statuses < 2) return { state: "running", turn: null, error: null };
      const turn = { id: "t2", side: "friend", messages: ["在", "咋了"], noReply: false, samples: [] };
      session = { ...session, turns: [...session.turns, turn], pending: null };
      return { state: "done", turn, error: null };
    }
    assert.fail(method);
  });
  await h.sim.load();
  assert.equal(await h.type("哈喽\n\n 在吗 "), true);
  assert.equal(h.input().value, "");
  // My turn shows at once, with the friend typing; no 换一个 while it runs, and the button stops it.
  assert.deepEqual(all(h.rows("me").at(-1), "fa-sim-bubble").map(node => node.textContent), ["哈喽", "在吗"]);
  assert.ok(all(h.root, "typing").length === 1);
  assert.ok(!findButton(h.root, "换一个"));
  assert.ok(findButton(h.root, "停止"));
  assert.equal(h.calls.filter(call => call.method === "page.simulationStatus").length, 0);
  await h.tick();
  assert.equal(h.calls.filter(call => call.method === "page.simulationStatus").length, 1);
  await h.tick();
  assert.equal(h.calls.filter(call => call.method === "page.simulationStatus").length, 2);
  assert.deepEqual(all(h.rows("friend").at(-1), "fa-sim-bubble").map(node => node.textContent), ["在", "咋了"]);
  assert.equal(all(h.root, "typing").length, 0);
  assert.ok(findButton(h.root, "换一个"));
  assert.ok(findButton(h.root, "发送"));
  assert.deepEqual(h.calls.map(call => call.method), ["page.simulation", "page.simulationSend", "page.simulationStatus",
    "page.simulationStatus", "page.simulation"]);
});

test("换一个 rerolls the last friend turn and replaces it", async () => {
  const me = { id: "t1", side: "me", messages: ["吃饭没"] };
  let session = syntheticSession({ turns: [me, { id: "t2", side: "friend", messages: ["吃了"], noReply: false, samples: [] }] });
  const h = harness(method => {
    if (method === "page.simulation") return session;
    if (method === "page.simulationReroll") return { requestId: "r2" };
    if (method === "page.simulationStatus") {
      session = { ...session, turns: [me, { id: "t3", side: "friend", messages: [], noReply: true, samples: [] }] };
      return { state: "done", turn: session.turns[1], error: null };
    }
    assert.fail(method);
  });
  await h.sim.load();
  await h.click("换一个");
  // The friend turn being replaced is hidden while the new one is generated.
  assert.ok(!descendants(h.root).some(node => node.textContent === "吃了"));
  assert.equal(all(h.root, "typing").length, 1);
  await h.tick();
  assert.deepEqual(h.texts("fa-sim-system"), ["对方没有回复"]);
  assert.ok(findButton(h.root, "换一个"));
});

test("opening the tab with a pending reply resumes polling it", async () => {
  let session = syntheticSession({ turns: [{ id: "t1", side: "me", messages: ["在吗"] }], pending: "r9" });
  const h = harness((method, params) => {
    if (method === "page.simulation") return session;
    if (method === "page.simulationStatus") {
      assert.equal(params.requestId, "r9");
      session = { ...session, turns: [...session.turns, { id: "t2", side: "friend", messages: ["在"], noReply: false, samples: [] }], pending: null };
      return { state: "done", turn: session.turns[1], error: null };
    }
    assert.fail(method);
  });
  await h.sim.load();
  await settle();
  assert.equal(all(h.root, "typing").length, 1);
  await h.tick();
  assert.equal(all(h.root, "typing").length, 0);
  assert.deepEqual(all(h.rows("friend").at(-1), "fa-sim-bubble").map(node => node.textContent), ["在"]);
});

test("a failed reply shows its error and gives back lines the session did not keep", async () => {
  const h = harness(method => {
    if (method === "page.simulation") return syntheticSession();
    if (method === "page.simulationSend") return { requestId: "r1" };
    if (method === "page.simulationStatus") return { state: "failed", turn: null, error: "模型请求失败" };
    assert.fail(method);
  });
  await h.sim.load();
  await h.type("一\n二");
  await h.tick();
  assert.deepEqual(h.texts("fa-sim-system error"), ["模型请求失败"]);
  // The session kept no turn for it, so the lines go back into the input.
  assert.equal(h.input().value, "一\n二");
});

test("a send refused at once restores the input; more than six lines is not sent", async () => {
  const h = harness(method => {
    if (method === "page.simulation") return syntheticSession();
    if (method === "page.simulationSend") throw Object.assign(new Error("x"), { code: "api-not-configured" });
    assert.fail(method);
  });
  await h.sim.load();
  await h.type("1\n2\n3\n4\n5\n6\n7");
  assert.equal(h.calls.filter(call => call.method === "page.simulationSend").length, 0);
  assert.match(h.texts("fa-sim-status")[0], /最多 6 条/);
  await h.type("你好");
  assert.equal(h.input().value, "你好");
  assert.deepEqual(h.texts("fa-sim-system error"), ["尚未配置生成模型，请在设置的模型设置中保存模型后重试。"]);
});

test("重新开始 clears the session and stops polling", async () => {
  let session = syntheticSession({ turns: [{ id: "t1", side: "me", messages: ["在吗"] }], pending: "r1" });
  const h = harness(method => {
    if (method === "page.simulation") return session;
    if (method === "page.simulationReset") { session = syntheticSession(); return { ok: true }; }
    if (method === "page.simulationStatus") return { state: "running", turn: null, error: null };
    assert.fail(method);
  });
  await h.sim.load();
  await settle();
  await h.click("重新开始");
  await h.tick();
  assert.equal(h.calls.filter(call => call.method === "page.simulationStatus").length, 0);
  assert.equal(h.rows("me").length, 1, "only the real chat remains");
  assert.equal(all(h.root, "typing").length, 0);
});

test("停止 cancels the running request", async () => {
  const h = harness((method, params) => {
    if (method === "page.simulation") return syntheticSession({ turns: [{ id: "t1", side: "me", messages: ["在吗"] }], pending: "r5" });
    if (method === "page.simulationCancel") { assert.deepEqual(plain(params), { requestId: "r5" }); return { ok: true }; }
    assert.fail(method);
  });
  await h.sim.load();
  await settle();
  await h.click("停止");
  assert.deepEqual(h.calls.map(call => call.method), ["page.simulation", "page.simulationCancel"]);
});

test("one notice at a time: model missing > model unavailable > history loading > analysis stale", async () => {
  let session = syntheticSession({ model: "missing", configured: false, historyReady: false, warning: "知己分析待更新" });
  const h = harness(method => { assert.equal(method, "page.simulation"); return session; });
  const shown = async () => { await h.sim.load(); const node = all(h.root, "fa-sim-notice")[0]; return node.hidden ? null : node.textContent; };
  assert.equal(await shown(), "尚未配置生成模型，请先在设置的模型设置中保存模型。");
  // A gateway that is down is not an unsaved model (the status bar already names the gateway error).
  session = { ...session, model: "unavailable", configured: true };
  assert.equal(await shown(), "生成模型暂时不可用，请稍后重试。");
  session = { ...session, model: "ready" };
  assert.equal(await shown(), "聊天历史仍在读取，模拟暂时只参考已读到的部分。");
  session = { ...session, historyReady: true };
  assert.equal(await shown(), "知己分析待更新");
  session = { ...session, warning: null };
  assert.equal(await shown(), null);
  // An older backend with only configured: false still says the model is not saved.
  session = { ...session, model: undefined, configured: false };
  assert.equal(await shown(), "尚未配置生成模型，请先在设置的模型设置中保存模型。");
  assert.equal(all(h.root, "fa-sim-notice").length, 1);
});

test("a stopped reply puts my lines back, as the backend drops the unanswered turn", async () => {
  let session = syntheticSession();
  const h = harness(method => {
    if (method === "page.simulation") return session;
    if (method === "page.simulationSend") {
      session = { ...session, turns: [{ id: "t1", side: "me", messages: ["一"] }], pending: "r1" };
      return { requestId: "r1" };
    }
    if (method === "page.simulationCancel") { session = { ...session, turns: [], pending: null }; return { ok: true }; }
    if (method === "page.simulationStatus") return { state: "cancelled", turn: null, error: null };
    assert.fail(method);
  });
  await h.sim.load();
  await h.type("一");
  await h.click("停止");
  await h.tick();
  assert.equal(h.input().value, "一");
  assert.deepEqual(h.texts("fa-sim-system error"), []);
  assert.equal(h.rows("me").length, 1, "only the real chat remains");
});
