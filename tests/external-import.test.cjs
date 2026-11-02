// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic UI rules only. No real friend, account, image, or model is accessed.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../chatui/external-import.js"), "utf8");
const scope = { module: { exports: {} } };
vm.runInNewContext(source, scope);
const rules = scope.module.exports;
const plain = value => JSON.parse(JSON.stringify(value));
const candidate = (id, changes = {}) => ({ id, ready: true, sender: "other", text: "合成消息", ignored: false, savedMessageId: null, ...changes });
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

// Run the actual page controller with a tiny DOM and synthetic API. These are scope
// transition and import-request tests; drawing, real file I/O and model inference
// are outside this fixture.
function pageFixture({ search = "?account=account-a&user=user-a", responder, nativeHost } = {}) {
  const ids = new Map(), nodes = [], listeners = {}, calls = [], requests = [], timers = new Map(); let sequence = 0;
  const text = node => `${node.textContent || ""}${(node.children || []).map(text).join("")}`;
  const find = (node, predicate) => predicate(node) ? node : (node.children || []).map(child => find(child, predicate)).find(Boolean);
  function node(tag = "div", id) {
    const value = { tagName: tag.toUpperCase(), children: [], listeners: {}, dataset: {}, attributes: {},
      className: "", textContent: "", hidden: false, disabled: false, open: false, isConnected: true };
    Object.defineProperty(value, "id", { get: () => value._id, set(id) { value._id = id; ids.set(id, value); } });
    if (id) value.id = id;
    const detach = child => { if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(item => item !== child); };
    value.append = (...children) => { for (const child of children) { detach(child); child.parentNode = value; value.children.push(child); } };
    value.prepend = (...children) => { for (const child of children) { detach(child); child.parentNode = value; } value.children.unshift(...children); };
    value.insertBefore = (child, before) => { detach(child); const index = value.children.indexOf(before); assert.ok(index >= 0); child.parentNode = value; value.children.splice(index, 0, child); };
    value.replaceChildren = (...children) => { value.children = []; value.textContent = ""; value.append(...children); };
    Object.defineProperty(value, "firstElementChild", { get: () => value.children[0] });
    value.after = child => { const parent = value.parentNode; child.parentNode = parent; parent.children.splice(parent.children.indexOf(value) + 1, 0, child); };
    value.remove = () => { if (value.parentNode) value.parentNode.children = value.parentNode.children.filter(child => child !== value); };
    value.setAttribute = (key, data) => { value.attributes[key] = data; };
    value.removeAttribute = key => { delete value.attributes[key]; };
    value.addEventListener = (key, handler) => { (value.listeners[key] ||= []).push(handler); };
    value.querySelector = selector => find(value, item => selector.startsWith(".") ? item.className.split(/\s+/).includes(selector.slice(1)) : item.tagName.toLowerCase() === selector);
    value.querySelectorAll = selector => nodes.filter(item => selector === "button" && item.tagName === "BUTTON");
    value.classList = { add(name) { value.className += ` ${name}`; }, remove(name) { value.className = value.className.split(/\s+/).filter(item => item !== name).join(" "); },
      toggle(name, enabled) { const has = value.className.split(/\s+/).includes(name); if (enabled ?? !has) { if (!has) this.add(name); } else this.remove(name); } };
    value.close = () => { value.open = false; }; value.showModal = () => { value.open = true; };
    nodes.push(value); return value;
  }
  for (const id of ["eiContent", "eiStatus", "eiError", "eiDialog", "eiTaskCount", "eiAdd", "eiUsage", "eiSettings", "eiRefresh"]) node("div", id);
  const tabs = ["saved", "tasks"].map(page => { const tab = node("button"); tab.dataset.page = page; return tab; });
  const document = { body: node("body"), createElement: node, getElementById: id => ids.get(id),
    querySelectorAll: selector => selector === "[data-page]" ? tabs : selector === '[data-mutation="true"]' ? nodes.filter(item => item.dataset.mutation === "true") : [] };
  const navigation = [], opener = { postMessage: value => navigation.push(value), focus: () => navigation.push("focus") };
  const window = { opener, externalImportHost: nativeHost, location: { search, hash: "", origin: "http://127.0.0.1" }, close: () => navigation.push("close"),
    addEventListener: (key, handler) => { (listeners[key] ||= []).push(handler); } };
  window.parent = window;
  const friends = [{ id: "friend-a", name: "合成甲", wechatAccount: "account-a", wechatUser: "user-a" },
    { id: "friend-b", name: "合成乙", wechatAccount: "account-b", wechatUser: "user-b" }];
  const defaultResponse = url => {
    if (url.pathname.endsWith("/friends")) return { friends };
    if (url.pathname.endsWith("/status")) return {};
    if (url.pathname.endsWith("/batches")) return { batches: [] };
    if (url.pathname.endsWith("/sources")) return { sources: [] };
    if (url.pathname.endsWith("/messages")) return { messages: [], gaps: [], sources: [] };
    throw new Error(`Unexpected synthetic API request: ${url.pathname}`);
  };
  vm.runInNewContext(source, { window, document, URLSearchParams, console,
    localStorage: { getItem: () => "{}" }, setTimeout: callback => { timers.set(++sequence, callback); return sequence; }, clearTimeout: id => timers.delete(id),
    fetch: async (address, options) => { const url = new URL(address, window.location.origin); calls.push(url); requests.push({ url, options });
      const body = responder ? await responder(url, options, defaultResponse) : defaultResponse(url);
      return { ok: true, status: 200, json: async () => body }; } });
  const message = (value, origin = window.location.origin) => { for (const handler of listeners.message || []) handler({ source: opener, origin, data: value }); };
  const scope = value => message({ type: "kyf-external:scope", ...value });
  const click = target => { assert.ok(target, "requested synthetic control exists"); assert.equal(target.hidden, false, "hidden controls cannot be clicked"); if (!target.disabled)
    for (const handler of target.listeners.click || []) handler({ target, currentTarget: target, preventDefault() {} }); };
  return { ids, calls, requests, scope, message, navigation, document, text: id => text(ids.get(id)), timers,
    pagehide: () => { for (const handler of listeners.pagehide || []) handler(); },
    unload: () => {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      for (const handler of listeners.beforeunload || []) handler(event);
      return event;
    },
    find: (id, predicate) => find(ids.get(id), predicate),
    click: id => click(ids.get(id)),
    change: (target, value) => { if (target.type === "file") target.files = value; else target.value = value;
      for (const handler of target.listeners.change || []) handler({ target, currentTarget: target }); },
    input: (target, value) => { target.value = value;
      for (const handler of target.listeners.input || []) handler({ target, currentTarget: target }); },
    tab: page => click(tabs.find(tab => tab.dataset.page === page)),
    button: (id, label) => click(find(ids.get(id), item => item.tagName === "BUTTON" && item.textContent === label)),
    tick: () => { const [id, callback] = timers.entries().next().value; timers.delete(id); return callback(); } };
}

