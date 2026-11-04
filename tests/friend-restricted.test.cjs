// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic checks for the restricted (Agent-opened, read-only) 知己分析 window: the hash flag from
// scripts/ui-host.cjs, never calling /api/friend/open, hidden write actions, ui-session masking and the
// host's select-tab event. The ordinary window keeps its behaviour.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

class Element {
  constructor(tag = "") {
    Object.assign(this, { tagName: tag, className: "", textContent: "", children: [], dataset: {}, listeners: {},
      style: {}, hidden: false, disabled: false, parentNode: null, parentElement: { scrollTop: 0 } });
  }
  append(...items) { for (const item of items) { if (item && typeof item === "object") item.parentNode = this; this.children.push(item); } }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  setAttribute(name, value) { this[name] = value; }
  contains(node) { return descendants(this).includes(node); }
  querySelector() { return null; }
  focus() {}
  get lastChild() { return this.children.at(-1); }
}
const descendants = node => [node, ...(node.children || []).flatMap(descendants)];
const buttons = root => descendants(root).filter(node => node.tagName === "button").map(node => node.textContent);
const settle = async () => { for (let index = 0; index < 6; index++) await new Promise(resolve => setImmediate(resolve)); };

const HASH = "account=synthetic-account&user=wxid_friend_a&name=Friend";
const OVERVIEW = { account: "synthetic-account", history: { state: "ready", readCount: 3, refreshing: false },
  memory: { built: true }, memoryVersion: 1 };
const MEMORIES = {
  pending: { items: [{ id: "p1", text: "喜欢爬山", tags: ["爱好"], status: "pending", relation: "new", version: 1 }], total: 1 },
  confirmed: { items: [{ id: "c1", text: "不吃香菜", tags: ["偏好与忌口"], status: "confirmed", version: 2, evidenceCount: 1 }],
    total: 1, memoryVersion: 1 },
};

function harness({ hash = HASH + "&restricted=1", respond, withScheme = false } = {}) {
  const nodes = new Map(), requests = [], hostListeners = [], selected = [], frames = [], runs = { refresh: 0 };
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id); };
  const ctx = {
    location: { hash: "#" + hash }, localStorage: { getItem: () => null }, crypto: webcrypto, TextEncoder, URLSearchParams,
    CSS: { escape: value => value }, BroadcastChannel: class { addEventListener() {} postMessage() {} },
    addEventListener() {}, setTimeout() { return 0; }, clearTimeout() {}, confirm: () => true,
    document: { getElementById: byId, createElement: tag => new Element(tag), createDocumentFragment: () => new Element("#fragment"),
      activeElement: null, addEventListener() {}, body: Object.assign(new Element("body"), { classList: { toggle() {}, contains: () => false } }) },
    friendHost: {
      onBeforeClose() { return () => {}; }, closeReady() {}, locate: async () => true,
      onHostEvent(listener) { hostListeners.push(listener); return () => {}; },
    },
    FriendScheme: { load: async () => {}, selectTab: tab => selected.push(tab), refresh() {}, historyChanged() {},
      flush: async () => true },
    FriendRun: { refresh: async () => { runs.refresh++; }, invalidate() {}, stop() {}, setScheme() {} },
    KyfFrames: { create: options => { frames.push(options); return { post() {}, event() {}, destroy() {}, ready: true }; } },
    fetch: async (url, init) => {
      const parsed = new URL(url, "http://synthetic.local");
      requests.push({ path: parsed.pathname, method: init.method, query: parsed.searchParams });
      const reply = respond?.(parsed.pathname, init.method, parsed.searchParams) ||
        (parsed.pathname === "/api/friend/memories" ? MEMORIES[parsed.searchParams.get("status")] || { items: [], total: 0 } :
          parsed.pathname === "/api/friend/drafts" ? { drafts: [], closedGenerations: {} } : OVERVIEW);
      const status = reply.status || 200;
      return { ok: status < 400, status, json: async () => reply.status ? reply.body : reply };
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const file of ["friend-core.js", "friend-memory.js", ...(withScheme ? ["friend-scheme.js"] : []), "friend.js"])
    vm.runInContext(readFileSync(path.join(__dirname, "../chatui", file), "utf8"), ctx);
  const hostEvent = async event => { for (const listener of hostListeners) listener(event); await settle(); };
  return { ctx, app: ctx.FriendApp, byId, requests, selected, hostEvent, frames, runs };
}
const opened = requests => requests.filter(request => request.path === "/api/friend/open");

