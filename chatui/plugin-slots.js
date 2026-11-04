"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Plugin contribution slots of the main window (added 2026-10-13): a nav entry per contributes.nav
// (its page fills #pluginNavView), contact-window buttons from contributes.chatHeader, and toolbar text buttons
// from contributes.chatToolbar (their pages open in one desktop window per friend).
// 自我剖析 opens in its own native window; browser-only use retains the nav-page fallback.
// Every page gets the context {account, user, name, theme,
// surface}; the host methods it may call are answered here. Enabling or disabling a plugin in 设置 → 插件
// adds or removes its entries without a reload.
// Modified 2026-10-18: keep completion acknowledgements without showing answer reminders on toolbar buttons.
// Modified 2026-10-22: a toolbar page opens in its own window per friend, so several friends' pages stay open.
// Modified 2026-10-25: the 自我剖析 nav icon is a fingerprint.
(function () {
  const SVG = "http://www.w3.org/2000/svg";
  // Host icon names a manifest may use; unknown names get the generic one.
  const ICONS = {
    assistant: [["rect", { x: "4", y: "7", width: "16", height: "13", rx: "3" }], ["path", { d: "M12 3v4M9 12v2m6-2v2M9 17h6M1 12h3m16 0h3" }]],
    self: [["path", { d: "M12 21a9 9 0 0 0 5-1.5M5 17a9 9 0 0 1-2-5.5 9 9 0 0 1 15.5-6.3M20.3 9a9 9 0 0 1 .7 3.5M8.5 19.5a12 12 0 0 0 1-4.5v-3a2.5 2.5 0 0 1 5 0v1M6 13.5V12a6 6 0 0 1 10.5-4M14.5 17a14 14 0 0 1-.8 3M12 12v3a10 10 0 0 1-1 4.5M17.5 11.5v2" }]],
    generic: [["rect", { x: "4", y: "4", width: "7", height: "7", rx: "1.5" }], ["rect", { x: "13", y: "4", width: "7", height: "7", rx: "1.5" }],
      ["rect", { x: "4", y: "13", width: "7", height: "7", rx: "1.5" }], ["rect", { x: "13", y: "13", width: "7", height: "7", rx: "1.5" }]],
  };
  const S = { nav: [], header: [], toolbar: [], frames: new Map(), view: "chat", account: "", user: "", name: "", cleared: new Set(),
    windows: [], serial: 0, options: {}, started: false,
    attention: new Map(), attentionSignature: "", attentionSerial: 0, attentionAcks: new Set() };
  const ATTENTION_MS = 3000;
  const byId = id => document.getElementById(id);
  const theme = () => document.body.classList.contains("theme-light") ? "light" : "dark";
  const viewKey = item => `plugin:${item.plugin}/${item.id}`;
  const context = surface => ({ account: S.account, user: S.user, name: S.name, theme: theme(), surface });
  const notify = message => {
    if (typeof S.options.toast === "function") S.options.toast(message);
    else if (typeof window.alert === "function") window.alert(message);
  };
  const failure = (code, message) => Object.assign(new Error(message), { code });
  // Added 2026-11-04: a chat window (init option navigation: false) has header and toolbar entries only.
  const navigable = () => S.options.navigation !== false;

  // -- contributions --------------------------------------------------------------------------------------
  function contributions(listing) {
    const nav = [], header = [], toolbar = [];
    for (const plugin of Array.isArray(listing?.plugins) ? listing.plugins : []) {
      if (!plugin || plugin.enabled !== true || plugin.incompatible || plugin.dependencyError || typeof plugin.id !== "string") continue;
      for (const item of Array.isArray(plugin.contributes?.nav) ? plugin.contributes.nav : [])
        if (item && typeof item.id === "string" && typeof item.page === "string")
          nav.push({ plugin: plugin.id, id: item.id, label: String(item.label || plugin.name || item.id), page: item.page, icon: item.icon });
      for (const item of Array.isArray(plugin.contributes?.chatHeader) ? plugin.contributes.chatHeader : [])
        if (item && typeof item.id === "string" && item.kind === "friend-window")
          header.push({ plugin: plugin.id, id: item.id, label: String(item.label || plugin.name || item.id) });
      for (const item of Array.isArray(plugin.contributes?.chatToolbar) ? plugin.contributes.chatToolbar : [])
        if (item && typeof item.id === "string" && typeof item.page === "string" && (item.kind || "panel") === "panel")
          toolbar.push({ plugin: plugin.id, id: item.id, label: String(item.label || plugin.name || item.id), page: item.page });
    }
    return { nav, header, toolbar };
  }
  function icon(name) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "plugin-nav-icon"); svg.setAttribute("aria-hidden", "true");
    for (const [tag, attributes] of ICONS[name] || ICONS.generic) {
      const shape = document.createElementNS(SVG, tag);
      for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, value);
      svg.appendChild(shape);
    }
    return svg;
  }
  function renderNav() {
    for (const node of [...document.querySelectorAll(".plugin-nav-item")]) node.remove();
    if (!navigable()) return;
    let anchor = byId("navChat");
    for (const item of S.nav) {
      const node = document.createElement("div");
      node.className = "nav-item plugin-nav-item" + (S.view === viewKey(item) ? " active" : "");
      node.title = item.label; node.setAttribute("aria-label", item.label); node.dataset.tab = viewKey(item);
      node.appendChild(icon(item.icon));
      node.addEventListener("click", () => openNav(item.plugin, item.id));
      anchor?.after(node);
      anchor = node;
    }
  }
  function renderToolbar() {
    const bar = byId("chatToolbar");
    for (const node of [...document.querySelectorAll(".plugin-toolbar-btn")]) node.remove();
    for (const item of S.toolbar) {
      const node = document.createElement("button");
      node.type = "button";
      node.className = "toolbar-text-btn plugin-toolbar-btn" + (hasWindow(S.user, item.plugin) ? " active" : "");
      node.textContent = item.label;
      node.dataset.plugin = item.plugin; node.dataset.contribution = item.id;
      node.addEventListener("click", () => { void openWindow(item.plugin, item.id); });
      bar?.appendChild(node);
    }
  }
  function hasFriendEntry(plugin, id) {
    return S.header.some(item => item.plugin === plugin && item.id === id);
  }
  function renderHeader() {
    for (const node of [...document.querySelectorAll(".plugin-header-btn")]) node.remove();
    const anchor = byId("btnChatHistory");
    for (const item of S.header) {
      const node = document.createElement("button");
      node.type = "button"; node.className = "chat-history-action plugin-header-btn";
      node.textContent = item.label; node.disabled = true; node.hidden = true;
      node.dataset.plugin = item.plugin; node.dataset.contribution = item.id;
      node.addEventListener("click", () => {
        if (hasFriendEntry(item.plugin, item.id)) void S.options.openFriendWindow?.(item.plugin, item.id);
      });
      anchor?.before(node);
    }
    S.options.updateFriendButton?.();
  }
  function apply(listing) {
    const next = contributions(listing);
    S.nav = next.nav; S.header = next.header; S.toolbar = next.toolbar;
    // Pages of removed contributions close; a page whose entry stays keeps its state.
    for (const [key, entry] of S.frames) {
      const item = S.nav.find(value => viewKey(value) === key);
      if (!item || item.page !== entry.page) { entry.handle?.destroy(); entry.node.remove(); S.frames.delete(key); }
    }
    renderNav(); renderHeader(); renderToolbar();
    if (navigable()) window.desktopHost?.syncSelfAnalysisWindow?.({ event: { type: "plugins-changed",
      enabled: S.nav.some(item => item.plugin === "kyf.self-analysis") } });
    void loadAttention();
    if (S.view.startsWith("plugin:") && !S.nav.some(item => viewKey(item) === S.view)) S.options.switchView?.("chat");
    retainWindows();
  }
  async function load() {
    const serial = ++S.serial;
    try {
      const response = await fetch("/api/plugins", { cache: "no-store" });
      const listing = response.ok ? await response.json() : null;
      if (serial === S.serial && listing) apply(listing);
    } catch { /* The slots stay as they are; settings or a later refresh retries. */ }
  }
  // -- host methods -----------------------------------------------------------------------------------------
  async function copy(params) {
    const value = params.text;
    if (typeof value !== "string" || !value || value.length > 1_000_000) throw failure("invalid-request", "复制内容不正确");
    // Modified 2026-10-14 (review): like the panel, a page copies only right after the user's gesture.
    if (window.navigator?.userActivation?.isActive === false) return { copied: false };
    let copied = false;
    try { if (typeof window.desktopHost?.copyDraft === "function") copied = await window.desktopHost.copyDraft(value) === true; } catch { }
    if (!copied && typeof window.navigator?.clipboard?.writeText === "function") {
      try { await window.navigator.clipboard.writeText(value); copied = true; } catch { }
    }
    return { copied };
  }
  function focusChat(_params, call) {
    if (call?.independent) {
      const scope = call.scope;
      if (!scope?.account || scope.account !== S.account || S.cleared.has(scope.account) || !scope.user)
        throw failure("context-changed", "当前账号已变化，请重新打开聊天助手");
      if (scope.user !== S.user) {
        if (typeof S.options.focusChat !== "function" || S.options.focusChat(scope) !== true)
          throw failure("context-changed", "原聊天暂时不可用，请在主窗口重新选择");
      }
    }
    if (S.view !== "chat") S.options.switchView?.("chat");
    byId("chatInput")?.focus?.();
    return { ok: true };
  }
  function toast(params) { notify(String(params.message || "").slice(0, 120)); return { ok: true }; }
  function currentAccount(params, scope) {
    if (!scope?.account || params.account !== scope.account || scope.account !== S.account || S.cleared.has(scope.account))
      throw failure("context-changed", "当前账号已变化，请重新打开自我剖析");
    return scope.account;
  }
  async function selfProfile(params, scope, save = false) {
    const account = currentAccount(params, scope);
    const url = "/api/account/self-profile";
    const response = await fetch(save ? url : url + "?" + new URLSearchParams({ account }), {
      cache: "no-store", ...(save ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account, background: params.background, style: params.style,
          expectedVersion: params.expectedVersion }) } : {}),
    });
    const data = await response.json();
    // An old response must not fill a newly selected account's form.
    currentAccount(params, scope);
    if (!response.ok) throw failure(data?.error || "profile-error", data?.message || "自己的资料读取或保存失败");
    if (save) {
      try {
        const channel = new BroadcastChannel("kyf-friend-analysis");
        channel.postMessage({ type: "account-profile-changed", account, from: "self-analysis" }); channel.close();
      } catch { }
    }
    return data;
  }
  function openSettings(params, scope, plugin) {
    if (params.account) currentAccount(params, scope);
    if (!["model", "source", "plugin"].includes(params.section)) throw failure("invalid-request", "设置入口不正确");
    // "plugin" opens the calling plugin's own settings, never another plugin's.
    const target = params.section === "plugin" ? { tab: "plugins", plugin } :
      { tab: params.section === "model" ? "models" : "general", ...(params.section === "source" ? { section: "source" } : {}) };
    if (window.openAppSettings) {
      void window.openAppSettings(target);
      return { ok: true };
    }
    byId("btnSettings")?.click();
    document.querySelector(`.settings-tab-btn[data-tab="${target.tab}"]`)?.click();
    if (target.plugin) void window.PluginSettings?.openSettings(target.plugin);
    else byId(params.section === "model" ? "apiModelSettings" : "inputDataRoot")?.scrollIntoView?.({ block: "center" });
    return { ok: true };
  }
  async function modelStatus() {
    // Read the saved Core selection only. Never probe the provider or expose its profile to a page.
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/model-source", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error();
      const value = await response.json();
      if (value && value.mode === "none" && value.api === null) return { configured: false };
      if (value && value.mode === "api" && value.api && !Array.isArray(value.api) &&
          typeof value.api.model === "string" && value.api.model.trim()) return { configured: true };
      return { configured: null };
    } catch {
      throw failure("model-status-unavailable", "已保存的模型配置暂时无法确认，请在核心模型设置中查看");
    } finally { clearTimeout(timer); }
  }
  function navHost(plugin) {
    return {
      "clipboard.copy": copy, "view.focusChat": focusChat, toast,
      ...(plugin === "kyf.self-analysis" ? {
        "settings.open": (params, scope) => openSettings(params, scope, plugin),
        "model.status": modelStatus,
        "self-profile.read": (params, scope) => selfProfile(params, scope),
        "self-profile.save": (params, scope) => selfProfile(params, scope, true),
      } : {}),
      // The desktop host shows the picker; the page receives a one-time grant for its plugin, never the path.
      async "file.pick"(params) {
        if (!["file", "directory"].includes(params.kind)) throw failure("invalid-request", "选择类型不正确");
        if (typeof window.desktopHost?.choosePluginFile !== "function") throw failure("unknown-method", "请在桌面客户端选择文件");
        const value = await window.desktopHost.choosePluginFile(plugin, params.kind);
        if (value && typeof value.error === "string") throw failure("file-grant-failed", value.error);
        return value && typeof value.token === "string" ? { token: value.token, label: String(value.label || "") } : null;
      },
    };
  }
  // Methods a toolbar window's page may reach in this window; it copies and closes in the desktop host itself.
  const PANEL_HOST = { "view.focusChat": focusChat, toast, "attention.seen": markSeen };

  // -- nav pages --------------------------------------------------------------------------------------------
  function openNav(plugin, id) {
    if (!navigable()) return;
    if (plugin === "kyf.self-analysis" && typeof window.desktopHost?.openSelfAnalysisWindow === "function") {
      if (!S.nav.some(item => item.plugin === plugin && item.id === id)) return;
      // Invoke synchronously in the click's activation; opening a window must not replace the main chat.
      return window.desktopHost.openSelfAnalysisWindow({ account: S.account, theme: theme() })
        .then(opened => { if (opened !== true) notify("自我剖析窗口未能打开，请重试"); })
        .catch(() => notify("自我剖析窗口未能打开，请重试"));
    }
    S.options.switchView?.(`plugin:${plugin}/${id}`);
  }
  function showNav(key) {
    const view = byId("pluginNavView");
    const item = S.nav.find(value => viewKey(value) === key);
    for (const [frameKey, entry] of S.frames) entry.node.hidden = frameKey !== key;
    if (!view || !item) return;
    let entry = S.frames.get(key);
    if (entry?.handle) { entry.handle.post(context("nav")); entry.handle.event({ type: "shown" }); return; }
    if (!entry) {
      const node = document.createElement("div");
      node.className = "plugin-nav-page"; node.dataset.view = key;
      view.appendChild(node);
      entry = { node, page: item.page, handle: null };
      S.frames.set(key, entry);
    }
    loadNav(item, entry);
  }
  function loadNav(item, entry) {
    entry.handle?.destroy();
    const handle = window.KyfFrames.create({ container: entry.node, plugin: item.plugin, page: item.page, title: item.label,
      context: context("nav"), host: navHost(item.plugin),
      onTimeout() {
        if (entry.handle !== handle) return;
        handle.destroy(); entry.handle = null;
        const note = document.createElement("div"), retry = document.createElement("button");
        note.className = "plugin-nav-empty";
        note.textContent = "页面没有响应。主聊天不受影响，可以重新载入。";
        retry.type = "button"; retry.textContent = "重新载入";
        retry.addEventListener("click", () => loadNav(item, entry));
        note.appendChild(document.createElement("br")); note.appendChild(retry);
        entry.node.replaceChildren(note);
      } });
    entry.handle = handle;
  }
  function onViewChanged(view) {
    S.view = typeof view === "string" ? view : "chat";
    const plugin = S.view.startsWith("plugin:");
    byId("pluginNavView")?.classList.toggle("active", plugin);
    for (const node of document.querySelectorAll(".plugin-nav-item")) node.classList.toggle("active", node.dataset.tab === S.view);
    if (plugin) showNav(S.view);
  }

  // -- toolbar windows (2026-10-22, replaces the docked panel) -------------------------------------------------
  // The desktop host reports each open window: {plugin, account, user, visible}.
  const hasWindow = (user, plugin, visible = false) => !!user && S.windows.some(item => item.plugin === plugin &&
    item.account === S.account && item.user === user && (!visible || item.visible));
  function markToolbar() {
    for (const node of document.querySelectorAll(".plugin-toolbar-btn"))
      node.classList.toggle("active", hasWindow(S.user, node.dataset.plugin));
  }
  function setWindows(list) {
    S.windows = (Array.isArray(list) ? list : []).filter(item => item && typeof item.plugin === "string" &&
      typeof item.account === "string" && typeof item.user === "string").map(item => ({
      plugin: item.plugin, account: item.account, user: item.user, visible: item.visible === true }));
    markToolbar();
    attentionChanged();
  }
  function retainWindows() {
    window.desktopHost?.retainPluginWindows?.({ targets: S.toolbar.map(item => ({ plugin: item.plugin, page: item.page })),
      account: S.account });
  }

  // -- attention (added 2026-10-15) ----------------------------------------------------------------------------
  // Core retains completed answers until a friend's visible window acknowledges the displayed tasks.
  const showing = (user, plugin) => hasWindow(user, plugin, true);
  function attentionFor(user) { return S.attention.get(user) || []; }
  function attentionChanged() {
    // Opening a window is not proof its current discussion has loaded. Ask the page which completed tasks it displays.
    for (const [user, items] of S.attention) for (const item of items) if (showing(user, item.plugin))
      sendWindow(item.plugin, user, { type: "attention-check", account: S.account, user, tasks: item.tasks });
    S.options.onAttention?.();
  }
  async function markSeen(params, call) {
    const plugin = call?.plugin, { account, user, taskIds } = params;
    // Only the friend's own visible window acknowledges that friend's answers, whichever chat the main window shows.
    if (account !== S.account || S.cleared.has(account) || call?.scope?.account !== account || call?.scope?.user !== user ||
      !showing(user, plugin) || !Array.isArray(taskIds) ||
      !taskIds.length || !taskIds.every(id => typeof id === "string" && id.length > 0 && id.length <= 240)) return { seen: false };
    const item = (S.attention.get(user) || []).find(item => item.plugin === plugin);
    const observed = new Set(item?.taskIds || []), ids = [...new Set(taskIds)].filter(id => observed.has(id));
    if (!ids.length) return { seen: false };
    const key = JSON.stringify([plugin, account, user, ids.slice().sort()]);
    if (S.attentionAcks.has(key)) return { seen: false };
    S.attentionAcks.add(key);
    try {
      const response = await fetch("/api/attention/seen", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plugin, account, user, taskIds: ids }) });
      const result = response.ok ? await response.json() : null;
      if (!Number.isFinite(result?.seenAt)) return { seen: false };
      if (account === S.account) {
        const acknowledged = new Set(ids);
        const remaining = (S.attention.get(user) || []).map(entry => entry.plugin !== plugin ? entry : {
          ...entry, taskIds: entry.taskIds.filter(id => !acknowledged.has(id)), tasks: entry.tasks.filter(task => !acknowledged.has(task.id))
        }).filter(entry => entry.taskIds.length);
        if (remaining.length) S.attention.set(user, remaining); else S.attention.delete(user);
        S.attentionSignature = JSON.stringify([...S.attention]);
        S.options.onAttention?.();
      }
      return { seen: true };
    } catch { return { seen: false }; }
    finally { S.attentionAcks.delete(key); }
  }
  async function loadAttention() {
    const account = S.account, serial = ++S.attentionSerial;
    if (!account || S.cleared.has(account) || !S.toolbar.length) {
      if (S.attention.size) { S.attention = new Map(); S.attentionSignature = ""; attentionChanged(); }
      return;
    }
    try {
      const response = await fetch("/api/attention?" + new URLSearchParams({ account }), { cache: "no-store" });
      const data = response.ok ? await response.json() : null;
      if (serial !== S.attentionSerial || account !== S.account || !Array.isArray(data?.items)) return;
      const next = new Map();
      for (const item of data.items) {
        if (!item || typeof item.user !== "string" || !item.user || !S.toolbar.some(entry => entry.plugin === item.plugin)) continue;
        if (!Array.isArray(item.taskIds) || !item.taskIds.length || !item.taskIds.every(id => typeof id === "string" && id.length > 0 && id.length <= 240)) continue;
        const label = S.toolbar.find(entry => entry.plugin === item.plugin).label;
        const taskIds = [...new Set(item.taskIds)].sort();
        const tasks = (Array.isArray(item.tasks) ? item.tasks : []).filter(task => task && taskIds.includes(task.id) &&
          typeof task.runId === "string" && typeof task.threadId === "string");
        next.set(item.user, [...(next.get(item.user) || []), { plugin: item.plugin, label,
          state: item.state === "failed" ? "failed" : "done", taskIds, tasks }]);
      }
      const signature = JSON.stringify([...next]);
      if (signature === S.attentionSignature) { attentionChanged(); return; }
      S.attention = next; S.attentionSignature = signature;
      attentionChanged();
    } catch { /* Keep the completion state; the next round retries. */ }
  }
  function sendWindow(plugin, user, event) {
    window.desktopHost?.sendPluginWindowEvent?.({ target: { plugin, account: S.account, user }, event });
  }
  // The current friend's window of a toolbar entry: opened, or brought to the front when it is already open.
  async function openWindow(plugin, id) {
    const item = S.toolbar.find(value => value.plugin === plugin && value.id === id);
    if (!item) return false;
    if (typeof window.desktopHost?.openPluginWindow !== "function") { notify(`${item.label}需要在桌面客户端中使用`); return false; }
    if (!S.account || !S.user || S.cleared.has(S.account)) { notify(`请先打开一个聊天，再使用${item.label}`); return false; }
    try {
      const result = await window.desktopHost.openPluginWindow({ plugin, page: item.page, account: S.account, user: S.user,
        name: S.name, label: item.label, theme: theme() });
      if (typeof result?.error === "string" && result.error) throw new Error(result.error);
      if (result?.opened !== true) throw new Error(`${item.label}打开失败`);
      return true;
    } catch (error) {
      notify(error.message || `${item.label}打开失败`); return false;
    }
  }
  // 分析与建议 → 继续聊这句: open the friend's chat window and hand it a quote of the source message as a draft.
  async function continueFromMessage(value) {
    const item = S.toolbar[0];
    if (!item || !value || value.account !== S.account || value.user !== S.user || !S.account || !S.user) return false;
    if (!await openWindow(item.plugin, item.id) || value.account !== S.account) return false;
    sendWindow(item.plugin, value.user, { type: "quote", account: value.account, user: value.user,
      messageId: String(value.messageId || "").slice(0, 4096), text: String(value.text || "").slice(0, 16000),
      // Added 2026-10-15 (message right-click menu): who wrote the message, so the draft names them.
      ...(["self", "other"].includes(value.side) ? { side: value.side } : {}),
      ...(typeof value.sender === "string" && value.sender ? { sender: value.sender.slice(0, 128) } : {}) });
    return true;
  }
  async function servePanelCall(call) {
    if (!call || typeof call.id !== "string" || typeof window.desktopHost?.replyPluginPanelHostCall !== "function") return;
    const handler = Object.prototype.hasOwnProperty.call(PANEL_HOST, call.method) ? PANEL_HOST[call.method] : null;
    try {
      if (!handler) throw failure("unknown-method", "宿主不支持该操作");
      window.desktopHost.replyPluginPanelHostCall(call.id, { result: await handler(call.params && typeof call.params === "object" ? call.params : {}, call) ?? null });
    } catch (error) {
      window.desktopHost.replyPluginPanelHostCall(call.id, { error: { code: error?.code || "error", message: error?.message || "请求失败" } });
    }
  }
  async function serveSelfAnalysisCall(call) {
    if (!call || typeof call.id !== "string" || typeof window.desktopHost?.replySelfAnalysisHostCall !== "function") return;
    const handlers = navHost("kyf.self-analysis");
    const allowed = ["self-profile.read", "self-profile.save", "model.status", "settings.open", "view.focusChat", "toast"];
    try {
      if (!S.nav.some(item => item.plugin === "kyf.self-analysis")) throw failure("plugin-disabled", "自我剖析已停用");
      if (call.scope?.account !== S.account || S.cleared.has(call.scope?.account))
        throw failure("context-changed", "当前账号已变化，请重新打开自我剖析");
      if (!allowed.includes(call.method)) throw failure("unknown-method", "宿主不支持该操作");
      const params = call.params && typeof call.params === "object" ? call.params : {};
      const result = await handlers[call.method](params, call.scope);
      if (call.scope?.account !== S.account || S.cleared.has(call.scope?.account))
        throw failure("context-changed", "当前账号已变化，请重新打开自我剖析");
      window.desktopHost.replySelfAnalysisHostCall(call.id, { result: result ?? null });
    } catch (error) {
      window.desktopHost.replySelfAnalysisHostCall(call.id, { error: { code: error?.code || "error", message: error?.message || "请求失败" } });
    }
  }

  // -- context ----------------------------------------------------------------------------------------------
  function pushContext() {
    for (const entry of S.frames.values()) entry.handle?.post(context("nav"));
    if (navigable()) window.desktopHost?.syncSelfAnalysisWindow?.({ context: { account: S.account, theme: theme() } });
  }
  function onSessionChanged(account, user, name) {
    account = typeof account === "string" ? account : ""; user = typeof user === "string" ? user : "";
    name = typeof name === "string" && name ? name.slice(0, 128) : user;
    if (S.account === account && S.user === user && S.name === name) return;
    const accountChanged = S.account !== account;
    S.account = account; S.user = user; S.name = name;
    pushContext();
    // Windows stay with their friend; another account's windows close.
    if (accountChanged && account) retainWindows();
    markToolbar();
    if (accountChanged) { S.attention = new Map(); S.attentionSignature = ""; void loadAttention(); }
    attentionChanged();
  }
  // Pages drop what they hold for the cleared account; Core has already stopped its plugin work.
  function onAccountCleared(account) {
    if (typeof account !== "string" || !account) return;
    S.cleared.add(account);
    for (const entry of S.frames.values()) { entry.handle?.forget(account); entry.handle?.event({ type: "account-cleared", account }); }
    window.desktopHost?.forgetPluginWindows?.(account);
    if (navigable()) window.desktopHost?.syncSelfAnalysisWindow?.({ event: { type: "account-cleared", account } });
    if (S.account === account) onSessionChanged("", "", "");
  }

  function init(options = {}) {
    S.options = options;
    if (S.started) return;
    S.started = true;
    window.addEventListener("kyf-plugins-changed", event => {
      if (event?.detail && typeof event.detail === "object") { S.serial++; apply(event.detail); } else void load();
    });
    window.addEventListener("kyf-service-restored", () => { void load(); });
    window.desktopHost?.onPluginPanelHostCall?.(call => { void servePanelCall(call); });
    window.desktopHost?.onPluginPanelState?.(state => setWindows(state?.windows));
    if (navigable()) window.desktopHost?.onSelfAnalysisHostCall?.(call => { void serveSelfAnalysisCall(call); });
    // A visible toolbar window keeps its reminders current while the main window is hidden.
    setInterval(() => {
      if (!document.hidden || S.windows.some(item => item.visible)) void loadAttention();
    }, ATTENTION_MS);
    if (typeof window.MutationObserver === "function") {
      let last = theme();
      new window.MutationObserver(() => { if (theme() !== last) { last = theme(); pushContext(); } })
        .observe(document.body, { attributes: true, attributeFilter: ["class"] });
    }
    void load();
  }
  // Whether 继续聊这句 has a chat window to open (added 2026-10-14 in review: no dead button without one).
  const canContinue = () => S.toolbar.length > 0;
  window.PluginSlots = Object.freeze({ init, load, apply, openNav, onViewChanged, onSessionChanged, onAccountCleared,
    hasFriendEntry, openWindow, continueFromMessage, canContinue, attentionFor, loadAttention, get state() { return S; } });
})();
