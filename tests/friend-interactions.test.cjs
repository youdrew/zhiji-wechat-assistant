// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic interactions use the real core/API/draft implementation and synthetic page data.
// Modified 2026-10-13: the window's run controls drive Core scheme tasks; the analysis pages are the
// kyf.friend-analysis scheme page and reach the host only through kyfHost calls.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

class Element {
  constructor(tag = "") {
    Object.assign(this, { tagName: tag, className: "", textContent: "", children: [], dataset: {},
      listeners: {}, style: {}, open: false, parentNode: null });
  }
  append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } }
  prepend(...items) { for (const item of items) item.parentNode = this; this.children.unshift(...items); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  setAttribute(name, value) { if (name === "disabled") this.disabled = true; else this[name] = value; }
  contains(node) { return descendants(this).includes(node); }
  focus() {}
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this); }
  get lastChild() { return this.children.at(-1); }
  get childNodes() { return this.children; }
  querySelector(selector) {
    if (selector === "details[data-key='legacy']")
      return descendants(this).find(node => node.tagName === "details" && node.dataset.key === "legacy") || null;
    return null;
  }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const findButton = (root, text) => descendants(root).find(node => node.tagName === "button" && node.textContent === text);
const dispatch = async (node, event) => Promise.all((node.listeners[event] || []).map(listener => listener({ target: node })));
const settle = async () => { for (let index = 0; index < 4; index++) await new Promise(resolve => setImmediate(resolve)); };

function context(nodes, extra) {
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const value = {
    location: { hash: "#account=synthetic-account&user=wxid_friend_a&name=Friend" },
    localStorage: { getItem: () => null }, crypto: webcrypto, TextEncoder, URLSearchParams,
    CSS: { escape: item => item }, addEventListener() {},
    setTimeout() { return 0; }, clearTimeout() {},
    document: { getElementById: byId, createElement: tag => new Element(tag), activeElement: null,
      body: Object.assign(new Element("body"), { classList: { toggle() {} } }), addEventListener() {}, removeEventListener() {} },
    ...extra,
  };
  value.window = value;
  vm.createContext(value);
  return { context: value, byId };
}
const plain = value => JSON.parse(JSON.stringify(value));

// -- the window's run controls (chatui/friend-run.js) over Core tasks and the scheme's RPC ------------------------
const SCHEME = { plugin: "kyf.friend-analysis", scheme: "default", name: "默认方案" };
function hostHarness(respond) {
  const nodes = new Map(), requests = [];
  const { context: ctx, byId } = context(nodes, {
    fetch: async (url, init) => {
      const parsed = new URL(url, "http://synthetic.local"), body = init.body ? JSON.parse(init.body) : null;
      requests.push({ path: parsed.pathname, query: parsed.searchParams, body });
      const result = await respond(parsed.pathname, body, parsed.searchParams);
      const status = result?.status || 200;
      return { ok: status < 400, status, json: async () => result?.status ? result.body : result };
    },
  });
  for (const file of ["friend-core.js", "friend-run.js"])
    vm.runInContext(readFileSync(path.join(__dirname, "../chatui", file), "utf8"), ctx);
  return { context: ctx, run: ctx.FriendRun, byId, requests };
}

test("the run line and button follow the scheme's status and drive Core scheme tasks", async () => {
  let status = { line: "需要配置 API 模型", disabled: true, action: { label: "开始分析", mode: "update" } }, tasks = [];
  const h = hostHarness(async (endpoint, body, query) => {
    if (endpoint === "/api/plugins/rpc") {
      assert.deepEqual([body.plugin, body.method], [SCHEME.plugin, "scheme.status"]);
      return { result: status };
    }
    if (endpoint === "/api/tasks" && !body) {
      assert.equal(query.get("plugin"), SCHEME.plugin);
      return { tasks };
    }
    return { task: { id: "task-1" } };
  });
  h.run.setScheme(SCHEME);
  await settle();
  const button = h.byId("faRunButton");
  assert.equal(h.byId("faRunInfo").textContent, "需要配置 API 模型");
  assert.equal(button.disabled, true, "an unconfigured model cannot start");
  status = { line: "完整分析约 1 分钟", action: { label: "开始分析", mode: "update" } };
  await h.run.refresh();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "开始分析");
  await button.onclick();
  const started = h.requests.find(request => request.path === "/api/tasks" && request.body);
  assert.deepEqual(started.body, { plugin: SCHEME.plugin, scheme: "default", title: "默认方案", params: { mode: "update" },
    account: "synthetic-account", user: "wxid_friend_a" });
  tasks = [{ id: "task-1", kind: "scheme", scheme: "default", state: "running", progress: { label: "记忆 · 3 / 19 条" } }];
  await h.run.refresh();
  assert.equal(button.textContent, "取消");
  assert.equal(h.byId("faRunInfo").textContent, "记忆 · 3 / 19 条");
  await button.onclick();
  assert.deepEqual(h.requests.find(request => request.path === "/api/tasks/stop").body,
    { id: "task-1", account: "synthetic-account", user: "wxid_friend_a" });
});

test("an unavailable model gateway keeps a 重试 button that recovers without reopening the window", async () => {
  let down = true;
  const h = hostHarness(async (endpoint, body) => {
    if (endpoint === "/api/plugins/rpc") return down ? { status: 503, body: { error: "upstream-unavailable",
      message: "统一模型网关暂时不可用或正在切换模型" } } : { result: { line: "已是最新", action: { label: "更新分析", mode: "update" } } };
    return { tasks: [] };
  });
  h.run.setScheme(SCHEME);
  await settle();
  const button = h.byId("faRunButton");
  assert.equal(h.byId("faRunInfo").textContent, "统一模型网关暂时不可用或正在切换模型");
  assert.deepEqual([button.hidden, button.disabled, button.textContent], [false, false, "重试"]);
  down = false;
  await button.onclick();
  await settle();
  assert.equal(h.byId("faRunInfo").textContent, "已是最新");
  assert.equal(button.textContent, "更新分析");
});

