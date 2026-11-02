// Developer-mode settings on the main page, and the captured request, response and errors that a
// call record shows through the trace viewer in the 模型调用 window.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(require('node:path').join(__dirname, '../chatui/model-traces.js'), 'utf8');
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
function find(element, predicate) {
  if (predicate(element)) return element;
  for (const child of element.children || []) { const match = find(child, predicate); if (match) return match; }
}
const findButton = (element, label) => find(element, item => item.tagName === 'BUTTON' && item.textContent === label);
function fixture({ standalone = true, settings = true, initialize = true } = {}) {
  const ids = new Map(), documentHandlers = new Map(), calls = [], timers = new Map(); let timer = 0;
  function node(tag = 'div', id) {
    const value = { tagName: tag.toUpperCase(), children: [], listeners: {}, attributes: {}, hidden: false, open: false, value: '', textContent: '', className: '', isConnected: true, scrollTop: 0 };
    Object.defineProperty(value, 'id', { get: () => value._id, set(name) { value._id = name; ids.set(name, value); } }); if (id) value.id = id;
    value.classList = { contains(name) { return value.className.split(/\s+/).includes(name); }, add(name) { value.className += ' ' + name; }, remove(name) { value.className = value.className.replace(name, ''); }, toggle(name, force) { const enabled = force === undefined ? !this.contains(name) : force; if (enabled && !this.contains(name)) this.add(name); if (!enabled) this.remove(name); } };
    value.append = (...children) => value.children.push(...children); value.appendChild = child => { value.children.push(child); return child; };
    value.replaceChildren = (...children) => { value.children = children; value.textContent = ''; };
    value.setAttribute = (key, data) => value.attributes[key] = data;
    value.getAttribute = key => value.attributes[key] ?? null;
    value.addEventListener = (key, handler) => (value.listeners[key] ||= []).push(handler);
    value.click = async () => { for (const handler of value.listeners.click || []) await handler({ currentTarget: value, target: value }); };
    value.focus = () => { document.activeElement = value; }; value.getClientRects = () => value.hidden ? [] : [{}]; return value;
  }
  const document = { body: node('body'), createElement: node, createTextNode: text => ({ textContent: text }), getElementById: id => ids.get(id),
    addEventListener: (key, handler) => (documentHandlers.get(key) || (documentHandlers.set(key, []), documentHandlers.get(key))).push(handler) };
  if (settings) {
    for (const id of ['btnDeveloperMode','modelTraceRetention','modelTraceStorage','btnRetryModelTraces','btnModelTraces','btnViewModelTraces','btnClearModelTraces','modelTraceClearConfirm','btnCancelClearModelTraces','btnConfirmClearModelTraces','settingsModal','btnSettings']) node('button', id);
    node('p', 'modelTraceError'); node('details', 'modelTraceHistory'); node('span', 'modelTraceHistoryCount'); node('ol', 'modelTraceHistoryList');
    ids.get('modelTraceClearConfirm').hidden = true;
  }
  for (const id of ['modelTracesStartup', 'modelTracesStartupStatus', 'modelTracesStartupRetry']) node('div', id);
  for (const id of ['modelCallsNavigation', 'modelCallsContent']) node('div', id);
  for (const id of ['modelCallsStatsTab', 'modelCallsRecordsTab']) node('button', id);
  const setting = { enabled: true, collecting: true, status: 'on', retentionDays: 7, recordCount: 1, storageBytes: 1024 };
  const row = { recordId: 'record-a', taskId: 'task-a', source: 'plugin:kyf.advisor', model: 'synthetic', startedAt: Date.now()/1000, collectionStatus: 'complete', transportStatus: 'completed', applicationStatus: 'completed', adoption: 'adopted', complete: true };
  const controls = { account: 'account-a', setting, row, body: '<script>BAD()</script>', responder: null, deleteVersion: 0 };
  const window = { document, addEventListener() {}, MutationObserver: class { constructor(callback) { controls.mutateTheme = callback; } observe() {} },
    modelTracesWindow: { close: async () => { calls.push(['close-window']); return true; } },
    desktopHost: { onModelTracesInvalidated: listener => { controls.invalidate = listener; },
      openModelTracesWindow: async context => { calls.push(['open-window', context]); return true; },
      syncModelTracesWindow: context => calls.push(['sync-window', context]),
      copyDraft: async text => { calls.push(['copy', text]); return true; }, modelTracesRequest: async (operation, args = {}) => {
    calls.push([operation, args]);
    if (controls.responder) { const result = controls.responder(operation, args); if (result) return result; }
    if (operation === 'scope') return { ok: true, accountId: controls.account, scope: args.scope, deleteVersion: controls.deleteVersion };
    if (operation === 'state' || operation === 'configure') return { ok: true, ...setting, deleteVersion: controls.deleteVersion };
    if (operation === 'detail') return { ok: true, record: { ...row, recordId: args.id }, deleteVersion: controls.deleteVersion };
    if (operation === 'body') return { ok: true, text: controls.body, offset: 0, nextOffset: null, totalBytes: controls.body.length, complete: true, deleteVersion: controls.deleteVersion };
    return { ok: true, deleteVersion: controls.deleteVersion };
  } } };
  const context = { window, document, URLSearchParams, Date, setTimeout: handler => { timers.set(++timer, handler); return timer; }, clearTimeout: id => timers.delete(id) };
  vm.runInNewContext(source, context);
  if (initialize) window.ModelTraces.init({ standalone, account: controls.account, user: 'contact-a', view: 'chat' });
  function text(value) { return String(value.textContent || '') + (value.children || []).map(text).join(''); }
  // A call record's detail hands the viewer a fresh container on every render.
  async function show(tab, id = 'record-a', options) {
    const container = node('div'); window.ModelTraces.renderTrace(container, id, tab, options); await settle(); return container;
  }
  const bodies = () => calls.filter(([op]) => op === 'body').map(([, args]) => args);
  return { window, ids, controls, calls, timers, documentHandlers, text, document, context, show, bodies, node };
}

