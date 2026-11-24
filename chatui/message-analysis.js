"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Saved per-message results are a read-only projection, never a source of chat facts.
// Modified 2026-10-13 (Apache-2.0): host slot for plugin message annotations. The provider is the
// enabled plugin that contributes messageAnnotations; data comes from its methods through Core RPC.
// Modified 2026-11-09 (Apache-2.0): results are per round. The friend's messages since the user's last
// reply form a round; its whole-round explanation and three reply plans (each one to five messages)
// hang under the round's last message: the explanation card on the friend's side, the plans on a row of their own on the user's side.
// Analysis and reply suggestions each have a switch beside 聊天助手.
(function () {
  let provider = null;
  async function rpc(method, params, captured, signal) {
    const response = await fetch("/api/plugins/rpc", { method: "POST", cache: "no-store", signal,
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ plugin: provider.id, method, params, account: captured.account, user: captured.user }) });
    const value = await response.json();
    return { ok: response.ok, status: response.status, value: response.ok ? value.result : value };
  }
  async function discover() {
    try {
      const response = await fetch("/api/plugins", { cache: "no-store" });
      const listing = response.ok ? await response.json() : null;
      const found = listing?.plugins?.find(item => item.enabled && !item.incompatible && !item.dependencyError && item.contributes?.messageAnnotations === true);
      const toggles = found?.contributes?.chatHeader?.filter(item => item.kind === "toggle") || [];
      const split = ["analysis", "suggestions"].every(part => toggles.some(toggle => toggle.id === part));
      provider = found ? { id: found.id, split, label: toggles[0]?.label || found.name } : null;
    } catch { provider = null; }
    const button = byId("btnMessageAnalysis");
    if (button && provider) button.textContent = provider.split ? "分析" : provider.label;
    return provider;
  }
  const states = new Set(["pending", "running", "ready", "partial", "error", "stale"]);
  const partStates = new Set(["none", "pending", "running", "ready", "error", "stale"]);
  const byId = id => document.getElementById(id);
  const node = (tag, className, text) => {
    const value = document.createElement(tag); value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  };
  const validId = value => typeof value === "string" && value.length > 0 && value.length <= 4096 && !/[\u0000-\u001f\u007f]/u.test(value);
  const pending = state => ["pending", "running"].includes(state);
  let scope = null, epoch = 0, serial = 0, controller = null, timer = null, copySerial = 0;
  let analysisEnabled = false, suggestionsEnabled = false;
  let ready = false, saving = false, posting = false, error = "", opened = "", queuedRefresh = false;
  let generationTarget = "", roundDueAt = 0, draftConfirm = "", draftTimer = null;
  let legacyAnalysisEnabled = false;
  // The chat the host asked for, kept while no provider plugin is enabled so re-enabling one restores the slot,
  // and bubbles rendered while the provider was still being discovered (attached once it is known).
  let requested = null, deferred = [];
  let messages = new Map(), items = new Map(), refs = new Map(), disclosures = new Set(), fingerprints = new Map(), observed = new Map(), generationErrors = new Map();
  // Rounds: last message id -> the round's message ids; ``latest`` is the round nothing follows yet.
  let rounds = new Map(), latest = "", collapsed = new Set(), plans = new Map();
  const fingerprint = message => JSON.stringify([message?.kind, message?.text, message?.media]);
  const current = captured => captured && scope && captured.epoch === epoch && captured.account === scope.account && captured.user === scope.user;
  const capture = () => scope ? { ...scope, epoch } : null;
  const showAnalysis = () => analysisEnabled || legacyAnalysisEnabled;
  const canView = () => showAnalysis() || suggestionsEnabled;
  const partEnabled = part => part === "analysis" ? analysisEnabled : suggestionsEnabled;
  const isOpen = id => id === latest ? !collapsed.has(id) : opened === id;

  function keepPosition(action) {
    const container = byId("chatMessages"), top = container?.scrollTop || 0;
    const bounds = container?.getBoundingClientRect();
    const anchor = bounds && [...container.querySelectorAll(".msg-item")].find(item => item.getBoundingClientRect().bottom > bounds.top);
    const offset = anchor?.getBoundingClientRect().top;
    action();
    if (container) container.scrollTop = anchor?.isConnected ? top + anchor.getBoundingClientRect().top - offset : top;
  }
  function paintToggle() {
    for (const [id, active, hint] of [
      ["btnMessageAnalysis", analysisEnabled, provider?.split ? "解读这位好友每一轮消息的意思" : "解读本轮消息并给出三种回复建议"],
      ["btnMessageSuggestions", suggestionsEnabled, "为这位好友的每一轮消息生成三种回复建议"],
    ]) {
      const toggle = byId(id);
      if (!toggle) continue;
      toggle.hidden = !scope || (id === "btnMessageSuggestions" && !provider?.split);
      toggle.disabled = toggle.hidden || !ready || saving;
      toggle.setAttribute("aria-checked", String(active));
      toggle.classList.toggle("active", active);
      toggle.title = saving ? "保存中…" : !ready ? "读取设置中…" : hint;
    }
    const status = byId("messageAnalysisStatus");
    if (status) {
      status.hidden = !scope || !error;
      status.textContent = "读取失败 · 重试";
      status.disabled = !error || !!controller;
    }
  }
  function refreshLater(delay = 3000) {
    clearTimeout(timer); timer = null;
    if (!scope || !canView() || document.hidden) return;
    timer = setTimeout(() => { timer = null; void refresh(); }, delay);
  }
  function requestIds() {
    return [...new Set([...(opened ? [opened] : []), ...[...rounds.keys()].reverse()])].slice(0, 240);
  }
  function validStatus(value, captured) {
    return value?.account === captured.account && value?.user === captured.user && typeof value.enabled === "boolean" &&
      (!provider.split || (typeof value.analysisEnabled === "boolean" && typeof value.suggestionsEnabled === "boolean")) &&
      Array.isArray(value.items) && value.items.every(item => validId(item?.messageId) && states.has(item.state) &&
        (item.analysisState === undefined || partStates.has(item.analysisState)) &&
        (item.suggestionsState === undefined || partStates.has(item.suggestionsState)));
  }
  const isObserved = id => fingerprints.has(id) && observed.get(id) === fingerprints.get(id);
  // sync may include rows with no annotation entry; those cannot acquire a fingerprint until mounted.
  const hasUnreadMessages = () => requestIds().some(id => refs.has(id) && fingerprints.has(id) && !items.has(id) && !isObserved(id));
  async function request(method = "GET", payload = {}, operation = "") {
    const captured = capture();
    if (!captured) return;
    if (controller && method === "GET") { queuedRefresh = true; return; }
    clearTimeout(timer); timer = null; controller?.abort();
    const ticket = ++serial, active = new AbortController(); controller = active; posting = method === "POST";
    const generating = operation === "retry" || operation === "generate";
    generationTarget = operation === "generate" ? payload.messageId : "";
    if (operation === "generate") generationErrors.delete(payload.messageId);
    saving = method === "POST" && !generating; paintAll();
    const requestedIds = requestIds(), versions = new Map(fingerprints);
    try {
      if (!provider) throw new Error("no annotation provider");
      const call = method === "GET" ? ["annotations.status", { messageIds: requestedIds }] :
        generating ? [`annotations.${operation}`, payload] : ["chatHeader.toggle", payload];
      const response = await rpc(call[0], call[1], captured, active.signal);
      const value = response.value;
      if (!current(captured) || ticket !== serial || active.signal.aborted) return;
      if (!response.ok || !validStatus(value, captured)) throw Object.assign(new Error("invalid result response"), { code: value?.error || value?.code });
      analysisEnabled = provider.split ? value.analysisEnabled : value.enabled;
      suggestionsEnabled = provider.split ? value.suggestionsEnabled : value.enabled;
      ready = true; error = value.error ? "unavailable" : "";
      // Preserve read access to the old analysis mode without consenting to new generation.
      legacyAnalysisEnabled = value.migrationRequired === true && value.legacy?.analysisEnabled === true;
      roundDueAt = value.enabled && Number.isSafeInteger(value.roundDueInMs) && value.roundDueInMs >= 0 ? Date.now() + value.roundDueInMs : 0;
      // A response computed before an edited message cannot revive its old result.
      if (method === "GET" && !value.error) for (const id of requestedIds) if (versions.get(id) === fingerprints.get(id)) {
        items.delete(id); observed.set(id, versions.get(id));
      }
      for (const item of value.items) if (messages.has(item.messageId) && versions.get(item.messageId) === fingerprints.get(item.messageId)) {
        items.set(item.messageId, item); generationErrors.delete(item.messageId);
      }
      if (!suggestionsEnabled) { opened = ""; copySerial++; }
      // A message mounted during a read (or while both switches were off) still needs an explicit status read.
      if (!value.error && canView() && hasUnreadMessages()) queuedRefresh = true;
      paintAll();
      return value;
    } catch (failure) {
      if (!current(captured) || ticket !== serial || active.signal.aborted) return;
      error = "unavailable";
      if (operation === "generate") generationErrors.set(payload.messageId,
        failure.code === "api-not-configured" ? "尚未配置生成模型，请先在设置中配置。" :
        failure.code === "not-found" ? "暂时无法读取这一轮消息，请稍后重试。" : "生成请求未完成，可以重试。");
      // A failed response may have started a job. Read again before allowing another retry.
      if (generating) queuedRefresh = true;
      paintAll();
    } finally {
      if (current(captured) && ticket === serial) {
        controller = null; saving = false; posting = false; generationTarget = ""; paintAll();
        const queued = queuedRefresh; queuedRefresh = false;
        if (queued && canView()) void refresh(); else refreshLater(roundDueAt ? Math.max(500, Math.min(3000, roundDueAt - Date.now() + 800)) : 3000);
      }
    }
  }
  function refresh() { return request(); }
  function retryPart(id, part, expectedVersion) {
    const item = items.get(id);
    if (!partEnabled(part) || saving || posting || !messages.has(id) || !validId(expectedVersion) || item?.version !== expectedVersion ||
        pending(item[`${part}State`])) return;
    void request("POST", { messageId: id, part, expectedVersion }, "retry");
  }
  function generate(id, ref) {
    if (!current(ref.scope) || !(analysisEnabled || suggestionsEnabled) || !ready || saving || posting || !isObserved(id) || !messages.has(id) || items.has(id)) return;
    void request("POST", { messageId: id }, "generate");
  }
  function action(label, onClick, className = "message-analysis-action") {
    const button = node("button", className, label); button.type = "button";
    button.addEventListener("click", onClick); return button;
  }
  function partState(item, part) {
    return item?.[`${part}State`] || (part === "analysis" ? item?.state : "none") || "none";
  }
  function candidates(item) {
    const values = item?.replies;
    if (!validId(item?.version) || !Array.isArray(values) || values.length !== 3) return [];
    const shaped = values.map(value => ({ label: value?.label, text: value?.text,
      messages: Array.isArray(value?.messages) && value.messages.length ? value.messages : [value?.text] }));
    if (!shaped.every(value => typeof value.label === "string" && value.label.trim() && value.label.length <= 100 &&
        typeof value.text === "string" && value.text.trim() && value.text.length <= 16000 && value.messages.length <= 5 &&
        value.messages.every(line => typeof line === "string" && line.trim() && line.length <= 4000))) return [];
    const normalized = shaped.map(value => value.text.normalize("NFKC").trim().replace(/\s+/gu, " ").toLowerCase());
    return new Set(normalized).size === 3 ? shaped : [];
  }
  function analysisParagraphs(item) {
    const analysis = item?.analysis;
    return [...new Set([analysis?.summary, analysis?.meaning, analysis?.uncertainty, analysis?.responseDirection]
      .filter(value => typeof value === "string" && value.trim()).map(value => value.trim()))];
  }
  // The summary is the card's headline; the other fields follow as labelled lines.
  function analysisFields(item) {
    const analysis = item?.analysis, seen = new Set(), fields = [];
    for (const [label, key] of [["", "summary"], ["意思", "meaning"], ["还不确定", "uncertainty"], ["回复方向", "responseDirection"]]) {
      const value = typeof analysis?.[key] === "string" ? analysis[key].trim() : "";
      if (!value || seen.has(value)) continue;
      seen.add(value); fields.push([label, value]);
    }
    return fields;
  }
  function entryState(id, item) {
    if (generationTarget === id) return "submitting";
    if (!item) return error ? "read-error" : generationErrors.has(id) ? "error" :
      id === latest && roundDueAt ? "waiting" : isObserved(id) ? "empty" : "loading";
    const parts = [];
    if (showAnalysis()) parts.push(partState(item, "analysis"));
    if (suggestionsEnabled) parts.push(partState(item, "suggestions"));
    if (parts.includes("running")) return "running";
    if (parts.includes("pending")) return "pending";
    const analysis = showAnalysis() && analysisParagraphs(item).length > 0, suggestions = suggestionsEnabled && candidates(item).length === 3;
    if (parts.includes("error")) return analysis || suggestions ? "partial" : "error";
    if (parts.includes("stale")) return analysis || suggestions ? "partial" : "stale";
    if (analysis || suggestions) return parts.every(value => value === "ready") && (!showAnalysis() || analysis) && (!suggestionsEnabled || suggestions) ? "ready" : "partial";
    return "incomplete";
  }
  const collapsedLabels = {
    loading: "回复建议", "read-error": "回复建议 · 读取失败", empty: "回复建议", waiting: "等对方说完…",
    submitting: "回复建议 · 提交中…", pending: "回复建议 · 排队中…", running: "回复建议 · 生成中…",
    error: "回复建议 · 未完成", stale: "回复建议 · 已中断", incomplete: "回复建议 · 不完整", partial: "回复建议", ready: "回复建议",
  };
  function sourceNotice(item) {
    if (typeof item?.sourceMessage === "string" && item.sourceMessage.trim()) return item.sourceMessage.trim();
    if (item?.sourceState === "changed") return "原消息已变化，以下为已保存结果。";
    if (item?.sourceState === "checking") return "聊天记录正在同步，已保存结果仍可查看和复制。";
    if (item?.sourceState === "unavailable" || item?.applicability === "unverified") return "暂时无法读取原消息，已保存结果仍可查看和复制。";
    if (item?.applicability === "needs-update") return "建议依据可能已变化，以下为已保存结果。";
    return "";
  }
  function heldByUser(section) {
    if (!section.contains) return false;
    const selection = window.getSelection?.();
    const focus = document.activeElement;
    // Focus left on a button whose click asks for the redraw (switch plan, fold, start) never holds it back.
    const operation = ["message-analysis-generate", "message-analysis-retry", "message-round-tab", "message-round-collapse", "message-round-toggle"]
      .some(name => focus?.classList?.contains(name));
    return !!(selection && !selection.isCollapsed &&
      (section.contains(selection.anchorNode) || section.contains(selection.focusNode))) ||
      (!operation && focus !== section && section.contains(focus));
  }
  function replaceSection(ref, name, signature, render) {
    const section = ref[name];
    if (section.hidden) {
      if (section.firstElementChild) section.replaceChildren();
      delete ref.signatures[name]; return;
    }
    if (ref.signatures[name] === signature || heldByUser(section)) return;
    ref.signatures[name] = signature; section.replaceChildren(); render(section);
  }
  function disclosure(id, key, summary) {
    const details = node("details", "message-analysis-details message-round-card");
    const token = `${id}:${key}`; details.open = disclosures.has(token);
    details.appendChild(summary);
    const captured = capture();
    details.addEventListener("toggle", () => {
      if (!current(captured)) return;
      if (details.open) disclosures.add(token); else disclosures.delete(token);
    });
    return details;
  }
  function partNotice(section, id, item, part, saved = false) {
    const state = partState(item, part), label = part === "analysis" ? "解读" : "回复建议";
    const description = saved ? `${label}${pending(state) ? (state === "pending" ? "排队更新，显示" : "正在更新，显示") :
      state === "error" ? "更新失败，保留" : state === "stale" ? "更新已中断，保留" : "尚未更新，显示"}上次结果。` : pending(state) ?
      (state === "pending" ? `${label}排队中…` : `正在生成${label}…`) :
      state === "stale" ? `${label}生成已中断，可以重试。` : state === "error" ? `${label}生成失败，可以重试。` :
      state === "none" ? `还没有这一轮的${label}。` : `${label}结果不完整，可以重试。`;
    const reason = state === "error" && typeof item?.[`${part}Error`] === "string" ? item[`${part}Error`].trim().slice(0, 240) : "";
    section.appendChild(node("p", "message-analysis-note", description + (reason ? ` ${reason}` : "")));
    if (partEnabled(part) && !pending(state) && validId(item?.version)) {
      section.appendChild(action((state === "none" ? "生成" : "重试") + label, () => {
        if (current(refs.get(id)?.scope)) retryPart(id, part, item.version);
      }, "message-analysis-action message-analysis-retry"));
    }
  }
  async function copyCandidate(text, ref, button, feedback) {
    if (!current(ref.scope) || !suggestionsEnabled || button.disabled || !button.isConnected) return;
    const captured = capture(), ticket = ++copySerial;
    const stillCurrent = () => current(captured) && suggestionsEnabled && ticket === copySerial;
    // Copy the displayed text captured by the click, even when newer results are available.
    const host = window.desktopHost;
    button.disabled = true; button.textContent = "复制中…"; feedback.textContent = "";
    try {
      let copied = false;
      if (typeof host?.copyDraft === "function") {
        try { copied = await host.copyDraft(text); } catch {}
      }
      if (!stillCurrent()) return;
      if (!copied && typeof window.navigator?.clipboard?.writeText === "function") {
        try { await window.navigator.clipboard.writeText(text); copied = true; } catch {}
      }
      if (!stillCurrent() || !button.isConnected) return;
      button.textContent = copied ? "已复制" : "复制";
      feedback.textContent = copied ? "" : "复制失败，请选中文字手动复制。";
    } finally {
      if (current(captured) && button.isConnected) {
        button.disabled = false;
        if (button.textContent === "复制中…") button.textContent = "复制";
      }
    }
  }
  // The whole plan goes into the composer; a draft the user typed is replaced only on a second click.
  function putInDraft(text, key, button, feedback) {
    const input = byId("chatInput");
    if (!input || input.disabled) { feedback.textContent = "现在无法编辑草稿。"; return; }
    clearTimeout(draftTimer); draftTimer = null;
    if (input.value.trim() && input.value !== text && draftConfirm !== key) {
      draftConfirm = key; button.textContent = "替换草稿？";
      draftTimer = setTimeout(() => { draftTimer = null; draftConfirm = ""; if (button.isConnected) button.textContent = "放入草稿"; }, 4000);
      return;
    }
    draftConfirm = ""; input.value = text;
    if (typeof Event === "function") input.dispatchEvent(new Event("input"));
    input.focus?.();
    button.textContent = "已放入草稿"; feedback.textContent = "";
  }
  function prepareBlock(id, ref) {
    if (ref.block) return;
    ref.signatures = {};
    ref.block = node("div", "message-round");
    ref.insight = node("div", "message-round-insight");
    ref.replies = node("div", "message-round-replies"); ref.replies.setAttribute("aria-live", "polite");
    ref.block.append(ref.insight, ref.replies);
    ref.item.appendChild(ref.block);
  }
  // The title line holds 回复建议 and 收起; the plan tabs get a row of their own below it.
  function renderPlans(section, id, item, ref) {
    const replies = candidates(item);
    const selected = Math.min(plans.get(id) || 0, replies.length - 1);
    const tabs = node("div", "message-round-tabs"); tabs.setAttribute("role", "tablist");
    replies.forEach((plan, index) => {
      const tab = action(plan.label, () => {
        if (!current(ref.scope)) return;
        plans.set(id, index); ref.signatures.replies = null; draftConfirm = ""; paintAll();
      }, "message-round-tab"); tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(index === selected)); tabs.appendChild(tab);
    });
    section.appendChild(tabs);
    const plan = replies[selected], feedback = node("p", "message-analysis-copy-feedback"); feedback.setAttribute("role", "status");
    const list = node("div", "message-round-bubbles");
    plan.messages.forEach((line, index) => {
      const row = node("div", "message-round-line");
      const bubble = node("p", "message-round-bubble", line);
      const copy = action("复制", () => { void copyCandidate(line, ref, copy, feedback); }, "message-analysis-copy");
      copy.setAttribute("aria-label", `复制第 ${index + 1} 句`);
      row.append(copy, bubble); list.appendChild(row);
    });
    section.appendChild(list);
    const footer = node("div", "message-round-actions");
    if (suggestionsEnabled && partState(item, "suggestions") === "ready") footer.appendChild(action("换一批", () => {
      if (current(ref.scope)) retryPart(id, "suggestions", item.version);
    }, "message-analysis-action message-analysis-retry"));
    const draft = action("放入草稿", () => {
      if (current(ref.scope)) putInDraft(plan.messages.join("\n"), `${id}:${item.version}:${selected}`, draft, feedback);
    }, "message-analysis-action message-round-draft");
    footer.appendChild(draft);
    section.append(footer, feedback);
  }
  function paintRound(id, ref) {
    const roundIds = rounds.get(id);
    const shown = !!roundIds && (analysisEnabled || suggestionsEnabled || (legacyAnalysisEnabled && items.has(id)));
    ref.item.classList.toggle("has-message-round", shown);
    ref.item.classList.toggle("has-message-analysis", showAnalysis());
    if (!shown) { if (ref.block) ref.block.hidden = true; return; }
    prepareBlock(id, ref); ref.block.hidden = false;
    const item = items.get(id), state = entryState(id, item), open = isOpen(id);
    const replies = suggestionsEnabled ? candidates(item) : [];
    ref.insight.hidden = !showAnalysis();
    ref.replies.hidden = !suggestionsEnabled;
    const count = Array.isArray(item?.roundIds) ? item.roundIds.length : roundIds.length;
    ref.block.setAttribute("data-analysis-state", state);
    ref.block.setAttribute("aria-busy", String(["loading", "submitting", "pending", "running", "waiting"].includes(state)));
    // The explanation card folds on its own; collapsing the reply card never hides it.
    replaceSection(ref, "insight", JSON.stringify([analysisEnabled, suggestionsEnabled, state, count, item?.version,
      partState(item, "analysis"), item?.analysis, item?.analysisError, generationErrors.get(id), error]), section => {
      if (!item) {
        if (analysisEnabled && !suggestionsEnabled) renderEmpty(section, id, ref, state, "本轮解读");
        return;
      }
      const fields = analysisFields(item), analysisState = partState(item, "analysis");
      const summary = node("summary", "message-round-card-head");
      const heading = node("span", "message-round-card-title", count > 1 ? `本轮解读 · ${count} 条消息` : "本轮解读");
      const gist = fields[0]?.[0] === "" ? fields.shift()[1] : pending(analysisState) ? "解读生成中…" : fields.length ? "" :
        analysisState === "none" ? "还没有本轮解读" : "解读未完成，展开可重试";
      summary.append(heading, node("span", "message-round-card-hint"));
      if (gist) summary.appendChild(node("span", "message-round-gist", gist));
      const details = disclosure(id, "insight", summary);
      if (!analysisParagraphs(item).length || analysisState !== "ready") partNotice(details, id, item, "analysis", analysisParagraphs(item).length > 0);
      for (const [label, value] of fields) {
        const field = node("div", "message-round-field");
        field.append(node("span", "message-round-field-label", label), node("p", "message-analysis-paragraph", value));
        details.appendChild(field);
      }
      section.appendChild(details);
    });
    replaceSection(ref, "replies", JSON.stringify([open, analysisEnabled, state, item?.version, partState(item, "suggestions"), item?.replies,
      item?.suggestionsError, generationErrors.get(id), plans.get(id), sourceNotice(item), error && !item]), section => {
      section.classList.toggle("is-collapsed", !open);
      if (!open) {
        const replyState = item ? partState(item, "suggestions") : state;
        section.appendChild(action((collapsedLabels[replyState] || "回复建议") + " ▸", () => {
          if (!current(ref.scope)) return;
          if (id === latest) collapsed.delete(id); else opened = id;
          paintAll();
        }, "message-analysis-action message-round-toggle"));
        return;
      }
      const head = node("div", "message-round-head");
      head.appendChild(node("span", "message-round-title", "回复建议"));
      const collapse = action("收起", () => {
        if (!current(ref.scope)) return;
        if (id === latest) collapsed.add(id); else opened = "";
        paintAll();
      }, "message-analysis-action message-round-collapse");
      head.appendChild(collapse);
      section.appendChild(head);
      const notice = sourceNotice(item);
      if (notice) section.appendChild(node("p", "message-analysis-caption", notice));
      if (!item) {
        renderEmpty(section, id, ref, state, analysisEnabled ? "分析与建议" : "回复建议");
        return;
      }
      if (!replies.length) { partNotice(section, id, item, "suggestions"); return; }
      if (partState(item, "suggestions") !== "ready") partNotice(section, id, item, "suggestions", true);
      renderPlans(section, id, item, ref);
    });
    // Only a submitted request disables these; the background status read is simply aborted by a click.
    for (const button of ref.block.querySelectorAll(".message-analysis-retry")) button.disabled = posting || saving;
    for (const button of ref.block.querySelectorAll(".message-analysis-generate")) button.disabled = posting || saving || (!error && !isObserved(id));
  }
  function renderEmpty(section, id, ref, state, label) {
    if (state === "waiting" || state === "submitting") {
      section.appendChild(node("p", "message-analysis-note", state === "waiting" ? "等对方说完再生成…" : "正在提交…"));
      return;
    }
    section.appendChild(node("p", "message-analysis-note", error ? "暂时无法读取结果。" : generationErrors.get(id) ||
      (isObserved(id) ? `暂无${label}。` : "正在读取这一轮的状态…")));
    section.appendChild(action(error ? "重新读取" : generationErrors.has(id) ? "重试" : `生成${label}`, () => {
      if (!current(ref.scope) || controller) return;
      if (error) void refresh(); else generate(id, ref);
    }, "message-analysis-action message-analysis-generate"));
  }
  function paintAll() { keepPosition(() => { for (const [id, ref] of refs) paintRound(id, ref); }); paintToggle(); }
  function reset() {
    epoch++; serial++; copySerial++; clearTimeout(timer); timer = null; controller?.abort(); controller = null;
    clearTimeout(draftTimer); draftTimer = null; draftConfirm = "";
    scope = null; analysisEnabled = false; suggestionsEnabled = false; ready = false; saving = false; posting = false; error = ""; opened = ""; queuedRefresh = false; generationTarget = "";
    legacyAnalysisEnabled = false; roundDueAt = 0;
    paintAll(); messages = new Map(); items = new Map(); refs = new Map(); disclosures = new Set(); fingerprints = new Map(); observed = new Map(); generationErrors = new Map();
    rounds = new Map(); latest = ""; collapsed = new Set(); plans = new Map();
    deferred = [];
  }
  function start(next) {
    scope = next;
    for (const [message, item] of deferred.splice(0)) if (scope && item.isConnected) attach(message, item);
    paintToggle(); if (scope) void refresh();
  }
  function setScope(account, user, isGroup = false) {
    const next = validId(account) && validId(user) && !isGroup && !user.endsWith("@chatroom") ? { account, user } : null;
    requested = next;
    if (scope?.account === next?.account && scope?.user === next?.user) return Promise.resolve();
    reset();
    // A known provider applies at once, so bubbles rendered right after this call get their entry.
    if (provider) { start(next); return Promise.resolve(); }
    const captured = epoch;
    return discover().then(found => {
      if (captured !== epoch || !found) return;
      start(next);
    });
  }
  function clear() { requested = null; reset(); }
  function pluginsChanged() {
    provider = null; const target = requested; reset();
    return target ? setScope(target.account, target.user) : discover().then(() => {});
  }
  function attach(message, item) {
    if (message.side !== "other" || message.kind === "system" || !validId(String(message.id))) return;
    if (!scope) { if (requested && deferred.length < 2000) deferred.push([message, item]); return; }
    const id = String(message.id);
    const ref = { item, block: null, scope: capture() }, version = fingerprint(message);
    if (fingerprints.has(id) && fingerprints.get(id) !== version) { items.delete(id); observed.delete(id); generationErrors.delete(id); }
    fingerprints.set(id, version); messages.set(id, message); refs.set(id, ref);
    paintRound(id, ref);
  }
  // A round is the friend's messages since the user's last reply; system notices do not break it.
  function roundsOf(list) {
    const result = new Map(); let run = [];
    for (const message of list) {
      if (message.kind === "system") continue;
      if (message.side === "other") { run.push(String(message.id)); continue; }
      if (run.length) result.set(run.at(-1), run.slice(-30));
      run = [];
    }
    if (run.length) result.set(run.at(-1), run.slice(-30));
    return [result, run.length ? run.at(-1) : ""];
  }
  function sync(next) {
    if (!scope) return;
    const selected = new Map(next.filter(message => message.side === "other" && message.kind !== "system").map(message => [String(message.id), message]));
    const [nextRounds, nextLatest] = roundsOf(next);
    const changed = [...nextRounds.keys()].join("\0") !== [...rounds.keys()].join("\0");
    messages = selected; rounds = nextRounds;
    if (latest !== nextLatest) { collapsed.delete(latest); latest = nextLatest; }
    for (const [id, ref] of refs) if (!selected.has(id) || !ref.item.isConnected) refs.delete(id);
    for (const id of fingerprints.keys()) if (!selected.has(id)) { fingerprints.delete(id); items.delete(id); observed.delete(id); generationErrors.delete(id); }
    if (opened && (!rounds.has(opened) || opened === latest)) opened = "";
    paintAll();
    if (changed && canView()) void refresh();
    else if (canView() && hasUnreadMessages()) {
      if (controller) queuedRefresh = true; else refreshLater(0);
    }
  }
  function recover() {
    if (!scope) return;
    serial++; copySerial++; controller?.abort(); controller = null; saving = false; posting = false; queuedRefresh = false; generationTarget = ""; void refresh();
  }
  for (const [id, part] of [["btnMessageAnalysis", "analysis"], ["btnMessageSuggestions", "suggestions"]]) {
    byId(id)?.addEventListener("click", () => {
      if (!ready || saving) return;
      copySerial++;
      void request("POST", { enabled: !partEnabled(part), ...(provider.split ? { part } : {}) });
    });
  }
  byId("messageAnalysisStatus")?.addEventListener("click", () => { if (!controller && error) void refresh(); });
  document.addEventListener("selectionchange", () => { if (opened) paintAll(); });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer); timer = null;
    if (!document.hidden && scope && (!ready || canView())) void refresh();
  });
  window.addEventListener("pagehide", reset);
  // ``isReading`` holds the scroll position only while the user opened an older round;
  // the latest round's suggestions are shown by default and never stop following new messages.
  window.MessageAnalysis = Object.freeze({ setScope, attach, sync, reset: clear, recover, pluginsChanged,
    isReading: () => canView() && !!opened, openedMessageId: () => canView() ? opened : "",
    closeReading: () => { opened = ""; paintAll(); } });
})();
