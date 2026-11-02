"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Core-owned import and review UI. Recognition is performed by the installed plugin.
(function () {
  const ACTIVE = new Set(["queued", "preparing", "running", "recognizing", "processing", "organizing", "stopping"]);
  const STATUS = { draft: "待添加素材", queued: "排队中", preparing: "准备素材", running: "后台整理中",
    recognizing: "识别中", processing: "后台整理中", organizing: "整理记录", review: "待核对", ready: "待核对",
    awaiting_review: "待核对", needs_review: "待核对", saved: "已保存", completed: "已完成", stopping: "正在停止", stopped: "已停止，可继续",
    interrupted: "已中断，可继续", failed: "处理失败，可重试", partial: "部分已保存，待核对" };
  const ISSUE = { unclear_text: "文字不清晰", unclear: "文字不清晰", unknown_sender: "发送者不明确", sender_unknown: "发送者不明确",
    truncated: "气泡被截断", incomplete: "内容不完整", uncertain_order: "顺序不确定", order_uncertain: "顺序不确定",
    duplicate: "疑似重复", possible_duplicate: "疑似重复", missing_time: "时间未知", time_unknown: "时间未知",
    incomplete_time: "时间不完整", recognition_difference: "重新识别存在差异", "recognition-difference": "重新识别存在差异",
    "edited-unconfirmed": "编辑待确认", "possible-duplicate": "疑似重复", missing_original: "原件不可用", overlap: "可能与已有记录重叠", gap: "画面存在缺口" };
  Object.assign(ISSUE, { missing_region: "画面有缺失区域", multiple_conversations: "素材中混入多个会话，请拆分导入", no_visible_messages: "这份画面没有可辨认的聊天消息",
    vision_unconfirmed: "当前模型未声明支持图片，识别结果可能不可靠", review_unresolved: "复核未能确认" });
  // Mirrors the store's non-blocking time issues; these never keep a record from saving.
  const TIME_ISSUES = new Set(["time-unknown", "time-incomplete", "unknown-time", "missing-time", "date-incomplete", "incomplete_time", "missing_original", "时间不完整", "时间未知"]);
  const BATCH_WARNINGS = new Set(["multiple_conversations", "vision_unconfirmed"]);
  const escId = value => encodeURIComponent(String(value || ""));
  function senderKnown(candidate) {
    return ["self", "other"].includes(candidate?.sender) || (candidate?.kind === "system" && candidate?.sender === "unknown");
  }
  function senderLabel(candidate, otherName = "对方") {
    return candidate?.kind === "system" ? "系统通知" : candidate?.sender === "self" ? "我" : candidate?.sender === "other" ? otherName : "发送者未知";
  }
  function candidateEvidence(candidate, segments = []) {
    const preferredId = candidate?.evidenceSegmentId || candidate?.preferredEvidenceSegmentId;
    return { segment: segments.find(item => item.id === (preferredId || candidate?.segmentId)),
      bbox: preferredId ? candidate?.bbox : candidate?.original?.bbox || candidate?.bbox };
  }
  // Every frame in which the bubble was observed, preferred frame first. Only boxes the
  // candidate actually records are highlighted; other frames are shown without a box.
  function evidenceFrames(candidate, segments = []) {
    const first = candidateEvidence(candidate, segments), frames = first.segment ? [first] : [];
    for (const id of [candidate?.segmentId, ...(candidate?.evidenceSegmentIds || [])]) {
      const segment = segments.find(item => item.id === id);
      if (segment && !frames.some(item => item.segment.id === id))
        frames.push({ segment, bbox: id === candidate.segmentId ? candidate.original?.bbox || null : null });
    }
    return frames;
  }
  function candidateReady(candidate) {
    return candidate?.ready === true && !candidate.ignored && !candidate.autoMergedInto && !candidate.savedMessageId &&
      senderKnown(candidate) && typeof candidate.text === "string" && candidate.text.trim().length > 0;
  }
  function candidatePending(candidate) {
    return !candidate.ignored && !candidate.autoMergedInto && (!candidate.savedMessageId || !!candidate.recognitionSuggestion) && !candidateReady(candidate);
  }
  // Labels shown under one message; ready, ignored and settled records carry none.
  function reviewIssues(candidate) {
    if (!candidatePending(candidate)) return [];
    const labels = (candidate.issues || []).filter(issue => !TIME_ISSUES.has(issue)).map(issue => ISSUE[issue] || issue);
    if (!senderKnown(candidate)) labels.push(ISSUE.unknown_sender);
    if (!String(candidate.text || "").trim()) labels.push("未识别出正文");
    if (candidate.truncated) labels.push(ISSUE.truncated);
    if (candidate.recognitionSuggestion) labels.push(ISSUE.recognition_difference);
    return labels.length ? [...new Set(labels)] : ["待核对"];
  }
  // Frame notes belong beside their frame; only whole-batch problems are shown above the review.
  function batchWarnings(batch) {
    return (batch?.warnings || []).filter(item => BATCH_WARNINGS.has(item) || (item === "no_visible_messages" && !batch.candidates?.length));
  }
  function frameWarnings(segment) {
    return (segment?.warnings || []).filter(item => !BATCH_WARNINGS.has(item)).map(item => ISSUE[item] || item);
  }
  function countCandidates(candidates = []) {
    return candidates.reduce((all, item) => {
      if (item.autoMergedInto) return all;
      all.total++;
      if (item.savedMessageId) all.saved++;
      if (item.ignored) all.ignored++;
      else if (candidateReady(item)) all.ready++;
      else if (!item.savedMessageId || item.recognitionSuggestion) all.pending++;
      return all;
    }, { total: 0, ready: 0, pending: 0, saved: 0, ignored: 0 });
  }
  function statusText(batch) {
    const counts = Array.isArray(batch?.candidates) ? countCandidates(batch.candidates) : batch?.counts;
    if (counts?.saved && counts?.pending) return "部分已保存，待核对";
    if (counts?.saved && counts?.ready) return "部分已保存，待确认保存";
    return STATUS[batch?.state] || batch?.state || "待添加素材";
  }
  function evidenceUrl(id, original = false) {
    return `/api/external-import/${original ? "material" : "evidence"}?id=${escId(id)}`;
  }
  function mergeDraft(candidate, draft) {
    return { text: draft?.text ?? candidate?.text ?? "", sender: draft?.sender ?? candidate?.sender ?? "unknown",
      dateLabel: draft?.dateLabel ?? candidate?.dateLabel ?? "", ...(draft?.duplicateDecision || candidate?.duplicateDecision ? { duplicateDecision: draft?.duplicateDecision || candidate?.duplicateDecision } : {}) };
  }
  function draftChanged(candidate, values) {
    return values.text !== (candidate.text ?? "") || values.sender !== (candidate.sender ?? "unknown") ||
      values.dateLabel !== (candidate.dateLabel || "") || (values.duplicateDecision || "") !== (candidate.duplicateDecision || "");
  }
  function reviewPatch(candidate, values) {
    return { ...values, ...(values.dateLabel !== (candidate.dateLabel || "") ? {
      time: null, timePrecision: values.dateLabel.trim() ? "partial" : "unknown",
    } : {}) };
  }
  // Confirming keeps the bubble as shown. A possible repeat of another bubble is kept as
  // its own message (deleting it is the other answer); a re-read of a saved one is the same.
  function confirmPatch(candidate, values) {
    const repeat = (candidate.duplicateMessageId || candidate.duplicateCandidateId) && !values.duplicateDecision;
    return { ...reviewPatch(candidate, values), ignored: false, ...(repeat ? { duplicateDecision: candidate.savedVersion ? "reuse" : "new" } : {}) };
  }
  // Deleted (ignored) bubbles leave the review at once; the doubt filter keeps only open ones.
  function reviewVisible(candidates = [], filter = "all") {
    return candidates.filter(item => !item.ignored && !item.autoMergedInto && (filter === "all" || candidatePending(item)));
  }
  function pictureUrl(kind, id) {
    return `/api/external-import/picture?${kind === "message" ? "message" : "candidate"}=${escId(id)}`;
  }
  function validateFiles(files, existing = [], limits = {}) {
    const maximum = Number(limits.maxFileBytes || limits.max_file_bytes || 128 * 1024 * 1024);
    if (files.length + existing.length > (limits.maxMaterials || 20)) return "每批最多添加 20 份素材。";
    if (existing.filter(item => item.kind === "video").length + files.filter(item => /\.(mp4|mov|webm)$/i.test(item.name)).length > 1)
      return "一批最多包含一段录屏；请为其他录屏新建批次。";
    for (const file of files) {
      if (!/\.(png|jpe?g|webp|mp4|mov|webm)$/i.test(file.name)) return `暂不支持 ${file.name}。请选择 PNG、JPG、WebP、MP4、MOV 或 WebM。`;
      if (file.size <= 0 || file.size > maximum) return `${file.name} 为空或超过 ${Math.round(maximum / 1048576)} MB 限制。`;
    }
    return "";
  }
  function orderWithGaps(messages, gaps, sourceId) {
    return [...messages.map(message => ({ kind: "message", value: message })),
      ...gaps.filter(gap => !sourceId || gap.sourceId === sourceId).map(gap => ({ kind: "gap", value: gap }))]
      .sort((a, b) => String(a.value.sourceId || "").localeCompare(String(b.value.sourceId || "")) ||
        String(a.value.order ?? "").localeCompare(String(b.value.order ?? ""), undefined, { numeric: true }));
  }
  function usageValue(summary, field, knownField) {
    if (!Number.isFinite(summary?.[field]) || !Number.isFinite(summary?.requests) || !Number.isFinite(summary?.[knownField])) return null;
    return summary.requests > 0 && summary[knownField] === 0 ? null : summary[field];
  }
  function validBox(value) {
    return Array.isArray(value) && value.length === 4 && value.every(item => Number.isFinite(item) && item >= 0 && item <= 1) &&
      value[2] > 0 && value[3] > 0 && value[0] + value[2] <= 1.001 && value[1] + value[3] <= 1.001;
  }
  function normalizedScope(value) {
    if ((value.account != null && typeof value.account !== "string") || (value.user != null && typeof value.user !== "string"))
      return { account: "", user: "", friendId: "", name: "" };
    const account = typeof value.account === "string" ? value.account : "", user = typeof value.user === "string" ? value.user : "";
    const friendId = typeof value.friendId === "string" && value.friendId.length <= 100 ? value.friendId : "";
    if ((account || user) && (!account || !user || account.length > 256 || user.length > 256 || user.endsWith("@chatroom")))
      return { account: "", user: "", friendId: "", name: "" };
    return { account, user, friendId, name: typeof value.name === "string" ? value.name.slice(0, 128) : "" };
  }
  function friendForScope(friends, scope) {
    const selected = normalizedScope(scope);
    if (selected.account && selected.user)
      return friends.find(item => item.wechatAccount === selected.account && item.wechatUser === selected.user) || null;
    return selected.friendId ? friends.find(item => item.id === selected.friendId) || null : null;
  }
  function batchesForFriend(batches, friendId) {
    return friendId ? batches.filter(item => item.friendId === friendId && !item.archived) : [];
  }
  function coalesceGaps(rows) {
    const result = [];
    for (const row of rows) {
      const previous = result[result.length - 1];
      if (row.kind === "gap" && previous?.kind === "gap" && row.value.sourceId === previous.value.sourceId &&
          row.value.batchId === previous.value.batchId && !!row.value.archived === !!previous.value.archived) previous.count++;
      else result.push({ ...row, count: 1 });
    }
    return result;
  }
  // Export only deterministic UI rules for synthetic regression tests.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { candidateReady, candidatePending, reviewIssues, batchWarnings, frameWarnings, countCandidates, statusText, evidenceUrl, mergeDraft, draftChanged, reviewPatch, confirmPatch, reviewVisible, pictureUrl, validateFiles, orderWithGaps, usageValue, validBox, senderLabel, candidateEvidence, evidenceFrames, normalizedScope, friendForScope, batchesForFriend, coalesceGaps };
    return;
  }
  const byId = id => document.getElementById(id);
  const el = (tag, className = "", text) => {
    const node = document.createElement(tag); node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const state = { friends: [], batches: [], sources: [], status: {}, friendId: "", batch: null, page: "saved", epoch: 0, listRevision: 0,
    editing: "", editSender: "", chain: Promise.resolve(), pendingEdits: 0, menu: null, undo: [], dragging: "",
    focused: "", frame: 0, reveal: false, rows: new Map(), filter: "all", busy: false, submitting: false, loading: 0, poll: null, destroyed: false,
    messageQuery: { q: "", platform: "", batchId: "" }, messages: { messages: [], gaps: [] } };
  const query = new URLSearchParams(window.location.search || window.location.hash.slice(1));
  const host = window.externalImportHost, opener = window.opener;
  let returningToChat = false;
  const linked = normalizedScope(host?.getContext ? {} : Object.fromEntries(query));
  function button(text, action, className = "", mutation = false) {
    const node = el("button", className, text); node.type = "button";
    node.addEventListener("click", action);
    if (mutation) { node.dataset.mutation = "true"; node.disabled = state.busy; }
    return node;
  }
  function disable(node, value) { node.dataset.disabled = String(!!value); node.disabled = !!value || state.busy; }
  function input(label, value = "", options = {}) {
    const wrapper = el("label", `ei-field ${options.wide ? "ei-span" : ""}`);
    wrapper.append(el("span", "", label));
    const node = el(options.multiline ? "textarea" : "input");
    node.value = value; node.setAttribute("aria-label", label);
    node.maxLength = options.maxLength || (options.multiline ? 10000 : 256);
    if (options.placeholder) node.placeholder = options.placeholder;
    if (options.required) node.required = true;
    if (options.onInput) node.addEventListener("input", () => options.onInput(node.value));
    wrapper.append(node);
    if (options.help) wrapper.append(el("small", "ei-help", options.help));
    return { wrapper, node };
  }
  function select(label, options, value, onChange) {
    const wrapper = el("label", "ei-field"); wrapper.append(el("span", "", label));
    const node = el("select"); node.setAttribute("aria-label", label);
    for (const [key, text] of options) { const option = el("option", "", text); option.value = key; node.append(option); }
    node.value = value;
    if (onChange) node.addEventListener("change", () => onChange(node.value));
    wrapper.append(node); return { wrapper, node };
  }
  function sourceInput(value, onInput) {
    const field = input("来源", value, { required: true, maxLength: 80, placeholder: "填写聊天来源名称", onInput });
    const names = [...new Set(state.sources.map(source => source.platform).filter(Boolean))];
    if (names.length) {
      const suggestions = el("datalist"); suggestions.id = "eiSourceNames";
      for (const name of names) { const option = el("option"); option.value = name; suggestions.append(option); }
      field.node.setAttribute("list", suggestions.id); field.wrapper.append(suggestions);
    }
    return field;
  }
  function notice(message) { byId("eiStatus").textContent = message; }
  function error(message) {
    const box = byId("eiError"); box.textContent = message || ""; box.hidden = !message;
    const modal = byId("eiDialog");
    modal.querySelector(".ei-dialog-error")?.remove();
    if (message && modal.open) {
      const local = el("div", "ei-error ei-dialog-error", message); local.setAttribute("role", "alert");
      modal.querySelector("h2").after(local);
    }
  }
  function formatBytes(value) {
    const size = Number(value) || 0;
    return size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB`;
  }
  function friend() { return state.friends.find(item => item.id === state.friendId); }
  function sourceName(source) { return source?.platform || "外部来源"; }
  function hasScope() { return !!(linked.friendId || linked.account && linked.user); }
  function counts(batch = state.batch) { return Array.isArray(batch?.candidates) ? countCandidates(batch.candidates) : batch?.counts || countCandidates(); }
  function issues(candidate) { return (candidate.issues || []).map(issue => ISSUE[issue] || issue).join("、"); }
  function navigateApp(target) {
    if (host?.navigate) {
      void Promise.resolve(host.navigate(target)).catch(() => error("未能返回主窗口，请稍后重试。"));
    } else if (opener && !opener.closed) {
      if (target === "chat") { returningToChat = true; window.close(); }
      else { opener.postMessage({ type: "kyf-external:navigate", target }, window.location.origin); opener.focus(); }
    } else window.location.href = target === "chat" ? "/" : `/?settings=${["models", "model-settings"].includes(target) ? "models" : "plugins"}`;
  }
  async function api(path, body, raw) {
    const epoch = state.epoch;
    let response;
    try {
      response = await fetch(`/api/external-import/${path}`, body !== undefined || raw ? {
        method: "POST", headers: { "Content-Type": raw ? "application/octet-stream" : "application/json" },
        body: raw || JSON.stringify(body),
      } : {});
    } catch { throw new Error("本机服务暂时无法连接，请稍后重试。已保留当前编辑。 "); }
    let data;
    try { data = await response.json(); } catch { throw new Error(`本机服务返回了无法读取的结果（${response.status}）。`); }
    if (epoch !== state.epoch) throw new Error("好友已切换。");
    if (!response.ok) throw new Error(data?.message || data?.detail || (typeof data?.error === "string" ? data.error : "请求未完成"));
    return data;
  }
  async function run(action) {
    if (state.busy) return;
    const epoch = state.epoch;
    state.listRevision++;
    state.busy = true; error("");
    for (const node of document.querySelectorAll('[data-mutation="true"]')) node.disabled = true;
    try { return await action(); }
    catch (problem) { if (epoch === state.epoch) error(problem?.message || "操作未完成，请重试。"); }
    finally {
      state.busy = false;
      for (const node of document.querySelectorAll('[data-mutation="true"]')) node.disabled = node.dataset.disabled === "true";
      if (state.hostRefreshPending) { state.hostRefreshPending = false; refreshFromHost(); }
    }
  }
  async function withPendingSubmission(action) {
    state.submitting = true;
    try { return await action(); }
    finally { state.submitting = false; }
  }
  function acceptBatch(result) {
    const batch = result?.batch || result;
    if (batch?.id && Array.isArray(batch.candidates)) {
      if (!state.friendId || batch.friendId !== state.friendId) throw new Error("这份任务不属于当前好友，请返回后重新打开。");
      if (state.batch?.id !== batch.id) state.undo = [];
      state.batch = batch;
      const index = state.batches.findIndex(item => item.id === batch.id);
      if (index < 0) state.batches.unshift(batch); else state.batches[index] = batch;
    }
    renderContext();
    return state.batch;
  }
  async function refreshLists() {
    const epoch = state.epoch;
    if (!hasScope()) { state.friends = []; state.batches = []; renderContext(); return; }
    const [friends, status] = await Promise.all([api("friends"), api("status")]);
    if (epoch !== state.epoch) return;
    state.friends = friends.friends || []; state.status = status;
    state.friendId = friendForScope(state.friends, linked)?.id || "";
    const batches = state.friendId ? await api(`batches?friendId=${escId(state.friendId)}`) : { batches: [] };
    if (epoch !== state.epoch) return;
    state.batches = batchesForFriend(batches.batches || [], state.friendId);
    renderContext();
  }
  async function loadSources() {
    const epoch = state.epoch, selected = state.friendId;
    const sources = selected ? (await api(`sources?friendId=${escId(selected)}`)).sources || [] : [];
    if (epoch === state.epoch && state.friendId === selected) state.sources = sources;
  }
  async function openBatch(id) {
    const serial = ++state.loading;
    error("");
    try {
      const batch = await api(`batch?id=${escId(id)}`);
      if (serial !== state.loading || state.destroyed) return;
      acceptBatch(batch); state.focused = ""; state.reveal = true;
      await loadSources();
      if (serial !== state.loading || state.destroyed) return;
      await setPage("review");
    } catch (problem) { error(problem.message); }
  }
  function renderContext() {
    document.title = `${friend()?.name || linked.name ? `${friend()?.name || linked.name} · ` : ""}外部聊天 · 知己微信助手`;
    const count = batchesForFriend(state.batches, state.friendId).length;
    byId("eiTaskCount").textContent = count ? ` ${count}` : "";
    const emptyHome = state.page === "tasks" ? !count : state.page === "saved" &&
      !state.messages.messages?.length && !state.messages.gaps?.length && !state.messageQuery.q && !state.messageQuery.platform && !state.messageQuery.batchId;
    byId("eiAdd").hidden = !hasScope() || !["saved", "tasks"].includes(state.page) || !!emptyHome;
    byId("eiUsage").disabled = !state.friendId;
  }
  async function setPage(page) {
    notice(""); error("");
    state.page = page;
    for (const node of document.querySelectorAll("[data-page]")) {
      if (node.dataset.page === (page === "saved" ? "saved" : "tasks")) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    }
    if (page === "saved") await loadMessages();
    renderPage();
  }
  function empty(text, label, action) {
    const box = el("div", "ei-empty"); box.append(el("p", "", text));
    if (action) box.append(button(label, action, "ei-primary")); return box;
  }
  function renderPage() {
    // A redraw is also a way of leaving the bubble being edited.
    if (state.editing) finishEdit(true);
    closeMenu();
    const root = byId("eiContent"), chatScroll = byId("eiChat")?.scrollTop;
    root.replaceChildren();
    renderContext();
    // A started task is a fixed two-pane workspace; every other page scrolls normally.
    document.body.classList.toggle("ei-reviewing", hasScope() && state.page === "review" && !!state.batch && !materialsEditable(state.batch));
    if (!hasScope()) { root.append(empty("先在聊天列表选择一位好友。", "返回聊天", () => navigateApp("chat"))); return; }
    if (state.page === "tasks") renderTasks(root);
    else if (state.page === "review") renderReview(root, chatScroll);
    else renderSaved(root);
  }
  function renderTasks(root) {
    const batches = batchesForFriend(state.batches, state.friendId);
    if (!batches.length) {
      byId("eiAdd").hidden = true;
      root.append(empty("还没有导入任务", "导入聊天", showImport)); return;
    }
    for (const item of batches) {
      const row = el("div", "ei-task-row"), text = el("div"), actions = el("div", "ei-actions");
      text.append(el("h3", "", item.note || item.materials?.[0]?.name || `${sourceName(item.source)}聊天`));
      text.append(el("small", "", `${sourceName(item.source)} · ${statusText(item)}`));
      const pending = item.counts?.pending || 0;
      const saved = item.counts?.saved && !pending && !item.counts?.ready;
      actions.append(button(pending ? `核对 ${pending} 处疑点` : ACTIVE.has(item.state) ? "查看进度" : item.counts?.ready ? "查看并保存" : saved ? "查看聊天" : "查看", () => void run(async () => {
        if (saved) { state.messageQuery.batchId = item.id; await setPage("saved"); }
        else await openBatch(item.id);
      })));
      actions.append(button("删除", () => showDeleteTask(item), "ei-plain ei-delete", true));
      row.append(text, actions); root.append(row);
    }
  }
  function identityFields(values, onChange) {
    const grid = el("div", "ei-grid");
    grid.append(sourceInput(values.platform, value => onChange("platform", value.trim())).wrapper);
    for (const [key, label] of [["ownAccount", "我在来源软件的账号"], ["conversationKey", "来源会话"]]) {
      grid.append(input(label, values[key] || "", { required: true, onInput: value => onChange(key, value) }).wrapper);
    }
    return grid;
  }
  async function ensureFriend() {
    if (state.friendId) return;
    if (!linked.account || !linked.user) throw new Error("请先从好友的聊天页进入外部聊天。");
    const epoch = state.epoch;
    const result = await api("friends", { name: linked.name || linked.user, wechatAccount: linked.account, wechatUser: linked.user });
    if (epoch !== state.epoch) throw new Error("好友已切换，请重新开始导入。");
    const item = result.friend || result;
    state.friends.push(item); state.friendId = item.id; renderContext();
  }
  function showImport() {
    if (!hasScope() || state.busy) return;
    if (state.status.plugin?.enabled === false) { error("导入识别插件已停用。启用后可以继续，现有记录仍可浏览。"); return; }
    const values = { platform: "", note: "", direction: "forward" };
    let files = [];
    dialog("导入聊天", (body, actions, close) => {
      const platform = sourceInput("", value => {
        const name = value.trim();
        if (name !== values.platform) { values.platform = name; useSource(); }
        updateStart();
      });
      body.append(platform.wrapper);
      const drop = el("label", "ei-upload"), picker = el("input"); picker.type = "file"; picker.multiple = true;
      picker.accept = ".png,.jpg,.jpeg,.webp,.mp4,.mov,.webm"; picker.setAttribute("aria-label", "选择聊天截图或录屏");
      const fileLabel = el("strong", "", "选择截图或录屏"), hint = el("p", "ei-help", "截图可多选，录屏限一段、3 分钟内");
      drop.append(fileLabel, hint, picker); body.append(drop);
      const direction = select("录屏顺序", [["forward", "从较早消息滑到较新消息"], ["reverse", "从较新消息滑到较早消息"]], "forward", value => { values.direction = value; });
      direction.wrapper.hidden = true; body.append(direction.wrapper);
      const options = el("details", "ei-options"); options.append(el("summary", "", "更多选项"));
      const sourcePicker = select("来源会话", [], "", value => { values.sourceId = value; useSource(false); });
      const own = input("来源账号", "", { placeholder: "默认：本人" });
      const conversation = input("会话标识", "", { placeholder: "默认：当前好友" });
      const note = input("本次备注", "", { maxLength: 10000, placeholder: "可选" });
      options.append(sourcePicker.wrapper, own.wrapper, conversation.wrapper, note.wrapper); body.append(options);
      function useSource(reset = true) {
        const sources = state.sources.filter(source => source.platform === values.platform);
        if (reset) values.sourceId = sources[0]?.id || "";
        sourcePicker.node.replaceChildren();
        for (const [id, label] of [["", "新的来源会话"], ...sources.map((source, index) => [source.id, source.note || (source.ownAccount === "本人" ? `已有会话 ${index + 1}` : source.ownAccount)])]) {
          const option = el("option", "", label); option.value = id; sourcePicker.node.append(option);
        }
        sourcePicker.node.value = values.sourceId || ""; sourcePicker.wrapper.hidden = !sources.length;
        if (sources.length > 1) body.insertBefore(sourcePicker.wrapper, drop);
        else options.prepend(sourcePicker.wrapper);
        const source = sources.find(item => item.id === values.sourceId);
        own.node.value = source?.ownAccount || ""; conversation.node.value = source?.conversationKey || "";
        if (!reset && sources.length && !source) { options.open = true; conversation.node.placeholder = "给新的会话填写名称或标识"; }
      }
      useSource();
      const start = button("开始导入", () => void run(async () => {
        if (state.status.plugin?.enabled === false) throw new Error("请先启用导入识别插件。");
        if (!values.platform) throw new Error("请填写聊天来源名称。");
        const validation = validateFiles(files, [], state.status.limits);
        if (!files.length || validation) throw new Error(validation || "请选择截图或录屏。");
        if (!values.sourceId && state.sources.some(item => item.platform === values.platform) && !conversation.node.value.trim())
          throw new Error("请填写新会话的名称或标识，或选择已有会话追加。");
        const selectedFiles = [...files], source = state.sources.find(item => item.id === values.sourceId);
        const submission = { platform: values.platform, ownAccount: own.node.value.trim() || "本人",
          conversationKey: conversation.node.value.trim(), sourceNote: source?.note || "", note: note.node.value.trim(), direction: values.direction };
        await withPendingSubmission(async () => {
          await ensureFriend();
          const batch = await api("batches", { ...submission, friendId: state.friendId, conversationKey: submission.conversationKey || state.friendId });
          acceptBatch(batch); close(); await setPage("review");
          for (let index = 0; index < selectedFiles.length; index++) {
            notice(`正在添加素材 ${index + 1} / ${selectedFiles.length}`);
            acceptBatch(await api(`upload?batchId=${escId(batch.id)}&name=${escId(selectedFiles[index].name)}`, undefined, selectedFiles[index]));
          }
          await loadSources();
          acceptBatch(await api("start", { batchId: batch.id })); renderPage(); notice("已开始，可以关闭此页面");
        });
      }), "ei-primary", true);
      disable(start, true);
      function updateStart() { disable(start, !values.platform || !files.length || !!validateFiles(files, [], state.status.limits)); }
      const change = selected => {
        files = selected;
        const validation = validateFiles(files, [], state.status.limits);
        fileLabel.textContent = files.length === 1 ? files[0].name : files.length ? `已选择 ${files.length} 份素材` : "选择截图或录屏";
        hint.textContent = validation || "截图可多选，录屏限一段、3 分钟内";
        hint.classList.toggle("ei-warning", !!validation);
        direction.wrapper.hidden = !files.some(file => /\.(mp4|mov|webm)$/i.test(file.name));
        updateStart();
      };
      picker.addEventListener("change", () => change(Array.from(picker.files || [])));
      drop.addEventListener("dragover", event => { event.preventDefault(); drop.classList.add("is-dragover"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("is-dragover"));
      drop.addEventListener("drop", event => { event.preventDefault(); drop.classList.remove("is-dragover"); change(Array.from(event.dataTransfer?.files || [])); });
      actions.append(start);
    });
  }
  // Materials can change only until recognition first reads them.
  function materialsEditable(batch) {
    return !batch.archived && !ACTIVE.has(batch.state) && !batch.segments.length;
  }
  function materialRow(material, index) {
    const row = el("li", "ei-material"), info = el("div", "ei-material-info"), actions = el("div", "ei-actions");
    if (material.kind === "image" && material.available !== false) {
      const thumb = el("img", "ei-thumbnail"); thumb.alt = `素材 ${index + 1}：${material.name}`; thumb.loading = "lazy"; thumb.src = evidenceUrl(material.id, true); row.append(thumb);
    } else row.append(el("div", "ei-thumbnail ei-empty", material.kind === "video" ? "▶" : "图"));
    info.append(el("strong", "", material.name), el("small", "", `${material.kind === "video" ? "录屏" : "截图"} · ${formatBytes(material.size)}${material.available === false ? " · 副本已清理" : ""}`));
    row.append(info, actions);
    return { row, actions };
  }
  function renderMaterials(root) {
    const batch = state.batch, card = el("section", "ei-card"), heading = el("div", "ei-page-head");
    heading.append(el("h2", "", "截图与录屏"), el("span", "ei-muted", `${batch.materials.length} / 20 份素材`)); card.append(heading);
    const drop = el("div", "ei-upload"), picker = el("input"); picker.type = "file"; picker.multiple = true;
    picker.dataset.mutation = "true"; picker.disabled = state.busy;
    picker.accept = ".png,.jpg,.jpeg,.webp,.mp4,.mov,.webm"; picker.setAttribute("aria-label", "选择聊天截图或录屏");
    picker.addEventListener("change", () => void uploadFiles(Array.from(picker.files || [])));
    drop.append(el("strong", "", "选择截图或录屏"), picker); card.append(drop);
    const list = el("ol", "ei-materials");
    batch.materials.forEach((material, index) => {
      const { row, actions } = materialRow(material, index);
      actions.append(button("查看", () => showMaterial(material), "ei-plain"));
      if (index > 0) actions.append(button("↑", () => void reorderMaterial(index, -1), "ei-plain", true));
      if (index + 1 < batch.materials.length) actions.append(button("↓", () => void reorderMaterial(index, 1), "ei-plain", true));
      actions.append(button("移除", () => void run(async () => { acceptBatch(await api("remove-material", { batchId: batch.id, materialId: material.id })); renderPage(); }), "ei-plain", true));
      actions.querySelectorAll("button").forEach(node => { if (node.textContent === "↑" || node.textContent === "↓") node.setAttribute("aria-label", `${node.textContent === "↑" ? "上移" : "下移"}${material.name}`); });
      list.append(row);
    }); card.append(list);
    if (batch.materials.some(item => item.kind === "video")) {
      card.append(select("录屏顺序", [["forward", "从较早消息滑到较新消息"], ["reverse", "从较新消息滑到较早消息"]], batch.direction || "forward", value => void run(async () => {
        acceptBatch(await api("direction", { batchId: batch.id, direction: value })); renderPage();
      })).wrapper);
    }
    root.append(card);
  }
  function showMaterials() {
    const materials = state.batch?.materials || [];
    if (materials.length === 1) { showMaterial(materials[0]); return; }
    dialog("截图与录屏", (body, actions, close) => {
      const list = el("ol", "ei-materials");
      materials.forEach((material, index) => {
        const item = materialRow(material, index);
        item.actions.append(button("查看", () => { close(); showMaterial(material); }, "ei-plain")); list.append(item.row);
      });
      body.append(list);
    });
  }
  function renderNotes(root) {
    const batch = state.batch, details = el("div", "ei-note-fields");
    const source = input("来源会话说明", batch.source?.note || "", { multiline: true }), note = input("本批备注", batch.note || "", { multiline: true });
    details.append(source.wrapper, note.wrapper, button("保存说明", () => void run(async () => {
      acceptBatch(await api("notes", { batchId: batch.id, note: note.node.value, sourceNote: source.node.value }));
      notice("已保存");
    }), "", true)); root.append(details);
  }
  async function uploadFiles(files) {
    if (!files.length || !state.batch) return;
    const batchId = state.batch.id;
    const validation = validateFiles(files, state.batch.materials, state.status.limits);
    if (validation) { error(validation); return; }
    await run(() => withPendingSubmission(async () => {
      for (let index = 0; index < files.length; index++) {
        notice(`正在保存素材 ${index + 1} / ${files.length}：${files[index].name}`);
        acceptBatch(await api(`upload?batchId=${escId(batchId)}&name=${escId(files[index].name)}`, undefined, files[index]));
      }
      renderPage(); notice(`已添加 ${files.length} 份素材`);
    }));
  }
  async function reorderMaterial(index, change) {
    await run(async () => {
      const ids = state.batch.materials.map(item => item.id); [ids[index], ids[index + change]] = [ids[index + change], ids[index]];
      acceptBatch(await api("reorder", { batchId: state.batch.id, ids })); renderPage();
    });
  }
  function renderProcessing(root) {
    const region = el("div"); region.id = "eiProcessing"; root.append(region); updateProcessing();
  }
  // One line above the review: progress while recognizing, or why it stopped and how to go on.
  // It disappears once every frame has been read.
  function updateProcessing() {
    const region = byId("eiProcessing"), batch = state.batch;
    if (!region || !batch) return;
    region.replaceChildren();
    const { done, total } = segmentProgress(batch), active = ACTIVE.has(batch.state);
    const halted = ["stopped", "interrupted", "failed"].includes(batch.state);
    if (!active && !halted && total && batch.state !== "draft") return;
    const line = el("div", `ei-status${halted ? " warn" : ""}`);
    if (active || halted) line.append(el("span", "", halted ? batch.error || statusText(batch) : `${statusText(batch)}${total ? ` ${done}/${total}` : ""}`));
    if (active || halted && done < total) {
      const progress = el("progress", "ei-progress"); progress.setAttribute("aria-label", "已识别的画面");
      // Before frames are extracted the total is unknown, so the bar only shows activity.
      if (total) { progress.max = total; progress.value = done; }
      line.append(progress);
    }
    if (active) line.append(button("停止", () => taskAction("stop"), "", true));
    else {
      const start = button(halted ? "继续未完成的部分" : "开始后台整理", () => taskAction("start"), "ei-primary", true);
      disable(start, batch.archived || !batch.materials.length || state.status.plugin?.enabled === false);
      line.append(start);
    }
    region.append(line);
    if (state.status.plugin?.enabled === false) region.append(el("p", "ei-warning", "导入识别插件已停用，启用后可以继续。"));
    if (state.status.model?.available === false || state.status.model?.supportsVision === false) region.append(el("p", "ei-warning", state.status.model?.message || "主模型尚未就绪，请检查模型设置及图片能力。"));
    if (!batch.candidates.length) showEvidence();
  }
  function taskAction(action) {
    return run(async () => {
      const request = () => api(action, { batchId: state.batch.id });
      acceptBatch(await (action === "start" ? withPendingSubmission(request) : request())); renderPage();
      notice(action === "stop" ? "已停止，进度已保留" : "已开始，可以关闭此页面");
    });
  }
  function segmentProgress(batch) {
    const segments = batch?.segments || [];
    return { done: segments.filter(item => ["done", "completed", "recognized"].includes(item.state)).length, total: segments.length };
  }
  // A task has a single page: recognition progress on top, and the review that fills in
  // below it as frames are read.
  function renderReview(root, chatScroll) {
    const batch = state.batch;
    if (!batch) { root.append(empty("请选择需要核对的导入任务。", "查看导入任务", () => void setPage("tasks"))); return; }
    const editable = materialsEditable(batch), reveal = state.reveal || chatScroll == null;
    const bar = el("div", "ei-review-bar"), back = button("‹", () => void setPage("tasks"), "ei-plain ei-icon");
    back.setAttribute("aria-label", "返回导入任务"); bar.append(back);
    if (batch.candidates.length) {
      const filters = el("div", "ei-segment"); filters.id = "eiFilters";
      for (const key of ["all", "issues"]) {
        const filter = button("", () => { state.filter = key; state.reveal = true; renderPage(); });
        filter.dataset.filter = key; filter.setAttribute("aria-pressed", String(state.filter === key)); filters.append(filter);
      }
      bar.append(filters);
    }
    const tools = el("div", "ei-actions ei-review-tools");
    if (!editable) tools.append(button("素材", showMaterials, "ei-plain"));
    tools.append(button("更多", showLifecycle, "ei-plain"));
    if (batch.candidates.length) { const save = button("保存", () => void saveBatch(), "ei-primary", true); save.id = "eiSave"; tools.append(save); }
    bar.append(tools); root.append(bar);
    updateReviewBar();
    renderProcessing(root);
    for (const warning of batchWarnings(batch)) root.append(el("p", "ei-warning", ISSUE[warning]));
    if (editable) { renderMaterials(root); return; }
    const visible = reviewVisible(batch.candidates, state.filter);
    if (reveal) state.focused = visible.find(candidatePending)?.id || state.focused;
    if (!visible.some(item => item.id === state.focused)) { state.focused = visible[0]?.id || ""; state.frame = 0; }
    const layout = el("div", "ei-review"), evidence = el("section", "ei-evidence"), chat = el("section", "ei-chat");
    evidence.id = "eiEvidence"; chat.id = "eiChat"; chat.setAttribute("aria-label", "识别记录");
    layout.append(evidence, chat); root.append(layout);
    renderChat(chat, visible);
    if (!visible.length) chat.append(empty(batch.candidates.length ? "没有待核对的疑点" : ACTIVE.has(batch.state) ? "识别出的消息会陆续显示在这里" : "还没有识别出消息"));
    showEvidence();
    if (chatScroll != null) chat.scrollTop = chatScroll;
    if (reveal) revealRow(state.focused, state.reveal === "smooth");
    state.reveal = false;
    chat.addEventListener("scroll", () => { closeMenu(); if (!state.scrollFrame) state.scrollFrame = requestAnimationFrame(syncFocus); });
  }
  function updateReviewBar() {
    const total = counts(), save = byId("eiSave");
    for (const node of byId("eiFilters")?.children || [])
      node.textContent = node.dataset.filter === "all" ? `全部 ${total.total - total.ignored}` : `疑点 ${total.pending}`;
    if (save) { save.textContent = total.ready ? `保存 ${total.ready} 条` : "保存"; disable(save, !total.ready || ACTIVE.has(state.batch?.state)); }
  }
  function otherName() { return state.batch?.friend?.name || friend()?.name || "对方"; }
  function candidateById(id) { return state.batch?.candidates.find(item => item.id === id); }
  function chatRow(sender, kind) {
    return el("div", `ei-chat-row${kind === "system" ? " system" : sender === "self" ? " self" : ""}`);
  }
  // A picture message shows the picture cut from its frame; the text label is the fallback.
  function bubbleButton(text, picture, action) {
    const bubble = button(picture ? "" : text || "—", action, "ei-bubble");
    if (picture) {
      const image = el("img"); image.alt = text || "图片"; image.src = picture; image.draggable = false;
      image.addEventListener("error", () => { bubble.classList.remove("ei-picture"); bubble.textContent = text || "[图片]"; });
      bubble.classList.add("ei-picture"); bubble.append(image);
    }
    return bubble;
  }
  function renderChat(chat, visible) {
    const list = el("div", "ei-chat-list");
    let date = "";
    state.rows = new Map();
    for (const candidate of visible) {
      if (candidate.dateLabel && candidate.dateLabel !== date) list.append(el("div", "ei-chat-time", candidate.dateLabel));
      date = candidate.dateLabel || date;
      const row = candidateRow(candidate); state.rows.set(candidate.id, row); list.append(row);
    }
    list.addEventListener("dragover", event => {
      if (!state.dragging) return;
      event.preventDefault(); event.dataTransfer.dropEffect = "move";
      markDrop(dropBefore(event.clientY));
    });
    list.addEventListener("drop", event => {
      if (!state.dragging) return;
      event.preventDefault();
      const id = state.dragging, before = dropBefore(event.clientY);
      markDrop(undefined); moveCandidate(id, before);
    });
    chat.replaceChildren(list);
  }
  // The bubble lands before the first other row whose middle is below the pointer.
  function dropBefore(y) {
    for (const [id, row] of state.rows) {
      if (id === state.dragging) continue;
      const rect = row.getBoundingClientRect();
      if (y < rect.top + rect.height / 2) return id;
    }
    return null;
  }
  function markDrop(before) {
    for (const row of state.rows.values()) row.classList.remove("drop-before", "drop-after");
    if (before === undefined) return;
    const rest = [...state.rows.keys()].filter(id => id !== state.dragging);
    if (before) state.rows.get(before)?.classList.add("drop-before");
    else state.rows.get(rest[rest.length - 1])?.classList.add("drop-after");
  }
  function moveCandidate(id, before) {
    const visible = [...state.rows.keys()], rest = visible.filter(item => item !== id);
    const at = before ? rest.indexOf(before) : rest.length;
    if ([...rest.slice(0, at), id, ...rest.slice(at)].join() === visible.join()) return;
    // The server places the bubble right after another one; hidden bubbles keep their places.
    const candidates = state.batch.candidates, moving = candidateById(id);
    const others = candidates.filter(item => item !== moving);
    const after = before ? others[others.findIndex(item => item.id === before) - 1]?.id ?? null : rest[rest.length - 1] ?? null;
    candidates.splice(candidates.indexOf(moving), 1);
    candidates.splice(after ? candidates.findIndex(item => item.id === after) + 1 : 0, 0, moving);
    const entry = recordUndo(id, "move"), batchId = state.batch.id;
    state.focused = id; renderPage();
    queue(async () => {
      entry.before = candidateById(id)?.version || 0;
      try { acceptBatch(await api("move-candidate", { batchId, candidateId: id, after })); }
      catch (problem) { acceptBatch(await api(`batch?id=${escId(batchId)}`)); renderPage(); throw problem; }
      finally { settleUndo(entry); }
    });
  }
  // An open doubt is simply a yellow bubble; its reasons are left to the tooltip.
  function candidateRow(candidate) {
    const row = chatRow(candidate.sender, candidate.kind), doubt = candidatePending(candidate);
    row.dataset.id = candidate.id;
    if (doubt) row.classList.add("is-doubt");
    if (candidate.id === state.focused) row.classList.add("is-focused");
    const bubble = bubbleButton(candidate.text, candidate.kind === "image" && pictureUrl("candidate", candidate.id), () => beginEdit(candidate.id));
    if (doubt) bubble.title = reviewIssues(candidate).join("、");
    bubble.addEventListener("contextmenu", event => openMenu(event, candidate.id));
    row.append(bubble);
    // An unsaved bubble can be dragged to where it really belongs.
    if (!candidate.savedMessageId) {
      row.draggable = true;
      row.addEventListener("dragstart", event => {
        if (state.editing === candidate.id || ACTIVE.has(state.batch?.state)) { event.preventDefault(); return; }
        if (state.editing) finishEdit(true);
        closeMenu(); state.dragging = candidate.id;
        event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", candidate.text || "");
        row.classList.add("is-dragging");
      });
      row.addEventListener("dragend", () => { row.classList.remove("is-dragging"); markDrop(undefined); state.dragging = ""; });
    }
    if (candidate.quote) row.append(el("div", "ei-quote", candidate.quote));
    return row;
  }
  function replaceRow(id, values) {
    const old = state.rows.get(id), candidate = candidateById(id);
    if (!old?.isConnected || state.editing === id) return;
    if (!candidate || candidate.ignored || candidate.autoMergedInto) { removeRow(id); return; }
    const row = candidateRow(values ? { ...candidate, ...values } : candidate);
    old.replaceWith(row); state.rows.set(id, row);
  }
  function removeRow(id) {
    const row = state.rows.get(id);
    state.rows.delete(id);
    if (!row) return;
    const before = row.previousElementSibling, after = row.nextElementSibling;
    row.remove();
    // A date line left with no message under it goes too.
    if (before?.classList.contains("ei-chat-time") && (!after || after.classList.contains("ei-chat-time"))) before.remove();
    if (state.focused === id) { state.focused = ""; syncFocus(); }
  }
  // Bubbles being edited size to their text like the bubble itself.
  const FIELD_SIZING = typeof CSS !== "undefined" && !!CSS.supports?.("field-sizing", "content");
  function fitText(text) {
    if (FIELD_SIZING) return;
    text.style.height = "auto"; text.style.height = `${text.scrollHeight}px`;
  }
  function segment(options, value, onChange, label) {
    const group = el("div", "ei-segment ei-segment-small"); group.setAttribute("role", "group");
    if (label) group.setAttribute("aria-label", label);
    for (const [key, text] of options) {
      const item = button(text, () => { for (const node of group.children) node.setAttribute("aria-pressed", String(node === item)); onChange(key); });
      item.setAttribute("aria-pressed", String(key === value)); group.append(item);
    }
    return group;
  }
  // A click edits the bubble where it stands; leaving it, edited or not, confirms it.
  function beginEdit(id) {
    if (state.editing === id) return;
    if (state.editing) finishEdit(true);
    const candidate = candidateById(id), row = state.rows.get(id);
    if (!candidate || !row) return;
    closeMenu(); state.editing = id; state.editSender = ""; focusCandidate(id);
    row.classList.add("is-editing"); row.draggable = false;
    let target = row.querySelector(".ei-bubble");
    if (candidate.kind !== "image") {
      const editor = el("textarea", "ei-bubble ei-bubble-edit"); editor.value = candidate.text || ""; editor.rows = 1;
      editor.maxLength = 30000; editor.setAttribute("aria-label", "消息正文");
      editor.addEventListener("input", () => fitText(editor));
      editor.addEventListener("contextmenu", event => openMenu(event, id));
      target.replaceWith(editor); target = editor; fitText(editor);
    }
    if (!senderKnown(candidate) && candidate.kind !== "system") {
      row.append(segment([["other", otherName()], ["self", "我"]], "", value => {
        state.editSender = value; row.classList.toggle("self", value === "self");
      }, "发送者"));
    }
    row.addEventListener("keydown", event => {
      if (event.isComposing) return;
      if (event.key === "Enter" && !event.shiftKey && event.target.tagName === "TEXTAREA") { event.preventDefault(); finishEdit(true); }
      else if (event.key === "Escape") { event.preventDefault(); finishEdit(false); }
    });
    // Switching to another window is not leaving the bubble.
    row.addEventListener("focusout", () => setTimeout(() => {
      if (state.editing === id && document.hasFocus() && !row.contains(document.activeElement) && !state.menu) finishEdit(true);
    }, 0));
    target.focus({ preventScroll: true });
    if (target.setSelectionRange) target.setSelectionRange(target.value.length, target.value.length);
  }
  function editingValues() {
    const candidate = candidateById(state.editing), editor = state.rows.get(state.editing)?.querySelector("textarea");
    if (!candidate) return null;
    return { ...mergeDraft(candidate, null), ...(editor ? { text: editor.value } : {}), ...(state.editSender ? { sender: state.editSender } : {}) };
  }
  function editingChanged() {
    const values = state.editing && editingValues();
    return !!values && draftChanged(candidateById(state.editing), values);
  }
  function finishEdit(confirm) {
    const id = state.editing, values = editingValues();
    state.editing = ""; state.editSender = "";
    if (!id) return;
    if (confirm && values) commit(id, values); else replaceRow(id);
  }
  function commit(id, values) {
    const candidate = candidateById(id);
    if (!candidate) return;
    const changed = draftChanged(candidate, values), batchId = state.batch.id;
    if (candidate.savedMessageId) {
      if (!changed && !candidate.recognitionSuggestion) { replaceRow(id); return; }
      replaceRow(id, values);
      queue(async () => {
        if (changed) await api("correct-message", { messageId: candidate.savedMessageId,
          patch: reviewPatch(candidate, { text: values.text, sender: values.sender, dateLabel: values.dateLabel }) });
        acceptBatch(candidate.recognitionSuggestion
          ? await api("candidate", { batchId, candidateId: id, patch: { duplicateDecision: "reuse" }, resolve: true })
          : await api(`batch?id=${escId(batchId)}`));
      }, id);
      return;
    }
    if (!changed && candidateReady(candidate)) { replaceRow(id); return; }
    const blocker = /[□�]/.test(values.text) || !values.text.trim() ? "把 □ 改成看清的字后，再离开气泡即可确认。"
      : !senderKnown({ ...candidate, sender: values.sender }) ? "右键气泡，选择是谁发的。" : "";
    replaceRow(id, values);
    const entry = recordUndo(id, "edit");
    queue(async () => {
      entry.before = candidateById(id)?.version || 0;
      const request = resolve => api("candidate", { batchId, candidateId: id,
        patch: resolve ? confirmPatch(candidate, values) : reviewPatch(candidate, values), resolve });
      try {
        if (blocker) {
          if (changed) acceptBatch(await request(false));
          throw new Error(blocker);
        }
        try { acceptBatch(await request(true)); }
        catch (problem) {
          // A bubble that cannot be confirmed yet still keeps the user's edit.
          if (changed) acceptBatch(await request(false));
          throw problem;
        }
      } finally { settleUndo(entry); }
    }, id);
  }
  // Deleting, editing or moving an unsaved bubble can be taken back, newest first (Ctrl+Z). An
  // entry is placed at once so a quick Ctrl+Z finds it; its versions are known once the change lands.
  function recordUndo(id, kind) {
    const entry = { batchId: state.batch.id, id, kind, before: 0, after: 0 };
    state.undo.push(entry);
    if (state.undo.length > 100) state.undo.shift();
    return entry;
  }
  function settleUndo(entry) {
    entry.after = candidateById(entry.id)?.version || 0;
    if (!(entry.after > entry.before)) state.undo = state.undo.filter(item => item !== entry);
  }
  function undo(entry = state.undo.findLast(item => item.batchId === state.batch?.id)) {
    // Inside a bubble being edited, the first Ctrl+Z gives up the unfinished edit.
    if (state.editing) { finishEdit(false); return; }
    if (!entry) return;
    state.undo = state.undo.filter(item => item !== entry);
    byId("eiUndo")?.remove();
    queue(async () => {
      if (!(entry.after > entry.before) || entry.batchId !== state.batch?.id) return;
      acceptBatch(await api("restore-candidate", { batchId: entry.batchId, candidateId: entry.id, version: entry.before, expected: entry.after }));
      state.focused = entry.id; renderPage();
      state.rows.get(entry.id)?.scrollIntoView({ block: "nearest" });
      notice({ delete: "已撤销删除", edit: "已撤销修改", move: "已撤销移动" }[entry.kind]);
    });
  }
  // Bubble changes are sent one after another without blocking the next click.
  function queue(action, id) {
    const epoch = state.epoch;
    state.pendingEdits++;
    state.chain = state.chain.then(async () => {
      if (epoch !== state.epoch) return;
      try { await action(); error(""); }
      catch (problem) { if (epoch === state.epoch) error(problem?.message || "操作未完成，请重试。"); }
      finally { if (id) replaceRow(id); updateReviewBar(); }
    }).finally(() => { state.pendingEdits--; });
    return state.chain;
  }
  function deleteCandidate(id) {
    const candidate = candidateById(id);
    if (!candidate || candidate.savedMessageId) return;
    if (state.editing === id) { state.editing = ""; state.editSender = ""; }
    removeRow(id);
    const entry = recordUndo(id, "delete");
    queue(async () => {
      entry.before = candidateById(id)?.version || 0;
      try { acceptBatch(await api("candidate", { batchId: state.batch.id, candidateId: id, patch: { ignored: true }, resolve: false })); }
      catch (problem) { renderPage(); throw problem; }
      finally { settleUndo(entry); }
      if (state.undo.includes(entry)) showUndo(entry);
    });
  }
  function showUndo(entry) {
    byId("eiUndo")?.remove(); clearTimeout(state.undoTimer);
    const toast = el("div", "ei-toast"); toast.id = "eiUndo"; toast.setAttribute("role", "status");
    const take = button("撤销", () => undo(entry), "ei-plain"); take.title = "Ctrl+Z";
    toast.append(el("span", "", "已删除"), take);
    document.body.append(toast);
    state.undoTimer = setTimeout(() => toast.remove(), 6000);
  }
  function confirmFromMenu(id, sender) {
    if (state.editing === id) { if (sender) state.editSender = sender; finishEdit(true); return; }
    if (state.editing) finishEdit(true);
    const candidate = candidateById(id);
    if (candidate) commit(id, { ...mergeDraft(candidate, null), ...(sender ? { sender } : {}) });
  }
  function openMenu(event, id) {
    event.preventDefault(); closeMenu();
    const candidate = candidateById(id), items = [];
    if (!candidate) return;
    if (candidate.savedMessageId) {
      if (candidate.recognitionSuggestion) items.push(["确认", () => confirmFromMenu(id)]);
    } else {
      if (!senderKnown(candidate) && candidate.kind !== "system")
        items.push(["我发的", () => confirmFromMenu(id, "self")], [`${otherName()}发的`, () => confirmFromMenu(id, "other")]);
      else if (candidatePending(candidate) || state.editing === id) items.push(["确认", () => confirmFromMenu(id)]);
      items.push(["删除", () => deleteCandidate(id)]);
    }
    if (!items.length) return;
    const menu = el("div", "ei-menu-pop"); menu.setAttribute("role", "menu");
    for (const [label, action] of items) {
      const item = button(label, () => { closeMenu(); action(); }, label === "删除" ? "ei-danger" : "");
      item.setAttribute("role", "menuitem");
      // Choosing an item must not count as leaving the bubble being edited.
      item.addEventListener("mousedown", down => down.preventDefault());
      menu.append(item);
    }
    document.body.append(menu); state.menu = menu;
    menu.style.left = `${Math.max(4, Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 4))}px`;
  }
  function closeMenu() { state.menu?.remove(); state.menu = null; }
  function revealRow(id, smooth) { state.rows.get(id)?.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "auto" }); }
  // The evidence follows a reading line that travels with the scroll position, so every
  // message reaches it; a bubble being edited that is still on screen keeps its own frame.
  function syncFocus() {
    state.scrollFrame = 0;
    const chat = byId("eiChat");
    if (!chat || !state.rows.size) return;
    const box = chat.getBoundingClientRect(), range = chat.scrollHeight - chat.clientHeight;
    const line = box.top + box.height * (range > 0 ? chat.scrollTop / range : 0);
    let target = "", distance = Infinity, editingVisible = false;
    for (const [id, row] of state.rows) {
      const rect = row.getBoundingClientRect();
      if (id === state.editing && rect.bottom > box.top && rect.top < box.bottom) editingVisible = true;
      const gap = Math.max(rect.top - line, line - rect.bottom, 0);
      if (gap < distance) { distance = gap; target = id; }
    }
    focusCandidate(editingVisible ? state.editing : target);
  }
  function focusCandidate(id) {
    if (!id || id === state.focused) return;
    state.rows.get(state.focused)?.classList.remove("is-focused");
    state.focused = id; state.frame = 0;
    state.rows.get(id)?.classList.add("is-focused");
    showEvidence();
  }
  function saveBatch() {
    if (state.editing) finishEdit(true);
    return run(async () => {
      await state.chain;
      const ids = state.batch.candidates.filter(candidateReady).map(item => item.id);
      if (!ids.length) { renderPage(); return; }
      const result = await api("save", { batchId: state.batch.id, candidateIds: ids }); acceptBatch(result);
      const left = counts().pending;
      if (left) renderPage();
      else {
        state.messageQuery = { q: "", batchId: state.batch.id, platform: state.batch.source.platform };
        await setPage("saved");
      }
      notice(`已保存 ${(result.added || 0) + (result.reused || 0)} 条${left ? `，还有 ${left} 处待核对` : ""}`);
    });
  }
  function showEvidence() {
    const root = byId("eiEvidence"), batch = state.batch;
    if (!root || !batch) return;
    const candidate = batch.candidates.find(item => item.id === state.focused);
    // Before any message is recognized, the frame being read shows what is happening.
    const latest = !batch.candidates.length && (batch.segments.find(item => item.state === "running" && item.evidenceAvailable) ||
      batch.segments.findLast(item => item.evidenceAvailable));
    const frames = candidate ? evidenceFrames(candidate, batch.segments) : latest ? [{ segment: latest, bbox: null }] : [];
    const index = Math.min(state.frame, Math.max(frames.length - 1, 0)), current = frames[index];
    const segment = current?.segment, material = batch.materials.find(item => item.id === segment?.materialId);
    let stage = root.querySelector(".ei-evidence-stage");
    if (stage && stage.dataset.segment === (segment?.id || "")) {
      root.replaceChildren(stage); setBox(stage.querySelector(".ei-evidence-picture"), current?.bbox);
    } else {
      state.fitObserver?.disconnect();
      stage = el("div", "ei-evidence-stage"); stage.dataset.segment = segment?.id || "";
      if (segment?.evidenceAvailable) {
        const picture = evidenceImage(segment.id, current.bbox, "对应的原始画面");
        if (material?.available) {
          picture.classList.add("is-zoomable"); picture.title = material.kind === "video" ? "打开录屏" : "查看原图";
          picture.addEventListener("click", () => showMaterial(material, segment.videoMs));
        }
        stage.append(picture); fitPicture(stage, picture);
      } else if (candidate) stage.append(empty("画面已清理"));
      root.replaceChildren(stage);
    }
    if (!segment) return;
    const caption = el("div", "ei-evidence-caption");
    caption.append(el("span", "", `${material?.name || "素材"}${segment.videoMs != null ? ` · ${(segment.videoMs / 1000).toFixed(1)} 秒` : ""}`));
    if (frames.length > 1) {
      const pager = el("span", "ei-pager"), turn = step => { state.frame = (index + step + frames.length) % frames.length; showEvidence(); };
      const previous = button("‹", () => turn(-1), "ei-plain"), next = button("›", () => turn(1), "ei-plain");
      previous.setAttribute("aria-label", "上一个画面"); next.setAttribute("aria-label", "下一个画面");
      pager.append(previous, el("span", "", `${index + 1}/${frames.length}`), next); caption.append(pager);
    }
    root.append(caption);
    const notes = frameWarnings(segment);
    if (notes.length) { const list = el("ul", "ei-frame-notes"); for (const note of notes) list.append(el("li", "", note)); root.append(list); }
  }
  function fitPicture(stage, picture) {
    const image = picture.querySelector("img");
    const fit = () => {
      if (!image?.naturalWidth || !stage.clientWidth || !stage.clientHeight) return;
      const scale = Math.min(stage.clientWidth / image.naturalWidth, stage.clientHeight / image.naturalHeight);
      picture.style.width = `${Math.floor(image.naturalWidth * scale)}px`; picture.style.height = `${Math.floor(image.naturalHeight * scale)}px`;
    };
    image?.addEventListener("load", fit);
    if (typeof ResizeObserver === "function") { state.fitObserver = new ResizeObserver(fit); state.fitObserver.observe(stage); }
    fit();
  }
  function evidenceImage(id, bbox, alt) {
    const picture = el("div", "ei-evidence-picture"), image = el("img"); image.alt = alt; image.src = evidenceUrl(id);
    image.addEventListener("error", () => picture.replaceChildren(empty("这份证据画面暂时无法读取。"))); picture.append(image);
    setBox(picture, bbox);
    return picture;
  }
  function setBox(picture, bbox) {
    if (!picture) return;
    picture.querySelector("svg")?.remove();
    if (!validBox(bbox)) return;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"), rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    svg.setAttribute("viewBox", "0 0 1000 1000"); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("aria-label", "所选气泡区域");
    ["x", "y", "width", "height"].forEach((name, index) => rect.setAttribute(name, String(bbox[index] * 1000)));
    svg.append(rect); picture.append(svg);
  }
  function dialog(title, build) {
    const modal = byId("eiDialog"); modal.replaceChildren();
    const heading = el("h2", "", title); heading.id = "eiDialogTitle"; modal.append(heading);
    const body = el("div"), actions = el("div", "ei-actions"); modal.append(body, actions);
    const close = () => modal.close(); build(body, actions, close);
    actions.prepend(button("取消", close)); modal.showModal();
  }
  async function loadMessages() {
    if (!state.friendId) { state.messages = { messages: [], gaps: [] }; return; }
    const epoch = state.epoch, fixedFriend = state.friendId, query = new URLSearchParams({ friendId: fixedFriend, ...state.messageQuery });
    const result = await api(`messages?${query}`);
    if (epoch === state.epoch && fixedFriend === state.friendId) state.messages = result;
  }
  function renderSaved(root) {
    const messages = state.messages.messages || [], gaps = state.messages.gaps || [];
    const filtered = !!(state.messageQuery.q || state.messageQuery.platform || state.messageQuery.batchId);
    if (!messages.length && !gaps.length && !filtered) {
      byId("eiAdd").hidden = true;
      const blank = empty("还没有外部聊天", "导入聊天", showImport);
      blank.classList.add("ei-welcome");
      root.append(blank); return;
    }
    const bar = el("div", "ei-saved-toolbar"), sources = el("div", "ei-source-tabs");
    const platforms = [...new Set(state.sources.map(source => source.platform))];
    for (const platform of platforms.length > 1 ? ["", ...platforms] : []) {
      const tab = button(platform || "全部来源", () => void run(async () => {
        state.messageQuery = { q: "", batchId: "", platform }; await loadMessages(); renderPage();
      }), "ei-plain"); tab.setAttribute("aria-pressed", String(state.messageQuery.platform === platform || !state.messageQuery.platform && platforms.length === 1)); sources.append(tab);
    }
    bar.append(sources, button(state.searching ? "收起搜索" : "搜索", () => { state.searching = !state.searching; renderPage(); }, "ei-plain")); root.append(bar);
    if (state.searching || state.messageQuery.q) {
      const form = el("form", "ei-search"), search = el("input"); search.type = "search"; search.value = state.messageQuery.q;
      search.placeholder = "搜索聊天记录"; search.maxLength = 256; search.setAttribute("aria-label", "搜索聊天记录");
      form.addEventListener("submit", event => { event.preventDefault(); state.messageQuery.q = search.value.trim(); void run(async () => { await loadMessages(); renderPage(); }); });
      const submit = el("button", "", "搜索"); submit.type = "submit"; form.append(search, submit); root.append(form);
    }
    if (state.messageQuery.batchId) root.append(button("显示全部聊天", () => void run(async () => { state.messageQuery.batchId = ""; await loadMessages(); renderPage(); }), "ei-plain"));
    if (!messages.length && !gaps.length) {
      root.append(empty("没有找到符合条件的聊天记录。")); return;
    }
    const rows = coalesceGaps(orderWithGaps(messages, state.messageQuery.q ? [] : gaps)), list = el("div", "ei-chat-list");
    let sourceId = "", date = "";
    root.append(list);
    for (const row of rows) {
      const item = row.value;
      if (item.sourceId !== sourceId) {
        sourceId = item.sourceId;
        const source = (state.messages.sources || state.sources).find(value => value.id === sourceId);
        if (platforms.length > 1 && !state.messageQuery.platform) list.append(el("h2", "ei-source-heading", sourceName(source || { platform: item.platform })));
        if (source?.note) list.append(el("div", "ei-chat-time", source.note));
      }
      if (row.kind === "gap") {
        const gap = el("div", "ei-gap"); gap.append(el("span", "", `${row.count > 1 ? `${row.count} 条聊天` : "有聊天"}尚未保存`));
        if (!item.archived) gap.append(button("核对", () => void run(() => openBatch(item.batchId)), "ei-plain")); list.append(gap); continue;
      }
      if (item.dateLabel && item.dateLabel !== date) list.append(el("div", "ei-chat-time", item.dateLabel));
      date = item.dateLabel || date;
      const message = chatRow(item.sender, item.kind);
      const bubble = bubbleButton(item.text, item.kind === "image" && pictureUrl("message", item.id), () => showMessageEvidence(item));
      bubble.title = "查看来源"; message.append(bubble);
      if (item.quote) message.append(el("div", "ei-quote", item.quote));
      if (item.orderUncertain) message.append(el("div", "ei-chat-note warn", "顺序不确定"));
      list.append(message);
    }
  }
  function showMaterial(material, videoMs) {
    dialog(material.name || "原素材", (body, actions) => {
      const frame = el("div", "ei-evidence-frame");
      if (material.available === false) frame.append(empty("原素材副本已清理，无法完整回放或重新抽取画面。"));
      else {
        const media = el(material.kind === "video" ? "video" : "img"); media.src = evidenceUrl(material.id, true);
        if (material.kind === "video") { media.controls = true; media.preload = "metadata"; if (videoMs != null) media.addEventListener("loadedmetadata", () => { media.currentTime = videoMs / 1000; }, { once: true }); }
        else media.alt = "本机保存的原始聊天截图";
        frame.append(media);
      }
      body.append(frame); actions.append(button("关闭", () => byId("eiDialog").close()));
    });
  }
  function showMessageEvidence(message) {
    dialog("来源", (body, actions, close) => {
      actions.append(button("更正", () => { close(); showCorrection(message); }));
      const batchId = message.batchIds?.find(id => state.batches.some(batch => batch.id === id));
      if (batchId) actions.append(button("查看导入任务", () => { close(); void run(() => openBatch(batchId)); }));
      const evidence = message.evidence || [];
      if (!evidence.length) { body.append(el("p", "ei-muted", "画面已清理")); return; }
      const picker = select("画面", evidence.map((item, index) => [String(index), `画面 ${index + 1}${item.videoMs != null ? ` · ${(item.videoMs / 1000).toFixed(1)} 秒` : ""}`]), "0");
      const frame = el("div", "ei-evidence-frame");
      const update = () => {
        const item = evidence[Number(picker.node.value)]; frame.replaceChildren();
        if (item.evidenceAvailable) frame.append(evidenceImage(item.segmentId, item.bbox, "已保存记录对应的原证据画面"));
        else frame.append(empty("画面已清理"));
      };
      picker.wrapper.hidden = evidence.length < 2;
      picker.node.addEventListener("change", update); body.append(picker.wrapper, frame); update();
    });
  }
  function showCorrection(message) {
    dialog("更正记录", (body, actions, close) => {
      const text = input("转录正文", message.text, { multiline: true, maxLength: 30000 }),
        sender = select("发送者", [...(message.kind === "system" ? [["unknown", "系统通知"]] : []), ["self", "我"], ["other", friend()?.name || "对方"]], message.sender),
        date = input("可见日期或时间", message.dateLabel || "", { placeholder: "不明确时留空" });
      body.append(text.wrapper, sender.wrapper, date.wrapper);
      actions.append(button("保存更正", () => void run(async () => {
        if (!text.node.value.trim()) throw new Error("正文不能为空。");
        await api("correct-message", { messageId: message.id, patch: reviewPatch(message, { text: text.node.value, sender: sender.node.value, dateLabel: date.node.value }) });
        if (state.batch) acceptBatch(await api(`batch?id=${escId(state.batch.id)}`));
        if (state.page === "saved") await loadMessages();
        close(); renderPage(); notice("已更正");
      }), "ei-primary", true));
    });
  }
  function showReassociate() {
    const batch = state.batch;
    if (!batch) return;
    dialog("修正这批记录的归属", (body, actions, close) => {
      const values = { friendId: batch.friendId, platform: batch.source.platform, ownAccount: batch.source.ownAccount, conversationKey: batch.source.conversationKey };
      body.append(identityFields(values, (key, value) => { values[key] = value; }),
        el("p", "ei-warning", `本批素材与记录${counts().saved ? `（含已保存的 ${counts().saved} 条）` : ""}都会改为新的归属。`));
      actions.append(button("确认修改关联", () => void run(async () => {
        if (!values.platform.trim()) throw new Error("请填写聊天来源名称。");
        if (!values.ownAccount.trim() || !values.conversationKey.trim()) throw new Error("本人账号与来源会话不能为空。");
        acceptBatch(await api("reassociate", { batchId: batch.id, ...values })); await refreshLists(); await loadSources();
        close(); renderPage(); notice("归属已更新");
      }), "ei-primary", true));
    });
  }
  function showLifecycle() {
    const batch = state.batch;
    if (!batch) return;
    dialog("管理本批资料", (body, actions, close) => {
      for (const [kind, title, help] of [["originals", "原素材副本", "完整录屏清理后不能回放或补抽画面。"],
        ["evidence", "证据画面", "清理后保留转录，但不能回查这些画面。"],
        ["records", "本批正式记录", "检查共享引用后移除本批保存的关联。"]]) {
        const row = el("div", "ei-cleanup-row"), text = el("div"); text.append(el("strong", "", title), el("small", "", help));
        row.append(text, button("查看影响", () => { close(); void previewCleanup(kind); })); body.append(row);
      }
      const recognizer = state.status.model;
      if (recognizer?.model || recognizer?.destination) {
        const model = el("details", "ei-options");
        model.append(el("summary", "", recognizer.model ? `识别模型：${recognizer.model}` : "识别模型"),
          el("p", "ei-help", "使用“模型设置”中配置的主模型。"));
        if (recognizer.destination) model.append(el("p", "ei-help", `连接地址：${recognizer.destination}`));
        body.append(model);
      }
      const notes = el("details", "ei-options"); notes.append(el("summary", "", "备注与来源"));
      renderNotes(notes); body.append(notes);
      actions.append(button("修改来源", () => { close(); showReassociate(); }),
        button("重新识别", () => { close(); showReprocess(); }),
        button("删除任务", () => { close(); showDeleteTask(batch); }, "ei-danger"));
    });
  }
  function showDeleteTask(batch) {
    dialog("删除导入任务？", (body, actions, close) => {
      const active = ACTIVE.has(batch.state);
      body.append(el("p", "", "已保存的聊天和素材会保留，未保存的记录不再核对。"));
      actions.append(button(active ? "停止并删除" : "删除任务", () => void run(async () => {
        if (active) await api("stop", { batchId: batch.id });
        let removed = false;
        for (let attempt = 0; attempt < 60; attempt++) {
          const current = await api(`batch?id=${escId(batch.id)}`);
          if (!ACTIVE.has(current.state)) {
            try { await api("cleanup", { batchId: batch.id, kind: "task" }); removed = true; break; }
            catch (problem) { if (!/请先停止|请等待|正在停止/.test(problem.message)) throw problem; }
          }
          notice("正在停止任务…"); await new Promise(resolve => setTimeout(resolve, 250));
        }
        if (!removed) throw new Error("任务正在停止，尚未删除。稍后可再次点击删除。");
        if (state.batch?.id === batch.id) state.batch = null;
        state.loading++;
        close(); await refreshLists(); await setPage("tasks"); notice("任务已删除");
      }), "ei-danger", true));
    });
  }
  function showUsage() {
    if (!state.friendId) return;
    dialog("导入模型调用", (body, actions) => {
      const currentFriend = state.friendId;
      const scope = select("查看范围", [[currentFriend, friend()?.name || linked.name || "当前好友"]], currentFriend);
      scope.wrapper.hidden = true;
      const progress = el("p", "ei-help"), content = el("section"), more = button("加载更多", () => void load(true));
      progress.setAttribute("role", "status"); body.append(scope.wrapper, progress, content, more);
      const count = value => Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
      const outcome = { ok: "成功", "http-error": "请求失败", "network-error": "连接失败", timeout: "超时", "stream-error": "传输中断", cancelled: "已取消" };
      let summary = null, records = [], next = null, serial = 0, pending = false;
      function render() {
        content.replaceChildren();
        if (summary) {
          const totals = el("div", "ei-usage-summary");
          for (const [label, value] of [["调用次数", summary.requests], ["已知总 token", usageValue(summary, "totalTokens", "knownTotalRequests")],
            ["输入（含缓存）", usageValue(summary, "totalInputTokens", "knownInputRequests")], ["输出（含推理）", usageValue(summary, "totalOutputTokens", "knownOutputRequests")]]) {
            const item = el("div"); item.append(el("small", "", label), el("strong", "", count(value))); totals.append(item);
          }
          content.append(totals, el("p", "ei-help", `${count(summary.knownTotalRequests)} / ${count(summary.requests)} 次报告了完整用量`));
          if (summary.droppedRecords) content.append(el("p", "ei-warning", `全局账本另有 ${count(summary.droppedRecords)} 次未写入，不属于当前筛选的专有缺口。`));
        }
        if (!records.length) content.append(el("p", "ei-empty", "这个范围暂时没有已记录的导入模型调用。"));
        for (const record of records) {
          const row = el("article", "ei-usage-record"), owner = state.friends.find(item => item.id === record.user);
          const time = Number.isFinite(record.at) ? new Date(record.at).toLocaleString("zh-CN") : "时间未知";
          row.append(el("h3", "", record.model || "模型未知"), el("p", "ei-help", `${owner?.name || "外部好友"} · ${time} · ${outcome[record.status] || record.status || "状态未知"}`),
            el("p", "", `输入 ${count(record.totalInputTokens)} · 输出 ${count(record.totalOutputTokens)} · 总 token ${count(record.totalTokens)}`));
          const detail = el("details"), fields = el("dl"); detail.append(el("summary", "ei-help", "调用详情"));
          for (const [label, value] of [["任务", record.task || "—"], ["步骤", record.step === "transcription" ? "聊天转录" : record.step || "—"],
            ["耗时", Number.isFinite(record.durationMs) ? `${(record.durationMs / 1000).toFixed(1)} 秒` : "—"],
            ["计量来源", record.totalTokensSource === "provider" ? "服务报告" : record.totalTokensSource === "derived" ? "按协议推导" : "服务未提供完整用量"]]) {
            fields.append(el("dt", "", label), el("dd", "", value));
          }
          detail.append(fields); row.append(detail); content.append(row);
        }
        more.hidden = !next; more.disabled = pending;
      }
      async function load(append = false) {
        if (pending) return;
        pending = true; more.disabled = true; scope.node.disabled = true; progress.textContent = "正在读取模型调用…";
        const request = ++serial, params = new URLSearchParams();
        if (scope.node.value) params.set("friendId", scope.node.value);
        if (append && next) params.set("before", next.join(","));
        try {
          const result = await api(`usage?${params}`);
          if (request !== serial || !body.isConnected || !byId("eiDialog").open) return;
          summary = result.summary; records = append ? records.concat(result.records || []) : result.records || []; next = result.next || null;
          progress.textContent = ""; render();
        } catch (problem) { if (body.isConnected) { progress.textContent = problem.message; progress.setAttribute("role", "alert"); } }
        finally { pending = false; more.disabled = false; scope.node.disabled = false; }
      }
      scope.node.addEventListener("change", () => { records = []; next = null; content.replaceChildren(); void load(); });
      actions.append(button("刷新", () => void load()));
      void load();
    });
  }
  function showReprocess() {
    dialog("重新识别", (body, actions, close) => {
      body.append(el("p", "", "已保存内容和人工修改不会被覆盖，差异会列为疑点。"));
      actions.append(button("开始重新识别", () => void run(async () => {
        acceptBatch(await api("start", { batchId: state.batch.id, reprocess: true })); close(); renderPage(); notice("已开始重新识别");
      }), "ei-primary", true));
    });
  }
  async function previewCleanup(kind) {
    await run(async () => {
      const result = await api("cleanup-preview", { batchId: state.batch.id, kind });
      dialog("确认清理影响", (body, actions, close) => {
        const labels = { originals: "原素材副本", evidence: "证据画面", task: "导入任务", records: "本批正式记录" };
        body.append(el("p", "", `清理范围：${labels[kind]}`));
        const explanation = result.effects || result.message || result.reason || result.summary;
        if (explanation) body.append(el("p", "ei-warning", explanation));
        if (result.bytes != null) body.append(el("p", "ei-muted", `可释放空间：${formatBytes(result.bytes)}`));
        if (result.savedRecords != null) body.append(el("p", "ei-help", `本批关联正式记录 ${result.savedRecords} 条；受影响批次 ${(result.affectedBatchIds || []).length} 个。`));
        for (const detail of result.impacts || result.warnings || []) body.append(el("p", "ei-help", typeof detail === "string" ? detail : detail.message || JSON.stringify(detail)));
        const blocked = result.allowed === false || result.blocked === true || (Array.isArray(result.blockers) && result.blockers.length > 0);
        if (blocked) body.append(el("p", "ei-warning", "存在未完成任务或共享依赖，暂不能清理。"));
        const commit = button("确认清理", () => void run(async () => {
          const id = state.batch.id; const cleared = await api("cleanup", { batchId: id, kind });
          close();
          if (kind === "task" || cleared.deleted === true) { state.batch = null; await refreshLists(); await setPage("tasks"); }
          else { acceptBatch(cleared.batch || await api(`batch?id=${escId(id)}`)); renderPage(); }
          notice(cleared.filesNotRemoved?.length ? "部分本机文件未能删除，请检查占用后重试" : "已清理");
        }), "ei-danger", true);
        disable(commit, blocked); actions.append(commit);
      });
    });
  }
  async function poll() {
    if (state.destroyed) return;
    try {
      if (!state.busy && state.friendId) {
        const epoch = state.epoch, revision = state.listRevision, friendId = state.friendId;
        const result = await api(`batches?friendId=${escId(friendId)}`);
        if (epoch !== state.epoch || revision !== state.listRevision || friendId !== state.friendId || state.busy) return;
        const nextBatches = batchesForFriend(result.batches || [], state.friendId);
        const listingChanged = JSON.stringify(nextBatches) !== JSON.stringify(state.batches);
        state.batches = nextBatches; renderContext();
        const current = state.batch;
        if (current && (ACTIVE.has(current.state) || ACTIVE.has(state.batches.find(item => item.id === current.id)?.state))) {
          const id = current.id, result = await api(`batch?id=${escId(id)}`);
          if (epoch !== state.epoch || revision !== state.listRevision) return;
          if (state.batch?.id === id && !state.busy) {
            const oldLength = state.batch.candidates.length, oldState = state.batch.state;
            acceptBatch(result); updateProcessing();
            if (state.page === "review" && !state.editing && !state.dragging && !state.pendingEdits && (oldLength !== state.batch.candidates.length || oldState !== state.batch.state)) renderPage();
            else if (oldLength !== state.batch.candidates.length) notice("有新的识别结果");
          }
        }
        if (state.page === "tasks" && listingChanged) renderPage();
      }
    } catch { /* Explicit actions report errors; transient polling failures do not replace an editor. */ }
    finally { if (!state.destroyed) state.poll = setTimeout(poll, 3000); }
  }
  function applyTheme() {
    let light = query.get("theme") === "light";
    try {
      const theme = JSON.parse(localStorage.getItem("real-ui-settings-1") || "{}").theme;
      if (theme === "light" || theme === "dark") light = theme === "light";
    } catch { }
    document.body.classList.toggle("theme-light", light);
  }
  function refreshFromHost() {
    if (state.destroyed) return;
    if (state.busy) { state.hostRefreshPending = true; return; }
    void run(async () => {
      await refreshLists();
      if (state.batch) acceptBatch(await api(`batch?id=${escId(state.batch.id)}`));
      await loadSources();
      if (state.page === "saved") await loadMessages();
      renderPage();
    });
  }
  function hostEvent(event) {
    if (event?.type === "theme" || event?.type === "kyf-external:theme") document.body.classList.toggle("theme-light", event.theme === "light");
    else if (["focus", "service-restored"].includes(event?.type)) refreshFromHost();
    else if (event?.type === "plugins-changed") {
      const plugin = event.plugins?.find(item => item.id === "kyf.external-chat-import");
      if (plugin) state.status.plugin = { ...state.status.plugin, enabled: plugin.enabled === true && !plugin.incompatible && !plugin.dependencyError };
      updateProcessing();
      if (plugin && state.status.plugin.enabled === false) notice(plugin.dependencyError?.message || "导入识别插件已停用，现有记录仍可浏览。");
      else refreshFromHost();
    }
  }
  window.addEventListener("message", event => {
    if (!opener || event.source !== opener || event.origin !== window.location.origin || !event.data || typeof event.data !== "object") return;
    hostEvent(event.data);
  });
  byId("eiAdd").addEventListener("click", showImport);
  // The bubble menu closes on any press outside it, Escape, or leaving the window.
  window.addEventListener("pointerdown", event => { if (state.menu && !state.menu.contains(event.target)) closeMenu(); }, true);
  window.addEventListener("keydown", event => {
    if (event.key === "Escape") closeMenu();
    // Text fields keep their own Ctrl+Z; elsewhere in the review it takes back the last change.
    else if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && !event.isComposing && event.key.toLowerCase() === "z" &&
        state.page === "review" && !byId("eiDialog").open && !event.target?.closest?.("input, textarea, select, [contenteditable]")) {
      event.preventDefault(); closeMenu(); undo();
    }
  });
  window.addEventListener("blur", closeMenu);
  window.addEventListener("resize", closeMenu);
  byId("eiSettings").addEventListener("click", () => navigateApp("model-settings"));
  byId("eiUsage").addEventListener("click", showUsage);
  byId("eiRefresh").addEventListener("click", () => void run(async () => {
    await refreshLists(); if (state.batch) acceptBatch(await api(`batch?id=${escId(state.batch.id)}`));
    await loadSources(); if (state.page === "saved") await loadMessages(); renderPage();
  }));
  document.querySelectorAll("[data-page]").forEach(node => node.addEventListener("click", () => void run(() => setPage(node.dataset.page))));
  window.addEventListener("storage", event => { if (event.key === "real-ui-settings-1") applyTheme(); });
  window.addEventListener("beforeunload", event => {
    if (state.pendingEdits || state.submitting || editingChanged()) { event.preventDefault(); event.returnValue = ""; }
  });
  const unsubscribeHost = host?.onHostEvent?.(hostEvent);
  window.addEventListener("pagehide", () => {
    if (returningToChat && opener && !opener.closed) {
      opener.postMessage({ type: "kyf-external:navigate", target: "chat" }, window.location.origin); opener.focus();
    }
    state.destroyed = true; clearTimeout(state.poll); unsubscribeHost?.();
  });
  applyTheme();
  void run(async () => {
    if (host?.getContext) {
      const context = await host.getContext();
      Object.assign(linked, normalizedScope(context || {}));
      if (context?.theme) document.body.classList.toggle("theme-light", context.theme === "light");
    }
    await refreshLists(); await loadSources();
    const requested = query.get("batchId");
    if (requested && state.friendId) await openBatch(requested); else await setPage("saved");
  }).finally(() => { state.poll = setTimeout(poll, 3000); });
  if (opener && !opener.closed) opener.postMessage({ type: "kyf-external:ready" }, window.location.origin);
})();