test('a captured request is read only when its tab is shown and stays literal text', async () => {
  const f = fixture(); await settle();
  assert.equal(f.calls.some(([op]) => ['detail', 'body', 'configure'].includes(op)), false);
  const container = await f.show('request');
  assert.deepEqual(f.calls.filter(([op]) => op === 'detail' || op === 'body').map(([op, args]) => [op, args.kind]), [['detail', undefined], ['body', 'request']]);
  assert.match(f.text(container), /<script>BAD\(\)<\/script>/);
  assert.equal(container.innerHTML, undefined);
});

test('switching accounts immediately drops shown bodies and discards their late result', async () => {
  const f = fixture(); await settle();
  const pending = deferred(); f.controls.responder = op => op === 'body' ? pending.promise : null;
  const container = await f.show('request');
  f.controls.account = 'account-b'; f.window.ModelTraces.setScope('account-b', 'contact-b', 'chat');
  pending.resolve({ ok: true, text: 'OLD_ACCOUNT_SECRET', complete: true, deleteVersion: 0 }); await settle();
  assert.doesNotMatch(f.text(container), /OLD_ACCOUNT_SECRET/);
  assert.equal(f.calls.filter(([op, args]) => op === 'scope' && args.scope === 'account').length >= 2, true);
});

test('deletion generation fences a late body and reloads the shown record', async () => {
  const f = fixture(); await settle();
  const pending = deferred(); f.controls.responder = op => op === 'body' && !f.controls.deleteVersion ? pending.promise : null;
  const container = await f.show('request');
  f.controls.deleteVersion = 1; f.controls.body = 'CURRENT_BODY'; await f.window.ModelTraces.refreshSettings(); await settle();
  pending.resolve({ ok: true, text: 'DELETED_BODY', complete: true, deleteVersion: 0 }); await settle();
  assert.doesNotMatch(f.text(container), /DELETED_BODY/);
  assert.match(f.text(container), /CURRENT_BODY/);
});

