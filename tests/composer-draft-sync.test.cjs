// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Cross-window draft sync (2026-11-04): two windows showing one contact share its draft in memory.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const storageUse = [];
const forbiddenStorage = new Proxy({}, { get(_target, name) { storageUse.push(String(name)); throw new Error("storage used"); } });
const modules = vm.createContext({ window: {}, localStorage: forbiddenStorage, sessionStorage: forbiddenStorage });
for (const file of ["composer-drafts.js", "chat-composer.js"])
  vm.runInContext(readFileSync(path.join(__dirname, "../chatui", file), "utf8"), modules);
const { ComposerDrafts, Composer } = modules.window;

// A BroadcastChannel stand-in: a message reaches every other channel of the bus (never its sender),
// structured-cloned, when the test flushes; the queue keeps posting order.
function bus() {
  const channels = new Set(), queue = [], log = [];
  function channel() {
    const listeners = new Set();
    const self = { closed: false,
      postMessage(data) { if (self.closed) throw new Error("closed"); log.push(structuredClone(data)); for (const peer of channels) if (peer !== self) queue.push([peer, structuredClone(data)]); },
      addEventListener(type, callback) { if (type === "message") listeners.add(callback); },
      removeEventListener(type, callback) { listeners.delete(callback); },
      close() { self.closed = true; channels.delete(self); },
      deliver(data) { if (!self.closed) for (const callback of [...listeners]) callback({ data }); } };
    channels.add(self);
    return self;
  }
  function flush() { while (queue.length) { const [peer, data] = queue.shift(); peer.deliver(data); } }
  return { channel, flush, log, queue };
}
const A = "synthetic-account-a", B = "synthetic-account-b", F = "synthetic-friend", G = "synthetic-friend-g";

test("an edit reaches the peer showing the same contact and is never echoed back", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  main.select(A, F); popout.select(A, F); net.flush();
  const changes = [];
  popout.subscribe(change => changes.push({ ...change }));
  main.update("你好"); net.flush();
  assert.equal(popout.snapshot().text, "你好");
  assert.deepEqual(changes, [{ account: A, username: F }]);
  assert.equal(net.queue.length, 0, "Receiving does not broadcast again");
  assert.equal(main.snapshot().text, "你好");
  assert.deepEqual(storageUse, []);
});

test("a peer edit revises the receiving draft so an earlier snapshot is no longer current", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  main.select(A, F); popout.select(A, F);
  popout.update("old"); net.flush();
  const captured = main.snapshot();
  assert.equal(captured.text, "old");
  popout.update("new"); net.flush();
  assert.equal(main.isCurrent(captured), false);
  assert.ok(main.snapshot().draftRevision > captured.draftRevision);
  // An identical text keeps the revision: nothing in flight is revoked for no change.
  const same = main.snapshot();
  assert.equal(main.receive({ type: "draft", account: A, username: F, text: "new", clock: 99, origin: "zzz" }), false);
  assert.equal(main.isCurrent(same), true);
});

test("concurrent edits converge on the newest stamp in every window regardless of delivery order", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  main.select(A, F); popout.select(A, F); net.flush();
  main.update("from main"); popout.update("from popout"); // same clock: origin breaks the tie
  net.flush();
  assert.equal(main.snapshot().text, popout.snapshot().text);
  assert.equal(main.snapshot().text, "from popout");
  // A later local edit after seeing the peer always wins.
  main.update("main again"); net.flush();
  assert.equal(popout.snapshot().text, "main again");
  // A stale (older) message delivered late cannot roll the draft back.
  assert.equal(popout.receive({ type: "draft", account: A, username: F, text: "stale", clock: 1, origin: "main" }), false);
  assert.equal(popout.snapshot().text, "main again");
});

test("drafts of other contacts and accounts are stored under their own key without touching the shown one", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  main.select(A, G); popout.select(B, F); net.flush();
  popout.update("account b draft"); net.flush();
  assert.equal(main.snapshot().text, "");
  main.select(A, F); assert.equal(main.snapshot().text, "", "Same friend id of another account stays separate");
  main.select(B, F); assert.equal(main.snapshot().text, "account b draft");
  assert.deepEqual([...main.accounts()].sort(), [B]);
});

