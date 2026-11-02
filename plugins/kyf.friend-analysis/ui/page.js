"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Scheme page router: the host window chooses the tab; this page renders it and reloads pages
// whose inputs a finished analysis step changed.
(function () {
  const A = window.FriendApp;
  const PAGES = { overview: "pageOverview", persona: "pagePersona", values: "pageValues", chat: "pageChat", trend: "pageTrend" };
  const MODULES = { overview: () => window.FriendOverview, persona: () => window.FriendPersona,
    values: () => window.FriendValues, chat: () => window.FriendChat, trend: () => window.FriendTrend };
  // Which pages show the result of each analysis step; only those reload when it finishes.
  const STEP_PAGES = { history: ["overview", "trend", "chat"], recent: ["overview"], archive: ["overview"],
    chat: ["chat"], persona: ["persona", "values", "overview"], overview: ["overview"] };
  let tab = null, restored = false;

  function show(next) {
    if (!PAGES[next] || A.masked) return;
    const changed = next !== tab;
    tab = next;
    for (const [key, id] of Object.entries(PAGES)) A.byId(id).hidden = key !== tab;
    if (changed) void MODULES[tab]().load();
  }
  function reload(keys) { if (tab && keys.includes(tab)) void MODULES[tab]().load(); }

  window.kyfHost.on(message => {
    if (message.type === "context") {
      document.body.classList.toggle("theme-light", message.context.theme === "light");
      window.FriendTrend.settingsChanged(message.context.trendSettings);
      show(message.context.tab);
      if (!restored) {
        restored = true;
        void A.loadDrafts().then(drafts => {
          if (![...drafts.keys()].some(entry => entry.startsWith("observation:"))) return;
          // A 三观分析 draft opens its tab when 性格分析 has none.
          void window.FriendPersona.restoreDrafts(drafts).then(async found => {
            if (await window.FriendValues.restoreDrafts(drafts) && !found) A.selectTab("values");
          });
        }).catch(() => { });
      }
    } else if (message.type === "event") {
      const event = message.event || {};
      if (event.type === "run-step") reload(STEP_PAGES[event.key] || []);
      else if (event.type === "run-finished" || event.type === "refresh") reload(Object.keys(PAGES));
      else if (event.type === "history") window.FriendTrend.historyChanged(event.readCount);
      else if (event.type === "masked") A.mask(event.message || "此窗口的资料已隐藏。");
      else if (event.type === "before-close")
        void A.flushDrafts().then(() => window.kyfHost.host("drafts.flushed")).catch(() => { });
    }
  });
})();
