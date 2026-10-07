"use strict";

(function () {
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value, limit) => typeof value === "string" && value.length <= limit;
  const terminal = new Set(["done", "error", "stopped", "cancelled"]);
  const byId = id => document.getElementById(id);
  const agentName = byId("agentName"), agentMenu = byId("agentMenu"), menuButton = byId("btnAgentMenu");
  const timeline = byId("advisorTimeline"), welcome = byId("advisorWelcome"), welcomeText = byId("welcomeText");
  const input = byId("advisorInput"), send = byId("btnAdvisorSend"), stop = byId("btnStopRun"), retry = byId("btnRetryRun");
  const controls = byId("timelineControls");
  let sessionNonce = null, scope = null, templates = [], catalogReady = false, engineState = "unknown";
  let threadId = null, renderedScope = "", currentRun = null, submitting = false, stopping = false, retryKind = "load", problem = "";
  let lastSavedMessage = null;
  let templateSignature = "";
  const messageNodes = new Map();
  function transientMessage(message) {
    if (message.role !== "status") return false;
    return message.status === "reasoning" || message.event?.type === "reasoning" ||
      ["busy", "preparing", "running", "answering", "generating", "thinking", "reading", "compacting", "compressing", "retry"].includes(message.event?.state) ||
      /^(正在生成|正在回答|正在思考|发送中|正在停止|已停止生成|已停止)$/.test(message.text.trim());
  }
  function validScope(value) {
    return object(value) && text(value.account, 240) && text(value.user, 240) && text(value.agentId, 240) &&
      Number.isSafeInteger(value.generation) && value.generation >= 0;
  }
  function sameScope(value) {
    return scope && value.account === scope.account && value.user === scope.user &&
      value.agentId === scope.agentId && value.generation === scope.generation;
  }
  function sendToParent(type, payload = {}) {
    if (sessionNonce && scope) window.parent.postMessage({ envelope: "advisor", nonce: sessionNonce, type, payload, scope }, "*");
  }
  function paintText(container, content, live) {
    if (typeof window.AdvisorTimeline?.renderInto === "function") {
      window.AdvisorTimeline.renderInto(container, content, live);
    } else container.textContent = content;
  }
  function disposeNode(node) {
    window.AdvisorTimeline?.disposeInto?.(node.bubble);
    node.wrap.remove();
  }
  function makeMessage(message) {
    const wrap = document.createElement("div"), bubble = document.createElement("div");
    wrap.className = `advisor-msg ${message.role}`; wrap.dataset.msgId = message.id;
    bubble.className = "advisor-bubble"; wrap.appendChild(bubble);
    return { wrap, bubble };
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
    const signature = JSON.stringify([scope?.agentId, catalogReady, engineState, templates.map(template => [template.id, template.name, template.welcome])]);
    if (signature === templateSignature) return;
    templateSignature = signature;
    const selected = templates.find(template => template.id === scope?.agentId);
    agentName.textContent = selected?.name || (catalogReady ? "未启用模板" : "模板未读取");
    welcomeText.textContent = selected?.welcome?.trim() || `嗨，我是你的${selected?.name || "狗头军师"}`;
    menuButton.disabled = templates.length < 2;
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
  function renderThread(thread, run, draft) {
    const nextScope = JSON.stringify([scope.account, scope.user, scope.agentId, scope.generation]);
    const changed = threadId !== thread.id || renderedScope !== nextScope;
    if (changed) {
      for (const node of messageNodes.values()) disposeNode(node);
      timeline.replaceChildren(); messageNodes.clear();
      threadId = thread.id; renderedScope = nextScope; problem = "";
    }
    currentRun = run || null; submitting = false; stopping = false;
    const messages = thread.messages.filter(message => !transientMessage(message));
    lastSavedMessage = messages.at(-1) || null;
    const activeIds = new Set(messages.map(message => message.id));
    for (const [id, node] of messageNodes) {
      if (!id.startsWith("ui:") && !activeIds.has(id)) { disposeNode(node); messageNodes.delete(id); }
    }
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
    for (let index = 0; index < messages.length; index++) {
      const message = messages[index];
      const node = updateMessage(message, message.role === "assistant" && message.runId === run?.id && !terminal.has(run?.state));
      if (timeline.children[index] !== node) timeline.insertBefore(node, timeline.children[index] || null);
    }
    if (text(draft, 16000) && input.value !== draft) input.value = draft;
    updateControls(); updateLayout();
    if (nearBottom || changed) timeline.scrollTop = timeline.scrollHeight;
  }
  function updateControls() {
    const running = !!currentRun && !terminal.has(currentRun.state);
    const resetCommand = /^\s*\/reset\s*$/.test(input.value);
    send.disabled = submitting || (!resetCommand && (running || !catalogReady || engineState !== "available")) || !scope?.account || !scope?.user || !scope?.agentId;
    stop.hidden = !running || resetCommand; send.hidden = running && !resetCommand;
    stop.disabled = stopping; stop.setAttribute("aria-label", stopping ? "正在停止" : "停止生成");
    stop.title = stopping ? "正在停止" : "停止生成";
    retry.hidden = !problem && currentRun?.state !== "error";
    controls.hidden = retry.hidden;
    const phase = currentRun?.phaseEvent || { state: currentRun?.phase || "preparing" };
    const labels = { preparing: "正在准备", starting: "正在准备", awaiting: "正在等待", answering: "正在回答",
      running: "正在回答", thinking: "正在思考", busy: "正在回答", retry: "正在重试", compressing: "正在压缩上下文", compacting: "正在压缩上下文" };
    const event = problem ? { state: "error", text: problem } : submitting ? { state: "preparing", text: "正在提交" } :
      stopping ? { state: "stopping", text: "正在停止" } : phase;
    const savedError = !problem && !submitting && !stopping && currentRun?.state === "error" &&
      lastSavedMessage?.role === "status" && lastSavedMessage.status === "error" &&
      lastSavedMessage.text === "生成失败：" + currentRun.error;
    const content = submitting || stopping || problem ? event.text : running ? phase.text || labels[phase.state] || "正在准备" :
      currentRun?.state === "stopped" ? "已停止" : currentRun?.state === "error" && !savedError ? currentRun.error || "生成失败" : "";
    statusRow("ui:run", content, event.state, event);
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
      (message.delivery === undefined || ["pending", "failed"].includes(message.delivery)) &&
      (message.runId === undefined || text(message.runId, 240)) &&
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
      ADVISOR_INIT: ["activeAgentId", "enabledTemplates", "thread", "draft", "catalogReady", "engineState", "theme", "run"],
      ADVISOR_THEME: ["theme"], ADVISOR_SET_TEMPLATES: ["enabledTemplates", "activeAgentId", "catalogReady", "engineState"],
      ADVISOR_SET_THREAD: ["thread", "run", "agentId", "draft"],
      ADVISOR_STREAM_UPDATE: ["threadId", "run", "messages"],
      ADVISOR_PROBLEM: ["error", "retryKind"], ADVISOR_SUBMIT_STATE: ["pending"], ADVISOR_STOP_STATE: ["pending"],
      ADVISOR_COPY_RESULT: ["copied"]
    };
    if (!permitted[type] || Object.keys(payload).some(key => !permitted[type].includes(key))) return false;
    if (type === "ADVISOR_INIT" || type === "ADVISOR_SET_TEMPLATES") {
      if (!Array.isArray(payload.enabledTemplates) || payload.enabledTemplates.length > 200 ||
        !payload.enabledTemplates.every(item => object(item) && text(item.id, 240) && text(item.name, 60) &&
          (item.welcome === undefined || text(item.welcome, 120))) ||
        !text(payload.activeAgentId, 240) || typeof payload.catalogReady !== "boolean" ||
        !["available", "missing", "unknown"].includes(payload.engineState)) return false;
    }
    if (type === "ADVISOR_INIT" || type === "ADVISOR_SET_THREAD") {
      if (!object(payload.thread) || !text(payload.thread.id, 240) || !Array.isArray(payload.thread.messages) ||
        !payload.thread.messages.every(validMessage) || !text(payload.draft, 16000)) return false;
      if (payload.run && !validRun(payload.run)) return false;
    }
    if (type === "ADVISOR_STREAM_UPDATE" && (!text(payload.threadId, 240) || !Array.isArray(payload.messages) ||
      payload.messages.length > 200 || !payload.messages.every(validMessage) || !validRun(payload.run))) return false;
    if (["ADVISOR_INIT", "ADVISOR_THEME"].includes(type) && !["light", "dark"].includes(payload.theme)) return false;
    if (type === "ADVISOR_PROBLEM" && (!text(payload.error, 20000) || !["send", "poll", "load", "catalog"].includes(payload.retryKind))) return false;
    if (["ADVISOR_SUBMIT_STATE", "ADVISOR_STOP_STATE"].includes(type) && typeof payload.pending !== "boolean") return false;
    if (type === "ADVISOR_COPY_RESULT" && typeof payload.copied !== "boolean") return false;
    return true;
  }
  menuButton.addEventListener("click", () => {
    agentMenu.hidden = !agentMenu.hidden; menuButton.setAttribute("aria-expanded", String(!agentMenu.hidden));
  });
  document.addEventListener("click", event => {
    if (!event.target.closest?.(".advisor-agent-control")) { agentMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false"); }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { agentMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false"); }
  });
  stop.addEventListener("click", () => sendToParent("ADVISOR_STOP"));
  retry.addEventListener("click", () => sendToParent("ADVISOR_RETRY", { retryKind: currentRun?.state === "error" ? "send" : retryKind }));
  function submitMessage() {
    const value = input.value;
    if (!value.trim() || value.length > 16000 || send.disabled) return;
    submitting = true; updateControls();
    sendToParent("ADVISOR_SEND", { text: value, agentId: scope.agentId });
  }
  send.addEventListener("click", submitMessage);
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitMessage(); }
  });
  input.addEventListener("input", () => {
    sendToParent("ADVISOR_DRAFT_UPDATE", { draft: input.value, agentId: scope?.agentId }); updateControls();
  });
  window.addEventListener("message", event => {
    if (event.source !== window.parent) return;
    const data = event.data;
    if (!object(data) || data.envelope !== "advisor" || !/^adv_[0-9a-f]{32}$/.test(data.nonce) ||
      Object.keys(data).some(key => !["envelope", "nonce", "type", "payload", "scope"].includes(key)) ||
      !validScope(data.scope) || !validPayload(data.type, data.payload)) return;
    if (data.type === "ADVISOR_INIT") {
      if (sessionNonce && sessionNonce !== data.nonce) return;
      sessionNonce = data.nonce; scope = data.scope; templates = data.payload.enabledTemplates;
      catalogReady = data.payload.catalogReady; engineState = data.payload.engineState;
      document.body.className = `advisor-frame-body theme-${data.payload.theme}`;
      renderTemplates(); renderThread(data.payload.thread, data.payload.run, data.payload.draft);
      sendToParent("ADVISOR_FRAME_READY"); return;
    }
    if (!sessionNonce || data.nonce !== sessionNonce) return;
    if (["ADVISOR_SET_TEMPLATES", "ADVISOR_SET_THREAD"].includes(data.type)) {
      if (data.scope.generation < scope.generation || (data.scope.generation === scope.generation && !sameScope(data.scope))) return;
      scope = data.scope;
    } else if (!sameScope(data.scope)) return;
    const payload = data.payload;
    if (data.type === "ADVISOR_SET_TEMPLATES") {
      templates = payload.enabledTemplates; catalogReady = payload.catalogReady; engineState = payload.engineState; renderTemplates();
    } else if (data.type === "ADVISOR_SET_THREAD") {
      renderTemplates(); renderThread(payload.thread, payload.run, payload.draft);
    } else if (data.type === "ADVISOR_STREAM_UPDATE") applyStream(payload);
    else if (data.type === "ADVISOR_THEME") {
      document.body.classList.toggle("theme-light", payload.theme === "light");
      document.body.classList.toggle("theme-dark", payload.theme === "dark");
    }
    else if (data.type === "ADVISOR_PROBLEM") {
      problem = payload.error; retryKind = payload.retryKind; updateControls();
    } else if (data.type === "ADVISOR_SUBMIT_STATE") {
      submitting = payload.pending; if (submitting) { problem = ""; statusRow("ui:problem", ""); } updateControls();
    } else if (data.type === "ADVISOR_STOP_STATE") { stopping = payload.pending; updateControls(); }
  });
  updateControls(); updateLayout();
})();