test("opening a contact without a draft asks peers once and adopts the reply", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  main.select(A, F); main.update("written before the popout opened"); net.flush();
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  popout.select(A, F);
  assert.deepEqual(net.log.at(-1), { type: "request", account: A, username: F });
  net.flush(); // request → main replies
  net.flush(); // reply → popout
  assert.equal(popout.snapshot().text, "written before the popout opened");
  // A forced re-selection of the same contact is not a new request.
  const before = net.log.length;
  popout.select(A, F, true);
  assert.equal(net.log.length, before);
});

test("a late reply never overrides what the opening window typed meanwhile, nor answers for an empty draft", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  main.select(A, F); main.update("peer text"); net.flush();
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  popout.select(A, F);
  net.flush(); // main has queued its reply
  popout.update("typed first");
  net.flush();
  assert.equal(popout.snapshot().text, "typed first");
  assert.equal(main.snapshot().text, "typed first", "The newer local edit still propagates");
  const empty = ComposerDrafts.create({ channel: net.channel(), origin: "empty" });
  empty.select(A, G); net.flush();
  assert.equal(net.log.filter(item => item.type === "reply" && item.username === G).length, 0);
});

test("malformed or oversized peer messages are ignored", () => {
  const drafts = ComposerDrafts.create({ origin: "solo" });
  drafts.select(A, F);
  for (const message of [null, "text", { type: "draft", account: A, username: F, text: 5, clock: 1, origin: "x" },
    { type: "draft", account: "", username: F, text: "x", clock: 1, origin: "x" },
    { type: "draft", account: A, username: F, text: "x", clock: 0, origin: "x" },
    { type: "draft", account: A, username: F, text: "x", clock: 1.5, origin: "x" },
    { type: "draft", account: A, username: F, text: "x", clock: 1 },
    { type: "draft", account: A, username: F, text: "x".repeat(200001), clock: 1, origin: "x" },
    { type: "other", account: A, username: F, text: "x", clock: 1, origin: "x" }])
    assert.equal(drafts.receive(message), false);
  assert.equal(drafts.snapshot().text, "");
});

test("a closed store neither posts nor receives", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  main.select(A, F); popout.select(A, F); main.update("draft"); net.flush();
  assert.equal(popout.snapshot().text, "draft");
  popout.close();
  main.update("after close"); net.flush();
  assert.equal(popout.receive({ type: "draft", account: A, username: F, text: "x", clock: 50, origin: "x" }), false);
  popout.select(A, F); popout.update("local only");
  net.flush();
  assert.equal(main.snapshot().text, "after close");
});

// -- composer integration -------------------------------------------------------------------------------------
function node(owner) {
  const listeners = new Map(), classes = new Set();
  const item = { value: "", disabled: false, textContent: "", hidden: false, ownerDocument: owner,
    selectionStart: 0, selectionEnd: 0,
    setSelectionRange(start, end) { item.selectionStart = start; item.selectionEnd = end; },
    classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); } },
    addEventListener(type, callback) { listeners.set(type, callback); },
    removeEventListener(type, callback) { if (listeners.get(type) === callback) listeners.delete(type); },
    emit(type, event = {}) { return listeners.get(type)?.(event); } };
  return item;
}
function windowComposer(net, origin) {
  const doc = { activeElement: null };
  const drafts = ComposerDrafts.create({ channel: net.channel(), origin });
  const input = node(doc);
  const controller = Composer.create({ drafts, input, copy() {} });
  controller.setScope(A, F, { available: true });
  return { doc, drafts, controller, input,
    type(value) { input.value = value; input.emit("input"); } };
}

test("typing in one window updates the other window's textarea", () => {
  const net = bus();
  const main = windowComposer(net, "main"), popout = windowComposer(net, "popout");
  net.flush();
  main.type("一起吃饭吗"); net.flush();
  assert.equal(popout.input.value, "一起吃饭吗");
  assert.equal(popout.input.disabled, false);
});

test("a remote change to the shown draft keeps a focused caret inside the new text", () => {
  const net = bus();
  const main = windowComposer(net, "main"), popout = windowComposer(net, "popout");
  net.flush();
  popout.type("long text here"); net.flush();
  main.doc.activeElement = main.input; main.input.selectionStart = main.input.selectionEnd = 14;
  popout.type("short"); net.flush();
  assert.equal(main.input.value, "short");
  assert.equal(main.input.selectionStart, 5);
});

