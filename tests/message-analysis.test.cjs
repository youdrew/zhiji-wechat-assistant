// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../chatui/message-analysis.js"), "utf8");
const flush = () => new Promise(setImmediate);
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function harness() {
  const timers = new Map(), calls = [], documentListeners = new Map(), windowListeners = new Map();
  let timerId = 0, requestImpl, pluginEnabled = true, pluginProblem = {};
  class Node {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase(); this.className = ""; this.children = []; this.parentNode = null;
      this.listeners = new Map(); this.attributes = {}; this.hidden = false; this.disabled = false; this.textContent = "";
      this.scrollTop = 150; this.style = {}; this.rect = { top: 0, bottom: 100 };
      this.classList = { contains: value => this.className.split(" ").includes(value),
        toggle: (value, on) => { this.className = this.className.split(" ").filter(item => item !== value).concat(on ? [value] : []).join(" "); } };
    }
    get firstElementChild() { return this.children[0] || null; }
    get isConnected() { return this === body || Boolean(this.parentNode?.isConnected); }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set textContent(value) { for (const child of [...this.children]) child.remove(); this._text = String(value); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, handler) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(handler); }
    emit(name) { return Promise.all((this.listeners.get(name) || []).map(handler => handler({ target: this }))); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
    appendChild(child) { child.remove(); child.parentNode = this; this.children.push(child); return child; }
    append(...children) { for (const child of children) this.appendChild(child); }
    insertBefore(child, before) { child.remove(); child.parentNode = this; this.children.splice(this.children.indexOf(before), 0, child); }
    replaceChildren(...children) { this.textContent = ""; this.append(...children); }
    getBoundingClientRect() { return this.rect; }
    contains(value) { return value === this || this.children.some(child => child.contains(value)); }
    querySelectorAll(selector) {
      const children = this.children.flatMap(child => [child, ...child.querySelectorAll("*")]);
      return children.filter(child => selector === "*" || (selector.startsWith(".") ? child.classList.contains(selector.slice(1)) : child.tagName === selector.toUpperCase()));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  const body = new Node("body"), container = new Node(), toggle = new Node("button"), status = new Node("button");
  body.append(container, toggle, status);
  const elements = { chatMessages: container, btnMessageAnalysis: toggle, messageAnalysisStatus: status,
    messageAnalysisMigration: new Node(), messageAnalysisMigrationText: new Node(), btnDisableLegacyAnalysis: new Node("button") };
  const document = { hidden: false, getElementById: id => elements[id], createElement: tag => new Node(tag),
    createElementNS: (namespace, tag) => Object.assign(new Node(tag), { namespaceURI: namespace }),
    addEventListener: (name, callback) => documentListeners.set(name, callback) };
  const window = { addEventListener: (name, callback) => windowListeners.set(name, callback) };
  const context = vm.createContext({ window, document, URLSearchParams, AbortController,
    setTimeout: handler => { timers.set(++timerId, handler); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      // The annotation slot discovers its provider plugin, then calls that plugin through Core RPC.
      if (url === "/api/plugins") return { ok: true, json: async () => ({ plugins: [{ id: "kyf.message-insights", name: "分析与建议",
        enabled: pluginEnabled, ...pluginProblem, contributes: { messageAnnotations: true, chatHeader: [{ id: "insights", label: "分析与建议", kind: "toggle" }] } }] }) };
      const rpc = JSON.parse(options.body);
      assert.equal(rpc.plugin, "kyf.message-insights");
      const route = { "annotations.status": "", "chatHeader.toggle": "", "annotations.generate": "/generate", "annotations.retry": "/retry", "annotations.copy": "/copy" }[rpc.method];
      assert.notEqual(route, undefined, rpc.method);
      const status = rpc.method === "annotations.status";
      const query = new URLSearchParams({ account: rpc.account, user: rpc.user, messageIds: JSON.stringify(rpc.params.messageIds || []) });
      const call = { url: `/api/advisor/message-analysis${route}${status ? `?${query}` : ""}`, options: { ...options, method: status ? "GET" : "POST" },
        body: status ? null : { account: rpc.account, user: rpc.user, ...rpc.params } };
      calls.push(call);
      const reply = requestImpl ? await requestImpl(call) : { ok: true, json: async () => ({ account: rpc.account, user: rpc.user,
        enabled: call.body?.enabled ?? true, items: [] }) };
      return { ok: reply.ok, status: reply.status, json: async () => { const value = await reply.json(); return reply.ok ? { result: value } : value; } };
    },
  });
  vm.runInContext(source, context);
  let activeMessages = [];
  function render(messages, replace = false) {
    if (replace) { container.replaceChildren(); activeMessages = []; }
    const result = [];
    for (const message of messages) {
      const item = new Node(); item.className = "msg-item";
      const wrap = new Node(); wrap.className = "msg-content-wrap";
      const bubble = new Node(); bubble.className = "msg-bubble"; bubble.textContent = message.text;
      wrap.append(bubble); item.append(wrap); container.append(item);
      window.MessageAnalysis.attach(message, item); result.push(item); activeMessages.push(message);
    }
    window.MessageAnalysis.sync(activeMessages);
    return result;
  }
  return { window, document, container, toggle, status, elements, calls, timers, ui: window.MessageAnalysis, render,
    selection: node => { window.getSelection = () => ({ isCollapsed: !node, anchorNode: node, focusNode: node }); documentListeners.get("selectionchange")?.(); },
    setRequest: fn => { requestImpl = fn; }, setPluginEnabled: value => { pluginEnabled = value; }, setPluginProblem: value => { pluginProblem = value; }, async tick() { const pending = [...timers.values()]; timers.clear(); for (const fn of pending) fn(); await flush(); },
    async select(account = "account-a", user = "friend-a", group = false) { window.MessageAnalysis.setScope(account, user, group); await flush(); },
    async visibility(hidden) { document.hidden = hidden; documentListeners.get("visibilitychange")(); await flush(); },
    pagehide: () => windowListeners.get("pagehide")() };
}
const message = (id, text = "虚构对话", side = "other") => ({ id, text, side, kind: "text" });
const result = (messageId, state = "ready") => ({ messageId, version: `${messageId}-v1`, state,
  analysisState: state, suggestionsState: state, applicability: "current", ...(state === "ready" ? {
  replies: ["简短回应", "自然接话", "轻松一点"].map((label, index) => ({ label, text: `合成完整回复 ${index + 1}\n完整保留换行与 <原话>` })), analysis: {
  summary: "可能暂时想休息。", meaning: "这不一定是在拒绝继续联系。", uncertainty: "仅凭这句不能确定原因。", responseDirection: "可以给对方留一点空间。",
  evidence: [{ kind: "chat", text: "<img src=bad> 合成摘录", sourceLabel: "较早对话" }] } } : {}) });
function response(call, items, enabled = true) {
  const query = new URL(call.url, "http://local.invalid").searchParams;
  return { ok: true, json: async () => ({ account: call.body?.account || query.get("account"), user: call.body?.user || query.get("user"), enabled, items }) };
}
function assertEntryIcon(entry, name) {
  const icons = entry.querySelectorAll("svg"); assert.equal(icons.length, 1);
  const icon = icons[0];
  assert.equal(icon.namespaceURI, "http://www.w3.org/2000/svg");
  assert.equal(icon.attributes["data-icon"], name);
  assert.equal(icon.attributes["aria-hidden"], "true"); assert.equal(icon.attributes.focusable, "false");
  assert.equal(entry.textContent, "", "status icons do not leave the old text beside the message");
  assert.match(entry.attributes["aria-label"], /\p{Script=Han}/u);
  assert.equal(entry.title, entry.attributes["aria-label"], "hover and assistive text describe the same state");
  return icon;
}

test("per-contact toggle reads persisted state, saves changes and stops polling when disabled", async () => {
  const h = harness(); h.setRequest(call => response(call, [], call.body?.enabled ?? false));
  await h.select();
  assert.equal(h.toggle.hidden, false); assert.equal(h.toggle.disabled, false);
  assert.equal(h.toggle.attributes["aria-checked"], "false"); assert.equal(h.timers.size, 0);
  const [incoming, self] = h.render([message("one"), message("self", "我说的话", "self")]);
  assert.equal(incoming.querySelector("button").hidden, true); assert.equal(self.querySelector("button"), null);
  await h.toggle.emit("click"); await flush();
  assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", enabled: true });
  assert.equal(incoming.querySelector("button").hidden, false); assert.equal(h.timers.size, 1);
  await h.toggle.emit("click"); await flush();
  assert.equal(incoming.querySelector("button").hidden, true); assert.equal(h.timers.size, 0);
});

test("only the opened message expands; complete natural text and background evidence stay local and text-only", async () => {
  const h = harness(); await h.select();
  const [first, second] = h.render([message("one"), message("two")]);
  h.setRequest(call => response(call, [result("one"), result("two")])); await h.tick();
  await first.querySelector("button").emit("click");
  const details = first.querySelector("details"), body = first.querySelector(".message-analysis-body");
  assert.equal(body.hidden, false); assert.equal(details.open, false);
  assert.equal(details.querySelector("summary").textContent, "详细解读");
  assert.equal(body.querySelectorAll("details").length, 1);
  assert.equal(body.querySelectorAll(".message-analysis-paragraph").length, 4);
  assert.equal(details.querySelector(".message-analysis-source").querySelector("p").textContent, "<img src=bad> 合成摘录");
  assert.equal(body.querySelector("img"), null);
  await second.querySelector("button").emit("click");
  assert.equal(body.hidden, true); assert.equal(second.querySelector(".message-analysis-body").hidden, false);
  assert.equal(h.container.scrollTop, 150);
});

test("the primary panel contains only reply text, small copy actions and one collapsed explanation", async () => {
  const h = harness(); h.window.desktopHost = { openPluginWindow() {} };
  h.window.PluginSlots = { continueFromMessage: async () => true };
  await h.select(); const [item] = h.render([message("one")]);
  h.setRequest(call => response(call, [result("one")])); await h.tick(); await item.querySelector("button").emit("click");
  const body = item.querySelector(".message-analysis-body"), details = body.querySelector("details");
  assert.equal(details.open, false); assert.equal(body.querySelectorAll("details").length, 1);
  assert.equal(body.querySelector(".message-analysis-meta").children.length, 0, "current results need no repeated message heading");
  const replies = body.querySelector(".message-analysis-replies");
  assert.equal(replies.children.length, 1); assert.equal(replies.querySelectorAll(".message-analysis-candidate").length, 3);
  assert.equal(replies.querySelectorAll(".message-analysis-copy").length, 3);
  assert.equal(replies.querySelector("strong"), null, "labels and repeated titles do not crowd the reply text");
  assert.equal(replies.querySelector(".message-analysis-action"), null, "regeneration belongs inside details");
  assert.ok(details.contains(body.querySelector(".message-analysis-explanation")));
  assert.ok(details.contains(body.querySelector(".message-analysis-evidence")));
  for (const button of body.querySelectorAll(".message-analysis-action")) assert.ok(details.contains(button));
  assert.equal(details.querySelector(".message-analysis-utilities").querySelector("button").textContent, "重新生成回复");
});

test("viewing missing or running history never generates; explicit component retry binds its version", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("historical", "不要出现在请求中")]);
  await h.tick();
  const initial = h.calls.length;
  await item.querySelector("button").emit("click"); await flush();
  assert.equal(h.calls.length, initial); assert.equal(item.querySelector(".message-analysis-generate").textContent, "开始分析");
  assert.equal(item.querySelector("details").hidden, true, "empty results have one clear action, no duplicate empty explanation");
  h.setRequest(call => response(call, [result("historical", "running")])); await h.tick();
  assert.equal(item.querySelector("button").attributes["data-analysis-state"], "running");
  assert.equal(item.querySelector(".message-analysis-replies").querySelector(".message-analysis-note").textContent, "正在生成回复建议…");
  const count = h.calls.length;
  await item.querySelector("button").emit("click"); await item.querySelector("button").emit("click");
  assert.equal(h.calls.length, count);
  h.setRequest(call => response(call, [result("historical", "error")])); await h.tick();
  assert.equal(item.querySelector(".message-analysis-explanation").querySelector(".message-analysis-action").textContent, "重试解读");
  h.setRequest(call => response(call, [result("historical")]));
  await item.querySelector(".message-analysis-explanation").querySelector(".message-analysis-action").emit("click"); await flush();
  assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", messageId: "historical", part: "analysis", expectedVersion: "historical-v1" });
  assert.ok(h.calls.at(-1).url.endsWith("/retry"));
  assert.equal(item.querySelector("button").textContent, "?");
  assert.equal(item.querySelector(".message-analysis-explanation").querySelector("p").textContent, "可能暂时想休息。");
});

