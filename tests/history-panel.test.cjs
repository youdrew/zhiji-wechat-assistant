// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic history browser coverage; never reads a real account, chat or media file.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/history-panel.js"), "utf8");
const scope = { account: "synthetic-account", user: "synthetic-friend", name: "合成朋友" };
const settle = () => new Promise(setImmediate);
const message = (id, text = `synthetic ${id}`) => ({ id, text, side: "other", senderName: "合成朋友",
  time: Date.UTC(2026, 9, 14, 4), historyCursor: `cursor-${id}` });
const response = (items = [message("m1")], changes = {}) => ({ ...scope,
  messages: items, hasMore: false, nextCursor: null, ...changes });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(options = {}) {
  const requests = [], mediaRenders = [], locates = [], timers = new Map(), documentListeners = new Map();
  let timerSerial = 0, resetCount = 0;
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
      this.attributes = {}; this.listeners = new Map(); this.className = "";
      this.dataset = {}; this.style = {}; this.hidden = false; this.disabled = false;
      this.value = ""; this._text = ""; this.tabIndex = 0; this.scrollTop = 0;
      this.classList = {
        contains: name => this.className.split(/\s+/).includes(name),
        add: name => { if (!this.classList.contains(name)) this.className = `${this.className} ${name}`.trim(); },
        remove: name => { this.className = this.className.split(/\s+/).filter(item => item !== name).join(" "); },
        toggle: (name, force) => { const add = force ?? !this.classList.contains(name);
          this.classList[add ? "add" : "remove"](name); return add; },
      };
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get isConnected() { return this === body || !!this.parent?.isConnected; }
    append(...items) { for (const item of items) this.appendChild(item); }
    appendChild(item) { item.remove(); this.children.push(item); item.parent = this; return item; }
    replaceChildren(...items) { for (const child of this.children) child.parent = null;
      this.children = []; this._text = ""; this.append(...items); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
    contains(item) { return this === item || this.children.some(child => child.contains(item)); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, callback) { if (!this.listeners.has(name)) this.listeners.set(name, []);
      this.listeners.get(name).push(callback); }
    removeEventListener(name, callback) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== callback)); }
    emit(name, properties = {}) {
      const event = { target: this, prevented: false, preventDefault() { this.prevented = true; },
        stopPropagation() {}, ...properties };
      for (const callback of this.listeners.get(name) || []) callback(event);
      return event;
    }
    click() { if (!this.disabled) this.emit("click"); }
    focus() { env.activeElement = this; }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
      const match = item => {
        if (selector === "*") return true;
        if (selector.startsWith(".")) return item.classList.contains(selector.slice(1));
        const attr = selector.match(/^\[([^=]+)="([^"]*)"\]$/);
        if (attr) return item.getAttribute(attr[1]) === attr[2];
        return item.tagName === selector.toUpperCase();
      };
      return this.children.flatMap(child => [child, ...child.querySelectorAll("*")]).filter(match);
    }
  }
  const env = { activeElement: null };
  const body = new Element("body"), container = new Element("main"); body.append(container);
  const document = { body, createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag),
    addEventListener(name, callback) { if (!documentListeners.has(name)) documentListeners.set(name, []);
      documentListeners.get(name).push(callback); },
    removeEventListener(name, callback) { documentListeners.set(name, (documentListeners.get(name) || []).filter(item => item !== callback)); },
  };
  const window = { location: { href: "http://127.0.0.1:32100/", hash: "" },
    ChatMedia: { reset: () => { resetCount++; }, render: (item, captured, current) => {
      mediaRenders.push({ item, captured, current }); return null;
    } } };
  // A standalone history window bootstraps itself from the desktop host context.
  const channels = [], standalone = {};
  if (options.standalone) {
    body.classList.add("history-window");
    document.getElementById = id => id === "historyRoot" ? container : null;
    Object.assign(window, { addEventListener() {}, historyHost: { getContext: async () => ({ ...scope, theme: "dark" }),
      locate: async target => { locates.push(target); return true; }, onHostEvent: () => () => {} } });
    const accountId = require("node:crypto").createHash("sha256").update(scope.account).digest("hex");
    Object.assign(standalone, { TextEncoder,
      crypto: { subtle: { digest: async (_name, data) => require("node:crypto").createHash("sha256").update(data).digest() } },
      BroadcastChannel: class { constructor(name) { this.name = name; this.listeners = []; channels.push(this); }
        addEventListener(_type, callback) { this.listeners.push(callback); } close() {} },
      fetch: async (url, init = {}) => {
        const target = new URL(url, window.location.href);
        if (target.pathname === "/api/accounts") return { ok: true, json: async () => ({ currentAccountId: accountId }) };
        requests.push({ url: target, signal: init.signal });
        const value = await responder({ url: target, signal: init.signal }, requests.length);
        return { ok: true, json: async () => value };
      } });
  }
  const context = vm.createContext({ window, document, URL, URLSearchParams, AbortController, Date,
    setTimeout: (callback, delay) => { const id = ++timerSerial; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id), ...standalone });
  vm.runInContext(source, context);
  let responder = options.response || (() => response());
  const api = async (url, _options, signal) => {
    const request = { url: new URL(url, window.location.href), signal }; requests.push(request);
    return responder(request, requests.length);
  };
  const browser = options.standalone ? null : window.ChatHistory.create({ container, scope: { ...scope }, api,
    onLocate: target => locates.push(target), onClose: () => { env.closed = true; },
    ...(options.validateScope ? { validateScope: options.validateScope } : {}) });
  const root = () => container.querySelector(".history-browser");
  const find = selector => container.querySelector(selector);
  const all = selector => container.querySelectorAll(selector);
  const control = label => all("button").find(node => node.textContent === label);
  return { browser, requests, mediaRenders, locates, timers, container, body, documentListeners, root, find, all, control, channels,
    get resetCount() { return resetCount; }, get activeElement() { return env.activeElement; },
    setResponse: callback => { responder = callback; },
    runTimers: () => { for (const [id, timer] of [...timers]) { timers.delete(id); timer.callback(); } },
    category: key => all(".history-tab").find(node => node.dataset.category === key),
  };
}

