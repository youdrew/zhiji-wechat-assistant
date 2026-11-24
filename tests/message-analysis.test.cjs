// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Modified 2026-11-09: per-round blocks replace the per-message entry.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../chatui/message-analysis.js"), "utf8");
const manifest = require("../plugins/kyf.message-insights/plugin.json");
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
  const suggestions = new Node("button"); suggestions.textContent = "建议";
  body.append(container, toggle, suggestions, status);
  const chatInput = Object.assign(new Node("textarea"), { value: "", events: [], dispatchEvent(event) { this.events.push(event.type); } });
  toggle.textContent = "分析";
  const elements = { chatMessages: container, btnMessageAnalysis: toggle, btnMessageSuggestions: suggestions, messageAnalysisStatus: status, chatInput };
  const document = { hidden: false, getElementById: id => elements[id], createElement: tag => new Node(tag),
    createElementNS: (namespace, tag) => Object.assign(new Node(tag), { namespaceURI: namespace }),
    addEventListener: (name, callback) => documentListeners.set(name, callback) };
  const window = { addEventListener: (name, callback) => windowListeners.set(name, callback) };
  const context = vm.createContext({ window, document, URLSearchParams, AbortController, Event: class { constructor(type) { this.type = type; } },
    setTimeout: handler => { timers.set(++timerId, handler); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      // The annotation slot discovers its provider plugin, then calls that plugin through Core RPC.
      if (url === "/api/plugins") return { ok: true, json: async () => ({ plugins: [{ ...manifest, enabled: pluginEnabled, ...pluginProblem }] }) };
      const rpc = JSON.parse(options.body);
      assert.equal(rpc.plugin, "kyf.message-insights");
      const route = { "annotations.status": "", "chatHeader.toggle": "", "annotations.generate": "/generate", "annotations.retry": "/retry", "annotations.copy": "/copy" }[rpc.method];
      assert.notEqual(route, undefined, rpc.method);
      const status = rpc.method === "annotations.status";
      const query = new URLSearchParams({ account: rpc.account, user: rpc.user, messageIds: JSON.stringify(rpc.params.messageIds || []) });
      const call = { url: `/api/advisor/message-analysis${route}${status ? `?${query}` : ""}`, options: { ...options, method: status ? "GET" : "POST" },
        body: status ? null : { account: rpc.account, user: rpc.user, ...rpc.params } };
      calls.push(call);
      const reply = requestImpl ? await requestImpl(call) : response(call, [], call.body?.enabled ?? true);
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
  return { window, document, container, toggle, suggestions, status, elements, calls, timers, ui: window.MessageAnalysis, render,
    selection: node => { window.getSelection = () => ({ isCollapsed: !node, anchorNode: node, focusNode: node }); documentListeners.get("selectionchange")?.(); },
    setRequest: fn => { requestImpl = fn; }, setPluginEnabled: value => { pluginEnabled = value; }, setPluginProblem: value => { pluginProblem = value; }, async tick() { const pending = [...timers.values()]; timers.clear(); for (const fn of pending) fn(); await flush(); },
    async select(account = "account-a", user = "friend-a", group = false) { window.MessageAnalysis.setScope(account, user, group); await flush(); },
    async visibility(hidden) { document.hidden = hidden; documentListeners.get("visibilitychange")(); await flush(); },
    pagehide: () => windowListeners.get("pagehide")() };
}
const message = (id, text = "虚构对话", side = "other") => ({ id, text, side, kind: "text" });
const plans = [["干脆答应", ["可以啊", "周六下午？"]], ["先聊书", ["书怎么样"]], ["问细节", ["哪家店呀", "远不远"]]];
const result = (messageId, state = "ready", roundIds = [messageId]) => ({ messageId, roundIds, version: `${messageId}-v1`, state,
  analysisState: state, suggestionsState: state, applicability: "current", ...(state === "ready" ? {
  replies: plans.map(([label, lines]) => ({ label, text: lines.join("\n"), messages: lines })), analysis: {
  summary: "约周末，也想聊书。", meaning: "一是问周末安排；二是分享读后感。", uncertainty: "不确定具体时间。", responseDirection: "两件事都可以接。",
  evidence: [{ kind: "chat", text: "<img src=bad> 合成摘录", sourceLabel: "目标微信消息" }] } } : {}) });
function response(call, items, enabled = true, extra = {}) {
  const query = new URL(call.url, "http://local.invalid").searchParams;
  return { ok: true, json: async () => ({ account: call.body?.account || query.get("account"), user: call.body?.user || query.get("user"),
    enabled, analysisEnabled: enabled, suggestionsEnabled: enabled, items, ...extra }) };
}
const requestedIds = call => JSON.parse(new URL(call.url, "http://local.invalid").searchParams.get("messageIds"));
const block = item => item.querySelector(".message-round");
const texts = (item, selector) => item.querySelectorAll(selector).map(value => value.textContent);
const button = (item, label) => item.querySelectorAll("button").find(value => value.textContent === label);

test("toolbar switches independently save each part and show only its round controls", async () => {
  const h = harness(), flags = { analysisEnabled: false, suggestionsEnabled: false };
  h.setRequest(call => {
    if (call.body?.part) flags[call.body.part + "Enabled"] = call.body.enabled;
    return response(call, [result("one")], flags.analysisEnabled || flags.suggestionsEnabled, flags);
  });
  await h.select();
  assert.equal(h.toggle.hidden, false); assert.equal(h.toggle.disabled, false);
  assert.equal(h.toggle.attributes["aria-checked"], "false"); assert.equal(h.toggle.classList.contains("active"), false);
  assert.deepEqual([h.toggle.textContent, h.suggestions.textContent], ["分析", "建议"]);
  const [incoming] = h.render([message("one")]);
  assert.equal(block(incoming), null, "nothing hangs under messages while off");
  await h.toggle.emit("click"); await flush();
  assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", part: "analysis", enabled: true });
  assert.equal(h.toggle.classList.contains("active"), true); assert.equal(h.timers.size, 1);
  assert.equal(block(incoming).hidden, false);
  assert.ok(incoming.querySelector("details")); assert.equal(incoming.querySelector(".message-round-bubble"), null);
  await h.suggestions.emit("click"); await flush();
  assert.equal(h.calls.at(-1).body.part, "suggestions"); assert.ok(incoming.querySelector(".message-round-bubble"));
  await h.toggle.emit("click"); await flush();
  assert.equal(h.suggestions.attributes["aria-checked"], "true"); assert.equal(incoming.querySelector("details"), null);
  assert.ok(incoming.querySelector(".message-round-bubble"));
  await h.suggestions.emit("click"); await flush();
  assert.equal(h.toggle.classList.contains("active"), false); assert.equal(block(incoming).hidden, true); assert.equal(h.timers.size, 0);
});

test("analysis alone can manually generate an older round after enabling it from the toolbar", async () => {
  const h = harness(); let analysisEnabled = false;
  h.setRequest(call => {
    if (call.body?.part === "analysis") analysisEnabled = call.body.enabled;
    return response(call, [], analysisEnabled, { analysisEnabled, suggestionsEnabled: false });
  });
  await h.select();
  const [item] = h.render([message("older"), message("mine", "好", "self")]);
  await h.toggle.emit("click"); await flush();
  const start = button(item, "生成本轮解读");
  assert.ok(start); assert.equal(start.disabled, false);
  assert.equal(item.querySelector(".message-round-toggle"), null);
  await start.emit("click"); await flush();
  assert.ok(h.calls.some(call => call.url.endsWith("/generate") && call.body.messageId === "older"));
});

test("a round is every friend message since the user's reply; only its last message carries the block", async () => {
  const h = harness(); await h.select();
  const nodes = h.render([message("a1"), message("a2"), message("mine", "我说的", "self"), message("b1"), message("b2"), message("b3")]);
  await h.tick();
  assert.deepEqual(requestedIds(h.calls.at(-1)), ["b3", "a2"], "status asks for round ends only, newest first");
  const [a1, a2, , b1, b2, b3] = nodes;
  for (const item of [a1, b1, b2]) assert.equal(block(item), null);
  assert.ok(block(a2) && block(b3));
  assert.equal(b3.classList.contains("has-message-round"), true);
  h.setRequest(call => response(call, [result("b3", "ready", ["b1", "b2", "b3"]), result("a2", "ready", ["a1", "a2"])])); await h.tick();
  // The latest round is open with its plans; the older round shows one collapsed line.
  assert.equal(b3.querySelector(".message-round-card-title").textContent, "本轮解读 · 3 条消息");
  assert.deepEqual(texts(b3, ".message-round-tab"), ["干脆答应", "先聊书", "问细节"]);
  assert.deepEqual(texts(b3, ".message-round-bubble"), ["可以啊", "周六下午？"]);
  assert.equal(a2.querySelector(".message-round-bubble"), null);
  assert.match(a2.querySelector(".message-round-toggle").textContent, /回复建议/);
  assert.equal(a2.querySelector(".message-round-card-title").textContent, "本轮解读 · 2 条消息", "a collapsed reply card leaves the explanation card");
  assert.equal(h.ui.isReading(), false, "the open latest round never stops following new messages");
  await a2.querySelector(".message-round-toggle").emit("click");
  assert.equal(h.ui.isReading(), true); assert.equal(h.ui.openedMessageId(), "a2");
  assert.deepEqual(texts(a2, ".message-round-bubble"), ["可以啊", "周六下午？"]);
  await button(a2, "收起").emit("click"); assert.equal(h.ui.isReading(), false);
  // Folding the reply card and the explanation card are independent.
  const card = b3.querySelector(".message-round-card");
  card.open = true; card.emit("toggle");
  await button(b3, "收起").emit("click");
  assert.equal(b3.querySelector(".message-round-bubble"), null);
  assert.equal(b3.querySelector(".message-round-card").open, true, "collapsing the replies keeps the explanation open");
  await b3.querySelector(".message-round-toggle").emit("click");
  assert.deepEqual(texts(b3, ".message-round-bubble"), ["可以啊", "周六下午？"]);
  assert.equal(b3.querySelector(".message-round-card").open, true);
});

test("a new friend message moves the round end and the user's reply closes it", async () => {
  const h = harness(); await h.select();
  const [one] = h.render([message("one")]); await h.tick();
  const [two] = h.render([message("two")]); await h.tick();
  assert.equal(block(one).hidden, true, "a cut-short round end no longer shows a block");
  assert.equal(block(two).hidden, false);
  h.render([message("mine", "回了", "self")]); await h.tick();
  assert.deepEqual(requestedIds(h.calls.at(-1)), ["two"]);
  h.setRequest(call => response(call, [result("two", "ready", ["one", "two"])])); await h.tick();
  assert.equal(two.querySelector(".message-round-bubble"), null, "an answered round starts collapsed");
});

test("plans switch by tab, copy one line at a time and go into the draft as a whole", async () => {
  const h = harness(), copied = []; h.window.desktopHost = { copyDraft: async text => { copied.push(text); return true; } };
  h.setRequest(call => response(call, [result("one")]));
  await h.select(); const [item] = h.render([message("one")]); await h.tick();
  const lines = item.querySelectorAll(".message-round-line");
  await lines[1].querySelector(".message-analysis-copy").emit("click"); await flush();
  assert.deepEqual(copied, ["周六下午？"]);
  await item.querySelectorAll(".message-round-tab")[2].emit("click");
  assert.deepEqual(texts(item, ".message-round-bubble"), ["哪家店呀", "远不远"]);
  assert.equal(item.querySelectorAll(".message-round-tab")[2].attributes["aria-selected"], "true");
  const input = h.elements.chatInput;
  await item.querySelector(".message-round-draft").emit("click");
  assert.equal(input.value, "哪家店呀\n远不远"); assert.equal(input.events.length, 1);
  assert.equal(h.calls.filter(call => call.options.method === "POST").length, 0, "copy and draft never call the plugin");
});

test("a focused tab or 收起 never holds back the redraw it asked for; 收起 stays on the title line", async () => {
  const h = harness(); h.setRequest(call => response(call, [result("one")]));
  await h.select(); const [item] = h.render([message("one")]); await h.tick();
  const head = item.querySelector(".message-round-head");
  assert.deepEqual(texts(head, "button"), ["收起"], "the tabs are not in the title line");
  const tab = item.querySelectorAll(".message-round-tab")[1];
  h.document.activeElement = tab; await tab.emit("click");
  assert.deepEqual(texts(item, ".message-round-bubble"), ["书怎么样"], "switching plans redraws at once");
  const collapse = button(item, "收起");
  h.document.activeElement = collapse; await collapse.emit("click");
  assert.equal(item.querySelector(".message-round-bubble"), null, "收起 works while it keeps focus");
  const expand = item.querySelector(".message-round-toggle");
  h.document.activeElement = expand; await expand.emit("click");
  assert.deepEqual(texts(item, ".message-round-bubble"), ["书怎么样"]);
});

test("the background status read never dims 换一批; a click aborts the read and submits", async () => {
  const h = harness(); h.setRequest(call => response(call, [result("one")]));
  await h.select(); const [item] = h.render([message("one")]); await h.tick();
  const wait = deferred(); h.setRequest(() => wait.promise); await h.tick();
  const read = h.calls.at(-1); assert.equal(read.options.method, "GET");
  const retry = button(item, "换一批");
  assert.equal(retry.disabled, false, "a poll in flight leaves the button alone");
  h.setRequest(call => response(call, [{ ...result("one"), suggestionsState: "running" }]));
  await retry.emit("click"); await flush();
  assert.equal(read.options.signal.aborted, true);
  assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", messageId: "one", part: "suggestions", expectedVersion: "one-v1" });
});

test("a typed draft is replaced only after a second click", async () => {
  const h = harness(); h.setRequest(call => response(call, [result("one")]));
  await h.select(); const [item] = h.render([message("one")]); await h.tick();
  const input = h.elements.chatInput; input.value = "我自己写的";
  const draft = item.querySelector(".message-round-draft");
  await draft.emit("click");
  assert.equal(input.value, "我自己写的"); assert.equal(draft.textContent, "替换草稿？");
  await draft.emit("click");
  assert.equal(input.value, "可以啊\n周六下午？");
});

test("while the friend may still be typing the latest round waits; the switch keeps the plain toolbar look", async () => {
  const h = harness(); h.setRequest(call => response(call, [], true, { roundDueInMs: 12000 }));
  await h.select(); const [item] = h.render([message("one")]); await h.tick();
  assert.match(item.querySelector(".message-round-replies").textContent, /等对方说完/);
  assert.equal(h.toggle.attributes["aria-busy"], undefined, "no extra busy marker on the toolbar button");
  assert.equal(item.querySelector(".message-analysis-generate"), null, "no manual start while the round may still grow");
});

test("an older round without results starts only on an explicit click and cannot submit twice", async () => {
  const h = harness(); await h.select();
  const [, older] = h.render([message("first"), message("second"), message("mine", "嗯", "self")]); await h.tick();
  await older.querySelector(".message-round-toggle").emit("click");
  const start = older.querySelector(".message-analysis-generate");
  assert.equal(start.textContent, "生成分析与建议");
  assert.equal(h.calls.some(call => call.options.method === "POST"), false);
  const wait = deferred(); h.setRequest(() => wait.promise);
  await start.emit("click");
  const submitted = h.calls.at(-1);
  assert.deepEqual(submitted.body, { account: "account-a", user: "friend-a", messageId: "second" });
  await older.querySelector(".message-round-replies").querySelectorAll("button").at(-1)?.emit("click");
  assert.equal(h.calls.filter(call => call.url.endsWith("/generate")).length, 1);
  wait.resolve(response(submitted, [result("second", "pending", ["first", "second"])])); await flush();
  assert.match(older.querySelector(".message-round-replies").textContent, /排队中/);
});

test("the round explanation stays collapsed, text-only, and failed parts retry only that part", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]); await h.tick();
  h.setRequest(call => response(call, [result("one")])); await h.tick();
  const details = item.querySelector("details");
  assert.equal(details.open, false);
  assert.equal(details.querySelector(".message-round-card-title").textContent, "本轮解读");
  assert.equal(details.querySelector(".message-round-gist").textContent, "约周末，也想聊书。", "the summary shows while collapsed");
  assert.deepEqual(texts(details, ".message-round-field-label"), ["意思", "还不确定", "回复方向"]);
  assert.match(details.textContent, /约周末/);
  assert.doesNotMatch(details.textContent, /参考背景|合成摘录/, "the card carries no separate evidence list");
  assert.equal(item.querySelectorAll("img").length, 0);
  const value = { ...result("one"), state: "partial", suggestionsState: "error", suggestionsError: "模型服务暂时不可用" };
  delete value.replies;
  h.setRequest(call => response(call, [value])); await h.tick();
  assert.match(item.querySelector(".message-round-replies").textContent, /回复建议生成失败/);
  await item.querySelector(".message-round-replies").querySelector(".message-analysis-retry").emit("click"); await flush();
  assert.deepEqual(h.calls.at(-1).body, { account: "account-a", user: "friend-a", messageId: "one", part: "suggestions", expectedVersion: "one-v1" });
});

