"use strict";
// Know Your Friends, Apache-2.0. No provider access, HTML rendering or body persistence.
// The main page keeps the developer-mode settings and opens the 模型调用 window. In that window a
// call record shows the captured request, response and errors of its attempt through this viewer.
(function () {
  const $ = id => document.getElementById(id);
  const UNKNOWN = "未采集";
  const KINDS = { request: [["request", "原始请求"]], response: [["text", "模型正文"], ["response", "原始返回"], ["result", "应用结果"]] };
  const TITLES = { request: "原始请求（已脱敏）", response: "原始返回（已脱敏）", text: "模型正文（已采集的累积文本）", result: "应用处理结果" };
  const taskEvent = row => row.kind === "task-event" || row.kind === "task" || row.requestCount === 0;
  function statusOf(row) {
    const capture = row.captureStatus || row.collectionStatus;
    const transport = row.transportStatus || row.status;
    const application = row.applicationStatus || row.processingStatus;
    const adoption = row.adoptionStatus || row.adoption || row.disposition;
    const success = row.kind === "local" ? "推理成功" : "返回成功";
    if (["cancelled", "canceled"].includes(transport) || ["cancelled", "canceled", "stopped"].includes(application)) return { key: "cancelled", label: "已取消" };
    if (["error", "failed", "interrupted"].includes(transport) || ["error", "failed", "parse_error", "validation_error", "parse-failed", "validation-failed"].includes(application))
      return { key: "error", label: ["success", "complete", "completed"].includes(transport) ? `${success} · 解析／校验失败` : row.kind === "local" ? "推理失败" : "请求失败" };
    if (["stopped", "interrupted", "paused"].includes(capture)) return { key: "unknown", label: capture === "interrupted" ? "进程中断 · 业务结果未知" : "采集已停止 · 业务结果未知" };
    if (["discarded", "not_adopted", "not-adopted", "rejected"].includes(adoption)) return { key: "not_adopted", label: `${success} · 结果未采用` };
    if (["done", "complete", "completed", "success"].includes(transport)) {
      if (["pending", "processing"].includes(application) || adoption === "pending") return { key: "running", label: `${success} · 处理中` };
      if (["success", "complete", "completed"].includes(application) && ["adopted", "not-applicable", "not_applicable"].includes(adoption))
        return { key: "complete", label: row.kind === "local" ? "推理成功" : "完成" };
      return { key: "unknown", label: `${success} · 应用结果未采集` };
    }
    if (["streaming", "receiving"].includes(transport)) return { key: "running", label: "接收中" };
    if (taskEvent(row)) return { key: "unknown", label: "未发出模型请求" };
    if (transport === "unknown" || ["unknown", "incomplete"].includes(row.status)) return { key: "unknown", label: "业务结果未知" };
    return { key: "running", label: "等待响应" };
  }
  function normalizeRecord(row) {
    const date = value => typeof value === "number" && value < 1e12 ? value * 1000 : value;
    return { ...row, id: row.recordId || row.id, user: row.contactId || row.user, threadId: row.sessionId || row.threadId,
      startedAt: date(row.startedAt), endedAt: date(row.finishedAt || row.endedAt), firstByteAt: date(row.firstByteAt || row.firstResponseAt), firstTextAt: date(row.firstTextAt),
      incomplete: (row.complete === false && row.collectionStatus !== "recording") || (!taskEvent(row) && !!row.incompleteReason), truncated: row.requestTruncated || row.responseTruncated || row.truncated };
  }
  function bytes(value) { return Number.isFinite(value) ? value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1048576).toFixed(1)} MB` : UNKNOWN; }
  function showValue(value) { return value === undefined || value === null || value === "" ? UNKNOWN : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value); }
  function node(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
  function button(label, handler) { const item = node("button", label); item.type = "button"; item.addEventListener("click", handler); return item; }
  function time(value) { if (!value) return UNKNOWN; const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value); return Number.isNaN(date.getTime()) ? UNKNOWN : date.toLocaleString(); }
  const state = { initialized: false, standalone: false, account: "", user: "", view: "chat", scopeEpoch: 0, readEpoch: 0, settingsSerial: 0,
    setting: null, settingError: "", savedOperation: null, configuring: false, session: null, scopePromise: null, deleteVersion: null,
    timer: null, viewers: new Map(), onSettings: null, historySnapshot: "" };
  const host = async (operation, args) => {
    try { return window.desktopHost?.modelTracesRequest ? await window.desktopHost.modelTracesRequest(operation, args) : { ok: false, error: "desktop-required", statusCode: 503 }; }
    catch { return { ok: false, error: "trace-unavailable", statusCode: 503 }; }
  };
  // Fences every outstanding read. Visible bodies are removed at once; a viewer that is still shown
  // reloads unless the reason says why it cannot (for example, an expired account scope).
  function invalidate(reason = "") {
    state.readEpoch++;
    for (const item of state.viewers.values()) {
      item.serial++; item.bodySerial++; item.detail = null; item.detailBusy = false; item.body = undefined; item.bodyBusy = false; item.bodyError = ""; item.error = reason;
      if (item.element) { drawContent(item); if (!reason) void loadDetail(item); }
    }
  }
  function scopeType() { return state.account ? "account" : "application"; }
  async function ensureScope(force = false) {
    const scope = scopeType();
    if (!force && state.session?.scope === scope) return true;
    if (!force && state.scopePromise) return state.scopePromise;
    const ticket = ++state.scopeEpoch; state.session = null;
    state.scopePromise = host("scope", { scope }).then(result => {
      if (ticket !== state.scopeEpoch) return false;
      if (!result.ok || (scope === "account" && result.accountId !== state.account)) throw new Error("scope-expired");
      state.session = result; state.deleteVersion = result.deleteVersion; return true;
    }).catch(() => false).finally(() => { if (ticket === state.scopeEpoch) state.scopePromise = null; });
    return state.scopePromise;
  }
  async function request(operation, args = {}) {
    if (!await ensureScope()) return { ok: false, error: "trace-unavailable" };
    const scope = state.scopeEpoch, generation = state.readEpoch;
    const result = await host(operation, args);
    if (scope !== state.scopeEpoch || generation !== state.readEpoch) return { ok: false, stale: true };
    if (result.statusCode === 409 || result.statusCode === 403) {
      state.session = null; invalidate("账号范围已变化，请收起后重新展开");
      return { ok: false, stale: true };
    }
    if (result.deleteVersion !== undefined && state.deleteVersion !== null && result.deleteVersion !== state.deleteVersion) {
      state.deleteVersion = result.deleteVersion; invalidate();
      if (operation !== "clear" && operation !== "state") return { ok: false, stale: true };
    }
    if (result.deleteVersion !== undefined) state.deleteVersion = result.deleteVersion;
    return result;
  }
  function healthLabel() {
    const data = state.setting;
    if (!data) return "记录服务不可用";
    if (data.status === "starting") return "正在开启";
    if (data.status === "stopping") return "正在关闭";
    if (data.status === "paused") return `采集已暂停${data.pauseReason ? `：${data.pauseReason}` : ""}`;
    return data.collecting ? "正在记录" : "采集已关闭";
  }
  function renderCaptureHistory(data) {
    const details = $('modelTraceHistory'), list = $('modelTraceHistoryList');
    if (!details || !list) return;
    const history = Array.isArray(data?.pauseHistory) ? data.pauseHistory : [];
    details.hidden = history.length === 0;
    $('modelTraceHistoryCount').textContent = `${history.length} 次`;
    if (!history.length) details.open = false;
    const snapshot = JSON.stringify(history);
    if (snapshot === state.historySnapshot) return;
    state.historySnapshot = snapshot;
    const date = value => {
      if (!value) return null;
      const result = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
      return Number.isNaN(result.getTime()) ? null : result;
    };
    const format = (value, onlyTime = false) => value ? value.toLocaleString("zh-CN", {
      ...(onlyTime ? {} : { year: "numeric", month: "2-digit", day: "2-digit" }),
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }) : "时间未记录";
    const rows = history.map(item => ({
      start: date(item.startedAt || item.from), end: date(item.finishedAt || item.endedAt || item.to), reason: item.reason || "采集暂停",
    })).sort((a, b) => (b.start?.getTime() || 0) - (a.start?.getTime() || 0));
    const scrollTop = list.scrollTop;
    list.replaceChildren(...rows.map(item => {
      const row = node("li");
      const sameDay = item.start && item.end && item.start.toDateString() === item.end.toDateString();
      row.append(node("p", item.reason, "model-traces-history-reason"), node("p",
        `${format(item.start)} — ${item.end ? format(item.end, sameDay) : "恢复时间未记录"}`, "model-traces-history-time"));
      return row;
    }));
    list.scrollTop = scrollTop;
  }
  function renderSettings() {
    state.onSettings?.(state.setting);
    if (!$('btnDeveloperMode')) return;
    const data = state.setting, busy = state.configuring;
    $('btnDeveloperMode').disabled = !data || busy;
    $('btnDeveloperMode').setAttribute("aria-checked", String(!!data?.enabled));
    $('btnDeveloperMode').textContent = busy && state.savedOperation.enabled !== undefined ? (state.savedOperation.enabled ? "正在开启…" : "正在关闭…") : data?.enabled ? "已开启" : "已关闭";
    $('modelTraceRetention').disabled = !data || busy;
    $('modelTraceRetention').value = String(data?.retentionDays || 7);
    $('modelTraceStorage').textContent = data ? `${healthLabel()} · 已保存 ${(data.recordCount ?? 0).toLocaleString("zh-CN")} 条 · ${bytes(data.storageBytes)}` : "记录服务不可用，请在桌面客户端中重试";
    const error = state.settingError || data?.saveError || "";
    if ($('modelTraceError')) { $('modelTraceError').textContent = error; $('modelTraceError').hidden = !error; }
    renderCaptureHistory(data);
    $('btnRetryModelTraces').hidden = !error;
    $('btnRetryModelTraces').textContent = state.savedOperation ? "重试保存" : "刷新状态";
  }
  async function refreshSettings() {
    const result = await request("state");
    if (result.stale) return;
    if (result.ok) { state.setting = result.state || result; if (!state.savedOperation) state.settingError = ""; }
    else state.settingError = "记录服务不可用，可重试读取";
    renderSettings();
  }
  async function configure(value) {
    const serial = ++state.settingsSerial; state.savedOperation = value; state.configuring = true; state.settingError = ""; renderSettings();
    const result = await request("configure", value);
    if (serial !== state.settingsSerial) return;
    state.configuring = false;
    if (result.ok || result.state) {
      state.setting = result.state || result;
      state.settingError = result.saveError || result.state?.saveError || "";
      state.savedOperation = state.settingError ? value : null;
    } else {
      if (value.enabled === false && state.setting) state.setting = { ...state.setting, collecting: false, status: "off" };
      state.settingError = value.enabled === false ? "关闭未得到确认；本次停止状态需刷新核对，重启可能恢复原设置" : "设置保存失败，已保留原设置";
      state.savedOperation = value;
    }
    renderSettings();
  }
  async function copy(item, value) {
    if (typeof value !== "string" || !value) return;
    try {
      const copied = window.desktopHost?.copyDraft ? await window.desktopHost.copyDraft(value) : await navigator.clipboard.writeText(value).then(() => true);
      if (item.feedback) item.feedback.textContent = copied ? "已复制。剪贴板副本不受记录清理和保留期限控制。" : "复制失败，请选择文字后复制";
    } catch { if (item.feedback) item.feedback.textContent = "复制失败，请选择文字后复制"; }
  }
  function addBlock(item, parent, title, value, folded = false) {
    const text = showValue(value), container = node(folded ? "details" : "section");
    if (folded) container.appendChild(node("summary", title));
    const head = node("div", undefined, "model-traces-block-title"); head.append(node("span", folded ? "" : title), button("复制", () => copy(item, text)));
    const pre = node("pre", text); pre.tabIndex = 0;
    const needle = item.needle.trim();
    if (needle) {
      const match = text.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase());
      if (match >= 0) pre.replaceChildren(document.createTextNode(text.slice(0, match)), node("mark", text.slice(match, match + needle.length)), document.createTextNode(text.slice(match + needle.length)));
    }
    container.append(head, pre); parent.appendChild(container);
  }
  function requestMessages(item, parent, actual, row) {
    if (!actual || typeof actual !== "object") return;
    if (row.kind === "local") { addBlock(item, parent, "本地推理输入与问题组", actual); return; }
    for (const key of ["instructions", "system", "systemInstruction"]) if (actual[key] !== undefined) addBlock(item, parent, key, actual[key]);
    const messages = actual.messages ?? actual.input ?? actual.contents;
    if (Array.isArray(messages)) for (let index = 0; index < messages.length; index++) {
      const message = messages[index];
      const label = message && typeof message === "object" ? message.role || message.type || "input" : "input";
      // Keep tool-call IDs, arguments, names and other wire fields beside the
      // content, rather than dropping them from the readable message view.
      const fields = message && typeof message === "object" ? Object.keys(message) : [];
      const contentOnly = fields.every(key => ["role", "content"].includes(key)) && fields.includes("content");
      addBlock(item, parent, `${index + 1}. ${label}`, contentOnly ? message.content : message);
    }
    else if (messages !== undefined) addBlock(item, parent, "input", messages);
    if (actual.tools) addBlock(item, parent, "工具定义", actual.tools, true);
    if (actual.previous_response_id) parent.appendChild(node("p", `服务端历史引用：${actual.previous_response_id}。未取得的远程历史不可见。`, "model-traces-note"));
    const parameters = Object.fromEntries(["model", "stream", "max_tokens", "max_output_tokens", "max_completion_tokens", "temperature", "top_p", "response_format", "tool_choice", "parallel_tool_calls", "thinking", "reasoning", "reasoning_effort"].map(key => [key, actual[key] ?? "未指定，由服务端决定"]));
    if (actual.contents && !Object.prototype.hasOwnProperty.call(actual, "model")) parameters.model = "通过请求路径指定，见过程与错误";
    for (const key of ["generationConfig", "toolConfig", "options", "format", "text", "think"]) if (actual[key] !== undefined) parameters[key] = actual[key];
    addBlock(item, parent, "实际发送参数", parameters, true);
  }
  // Technical metadata of the captured attempt; the call record already shows friend, step, usage and timing.
  function metadata(parent, row) {
    const local = row.kind === "local";
    const pairs = [["请求状态", statusOf(row).label], ["应用处理", row.applicationStatus || row.processingStatus], ["结果采用", row.adoptionStatus || row.adoption || row.disposition],
      ["采集状态", row.captureStatus || row.collectionStatus], ["完整性", row.incomplete || row.truncated ? "记录不完整" : row.captureStatus === "complete" ? "本次采集已结束" : ""],
      ["配置协议", local ? "本地推理，不适用 HTTP" : row.configuredProtocol || row.protocol], ["传输协议", local ? "" : row.transportProtocol],
      ["服务 / 路径", local ? "" : [row.endpoint || row.baseUrl, row.path].filter(Boolean).join(" ")], ["请求方法", row.method], ["HTTP 状态", local ? "" : row.httpStatus],
      ["结束原因", row.finishReason || row.finish_reason], ["服务请求编号", row.providerRequestId || row.requestId], ["服务报告用量", local ? "" : row.usage],
      ["首个响应字节", row.firstByteAt ? time(row.firstByteAt) : ""], ["首段可见正文", row.firstTextAt ? time(row.firstTextAt) : ""], ["结束", row.endedAt ? time(row.endedAt) : ""],
      ["本机超时", (row.timeoutMs ?? row.parameters?.timeoutMs) ? `${row.timeoutMs ?? row.parameters.timeoutMs}ms` : ""],
      ["任务编号", row.taskId], ["业务 / 执行", [row.businessJobId, row.executionId].filter(Boolean).join(" / ")], ["助手会话", row.threadId],
      ["原生会话编号", row.nativeSessionId], ["原生消息编号", row.nativeMessageId], ["提示词 / 资料版本", row.promptVersion || row.contextVersion], ["来源信息", row.provenance],
      ["执行设备", local ? row.device : ""], ["诊断记录编号", row.id]].filter(([, value]) => value !== undefined && value !== null && value !== "");
    const details = node("details"), list = node("dl"); details.appendChild(node("summary", "诊断元数据"));
    for (const [label, value] of pairs) list.append(node("dt", label), node("dd", showValue(value)));
    details.appendChild(list); parent.appendChild(details);
  }
  function processView(item, parent) {
    const row = item.detail;
    if (row.error || row.errorCode || row.reason || row.incompleteReason)
      addBlock(item, parent, "错误与说明", { errorCode: row.errorCode, error: row.error, reason: row.reason, incompleteReason: row.incompleteReason });
    if (item.kind === "response") {
      if (item.bodyError) parent.appendChild(node("p", item.bodyError, "model-traces-note"));
      else if (item.body === undefined) parent.appendChild(node("p", "正在读取服务返回…", "model-traces-note"));
      else if (item.body.text) addBlock(item, parent, "服务返回内容（已脱敏）", item.body.text);
    }
    metadata(parent, row);
  }
  function drawContent(item) {
    const parent = item.content; if (!parent) return;
    parent.replaceChildren();
    const note = text => { parent.appendChild(node("p", text, "model-traces-note")); };
    if (item.error) { note(item.error); return; }
    if (!item.detail) { note("正在读取…"); return; }
    if (item.tab === "events") { processView(item, parent); return; }
    if (item.bodyError) { note(item.bodyError); return; }
    if (item.bodyBusy || item.body === undefined) { note("正在读取正文…"); return; }
    const body = item.body;
    if (!body.text) { note(item.detail.collectionStatus === "recording" ? "尚未收到这部分内容" : "此部分未采集"); return; }
    if (body.truncated || !body.complete) note(body.truncated ? "正文已截断，记录不完整；以下仅为已保存内容。" : "记录尚未完整，以下仅为当前已采集内容。");
    if (item.kind === "request" && body.offset === 0 && !body.nextOffset) {
      try { const parsed = JSON.parse(body.text); requestMessages(item, parent, parsed.body || parsed, item.detail); }
      catch { /* Chunked or non-JSON requests remain visible as literal text. */ }
    }
    addBlock(item, parent, TITLES[item.kind], body.text, item.kind === "request" && body.offset === 0 && !body.nextOffset);
    if (!body.offset && (body.nextOffset === null || body.nextOffset === undefined)) return;
    const pages = node("div", undefined, "model-traces-pages");
    const previous = button("上一块", () => { item.offset = item.offsets.pop() ?? 0; void loadBody(item); }); previous.disabled = item.offset === 0;
    const next = button("下一块", () => { item.offsets.push(item.offset); item.offset = body.nextOffset; void loadBody(item); }); next.disabled = body.nextOffset === null || body.nextOffset === undefined;
    pages.append(previous, node("span", `${bytes(body.offset || 0)} / ${bytes(body.totalBytes)}，每次最多读取 64 KB`), next); parent.appendChild(pages);
  }
  // The toolbar outlives content redraws, so typing a search keeps focus while a live attempt updates.
  function draw(item) {
    const root = item.element; if (!root) return;
    if (item.toolbarFor !== root || item.toolbarTab !== item.tab) {
      item.toolbarFor = root; item.toolbarTab = item.tab; item.choices = [];
      const tools = node("div", undefined, "model-traces-detail-tools"), kinds = KINDS[item.tab] || [];
      if (kinds.length > 1) for (const [kind, label] of kinds) { const choice = button(label, () => selectKind(item, kind)); item.choices.push([kind, choice]); tools.appendChild(choice); }
      if (item.tab !== "events") {
        const search = node("input"); search.type = "search"; search.placeholder = "在正文中查找"; search.setAttribute("aria-label", "在正文中查找"); search.maxLength = 240; search.value = item.needle;
        search.addEventListener("input", () => { item.needle = search.value; drawContent(item); }); tools.appendChild(search);
      }
      item.content = node("div", undefined, "model-traces-detail");
      item.feedback = node("p", "", "model-traces-note model-traces-feedback"); item.feedback.setAttribute("role", "status");
      root.replaceChildren(...(tools.children.length ? [tools] : []), item.content, item.feedback);
    }
    for (const [kind, choice] of item.choices) choice.setAttribute("aria-pressed", String(kind === item.kind));
    drawContent(item);
  }
  function selectKind(item, kind) {
    if (item.kind === kind) return;
    item.kind = kind; item.offset = 0; item.offsets = []; item.body = undefined; item.bodyError = ""; item.bodySerial++;
    draw(item); if (item.detail) void loadBody(item);
  }
  async function loadDetail(item, live = false) {
    const serial = ++item.serial;
    item.detailBusy = true;
    if (!live) { item.detail = null; item.error = ""; drawContent(item); }
    const result = await request("detail", { id: item.id });
    if (serial !== item.serial || result.stale || state.viewers.get(item.id) !== item) return;
    item.detailBusy = false;
    if (!result.ok) { item.detail = null; item.error = result.statusCode === 404 || result.statusCode === 410 ? "诊断记录已过期或删除" : "诊断记录读取失败，请稍后重试"; drawContent(item); return; }
    item.error = ""; item.detail = normalizeRecord(result.record || result);
    if (!live || !item.kind) drawContent(item);
    if (item.kind) await loadBody(item, live);
  }
  async function loadBody(item, live = false) {
    if (!item.kind || !item.detail) return;
    const serial = ++item.bodySerial, kind = item.kind, offset = item.offset;
    if (!live) { item.bodyBusy = true; item.bodyError = ""; drawContent(item); }
    const result = await request("body", { id: item.id, kind, offset });
    if (serial !== item.bodySerial || result.stale || state.viewers.get(item.id) !== item || item.kind !== kind || item.offset !== offset) return;
    item.bodyBusy = false; item.body = result.ok ? result : undefined;
    item.bodyError = result.ok ? "" : result.statusCode === 404 || result.statusCode === 410 ? "记录已过期或删除" : "记录服务不可用，请稍后重试";
    drawContent(item);
  }
  // Renders the captured part of one attempt into a call-record detail. `failed` also shows the
  // service's returned content on the process tab.
  function renderTrace(container, id, tab, { failed = false } = {}) {
    if (!state.standalone || !container || typeof id !== "string" || !id || !["request", "response", "events"].includes(tab)) return;
    let item = state.viewers.get(id);
    if (!item) {
      item = { id, element: null, tab: "", kind: "", offset: 0, offsets: [], detail: null, detailBusy: false, error: "", body: undefined, bodyBusy: false, bodyError: "",
        serial: 0, bodySerial: 0, needle: "", toolbarFor: null, toolbarTab: "", choices: [], content: null, feedback: null };
      state.viewers.set(id, item);
    }
    const kind = tab === "request" ? "request" : tab === "response" ? (item.tab === tab ? item.kind : "text") : failed ? "response" : "";
    if (item.tab !== tab || item.kind !== kind) { item.tab = tab; item.kind = kind; item.offset = 0; item.offsets = []; item.body = undefined; item.bodyError = ""; item.bodyBusy = false; item.bodySerial++; }
    item.element = container;
    draw(item);
    if (!item.detail && !item.detailBusy && !item.error) void loadDetail(item);
    else if (item.detail && item.kind && item.body === undefined && !item.bodyBusy && !item.bodyError) void loadBody(item);
  }
  // Re-reads a shown attempt that is still in progress without blanking its content first.
  function refreshTrace(id) {
    const item = state.viewers.get(id);
    if (item?.element && item.detail && !item.detailBusy) void loadDetail(item, true);
  }
  // Viewers of records that are no longer expanded are discarded and their reads fenced.
  function retainTraces(ids) {
    const keep = new Set(ids);
    for (const [id, item] of state.viewers) if (!keep.has(id)) { item.serial++; item.bodySerial++; state.viewers.delete(id); }
  }
  function windowContext() {
    return { account: state.account, user: state.user, view: state.view, theme: document.body.classList.contains("theme-light") ? "light" : "dark" };
  }
  function syncWindowContext() {
    if (!state.standalone) window.desktopHost?.syncModelTracesWindow?.(windowContext());
  }
  async function open() {
    if (state.standalone) return;
    // Invoke during the original click; the host owns window reuse and focus.
    try {
      if (!window.desktopHost?.openModelTracesWindow || await window.desktopHost.openModelTracesWindow(windowContext()) === false) throw new Error("window-unavailable");
      if (!window.desktopHost?.isSettingsWindow) $('settingsModal')?.classList.remove("show");
      if (state.settingError === "模型调用窗口暂时无法打开，请重试") { state.settingError = ""; renderSettings(); }
    } catch { state.settingError = "模型调用窗口暂时无法打开，请重试"; renderSettings(); }
  }
  async function poll() {
    try { await refreshSettings(); } finally { state.timer = setTimeout(poll, 3000); }
  }
  function setScope(account, user, view) {
    account = account || ""; user = user || "";
    const changedAccount = account !== state.account;
    state.account = account; state.user = user; if (view) state.view = view;
    if (changedAccount) {
      retainTraces([]); state.readEpoch++; state.session = null; state.scopeEpoch++; state.scopePromise = null;
      void ensureScope(true).then(() => { void refreshSettings(); });
    }
    syncWindowContext();
  }
  function init(options = {}) {
    if (state.initialized || (!options.standalone && !$('btnDeveloperMode'))) return;
    state.initialized = true; state.standalone = options.standalone === true;
    state.onSettings = typeof options.onSettings === "function" ? options.onSettings : null;
    if (!state.standalone && window.MutationObserver) new window.MutationObserver(syncWindowContext).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    setScope(options.account, options.user, options.view);
    window.desktopHost?.onModelTracesInvalidated?.(value => {
      state.session = null; state.scopeEpoch++; state.scopePromise = null;
      if (Number.isSafeInteger(value.deleteVersion)) state.deleteVersion = value.deleteVersion;
      invalidate();
    });
    $('btnModelTraces')?.addEventListener("click", () => open()); $('btnViewModelTraces')?.addEventListener("click", () => open());
    $('btnDeveloperMode')?.addEventListener("click", () => configure({ enabled: !state.setting?.enabled }));
    $('modelTraceRetention')?.addEventListener("change", event => configure({ retentionDays: Number(event.target.value) }));
    $('btnRetryModelTraces')?.addEventListener("click", () => state.savedOperation ? configure(state.savedOperation) : refreshSettings());
    $('btnClearModelTraces')?.addEventListener("click", () => { $('modelTraceClearConfirm').hidden = false; $('btnCancelClearModelTraces').focus(); });
    $('btnCancelClearModelTraces')?.addEventListener("click", () => { $('modelTraceClearConfirm').hidden = true; $('btnClearModelTraces').focus(); });
    $('btnConfirmClearModelTraces')?.addEventListener("click", async () => {
      $('btnConfirmClearModelTraces').disabled = true;
      invalidate();
      const result = await request("clear", { confirmAllAccounts: true });
      if (result.ok) { $('modelTraceClearConfirm').hidden = true; state.settingError = ""; await refreshSettings(); }
      else { state.settingError = "清空尚未完成，请重试；不会显示为清理成功。"; renderSettings(); }
      $('btnConfirmClearModelTraces').disabled = false;
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
      if ($('modelTraceClearConfirm') && !$('modelTraceClearConfirm').hidden) { $('modelTraceClearConfirm').hidden = true; event.preventDefault(); event.stopImmediatePropagation(); }
    }, true);
    window.addEventListener("kyf-service-restored", () => { state.session = null; invalidate(); void ensureScope(true).then(refreshSettings); });
    void refreshSettings(); state.timer = setTimeout(poll, 3000);
  }
  window.ModelTraces = { init, setScope, open, refreshSettings, statusOf, renderTrace, refreshTrace, retainTraces, setting: () => state.setting };
})();
