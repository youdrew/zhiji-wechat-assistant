// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/conversation-menu.js"), "utf8");
const storageKey = account => `conversation-pins-v1:${account}`;

function events(target = {}) {
  target.listeners = new Map();
  target.addEventListener = (type, callback) => {
    if (!target.listeners.has(type)) target.listeners.set(type, []);
    target.listeners.get(type).push(callback);
  };
  target.removeEventListener = (type, callback) => {
    target.listeners.set(type, (target.listeners.get(type) || []).filter(value => value !== callback));
  };
  target.emit = (type, properties = {}) => {
    const event = { type, target, currentTarget: target, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; }, ...properties };
    event.finished = Promise.all((target.listeners.get(type) || []).map(callback => callback(event)));
    return event;
  };
  return target;
}

function harness({ account = "synthetic-a", storage = new Map(), storageFails = false, zoom = 1, renderImpl, openChatWindow } = {}) {
  let document;
  class Node {
    constructor(id = "", tag = "div") {
      events(this);
      this.id = id; this.tagName = tag.toUpperCase(); this.dataset = {}; this.attributes = {};
      this.style = {}; this.children = []; this.parentNode = null; this.hidden = false;
      this.disabled = false; this.textContent = ""; this.className = "";
      this.rect = { left: 20, top: 30, width: 240, height: 64 };
      this.classList = {
        contains: name => this.className.split(/\s+/).includes(name),
        add: name => { if (!this.classList.contains(name)) this.className = `${this.className} ${name}`.trim(); },
        remove: name => { this.className = this.className.split(/\s+/).filter(value => value !== name).join(" "); },
        toggle: (name, enabled) => this.classList[enabled ? "add" : "remove"](name),
      };
    }
    append(...children) {
      for (const child of children) { child.remove(); child.parentNode = this; this.children.push(child); }
    }
    remove() {
      if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
      this.parentNode = null;
    }
    get isConnected() { return this === document.body || Boolean(this.parentNode?.isConnected); }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    removeAttribute(name) { delete this.attributes[name]; }
    focus() { document.activeElement = this; }
    getBoundingClientRect() {
      const left = (parseFloat(this.style.left) || this.rect.left) * zoom;
      const top = (parseFloat(this.style.top) || this.rect.top) * zoom;
      const width = this.rect.width * zoom, height = this.rect.height * zoom;
      return { left, top, width, height, right: left + width, bottom: top + height };
    }
    get offsetWidth() { return this.rect.width; }
    get offsetHeight() { return this.rect.height; }
    querySelectorAll(selector) {
      const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll("*")]);
      return descendants.filter(child => selector === "*" || selector === "button" && child.tagName === "BUTTON" ||
        selector === '[role="menuitem"]' && child.getAttribute("role") === "menuitem" ||
        selector.startsWith(".") && child.classList.contains(selector.slice(1)));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    click() { return this.disabled ? Promise.resolve() : this.emit("click").finished; }
  }
  document = events({ documentElement: { clientWidth: 800, clientHeight: 600,
    style: { zoom: String(zoom) } }, activeElement: null });
  document.body = new Node("body", "body");
  const nodes = new Map();
  for (const id of ["sessionContextMenu", "btnPinConversation", "btnOpenConversationWindow", "btnDeleteConversation", "sessionList"]) {
    nodes.set(id, new Node(id, id.startsWith("btn") ? "button" : "div"));
  }
  const byId = id => nodes.get(id);
  const menu = byId("sessionContextMenu"), pin = byId("btnPinConversation"), remove = byId("btnDeleteConversation");
  menu.hidden = true; menu.rect = { left: 0, top: 0, width: 180, height: 100 };
  const popout = byId("btnOpenConversationWindow");
  pin.setAttribute("role", "menuitem"); popout.setAttribute("role", "menuitem"); remove.setAttribute("role", "menuitem");
  menu.append(pin, popout, remove); document.body.append(byId("sessionList"), menu);
  document.getElementById = byId;
  const window = events({ innerWidth: 800, innerHeight: 600,
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem(key, value) { if (storageFails) throw new Error("Synthetic storage failure"); storage.set(key, String(value)); },
    } });
  const state = {
    currentAccount: account, selectionLoadedAccount: account, currentUser: "first",
    selectedConversations: new Set(["first", "second", "source-pinned"]), conversationSelectionBusy: false,
    conversationSelectionEpoch: 0,
    sessions: new Map([
      ["first", { username: "first", name: "合成甲", pinned: false, time: 100, unread: 3 }],
      ["second", { username: "second", name: "合成乙", pinned: false, time: 200, unread: 5 }],
      ["source-pinned", { username: "source-pinned", name: "源置顶", pinned: true, time: 50 }],
    ]),
  };
  const deletions = [], statuses = [];
  let renders = 0;
  const context = vm.createContext({ window, document, console, localStorage: window.localStorage });
  vm.runInContext(source, context);
  const ui = window.ConversationMenu;
  const init = () => ui.init({ state, byId, renderSessions: () => {
    renders++;
    if (renderImpl) renderImpl({ rows, Node, byId, document, state, ui });
  },
    deleteConversation: (...args) => { deletions.push(args); }, setStatus: (...args) => { statuses.push(args); },
    ...(openChatWindow ? { openChatWindow } : {}) });
  init();
  const rows = new Map();
  for (const username of state.selectedConversations) {
    const row = new Node(username); row.dataset.id = username; row.className = "session-item";
    byId("sessionList").append(row); ui.attach(row); rows.set(username, row);
  }
  return { ui, state, window, document, byId, rows, menu, pin, popout, remove, storage, deletions, statuses, init,
    get renders() { return renders; },
    open(username = "second", x = 140, y = 220) {
      return rows.get(username).emit("contextmenu", { clientX: x, clientY: y, button: 2 });
    },
    sorted() { return Array.from(ui.sortSessions([...state.sessions.values()]), value => value.username); },
  };
}

