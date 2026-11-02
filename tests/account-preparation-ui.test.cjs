const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

const source = readFileSync(path.join(__dirname, '../chatui/app.js'), 'utf8');
const start = source.indexOf('async function api(');
const end = source.indexOf('\nfunction status(', start);

test('safe preparation diagnostics survive the HTTP helper without exposing raw fields', async () => {
  const context = vm.createContext({ fetch: async () => ({ ok: false, status: 503,
    json: async () => ({ error: 'AccountUnavailableError', preparation: {
      reason: 'scan_limit', message: 'Synthetic bounded scan failed', key: 'synthetic-secret', path: 'private' } }) }) });
  vm.runInContext(source.slice(start, end) + '\nglobalThis.call = api;', context);
  await assert.rejects(context.call('/api/sessions'), error => {
    assert.equal(error.code, 'AccountUnavailableError');
    assert.deepEqual(Object.keys(error.preparation).sort(), ['message', 'reason']);
    assert.equal(error.preparation.message, 'Synthetic bounded scan failed');
    assert.ok(!JSON.stringify(error).includes('synthetic-secret'));
    return true;
  });
});

test('malformed or oversized preparation messages are ignored', async () => {
  for (const preparation of [null, { reason: 'scan_limit', message: 'x'.repeat(241) }, { reason: 1, message: 'x' }]) {
    const context = vm.createContext({ fetch: async () => ({ ok: false, status: 503,
      json: async () => ({ error: 'AccountUnavailableError', preparation }) }) });
    vm.runInContext(source.slice(start, end) + '\nglobalThis.call = api;', context);
    await assert.rejects(context.call('/api/sessions'), error => error.preparation === undefined);
  }
});

test('only supported preparation states survive the HTTP helper', async () => {
  const allowed = ['preparing', 'sessions', 'failed', 'ready'];
  for (const state of [...allowed, 'private-path', '', null, 1, { value: 'preparing' }]) {
    const context = vm.createContext({ fetch: async () => ({ ok: false, status: 503,
      json: async () => ({ error: 'AccountUnavailableError', preparation: {
        state, reason: 'keys_incomplete', message: '合成读取状态', key: 'synthetic-secret' } }) }) });
    vm.runInContext(source.slice(start, end) + '\nglobalThis.call = api;', context);
    await assert.rejects(context.call('/api/sessions'), error => {
      assert.equal(error.preparation.state, allowed.includes(state) ? state : undefined);
      assert.equal(error.preparation.reason, 'keys_incomplete');
      assert.equal(error.preparation.message, '合成读取状态');
      assert.deepEqual(Object.keys(error.preparation).sort(),
        allowed.includes(state) ? ['message', 'reason', 'state'] : ['message', 'reason']);
      return true;
    });
  }
});

function unavailable(reason, message, state) {
  return Object.assign(new Error('synthetic account unavailable'), { status: 503, code: 'AccountUnavailableError', preparation: { reason, message, state } });
}

function node(tag = 'div') {
  const result = {
    tag, className: '', children: [], events: {}, attributes: {}, dataset: {}, ownText: '', hidden: true,
    set textContent(value) { this.ownText = String(value); this.children = []; },
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); },
    get message() { return (this.querySelector('.account-login-message') || this.querySelector('.ui-state'))?.textContent; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.append(child); return child; },
    replaceChildren(...children) { this.ownText = ''; this.children = children; },
    addEventListener(name, callback) { this.events[name] = callback; },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      const matches = [];
      for (const child of this.children) {
        if (selector === '[data-step]' ? child.dataset.step !== undefined : child.classList.contains(selector.slice(1))) matches.push(child);
        matches.push(...child.querySelectorAll(selector));
      }
      return matches;
    },
  };
  result.classList = {
    contains: name => result.className.split(/\s+/).includes(name),
    toggle(name, enabled) {
      const classes = new Set(result.className.split(/\s+/).filter(Boolean));
      if (enabled) classes.add(name); else classes.delete(name);
      result.className = [...classes].join(' ');
    },
  };
  return result;
}

