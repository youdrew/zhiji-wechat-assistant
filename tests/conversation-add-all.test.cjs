const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const read = (...parts) => readFileSync(path.join(root, ...parts), "utf8");

it("separates bulk addition from selection controls and provides a cancel-first confirmation", () => {
  const html = read("chatui", "index.html");
  assert.match(html, /id="conversationManagerList"[\s\S]{0,400}id="btnAddAllConversations"/u);
  assert.match(html, /<dialog[^>]*id="conversationActionConfirm"/u);
  assert.match(html, /id="btnCancelConversationAction"[^>]*autofocus/u);
  const app = read("chatui", "app.js");
  assert.match(app, /async function addAllConversations\(/u);
  assert.match(app, /byId\("btnAddAllConversations"\)\.addEventListener\("click"/u);
});

// Runs the selection, follow and add-all code from app.js against a fake bridge.
function harness(selected = [], { initialized = true, confirm = true, postImpl } = {}) {
  const app = read("chatui", "app.js");
  const first = app.indexOf("async function loadConversationSelection(");
  const last = app.indexOf("settingsState.sweepBusy = false;", first);
  assert.ok(first >= 0 && last > first, "selection code must stay together in app.js");
  const session = id => ({ username: id, name: id });
  const chatState = {
    sessions: new Map(["a", "b", "c"].map(id => [id, session(id)])),
    selectedConversations: new Set(), currentAccount: "acct", selectionLoadedAccount: null,
    sessionRequest: 1, conversationSelectionBusy: false, conversationSelectionEpoch: 0, currentUser: "a",
  };
  const server = { selected: new Set(selected), initialized };
  const posts = [];
  const confirmations = [];
  const nodes = new Map();
  let close;
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, { hidden: true, textContent: "", focus() {},
      addEventListener(_name, fn) { close = fn; },
      showModal() {
        confirmations.push({ title: byId("conversationActionTitle").textContent,
          message: byId("conversationActionMessage").textContent });
        const answer = typeof confirm === "function" ? confirm(chatState) : confirm;
        this.returnValue = answer ? "confirm" : "cancel";
        close();
      },
    });
    return nodes.get(id);
  };
  const storage = new Map();
  const reply = () => ({ account: "acct", initialized: server.initialized,
    selectedSessions: [...server.selected] });
  const context = vm.createContext({
    chatState, accountClearedExiting: false,
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    byId, text: (id, value) => { byId(id).textContent = value; }, renderSessions() {}, renderConversationManager() {},
    switchSession() {},
    filteredConversationManagerSessions: () => [...chatState.sessions.values()],
    async api(_url, options = {}) {
      if (!options.method) return reply();
      const body = JSON.parse(options.body);
      posts.push(body);
      if (postImpl) return postImpl(body, chatState);
      server.initialized = true;
      if (body.all) for (const id of chatState.sessions.keys()) server.selected.add(id);
      else if (Array.isArray(body.sessions)) for (const id of body.sessions) server.selected.delete(id);
      else if (body.selected) server.selected.add(body.session);
      else server.selected.delete(body.session);
      return reply();
    },
  });
  const removalStart = app.indexOf("function conversationRemovalSelection(");
  const removalEnd = app.indexOf("function openConversationManager(", removalStart);
  const toggleStart = app.indexOf("async function toggleConversationSelected(");
  const toggleEnd = app.indexOf("async function loadSessions(", toggleStart);
  assert.ok(removalStart >= 0 && removalEnd > removalStart && toggleStart >= 0 && toggleEnd > toggleStart);
  vm.runInContext(app.slice(first, last) + app.slice(removalStart, removalEnd) + app.slice(toggleStart, toggleEnd) +
    "\nglobalThis.fns = { loadConversationSelection, trackNewConversations, addAllConversations, setConversationAutoFollow, " +
    "toggleConversationSelected, conversationRemovalSelection, removeSelectedConversations };",
    context);
  return {
    chatState, server, posts, confirmations, storage, byId, fns: context.fns,
    selected: () => [...chatState.selectedConversations].sort(),
    removeByHand(id) { server.selected.delete(id); chatState.selectedConversations.delete(id); },
    async reload() { chatState.selectionLoadedAccount = null; await context.fns.loadConversationSelection("acct", 1); },
  };
}

it("never fills the sidebar or follows new chats on its own", async () => {
  const run = harness([], { initialized: false });
  await run.reload();
  run.chatState.sessions.set("d", { username: "d", name: "d" });
  await run.fns.trackNewConversations("acct");
  assert.equal(run.posts.length, 0);
  assert.deepEqual(run.selected(), []);
});

