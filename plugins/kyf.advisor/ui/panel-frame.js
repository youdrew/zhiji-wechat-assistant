"use strict";
// Modified by Know Your Friends, 2026-10-10: persistent session controls and scoped history.
// Modified by Know Your Friends, 2026-10-11: thinking toggle.
// Modified by Know Your Friends, 2026-10-14: @ picks a friend into the discussion; joined friends show above the input.
// Modified by Know Your Friends, 2026-10-16: editable per-discussion pending queue, separate from the composer.
// Modified by Know Your Friends, 2026-10-18: sending and compaction no longer wait on the catalog's model reading;
// the service checks the model when a question or compaction starts.
// Moved from chatui/advisor-frame.js to plugins/kyf.advisor/ui/panel-frame.js on 2026-10-13 (kyf.advisor plugin):
// the view and its controller (panel-controller.js) share the sandboxed panel page and exchange scoped
// messages directly instead of through the main window.

(function () {
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value, limit) => typeof value === "string" && value.length <= limit;
  const terminal = new Set(["done", "error", "stopped", "cancelled", "interrupted"]);
  const byId = id => document.getElementById(id);
  const agentName = byId("agentName"), agentMenu = byId("agentMenu"), menuButton = byId("btnAgentMenu");
  const timeline = byId("advisorTimeline"), welcome = byId("advisorWelcome"), welcomeText = byId("welcomeText");
  const input = byId("advisorInput"), send = byId("btnAdvisorSend"), stop = byId("btnStopRun"), retry = byId("btnRetryRun");
  const controls = byId("timelineControls");
  let initialized = false, scope = null, templates = [], catalogReady = false;
  let threadId = null, renderedScope = "", currentRun = null, submitting = false, stopping = false, retryKind = "load", problem = "";
  let lastSavedMessage = null;
  let templateSignature = "";
  let currentThread = {}, currentContact = null, currentHistory = [], contextTarget = null, dialogAction = null, composing = false, commandIndex = 0, historySignature = "";
  const commands = [{ command: "/compact", description: "压缩当前讨论，保留聊天记录" },
    { command: "/new", description: "开始新讨论，原任务继续运行" }, { command: "/sessions", description: "查看当前联系人的历史会话" }];
  const messageNodes = new Map();
  const contextExpanded = new Map();
  let contextControlSerial = 0;
  // Picked identities travel with the saved draft, so restoring text never resolves an ambiguous display name.
  const mentionDrafts = new Map();
  const queueEditors = new Map(), queueExpanded = new Map();
  let currentQueue = null, queuePending = false, queueError = "", queueRetryEnqueue = false;
  let friends = [], friendsKey = "", friendsRequested = "", friendsError = "", mentionIndex = 0;
  // Editing the latest question (added 2026-10-15): {runId, previousDraft}; sending replaces that turn.
  let editing = null;
  const turnEdits = new Map();
  const queuedNow = () => !!currentQueue?.items?.length;
  function transientMessage(message) {
    if (message.role !== "status") return false;
    return message.status === "reasoning" || message.event?.type === "reasoning" ||
      ["busy", "preparing", "running", "answering", "generating", "thinking", "reading", "searching", "compacting", "compressing", "retry"].includes(message.event?.state) ||
      /^(正在生成|正在回答|正在思考|发送中|正在停止|已停止生成|已停止)$/.test(message.text.trim());
  }
  function validScope(value) {
    return object(value) && text(value.account, 240) && text(value.user, 240) && text(value.agentId, 240) &&
      (value.threadId === undefined || text(value.threadId, 240)) &&
      Number.isSafeInteger(value.generation) && value.generation >= 0;
  }
  function sameScope(value) {
    return scope && value.account === scope.account && value.user === scope.user &&
      value.agentId === scope.agentId && value.threadId === scope.threadId && value.generation === scope.generation;
  }
  let thinking = true;
  function renderThinking() {
    const button = byId("btnThinking"); if (!button) return;
    button.setAttribute("aria-pressed", String(thinking));
    button.title = thinking ? "先思考再回答，较慢；点击关闭" : "不思考直接回答，较快；点击开启";
  }
  function sendToParent(type, payload = {}) {
    if (initialized && scope) window.AdvisorController?.receive({ type, payload, scope });
  }
  function paintText(container, content, live) {
    if (typeof window.AdvisorTimeline?.renderInto === "function") {
      window.AdvisorTimeline.renderInto(container, content, live);
    } else container.textContent = content;
  }
  function disposeNode(node) {
    window.AdvisorTimeline?.disposeInto?.(node.bubble);
    for (const historical of node.historyNodes || []) window.AdvisorTimeline?.disposeInto?.(historical.bubble);
    node.wrap.remove();
  }
  function makeMessage(message) {
    const wrap = document.createElement("div"), bubble = document.createElement("div");
    wrap.className = `advisor-msg ${message.role}`; wrap.dataset.msgId = message.id;
    bubble.className = "advisor-bubble"; wrap.appendChild(bubble);
    return { wrap, bubble };
  }
  function validContextSnapshot(value) {
    return object(value) && value.version === 1 && ["pending", "recorded"].includes(value.state) &&
      Number.isSafeInteger(value.capturedAtMs) && value.capturedAtMs >= 0 && Array.isArray(value.contacts) && value.contacts.length > 0 &&
      new Set(value.contacts.map(item => item?.user)).size === value.contacts.length && value.contacts.every((item, index) =>
        object(item) && text(item.user, 256) && item.user && text(item.name, 240) && item.name.trim() &&
        !/[\x00-\x1f\x7f]/.test(item.user + item.name) && item.primary === (index === 0) &&
        ["pending", "ready", "empty", "unavailable"].includes(item.status) &&
        item.access === (index === 0 ? "required" : "available") && Array.isArray(item.sources) && item.sources.length <= 2 &&
        item.sources.every(source => ["chat", "profile"].includes(source)) && new Set(item.sources).size === item.sources.length);
  }
  function contextSources(contact) {
    if (contact.status === "pending") return "资料准备中，尚未确认可用范围";
    if (contact.status === "unavailable") return "资料不可用，本轮未提供";
    if (contact.status === "empty" || !contact.sources.length) return "无可用资料";
    return (contact.access === "required" ? "已提供 · " : "可按需读取 · ") +
      contact.sources.map(source => source === "chat" ? "聊天记录" : "好友资料").join("、");
  }
  function renderMessageContext(node, message) {
    if (message.role !== "user") return;
    const snapshot = validContextSnapshot(message.contextSnapshot) ? message.contextSnapshot : null;
    const unconfirmed = !snapshot && (message.delivery === "pending" || message.delivery === "failed" || message._contextPending === true);
    const signature = JSON.stringify([snapshot, unconfirmed]);
    if (node.contextSignature === signature) return;
    node.contextSignature = signature;
    const restoreFocus = !!node.context && node.context.children[0] === document.activeElement;
    node.context?.remove();
    const context = document.createElement("div"); context.className = "advisor-message-context";
    node.context = context;
    // Keep this label below its question and above the optional edit action.
    node.wrap.insertBefore(context, node.actions || null);
    if (!snapshot) {
      context.textContent = unconfirmed ? "上下文待确认" : "上下文未记录";
      return;
    }
    const contacts = snapshot.contacts, key = JSON.stringify([scope.account, scope.user, scope.agentId, scope.threadId, message.id]);
    const toggle = document.createElement("button"), label = document.createElement("span");
    toggle.type = "button"; toggle.className = "advisor-context-toggle";
    label.className = "advisor-context-label"; label.textContent = snapshot.state === "pending" ? "上下文准备中：" : "本轮上下文：";
    toggle.appendChild(label);
    const nameCounts = new Map();
    for (const item of contacts) nameCounts.set(item.name, (nameCounts.get(item.name) || 0) + 1);
    contacts.slice(0, 2).forEach(item => {
      const name = document.createElement("span"); name.className = "advisor-context-name";
      name.textContent = "@" + item.name; name.title = `${item.name}（${item.user}）\n${contextSources(item)}`;
      toggle.appendChild(name);
    });
    if (contacts.length > 2) {
      const count = document.createElement("span"); count.className = "advisor-context-count"; count.textContent = "+" + (contacts.length - 2);
      toggle.appendChild(count);
    }
    const chevron = document.createElement("span"); chevron.className = "advisor-context-chevron"; chevron.textContent = "⌄"; chevron.setAttribute("aria-hidden", "true");
    toggle.appendChild(chevron);
    const details = document.createElement("div"); details.className = "advisor-context-details";
    details.id = "advisorContextDetails" + ++contextControlSerial;
    details.setAttribute("role", "region"); details.setAttribute("aria-label", "本轮好友上下文详情");
    toggle.setAttribute("aria-controls", details.id);
    toggle.title = "查看本轮提供或可按需读取的好友资料";
    for (const item of contacts) {
      const row = document.createElement("div"), name = document.createElement("span"), sources = document.createElement("small");
      row.className = "advisor-context-contact"; row.dataset.user = item.user; row.title = `${item.name}（${item.user}）`;
      name.className = "advisor-context-contact-name"; name.textContent = "@" + item.name;
      if (item.primary) { const primary = document.createElement("small"); primary.className = "advisor-context-primary"; primary.textContent = "当前联系人"; name.appendChild(primary); }
      row.appendChild(name);
      if (nameCounts.get(item.name) > 1) { const identity = document.createElement("small"); identity.className = "advisor-context-identity"; identity.textContent = item.user; row.appendChild(identity); }
      sources.className = "advisor-context-sources"; sources.textContent = contextSources(item); row.appendChild(sources); details.appendChild(row);
    }
    const note = document.createElement("p"); note.className = "advisor-context-note";
    note.textContent = "仅记录本轮提供或可按需读取的范围，不代表模型已读取全部资料。"; details.appendChild(note);
    const setExpanded = expanded => {
      contextExpanded.set(key, expanded); details.hidden = !expanded;
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.setAttribute("aria-label", `${expanded ? "收起" : "展开"}本轮上下文，共 ${contacts.length} 位联系人`);
    };
    setExpanded(contextExpanded.get(key) === true);
    toggle.addEventListener("click", () => setExpanded(details.hidden));
    toggle.addEventListener("keydown", event => {
      if (event.key === "Escape" && !details.hidden) { event.preventDefault(); setExpanded(false); toggle.focus(); }
    });
    context.appendChild(toggle); context.appendChild(details);
    if (restoreFocus) toggle.focus();
  }
  function updateMessage(message, live = false) {
    let node = messageNodes.get(message.id);
    if (!node) { node = makeMessage(message); messageNodes.set(message.id, node); }
    node.wrap.dataset.delivery = message.delivery || "";
    if (message.role === "user") node.wrap.title = message.delivery === "failed" ? "发送失败，可重试" : message.delivery === "pending" ? "正在提交" : "";
    const stateKey = message.role === "status" ? JSON.stringify(message.event || { state: message.status }) : "";
    if (node.bubble.dataset.rawText !== message.text || node.bubble.dataset.live !== String(live) || node.bubble.dataset.stateKey !== stateKey) {
      node.bubble.dataset.rawText = message.text; node.bubble.dataset.live = String(live);
      node.bubble.dataset.stateKey = stateKey;
      if (message.role === "assistant") paintText(node.bubble, message.text, live);
      else if (message.role === "status" && typeof window.AdvisorTimeline?.statusInto === "function") {
        window.AdvisorTimeline.statusInto(node.bubble, message.event || { text: message.text, state: message.status });
      } else node.bubble.textContent = message.text;
    }
    renderMessageContext(node, message);
    return node.wrap;
  }
  function statusRow(id, content, kind = "status", event) {
    if (!content) {
      const node = messageNodes.get(id); if (node) disposeNode(node); messageNodes.delete(id); return;
    }
    const row = updateMessage({ id, role: "status", text: content, status: kind, event }); row.dataset.status = kind;
    if (id === "ui:run") { row.classList.add("advisor-current-status"); row.dataset.runId = currentRun?.id || ""; }
    if (timeline.children[timeline.children.length - 1] !== row) timeline.appendChild(row);
    updateLayout();
  }
  function updateLayout() {
    const hasMessages = messageNodes.size > 0;
    document.body.classList.toggle("advisor-empty", !hasMessages);
    welcome.hidden = hasMessages; timeline.hidden = !hasMessages;
  }
  function renderTemplates() {
    const signature = JSON.stringify([scope?.agentId, catalogReady, templates.map(template => [template.id, template.name, template.welcome])]);
    if (signature === templateSignature) return;
    templateSignature = signature;
    const selected = templates.find(template => template.id === scope?.agentId);
    agentName.textContent = selected?.name || (catalogReady ? "未启用模板" : "模板未读取");
    welcomeText.textContent = selected?.welcome?.trim() || "开始新讨论";
    menuButton.disabled = templates.length === 0 || (templates.length === 1 && !!selected);
    agentMenu.replaceChildren();
    for (const template of templates) {
      const button = document.createElement("button"); button.type = "button";
      button.className = "advisor-agent-choice" + (template.id === scope?.agentId ? " active" : "");
      button.setAttribute("role", "menuitemradio"); button.setAttribute("aria-checked", String(template.id === scope?.agentId));
      button.textContent = template.name; button.title = template.name;
      button.addEventListener("click", () => {
        agentMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false");
        if (template.id !== scope?.agentId) sendToParent("ADVISOR_SWITCH_TEMPLATE", { agentId: template.id });
      });
      agentMenu.appendChild(button);
    }
    agentMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false");
    updateControls();
  }
  function renderThread(thread, run, draft, scrollTop = null, canReturn = false, older = false, draftMentions) {
    const nextScope = JSON.stringify([scope.account, scope.user, scope.agentId, scope.threadId, scope.generation]);
    const changed = threadId !== thread.id || renderedScope !== nextScope;
    const oldHeight = timeline.scrollHeight, oldTop = timeline.scrollTop;
    if (changed) {
      for (const node of messageNodes.values()) disposeNode(node);
      timeline.replaceChildren(); messageNodes.clear();
      threadId = thread.id; renderedScope = nextScope; problem = ""; setHistoryVisible(false);
      currentQueue = null; queueError = ""; queuePending = false; queueRetryEnqueue = false;
      if (byId("usageDetails")) byId("usageDetails").hidden = true;
      if (byId("moreMenu")) byId("moreMenu").hidden = true;
    }
    currentThread = thread; currentRun = run || null; submitting = false; stopping = false;
    if (!older && Array.isArray(draftMentions)) mentionDrafts.set(draftKey(), draftMentions.map(item => ({ user: item.user, name: item.name })));
    if (validQueue(thread.queue)) currentQueue = thread.queue;
    renderParticipants();
    if (byId("sessionTitle")) { byId("sessionTitle").textContent = thread.title || "开始新讨论"; byId("sessionTitle").title = thread.title || "开始新讨论"; }
    if (thread.readonly && thread.agentName) agentName.textContent = thread.agentName;
    if (byId("readonlyNotice")) byId("readonlyNotice").hidden = !thread.readonly && !thread.deleteState;
    if (byId("readonlyText")) byId("readonlyText").textContent = thread.deleteState ? "删除未完成，可在历史列表重试" : thread.readonlyReason || "原助手已停用或删除，仅可查看历史";
    if (byId("btnReturnHistory")) byId("btnReturnHistory").hidden = !canReturn;
    const messages = thread.messages.filter(message => !transientMessage(message));
    lastSavedMessage = messages.filter(message => !message.supersededBy).at(-1) || null;
    // Messages a regenerated or edited turn replaced fold into one collapsed entry where they were.
    const items = [];
    for (const message of messages) {
      if (!message.supersededBy) { items.push(message); continue; }
      const last = items.at(-1);
      if (last?.group) last.messages.push(message);
      else items.push({ group: true, id: "superseded:" + message.id, messages: [message] });
    }
    const compactions = (thread.compactions || []).filter(item => ["completed", "done", "failed", "interrupted", "stopped"].includes(item.state));
    const activeIds = new Set([...items.map(item => item.id), ...compactions.map(item => "compact:" + item.id)]);
    for (const [id, node] of messageNodes) {
      if (!id.startsWith("ui:") && !activeIds.has(id)) { disposeNode(node); messageNodes.delete(id); }
    }
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
    const placed = new Map();
    for (let index = 0; index < items.length; index++) {
      const item = items[index];
      const node = item.group ? supersededNode(item) :
        updateMessage(item, item.role === "assistant" && item.runId === run?.id && !terminal.has(run?.state));
      for (const message of item.group ? item.messages : [item]) placed.set(message.id, node);
      if (timeline.children[index] !== node) timeline.insertBefore(node, timeline.children[index] || null);
    }
    if (changed) editing = turnEdits.get(draftKey()) || null;
    renderEditBar();
    for (const item of compactions) {
      const nodeId = "compact:" + item.id;
      let node = messageNodes.get(nodeId);
      if (!node) {
        const wrap = document.createElement("details"), title = document.createElement("summary"), bubble = document.createElement("div");
        wrap.className = "advisor-compaction"; bubble.className = "advisor-bubble";
        wrap.appendChild(title); wrap.appendChild(bubble); node = { wrap, bubble, title }; messageNodes.set(nodeId, node);
      }
      const completed = ["completed", "done"].includes(item.state);
      node.title.textContent = `${completed ? "上下文已压缩" : { failed: "压缩失败", interrupted: "压缩已中断", stopped: "压缩已停止" }[item.state]} · ${item.automatic === true || item.kind === "automatic" ? "自动" : "手动"} · ${formatTime(item.completedAtMs || item.createdAtMs)}`;
      const summary = completed ? item.summary || "已压缩，摘要暂不可查看" : item.error || "原讨论记录已保留。";
      if (node.bubble.dataset.rawText !== summary) { node.bubble.dataset.rawText = summary; paintText(node.bubble, summary, false); }
      const following = messages.find(message => message.createdAtMs > (item.completedAtMs || item.createdAtMs || 0));
      timeline.insertBefore(node.wrap, following ? placed.get(following.id) || null : null);
    }
    let earlierButton = byId("btnLoadEarlier");
    if (!earlierButton && thread.hasMore) {
      earlierButton = document.createElement("button"); earlierButton.id = "btnLoadEarlier";
      earlierButton.type = "button"; earlierButton.className = "advisor-text-btn advisor-load-earlier"; earlierButton.textContent = "加载更早的讨论";
      earlierButton.addEventListener("click", () => sendToParent("ADVISOR_LOAD_EARLIER"));
    }
    if (earlierButton) { earlierButton.hidden = !thread.hasMore; timeline.insertBefore(earlierButton, timeline.children[0] || null); }
    if (text(draft, 16000) && input.value !== draft) input.value = draft;
    if (editing?.completed) finishEdit();
    renderQueue(); updateControls(); updateLayout(); renderUsage();
    if (older) timeline.scrollTop = oldTop + timeline.scrollHeight - oldHeight;
    else if (changed && Number.isFinite(scrollTop)) timeline.scrollTop = scrollTop;
    else if (nearBottom || changed) timeline.scrollTop = timeline.scrollHeight;
  }
  function formatTime(value) {
    return Number.isFinite(value) && value > 0 ? new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  }
  function operationForView() {
    if (currentThread.activeOperation) return currentThread.activeOperation;
    const latest = [...(currentThread.compactions || [])].sort((a, b) => (b.createdAtMs || 0) - (a.createdAtMs || 0))[0];
    const lastQuestion = currentThread.messages?.filter(item => item.role === "user").at(-1);
    return latest && (latest.createdAtMs || 0) >= (lastQuestion?.createdAtMs || 0) ? latest : null;
  }
  function tokens(value) { return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k` : String(value); }
  function compactReason() {
    if (currentThread.readonly || currentThread.deleteState) return "只读会话不可压缩";
    if (currentThread.compactPending) return "正在提交压缩";
    if (currentRun && !terminal.has(currentRun.state) || submitting) return "回答完成后可压缩";
    if (queuedNow()) return "排队的问题回答完成后可压缩";
    if (["running", "unknown"].includes(currentThread.activeOperation?.state)) return currentThread.activeOperation.state === "unknown" ? "正在核对压缩状态" : "正在压缩上下文";
    if (!threadId || !currentThread.messages?.some(message => message.role === "assistant")) return "当前无需压缩";
    return "";
  }
  function renderUsage() {
    const button = byId("usageButton"); if (!button) return;
    const usage = currentThread.usage;
    const recent = (currentThread.compactions || []).filter(item => item.state === "completed" || item.state === "done")
      .sort((a, b) => (b.completedAtMs || b.createdAtMs || 0) - (a.completedAtMs || a.createdAtMs || 0))[0];
    const valid = object(usage) && Number.isFinite(usage.inputTokens) && usage.inputTokens > 0 &&
      Number.isFinite(usage.contextTokens) && usage.contextTokens > 0;
    // Only the backend can verify whether a compaction belongs to this model/native/context generation.
    const compacted = currentThread.compactedSinceUsage === true;
    button.hidden = !valid && !compacted;
    button.textContent = compacted ? "已压缩" : valid ? `${usage.estimated ? "约 " : ""}${tokens(usage.inputTokens)} / ${tokens(usage.contextTokens)}` : "";
    button.title = "上下文用量";
    byId("usageLast").textContent = valid ? `上次输入：${usage.estimated ? "约 " : ""}${usage.inputTokens.toLocaleString()} · ${formatTime(usage.recordedAtMs)}` : "上次输入：暂无新读数";
    const contextTokens = usage?.contextTokens || currentThread.contextTokens;
    byId("usageWindow").textContent = Number.isFinite(contextTokens) && contextTokens > 0 ? `窗口：${contextTokens.toLocaleString()}` : "窗口：暂无读数";
    byId("usageCompaction").hidden = !recent;
    if (recent) byId("usageCompaction").textContent = `最近压缩：${formatTime(recent.completedAtMs || recent.createdAtMs)} · ${recent.automatic === true || recent.kind === "automatic" ? "自动" : "手动"}`;
  }
  function updateControls() {
    const running = !!currentRun && !terminal.has(currentRun.state), operation = operationForView();
    const compacting = ["running", "unknown"].includes(operation?.state);
    const readonly = currentThread.readonly || !!currentThread.deleteState;
    const command = composerCommand(), localCommand = (!editing || editing.completed) && ["/new", "/reset", "/sessions", "/compact"].includes(command);
    input.disabled = !!readonly;
    // The model is checked by the backend when a question starts; a stale catalog reading never blocks sending.
    send.disabled = readonly || submitting || (!localCommand && !catalogReady) ||
      !scope?.account || !scope?.user || (!scope?.agentId && command !== "/sessions");
    stop.hidden = !running && !compacting; send.hidden = false;
    stop.disabled = stopping; stop.setAttribute("aria-label", stopping ? "正在停止" : compacting ? "停止压缩" : "停止生成");
    stop.title = stopping ? "正在停止" : compacting ? "停止压缩并暂停队列" : "停止生成并暂停队列";
    const enqueue = !localCommand && (running || compacting || currentThread.compactPending || currentQueue?.paused || currentQueue?.items?.length);
    send.title = enqueue ? "加入队列，按顺序处理" : "发送";
    send.setAttribute("aria-label", enqueue ? "加入队列" : "发送");
    if (byId("sendLabel")) byId("sendLabel").textContent = enqueue ? "加入队列" : "发送";
    const compactFailure = ["failed", "interrupted", "stopped", "noop"].includes(operation?.state);
    retry.hidden = !problem && !["error", "interrupted", "stopped"].includes(currentRun?.state) && !["failed", "interrupted"].includes(operation?.state);
    controls.hidden = retry.hidden;
    const reason = compactReason();
    for (const buttonId of ["btnCompact", "btnUsageCompact"]) {
      const button = byId(buttonId); if (button) { button.disabled = !!reason; button.title = reason || "压缩当前讨论，原记录保留"; button.textContent = compacting ? "正在压缩上下文" : "压缩上下文"; }
    }
    if (byId("compactHint")) byId("compactHint").textContent = reason;
    if (byId("btnNewThread")) byId("btnNewThread").disabled = !scope?.account || !scope?.user || !scope?.agentId || readonly;
    if (byId("btnHistory")) byId("btnHistory").disabled = !scope?.account || !scope?.user;
    const phase = currentRun?.phaseEvent || { state: currentRun?.phase || "preparing" };
    const labels = { preparing: "正在准备", starting: "正在准备", awaiting: "正在等待", answering: "正在回答",
      running: "正在回答", thinking: "正在思考", busy: "正在回答", retry: "正在重试", searching: "正在搜索", compressing: "正在压缩上下文", compacting: "正在压缩上下文", rebuilding: "正在重建上下文" };
    const operationText = { running: "正在压缩上下文", unknown: "正在核对压缩状态", failed: "压缩失败，可重试", interrupted: "压缩已中断，可重试", stopped: "压缩已停止", noop: "当前无需压缩" };
    // A question sent into the queue keeps the running answer's status in view.
    const announce = submitting && !running;
    const event = problem ? { state: "error", text: problem } : announce ? { state: "preparing", text: "正在提交" } :
      stopping ? { state: "stopping", text: "正在停止" } : currentThread.compactPending ? { state: "preparing", text: "正在提交压缩" } :
        compacting || compactFailure ? { state: operation.state, text: operation.error || operationText[operation.state] } : phase;
    const savedError = !problem && !submitting && !stopping && currentRun?.state === "error" &&
      lastSavedMessage?.role === "status" && lastSavedMessage.status === "error" && lastSavedMessage.text === "生成失败：" + currentRun.error;
    const content = announce || stopping || problem || compacting || compactFailure || currentThread.compactPending ? event.text : running ? phase.text || labels[phase.state] || "正在准备" :
      currentRun?.state === "stopped" ? "已停止" : ["error", "interrupted"].includes(currentRun?.state) && !savedError ? currentRun.error || "生成已中断，可重试" : "";
    statusRow("ui:run", content, event.state, event);
    renderTurnActions();
  }
  // A collapsed entry for the messages a regenerated or edited turn replaced; nothing said is dropped from view.
  function supersededNode(group) {
    let node = messageNodes.get(group.id);
    if (!node) {
      const wrap = document.createElement("details"), title = document.createElement("summary"), bubble = document.createElement("div");
      wrap.className = "advisor-superseded"; bubble.className = "advisor-bubble";
      wrap.appendChild(title); wrap.appendChild(bubble); node = { wrap, bubble, title }; messageNodes.set(group.id, node);
    }
    node.title.textContent = group.messages.some(message => message.role === "user") ? "修改前的提问" : "上一版回答";
    const content = group.messages.filter(message => message.role !== "status")
      .map(message => message.role === "user" ? `问：${message.text}` : message.text).join("\n\n");
    const signature = JSON.stringify(group.messages.map(message => [message.id, message.role, message.text, message.contextSnapshot, message.delivery]));
    if (node.groupSignature !== signature) {
      node.groupSignature = signature; node.bubble.dataset.rawText = content;
      for (const historical of node.historyNodes || []) window.AdvisorTimeline?.disposeInto?.(historical.bubble);
      node.bubble.replaceChildren(); node.historyNodes = [];
      for (const message of group.messages.filter(item => item.role !== "status")) {
        const wrap = document.createElement("div"), bubble = document.createElement("div");
        wrap.className = "advisor-superseded-message"; bubble.className = "advisor-superseded-message-content";
        wrap.dataset.msgId = message.id; wrap.appendChild(bubble); node.bubble.appendChild(wrap);
        const historical = { wrap, bubble }; node.historyNodes.push(historical);
        if (message.role === "user") bubble.textContent = "问：" + message.text;
        else paintText(bubble, message.text, false);
        renderMessageContext(historical, message);
      }
    }
    return node.wrap;
  }
  // The latest finished turn while nothing runs or waits: it can be regenerated (when answered) or asked again.
  function latestTurn() {
    if (!currentRun || !["done", "error", "stopped"].includes(currentRun.state) || submitting || stopping ||
      currentThread.readonly || currentThread.deleteState || queuedNow() || currentThread.compactPending ||
      ["running", "unknown"].includes(currentThread.activeOperation?.state)) return null;
    const current = (currentThread.messages || []).filter(message => !message.supersededBy && Number.isSafeInteger(message.seq));
    const question = current.filter(message => message.role === "user").at(-1);
    if (!question) return null;
    const answer = currentRun.state === "done" ?
      current.filter(message => message.role === "assistant" && message.runId === currentRun.id && message.seq > question.seq).at(-1) : null;
    return { runId: currentRun.id, question, answer: answer || null };
  }
  // Shown on hover under the latest question (编辑) and the latest answer (重新生成).
  function renderTurnActions() {
    const turn = editing ? null : latestTurn(), wanted = new Map();
    if (turn) { wanted.set(turn.question.id, "edit"); if (turn.answer) wanted.set(turn.answer.id, "regenerate"); }
    for (const [id, node] of messageNodes) {
      const kind = wanted.get(id);
      if (node.actions && node.actions.dataset.kind !== kind) { node.actions.remove(); node.actions = null; }
      if (!kind || node.actions) continue;
      const row = document.createElement("div"), button = document.createElement("button");
      row.className = "advisor-msg-actions"; row.dataset.kind = kind;
      button.type = "button"; button.className = "advisor-text-btn";
      button.textContent = kind === "edit" ? "编辑" : "重新生成";
      button.title = kind === "edit" ? "修改这个问题后重新提问" : "重新生成这个回答";
      button.addEventListener("click", () => kind === "edit" ? startEdit() : sendToParent("ADVISOR_REGENERATE"));
      row.appendChild(button); node.wrap.appendChild(row); node.actions = row;
    }
  }
  function startEdit() {
    const turn = latestTurn();
    if (!turn || editing) return;
    editing = { runId: turn.runId, previousDraft: input.value, previousMentions: selectedDraftMentions() };
    turnEdits.set(draftKey(), editing);
    input.value = turn.question.text;
    // Older messages do not carry per-turn selections. Joined contacts already supply their context;
    // matching their display names against the question could silently select several same-named people.
    const explicit = Array.isArray(turn.question.mentions) ? turn.question.mentions : [];
    const contacts = validContextSnapshot(turn.question.contextSnapshot) ? turn.question.contextSnapshot.contacts : [];
    const participants = new Map([...(currentThread.participants || []), ...contacts].map(item => [item.user, item]));
    mentionDrafts.set(draftKey(), explicit.flatMap(value => {
      const item = typeof value === "string" ? participants.get(value) : value;
      return object(item) && text(item.user, 240) && item.user && text(item.name, 240) && item.name &&
        input.value.includes("@" + item.name) ? [{ user: item.user, name: item.name }] : [];
    }));
    renderEditBar();
    saveDraft();
    input.focus(); input.setSelectionRange?.(input.value.length, input.value.length);
    updateControls();
  }
  function renderEditBar() {
    if (byId("editBar")) byId("editBar").hidden = !editing;
    if (byId("editLabel")) byId("editLabel").textContent = editing?.overflow ? "修改已提交 · 两份草稿分别保留" : "修改上一个问题";
    if (byId("btnCancelEdit")) byId("btnCancelEdit").textContent = editing?.overflow ? "切换草稿" : "取消";
    if (byId("btnCopyHeldDraft")) byId("btnCopyHeldDraft").hidden = !editing?.overflow;
  }
  function cancelEdit(restore = true) {
    if (!editing) return;
    if (restore && editing.overflow) {
      const held = editing.previousDraft; editing.previousDraft = input.value; input.value = held;
      const heldMentions = editing.previousMentions || []; editing.previousMentions = mentionDrafts.get(draftKey()) || [];
      mentionDrafts.set(draftKey(), heldMentions); saveDraft();
      updateControls(); return;
    }
    const previous = editing.previousDraft, previousMentions = editing.previousMentions || [];
    turnEdits.delete(draftKey()); editing = null;
    renderEditBar();
    if (restore) { input.value = previous; mentionDrafts.set(draftKey(), previousMentions); saveDraft(); }
    updateControls();
  }
  function finishEdit() {
    if (!editing) return;
    const previous = editing.previousDraft, next = input.value;
    const mentions = [...(editing.previousMentions || []), ...selectedDraftMentions()];
    const merged = previous && next && previous !== next ? `${previous}\n\n${next}` : next || previous;
    if (merged.length > 16000) {
      editing.completed = true; editing.overflow = true; renderEditBar(); return;
    }
    cancelEdit(false);
    // A user may already be composing the next question while acceptance is in flight.
    // Keep both that input and the draft temporarily put aside by the edit.
    input.value = merged;
    mentionDrafts.set(draftKey(), [...new Map(mentions.map(item => [item.user, item])).values()]); saveDraft();
    updateControls();
  }
  function setHistoryVisible(visible) {
    if (byId("historyPanel")) byId("historyPanel").hidden = !visible;
    document.body.classList.toggle("advisor-show-history", visible);
    byId("btnHistory")?.setAttribute("aria-expanded", String(visible));
    if (!visible && byId("historyContext")) byId("historyContext").hidden = true;
  }
  function validQueue(value) {
    return object(value) && typeof value.paused === "boolean" && Number.isSafeInteger(value.version) && value.version >= 0 &&
      text(value.reason || "", 2000) && Array.isArray(value.items) && value.items.every(item => object(item) && text(item.id, 240) && item.id &&
        text(item.message, 16000) && Number.isSafeInteger(item.version) && ["pending", "starting"].includes(item.state));
  }
  function renderQueue() {
    const panel = byId("queuePanel"); if (!panel) return;
    const previousScroll = panel.scrollTop;
    const key = draftKey(), items = currentQueue?.items || [], readonly = currentThread.readonly || !!currentThread.deleteState;
    const editor = queueEditors.get(key), expanded = queueExpanded.get(key) === true;
    panel.hidden = !items.length && !currentQueue?.paused && !queueError && !editor;
    byId("queueCount").textContent = `待处理 ${items.length}`;
    byId("btnQueueToggle").setAttribute("aria-expanded", String(expanded));
    const reasons = { stopped: "已停止，队列暂停", failed: "上一条失败，队列暂停", error: "上一条失败，队列暂停",
      restart: "重启后已暂停", restarted: "重启后已暂停", unknown: "状态待核对", manual: "已暂停", interrupted: "已中断，队列暂停",
      "account-changed": "账号已切换，队列暂停", unavailable: "服务不可用，请检查后继续", "dispatch-failed": "开始处理失败，队列暂停",
      "model-unavailable": "模型服务不可用，请检查配置后继续", "invalid-mentions": "@联系人已不可用，请检查待处理消息",
      "settings-unavailable": "插件设置不可用，请检查设置后继续", "agent-unavailable": "助手已停用或删除" };
    const status = currentQueue?.paused ? reasons[currentQueue.reason] || "已暂停" : items.length ? "按顺序处理" : "";
    byId("queueStatus").textContent = status; byId("queueStatus").title = status;
    const pause = byId("btnQueuePause"); pause.hidden = readonly || !currentQueue;
    pause.disabled = queuePending; pause.textContent = currentQueue?.paused ? "继续" : "暂停";
    pause.title = currentQueue?.paused ? "继续按顺序处理待处理消息" : "暂停后续消息，当前回答继续";
    byId("queueError").textContent = queueError;
    byId("btnQueueRetry").hidden = !queueError || readonly;
    byId("btnQueueRetry").disabled = queuePending || submitting;
    byId("btnQueueRetry").textContent = queueRetryEnqueue ? "重试确认" : "刷新队列";
    const list = byId("queueList"); list.hidden = !expanded;
    const signature = JSON.stringify([key, items, readonly, queuePending]);
    if (list.dataset.signature !== signature) {
      list.dataset.signature = signature; list.replaceChildren();
      for (const item of items) {
      const row = document.createElement("div"), message = document.createElement("p"), actions = document.createElement("div"), state = document.createElement("small");
      row.className = "advisor-queue-item"; row.dataset.itemId = item.id; message.textContent = item.message;
      actions.className = "advisor-queue-item-actions"; state.textContent = item.state === "starting" ? "正在开始" : "等待处理";
      actions.appendChild(state);
      if (!readonly) for (const [action, label] of [["edit", "编辑"], ["remove", "移除"]]) {
        const button = document.createElement("button"); button.type = "button"; button.className = `advisor-text-btn queue-${action}`;
        button.textContent = label; button.disabled = queuePending || item.state !== "pending";
        button.addEventListener("click", () => {
          if (item.state !== "pending" || queuePending) return;
          if (action === "edit") {
            queueEditors.set(key, { id: item.id, version: item.version, text: item.message, error: "" });
            renderQueue();
            // Keep the editor and Save visible inside the bounded queue area on short windows.
            panel.scrollTop = panel.scrollHeight;
            byId("queueEditInput").focus({ preventScroll: true });
          } else sendToParent("ADVISOR_QUEUE_REMOVE", { itemId: item.id, version: item.version });
        }); actions.appendChild(button);
      }
      row.appendChild(message); row.appendChild(actions); list.appendChild(row);
      }
    }
    byId("queueEditor").hidden = !editor || readonly;
    if (editor && !readonly) {
      const current = items.find(item => item.id === editor.id), editable = current?.state === "pending";
      if (byId("queueEditInput").value !== editor.text) byId("queueEditInput").value = editor.text;
      byId("queueEditInput").disabled = queuePending || !editable;
      byId("btnQueueSave").disabled = queuePending || !editable || !editor.text.trim();
      byId("btnQueueCancel").disabled = queuePending;
      byId("queueEditStatus").textContent = !editable ? "这条消息已开始处理或已移除，无法再修改。" : editor.error ||
        (current.version !== editor.version ? "这条消息已在另一窗口更新；保存时会核对版本。" : "仅修改正文，保留提交时的 @好友和思考模式，不影响输入框草稿。");
    }
    panel.scrollTop = previousScroll;
  }
  function historyBusy(row) {
    return ["running", "queued", "starting", "compacting", "unknown"].includes(row.state);
  }
  function renderHistory(payload) {
    if (!byId("historyPanel")) return;
    updateControls(); setHistoryVisible(payload.open);
    if (!payload.open) return;
    const signature = JSON.stringify(payload);
    if (signature === historySignature) return;
    historySignature = signature;
    currentHistory = payload.threads;
    byId("historyState").textContent = payload.loading ? "正在读取历史…" : payload.error || (!payload.threads.length ? "还没有历史会话" : "");
    byId("btnRetryHistory").hidden = !payload.error;
    const regular = byId("historyList"), other = byId("otherHistoryList"); regular.replaceChildren(); other.replaceChildren();
    const otherGroups = new Map();
    for (const row of payload.threads) {
      const button = document.createElement("button"), title = document.createElement("strong"), preview = document.createElement("p"), detail = document.createElement("small");
      button.type = "button"; button.className = "advisor-history-row"; button.setAttribute("aria-current", String(row.id === threadId));
      button.title = row.title || "新讨论"; title.textContent = row.title || "新讨论"; preview.textContent = row.preview || "暂无消息";
      const state = row.deleteState ? row.deleteState === "failed" ? "删除未完成，可重试" : "正在删除" :
        { running: "正在回答", queued: "正在准备", compacting: "正在压缩", unknown: "状态核对中", error: "生成失败", interrupted: "已中断" }[row.state] || "";
      detail.textContent = [formatTime(row.updatedAtMs), state, row.readonly ? row.agentName || row.agentId : ""].filter(Boolean).join(" · ");
      button.appendChild(title); button.appendChild(preview); button.appendChild(detail);
      button.addEventListener("click", () => { if (!row.deleteState) sendToParent("ADVISOR_SELECT_THREAD", { threadId: row.id, agentId: row.agentId }); });
      const context = event => {
        event.preventDefault(); contextTarget = row;
        const menu = byId("historyContext"); menu.hidden = false;
        menu.style.left = `${Math.max(8, Math.min(event.clientX || 20, window.innerWidth - 220))}px`;
        menu.style.top = `${Math.max(8, Math.min(event.clientY || 80, (window.innerHeight || 600) - 125))}px`;
        byId("btnRenameThread").disabled = !!row.deleteState;
        byId("btnDeleteThread").disabled = historyBusy(row) && !row.deleteState;
        byId("btnDeleteThread").textContent = row.deleteState ? "重试删除" : "删除";
        byId("deleteHint").textContent = row.deleteState ? "删除未完成时不能继续讨论" : row.state === "unknown" ? "状态核对后可删除" : historyBusy(row) ? "停止后可删除" : "";
      };
      button.addEventListener("contextmenu", context);
      button.addEventListener("keydown", event => { if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) context(event); });
      if (row.readonly) {
        let group = otherGroups.get(row.agentId);
        if (!group) {
          group = document.createElement("section");
          const heading = document.createElement("h3"); heading.className = "advisor-history-group";
          heading.textContent = row.agentName || row.agentId; group.appendChild(heading); other.appendChild(group); otherGroups.set(row.agentId, group);
        }
        group.appendChild(button);
      } else regular.appendChild(button);
    }
    byId("otherHistory").hidden = !payload.threads.some(row => row.readonly);
  }
  function showThreadDialog(kind) {
    const row = contextTarget; if (!row || (kind === "delete" && historyBusy(row) && !row.deleteState)) return;
    byId("historyContext").hidden = true;
    dialogAction = { kind, row, scope: { ...scope } };
    byId("sessionDialogTitle").textContent = kind === "rename" ? "修改会话标题" : "删除这段讨论？";
    byId("sessionDialogText").textContent = kind === "rename" ? "" : "此操作会删除本段助手讨论、草稿及尚未处理的排队消息，无法撤销。微信聊天记录与关系记忆仍会保留。";
    const title = byId("sessionTitleInput"); title.hidden = kind !== "rename"; title.value = row.title || "";
    byId("btnConfirmDialog").textContent = kind === "rename" ? "保存" : row.deleteState ? "重试删除" : "删除";
    byId("sessionDialog").showModal(); if (kind === "rename") { title.focus(); title.select(); }
  }
  function composerCommand() {
    const value = input.value.trim(), prefix = currentContact ? "@" + currentContact.name : "";
    return prefix && value.startsWith(prefix) && /^\s/.test(value.slice(prefix.length)) ? value.slice(prefix.length).trim() : value;
  }
  function commandMatches() {
    const value = composerCommand();
    if (composing || !/^\/\w*$/.test(value)) return [];
    return commands.filter(item => item.command.startsWith(value));
  }
  function renderCommands() {
    const menu = byId("commandMenu"); if (!menu) return;
    const matches = commandMatches(); menu.hidden = !matches.length; menu.replaceChildren();
    commandIndex = Math.min(commandIndex, Math.max(0, matches.length - 1));
    matches.forEach((item, index) => {
      const button = document.createElement("button"), description = document.createElement("small");
      button.type = "button"; button.className = "advisor-menu-action"; button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(index === commandIndex)); button.textContent = item.command;
      description.textContent = item.description; button.appendChild(description);
      button.addEventListener("click", () => chooseCommand(item.command)); menu.appendChild(button);
    });
  }
  function draftKey(value = scope) { return JSON.stringify([value?.account, value?.user, value?.agentId, value?.threadId]); }
  function selectedDraftMentions() {
    return (mentionDrafts.get(draftKey()) || []).filter(item => input.value.includes("@" + item.name));
  }
  function saveDraft(scrollTop = timeline.scrollTop) {
    const mentions = selectedDraftMentions(); mentionDrafts.set(draftKey(), mentions);
    const localCommand = (!editing || editing.completed) && ["/new", "/reset", "/sessions", "/compact"].includes(composerCommand());
    sendToParent("ADVISOR_DRAFT_UPDATE", { draft: localCommand ? "" : input.value, agentId: scope?.agentId, scrollTop, mentions: localCommand ? [] : mentions });
  }
  function friendScope() { return JSON.stringify([scope?.account, scope?.user]); }
  // "@" (or a Chinese IME's "＠") right before the caret starts a pick, unless it is part of an address such as name@example.com.
  function mentionQuery() {
    if (composing || input.selectionStart !== input.selectionEnd) return null;
    const before = input.value.slice(0, input.selectionStart);
    const match = /(^|[^A-Za-z0-9._%+-])[@＠]([^\s@＠]{0,40})$/.exec(before);
    return match ? { query: match[2], start: before.length - match[2].length - 1 } : null;
  }
  function mentionMatches() {
    const found = mentionQuery();
    if (!found || friendsKey !== friendScope()) return [];
    const needle = found.query.toLowerCase();
    return friends.filter(item => item.name.toLowerCase().includes(needle) || item.user.toLowerCase().includes(needle)).slice(0, 50);
  }
  function renderMentions() {
    const menu = byId("mentionMenu"); if (!menu) return;
    const found = mentionQuery();
    if (!found || currentThread.readonly || currentThread.deleteState || !scope?.account || !scope?.user) { menu.hidden = true; return; }
    if (friendsKey !== friendScope() && friendsRequested !== friendScope()) { friendsRequested = friendScope(); sendToParent("ADVISOR_FRIENDS_REQUEST"); }
    const matches = mentionMatches();
    mentionIndex = Math.min(mentionIndex, Math.max(0, matches.length - 1));
    menu.replaceChildren(); menu.hidden = false;
    if (!matches.length) {
      const hint = document.createElement("div"); hint.className = "advisor-menu-hint";
      hint.textContent = friendsKey !== friendScope() ? "正在读取好友…" : friendsError || (friends.length ? "没有匹配的好友" : "还没有可以 @ 的好友");
      menu.appendChild(hint); return;
    }
    const names = new Map(); for (const item of matches) names.set(item.name, (names.get(item.name) || 0) + 1);
    matches.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button"; button.className = "advisor-menu-action"; button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(index === mentionIndex)); button.textContent = item.name;
      if (names.get(item.name) > 1) { const detail = document.createElement("small"); detail.textContent = item.user; button.appendChild(detail); }
      // Keep the caret in the input: a click must not blur it before the pick reads the caret.
      button.addEventListener("mousedown", event => event.preventDefault());
      button.addEventListener("click", () => chooseMention(item)); menu.appendChild(button);
    });
  }
  function chooseMention(item) {
    const found = mentionQuery(); if (!found) return;
    const insert = "@" + item.name + " ", end = input.selectionStart;
    input.value = input.value.slice(0, found.start) + insert + input.value.slice(end);
    input.setSelectionRange(found.start + insert.length, found.start + insert.length);
    const key = draftKey();
    mentionDrafts.set(key, [...(mentionDrafts.get(key) || []).filter(entry => entry.user !== item.user), { user: item.user, name: item.name }]);
    byId("mentionMenu").hidden = true; input.focus();
    saveDraft();
    updateControls();
  }
  function renderParticipants() {
    const bar = byId("participantBar"); if (!bar) return;
    const joined = Array.isArray(currentThread.participants) ? currentThread.participants : [];
    const list = currentContact ? [currentContact, ...joined.filter(item => item.user !== currentContact.user)] : joined;
    const editable = !currentThread.readonly && !currentThread.deleteState;
    const signature = JSON.stringify([list, editable, currentContact?.user]);
    if (bar.dataset.signature === signature) return;
    bar.dataset.signature = signature; bar.replaceChildren(); bar.hidden = !list.length;
    for (const item of list) {
      const chip = document.createElement("span"), label = document.createElement("span");
      const primary = item.user === currentContact?.user;
      chip.className = "advisor-participant" + (primary ? " advisor-primary-contact" : "");
      chip.title = primary ? `当前联系人：${item.name}，已自动带入上下文` : `${item.name}（${item.user}）`;
      label.textContent = "@" + item.name; chip.appendChild(label);
      if (editable && !primary) {
        const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×";
        remove.title = "移出讨论"; remove.setAttribute("aria-label", `把 ${item.name} 移出讨论`);
        remove.addEventListener("click", () => sendToParent("ADVISOR_REMOVE_PARTICIPANT", { user: item.user }));
        chip.appendChild(remove);
      }
      bar.appendChild(chip);
    }
  }
  function chooseCommand(command) {
    input.value = command; byId("commandMenu").hidden = true;
    updateControls(); submitMessage();
  }
  function applyStream(payload) {
    if (payload.threadId !== threadId) return;
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
    currentRun = payload.run;
    for (const message of payload.messages) {
      if (transientMessage(message)) continue;
      lastSavedMessage = message;
      const existed = messageNodes.has(message.id);
      const node = updateMessage(message, message.role === "assistant" && message.runId === currentRun?.id && !terminal.has(currentRun?.state));
      if (!existed) {
        const status = messageNodes.get("ui:run")?.wrap;
        timeline.insertBefore(node, status?.parentNode === timeline ? status : null);
      }
    }
    updateControls(); updateLayout();
    if (nearBottom) timeline.scrollTop = timeline.scrollHeight;
  }
  function validMessage(message) {
    return object(message) && text(message.id, 240) && ["user", "assistant", "status"].includes(message.role) && text(message.text, 2000000) &&
      (message._contextPending === undefined || typeof message._contextPending === "boolean") &&
      (message.delivery === undefined || ["pending", "failed"].includes(message.delivery)) &&
      (message.runId === undefined || text(message.runId, 240)) &&
      (message.supersededBy === undefined || text(message.supersededBy, 240)) &&
      (message.event === undefined || (object(message.event) &&
        Object.keys(message.event).every(key => ["seq", "type", "text", "state", "messageId", "skillId", "next", "attempt"].includes(key)) &&
        (message.event.state === undefined || text(message.event.state, 60)) &&
        (message.event.text === undefined || text(message.event.text, 2000000)) &&
        (message.event.next === undefined || (Number.isSafeInteger(message.event.next) && message.event.next >= 0)) &&
        (message.event.attempt === undefined || (Number.isSafeInteger(message.event.attempt) && message.event.attempt >= 0))));
  }
  function validRun(run) {
    return object(run) && text(run.id, 240) && text(run.state, 60) &&
      (run.phase === undefined || text(run.phase, 60)) &&
      (run.phaseEvent === undefined || (object(run.phaseEvent) &&
        (run.phaseEvent.state === undefined || text(run.phaseEvent.state, 60)) &&
        (run.phaseEvent.text === undefined || text(run.phaseEvent.text, 20000)) &&
        (run.phaseEvent.next === undefined || (Number.isSafeInteger(run.phaseEvent.next) && run.phaseEvent.next >= 0)) &&
        (run.phaseEvent.attempt === undefined || (Number.isSafeInteger(run.phaseEvent.attempt) && run.phaseEvent.attempt >= 0))));
  }
  function validPayload(type, payload) {
    if (!object(payload)) return false;
    const permitted = {
      ADVISOR_INIT: ["activeAgentId", "enabledTemplates", "thread", "draft", "draftMentions", "currentContact", "catalogReady", "theme", "run", "scrollTop", "canReturn", "thinking"],
      ADVISOR_THEME: ["theme"], ADVISOR_SET_TEMPLATES: ["enabledTemplates", "activeAgentId", "catalogReady"],
      ADVISOR_SET_THREAD: ["thread", "run", "agentId", "draft", "draftMentions", "currentContact", "scrollTop", "canReturn"],
      ADVISOR_STREAM_UPDATE: ["threadId", "run", "messages"],
      ADVISOR_PROBLEM: ["error", "retryKind"], ADVISOR_SUBMIT_STATE: ["pending"], ADVISOR_STOP_STATE: ["pending"],
      ADVISOR_COPY_RESULT: ["copied"], ADVISOR_HISTORY: ["threads", "open", "loading", "error"], ADVISOR_EARLIER: ["thread"], ADVISOR_COMMAND_FAILED: ["command"],
      ADVISOR_EDIT_RESULT: ["runId", "ok", "error"],
      ADVISOR_FRIENDS: ["friends", "error"], ADVISOR_QUEUE: ["queue", "error", "pending", "retryEnqueue"],
      ADVISOR_QUEUE_RESULT: ["action", "itemId", "ok", "error"], ADVISOR_ENQUEUED: ["message"]
    };
    if (!permitted[type] || Object.keys(payload).some(key => !permitted[type].includes(key))) return false;
    if (type === "ADVISOR_INIT" || type === "ADVISOR_SET_TEMPLATES") {
      if (!Array.isArray(payload.enabledTemplates) || payload.enabledTemplates.length > 200 ||
        !payload.enabledTemplates.every(item => object(item) && text(item.id, 240) && text(item.name, 60) &&
          (item.welcome === undefined || text(item.welcome, 120))) ||
        !text(payload.activeAgentId, 240) || typeof payload.catalogReady !== "boolean") return false;
    }
    if (type === "ADVISOR_INIT" || type === "ADVISOR_SET_THREAD" || type === "ADVISOR_EARLIER") {
      if (!object(payload.thread) || !text(payload.thread.id, 240) || !Array.isArray(payload.thread.messages) ||
        !payload.thread.messages.every(validMessage) || (type !== "ADVISOR_EARLIER" && !text(payload.draft, 16000))) return false;
      if (payload.run && !validRun(payload.run)) return false;
      if (payload.currentContact != null && (!object(payload.currentContact) || !text(payload.currentContact.user, 240) ||
        !payload.currentContact.user || !text(payload.currentContact.name, 240) || !payload.currentContact.name)) return false;
      if (payload.draftMentions !== undefined && (!Array.isArray(payload.draftMentions) || payload.draftMentions.length > 8000 ||
        !payload.draftMentions.every(item => object(item) && text(item.user, 240) && item.user && text(item.name, 240) && item.name))) return false;
      if (payload.thread.compactions !== undefined && (!Array.isArray(payload.thread.compactions) || !payload.thread.compactions.every(item =>
        object(item) && text(item.id, 240) && text(item.state, 60) && (item.summary == null || text(item.summary, 2000000))))) return false;
      if (payload.thread.participants !== undefined && (!Array.isArray(payload.thread.participants) ||
        !payload.thread.participants.every(item => object(item) && text(item.user, 240) && text(item.name, 240)))) return false;
      if (payload.thread.queue !== undefined && !validQueue(payload.thread.queue)) return false;
      if (payload.scrollTop != null && (!Number.isFinite(payload.scrollTop) || payload.scrollTop < 0)) return false;
      if (payload.canReturn !== undefined && typeof payload.canReturn !== "boolean") return false;
    }
    if (type === "ADVISOR_HISTORY" && (!Array.isArray(payload.threads) || typeof payload.open !== "boolean" ||
      typeof payload.loading !== "boolean" || !text(payload.error, 20000) || !payload.threads.every(item => object(item) &&
        text(item.id, 240) && text(item.agentId, 240) && text(item.title || "", 240) && text(item.preview || "", 2000)))) return false;
    if (type === "ADVISOR_STREAM_UPDATE" && (!text(payload.threadId, 240) || !Array.isArray(payload.messages) ||
      payload.messages.length > 200 || !payload.messages.every(validMessage) || !validRun(payload.run))) return false;
    if (["ADVISOR_INIT", "ADVISOR_THEME"].includes(type) && !["light", "dark"].includes(payload.theme)) return false;
    if (type === "ADVISOR_INIT" && payload.thinking !== undefined && typeof payload.thinking !== "boolean") return false;
    if (type === "ADVISOR_PROBLEM" && (!text(payload.error, 20000) || !["send", "poll", "load", "catalog", "draft", "new", "history", "earlier", "compact", "operation"].includes(payload.retryKind))) return false;
    if (["ADVISOR_SUBMIT_STATE", "ADVISOR_STOP_STATE"].includes(type) && typeof payload.pending !== "boolean") return false;
    if (type === "ADVISOR_COPY_RESULT" && typeof payload.copied !== "boolean") return false;
    if (type === "ADVISOR_COMMAND_FAILED" && !["/new", "/reset"].includes(payload.command)) return false;
    if (type === "ADVISOR_FRIENDS" && (!Array.isArray(payload.friends) || !text(payload.error, 20000) ||
      !payload.friends.every(item => object(item) && text(item.user, 240) && item.user && text(item.name, 240)))) return false;
    if (type === "ADVISOR_QUEUE" && ((payload.queue !== null && !validQueue(payload.queue)) || !text(payload.error, 20000) ||
      typeof payload.pending !== "boolean" || typeof payload.retryEnqueue !== "boolean")) return false;
    if (type === "ADVISOR_QUEUE_RESULT" && (!["edit", "remove", "pause", "resume"].includes(payload.action) ||
      !text(payload.itemId, 240) || typeof payload.ok !== "boolean" || !text(payload.error, 20000))) return false;
    if (type === "ADVISOR_EDIT_RESULT" && (!text(payload.runId, 240) || typeof payload.ok !== "boolean" || !text(payload.error, 20000))) return false;
    if (type === "ADVISOR_ENQUEUED" && !text(payload.message, 16000)) return false;
    return true;
  }
  menuButton.addEventListener("click", () => {
    agentMenu.hidden = !agentMenu.hidden; menuButton.setAttribute("aria-expanded", String(!agentMenu.hidden));
  });
  document.addEventListener("click", event => {
    if (!event.target.closest?.(".advisor-agent-control")) { agentMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false"); }
    if (!event.target.closest?.(".advisor-frame-top") && byId("moreMenu")) { byId("moreMenu").hidden = true; byId("btnMore").setAttribute("aria-expanded", "false"); }
    if (!event.target.closest?.(".advisor-usage-wrap") && byId("usageDetails")) { byId("usageDetails").hidden = true; byId("usageButton").setAttribute("aria-expanded", "false"); }
    if (!event.target.closest?.(".advisor-history-context") && byId("historyContext")) byId("historyContext").hidden = true;
    if (!event.target.closest?.(".advisor-input-area") && byId("mentionMenu")) byId("mentionMenu").hidden = true;
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && editing) { cancelEdit(true); return; }
    if (event.key === "Escape") {
      agentMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false");
      for (const id of ["moreMenu", "usageDetails", "commandMenu", "mentionMenu", "historyContext"]) if (byId(id)) byId(id).hidden = true;
      setHistoryVisible(false);
      sendToParent("ADVISOR_HISTORY_CLOSE");
    }
  });
  stop.addEventListener("click", () => sendToParent(["running", "unknown"].includes(currentThread.activeOperation?.state) ? "ADVISOR_STOP_OPERATION" : "ADVISOR_STOP"));
  byId("btnQueueToggle")?.addEventListener("click", () => { queueExpanded.set(draftKey(), !queueExpanded.get(draftKey())); renderQueue(); });
  byId("btnQueuePause")?.addEventListener("click", () => sendToParent(currentQueue?.paused ? "ADVISOR_QUEUE_RESUME" : "ADVISOR_QUEUE_PAUSE"));
  byId("btnQueueRetry")?.addEventListener("click", () => sendToParent("ADVISOR_QUEUE_RETRY"));
  byId("queueEditInput")?.addEventListener("input", () => {
    const editor = queueEditors.get(draftKey()); if (editor) { editor.text = byId("queueEditInput").value; renderQueue(); }
  });
  byId("btnQueueCancel")?.addEventListener("click", () => { queueEditors.delete(draftKey()); renderQueue(); });
  byId("btnQueueSave")?.addEventListener("click", () => {
    const editor = queueEditors.get(draftKey());
    if (editor && !byId("btnQueueSave").disabled) sendToParent("ADVISOR_QUEUE_EDIT", { itemId: editor.id, version: editor.version, message: editor.text });
  });
  retry.addEventListener("click", () => sendToParent("ADVISOR_RETRY", { retryKind: problem ? retryKind :
    ["failed", "interrupted"].includes(operationForView()?.state) ? "compact" : ["error", "interrupted", "stopped"].includes(currentRun?.state) ? "send" : retryKind }));
  byId("btnHistory")?.addEventListener("click", () => sendToParent("ADVISOR_HISTORY_OPEN"));
  byId("btnRetryHistory")?.addEventListener("click", () => sendToParent("ADVISOR_HISTORY_OPEN"));
  byId("btnCloseHistory")?.addEventListener("click", () => { setHistoryVisible(false); sendToParent("ADVISOR_HISTORY_CLOSE"); });
  byId("btnNewThread")?.addEventListener("click", () => sendToParent("ADVISOR_NEW_THREAD"));
  byId("btnThinking")?.addEventListener("click", () => {
    if (!initialized || !scope) return;
    thinking = !thinking; renderThinking(); sendToParent("ADVISOR_SET_THINKING", { enabled: thinking });
  });
  byId("btnReturnHistory")?.addEventListener("click", () => sendToParent("ADVISOR_RETURN_HISTORY"));
  byId("btnMore")?.addEventListener("click", () => {
    byId("moreMenu").hidden = !byId("moreMenu").hidden; byId("btnMore").setAttribute("aria-expanded", String(!byId("moreMenu").hidden));
  });
  byId("usageButton")?.addEventListener("click", () => {
    byId("usageDetails").hidden = !byId("usageDetails").hidden; byId("usageButton").setAttribute("aria-expanded", String(!byId("usageDetails").hidden));
  });
  for (const id of ["btnCompact", "btnUsageCompact"]) byId(id)?.addEventListener("click", () => {
    byId("moreMenu").hidden = true; byId("usageDetails").hidden = true; sendToParent("ADVISOR_COMPACT");
  });
  byId("btnRenameThread")?.addEventListener("click", () => showThreadDialog("rename"));
  byId("btnDeleteThread")?.addEventListener("click", () => showThreadDialog("delete"));
  byId("btnCancelDialog")?.addEventListener("click", () => byId("sessionDialog").close());
  // A sandboxed page cannot submit forms, so the dialog confirms by its button and by Enter in the title.
  function confirmDialog(event) {
    event?.preventDefault?.();
    if (!dialogAction || !sameScope(dialogAction.scope)) { byId("sessionDialog").close(); return; }
    const { kind, row } = dialogAction, title = byId("sessionTitleInput").value.trim();
    if (kind === "rename" && !title) { byId("sessionTitleInput").focus(); return; }
    sendToParent(kind === "rename" ? "ADVISOR_RENAME_THREAD" : "ADVISOR_DELETE_THREAD", { threadId: row.id, agentId: row.agentId, ...(kind === "rename" ? { title } : {}) });
    byId("sessionDialog").close(); dialogAction = null;
  }
  byId("sessionDialogForm")?.addEventListener("submit", confirmDialog);
  byId("btnConfirmDialog")?.addEventListener("click", confirmDialog);
  byId("sessionTitleInput")?.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229) confirmDialog(event);
  });
  function submitMessage() {
    const value = input.value;
    if (!value.trim() || value.length > 16000 || send.disabled || composing) return;
    const command = composerCommand();
    const localCommand = (!editing || editing.completed) && ["/new", "/reset", "/sessions", "/compact"].includes(command);
    if (localCommand) {
      if (byId("commandMenu")) byId("commandMenu").hidden = true;
      input.value = ""; saveDraft();
    }
    // A changed turn never silently turns an edit into a new queued question.
    if (editing && !editing.completed && latestTurn()?.runId !== editing.runId) {
      problem = "当前轮次已变化，请先取消编辑；修改后的文字仍在输入框中";
      updateControls(); return;
    }
    const edit = editing && !editing.completed ? editing.runId : null;
    if (!localCommand) { submitting = true; }
    updateControls();
    // Only picks whose "@名字" is still in the text join; typing "@名字" by hand stays plain text.
    const mentions = localCommand ? [] : selectedDraftMentions().map(item => item.user);
    sendToParent("ADVISOR_SEND", { text: localCommand ? command : value, agentId: scope.agentId, ...(mentions.length ? { mentions } : {}), ...(edit ? { editRunId: edit } : {}) });
  }
  byId("btnCancelEdit")?.addEventListener("click", () => { cancelEdit(true); input.focus(); });
  byId("btnCopyHeldDraft")?.addEventListener("click", () => { if (editing?.overflow) sendToParent("ADVISOR_COPY", { text: editing.previousDraft }); });
  send.addEventListener("click", submitMessage);
  input.addEventListener("keydown", event => {
    if (event.isComposing || composing || event.keyCode === 229) return;
    const picks = byId("mentionMenu")?.hidden === false ? mentionMatches() : [];
    if (picks.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault(); mentionIndex = (mentionIndex + (event.key === "ArrowDown" ? 1 : picks.length - 1)) % picks.length; renderMentions(); return;
    }
    if (picks.length && (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey))) {
      event.preventDefault(); chooseMention(picks[mentionIndex]); return;
    }
    const matches = commandMatches();
    if (matches.length && !byId("commandMenu")?.hidden && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault(); commandIndex = (commandIndex + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length; renderCommands(); return;
    }
    if (matches.length && !byId("commandMenu")?.hidden && (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey))) {
      event.preventDefault(); chooseCommand(matches[commandIndex].command); return;
    }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitMessage(); }
  });
  input.addEventListener("compositionstart", () => {
    composing = true; for (const id of ["commandMenu", "mentionMenu"]) if (byId(id)) byId(id).hidden = true;
  });
  input.addEventListener("compositionend", () => { composing = false; renderCommands(); renderMentions(); });
  input.addEventListener("click", renderMentions);
  input.addEventListener("keyup", event => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) renderMentions(); });
  input.addEventListener("input", () => {
    saveDraft();
    // A failed friend list is asked again on the next keystroke, never in a loop.
    if (friendsError) friendsKey = friendsRequested = friendsError = "";
    commandIndex = 0; mentionIndex = 0; renderCommands(); renderMentions(); updateControls();
  });
  let scrollTimer;
  timeline.addEventListener("scroll", () => {
    clearTimeout(scrollTimer); const originalScope = scope;
    scrollTimer = setTimeout(() => {
      if (originalScope === scope && !currentThread.readonly) saveDraft(Math.max(0, timeline.scrollTop));
    }, 200);
  });
  // Scoped messages from the controller; anything malformed or for an older scope is ignored.
  function receive(data) {
    if (!object(data) || Object.keys(data).some(key => !["type", "payload", "scope"].includes(key)) ||
      !validScope(data.scope) || !validPayload(data.type, data.payload)) return;
    if (data.type === "ADVISOR_INIT") {
      initialized = true; scope = data.scope; templates = data.payload.enabledTemplates;
      currentContact = data.payload.currentContact || null;
      catalogReady = data.payload.catalogReady;
      if (typeof data.payload.thinking === "boolean") thinking = data.payload.thinking;
      renderThinking();
      document.body.className = `advisor-frame-body theme-${data.payload.theme}`;
      renderTemplates(); renderThread(data.payload.thread, data.payload.run, data.payload.draft, data.payload.scrollTop, data.payload.canReturn, false, data.payload.draftMentions);
      sendToParent("ADVISOR_FRAME_READY"); return;
    }
    if (!initialized) return;
    if (data.type === "ADVISOR_EDIT_RESULT" && !sameScope(data.scope)) {
      const edit = turnEdits.get(draftKey(data.scope));
      if (edit?.runId === data.payload.runId && data.payload.ok) edit.completed = true;
      return;
    }
    if (["ADVISOR_SET_TEMPLATES", "ADVISOR_SET_THREAD"].includes(data.type)) {
      if (data.scope.generation < scope.generation || (data.scope.generation === scope.generation && !sameScope(data.scope))) return;
      scope = data.scope;
    } else if (!sameScope(data.scope)) return;
    const payload = data.payload;
    if (data.type === "ADVISOR_SET_TEMPLATES") {
      templates = payload.enabledTemplates; catalogReady = payload.catalogReady; renderTemplates();
    } else if (data.type === "ADVISOR_SET_THREAD") {
      currentContact = payload.currentContact || null;
      renderTemplates(); renderThread(payload.thread, payload.run, payload.draft, payload.scrollTop, payload.canReturn, false, payload.draftMentions);
    } else if (data.type === "ADVISOR_STREAM_UPDATE") applyStream(payload);
    else if (data.type === "ADVISOR_FRIENDS") {
      friends = payload.friends; friendsError = payload.error; friendsKey = friendScope();
      mentionIndex = 0; renderMentions();
    }
    else if (data.type === "ADVISOR_EDIT_RESULT") {
      if (editing?.runId === payload.runId) {
        if (payload.ok) finishEdit();
        else { submitting = false; problem = payload.error; updateControls(); }
      }
    }
    else if (data.type === "ADVISOR_HISTORY") renderHistory(payload);
    else if (data.type === "ADVISOR_QUEUE") {
      if (!currentQueue || payload.queue === null || payload.queue.version >= currentQueue.version) currentQueue = payload.queue;
      queueError = payload.error; queuePending = payload.pending; queueRetryEnqueue = payload.retryEnqueue;
      renderQueue(); updateControls();
    } else if (data.type === "ADVISOR_QUEUE_RESULT") {
      const key = draftKey(), editor = queueEditors.get(key);
      if (editor && editor.id === payload.itemId) {
        if (payload.ok) queueEditors.delete(key);
        else {
          editor.error = payload.error;
          const latest = currentQueue?.items?.find(item => item.id === editor.id);
          if (latest) editor.version = latest.version;
        }
      }
      renderQueue();
    } else if (data.type === "ADVISOR_ENQUEUED") {
      if (!input.value) mentionDrafts.delete(draftKey());
      renderMentions();
    }
    else if (data.type === "ADVISOR_COMMAND_FAILED") { if (!input.value) input.value = payload.command; submitting = false; updateControls(); }
    else if (data.type === "ADVISOR_EARLIER" && payload.thread.id === threadId) renderThread(payload.thread, currentRun, input.value, null, !byId("btnReturnHistory")?.hidden, true);
    else if (data.type === "ADVISOR_THEME") {
      document.body.classList.toggle("theme-light", payload.theme === "light");
      document.body.classList.toggle("theme-dark", payload.theme === "dark");
    }
    else if (data.type === "ADVISOR_PROBLEM") {
      problem = payload.error; retryKind = payload.retryKind; updateControls();
    } else if (data.type === "ADVISOR_SUBMIT_STATE") {
      submitting = payload.pending; if (submitting) { problem = ""; statusRow("ui:problem", ""); } updateControls();
    } else if (data.type === "ADVISOR_STOP_STATE") { stopping = payload.pending; updateControls(); }
  }
  window.AdvisorView = Object.freeze({ receive });
  renderThinking(); updateControls(); updateLayout();
})();