test("a missing historical result starts only on an explicit click and cannot submit twice", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("old", "原话不发送给生成接口")]); await flush();
  await item.querySelector("button").emit("click"); await h.tick();
  assert.equal(h.calls.filter(call => call.options.method === "POST").length, 0);
  const wait = deferred(); h.setRequest(() => wait.promise);
  const start = item.querySelector(".message-analysis-generate");
  h.document.activeElement = start;
  await start.emit("click");
  const submitted = h.calls.at(-1);
  assert.ok(submitted.url.endsWith("/generate"));
  assert.deepEqual(submitted.body, { account: "account-a", user: "friend-a", messageId: "old" });
  assert.equal(start.disabled, true); assert.equal(start.textContent, "正在提交…");
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "submitting");
  assertEntryIcon(item.querySelector(".message-analysis-question"), "spinner");
  assert.equal(item.querySelector(".message-analysis-question").classList.contains("has-result"), false);
  await start.emit("click");
  assert.equal(h.calls.at(-1), submitted);
  wait.resolve(response(submitted, [result("old", "pending")])); await flush();
  assert.equal(item.querySelector(".message-analysis-generate"), null, "a focused start button must not pin the empty state");
  assert.equal(item.querySelector("button").attributes["data-analysis-state"], "pending");
  assert.equal(item.querySelector("details").hidden, false);
  h.setRequest(call => response(call, [result("old")])); await h.tick();
  assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 3);
  assert.equal(item.querySelector("details").open, false);
  assert.equal(h.calls.filter(call => call.options.method === "POST").length, 1);
});

