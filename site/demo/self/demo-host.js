"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The public demo supplies the renderer with fictional records and in-memory replies.
(() => {
  const account = "演示账号";
  const context = Object.freeze({ account, user: "", theme: "light", surface: "self-analysis-window" });
  const firstPrompt = "周六一早去公园走走？";
  const nextPrompt = "那下午三点见？";
  const samples = [
    {
      name: "苏禾",
      input: "苏禾：周日一早去逛展？",
      reply: "想去呀！不过早上我想睡个懒觉，下午见？",
      matchKinds: ["semantic"],
    },
    {
      name: "林沐",
      input: "林沐：下午想去公园走一圈吗？",
      reply: "好呀，走累了就找家店坐坐，别把行程排太满。",
      matchKinds: ["semantic"],
    },
  ];
  const initialReply = ["想去呀，不过上午我想睡个懒觉。", "下午见？走累了就找家咖啡馆坐坐。"];
  const followupReply = ["好呀，三点公园门口见。", "路线到时候边走边挑，别排太满。"];
  const tracks = [
    { name: "工作", color: "#07c160", visible: true, builtin: true },
    { name: "兴趣", color: "#576b95", visible: true, builtin: true },
    { name: "关系", color: "#c2841c", visible: true, builtin: true },
  ];
  const events = [
    ["2026-10", "和林沐去了植物园", "关系", "上次一起去植物园很开心，下次还想带相机去。"],
    ["2026-06", "开始每周画一张速写", "兴趣", "周末找家安静的咖啡馆，慢慢画一张。"],
    ["2025-12", "做完第一个独立项目", "工作", "终于把第一版交出去了。下班想去散个步。"],
  ].map(([month, title, track, phrase], index) => ({
    id: "demo-event-" + index,
    title, track, phrase,
    status: "occurred",
    importance: index === 0 ? "important" : "general",
    level: "confirmed",
    startMs: Date.parse(month + "-15T12:00:00+09:00"),
    endMs: null,
    precision: "month",
    whenText: month.replace("-", "年") + "月",
    evidenceCount: 1,
  }));
  const timeline = {
    version: 1, months: 6, periods: [], events, tracks, eventsTotal: events.length,
    nextOffset: null, coverage: { earliestMs: events.at(-1).startMs }, run: { state: "done" },
  };
  const inference = (text, quote, position) => ({
    text, confidence: "有一定依据", scope: "仅限演示聊天",
    position,
    counts: position < 0 ? { low: 1, high: 0, mixed: 0 } : { low: 0, high: 1, mixed: 0 },
    evidence: [{ id: "demo-quote-" + position, chat: "林沐", time: events[0].startMs, quote }],
    counter: [],
  });
  const persona = {
    version: 1, coverage: { ownMessages: 42, analyzedMessages: 42 },
    dimensions: [
      { key: "big5_o", label: "开放性", inference: inference("愿意尝试新的兴趣，也会按自己的节奏来。", "这次我想试试胶片摄影，先随便拍几张。", 0.5) },
      { key: "big5_e", label: "外向性", inference: inference("在和熟悉朋友相处时，更喜欢轻松、留有余地的安排。", "下午见？走累了就找家店坐坐。", -0.35) },
    ],
  };
  const values = {
    version: 1, coverage: { ownMessages: 42, analyzedMessages: 42 },
    dimensions: [{ key: "core", label: "价值底色", inference: inference("重视彼此照顾，也愿意直接说出自己的需要。", "我今天有点累，想早点回去。下周我请你喝咖啡？", 0.3) }],
  };
  const tags = {
    account, run: { state: "complete" }, counts: { messages: 42, pendingMessages: 0 },
    categories: ["兴趣"],
    tags: [{ key: "速写", name: "速写", category: "兴趣", relation: "interest", level: "confirmed", mentions: 3, chats: 2, days: 3 }],
  };
  const status = {
    account, source: { ready: true },
    build: { state: "complete", sampleCount: 18, messageCount: 42, totalConversations: 4, processedConversations: 4, failedCount: 0 },
    coverage: { complete: true, catalogComplete: true, sampleCount: 18, messageCount: 42, discoveredConversations: 4, indexedConversations: 4 },
    embedding: { state: "ready", ready: true, indexState: "ready" },
    retrieval: { mode: "hybrid", semanticReady: true, indexedSamples: 18, pendingSamples: 0, totalSamples: 18 },
  };

  function reply({ messages }) {
    const turn = messages?.filter(message => message.from === "friend").at(-1)?.text || "";
    const followup = /三点|公园门口/.test(turn);
    const supported = followup || /周[六日末]|散步|公园|逛展|咖啡/.test(turn);
    const answer = supported
      ? (followup ? followupReply : initialReply)
      : ["试试下方的周末邀约示例，看看这个模拟对话会怎样回答。"];
    return { id: "demo-reply", state: "done", result: { messages: answer }, sources: supported ? samples : [] };
  }

  function timelineEvidence({ id }) {
    const event = events.find(item => item.id === id);
    return { total: event ? 1 : 0, items: event ? [{
      chat: "林沐", time: event.startMs, phrase: event.phrase, text: event.phrase,
      before: [{ side: "other", text: "最近有什么想记下来的事？" }],
      after: [{ side: "other", text: "听起来不错，下次再一起去。" }],
    }] : [] };
  }

  const methods = {
    "self.status": () => status,
    "self.analysisSettings": params => {
      if (params.outputs) throw new Error("分析设置可在客户端更改。");
      return { outputs: ["profile", "tags", "timeline", "persona", "values", "library"], depth: "deep" };
    },
    "self.analysisStatus": () => ({ state: "idle" }),
    "self.draftStatus": () => ({ state: "idle" }),
    "self.exclusions": () => ({ sources: [] }),
    "self.search": ({ query }) => ({
      mode: "hybrid", sources: /周[六日末]|散步|公园|逛展|咖啡|见面|邀请/.test(query) ? samples : [],
    }),
    "self.reply": reply,
    "self.timeline": () => timeline,
    "self.timelineEvidence": timelineEvidence,
    "self.persona": () => persona,
    "self.values": () => values,
    "self.periods": () => ({ periods: [] }),
    "self.worldview": () => ({ periods: [] }),
    "self.tags": () => tags,
    "self.tagEvidence": () => ({ total: 1, items: [{
      chat: "林沐", time: events[1].startMs, text: events[1].phrase,
      quote: "周末找家安静的咖啡馆，慢慢画一张。",
      reason: "提到会主动安排时间画速写。",
    }] }),
  };
  const host = {
    "model.status": () => ({ configured: true }),
    "self-profile.read": () => ({
      background: "喜欢散步、逛展和找安静的咖啡馆。最近开始练习速写。",
      style: "简短随和，先回应对方的邀请，再商量一个舒服的时间。",
      version: 1,
    }),
  };
  window.kyfHost = Object.freeze({
    ready: async () => context,
    context: () => context,
    on: () => {},
    async host(method, params = {}) {
      if (!Object.hasOwn(host, method)) throw new Error("设置可在客户端打开。");
      return host[method](params);
    },
    async rpc(method, params = {}, scope) {
      if (scope?.account !== account || !Object.hasOwn(methods, method)) {
        throw new Error("这项操作可在客户端使用。");
      }
      return methods[method](params);
    },
  });

  const byId = id => document.getElementById(id);
  byId("background").readOnly = true;
  byId("style").readOnly = true;
  const readOnlyButtons = ".tl-month, .tl-mark";
  new MutationObserver(() => {
    document.querySelectorAll(readOnlyButtons).forEach(button => {
      button.disabled = true;
      button.title = "编辑时间线请在客户端操作";
    });
  }).observe(byId("timelineList"), { childList: true, subtree: true });
  byId("timelineList").addEventListener("contextmenu", event => {
    if (!event.target.closest(".tl-event-head")) return;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  function selectPrompt(prompt) {
    const input = byId("trialInput");
    input.value = prompt;
    input.focus();
  }
  byId("demoFirstPrompt").addEventListener("click", () => selectPrompt(firstPrompt));
  byId("demoNextPrompt").addEventListener("click", () => selectPrompt(nextPrompt));
  window.addEventListener("DOMContentLoaded", () => {
    byId("tab-trial").click();
    byId("trialInput").value = firstPrompt;
    byId("trialSend").click();
  });
})();
