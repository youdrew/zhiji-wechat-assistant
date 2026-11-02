// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = readFileSync(path.join(root, "chatui/model-settings.js"), "utf8");
const html = readFileSync(path.join(root, "chatui/index.html"), "utf8");
const app = readFileSync(path.join(root, "chatui/app.js"), "utf8");
const fieldIds = { searchSource: "selectModelSearchSource",
  searxngUrl: "inputModelSearxngUrl", searchOpenPages: "inputModelSearchOpenPages" };
// 统一调度总额（maxConcurrentTasks）已退役：并发随每个模型下发，旧响应多带的字段被忽略。
const defaults = { searchSource: "none", searxngUrl: "http://127.0.0.1:8080", searchOpenPages: true };
const snapshot = (revision = 0, values = {}) => ({ revision, values: { ...defaults, ...values } });
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function fixture() {
  const nodes = new Map(), calls = [], timers = new Map(); let timer = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { value: "", checked: false, textContent: "", hidden: false, disabled: false,
      attributes: {}, listeners: {}, setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(key, listener) { this.listeners[key] = listener; },
      click() { if (!this.disabled) this.listeners.click?.(); } });
    return nodes.get(id);
  };
  for (const [key, id] of Object.entries(fieldIds)) {
    if (key === "searchOpenPages") node(id).checked = defaults[key];
    else node(id).value = String(defaults[key]);
  }
  const controls = { responder: async () => response(snapshot()) };
  const window = {};
  vm.runInNewContext(source, { window, document: { getElementById: node }, URL, AbortController,
    fetch: async (url, options) => { calls.push({ url, options, body: options.body ? JSON.parse(options.body) : null }); return controls.responder(url, options); },
    setTimeout: callback => { timers.set(++timer, callback); return timer; }, clearTimeout: id => timers.delete(id),
    localStorage: { setItem() { throw new Error("Core settings must not use browser storage"); } },
  });
  const edit = (key, value) => {
    const field = node(fieldIds[key]);
    if (key === "searchOpenPages") field.checked = value;
    else field.value = String(value);
    field.listeners[key === "searchOpenPages" || key === "searchSource" ? "change" : "input"]();
  };
  return { ui: window.CoreModelSettings, node, controls, calls, timers, edit,
    value: key => key === "searchOpenPages" ? node(fieldIds[key]).checked : node(fieldIds[key]).value,
    status: () => node("coreModelSettingsStatus").textContent };
}

test("core settings live in the model panel and save independently of API activation", async () => {
  const section = html.slice(html.indexOf('id="panelModels"'), html.indexOf('id="panelPlugins"'));
  for (const id of ["coreModelSettings", ...Object.values(fieldIds)]) assert.ok(section.includes(`id="${id}"`));
  assert.ok(!section.includes("inputModelMaxConcurrentTasks"), "统一调度总额字段已退役");
  assert.match(html, /<script src="model-settings.js"><\/script>/);
  assert.match(app, /tab\.dataset\.tab === "models"\) void window\.CoreModelSettings\?\.refresh\(\)/);
  const f = fixture();
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, true);
  assert.equal(f.calls.length, 0);
  await f.ui.refresh();
  assert.equal(f.value("searchSource"), "none");
  assert.equal(f.node("coreSearchOptions").hidden, true);
  f.edit("searchOpenPages", false);
  f.controls.responder = async (_, options) => response(snapshot(1, JSON.parse(options.body).values));
  await f.ui.save();
  assert.deepEqual(f.calls.at(-1).body, snapshot(0, { searchOpenPages: false }));
  assert.ok(f.calls.every(call => call.url === "/api/model-settings"));
  assert.match(f.status(), /已保存/);
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, true);
  assert.equal(f.timers.size, 0);
});

test("reopening settings preserves edits and merges only unchanged fields from a newer revision", async () => {
  const f = fixture(); await f.ui.refresh(); f.edit("searchOpenPages", false);
  f.controls.responder = async () => response(snapshot(1, { searchOpenPages: true, searchSource: "searxng", searxngUrl: "https://search.example.test" }));
  await f.ui.refresh();
  assert.equal(f.value("searchOpenPages"), false);
  assert.equal(f.value("searchSource"), "searxng");
  assert.equal(f.value("searxngUrl"), "https://search.example.test");
  assert.equal(f.node("coreSearchOptions").hidden, false);
  assert.match(f.status(), /其他窗口更新/);
  assert.equal(f.node("btnSaveCoreModelSettings").textContent, "确认保存我的更改");
  f.controls.responder = async (_, options) => response(snapshot(2, JSON.parse(options.body).values));
  await f.ui.save();
  assert.deepEqual(f.calls.at(-1).body, snapshot(1, { searchOpenPages: false, searchSource: "searxng", searxngUrl: "https://search.example.test" }));
});