test("partial save counts exclude unknown speakers, empty bodies, ignored and already-saved candidates", () => {
  const rows = [candidate("new"), candidate("speaker", { sender: "unknown" }), candidate("empty", { text: "  " }),
    candidate("ignored", { ignored: true }), candidate("saved", { savedMessageId: "m1" }), candidate("pending", { ready: false })];
  assert.deepEqual(plain(rules.countCandidates(rows)), { total: 6, ready: 1, pending: 3, saved: 1, ignored: 1 });
  assert.equal(rules.statusText({ state: "saved", candidates: rows }), "部分已保存，待核对");
});

test("saved re-recognition differences stay visible as pending while the original saved count is preserved", () => {
  const rows = [candidate("diff", { savedMessageId: "m1", ready: false, recognitionSuggestion: { text: "另一识别结果" } })];
  assert.deepEqual(plain(rules.countCandidates(rows)), { total: 1, ready: 0, pending: 1, saved: 1, ignored: 0 });
  assert.equal(rules.statusText({ state: "partial", candidates: rows }), "部分已保存，待核对");
});

test("clear system notices keep their unknown sender without becoming artificial review doubts", () => {
  const notice = candidate("system", { kind: "system", sender: "unknown", text: "合成系统通知" });
  assert.equal(rules.candidateReady(notice), true);
  assert.equal(rules.candidateReady({ ...notice, ready: false }), false);
  assert.equal(rules.candidateReady({ ...notice, kind: "text" }), false);
  assert.equal(rules.candidateReady({ ...notice, savedMessageId: "saved-notice" }), false);
  assert.deepEqual(plain(rules.countCandidates([notice])), { total: 1, ready: 1, pending: 0, saved: 0, ignored: 0 });
  assert.equal(rules.senderLabel(notice, "合成好友"), "系统通知");
  assert.equal(rules.senderLabel(candidate("peer"), "合成好友"), "合成好友");
  assert.equal(rules.senderLabel(candidate("unknown", { sender: "unknown" })), "发送者未知");
});

test("background-linked observations do not add phantom pending items or enter batch saving", () => {
  const original = candidate("first");
  const overlap = candidate("second", { autoMergedInto: "first", ready: false });
  assert.equal(rules.candidateReady(overlap), false);
  assert.deepEqual(plain(rules.countCandidates([original, overlap])), { total: 1, ready: 1, pending: 0, saved: 0, ignored: 0 });
});

test("completed rereads select the preferred evidence frame and its own bubble box", () => {
  const segments = [{ id: "partial-frame", videoMs: 0 }, { id: "complete-frame", videoMs: 1000 }];
  const oldBox = [0.1, 0.8, 0.6, 0.2], fullBox = [0.1, 0.3, 0.6, 0.5];
  const completed = candidate("completed", { segmentId: "partial-frame", evidenceSegmentId: "complete-frame",
    bbox: fullBox, original: { bbox: oldBox } });
  assert.deepEqual(plain(rules.candidateEvidence(completed, segments)), { segment: segments[1], bbox: fullBox });
  assert.deepEqual(plain(rules.candidateEvidence({ ...completed, evidenceSegmentId: "partial-frame", bbox: fullBox }, segments)),
    { segment: segments[0], bbox: fullBox });
  const legacy = { ...completed }; delete legacy.evidenceSegmentId;
  assert.deepEqual(plain(rules.candidateEvidence(legacy, segments)), { segment: segments[0], bbox: oldBox });
});

test("resolving the last issue does not falsely mark an unsaved candidate as a completed batch", () => {
  assert.equal(rules.statusText({ state: "partial", candidates: [candidate("old", { savedMessageId: "m1" }), candidate("new")] }), "部分已保存，待确认保存");
});

test("empty edits and explicit unknown sender survive background result refreshes", () => {
  const original = candidate("c1", { dateLabel: "昨天 13:02" });
  const edited = { text: "", sender: "unknown", dateLabel: "", duplicateDecision: "new" };
  assert.deepEqual(plain(rules.mergeDraft(original, edited)), edited);
  assert.equal(original.text, "合成消息");
  assert.equal(original.dateLabel, "昨天 13:02");
});

test("changing a visible date does not preserve an unrelated exact timestamp or invent a date", () => {
  const original = candidate("c1", { dateLabel: "2026-10-15 13:02", time: 1792040520000 });
  assert.deepEqual(plain(rules.reviewPatch(original, { text: "合成消息", sender: "other", dateLabel: "昨天" })),
    { text: "合成消息", sender: "other", dateLabel: "昨天", time: null, timePrecision: "partial" });
  assert.equal(rules.reviewPatch(original, { dateLabel: "" }).timePrecision, "unknown");
  assert.equal(Object.hasOwn(rules.reviewPatch(original, { dateLabel: original.dateLabel }), "time"), false);
});

test("composite source order retains unresolved gaps when records are saved out of order", () => {
  const messages = [{ id: "m3", sourceId: "s1", order: "000000000001:000000000000:000000000003" },
    { id: "m1", sourceId: "s1", order: "000000000001:000000000000:000000000001" },
    { id: "m10", sourceId: "s1", order: "000000000010:000000000000:000000000000" }];
  const gaps = [{ candidateId: "c2", sourceId: "s1", order: "000000000001:000000000000:000000000002" }];
  assert.deepEqual(plain(rules.orderWithGaps(messages, gaps)).map(row => row.value.id || row.value.candidateId), ["m1", "c2", "m3", "m10"]);
});