test("rebuilding asks the scheme for a full-history estimate before starting a rebuild task", async () => {
  const h = hostHarness(async (endpoint, body) => {
    if (endpoint === "/api/plugins/rpc" && body.method === "scheme.estimate") {
      assert.deepEqual(body.params, { mode: "rebuild" });
      return { result: { line: "完整分析约 3 分钟" } };
    }
    if (endpoint === "/api/plugins/rpc") return { result: {} };
    return endpoint === "/api/tasks" && !body ? { tasks: [] } : { task: { id: "task-2" } };
  });
  h.run.setScheme(SCHEME);
  await settle();
  await h.run.confirmRebuild();
  const sheet = h.byId("faSheetBody");
  assert.ok(descendants(sheet).some(node => node.textContent === "完整分析约 3 分钟"));
  await dispatch(findButton(sheet, "开始重新分析"), "click");
  await settle();
  const started = h.requests.find(request => request.path === "/api/tasks" && request.body);
  assert.deepEqual(plain(started.body.params), { mode: "rebuild" });
});

test("another running scheme of the friend offers stopping it or queueing behind it", async () => {
  let attempts = 0;
  const h = hostHarness(async (endpoint, body) => {
    if (endpoint === "/api/plugins/rpc") return { result: { action: { label: "开始分析", mode: "update" } } };
    if (endpoint === "/api/tasks" && !body) return { tasks: [] };
    if (attempts++ === 0) return { status: 409, body: { error: "scheme-busy", message: "busy",
      detail: { plugin: "dev.other", scheme: "deep", title: "深度方案" } } };
    return { task: { id: "task-3" } };
  });
  h.run.setScheme(SCHEME);
  await settle();
  await h.byId("faRunButton").onclick();
  const sheet = h.byId("faSheetBody");
  assert.ok(findButton(sheet, "停止「深度方案」并开始"));
  await dispatch(findButton(sheet, "排在它后面"), "click");
  await settle();
  const starts = h.requests.filter(request => request.path === "/api/tasks" && request.body);
  assert.equal(starts.length, 2);
  assert.equal(starts[1].body.conflict, "queue");
});

// -- the scheme page (plugins/kyf.friend-analysis/ui) over kyfHost ----------------------------------------------
function pageHarness(module, respond, extraContext = {}) {
  const nodes = new Map(), calls = [];
  const call = target => async (method, params = {}) => {
    calls.push({ target, method, params: plain(params) });
    const result = await respond(target, method, params);
    if (result?.error) throw Object.assign(new Error(result.error.message), { code: result.error.code });
    return result;
  };
  const host = { rpc: call("plugin"), host: call("host"),
    context: () => ({ account: "synthetic-account", user: "wxid_friend_a", plugin: "kyf.friend-analysis",
      ...extraContext }) };
  const { context: ctx, byId } = context(nodes, { kyfHost: host });
  vm.runInContext(readFileSync(path.join(__dirname, "../plugin-sdk/web/persona-charts.js"), "utf8"), ctx);
  for (const file of ["page-core.js", module])
    vm.runInContext(readFileSync(path.join(__dirname, "../plugins/kyf.friend-analysis/ui", file), "utf8"), ctx);
  return { context: ctx, app: ctx.FriendApp, byId, calls };
}
const hostCalls = (h, method) => h.calls.filter(item => item.target === "host" && item.method === method);

const DIMENSIONS = [["big5_o", "开放性"], ["big5_c", "尽责性"], ["big5_e", "外向性"], ["big5_a", "宜人性"], ["big5_n", "情绪性"],
  ["att_anx", "依恋焦虑"], ["att_avo", "依恋回避"], ["pol_axis", "政治倾向"], ["pol_ext", "立场强度"], ["pol_econ", "经济立场"],
  ["pol_lib", "自由—威权"], ["val_change", "求变—守成"], ["val_self", "利己—利他"], ["moral_care", "关爱"], ["moral_fair", "公平"],
  ["moral_loyal", "忠诚"], ["moral_auth", "权威"], ["moral_sanct", "圣洁"], ["moral_liberty", "自由"],
  ["cog_need", "认知需求"], ["cog_close", "认知闭合"], ["locus", "控制点"], ["optimism", "气质性乐观"],
  ["time_focus", "时间焦点"], ["time_past", "过去态度"], ["self_constr", "自我建构"], ["dialect", "辩证思维"],
  ["direct", "沟通直接性"], ["self_monitor", "自我监控"], ["reject_sens", "拒绝敏感性"], ["moral_scope", "道德关怀范围"],
  ["val_enh", "自我提升—自我超越"], ["humor", "幽默风格"], ["conflict", "冲突处理"]];
const FAMILIES = { personality: ["big5_o", "big5_c", "big5_e", "big5_a", "big5_n", "att_anx", "att_avo"],
  mind: ["cog_need", "cog_close", "locus", "optimism"], time: ["time_focus", "time_past"],
  expression: ["self_constr", "dialect", "direct", "self_monitor"], politics: ["pol_axis", "pol_ext", "pol_econ", "pol_lib"],
  values: ["val_change", "val_self", "val_enh"],
  moral: ["moral_care", "moral_fair", "moral_loyal", "moral_auth", "moral_sanct", "moral_liberty", "moral_scope"],
  humor: ["humor"], conflict: ["conflict"] };
