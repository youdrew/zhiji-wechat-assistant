"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// External chat opens in a separate window fixed to the friend who opened it.
(function () {
  if (window.desktopHost?.isSettingsWindow) return;
  const pluginId = "kyf.external-chat-import";
  const popups = new Map();
  let enabled = false, listingRequest = 0, popupSequence = 0;
  const friendButton = document.getElementById("btnExternalImport");
  friendButton.hidden = true;
  function openSettings(tab) {
    if (typeof window.openAppSettings === "function") { void window.openAppSettings({ tab }); return; }
    document.getElementById("btnSettings")?.click();
    document.querySelector(`[data-tab="${tab}"]`)?.click();
  }
  function scopeFor(account = chatState.currentAccount, user = chatState.currentUser, name) {
    if (typeof account !== "string" || !account || typeof user !== "string" || !user) return null;
    const session = chatState.sessions.get(user);
    if (!session || session.isGroup || user.endsWith("@chatroom")) return null;
    const scope = { account, user, name: String(typeof name === "string" && name ? name : pluginFriendName(user)).slice(0, 128) };
    if (typeof session.externalFriendId === "string" && session.externalFriendId) scope.friendId = session.externalFriendId;
    return scope;
  }
  function updateFriendEntry(scope = scopeFor()) {
    friendButton.hidden = !enabled || !scope;
    friendButton.disabled = !enabled || !scope;
  }
  function eachPopup(callback) {
    for (const entry of popups.values()) if (!entry.window.closed) callback(entry);
  }
  function broadcast(payload) {
    eachPopup(entry => entry.window.postMessage(payload, location.origin));
  }
  function applyPlugins(listing) {
    if (!Array.isArray(listing?.plugins)) return;
    const plugin = listing.plugins.find(item => item?.id === pluginId);
    enabled = plugin?.enabled === true && !plugin.incompatible && !plugin.dependencyError;
    updateFriendEntry();
    const payload = { type: "plugins-changed", plugins: [{ id: pluginId, enabled }] };
    broadcast(payload);
    const notified = window.desktopHost?.notifyExternalImportWindows?.(payload);
    if (notified?.catch) void notified.catch(() => {});
  }
  async function refreshPlugins() {
    const request = ++listingRequest;
    try {
      const response = await fetch("/api/plugins", { cache: "no-store" });
      const listing = response.ok ? await response.json() : null;
      if (request === listingRequest && listing) applyPlugins(listing);
    } catch { /* Retain the last known state until service recovery or settings retries. */ }
  }
  function reportOpenFailure() {
    if (typeof toast === "function") toast("外部聊天窗口未能打开，请重试或允许此页面打开窗口");
  }
  async function open() {
    const scope = scopeFor();
    if (!enabled || !scope) return;
    const context = { ...scope, theme: settingsState.settings.theme };
    if (typeof window.desktopHost?.openExternalImportWindow === "function") {
      try { if (await window.desktopHost.openExternalImportWindow(context) !== true) reportOpenFailure(); }
      catch { reportOpenFailure(); }
      return;
    }
    const key = JSON.stringify([scope.account, scope.user]), existing = popups.get(key);
    if (existing && !existing.window.closed) { existing.window.focus(); return; }
    const params = new URLSearchParams(context);
    const popup = window.open(`/external-import.html?${params}`, `kyf-external-${++popupSequence}`,
      "popup=yes,width=1000,height=780,resizable=yes,scrollbars=yes");
    if (!popup) { reportOpenFailure(); return; }
    popups.set(key, { window: popup, scope });
    popup.focus();
  }
  function navigate(target, scope) {
    if (target === "chat") {
      switchView("chat");
      if (scope?.account === chatState.currentAccount && chatState.sessions.has(scope.user)) {
        if (typeof switchSession === "function") switchSession(scope.user);
      }
      return;
    }
    if (target === "models") document.getElementById("btnModelTraces")?.click();
    if (target === "plugins") openSettings("plugins");
    if (target === "model-settings") openSettings("models");
  }
  friendButton.addEventListener("click", () => { void open(); });
  window.ExternalImportHost = {
    onViewChanged() {},
    onSessionChanged(account, user, name) { updateFriendEntry(scopeFor(account, user, name)); },
    theme(theme) { broadcast({ type: "kyf-external:theme", theme }); },
  };
  window.desktopHost?.onExternalImportNavigate?.(event => { navigate(event.target, event.scope); });
  window.addEventListener("kyf-plugins-changed", event => {
    if (Array.isArray(event.detail?.plugins)) { listingRequest++; applyPlugins(event.detail); }
    else void refreshPlugins();
  });
  window.addEventListener("kyf-service-restored", () => { broadcast({ type: "service-restored" }); void refreshPlugins(); });
  window.addEventListener("message", event => {
    if (event.origin !== location.origin) return;
    const entry = [...popups.values()].find(value => event.source === value.window);
    if (!entry) return;
    if (entry.window.closed && !(event.data?.type === "kyf-external:navigate" && event.data.target === "chat")) return;
    if (event.data?.type === "kyf-external:ready") {
      entry.window.postMessage({ type: "kyf-external:theme", theme: settingsState.settings.theme }, location.origin);
    } else if (event.data?.type === "kyf-external:navigate") {
      navigate(event.data.target, entry.scope);
      window.focus();
    }
  });
  // Browser popups belong to this app session, just like native companion windows.
  window.addEventListener("pagehide", () => { eachPopup(entry => entry.window.close()); popups.clear(); });
  void refreshPlugins();
})();