test('another trusted window immediately removes visible bodies and fences outstanding reads', async () => {
  const f = fixture(); await settle();
  const container = await f.show('response');
  assert.match(f.text(container), /BAD/);
  const pending = deferred(); f.controls.responder = op => op === 'body' ? pending.promise : null;
  await findButton(container, '原始返回').click(); await settle();
  f.controls.invalidate({ deleteVersion: 1 });
  assert.doesNotMatch(f.text(container), /BAD/);
  pending.resolve({ ok: true, text: 'LATE_DELETED_RESPONSE', complete: true, deleteVersion: 0 }); await settle();
  assert.doesNotMatch(f.text(container), /LATE_DELETED_RESPONSE/);
});

test('the response tab shows the model text first and switches to the raw return and application result', async () => {
  const f = fixture(); await settle();
  const container = await f.show('response');
  assert.equal(f.bodies().at(-1).kind, 'text');
  assert.equal(findButton(container, '模型正文').attributes['aria-pressed'], 'true');
  assert.match(f.text(container), /模型正文（已采集的累积文本）/);
  await findButton(container, '原始返回').click(); await settle();
  assert.equal(f.bodies().at(-1).kind, 'response');
  assert.equal(findButton(container, '原始返回').attributes['aria-pressed'], 'true');
  await findButton(container, '应用结果').click(); await settle();
  assert.equal(f.bodies().at(-1).kind, 'result');
  // A later render of the same record keeps the chosen body.
  const again = await f.show('response');
  assert.equal(findButton(again, '应用结果').attributes['aria-pressed'], 'true');
  assert.equal(f.bodies().length, 3);
});

test('the process tab shows errors, a failed attempt\'s returned content and folded metadata', async () => {
  const f = fixture(); await settle();
  Object.assign(f.controls.row, { transportStatus: 'failed', error: { code: 'http-error', message: '服务返回 429' }, httpStatus: 429, endpoint: 'https://example.invalid/v1/chat' });
  f.controls.body = '{"error":"RATE_LIMITED"}';
  const failed = await f.show('events', 'record-a', { failed: true });
  assert.match(f.text(failed), /错误与说明/); assert.match(f.text(failed), /服务返回 429/);
  assert.match(f.text(failed), /服务返回内容（已脱敏）/); assert.match(f.text(failed), /RATE_LIMITED/);
  const metadata = find(failed, item => item.tagName === 'DETAILS');
  assert.match(f.text(metadata), /^诊断元数据/); assert.match(f.text(metadata), /example\.invalid/);
  assert.doesNotMatch(f.text(metadata), /未采集/, 'empty metadata fields are left out');
  assert.equal(f.bodies().at(-1).kind, 'response');
  const before = f.bodies().length;
  await f.show('events', 'record-b');
  assert.equal(f.bodies().length, before, 'a successful attempt needs no body for its process tab');
});

test('search keeps its input while a live attempt refreshes without blanking content', async () => {
  const f = fixture(); await settle();
  f.controls.body = 'first PART of the reply';
  const container = await f.show('response');
  const input = find(container, item => item.tagName === 'INPUT');
  input.value = 'part'; for (const handler of input.listeners.input) handler(); await settle();
  assert.equal(find(container, item => item.tagName === 'MARK').textContent, 'PART');
  const pending = deferred(); f.controls.responder = op => op === 'body' ? pending.promise : null;
  f.window.ModelTraces.refreshTrace('record-a'); await settle();
  assert.match(f.text(container), /first PART/, 'the previous content stays until the refresh arrives');
  pending.resolve({ ok: true, text: 'first PART of the reply, continued', offset: 0, nextOffset: null, complete: false, deleteVersion: 0 }); await settle();
  assert.match(f.text(container), /continued/);
  assert.equal(find(container, item => item.tagName === 'INPUT'), input);
  assert.equal(find(container, item => item.tagName === 'MARK').textContent, 'PART');
});

test('collapsed records drop their viewer and fence its late reads', async () => {
  const f = fixture(); await settle();
  const pending = deferred(); f.controls.responder = op => op === 'body' ? pending.promise : null;
  const container = await f.show('request');
  f.window.ModelTraces.retainTraces([]);
  pending.resolve({ ok: true, text: 'COLLAPSED_BODY', complete: true, deleteVersion: 0 }); await settle();
  assert.doesNotMatch(f.text(container), /COLLAPSED_BODY/);
  f.controls.responder = null;
  const reopened = await f.show('request');
  assert.match(f.text(reopened), /BAD/);
  assert.equal(f.calls.filter(([op]) => op === 'detail').length, 2);
});