const FAMILY_LABELS = { personality: "性格与依恋", mind: "认知与动机", time: "时间观", expression: "表达与思维",
  politics: "政治光谱", values: "价值观", moral: "道德基础", humor: "幽默风格", conflict: "冲突处理" };
const GROUP_OF = Object.fromEntries(Object.entries(FAMILIES).flatMap(([family, keys]) => keys.map(key => [key, family])));
const TYPE_KEYS = new Set(["humor", "conflict"]);
function personaData() {
  const dimensions = DIMENSIONS.map(([id]) => ({ id, label: null, detail: "", confidence: "无法判断", support: [], counter: [],
    periods: null, position: null, tier: "main", ...(TYPE_KEYS.has(id) ? { types: {}, dominant: null } : {}) }));
  Object.assign(dimensions[2], { label: "偏高", confidence: "有一定依据", position: 0.4, support: ["e1", "e2"] });
  Object.assign(dimensions[7], { label: "偏进步", confidence: "把握较低", position: -0.3, support: ["e3", "e4"] });
  Object.assign(dimensions[9], { label: "偏左", confidence: "把握较低", position: -0.3, support: ["e5", "e6"] });
  return {
    result: { coverage: {}, historyComplete: true, removed: 0, dimensions, drift: [],
      values: ["love", "family", "money", "plan", "core"].map(id => ({ id, selfReport: null,
        inference: { id, label: null, detail: "", confidence: "无法判断", support: [], counter: [], periods: null } })),
      selfReports: { mbti: null, attachment: { value: "焦虑型", quote: "我是焦虑型", timeMs: 1000 }, politics: null } },
    catalog: DIMENSIONS.map(([id, label]) => ({ id, label, group: GROUP_OF[id] })), observations: [],
    labels: { mbti: "MBTI", ...Object.fromEntries(DIMENSIONS) }, families: FAMILY_LABELS, sources: {},
  };
}
function personaHarness(data, drafts = [], extraContext = {}) {
  return pageHarness("friend-persona.js", async (target, method, params) => {
    if (target === "plugin") {
      if (method === "page.persona") return data;
      assert.equal(method, "page.personaEvidence");
      return { items: params.ids.map(id => ({ id, status: "ok", timeMs: 1000, excerpt: `原文 ${id}`,
        note: "synthetic evidence", messageId: id })) };
    }
    if (method === "drafts.list") return { drafts, closedGenerations: {} };
    if (method === "drafts.save" || method === "drafts.close") return { saved: true };
    assert.equal(method, "observations.save");
    data.observations.push({ id: "saved-observation", version: 1, judgement: params.judgement, note: params.note,
      dimension: params.dimension });
    return { items: data.observations };
  }, extraContext);
}
function observationForm(root) { return descendants(root).find(node => node.className === "fa-form"); }
function chart(root, title) {
  return descendants(root).find(node => node.className.startsWith("kpc-card") &&
    descendants(node).some(child => child.className === "kpc-title" && child.textContent === title));
}
function correctionButton(root, label) {
  const heading = descendants(root).find(node => node.className === "fa-row-label" && node.textContent === label);
  return findButton(heading.parentNode.parentNode.parentNode, "修正");
}

test("性格分析 draws the Big Five as MBTI scales and the attachment plane with the self-report apart", async () => {
  const h = personaHarness(personaData());
  await h.context.FriendPersona.load();
  const page = h.byId("pagePersona"), big5 = chart(page, "MBTI");
  assert.equal(descendants(big5).find(node => node.className === "kpc-aside"), undefined,
    "a partly known scale is not a type, so no derived aside without all four letters");
  assert.deepEqual(descendants(big5).filter(node => node.className === "kpc-label").map(node => node.textContent),
    ["能量", "心智", "天性", "应对方式", "身份特征"], "MBTI letter order");
  const dots = descendants(big5).filter(node => node.className.startsWith("kpc-dot"));
  assert.deepEqual(dots.map(node => [node.className, node.style.left]), [["kpc-dot some", "30%"]], "high 外向性 leans to E on the left");
  assert.deepEqual(descendants(big5).filter(node => node.className.startsWith("kpc-letter")).slice(0, 2)
    .map(node => [node.textContent, node.className]), [["E", "kpc-letter on"], ["I", "kpc-letter"]]);
  assert.deepEqual(descendants(big5).filter(node => node.className.startsWith("kpc-value")).map(node => node.textContent),
    ["70% 外向", "证据不足", "证据不足", "证据不足", "证据不足"]);
  assert.equal(descendants(big5).filter(node => node.className === "kpc-track unknown").length, 4, "no point without evidence");
  const attachment = chart(page, "依恋类型");
  assert.deepEqual(descendants(attachment).filter(node => node.className.startsWith("kpc-end")).map(node => node.textContent),
    ["低回避", "低焦虑", "高焦虑", "高回避"]);
  assert.deepEqual(descendants(attachment).filter(node => node.className.startsWith("kpc-quadrant")).map(node => node.textContent),
    ["安全型", "焦虑型", "回避型", "恐惧型"]);
  const self = descendants(attachment).find(node => node.className === "kpc-self");
  assert.deepEqual([self.style.left, self.style.top], ["75%", "25%"], "焦虑型: high anxiety, low avoidance on top");
  assert.ok(descendants(attachment).find(node => node.className === "kpc-plane tinted unknown"));
  assert.equal(chart(page, "政治光谱"), undefined, "三观 charts live on their own tab");
  assert.equal(observationForm(page), undefined);
  assert.equal(descendants(page).some(node => node.className.startsWith("fa-list fa-chart-detail")), false, "details open on demand");
});

