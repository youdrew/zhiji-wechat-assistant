"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared helpers of the 知己分析 scheme page (moved from the window's friend-core.js, 2026-10-13).
// The page runs in a sandboxed frame; data comes from this plugin (A.rpc) or from the host (A.host).
(function () {
  const byId = id => document.getElementById(id);
  let masked = false;

  function el(tag, className = "", text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function button(label, className, onClick, attributes = {}) {
    const node = el("button", className, label);
    node.type = "button";
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }
  const pad = value => String(value).padStart(2, "0");
  function formatDate(ms, withTime = false) {
    if (!Number.isFinite(ms) || ms <= 0) return "时间未知";
    const date = new Date(ms);
    const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    return withTime ? `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}` : day;
  }
  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    if (seconds < 90) return `${Math.round(seconds)} 秒`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟`;
    if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} 小时`;
    return `${(seconds / 86400).toFixed(1)} 天`;
  }
  let toastTimer = null;
  function toast(message) {
    const node = byId("faToast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
  }
  // Sandboxed frames get no window.confirm() (Electron drops it silently); a small page dialog asks
  // instead. Modified 2026-10-14. Escape, the backdrop and 取消 answer no.
  function confirm(message, action = "确定") {
    return new Promise(resolve => {
      const overlay = el("div", "fa-confirm"), card = el("div", "fa-confirm-card"), actions = el("div", "fa-confirm-actions");
      const onKey = event => { if (event.key === "Escape") done(false); };
      const done = value => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
      const cancel = button("取消", "fa-plain", () => done(false));
      actions.append(cancel, button(action, "fa-plain fa-danger-text", () => done(true)));
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.append(el("p", "", message), actions);
      overlay.append(card);
      overlay.addEventListener("click", event => { if (event.target === overlay) done(false); });
      document.addEventListener("keydown", onKey);
      document.body.append(overlay);
      cancel.focus();
    });
  }
  function mask(message) {
    if (masked) return;
    masked = true;
    for (const id of ["pageOverview", "pagePersona", "pageValues", "pageChat", "pageTrend", "faSheetBody"]) byId(id).replaceChildren();
    byId("pageOverview").hidden = false;
    byId("pageOverview").append(el("div", "fa-empty", message));
  }
  function failed(error) {
    if (["account-changed", "account-unavailable", "account-paused", "account-closed"].includes(error?.code))
      mask("微信账号已切换或正在清理，此页面的资料已隐藏。");
    return error;
  }
  const rpc = (method, params) => masked ? Promise.reject(new Error("页面已失效")) :
    window.kyfHost.rpc(method, params).catch(error => { throw failed(error); });
  const host = (method, params) => masked ? Promise.reject(new Error("页面已失效")) :
    window.kyfHost.host(method, params).catch(error => { throw failed(error); });

  // -- sheet (inside this page) ----------------------------------------------------------------------
  let sheetOwner = null;
  function openSheet(title, owner) {
    sheetOwner = owner;
    byId("faSheetTitle").textContent = title;
    byId("faSheetBody").replaceChildren();
    byId("faSheet").hidden = false;
    return byId("faSheetBody");
  }
  function closeSheet() {
    sheetOwner = null;
    byId("faSheet").hidden = true;
    byId("faSheetBody").replaceChildren();
  }
  byId("faSheetClose").addEventListener("click", closeSheet);
  window.addEventListener("keydown", event => { if (event.key === "Escape" && !byId("faSheet").hidden) closeSheet(); });

  // -- drafts of the user's own observations are kept by the host -------------------------------------
  const drafts = { saved: new Map(), closed: {}, active: new Map(), timers: new Map() };
  async function loadDrafts() {
    const data = await host("drafts.list");
    drafts.saved = new Map((data.drafts || []).map(item => [item.entry, item]));
    drafts.closed = data.closedGenerations || {};
    return drafts.saved;
  }
  function draft(entry) {
    let state = drafts.active.get(entry);
    if (state) return state;
    const existing = drafts.saved.get(entry);
    state = { entry, generation: existing ? existing.generation : Number(drafts.closed[entry] || 0) + 1,
      seq: existing ? existing.seq : 0, payload: existing ? existing.payload : null,
      baseVersion: existing ? existing.baseVersion : null, restored: !!existing, dirty: false };
    drafts.active.set(entry, state);
    return state;
  }
  async function flushDraft(state) {
    clearTimeout(drafts.timers.get(state.entry));
    drafts.timers.delete(state.entry);
    if (!state.dirty || masked) return true;
    state.dirty = false;
    try {
      const result = await host("drafts.save", { entry: state.entry, generation: state.generation, seq: ++state.seq,
        payload: state.payload, baseVersion: state.baseVersion });
      return result?.saved === true || drafts.active.get(state.entry) !== state;
    } catch {
      if (drafts.active.get(state.entry) === state) state.dirty = true;
      return masked;
    }
  }
  function updateDraft(state, payload, baseVersion = null) {
    if (masked) return;
    state.payload = payload;
    state.baseVersion = baseVersion;
    state.dirty = true;
    clearTimeout(drafts.timers.get(state.entry));
    drafts.timers.set(state.entry, setTimeout(() => { void flushDraft(state); }, 500));
  }
  async function closeDraft(entry) {
    const state = drafts.active.get(entry) || (drafts.saved.has(entry) ? draft(entry) : null);
    if (!state) return;
    clearTimeout(drafts.timers.get(entry));
    drafts.timers.delete(entry);
    drafts.active.delete(entry);
    drafts.saved.delete(entry);
    drafts.closed[entry] = state.generation;
    try { await host("drafts.close", { entry, generation: state.generation }); } catch { }
  }
  async function flushDrafts() {
    if (masked) return true;
    return (await Promise.all([...drafts.active.values()].map(flushDraft))).every(Boolean);
  }

  // Five messages on each side of a cited one, read by the host from the managed history.
  function contextList(data) {
    const box = el("div", "fa-context");
    if (data.status !== "ok") {
      box.append(el("p", "fa-note", data.status === "unready" ? "聊天历史仍在读取，稍后再看。" : "原文已不在当前可读历史中。"));
      return box;
    }
    for (const message of data.messages) {
      const row = el("div", message.target ? "fa-msg target" : "fa-msg");
      row.append(el("div", "fa-msg-head", `${message.speaker} · ${formatDate(message.timeMs, true)}`),
        el("div", "fa-msg-text", message.text));
      box.append(row);
    }
    return box;
  }

  window.FriendApp = {
    byId, el, button, formatDate, formatDuration, toast, confirm, mask, rpc, host, openSheet, closeSheet, contextList,
    loadDrafts, draft, updateDraft, closeDraft, flushDrafts, savedDrafts: () => drafts.saved,
    selectTab: tab => { void host("tab.select", { tab }).catch(() => { }); },
    get scope() { return window.kyfHost.context() || {}; },
    get masked() { return masked; },
    get sheetOwner() { return sheetOwner; },
  };
})();