test('an expired account scope is reported once instead of retrying in a loop', async () => {
  const f = fixture(); await settle();
  f.controls.responder = op => op === 'detail' ? { ok: false, statusCode: 409 } : null;
  const container = await f.show('request');
  assert.match(f.text(container), /账号范围已变化/);
  await f.show('request'); await settle();
  assert.equal(f.calls.filter(([op]) => op === 'detail').length, 1);
});

test('status display separates parse failure, discarded result, capture stop and unknown usage', () => {
  const f = fixture(), status = f.window.ModelTraces.statusOf;
  assert.equal(status({ transportStatus: 'completed', applicationStatus: 'parse-failed' }).key, 'error');
  assert.equal(status({ transportStatus: 'completed', adoption: 'discarded' }).key, 'not_adopted');
  assert.equal(status({ transportStatus: 'pending', collectionStatus: 'stopped' }).key, 'unknown');
  assert.equal(status({ transportStatus: 'completed', applicationStatus: 'pending' }).key, 'running');
  assert.equal(status({ transportStatus: 'completed' }).key, 'unknown');
  assert.equal(status({ transportStatus: 'completed', applicationStatus: 'cancelled' }).key, 'cancelled');
  assert.equal(status({ kind: 'local', transportStatus: 'failed' }).label, '推理失败');
  assert.equal(status({ kind: 'local', transportStatus: 'completed', applicationStatus: 'pending' }).label, '推理成功 · 处理中');
});

test('failed preference save finishes the pending UI and can be retried after a state refresh', async () => {
  const f = fixture(); await settle();
  f.controls.setting.enabled = false; f.controls.setting.collecting = false; await f.window.ModelTraces.refreshSettings();
  f.controls.responder = op => op === 'configure' ? { ok: false, statusCode: 503 } : null;
  await f.ids.get('btnDeveloperMode').click(); await settle();
  assert.equal(f.ids.get('btnDeveloperMode').disabled, false);
  assert.doesNotMatch(f.ids.get('btnDeveloperMode').textContent, /正在/);
  assert.match(f.ids.get('modelTraceError').textContent, /设置保存失败/);
  assert.equal(f.ids.get('modelTraceError').hidden, false);
  assert.doesNotMatch(f.ids.get('modelTraceStorage').textContent, /设置保存失败/);
  await f.window.ModelTraces.refreshSettings();
  assert.match(f.ids.get('modelTraceError').textContent, /设置保存失败/);
  assert.equal(f.ids.get('btnRetryModelTraces').hidden, false);
  f.controls.responder = null; f.controls.setting.enabled = true; f.controls.setting.collecting = true;
  await f.ids.get('btnRetryModelTraces').click(); await settle();
  assert.equal(f.ids.get('btnDeveloperMode').textContent, '已开启');
  assert.equal(f.ids.get('btnRetryModelTraces').hidden, true);
  assert.equal(f.ids.get('modelTraceError').hidden, true);
});

