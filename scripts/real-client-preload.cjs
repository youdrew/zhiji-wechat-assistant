const { contextBridge, ipcRenderer } = require("electron");
// Modified by Know Your Friends, 2026-10-09: expose the shared native portrait extension.
// Modified by Know Your Friends, 2026-10-10: gesture-gated independent image previews.
// Modified by Know Your Friends, 2026-10-10: open 知己分析 windows and receive verified chat locations.
// Modified 2026-10-13: the docked panel hosts chat-toolbar plugin pages; host-picked files become Core grants.
// Modified 2026-10-22: chat-toolbar plugin pages open in one independent window per friend instead of the dock.
const appUpdatesEnabled = process.argv?.includes("--kyf-app-updates-enabled") === true;
const isSettingsWindow = process.argv?.includes("--kyf-settings-window") === true;
const updateValidationMode = !isSettingsWindow && appUpdatesEnabled && process.env.KYF_UPDATE_VALIDATE === "1";
const updateFinalReadyMode = !isSettingsWindow && appUpdatesEnabled && !updateValidationMode &&
  typeof process.env.KYF_UPDATE_FINAL_READY_FILE === "string" &&
  typeof process.env.KYF_UPDATE_FINAL_READY_NONCE === "string";

ipcRenderer.on("real-client:bridge-restored", () => {
  window.dispatchEvent(new Event("kyf-service-restored"));
});
ipcRenderer.on("real-client:update-state", (_event, state) => {
  if (appUpdatesEnabled && state && typeof state === "object")
    window.dispatchEvent(new CustomEvent("kyf-update-state", { detail: state }));
});

const DOC_URLS = new Set([
  "https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/",
  "https://www.themyersbriggs.com/en-US/Products-and-Services/Myers-Briggs",
  "https://github.com/youdrew",
  "https://github.com/youdrew/zhiji-wechat-assistant",
  "https://eugenepage.com/",
  "mailto:youdrewxuan@icloud.com",
  "https://github.com/tswawa",
  "https://github.com/tswawa/WechatVibe",
  "https://github.com/youdrew/zhiji-wechat-assistant/releases",
]);

document.addEventListener("click", (event) => {
  if (!event.isTrusted || event.button !== 0 || !navigator.userActivation.isActive || window.top !== window) return;
  const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!anchor) return;
  let target;
  try {
    target = new URL(anchor.href);
  } catch (_) {
    return;
  }
  if (!DOC_URLS.has(target.href) || target.username || target.password) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  ipcRenderer.send("real-client:open-doc", target.href, true, true);
}, true);

// Legacy callers can reserve one click-bound text across asynchronous work.
// Direct copies use the current user activation without a reservation.
let pendingCopy = null, copySequence = 0;
const copyableText = value => typeof value === "string" && !!value.trim() && value.length <= 1_000_000;

