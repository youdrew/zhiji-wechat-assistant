"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// One fixed conversation per browser; browsing never mutates the main chat or its draft.
(function () {
  const categories = [["all", "全部"], ["files", "文件"], ["media", "媒体"], ["links", "链接"],
    ["music", "音乐"], ["mini-programs", "小程序"], ["channels", "视频号"]];
  const el = (tag, className = "", value) => {
    const node = document.createElement(tag);
    node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  };
  function button(label, className, action) {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  const localDate = value => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  function messageTime(value) {
    const date = new Date(value);
    if (value == null || !Number.isFinite(date.getTime())) return "";
    return `${localDate(date)} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  }
  function safeAvatar(value) {
    if (typeof value !== "string" || value.length > 2048) return false;
    try {
      const url = new URL(value, window.location.href);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password;
    } catch { return false; }
  }
  function create({ container, scope, api, onClose, onLocate, validateScope }) {
    const fixed = { ...scope };
    let serial = 0, controller = null, destroyed = false, suspended = false;
    let category = "all", date = "", page = 0, pageStarts = [null], hasMore = false, pending = false;
    let attemptedPage = 0, inputTimer = null, selectedDate = "", calendarMonth = new Date();
    const root = el("section", "history-browser");
    const heading = el("header", "history-heading");
    const title = el("h1", "", `与“${fixed.name || fixed.user}”的聊天记录`);
    heading.append(title);
    if (onClose) heading.append(button("×", "history-close", onClose));
    heading.querySelector("button")?.setAttribute("aria-label", "关闭聊天记录");
    const search = el("form", "history-search");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M16 16l5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0"); icon.append(path);
    const keyword = el("input"); keyword.type = "search"; keyword.placeholder = "搜索聊天记录";
    keyword.maxLength = 256; keyword.setAttribute("aria-label", "搜索聊天记录");
    const searchButton = el("button", "", "搜索"); searchButton.type = "submit";
    search.append(icon, keyword, searchButton);
    const filterBar = el("div", "history-filter-bar");
    const tabs = el("div", "history-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "记录分类");
    const tabButtons = categories.map(([key, label], index) => {
      const tab = button(label, "history-tab", () => {
        if (key === category || suspended) return;
        category = key; updateTabs(); restart();
      });
      tab.dataset.category = key; tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(index === 0));
      tab.tabIndex = index === 0 ? 0 : -1;
      tabs.append(tab);
      return tab;
    });
    const dateToggle = button("日期 ›", "history-date-toggle", () => {
      if (!calendar.hidden) { closeCalendar(); return; }
      selectedDate = date || localDate(new Date());
      calendarMonth = new Date(`${selectedDate}T12:00:00`);
      renderCalendar(); calendar.hidden = false; dateToggle.setAttribute("aria-expanded", "true");
      days.querySelector('[aria-pressed="true"]')?.focus();
    });
    dateToggle.setAttribute("aria-expanded", "false"); dateToggle.setAttribute("aria-pressed", "false");
    const calendar = el("section", "history-calendar"); calendar.hidden = true;
    calendar.setAttribute("role", "dialog"); calendar.setAttribute("aria-label", "选择发送日期");
    calendar.append(el("p", "history-calendar-title", "选择发送日期"));
    const selectors = el("div", "history-calendar-selects");
    const year = el("select"), month = el("select");
    year.setAttribute("aria-label", "年份"); month.setAttribute("aria-label", "月份");
    for (let value = 1990; value <= new Date().getFullYear() + 1; value++) {
      const option = el("option", "", value); option.value = String(value); year.append(option);
    }
    for (let value = 1; value <= 12; value++) {
      const option = el("option", "", `${value} 月`); option.value = String(value - 1); month.append(option);
    }
    selectors.append(year, month); calendar.append(selectors);
    const days = el("div", "history-calendar-grid"); calendar.append(days);
    const calendarActions = el("div", "history-calendar-actions");
    const applyDate = button("确定", "history-apply-date", () => {
      date = selectedDate; closeCalendar(); updateDate(); restart();
    });
    calendarActions.append(applyDate, button("取消", "", closeCalendar)); calendar.append(calendarActions);
    filterBar.append(tabs, dateToggle, calendar);
    const activeDate = el("div", "history-active-date"); activeDate.hidden = true;
    const dateLabel = el("span"); activeDate.append(dateLabel, button("×", "", () => {
      date = ""; updateDate(); restart();
    }));
    activeDate.querySelector("button").setAttribute("aria-label", "清除日期筛选");
    const progress = el("div", "history-progress");
    const status = el("span", "history-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const cancel = button("取消", "", () => { stop(); status.textContent = "已取消读取"; retry.hidden = false; }); cancel.hidden = true;
    const retry = button("重试", "", () => { suspended ? void refresh() : void loadPage(attemptedPage); }); retry.hidden = true;
    progress.append(status, cancel, retry);
    const records = el("div", "history-records"); records.setAttribute("role", "tabpanel"); records.setAttribute("aria-label", "聊天记录列表");
    records.tabIndex = 0;
    const pages = el("footer", "history-pages");
    const previous = button("上一页", "", () => void loadPage(page - 1));
    const next = button("下一页", "", () => void loadPage(page + 1));
    const pageLabel = el("span"); pages.append(previous, pageLabel, next);
    root.append(heading, search, filterBar, activeDate, progress, records, pages); container.replaceChildren(root);

    function updateTabs() {
      for (const tab of tabButtons) {
        const active = tab.dataset.category === category;
        tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1;
      }
    }
    function updateDate() {
      activeDate.hidden = !date; dateLabel.textContent = date;
      dateToggle.setAttribute("aria-pressed", String(!!date));
    }
    function closeCalendar() {
      calendar.hidden = true; dateToggle.setAttribute("aria-expanded", "false");
    }
    function renderCalendar() {
      applyDate.disabled = !selectedDate;
      year.value = String(calendarMonth.getFullYear()); month.value = String(calendarMonth.getMonth());
      days.replaceChildren();
      for (const label of ["一", "二", "三", "四", "五", "六", "日"]) days.append(el("span", "history-weekday", label));
      const first = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth(), 1);
      for (let i = 0; i < (first.getDay() + 6) % 7; i++) days.append(el("span"));
      const count = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
      for (let day = 1; day <= count; day++) {
        const value = localDate(new Date(first.getFullYear(), first.getMonth(), day));
        const item = button(day, "history-day", () => {
          selectedDate = value; renderCalendar(); days.querySelector('[aria-pressed="true"]')?.focus();
        });
        item.setAttribute("aria-label", value); item.setAttribute("aria-pressed", String(value === selectedDate));
        days.append(item);
      }
    }
    for (const selector of [year, month]) selector.addEventListener("change", () => {
      selectedDate = ""; calendarMonth = new Date(Number(year.value), Number(month.value), 1); renderCalendar();
    });
    tabs.addEventListener("keydown", event => {
      const index = tabButtons.indexOf(event.target);
      if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? 0 : event.key === "End" ? tabButtons.length - 1 :
        (index + (event.key === "ArrowRight" ? 1 : -1) + tabButtons.length) % tabButtons.length;
      tabButtons[target].click(); tabButtons[target].focus();
    });
    root.addEventListener("keydown", event => {
      if (event.key === "Escape" && !calendar.hidden) { event.preventDefault(); event.stopPropagation(); closeCalendar(); dateToggle.focus(); }
      else if (event.key === "Enter" && !calendar.hidden && !["SELECT", "BUTTON"].includes(event.target.tagName)) {
        event.preventDefault(); applyDate.click();
      }
    });
    const outsideCalendar = event => { if (!filterBar.contains(event.target)) closeCalendar(); };
    document.addEventListener("pointerdown", outsideCalendar);
    search.addEventListener("submit", event => { event.preventDefault(); restart(); });
    keyword.addEventListener("input", () => {
      clearTimeout(inputTimer); inputTimer = setTimeout(restart, 300);
    });

    function updatePaging() {
      previous.hidden = page === 0; previous.disabled = pending || suspended;
      next.hidden = !hasMore; next.disabled = pending || suspended;
      pageLabel.textContent = records.children.length || page ? `第 ${page + 1} 页` : "";
      cancel.hidden = !pending; records.setAttribute("aria-busy", String(pending));
    }
    function stop() {
      serial++; controller?.abort(); controller = null; pending = false;
      window.ChatMedia?.reset();
      clearTimeout(inputTimer); inputTimer = null; updatePaging();
    }
    function clearRecords() { window.ChatMedia?.reset(); records.replaceChildren(); }
    function suspend(message = "微信账号已变化，聊天记录已隐藏。请从聊天重新打开。") {
      stop(); suspended = true; clearRecords(); hasMore = false; closeCalendar();
      status.textContent = message; retry.hidden = true;
      for (const control of [keyword, searchButton, dateToggle, ...tabButtons]) control.disabled = true;
      updatePaging();
    }
    function render(items) {
      clearRecords();
      for (const message of items) {
        const row = el("article", "history-record"); row.dataset.messageId = String(message.id);
        const sender = message.senderName || (message.side === "self" ? fixed.self?.name || "我" : fixed.name || message.senderId || "对方");
        const avatar = el("span", "history-avatar", Array.from(sender)[0] || "?"); avatar.setAttribute("aria-hidden", "true");
        const candidates = [message.senderAvatar, ...(message.senderAvatarCandidates || []),
          ...(message.side === "self" ? [fixed.self?.avatar, ...(fixed.self?.avatarCandidates || [])] :
            [fixed.avatar, ...(fixed.avatarCandidates || [])])].filter(safeAvatar);
        if (candidates.length) {
          const image = el("img"); image.alt = ""; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
          let index = 0; image.addEventListener("error", () => {
            if (!row.isConnected || destroyed || suspended) return;
            if (++index < candidates.length) image.src = candidates[index]; else image.remove();
          });
          image.src = candidates[0]; avatar.append(image);
        }
        const body = el("div", "history-record-body"), meta = el("div", "history-record-meta");
        const timestamp = el("time", "", messageTime(message.time));
        if (timestamp.textContent) timestamp.dateTime = new Date(message.time).toISOString();
        meta.append(el("span", "history-sender", sender), timestamp);
        const content = el("div", "history-content");
        const mediaScope = { account: fixed.account, user: fixed.user, generation: serial };
        const media = window.ChatMedia?.render(message, mediaScope,
          captured => !destroyed && !suspended && captured.generation === serial && row.isConnected);
        content.append(media || el("span", "", message.text || `[${categories.find(([key]) => key === message.historyCategory)?.[1] || "非文字消息"}]`));
        body.append(meta, content);
        if (onLocate && message.historyCursor && message.id) {
          body.append(button("定位到聊天", "history-locate", () => {
            if (!destroyed && !suspended) onLocate({ account: fixed.account, user: fixed.user, cursor: message.historyCursor, messageId: String(message.id) });
          }));
        }
        row.append(avatar, body); records.append(row);
      }
      records.scrollTop = 0;
    }
    async function loadPage(index) {
      if (destroyed || suspended || index < 0 || index > 0 && !pageStarts[index]) return;
      stop(); const request = serial, abort = new AbortController(); controller = abort; pending = true;
      attemptedPage = index; retry.hidden = true; status.textContent = "正在读取…"; updatePaging();
      const current = () => !destroyed && !suspended && request === serial && !abort.signal.aborted;
      const query = keyword.value.trim(), selectedCategory = category, selectedDay = date;
      try {
        if (validateScope && !await validateScope(abort.signal)) { if (current()) suspend(); return; }
        let cursor = pageStarts[index], data;
        // Sparse categories may span many raw messages. Bound work per action; the next page continues the scan.
        for (let scan = 0; scan < 4; scan++) {
          const params = new URLSearchParams({ account: fixed.account, user: fixed.user, category: selectedCategory, limit: "50" });
          if (query) params.set("q", query); if (selectedDay) params.set("date", selectedDay); if (cursor) params.set("before", cursor);
          data = await api(`/api/history/search?${params}`, {}, abort.signal);
          if (!current()) return;
          if (data.account !== fixed.account || data.user !== fixed.user) { suspend(); return; }
          if (!Array.isArray(data.messages)) throw new Error("Invalid history response");
          if (data.hasMore && (typeof data.nextCursor !== "string" || !data.nextCursor || data.nextCursor === cursor)) throw new Error("History cursor did not advance");
          cursor = data.nextCursor || null;
          if (data.messages.length || !data.hasMore) break;
        }
        if (validateScope && !await validateScope(abort.signal)) { if (current()) suspend(); return; }
        if (!current()) return;
        page = index; hasMore = !!data.hasMore; pageStarts[index + 1] = cursor;
        render(data.messages);
        status.textContent = data.messages.length ? "" : hasMore ? "本次未找到匹配记录，可继续查找" :
          query || selectedDay || selectedCategory !== "all" ? "没有找到匹配的聊天记录" : "暂无聊天记录";
        next.textContent = data.messages.length ? "下一页" : "继续查找";
      } catch (error) {
        if (!current()) return;
        if (["AccountChangedError", "AccountUnavailableError", "account-paused", "account-closed"].includes(error.code)) { suspend(); return; }
        if (error.code === "ui-session-forbidden") { suspend("此聊天记录窗口的授权已失效。"); return; }
        status.textContent = "读取失败，请重试"; retry.hidden = false;
      } finally {
        if (request === serial) { pending = false; controller = null; updatePaging(); }
      }
    }
    function restart() {
      if (destroyed || suspended) return;
      stop(); page = 0; pageStarts = [null]; hasMore = false; clearRecords(); closeCalendar();
      void loadPage(0);
    }
    async function refresh() {
      if (destroyed) return;
      if (suspended) {
        const token = serial;
        try { if (validateScope && !await validateScope() || destroyed || token !== serial) return; }
        catch { return; }
        suspended = false;
        for (const control of [keyword, searchButton, dateToggle, ...tabButtons]) control.disabled = false;
      }
      restart();
    }
    function destroy() {
      destroyed = true; stop(); clearRecords(); document.removeEventListener("pointerdown", outsideCalendar);
      container.replaceChildren();
    }
    updatePaging(); restart();
    return Object.freeze({ cancel: stop, suspend, refresh, destroy, focus: () => keyword.focus() });
  }
  window.ChatHistory = Object.freeze({ create });

  if (document.body.classList.contains("history-window")) void (async () => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const host = window.historyHost;
    const restricted = !host && params.get("restricted") === "1";
    const scope = await host?.getContext?.() || Object.fromEntries(params);
    if (![scope.account, scope.user].every(value => typeof value === "string" && value && value.length <= 256 && !/[\u0000-\u001f\u007f]/u.test(value))) {
      document.getElementById("historyRoot").textContent = "请从聊天打开聊天记录。"; return;
    }
    const theme = value => document.body.classList.toggle("theme-light", value === "light"); theme(scope.theme);
    const api = async (url, _options = {}, signal) => {
      const response = await fetch(url, { signal }); const data = await response.json();
      if (!response.ok || data?.error) throw Object.assign(new Error("History request failed"), { code: data?.error });
      return data;
    };
    // The Agent host isolates storage and Core pins every history/media request to this account and contact.
    if (restricted) {
      const browser = create({ container: document.getElementById("historyRoot"), scope, api });
      window.addEventListener("beforeunload", browser.destroy);
      return;
    }
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(scope.account));
    const accountId = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
    const validateScope = async signal => (await api("/api/accounts", {}, signal)).currentAccountId === accountId;
    const browser = create({ container: document.getElementById("historyRoot"), scope, api, validateScope,
      onLocate: target => { void host?.locate?.(target); } });
    let channel;
    try { channel = new BroadcastChannel("kyf-friend-analysis"); } catch { }
    channel?.addEventListener("message", event => {
      const message = event.data;
      // As in 知己分析 windows, null only means the main window is re-reading; requests still validate.
      if (message?.type === "account" && typeof message.value === "string" && message.value !== scope.account ||
          message?.type === "account-cleared" && message.value === accountId || message?.type === "host-gone") browser.suspend();
    });
    const unsubscribe = host?.onHostEvent?.(event => {
      if (event?.theme) theme(event.theme);
      if (event?.type === "focus" || event?.type === "service-restored") void browser.refresh();
    });
    window.addEventListener("storage", event => {
      if (event.key !== "real-ui-settings-1") return;
      try { theme(JSON.parse(event.newValue || "{}").theme); } catch { }
    });
    window.addEventListener("focus", () => { void validateScope().then(valid => { if (!valid) browser.suspend(); }).catch(() => {}); });
    window.addEventListener("beforeunload", () => { unsubscribe?.(); channel?.close(); browser.destroy(); });
  })().catch(() => { document.getElementById("historyRoot").textContent = "聊天记录未能打开，请重新打开窗口。"; });
})();
