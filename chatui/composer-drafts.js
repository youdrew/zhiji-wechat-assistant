// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// In-memory composer state. Chat text never enters browser storage or a send-scope notification.
// Modified 2026-11-04: windows showing the same [account, contact] share its draft over an optional
// in-memory BroadcastChannel. Each draft carries a Lamport stamp {clock, origin}; the newest stamp wins,
// so concurrent edits in two windows converge. A window that opens a contact it holds no draft for asks
// its peers once; a reply never overrides a draft this window already holds. Clearing an account's
// data is broadcast too: every window drops that account's drafts, and a draft stamped before the
// clear (still in flight, or held by a late peer) is never accepted or offered again.
(function (host) {
  "use strict";
  const MAX_TEXT = 200000, MAX_ID = 512;
  const validId = value => typeof value === "string" && value.length > 0 && value.length <= MAX_ID;
  function randomOrigin() {
    try { if (typeof host.crypto?.randomUUID === "function") return host.crypto.randomUUID(); } catch { }
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
  function create(options = {}) {
    const channel = options.channel && typeof options.channel.postMessage === "function" ? options.channel : null;
    const origin = validId(options.origin) ? options.origin : randomOrigin();
    const drafts = new Map(), listeners = new Set(), cleared = new Map();
    let account = null, username = null, scopeRevision = 0, revision = 0, clock = 0, closed = false;
    const keyOf = (owner, contact) => owner && contact ? JSON.stringify([owner, contact]) : null;
    const key = () => keyOf(account, username);
    // A stamp is newer by clock, then by origin, so every window orders two edits the same way.
    const newer = (candidate, existing) => !existing || candidate.clock > existing.clock ||
      candidate.clock === existing.clock && candidate.origin > existing.origin;
    // A clear covers every draft of its account stamped at or before it; later edits start afresh.
    const clearedBy = (owner, stamp) => (cleared.get(owner) || 0) >= stamp.clock;
    function post(message) {
      if (!channel || closed) return;
      try { channel.postMessage(message); } catch { /* A closed peer channel loses only the sync. */ }
    }
    function snapshot() {
      const draft = drafts.get(key());
      return Object.freeze({ account, username, scopeRevision,
        draftRevision: draft?.revision || 0, text: draft?.text || "" });
    }
    function select(nextAccount, nextUsername, force = false) {
      nextAccount = typeof nextAccount === "string" && nextAccount ? nextAccount : null;
      nextUsername = nextAccount && typeof nextUsername === "string" && nextUsername ? nextUsername : null;
      if (account !== nextAccount || username !== nextUsername || force) {
        const opened = account !== nextAccount || username !== nextUsername;
        account = nextAccount; username = nextUsername; scopeRevision++;
        if (opened && key() && !drafts.has(key())) post({ type: "request", account, username });
      }
      return snapshot();
    }
    function update(text) {
      const id = key();
      if (id && typeof text === "string" && text !== snapshot().text) {
        clock++;
        drafts.set(id, { text, revision: ++revision, clock, origin });
        post({ type: "draft", account, username, text, clock, origin });
      }
      return snapshot();
    }
    function isCurrent(candidate) {
      const current = snapshot();
      return !!candidate && ["account", "username", "scopeRevision", "draftRevision", "text"]
        .every(field => candidate[field] === current[field]);
    }
    function notify(change) {
      for (const listener of [...listeners]) {
        try { listener(Object.freeze(change)); } catch { /* One listener's failure does not stop the others. */ }
      }
    }
    function dropAccount(owner) {
      let shown = false;
      for (const id of [...drafts.keys()]) if (JSON.parse(id)[0] === owner) {
        if (id === key() && drafts.get(id).text) shown = true;
        drafts.delete(id);
      }
      return shown;
    }
    // Applies one peer message. Returns true when the stored draft text changed.
    function receive(message) {
      if (closed || !message || typeof message !== "object") return false;
      if (message.type === "clear") {
        if (!validId(message.account) || !Number.isSafeInteger(message.clock) || message.clock < 1) return false;
        clock = Math.max(clock, message.clock);
        cleared.set(message.account, Math.max(cleared.get(message.account) || 0, message.clock));
        const shown = dropAccount(message.account);
        if (shown) notify({ account, username });
        return shown;
      }
      if (!validId(message.account) || !validId(message.username)) return false;
      const id = keyOf(message.account, message.username), existing = drafts.get(id);
      if (message.type === "request") {
        if (existing?.text && !clearedBy(message.account, existing)) post({ type: "reply", account: message.account, username: message.username,
          text: existing.text, clock: existing.clock, origin: existing.origin });
        return false;
      }
      if (message.type !== "draft" && message.type !== "reply") return false;
      if (typeof message.text !== "string" || message.text.length > MAX_TEXT || !validId(message.origin) ||
          !Number.isSafeInteger(message.clock) || message.clock < 1) return false;
      clock = Math.max(clock, message.clock);
      if (clearedBy(message.account, message)) return false;
      if (message.type === "reply" && existing) return false;
      if (existing && !newer(message, existing)) return false;
      if (existing?.text === message.text) {
        // Same text: adopt the stamp without a new revision, so no in-flight operation is revoked.
        existing.clock = message.clock; existing.origin = message.origin;
        return false;
      }
      drafts.set(id, { text: message.text, revision: ++revision, clock: message.clock, origin: message.origin });
      notify({ account: message.account, username: message.username });
      return true;
    }
    // listener({account, username}) runs after a peer changed a stored draft.
    function subscribe(listener) {
      if (typeof listener !== "function") return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
    function clearAccount(clearedAccount) {
      for (const id of drafts.keys()) if (JSON.parse(id)[0] === clearedAccount) drafts.delete(id);
      if (validId(clearedAccount)) {
        clock++;
        cleared.set(clearedAccount, clock);
        post({ type: "clear", account: clearedAccount, clock, origin });
      }
      // Never reuse the old selection generation even when the same account is reopened.
      if (account === clearedAccount) select(null, null, true);
      return snapshot();
    }
    function accounts() { return [...new Set([...drafts.keys()].map(id => JSON.parse(id)[0]))]; }
    const onMessage = event => { receive(event?.data); };
    if (channel) {
      if (typeof channel.addEventListener === "function") channel.addEventListener("message", onMessage);
      else channel.onmessage = onMessage;
    }
    function close() {
      if (closed) return;
      closed = true;
      listeners.clear();
      if (!channel) return;
      try {
        if (typeof channel.removeEventListener === "function") channel.removeEventListener("message", onMessage);
        channel.close?.();
      } catch { }
    }
    return Object.freeze({ snapshot, select, update, isCurrent, clearAccount, accounts, receive, subscribe, close });
  }
  const api = Object.freeze({ create });
  if (typeof module === "object" && module.exports) module.exports = api;
  else host.ComposerDrafts = api;
})(typeof window === "object" ? window : globalThis);
