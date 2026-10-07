const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../chatui/app.js'), 'utf8');
const start = source.indexOf('async function deleteManagedAccount()');
const end = source.indexOf('\nasync function copyDraft(', start);

test('current-account deletion clears Advisor using resolved account identity, not its public hash', async () => {
  const account = 'synthetic-current-account';
  const accountId = crypto.createHash('sha256').update(account).digest('hex');
  const cleared = [];
  const nodes = new Map();
  const context = vm.createContext({
    managedAccounts: [{ accountId }], pendingDeleteAccountId: accountId, accountDeleteBusy: false,
    accountClearedExiting: false, selectedManagedAccountId: accountId,
    byId(id) { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    renderManagedAccounts() {}, text() {}, resetAccountView() {}, clearTimeout() {}, startupWatchdog: null,
    chatState: { sessionRequest: 1, advance() {}, currentUser: 'synthetic-friend', controller: null },
    portraitState: { advance() {} },
    fetch: async () => ({ ok: true, json: async () => ({ deleted: accountId, current: true, exitApp: true }) }),
    clearStoredProfilesForAccount: async id => { assert.equal(id, accountId); return new Set([account]); },
    window: { Advisor: { onAccountCleared: id => cleared.push(id) }, desktopHost: { exitApp: async () => true } }
  });
  vm.runInContext(source.slice(start, end) + '\nglobalThis.clear = deleteManagedAccount;', context);
  await context.clear();
  assert.deepEqual(cleared, [account]);
  assert.ok(!cleared.includes(accountId));
  assert.equal(context.accountClearedExiting, true);
});