it("adding everything confirms only missing conversations and does not opt in to future additions", async () => {
  const run = harness(["a"]);
  await run.reload();
  await run.fns.addAllConversations();
  assert.match(run.confirmations[0].title, /2/);
  assert.match(run.confirmations[0].message, /不受搜索条件影响/);
  assert.deepEqual(run.selected(), ["a", "b", "c"]);
  run.chatState.sessions.set("d", { username: "d", name: "d" });
  await run.fns.trackNewConversations("acct");
  assert.deepEqual(run.selected(), ["a", "b", "c"]);
  assert.equal(run.posts.length, 1);
  assert.equal(run.chatState.conversationSelectionBusy, false);
});

it("only explicitly following adds unseen chats and removed ones stay out", async () => {
  const run = harness(["a"]);
  await run.reload();
  run.fns.setConversationAutoFollow(true);
  await run.fns.addAllConversations();
  run.removeByHand("b");
  run.chatState.sessions.set("d", { username: "d", name: "d" });
  await run.fns.trackNewConversations("acct");
  assert.deepEqual(run.selected(), ["a", "c", "d"]);
  assert.deepEqual(run.posts.slice(1), [{ expectedAccount: "acct", session: "d", selected: true }]);
  // Following survives a restart and still leaves the removed chat alone.
  await run.reload();
  await run.fns.trackNewConversations("acct");
  assert.equal(run.posts.length, 2);
  assert.deepEqual(run.selected(), ["a", "c", "d"]);
});

it("following remembers selected saved-only chats so removing one keeps it out when it reappears", async () => {
  const run = harness(["a", "saved-only"]);
  await run.reload();
  run.fns.setConversationAutoFollow(true);
  assert.ok(JSON.parse(run.storage.get("conversation-follow-v1")).acct.known.includes("saved-only"));
  await run.fns.toggleConversationSelected("saved-only");
  run.chatState.sessions.set("saved-only", { username: "saved-only", name: "saved-only" });
  await run.fns.trackNewConversations("acct");
  assert.deepEqual(run.selected(), ["a"]);
  assert.deepEqual(run.posts, [{ expectedAccount: "acct", session: "saved-only", selected: false }]);
});

it("single and batch removal extend an existing follow preference with saved-only ids", async () => {
  for (const batch of [false, true]) {
    const removed = batch ? ["saved-first", "saved-second"] : ["saved-first"];
    const run = harness(["a", ...removed]);
    await run.reload();
    // A preference created before these selected chats became visible lacks their ids.
    run.storage.set("conversation-follow-v1", JSON.stringify({ acct: { known: ["a", "b", "c"] } }));
    if (batch) {
      for (const id of removed) run.fns.conversationRemovalSelection().add(id);
      await run.fns.removeSelectedConversations();
    } else await run.fns.toggleConversationSelected(removed[0]);
    const known = JSON.parse(run.storage.get("conversation-follow-v1")).acct.known;
    for (const id of removed) {
      assert.ok(known.includes(id));
      run.chatState.sessions.set(id, { username: id, name: id });
    }
    await run.fns.trackNewConversations("acct");
    assert.deepEqual(run.selected(), ["a"]);
    assert.equal(run.posts.length, 1, "reappearing removed chats are not automatically added again");
  }
});

it("remembering removed chats does not enable automatic following", async () => {
  for (const batch of [false, true]) {
    const run = harness(["a", "saved-only"]);
    await run.reload();
    if (batch) {
      run.fns.conversationRemovalSelection().add("saved-only");
      await run.fns.removeSelectedConversations();
    } else await run.fns.toggleConversationSelected("saved-only");
    assert.equal(run.storage.size, 0);
    run.chatState.sessions.set("saved-only", { username: "saved-only", name: "saved-only" });
    await run.fns.trackNewConversations("acct");
    assert.deepEqual(run.selected(), ["a"]);
    assert.equal(run.posts.length, 1);
  }
});

it("a failed follow-preference write does not undo a successful single or batch removal", async () => {
  for (const batch of [false, true]) {
    const run = harness(["a", "saved-only"]);
    await run.reload();
    run.storage.set("conversation-follow-v1", JSON.stringify({ acct: { known: ["a", "b", "c"] } }));
    run.storage.set = () => { throw new Error("synthetic storage unavailable"); };
    if (batch) {
      run.fns.conversationRemovalSelection().add("saved-only");
      await run.fns.removeSelectedConversations();
    } else await run.fns.toggleConversationSelected("saved-only");
    assert.deepEqual(run.selected(), ["a"]);
    assert.equal(run.posts.length, 1);
    assert.match(run.byId("conversationManagerStatus").textContent, /移除.*设置保存失败/);
    assert.equal(run.chatState.conversationSelectionBusy, false);
  }
});

it("can disable the legacy add-all follow setting without removing existing conversations", async () => {
  const run = harness(["a"]);
  run.storage.set("conversation-follow-v1", JSON.stringify({ acct: { known: ["a", "b", "c"] } }));
  await run.reload();
  run.fns.setConversationAutoFollow(false);
  run.chatState.sessions.set("d", { username: "d", name: "d" });
  await run.fns.trackNewConversations("acct");
  assert.deepEqual(run.selected(), ["a"]);
  assert.equal(run.posts.length, 0);
});