test("opens with current conversation history and all requested categories without touching a draft", async () => {
  const h = harness(); await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url.pathname, "/api/history/search");
  assert.deepEqual(Object.fromEntries(h.requests[0].url.searchParams),
    { account: scope.account, user: scope.user, category: "all", limit: "50" });
  assert.deepEqual(h.all(".history-tab").map(node => node.textContent), ["全部", "文件", "媒体", "链接", "音乐", "小程序", "视频号"]);
  assert.equal(h.find(".history-records").children.length, 1);
  assert.equal(h.control("上一页").hidden, true); assert.equal(h.control("下一页").hidden, true);
  assert.equal(h.find("textarea"), null);
  assert.match(h.find("h1").textContent, /合成朋友/);
});

test("category changes abort pending work and discard late results from the previous filter", async () => {
  const old = deferred();
  const h = harness({ response: (_request, count) => count === 1 ? old.promise : response([message("media")]) });
  const signal = h.requests[0].signal;
  h.category("media").click(); await settle();
  assert.equal(signal.aborted, true);
  assert.equal(h.requests[1].url.searchParams.get("category"), "media");
  assert.equal(h.category("media").getAttribute("aria-selected"), "true");
  old.resolve(response([message("stale")])); await settle();
  assert.deepEqual(h.all(".history-record").map(node => node.dataset.messageId), ["media"]);
  assert.equal(h.find(".history-records").getAttribute("aria-busy"), "false");
});

test("sparse category work is bounded and continues from raw scan progress", async () => {
  const h = harness({ response: (_request, count) => response([], { hasMore: true, nextCursor: `scan-${count}` }) });
  await settle();
  assert.equal(h.requests.length, 4, "an empty category cannot read the entire history in one action");
  assert.deepEqual(h.requests.map(item => item.url.searchParams.get("before")), [null, "scan-1", "scan-2", "scan-3"]);
  assert.match(h.find(".history-status").textContent, /继续查找/);
  h.setResponse(() => response([message("found")])); h.control("继续查找").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("before"), "scan-4");
  assert.equal(h.find(".history-records").children.length, 1);
  assert.equal(h.control("上一页").hidden, false);
});

