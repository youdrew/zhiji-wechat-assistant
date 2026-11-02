"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 好友概述: local facts, the recent-conversation summary and next step, and pointers to the other pages.
(function () {
  const A = window.FriendApp;
  const O = { data: null, request: 0 };

  function ago(ms) {
    if (!Number.isFinite(ms)) return "—";
    const days = Math.floor((Date.now() - ms) / 86400000);
    return days <= 0 ? "今天" : days === 1 ? "昨天" : days < 30 ? `${days} 天前` : A.formatDate(ms);
  }
  function range(from, to) {
    const left = A.formatDate(from), right = A.formatDate(to);
    return left === right ? left : `${left} 至 ${right}`;
  }

  async function load() {
    const request = ++O.request;
    try {
      const data = await A.rpc("page.overview");
      if (request !== O.request) return;
      O.data = data;
      render();
    } catch (error) {
      if (request === O.request && !A.masked)
        A.byId("pageOverview").replaceChildren(A.el("div", "fa-empty", error.message || "概述读取失败"));
    }
  }

  function section(title, content, aside) {
    const node = A.el("section", "fa-card");
    const head = A.el("div", "fa-card-head");
    head.append(A.el("h3", "", title));
    if (aside) head.append(A.el("span", "fa-spacer"), aside);
    node.append(head, content);
    return node;
  }
  function render() {
    const root = A.byId("pageOverview");
    if (A.masked || !O.data) return;
    const data = O.data, facts = data.facts;
    root.replaceChildren();
    const reading = facts.historyState !== "ready";
    const line = [facts.firstMs ? "可读记录最早 " + A.formatDate(facts.firstMs) : null,
      `共 ${facts.messages} 条消息${reading ? "（统计中）" : ""}`,
      facts.lastMs ? "最近联系 " + ago(facts.lastMs) : null,
      "联系目标：" + facts.goal + (facts.goalIsDefault ? "（默认）" : "")].filter(Boolean).join(" · ");
    root.append(A.el("p", "fa-facts", line));

    if (data.summary) {
      const body = A.el("div");
      body.append(A.el("p", "fa-hero-text", data.summary.summary));
      const meta = A.el("div", "fa-item-meta");
      meta.append(A.el("span", "", `依据 ${range(data.summary.fromMs, data.summary.toMs)} 的 ${data.summary.messages} 条消息` +
        (data.summary.complete ? "" : "（较长，只含最近部分）")));
      if (data.stale) meta.append(A.el("span", "fa-mark warn", "有新消息，下次分析时更新"));
      body.append(meta);
      root.append(section("最近聊到了这里", body,
        A.button("查看原文", "fa-plain", () => showEvidence(data.summary.evidence))));
    } else root.append(section("最近聊到了这里", A.el("p", "fa-note", "开始分析后显示。")));

    root.append(section("下一步", data.next ? A.el("p", "fa-hero-text", data.next.next) :
      A.el("p", "fa-note", "开始分析后显示。")));

    if (data.about.length) {
      const list = A.el("div", "fa-list");
      for (const item of data.about) {
        const row = A.button("", "fa-row-link", () => A.selectTab(item.kind === "persona" ? "persona" : "memories"));
        row.append(A.el("span", "fa-row-label", item.label || ""), A.el("span", "fa-row-text", item.text));
        if (item.confidence) row.append(A.el("span", "fa-mark", item.confidence + " · 推测"));
        list.append(row);
      }
      root.append(section("关于 TA", list));
    } else root.append(section("关于 TA", A.el("p", "fa-note", "开始分析后显示。")));

    if (data.remember.length) {
      const list = A.el("div", "fa-list");
      for (const item of data.remember) {
        const row = A.el("div", "fa-item");
        row.append(A.el("div", "fa-item-text", item.text));
        list.append(row);
      }
      root.append(section("值得记住", list, A.button("全部", "fa-plain", () => A.selectTab("memories"))));
    }
    if (data.change) {
      const difference = data.change.days - data.change.previousDays;
      const trend = difference === 0 ? "与前一个月相同" : `比前一个月${difference > 0 ? "多" : "少"} ${Math.abs(difference)} 天`;
      root.append(section("关系变化", A.el("p", "fa-hero-text", `${data.change.label} 联系 ${data.change.days} 天，${trend}。`)));
    }
    if (data.summary) {
      const source = [`依据 ${range(data.summary.fromMs, data.summary.toMs)} 的对话`];
      if (data.summary.model) source.push(data.summary.model);
      source.push(A.formatDate(data.summary.createdAtMs, true));
      root.append(A.el("p", "fa-note", source.join(" · ")));
    }
  }

  function showEvidence(items) {
    const body = A.openSheet("摘要依据的原文", "overview-evidence");
    const list = A.el("div");
    for (const item of items || []) {
      const row = A.el("div", "fa-msg");
      row.append(A.el("div", "fa-msg-head", `${item.speaker} · ${A.formatDate(item.timeMs, true)}`),
        A.el("div", "fa-msg-text", item.text));
      const more = A.button("看上下文", "fa-plain", () => showContext(item.msgId, row, more));
      row.append(more);
      list.append(row);
    }
    body.append(list, A.el("p", "fa-note", "摘要是对这些原文的概括，不代表对方的内心。"));
  }
  async function showContext(msgId, container, trigger) {
    trigger.disabled = true;
    try {
      const data = await A.host("message.context", { id: msgId });
      container.append(A.contextList(data));
      trigger.remove();
    } catch (error) { trigger.disabled = false; A.toast(error.message || "读取失败"); }
  }

  window.FriendOverview = { load };
})();
