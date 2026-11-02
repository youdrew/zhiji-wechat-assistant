const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { seed } = require("./helpers/view-state-harness.cjs");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const start = source.indexOf("async function loadSessions(");
const end = source.indexOf("function messageNode(", start);
assert.ok(start >= 0 && end > start);
const loadSessionsSource = source.slice(start, end);
const selectionStart = source.indexOf("async function loadConversationSelection(");
const selectionEnd = source.indexOf("function clearUnselectedConversation(", selectionStart);
assert.ok(selectionStart >= 0 && selectionEnd > selectionStart);
const loadSelectionSource = source.slice(selectionStart, selectionEnd);
const preloadSource = source.slice(source.indexOf("async function preloadSessionWindows("), selectionStart);

function harness(responses, selectedByAccount = {}, options = {}) {
  const context = vm.createContext({ responses: [...responses], selectedByAccount, AbortController,
    batchHook: options.batchHook, rememberedByAccount: options.rememberedByAccount || {},
    initialStartupActive: options.startupActive === true });
  seed(context, { messages: [{ id: "stale" }], results: { stale: true }, conversationMood: "stale" });
  vm.runInContext(`
    let accountClearedExiting = false, accountUnavailable = false, startupAccountRetryTimer = null;
    const elements = new Map(), events = [], startupStates = [], batches = [], windowCache = new Map();
    const byId = id => {
      if (!elements.has(id)) elements.set(id, { scrollTop: 0 });
      return elements.get(id);
    };
    let lastAccount = null;
    const api = async (path, options = {}) => {
      events.push(path);
      if (path === "/api/conversation-selection")
        return { account: lastAccount, selectedSessions: selectedByAccount[lastAccount] || [] };
      if (path === "/api/messages/batch") {
        const body = JSON.parse(options.body); batches.push(body);
        return batchHook ? batchHook(body) : { account: body.account, windows: body.users.map(user => ({ user, messages: [{ id: user, text: "合成消息" }] })) };
      }
      const response = responses.shift();
      lastAccount = response?.account;
      return response;
    };
    const clearTimeout = () => {};
    // Modified by Know Your Friends, 2026-10-10: 知己分析 windows observe account changes.
    function announceFriendAccount(type, value) { events.push("friend-account:" + type + ":" + value); }
    function resetAccountView() {
      events.push("reset");
      sessionRequest++;
      currentAccount = null;
      selectionLoadedAccount = null;
      selectedConversations.clear();
      currentUser = null;
      messageSourceReady = false;
      sessions.clear();
      messages = [];
      results = {};
      conversationMood = null;
    }
    const setAvatar = () => {}, text = () => {};
    const status = (_node, message) => events.push(message);
    const switchView = target => events.push("view:" + target);
    const updateFriendButton = () => events.push("friend-entry-ready:" + messageSourceReady);
    const renderSessions = () => events.push("render:" + sessions.size);
    const unlockStartupUi = () => events.push("unlock");
    const completeStartup = () => { events.push("complete"); startupActive = false; };
    const accountCheckStatus = message => events.push("status:" + message);
    const sessionWindowReady = (account, session) => windowCache.has(JSON.stringify([account, session.username]));
    const cacheSessionWindow = (account, session, messages) => windowCache.set(JSON.stringify([account, session.username]), messages);
    ${preloadSource}
    const originalPreload = preloadSessionWindows;
    preloadSessionWindows = async (...args) => {
      const [_account, nextSessions] = args;
      events.push("preload:" + [...nextSessions.keys()].filter(id => selectedConversations.has(id)).length);
      return originalPreload(...args);
    };
    const pruneSessionCache = () => {};
    const localStorage = { getItem: key => rememberedByAccount[key] || null };
    const switchSession = user => { events.push("switch:" + user); currentUser = user; };
    const clearUnselectedConversation = () => {
      events.push("clear-unselected");
      currentUser = null;
      messages = [];
      results = {};
      conversationMood = null;
    };
    const loadProfile = () => { throw new Error("profile must not run in chat view"); };
    const renderConversationManager = () => events.push("manager");
    const trackNewConversations = async () => {};
    const accountChangedError = () => false;
    const accountUnavailableError = () => false;
    const contactSnapshotStaleError = () => false;
    const showStartup = (stage, message, options = {}) => {
      events.push("startup:" + message); startupStates.push({ stage, message, options });
    };
    let startupActive = initialStartupActive;
    ${loadSelectionSource}
    ${loadSessionsSource}
    globalThis.check = () => ({ events: [...events], startupStates: [...startupStates], startupActive,
      account: currentAccount, user: currentUser,
      ready: messageSourceReady, sessions: [...sessions.keys()], messages: [...messages],
      results: { ...results }, mood: conversationMood, refresh: [...profileSnapshotsRequireRefresh],
      selected: [...selectedConversations], batches, cached: [...windowCache.keys()] });
  `, context);
  return context;
}