test("the MBTI type follows the scales and the detail rows use the scales' names and wording", async () => {
  const data = personaData();
  data.result.selfReports.mbti = { value: "ISTP", quote: "我是 ISTP", timeMs: 1000 };
  data.result.dimensions[5].label = "偏高";
  Object.assign(data.result.dimensions[5], { confidence: "有一定依据", position: 0.6 });
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  const page = h.byId("pagePersona");
  assert.equal(descendants(chart(page, "MBTI")).find(node => node.className === "kpc-aside").textContent, "ISTP",
    "with no derivable type the self-report stands in as the aside");
  await dispatch(chart(page, "MBTI"), "click");
  const rows = descendants(page).filter(node => node.className === "fa-persona-head")
    .map(node => node.childNodes.map(child => child.textContent).join("|"));
  assert.deepEqual(rows, ["能量|70% 外向|有一定依据", "心智|证据不足", "天性|证据不足", "应对方式|证据不足", "身份特征|证据不足",
    "MBTI|ISTP|本人自述"]);
  assert.ok(descendants(page).some(node => node.className === "fa-note" &&
    node.textContent.startsWith("原话（") && node.textContent.includes("我是 ISTP")),
    "the friend's own words stay beside the type, with no derived prefix when the type is not derivable");
  await dispatch(chart(page, "依恋类型"), "click");
  assert.deepEqual(descendants(page).filter(node => node.className === "fa-row-label").map(node => node.textContent).slice(0, 2),
    ["焦虑", "回避"]);
});

test("a shared political plane names its one supported axis without inventing the other coordinate", async () => {
  const h = personaHarness(personaData());
  await h.context.FriendValues.load();
  const politics = chart(h.byId("pageValues"), "政治光谱");
  assert.equal(descendants(politics).some(node => node.className.startsWith("kpc-dot")), false);
  assert.equal(descendants(politics).find(node => node.className === "kpc-plane-status").textContent,
    "仅经济立场轴有依据；自由—威权轴证据不足");
});

test("三观分析 draws the political plane and a weak point stays hollow", async () => {
  const data = personaData();
  Object.assign(data.result.dimensions[9], { confidence: "较有把握" });
  Object.assign(data.result.dimensions[10], { label: "偏自由", confidence: "较有把握", position: -0.9 });
  const h = personaHarness(data);
  await h.context.FriendValues.load();
  const values = h.byId("pageValues");
  const politics = chart(values, "政治光谱");
  const dot = descendants(politics).find(node => node.className.startsWith("kpc-dot"));
  assert.deepEqual([dot.className, dot.style.left, dot.style.top], ["kpc-dot firm", "35%", "95%"],
    "经济立场 leans left, a 自由-leaning 自由—威权 lands at the 自由 end the bottom label names");
  assert.deepEqual(descendants(politics).filter(node => node.className.startsWith("kpc-end")).map(node => node.textContent),
    ["威权", "左派", "右派", "自由"], "the plane draws the second dimension's high end at the top");
  assert.deepEqual(descendants(politics).filter(node => node.className.startsWith("kpc-quadrant")).map(node => node.textContent),
    ["社会主义", "保守主义", "社会民主主义", "保守自由主义"]);
  assert.equal(descendants(politics).find(node => node.className === "kpc-plane-center").textContent, "中间派");
  const moral = chart(values, "道德基础");
  assert.deepEqual(descendants(moral).filter(node => node.className === "kpc-label").map(node => node.textContent),
    ["个人", "集体", "道德关怀范围"], "the pooled axis names its two ends, 道德关怀范围 joins as a second row");
  assert.deepEqual(descendants(moral).filter(node => node.className.startsWith("kpc-value")).map(node => node.textContent),
    ["证据不足", "证据不足"], "no foundation evidence, no point");
  assert.equal(descendants(moral).filter(node => node.className.split(" ").includes("kpc-slide-row")).length, 1,
    "the extra 道德关怀范围 row uses the slider grid");
  const valuesCard = chart(values, "价值观");
  const enhance = descendants(valuesCard).filter(node => node.className.split(" ").includes("kpc-slide-row"));
  assert.equal(enhance.length, 1, "自我提升—自我超越 joins as a slider row");
  assert.equal(descendants(enhance[0]).find(node => node.className === "kpc-label").textContent, "人生目标");
  assert.equal(chart(values, "MBTI"), undefined);
  assert.deepEqual(descendants(values).filter(node => node.className === "fa-value-card")
    .map(node => node.children[0].textContent), ["感情观", "家庭观", "金钱观", "人生规划", "价值底色"]);
  assert.equal(descendants(values).filter(node => node.className === "fa-value-row" &&
    descendants(node).some(child => child.textContent === "证据不足，暂不推测")).length, 5);
});

test("三观分析 before a run shows pending text instead of judging evidence", async () => {
  const data = personaData();
  data.result = null;
  const h = personaHarness(data);
  await h.context.FriendValues.load();
  const page = h.byId("pageValues");
  assert.deepEqual(descendants(page).filter(node => node.className === "fa-value-card")
    .map(node => node.children[0].textContent), ["感情观", "家庭观", "金钱观", "人生规划", "价值底色"]);
  assert.equal(descendants(page).filter(node => node.textContent === "分析后显示").length, 10);
  assert.equal(descendants(page).some(node => node.textContent === "证据不足，暂不推测"), false);
});

