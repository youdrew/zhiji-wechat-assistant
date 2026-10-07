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