test("a non-advancing cursor fails visibly and does not repeat an unbounded request", async () => {
  const h = harness({ response: () => response([], { hasMore: true, nextCursor: "same-cursor" }) });
  await settle();
  assert.equal(h.requests.length, 2);
  assert.match(h.find(".history-status").textContent, /失败/);
  assert.equal(h.control("重试").hidden, false);
  assert.equal(h.find(".history-records").children.length, 0);
});

test("next and previous pages retain cursor positions and a new category restarts at the newest record", async () => {
  const h = harness({ response: request => request.url.searchParams.has("before") ? response([message("older")]) :
    response([message("newest")], { hasMore: true, nextCursor: "next-position" }) });
  await settle(); h.control("下一页").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("before"), "next-position");
  assert.equal(h.find(".history-record").dataset.messageId, "older");
  h.control("上一页").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("before"), null);
  assert.equal(h.find(".history-record").dataset.messageId, "newest");
  h.control("下一页").click(); await settle(); h.category("files").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("category"), "files");
  assert.equal(h.requests.at(-1).url.searchParams.get("before"), null);
  assert.equal(h.control("上一页").hidden, true);
});

test("keyword searches are debounced, trimmed and retained across category and date filters", async () => {
  const h = harness(); await settle();
  const keyword = h.find("input"); keyword.value = "  合成关键词  "; keyword.emit("input"); keyword.emit("input");
  assert.equal(h.timers.size, 1); assert.equal(h.requests.length, 1);
  h.runTimers(); await settle();
  assert.equal(h.requests.length, 2); assert.equal(h.requests.at(-1).url.searchParams.get("q"), "合成关键词");
  h.category("links").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("q"), "合成关键词");
  assert.equal(h.requests.at(-1).url.searchParams.get("category"), "links");
  h.find(".history-search").emit("submit"); await settle();
  assert.equal(h.requests.length, 4, "submitting one search makes one request");
});

test("calendar cancel preserves the applied date while apply and clear update the query", async () => {
  const h = harness(); await settle();
  h.find(".history-date-toggle").click();
  const selected = h.all(".history-day").find(node => node.textContent === "12");
  selected.click(); h.control("确定").click(); await settle();
  const day = h.requests.at(-1).url.searchParams.get("date");
  assert.match(day, /^\d{4}-\d{2}-12$/);
  assert.equal(h.find(".history-active-date").hidden, false);
  h.find(".history-date-toggle").click();
  h.all(".history-day").find(node => node.textContent === "13").click();
  h.find(".history-calendar-actions").querySelectorAll("button").find(node => node.textContent === "取消").click();
  assert.equal(h.requests.length, 2, "canceling an unapplied selection does not fetch");
  h.category("music").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("date"), day);
  h.find(".history-active-date").querySelector("button").click(); await settle();
  assert.equal(h.requests.at(-1).url.searchParams.get("date"), null);
  assert.equal(h.find(".history-active-date").hidden, true);
});

test("tabs support keyboard movement and Escape closes the calendar without closing history", async () => {
  const h = harness(); await settle();
  const event = h.find(".history-tabs").emit("keydown", { target: h.category("all"), key: "ArrowRight" });
  await settle();
  assert.equal(event.prevented, true);
  assert.equal(h.category("files").getAttribute("aria-selected"), "true");
  assert.equal(h.activeElement, h.category("files"));
  h.find(".history-date-toggle").click();
  h.root().emit("keydown", { target: h.find(".history-calendar"), key: "Escape" });
  assert.equal(h.find(".history-calendar").hidden, true);
  assert.equal(h.activeElement, h.find(".history-date-toggle"));
  assert.ok(h.root());
});