test("三观文字 separates direct statements, inference, sources and my observation", async () => {
  const data = personaData(), love = data.result.values[0];
  love.selfReport = { text: "我希望关系里能长期相互支持", evidence: ["self1"], timeMs: 1000 };
  love.inference = { id: "love", label: "重视长期支持", detail: "两次谈到长期关系。",
    confidence: "有一定依据", support: ["e1", "e2"], counter: ["e3"], periods: null };
  data.observations = [{ id: "own", version: 1, dimension: "love", judgement: "我的看法", note: "" }];
  const h = personaHarness(data);
  await h.context.FriendValues.load();
  const page = h.byId("pageValues"), card = descendants(page).find(node =>
    node.className === "fa-value-card" && node.children[0].textContent === "感情观");
  assert.ok(card);
  assert.ok(descendants(card).some(node => node.textContent === love.selfReport.text));
  assert.ok(descendants(card).some(node => node.textContent.includes("重视长期支持")));
  for (const title of ["原文 1 条", "支持的原文 2 条", "相反的原文 1 条", "编辑"])
    assert.ok(findButton(card, title), title);
  assert.ok(descendants(card).some(node => node.textContent === "我的看法"));
  await h.context.FriendPersona.load();
  assert.equal(descendants(h.byId("pagePersona")).some(node => node.textContent === "我的看法"), false,
    "a value observation belongs only on the values tab");
});

test("a life-view correction draft restores in 三观分析", async () => {
  const data = personaData();
  const h = personaHarness(data, [{ entry: "observation:new", generation: 3, seq: 4,
    payload: { dimension: "love", judgement: "我的补充", note: "" } }]);
  const drafts = await h.app.loadDrafts();
  assert.equal(await h.context.FriendPersona.restoreDrafts(drafts), false);
  assert.equal(await h.context.FriendValues.restoreDrafts(drafts), true);
  const page = h.byId("pageValues");
  assert.ok(observationForm(page));
  assert.ok(descendants(page).some(node => node.className === "fa-value-card"));
});

test("many life-view statements reveal the newest sources first and load the rest", async () => {
  const data = personaData(), ids = Array.from({ length: 45 }, (_, i) => `newest-${i}`);
  data.result.values[0].selfReport = { text: "我对关系有想法", statements: [{ text: "我对关系有想法",
    timeMs: 1000, evidence: [ids[0]] }], evidence: ids };
  const h = personaHarness(data);
  await h.context.FriendValues.load();
  const page = h.byId("pageValues");
  await dispatch(findButton(page, "原文 45 条"), "click");
  const sheet = h.byId("faSheetBody");
  assert.equal(descendants(sheet).filter(node => node.className === "fa-msg").length, 40);
  assert.equal(descendants(sheet).find(node => node.className === "fa-msg-text").textContent, "原文 newest-0");
  await dispatch(findButton(sheet, "加载更多原文"), "click");
  await settle();
  assert.equal(descendants(sheet).filter(node => node.className === "fa-msg").length, 45);
  assert.equal(findButton(sheet, "加载更多原文"), undefined);
  assert.deepEqual(h.calls.filter(call => call.method === "page.personaEvidence").map(call => call.params.ids.length), [40, 5]);
});

test("the extended cards draw as slider rows without letters and fold 把握较低 into 参考", async () => {
  const data = personaData();
  Object.assign(data.result.dimensions.find(item => item.id === "cog_need"),
    { label: "偏高", confidence: "有一定依据", position: 0.4 });
  Object.assign(data.result.dimensions.find(item => item.id === "optimism"),
    { label: "偏低", confidence: "把握较低", position: -0.3, tier: "reference" });
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  const page = h.byId("pagePersona");
  for (const title of ["认知与动机", "表达与思维", "时间观", "幽默风格", "冲突处理"])
    assert.ok(chart(page, title), `${title} renders on the personality tab`);
  assert.equal(chart(h.byId("pageValues"), "认知与动机"), undefined, "the new cards stay on the personality tab");
  const mind = chart(page, "认知与动机");
  assert.deepEqual(descendants(mind).filter(node => node.className === "kpc-label").map(node => node.textContent),
    ["认知需求", "认知闭合", "控制点", "气质性乐观"]);
  assert.equal(descendants(mind).some(node => node.className === "kpc-letter"), false, "slider rows keep no letter columns");
  assert.deepEqual(descendants(mind).filter(node => node.className.startsWith("kpc-value")).map(node => node.textContent),
    ["70% 高", "证据不足", "证据不足", "65% 悲观"]);
  assert.deepEqual(descendants(chart(page, "时间观")).filter(node => node.className === "kpc-label")
    .map(node => node.textContent), ["时间焦点", "过去态度"], "the time perspective card keeps its two axes");
  await dispatch(mind, "click");
  const folded = descendants(page).find(node => node.tagName === "details" &&
    node.children[0]?.textContent?.startsWith("参考 · 把握较低"));
  assert.ok(folded, "把握较低 rows fold into their own group");
  assert.deepEqual(descendants(folded).filter(node => node.className === "fa-row-label").map(node => node.textContent),
    ["气质性乐观"], "only the reference tier folds");
});

test("幽默风格 draws four diverging bars and the dominant aside follows the local rule", async () => {
  const data = personaData();
  Object.assign(data.result.dimensions.find(item => item.id === "humor"),
    { types: { "亲和型": 5, "自强型": 1, "攻击型": 2, "自嘲型": 2 }, dominant: { type: "亲和型", count: 5 },
      label: "亲和型为主", confidence: "有一定依据" });
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  const page = h.byId("pagePersona"), humor = chart(page, "幽默风格");
  assert.equal(descendants(humor).find(node => node.className === "kpc-aside").textContent, "亲和型为主");
  assert.deepEqual(descendants(humor).filter(node => node.className === "kpc-bar")
    .map(node => [node.style.left, node.style.right, node.style.width]),
    [["50%", undefined, "25%"], ["50%", undefined, "5%"], [undefined, "50%", "10%"], [undefined, "50%", "10%"]],
    "亲和/自强 grow right from the midpoint, 攻击/自嘲 left, width by count share");
  await dispatch(humor, "click");
  assert.deepEqual(descendants(page).filter(node => node.className === "fa-row-text").map(node => node.textContent),
    ["亲和型为主"]);
  const flat = personaData();
  Object.assign(flat.result.dimensions.find(item => item.id === "humor"), { types: { "自嘲型": 2 }, dominant: null });
  const other = personaHarness(flat);
  await other.context.FriendPersona.load();
  const card = chart(other.byId("pagePersona"), "幽默风格");
  assert.equal(descendants(card).find(node => node.className === "kpc-aside"), undefined,
    "no dominant badge without the three same-type cross-date items");
  assert.equal(descendants(card).filter(node => node.className === "kpc-bar").length, 1,
    "the distribution draws regardless of a dominant type");
  await dispatch(card, "click");
  assert.deepEqual(descendants(other.byId("pagePersona")).filter(node => node.className === "fa-row-text")
    .map(node => node.textContent), ["自嘲型 2"]);
});