test("saved single-text replies from before rounds still render as one-line plans", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]); await h.tick();
  const value = result("one"); value.replies = value.replies.map(({ label, text }) => ({ label, text: text.replace("\n", "，") }));
  h.setRequest(call => response(call, [value])); await h.tick();
  assert.deepEqual(texts(item, ".message-round-bubble"), ["可以啊，周六下午？"]);
});

test("incomplete or duplicate plans never render as usable candidates", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  for (const replies of [result("one").replies.slice(0, 2), result("one").replies.map(() => ({ label: "同", text: "同", messages: ["同"] })),
    result("one").replies.map((reply, index) => index ? reply : { ...reply, messages: ["", "x"] })]) {
    h.setRequest(call => response(call, [{ ...result("one"), replies }])); await h.tick();
    assert.equal(item.querySelector(".message-round-bubble"), null);
  }
});

test("late responses after switching account, user, or clearing cannot render or restart polling", async () => {
  for (const target of [["account-b", "friend-a"], ["account-a", "friend-b"], null]) {
    const h = harness(), wait = deferred();
    await h.select(); const [old] = h.render([message("one")]);
    h.setRequest(() => wait.promise); h.ui.recover();
    const oldCall = h.calls.at(-1);
    h.setRequest(call => response(call, [], false));
    if (target) await h.select(...target); else h.ui.reset();
    wait.resolve(response(oldCall, [result("one")])); await flush();
    assert.equal(old.querySelector(".message-round-bubble"), null);
    assert.equal(h.toggle.attributes["aria-checked"], "false"); assert.equal(h.timers.size, 0);
    assert.equal(oldCall.options.signal.aborted, true);
  }
});