test("file selection accepts actual supported combinations and rejects oversized, empty or multiple recordings", () => {
  assert.equal(rules.validateFiles([{ name: "screenshot.png", size: 2048 }, { name: "recording.mp4", size: 8000 }]), "");
  assert.match(rules.validateFiles([{ name: "second.mov", size: 8000 }], [{ kind: "video" }]), /最多包含一段/);
  assert.match(rules.validateFiles([{ name: "empty.jpg", size: 0 }]), /为空或超过/);
  assert.match(rules.validateFiles([{ name: "large.png", size: 128 * 1048576 + 1 }]), /128 MB/);
  assert.match(rules.validateFiles([{ name: "page.svg", size: 1000 }]), /暂不支持/);
  assert.match(rules.validateFiles(Array.from({ length: 21 }, () => ({ name: "a.png", size: 1 }))), /最多添加 20/);
});

test("evidence references are always same-origin API paths even for hostile identifiers", () => {
  const url = rules.evidenceUrl('x&name=<script>"https://hostile.invalid');
  assert.ok(url.startsWith("/api/external-import/evidence?id="));
  assert.equal(new URL(url, "http://127.0.0.1").searchParams.size, 1);
  assert.equal(new URL(url, "http://127.0.0.1").origin, "http://127.0.0.1");
});

test("usage aggregates preserve unknown values and distinguish an empty ledger from unreported consumption", () => {
  assert.equal(rules.usageValue({ requests: 3, totalTokens: 0, knownTotalRequests: 0 }, "totalTokens", "knownTotalRequests"), null);
  assert.equal(rules.usageValue({ requests: 0, totalTokens: 0, knownTotalRequests: 0 }, "totalTokens", "knownTotalRequests"), 0);
  assert.equal(rules.usageValue({ requests: 3, totalTokens: 120, knownTotalRequests: 2 }, "totalTokens", "knownTotalRequests"), 120);
  assert.equal(rules.usageValue({ requests: 3, totalTokens: 0 }, "totalTokens", "knownTotalRequests"), null);
  assert.equal(rules.usageValue({}, "totalInputTokens", "knownInputRequests"), null);
});

test("evidence highlights accept only finite rectangles within the original picture", () => {
  assert.equal(rules.validBox([0.1, 0.2, 0.6, 0.3]), true);
  for (const invalid of [null, [], [0, 0, 0, 1], [0, 0, Infinity, 1], [0.8, 0, 0.5, 1], ["0", 0, 1, 1]])
    assert.equal(rules.validBox(invalid), false);
});

test("friend scope uses the exact account and user binding, never the first friend or a conflicting id", () => {
  const friends = [
    { id: "friend-a", name: "同名好友", wechatAccount: "account-a", wechatUser: "user-1" },
    { id: "friend-b", name: "同名好友", wechatAccount: "account-b", wechatUser: "user-1" },
    { id: "external-only", name: "外部好友" },
  ];
  assert.equal(rules.friendForScope(friends, { account: "account-b", user: "user-1" }), friends[1]);
  assert.equal(rules.friendForScope(friends, { account: "account-a", user: "user-1", friendId: "friend-b" }), friends[0]);
  assert.equal(rules.friendForScope(friends, { account: "missing", user: "user-1", friendId: "friend-a" }), null);
  assert.equal(rules.friendForScope(friends, { friendId: "external-only" }), friends[2]);
  for (const scope of [{}, { name: "同名好友" }, { friendId: "missing" }])
    assert.equal(rules.friendForScope(friends, scope), null);
});

test("invalid or group bindings cannot fall back to a previously selected friend id", () => {
  const friends = [{ id: "old-friend", wechatAccount: "account-a", wechatUser: "user-a" }];
  for (const scope of [
    { account: "account-a", user: "group@chatroom", friendId: "old-friend" },
    { account: "account-a", user: "", friendId: "old-friend" },
    { account: "", user: "user-a", friendId: "old-friend" },
    { account: "a".repeat(257), user: "user-a", friendId: "old-friend" },
    { account: "account-a", user: 123, friendId: "old-friend" },
  ]) assert.equal(rules.friendForScope(friends, scope), null);
});

test("task lists stay inside the selected friend and omit archived tasks", () => {
  const rows = [{ id: "a-running", friendId: "a", state: "running" },
    { id: "b-draft", friendId: "b", state: "draft" },
    { id: "a-archived", friendId: "a", state: "saved", archived: true },
    { id: "a-review", friendId: "a", state: "review", archived: false }];
  assert.deepEqual(plain(rules.batchesForFriend(rows, "a")).map(row => row.id), ["a-running", "a-review"]);
  assert.deepEqual(plain(rules.batchesForFriend(rows, "b")).map(row => row.id), ["b-draft"]);
  for (const missing of ["", undefined, null, "unknown-friend"])
    assert.deepEqual(plain(rules.batchesForFriend(rows, missing)), []);
  assert.equal(rows.length, 4, "scope filtering must not remove another friend's stored task");
});

test("adjacent unsaved gaps collapse into one marker while retaining the task that can resolve them", () => {
  const rows = [1, 2, 3].map(index => ({ kind: "gap", value: {
    candidateId: `candidate-${index}`, sourceId: "source-a", batchId: "batch-a", order: String(index),
  } }));
  const before = plain(rows), grouped = plain(rules.coalesceGaps(rows));
  assert.equal(grouped.length, 1);
  assert.equal(grouped[0].count, 3);
  assert.equal(grouped[0].value.batchId, "batch-a");
  assert.deepEqual(plain(rows), before, "display grouping must not mutate stored gap identities");
});

