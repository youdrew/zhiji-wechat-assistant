"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Fictional conversations and precomputed analysis for the public demo.
(function () {
  const time = date => Date.parse(date + "+09:00");
  const message = (id, speaker, date, text) => ({ id: `analysis-${id}`, speaker, timeMs: time(date), text });
  const walk = [
    message("walk-question", "我", "2026-10-18T15:10:00", "周六要不要去人多的市集转转？"),
    message("walk-answer", "林沐", "2026-10-18T15:24:00", "这周有点累，想找个安静的地方走走。河边那条路就不错。"),
    message("walk-followup", "我", "2026-10-18T15:30:00", "那就去河边，走累了再找个地方坐坐。"),
  ];
  const plants = [
    message("plants-question", "我", "2026-10-25T10:00:00", "植物园周末有个小展览，要不要一起去？"),
    message("plants-answer", "林沐", "2026-10-25T10:12:00", "好啊！我之前没看过苔藓展，想看看它们在玻璃里怎么长。"),
    message("plants-time", "林沐", "2026-10-25T10:13:00", "我先看看开馆时间，选一个不用排队的时段吧。"),
  ];
  const coffee = [
    message("coffee-question", "我", "2026-11-01T16:00:00", "散步之后去喝咖啡？我可以先把地方定好。"),
    message("coffee-answer", "林沐", "2026-11-01T16:08:00", "好呀。你要是临时累了，改天也完全可以，别为了赴约把自己排太满。"),
    message("coffee-followup", "我", "2026-11-01T16:11:00", "这样挺好，周六中午再互相确认一下。"),
  ];
  const threads = [walk, plants, coffee];
  const evidence = [
    ["quiet", walk[1], "忙碌后偏好安静的活动，依据来自这一轮对话。"],
    ["curiosity", plants[1], "对没看过的植物展表现出好奇。"],
    ["plan", plants[2], "主动查开馆时间，安排人少的时段。"],
    ["pace", coffee[1], "明确接受临时调整，主动照顾对方节奏。"],
  ];
  const fromMs = walk[0].timeMs;
  const toMs = coffee[2].timeMs;
  const dimension = (id, position, label, detail, support, counter = []) => ({
    id, position, label, detail, support, counter, confidence: "有一定依据",
  });
  const persona = {
    createdAtMs: time("2026-11-02T12:00:00"),
    stale: false,
    observations: [{ id: "pace-observation", dimension: "big5_a", judgement: "约活动时会主动留出调整空间" }],
    labels: { core: "价值底色" },
    catalog: [{ id: "big5_a" }],
    sources: {},
    families: {},
    result: {
      dimensions: [
        dimension("big5_e", -0.28, "偏内向", "工作忙时更愿意在安静的小场景里相处。", ["quiet"]),
        dimension("big5_o", 0.38, "乐于探索", "会对新的活动内容提出具体问题。", ["curiosity"]),
        dimension("big5_c", 0.32, "愿意计划", "约好活动后会主动查开放时间，同时给安排留余地。", ["plan"], ["pace"]),
        dimension("big5_a", 0.42, "照顾彼此感受", "在表达期待时也会尊重对方当天的状态。", ["pace"]),
        dimension("val_change", -0.28, "愿意尝试", "对没有看过的展览有兴趣。", ["curiosity"]),
        dimension("val_self", 0.36, "考虑彼此", "共同安排时会照顾双方的精力。", ["pace"]),
      ],
      selfReports: {},
      values: [{
        id: "core",
        selfReport: {
          statements: [{ text: coffee[1].text, timeMs: coffee[1].timeMs, evidence: ["pace"] }],
        },
        inference: dimension("core", null, "重视相处的舒适度", "愿意把具体安排留到双方都有余裕时。", ["pace"]),
      }],
      coverage: { fromMs, toMs, processed: 4, excluded: 0, evidence: 4, cited: 4 },
      historyComplete: true,
      model: "演示分析",
    },
  };

  function distribution(entries) {
    const total = entries.reduce((sum, [, count]) => sum + count, 0);
    return { total, items: entries.map(([label, count]) => ({ label, count, share: count / total })) };
  }

  const chat = {
    configured: true,
    historyReady: true,
    coverage: { fromMs, toMs, blocks: 8 },
    reply: { rate: null, eligible: 4, replied: 3, medianSeconds: 12 * 60, observing: 1, excluded: 0 },
    statistics: {
      responses: [{
        ask: "邀请",
        rounds: 3,
        determinable: 3,
        undetermined: 0,
        fromMs,
        toMs,
        items: [
          { label: "明确接受", count: 2, share: null, examples: ["plants", "coffee"] },
          { label: "提出替代安排", count: 1, share: null, examples: ["walk"] },
        ],
      }],
      invites: { total: 3, withoutReply: 0, counts: { 明确接受: 2, 明确婉拒: 0, 未明确答复: 1, 无法判断: 0 } },
      signals: {
        self: { labelled: 5, intent: distribution([["邀请", 3], ["回应", 2]]), emotion: distribution([["平静", 3], ["期待", 2]]) },
        other: { labelled: 3, intent: distribution([["回应", 2], ["分享", 1]]), emotion: distribution([["平静", 2], ["愉快", 1]]) },
      },
      keywords: { self: [{ word: "周六", count: 2 }], other: [] },
    },
  };

  function personaEvidence(ids) {
    const items = evidence.filter(([id]) => ids.includes(id)).map(([id, source, note]) => ({
      id, note, excerpt: source.text, timeMs: source.timeMs, messageId: source.id, status: "ok",
    }));
    return { items };
  }

  function round(id) {
    const thread = { walk, plants, coffee }[id];
    if (!Array.isArray(thread)) return { blocks: [] };
    const messages = thread.filter((item, index) => index === 0 || item.speaker === "林沐");
    return { blocks: [{ messages }] };
  }

  function messageContext(id) {
    const thread = threads.find(items => items.some(item => item.id === id));
    if (!thread) return null;
    return { status: "ok", messages: thread.map(item => ({ ...item, target: item.id === id })) };
  }

  window.FriendDemoAnalysis = { persona, chat, personaEvidence, round, messageContext };
})();
