const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter } = require('node:events');
const { attachModelTraces } = require('../scripts/model-traces-host.cjs');

function fixture(responder = () => ({ records: [] }), shared = {}) {
  const ipcMain = shared.ipcMain || new EventEmitter(), handlers = shared.handlers || new Map(), calls = [];
  ipcMain.handle = (key, fn) => handlers.set(key, fn); ipcMain.removeHandler = key => handlers.delete(key);
  const webContents = new EventEmitter(); webContents.id = 10;
  webContents.send = (channel, payload) => webContents.emit(channel, payload);
  const frame = {}, event = { sender: webContents, senderFrame: frame };
  let serial = 0;
  const mainWindow = { webContents };
  const host = attachModelTraces({ ipcMain, mainWindow, clientUrl: 'http://127.0.0.1:43210/',
    trustedMainFrame: e => e.sender === webContents && e.senderFrame === frame,
    getControlToken: () => 'synthetic-control', fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      const data = String(url).endsWith('/session') ? { token: `synthetic-viewer-${++serial}`, accountId: 'account-a', scope: JSON.parse(options.body).scope, deleteVersion: 0 } : await responder(url, options);
      return { ok: true, status: 200, json: async () => data };
    } });
  return { calls, event, handlers, ipcMain, webContents, mainWindow, host, run: (operation, args) => handlers.get('model-traces:request')(event, operation, args) };
}

test('window destruction revokes pending reads without accessing destroyed window contents', async () => {
  let resolve;
  const f = fixture(() => new Promise(done => { resolve = done; }));
  await f.run('scope', { scope: 'account' });
  const pending = f.run('body', { id: 'closing', kind: 'request' });
  await new Promise(done => setImmediate(done));
  const signal = f.calls.at(-1).options.signal;
  Object.defineProperty(f.mainWindow, 'webContents', { get() { throw new Error('Object has been destroyed'); } });
  assert.doesNotThrow(() => f.host.close());
  assert.equal(signal.aborted, true);
  assert.equal(f.webContents.listenerCount('did-start-navigation'), 0);
  resolve({ text: 'CLOSED_WINDOW_PRIVATE_BODY' });
  assert.equal((await pending).error, 'trace-stale');
});

test('repeated teardown cannot remove a newly registered viewer', async () => {
  const first = fixture(); first.host.close();
  const second = fixture(undefined, first);
  first.host.close();
  assert.equal((await second.run('state')).ok, true);
  second.host.close();
});
test('only the trusted main frame can mint a viewer; tokens never return to renderer', async () => {
  const f = fixture();
  assert.equal((await f.handlers.get('model-traces:request')({ sender: {}, senderFrame: {} }, 'scope', { scope: 'account' })).statusCode, 403);
  assert.equal(f.calls.length, 0);
  const result = await f.run('scope', { scope: 'account', account: 'victim' });
  assert.equal(result.accountId, 'account-a'); assert.equal(result.token, undefined);
  assert.equal(JSON.parse(f.calls[0].options.body).account, undefined);
  assert.equal(f.calls[0].options.headers['X-KYF-Control-Token'], 'synthetic-control');
  await f.run('state'); assert.equal(f.calls[1].options.headers['X-Model-Trace-Session'], 'synthetic-viewer-1');
  assert.equal(f.calls[1].options.headers['X-KYF-Control-Token'], undefined); f.host.close();
});
test('viewer forwarding fixes page and chunk limits and rejects arbitrary URLs and account filters', async () => {
  const f = fixture(); await f.run('scope', { scope: 'account' });
  await f.run('records', { limit: 9999, account: 'victim', offset: 50, user: 'contact' });
  const url = new URL(f.calls.at(-1).url); assert.equal(url.searchParams.get('limit'), '50'); assert.equal(url.searchParams.has('account'), false);
  const count = f.calls.length;
  assert.equal((await f.run('https://evil.example')).statusCode, 403);
  assert.equal((await f.run('body', { id: '../other', kind: 'request' })).ok, false);
  assert.equal(f.calls.length, count);
  await f.run('body', { id: 'valid-id', kind: 'request', limit: 999999 });
  assert.equal(new URL(f.calls.at(-1).url).searchParams.get('limit'), '65536'); f.host.close();
});
test('scope changes abort and discard a late old-account body even if transport ignores cancellation', async () => {
  let resolve;
  const delayed = new Promise(done => { resolve = done; });
  const f = fixture(() => delayed); await f.run('scope', { scope: 'account' });
  const body = f.run('body', { id: 'old', kind: 'request' }); await new Promise(done => setImmediate(done));
  const request = f.calls.at(-1);
  await f.run('scope', { scope: 'application' }); assert.equal(request.options.signal.aborted, true);
  resolve({ text: 'OLD_PRIVATE_BODY' }); const result = await body;
  assert.equal(result.error, 'trace-stale'); assert.equal(result.text, undefined); f.host.close();
});
test('clear requires an explicit all-accounts confirmation and navigation invalidates a capability', async () => {
  const f = fixture(); await f.run('scope', { scope: 'account' });
  assert.equal((await f.run('clear')).error, 'trace-confirmation-required');
  await f.run('clear', { confirmAllAccounts: true }); assert.deepEqual(JSON.parse(f.calls.at(-1).options.body), { confirmAllAccounts: true });
  f.webContents.emit('did-start-navigation'); await f.run('state');
  assert.equal(f.calls.filter(call => call.url.endsWith('/session')).length, 3); f.host.close();
});

