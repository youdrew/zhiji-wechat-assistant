"use strict";
// Modified by Know Your Friends, 2026-10-19 (Apache-2.0): isolated drafts and explicit WeChat check/send actions.
// Modified by Know Your Friends, 2026-10-16 (Apache-2.0): configurable model generation deadlines.
// Modified by Know Your Friends, 2026-10-09: keep chat visible beside the portrait panel.
// Modified by Know Your Friends, 2026-10-10: 知己分析 opens per-contact windows; hidden in group chats.
// Modified by Know Your Friends, 2026-10-10 (Apache-2.0): scoped inline message analysis.
// Modified by Know Your Friends, 2026-10-11: explicit startup state and active-chat-only preloading.
// Modified by Know Your Friends, 2026-10-11 (Apache-2.0): actionable inline WeChat login guidance.
// Modified by Know Your Friends, 2026-10-13 (Apache-2.0): account-scoped chat context menu.
// Modified by Know Your Friends, 2026-10-13 (Apache-2.0): plugin architecture; the WechatVibe label,
// portrait, background-analysis and local-model UI is retired.
// Modified by Know Your Friends, 2026-10-14 (Apache-2.0): blank context budgets use Core's default on save.
const chatState = window.ViewState.create("chat");
const settingsState = window.ViewState.create("settings");
const byId = id => document.getElementById(id);
const element = (tag, className = "", text) => {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const text = (id, value) => { byId(id).textContent = value == null ? "" : String(value); };
// Short status notices for chat-list actions; an empty message has nothing to show.
const notifyStatus = (message = "") => { if (message) toast(message); };
const svgIcon = (pathD, className = "", viewBox = "0 0 24 24") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (className) svg.setAttribute("class", className);
  svg.setAttribute("viewBox", viewBox);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", pathD);
  svg.appendChild(path);
  return svg;
};
function getWatermarks() {
  const key = `read-watermark:${chatState.currentAccount || "default"}`;
  try {
    const marks = JSON.parse(localStorage.getItem(key) || "{}");
    return marks && typeof marks === "object" && !Array.isArray(marks) ? marks : {};
  }
  catch { return {}; }
}
function markSessionAsRead(username) {
  if (window.desktopHost?.isSettingsWindow) return;
  if (!username || document.hidden || chatState.view !== "chat") return;
  const key = `read-watermark:${chatState.currentAccount || "default"}`;
  const marks = getWatermarks();
  const session = chatState.sessions.get(username);
  if (!session) return;
  marks[username] = {
    // The message timestamp is independent of pinning and conversation ordering.
    messageTime: Number(session.time) || 0,
    unreadCount: Number(session?.unreadCount) || 0,
    preview: String(session?.preview || ""),
    readAt: Date.now()
  };
  try { localStorage.setItem(key, JSON.stringify(marks)); } catch {}
}
function getVisibleUnreadCount(session) {
  if (!session) return 0;
  const serverUnread = Number(session.unreadCount) || 0;
  if (serverUnread <= 0) return 0;
  if (session.username === chatState.currentUser && chatState.view === "chat" && !document.hidden) return 0;
  const marks = getWatermarks();
  const wm = marks[session.username];
  if (!wm) return serverUnread;
  const curTime = Number(session.time) || 0;
  const curPreview = String(session.preview || "");
  const hasNewTime = Number.isFinite(wm.messageTime) && curTime > wm.messageTime;
  const hasIncreasedUnread = serverUnread > wm.unreadCount;
  // Older watermarks stored sortTimestamp; use their count and preview until the next read.
  const hasNewPreview = (!Number.isFinite(wm.messageTime) || curTime === wm.messageTime) && curPreview !== wm.preview && curPreview !== "";
  if (hasIncreasedUnread) return serverUnread - wm.unreadCount;
  if (session.lastMessageFromSelf === true) return 0;
  if (hasNewTime || hasNewPreview) return serverUnread;
  return 0;
}
const defaults = { theme: "dark", zoom: "1.0" };
settingsState.settings = undefined;
try { settingsState.settings = { ...defaults, ...JSON.parse(localStorage.getItem("real-ui-settings-1") || "{}") }; }
catch { settingsState.settings = { ...defaults }; }
delete settingsState.settings.historyLimit;
// Retired settings of the WechatVibe label and background-analysis features.
delete settingsState.settings.intent;
delete settingsState.settings.backgroundAnalyze;
if (!["dark", "light"].includes(settingsState.settings.theme)) settingsState.settings.theme = "dark";
if (!["0.9", "1.0", "1.1", "1.25", "1.5"].includes(settingsState.settings.zoom)) settingsState.settings.zoom = "1.0";
const save = () => localStorage.setItem("real-ui-settings-1", JSON.stringify(settingsState.settings));
chatState.sessions = new Map();
chatState.selectedConversations = new Set();
chatState.selectionLoadedAccount = null;
chatState.conversationSelectionBusy = false;
chatState.sessionCache = new Map();
const sessionCacheMessageLimit = 80;
const historyWindowLimit = 240;
// Modified by Know Your Friends, 2026-10-11 (Apache-2.0): continuous, bounded history browsing.
const historyLoadDistance = 200;
const historyPrefetchDistance = 800;
chatState.historyState = null;
chatState.historyRequest = 0;
chatState.historyController = null;
chatState.historyPrefetch = null;
chatState.historyPrefetchTimer = null;
chatState.historyScrollFrame = null;
chatState.self = null;
chatState.sessionSignature = null;
chatState.sessionRequest = 0;
chatState.sessionLoading = false;
chatState.sessionRefreshQueued = false;
chatState.windowRequestSerial = 0;
chatState.preloadDone = 0;
chatState.preloadTotal = 0;
chatState.currentAccount = null;
chatState.currentUser = null;
chatState.currentHasMoreBefore = null;
chatState.messageSourceReady = false;
chatState.view = "chat";
chatState.generation = 0;
chatState.controller = null;
chatState.messages = [];
chatState.messagePending = false;
chatState.messageRequest = 0;
chatState.messageRefreshQueued = false;
chatState.emptyMessagePolls = 0;
chatState.followLatest = true;
chatState.lastChatScrollTop = 0;
let toastTimer;
let accountUnavailable = false;
let accountClearedExiting = false;
let startupActive = !window.desktopHost?.isSettingsWindow;
let startupStage = "account";
let startupStageEpoch = 0;
let startupAttempt = 0;
let startupWatchdog = null;
let startupAccountRetryUsed = false;
let startupAccountRetryTimer = null;
function renderDatabaseLoading(stage, message, attention = false) {
  const panel = byId("databaseLoading");
  panel.hidden = false;
  panel.dataset.state = attention ? "attention" : "loading";
  text("databaseLoadingTitle", attention ? "微信数据库读取尚未完成" : "正在读取微信数据库");
  text("databaseLoadingStatus", message);
  byId("databaseLoadingRetry").hidden = !attention;
  byId("databaseLoadingProgress").hidden = attention;
  const steps = ["account", "sessions", "messages"];
  for (const item of panel.querySelectorAll("[data-step]")) {
    item.classList.toggle("done", steps.indexOf(item.dataset.step) < steps.indexOf(stage));
    item.classList.toggle("active", item.dataset.step === stage);
  }
}
function accountCheckStatus(message, retry = false) {
  text("accountCheckSummary", message);
  text("accountCheckStatus", message);
  const ready = message.startsWith("聊天记录就绪") || message === "微信账号已就绪";
  byId("accountValidation").hidden = ready;
  byId("accountCheckSummary").dataset.state = ready ? "ready" : retry ? "attention" : "working";
  byId("accountCheckSummary").title = message;
  byId("btnRetryAccountCheck").hidden = !retry;
}
function showStartup(stage, message, options = {}) {
  accountCheckStatus(message, options.retry === true);
  renderDatabaseLoading(stage, message, options.retry === true);
  if (!startupActive) return;
  startupStage = stage;
  const epoch = ++startupStageEpoch;
  const steps = ["account", "sessions", "messages"];
  for (const item of byId("startupOverlay").querySelectorAll(".startup-step")) {
    const position = steps.indexOf(item.dataset.step);
    item.classList.toggle("done", position < steps.indexOf(stage));
    item.classList.toggle("active", item.dataset.step === stage);
  }
  text("startupStatus", message);
  byId("startupRetry").hidden = !options.retry;
  byId("startupContinue").hidden = !options.continueEmpty;
  clearTimeout(startupWatchdog);
  if (!options.retry) startupWatchdog = setTimeout(() => {
    if (startupActive && startupStageEpoch === epoch) {
      byId("startupRetry").hidden = false;
      byId("databaseLoadingRetry").hidden = false;
    }
  }, 12000);
}
function unlockStartupUi() {
  byId("startupOverlay").hidden = true;
  byId("appWindow").removeAttribute("inert");
  // The scheduler lives with the update flow. A startup slice can run this alone.
  if (typeof scheduleBackgroundUpdateChecks === "function") scheduleBackgroundUpdateChecks();
}
function completeStartup() {
  byId("databaseLoading").hidden = true;
  accountCheckStatus(chatState.preloadTotal ? `聊天记录就绪 ${chatState.preloadDone}/${chatState.preloadTotal}` : "微信账号已就绪");
  if (!startupActive) return;
  startupActive = false;
  startupAttempt++;
  clearTimeout(startupWatchdog);
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  startupAccountRetryUsed = false;
  unlockStartupUi();
}
function retryStartup() {
  if (!startupActive || accountClearedExiting) return;
  startupAttempt++;
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  startupAccountRetryUsed = true;
  showStartup(chatState.currentAccount ? "messages" : "sessions", chatState.currentAccount ? `正在准备聊天记录 ${chatState.preloadDone}/${chatState.preloadTotal}` : "正在读取会话列表…");
  void loadSessions();
}
function sessionCacheKey(account, user) {
  return JSON.stringify([account, user]);
}
function sessionSummarySignature(session) {
  return JSON.stringify(session);
}
function cacheCurrentSession(update) {
  if (!chatState.currentAccount || !chatState.currentUser) return;
  const key = sessionCacheKey(chatState.currentAccount, chatState.currentUser);
  const cached = chatState.sessionCache.get(key);
  if (!cached && !Array.isArray(update.messages)) return;
  if (update.windowSerial && cached?.windowSerial > update.windowSerial) return;
  const entry = { ...(cached || { account: chatState.currentAccount, user: chatState.currentUser, messages: null, scrollTop: 0, followLatest: true }), ...update };
  if (Array.isArray(entry.messages)) entry.messages = entry.messages.slice(-sessionCacheMessageLimit);
  chatState.sessionCache.set(key, entry);
}
function cacheSessionWindow(account, session, next, serial, hasMoreBefore) {
  const key = sessionCacheKey(account, session.username);
  const cached = chatState.sessionCache.get(key);
  if (serial < (cached?.windowSerial || 0)) return;
  const selected = next.slice(-sessionCacheMessageLimit);
  chatState.sessionCache.set(key, {
    ...(cached || { account, user: session.username, scrollTop: 0, followLatest: true }),
    messages: selected,
    summarySignature: sessionSummarySignature(session),
    ...(typeof hasMoreBefore === "boolean" ? { hasMoreBefore } : {}),
    windowSerial: serial
  });
}
function sessionWindowReady(account, session) {
  const cached = chatState.sessionCache.get(sessionCacheKey(account, session.username));
  return Array.isArray(cached?.messages) &&
    (session.username === chatState.currentUser && !startupActive || cached.summarySignature === sessionSummarySignature(session));
}
function pruneSessionCache(account, nextSessions) {
  for (const [key, entry] of chatState.sessionCache) {
    if (entry.account !== account || !nextSessions.has(entry.user)) chatState.sessionCache.delete(key);
  }
}
const percent = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(1).replace(/\.0$/, "")}%` : "";
const time = value => { const number = Number(value); if (!Number.isFinite(number) || number <= 0) return ""; const date = new Date(number); return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""; };
function toast(value) {
  text("toastMsg", value);
  byId("toastMsg").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => byId("toastMsg").classList.remove("show"), 2300);
}
function avatarUrls(value, candidates = []) {
  return [value, ...(Array.isArray(candidates) ? candidates : [])].map(candidate => {
    try { const url = new URL(candidate); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch { return null; }
  }).filter(Boolean);
}
function avatar(value, className, candidates = [], name = "", group = false) {
  const frame = element("span", `${className === "msg-avatar" ? "msg-avatar-frame" : className} avatar-frame`);
  const fallback = element("span", "avatar-fallback", group ? "群" : Array.from(String(name || "·").trim())[0] || "·");
  frame.appendChild(fallback);
  const urls = avatarUrls(value, candidates);
  if (urls.length) {
    const image = element("img", `${className === "msg-avatar" ? "msg-avatar " : ""}avatar-image`);
    image.alt = "";
    if (className === "session-avatar" || className === "msg-avatar") {
      image.loading = "lazy";
      image.decoding = "async";
    }
    let index = 0;
    image.onload = () => frame.classList.add("loaded");
    image.onerror = () => { frame.classList.remove("loaded"); index++; if (index < urls.length) image.src = urls[index]; else image.remove(); };
    frame.appendChild(image);
    image.src = urls[index];
  }
  return frame;
}
function setAvatar(id, value, candidates = [], name = "", group = false) {
  const target = byId(id);
  if (id === "selfAvatar") {
    const fallback = byId("selfAvatarFallback");
    fallback.textContent = Array.from(String(name || "我").trim())[0] || "我";
    fallback.style.display = "grid";
    target.style.display = "none";
    const urls = avatarUrls(value, candidates);
    let index = 0;
    target.onload = () => { target.style.display = "block"; fallback.style.display = "none"; };
    target.onerror = () => { index++; if (index < urls.length) target.src = urls[index]; else { target.removeAttribute("src"); target.style.display = "none"; } };
    if (urls.length) target.src = urls[index];
    else target.removeAttribute("src");
    return;
  }
  target.replaceChildren(avatar(value, "avatar-inner", candidates, name, group));
}
async function api(path, options = {}, signal) {
  const response = await fetch(path, { ...options, signal, headers: { ...(options.body ? { "Content-Type": "application/json" } : {}) } });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    if (response.status === 503) {
      try {
        const body = await response.json();
        error.code = typeof body?.error === "string" ? body.error : "";
        const preparation = body?.preparation;
        if (preparation && typeof preparation === "object" && typeof preparation.reason === "string" &&
            typeof preparation.message === "string" && preparation.message.length <= 240) {
          error.preparation = { reason: preparation.reason, message: preparation.message };
          if (["preparing", "sessions", "failed", "ready"].includes(preparation.state))
            error.preparation.state = preparation.state;
        }
      } catch { }
    }
    throw error;
  }
  const data = await response.json();
  if (data.error) throw new Error(String(data.error));
  if (options.method && options.method !== "GET") window.SettingsSync?.changed(path);
  return data;
}
function status(container, message, retry) {
  container.replaceChildren(element("div", "ui-state", message));
  if (retry) {
    const button = element("button", "ui-retry", "重试");
    button.addEventListener("click", retry);
    container.appendChild(button);
  }
}
const isNetworkFailure = error => error instanceof TypeError;
function resetAccountView(message = "当前微信账号未就绪", preserveOtherCaches = false) {
  window.ConversationMenu?.close();
  chatState.conversationSelectionEpoch++;
  chatState.conversationSelectionBusy = false;
  if (byId("conversationActionConfirm")?.open) byId("conversationActionConfirm").close("cancel");
  window.ChatMedia?.reset();
  window.MessageAnalysis?.reset();
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  cancelHistoryRequest();
  chatState.historyState = null;
  resetHistorySearch();
  if (!startupActive) startupActive = true;
  showStartup("account", message, { retry: message === "当前微信账号未就绪", continueEmpty: message === "当前微信账号未就绪" });
  accountUnavailable = false;
  chatState.sessionRequest++;
  chatState.controller?.abort();
  chatState.controller = null;
  chatState.advance("generation");
  chatState.messageRequest++;
  chatState.currentAccount = null;
  announceFriendAccount("account", null);
  chatState.selectionLoadedAccount = null;
  chatState.selectedConversations.clear();
  updateAddConversationButton();
  chatState.currentUser = null;
  window.ChatComposer?.setScope(null, null);
  chatState.currentHasMoreBefore = null;
  window.PluginSlots?.onSessionChanged(null, null);
  window.ExternalImportHost?.onSessionChanged(null, null);
  window.ModelTraces?.setScope(null, null, chatState.view);
  chatState.messageSourceReady = false;
  chatState.self = null;
  chatState.sessionSignature = null;
  chatState.preloadDone = 0;
  chatState.preloadTotal = 0;
  chatState.sessions.clear();
  if (!preserveOtherCaches) chatState.sessionCache.clear();
  chatState.messages = [];
  chatState.messagePending = false;
  chatState.messageRefreshQueued = false;
  chatState.followLatest = true;
  chatState.lastChatScrollTop = 0;
  byId("searchInput").value = "";
  setAvatar("selfAvatar", null, [], "我");
  text("chatTitle", "聊天");
  updateFriendButton();
  status(byId("chatMessages"), message);
  status(byId("sessionList"), message);
  updateHistoryNavigation();
}
function accountUnavailableError(error) {
  return error?.status === 503 && error?.code === "AccountUnavailableError";
}
function accountChangedError(error) {
  return error?.status === 503 && error?.code === "AccountChangedError";
}
function contactSnapshotStaleError(error) {
  return error?.status === 503 && error?.code === "ContactSnapshotStaleError";
}
function renderAccountUnavailable(error) {
  const message = error.preparation?.message || "当前微信账号未就绪";
  const reason = error.preparation?.reason;
  const title = reason === "wechat_not_running" ? "请先打开并登录微信" :
    reason === "active_account_unavailable" ? "请确认电脑版微信已登录" : null;
  if (title) {
    const card = element("section", "account-login-card");
    card.setAttribute("aria-label", "微信连接提示");
    const description = element("div", "account-login-description");
    description.setAttribute("role", "status");
    description.append(element("h2", "account-login-title", title), element("p", "account-login-message", message));
    const button = element("button", "account-login-retry", "我已登录，重新检测");
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "正在检测…";
      try { await loadSessions(); }
      finally { button.disabled = false; button.textContent = "我已登录，重新检测"; }
    });
    card.append(description, button, element("p", "account-login-hint", "登录后会自动连接"));
    byId("chatMessages").replaceChildren(card);
  } else status(byId("chatMessages"), message);
  const preparing = error.preparation?.state === "preparing" || error.preparation?.reason === "preparing";
  status(byId("sessionList"), title || message, preparing ? undefined : () => { void loadSessions(); });
}
function handleAccountBoundaryError(error) {
  if (accountChangedError(error)) {
    resetAccountView("微信账号已变化，正在读取会话…");
    void loadSessions();
    return true;
  }
  if (accountUnavailableError(error)) {
    const reason = error.preparation?.message || "当前微信账号未就绪";
    resetAccountView(reason);
    accountUnavailable = true;
    renderAccountUnavailable(error);
    const preparing = error.preparation?.state === "preparing" || error.preparation?.reason === "preparing";
    showStartup("account", reason, { retry: !preparing, continueEmpty: !preparing });
    return true;
  }
  return false;
}
function acceptResponseAccount(account) {
  if (account === undefined || account === chatState.currentAccount) return true;
  resetAccountView("微信账号已变化，正在读取会话…");
  void loadSessions();
  return false;
}
function renderSessions() {
  // A visible conversation stays read when its session snapshot refreshes, even while browsing history.
  markSessionAsRead(chatState.currentUser);
  const container = byId("sessionList");
  const query = byId("searchInput").value.trim().toLowerCase();
  const existing = new Map([...container.children].filter(node => node.classList.contains("session-item")).map(node => [node.dataset.id, node]));
  let visible = 0;
  const ordered = window.ConversationMenu?.sortSessions([...chatState.sessions.values()]) || chatState.sessions.values();
  for (const session of ordered) {
    if (!chatState.selectedConversations.has(session.username)) continue;
    if (!`${session.name || ""} ${session.preview || ""}`.toLowerCase().includes(query)) continue;
    let item = existing.get(session.username);
    if (!item) {
      item = element("div", "session-item");
      item.dataset.id = session.username;
      item.tabIndex = 0;
      window.ConversationMenu?.attach(item);
      const avatarWrap = element("div", "session-avatar-wrap");
      const info = element("div", "session-info");
      const top = element("div", "session-top");
      top.append(element("span", "session-name"), element("span", "session-time"));
      const bottom = element("div", "session-bottom");
      bottom.appendChild(element("span", "session-preview"));
      info.append(top, bottom);
      item.append(avatarWrap, info);
      item.addEventListener("click", () => {
        if (!chatState.messageSourceReady) {
          text("chatTitle", chatState.sessions.get(item.dataset.id)?.name || item.dataset.id);
          status(byId("chatMessages"), "聊天记录尚未就绪，正在重试…");
          return;
        }
        markSessionAsRead(item.dataset.id);
        switchSession(item.dataset.id);
      });
    }
    item.classList.toggle("active", session.username === chatState.currentUser);
    const avatarWrap = item.querySelector(".session-avatar-wrap");
    const avatarSignature = JSON.stringify([session.avatar, session.avatarCandidates, session.name || session.username, session.isGroup]);
    if (item.dataset.avatarSignature !== avatarSignature) {
      avatarWrap.querySelector(".avatar-frame")?.remove();
      avatarWrap.prepend(avatar(session.avatar, "session-avatar", session.avatarCandidates, session.name || session.username, session.isGroup));
      item.dataset.avatarSignature = avatarSignature;
    }
    const unread = getVisibleUnreadCount(session);
    const badge = avatarWrap.querySelector(".session-unread-dot");
    if (unread > 0) {
      const label = unread > 99 ? "99+" : unread > 9 ? "9+" : String(unread);
      if (badge) { if (badge.textContent !== label) badge.textContent = label; }
      else avatarWrap.appendChild(element("span", "session-unread-dot", label));
    } else badge?.remove();
    const name = item.querySelector(".session-name");
    const displayName = session.name || session.username;
    const pinned = window.ConversationMenu?.isPinned(session) ?? !!session.pinned;
    item.classList.toggle("pinned", pinned);
    if (name.dataset.label !== displayName || name.dataset.pinned !== String(pinned)) {
      name.replaceChildren(document.createTextNode(displayName));
      if (pinned) name.appendChild(element("span", "session-pinned", "置顶"));
      name.dataset.label = displayName;
      name.dataset.pinned = String(pinned);
    }
    const timeNode = item.querySelector(".session-time");
    const displayTime = time(session.time);
    if (timeNode.textContent !== displayTime) timeNode.textContent = displayTime;
    const preview = item.querySelector(".session-preview");
    if (preview.textContent !== (session.preview || "")) preview.textContent = session.preview || "";
    if (container.children[visible] !== item) container.insertBefore(item, container.children[visible] || null);
    visible++;
  }
  while (container.children.length > visible) container.lastElementChild.remove();
  updateAddConversationButton();
  if (!visible && chatState.sessions.size && !chatState.selectedConversations.size) {
    container.replaceChildren();
    const empty = element("div", "session-empty");
    empty.appendChild(element("strong", "", "还没有添加会话"));
    empty.appendChild(element("span", "", "从信息列表选择要查看的聊天"));
    const button = element("button", "settings-action-btn", "打开信息列表");
    button.type = "button";
    button.addEventListener("click", openConversationManager);
    empty.appendChild(button);
    container.appendChild(empty);
  } else if (!visible && chatState.sessions.size && query) {
    container.replaceChildren();
    const empty = element("div", "session-empty");
    empty.appendChild(element("strong", "", "没有匹配的会话"));
    const button = element("button", "settings-action-btn", "打开信息列表");
    button.type = "button";
    button.addEventListener("click", openConversationManager);
    empty.appendChild(button);
    container.appendChild(empty);
  } else if (!visible) status(container, chatState.sessions.size ? "没有匹配的会话" : "暂无会话");
  window.ConversationMenu?.refresh();
}
function updateAddConversationButton() {
  const button = byId("btnAddConversation");
  // Browsing the current account's directory does not require chat messages to be ready.
  const ready = !!chatState.currentAccount && chatState.selectionLoadedAccount === chatState.currentAccount;
  button.disabled = !ready;
  button.title = ready ? "添加要查看的聊天" : "会话目录尚未就绪";
}
async function preloadSessionWindows(account, nextSessions, request) {
  const list = [...nextSessions.values()].filter(session => chatState.selectedConversations.has(session.username));
  chatState.preloadTotal = list.length;
  if (!list.length) { chatState.preloadDone = 0; return true; }
  const missing = list.filter(session => !sessionWindowReady(account, session));
  chatState.preloadDone = list.length - missing.length;
  if (startupActive) showStartup("messages", `正在准备聊天记录 ${chatState.preloadDone}/${chatState.preloadTotal}`);
  for (let offset = 0; offset < missing.length; offset += 64) {
    const batch = missing.slice(offset, offset + 64);
    const serial = ++chatState.windowRequestSerial;
    const data = await api("/api/messages/batch", { method: "POST", body: JSON.stringify({ account, users: batch.map(session => session.username) }) });
    if (request !== chatState.sessionRequest || chatState.currentAccount !== account) return false;
    if (data.account !== account) {
      const error = new Error("Batch account changed");
      error.status = 503;
      error.code = "AccountChangedError";
      throw error;
    }
    if (!Array.isArray(data.windows)) throw new Error("Invalid batch response");
    const expected = new Map(batch.map(session => [session.username, session]));
    const seen = new Set();
    for (const window of data.windows) {
      if (!window || !expected.has(window.user) || seen.has(window.user) || !Array.isArray(window.messages)) throw new Error("Invalid batch window");
      seen.add(window.user);
      cacheSessionWindow(account, expected.get(window.user), window.messages, serial, window.hasMoreBefore);
    }
    chatState.preloadDone = list.filter(session => sessionWindowReady(account, session)).length;
    if (startupActive) showStartup("messages", `正在准备聊天记录 ${chatState.preloadDone}/${chatState.preloadTotal}`);
    if (seen.size !== batch.length) throw new Error("Incomplete batch response");
  }
  return true;
}
async function loadConversationSelection(account, request) {
  if (chatState.selectionLoadedAccount === account) return;
  const state = await api("/api/conversation-selection");
  if (request !== chatState.sessionRequest || accountClearedExiting) return;
  if (!validConversationSelection(state, account))
    throw new Error("Invalid conversation selection response");
  // A new or cleared account starts with an empty sidebar and without following new chats.
  if (state.initialized === false) forgetConversationFollow(account);
  chatState.selectedConversations.clear();
  for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
  chatState.selectionLoadedAccount = account;
}
function validConversationSelection(state, account) {
  return Boolean(state) && state.account === account && Array.isArray(state.selectedSessions) &&
    state.selectedSessions.every(id => typeof id === "string");
}
function clearUnselectedConversation() {
  window.MessageAnalysis?.reset();
  if (chatState.currentUser) {
    cacheCurrentSession({ messages: chatState.messages, scrollTop: byId("chatMessages").scrollTop,
      followLatest: chatState.followLatest });
    cancelHistoryRequest();
    chatState.historyState = null;
    resetHistorySearch();
    chatState.controller?.abort();
    chatState.controller = null;
    chatState.advance("generation");
    chatState.currentUser = null;
    chatState.messages = [];
  }
  window.ChatComposer?.setScope(chatState.currentAccount, null);
  text("chatTitle", "聊天");
  showChatEmptyState();
  byId("btnChatHistory").disabled = true;
  updateFriendButton();
  updateHistoryNavigation();
  switchView("chat");
}
// Following new conversations is an explicit, account-scoped opt-in. Only unseen ids
// join the sidebar, so a conversation the user removed by hand stays removed.
const conversationFollowKey = "conversation-follow-v1";
function readConversationFollow() {
  try {
    const data = JSON.parse(localStorage.getItem(conversationFollowKey) || "{}");
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}
function rememberConversations(account, ids) {
  const data = readConversationFollow();
  const known = new Set(Array.isArray(data[account]?.known) ? data[account].known : []);
  for (const id of ids) known.add(id);
  data[account] = { known: [...known] };
  localStorage.setItem(conversationFollowKey, JSON.stringify(data));
}
function forgetConversationFollow(account) {
  const data = readConversationFollow();
  if (!(account in data)) return;
  delete data[account];
  localStorage.setItem(conversationFollowKey, JSON.stringify(data));
}
async function trackNewConversations(account) {
  if (chatState.conversationSelectionBusy || chatState.selectionLoadedAccount !== account ||
      account !== chatState.currentAccount) return;
  const epoch = chatState.conversationSelectionEpoch;
  const entry = readConversationFollow()[account];
  if (!Array.isArray(entry?.known)) return;
  const known = new Set(entry.known);
  const fresh = [...chatState.sessions.keys()].filter(id => !known.has(id));
  if (!fresh.length) return;
  chatState.conversationSelectionBusy = true;
  const added = [];
  try {
    for (const id of fresh) {
      if (account !== chatState.currentAccount || epoch !== chatState.conversationSelectionEpoch) return;
      if (chatState.selectedConversations.has(id)) {
        added.push(id);
        continue;
      }
      try {
        const state = await api("/api/conversation-selection", { method: "POST", body: JSON.stringify({
          expectedAccount: account, session: id, selected: true,
        }) });
        if (account !== chatState.currentAccount || epoch !== chatState.conversationSelectionEpoch ||
            !validConversationSelection(state, account)) return;
        chatState.selectedConversations.clear();
        for (const selected of state.selectedSessions) chatState.selectedConversations.add(selected);
        added.push(id);
      } catch { /* stays unknown, so the next session refresh tries it again */ }
    }
  } finally {
    if (account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch)
      chatState.conversationSelectionBusy = false;
    if (added.length && account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch) {
      rememberConversations(account, added);
      renderSessions();
      if (!byId("conversationManager").hidden) renderConversationManager();
    }
  }
}
function setConversationAutoFollow(enabled) {
  const account = chatState.currentAccount;
  if (!account || chatState.selectionLoadedAccount !== account || chatState.conversationSelectionBusy) return;
  try {
    if (enabled) rememberConversations(account, [...chatState.sessions.keys(), ...chatState.selectedConversations]);
    else forgetConversationFollow(account);
    text("conversationManagerStatus", enabled ? "以后新出现的会话将自动加入分析列表" : "已关闭自动添加新会话");
  } catch {
    text("conversationManagerStatus", "自动添加设置保存失败，请重试");
  }
  renderConversationManager();
}
function confirmConversationAction({ title, message, confirmLabel }) {
  const dialog = byId("conversationActionConfirm");
  text("conversationActionTitle", title);
  text("conversationActionMessage", message);
  text("btnConfirmConversationAction", confirmLabel);
  dialog.returnValue = "";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    dialog.showModal();
    byId("btnCancelConversationAction").focus();
  });
}
async function addAllConversations() {
  const account = chatState.currentAccount;
  const epoch = chatState.conversationSelectionEpoch;
  if (!account || chatState.conversationSelectionBusy || chatState.selectionLoadedAccount !== account) return;
  const pending = [...chatState.sessions.keys()].filter(id => !chatState.selectedConversations.has(id));
  if (!pending.length) return;
  chatState.conversationSelectionBusy = true;
  renderConversationManager();
  try {
    if (!await confirmConversationAction({ title: `添加 ${pending.length} 个会话？`,
      message: `将整个会话目录中尚未添加的 ${pending.length} 个会话加入分析列表，不受搜索条件影响。添加后可能触发较多分析任务。此操作不会开启“自动添加新会话”。`,
      confirmLabel: `确认添加 ${pending.length} 个`,
    })) return;
    if (account !== chatState.currentAccount || chatState.selectionLoadedAccount !== account ||
        epoch !== chatState.conversationSelectionEpoch) return;
    const current = [...chatState.sessions.keys()].filter(id => !chatState.selectedConversations.has(id));
    if (current.length !== pending.length || pending.some(id => !current.includes(id))) {
      text("conversationManagerStatus", "会话目录已更新，请重新确认添加范围");
      return;
    }
    text("conversationManagerStatus", "正在添加全部会话…");
    const state = await api("/api/conversation-selection", { method: "POST", body: JSON.stringify({
      expectedAccount: account, all: true,
    }) });
    if (account !== chatState.currentAccount || chatState.selectionLoadedAccount !== account ||
        epoch !== chatState.conversationSelectionEpoch) return;
    if (!validConversationSelection(state, account) || pending.some(id => !state.selectedSessions.includes(id)))
      throw new Error("选择结果不匹配");
    chatState.selectedConversations.clear();
    for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
    let followSaveFailed = false;
    try {
      if (Array.isArray(readConversationFollow()[account]?.known)) rememberConversations(account, chatState.sessions.keys());
    } catch { followSaveFailed = true; }
    renderSessions();
    text("conversationManagerStatus", "已添加 " + chatState.selectedConversations.size + " 个会话" +
      (followSaveFailed ? "，自动添加设置保存失败，请重新设置" : ""));
    if (!chatState.currentUser) {
      const first = [...chatState.sessions.keys()].find(id => chatState.selectedConversations.has(id));
      if (first) switchSession(first);
    }
  } catch {
    if (account === chatState.currentAccount && chatState.selectionLoadedAccount === account &&
        epoch === chatState.conversationSelectionEpoch)
      text("conversationManagerStatus", "操作失败，请重试");
  } finally {
    if (account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch) {
      chatState.conversationSelectionBusy = false;
      renderConversationManager();
      if (!byId("conversationManager").hidden)
        (byId("btnAddAllConversations").disabled ? byId("conversationSearch") : byId("btnAddAllConversations")).focus();
    }
  }
}
settingsState.sweepBusy = false;
settingsState.overviewBusy = false;
settingsState.analysisOverviewSnapshot = null;
settingsState.workerSettings = null;
function showChatEmptyState() {
  const container = byId("chatMessages");
  const empty = element("div", "session-empty chat-empty");
  empty.appendChild(element("strong", "", "还没有打开会话"));
  const button = element("button", "settings-action-btn", "打开信息列表");
  button.type = "button";
  button.addEventListener("click", openConversationManager);
  empty.appendChild(button);
  container.replaceChildren(empty);
}
function filteredConversationManagerSessions() {
  const query = byId("conversationSearch").value.trim().toLowerCase();
  const sessions = [...chatState.sessions.values()];
  for (const id of chatState.selectedConversations) if (!chatState.sessions.has(id))
    sessions.push({ username: id, name: "已保存但当前不可见的会话", unavailable: true });
  return sessions.filter(session => `${session.name || ""} ${session.username}`.toLowerCase().includes(query))
    .sort((left, right) => Number(chatState.selectedConversations.has(right.username)) -
      Number(chatState.selectedConversations.has(left.username)));
}
function updateConversationManagerSelection() {
  const checked = conversationRemovalSelection();
  for (const id of checked) if (!chatState.selectedConversations.has(id)) checked.delete(id);
  const busy = chatState.conversationSelectionBusy || !chatState.currentAccount ||
    chatState.selectionLoadedAccount !== chatState.currentAccount;
  const removable = filteredConversationManagerSessions().filter(session => chatState.selectedConversations.has(session.username));
  const visibleChecked = removable.filter(session => checked.has(session.username)).length;
  const selectAll = byId("conversationSelectAll");
  selectAll.checked = removable.length > 0 && visibleChecked === removable.length;
  selectAll.indeterminate = visibleChecked > 0 && visibleChecked < removable.length;
  selectAll.disabled = busy || !removable.length;
  const filtered = !!byId("conversationSearch").value.trim();
  text("conversationSelectAllLabel", `${filtered ? "全选搜索结果中的已添加会话" : "全选已添加"}（${removable.length}）`);
  byId("btnClearConversationSelection").disabled = busy || !checked.size;
  const hiddenCount = checked.size - visibleChecked;
  text("conversationSelectionSummary", `已选 ${checked.size} 个${hiddenCount ? `，其中 ${hiddenCount} 个不在当前搜索结果中` : ""}${checked.size > 1000 ? "。每次最多移除 1000 个，请取消选择后按搜索结果分批移除" : ""}`);
  const removeButton = byId("btnRemoveSelectedConversations");
  removeButton.disabled = busy || !checked.size || checked.size > 1000;
  removeButton.textContent = checked.size ? `移除所选（${checked.size}）` : "移除所选";
  removeButton.title = checked.size > 1000 ? "每次最多移除 1000 个会话，请分批选择" : "";
  const pendingCount = [...chatState.sessions.keys()].filter(id => !chatState.selectedConversations.has(id)).length;
  byId("btnAddAllConversations").disabled = busy || !pendingCount;
  text("btnAddAllConversations", pendingCount ? `添加全部未添加会话（${pendingCount}）…` : "所有会话均已添加");
  const follow = byId("conversationAutoFollow");
  follow.checked = Array.isArray(readConversationFollow()[chatState.currentAccount]?.known);
  follow.disabled = busy;
  byId("conversationSearch").disabled = busy;
  const total = new Set([...chatState.sessions.keys(), ...chatState.selectedConversations]).size;
  text("conversationManagerCount", `已添加 ${chatState.selectedConversations.size} / ${total} 个会话`);
  for (const row of byId("conversationManagerList").children) {
    const checkbox = row.querySelector(".conversation-manager-check");
    if (checkbox) { checkbox.checked = checked.has(row.dataset.user); checkbox.disabled = busy; }
  }
}
function selectAllRemovableConversations(selected) {
  if (chatState.conversationSelectionBusy || !chatState.currentAccount ||
      chatState.selectionLoadedAccount !== chatState.currentAccount) return;
  const checked = conversationRemovalSelection();
  for (const session of filteredConversationManagerSessions()) if (chatState.selectedConversations.has(session.username)) {
    if (selected) checked.add(session.username); else checked.delete(session.username);
  }
  updateConversationManagerSelection();
}
function clearConversationRemovalSelection() {
  if (chatState.conversationSelectionBusy) return;
  conversationRemovalSelection().clear();
  updateConversationManagerSelection();
}
function renderConversationManager() {
  const list = byId("conversationManagerList");
  const checked = conversationRemovalSelection();
  const scrollTop = list.scrollTop;
  list.replaceChildren();
  for (const session of filteredConversationManagerSessions()) {
    const row = element("div", "conversation-manager-row");
    row.dataset.user = session.username;
    const selected = chatState.selectedConversations.has(session.username);
    if (selected) {
      const checkbox = element("input", "conversation-manager-check");
      checkbox.type = "checkbox";
      checkbox.checked = checked.has(session.username);
      checkbox.disabled = chatState.conversationSelectionBusy;
      checkbox.setAttribute("aria-label", `选择移除${session.name || session.username}`);
      checkbox.addEventListener("change", () => {
        if (chatState.conversationSelectionBusy) return;
        if (checkbox.checked) checked.add(session.username); else checked.delete(session.username);
        updateConversationManagerSelection();
      });
      row.appendChild(checkbox);
    }
    row.appendChild(avatar(session.avatar, "session-avatar", session.avatarCandidates,
      session.name || session.username, session.isGroup));
    row.appendChild(element("strong", "", session.name || session.username));
    const button = element("button", "settings-action-btn", selected ? "从列表移除" : "添加");
    button.type = "button";
    button.disabled = chatState.conversationSelectionBusy;
    button.addEventListener("click", () => { void toggleConversationSelected(session.username); });
    row.appendChild(button);
    list.appendChild(row);
  }
  if (!list.children.length) status(list, chatState.sessions.size ? "没有匹配的会话" : "会话目录尚未就绪");
  updateConversationManagerSelection();
  list.scrollTop = scrollTop;
}
function conversationRemovalSelection() {
  if (chatState.conversationRemoveAccount !== chatState.currentAccount || !chatState.conversationRemoveSelection) {
    chatState.conversationRemoveAccount = chatState.currentAccount;
    chatState.conversationRemoveSelection = new Set();
  }
  return chatState.conversationRemoveSelection;
}
async function removeSelectedConversations() {
  const account = chatState.currentAccount;
  const epoch = chatState.conversationSelectionEpoch;
  const checked = conversationRemovalSelection();
  const sessions = [...checked].filter(id => chatState.selectedConversations.has(id));
  if (!account || chatState.selectionLoadedAccount !== account || chatState.conversationSelectionBusy || !sessions.length) return;
  if (sessions.length > 1000) {
    text("conversationManagerStatus", "每次最多移除 1000 个会话，请分批选择");
    return;
  }
  chatState.conversationSelectionBusy = true;
  renderConversationManager();
  try {
    const visible = new Set(filteredConversationManagerSessions().map(session => session.username));
    const hiddenCount = sessions.filter(id => !visible.has(id)).length;
    const removingAll = sessions.length === chatState.selectedConversations.size;
    if (!await confirmConversationAction({ title: `移除 ${sessions.length} 个会话？`,
      message: `将所选 ${sessions.length} 个会话从分析列表移除${hiddenCount ? `（其中 ${hiddenCount} 个不在当前搜索结果中）` : ""}。聊天记录、已有画像和助手记录都会保留。已排队或启动的分析任务可能继续完成。${removingAll ? "列表将被清空，并关闭自动添加新会话。" : ""}`,
      confirmLabel: `确认移除 ${sessions.length} 个`,
    })) return;
    if (account !== chatState.currentAccount || chatState.selectionLoadedAccount !== account ||
        epoch !== chatState.conversationSelectionEpoch) return;
    text("conversationManagerStatus", "正在移除所选会话…");
    const state = await api("/api/conversation-selection", { method: "POST", body: JSON.stringify({
      expectedAccount: account, sessions, selected: false,
    }) });
    if (account !== chatState.currentAccount || chatState.selectionLoadedAccount !== account ||
        epoch !== chatState.conversationSelectionEpoch) return;
    if (!validConversationSelection(state, account) || sessions.some(id => state.selectedSessions.includes(id)))
      throw new Error("选择结果不匹配");
    chatState.selectedConversations.clear();
    for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
    let followSaveFailed = false;
    try {
      if (!chatState.selectedConversations.size) forgetConversationFollow(account);
      else if (Array.isArray(readConversationFollow()[account]?.known)) rememberConversations(account, sessions);
    }
    catch { followSaveFailed = true; }
    checked.clear();
    renderSessions();
    if (chatState.currentUser && !chatState.selectedConversations.has(chatState.currentUser)) {
      const next = [...chatState.sessions.keys()].find(id => chatState.selectedConversations.has(id));
      if (next) switchSession(next); else clearUnselectedConversation();
    }
    text("conversationManagerStatus", `已移除 ${sessions.length} 个会话，聊天和画像已保留${followSaveFailed ? "；自动添加设置保存失败，请手动关闭" : ""}`);
  } catch {
    if (account === chatState.currentAccount && chatState.selectionLoadedAccount === account &&
        epoch === chatState.conversationSelectionEpoch)
      text("conversationManagerStatus", "移除失败，请重试");
  } finally {
    if (account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch) {
      chatState.conversationSelectionBusy = false;
      renderConversationManager();
      if (!byId("conversationManager").hidden)
        (byId("btnRemoveSelectedConversations").disabled ? byId("conversationSearch") : byId("btnRemoveSelectedConversations")).focus();
    }
  }
}
function openConversationManager() {
  if (!window.desktopHost?.isSettingsWindow && window.desktopHost?.openSettingsWindow) {
    void openAppSettings({ tab: "general", section: "conversations" });
    return;
  }
  if (!byId("settingsModal").classList.contains("show")) byId("btnSettings").click();
  document.querySelector('.settings-tab-btn[data-tab="general"]')?.click();
  byId("conversationManager").hidden = false;
  text("btnManageConversations", "收起");
  renderConversationManager();
  byId("conversationSearch").focus();
}
function closeConversationManager() {
  byId("conversationManager").hidden = true;
  text("btnManageConversations", "管理会话");
}
async function deleteConversation(user, account, epoch = chatState.conversationSelectionEpoch) {
  const ownsSelection = () => account === chatState.currentAccount && chatState.selectionLoadedAccount === account &&
    epoch === chatState.conversationSelectionEpoch;
  if (!account || !ownsSelection() || chatState.conversationSelectionBusy || !chatState.selectedConversations.has(user)) return;
  const name = chatState.sessions.get(user)?.name || user;
  chatState.conversationSelectionBusy = true;
  renderConversationManager();
  try {
    if (!await confirmConversationAction({ title: `删除与“${name}”的会话？`,
      message: "此会话将从聊天列表移除，聊天记录、已有画像和助手记录会保留。需要时可从信息列表重新添加。",
      confirmLabel: "删除",
    })) return;
    if (!ownsSelection() || !chatState.selectedConversations.has(user)) return;
    const state = await api("/api/conversation-selection", { method: "POST", body: JSON.stringify({
      expectedAccount: account, session: user, selected: false,
    }) });
    if (!ownsSelection()) return;
    if (!validConversationSelection(state, account) || state.selectedSessions.includes(user))
      throw new Error("选择结果不匹配");
    chatState.selectedConversations.clear();
    for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
    conversationRemovalSelection().delete(user);
    let followSaveFailed = false;
    try {
      if (!chatState.selectedConversations.size) forgetConversationFollow(account);
      else if (Array.isArray(readConversationFollow()[account]?.known)) rememberConversations(account, [user]);
    } catch { followSaveFailed = true; }
    renderSessions();
    if (chatState.currentUser === user) {
      const ordered = window.ConversationMenu?.sortSessions([...chatState.sessions.values()]) || [...chatState.sessions.values()];
      const next = ordered.find(session => chatState.selectedConversations.has(session.username));
      if (next) switchSession(next.username); else clearUnselectedConversation();
    }
    notifyStatus(followSaveFailed ? "会话已删除，自动添加设置保存失败，请重新设置" : "");
    text("conversationManagerStatus", "已从列表删除，聊天和画像已保留");
  } catch {
    if (ownsSelection()) notifyStatus("删除失败，请重试");
  } finally {
    if (ownsSelection()) {
      chatState.conversationSelectionBusy = false;
      renderConversationManager();
    }
  }
}
async function toggleConversationSelected(user) {
  const account = chatState.currentAccount;
  const epoch = chatState.conversationSelectionEpoch;
  if (!account || chatState.conversationSelectionBusy || chatState.selectionLoadedAccount !== account ||
      (!chatState.sessions.has(user) && !chatState.selectedConversations.has(user))) return;
  const selected = !chatState.selectedConversations.has(user);
  chatState.conversationSelectionBusy = true;
  text("conversationManagerStatus", selected ? "正在添加…" : "正在移除…");
  renderConversationManager();
  try {
    const state = await api("/api/conversation-selection", { method: "POST", body: JSON.stringify({
      expectedAccount: account, session: user, selected,
    }) });
    if (account !== chatState.currentAccount || chatState.selectionLoadedAccount !== account ||
        epoch !== chatState.conversationSelectionEpoch) return;
    if (!validConversationSelection(state, account) ||
        state.selectedSessions.includes(user) !== selected) throw new Error("选择结果不匹配");
    chatState.selectedConversations.clear();
    for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
    let followSaveFailed = false;
    try {
      if (!selected && Array.isArray(readConversationFollow()[account]?.known)) rememberConversations(account, [user]);
    } catch { followSaveFailed = true; }
    renderSessions();
    if (selected && chatState.messageSourceReady && chatState.sessions.has(user)) {
      if (!chatState.currentUser) switchSession(user);
      else void preloadSessionWindows(account, new Map([[user, chatState.sessions.get(user)]]), chatState.sessionRequest)
        .catch(() => {
          if (account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch)
            text("conversationManagerStatus", "已添加，消息将在点开会话时读取");
        });
    } else if (!selected && chatState.currentUser === user) {
      const next = [...chatState.sessions.keys()].find(id => chatState.selectedConversations.has(id));
      if (next) switchSession(next);
      else clearUnselectedConversation();
    }
    text("conversationManagerStatus", (selected ? "已添加" : "已从列表移除") +
      (followSaveFailed ? "，自动添加设置保存失败，请重新设置" : ""));
  } catch {
    if (account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch)
      text("conversationManagerStatus", "操作失败，请重试");
  } finally {
    if (account === chatState.currentAccount && epoch === chatState.conversationSelectionEpoch) {
      chatState.conversationSelectionBusy = false;
      renderConversationManager();
    }
  }
}
async function loadSessions(retryChanged = true) {
  if (accountClearedExiting) return;
  if (chatState.sessionLoading) { chatState.sessionRefreshQueued = true; return; }
  chatState.sessionLoading = true;
  if (startupActive || !chatState.messageSourceReady || accountUnavailable)
    showStartup(chatState.currentAccount ? "messages" : "account", chatState.currentAccount ? "正在准备聊天记录…" : "正在连接微信并读取数据库…");
  let request = ++chatState.sessionRequest;
  let followup = null;
  try {
    const data = await api("/api/sessions");
    if (request !== chatState.sessionRequest || accountClearedExiting) return;
    if (typeof data.account !== "string" || !data.account || !Array.isArray(data.sessions)) throw new Error("Invalid sessions response");
    if (window.desktopHost?.isSettingsWindow) {
      const changedAccount = chatState.currentAccount !== data.account;
      if (changedAccount) {
        chatState.conversationSelectionEpoch++;
        chatState.conversationSelectionBusy = false;
        chatState.selectionLoadedAccount = null;
        if (byId("conversationActionConfirm")?.open) byId("conversationActionConfirm").close("cancel");
      }
      await loadConversationSelection(data.account, request);
      if (request !== chatState.sessionRequest || accountClearedExiting) return;
      chatState.currentAccount = data.account;
      chatState.messageSourceReady = data.messagesReady !== false;
      chatState.self = data.self || null;
      chatState.sessions = new Map(data.sessions.filter(item => item?.username).map(item => [item.username, item]));
      accountUnavailable = false;
      window.ModelTraces?.setScope(data.account, null, "settings");
      accountCheckStatus(data.messagesReady === false ? "聊天记录正在准备中…" : "微信账号已就绪", data.messagesReady === false);
      renderConversationManager();
      if (changedAccount) void window.MediaSettings?.refresh?.();
      return;
    }
    if (startupActive) showStartup("sessions", "正在整理会话列表…");
    const signature = JSON.stringify([data.self, data.sessions, data.account]);
    if (data.messagesReady === false) {
      if (chatState.messageSourceReady || chatState.currentAccount !== data.account)
        resetAccountView("聊天记录尚未就绪", false);
      request = chatState.sessionRequest;
      await loadConversationSelection(data.account, request);
      if (request !== chatState.sessionRequest || accountClearedExiting) return;
      accountUnavailable = false;
      chatState.currentAccount = data.account;
      chatState.messageSourceReady = false;
      clearTimeout(startupAccountRetryTimer);
      startupAccountRetryTimer = null;
      chatState.self = data.self || null;
      setAvatar("selfAvatar", chatState.self?.avatar, chatState.self?.avatarCandidates, chatState.self?.name || "我");
      chatState.sessions.clear();
      for (const session of data.sessions) if (session?.username) chatState.sessions.set(session.username, session);
      chatState.sessionSignature = signature;
      chatState.currentUser = null;
      window.ChatComposer?.setScope(chatState.currentAccount, null);
      window.MessageAnalysis?.reset();
      byId("btnChatHistory").disabled = true;
      updateFriendButton();
      window.PluginSlots?.onSessionChanged(chatState.currentAccount, null);
      window.ExternalImportHost?.onSessionChanged(chatState.currentAccount, null);
      window.ModelTraces?.setScope(chatState.currentAccount, null, chatState.view);
      chatState.messages = [];
      const preparingMessage = typeof data.preparation?.message === "string" && data.preparation.message.length <= 240
        ? data.preparation.message : "聊天记录尚未就绪，正在重试…";
      status(byId("chatMessages"), preparingMessage);
      switchView("chat");
      renderSessions();
      if (!byId("conversationManager").hidden) renderConversationManager();
      // The directory is usable while message databases are still being prepared.
      unlockStartupUi();
      const preparing = !data.preparation || data.preparation.state === "preparing" || data.preparation.reason === "preparing";
      showStartup("messages", preparingMessage, { retry: !preparing });
      return;
    }
    const wasPartial = !chatState.messageSourceReady;
    const accountChanged = chatState.currentAccount !== data.account;
    if (accountChanged && chatState.currentAccount !== null) {
      resetAccountView("正在读取会话…");
      request = ++chatState.sessionRequest;
    }
    await loadConversationSelection(data.account, request);
    if (request !== chatState.sessionRequest || accountClearedExiting) return;
    accountUnavailable = false;
    chatState.currentAccount = data.account;
    chatState.messageSourceReady = true;
    announceFriendAccount("account", data.account);
    window.PluginSlots?.onSessionChanged(chatState.currentAccount, chatState.currentUser, pluginFriendName(chatState.currentUser));
    window.ExternalImportHost?.onSessionChanged(chatState.currentAccount, chatState.currentUser, pluginFriendName(chatState.currentUser));
    window.ModelTraces?.setScope(chatState.currentAccount, chatState.currentUser, chatState.view);
    clearTimeout(startupAccountRetryTimer);
    startupAccountRetryTimer = null;
    const nextSessions = new Map();
    for (const session of data.sessions) if (session?.username) nextSessions.set(session.username, session);
    let remembered = null;
    try { remembered = localStorage.getItem(`last-conversation:${chatState.currentAccount}`); } catch {}
    const preferredSession = () => chatState.selectedConversations.has(chatState.currentUser) && nextSessions.has(chatState.currentUser) ? chatState.currentUser :
      chatState.selectedConversations.has(remembered) && nextSessions.has(remembered) ? remembered :
      [...nextSessions.keys()].find(id => chatState.selectedConversations.has(id)) || null;
    const selected = preferredSession();
    if (!wasPartial && signature === chatState.sessionSignature &&
        selected === chatState.currentUser && (!selected || sessionWindowReady(data.account, nextSessions.get(selected)))) {
      completeStartup();
      return;
    }
    const scroll = byId("sessionList").scrollTop;
    chatState.self = data.self || null;
    setAvatar("selfAvatar", chatState.self?.avatar, chatState.self?.avatarCandidates, chatState.self?.name || "我");
    if (!nextSessions.size) {
      chatState.sessions.clear();
      chatState.sessionSignature = signature;
      renderSessions();
      if (!byId("conversationManager").hidden) renderConversationManager();
      clearUnselectedConversation();
      completeStartup();
      return;
    }
    chatState.sessions.clear();
    for (const [user, session] of nextSessions) chatState.sessions.set(user, session);
    window.ChatComposer?.setScope(chatState.currentAccount, chatState.currentUser, {
      available: chatState.messageSourceReady && chatState.view === "chat", isGroup: chatState.sessions.get(chatState.currentUser)?.isGroup === true });
    chatState.sessionSignature = signature;
    pruneSessionCache(data.account, nextSessions);
    renderSessions();
    if (!byId("conversationManager").hidden) renderConversationManager();
    void trackNewConversations(data.account);
    byId("sessionList").scrollTop = scroll;
    // First paint waits only for its selected conversation; other windows load when opened.
    const firstWindow = selected ? new Map([[selected, nextSessions.get(selected)]]) : new Map();
    if (!await preloadSessionWindows(data.account, firstWindow, request)) return;
    if (request !== chatState.sessionRequest || chatState.currentAccount !== data.account) return;
    // A click during the awaited read takes priority over the initial remembered choice.
    const activeSelected = preferredSession();
    if (!activeSelected) clearUnselectedConversation();
    else if (activeSelected !== chatState.currentUser) switchSession(activeSelected, accountChanged);
    if (chatState.currentUser && chatState.sessions.has(chatState.currentUser)) text("chatTitle", chatState.sessions.get(chatState.currentUser).name || chatState.currentUser);
    completeStartup();
  } catch (error) {
    if (request !== chatState.sessionRequest) return;
    if (window.desktopHost?.isSettingsWindow) {
      if (accountChangedError(error) || accountUnavailableError(error)) {
        chatState.conversationSelectionEpoch++;
        chatState.conversationSelectionBusy = false;
        if (byId("conversationActionConfirm")?.open) byId("conversationActionConfirm").close("cancel");
        chatState.currentAccount = null;
        chatState.selectionLoadedAccount = null;
        chatState.sessions.clear();
        chatState.selectedConversations.clear();
        chatState.messageSourceReady = false;
        accountUnavailable = true;
        window.ModelTraces?.setScope(null, null, "settings");
        void window.MediaSettings?.refresh?.();
        renderConversationManager();
      }
      accountCheckStatus(error.preparation?.message || "微信账号暂不可用，请重试", true);
      return;
    }
    if (accountChangedError(error)) {
      resetAccountView("微信账号已变化，正在读取会话…");
      if (retryChanged !== false) followup = false;
      else showStartup("account", "微信账号已变化，请重试", { retry: true });
    } else if (accountUnavailableError(error)) {
      const reason = error.preparation?.message || "当前微信账号未就绪";
      // Missing WeChat or an unconfirmed login needs an explicit instruction, not a loading message.
      const preparing = error.preparation?.state === "preparing" || error.preparation?.reason === "preparing";
      const requiresAction = !preparing && ["wechat_not_running", "active_account_unavailable", "keys_incomplete", "scan_limit", "process_read_denied"].includes(error.preparation?.reason);
      const autoRetry = !requiresAction && startupActive && (preparing || !startupAccountRetryUsed);
      if (!accountUnavailable || chatState.currentAccount !== null || chatState.sessions.size) resetAccountView(reason);
      accountUnavailable = true;
      renderAccountUnavailable(error);
      if (autoRetry) {
        startupAccountRetryUsed = true;
        showStartup("account", preparing ? reason : "正在重试连接微信…");
        const attempt = startupAttempt;
        const pendingRequest = chatState.sessionRequest;
        startupAccountRetryTimer = setTimeout(() => {
          startupAccountRetryTimer = null;
          if (startupActive && startupAttempt === attempt && chatState.sessionRequest === pendingRequest &&
              accountUnavailable && !accountClearedExiting) {
            void loadSessions();
          }
        }, 2500);
      } else showStartup("account", reason, { retry: true, continueEmpty: true });
    } else if (contactSnapshotStaleError(error)) {
      if (chatState.currentAccount !== null || chatState.sessions.size || !startupActive) {
        resetAccountView("联系人资料更新中…", true);
      }
      status(byId("sessionList"), "联系人资料更新中…", () => { void loadSessions(); });
      showStartup("sessions", "联系人资料更新中…", { retry: true });
      const pendingRequest = chatState.sessionRequest;
      setTimeout(() => {
        if (pendingRequest === chatState.sessionRequest && !accountClearedExiting) void loadSessions();
      }, 2000);
    } else {
      if (!chatState.sessions.size && !accountUnavailable) status(byId("sessionList"), "会话读取失败，请重试", () => { void loadSessions(); });
      showStartup(chatState.currentAccount && chatState.preloadTotal ? "messages" : "sessions",
        chatState.currentAccount && chatState.preloadTotal ? `聊天记录准备失败 ${chatState.preloadDone}/${chatState.preloadTotal}，请重试` : "会话读取失败，请重试", { retry: true });
    }
  } finally {
    chatState.sessionLoading = false;
    const queued = chatState.sessionRefreshQueued;
    chatState.sessionRefreshQueued = false;
    if (followup !== null) void loadSessions(followup);
    else if (queued) void loadSessions();
  }
}
function messageNode(message) {
  const session = chatState.sessions.get(chatState.currentUser);
  const item = element("div", `msg-item ${message.side === "self" ? "outgoing" : "incoming"}`);
  item.dataset.messageId = String(message.id);
  const avatarColumn = element("div", "msg-avatar-column");
  avatarColumn.appendChild(message.side === "self" ? avatar(chatState.self?.avatar, "msg-avatar", chatState.self?.avatarCandidates, chatState.self?.name || "我") : avatar(message.senderAvatar || (session?.isGroup ? null : session?.avatar), "msg-avatar", message.senderAvatarCandidates || (session?.isGroup ? [] : session?.avatarCandidates), message.senderName || session?.name || message.senderId, session?.isGroup && !message.senderId));
  item.appendChild(avatarColumn);
  const wrap = element("div", "msg-content-wrap");
  if (session?.isGroup && message.side !== "self") wrap.appendChild(element("span", "msg-sender", message.senderName || message.senderId || "未知成员"));
  // Know Your Friends, 2026-10-10: local media bubbles keep full bytes behind a user gesture.
  const media = window.ChatMedia?.render(message,
    { account: chatState.currentAccount, user: chatState.currentUser, generation: chatState.generation },
    scope => scope.account === chatState.currentAccount && scope.user === chatState.currentUser &&
      scope.generation === chatState.generation);
  wrap.appendChild(media || window.MessageLinks?.render(message) || element("div", "msg-bubble", message.kind === "image" ? "[图片]" :
    message.kind === "text" ? message.text || "" : message.text || "[不支持的消息]"));
  item.appendChild(wrap);
  window.MessageAnalysis?.attach(message, item);
  window.MessageMenu?.attach(item, message);
  return item;
}
function renderMessages(next, restoreScroll = null, historyAnchor = null) {
  const container = byId("chatMessages");
  const previousMessages = chatState.messages;
  const oldIds = chatState.messages.map(message => String(message.id));
  const nextIds = next.map(message => String(message.id));
  const appendOnly = oldIds.length && oldIds.length <= nextIds.length && oldIds.every((id, index) => id === nextIds[index] && JSON.stringify(chatState.messages[index]) === JSON.stringify(next[index])) && container.querySelectorAll(".msg-item").length === oldIds.length;
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80 && !window.MessageAnalysis?.isReading();
  chatState.followLatest = historyAnchor ? false : restoreScroll ? restoreScroll.followLatest : atBottom || !oldIds.length;
  const scroll = container.scrollTop;
  const anchor = !atBottom && !appendOnly ? [...container.querySelectorAll(".msg-item")].find(node => node.getBoundingClientRect().bottom >= container.getBoundingClientRect().top) : null;
  const anchorId = anchor?.dataset.messageId;
  const anchorOffset = anchor ? anchor.getBoundingClientRect().top - container.getBoundingClientRect().top : 0;
  const start = appendOnly ? oldIds.length : 0;
  // Retain unchanged bubbles in place so polling cannot detach selection, focus or media.
  const previousById = new Map(previousMessages.map(message => [String(message.id), message]));
  const retained = new Map([...container.querySelectorAll(".msg-item")].map(node => [node.dataset.messageId, node]));
  const desired = [];
  const fragment = document.createDocumentFragment();
  try {
    chatState.messages = next;
    for (let index = start; index < next.length; index++) {
      const message = next[index];
      const previous = next[index - 1];
      if (!previous || Number(message.time) - Number(previous.time) > 300000) {
        const row = element("div", "msg-time-row");
        row.appendChild(element("span", "msg-time", time(message.time)));
        desired.push(row);
      }
      const existing = retained.get(String(message.id));
      desired.push(existing && JSON.stringify(previousById.get(String(message.id))) === JSON.stringify(message)
        ? existing : messageNode(message));
    }
  } catch (error) {
    chatState.messages = previousMessages;
    throw error;
  }
  if (!next.length) status(container, "暂无消息");
  else if (appendOnly) { fragment.append(...desired); container.appendChild(fragment); }
  else {
    const keep = new Set(desired);
    for (const child of [...container.children]) if (!keep.has(child)) child.remove();
    for (let index = 0; index < desired.length; index++) {
      if (container.children[index] !== desired[index]) container.insertBefore(desired[index], container.children[index] || null);
    }
  }
  window.MessageAnalysis?.sync(next);
  if (next.length && historyAnchor) {
    const anchor = [...container.querySelectorAll(".msg-item")].find(node => node.dataset.messageId === historyAnchor.id);
    if (anchor) container.scrollTop += anchor.getBoundingClientRect().top - container.getBoundingClientRect().top - historyAnchor.top;
    chatState.lastChatScrollTop = container.scrollTop;
  } else if (next.length && restoreScroll) {
    const token = chatState.generation;
    const user = chatState.currentUser;
    requestAnimationFrame(() => {
      if (token !== chatState.generation || user !== chatState.currentUser || chatState.view !== "chat") return;
      container.scrollTop = restoreScroll.followLatest && !window.MessageAnalysis?.isReading() ? container.scrollHeight : restoreScroll.scrollTop;
      chatState.lastChatScrollTop = container.scrollTop;
    });
  } else if (next.length && (atBottom || !oldIds.length)) scrollToLatest();
  else if (next.length && anchorId && !appendOnly) {
    const replacement = [...container.querySelectorAll(".msg-item")].find(node => node.dataset.messageId === anchorId);
    container.scrollTop = replacement ? scroll + replacement.getBoundingClientRect().top - container.getBoundingClientRect().top - anchorOffset : scroll;
  } else container.scrollTop = scroll;
  chatState.lastChatScrollTop = container.scrollTop;
  updateHistoryNavigation();
}
function scrollToLatest() {
  if (window.MessageAnalysis?.isReading()) return;
  chatState.followLatest = true;
  const token = chatState.generation;
  const user = chatState.currentUser;
  requestAnimationFrame(() => { if (chatState.view === "chat" && token === chatState.generation && user === chatState.currentUser && !window.MessageAnalysis?.isReading()) { const container = byId("chatMessages"); container.scrollTop = container.scrollHeight; chatState.lastChatScrollTop = container.scrollTop; } });
}
function historyAnchor(fromEnd = false) {
  const container = byId("chatMessages");
  const bounds = container.getBoundingClientRect();
  const visible = [...container.querySelectorAll(".msg-item")].filter(node => {
    const rect = node.getBoundingClientRect();
    return rect.bottom > bounds.top && rect.top < bounds.bottom;
  });
  const node = fromEnd ? visible.at(-1) : visible[0];
  return node ? { id: node.dataset.messageId, top: node.getBoundingClientRect().top - bounds.top } : null;
}
function updateHistoryNavigation() {
  const firstCursor = chatState.messages[0]?.historyCursor;
  const state = chatState.historyState;
  const container = byId("chatMessages");
  const atTop = container.scrollTop <= historyLoadDistance;
  const message = chatState.historyController ? "读取中…" : state?.error ||
    (atTop && (state?.hasMoreBefore === false || (!state && firstCursor && chatState.currentHasMoreBefore === false)) ? "已到本机最早消息" : "");
  byId("historyNavigation").hidden = !chatState.currentUser || !message;
  byId("btnHistoryRetry").hidden = !state?.error;
  byId("btnHistoryRetry").disabled = !!chatState.historyController;
  byId("historyNavigation").classList.toggle("at-bottom", chatState.historyDirection === "newer");
  byId("btnReturnLatest").hidden = !state;
  text("historyNavStatus", message);
  if (!chatState.historyPrefetch && !chatState.historyPrefetchTimer && !chatState.historyController && !state?.error)
    scheduleHistoryPrefetch();
}
function cancelHistoryRequest() {
  chatState.historyRequest++;
  chatState.historyController?.abort();
  chatState.historyController = null;
  chatState.historyPrefetch?.controller.abort();
  chatState.historyPrefetch = null;
  clearTimeout(chatState.historyPrefetchTimer);
  chatState.historyPrefetchTimer = null;
  if (chatState.historyScrollFrame != null) cancelAnimationFrame(chatState.historyScrollFrame);
  chatState.historyScrollFrame = null;
  chatState.historyDirection = null;
}
function historyPageDescriptor(direction) {
  if (!chatState.currentAccount || !chatState.currentUser || !chatState.messages.length) return null;
  const state = chatState.historyState;
  if (direction === "older" && (state?.hasMoreBefore === false || (!state && chatState.currentHasMoreBefore === false))) return null;
  if (direction === "newer" && !state?.hasMoreAfter) return null;
  const cursor = direction === "older" ? state?.beforeCursor || chatState.messages[0]?.historyCursor :
    state?.afterCursor || chatState.messages.at(-1)?.historyCursor;
  if (!cursor) return null;
  const account = chatState.currentAccount, user = chatState.currentUser, generation = chatState.generation;
  return { account, user, generation, direction, cursor, key: JSON.stringify([account, user, generation, direction, cursor]) };
}
function prefetchHistory(direction = "older") {
  const page = historyPageDescriptor(direction);
  if (!page) return null;
  if (chatState.historyPrefetch?.key === page.key) return chatState.historyPrefetch;
  if (chatState.historyController) return null;
  chatState.historyPrefetch?.controller.abort();
  const entry = { ...page, controller: new AbortController(), data: null, error: null };
  chatState.historyPrefetch = entry;
  entry.promise = (async () => {
    let cursor = page.cursor, data;
    // Skip bounded runs of non-displayable rows without monopolizing the source.
    for (let skipped = 0; skipped < 4; skipped++) {
      data = await api(historyUrl(page.account, page.user, direction === "older" ? "before" : "after", cursor), {}, entry.controller.signal);
      if (entry.controller.signal.aborted) throw new DOMException("History cancelled", "AbortError");
      historyResponseValid(data, page.account, page.user);
      const more = direction === "older" ? data.hasMoreBefore : data.hasMoreAfter;
      if (more && (!data.nextCursor || data.nextCursor === cursor)) throw new Error("History cursor did not advance");
      if (data.messages.length || !more) break;
      cursor = data.nextCursor;
    }
    entry.data = data;
    return data;
  })().catch(error => { entry.error = error; return null; });
  return entry;
}
function scheduleHistoryPrefetch(direction = "older") {
  clearTimeout(chatState.historyPrefetchTimer);
  chatState.historyPrefetchTimer = setTimeout(() => {
    chatState.historyPrefetchTimer = null;
    if (chatState.view !== "chat" || document.hidden || chatState.historyController || chatState.historyState?.error) return;
    prefetchHistory(direction);
  }, 300);
}
function maybeLoadHistory(direction) {
  if (chatState.view !== "chat" || chatState.historyController || chatState.historyState?.error) return;
  const container = byId("chatMessages");
  const distance = direction === "older" ? container.scrollTop : container.scrollHeight - container.scrollTop - container.clientHeight;
  if (direction === "newer" && chatState.historyState && !chatState.historyState.hasMoreAfter && distance <= 2) {
    returnToLatest();
    return;
  }
  if (!historyPageDescriptor(direction)) return;
  if (distance <= historyLoadDistance) void (direction === "older" ? loadOlderHistory() : loadNewerHistory());
  else if (distance <= historyPrefetchDistance) scheduleHistoryPrefetch(direction);
}
function queueHistoryScroll(direction) {
  if (chatState.historyScrollFrame != null) cancelAnimationFrame(chatState.historyScrollFrame);
  chatState.historyScrollFrame = requestAnimationFrame(() => {
    chatState.historyScrollFrame = null;
    maybeLoadHistory(direction);
  });
}
function retryHistory() {
  const direction = chatState.historyState?.errorDirection || "older";
  if (!chatState.historyState?.error || chatState.historyController) return;
  chatState.historyPrefetch?.controller.abort();
  chatState.historyPrefetch = null;
  chatState.historyState.error = "";
  void (direction === "older" ? loadOlderHistory() : loadNewerHistory());
}
function enterHistoryView() {
  if (chatState.historyState) return;
  chatState.historyState = { beforeCursor: chatState.messages[0]?.historyCursor || null, hasMoreBefore: true, hasMoreAfter: false, error: "" };
  chatState.messageRequest++;
  chatState.messagePending = false;
  chatState.messageRefreshQueued = false;
  chatState.followLatest = false;
  updateHistoryNavigation();
}
function preserveAnalysisReading(next) {
  const target = window.MessageAnalysis?.openedMessageId?.();
  if (!target || chatState.historyState || !chatState.messages.some(message => String(message.id) === target) ||
      next.some(message => String(message.id) === target)) return false;
  // A sliding latest-message window must not evict the result the user is reading.
  const afterCursor = chatState.messages.at(-1)?.historyCursor || null;
  enterHistoryView();
  chatState.historyState.afterCursor = afterCursor;
  chatState.historyState.hasMoreAfter = true;
  chatState.historyState.notice = "新消息已到达，当前保留正在查看的消息";
  updateHistoryNavigation();
  return true;
}
function historyResponseValid(data, account, user) {
  if (data?.account !== account || data?.user !== user || !Array.isArray(data.messages)) throw new Error("Invalid history response");
}
function historyUrl(account, user, key, cursor) {
  const params = new URLSearchParams({ account, user, limit: "80" });
  params.set(key, cursor);
  return `/api/history?${params}`;
}
async function loadOlderHistory() {
  if (!chatState.currentAccount || !chatState.currentUser || chatState.historyController || chatState.historyState?.hasMoreBefore === false) return;
  if (byId("chatMessages").scrollTop > historyLoadDistance) return;
  const initialCursor = chatState.historyState?.beforeCursor || chatState.messages[0]?.historyCursor;
  if (!initialCursor) return;
  const entry = prefetchHistory("older");
  if (!entry) return;
  enterHistoryView();
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historyRequest;
  const controller = entry.controller;
  chatState.historyController = controller;
  chatState.historyDirection = "older";
  chatState.historyState.error = "";
  updateHistoryNavigation();
  let movedAway = false;
  try {
    const data = await entry.promise;
    if (request !== chatState.historyRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
    if (!data) throw entry.error;
    // The reader may have moved away while this page was in flight; keep it prefetched.
    if (byId("chatMessages").scrollTop > historyLoadDistance) { movedAway = true; return; }
    const existing = new Set(chatState.messages.map(message => String(message.id)));
    const older = data.messages.filter(message => !existing.has(String(message.id)));
    const combined = [...older, ...chatState.messages];
    const removedFromEnd = Math.max(0, combined.length - historyWindowLimit);
    const next = combined.slice(0, historyWindowLimit);
    const anchor = historyAnchor();
    chatState.historyState.beforeCursor = data.nextCursor || older[0]?.historyCursor || initialCursor;
    chatState.historyState.hasMoreBefore = !!data.hasMoreBefore;
    chatState.historyState.hasMoreAfter ||= removedFromEnd > 0;
    if (removedFromEnd) chatState.historyState.afterCursor = next.at(-1)?.historyCursor;
    chatState.historyState.notice = data.hasMoreBefore ? "" : "已到本机最早消息";
    if (older.length) renderMessages(next, null, anchor);
    chatState.historyPrefetch = null;
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historyRequest && token === chatState.generation) {
      chatState.historyState.error = "读取失败";
      chatState.historyState.errorDirection = "older";
    }
  } finally {
    if (request === chatState.historyRequest) {
      chatState.historyController = null;
      updateHistoryNavigation();
      if (!chatState.historyState?.error) scheduleHistoryPrefetch("older");
      // A user can reach the live edge while the older-page request is pending.
      if (movedAway) maybeLoadHistory("newer");
    }
  }
}
async function loadNewerHistory() {
  if (!chatState.historyState?.hasMoreAfter || chatState.historyController || !chatState.messages.length) return;
  const container = byId("chatMessages");
  if (container.scrollHeight - container.scrollTop - container.clientHeight > historyLoadDistance) return;
  const entry = prefetchHistory("newer");
  if (!entry) return;
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historyRequest;
  const controller = entry.controller;
  chatState.historyController = controller;
  chatState.historyDirection = "newer";
  chatState.historyState.error = "";
  updateHistoryNavigation();
  try {
    const data = await entry.promise;
    if (request !== chatState.historyRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
    if (!data) throw entry.error;
    if (container.scrollHeight - container.scrollTop - container.clientHeight > historyLoadDistance) return;
    const existing = new Set(chatState.messages.map(message => String(message.id)));
    const newer = data.messages.filter(message => !existing.has(String(message.id)));
    const combined = [...chatState.messages, ...newer];
    const removedFromStart = Math.max(0, combined.length - historyWindowLimit);
    const next = combined.slice(removedFromStart);
    const anchor = historyAnchor(true);
    chatState.historyState.hasMoreBefore ||= removedFromStart > 0;
    chatState.historyState.beforeCursor = next[0]?.historyCursor || chatState.historyState.beforeCursor;
    chatState.historyState.afterCursor = data.nextCursor || next.at(-1)?.historyCursor;
    chatState.historyState.hasMoreAfter = !!data.hasMoreAfter;
    if (newer.length) renderMessages(next, null, anchor);
    chatState.historyPrefetch = null;
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historyRequest && token === chatState.generation) {
      chatState.historyState.error = "读取失败";
      chatState.historyState.errorDirection = "newer";
    }
  } finally {
    if (request === chatState.historyRequest) {
      chatState.historyController = null;
      updateHistoryNavigation();
      if (!chatState.historyState?.error) scheduleHistoryPrefetch("newer");
    }
  }
}
async function jumpToHistory(cursor, messageId) {
  if (!chatState.currentAccount || !chatState.currentUser || !cursor) return;
  const previous = chatState.historyState;
  enterHistoryView();
  cancelHistoryRequest();
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historyRequest;
  const controller = new AbortController();
  chatState.historyController = controller;
  chatState.historyState.error = "";
  updateHistoryNavigation();
  try {
    const data = await api(historyUrl(account, user, "around", cursor), {}, controller.signal);
    if (request !== chatState.historyRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
    historyResponseValid(data, account, user);
    const target = data.messages.find(message => String(message.id) === String(messageId));
    if (!target) throw new Error("History target missing");
    chatState.historyState = { beforeCursor: data.nextCursor || data.messages[0]?.historyCursor || null,
      hasMoreBefore: !!data.hasMoreBefore, hasMoreAfter: !!data.hasMoreAfter, error: "" };
    renderMessages(data.messages, null, { id: String(messageId), top: byId("chatMessages").clientHeight / 2 });
    const hit = [...byId("chatMessages").querySelectorAll(".msg-item")].find(node => node.dataset.messageId === String(messageId));
    hit?.classList.add("history-hit");
    setTimeout(() => hit?.classList.remove("history-hit"), 2500);
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historyRequest && token === chatState.generation) {
      chatState.historyState = previous;
      if (!previous) void loadMessages(token, true);
      toast("记录定位失败，请重试");
    }
  } finally {
    if (request === chatState.historyRequest) {
      chatState.historyController = null;
      updateHistoryNavigation();
    }
  }
}
function returnToLatest() {
  if (!chatState.historyState) return;
  window.MessageAnalysis?.closeReading?.();
  cancelHistoryRequest();
  chatState.historyState = null;
  const cached = chatState.sessionCache.get(sessionCacheKey(chatState.currentAccount, chatState.currentUser));
  chatState.conversationMood = cached?.mood || null;
  if (Array.isArray(cached?.messages)) renderMessages(cached.messages, { followLatest: true, scrollTop: 0 });
  else status(byId("chatMessages"), "正在读取消息…");
  chatState.followLatest = true;
  updateHistoryNavigation();
  void loadMessages(chatState.generation, !Array.isArray(cached?.messages));
}
let historyBrowser = null;
let historyWindowRequest = 0;
function cancelHistorySearch() {
  historyBrowser?.cancel();
}
function closeHistorySearch() {
  historyWindowRequest++;
  historyBrowser?.destroy();
  historyBrowser = null;
  const dialog = byId("historySearchPanel");
  if (dialog?.open) dialog.close();
  byId("btnChatHistory").setAttribute("aria-expanded", "false");
}
function resetHistorySearch() {
  closeHistorySearch();
}
async function openHistorySearch() {
  if (!chatState.currentUser || !chatState.currentAccount) { toast("请先选择会话"); return; }
  const session = chatState.sessions.get(chatState.currentUser);
  const scope = { account: chatState.currentAccount, user: chatState.currentUser,
    name: String(session?.name || chatState.currentUser).slice(0, 128), theme: settingsState.settings.theme };
  if (typeof window.desktopHost?.openHistoryWindow === "function") {
    const request = ++historyWindowRequest;
    try {
      const opened = await window.desktopHost.openHistoryWindow(scope);
      if (request === historyWindowRequest && opened !== true) toast("聊天记录窗口未能打开");
    } catch { if (request === historyWindowRequest) toast("聊天记录窗口未能打开"); }
    return;
  }
  const dialog = byId("historySearchPanel");
  if (dialog.open) { closeHistorySearch(); return; }
  dialog.showModal();
  byId("btnChatHistory").setAttribute("aria-expanded", "true");
  historyBrowser = window.ChatHistory.create({ container: dialog, api,
    scope: { ...scope, avatar: session?.isGroup ? null : session?.avatar,
      avatarCandidates: session?.isGroup ? [] : session?.avatarCandidates, self: chatState.self },
    onClose: closeHistorySearch,
    onLocate: target => { closeHistorySearch(); void locateFromFriend(target); },
    validateScope: async () => scope.account === chatState.currentAccount && scope.user === chatState.currentUser });
  historyBrowser.focus();
}
async function loadMessages(token = chatState.generation, poll = false, refresh = false) {
  if (window.desktopHost?.isSettingsWindow) return;
  if (!chatState.currentUser || token !== chatState.generation || chatState.historyState) return;
  if (chatState.messagePending) {
    if (refresh) chatState.messageRefreshQueued = true;
    return;
  }
  chatState.messagePending = true;
  const request = ++chatState.messageRequest;
  const windowSerial = ++chatState.windowRequestSerial;
  const user = chatState.currentUser;
  const signal = chatState.controller.signal;
  try {
    const data = await api(`/api/messages?user=${encodeURIComponent(user)}&limit=80`, {}, signal);
    if (token !== chatState.generation || request !== chatState.messageRequest || chatState.historyState) return;
    if (!acceptResponseAccount(data.account)) return;
    if (!Array.isArray(data.messages)) throw new Error("Invalid messages response");
    const next = data.messages;
    if (poll && !next.length && chatState.messages.length && ++chatState.emptyMessagePolls < 2) return;
    if (next.length) chatState.emptyMessagePolls = 0;
    if (typeof data.hasMoreBefore === "boolean") chatState.currentHasMoreBefore = data.hasMoreBefore;
    const changed = JSON.stringify(next) !== JSON.stringify(chatState.messages);
    const snapshot = { messages: next,
      ...(typeof data.hasMoreBefore === "boolean" ? { hasMoreBefore: data.hasMoreBefore } : {}),
      summarySignature: sessionSummarySignature(chatState.sessions.get(user)), windowSerial };
    if (window.MessageAnalysis?.isReading()) {
      cacheCurrentSession(snapshot);
      if (preserveAnalysisReading(next)) return;
    }
    if (!poll || changed) renderMessages(next);
    cacheCurrentSession(snapshot);
    markSessionAsRead(user);
    renderSessions();
  } catch (error) {
    if (error.name !== "AbortError" && token === chatState.generation && request === chatState.messageRequest) {
      if (handleAccountBoundaryError(error)) return;
      if (!poll) status(byId("chatMessages"), "消息读取失败，请重试", () => loadMessages(token));
      else notifyStatus("消息读取失败，请重试");
    }
  } finally {
    if (token === chatState.generation && request === chatState.messageRequest) {
      chatState.messagePending = false;
      if (chatState.messageRefreshQueued) {
        chatState.messageRefreshQueued = false;
        void loadMessages(token, true);
      }
    }
  }
}
function switchSession(user, force = false) {
  if (window.desktopHost?.isSettingsWindow) return;
  if (!chatState.sessions.has(user)) return;
  if (!chatState.messageSourceReady) {
    text("chatTitle", chatState.sessions.get(user).name || user);
    status(byId("chatMessages"), "聊天记录尚未就绪，正在重试…");
    return;
  }
  try { localStorage.setItem(`last-conversation:${chatState.currentAccount}`, user); } catch {}
  markSessionAsRead(user);
  if (user === chatState.currentUser && !force) {
    renderSessions();
    return;
  }
  window.ChatMedia?.reset();
  cacheCurrentSession({ scrollTop: chatState.historyState ? 0 : byId("chatMessages").scrollTop, followLatest: chatState.historyState ? true : chatState.followLatest });
  const cached = chatState.sessionCache.get(sessionCacheKey(chatState.currentAccount, user));
  cancelHistoryRequest();
  chatState.historyState = null;
  resetHistorySearch();
  chatState.controller?.abort();
  chatState.controller = new AbortController();
  chatState.advance("generation");
  chatState.messagePending = false;
  chatState.messageRefreshQueued = false;
  chatState.emptyMessagePolls = 0;
  chatState.currentUser = user;
  window.ChatComposer?.setScope(chatState.currentAccount, user, {
    available: chatState.messageSourceReady, isGroup: chatState.sessions.get(user)?.isGroup === true, force });
  window.MessageAnalysis?.setScope(chatState.currentAccount, user, chatState.sessions.get(user)?.isGroup === true);
  byId("btnChatHistory").disabled = false;
  updateFriendButton(user);
  chatState.currentHasMoreBefore = typeof cached?.hasMoreBefore === "boolean" ? cached.hasMoreBefore : null;
  chatState.messages = [];
  chatState.followLatest = cached?.followLatest ?? true;
  chatState.lastChatScrollTop = cached?.scrollTop || 0;
  text("chatTitle", chatState.sessions.get(user).name || user);
  if (Array.isArray(cached?.messages)) renderMessages(cached.messages, cached);
  else status(byId("chatMessages"), "正在读取消息…");
  updateHistoryNavigation();
  renderSessions();
  window.PluginSlots?.onSessionChanged(chatState.currentAccount, user, pluginFriendName(user));
  window.ExternalImportHost?.onSessionChanged(chatState.currentAccount, user, pluginFriendName(user));
  window.ModelTraces?.setScope(chatState.currentAccount, user, chatState.view);
  void loadMessages(chatState.generation, !!cached);
}
let friendWindowRequest = 0;
// Modified by Know Your Friends, 2026-10-10: “知己分析” opens a fixed window per contact
// instead of the docked portrait. Group chats do not offer it in this release.
function updateFriendButton(user = chatState.currentUser) {
  const group = !!chatState.sessions.get(user)?.isGroup;
  for (const button of document.querySelectorAll(".plugin-header-btn")) {
    button.hidden = group;
    button.disabled = group || !chatState.messageSourceReady || !user;
    button.title = typeof window.desktopHost?.openFriendWindow === "function" ?
      `打开当前联系人的${button.textContent}` : `${button.textContent}需要在桌面客户端中打开`;
  }
}
async function openFriendWindow(plugin, id) {
  if (!window.PluginSlots?.hasFriendEntry(plugin, id)) return;
  if (!chatState.messageSourceReady || !chatState.currentUser) { toast("请先选择已就绪的会话"); return; }
  const session = chatState.sessions.get(chatState.currentUser);
  if (session?.isGroup) return;
  if (typeof window.desktopHost?.openFriendWindow !== "function") {
    toast("知己分析需要在桌面客户端中打开");
    return;
  }
  const request = ++friendWindowRequest;
  try {
    const avatar = typeof session?.avatar === "string" && session.avatar.length <= 512 &&
      /^https?:\/\/[^\s"'<>]+$/i.test(session.avatar) ? session.avatar : undefined;
    const opened = await window.desktopHost.openFriendWindow({ account: chatState.currentAccount,
      user: chatState.currentUser, name: String(session?.name || chatState.currentUser).slice(0, 128),
      ...(avatar ? { avatar } : {}) });
    if (request === friendWindowRequest && opened !== true) toast("知己分析窗口未能打开");
  } catch {
    if (request === friendWindowRequest) toast("知己分析窗口未能打开");
  }
}
// 知己分析 windows mask themselves when their account is no longer current or was cleared.
function announceFriendAccount(type, value) {
  try {
    if (typeof BroadcastChannel !== "function") return;
    announceFriendAccount.channel ||= new BroadcastChannel("kyf-friend-analysis");
    announceFriendAccount.channel.postMessage({ type, value: value ?? null });
  } catch { }
}
// A 知己分析 window asks to show a verified original message. Only the current
// account is accepted; the composer keeps each contact's unsent draft separately.
async function locateFromFriend(target) {
  if (!target || target.account !== chatState.currentAccount || !chatState.messageSourceReady) {
    toast("微信账号已变化，无法定位这条消息");
    return;
  }
  if (!chatState.sessions.has(target.user)) {
    toast("聊天列表中没有这位联系人，请先添加该会话");
    return;
  }
  if (chatState.view !== "chat") switchView("chat");
  if (target.user !== chatState.currentUser) switchSession(target.user);
  if (chatState.currentUser !== target.user || chatState.currentAccount !== target.account) return;
  await jumpToHistory(target.cursor, target.messageId);
}
function switchView(target) {
  const chatVisible = target === "chat";
  friendWindowRequest++;
  updateFriendButton();
  chatState.view = target;
  window.ChatComposer?.setScope(chatState.currentAccount, chatState.currentUser, {
    available: chatVisible && chatState.messageSourceReady, isGroup: chatState.sessions.get(chatState.currentUser)?.isGroup === true, force: true });
  byId("chatView").classList.toggle("active", chatVisible);
  if (chatVisible) window.ComposerResize?.refresh();
  byId("navChat").classList.toggle("active", chatVisible);
  if (chatVisible && !chatState.historyState) scrollToLatest();
  // Modified 2026-10-13: "plugin:<plugin>/<nav>" shows a plugin's nav page (plugin-slots.js).
  window.PluginSlots?.onViewChanged(target);
  window.ExternalImportHost?.onViewChanged(target);
  window.ModelTraces?.setScope(chatState.currentAccount, chatState.currentUser, target);
}
// The friend's display name in the context handed to plugin pages.
function pluginFriendName(user) {
  return user ? String(chatState.sessions.get(user)?.name || user).slice(0, 128) : "";
}
function applySettings() {
  document.body.classList.toggle("theme-light", settingsState.settings.theme === "light");
  document.documentElement.classList.toggle("desktop-host", !!window.desktopHost);
  document.documentElement.style.zoom = settingsState.settings.zoom;
  document.documentElement.style.setProperty("--zoom-inverse", String(1 / Number(settingsState.settings.zoom)));
  window.SessionResize?.refresh();
  window.ComposerResize?.refresh();
  window.desktopHost?.setTheme?.(settingsState.settings.theme);
  window.ExternalImportHost?.theme(settingsState.settings.theme);
  byId("selectThemeMode").value = settingsState.settings.theme;
  byId("selectZoomLevel").value = settingsState.settings.zoom;
}
function dataRootControlsBusy(busy) {
  settingsState.dataRootBusy = busy;
  for (const id of ["inputDataRoot", "btnBrowseDataRoot", "btnSaveDataRoot", "btnClearDataRoot"])
    byId(id).disabled = busy;
  if (typeof window.desktopHost?.chooseDataRoot !== "function") byId("btnBrowseDataRoot").disabled = true;
}
function showDataRoot(data) {
  if (!data || !["unset", "ready", "missing", "invalid"].includes(data.state) ||
      typeof data.path !== "string" || !Number.isInteger(data.accounts) || data.accounts < 0)
    throw new Error("数据目录状态无效");
  text("dataRootStatus", data.state === "ready" ? `已设置 · 发现 ${data.accounts} 个账号目录` :
    data.state === "missing" ? "所选目录不可用" : data.state === "invalid" ? "配置不可用，请重新选择或恢复自动" : "自动发现");
  byId("dataRootStatus").title = data.path;
  byId("inputDataRoot").value = data.path;
  settingsState.dataRootDraftDirty = false;
}
async function loadDataRoot() {
  if (settingsState.dataRootBusy) return;
  const request = ++settingsState.dataRootRequest;
  dataRootControlsBusy(false);
  try {
    const data = await api("/api/data-root");
    if (request !== settingsState.dataRootRequest || settingsState.dataRootDraftDirty) return;
    showDataRoot(data);
  } catch {
    if (request === settingsState.dataRootRequest) text("dataRootStatus", "读取失败，请重新打开设置重试");
  }
}
async function changeDataRoot(clear = false) {
  if (settingsState.dataRootBusy) return;
  const value = byId("inputDataRoot").value.trim();
  if (!clear && !value) { text("dataRootStatus", "请选择微信数据目录"); return; }
  const request = ++settingsState.dataRootRequest;
  dataRootControlsBusy(true);
  text("dataRootStatus", clear ? "正在恢复自动发现…" : "正在校验路径…");
  try {
    const data = await api(clear ? "/api/data-root/clear" : "/api/data-root", {
      method: "POST", body: JSON.stringify(clear ? {} : { path: value }),
    });
    if (request === settingsState.dataRootRequest) {
      showDataRoot(data);
      toast(clear ? "已恢复自动发现" : "聊天记录路径已保存");
    }
    // A directory is only a discovery hint. The normal account-boundary flow
    // decides whether a live account is available before displaying any data.
    void loadSessions();
  } catch (error) {
    if (request === settingsState.dataRootRequest)
      text("dataRootStatus", error.message || "路径设置失败，请重试");
  } finally {
    dataRootControlsBusy(false);
    if (request !== settingsState.dataRootRequest && byId("settingsModal").classList.contains("show"))
      void loadDataRoot();
  }
}
async function browseDataRoot() {
  if (settingsState.dataRootBusy || typeof window.desktopHost?.chooseDataRoot !== "function") return;
  const request = ++settingsState.dataRootRequest;
  dataRootControlsBusy(true);
  try {
    const directory = await window.desktopHost.chooseDataRoot();
    if (directory && request === settingsState.dataRootRequest) {
      byId("inputDataRoot").value = directory;
      settingsState.dataRootDraftDirty = true;
    }
  } catch {
    if (request === settingsState.dataRootRequest) text("dataRootStatus", "无法打开目录选择器");
  } finally { dataRootControlsBusy(false); }
}
byId("btnBrowseDataRoot").addEventListener("click", () => void browseDataRoot());
byId("btnSaveDataRoot").addEventListener("click", () => void changeDataRoot());
byId("btnClearDataRoot").addEventListener("click", () => void changeDataRoot(true));
byId("inputDataRoot").addEventListener("input", () => { settingsState.dataRootDraftDirty = true; });
byId("inputDataRoot").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); void changeDataRoot(); }
});
const MODEL_SOURCE_PROTOCOLS = new Set(["anthropic", "responses", "chat_completions", "gemini", "ollama"]);
settingsState.modelSourceSnapshot = { mode: "none", api: null, sourceId: "local:laya", status: "idle" };
settingsState.modelSourceResolved = false;
settingsState.modelSourceReadRequest = 0;
settingsState.modelSourceLoadController = null;
settingsState.modelSourceRevision = 0;
settingsState.modelListRequest = 0;
settingsState.modelListController = null;
settingsState.modelTestRequest = 0;
settingsState.modelTestController = null;
settingsState.apiKeyRevealRequest = 0;
settingsState.apiKeyRevealController = null;
settingsState.apiKeyRevealedSaved = false;
settingsState.modelSourceLoading = false;
settingsState.modelSourceBusy = false;
settingsState.modelListBusy = false;
settingsState.modelTestBusy = false;
settingsState.modelSourceDraftDirty = false;
settingsState.modelDetailId = null;
settingsState.localJudgePoll = null;
settingsState.localJudgeError = "";
function validModelSource(data) {
  return !!data && ["none", "api"].includes(data.mode) &&
    typeof data.sourceId === "string" && !!data.sourceId &&
    (data.mode !== "api" || !!data.api) &&
    (data.api === null || (!!data.api && MODEL_SOURCE_PROTOCOLS.has(data.api.protocol) &&
      typeof data.api.baseUrl === "string" && typeof data.api.model === "string" &&
      (data.api.contextTokens == null || Number.isSafeInteger(data.api.contextTokens) &&
        data.api.contextTokens >= 4096 && data.api.contextTokens <= 1000000) &&
      (data.api.timeoutMs === undefined || Number.isSafeInteger(data.api.timeoutMs) &&
        data.api.timeoutMs >= 60_000 && data.api.timeoutMs <= 10_800_000) &&
      typeof data.api.hasKey === "boolean"));
}
// The saved generation model every plugin reaches through Core's model routing.
function applyActiveModelSource(data) {
  settingsState.modelSourceSnapshot = data;
  settingsState.modelSourceResolved = true;
}
function beginUnknownModelSource() {
  hideApiKey();
  settingsState.modelSourceResolved = false;
  updateModelSourceControls();
}
function updateModelSourceControls() {
  byId("btnReloadModelSource").hidden = settingsState.modelSourceResolved || settingsState.modelSourceLoading;
  for (const id of ["selectApiProtocol", "inputApiBaseUrl", "inputApiKey", "selectApiModel", "inputApiModelId",
    "inputApiContextTokens", "inputApiTimeoutMinutes", "inputApiConcurrency"])
    byId(id).disabled = settingsState.modelSourceLoading || settingsState.modelSourceBusy ||
      (id === "selectApiModel" && byId(id).options.length < 2);
  byId("btnFetchApiModels").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelListBusy;
  byId("btnTestApiModel").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelTestBusy;
  byId("btnSaveApiModel").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelTestBusy;
  byId("btnDeleteApiModel").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy;
  for (const id of ["selectRoleMain", "selectRoleJudge", "selectRoleImage"])
    byId(id).disabled = settingsState.modelSourceLoading || settingsState.modelSourceBusy || !settingsState.modelSourceResolved;
  byId("btnAddModel").disabled = settingsState.modelSourceLoading || settingsState.modelSourceBusy || !settingsState.modelSourceResolved;
  syncApiKeyVisibility();
}
function clearModelList() {
  const select = byId("selectApiModel");
  select.replaceChildren();
  const option = document.createElement("option");
  option.value = "";
  option.textContent = "获取列表后选择";
  select.appendChild(option);
  select.value = "";
  select.disabled = true;
  text("btnFetchApiModels", "获取模型列表");
}
function matchesApiConnection(profile) {
  return !!profile && profile.protocol === byId("selectApiProtocol").value &&
    profile.baseUrl.replace(/\/+$/, "") === byId("inputApiBaseUrl").value.trim().replace(/\/+$/, "");
}
function selectSavedApiModel() {
  const select = byId("selectApiModel");
  const model = byId("inputApiModelId").value.trim();
  const saved = modelDetailEntry();
  if (model && saved?.model === model && matchesApiConnection(saved) &&
      !Array.from(select.options).some(option => option.value === model)) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = `${model}（已保存）`;
    select.appendChild(option);
  }
  select.value = Array.from(select.options).some(option => option.value === model) ? model : "";
}
function renderModelList(result, { restore = false } = {}) {
  clearModelList();
  const select = byId("selectApiModel");
  const known = new Set();
  for (const item of result.supported ? result.models : []) {
    if (!item || typeof item.id !== "string" || !item.id.trim() || known.has(item.id)) continue;
    known.add(item.id);
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = typeof item.name === "string" && item.name ? item.name : item.id;
    if (Number.isSafeInteger(item.contextTokens) && item.contextTokens >= 4096 && item.contextTokens <= 1000000)
      option.dataset.contextTokens = String(item.contextTokens);
    select.appendChild(option);
  }
  selectSavedApiModel();
  if (!restore) {
    const selected = Array.from(select.options).find(option => option.value === select.value);
    if (selected?.dataset.contextTokens) byId("inputApiContextTokens").value = selected.dataset.contextTokens;
  }
  text("apiModelCount", result.supported ? `${restore ? "已保存的列表 · " : ""}${known.size} 个模型${restore ? "" : "可用"}` :
    "此接口不提供模型列表，可手动填写模型 ID");
  text("btnFetchApiModels", "刷新模型列表");
}
function restoreModelList() {
  const cached = settingsState.modelSourceSnapshot.modelList;
  if (cached && typeof cached.baseUrl === "string" && matchesApiConnection(cached) &&
      typeof cached.supported === "boolean" && Array.isArray(cached.models))
    renderModelList(cached, { restore: true });
  else selectSavedApiModel();
  updateModelSourceControls();
}
function invalidateModelDiscovery() {
  settingsState.modelListController?.abort();
  settingsState.modelListController = null;
  settingsState.modelTestController?.abort();
  settingsState.modelTestController = null;
  ++settingsState.modelSourceRevision;
  ++settingsState.modelListRequest;
  ++settingsState.modelTestRequest;
  settingsState.modelListBusy = false;
  settingsState.modelTestBusy = false;
  clearModelList();
  text("apiModelCount", "");
  text("apiModelTestStatus", "");
  text("modelSourceStatus", "");
  updateModelSourceControls();
}
function invalidateModelTest() {
  settingsState.modelTestController?.abort();
  settingsState.modelTestController = null;
  ++settingsState.modelSourceRevision;
  ++settingsState.modelTestRequest;
  settingsState.modelTestBusy = false;
  text("apiModelTestStatus", "");
  text("modelSourceStatus", "");
  updateModelSourceControls();
}
function modelDetailEntry() {
  return (settingsState.modelSourceSnapshot.models || []).find(item => item.id === settingsState.modelDetailId);
}
function canReuseSavedApiKey() {
  const saved = modelDetailEntry();
  return !!saved?.hasKey && matchesApiConnection(saved);
}
function syncApiKeyVisibility() {
  const button = byId("btnToggleApiKeyVisibility");
  const visible = byId("inputApiKey").type === "text";
  const label = visible ? "隐藏 API Key" : "显示 API Key";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", String(visible));
  button.disabled = settingsState.modelSourceLoading || settingsState.modelSourceBusy ||
    !!settingsState.apiKeyRevealController || (!byId("inputApiKey").value &&
      (!settingsState.modelSourceResolved || !canReuseSavedApiKey()));
}
function syncSavedApiKeyHint() {
  const reusable = canReuseSavedApiKey();
  const input = byId("inputApiKey");
  byId("apiKeySaved").hidden = !reusable;
  text("apiKeySaved", reusable && input.value && !settingsState.apiKeyRevealedSaved ? "已保存 · 新密钥待保存" : "已保存");
  input.placeholder = reusable ? "••••••••••••••••" : "按服务要求填写 API Key";
  input.dataset.saved = String(reusable && !input.value);
  syncApiKeyVisibility();
}
function hideApiKey() {
  settingsState.apiKeyRevealController?.abort();
  settingsState.apiKeyRevealController = null;
  ++settingsState.apiKeyRevealRequest;
  if (settingsState.apiKeyRevealedSaved) byId("inputApiKey").value = "";
  settingsState.apiKeyRevealedSaved = false;
  byId("inputApiKey").type = "password";
  text("apiKeyStatus", "");
  syncSavedApiKeyHint();
}
async function toggleApiKeyVisibility() {
  const input = byId("inputApiKey");
  if (input.type === "text") { hideApiKey(); return; }
  if (settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.apiKeyRevealController) return;
  if (input.value) { input.type = "text"; syncApiKeyVisibility(); return; }
  if (!settingsState.modelSourceResolved || !canReuseSavedApiKey()) return;
  const request = ++settingsState.apiKeyRevealRequest;
  const protocol = byId("selectApiProtocol").value;
  const baseUrl = byId("inputApiBaseUrl").value.trim().replace(/\/+$/, "");
  const controller = new AbortController();
  settingsState.apiKeyRevealController = controller;
  const timeoutId = setTimeout(() => controller.abort(), 15_000);
  text("apiKeyStatus", "正在读取已保存密钥…");
  syncApiKeyVisibility();
  try {
    const result = await api("/api/model-source/reveal-key", {
      method: "POST", body: JSON.stringify({ id: settingsState.modelDetailId, protocol, baseUrl }),
    }, controller.signal);
    if (request !== settingsState.apiKeyRevealRequest || !matchesApiConnection({ protocol, baseUrl }) || input.value) return;
    if (!result || typeof result.apiKey !== "string" || !result.apiKey) throw new Error("key unavailable");
    input.value = result.apiKey;
    settingsState.apiKeyRevealedSaved = true;
    input.type = "text";
    text("apiKeyStatus", "");
  } catch {
    if (request === settingsState.apiKeyRevealRequest) text("apiKeyStatus", "无法读取已保存密钥，请重试");
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.apiKeyRevealController === controller) settingsState.apiKeyRevealController = null;
    syncSavedApiKeyHint();
  }
}
function apiKeyInputChanged() {
  const visible = byId("inputApiKey").type === "text";
  settingsState.apiKeyRevealedSaved = false;
  hideApiKey();
  byId("inputApiKey").type = visible ? "text" : "password";
  syncApiKeyVisibility();
  settingsState.modelSourceDraftDirty = true;
  invalidateModelDiscovery();
}
function apiConnectionChanged() {
  hideApiKey();
  settingsState.modelSourceDraftDirty = true;
  invalidateModelDiscovery();
  restoreModelList();
}
function showModelSource(data) {
  hideApiKey();
  applyActiveModelSource(data);
  settingsState.modelSourceDraftDirty = false;
  invalidateModelDiscovery();
  renderModelHome();
  if (!byId("modelDetailView").hidden) openModelDetail(settingsState.modelDetailId);
}
async function loadModelSource(preserveDraft = false) {
  hideApiKey();
  settingsState.modelSourceLoadController?.abort();
  const request = ++settingsState.modelSourceReadRequest;
  const abortController = new AbortController();
  settingsState.modelSourceLoadController = abortController;
  const timeoutId = setTimeout(() => abortController.abort(), 15_000);
  settingsState.modelSourceLoading = true;
  text("modelSourceStatus", "读取中…");
  updateModelSourceControls();
  try {
    const data = await api("/api/model-source", {}, abortController.signal);
    if (request !== settingsState.modelSourceReadRequest) return;
    if (!validModelSource(data)) throw new Error("invalid model source");
    settingsState.localJudgeError = "";
    if (settingsState.modelSourceDraftDirty) {
      applyActiveModelSource(data);
      renderModelHome();
      syncSavedApiKeyHint();
    } else showModelSource(data);
    text("modelSourceStatus", "");
  } catch {
    if (request === settingsState.modelSourceReadRequest)
      text("modelSourceStatus", abortController.signal.aborted ? "模型设置读取超时" : "模型设置读取失败");
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.modelSourceLoadController === abortController) settingsState.modelSourceLoadController = null;
    if (request === settingsState.modelSourceReadRequest) {
      settingsState.modelSourceLoading = false;
      updateModelSourceControls();
    }
  }
}
function modelSourceRequestError(error) {
  const reasons = {
    auth: "密钥或访问权限有误", "rate-limit": "请求过于频繁",
    timeout: "连接超时", unsupported: "接口不支持",
    network: "无法连接服务", "invalid-url": "地址格式有误",
    "response-too-large": "服务响应过大", "empty-response": "模型未返回内容",
    "provider-error": "模型服务返回错误", "invalid-output": "模型返回格式不正确",
    "output-truncated": "模型输出达到长度上限被截断",
  };
  if (typeof error?.code === "string" && reasons[error.code]) return reasons[error.code];
  return Number.isInteger(error?.status) ? `HTTP ${error.status}` : "网络或服务错误";
}
function apiModelDraft(requireModel) {
  const protocol = byId("selectApiProtocol").value;
  const baseUrl = byId("inputApiBaseUrl").value.trim();
  const model = byId("inputApiModelId").value.trim();
  if (!MODEL_SOURCE_PROTOCOLS.has(protocol)) throw new Error("请选择接口协议");
  let url;
  try { url = new URL(baseUrl); } catch { throw new Error("请输入有效的 Base URL"); }
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("Base URL 须使用 HTTP 或 HTTPS");
  if (url.username || url.password || url.search || url.hash || /[\s\\]/.test(baseUrl))
    throw new Error("Base URL 不能包含账号、查询参数或空格");
  if (requireModel && !model) throw new Error("请输入模型 ID");
  const rawContext = byId("inputApiContextTokens").value.trim();
  const contextTokens = rawContext ? Number(rawContext) : null;
  if (contextTokens !== null && (!Number.isSafeInteger(contextTokens) ||
      contextTokens < 4096 || contextTokens > 1000000)) throw new Error("上下文大小须为 4096～1000000 tokens");
  const draft = { protocol, baseUrl: baseUrl.replace(/\/+$/, ""),
    ...(settingsState.modelDetailId ? { id: settingsState.modelDetailId } : {}) };
  const apiKey = byId("inputApiKey").value.trim();
  if (apiKey && !settingsState.apiKeyRevealedSaved) draft.apiKey = apiKey;
  if (requireModel) {
    const rawTimeout = String(byId("inputApiTimeoutMinutes").value).trim();
    const minutes = rawTimeout === "" ? 60 : Number(rawTimeout);
    if (!Number.isSafeInteger(minutes) || minutes < 1 || minutes > 180)
      throw new Error("生成超时须为 1～180 分钟的整数");
    draft.model = model;
    draft.timeoutMs = minutes * 60_000;
    if (contextTokens !== null) draft.contextTokens = contextTokens;
  }
  return draft;
}
async function fetchApiModels() {
  let draft;
  try { draft = apiModelDraft(false); }
  catch (error) { text("apiModelCount", error.message); return; }
  const request = ++settingsState.modelListRequest;
  const revision = settingsState.modelSourceRevision;
  const abortController = new AbortController();
  settingsState.modelListController = abortController;
  const timeoutId = setTimeout(() => abortController.abort(), 20_000);
  settingsState.modelListBusy = true;
  text("apiModelCount", "正在获取…");
  text("apiModelTestStatus", "");
  updateModelSourceControls();
  try {
    const result = await api("/api/model-source/list", { method: "POST", body: JSON.stringify(draft) },
      abortController.signal);
    if (request !== settingsState.modelListRequest || revision !== settingsState.modelSourceRevision) return;
    if (!result || typeof result.supported !== "boolean" || !Array.isArray(result.models))
      throw new Error("invalid model list");
    settingsState.modelSourceSnapshot = { ...settingsState.modelSourceSnapshot,
      modelList: { protocol: draft.protocol, baseUrl: draft.baseUrl, supported: result.supported, models: result.models } };
    renderModelList(result);
  } catch (error) {
    if (request === settingsState.modelListRequest && revision === settingsState.modelSourceRevision)
      text("apiModelCount", abortController.signal.aborted
        ? "获取超时，可手动填写模型 ID"
        : `获取失败（${modelSourceRequestError(error)}），可手动填写模型 ID`);
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.modelListController === abortController) settingsState.modelListController = null;
    if (request === settingsState.modelListRequest) {
      settingsState.modelListBusy = false;
      updateModelSourceControls();
    }
  }
}
async function testApiModel() {
  if (settingsState.modelSourceBusy || settingsState.modelTestBusy) return;
  let draft;
  try { draft = apiModelDraft(true); }
  catch (error) { text("apiModelTestStatus", error.message); return; }
  const request = ++settingsState.modelTestRequest;
  const revision = settingsState.modelSourceRevision;
  const abortController = new AbortController();
  settingsState.modelTestController = abortController;
  const timeoutId = setTimeout(() => abortController.abort(), 20_000);
  settingsState.modelTestBusy = true;
  text("apiModelTestStatus", "正在测试…");
  updateModelSourceControls();
  try {
    const result = await api("/api/model-source/test", { method: "POST", body: JSON.stringify(draft) },
      abortController.signal);
    if (request !== settingsState.modelTestRequest || revision !== settingsState.modelSourceRevision) return;
    if (result?.ok !== true) throw new Error("connection test failed");
    const latency = Number.isFinite(result.latencyMs) ? ` · ${Math.round(result.latencyMs)} ms` : "";
    text("apiModelTestStatus", `连接成功${latency}`);
  } catch (error) {
    if (request === settingsState.modelTestRequest && revision === settingsState.modelSourceRevision)
      text("apiModelTestStatus", abortController.signal.aborted ? "连接测试超时" :
        `连接失败（${modelSourceRequestError(error)}）`);
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.modelTestController === abortController) settingsState.modelTestController = null;
    if (request === settingsState.modelTestRequest) {
      settingsState.modelTestBusy = false;
      updateModelSourceControls();
    }
  }
}
// Modified 2026-11-01: the model home lists saved models, assigns purposes and
// opens one detail page per model; /activate stays the main-model save for MCP.
function showModelView(view) {
  byId("modelHomeView").hidden = view !== "home";
  byId("modelDetailView").hidden = view !== "detail";
  byId("searchSettingsView").hidden = view !== "search";
  byId("developerSettingsView").hidden = view !== "developer";
  byId("localJudgeView").hidden = view !== "local";
}
const LOCAL_JUDGE_BUSY = new Set(["checking", "downloading", "verifying", "selftest"]);
function renderLocalJudge() {
  const local = settingsState.modelSourceSnapshot.localJudge;
  const active = LOCAL_JUDGE_BUSY.has(local?.state);
  const labels = {missing: "尚未下载", checking: "检查电脑…", downloading: "下载中", verifying: "校验中…",
    selftest: "自测中…", ready: "已下载 · 用于影子评估", slow: "电脑较慢", error: "准备失败"};
  let label = labels[local?.state] || "暂不可用";
  if (local?.state === "downloading" && local.totalBytes > 0)
    label += ` ${Math.min(100, Math.floor(100 * local.downloadedBytes / local.totalBytes))}%`;
  if (local?.state === "ready" && Number.isFinite(local.latencyMs)) label += ` · 约 ${(local.latencyMs / 1000).toFixed(2)} 秒/次`;
  if (local?.error) label += `（${typeof local.error === "string" ? local.error : "请重试"}）`;
  if (settingsState.localJudgeError) label = settingsState.localJudgeError;
  text("localJudgeStatus", label);
  const progress = byId("localJudgeProgress");
  progress.hidden = !active;
  if (local?.state === "downloading" && local.totalBytes > 0) {
    progress.max = local.totalBytes;
    progress.value = Math.min(local.totalBytes, local.downloadedBytes || 0);
  } else progress.removeAttribute?.("value");
  byId("btnPrepareLocalJudge").hidden = active || local?.ready === true;
  byId("btnPrepareLocalJudge").disabled = settingsState.modelSourceBusy || local?.supported === false;
  text("btnPrepareLocalJudge", local?.downloadedBytes ? "继续准备" : "下载（168 MB）");
  byId("btnCancelLocalJudge").hidden = !active;
  byId("btnDeleteLocalJudge").hidden = active || !(local?.ready || local?.downloadedBytes > 0);
  for (const id of ["btnCancelLocalJudge", "btnDeleteLocalJudge"]) byId(id).disabled = settingsState.modelSourceBusy;
  clearTimeout(settingsState.localJudgePoll);
  settingsState.localJudgePoll = null;
  if (active && byId("settingsModal").classList.contains("show"))
    settingsState.localJudgePoll = setTimeout(() => { settingsState.localJudgePoll = null; void loadModelSource(true); }, 1000);
}
async function localJudgeAction(action) {
  if (settingsState.modelSourceBusy) return;
  settingsState.modelSourceBusy = true;
  settingsState.localJudgeError = "";
  updateModelSourceControls();
  renderLocalJudge();
  showModelView("local");
  try {
    const data = await api(`/api/model-source/local-judge/${action}`, {method: "POST", body: "{}"});
    if (!validModelSource(data)) throw new Error("invalid model source");
    showModelSource(data);
  } catch (error) { settingsState.localJudgeError = `操作失败（${modelSourceRequestError(error)}）`; }
  finally {
    settingsState.modelSourceBusy = false;
    updateModelSourceControls();
    renderLocalJudge();
  }
}
function protocolLabel(protocol) {
  return {anthropic: "Anthropic", responses: "Responses", chat_completions: "Completions",
    gemini: "Gemini", ollama: "Ollama", systemone: "本地"}[protocol] || protocol;
}
function modelEntryLabel(item) {
  return item.name || item.model;
}
function renderModelHome() {
  const data = settingsState.modelSourceSnapshot;
  if (!data) return;
  const models = Array.isArray(data.models) ? data.models : [];
  const roles = data.roles || {main: null, judge: null, image: null};
  const busy = settingsState.modelSourceLoading || settingsState.modelSourceBusy || !settingsState.modelSourceResolved;
  const fill = (id, value, followAllowed) => {
    const select = byId(id);
    select.replaceChildren();
    if (followAllowed) {
      const follow = document.createElement("option");
      follow.value = "";
      follow.textContent = "跟随主模型";
      select.appendChild(follow);
    }
    for (const item of models) {
      if (item.builtin && id !== "selectRoleJudge") continue;
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = modelEntryLabel(item) + (item.id === roles.main ? "（主模型）" : "");
      select.appendChild(option);
    }
    select.value = value || "";
    if (select.selectedIndex < 0) select.value = followAllowed ? "" : String(roles.main || "");
    select.disabled = busy;
  };
  fill("selectRoleMain", roles.main, false);
  fill("selectRoleJudge", roles.judge, true);
  fill("selectRoleImage", roles.image, true);
  if (data.localJudge?.supported && !models.some(item => item.builtin)) {
    const download = document.createElement("option");
    download.value = "download-local";
    download.textContent = "下载本地模型（168 MB）";
    byId("selectRoleJudge").appendChild(download);
  }
  const rows = byId("modelListRows");
  rows.replaceChildren();
  for (const item of models) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "settings-row model-list-row";
    const name = document.createElement("span");
    name.textContent = modelEntryLabel(item);
    const sub = document.createElement("span");
    sub.className = "model-source-status";
    sub.textContent = item.builtin ? "本地" : protocolLabel(item.protocol);
    row.appendChild(name);
    row.appendChild(sub);
    row.addEventListener("click", () => openModelDetail(item.id));
    rows.appendChild(row);
  }
  byId("btnAddModel").disabled = busy;
  text("btnOpenSearchSettings", window.CoreModelSettings?.searchSource?.() === "searxng" ? "SearXNG" : "关闭");
  updateModelSourceControls();
  renderLocalJudge();
}
function openModelDetail(entryId) {
  const data = settingsState.modelSourceSnapshot;
  const entry = entryId ? (data.models || []).find(item => item.id === entryId) : null;
  if (entryId && !entry) return;
  if (entry?.builtin) { hideApiKey(); showModelView("local"); renderLocalJudge(); return; }
  hideApiKey();
  settingsState.modelDetailId = entryId || null;
  byId("apiModelAdvanced").open = false;
  text("modelDetailTitle", entry ? "编辑模型" : "添加模型");
  byId("selectApiProtocol").value = entry?.protocol || data.modelList?.protocol || "responses";
  byId("inputApiBaseUrl").value = entry?.baseUrl || data.modelList?.baseUrl || "";
  byId("inputApiModelId").value = entry?.model || "";
  byId("inputApiContextTokens").value = String(entry?.contextTokens ?? "");
  byId("inputApiTimeoutMinutes").value = String((entry?.timeoutMs ?? 3_600_000) / 60_000);
  byId("inputApiConcurrency").value = String(entry?.concurrency || 4);
  byId("inputApiKey").value = "";
  syncSavedApiKeyHint();
  byId("btnDeleteApiModel").hidden = !entry || entry.id === data.roles?.main;
  invalidateModelDiscovery();
  restoreModelList();
  text("modelSourceStatus", "");
  text("apiModelTestStatus", "");
  showModelView("detail");
}
function mainIdentityWillChange(entryId, payload) {
  if (!entryId || entryId !== settingsState.modelSourceSnapshot.roles?.main) return false;
  const entry = (settingsState.modelSourceSnapshot.models || []).find(item => item.id === entryId);
  return !!entry && (payload.apiKey !== undefined ||
    (payload.contextTokens !== undefined && payload.contextTokens !== entry.contextTokens) ||
    [entry.protocol, entry.baseUrl, entry.model].join("\u0000") !==
    [payload.protocol, payload.baseUrl, payload.model].join("\u0000"));
}
async function saveApiModel() {
  if (settingsState.modelSourceBusy || settingsState.modelTestBusy) return;
  const entryId = settingsState.modelDetailId;
  let payload;
  try {
    payload = {...(entryId ? {id: entryId} : {}), ...apiModelDraft(true)};
    const concurrency = Number(byId("inputApiConcurrency").value);
    if (!Number.isSafeInteger(concurrency) || concurrency < 1 || concurrency > 8)
      throw new Error("同时请求数须为 1～8 的整数");
    payload.concurrency = concurrency;
  } catch (error) { text("modelSourceStatus", error.message); return; }
  if (mainIdentityWillChange(entryId, payload) && !window.confirm("修改主模型会停止运行中的任务，继续吗？")) return;
  hideApiKey();
  settingsState.modelSourceBusy = true;
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), 60_000);
  text("modelSourceStatus", "正在保存…");
  updateModelSourceControls();
  try {
    const data = await api("/api/model-source/models/save", { method: "POST", body: JSON.stringify(payload) },
      abortController.signal);
    if (!validModelSource(data)) throw new Error("invalid model source");
    showModelSource(data);
    showModelView("home");
    text("modelSourceStatus", "已保存");
  } catch (error) {
    text("modelSourceStatus", `保存失败（${abortController.signal.aborted ? "连接超时" : modelSourceRequestError(error)}）`);
  } finally {
    clearTimeout(timeoutId);
    settingsState.modelSourceBusy = false;
    updateModelSourceControls();
  }
}
async function deleteApiModel() {
  const entryId = settingsState.modelDetailId;
  if (!entryId || settingsState.modelSourceBusy) return;
  settingsState.modelSourceBusy = true;
  text("modelSourceStatus", "正在删除…");
  updateModelSourceControls();
  try {
    const data = await api("/api/model-source/models/delete", { method: "POST", body: JSON.stringify({id: entryId}) });
    if (!validModelSource(data)) throw new Error("invalid model source");
    showModelSource(data);
    showModelView("home");
    text("modelSourceStatus", "已删除");
  } catch (error) {
    text("modelSourceStatus", `删除失败（${modelSourceRequestError(error)}）`);
  } finally {
    settingsState.modelSourceBusy = false;
    updateModelSourceControls();
  }
}
async function applyRole(role, value) {
  if (settingsState.modelSourceBusy || !settingsState.modelSourceResolved) return;
  if (role === "judge" && value === "download-local") { renderModelHome(); await localJudgeAction("prepare"); return; }
  const snapshot = settingsState.modelSourceSnapshot;
  if ((snapshot.roles?.[role] || null) === (value || null)) { renderModelHome(); return; }
  if (role === "main" && !window.confirm("修改主模型会停止运行中的任务，继续吗？")) { renderModelHome(); return; }
  settingsState.modelSourceBusy = true;
  updateModelSourceControls();
  try {
    const data = await api("/api/model-source/roles", { method: "POST", body: JSON.stringify({[role]: value || null}) });
    if (!validModelSource(data)) throw new Error("invalid model source");
    applyActiveModelSource(data);
    text("modelRolesStatus", "");
  } catch (error) {
    text("modelRolesStatus", `保存失败（${modelSourceRequestError(error)}）`);
  } finally {
    settingsState.modelSourceBusy = false;
    updateModelSourceControls();
    renderModelHome();
  }
}
// Modified 2026-11-01: 密钥清除随模型删除；blank 保存沿用同端点密文，不再单设清除入口。
let managedAccounts = [];
let managedCurrentAccountId = null;
let selectedManagedAccountId = null;
let pendingDeleteAccountId = null;
let accountManagerRequest = 0;
let accountDeleteBusy = false;
let accountManagementOpen = false;
function managedAccountIsCurrent(account) {
  return !!account && (account.current || account.accountId === managedCurrentAccountId);
}
function renderManagedAccounts() {
  const list = byId("accountList");
  list.replaceChildren();
  if (!managedAccounts.length) status(list, "暂无已保存账号");
  for (const account of managedAccounts) {
    const row = element("div", "account-row");
    const item = element("button", `account-item${account.accountId === selectedManagedAccountId ? " active" : ""}`);
    item.type = "button";
    item.setAttribute("aria-pressed", String(account.accountId === selectedManagedAccountId));
    item.appendChild(element("span", "account-wechat-id", account.wechatId));
    if (managedAccountIsCurrent(account)) item.appendChild(element("span", "account-current", "正在使用"));
    item.addEventListener("click", () => {
      if (accountDeleteBusy) return;
      selectedManagedAccountId = account.accountId;
      pendingDeleteAccountId = null;
      byId("accountDeleteConfirm").hidden = true;
      text("accountManagerStatus", managedAccountIsCurrent(account) ? "正在使用" : "");
      renderManagedAccounts();
    });
    row.appendChild(item);
    if (accountManagementOpen) {
      const clear = element("button", "settings-danger-btn account-clear-btn", "清除");
      clear.type = "button";
      clear.disabled = accountDeleteBusy;
      clear.title = "清除本软件中的账号数据";
      clear.addEventListener("click", () => {
        if (clear.disabled) return;
        selectedManagedAccountId = account.accountId;
        pendingDeleteAccountId = account.accountId;
        text("accountDeleteQuestion", `清除账号 ${account.wechatId} 在本软件中的聊天记录副本、分析与画像、运行缓存？微信原始记录不会删除。${managedAccountIsCurrent(account) ? "若清除时仍为当前账号，软件将退出；下次启动重新初始化。" : "若清除时仍非当前账号，软件继续运行。"}`);
        text("btnConfirmDeleteAccount", managedAccountIsCurrent(account) ? "清除并退出" : "确认清除");
        byId("accountDeleteConfirm").hidden = false;
        renderManagedAccounts();
      });
      row.appendChild(clear);
    }
    list.appendChild(row);
  }
}
async function loadAccounts() {
  if (accountDeleteBusy) return;
  const request = ++accountManagerRequest;
  status(byId("accountList"), "正在读取账号…");
  try {
    const data = await api("/api/accounts");
    if (request !== accountManagerRequest) return;
    if (!Array.isArray(data.accounts) || !data.accounts.every(account => account &&
      typeof account.accountId === "string" && account.accountId &&
      typeof account.wechatId === "string" && account.wechatId && typeof account.current === "boolean") ||
      !(data.currentAccountId === null || typeof data.currentAccountId === "string") ||
      new Set(data.accounts.map(account => account.accountId)).size !== data.accounts.length) throw new Error("账号列表无效");
    managedAccounts = data.accounts;
    managedCurrentAccountId = data.currentAccountId;
    if (!managedAccounts.some(account => account.accountId === selectedManagedAccountId)) selectedManagedAccountId = null;
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
    renderManagedAccounts();
    text("accountManagerStatus", "");
  } catch (error) {
    if (request !== accountManagerRequest) return;
    managedAccounts = [];
    managedCurrentAccountId = null;
    selectedManagedAccountId = null;
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
    status(byId("accountList"), "账号读取失败", () => { void loadAccounts(); });
    text("accountManagerStatus", error.message);
  }
}
function closeSettingsModal() {
  if (window.PluginSettings?.requestLeave) { window.PluginSettings.requestLeave(closeSettingsModalNow); return; }
  closeSettingsModalNow();
}
function closeSettingsModalNow() {
  hideApiKey();
  settingsState.modelSourceDraftDirty = false;
  ++settingsState.dataRootRequest;
  settingsState.dataRootDraftDirty = false;
  settingsState.modelSourceLoadController?.abort();
  settingsState.modelSourceLoadController = null;
  settingsState.modelListController?.abort();
  settingsState.modelListController = null;
  settingsState.modelTestController?.abort();
  settingsState.modelTestController = null;
  byId("settingsModal").classList.remove("show");
  closeConversationManager();
  text("conversationManagerStatus", "");
  ++settingsState.modelSourceReadRequest;
  ++settingsState.modelListRequest;
  ++settingsState.modelTestRequest;
  settingsState.modelSourceLoading = false;
  settingsState.modelListBusy = false;
  settingsState.modelTestBusy = false;
  byId("inputApiKey").value = "";
  accountManagementOpen = false;
  text("btnToggleAccountManagement", "管理");
  byId("btnToggleAccountManagement").setAttribute("aria-pressed", "false");
  pendingDeleteAccountId = null;
  byId("accountDeleteConfirm").hidden = true;
  renderManagedAccounts();
  if (window.desktopHost?.isSettingsWindow) void window.desktopHost.closeSettingsWindow();
}
// Forget this window's per-account conveniences (read marks, last chat, pins) of a cleared account.
async function clearAccountUiData(accountId) {
  const storages = [localStorage];
  if (typeof sessionStorage !== "undefined") storages.push(sessionStorage);
  const prefixes = ["read-watermark:", "last-conversation:", "conversation-pins-v1:"];
  const accounts = new Set();
  for (const account of window.ChatComposer?.accounts() || []) accounts.add(account);
  if (typeof chatState.currentAccount === "string" && chatState.currentAccount) accounts.add(chatState.currentAccount);
  for (const key of chatState.sessionCache.keys()) {
    try {
      const [account] = JSON.parse(key);
      if (typeof account === "string" && account) accounts.add(account);
    } catch { }
  }
  for (const storage of storages) try {
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      const prefix = prefixes.find(item => key?.startsWith(item));
      if (prefix) accounts.add(key.slice(prefix.length));
      else if (key?.startsWith("mbti-unlocked:")) accounts.add(key.slice("mbti-unlocked:".length).split(":", 1)[0]);
    }
  } catch { }
  const matching = new Set();
  const encoder = new TextEncoder();
  for (const account of accounts) {
    const digest = await crypto.subtle.digest("SHA-256", encoder.encode(account));
    const id = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
    if (id === accountId) matching.add(account);
  }
  for (const key of chatState.sessionCache.keys()) {
    try { if (matching.has(JSON.parse(key)[0])) chatState.sessionCache.delete(key); } catch { }
  }
  for (const account of matching) window.ChatComposer?.clearAccount(account);
  for (const account of matching) for (const storage of storages) try {
    for (const prefix of prefixes) storage.removeItem(prefix + account);
    for (let index = storage.length - 1; index >= 0; index--) {
      const key = storage.key(index);
      if (key?.startsWith(`mbti-unlocked:${account}:`)) storage.removeItem(key);
    }
  } catch { }
  return matching;
}
async function deleteManagedAccount() {
  const account = managedAccounts.find(item => item.accountId === pendingDeleteAccountId);
  if (!account || accountDeleteBusy) return;
  const accountId = account.accountId;
  accountDeleteBusy = true;
  byId("btnConfirmDeleteAccount").disabled = true;
  renderManagedAccounts();
  text("accountManagerStatus", "正在删除…");
  try {
    const response = await fetch(`/api/accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" });
    let data = null;
    try { data = await response.json(); } catch { }
    if (!response.ok) throw new Error(typeof data?.message === "string" && data.message ? data.message :
      typeof data?.error === "string" && data.error ? data.error : `删除失败（HTTP ${response.status}）`);
    if (data?.deleted !== accountId) throw new Error("删除结果不匹配");
    window.SettingsSync?.accountCleared(accountId);
    announceFriendAccount("account-cleared", accountId);
    const exitAfterDelete = data.exitApp === true || data.current === true;
    if (exitAfterDelete) {
      accountClearedExiting = true;
      chatState.sessionRequest++;
      chatState.advance("generation");
      chatState.controller?.abort();
      chatState.currentUser = null;
    }
    let cacheCleared = true;
    let clearedAccounts = new Set();
    try { clearedAccounts = await clearAccountUiData(accountId); }
    catch { cacheCleared = false; }
    try { for (const clearedAccount of clearedAccounts) window.PluginSlots?.onAccountCleared(clearedAccount); }
    catch { cacheCleared = false; }
    if (exitAfterDelete) {
      resetAccountView(cacheCleared ? "账号数据已清除，正在退出…" : "账号已清除，本地缓存清理失败，请关闭软件", true);
      clearTimeout(startupWatchdog);
      byId("startupRetry").hidden = true;
      byId("startupContinue").hidden = true;
      text("accountManagerStatus", cacheCleared ? "已清除，正在退出…" : "账号已清除，本地缓存清理失败");
      try {
        if (typeof window.desktopHost?.exitApp !== "function") throw new Error("exitApp unavailable");
        await window.desktopHost.exitApp();
      } catch { text("startupStatus", "账号已清除，请关闭软件。"); }
      return;
    }
    selectedManagedAccountId = null;
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
    accountDeleteBusy = false;
    await loadAccounts();
    text("accountManagerStatus", cacheCleared ? "已清除" : "账号已清除，本地界面缓存清理失败");
    void loadSessions();
  } catch (error) {
    text("accountManagerStatus", error.message);
  } finally {
    accountDeleteBusy = false;
    byId("btnConfirmDeleteAccount").disabled = false;
    if (!accountClearedExiting) renderManagedAccounts();
  }
}
async function copyDraft() {
  const value = byId("chatInput").value;
  if (!value.trim()) return;
  const scope = { account: chatState.currentAccount, user: chatState.currentUser };
  const current = () => scope.account === chatState.currentAccount && scope.user === chatState.currentUser && byId("chatInput").value === value;
  let copied = false;
  if (window.desktopHost?.copyDraft) {
    try { copied = await window.desktopHost.copyDraft(value); } catch { }
  }
  if (!copied && navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); copied = true; } catch { }
  }
  if (!copied) {
    // A delayed clipboard denial must not select and copy a different contact's new draft.
    if (!current()) return;
    const input = byId("chatInput");
    const focus = document.activeElement;
    const start = input.selectionStart;
    const end = input.selectionEnd;
    try {
      input.focus();
      input.select();
      copied = document.execCommand("copy");
    } catch { }
    finally {
      input.setSelectionRange(start, end);
      if (focus && focus !== input && typeof focus.focus === "function") focus.focus();
    }
  }
  if (current()) toast(copied ? "草稿已复制" : "复制失败，请手动复制草稿");
}
byId("searchInput").addEventListener("input", renderSessions);
byId("chatMessages").addEventListener("scroll", event => {
  const container = event.currentTarget;
  const delta = container.scrollTop - chatState.lastChatScrollTop;
  if (delta) queueHistoryScroll(delta < 0 ? "older" : "newer");
  if (chatState.historyState) {
    chatState.followLatest = false;
    chatState.lastChatScrollTop = container.scrollTop;
    updateHistoryNavigation();
    return;
  }
  if (container.scrollHeight - container.scrollTop - container.clientHeight < 80) chatState.followLatest = true;
  else if (container.scrollTop < chatState.lastChatScrollTop - 1) chatState.followLatest = false;
  chatState.lastChatScrollTop = container.scrollTop;
  updateHistoryNavigation();
});
// A short conversation cannot emit scroll at its boundary. Wheel/key/touch intent still pages it.
function historyWheel(event) {
  if (!event.ctrlKey && event.deltaY) queueHistoryScroll(event.deltaY < 0 ? "older" : "newer");
}
byId("chatMessages").addEventListener("wheel", historyWheel, { passive: true });
byId("chatMessages").addEventListener("keydown", event => {
  if (event.target !== event.currentTarget) return;
  if (["ArrowUp", "PageUp", "Home"].includes(event.key)) queueHistoryScroll("older");
  else if (["ArrowDown", "PageDown", "End"].includes(event.key)) queueHistoryScroll("newer");
});
let historyTouchY = null;
byId("chatMessages").addEventListener("touchstart", event => { historyTouchY = event.touches[0]?.clientY ?? null; }, { passive: true });
byId("chatMessages").addEventListener("touchmove", event => {
  const y = event.touches[0]?.clientY;
  if (historyTouchY != null && y != null && Math.abs(y - historyTouchY) > 4) {
    queueHistoryScroll(y > historyTouchY ? "older" : "newer");
    historyTouchY = y;
  }
}, { passive: true });
byId("btnHistoryRetry").addEventListener("click", retryHistory);
byId("btnReturnLatest").addEventListener("click", returnToLatest);
byId("btnChatHistory").addEventListener("click", () => { void openHistorySearch(); });
byId("historySearchPanel").addEventListener("cancel", event => { event.preventDefault(); closeHistorySearch(); });
window.ChatComposer = window.Composer.create({ drafts: window.ComposerDrafts.create(),
  input: byId("chatInput"), copy: copyDraft });