test("an uncertain generate response rereads the job without automatically generating again", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]); await h.tick();
  await item.querySelector("button").emit("click");
  h.setRequest(call => call.url.endsWith("/generate") ? { ok: false, status: 503, json: async () => ({ error: "unavailable" }) } :
    response(call, [result("one", "running")]));
  await item.querySelector(".message-analysis-generate").emit("click"); await flush();
  assert.equal(h.calls.at(-1).options.method, "GET");
  assert.equal(h.calls.filter(call => call.url.endsWith("/generate")).length, 1);
  assert.equal(item.querySelector(".message-analysis-generate"), null);
  assert.match(item.querySelector(".message-analysis-replies").querySelector("p").textContent, /正在生成/);
});

test("a rejected first generation keeps an actionable error after successful empty polling", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]); await h.tick();
  await item.querySelector("button").emit("click");
  h.setRequest(call => call.url.endsWith("/generate") ? { ok: false, status: 422, json: async () => ({ error: "api-not-configured" }) } : response(call, []));
  await item.querySelector(".message-analysis-generate").emit("click"); await flush(); await h.tick();
  const start = item.querySelector(".message-analysis-generate");
  assert.equal(start.textContent, "重试分析"); assert.equal(start.disabled, false);
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "error");
  assert.match(item.querySelector(".message-analysis-empty").querySelector("p").textContent, /配置生成模型/);
  h.setRequest(call => response(call, [result("one", "pending")]));
  await start.emit("click"); await flush();
  assert.equal(h.calls.filter(call => call.url.endsWith("/generate")).length, 2);
  assert.equal(item.querySelector(".message-analysis-generate"), null);
});

test("failed status reads offer a read retry instead of treating unknown results as safe to generate", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]); await flush();
  await item.querySelector("button").emit("click");
  h.setRequest(() => ({ ok: false, status: 503, json: async () => ({}) })); await h.tick();
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "read-error");
  assertEntryIcon(item.querySelector(".message-analysis-question"), "alert");
  assert.equal(item.querySelector(".message-analysis-generate").textContent, "重新读取");
  h.setRequest(call => response(call, []));
  await item.querySelector(".message-analysis-generate").emit("click"); await flush();
  assert.equal(h.calls.at(-1).options.method, "GET");
  assert.equal(item.querySelector(".message-analysis-generate").textContent, "开始分析");
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "empty");
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
});

test("late generation cannot update a different chat or a disabled provider", async () => {
  for (const change of ["friend", "disabled"]) {
    const h = harness(); await h.select(); const [item] = h.render([message("one")]); await h.tick();
    await item.querySelector("button").emit("click");
    const wait = deferred(); h.setRequest(() => wait.promise);
    const start = item.querySelector(".message-analysis-generate"); await start.emit("click"); const submitted = h.calls.at(-1);
    h.setRequest(call => response(call, [], false));
    if (change === "friend") await h.select("account-a", "friend-b");
    else { h.setPluginEnabled(false); await h.ui.pluginsChanged(); }
    wait.resolve(response(submitted, [result("one")])); await flush();
    assert.equal(submitted.options.signal.aborted, true);
    assert.equal(item.querySelector(".message-analysis-body").hidden, true);
    assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 0);
    const before = h.calls.length; await start.emit("click"); await flush(); assert.equal(h.calls.length, before);
  }
});

test("rerender preserves the single expanded analysis, evidence disclosure and scroll position", async () => {
  const h = harness(); await h.select();
  let [item] = h.render([message("one")]); h.setRequest(call => response(call, [result("one")])); await h.tick();
  await item.querySelector("button").emit("click"); const details = item.querySelector("details"); details.open = true; await details.emit("toggle");
  [item] = h.render([message("one")], true);
  assert.equal(item.querySelector(".message-analysis-body").hidden, false); assert.equal(item.querySelector("details").open, true);
  const body = item.querySelector(".message-analysis-body"), firstParagraph = body.children[0];
  await h.tick();
  assert.equal(body.children[0], firstParagraph, "unchanged polling does not rebuild the opened body");
  assert.equal(h.container.scrollTop, 150);
});

test("late responses after switching account, user, or clearing cannot render or restart polling", async () => {
  for (const target of [["account-b", "friend-a"], ["account-a", "friend-b"], null]) {
    const h = harness(), wait = deferred();
    await h.select(); const [old] = h.render([message("one")]);
    h.setRequest(() => wait.promise); const running = h.ui.recover();
    const oldCall = h.calls.at(-1);
    h.setRequest(call => response(call, [], false));
    if (target) await h.select(...target); else h.ui.reset();
    wait.resolve(response(oldCall, [result("one")])); await flush();
    assert.equal(old.querySelector("button").hidden, true);
    assert.equal(h.toggle.attributes["aria-checked"], "false"); assert.equal(h.timers.size, 0);
    assert.equal(oldCall.options.signal.aborted, true);
  }
});

test("group and empty selections have no control, requests, or message analysis hooks", async () => {
  const h = harness();
  for (const [account, user, group] of [["account-a", "room@chatroom", false], ["account-a", "room", true], [null, null, false]]) {
    await h.select(account, user, group);
    const [item] = h.render([message("one")], true);
    assert.equal(item.querySelector("button"), null); assert.equal(h.calls.length, 0);
  }
});

test("poll includes new messages but never sends their text and suspends while hidden", async () => {
  const h = harness(); await h.select(); h.render([message("one", "private-source-text")]); await h.tick();
  h.render([message("two", "more-private-source-text")]); await h.tick();
  const ids = JSON.parse(new URL(h.calls.at(-1).url, "http://local.invalid").searchParams.get("messageIds"));
  assert.ok(ids.includes("one") && ids.includes("two"));
  assert.equal(JSON.stringify(h.calls).includes("private-source-text"), false);
  await h.visibility(true); assert.equal(h.timers.size, 0);
  const count = h.calls.length; await h.visibility(false);
  assert.equal(h.calls.length, count + 1); assert.equal(h.timers.size, 1);
});

