"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 标签: what kind of person ta is. Two panes copied from the 自我剖析 标签 tab (kyf.self-analysis/ui/page.js
// renderTags / renderTagDetail / evidenceCard / loadEvidence, 2026-11-07): chips by category on the left with
// 说话习惯 first, the selected chip's sources on the right. The user's edits are kept by the plugin; every write is
// version-checked. Ctrl+Z undoes the user's own edits (page.tagUndo); the Agent's have their own undo line.
(function () {
  const A = window.FriendApp;
  const HABITS = "说话习惯";
  const LEVELS = ["confirmed", "strong", "possible"];
  const RELATIONS = { mention: "仅提及", occasional: "偶尔参与", interest: "兴趣", trait: "特征", uncertain: "待判断" };
  const CONFIDENCE = { confirmed: "已确认", strong: "较可靠", possible: "可能" };
  const T = { data: null, request: 0, busy: false, error: "", notice: "", deleted: null, agentDismissed: null, selected: "",
    evidenceSerial: 0, evidenceFor: null, ui: null };
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const string = (value, limit = 2000) => typeof value === "string" ? value.slice(0, limit) : "";
  const lines = value => Array.isArray(value) ? value.filter(line => typeof line === "string").join("\n") : string(value);
  const valid = value => object(value) && Array.isArray(value.tags) && Array.isArray(value.habits) && Number.isSafeInteger(value.version);
  const relation = tag => tag.manual === true ? "手动添加" : RELATIONS[tag.relation] || RELATIONS.uncertain;
  const level = tag => LEVELS.includes(tag.level) ? tag.level : "possible";
  const day = ms => new Date(ms).toLocaleDateString("zh-CN");
  const friendName = () => string(A.scope.name, 40) || "ta";
  const tags = () => (T.data?.tags || []).filter(tag => object(tag) && typeof tag.name === "string" && typeof tag.key === "string");
  const habits = () => (T.data?.habits || []).filter(item => object(item) && typeof item.key === "string" && item.hidden !== true);

  function selected() {
    const [kind, ...rest] = T.selected.split(":"), key = rest.join(":");
    if (kind === "habit") { const item = habits().find(habit => habit.key === key); return item ? { kind, item } : null; }
    const item = kind === "tag" ? tags().find(tag => tag.key === key) : null;
    return item ? { kind, item } : null;
  }
  function select(id) { T.selected = id; T.notice = ""; T.deleted = null; render(); void loadEvidence(); }
  function agentUndo() {
    const side = object(T.data?.undo?.agent) ? T.data.undo.agent : {};
    return { total: count(side.count) ?? 0, items: (Array.isArray(side.items) ? side.items : []).filter(object) };
  }
  const agentKey = () => { const top = agentUndo().items[0]; return top ? String(top.seq ?? top.updatedMs ?? "") : ""; };

  // The page skeleton mirrors #panel-tags of 自我剖析; it is built once and updated by render().
  function build() {
    const root = A.byId("pageTags");
    if (T.ui && T.ui.root === root && root.children.length) return T.ui;
    const ui = { root };
    const left = A.el("section", "fa-pane"), right = A.el("section", "fa-pane");
    const head = A.el("header", "fa-pane-head");
    ui.add = A.button("添加", "fa-plain", () => {
      ui.form.hidden = !ui.form.hidden;
      if (!ui.form.hidden) ui.name.focus();
    });
    head.append(A.el("h2", "", "标签"), ui.add);
    ui.form = A.el("div", "fa-tg-form");
    ui.form.hidden = true;
    ui.name = A.el("input");
    ui.name.type = "text";
    ui.name.maxLength = 20;
    ui.name.placeholder = "标签名称";
    ui.name.setAttribute("aria-label", "标签名称");
    ui.name.addEventListener("keydown", event => { if (event.key === "Enter" && !event.isComposing) void save(); });
    ui.category = A.el("select");
    ui.category.setAttribute("aria-label", "类别");
    ui.save = A.button("添加", "fa-primary", () => { void save(); });
    ui.form.append(ui.name, ui.category, ui.save, A.button("取消", "fa-secondary", () => { ui.form.hidden = true; ui.name.value = ""; }));
    ui.status = A.el("div", "fa-pane-status");
    ui.message = A.el("p", "fa-note");
    ui.message.setAttribute("role", "status");
    ui.status.append(ui.message);
    ui.agent = A.el("div", "fa-note fa-tg-agent");
    ui.agentText = A.el("span");
    ui.agentUndo = A.button("撤销", "fa-link", () => { void undo(true); });
    ui.agentClose = A.button("×", "fa-plain", () => { T.agentDismissed = agentKey(); render(); }, { "aria-label": "收起" });
    ui.agent.append(ui.agentText, ui.agentUndo, ui.agentClose);
    ui.guide = A.el("p", "fa-note fa-tg-guide", "“仅提及”“偶尔参与”不代表兴趣，消息数量仅作统计。");
    ui.list = A.el("div", "fa-tg-list");
    ui.empty = A.el("p", "fa-pane-empty");
    left.append(head, ui.form, ui.status, ui.agent, ui.guide, ui.list, ui.empty);

    const detailHead = A.el("header", "fa-pane-head");
    ui.title = A.el("h2", "", "依据");
    ui.actions = A.el("span", "fa-head-actions");
    ui.confirm = A.button("确认", "fa-plain", () => {
      const current = selected();
      if (current?.kind === "tag") void edit({ action: current.item.level === "confirmed" ? "reset" : "confirm", name: current.item.name });
    });
    ui.remove = A.button("删除", "fa-plain fa-danger-text", () => { void remove(); });
    ui.actions.append(ui.confirm, ui.remove);
    detailHead.append(ui.title, ui.actions);
    ui.stats = A.el("p", "fa-note fa-pane-line");
    ui.memories = A.el("div", "fa-pane-line fa-tg-memories");
    ui.undoRow = A.el("div", "fa-note fa-pane-line fa-tg-undo");
    ui.undoText = A.el("span");
    ui.undoButton = A.button("撤销", "fa-link", () => { void undo(false); });
    ui.undoRow.append(ui.undoText, ui.undoButton);
    ui.evidence = A.el("div", "fa-tg-samples");
    ui.detailEmpty = A.el("p", "fa-pane-empty", "选择一个标签，查看它来自哪些消息");
    right.append(detailHead, ui.stats, ui.memories, ui.undoRow, ui.evidence, ui.detailEmpty);
    root.replaceChildren(left, right);
    T.ui = ui;
    return ui;
  }

  async function load() {
    if (A.masked) return;
    build();
    const request = ++T.request;
    try {
      const data = await A.rpc("page.tags");
      if (request !== T.request) return;
      if (!valid(data)) throw new Error("标签暂时无法读取，请稍后重试。");
      T.data = data;
      T.error = "";
    } catch (error) {
      if (request === T.request) T.error = error.message || "标签读取失败";
    }
    if (request !== T.request || A.masked) return;
    render();
    if (T.selected && T.evidenceFor !== T.selected) void loadEvidence();
  }
  // Every write carries the version it was made on and answers with the whole page, so nothing is merged here.
  async function write(method, params) {
    if (!T.data || T.busy || A.restricted || A.masked) return null;
    T.busy = true;
    T.error = "";
    T.request++;
    render();
    let conflict = false;
    try {
      const result = await A.rpc(method, { ...params, expectedVersion: T.data.version });
      const value = valid(result) ? result : await A.rpc("page.tags");
      if (!valid(value)) throw new Error("标签暂时无法读取，请稍后重试。");
      T.data = value;
      return object(result) ? result : {};
    } catch (error) {
      conflict = error?.code === "conflict";
      T.error = error.message || "操作失败";
      return null;
    } finally {
      T.busy = false;
      if (!A.masked) render();
      // The Agent edits tags too: on a version mismatch read the latest page; the user just does it again.
      if (conflict && !A.masked) void load().then(() => { T.error = "标签刚被修改过，已刷新，请重试。"; render(); });
    }
  }
  const edit = async params => !!await write("page.tagEdit", params);
  function undoText(result, agent) {
    if (result.undone) return `${agent ? "已撤销 Agent 的修改" : "已撤销"}：${string(result.undone, 80)}`;
    if (result.skipped) return string(result.skipped.reason, 200) || "这处之后又被改过，没有撤销";
    return agent ? "Agent 没有可撤销的修改" : "没有可撤销的修改";
  }
  // Ctrl+Z and the 撤销 after a deletion undo the user's own last edit; the Agent's line undoes the Agent's.
  async function undo(agent) {
    const deleted = T.deleted;
    const result = await write(agent ? "page.tagUndoAgent" : "page.tagUndo", {});
    if (!result) return;
    T.deleted = null;
    T.notice = undoText(result, agent);
    if (!agent && result.undone && deleted) {
      const found = deleted.startsWith("habit:") ? habits().some(item => "habit:" + item.key === deleted) :
        tags().some(item => "tag:" + item.key === deleted);
      if (found) { T.selected = deleted; void loadEvidence(); }
    }
    render();
  }
  async function save() {
    const ui = T.ui, name = ui.name.value.trim().replace(/^[#＃]+/, "").trim(), category = ui.category.value;
    if (!name) { ui.name.focus(); return; }
    if (!await edit({ action: "add", name, category })) return;
    ui.name.value = "";
    ui.form.hidden = true;
    const habit = category === HABITS ? habits().find(item => item.manual === true && item.label === name) : null;
    const tag = habit ? null : tags().find(item => item.name === name);
    select(habit ? "habit:" + habit.key : tag ? "tag:" + tag.key : "");
  }
  // A speaking habit is hidden (the user's own too), a content tag deleted; 撤销 / Ctrl+Z brings it back.
  async function remove() {
    const current = selected();
    if (!current) return;
    const { kind, item } = current, id = T.selected;
    const label = kind === "habit" ? string(item.label, 40) : item.name;
    if (!await edit(kind === "habit" ? { action: "hide", key: item.key } : { action: "delete", name: item.name })) return;
    T.selected = "";
    T.deleted = id;
    T.notice = `已删除“${label}”，按 Ctrl+Z 撤销`;
    render();
  }

  function chip(id, label, className, title, ...extra) {
    const node = A.button(label, "fa-tg-chip " + className, () => select(id), { title, "aria-pressed": String(id === T.selected) });
    for (const [text, kind] of extra) if (text) node.append(A.el("span", kind, text));
    return node;
  }
  function group(title, chips) {
    const section = A.el("section", "fa-tg-group"), box = A.el("div", "fa-tg-chips");
    box.append(...chips);
    section.append(A.el("h3", "", title), box);
    return section;
  }
  function categories() {
    const names = (T.data?.categories || []).filter(name => typeof name === "string");
    return names.length ? names : [HABITS, "其他"];
  }
  function render() {
    const ui = T.ui;
    if (!ui || A.masked) return;
    // ``run`` is the 标签分析 step of the latest run (page.tags); the window's run line shows live progress,
    // this page is read again when the step finishes.
    const data = T.data, run = object(data?.run) ? data.run : {}, writable = !A.restricted;
    const running = run.state === "running" || run.state === "pending" && ["queued", "running"].includes(run.runState);
    let text = T.error;
    if (!text && running) text = run.state === "running" ? "正在分析标签，完成后自动显示" : "等待分析标签…";
    else if (!text && run.state === "stopped") text = "标签分析已停止";
    else if (!text && run.state === "failed") text = string(run.error, 200) || "标签分析未完成，请重试";
    ui.message.textContent = text;
    ui.message.hidden = !text;
    ui.status.hidden = !text;
    const agent = agentUndo();
    ui.agent.hidden = !writable || !data || !agent.total || agentKey() === T.agentDismissed;
    if (!ui.agent.hidden) {
      ui.agentText.textContent = `Agent 改了 ${agent.total} 处 ·`;
      ui.agent.title = agent.items.slice(0, 5).map(item => `${string(item.label, 80)}${item.agent ? " · " + string(item.agent, 40) : ""}`).join("\n");
      ui.agentUndo.disabled = T.busy;
    }

    ui.add.hidden = !writable;
    ui.add.disabled = T.busy || !data;
    ui.save.disabled = T.busy;
    if (!writable) ui.form.hidden = true;
    const options = categories();
    if (ui.category.children.length !== options.length) {
      const chosen = ui.category.value;
      ui.category.replaceChildren(...options.map(name => { const option = A.el("option", "", name); option.value = name; return option; }));
      ui.category.value = options.includes(chosen) ? chosen : options.includes("其他") ? "其他" : options[0];
    }

    const shownHabits = habits(), shownTags = tags(), sections = [];
    if (shownHabits.length) sections.push(group(HABITS, shownHabits.map(item => chip("habit:" + item.key, string(item.label, 40),
      item.manual ? "confirmed" : "strong", item.manual ? `${HABITS} · 我的补充` : `${HABITS} · 统计`,
      [item.manual ? "我的补充" : string(item.detail, 20), "fa-tg-count"]))));
    const byCategory = new Map(options.filter(name => name !== HABITS).map(name => [name, []]));
    for (const tag of shownTags) {
      const name = string(tag.category, 20) || "其他";
      if (!byCategory.has(name)) byCategory.set(name, []);
      byCategory.get(name).push(tag);
    }
    for (const [name, items] of byCategory) {
      if (!items.length) continue;
      sections.push(group(name, items.map(tag => chip("tag:" + tag.key, string(tag.name, 20), level(tag),
        `${relation(tag)} · ${CONFIDENCE[level(tag)]}`, [relation(tag), "fa-tg-relation"],
        [count(tag.mentions) ? `${tag.mentions} 条` : "", "fa-tg-count"]))));
    }
    ui.list.replaceChildren(...sections);
    const sparse = !!data && !data.habits.length && !shownTags.length && !running;
    ui.guide.hidden = !data || sparse;
    ui.empty.hidden = !data || running || shownTags.length > 0;
    ui.empty.textContent = sparse ? "聊天太少，暂时看不出" : run.analyzed === true ? (writable ? "没有找到标签，可以手动添加" : "没有找到标签") :
      "开始分析后，从 ta 的聊天里整理 ta 的兴趣、习惯和偏好";
    renderDetail();
  }
  function stats(current) {
    const { kind, item } = current;
    if (kind === "habit") return `${HABITS} · ${item.manual ? "我的补充" : "统计"}`;
    const parts = [string(item.category, 20) || "其他"], confidence = CONFIDENCE[level(item)];
    if (item.manual) parts.push(confidence);
    else {
      parts.push(relation(item), confidence, `关联消息 ${count(item.mentions) ?? 0} 条`);
      if (Number.isFinite(item.firstMs) && Number.isFinite(item.lastMs))
        parts.push(day(item.firstMs) === day(item.lastMs) ? day(item.lastMs) : `${day(item.firstMs)}–${day(item.lastMs)}`);
    }
    return parts.join(" · ");
  }
  function renderDetail() {
    const ui = T.ui, current = selected(), writable = !A.restricted;
    if (T.selected && !current) T.selected = "";
    ui.title.textContent = current ? (current.kind === "habit" ? string(current.item.label, 40) : string(current.item.name, 20)) : "依据";
    ui.actions.hidden = !current || !writable;
    ui.confirm.hidden = !current || current.kind === "habit" || current.item.manual === true;
    ui.confirm.textContent = current?.item.level === "confirmed" ? "取消确认" : "确认";
    ui.confirm.disabled = ui.remove.disabled = ui.undoButton.disabled = T.busy;
    ui.stats.textContent = current ? stats(current) : "";
    ui.stats.hidden = !current;
    // 3.4: a confirmed 关系记忆 sharing a source message is shown under the tag, never as a tag of its own.
    const linked = current?.kind === "tag" && Array.isArray(current.item.memories) ? current.item.memories.filter(object) : [];
    ui.memories.replaceChildren(...linked.map(memory => {
      const row = A.el("div", "fa-tg-memory");
      row.append(A.button("也记在关系记忆里：" + string(memory.text, 300), "fa-tg-memory-text", () => A.selectTab("memories")));
      if (writable) {
        const unlink = A.button("取消关联", "fa-link", () => {
          void edit({ action: "unlink", name: current.item.name, memoryId: memory.id });
        });
        unlink.disabled = T.busy;
        row.append(unlink);
      }
      return row;
    }));
    ui.memories.hidden = !linked.length;
    ui.undoText.textContent = T.notice;
    ui.undoButton.hidden = !T.deleted;
    ui.undoRow.hidden = !T.notice || !writable;
    ui.detailEmpty.hidden = !!current || (!!T.notice && writable);
    if (!current) { T.evidenceSerial++; T.evidenceFor = null; ui.evidence.replaceChildren(); }
  }

  function card(item, habit) {
    const node = A.el("article", "fa-tg-sample"), head = A.el("div", "fa-tg-sample-head");
    const date = Number.isFinite(item.timeMs) ? day(item.timeMs) : "";
    head.append(A.el("p", "fa-tg-meta", habit ? date || "时间未知" : `${date ? date + " · " : ""}${relation(item)}`));
    const id = item.status === "missing" ? "" : string(item.msgId, 200);
    if (id) {
      const more = A.button("看上下文", "fa-plain", async () => {
        more.disabled = true;
        try { node.append(A.contextList(await A.rpc("page.message", { id }))); more.remove(); }
        catch (error) { more.disabled = false; A.toast(error.message || "读取失败"); }
      });
      head.append(more);
    }
    node.append(head);
    const before = lines(item.context);
    if (before) node.append(A.el("p", "fa-tg-text", before));
    node.append(A.el("p", "fa-tg-reply", `${friendName()}：${item.status === "missing" ? "（原消息已不在本机）" : string(item.text, 1200)}`));
    const after = lines(item.followup);
    if (after) node.append(A.el("p", "fa-tg-text", "后续：\n" + after));
    if (string(item.quote)) node.append(A.el("p", "fa-tg-note", "支持原话：" + string(item.quote, 1200)));
    if (string(item.reason)) node.append(A.el("p", "fa-tg-note", "判断理由：" + string(item.reason, 1000)));
    if (item.status === "changed") node.append(A.el("p", "fa-tg-note", "原消息已变化，这条依据可能过时。"));
    if (item.truncated === true) node.append(A.el("p", "fa-tg-note", "原文过长，本次仅分析部分文字，语义待判断。"));
    return node;
  }
  async function loadEvidence() {
    const ui = T.ui, current = selected(), serial = ++T.evidenceSerial;
    T.evidenceFor = T.selected || null;
    ui.evidence.replaceChildren();
    // Manual entries and the counted “从不” habits have no messages behind them.
    if (!current || current.item.manual === true || (current.kind === "habit" && current.item.key.startsWith("never:"))) return;
    const habit = current.kind === "habit";
    try {
      const result = await A.rpc("page.tagEvidence", habit ? { key: current.item.key } : { name: current.item.name });
      if (serial !== T.evidenceSerial || A.masked) return;
      if (!object(result) || !Array.isArray(result.items)) throw new Error("依据暂时无法读取，请重试。");
      const items = result.items.filter(object);
      ui.evidence.replaceChildren(...items.map(item => card(item, habit)));
      if (count(result.total) > items.length) ui.evidence.append(A.el("p", "fa-note", `共 ${result.total} 条，显示最近 ${items.length} 条`));
    } catch (error) {
      if (serial === T.evidenceSerial && !A.masked) ui.evidence.replaceChildren(A.el("p", "fa-note", error.message || "依据读取失败"));
    }
  }

  window.addEventListener("keydown", event => {
    // 输入框里的 Ctrl+Z 留给文字撤销；受限窗口没有撤销。
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && String(event.key).toLowerCase() === "z" && !event.repeat &&
      T.ui && !T.ui.root.hidden && T.data && !A.restricted && !A.masked &&
      !event.target?.closest?.("textarea, select, [contenteditable], input:not([type=checkbox])")) {
      event.preventDefault(); void undo(false);
    }
  });

  window.FriendTags = { load, undo: () => undo(false) };
})();
