const assert = require("node:assert/strict");
// Modified by Know Your Friends, 2026-10-09: synthetic automatic-suggestion isolation coverage.
// Moved from tests/advisor-ui.test.cjs to plugins/kyf.advisor/tests/ui.test.cjs on 2026-10-13 (kyf.advisor plugin):
// the 聊天助手 panel (panel-controller.js + panel-frame.js) and 助手仓库 (repository.js) are sandboxed plugin pages
// tested with a fake kyfHost; RPC methods replace /api/advisor/*. The retired automatic replies are gone.
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const root = path.join(__dirname, "../ui");
const hostSource = readFileSync(path.join(root, "panel-controller.js"), "utf8");
const frameSource = readFileSync(path.join(root, "panel-frame.js"), "utf8");
const repositorySource = readFileSync(path.join(root, "repository.js"), "utf8");
const frameHtml = readFileSync(path.join(root, "panel.html"), "utf8");
const repositoryHtml = readFileSync(path.join(root, "repository.html"), "utf8");
const json = value => JSON.parse(JSON.stringify(value));
const descendantText = element => [element.textContent, ...element.children.map(descendantText)].join("\n");
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
// A failed plugin call: kyfHost rejects with the plugin's error code and message.
const fail = (message, code = "error") => Promise.reject(Object.assign(new Error(message), { code }));
function node(tag = "div", id = "") {
  const element = { tag, id, className: "", textContent: "", value: "", style: {}, hidden: false,
    disabled: false, dataset: {}, children: [], listeners: {}, attributes: {}, parentNode: null,
    scrollTop: 0, scrollHeight: 200, clientHeight: 180, offsetWidth: 360, moves: 0 };
  element.classList = {
    contains: name => element.className.split(/\s+/).includes(name),
    add: name => { if (!element.classList.contains(name)) element.className += " " + name; },
    remove: name => { element.className = element.className.split(/\s+/).filter(item => item !== name).join(" "); },
    toggle: (name, enabled) => enabled ? element.classList.add(name) : element.classList.remove(name)
  };
  element.setAttribute = (name, value) => { element.attributes[name] = value; };
  element.addEventListener = (name, handler) => { (element.listeners[name] ||= []).push(handler); };
  element.appendChild = child => {
    element.moves++;
    child.remove(); child.parentNode = element; element.children.push(child); return child;
  };
  element.insertBefore = (child, reference) => {
    if (!reference) return element.appendChild(child);
    element.moves++;
    child.remove(); child.parentNode = element;
    const index = element.children.indexOf(reference);
    element.children.splice(index < 0 ? element.children.length : index, 0, child); return child;
  };
  element.remove = () => {
    if (element.parentNode) element.parentNode.children = element.parentNode.children.filter(child => child !== element);
    element.parentNode = null;
  };
  element.replaceChildren = (...children) => {
    for (const child of [...element.children]) child.remove();
    for (const child of children) element.appendChild(child);
  };
  element.dispatch = (name, event = {}) => Promise.all((element.listeners[name] || []).map(handler => handler(event)));
  element.focus = () => { element.focused = true; };
  element.select = () => {};
  element.showModal = () => { element.open = true; };
  element.close = () => { element.open = false; };
  return element;
}
// One sandboxed page with a fake kyfHost: plugin RPC goes to env.rpc, host methods to env.host.
function environment(ids) {
  const elements = new Map(ids.map(id => [id, node("div", id)]));
  const listeners = {}, messages = [], timers = new Map(), rpcCalls = [], hostCalls = [];
  let nextTimer = 0;
  const document = { body: node("body"), readyState: "complete", getElementById: id => elements.get(id) || null,
    createElement: tag => node(tag), createTextNode: text => { const result = node("text"); result.textContent = text; return result; },
    addEventListener() {} };
  const env = { elements, listeners, messages, timers, rpcCalls, hostCalls };
  env.rpc = async () => ({});
  env.host = async () => ({ copied: true });
  const kyfHost = {
    rpc(method, params, scope) {
      rpcCalls.push({ method, params: json(params || {}), ...(scope ? { scope: json(scope) } : {}) });
      return Promise.resolve().then(() => env.rpc(method, params || {}, scope));
    },
    host(method, params) { hostCalls.push([method, json(params || {})]); return Promise.resolve().then(() => env.host(method, params || {})); },
    ready: () => new Promise(() => {}), on(listener) { env.kyfListener = listener; return () => {}; }, context: () => null,
  };
  const window = { innerWidth: 1200, innerHeight: 600, crypto: webcrypto, document, kyfHost,
    addEventListener: (name, handler) => { (listeners[name] ||= []).push(handler); } };
  const context = vm.createContext({ window, document,
    setTimeout: handler => { const id = ++nextTimer; timers.set(id, handler); return id; },
    clearTimeout: id => timers.delete(id), URLSearchParams, Uint8Array, AbortController, Date });
  Object.assign(env, { window, context, document,
    async flushTimers() { const batch = [...timers.values()]; timers.clear(); for (const handler of batch) await handler(); } });
  return env;
}
function host() {
  const env = environment([]);
  env.window.AdvisorView = { receive: data => env.messages.push(json(data)) };
  vm.runInContext(hostSource, env.context);
  env.advisor = env.window.AdvisorController;
  return env;
}
function frame() {
  const env = environment([...frameHtml.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));
  env.window.AdvisorController = { receive: data => env.messages.push(json(data)) };
  vm.runInContext(frameSource, env.context);
  env.emit = data => env.window.AdvisorView.receive(json(data));
  return env;
}
// The real panel page: view and controller in one document.
function pair() {
  const env = environment([...frameHtml.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));
  vm.runInContext(frameSource, env.context);
  const view = env.window.AdvisorView;
  env.window.AdvisorView = { receive: data => { env.messages.push(json(data)); view.receive(data); } };
  vm.runInContext(hostSource, env.context);
  env.advisor = env.window.AdvisorController;
  return { hostEnv: env, frameEnv: env };
}
function catalog(enabled = true) {
  return { agents: [{ id: "advisor", name: "军师", description: "", prompt: "沟通建议", skillIds: [], enabled,
    builtin: true, revision: 1 }], skills: [], engine: { state: "available" }, permissions: { readOnly: true } };
}
function thread(account = "account-a", user = "friend-a", agentId = "advisor", messages = []) {
  return { id: `${account}-${user}-${agentId}`, account, user, agentId, messages };
}
function queue(items = [], version = 0, paused = false, reason = "") { return { items, version, paused, reason }; }
function contextContact(user, name, overrides = {}) {
  return { user, name, primary: false, sources: ["chat", "profile"], status: "ready", access: "available", ...overrides };
}
function contextSnapshot(contacts, state = "recorded") {
  return { version: 1, state, capturedAtMs: 1234,
    contacts: contacts.map((item, index) => ({ ...item, primary: index === 0, access: index === 0 ? "required" : "available" })) };
}
function messageContext(env, messageId) {
  return env.elements.get("advisorTimeline").children.find(row => row.dataset.msgId === messageId)
    ?.children.find(child => child.className === "advisor-message-context");
}
function queuedItem(params, extra = {}) {
  return { id: "item-" + params.requestId, requestId: params.requestId, message: params.message, version: 1,
    thinking: params.thinking ?? true, mentions: params.mentions || [], state: "pending", createdAtMs: 1, ...extra };
}
function enqueueAck(params, extra = {}) {
  const item = queuedItem(params);
  return { threadId: params.threadId, item, queue: queue([item], 1), ...extra };
}
function ready(env, account = "account-a", user = "friend-a") {
  const advisor = env.advisor;
  advisor.catalog = catalog(); advisor.catalogReady = true; advisor.activeAgentId = "advisor";
  advisor.currentAccount = account; advisor.currentUser = user;
  advisor.currentThreadId = thread(account, user).id;
  advisor.selectedThreads[advisor.baseKey(account, user, "advisor")] = advisor.currentThreadId;
  advisor.setCachedThread(account, user, "advisor", thread(account, user));
  return advisor;
}
function request(env, type, payload = {}, overrides = {}) {
  return { type, payload, scope: json(env.advisor.captureScope()), ...overrides };
}
const methods = env => env.rpcCalls.map(call => call.method);
test("pages load only local scripts through kyfHost and keep no browser storage or network of their own", () => {
  const scripts = html => [...html.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(scripts(frameHtml), ["/plugin-sdk/web/kyf-host.js", "advisor-ui-bundle.js", "panel-frame.js", "panel-controller.js"]);
  assert.deepEqual(scripts(repositoryHtml), ["/plugin-sdk/web/kyf-host.js", "repository.js"]);
  for (const html of [frameHtml, repositoryHtml]) {
    assert.ok(html.includes("connect-src 'none'")); assert.ok(html.includes("object-src 'none'"));
    assert.doesNotMatch(html, /<script>[^<]/); assert.doesNotMatch(html, /\son[a-z]+=/);
  }
  for (const source of [hostSource, frameSource, repositorySource]) {
    assert.ok(!/localStorage|sessionStorage|indexedDB/.test(source)); assert.ok(!/\bfetch\(/.test(source));
    assert.ok(!source.includes("/api/advisor/")); assert.ok(!source.includes(".innerHTML"));
    assert.ok(!/auto-?reply|autoReply/i.test(source), "the retired automatic replies stay retired");
  }
  assert.ok(!frameHtml.includes("contextStatusPill")); assert.ok(!frameHtml.includes("Shift+Enter"));
  assert.ok(frameSource.includes("AdvisorTimeline?.renderInto"));
  assert.ok(!hostSource.includes("context.prepare"), "opening the panel never prepares context");
});

test("catalog failure cannot create presets, enabled relationships or an available engine", async () => {
  const env = host(), advisor = env.advisor;
  env.rpc = () => fail("目录服务不可用");
  assert.equal(await advisor.loadCatalog(), false);
  assert.equal(advisor.catalogReady, false);
  assert.equal(advisor.getEnabledTemplates().length, 0);
  assert.equal(advisor.catalogError, "目录服务不可用");
  assert.equal(advisor.activeAgentId, "");
  assert.deepEqual(env.rpcCalls, [{ method: "catalog.get", params: {} }]);
});

test("plugin errors keep their message and code", async () => {
  const env = host();
  env.rpc = () => fail("当前账号已经变更", "account-paused");
  const result = await env.advisor.call("run.start", {});
  assert.equal(result.error, "当前账号已经变更"); assert.equal(result.code, "account-paused");
});

test("view requests are checked against the current scope, fields and sizes", () => {
  const env = host(), advisor = ready(env); let calls = 0;
  advisor.startRun = () => { calls++; };
  const good = request(env, "ADVISOR_SEND", { text: "建议", agentId: "advisor" });
  advisor.receive({ ...good, envelope: "advisor" });
  advisor.receive(request(env, "ADVISOR_SEND", good.payload, { scope: { ...good.scope, generation: 99 } }));
  advisor.receive(request(env, "ADVISOR_SEND", { text: "建议", agentId: "advisor", account: "other" }));
  advisor.receive(request(env, "ADVISOR_SEND", { text: 7, agentId: "advisor" }));
  advisor.receive(request(env, "ADVISOR_SEND", { text: "a".repeat(16001), agentId: "advisor" }));
  advisor.receive(request(env, "ADVISOR_SEND", { text: "建议", agentId: "advisor" }, { scope: { ...good.scope, threadId: "different-thread" } }));
  assert.equal(calls, 0); advisor.receive(good); assert.equal(calls, 1);
  assert.equal(advisor.validFrameRequest(request(env, "ADVISOR_UNKNOWN")), false);
});

test("handshake and draft requests preserve the selected identity", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.initFrameHandshake(); assert.equal(advisor.frameReady, true);
  const input = frameEnv.elements.get("advisorInput"); input.value = "这是一条草稿"; input.dispatch("input");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "这是一条草稿");
  const scope = json(advisor.captureScope());
  advisor.onSessionChanged("account-b", "friend-b");
  advisor.receive({ type: "ADVISOR_DRAFT_UPDATE", payload: { draft: "迟到草稿", agentId: "advisor" }, scope });
  assert.equal(advisor.getDraft("account-b", "friend-b", "advisor"), "");
});

test("深度思考 toggle is kept for the panel and sent with each answer", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  hostEnv.rpc = (method, params) => enqueueAck(params);
  advisor.initFrameHandshake();
  const button = frameEnv.elements.get("btnThinking");
  assert.equal(button.attributes["aria-pressed"], "true");
  await advisor.startRun("先想清楚再答");
  await button.dispatch("click");
  assert.equal(button.attributes["aria-pressed"], "false");
  assert.equal(advisor.thinkingEnabled, false);
  await advisor.startRun("直接回答");
  assert.deepEqual(hostEnv.rpcCalls.filter(call => call.method === "queue.enqueue").map(call => call.params.thinking), [true, false]);
  advisor.receive(request(hostEnv, "ADVISOR_SET_THINKING", { enabled: "yes" }));
  assert.equal(advisor.thinkingEnabled, false);
  advisor.initFrameHandshake();
  assert.equal(button.attributes["aria-pressed"], "false");
});

test("late thread responses after a switch cannot overwrite new conversation", async () => {
  const env = host(), advisor = ready(env), threadResult = deferred();
  env.rpc = () => threadResult.promise;
  const scopeA = advisor.captureScope();
  const loading = advisor.loadThread(scopeA);
  advisor.generation++; advisor.currentAccount = "account-b"; advisor.currentUser = "friend-b";
  advisor.syncThreadToFrame(thread("account-b", "friend-b"));
  const count = env.messages.length;
  threadResult.resolve({ thread: thread("account-a", "friend-a", "advisor", [{ id: "late", role: "assistant", text: "A" }]) });
  await loading;
  assert.equal(env.messages.length, count);
  assert.equal(advisor.getCachedThread("account-b", "friend-b", "advisor").messages.length, 0);
});

test("late unconfirmed enqueue cannot change the new account run or clear an old draft", async () => {
  const env = host(), advisor = ready(env), response = deferred();
  env.rpc = () => response.promise;
  const starting = advisor.startRun("未提交完的消息");
  advisor.generation++; advisor.currentAccount = "account-b"; advisor.currentUser = "friend-b";
  advisor.submitPending = false; advisor.currentRunId = "new-run";
  response.resolve({ run: { id: "old-run", threadId: thread().id, state: "running" } }); await starting;
  assert.equal(advisor.currentRunId, "new-run");
  assert.equal(advisor.pendingEnqueues[advisor.scopeKey("account-a", "friend-a", "advisor")].message, "未提交完的消息");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "未提交完的消息");
});

test("late polling cannot contaminate another agent, even under same account/user", async () => {
  const env = host(), advisor = ready(env), response = deferred();
  env.rpc = () => response.promise;
  const polling = advisor.pollEvents("run-a", advisor.captureScope());
  await tick();
  advisor.generation++; advisor.activeAgentId = "empathy"; advisor.abortCurrentPoll();
  response.resolve({ events: [{ seq: 1, type: "text", text: "旧消息", messageId: "a" }],
    run: { id: "run-a", state: "done" } }); await polling;
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "empathy").messages.length, 0);
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages.length, 0);
});

test("failed start keeps draft, never invents output, and retries the same requestId for the same friend", async () => {
  const env = host(), advisor = ready(env);
  env.rpc = () => fail("连接超时");
  await advisor.startRun("保留我的草稿"); await advisor.retryQueue();
  const starts = env.rpcCalls.filter(call => call.method === "queue.enqueue");
  assert.equal(starts.length, 2); assert.equal(starts[0].params.requestId, starts[1].params.requestId);
  assert.deepEqual(starts[0].scope, { account: "account-a", user: "friend-a" });
  assert.deepEqual(Object.keys(starts[0].params).sort(), ["agentId", "mentions", "message", "requestId", "thinking", "threadId"]);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "保留我的草稿");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages.length, 0);
  assert.equal(advisor.currentRunId, null);
  assert.ok(env.messages.some(message => message.type === "ADVISOR_QUEUE" && message.payload.error === "连接超时"));
});

test("stop is acknowledged by backend before terminal state; partial text survives failure", async () => {
  const env = host(), advisor = ready(env), response = deferred();
  advisor.setCachedThread("account-a", "friend-a", "advisor", thread("account-a", "friend-a", "advisor",
    [{ id: "partial", role: "assistant", text: "已经生成的片段" }]));
  advisor.currentRunId = "active-run"; env.rpc = () => response.promise;
  const stopping = advisor.stopRun();
  assert.equal(advisor.currentRunId, "active-run"); assert.equal(advisor.stopPending, true);
  assert.ok(!env.messages.some(message => message.payload.run?.state === "stopped"));
  response.resolve(fail("停止请求失败")); await stopping;
  assert.equal(advisor.currentRunId, "active-run");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages[0].text, "已经生成的片段");
});

test("confirmed stop preserves partial projection and terminal status", async () => {
  const env = host(), advisor = ready(env);
  advisor.setCachedThread("account-a", "friend-a", "advisor", thread("account-a", "friend-a", "advisor",
    [{ id: "partial", role: "assistant", text: "片段" }]));
  advisor.currentRunId = "active-run";
  env.rpc = method => method === "run.stop" ? { run: { id: "active-run", state: "stopped" } } : fail("读取暂时失败");
  await advisor.stopRun();
  assert.equal(advisor.currentRunId, null);
  assert.deepEqual(env.rpcCalls[0], { method: "run.stop", params: { runId: "active-run" }, scope: { account: "account-a", user: "friend-a" } });
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages[0].text, "片段");
  assert.ok(env.messages.some(message => message.payload.run?.state === "stopped"));
});