test("scope-mismatched and failed status requests show a retry without trusting incoming content", async () => {
  const h = harness();
  h.setRequest(() => ({ ok: true, json: async () => ({ account: "wrong", user: "friend-a", enabled: true, items: [] }) }));
  await h.select();
  assert.equal(h.toggle.disabled, true); assert.equal(h.status.hidden, false); assert.equal(h.timers.size, 0);
  h.setRequest(call => response(call, [], false)); await h.status.emit("click"); await flush();
  assert.equal(h.toggle.disabled, false); assert.equal(h.status.hidden, true);
});

test("continue in assistant carries only a source quote, never inferred analysis, and requires a gesture", async () => {
  const h = harness(), received = []; h.window.desktopHost = { openPluginWindow() {} };
  h.window.PluginSlots = { continueFromMessage: async value => { received.push(plain(value)); return true; } };
  await h.select(); const [item] = h.render([message("one", "合成原话")]);
  h.setRequest(call => response(call, [result("one")])); await h.tick(); await item.querySelector("button").emit("click");
  assert.equal(received.length, 0); await item.querySelectorAll(".message-analysis-action").find(button => button.textContent === "继续聊这句").emit("click");
  assert.deepEqual(received, [{ account: "account-a", user: "friend-a", messageId: "one", text: "合成原话", kind: "text" }]);
});

test("without an enabled chat panel plugin there is no 继续聊这句 action", async () => {
  // Added 2026-10-14 (review): a disabled 聊天助手 leaves no button that can only fail.
  const h = harness(); h.window.desktopHost = { openPluginWindow() {} };
  h.window.PluginSlots = { continueFromMessage: async () => true, canContinue: () => false };
  await h.select(); const [item] = h.render([message("one", "合成原话")]);
  h.setRequest(call => response(call, [result("one")])); await h.tick(); await item.querySelector("button").emit("click");
  assert.ok(item.querySelector(".message-analysis-body").querySelector("details"));
  assert.equal(item.querySelectorAll(".message-analysis-action").some(button => button.textContent === "继续聊这句"), false);
});

test("missing results in authoritative polling clear old ready text instead of retaining an outdated analysis", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  h.setRequest(call => response(call, [result("one")])); await h.tick(); await item.querySelector("button").emit("click");
  assert.ok(item.querySelector(".message-analysis-explanation").querySelector(".message-analysis-paragraph"));
  h.setRequest(call => response(call, [])); await h.tick();
  assert.equal(item.querySelector(".message-analysis-explanation").querySelector(".message-analysis-paragraph"), null);
  assert.equal(item.querySelector(".message-analysis-generate").textContent, "开始分析");
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "empty");
  assert.equal(item.querySelector(".message-analysis-question").classList.contains("has-result"), false);
});

test("replacing message content invalidates its result and rejects an older pending response", async () => {
  const h = harness(); await h.select(); let [item] = h.render([message("one", "旧原话")]);
  h.setRequest(call => response(call, [result("one")])); await h.tick(); await item.querySelector("button").emit("click");
  const wait = deferred(); h.setRequest(() => wait.promise); h.ui.recover(); const oldCall = h.calls.at(-1);
  [item] = h.render([message("one", "新原话")], true);
  assert.equal(item.querySelector(".message-analysis-explanation").querySelector(".message-analysis-paragraph"), null);
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "loading");
  const fresh = deferred(); h.setRequest(() => fresh.promise);
  wait.resolve(response(oldCall, [result("one")])); await flush();
  assert.equal(item.querySelector(".message-analysis-explanation").querySelector(".message-analysis-paragraph"), null);
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "loading");
  fresh.resolve(response(h.calls.at(-1), [])); await flush();
  assert.equal(item.querySelector(".message-analysis-question").attributes["data-analysis-state"], "empty");
});

test("unread messages stay visibly unknown until their own successful status read, then allow manual start", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  const entry = item.querySelector(".message-analysis-question");
  assert.equal(entry.attributes["data-analysis-state"], "loading"); assert.equal(entry.attributes["aria-busy"], "true");
  assertEntryIcon(entry, "spinner");
  assert.equal(entry.classList.contains("has-result"), false);
  await entry.emit("click");
  const start = item.querySelector(".message-analysis-generate");
  assert.equal(start.textContent, "读取中…"); assert.equal(start.disabled, true);
  await start.emit("click"); assert.equal(h.calls.some(call => call.options.method === "POST"), false);
  await h.tick();
  assert.equal(entry.attributes["data-analysis-state"], "empty"); assert.equal(entry.attributes["aria-busy"], "false");
  assertEntryIcon(entry, "play");
  assert.equal(entry.classList.contains("has-result"), false);
  assert.equal(item.querySelector(".message-analysis-generate").disabled, false);
  assert.equal(item.querySelector(".message-analysis-generate").textContent, "开始分析");
  assert.equal(item.querySelector(".message-analysis-body").hidden, false);
});

test("four compact markers retain the detailed status through their accessible labels", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  const entry = item.querySelector(".message-analysis-question"), markers = new Set();
  for (const [value, state, icon, description, readable] of [
    [null, "empty", "play", /尚未分析/, false],
    [result("one", "pending"), "pending", "spinner", /排队/, false],
    [result("one", "running"), "running", "spinner", /生成/, false],
    [result("one", "error"), "error", "alert", /生成失败/, false],
    [result("one", "stale"), "stale", "alert", /中断/, false],
    [{ ...result("one"), state: "partial", suggestionsState: "error", replies: undefined }, "partial", "alert", /解读已完成；回复建议生成失败/, true],
    [result("one"), "ready", null, /查看/, true],
    [{ ...result("one"), applicability: "needs-update" }, "ready", null, /查看/, true],
  ]) {
    h.setRequest(call => response(call, value ? [value] : [])); await h.tick();
    assert.equal(entry.attributes["data-analysis-state"], state); assert.equal(entry.classList.contains("has-result"), readable);
    assert.match(entry.attributes["aria-label"], description);
    if (icon) assertEntryIcon(entry, icon);
    else { assert.equal(entry.textContent, "?"); assert.equal(entry.querySelector("svg"), null); }
    markers.add(icon || "?");
    assert.equal(item.querySelector(".message-analysis-body").hidden, true, "state transitions never expand a result");
  }
  assert.deepEqual([...markers].sort(), ["?", "alert", "play", "spinner"]);
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
});