test("Enter on calendar day and cancel buttons preserves their native actions", async () => {
  const h = harness(); await settle(); h.find(".history-date-toggle").click();
  const day = h.all(".history-day").find(node => node.textContent === "12");
  const selectEvent = h.root().emit("keydown", { target: day, key: "Enter" });
  assert.equal(selectEvent.prevented, false, "Enter should activate the focused day instead of applying a previous date");
  day.click(); assert.equal(h.find(".history-calendar").hidden, false);
  const cancel = h.find(".history-calendar-actions").querySelectorAll("button").find(node => node.textContent === "取消");
  const cancelEvent = h.root().emit("keydown", { target: cancel, key: "Enter" });
  assert.equal(cancelEvent.prevented, false, "the cancel button retains its native activation");
  cancel.click(); await settle();
  assert.equal(h.find(".history-calendar").hidden, true);
  assert.equal(h.requests.length, 1);
});

test("location passes fixed identity and an original-message cursor without changing local records", async () => {
  const h = harness(); await settle(); h.find(".history-locate").click();
  assert.deepEqual(JSON.parse(JSON.stringify(h.locates)), [{ account: scope.account, user: scope.user,
    cursor: "cursor-m1", messageId: "m1" }]);
  assert.equal(h.find(".history-record").dataset.messageId, "m1");
  h.browser.suspend(); h.find(".history-records").click();
  assert.equal(h.locates.length, 1);
});

test("untrusted rich content remains text and avatar schemes never execute", async () => {
  const content = '<img src=x onerror="execute()"> & <script>execute()</script>';
  const item = { ...message("unsafe", content), senderAvatar: "javascript:execute()",
    senderAvatarCandidates: ["data:text/html,unsafe", "file:///private.png", "https://u:p@invalid.test/avatar.png"] };
  const h = harness({ response: () => response([item]) }); await settle();
  assert.equal(h.find(".history-content").textContent, content);
  assert.equal(h.all("img").length, 0); assert.equal(h.all("script").length, 0);
  assert.equal(h.mediaRenders.length, 1);
  assert.equal(h.mediaRenders[0].captured.account, scope.account);
  assert.equal(h.mediaRenders[0].captured.user, scope.user);
});

test("a response from another account or conversation is rejected before rendering", async () => {
  for (const changes of [{ account: "other-account" }, { user: "other-friend" }]) {
    const h = harness({ response: () => response([message("foreign")], changes) }); await settle();
    assert.equal(h.find(".history-records").children.length, 0);
    assert.equal(h.mediaRenders.length, 0); assert.equal(h.locates.length, 0);
    assert.match(h.find(".history-status").textContent, /失败|变化|隐藏/);
  }
});

test("a changed-account response on a later page hides already-rendered old records", async () => {
  const h = harness({ response: (_request, count) => count === 1 ?
    response([message("old-account")], { hasMore: true, nextCursor: "older" }) :
    response([message("foreign-account")], { account: "other-account" }) });
  await settle(); assert.equal(h.find(".history-records").children.length, 1);
  h.control("下一页").click(); await settle();
  assert.equal(h.find(".history-records").children.length, 0);
  assert.equal(h.find("input").disabled, true);
  assert.match(h.find(".history-status").textContent, /隐藏/);
});

test("paging and cancellation stop media immediately even while old records remain displayed", async () => {
  const h = harness({ response: () => response([message("playing")], { hasMore: true, nextCursor: "older" }) });
  await settle(); const resets = h.resetCount, oldMedia = h.mediaRenders[0], waiting = deferred();
  h.setResponse(() => waiting.promise); h.control("下一页").click();
  assert.ok(h.resetCount > resets, "starting a page must stop voice/video before its network response");
  assert.equal(h.find(".history-records").children.length, 1);
  assert.equal(oldMedia.current(oldMedia.captured), false);
  const during = h.resetCount;
  h.find(".history-progress").querySelectorAll("button").find(node => node.textContent === "取消").click();
  assert.ok(h.resetCount > during);
  waiting.resolve(response([message("late")])); await settle();
  assert.equal(h.find(".history-record").dataset.messageId, "playing");
});

