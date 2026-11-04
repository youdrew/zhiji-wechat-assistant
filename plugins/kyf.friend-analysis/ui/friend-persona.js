"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 性格分析 and 三观分析 (split from 人格分析, 2026-10-25): the shared charts on top; choosing a chart opens its
// dimensions below with confidence, evidence and 我的观察. AI results and the user's own observations are
// separate records; neither overwrites the other.
(function () {
  const A = window.FriendApp, C = window.KyfPersonaCharts;
  const VALUE_ITEMS = [["love", "感情观"], ["family", "家庭观"], ["money", "金钱观"],
    ["plan", "人生规划"], ["core", "价值底色"]];
  const VALUE_KEYS = new Set(VALUE_ITEMS.map(([id]) => id));
  const shared = { data: null, request: 0, pages: [] };

  async function fetchData() {
    const request = ++shared.request;
    const data = await A.rpc("page.persona");
    if (request === shared.request) shared.data = data;
    return request === shared.request;
  }

  function make(tab, pageId) {
    const P = { open: new Set(), observing: null, selected: null };
    const root = () => A.byId(pageId);
    const owns = dimension => VALUE_KEYS.has(dimension) ? tab === "values" :
      dimension === "mbti" ? tab === "personality" : C.tabOf(dimension) === tab;

    async function load() {
      try {
        if (await fetchData()) render();
      } catch (error) {
        if (!A.masked) root().replaceChildren(A.el("div", "fa-empty", error.message || "读取失败"));
      }
    }

    // The charts' names (能量, 焦虑) rather than the analysis catalog's (外向性, 依恋焦虑).
    function label(id) { return C.DIMENSIONS[id] ? C.label(id) : shared.data.labels[id] ||
      VALUE_ITEMS.find(([key]) => key === id)?.[1] || id; }
    function observationsFor(id) { return shared.data.observations.filter(item => item.dimension === id); }
    function newObservationFor(id) { return P.observing && !P.observing.item && P.observing.dimension === id; }
    function result() { return shared.data.result; }
    function dimension(id) { return result().dimensions.find(item => item.id === id); }

    function render() {
      const page = root();
      if (A.masked || !shared.data) return;
      const focusKey = page.contains(document.activeElement) ? document.activeElement?.dataset?.key : null;
      page.replaceChildren();
      if (result()) {
        if (shared.data.stale) page.append(A.el("p", "fa-facts", "有新证据，下次分析时更新"));
        const charts = A.el("div", "fa-charts");
        C.render(charts, { tab, selected: P.selected, friend: true, sources: shared.data.sources,
          dims: Object.fromEntries(result().dimensions.map(item => [item.id, item])),
          selfReports: Object.fromEntries(Object.entries(result().selfReports).map(([key, value]) => [key, value?.value || null])) },
        id => { P.selected = P.selected === id ? null : id; render(); });
        page.append(charts);
        const drift = driftBlock(result());
        if (drift) page.append(drift);
        if (P.selected) page.append(cardDetail(C.CARDS.find(card => card.id === P.selected)));
        if (tab === "values") {
          if (!Array.isArray(result().values))
            page.append(A.el("p", "fa-note", "这份旧版结果尚未包含感情观等文字分析，下次分析后更新。"));
          page.append(valuesBlock());
        }
        page.append(detailsBlock(result()));
        const sources = sourcesBlock();
        if (sources) page.append(sources);
      } else {
        page.append(A.el("div", "fa-empty", shared.data.stale ? "旧版分析待更新，重新分析后显示" : "开始分析后显示"));
        if (tab === "values") page.append(valuesBlock());
      }
      if (tab === "personality") page.append(otherObservations());
      if (focusKey) page.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
    }

    function cardDetail(card) {
      const list = A.el("div", "fa-list fa-chart-detail");
      const reports = result().selfReports;
      const ids = [...card.dims, ...(card.extra || [])].map(dimension).filter(Boolean);
      // 把握较低 folds into a separate 参考 group instead of mixing with the firmer rows.
      for (const item of ids.filter(item => item.tier !== "reference")) list.append(dimensionRow(item));
      const reference = ids.filter(item => item.tier === "reference");
      if (reference.length) {
        const details = A.el("details", "fa-details");
        details.append(A.el("summary", "", `参考 · 把握较低（${reference.length}）`));
        for (const item of reference) details.append(dimensionRow(item));
        list.append(details);
      }
      if (card.id === "big5") list.append(reportRow("mbti", reports.mbti));
      if (card.id === "attachment" && reports.attachment) list.append(reportRow(null, reports.attachment, "依恋类型自述"));
      if (card.id === "politics" && reports.politics) list.append(reportRow(null, reports.politics, "政治自我定位"));
      return list;
    }

    function head(id, text, confidence) {
      const main = A.el("div", "fa-item-main");
      const body = A.el("div", "fa-item-text");
      const top = A.el("div", "fa-persona-head");
      top.append(A.el("span", "fa-row-label", label(id)), A.el("span", "fa-row-text", text));
      if (confidence) top.append(A.el("span", "fa-mark", confidence));
      body.append(top);
      for (const item of observationsFor(id)) body.append(observationLine(item));
      const actions = A.el("div", "fa-item-actions");
      if (!A.restricted) actions.append(A.button("修正", "fa-plain", () => startObservation(null, id)));
      main.append(body, actions);
      return { main, body };
    }
    function dimensionRow(item) {
      const row = A.el("div", "fa-item");
      if (newObservationFor(item.id)) { row.append(observationForm(P.observing)); return row; }
      // A Big Five row reads like its scale ("93% 外向"); other rows keep the analysis' own wording,
      // except the extended sliders (same share wording) and the type dims' distribution line.
      const text = item.label ? C.share(item.id, item) || item.label : C.typeText(item.id, item) || "证据不足";
      const { main, body } = head(item.id, text, item.label ? item.confidence : null);
      if (item.label) {
        main.lastChild.prepend(A.button(P.open.has(item.id) ? "收起" : "依据", "fa-plain", () => {
          P.open.has(item.id) ? P.open.delete(item.id) : P.open.add(item.id);
          render();
        }));
        if (P.open.has(item.id)) body.append(detail(item));
      }
      row.append(main);
      return row;
    }
    function detail(item) {
      const box = A.el("div", "fa-persona-detail");
      // 防漂移: a dimension that moved keeps its previous conclusion beside the new one.
      if (item.previous) box.append(A.el("p", "fa-note",
        `上次判断：${item.previous.label}（${item.previous.confidence || "把握未知"}）`));
      if (item.detail) box.append(A.el("p", "fa-text", item.detail));
      if (item.periods) box.append(A.el("p", "fa-note", "不同时期：" + item.periods));
      const links = A.el("div", "fa-choice");
      if (item.support.length) links.append(A.button(`支持的原文 ${item.support.length} 条`, "fa-secondary",
        () => showEvidence(item.support, label(item.id) + " · 支持")));
      if (item.counter.length) links.append(A.button(`相反的原文 ${item.counter.length} 条`, "fa-secondary",
        () => showEvidence(item.counter, label(item.id) + " · 相反")));
      box.append(links);
      const cites = (shared.data.sources || {})[item.id];
      if (cites && cites.length) {
        box.append(A.el("p", "fa-note", `理论依据：${cites[0]}${cites.length > 1 ? ` 等 ${cites.length} 篇` : ""}`),
          A.button("查看文献", "fa-plain", () => showSources(item.id)));
      }
      return box;
    }
    function valuesBlock() {
      const list = A.el("div", "fa-values");
      const entries = new Map((result()?.values || []).map(item => [item.id, item]));
      for (const [id, title] of VALUE_ITEMS) list.append(valueCard(id, title, entries.get(id)));
      return list;
    }
    function valueCard(id, title, item) {
      const card = A.el("section", "fa-value-card");
      card.append(A.el("h3", "", title));
      const report = item?.selfReport, inference = item?.inference;
      const self = A.el("div", "fa-value-row");
      self.append(A.el("span", "fa-row-label", "对方自述"));
      const statements = Array.isArray(report?.statements) ? report.statements : [];
      if (statements.length) {
        const lines = A.el("div", "fa-value-statements");
        for (const statement of statements) {
          const line = A.el("div", "fa-value-statement");
          line.append(A.el("span", "", `${A.formatDate(statement.timeMs)} · ${statement.text}`));
          if (statement.evidence?.length) line.append(A.button("原文", "fa-plain",
            () => showEvidence(statement.evidence, title + " · 对方自述")));
          lines.append(line);
        }
        if (report.moreCount) lines.append(A.el("div", "fa-note", `另有 ${report.moreCount} 条不同自述，可查看全部原文。`));
        self.append(lines);
      } else self.append(A.el("span", "fa-row-text", report?.text || (result() ? "尚无明确自述" : "分析后显示")));
      if (report?.evidence?.length) self.append(A.button(`原文 ${report.evidence.length} 条`, "fa-plain",
        () => showEvidence(report.evidence, title + " · 对方自述")));
      card.append(self);
      const ai = A.el("div", "fa-value-row");
      ai.append(A.el("span", "fa-row-label", "AI 推测"),
        A.el("span", "fa-row-text", inference?.label ?
          [inference.label, inference.detail].filter(Boolean).join(" · ") : result() ? "证据不足，暂不推测" : "分析后显示"));
      if (inference?.label) {
        ai.append(A.el("span", "fa-mark", inference.confidence));
        card.append(ai, detail(inference));
      } else card.append(ai);
      const observe = A.el("div", "fa-value-observation");
      if (newObservationFor(id)) observe.append(observationForm(P.observing));
      else {
        for (const saved of observationsFor(id)) observe.append(observationLine(saved));
        if (!A.restricted) observe.append(A.button("记录我的观察", "fa-plain", () => startObservation(null, id)));
      }
      card.append(observe);
      return card;
    }
    // MBTI keeps 我的观察 beside it; its type itself comes from the chart (自述 first, else the Big Five).
    function reportRow(id, report, title) {
      const row = A.el("div", "fa-item");
      if (id && newObservationFor(id)) { row.append(observationForm(P.observing)); return row; }
      const dims = Object.fromEntries(result().dimensions.map(item => [item.id, item]));
      // MBTI shows the type the chart draws; the friend's own words stay beside it, even when they differ.
      const type = id === "mbti" ? C.mbti(dims, null) : null;
      const text = type ? type.text : report ? report.value : "证据不足";
      let main, body;
      if (id) ({ main, body } = head(id, text, type ? "由图表推算" : report ? "本人自述" : null));
      else {
        main = A.el("div", "fa-item-main");
        body = A.el("div", "fa-item-text");
        const top = A.el("div", "fa-persona-head");
        top.append(A.el("span", "fa-row-label", title), A.el("span", "fa-row-text", text), A.el("span", "fa-mark", "本人自述"));
        body.append(top);
        main.append(body);
      }
      if (report) body.append(A.el("div", "fa-note", (type ? `本人自述 ${report.value} · ` : "") +
        `原话（${A.formatDate(report.timeMs)}）：${report.quote}` +
        (report.differs ? " · 不同时期的说法不一致，显示最近一次" : "")));
      row.append(main);
      return row;
    }
    // The dims of this tab across all its cards, including the friend-only extra rows.
    function tabDims() {
      return new Set(C.cards(tab).flatMap(card => [...card.dims, ...(card.extra || [])]));
    }
    function familyOf() {
      return Object.fromEntries((shared.data.catalog || []).map(item => [item.id, item.group]).filter(([, group]) => group));
    }
    function detailsBlock(data) {
      const details = A.el("details", "fa-details");
      details.append(A.el("summary", "", "分析依据与说明"));
      const list = A.el("dl", "fa-kv");
      const coverage = data.coverage;
      const rows = [["覆盖时间", coverage.fromMs ? `${A.formatDate(coverage.fromMs)} 至 ${A.formatDate(coverage.toMs)}` : "—"],
        ["已处理原文", `${coverage.processed} 条对方文字消息`], ["未计入", `${coverage.excluded} 条媒体或非文字消息`],
        ["证据", `共 ${coverage.evidence} 条，引用 ${coverage.cited} 条`],
        ["核查", data.removed ? `${data.removed} 项未通过逐项核查，已改为证据不足` : "全部通过逐项核查"],
        ["历史", data.historyComplete ? "全部可读历史" : "历史分析未完成"],
        ["模型", data.model || "—"], ["生成时间", A.formatDate(shared.data.createdAtMs, true)]];
      for (const [key, value] of rows) list.append(A.el("dt", "", key), A.el("dd", "", value));
      // The method note follows the catalog: one sentence per family of this tab, so new dimensions
      // describe themselves instead of the wording drifting out of date.
      const families = shared.data.families || {}, groups = familyOf(), dims = tabDims();
      const notes = { personality: "大五人格按 MBTI 五条轴显示、类型由轴推算，依恋是焦虑 × 回避平面" };
      const parts = [];
      for (const [family, name] of Object.entries(families)) {
        const names = Object.keys(groups).filter(key => groups[key] === family && dims.has(key))
          .map(key => C.TYPE_VOCAB[key] ? C.TYPE_VOCAB[key].join("、") : label(key));
        if (names.length) parts.push(`${name}（${names.join("、")}${notes[family] ? "；" + notes[family] : ""}）`);
      }
      details.append(list, A.el("p", "fa-note", "图上的点按证据偏向两端的条数计算，证据少时靠近中间；只有通过核查的维度才画点。" +
        (parts.length ? "维度来自" + parts.join("；") + "。" : "") +
        "全部是根据聊天表现的推测，不是心理测评或诊断；“我的观察”单独保存，不会作为分析的输入。"));
      return details;
    }
    // 防漂移: the page names the dimensions whose position moved by 0.3 or more instead of
    // silently overwriting the previous result.
    function driftBlock(data) {
      const moves = data.drift || [];
      if (!moves.length) return null;
      const details = A.el("details", "fa-details");
      details.append(A.el("summary", "", `与上次相比 ${moves.length} 个维度位置移动 ≥0.3`));
      for (const move of moves) details.append(A.el("p", "fa-note",
        `${label(move.id)}：上次 ${C.poleShare(move.id, { position: move.from })} → 本次 ${C.poleShare(move.id, { position: move.to })}`));
      return details;
    }
    function sourcesBlock() {
      const sources = shared.data.sources || {};
      if (!Object.keys(sources).length) return null;
      const groups = familyOf(), dims = tabDims();
      const details = A.el("details", "fa-details");
      details.append(A.el("summary", "", "参考量表来源"));
      const box = A.el("div", "fa-sources");
      for (const [family, name] of Object.entries(shared.data.families || {})) {
        const cites = [];
        const seen = new Set();
        for (const key of Object.keys(groups).filter(key => groups[key] === family && dims.has(key)))
          for (const cite of sources[key] || []) if (!seen.has(cite)) { seen.add(cite); cites.push(cite); }
        if (!cites.length) continue;
        box.append(A.el("h4", "", name));
        for (const cite of cites) box.append(A.el("p", "", cite));
      }
      if (!box.childNodes.length) return null;
      details.append(box);
      return details;
    }
    function showSources(id) {
      const body = A.openSheet(label(id) + " · 文献", "persona-sources");
      const cites = (shared.data.sources || {})[id] || [];
      if (!cites.length) body.append(A.el("p", "fa-note", "暂无文献记录"));
      for (const cite of cites) body.append(A.el("p", "fa-msg-text", cite));
    }

    async function showEvidence(ids, title) {
      const body = A.openSheet(title, "persona-evidence");
      let offset = 0;
      const more = A.button("加载更多原文", "fa-secondary", () => { void loadMore(); });
      async function loadMore() {
        more.remove();
        more.disabled = true;
        const loading = A.el("p", "fa-note", "读取中…");
        body.append(loading);
        try {
          const batch = ids.slice(offset, offset + 40);
          const data = await A.rpc("page.personaEvidence", { ids: batch });
          if (A.sheetOwner !== "persona-evidence") return;
          loading.remove();
          offset += batch.length;
          const byId = new Map(data.items.map(item => [item.id, item]));
          for (const id of batch) {
            const item = byId.get(id);
            if (!item) continue;
            const row = A.el("div", "fa-msg");
            row.append(A.el("div", "fa-msg-head", `对方 · ${A.formatDate(item.timeMs, true)}` +
              (item.status === "ok" ? "" : item.status === "changed" ? " · 原文已变化" : " · 原文已不在可读历史中")),
              A.el("div", "fa-msg-text", item.excerpt), A.el("div", "fa-note", item.note));
            const context = A.button("看上下文", "fa-plain", async () => {
              context.disabled = true;
              try { row.append(A.contextList(await A.host("message.context", { id: item.messageId }))); context.remove(); }
              catch (error) { context.disabled = false; A.toast(error.message || "读取失败"); }
            });
            row.append(context);
            body.append(row);
          }
          if (offset < ids.length) { more.disabled = false; body.append(more); }
        } catch (error) {
          loading.textContent = error.message || "读取失败";
          more.disabled = false;
          body.append(more);
        }
      }
      await loadMore();
    }

    // -- 我的观察 ---------------------------------------------------------------------------------
    function observationLine(item) {
      const line = A.el("div", "fa-observation");
      if (P.observing?.item?.id === item.id) { line.append(observationForm(P.observing)); return line; }
      line.append(A.el("span", "fa-row-label", "我的观察"), A.el("span", "", item.judgement));
      if (item.note) line.append(A.el("span", "fa-note", " · " + item.note));
      if (!A.restricted) line.append(A.button("编辑", "fa-plain", () => startObservation(item)),
        A.button("删除", "fa-plain", () => deleteObservation(item)));
      return line;
    }
    function shownDimensions() {
      const keys = new Set((shared.data.catalog || []).map(item => item.id));
      keys.add("mbti");
      for (const [id] of VALUE_ITEMS) keys.add(id);
      return keys;
    }
    function otherObservations() {
      const known = result() ? shownDimensions() : new Set(VALUE_KEYS);
      const items = shared.data.observations.filter(item => !item.dimension || !known.has(item.dimension));
      const block = A.el("section", "fa-section");
      const top = A.el("div", "fa-group");
      top.append(A.el("span", "", "其他观察"), A.el("span", "fa-spacer"));
      if (!A.restricted) top.append(A.button("记录观察", "fa-plain", () => startObservation(null, "")));
      block.append(top);
      const list = A.el("div", "fa-list");
      if (P.observing && !P.observing.item && !known.has(P.observing.dimension)) {
        const row = A.el("div", "fa-item");
        row.append(observationForm(P.observing));
        list.append(row);
      }
      for (const item of items) {
        const row = A.el("div", "fa-item");
        if (item.legacyLabel) row.append(A.el("div", "fa-note", "原关联：" + item.legacyLabel));
        row.append(observationLine(item));
        list.append(row);
      }
      if (list.childNodes.length) block.append(list);
      return block;
    }
    function startObservation(item, dimension = "") {
      if (A.restricted) return;
      const entry = item ? "observation:" + item.id : "observation:new";
      const draft = A.draft(entry);
      const base = draft.payload || { judgement: item?.judgement || "", note: item?.note || "",
        dimension: item ? item.dimension || "" : dimension };
      const baseVersion = draft.payload && Number.isInteger(draft.baseVersion) ? draft.baseVersion : item?.version ?? null;
      P.observing = { entry, item, draft, ...base, baseVersion, restored: draft.restored };
      // The editor opens inside the chart that owns its dimension.
      if (result() && base.dimension && owns(base.dimension) && !VALUE_KEYS.has(base.dimension))
        P.selected = C.cardOf(base.dimension) || "big5";
      render();
    }
    function observationForm(state) {
      const form = A.el("div", "fa-form");
      const judgement = A.el("input");
      judgement.type = "text";
      judgement.maxLength = 200;
      judgement.placeholder = "你的判断，例如：熟了以后说话很直接";
      judgement.value = state.judgement;
      judgement.dataset.key = state.entry + ":judgement";
      const note = A.el("textarea");
      note.placeholder = "依据备注（可选）";
      note.value = state.note;
      note.dataset.key = state.entry + ":note";
      const dimension = A.el("select");
      dimension.setAttribute("aria-label", "关联的维度");
      const options = [["", "不关联维度"], ["mbti", label("mbti")],
        ...(shared.data.catalog || []).map(item => [item.id, label(item.id)]), ...VALUE_ITEMS];
      for (const [value, text] of options) {
        const option = A.el("option", "", text);
        option.value = value;
        dimension.append(option);
      }
      if (state.dimension && !options.some(([value]) => value === state.dimension)) {
        const option = A.el("option", "", "原关联维度");
        option.value = state.dimension;
        dimension.append(option);
      }
      dimension.value = state.dimension || "";
      const unsaved = A.el("span", "fa-unsaved", state.restored ? "未保存（已恢复上次编辑）" : "");
      const changed = () => {
        Object.assign(state, { judgement: judgement.value, note: note.value, dimension: dimension.value });
        unsaved.textContent = "未保存";
        A.updateDraft(state.draft, { judgement: state.judgement, note: state.note, dimension: state.dimension },
          state.baseVersion);
      };
      for (const input of [judgement, note, dimension]) input.addEventListener("input", changed);
      const finish = () => { P.observing = null; void load(); };
      const save = A.button("保存", "fa-primary", async () => {
        save.disabled = true;
        try {
          await A.host("observations.save", { id: state.item?.id ?? null,
            judgement: state.judgement, note: state.note, dimension: state.dimension || null,
            expectedVersion: state.baseVersion });
          await A.closeDraft(state.entry);
          finish();
        } catch (error) { save.disabled = false; A.toast(error.message || "保存失败"); }
      });
      const cancel = A.button("放弃", "fa-plain", async () => { await A.closeDraft(state.entry); finish(); });
      const actions = A.el("div", "fa-form-actions");
      actions.append(save, cancel, unsaved);
      form.append(judgement, note, dimension, actions);
      return form;
    }
    async function deleteObservation(item) {
      if (!await A.confirm("删除这条观察？", "删除")) return;
      try {
        await A.host("observations.save", { action: "delete", id: item.id });
        void load();
      } catch (error) { A.toast(error.message || "删除失败"); }
    }

    const page = {
      load, render,
      // Restores the drafts whose dimension belongs to this tab; 不关联维度 drafts live on 性格分析.
      async restoreDrafts(drafts) {
        if (!shared.data) await fetchData();
        let restored = false;
        for (const [entry, saved] of drafts) if (entry.startsWith("observation:")) {
          const id = entry.slice(12);
          const item = id === "new" ? null : shared.data?.observations.find(candidate => candidate.id === id);
          const dimension = item ? item.dimension : saved?.payload?.dimension || "";
          if ((id === "new" || item) && (dimension && owns(dimension) || !dimension && tab === "personality" ||
              dimension && !C.tabOf(dimension) && dimension !== "mbti" && !VALUE_KEYS.has(dimension) && tab === "personality")) {
            startObservation(item, id === "new" ? dimension : "");
            restored = true;
          }
        }
        return restored;
      },
    };
    shared.pages.push(page);
    return page;
  }

  window.FriendPersona = make("personality", "pagePersona");
  window.FriendValues = make("values", "pageValues");
})();