test("a detached cached run can still be explicitly stopped when reloading its thread fails", async () => {
  const env = host(), advisor = ready(env);
  const saved = thread("account-a", "friend-a", "advisor", [{ id: "partial", role: "assistant", text: "已保存的部分回复" }]);
  const run = { id: "detached-run", threadId: saved.id, state: "running" };
  saved._advisorRun = run;
  advisor.setCachedThread("account-a", "friend-a", "advisor", saved);
  advisor.syncThreadToFrame(saved, run); advisor.currentRunId = run.id;
  advisor.hideCurrentRun(); assert.equal(advisor.currentRunId, null);
  env.rpc = method => method === "run.stop" ? { run: { ...run, state: "stopped" } } : fail("Synthetic thread reload failure");
  await advisor.loadThread(); assert.equal(advisor.currentRunId, null);
  assert.equal(advisor.threadProjection.run.state, "running");
  await advisor.stopRun();
  assert.equal(methods(env).filter(name => name === "run.stop").length, 1);
  assert.equal(advisor.currentRunId, null); assert.equal(advisor.activeRunId(), null);
  assert.equal(advisor.threadProjection.run.state, "stopped");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages[0].text, "已保存的部分回复");
  advisor.onSessionChanged("account-a", "friend-b"); await tick();
  advisor.onSessionChanged("account-a", "friend-a"); await tick();
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor")._advisorRun.state, "stopped");
  assert.equal(advisor.activeRunId(), null); assert.equal(advisor.currentRunId, null);
  await advisor.stopRun();
  assert.equal(methods(env).filter(name => name === "run.stop").length, 1);
});

test("explicit stop ignores cached or projected runs from another scope, thread, or a terminal state", async () => {
  for (const invalid of ["thread", "cached-scope", "projected-account", "projected-contact", "projected-template", "done", "stopped", "error"]) {
    const env = host(), advisor = ready(env);
    const saved = thread(), run = { id: "unrelated-run", threadId: saved.id, state: "running" };
    if (invalid === "thread") {
      saved._advisorRun = { ...run, threadId: "another-thread" }; advisor.setCachedThread("account-a", "friend-a", "advisor", saved);
    } else if (invalid === "cached-scope") {
      advisor.threadCache[advisor.scopeKey("account-a", "friend-a", "advisor")] = { ...thread("account-b", "friend-b"), _advisorRun: run };
    } else if (invalid.startsWith("projected-")) {
      const scope = { ...advisor.captureScope() };
      scope[invalid === "projected-account" ? "account" : invalid === "projected-contact" ? "user" : "agentId"] = "another-scope";
      advisor.threadProjection = { scope, thread: saved, run };
    } else {
      saved._advisorRun = { ...run, state: invalid }; advisor.setCachedThread("account-a", "friend-a", "advisor", saved);
    }
    await advisor.stopRun();
    assert.equal(advisor.activeRunId(), null, invalid); assert.equal(env.rpcCalls.length, 0, invalid);
  }
});

test("retrying a detached cached run after a failed reload only reconnects event polling", async () => {
  const env = host(), advisor = ready(env);
  const saved = thread("account-a", "friend-a", "advisor", [{ id: "original", role: "user", text: "原来的问题" }]);
  const run = { id: "detached-run", threadId: saved.id, state: "running" };
  saved._advisorRun = run;
  advisor.setCachedThread("account-a", "friend-a", "advisor", saved);
  advisor.syncThreadToFrame(saved, run); advisor.currentRunId = run.id; advisor.hideCurrentRun();
  advisor.setDraft("account-a", "friend-a", "advisor", "新的未发送草稿");
  env.rpc = method => method === "run.events" ? { events: [], run: { ...run, state: "done" },
    thread: thread("account-a", "friend-a", "advisor", [...saved.messages, { id: "answer", role: "assistant", text: "后台完成的回答" }]) } :
    fail("Synthetic thread reload failure");
  await advisor.loadThread(); assert.equal(advisor.currentRunId, null);
  await advisor.startRun("不应重复发送的问题");
  await advisor.retryRun(); await tick(); await tick();
  const events = env.rpcCalls.filter(call => call.method === "run.events");
  assert.equal(events.length, 1);
  assert.deepEqual(events[0], { method: "run.events", params: { runId: run.id, after: 0 }, scope: { account: "account-a", user: "friend-a" } });
  assert.ok(env.rpcCalls.every(call => call.method !== "run.start" && call.method !== "run.stop"));
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages.at(-1).text, "后台完成的回答");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "新的未发送草稿");
  assert.equal(advisor.currentRunId, null);
});

test("account clearing erases only that account's memory, blocks late writers and calls nothing", async () => {
  const env = host(), advisor = ready(env);
  advisor.setDraft("account-a", "friend-a", "advisor", "A"); advisor.setDraft("account-b", "friend-b", "advisor", "B", "b-thread");
  advisor.setCachedThread("account-b", "friend-b", "advisor", thread("account-b", "friend-b"));
  advisor.currentRunId = "run-a";
  advisor.onEvent({ type: "account-cleared", account: "account-a" });
  advisor.setDraft("account-a", "friend-a", "advisor", "迟到写入");
  advisor.setCachedThread("account-a", "friend-a", "advisor", thread());
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "");
  assert.equal(advisor.getDraft("account-b", "friend-b", "advisor", "b-thread"), "B");
  assert.equal(Object.keys(advisor.threadCache).length, 1);
  assert.ok(!Object.keys(advisor.draftRecords).some(key => JSON.parse(key)[0] === "account-a"));
  assert.equal(advisor.currentRunId, null); assert.equal(advisor.currentAccount, "");
  await env.flushTimers(); await tick();
  assert.ok(env.rpcCalls.every(call => call.scope?.account !== "account-a"), "Core already stopped the cleared account's work");
  assert.deepEqual(methods(env), ["draft.save"], "only the other account's pending draft is still saved");
});

test("context switches follow the host; session changes do not prepare context", async () => {
  const env = host(), advisor = ready(env);
  env.rpc = (method, params, scope) => ({ thread: thread(scope.account, scope.user, params.agentId) });
  advisor.onContext({ account: "account-a", user: "friend-b", theme: "light" }); await tick();
  assert.equal(advisor.currentUser, "friend-b");
  assert.ok(env.messages.some(message => message.type === "ADVISOR_THEME" && message.payload.theme === "light"));
  assert.ok(env.rpcCalls.every(call => call.method === "thread.get"));
  assert.deepEqual(env.rpcCalls.at(-1), { method: "thread.get", params: { agentId: "advisor" }, scope: { account: "account-a", user: "friend-b" } });
});

test("a draft typed just before a friend switch is saved for that friend, not the new one", async () => {
  const env = host(), advisor = ready(env), ownId = advisor.currentThreadId;
  advisor.acceptDraft(advisor.captureScope(), { ...thread(), draft: { text: "", version: 2 } });
  env.rpc = (method, params) => method === "draft.save" ? { draft: { text: params.text, version: 3 } } : { thread: thread("account-a", "friend-b") };
  advisor.setDraft("account-a", "friend-a", "advisor", "切换前的输入", ownId);
  advisor.onSessionChanged("account-a", "friend-b"); await tick(); await tick();
  const save = env.rpcCalls.find(call => call.method === "draft.save");
  assert.deepEqual(save, { method: "draft.save", params: { agentId: "advisor", threadId: ownId, text: "切换前的输入", version: 2, scrollTop: 0 },
    scope: { account: "account-a", user: "friend-a" } });
  assert.equal(advisor.draftRecords[advisor.scopeKey("account-a", "friend-a", "advisor", ownId)].dirty, false);
});
test("view validates its init and rejects stale scopes without blanking current text", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.setCachedThread("account-a", "friend-a", "advisor", thread("account-a", "friend-a", "advisor",
    [{ id: "safe", role: "assistant", text: "<script>bad()</script>" }]));
  advisor.initFrameHandshake();
  const timeline = frameEnv.elements.get("advisorTimeline");
  const bubble = timeline.children.find(child => child.dataset.msgId === "safe").children[0];
  assert.equal(bubble.textContent, "<script>bad()</script>");
  const init = hostEnv.messages.find(message => message.type === "ADVISOR_INIT");
  frameEnv.window.AdvisorView.receive({ ...init, envelope: "unknown" });
  frameEnv.window.AdvisorView.receive({ ...init, payload: { ...init.payload, theme: "blue" } });
  assert.equal(bubble.textContent, "<script>bad()</script>");
  advisor.generation++;
  advisor.syncThreadToFrame(thread("account-a", "friend-a", "advisor", [{ id: "new", role: "assistant", text: "新结果" }]));
  frameEnv.window.AdvisorView.receive({ type: "ADVISOR_SET_THREAD", scope: init.scope, payload: { thread: init.payload.thread, run: null,
    agentId: "advisor", draft: "" } });
  assert.ok(timeline.children.some(child => child.dataset.msgId === "new"));
  assert.ok(!timeline.children.some(child => child.dataset.msgId === "safe"));
});

test("view submit failure leaves the input draft and relies on the real acknowledgement", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  hostEnv.rpc = () => fail("额度不足"); advisor.initFrameHandshake();
  const input = frameEnv.elements.get("advisorInput"); input.value = "我要保留"; await input.dispatch("input");
  await frameEnv.elements.get("btnAdvisorSend").dispatch("click");
  await tick(); await tick();
  assert.equal(input.value, "我要保留");
  assert.equal(frameEnv.elements.get("btnAdvisorSend").disabled, false);
  assert.equal(frameEnv.elements.get("queueError").textContent, "额度不足");
});

test("different unloaded threads with empty IDs cannot retain the previous scope error", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.threadCache = {}; advisor.initFrameHandshake(); advisor.problem({ error: "旧会话错误" });
  advisor.generation++; advisor.currentUser = "friend-b";
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-b", "advisor"));
  assert.ok(!frameEnv.elements.get("advisorTimeline").children.some(child => child.children[0]?.textContent === "旧会话错误"));
});

test("retry fields reach the optional timeline component and disposed nodes release timers", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), received = [], disposed = [];
  frameEnv.window.AdvisorTimeline = {
    statusInto: (container, event) => { received.push(json(event)); container.textContent = event.text || event.state; },
    disposeInto: container => disposed.push(container), renderInto: (container, value) => { container.textContent = value; }
  };
  advisor.initFrameHandshake();
  const event = { seq: 1, type: "status", state: "retry", text: "正在重试", next: 1900000000000, attempt: 3 };
  advisor.syncThreadToFrame(thread(), { id: "retry-run", threadId: thread().id, state: "running", phaseEvent: event });
  assert.ok(received.some(value => value.next === event.next && value.attempt === 3));
  advisor.generation++; advisor.currentUser = "friend-b";
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-b", "advisor"));
  assert.ok(disposed.length > 0);
  const previous = received.length;
  advisor.syncThreadToFrame(thread("account-a", "friend-b", "advisor", [{ id: "invalid", role: "status", text: "fake",
    event: { ...event, next: "later" } }]));
  assert.equal(received.length, previous);
});

test("hiding the panel detaches polling without stopping the run; showing it reconnects", async () => {
  const env = host(), advisor = ready(env), poll = new AbortController();
  advisor.currentRunId = "manual-run"; advisor.pollAbortController = poll;
  env.rpc = (method, params) => method === "catalog.get" ? catalog() : method === "thread.get" ?
    { thread: thread(), run: { id: "manual-run", threadId: thread().id, state: "running" } } : new Promise(() => {});
  advisor.onEvent({ type: "visibility", visible: false }); await tick();
  assert.equal(poll.signal.aborted, true); assert.equal(advisor.currentRunId, null);
  assert.ok(env.rpcCalls.every(call => call.method !== "run.stop"));
  await advisor.loadThread(); assert.equal(advisor.currentRunId, null, "a hidden panel keeps only the saved projection");
  assert.ok(!methods(env).includes("run.events"));
  advisor.onEvent({ type: "visibility", visible: true }); await tick(); await tick(); await tick();
  assert.equal(methods(env).filter(name => name === "catalog.get").length, 1, "the catalog may have changed in 助手仓库");
  assert.equal(advisor.currentRunId, "manual-run"); assert.ok(methods(env).includes("run.events"));
});

test("conversation and template switches detach the visible poll without stopping backend runs", async () => {
  const env = host(), advisor = ready(env);
  env.rpc = (method, params, scope) => ({ thread: thread(scope.account, scope.user, params.agentId) });
  advisor.currentRunId = "previous-conversation-run";
  const conversationPoll = new AbortController(); advisor.pollAbortController = conversationPoll;
  advisor.onSessionChanged("account-a", "friend-b"); await tick();
  assert.equal(conversationPoll.signal.aborted, true);
  advisor.catalog.agents.push({ ...advisor.catalog.agents[0], id: "empathy", name: "共情" });
  advisor.currentRunId = "previous-template-run";
  const templatePoll = new AbortController(); advisor.pollAbortController = templatePoll;
  advisor.receive(request(env, "ADVISOR_SWITCH_TEMPLATE", { agentId: "empathy" })); await tick();
  assert.equal(templatePoll.signal.aborted, true); assert.equal(advisor.activeAgentId, "empathy");
  assert.ok(env.rpcCalls.every(call => call.method !== "run.stop"));
});

test("returning from B to A restores A's running task or completed result from the backend", async () => {
  for (const stateOnReturn of ["running", "done"]) {
    const env = host(), advisor = ready(env), polls = [];
    let state = "running";
    const run = () => ({ id: "background-a-run", threadId: thread().id, state });
    const savedA = () => thread("account-a", "friend-a", "advisor", [
      { id: "a-user", role: "user", text: "A 的问题" },
      ...(state === "done" ? [{ id: "a-answer", role: "assistant", text: "A 的后台完成结果" }] : []),
    ]);
    env.rpc = (method, params, scope) => {
      if (method === "run.events") { const gate = deferred(); polls.push(gate); return gate.promise; }
      return scope.user === "friend-a" ? { thread: savedA(), run: run() } :
        { thread: thread("account-a", "friend-b", "advisor", [{ id: "b-answer", role: "assistant", text: "仅 B 的结果" }]), run: null };
    };
    await advisor.loadThread(); await tick();
    assert.equal(advisor.currentRunId, "background-a-run"); assert.equal(polls.length, 1);
    advisor.onSessionChanged("account-a", "friend-b"); await tick(); await tick();
    assert.equal(advisor.currentRunId, null);
    const count = env.messages.length;
    polls[0].resolve({ events: [{ seq: 1, type: "text", text: "不应进入 B", messageId: "late-a" }], run: run() }); await tick();
    assert.equal(env.messages.length, count);
    assert.equal(advisor.getCachedThread("account-a", "friend-b", "advisor").messages[0].text, "仅 B 的结果");
    state = stateOnReturn;
    advisor.onSessionChanged("account-a", "friend-a"); await tick(); await tick();
    assert.equal(advisor.threadProjection.scope.user, "friend-a");
    assert.equal(advisor.threadProjection.run.state, stateOnReturn);
    if (stateOnReturn === "running") {
      assert.equal(advisor.currentRunId, "background-a-run"); assert.equal(polls.length, 2);
      state = "done";
      polls[1].resolve({ events: [], run: run(), thread: savedA() }); await tick();
    } else {
      assert.equal(advisor.currentRunId, null); assert.equal(polls.length, 1);
    }
    assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages.at(-1).text, "A 的后台完成结果");
    assert.ok(env.rpcCalls.every(call => call.method !== "run.stop" && call.method !== "run.start"));
  }
});

test("a submitted task accepted after switching to B stays with A and never updates B's view", async () => {
  const env = host(), advisor = ready(env), acceptance = deferred();
  env.rpc = method => method === "queue.enqueue" ? acceptance.promise :
    { thread: thread("account-a", "friend-b", "advisor", [{ id: "b-only", role: "assistant", text: "B 的内容" }]), run: null };
  const pending = advisor.startRun("A 已提交的问题");
  advisor.onSessionChanged("account-a", "friend-b"); await tick(); await tick();
  const visibleMessages = env.messages.length;
  acceptance.resolve(enqueueAck(env.rpcCalls.find(call => call.method === "queue.enqueue").params));
  await pending;
  assert.equal(env.messages.length, visibleMessages);
  assert.equal(advisor.currentUser, "friend-b"); assert.equal(advisor.currentRunId, null);
  assert.equal(advisor.pendingRequests[advisor.scopeKey("account-a", "friend-a", "advisor")], undefined);
  const saved = advisor.getCachedThread("account-a", "friend-a", "advisor");
  assert.equal(saved.queue.items[0].message, "A 已提交的问题"); assert.equal(saved.messages.length, 0);
  assert.equal(advisor.getCachedThread("account-a", "friend-b", "advisor").messages[0].text, "B 的内容");
  assert.deepEqual(env.rpcCalls.find(call => call.method === "queue.enqueue").scope, { account: "account-a", user: "friend-a" });
  assert.equal(methods(env).filter(name => name === "run.stop").length, 0);
});