test("right-click opens at the pointer and preserves the active chat, selection and unread counts", () => {
  const h = harness();
  const before = JSON.stringify([...h.state.sessions]);
  const event = h.open();
  assert.equal(event.defaultPrevented, true);
  assert.equal(h.menu.hidden, false);
  assert.equal(h.menu.style.left, "140px");
  assert.equal(h.menu.style.top, "220px");
  assert.equal(h.pin.textContent, "置顶");
  assert.equal(h.state.currentUser, "first");
  assert.deepEqual([...h.state.selectedConversations], ["first", "second", "source-pinned"]);
  assert.equal(JSON.stringify([...h.state.sessions]), before);
  assert.equal(h.renders, 0);
  assert.deepEqual(h.deletions, []);
});

test("pin and unpin change local ordering and label without mutating source metadata", async () => {
  const h = harness();
  assert.deepEqual(h.sorted(), ["source-pinned", "second", "first"]);
  h.open("first"); await h.pin.click();
  assert.equal(h.ui.isPinned(h.state.sessions.get("first")), true);
  assert.equal(h.state.sessions.get("first").pinned, false);
  assert.equal(h.menu.hidden, true);
  assert.equal(h.renders, 1);
  assert.deepEqual(h.sorted(), ["first", "source-pinned", "second"]);
  assert.equal(JSON.parse(h.storage.get(storageKey("synthetic-a"))).first, true);
  h.open("first"); assert.equal(h.pin.textContent, "取消置顶");
  await h.pin.click();
  assert.equal(h.ui.isPinned(h.state.sessions.get("first")), false);
  assert.deepEqual(h.sorted(), ["source-pinned", "second", "first"]);
  assert.equal(h.renders, 2);
});

test("unpinning a source-pinned conversation persists an explicit false override", async () => {
  const h = harness();
  h.open("source-pinned"); assert.equal(h.pin.textContent, "取消置顶");
  await h.pin.click();
  assert.equal(h.state.sessions.get("source-pinned").pinned, true);
  assert.equal(h.ui.isPinned(h.state.sessions.get("source-pinned")), false);
  assert.equal(JSON.parse(h.storage.get(storageKey("synthetic-a")))["source-pinned"], false);
  assert.deepEqual(h.sorted(), ["second", "first", "source-pinned"]);
  const reopened = harness({ storage: h.storage });
  assert.equal(reopened.ui.isPinned(reopened.state.sessions.get("source-pinned")), false);
});