const session = account => ({ account, self: { name: "我" },
  sessions: [{ username: "friend", name: "朋友", preview: "合成会话" }] });

test("partial startup renders sessions and keeps the chat empty when none were selected", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: false },
    { ...session("account-a"), messagesReady: false },
    { ...session("account-a"), messagesReady: true }], {}, { startupActive: true });
  await context.loadSessions();
  let state = context.check();
  assert.equal(state.ready, false);
  assert.equal(state.account, "account-a");
  assert.equal(state.user, null);
  assert.equal(state.sessions.join(","), "friend");
  assert.equal(state.messages.length, 0);
  assert.equal(Object.keys(state.results).length, 0);
  assert.equal(state.mood, null);
  assert.equal(state.events.some(value => value.startsWith("preload:")), false);
  assert.equal(state.events.includes("switch:friend"), false);
  assert.ok(state.events.includes("friend-entry-ready:false"), "plugin entries refresh after messages become unavailable");
  assert.ok(state.events.includes("render:1"));
  assert.equal(state.events.includes("complete"), false, "a readable directory does not mean messages are ready");
  assert.ok(state.events.includes("unlock"), "the directory remains usable during message preparation");
  assert.equal(state.startupActive, true);
  assert.equal(state.startupStates.at(-1).stage, "messages");
  assert.equal(state.startupStates.at(-1).options.retry, false);
  await context.loadSessions();
  state = context.check();
  assert.equal(state.events.filter(value => value === "reset").length, 1);
  assert.equal(state.events.some(value => value.startsWith("preload:")), false);
  assert.equal(state.events.includes("complete"), false);
  assert.equal(state.events.some(value => value.includes("读取失败")), false, "partial handling must not silently fall into its catch branch");
  await context.loadSessions();
  state = context.check();
  assert.equal(state.ready, true);
  assert.equal(state.selected.length, 0);
  assert.ok(state.events.includes("preload:0"));
  assert.ok(state.events.includes("clear-unselected"));
  assert.equal(state.events.includes("switch:friend"), false);
  assert.equal(state.events.filter(value => value === "/api/conversation-selection").length, 1);
  assert.equal(state.events.filter(value => value === "complete").length, 1);
  assert.equal(state.startupActive, false);
});

test("partial database failure preserves usable sessions and exposes retry until a successful read", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: false,
    preparation: { state: "failed", reason: "keys_incomplete", message: "合成数据库读取未完成，请重试。" } },
    { ...session("account-a"), messagesReady: true }], {}, { startupActive: true });
  await context.loadSessions();
  let state = context.check();
  assert.equal(state.sessions.join(","), "friend");
  assert.equal(state.ready, false);
  assert.equal(state.startupActive, true);
  assert.ok(state.events.includes("unlock"));
  assert.equal(state.events.includes("complete"), false);
  assert.equal(state.startupStates.at(-1).stage, "messages");
  assert.equal(state.startupStates.at(-1).message, "合成数据库读取未完成，请重试。");
  assert.equal(state.startupStates.at(-1).options.retry, true);
  await context.loadSessions();
  state = context.check();
  assert.equal(state.ready, true);
  assert.equal(state.startupActive, false);
  assert.equal(state.events.filter(value => value === "complete").length, 1);
});

test("a saved selection alone enables its chat window and first session", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: true }],
    { "account-a": ["friend"] });
  await context.loadSessions();
  const state = context.check();
  assert.equal(state.selected.join(","), "friend");
  assert.equal(state.user, "friend");
  assert.ok(state.events.includes("preload:1"));
  assert.ok(state.events.includes("switch:friend"));
});

test("partial account change drops the previous account view", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: false },
    { ...session("account-b"), messagesReady: false }]);
  await context.loadSessions();
  await context.loadSessions();
  const state = context.check();
  assert.equal(state.account, "account-b");
  assert.equal(state.user, null);
  assert.equal(state.ready, false);
  assert.equal(state.events.filter(value => value === "reset").length, 2);
  assert.equal(state.events.some(value => value.startsWith("preload:")), false);
  assert.equal(state.selected.length, 0);
});

const manySessions = account => ({ account, messagesReady: true, self: { name: "我" },
  sessions: ["first", "remembered", "third"].map(username => ({ username, name: username, preview: "合成预览" })) });
const tick = () => new Promise(setImmediate);

test("first paint restores only the remembered conversation and repeated session polls do not warm the others", async () => {
  const data = manySessions("account-a");
  const context = harness([data, data, data], { "account-a": ["first", "remembered", "third"] },
    { rememberedByAccount: { "last-conversation:account-a": "remembered" } });
  await context.loadSessions();
  let state = context.check();
  assert.equal(state.user, "remembered");
  assert.deepEqual(JSON.parse(JSON.stringify(state.batches)), [{ account: "account-a", users: ["remembered"] }]);
  assert.equal(state.cached.length, 1);
  await context.loadSessions(); await context.loadSessions();
  state = context.check();
  assert.equal(state.batches.length, 1, "unchanged polls do not require uncached sidebar contacts");
  assert.equal(state.events.filter(value => value.startsWith("preload:")).length, 1);
});

