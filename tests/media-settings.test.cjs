// Know Your Friends, 2026-10-10. Apache-2.0.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(new URL('../chatui/media-settings.js', `file://${__filename.replace(/\\/g, '/')}`), 'utf8');
function setup(request) {
  const nodes = new Map();
  for (const id of ['mediaStorageMode', 'mediaCacheLimit', 'btnClearMediaCache', 'mediaStorageStatus', 'mediaStorageHint', 'btnSettings']) {
    nodes.set(id, { value: id === 'mediaStorageMode' ? 'reference' : '', textContent: '', disabled: false,
      listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; } });
  }
  let owner = 'account-a';
  const window = {};
  vm.runInNewContext(source, { window, document: { getElementById: id => nodes.get(id) } });
  window.MediaSettings.init({ account: () => owner, request });
  return { nodes, switchAccount: next => { owner = next; }, event: (id, name = 'click') => nodes.get(id).listeners[name]() };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (account = 'account-a', mode = 'reference') => ({ account, mode, cacheMaxBytes: 268435456, cacheBytes: 1048576, archiveBytes: 0 });
test('media settings stay account scoped and clear only cache', async () => {
  const calls = [];
  const ui = setup(async (url, options) => { calls.push([url, options]); return response('account-a', 'archive'); });
  assert.equal(ui.nodes.get('mediaStorageMode').disabled, true);
  ui.event('btnSettings'); await tick();
  assert.equal(calls[0][0], '/api/media/settings?account=account-a');
  assert.equal(ui.nodes.get('mediaStorageMode').disabled, false);
  assert.equal(ui.nodes.get('mediaStorageMode').value, 'archive');
  assert.match(ui.nodes.get('mediaStorageHint').textContent, /复制到本机/);
  assert.equal(ui.nodes.get('mediaStorageStatus').textContent, '缓存 1 MB · 留存 0 MB');
  ui.event('btnClearMediaCache'); await tick();
  assert.equal(calls[1][0], '/api/media/cache/clear');
  assert.deepEqual(JSON.parse(calls[1][1].body), { account: 'account-a' });
});
test('failed changes restore the saved mode and leave a retryable control', async () => {
  let count = 0;
  const ui = setup(async () => { if (++count > 1) throw new Error('offline'); return response(); });
  ui.event('btnSettings'); await tick();
  ui.nodes.get('mediaStorageMode').value = 'archive';
  ui.event('mediaStorageMode', 'change'); await tick();
  assert.equal(ui.nodes.get('mediaStorageMode').value, 'reference');
  assert.equal(ui.nodes.get('mediaStorageMode').disabled, false);
  assert.equal(ui.nodes.get('mediaStorageStatus').textContent, '保存失败，请重试');
});
test('late settings cannot cross an account change or mutate the next account', async () => {
  let resolve;
  const ui = setup(() => new Promise(done => { resolve = done; }));
  ui.event('btnSettings');
  ui.switchAccount('account-b');
  resolve(response()); await tick();
  assert.equal(ui.nodes.get('mediaStorageMode').disabled, true);
  assert.equal(ui.nodes.get('mediaStorageStatus').textContent, '账号已切换，请重新打开设置');
});