test("polling, expansion and queued-to-running transitions preserve the busy icon", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  const entry = item.querySelector(".message-analysis-question");
  h.setRequest(call => response(call, [result("one", "pending")])); await h.tick();
  const spinner = assertEntryIcon(entry, "spinner");
  await h.tick(); await entry.emit("click");
  assert.equal(entry.querySelector("svg"), spinner, "a read or disclosure change must not restart the icon");
  h.setRequest(call => response(call, [result("one", "running")])); await h.tick();
  assert.equal(assertEntryIcon(entry, "spinner"), spinner);
  assert.equal(entry.attributes["data-analysis-state"], "running"); assert.match(entry.title, /正在生成/);
  h.setRequest(call => response(call, [{ ...result("one", "running"), version: "one-v2" }])); await h.tick();
  assert.equal(entry.querySelector("svg"), spinner, "same-state polling preserves spinner animation continuity");
  h.setRequest(call => response(call, [result("one")])); await h.tick();
  assert.equal(entry.attributes["data-analysis-state"], "ready");
  assert.equal(entry.querySelector("svg"), null); assert.equal(entry.textContent, "?");
  assert.equal(item.querySelector(".message-analysis-body").hidden, false);
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
});

test("ready and partial status names alone never imply readable content", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  const entry = item.querySelector(".message-analysis-question");
  for (const state of ["ready", "partial"]) {
    h.setRequest(call => response(call, [{ ...result("one"), state, analysis: { summary: " " }, replies: [] }]));
    await h.tick(); assert.equal(entry.attributes["data-analysis-state"], "incomplete"); assert.equal(entry.classList.contains("has-result"), false);
    assertEntryIcon(entry, "alert");
  }
  await entry.emit("click"); assert.ok(item.querySelector(".message-analysis-retry"), "an incomplete saved record retains a retry action");
});

test("a status reply from before a new message cannot mark the new message empty or analyzed", async () => {
  const h = harness(); await h.select(); h.render([message("one")]); await h.tick();
  const previous = deferred(), next = deferred();
  h.setRequest(() => previous.promise); h.ui.recover(); const oldCall = h.calls.at(-1);
  const [newItem] = h.render([message("two")]); const entry = newItem.querySelector(".message-analysis-question");
  assert.equal(entry.attributes["data-analysis-state"], "loading");
  h.setRequest(() => next.promise); previous.resolve(response(oldCall, [result("one")])); await flush();
  assert.equal(entry.attributes["data-analysis-state"], "loading"); assert.equal(entry.classList.contains("has-result"), false);
  const nextCall = h.calls.at(-1), ids = JSON.parse(new URL(nextCall.url, "http://local.invalid").searchParams.get("messageIds"));
  assert.ok(ids.includes("two")); assert.notEqual(nextCall, oldCall);
  next.resolve(response(nextCall, [result("one"), result("two", "pending")])); await flush();
  assert.equal(entry.attributes["data-analysis-state"], "pending"); assert.equal(entry.classList.contains("has-result"), false);
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
});

test("legacy intent and emotion labels hide only while message analysis is enabled and retain their nodes", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  const labels = h.document.createElement("div"); labels.className = "inline-intent-row"; labels.textContent = "合成旧标签";
  item.querySelector(".msg-content-wrap").appendChild(labels);
  assert.equal(item.classList.contains("has-message-analysis"), true);
  await h.toggle.emit("click"); await flush();
  assert.equal(item.classList.contains("has-message-analysis"), false);
  assert.equal(item.querySelector(".inline-intent-row"), labels);
  await h.toggle.emit("click"); await flush();
  assert.equal(item.classList.contains("has-message-analysis"), true);
  assert.equal(item.querySelector(".inline-intent-row"), labels);
  const css = readFileSync(path.join(__dirname, "../chatui/message-analysis.css"), "utf8");
  for (const selector of ["inline-intent-row", "inline-expression-row", "inline-intent-pending", "msg-mood"]) assert.ok(css.includes(`.has-message-analysis .${selector}`));
});

test("all three incoming results stay collapsed and a new result cannot replace the selected target", async () => {
  const h = harness(); await h.select();
  const nodes = h.render([message("one"), message("two"), message("three")]);
  h.setRequest(call => response(call, [result("one"), result("two"), result("three")])); await h.tick();
  for (const item of nodes) assert.equal(item.querySelector(".message-analysis-body").hidden, true);
  const calls = h.calls.length; await nodes[0].querySelector("button").emit("click");
  assert.equal(h.calls.length, calls, "opening a saved result is local");
  assert.equal(nodes[0].querySelectorAll(".message-analysis-candidate").length, 3);
  const [newNode] = h.render([message("four")]);
  h.setRequest(call => response(call, [result("one"), result("two"), result("three"), result("four")])); await h.tick();
  assert.equal(nodes[0].querySelector(".message-analysis-body").hidden, false);
  assert.equal(newNode.querySelector(".message-analysis-body").hidden, true);
  assert.equal(h.ui.isReading(), true); assert.equal(h.container.scrollTop, 150);
});

test("partial success retains the other part and retry explicitly requests only the failed component", async () => {
  for (const failed of ["analysis", "suggestions"]) {
    const h = harness(); await h.select(); const [item] = h.render([message("one")]);
    const value = { ...result("one"), state: "partial", [`${failed}State`]: "error" };
    delete value[failed === "analysis" ? "analysis" : "replies"];
    h.setRequest(call => response(call, [value])); await h.tick(); await item.querySelector("button").emit("click");
    const failedSection = item.querySelector(failed === "analysis" ? ".message-analysis-explanation" : ".message-analysis-replies");
    const failedLabel = failed === "analysis" ? "解读" : "回复建议", entry = item.querySelector(".message-analysis-question");
    assertEntryIcon(entry, "alert"); assert.match(entry.title, new RegExp(`${failedLabel}生成失败`));
    assert.match(item.querySelector(".message-analysis-caption").textContent, new RegExp(`${failedLabel}生成失败`));
    assert.equal(item.querySelectorAll(".message-analysis-candidate").length, failed === "analysis" ? 3 : 0);
    if (failed === "suggestions") assert.match(item.querySelector(".message-analysis-explanation").querySelector("p").textContent, /可能暂时/);
    await failedSection.querySelector("button").emit("click"); await flush();
    assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", messageId: "one", part: failed, expectedVersion: "one-v1" });
  }
});