test('clearing all records revokes an already in-flight body delivery', async () => {
  let resolve;
  const f = fixture(url => String(url).includes('/body?') ? new Promise(done => { resolve = done; }) : { cleared: true });
  await f.run('scope', { scope: 'account' });
  const old = f.run('body', { id: 'deleted', kind: 'request' }); await new Promise(done => setImmediate(done));
  const signal = f.calls.at(-1).options.signal;
  await f.run('clear', { confirmAllAccounts: true }); assert.equal(signal.aborted, true);
  resolve({ text: 'SHOULD_NOT_REACH_RENDERER' });
  const result = await old; assert.equal(result.error, 'trace-stale'); assert.equal(result.text, undefined); f.host.close();
});

test('all trusted windows revoke cached and in-flight bodies when another window clears', async () => {
  let resolve;
  const first = fixture(() => ({ cleared: true, deleteVersion: 1 }));
  const second = fixture(url => String(url).includes('/body?') ? new Promise(done => { resolve = done; }) : { records: [] }, first);
  const invalidations = [];
  second.webContents.on('model-traces:invalidated', value => invalidations.push(value));
  await first.run('scope', { scope: 'account' }); await second.run('scope', { scope: 'account' });
  const old = second.run('body', { id: 'deleted', kind: 'request' }); await new Promise(done => setImmediate(done));
  const signal = second.calls.at(-1).options.signal;
  await first.run('clear', { confirmAllAccounts: true });
  assert.equal(signal.aborted, true); assert.equal(invalidations.at(-1).deleteVersion, 1);
  resolve({ text: 'OTHER_WINDOW_PRIVATE_BODY', deleteVersion: 0 });
  assert.equal((await old).error, 'trace-stale');
  first.host.close(); assert.equal((await second.run('state')).ok, true);
  second.host.close(); assert.equal(first.handlers.size, 0);
});