test("a disposed composer ignores later peer edits", () => {
  const net = bus();
  const main = windowComposer(net, "main"), popout = windowComposer(net, "popout");
  net.flush();
  popout.controller.dispose();
  const value = popout.input.value;
  main.type("after dispose"); net.flush();
  assert.equal(popout.input.value, value);
  assert.deepEqual(storageUse, []);
});

// -- review fixes: account clearing and IME composition -------------------------------------------------
test("clearing an account is broadcast: peers drop its drafts, never offer them again and reject older ones", () => {
  const net = bus();
  const main = ComposerDrafts.create({ channel: net.channel(), origin: "main" });
  const popout = ComposerDrafts.create({ channel: net.channel(), origin: "popout" });
  main.select(A, F); popout.select(A, F); net.flush();
  main.update("account a draft"); net.flush();
  popout.select(B, F); popout.update("account b draft"); net.flush();
  const stale = net.log.find(item => item.type === "draft" && item.text === "account a draft");
  const changes = [];
  main.subscribe(change => changes.push({ ...change }));
  popout.clearAccount(A);
  assert.deepEqual(net.log.at(-1), { type: "clear", account: A, clock: net.log.at(-1).clock, origin: "popout" });
  net.flush();
  assert.equal(main.snapshot().text, "", "The shown draft of the cleared account is dropped");
  assert.deepEqual(changes, [{ account: A, username: F }]);
  assert.deepEqual([...main.accounts()], [B], "Other accounts keep their drafts");
  // An edit stamped before the clear, delivered late, is not accepted again.
  assert.equal(main.receive(stale), false);
  assert.equal(main.snapshot().text, "");
  // Nobody answers a request with the cleared draft.
  const late = ComposerDrafts.create({ channel: net.channel(), origin: "late" });
  late.select(A, F); net.flush(); net.flush();
  assert.equal(late.snapshot().text, "");
  assert.equal(net.log.filter(item => item.type === "reply" && item.account === A).length, 0);
  // The account can be used again afterwards.
  main.update("new draft after clearing"); net.flush();
  assert.equal(late.snapshot().text, "new draft after clearing");
});

test("a peer that still holds the cleared account's draft drops it and refuses to answer for it", () => {
  const net = bus();
  const holder = ComposerDrafts.create({ channel: net.channel(), origin: "holder" });
  holder.select(A, G); holder.update("held text"); net.flush();
  holder.select(B, F);
  const clearer = ComposerDrafts.create({ channel: net.channel(), origin: "clearer" });
  clearer.clearAccount(A); net.flush();
  assert.deepEqual([...holder.accounts()], []);
  assert.equal(holder.receive({ type: "request", account: A, username: G }), false);
  net.flush();
  assert.equal(net.log.filter(item => item.type === "reply").length, 0);
  assert.equal(holder.receive({ type: "clear", account: A, clock: 0 }), false, "A malformed clear is ignored");
});

test("a remote draft waits for an IME composition to end", () => {
  const net = bus();
  const main = windowComposer(net, "main"), popout = windowComposer(net, "popout");
  net.flush();
  popout.type("old text"); net.flush();
  popout.input.emit("compositionstart");
  popout.input.value = "old textni";
  main.type("peer text"); net.flush();
  assert.equal(popout.input.value, "old textni");
  assert.equal(popout.drafts.snapshot().text, "peer text");
  popout.input.emit("compositionend");
  assert.equal(popout.input.value, "peer text");
});

test("text committed by the composition after a peer edit is the newer draft and survives compositionend", () => {
  const net = bus();
  const main = windowComposer(net, "main"), popout = windowComposer(net, "popout");
  net.flush();
  popout.input.emit("compositionstart");
  main.type("peer text"); net.flush();
  popout.type("你好"); // the commit's input event precedes compositionend
  popout.input.emit("compositionend");
  net.flush();
  assert.equal(popout.input.value, "你好");
  assert.equal(main.input.value, "你好", "The local commit wins everywhere");
  // A scope change during a composition does not later replay a stale deferral.
  popout.input.emit("compositionstart");
  main.type("peer again"); net.flush();
  popout.controller.setScope(A, G, { available: true });
  popout.input.emit("compositionend");
  assert.equal(popout.input.value, "");
});
