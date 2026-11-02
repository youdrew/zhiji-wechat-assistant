const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");
const { seed } = require("./helpers/view-state-harness.cjs");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const html = readFileSync(path.join(__dirname, "../chatui/index.html"), "utf8");
function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing UI section: ${start}`);
  return source.slice(first, last);
}
const code = section("function renderSessions()", "async function preloadSessionWindows(") +
  section("function validConversationSelection(", "function clearUnselectedConversation(") +
  section("function showChatEmptyState()", "async function loadSessions(") +
  section('byId("btnAddConversation").addEventListener', 'byId("conversationSearch").addEventListener') +
  "globalThis.ui = { renderSessions, showChatEmptyState, closeConversationManager, " +
  "updateAddConversationButton, toggleConversationSelected, deleteConversation, renderConversationManager, " +
  "conversationRemovalSelection, removeSelectedConversations, filteredConversationManagerSessions, " +
  "updateConversationManagerSelection, selectAllRemovableConversations, clearConversationRemovalSelection };";

function node(tag = "div", className = "", textContent = "") {
  const item = { tag, className, textContent, value: "", dataset: {}, children: [],
    listeners: {}, parent: null, hidden: false, disabled: false,
    append(...children) { for (const child of children) this.appendChild(child); },
    appendChild(child) { child.remove(); child.parent = this; this.children.push(child); return child; },
    prepend(child) { child.remove(); child.parent = this; this.children.unshift(child); },
    insertBefore(child, before) {
      child.remove(); child.parent = this;
      const index = this.children.indexOf(before);
      this.children.splice(index < 0 ? this.children.length : index, 0, child);
    },
    replaceChildren(...children) { for (const child of [...this.children]) child.remove(); this.append(...children); },
    remove() {
      if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = null;
    },
    querySelector(selector) {
      for (const child of this.children) {
        if (selector.startsWith(".") && child.classList.contains(selector.slice(1))) return child;
        const found = child.querySelector(selector);
        if (found) return found;
      }
      return null;
    },
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); },
    click() { if (!this.disabled) for (const listener of this.listeners.click || []) listener({ target: this }); },
    focus() { this.focused = true; },
    setAttribute(name, value) { this[name] = value; },
  };
  item.classList = {
    contains: name => item.className.split(/\s+/).includes(name),
    add: name => { if (!item.classList.contains(name)) item.className = `${item.className} ${name}`.trim(); },
    remove: name => { item.className = item.className.split(/\s+/).filter(value => value !== name).join(" "); },
    toggle: (name, enabled) => { item.classList[enabled ? "add" : "remove"](name); },
  };
  Object.defineProperty(item, "lastElementChild", { get: () => item.children.at(-1) });
  return item;
}

function harness({ selected = [], messagesReady = true, sourceMode = "api", postImpl, confirmImpl, forgetFollowFails = false } = {}) {
  const nodes = new Map();
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const posts = [], preloads = [], switches = [], confirmations = [], forgottenFollow = [], stripStatuses = [], remembered = [];
  const follow = { "synthetic-account": { known: ["first", "second"] } };
  const sessions = new Map([
    ["first", { username: "first", name: "合成甲", preview: "第一段", time: 1 }],
    ["second", { username: "second", name: "合成乙", preview: "第二段", time: 2 }],
  ]);
  byId("conversationManager").hidden = true;
  byId("btnAddConversation").disabled = true;
  byId("settingsModal").appendChild(node("div", "settings-modal-card"));
  byId("btnSettings").addEventListener("click", () => byId("settingsModal").classList.add("show"));
  const generalTab = node("button");
  const context = vm.createContext({
    byId, element: node,
    document: { createTextNode: value => node("#text", "", value), querySelector: () => generalTab },
    text: (id, value) => { byId(id).textContent = value; },
    status: (container, value) => container.replaceChildren(node("div", "ui-state", value)),
    avatar: () => node("div", "avatar-frame"),
    time: value => String(value), getVisibleUnreadCount: () => 0, markSessionAsRead() {},
    switchSession: user => { switches.push(user); },
    preloadSessionWindows: async (_account, values) => { preloads.push([...values.keys()]); },
    clearUnselectedConversation() {
      assert.equal(context.chatState.selectedConversations.size, 0, "An existing selected conversation must be preserved");
      context.chatState.currentUser = null;
    },
    readConversationFollow: () => follow,
    notifyStatus: value => { if (value) stripStatuses.push(value); },
    rememberConversations(account, ids) { remembered.push({ account, ids: [...ids] }); },
    forgetConversationFollow(account) {
      forgottenFollow.push(account);
      if (forgetFollowFails) throw new Error("Synthetic local storage failure");
      delete follow[account];
    },
    confirmConversationAction: async options => {
      confirmations.push(options);
      return confirmImpl ? confirmImpl(options, context) : true;
    },
    api: async (url, options) => {
      assert.equal(url, "/api/conversation-selection");
      const payload = JSON.parse(options.body);
      posts.push(payload);
      if (postImpl) return postImpl(payload, context);
      const next = new Set(context.chatState.selectedConversations);
      if (Array.isArray(payload.sessions)) for (const id of payload.sessions) next.delete(id);
      else if (payload.selected) next.add(payload.session); else next.delete(payload.session);
      return { account: payload.expectedAccount, selectedSessions: [...next] };
    },
  });
  seed(context, {
    currentAccount: "synthetic-account", selectionLoadedAccount: "synthetic-account",
    selectedConversations: new Set(selected), sessions, currentUser: selected[0] || null,
    messageSourceReady: messagesReady, conversationSelectionBusy: false, sessionRequest: 1,
    modelSourceSnapshot: { mode: sourceMode, sourceId: sourceMode === "api" ? "synthetic-api" : "local" },
  });
  vm.runInContext(code, context);
  return { context, ui: context.ui, byId, posts, preloads, switches, confirmations, forgottenFollow, stripStatuses, remembered };
}

function assertManagerOpen(h) {
  assert.equal(h.byId("conversationManager").hidden, false);
  assert.equal(h.byId("settingsModal").classList.contains("show"), true);
  assert.equal(h.byId("conversationSearch").focused, true);
  assert.equal(h.byId("conversationManagerList").children.length, 2);
}

it("keeps a permanent add entry and opens the selector with no selected conversations", () => {
  assert.match(html, /<button\b[^>]*id="btnAddConversation"[^>]*disabled/);
  const h = harness();
  h.ui.renderSessions();
  assert.equal(h.byId("btnAddConversation").disabled, false);
  h.byId("sessionList").children[0].children.at(-1).click();
  assertManagerOpen(h);
  assert.equal(h.posts.length, 0);
  assert.equal(h.preloads.length, 0);
});

it("opens the same selector after the first conversation is added without changing the active view or selection", () => {
  for (const sourceMode of ["api", "local"]) {
    const h = harness({ selected: ["first"], sourceMode });
    h.ui.renderSessions();
    const existingRow = h.byId("sessionList").children[0];
    const sourceSnapshot = h.context.settingsState.modelSourceSnapshot;
    h.byId("btnAddConversation").click();
    assertManagerOpen(h);
    h.ui.closeConversationManager();
    h.byId("btnAddConversation").click();
    assertManagerOpen(h);
    h.ui.renderSessions();
    assert.equal(h.byId("sessionList").children[0], existingRow);
    assert.equal(h.context.chatState.currentUser, "first");
    assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
    assert.equal(h.context.settingsState.modelSourceSnapshot, sourceSnapshot);
    assert.equal(h.byId("conversationManagerList").children[0].children.at(-1).textContent, "从列表移除");
    assert.equal(h.posts.length, 0);
    assert.equal(h.preloads.length, 0);
  }
});

it("keeps the directory usable when messages are not ready and when the directory is empty", () => {
  const h = harness({ messagesReady: false });
  h.ui.renderSessions();
  assert.equal(h.byId("btnAddConversation").disabled, false);
  h.byId("btnAddConversation").click();
  assertManagerOpen(h);
  h.context.chatState.sessions.clear();
  h.ui.renderSessions();
  assert.equal(h.byId("btnAddConversation").disabled, false);
  assert.equal(h.preloads.length, 0);
});

it("disables the add entry while the current account's selection is not loaded", () => {
  const h = harness({ selected: ["first"] });
  h.context.chatState.currentAccount = "another-account";
  h.ui.renderSessions();
  assert.equal(h.byId("btnAddConversation").disabled, true);
  h.byId("btnAddConversation").click();
  assert.equal(h.byId("conversationManager").hidden, true);
  h.context.chatState.currentAccount = null;
  h.context.chatState.selectionLoadedAccount = null;
  h.ui.updateAddConversationButton();
  assert.equal(h.byId("btnAddConversation").disabled, true);
});

it("provides selector access from a filtered empty list and from the idle chat pane", () => {
  const h = harness({ selected: ["first"] });
  h.byId("searchInput").value = "没有这样的名字";
  h.ui.renderSessions();
  const empty = h.byId("sessionList").children[0];
  assert.equal(empty.children[0].textContent, "没有匹配的会话");
  empty.children.at(-1).click();
  assertManagerOpen(h);
  h.ui.closeConversationManager();
  h.ui.showChatEmptyState();
  h.byId("chatMessages").children[0].children.at(-1).click();
  assertManagerOpen(h);
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
});

it("adds and removes a second conversation through the existing account-scoped selection endpoint", async () => {
  const h = harness({ selected: ["first"] });
  h.ui.renderSessions();
  h.byId("btnAddConversation").click();
  await h.ui.toggleConversationSelected("second");
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first", "second"]);
  assert.deepEqual(h.byId("sessionList").children.map(row => row.dataset.id), ["first", "second"]);
  assert.deepEqual(h.posts[0], { expectedAccount: "synthetic-account", session: "second", selected: true });
  assert.deepEqual(h.preloads, [["second"]]);
  assert.equal(h.switches.length, 0);
  await h.ui.toggleConversationSelected("second");
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
  assert.equal(h.context.chatState.currentUser, "first");
  assert.equal(h.byId("btnAddConversation").disabled, false);
});

it("sorts selected conversations first and exposes checkboxes only for removable entries", () => {
  const h = harness({ selected: ["second", "saved-only"] });
  h.ui.renderConversationManager();
  const rows = h.byId("conversationManagerList").children;
  assert.deepEqual(rows.map(row => row.dataset.user), ["second", "saved-only", "first"]);
  assert.equal(rows[0].children[0].type, "checkbox");
  assert.equal(rows[1].children[0].type, "checkbox");
  assert.notEqual(rows[2].children[0].type, "checkbox");
});

it("removes a selected batch with one metadata-only request while preserving the active chat", async () => {
  const h = harness({ selected: ["first", "second", "saved-only"] });
  h.ui.conversationRemovalSelection().add("second").add("saved-only");
  const sentinel = { untouched: true };
  h.context.chatState.sessionCache.set("sentinel", sentinel);
  await h.ui.removeSelectedConversations();
  assert.deepEqual(h.posts, [{ expectedAccount: "synthetic-account", sessions: ["second", "saved-only"], selected: false }]);
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
  assert.equal(h.context.chatState.currentUser, "first");
  assert.equal(h.context.chatState.sessionCache.get("sentinel"), sentinel);
  assert.equal(h.switches.length, 0);
  assert.equal(h.ui.conversationRemovalSelection().size, 0);
  assert.match(h.byId("conversationManagerStatus").textContent, /聊天和画像已保留/);
});

it("refuses oversized removal batches and keeps selections intact on failed responses", async () => {
  const h = harness({ selected: ["first"] });
  for (let i = 0; i < 1001; i++) {
    const id = `saved-${i}`;
    h.context.chatState.selectedConversations.add(id);
    h.ui.conversationRemovalSelection().add(id);
  }
  await h.ui.removeSelectedConversations();
  assert.equal(h.posts.length, 0);
  assert.match(h.byId("conversationManagerStatus").textContent, /1000/);
  const failed = harness({ selected: ["first", "second"], postImpl: async () => { throw new Error("synthetic failure"); } });
  failed.ui.conversationRemovalSelection().add("second");
  await failed.ui.removeSelectedConversations();
  assert.deepEqual([...failed.context.chatState.selectedConversations], ["first", "second"]);
  assert.equal(failed.ui.conversationRemovalSelection().has("second"), true);
});

it("accepts a same-account removal acknowledgement during a session refresh", async () => {
  let resolve;
  const h = harness({ selected: ["first", "second"], postImpl: () => new Promise(done => { resolve = done; }) });
  h.ui.conversationRemovalSelection().add("second");
  const pending = h.ui.removeSelectedConversations();
  await new Promise(setImmediate);
  h.context.chatState.sessionRequest++;
  resolve({ account: "synthetic-account", selectedSessions: ["first"] });
  await pending;
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
});

it("ignores a delayed batch response after switching account or refreshing selection ownership", async () => {
  let resolve;
  const h = harness({ selected: ["first", "second"], postImpl: () => new Promise(done => { resolve = done; }) });
  h.ui.conversationRemovalSelection().add("second");
  const pending = h.ui.removeSelectedConversations();
  await new Promise(setImmediate);
  h.context.chatState.currentAccount = "new-account";
  h.context.chatState.selectionLoadedAccount = "new-account";
  h.context.chatState.selectedConversations = new Set(["new-chat"]);
  resolve({ account: "synthetic-account", selectedSessions: ["first"] });
  await pending;
  assert.deepEqual([...h.context.chatState.selectedConversations], ["new-chat"]);
  assert.equal(h.posts.length, 1);
});

it("selects and removes 400 added conversations, including saved-only entries, in one confirmed request", async () => {
  const selected = Array.from({ length: 400 }, (_, index) => `saved-${index}`);
  const h = harness({ selected });
  h.ui.renderConversationManager();
  const rows = [...h.byId("conversationManagerList").children];
  h.byId("conversationManagerList").scrollTop = 350;
  h.ui.selectAllRemovableConversations(true);
  assert.equal(h.ui.conversationRemovalSelection().size, 400);
  assert.equal(h.byId("conversationSelectAll").checked, true);
  assert.equal(h.byId("conversationSelectAll").indeterminate, false);
  assert.deepEqual(h.byId("conversationManagerList").children, rows, "selection preserves rendered rows");
  assert.equal(h.byId("conversationManagerList").scrollTop, 350);
  assert.equal(h.ui.conversationRemovalSelection().has("first"), false, "unadded entries cannot be selected");
  await h.ui.removeSelectedConversations();
  assert.equal(h.confirmations.length, 1);
  assert.match(h.confirmations[0].title, /400/);
  assert.match(h.confirmations[0].message, /关闭自动添加新会话/);
  assert.equal(h.posts.length, 1);
  assert.deepEqual(h.posts[0].sessions, selected);
  assert.equal(h.context.chatState.selectedConversations.size, 0);
  assert.equal(h.context.chatState.currentUser, null);
  assert.deepEqual(h.forgottenFollow, ["synthetic-account"]);
  assert.equal(h.byId("conversationAutoFollow").checked, false);
});

it("selects only matching added conversations and keeps hidden selections explicit", () => {
  const h = harness({ selected: ["first", "second", "saved-only"] });
  h.ui.renderConversationManager();
  const firstCheckbox = h.byId("conversationManagerList").children[0].children[0];
  firstCheckbox.checked = true;
  firstCheckbox.listeners.change[0]();
  assert.equal(h.byId("conversationSelectAll").checked, false);
  assert.equal(h.byId("conversationSelectAll").indeterminate, true);
  h.byId("conversationSearch").value = "合成乙";
  h.ui.renderConversationManager();
  assert.match(h.byId("conversationSelectionSummary").textContent, /其中 1 个不在当前搜索结果中/);
  h.ui.selectAllRemovableConversations(true);
  assert.deepEqual([...h.ui.conversationRemovalSelection()], ["first", "second"]);
  assert.equal(h.byId("conversationSelectAll").checked, true);
  assert.match(h.byId("conversationSelectAllLabel").textContent, /搜索结果.*（1）/);
  h.ui.selectAllRemovableConversations(false);
  assert.deepEqual([...h.ui.conversationRemovalSelection()], ["first"]);
  assert.equal(h.byId("conversationSelectAll").checked, false);
  h.ui.clearConversationRemovalSelection();
  assert.equal(h.ui.conversationRemovalSelection().size, 0);
  assert.equal(h.byId("btnClearConversationSelection").disabled, true);
  assert.equal(h.byId("btnRemoveSelectedConversations").disabled, true);
  assert.equal(h.posts.length, 0);
});

it("finishes clearing the list and active chat when saving the auto-follow preference fails", async () => {
  const h = harness({ selected: ["first", "second"], forgetFollowFails: true });
  h.ui.renderSessions();
  h.ui.selectAllRemovableConversations(true);
  await h.ui.removeSelectedConversations();
  assert.equal(h.posts.length, 1);
  assert.deepEqual(h.posts[0].sessions, ["first", "second"]);
  assert.equal(h.context.chatState.selectedConversations.size, 0);
  assert.equal(h.ui.conversationRemovalSelection().size, 0);
  assert.equal(h.context.chatState.currentUser, null);
  assert.equal(h.byId("sessionList").children.some(row => row.dataset.id), false);
  assert.equal(h.context.chatState.conversationSelectionBusy, false);
  assert.equal(h.byId("btnRemoveSelectedConversations").disabled, true);
  assert.deepEqual(h.forgottenFollow, ["synthetic-account"]);
  assert.equal(h.byId("conversationAutoFollow").checked, true, "failed storage keeps the persisted preference visible");
  assert.match(h.byId("conversationManagerStatus").textContent, /已移除 2 个会话，聊天和画像已保留/);
  assert.match(h.byId("conversationManagerStatus").textContent, /自动添加设置保存失败，请手动关闭/);
});

it("keeps selection intact and sends no request when batch removal is cancelled", async () => {
  const h = harness({ selected: ["first", "second"], confirmImpl: () => false });
  h.ui.conversationRemovalSelection().add("first").add("second");
  h.byId("conversationSearch").value = "合成乙";
  await h.ui.removeSelectedConversations();
  assert.equal(h.posts.length, 0);
  assert.equal(h.confirmations.length, 1);
  assert.match(h.confirmations[0].message, /其中 1 个不在当前搜索结果中/);
  assert.deepEqual([...h.ui.conversationRemovalSelection()], ["first", "second"]);
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first", "second"]);
  assert.deepEqual(h.forgottenFollow, []);
  assert.equal(h.context.chatState.conversationSelectionBusy, false);
  assert.equal(h.byId("btnRemoveSelectedConversations").disabled, false);
});

it("locks management controls and ignores selection changes while confirmation is pending", async () => {
  let settle;
  const h = harness({ selected: ["first", "second"], confirmImpl: () => new Promise(resolve => { settle = resolve; }) });
  h.ui.conversationRemovalSelection().add("first");
  const pending = h.ui.removeSelectedConversations();
  assert.equal(h.context.chatState.conversationSelectionBusy, true);
  for (const id of ["conversationSelectAll", "btnClearConversationSelection", "btnRemoveSelectedConversations",
    "btnAddAllConversations", "conversationAutoFollow", "conversationSearch"]) {
    assert.equal(h.byId(id).disabled, true, `${id} is disabled during confirmation`);
  }
  for (const row of h.byId("conversationManagerList").children) {
    assert.equal(row.children.at(-1).disabled, true);
    assert.equal(row.children[0].disabled, true);
  }
  h.ui.selectAllRemovableConversations(true);
  h.ui.clearConversationRemovalSelection();
  await h.ui.toggleConversationSelected("second");
  await h.ui.removeSelectedConversations();
  assert.deepEqual([...h.ui.conversationRemovalSelection()], ["first"]);
  assert.equal(h.confirmations.length, 1);
  assert.equal(h.posts.length, 0);
  settle(false);
  await pending;
  assert.equal(h.byId("conversationSearch").disabled, false);
});

it("discards a confirmation after switching account and resets account-owned checkboxes", async () => {
  let settle;
  const h = harness({ selected: ["first", "second"], confirmImpl: () => new Promise(resolve => { settle = resolve; }) });
  h.ui.conversationRemovalSelection().add("second");
  const pending = h.ui.removeSelectedConversations();
  h.context.chatState.currentAccount = "new-account";
  h.context.chatState.selectionLoadedAccount = "new-account";
  h.context.chatState.selectedConversations = new Set(["new-chat"]);
  assert.equal(h.ui.conversationRemovalSelection().size, 0);
  settle(true);
  await pending;
  assert.equal(h.posts.length, 0);
  assert.deepEqual([...h.context.chatState.selectedConversations], ["new-chat"]);
  assert.deepEqual(h.forgottenFollow, []);
});

it("deletes the right-clicked chat with one metadata-only request and preserves another active chat", async () => {
  const h = harness({ selected: ["first", "second"] });
  const saved = { messages: ["synthetic retained message"] };
  h.context.chatState.sessionCache.set("saved", saved);
  h.ui.conversationRemovalSelection().add("first").add("second");
  await h.ui.deleteConversation("second", "synthetic-account");
  assert.equal(h.confirmations.length, 1);
  assert.match(h.confirmations[0].title, /合成乙/);
  assert.match(h.confirmations[0].message, /聊天记录、已有画像和助手记录会保留/);
  assert.deepEqual(h.posts, [{ expectedAccount: "synthetic-account", session: "second", selected: false }]);
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
  assert.equal(h.context.chatState.currentUser, "first");
  assert.equal(h.context.chatState.sessionCache.get("saved"), saved);
  assert.deepEqual(h.switches, []);
  assert.deepEqual(h.preloads, []);
  assert.deepEqual([...h.ui.conversationRemovalSelection()], ["first"]);
  assert.deepEqual(h.remembered, [{ account: "synthetic-account", ids: ["second"] }]);
  assert.equal(h.context.chatState.conversationSelectionBusy, false);
});

it("deleting the active chat chooses the next chat and deleting the last chat clears the pane", async () => {
  const h = harness({ selected: ["first", "second"] });
  await h.ui.deleteConversation("first", "synthetic-account");
  assert.deepEqual(h.switches, ["second"]);
  const last = harness({ selected: ["first"], messagesReady: false });
  await last.ui.deleteConversation("first", "synthetic-account");
  assert.equal(last.context.chatState.selectedConversations.size, 0);
  assert.equal(last.context.chatState.currentUser, null);
  assert.deepEqual(last.forgottenFollow, ["synthetic-account"]);
});

it("cancelled or failed deletion keeps the selected chat and shows errors in the main window", async () => {
  const cancelled = harness({ selected: ["first"], confirmImpl: () => false });
  await cancelled.ui.deleteConversation("first", "synthetic-account");
  assert.equal(cancelled.posts.length, 0);
  assert.deepEqual([...cancelled.context.chatState.selectedConversations], ["first"]);
  const failed = harness({ selected: ["first"], postImpl: () => { throw new Error("Synthetic failure"); } });
  await failed.ui.deleteConversation("first", "synthetic-account");
  assert.deepEqual([...failed.context.chatState.selectedConversations], ["first"]);
  assert.equal(failed.context.chatState.currentUser, "first");
  assert.match(failed.stripStatuses.at(-1), /删除失败/);
  assert.equal(failed.context.chatState.conversationSelectionBusy, false);
  const malformed = harness({ selected: ["first"], postImpl: () => ({ account: "synthetic-account", selectedSessions: ["first"] }) });
  await malformed.ui.deleteConversation("first", "synthetic-account");
  assert.deepEqual([...malformed.context.chatState.selectedConversations], ["first"]);
  assert.match(malformed.stripStatuses.at(-1), /删除失败/);
});

it("dismisses a stale deletion confirmation after the account changes", async () => {
  let settle;
  const h = harness({ selected: ["first"], confirmImpl: () => new Promise(resolve => { settle = resolve; }) });
  const pending = h.ui.deleteConversation("first", "synthetic-account");
  h.context.chatState.currentAccount = "new-account";
  h.context.chatState.selectionLoadedAccount = "new-account";
  h.context.chatState.conversationSelectionEpoch++;
  h.context.chatState.conversationSelectionBusy = false;
  h.context.chatState.selectedConversations = new Set(["new-chat"]);
  settle(true);
  await pending;
  assert.equal(h.posts.length, 0);
  assert.deepEqual([...h.context.chatState.selectedConversations], ["new-chat"]);
});

it("ignores deletion responses from an earlier account visit without unlocking a newer operation", async () => {
  let settle;
  const h = harness({ selected: ["first"], postImpl: () => new Promise(resolve => { settle = resolve; }) });
  const pending = h.ui.deleteConversation("first", "synthetic-account");
  await new Promise(setImmediate);
  h.context.chatState.conversationSelectionEpoch++;
  h.context.chatState.selectedConversations = new Set(["first", "restored-chat"]);
  h.context.chatState.conversationSelectionBusy = true;
  settle({ account: "synthetic-account", selectedSessions: [] });
  await pending;
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first", "restored-chat"]);
  assert.equal(h.context.chatState.conversationSelectionBusy, true);
  assert.equal(h.stripStatuses.length, 0);
});

it("accepts deletion during a same-account session refresh but blocks overlapping selection operations", async () => {
  let settle;
  const h = harness({ selected: ["first", "second"], postImpl: () => new Promise(resolve => { settle = resolve; }) });
  const pending = h.ui.deleteConversation("second", "synthetic-account");
  await new Promise(setImmediate);
  h.context.chatState.sessionRequest++;
  await h.ui.deleteConversation("first", "synthetic-account");
  await h.ui.toggleConversationSelected("first");
  assert.equal(h.posts.length, 1);
  settle({ account: "synthetic-account", selectedSessions: ["first"] });
  await pending;
  assert.deepEqual([...h.context.chatState.selectedConversations], ["first"]);
  assert.equal(h.context.chatState.conversationSelectionBusy, false);
});

it("old single and batch selection responses cannot replace selection or unlock a newer account operation", async () => {
  for (const action of ["single", "batch"]) {
    let settle;
    const h = harness({ selected: ["first", "second"], postImpl: () => new Promise(resolve => { settle = resolve; }) });
    h.ui.conversationRemovalSelection().add("second");
    const pending = action === "single" ? h.ui.toggleConversationSelected("second") : h.ui.removeSelectedConversations();
    await new Promise(setImmediate);
    h.context.chatState.conversationSelectionEpoch++;
    h.context.chatState.selectedConversations = new Set(["first", "second", "restored-chat"]);
    h.context.chatState.conversationSelectionBusy = true;
    settle({ account: "synthetic-account", selectedSessions: ["first"] });
    await pending;
    assert.deepEqual([...h.context.chatState.selectedConversations], ["first", "second", "restored-chat"], action);
    assert.equal(h.context.chatState.conversationSelectionBusy, true, action);
  }
});
