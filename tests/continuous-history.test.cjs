// Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../chatui/app.js'), 'utf8');
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing history section: ${start}`);
  return source.slice(first, last);
}
function messages(first, count) {
  return Array.from({ length: count }, (_, index) => ({ id: `m${first + index}`,
    historyCursor: `c${first + index}`, text: `synthetic message ${first + index}`, time: first + index }));
}
function page(items, changes = {}) {
  return { account: 'synthetic-account', user: 'synthetic-friend', messages: items,
    results: {}, nextCursor: items[0]?.historyCursor || null,
    hasMoreBefore: true, hasMoreAfter: true, ...changes };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const settle = () => new Promise(setImmediate);

function harness(initial = messages(241, 80)) {
  const nodes = new Map(), requests = [], renders = [], timers = new Map(), frames = new Map(), latestReads = [];
  let timerSerial = 0;
  const chatState = {
    currentAccount: 'synthetic-account', currentUser: 'synthetic-friend', generation: 1,
    view: 'chat', currentHasMoreBefore: true, messages: initial, followLatest: true,
    lastChatScrollTop: 0, historyState: null, historyRequest: 0, historyController: null,
    historyPrefetch: null, historyPrefetchTimer: null, historyScrollFrame: null,
    messageRequest: 0, messagePending: false, sessionCache: new Map(),
  };
  const getNode = id => {
    if (!nodes.has(id)) nodes.set(id, { hidden: false, disabled: false, textContent: '', dataset: {},
      listeners: {}, classList: { toggle() {} }, setAttribute() {}, addEventListener(type, listener) {
        (this.listeners[type] ||= []).push(listener);
      } });
    return nodes.get(id);
  };
  const container = getNode('chatMessages');
  Object.assign(container, { clientHeight: 300, scrollHeight: initial.length * 30,
    scrollTop: Math.max(0, initial.length * 30 - 300),
    getBoundingClientRect() { return { top: 100, bottom: 100 + this.clientHeight }; },
    querySelectorAll() {
      return chatState.messages.map((message, index) => ({ dataset: { messageId: String(message.id) },
        classList: { add() {}, remove() {} }, getBoundingClientRect() {
          return { top: 100 + index * 30 - container.scrollTop, bottom: 130 + index * 30 - container.scrollTop };
        } }));
    },
  });
  const context = vm.createContext({
    chatState, labelState: { results: {}, selectedAnalysisTimer: null }, portraitState: { analysisGeneration: 0 },
    settingsState: { modelSourceResolved: false, settings: {} },
    historyWindowLimit: 240, historyLoadDistance: 200, historyPrefetchDistance: 800,
    AbortController, DOMException, URLSearchParams, console,
    window: {}, document: { hidden: false, visibilityState: 'visible' },
    byId: getNode, text: (id, value) => { getNode(id).textContent = value; },
    clearTimeout: id => timers.delete(id),
    setTimeout: (callback, delay) => { timers.set(++timerSerial, { callback, delay }); return timerSerial; },
    requestAnimationFrame: callback => { frames.set(++timerSerial, callback); return timerSerial; },
    cancelAnimationFrame: id => frames.delete(id),
    clearInlineIntentPending() {}, refreshLabels() {}, ensureApiInsights() {}, toast() {},
    status: (node, value) => { node.textContent = value; },
    sessionCacheKey: (account, user) => `${account}:${user}`,
    visibleResults: (results, items) => Object.fromEntries(items.filter(item => results[item.id]).map(item => [item.id, results[item.id]])),
    renderMessages: (items, restore, anchor) => {
      renders.push({ items, restore, anchor }); chatState.messages = items;
      container.scrollHeight = items.length * 30;
      if (anchor) container.scrollTop = Math.max(0, items.findIndex(item => String(item.id) === anchor.id) * 30 - anchor.top);
      else if (restore?.followLatest) container.scrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
    },
    loadMessages: async (...args) => { latestReads.push(args); },
    api: async (url, options, signal) => {
      requests.push({ url, options, signal });
      return context.response(url, signal);
    },
    response: async () => page([]),
  });
  vm.runInContext(section('function historyAnchor(', 'function cancelHistorySearch('), context);
  vm.runInContext(section('byId("chatMessages").addEventListener("scroll",', 'byId("btnChatHistory").addEventListener('), context);
  const runFrames = () => { for (const [id, callback] of [...frames]) { frames.delete(id); callback(); } };
  const runPrefetch = () => {
    for (const [id, timer] of [...timers]) if (timer.delay === 300) { timers.delete(id); timer.callback(); }
  };
  return { context, chatState, container, nodes, requests, renders, timers, latestReads, runFrames, runPrefetch };
}

test('initial prefetch reads just one older page without entering history or changing visible messages', async () => {
  const h = harness();
  h.context.response = async () => page(messages(161, 80));
  h.context.scheduleHistoryPrefetch();
  assert.equal(h.requests.length, 0);
  const timer = [...h.timers.values()].find(item => item.delay === 300);
  assert.ok(timer, 'prefetch waits until after the initial render');
  timer.callback();
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(new URL(h.requests[0].url, 'http://localhost').searchParams.get('before'), 'c241');
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.chatState.messages.length, 80);
  assert.equal(h.chatState.messages[0].id, 'm241');
  assert.equal(h.chatState.followLatest, true);
  assert.equal(h.renders.length, 0);
  await settle();
  assert.equal(h.requests.length, 1, 'prefetch must not recursively download all history');
});

test('the next 80-message window cannot evict an expanded analysis and explicit latest navigation releases it', () => {
  const initial = messages(1, 80), next = messages(2, 80), h = harness(initial);
  let reading = 'm1';
  h.context.window.MessageAnalysis = { openedMessageId: () => reading, closeReading: () => { reading = ''; } };
  h.container.scrollTop = 25;
  assert.equal(h.context.preserveAnalysisReading(next), true);
  assert.equal(h.chatState.messages, initial, 'the original DOM window remains selected');
  assert.equal(h.container.scrollTop, 25);
  assert.equal(h.chatState.historyState.afterCursor, 'c80');
  assert.equal(h.chatState.historyState.hasMoreAfter, true);
  assert.equal(h.nodes.get('btnReturnLatest').hidden, false);
  assert.equal(h.renders.length, 0);
  h.chatState.sessionCache.set('synthetic-account:synthetic-friend', { messages: next, results: {} });
  h.context.returnToLatest();
  assert.equal(reading, ''); assert.equal(h.chatState.historyState, null);
  assert.equal(h.chatState.messages, next); assert.equal(h.renders[0].restore.followLatest, true);
});

test('new messages that keep the expanded target in the latest window do not freeze the live view', () => {
  const h = harness(messages(1, 80));
  h.context.window.MessageAnalysis = { openedMessageId: () => 'm40' };
  assert.equal(h.context.preserveAnalysisReading(messages(2, 80)), false);
  assert.equal(h.chatState.historyState, null);
});

test('reaching the upper edge consumes the prefetched page without another request', async () => {
  const h = harness();
  h.context.response = async () => page(messages(161, 80));
  h.context.prefetchHistory('older');
  await settle();
  h.container.scrollTop = 150;
  h.context.maybeLoadHistory('older');
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.chatState.messages.length, 160);
  assert.equal(h.chatState.messages[0].id, 'm161');
  assert.equal(h.chatState.historyState.hasMoreBefore, true);
  assert.equal(h.renders[0].anchor.id, 'm246');
});

test('scrolling toward a nearby edge prefetches without replacing the live message view', async () => {
  const h = harness();
  h.context.response = async () => page(messages(161, 80));
  h.container.scrollTop = 500;
  h.context.maybeLoadHistory('older');
  assert.equal(h.requests.length, 0);
  h.runPrefetch();
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.renders.length, 0);
});

test('repeated edge events share the in-flight prefetch and render its page only once', async () => {
  const h = harness(), pending = deferred();
  h.context.response = () => pending.promise;
  h.context.prefetchHistory('older');
  h.container.scrollTop = 0;
  h.context.maybeLoadHistory('older');
  h.context.maybeLoadHistory('older');
  h.context.maybeLoadHistory('newer');
  assert.equal(h.requests.length, 1);
  pending.resolve(page(messages(161, 80)));
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.renders.length, 1);
  assert.equal(h.chatState.messages.length, 160);
});

test('an exhausted conversation does not request additional history', async () => {
  const h = harness(messages(1, 3));
  h.chatState.currentHasMoreBefore = false;
  h.container.scrollTop = 0;
  h.context.maybeLoadHistory('older');
  h.context.prefetchHistory('older');
  h.context.maybeLoadHistory('newer');
  await settle();
  assert.equal(h.requests.length, 0);
  assert.equal(h.chatState.historyState, null);
});

test('a read failure waits for explicit retry instead of looping at the same scroll edge', async () => {
  const h = harness();
  h.container.scrollTop = 0;
  h.context.response = async () => { throw new Error('synthetic network failure'); };
  h.context.maybeLoadHistory('older');
  await settle();
  assert.ok(h.chatState.historyState.error);
  for (let index = 0; index < 10; index++) h.context.maybeLoadHistory('older');
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.renders.length, 0);
  h.context.response = async () => page(messages(161, 80));
  await h.context.retryHistory();
  await settle();
  assert.equal(h.requests.length, 2);
  assert.equal(h.chatState.historyState.error, '');
  assert.equal(h.chatState.messages[0].id, 'm161');
});

test('failed speculative prefetch is silent and is not retried by every nearby scroll event', async () => {
  const h = harness();
  h.container.scrollTop = 500;
  h.context.response = async () => { throw new Error('synthetic prefetch failure'); };
  h.context.prefetchHistory('older');
  await settle();
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.renders.length, 0);
  for (let index = 0; index < 3; index++) {
    h.context.maybeLoadHistory('older');
    h.runPrefetch();
    await settle();
  }
  assert.equal(h.requests.length, 1);
  h.container.scrollTop = 0;
  h.context.maybeLoadHistory('older');
  await settle();
  assert.equal(h.requests.length, 1);
  assert.ok(h.chatState.historyState.error, 'error becomes actionable only when its page is requested');
});

test('scrolling to the end of history rejoins the latest view and its normal message refresh', () => {
  const h = harness(messages(241, 80)), latest = messages(242, 80);
  h.chatState.historyState = { beforeCursor: 'c241', hasMoreBefore: true, hasMoreAfter: false, error: '' };
  h.chatState.sessionCache.set('synthetic-account:synthetic-friend', { messages: latest, results: {} });
  h.container.scrollTop = h.container.scrollHeight - h.container.clientHeight;
  h.context.maybeLoadHistory('newer');
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.chatState.followLatest, true);
  assert.equal(h.chatState.messages.at(-1).id, 'm321');
  assert.equal(h.latestReads.length, 1);
  assert.equal(h.requests.length, 0);
});

test('cancelled prefetch cannot populate a new account cache after its late response', async () => {
  const h = harness(), pending = deferred();
  h.context.response = () => pending.promise;
  h.context.prefetchHistory('older');
  h.context.cancelHistoryRequest();
  h.chatState.currentAccount = 'another-synthetic-account';
  h.chatState.generation++;
  pending.resolve(page(messages(161, 80)));
  await settle();
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.chatState.historyPrefetch, null);
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.renders.length, 0);
});

test('wheel-up on a short conversation fetches older history even when scroll cannot move', async () => {
  const h = harness(messages(241, 3));
  h.container.scrollTop = 0;
  h.context.response = async () => page(messages(161, 80));
  for (const listener of h.container.listeners.wheel) listener({ ctrlKey: false, deltaY: -20, currentTarget: h.container });
  h.runFrames();
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.chatState.messages.length, 83);
  assert.equal(h.renders[0].anchor.id, 'm241');
});

test('loading older messages retains a visible anchor and bounds the rendered window', async () => {
  const h = harness(messages(81, 240));
  h.container.scrollTop = 45;
  h.context.response = async () => page(messages(1, 80));
  await h.context.loadOlderHistory();
  assert.equal(h.chatState.messages.length, 240);
  assert.equal(h.chatState.messages[0].id, 'm1');
  assert.equal(h.chatState.messages.at(-1).id, 'm240');
  assert.equal(h.renders[0].anchor.id, 'm82');
  assert.equal(h.renders[0].anchor.top, -15);
  assert.equal(h.container.scrollTop, 2445);
  assert.equal(h.chatState.historyState.hasMoreAfter, true);
});

test('loading newer messages preserves the visible bottom anchor while releasing older rows', async () => {
  const h = harness(messages(1, 240));
  h.chatState.historyState = { beforeCursor: 'c1', hasMoreBefore: false, hasMoreAfter: true, error: '' };
  h.container.scrollTop = h.container.scrollHeight - h.container.clientHeight;
  h.context.response = async () => page(messages(241, 80), { nextCursor: 'c320' });
  await h.context.loadNewerHistory();
  assert.equal(h.chatState.messages.length, 240);
  assert.equal(h.chatState.messages[0].id, 'm81');
  assert.equal(h.chatState.messages.at(-1).id, 'm320');
  assert.equal(h.renders[0].anchor.id, 'm240');
  assert.equal(h.renders[0].anchor.top, 270);
  assert.equal(h.chatState.historyState.hasMoreBefore, true);
  assert.equal(new URL(h.requests[0].url, 'http://localhost').searchParams.get('after'), 'c240');
});

test('empty after-pages advance the cursor without declaring the conversation complete', async () => {
  const h = harness(messages(1, 240));
  h.chatState.historyState = { beforeCursor: 'c1', hasMoreBefore: false, hasMoreAfter: true, error: '' };
  h.container.scrollTop = h.container.scrollHeight - h.container.clientHeight;
  h.context.response = async url => {
    const after = new URL(url, 'http://localhost').searchParams.get('after');
    const number = Number(after.slice(1));
    return number < 244 ? page([], { nextCursor: `c${number + 1}` }) :
      page(messages(245, 3), { nextCursor: 'c247', hasMoreAfter: false });
  };
  await h.context.loadNewerHistory();
  assert.equal(h.requests.length, 4, 'one prefetch bounds the empty-row scan');
  assert.equal(h.chatState.historyState.afterCursor, 'c244');
  assert.equal(h.chatState.historyState.hasMoreAfter, true);
  assert.equal(h.renders.length, 0);
  await h.context.loadNewerHistory();
  assert.equal(h.requests.length, 5);
  assert.equal(h.chatState.messages.at(-1).id, 'm247');
  assert.equal(h.chatState.historyState.hasMoreAfter, false);
});

test('moving away from the edge while awaiting older history keeps the current reading window', async () => {
  const h = harness(messages(81, 240)), pending = deferred();
  h.container.scrollTop = 0;
  h.context.response = () => pending.promise;
  const loading = h.context.loadOlderHistory();
  h.container.scrollTop = 6000;
  pending.resolve(page(messages(1, 80)));
  await loading;
  assert.equal(h.renders.length, 0);
  assert.equal(h.chatState.messages[0].id, 'm81');
  assert.equal(h.chatState.messages.at(-1).id, 'm320');
  assert.equal(h.container.scrollTop, 6000);
  assert.ok(h.chatState.historyPrefetch?.data);
  h.container.scrollTop = 0;
  await h.context.loadOlderHistory();
  assert.equal(h.requests.length, 1, 'the deferred page remains available when scrolling back');
  assert.equal(h.renders.length, 1);
  assert.equal(h.chatState.messages[0].id, 'm1');
});

test('moving away from the bottom while awaiting newer history preserves the visible older rows', async () => {
  const h = harness(messages(1, 240)), pending = deferred();
  h.chatState.historyState = { beforeCursor: 'c1', hasMoreBefore: false, hasMoreAfter: true, error: '' };
  h.container.scrollTop = h.container.scrollHeight - h.container.clientHeight;
  h.context.response = () => pending.promise;
  const loading = h.context.loadNewerHistory();
  h.container.scrollTop = 50;
  pending.resolve(page(messages(241, 80), { nextCursor: 'c320' }));
  await loading;
  assert.equal(h.renders.length, 0);
  assert.equal(h.chatState.messages[0].id, 'm1');
  assert.equal(h.chatState.messages.at(-1).id, 'm240');
  assert.equal(h.container.scrollTop, 50);
  assert.ok(h.chatState.historyPrefetch?.data);
});

test('a pending history request cannot render into another conversation', async () => {
  const h = harness(), pending = deferred();
  h.container.scrollTop = 0;
  h.context.response = () => pending.promise;
  const loading = h.context.loadOlderHistory();
  h.context.cancelHistoryRequest();
  h.chatState.generation++;
  h.chatState.currentUser = 'another-synthetic-friend';
  h.chatState.messages = messages(900, 3);
  h.chatState.historyState = null;
  pending.resolve(page(messages(161, 80)));
  await loading;
  assert.equal(h.requests[0].signal.aborted, true);
  assert.deepEqual(h.chatState.messages.map(message => message.id), ['m900', 'm901', 'm902']);
  assert.equal(h.renders.length, 0);
  assert.equal(h.chatState.historyState, null);
});

test('returning to latest aborts old history and late responses cannot replace the latest window', async () => {
  const h = harness(), pending = deferred(), latest = messages(500, 80);
  h.container.scrollTop = 0;
  h.context.response = () => pending.promise;
  h.chatState.sessionCache.set('synthetic-account:synthetic-friend', { messages: latest, results: {} });
  const loading = h.context.loadOlderHistory();
  h.context.returnToLatest();
  pending.resolve(page(messages(161, 80)));
  await loading;
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.chatState.messages[0].id, 'm500');
  assert.equal(h.chatState.followLatest, true);
  assert.equal(h.latestReads.length, 1);
  assert.equal(h.renders.length, 1);
});

test('reaching the live bottom during an older read restores polling after that read settles', async () => {
  const h = harness(), pending = deferred(), latest = h.chatState.messages;
  h.chatState.sessionCache.set('synthetic-account:synthetic-friend', { messages: latest, results: {} });
  h.container.scrollTop = 0;
  h.context.response = () => pending.promise;
  const loading = h.context.loadOlderHistory();
  h.container.scrollTop = h.container.scrollHeight - h.container.clientHeight;
  pending.resolve(page(messages(161, 80)));
  await loading;
  assert.equal(h.chatState.historyState, null);
  assert.equal(h.chatState.followLatest, true);
  assert.equal(h.latestReads.length, 1);
  assert.equal(h.chatState.messages[0].id, 'm241');
});