function startupFailureHarness(failure, alreadyUnavailable = false) {
  const nodes = new Map(), timers = new Map(), startup = [], requests = [];
  let timerId = 0;
  const context = vm.createContext({
    chatState: { sessionLoading: false, sessionRefreshQueued: false, sessionRequest: 0, currentAccount: null, currentUser: null, sessions: new Map(), selectedConversations: new Set() },
    accountClearedExiting: false, accountUnavailable: alreadyUnavailable, startupActive: true, startupAccountRetryUsed: false,
    startupAttempt: 0, startupAccountRetryTimer: null,
    byId: id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    document: { createElement: node }, window: {}, updateFriendButton() {},
    showStartup: (stage, message, options = {}) => startup.push({ stage, message, options }),
    api: async url => { requests.push(url); throw failure; },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: id => timers.delete(id),
  });
  context.resetAccountView = message => {
    context.chatState.sessionRequest++;
    context.chatState.currentAccount = null; context.chatState.sessions.clear();
    context.accountUnavailable = false;
    context.status(context.byId('chatMessages'), message);
  };
  const loadStart = source.indexOf('async function loadSessions('), loadEnd = source.indexOf('function messageNode(', loadStart);
  const errorStart = source.indexOf('function accountUnavailableError('), errorEnd = source.indexOf('function acceptResponseAccount(', errorStart);
  const retryStart = source.indexOf('function retryStartup('), retryEnd = source.indexOf('\n}', retryStart) + 2;
  const elementStart = source.indexOf('const element = '), elementEnd = source.indexOf('const text = ', elementStart);
  const statusEnd = source.indexOf('const isNetworkFailure', end);
  vm.runInContext(source.slice(elementStart, elementEnd) + source.slice(end, statusEnd) +
    source.slice(errorStart, errorEnd) + source.slice(loadStart, loadEnd) + source.slice(retryStart, retryEnd), context);
  return { context, nodes, timers, startup, requests };
}

function useRealStartup(h) {
  Object.assign(h.context, { startupStage: 'account', startupStageEpoch: 0, startupWatchdog: null,
    text: (id, value) => { h.context.byId(id).textContent = value; } });
  for (const id of ['databaseLoading', 'startupOverlay']) {
    const panel = h.context.byId(id);
    panel.append(...['account', 'sessions', 'messages'].map(stage => {
      const item = node(); item.dataset.step = stage; item.className = 'startup-step'; return item;
    }));
  }
  h.context.byId('appWindow').setAttribute('inert', '');
  const startupStart = source.indexOf('function renderDatabaseLoading(');
  const startupEnd = source.indexOf('function sessionCacheKey(', startupStart);
  assert.ok(startupStart >= 0 && startupEnd > startupStart);
  vm.runInContext(source.slice(startupStart, startupEnd), h.context);
}

function prepareSuccessfulRecovery(h) {
  Object.assign(h.context, {
    localStorage: { getItem: () => null }, loadConversationSelection: async () => {}, announceFriendAccount() {},
    setAvatar() {}, renderSessions() {}, renderConversationManager() {},
    clearUnselectedConversation: () => h.context.status(h.context.byId('chatMessages'), '选择聊天开始'),
  });
  h.context.api = async url => { h.requests.push(url); return { account: 'synthetic-account', sessions: [], messagesReady: true }; };
}

async function runTimer(h, delay) {
  const entry = [...h.timers.entries()].find(([, timer]) => timer.delay === delay);
  assert.ok(entry, `expected an active ${delay} ms timer`);
  h.timers.delete(entry[0]);
  entry[1].callback();
  await new Promise(setImmediate);
}

test('missing WeChat or unconfirmed account shows actionable text without the fast connecting retry', async () => {
  for (const reason of ['wechat_not_running', 'active_account_unavailable']) {
    const message = reason === 'wechat_not_running' ? '请打开并登录电脑版微信后重试。' : '请确认电脑版微信已登录后重试。';
    const h = startupFailureHarness(unavailable(reason, message));
    await h.context.loadSessions();
    assert.equal(h.nodes.get('chatMessages').message, message);
    const title = reason === 'wechat_not_running' ? '请先打开并登录微信' : '请确认电脑版微信已登录';
    const card = h.nodes.get('chatMessages').querySelector('.account-login-card');
    assert.equal(card.querySelector('.account-login-title').textContent, title);
    assert.equal(card.querySelector('.account-login-description').attributes.role, 'status');
    assert.equal(card.querySelector('.account-login-hint').textContent, '登录后会自动连接');
    assert.equal(h.nodes.get('sessionList').message, title);
    assert.equal(h.startup.at(-1).message, message);
    assert.equal(h.startup.at(-1).options.retry, true);
    assert.equal(h.startup.at(-1).options.continueEmpty, true);
    assert.equal([...h.timers.values()].some(timer => timer.delay === 2500), false);
    const button = card.querySelector('.account-login-retry');
    const retry = button.events.click();
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, '正在检测…');
    await retry;
    assert.deepEqual(h.requests, ['/api/sessions', '/api/sessions']);
    assert.equal(h.startup.at(-1).message, message);
  }
});

