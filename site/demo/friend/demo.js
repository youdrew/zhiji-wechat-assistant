"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// In-memory fixture adapter for the desktop renderer. All data below is fictional.
(function () {
  const A = window.FriendApp;
  const analysis = window.FriendDemoAnalysis;
  const time = (month, day, hour, minute = 0) => new Date(2026, month - 1, day, hour, minute).getTime();
  const stories = [
    [
      { id: "garden-1", speaker: "我", timeMs: time(10, 18, 17, 12), text: "今天植物展逛得挺开心，下次还一起去？" },
      { id: "garden-2", speaker: "林沐", timeMs: time(10, 18, 17, 16), text: "好呀。温室那一段我特别喜欢，下次早点去，人少一点。" },
      { id: "garden-3", speaker: "我", timeMs: time(10, 18, 17, 18), text: "那下次先逛温室，出来再喝咖啡。" },
    ],
    [
      { id: "design-1", speaker: "林沐", timeMs: time(10, 28, 20, 34), text: "这周一直在改设计稿，今天终于交完了。" },
      { id: "design-2", speaker: "我", timeMs: time(10, 28, 20, 37), text: "辛苦了，周末可以好好歇一下。" },
    ],
    [
      { id: "coffee-1", speaker: "我", timeMs: time(11, 3, 19, 2), text: "周末要不要去河边散步？沿路新开了一家咖啡店。" },
      { id: "coffee-2", speaker: "林沐", timeMs: time(11, 3, 19, 6), text: "可以啊，这周想慢一点。走累了就坐坐，不用排太满。" },
      { id: "coffee-3", speaker: "我", timeMs: time(11, 3, 19, 9), text: "好，喝的还是燕麦拿铁？" },
      { id: "coffee-4", speaker: "林沐", timeMs: time(11, 3, 19, 11), text: "嗯，燕麦拿铁不加糖就好。" },
    ],
  ];

  function messageContext(id) {
    const story = stories.find(items => items.some(item => item.id === id));
    if (story) return { status: "ok", messages: story.map(item => ({ ...item, target: item.id === id })) };
    return analysis.messageContext(id) ?? { status: "missing", messages: [] };
  }

  const memories = [
    { id: "coffee", text: "喜欢燕麦拿铁，不加糖。", tags: ["偏好与忌口"], source: "coffee-4" },
    { id: "weekend", text: "最近希望周末安排松一点，散步累了可以坐坐。", tags: ["边界"], source: "coffee-2", status: "pending" },
    { id: "design", text: "十月底刚交完一轮设计稿，想在周末休息。", tags: ["经历"], source: "design-1" },
    { id: "garden", text: "一起看过植物展，林沐很喜欢温室。", tags: ["共同事件"], source: "garden-2", eventTime: "2026-10-18" },
  ].map(item => ({
    origin: "analysis", version: 1, status: "confirmed", evidenceCount: 1, evidenceStates: [],
    firstMentionMs: messageContext(item.source).messages.find(message => message.target).timeMs,
    ...item,
  }));

  // The production trend renderer consumes synthetic per-message facts, just as it does locally.
  function makeTrend() {
    const facts = [];
    const sessions = [4, 7, 5, 9, 8, 13, 3];
    sessions.forEach((count, index) => {
      const month = index + 5;
      for (let session = 0; session < count; session++) {
        const day = month === 11 ? session + 1 : 1 + Math.floor(session * 26 / count);
        let at = time(month, day, 17 + (session % 4));
        const turns = 4 + ((session + index) % 5);
        for (let turn = 0; turn < turns; turn++) {
          const side = (turn + session) % 2;
          const seconds = 40 + 18 * ((turn * 3 + session) % 18);
          if (turn) at += seconds * 1000;
          const kind = (turn + session * 2) % 17 === 0 ? 1 : 0;
          facts.push({ at, side, kind, chars: kind ? 0 : 7 + ((turn * 3 + session) % 35), flags: turn ? 1 : 3, reply: turn ? seconds : null });
          if (!kind && (turn + session) % 4 === 0) {
            at += 11000;
            facts.push({ at, side, kind: 0, chars: 9, flags: 0, reply: null });
          }
        }
      }
    });
    const messages = { time: [], side: [], chars: [], kind: [], flags: [] };
    const replies = [];
    let prior = 0;
    facts.forEach((fact, index) => {
      messages.time.push(fact.at - prior);
      for (const key of ["side", "chars", "kind", "flags"]) messages[key].push(fact[key]);
      if (fact.reply !== null) replies.push([index, fact.reply]);
      prior = fact.at;
    });
    const cutoffMs = time(11, 5, 18);
    return {
      dataRevision: "fictional-friend-demo", cutoffMs, kinds: ["text", "image"], messages, replies, insufficient: [],
      history: { firstMessageMs: facts[0].at, lastMessageMs: facts.at(-1).at, complete: true, readCount: facts.length },
      timezone: "本机时区", utcOffsetMinutes: -new Date(cutoffMs).getTimezoneOffset(),
      coverage: { unknownDirection: 0, untimed: 0, future: 0, ambiguousSameTime: 0 },
    };
  }

  const trend = makeTrend();
  const overview = {
    facts: { historyState: "ready", firstMs: trend.history.firstMessageMs, lastMs: time(11, 3, 19, 11),
      messages: trend.history.readCount, goal: "保持联系", goalIsDefault: true },
    summary: { summary: "林沐刚忙完一轮设计稿，想让周末轻松一点。你们聊到沿河散步和新开的咖啡店。上次一起逛植物展时，林沐最喜欢温室。",
      fromMs: time(10, 18, 17), toMs: time(11, 3, 19, 11), messages: stories.flat().length, complete: true, createdAtMs: time(11, 4, 10),
      evidence: [stories[0][1], stories[1][0], stories[2][1]].map(item => ({ ...item, msgId: item.id })) },
    next: { next: "提议先散一小段步，走累了就去喝咖啡。可以顺手记得林沐喜欢燕麦拿铁不加糖。" },
    about: [
      { kind: "memory", label: "近况", text: "刚交完设计稿，周末想休息。" },
      { kind: "memory", label: "偏好", text: "燕麦拿铁，不加糖。" },
      { kind: "persona", label: "相处节奏", text: "喜欢提前商量，也给计划留一点余地。", confidence: "中等" },
    ],
    remember: [{ text: "一起看过植物展，林沐很喜欢温室。" }],
    change: { label: "近一个月", days: 15, previousDays: 9 },
  };

  const tabs = [...document.querySelectorAll("[data-tab]")];
  const modules = {
    overview: window.FriendOverview, memories: window.FriendMemory, persona: window.FriendPersona,
    values: window.FriendValues, chat: window.FriendChat, trend: window.FriendTrend,
  };
  const main = document.querySelector("main");
  let selected;
  let evidenceTrigger;

  function show(key, focus = false) {
    const chosen = tabs.find(tab => tab.dataset.tab === key);
    if (!chosen) return;
    if (selected !== key) {
      A.closeSheet();
      evidenceTrigger = null;
      for (const tab of tabs) {
        const active = tab === chosen;
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = active ? 0 : -1;
        A.byId(tab.getAttribute("aria-controls")).hidden = !active;
      }
      selected = key;
      main.scrollTop = 0;
      void modules[key].load();
    }
    if (focus) chosen.focus();
  }

  const settings = { unit: "month", metric: "overview", log: false, zoom: {} };
  window.kyfHost = Object.freeze({
    context: () => ({ name: "林沐", restricted: true, theme: "light" }),
    async rpc(method, params) {
      switch (method) {
        case "page.overview": return overview;
        case "page.persona": return analysis.persona;
        case "page.personaEvidence": return analysis.personaEvidence(params.ids);
        case "page.chat": return analysis.chat;
        case "page.round": return analysis.round(params.id);
        case "page.trend": return trend;
        default: throw new Error("此操作在演示中不可用。");
      }
    },
    async host(method, params) {
      switch (method) {
        case "tab.select": show(params.tab, true); return { ok: true };
        case "message.context": return messageContext(params.id);
        case "trend.settings.get": return { ...settings, zoom: { ...settings.zoom } };
        case "trend.settings": {
          for (const key of ["unit", "metric", "log"]) if (Object.hasOwn(params, key)) settings[key] = params[key];
          if (params.zoom) settings.zoom[params.zoom.unit] = params.zoom.slot;
          return { ok: true };
        }
        case "legacy.portrait": return { items: [] };
        default: throw new Error("此操作在演示中不可用。");
      }
    },
  });

  // The memory renderer normally reads the application’s host API. Serve only the known reads.
  // Its evidence view references this label lookup in the shipped renderer.
  window.roleWords = Object.freeze({ direct: "发言", quote: "引用", forward: "转发" });
  A.query = values => new URLSearchParams(values).toString();
  A.api = async (request, options) => {
    if (options && options.method !== "GET") throw new Error("此操作在演示中不可用。");
    const [path, query] = request.split("?");
    const params = new URLSearchParams(query);
    if (path === "/api/friend/memories") {
      const items = memories.filter(item => item.status === params.get("status"));
      if (params.get("view") === "timeline") items.sort((left, right) => right.firstMentionMs - left.firstMentionMs);
      const offset = Number(params.get("offset")), limit = Number(params.get("limit"));
      return { items: items.slice(offset, offset + limit), total: items.length, memoryVersion: 1 };
    }
    const memory = memories.find(item => item.id === params.get("id"));
    if (memory && (path === "/api/friend/memory" || path === "/api/friend/evidence")) {
      const context = messageContext(memory.source);
      if (path === "/api/friend/evidence") return context;
      const message = context.messages.find(item => item.target);
      return { memory, evidence: [{ ordinal: 0, role: "direct", status: "ok", timeMs: message.timeMs, excerpt: message.text }] };
    }
    throw new Error("此操作在演示中不可用。");
  };

  // Keep keyboard focus close to the evidence interaction.
  const openSheet = A.openSheet;
  A.openSheet = (title, owner) => {
    evidenceTrigger = document.activeElement;
    const body = openSheet(title, owner);
    A.byId("faSheetClose").focus();
    return body;
  };
  function restoreEvidenceFocus() {
    if (evidenceTrigger?.isConnected) evidenceTrigger.focus();
    evidenceTrigger = null;
  }
  A.byId("faSheetClose").addEventListener("click", restoreEvidenceFocus);
  window.addEventListener("keydown", event => {
    if (event.key === "Escape" && A.byId("faSheet").hidden) restoreEvidenceFocus();
  });

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => show(tab.dataset.tab));
    tab.addEventListener("keydown", event => {
      const target = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length,
        Home: 0, End: tabs.length - 1 }[event.key];
      if (target === undefined) return;
      event.preventDefault();
      show(tabs[target].dataset.tab, true);
    });
  });
  function readHash() {
    const key = window.location.hash.slice(1);
    show(tabs.some(tab => tab.dataset.tab === key) ? key : "overview");
  }
  window.addEventListener("hashchange", readHash);
  readHash();
})();
