// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// In-memory composer state. Chat text never enters browser storage or a send-scope notification.
(function (host) {
  "use strict";
  function create() {
    const drafts = new Map();
    let account = null, username = null, scopeRevision = 0, revision = 0;
    const key = () => account && username ? JSON.stringify([account, username]) : null;
    function snapshot() {
      const draft = drafts.get(key());
      return Object.freeze({ account, username, scopeRevision,
        draftRevision: draft?.revision || 0, text: draft?.text || "" });
    }
    function select(nextAccount, nextUsername, force = false) {
      nextAccount = typeof nextAccount === "string" && nextAccount ? nextAccount : null;
      nextUsername = nextAccount && typeof nextUsername === "string" && nextUsername ? nextUsername : null;
      if (account !== nextAccount || username !== nextUsername || force) {
        account = nextAccount; username = nextUsername; scopeRevision++;
      }
      return snapshot();
    }
    function update(text) {
      const id = key();
      if (id && typeof text === "string" && text !== snapshot().text)
        drafts.set(id, { text, revision: ++revision });
      return snapshot();
    }
    function isCurrent(candidate) {
      const current = snapshot();
      return !!candidate && ["account", "username", "scopeRevision", "draftRevision", "text"]
        .every(field => candidate[field] === current[field]);
    }
    function clearAccount(clearedAccount) {
      for (const id of drafts.keys()) if (JSON.parse(id)[0] === clearedAccount) drafts.delete(id);
      // Never reuse the old selection generation even when the same account is reopened.
      if (account === clearedAccount) select(null, null, true);
      return snapshot();
    }
    function accounts() { return [...new Set([...drafts.keys()].map(id => JSON.parse(id)[0]))]; }
    return Object.freeze({ snapshot, select, update, isCurrent, clearAccount, accounts });
  }
  const api = Object.freeze({ create });
  if (typeof module === "object" && module.exports) module.exports = api;
  else host.ComposerDrafts = api;
})(typeof window === "object" ? window : globalThis);