test("account boundaries abort work, clear records and prevent late responses or gestures", async () => {
  const h = harness(); await settle();
  const rendered = h.mediaRenders[0]; assert.equal(rendered.current(rendered.captured), true);
  const old = deferred(); h.setResponse(() => old.promise); h.browser.refresh();
  const request = h.requests.at(-1); h.browser.suspend();
  assert.equal(request.signal.aborted, true);
  assert.equal(h.find(".history-records").children.length, 0);
  assert.equal(rendered.current(rendered.captured), false);
  assert.equal(h.find("input").disabled, true);
  assert.equal(h.find(".history-date-toggle").disabled, true);
  const count = h.requests.length; h.category("media").click();
  assert.equal(h.requests.length, count);
  old.resolve(response([message("late")])); await settle();
  assert.equal(h.find(".history-records").children.length, 0);
  assert.match(h.find(".history-status").textContent, /隐藏/);
});

test("account validation happens before and after a fetch, and only verified reopening can resume", async () => {
  let valid = true, checks = 0;
  const h = harness({ validateScope: async () => { checks++; return valid; } }); await settle();
  assert.equal(checks, 2);
  h.browser.suspend(); valid = false; await h.browser.refresh(); await settle();
  assert.equal(h.requests.length, 1); assert.equal(h.find("input").disabled, true);
  valid = true; await h.browser.refresh(); await settle();
  assert.equal(h.requests.length, 2); assert.equal(h.find("input").disabled, false);
  assert.equal(h.find(".history-records").children.length, 1);
  h.setResponse(() => { valid = false; return response([message("account-changed-mid-flight")]); });
  await h.browser.refresh(); await settle();
  assert.equal(h.find(".history-records").children.length, 0);
  assert.equal(h.find("input").disabled, true);
});

test("a standalone window keeps records while the main window re-reads, and hides them for another account", async () => {
  const h = harness({ standalone: true });
  for (let round = 0; round < 20 && !h.all(".history-record").length; round++) await settle();
  assert.equal(h.all(".history-record").length, 1);
  const [channel] = h.channels; assert.equal(channel.name, "kyf-friend-analysis");
  const post = data => channel.listeners.forEach(listener => listener({ data }));
  post({ type: "account", value: null });
  assert.equal(h.all(".history-record").length, 1);
  assert.equal(h.find("input").disabled, false);
  post({ type: "account", value: scope.account });
  assert.equal(h.all(".history-record").length, 1);
  post({ type: "account", value: "another-account" });
  assert.equal(h.all(".history-record").length, 0);
  assert.equal(h.find("input").disabled, true);
});

test("cancel leaves a visible retry and destroy releases work, media and document listeners", async () => {
  const waiting = deferred(), h = harness({ response: () => waiting.promise });
  const request = h.requests[0]; h.find(".history-progress").querySelectorAll("button").find(node => node.textContent === "取消").click();
  assert.equal(request.signal.aborted, true); assert.match(h.find(".history-status").textContent, /取消/);
  assert.equal(h.control("重试").hidden, false);
  h.browser.destroy();
  assert.equal(h.container.children.length, 0);
  assert.equal((h.documentListeners.get("pointerdown") || []).length, 0);
  assert.equal(h.timers.size, 0); assert.ok(h.resetCount > 0);
  waiting.resolve(response([message("late-after-destroy")])); await settle();
  assert.equal(h.container.children.length, 0);
});