test('an already empty unavailable account updates the main chat and sidebar to the latest reason', async () => {
  const h = startupFailureHarness(unavailable('wechat_not_running', '请先打开电脑版微信。'), true);
  h.context.status(h.context.byId('chatMessages'), '当前微信账号未就绪');
  h.context.resetAccountView = () => { throw new Error('an already empty account need not reset'); };
  await h.context.loadSessions();
  assert.equal(h.nodes.get('chatMessages').message, '请先打开电脑版微信。');
  assert.equal(h.nodes.get('sessionList').message, '请先打开并登录微信');
});

test('missing diagnostics and scan failures remain neutral rather than claiming login is missing', async () => {
  const missing = unavailable(); delete missing.preparation;
  for (const error of [missing, unavailable('preparing', '正在准备合成账号。'),
    unavailable('keys_incomplete', '合成密钥校验未完成。'), unavailable('scan_limit', '合成扫描达到上限。'),
    unavailable('process_read_denied', '合成读取权限不足。')]) {
    const h = startupFailureHarness(error);
    await h.context.loadSessions();
    assert.equal(h.nodes.get('chatMessages').querySelector('.account-login-card'), null);
    assert.equal(h.nodes.get('chatMessages').message, error.preparation?.message || '当前微信账号未就绪');
    const fastRetry = [...h.timers.values()].some(timer => timer.delay === 2500);
    assert.equal(fastRetry, !error.preparation || error.preparation.reason === 'preparing');
  }
});

test('active preparation keeps retrying and the real loading panel closes only after readiness', async () => {
  const h = startupFailureHarness(unavailable('keys_incomplete', '正在准备合成账号。', 'preparing'));
  useRealStartup(h);
  await h.context.loadSessions();
  for (let attempt = 1; attempt <= 3; attempt++) {
    assert.equal(h.requests.length, attempt);
    assert.equal(h.nodes.get('databaseLoading').hidden, false);
    assert.equal(h.nodes.get('databaseLoading').dataset.state, 'loading');
    assert.equal(h.nodes.get('databaseLoadingStatus').textContent, '正在准备合成账号。');
    assert.equal(h.nodes.get('databaseLoadingProgress').hidden, false);
    assert.equal(h.nodes.get('databaseLoadingRetry').hidden, true);
    assert.equal(h.nodes.get('sessionList').querySelector('.ui-retry'), null);
    assert.equal(h.nodes.get('databaseLoading').querySelectorAll('[data-step]')[0].classList.contains('active'), true);
    assert.equal(h.context.startupActive, true);
    if (attempt === 3) prepareSuccessfulRecovery(h);
    await runTimer(h, 2500);
  }
  assert.equal(h.requests.length, 4);
  assert.equal(h.context.chatState.messageSourceReady, true);
  assert.equal(h.nodes.get('databaseLoading').hidden, true);
  assert.equal(h.nodes.get('startupOverlay').hidden, true);
  assert.equal(h.nodes.get('appWindow').attributes.inert, undefined);
  assert.equal(h.context.startupActive, false);
  assert.equal(h.timers.size, 0);
});