test('capture interruptions stay folded apart from the current summary and retain every history entry newest first', async () => {
  const f = fixture(); await settle();
  const startedAt = new Date(2026, 9, 18, 10, 0, 0).getTime();
  const history = Array.from({ length: 30 }, (_, index) => ({
    startedAt: (startedAt + index * 60000) / 1000, finishedAt: (startedAt + index * 60000 + 30000) / 1000,
    reason: `历史中断 ${index}`,
  }));
  history[29].reason = '<script>HISTORY_TEXT()</script>';
  f.controls.setting.pauseHistory = history;
  f.controls.setting.recordCount = 3218;
  await f.window.ModelTraces.refreshSettings();
  assert.match(f.ids.get('modelTraceStorage').textContent, /正在记录 · 已保存 3,218 条/);
  assert.doesNotMatch(f.ids.get('modelTraceStorage').textContent, /历史中断|HISTORY_TEXT/);
  assert.equal(f.ids.get('modelTraceHistory').hidden, false);
  assert.equal(f.ids.get('modelTraceHistory').open, false);
  assert.equal(f.ids.get('modelTraceHistoryCount').textContent, '30 次');
  const rows = f.ids.get('modelTraceHistoryList').children;
  assert.equal(rows.length, 30);
  assert.equal(rows[0].children[0].textContent, '<script>HISTORY_TEXT()</script>');
  assert.equal(rows[0].innerHTML, undefined);
  assert.match(rows[0].children[1].textContent, /^2026\/10\/18 10:29:00 — 10:29:30$/);
  assert.match(f.text(rows.at(-1)), /历史中断 0/);
  assert.equal(history[0].reason, '历史中断 0', 'rendering must not reorder stored history');

  const details = f.ids.get('modelTraceHistory'), list = f.ids.get('modelTraceHistoryList');
  details.open = true; list.scrollTop = 120;
  await f.window.ModelTraces.refreshSettings();
  assert.equal(list.children[0], rows[0], 'unchanged history keeps its existing rows during polling');
  history.push({ startedAt: startedAt + 30 * 60000, reason: '恢复时间未知' });
  await f.window.ModelTraces.refreshSettings();
  assert.equal(details.open, true);
  assert.equal(list.scrollTop, 120);
  assert.equal(list.children.length, 31);
  assert.match(f.text(list.children[0]), /恢复时间未记录/);
  assert.doesNotMatch(f.text(list.children[0]), /尚未恢复/, 'an old incomplete interval must not imply collection is still paused');
});

test('current capture failures remain visible while empty history is hidden', async () => {
  const f = fixture(); await settle();
  assert.equal(f.ids.get('modelTraceHistory').hidden, true);
  assert.equal(f.ids.get('modelTraceError').hidden, true);
  Object.assign(f.controls.setting, { status: 'paused', collecting: false, pauseReason: '记录存储不可用', saveError: '设置未能保存' });
  await f.window.ModelTraces.refreshSettings();
  assert.match(f.ids.get('modelTraceStorage').textContent, /采集已暂停：记录存储不可用/);
  assert.equal(f.ids.get('modelTraceError').textContent, '设置未能保存');
  assert.equal(f.ids.get('modelTraceError').hidden, false);
  assert.equal(f.ids.get('btnRetryModelTraces').hidden, false);
  assert.equal(f.ids.get('modelTraceHistory').hidden, true);
});

test('body pagination returns to the exact previous UTF-8 boundary', async () => {
  const f = fixture(); await settle();
  f.controls.responder = (op, args) => op === 'body' ? { ok: true, text: '中', offset: args.offset,
    nextOffset: args.offset < 131070 ? args.offset + 65535 : null, totalBytes: 131073, complete: true, deleteVersion: 0 } : null;
  const container = await f.show('response');
  await findButton(container, '下一块').click(); await settle();
  await findButton(container, '下一块').click(); await settle();
  await findButton(container, '上一块').click(); await settle();
  assert.equal(f.bodies().at(-1).offset, 65535);
  await findButton(container, '上一块').click(); await settle();
  assert.equal(f.bodies().at(-1).offset, 0);
});

test('late reads cannot replace a newer body after switching away and back to its tab', async () => {
  const f = fixture(); await settle();
  const pending = deferred(); let first = true;
  f.controls.responder = (op, args) => {
    if (op === 'body' && args.kind === 'request' && first) { first = false; return pending.promise; }
    return null;
  };
  await f.show('request'); await f.show('response');
  f.controls.body = 'CURRENT_BODY'; const container = await f.show('request');
  pending.resolve({ ok: true, text: 'STALE_BODY', offset: 0, nextOffset: null, complete: true, deleteVersion: 0 }); await settle();
  assert.match(f.text(container), /CURRENT_BODY/);
  assert.doesNotMatch(f.text(container), /STALE_BODY/);
});