test("a restricted window reads the overview and never calls /api/friend/open", async () => {
  const h = harness({ hash: HASH + "&restricted=1&theme=light&tab=persona" });
  await settle();
  assert.equal(h.app.restricted, true);
  assert.equal(h.app.initialTab, "persona");
  assert.deepEqual(opened(h.requests), []);
  assert.ok(h.requests.some(request => request.path === "/api/friend/overview" && request.method === "GET"));
  assert.ok(!h.requests.some(request => request.path === "/api/friend/drafts"), "no drafts are restored");
  assert.deepEqual(h.selected, ["persona"]);
  // The host's focus event refreshes with a read, still never an open.
  await h.hostEvent({ type: "focus" });
  assert.deepEqual(opened(h.requests), []);
  assert.ok(h.requests.every(request => request.method === "GET"), "the window itself only reads");
});

test("a restricted window hides run controls, the settings menu and scheme switching", async () => {
  const h = harness();
  await settle();
  assert.equal(h.byId("faRunControl").hidden, true);
  assert.equal(h.byId("faMoreButton").hidden, true);
  assert.equal(h.byId("faScheme").disabled, true);
});

test("restricted memories show records and sources without review, edit or add actions", async () => {
  const h = harness();
  await settle();
  await h.ctx.FriendMemory.load();
  await settle();
  const labels = buttons(h.byId("pageMemory"));
  assert.ok(labels.includes("原文") && labels.includes("按标签"), labels.join(","));
  for (const label of ["新增", "确认", "忽略", "编辑", "删除", "全部确认", "全部忽略", "确认本组", "忽略本组"])
    assert.ok(!labels.includes(label), label);

  const plain = harness({ hash: HASH });
  await settle();
  await plain.ctx.FriendMemory.load();
  await settle();
  const ordinary = buttons(plain.byId("pageMemory"));
  for (const label of ["新增", "确认", "编辑", "删除", "全部确认"]) assert.ok(ordinary.includes(label), label);
});

test("a restricted window refuses writes locally but keeps plugin reads", async () => {
  const h = harness();
  await settle();
  const before = h.requests.length;
  for (const [route, body] of [["/api/friend/open", {}], ["/api/friend/memory", { action: "clear" }],
    ["/api/friend/observation", { action: "save" }], ["/api/friend/draft", { entry: "goal" }], ["/api/tasks", {}],
    ["/api/friend/settings", { kind: "goal" }], ["/api/schemes/select", {}], ["/api/friend/locate", {}]]) {
    await assert.rejects(h.app.api(route, { method: "POST", body }), error => error.code === "restricted", route);
  }
  assert.equal(h.requests.length, before, "nothing reached the network");
  await h.app.api("/api/plugins/rpc", { method: "POST", body: { plugin: "kyf.friend-analysis", method: "scheme.status" } });
  assert.equal(h.requests.at(-1).path, "/api/plugins/rpc");
  assert.equal(h.app.masked, false);
});

test("a ui-session 403 masks the restricted window with the Agent notice", async () => {
  const h = harness({ respond: route => route === "/api/friend/overview" ?
    { status: 403, body: { error: "ui-session-expired", message: "expired" } } : null });
  await settle();
  assert.equal(h.app.masked, true);
  assert.equal(h.byId("faMask").hidden, false);
  assert.equal(h.byId("faMaskText").textContent, "授权已失效或窗口已关闭，请回到 Agent 重新打开");
  await assert.rejects(h.app.api("/api/friend/overview?x"), error => error.code === "masked");
  // Other 403s do not mask.
  const other = harness({ respond: route => route === "/api/friend/overview" ? { status: 403, body: { error: "forbidden" } } : null });
  await settle();
  assert.equal(other.app.masked, false);
});