test("switching away before a thread exists preserves the unsent question without posting it", async () => {
  for (const laterDraft of ["", "稍后输入的新草稿"]) {
    const env = host(), advisor = ready(env), threadRead = deferred();
    delete advisor.threadCache[advisor.scopeKey("account-a", "friend-a", "advisor")];
    env.rpc = (method, params, scope) => scope.user === "friend-a" ? threadRead.promise : { thread: thread("account-a", "friend-b"), run: null };
    const pending = advisor.startRun("尚未提交的问题");
    const key = advisor.scopeKey("account-a", "friend-a", "advisor");
    assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "尚未提交的问题");
    if (laterDraft) advisor.setDraft("account-a", "friend-a", "advisor", laterDraft);
    advisor.onSessionChanged("account-a", "friend-b"); await tick();
    threadRead.resolve({ thread: thread(), run: null }); await pending;
    assert.equal(methods(env).filter(name => name === "queue.enqueue").length, 0);
    assert.equal(advisor.pendingEnqueues[key], undefined);
    assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), laterDraft || "尚未提交的问题");
    assert.equal(advisor.getDraft("account-a", "friend-b", "advisor"), "");
  }
});

test("a submit failure arriving after A to B to A restores A's queue error and draft without inventing a transcript", async () => {
  for (const laterDraft of ["", "回来后输入的新草稿"]) {
    const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), response = deferred();
    hostEnv.rpc = (method, params, scope) => method === "queue.enqueue" ? response.promise : { thread: thread("account-a", scope.user), run: null };
    advisor.initFrameHandshake();
    const starting = advisor.startRun("等待回执的问题");
    advisor.onSessionChanged("account-a", "friend-b"); await tick();
    advisor.onSessionChanged("account-a", "friend-a"); await tick(); await tick();
    const input = frameEnv.elements.get("advisorInput");
    if (laterDraft) { input.value = laterDraft; await input.dispatch("input"); }
    response.resolve(fail("Synthetic late submit failure")); await starting;
    assert.equal(advisor.currentUser, "friend-a");
    assert.equal(input.value, laterDraft || "等待回执的问题");
    const users = frameEnv.elements.get("advisorTimeline").children.filter(item => item.className === "advisor-msg user");
    assert.equal(users.length, 0);
    assert.equal(frameEnv.elements.get("queueError").textContent, "Synthetic late submit failure");
    assert.equal(advisor.submitPending, false);
  }
});

test("a disabled current template keeps its history read-only without switching to another assistant", async () => {
  const env = host(), advisor = ready(env), poll = new AbortController();
  advisor.catalog.agents[0].enabled = false;
  advisor.catalog.agents.push({ ...advisor.catalog.agents[0], id: "empathy", enabled: true });
  advisor.currentRunId = "previous-template-run"; advisor.pollAbortController = poll;
  advisor.syncTemplatesToFrame(); await tick();
  assert.equal(advisor.activeAgentId, "advisor"); assert.equal(poll.signal.aborted, false);
  assert.equal(advisor.projectionForScope().thread.readonly, true);
  assert.equal(env.rpcCalls.length, 0);
});
test("welcome text appears safely in an empty panel", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.catalog.agents[0].welcome = "你好，朋友 <b>欢迎</b>";
  advisor.initFrameHandshake();
  assert.equal(frameEnv.elements.get("welcomeText").textContent, "你好，朋友 <b>欢迎</b>");
  assert.equal(frameEnv.elements.get("advisorWelcome").hidden, false);
  assert.equal(frameEnv.elements.get("advisorTimeline").hidden, true);
  assert.ok(frameEnv.document.body.classList.contains("advisor-empty"));
  advisor.syncThreadToFrame(thread("account-a", "friend-a", "advisor", [{ id: "first", role: "user", text: "你好" }]));
  assert.equal(frameEnv.elements.get("advisorWelcome").hidden, true);
  assert.equal(frameEnv.elements.get("advisorTimeline").hidden, false);
  assert.ok(!frameEnv.document.body.classList.contains("advisor-empty"));
});

test("reset aliases new without stopping the old run; its draft stays with the old thread", async () => {
  const env = host(), advisor = ready(env);
  advisor.catalog.engine.state = "missing"; advisor.currentRunId = "old-run";
  advisor.setDraft("account-a", "friend-a", "advisor", "没有提交的普通草稿");
  env.rpc = method => method === "draft.save" ? { draft: { text: "没有提交的普通草稿", version: 1 } } : { thread: { ...thread(), id: "fresh-thread" }, run: null };
  await advisor.startRun("  /reset  ");
  assert.deepEqual(methods(env), ["draft.save", "thread.new"]);
  assert.deepEqual(Object.keys(env.rpcCalls[1].params).sort(), ["agentId", "requestId"]);
  assert.match(env.rpcCalls[1].params.requestId, /^req_[a-f0-9]{32}$/);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", thread().id), "没有提交的普通草稿");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").id, "fresh-thread");
  assert.equal(advisor.currentRunId, null);
  assert.ok(frameHtml.includes("btnNewThread")); assert.ok(!frameHtml.includes("reset"));
});

test("failed reset preserves prior history and offers an ephemeral command retry", async () => {
  const env = host(), advisor = ready(env);
  advisor.setDraft("account-a", "friend-a", "advisor", "/reset");
  advisor.setCachedThread("account-a", "friend-a", "advisor", thread("account-a", "friend-a", "advisor", [{ id: "saved", role: "assistant", text: "旧结果" }]));
  env.rpc = method => method === "draft.save" ? { draft: { text: "", version: 1 } } : fail("暂时失败");
  await advisor.startRun("/reset");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "");
  assert.ok(env.messages.some(item => item.type === "ADVISOR_COMMAND_FAILED" && item.payload.command === "/reset"));
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages.length, 1);
  env.rpc = () => ({ thread: { ...thread(), id: "new-thread" } }); await advisor.startRun("/reset");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages.length, 0);
});

test("simple agent popover switches the enabled template", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.catalog.agents.push({ ...advisor.catalog.agents[0], id: "empathy", name: "共情" });
  advisor.catalog.agents.push({ ...advisor.catalog.agents[0], id: "disabled", enabled: false, name: "停用" });
  hostEnv.rpc = (method, params) => ({ thread: thread("account-a", "friend-a", params.agentId) });
  advisor.initFrameHandshake();
  const menu = frameEnv.elements.get("agentMenu");
  assert.equal(menu.children.length, 2); assert.equal(frameEnv.elements.get("agentName").textContent, "军师");
  await frameEnv.elements.get("btnAgentMenu").dispatch("click"); assert.equal(menu.hidden, false);
  await menu.children[1].dispatch("click"); await tick();
  assert.equal(advisor.activeAgentId, "empathy"); assert.equal(menu.hidden, true);
});

test("running handshake retains stop and enables queued sending, including after a cached-load failure", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  const partial = thread("account-a", "friend-a", "advisor", [{ id: "partial", role: "assistant", text: "已生成的内容" }]);
  const run = { id: "running-run", threadId: partial.id, state: "running" };
  advisor.setCachedThread("account-a", "friend-a", "advisor", partial);
  advisor.currentRunId = run.id; advisor.syncThreadToFrame(partial, run);
  hostEnv.rpc = () => fail("暂时断网");
  await advisor.loadThread(); advisor.initFrameHandshake();
  assert.equal(hostEnv.messages.find(message => message.type === "ADVISOR_INIT").payload.run.id, run.id);
  assert.equal(frameEnv.elements.get("btnStopRun").hidden, false);
  assert.equal(frameEnv.elements.get("btnAdvisorSend").disabled, false);
  assert.equal(frameEnv.elements.get("btnAdvisorSend").hidden, false);
  assert.equal(frameEnv.elements.get("sendLabel").textContent, "加入队列");
  let stopped = false; advisor.stopRun = () => { stopped = true; };
  await frameEnv.elements.get("btnStopRun").dispatch("click"); assert.equal(stopped, true);
});

test("FRAME_READY resynchronizes submit, stop and error states", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.submitPending = true; advisor.stopPending = true;
  advisor.syncThreadToFrame(thread(), { id: "running-run", threadId: thread().id, state: "running" });
  advisor.problem({ error: "真实停止错误" }, advisor.captureScope(), "poll");
  advisor.initFrameHandshake();
  assert.equal(frameEnv.elements.get("btnStopRun").disabled, true);
  assert.equal(frameEnv.elements.get("btnStopRun").attributes["aria-label"], "正在停止");
  assert.equal(frameEnv.elements.get("btnAdvisorSend").disabled, true);
  assert.ok(frameEnv.elements.get("advisorTimeline").children.some(child => child.children[0]?.textContent === "真实停止错误"));
  assert.equal(hostEnv.messages.filter(message => message.type === "ADVISOR_INIT").length, 1);
});

test("successful catalog and thread recovery clears only its corresponding stored error", async () => {
  const env = host(), advisor = ready(env);
  advisor.problem({ error: "旧目录错误" }, advisor.captureScope(), "catalog");
  env.rpc = method => method === "catalog.get" ? catalog() : { thread: thread(), run: null };
  await advisor.loadCatalog(); assert.equal(advisor.lastProblem, null);
  advisor.problem({ error: "旧会话错误" }, advisor.captureScope(), "load");
  await advisor.loadThread(); assert.equal(advisor.lastProblem, null);
  advisor.problem({ error: "未恢复的发送错误" }, advisor.captureScope(), "send");
  await advisor.loadThread(); assert.equal(advisor.lastProblem.payload.error, "未恢复的发送错误");
});

test("new submission clears send error but preserves unrelated failure", async () => {
  const env = host(), advisor = ready(env), pending = deferred();
  advisor.problem({ error: "旧发送失败" }, advisor.captureScope(), "send");
  env.rpc = () => pending.promise;
  const started = advisor.startRun("再次发送"); assert.equal(advisor.lastProblem, null);
  pending.resolve(fail("新发送失败")); await started;
  assert.equal(advisor.queueProblems[advisor.queueKey()], "新发送失败");
  advisor.problem({ error: "未恢复的目录错误" }, advisor.captureScope(), "catalog");
  const second = deferred(); env.rpc = () => second.promise;
  const unrelated = advisor.retryQueue();
  assert.equal(advisor.lastProblem.payload.error, "未恢复的目录错误");
  second.resolve(fail("新问题失败")); await unrelated;
});

test("delayed enqueue keeps exact input until durable acknowledgement and does not invent user messages", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), response = deferred();
  const exact = "  用户原始内容  \n";
  hostEnv.rpc = method => method === "queue.enqueue" ? response.promise : fail("离线");
  advisor.initFrameHandshake(); const input = frameEnv.elements.get("advisorInput");
  input.value = exact; await input.dispatch("input"); await frameEnv.elements.get("btnAdvisorSend").dispatch("click");
  const timeline = frameEnv.elements.get("advisorTimeline");
  const users = timeline.children.filter(child => child.className === "advisor-msg user");
  assert.equal(users.length, 0); assert.equal(input.value, exact);
  assert.equal(frameEnv.elements.get("btnAdvisorSend").disabled, true);
  response.resolve(fail("真实发送失败")); await tick(); await tick();
  assert.equal(input.value, exact); assert.equal(frameEnv.elements.get("queueError").textContent, "真实发送失败");
  const requestId = advisor.pendingEnqueues[advisor.queueKey()].requestId;
  hostEnv.rpc = () => fail("再次失败"); await advisor.retryQueue();
  assert.equal(advisor.pendingEnqueues[advisor.queueKey()].requestId, requestId);
  assert.equal(timeline.children.filter(child => child.className === "advisor-msg user").length, 0);
});

test("durably queued questions stay separate then become one transcript message when dequeued", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), response = deferred();
  hostEnv.rpc = method => method === "queue.enqueue" ? response.promise : method === "queue.get" ? { queue: queue([], 2) } :
    { events: [], run: { id: "accepted", threadId: thread().id, messageId: "server-user", state: "done" },
      thread: thread("account-a", "friend-a", "advisor", [{ id: "server-user", role: "user", text: "测试提交" }]) };
  advisor.initFrameHandshake(); const input = frameEnv.elements.get("advisorInput"); input.value = "测试提交";
  await input.dispatch("input"); await frameEnv.elements.get("btnAdvisorSend").dispatch("click");
  await tick();
  response.resolve(enqueueAck(hostEnv.rpcCalls.find(call => call.method === "queue.enqueue").params));
  await tick(); await tick(); await tick();
  assert.equal(input.value, "");
  assert.equal(frameEnv.elements.get("queueCount").textContent, "待处理 1");
  assert.equal(frameEnv.elements.get("advisorTimeline").children.filter(child => child.className === "advisor-msg user").length, 0);
  await advisor.refreshQueue();
  const users = frameEnv.elements.get("advisorTimeline").children.filter(child => child.className === "advisor-msg user");
  assert.equal(users.length, 1); assert.equal(users[0].dataset.msgId, "server-user"); assert.equal(users[0].dataset.delivery, "");
  assert.ok(hostEnv.rpcCalls.find(call => call.method === "queue.enqueue").params.requestId.startsWith("req_"));
  assert.equal(Object.keys(advisor.pendingEnqueues).length, 0);
});

test("stream updates only the changing answer, keeps static DOM and has one current state", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), paints = [];
  frameEnv.window.AdvisorTimeline = { renderInto: (container, value, live) => { paints.push({ value, live }); container.textContent = value; },
    statusInto: (container, event) => { container.textContent = event.text || event.state; }, disposeInto() {} };
  const old = { id: "old-answer", role: "assistant", text: "以前的答案" }, user = { id: "new-user", role: "user", text: "新问题" };
  const run = { id: "live", threadId: thread().id, state: "running", phase: "preparing" };
  advisor.setCachedThread("account-a", "friend-a", "advisor", thread("account-a", "friend-a", "advisor", [old, user]));
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), run); advisor.initFrameHandshake();
  const timeline = frameEnv.elements.get("advisorTimeline");
  const oldNode = timeline.children.find(child => child.dataset.msgId === "old-answer");
  const oldPaints = paints.filter(value => value.value === old.text).length;
  for (let i = 0; i < 25; i++) advisor.postToFrame("ADVISOR_STREAM_UPDATE", { threadId: thread().id,
    run: { ...run, phaseEvent: { state: i === 0 ? "thinking" : "answering" } },
    messages: [{ id: "live:assistant", runId: "live", role: "assistant", text: "x".repeat(i + 1) }] });
  assert.equal(timeline.children.filter(child => child.classList.contains("advisor-current-status")).length, 1);
  assert.equal(timeline.children.at(-1).dataset.msgId, "ui:run");
  assert.equal(timeline.children.find(child => child.dataset.msgId === "old-answer"), oldNode);
  assert.equal(paints.filter(value => value.value === old.text).length, oldPaints);
});

test("streaming sends increments before one authoritative terminal snapshot", async () => {
  const env = host(), advisor = ready(env); const run = { id: "stream-run", threadId: thread().id, state: "running", phase: "answering" };
  advisor.syncThreadToFrame(thread(), run); env.messages.length = 0;
  let step = 0;
  env.rpc = () => step++ === 0 ? { events: [{ seq: 1, type: "text", text: "首段" }], run } :
    { events: [{ seq: 2, type: "text", text: "尾段" }, { seq: 3, type: "done" }], run: { ...run, state: "done" },
      thread: thread("account-a", "friend-a", "advisor", [{ id: "real-final", role: "assistant", text: "首段尾段" }]) };
  const polling = advisor.pollEvents(run.id); await tick(); await tick();
  assert.equal(env.messages.filter(message => message.type === "ADVISOR_STREAM_UPDATE").length, 1);
  assert.equal(env.messages.filter(message => message.type === "ADVISOR_SET_THREAD").length, 0);
  assert.deepEqual(env.rpcCalls[0].params, { runId: run.id, after: 0 });
  await env.flushTimers(); await polling;
  assert.deepEqual(env.rpcCalls[1].params, { runId: run.id, after: 1 });
  assert.equal(env.messages.filter(message => message.type === "ADVISOR_SET_THREAD").length, 1);
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").messages[0].text, "首段尾段");
});

test("a saved terminal error is shown once and legacy busy rows are not duplicated", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  const history = thread("account-a", "friend-a", "advisor", [
    { id: "old:1", role: "status", text: "正在回答", event: { type: "status", state: "answering" } },
    { id: "current-user", role: "user", text: "新问题" },
    { id: "current-error", role: "status", status: "error", text: "生成失败：合成模型错误" }]);
  const run = { id: "failed-run", threadId: history.id, state: "error", error: "合成模型错误" };
  advisor.setCachedThread("account-a", "friend-a", "advisor", history);
  advisor.syncThreadToFrame(history, run); advisor.initFrameHandshake();
  const ids = frameEnv.elements.get("advisorTimeline").children.map(child => child.dataset.msgId);
  assert.deepEqual(ids, ["current-user", "current-error"]);
});
test("sessions under one contact isolate late acceptance, drafts, and selection", async () => {
  const env = host(), advisor = ready(env), accepted = deferred();
  const a = advisor.currentThreadId, b = "discussion-b";
  env.rpc = (method, params) => method === "queue.enqueue" ? accepted.promise :
    method === "draft.save" ? { draft: { text: params.text, version: 1 } } :
    method === "thread.new" ? { thread: { ...thread(), id: b, draft: { text: "", version: 0 } }, run: null } : fail("unexpected");
  const pending = advisor.startRun("A 中的问题");
  advisor.setDraft("account-a", "friend-a", "advisor", "A 的下一条草稿", a);
  await advisor.newThread();
  assert.equal(advisor.currentThreadId, b);
  advisor.setDraft("account-a", "friend-a", "advisor", "B 的草稿", b);
  accepted.resolve(enqueueAck(env.rpcCalls.find(call => call.method === "queue.enqueue").params)); await pending;
  assert.equal(advisor.currentThreadId, b); assert.equal(advisor.threadProjection.thread.id, b);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", a), "A 的下一条草稿");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", b), "B 的草稿");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor", a).queue.items[0].message, "A 中的问题");
  assert.ok(!methods(env).includes("run.stop"));
});