test('a failed preparation shows retry and a manual retry can finish the real loading panel', async () => {
  const h = startupFailureHarness(unavailable('scan_limit', '合成扫描达到上限。', 'failed'));
  useRealStartup(h);
  await h.context.loadSessions();
  assert.equal(h.nodes.get('databaseLoading').hidden, false);
  assert.equal(h.nodes.get('databaseLoading').dataset.state, 'attention');
  assert.equal(h.nodes.get('databaseLoadingProgress').hidden, true);
  assert.equal(h.nodes.get('databaseLoadingRetry').hidden, false);
  assert.equal(h.nodes.get('databaseLoadingStatus').textContent, '合成扫描达到上限。');
  assert.equal([...h.timers.values()].some(timer => timer.delay === 2500), false);
  prepareSuccessfulRecovery(h);
  h.context.retryStartup();
  assert.equal(h.nodes.get('databaseLoading').dataset.state, 'loading');
  assert.equal(h.nodes.get('databaseLoadingRetry').hidden, true);
  await new Promise(setImmediate);
  assert.equal(h.requests.length, 2);
  assert.equal(h.context.chatState.messageSourceReady, true);
  assert.equal(h.nodes.get('databaseLoading').hidden, true);
});

test('account loss during normal operation keeps the server reason and offers retry immediately', () => {
  const error = unavailable('active_account_unavailable', '当前登录账号暂时无法确认，请重试。');
  const h = startupFailureHarness(error);
  assert.equal(h.context.handleAccountBoundaryError(error), true);
  assert.equal(h.nodes.get('chatMessages').message, error.preparation.message);
  assert.equal(h.nodes.get('chatMessages').querySelector('.account-login-title').textContent, '请确认电脑版微信已登录');
  assert.equal(h.nodes.get('sessionList').querySelector('.ui-retry').textContent, '重试');
  assert.equal(h.startup.at(-1).message, error.preparation.message);
  assert.equal(h.startup.at(-1).options.retry, true);
  assert.equal(h.context.accountUnavailable, true);
});

test('login guidance is text-only and is cleared by successful account recovery', async () => {
  const message = '合成说明 <img src=x onerror=alert(1)>';
  const h = startupFailureHarness(unavailable('wechat_not_running', message));
  await h.context.loadSessions();
  const chat = h.nodes.get('chatMessages'), card = chat.querySelector('.account-login-card');
  assert.equal(card.querySelector('.account-login-message').textContent, message);
  assert.equal(card.querySelector('.account-login-message').children.length, 0);
  const clearStart = source.indexOf('function clearUnselectedConversation('), clearEnd = source.indexOf('const conversationFollowKey', clearStart);
  Object.assign(h.context, {
    localStorage: { getItem: () => null }, loadConversationSelection: async () => {}, announceFriendAccount() {},
    setAvatar() {}, renderSessions() {}, updateHistoryNavigation() {}, renderMood() {}, switchView() {},
    text: (id, value) => { h.context.byId(id).textContent = value; },
    showChatEmptyState: () => h.context.status(chat, '选择聊天开始'),
    completeStartup: () => { h.context.startupActive = false; },
  });
  vm.runInContext(source.slice(clearStart, clearEnd), h.context);
  h.context.api = async url => { h.requests.push(url); return { account: 'synthetic-account', sessions: [], messagesReady: true }; };
  await card.querySelector('.account-login-retry').events.click();
  assert.equal(h.context.accountUnavailable, false);
  assert.equal(h.context.chatState.currentAccount, 'synthetic-account');
  assert.equal(chat.querySelector('.account-login-card'), null);
  assert.equal(chat.message, '选择聊天开始');
  assert.equal(h.context.startupActive, false);
});

test('the visible-page 15 second account check still runs after a missing WeChat error', async () => {
  const h = startupFailureHarness(unavailable('wechat_not_running', '请打开微信。'));
  await h.context.loadSessions();
  const intervals = [];
  h.context.updateValidationMode = false; h.context.updateCommitReady = true;
  h.context.document.hidden = false;
  h.context.setInterval = (callback, delay) => intervals.push({ callback, delay });
  const pollingStart = source.search(/if \(!updateValidationMode && !window\.desktopHost\?\.isSettingsWindow\) \{\r?\n  setInterval\(/);
  const pollingEnd = source.indexOf('window.MediaSettings', pollingStart);
  assert.ok(pollingStart >= 0 && pollingEnd > pollingStart);
  vm.runInContext(source.slice(pollingStart, pollingEnd), h.context);
  intervals.find(timer => timer.delay === 15000).callback(); await new Promise(setImmediate);
  assert.equal(h.requests.length, 2);
  intervals.length = 0;
  h.context.window.desktopHost = { isSettingsWindow: true };
  vm.runInContext(source.slice(pollingStart, pollingEnd), h.context);
  assert.equal(intervals.length, 0, "a settings window must not start another chat poller");
});