contextBridge.exposeInMainWorld("desktopHost", Object.freeze({
  platform: "win32",
  appUpdatesEnabled,
  isSettingsWindow,
  openSettingsWindow(target = {}) {
    if (window.top !== window || isSettingsWindow || !target || typeof target !== "object") return Promise.resolve(false);
    return ipcRenderer.invoke("settings-window:open", target);
  },
  getSettingsTarget() {
    if (window.top !== window || !isSettingsWindow) return Promise.resolve(null);
    return ipcRenderer.invoke("settings-window:target");
  },
  onSettingsTarget(listener) {
    if (window.top !== window || !isSettingsWindow || typeof listener !== "function") return () => {};
    const handler = (_event, target) => { if (target && typeof target === "object") listener(target); };
    ipcRenderer.on("settings-window:target", handler);
    return () => ipcRenderer.removeListener("settings-window:target", handler);
  },
  onSettingsCloseRequested(listener) {
    if (window.top !== window || !isSettingsWindow || typeof listener !== "function") return () => {};
    const handler = () => listener();
    ipcRenderer.on("settings-window:request-close", handler);
    return () => ipcRenderer.removeListener("settings-window:request-close", handler);
  },
  closeSettingsWindow() {
    if (window.top !== window || !isSettingsWindow) return Promise.resolve(false);
    return ipcRenderer.invoke("settings-window:close");
  },
  openSettingsMainNav(plugin, nav) {
    if (window.top !== window || !isSettingsWindow || typeof plugin !== "string" || typeof nav !== "string") return Promise.resolve(false);
    return ipcRenderer.invoke("settings-window:main-nav", plugin, nav);
  },
  onSettingsMainNav(listener) {
    if (window.top !== window || isSettingsWindow || typeof listener !== "function") return () => {};
    const handler = (_event, target) => { if (target && typeof target.plugin === "string" && typeof target.nav === "string") listener(target); };
    ipcRenderer.on("settings-window:main-nav", handler);
    return () => ipcRenderer.removeListener("settings-window:main-nav", handler);
  },
  openModelTracesWindow(context) {
    if (window.top !== window || !navigator.userActivation.isActive || !context || typeof context !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("model-traces-window:open", context, true);
  },
  syncModelTracesWindow(context) {
    if (!isSettingsWindow && window.top === window && context && typeof context === "object") ipcRenderer.send("model-traces-window:sync", context);
  },
  modelTracesRequest(operation, args = {}) {
    if (window.top !== window || typeof operation !== "string") return Promise.resolve({ ok: false, error: "trace-forbidden" });
    return ipcRenderer.invoke("model-traces:request", operation, args);
  },
  onModelTracesInvalidated(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, value) => listener({ deleteVersion: Number.isSafeInteger(value?.deleteVersion) ? value.deleteVersion : null });
    ipcRenderer.on("model-traces:invalidated", handler);
    return () => ipcRenderer.removeListener("model-traces:invalidated", handler);
  },
  openImagePreview(scope) {
    if (window.top !== window || !navigator.userActivation.isActive) return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:open-image-preview", scope, true);
  },
  openChatLink(value) {
    if (window.top !== window || isSettingsWindow || !navigator.userActivation.isActive ||
        typeof value !== "string" || !value || value.length > 8192 ||
        /[\s\u0000-\u001f\u007f\\]/u.test(value) || !/^https?:\/\//i.test(value)) return Promise.resolve(false);
    let target;
    try { target = new URL(value); } catch { return Promise.resolve(false); }
    if (!["https:", "http:"].includes(target.protocol) || !target.hostname || target.username || target.password)
      return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:open-chat-link", target.href, true);
  },
  closeImagePreviews() {
    if (window.top === window) ipcRenderer.send("real-client:close-image-previews");
  },
  openFriendWindow(scope) {
    if (window.top !== window || !navigator.userActivation.isActive || !scope || typeof scope !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("friend:open", scope, true);
  },
  openSelfAnalysisWindow(context) {
    if (window.top !== window || isSettingsWindow || !navigator.userActivation.isActive || !context || typeof context !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("self-analysis:open", context, true);
  },
  syncSelfAnalysisWindow(message) {
    if (window.top === window && !isSettingsWindow && message && typeof message === "object")
      ipcRenderer.send("self-analysis:sync", message);
  },
  onSelfAnalysisHostCall(listener) {
    if (window.top !== window || isSettingsWindow || typeof listener !== "function") return () => {};
    const handler = (_event, call) => { if (call && typeof call === "object") listener(call); };
    ipcRenderer.on("self-analysis:host-call", handler);
    return () => ipcRenderer.removeListener("self-analysis:host-call", handler);
  },
  replySelfAnalysisHostCall(id, reply) {
    if (window.top === window && !isSettingsWindow && typeof id === "string" && reply && typeof reply === "object")
      ipcRenderer.send("self-analysis:host-result", id, reply);
  },
  openHistoryWindow(scope) {
    if (window.top !== window || !navigator.userActivation.isActive || !scope || typeof scope !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("history:open", scope, true);
  },
  openExternalImportWindow(scope) {
    if (window.top !== window || isSettingsWindow || !navigator.userActivation.isActive || !scope || typeof scope !== "object")
      return Promise.resolve(false);
    return ipcRenderer.invoke("external-import:open", scope, true);
  },
  notifyExternalImportWindows(event) {
    if (window.top !== window || isSettingsWindow || !event || event.type !== "plugins-changed") return Promise.resolve(false);
    return ipcRenderer.invoke("external-import:notify", event);
  },
  onExternalImportNavigate(listener) {
    if (window.top !== window || isSettingsWindow || typeof listener !== "function") return () => {};
    const handler = (_event, value) => {
      if (value && ["chat", "model-settings", "models", "plugins"].includes(value.target) && value.scope && typeof value.scope === "object")
        listener(value);
    };
    ipcRenderer.on("external-import:navigate", handler);
    return () => ipcRenderer.removeListener("external-import:navigate", handler);
  },
  onHistoryLocate(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, target) => { if (target && typeof target === "object") listener(target); };
    ipcRenderer.on("history:locate", handler);
    return () => ipcRenderer.removeListener("history:locate", handler);
  },
  onFriendLocate(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, target) => { if (target && typeof target === "object") listener(target); };
    ipcRenderer.on("friend:locate", handler);
    return () => ipcRenderer.removeListener("friend:locate", handler);
  },
  // One window per plugin page and friend: {plugin, page, account, user, name, label, theme} -> {opened}.
  openPluginWindow(target) {
    if (window.top !== window || isSettingsWindow || !target || typeof target !== "object") return Promise.resolve({ opened: false });
    const { plugin, page, account, user, name, label, theme } = target;
    return ipcRenderer.invoke("plugin-panel:open", { plugin, page, account, user, name, label, theme });
  },
  sendPluginWindowEvent(message) {
    if (window.top !== window || isSettingsWindow || !message || typeof message !== "object") return;
    ipcRenderer.send("plugin-panel:event", message);
  },
  retainPluginWindows(value) {
    if (window.top !== window || isSettingsWindow || !value || typeof value !== "object") return;
    ipcRenderer.send("plugin-panel:retain", value);
  },
  forgetPluginWindows(account) {
    if (window.top === window && !isSettingsWindow && typeof account === "string") ipcRenderer.send("plugin-panel:forget", account);
  },
  onPluginPanelHostCall(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, call) => { if (call && typeof call === "object") listener(call); };
    ipcRenderer.on("plugin-panel:host-call", handler);
    return () => ipcRenderer.removeListener("plugin-panel:host-call", handler);
  },
  replyPluginPanelHostCall(id, reply) {
    if (window.top !== window || typeof id !== "string" || !reply || typeof reply !== "object") return;
    ipcRenderer.send("plugin-panel:host-result", id, reply);
  },
  onPluginPanelState(listener) {
    if (window.top !== window || typeof listener !== "function") return () => {};
    const handler = (_event, state) => { if (state && Array.isArray(state.windows)) listener({ windows: state.windows }); };
    ipcRenderer.on("plugin-panel:state", handler);
    return () => ipcRenderer.removeListener("plugin-panel:state", handler);
  },
  choosePluginFile(plugin, kind) {
    if (window.top !== window || typeof plugin !== "string" || !["file", "directory"].includes(kind) || !navigator.userActivation.isActive)
      return Promise.resolve(null);
    return ipcRenderer.invoke("real-client:choose-plugin-file", plugin, kind, true);
  },
  updateValidationMode,
  updateFinalReadyMode,
  reportUiReady() {
    if (!appUpdatesEnabled || (!updateValidationMode && !updateFinalReadyMode) || window.top !== window)
      return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:update-ui-ready");
  },
  setTheme(theme) {
    if (theme !== "dark" && theme !== "light") return false;
    ipcRenderer.send("real-client:set-theme", theme);
    return true;
  },
  reserveCopyDraft(value) {
    pendingCopy = null;
    if (!copyableText(value) || !navigator.userActivation.isActive || window.top !== window) return null;
    pendingCopy = { token: ++copySequence, value, expires: Date.now() + 60_000 };
    return pendingCopy.token;
  },
  releaseCopyDraft(token) {
    if (pendingCopy?.token === token) pendingCopy = null;
  },
  copyDraft(value, token) {
    const reservation = pendingCopy;
    pendingCopy = null;
    const activated = token === undefined ? navigator.userActivation.isActive :
      reservation && reservation.token === token && reservation.value === value && Date.now() < reservation.expires;
    if (!copyableText(value) || !activated || window.top !== window) return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:copy-draft", value);
  },
  exitApp() {
    if (window.top !== window) return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:exit-app");
  },
  getAppVersion() {
    if (window.top !== window) return Promise.resolve(null);
    return ipcRenderer.invoke("real-client:app-version");
  },
  chooseDataRoot() {
    if (window.top !== window || !navigator.userActivation.isActive) return Promise.resolve(null);
    return ipcRenderer.invoke("real-client:data-root-choose-directory");
  },
  checkForUpdates() {
    if (!appUpdatesEnabled) return Promise.resolve({ status: "disabled" });
    if (window.top !== window) return Promise.resolve({ status: "blocked" });
    return ipcRenderer.invoke("real-client:check-updates");
  },
  getUpdateState() {
    if (!appUpdatesEnabled) return Promise.resolve({ phase: "disabled" });
    if (window.top !== window) return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:update-state");
  },
  beginUpdate() {
    if (!appUpdatesEnabled) return Promise.resolve({ phase: "disabled" });
    if (window.top !== window || !navigator.userActivation.isActive)
      return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:begin-update");
  },
  rollbackUpdate() {
    if (!appUpdatesEnabled) return Promise.resolve({ phase: "disabled" });
    if (window.top !== window || !navigator.userActivation.isActive)
      return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:rollback-update");
  },
}));
