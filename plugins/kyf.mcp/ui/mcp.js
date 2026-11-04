"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// MCP 管理 page: pairing, authorization scope, records and the master switch, plus the
// centralized 安全与数据说明 view. All data flows through kyfHost.rpc to this plugin's
// backend; a credential is shown once and never leaves the dialog it was shown in.
(function () {
  // Data scope (which data) and operation capabilities (what it may do) are separate settings.
  const MODE_LABEL = { accounts: "按账号", per_contact: "逐联系人", full: "全部账号（旧）" };
  const CAPABILITY_LABEL = { write: "修改资料与发起分析",
    manage: "全局管理（应用与模型设置，不含联系人资料）" };
  const EXTERNAL_LABEL = "外部聊天导入";
  const REFRESH_MS = 5000;
  const S = { status: null, profiles: [], calls: [], directory: null, grants: null, expanded: null,
    modal: false, rendered: "", serial: 0, timer: null, defaultMode: "accounts", nameById: {},
    pairings: [], pairingAccounts: [] };
  const byId = id => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function button(label, className, onClick) {
    const node = el("button", className, label);
    node.type = "button";
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }
  function pad(value) { return String(value).padStart(2, "0"); }
  function formatTime(ms) {
    if (!Number.isFinite(ms) || ms <= 0) return "—";
    const date = new Date(ms);
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  function formatDuration(ms) {
    if (!Number.isFinite(ms) || ms < 0) return "—";
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toFixed(1)} s`;
  }
  let toastTimer = null;
  function toast(message) {
    const node = byId("mcpToast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
  }
  function fail(error) { toast(error && error.message ? error.message : "操作失败，请重试"); }

  // Sandboxed frames get no window.confirm; a small page dialog asks instead.
  function confirm(message, action = "确定", danger = true) {
    return new Promise(resolve => {
      const overlay = el("div", "mcp-overlay"), card = el("div", "mcp-dialog"), actions = el("div", "mcp-dialog-actions");
      const done = value => { overlay.remove(); resolve(value); };
      const cancel = button("取消", "mcp-secondary", () => done(false));
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.appendChild(el("p", "", message));
      actions.appendChild(cancel);
      actions.appendChild(button(action, danger ? "mcp-secondary mcp-danger-text" : "mcp-primary", () => done(true)));
      card.appendChild(actions);
      overlay.appendChild(card);
      overlay.addEventListener("click", event => { if (event.target === overlay) done(false); });
      document.body.appendChild(overlay);
      cancel.focus();
    });
  }
  function dialog(build) {
    const overlay = el("div", "mcp-overlay"), card = el("div", "mcp-dialog");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    const close = () => { overlay.remove(); S.modal = false; void refresh(); };
    S.modal = true;
    build(card, close);
    overlay.appendChild(card);
    overlay.addEventListener("click", event => { if (event.target === overlay) close(); });
    document.body.appendChild(overlay);
    return close;
  }

  async function rpc(method, params) {
    try { return await kyfHost.rpc(method, params || {}); }
    catch (error) { fail(error); throw error; }
  }

  // -- data ---------------------------------------------------------------------------------
  async function load() {
    const serial = ++S.serial;
    const results = await Promise.allSettled([
      rpc("mcp.status"), rpc("mcp.profiles"), rpc("mcp.calls"),
      ...(S.expanded ? [rpc("mcp.profile", { clientId: S.expanded })] : []),
    ]);
    // Pending CLI pairings are optional: an older Core without the route simply shows none.
    const pairing = await kyfHost.rpc("mcp.pairings", {}).catch(() => null);
    if (serial !== S.serial) return;
    S.pairings = pairing && Array.isArray(pairing.pairings) ? pairing.pairings : [];
    S.pairingAccounts = pairing && Array.isArray(pairing.accounts) ? pairing.accounts : [];
    const [status, profiles, calls, profile] = results.map(item => item.status === "fulfilled" ? item.value : null);
    if (status) S.status = status;
    if (profiles) S.profiles = profiles.profiles || [];
    if (calls) S.calls = calls.calls || [];
    if (profile && profile.profile && profile.profile.clientId === S.expanded) S.grants = profile;
    for (const item of S.profiles) S.nameById[item.clientId] = item.name;
    render();
  }
  const refresh = () => load();
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(() => { if (!S.modal) void load(); schedule(); }, REFRESH_MS);
  }

  // -- admin view ---------------------------------------------------------------------------
  function statusText() {
    const status = S.status;
    if (!status) return "正在读取状态…";
    if (!status.coreReady) return "Core 未就绪";
    if (!status.enabled) return "MCP 已关闭";
    const background = status.guiAttached ? "主窗口运行中 · " : "后台运行中 · ";
    return `Core 就绪 · ${background}${status.enabledProfiles}/${status.profiles} 个客户端启用 · ${status.activeConnections} 个活动连接`;
  }

  function stateKey() {
    return JSON.stringify([S.status, S.profiles, S.calls, S.expanded, S.grants,
      S.pairings.map(item => [item.pairingId, item.ui]),
      S.directory && S.directory.accounts.map(item => [item.accountId, item.label, item.contacts.length])]);
  }

  function render(force) {
    // Polling must not rebuild unchanged DOM: that would drop focus and swallow a click in progress.
    const key = stateKey();
    if (!force && key === S.rendered) return;
    S.rendered = key;
    const status = S.status, enabled = !!(status && status.enabled);
    byId("statusLine").textContent = statusText();
    const master = byId("masterSwitch");
    master.setAttribute("aria-checked", String(enabled));
    byId("masterOffBanner").hidden = enabled || !status;
    byId("btnCreate").disabled = !enabled;
    renderPairings();
    renderProfiles(enabled);
    renderCalls();
  }

  // -- command-line pairing requests (G12): the fallback when no pairing window could open ----
  function renderPairings() {
    const card = byId("pairingCard"), list = byId("pairingList");
    if (!card || !list) return;
    card.hidden = !S.pairings.length;
    list.replaceChildren();
    for (const item of S.pairings) {
      const row = el("div", "mcp-profile");
      const head = el("div", "mcp-profile-head");
      head.appendChild(el("span", "mcp-profile-name", item.clientName));
      head.appendChild(el("span", "mcp-chip", item.clientLabel));
      head.appendChild(el("span", "mcp-spacer"));
      head.appendChild(button("审核", "mcp-primary", () => openPairing(item)));
      row.appendChild(head);
      row.appendChild(el("p", "mcp-profile-meta", `发起于 ${formatTime(item.createdAtMs)} · 有效至 ${formatTime(item.expiresAtMs)}` +
        (item.replaces ? ` · 将修复已有客户端「${item.replaces}」（旧凭据失效）` : "")));
      list.appendChild(row);
    }
  }

  function openPairing(item) {
    dialog((card, close) => {
      card.appendChild(el("h2", "", `允许「${item.clientName}」连接知己？`));
      card.appendChild(el("p", "mcp-note", "选择可读取的账号；额外操作能力默认关闭。"));
      const accountBox = el("div", "mcp-contacts mcp-dialog-accounts");
      const accountChecks = new Map();
      for (const account of S.pairingAccounts) {
        const label = checkLabel(account.label, false);
        label.className = "mcp-contact";
        label.title = account.accountId;
        accountChecks.set(account.accountId, label.children[0]);
        accountBox.appendChild(label);
      }
      card.appendChild(accountBox);
      const capabilityChecks = {};
      const capabilityBox = el("div", "mcp-dialog-capabilities");
      for (const key of Object.keys(CAPABILITY_LABEL)) {
        const label = checkLabel(CAPABILITY_LABEL[key], false);
        capabilityChecks[key] = label.children[0];
        capabilityBox.appendChild(label);
      }
      card.appendChild(capabilityBox);
      const actions = el("div", "mcp-dialog-actions");
      const decide = async approve => {
        const params = { pairingId: item.pairingId, approve };
        if (approve) {
          params.accounts = [...accountChecks].filter(([, check]) => check.checked).map(([id]) => id);
          if (!params.accounts.length) { toast("请至少选择一个账号"); return; }
          params.capabilities = Object.fromEntries(Object.keys(capabilityChecks)
            .map(key => [key, capabilityChecks[key].checked]));
        }
        try { await rpc("mcp.decide_pairing", params); } catch { return; }
        toast(approve ? "已允许，命令行将自动完成接入" : "已拒绝该配对请求");
        close();
      };
      actions.appendChild(button("拒绝", "mcp-secondary", () => void decide(false)));
      actions.appendChild(button("允许连接", "mcp-primary", () => void decide(true)));
      card.appendChild(actions);
    });
  }

  function renderProfiles(enabled) {
    const list = byId("profileList");
    list.replaceChildren();
    if (!S.profiles.length) {
      list.appendChild(el("p", "mcp-empty", "还没有配对的客户端。创建一个档案，把生成的凭据填进对应 AI 客户端的配置。"));
      return;
    }
    for (const profile of S.profiles) list.appendChild(profileCard(profile, enabled));
  }

  function profileCard(profile, enabled) {
    const card = el("div", "mcp-profile");
    const head = el("div", "mcp-profile-head");
    const name = el("span", "mcp-profile-name", profile.name);
    name.title = profile.clientId;
    head.appendChild(name);
    const chip = el("span", "mcp-chip" + (profile.enabled ? "" : " mcp-chip-off"),
      profile.enabled ? MODE_LABEL[profile.mode] : "已停用");
    chip.title = `客户端 ${profile.clientId}`;
    head.appendChild(chip);
    head.appendChild(el("span", "mcp-spacer"));
    const actions = el("div", "mcp-profile-actions");
    if (profile.mode === "per_contact" || profile.mode === "accounts") {
      actions.appendChild(button(profile.mode === "accounts" ? "授权账号" : "授权联系人", "mcp-plain", () => {
        S.expanded = S.expanded === profile.clientId ? null : profile.clientId;
        S.grants = null;
        render(true);
        if (S.expanded) void load(); // fetch this profile's grants for the matrix
      }));
    }
    actions.appendChild(button("重新生成凭据", "mcp-plain", () => void regenerate(profile)));
    actions.appendChild(button("撤销", "mcp-plain mcp-danger-text", () => void revoke(profile)));
    head.appendChild(actions);
    const toggle = el("button", "mcp-switch");
    toggle.type = "button";
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", String(!!profile.enabled));
    toggle.setAttribute("aria-label", `${profile.enabled ? "停用" : "启用"}${profile.name}`);
    toggle.addEventListener("click", () => void toggleProfile(profile));
    head.appendChild(toggle);
    card.appendChild(head);

    card.appendChild(el("p", "mcp-profile-meta",
      `创建于 ${formatTime(profile.createdAt)} · 最近使用 ${formatTime(profile.lastUsedAt)}` +
      (profile.mode === "per_contact" ? ` · 已授权联系人 ${profile.contactCount}` : "") +
      (profile.mode === "accounts" ? ` · 已授权账号 ${(profile.accountGrants || []).length}` : "")));

    const body = el("div", "mcp-profile-body");
    const modeLine = el("div", "mcp-fieldline");
    modeLine.appendChild(el("span", "", "数据范围"));
    const mode = modeSelect(profile.mode);
    mode.addEventListener("change", () => void setMode(profile, mode.value, mode));
    modeLine.appendChild(mode);
    body.appendChild(modeLine);
    const capabilities = profileCapabilities(profile);
    const line = el("div", "mcp-fieldline mcp-capabilities");
    for (const key of Object.keys(CAPABILITY_LABEL)) {
      line.appendChild(checkLabel(CAPABILITY_LABEL[key], capabilities[key],
        check => void setCapability(profile, key, check)));
    }
    body.appendChild(line);
    if (S.expanded === profile.clientId) {
      body.appendChild(profile.mode === "accounts" ? accountList(profile) : matrix(profile));
    }
    card.appendChild(body);
    return card;
  }

  function modeSelect(value) {
    const select = el("select");
    for (const [key, label] of Object.entries(MODE_LABEL)) {
      const option = el("option", "", label);
      option.value = key;
      select.appendChild(option);
    }
    select.value = MODE_LABEL[value] ? value : "accounts";
    select.setAttribute("aria-label", "数据范围");
    return select;
  }
  function checkLabel(text, checked, onChange) {
    const label = el("label", "mcp-check");
    const check = el("input");
    check.type = "checkbox";
    check.checked = !!checked;
    if (onChange) check.addEventListener("change", () => onChange(check));
    label.appendChild(check);
    label.appendChild(el("span", "", text));
    return label;
  }
  function profileCapabilities(profile) {
    const capabilities = profile.capabilities || {};
    return { write: !!capabilities.write,
      manage: !!(capabilities.manage || profile.globalManagement) };
  }

  async function toggleProfile(profile) {
    if (!await confirm(`${profile.enabled ? "停用" : "启用"}「${profile.name}」？` +
        (profile.enabled ? "该客户端的调用与进行中的操作立即失效。" : ""), profile.enabled ? "停用" : "启用",
      profile.enabled)) return;
    try { await rpc("mcp.set_enabled", { clientId: profile.clientId, enabled: !profile.enabled }); }
    catch { return; }
    await load();
  }
  async function setCapability(profile, key, check) {
    // Any capability change re-pairs the client: its current connection stops being valid.
    check.disabled = true;
    try { await rpc("mcp.set_capabilities", { clientId: profile.clientId, [key]: check.checked }); }
    catch { check.checked = !check.checked; check.disabled = false; return; }
    check.disabled = false;
    await load();
  }
  async function setMode(profile, mode, select) {
    if (mode === profile.mode) return;
    if (!await confirm(`切换「${profile.name}」的数据范围？切换后授权版本提升，现有游标与进行中的操作失效。`, "切换")) {
      if (select) select.value = profile.mode;
      return;
    }
    try { await rpc("mcp.set_mode", { clientId: profile.clientId, mode }); }
    catch { if (select) select.value = profile.mode; return; }
    await load();
  }
  async function regenerate(profile) {
    if (!await confirm(`重新生成「${profile.name}」的凭据？旧凭据立即失效，客户端需用新凭据重新配对。`, "重新生成")) return;
    let result;
    try { result = await rpc("mcp.credential", { clientId: profile.clientId }); }
    catch { return; }
    showCredential(result.profile.name, result.profile.clientId, result.credential);
  }
  async function revoke(profile) {
    if (!await confirm(`撤销「${profile.name}」？其凭据与全部联系人授权立即删除，正在进行的调用立即失效。此操作不能撤销。`, "撤销")) return;
    try { await rpc("mcp.revoke", { clientId: profile.clientId }); }
    catch { return; }
    if (S.expanded === profile.clientId) { S.expanded = null; S.grants = null; }
    toast(`已撤销 ${profile.name}`);
    await load();
  }

  // -- contact matrix -------------------------------------------------------------------------
  function matrix(profile) {
    const box = el("div", "mcp-matrix");
    if (!S.directory) {
      box.appendChild(el("p", "mcp-empty", "正在读取账号目录…"));
      void loadDirectory();
      return box;
    }
    const bar = el("div", "mcp-matrix-bar");
    const search = el("input");
    search.type = "text";
    search.placeholder = "搜索联系人";
    search.setAttribute("aria-label", "搜索联系人");
    bar.appendChild(search);
    box.appendChild(bar);
    const accounts = S.directory.accounts || [];
    if (!accounts.length) { box.appendChild(el("p", "mcp-empty", "还没有已保存账号的联系人目录。")); return box; }
    const granted = new Set((S.grants && S.grants.contacts || [])
      .filter(item => item.sourceFingerprint === S.directory.sourceFingerprint)
      .map(item => `${item.accountId}\u0000${item.subjectType}\u0000${item.subjectId}`));
    let grid = null;
    for (const account of accounts) {
      const section = el("div", "mcp-account");
      const title = el("div", "mcp-account-title");
      title.appendChild(el("span", "", `${account.label}（${account.contacts.length} 个联系人${account.truncated ? "，已截断" : ""}）`));
      title.appendChild(el("span", "mcp-spacer"));
      title.appendChild(el("span", "", account.accountId.slice(0, 12)));
      section.appendChild(title);
      grid = el("div", "mcp-contacts");
      for (const contact of account.contacts) {
        const key = `${account.accountId}\u0000wechat_contact\u0000${contact.subjectId}`;
        const label = el("label", "mcp-contact");
        const check = el("input");
        check.type = "checkbox";
        check.checked = granted.has(key);
        check.addEventListener("change", () =>
          void toggleContact(profile, account.accountId, contact.subjectId, check.checked, check));
        label.appendChild(check);
        label.appendChild(el("span", "", contact.isGroup ? `${contact.name}（群聊）` : contact.name));
        label.title = contact.subjectId;
        grid.appendChild(label);
      }
      section.appendChild(grid);
      box.appendChild(section);
    }
    // Search filters in place: keystrokes never rebuild the checkboxes under the pointer.
    search.addEventListener("input", () => {
      const query = search.value.trim().toLowerCase();
      if (!grid) return;
      for (const label of grid.children) {
        const text = `${label.textContent || ""}\u0000${label.title || ""}`.toLowerCase();
        label.hidden = !!query && !text.includes(query);
      }
    });
    return box;
  }
  // -- account grants (按账号) ------------------------------------------------------------------
  // The directory's saved accounts plus the external-import library as one pseudo-account.
  function directoryAccounts() {
    const accounts = ((S.directory && S.directory.accounts) || [])
      .map(account => ({ accountId: account.accountId, label: account.label }));
    if (S.directory && S.directory.externalAccountId) {
      accounts.push({ accountId: S.directory.externalAccountId, label: EXTERNAL_LABEL });
    }
    return accounts;
  }
  function accountList(profile) {
    const box = el("div", "mcp-matrix");
    if (!S.directory) {
      box.appendChild(el("p", "mcp-empty", "正在读取账号目录…"));
      void loadDirectory();
      return box;
    }
    const accounts = directoryAccounts();
    if (!accounts.length) { box.appendChild(el("p", "mcp-empty", "还没有已保存账号。")); return box; }
    const granted = new Set(((S.grants && S.grants.accountGrants) || [])
      .filter(item => item.sourceFingerprint === S.directory.sourceFingerprint)
      .map(item => item.accountId));
    const grid = el("div", "mcp-contacts");
    for (const account of accounts) {
      const label = checkLabel(account.label, granted.has(account.accountId),
        check => void toggleAccount(profile, account.accountId, check));
      label.className = "mcp-contact";
      label.title = account.accountId;
      grid.appendChild(label);
    }
    box.appendChild(grid);
    box.appendChild(el("p", "mcp-note mcp-matrix-note", "授权账号下的全部联系人；之后新增的账号不会自动授权。"));
    return box;
  }
  async function toggleAccount(profile, accountId, check) {
    const fingerprint = S.directory && S.directory.sourceFingerprint;
    const granted = check.checked;
    if (!fingerprint) { check.checked = !granted; return; }
    check.disabled = true;
    try {
      const result = await rpc(granted ? "mcp.grant_account" : "mcp.revoke_account",
        { clientId: profile.clientId, sourceFingerprint: fingerprint, accountId });
      S.grants = { ...(S.grants || {}), accountGrants: result.accountGrants, sourceFingerprint: fingerprint };
    } catch { check.checked = !granted; }
    check.disabled = false;
    render();
  }

  async function loadDirectory() {
    try { S.directory = await rpc("mcp.directory"); }
    catch { S.directory = { accounts: [] }; toast("账号目录暂不可用，无法编辑联系人授权"); }
    render();
  }
  async function toggleContact(profile, accountId, subjectId, granted, check) {
    const fingerprint = S.directory && S.directory.sourceFingerprint;
    if (!fingerprint) { check.checked = !granted; return; }
    check.disabled = true;
    try {
      const result = await rpc(granted ? "mcp.grant_contact" : "mcp.revoke_contact",
        { clientId: profile.clientId, sourceFingerprint: fingerprint, accountId,
          subjectType: "wechat_contact", subjectId });
      S.grants = { ...(S.grants || {}), contacts: result.contacts, sourceFingerprint: fingerprint };
    } catch { check.checked = !granted; }
    check.disabled = false;
    render();
  }

  // -- calls -----------------------------------------------------------------------------------
  function renderCalls() {
    const table = byId("callsTable"), empty = byId("callsEmpty"), body = byId("callsBody");
    table.hidden = !S.calls.length;
    empty.hidden = !!S.calls.length;
    body.replaceChildren();
    for (const call of S.calls.slice(0, 50)) {
      const row = el("tr");
      for (const [className, text] of [
        ["", formatTime(call.time)], ["mcp-wrap", S.nameById[call.clientId] || call.clientId],
        ["mcp-wrap", call.action], ["", call.accountId || "—"], ["", ""], ["", formatDuration(call.durationMs)]]) {
        row.appendChild(el("td", className, text));
      }
      const code = row.children[4];
      code.textContent = call.resultCode === "ok" ? "成功" : call.resultCode;
      code.className = "mcp-code" + (call.resultCode === "ok" ? "" : " mcp-code-error");
      body.appendChild(row);
    }
  }

  // -- pairing and the one-time credential -------------------------------------------------------
  function openCreate() {
    dialog((card, close) => {
      card.appendChild(el("h2", "", "配对新客户端"));
      card.appendChild(el("p", "mcp-note", "凭据只在创建后展示一次；配对前可先阅读安全与数据说明。"));
      const form = el("form");
      const nameLabel = el("label", "", "客户端名称");
      const name = el("input");
      name.type = "text";
      name.maxLength = 64;
      name.required = true;
      name.setAttribute("aria-label", "客户端名称");
      nameLabel.appendChild(name);
      form.appendChild(nameLabel);
      const modeLabel = el("label", "", "数据范围");
      const mode = modeSelect(S.defaultMode);
      modeLabel.appendChild(mode);
      form.appendChild(modeLabel);
      // 按账号: pick the accounts here; 逐联系人 is granted per contact after creation.
      const accountBox = el("div", "mcp-contacts mcp-dialog-accounts");
      const accountChecks = new Map();
      const fillAccounts = () => {
        accountBox.replaceChildren();
        accountChecks.clear();
        const accounts = directoryAccounts();
        if (!accounts.length) accountBox.appendChild(el("p", "mcp-note", S.directory ? "还没有已保存账号。" : "正在读取账号目录…"));
        for (const account of accounts) {
          const label = checkLabel(account.label, false);
          label.className = "mcp-contact";
          label.title = account.accountId;
          accountChecks.set(account.accountId, label.children[0]);
          accountBox.appendChild(label);
        }
      };
      fillAccounts();
      if (!S.directory) void loadDirectory().then(fillAccounts);
      accountBox.hidden = mode.value !== "accounts";
      mode.addEventListener("change", () => { accountBox.hidden = mode.value !== "accounts"; });
      form.appendChild(accountBox);
      const capabilityChecks = {};
      const capabilityBox = el("div", "mcp-dialog-capabilities");
      capabilityBox.appendChild(el("p", "mcp-note", "默认只读；需要时再开启："));
      for (const key of Object.keys(CAPABILITY_LABEL)) {
        const label = checkLabel(CAPABILITY_LABEL[key], false);
        capabilityChecks[key] = label.children[0];
        capabilityBox.appendChild(label);
      }
      form.appendChild(capabilityBox);
      const notice = el("p", "mcp-note mcp-dialog-notice", "");
      notice.hidden = true;
      form.appendChild(notice);
      const actions = el("div", "mcp-dialog-actions");
      actions.appendChild(button("取消", "mcp-secondary", close));
      const create = button("创建并生成凭据", "mcp-primary", async () => {
        if (!name.value.trim() || create.disabled) return;
        create.disabled = true;
        notice.hidden = true;
        // Account grants bind to the data source; without its fingerprint they cannot be
        // made, so never create the client silently without them: retry once, else say so.
        const wanted = mode.value === "accounts" ? [...accountChecks].filter(([, check]) => check.checked) : [];
        if (wanted.length && !(S.directory && S.directory.sourceFingerprint)) {
          try { S.directory = await kyfHost.rpc("mcp.directory", {}); } catch { /* reported below */ }
          if (!(S.directory && S.directory.sourceFingerprint)) {
            notice.textContent = "暂时读不到数据来源，所选账号无法授权；请稍后重试，或不选账号先创建。";
            notice.hidden = false;
            create.disabled = false;
            return;
          }
        }
        let result;
        try { result = await rpc("mcp.create_profile", { name: name.value.trim(), mode: mode.value,
          allowWrite: capabilityChecks.write.checked,
          globalManagement: capabilityChecks.manage.checked }); }
        catch { create.disabled = false; return; }
        const fingerprint = S.directory && S.directory.sourceFingerprint;
        if (wanted.length && fingerprint) {
          let failed = 0;
          for (const [accountId] of wanted) {
            try { await rpc("mcp.grant_account", { clientId: result.profile.clientId, sourceFingerprint: fingerprint, accountId }); }
            catch { failed += 1; }
          }
          if (failed) toast(`有 ${failed} 个账号未能授权，请在客户端卡片的“授权账号”中重试`);
        }
        close();
        showCredential(result.profile.name, result.profile.clientId, result.credential);
      });
      actions.appendChild(create);
      form.appendChild(actions);
      form.addEventListener("submit", event => { event.preventDefault(); create.click(); });
      card.appendChild(form);
      name.focus();
    });
  }

  function showCredential(name, clientId, credential) {
    dialog((card, close) => {
      card.appendChild(el("h2", "", `「${name}」的凭据`));
      card.appendChild(el("p", "mcp-note", "请立即保存到对应 AI 客户端的配置。关闭后不再显示，只能重新生成。"));
      const line = el("div", "mcp-secret-line");
      line.appendChild(el("code", "mcp-secret", credential));
      line.appendChild(button("复制", "mcp-secondary", async () => {
        try {
          const result = await kyfHost.host("clipboard.copy", { text: credential });
          toast(result && result.copied ? "已复制" : "复制未完成，请手动选择复制");
        } catch { toast("复制未完成，请手动选择复制"); }
      }));
      card.appendChild(line);
      card.appendChild(el("p", "mcp-note", `客户端标识：${clientId}`));
      // The pairing flow's single entry to the centralized safety page.
      card.appendChild(button("安全与数据说明", "mcp-plain", () => { close(); showSafety(); }));
      const actions = el("div", "mcp-dialog-actions");
      actions.appendChild(button("我已保存，不再显示", "mcp-primary", close));
      card.appendChild(actions);
    });
  }

  async function toggleMaster() {
    const enabled = !!(S.status && S.status.enabled);
    if (enabled) {
      if (!await confirm("关闭 MCP 总开关？所有客户端连接立即断开，且在重新打开前无法连接。", "关闭")) return;
    } else if (!await confirm("打开 MCP 总开关？", "打开", false)) return;
    try { await rpc("mcp.set_master", { enabled: !enabled }); }
    catch { return; }
    await load();
  }
  async function disconnectAll() {
    if (!await confirm("立即断开所有 MCP 连接？客户端可用其凭据重新连接。", "断开")) return;
    try {
      const result = await rpc("mcp.disconnect_all");
      toast(`已断开 ${result.disconnected} 个连接`);
    } catch { return; }
    await load();
  }

  // -- safety view (the centralized explanations) -------------------------------------------------
  function showSafety() {
    byId("viewAdmin").hidden = true;
    byId("viewSafety").hidden = false;
  }
  function showAdmin() {
    byId("viewSafety").hidden = true;
    byId("viewAdmin").hidden = false;
  }

  // -- init --------------------------------------------------------------------------------------
  async function start() {
    document.body.classList.toggle("theme-light", (kyfHost.context() || {}).theme === "light");
    kyfHost.on(message => {
      if (message.type === "context") document.body.classList.toggle("theme-light", message.context.theme === "light");
    });
    try { S.defaultMode = (await rpc("mcp.settings")).defaultMode; } catch { }
    byId("btnCreate").addEventListener("click", openCreate);
    byId("masterSwitch").addEventListener("click", () => void toggleMaster());
    byId("btnDisconnect").addEventListener("click", () => void disconnectAll());
    byId("btnRefresh").addEventListener("click", () => { if (!S.directory) void loadDirectory(); void refresh(); });
    byId("btnSafety").addEventListener("click", showSafety);
    byId("btnSafetyBack").addEventListener("click", showAdmin);
    await refresh();
    schedule();
  }
  kyfHost.ready.then(() => { void start(); });
})();