test("gap grouping stops at saved messages, different sources, different tasks, and archive boundaries", () => {
  const gap = (id, changes = {}) => ({ kind: "gap", value: {
    candidateId: id, sourceId: "source-a", batchId: "batch-a", ...changes,
  } });
  const message = { kind: "message", value: { id: "saved", sourceId: "source-a", batchId: "batch-a" } };
  for (const rows of [
    [gap("before"), message, gap("after")],
    [gap("before"), gap("other-source", { sourceId: "source-b" })],
    [gap("before"), gap("other-task", { batchId: "batch-b" })],
    [gap("before"), gap("archived", { archived: true })],
  ]) {
    const grouped = plain(rules.coalesceGaps(rows));
    assert.equal(grouped.length, rows.length);
    assert.ok(grouped.every(row => row.count === 1));
  }
});

test("the friend-owned page has two main views and no duplicate friend label, back button or selector sidebar", () => {
  const html = readFileSync(path.join(__dirname, "../chatui/external-import.html"), "utf8");
  assert.deepEqual([...html.matchAll(/data-page="([^"]+)"/g)].map(match => match[1]), ["saved", "tasks"]);
  assert.doesNotMatch(html, /id="(?:eiFriends|eiNewFriend|eiTasks)"|class="ei-sidebar"/);
  // The window title already names the friend, and closing the window returns to chat.
  assert.doesNotMatch(html, /id="(?:eiFriendName|eiBack)"/);
});

test("a standalone window ignores later scope messages from the main window", async () => {
  const page = pageFixture(); await settle();
  for (const next of [{ account: "account-b", user: "user-b", name: "合成乙" },
    { account: "account-a", user: "group@chatroom", friendId: "friend-a" }, { account: "account-a", user: "" }]) {
    page.scope(next); await settle();
    assert.equal(page.document.title, "合成甲 · 外部聊天 · 知己微信助手");
    assert.ok(page.calls.filter(url => url.pathname.endsWith("/batches")).every(url => url.searchParams.get("friendId") === "friend-a"));
  }
});

test("an in-flight read remains bound to its window when the main window selects another friend", async () => {
  const old = deferred();
  const message = (id, text) => ({ id, text, sourceId: `source-${id}`, sender: "other", kind: "text", order: "1" });
  const page = pageFixture({ responder: (url, options, defaults) => {
    if (url.pathname.endsWith("/messages")) return url.searchParams.get("friendId") === "friend-a" ? old.promise :
      { messages: [message("b", "乙的合成消息")], gaps: [], sources: [] };
    return defaults(url);
  } });
  await settle();
  page.scope({ account: "account-b", user: "user-b", name: "合成乙" }); await settle();
  assert.doesNotMatch(page.text("eiContent"), /甲的旧消息/);
  old.resolve({ messages: [message("a", "甲的旧消息")], gaps: [], sources: [] }); await settle();
  assert.equal(page.document.title, "合成甲 · 外部聊天 · 知己微信助手");
  assert.match(page.text("eiContent"), /甲的旧消息/);
  assert.doesNotMatch(page.text("eiContent"), /乙的合成消息/);
  assert.equal(page.calls.filter(url => url.searchParams.get("friendId") === "friend-b").length, 0);
});

test("native context is authoritative and no query friend is read while it is pending", async () => {
  const context = deferred();
  const page = pageFixture({ nativeHost: { getContext: () => context.promise } }); await settle();
  assert.equal(page.calls.length, 0);
  context.resolve({ account: "account-b", user: "user-b", name: "合成乙", theme: "light" }); await settle();
  assert.equal(page.document.title, "合成乙 · 外部聊天 · 知己微信助手");
  assert.equal(page.document.title, "合成乙 · 外部聊天 · 知己微信助手");
  assert.ok(page.document.body.className.includes("theme-light"));
  assert.ok(page.calls.filter(url => ["batches", "sources", "messages"].includes(url.pathname.split("/").pop()))
    .every(url => url.searchParams.get("friendId") === "friend-b"));
});

test("popup navigation focuses its opener and closing the popup leaves the main window alone", async () => {
  const page = pageFixture(); await settle();
  page.click("eiSettings");
  assert.deepEqual(plain(page.navigation.slice(-2)), [{ type: "kyf-external:navigate", target: "model-settings" }, "focus"]);
  page.pagehide();
  assert.equal(page.navigation.some(value => value?.target === "chat"), false, "closing the window leaves the main conversation as it was");
  page.message({ type: "kyf-external:theme", theme: "light" }, "https://untrusted.invalid");
  assert.equal(page.document.body.className.includes("theme-light"), false);
  page.message({ type: "kyf-external:theme", theme: "light" });
  assert.ok(page.document.body.className.includes("theme-light"));
});

test("native navigation is delegated to the host instead of opening an embedded view", async () => {
  const navigation = [];
  const page = pageFixture({ nativeHost: { getContext: async () => ({ account: "account-a", user: "user-a" }),
    navigate: async target => navigation.push(target) } }); await settle();
  page.click("eiSettings"); await settle();
  assert.deepEqual(navigation, ["model-settings"]);
  assert.equal(page.navigation.includes("close"), false, "the native host owns window closure");
});

test("recognition shows the primary model's actual name separately from its connection address", async () => {
  const batch = { id: "task-a", friendId: "friend-a", state: "failed", candidates: [], materials: [], segments: [], source: { platform: "合成来源" } };
  const page = pageFixture({ search: "?account=account-a&user=user-a&batchId=task-a", responder: (url, options, defaults) => {
    if (url.pathname.endsWith("/status")) return { model: { available: true, supportsVision: true,
      model: "synthetic-primary-model", destination: "http://127.0.0.1:12345" } };
    if (url.pathname.endsWith("/batch")) return batch;
    if (url.pathname.endsWith("/batches")) return { batches: [batch] };
    return defaults(url);
  } });
  await settle();
  assert.match(page.text("eiContent"), /处理失败，可重试/);
  assert.doesNotMatch(page.text("eiContent"), /识别模型/, "model details stay out of the task page");
  page.button("eiContent", "更多");
  assert.match(page.text("eiDialog"), /识别模型：synthetic-primary-model/);
  assert.match(page.text("eiDialog"), /使用“模型设置”中配置的主模型/);
  assert.match(page.text("eiDialog"), /连接地址：http:\/\/127\.0\.0\.1:12345/);
});

test("a task opens on one page: progress and its failure sit above the review that fills in", async () => {
  const segments = [{ id: "frame-1", materialId: "video", state: "done", evidenceAvailable: true, videoMs: 0 },
    { id: "frame-2", materialId: "video", state: "pending", evidenceAvailable: true, videoMs: 750 }];
  let batch = { id: "task-a", friendId: "friend-a", state: "running", materials: [{ id: "video", name: "合成录屏.mov", kind: "video", size: 4096, available: true }],
    segments, source: { platform: "合成来源" }, candidates: [] };
  const page = pageFixture({ search: "?account=account-a&user=user-a&batchId=task-a", responder: (url, options, defaults) => {
    if (url.pathname.endsWith("/batch") || url.pathname.endsWith("/start")) return batch;
    if (url.pathname.endsWith("/batches")) return { batches: [batch] };
    return defaults(url);
  } });
  await settle();
  assert.match(page.text("eiContent"), /后台整理中 1\/2/);
  assert.match(page.text("eiContent"), /识别出的消息会陆续显示在这里/);
  assert.equal(page.document.body.className.includes("ei-reviewing"), true, "the review workspace is shown from the start");
  assert.equal(page.find("eiContent", node => node.type === "file"), undefined, "started materials can no longer change");
  batch = { ...batch, state: "failed", error: "无法读取录屏 0.8 秒处的画面；已完成部分已保留",
    candidates: [{ id: "c1", segmentId: "frame-1", text: "合成消息", sender: "other", kind: "text", ready: true, issues: [] }] };
  await page.tick(); await settle();
  assert.match(page.text("eiContent"), /无法读取录屏 0\.8 秒处的画面/);
  assert.match(page.text("eiContent"), /合成消息/);
  assert.ok(page.find("eiContent", node => node.tagName === "BUTTON" && node.textContent === "继续未完成的部分"));
  batch = { ...batch, state: "review", error: "", segments: segments.map(item => ({ ...item, state: "done" })) };
  page.button("eiContent", "继续未完成的部分"); await settle();
  assert.doesNotMatch(page.text("eiContent"), /继续未完成的部分|后台整理中/, "nothing is left above a finished review");
});

test("native host updates retain the window scope and disabled recognition leaves records accessible", async () => {
  let event, enabled = true, statusReads = 0;
  const page = pageFixture({ nativeHost: { getContext: async () => ({ account: "account-a", user: "user-a" }),
    onHostEvent: listener => { event = listener; return () => {}; } }, responder: (url, options, defaults) => {
      if (url.pathname.endsWith("/status")) { statusReads++; return { plugin: { enabled } }; }
      return defaults(url);
    } }); await settle();
  enabled = false;
  event({ type: "plugins-changed", plugins: [{ id: "kyf.external-chat-import", enabled: false }] });
  page.button("eiContent", "导入聊天");
  assert.equal(page.ids.get("eiDialog").open, false);
  assert.match(page.text("eiError"), /插件已停用/);
  assert.match(page.text("eiContent"), /还没有外部聊天/);
  enabled = true;
  event({ type: "service-restored" }); await settle();
  event({ type: "focus", account: "account-b", user: "user-b", name: "合成乙" }); await settle();
  assert.equal(statusReads, 3);
  assert.equal(page.document.title, "合成甲 · 外部聊天 · 知己微信助手");
  assert.ok(page.calls.filter(url => ["batches", "sources", "messages"].includes(url.pathname.split("/").pop()))
    .every(url => url.searchParams.get("friendId") === "friend-a"));
  page.button("eiContent", "导入聊天");
  assert.equal(page.ids.get("eiDialog").open, true);
});

test("dependency errors block new imports in an already open window and recovery retains its friend", async () => {
  let event, statusReads = 0;
  const page = pageFixture({ nativeHost: { getContext: async () => ({ account: "account-a", user: "user-a" }),
    onHostEvent: listener => { event = listener; return () => {}; } }, responder: (url, options, defaults) => {
      if (url.pathname.endsWith("/status")) { statusReads++; return { plugin: { enabled: true } }; }
      return defaults(url);
    } }); await settle();
  event({ type: "plugins-changed", plugins: [{ id: "kyf.external-chat-import", enabled: true,
    dependencyError: { code: "plugin-dependency-missing", message: "缺少依赖插件 dev.provider" } }] });
  await settle(); assert.equal(statusReads, 1, "blocked notifications do not immediately overwrite the effective disabled state");
  page.button("eiContent", "导入聊天"); assert.equal(page.ids.get("eiDialog").open, false);
  assert.match(page.text("eiContent"), /还没有外部聊天/); assert.equal(page.document.title, "合成甲 · 外部聊天 · 知己微信助手");
  event({ type: "plugins-changed", plugins: [{ id: "kyf.external-chat-import", enabled: true }] }); await settle();
  assert.equal(statusReads, 2); page.button("eiContent", "导入聊天");
  assert.equal(page.ids.get("eiDialog").open, true); assert.equal(page.document.title, "合成甲 · 外部聊天 · 知己微信助手");
});

test("a direct batch link from another friend cannot replace the selected scope", async () => {
  const page = pageFixture({ search: "?account=account-a&user=user-a&batchId=task-b", responder: (url, options, defaults) =>
    url.pathname.endsWith("/batch") ? { id: "task-b", friendId: "friend-b", candidates: [] } : defaults(url) });
  await settle();
  assert.equal(page.document.title, "合成甲 · 外部聊天 · 知己微信助手");
  assert.match(page.text("eiError"), /不属于当前好友/);
  assert.equal(page.calls.filter(url => url.pathname.endsWith("/messages") && url.searchParams.get("friendId") === "friend-b").length, 0);
});

test("deleting a running task stops then archives it, and a late poll cannot restore the removed row", async () => {
  const lateList = deferred(); let listReads = 0, archived = false, state = "running";
  const task = () => ({ id: "task-a", friendId: "friend-a", state, note: "待归档合成任务", candidates: [],
    materials: [], segments: [], source: { platform: "合成来源" } });
  const page = pageFixture({ responder: (url, options, defaults) => {
    if (url.pathname.endsWith("/batches")) {
      if (++listReads === 2) return lateList.promise;
      return { batches: archived ? [] : [task()] };
    }
    if (url.pathname.endsWith("/batch")) return task();
    if (url.pathname.endsWith("/stop")) { state = "stopped"; return { batch: task() }; }
    if (url.pathname.endsWith("/cleanup")) { archived = true; return { deleted: true }; }
    return defaults(url);
  } });
  await settle(); page.tab("tasks"); await settle();
  assert.match(page.text("eiContent"), /待归档合成任务/);
  const polling = page.tick(); await settle();
  page.button("eiContent", "删除"); page.button("eiDialog", "停止并删除"); await settle();
  assert.equal(archived, true);
  const mutations = page.requests.filter(request => request.options?.method === "POST");
  assert.deepEqual(mutations.map(request => request.url.pathname.split("/").pop()), ["stop", "cleanup"]);
  assert.deepEqual(JSON.parse(mutations[1].options.body), { batchId: "task-a", kind: "task" },
    "the visible delete action archives the task without deleting records or media");
  lateList.resolve({ batches: [task()] }); await polling; await settle();
  assert.doesNotMatch(page.text("eiContent"), /待归档合成任务/);
  assert.match(page.text("eiContent"), /还没有导入任务/);
});

function importFlowFixture({ unbound = false, sources = [], beforeUpload, beforeStart } = {}) {
  const createdFriend = { id: "created-friend", name: "合成新好友", wechatAccount: "new-account", wechatUser: "new-user" };
  let batch;
  return pageFixture({ search: unbound ? "?account=new-account&user=new-user&name=合成新好友" : "?account=account-a&user=user-a",
    responder: async (url, options, defaults) => {
      const post = options?.method === "POST", operation = url.pathname.split("/").pop();
      if (operation === "friends") return post ? { friend: createdFriend } : unbound ? { friends: [] } : defaults(url);
      if (operation === "sources") return { sources };
      if (operation === "batches" && post) {
        const values = JSON.parse(options.body);
        batch = { id: "created-task", friendId: values.friendId, state: "draft", candidates: [], segments: [], materials: [],
          source: { id: "created-source", platform: values.platform, ownAccount: values.ownAccount, conversationKey: values.conversationKey } };
        return plain(batch);
      }
      if (operation === "upload") {
        await beforeUpload?.();
        batch.materials.push({ id: "synthetic-material", name: url.searchParams.get("name"), kind: "image", size: options.body.size });
        return plain(batch);
      }
      if (operation === "start") { await beforeStart?.(); batch.state = "queued"; return plain(batch); }
      return defaults(url);
    } });
}

test("first import needs only source and a file, creates the exact friend binding, then uploads and starts", async () => {
  const page = importFlowFixture({ unbound: true }); await settle(); page.button("eiContent", "导入聊天");
  assert.equal(page.ids.get("eiDialog").open, true);
  assert.doesNotMatch(page.text("eiDialog"), /归属好友|选择好友/);
  const sourceField = page.find("eiDialog", node => node.attributes["aria-label"] === "来源");
  assert.equal(sourceField.tagName, "INPUT");
  assert.equal(sourceField.value, "", "no external software is preselected");
  assert.equal(sourceField.required, true);
  assert.equal(sourceField.maxLength, 80);
  assert.equal(page.find("eiDialog", node => node.required === true && node !== sourceField), undefined,
    "manual account and conversation identifiers must not block the default import flow");
  const options = page.find("eiDialog", node => node.tagName === "DETAILS");
  assert.equal(options.open, false);
  const file = { name: "synthetic-chat.png", size: 4096, type: "image/png" };
  page.change(page.find("eiDialog", node => node.type === "file"), [file]);
  assert.equal(page.find("eiDialog", node => node.tagName === "BUTTON" && node.textContent === "开始导入").disabled, true);
  page.input(sourceField, "   ");
  assert.equal(page.find("eiDialog", node => node.tagName === "BUTTON" && node.textContent === "开始导入").disabled, true);
  page.input(sourceField, "  自定来源 / Synthetic 2026  ");
  page.button("eiDialog", "开始导入"); await settle();
  assert.equal(page.text("eiError"), "");
  const writes = page.requests.filter(request => request.options?.method === "POST");
  assert.deepEqual(writes.map(request => request.url.pathname.split("/").pop()), ["friends", "batches", "upload", "start"]);
  assert.deepEqual(JSON.parse(writes[0].options.body), { name: "合成新好友", wechatAccount: "new-account", wechatUser: "new-user" });
  const values = JSON.parse(writes[1].options.body);
  assert.equal(values.friendId, "created-friend");
  assert.equal(values.platform, "自定来源 / Synthetic 2026");
  assert.equal(values.ownAccount, "本人");
  assert.equal(values.conversationKey, "created-friend");
  assert.equal(writes[2].options.body, file, "upload sends the selected file as binary rather than JSON");
  assert.equal(writes[2].options.headers["Content-Type"], "application/octet-stream");
  assert.equal(writes[2].url.searchParams.get("batchId"), "created-task");
  assert.deepEqual(JSON.parse(writes[3].options.body), { batchId: "created-task" });
});

test("multiple same-platform account sources expose a visible choice and honor the selected source", async () => {
  const sources = [{ id: "first", platform: "用户自定来源", ownAccount: "合成账号一", conversationKey: "conversation-one" },
    { id: "second", platform: "用户自定来源", ownAccount: "合成账号二", conversationKey: "conversation-two" }];
  const page = importFlowFixture({ sources }); await settle(); page.button("eiContent", "导入聊天");
  const field = page.find("eiDialog", node => node.attributes["aria-label"] === "来源");
  const suggestions = page.find("eiDialog", node => node.tagName === "DATALIST");
  assert.deepEqual(suggestions.children.map(node => node.value), ["用户自定来源"]);
  page.input(field, "用户自定来源");
  const select = page.find("eiDialog", node => node.tagName === "SELECT" && node.children.some(child => child.value === "second"));
  assert.ok(select);
  assert.equal(select.parentNode.hidden, false);
  for (let ancestor = select.parentNode; ancestor; ancestor = ancestor.parentNode)
    assert.notEqual(ancestor.tagName, "DETAILS", "ambiguous sources must not be hidden inside closed advanced options");
  page.change(select, "second");
  page.change(page.find("eiDialog", node => node.type === "file"), [{ name: "another-synthetic.png", size: 2048 }]);
  page.button("eiDialog", "开始导入"); await settle();
  assert.equal(page.text("eiError"), "");
  const writes = page.requests.filter(request => request.options?.method === "POST");
  assert.deepEqual(writes.map(request => request.url.pathname.split("/").pop()), ["batches", "upload", "start"]);
  const values = JSON.parse(writes[0].options.body);
  assert.equal(values.friendId, "friend-a");
  assert.equal(values.platform, "用户自定来源");
  assert.equal(values.ownAccount, "合成账号二");
  assert.equal(values.conversationKey, "conversation-two");
});

test("closing an import window guards unfinished uploads and start submission, then permits background recognition", async () => {
  const uploading = deferred(), starting = deferred(); let uploads = 0;
  const page = importFlowFixture({ beforeUpload: () => ++uploads === 1 ? uploading.promise : undefined,
    beforeStart: () => starting.promise }); await settle();
  page.button("eiContent", "导入聊天");
  page.input(page.find("eiDialog", node => node.attributes["aria-label"] === "来源"), "合成来源");
  page.change(page.find("eiDialog", node => node.type === "file"),
    [{ name: "first.png", size: 2048 }, { name: "second.png", size: 2048 }]);
  assert.equal(page.unload().prevented, false, "choosing files alone does not block closing");
  page.button("eiDialog", "开始导入"); await settle();
  assert.equal(page.unload().prevented, true, "the renderer still holds materials that have not reached the backend");
  assert.equal(uploads, 1);
  uploading.resolve(); await settle();
  assert.equal(uploads, 2);
  assert.equal(page.unload().prevented, true, "all materials must also finish submitting the background task");
  starting.resolve(); await settle();
  assert.equal(page.text("eiError"), "");
  assert.match(page.text("eiContent"), /排队中/);
  assert.equal(page.unload().prevented, false, "a durably submitted background task continues after the window closes");
});

test("an upload failure releases the submission guard while retaining the created task", async () => {
  const page = importFlowFixture({ beforeUpload: () => { throw new Error("synthetic upload interruption"); } }); await settle();
  page.button("eiContent", "导入聊天");
  page.input(page.find("eiDialog", node => node.attributes["aria-label"] === "来源"), "合成来源");
  page.change(page.find("eiDialog", node => node.type === "file"), [{ name: "interrupted.png", size: 2048 }]);
  page.button("eiDialog", "开始导入"); await settle();
  assert.match(page.text("eiError"), /暂时无法连接/);
  assert.equal(page.unload().prevented, false);
  assert.equal(page.requests.filter(request => request.url.pathname.endsWith("/batches") && request.options?.method === "POST").length, 1);
  assert.equal(page.requests.filter(request => request.url.pathname.endsWith("/start")).length, 0);
});

test("appending materials to a draft and submitting its start both guard window closure", async () => {
  const uploading = deferred(), starting = deferred(); let appended = false, state = "draft";
  const task = () => ({ id: "draft-task", friendId: "friend-a", state, note: "合成草稿", candidates: [], segments: [],
    materials: [{ id: "first", name: "first.png", kind: "image", size: 2048 },
      ...(appended ? [{ id: "second", name: "second.png", kind: "image", size: 2048 }] : [])], source: { platform: "合成来源" } });
  const page = pageFixture({ responder: async (url, options, defaults) => {
    if (url.pathname.endsWith("/batches")) return { batches: [task()] };
    if (url.pathname.endsWith("/batch")) return task();
    if (url.pathname.endsWith("/upload")) { await uploading.promise; appended = true; return task(); }
    if (url.pathname.endsWith("/start")) { await starting.promise; state = "queued"; return task(); }
    return defaults(url);
  } }); await settle(); page.tab("tasks"); await settle(); page.button("eiContent", "查看"); await settle();
  page.change(page.find("eiContent", node => node.type === "file"), [{ name: "second.png", size: 2048 }]); await settle();
  assert.equal(page.unload().prevented, true);
  uploading.resolve(); await settle();
  assert.equal(page.unload().prevented, false);
  page.button("eiContent", "开始后台整理"); await settle();
  assert.equal(page.unload().prevented, true);
  starting.resolve(); await settle();
  assert.equal(page.unload().prevented, false);
  assert.equal(page.text("eiError"), "");
});

test("saved chat source labels and filtering preserve arbitrary user-entered names", async () => {
  const names = ["用户自己的来源", "Custom / β <name>"];
  const sources = names.map((platform, index) => ({ id: `source-${index}`, platform }));
  const page = pageFixture({ responder: (url, options, defaults) => {
    if (url.pathname.endsWith("/sources")) return { sources };
    if (url.pathname.endsWith("/messages")) return { sources, gaps: [], messages: sources
      .filter(item => !url.searchParams.get("platform") || item.platform === url.searchParams.get("platform"))
      .map(item => ({ id: `message-${item.id}`, sourceId: item.id, text: `${item.platform} 的合成消息`, sender: "other", kind: "text", order: "1" })) };
    return defaults(url);
  } }); await settle();
  for (const name of names) assert.ok(page.text("eiContent").includes(name));
  page.button("eiContent", names[1]); await settle();
  assert.equal(page.calls.filter(url => url.pathname.endsWith("/messages")).at(-1).searchParams.get("platform"), names[1]);
  assert.ok(page.text("eiContent").includes(`${names[1]} 的合成消息`));
  assert.ok(!page.text("eiContent").includes(`${names[0]} 的合成消息`));
});

test("polling preserves a single empty-state import action and keeps the populated task header action", async () => {
  for (const view of ["saved", "tasks"]) {
    const page = pageFixture(); await settle();
    if (view === "tasks") { page.tab(view); await settle(); }
    assert.equal(page.ids.get("eiAdd").hidden, true);
    await page.tick(); await settle();
    assert.equal(page.ids.get("eiAdd").hidden, true, "background refresh must not duplicate the empty-state action");
    page.button("eiContent", "导入聊天");
    assert.equal(page.ids.get("eiDialog").open, true, "the visible empty-state action remains usable after polling");
  }
  const page = pageFixture({ responder: (url, options, defaults) => url.pathname.endsWith("/batches") ? {
    batches: [{ id: "task-a", friendId: "friend-a", state: "draft", source: { platform: "合成来源" } }],
  } : defaults(url) });
  await settle(); page.tab("tasks"); await settle();
  assert.equal(page.ids.get("eiAdd").hidden, false);
  await page.tick(); await settle();
  assert.equal(page.ids.get("eiAdd").hidden, false);
  page.click("eiAdd");
  assert.equal(page.ids.get("eiDialog").open, true);
});

test("doubts are labelled on their own message, and ready, ignored or settled messages carry no label", () => {
  assert.deepEqual(plain(rules.reviewIssues(candidate("ready", { issues: ["missing_time"] }))), []);
  assert.deepEqual(plain(rules.reviewIssues(candidate("ignored", { ready: false, ignored: true, issues: ["truncated"] }))), []);
  assert.deepEqual(plain(rules.reviewIssues(candidate("saved", { ready: false, savedMessageId: "m1", issues: ["truncated"] }))), []);
  assert.deepEqual(plain(rules.reviewIssues(candidate("cut", { ready: false, sender: "unknown", truncated: true, issues: ["truncated", "时间未知"] }))),
    ["气泡被截断", "发送者不明确"]);
  assert.deepEqual(plain(rules.reviewIssues(candidate("model-note", { ready: false, issues: ["顶部气泡被裁切"] }))), ["顶部气泡被裁切"]);
  assert.deepEqual(plain(rules.reviewIssues(candidate("bare", { ready: false }))), ["待核对"]);
  assert.deepEqual(plain(rules.reviewIssues(candidate("reread", { ready: false, savedMessageId: "m2", recognitionSuggestion: { text: "新" } }))), ["重新识别存在差异"]);
});

test("frame notes stay with their frame while only whole-batch problems are raised above the review", () => {
  const batch = { candidates: [candidate("c1")], warnings: ["顶部状态栏不是消息", "multiple_conversations", "no_visible_messages"] };
  assert.deepEqual(plain(rules.batchWarnings(batch)), ["multiple_conversations"]);
  assert.deepEqual(plain(rules.batchWarnings({ ...batch, candidates: [] })), ["multiple_conversations", "no_visible_messages"]);
  assert.deepEqual(plain(rules.frameWarnings({ warnings: ["顶部状态栏不是消息", "vision_unconfirmed", "review_unresolved"] })), ["顶部状态栏不是消息", "复核未能确认"]);
});

test("every observed frame can be compared, but only recorded boxes are highlighted", () => {
  const segments = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const box = [0.1, 0.2, 0.5, 0.1], originalBox = [0.1, 0.7, 0.5, 0.1];
  const frames = rules.evidenceFrames(candidate("c", { segmentId: "a", evidenceSegmentId: "b", bbox: box, original: { bbox: originalBox },
    evidenceSegmentIds: ["a", "b", "c", "missing"] }), segments);
  assert.deepEqual(plain(frames.map(item => [item.segment.id, item.bbox])), [["b", box], ["a", originalBox], ["c", null]]);
});

test("returning an edit to the recognized values leaves no unconfirmed draft", () => {
  const original = candidate("c1", { dateLabel: "昨天" });
  assert.equal(rules.draftChanged(original, rules.mergeDraft(original, null)), false);
  assert.equal(rules.draftChanged(original, { ...rules.mergeDraft(original, null), text: "改过" }), true);
  assert.equal(rules.draftChanged(original, { ...rules.mergeDraft(original, null), duplicateDecision: "new" }), true);
});

test("deleted bubbles leave the review at once and the doubt view keeps only open ones", () => {
  const rows = [candidate("ready"), candidate("doubt", { ready: false, issues: ["truncated"] }), candidate("gone", { ignored: true }),
    candidate("linked", { autoMergedInto: "ready" })];
  assert.deepEqual(plain(rules.reviewVisible(rows, "all")).map(row => row.id), ["ready", "doubt"]);
  assert.deepEqual(plain(rules.reviewVisible(rows, "issues")).map(row => row.id), ["doubt"]);
});

test("confirming a possible repeat keeps it as its own message, and a re-read of a saved one reuses it", () => {
  const values = row => rules.mergeDraft(row, null);
  const repeat = candidate("r", { ready: false, duplicateCandidateId: "other", issues: ["possible_duplicate"] });
  assert.equal(rules.confirmPatch(repeat, values(repeat)).duplicateDecision, "new");
  assert.equal(rules.confirmPatch(repeat, values(repeat)).ignored, false);
  const reread = candidate("s", { ready: false, duplicateMessageId: "m1", savedVersion: { text: "旧" } });
  assert.equal(rules.confirmPatch(reread, values(reread)).duplicateDecision, "reuse");
  const chosen = candidate("c", { ready: false, duplicateCandidateId: "other", duplicateDecision: "reuse" });
  assert.equal(rules.confirmPatch(chosen, values(chosen)).duplicateDecision, "reuse");
  assert.equal(Object.hasOwn(plain(rules.confirmPatch(candidate("plain"), values(candidate("plain")))), "duplicateDecision"), false);
});

test("pictures are requested from the same-origin import API for candidates and saved records", () => {
  assert.equal(rules.pictureUrl("candidate", "c/1?x"), "/api/external-import/picture?candidate=c%2F1%3Fx");
  assert.equal(rules.pictureUrl("message", "m1"), "/api/external-import/picture?message=m1");
});
