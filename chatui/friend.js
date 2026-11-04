"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 知己分析 window shell: header, history status, settings, menu and lifecycle.
// Modified 2026-10-13 (Apache-2.0): plugin architecture. Tabs and the analysis come from the
// friend's current scheme (friend-scheme.js); 关系记忆, goals and the own profile stay here.
(function () {
  const A = window.FriendApp;
  const S = { overview: null, timer: null };
  A.schemeName = (plugin, scheme) => window.FriendScheme.schemeName(plugin, scheme);

  function header() {
    A.byId("faName").textContent = A.scope.name;
    document.title = `${A.scope.name} · 知己分析`;
    const avatar = A.byId("faAvatar");
    avatar.textContent = Array.from(A.scope.name)[0] || "友";
    if (A.scope.avatar) {
      const image = A.el("img");
      image.alt = "";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("load", () => avatar.replaceChildren(image), { once: true });
      image.src = A.scope.avatar;
    }
  }

  // -- the shared history line --------------------------------------------------------------------------
  function schedule(delay) {
    clearTimeout(S.timer);
    if (!A.masked) S.timer = setTimeout(() => { void refresh(); }, delay);
  }
  async function refresh(open = false) {
    try {
      // A restricted window never opens (open may start model work); it only reads the overview.
      const overview = open && !A.restricted ? await A.api("/api/friend/open", { method: "POST", body: {} }) :
        await A.api("/api/friend/overview?" + A.query());
      const previous = S.overview;
      S.overview = overview;
      renderHistory();
      window.FriendMemory.setOverview(overview);
      if (previous && (previous.history.readCount !== overview.history.readCount ||
                       previous.history.state !== overview.history.state))
        window.FriendScheme.historyChanged(overview.history.readCount);
      const busy = overview.history.refreshing || overview.history.state === "reading";
      schedule(busy ? 1200 : 8000);
    } catch {
      if (!A.masked) schedule(4000);
    }
  }
  // Reading progress is part of the analysis line; only a failed read gets its own row.
  function renderHistory() {
    const history = S.overview.history;
    A.setTask("history", history.state === "error" ? { text: "读取聊天历史失败：" + (history.error || "请重试"), state: "error",
      actions: [{ label: "重试", onClick: () => refresh(true) }] } : null);
  }

  // -- settings: contact goal and the account's own profile -------------------------------------------
  const PRESET_HINT = "可选起点：";
  async function openSettings() {
    const body = A.openSheet("联系目标与自己的资料", "settings");
    let data;
    try { data = await A.api("/api/friend/settings?" + A.query()); }
    catch (error) { body.append(A.el("p", "", error.message || "读取失败")); return; }
    if (A.sheetOwner !== "settings") return;
    body.replaceChildren(goalForm(data.goal), profileForm(data.accountProfile));
  }
  function textForm(entry, title, note, fields, version, save) {
    const draft = A.draft(entry);
    const values = { ...Object.fromEntries(fields.map(field => [field.key, field.value])), ...(draft.payload || {}) };
    const block = A.el("div", "fa-section");
    block.append(A.el("h3", "", title));
    if (note) block.append(A.el("p", "fa-note", note));
    const form = A.el("div", "fa-form");
    const unsaved = A.el("span", "fa-unsaved", draft.restored ? "未保存（已恢复上次编辑）" : "");
    for (const field of fields) {
      const input = A.el("textarea");
      input.value = values[field.key] || "";
      input.placeholder = field.placeholder;
      input.setAttribute("aria-label", field.label);
      input.addEventListener("input", () => {
        values[field.key] = input.value;
        unsaved.textContent = "未保存";
        A.updateDraft(draft, { ...values }, version);
      });
      if (field.presets) {
        const chips = A.el("div", "fa-tags");
        chips.append(A.el("span", "fa-note", PRESET_HINT));
        for (const preset of field.presets) chips.append(A.button(preset, "fa-tag", () => {
          input.value = preset;
          input.dispatchEvent(new Event("input"));
        }));
        form.append(chips);
      }
      form.append(input);
    }
    const actions = A.el("div", "fa-form-actions");
    const saveButton = A.button("保存", "fa-primary", async () => {
      saveButton.disabled = true;
      try {
        await save(values, version);
        await A.closeDraft(entry);
        A.toast("已保存");
        void openSettings();
      } catch (error) {
        saveButton.disabled = false;
        A.toast(error.message || "保存失败");
      }
    });
    actions.append(saveButton, A.button("放弃修改", "fa-plain", async () => { await A.closeDraft(entry); void openSettings(); }), unsaved);
    form.append(actions);
    block.append(form);
    return block;
  }
  function goalForm(goal) {
    return textForm("goal", "联系目标", "只属于这位联系人。目标是你希望的方向，不代表对方同意或关系状态。",
      [{ key: "text", label: "联系目标", value: goal.text, placeholder: `例如：${goal.default}`, presets: goal.presets }],
      goal.version, async (values, version) => {
        await A.api("/api/friend/settings", { method: "POST", body: { kind: "goal", text: values.text || "", expectedVersion: version } });
        window.FriendRun.invalidate();
        window.FriendScheme.refresh();
      });
  }
  function profileForm(profile) {
    return textForm("self", "自己的资料", "本账号所有联系人共用，用于让建议更像你；不会写进朋友的记忆。",
      [{ key: "background", label: "自身背景", value: profile.background, placeholder: "例如：在上海做设计，周末常去爬山" },
        { key: "style", label: "表达风格", value: profile.style, placeholder: "例如：简短、直接，少用表情" }],
      profile.version, async (values, version) => {
        await A.api("/api/friend/settings", { method: "POST", body: { kind: "account-profile",
          background: values.background || "", style: values.style || "", expectedVersion: version } });
        A.broadcast("account-profile-changed");
      });
  }

  // -- menu ---------------------------------------------------------------------------------------------
  const menu = A.byId("faMenu"), more = A.byId("faMoreButton");
  function closeMenu() { menu.hidden = true; more.setAttribute("aria-expanded", "false"); }
  more.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
    more.setAttribute("aria-expanded", String(!menu.hidden));
  });
  document.addEventListener("click", event => { if (!event.target.closest(".fa-more")) closeMenu(); });
  menu.addEventListener("click", event => {
    const action = event.target.closest("button")?.dataset.menu;
    if (!action) return;
    closeMenu();
    if (action === "settings") void openSettings();
    else if (action === "rebuild") void window.FriendRun.confirmRebuild();
    else {
      window.FriendScheme.selectTab("memories");
      if (action === "add-memory") window.FriendMemory.startAdd();
      else if (action === "ignored") void window.FriendMemory.showIgnored();
      else if (action === "clear-memory") void window.FriendMemory.clear();
    }
  });

  // -- lifecycle ------------------------------------------------------------------------------------------
  A.on(message => {
    if (message.type === "memory-changed" || message.type === "refresh-overview") {
      void refresh();
      window.FriendRun.invalidate();
      window.FriendScheme.refresh();
    } else if (message.type === "select-tab") window.FriendScheme.selectTab(message.tab === "memory" ? "memories" : message.tab);
    else if (message.type === "analysis-step" && message.key === "archive") void refresh();
    else if (message.type === "analysis-finished") {
      void refresh();
      if (message.state === "done") A.toast("分析完成");
      else if (message.state === "partial") A.toast("部分完成，详情见顶部");
    } else if (message.type === "account-profile-changed" && message.account === A.scope.account &&
             message.from !== A.scope.user && A.sheetOwner === "settings") {
      A.toast("其他窗口已更新自己的资料，保存时会检查版本");
    } else if (message.type === "masked") { clearTimeout(S.timer); window.FriendRun.stop(); }
  });
  window.friendHost?.onBeforeClose(async token => {
    let ok = false;
    try { ok = (await Promise.all([A.flushDrafts(), window.FriendScheme.flush()])).every(Boolean); } catch { ok = false; }
    window.friendHost.closeReady(token, ok);
  });
  window.friendHost?.onHostEvent(event => {
    if (event.type === "focus") {
      // A restricted window only re-reads the overview on focus; it never restarts run polling.
      if (A.restricted) void refresh(true);
      else void refresh(true).then(() => window.FriendRun.refresh());
      void window.FriendScheme.load().catch(() => { });
    } else if (event.type === "host-gone") A.mask("主窗口已关闭或重新载入，请从聊天重新打开知己分析。");
    else if (A.restricted && event.type === "select-tab" && typeof event.tab === "string")
      window.FriendScheme.selectTab(event.tab === "memory" ? "memories" : event.tab);
  });

  A.applyTheme();
  if (!A.validScope) {
    A.mask("窗口参数无效，请从聊天重新打开知己分析。");
    return;
  }
  header();
  if (A.restricted) {
    // Read-only window for an external Agent: no run controls, settings, memory edits or scheme switching.
    A.byId("faRunControl").hidden = true;
    A.byId("faMoreButton").hidden = true;
    A.byId("faScheme").disabled = true;
    void (async () => {
      try { await window.FriendScheme.load(); }
      catch (error) { A.toast(error.message || "分析方案读取失败"); }
      if (A.initialTab) window.FriendScheme.selectTab(A.initialTab === "memory" ? "memories" : A.initialTab);
      await refresh(true);
    })();
    return;
  }
  void (async () => {
    try { await window.FriendScheme.load(); }
    catch (error) { A.toast(error.message || "分析方案读取失败"); }
    await refresh(true);
    try {
      const drafts = await A.loadDrafts();
      if (drafts.size) {
        window.FriendMemory.restoreDrafts(drafts);
        if ([...drafts.keys()].some(entry => entry === "goal" || entry === "self")) void openSettings();
        if ([...drafts.keys()].some(entry => entry.startsWith("observation:"))) window.FriendScheme.selectTab("persona");
      }
    } catch { }
  })();
})();