test("the host's select-tab event switches tabs in a restricted window", async () => {
  const h = harness();
  await settle();
  await h.hostEvent({ type: "select-tab", tab: "trend" });
  await h.hostEvent({ type: "select-tab", tab: "memory" });
  await h.hostEvent({ type: "select-tab", tab: 5 });
  assert.deepEqual(h.selected, ["trend", "memories"]);
});

test("the ordinary window still opens, ignores select-tab and the ui-session code", async () => {
  const h = harness({ hash: HASH + "&tab=persona&theme=light",
    respond: route => route === "/api/friend/drafts" ? { status: 403, body: { error: "ui-session-expired" } } : null });
  await settle();
  assert.equal(h.app.restricted, false);
  assert.equal(h.app.initialTab, null);
  assert.equal(opened(h.requests).length, 1);
  assert.equal(opened(h.requests)[0].method, "POST");
  assert.equal(h.byId("faRunControl").hidden, false);
  assert.equal(h.byId("faMoreButton").hidden, false);
  assert.equal(h.app.masked, false);
  await h.hostEvent({ type: "select-tab", tab: "trend" });
  assert.deepEqual(h.selected, []);
  await h.hostEvent({ type: "focus" });
  assert.equal(opened(h.requests).length, 2);
});

const SCHEMES = { schemes: [{ plugin: "kyf.friend-analysis", id: "default", name: "好友分析", page: "ui/index.html",
  tabs: [{ id: "overview", title: "概述" }, { host: "memories" }] }], selected: { plugin: "kyf.friend-analysis", scheme: "default" } };

test("the scheme page's drafts.list never reaches the network in a restricted window", async () => {
  // The UI session refuses /api/friend/drafts; a request would mask the whole window.
  const h = harness({ withScheme: true, respond: route => route === "/api/schemes" ? SCHEMES :
    route === "/api/friend/drafts" ? { status: 403, body: { error: "ui-session-forbidden" } } : null });
  await settle();
  assert.equal(h.frames.length, 1);
  assert.equal(JSON.stringify(await h.frames[0].host["drafts.list"]()), JSON.stringify({ drafts: [], closedGenerations: {} }));
  await assert.rejects(Promise.resolve().then(() => h.frames[0].host["drafts.save"]({ entry: "observation:x" })),
    error => error.code === "restricted");
  await h.hostEvent({ type: "focus" });
  assert.ok(!h.requests.some(request => request.path === "/api/friend/drafts"));
  assert.equal(h.app.masked, false);
  const allowed = new Set(["/api/schemes", "/api/friend/overview", "/api/friend/memories", "/api/plugins/rpc", "/api/tasks"]);
  for (const request of h.requests) assert.ok(allowed.has(request.path), request.path);

  // The scheme page learns it is read-only from its frame context and hides edit controls.
  assert.equal(h.frames[0].context.restricted, true);

  const plain = harness({ hash: HASH, withScheme: true, respond: route => route === "/api/schemes" ? SCHEMES : null });
  await settle();
  assert.equal(plain.frames[0].context.restricted, undefined);
  await plain.frames[0].host["drafts.list"]();
  assert.ok(plain.requests.some(request => request.path === "/api/friend/drafts"));
});

test("focus in a restricted window re-reads the overview without restarting run polling", async () => {
  const h = harness();
  await settle();
  const before = h.runs.refresh;
  await h.hostEvent({ type: "focus" });
  assert.equal(h.runs.refresh, before);
  assert.ok(h.requests.filter(request => request.path === "/api/friend/overview").length >= 2);
  const plain = harness({ hash: HASH });
  await settle();
  const ordinary = plain.runs.refresh;
  await plain.hostEvent({ type: "focus" });
  assert.equal(plain.runs.refresh, ordinary + 1);
});
