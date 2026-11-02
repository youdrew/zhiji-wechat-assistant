"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
(function () {
  if (window.desktopHost?.isSettingsWindow) {
    document.documentElement.classList.add("settings-window");
    document.title = "设置 · 知己微信助手";
    // Reuse the settings form and controllers without displaying the chat shell.
    document.body.append(document.getElementById("settingsModal"), document.getElementById("toastMsg"));
    document.getElementById("appWindow").setAttribute("inert", "");
  }
  // Only invalidations cross windows; model credentials and form drafts never do.
  const paths = new Set(["/api/plugins", "/api/model-source/activate", "/api/model-source/clear-key",
    "/api/model-source/models/save", "/api/model-source/models/delete", "/api/model-source/roles",
    "/api/model-source/local-judge/prepare", "/api/model-source/local-judge/cancel", "/api/model-source/local-judge/delete",
    "/api/conversation-selection", "/api/data-root", "/api/data-root/clear", "/api/accounts"]);
  let channel = null;
  try { if (typeof BroadcastChannel === "function") channel = new BroadcastChannel("kyf-settings-changed"); } catch { }
  window.SettingsSync = Object.freeze({
    changed(path) { if (paths.has(path)) channel?.postMessage(path); },
    accountCleared(accountId) { channel?.postMessage({ path: "/api/accounts", accountId }); },
    listen(listener) {
      if (channel) channel.onmessage = event => {
        if (paths.has(event.data)) listener(event.data);
        else if (event.data?.path === "/api/accounts" && typeof event.data.accountId === "string" &&
            /^[a-f0-9]{64}$/.test(event.data.accountId)) listener(event.data.path, event.data.accountId);
      };
    },
  });
  window.addEventListener("pagehide", () => channel?.close());
})();