test("regeneration keeps saved content and copy access while exposing busy or failed updates", async () => {
  for (const part of ["analysis", "suggestions"]) {
    const h = harness(), value = { ...result("one"), copyAllowed: true }, copied = [];
    h.window.desktopHost = { copyDraft: async text => { copied.push(text); return true; } };
    h.setRequest(call => response(call, [value]));
    await h.select(); const [item] = h.render([message("one")]); await h.tick(); await item.querySelector("button").emit("click");
    const entry = item.querySelector(".message-analysis-question"), label = part === "analysis" ? "解读" : "回复建议";
    for (const state of ["pending", "running", "error"]) {
      Object.assign(value, { state: state === "error" ? "partial" : state, [`${part}State`]: state, version: `one-${part}-${state}` });
      if (state === "error") value[`${part}Error`] = "模型服务暂时不可用（HTTP 502）。<img src=synthetic>";
      await h.tick();
      assertEntryIcon(entry, state === "error" ? "alert" : "spinner");
      assert.match(entry.title, new RegExp(`${label}${state === "pending" ? "排队更新" : state === "running" ? "正在更新" : "更新失败"}`));
      const section = item.querySelector(part === "analysis" ? ".message-analysis-explanation" : ".message-analysis-replies");
      assert.match(section.querySelector(".message-analysis-note").textContent, /上次结果/);
      assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 3);
      assert.equal(item.querySelector(".message-analysis-paragraph").textContent, value.analysis.summary);
      assert.ok(item.querySelector(".message-analysis-source"), "saved evidence remains available");
      const copy = item.querySelector(".message-analysis-copy"); assert.equal(copy.disabled, false);
      await copy.emit("click"); await flush();
      assert.equal(copied.at(-1), value.replies[0].text);
      if (state === "error") {
        assert.match(section.querySelector(".message-analysis-note").textContent, /HTTP 502/);
        assert.equal(section.querySelector("img"), null, "failure classifications are plain text");
        const retry = section.querySelector(".message-analysis-retry"); assert.ok(retry); assert.equal(retry.disabled, false);
        await retry.emit("click"); await flush();
        assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", messageId: "one", part, expectedVersion: value.version });
      }
    }
    Object.assign(value, { state: "ready", [`${part}State`]: "ready", version: `one-${part}-updated` });
    delete value[`${part}Error`];
    if (part === "analysis") value.analysis.summary = "更新后的合成解读。";
    else value.replies[0].text = "更新后的合成回复。";
    await h.tick();
    assert.equal(entry.textContent, "?");
    assert.equal(item.querySelector(".message-analysis-paragraph").textContent, value.analysis.summary);
    assert.equal(item.querySelector(".message-analysis-candidate-text").textContent, value.replies[0].text);
    assert.equal(item.querySelector(".message-analysis-utilities").querySelector("button").textContent, "重新生成回复");
  }
});

test("copy immediately writes the exact displayed text without reserving or making a request", async () => {
  const h = harness(), copied = [], value = result("one");
  value.replies[1].text = "  保留开头空格\n\n完整 <原话> 与 emoji 🙂\n末尾空格  ";
  h.window.desktopHost = {
    reserveCopyDraft: () => { assert.fail("copy must not reserve a verification ticket"); },
    copyDraft: async (...args) => { copied.push(args); return true; },
    releaseCopyDraft: () => { assert.fail("copy must not release a verification ticket"); },
  };
  h.setRequest(call => response(call, [value]));
  await h.select(); const [item] = h.render([message("one")]);
  await h.tick(); await item.querySelector("button").emit("click");
  const buttons = item.querySelectorAll(".message-analysis-copy"), calls = h.calls.length;
  await buttons[1].emit("click"); await flush();
  assert.deepEqual(copied, [[value.replies[1].text]]);
  assert.equal(h.calls.length, calls, "copy has no network dependency");
  assert.equal(buttons[1].textContent, "已复制"); assert.equal(buttons[0].textContent, "复制");
  assert.equal(buttons[1].disabled, false);
});

test("an in-flight clipboard write survives concurrent source checks and failed status reads", async () => {
  for (const scenario of ["checking", "unavailable", "http-error", "status-error"]) {
    const h = harness(), wait = deferred(), copied = [], value = { ...result("one"), sourceState: "verified", suggestionsSourceState: "verified", copyAllowed: true };
    h.window.desktopHost = { copyDraft: text => { copied.push(text); return wait.promise; } };
    h.setRequest(call => response(call, [value]));
    await h.select(); const [item] = h.render([message("one")]); await h.tick(); await item.querySelector("button").emit("click");
    h.setRequest(call => {
      if (scenario === "http-error") return { ok: false, status: 503, json: async () => ({ error: "unavailable" }) };
      if (scenario === "status-error") return { ok: true, json: async () => ({ account: "account-a", user: "friend-a", enabled: true, items: [], error: "unavailable" }) };
      return response(call, [{ ...value, sourceState: scenario, suggestionsSourceState: scenario, applicability: "unverified", copyAllowed: false }]);
    });
    const copy = item.querySelector(".message-analysis-copy"), calls = h.calls.length;
    await copy.emit("click");
    assert.equal(h.calls.length, calls, scenario);
    assert.deepEqual(copied, [value.replies[0].text], scenario);
    await h.tick();
    assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 3, scenario);
    wait.resolve(true); await flush();
    assert.equal(copy.textContent, "已复制", scenario);
    assert.equal(copy.disabled, false, scenario);
  }
});

test("copy uses the visible candidate when selection or focus holds an older version on screen", async () => {
  for (const hold of ["selection", "focus"]) {
    const h = harness(), value = result("one"), copied = [];
    h.window.desktopHost = { copyDraft: async text => { copied.push(text); return true; } };
    h.setRequest(call => response(call, [plain(value)]));
    await h.select(); const [item] = h.render([message("one")]); await h.tick(); await item.querySelector("button").emit("click");
    const candidate = item.querySelector(".message-analysis-candidate-text"), copy = item.querySelector(".message-analysis-copy");
    const displayed = candidate.textContent;
    if (hold === "selection") h.selection(candidate); else h.document.activeElement = copy;
    Object.assign(value, { version: "one-v2", sourceState: "changed", suggestionsSourceState: "changed", applicability: "needs-update", copyAllowed: false });
    value.replies[0].text = "后台已经更新，但屏幕还没有展示的新候选";
    await h.tick();
    assert.equal(item.querySelector(".message-analysis-candidate-text"), candidate, hold);
    assert.equal(candidate.textContent, displayed, hold);
    assert.equal(copy.disabled, false, hold);
    const calls = h.calls.length;
    await copy.emit("click"); await flush();
    assert.deepEqual(copied, [displayed], hold);
    assert.equal(copy.textContent, "已复制", hold);
    assert.equal(h.calls.length, calls, hold);
  }
});

test("browser clipboard fallback writes the same text when the desktop bridge is absent or fails", async () => {
  for (const bridge of ["absent", "false", "throws"]) {
    const h = harness(), copied = [], value = result("one");
    if (bridge !== "absent") h.window.desktopHost = { copyDraft: async text => {
      copied.push(["desktop", text]);
      if (bridge === "throws") throw new Error("clipboard unavailable");
      return false;
    } };
    h.window.navigator = { clipboard: { writeText: async text => { copied.push(["browser", text]); } } };
    h.setRequest(call => response(call, [value]));
    await h.select(); const [item] = h.render([message("one")]); await h.tick(); await item.querySelector("button").emit("click");
    const copy = item.querySelector(".message-analysis-copy"), calls = h.calls.length;
    await copy.emit("click"); await flush();
    const expected = [["browser", value.replies[0].text]];
    if (bridge !== "absent") expected.unshift(["desktop", value.replies[0].text]);
    assert.deepEqual(copied, expected, bridge);
    assert.equal(copy.textContent, "已复制", bridge);
    assert.equal(h.calls.length, calls, bridge);
  }
});