test('usage reads go to the Core ledger without a trace session and clearing needs confirmation', async () => {
  const f = fixture(() => ({ requests: 3 }));
  const summary = await f.run('usage-summary', { account: 'account-a', friend: 'wxid_a', since: '1700000000000', limit: '50', junk: 'x' });
  assert.equal(summary.ok, true); assert.equal(summary.requests, 3);
  assert.equal(f.calls.length, 1);
  const url = new URL(f.calls[0].url);
  assert.equal(url.pathname, '/api/usage/summary');
  assert.deepEqual(Object.fromEntries(url.searchParams), { account: 'account-a', friend: 'wxid_a', since: '1700000000000', limit: '50' });
  assert.equal(f.calls[0].options.headers, undefined);
  await f.run('usage-records', { account: 'account-a', before: '123,abc' });
  assert.equal(new URL(f.calls.at(-1).url).pathname, '/api/usage/records');
  assert.equal((await f.run('usage-summary', {})).statusCode, 400);
  assert.equal((await f.run('usage-clear', { account: 'account-a' })).error, 'usage-confirmation-required');
  const count = f.calls.length;
  await f.run('usage-clear', { account: 'account-a', confirm: true });
  assert.equal(f.calls.length, count + 1);
  assert.equal(f.calls.at(-1).options.method, 'POST'); assert.deepEqual(JSON.parse(f.calls.at(-1).options.body), { account: 'account-a' });
  assert.equal((await f.run('usage-other', { account: 'account-a' })).statusCode, 403);
  assert.equal((await f.run('toString', { account: 'account-a' })).statusCode, 403);
  assert.equal((await f.handlers.get('model-traces:request')({ sender: {}, senderFrame: {} }, 'usage-summary', { account: 'account-a' })).statusCode, 403);
  f.host.close();
});

test('usage trend and module IPC forwards selected periods and step filters without a trace capability', async () => {
  const f = fixture(url => new URL(url).pathname.endsWith('/timeline') ? { buckets: [] } : { modules: [] });
  const boundaries = Array.from({ length: 1001 }, (_, index) => String(1791800000000 + index * 3600000)).join(',');
  assert.ok(boundaries.length > 256, 'calendar boundaries exceed ordinary filter limits');
  const filters = { account: 'account-a', friend: 'wxid_a', plugin: 'kyf.friend-analysis', step: 'overview',
    model: 'model-x', status: 'cancelled', since: '1791800000000', until: '1791886400000' };
  assert.equal((await f.run('usage-timeline', { ...filters, boundaries, junk: 'ignored' })).ok, true);
  const timeline = new URL(f.calls.at(-1).url);
  assert.equal(timeline.pathname, '/api/usage/timeline');
  assert.deepEqual(Object.fromEntries(timeline.searchParams), { ...filters, boundaries });
  assert.equal(f.calls.at(-1).options.headers, undefined);
  assert.equal((await f.run('usage-modules', { ...filters, boundaries })).ok, true);
  const modules = new URL(f.calls.at(-1).url);
  assert.equal(modules.pathname, '/api/usage/modules');
  assert.deepEqual(Object.fromEntries(modules.searchParams), filters);
  assert.equal(f.calls.some(call => call.url.endsWith('/session')), false);
  f.host.close();
});

test('usage timeline has a bounded dedicated boundaries argument and rejects malformed input before HTTP', async () => {
  const f = fixture(() => ({ buckets: [] }));
  const max = '1,'.repeat(15999) + '10';
  assert.equal(max.length, 32000);
  assert.equal((await f.run('usage-timeline', { account: 'account-a', boundaries: max })).ok, true);
  assert.equal(new URL(f.calls.at(-1).url).searchParams.get('boundaries'), max);
  const callsBefore = f.calls.length;
  for (const boundaries of [undefined, '', '123', '1,2,', '1, -2', '1,2.5', '1,2\n', [1, 2], '1,'.repeat(16000) + '1']) {
    const result = await f.run('usage-timeline', { account: 'account-a', boundaries });
    assert.equal(result.ok, false);
    assert.equal(result.statusCode, 400);
    assert.equal(result.error, 'usage-invalid-boundaries');
  }
  assert.equal(f.calls.length, callsBefore);
  assert.equal((await f.run('usage-modules', { account: 'account-a', step: 'x'.repeat(257) })).ok, true);
  assert.equal(new URL(f.calls.at(-1).url).searchParams.has('step'), false);
  f.host.close();
});