function mainHarness({ desktop = true, opened = true, confirm = true } = {}) {
  const app = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
  const section = (start, end) => {
    const first = app.indexOf(start), last = app.indexOf(end, first);
    assert.ok(first >= 0 && last > first, `Missing main integration section: ${start}`);
    return app.slice(first, last);
  };
  const nodes = new Map(), env = { opens: [], toasts: [], creates: [], locates: [], switches: [], confirms: [], destroys: 0 };
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, { value: "合成未复制草稿", open: false, attributes: {},
      setAttribute(key, value) { this.attributes[key] = value; },
      showModal() { this.open = true; }, close() { this.open = false; } });
    return nodes.get(id);
  };
  const state = { ...scope, currentAccount: scope.account, currentUser: scope.user,
    messageSourceReady: true, view: "plugin:synthetic/page", self: { name: "合成自己" },
    sessions: new Map([[scope.user, { name: scope.name }], ["other-friend", { name: "合成朋友乙" }]]) };
  const window = { desktopHost: desktop ? { openHistoryWindow: async value => { env.opens.push(value); return opened; } } : {},
    confirm: value => { env.confirms.push(value); return confirm; },
    ChatHistory: { create: options => { env.creates.push(options);
      return { cancel() {}, destroy() { env.destroys++; }, focus() { env.focused = true; } }; } } };
  const context = vm.createContext({ window, byId, chatState: state, settingsState: { settings: { theme: "light" } },
    api() {}, toast: value => env.toasts.push(value),
    switchView: value => { state.view = value; },
    switchSession: value => { env.switches.push(value); state.currentUser = value; },
    jumpToHistory: async (...args) => env.locates.push(args) });
  vm.runInContext(section("let historyBrowser =", "async function loadMessages(") +
    section("async function locateFromFriend(", "function switchView(") +
    "globalThis.historyEntry = { openHistorySearch, closeHistorySearch, locateFromFriend };", context);
  return { env, state, byId, entry: context.historyEntry };
}

test("desktop history opens independently with fixed identity, keeping the draft and selected main view", async () => {
  const h = mainHarness(); await h.entry.openHistorySearch();
  assert.deepEqual(JSON.parse(JSON.stringify(h.env.opens)), [{ ...scope, theme: "light" }]);
  assert.equal(h.state.view, "plugin:synthetic/page");
  assert.equal(h.byId("chatInput").value, "合成未复制草稿");
  assert.equal(h.env.creates.length, 0);
  const unavailable = mainHarness({ opened: false }); await unavailable.entry.openHistorySearch();
  assert.deepEqual(unavailable.env.toasts, ["聊天记录窗口未能打开"]);
});

test("browser history uses a separate dialog and validates the original account and conversation", async () => {
  const h = mainHarness({ desktop: false }); await h.entry.openHistorySearch();
  const options = h.env.creates[0];
  assert.equal(h.byId("historySearchPanel").open, true);
  assert.equal(h.state.view, "plugin:synthetic/page");
  assert.equal(h.byId("chatInput").value, "合成未复制草稿");
  assert.equal(await options.validateScope(), true);
  h.state.currentUser = "other-friend";
  assert.equal(await options.validateScope(), false);
  h.state.currentUser = scope.user;
  h.state.currentAccount = "another-account";
  assert.equal(await options.validateScope(), false);
  h.entry.closeHistorySearch();
  assert.equal(h.byId("historySearchPanel").open, false);
  assert.equal(h.env.destroys, 1);
});

test("history location refuses a changed account and delegates draft-preserving contact switches", async () => {
  const h = mainHarness({ confirm: false });
  const target = { account: scope.account, user: "other-friend", cursor: "synthetic-cursor", messageId: "synthetic-message" };
  await h.entry.locateFromFriend({ ...target, account: "other-account" });
  assert.equal(h.env.locates.length, 0);
  await h.entry.locateFromFriend(target);
  assert.equal(h.env.confirms.length, 0);
  assert.equal(h.env.switches.length, 1); assert.equal(h.env.locates.length, 1);
  await h.entry.locateFromFriend({ ...target, user: scope.user });
  assert.deepEqual(JSON.parse(JSON.stringify(h.env.locates)), [["synthetic-cursor", "synthetic-message"], ["synthetic-cursor", "synthetic-message"]]);
  assert.equal(h.env.confirms.length, 0, "contact positioning relies on isolated composer drafts without asking");
  assert.equal(h.byId("chatInput").value, "合成未复制草稿");
});
