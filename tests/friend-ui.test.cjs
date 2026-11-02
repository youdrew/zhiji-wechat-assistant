// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic renderer tests for the 知己分析 window core: masking, API scope and drafts.
// Modified 2026-10-13: the analysis pages are the kyf.friend-analysis scheme page in a sandboxed
// frame; the window builds its tabs from the scheme manifest and answers the page's host calls.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const core = readFileSync(path.join(__dirname, "../chatui/friend-core.js"), "utf8");
const html = readFileSync(path.join(__dirname, "../chatui/friend.html"), "utf8");
const schemeHost = readFileSync(path.join(__dirname, "../chatui/friend-scheme.js"), "utf8");
const frameHost = readFileSync(path.join(__dirname, "../chatui/plugin-frame.js"), "utf8");
const pluginRoot = path.join(__dirname, "../plugins/kyf.friend-analysis");
const manifest = JSON.parse(readFileSync(path.join(pluginRoot, "plugin.json"), "utf8"));
const page = readFileSync(path.join(pluginRoot, "ui/index.html"), "utf8");

function node() {
  return { hidden: false, textContent: "", children: [], listeners: {}, className: "", dataset: {}, style: {},
    replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); },
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }, setAttribute() {} };
}
function harness(hash = "account=synthetic-account&user=wxid_friend_a&name=%E6%9C%8B%E5%8F%8B") {
  const nodes = new Map(), requests = [], channels = [];
  const responses = [];
  class Channel {
    constructor(name) { this.name = name; this.listeners = []; channels.push(this); }
    addEventListener(_type, listener) { this.listeners.push(listener); }
    postMessage() { }
    deliver(data) { return Promise.all(this.listeners.map(listener => listener({ data }))); }
  }
  const context = {
    location: { hash: "#" + hash }, localStorage: { getItem: () => null }, crypto: webcrypto, TextEncoder,
    BroadcastChannel: Channel, setTimeout, clearTimeout, URLSearchParams, Promise, JSON,
    document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
      createElement: () => node(), body: { classList: { toggle() { } } } },
    addEventListener() { },
    fetch: async (url, init) => {
      requests.push({ url, init, body: init.body ? JSON.parse(init.body) : null });
      const next = responses.shift() || { status: 200, body: { saved: true } };
      return { ok: next.status < 400, status: next.status, json: async () => next.body };
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(core, context);
  return { app: context.FriendApp, nodes, requests, channels, responses };
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

const scripts = source => [...source.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);

test("the window and the scheme page load only local scripts under a strict content policy", () => {
  assert.match(html, /default-src 'none'; script-src 'self'/);
  for (const source of [html, page]) {
    assert.doesNotMatch(source, /<script>[^<]/, "no inline script");
    assert.doesNotMatch(source, /\son[a-z]+=/, "no inline event handlers");
  }
  assert.deepEqual(scripts(html), ["/friend-core.js", "/friend-memory.js", "/friend-run.js", "/plugin-frame.js", "/friend-scheme.js",
    "/friend.js"]);
  // The analysis pages run in the plugin's sandboxed frame, never inside the host window.
  assert.deepEqual(scripts(page), ["/plugin-sdk/web/kyf-host.js", "/plugin-sdk/web/time-trend.js", "/plugin-sdk/web/persona-charts.js",
    "page-core.js", "friend-trend-math.js",
    "friend-trend.js", "friend-overview.js", "friend-persona.js", "friend-chat.js", "page.js"]);
  assert.doesNotMatch(html, /friend-insight\.js/);
});

class Element {
  constructor(tag = "div") {
    Object.assign(this, { tagName: tag, hidden: false, textContent: "", className: "", children: [], dataset: {},
      attributes: {}, listeners: {}, style: {} });
    if (tag === "iframe") this.contentWindow = { messages: [], postMessage(message) { this.messages.push(message); } };
  }
  replaceChildren(...items) { this.children = items; }
  append(...items) { this.children.push(...items); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) this.dataset[name.slice(5)] = String(value);
  }
  querySelector() { return null; }
}
function schemeHarness(storage = new Map()) {
  const nodes = new Map(), requests = [], listeners = {}, schemes = [], memoryLoads = [];
  const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const available = manifest.contributes.schemes.map(scheme => ({ plugin: manifest.id, pluginName: manifest.name,
    pluginVersion: manifest.version, ...scheme }));
  const context = {
    location: { hash: "#account=synthetic-account&user=wxid_friend_a&name=Friend" },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    crypto: webcrypto, TextEncoder, URLSearchParams, Promise, JSON, CSS: { escape: value => value },
    setTimeout: () => 0, clearTimeout() { },
    document: { getElementById: byId, createElement: tag => new Element(tag),
      body: { classList: { toggle() { }, contains: () => false } } },
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    fetch: async (url, init) => {
      const body = init.body ? JSON.parse(init.body) : null;
      requests.push({ url, method: init.method, body });
      const reply = url.startsWith("/api/schemes?") ? { schemes: available, selected: { plugin: manifest.id, scheme: "default" } } :
        url === "/api/plugins/rpc" ? { result: { echoed: body.method } } : { saved: true };
      return { ok: true, status: 200, json: async () => reply };
    },
  };
  context.window = context;
  context.FriendRun = { setScheme: scheme => schemes.push(scheme) };
  context.FriendMemory = { load: async () => { memoryLoads.push(true); } };
  vm.createContext(context);
  vm.runInContext(core, context);
  vm.runInContext(frameHost, context);
  vm.runInContext(schemeHost, context);
  const frame = () => byId("faSchemeFrame").children[0];
  const settle = async () => { for (let index = 0; index < 5; index++) await new Promise(resolve => setImmediate(resolve)); };
  async function fromPage(data) {
    await Promise.all((listeners.message || []).map(listener => listener({ source: frame().contentWindow, data })));
    await settle();
    return frame().contentWindow.messages.at(-1);
  }
  return { context, byId, requests, schemes, memoryLoads, frame, fromPage, available, listeners, storage };
}

test("tabs follow the scheme manifest with 关系记忆 from the host, and 好友概述 is selected first", async () => {
  const h = schemeHarness();
  await h.context.FriendScheme.load();
  const tabs = h.byId("faTabs").children;
  assert.deepEqual(tabs.map(tab => tab.textContent), ["好友概述", "关系记忆", "性格分析", "三观分析", "聊天分析", "关系趋势"]);
  assert.deepEqual(tabs.filter(tab => tab.attributes["aria-selected"] === "true").map(tab => tab.dataset.tab), ["overview"]);
  assert.equal(h.byId("faSchemeLabel").hidden, true, "one scheme needs no picker");
  assert.deepEqual(JSON.parse(JSON.stringify(h.schemes)), [{ plugin: "kyf.friend-analysis", scheme: "default", name: "默认方案" }]);
  const frame = h.frame();
  assert.equal(frame.src, "/plugin-pages/kyf.friend-analysis/ui/index.html");
  assert.equal(frame.attributes.sandbox, "allow-scripts", "the page never shares the window's origin");
  h.context.FriendScheme.selectTab("memories");
  assert.equal(h.byId("pageMemory").hidden, false);
  assert.equal(h.byId("faSchemeFrame").hidden, true);
  assert.equal(h.memoryLoads.length, 1);
  assert.equal(html.match(/id="faRunButton"/g).length, 1);
  assert.match(html, /data-menu="rebuild">重新分析全部…</);
});

test("the scheme page reaches Core only through calls fixed to this window's friend", async () => {
  const h = schemeHarness();
  await h.context.FriendScheme.load();
  const ready = await h.fromPage({ kyf: 1, type: "ready" });
  assert.deepEqual(JSON.parse(JSON.stringify(ready.context)), { account: "synthetic-account", user: "wxid_friend_a", name: "Friend",
    plugin: "kyf.friend-analysis", scheme: "default", tab: "overview", theme: "dark", surface: "scheme",
    trendSettings: { unit: "month", metric: "overview", log: false, zoom: {} } });
  const rpc = await h.fromPage({ kyf: 1, type: "call", id: "c1", target: "plugin", method: "page.persona",
    params: { user: "someone-else" } });
  assert.deepEqual(rpc.result, { echoed: "page.persona" });
  assert.deepEqual(h.requests.at(-1).body, { plugin: "kyf.friend-analysis", method: "page.persona",
    params: { user: "someone-else" }, account: "synthetic-account", user: "wxid_friend_a" });
  await h.fromPage({ kyf: 1, type: "call", id: "c2", target: "host", method: "drafts.save",
    params: { entry: "observation:new", generation: 1, seq: 1, payload: { judgement: "x" }, account: "other" } });
  assert.deepEqual(h.requests.at(-1), { url: "/api/friend/draft", method: "POST", body: { entry: "observation:new",
    generation: 1, seq: 1, payload: { judgement: "x" }, account: "synthetic-account", user: "wxid_friend_a" } });
  const count = h.requests.length;
  const foreign = await h.fromPage({ kyf: 1, type: "call", id: "c3", target: "host", method: "drafts.save",
    params: { entry: "goal", generation: 1, seq: 1, payload: {} } });
  assert.equal(foreign.error.code, "invalid-request", "the page cannot touch the window's own drafts");
  const unknown = await h.fromPage({ kyf: 1, type: "call", id: "c4", target: "host", method: "memory.delete" });
  assert.equal(unknown.error.code, "unknown-method");
  assert.equal(h.requests.length, count);
});

test("trend display settings persist in the host, merge zooms and synchronize other windows", async () => {
  const storage = new Map(), first = schemeHarness(storage), second = schemeHarness(storage);
  for (const h of [first, second]) {
    await h.context.FriendScheme.load();
    await h.fromPage({ kyf: 1, type: "ready" });
  }
  for (const params of [{ unit: "day" }, { metric: "reply" }, { log: true },
    { zoom: { unit: "hour", slot: 24 } }, { zoom: { unit: "day", slot: 16 } }]) {
    const result = await first.fromPage({ kyf: 1, type: "call", id: "setting", target: "host", method: "trend.settings", params });
    assert.equal(result.result.ok, true);
  }
  assert.equal(storage.get("kyf-friend-trend-granularity"), "day");
  assert.equal(storage.get("kyf-friend-trend-metric"), "reply");
  assert.equal(storage.get("kyf-friend-trend-log"), "1");
  const expected = { unit: "day", metric: "reply", log: true, zoom: { hour: 24, day: 16 } };
  const latest = await first.fromPage({ kyf: 1, type: "call", id: "latest", target: "host", method: "trend.settings.get" });
  assert.deepEqual(JSON.parse(JSON.stringify(latest.result)), expected);
  for (const listener of second.listeners.storage) listener({ key: "kyf-friend-trend-granularity" });
  assert.deepEqual(JSON.parse(JSON.stringify(second.frame().contentWindow.messages.at(-1).context.trendSettings)), expected);
  const reopened = schemeHarness(storage);
  await reopened.context.FriendScheme.load();
  const ready = await reopened.fromPage({ kyf: 1, type: "ready" });
  assert.deepEqual(JSON.parse(JSON.stringify(ready.context.trendSettings)), expected);
});

test("the trend setting capability rejects arbitrary storage access and invalid values", async () => {
  const h = schemeHarness();
  await h.context.FriendScheme.load();
  for (const params of [{}, { key: "private-key", value: "x" }, { unit: "minute" },
    { unit: "day", metric: "reply" }, { metric: "unknown" }, { log: "1" },
    { zoom: { unit: "unknown", slot: 20 } }, { zoom: { unit: "day", slot: 61 } },
    { zoom: { unit: "day", slot: 1 } }, { zoom: { unit: "day", slot: "20" } },
    { zoom: { unit: "day", slot: 20, extra: true } }]) {
    const result = await h.fromPage({ kyf: 1, type: "call", id: "invalid", target: "host", method: "trend.settings", params });
    assert.equal(result.error.code, "invalid-request");
  }
  assert.equal(h.storage.size, 0);
  h.storage.set("kyf-friend-trend-granularity", "bad");
  h.storage.set("kyf-friend-trend-metric", "bad");
  h.storage.set("kyf-friend-trend-zoom", '{"day":-1,"hour":20,"private":10}');
  for (const listener of h.listeners.storage) listener({ key: "kyf-friend-trend-zoom" });
  const ready = await h.fromPage({ kyf: 1, type: "ready" });
  assert.deepEqual(JSON.parse(JSON.stringify(ready.context.trendSettings)),
    { unit: "month", metric: "overview", log: false, zoom: { hour: 20 } });
});

test("removing the last available scheme retires its frame and run controls", async () => {
  const h = schemeHarness();
  await h.context.FriendScheme.load();
  const previous = h.frame();
  h.available.splice(0);
  await h.context.FriendScheme.load();
  assert.equal(h.context.FriendScheme.scheme, null);
  assert.equal(h.context.FriendScheme.tab, null);
  assert.equal(h.schemes.at(-1), null);
  assert.equal(h.byId("faRunButton").hidden, true);
  assert.equal(h.byId("faSchemeLabel").hidden, true);
  const count = h.requests.length;
  for (const listener of h.listeners.message || []) listener({ source: previous.contentWindow,
    data: { kyf: 1, type: "call", id: "stale", target: "plugin", method: "page.persona" } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.length, count, "a removed plugin page cannot keep issuing RPC calls");
});

test("scope comes from the window hash and rejects group chats", () => {
  assert.equal(harness().app.validScope, true);
  assert.equal(harness("account=a&user=123%40chatroom").app.validScope, false);
  assert.equal(harness("user=only").app.validScope, false);
});

test("another account or a cleared account masks the window permanently", async () => {
  const h = harness();
  const channel = h.channels[0];
  await channel.deliver({ type: "account", value: null });
  await channel.deliver({ type: "account", value: "synthetic-account" });
  assert.equal(h.app.masked, false, "a transient refresh of the same account keeps the window usable");
  await channel.deliver({ type: "account", value: "another-account" });
  assert.equal(h.app.masked, true);
  await assert.rejects(h.app.api("/api/friend/overview"), error => error.code === "masked");
  assert.equal(h.requests.length, 0);
  const cleared = harness();
  const digest = Buffer.from(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode("synthetic-account"))).toString("hex");
  await cleared.channels[0].deliver({ type: "account-cleared", value: digest });
  assert.equal(cleared.app.masked, true);
});

test("every request carries the window's own account and contact; boundary errors mask", async () => {
  const h = harness();
  h.responses.push({ status: 200, body: { account: "synthetic-account", ok: true } });
  await h.app.api("/api/friend/open", { method: "POST", body: { user: "someone-else" } });
  assert.equal(h.requests[0].body.account, "synthetic-account");
  assert.equal(h.requests[0].body.user, "wxid_friend_a", "the fixed contact cannot be overridden");
  h.responses.push({ status: 200, body: { account: "different-account" } });
  await assert.rejects(h.app.api("/api/friend/overview"));
  assert.equal(h.app.masked, true);
  const changed = harness();
  changed.responses.push({ status: 503, body: { error: "AccountChangedError" } });
  await assert.rejects(changed.app.api("/api/friend/overview"));
  assert.equal(changed.app.masked, true);
});

test("drafts save the newest edit, never revive after close and flush before closing", async () => {
  const h = harness();
  h.responses.push({ status: 200, body: { drafts: [], closedGenerations: { "memory:new": 3 } } });
  await h.app.loadDrafts();
  const draft = h.app.draft("memory:new");
  assert.equal(draft.generation, 4, "a closed generation is never reused");
  h.app.updateDraft(draft, { text: "第一版" });
  h.app.updateDraft(draft, { text: "第二版" });
  assert.equal(await h.app.flushDrafts(), true);
  const saves = h.requests.filter(request => request.url === "/api/friend/draft");
  assert.equal(saves.length, 1, "debounced edits are sent once");
  assert.deepEqual(saves[0].body.payload, { text: "第二版" });
  assert.equal(saves[0].body.seq, 1);
  h.app.updateDraft(draft, { text: "第三版" });
  await h.app.closeDraft("memory:new");
  await wait(600);
  const after = h.requests.filter(request => request.url === "/api/friend/draft");
  assert.equal(after.length, 2, "closing cancels the pending save");
  assert.deepEqual(after[1].body, { account: "synthetic-account", user: "wxid_friend_a", entry: "memory:new",
    generation: 4, close: true });
  assert.equal(h.app.draft("memory:new").generation, 5);
  h.responses.push({ status: 500, body: { error: "disk" } });
  const failing = h.app.draft("goal");
  h.app.updateDraft(failing, { text: "目标" });
  assert.equal(await h.app.flushDrafts(), false, "a failed save keeps the window open");
});
