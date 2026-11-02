// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Account-scoped drafts stay in memory. Enter copies; Shift+Enter inserts a newline.
(function (host) {
  "use strict";
  function create({ drafts, input, copy }) {
    let disposed = false, available = false;
    function captureInput() {
      if (input.value !== drafts.snapshot().text) drafts.update(input.value);
    }
    function setScope(account, username, options = {}) {
      if (disposed) return;
      captureInput();
      const current = drafts.select(account, username, options.force === true);
      available = options.available === true;
      input.value = current.text;
      input.disabled = !available || !current.account || !current.username;
    }
    function changed() { if (!disposed) captureInput(); }
    function keydown(event) {
      if (!disposed && !input.disabled && event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        copy();
      }
    }
    function clearAccount(account) {
      const active = drafts.snapshot().account === account;
      drafts.clearAccount(account);
      if (active) { available = false; input.value = ""; input.disabled = true; }
    }
    input.addEventListener("input", changed);
    input.addEventListener("keydown", keydown);
    input.disabled = true;
    return Object.freeze({ setScope, clearAccount, accounts: drafts.accounts,
      dispose() {
        if (disposed) return;
        captureInput(); disposed = true;
        input.removeEventListener("input", changed);
        input.removeEventListener("keydown", keydown);
        drafts.select(null, null, true);
      } });
  }
  const api = Object.freeze({ create });
  if (typeof module === "object" && module.exports) module.exports = api;
  else host.Composer = api;
})(typeof window === "object" ? window : globalThis);
