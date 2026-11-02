"use strict";
// Modified by Know Your Friends, 2026-10-09: isolated reply candidates, explicit
// regeneration from latest chat, and assistant runs that survive view changes.
// Modified 2026-10-10: persistent, isolated assistant sessions and native compaction.
// Modified 2026-10-11: remembered 深度思考 choice sent with each answer.
// Moved from chatui/advisor.js to plugins/kyf.advisor/ui/panel-controller.js on 2026-10-13 (kyf.advisor plugin):
// the session/run half now runs inside the sandboxed 聊天助手 panel page and reaches Core only through
// kyfHost (plugin RPC + host methods); the repository half is repository.js; the retired automatic reply
// suggestions and browser storage are gone (drafts persist through draft.save, the outbox stays in memory).
// Modified 2026-10-14: friends picked with @ are sent with the question and can be removed from the discussion.
// Modified 2026-10-16: durable per-discussion FIFO and independent composer drafts.

(function () {
  const TERMINAL = new Set(["done", "error", "stopped", "cancelled", "interrupted"]);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value, limit) => typeof value === "string" && value.length <= limit;
  const id = value => text(value, 240) && value.length > 0;
  const validDraftMentions = value => Array.isArray(value) && value.length <= 8000 && value.every(item =>
    object(item) && id(item.user) && text(item.name, 240) && item.name.trim().length > 0);
  function sameDraftContent(previous, value, mentions) {
    const selected = previous?.mentions || [];
    return previous?.text === value && selected.length === mentions.length &&
      selected.every((item, index) => item.user === mentions[index].user && item.name === mentions[index].name);
  }
  function nonce(prefix) {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return prefix + Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  }
  function validCatalog(value) {
    return object(value) && Array.isArray(value.agents) && Array.isArray(value.skills) &&
      value.agents.every(agent => object(agent) && id(agent.id) && text(agent.name, 60) &&
        text(agent.description, 240) && text(agent.prompt, 16000) && typeof agent.enabled === "boolean" &&
        (agent.welcome === undefined || text(agent.welcome, 120)) &&
        Array.isArray(agent.skillIds) && agent.skillIds.every(id)) &&
      value.skills.every(skill => object(skill) && id(skill.id) && text(skill.name, 60)) &&
      ["available", "missing"].includes(value.engine?.state);
  }
  function validThread(value, scope) {
    return object(value) && id(value.id) && value.account === scope.account && value.user === scope.user &&
      value.agentId === scope.agentId && Array.isArray(value.messages) && value.messages.every(message =>
        object(message) && id(message.id) && ["user", "assistant", "status"].includes(message.role) && text(message.text, 2000000));
  }
  function errorText(value) {
    if (text(value?.message, 20000) && value.message) return value.message;
    if (text(value?.error, 20000) && value.error) return value.error;
    if (text(value?.error?.message, 20000) && value.error.message) return value.error.message;
    return "服务返回无效数据";
  }
  function validQueue(queue) {
    return object(queue) && typeof queue.paused === "boolean" && text(queue.reason || "", 2000) &&
      Number.isSafeInteger(queue.version) && queue.version >= 0 && Array.isArray(queue.items) &&
      queue.items.every(item => object(item) && id(item.id) && id(item.requestId) && text(item.message, 16000) &&
        Number.isSafeInteger(item.version) && item.version >= 0 && ["pending", "starting"].includes(item.state) &&
        typeof item.thinking === "boolean" && Array.isArray(item.mentions) && item.mentions.every(id));
  }
  const Advisor = {
    panelVisible: true,
    activeAgentId: "",
    // 深度思考 applies to the next answer and its summaries; kept for this panel's lifetime.
    thinkingEnabled: true,
    currentAccount: "", currentUser: "", currentName: "", currentRunId: null, theme: "dark",
    generation: 0, loadSerial: 0, catalogSerial: 0,
    frameReady: false,
    catalog: { agents: [], skills: [], engine: { state: "missing" } },
    catalogReady: false, catalogError: "",
    threadCache: {}, pendingRequests: {},
    selectedThreads: {}, draftRecords: {}, draftTimers: {}, draftSaves: {}, sessionProblems: {},
    currentThreadId: "", historyReturn: null, historyItems: [], historySerial: 0, historyOpen: false, historyTimer: null,
    sessionActions: {}, pendingSubmits: new Set(), pendingCompacts: new Set(), pendingNew: new Set(), operationTimer: null,
    pollAbortController: null, submitPending: false, stopPending: false,
    threadProjection: null, lastProblem: null, pendingQuote: null,
    clearedAccounts: new Set(), pollError: false,
    pendingEnqueues: {}, queueProblems: {}, queueThreadVersions: {}, queueMutations: new Set(), queueTimer: null, queuePollSerial: 0,

    baseKey(account, user, agentId) { return JSON.stringify([account || "", user || "", agentId || ""]); },
    scopeKey(account, user, agentId, threadId) {
      return JSON.stringify([account || "", user || "", agentId || "", threadId ?? this.selectedThreads[this.baseKey(account, user, agentId)] ?? ""]);
    },
    captureScope() {
      return Object.freeze({ account: this.currentAccount, user: this.currentUser,
        agentId: this.activeAgentId, threadId: this.currentThreadId, generation: this.generation });
    },
    isCurrent(scope) {
      return scope.generation === this.generation && scope.account === this.currentAccount &&
        scope.user === this.currentUser && scope.agentId === this.activeAgentId &&
        (scope.threadId === undefined || scope.threadId === this.currentThreadId) && !this.clearedAccounts.has(scope.account);
    },
    // Plugin RPC through the host. The call names the friend it was made for; errors become {error, code, status}.
    async call(method, params = {}, scope = null) {
      try {
        const result = await window.kyfHost.rpc(method, params, scope ? { account: scope.account, user: scope.user } : undefined);
        return object(result) ? result : {};
      } catch (error) {
        return { error: error?.message || "请求失败", code: error?.code || "error", status: error?.status || 0 };
      }
    },
    hideCurrentRun() {
      // The service owns generation. Hiding its view only detaches UI polling;
      // loadThread reconnects to the saved run when this conversation returns.
      this.abortCurrentPoll(); this.stopQueuePolling();
      this.closeHistory(); void this.flushDraft();
    },
    getDraft(account, user, agentId, threadId) {
      return this.draftRecords[this.scopeKey(account, user, agentId, threadId)]?.text || "";
    },
    setDraft(account, user, agentId, draft, threadId, scrollTop, mentions) {
      if (!account || !user || !agentId || this.clearedAccounts.has(account) || !text(draft, 16000)) return;
      threadId ??= this.selectedThreads[this.baseKey(account, user, agentId)] || "";
      const key = this.scopeKey(account, user, agentId, threadId), previous = this.draftRecords[key] || { version: 0, scrollTop: 0, localVersion: 0 };
      // Exact commands are UI actions, never database drafts or conversation messages.
      if (/^\s*\/(?:new|reset|compact|sessions)\s*$/.test(draft)) draft = "";
      const selected = validDraftMentions(mentions) ? mentions : previous.mentions || [];
      const nextMentions = selected.filter(item => draft.includes("@" + item.name)).map(item => ({ user: item.user, name: item.name }));
      this.draftRecords[key] = { ...previous, text: draft,
        mentions: nextMentions,
        // Scroll saves must not make an accepted question look like new composer input.
        composerVersion: (previous.composerVersion || 0) + (sameDraftContent(previous, draft, nextMentions) ? 0 : 1),
        dirty: true, localVersion: previous.localVersion + 1,
        ...(Number.isFinite(scrollTop) && scrollTop >= 0 ? { scrollTop } : {}) };
      clearTimeout(this.draftTimers[key]);
      if (threadId) this.draftTimers[key] = setTimeout(() => { delete this.draftTimers[key]; void this.flushDraft({ account, user, agentId, threadId }); }, 350);
    },
    async flushDraft(scope = this.captureScope(), retry = false) {
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      clearTimeout(this.draftTimers[key]); delete this.draftTimers[key];
      if (this.draftSaves[key]) return this.draftSaves[key];
      const own = async () => {
        while (!this.clearedAccounts.has(scope.account)) {
          const draft = this.draftRecords[key];
          if (!scope.threadId || !draft?.dirty || (draft.conflict && !retry)) return;
          const localVersion = draft.localVersion;
          const result = await this.call("draft.save", { agentId: scope.agentId, threadId: scope.threadId,
            text: draft.text, version: draft.version, scrollTop: draft.scrollTop || 0,
            ...(draft.mentions?.length ? { mentions: draft.mentions } : {}) }, scope);
          if (this.clearedAccounts.has(scope.account) || !this.draftRecords[key]) return;
          const current = this.draftRecords[key];
          if (!object(result.draft) || !Number.isSafeInteger(result.draft.version) || result.error) {
            current.conflict = true;
            // Preserve this editor's text. A conflict must be explicitly retried, never overwrite another editor silently.
            const conflict = result.code === "request-conflict";
            if (conflict) {
              const latest = await this.call("thread.get", { agentId: scope.agentId, threadId: scope.threadId }, scope);
              if (this.draftRecords[key] === current && Number.isSafeInteger(latest.thread?.draft?.version)) current.version = latest.thread.draft.version;
            }
            this.problem({ error: conflict ? "另一窗口更新了草稿。当前输入已保留；点击重试保存当前输入。" : "草稿尚未保存，请重试。" }, scope, "draft");
            return;
          }
          current.version = result.draft.version; current.conflict = false;
          current.dirty = current.localVersion !== localVersion;
          this.clearProblem("draft", scope);
          if (!current.dirty) return;
          retry = false;
        }
      };
      const pending = own(); this.draftSaves[key] = pending;
      try { await pending; } finally { if (this.draftSaves[key] === pending) delete this.draftSaves[key]; }
    },
    acceptDraft(scope, thread) {
      if (!thread.id || thread.readonly) return;
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, thread.id), saved = this.draftRecords[key];
      const draft = thread.draft;
      if (object(draft) && saved?.dirty && saved.version !== draft.version) {
        saved.version = draft.version; saved.conflict = true;
        this.problem({ error: "另一窗口更新了草稿。当前输入已保留；点击重试保存当前输入。" }, { ...scope, threadId: thread.id }, "draft");
      }
      if (object(draft) && text(draft.text, 16000) && Number.isSafeInteger(draft.version) && !saved?.dirty) {
        const mentions = validDraftMentions(draft.mentions) ? draft.mentions.map(item => ({ user: item.user, name: item.name })) : [];
        this.draftRecords[key] = { text: draft.text, version: draft.version, scrollTop: draft.scrollTop || 0,
          mentions, composerVersion: (saved?.composerVersion || 0) + (sameDraftContent(saved, draft.text, mentions) ? 0 : 1),
          localVersion: saved?.localVersion || 0, dirty: false };
      }
    },
    currentContact(scope = this.captureScope()) {
      return this.isCurrent(scope) && this.currentName && scope.user ? { user: scope.user, name: this.currentName } : null;
    },
    seedContactDraft(thread, scope) {
      const contact = this.currentContact(scope), key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      const draft = this.draftRecords[key];
      // Only a freshly loaded, unused discussion gets the opening @. Saving it makes an intentional
      // deletion durable too: an empty draft at a later version is never filled again.
      if (!contact || !thread.id || thread.readonly || thread.deleteState || !draft || draft.version !== 0 ||
        draft.dirty || draft.text || thread.messages.length || thread.queue?.items?.length) return;
      this.setDraft(scope.account, scope.user, scope.agentId, "@" + contact.name + " ", scope.threadId);
    },
    getCachedThread(account, user, agentId, threadId) {
      const value = this.threadCache[this.scopeKey(account, user, agentId, threadId)];
      return validThread(value, { account, user, agentId }) ? value : { id: "", account, user, agentId, messages: [] };
    },
    setCachedThread(account, user, agentId, thread) {
      if (this.clearedAccounts.has(account) || !validThread(thread, { account, user, agentId })) return;
      this.threadCache[this.scopeKey(account, user, agentId, thread.id)] = thread;
    },
    getEnabledTemplates() { return this.catalogReady ? this.catalog.agents.filter(agent => agent.enabled) : []; },
    // The conversation view lives in the same document (panel-frame.js); messages keep their scope.
    postToFrame(type, payload = {}, scope = this.captureScope()) {
      window.AdvisorView?.receive({ type, payload, scope });
    },
    problem(error, scope = this.captureScope(), retryKind = "load") {
      const payload = { error: errorText(error), retryKind };
      if (this.clearedAccounts.has(scope.account)) return;
      this.sessionProblems[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)] = payload;
      if (!this.isCurrent(scope)) return;
      this.lastProblem = { scope, payload };
      this.postToFrame("ADVISOR_PROBLEM", payload, scope);
    },
    clearProblem(retryKind, scope = this.captureScope()) {
      if (!this.isCurrent(scope) || !this.lastProblem || !this.isCurrent(this.lastProblem.scope) ||
        (retryKind && this.lastProblem.payload.retryKind !== retryKind)) return;
      const previousKind = this.lastProblem.payload.retryKind;
      delete this.sessionProblems[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)];
      this.lastProblem = null;
      this.postToFrame("ADVISOR_PROBLEM", { error: "", retryKind: previousKind }, scope);
    },
    // Pending turn edits appear once; regenerating keeps the original question visible.
    withPending(thread, pending) {
      if (!pending || pending.retryRunId || pending.replace?.mode === "regenerate" || !id(pending.requestId) || !text(pending.message, 16000)) return thread;
      if (thread.messages.some(message => message.id === pending.requestId || message.requestId === pending.requestId)) return thread;
      return { ...thread, messages: [...thread.messages, { id: pending.requestId, requestId: pending.requestId, role: "user",
        text: pending.message, delivery: pending.delivery || "pending", createdAtMs: pending.createdAtMs || Date.now() }] };
    },
    projectionForScope(scope = this.captureScope()) {
      const cached = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      const pending = this.pendingRequests[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)];
      const thread = this.withPending({ ...cached, messages: [...cached.messages] }, pending);
      const saved = this.threadProjection;
      return { thread, run: saved && this.isCurrent(saved.scope) && saved.thread.id === thread.id ? saved.run : cached._advisorRun || null };
    },
    activeRunId() {
      if (this.currentRunId) return this.currentRunId;
      const { thread, run } = this.projectionForScope();
      // A detached view can still have a verified saved run even if fetching
      // its latest progress fails. Keep explicit stop/reconnect available.
      return id(run?.id) && run.threadId === thread.id && !TERMINAL.has(run.state) ? run.id : null;
    },
    syncThreadToFrame(thread, run = null, scope = this.captureScope()) {
      if (!this.isCurrent(scope)) return;
      this.seedContactDraft(thread, scope);
      thread = { ...thread, compactPending: this.pendingCompacts.has(this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)) };
      thread = this.withPending(thread, this.pendingRequests[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)]);
      this.threadProjection = { scope, thread, run };
      this.postToFrame("ADVISOR_SET_THREAD", { thread, run, agentId: scope.agentId,
        draft: thread.readonly ? "" : this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId),
        draftMentions: thread.readonly ? [] : this.draftRecords[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)]?.mentions || [],
        currentContact: this.currentContact(scope),
        scrollTop: this.draftRecords[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)]?.scrollTop ?? null,
        canReturn: !!this.historyReturn }, scope);
      this.syncQueueToFrame(scope);
      this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: this.pendingSubmits.has(this.queueKey(scope)) }, scope);
    },
    templatesPayload() {
      return { enabledTemplates: this.getEnabledTemplates(), activeAgentId: this.activeAgentId,
        catalogReady: this.catalogReady };
    },
    syncReadyFrame() {
      const scope = this.captureScope(), projection = this.projectionForScope(scope);
      this.postToFrame("ADVISOR_SET_TEMPLATES", this.templatesPayload(), scope);
      this.syncThreadToFrame(projection.thread, projection.run, scope);
      this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: this.submitPending }, scope);
      this.postToFrame("ADVISOR_STOP_STATE", { pending: this.stopPending }, scope);
      if (this.lastProblem && this.isCurrent(this.lastProblem.scope)) this.postToFrame("ADVISOR_PROBLEM", this.lastProblem.payload, scope);
    },
    syncTemplatesToFrame() {
      const enabled = this.getEnabledTemplates(), previous = this.activeAgentId;
      const thread = this.getCachedThread(this.currentAccount, this.currentUser, previous, this.currentThreadId);
      if (this.catalogReady && !this.historyReturn && !enabled.some(agent => agent.id === previous)) {
        if (thread.id) {
          thread.readonly = true; thread.readonlyReason = "原助手已停用或删除，仅可查看历史";
          this.setCachedThread(this.currentAccount, this.currentUser, previous, thread);
          this.syncThreadToFrame(thread, thread._advisorRun || this.threadProjection?.run || null);
        } else this.activeAgentId = enabled[0]?.id || "";
      }
      if (previous !== this.activeAgentId) {
        this.generation++; this.abortCurrentPoll(); this.submitPending = false;
        this.currentThreadId = this.selectedThreads[this.baseKey(this.currentAccount, this.currentUser, this.activeAgentId)] || "";
      }
      this.postToFrame("ADVISOR_SET_TEMPLATES", this.templatesPayload());
      if (previous !== this.activeAgentId) void this.loadThread(this.captureScope(), true);
    },
    async loadCatalog() {
      const serial = ++this.catalogSerial;
      const data = await this.call("catalog.get");
      if (serial !== this.catalogSerial) return false;
      if (!validCatalog(data)) {
        this.catalogError = errorText(data); this.catalogReady = false;
        this.syncTemplatesToFrame(); this.problem(data, this.captureScope(), "catalog"); return false;
      }
      this.catalog = data; this.catalogReady = true; this.catalogError = "";
      this.clearProblem("catalog");
      this.syncTemplatesToFrame(); return true;
    },
    abortCurrentPoll() {
      this.pollAbortController?.abort(); this.pollAbortController = null; this.currentRunId = null;
      this.pollError = false; this.stopPending = false; clearTimeout(this.operationTimer); this.operationTimer = null;
    },
    rememberProjection(scope, data) {
      if (!validThread(data?.thread, scope) || this.clearedAccounts.has(scope.account)) return false;
      const thread = { ...data.thread, _advisorRun: data.run || null };
      const old = this.getCachedThread(scope.account, scope.user, scope.agentId, thread.id);
      if (validQueue(old.queue) && (!validQueue(thread.queue) || old.queue.version > thread.queue.version)) thread.queue = old.queue;
      // A fresh last page must not discard earlier pages already opened in this view.
      if (old.id === thread.id && old.messages.length && old._olderLoaded) {
        const boundary = thread.messages.find(message => Number.isSafeInteger(message.seq))?.seq;
        // Only retain acknowledged earlier rows. Streaming placeholders in the refreshed page
        // must be replaced by the database's final message, even if the IDs differ.
        const messages = new Map(old.messages.filter(message => Number.isSafeInteger(message.seq) &&
          Number.isSafeInteger(boundary) && message.seq < boundary).map(message => [message.id, message]));
        for (const message of thread.messages) messages.set(message.id, message);
        thread.messages = [...messages.values()].sort((a, b) => (a.seq ?? a.createdAtMs ?? 0) - (b.seq ?? b.createdAtMs ?? 0));
        thread._olderLoaded = true; thread.beforeSeq = old.beforeSeq; thread.hasMore = old.hasMore;
      }
      this.acceptDraft(scope, thread);
      this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
      return thread;
    },
    adoptThread(data, previousScope = this.captureScope()) {
      if (!this.isCurrent(previousScope) || !validThread(data?.thread, previousScope)) return null;
      const thread = this.rememberProjection(previousScope, data);
      if (this.currentThreadId !== thread.id) {
        this.generation++; this.abortCurrentPoll(); this.currentThreadId = thread.id;
      }
      if (!thread.readonly) this.selectedThreads[this.baseKey(previousScope.account, previousScope.user, previousScope.agentId)] = thread.id;
      const scope = this.captureScope(), key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      this.threadProjection = { scope, thread, run: data.run || null };
      this.submitPending = this.pendingSubmits.has(key);
      const pending = this.pendingRequests[key];
      const acknowledged = pending && data.run?.requestId === pending.requestId && data.thread.messages.some(item => item.id === data.run.messageId);
      if (acknowledged) {
        delete this.pendingRequests[key];
        if (pending.replace?.mode === "edit" && this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId) === pending.message)
          this.setDraft(scope.account, scope.user, scope.agentId, "", scope.threadId);
      }
      this.lastProblem = this.sessionProblems[key] ? { scope, payload: this.sessionProblems[key] } : null;
      this.syncReadyFrame();
      if (acknowledged && pending.replace?.mode === "edit") this.postToFrame("ADVISOR_EDIT_RESULT", {
        runId: pending.replace.runId, ok: true, error: "" }, scope);
      if (this.pendingQuote) { const quote = this.pendingQuote; this.pendingQuote = null; this.quoteMessage(quote); }
      // A hidden panel only keeps the saved projection; showing it reconnects.
      if (!this.panelVisible) return scope;
      this.scheduleQueuePoll(scope);
      if (thread.activeOperation && ["running", "unknown"].includes(thread.activeOperation.state)) this.pollOperation(thread.activeOperation, scope);
      else if (data.run?.id && !TERMINAL.has(data.run.state)) void this.pollEvents(data.run.id, scope);
      const draft = this.draftRecords[key];
      // An edit made just before a friend switch may still be unsaved; save it now that its discussion is open again.
      if (draft?.dirty && !draft.conflict && !this.draftTimers[key]) void this.flushDraft(scope);
      return scope;
    },
    acknowledgePending(scope, pending, run) {
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      if (!pending || this.pendingRequests[key]?.requestId !== pending.requestId || this.clearedAccounts.has(scope.account) ||
        run?.threadId !== pending.threadId || !id(run.messageId)) return false;
      delete this.pendingRequests[key];
      const thread = this.getCachedThread(scope.account, scope.user, scope.agentId, pending.threadId);
      if (thread.id === pending.threadId && pending.replace) {
        // The replaced answer (and, for an edit, its question) folds away at once; the service marked it the same way.
        const question = thread.messages.filter(item => item.role === "user" && !item.supersededBy && Number.isSafeInteger(item.seq)).at(-1);
        for (const item of thread.messages) {
          if (question && !item.supersededBy && Number.isSafeInteger(item.seq) &&
            item.seq >= question.seq + (pending.replace.mode === "regenerate" ? 1 : 0)) item.supersededBy = run.id;
        }
      }
      if (thread.id === pending.threadId && !thread.messages.some(item => item.id === run.messageId)) {
        thread.messages.push({ id: run.messageId, requestId: pending.requestId, role: "user", text: pending.message,
          createdAtMs: pending.createdAtMs || Date.now(),
          ...(object(run.contextSnapshot) ? { contextSnapshot: run.contextSnapshot } : { _contextPending: true }),
          ...(pending.mentions?.length ? { mentions: [...pending.mentions] } : {}) });
      }
      thread._advisorRun = run;
      this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
      return true;
    },
    failPending(scope, pending, error = { error: "提问暂未确认提交成功，请重试。" }) {
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      if (this.clearedAccounts.has(scope.account) || this.pendingRequests[key]?.requestId !== pending.requestId) return false;
      pending.delivery = "failed";
      if (!pending.retryRunId && pending.replace?.mode !== "regenerate" && !this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId))
        this.setDraft(scope.account, scope.user, scope.agentId, pending.message, scope.threadId);
      this.problem(error, scope, "send");
      return true;
    },
    async loadThread(scope = this.captureScope(), restoreSelection = false) {
      if (!this.isCurrent(scope)) return;
      const serial = ++this.loadSerial, projection = this.projectionForScope(scope);
      this.syncThreadToFrame(projection.thread, projection.run, scope);
      if (!scope.account || !scope.user || !this.catalogReady) return;
      if (!scope.agentId) { this.pendingQuote = null; await this.loadHistory(); return; }
      const data = await this.call("thread.get", { agentId: scope.agentId,
        ...(!restoreSelection && scope.threadId ? { threadId: scope.threadId } : {}) }, scope);
      if (!this.isCurrent(scope) || serial !== this.loadSerial) return;
      if (!validThread(data?.thread, scope)) { this.problem(data, scope); return; }
      this.clearProblem("load", scope);
      return this.adoptThread(data, scope);
    },
    queueKey(scope = this.captureScope()) { return this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId); },
    syncQueueToFrame(scope = this.captureScope()) {
      if (!this.isCurrent(scope)) return;
      const key = this.queueKey(scope), thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      this.postToFrame("ADVISOR_QUEUE", { queue: validQueue(thread.queue) ? thread.queue : null,
        error: this.queueProblems[key] || "", pending: this.queueMutations.has(key),
        retryEnqueue: !!this.pendingEnqueues[key] && !this.pendingSubmits.has(key) }, scope);
    },
    acceptQueue(scope, queue) {
      if (!validQueue(queue) || this.clearedAccounts.has(scope.account)) return false;
      const thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      if (!thread.id) return false;
      if (!validQueue(thread.queue) || queue.version >= thread.queue.version) thread.queue = queue;
      this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
      if (this.isCurrent(scope)) this.syncQueueToFrame(scope);
      return true;
    },
    stopQueuePolling() { clearTimeout(this.queueTimer); this.queueTimer = null; this.queuePollSerial++; },
    scheduleQueuePoll(scope = this.captureScope(), delay = 1500) {
      this.stopQueuePolling();
      if (!this.panelVisible || !this.isCurrent(scope) || !scope.threadId) return;
      const serial = this.queuePollSerial;
      this.queueTimer = setTimeout(async () => {
        this.queueTimer = null;
        if (!this.panelVisible || !this.isCurrent(scope) || serial !== this.queuePollSerial) return;
        await this.refreshQueue(scope);
        if (this.panelVisible && this.isCurrent(scope) && serial === this.queuePollSerial) this.scheduleQueuePoll(scope);
      }, delay);
    },
    async refreshQueue(scope = this.captureScope()) {
      if (!scope.threadId || !this.isCurrent(scope)) return;
      const result = await this.postSession("queue.get", scope);
      if (!this.isCurrent(scope)) return;
      const key = this.queueKey(scope);
      if (!this.acceptQueue(scope, result.queue)) {
        this.queueProblems[key] = errorText(result); this.syncQueueToFrame(scope); return;
      }
      // Run polling owns a live answer; when it ends, discover the next dequeued run independently of view navigation.
      if (!this.activeRunId() && this.queueThreadVersions[key] !== result.queue.version) {
        const data = await this.call("thread.get", { agentId: scope.agentId, threadId: scope.threadId }, scope);
        if (!this.isCurrent(scope) || !validThread(data.thread, scope) || data.thread.id !== scope.threadId) return;
        this.queueThreadVersions[key] = result.queue.version;
        if (data.run?.id && !TERMINAL.has(data.run.state)) this.adoptThread(data, scope);
        else {
          const thread = this.rememberProjection(scope, data);
          this.syncThreadToFrame(thread, data.run || null, scope);
        }
      }
    },
    async enqueueMessage(message, mentions = []) {
      let scope = this.captureScope();
      if (!scope.account || !scope.user || !id(scope.agentId) || !text(message, 16000) || !message.trim()) return;
      if (!this.catalogReady || !this.getEnabledTemplates().some(agent => agent.id === scope.agentId)) {
        this.problem({ error: "模板未启用" }, scope, "catalog"); return;
      }
      if (!scope.threadId || !this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId).id) {
        if (!this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId)) this.setDraft(scope.account, scope.user, scope.agentId, message, scope.threadId);
        const next = await this.loadThread(scope);
        if (!next || !this.isCurrent(next)) return;
        scope = next;
        if (!this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId)) this.setDraft(scope.account, scope.user, scope.agentId, message, scope.threadId);
      }
      const thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId), key = this.queueKey(scope);
      if (!thread.id || thread.readonly || thread.deleteState || this.pendingSubmits.has(key)) return;
      let pending = this.pendingEnqueues[key];
      if (pending && pending.message !== message) {
        this.queueProblems[key] = "上一条消息是否已加入队列尚未确认，请先重试确认；当前输入已保留。";
        this.syncQueueToFrame(scope); return;
      }
      if (!pending) {
        if (!this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId)) this.setDraft(scope.account, scope.user, scope.agentId, message, scope.threadId);
        pending = { requestId: nonce("req_"), message, mentions: [...mentions], thinking: this.thinkingEnabled,
          composerVersion: this.draftRecords[key]?.composerVersion, threadId: scope.threadId };
        this.pendingEnqueues[key] = pending;
      }
      this.pendingSubmits.add(key); this.submitPending = true;
      this.clearProblem("send", scope);
      delete this.queueProblems[key]; this.syncQueueToFrame(scope);
      this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: true }, scope);
      const result = await this.postSession("queue.enqueue", scope, { message: pending.message, requestId: pending.requestId,
        thinking: pending.thinking, mentions: pending.mentions });
      this.pendingSubmits.delete(key);
      if (this.clearedAccounts.has(scope.account)) return;
      const accepted = result.threadId === scope.threadId && id(result.item?.id) && result.item.requestId === pending.requestId && validQueue(result.queue);
      if (accepted) {
        delete this.pendingEnqueues[key]; delete this.queueProblems[key];
        const draft = this.draftRecords[key];
        // A late acknowledgement clears only the exact submitted composer version, never newer typing.
        if (draft && draft.text === pending.message && draft.composerVersion === pending.composerVersion)
          this.setDraft(scope.account, scope.user, scope.agentId, "", scope.threadId);
        this.acceptQueue(scope, result.queue);
      } else {
        this.queueProblems[key] = result.error ? errorText(result) : "尚未确认加入队列，消息已保留，请重试确认。";
        // These errors are returned before durable acceptance; edited input may be submitted as a new request.
        if (["invalid-request", "agent-disabled", "agent-unknown", "thread-unknown", "thread-readonly", "thread-deleting", "invalid-settings"].includes(result.code))
          delete this.pendingEnqueues[key];
      }
      const view = this.captureScope();
      if (view.account !== scope.account || view.user !== scope.user || view.agentId !== scope.agentId || view.threadId !== scope.threadId) return;
      this.submitPending = false; this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: false }, view);
      this.syncThreadToFrame(this.getCachedThread(view.account, view.user, view.agentId, view.threadId), this.projectionForScope(view).run, view);
      if (accepted) this.postToFrame("ADVISOR_ENQUEUED", { message: pending.message }, view);
      this.scheduleQueuePoll(view, 100);
    },
    async mutateQueue(action, payload = {}) {
      const scope = this.captureScope(), key = this.queueKey(scope);
      const thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      if (!scope.threadId || thread.readonly || thread.deleteState || this.queueMutations.has(key)) return;
      this.queueMutations.add(key); delete this.queueProblems[key]; this.syncQueueToFrame(scope);
      const result = await this.postSession("queue." + action, scope, payload);
      this.queueMutations.delete(key);
      if (this.clearedAccounts.has(scope.account)) return;
      const ok = !result.error && this.acceptQueue(scope, result.queue);
      if (!ok) this.queueProblems[key] = errorText(result);
      if (!this.isCurrent(scope)) return;
      if (!ok) await this.refreshQueue(scope);
      if (!this.isCurrent(scope)) return;
      this.syncQueueToFrame(scope);
      this.postToFrame("ADVISOR_QUEUE_RESULT", { action, itemId: payload.itemId || "", ok: !!ok, error: ok ? "" : errorText(result) }, scope);
      this.scheduleQueuePoll(scope, 100);
    },
    retryQueue() {
      const scope = this.captureScope(), key = this.queueKey(scope), pending = this.pendingEnqueues[key];
      if (pending) return this.enqueueMessage(pending.message, pending.mentions);
      delete this.queueProblems[key]; this.syncQueueToFrame(scope);
      return this.refreshQueue(scope);
    },
    // ``replace`` {mode: "regenerate" | "edit", runId} answers the discussion's latest turn again (added 2026-10-15).
    async startRun(message, agentId = this.activeAgentId, retryRunId = null, mentions = [], replace = null) {
      let scope = this.captureScope();
      if (!this.isCurrent(scope) || !scope.account || !scope.user || agentId !== scope.agentId || !text(message, 16000) || !message.trim()) return;
      const command = message.trim();
      if (!replace && (command === "/new" || command === "/reset")) return this.newThread(agentId, command);
      if (!replace && command === "/sessions") return this.loadHistory();
      if (!replace && command === "/compact") return this.compactThread();
      if (!retryRunId && !replace) return this.enqueueMessage(message, mentions);
      if (replace && this.latestTurn(scope)?.runId !== replace.runId) {
        this.problem({ error: "当前轮次已变化，回答和待处理消息结束后再编辑或重新生成" }, scope, "send"); return;
      }
      if (!id(scope.agentId) || this.submitPending || this.activeRunId()) return;
      if (!this.catalogReady || !this.catalog.agents.some(agent => agent.id === agentId && agent.enabled)) {
        this.problem({ error: "模板未启用" }, scope, "catalog"); return;
      }
      if (!scope.threadId || !this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId).id) {
        // No accepted request exists yet. Keep this unsent question if the user leaves during the initial read.
        const preparingKey = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
        const preparing = { account: scope.account, user: scope.user, agentId: scope.agentId, threadId: scope.threadId,
          message, mentions, requestId: nonce("req_"), createdAtMs: Date.now(), delivery: "pending" };
        this.pendingRequests[preparingKey] = preparing;
        await this.loadThread(scope);
        if (scope.account !== this.currentAccount || scope.user !== this.currentUser || scope.agentId !== this.activeAgentId) {
          this.failPending(scope, preparing); return;
        }
        const next = this.captureScope();
        if (!next.threadId || !this.getCachedThread(next.account, next.user, next.agentId, next.threadId).id) {
          this.failPending(scope, preparing, { error: "会话暂时不可用，问题已保留，请重试" });
          this.syncThreadToFrame(this.projectionForScope(next).thread, null, next);
          return;
        }
        delete this.pendingRequests[preparingKey];
        scope = next;
      }
      const current = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      if (current.readonly || current.deleteState || this.pendingCompacts.has(this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)) ||
        ["running", "unknown"].includes(current.activeOperation?.state)) {
        this.problem({ error: current.readonlyReason || "当前会话暂不可提问" }, scope, "load"); return;
      }
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      if (this.pendingSubmits.has(key)) return;
      this.pendingSubmits.add(key); this.submitPending = true; this.clearProblem("send", scope);
      let pending = this.pendingRequests[key];
      if (!pending || pending.message !== message || pending.threadId !== scope.threadId || pending.retryRunId !== (retryRunId || undefined) ||
        pending.replace?.mode !== replace?.mode || pending.replace?.runId !== replace?.runId || !/^req_[0-9a-f]{32}$/.test(pending.requestId)) {
        pending = { account: scope.account, user: scope.user, agentId: scope.agentId, threadId: scope.threadId,
          message, requestId: nonce("req_"), thinking: this.thinkingEnabled, createdAtMs: Date.now(), ...(retryRunId ? { retryRunId } : {}), ...(replace ? { replace } : {}) };
      }
      if (mentions.length) pending.mentions = mentions;
      pending.delivery = "pending"; this.pendingRequests[key] = pending;
      const previousRun = this.projectionForScope(scope).run;
      // Retrying/regenerating preserves the next draft; submitting an edit clears only its own text.
      if (pending.replace?.mode === "edit" && this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId) === message)
        this.setDraft(scope.account, scope.user, scope.agentId, "", scope.threadId);
      this.syncThreadToFrame(this.projectionForScope(scope).thread, previousRun, scope);
      this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: true }, scope);
      const result = await this.call("run.start", { agentId: scope.agentId, threadId: scope.threadId,
        message, requestId: pending.requestId, thinking: pending.thinking ?? this.thinkingEnabled,
        ...(pending.mentions?.length ? { mentions: pending.mentions } : {}),
        ...(pending.retryRunId ? { retryRunId: pending.retryRunId } : {}),
        ...(pending.replace ? { [pending.replace.mode === "edit" ? "editRunId" : "regenerateRunId"]: pending.replace.runId } : {}) }, scope);
      this.pendingSubmits.delete(key);
      if (this.clearedAccounts.has(scope.account)) return;
      const accepted = id(result.run?.id) && result.run.threadId === scope.threadId;
      if (accepted) this.acknowledgePending(scope, pending, result.run); else this.failPending(scope, pending, result);
      // Friends picked with @ show above the input as soon as the question is accepted.
      if (accepted && Array.isArray(result.participants) && result.participants.every(item => object(item) && id(item.user) && text(item.name, 240))) {
        const joined = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
        if (joined.id === scope.threadId) { joined.participants = result.participants; this.setCachedThread(scope.account, scope.user, scope.agentId, joined); }
      }
      // The original discussion retains the edit result even if its view has changed meanwhile.
      if (pending.replace?.mode === "edit") this.postToFrame("ADVISOR_EDIT_RESULT", {
        runId: pending.replace.runId, ok: accepted, error: accepted ? "" : errorText(result) }, scope);
      const view = this.captureScope();
      if (view.account !== scope.account || view.user !== scope.user || view.agentId !== scope.agentId || view.threadId !== scope.threadId) return;
      this.submitPending = false; this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: false }, view);
      this.syncThreadToFrame(this.projectionForScope(view).thread, accepted ? result.run : previousRun, view);
      if (!accepted) { this.problem(result, view, "send"); return; }
      if (!TERMINAL.has(result.run.state)) void this.pollEvents(result.run.id, view);
      else await this.loadThread(view);
    },
    applyEvent(thread, event, runId, buffers) {
      if (event.type === "text") {
        const messageId = id(event.messageId) ? event.messageId : `${runId}:assistant`;
        const content = (buffers[messageId] || "") + (event.text || ""); buffers[messageId] = content;
        let message = thread.messages.find(item => item.id === messageId);
        if (!message) { message = { id: messageId, runId, role: "assistant", text: "", createdAtMs: Date.now() }; thread.messages.push(message); }
        if (!message.text.startsWith(content) || content.length >= message.text.length) message.text = content;
        return message;
      }
      return null;
    },
    async pollEvents(runId, scope = this.captureScope()) {
      if (!this.isCurrent(scope)) return;
      this.abortCurrentPoll();
      const controller = new AbortController(); this.pollAbortController = controller; this.currentRunId = runId;
      let after = 0; const buffers = {};
      let phaseEvent = this.threadProjection?.run?.phaseEvent || null;
      while (!controller.signal.aborted && this.isCurrent(scope) && this.currentRunId === runId) {
        const data = await this.call("run.events", { runId, after }, scope);
        if (controller.signal.aborted || !this.isCurrent(scope) || this.currentRunId !== runId) return;
        if (!Array.isArray(data?.events) || data.run?.id !== runId) { this.pollError = true; this.problem(data, scope, "poll"); return; }
        this.clearProblem("poll", scope);
        let thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
        const previousQuestion = thread.messages.find(message => message.id === data.run.messageId && message.role === "user");
        const previousContext = JSON.stringify([previousQuestion?.contextSnapshot, previousQuestion?._contextPending]);
        const changedMessages = new Map();
        for (const rawEvent of data.events) {
          if (!object(rawEvent) || !Number.isSafeInteger(rawEvent.seq) || rawEvent.seq <= after ||
            !["status", "text", "reasoning", "skill", "done", "error"].includes(rawEvent.type) ||
            (rawEvent.text !== undefined && !text(rawEvent.text, 2000000)) ||
            (rawEvent.state !== undefined && !text(rawEvent.state, 60)) ||
            (rawEvent.next !== undefined && (!Number.isSafeInteger(rawEvent.next) || rawEvent.next < 0)) ||
            (rawEvent.attempt !== undefined && (!Number.isSafeInteger(rawEvent.attempt) || rawEvent.attempt < 0))) continue;
          const event = Object.fromEntries(Object.entries(rawEvent).filter(([key]) =>
            ["seq", "type", "text", "state", "messageId", "skillId", "next", "attempt"].includes(key)));
          after = event.seq;
          const changed = this.applyEvent(thread, event, runId, buffers);
          if (changed) {
            changedMessages.set(changed.id, changed);
            phaseEvent = { state: "answering" };
          } else if (event.type === "reasoning" && event.text?.trim()) phaseEvent = { state: "thinking" };
          else if (event.type === "status") phaseEvent = event;
          else if (event.type === "error") phaseEvent = { state: "error", text: event.text || "生成失败" };
          else if (event.type === "skill") phaseEvent = { state: "thinking", text: `正在使用技能 · ${event.skillId || event.text || ""}` };
        }
        if (!data.events.length && data.run.phase && phaseEvent?.state !== data.run.phase)
          phaseEvent = { state: data.run.phase };
        const previousRun = this.threadProjection?.run;
        const run = { ...data.run, ...(phaseEvent ? { phaseEvent } : {}) };
        const runSignature = value => JSON.stringify([value?.state, value?.phase, value?.error,
          value?.phaseEvent?.state, value?.phaseEvent?.text, value?.phaseEvent?.next, value?.phaseEvent?.attempt]);
        const runChanged = runSignature(run) !== runSignature(previousRun);
        if (validThread(data.thread, scope)) thread = this.rememberProjection(scope, data);
        const question = thread.messages.find(message => message.id === run.messageId && message.role === "user");
        if (question && object(run.contextSnapshot)) {
          question.contextSnapshot = run.contextSnapshot; delete question._contextPending;
        }
        if (question && previousContext !== JSON.stringify([question.contextSnapshot, question._contextPending]))
          changedMessages.set(question.id, question);
        thread._advisorRun = run;
        if (changedMessages.size || runChanged || TERMINAL.has(run.state)) this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
        this.threadProjection = { scope, thread, run };
        if (TERMINAL.has(run.state)) {
          this.currentRunId = null; this.stopPending = false;
          this.syncThreadToFrame(thread, run, scope);
          if (!validThread(data.thread, scope)) void this.loadThread(scope);
          return;
        }
        if (changedMessages.size || runChanged) this.postToFrame("ADVISOR_STREAM_UPDATE", {
          threadId: thread.id, run, messages: [...changedMessages.values()]
        }, scope);
        await new Promise(resolve => setTimeout(resolve, data.events.length ? 50 : 200));
      }
    },
    async stopRun() {
      const scope = this.captureScope(), runId = this.activeRunId();
      if (!runId || !this.isCurrent(scope) || this.stopPending) return;
      this.currentRunId = runId;
      this.stopPending = true; this.postToFrame("ADVISOR_STOP_STATE", { pending: true }, scope);
      const result = await this.call("run.stop", { runId }, scope);
      if (!this.isCurrent(scope) || this.currentRunId !== runId) return;
      this.stopPending = false; this.postToFrame("ADVISOR_STOP_STATE", { pending: false }, scope);
      if (result?.run?.id !== runId) { this.problem(result, scope, "poll"); return; }
      if (TERMINAL.has(result.run.state)) {
        const thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
        thread._advisorRun = { ...this.projectionForScope(scope).run, ...result.run };
        this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
        this.abortCurrentPoll(); this.syncThreadToFrame(thread, thread._advisorRun, scope);
        await this.loadThread(scope);
      } else if (this.pollError) void this.pollEvents(runId, scope);
    },
    // thread.new / select / rename / delete / compact and operation.stop: the friend comes from the host.
    postSession(method, scope, extra = {}) {
      return this.call(method, { agentId: scope.agentId, ...(scope.threadId ? { threadId: scope.threadId } : {}), ...extra }, scope);
    },
    actionId(scope, kind) {
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId) + ":" + kind;
      if (!this.sessionActions[key]) this.sessionActions[key] = nonce("req_");
      return this.sessionActions[key];
    },
    finishAction(scope, kind) {
      delete this.sessionActions[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId) + ":" + kind];
    },
    async newThread(agentId = this.activeAgentId, command = "") {
      const scope = this.captureScope();
      const key = this.baseKey(scope.account, scope.user, scope.agentId);
      if (!scope.account || !scope.user || !agentId || agentId !== scope.agentId || this.pendingNew.has(key) || this.historyReturn) return;
      if (!this.getEnabledTemplates().some(agent => agent.id === agentId)) return;
      this.pendingNew.add(key);
      await this.flushDraft(scope);
      if (!this.isCurrent(scope)) { this.pendingNew.delete(key); return; }
      const draft = this.draftRecords[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)];
      // An unsaved local draft must not look like an empty reusable thread to the server.
      if (draft?.dirty || draft?.conflict) {
        this.pendingNew.delete(key);
        this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: false }, scope);
        this.problem({ error: "草稿尚未保存，请先重试保存后再新建会话。" }, scope, "draft");
        if (command) this.postToFrame("ADVISOR_COMMAND_FAILED", { command }, scope);
        return;
      }
      const result = await this.call("thread.new", { agentId: scope.agentId, requestId: this.actionId(scope, "new") }, scope);
      this.pendingNew.delete(key);
      if (!validThread(result?.thread, scope)) {
        if (this.isCurrent(scope)) {
          this.postToFrame("ADVISOR_SUBMIT_STATE", { pending: false }, scope); this.problem(result, scope, "new");
          if (command) this.postToFrame("ADVISOR_COMMAND_FAILED", { command }, scope);
        }
        return;
      }
      this.finishAction(scope, "new");
      this.rememberProjection(scope, result);
      if (!this.isCurrent(scope)) return;
      this.closeHistory();
      this.clearProblem(null, scope); this.adoptThread(result, scope);
      this.postToFrame("ADVISOR_HISTORY", { open: false, threads: [], loading: false, error: "" });
    },
    closeHistory() {
      this.historyOpen = false; clearTimeout(this.historyTimer); this.historyTimer = null; this.historySerial++;
    },
    async loadHistory(refresh = false) {
      const scope = this.captureScope(), serial = ++this.historySerial;
      if (!scope.account || !scope.user) return;
      clearTimeout(this.historyTimer); this.historyTimer = null; this.historyOpen = true;
      if (!refresh) this.postToFrame("ADVISOR_HISTORY", { open: true, threads: [], loading: true, error: "" }, scope);
      const result = await this.call("threads.list", scope.agentId ? { agentId: scope.agentId } : {}, scope);
      if (!this.isCurrent(scope) || serial !== this.historySerial) return;
      if (!Array.isArray(result.threads) || !result.threads.every(item => object(item) && id(item.id) && id(item.agentId) &&
        text(item.title || "", 240) && text(item.preview || "", 2000))) {
        this.postToFrame("ADVISOR_HISTORY", { open: true, threads: [], loading: false, error: errorText(result) }, scope); return;
      }
      if (refresh) {
        const remaining = new Map(result.threads.map(item => [item.id, item]));
        const stable = this.historyItems.flatMap(item => { const next = remaining.get(item.id); remaining.delete(item.id); return next ? [next] : []; });
        this.historyItems = [...stable, ...remaining.values()];
      } else this.historyItems = result.threads;
      this.postToFrame("ADVISOR_HISTORY", { open: true, threads: this.historyItems, loading: false, error: "" }, scope);
      this.historyTimer = setTimeout(() => { if (this.historyOpen && this.isCurrent(scope)) void this.loadHistory(true); }, 3000);
    },
    async selectThread(threadId, agentId = this.activeAgentId) {
      const scope = this.captureScope(), row = this.historyItems.find(item => item.id === threadId && item.agentId === agentId);
      if (!row || row.deleteState || this.selectPending) return;
      this.selectPending = true; await this.flushDraft(scope);
      if (!this.isCurrent(scope)) { this.selectPending = false; return; }
      const target = { ...scope, agentId, threadId };
      const result = await this.postSession("thread.select", target);
      this.selectPending = false;
      if (!this.isCurrent(scope)) return;
      if (!validThread(result?.thread, target)) { this.problem(result, scope, "history"); return; }
      if (result.thread.readonly && !this.historyReturn) this.historyReturn = { agentId: scope.agentId, threadId: scope.threadId };
      else if (!result.thread.readonly) this.historyReturn = null;
      this.generation++; this.abortCurrentPoll(); this.activeAgentId = agentId; this.currentThreadId = threadId;
      this.closeHistory();
      this.adoptThread(result, this.captureScope());
      this.postToFrame("ADVISOR_HISTORY", { open: false, threads: [], loading: false, error: "" });
    },
    async returnFromHistory() {
      if (!this.historyReturn) return;
      const target = this.historyReturn; this.historyReturn = null;
      this.generation++; this.abortCurrentPoll(); this.activeAgentId = target.agentId; this.currentThreadId = target.threadId;
      this.syncTemplatesToFrame(); await this.loadThread();
    },
    async renameThread(threadId, agentId, title) {
      const scope = this.captureScope(), row = this.historyItems.find(item => item.id === threadId && item.agentId === agentId);
      if (!row || !text(title, 120) || !title.trim() || row.deleteState) return;
      const target = { ...scope, threadId, agentId };
      const result = await this.postSession("thread.rename", target, { title: title.trim(), requestId: nonce("req_") });
      if (!this.isCurrent(scope)) return;
      if (!validThread(result?.thread, target)) { this.problem(result, scope, "history"); return; }
      this.rememberProjection(target, result);
      if (scope.threadId === threadId) this.syncThreadToFrame(this.getCachedThread(scope.account, scope.user, agentId, threadId), result.run, scope);
      await this.loadHistory();
    },
    async deleteThread(threadId, agentId) {
      const scope = this.captureScope(), row = this.historyItems.find(item => item.id === threadId && item.agentId === agentId);
      if (!row || (!row.deleteState && ["running", "queued", "starting", "compacting", "unknown"].includes(row.state))) return;
      const target = { ...scope, threadId, agentId }, key = this.scopeKey(scope.account, scope.user, agentId, threadId);
      clearTimeout(this.draftTimers[key]); delete this.draftTimers[key];
      const result = await this.postSession("thread.delete", target, { requestId: this.actionId(target, "delete") });
      if (result.deleted === true) {
        this.finishAction(target, "delete"); delete this.threadCache[key]; delete this.draftRecords[key];
        delete this.pendingEnqueues[key]; delete this.queueProblems[key];
        delete this.pendingRequests[key]; delete this.sessionProblems[key];
        if (this.selectedThreads[this.baseKey(scope.account, scope.user, agentId)] === threadId) delete this.selectedThreads[this.baseKey(scope.account, scope.user, agentId)];
        if (!this.isCurrent(scope)) return;
        if (scope.threadId === threadId) {
          this.generation++; this.abortCurrentPoll(); this.currentThreadId = "";
          if (this.historyReturn) await this.returnFromHistory(); else await this.loadThread();
        }
      } else if (this.isCurrent(scope)) {
        if (["deleting", "pending", "delete_failed", "failed"].includes(result.state)) {
          const cached = this.threadCache[key];
          if (cached) { cached.deleteState = result.state; cached.readonly = true; cached.readonlyReason = "删除未完成，可重试"; }
          if (scope.threadId === threadId && cached) this.syncThreadToFrame(cached, null, scope);
        }
        this.problem(result.error ? result : { error: "删除未完成，可从历史列表重试" }, scope, "history");
      }
      if (this.currentAccount === scope.account && this.currentUser === scope.user) await this.loadHistory();
    },
    async loadEarlier() {
      const scope = this.captureScope(), cached = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      if (!cached.hasMore || this.earlierPending) return;
      this.earlierPending = true;
      const result = await this.call("thread.get", { agentId: scope.agentId, threadId: scope.threadId, beforeSeq: cached.beforeSeq }, scope);
      this.earlierPending = false;
      if (!this.isCurrent(scope)) return;
      if (!validThread(result.thread, scope) || result.thread.id !== scope.threadId) { this.problem(result, scope, "earlier"); return; }
      const messages = new Map(result.thread.messages.map(message => [message.id, message]));
      for (const message of cached.messages) messages.set(message.id, message);
      cached.messages = [...messages.values()]; cached._olderLoaded = true;
      cached.hasMore = result.thread.hasMore; cached.beforeSeq = result.thread.beforeSeq;
      this.setCachedThread(scope.account, scope.user, scope.agentId, cached);
      this.postToFrame("ADVISOR_EARLIER", { thread: cached }, scope);
    },
    async compactThread() {
      const scope = this.captureScope(), thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      const key = this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId);
      if (thread.readonly || thread.deleteState) { this.problem({ error: thread.readonlyReason || "只读会话不可压缩" }, scope, "load"); return; }
      if (this.activeRunId() || this.submitPending) { this.problem({ error: "回答完成后可压缩" }, scope, "compact"); return; }
      if (!scope.threadId) { this.problem({ error: "当前无需压缩" }, scope, "compact"); return; }
      if (this.pendingCompacts.has(key)) return;
      if (["running", "unknown"].includes(thread.activeOperation?.state)) { this.pollOperation(thread.activeOperation, scope); return; }
      this.pendingCompacts.add(key);
      this.syncThreadToFrame(thread, thread._advisorRun || null, scope);
      const uncertainRequestId = this.sessionActions[key + ":compact"];
      const requestId = this.actionId(scope, "compact");
      let result;
      if (uncertainRequestId) {
        // A lost acknowledgement may still have started the operation: ask by the original request id first.
        result = await this.call("operation.get", { threadId: scope.threadId, requestId }, scope);
        if (!id(result.operation?.id) && result.code !== "not-found") {
          this.pendingCompacts.delete(key); this.syncThreadToFrame(thread, thread._advisorRun || null, scope);
          this.problem({ error: "暂时无法核对上次压缩，请稍后重试。" }, scope, "compact"); return;
        }
      }
      if (!id(result?.operation?.id)) result = await this.postSession("thread.compact", scope, { requestId });
      this.pendingCompacts.delete(key);
      if (this.clearedAccounts.has(scope.account)) return;
      if (!id(result.operation?.id)) { this.syncThreadToFrame(thread, thread._advisorRun || null, scope); this.problem(result, scope, "compact"); return; }
      thread.activeOperation = result.operation; this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
      // An uncertain operation keeps its id until reconciliation has confirmed a terminal result.
      if (!["running", "unknown"].includes(result.operation.state)) this.finishAction(scope, "compact");
      if (!this.isCurrent(scope)) return;
      this.clearProblem("compact", scope); this.syncThreadToFrame(thread, thread._advisorRun || null, scope);
      this.pollOperation(result.operation, scope);
    },
    pollOperation(operation, scope = this.captureScope()) {
      clearTimeout(this.operationTimer); this.operationTimer = null;
      if (!this.isCurrent(scope)) return;
      if (!["running", "unknown"].includes(operation.state)) { void this.loadThread(scope); return; }
      this.operationTimer = setTimeout(async () => {
        if (!this.isCurrent(scope)) return;
        const result = await this.call("operation.get", { threadId: scope.threadId, operationId: operation.id }, scope);
        if (!this.isCurrent(scope)) return;
        if (!id(result.operation?.id) || result.operation.id !== operation.id) {
          this.problem({ error: "暂时无法核对压缩状态，请重试查询。" }, scope, "operation"); return;
        }
        this.clearProblem("operation", scope);
        const thread = validThread(result.thread, scope) ? this.rememberProjection(scope, result) : this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
        thread.activeOperation = result.operation;
        this.setCachedThread(scope.account, scope.user, scope.agentId, thread);
        this.syncThreadToFrame(thread, thread._advisorRun || null, scope);
        if (!["running", "unknown"].includes(result.operation.state)) this.finishAction(scope, "compact");
        this.pollOperation(result.operation, scope);
      }, 1000);
    },
    async stopOperation() {
      const scope = this.captureScope(), thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      const operation = thread.activeOperation;
      if (!id(operation?.id) || !["running", "unknown"].includes(operation.state) || this.stopPending) return;
      this.stopPending = true; this.postToFrame("ADVISOR_STOP_STATE", { pending: true }, scope);
      const result = await this.call("operation.stop", { threadId: scope.threadId, operationId: operation.id }, scope);
      if (!this.isCurrent(scope)) return;
      this.stopPending = false; this.postToFrame("ADVISOR_STOP_STATE", { pending: false }, scope);
      if (result.operation?.id !== operation.id) { this.problem(result, scope, "operation"); return; }
      thread.activeOperation = result.operation; this.syncThreadToFrame(thread, thread._advisorRun || null, scope);
      this.pollOperation(result.operation, scope);
    },
    // The discussion's latest turn while nothing runs or waits: its run, question and (when done) answer.
    latestTurn(scope = this.captureScope()) {
      const { thread, run } = this.projectionForScope(scope);
      if (!id(run?.id) || run.threadId !== thread.id || !["done", "error", "stopped"].includes(run.state) || thread.readonly ||
        thread.deleteState || this.activeRunId() || this.submitPending || (thread.queue?.items?.length || ["running", "unknown"].includes(thread.activeOperation?.state) || thread.compactPending || this.pendingCompacts.has(this.queueKey(scope)))) return null;
      const current = thread.messages.filter(item => !item.supersededBy && Number.isSafeInteger(item.seq));
      const question = current.filter(item => item.role === "user").at(-1);
      if (!question) return null;
      const answer = run.state === "done" ? current.filter(item => item.role === "assistant" && item.runId === run.id && item.seq > question.seq).at(-1) : null;
      return { runId: run.id, question, answer: answer || null };
    },
    retryRun() {
      const runId = this.activeRunId();
      const scope = this.captureScope(), pending = this.pendingRequests[this.scopeKey(scope.account, scope.user, scope.agentId, scope.threadId)];
      if (runId) { void this.pollEvents(runId); return; }
      const { thread, run } = this.projectionForScope(scope);
      const previous = thread.messages.filter(item => item.role === "user").at(-1);
      return this.startRun(pending?.message || previous?.text || this.getDraft(scope.account, scope.user, scope.agentId, scope.threadId),
        scope.agentId, pending?.retryRunId || (!pending && ["error", "interrupted", "stopped"].includes(run?.state) ? run.id : null), pending?.mentions || [], pending?.replace || null);
    },
    // Friends the current friend's discussion can @: the host names the friend, Core lists the added chats.
    async loadFriends() {
      const scope = this.captureScope();
      if (!scope.account || !scope.user) return;
      const result = await this.call("friends.list", {}, scope);
      if (!this.isCurrent(scope)) return;
      const valid = Array.isArray(result.friends) && result.friends.every(item => object(item) && id(item.user) && text(item.name, 240));
      this.postToFrame("ADVISOR_FRIENDS", valid ? { friends: result.friends, error: "" } : { friends: [], error: errorText(result) }, scope);
    },
    async removeParticipant(user) {
      const scope = this.captureScope(), thread = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      if (!scope.threadId || thread.readonly || thread.deleteState) return;
      const result = await this.postSession("thread.removeParticipant", scope, { participant: user });
      if (this.clearedAccounts.has(scope.account)) return;
      if (!validThread(result?.thread, scope) || result.thread.id !== scope.threadId || !Array.isArray(result.thread.participants)) {
        this.problem(result, scope, "load"); return;
      }
      // Only the participants change: a streaming answer keeps its messages and run.
      const cached = this.getCachedThread(scope.account, scope.user, scope.agentId, scope.threadId);
      cached.participants = result.thread.participants;
      this.setCachedThread(scope.account, scope.user, scope.agentId, cached);
      if (this.isCurrent(scope)) this.syncThreadToFrame(cached, this.projectionForScope(scope).run, scope);
    },
    // Modified by Know Your Friends: bring a source quote into a draft, never send it automatically.
    // The host opens the panel first and then delivers a "quote" event for the friend it shows.
    quoteMessage(value) {
      const account = this.currentAccount, user = this.currentUser;
      if (!object(value) || value.account !== account || value.user !== user || !account || !user ||
          !text(value.messageId, 4096) || !value.messageId || !text(value.text, 16000) || this.clearedAccounts.has(account)) return false;
      const scope = this.captureScope(), projection = this.projectionForScope(scope);
      // A panel just opened or switched to this friend still reads the catalog or the discussion, whose saved
      // draft would replace the quote: it waits for that discussion (adoptThread).
      if (!this.catalogReady || (scope.agentId && !projection.thread.id)) { this.pendingQuote = value; return true; }
      if (!scope.agentId || projection.thread.readonly || this.historyReturn) return false;
      const existing = this.getDraft(account, user, scope.agentId, scope.threadId);
      // Modified 2026-10-15: a message from the chat's right-click menu may be the user's own or a group member's.
      const author = value.side === "self" ? "我发的" : text(value.sender, 128) && value.sender.trim() ? `${value.sender.trim()}的` : "对方";
      const quote = `关于${author}这条消息（${value.messageId}）：\n${value.text.trim() || "[媒体消息，请结合原消息查看]"}\n\n我的问题：`;
      const draft = existing ? `${existing}\n\n${quote}` : quote;
      if (draft.length > 16000) return false;
      this.setDraft(account, user, scope.agentId, draft, scope.threadId);
      this.syncThreadToFrame(projection.thread, projection.run, scope);
      return true;
    },
    onSessionChanged(account, user, name = "") {
      account = typeof account === "string" ? account : ""; user = typeof user === "string" ? user : "";
      name = text(name, 240) ? name.trim() : "";
      if (this.currentAccount === account && this.currentUser === user) {
        if (this.currentName !== name) {
          this.currentName = name;
          const projection = this.projectionForScope(); this.syncThreadToFrame(projection.thread, projection.run);
        }
        return;
      }
      void this.flushDraft();
      this.closeHistory(); this.pendingQuote = null;
      this.generation++; this.abortCurrentPoll(); this.stopQueuePolling(); this.submitPending = false;
      this.currentThreadId = this.selectedThreads[this.baseKey(account, user, this.activeAgentId)] || ""; this.historyReturn = null; this.historyItems = [];
      this.currentAccount = account; this.currentUser = user; this.currentName = name;
      this.syncTemplatesToFrame(); void this.loadThread(this.captureScope(), true);
    },
    // Core already stopped the plugin's work for a cleared account; only this page's memory is dropped.
    onAccountCleared(account) {
      if (!text(account, 240) || !account) return;
      this.clearedAccounts.add(account);
      const oldScope = this.captureScope();
      if (oldScope.account === account) {
        this.generation++; this.abortCurrentPoll(); this.stopQueuePolling(); this.submitPending = false;
        this.currentAccount = ""; this.currentUser = ""; this.currentName = ""; this.currentThreadId = ""; this.historyReturn = null; this.historyItems = [];
        this.closeHistory();
      }
      if (this.threadProjection?.scope.account === account) this.threadProjection = null;
      if (this.lastProblem?.scope.account === account) this.lastProblem = null;
      if (this.pendingQuote?.account === account) this.pendingQuote = null;
      for (const data of [this.threadCache, this.pendingRequests, this.pendingEnqueues, this.queueProblems, this.queueThreadVersions, this.sessionActions, this.draftRecords, this.selectedThreads, this.sessionProblems]) {
        for (const key of Object.keys(data)) {
          let matches = data[key]?.account === account;
          try { matches ||= JSON.parse(key.replace(/:(?:new|compact|delete)$/, ""))[0] === account; } catch {}
          if (matches) { clearTimeout(this.draftTimers[key]); delete data[key]; }
        }
      }
      if (oldScope.account === account) this.syncThreadToFrame(this.getCachedThread("", "", this.activeAgentId));
    },
    setVisible(visible) {
      if (typeof visible !== "boolean" || visible === this.panelVisible) return;
      this.panelVisible = visible;
      if (!visible) { this.hideCurrentRun(); return; }
      // The catalog may have changed in 助手仓库 while the panel was closed.
      void this.refresh();
    },
    async refresh() {
      const before = this.activeAgentId;
      await this.loadCatalog();
      if (this.activeAgentId === before) await this.loadThread();
    },
    initFrameHandshake() {
      const projection = this.projectionForScope();
      this.seedContactDraft(projection.thread, this.captureScope());
      this.postToFrame("ADVISOR_INIT", { ...this.templatesPayload(),
        thread: projection.thread, run: projection.run,
        draft: projection.thread.readonly ? "" : this.getDraft(this.currentAccount, this.currentUser, this.activeAgentId, this.currentThreadId),
        draftMentions: projection.thread.readonly ? [] : this.draftRecords[this.scopeKey(this.currentAccount, this.currentUser, this.activeAgentId, this.currentThreadId)]?.mentions || [],
        currentContact: this.currentContact(),
        scrollTop: this.draftRecords[this.scopeKey(this.currentAccount, this.currentUser, this.activeAgentId, this.currentThreadId)]?.scrollTop ?? null,
        canReturn: !!this.historyReturn, thinking: this.thinkingEnabled, theme: this.theme });
    },
    validFrameRequest(data) {
      if (!object(data) || !object(data.payload)) return false;
      if (Object.keys(data).some(key => !["type", "payload", "scope"].includes(key))) return false;
      const allowed = { ADVISOR_FRAME_READY: [], ADVISOR_SWITCH_TEMPLATE: ["agentId"], ADVISOR_SEND: ["text", "agentId", "mentions", "editRunId"], ADVISOR_REGENERATE: [],
        ADVISOR_FRIENDS_REQUEST: [], ADVISOR_REMOVE_PARTICIPANT: ["user"],
        ADVISOR_QUEUE_PAUSE: [], ADVISOR_QUEUE_RESUME: [], ADVISOR_QUEUE_RETRY: [],
        ADVISOR_QUEUE_EDIT: ["itemId", "version", "message"], ADVISOR_QUEUE_REMOVE: ["itemId", "version"],
        ADVISOR_STOP: [], ADVISOR_RETRY: ["retryKind"], ADVISOR_SET_THINKING: ["enabled"],
        ADVISOR_DRAFT_UPDATE: ["draft", "agentId", "scrollTop", "mentions"], ADVISOR_COPY: ["text"],
        ADVISOR_HISTORY_OPEN: [], ADVISOR_HISTORY_CLOSE: [], ADVISOR_NEW_THREAD: [], ADVISOR_COMPACT: [], ADVISOR_STOP_OPERATION: [],
        ADVISOR_SELECT_THREAD: ["threadId", "agentId"], ADVISOR_RENAME_THREAD: ["threadId", "agentId", "title"],
        ADVISOR_DELETE_THREAD: ["threadId", "agentId"], ADVISOR_RETURN_HISTORY: [], ADVISOR_LOAD_EARLIER: [] };
      if (!Object.prototype.hasOwnProperty.call(allowed, data.type) ||
        Object.keys(data.payload).some(key => !allowed[data.type].includes(key))) return false;
      if (data.type === "ADVISOR_FRAME_READY") return true;
      if (!object(data.scope) || !this.isCurrent(data.scope) ||
        data.scope.threadId !== this.currentThreadId ||
        Object.keys(data.scope).some(key => !["account", "user", "agentId", "threadId", "generation"].includes(key))) return false;
      const historyAction = ["ADVISOR_SELECT_THREAD", "ADVISOR_RENAME_THREAD", "ADVISOR_DELETE_THREAD"].includes(data.type);
      if (data.payload.agentId !== undefined && (!id(data.payload.agentId) ||
        (data.type !== "ADVISOR_SWITCH_TEMPLATE" && !historyAction && data.payload.agentId !== this.activeAgentId))) return false;
      if (historyAction && (!id(data.payload.threadId) || !this.historyItems.some(item =>
        item.id === data.payload.threadId && item.agentId === data.payload.agentId))) return false;
      if (data.type === "ADVISOR_RENAME_THREAD" && (!text(data.payload.title, 120) || !data.payload.title.trim())) return false;
      if (data.type === "ADVISOR_SWITCH_TEMPLATE") return this.getEnabledTemplates().some(agent => agent.id === data.payload.agentId);
      // Only the latest finished turn of the current discussion can be asked again.
      if (data.payload.editRunId !== undefined && data.payload.editRunId !== this.latestTurn()?.runId) return false;
      if (data.type === "ADVISOR_REGENERATE") return !!this.latestTurn()?.answer;
      if (data.type === "ADVISOR_SEND") return text(data.payload.text, 16000) && data.payload.text.trim().length > 0 &&
        (data.payload.mentions === undefined || (Array.isArray(data.payload.mentions) && data.payload.mentions.length <= 8000 &&
          data.payload.mentions.every(id)));
      if (data.type === "ADVISOR_REMOVE_PARTICIPANT") return id(data.payload.user);
      if (["ADVISOR_QUEUE_EDIT", "ADVISOR_QUEUE_REMOVE"].includes(data.type)) {
        const item = this.getCachedThread(this.currentAccount, this.currentUser, this.activeAgentId, this.currentThreadId).queue?.items?.find(item => item.id === data.payload.itemId);
        return !!item && item.state === "pending" && Number.isSafeInteger(data.payload.version) && data.payload.version >= 0 &&
          (data.type !== "ADVISOR_QUEUE_EDIT" || (text(data.payload.message, 16000) && data.payload.message.trim().length > 0));
      }
      if (data.type === "ADVISOR_DRAFT_UPDATE") return text(data.payload.draft, 16000) &&
        (data.payload.mentions === undefined || validDraftMentions(data.payload.mentions)) &&
        (data.payload.scrollTop === undefined || (Number.isFinite(data.payload.scrollTop) && data.payload.scrollTop >= 0));
      if (data.type === "ADVISOR_COPY") return text(data.payload.text, 2000000);
      if (data.type === "ADVISOR_SET_THINKING") return typeof data.payload.enabled === "boolean";
      if (data.type === "ADVISOR_RETRY") return ["send", "poll", "load", "catalog", "draft", "new", "history", "earlier", "compact", "operation"].includes(data.payload.retryKind);
      return true;
    },
    // Actions from the conversation view (panel-frame.js).
    receive(data) {
      if (!this.validFrameRequest(data)) return;
      const { type, payload } = data;
      if (type === "ADVISOR_FRAME_READY") { this.frameReady = true; this.syncReadyFrame(); return; }
      if (type === "ADVISOR_SWITCH_TEMPLATE") {
        if (payload.agentId === this.activeAgentId) return;
        void this.flushDraft(); this.closeHistory(); this.historyReturn = null;
        this.generation++; this.abortCurrentPoll(); this.submitPending = false; this.activeAgentId = payload.agentId;
        this.currentThreadId = this.selectedThreads[this.baseKey(this.currentAccount, this.currentUser, payload.agentId)] || "";
        this.syncTemplatesToFrame(); void this.loadThread(this.captureScope(), true);
      } else if (type === "ADVISOR_SEND") void this.startRun(payload.text, payload.agentId, null, payload.mentions || [],
        payload.editRunId ? { mode: "edit", runId: payload.editRunId } : null);
      else if (type === "ADVISOR_REGENERATE") {
        const turn = this.latestTurn();
        void this.startRun(turn.question.text, this.activeAgentId, null, [], { mode: "regenerate", runId: turn.runId });
      }
      else if (type === "ADVISOR_FRIENDS_REQUEST") void this.loadFriends();
      else if (type === "ADVISOR_REMOVE_PARTICIPANT") void this.removeParticipant(payload.user);
      else if (type === "ADVISOR_QUEUE_PAUSE") void this.mutateQueue("pause");
      else if (type === "ADVISOR_QUEUE_RESUME") void this.mutateQueue("resume");
      else if (type === "ADVISOR_QUEUE_EDIT") void this.mutateQueue("edit", payload);
      else if (type === "ADVISOR_QUEUE_REMOVE") void this.mutateQueue("remove", payload);
      else if (type === "ADVISOR_QUEUE_RETRY") void this.retryQueue();
      else if (type === "ADVISOR_STOP") void this.stopRun();
      else if (type === "ADVISOR_SET_THINKING") this.thinkingEnabled = payload.enabled;
      else if (type === "ADVISOR_DRAFT_UPDATE") {
        if (!this.projectionForScope().thread.readonly) this.setDraft(this.currentAccount, this.currentUser, this.activeAgentId, payload.draft, this.currentThreadId, payload.scrollTop, payload.mentions);
      }
      else if (type === "ADVISOR_HISTORY_OPEN") void this.loadHistory();
      else if (type === "ADVISOR_HISTORY_CLOSE") this.closeHistory();
      else if (type === "ADVISOR_NEW_THREAD") void this.newThread();
      else if (type === "ADVISOR_COMPACT") void this.compactThread();
      else if (type === "ADVISOR_STOP_OPERATION") void this.stopOperation();
      else if (type === "ADVISOR_SELECT_THREAD") void this.selectThread(payload.threadId, payload.agentId);
      else if (type === "ADVISOR_RENAME_THREAD") void this.renameThread(payload.threadId, payload.agentId, payload.title);
      else if (type === "ADVISOR_DELETE_THREAD") void this.deleteThread(payload.threadId, payload.agentId);
      else if (type === "ADVISOR_RETURN_HISTORY") void this.returnFromHistory();
      else if (type === "ADVISOR_LOAD_EARLIER") void this.loadEarlier();
      else if (type === "ADVISOR_RETRY") {
        if (payload.retryKind === "catalog") void this.loadCatalog();
        else if (payload.retryKind === "load") void this.loadThread();
        else if (payload.retryKind === "draft") void this.flushDraft(this.captureScope(), true);
        else if (payload.retryKind === "new") void this.newThread();
        else if (payload.retryKind === "history") void this.loadHistory();
        else if (payload.retryKind === "earlier") void this.loadEarlier();
        else if (payload.retryKind === "compact") void this.compactThread();
        else if (payload.retryKind === "operation") void this.loadThread();
        else void this.retryRun();
      } else if (type === "ADVISOR_COPY") void this.copyText(payload.text);
    },
    async copyText(value) {
      const scope = this.captureScope();
      let copied = false;
      try { copied = (await window.kyfHost.host("clipboard.copy", { text: value }))?.copied === true; } catch { }
      if (this.isCurrent(scope)) this.postToFrame("ADVISOR_COPY_RESULT", { copied }, scope);
    },
    // Context and events pushed by the host (kyf-host.js).
    onContext(context) {
      if (!object(context)) return;
      const theme = context.theme === "light" ? "light" : "dark";
      if (theme !== this.theme) { this.theme = theme; this.postToFrame("ADVISOR_THEME", { theme }); }
      this.onSessionChanged(context.account, context.user, context.name);
    },
    acknowledgeAttention(event) {
      const scope = this.captureScope();
      if (!this.panelVisible || document.hidden || !this.frameReady || event.account !== scope.account || event.user !== scope.user ||
        !Array.isArray(event.tasks) || !this.isCurrent(scope)) return;
      const { thread, run } = this.projectionForScope(scope);
      if (!thread.id || thread.id !== scope.threadId) return;
      const displayed = new Set(thread.messages.filter(message => Number.isSafeInteger(message.seq) &&
        (message.role === "assistant" || message.role === "status" && message.status === "error"))
        .map(message => message.runId));
      if (run?.threadId === thread.id && TERMINAL.has(run.state)) displayed.add(run.id);
      const taskIds = event.tasks.filter(task => object(task) && id(task.id) && task.threadId === thread.id && displayed.has(task.runId)).map(task => task.id);
      if (taskIds.length) void window.kyfHost.host("attention.seen", { account: scope.account, user: scope.user, taskIds }).catch(() => {});
    },
    onEvent(event) {
      if (!object(event)) return;
      if (event.type === "account-cleared") this.onAccountCleared(event.account);
      else if (event.type === "visibility") this.setVisible(event.visible);
      else if (event.type === "quote") this.quoteMessage(event);
      else if (event.type === "attention-check") this.acknowledgeAttention(event);
    },
    async start() {
      const host = window.kyfHost;
      if (!host) return;
      host.on(message => {
        if (message.type === "context") this.onContext(message.context);
        else if (message.type === "event") this.onEvent(message.event);
      });
      await host.ready();
      // The first context can arrive while later scripts of the page still load, before the listener above.
      this.onContext(host.context());
      this.initFrameHandshake();
      await this.loadCatalog();
      await this.loadThread(this.captureScope(), true);
    },
  };
  window.AdvisorController = Advisor;
  void Advisor.start();
})();