test("group and empty selections have no control, requests, or round hooks", async () => {
  const h = harness();
  for (const [account, user, group] of [["account-a", "room@chatroom", false], ["account-a", "room", true], [null, null, false]]) {
    await h.select(account, user, group);
    const [item] = h.render([message("one")], true);
    assert.equal(block(item), null); assert.equal(h.calls.length, 0);
  }
});

test("polling never sends message text and suspends while hidden", async () => {
  const h = harness(); await h.select(); h.render([message("one", "private-source-text")]); await h.tick();
  h.render([message("two", "more-private-source-text")]); await h.tick();
  assert.deepEqual(requestedIds(h.calls.at(-1)), ["two"]);
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

test("replacing message content invalidates its result and rejects an older pending response", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one", "旧文本")]); await h.tick();
  h.setRequest(call => response(call, [result("one")])); await h.tick();
  assert.ok(item.querySelector(".message-round-bubble"));
  const wait = deferred(); h.setRequest(() => wait.promise); h.ui.recover(); const call = h.calls.at(-1);
  const [edited] = h.render([message("one", "新文本")], true);
  h.setRequest(next => response(next, []));
  wait.resolve(response(call, [result("one")])); await flush();
  assert.equal(edited.querySelector(".message-round-bubble"), null);
});

test("legacy intent and emotion labels hide only while message analysis is enabled and retain their nodes", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  const labels = h.document.createElement("div"); labels.className = "inline-intent-row"; labels.textContent = "合成旧标签";
  item.querySelector(".msg-content-wrap").appendChild(labels);
  assert.equal(item.classList.contains("has-message-analysis"), true);
  await h.toggle.emit("click"); await flush();
  assert.equal(item.classList.contains("has-message-analysis"), false);
  assert.equal(item.querySelector(".inline-intent-row"), labels);
  const css = readFileSync(path.join(__dirname, "../chatui/message-analysis.css"), "utf8");
  for (const selector of ["inline-intent-row", "inline-expression-row", "inline-intent-pending", "msg-mood"]) assert.ok(css.includes(`.has-message-analysis .${selector}`));
});

