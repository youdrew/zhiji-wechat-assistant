"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Account-scoped independent panel; the host owns credentials and authoritative self-profile storage.
(function () {
  const byId = id => document.getElementById(id);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const string = (value, limit = 16000) => typeof value === "string" ? value.slice(0, limit) : "";
  const tabs = ["profile", "tags", "timeline", "persona", "values", "build", "trial"];
  const MAX_TURNS = 20;
  const DEPTHS = { basic: "概括主要信息", standard: "更细致地整理信息", deep: "细致分析并复核判断，耗时较长" };
  const validDepth = value => Object.hasOwn(DEPTHS, value);
  const DEPTH_NAMES = { basic: "基础", standard: "标准", deep: "深度" };
  const OUTPUTS = ["profile", "tags", "timeline", "persona", "periods", "worldview", "values", "library"];
  // 既有账号未保存过选择时保持原默认五项；时间线只在勾选后运行。
  const DEFAULT_OUTPUTS = ["profile", "tags", "persona", "values", "library"];
  const Charts = window.KyfPersonaCharts;
  // 性格 and 三观 share one layout: an audited inference (and for the 三观 text items a self-report) plus the user's own correction.
  const OPINIONS = {
    persona: { rpc: "self.persona", edit: "self.personaEdit", noun: "性格",
      keys: Object.fromEntries(Object.entries(Charts.DIMENSIONS).map(([key, [label]]) => [key, label])) },
    values: { rpc: "self.values", edit: "self.valuesEdit", noun: "三观", keys: { love: "感情观", family: "家庭观", money: "金钱观", plan: "人生规划", core: "价值底色" } } };
  const freshPeriods = (serial = 0) => ({ value: null, serial, detailSerial: 0, pending: false, selected: "", detailKey: "", detail: null, expanded: new Set(), evidencePages: {}, drafts: {}, error: "", edit: null, saving: false });
  const freshOpinion = (serial = 0) => ({ value: null, serial, pending: false, busy: false, drafts: {}, dirty: new Set(), editing: new Set(), expanded: new Set(), evidencePages: {}, notice: "" });
  const selectedOutputs = value => Array.isArray(value) && value.every(key => OUTPUTS.includes(key)) ? OUTPUTS.filter(key => value.includes(key)) : null;
  const state = { context: null, epoch: 0, status: null, statusSerial: 0, statusPending: false, busy: false,
    profileSerial: 0, profileReady: false, profileVersion: null, profileBusy: false, profileStale: false, dirty: false,
    searchSerial: 0, searching: false, exclusionSerial: 0, exclusionsBusy: false, timer: null, disabled: false,
    modelConfigured: null, modelPending: false, modelSerial: 0, tab: "profile",
    draftSerial: 0, draft: null, draftJob: null, analysisDepth: "",
    turns: [], replying: false, replyError: "", trialSerial: 0, trialTimer: null,
    tags: null, tagSerial: 0, tagPending: false, tagBusy: false, tagSelected: "", evidenceSerial: 0, tagUndo: "", tagError: "",
    analysis: null, analysisSerial: 0, analysisPending: false, analysisBusy: false, analysisAction: "", analysisTimer: null, analysisError: "", deliveredDraft: "",
    analysisOutputs: [...DEFAULT_OUTPUTS], outputsReady: false, outputsVersion: 0, outputsSettingsSerial: 0, outputsSettingsBusy: false, outputsSettingsLoaded: false,
    menuOpen: false, panelOpen: false, wasRunning: false,
    opinions: { persona: freshOpinion(), values: freshOpinion() }, chartSelected: { persona: null, values: null }, periods: freshPeriods(), worldview: freshPeriods(),
    timeline: null, timelineSerial: 0, timelinePending: false, timelineBusy: false, timelineError: "",
    timelineOpen: "", timelineEvidence: {}, timelineFilter: "", timelineImportantOnly: false,
    timelineCollapsed: new Set(), timelineExpanded: new Set(), timelineUnknownOpen: false, timelineTrack: "",
    timelineManage: false, timelineEditing: "", timelineWhen: null, timelineRendering: false,
    timelineAddType: null, timelineNotice: "", timelineUndoQueued: 0 };
  const account = () => string(state.context?.account, 240);
  const scope = () => ({ account: account(), user: string(state.context?.user, 240) });
  const current = (epoch, value) => state.epoch === epoch && account() === value;
  let judgeShadowSerial = 0;
  async function loadJudgeShadow() {
    if (!account() || state.disabled) return;
    const epoch = state.epoch, original = account(), serial = ++judgeShadowSerial;
    byId("judgeShadowRefresh").disabled = true;
    message("judgeShadowSummary", "读取中…");
    try {
      const result = await window.kyfHost.rpc("self.judgeShadow", {}, scope());
      if (!current(epoch, original) || serial !== judgeShadowSerial) return;
      if (result?.mode !== "shadow" || !object(result.counts) || !Array.isArray(result.engines)) throw new Error("invalid shadow summary");
      const lines = [`已判断 ${count(result.counts.done) ?? 0} / ${count(result.counts.total) ?? 0} 个窗口`];
      if (result.counts.error) lines[0] += `，${count(result.counts.error) ?? 0} 个未完成`;
      for (const group of result.engines) {
        const positives = count(group.positiveWindows), negatives = count(group.falseNegativeWindows);
        if (positives === null || negatives === null) continue;
        const name = `${group.kind === "timeline" ? "时间线" : "本人证据"} · ${group.engine === "systemone" ? "本地判断" : "生成式判断"}`;
        lines.push(`${name}：漏判 ${negatives} / ${positives}`);
      }
      const threshold = result.diagnosticThreshold;
      const boundary = Number.isFinite(threshold) && threshold >= 0 && threshold <= 1 ? `，诊断分界 ${threshold}` : "";
      if (result.counts.done) lines.push(`以主模型提取为对照${boundary}。仅记录判断，尚未启用跳过。`);
      message("judgeShadowSummary", lines.join("；"));
    } catch {
      if (current(epoch, original) && serial === judgeShadowSerial) message("judgeShadowSummary", "评估暂时无法读取");
    } finally {
      if (current(epoch, original) && serial === judgeShadowSerial) byId("judgeShadowRefresh").disabled = false;
    }
  }
  byId("judgeShadowRefresh").addEventListener("click", () => void loadJudgeShadow());
  function message(id, text) { const node = byId(id); node.textContent = text; node.hidden = !text; }
  function badge(id, text, ready = false) { const node = byId(id); node.textContent = text; node.className = "badge" + (ready ? " ready" : ""); }
  function selectTab(key, focus = false) {
    if (!tabs.includes(key)) return;
    state.tab = key;
    for (const tab of tabs) {
      const selected = tab === key, button = byId("tab-" + tab), panel = byId("panel-" + tab);
      button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1;
      panel.hidden = !selected; panel.tabIndex = selected ? 0 : -1;
    }
    byId("tabContent").scrollTop = 0;
    if (key === "build") void loadExclusions();
    if ((key === "trial" || key === "tags") && !state.modelPending) void loadModelStatus();
    if (key === "tags") void loadTags();
    if (key === "timeline") void loadTimeline();
    if (Object.hasOwn(OPINIONS, key)) { void loadOpinion(key); if (key === "values") void loadOpinion("persona"); }
    if (key === "persona") void loadPeriods();
    if (key === "values") void loadWorldview();
    if (focus) { byId("tab-" + key).focus(); byId("tab-" + key).scrollIntoView?.({ block: "nearest", inline: "nearest" }); }
  }
  function friendly(error) {
    if (["plugin-disabled", "disabled", "plugin-unavailable"].includes(error?.code)) return "自我剖析尚未启用，请在设置的插件管理中启用后重试。";
    if (["context-changed", "account-changed"].includes(error?.code)) return "当前账号已切换，请在新账号下重试。";
    if (error?.code === "unknown-method") return "当前软件暂不支持此操作，请更新后重试。";
    return string(error?.message, 500) || "操作暂未完成，请稍后重试。";
  }
  function cancelTimer() { if (state.timer !== null) clearTimeout(state.timer); state.timer = null; }
  function semanticStatus(value) {
    const embedding = object(value?.embedding) ? value.embedding : {}, retrieval = object(value?.retrieval) ? value.retrieval : {};
    const indexed = count(retrieval.indexedSamples), pending = count(retrieval.pendingSamples);
    const total = count(retrieval.totalSamples) ?? (indexed !== null && pending !== null ? count(indexed + pending) : null);
    const engineReady = embedding.state === "ready" && embedding.ready === true;
    const usable = engineReady && retrieval.mode === "hybrid" && retrieval.semanticReady === true && indexed > 0;
    return { embedding, retrieval, indexed, pending, total, engineReady, present: object(value?.embedding),
      active: ["waiting", "indexing"].includes(embedding.indexState) && embedding.state !== "error", paused: embedding.indexState === "paused", stopping: embedding.stopping === true,
      usable, complete: engineReady && embedding.indexState === "ready" && pending === 0 && (total === 0 || usable) };
  }
  function updateMode(value = state.status, semantic = semanticStatus(value)) {
    const samples = count(value?.coverage?.sampleCount) ?? count(value?.build?.sampleCount) ?? 0;
    return value?.build?.state === "complete" && samples > 0 && semantic.present && !semantic.complete ? "semantic" : "incremental";
  }
  function schedule() {
    cancelTimer();
    if (state.analysis?.state === "running") return;
    const semantic = semanticStatus(state.status);
    if (state.disabled || !account() || !(state.status?.build?.state === "running" || state.status?.build?.stopping || semantic.active || semantic.stopping || ["downloading", "loading"].includes(semantic.embedding.state))) return;
    state.timer = setTimeout(() => { state.timer = null; void loadStatus(); }, 1800);
  }
  function validStatus(value) {
    return object(value) && value.account === account() && object(value.source) && object(value.build) &&
      object(value.coverage) && ["idle", "running", "paused", "complete", "partial", "failed"].includes(value.build.state);
  }
  function render() {
    const available = !!account() && !state.disabled;
    const value = state.status, build = value?.build || {}, coverage = value?.coverage || {};
    const samples = count(coverage.sampleCount) ?? count(build.sampleCount) ?? 0;
    const processed = count(build.processedConversations) ?? count(coverage.indexedConversations);
    const total = count(build.totalConversations) ?? count(coverage.discoveredConversations);
    const failures = count(build.failedCount) ?? count(coverage.failedConversations) ?? 0;
    const running = build.state === "running" || build.stopping === true;
    const semantic = semanticStatus(value);
    const needsSemantic = semantic.present && !semantic.complete, semanticOnly = updateMode(value, semantic) === "semantic";
    const semanticMessage = semanticOnly ? `${semantic.active ? "正在补充" : "仅补充"}语义索引，已有样本和历史进度保留。` : "";
    byId("account").textContent = available ? `当前账号：${account()}` : state.disabled ? "自我剖析暂不可用" : "请先连接并选择一个微信账号";
    // Readiness stays out of the way: a hint appears only while something still needs setting up.
    byId("sourceHint").hidden = !!account() && value?.source?.ready !== false;
    renderModel();
    const labels = { idle: "尚未建立", running: "正在建立", paused: "已暂停", complete: "全部处理完", partial: "有未完成项", failed: "暂未完成" };
    const historyLabel = semantic.stopping ? "正在暂停" : build.state === "complete" && semantic.present && (needsSemantic || semantic.active) ? semantic.active ? "正在完善" : "历史已整理" : labels[build.state] || "待检查";
    badge("buildState", historyLabel, build.state === "complete" && !needsSemantic && !semantic.active);
    const newMessages = count(value?.newMessagesAvailable ?? coverage.newMessagesAvailable) > 0;
    let description = "";
    if (build.stopping) description = "正在保存本批进度…";
    else if (semantic.stopping) description = "正在暂停，已有样本可先使用。";
    else if (build.state === "running") description = build.phase === "discovering" ? "正在发现私聊…" : build.phase === "embedding" ? "正在补充语义索引…" : "正在整理你的回复，可以离开页面。";
    else if (build.state === "idle" && value && !samples) description = "从全部私聊整理你发出的回复，首次需下载约 96 MB 检索资源。";
    else if (build.state === "paused") description = "进度已保留。";
    else if (build.state === "complete" && !samples) description = "没有找到可用的本人文字回复。";
    else if (semanticOnly) description = semanticMessage;
    else if (build.state === "partial") description = "部分私聊未完成，可从顶部继续分析。";
    else if (build.state === "failed") description = string(build.error, 500) || "整理未完成，已完成的部分已保留。";
    if (newMessages && !running) description += (description ? " " : "") + "有新消息，可从顶部更新分析。";
    byId("buildMessage").textContent = description;
    const progress = byId("progress"); progress.hidden = !running || build.phase === "embedding";
    if (running && coverage.catalogComplete === true && total > 0 && processed !== null) { progress.max = total; progress.value = Math.min(processed, total); }
    else progress.removeAttribute("value");
    for (const [id, number] of [["conversationCount", total], ["processedCount", processed], ["messageCount", count(coverage.messageCount) ?? count(build.messageCount)], ["sampleCount", samples], ["failedCount", failures]]) byId(id).textContent = number === null ? "—" : String(number);
    // Only known gaps are worth a line; the counts above already say how much was read.
    const gaps = [], skipped = count(coverage.skippedMessages), failedFiles = Array.isArray(coverage.failedShards) ? coverage.failedShards.length : count(coverage.failedShards), unresolved = count(coverage.unresolvedTables);
    if (skipped > 0) gaps.push(`跳过 ${skipped} 条消息`);
    if (failedFiles > 0) gaps.push(`${failedFiles} 份历史文件未能读取`);
    if (unresolved > 0) gaps.push(`${unresolved} 处历史记录尚未识别`);
    message("coverageMessage", value && gaps.length ? gaps.join("；") + "。" : "");
    renderSemantic(semantic);
    byId("reset").disabled = !available || state.busy || !value || build.state === "idle" && samples === 0;
    byId("confirmReset").disabled = !available || state.busy;
    byId("sourceSettings").disabled = false;
    byId("modelSettings").disabled = false;
    byId("query").disabled = !available || samples === 0;
    byId("query").placeholder = samples ? "输入一个情境，检索你过去的回复" : "从顶部开始分析后即可检索";
    byId("search").disabled = !available || samples === 0 || state.searching || state.busy;
    byId("trialInput").disabled = !available || samples === 0;
    byId("trialSend").disabled = !available || samples === 0 || state.replying;
    byId("clearTrial").disabled = !state.turns.length && !state.replying && !state.replyError;
    byId("chatEmpty").textContent = samples ? "以好友身份发消息，看看你会怎么回" : "从顶部开始分析，整理表达样本后即可试用";
    renderAnalysis();
  }
  function renderModel() {
    // Only an explicitly missing configuration is worth a hint; reply failures explain themselves.
    byId("modelHint").hidden = byId("tagModelHint").hidden = byId("analysisModelHint").hidden = state.modelConfigured !== false;
  }
  async function loadModelStatus() {
    const serial = ++state.modelSerial;
    state.modelPending = true; render();
    try {
      const value = await window.kyfHost.host("model.status", {});
      if (serial !== state.modelSerial) return;
      state.modelConfigured = object(value) && typeof value.configured === "boolean" ? value.configured : null;
    } catch { if (serial === state.modelSerial) state.modelConfigured = null; }
    finally { if (serial === state.modelSerial) { state.modelPending = false; render(); } }
  }
  function renderSemantic(semantic) {
    const { embedding, indexed, pending, total, engineReady } = semantic;
    const build = state.status?.build || {}, paused = semantic.paused || build.state === "paused" || build.stopping === true;
    let label = "待检查";
    if (semantic.stopping) label = "正在暂停";
    else if (embedding.state === "missing") label = "待准备";
    else if (["downloading", "loading"].includes(embedding.state)) label = embedding.state === "downloading" ? "正在下载资源" : "正在准备资源";
    else if (embedding.state === "error") label = "准备未完成";
    else if (engineReady) {
      if (embedding.indexState === "error") label = "样本整理未完成";
      else if (paused) label = "进度已保留";
      else if (semantic.active) label = "正在理解样本";
      else if (semantic.usable && pending === 0) label = "已可用";
      else if (semantic.usable) label = "部分可用";
      else label = "资源已准备";
    }
    badge("semanticState", label, semantic.usable && embedding.indexState !== "error");
    const download = byId("downloadProgress"), bytes = count(embedding.downloadedBytes), maximum = count(embedding.totalBytes);
    download.hidden = embedding.state !== "downloading";
    if (!download.hidden && maximum > 0 && bytes !== null) { download.max = maximum; download.value = Math.min(bytes, maximum); }
    else download.removeAttribute("value");
    const megabytes = amount => (amount / (1024 * 1024)).toFixed(1);
    message("downloadMessage", !download.hidden && bytes !== null ? maximum > 0 ? `已下载 ${megabytes(Math.min(bytes, maximum))} MB / ${megabytes(maximum)} MB` : `已下载 ${megabytes(bytes)} MB，总量待确认` : "");
    const progress = byId("semanticProgress"); progress.hidden = !(total > 0 && indexed !== null && indexed < total) && !semantic.active;
    if (!progress.hidden && total > 0 && indexed !== null) { progress.max = total; progress.value = Math.min(indexed, total); }
    else progress.removeAttribute("value");
    message("semanticSampleMessage", indexed !== null && total > 0 && indexed < total ? `${indexed} / ${total} 条` : "");
    const failed = embedding.state === "error" || embedding.indexState === "error";
    const reason = string(embedding.state === "error" ? embedding.error : embedding.indexError, 180);
    message("semanticError", failed ? `${reason || "语义检索准备未完成"}。请从顶部继续分析，已有样本仍可按词语检索。` : "");
  }
  function renderProfile() {
    const editable = !!account() && state.profileReady && !state.disabled;
    byId("background").disabled = !editable || state.profileBusy;
    byId("style").disabled = !editable || state.profileBusy;
    byId("saveProfile").disabled = !editable || !state.dirty || state.profileBusy;
    byId("draftProfile").hidden = byId("pauseDraft").hidden = byId("draftProgress").hidden = true;
    byId("draftProfile").disabled = !account() || state.analysisBusy || state.analysis?.state === "running";
    byId("pauseDraft").disabled = state.analysisBusy;
    byId("reloadProfile").disabled = !account() || state.profileBusy || state.disabled;
    byId("reloadProfile").hidden = !state.profileStale;
  }
  async function loadStatus() {
    if (!account() || state.disabled || state.statusPending || state.busy) return;
    const epoch = state.epoch, original = account(), serial = ++state.statusSerial;
    state.statusPending = true; render();
    try {
      const value = await window.kyfHost.rpc("self.status", {}, scope());
      if (!current(epoch, original) || serial !== state.statusSerial) return;
      if (!validStatus(value)) throw new Error("知识库状态暂时无法读取，请刷新重试。");
      state.status = value; message("error", "");
    } catch (error) {
      if (!current(epoch, original) || serial !== state.statusSerial) return;
      state.disabled = ["plugin-disabled", "disabled", "plugin-unavailable"].includes(error?.code);
      message("error", friendly(error));
    } finally {
      if (current(epoch, original) && serial === state.statusSerial) { state.statusPending = false; render(); schedule(); }
    }
  }
  async function loadProfile() {
    if (!account() || state.profileBusy) return;
    const epoch = state.epoch, original = account(), serial = ++state.profileSerial;
    state.profileBusy = true; renderAnalysis();
    try {
      const result = await window.kyfHost.host("self-profile.read", { account: original });
      if (!current(epoch, original) || serial !== state.profileSerial) return;
      if (!object(result) || typeof result.background !== "string" || typeof result.style !== "string" || result.version === undefined) throw new Error("本人资料暂时无法读取，请重试。");
      state.profileVersion = result.version; state.profileReady = true; state.profileStale = false;
      if (state.dirty) {
        byId("latestBackground").textContent = result.background || "（未填写）";
        byId("latestStyle").textContent = result.style || "（未填写）";
        byId("latestProfile").hidden = false;
        message("profileMessage", "请对照最新资料合并后保存，当前草稿已保留。");
      } else {
        byId("background").value = result.background; byId("style").value = result.style;
        byId("latestProfile").hidden = true; message("profileMessage", "");
      }
      message("profileError", "");
    } catch (error) { if (current(epoch, original) && serial === state.profileSerial) { state.profileStale = true; message("profileError", friendly(error)); } }
    finally { if (current(epoch, original) && serial === state.profileSerial) { state.profileBusy = false; renderAnalysis(); } }
  }
  async function saveProfile() {
    if (!account() || !state.profileReady || state.profileBusy || !state.dirty) return;
    const epoch = state.epoch, original = account(), serial = ++state.profileSerial;
    const background = byId("background").value, style = byId("style").value;
    if (background.length > 4000 || style.length > 4000) { message("profileError", "每栏最多填写 4000 字，请缩短后保存。"); return; }
    state.profileBusy = true; renderAnalysis(); message("profileError", "");
    try {
      const result = await window.kyfHost.host("self-profile.save", { account: original, background, style, expectedVersion: state.profileVersion });
      if (!current(epoch, original) || serial !== state.profileSerial) return;
      if (!object(result) || result.version === undefined) throw new Error("保存结果暂时无法确认，请读取最新资料核对。");
      state.profileVersion = result.version; state.dirty = false; byId("latestProfile").hidden = true;
      message("profileMessage", "已保存");
    } catch (error) {
      if (!current(epoch, original) || serial !== state.profileSerial) return;
      const conflict = ["profile-conflict", "request-conflict", "version-conflict", "conflict"].includes(error?.code);
      state.profileStale = state.profileStale || conflict;
      message("profileError", conflict ? "资料已在其他页面修改，草稿已保留，请读取最新资料后合并。" : friendly(error));
    } finally { if (current(epoch, original) && serial === state.profileSerial) { state.profileBusy = false; renderAnalysis(); } }
  }
  function clearDraft() {
    state.draftSerial++; state.draft = null; state.draftJob = null;
    byId("draftProgress").hidden = true; byId("draftPanel").hidden = true;
    byId("draftCoverage").hidden = true;
  }
  function renderDraftCoverage(job) {
    const c = object(job.coverage) ? job.coverage : null;
    if (!c) return;
    const n = key => count(c[key]) ?? 0;
    const gaps = Array.isArray(c.gaps) ? c.gaps.filter(item => object(item) && count(item.count) > 0) : [];
    const unfinished = job.state === "done" && (n("analyzedMessages") < n("ownMessages") || n("analyzedParts") < n("totalParts"));
    const partial = unfinished || n("failedConversations") > 0 || n("unreadMessages") > 0 || c.directoryPartial === true || gaps.length > 0;
    byId("draftCoverage").hidden = false;
    byId("draftCoverageTitle").textContent = job.state === "done" ? partial ? "已完成可读部分 · 仍有读取缺口" : "本地可读文字已分析完成" : "本地累计覆盖范围";
    const conversations = c.catalogComplete === true ? String(n("conversations")) : "总量待确认";
    byId("draftReadCoverage").textContent = `本地累计已读取 ${n("readConversations")} / ${conversations} 个聊天，${n("messagesRead")} 条双方消息；保存 ${n("ownMessages")} 条本人文字（私聊 ${n("privateMessages")}，群聊 ${n("groupMessages")}${n("externalMessages") ? `，外部聊天 ${n("externalMessages")}` : ""}）。`;
    const own = c.catalogComplete === true && n("readConversations") + n("failedConversations") >= n("conversations") ? String(n("ownMessages")) : `${n("ownMessages")}（读取中，总量待确认）`;
    const parts = n("totalParts") > 0 ? `；长消息分段 ${n("analyzedParts")} / ${n("totalParts")}` : "";
    const reused = count(c.reusedMessages), newlyAnalyzed = count(c.newlyAnalyzedMessages);
    const reuse = reused !== null && newlyAnalyzed !== null ? `；复用已有分析 ${reused} 条，本轮新分析 ${newlyAnalyzed} 条` : "";
    byId("draftModelCoverage").textContent = `累计已分析 ${n("analyzedMessages")} / ${own} 条本人文字${parts}${reuse}。`;
    message("draftBatchCoverage", count(job.batchesDone) !== null ? `已完成 ${job.batchesDone} 批分析${validDepth(job.depth) ? ` · ${job.depth === "deep" ? "深度" : job.depth === "standard" ? "标准" : "基础"}档` : ""}。` : "");
    const gapLabels = { conversation: "聊天未能读取", "conversation-failed": "聊天未能读取", "unreadable-conversations": "聊天未能读取", failedConversations: "聊天未能读取", unreadMessages: "原始消息未能读取", "unread-messages": "原始消息未能读取", "directory-unread-messages": "目录记录的原始消息未能读取", skipped: "原始消息未能读取", "skipped-messages": "原始消息未能读取", shard: "历史文件未能读取", "failed-shards": "历史文件未能读取", unresolved: "历史记录未能识别", "unresolved-tables": "历史记录未能识别", "sequence-gap": "消息序号缺口", "noncontiguous-messages": "消息分页不连续", "partial-conversations": "聊天历史读取不完整", directory: "聊天目录不完整", "directory-partial": "聊天目录不完整", "external-unreadable": "外部聊天未能读取" };
    const lines = gaps.map(item => `${gapLabels[item.kind] || "其他读取缺口"}：${item.count}`);
    if (n("failedConversations") > 0 && !gaps.some(item => /conversation/i.test(item.kind))) lines.push(`${n("failedConversations")} 个聊天未能读取`);
    if (n("unreadMessages") > 0 && !gaps.some(item => /skipped|unread/i.test(item.kind))) lines.push(`${n("unreadMessages")} 条原始消息未能读取（本人文字数量未知）`);
    if (c.directoryPartial === true && !gaps.some(item => /directory/i.test(item.kind))) lines.push("聊天目录不完整，实际总量未知");
    if (unfinished) lines.push("仍有本人文字或长消息分段未完成模型分析");
    if (n("excludedMessages") > 0) lines.push(`非文字或无法确认归属等未参与：${n("excludedMessages")} 条`);
    message("draftGaps", lines.length ? lines.join("；") + "。" : "");
  }
  function renderDraftProgress(job) {
    state.draftJob = job;
    const done = count(job.done), total = count(job.total), progress = byId("draftProgress");
    progress.hidden = true;
    if (job.phase !== "reading" && total > 0 && done !== null) { progress.max = total; progress.value = Math.min(done, total); } else progress.removeAttribute("value");
    const labels = { reading: "正在同步聊天中的本人文字…", extracting: "正在分批分析尚未处理的本人文字…", reviewing: "正在复核本批分析…", merging: "正在分层汇总与交叉核对…", writing: "正在整理初稿…" };
    message("profileMessage", labels[job.phase] || "正在分析本人文字…");
    renderDraftCoverage(job); renderProfile();
  }
  function showDraftField(key, value) {
    const field = byId(key), block = byId(key === "background" ? "draftBackgroundBlock" : "draftStyleBlock");
    if (!value || field.value.trim() === value.trim()) { block.hidden = true; return false; }
    if (!field.value.trim() && !state.dirty) { field.value = value; block.hidden = true; return "filled"; }
    byId(key === "background" ? "draftBackground" : "draftStyle").textContent = value; block.hidden = false; return "preview";
  }
  function deliverDraft(job) {
    const result = object(job.result) ? job.result : {};
    state.draft = { background: string(result.background, 4000), style: string(result.style, 4000) };
    const outcomes = [showDraftField("background", state.draft.background), showDraftField("style", state.draft.style)];
    byId("draftPanel").hidden = !outcomes.includes("preview");
    if (outcomes.includes("filled")) state.dirty = true;
    message("profileMessage", outcomes.includes("preview") ? "已有内容未改动，可对照下方初稿替换。" : outcomes.includes("filled") ? "初稿已填入，修改后保存。" : "初稿与现有内容相同。");
  }
  function useDraft(key) {
    if (!state.draft?.[key]) return;
    byId(key).value = state.draft[key]; byId(key === "background" ? "draftBackgroundBlock" : "draftStyleBlock").hidden = true;
    byId("draftPanel").hidden = byId("draftBackgroundBlock").hidden && byId("draftStyleBlock").hidden;
    state.dirty = true; message("profileMessage", "已替换为初稿，修改后保存。"); renderProfile();
  }
  function cancelAnalysisTimer() { if (state.analysisTimer !== null) clearTimeout(state.analysisTimer); state.analysisTimer = null; }
  function scheduleAnalysis() {
    cancelAnalysisTimer();
    if (account() && !state.disabled && state.analysis?.state === "running") state.analysisTimer = setTimeout(() => { state.analysisTimer = null; void loadAnalysis(); }, 1500);
  }
  function stepProgress(step) {
    const value = object(step?.progress) ? { ...step, ...step.progress } : step || {}, done = count(value.done), total = count(value.total);
    const units = { chats: "个聊天", conversations: "个聊天", messages: "条", batches: "批", samples: "个样本", parts: "段" };
    const defaults = { history: "个聊天", library: "个聊天", evidence: "条", profile: "条", tags: "条", "timeline-extract": "条", timeline: "批", persona: "条", periods: "题", worldview: "题", values: "条", semantic: "个样本" };
    const rawUnit = string(value.unit, 20), unit = rawUnit ? Object.hasOwn(units, rawUnit) ? units[rawUnit] : rawUnit : Object.hasOwn(defaults, step?.key) ? defaults[step.key] : "";
    return { done, total, unit, known: done !== null && total > 0 && done <= total,
      text: done !== null && total > 0 && done <= total ? `${done} / ${total}${unit ? " " + unit : ""}` : done > 0 ? `已处理 ${done}${unit ? " " + unit : ""}` : "" };
  }
  const milliseconds = value => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  function duration(value) {
    const seconds = Math.floor(value / 1000), minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
    if (hours) return `${hours}小时${minutes % 60 ? minutes % 60 + "分" : ""}`;
    if (minutes) return `${minutes}分${seconds % 60 ? seconds % 60 + "秒" : ""}`;
    return `${seconds}秒`;
  }
  function analysisElapsed(run) {
    const elapsed = milliseconds(run?.elapsedMs);
    if (elapsed !== null) return elapsed;
    const started = milliseconds(run?.startedAt), finished = milliseconds(run?.finishedAt);
    if (started === null || finished === null && run?.state !== "running") return null;
    const end = finished ?? Date.now();
    return end >= started ? end - started : null;
  }
  function phaseEta(run) {
    const eta = run?.eta, low = milliseconds(eta?.lowMs), high = milliseconds(eta?.highMs);
    if (run?.state !== "running" || eta?.scope !== "phase" || eta?.basis !== "observed" || low === null || high === null || high < low || high <= 0) return "";
    const from = duration(low), to = duration(Math.ceil(high / 1000) * 1000);
    // The headline already names the running step, so the estimate stays short.
    return `约还需 ${from}${from === to ? "" : "–" + to}`;
  }
  function renderAnalysis() {
    const run = state.analysis, running = run?.state === "running", available = !!account() && !state.disabled;
    byId("analysisDepthSettings").textContent = DEPTH_NAMES[state.analysisDepth] || "插件设置";
    byId("analysisDepthSettings").title = state.analysisDepth ? DEPTHS[state.analysisDepth] + "；在插件设置中更改" : "在插件设置中更改";
    // Steps a run skips never execute, so the progress overlay lists only the work this run actually does.
    const steps = (run?.steps || []).filter(step => object(step) && step.state !== "skipped"), active = steps.find(step => step.state === "running" && step.key === run?.phase) || steps.find(step => step.state === "running");
    const labels = { idle: "", running: active ? string(active.title, 100) : "正在分析", done: "分析完成", partial: "部分步骤未完成", paused: "已暂停", interrupted: "分析已中断" };
    byId("analysisRun").textContent = running ? "正在分析…" : ["paused", "interrupted", "partial"].includes(run?.state) ? "继续分析" : run?.state === "done" ? "更新分析" : "开始分析";
    byId("analysisRun").disabled = !available || running || state.analysisBusy || state.outputsSettingsBusy || !state.outputsReady || !state.analysisOutputs.length;
    const choosing = !available || running || state.analysisBusy || state.outputsSettingsBusy;
    byId("analysisSplit").hidden = running; byId("analysisMenuToggle").disabled = choosing;
    if (!available || running) state.menuOpen = false;
    byId("analysisMenu").hidden = !state.menuOpen; byId("analysisMenuToggle").setAttribute("aria-expanded", String(state.menuOpen));
    byId("analysisPause").hidden = !running;
    byId("analysisPause").disabled = !available || state.analysisBusy || run?.stopping === true;
    byId("analysisPause").textContent = state.analysisAction === "pause" || run?.stopping === true ? "正在暂停…" : "暂停";
    for (const key of OUTPUTS) { const input = byId("analysisOutput-" + key); input.checked = state.analysisOutputs.includes(key); input.disabled = choosing; }
    const headline = state.analysisError || string(run?.error, 300) || labels[run?.state] || (run?.state === "idle" ? "" : available ? "读取状态…" : "请先连接微信账号");
    const elapsed = running ? analysisElapsed(run) : null;
    message("analysisMessage", [headline || (available && !state.analysisOutputs.length ? "请选择分析内容" : ""), elapsed !== null ? "已用时 " + duration(elapsed) : "", phaseEta(run)].filter(Boolean).join(" · "));
    byId("analysisMessage").className = state.analysisError || string(run?.error, 300) ? "error-text" : "";
    // The overlay opens by itself when a run starts and closes when it finishes cleanly; the status text reopens it.
    if (running && !state.wasRunning) state.panelOpen = true;
    if (!running && state.wasRunning && run?.state === "done") state.panelOpen = false;
    state.wasRunning = running;
    byId("analysisStatus").hidden = byId("analysisMessage").hidden; byId("analysisStatus").disabled = !steps.length;
    byId("analysisPanel").hidden = !state.panelOpen || !steps.length; byId("analysisStatus").setAttribute("aria-expanded", String(!byId("analysisPanel").hidden));
    const states = { pending: "等待", running: "进行中", done: "完成", error: "未完成", paused: "已暂停" };
    // Each step stays one short line; counts and diagnostics live in its tooltip, and only a running step shows a live count.
    byId("analysisSteps").replaceChildren(...steps.map(step => {
      const item = element("li", "analysis-step " + (Object.hasOwn(states, step.state) ? step.state : "pending"), "");
      item.appendChild(element("span", "step-title", string(step.title, 100) || "分析步骤"));
      item.appendChild(element("span", "step-state", states[step.state] || "等待"));
      const action = stepAction(step.key);
      if (action) { const button = element("button", "link step-action" + (step.stopping ? " stopping" : ""), step.stopping ? "正在暂停…" : action === "pause" ? "暂停" : "继续"); button.type = "button"; button.id = `analysisStep${action === "pause" ? "Pause" : "Resume"}-${step.key}`; button.setAttribute("aria-label", `${action === "pause" ? "暂停" : "继续"}${string(step.title, 100) || "此项分析"}`); button.disabled = !available || state.analysisBusy || state.outputsSettingsBusy || run?.stopping === true || step.stopping === true; button.addEventListener("click", () => { void analysisCall(action === "pause", step.key); }); item.appendChild(button); }
      const progress = stepProgress(step), live = running && step.state === "running";
      if (live && progress.text) item.appendChild(element("span", "step-metric", progress.text));
      let reading = "";
      if (step.key === "history") {
        const read = count(step.sourceMessagesRead), verified = count(step.sourceMessagesVerified), chatDone = count(step.currentChatDone), chatTotal = count(step.currentChatTotal);
        reading = [read !== null ? `本轮读取 ${read} 条` : "", verified > 0 ? `已核对 ${verified} 条旧记录` : "",
          chatDone !== null && chatTotal > 0 && chatDone <= chatTotal ? `当前聊天 ${chatDone} / ${chatTotal} 条` : ""].filter(Boolean).join(" · ");
      }
      const calls = count(step.modelCalls) ?? count(step.completedCallCount) ?? count(step.successfulCallCount), activeStarted = milliseconds(step.activeCallStartedAt);
      const activity = [calls > 0 ? `已调用 ${calls} 次` : "", live && activeStarted > 0 && activeStarted <= Date.now() ? "当前请求已用 " + duration(Date.now() - activeStarted) : ""].filter(Boolean).join(" · ");
      const raw = string(step.error || step.detail, 400), detail = raw && !["已有结果已保存", "已完成", "完成", "已就绪", "等待处理", "未选择"].includes(raw) ? raw : "";
      if (detail && step.state === "error" && detail !== headline) item.appendChild(element("span", "step-error", detail));
      const tip = [progress.text, reading, activity, detail].filter(Boolean).join("\n");
      if (tip) item.setAttribute("title", tip);
      return item;
    }));
    const progress = byId("analysisProgress"), currentProgress = stepProgress(active); progress.hidden = !running || !currentProgress.known;
    progress.setAttribute("aria-label", active ? string(active.title, 100) + "进度" : "当前阶段进度");
    if (!progress.hidden) { progress.max = currentProgress.total; progress.value = currentProgress.done; } else progress.removeAttribute("value");
    renderProfile();
    if (state.tab === "values") renderWorldviewNotice();
  }
  function validAnalysis(value) { return object(value) && ["idle", "running", "done", "partial", "paused", "interrupted"].includes(value.state) && (value.steps === undefined || Array.isArray(value.steps)); }
  const stepOutput = key => key === "semantic" ? "library" : key === "timeline-extract" ? "timeline" : key;
  function stepAction(key) {
    const run = state.analysis, step = run?.steps?.find(item => item?.key === key), output = stepOutput(key);
    if (!OUTPUTS.includes(output) || !step || step.state === "skipped" || !(selectedOutputs(run.outputs) || state.analysisOutputs).includes(output)) return "";
    if (step.state === "paused" || run.pausedOutputs?.includes(output) && step.state !== "done") return "resume";
    if (run.state === "running" && ["pending", "running"].includes(step.state)) return "pause";
    return ["paused", "partial", "interrupted"].includes(run.state) && ["pending", "running", "error"].includes(step.state) ? "resume" : "";
  }
  function acceptAnalysis(run, outputsVersion = state.outputsVersion, explicit = false) {
    const ended = state.analysis?.state === "running" && run.state !== "running";
    state.analysis = run;
    const outputs = selectedOutputs(run.outputs);
    if (outputs && (explicit || run.state === "running" || !state.outputsSettingsLoaded && outputsVersion === state.outputsVersion && !state.outputsSettingsBusy)) { state.analysisOutputs = outputs; if (explicit || run.state === "running") state.outputsVersion++; }
    // A run shows its own items while it works; a one-item continue must not leave the choices narrowed afterwards.
    if (ended) void loadAnalysisSettings();
  }
  function refreshAnalysisResults() { void loadStatus(); void loadDraftState(); void loadTags(); for (const kind of Object.keys(OPINIONS)) void loadOpinion(kind); void loadPeriods(); void loadWorldview(); }
  async function loadAnalysis() {
    if (!account() || state.disabled || state.analysisBusy || state.analysisPending) return;
    const epoch = state.epoch, original = account(), serial = ++state.analysisSerial, outputsVersion = state.outputsVersion;
    state.analysisPending = true;
    try {
      const run = await window.kyfHost.rpc("self.analysisStatus", {}, scope());
      if (!current(epoch, original) || serial !== state.analysisSerial) return;
      if (!validAnalysis(run)) throw new Error("分析状态暂时无法读取，请重试。");
      acceptAnalysis(run, outputsVersion); state.analysisError = ""; if (run.state !== "idle") refreshAnalysisResults();
    } catch (error) { if (current(epoch, original) && serial === state.analysisSerial) state.analysisError = friendly(error); }
    finally { if (current(epoch, original) && serial === state.analysisSerial) { state.analysisPending = false; renderAnalysis(); scheduleAnalysis(); } }
  }
  async function analysisCall(pausing = false, stepKey = "") {
    const output = stepOutput(stepKey);
    if (!account() || state.disabled || state.analysisBusy || state.outputsSettingsBusy || stepKey && stepAction(stepKey) !== (pausing ? "pause" : "resume") || !pausing && !output && (!state.outputsReady || state.analysis?.state === "running")) return;
    const outputs = output ? [output] : [...state.analysisOutputs];
    if (!pausing && !outputs.length) return;
    const background = byId("background").value, style = byId("style").value;
    if (!pausing && state.profileReady && (background.length > 4000 || style.length > 4000)) { state.analysisError = "本人资料每栏最多 4000 字，请缩短后再分析。"; renderAnalysis(); return; }
    const profile = state.profileReady ? { background, style } : {}, params = { outputs, ...profile };
    // Continuing one item always goes through analysisResume, which starts that item alone without replacing the saved choices.
    const resuming = !pausing && !!output, resumeParams = state.analysis?.state === "running" ? { output } : { output, ...profile };
    const epoch = state.epoch, original = account(), serial = ++state.analysisSerial;
    state.analysisBusy = true; state.analysisAction = pausing && !output ? "pause" : ""; state.analysisPending = false; state.analysisError = ""; cancelAnalysisTimer(); renderAnalysis();
    try {
      const run = await window.kyfHost.rpc(pausing ? "self.analysisPause" : resuming ? "self.analysisResume" : "self.analyze", pausing ? output ? { output } : {} : resuming ? resumeParams : params, scope());
      if (!current(epoch, original) || serial !== state.analysisSerial) return;
      if (!validAnalysis(run)) throw new Error("分析任务状态暂时无法确认，请重新查看。");
      acceptAnalysis(run, state.outputsVersion, true); refreshAnalysisResults();
    } catch (error) {
      if (!current(epoch, original) || serial !== state.analysisSerial) return;
      if (error?.code === "api-not-configured") { state.modelConfigured = false; renderModel(); }
      state.analysisError = friendly(error);
    } finally { if (current(epoch, original) && serial === state.analysisSerial) { state.analysisBusy = false; state.analysisAction = ""; renderAnalysis(); scheduleAnalysis(); } }
  }
  async function loadAnalysisSettings() {
    if (!account() || state.disabled) return;
    const epoch = state.epoch, original = account(), serial = state.outputsSettingsSerial, outputsVersion = state.outputsVersion;
    try {
      const result = await window.kyfHost.rpc("self.analysisSettings", {}, scope()), outputs = selectedOutputs(result?.outputs);
      if (current(epoch, original) && validDepth(result?.depth)) state.analysisDepth = result.depth;
      if (current(epoch, original) && serial === state.outputsSettingsSerial && outputsVersion === state.outputsVersion && state.analysis?.state !== "running" && outputs) { state.analysisOutputs = outputs; state.outputsSettingsLoaded = true; renderAnalysis(); }
    } catch { /* Saved results remain usable when preferences are unavailable. */ }
    finally { if (current(epoch, original)) { state.outputsReady = true; renderAnalysis(); } }
  }
  async function changeAnalysisOutputs() {
    if (!account() || state.disabled || state.analysisBusy || state.outputsSettingsBusy || state.analysis?.state === "running") return;
    const outputs = OUTPUTS.filter(key => byId("analysisOutput-" + key).checked), previous = state.analysisOutputs;
    const epoch = state.epoch, original = account(), serial = ++state.outputsSettingsSerial;
    state.analysisOutputs = outputs; state.outputsReady = true; state.outputsVersion++; state.outputsSettingsLoaded = true; state.outputsSettingsBusy = true; state.analysisError = ""; renderAnalysis();
    try {
      const result = await window.kyfHost.rpc("self.analysisSettings", { outputs }, scope());
      if (!current(epoch, original) || serial !== state.outputsSettingsSerial) return;
      if (JSON.stringify(selectedOutputs(result?.outputs)) !== JSON.stringify(outputs)) throw new Error("分析项目未保存，请重试。");
    } catch (error) { if (current(epoch, original) && serial === state.outputsSettingsSerial) { state.analysisOutputs = previous; state.analysisError = friendly(error); } }
    finally { if (current(epoch, original) && serial === state.outputsSettingsSerial) { state.outputsSettingsBusy = false; renderAnalysis(); } }
  }
  async function loadDraftState() {
    if (!account() || state.disabled || !state.profileReady) return;
    const epoch = state.epoch, original = account(), serial = ++state.draftSerial;
    try {
      const job = await window.kyfHost.rpc("self.draftStatus", {}, scope());
      if (!current(epoch, original) || serial !== state.draftSerial || !object(job) || !["running", "done", "error", "cancelled", "paused"].includes(job.state)) return;
      state.draftJob = job; renderDraftCoverage(job);
      if (job.state === "running") renderDraftProgress(job);
      else if (job.state === "done") {
        const key = string(job.id) + JSON.stringify(job.result);
        if (state.deliveredDraft !== key) { state.deliveredDraft = key; deliverDraft(job); }
      } else if (job.state === "error") message("profileError", string(job.error, 200) || "本人资料初稿未完成，可从顶部继续分析。");
    } catch { if (current(epoch, original) && serial === state.draftSerial) { byId("draftProgress").hidden = true; message("profileMessage", ""); message("profileError", "分析状态暂时无法读取，已保存的进度保留。再次显示窗口可重新查看。"); } }
    finally { if (current(epoch, original) && serial === state.draftSerial) renderProfile(); }
  }
  async function mutate(method, params = {}) {
    if (!account() || state.busy || state.disabled) return;
    const epoch = state.epoch, original = account();
    state.busy = true; state.statusSerial++; state.statusPending = false; cancelTimer(); render(); message("error", "");
    if (method === "self.reset") { state.searchSerial++; state.exclusionSerial++; state.exclusionsBusy = false; byId("samples").replaceChildren(); byId("query").value = ""; state.searching = false; }
    try {
      const value = await window.kyfHost.rpc(method, params, scope());
      if (!current(epoch, original)) return;
      if (validStatus(value)) state.status = value;
      else throw new Error("操作结果暂时无法确认，请刷新状态核对。");
      byId("resetConfirm").hidden = true;
      if (method === "self.reset") { byId("excludedSamples").replaceChildren(); byId("exclusionsPanel").hidden = true; message("searchMessage", ""); }
      return true;
    } catch (error) { if (current(epoch, original)) message("error", friendly(error)); return false; }
    finally { if (current(epoch, original)) { state.busy = false; render(); schedule(); } }
  }
  function element(tag, className, text) { const node = document.createElement(tag); node.className = className; node.textContent = text; return node; }
  function sampleCard(source, excluded = false, editable = true) {
    const card = element("article", "sample", ""), head = element("div", "sample-head", "");
    const who = string(source.name || source.displayName, 128) || "其他私聊";
    const date = Number.isFinite(source.timeMs) ? new Date(source.timeMs).toLocaleDateString("zh-CN") : "";
    head.appendChild(element("p", "sample-meta", `${who}${date ? " · " + date : ""}`));
    if (!excluded && Array.isArray(source.matchKinds) && source.matchKinds.includes("semantic")) head.appendChild(element("span", "sample-match", source.matchKinds.includes("lexical") ? "词语与含义相近" : "含义相近"));
    if (editable && typeof source.id === "string" && source.id) {
      const button = element("button", "plain", excluded ? "恢复" : "排除"); button.type = "button";
      button.title = excluded ? "恢复学习这条回复" : "不再学习这条回复";
      const epoch = state.epoch, original = account(), list = excluded ? "excludedSamples" : "samples";
      button.addEventListener("click", async () => {
        if (!current(epoch, original) || state.busy) return;
        button.disabled = true;
        const saved = await mutate("self.exclude", { id: source.id, excluded: !excluded });
        if (!current(epoch, original)) return;
        button.disabled = false;
        if (!saved) return;
        byId(list).replaceChildren(...[...byId(list).children].filter(node => node !== card));
        if (excluded) byId("exclusionsPanel").hidden = !byId(list).children.length;
        else message("searchMessage", "已排除，可在“表达知识库”恢复。");
        if (!excluded && state.tab === "build") void loadExclusions();
      });
      head.appendChild(button);
    }
    card.appendChild(head);
    // Stored context marks the account's own lines as 本人; the page speaks as 我.
    card.appendChild(element("p", "sample-text", string(source.input, 6000).replace(/^本人：/gm, "我：") || "（未保存前文）"));
    card.appendChild(element("p", "sample-reply", "我：" + string(source.reply, 6000)));
    return card;
  }
  async function loadExclusions() {
    if (!account() || state.exclusionsBusy || state.disabled) return;
    const epoch = state.epoch, original = account(), serial = ++state.exclusionSerial;
    state.exclusionsBusy = true; render();
    try {
      const result = await window.kyfHost.rpc("self.exclusions", {}, scope());
      if (!current(epoch, original) || serial !== state.exclusionSerial) return;
      if (!object(result) || !Array.isArray(result.sources)) throw new Error("已排除样本暂时无法读取，请重试。");
      byId("excludedSamples").replaceChildren();
      for (const source of result.sources) if (object(source) && typeof source.reply === "string") byId("excludedSamples").appendChild(sampleCard(source, true));
      message("exclusionMessage", "");
      byId("exclusionsPanel").hidden = !byId("excludedSamples").children.length;
    } catch (error) { if (current(epoch, original) && serial === state.exclusionSerial) { message("exclusionMessage", friendly(error)); byId("exclusionsPanel").hidden = false; } }
    finally { if (current(epoch, original) && serial === state.exclusionSerial) { state.exclusionsBusy = false; render(); } }
  }
  function showSamples(sources, summary) {
    byId("samples").replaceChildren();
    for (const source of sources.slice(0, 16)) if (object(source) && typeof source.reply === "string") byId("samples").appendChild(sampleCard(source));
    message("searchMessage", summary(byId("samples").children.length));
  }
  async function search() {
    if (!account() || state.searching || byId("search").disabled) return;
    const query = byId("query").value.trim();
    if (!query) { byId("query").focus(); return; }
    const epoch = state.epoch, original = account(), serial = ++state.searchSerial;
    state.searching = true; render(); message("searchMessage", "正在检索…");
    try {
      const result = await window.kyfHost.rpc("self.search", { query, limit: 6 }, scope());
      if (!current(epoch, original) || serial !== state.searchSerial) return;
      if (!object(result) || !Array.isArray(result.sources)) throw new Error("历史回复暂时无法读取，请重试。");
      const partial = result.mode === "hybrid" && result.semanticPartial === true;
      showSamples(result.sources, found => !found ? "没有找到相近的回复，换个说法试试。" : partial ? "语义索引尚未完成，结果可能不全。" : "");
    } catch (error) { if (current(epoch, original) && serial === state.searchSerial) message("searchMessage", friendly(error)); }
    finally { if (current(epoch, original) && serial === state.searchSerial) { state.searching = false; render(); } }
  }
  function cancelTrialTimer() { if (state.trialTimer !== null) clearTimeout(state.trialTimer); state.trialTimer = null; }
  function renderChat() {
    // Consecutive messages from one side share an avatar, as in a WeChat chat window.
    const log = byId("chatLog"), groups = [];
    for (const turn of state.turns) {
      if (groups.at(-1)?.from === turn.from) groups.at(-1).bubbles.push(element("p", "bubble", turn.text));
      else groups.push({ from: turn.from, bubbles: [element("p", "bubble", turn.text)] });
    }
    const status = state.replying ? element("p", "bubble pending", "正在输入…") : state.replyError ? element("p", "bubble failed", state.replyError) : null;
    if (status) { if (groups.at(-1)?.from === "me") groups.at(-1).bubbles.push(status); else groups.push({ from: "me", bubbles: [status] }); }
    const empty = byId("chatEmpty"); empty.hidden = groups.length > 0;
    log.replaceChildren(empty, ...groups.map(group => {
      const row = element("div", "turn " + group.from, ""), bubbles = element("div", "bubbles", "");
      row.appendChild(element("span", "turn-avatar", group.from === "me" ? "我" : "友"));
      for (const bubble of group.bubbles) bubbles.appendChild(bubble);
      row.appendChild(bubbles); return row;
    }));
    log.scrollTop = log.scrollHeight;
  }
  function clearTrial() {
    state.trialSerial++; cancelTrialTimer();
    Object.assign(state, { turns: [], replying: false, replyError: "" });
    message("trialMessage", ""); renderChat(); render();
  }
  async function sendTrial() {
    if (!account() || state.replying || byId("trialSend").disabled) return;
    // 换行分条：一次输入按行拆成连续几条好友消息，和真实聊天里对方连发一样。
    const texts = byId("trialInput").value.split("\n").map(item => item.trim()).filter(Boolean);
    if (!texts.length) { byId("trialInput").focus(); return; }
    const epoch = state.epoch, original = account(), serial = ++state.trialSerial;
    for (const text of texts) state.turns.push({ from: "friend", text: string(text, 1000) });
    byId("trialInput").value = "";
    Object.assign(state, { replying: true, replyError: "" }); message("trialMessage", ""); renderChat(); render();
    const live = () => current(epoch, original) && serial === state.trialSerial;
    try {
      let job = await window.kyfHost.rpc("self.reply", { messages: state.turns.slice(-MAX_TURNS) }, scope());
      const id = job?.id;
      while (live() && object(job) && job.state === "running" && job.id === id) {
        await new Promise(resolve => { state.trialTimer = setTimeout(() => { state.trialTimer = null; resolve(); }, 1000); });
        if (!live()) return;
        job = await window.kyfHost.rpc("self.replyStatus", {}, scope());
      }
      if (!live()) return;
      const messages = job?.id === id && job.state === "done" && Array.isArray(job.result?.messages) ? job.result.messages.filter(item => typeof item === "string" && item.trim()) : [];
      if (!messages.length) { state.replyError = job?.state === "error" && string(job.error, 200) || "回复生成未完成，请重试。"; return; }
      for (const item of messages.slice(0, 6)) state.turns.push({ from: "me", text: string(item, 500) });
      // The left pane shows what this reply drew on, replacing any earlier search.
      if (Array.isArray(job.sources)) {
        state.searchSerial++; state.searching = false;
        const researched = job.research > 0 ? `，另参考时间线 ${job.research} 条` : "";
        showSamples(job.sources, found => found
          ? `这条回复参考了 ${found} 条历史回复${researched}` : "这条回复没有找到相近的历史回复");
      }
    } catch (error) {
      if (!live()) return;
      if (error?.code === "api-not-configured") { state.modelConfigured = false; renderModel(); }
      state.replyError = friendly(error);
    } finally { if (live()) { state.replying = false; renderChat(); render(); } }
  }
  const TAG_PHASES = { reading: "正在读取聊天", extracting: "正在提取标签", merging: "正在合并重复标签" };
  const TAG_LEVELS = ["confirmed", "strong", "possible"];
  const TAG_RELATIONS = { mention: "仅提及", occasional: "偶尔参与", interest: "明确兴趣", trait: "明确特征", uncertain: "待判断" };
  const TAG_CONFIDENCE = { confirmed: "已确认", strong: "较可靠", possible: "可能" };
  const tagRelation = tag => tag.manual === true ? "手动添加" : Object.hasOwn(TAG_RELATIONS, tag.relation) ? TAG_RELATIONS[tag.relation] : TAG_RELATIONS.uncertain;
  const validTags = value => object(value) && value.account === account() && object(value.run) && object(value.counts) && Array.isArray(value.tags);
  const selectedTag = () => (state.tags?.tags || []).find(tag => object(tag) && tag.key === state.tagSelected) || null;
  async function loadTags() {
    if (!account() || state.disabled || state.tagPending || state.tagBusy) return;
    const epoch = state.epoch, original = account(), serial = ++state.tagSerial;
    state.tagPending = true;
    try {
      const value = await window.kyfHost.rpc("self.tags", {}, scope());
      if (!current(epoch, original) || serial !== state.tagSerial) return;
      if (!validTags(value)) throw new Error("标签暂时无法读取，请稍后重试。");
      state.tags = value; state.tagError = "";
    } catch (error) { if (current(epoch, original) && serial === state.tagSerial) state.tagError = friendly(error); }
    finally { if (current(epoch, original) && serial === state.tagSerial) { state.tagPending = false; renderTags(); } }
  }
  // One path for analysing, pausing and editing: each returns the whole tag list, so the page never merges partial state.
  async function tagCall(method, params = {}) {
    if (!account() || state.tagBusy || state.disabled) return false;
    const epoch = state.epoch, original = account();
    state.tagSerial++; state.tagPending = false; state.tagBusy = true; state.tagError = ""; renderTags();
    try {
      const value = await window.kyfHost.rpc(method, params, scope());
      if (!current(epoch, original)) return false;
      if (!validTags(value)) throw new Error("标签暂时无法读取，请稍后重试。");
      state.tags = value; return true;
    } catch (error) {
      if (!current(epoch, original)) return false;
      if (error?.code === "api-not-configured") { state.modelConfigured = false; renderModel(); }
      state.tagError = friendly(error); return false;
    } finally { if (current(epoch, original)) { state.tagBusy = false; renderTags(); } }
  }
  function tagStats(tag) {
    const parts = [string(tag.category, 20)], date = ms => new Date(ms).toLocaleDateString("zh-CN");
    const confidence = Object.hasOwn(TAG_CONFIDENCE, tag.level) ? TAG_CONFIDENCE[tag.level] : TAG_CONFIDENCE.possible;
    if (tag.manual) parts.push(confidence, tagRelation(tag));
    else {
      parts.push(tagRelation(tag), confidence);
      parts.push(`关联消息 ${count(tag.mentions) ?? 0} 条`, `${count(tag.chats) ?? 0} 个聊天`);
      if (Number.isFinite(tag.firstMs) && Number.isFinite(tag.lastMs)) parts.push(date(tag.firstMs) === date(tag.lastMs) ? date(tag.lastMs) : `${date(tag.firstMs)}–${date(tag.lastMs)}`);
    }
    return parts.join(" · ");
  }
  function renderTags() {
    const value = state.tags, run = object(value?.run) ? value.run : {}, tags = (value?.tags || []).filter(tag => object(tag) && typeof tag.name === "string");
    const running = run.state === "running", available = !!account() && !state.disabled, done = count(run.done), total = count(run.total);
    let text = state.tagError;
    if (!text && running) text = run.stopping ? "正在暂停…" : `${TAG_PHASES[run.phase] || "正在分析"}${total ? ` ${Math.min(done ?? 0, total)} / ${total}` : "…"}`;
    else if (!text && run.state === "paused") text = "已暂停，进度已保留";
    else if (!text && run.state === "failed") text = string(run.error, 200) || "标签分析未完成，请重试";
    message("tagsMessage", text);
    const progress = byId("tagsProgress"); progress.hidden = !running || !total;
    if (!progress.hidden && done !== null) { progress.max = total; progress.value = Math.min(done, total); } else progress.removeAttribute("value");
    byId("tagsStatus").hidden = !text && progress.hidden;
    byId("tagsReview").hidden = run.needsReview !== true;
    const empty = !!value && !tags.length && !running, resumable = ["paused", "failed"].includes(run.state);
    byId("tagsEmpty").hidden = !empty;
    byId("tagsEmptyText").textContent = run.state === "complete" ? "没有找到标签，可以手动添加" : "从顶部开始分析，区分聊天中的提及、参与、兴趣与个人特征";
    byId("tagsStart").textContent = resumable ? "继续分析" : run.state === "complete" ? "重新检查" : "分析标签";
    byId("tagsStart").disabled = !available || state.tagBusy;
    byId("tagsRun").hidden = !value || empty;
    byId("tagsRun").textContent = running ? "暂停" : resumable ? "继续" : "更新";
    byId("tagsRun").disabled = !available || state.tagBusy || run.stopping === true;
    byId("tagsRun").hidden = byId("tagsStart").hidden = true;
    byId("addTag").disabled = !available || state.tagBusy || !value;
    byId("saveTag").disabled = !available || state.tagBusy;
    const select = byId("tagCategory");
    if (!select.children.length && Array.isArray(value?.categories)) {
      for (const name of value.categories.filter(item => typeof item === "string")) { const option = element("option", "", name); option.value = name; select.appendChild(option); }
      select.value = "其他";
    }
    const groups = new Map();
    for (const tag of tags) { const key = string(tag.category, 20) || "其他"; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(tag); }
    byId("tagList").replaceChildren(...[...groups].map(([category, items]) => {
      const group = element("section", "tag-group", ""), chips = element("div", "chips", "");
      group.appendChild(element("h3", "", category));
      for (const tag of items) {
        const level = TAG_LEVELS.includes(tag.level) ? tag.level : "possible", chip = element("button", "chip " + level, string(tag.name, 20));
        chip.type = "button"; chip.title = `${tagRelation(tag)} · ${TAG_CONFIDENCE[level]}`;
        chip.setAttribute("aria-pressed", String(tag.key === state.tagSelected));
        chip.appendChild(element("span", "chip-relation", tagRelation(tag)));
        if (count(tag.mentions)) chip.appendChild(element("span", "chip-count", `${tag.mentions} 条`));
        chip.addEventListener("click", () => { state.tagSelected = tag.key; state.tagUndo = ""; renderTags(); void loadEvidence(); });
        chips.appendChild(chip);
      }
      group.appendChild(chips); return group;
    }));
    renderTagDetail();
  }
  function renderTagDetail() {
    const tag = selectedTag();
    byId("tagTitle").textContent = tag ? string(tag.name, 20) : "依据";
    byId("tagActions").hidden = !tag;
    byId("confirmTag").hidden = !tag || tag.manual === true;
    byId("confirmTag").textContent = tag?.level === "confirmed" ? "取消确认" : "确认";
    byId("confirmTag").disabled = byId("deleteTag").disabled = byId("undoTag").disabled = state.tagBusy || !account();
    message("tagStats", tag ? tagStats(tag) : "");
    byId("tagUndoText").textContent = state.tagUndo ? `已删除“${state.tagUndo}”` : ""; byId("tagUndo").hidden = !state.tagUndo;
    byId("tagDetailEmpty").hidden = !!tag || !!state.tagUndo;
    if (!tag) { state.evidenceSerial++; byId("tagEvidence").replaceChildren(); }
  }
  function evidenceCard(item) {
    const card = element("article", "sample", ""), head = element("div", "sample-head", "");
    const date = Number.isFinite(item.timeMs) ? new Date(item.timeMs).toLocaleDateString("zh-CN") : "";
    head.appendChild(element("p", "sample-meta", `${string(item.chat, 128) || "聊天"}${item.isGroup === true ? "（群）" : ""}${date ? " · " + date : ""} · ${tagRelation(item)}`));
    card.appendChild(head);
    if (string(item.context)) card.appendChild(element("p", "sample-text", string(item.context, 2000)));
    card.appendChild(element("p", "sample-reply", "我：" + string(item.text, 1200)));
    const followup = Array.isArray(item.followup) ? item.followup.filter(line => typeof line === "string").join("\n") : string(item.followup, 1000);
    if (followup) card.appendChild(element("p", "sample-text", "后续：\n" + followup));
    if (string(item.quote)) card.appendChild(element("p", "tag-interpretation", "支持原话：" + string(item.quote, 1200)));
    if (string(item.reason)) card.appendChild(element("p", "tag-interpretation", "判断理由：" + string(item.reason, 1000)));
    if (item.truncated === true) card.appendChild(element("p", "tag-interpretation", "原文过长，本次仅分析部分文字，语义待判断。"));
    return card;
  }
  async function loadEvidence() {
    const tag = selectedTag(), serial = ++state.evidenceSerial;
    byId("tagEvidence").replaceChildren();
    if (!tag || tag.manual === true || !account()) return;
    const epoch = state.epoch, original = account();
    try {
      const result = await window.kyfHost.rpc("self.tagEvidence", { name: tag.name }, scope());
      if (!current(epoch, original) || serial !== state.evidenceSerial) return;
      if (!object(result) || !Array.isArray(result.items)) throw new Error("依据暂时无法读取，请重试。");
      const items = result.items.filter(object);
      byId("tagEvidence").replaceChildren(...items.map(evidenceCard));
      if (count(result.total) > items.length) byId("tagEvidence").appendChild(element("p", "help", `共 ${result.total} 条，显示最近 ${items.length} 条`));
    } catch (error) { if (current(epoch, original) && serial === state.evidenceSerial) byId("tagEvidence").replaceChildren(element("p", "help", friendly(error))); }
  }
  function selectTagNamed(name) {
    const tag = (state.tags?.tags || []).find(item => object(item) && item.name === name);
    state.tagSelected = tag ? tag.key : ""; renderTags(); void loadEvidence();
  }
  // ── 时间线：顶部总览条 + 按年分组的事件列表；用户修改以本人为准（doc 5）──────────────
  const TL_STATUS = { occurred: "已发生", planned: "计划", cancelled: "已取消", uncertain: "待判断" };
  const TL_STATUS_SHORT = { occurred: "", planned: "计划", cancelled: "已取消", uncertain: "待判断" };
  const TL_LEVELS = { confirmed: "已确认", strong: "较可靠", possible: "可能" };
  const TL_KINDS = { point: "事件", start: "开始", end: "结束", state: "状态" };
  const TL_COLORS = ["#07c160", "#576b95", "#c2841c", "#b05a4a", "#7f77dd", "#3d8f8a", "#9a6fb0", "#7b8a4e"];
  const validTimeline = value => object(value) && Array.isArray(value.events) && Array.isArray(value.periods) &&
    Array.isArray(value.tracks) && typeof value.version === "number";
  const trackColor = name => (state.timeline?.tracks || []).find(track => track.name === name)?.color || TL_COLORS[0];
  const trackNames = () => (state.timeline?.tracks || []).map(track => track.name);
  const hasMonth = event => ["day", "month", "season"].includes(event.precision);
  const monthLabel = event => Number.isFinite(event.startMs) && hasMonth(event) ? `${new Date(event.startMs).getMonth() + 1}月` : "—";
  const fullDate = ms => new Date(ms).toLocaleDateString("zh-CN");
  const isImportant = event => event.importance === "important";
  const WHEN_HINT = "时间可写成 2024、2024.05 或 2024.05.01";
  // 分隔符不挑：2024.05、2024/5、2024-05、2024年5月、202405 都认；日期要真实存在。
  function parseWhenInput(text) {
    const compact = (text || "").trim().replace(/[./．／。、年月\s]+/g, "-").replace(/[日号]/g, "").replace(/^-+|-+$/g, "");
    const digits = /^(\d{4})(\d{2})(\d{2})?$/.exec(compact);
    const match = digits || /^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$/.exec(compact);
    if (!match) return null;
    const year = +match[1], month = match[2] ? +match[2] : null, day = match[3] ? +match[3] : null;
    if (year < 1970 || year > 2099 || month !== null && (month < 1 || month > 12)) return null;
    if (day !== null) {
      const start = new Date(year, month - 1, day);
      if (start.getMonth() !== month - 1) return null;
      return { startMs: start.getTime(), endMs: new Date(year, month - 1, day + 1).getTime() - 1, precision: "day" };
    }
    if (month !== null) return { startMs: new Date(year, month - 1, 1).getTime(), endMs: new Date(year, month, 1).getTime() - 1, precision: "month" };
    return { startMs: new Date(year, 0, 1).getTime(), endMs: new Date(year + 1, 0, 1).getTime() - 1, precision: "year" };
  }
  // 事件分页返回（单次回复有大小上限）；续读中版本或总数变了就从头再读。
  async function fullTimeline(value) {
    for (let attempt = 0; ; attempt++) {
      if (!validTimeline(value)) throw new Error("时间线暂时无法读取，请稍后重试。");
      const events = [...value.events];
      let next = value.nextOffset, changed = false;
      while (Number.isInteger(next) && !changed) {
        const page = await window.kyfHost.rpc("self.timeline", { offset: next }, scope());
        changed = !validTimeline(page) || page.version !== value.version || page.eventsTotal !== value.eventsTotal ||
          !page.events.length;
        if (!changed) { events.push(...page.events); next = page.nextOffset; }
      }
      if (!changed) return { ...value, events };
      if (attempt >= 2) throw new Error("时间线正在更新，请稍后重试。");
      value = await window.kyfHost.rpc("self.timeline", {}, scope());
    }
  }
  async function loadTimeline() {
    if (!account() || state.disabled || state.timelinePending || state.timelineBusy) return;
    const epoch = state.epoch, original = account(), serial = ++state.timelineSerial;
    state.timelinePending = true; renderTimeline();
    try {
      const value = await fullTimeline(await window.kyfHost.rpc("self.timeline", {}, scope()));
      if (!current(epoch, original) || serial !== state.timelineSerial) return;
      state.timeline = value; state.timelineError = "";
    } catch (error) { if (current(epoch, original) && serial === state.timelineSerial) state.timelineError = friendly(error); }
    finally { if (current(epoch, original) && serial === state.timelineSerial) { state.timelinePending = false; renderTimeline(); } }
  }
  async function timelineWrite(method, params) {
    if (!account() || state.timelineBusy || state.disabled) return null;
    const epoch = state.epoch, original = account();
    state.timelineSerial++; state.timelinePending = false; state.timelineBusy = true; state.timelineError = ""; state.timelineNotice = ""; renderTimeline();
    let conflict = false;
    try {
      const edited = await window.kyfHost.rpc(method, { ...params, expectedVersion: state.timeline?.version }, scope());
      if (!current(epoch, original)) return null;
      const value = await fullTimeline(edited);
      if (!current(epoch, original)) return null;
      state.timeline = value; return edited;
    } catch (error) {
      if (!current(epoch, original)) return null;
      conflict = ["conflict", "version-conflict"].includes(error?.code);
      state.timelineError = friendly(error);
      return null;
    } finally {
      if (current(epoch, original)) {
        state.timelineBusy = false; renderTimeline();
        // 外部 Agent 也会改时间线：版本对不上时读回最新内容，用户重做一次就好。
        if (conflict) void loadTimeline().then(() => { state.timelineError = "时间线刚被修改过，已刷新，请重试。"; renderTimeline(); });
      }
    }
  }
  const timelineCall = async params => !!await timelineWrite("self.timelineEdit", params);
  // 连按 Ctrl+Z 时前一次还在写入，排队按顺序撤销，一次都不丢。
  async function undoTimeline() {
    if (!state.timeline) return;
    if (state.timelineBusy) { state.timelineUndoQueued++; return; }
    const undone = await timelineWrite("self.timelineUndo", {});
    if (undone) { state.timelineNotice = undone.undone ? `已撤销：${undone.undone}` : "没有可撤销的修改"; renderTimeline(); }
    if (state.timelineUndoQueued) { state.timelineUndoQueued--; if (undone) void undoTimeline(); else state.timelineUndoQueued = 0; }
  }
  // 行内修改先改本地显示，失败再改回来，这样点击后不用等写入完成。
  async function patchTimelineEvent(event, fields) {
    const before = Object.fromEntries(Object.keys(fields).map(key => [key, event[key]]));
    Object.assign(event, fields);
    if (await timelineCall({ target: "event", action: "edit", id: event.id, fields })) return;
    Object.assign(event, before); renderTimeline();
  }
  function renderTimeline() {
    const value = state.timeline, available = !!account() && !state.disabled;
    byId("timelineAdd").disabled = !available || state.timelineBusy;
    byId("timelineImportantOnly").disabled = !available;
    const run = object(value?.run) ? value.run : {};
    let text = state.timelineError;
    // 首次读取还没返回时先显示“正在读取”，不显示空状态，避免误以为没有信息。
    if (!text && !value && state.timelinePending) text = "正在读取…";
    if (!text && ["running", "paused", "failed"].includes(run.state)) {
      text = run.state === "running" ? "正在分析时间线…" : run.state === "paused" ? "时间线分析已暂停，进度已保留" : string(run.error, 200) || "时间线分析未完成，请重试";
    }
    message("timelineMessage", text);
    const empty = !value || (!value.events.length && !value.periods.length && ["idle", "done", "complete"].includes(run.state));
    byId("timelineEmpty").hidden = !empty || !!text;
    byId("timelineAddForm").hidden = state.timelineAddType === null;
    renderTimelineAddForm();
    renderTimelineOverview();
    renderTimelineTracks();
    renderTimelinePeriodBar();
    renderTimelineList();
  }
  // 时间线按插件设置的时间颗粒度分段，段号是从公元 0 年算起的第几段，相邻两段差 1。
  const grainMonths = () => state.timeline?.months || 12;
  function timelineBucket(ms) { const day = new Date(ms); return Math.floor((day.getFullYear() * 12 + day.getMonth()) / grainMonths()); }
  function bucketName(bucket, short = false) {
    const months = grainMonths(), start = bucket * months, year = Math.floor(start / 12), month = start % 12;
    if (months === 12) return String(year);
    if (months > 12) { const last = year + months / 12 - 1; return short ? `${year}–${String(last).slice(2)}` : `${year}–${last}`; }
    if (months === 6) return short ? `${String(year).slice(2)}${month ? "下" : "上"}` : `${year} ${month ? "下" : "上"}半年`;
    return short ? `${String(year).slice(2)}Q${month / 3 + 1}` : `${year} 第${"一二三四"[month / 3]}季度`;
  }
  // 总览按“主线 × 时间段”画成格子，颜色深浅表示这一段的事件多少；连续三段以上全空的收成一格“…”。
  function timelineYears(value, counted) {
    const years = new Set(counted.map(event => timelineBucket(event.startMs)));
    for (const period of value.periods) if (Number.isFinite(period.startMs)) years.add(timelineBucket(period.startMs));
    if (Number.isFinite(value.coverage?.earliestMs)) years.add(timelineBucket(value.coverage.earliestMs));
    years.add(timelineBucket(Date.now()));
    const first = Math.min(...years), last = Math.max(...years), columns = [];
    for (let year = first; year <= last; year++) {
      if (years.has(year)) { columns.push(year); continue; }
      let next = year; while (!years.has(next)) next++;
      if (next - year >= 3) { columns.push(null); year = next - 1; } else columns.push(year);
    }
    return columns;
  }
  function hideTimelineTip() { document.querySelector(".tl-tip")?.remove(); }
  function showTimelineTip(anchor, events) {
    hideTimelineTip();
    const tip = element("div", "popover tl-tip", ""); tip.setAttribute("role", "tooltip");
    for (const event of events) {
      const line = element("div", "tl-tip-line", "");
      line.append(element("span", "tl-tip-when", event.whenText), element("span", "", event.title));
      tip.appendChild(line);
    }
    document.body.appendChild(tip);
    const box = anchor.getBoundingClientRect(), { width, height } = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(4, Math.min(box.left + box.width / 2 - width / 2, innerWidth - width - 4))}px`;
    tip.style.top = `${box.top - height - 6 >= 4 ? box.top - height - 6 : box.bottom + 6}px`;
  }
  function revealTimelineEvent(event) {
    if (state.timelineTrack && state.timelineTrack !== event.track) state.timelineTrack = event.track;
    state.timelineFilter = ""; state.timelineEditing = ""; state.timelineOpen = event.id;
    state.timelineCollapsed.delete(timelineBucket(event.startMs));
    if (!state.timelineEvidence[event.id] && !event.manual) void loadTimelineEvidence(event.id);
    renderTimeline();
    byId("timelineList").querySelector(`[data-event="${CSS.escape(event.id)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  // 星标按月落在时间段格子里；日期只到年的放在格子中间。同月的并成一颗，提示里逐件列出。
  function overviewStars(events) {
    const slots = new Map(), months = grainMonths();
    for (const event of events) {
      const day = new Date(event.startMs), slot = hasMonth(event) ? (day.getFullYear() * 12 + day.getMonth()) % months : (months - 1) / 2;
      slots.set(slot, [...(slots.get(slot) || []), event]);
    }
    return [...slots].map(([slot, items]) => {
      const star = element("button", "tl-overview-star", items.every(event => event.status === "planned") ? "☆" : "★");
      star.type = "button"; star.style.left = `${(slot + .5) / months * 100}%`;
      star.setAttribute("aria-label", items.map(event => event.title).join("、"));
      star.addEventListener("mouseenter", () => showTimelineTip(star, items));
      star.addEventListener("focus", () => showTimelineTip(star, items));
      star.addEventListener("mouseleave", hideTimelineTip);
      star.addEventListener("blur", hideTimelineTip);
      star.addEventListener("click", () => { hideTimelineTip(); revealTimelineEvent(items[0]); });
      return star;
    });
  }
  function renderTimelineOverview() {
    hideTimelineTip();
    const value = state.timeline, host = byId("timelineOverview");
    if (!value || !value.periods.length && !value.events.length) { host.replaceChildren(); host.hidden = true; return; }
    host.hidden = false;
    const counted = value.events.filter(event => Number.isFinite(event.startMs) && event.status !== "cancelled" &&
      (!state.timelineImportantOnly || isImportant(event)));
    const columns = timelineYears(value, counted);
    const cells = new Map();
    for (const event of counted) {
      const key = `${event.track}|${timelineBucket(event.startMs)}`;
      const cell = cells.get(key) || { total: 0, important: [] };
      cell.total++; if (isImportant(event)) cell.important.push(event);
      cells.set(key, cell);
    }
    // 深浅分四档，按全图非空格子的分位切，避免一两个大年份把其他格子都压成最浅。
    const sorted = [...cells.values()].map(cell => cell.total).sort((a, b) => a - b);
    const cut = ratio => sorted[Math.floor((sorted.length - 1) * ratio)] || 0;
    const steps = [cut(.25), cut(.5), cut(.75)];
    const level = total => total <= 0 ? 0 : 1 + steps.filter(step => total > step).length;
    const chatYear = Number.isFinite(value.coverage?.earliestMs) ? timelineBucket(value.coverage.earliestMs) : null;
    const grid = element("div", "tl-grid", ""), width = grainMonths() > 12 ? 52 : grainMonths() < 12 ? 32 : 24;
    grid.style.gridTemplateColumns = `64px ${columns.map(year => year === null ? "16px" : `minmax(${width}px, 1fr)`).join(" ")}`;
    grid.appendChild(element("span", "", ""));
    for (const year of columns) {
      const label = element("span", "tl-year-label" + (year !== null && year === chatYear ? " chat-start" : ""),
        year === null ? "…" : bucketName(year, true));
      if (year !== null && year === chatYear) label.title = `聊天记录从 ${fullDate(value.coverage.earliestMs)} 开始`;
      grid.appendChild(label);
    }
    for (const track of timelineTracks()) {
      const dim = state.timelineTrack && state.timelineTrack !== track.name ? " dim" : "";
      const label = element("button", "tl-track-label" + dim, track.name); label.type = "button";
      label.title = track.description || track.name;
      label.addEventListener("click", () => selectTimelineTrack(track.name));
      grid.appendChild(label);
      for (const year of columns) {
        if (year === null) { grid.appendChild(element("span", "tl-cell gap" + dim, "")); continue; }
        const cell = cells.get(`${track.name}|${year}`);
        const box = element("button", `tl-cell level-${level(cell?.total || 0)}${dim}`, ""); box.type = "button";
        box.style.setProperty("--track", trackColor(track.name));
        box.title = cell ? `${track.name} · ${bucketName(year)}：${cell.total} 件${cell.important.length ? `，重要 ${cell.important.length} 件` : ""}` : `${track.name} · ${bucketName(year)}`;
        box.disabled = !cell;
        box.addEventListener("click", () => selectTimelineTrack(track.name, year));
        const slot = element("div", "tl-cell-slot" + dim, "");
        slot.append(box, ...overviewStars(cell?.important || []));
        grid.appendChild(slot);
      }
    }
    host.replaceChildren(grid);
  }
  // 用户开启的主线（可见且有内容）各占一个页签，“全部”在最前。
  function timelineTracks() {
    const value = state.timeline;
    const used = new Set([...value.periods, ...value.events].map(item => item.track));
    return value.tracks.filter(track => track.visible && used.has(track.name));
  }
  function selectTimelineTrack(name, year = null) {
    state.timelineTrack = name; state.timelineOpen = ""; state.timelineEditing = "";
    if (year !== null) state.timelineCollapsed.delete(year);
    renderTimeline();
    if (year !== null) byId("timelineList").querySelector(`[data-year="${year}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  function renderTimelineTracks() {
    const value = state.timeline, host = byId("timelineTracks");
    if (!value || !value.periods.length && !value.events.length) { host.replaceChildren(); host.hidden = true; return; }
    host.hidden = false;
    const tracks = timelineTracks();
    if (state.timelineTrack && !tracks.some(track => track.name === state.timelineTrack)) state.timelineTrack = "";
    const bar = element("div", "tl-tabs", ""); bar.setAttribute("role", "tablist");
    for (const [name, label] of [["", "全部"], ...tracks.map(track => [track.name, track.name])]) {
      const tab = element("button", "", label); tab.type = "button"; tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(state.timelineTrack === name));
      tab.addEventListener("click", () => selectTimelineTrack(name));
      bar.appendChild(tab);
    }
    const manage = element("button", "plain tl-manage-toggle", state.timelineManage ? "收起" : "管理主线"); manage.type = "button";
    manage.addEventListener("click", () => { state.timelineManage = !state.timelineManage; renderTimeline(); });
    const row = element("div", "tl-tabs-row", "");
    row.append(bar, manage);
    host.replaceChildren(row);
    if (state.timelineManage) host.appendChild(trackManager());
  }
  function trackManager() {
    const box = element("div", "tl-manage", "");
    for (const track of state.timeline.tracks) {
      const row = element("div", "tl-manage-row", "");
      const visible = document.createElement("input");
      visible.type = "checkbox"; visible.checked = track.visible; visible.disabled = state.timelineBusy;
      visible.setAttribute("aria-label", `在总览条显示${track.name}`);
      visible.addEventListener("change", () => { void timelineCall({ target: "track", action: "visible", id: track.name, fields: { visible: visible.checked } }); });
      row.appendChild(visible);
      row.appendChild(element("span", "tl-swatch", "")).style.background = track.color;
      row.appendChild(element("span", "tl-manage-name", track.name));
      if (track.description) row.appendChild(element("span", "help", track.description));
      if (!track.builtin) {
        const remove = element("button", "plain danger", "删除");
        remove.type = "button"; remove.disabled = state.timelineBusy;
        remove.addEventListener("click", () => { void timelineCall({ target: "track", action: "delete", id: track.name }); });
        row.appendChild(remove);
      }
      box.appendChild(row);
    }
    const form = element("div", "tl-manage-row tl-manage-new", "");
    const name = document.createElement("input"); name.maxLength = 8; name.placeholder = "主线名称（最多 8 字）";
    const description = document.createElement("input"); description.maxLength = 60; description.placeholder = "说明：告诉 AI 什么事归到这里（可选）";
    const colors = element("div", "tl-colors", "");
    let color = TL_COLORS[0];
    for (const option of TL_COLORS) {
      const swatch = element("button", "tl-swatch-btn", "");
      swatch.type = "button"; swatch.style.background = option; swatch.setAttribute("aria-label", "颜色");
      swatch.addEventListener("click", () => { color = option; colors.querySelectorAll(".tl-swatch-btn").forEach(node => node.classList.remove("picked")); swatch.classList.add("picked"); });
      colors.appendChild(swatch);
    }
    const save = element("button", "primary", "新建主线");
    save.type = "button"; save.disabled = state.timelineBusy;
    save.addEventListener("click", () => { void timelineCall({ target: "track", action: "add", fields: { title: name.value, description: description.value, color } }); });
    form.append(name, description, colors, save);
    box.appendChild(form);
    return box;
  }
  function renderTimelinePeriodBar() {
    const host = byId("timelinePeriodBar");
    const period = (state.timeline?.periods || []).find(item => item.id === state.timelineFilter);
    if (!period) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    host.replaceChildren();
    host.appendChild(element("span", "tl-period-title", `${period.title} · ${period.whenText}${period.ongoing ? " · 至今" : ""} · ${period.evidenceCount} 依据`));
    const edit = element("button", "plain", "编辑"); edit.type = "button";
    edit.addEventListener("click", () => { state.timelineEditing = state.timelineEditing === period.id ? "" : period.id; renderTimeline(); });
    host.appendChild(edit);
    const close = element("button", "plain", "×"); close.type = "button"; close.setAttribute("aria-label", "取消筛选");
    close.addEventListener("click", () => { state.timelineFilter = ""; state.timelineEditing = ""; renderTimeline(); });
    host.appendChild(close);
    if (state.timelineEditing === period.id) host.appendChild(periodForm(period));
  }
  function periodForm(period = null, onDone = () => {}) {
    const form = element("div", "tl-edit", "");
    const title = document.createElement("input"); title.value = period?.title || ""; title.placeholder = "时期名称"; title.maxLength = 40;
    const track = document.createElement("select");
    for (const name of trackNames()) { const option = element("option", "", name); option.value = name; if (period?.track === name) option.selected = true; track.appendChild(option); }
    const start = document.createElement("input"); start.placeholder = "开始：2022-09"; start.value = whenInput(period?.startMs, period?.precision);
    const end = document.createElement("input"); end.placeholder = "结束：留空表示至今"; end.value = whenInput(period?.endMs, period?.precision);
    const save = element("button", "primary", period ? "保存" : "添加"); save.type = "button";
    save.disabled = state.timelineBusy;
    save.addEventListener("click", () => {
      const fields = { title: title.value.trim(), track: track.value };
      const parsedStart = parseWhenInput(start.value);
      if (!parsedStart) { state.timelineError = `开始${WHEN_HINT}`; renderTimeline(); return; }
      fields.startMs = parsedStart.startMs; fields.precision = parsedStart.precision;
      const parsedEnd = end.value.trim() ? parseWhenInput(end.value) : null;
      if (end.value.trim() && !parsedEnd) { state.timelineError = `结束${WHEN_HINT}`; renderTimeline(); return; }
      fields.endMs = parsedEnd ? parsedEnd.endMs : null;
      fields.ongoing = !end.value.trim();
      void (async () => {
        if (await timelineCall({ target: "period", action: period ? "edit" : "add", id: period?.id, fields })) { state.timelineEditing = ""; onDone(); }
      })();
    });
    const cancel = element("button", "plain", "取消"); cancel.type = "button";
    cancel.addEventListener("click", () => { state.timelineEditing = ""; if (period) state.timelineFilter = state.timelineFilter; renderTimeline(); onDone(); });
    form.append(title, track, start, end, element("div", "tl-form-actions", ""), save, cancel);
    return form;
  }
  function whenInput(ms, precision) {
    if (!Number.isFinite(ms)) return "";
    const day = new Date(ms);
    if (precision === "day") return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    if (["month", "season"].includes(precision)) return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}`;
    return String(day.getFullYear());
  }
  function renderTimelineList() {
    const host = byId("timelineList"), value = state.timeline;
    if (!value) { host.replaceChildren(); return; }
    const children = [];
    if (state.timelineNotice) children.push(element("div", "tl-notice", state.timelineNotice));
    const period = value.periods.find(item => item.id === state.timelineFilter);
    const events = value.events.filter(event => {
      if (state.timelineImportantOnly && !isImportant(event)) return false;
      if (state.timelineTrack && event.track !== state.timelineTrack) return false;
      if (period && Number.isFinite(period.startMs)) {
        const end = Number.isFinite(period.endMs) ? period.endMs : Date.now();
        return Number.isFinite(event.startMs) && event.startMs >= period.startMs && event.startMs <= end;
      }
      return true;
    });
    const dated = events.filter(event => Number.isFinite(event.startMs)).sort((a, b) => b.startMs - a.startMs);
    const unknown = events.filter(event => !Number.isFinite(event.startMs));
    const earliest = Number.isFinite(value.coverage?.earliestMs) ? value.coverage.earliestMs : null;
    const years = [...new Set(dated.map(event => timelineBucket(event.startMs)))].sort((a, b) => b - a);
    for (const year of years) {
      const items = dated.filter(event => timelineBucket(event.startMs) === year);
      const shown = items.filter(event => state.timelineImportantOnly || isImportant(event) || event.status === "planned");
      children.push(yearHeader(year, items, shown));
    }
    if (earliest) {
      const marker = element("div", "tl-list-marker", `聊天记录从 ${fullDate(earliest)} 开始`);
      children.push(marker);
    }
    if (unknown.length) {
      const group = element("section", "tl-year", "");
      const head = element("button", "tl-year-head", ""); head.type = "button";
      head.appendChild(element("span", "", "时间不明"));
      head.appendChild(element("span", "tl-count", `${unknown.length} 件`));
      head.addEventListener("click", () => { state.timelineUnknownOpen = !state.timelineUnknownOpen; renderTimeline(); });
      group.appendChild(head);
      const body = element("div", "tl-fold" + (state.timelineUnknownOpen ? " open" : ""), "");
      const inner = element("div", "tl-fold-inner", "");
      for (const event of unknown) inner.appendChild(eventRow(event));
      body.appendChild(inner);
      group.appendChild(body);
      children.push(group);
    }
    // 重绘会摘掉正在输入的时间框，这次失焦不算用户离开。
    state.timelineRendering = true;
    host.replaceChildren(...children);
    state.timelineRendering = false;
    const when = host.querySelector(".tl-when-input");
    if (when && document.activeElement !== when) { when.focus(); when.select(); }
  }
  function yearHeader(year, items, shown) {
    const group = element("section", "tl-year", "");
    group.dataset.year = String(year);
    const collapsed = state.timelineCollapsed.has(year);
    const head = element("button", "tl-year-head", ""); head.type = "button";
    head.setAttribute("aria-expanded", String(!collapsed));
    head.appendChild(element("span", "", bucketName(year)));
    head.appendChild(element("span", "tl-count", `${items.length} 件`));
    head.addEventListener("click", () => { collapsed ? state.timelineCollapsed.delete(year) : state.timelineCollapsed.add(year); renderTimeline(); });
    group.appendChild(head);
    const body = element("div", "tl-fold" + (collapsed ? "" : " open"), "");
    const inner = element("div", "tl-fold-inner", "");
    // 展开后所有事件按时间排在一起，不再分重要和一般两段。
    const expanded = state.timelineExpanded.has(year);
    for (const event of expanded ? items : shown) inner.appendChild(eventRow(event));
    if (items.length > shown.length) {
      const toggle = element("button", "link tl-folded", expanded ? "收起" : "展开全部事件"); toggle.type = "button";
      toggle.addEventListener("click", () => { expanded ? state.timelineExpanded.delete(year) : state.timelineExpanded.add(year); renderTimeline(); });
      inner.appendChild(toggle);
    }
    body.appendChild(inner);
    group.appendChild(body);
    return group;
  }
  // 时间格是输入框的入口：回车或点到别处保存，Esc 放弃，清空表示时间不明。
  function whenCell(event) {
    if (state.timelineWhen?.id !== event.id) {
      const button = element("button", "tl-month", monthLabel(event)); button.type = "button"; button.title = "修改时间";
      button.addEventListener("click", () => { state.timelineWhen = { id: event.id, text: whenInput(event.startMs, event.precision) }; renderTimeline(); });
      return button;
    }
    const input = document.createElement("input");
    input.className = "tl-when-input"; input.value = state.timelineWhen.text; input.placeholder = "2024.05"; input.setAttribute("aria-label", "时间");
    input.addEventListener("input", () => { state.timelineWhen.text = input.value; });
    input.addEventListener("keydown", key => {
      if (key.isComposing) return;
      if (key.key === "Enter") { key.preventDefault(); saveWhen(event, input.value, false); }
      if (key.key === "Escape") { state.timelineWhen = null; state.timelineError = ""; renderTimeline(); }
    });
    input.addEventListener("blur", () => { if (!state.timelineRendering && state.timelineWhen?.id === event.id) saveWhen(event, input.value, true); });
    return input;
  }
  function saveWhen(event, text, leaving) {
    const value = text.trim(), parsed = value ? parseWhenInput(value) : null;
    if (value && !parsed) {
      if (leaving) state.timelineWhen = null;
      state.timelineError = WHEN_HINT; renderTimeline(); return;
    }
    state.timelineWhen = null;
    if (value === whenInput(event.startMs, event.precision)) { renderTimeline(); return; }
    if (!parsed) { void patchTimelineEvent(event, { startMs: null, endMs: null, precision: "unknown" }); return; }
    // 列表只显示开始时间；原本跨月的事件改开始时，结束时间不动。
    const spanning = Number.isFinite(event.endMs) && whenInput(event.endMs, event.precision) !== whenInput(event.startMs, event.precision);
    void patchTimelineEvent(event, { ...parsed, endMs: spanning && event.endMs >= parsed.startMs ? event.endMs : parsed.endMs });
  }
  // 重要节点用主线色星标，其余用圆点，计划中的空心；点一下切换重要程度。
  function importanceMark(event) {
    const important = isImportant(event), planned = event.status === "planned";
    const mark = element("button", "tl-mark", ""); mark.type = "button";
    mark.title = important ? "改为一般事件" : "标为重要节点";
    mark.setAttribute("aria-pressed", String(important)); mark.setAttribute("aria-label", mark.title);
    const glyph = element("span", important ? "tl-star" : "tl-event-dot" + (planned ? " planned" : ""), important ? (planned ? "☆" : "★") : "");
    glyph.style.color = trackColor(event.track); if (!important) glyph.style.background = trackColor(event.track);
    mark.appendChild(glyph);
    mark.addEventListener("click", () => { void patchTimelineEvent(event, { importance: important ? "general" : "important" }); });
    return mark;
  }
  function eventRow(event) {
    const row = element("div", "tl-event" + (state.timelineOpen === event.id ? " open" : ""), "");
    row.dataset.event = event.id;
    const head = element("div", "tl-event-head" + (state.timelineWhen?.id === event.id ? " editing" : ""), "");
    head.append(whenCell(event), importanceMark(event));
    const open = element("button", "tl-event-open", ""); open.type = "button";
    open.setAttribute("aria-expanded", String(state.timelineOpen === event.id));
    // 状态提示跟在标题后，每行高度不变。
    const important = isImportant(event), planned = event.status === "planned";
    const name = element("span", "tl-name", "");
    name.appendChild(element("span", "tl-title" + (important ? " important" : "") +
      (event.status === "cancelled" ? " cancelled" : "") + (event.status === "uncertain" ? " uncertain" : ""), event.title));
    if (planned) name.appendChild(element("span", "tl-hint", "计划"));
    if (event.status === "uncertain") name.appendChild(element("span", "tl-hint", "待判断"));
    open.append(name, element("span", "tl-track", state.timelineTrack ? "" : event.track));
    head.appendChild(open);
    open.addEventListener("click", () => {
      state.timelineOpen = state.timelineOpen === event.id ? "" : event.id;
      if (state.timelineOpen && !state.timelineEvidence[event.id] && !event.manual) void loadTimelineEvidence(event.id);
      renderTimeline();
    });
    head.addEventListener("contextmenu", menu => {
      menu.preventDefault();
      const rect = head.getBoundingClientRect();
      // 键盘（菜单键、Shift+F10）触发时没有指针坐标，菜单贴在这一行下方。
      openTimelineMenu(event, menu.clientX || rect.left + 48, menu.clientY || rect.bottom);
    });
    row.appendChild(head);
    const detail = element("div", "tl-detail" + (state.timelineOpen === event.id ? " open" : ""), "");
    detail.appendChild(element("div", "tl-detail-inner", ""),);
    detail.firstChild.appendChild(eventDetail(event));
    row.appendChild(detail);
    return row;
  }
  // 事件的确认、编辑、删除放在右键菜单里，详情只用来看依据。
  function closeTimelineMenu() { document.querySelector(".tl-menu")?.remove(); }
  function openTimelineMenu(event, x, y) {
    closeTimelineMenu();
    const menu = element("div", "popover tl-menu", ""); menu.setAttribute("role", "menu");
    const item = (label, action, danger = false) => {
      const button = element("button", "tl-menu-item" + (danger ? " danger" : ""), label);
      button.type = "button"; button.setAttribute("role", "menuitem"); button.disabled = state.timelineBusy;
      button.addEventListener("click", () => { closeTimelineMenu(); action(); });
      menu.appendChild(button);
    };
    const remove = () => {
      void timelineCall({ target: "event", action: "delete", id: event.id }).then(ok => {
        if (ok) { state.timelineNotice = `已删除“${event.title}”，按 Ctrl+Z 撤销`; state.timelineOpen = ""; renderTimeline(); }
      });
    };
    if (!event.manual) {
      const confirmed = event.level === "confirmed";
      item(confirmed ? "取消确认" : "确认", () => { void timelineCall({ target: "event", action: confirmed ? "unconfirm" : "confirm", id: event.id }); });
      item("编辑", () => {
        state.timelineOpen = event.id; state.timelineEditing = event.id;
        if (!state.timelineEvidence[event.id]) void loadTimelineEvidence(event.id);
        renderTimeline();
      });
    }
    item("删除", remove, true);
    document.body.appendChild(menu);
    const { width, height } = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(x, innerWidth - width - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, innerHeight - height - 4))}px`;
    menu.querySelector("button:not(:disabled)")?.focus();
  }
  async function loadTimelineEvidence(id) {
    const epoch = state.epoch, original = account();
    try {
      const result = await window.kyfHost.rpc("self.timelineEvidence", { id }, scope());
      if (!current(epoch, original) || !object(result)) return;
      state.timelineEvidence[id] = result;
    } catch { state.timelineEvidence[id] = { total: 0, items: [] }; }
    if (state.timelineOpen === id) renderTimeline();
  }
  function eventDetail(event) {
    const box = element("div", "tl-detail-box", "");
    const parts = [`${event.whenText}${event.status === "planned" ? "（计划）" : ""}${event.inferred ? "（推断）" : ""}`];
    if (!event.manual) parts.push(TL_LEVELS[event.level] || TL_LEVELS.possible);
    if (event.manual) parts.push("手动添加");
    box.appendChild(element("p", "tl-line", parts.filter(Boolean).join(" · ")));
    const evidence = state.timelineEvidence[event.id];
    const phrase = event.phrase || evidence?.items?.[0]?.phrase;
    if (phrase) {
      const said = evidence?.items?.[0]?.time;
      box.appendChild(element("p", "tl-line", `“${phrase}”${Number.isFinite(said) ? "，" + fullDate(said) + " 说" : ""}`));
    }
    for (const conflict of event.conflicts || []) {
      box.appendChild(element("p", "tl-line", `另一说法：${conflict.when}${conflict.chat ? `（与 ${conflict.chat}）` : ""}`));
    }
    if (Array.isArray(evidence?.items)) {
      for (const item of evidence.items.slice(0, 2)) box.appendChild(evidenceChat(item));
      if (evidence.total > 2) {
        const more = element("button", "link", `查看全部 ${evidence.total} 条依据`); more.type = "button";
        more.addEventListener("click", () => {
          more.replaceWith(...evidence.items.slice(2).map(evidenceChat),
            element("p", "help", `共 ${evidence.total} 条，显示最近 ${Math.min(50, evidence.total)} 条`));
        });
        box.appendChild(more);
      }
    } else if (!event.manual && event.evidenceCount) {
      box.appendChild(element("p", "help", "正在读取依据…"));
    }
    if (event.reason) box.appendChild(element("p", "tl-line", event.reason));
    if (state.timelineEditing === event.id) box.appendChild(eventForm(event));
    return box;
  }
  function evidenceChat(item) {
    const chat = element("div", "tl-chat", "");
    chat.appendChild(element("p", "tl-chat-head",
      `${string(item.chat, 120) || "聊天"}${item.isGroup ? "（群）" : ""}${Number.isFinite(item.time) ? " · " + fullDate(item.time) : ""}`));
    const lines = [...(Array.isArray(item.before) ? item.before : []), { side: "self", text: item.text, own: true },
      ...(Array.isArray(item.after) ? item.after : [])];
    let speaker = "";
    for (const line of lines) {
      const mine = line.side === "self", row = element("div", "tl-msg" + (mine ? " self" : ""), "");
      // 群聊里换人说话时标出名字，私聊不需要。
      if (item.isGroup && !mine && line.speaker && line.speaker !== speaker) row.appendChild(element("span", "tl-msg-name", string(line.speaker, 40)));
      speaker = mine ? "" : line.speaker;
      row.appendChild(element("p", "tl-bubble" + (line.own ? " own" : ""), string(line.text, 600)));
      chat.appendChild(row);
    }
    return chat;
  }
  function eventForm(event) {    const form = element("div", "tl-edit", "");
    const title = document.createElement("input"); title.value = event.title; title.maxLength = 40; title.setAttribute("aria-label", "标题");
    const track = document.createElement("select");
    for (const name of trackNames()) { const option = element("option", "", name); option.value = name; if (event.track === name) option.selected = true; track.appendChild(option); }
    const status = document.createElement("select");
    for (const [key, label] of Object.entries(TL_STATUS)) { const option = element("option", "", label); option.value = key; if (event.status === key) option.selected = true; status.appendChild(option); }
    const importance = document.createElement("select");
    importance.append(...["important", "general"].map((key, index) => {
      const option = element("option", "", key === "important" ? "重要节点" : "一般事件"); option.value = key;
      if ((event.importance || "general") === key) option.selected = true; return option;
    }));
    const start = document.createElement("input"); start.placeholder = "时间：2024、2024.05 或 2024.05.01"; start.value = whenInput(event.startMs, event.precision);
    const end = document.createElement("input"); end.placeholder = "结束（可选）"; end.value = whenInput(event.endMs, event.precision);
    const save = element("button", "primary", "保存"); save.type = "button"; save.disabled = state.timelineBusy;
    save.addEventListener("click", () => {
      const fields = { title: title.value.trim(), track: track.value, status: status.value, importance: importance.value };
      const parsedStart = parseWhenInput(start.value);
      if (start.value.trim() && !parsedStart) { state.timelineError = WHEN_HINT; renderTimeline(); return; }
      fields.precision = parsedStart ? parsedStart.precision : "unknown";
      fields.startMs = parsedStart ? parsedStart.startMs : null;
      const parsedEnd = end.value.trim() ? parseWhenInput(end.value) : null;
      if (end.value.trim() && !parsedEnd) { state.timelineError = `结束${WHEN_HINT}`; renderTimeline(); return; }
      fields.endMs = parsedEnd ? parsedEnd.endMs : null;
      void timelineCall({ target: "event", action: "edit", id: event.id, fields }).then(ok => { if (ok) state.timelineEditing = ""; });
    });
    const cancel = element("button", "plain", "取消"); cancel.type = "button";
    cancel.addEventListener("click", () => { state.timelineEditing = ""; renderTimeline(); });
    form.append(title, track, status, importance, start, end, save, cancel);
    return form;
  }
  function renderTimelineAddForm() {
    const host = byId("timelineAddForm");
    if (state.timelineAddType === null) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    const type = element("select", "", "");
    for (const [key, label] of [["event", "手动添加事件"], ["period", "手动添加时期"]]) {
      const option = element("option", "", label); option.value = key;
      if (state.timelineAddType === key) option.selected = true;
      type.appendChild(option);
    }
    type.addEventListener("change", () => { state.timelineAddType = type.value; renderTimelineAddForm(); });
    const close = element("button", "plain", "取消"); close.type = "button";
    close.addEventListener("click", () => { state.timelineAddType = null; renderTimeline(); });
    if (state.timelineAddType === "period") host.replaceChildren(type, periodForm(null, () => { state.timelineAddType = null; }), close);
    else {
      const blank = { title: "", track: trackNames()[0] || "其他", status: "occurred", importance: "general",
        startMs: null, endMs: null, precision: "day" };
      host.replaceChildren(type, eventForm(blank), close);
      // A blank form saves through the add action instead of editing an event.
      const save = host.querySelector(".tl-edit .primary");
      if (save) {
        const clone = save.cloneNode(true);
        save.replaceWith(clone);
        clone.textContent = "添加";
        clone.addEventListener("click", () => {
          const form = host.querySelector(".tl-edit");
          const inputs = form.querySelectorAll("input, select");
          const fields = { title: inputs[0].value.trim(), track: inputs[1].value, status: inputs[2].value,
            importance: inputs[3].value };
          const parsedStart = parseWhenInput(inputs[4].value);
          if (!parsedStart) { state.timelineError = WHEN_HINT; renderTimeline(); return; }
          fields.precision = parsedStart.precision;
          fields.startMs = parsedStart.startMs;
          const parsedEnd = inputs[5].value.trim() ? parseWhenInput(inputs[5].value) : null;
          if (inputs[5].value.trim() && !parsedEnd) { state.timelineError = `结束${WHEN_HINT}`; renderTimeline(); return; }
          fields.endMs = parsedEnd ? parsedEnd.endMs : null;
          void timelineCall({ target: "event", action: "add", fields }).then(ok => { if (ok) state.timelineAddType = null; });
        });
      }
    }
  }
  async function saveTag() {
    const name = byId("tagName").value.trim();
    if (!name) { byId("tagName").focus(); return; }
    if (!await tagCall("self.tagEdit", { action: "add", name, category: byId("tagCategory").value })) return;
    byId("tagName").value = ""; byId("tagForm").hidden = true;
    selectTagNamed(name.replace(/^[#＃]+/, "").trim());
  }
  // 性格分析 shows the 性格 charts; 三观分析 shows the 三观 charts above the five 三观 text items. Both chart sets
  // come from self.persona; choosing a chart opens its dimensions below it (2026-10-25).
  const CHART_TABS = { persona: ["personality", "personaCharts", "personaList"], values: ["values", "valuesCharts", "valuesChartList"] };
  const REPORT_TITLES = { mbti: "MBTI", attachment: "依恋类型", politics: "政治自我定位" };
  const STALE = "聊天记录已变化，AI 结论待更新；你的修正已保留。";
  function evidencePage(panel, id, initial, total, nextOffset, version) {
    const rows = Array.isArray(initial) ? initial.filter(object) : [];
    if (panel.evidencePages[id]?.version !== version) delete panel.evidencePages[id];
    return panel.evidencePages[id] ||= { items: rows, total: count(total) ?? rows.length, version,
      nextOffset: count(nextOffset) ?? (count(total) > rows.length ? rows.length : null), pending: false, error: "" };
  }
  function evidenceMore(details, panel, id, page, method, params, render, version, reload) {
    if (page.error) details.appendChild(element("p", "help", page.error));
    if (page.nextOffset === null || !method) return;
    const more = element("button", "link evidence-more", page.pending ? "正在读取…" : page.stale ? "读取最新依据" : page.error ? "重试读取依据" : `继续读取依据（已显示 ${page.items.length} / ${page.total}）`);
    more.type = "button"; more.disabled = page.pending;
    more.addEventListener("click", async () => {
      if (page.pending || !account() || state.disabled) return;
      if (page.stale) { if (reload) void reload(); return; }
      const epoch = state.epoch, original = account(), offset = page.nextOffset;
      page.pending = true; page.error = ""; panel.expanded.add(id); render();
      try {
        const value = await window.kyfHost.rpc(method, { ...params, offset, limit: 50, ...(version === undefined ? {} : { expectedVersion: version }) }, scope());
        if (!current(epoch, original) || panel.evidencePages[id] !== page) return;
        if (version !== undefined && value?.version !== version) throw Object.assign(new Error("依据已更新，请读取最新依据后继续。"), { code: "stale-evidence" });
        if (!object(value) || !Array.isArray(value.items) || count(value.total) === null ||
          (value.nextOffset !== null && (count(value.nextOffset) === null || value.nextOffset <= offset))) throw new Error("依据读取结果不完整，请重试。");
        page.items.push(...value.items.filter(object)); page.total = value.total; page.nextOffset = value.nextOffset;
      } catch (error) { if (current(epoch, original) && panel.evidencePages[id] === page) { page.error = friendly(error); page.stale = error?.code === "stale-evidence"; } }
      finally { if (current(epoch, original) && panel.evidencePages[id] === page) { page.pending = false; render(); } }
    });
    details.appendChild(more);
  }
  function opinionEvidence(kind, value, side, title, id) {
    const panel = state.opinions[kind], page = evidencePage(panel, id, value[side], value[side + "Total"], value[side + "NextOffset"], panel.value?.version);
    const rows = page.items, details = element("details", "persona-evidence", "");
    const epoch = state.epoch, original = account();
    details.id = id; details.open = panel.expanded.has(id);
    details.addEventListener("toggle", () => {
      if (!current(epoch, original)) return;
      if (details.open) state.opinions[kind].expanded.add(id); else state.opinions[kind].expanded.delete(id);
    });
    details.appendChild(element("summary", "", `${title} ${page.total}`));
    for (const evidence of rows) {
      const card = element("article", "sample", ""), date = Number.isFinite(evidence.time) ? new Date(evidence.time).toLocaleDateString("zh-CN") : "时间未知";
      card.appendChild(element("p", "sample-meta", `${string(evidence.chat, 120) || string(evidence.user, 120) || "聊天"} · ${date}${evidence.truncated === true ? " · 仅部分文字" : ""}`));
      const context = lines => (Array.isArray(lines) ? lines.filter(object) : []).map(line => `${string(line.speaker, 120) || (line.side === "self" ? "本人" : "对方")}：${string(line.text, 1200)}${line.truncated === true ? "（仅部分文字）" : ""}`).join("\n");
      const before = context(evidence.contextBefore), after = context(evidence.contextAfter);
      if (before) card.appendChild(element("p", "sample-text", "前文：\n" + before));
      card.appendChild(element("p", "sample-reply", "本人原话：" + string(evidence.quote, 8000)));
      if (after) card.appendChild(element("p", "sample-text", "后文：\n" + after));
      if (string(evidence.reason)) card.appendChild(element("p", "tag-interpretation", "判断理由：" + string(evidence.reason, 1200)));
      if (evidence.cited === false) card.appendChild(element("p", "help", "同维度相关记录，未被本段结论直接引用"));
      details.appendChild(card);
    }
    const source = id.match(/^(?:persona|values)-evidence-(.+)-(inference|selfReport)-(?:support|counter)$/);
    if (source) evidenceMore(details, panel, id, page, `self.${kind === "persona" ? "persona" : "values"}Evidence`,
      { key: source[1], kind: source[2], side }, () => renderOpinion(kind), panel.value?.version, () => { panel.evidencePages = {}; return loadOpinion(kind); });
    return details;
  }
  // Minimal cards (2026-10-28): the conclusion, one line of what it rests on, the quotes on demand;
  // the correction editor opens only when asked. Empty dimensions collapse to one line.
  // Boilerplate the backend attaches to every conclusion carries no information here.
  const DEFAULT_SCOPE = /^(仅限(所见|这些)?聊天|本人明确自述)/;
  function opinionMeta(value) {
    const parts = [string(value.confidence, 80) === "本人自述" ? "" : string(value.confidence, 80)];
    const support = Number.isInteger(value.evidenceTotal) ? value.evidenceTotal : Array.isArray(value.evidence) ? value.evidence.length : 0;
    if (support) parts.push(`${support} 条依据`);
    if (Number.isFinite(value.fromMs) && Number.isFinite(value.toMs)) {
      const from = new Date(value.fromMs).getFullYear(), to = new Date(value.toMs).getFullYear();
      parts.push(from === to ? String(from) : `${from}–${to}`);
    }
    const scope = string(value.scope, 200);
    if (scope && !DEFAULT_SCOPE.test(scope)) parts.push(scope);
    return parts.filter(Boolean).join(" · ");
  }
  function opinionColumn(kind, title, value, id) {
    const section = element("section", "persona-column", "");
    if (title) section.appendChild(element("h3", "", title));
    section.appendChild(element("p", "persona-text", string(value.text, 4000)));
    const meta = opinionMeta(value);
    if (meta) section.appendChild(element("p", "help", meta));
    if (value.conflicting === true) section.appendChild(element("p", "help", "自述有不同说法"));
    const support = Array.isArray(value.evidence) ? value.evidence.filter(object) : [];
    const counter = Array.isArray(value.counter) ? value.counter.filter(object) : [];
    if (support.length || value.evidenceTotal > 0) section.appendChild(opinionEvidence(kind, value, "evidence", "原话", id + "-support"));
    if (counter.length || value.counterTotal > 0) section.appendChild(opinionEvidence(kind, value, "counter", "反例", id + "-counter"));
    return section;
  }
  const shownColumns = columns => columns.filter(column => object(column.value) && string(column.value.text, 4000));
  function opinionColumns(kind, columns) {
    const shown = shownColumns(columns), row = element("div", shown.length > 1 ? "persona-columns" : "persona-columns single", "");
    for (const column of shown) row.appendChild(opinionColumn(kind, shown.length > 1 ? column.title : "", column.value, column.id));
    return row;
  }
  // columns: [{ title, value, id }]; only those with a conclusion are drawn.
  function opinionCard(kind, dimension, title, columns) {
    const panel = state.opinions[kind], key = dimension.key, card = element("article", "persona-card", ""); card.id = kind + "-" + key;
    const shown = shownColumns(columns), saved = string(dimension.manual?.text, 2000);
    const editing = panel.editing.has(key) || panel.dirty.has(key), locked = panel.busy || !account() || state.disabled;
    const head = element("div", "persona-head", "");
    head.appendChild(element("h2", "", title));
    if (!shown.length) head.appendChild(element("span", "help", "暂无依据"));
    if (!editing) {
      const edit = element("button", "plain persona-edit", saved ? "编辑修正" : "修正"); edit.type = "button"; edit.id = kind + "-edit-" + key;
      edit.disabled = locked;
      edit.addEventListener("click", () => { panel.editing.add(key); renderOpinion(kind); byId(kind + "-manual-" + key)?.focus(); });
      head.appendChild(edit);
    }
    card.appendChild(head);
    if (shown.length) card.appendChild(opinionColumns(kind, columns));
    if (saved && !editing) card.appendChild(element("p", "persona-mine", "我的理解：" + saved));
    if (!editing) return card;
    const manual = element("section", "persona-manual", ""), input = element("textarea", "", "");
    input.id = kind + "-manual-" + key; input.maxLength = 2000; input.rows = 3; input.setAttribute("aria-label", title + "的修正");
    input.value = panel.dirty.has(key) ? panel.drafts[key] : saved;
    input.placeholder = "你对自己的理解；清空后保存即移除";
    input.disabled = locked;
    const actions = element("div", "actions", ""), save = element("button", "", "保存"), cancel = element("button", "plain", "取消");
    save.type = "button"; cancel.type = "button";
    save.disabled = !panel.dirty.has(key) || input.disabled; cancel.disabled = panel.busy;
    input.addEventListener("input", () => { panel.drafts[key] = input.value; panel.dirty.add(key); save.disabled = panel.busy || !account() || state.disabled; });
    save.addEventListener("click", () => { void saveOpinion(kind, key); });
    cancel.addEventListener("click", () => { panel.dirty.delete(key); delete panel.drafts[key]; panel.editing.delete(key); renderOpinion(kind); });
    manual.appendChild(input); actions.appendChild(save); actions.appendChild(cancel);
    manual.appendChild(actions); card.appendChild(manual); return card;
  }
  function reportCard(key, report, derived) {
    const card = element("article", "persona-card", ""); card.id = "persona-" + key;
    const columns = [{ title: "自述", value: object(report) ? report : null, id: "persona-evidence-" + key + "-selfReport" },
      { title: "推算", value: derived ? { text: derived } : null, id: "persona-evidence-" + key + "-derived" }];
    const head = element("div", "persona-head", ""); head.appendChild(element("h2", "", REPORT_TITLES[key]));
    if (!shownColumns(columns).length) head.appendChild(element("span", "help", "暂无依据"));
    card.appendChild(head);
    if (shownColumns(columns).length) card.appendChild(opinionColumns("persona", columns));
    return card;
  }
  function personaDims() {
    const dims = {};
    for (const item of Array.isArray(state.opinions.persona.value?.dimensions) ? state.opinions.persona.value.dimensions : [])
      if (object(item) && Object.hasOwn(Charts.DIMENSIONS, item.key)) dims[item.key] = item;
    return dims;
  }
  function historyChartCards(chart) {
    const dims = personaDims(), reports = object(state.opinions.persona.value?.selfReports) ? state.opinions.persona.value.selfReports : {};
    const cards = chart.dims.filter(key => dims[key]).map(key => opinionCard("persona", dims[key], Charts.label(key),
      [{ title: "推测", value: dims[key].inference, id: "persona-evidence-" + key + "-inference" }]));
    if (chart.id === "big5") {
      const derived = Charts.mbti(Object.fromEntries(Object.entries(dims).map(([key, item]) => [key, item.inference])), null);
      cards.push(reportCard("mbti", reports.mbti, derived ? derived.text : null));
    }
    if (chart.id === "attachment" && object(reports.attachment)) cards.push(reportCard("attachment", reports.attachment));
    if (chart.id === "politics" && object(reports.politics)) cards.push(reportCard("politics", reports.politics));
    return cards;
  }
  function chartCards(chart) {
    if (chart.id === "attachment") return historyChartCards(chart);
    const timeline = chart.id === "big5" ? "period" : "worldview";
    const period = timeline === "period" ? chosenPeriod() : chosenWorldview();
    if (!period) return historyChartCards(chart);
    const open = timeline === "period" ? openPeriodDetail : openWorldviewDetail;
    const cards = chart.dims.filter(key => Object.hasOwn(period.factors || {}, key)).map(key => {
      const card = element("article", "persona-card", ""), factor = period.factors?.[key] || {};
      const score = Number.isFinite(factor.score) ? `${factor.score.toFixed(1)} / 5` : period.status === "pending" ? "本期尚未分析，暂无分数" : period.status === "partial" ? "本期调研未完成，暂无分数" : "本期依据不足，暂无分数";
      card.append(element("h2", "", Charts.label(key)), element("p", "persona-text", `${period.label} · ${score}`));
      if (Number.isInteger(factor.items) && Number.isInteger(factor.total)) card.appendChild(element("p", "help", `可计分题目 ${factor.items} / ${factor.total}`));
      const button = element("button", "link", "查看本期逐题依据"); button.type = "button";
      button.addEventListener("click", () => { void open(key); }); card.appendChild(button);
      return card;
    });
    const history = element("details", "opinion-history", "");
    history.id = `persona-history-${chart.id}`; history.open = state.opinions.persona.expanded.has(history.id);
    const epoch = state.epoch, original = account();
    history.addEventListener("toggle", () => {
      if (!current(epoch, original)) return;
      if (history.open) state.opinions.persona.expanded.add(history.id); else state.opinions.persona.expanded.delete(history.id);
    });
    history.appendChild(element("summary", "", "全历史综合分析与依据"));
    const list = element("div", "persona-list", ""); list.append(...historyChartCards(chart)); history.appendChild(list);
    return [...cards, history];
  }
  function renderCharts() {
    const value = state.opinions.persona.value, dims = personaDims();
    const reports = object(value?.selfReports) ? value.selfReports : {};
    const chartDims = Object.fromEntries(Object.entries(dims).map(([key, item]) => [key, object(item.inference) ? item.inference : null]));
    const period = chosenPeriod(); if (period) Object.assign(chartDims, Object.fromEntries(Charts.MBTI.map(scale => [scale.key, null])), periodDims(period));
    const view = chosenWorldview(); if (view) Object.assign(chartDims, Object.fromEntries(WORLDVIEW_SCALES.map(scale => [scale.key, null])), periodDims(view));
    const chartReports = Object.fromEntries(Object.keys(REPORT_TITLES).map(key => [key, object(reports[key]) ? string(reports[key].text, 40) || null : null]));
    if (period) chartReports.mbti = string(period.report?.value, 40) || null;
    if (view) chartReports.politics = null;
    message("personaChartContext", period ? `性格时期：${period.label} · 依恋倾向使用全历史资料` : value ? "全历史综合分析" : "");
    message("valuesChartContext", view ? `当前时期：${view.label} · 点图表查看本期依据` : value ? "全历史综合分析" : "");
    for (const [tab, [chartTab, chartsId, listId]] of Object.entries(CHART_TABS)) {
      if (!value && !(tab === "persona" ? period : view)) { byId(chartsId).replaceChildren(); byId(listId).replaceChildren(); continue; }
      Charts.render(byId(chartsId), { tab: chartTab, dims: chartDims, selfReports: chartReports, selected: state.chartSelected[tab],
        type: period ? { text: (period.letters || []).join("") || "尚无类型", title: period.label + ((period.edited || []).includes("type") ? "，你改过" : "，由性格时间线推算") } : null }, id => {
        state.chartSelected[tab] = state.chartSelected[tab] === id ? null : id; renderOpinion("persona");
      });
      const chart = Charts.CARDS.find(item => item.id === state.chartSelected[tab]);
      byId(listId).replaceChildren(...(chart ? chartCards(chart) : []));
    }
  }
  // 性格时间线 (2026-11-01): the selected period also drives the MBTI card, so the page shows one reading at a time.
  const periodList = () => Array.isArray(state.periods.value?.periods) ? state.periods.value.periods.filter(object) : [];
  function chosenPeriod() {
    const list = periodList();
    const selected = list.find(period => period.id === state.periods.selected);
    if (selected) return selected;
    return list[list.length - 1] || null;
  }
  function periodDims(period) {
    const dims = {};
    for (const [key, item] of Object.entries(period.factors || {})) {
      if (!Number.isFinite(item?.score)) { dims[key] = null; continue; }
      const width = Number.isFinite(item.low) ? item.high - item.low : Infinity;
      dims[key] = { position: (item.score - 3) / 2, confidence: width <= .3 ? "较有把握" : width <= .6 ? "有一定依据" : "把握较低" };
    }
    return dims;
  }
  const PERIOD_HELP = ["点一列看那一期，点左边的名字看每道题。下面色条里颜色相同，就是结果相同。点右边的分数、下面的色条或题目的“修正”可以改，AI 的判断会留着，带小圆点的是你改过的。",
    "一期多长由插件设置的“分析时间颗粒度”决定。模型按问卷的每道题搜索那一期的聊天，判断你会怎么答；它看不到日期和你的答案。",
    "线是分数，1 到 5，虚线是中线 3。阴影是 3 次调研的最低到最高，越宽越不确定；只有深度分析才有阴影。",
    "字母只看分数在中线哪一侧，离中线再近也定下来。阴影跨过中线说明这一项不稳，换一批聊天可能翻到另一边。",
    "被事情推着做的行为不算性格，比如为了找对象多社交。发起次数、活跃度不参与计算。",
    "聊天看得到外在表现，看不到内心，也更容易留下热情和抱怨，所以绝对分可能偏外向、偏宜人、偏不尽责。看时期之间的变化比看绝对分可靠。"];
  const WORLDVIEW_HELP = ["点一列看那一期，下面的两张图和五项文字会跟着切换。点左边的名字看每道题。点右边的分数、下面的色条或题目的“修正”可以改，AI 的判断会留着，带小圆点的是你改过的。",
    "政治题目来自 8values 的经济与公民自由两轴，保留原题权重；价值观仍是 12 道自拟题。8values 是开源政治问卷，不是经过验证的心理量表。这里是依据聊天推测的答案，不是你实际填写的测试结果。",
    "政治题按 1 强烈反对、2 反对、3 中立、4 同意、5 强烈同意回答，再按权重汇总为 1 到 5 的坐标。缺少依据的题留空，不算中立；已答题数和权重都至少覆盖该轴一半才显示分数，缺题时只对已答权重归一化。",
    "聊天里很少谈政治。一期里搜不到足够的相关聊天时，这道题不答。把时间颗粒度调长，能答的题会变多。",
    "政治和价值观只看分数在中线 3 的哪一侧，离中线再近也定下来。阴影跨过中线说明这一项不稳。",
    "五项文字是模型读这一期的相关聊天写的一句话，点“原话”能看到依据。"];
  function periodEvidence(item, id, panel = state.periods, timeline = null, target = null) {
    const page = evidencePage(panel, id, item.evidence, item.evidenceTotal, item.evidenceNextOffset, item.evidenceVersion), rows = page.items;
    const period = target?.period ?? panel.detail?.period, detailKey = panel.detailKey;
    const pageable = timeline && Number.isInteger(item.item) && period && (!target || typeof item.evidenceVersion === "string");
    const details = element("details", "persona-evidence", ""), epoch = state.epoch, original = account();
    details.id = id; details.open = panel.expanded.has(id);
    details.addEventListener("toggle", () => { if (current(epoch, original)) { if (details.open) panel.expanded.add(id); else panel.expanded.delete(id); } });
    details.appendChild(element("summary", "", `原话 ${page.total}${!pageable && rows.length < page.total ? `（已显示 ${rows.length} 条）` : ""}`));
    for (const row of rows) {
      const card = element("article", "sample", ""), date = Number.isFinite(row.time) ? new Date(row.time).toLocaleDateString("zh-CN") : "时间未知";
      const support = row.counted === false || row.k === "处境" ? "未计入" : row.s > 0 ? "支持" : row.s < 0 ? "反对" : "背景";
      card.appendChild(element("p", "sample-meta", `${string(row.name, 120) || "聊天"} · ${date} · ${string(row.k, 8)} · ${support}${row.truncated === true ? " · 仅部分文字" : ""}`));
      card.appendChild(element("p", "sample-reply", string(row.text, 8000)));
      details.appendChild(card);
    }
    if (pageable) evidenceMore(details, panel, id, page, `self.${timeline === "period" ? "period" : timeline}Evidence`,
      { period, item: item.item }, renderTimelines, item.evidenceVersion,
      target?.reload || (() => (timeline === "period" ? openPeriodDetail : openWorldviewDetail)(detailKey, true)));
    return details;
  }
  // Every value the model produced can be changed (2026-11-01): the user's shows, the model's stays on record.
  const MBTI_TYPES = ["INTJ", "INTP", "ENTJ", "ENTP", "INFJ", "INFP", "ENFJ", "ENFP", "ISTJ", "ISFJ", "ESTJ", "ESFJ", "ISTP", "ISFP", "ESTP", "ESFP"];
  const ITEM_SCALE = [[1, "很不符合"], [2, "较不符合"], [3, "说不准"], [4, "较符合"], [5, "很符合"]];
  const POLITICAL_SCALE = [[1, "强烈反对"], [2, "反对"], [3, "中立"], [4, "同意"], [5, "强烈同意"]];
  const TIMELINES = {
    period: { panel: () => state.periods, rpc: "self.periodEdit", detail: "self.periodDetail", names: { type: "读聊天", report: "你说过" }, types: { type: MBTI_TYPES, report: MBTI_TYPES } },
    worldview: { panel: () => state.worldview, rpc: "self.worldviewEdit", detail: "self.worldviewDetail", names: { type: "政治", style: "价值观" },
      types: { type: ["社会主义", "保守主义", "社会民主主义", "保守自由主义"], style: ["理想派", "守护派", "进取派", "务实派"] } },
  };
  const renderTimelines = () => {
    const active = document.activeElement;
    const focus = active?.id?.startsWith("period-draft-") ? { id: active.id, start: active.selectionStart, end: active.selectionEnd, scroll: active.scrollTop } : null;
    renderPeriods(); renderWorldview();
    const control = focus && byId(focus.id);
    if (control && !control.disabled) {
      control.focus({ preventScroll: true });
      if (typeof focus.start === "number") control.setSelectionRange?.(focus.start, focus.end);
      control.scrollTop = focus.scroll;
    }
  };
  function editControl(timeline, field, shown) {
    if (field.startsWith("score:")) {
      const input = element("input", "period-edit-control", ""); input.type = "number"; input.min = "1"; input.max = "5"; input.step = "0.1"; input.value = shown === "" ? "" : String(shown);
      return input;
    }
    if (field.startsWith("view:")) {
      const input = element("textarea", "period-edit-control", ""); input.rows = 2; input.maxLength = 120; input.value = String(shown);
      return input;
    }
    const select = element("select", "period-edit-control", "");
    const choices = field.startsWith("item:") ? (timeline === "worldview" && Number(field.slice(5)) >= 1000 ? POLITICAL_SCALE : ITEM_SCALE) : [["", "无"], ...TIMELINES[timeline].types[field].map(value => [value, value])];
    for (const [value, text] of choices) { const option = element("option", "", `${text}`); option.value = String(value); select.appendChild(option); }
    select.value = String(shown);
    return select;
  }
  function editValue(field, control) {
    if (field.startsWith("score:")) { const value = Number(control.value); return control.value !== "" && value >= 1 && value <= 5 ? value : undefined; }
    return field.startsWith("item:") ? Number(control.value) : control.value;
  }
  // name: what is being changed; ai: the model's value as text; shown: the value the control starts on.
  function periodEditor(timeline, id, label, field, name, ai, shown, edited) {
    const panel = TIMELINES[timeline].panel(), box = element("div", "period-picker", ""), draftKey = `${id}:${field}`;
    const control = editControl(timeline, field, Object.hasOwn(panel.drafts, draftKey) ? panel.drafts[draftKey] : shown);
    control.id = `period-draft-${timeline}-${id}-${field}`;
    control.disabled = panel.saving;
    const remember = () => { panel.drafts[draftKey] = control.value; };
    control.addEventListener("input", remember); control.addEventListener("change", remember);
    box.appendChild(element("p", "help", `${label} · ${name}。AI 判断：${ai || "无"}`));
    control.setAttribute("aria-label", name); box.appendChild(control);
    const actions = element("div", "actions", ""), save = element("button", "primary", "保存"), back = element("button", "", "恢复 AI 判断"), cancel = element("button", "", "取消");
    save.type = "button"; back.type = "button"; cancel.type = "button"; save.disabled = panel.saving; back.disabled = panel.saving;
    save.addEventListener("click", () => {
      const value = editValue(field, control);
      if (value === undefined) { panel.error = "分数要在 1 到 5 之间"; renderTimelines(); return; }
      void saveEdit(timeline, id, field, value);
    });
    back.addEventListener("click", () => { void saveEdit(timeline, id, field, null); });
    cancel.addEventListener("click", () => { delete panel.drafts[draftKey]; panel.edit = null; renderTimelines(); });
    actions.appendChild(save); if (edited) actions.appendChild(back); actions.appendChild(cancel); box.appendChild(actions);
    return box;
  }
  function stripEditor(timeline, periods) {
    const panel = TIMELINES[timeline].panel(), field = panel.edit?.field, period = periods.find(item => item.id === panel.edit?.period);
    if (!period || field.startsWith("item:") || field.startsWith("view:")) return [];
    const edited = (period.edited || []).includes(field), scale = field.startsWith("score:") ? field.slice(6) : "";
    if (scale) {
      const value = period.factors?.[scale]?.score, ai = period.ai?.[field];
      return [periodEditor(timeline, period.id, period.label, field, Charts.label(scale), Number.isFinite(ai) ? ai.toFixed(1) : "", Number.isFinite(value) ? value : "", edited)];
    }
    const shown = field === "type" ? (period.letters || []).join("") : field === "style" ? string(period.style, 40) : string(period.report?.value, 40);
    return [periodEditor(timeline, period.id, period.label, field, TIMELINES[timeline].names[field], period.ai ? string(period.ai[field], 40) : shown, shown, edited)];
  }
  function openEdit(timeline, field, id) {
    if (timeline === "worldview") chooseWorldview(id); else choosePeriod(id);
    TIMELINES[timeline].panel().edit = { field, period: id };
    renderTimelines();
  }
  async function saveEdit(timeline, id, field, value) {
    const { panel: find, rpc, detail } = TIMELINES[timeline], panel = find();
    if (!account() || panel.saving) return;
    const epoch = state.epoch, original = account(), serial = ++panel.serial;
    panel.saving = true; renderTimelines();
    try {
      const result = await window.kyfHost.rpc(rpc, { period: id, field, value }, scope());
      if (!current(epoch, original) || serial !== panel.serial) return;
      if (object(result) && Array.isArray(result.periods)) { panel.value = result; delete panel.drafts[`${id}:${field}`]; panel.edit = null; panel.error = ""; }
      if (panel.detailKey) {
        const list = await window.kyfHost.rpc(detail, { period: id, key: panel.detailKey }, scope());
        if (current(epoch, original) && serial === panel.serial && object(list)) panel.detail = list;
      }
    } catch (error) { if (current(epoch, original) && serial === panel.serial) panel.error = friendly(error); }
    finally { if (current(epoch, original) && serial === panel.serial) { panel.saving = false; renderTimelines(); renderOpinion("persona"); } }
  }
  function periodItem(item, key, panel, timeline = "period") {
    const card = element("article", "persona-card", ""), head = element("div", "persona-head", ""), field = `item:${item.item}`;
    const answers = (Array.isArray(item.answers) ? item.answers : []).map(value => Number.isInteger(value) ? String(value) : "无依据").join(" · ");
    const mine = Number.isInteger(item.mine), editing = panel.edit?.field === field;
    head.appendChild(element("h2", "", string(item.text, 80))); head.appendChild(element("span", "help", mine ? `你选 ${item.mine}（AI：${answers}）` : answers));
    if (!editing) {
      const edit = element("button", "plain persona-edit", mine ? "编辑修正" : "修正"); edit.type = "button";
      edit.addEventListener("click", () => { panel.edit = { field, period: panel.detail?.period }; renderTimelines(); });
      head.appendChild(edit);
    }
    card.appendChild(head);
    if (editing) card.appendChild(periodEditor(timeline, panel.detail?.period, string(panel.detail?.label, 40), field, string(item.text, 80), answers, mine ? item.mine : 3, mine));
    if (timeline === "worldview" && item.source) {
      card.appendChild(element("p", "help", `${string(item.source, 60)} · 1 强烈反对 · 2 反对 · 3 中立 · 4 同意 · 5 强烈同意`));
      const original = element("details", "persona-evidence", "");
      original.appendChild(element("summary", "", "英文原题")); original.appendChild(element("p", "", string(item.english, 400))); card.appendChild(original);
    }
    const reasons = [...new Set((Array.isArray(item.why) ? item.why : []).map(value => string(value, 2000)).filter(Boolean))];
    for (const why of reasons) card.appendChild(element("p", "persona-text", why));
    const itemStatus = { pending: "本题尚未分析", partial: "本题调研未完成", insufficient: "已调研，相关证据不足" }[item.status];
    if (itemStatus) card.appendChild(element("p", "help", itemStatus));
    const notes = [item.reverse === true ? "反向计分" : "", item.inner === true ? "问内心感受，只看自述" : "", Number.isInteger(item.situational) && item.situational ? `${item.situational} 条被事情推着的片段未计入` : ""].filter(Boolean);
    if (notes.length) card.appendChild(element("p", "help", notes.join(" · ")));
    if (Array.isArray(item.evidence) && item.evidence.length || item.evidenceTotal > 0) card.appendChild(periodEvidence(item, `${timeline}-${panel.detail?.period}-${key}-${item.item}`, panel, timeline));
    return card;
  }
  function clearPeriodDetail(panel) {
    panel.detailSerial++; panel.detail = null; panel.detailKey = ""; panel.edit = null; panel.evidencePages = {};
  }
  // Personality and worldview keep their own selections across tabs.
  function choosePeriod(id) {
    clearPeriodDetail(state.periods);
    state.periods.selected = id;
    renderTimelines(); renderOpinion("persona");
  }
  function chooseWorldview(id) {
    clearPeriodDetail(state.worldview); state.worldview.selected = id;
    renderWorldview(); renderOpinion("persona");
  }
  const PERIOD_STATUS = { pending: "尚未分析", partial: "调研未完成", complete: "调研完成", insufficient: "证据不足", error: "分析失败，待重试" };
  function periodContext(panel, period) {
    const parts = [], months = count(panel.value?.months);
    if (months) parts.push(`时间尺度：每 ${months} 个月`);
    if (period) parts.push(`当前：${period.label}`, PERIOD_STATUS[period.status] || "");
    const coverage = panel.value?.coverage;
    if (object(coverage) && count(coverage.total) !== null) {
      parts.push(`已完成 ${count(coverage.completed) ?? 0} / ${coverage.total} 期`);
      if (coverage.pending) parts.push(`${coverage.pending} 期待完成`);
      if (coverage.insufficient) parts.push(`${coverage.insufficient} 期证据不足`);
      if (coverage.failed) parts.push(`${coverage.failed} 期需重试`);
    }
    return parts.filter(Boolean).join(" · ");
  }
  // After the grain changes nothing fits it until the timeline has run with that length.
  const otherGrain = (panel, list, name) => !list.length && panel.value?.others === true ? `这个时间颗粒度还没有${name}结果，在分析菜单勾选“${name}”后开始分析。` : "";
  function renderPeriods() {
    const panel = state.periods, list = periodList(), host = byId("personaPeriods"), detail = byId("personaPeriodDetail"), period = chosenPeriod();
    message("personaPeriodContext", periodContext(panel, period) || "按聊天所属时期观察性格变化");
    message("personaPeriodLine", panel.error || otherGrain(panel, list, "性格时间线"));
    byId("personaPeriodEdit").replaceChildren(...stripEditor("period", list));
    if (!list.length) { host.replaceChildren(); detail.replaceChildren(); return; }
    Charts.periods(host, { periods: list, selected: period?.id || "", detail: panel.detailKey, help: PERIOD_HELP }, {
      onSelect: choosePeriod, onEdit: (field, id) => openEdit("period", field, id),
      onDetail: key => { void openPeriodDetail(key); } });
    const items = Array.isArray(panel.detail?.items) ? panel.detail.items.filter(object) : [];
    detail.replaceChildren(...items.map(item => periodItem(item, panel.detailKey, panel)));
  }
  async function loadPeriods() {
    const panel = state.periods;
    if (!account() || state.disabled || panel.pending || state.tab !== "persona") return;
    const epoch = state.epoch, original = account(), serial = ++panel.serial;
    panel.pending = true;
    try {
      const result = await window.kyfHost.rpc("self.periods", {}, scope());
      if (!current(epoch, original) || serial !== panel.serial) return;
      if (object(result) && Array.isArray(result.periods)) { panel.value = result; panel.error = ""; }
    } catch (error) { if (current(epoch, original) && serial === panel.serial) panel.error = friendly(error); }
    finally { if (current(epoch, original) && serial === panel.serial) { panel.pending = false; renderPeriods(); renderOpinion("persona"); } }
  }
  async function openPeriodDetail(key, reload = false) {
    const panel = state.periods, period = chosenPeriod();
    if (!period || !account()) return;
    if (!reload && panel.detailKey === key) { clearPeriodDetail(panel); renderPeriods(); return; }
    const epoch = state.epoch, original = account(), serial = ++panel.detailSerial;
    panel.detailKey = key; panel.detail = null; panel.evidencePages = {}; renderPeriods();
    try {
      const result = await window.kyfHost.rpc("self.periodDetail", { period: period.id, key }, scope());
      if (current(epoch, original) && serial === panel.detailSerial && object(result)) { panel.detail = result; panel.error = ""; }
    } catch (error) { if (current(epoch, original) && serial === panel.detailSerial) panel.error = friendly(error); }
    if (current(epoch, original) && serial === panel.detailSerial) renderPeriods();
  }
  // 三观时间线: the selected period also drives the two charts below it.
  const WORLDVIEW_SCALES = [{ key: "pol_econ", tone: 0 }, { key: "pol_lib", tone: 1 }, { key: "val_change", tone: 2 }, { key: "val_self", tone: 3 }];
  const worldviewList = () => Array.isArray(state.worldview.value?.periods) ? state.worldview.value.periods.filter(object) : [];
  function chosenWorldview() {
    const list = worldviewList();
    return list.find(period => period.id === state.worldview.selected) || list[list.length - 1] || null;
  }
  function worldviewCard(period, key) {
    const panel = state.worldview, keys = OPINIONS.values.keys, field = `view:${key}`, item = object(period.views?.[key]) ? period.views[key] : {};
    const text = string(item.text, 200), mine = (period.edited || []).includes(field), editing = panel.edit?.field === field;
    const card = element("article", "persona-card", ""), head = element("div", "persona-head", ""), ai = string(period.ai?.[field], 200);
    head.appendChild(element("h2", "", keys[key]));
    if (!text) head.appendChild(element("span", "help", item.status === "pending" ? "本期尚未分析" : "本期证据不足"));
    if (!editing) {
      const edit = element("button", "plain persona-edit", mine ? "编辑修正" : "修正"); edit.type = "button";
      edit.addEventListener("click", () => { panel.edit = { field, period: period.id }; renderWorldview(); });
      head.appendChild(edit);
    }
    card.appendChild(head);
    if (editing) card.appendChild(periodEditor("worldview", period.id, period.label, field, keys[key], ai, text, mine));
    if (text && !editing) card.appendChild(element("p", "persona-text", text));
    if (mine && !editing) card.appendChild(element("p", "help", `AI 判断：${ai || "无"}`));
    if (Array.isArray(item.evidence) && item.evidence.length || item.evidenceTotal > 0) {
      const id = `worldview-${period.id}-${key}`;
      card.appendChild(periodEvidence(item, id, panel, "worldview", { period: period.id, reload: () => {
        delete panel.evidencePages[id]; return loadWorldview();
      } }));
    }
    return card;
  }
  function renderWorldviewNotice() {
    const panel = state.worldview, list = worldviewList(), run = state.analysis;
    const step = run?.outputs?.includes("worldview") && run.steps?.find(item => item?.key === "worldview");
    let activity = "";
    if (step?.state === "error") activity = "三观时间线分析失败，可从顶部重试；已完成结果保留。";
    else if (step?.state === "paused") activity = "三观时间线已暂停，可从顶部继续分析。";
    else if (step && ["pending", "running"].includes(step.state)) {
      activity = run.state === "running" ? step.state === "pending" ? "三观时间线排队中，前面的分析完成后会自动开始。" :
        "正在分析三观时间线，已完成的时期会陆续显示。" : "三观时间线尚未完成，可从顶部继续分析。";
    }
    message("valuesPeriodLine", panel.error || activity || otherGrain(panel, list, "三观时间线") ||
      (!list.length && panel.value?.legacy ? "政治题集已更新为 8values，请在分析菜单勾选“三观时间线”后开始分析。旧题集结果仍保存在本机。" : ""));
  }
  function renderWorldview() {
    const panel = state.worldview, list = worldviewList(), host = byId("valuesPeriods"), detail = byId("valuesPeriodDetail"), views = byId("valuesPeriodViews"), period = chosenWorldview();
    message("valuesPeriodContext", periodContext(panel, period) || "按聊天所属时期观察三观变化");
    renderWorldviewNotice();
    byId("valuesPeriodEdit").replaceChildren(...stripEditor("worldview", list));
    if (!list.length) { host.replaceChildren(); detail.replaceChildren(); views.replaceChildren(); return; }
    Charts.periods(host, { title: "三观时间线", instrument: "政治：8values · 价值观：自拟 12 题", scales: WORLDVIEW_SCALES, periods: list, selected: period?.id || "", detail: panel.detailKey, help: WORLDVIEW_HELP,
      strips: [["政治", item => (item.letters || []).join(""), "type"], ["价值观", item => string(item.style, 40), "style"]] }, {
      onSelect: chooseWorldview, onEdit: (field, id) => openEdit("worldview", field, id),
      onDetail: key => { void openWorldviewDetail(key); } });
    const items = Array.isArray(panel.detail?.items) ? panel.detail.items.filter(object) : [];
    detail.replaceChildren(...items.map(item => periodItem(item, panel.detailKey, panel, "worldview")));
    views.replaceChildren(...Object.keys(OPINIONS.values.keys).map(key => worldviewCard(period, key)));
  }
  async function loadWorldview() {
    const panel = state.worldview;
    if (!account() || state.disabled || panel.pending || state.tab !== "values") return;
    const epoch = state.epoch, original = account(), serial = ++panel.serial;
    panel.pending = true;
    try {
      const result = await window.kyfHost.rpc("self.worldview", {}, scope());
      if (!current(epoch, original) || serial !== panel.serial) return;
      if (object(result) && Array.isArray(result.periods)) { panel.value = result; panel.error = ""; }
    } catch (error) { if (current(epoch, original) && serial === panel.serial) panel.error = friendly(error); }
    finally { if (current(epoch, original) && serial === panel.serial) { panel.pending = false; renderTimelines(); renderOpinion("persona"); } }
  }
  async function openWorldviewDetail(key, reload = false) {
    const panel = state.worldview, period = chosenWorldview();
    if (!period || !account()) return;
    if (!reload && panel.detailKey === key) { clearPeriodDetail(panel); renderWorldview(); return; }
    const epoch = state.epoch, original = account(), serial = ++panel.detailSerial;
    panel.detailKey = key; panel.detail = null; panel.evidencePages = {}; renderWorldview();
    try {
      const result = await window.kyfHost.rpc("self.worldviewDetail", { period: period.id, key }, scope());
      if (current(epoch, original) && serial === panel.detailSerial && object(result)) { panel.detail = result; panel.error = ""; }
    } catch (error) { if (current(epoch, original) && serial === panel.detailSerial) panel.error = friendly(error); }
    if (current(epoch, original) && serial === panel.detailSerial) renderWorldview();
  }
  function renderValueCards() {
    const keys = OPINIONS.values.keys, value = state.opinions.values.value;
    const dimensions = Array.isArray(value?.dimensions) ? value.dimensions.filter(item => object(item) && Object.hasOwn(keys, item.key)) : [];
    byId("valuesHistory").hidden = !dimensions.length;
    byId("valuesList").replaceChildren(...dimensions.map(dimension => opinionCard("values", dimension, keys[dimension.key], [
      { title: "自述", value: dimension.selfReport, id: "values-evidence-" + dimension.key + "-selfReport" },
      { title: "推测", value: dimension.inference, id: "values-evidence-" + dimension.key + "-inference" }])));
  }
  const notice = kind => state.opinions[kind].notice || (state.opinions[kind].value?.coverage?.stale === true ? STALE : "");
  function renderOpinion(kind) {
    const active = document.activeElement, focus = /^(persona|values)-manual-/.test(active?.id || "") ? { id: active.id, start: active.selectionStart, end: active.selectionEnd, scroll: active.scrollTop } : null;
    const lists = kind === "persona" ? ["personaList", "valuesChartList"] : ["valuesList"];
    for (const id of lists) for (const details of byId(id).querySelectorAll?.("details[id]") || []) {
      if (details.open) state.opinions[kind].expanded.add(details.id); else state.opinions[kind].expanded.delete(details.id);
    }
    if (kind === "persona") renderCharts(); else renderValueCards();
    // 三观分析 reads both stores, so its line reports either one.
    message("personaMessage", notice("persona")); message("valuesMessage", notice("values") || notice("persona"));
    byId("personaReload").hidden = !notice("persona"); byId("valuesReload").hidden = !byId("valuesMessage").textContent;
    for (const id of ["personaReload", "valuesReload"]) byId(id).disabled = !account() || Object.values(state.opinions).some(panel => panel.pending || panel.busy);
    if (focus) {
      const input = byId(focus.id);
      if (input && !input.disabled) { input.focus({ preventScroll: true }); input.setSelectionRange?.(focus.start, focus.end); input.scrollTop = focus.scroll; }
    }
  }
  async function loadOpinion(kind) {
    const panel = state.opinions[kind];
    if (!account() || state.disabled || panel.pending || panel.busy) return;
    const epoch = state.epoch, original = account(), serial = ++panel.serial;
    panel.pending = true;
    try {
      const result = await window.kyfHost.rpc(OPINIONS[kind].rpc, {}, scope());
      if (!current(epoch, original) || serial !== panel.serial) return;
      if (!object(result) || !Array.isArray(result.dimensions) || result.version === undefined) throw new Error(OPINIONS[kind].noun + "分析暂时无法读取，请重试。");
      if (panel.value?.version !== result.version) panel.evidencePages = {};
      panel.value = result; panel.notice = "";
    } catch (error) { if (current(epoch, original) && serial === panel.serial) panel.notice = friendly(error); }
    finally { if (current(epoch, original) && serial === panel.serial) { panel.pending = false; renderOpinion(kind); } }
  }
  async function saveOpinion(kind, key) {
    const panel = state.opinions[kind];
    if (!account() || state.disabled || panel.busy || !Object.hasOwn(OPINIONS[kind].keys, key) || !panel.dirty.has(key)) return;
    const text = panel.drafts[key];
    if (text.length > 2000) { panel.notice = "每项修正最多 2000 字，请缩短后保存。"; renderOpinion(kind); return; }
    const epoch = state.epoch, original = account(), serial = ++panel.serial;
    panel.busy = true; panel.pending = false; renderOpinion(kind);
    try {
      const result = await window.kyfHost.rpc(OPINIONS[kind].edit, { key, text, expectedVersion: panel.value.version }, scope());
      if (!current(epoch, original) || serial !== panel.serial) return;
      if (!object(result) || !Array.isArray(result.dimensions) || result.version === undefined) throw new Error("保存结果暂时无法确认，请读取最新核对。");
      panel.value = result; panel.dirty.delete(key); panel.editing.delete(key); delete panel.drafts[key]; panel.notice = "";
    } catch (error) {
      if (!current(epoch, original) || serial !== panel.serial) return;
      panel.notice = ["conflict", "version-conflict"].includes(error?.code) ? OPINIONS[kind].noun + "资料已在其他页面更新，你的修正已保留，请读取最新后重试。" : friendly(error);
    } finally { if (current(epoch, original) && serial === panel.serial) { panel.busy = false; renderOpinion(kind); } }
  }
  function resetTags() {
    state.tagSerial++; state.evidenceSerial++;
    Object.assign(state, { tags: null, tagPending: false, tagBusy: false, tagSelected: "", tagUndo: "", tagError: "" });
    byId("tagForm").hidden = true; byId("tagName").value = ""; renderTags();
  }
  async function openSettings(section) {
    try { await window.kyfHost.host("settings.open", { section, account: account() }); }
    catch (error) { message("error", friendly(error)); }
  }
  function changeContext(next) {
    if (!object(next)) return;
    const previous = account(), changed = previous !== string(next.account, 240);
    state.context = next;
    if (!changed) return;
    const discarded = state.dirty;
    state.epoch++; state.statusSerial++; state.profileSerial++; state.searchSerial++; state.exclusionSerial++; state.analysisSerial++; cancelTimer(); cancelAnalysisTimer();
    judgeShadowSerial++; byId("judgeShadowRefresh").disabled = false; message("judgeShadowSummary", ""); byId("judgeShadowPanel").open = false;
    Object.assign(state, { status: null, statusPending: false, busy: false, profileReady: false, profileVersion: null, profileBusy: false, profileStale: false, dirty: false, searching: false, exclusionsBusy: false, disabled: false });
    clearDraft(); Object.assign(state, { analysis: null, analysisPending: false, analysisBusy: false, analysisAction: "", analysisError: "", deliveredDraft: "", analysisOutputs: [...DEFAULT_OUTPUTS], outputsReady: false, outputsVersion: 0, outputsSettingsBusy: false, outputsSettingsLoaded: false, menuOpen: false, panelOpen: false, wasRunning: false });
    for (const kind of Object.keys(OPINIONS)) state.opinions[kind] = freshOpinion(state.opinions[kind].serial + 1);
    state.chartSelected = { persona: null, values: null }; state.periods = freshPeriods(state.periods.serial + 1); state.worldview = freshPeriods(state.worldview.serial + 1); state.outputsSettingsSerial++;
    byId("valuesHistory").open = false;
    state.analysisDepth = ""; byId("background").value = ""; byId("style").value = ""; byId("query").value = ""; byId("trialInput").value = "";
    byId("samples").replaceChildren(); byId("excludedSamples").replaceChildren(); byId("exclusionsPanel").hidden = true; byId("latestProfile").hidden = true; byId("resetConfirm").hidden = true; selectTab("profile");
    for (const id of ["error", "profileError", "profileMessage", "searchMessage"]) message(id, "");
    message("notice", discarded ? "已切换账号，上一账号未保存的输入已清除。" : "");
    clearTrial(); resetTags(); renderAnalysis();
    state.timelineSerial++; closeTimelineMenu(); hideTimelineTip();
    Object.assign(state, { timeline: null, timelinePending: false, timelineBusy: false, timelineError: "",
      timelineOpen: "", timelineEvidence: {}, timelineFilter: "", timelineEditing: "", timelineWhen: null,
      timelineNotice: "", timelineUndoQueued: 0, timelineAddType: null, timelineManage: false, timelineTrack: "",
      timelineUnknownOpen: false });
    state.timelineCollapsed = new Set(); state.timelineExpanded = new Set();
    renderTimeline();
    for (const kind of Object.keys(OPINIONS)) renderOpinion(kind);
    if (account()) { void loadStatus(); void loadAnalysis(); void loadAnalysisSettings(); void loadProfile().then(() => loadDraftState()); }
  }
  for (const key of tabs) {
    byId("tab-" + key).addEventListener("click", () => selectTab(key));
    byId("tab-" + key).addEventListener("keydown", event => {
      const index = tabs.indexOf(key), next = event.key === "ArrowRight" ? tabs[(index + 1) % tabs.length] : event.key === "ArrowLeft" ? tabs[(index + tabs.length - 1) % tabs.length] : event.key === "Home" ? tabs[0] : event.key === "End" ? tabs.at(-1) : null;
      if (next) { event.preventDefault(); selectTab(next, true); }
    });
  }
  for (const id of ["analysisRun", "draftProfile", "tagsRun", "tagsStart"]) byId(id).addEventListener("click", () => { void analysisCall(); });
  for (const id of ["analysisPause", "pauseDraft"]) byId(id).addEventListener("click", () => { void analysisCall(true); });
  byId("reset").addEventListener("click", () => { byId("resetConfirm").hidden = false; });
  byId("cancelReset").addEventListener("click", () => { byId("resetConfirm").hidden = true; });
  byId("confirmReset").addEventListener("click", () => { void mutate("self.reset"); });
  byId("saveProfile").addEventListener("click", () => { void saveProfile(); });
  byId("reloadProfile").addEventListener("click", () => { void loadProfile(); });
  byId("analysisDepthSettings").addEventListener("click", () => { void openSettings("plugin"); });
  for (const id of ["personaPeriodSettings", "valuesPeriodSettings"]) byId(id).addEventListener("click", () => { void openSettings("plugin"); });
  for (const key of OUTPUTS) byId("analysisOutput-" + key).addEventListener("change", () => { void changeAnalysisOutputs(); });
  byId("useDraftBackground").addEventListener("click", () => useDraft("background"));
  byId("useDraftStyle").addEventListener("click", () => useDraft("style"));
  byId("closeDraft").addEventListener("click", () => { byId("draftPanel").hidden = true; });
  for (const id of ["background", "style"]) byId(id).addEventListener("input", () => { state.dirty = true; message("profileMessage", "未保存"); renderProfile(); });
  byId("sourceSettings").addEventListener("click", () => { void openSettings("source"); });
  byId("modelSettings").addEventListener("click", () => { void openSettings("model"); });
  byId("search").addEventListener("click", () => { void search(); });
  byId("query").addEventListener("input", () => { state.searchSerial++; state.searching = false; render(); });
  // Enter submits as in WeChat; Shift + Enter keeps a line break, and IME composition is never cut short.
  const onEnter = action => event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void action(); } };
  byId("query").addEventListener("keydown", onEnter(search));
  byId("trialInput").addEventListener("keydown", onEnter(sendTrial));
  byId("trialSend").addEventListener("click", () => { void sendTrial(); });
  byId("clearTrial").addEventListener("click", clearTrial);
  byId("addTag").addEventListener("click", () => { byId("tagForm").hidden = !byId("tagForm").hidden; if (!byId("tagForm").hidden) byId("tagName").focus(); });
  byId("cancelTag").addEventListener("click", () => { byId("tagForm").hidden = true; byId("tagName").value = ""; });
  byId("saveTag").addEventListener("click", () => { void saveTag(); });
  byId("tagName").addEventListener("keydown", onEnter(saveTag));
  byId("confirmTag").addEventListener("click", () => { const tag = selectedTag(); if (tag) void tagCall("self.tagEdit", { action: tag.level === "confirmed" ? "reset" : "confirm", name: tag.name }); });
  byId("deleteTag").addEventListener("click", async () => {
    const tag = selectedTag();
    if (tag && await tagCall("self.tagEdit", { action: "delete", name: tag.name })) { state.tagSelected = ""; state.tagUndo = tag.name; renderTags(); }
  });
  byId("undoTag").addEventListener("click", async () => {
    const name = state.tagUndo;
    if (name && await tagCall("self.tagEdit", { action: "reset", name })) { state.tagUndo = ""; selectTagNamed(name); }
  });
  byId("tagModelSettings").addEventListener("click", () => { void openSettings("model"); });
  byId("timelineAdd").addEventListener("click", () => { state.timelineAddType = state.timelineAddType === null ? "event" : null; renderTimeline(); });
  byId("timelineImportantOnly").addEventListener("change", () => { state.timelineImportantOnly = byId("timelineImportantOnly").checked; renderTimeline(); });
  byId("analysisModelSettings").addEventListener("click", () => { void openSettings("model"); });
  byId("personaReload").addEventListener("click", () => { void loadOpinion("persona"); void loadPeriods(); });
  byId("valuesReload").addEventListener("click", () => { void loadOpinion("values"); void loadOpinion("persona"); void loadWorldview(); });
  // The content menu and the progress overlay float over the page; one is open at a time, and Esc or a click elsewhere closes it.
  function closePopovers(focus = false) {
    const target = state.menuOpen ? "analysisMenuToggle" : state.panelOpen ? "analysisStatus" : "";
    state.menuOpen = state.panelOpen = false; renderAnalysis();
    if (focus && target) byId(target).focus();
  }
  byId("analysisMenuToggle").addEventListener("click", () => { state.menuOpen = !state.menuOpen; state.panelOpen = false; renderAnalysis(); });
  byId("analysisStatus").addEventListener("click", () => { state.panelOpen = byId("analysisPanel").hidden; state.menuOpen = false; renderAnalysis(); });
  byId("analysisPanelClose").addEventListener("click", () => closePopovers(true));
  window.addEventListener("keydown", event => { if (event.key === "Escape" && (state.menuOpen || state.panelOpen)) closePopovers(true); });
  window.addEventListener("keydown", event => {
    if (event.key === "Escape") closeTimelineMenu();
    // 输入框里的 Ctrl+Z 留给文字撤销；其余时候撤销时间线上最近一次修改。
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "z" && !event.repeat &&
      state.tab === "timeline" && !event.target?.closest?.("textarea, select, [contenteditable], input:not([type=checkbox])")) {
      event.preventDefault(); void undoTimeline();
    }
  });
  window.addEventListener("pointerdown", event => { if (!event.target?.closest?.(".tl-menu")) closeTimelineMenu(); });
  window.addEventListener("blur", () => { closeTimelineMenu(); hideTimelineTip(); });
  byId("tabContent").addEventListener("scroll", () => { closeTimelineMenu(); hideTimelineTip(); });
  window.addEventListener("pointerdown", event => { if ((state.menuOpen || state.panelOpen) && !event.target?.closest?.(".run-control")) closePopovers(); });
  window.kyfHost.on(event => {
    if (event.type === "context") changeContext(event.context);
    else if (event.type === "event" && event.event?.type === "shown") { void loadStatus(); void loadModelStatus(); void loadAnalysis(); void loadDraftState(); if (state.tab === "tags") void loadTags(); if (state.tab === "timeline") void loadTimeline(); if (state.tab === "persona") void loadPeriods(); if (state.tab === "values") void loadWorldview(); if (Object.hasOwn(OPINIONS, state.tab)) { void loadOpinion(state.tab); if (state.tab === "values") void loadOpinion("persona"); } }
    else if (event.type === "event" && event.event?.type === "account-cleared" && event.event.account === account()) changeContext({ ...state.context, account: "", user: "" });
  });
  window.addEventListener("pagehide", () => { cancelTimer(); cancelTrialTimer(); cancelAnalysisTimer(); });
  void window.kyfHost.ready().then(() => { changeContext(window.kyfHost.context()); void loadModelStatus(); });
  selectTab("profile"); render(); renderTags(); renderTimeline();
})();