it("canceling bulk addition leaves selection and automatic following unchanged", async () => {
  const run = harness(["a"], { confirm: false });
  await run.reload();
  await run.fns.addAllConversations();
  assert.equal(run.confirmations.length, 1);
  assert.equal(run.posts.length, 0);
  assert.deepEqual(run.selected(), ["a"]);
  assert.equal(run.storage.size, 0);
  assert.equal(run.chatState.conversationSelectionBusy, false);
});

it("ignores repeat clicks, an already-complete list, and mismatched account ownership", async () => {
  const run = harness(["a", "b", "c"]);
  await run.reload();
  await run.fns.addAllConversations();
  run.chatState.selectedConversations.clear();
  run.chatState.conversationSelectionBusy = true;
  await run.fns.addAllConversations();
  run.chatState.conversationSelectionBusy = false;
  run.chatState.selectionLoadedAccount = "another-account";
  await run.fns.addAllConversations();
  assert.equal(run.confirmations.length, 0);
  assert.equal(run.posts.length, 0);
});

it("does not act on a changed account or directory after confirmation", async () => {
  for (const change of [
    state => { state.currentAccount = "other"; },
    state => { state.selectionLoadedAccount = null; },
    state => { state.sessions.set("d", { username: "d" }); },
  ]) {
    const run = harness(["a"], { confirm: state => { change(state); return true; } });
    await run.reload();
    await run.fns.addAllConversations();
    assert.equal(run.posts.length, 0);
    assert.deepEqual(run.selected(), ["a"]);
  }
});

it("preserves selection when the server fails or acknowledges the wrong account", async () => {
  for (const postImpl of [
    async () => { throw new Error("synthetic failure"); },
    async () => ({ account: "other", selectedSessions: ["a", "b", "c"] }),
    async () => ({ account: "acct", selectedSessions: ["a", "b"] }),
  ]) {
    const run = harness(["a"], { postImpl });
    await run.reload();
    await run.fns.addAllConversations();
    assert.deepEqual(run.selected(), ["a"]);
    assert.equal(run.storage.size, 0);
    assert.equal(run.chatState.conversationSelectionBusy, false);
  }
});

it("keeps a successful addition when saving the existing follow preference fails", async () => {
  const run = harness(["a"]);
  await run.reload();
  run.fns.setConversationAutoFollow(true);
  run.storage.set = () => { throw new Error("synthetic storage unavailable"); };
  await run.fns.addAllConversations();
  assert.deepEqual(run.selected(), ["a", "b", "c"]);
  assert.equal(run.posts.length, 1);
  assert.match(run.byId("conversationManagerStatus").textContent, /已添加 3 个会话.*设置保存失败/);
  assert.equal(run.chatState.conversationSelectionBusy, false);
});

it("stops following once the account's selection starts over", async () => {
  const run = harness(["a"]);
  await run.reload();
  run.fns.setConversationAutoFollow(true);
  await run.fns.addAllConversations();
  run.server.selected.clear();
  run.server.initialized = false;
  await run.reload();
  run.chatState.sessions.set("e", { username: "e", name: "e" });
  await run.fns.trackNewConversations("acct");
  assert.equal(run.posts.length, 1);
  assert.deepEqual(run.selected(), []);
});

it("keeps the existing two-field selection request working", () => {
  const http = read("bridge", "real_http.py");
  assert.match(http, /set\(request\) == \{"expectedAccount", "all"\} and request\["all"\] is True/u);
  assert.match(http, /set\(request\) != \{"expectedAccount", "session", "selected"\}/u);
  assert.match(read("bridge", "conversation_selection.py"),
    /def set_all_selected\(self, account, sessions\):/u);
});

it("old bulk-add and automatic-follow responses cannot unlock a newer account action or replace its selection", async () => {
  for (const action of ["all", "follow"]) {
    let settle;
    const run = harness(["a"], { postImpl: () => new Promise(resolve => { settle = resolve; }) });
    await run.reload();
    if (action === "follow") {
      run.fns.setConversationAutoFollow(true);
      run.chatState.sessions.set("d", { username: "d", name: "d" });
    }
    const pending = action === "all" ? run.fns.addAllConversations() : run.fns.trackNewConversations("acct");
    await new Promise(setImmediate);
    assert.equal(run.posts.length, 1);
    run.chatState.conversationSelectionEpoch++;
    run.chatState.selectedConversations = new Set(["a", "restored-chat"]);
    run.chatState.conversationSelectionBusy = true;
    settle({ account: "acct", initialized: true, selectedSessions: ["a", "b", "c", "d"] });
    await pending;
    assert.deepEqual(run.selected(), ["a", "restored-chat"], action);
    assert.equal(run.chatState.conversationSelectionBusy, true, action);
  }
});