window.addEventListener("pagehide", () => window.ChatComposer.dispose());
byId("navChat").addEventListener("click", () => switchView("chat"));
window.desktopHost?.onFriendLocate?.(target => { void locateFromFriend(target); });
window.desktopHost?.onHistoryLocate?.(target => { void locateFromFriend(target); });
for (const [id, key] of [["selectThemeMode", "theme"], ["selectZoomLevel", "zoom"]]) byId(id).addEventListener("change", event => { settingsState.settings[key] = event.target.value; save(); applySettings(); });
for (const id of ["selectApiProtocol", "inputApiBaseUrl"])
  byId(id).addEventListener(id === "selectApiProtocol" ? "change" : "input", apiConnectionChanged);
byId("inputApiKey").addEventListener("input", apiKeyInputChanged);
byId("btnToggleApiKeyVisibility").addEventListener("click", () => { void toggleApiKeyVisibility(); });
byId("selectApiModel").addEventListener("change", event => {
  if (event.target.value) {
    byId("inputApiModelId").value = event.target.value;
    byId("inputApiContextTokens").value = event.target.selectedOptions?.[0]?.dataset.contextTokens || "";
  }
  settingsState.modelSourceDraftDirty = true;
  invalidateModelTest();
});
byId("inputApiModelId").addEventListener("input", () => {
  const select = byId("selectApiModel");
  const model = byId("inputApiModelId").value.trim();
  select.value = Array.from(select.options).some(option => option.value === model) ? model : "";
  byId("inputApiContextTokens").value = select.selectedOptions?.[0]?.dataset.contextTokens || "";
  settingsState.modelSourceDraftDirty = true;
  invalidateModelTest();
});
for (const id of ["inputApiContextTokens", "inputApiTimeoutMinutes", "inputApiConcurrency"]) byId(id).addEventListener("input", () => {
  settingsState.modelSourceDraftDirty = true;
  text("modelSourceStatus", "");
});
byId("btnFetchApiModels").addEventListener("click", () => { void fetchApiModels(); });
byId("btnReloadModelSource").addEventListener("click", () => { void loadModelSource(true); });
byId("btnTestApiModel").addEventListener("click", () => { void testApiModel(); });
byId("btnSaveApiModel").addEventListener("click", () => { void saveApiModel(); });
byId("btnDeleteApiModel").addEventListener("click", () => { void deleteApiModel(); });
byId("btnAddModel").addEventListener("click", () => openModelDetail(null));
byId("btnBackModelHome").addEventListener("click", () => showModelView("home"));
byId("btnOpenSearchSettings").addEventListener("click", () => showModelView("search"));
byId("btnOpenDeveloperSettings").addEventListener("click", () => showModelView("developer"));
byId("btnBackDeveloperSettings").addEventListener("click", () => showModelView("home"));
byId("btnBackLocalJudge").addEventListener("click", () => showModelView("home"));
byId("btnPrepareLocalJudge").addEventListener("click", () => void localJudgeAction("prepare"));
byId("btnCancelLocalJudge").addEventListener("click", () => void localJudgeAction("cancel"));
byId("btnDeleteLocalJudge").addEventListener("click", () => void localJudgeAction("delete"));
byId("btnBackFromSearch").addEventListener("click", () => showModelView("home"));
for (const [id, role] of [["selectRoleMain", "main"], ["selectRoleJudge", "judge"], ["selectRoleImage", "image"]])
  byId(id).addEventListener("change", event => { void applyRole(role, event.target.value); });
