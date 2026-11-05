"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The unchanged public renderer receives only in-memory fictional records.
(() => {
  const data = window.SelfDemoData;
  const account = "演示账号";
  const context = Object.freeze({ account, user: "", theme: "light", surface: "self-analysis-window" });
  const prompts = [
    ["demoFirstPrompt", "周六一早去公园走走？"],
    ["demoNextPrompt", "那下午三点见？"],
    ["demoRestPrompt", "我这周有点累，怕周末出门太折腾。"],
    ["demoArtPrompt", "下周有空一起看看摄影展吗？"],
  ];
  const definition = {
    history: "同步本人文字", library: "表达知识库", timeline: "时间线",
    evidence: "提取共同证据", profile: "本人资料", tags: "标签",
    persona: "性格分析", periods: "性格时间线", values: "三观分析",
    worldview: "三观时间线", semantic: "本地语义索引",
  };
  const status = {
    account, source: { ready: true },
    build: { state: "complete", sampleCount: data.samples.length, messageCount: 184, totalConversations: 4, processedConversations: 4, failedCount: 0 },
    coverage: { complete: true, catalogComplete: true, sampleCount: data.samples.length, messageCount: 184, discoveredConversations: 4, indexedConversations: 4 },
    embedding: { state: "ready", ready: true, indexState: "ready" },
    retrieval: { mode: "hybrid", semanticReady: true, indexedSamples: data.samples.length, pendingSamples: 0, totalSamples: data.samples.length },
  };
  const timeline = {
    version: 1, months: 6, periods: data.periods, events: data.events, tracks: data.tracks,
    eventsTotal: data.events.length, nextOffset: null,
    coverage: { earliestMs: data.at("2024-01-01") }, run: { state: "done" },
  };
  const replyCases = [
    {
      match: /累|休息|折腾|加班|改天/,
      messages: ["那我们就不赶行程了。", "想见你的话，找家近一点的店坐坐也挺好。实在累就改天，先照顾自己。"],
      sources: [3, 4, 8],
    },
    {
      match: /三点|几点|门口|下午见|在哪里/,
      messages: ["好呀，三点公园门口见。", "我带相机，路线到时候边走边挑，别排太满。"],
      sources: [2, 1, 4],
    },
    {
      match: /展|摄影|速写/,
      messages: ["想去！上次聊的那组街景就在里面吧？", "下周日下午我有空，我们慢慢看一个展，之后再找地方喝点东西。"],
      sources: [5, 8, 10],
    },
    {
      match: /难受|难过|烦|不开心|聊聊/,
      messages: ["那先不用一下说清楚。", "想从哪一点开始就从哪一点开始，我在听。要是愿意，晚上一起走走？"],
      sources: [7, 3],
    },
    {
      match: /周[六日末]|散步|公园|咖啡|有空/,
      messages: ["想去呀，不过上午我想睡个懒觉。", "下午见？走累了就找家咖啡馆坐坐。"],
      sources: [0, 1, 2],
    },
  ];
  function reply({ messages = [] }) {
    const turn = messages.filter(message => message.from === "friend").at(-1)?.text || "";
    const choice = replyCases.find(item => item.match.test(turn));
    return {
      id: "demo-self-reply", state: "done",
      result: { messages: choice?.messages || ["这里预设了周末邀约、确认时间、休息和看展的几段回复。选一句示例试试？"] },
      sources: choice ? choice.sources.map(index => data.samples[index]) : [],
    };
  }
  function search({ query = "", limit = 6 }) {
    const normalized = query.trim();
    const matching = data.samples.filter(item => (item.input + item.reply + item.keywords).includes(normalized));
    const related = /忙|累|改天|休息/.test(normalized) ? [3, 4, 6, 8]
      : /展|摄影|速写|兴趣/.test(normalized) ? [5, 8, 10, 0]
      : /邀请|见面|周末|周六|散步/.test(normalized) ? [0, 1, 2, 4, 5, 8] : [];
    const choices = new Set([...matching, ...related.map(index => data.samples[index])]);
    return { mode: "hybrid", sources: [...choices].slice(0, limit) };
  }
  function timelineEvidence({ id }) {
    const event = data.events.find(item => item.id === id);
    const period = data.periods.find(item => item.id === id);
    const source = event || data.events.find(item => item.track === period?.track);
    return { total: source ? 1 : 0, items: source ? [{
      chat: source.track === "工作" ? "周屿" : "林沐",
      time: source.startMs, phrase: source.phrase, text: source.phrase,
      before: [{ side: "other", text: "最近有什么想记下来的事？" }],
      after: [{ side: "other", text: "听起来不错，下次见面再慢慢聊。" }],
    }] : [] };
  }
  function periodDetail({ period, key }) {
    const selected = data.personalityPeriods.periods.find(item => item.id === period);
    const factor = selected?.factors[key];
    const dimension = data.persona.dimensions.find(item => item.key === key);
    const questions = data.questionKeys[key] || [];
    const score = factor?.score ?? 3;
    const whole = Math.floor(score), high = Math.round((score - whole) * questions.length);
    return { period, key, label: selected?.label || "", items: questions.map((signed, index) => {
      const item = Math.abs(signed);
      const value = whole + Number(index < high);
      const answer = signed < 0 ? 6 - value : value;
      return {
        item, text: data.questionTexts[item - 1], answers: [answer, answer, answer],
        reverse: signed < 0, status: "complete",
        why: "演示中按这些虚构聊天整理出这一期的表现。",
        evidence: [{ id: "demo-period-" + period + "-" + item, name: "林沐", time: data.at(period.slice(0, 4) + "-06-10"), k: "自述", s: answer >= 3 ? 1 : -1, text: dimension?.inference.evidence[0].quote || data.profile.style }],
      };
    }) };
  }
  const viewQuestions = {
    // Political question wording is from the shipped MIT-licensed 8values bank.
    pol_econ: [
      [1002, "政府有必要干预经济以保护消费者。", "It is necessary for the government to intervene in the economy to protect consumers."],
      [1004, "保持预算平衡比保障所有公民的福利更重要。", "It is better to maintain a balanced budget than to ensure welfare for all citizens."],
      [1007, "各尽所能，按需分配。", "From each according to his ability, to each according to his needs."],
    ],
    pol_lib: [
      [1028, "即使是在抗议威权政府时，暴力也是不可接受的。", "Even when protesting an authoritarian government, violence is not acceptable."],
      [1030, "我国的价值观应该尽可能广泛地传播。", "Our nation's values should be spread as much as possible."],
      [1032, "普通民众会做出糟糕的决定。", "The general populace makes poor decisions."],
    ],
    val_change: [
      [13, "我喜欢尝试没做过的事"], [14, "为了新机会我愿意换城市或换工作"], [15, "我想按自己的想法活，不走别人安排的路"],
      [16, "我更想要稳定的生活"], [17, "我不喜欢生活有太大变动"], [18, "按规矩来、守本分让我觉得踏实"],
    ],
    val_self: [
      [19, "我很想在事业上出人头地"], [20, "赚钱和社会地位对我很重要"], [21, "做事时我先考虑对自己有没有好处"],
      [22, "我愿意为身边的人牺牲自己的利益"], [23, "我在乎弱势的人过得怎么样"], [24, "公平比自己得利更重要"],
    ],
  };
  function worldviewDetail({ period, key }) {
    const selected = data.worldviewPeriods.periods.find(item => item.id === period);
    const dimension = data.persona.dimensions.find(item => item.key === key);
    return { period, key, label: selected?.label || "", items: (viewQuestions[key] || []).map(([item, text, english], index) => ({
      item, text, english: english || "", source: english ? "8values · 题目示例" : "项目自拟价值观题目",
      answers: index % 2 ? [3, 3, 3] : [4, 4, 4], status: "complete",
      why: "这里展示虚构聊天与对应题目之间的参考关系。",
      evidence: [{ id: "demo-world-" + period + "-" + item, k: "自述", s: 1, name: "苏禾", time: data.at(period.slice(0, 4) + "-05-12"), text: dimension?.inference.evidence[0].quote || data.profile.style }],
    })) };
  }
  const methods = {
    "self.status": () => status,
    "self.analysisSettings": params => {
      if (params.outputs) throw new Error("分析设置可在客户端更改。");
      return { outputs: ["profile", "tags", "timeline", "persona", "periods", "values", "worldview", "library"], depth: "deep" };
    },
    "self.analysisStatus": () => ({
      id: "demo-analysis", state: "done",
      steps: Object.entries(definition).map(([key, title]) => ({ key, title, state: "done" })),
    }),
    "self.draftStatus": () => ({ state: "idle" }),
    "self.exclusions": () => ({ sources: [] }),
    "self.search": search,
    "self.reply": reply,
    "self.timeline": () => timeline,
    "self.timelineEvidence": timelineEvidence,
    "self.persona": () => data.persona,
    "self.values": () => data.values,
    "self.periods": () => data.personalityPeriods,
    "self.periodDetail": periodDetail,
    "self.worldview": () => data.worldviewPeriods,
    "self.worldviewDetail": worldviewDetail,
    "self.tags": () => data.tags,
    "self.tagEvidence": ({ name }) => {
      const row = data.tagRows.find(item => item[0] === name);
      return { total: row ? 1 : 0, items: row ? [{
        chat: "林沐", timeMs: data.at("2026-10-18"), relation: row[2], text: row[3],
        context: "林沐：最近有什么新鲜事？", quote: row[3], reason: row[4],
        followup: ["林沐：下次见面时再讲给我听。"],
      }] : [] };
    },
  };
  const host = {
    "model.status": () => ({ configured: true }),
    "self-profile.read": () => data.profile,
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
      if (scope?.account !== account || !Object.hasOwn(methods, method)) throw new Error("这项操作可在客户端使用。");
      return methods[method](params);
    },
  });

  const byId = id => document.getElementById(id);
  byId("background").readOnly = true;
  byId("style").readOnly = true;
  const readOnlyButtons = ".tl-month, .tl-mark, .kpc-p-value, button.kpc-p-seg, #analysisRun, #analysisMenuToggle";
  function disableEditing() {
    document.querySelectorAll(readOnlyButtons).forEach(button => {
      if (!button.disabled) button.disabled = true;
      if (button.title !== "编辑和分析请在客户端操作") button.title = "编辑和分析请在客户端操作";
    });
  }
  new MutationObserver(disableEditing).observe(document.querySelector(".page"), {
    childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"],
  });
  byId("timelineList").addEventListener("contextmenu", event => {
    if (!event.target.closest(".tl-event-head")) return;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  for (const [id, prompt] of prompts) {
    byId(id).addEventListener("click", () => {
      byId("trialInput").value = prompt;
      byId("trialInput").focus();
    });
  }
  window.addEventListener("DOMContentLoaded", () => {
    byId("tab-trial").click();
    byId("query").value = "周末邀请";
    byId("trialInput").value = prompts[0][1];
    byId("trialSend").click();
  });
})();
