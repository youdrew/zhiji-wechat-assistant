const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const html = readFileSync(path.join(__dirname, "../chatui/index.html"), "utf8");
const first = source.indexOf("let pendingAnalysisScopeReset = null;");
const last = source.indexOf("function retryAnalysis()", first);
assert.ok(first >= 0 && last > first);

function harness({ mode = "local", member = "", postImpl } = {}) {
  const nodes = new Map(), posts = [], loads = [], retries = [], messages = [];
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, { hidden: false, disabled: false, textContent: "", listeners: {},
      addEventListener(name, fn) { this.listeners[name] = fn; }, focus() {} });
    return nodes.get(id);
  };
  const map = () => new Map();
  const context = vm.createContext({
    byId, document: { activeElement: { focus() {} } }, AbortController, clearTimeout() {},
    text: (id, value) => { byId(id).textContent = value; }, toast() {}, saveStoredProfiles() {},
    apiInsightKey: (...values) => JSON.stringify(values), sessionCacheKey: (...values) => JSON.stringify(values),
    cancelApiPortraitPoll() {}, clearProfileView() {}, clearApiPortraitView() {},
    cancelApiInsightWork() {}, clearInlineIntentPending() {},
    renderMessages: values => messages.push(values), retryAnalysis: () => retries.push("retry"),
    loadProfile: (...args) => loads.push(args),
    chatState: { currentAccount: "account-a", currentUser: "room@chatroom", selectedConversations: new Set(["room@chatroom"]),
      sessions: new Map([["room@chatroom", { name: "Synthetic room", isGroup: true }]]),
      generation: 1, sessionCache: map(), controller: new AbortController(), messages: [{ id: "m1" }],
      view: "persona", advance(field) { this[field]++; } },
    settingsState: { modelSourceResolved: true, modelSourceRevision: 2,
      modelSourceSnapshot: { mode, sourceId: mode === "local" ? "local" : "api-a", api: { model: "Synthetic API" } } },
    portraitState: { activeMember: member, groupMembers: [{ id: "member-a", name: "Synthetic member" }],
      profileCache: map(), storedProfileSnapshots: map(), profileRateSamples: map(),
      profileSnapshotsRequireRefresh: new Set(), apiPortraitSubmitErrors: map(), autoIncrementalState: map(), profileGeneration: 1,
      analysisGeneration: 1 },
    labelState: { apiInsightCache: map(), requestedRecentSignatures: new Set() },
    async api(url, options) {
      assert.equal(url, "/api/analysis-scope/clear");
      const payload = JSON.parse(options.body);
      posts.push(payload);
      return postImpl ? postImpl(payload) : { cleared: true, ...payload };
    },
  });
  vm.runInContext(source.slice(first, last) + "\nglobalThis.ui = { analysisResetScope, openAnalysisScopeReset, closeAnalysisScopeReset, confirmAnalysisScopeReset, invalidateAnalysisScopeCaches };", context);
  return { context, ui: context.ui, byId, posts, loads, retries, messages };
}

function populate(h) {
  const key = (...values) => JSON.stringify(values);
  const keys = {
    local: key("account-a", "room@chatroom", ""), member: key("account-a", "room@chatroom", "member-a"),
    otherMember: key("account-a", "room@chatroom", "member-b"),
    api: key("account-a", "room@chatroom", "", "api-a"), apiMember: key("account-a", "room@chatroom", "member-a", "api-a"),
    otherApi: key("account-a", "room@chatroom", "member-a", "api-b"),
    otherUser: key("account-a", "other", "member-a"), otherAccount: key("account-b", "room@chatroom", "member-a"),
  };
  for (const map of [h.context.portraitState.profileCache, h.context.portraitState.storedProfileSnapshots, h.context.portraitState.profileRateSamples])
    for (const value of Object.values(keys)) map.set(value, { sentinel: true });
  h.context.chatState.sessionCache.set(key("account-a", "room@chatroom"), { messages: [{ id: "m1" }], results: { m1: "saved" }, mood: "saved" });
  h.context.portraitState.autoIncrementalState.set(key("account-a", "room@chatroom", "current"), { saved: true });
  h.context.portraitState.autoIncrementalState.set(key("account-a", "other", "current"), { saved: true });
  h.context.labelState.apiInsightCache.set(key("account-a", "room@chatroom", "api-a"), { saved: true });
  h.context.labelState.apiInsightCache.set(key("account-a", "room@chatroom", "api-b"), { saved: true });
  return keys;
}

it("requires a selected chat and explicit confirmation before posting a fixed reset scope", async () => {
  assert.match(html, /id="btnRetryAnalysis"[\s\S]{0,300}id="btnResetConversationAnalysis"/);
  assert.match(html, /id="btnRetryProfile"[\s\S]{0,300}id="btnResetPortrait"/);
  const h = harness();
  h.ui.openAnalysisScopeReset("conversation");
  assert.equal(h.posts.length, 0);
  assert.match(h.byId("analysisScopeQuestion").textContent, /聊天记录和聊天助手数据会保留/);
  await h.ui.confirmAnalysisScopeReset();
  assert.deepEqual(h.posts, [{ account: "account-a", user: "room@chatroom", sourceId: "local", kind: "conversation" }]);
  const unavailable = harness();
  unavailable.context.chatState.selectedConversations.clear();
  unavailable.ui.openAnalysisScopeReset("conversation");
  await unavailable.ui.confirmAnalysisScopeReset();
  assert.equal(unavailable.posts.length, 0);
});

