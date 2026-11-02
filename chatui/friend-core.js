"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared state for one 知己分析 window: fixed account + contact, API, drafts, tasks and masking.
// Modified 2026-10-13 (Apache-2.0): the host part of the window; scheme pages run in a sandboxed frame.
(function () {
  const byId = id => document.getElementById(id);
  const parameters = new URLSearchParams(window.location.hash.slice(1));
  const valid = (value, maximum = 256) => typeof value === "string" && value.length > 0 && value.length <= maximum &&
    !/[\u0000-\u001f\u007f]/u.test(value);
  const scope = { account: parameters.get("account"), user: parameters.get("user"), name: parameters.get("name") };
  const validScope = valid(scope.account) && valid(scope.user) && !scope.user.endsWith("@chatroom");
  if (!valid(scope.name, 128)) scope.name = scope.user || "";
  const avatar = parameters.get("avatar");
  scope.avatar = typeof avatar === "string" && avatar.length <= 512 && /^https?:\/\/[^\s"'<>]+$/i.test(avatar) ? avatar : "";
  const listeners = new Set();
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
  function mask(message) {
    if (masked) return;
    masked = true;
    byId("faMaskText").textContent = message;
    byId("faMask").hidden = false;
    for (const listener of listeners) try { listener({ type: "masked" }); } catch { }
    // Old account data never re-renders after this point; the scheme page is removed.
    for (const id of ["pageMemory", "faRunInfo", "faSheetBody", "faSchemeFrame"]) byId(id)?.replaceChildren();
    byId("faRunButton").hidden = true;
  }

  class ApiError extends Error {
    constructor(status, code, message) { super(message || "请求失败"); this.status = status; this.code = code; }
  }
  async function api(path, options = {}) {
    if (masked) throw new ApiError(0, "masked", "窗口已失效");
    const init = { method: options.method || "GET", signal: options.signal, headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      // The window's fixed account and contact always win over caller fields.
      init.body = JSON.stringify({ ...options.body, account: scope.account, user: scope.user });
    }
    let response;
    try { response = await fetch(path, init); }
    catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new ApiError(0, "network", "本地服务暂时无法连接");
    }
    let data = null;
    try { data = await response.json(); } catch { }
    if (!response.ok) {
      const code = typeof data?.error === "string" ? data.error : "";
      if (["AccountChangedError", "AccountUnavailableError"].includes(code) ||
          (response.status === 409 && ["account-paused", "account-closed"].includes(code)))
        mask("微信账号已切换或正在清理，此窗口的资料已隐藏。请从聊天重新打开知己分析。");
      throw Object.assign(new ApiError(response.status, code, typeof data?.message === "string" ? data.message : "请求失败"),
        { detail: data?.detail });
    }
    if (data && typeof data.account === "string" && data.account !== scope.account) {
      mask("微信账号已切换，此窗口的资料已隐藏。请从聊天重新打开知己分析。");
      throw new ApiError(0, "account-changed", "账号已变化");
    }
    return data;
  }
  function query(extra = {}) {
    return new URLSearchParams({ ...extra, account: scope.account, user: scope.user }).toString();
  }

  // -- theme follows the main window's saved setting ---------------------------------
  function applyTheme() {
    let theme = "dark";
    try { theme = JSON.parse(localStorage.getItem("real-ui-settings-1") || "{}").theme === "light" ? "light" : "dark"; }
    catch { }
    document.body.classList.toggle("theme-light", theme === "light");
  }
  window.addEventListener("storage", event => {
    if (event.key === "real-ui-settings-1") applyTheme();
    for (const listener of listeners) try { listener({ type: "storage", key: event.key }); } catch { }
  });

  // -- account changes announced by the main window -----------------------------------
  async function accountId(account) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(account));
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  }
  let channel = null;
  try { channel = new BroadcastChannel("kyf-friend-analysis"); } catch { }
  channel?.addEventListener("message", async event => {
    const message = event.data;
    if (!message || typeof message.type !== "string") return;
    if (message.type === "account" && typeof message.value === "string" && message.value !== scope.account)
      mask("微信账号已切换，此窗口的资料已隐藏。请从聊天重新打开知己分析。");
    else if (message.type === "account-cleared" && typeof message.value === "string" &&
             message.value === await accountId(scope.account))
      mask("该账号的数据已清除，此窗口已失效。");
    for (const listener of listeners) try { listener(message); } catch { }
  });
  function broadcast(type, value) {
    try { channel?.postMessage({ type, value: value ?? null, from: scope.user, account: scope.account }); } catch { }
  }

  // -- one-line tasks --------------------------------------------------------------------
  const tasks = new Map();
  function setTask(key, task) {
    if (!task) tasks.delete(key); else tasks.set(key, task);
    const container = byId("faTasks");
    container.replaceChildren();
    for (const [_name, item] of tasks) {
      const row = el("div", `fa-task${item.state ? " " + item.state : ""}`);
      row.append(el("span", "fa-task-text", item.text));
      for (const action of item.actions || []) row.append(button(action.label, "fa-plain", action.onClick));
      container.append(row);
    }
  }

  // -- sheet -------------------------------------------------------------------------------
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

  // -- drafts: autosaved per account + contact + entry, never treated as saved data ----------
  const drafts = { saved: new Map(), closed: {}, active: new Map(), timers: new Map() };
  async function loadDrafts() {
    const data = await api("/api/friend/drafts?" + query());
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
      const result = await api("/api/friend/draft", { method: "POST", body: { entry: state.entry,
        generation: state.generation, seq: ++state.seq, payload: state.payload, baseVersion: state.baseVersion } });
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
    try { await api("/api/friend/draft", { method: "POST", body: { entry, generation: state.generation, close: true } }); }
    catch { }
  }
  async function flushDrafts() {
    if (masked) return true;
    const results = await Promise.all([...drafts.active.values()].map(flushDraft));
    return results.every(Boolean);
  }

  // Five messages on each side of a cited one, read from the managed history (no model call).
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
    scope, validScope, byId, el, button, formatDate, formatDuration, toast, mask, api, query, ApiError,
    applyTheme, broadcast, setTask, openSheet, closeSheet, contextList,
    loadDrafts, draft, updateDraft, closeDraft, flushDrafts, savedDrafts: () => drafts.saved,
    get masked() { return masked; },
    get sheetOwner() { return sheetOwner; },
    on(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    emit(message) { for (const listener of listeners) try { listener(message); } catch { } },
  };
})();
