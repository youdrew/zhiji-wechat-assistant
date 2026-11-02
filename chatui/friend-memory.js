"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 关系记忆: candidates, confirmed records, tags/timeline views, editing and evidence.
(function () {
  const A = window.FriendApp;
  const TAGS = ["基本信息", "经历", "性格", "爱好", "偏好与忌口", "边界", "共同事件"];
  const PAGE = 50;
  const M = { view: "tags", pending: [], confirmed: [], total: 0, version: 0, loading: false, request: 0,
    editing: new Map(), adding: false, ignored: null, overview: null, pendingShown: PAGE };
  const page = () => A.byId("pageMemory");
  const evidenceWords = { changed: "原文已变化", missing: "原文当前缺失", recalled: "原文已撤回",
    unready: "原文待核对", "source-changed": "来源已变化" };

  async function loadList(status, offset, limit) {
    return A.api("/api/friend/memories?" + A.query({ view: M.view, status, offset: String(offset), limit: String(limit) }));
  }
  async function load(reset = true) {
    const request = ++M.request;
    M.loading = true;
    try {
      const pending = [];
      for (let offset = 0; offset < 1000; offset += 200) {
        const part = await loadList("pending", offset, 200);
        pending.push(...part.items);
        if (offset + 200 >= part.total) break;
      }
      const confirmed = await loadList("confirmed", reset ? 0 : M.confirmed.length, PAGE);
      if (request !== M.request) return;
      M.pending = pending;
      M.confirmed = reset ? confirmed.items : M.confirmed.concat(confirmed.items);
      M.total = confirmed.total;
      M.version = confirmed.memoryVersion;
    } catch (error) {
      if (request === M.request && !A.masked) A.toast(error.message || "记忆读取失败");
    } finally {
      if (request === M.request) {
        M.loading = false;
        M.loaded = true;
        render();
        if (M.restore) applyDrafts();
      }
    }
  }

  function timeLabel(item) {
    if (item.eventTime) return "事件 " + item.eventTime;
    if (item.firstMentionMs) return "首次提及 " + A.formatDate(item.firstMentionMs);
    return "";
  }
  function marks(item) {
    const result = [];
    if (item.status === "pending") result.push(["待确认", "pending"]);
    if (item.relation === "supplement") result.push(["补充", "warn"]);
    if (item.relation === "conflict") result.push(["与原记录矛盾", "warn"]);
    if (item.relation === "uncertain") result.push(["待核对是否重复", "warn"]);
    if (item.origin === "user") result.push(["用户记录" + (item.evidenceCount ? "" : " · 未关联原文"), ""]);
    if (item.userCorrected) result.push(["用户修正", ""]);
    for (const state of item.evidenceStates || []) if (evidenceWords[state]) result.push([evidenceWords[state], "warn"]);
    return result;
  }
  function itemRow(item) {
    const row = A.el("div", "fa-item");
    row.dataset.id = item.id;
    const main = A.el("div", "fa-item-main");
    const body = A.el("div", "fa-item-text");
    body.append(A.el("div", "", item.text));
    const meta = A.el("div", "fa-item-meta");
    meta.append(A.el("span", "", item.tags.join("、")));
    if (timeLabel(item)) meta.append(A.el("span", "", timeLabel(item)));
    for (const [label, kind] of marks(item)) meta.append(A.el("span", "fa-mark" + (kind ? " " + kind : ""), label));
    body.append(meta);
    const actions = A.el("div", "fa-item-actions");
    if (item.evidenceCount) actions.append(A.button("原文", "fa-plain", () => openEvidence(item)));
    if (item.status === "pending" && item.relation !== "conflict") {
      actions.append(A.button(item.relation === "supplement" ? "确认合并" : "确认", "fa-plain", () => decide(item, "confirm")),
        A.button("忽略", "fa-plain", () => decide(item, "ignore")));
    } else if (item.status === "pending") {
      actions.append(A.button("忽略", "fa-plain", () => decide(item, "ignore")));
    } else if (item.status === "confirmed") {
      actions.append(A.button("编辑", "fa-plain", () => startEdit(item)), A.button("删除", "fa-plain", () => remove(item)));
    }
    main.append(body, actions);
    row.append(main);
    if (item.relatedId && item.status === "pending") row.append(relatedBlock(item));
    const editing = M.editing.get(item.id);
    if (editing) row.append(editor(editing));
    return row;
  }
  function relatedBlock(item) {
    const block = A.el("div", "fa-related", "相关原记录读取中…");
    void A.api("/api/friend/memory?" + A.query({ id: item.relatedId })).then(data => {
      block.textContent = (item.relation === "supplement" ? "将合并到：" : "原记录：") + data.memory.text;
      if (item.relation === "conflict") {
        const choice = A.el("div", "fa-choice");
        choice.append(A.button("保留原记录", "fa-secondary", () => decide(item, "confirm", "keep")),
          A.button("采用新说法", "fa-secondary", () => decide(item, "confirm", "adopt")),
          A.button("分别注明时期", "fa-secondary", () => decide(item, "confirm", "both")));
        block.append(choice);
      }
    }).catch(() => { block.textContent = "原记录已不存在，可直接确认或忽略。"; });
    return block;
  }

  function group(title, items, batch) {
    const fragment = document.createDocumentFragment();
    const head = A.el("div", "fa-group");
    head.append(A.el("span", "", title), A.el("span", "fa-spacer"));
    if (batch) for (const [label, action] of batch) head.append(A.button(label, "fa-plain", action));
    const list = A.el("div", "fa-list");
    for (const item of items) list.append(itemRow(item));
    fragment.append(head, list);
    return fragment;
  }
  function firstTag(item) { return TAGS.find(tag => item.tags.includes(tag)) || TAGS[0]; }
  function render() {
    const root = page();
    if (A.masked) return;
    // Keep typing position when background progress re-renders the list.
    const active = document.activeElement;
    const focusKey = root.contains(active) ? active?.dataset?.key : null;
    const selection = focusKey ? [active.selectionStart, active.selectionEnd] : null;
    const scroll = root.parentElement.scrollTop;
    renderInto(root);
    root.parentElement.scrollTop = scroll;
    if (focusKey) {
      const target = root.querySelector(`[data-key="${CSS.escape(focusKey)}"]`);
      if (target) { target.focus(); try { target.setSelectionRange(...selection); } catch { } }
    }
  }
  function renderInto(root) {
    root.replaceChildren();
    if (M.ignored) { renderIgnored(root); return; }
    const bar = A.el("div", "fa-bar");
    const segment = A.el("div", "fa-segment");
    for (const [view, label] of [["tags", "按标签"], ["timeline", "按时间线"]])
      segment.append(A.button(label, "", () => { if (M.view !== view) { M.view = view; void load(); } },
        { "aria-pressed": String(M.view === view) }));
    // Analysis schemes propose candidates; this host page reviews and edits the user's memories.
    bar.append(segment, A.el("span", "fa-spacer"), A.button("新增", "fa-plain", () => startAdd()));
    root.append(bar);
    if (M.adding) {
      const block = A.el("div", "fa-list");
      block.append(editor(M.adding));
      root.append(block);
    }
    if (M.pending.length) {
      const all = [["全部确认", () => batch("batch-confirm", { all: true })], ["全部忽略", () => batch("batch-ignore", { all: true })]];
      // Long candidate lists render in pages; group batches act only on what is shown.
      const shown = M.pending.slice(0, M.pendingShown);
      if (M.view === "tags") {
        root.append(A.el("div", "fa-group", `待确认 ${M.pending.length} 条`));
        const head = root.lastChild;
        head.append(A.el("span", "fa-spacer"));
        for (const [label, action] of all) head.append(A.button(label, "fa-plain", action));
        for (const tag of TAGS) {
          const items = shown.filter(item => firstTag(item) === tag);
          if (items.length) root.append(group(`${tag} · ${items.length}`, items, [
            ["确认本组", () => batch("batch-confirm", null, items)], ["忽略本组", () => batch("batch-ignore", null, items)]]));
        }
      } else root.append(group(`待确认 ${M.pending.length} 条`, shown, all));
      if (shown.length < M.pending.length)
        root.append(A.button(`显示更多待确认（还有 ${M.pending.length - shown.length} 条）`, "fa-plain", () => {
          M.pendingShown += PAGE;
          render();
        }));
    }
    if (M.confirmed.length) {
      if (M.pending.length) root.append(A.el("div", "fa-group", `已确认 ${M.total} 条`));
      if (M.view === "tags") {
        for (const tag of TAGS) {
          const items = M.confirmed.filter(item => firstTag(item) === tag);
          if (items.length) root.append(group(tag, items));
        }
      } else root.append(group("已确认 · 按事件或首次提及时间", M.confirmed));
      if (M.confirmed.length < M.total)
        root.append(A.button(`加载更多（还有 ${M.total - M.confirmed.length} 条）`, "fa-plain", () => load(false)));
    }
    if (!M.pending.length && !M.confirmed.length && !M.adding && !M.loading) {
      const empty = A.el("div", "fa-empty");
      const built = M.overview?.memory?.built;
      empty.append(A.el("p", "", built ? "还没有记忆。可以手动新增，或等有新聊天时再分析。" :
        "还没有关系记忆。开始分析后，这里会出现待你确认的候选，也可以手动新增。"));
      root.append(empty);
    }
  }
  function renderIgnored(root) {
    const bar = A.el("div", "fa-bar");
    bar.append(A.button("返回", "fa-plain", () => { M.ignored = null; render(); }),
      A.el("span", "fa-note", "已忽略的记录不会再次自动提出，也不会用于建议。"));
    root.append(bar);
    if (!M.ignored.length) root.append(A.el("div", "fa-empty", "没有已忽略的记录。"));
    else root.append(group(`已忽略 ${M.ignored.length} 条`, M.ignored));
  }
  async function showIgnored() {
    try {
      const data = await loadList("rejected", 0, 200);
      M.ignored = data.items;
      render();
    } catch (error) { A.toast(error.message || "读取失败"); }
  }

  // -- decisions ---------------------------------------------------------------------------
  async function mutate(body, message) {
    try {
      const result = await A.api("/api/friend/memory", { method: "POST", body });
      if (message) A.toast(message);
      A.emit({ type: "memory-changed" });
      return result;
    } catch (error) {
      A.toast(error.message || "操作失败");
      if (error.code === "request-conflict") void load();
      return null;
    } finally { void load(); }
  }
  function decide(item, action, choice) {
    return mutate({ action, id: item.id, expectedVersion: item.version, ...(choice ? { choice } : {}) });
  }
  function batch(action, scope, items) {
    const verb = action === "batch-confirm" ? "确认" : "忽略";
    const count = items ? items.length : M.pending.length;
    if (!window.confirm(`${verb}所选范围内的 ${count} 条候选？${action === "batch-confirm" ? "需要单独选择的矛盾候选会跳过。" : ""}`)) return;
    return mutate(items ? { action, ids: items.map(item => item.id) } :
      { action, scope, expectedMemoryVersion: M.version }, `已${verb}`);
  }
  function remove(item) {
    if (!window.confirm("删除这条记忆？之后不会再自动提出相同内容。不会删除微信原记录、助手对话或联系目标。")) return;
    return mutate({ action: "delete", id: item.id, expectedVersion: item.version }, "已删除");
  }
  function clearAll() {
    if (!window.confirm(`清空这位联系人的全部结构化记忆（含待确认候选）？\n\n这只清理记忆条目，聊天原文仍在本机，之后重新建档可能再次提出。不会影响其他联系人、微信记录、助手对话或联系目标。`)) return;
    return mutate({ action: "clear" }, "已清空");
  }

  // -- editor with autosaved drafts ---------------------------------------------------------------
  function editorState(entry, item) {
    const draft = A.draft(entry);
    const base = draft.payload || { text: item?.text || "", tags: item?.tags || [], eventTime: item?.eventTime || "" };
    return { entry, item, draft, text: base.text, tags: [...base.tags], eventTime: base.eventTime || "",
      restored: draft.restored, baseVersion: item?.version ?? null };
  }
  function startEdit(item) { M.editing.set(item.id, editorState("memory:" + item.id, item)); render(); }
  function startAdd() { M.adding = M.adding || editorState("memory:new", null); render(); }
  function editor(state) {
    const form = A.el("div", "fa-form");
    const text = A.el("textarea");
    text.value = state.text;
    text.maxLength = 300;
    text.placeholder = "例如：对方不吃香菜";
    text.setAttribute("aria-label", "记忆内容");
    text.dataset.key = state.entry + ":text";
    const tags = A.el("div", "fa-tags");
    for (const tag of TAGS) {
      const chip = A.button(tag, "fa-tag", () => {
        state.tags = state.tags.includes(tag) ? state.tags.filter(value => value !== tag) : [...state.tags, tag];
        chip.setAttribute("aria-pressed", String(state.tags.includes(tag)));
        changed();
      }, { "aria-pressed": String(state.tags.includes(tag)) });
      tags.append(chip);
    }
    const when = A.el("input");
    when.type = "text";
    when.value = state.eventTime;
    when.placeholder = "事件时间（可选）：YYYY、YYYY-MM 或 YYYY-MM-DD";
    when.setAttribute("aria-label", "事件时间");
    when.dataset.key = state.entry + ":when";
    const actions = A.el("div", "fa-form-actions");
    const unsaved = A.el("span", "fa-unsaved", state.restored ? "未保存（已恢复上次编辑）" : "");
    const save = A.button("保存", "fa-primary", async () => {
      save.disabled = true;
      const body = state.item ? { action: "edit", id: state.item.id, expectedVersion: state.baseVersion } : { action: "create" };
      const result = await mutate({ ...body, text: state.text, tags: state.tags, eventTime: state.eventTime || null },
        "已保存");
      save.disabled = false;
      if (result) { await A.closeDraft(state.entry); finish(); }
    });
    const cancel = A.button("放弃", "fa-plain", async () => { await A.closeDraft(state.entry); finish(); });
    actions.append(save, cancel, unsaved);
    function changed() {
      state.text = text.value;
      state.eventTime = when.value.trim();
      unsaved.textContent = "未保存";
      A.updateDraft(state.draft, { text: state.text, tags: state.tags, eventTime: state.eventTime }, state.baseVersion);
    }
    function finish() {
      if (state.item) M.editing.delete(state.item.id); else M.adding = false;
      render();
    }
    text.addEventListener("input", changed);
    when.addEventListener("input", changed);
    form.append(text, tags, when, actions);
    if (!state.item) form.prepend(A.el("div", "fa-note", "手动记录默认已确认，标为“用户记录”。"));
    return form;
  }
  function restoreDrafts(drafts) {
    // Restore after the list has loaded, so edits attach to their records.
    M.restore = drafts;
    if (M.loaded && !M.loading) applyDrafts();
  }
  function applyDrafts() {
    const drafts = M.restore;
    M.restore = null;
    if (!drafts) return;
    for (const [entry] of drafts) {
      if (entry === "memory:new") M.adding = editorState(entry, null);
      else if (entry.startsWith("memory:")) {
        const id = entry.slice(7);
        const item = M.confirmed.find(candidate => candidate.id === id);
        if (item) M.editing.set(id, editorState(entry, item));
      }
    }
    render();
  }

  // -- building -------------------------------------------------------------------------------------
  async function openEvidence(item) {
    const body = A.openSheet("原文与上下文", "evidence:" + item.id);
    try {
      const detail = await A.api("/api/friend/memory?" + A.query({ id: item.id }));
      if (A.sheetOwner !== "evidence:" + item.id) return;
      body.append(A.el("p", "fa-text", detail.memory.text));
      if (detail.memory.userCorrected) body.append(A.el("p", "fa-note", "用户修正过内容，以下原文仅供核对。"));
      detail.evidence.forEach((evidence, index) => {
        const block = A.el("div", "fa-section");
        block.append(A.el("h3", "", `${roleWords[evidence.role] || "发言"} · ${A.formatDate(evidence.timeMs, true)}` +
          (evidenceWords[evidence.status] ? " · " + evidenceWords[evidence.status] : "")));
        const context = A.el("div");
        context.append(A.el("div", "fa-msg-text", evidence.excerpt));
        block.append(context);
        // The first source opens with its context; others load on request.
        if (index === 0) void showContext(item, evidence, context);
        else {
          const more = A.button("查看上下文", "fa-plain", () => { more.remove(); void showContext(item, evidence, context); });
          block.append(more);
        }
        body.append(block);
      });
    } catch (error) {
      if (A.sheetOwner === "evidence:" + item.id) body.append(A.el("p", "", error.message || "原文读取失败"));
    }
  }
  async function showContext(item, evidence, container) {
    try {
      const data = await A.api("/api/friend/evidence?" + A.query({ id: item.id, ordinal: String(evidence.ordinal) }));
      if (A.sheetOwner !== "evidence:" + item.id || !data.messages.length) return;
      container.replaceChildren();
      for (const message of data.messages) {
        const row = A.el("div", "fa-msg" + (message.target ? " target" : ""));
        row.append(A.el("div", "fa-msg-head", `${message.speaker} · ${A.formatDate(message.timeMs, true)}` +
          (message.role === "forward" ? " · 转发" : "") + (message.quote ? " · 含引用" : "")));
        row.append(A.el("div", "fa-msg-text", message.media ? `[${message.media.kind === "voice" ? "语音" : "媒体"}，内容未在此加载]` : message.text));
        container.append(row);
      }
      if (data.locatable && window.friendHost?.locate) {
        container.append(A.button("在聊天中定位", "fa-secondary", async () => {
          try {
            const target = await A.api("/api/friend/locate", { method: "POST", body: { id: item.id, ordinal: evidence.ordinal } });
            const ok = await window.friendHost.locate(target);
            if (!ok) A.toast("未能定位，请确认主窗口仍是同一账号");
          } catch (error) { A.toast(error.message || "未能定位"); }
        }));
      }
    } catch { }
  }

  window.FriendMemory = {
    load, render, showIgnored, startAdd, clear: clearAll, restoreDrafts,
    setOverview(overview) {
      const previous = M.overview;
      M.overview = overview;
      const signature = value => JSON.stringify([value.history.state, value.memory]);
      if (!previous || previous.memoryVersion !== overview.memoryVersion) void load();
      else if (signature(previous) !== signature(overview)) render();
    },
  };
})();