test("draft writes coalesce by thread and a version conflict preserves local input until explicit retry", async () => {
  const env = host(), advisor = ready(env), draftWrite = deferred();
  const ownId = advisor.currentThreadId;
  advisor.acceptDraft(advisor.captureScope(), { ...thread(), draft: { text: "服务器草稿", version: 3, scrollTop: 27 } });
  env.rpc = method => method === "thread.get" ? { thread: { ...thread(), draft: { text: "另一窗口的新草稿", version: 4 } } } : draftWrite.promise;
  advisor.setDraft("account-a", "friend-a", "advisor", "当前编辑", ownId);
  const saving = advisor.flushDraft();
  advisor.setDraft("account-a", "friend-a", "advisor", "当前编辑又增加", ownId);
  draftWrite.resolve(fail("草稿已更新，请保留当前内容后重试", "request-conflict")); await saving;
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", ownId), "当前编辑又增加");
  assert.match(advisor.lastProblem.payload.error, /另一窗口更新了草稿/);
  await advisor.flushDraft(); assert.equal(methods(env).filter(name => name === "draft.save").length, 1);
  env.rpc = (method, params) => ({ draft: { text: params.text, version: 5 } });
  await advisor.flushDraft(advisor.captureScope(), true);
  const saves = env.rpcCalls.filter(call => call.method === "draft.save");
  assert.equal(saves[1].params.version, 4); assert.equal(saves[1].params.text, "当前编辑又增加");
  assert.equal(advisor.draftRecords[advisor.scopeKey("account-a", "friend-a", "advisor", ownId)].dirty, false);
});

test("a new discussion waits for a persisted draft", async () => {
  const env = host(), advisor = ready(env), ownId = advisor.currentThreadId;
  advisor.acceptDraft(advisor.captureScope(), { ...thread(), draft: { text: "", version: 0 } });
  advisor.setDraft("account-a", "friend-a", "advisor", "未保存的合成草稿", ownId, 71);
  env.rpc = method => method === "draft.save" ? fail("合成保存失败") : fail("新建请求不能越过未保存的草稿");
  await advisor.newThread(); await advisor.newThread();
  assert.equal(advisor.currentThreadId, ownId); assert.equal(advisor.pendingNew.size, 0);
  assert.deepEqual(methods(env), ["draft.save"]);
  assert.equal(advisor.lastProblem.payload.retryKind, "draft");
  const key = advisor.scopeKey("account-a", "friend-a", "advisor", ownId);
  assert.equal(advisor.draftRecords[key].text, "未保存的合成草稿"); assert.equal(advisor.draftRecords[key].scrollTop, 71);
  env.rpc = (method, params) => method === "draft.save" ? { draft: { text: params.text, version: params.version + 1 } } :
    { thread: { ...thread(), id: "fresh-after-save", draft: { text: "", version: 0 } }, run: null };
  await advisor.flushDraft(advisor.captureScope(), true); await advisor.newThread();
  assert.equal(methods(env).filter(name => name === "thread.new").length, 1);
  assert.equal(advisor.currentThreadId, "fresh-after-save");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", ownId), "未保存的合成草稿");
});

test("history can open with every template deleted and groups old assistants read-only", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.catalog.agents = []; advisor.currentThreadId = ""; advisor.activeAgentId = "";
  advisor.initFrameHandshake();
  hostEnv.rpc = () => ({ threads: [
    { id: "old-one", agentId: "deleted-a", agentName: "旧助手甲", title: "旧讨论", preview: "合成历史", readonly: true },
    { id: "old-two", agentId: "deleted-b", agentName: "旧助手乙", title: "另一次讨论", preview: "另一历史", readonly: true },
  ] });
  await advisor.loadHistory();
  assert.deepEqual(hostEnv.rpcCalls[0], { method: "threads.list", params: {}, scope: { account: "account-a", user: "friend-a" } });
  assert.equal(frameEnv.elements.get("btnHistory").disabled, false);
  assert.equal(frameEnv.elements.get("otherHistory").hidden, false);
  assert.equal(frameEnv.elements.get("otherHistoryList").children.length, 2);
  assert.equal(frameEnv.elements.get("historyList").children.length, 0);
});

test("read-only history returns to the original selected thread without touching its draft", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), normal = advisor.currentThreadId;
  advisor.setDraft("account-a", "friend-a", "advisor", "保留普通讨论草稿", normal);
  advisor.historyItems = [{ id: "retired-history", agentId: "retired", title: "旧讨论", readonly: true }];
  hostEnv.rpc = method => method === "draft.save" ? { draft: { text: "保留普通讨论草稿", version: 1 } } :
    method === "thread.select" ? { thread: { ...thread("account-a", "friend-a", "retired"), id: "retired-history",
      readonly: true, readonlyReason: "原助手已删除", agentName: "旧助手", messages: [{ id: "old-answer", role: "assistant", text: "旧回答" }] } } :
    { thread: { ...thread(), draft: { text: "保留普通讨论草稿", version: 1 } } };
  advisor.initFrameHandshake(); await advisor.selectThread("retired-history", "retired");
  assert.deepEqual(hostEnv.rpcCalls.find(call => call.method === "thread.select").params, { agentId: "retired", threadId: "retired-history" });
  assert.equal(frameEnv.elements.get("advisorInput").disabled, true);
  assert.equal(frameEnv.elements.get("btnCompact").disabled, true);
  assert.equal(frameEnv.elements.get("btnReturnHistory").hidden, false);
  assert.equal(advisor.selectedThreads[advisor.baseKey("account-a", "friend-a", "advisor")], normal);
  await advisor.returnFromHistory();
  assert.equal(advisor.currentThreadId, normal); assert.equal(advisor.activeAgentId, "advisor");
  assert.equal(frameEnv.elements.get("advisorInput").value, "保留普通讨论草稿");
});

test("usage hides invalid values, marks estimates and presents real compaction summaries", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  const value = { ...thread(), title: "合成上下文测试", contextTokens: 32000, usage: null,
    messages: [{ id: "a", role: "assistant", text: "早期讨论", createdAtMs: 1 }], compactions: [] };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  assert.equal(frameEnv.elements.get("usageButton").hidden, true);
  value.usage = { inputTokens: 12500, contextTokens: 32000, estimated: true, recordedAtMs: 1000 };
  advisor.syncThreadToFrame(value);
  assert.match(frameEnv.elements.get("usageButton").textContent, /^约 /);
  value.compactions = [{ id: "compact-a", state: "completed", kind: "manual", summary: "用户决定：先观察。助手的推测尚未确认。", completedAtMs: 2000 }];
  value.compactedSinceUsage = true; advisor.syncThreadToFrame(value);
  assert.equal(frameEnv.elements.get("usageButton").textContent, "已压缩");
  const event = frameEnv.elements.get("advisorTimeline").children.find(item => item.className === "advisor-compaction");
  assert.equal(event.tag, "details"); assert.match(event.children[1].textContent, /尚未确认/);
});

test("manual compaction never queues while answering and its Stop targets only the operation", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  const value = { ...thread(), messages: [{ id: "a", role: "assistant", text: "已有讨论" }] };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  const run = { id: "run-active", threadId: value.id, state: "running" };
  advisor.syncThreadToFrame(value, run); advisor.currentRunId = run.id;
  await advisor.startRun("/compact");
  assert.equal(hostEnv.rpcCalls.length, 0); assert.match(advisor.lastProblem.payload.error, /回答完成后/);
  advisor.abortCurrentPoll(); advisor.threadProjection = null;
  value.activeOperation = { id: "compact-only", state: "running", kind: "compact" };
  advisor.syncThreadToFrame(value, null); advisor.setCachedThread("account-a", "friend-a", "advisor", value);
  hostEnv.rpc = () => ({ operation: { id: "compact-only", state: "unknown" } });
  await advisor.stopOperation();
  assert.deepEqual(hostEnv.rpcCalls[0], { method: "operation.stop", params: { threadId: value.id, operationId: "compact-only" },
    scope: { account: "account-a", user: "friend-a" } });
  assert.equal(frameEnv.elements.get("btnStopRun").title, "停止压缩并暂停队列");
  assert.equal(frameEnv.elements.get("btnAdvisorSend").disabled, false);
});

test("a stale missing-model reading blocks neither sending, compaction nor retry; the service decides", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  advisor.catalog.engine.state = "missing";
  const value = thread("account-a", "friend-a", "advisor", [{ id: "original-user", role: "user", text: "原问题" },
    { id: "earlier-answer", role: "assistant", text: "已有讨论" }]);
  const run = { id: "failed-original", threadId: value.id, messageId: "original-user", state: "error" };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.syncThreadToFrame(value, run); advisor.initFrameHandshake();
  assert.equal(env.elements.get("btnAdvisorSend").disabled, false);
  assert.equal(env.elements.get("btnCompact").disabled, false);
  assert.equal(env.elements.get("compactHint").textContent, "");
  env.rpc = () => fail("模型服务暂不可用，请检查模型设置后重试", "api-not-configured");
  await advisor.retryRun();
  assert.equal(env.rpcCalls.filter(call => call.method === "run.start").length, 1);
  assert.equal(advisor.lastProblem.payload.error, "模型服务暂不可用，请检查模型设置后重试");
  env.rpc = (method, params) => method === "queue.enqueue" ? enqueueAck(params) : {};
  const input = env.elements.get("advisorInput"); input.value = "你现在可以连网吗？"; await input.dispatch("input");
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick(); await tick();
  assert.equal(env.rpcCalls.find(call => call.method === "queue.enqueue").params.message, "你现在可以连网吗？");
});

test("a lost compaction acknowledgement is queried by the original request id before retry", async () => {
  const env = host(), advisor = ready(env);
  env.rpc = method => method === "thread.compact" ? fail("response lost") : { operation: { id: "accepted-compact", state: "running" } };
  await advisor.compactThread(); await advisor.compactThread();
  assert.deepEqual(methods(env), ["thread.compact", "operation.get"]);
  assert.equal(env.rpcCalls[1].params.requestId, env.rpcCalls[0].params.requestId);
  assert.deepEqual(Object.keys(env.rpcCalls[1].params).sort(), ["requestId", "threadId"]);
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").activeOperation.id, "accepted-compact");
  const missing = host(), other = ready(missing);
  missing.rpc = method => method === "thread.compact" && missing.rpcCalls.length === 1 ? fail("response lost") :
    method === "operation.get" ? fail("操作不存在", "not-found") : { operation: { id: "second", state: "running" } };
  await other.compactThread(); await other.compactThread();
  assert.deepEqual(methods(missing), ["thread.compact", "operation.get", "thread.compact"], "a missing operation is safely retried");
});

test("history refresh keeps row order stable while background states change", async () => {
  const env = host(), advisor = ready(env); let refresh = false;
  env.rpc = () => ({ threads: refresh ? [
    { id: "b", agentId: "advisor", title: "B", preview: "new", state: "done" }, { id: "a", agentId: "advisor", title: "A", preview: "a" },
  ] : [{ id: "a", agentId: "advisor", title: "A", preview: "a" }, { id: "b", agentId: "advisor", title: "B", preview: "", state: "running" }] });
  await advisor.loadHistory(); refresh = true; await advisor.loadHistory(true);
  assert.deepEqual(json(advisor.historyItems.map(item => item.id)), ["a", "b"]);
  assert.equal(advisor.historyItems[1].state, "done"); advisor.closeHistory();
});

test("unknown slashes are plain text, exact commands share actions and composition Enter never submits", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), sent = [];
  advisor.initFrameHandshake(); advisor.startRun = message => sent.push(message);
  const input = frameEnv.elements.get("advisorInput"); input.value = "/sessions more";
  await input.dispatch("keydown", { key: "Enter", preventDefault() {} }); assert.equal(sent[0], "/sessions more");
  input.value = "中文输入"; await input.dispatch("compositionstart");
  await input.dispatch("keydown", { key: "Enter", preventDefault() {}, isComposing: true }); assert.equal(sent.length, 1);
  await input.dispatch("compositionend");
  advisor.postToFrame("ADVISOR_SUBMIT_STATE", { pending: false });
  input.value = "/sessions"; await input.dispatch("input");
  await input.dispatch("keydown", { key: "Enter", preventDefault() {} }); assert.equal(sent[1], "/sessions");
});

test("supported commands after the default @ stay local while unknown commands and other mentions keep their text", async () => {
  for (const [value, expected] of [
    ["@合成好友 /new", "/new"], ["@合成好友 /reset", "/reset"],
    ["@合成好友 /compact", "/compact"], ["@合成好友 /sessions", "/sessions"],
    ["@合成好友 /unknown", "@合成好友 /unknown"],
    ["@合成好友 /sessions more", "@合成好友 /sessions more"],
    ["@另一好友 /new", "@另一好友 /new"], ["@合成好友 @另一好友 /new", "@合成好友 @另一好友 /new"]
  ]) {
    const { hostEnv: env } = pair(), advisor = ready(env), sent = [];
    advisor.currentName = "合成好友"; advisor.initFrameHandshake(); advisor.startRun = message => sent.push(message);
    const input = env.elements.get("advisorInput"); input.value = value; await input.dispatch("input");
    const local = expected.startsWith("/");
    assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", thread().id), local ? "" : value);
    await env.elements.get("btnAdvisorSend").dispatch("click");
    assert.deepEqual(sent, [expected]);
    assert.equal(input.value, local ? "" : value);
  }
});

test("loading an older page preserves newer messages and later refreshes retain the loaded page", async () => {
  const env = host(), advisor = ready(env);
  const latest = { ...thread(), messages: [{ id: "m3", seq: 3, role: "assistant", text: "最近讨论" }], hasMore: true, beforeSeq: 3 };
  advisor.setCachedThread("account-a", "friend-a", "advisor", latest);
  env.rpc = () => ({ thread: { ...thread(), messages: [{ id: "m1", seq: 1, role: "user", text: "早期问题" }], hasMore: false, beforeSeq: 1 } });
  await advisor.loadEarlier();
  assert.deepEqual(env.rpcCalls[0].params, { agentId: "advisor", threadId: thread().id, beforeSeq: 3 });
  advisor.rememberProjection(advisor.captureScope(), { thread: { ...latest, messages: [{ id: "m3", seq: 3, role: "assistant", text: "最近讨论更新" }] } });
  const saved = advisor.getCachedThread("account-a", "friend-a", "advisor");
  assert.deepEqual(json(saved.messages.map(item => item.id)), ["m1", "m3"]);
  assert.equal(saved.messages[1].text, "最近讨论更新"); assert.equal(saved.hasMore, false);
});

test("busy deletion is refused and a failed cleanup stays read-only until the same operation is retried", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), ownId = advisor.currentThreadId;
  advisor.setDraft("account-a", "friend-a", "advisor", "将随本会话删除", ownId);
  advisor.setDraft("account-a", "friend-a", "advisor", "另一讨论的合成草稿", "other-thread");
  advisor.historyItems = [{ id: ownId, agentId: "advisor", title: "讨论 A", preview: "", state: "running" }];
  await advisor.deleteThread(ownId, "advisor"); assert.equal(hostEnv.rpcCalls.length, 0);
  advisor.historyItems[0].state = "idle"; let complete = false;
  hostEnv.rpc = method => method === "thread.delete" ? (complete ? { deleted: true, state: "completed", threadId: ownId } :
    { deleted: false, state: "failed", error: "原生清理失败" }) :
    method === "threads.list" ? { threads: complete ? [] : [{ id: ownId, agentId: "advisor", title: "讨论 A", preview: "", state: "failed", deleteState: "failed" }] } :
    method === "draft.save" ? new Promise(() => {}) : { thread: { ...thread(), id: "fresh-after-delete" }, run: null };
  advisor.initFrameHandshake(); await advisor.deleteThread(ownId, "advisor");
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor", ownId).deleteState, "failed");
  assert.equal(frameEnv.elements.get("advisorInput").disabled, true);
  complete = true; await advisor.deleteThread(ownId, "advisor");
  const deletes = hostEnv.rpcCalls.filter(item => item.method === "thread.delete");
  assert.equal(deletes.length, 2); assert.equal(deletes[0].params.requestId, deletes[1].params.requestId);
  assert.equal(advisor.currentThreadId, "fresh-after-delete");
  assert.equal(advisor.draftRecords[advisor.scopeKey("account-a", "friend-a", "advisor", ownId)], undefined);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", "other-thread"), "另一讨论的合成草稿");
});

test("copying an answer goes through the host and reports its result", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.initFrameHandshake();
  advisor.receive(request(hostEnv, "ADVISOR_COPY", { text: "建议内容" })); await tick(); await tick();
  assert.deepEqual(hostEnv.hostCalls, [["clipboard.copy", { text: "建议内容" }]]);
  assert.deepEqual(hostEnv.messages.at(-1).payload, { copied: true });
  hostEnv.host = () => Promise.reject(new Error("no"));
  await advisor.copyText("x"); assert.deepEqual(hostEnv.messages.at(-1).payload, { copied: false });
  assert.ok(frameEnv.elements.get("advisorTimeline"));
});