test("冲突处理 draws a five-cell heat strip and stays dashed without a recorded conflict", async () => {
  const data = personaData();
  Object.assign(data.result.dimensions.find(item => item.id === "conflict"),
    { types: { "竞争": 2, "合作": 4 }, dominant: { type: "合作", count: 4 }, label: "合作为主", confidence: "有一定依据" });
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  const card = chart(h.byId("pagePersona"), "冲突处理");
  assert.equal(descendants(card).find(node => node.className === "kpc-aside").textContent, "合作为主");
  const cells = descendants(card).filter(node => node.className.startsWith("kpc-heat-cell"));
  assert.deepEqual(cells.map(node => node.textContent), ["竞争", "合作", "妥协", "回避", "迁就"], "all five cells stay visible");
  assert.deepEqual(cells.filter(node => node.className.split(" ").includes("on")).map(node => node.textContent), ["合作"]);
  assert.deepEqual(cells.filter(node => node.className.split(" ").includes("none")).map(node => node.textContent),
    ["妥协", "回避", "迁就"], "types without records stay as dashed slots");
  const empty = personaHarness(personaData());
  await empty.context.FriendPersona.load();
  const emptyCard = chart(empty.byId("pagePersona"), "冲突处理");
  const emptyBox = descendants(emptyCard).find(node => node.className === "kpc-heat empty");
  assert.ok(emptyBox && descendants(emptyBox).some(node => node.className === "kpc-heat-empty" &&
    node.textContent === "暂无冲突情境"), "no conflict records leave the whole card dashed");
});

test("drift names the moved dimensions and keeps the previous conclusion beside the new one", async () => {
  const data = personaData();
  Object.assign(data.result.dimensions.find(item => item.id === "cog_need"),
    { label: "偏高", confidence: "有一定依据", position: 0.1, previous: { label: "很高", confidence: "较有把握", position: 0.5 } });
  Object.assign(data.result.dimensions.find(item => item.id === "pol_axis"),
    { label: "偏进步", confidence: "有一定依据", position: -0.2 });
  data.result.drift = [{ id: "cog_need", label: "偏高", from: 0.5, to: 0.1 },
    { id: "pol_axis", label: "偏进步", from: -0.8, to: -0.2 }];
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  const page = h.byId("pagePersona");
  const moved = descendants(page).find(node => node.tagName === "details" &&
    node.children[0]?.textContent === "与上次相比 2 个维度位置移动 ≥0.3");
  assert.ok(moved, "the page names drift instead of silently overwriting");
  assert.deepEqual(descendants(moved).filter(node => node.className === "fa-note").map(node => node.textContent),
    ["认知需求：上次 75% 高 → 本次 55% 高", "政治倾向：上次 90% 进步 → 本次 60% 进步"],
    "the move reads in each dimension's own ends, also for non-slider dims");
  await dispatch(chart(page, "认知与动机"), "click");
  await dispatch(findButton(page, "依据"), "click");
  assert.ok(descendants(page).some(node => node.className === "fa-note" && node.textContent === "上次判断：很高（较有把握）"),
    "the previous conclusion stays beside the new one");
});

test("文献佐证: title hover, the 理论依据 line, the citation sheet and the grouped 参考量表来源 section", async () => {
  const data = personaData();
  data.sources = { cog_need: ["Cacioppo & Petty (1982) JPSP, 42, 116–131", "Cacioppo, Petty & Kao (1984) JPA, 48, 306–307"],
    optimism: ["Scheier & Carver (1985) Health Psychology, 4, 219–247"],
    big5_e: ["Goldberg (1990) JPSP, 59, 1216–1229", "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"],
    big5_o: ["Goldberg (1990) JPSP, 59, 1216–1229"] };
  Object.assign(data.result.dimensions.find(item => item.id === "cog_need"),
    { label: "偏高", confidence: "有一定依据", position: 0.4 });
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  const page = h.byId("pagePersona");
  assert.equal(descendants(chart(page, "认知与动机")).find(node => node.className === "kpc-title").title,
    "Cacioppo & Petty (1982) JPSP, 42, 116–131；Cacioppo, Petty & Kao (1984) JPA, 48, 306–307；" +
    "Scheier & Carver (1985) Health Psychology, 4, 219–247",
    "the card title names its sources on hover");
  await dispatch(chart(page, "认知与动机"), "click");
  await dispatch(findButton(page, "依据"), "click");
  assert.ok(descendants(page).some(node => node.className === "fa-note" &&
    node.textContent === "理论依据：Cacioppo & Petty (1982) JPSP, 42, 116–131 等 2 篇"));
  await dispatch(findButton(page, "查看文献"), "click");
  assert.equal(h.byId("faSheetTitle").textContent, "认知需求 · 文献");
  assert.deepEqual(descendants(h.byId("faSheetBody")).filter(node => node.className === "fa-msg-text")
    .map(node => node.textContent),
    ["Cacioppo & Petty (1982) JPSP, 42, 116–131", "Cacioppo, Petty & Kao (1984) JPA, 48, 306–307"]);
  const listed = descendants(page).find(node => node.tagName === "details" &&
    node.children[0]?.textContent === "参考量表来源");
  assert.ok(listed, "the method block is followed by the full grouped citations");
  assert.deepEqual(descendants(listed).filter(node => node.tagName === "h4").map(node => node.textContent),
    ["性格与依恋", "认知与动机"], "families follow the response order and skip empty ones");
  const method = descendants(page).find(node => node.tagName === "details" &&
    node.children[0]?.textContent === "分析依据与说明");
  const note = descendants(method).find(node => node.className === "fa-note" && node.textContent.includes("维度来自"));
  assert.ok(note.textContent.includes("认知与动机（认知需求、认知闭合、控制点、气质性乐观）"));
  assert.ok(note.textContent.includes("幽默风格（亲和型、自强型、攻击型、自嘲型）"), "type dims name their vocabulary");
  assert.ok(note.textContent.includes("全部是根据聊天表现的推测，不是心理测评或诊断"), "the disclaimer stays");
});