test("legacy analysis remains readable without unified consent", async () => {
  const h = harness();
  h.setRequest(call => ({ ok: true, json: async () => ({ account: "account-a", user: "friend-a", enabled: false, analysisEnabled: false, suggestionsEnabled: false, migrationRequired: true,
    legacy: { analysisEnabled: true, suggestionsEnabled: false }, items: [result("one")] }) }));
  await h.select(); const [item] = h.render([message("one")]); await h.tick();
  assert.match(item.querySelector("details").textContent, /约周末/);
  assert.equal(item.querySelector(".message-round-bubble"), null, "suggestions need the unified switch");
});

test("disabling and re-enabling the provider plugin removes and restores the slot for the open chat", async () => {
  const h = harness(); await h.select(); const [item] = h.render([message("one")]);
  assert.ok(block(item));
  h.setPluginEnabled(false); await h.ui.pluginsChanged(); await flush();
  const [again] = h.render([message("one")], true);
  assert.equal(block(again), null); assert.equal(h.toggle.hidden, true);
  h.setPluginEnabled(true); await h.ui.pluginsChanged(); await flush();
  const [restored] = h.render([message("one")], true);
  assert.ok(block(restored));
});

test("bubbles rendered while the provider plugin is still being discovered get their round once it is known", async () => {
  const h = harness(); const selecting = h.ui.setScope("account-a", "friend-a");
  const [item] = h.render([message("one")]);
  await selecting; await flush(); h.ui.sync([message("one")]);
  assert.ok(block(item));
});
