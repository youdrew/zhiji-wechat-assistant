"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Saved per-message results are a read-only projection, never a source of chat facts.
// Modified 2026-10-13 (Apache-2.0): host slot for plugin message annotations. The provider is the
// enabled plugin that contributes messageAnnotations; data comes from its methods through Core RPC.
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
      const toggle = found?.contributes?.chatHeader?.find(item => item.kind === "toggle");
      provider = found ? { id: found.id, label: toggle?.label || found.name } : null;
    } catch { provider = null; }
    const label = byId("btnMessageAnalysis")?.querySelector("span:last-child");
    if (label && provider) label.textContent = provider.label;
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
  let enabled = false, ready = false, saving = false, error = "", opened = "", queuedRefresh = false;
  let generationTarget = "";
  let migrationRequired = false, legacyAnalysisEnabled = false, pendingCount = 0;
  // The chat the host asked for, kept while no provider plugin is enabled so re-enabling one restores the slot,
  // and bubbles rendered while the provider was still being discovered (attached once it is known).
  let requested = null, deferred = [];
  let messages = new Map(), items = new Map(), refs = new Map(), disclosures = new Set(), fingerprints = new Map(), observed = new Map(), generationErrors = new Map();
  const fingerprint = message => JSON.stringify([message?.kind, message?.text, message?.media]);
  const current = captured => captured && scope && captured.epoch === epoch && captured.account === scope.account && captured.user === scope.user;
  const capture = () => scope ? { ...scope, epoch } : null;
  const canView = () => enabled || legacyAnalysisEnabled;

  function keepPosition(action) {
    const container = byId("chatMessages"), top = container?.scrollTop || 0;
    const bounds = container?.getBoundingClientRect();
    const anchor = bounds && [...container.querySelectorAll(".msg-item")].find(item => item.getBoundingClientRect().bottom > bounds.top);
    const offset = anchor?.getBoundingClientRect().top;
    action();
    if (container) container.scrollTop = anchor?.isConnected ? top + anchor.getBoundingClientRect().top - offset : top;
  }
  function paintToggle() {
    const toggle = byId("btnMessageAnalysis"), status = byId("messageAnalysisStatus"), note = byId("messageAnalysisMigration");
    if (!toggle) return;
    toggle.hidden = !scope; toggle.disabled = !scope || !ready || saving;
    toggle.setAttribute("aria-checked", String(enabled));
    toggle.title = saving ? "保存中…" : !ready ? "读取设置中…" :
      "开启后，为每条好友新消息生成解读和三条回复；结果默认收起，不自动发送。";
    if (status) {
      status.hidden = !scope || (!error && !pendingCount);
      status.textContent = error ? "读取失败 · 重试" : `${pendingCount} 条生成中`;
      status.disabled = !error || !!controller;
    }
    if (note) {
      note.hidden = !scope || !migrationRequired;
      const text = byId("messageAnalysisMigrationText");
      if (text) text.textContent = "原自动分析和回复建议已停用。开启“分析与建议”后，改为每条好友新消息生成解读和三条回复；已有结果保留。";
      const disable = byId("btnDisableLegacyAnalysis");
      if (disable) disable.disabled = saving;
    }
  }
  function refreshLater(delay = 3000) {
    clearTimeout(timer); timer = null;
    if (!scope || !canView() || document.hidden) return;
    timer = setTimeout(() => { timer = null; void refresh(); }, delay);
  }
  function requestIds() {
    const container = byId("chatMessages"), bounds = container?.getBoundingClientRect();
    const visible = bounds ? [...refs.entries()].filter(([, ref]) => {
      const rect = ref.item.getBoundingClientRect();
      return ref.item.isConnected && rect.bottom > bounds.top && rect.top < bounds.bottom;
    }).map(([id]) => id) : [];
    return [...new Set([...(opened ? [opened] : []), ...visible, ...[...messages.keys()].reverse()])].slice(0, 240);
  }
  function validStatus(value, captured) {
    return value?.account === captured.account && value?.user === captured.user && typeof value.enabled === "boolean" &&
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
    const ticket = ++serial, active = new AbortController(); controller = active;
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
      enabled = value.enabled; ready = true; error = value.error ? "unavailable" : "";
      migrationRequired = value.migrationRequired === true;
      // Preserve read access to the old analysis mode without consenting to new generation.
      legacyAnalysisEnabled = migrationRequired && value.legacy?.analysisEnabled === true;
      pendingCount = Number.isSafeInteger(value.pendingCount) && value.pendingCount > 0 ? value.pendingCount : 0;
      // A response computed before an edited message cannot revive its old result.
      if (method === "GET" && !value.error) for (const id of requestedIds) if (versions.get(id) === fingerprints.get(id)) {
        items.delete(id); observed.set(id, versions.get(id));
      }
      for (const item of value.items) if (messages.has(item.messageId) && versions.get(item.messageId) === fingerprints.get(item.messageId)) {
        items.set(item.messageId, item); generationErrors.delete(item.messageId);
      }
      if (!canView()) opened = "";
      // Messages attached or edited during this read were not covered by its snapshot.
      if (method === "GET" && !value.error && canView() && hasUnreadMessages()) queuedRefresh = true;
      if (!enabled) copySerial++;
      paintAll();
      return value;
    } catch (failure) {
      if (!current(captured) || ticket !== serial || active.signal.aborted) return;
      error = "unavailable";
      if (operation === "generate") generationErrors.set(payload.messageId,
        failure.code === "api-not-configured" ? "尚未配置生成模型，请先在设置中配置。" :
        failure.code === "not-found" ? "暂时无法读取这条消息，请稍后重试。" : "分析请求未完成，可以重试。");
      // A failed response may have started a job. Read again before allowing another retry.
      if (generating) queuedRefresh = true;
      paintAll();
    } finally {
      if (current(captured) && ticket === serial) {
        controller = null; saving = false; generationTarget = ""; paintAll();
        const queued = queuedRefresh; queuedRefresh = false;
        if (queued && canView()) void refresh(); else refreshLater();
      }
    }
  }
  function refresh() { return request(); }
  function retryPart(id, part, expectedVersion) {
    const item = items.get(id);
    if (!enabled || saving || controller || !messages.has(id) || !validId(expectedVersion) || item?.version !== expectedVersion ||
        (part === "all" ? pending(item.analysisState) || pending(item.suggestionsState) : pending(item[`${part}State`]))) return;
    void request("POST", { messageId: id, part, expectedVersion }, "retry");
  }
  function generate(id, ref) {
    if (!current(ref.scope) || !enabled || !ready || saving || controller || !isObserved(id) || !messages.has(id) || items.has(id)) return;
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
    if (!values.every(value => typeof value?.label === "string" && value.label.trim() && value.label.length <= 100 &&
        typeof value.text === "string" && value.text.trim() && value.text.length <= 16000)) return [];
    const normalized = values.map(value => value.text.normalize("NFKC").trim().replace(/\s+/gu, " ").toLowerCase());
    return new Set(normalized).size === 3 ? values : [];
  }
  function analysisParagraphs(item) {
    const analysis = item?.analysis;
    return [...new Set([analysis?.summary, analysis?.meaning, analysis?.uncertainty, analysis?.responseDirection]
      .filter(value => typeof value === "string" && value.trim()).map(value => value.trim()))];
  }
  function entryState(id, item) {
    if (generationTarget === id) return "submitting";
    if (!item) return error ? "read-error" : generationErrors.has(id) ? "error" : isObserved(id) ? "empty" : "loading";
    const parts = [partState(item, "analysis"), ...(enabled ? [partState(item, "suggestions")] : [])];
    if (item.state === "running" || parts.includes("running")) return "running";
    if (item.state === "pending" || parts.includes("pending")) return "pending";
    const analysis = analysisParagraphs(item).length > 0, suggestions = enabled && candidates(item).length === 3;
    if (item.state === "error" || parts.includes("error")) return analysis || suggestions ? "partial" : "error";
    if (item.state === "stale" || parts.includes("stale")) return analysis || suggestions ? "partial" : "stale";
    if (analysis || suggestions) return parts.every(value => value === "ready") && analysis && (!enabled || suggestions) ? "ready" : "partial";
    return "incomplete";
  }
  const entryLabels = {
    loading: ["spinner", "正在读取这句的分析状态"], "read-error": ["alert", "分析状态读取失败，展开后重新读取"],
    empty: ["play", "这句尚未分析，展开后可开始分析"], submitting: ["spinner", "正在提交这句的分析请求"],
    pending: ["spinner", "这句的分析任务正在排队"], running: ["spinner", "这句的分析任务正在生成"],
    error: ["alert", "这句的分析任务未完成，展开后可重试"], stale: ["alert", "上次生成已中断，展开后可重试"],
    incomplete: ["alert", "这句尚无完整分析结果，展开后可重试"],
    partial: ["alert", "部分生成未完成，已完成的内容保留，展开后可重试"], ready: [null, "查看这句的分析与建议"],
  };
  function resultProblem(item, state) {
    if (!item) return "";
    if (pending(state)) return ["analysis", ...(enabled ? ["suggestions"] : [])].filter(part =>
      pending(partState(item, part)) && (part === "analysis" ? analysisParagraphs(item).length : candidates(item).length))
      .map(part => `${part === "analysis" ? "解读" : "回复建议"}${partState(item, part) === "pending" ? "排队更新" : "正在更新"}，显示上次结果。`).join(" ");
    if (!["partial", "error", "stale", "incomplete"].includes(state)) return "";
    return ["analysis", ...(enabled ? ["suggestions"] : [])].map(part => {
      const label = part === "analysis" ? "解读" : "回复建议", status = partState(item, part);
      const complete = part === "analysis" ? analysisParagraphs(item).length > 0 : candidates(item).length === 3;
      return label + (status === "error" ? (complete ? "更新失败，保留上次结果" : "生成失败") :
        status === "stale" ? (complete ? "更新已中断，保留上次结果" : "生成已中断") : complete && status === "ready" ? "已完成" :
        status === "none" ? "尚未生成" : "结果不完整");
    }).join("；") + "。可重试未完成部分。";
  }
  function sourceNotice(item) {
    if (typeof item?.sourceMessage === "string" && item.sourceMessage.trim()) return item.sourceMessage.trim();
    if (item?.sourceState === "changed") return "原消息已变化，以下为已保存结果。";
    if (item?.sourceState === "checking") return "聊天记录正在同步，已保存结果仍可查看和复制。";
    if (item?.sourceState === "unavailable" || item?.applicability === "unverified") return "暂时无法读取原消息，已保存结果仍可查看和复制。";
    if (item?.applicability === "needs-update") return "建议依据可能已变化，以下为已保存结果。";
    return item?.applicability === "historical" ? "历史参考 · 请结合最新聊天" : "";
  }
  const iconCircle = ["circle", { cx: 8, cy: 8, r: 5.5 }];
  const entryIcons = {
    spinner: [["circle", { cx: 8, cy: 8, r: 5.5, opacity: .2 }], ["path", { d: "M8 2.5A5.5 5.5 0 0 1 13.5 8" }]],
    play: [["path", { d: "m5.5 3.5 7 4.5-7 4.5Z" }]],
    alert: [iconCircle, ["path", { d: "M8 4.75v3.5M8 10.75h.01" }]],
  };
  function paintEntryIcon(ref, name) {
    if (ref.iconName === name) return; // Polling and queued-to-running transitions keep the spinner.
    ref.iconName = name;
    ref.button.replaceChildren(); ref.button.textContent = name ? "" : "?";
    if (!name) return;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({
      viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": 1.4,
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false",
      class: `message-analysis-icon${name === "spinner" ? " is-spinning" : ""}`, "data-icon": name,
    })) svg.setAttribute(key, String(value));
    for (const [tag, attributes] of entryIcons[name]) {
      const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, String(value));
      svg.appendChild(shape);
    }
    ref.button.appendChild(svg);
  }
  function heldByUser(section) {
    if (!section.contains) return false;
    const selection = window.getSelection?.();
    const focus = document.activeElement;
    const operation = focus?.classList?.contains("message-analysis-generate") || focus?.classList?.contains("message-analysis-retry");
    return !!(selection && !selection.isCollapsed &&
      (section.contains(selection.anchorNode) || section.contains(selection.focusNode))) ||
      (!operation && focus !== section && section.contains(focus));
  }
  function replaceSection(ref, name, signature, render) {
    const section = ref[name];
    if (ref.signatures[name] === signature || heldByUser(section)) return;
    ref.signatures[name] = signature; section.replaceChildren(); render(section);
  }
  function disclosure(id, key, title) {
    const details = node("details", "message-analysis-details");
    const token = `${id}:${key}`; details.open = disclosures.has(token);
    details.appendChild(node("summary", "", title));
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
      state === "none" ? `还没有保存这条消息的${label}。` : `${label}结果不完整，可以重试。`;
    const reason = state === "error" && typeof item?.[`${part}Error`] === "string" ? item[`${part}Error`].trim().slice(0, 240) : "";
    section.appendChild(node("p", "message-analysis-note", description + (reason ? ` ${reason}` : "")));
    if (enabled && !pending(state) && validId(item?.version)) {
      const retry = action(`重试${label}`, () => {
        if (current(refs.get(id)?.scope)) retryPart(id, part, item.version);
      }, "message-analysis-action message-analysis-retry");
      section.appendChild(retry);
    }
  }
  async function copyCandidate(text, ref, button, feedback) {
    if (!current(ref.scope) || !enabled || button.disabled || !button.isConnected) return;
    const captured = capture(), ticket = ++copySerial;
    const stillCurrent = () => current(captured) && enabled && ticket === copySerial;
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
  function prepareBody(id, ref) {
    if (ref.meta) return;
    ref.signatures = {};
    for (const name of ["meta", "replies"]) {
      ref[name] = node("div", `message-analysis-${name}`); ref.body.appendChild(ref[name]);
    }
    ref.detail = disclosure(id, "explanation", "详细解读");
    for (const name of ["explanation", "evidence", "utilities"]) {
      ref[name] = node("div", `message-analysis-${name}`); ref.detail.appendChild(ref[name]);
    }
    ref.body.appendChild(ref.detail);
    // Modified 2026-10-13: the chat panel is a plugin contribution handed the quote by plugin-slots.js.
    // Modified 2026-10-14 (review): no 继续聊这句 while no chat panel plugin is enabled.
    if (typeof window.PluginSlots?.continueFromMessage === "function" && window.PluginSlots.canContinue?.() !== false &&
        typeof window.desktopHost?.openPluginWindow === "function") {
      const follow = action("继续聊这句", async () => {
        if (!current(ref.scope)) return;
        follow.disabled = true;
        const ok = await window.PluginSlots.continueFromMessage({ account: scope.account, user: scope.user, messageId: id,
          text: messages.get(id)?.text || "", kind: messages.get(id)?.kind || "" });
        if (!current(ref.scope)) return;
        follow.disabled = false; follow.textContent = ok ? "已准备助手草稿" : "暂无法带入助手";
      });
      ref.detail.appendChild(follow);
    }
  }
  function paintMessage(id, ref) {
    const viewable = canView();
    ref.button.hidden = !viewable; ref.item.classList.toggle("has-message-analysis", viewable);
    const item = items.get(id), state = entryState(id, item), [icon, description] = entryLabels[state];
    paintEntryIcon(ref, icon);
    ref.button.classList.toggle("has-result", state === "ready" || state === "partial");
    ref.button.classList.toggle("is-pending", pending(state) || state === "submitting");
    const problem = resultProblem(item, state);
    const label = !enabled && state === "ready" ? "查看这句的聊天分析" : problem || description;
    ref.button.title = label; ref.button.setAttribute("aria-label", label);
    ref.button.setAttribute("data-analysis-state", state);
    ref.button.setAttribute("aria-busy", String(["loading", "submitting", "pending", "running"].includes(state)));
    ref.button.setAttribute("aria-expanded", String(viewable && opened === id));
    ref.body.hidden = !viewable || opened !== id;
    if (ref.body.hidden) return;
    prepareBody(id, ref);
    const replies = enabled ? candidates(item) : [];
    const notice = sourceNotice(item);
    replaceSection(ref, "meta", JSON.stringify([notice, problem, enabled]), section => {
      for (const text of [problem, notice]) if (text) section.appendChild(node("p", "message-analysis-caption", text));
    });
    replaceSection(ref, "explanation", JSON.stringify([enabled, partState(item, "analysis"), item?.analysis, item?.analysisError, partState(item, "analysis") === "ready" ? null : item?.version]), section => {
      const paragraphs = analysisParagraphs(item);
      if (!paragraphs.length || partState(item, "analysis") !== "ready") partNotice(section, id, item, "analysis", paragraphs.length > 0);
      for (const paragraph of paragraphs) section.appendChild(node("p", "message-analysis-paragraph", paragraph));
    });
    ref.replies.hidden = !enabled;
    ref.detail.hidden = enabled && !item;
    replaceSection(ref, "replies", JSON.stringify([enabled, item?.version, partState(item, "suggestions"), item?.replies, item?.suggestionsError, !item && error, !item && isObserved(id), generationErrors.get(id)]), section => {
      if (!enabled) return;
      if (!item) {
        const empty = node("div", "message-analysis-empty");
        empty.appendChild(node("p", "message-analysis-note", error ? "暂时无法读取分析结果。" : generationErrors.get(id) ||
          (isObserved(id) ? "这条消息还没有分析结果。" : "正在读取这条消息的分析状态…")));
        empty.appendChild(action(error ? "重新读取" : generationErrors.has(id) ? "重试分析" : "开始分析", () => {
          if (!current(ref.scope) || controller) return;
          if (error) void refresh(); else generate(id, ref);
        }, "message-analysis-action message-analysis-generate"));
        section.appendChild(empty); return;
      }
      if (!replies.length) { partNotice(section, id, item, "suggestions"); return; }
      if (partState(item, "suggestions") !== "ready") partNotice(section, id, item, "suggestions", true);
      const list = node("div", "message-analysis-candidates");
      replies.forEach(candidate => {
        const card = node("article", "message-analysis-candidate");
        const text = node("p", "message-analysis-candidate-text", candidate.text);
        const feedback = node("p", "message-analysis-copy-feedback"); feedback.setAttribute("role", "status");
        const copy = action("复制", () => { void copyCandidate(text.textContent, ref, copy, feedback); }, "message-analysis-copy");
        copy.setAttribute("aria-label", `复制${candidate.label}`); copy.title = candidate.label;
        card.append(text, copy, feedback); list.appendChild(card);
      });
      section.appendChild(list);
    });
    replaceSection(ref, "evidence", JSON.stringify(item?.analysis?.evidence), section => {
      if (!item?.analysis) return;
      const analysis = item.analysis;
      const evidence = Array.isArray(analysis.evidence) ? analysis.evidence.filter(value => typeof value?.text === "string" && value.text.trim()).slice(0, 20) : [];
      if (evidence.length) {
        section.appendChild(node("p", "message-analysis-note", "参考背景"));
        for (const value of evidence) {
          const entry = node("div", "message-analysis-source");
          if (typeof value.sourceLabel === "string" && value.sourceLabel) entry.appendChild(node("span", "message-analysis-source-label", value.sourceLabel));
          entry.appendChild(node("p", "", value.text)); section.appendChild(entry);
        }
      }
    });
    replaceSection(ref, "utilities", JSON.stringify([enabled, item?.version, replies.length, partState(item, "suggestions")]), section => {
      if (enabled && replies.length && partState(item, "suggestions") === "ready") section.appendChild(action("重新生成回复", () => {
        if (current(ref.scope)) retryPart(id, "suggestions", item.version);
      }, "message-analysis-action message-analysis-retry"));
    });
    for (const button of ref.body.querySelectorAll(".message-analysis-retry")) button.disabled = !!controller || saving;
    for (const button of ref.body.querySelectorAll(".message-analysis-generate")) {
      button.disabled = !!controller || saving || (!error && !isObserved(id));
      button.textContent = generationTarget === id ? "正在提交…" : error ? "重新读取" : generationErrors.has(id) ? "重试分析" : !isObserved(id) ? "读取中…" : "开始分析";
    }
  }
  function paintAll() { keepPosition(() => { for (const [id, ref] of refs) paintMessage(id, ref); }); paintToggle(); }
  function reset() {
    epoch++; serial++; copySerial++; clearTimeout(timer); timer = null; controller?.abort(); controller = null;
    scope = null; enabled = false; ready = false; saving = false; error = ""; opened = ""; queuedRefresh = false; generationTarget = "";
    migrationRequired = false; legacyAnalysisEnabled = false; pendingCount = 0;
    paintAll(); messages = new Map(); items = new Map(); refs = new Map(); disclosures = new Set(); fingerprints = new Map(); observed = new Map(); generationErrors = new Map();
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
    const id = String(message.id), wrap = item.querySelector(".msg-content-wrap"), bubble = wrap?.firstElementChild;
    if (!bubble) return;
    const row = node("div", "message-analysis-line"), button = node("button", "message-analysis-question");
    button.type = "button"; button.hidden = !enabled;
    const body = node("div", "message-analysis-body"); body.hidden = true; body.setAttribute("aria-live", "polite");
    body.id = `message-analysis-${++attach.counter}`; button.setAttribute("aria-controls", body.id);
    wrap.insertBefore(row, bubble); row.append(bubble, button); wrap.appendChild(body);
    const ref = { item, button, body, scope: capture() }, version = fingerprint(message);
    if (fingerprints.has(id) && fingerprints.get(id) !== version) { items.delete(id); observed.delete(id); generationErrors.delete(id); }
    fingerprints.set(id, version); messages.set(id, message); refs.set(id, ref);
    button.addEventListener("click", () => {
      if (!current(ref.scope) || !canView()) return;
      opened = opened === id ? "" : id; paintAll();
      // Viewing is deliberately read-only, including messages without a saved result.
    });
    body.addEventListener("focusout", () => { setTimeout(() => { if (current(ref.scope)) paintAll(); }, 0); });
    paintMessage(id, ref);
  }
  attach.counter = 0;
  function sync(next) {
    if (!scope) return;
    const selected = new Map(next.filter(message => message.side === "other" && message.kind !== "system").map(message => [String(message.id), message]));
    const changed = [...selected.keys()].join("\0") !== [...messages.keys()].join("\0");
    messages = selected;
    for (const [id, ref] of refs) if (!selected.has(id) || !ref.item.isConnected) refs.delete(id);
    for (const id of fingerprints.keys()) if (!selected.has(id)) { fingerprints.delete(id); items.delete(id); observed.delete(id); generationErrors.delete(id); }
    if (opened && !selected.has(opened)) opened = "";
    if (changed && canView()) void refresh();
    else if (canView() && hasUnreadMessages()) {
      if (controller) queuedRefresh = true; else refreshLater(0);
    }
  }
  function recover() {
    if (!scope) return;
    serial++; copySerial++; controller?.abort(); controller = null; saving = false; queuedRefresh = false; generationTarget = ""; void refresh();
  }
  byId("btnMessageAnalysis")?.addEventListener("click", () => {
    if (ready && !saving) { copySerial++; void request("POST", { enabled: !enabled }); }
  });
  byId("messageAnalysisStatus")?.addEventListener("click", () => { if (!controller && error) void refresh(); });
  byId("btnDisableLegacyAnalysis")?.addEventListener("click", () => {
    if (ready && !saving && migrationRequired) { copySerial++; void request("POST", { enabled: false }); }
  });
  document.addEventListener("selectionchange", () => { if (opened) paintAll(); });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer); timer = null;
    if (!document.hidden && scope && (!ready || canView())) void refresh();
  });
  window.addEventListener("pagehide", reset);
  window.MessageAnalysis = Object.freeze({ setScope, attach, sync, reset: clear, recover, pluginsChanged,
    isReading: () => canView() && !!opened, openedMessageId: () => canView() ? opened : "",
    closeReading: () => { opened = ""; paintAll(); } });
})();