it("resets local conversation caches without changing other users, accounts, sources or chat messages", async () => {
  const h = harness();
  const keys = populate(h);
  h.ui.openAnalysisScopeReset("conversation");
  await h.ui.confirmAnalysisScopeReset();
  for (const name of ["local", "member", "otherMember"]) assert.equal(h.context.portraitState.profileCache.has(keys[name]), false);
  for (const name of ["api", "apiMember", "otherApi", "otherUser", "otherAccount"]) assert.equal(h.context.portraitState.profileCache.has(keys[name]), true);
  const cache = h.context.chatState.sessionCache.get(JSON.stringify(["account-a", "room@chatroom"]));
  assert.equal(JSON.stringify(cache.results), "{}");
  assert.deepEqual(cache.messages, [{ id: "m1" }]);
  assert.equal(h.context.portraitState.autoIncrementalState.size, 1);
  assert.equal(h.context.labelState.apiInsightCache.size, 2);
  assert.equal(h.retries.length, 1);
  assert.equal(h.loads.length, 1);
});

it("resets one API member portrait while preserving labels and all other snapshots", async () => {
  const h = harness({ mode: "api", member: "member-a" });
  const keys = populate(h);
  h.ui.openAnalysisScopeReset("portrait");
  await h.ui.confirmAnalysisScopeReset();
  assert.deepEqual(h.posts, [{ account: "account-a", user: "room@chatroom", sourceId: "api-a", kind: "portrait", member: "member-a" }]);
  assert.equal(h.context.portraitState.profileCache.has(keys.apiMember), false);
  for (const [name, value] of Object.entries(keys)) if (name !== "apiMember") assert.equal(h.context.portraitState.profileCache.has(value), true);
  assert.equal(h.context.labelState.apiInsightCache.size, 2);
  assert.equal(h.retries.length, 0);
  assert.deepEqual(h.loads, [["member-a", true]]);
});

it("resets an API conversation only for the selected source and leaves local labels intact", async () => {
  const h = harness({ mode: "api" });
  const keys = populate(h);
  h.ui.openAnalysisScopeReset("conversation");
  await h.ui.confirmAnalysisScopeReset();
  assert.equal(h.context.portraitState.profileCache.has(keys.api), false);
  assert.equal(h.context.portraitState.profileCache.has(keys.apiMember), false);
  assert.equal(h.context.portraitState.profileCache.has(keys.local), true);
  assert.equal(h.context.portraitState.profileCache.has(keys.otherApi), true);
  assert.equal(h.context.labelState.apiInsightCache.has(JSON.stringify(["account-a", "room@chatroom", "api-a"])), false);
  assert.equal(h.context.labelState.apiInsightCache.has(JSON.stringify(["account-a", "room@chatroom", "api-b"])), true);
  assert.equal(h.context.chatState.sessionCache.get(JSON.stringify(["account-a", "room@chatroom"])).results.m1, "saved");
});

it("does not submit a confirmation after account, conversation, source, generation or member changes", async () => {
  const changes = [
    h => { h.context.chatState.currentAccount = "account-b"; },
    h => { h.context.chatState.currentUser = "other"; },
    h => { h.context.settingsState.modelSourceSnapshot.sourceId = "api-b"; },
    h => { h.context.chatState.generation++; },
    h => { h.context.portraitState.activeMember = "member-b"; },
    h => { h.context.settingsState.modelSourceRevision++; },
  ];
  for (const change of changes) {
    const h = harness({ mode: "api", member: "member-a" });
    h.ui.openAnalysisScopeReset("portrait");
    change(h);
    await h.ui.confirmAnalysisScopeReset();
    assert.equal(h.posts.length, 0);
    assert.match(h.byId("analysisScopeStatus").textContent, /已变化/);
  }
});

it("ignores late success and does not invalidate a new active scope", async () => {
  let resolve;
  const h = harness({ postImpl: () => new Promise(done => { resolve = done; }) });
  const keys = populate(h);
  h.ui.openAnalysisScopeReset("conversation");
  const pending = h.ui.confirmAnalysisScopeReset();
  h.context.chatState.generation++;
  resolve({ cleared: true, account: "account-a", user: "room@chatroom", sourceId: "local", kind: "conversation" });
  await pending;
  assert.equal(h.context.portraitState.profileCache.has(keys.local), true);
  assert.equal(h.retries.length, 0);
  assert.equal(h.byId("analysisScopeConfirm").hidden, true);
});

it("keeps caches and the confirmation available after an invalid or failed reset acknowledgement", async () => {
  for (const postImpl of [async () => { throw new Error("'happy'"); }, async () => ({ cleared: true, account: "wrong" })]) {
    const h = harness({ postImpl });
    const keys = populate(h);
    h.ui.openAnalysisScopeReset("conversation");
    await h.ui.confirmAnalysisScopeReset();
    assert.equal(h.context.portraitState.profileCache.has(keys.local), true);
    assert.equal(h.byId("analysisScopeConfirm").hidden, false);
    assert.match(h.byId("analysisScopeStatus").textContent, /重置未完成/);
    assert.doesNotMatch(h.byId("analysisScopeStatus").textContent, /happy/);
    assert.equal(h.retries.length, 0);
  }
});