test('Gemini and Responses requests show actual protocol messages and native parameter fields', async () => {
  const f = fixture(); await settle();
  const content = container => find(container, item => item.className === 'model-traces-detail');
  f.controls.body = JSON.stringify({ body: { systemInstruction: { parts: [{ text: 'SYSTEM_INSTRUCTION' }] },
    contents: [{ role: 'user', parts: [{ text: 'GEMINI_INPUT' }] }], generationConfig: { maxOutputTokens: 42, temperature: 0 } } });
  let detail = content(await f.show('request'));
  assert.match(f.text(detail.children.find(child => f.text(child).startsWith('systemInstruction'))), /SYSTEM_INSTRUCTION/);
  assert.match(f.text(detail.children.find(child => f.text(child).startsWith('1. user'))), /GEMINI_INPUT/);
  const parameters = detail.children.find(child => f.text(child).startsWith('实际发送参数'));
  assert.match(f.text(parameters), /maxOutputTokens/); assert.match(f.text(parameters), /通过请求路径指定/);
  f.controls.body = JSON.stringify({ body: { instructions: 'RESPONSES_INSTRUCTIONS', input: [
    { role: 'assistant', content: '', tool_calls: [{ id: 'call-a', function: { name: 'read', arguments: '{}' } }] },
    { type: 'function_call_output', call_id: 'call-a', output: 'TOOL_OUTPUT' }], text: { format: { type: 'json_object' } } } });
  detail = content(await f.show('request', 'record-b'));
  assert.match(f.text(detail.children.find(child => f.text(child).startsWith('instructions'))), /RESPONSES_INSTRUCTIONS/);
  assert.match(f.text(detail.children.find(child => f.text(child).startsWith('1. assistant'))), /call-a/);
  assert.match(f.text(detail.children.find(child => f.text(child).startsWith('2. function_call_output'))), /TOOL_OUTPUT/);
});

test('unavailable detail service is distinct from deleted history even when the desktop call rejects', async () => {
  const f = fixture(); await settle();
  f.controls.responder = op => op === 'detail' ? Promise.reject(new Error('service unavailable')) : null;
  const unavailable = await f.show('response');
  assert.match(f.text(unavailable), /读取失败/);
  assert.doesNotMatch(f.text(unavailable), /已过期或删除/);
  f.controls.responder = op => op === 'detail' ? { ok: false, statusCode: 404 } : null;
  assert.match(f.text(await f.show('response', 'record-b')), /诊断记录已过期或删除/);
});

test('copying a block reports that the clipboard copy is outside retention', async () => {
  const f = fixture(); await settle();
  const container = await f.show('response');
  await findButton(container, '复制').click(); await settle();
  assert.deepEqual(f.calls.find(([op]) => op === 'copy'), ['copy', '<script>BAD()</script>']);
  assert.match(f.text(find(container, item => item.className.includes('model-traces-feedback'))), /剪贴板副本不受记录清理/);
});

test('main page opens the native viewer during the click and renders no viewer itself', async () => {
  const f = fixture({ standalone: false }); await settle();
  assert.equal(f.document.body.children.length, 0);
  const opening = deferred(); let invoked = false;
  f.window.desktopHost.openModelTracesWindow = context => { invoked = true; f.calls.push(['open-window', context]); return opening.promise; };
  f.document.body.classList.add('theme-light');
  const click = f.ids.get('btnViewModelTraces').click();
  assert.equal(invoked, true);
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls.find(([op]) => op === 'open-window')[1])),
    { account: 'account-a', user: 'contact-a', view: 'chat', theme: 'light' });
  f.window.ModelTraces.setScope('account-a', 'contact-b', 'advisor');
  opening.resolve(true); await click;
  const container = f.node('div'); f.window.ModelTraces.renderTrace(container, 'record-a', 'request'); await settle();
  assert.equal(container.children.length, 0);
  assert.equal(f.calls.some(([op]) => ['detail', 'body', 'close-window'].includes(op)), false);
  const current = f.calls.filter(([op]) => op === 'sync-window').at(-1)[1];
  assert.equal(current.user, 'contact-b'); assert.equal(current.view, 'advisor');
});