const OFFICIAL_RELEASES_URL = "https://github.com/youdrew/zhiji-wechat-assistant/releases";
// Modified by Know Your Friends, 2026-10-12: development builds do not use upstream updates.
const appUpdatesEnabled = window.desktopHost?.appUpdatesEnabled === true;
const UPDATE_BUSY_PHASES = new Set(["downloading", "verifying", "extracting", "installing", "restarting"]);
const UPDATE_BACKGROUND_CHECK_DELAY_MS = 10_000;
const UPDATE_BACKGROUND_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
let updateBackgroundChecksScheduled = false;
let aboutVersionPromise = null;
let versionLoadFailed = false;
let updateState = { phase: "idle" };
let updateStateSequence = 0;
let updateCheckPending = false;
let updateActionPending = false;
let updateOperationStatus = "";
let updateCheckedOnce = false;
let updatePreviousFocus = null;

function displayVersion(value) {
  const version = typeof value === "string" ? value.trim() : "";
  return version ? (version.startsWith("v") ? version : `v${version}`) : "";
}
function setDisplayedVersion(value) {
  const version = displayVersion(value);
  if (!version) return;
  text("aboutCurrentVersion", version);
  text("updateCurrentVersion", version);
  byId("btnAboutVersion").setAttribute("aria-label",
    appUpdatesEnabled ? `查看软件更新，当前版本 ${version}` : `当前版本 ${version}`);
}
function loadAboutVersion() {
  if (aboutVersionPromise) return aboutVersionPromise;
  if (typeof window.desktopHost?.getAppVersion !== "function") {
    text("aboutCurrentVersion", "--");
    text("updateCurrentVersion", "--");
    text("updateStatus", "仅桌面版可用");
    return Promise.resolve();
  }
  aboutVersionPromise = window.desktopHost.getAppVersion().then(version => {
    if (!displayVersion(version)) throw new Error("Invalid app version");
    versionLoadFailed = false;
    setDisplayedVersion(version);
  }).catch(() => {
    versionLoadFailed = true;
    text("aboutCurrentVersion", "读取失败");
    text("updateCurrentVersion", "读取失败");
    renderUpdateState();
    aboutVersionPromise = null;
  });
  return aboutVersionPromise;
}
function officialReleaseUrl(candidate) {
  try {
    const url = new URL(candidate);
    if (url.href === OFFICIAL_RELEASES_URL) return url.href;
  } catch { }
  return OFFICIAL_RELEASES_URL;
}
function updatePhase(state) {
  return typeof state?.phase === "string" ? state.phase :
    (typeof state?.status === "string" ? state.status : "");
}
function updateStatusMessage(state) {
  const latest = displayVersion(state.latestVersion);
  const error = typeof state.error === "string" ? state.error.trim().slice(0, 180) : "";
  return {
    idle: "准备检查更新",
    current: "已是最新版本",
    "preview-current": "已是最新版本",
    available: latest ? `发现新版本 ${latest}` : "发现新版本",
    downloading: "正在下载更新",
    verifying: "正在校验更新包",
    extracting: "正在解压更新包",
    ready: "更新已下载，准备安装",
    installing: "正在安装更新",
    restarting: "正在重启",
    rolled_back: "已回退到上一版本",
    failed: error ? `更新失败：${error}` : "更新失败，请重试",
    "incomplete-release": latest ? `发现 ${latest}，发布文件尚未齐全` : "发布文件尚未齐全",
    "no-release": "暂无正式发布版本",
    "invalid-current": "当前版本信息异常",
    "invalid-release": "发布信息异常，请稍后重试",
    "rate-limited": "检查次数受限，请稍后重试",
    timeout: "检查超时，请重试",
    offline: "网络不可用，请重试",
    "server-error": "暂时无法检查更新",
  }[updatePhase(state)] || "暂时无法检查更新";
}
function renderUpdateState() {
  const phase = updatePhase(updateState);
  const latest = displayVersion(updateState.latestVersion);
  byId("btnAboutVersion").disabled = !appUpdatesEnabled;
  byId("btnCheckUpdates").hidden = !appUpdatesEnabled;
  byId("updateReleaseLink").hidden = !appUpdatesEnabled;
  byId("updateLatestRow").hidden = !latest;
  text("updateLatestVersion", latest);
  text("updateStatus", updateCheckPending ? "正在检查更新" :
    (updateOperationStatus || (!window.desktopHost ? "仅桌面版可用" :
      (versionLoadFailed && phase === "idle" ? "版本读取失败" : updateStatusMessage(updateState)))));
  byId("updateReleaseLink").href = officialReleaseUrl(updateState.releaseUrl);

  const downloading = phase === "downloading";
  const downloaded = Number(updateState.downloadedBytes);
  const total = Number(updateState.totalBytes);
  byId("updateProgress").hidden = !downloading;
  if (downloading) {
    const knownTotal = Number.isFinite(total) && total > 0;
    const safeDownloaded = Number.isFinite(downloaded) ? Math.max(0, downloaded) : 0;
    const percent = knownTotal ? Math.min(100, Math.round(safeDownloaded / total * 100)) : 0;
    const bar = byId("updateProgressBar");
    bar.classList.toggle("indeterminate", !knownTotal);
    if (knownTotal) bar.setAttribute("aria-valuenow", String(percent));
    else bar.removeAttribute("aria-valuenow");
    byId("updateProgressFill").style.width = `${percent}%`;
    text("updateProgressText", knownTotal ? `${percent}%` : `${(safeDownloaded / 1048576).toFixed(1)} MB`);
  }

  const canRollback = appUpdatesEnabled && !!displayVersion(updateState.rollbackVersion) &&
    typeof window.desktopHost?.rollbackUpdate === "function";
  byId("updateRollback").hidden = !canRollback;
  text("updateRollbackVersion", displayVersion(updateState.rollbackVersion));
  byId("btnRollbackUpdate").disabled = !canRollback || updateActionPending || updateCheckPending || UPDATE_BUSY_PHASES.has(phase);

  const canCheck = appUpdatesEnabled && typeof window.desktopHost?.checkForUpdates === "function";
  byId("btnCheckUpdates").disabled = !canCheck || updateCheckPending || updateActionPending ||
    UPDATE_BUSY_PHASES.has(phase) || phase === "ready";
  const canBegin = appUpdatesEnabled && typeof window.desktopHost?.beginUpdate === "function" &&
    (phase === "available" || phase === "ready");
  byId("btnBeginUpdate").hidden = !canBegin;
  byId("btnBeginUpdate").disabled = !canBegin || updateCheckPending || updateActionPending;
  text("btnBeginUpdate", phase === "ready" ? "立即安装" : "下载并安装");

  const notice = byId("btnUpdateNotice");
  const showNotice = appUpdatesEnabled &&
    (phase === "available" || phase === "ready" || UPDATE_BUSY_PHASES.has(phase));
  const noticeLabel = latest ? `更新 ${latest}` : "更新";
  notice.hidden = !showNotice;
  notice.title = noticeLabel;
  notice.setAttribute("aria-label", noticeLabel);
  notice.textContent = "更新";
}
function applyUpdateState(next) {
  if (!appUpdatesEnabled || !updatePhase(next)) return false;
  updateState = { ...updateState, ...next, phase: updatePhase(next) };
  updateOperationStatus = "";
  updateStateSequence++;
  renderUpdateState();
  return true;
}
async function checkForUpdates(quiet = false) {
  if (!appUpdatesEnabled || updateCheckPending || updateActionPending || typeof window.desktopHost?.checkForUpdates !== "function" ||
      UPDATE_BUSY_PHASES.has(updatePhase(updateState))) return;
  // A quiet check publishes only a definitive result. A miss leaves the first modal check unused.
  if (!quiet) updateCheckedOnce = true;
  updateCheckPending = true;
  renderUpdateState();
  const before = updateStateSequence;
  try {
    const result = await window.desktopHost.checkForUpdates();
    const phase = updatePhase(result);
    const applicable = !quiet || ["available", "ready", "current", "preview-current"].includes(phase);
    if (applicable && before === updateStateSequence && applyUpdateState(result)) updateCheckedOnce = true;
  } catch {
    // A background check must not surface a failure the user did not ask about.
    if (!quiet && before === updateStateSequence) applyUpdateState({ phase: "server-error" });
  } finally {
    updateCheckPending = false;
    renderUpdateState();
  }
}
function scheduleBackgroundUpdateChecks() {
  if (window.desktopHost?.isSettingsWindow) return;
  if (!appUpdatesEnabled || updateBackgroundChecksScheduled || typeof window.desktopHost?.checkForUpdates !== "function") return;
  updateBackgroundChecksScheduled = true;
  setTimeout(() => {
    void checkForUpdates(true);
    setInterval(() => { void checkForUpdates(true); }, UPDATE_BACKGROUND_CHECK_INTERVAL_MS);
  }, UPDATE_BACKGROUND_CHECK_DELAY_MS);
}
async function refreshUpdateState() {
  if (!appUpdatesEnabled) return;
  if (typeof window.desktopHost?.getUpdateState === "function") {
    const before = updateStateSequence;
    try {
      const state = await window.desktopHost.getUpdateState();
      if (before === updateStateSequence) applyUpdateState(state);
    } catch { /* The check action remains available. */ }
  }
  if (!updateCheckedOnce && ["idle", "current", "preview-current"].includes(updatePhase(updateState)))
    void checkForUpdates();
}
function openUpdateModal() {
  if (!appUpdatesEnabled) return;
  updatePreviousFocus = document.activeElement;
  byId("updateModal").classList.add("show");
  byId("btnCloseUpdate").focus();
  void loadAboutVersion();
  void refreshUpdateState();
  renderUpdateState();
}
function closeUpdateModal() {
  byId("updateModal").classList.remove("show");
  if (updatePreviousFocus?.isConnected) updatePreviousFocus.focus();
}
byId("btnAboutVersion").addEventListener("click", openUpdateModal);
byId("btnCloseUpdate").addEventListener("click", closeUpdateModal);
byId("updateModal").addEventListener("click", event => { if (event.target === byId("updateModal")) closeUpdateModal(); });
document.addEventListener("keydown", event => { if (event.key === "Escape" && byId("updateModal").classList.contains("show")) { event.preventDefault(); closeUpdateModal(); } });
window.addEventListener("kyf-update-state", event => { applyUpdateState(event.detail); });
byId("btnCheckUpdates").addEventListener("click", () => { void checkForUpdates(); });
byId("btnBeginUpdate").addEventListener("click", async () => {
  if (!appUpdatesEnabled || updateActionPending || !["available", "ready"].includes(updatePhase(updateState))) return;
  updateActionPending = true;
  updateOperationStatus = updatePhase(updateState) === "ready" ? "正在安装更新" : "正在启动更新";
  renderUpdateState();
  const before = updateStateSequence;
  try {
    const result = await window.desktopHost.beginUpdate();
    if (result === false) throw new Error("更新未启动");
    if (before === updateStateSequence && !applyUpdateState(result)) void refreshUpdateState();
  } catch (error) {
    if (before === updateStateSequence) applyUpdateState({ phase: "failed", error: error?.message || "请重试" });
  } finally {
    updateActionPending = false;
    updateOperationStatus = "";
    renderUpdateState();
  }
});
byId("btnRollbackUpdate").addEventListener("click", async () => {
  if (!appUpdatesEnabled || updateActionPending || !updateState.rollbackVersion ||
      typeof window.desktopHost?.rollbackUpdate !== "function") return;
  updateActionPending = true;
  updateOperationStatus = "正在回退版本";
  renderUpdateState();
  const before = updateStateSequence;
  try {
    const result = await window.desktopHost.rollbackUpdate();
    if (result === false) throw new Error("回退未启动");
    if (before === updateStateSequence && !applyUpdateState(result)) void refreshUpdateState();
  } catch (error) {
    if (before === updateStateSequence) applyUpdateState({ phase: "failed", error: error?.message || "回退失败" });
  } finally {
    updateActionPending = false;
    updateOperationStatus = "";
    renderUpdateState();
  }
});
void loadAboutVersion();
renderUpdateState();
function showSettingsPanel() {
  byId("settingsModal").classList.add("show");
  void window.ModelTraces?.refreshSettings();
  if (byId("panelPlugins")?.classList.contains("active")) void window.PluginSettings?.refresh();
  if (byId("panelModels")?.classList.contains("active")) void window.CoreModelSettings?.refresh();
  void loadModelSource();
  void loadDataRoot();
}
async function openAppSettings(target) {
  if (!window.desktopHost?.isSettingsWindow && window.desktopHost?.openSettingsWindow) {
    try {
      if (await window.desktopHost.openSettingsWindow(target || {}) !== true) toast("设置窗口未能打开，请重试");
    } catch { toast("设置窗口未能打开，请重试"); }
    return;
  }
  showSettingsPanel();
  if (target) applySettingsTarget(target);
}
function applySettingsTarget(target = {}) {
  const activate = () => {
    const tab = ["general", "models", "plugins", "about"].includes(target.tab) ? target.tab : "general";
    document.querySelector(`.settings-tab-btn[data-tab="${tab}"]`)?.click();
    if (target.section === "conversations") openConversationManager();
    else if (target.section === "source") byId("inputDataRoot")?.focus();
    else if (target.section === "update") openUpdateModal();
    if (target.plugin && tab === "plugins") void window.PluginSettings?.openSettings(target.plugin);
  };
  if (window.PluginSettings?.requestLeave) window.PluginSettings.requestLeave(activate);
  else activate();
}
window.openAppSettings = openAppSettings;
byId("btnSettings").addEventListener("click", () => { void openAppSettings(); });
byId("btnUpdateNotice").addEventListener("click", () => {
  if (!appUpdatesEnabled) return;
  void openAppSettings({ tab: "about", section: "update" });
});
byId("btnAddConversation").addEventListener("click", openConversationManager);
byId("btnManageConversations").addEventListener("click", () => {
  const panel = byId("conversationManager");
  if (panel.hidden) openConversationManager();
  else closeConversationManager();
});
byId("conversationSearch").addEventListener("input", renderConversationManager);
byId("conversationSelectAll").addEventListener("change", event => selectAllRemovableConversations(event.target.checked));
byId("btnClearConversationSelection").addEventListener("click", clearConversationRemovalSelection);
byId("conversationAutoFollow").addEventListener("change", event => setConversationAutoFollow(event.target.checked));
byId("btnAddAllConversations").addEventListener("click", () => { void addAllConversations(); });
byId("btnRemoveSelectedConversations").addEventListener("click", () => { void removeSelectedConversations(); });
byId("btnCloseSettings").addEventListener("click", closeSettingsModal);
byId("settingsModal").addEventListener("click", event => { if (event.target === byId("settingsModal")) closeSettingsModal(); });
byId("btnManageAccounts").addEventListener("click", () => {
  const panel = byId("accountManager");
  panel.hidden = !panel.hidden;
  text("btnManageAccounts", panel.hidden ? "查看账号" : "收起");
  if (!panel.hidden) void loadAccounts();
});
byId("btnRefreshAccounts").addEventListener("click", () => { void loadAccounts(); });
byId("btnRetryAccountCheck").addEventListener("click", () => { void loadSessions(); });
byId("btnToggleAccountManagement").addEventListener("click", () => {
  accountManagementOpen = !accountManagementOpen;
  text("btnToggleAccountManagement", accountManagementOpen ? "完成" : "管理");
  byId("btnToggleAccountManagement").setAttribute("aria-pressed", String(accountManagementOpen));
  if (!accountManagementOpen) {
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
  }
  renderManagedAccounts();
});
byId("btnCancelDeleteAccount").addEventListener("click", () => {
  pendingDeleteAccountId = null;
  byId("accountDeleteConfirm").hidden = true;
});
byId("btnConfirmDeleteAccount").addEventListener("click", () => { void deleteManagedAccount(); });
// Modified 2026-10-16: model settings have their own top-level panel.
document.querySelectorAll(".settings-tab-btn").forEach(tab => tab.addEventListener("click", () => {
  const activate = () => {
    if (tab.dataset.tab !== "models") hideApiKey();
    document.querySelectorAll(".settings-tab-btn").forEach(node => node.classList.toggle("active", node === tab));
    document.querySelectorAll(".settings-panel").forEach(node => node.classList.toggle("active", node.id === ({ general: "panelGeneral", models: "panelModels", plugins: "panelPlugins", about: "panelAbout" })[tab.dataset.tab]));
    if (tab.dataset.tab === "about") void loadAboutVersion();
    if (tab.dataset.tab === "models") void window.CoreModelSettings?.refresh();
    if (tab.dataset.tab === "plugins") void window.PluginSettings?.refresh();
  };
  if (window.PluginSettings?.requestLeave) window.PluginSettings.requestLeave(activate);
  else activate();
}));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !event.defaultPrevented && byId("settingsModal").classList.contains("show")) { event.preventDefault(); closeSettingsModal(); }
});
byId("btnEmoji").addEventListener("click", event => { event.stopPropagation(); byId("emojiPopover").classList.toggle("show"); });
document.querySelectorAll(".popover-tab").forEach(tab => tab.addEventListener("click", event => {
  event.stopPropagation();
  document.querySelectorAll(".popover-tab").forEach(node => node.classList.toggle("active", node === tab));
  byId("emojiGrid").classList.toggle("active", tab.dataset.tab === "emoji");
  byId("kaomojiGrid").classList.toggle("active", tab.dataset.tab === "kaomoji");
}));
function insertKaomoji(value) {
  const input = byId("chatInput");
  input.setRangeText(value, input.selectionStart, input.selectionEnd, "end");
  input.dispatchEvent(new Event("input"));
  input.focus();
  byId("emojiPopover").classList.remove("show");
}
document.querySelectorAll(".emoji-btn").forEach(button => button.addEventListener("click", () => insertKaomoji(button.textContent)));
byId("kaomojiGrid").addEventListener("click", event => {
  const button = event.target.closest(".kaomoji-btn");
  if (button) insertKaomoji(button.textContent);
});
function renderKaomojiPanel() {
  const grid = byId("kaomojiGrid");
  grid.replaceChildren();
  for (const group of window.Kaomoji.panelItems()) {
    const section = element("section", "kaomoji-category");
    section.appendChild(element("div", "kaomoji-category-title", group.category));
    const items = element("div", "kaomoji-category-items");
    for (const item of group.items) for (const variant of item.variants) {
      const button = element("button", "kaomoji-btn", variant);
      button.type = "button";
      button.title = item.label;
      items.appendChild(button);
    }
    section.appendChild(items);
    grid.appendChild(section);
  }
}
document.addEventListener("click", event => { if (!event.target.closest("#emojiPopover, #btnEmoji")) byId("emojiPopover").classList.remove("show"); });
document.addEventListener("keydown", event => { if (event.key === "Escape") { closeHistorySearch(); byId("emojiPopover").classList.remove("show"); } });
renderKaomojiPanel();
applySettings();
async function startInitialLoad() {
  const attempt = ++startupAttempt;
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  startupAccountRetryUsed = false;
  showStartup("account", "正在连接当前微信账号…");
  let healthReady = false;
  try {
    const data = await api("/api/health");
    if (attempt !== startupAttempt || !startupActive) return;
    healthReady = data.data?.state === "ready";
    if (data.data?.state === "error") notifyStatus("数据源不可用");
  } catch { }
  if (attempt !== startupAttempt || !startupActive) return;
  showStartup(healthReady ? "sessions" : "account", healthReady ? "正在读取会话列表…" : "正在确认当前微信账号…");
  void loadSessions();
}
byId("startupRetry").addEventListener("click", retryStartup);
byId("databaseLoadingRetry").addEventListener("click", () => { void loadSessions(); });
byId("startupContinue").addEventListener("click", () => { if (startupActive && accountUnavailable) completeStartup(); });
const updateValidationMode = window.desktopHost?.updateValidationMode === true;
window.ConversationMenu?.init({ state: chatState, byId, renderSessions, deleteConversation, setStatus: notifyStatus });
window.MessageMenu?.init({ state: chatState, byId, toast });
window.SessionResize?.init({ pane: byId("sessionPane"), handle: byId("sessionResizeHandle"),
  container: byId("appWindow"), rail: document.querySelector(".nav-rail") });