test("a quote from 分析与建议 appends the source message to the latest draft without sending", async () => {
  const env = host(), advisor = ready(env);
  advisor.startRun = () => { throw new Error("never sent"); };
  advisor.setDraft("account-a", "friend-a", "advisor", "我已经写下的问题", advisor.currentThreadId);
  advisor.onEvent({ type: "quote", account: "account-a", user: "friend-a", messageId: "synthetic-message", text: "合成原话" });
  const draft = advisor.getDraft("account-a", "friend-a", "advisor", advisor.currentThreadId);
  assert.ok(draft.startsWith("我已经写下的问题\n\n")); assert.match(draft, /synthetic-message/); assert.match(draft, /合成原话/);
  assert.ok(env.messages.some(item => item.type === "ADVISOR_SET_THREAD" && item.payload.draft === draft));
  for (const scenario of ["foreign", "readonly", "overflow"]) {
    const other = host(), controller = ready(other), id = controller.currentThreadId;
    if (scenario === "readonly") controller.setCachedThread("account-a", "friend-a", "advisor", { ...thread(), readonly: true });
    controller.setDraft("account-a", "friend-a", "advisor", scenario === "overflow" ? "x".repeat(15999) : "保留的草稿", id);
    const before = controller.getDraft("account-a", "friend-a", "advisor", id);
    assert.equal(controller.quoteMessage({ account: "account-a", user: scenario === "foreign" ? "friend-b" : "friend-a", messageId: "one", text: "合成原话" }), false, scenario);
    assert.equal(controller.getDraft("account-a", "friend-a", "advisor", id), before, scenario);
  }
});

test("a quote reaching a panel that still loads waits for the friend's discussion instead of being lost", async () => {
  // Added 2026-10-13 (review): 继续聊这句 first opens the panel; its context and quote arrive before catalog.get and
  // thread.get have answered, and the loaded discussion's saved draft must not replace the quote.
  const env = environment([]), waits = [];
  let current = null, start;
  const started = new Promise(resolve => { start = resolve; });
  Object.assign(env.window.kyfHost, { ready: () => started, context: () => current });
  env.window.AdvisorView = { receive: data => env.messages.push(json(data)) };
  env.rpc = (method, params, scope) => { const wait = deferred(); waits.push({ method, params, scope, resolve: wait.resolve }); return wait.promise; };
  vm.runInContext(hostSource, env.context);
  const advisor = env.window.AdvisorController;
  const answer = async (method, value) => {
    const due = waits.filter(item => item.method === method);
    waits.splice(0, waits.length, ...waits.filter(item => item.method !== method));
    for (const wait of due) wait.resolve(value(wait));
    for (let index = 0; index < 6; index++) await tick();
  };
  const show = user => {
    current = { account: "account-a", user, theme: "dark", surface: "panel" };
    env.kyfListener({ type: "context", context: current }); start();
  };
  const quote = user => env.kyfListener({ type: "event", event: { type: "quote", account: "account-a", user, messageId: "m-" + user, text: "合成原话" } });
  const saved = item => ({ thread: { ...thread(item.scope.account, item.scope.user, item.params.agentId), draft: { text: "", version: 1 } }, run: null });
  show("friend-a"); await tick();
  quote("friend-a"); await tick();
  await answer("catalog.get", () => catalog());
  await answer("thread.get", saved);
  const id = thread().id, draft = advisor.getDraft("account-a", "friend-a", "advisor", id);
  assert.match(draft, /m-friend-a/); assert.match(draft, /合成原话/);
  assert.equal(env.messages.filter(item => item.type === "ADVISOR_SET_THREAD").at(-1).payload.draft, draft);
  assert.ok(!methods(env).includes("run.start"), "a quote is never sent");
  // A friend switched to just before the quote: the quote waits for that friend's discussion as well.
  show("friend-b"); quote("friend-b"); await tick();
  await answer("draft.save", () => ({ draft: { text: draft, version: 2 } }));
  await answer("thread.get", saved);
  assert.match(advisor.getDraft("account-a", "friend-b", "advisor", thread("account-a", "friend-b").id), /m-friend-b/);
  // A quote still waiting when the friend changes is dropped, never applied to another discussion.
  show("friend-c"); quote("friend-c"); show("friend-d"); await tick();
  await answer("thread.get", saved);
  assert.equal(advisor.getDraft("account-a", "friend-c", "advisor", thread("account-a", "friend-c").id), "");
  assert.equal(advisor.getDraft("account-a", "friend-d", "advisor", thread("account-a", "friend-d").id), "");
  assert.equal(advisor.pendingQuote, null);
});
// -- 助手仓库 --------------------------------------------------------------------------------------------------
function repository(value = catalog()) {
  const env = environment(["advisorRepoContainer"]);
  vm.runInContext(repositorySource, env.context);
  const repo = env.window.AdvisorRepository;
  repo.catalog = json(value); repo.catalogReady = true; repo.render();
  return { env, repo, container: env.elements.get("advisorRepoContainer"), modal: () => env.document.body.children.at(-1) };
}
function descendants(element, predicate) {
  return [element, ...element.children.flatMap(child => descendants(child, () => true))].filter(predicate);
}
const buttonNamed = (root, label) => descendants(root, item => item.tag === "button" && item.textContent === label)[0];

test("the repository loads its catalog when shown and keeps the minimal card layout", async () => {
  const { env, repo, container } = repository();
  env.rpc = () => catalog();
  env.kyfListener({ type: "event", event: { type: "shown" } }); await tick(); await tick();
  assert.deepEqual(env.rpcCalls, [{ method: "catalog.get", params: {} }]);
  assert.equal(descendants(container, item => item.className === "advisor-repo-title")[0].textContent, "助手仓库");
  assert.equal(descendants(container, item => item.textContent === "导入技能").length, 0);
  assert.equal(descendants(container, item => item.className.includes("advisor-engine-badge")).length, 0);
  assert.equal(descendants(container, item => item.className === "advisor-pill").length, 0);
  assert.equal(descendants(container, item => item.tag === "button" && item.textContent === "删除").length, repo.catalog.agents.length);
});

test("catalog edits are plugin calls; a failed save is reported in place and changes nothing", async () => {
  const { env, repo, container } = repository();
  env.rpc = () => fail("保存拒绝");
  assert.equal(await repo.saveTemplate({ id: "advisor", name: "别名", description: "", prompt: "test", skillIds: ["missing"] }), false);
  assert.equal(repo.catalog.agents[0].name, "军师"); assert.equal(repo.catalogError, "保存拒绝");
  assert.deepEqual(env.rpcCalls[0], { method: "catalog.saveAgent", params: { agent: { id: "advisor", name: "别名", description: "",
    prompt: "test", welcome: "", skillIds: [] } } });
  env.rpc = () => catalog(false);
  const toggle = descendants(container, item => item.className.startsWith("advisor-switch"))[0];
  await toggle.dispatch("click"); await tick();
  assert.deepEqual(env.rpcCalls.at(-1), { method: "catalog.enableAgent", params: { id: "advisor", enabled: false } });
  assert.equal(repo.catalog.agents[0].enabled, false);
});

test("deleting an assistant asks in the page, then deletes through the plugin", async () => {
  const { env, repo, container, modal } = repository();
  await buttonNamed(container, "删除").dispatch("click");
  const dialog = modal();
  assert.equal(descendants(dialog, item => item.className === "advisor-modal-title")[0].textContent, "删除「军师」？");
  env.rpc = () => ({ ...catalog(), agents: [] });
  await buttonNamed(dialog, "删除").dispatch("click");
  assert.deepEqual(env.rpcCalls, [{ method: "catalog.deleteAgent", params: { id: "advisor" } }]);
  assert.equal(dialog.parentNode, null); assert.equal(repo.catalog.agents.length, 0);
  assert.equal(descendants(container, item => item.className === "advisor-repo-empty").length, 1);
});

test("duplicate skill import is inline and the same modal can retry with a new name", async () => {
  const { env, repo, modal } = repository();
  let attempts = 0;
  env.rpc = () => ++attempts === 1 ? fail("技能名称已存在") : { ...catalog(), skills: [{ id: "skill:new", name: "新名称" }] };
  repo.showImportSkillModal();
  const dialog = modal(), fields = descendants(dialog, item => item.tag === "input" || item.tag === "textarea");
  fields[0].value = "重复名称"; fields[2].value = "合成技能内容";
  const submit = buttonNamed(dialog, "导入");
  await submit.dispatch("click");
  assert.equal(submit.disabled, false); assert.equal(fields[2].value, "合成技能内容"); assert(dialog.parentNode);
  const error = descendants(dialog, item => item.className === "advisor-modal-error")[0];
  assert.equal(error.hidden, false); assert.equal(error.textContent, "技能名称已存在");
  fields[0].value = "新名称"; await submit.dispatch("click");
  assert.equal(attempts, 2); assert.equal(dialog.parentNode, null);
  assert.deepEqual(env.rpcCalls.at(-1), { method: "catalog.importSkill", params: { name: "新名称", description: "", content: "合成技能内容" } });
  assert.equal(repo.catalog.skills[0].name, "新名称");
});

test("skills keep five-row bands and removal changes only the unsaved current assistant", async () => {
  const { env, repo, modal } = repository();
  repo.catalog.skills = Array.from({ length: 12 }, (_, index) => ({ id: `skill:${index}`, name: `技能${index}` }));
  repo.catalog.agents[0].skillIds = ["skill:2"];
  repo.showEditAgentModal(repo.catalog.agents[0]);
  const dialog = modal();
  assert.equal(dialog.children[0].children[0].textContent, "编辑助手");
  const list = descendants(dialog, item => item.className === "advisor-skill-list")[0];
  assert.equal(list.children[4].style.gridColumn, "1"); assert.equal(list.children[4].style.gridRow, "5");
  assert.equal(list.children[5].style.gridColumn, "2"); assert.equal(list.children[5].style.gridRow, "1");
  assert.equal(list.children[10].style.gridColumn, "1"); assert.equal(list.children[10].style.gridRow, "6");
  const toggle = descendants(dialog, item => item.textContent === "移除技能")[0];
  await toggle.dispatch("click");
  assert.equal(descendants(list, item => item.className === "advisor-skill-remove").length, 12);
  await descendants(list, item => item.className === "advisor-skill-remove")[2].dispatch("click");
  assert.deepEqual(repo.catalog.agents[0].skillIds, ["skill:2"]); assert.equal(env.rpcCalls.length, 0);
  const fields = descendants(dialog, item => item.tag === "input" && item.type !== "checkbox");
  fields[0].value = "";
  await buttonNamed(dialog, "保存").dispatch("click");
  assert.equal(descendants(dialog, item => item.className === "advisor-modal-error")[0].textContent, "请输入名称和提示词");
  assert.equal(env.rpcCalls.length, 0);
});

test("a new assistant previews a GitHub package then is created atomically", async () => {
  const { env, repo, container, modal } = repository();
  env.rpc = method => method === "import.preview" ? { preview: { id: "imp_" + "a".repeat(32), name: "导入助手", description: "说明",
    defaultPrompt: "合成提示词", welcome: "你好", resourceCount: 3, compatibility: { state: "readonly", warnings: [] } } } :
    method === "import.cancel" ? { cancelled: true } :
    { ...catalog(), agents: [{ ...catalog().agents[0], id: "imported", name: "导入助手" }], importedAgentId: "imported" };
  await buttonNamed(container, "新建助手").dispatch("click");
  const dialog = modal();
  descendants(dialog, item => item.tag === "input")[0].value = "https://github.com/synthetic/package";
  await buttonNamed(dialog, "读取").dispatch("click");
  assert.deepEqual(env.rpcCalls[0], { method: "import.preview", params: { source: { kind: "github", url: "https://github.com/synthetic/package" } } });
  assert.equal(descendants(dialog, item => item.tag === "input" && item.type !== "checkbox")[1].value, "导入助手");
  const create = buttonNamed(dialog, "导入并创建");
  assert.equal(create.disabled, false); await create.dispatch("click");
  const commit = env.rpcCalls.find(call => call.method === "import.commit");
  assert.equal(commit.params.agent.prompt, "合成提示词"); assert.equal(commit.params.previewId, "imp_" + "a".repeat(32));
  assert.match(commit.params.requestId, /^req_[a-f0-9]{32}$/);
  assert.equal(dialog.parentNode, null); assert.equal(repo.catalog.agents[0].id, "imported");
});

test("cancel during preview cancels its late result without creating any assistant", async () => {
  const { env, repo, modal } = repository(), loading = deferred();
  env.rpc = method => method === "import.preview" ? loading.promise : { cancelled: true };
  repo.showImportAssistantModal();
  const dialog = modal();
  const reading = buttonNamed(dialog, "读取").dispatch("click");
  await buttonNamed(dialog, "取消").dispatch("click");
  loading.resolve({ preview: { id: "imp_" + "b".repeat(32) } }); await reading; await tick();
  assert.deepEqual(env.rpcCalls.map(call => [call.method, call.params.previewId]), [["import.preview", undefined], ["import.cancel", "imp_" + "b".repeat(32)]]);
  assert.equal(dialog.parentNode, null);
});

test("a local package comes from the host picker as a one-time grant, never a path", async () => {
  const { env, repo, modal } = repository();
  env.host = () => ({ token: "fg_" + "c".repeat(32), label: "包.zip" });
  env.rpc = () => fail("合成预览失败");
  repo.showImportAssistantModal();
  const dialog = modal();
  await buttonNamed(dialog, "文件 / ZIP").dispatch("click"); await tick(); await tick();
  assert.deepEqual(env.hostCalls, [["file.pick", { kind: "file" }]]);
  assert.deepEqual(env.rpcCalls[0], { method: "import.preview", params: { source: { kind: "local", grant: "fg_" + "c".repeat(32) } } });
  env.host = () => null; await buttonNamed(dialog, "文件夹").dispatch("click"); await tick();
  assert.deepEqual(env.hostCalls.at(-1), ["file.pick", { kind: "directory" }]); assert.equal(env.rpcCalls.length, 1);
  env.host = () => Promise.reject(Object.assign(new Error("x"), { code: "unknown-method" }));
  await buttonNamed(dialog, "文件夹").dispatch("click"); await tick();
  assert.equal(descendants(dialog, item => item.className === "advisor-modal-error")[0].textContent, "请在桌面客户端选择文件");
});

test("renaming from the history menu confirms by button, since a sandboxed page cannot submit forms", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.initFrameHandshake();
  hostEnv.rpc = method => method === "threads.list" ? { threads: [{ id: thread().id, agentId: "advisor", title: "旧标题", preview: "" }] } :
    method === "thread.rename" ? { thread: { ...thread(), title: "新标题" } } : {};
  await advisor.loadHistory();
  const row = frameEnv.elements.get("historyList").children[0];
  await row.dispatch("contextmenu", { preventDefault() {}, clientX: 10, clientY: 10 });
  await frameEnv.elements.get("btnRenameThread").dispatch("click");
  frameEnv.elements.get("sessionTitleInput").value = "新标题";
  await frameEnv.elements.get("btnConfirmDialog").dispatch("click"); await tick(); await tick();
  const rename = hostEnv.rpcCalls.find(call => call.method === "thread.rename");
  assert.deepEqual({ ...rename.params, requestId: "" }, { agentId: "advisor", threadId: thread().id, title: "新标题", requestId: "" });
  assert.match(frameHtml, /type="button" class="advisor-ctrl-btn" id="btnConfirmDialog"/);
});

test("@ picks friends from the added chats; only picks still in the text join, and a joined friend can be removed", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), sent = [];
  hostEnv.rpc = async (method, params) => {
    if (method === "friends.list") return { friends: [{ user: "friend-b", name: "小李" }, { user: "friend-c", name: "小李" }, { user: "friend-d", name: "老周" }] };
    if (method === "thread.removeParticipant") return { thread: { ...thread(), participants: [] }, run: null };
    return {};
  };
  advisor.initFrameHandshake(); advisor.startRun = (...args) => sent.push(args);
  const input = frameEnv.elements.get("advisorInput"), menu = frameEnv.elements.get("mentionMenu");
  input.setSelectionRange = (start, end) => { input.selectionStart = start; input.selectionEnd = end; };
  const type = async value => { input.value = value; input.selectionStart = input.selectionEnd = value.length; await input.dispatch("input"); };
  const key = name => input.dispatch("keydown", { key: name, preventDefault() {} });
  await type("name@example");
  assert.equal(menu.hidden, true, "an address is not a mention");
  await type("对比 @");
  assert.equal(menu.hidden, false);
  await tick(); await tick();
  assert.equal(methods(hostEnv).filter(method => method === "friends.list").length, 1);
  await type("对比 @小");
  assert.deepEqual(menu.children.map(button => [button.textContent, button.children[0]?.textContent]), [["小李", "friend-b"], ["小李", "friend-c"]]);
  await key("ArrowDown"); await key("Enter");
  assert.equal(input.value, "对比 @小李 "); assert.equal(menu.hidden, true); assert.equal(sent.length, 0);
  await type(input.value + "和 ＠老"); await key("Tab");
  assert.equal(input.value, "对比 @小李 和 @老周 ", "a Chinese IME's full-width ＠ also picks");
  await type("对比 @小李 和老周，再看 @老周");
  input.value = "对比 @小李 和老周"; await frameEnv.elements.get("btnAdvisorSend").dispatch("click");
  assert.deepEqual(json(sent[0]), ["对比 @小李 和老周", "advisor", null, ["friend-c"], null]);

  advisor.syncThreadToFrame({ ...thread(), participants: [{ user: "friend-c", name: "小李" }] });
  const bar = frameEnv.elements.get("participantBar");
  assert.equal(bar.hidden, false);
  assert.equal(bar.children[0].children[0].textContent, "@小李");
  await bar.children[0].children[1].dispatch("click"); await tick(); await tick();
  const removal = hostEnv.rpcCalls.find(call => call.method === "thread.removeParticipant");
  assert.deepEqual(removal.params, { agentId: "advisor", threadId: thread().id, participant: "friend-c" });
  assert.equal(bar.hidden, true);
});