test("pinning returns keyboard focus to the targeted row after reordering or replacing it", async () => {
  for (const replace of [false, true]) {
    const h = harness({ renderImpl({ rows, Node, byId, document, ui }) {
      let row = rows.get("first");
      row.remove();
      if (replace) {
        row = new Node("replacement-first");
        row.dataset.id = "first";
        row.className = "session-item";
        rows.set("first", row);
        ui.attach(row);
      }
      byId("sessionList").append(row);
      document.activeElement = document.body; // Hiding/reparenting the focused content loses focus.
    } });
    const original = h.rows.get("first");
    original.focus();
    original.emit("keydown", { key: "ContextMenu" });
    assert.equal(h.document.activeElement, h.pin);
    await h.pin.click();
    assert.equal(h.menu.hidden, true);
    assert.equal(h.document.activeElement, h.rows.get("first"));
    assert.equal(h.state.currentUser, "first");
    assert.equal(h.ui.isPinned(h.state.sessions.get("first")), true);
  }
});

test("pinning never restores focus into a stale account visit after rendering", async () => {
  const h = harness({ renderImpl({ document, state }) {
    document.activeElement = document.body;
    state.conversationSelectionEpoch++;
  } });
  h.open();
  await h.pin.click();
  assert.equal(h.menu.hidden, true);
  assert.equal(h.document.activeElement, h.document.body);
});

test("pins survive initialization and a new page but remain isolated between accounts", async () => {
  const h = harness(); h.open("first"); await h.pin.click();
  h.init();
  assert.equal(h.ui.isPinned(h.state.sessions.get("first")), true);
  const reopened = harness({ storage: h.storage });
  assert.equal(reopened.ui.isPinned(reopened.state.sessions.get("first")), true);
  h.state.currentAccount = h.state.selectionLoadedAccount = "synthetic-b";
  h.ui.refresh();
  assert.equal(h.ui.isPinned(h.state.sessions.get("first")), false);
  h.open("second"); await h.pin.click();
  assert.equal(JSON.parse(h.storage.get(storageKey("synthetic-b"))).second, true);
  h.state.currentAccount = h.state.selectionLoadedAccount = "synthetic-a";
  h.ui.refresh();
  assert.equal(h.ui.isPinned(h.state.sessions.get("first")), true);
  assert.equal(h.ui.isPinned(h.state.sessions.get("second")), false);
});

test("latest timestamps sort within each pin group and sorting preserves the input array", () => {
  const h = harness();
  const first = h.state.sessions.get("first"), second = h.state.sessions.get("second");
  first.sortTimestamp = 300;
  const source = [second, first, h.state.sessions.get("source-pinned")];
  const original = [...source];
  assert.deepEqual(Array.from(h.ui.sortSessions(source), value => value.username), ["source-pinned", "first", "second"]);
  assert.deepEqual(source, original);
});

test("a failed storage write preserves the previous pin preference and source values", async () => {
  const storage = new Map([[storageKey("synthetic-a"), '{"first":true}']]);
  const h = harness({ storage, storageFails: true });
  h.open("first"); await h.pin.click();
  assert.equal(h.ui.isPinned(h.state.sessions.get("first")), true);
  assert.equal(h.state.sessions.get("first").pinned, false);
  assert.equal(h.storage.get(storageKey("synthetic-a")), '{"first":true}');
  assert.equal(h.renders, 0);
  assert.ok(h.statuses.length > 0, "A failed pin save is reported to the user");
  h.open("second"); await h.pin.click();
  assert.equal(h.ui.isPinned(h.state.sessions.get("second")), false);
});

test("unloaded accounts, missing accounts and pending conversation actions cannot open a menu", () => {
  for (const update of [
    state => { state.currentAccount = null; state.selectionLoadedAccount = null; },
    state => { state.selectionLoadedAccount = "other-account"; },
    state => { state.conversationSelectionBusy = true; },
    state => { state.selectedConversations.delete("second"); },
  ]) {
    const h = harness(); update(h.state); h.open();
    assert.equal(h.menu.hidden, true);
    assert.equal(h.renders, 0);
    assert.equal(h.storage.size, 0);
    assert.deepEqual(h.deletions, []);
  }
});