function keepComposerScroll() {
  if (chatState.view !== "chat" || !chatState.followLatest || chatState.historyState || window.MessageAnalysis?.isReading()) return;
  const messages = byId("chatMessages");
  messages.scrollTop = messages.scrollHeight;
  chatState.lastChatScrollTop = messages.scrollTop;
}
window.ComposerResize?.init({ pane: byId("chatInputPane"), handle: byId("chatInputResizeHandle"),
  container: byId("chatView"), mainArea: byId("chatMainArea"), onResize: keepComposerScroll });
const updateFinalReadyMode = window.desktopHost?.updateFinalReadyMode === true;
let updateCommitReady = !updateFinalReadyMode;
if (updateValidationMode) {
  // The updater checks that this script and its desktop bridge actually run
  // before opening a writable chat session in the new installation.
  byId("startupOverlay").hidden = false;
  byId("appWindow").setAttribute("inert", "");
  byId("startupOverlay").classList.add("update-validation");
  text("startupStatus", "正在验证新版本…");
  void window.desktopHost.reportUiReady().catch(() => {});
} else if (updateFinalReadyMode) {
  byId("startupOverlay").hidden = false;
  byId("appWindow").setAttribute("inert", "");
  text("startupStatus", "正在完成更新…");
  void window.desktopHost.reportUiReady().then(ready => {
    if (!ready) return;
    updateCommitReady = true;
    unlockStartupUi();
    void startInitialLoad();
  }).catch(() => {});
} else if (!window.desktopHost?.isSettingsWindow) {
  void startInitialLoad();
  scheduleBackgroundUpdateChecks();
}
if (!updateValidationMode) void loadModelSource();
window.addEventListener("kyf-service-restored", () => {
  if (updateValidationMode || !updateCommitReady) return;
  window.ChatMedia?.reset();
  window.MessageAnalysis?.recover();
  beginUnknownModelSource();
  if (byId("settingsModal").classList.contains("show")) invalidateModelDiscovery();
  void loadModelSource(true);
  if (byId("settingsModal").classList.contains("show") && byId("panelModels")?.classList.contains("active")) void window.CoreModelSettings?.refresh();
  void loadSessions();
  // Refresh in place: a bridge recovery is not an account or conversation change.
  if (chatState.currentUser && chatState.controller && !chatState.historyState)
    void loadMessages(chatState.generation, true, true);
});
if (!updateValidationMode && !window.desktopHost?.isSettingsWindow) {
  setInterval(() => { if (updateCommitReady && chatState.currentUser && !document.hidden) loadMessages(chatState.generation, true); }, 4000);
  setInterval(() => { if (updateCommitReady && !document.hidden) loadSessions(); }, 15000);
}
window.MediaSettings?.init({ account: () => chatState.currentAccount, request: api });
window.ModelTraces?.init({ account: chatState.currentAccount, user: chatState.currentUser, view: chatState.view });
// Modified 2026-10-13: enabling, disabling or rescanning plugins in 设置 → 插件 refreshes every contribution slot.
async function pluginsChanged(listing) {
  window.dispatchEvent(new CustomEvent("kyf-plugins-changed", { detail: listing }));
  await window.MessageAnalysis?.pluginsChanged();
  const container = byId("chatMessages");
  if (chatState.view === "chat" && chatState.currentUser && chatState.messages.length) {
    // Rebuild the open chat's bubbles so message annotations attach or detach without switching chats.
    const messages = chatState.messages, scrollTop = container.scrollTop;
    chatState.messages = []; container.replaceChildren();
    renderMessages(messages, { followLatest: chatState.followLatest, scrollTop });
  }
}
window.PluginSettings?.init({ onChange: listing => {
  window.SettingsSync?.changed("/api/plugins");
  if (!window.desktopHost?.isSettingsWindow) void pluginsChanged(listing);
},
  onModelSettings() {
    document.querySelector('.settings-tab-btn[data-tab="models"]')?.click();
    byId("apiModelSettings")?.scrollIntoView({ block: "start" });
    byId("selectApiProtocol")?.focus();
  },
  onOpenNav(plugin, nav) {
    if (window.desktopHost?.isSettingsWindow) { void window.desktopHost.openSettingsMainNav(plugin, nav); return; }
    closeSettingsModal(); window.PluginSlots?.openNav(plugin, nav);
  }
});
if (!window.desktopHost?.isSettingsWindow) window.PluginSlots?.init({ switchView, toast, openFriendWindow, updateFriendButton,
  focusChat(scope) {
    if (scope.account !== chatState.currentAccount || !chatState.sessions.has(scope.user)) return false;
    switchSession(scope.user); return true;
  } });