test("clipboard completion after switching friends cannot change the next friend's copy feedback", async () => {
  for (const outcome of [true, false]) {
    const h = harness(), wait = deferred(), copied = [];
    h.window.desktopHost = { copyDraft: text => { copied.push(text); return wait.promise; } };
    h.setRequest(call => response(call, [result("one")]));
    await h.select(); const [first] = h.render([message("one")]); await h.tick(); await first.querySelector("button").emit("click");
    await first.querySelector(".message-analysis-copy").emit("click");
    assert.deepEqual(copied, [result("one").replies[0].text]);
    await h.select("account-a", "friend-b");
    const [next] = h.render([message("one")], true); await h.tick(); await next.querySelector("button").emit("click");
    wait.resolve(outcome); await flush();
    assert.equal(next.querySelector(".message-analysis-copy").textContent, "复制", String(outcome));
    assert.equal(next.querySelector(".message-analysis-copy").disabled, false, String(outcome));
    assert.equal(next.querySelector(".message-analysis-copy-feedback").textContent, "", String(outcome));
  }
});

test("clipboard rejection reports actual failure on the candidate and retains its full selectable text", async () => {
  const h = harness(), value = result("one");
  h.window.navigator = { clipboard: { writeText: async () => { throw new Error("denied"); } } };
  await h.select(); const [item] = h.render([message("one")]);
  h.setRequest(call => response(call, [value]));
  await h.tick(); await item.querySelector("button").emit("click"); await item.querySelector(".message-analysis-copy").emit("click"); await flush();
  assert.match(item.querySelector(".message-analysis-copy-feedback").textContent, /复制失败/);
  assert.equal(item.querySelector(".message-analysis-candidate-text").textContent, value.replies[0].text);
  assert.equal(item.querySelector(".message-analysis-copy").textContent, "复制");
  assert.equal(item.querySelector(".message-analysis-copy").disabled, false);
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
});

test("incomplete, empty or duplicate reply triples never render as usable candidates", async () => {
  for (const replies of [[{ label: "少一条", text: "一" }], ["一", "二", " "].map(text => ({ label: "回应", text })),
    ["Ａ", "A", "不同"].map(text => ({ label: "回应", text }))]) {
    const h = harness(); await h.select(); const [item] = h.render([message("one")]);
    h.setRequest(call => response(call, [{ ...result("one"), replies }])); await h.tick(); await item.querySelector("button").emit("click");
    assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 0);
    assert.match(item.querySelector(".message-analysis-replies").querySelector("p").textContent, /结果不完整/);
  }
});

test("historical labeling keeps reply nodes stable, selected text survives updates and changed sources keep copy available", async () => {
  const h = harness(), value = result("one"); await h.select(); const [item] = h.render([message("one")]);
  h.setRequest(call => response(call, [plain(value)])); await h.tick(); await item.querySelector("button").emit("click");
  const first = item.querySelector(".message-analysis-candidate-text");
  value.applicability = "historical"; await h.tick();
  assert.equal(item.querySelector(".message-analysis-candidate-text"), first);
  assert.match(item.querySelector(".message-analysis-caption").textContent, /历史参考/);
  h.selection(first); value.version = "one-v2"; value.replies[0].text = "更新后的完整回复"; await h.tick();
  assert.equal(item.querySelector(".message-analysis-candidate-text"), first);
  h.selection(null);
  assert.equal(item.querySelector(".message-analysis-candidate-text").textContent, "更新后的完整回复");
  value.applicability = "needs-update"; value.sourceState = "changed";
  value.sourceMessage = "原消息内容已变更，请重新生成。"; value.copyAllowed = false; await h.tick();
  assert.equal(item.querySelector(".message-analysis-copy").disabled, false);
  assert.equal(item.querySelector(".message-analysis-caption").textContent, value.sourceMessage);
});

test("historical, changed and unverified source states keep completed replies directly copyable", async () => {
  const h = harness(), value = result("one"), copied = []; await h.select(); const [item] = h.render([message("one")]);
  h.window.desktopHost = { copyDraft: async text => { copied.push(text); return true; } };
  h.setRequest(call => response(call, [plain(value)])); await h.tick(); await item.querySelector("button").emit("click");
  const candidate = item.querySelector(".message-analysis-candidate-text"), copy = item.querySelector(".message-analysis-copy");
  for (const [sourceState, sourceMessage] of [["checking", "正在核对原消息，请稍候。"], ["unavailable", "原消息暂时无法读取，请稍后重试。"], ["changed", "原消息内容已变更，请重新生成。"]]) {
    Object.assign(value, { sourceState, sourceMessage, copyAllowed: false, applicability: sourceState === "changed" ? "needs-update" : "unverified" });
    await h.tick();
    assert.equal(item.querySelector("button").textContent, "?", "source checks do not erase successful generation");
    assert.equal(item.querySelector(".message-analysis-candidate-text"), candidate);
    assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 3);
    assert.equal(item.querySelector(".message-analysis-caption").textContent, sourceMessage);
    assert.equal(copy.disabled, false);
    const calls = h.calls.length;
    await copy.emit("click"); await flush();
    assert.equal(copied.at(-1), candidate.textContent);
    assert.equal(copy.textContent, "已复制");
    assert.equal(h.calls.length, calls);
  }
  Object.assign(value, { sourceState: "verified", sourceMessage: "", copyAllowed: true, applicability: "historical" });
  await h.tick(); assert.equal(copy.disabled, false);
  assert.equal(item.querySelector(".message-analysis-candidate-text"), candidate);
  assert.match(item.querySelector(".message-analysis-caption").textContent, /历史参考/);
  await copy.emit("click"); await flush(); assert.equal(copied.length, 4);
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
});

test("legacy migration explains that old automation stopped and can be dismissed without enabling the unified feature", async () => {
  const h = harness();
  h.setRequest(async call => {
    const value = await response(call, [], false).json(); value.migrationRequired = call.options.method !== "POST";
    return { ok: true, json: async () => value };
  });
  await h.select();
  assert.equal(h.elements.messageAnalysisMigration.hidden, false);
  assert.match(h.elements.messageAnalysisMigrationText.textContent, /已停用/);
  await h.elements.btnDisableLegacyAnalysis.emit("click"); await flush();
  assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", enabled: false });
  assert.equal(h.elements.messageAnalysisMigration.hidden, true); assert.equal(h.toggle.attributes["aria-checked"], "false");
});