test('main page sends theme changes to an existing native viewer', async () => {
  const f = fixture({ standalone: false }); await settle();
  f.document.body.classList.add('theme-light'); f.controls.mutateTheme();
  assert.equal(f.calls.filter(([op]) => op === 'sync-window').at(-1)[1].theme, 'light');
  f.document.body.classList.remove('theme-light'); f.controls.mutateTheme();
  assert.equal(f.calls.filter(([op]) => op === 'sync-window').at(-1)[1].theme, 'dark');
});

test('opening model calls keeps a standalone settings window visible', async () => {
  const f = fixture({ standalone: false }); await settle();
  f.window.desktopHost.isSettingsWindow = true;
  f.ids.get('settingsModal').classList.add('show');
  await f.ids.get('btnViewModelTraces').click();
  assert.equal(f.ids.get('settingsModal').classList.contains('show'), true);
  assert.equal(f.calls.some(([op]) => op === 'open-window'), true);
});

test('failed native window opening leaves the main page usable and can be retried', async () => {
  const f = fixture({ standalone: false }); await settle();
  f.window.desktopHost.openModelTracesWindow = async () => false;
  await f.ids.get('btnModelTraces').click();
  assert.match(f.ids.get('modelTraceError').textContent, /窗口暂时无法打开/);
  let opened = false; f.window.desktopHost.openModelTracesWindow = async () => { opened = true; return true; };
  await f.ids.get('btnViewModelTraces').click(); assert.equal(opened, true);
  assert.doesNotMatch(f.ids.get('modelTraceError').textContent, /窗口暂时无法打开/);
});

test('Escape on the main page only dismisses the clear confirmation', async () => {
  const f = fixture({ standalone: false }); await settle();
  await f.ids.get('btnClearModelTraces').click();
  assert.equal(f.ids.get('modelTraceClearConfirm').hidden, false);
  const handler = f.documentHandlers.get('keydown')[0]; let prevented = false;
  handler({ key: 'Escape', isComposing: true }); assert.equal(f.ids.get('modelTraceClearConfirm').hidden, false);
  handler({ key: 'Escape', preventDefault() { prevented = true; }, stopImmediatePropagation() {} });
  assert.equal(prevented, true); assert.equal(f.ids.get('modelTraceClearConfirm').hidden, true);
  assert.equal(f.calls.some(([op]) => op === 'close-window'), false);
});

test('standalone bootstrap ignores stale initial context and applies live scope and theme', async () => {
  const f = fixture({ settings: false, initialize: false });
  const initial = deferred(); let listener;
  f.window.modelTracesWindow.getContext = () => initial.promise;
  f.window.modelTracesWindow.onContext = fn => { listener = fn; return () => {}; };
  for (const name of ['model-usage.js', 'model-traces-window.js']) vm.runInNewContext(readFileSync(require('node:path').join(__dirname, '../chatui/' + name), 'utf8'), f.context);
  listener({ revision: 2, account: 'account-a', user: 'contact-b', view: 'advisor', theme: 'light' }); await settle();
  initial.resolve({ revision: 1, account: 'old-account', user: 'old-contact', view: 'chat', theme: 'dark' }); await settle();
  // Usage statistics for all friends open first; captured bodies load only inside an opened record.
  assert.equal(f.window.ModelUsage.isOpen(), true);
  assert.equal(f.calls.some(([op]) => op === 'detail' || op === 'body'), false);
  assert.equal(f.ids.get('modelTracesStartup').hidden, true);
  assert.equal(f.document.body.classList.contains('theme-light'), true);
  assert.equal(f.calls.find(([op]) => op === 'usage-summary')[1].account, 'account-a');
  assert.equal(f.calls.some(([op, args]) => op.startsWith('usage-') && args.account === 'old-account'), false);
  listener({ revision: 3, account: 'account-b', user: 'contact-c', view: 'settings', theme: 'dark' }); await settle();
  assert.equal(f.calls.filter(([op]) => op === 'usage-summary').at(-1)[1].account, 'account-b');
  assert.equal(f.document.body.classList.contains('theme-light'), false);
  assert.equal(f.window.ModelUsage.isOpen(), true);
});
