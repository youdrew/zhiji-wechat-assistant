const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const window = {};
for (const name of ['composer-drafts.js', 'chat-composer.js'])
  vm.runInNewContext(readFileSync(path.join(__dirname, '../chatui', name), 'utf8'), { window });
const Drafts = window.ComposerDrafts;
const Composer = window.Composer;

function fixture() {
  const listeners = new Map();
  const input = { value: '', disabled: false,
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: name => listeners.delete(name) };
  const copies = [];
  const ui = Composer.create({ drafts: Drafts.create(), input, copy: () => copies.push(input.value) });
  const select = (account, username, available = true) => ui.setScope(account, username, { available });
  const type = text => { input.value = text; listeners.get('input')?.(); };
  const key = changes => { const event = { key: 'Enter', shiftKey: false, isComposing: false,
    prevented: false, preventDefault() { this.prevented = true; }, ...changes }; listeners.get('keydown')?.(event); return event; };
  return { input, copies, ui, select, type, key, listeners };
}

test('drafts survive contact and account switches without being shared', () => {
  const f = fixture(); f.select('account-a', 'friend-a'); f.type('first draft');
  f.select('account-a', 'friend-b'); assert.equal(f.input.value, ''); f.type('second draft');
  f.select('account-b', 'friend-a'); assert.equal(f.input.value, ''); f.type('another account');
  f.select('account-a', 'friend-a'); assert.equal(f.input.value, 'first draft');
  f.select('account-a', 'friend-b'); assert.equal(f.input.value, 'second draft');
  f.select('account-b', 'friend-a'); assert.equal(f.input.value, 'another account');
});

test('Enter copies the current draft; composition and Shift+Enter retain typing', () => {
  const f = fixture(); f.select('account-a', 'friend-a'); f.type('copy this');
  assert.equal(f.key({ isComposing: true }).prevented, false);
  assert.equal(f.key({ shiftKey: true }).prevented, false); assert.deepEqual(f.copies, []);
  assert.equal(f.key({}).prevented, true); assert.deepEqual(f.copies, ['copy this']);
  assert.equal(f.input.value, 'copy this');
  f.select('account-a', 'friend-a', false); f.key({}); assert.equal(f.copies.length, 1);
});

test('clearing an account removes its drafts while retaining other accounts', () => {
  const f = fixture(); f.select('account-a', 'friend-a'); f.type('clear me');
  f.select('account-b', 'friend-a'); f.type('retain me'); f.ui.clearAccount('account-a');
  assert.equal(f.input.value, 'retain me'); assert.deepEqual(Array.from(f.ui.accounts()), ['account-b']);
  f.ui.clearAccount('account-b'); assert.equal(f.input.value, ''); assert.equal(f.input.disabled, true);
  f.select('account-a', 'friend-a'); assert.equal(f.input.value, '');
});

test('selection captures a programmatic edit and disposal detaches copy gestures', () => {
  const f = fixture(); f.select('account-a', 'friend-a'); f.input.value = 'manual edit before navigation';
  f.select('account-a', 'friend-b'); f.select('account-a', 'friend-a');
  assert.equal(f.input.value, 'manual edit before navigation');
  f.ui.dispose(); f.ui.dispose(); assert.equal(f.listeners.size, 0); f.key({}); assert.deepEqual(f.copies, []);
});