test("a save conflict reads the latest state without silently retrying the write", async () => {
  const f = fixture(); await f.ui.refresh(); f.edit("searchSource", "searxng"); f.edit("searchOpenPages", false);
  f.controls.responder = async (_, options) => options.method === "POST" ? response({ error: "conflict" }, 409)
    : response(snapshot(4, { searchSource: "searxng", searxngUrl: "http://search.example.test:8888" }));
  await f.ui.save();
  assert.equal(f.calls.filter(call => call.options.method === "POST").length, 1);
  assert.equal(f.value("searchOpenPages"), false);
  assert.equal(f.value("searchSource"), "searxng");
  assert.equal(f.value("searxngUrl"), "http://search.example.test:8888");
  assert.equal(f.node("btnUseLatestModelSettings").hidden, false);
  f.node("btnUseLatestModelSettings").click();
  assert.equal(f.value("searchSource"), "searxng");
  assert.equal(f.value("searchOpenPages"), true);
  assert.equal(f.value("searxngUrl"), "http://search.example.test:8888");
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, true);
  assert.equal(f.node("btnUseLatestModelSettings").hidden, true);
});

test("late reads cannot replace newer settings even if transport ignores abort", async () => {
  const f = fixture(), pending = deferred();
  f.controls.responder = () => pending.promise;
  const oldRead = f.ui.refresh();
  f.controls.responder = async () => response(snapshot(2, { searxngUrl: "http://newer.example.test" }));
  await f.ui.refresh();
  pending.resolve(response(snapshot(1, { searxngUrl: "http://older.example.test" }))); await oldRead;
  assert.equal(f.value("searxngUrl"), "http://newer.example.test");
  assert.equal(f.calls[0].options.signal.aborted, true);
  assert.equal(f.node(fieldIds.searchSource).disabled, false);
});

test("failed reads preserve drafts and require successful recovery before saving", async () => {
  const f = fixture(); await f.ui.refresh(); f.edit("searxngUrl", "http://draft.example.test");
  f.controls.responder = async () => response({}, 503);
  await f.ui.refresh();
  assert.equal(f.value("searxngUrl"), "http://draft.example.test");
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, true);
  assert.match(f.status(), /HTTP 503.*编辑已保留/);
  await f.ui.save(); assert.equal(f.calls.length, 2);
  f.controls.responder = async () => response(snapshot()); await f.ui.refresh();
  assert.equal(f.value("searxngUrl"), "http://draft.example.test");
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, false);
  assert.match(f.status(), /未保存/);
});

test("HTTP write failures preserve edits for retry and malformed reads never enable defaults", async () => {
  const f = fixture(); await f.ui.refresh(); f.edit("searxngUrl", "http://draft.example.test");
  f.controls.responder = async () => response({}, 500);
  await f.ui.save();
  assert.match(f.status(), /保存失败（HTTP 500）/);
  assert.equal(f.value("searxngUrl"), "http://draft.example.test");
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, false);
  const malformed = fixture(); malformed.controls.responder = async () => response({ revision: 3, values: { searchSource: "none" } });
  await malformed.ui.refresh();
  assert.match(malformed.status(), /读取失败/);
  assert.equal(malformed.node(fieldIds.searchSource).disabled, true);
  assert.equal(malformed.node("btnSaveCoreModelSettings").disabled, true);
});

test("an ambiguous timeout requires rereading and recognizes a write that reached Core", async () => {
  const f = fixture(); await f.ui.refresh(); f.edit("searchOpenPages", false);
  f.controls.responder = (_, options) => new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted"))));
  const saving = f.ui.save();
  for (const timeout of [...f.timers.values()]) timeout();
  await saving;
  assert.match(f.status(), /保存结果未确认（请求超时）/);
  assert.equal(f.value("searchOpenPages"), false);
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, true);
  f.controls.responder = async () => response(snapshot(1, { searchOpenPages: false })); await f.ui.refresh();
  assert.equal(f.node("btnSaveCoreModelSettings").disabled, true);
  assert.equal(f.node("btnUseLatestModelSettings").hidden, true);
  assert.doesNotMatch(f.status(), /未保存/);
});

test("enabled search validates URLs but disabled search can retain an unfinished address", async () => {
  for (const value of ["", "file:///tmp/search", "https:example.test", "https://name:secret@example.test", "https://example.test?q=x",
    "https://example.test#fragment", "http://example.test:invalid", "http://example.test:0", "https://example.test/a b",
    "https://example.test/a b", "https://example.test/\\path", "x".repeat(513)]) {
    const f = fixture(); await f.ui.refresh(); f.edit("searchSource", "searxng"); f.edit("searxngUrl", value); await f.ui.save();
    assert.equal(f.calls.length, 1, value);
    assert.match(f.status(), /SearXNG/);
  }
  const f = fixture(); await f.ui.refresh(); f.edit("searxngUrl", "unfinished address");
  f.controls.responder = async (_, options) => response(snapshot(1, JSON.parse(options.body).values));
  await f.ui.save();
  assert.equal(f.calls.at(-1).body.values.searxngUrl, "unfinished address");
  assert.match(f.status(), /已保存/);
});

test("successful saves trim the search address and preserve a disabled page-reading choice", async () => {
  const f = fixture(); await f.ui.refresh(); f.edit("searchSource", "searxng");
  f.edit("searxngUrl", "  https://search.example.test/subpath  "); f.edit("searchOpenPages", false);
  f.controls.responder = async (_, options) => response(snapshot(1, JSON.parse(options.body).values));
  await f.ui.save();
  assert.equal(f.calls.at(-1).body.values.searxngUrl, "https://search.example.test/subpath");
  assert.equal(f.calls.at(-1).body.values.searchOpenPages, false);
  assert.equal(f.value("searxngUrl"), "https://search.example.test/subpath");
});