test("opening a pristine discussion seeds the current contact once and preserves an intentional empty draft across reloads", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  let saved = { text: "", version: 0, mentions: [] };
  env.rpc = async (method, params) => {
    if (method === "draft.save") {
      saved = { text: params.text, version: params.version + 1, scrollTop: params.scrollTop, mentions: params.mentions || [] };
      return { draft: saved };
    }
    return { thread: { ...thread(), draft: saved }, run: null };
  };
  advisor.currentName = "合成好友";
  advisor.acceptDraft(advisor.captureScope(), { ...thread(), draft: saved });
  advisor.initFrameHandshake();
  const input = env.elements.get("advisorInput"), bar = env.elements.get("participantBar");
  assert.equal(input.value, "@合成好友 ");
  assert.equal(bar.children[0].children[0].textContent, "@合成好友");
  assert.equal(bar.children[0].children.length, 1, "the current chat is the discussion's fixed context");
  assert.match(bar.children[0].title, /已自动带入上下文/);
  await advisor.flushDraft();
  assert.equal(saved.version, 1);
  advisor.syncReadyFrame(); assert.equal(input.value, "@合成好友 ");
  input.value = ""; await input.dispatch("input"); await advisor.flushDraft();
  await advisor.loadThread(); assert.equal(input.value, "");

  const { hostEnv: reloaded } = pair(), restored = ready(reloaded);
  restored.currentName = "合成好友";
  restored.acceptDraft(restored.captureScope(), { ...thread(), draft: saved });
  restored.initFrameHandshake();
  assert.equal(reloaded.elements.get("advisorInput").value, "", "deleting the default survives reopening the independent window");
  assert.deepEqual(methods(reloaded), [], "opening the view does not call a model or read private context");
});

test("the default @ never overwrites existing drafts or fills used, queued or read-only discussions", () => {
  for (const value of [
    { ...thread(), draft: { text: "原有输入", version: 0 } },
    { ...thread("account-a", "friend-a", "advisor", [{ id: "old", role: "user", text: "原有问题" }]), draft: { text: "", version: 0 } },
    { ...thread(), queue: queue([queuedItem({ requestId: "pending", message: "排队的问题" })]), draft: { text: "", version: 0 } },
    { ...thread(), readonly: true, draft: { text: "", version: 0 } }
  ]) {
    const { hostEnv: env } = pair(), advisor = ready(env);
    advisor.currentName = "合成好友";
    advisor.setCachedThread("account-a", "friend-a", "advisor", value);
    advisor.acceptDraft(advisor.captureScope(), value);
    advisor.initFrameHandshake();
    assert.equal(env.elements.get("advisorInput").value, value.draft.text);
    assert.equal(env.timers.size, 0, "no seed is saved for an existing discussion");
  }
});

test("saved @ picks restore exact identities for duplicate names and stay isolated from other accounts", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  let saved;
  env.rpc = async (method, params) => {
    if (method === "friends.list") return { friends: [{ user: "friend-b", name: "同名好友" }, { user: "friend-c", name: "同名好友" }] };
    if (method === "draft.save") {
      saved = { text: params.text, version: params.version + 1, scrollTop: 0, mentions: params.mentions || [] };
      return { draft: saved };
    }
    return {};
  };
  advisor.initFrameHandshake();
  const input = env.elements.get("advisorInput");
  input.setSelectionRange = (start, end) => { input.selectionStart = start; input.selectionEnd = end; };
  input.value = "比较 @同名"; input.selectionStart = input.selectionEnd = input.value.length;
  await input.dispatch("input"); await tick(); await tick();
  await input.dispatch("keydown", { key: "ArrowDown", preventDefault() {} });
  await input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  await advisor.flushDraft();
  assert.deepEqual(json(saved.mentions), [{ user: "friend-c", name: "同名好友" }]);

  const { hostEnv: reloaded } = pair(), restored = ready(reloaded), sent = [];
  restored.acceptDraft(restored.captureScope(), { ...thread(), draft: saved });
  restored.initFrameHandshake(); restored.startRun = (...args) => sent.push(args);
  await reloaded.elements.get("btnAdvisorSend").dispatch("click");
  assert.deepEqual(json(sent[0][3]), ["friend-c"], "the restored selection does not resolve the duplicate name again");
  assert.ok(!methods(reloaded).includes("friends.list"));

  ready(reloaded, "account-b", "friend-a");
  const other = { ...thread("account-b", "friend-a"), draft: { text: saved.text, version: 1, mentions: [] } };
  restored.setCachedThread("account-b", "friend-a", "advisor", other);
  restored.acceptDraft(restored.captureScope(), other); restored.initFrameHandshake();
  await reloaded.elements.get("btnAdvisorSend").dispatch("click");
  assert.deepEqual(json(sent[1][3]), [], "matching plain text under another account cannot inherit the selected friend ID");
});

test("removing an @ token clears its saved identity and malformed draft selections are rejected", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const picked = { text: "@合成好友 问题", version: 1, mentions: [{ user: "friend-b", name: "合成好友" }] };
  advisor.acceptDraft(advisor.captureScope(), { ...thread(), draft: picked }); advisor.initFrameHandshake();
  const input = env.elements.get("advisorInput"); input.value = "问题"; await input.dispatch("input");
  assert.deepEqual(json(advisor.draftRecords[advisor.scopeKey("account-a", "friend-a", "advisor", thread().id)].mentions), []);
  assert.equal(advisor.validFrameRequest(request(env, "ADVISOR_DRAFT_UPDATE", { draft: "问题", mentions: [{ user: 7, name: "合成好友" }] })), false);
});

test("question context uses its frozen snapshot, shows two names and expands exact identities and honest source states", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const malicious = "<img src=x onerror=alert(1)>";
  const snapshot = contextSnapshot([
    contextContact("friend-a", "提问时的名字", { primary: true, access: "required" }),
    contextContact("friend-b", "同名好友"), contextContact("friend-c", "同名好友", { sources: [], status: "empty" }),
    contextContact("friend-d", malicious, { sources: [], status: "unavailable" })
  ]);
  const value = { ...thread("account-a", "friend-a", "advisor", [{ id: "scope-q", role: "user", text: "合成问题", contextSnapshot: snapshot }]),
    participants: [{ user: "unrelated", name: "当前新增好友" }] };
  advisor.currentName = "现在的新名字"; advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  const context = messageContext(env, "scope-q"), [button, details] = context.children;
  assert.equal(button.tag, "button"); assert.equal(button.type, "button");
  assert.deepEqual(button.children.filter(child => child.className === "advisor-context-name").map(child => child.textContent), ["@提问时的名字", "@同名好友"]);
  assert.equal(button.children.find(child => child.className === "advisor-context-count").textContent, "+2");
  assert.equal(button.attributes["aria-controls"], details.id); assert.equal(details.hidden, true);
  await button.dispatch("click");
  assert.equal(details.hidden, false); assert.equal(button.attributes["aria-expanded"], "true");
  const rows = details.children.filter(child => child.className === "advisor-context-contact");
  assert.deepEqual(rows.map(row => row.dataset.user), ["friend-a", "friend-b", "friend-c", "friend-d"]);
  assert.deepEqual(rows.map(row => row.children.find(child => child.className === "advisor-context-sources").textContent),
    ["已提供 · 聊天记录、好友资料", "可按需读取 · 聊天记录、好友资料", "无可用资料", "资料不可用，本轮未提供"]);
  assert.deepEqual(rows.slice(1, 3).map(row => row.children.find(child => child.className === "advisor-context-identity").textContent), ["friend-b", "friend-c"]);
  assert.equal(rows[3].children[0].textContent, "@" + malicious);
  assert.deepEqual(rows[3].children[0].children, [], "untrusted names are text nodes, never HTML");
  assert.match(details.children.at(-1).textContent, /不代表模型已读取/);
  advisor.currentName = "再改名"; advisor.syncThreadToFrame({ ...value, participants: [] });
  assert.equal(messageContext(env, "scope-q"), context, "current participants and display names never rewrite the saved scope");
  assert.equal(details.hidden, false);
});

test("context disclosure state survives same-discussion rerenders but is isolated by account and closes with Escape", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const messages = [{ id: "same-id", role: "user", text: "合成问题", contextSnapshot: contextSnapshot([contextContact("friend-a", "合成好友")]) }];
  const value = thread("account-a", "friend-a", "advisor", messages);
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  await messageContext(env, "same-id").children[0].dispatch("click");
  advisor.generation++; advisor.syncThreadToFrame(value);
  assert.equal(messageContext(env, "same-id").children[1].hidden, false, "rebuilding a view retains this question's disclosure state");
  ready(env, "account-b", "friend-a");
  advisor.setCachedThread("account-b", "friend-a", "advisor", thread("account-b", "friend-a", "advisor", messages)); advisor.initFrameHandshake();
  assert.equal(messageContext(env, "same-id").children[1].hidden, true, "the same message id on another account starts collapsed");
  ready(env); advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  const [button, details] = messageContext(env, "same-id").children;
  assert.equal(details.hidden, false);
  let prevented = false; await button.dispatch("keydown", { key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(details.hidden, true); assert.equal(button.focused, true);
  assert.equal(button.attributes["aria-expanded"], "false");
});

test("missing or malformed snapshots remain unknown while optimistic and admitted preparing questions stay explicit", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const pending = contextSnapshot([contextContact("friend-a", "准备中的好友", { status: "pending", sources: [] })], "pending");
  const malformed = contextSnapshot([contextContact("friend-z", "不能据此展示", { sources: ["invented"] })]);
  const value = thread("account-a", "friend-a", "advisor", [
    { id: "legacy-q", role: "user", text: "旧问题", contextSnapshot: null },
    { id: "bad-q", role: "user", text: "仍可读取的问题", contextSnapshot: malformed },
    { id: "optimistic-q", role: "user", text: "@某人 待确认的问题", delivery: "pending" },
    { id: "admitted-q", role: "user", text: "正在准备的问题", contextSnapshot: pending }
  ]);
  advisor.currentName = "当前好友不能倒填";
  value.participants = [contextContact("friend-other", "后来加入的人")];
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  assert.equal(messageContext(env, "legacy-q").textContent, "上下文未记录");
  assert.equal(messageContext(env, "bad-q").textContent, "上下文未记录", "bad metadata never hides the transcript or claims a fabricated scope");
  assert.equal(messageContext(env, "optimistic-q").textContent, "上下文待确认");
  const [button, details] = messageContext(env, "admitted-q").children;
  assert.equal(button.children[0].textContent, "上下文准备中：");
  await button.dispatch("click");
  assert.equal(details.children[0].children.at(-1).textContent, "资料准备中，尚未确认可用范围");
});

test("an admitted local question uses only the backend snapshot and polling updates its source status before the answer finishes", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), scope = advisor.captureScope();
  const initial = contextSnapshot([contextContact("friend-a", "冻结好友", { sources: [], status: "pending" })], "pending");
  const final = contextSnapshot([contextContact("friend-a", "冻结好友", { sources: ["profile"] })]);
  const run = { id: "snapshot-run", threadId: scope.threadId, messageId: "admitted-q", state: "running", contextSnapshot: initial };
  const pending = { requestId: "snapshot-req", threadId: scope.threadId, message: "合成问题", mentions: ["friend-b"] };
  advisor.pendingRequests[advisor.queueKey(scope)] = pending;
  advisor.acknowledgePending(scope, pending, run); advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), run); advisor.initFrameHandshake();
  assert.equal(messageContext(env, "admitted-q").children[0].children[0].textContent, "上下文准备中：");
  let nextPoll;
  env.rpc = method => method === "run.events" ? { events: [], run: { ...run, contextSnapshot: final } } : {};
  nextPoll = advisor.pollEvents(run.id, advisor.captureScope()); await tick(); await tick();
  const [button, details] = messageContext(env, "admitted-q").children;
  assert.equal(button.children[0].textContent, "本轮上下文：");
  await button.dispatch("click"); assert.equal(details.children[0].children.at(-1).textContent, "已提供 · 好友资料");
  const cached = advisor.getCachedThread("account-a", "friend-a", "advisor").messages.find(message => message.id === "admitted-q");
  assert.deepEqual(json(cached.contextSnapshot), final);
  advisor.abortCurrentPoll(); await env.flushTimers(); await nextPoll;
});

test("preparation status updates preserve the focused context toggle and its expanded state", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const question = { id: "focused-q", role: "user", text: "合成问题", contextSnapshot:
    contextSnapshot([contextContact("friend-a", "合成好友", { sources: [], status: "pending" })], "pending") };
  const value = thread("account-a", "friend-a", "advisor", [question]);
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  const previousToggle = messageContext(env, "focused-q").children[0];
  await previousToggle.dispatch("click"); env.document.activeElement = previousToggle;
  advisor.syncThreadToFrame({ ...value, messages: [{ ...question, contextSnapshot:
    contextSnapshot([contextContact("friend-a", "合成好友")]) }] });
  const [toggle, details] = messageContext(env, "focused-q").children;
  assert.notEqual(toggle, previousToggle); assert.equal(toggle.focused, true);
  assert.equal(toggle.attributes["aria-expanded"], "true"); assert.equal(details.hidden, false);
});

test("enqueue carries picked friends and freezes them with thinking mode when acknowledgement is lost", async () => {
  const env = host(), advisor = ready(env);
  env.rpc = () => fail("连接超时");
  await advisor.startRun("对比 @小李", "advisor", null, ["friend-b"]);
  advisor.thinkingEnabled = false;
  await advisor.retryQueue();
  assert.deepEqual(env.rpcCalls.filter(call => call.method === "queue.enqueue").map(call => call.params.mentions), [["friend-b"], ["friend-b"]]);
  assert.deepEqual(env.rpcCalls.filter(call => call.method === "queue.enqueue").map(call => call.params.thinking), [true, true]);
  assert.equal(advisor.validFrameRequest(request(env, "ADVISOR_SEND", { text: "问题", agentId: "advisor", mentions: [7] })), false);
  assert.equal(advisor.validFrameRequest(request(env, "ADVISOR_SEND", { text: "问题", agentId: "advisor", mentions: ["friend-b"] })), true);
});

test("queued mentions become joined friends only when the backend starts that question", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  hostEnv.rpc = async (method, params) => method === "queue.enqueue" ? enqueueAck(params) :
    method === "queue.get" ? { queue: queue([], 2) } :
    { thread: { ...thread(), participants: [{ user: "friend-b", name: "小李" }], queue: queue([], 2) }, run: null };
  advisor.initFrameHandshake();
  await advisor.startRun("对比 @小李", "advisor", null, ["friend-b"]);
  assert.equal(frameEnv.elements.get("participantBar").hidden, true);
  await advisor.refreshQueue();
  assert.deepEqual(json(advisor.getCachedThread("account-a", "friend-a", "advisor", thread().id).participants), [{ user: "friend-b", name: "小李" }]);
  assert.equal(frameEnv.elements.get("participantBar").hidden, false);
});

test("enqueue during a live answer keeps stop independent and clears only the acknowledged composer version", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), accepted = deferred();
  const run = { id: "live", threadId: thread().id, state: "running" };
  advisor.currentRunId = run.id; advisor.syncThreadToFrame(thread(), run); advisor.initFrameHandshake();
  const input = env.elements.get("advisorInput"); input.value = "下一条"; await input.dispatch("input");
  env.rpc = () => accepted.promise;
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
  assert.equal(input.value, "下一条"); assert.equal(env.elements.get("btnStopRun").hidden, false);
  assert.equal(env.elements.get("sendLabel").textContent, "加入队列");
  input.value = "再下一条草稿"; await input.dispatch("input");
  advisor.syncThreadToFrame(thread(), run);
  assert.equal(env.elements.get("btnAdvisorSend").disabled, true, "a read cannot release an outstanding durable acknowledgement");
  accepted.resolve(enqueueAck(env.rpcCalls.find(call => call.method === "queue.enqueue").params)); await tick(); await tick();
  assert.equal(input.value, "再下一条草稿"); assert.equal(advisor.currentRunId, run.id);
  assert.equal(env.elements.get("btnStopRun").hidden, false);
  assert.deepEqual(methods(env), ["queue.enqueue"]);
  assert.equal(env.elements.get("queueCount").textContent, "待处理 1");
});

test("a timeline scroll while enqueue acknowledgement is pending does not retain the already accepted question in the composer", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), accepted = deferred();
  env.rpc = (method, params) => method === "queue.enqueue" ? accepted.promise :
    method === "draft.save" ? { draft: { text: params.text, version: params.version + 1, scrollTop: params.scrollTop, mentions: params.mentions || [] } } : {};
  advisor.initFrameHandshake();
  const input = env.elements.get("advisorInput"), timeline = env.elements.get("advisorTimeline");
  input.value = "已经发出的合成问题"; await input.dispatch("input");
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
  const enqueue = env.rpcCalls.find(call => call.method === "queue.enqueue");
  timeline.scrollTop = 87; await timeline.dispatch("scroll"); await env.flushTimers(); await tick(); await tick();
  assert.equal(input.value, enqueue.params.message, "the question remains until durable acceptance");
  accepted.resolve(enqueueAck(enqueue.params)); await tick(); await tick();
  assert.equal(input.value, "", "saving only the scroll position must not count as new typing");
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", thread().id), "");
});