document.addEventListener("visibilitychange", () => {
  if (document.hidden || updateValidationMode || !updateCommitReady) return;
  void loadSessions();
  if (chatState.currentUser) void loadMessages(chatState.generation, true, true);
});
window.desktopHost?.onSettingsMainNav?.(target => { window.PluginSlots?.openNav(target.plugin, target.nav); });
window.SettingsSync?.listen((path, clearedAccount) => {
  if (path === "/api/accounts" && clearedAccount) {
    void clearAccountUiData(clearedAccount).then(accounts => {
      for (const account of accounts) window.PluginSlots?.onAccountCleared(account);
    }).catch(() => { toast("账号已清除，请重新打开应用以刷新界面缓存"); });
  }
  if (path === "/api/plugins") { void window.PluginSettings?.refresh(); }
  else if (path.startsWith("/api/model-source/")) { void loadModelSource(true); }
  else if (path === "/api/conversation-selection" || path.startsWith("/api/data-root") || path === "/api/accounts") {
    // Fence an in-flight directory read as well as invalidating its cached selection.
    chatState.sessionRequest++;
    chatState.selectionLoadedAccount = null;
    chatState.sessionSignature = null;
    void loadSessions();
    if (path.startsWith("/api/data-root")) void loadDataRoot();
    if (!byId("accountManager").hidden) void loadAccounts();
  }
});
window.addEventListener("storage", event => {
  if (event.key !== "real-ui-settings-1") return;
  try {
    const next = JSON.parse(event.newValue || "{}");
    if (["light", "dark"].includes(next.theme)) settingsState.settings.theme = next.theme;
    if (["0.9", "1.0", "1.1", "1.25", "1.5"].includes(next.zoom)) settingsState.settings.zoom = next.zoom;
    applySettings();
  } catch { }
});
if (window.desktopHost?.isSettingsWindow) {
  let targetReceived = false;
  window.addEventListener("focus", () => {
    void loadSessions();
    void window.MediaSettings?.refresh?.();
  });
  window.desktopHost.onSettingsCloseRequested?.(closeSettingsModal);
  window.desktopHost.onSettingsTarget?.(target => { targetReceived = true; applySettingsTarget(target); });
  showSettingsPanel();
  void loadSessions();
  void window.desktopHost.getSettingsTarget?.().then(target => { if (target && !targetReceived) applySettingsTarget(target); });
}