test("legacy analysis remains readable without unified consent and explicit disable hides it", async () => {
  for (const suggestionsEnabled of [false, true]) {
    const h = harness();
    let migrationRequired = true;
    let saved = { ...result("one", "running"), suggestionsState: "none" };
    h.setRequest(async call => {
      if (call.options.method === "POST") migrationRequired = false;
      const value = await response(call, [plain(saved)], false).json();
      return { ok: true, json: async () => ({ ...value, migrationRequired,
        legacy: { analysisEnabled: true, suggestionsEnabled } }) };
    });
    await h.select(); const [item] = h.render([message("one")]); await flush();
    assert.equal(h.toggle.attributes["aria-checked"], "false");
    assert.equal(item.querySelector("button").hidden, false);
    assert.equal(h.timers.size, 1, "existing analysis tasks remain observable");
    const calls = h.calls.length;
    await item.querySelector("button").emit("click");
    assert.equal(h.calls.length, calls, "viewing does not start generation");
    assert.equal(item.querySelector(".message-analysis-body").hidden, false);
    assert.equal(h.ui.isReading(), true); assert.equal(h.ui.openedMessageId(), "one");
    assert.equal(item.querySelector(".message-analysis-replies").hidden, true);
    assert.equal(item.querySelector(".message-analysis-action"), null);
    saved = { ...result("one", "error"), suggestionsState: "none" };
    await h.tick();
    assert.equal(item.querySelector(".message-analysis-action"), null, "legacy viewing cannot call the unified retry endpoint");
    saved = { ...result("one"), suggestionsState: "none", replies: undefined };
    await h.tick();
    assert.equal(item.querySelector(".message-analysis-explanation").querySelector("p").textContent, "可能暂时想休息。");
    assert.equal(item.querySelector(".message-analysis-body").hidden, false);
    assert.equal(item.querySelectorAll(".message-analysis-candidate").length, 0);
    assert.equal(h.calls.some(call => call.options.method === "POST"), false);
    await h.visibility(true); assert.equal(h.timers.size, 0);
    await h.visibility(false); assert.equal(h.timers.size, 1);
    await h.elements.btnDisableLegacyAnalysis.emit("click"); await flush();
    assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", enabled: false });
    assert.equal(item.querySelector("button").hidden, true);
    assert.equal(item.querySelector(".message-analysis-body").hidden, true);
    assert.equal(h.ui.isReading(), false); assert.equal(h.timers.size, 0);
  }
});

test("legacy suggestions alone does not expose a per-message result or enable unified generation", async () => {
  const h = harness();
  h.setRequest(async call => {
    const value = await response(call, [], false).json();
    return { ok: true, json: async () => ({ ...value, migrationRequired: true,
      legacy: { analysisEnabled: false, suggestionsEnabled: true } }) };
  });
  await h.select(); const [item] = h.render([message("one")]);
  assert.equal(h.toggle.attributes["aria-checked"], "false");
  assert.equal(item.querySelector("button").hidden, true);
  assert.equal(h.elements.messageAnalysisMigration.hidden, false);
  const calls = h.calls.length;
  await item.querySelector("button").emit("click"); await h.tick();
  assert.equal(h.calls.length, calls); assert.equal(h.timers.size, 0);
  assert.equal(item.querySelector(".message-analysis-body").hidden, true);
});

test("disabling and re-enabling the provider plugin removes and restores the slot for the open chat", async () => {
  const h = harness(); h.setRequest(call => response(call, [], true));
  await h.select();
  assert.equal(h.toggle.hidden, false);
  h.setPluginEnabled(false); await h.ui.pluginsChanged(); await flush();
  assert.equal(h.toggle.hidden, true);
  const [incoming] = h.render([message("one")], true);
  assert.equal(incoming.querySelector("button"), null);
  h.setPluginEnabled(true); await h.ui.pluginsChanged(); await flush();
  assert.equal(h.toggle.hidden, false);
  assert.equal(h.calls.at(-1).body, null);
  const [again] = h.render([message("one")], true);
  assert.equal(again.querySelector("button").hidden, false);
  h.ui.reset(); h.setPluginEnabled(true); await h.ui.pluginsChanged(); await flush();
  assert.equal(h.toggle.hidden, true);
});

test("incompatible or dependency-blocked annotation providers do not start RPCs", async () => {
  for (const problem of [{ incompatible: "插件需要 Core 接口版本 2" },
    { dependencyError: { code: "plugin-dependency-missing", message: "缺少依赖插件 dev.provider" } }]) {
    const h = harness(); h.setPluginProblem(problem); await h.select();
    assert.equal(h.toggle.hidden, true); assert.equal(h.calls.length, 0);
    const [item] = h.render([message("blocked")]);
    assert.equal(item.querySelector("button"), null); assert.equal(h.timers.size, 0);
  }
});

test("dependency loss removes annotations and recovery restores the open chat without changing enable selection", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]); await h.tick();
  assert.equal(h.toggle.hidden, false);
  h.setPluginProblem({ dependencyError: { code: "plugin-dependency-disabled", message: "依赖插件 dev.provider 已停用" } });
  await h.ui.pluginsChanged(); await flush();
  assert.equal(h.toggle.hidden, true); assert.equal(item.querySelector("button").hidden, true);
  const before = h.calls.length; await h.tick(); assert.equal(h.calls.length, before);
  h.setPluginProblem({}); await h.ui.pluginsChanged(); await flush();
  assert.equal(h.toggle.hidden, false); assert.equal(h.calls.length, before + 1);
  const [restored] = h.render([message("one")], true);
  assert.equal(restored.querySelector("button").hidden, false);
});

test("synced messages without mounted entries do not create an immediate status request loop", async () => {
  const h = harness(); await h.select();
  const messages = [message("before-attach"), message("without-bubble")], waiting = deferred();
  let reads = 0;
  // Bound a regression's request loop so this test reports a failure instead of starving the event loop.
  h.setRequest(call => ++reads <= 4 ? response(call, []) : waiting.promise);
  h.ui.attach(messages[1], h.document.createElement("div"));
  h.ui.sync(messages); await flush();
  assert.equal(reads, 1, "unmounted messages wait for the regular poll after a successful read");
  assert.equal(h.timers.size, 1);
  await h.tick(); assert.equal(reads, 2);
  const mounted = h.render(messages);
  assert.equal(mounted[0].querySelector("button").attributes["data-analysis-state"], "loading");
  await h.tick(); assert.equal(reads, 3, "mounting actual entries still triggers one fresh read");
  assert.ok(mounted.every(item => item.querySelector("button").attributes["data-analysis-state"] === "empty"));
  h.ui.reset();
});

test("bubbles rendered while the provider plugin is still being discovered get their entry once it is known", async () => {
  const h = harness(); h.setRequest(call => response(call, [], true));
  h.ui.setScope("account-a", "friend-a");
  const [early] = h.render([message("one")]);
  assert.equal(early.querySelector("button"), null);
  await flush();
  assert.equal(early.querySelector("button").hidden, false);
  // With the provider known, a newly opened chat applies at once.
  h.ui.setScope("account-a", "friend-b");
  const [next] = h.render([message("two")], true);
  assert.ok(next.querySelector("button"));
});