test("real text edits during a delayed enqueue preserve the new draft even when restored to the exact submitted text", async () => {
  for (const restoreOriginal of [false, true]) {
    const { hostEnv: env } = pair(), advisor = ready(env), accepted = deferred();
    env.rpc = (method, params) => method === "queue.enqueue" ? accepted.promise :
      method === "draft.save" ? { draft: { text: params.text, version: params.version + 1 } } : {};
    advisor.initFrameHandshake();
    const input = env.elements.get("advisorInput"), timeline = env.elements.get("advisorTimeline");
    input.value = "原合成问题"; await input.dispatch("input");
    await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
    const enqueue = env.rpcCalls.find(call => call.method === "queue.enqueue");
    input.value = "确实编辑过的新草稿"; await input.dispatch("input");
    if (restoreOriginal) { input.value = "原合成问题"; await input.dispatch("input"); }
    timeline.scrollTop = 63; await timeline.dispatch("scroll"); await env.flushTimers(); await tick(); await tick();
    const expected = input.value;
    accepted.resolve(enqueueAck(enqueue.params)); await tick(); await tick();
    assert.equal(input.value, expected, "a real edit is distinct from scroll bookkeeping, including edit-and-restore");
    assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", thread().id), expected);
  }
});

test("changing only the selected identity of a same-named @ friend preserves that draft after delayed acceptance", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), accepted = deferred();
  env.rpc = (method, params) => method === "queue.enqueue" ? accepted.promise :
    method === "draft.save" ? { draft: { text: params.text, version: params.version + 1, mentions: params.mentions || [] } } : {};
  advisor.initFrameHandshake();
  const draft = "比较 @同名好友 的看法";
  advisor.receive(request(env, "ADVISOR_DRAFT_UPDATE", { draft, agentId: "advisor", mentions: [{ user: "friend-b", name: "同名好友" }] }));
  advisor.syncReadyFrame();
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
  const enqueue = env.rpcCalls.find(call => call.method === "queue.enqueue");
  assert.deepEqual(enqueue.params.mentions, ["friend-b"]);
  advisor.receive(request(env, "ADVISOR_DRAFT_UPDATE", { draft, agentId: "advisor", mentions: [{ user: "friend-c", name: "同名好友" }] }));
  accepted.resolve(enqueueAck(enqueue.params)); await tick(); await tick();
  assert.equal(env.elements.get("advisorInput").value, draft);
  const stored = advisor.draftRecords[advisor.scopeKey("account-a", "friend-a", "advisor", thread().id)];
  assert.deepEqual(json(stored.mentions), [{ user: "friend-c", name: "同名好友" }]);
});

test("scroll persistence during an in-flight draft save is written after that save acknowledges unchanged text", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), firstSave = deferred();
  let writes = 0;
  env.rpc = (method, params) => {
    if (method !== "draft.save") return {};
    return ++writes === 1 ? firstSave.promise : { draft: { text: params.text, version: params.version + 1, scrollTop: params.scrollTop } };
  };
  advisor.initFrameHandshake();
  const input = env.elements.get("advisorInput"), timeline = env.elements.get("advisorTimeline");
  input.value = "正文没有变化的合成草稿"; await input.dispatch("input");
  const saving = advisor.flushDraft(); await tick();
  const initial = env.rpcCalls.find(call => call.method === "draft.save");
  timeline.scrollTop = 211; await timeline.dispatch("scroll"); await env.flushTimers();
  firstSave.resolve({ draft: { text: initial.params.text, version: initial.params.version + 1, scrollTop: initial.params.scrollTop } });
  await saving;
  const saves = env.rpcCalls.filter(call => call.method === "draft.save");
  assert.equal(saves.length, 2, "the newer scroll position still requires a follow-up write");
  assert.equal(saves[1].params.text, initial.params.text);
  assert.equal(saves[1].params.scrollTop, 211);
  assert.equal(saves[1].params.version, initial.params.version + 1);
});

test("uncertain admission freezes its nonce and input metadata; definitive rejection permits a corrected question", async () => {
  const env = host(), advisor = ready(env);
  env.rpc = () => fail("暂未收到确认");
  await advisor.startRun("原问题", "advisor", null, ["friend-b"]);
  const original = env.rpcCalls[0].params;
  advisor.setDraft("account-a", "friend-a", "advisor", "修改后的草稿");
  await advisor.startRun("修改后的草稿");
  assert.equal(env.rpcCalls.length, 1); assert.match(advisor.queueProblems[advisor.queueKey()], /先重试确认/);
  advisor.thinkingEnabled = false; env.rpc = (method, params) => enqueueAck(params);
  await advisor.retryQueue();
  assert.deepEqual(env.rpcCalls[1].params, original);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor"), "修改后的草稿");
  assert.equal(advisor.pendingEnqueues[advisor.queueKey()], undefined);
  env.rpc = () => fail("引用联系人无效", "invalid-request");
  await advisor.startRun("引用有误");
  assert.equal(advisor.pendingEnqueues[advisor.queueKey()], undefined);
  env.rpc = (method, params) => enqueueAck(params);
  await advisor.startRun("修正引用");
  assert.notEqual(env.rpcCalls[2].params.requestId, env.rpcCalls[3].params.requestId);
});

test("queue polling discovers background starts without an active run and does not reread an unchanged discussion", async () => {
  const env = host(), advisor = ready(env);
  let version = 1, running = false;
  env.rpc = method => method === "queue.get" ? { queue: queue([], version) } :
    { thread: { ...thread(), queue: queue([], version) }, run: running ? { id: "dequeued", threadId: thread().id, state: "running" } : null };
  await advisor.refreshQueue(); await advisor.refreshQueue();
  assert.deepEqual(methods(env), ["queue.get", "thread.get", "queue.get"]);
  running = true; version++;
  await advisor.refreshQueue();
  assert.equal(advisor.activeRunId(), "dequeued");
  const count = methods(env).filter(method => method === "thread.get").length;
  await advisor.refreshQueue();
  assert.equal(methods(env).filter(method => method === "thread.get").length, count);
  const before = advisor.captureScope(), pending = deferred(); env.rpc = () => pending.promise;
  const oldPoll = advisor.refreshQueue(before);
  advisor.generation++; advisor.currentThreadId = "other-discussion";
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...thread(), id: "other-discussion", queue: queue([], 0) });
  pending.resolve({ queue: queue([], 99, true, "failed") }); await oldPoll;
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor", "other-discussion").queue.version, 0);
});

test("terminal projections cannot roll a newer queue backwards", () => {
  const env = host(), advisor = ready(env), scope = advisor.captureScope();
  advisor.acceptQueue(scope, queue([], 6, true, "manual"));
  advisor.rememberProjection(scope, { thread: { ...thread(), queue: queue([], 3, false) }, run: null });
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").queue.version, 6);
  advisor.acceptQueue(scope, queue([], 2, false));
  assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").queue.paused, true);
});

test("queue editing and removal are versioned, scoped and leave the composer and original metadata intact", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const item = queuedItem({ requestId: "q-one", message: "原正文 @小李", thinking: false, mentions: ["friend-b"] });
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...thread(), queue: queue([item], 1) });
  advisor.initFrameHandshake(); const input = env.elements.get("advisorInput"); input.value = "独立的下一条草稿"; await input.dispatch("input");
  await env.elements.get("btnQueueToggle").dispatch("click");
  const rows = env.elements.get("queueList"); const editButton = rows.children[0].children[1].children[1];
  await editButton.dispatch("click");
  assert.equal(env.elements.get("queueEditor").hidden, false);
  assert.match(env.elements.get("queueEditStatus").textContent, /仅修改正文，保留提交时的 @好友和思考模式/);
  assert.equal(env.elements.get("queuePanel").scrollTop, env.elements.get("queuePanel").scrollHeight);
  const editor = env.elements.get("queueEditInput"); editor.value = "改后的正文"; await editor.dispatch("input");
  advisor.syncQueueToFrame();
  assert.equal(rows.children[0].children[1].children[1], editButton, "unchanged polls must not recreate focused row controls");
  env.rpc = (method, params) => method === "queue.edit" ? { queue: queue([{ ...item, message: params.message, version: 2 }], 2) } : { queue: queue([], 3) };
  await env.elements.get("btnQueueSave").dispatch("click"); await tick();
  assert.deepEqual(env.rpcCalls[0].params, { agentId: "advisor", threadId: thread().id, itemId: item.id, version: 1, message: "改后的正文" });
  assert.equal(env.elements.get("queueEditor").hidden, true); assert.equal(input.value, "独立的下一条草稿");
  assert.deepEqual(json(advisor.getCachedThread("account-a", "friend-a", "advisor").queue.items[0].mentions), ["friend-b"]);
  const remove = rows.children[0].children[1].children[2]; await remove.dispatch("click"); await tick();
  assert.deepEqual(env.rpcCalls[1].params, { agentId: "advisor", threadId: thread().id, itemId: item.id, version: 2 });
  assert.equal(input.value, "独立的下一条草稿");
});

test("an edit conflict preserves local text, refreshes version, and disables editing once claimed", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const item = queuedItem({ requestId: "q-one", message: "原正文" });
  let latest = { ...item, version: 2, message: "另一窗口的正文" }, attempts = 0;
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...thread(), queue: queue([item], 1) }); advisor.initFrameHandshake();
  await env.elements.get("btnQueueToggle").dispatch("click");
  await env.elements.get("queueList").children[0].children[1].children[1].dispatch("click");
  const editor = env.elements.get("queueEditInput"); editor.value = "我的编辑未丢失"; await editor.dispatch("input");
  env.rpc = (method, params) => {
    if (method === "queue.edit") { attempts++; return fail(attempts === 1 ? "版本冲突" : "已开始处理", "queue-conflict"); }
    if (method === "queue.get") return { queue: queue([latest], latest.version + 1) };
    return { thread: { ...thread(), queue: queue([latest], latest.version + 1) }, run: null };
  };
  await env.elements.get("btnQueueSave").dispatch("click"); await tick(); await tick();
  assert.equal(editor.value, "我的编辑未丢失"); assert.match(env.elements.get("queueEditStatus").textContent, /版本冲突/);
  latest = { ...latest, state: "starting", version: 3 };
  await env.elements.get("btnQueueSave").dispatch("click"); await tick(); await tick();
  const edits = env.rpcCalls.filter(call => call.method === "queue.edit");
  assert.deepEqual(edits.map(call => call.params.version), [1, 2]);
  assert.equal(editor.value, "我的编辑未丢失"); assert.equal(editor.disabled, true);
  assert.equal(env.elements.get("btnQueueSave").disabled, true); assert.match(env.elements.get("queueEditStatus").textContent, /已开始处理/);
});

test("pause is backend acknowledged and does not stop the active answer; paused recovery never auto-resumes", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), result = deferred();
  const item = queuedItem({ requestId: "q-pending", message: "随后处理" });
  const value = { ...thread(), queue: queue([item], 1) }, run = { id: "live", threadId: value.id, state: "running" };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.currentRunId = run.id;
  advisor.syncThreadToFrame(value, run); advisor.initFrameHandshake();
  env.rpc = () => result.promise;
  await env.elements.get("btnQueuePause").dispatch("click"); await tick();
  assert.equal(env.elements.get("btnQueuePause").disabled, true); assert.equal(env.elements.get("queueStatus").textContent, "按顺序处理");
  assert.deepEqual(methods(env), ["queue.pause"]);
  result.resolve({ queue: queue([item], 2, true, "manual") }); await tick();
  assert.equal(advisor.currentRunId, "live"); assert.equal(env.elements.get("btnQueuePause").textContent, "继续");
  assert.deepEqual(methods(env), ["queue.pause"]);
  advisor.acceptQueue(advisor.captureScope(), queue([item], 3, true, "restart")); advisor.initFrameHandshake();
  assert.equal(env.elements.get("queueStatus").textContent, "重启后已暂停"); assert.deepEqual(methods(env), ["queue.pause"]);
  env.rpc = () => ({ queue: queue([item], 4) });
  await env.elements.get("btnQueuePause").dispatch("click"); await tick();
  assert.deepEqual(methods(env), ["queue.pause", "queue.resume"]);
});

test("read-only histories expose queued text with no mutation controls and reject stale item actions", () => {
  const { hostEnv: env } = pair(), advisor = ready(env), scope = json(advisor.captureScope());
  const item = queuedItem({ requestId: "q-pending", message: "只读待处理" });
  const value = { ...thread(), readonly: true, readonlyReason: "原助手已停用", queue: queue([item], 1, true, "agent-unavailable") };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  assert.equal(env.elements.get("btnQueuePause").hidden, true);
  assert.equal(env.elements.get("queueList").children[0].children[1].children.length, 1);
  assert.equal(env.elements.get("queueStatus").textContent, "助手已停用或删除");
  advisor.receive({ type: "ADVISOR_QUEUE_REMOVE", payload: { itemId: item.id, version: 1 }, scope });
  advisor.generation++;
  advisor.receive({ type: "ADVISOR_QUEUE_EDIT", payload: { itemId: item.id, version: 1, message: "越界" }, scope });
  assert.deepEqual(methods(env), []);
});

test("failed and stopped original retries keep a separate draft and reuse retryRunId after a lost acknowledgement", async () => {
  for (const state of ["error", "stopped"]) for (const draft of ["", "稍后才要发送的草稿"]) {
    const { hostEnv: env } = pair(), advisor = ready(env);
    const value = { ...thread("account-a", "friend-a", "advisor", [{ id: "original-user", role: "user", text: "原问题" }]),
      queue: queue([queuedItem({ requestId: "q-followup", message: "已排队的后续问题" })], 1, true, "failed") };
    const run = { id: "failed-original", threadId: value.id, messageId: "original-user", state };
    advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.setDraft("account-a", "friend-a", "advisor", draft);
    advisor.syncThreadToFrame(value, run); advisor.initFrameHandshake();
    assert.equal(env.elements.get("btnRetryRun").hidden, false);
    env.rpc = () => fail("原问题重试回执丢失");
    await advisor.retryRun(); advisor.thinkingEnabled = false; await advisor.retryRun();
    const calls = env.rpcCalls.filter(call => call.method === "run.start");
    assert.equal(calls.length, 2); assert.equal(calls[0].params.retryRunId, run.id);
    assert.deepEqual(calls[1].params, calls[0].params);
    assert.equal(env.elements.get("advisorInput").value, draft);
    assert.equal(advisor.getCachedThread("account-a", "friend-a", "advisor").queue.paused, true);
    assert.ok(!methods(env).includes("queue.enqueue")); assert.ok(!methods(env).includes("queue.resume"));
  }
});

test("a quote from the chat's right-click menu names who wrote the message", () => {
  const draftFor = value => {
    const env = host(), advisor = ready(env);
    advisor.quoteMessage({ account: "account-a", user: "friend-a", messageId: "m1", text: "原话", ...value });
    return advisor.getDraft("account-a", "friend-a", "advisor", advisor.currentThreadId);
  };
  assert.ok(draftFor({ side: "self" }).startsWith("关于我发的这条消息（m1）：\n原话"));
  assert.ok(draftFor({ side: "other", sender: "小李" }).startsWith("关于小李的这条消息（m1）："));
  assert.ok(draftFor({ side: "other" }).startsWith("关于对方这条消息（m1）："));
  assert.ok(draftFor({}).startsWith("关于对方这条消息（m1）："), "分析与建议 keeps its wording");
});

// Added 2026-10-15: regenerate the latest answer, or ask the latest question again in other words.
function answered() {
  return { ...thread("account-a", "friend-a", "advisor", [
    { id: "q1", seq: 1, role: "user", text: "她为什么不回我", runId: "run-1" },
    { id: "a1", seq: 2, role: "assistant", text: "可能在忙", runId: "run-1" }]) };
}
const done = { id: "run-1", threadId: thread().id, state: "done" };
const actions = env => env.elements.get("advisorTimeline").children
  .flatMap(child => child.children.filter(node => node.className === "advisor-msg-actions").map(node => [child.dataset.msgId, node.dataset.kind]));
test("the latest turn offers 编辑 and 重新生成 only while nothing runs or waits", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.setCachedThread("account-a", "friend-a", "advisor", answered());
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), done); advisor.initFrameHandshake();
  assert.deepEqual(actions(frameEnv), [["q1", "edit"], ["a1", "regenerate"]]);
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), { ...done, state: "running" });
  assert.deepEqual(actions(frameEnv), []);
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), { ...done, state: "stopped" });
  assert.deepEqual(actions(frameEnv), [["q1", "edit"]], "a stopped turn can be asked again but has no answer to regenerate");
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...answered(), queue: queue([queuedItem({ requestId: "q-one", message: "排队中" })], 1) });
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), done);
  assert.deepEqual(actions(frameEnv), []);
  assert.equal(advisor.validFrameRequest(request(hostEnv, "ADVISOR_REGENERATE")), false);
});

test("重新生成 sends the saved question for the latest run, keeps the typed draft and folds the old answer", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv), response = deferred();
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...answered(), _advisorRun: done });
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), done); advisor.initFrameHandshake();
  hostEnv.rpc = method => method === "run.start" ? response.promise : new Promise(() => {});
  const input = frameEnv.elements.get("advisorInput"); input.value = "正在写的另一个问题"; await input.dispatch("input");
  const row = frameEnv.elements.get("advisorTimeline").children.find(child => child.dataset.msgId === "a1");
  await row.children.find(node => node.className === "advisor-msg-actions").children[0].dispatch("click");
  const sent = hostEnv.rpcCalls.find(call => call.method === "run.start").params;
  assert.deepEqual([sent.message, sent.regenerateRunId, sent.editRunId], ["她为什么不回我", "run-1", undefined]);
  assert.equal(input.value, "正在写的另一个问题");
  assert.equal(frameEnv.elements.get("advisorTimeline").children.filter(child => child.className === "advisor-msg user").length, 1,
    "no second copy of the question");
  response.resolve({ run: { id: "run-2", threadId: thread().id, state: "running", messageId: "q1", requestId: sent.requestId }, threadId: thread().id });
  await tick(); await tick();
  const timeline = frameEnv.elements.get("advisorTimeline").children;
  const folded = timeline.find(child => child.className === "advisor-superseded");
  assert.ok(folded, "the old answer folds away at once");
  assert.equal(folded.children[0].textContent, "上一版回答");
  assert.ok(!timeline.some(child => child.dataset.msgId === "a1"));
});