test("delete captures the right-clicked conversation and its account instead of the active chat", async () => {
  const h = harness(); h.open("second"); await h.remove.click();
  assert.equal(h.menu.hidden, true);
  assert.deepEqual(h.deletions, [["second", "synthetic-a", 0]]);
  assert.equal(h.state.currentUser, "first");
  assert.equal(h.state.sessions.get("second").unread, 5);
});

test("a stale menu cannot pin or delete after account ownership or selection changes", async () => {
  for (const action of ["pin", "remove"]) {
    for (const update of [
      state => { state.currentAccount = state.selectionLoadedAccount = "synthetic-b"; },
      state => { state.selectionLoadedAccount = null; },
      state => { state.conversationSelectionEpoch++; },
      state => { state.selectedConversations.delete("second"); },
      state => { state.conversationSelectionBusy = true; },
    ]) {
      const h = harness(); h.open(); update(h.state); await h[action].click();
      assert.equal(h.menu.hidden, true);
      assert.deepEqual(h.deletions, []);
      assert.equal(h.storage.size, 0);
    }
  }
});

test("outside clicks, Escape, list scrolling and window resizing dismiss the menu", () => {
  for (const dismiss of [
    h => h.document.emit("pointerdown", { target: h.document.body }),
    h => h.document.emit("keydown", { key: "Escape", target: h.pin }),
    h => h.byId("sessionList").emit("scroll"),
    h => h.window.emit("resize"),
  ]) {
    const h = harness(); h.open(); dismiss(h);
    assert.equal(h.menu.hidden, true);
    assert.equal(h.state.currentUser, "first");
    assert.deepEqual(h.deletions, []);
  }
  const h = harness(); h.open();
  h.document.emit("pointerdown", { target: h.pin });
  assert.equal(h.menu.hidden, false, "Menu item pointer events must reach the action");
});

test("menu placement stays inside the window at each edge", () => {
  const h = harness();
  for (const [x, y] of [[799, 599], [-20, -40], [900, 20], [10, 900]]) {
    h.open("second", x, y);
    const left = parseFloat(h.menu.style.left), top = parseFloat(h.menu.style.top);
    assert.ok(left >= 0 && left + h.menu.offsetWidth <= h.window.innerWidth);
    assert.ok(top >= 0 && top + h.menu.offsetHeight <= h.window.innerHeight);
  }
});

test("page zoom keeps the rendered menu at the pointer and clamps it to the physical viewport", () => {
  for (const zoom of [0.9, 1, 1.1, 1.25, 1.5]) {
    const h = harness({ zoom });
    h.open("second", 140, 220);
    let rendered = h.menu.getBoundingClientRect();
    assert.ok(Math.abs(rendered.left - 140) < 0.001, `Zoom ${zoom} aligns the rendered menu horizontally`);
    assert.ok(Math.abs(rendered.top - 220) < 0.001, `Zoom ${zoom} aligns the rendered menu vertically`);
    h.open("second", h.window.innerWidth - 1, h.window.innerHeight - 1);
    rendered = h.menu.getBoundingClientRect();
    assert.ok(rendered.left >= 8 && rendered.right <= h.window.innerWidth - 8, `Zoom ${zoom} respects the right edge`);
    assert.ok(rendered.top >= 8 && rendered.bottom <= h.window.innerHeight - 8, `Zoom ${zoom} respects the bottom edge`);
  }
});

test("refresh closes menus when the account, selected target or attached row becomes stale", () => {
  for (const invalidate of [
    h => { h.state.currentAccount = h.state.selectionLoadedAccount = "synthetic-b"; },
    h => h.state.selectedConversations.delete("second"),
    h => h.rows.get("second").remove(),
  ]) {
    const h = harness(); h.open(); invalidate(h); h.ui.refresh();
    assert.equal(h.menu.hidden, true);
    assert.deepEqual(h.deletions, []);
  }
});