for (const [dimension, label, tab, title] of [["mbti", "MBTI", "FriendPersona", "MBTI"],
  ["big5_e", "能量", "FriendPersona", "MBTI"], ["pol_axis", "政治倾向", "FriendValues", "政治光谱"]]) {
  test(`${label} corrections open from its chart and save the observation separately`, async () => {
    const data = personaData(), h = personaHarness(data), module = h.context[tab];
    const page = h.byId(tab === "FriendPersona" ? "pagePersona" : "pageValues");
    await module.load();
    await dispatch(chart(page, title), "click");
    await dispatch(correctionButton(page, label), "click");
    let form = observationForm(page);
    assert.ok(form);
    const input = descendants(form).find(node => node.tagName === "input");
    input.value = "synthetic personal observation";
    await dispatch(input, "input");
    assert.equal(await h.app.flushDrafts(), true);
    const draft = hostCalls(h, "drafts.save")[0];
    assert.equal(draft.params.payload.dimension, dimension);
    assert.equal(draft.params.payload.judgement, input.value);
    await module.load(); // A normal tab reload preserves the unfinished editor.
    form = observationForm(page);
    assert.equal(descendants(form).find(node => node.tagName === "input").value, input.value);
    await dispatch(findButton(form, "保存"), "click");
    await settle();
    const saved = hostCalls(h, "observations.save")[0];
    assert.equal(saved.params.dimension, dimension);
    assert.equal(saved.params.judgement, input.value);
    assert.equal(saved.params.expectedVersion, null);
    assert.equal(observationForm(page), undefined);
    assert.equal(data.result.dimensions[0].label, null, "saving a correction leaves the AI result intact");
    assert.ok(hostCalls(h, "drafts.close").length);
  });
}

for (const [dimension, tab] of [["mbti", "FriendPersona"], ["big5_o", "FriendPersona"], ["val_self", "FriendValues"]]) {
  test(`restored ${dimension} correction drafts open their chart and can be discarded`, async () => {
    const entry = "observation:new", data = personaData();
    const h = personaHarness(data, [{ entry, generation: 3, seq: 4, baseVersion: null,
      payload: { dimension, judgement: "restored judgement", note: "restored note" } }]);
    const drafts = await h.app.loadDrafts();
    const other = tab === "FriendPersona" ? "FriendValues" : "FriendPersona";
    assert.equal(await h.context[other].restoreDrafts(drafts), false, "only the owning tab restores it");
    assert.equal(await h.context[tab].restoreDrafts(drafts), true);
    await h.context[tab].load();
    const page = h.byId(tab === "FriendPersona" ? "pagePersona" : "pageValues");
    const form = observationForm(page);
    assert.ok(form);
    assert.equal(descendants(form).find(node => node.tagName === "input").value, "restored judgement");
    assert.equal(descendants(form).find(node => node.tagName === "textarea").value, "restored note");
    await dispatch(findButton(form, "放弃"), "click");
    await settle();
    assert.equal(observationForm(page), undefined);
    assert.equal(hostCalls(h, "observations.save").length, 0);
    assert.deepEqual(hostCalls(h, "drafts.close")[0].params, { entry, generation: 3 });
  });
}

test("deleting an observation asks inside the page and deletes only after 删除", async () => {
  const data = personaData();
  // A dimension of the earlier version stays under 其他观察.
  data.observations = [{ id: "existing", version: 2, dimension: "openness", legacyLabel: "尝试偏好",
    judgement: "synthetic observation", note: "" }];
  const h = personaHarness(data);
  await h.context.FriendPersona.load();
  assert.ok(descendants(h.byId("pagePersona")).find(node => node.textContent === "原关联：尝试偏好"));
  const body = h.context.document.body;
  const remove = () => descendants(h.byId("pagePersona")).find(node => node.tagName === "button" && node.textContent === "删除");
  // The click handler waits for the answer, so the click is not awaited before answering.
  let pending = dispatch(remove(), "click");
  await settle();
  let dialog = body.children.find(node => node.className === "fa-confirm");
  assert.ok(dialog, "a page dialog replaces window.confirm, which sandboxed frames do not get");
  await dispatch(findButton(dialog, "取消"), "click");
  await pending;
  assert.equal(body.children.length, 0);
  assert.equal(hostCalls(h, "observations.save").length, 0);
  pending = dispatch(remove(), "click");
  await settle();
  dialog = body.children.find(node => node.className === "fa-confirm");
  await dispatch(findButton(dialog, "删除"), "click");
  await pending;
  await settle();
  assert.deepEqual(hostCalls(h, "observations.save").map(call => call.params), [{ action: "delete", id: "existing" }]);
});