test("编辑 puts the question into the input; sending replaces the turn and 取消 restores the draft", async () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...answered(), _advisorRun: done });
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), done); advisor.initFrameHandshake();
  hostEnv.rpc = () => new Promise(() => {});
  const input = frameEnv.elements.get("advisorInput"), bar = frameEnv.elements.get("editBar");
  input.value = "原来的草稿"; await input.dispatch("input");
  const edit = () => frameEnv.elements.get("advisorTimeline").children.find(child => child.dataset.msgId === "q1")
    .children.find(node => node.className === "advisor-msg-actions").children[0].dispatch("click");
  await edit();
  assert.equal(input.value, "她为什么不回我"); assert.equal(bar.hidden, false);
  assert.deepEqual(actions(frameEnv), [], "no actions while editing");
  await frameEnv.elements.get("btnCancelEdit").dispatch("click");
  assert.equal(input.value, "原来的草稿"); assert.equal(bar.hidden, true);
  await edit();
  input.value = "她两天没回我，是不是生气了"; await input.dispatch("input");
  await frameEnv.elements.get("btnAdvisorSend").dispatch("click"); await tick();
  const sent = hostEnv.rpcCalls.find(call => call.method === "run.start").params;
  assert.deepEqual([sent.message, sent.editRunId, sent.regenerateRunId], ["她两天没回我，是不是生气了", "run-1", undefined]);
  assert.equal(bar.hidden, false, "the edit remains recoverable until acknowledgement");
  // A stale edit request naming another run is refused by the controller.
  assert.equal(advisor.validFrameRequest(request(hostEnv, "ADVISOR_SEND", { text: "x", agentId: "advisor", editRunId: "run-0" })), false);
});

test("editing a question never reconstructs @ identities from duplicate names and retains only explicit per-turn IDs", async () => {
  for (const mentions of [undefined, ["friend-c"], [{ user: "friend-c", name: "同名好友" }]]) {
    const { hostEnv: env } = pair(), advisor = ready(env), sent = [];
    const value = { ...answered(), participants: [{ user: "friend-b", name: "同名好友" }, { user: "friend-c", name: "同名好友" }] };
    value.messages[0] = { ...value.messages[0], text: "@同名好友 的想法是什么", ...(mentions ? { mentions } : {}) };
    advisor.setCachedThread("account-a", "friend-a", "advisor", value);
    advisor.syncThreadToFrame(value, done); advisor.initFrameHandshake();
    advisor.startRun = (...args) => sent.push(args);
    const question = env.elements.get("advisorTimeline").children.find(child => child.dataset.msgId === "q1");
    await question.children.find(child => child.className === "advisor-msg-actions").children[0].dispatch("click");
    await env.elements.get("btnAdvisorSend").dispatch("click");
    assert.deepEqual(json(sent[0][3]), mentions ? ["friend-c"] : []);
    assert.deepEqual(json(value.participants).map(item => item.user), ["friend-b", "friend-c"], "existing discussion context is preserved");
  }
});

test("replaced messages from the service render as one collapsed entry and are not the latest turn", () => {
  const { hostEnv, frameEnv } = pair(), advisor = ready(hostEnv);
  const saved = thread("account-a", "friend-a", "advisor", [
    { id: "q1", seq: 1, role: "user", text: "吃饭合适吗", runId: "run-1", supersededBy: "run-2" },
    { id: "a1", seq: 2, role: "assistant", text: "合适", runId: "run-1", supersededBy: "run-2" },
    { id: "q2", seq: 3, role: "user", text: "看电影合适吗", runId: "run-2" },
    { id: "a2", seq: 4, role: "assistant", text: "也可以", runId: "run-2" }]);
  advisor.setCachedThread("account-a", "friend-a", "advisor", saved);
  advisor.syncThreadToFrame(saved, { id: "run-2", threadId: thread().id, state: "done" }); advisor.initFrameHandshake();
  const timeline = frameEnv.elements.get("advisorTimeline").children;
  assert.deepEqual(timeline.filter(child => child.dataset.msgId !== "ui:run").map(child => child.className === "advisor-superseded" ? "folded" : child.dataset.msgId),
    ["folded", "q2", "a2"]);
  assert.equal(timeline[0].children[0].textContent, "修改前的提问");
  assert.match(descendantText(timeline[0].children[1]), /问：吃饭合适吗[\s\S]*合适/);
  assert.deepEqual(actions(frameEnv), [["q2", "edit"], ["a2", "regenerate"]]);
  assert.deepEqual(json(advisor.latestTurn()), { runId: "run-2", question: saved.messages[2], answer: saved.messages[3] });
});

test("superseded question versions retain their own context disclosures adjacent to their original text", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env);
  const original = contextSnapshot([contextContact("friend-old", "原范围好友")]);
  const edited = contextSnapshot([contextContact("friend-new", "修改时加入的好友")]);
  const messages = [
    { id: "old-q", seq: 1, role: "user", text: "旧问题", runId: "old-run", supersededBy: "new-run", contextSnapshot: original },
    { id: "old-a", seq: 2, role: "assistant", text: "旧回答", runId: "old-run", supersededBy: "new-run" },
    { id: "new-q", seq: 3, role: "user", text: "修改后的问题", runId: "new-run", contextSnapshot: edited }
  ];
  const value = thread("account-a", "friend-a", "advisor", messages);
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.initFrameHandshake();
  const folded = env.elements.get("advisorTimeline").children.find(node => node.className === "advisor-superseded");
  assert.equal(folded.children[0].textContent, "修改前的提问");
  const question = folded.children[1].children.find(node => node.dataset.msgId === "old-q");
  assert.equal(question.children[0].textContent, "问：旧问题");
  const context = question.children[1];
  await context.children[0].dispatch("click");
  assert.equal(context.children[1].children[0].dataset.user, "friend-old");
  assert.ok(!descendantText(context).includes("修改时加入的好友"));
  assert.equal(messageContext(env, "new-q").children[0].children[1].textContent, "@修改时加入的好友");
});

test("editing retains an explicit mention by its frozen identity even after that contact leaves the discussion", async () => {
  const { hostEnv: env } = pair(), advisor = ready(env), sent = [];
  const value = { ...answered(), participants: [{ user: "same-name-other", name: "同名好友" }] };
  value.messages[0] = { ...value.messages[0], text: "@同名好友 的想法是什么", mentions: ["original-friend"],
    contextSnapshot: contextSnapshot([contextContact("original-friend", "同名好友")]) };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value);
  advisor.syncThreadToFrame(value, done); advisor.initFrameHandshake(); advisor.startRun = (...args) => sent.push(args);
  const question = env.elements.get("advisorTimeline").children.find(child => child.dataset.msgId === "q1");
  await question.children.find(child => child.className === "advisor-msg-actions").children[0].dispatch("click");
  await env.elements.get("btnAdvisorSend").dispatch("click");
  assert.deepEqual(json(sent[0][3]), ["original-friend"]);
});
function preparedEdit() {
  const { hostEnv: env } = pair(), advisor = ready(env);
  advisor.setCachedThread("account-a", "friend-a", "advisor", { ...answered(), _advisorRun: done });
  advisor.syncThreadToFrame(advisor.getCachedThread("account-a", "friend-a", "advisor"), done); advisor.initFrameHandshake();
  return { env, advisor, input: env.elements.get("advisorInput"),
    edit: () => env.elements.get("advisorTimeline").children.find(child => child.dataset.msgId === "q1")
      .children.find(node => node.className === "advisor-msg-actions").children[0].dispatch("click") };
}

test("replacement retries keep their request identity, but another operation or target never inherits it", async () => {
  const { env, advisor } = preparedEdit(); env.rpc = () => fail("回执暂不可用");
  const message = answered().messages[0].text;
  await advisor.startRun(message, "advisor", null, [], { mode: "edit", runId: "run-1" });
  await advisor.startRun(message, "advisor", null, [], { mode: "edit", runId: "run-1" });
  await advisor.startRun(message, "advisor", null, [], { mode: "regenerate", runId: "run-1" });
  const next = { ...answered(), _advisorRun: { ...done, id: "run-2" } };
  next.messages = next.messages.map(item => ({ ...item, runId: "run-2" }));
  advisor.setCachedThread("account-a", "friend-a", "advisor", next); advisor.syncThreadToFrame(next, next._advisorRun);
  await advisor.startRun(message, "advisor", null, [], { mode: "regenerate", runId: "run-2" });
  await advisor.startRun(message, "advisor", "run-2");
  const calls = env.rpcCalls.filter(call => call.method === "run.start").map(call => call.params);
  assert.equal(calls.length, 5); assert.equal(calls[0].requestId, calls[1].requestId);
  assert.equal(new Set(calls.slice(1).map(call => call.requestId)).size, 4);
  assert.deepEqual(calls.map(call => [call.editRunId, call.regenerateRunId, call.retryRunId]),
    [["run-1", undefined, undefined], ["run-1", undefined, undefined], [undefined, "run-1", undefined],
      [undefined, "run-2", undefined], [undefined, undefined, "run-2"]]);
});

test("an edit failure keeps its text and edit mode; retry reuses its request and cancel restores the original draft", async () => {
  const { env, advisor, input, edit } = preparedEdit(); env.rpc = () => fail("提交失败");
  input.value = "原草稿"; await input.dispatch("input"); await edit();
  input.value = "修改后的问题"; await input.dispatch("input");
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick(); await tick();
  assert.equal(input.value, "修改后的问题"); assert.equal(env.elements.get("editBar").hidden, false);
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick(); await tick();
  const calls = env.rpcCalls.filter(call => call.method === "run.start");
  assert.equal(calls.length, 2); assert.equal(calls[0].params.requestId, calls[1].params.requestId);
  await env.elements.get("btnCancelEdit").dispatch("click");
  assert.equal(input.value, "原草稿"); assert.equal(env.elements.get("editBar").hidden, true);
  assert.ok(!methods(env).includes("queue.enqueue"));
});

test("an accepted edit preserves both the original draft and text typed while the acknowledgement was pending", async () => {
  const { env, advisor, input, edit } = preparedEdit(), response = deferred();
  env.rpc = method => method === "run.start" ? response.promise : new Promise(() => {});
  input.value = "原草稿"; await input.dispatch("input"); await edit();
  input.value = "修改的问题"; await input.dispatch("input"); await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
  assert.equal(input.value, ""); input.value = "下一问"; await input.dispatch("input");
  response.resolve({ run: { id: "run-2", threadId: thread().id, messageId: "q2", state: "running" } }); await tick(); await tick();
  assert.equal(input.value, "原草稿\n\n下一问"); assert.equal(env.elements.get("editBar").hidden, true);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", thread().id), input.value);
});

test("long drafts remain separate and recoverable when edit acknowledgement cannot combine them within the limit", async () => {
  const { env, advisor, input, edit } = preparedEdit(), response = deferred(), previous = "原".repeat(9000), next = "新".repeat(9000);
  env.rpc = method => method === "run.start" ? response.promise : new Promise(() => {});
  input.value = previous; await input.dispatch("input"); await edit();
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick(); input.value = next; await input.dispatch("input");
  response.resolve({ run: { id: "run-2", threadId: thread().id, messageId: "q2", state: "running" } }); await tick(); await tick();
  assert.equal(input.value, next); assert.equal(env.elements.get("btnCancelEdit").textContent, "切换草稿");
  assert.equal(env.elements.get("btnCopyHeldDraft").hidden, false);
  await env.elements.get("btnCopyHeldDraft").dispatch("click"); await tick();
  assert.deepEqual(env.hostCalls.at(-1), ["clipboard.copy", { text: previous }]);
  await env.elements.get("btnCancelEdit").dispatch("click"); assert.equal(input.value, previous);
  await env.elements.get("btnCancelEdit").dispatch("click"); assert.equal(input.value, next);
  assert.equal(advisor.getDraft("account-a", "friend-a", "advisor", thread().id), next);
});

test("a late edit acknowledgement never touches the new friend and settles when the original discussion returns", async () => {
  const { env, advisor, input, edit } = preparedEdit(), response = deferred();
  env.rpc = method => method === "run.start" ? response.promise : new Promise(() => {});
  input.value = "好友A原草稿"; await input.dispatch("input"); await edit();
  await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
  input.value = "好友A下一问"; await input.dispatch("input");
  const other = thread("account-a", "friend-b", "advisor");
  advisor.selectedThreads[advisor.baseKey("account-a", "friend-b", "advisor")] = other.id;
  advisor.setCachedThread("account-a", "friend-b", "advisor", other);
  advisor.setDraft("account-a", "friend-b", "advisor", "好友B草稿", other.id);
  advisor.onSessionChanged("account-a", "friend-b");
  assert.equal(input.value, "好友B草稿");
  response.resolve({ run: { id: "run-2", threadId: thread().id, messageId: "q2", state: "running" } }); await tick(); await tick();
  assert.equal(input.value, "好友B草稿"); assert.equal(env.elements.get("editBar").hidden, true);
  advisor.onSessionChanged("account-a", "friend-a");
  assert.equal(input.value, "好友A原草稿\n\n好友A下一问"); assert.equal(env.elements.get("editBar").hidden, true);
});

test("paused or starting queued work and unresolved operations reject replacement without converting an edit into enqueue", async () => {
  for (const blocker of [
    { queue: queue([queuedItem({ requestId: "waiting", message: "待处理" })], 1, true, "manual") },
    { queue: queue([queuedItem({ requestId: "starting", message: "启动中" }, { state: "starting" })], 2) },
    { activeOperation: { id: "op", state: "unknown" } }, { activeOperation: { id: "op", state: "running" } }
  ]) {
    const { env, advisor, input, edit } = preparedEdit();
    input.value = "原草稿"; await input.dispatch("input"); await edit();
    input.value = "待修改的问题"; await input.dispatch("input");
    const value = { ...answered(), ...blocker }; advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.syncThreadToFrame(value, done);
    assert.equal(advisor.latestTurn(), null); assert.deepEqual(actions(env), []);
    await env.elements.get("btnAdvisorSend").dispatch("click"); await tick();
    assert.equal(input.value, "待修改的问题"); assert.equal(env.elements.get("editBar").hidden, false);
    assert.ok(!methods(env).some(method => ["queue.enqueue", "run.start"].includes(method)));
  }
});

test("attention acknowledges only displayed saved completions from the current discussion, including old failure statuses", async () => {
  const { env, advisor } = preparedEdit();
  const value = { ...answered(), messages: [...answered().messages,
    { id: "failed", seq: 3, role: "status", status: "error", text: "生成失败", runId: "old-failed" },
    { id: "transient", role: "assistant", text: "尚未保存", runId: "not-saved" },
    { id: "thinking", seq: 4, role: "status", status: "thinking", text: "思考中", runId: "thinking" }] };
  advisor.setCachedThread("account-a", "friend-a", "advisor", value); advisor.syncThreadToFrame(value, { ...done, id: "new-running", state: "running" });
  const event = { type: "attention-check", account: "account-a", user: "friend-a", tasks: [
    { id: "saved-task", runId: "run-1", threadId: thread().id },
    { id: "failed-task", runId: "old-failed", threadId: thread().id },
    { id: "other-discussion", runId: "run-1", threadId: "another-thread" },
    { id: "unloaded", runId: "unloaded", threadId: thread().id },
    { id: "transient-task", runId: "not-saved", threadId: thread().id },
    { id: "thinking-task", runId: "thinking", threadId: thread().id }] };
  advisor.onEvent(event); await tick();
  assert.deepEqual(env.hostCalls, [["attention.seen", { account: "account-a", user: "friend-a", taskIds: ["saved-task", "failed-task"] }]]);
  env.document.hidden = true; advisor.onEvent(event); env.document.hidden = false;
  advisor.onEvent({ ...event, user: "friend-b" }); advisor.frameReady = false; advisor.onEvent(event); await tick();
  assert.equal(env.hostCalls.length, 1);
});

test("loading the accepted turn after a lost acknowledgement settles the edit without retaining its submitted question as a new draft", async () => {
  const { env, advisor, input, edit } = preparedEdit(); env.rpc = () => fail("回执丢失");
  input.value = "先前草稿"; await input.dispatch("input"); await edit();
  input.value = "已提交的修改"; await input.dispatch("input"); await env.elements.get("btnAdvisorSend").dispatch("click"); await tick(); await tick();
  const sent = env.rpcCalls.find(call => call.method === "run.start").params;
  assert.equal(input.value, "已提交的修改");
  env.rpc = () => new Promise(() => {});
  const saved = { ...answered(), messages: [
    ...answered().messages.map(item => ({ ...item, supersededBy: "run-2" })),
    { id: "q2", role: "user", seq: 3, text: "已提交的修改", runId: "run-2" }] };
  advisor.adoptThread({ thread: saved, run: { id: "run-2", threadId: thread().id, messageId: "q2", requestId: sent.requestId, state: "running" } });
  assert.equal(input.value, "先前草稿"); assert.equal(env.elements.get("editBar").hidden, true);
});