test("keyboard context menus focus the first action, navigate actions and restore row focus on Escape", () => {
  for (const properties of [{ key: "ContextMenu" }, { key: "F10", shiftKey: true }]) {
    const h = harness(), row = h.rows.get("second"); row.focus();
    const open = row.emit("keydown", properties);
    assert.equal(open.defaultPrevented, true);
    assert.equal(h.menu.hidden, false);
    assert.equal(h.document.activeElement, h.pin);
    h.menu.emit("keydown", { key: "ArrowDown", target: h.pin });
    assert.equal(h.document.activeElement, h.remove);
    h.menu.emit("keydown", { key: "ArrowDown", target: h.remove });
    assert.equal(h.document.activeElement, h.pin);
    h.menu.emit("keydown", { key: "ArrowUp", target: h.pin });
    assert.equal(h.document.activeElement, h.remove);
    h.document.emit("keydown", { key: "Escape", target: h.remove });
    assert.equal(h.menu.hidden, true);
    assert.equal(h.document.activeElement, row);
    assert.equal(h.state.currentUser, "first");
  }
});

test("repeated attachment and initialization bind one action per user click", async () => {
  const h = harness(); h.ui.attach(h.rows.get("second")); h.init();
  h.open(); await h.pin.click();
  assert.equal(h.renders, 1);
  assert.equal(h.ui.isPinned(h.state.sessions.get("second")), true);
  h.open(); await h.remove.click();
  assert.equal(h.deletions.length, 1);
});

test("clearing an account discovers and removes its stored pins while preserving another account", async () => {
  const app = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
  const start = app.indexOf("async function clearAccountUiData(");
  const end = app.indexOf("async function deleteManagedAccount(", start);
  assert.ok(start >= 0 && end > start, "The account cleanup function is available");
  const account = "synthetic-cleared-account", other = "synthetic-kept-account";
  const accountId = crypto.createHash("sha256").update(account).digest("hex");
  const pins = '{"synthetic-friend":true}';
  function storedPins() {
    const values = new Map([[storageKey(account), pins], [storageKey(other), pins]]);
    return {
      values,
      get length() { return values.size; },
      key: index => [...values.keys()][index] ?? null,
      removeItem: key => values.delete(key),
    };
  }
  const localStorage = storedPins(), sessionStorage = storedPins();
  const context = vm.createContext({
    crypto: crypto.webcrypto, TextEncoder, localStorage, sessionStorage,
    window: {}, // The composer is optional in this storage-only account cleanup fixture.
    chatState: { currentAccount: null, sessionCache: new Map() },
  });
  vm.runInContext(app.slice(start, end) + "\nglobalThis.clearAccount = clearAccountUiData;", context);
  const cleared = await context.clearAccount(accountId);
  assert.deepEqual([...cleared], [account], "Pin keys alone identify the account behind its public hash");
  for (const storage of [localStorage, sessionStorage]) {
    assert.equal(storage.values.has(storageKey(account)), false);
    assert.equal(storage.values.get(storageKey(other)), pins);
  }
});

// Added 2026-11-04: 在独立窗口中打开 pops the conversation out, only where the desktop host offers it.
test("the pop-out item appears only with a desktop host, opens the targeted chat and keeps keyboard order", async () => {
  const plain = harness();
  plain.open("second");
  assert.equal(plain.popout.hidden, true);
  plain.menu.emit("keydown", { key: "ArrowDown" });
  assert.equal(plain.document.activeElement, plain.remove, "A hidden item is skipped by the arrow keys");

  const opened = [];
  const h = harness({ openChatWindow: user => opened.push(user) });
  h.open("second");
  assert.equal(h.popout.hidden, false);
  h.menu.emit("keydown", { key: "ArrowDown" });
  assert.equal(h.document.activeElement, h.popout);
  await h.popout.click();
  assert.deepEqual(opened, ["second"]);
  assert.equal(h.menu.hidden, true);
  assert.equal(h.state.currentUser, "first", "Opening a window does not switch the main conversation");
  assert.deepEqual(h.deletions, []);
  h.open("second"); h.state.conversationSelectionEpoch++;
  await h.popout.click();
  assert.deepEqual(opened, ["second"], "A stale menu target opens nothing");
});