test("a restricted scheme context hides every observation edit control", async () => {
  const labels = ["记录我的观察", "记录观察", "修正", "编辑", "删除"];
  const shown = async extra => {
    const data = personaData();
    data.observations = [{ id: "existing", version: 2, dimension: "big5_e", judgement: "synthetic observation", note: "" },
      { id: "other", version: 1, dimension: "", judgement: "free observation", note: "" }];
    const h = personaHarness(data, [{ entry: "observation:new", generation: 1, seq: 1,
      payload: { dimension: "mbti", judgement: "restored", note: "" } }], extra);
    await h.context.FriendPersona.load();
    await h.context.FriendPersona.restoreDrafts(await h.app.loadDrafts());
    const page = h.byId("pagePersona");
    return { h, page, buttons: descendants(page).filter(node => node.tagName === "button").map(node => node.textContent) };
  };
  const restricted = await shown({ restricted: true });
  assert.equal(restricted.h.app.restricted, true);
  for (const label of labels) assert.ok(!restricted.buttons.includes(label), label);
  assert.equal(observationForm(restricted.page), undefined, "no draft form reopens in a read-only window");
  assert.ok(descendants(restricted.page).some(node => node.textContent === "free observation"), "records stay visible");
  const ordinary = await shown({});
  assert.equal(ordinary.h.app.restricted, false);
  for (const label of ["记录观察", "修正", "编辑", "删除"]) assert.ok(ordinary.buttons.includes(label), label);
});

test("dimension drafts and saved observations remain accessible before the first analysis", async () => {
  const data = personaData();
  data.result = null;
  data.observations = [{ id: "existing", version: 2, dimension: "openness", judgement: "existing observation", note: "" }];
  const h = personaHarness(data, [{ entry: "observation:new", generation: 1, seq: 1,
    payload: { dimension: "mbti", judgement: "restored before analysis", note: "" } }]);
  await h.context.FriendPersona.restoreDrafts(await h.app.loadDrafts());
  assert.ok(observationForm(h.byId("pagePersona")));
  assert.ok(findButton(h.byId("pagePersona"), "编辑"));
});

test("restored observation edits retain the original version and keep the draft on conflict", async () => {
  const data = personaData();
  data.observations = [{ id: "existing", version: 2, dimension: "mbti", judgement: "newer saved observation", note: "" }];
  const h = pageHarness("friend-persona.js", async (target, method) => {
    if (target === "plugin") return data;
    if (method === "drafts.list") return { closedGenerations: {}, drafts: [{ entry: "observation:existing",
      generation: 2, seq: 3, baseVersion: 1, payload: { dimension: "mbti", judgement: "older unfinished edit", note: "" } }] };
    if (method === "drafts.save") return { saved: true };
    assert.equal(method, "observations.save");
    return { error: { code: "version-conflict", message: "观察已更新" } };
  });
  await h.context.FriendPersona.restoreDrafts(await h.app.loadDrafts());
  const form = observationForm(h.byId("pagePersona"));
  const input = descendants(form).find(node => node.tagName === "input");
  input.value = "edited restored draft";
  await dispatch(input, "input");
  await h.app.flushDrafts();
  assert.equal(hostCalls(h, "drafts.save")[0].params.baseVersion, 1);
  await dispatch(findButton(form, "保存"), "click");
  assert.equal(hostCalls(h, "observations.save")[0].params.expectedVersion, 1);
  assert.equal(data.observations[0].judgement, "newer saved observation");
  assert.ok(observationForm(h.byId("pagePersona")));
  assert.equal(h.app.savedDrafts().has("observation:existing"), true);
  assert.equal(hostCalls(h, "drafts.close").length, 0);
});

test("cached legacy portraits survive collapse, tab reload and reopening without refetching", async () => {
  let portraitCalls = 0;
  const h = pageHarness("friend-chat.js", async (target, method) => {
    if (target === "plugin") {
      assert.equal(method, "page.chat");
      return { configured: true, statistics: null, reply: { eligible: 0, observing: 0 } };
    }
    assert.equal(method, "legacy.portrait");
    portraitCalls++;
    return { available: true, items: [{ source: "api", label: "API · synthetic", kind: "portrait",
      portrait: { summary: "synthetic legacy summary", traits: {}, affinity: null } }] };
  });
  await h.context.FriendChat.load();
  const root = h.byId("pageChat");
  let details = root.querySelector("details[data-key='legacy']");
  details.open = true;
  await dispatch(details, "toggle");
  await settle();
  details = root.querySelector("details[data-key='legacy']");
  assert.equal(details.open, true);
  assert.ok(descendants(details).some(node => node.textContent === "synthetic legacy summary"));
  details.open = false;
  // Reload can run before the browser delivers the closing toggle event.
  await h.context.FriendChat.load();
  details = root.querySelector("details[data-key='legacy']");
  assert.equal(details.open, false);
  details.open = true;
  await dispatch(details, "toggle");
  await settle();
  assert.ok(descendants(details).some(node => node.textContent === "synthetic legacy summary"));
  assert.equal(portraitCalls, 1);
});

test("an account change reported by the host masks the scheme page", async () => {
  const h = pageHarness("friend-persona.js", async () => ({ error: { code: "account-changed", message: "账号已变化" } }));
  await h.context.FriendPersona.load();
  assert.equal(h.app.masked, true);
  await assert.rejects(h.app.rpc("page.persona"));
  assert.equal(h.calls.length, 1, "a masked page makes no further calls");
});