test("the current selected conversation outranks the remembered one; unavailable remembered IDs fall back to the first selection", async () => {
  for (const current of ["third", null]) {
    const context = harness([manySessions("account-a")], { "account-a": ["first", "remembered", "third"] },
      { rememberedByAccount: { "last-conversation:account-a": current ? "remembered" : "not-in-sessions" } });
    context.currentAccount = "account-a"; context.currentUser = current;
    await context.loadSessions();
    const state = context.check(), expected = current || "first";
    assert.equal(state.user, expected);
    assert.deepEqual(Array.from(state.batches[0].users), [expected]);
  }
});

test("a click on another conversation while the first window loads is never switched back by its late result", async () => {
  let finish;
  const context = harness([manySessions("account-a")], { "account-a": ["first", "remembered", "third"] },
    { batchHook: () => new Promise(resolve => { finish = resolve; }) });
  const pending = context.loadSessions(); await tick();
  assert.ok(finish);
  vm.runInContext('switchSession("third")', context);
  finish({ account: "account-a", windows: [{ user: "first", messages: [] }] });
  await pending;
  const state = context.check();
  assert.equal(state.user, "third");
  assert.deepEqual(Array.from(state.events.filter(value => value.startsWith("switch:"))), ["switch:third"]);
  assert.deepEqual(Array.from(state.batches[0].users), ["first"]);
});

test("a delayed first window cannot cache or select the old account after the account boundary changes", async () => {
  let finish;
  const context = harness([manySessions("account-a")], { "account-a": ["first", "remembered", "third"] },
    { batchHook: () => new Promise(resolve => { finish = resolve; }) });
  const pending = context.loadSessions(); await tick();
  context.resetAccountView(); context.currentAccount = "account-b"; context.currentUser = "new-friend";
  finish({ account: "account-a", windows: [{ user: "first", messages: [{ id: "old-account-message" }] }] });
  await pending;
  const state = context.check();
  assert.equal(state.account, "account-b"); assert.equal(state.user, "new-friend");
  assert.equal(state.cached.length, 0);
  assert.equal(state.events.some(value => value.startsWith("switch:")), false);
});

test("final update unlocks the UI after commit while account validation can continue", async () => {
  const unlockStart = source.indexOf("function unlockStartupUi()");
  const unlockEnd = source.indexOf("function completeStartup()", unlockStart);
  const bootStart = source.indexOf("const updateValidationMode =");
  const bootEnd = source.indexOf('window.addEventListener("kyf-service-restored"', bootStart);
  assert.ok(unlockStart >= 0 && unlockEnd > unlockStart && bootStart >= 0 && bootEnd > bootStart);
  const nodes = new Map();
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: true, inert: false,
      classList: { add() {} },
      setAttribute(name) { if (name === "inert") this.inert = true; },
      removeAttribute(name) { if (name === "inert") this.inert = false; },
    });
    return nodes.get(id);
  };
  let finishCommit;
  const commit = new Promise(resolve => { finishCommit = resolve; });
  const events = [];
  const context = vm.createContext({
    byId, text() {},
    window: { desktopHost: { updateValidationMode: false, updateFinalReadyMode: true,
      reportUiReady: () => commit } },
    startInitialLoad: () => { events.push("account-validation-started"); return new Promise(() => {}); },
    loadModelSource: () => events.push("model-source-load-started"),
  });
  vm.runInContext(source.slice(unlockStart, unlockEnd) + source.slice(bootStart, bootEnd), context);
  assert.equal(byId("startupOverlay").hidden, false);
  assert.equal(byId("appWindow").inert, true);
  assert.equal(events.includes("account-validation-started"), false);
  finishCommit(true);
  await Promise.resolve();
  assert.equal(byId("startupOverlay").hidden, true);
  assert.equal(byId("appWindow").inert, false);
  assert.equal(events.includes("account-validation-started"), true);

  const validationContext = vm.createContext({
    byId, text() {},
    window: { desktopHost: { updateValidationMode: true, updateFinalReadyMode: false,
      reportUiReady: () => Promise.resolve(true) } },
    startInitialLoad: () => { throw new Error("validation must not open account data"); },
    loadModelSource: () => { throw new Error("validation must not load model settings"); },
  });
  vm.runInContext(source.slice(unlockStart, unlockEnd) + source.slice(bootStart, bootEnd), validationContext);
  await Promise.resolve();
  assert.equal(byId("startupOverlay").hidden, false);
  assert.equal(byId("appWindow").inert, true);
});
