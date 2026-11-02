"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 聊天分析: how the friend responds to me, from block labels of both sides, plus local reply
// statistics. The folded 旧版画像 is the earlier one-sided portrait, read only when it is opened.
(function () {
  const A = window.FriendApp;
  const C = { data: null, request: 0, side: "other", legacy: null, legacyOpen: false, legacyRequest: 0 };
  const ASK = { 提问: "你提问后", 分享: "你分享后", 邀请: "你邀请后", 关心: "你关心对方后", 玩笑: "你开玩笑后",
    请求帮助: "你请求帮助后", 回应: "你回应对方后", 混合: "意图混合时", 其他: "其他情况", 未判定: "意图未判定时" };
  const percent = value => `${Math.max(1, Math.round(value * 100))}%`;
  function range(from, to) {
    if (!from) return "";
    const left = A.formatDate(from), right = A.formatDate(to);
    return left === right ? left : `${left} 至 ${right}`;
  }

  async function load() {
    const request = ++C.request;
    try {
      const data = await A.rpc("page.chat");
      if (request !== C.request) return;
      C.data = data;
      render();
      if (!data.configured && !data.statistics && !C.legacy) void loadLegacy();
    } catch (error) {
      if (request === C.request && !A.masked)
        A.byId("pageChat").replaceChildren(A.el("div", "fa-empty", error.message || "聊天分析读取失败"));
    }
  }

  function section(title, content, aside) {
    const node = A.el("section", "fa-card");
    const head = A.el("div", "fa-card-head");
    head.append(A.el("h3", "", title));
    if (aside) head.append(A.el("span", "fa-spacer"), typeof aside === "string" ? A.el("span", "fa-card-aside", aside) : aside);
    node.append(head, content);
    return node;
  }
  // One labelled bar. A null fraction has no ratio: a dashed empty track, never a filled 0.
  function meter(label, fraction, value, onClick) {
    const row = onClick ? A.button("", "fa-meter fa-meter-button", onClick) : A.el("div", "fa-meter");
    const track = A.el("span", fraction === null ? "fa-track unknown" : "fa-track");
    track.setAttribute("aria-hidden", "true");
    const fill = A.el("i");
    if (fraction !== null) fill.style.width = `${Math.round(Math.max(0, Math.min(1, fraction)) * 1000) / 10}%`;
    track.append(fill);
    row.append(A.el("span", "fa-meter-label", label), track, A.el("span", "fa-meter-value", value));
    return row;
  }

  function render() {
    const root = A.byId("pageChat");
    if (A.masked || !C.data) return;
    const data = C.data, coverage = data.coverage;
    const open = root.querySelector("details[data-key='legacy']")?.open ?? C.legacyOpen;
    root.replaceChildren();
    if (coverage) {
      const line = [coverage.fromMs ? `已分析 ${range(coverage.fromMs, coverage.toMs)} 的聊天，共 ${coverage.blocks} 段连续消息` :
        "还没有可分析的完整互动"];
      if (coverage.pending) line.push(`另有 ${coverage.pending} 条待下次分析`);
      if (coverage.missingTextBlocks) line.push(`${coverage.missingTextBlocks} 个文本块的标注尚未完成，需补齐`);
      if (coverage.outdated) line.push("旧版结果，需重新分析");
      root.append(A.el("p", "fa-facts", line.join(" · ")));
    }
    root.append(replyCard(data.reply, data.historyReady));
    if (data.statistics) {
      root.append(responseCard(data.statistics.responses));
      const invites = data.statistics.invites;
      if (invites.total) root.append(inviteCard(invites));
      root.append(signalCards(data.statistics.signals));
      root.append(keywordCard(data.statistics.keywords));
    } else if (!data.configured) {
      root.append(section("对方如何回应我", A.el("p", "fa-note", "回应方式需要配置 API 模型。")));
      if (C.legacy) root.append(...legacyCards(C.legacy));
    } else root.append(A.el("p", "fa-note", "开始分析后显示双方的意图与情绪，以及对方如何回应你。"));
    if (data.configured || data.statistics) root.append(legacyDetails(open));
    root.append(methodDetails());
  }

  function replyCard(reply, ready) {
    const body = A.el("div");
    if (!reply.eligible && !reply.observing) body.append(A.el("p", "fa-note", "还没有可统计的互动。"));
    else {
      body.append(A.el("p", "fa-hero-text", reply.rate === null ?
        `仅 ${reply.eligible} 次可判定，暂不给比例（对方 24 小时内回应 ${reply.replied} 次）` :
        `对方在 24 小时内回应了 ${percent(reply.rate)}（${reply.replied} / ${reply.eligible} 次）`));
      const meta = A.el("div", "fa-item-meta");
      if (reply.medianSeconds !== null) meta.append(A.el("span", "", "回复时间中位数 " + A.formatDuration(reply.medianSeconds)));
      if (reply.observing) meta.append(A.el("span", "", `观察中 ${reply.observing} 次（不足 24 小时）`));
      if (reply.excluded) meta.append(A.el("span", "", `${reply.excluded} 次顺序或时间无法确认，未计入`));
      if (!ready) meta.append(A.el("span", "fa-mark warn", "聊天历史仍在读取，按已读部分统计"));
      if (meta.childNodes.length) body.append(meta);
    }
    return section("回复率", body, "本地统计");
  }

  function responseCard(groups) {
    const list = A.el("div", "fa-list");
    for (const group of groups) {
      const row = A.el("div", "fa-item");
      const head = A.el("div", "fa-persona-head");
      head.append(A.el("span", "fa-row-label", ASK[group.ask] || group.ask), A.el("span", "fa-row-text", `${group.rounds} 次互动`));
      row.append(head);
      const meters = A.el("div", "fa-meters");
      for (const item of group.items)
        meters.append(meter(item.label, item.share, item.share === null ? `${item.count} 次` : percent(item.share),
          () => showExamples(`${ASK[group.ask] || group.ask} · ${item.label}`, item.examples)));
      if (group.items.length) row.append(meters);
      const meta = A.el("div", "fa-item-meta");
      if (group.determinable < 5) meta.append(A.el("span", "", `仅 ${group.determinable} 次可判定，暂不给比例`));
      if (group.undetermined) meta.append(A.el("span", "", `${group.undetermined} 次无法判断`));
      if (group.fromMs) meta.append(A.el("span", "", range(group.fromMs, group.toMs)));
      if (meta.childNodes.length) row.append(meta);
      list.append(row);
    }
    if (!groups.length) list.append(A.el("p", "fa-note", "还没有你发言后对方在 24 小时内接话的互动。"));
    return section("对方如何回应我", list, "点一行看原文");
  }

  function inviteCard(invites) {
    const counts = invites.counts;
    const body = A.el("div");
    body.append(A.el("p", "fa-hero-text", ["明确接受", "明确婉拒", "未明确答复", "无法判断"]
      .filter(label => counts[label]).map(label => `${label} ${counts[label]} 次`).join(" · ")));
    body.append(A.el("p", "fa-note", (invites.withoutReply ? `未明确答复含 ${invites.withoutReply} 次对方没有接话。` : "") +
      "没有明确答复不当作拒绝。"));
    return section("邀请", body, `共 ${invites.total} 次`);
  }

  // Shares of labelled blocks; the long tail becomes 其他 so each side stays a short list.
  function shareList(group, limit = 5) {
    const list = A.el("div", "fa-meters");
    const items = [...group.items].sort((left, right) => right.count - left.count);
    const top = items.slice(0, limit), rest = items.slice(limit).reduce((sum, item) => sum + item.count, 0);
    for (const item of top) list.append(meter(item.label, item.share, percent(item.share)));
    if (rest) list.append(meter("其他", rest / group.total, percent(rest / group.total)));
    if (!group.total) list.append(A.el("p", "fa-note", "暂无"));
    return list;
  }
  function compare(signals, key) {
    const grid = A.el("div", "fa-compare");
    for (const [side, name] of [["self", "我"], ["other", "对方"]]) {
      const column = A.el("div");
      column.append(A.el("div", "fa-compare-head", `${name} · ${signals[side].labelled} 段`), shareList(signals[side][key]));
      grid.append(column);
    }
    return grid;
  }
  function signalCards(signals) {
    const pair = A.el("div", "fa-cards");
    pair.append(section("交流意图", compare(signals, "intent")), section("情绪信号", compare(signals, "emotion")));
    return pair;
  }
  function keywordCard(keywords) {
    const segment = A.el("div", "fa-segment");
    for (const [value, label] of [["other", "对方"], ["self", "我"]])
      segment.append(A.button(label, "", () => { C.side = value; render(); }, { "aria-pressed": String(C.side === value) }));
    const chips = A.el("div", "fa-chips");
    for (const item of keywords[C.side] || []) {
      const chip = A.el("span", "fa-chip", item.word);
      chip.append(A.el("small", "", `${item.count} 次`));
      chips.append(chip);
    }
    if (!chips.childNodes.length) chips.append(A.el("span", "fa-note", "暂无出现两次以上的词"));
    return section("高频词", chips, segment);
  }

  // -- examples: my block and the friend's answer, read from the managed history ----------------
  let sheetToken = 0;
  async function showExamples(title, ids) {
    const token = ++sheetToken;
    const body = A.openSheet(title, "chat-round");
    if (!ids.length) { body.append(A.el("p", "fa-note", "没有可显示的例子。")); return; }
    const loading = A.el("p", "fa-note", "读取中…");
    body.append(loading);
    try {
      const results = await Promise.all(ids.map(id => A.rpc("page.round", { id })));
      if (token !== sheetToken || A.sheetOwner !== "chat-round") return;
      loading.remove();
      for (const result of results) {
        const box = A.el("div", "fa-round");
        for (const block of result.blocks) for (const message of block.messages) {
          const row = A.el("div", "fa-msg");
          row.append(A.el("div", "fa-msg-head", `${message.speaker} · ${A.formatDate(message.timeMs, true)}`),
            A.el("div", "fa-msg-text", message.text));
          box.append(row);
        }
        if (!result.blocks.length) box.append(A.el("p", "fa-note", "原文已不在当前可读历史中。"));
        body.append(box);
      }
      body.append(A.el("p", "fa-note", "每个例子是你的一段消息和对方随后的回应，最多显示 3 个。"));
    } catch (error) { if (token === sheetToken) loading.textContent = error.message || "读取失败"; }
  }

  // -- 旧版画像: the earlier one-sided portrait, kept for reference and never updated here --------
  const TRAITS = { socialEnergy: "表达活力", humor: "幽默表达", composure: "情绪平和", initiative: "话题主动",
    care: "关怀支持", affection: "亲近表达" };
  const score = value => Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
  // Model probabilities averaged over messages need not add up to 1; the tail becomes 其他.
  function shares(entries, limit = 4) {
    const valid = (Array.isArray(entries) ? entries : []).filter(item => typeof item?.label === "string" &&
      Number.isFinite(item.probability) && item.probability > 0).sort((left, right) => right.probability - left.probability);
    const top = valid.slice(0, limit).filter(item => item.probability >= 0.005);
    const rest = valid.slice(top.length).reduce((sum, item) => sum + item.probability, 0);
    return [...top.map(item => ({ label: item.label, share: Math.min(1, item.probability) })),
      ...(rest >= 0.005 ? [{ label: "其他", share: Math.min(1, rest) }] : [])];
  }
  function statisticsModel(profile, source, analyzed, total, local = false) {
    return { local, source, emotions: shares(profile.emotion), intents: shares(profile.intent),
      words: (profile.keywords || []).filter(item => typeof item?.word === "string"),
      traits: (profile.traits || []).map(item => ({ label: item.label, value: score(Math.round(item.val)) })),
      affinity: score(profile.affinity), analyzed, total };
  }
  // One saved portrait of the retired analysis: local Laya statistics or an API source.
  function legacyModel(item) {
    if (!item) return { empty: true };
    if (item.kind === "statistics") return statisticsModel(item, item.label, item.analyzed, item.total, item.source === "local");
    const portrait = item.portrait || {};
    return { source: item.label, summary: portrait.summary, communication: portrait.communication,
      emotion: portrait.emotionExpression, preferences: portrait.interactionPreferences,
      patterns: portrait.patterns || [], boundaries: portrait.boundaries || [], uncertain: portrait.uncertain || [],
      topics: portrait.topics || [], traits: Object.entries(TRAITS).map(([key, label]) => ({ label, value: score(portrait.traits?.[key]) })),
      affinity: score(portrait.affinity), analyzed: item.analyzed, total: item.total };
  }
  async function loadLegacy() {
    const request = ++C.legacyRequest;
    try {
      const data = await A.host("legacy.portrait");
      if (request !== C.legacyRequest) return;
      C.legacy = data.error ? { error: data.error } : legacyModel(Array.isArray(data.items) ? data.items[0] : null);
    } catch (error) {
      if (request !== C.legacyRequest) return;
      C.legacy = { error: error.message || "旧版画像读取失败" };
    }
    render();
  }
  function legacyDetails(open) {
    const details = A.el("details", "fa-details");
    details.dataset.key = "legacy";
    details.open = open;
    details.append(A.el("summary", "", "已有分析详情（旧版画像）"));
    details.addEventListener("toggle", () => {
      C.legacyOpen = details.open;
      if (details.open && !C.legacy) void loadLegacy();
    });
    if (C.legacy) details.append(...legacyCards(C.legacy));
    else if (open) details.append(A.el("p", "fa-note", "读取中…"));
    return details;
  }
  function legacyCards(model) {
    if (model.error) return [A.el("p", "fa-note", model.error)];
    if (model.empty) return [A.el("p", "fa-note", "没有旧版画像。")];
    const nodes = [];
    if (model.summary) nodes.push(section("旧版摘要", A.el("p", "fa-text", model.summary)));
    const style = A.el("dl", "fa-kv");
    for (const [label, value] of [["交流方式", model.communication], ["情绪表达", model.emotion], ["互动偏好", model.preferences]])
      if (value) style.append(A.el("dt", "", label), A.el("dd", "", value));
    if (style.childNodes.length) nodes.push(section("沟通方式", style));
    const known = model.traits.some(item => item.value !== null);
    const behaviour = A.el("div");
    if (known) {
      const meters = A.el("div", "fa-meters fa-meters-two");
      for (const item of model.traits) meters.append(meter(item.label, item.value === null ? null : item.value / 100,
        item.value === null ? "未知" : String(item.value)));
      behaviour.append(meters);
    }
    for (const pattern of model.patterns || []) behaviour.append(A.el("div", "fa-text fa-pattern", "· " + pattern));
    if (behaviour.childNodes.length) nodes.push(section("互动表现", behaviour, known ? "按聊天表现估计，满分 100" : ""));
    const pair = A.el("div", "fa-cards");
    for (const [title, items] of [["对方的情绪信号", model.emotions], ["对方的交流意图", model.intents]]) if ((items || []).length) {
      const list = A.el("div", "fa-meters");
      for (const item of items) list.append(meter(item.label, item.share, percent(item.share)));
      pair.append(section(title, list, "约占对方消息"));
    }
    if (pair.childNodes.length) nodes.push(pair);
    if ((model.words || []).length || (model.topics || []).length) {
      const chips = A.el("div", "fa-chips");
      for (const item of model.words || []) {
        const chip = A.el("span", "fa-chip", item.word);
        if (Number.isInteger(item.count)) chip.append(A.el("small", "", `${item.count} 次`));
        chips.append(chip);
      }
      for (const topic of model.topics || []) chips.append(A.el("span", "fa-chip", topic));
      nodes.push(section(model.words?.length ? "对方高频词" : "常聊话题", chips));
    }
    const rating = A.el("div");
    const affinity = A.el("div", "fa-meters");
    affinity.append(meter("互动评分", model.affinity === null ? null : model.affinity / 100,
      model.affinity === null ? "待分析" : String(model.affinity)));
    rating.append(affinity, A.el("p", "fa-note", "原“好感度”。评分方法已知存在问题，后续会重新设计，仅供参考。"));
    nodes.push(section("互动评分", rating));
    const limits = A.el("dl", "fa-kv");
    if ((model.boundaries || []).length) limits.append(A.el("dt", "", "明确边界"), A.el("dd", "", model.boundaries.join("；")));
    if ((model.uncertain || []).length) limits.append(A.el("dt", "", "证据不足"), A.el("dd", "", model.uncertain.join("；")));
    if (limits.childNodes.length) nodes.push(section("其他", limits));
    const coverage = Number.isFinite(model.total) && model.total > 0 ?
      ` · 已分析 ${Math.min(model.total, Number(model.analyzed) || 0)} / ${model.total} 条对方文本` : "";
    nodes.push(A.el("p", "fa-note", `${model.source}${coverage} · 推测。旧版只分析对方消息，此处不再更新；MBTI 已移到性格分析。`));
    return nodes;
  }

  function methodDetails() {
    const details = A.el("details", "fa-details");
    details.append(A.el("summary", "", "统计口径"));
    const list = A.el("dl", "fa-kv");
    for (const [key, value] of [
      ["一段消息", "同一方连续发出的消息算一段；你连发三条、对方回一条只算一次互动。"],
      ["回复率", "你的一段消息之后 24 小时内对方接话算回应，只回表情、图片或语音也算；不足 24 小时的单列“观察中”。"],
      ["回应方式", "由模型按原文逐段标注，只描述可见的表达；慢回复或简短回复不代表冷淡。可判定少于 5 次时不给比例。"],
      ["邀请", "只有对方明确答复才算接受或婉拒，没有明确答复不当作拒绝。"],
      ["未计入", "系统消息、同一秒的双方消息与时间未知的消息不参与配对；引用的内容不算发言人本人的表达。"]])
      list.append(A.el("dt", "", key), A.el("dd", "", value));
    details.append(list);
    return details;
  }

  window.FriendChat = { load };
})();
