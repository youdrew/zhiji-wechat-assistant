/* Know Your Friends, 2026-10-13. Apache-2.0. Local conversation pin and delete menu. */
(function (host) {
  "use strict";

  const attachedRows = new WeakSet();
  let config = null, menu = null, pinButton = null, deleteButton = null;
  let target = null, initialized = false;
  const storageKey = account => `conversation-pins-v1:${account}`;
  const epoch = state => state.conversationSelectionEpoch ?? 0;

  function readPins(account) {
    const pins = Object.create(null);
    if (!account) return pins;
    const value = JSON.parse(host.localStorage.getItem(storageKey(account)) || "{}");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [user, pinned] of Object.entries(value)) {
        if (typeof pinned === "boolean") pins[user] = pinned;
      }
    }
    return pins;
  }

  function visiblePins() {
    try { return readPins(config?.state.currentAccount); }
    catch (_) { return Object.create(null); }
  }

  function effectivePinned(session, pins) {
    return Object.prototype.hasOwnProperty.call(pins, session.username)
      ? pins[session.username] : !!session.pinned;
  }

  function isPinned(session) {
    return effectivePinned(session, visiblePins());
  }

  function sortSessions(sessions) {
    const pins = visiblePins();
    const timestamp = session => {
      const value = Number(session.sortTimestamp || session.time);
      return Number.isFinite(value) ? value : 0;
    };
    return sessions.map((session, index) => ({ session, index }))
      .sort((left, right) =>
        Number(effectivePinned(right.session, pins)) - Number(effectivePinned(left.session, pins)) ||
        timestamp(right.session) - timestamp(left.session) || left.index - right.index)
      .map(item => item.session);
  }

  function validTarget(candidate = target) {
    if (!config || !candidate) return false;
    const state = config.state;
    return !!state.currentAccount && candidate.account === state.currentAccount &&
      state.selectionLoadedAccount === candidate.account && candidate.epoch === epoch(state) &&
      !state.conversationSelectionBusy && state.selectedConversations.has(candidate.user) &&
      state.sessions.has(candidate.user) && candidate.row.isConnected &&
      candidate.row.dataset.id === candidate.user;
  }

  function close(restoreFocus = false) {
    const row = target?.row;
    target = null;
    if (menu) menu.hidden = true;
    if (row) {
      row.classList.remove("context-open");
      row.setAttribute("aria-expanded", "false");
      if (restoreFocus && row.isConnected) row.focus();
    }
  }

  function refresh() {
    if (!target) return;
    if (!validTarget()) { close(); return; }
    pinButton.textContent = isPinned(config.state.sessions.get(target.user)) ? "取消置顶" : "置顶";
  }

  function open(row, x, y) {
    close();
    if (!config || !menu) return;
    const candidate = { row, user: row.dataset.id, account: config.state.currentAccount,
      epoch: epoch(config.state) };
    if (!validTarget(candidate)) return;
    target = candidate;
    row.classList.add("context-open");
    row.setAttribute("aria-expanded", "true");
    refresh();
    menu.hidden = false;
    const bounds = menu.getBoundingClientRect();
    const zoom = Number(document.documentElement.style?.zoom) || 1;
    const width = host.innerWidth || document.documentElement.clientWidth;
    const height = host.innerHeight || document.documentElement.clientHeight;
    const margin = 8;
    // Pointer coordinates and rendered bounds include the root CSS zoom; fixed offsets do not.
    menu.style.left = `${Math.max(margin, Math.min(x, width - bounds.width - margin)) / zoom}px`;
    menu.style.top = `${Math.max(margin, Math.min(y, height - bounds.height - margin)) / zoom}px`;
    pinButton.focus();
  }

  function attach(row) {
    if (attachedRows.has(row)) return;
    attachedRows.add(row);
    row.setAttribute("aria-haspopup", "menu");
    row.setAttribute("aria-controls", "sessionContextMenu");
    row.setAttribute("aria-expanded", "false");
    row.addEventListener("contextmenu", event => {
      event.preventDefault();
      event.stopPropagation();
      open(row, event.clientX, event.clientY);
    });
    row.addEventListener("keydown", event => {
      if (event.key !== "ContextMenu" && !(event.key === "F10" && event.shiftKey)) return;
      event.preventDefault();
      event.stopPropagation();
      const bounds = row.getBoundingClientRect();
      open(row, bounds.left + Math.min(32, bounds.width / 2), bounds.bottom);
    });
  }

  function togglePin() {
    if (!validTarget()) { close(); return; }
    const captured = target;
    let pinned;
    try {
      const pins = readPins(captured.account);
      pinned = !effectivePinned(config.state.sessions.get(captured.user), pins);
      pins[captured.user] = pinned;
      host.localStorage.setItem(storageKey(captured.account), JSON.stringify(pins));
    } catch (_) {
      config.setStatus("置顶设置保存失败，请检查本地存储后重试");
      return;
    }
    close();
    config.renderSessions();
    const row = validTarget(captured) ? captured.row : [...config.byId("sessionList").children]
      .find(item => item.dataset.id === captured.user);
    if (row && validTarget({ ...captured, row })) row.focus();
    config.setStatus(pinned ? "会话已置顶" : "已取消置顶");
  }

  function removeConversation() {
    if (!validTarget()) { close(); return; }
    const captured = target;
    close();
    void config.deleteConversation(captured.user, captured.account, captured.epoch);
  }

  function init(options) {
    close();
    config = options;
    if (initialized) return;
    menu = config.byId("sessionContextMenu");
    pinButton = config.byId("btnPinConversation");
    deleteButton = config.byId("btnDeleteConversation");
    if (!menu || !pinButton || !deleteButton) return;
    initialized = true;
    menu.hidden = true;
    pinButton.addEventListener("click", togglePin);
    deleteButton.addEventListener("click", removeConversation);
    menu.addEventListener("contextmenu", event => event.preventDefault());
    menu.addEventListener("keydown", event => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      if (!validTarget()) { close(); return; }
      event.preventDefault();
      event.stopPropagation();
      const items = [pinButton, deleteButton];
      const current = items.indexOf(document.activeElement);
      let next;
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = items.length - 1;
      else if (current < 0) next = event.key === "ArrowDown" ? 0 : items.length - 1;
      else next = (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next].focus();
    });
    const dismissOutside = event => {
      if (target && !menu.contains(event.target)) close();
    };
    document.addEventListener("pointerdown", dismissOutside);
    document.addEventListener("contextmenu", dismissOutside);
    document.addEventListener("keydown", event => {
      if (!target || !["Escape", "Tab"].includes(event.key)) return;
      if (!validTarget()) { close(); return; }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
      }
      close(true);
    });
    config.byId("sessionList")?.addEventListener("scroll", () => close());
    host.addEventListener("resize", () => close());
    host.addEventListener("blur", () => close());
  }

  host.ConversationMenu = { init, attach, isPinned, sortSessions, refresh, close };
})(window);
