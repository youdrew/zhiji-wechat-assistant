"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// In-memory fixture adapter for the desktop renderer. All data below is fictional.
(function () {
  const A = window.FriendApp;
  const analysis = window.FriendDemoAnalysis;
  const time = (month, day, hour, minute = 0) => new Date(2026, month - 1, day, hour, minute).getTime();
  function conversation(id, month, day, lines) {
    return lines.map(([speaker, text], index) => ({ id: `${id}-${index + 1}`, speaker,
      timeMs: time(month, day, 19, index * 3), text }));
  }
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
    conversation("city", 5, 16, [["我", "你搬来这边多久了？"], ["林沐", "去年秋天来的，现在做品牌设计。最近慢慢找到喜欢的街区了。"], ["我", "那可以交换一下附近好逛的小店。"]]),
    conversation("books", 6, 8, [["林沐", "周末逛书店，看到一本讲城市树木的小书。"], ["我", "你喜欢读植物和城市这类的书？"], ["林沐", "对，边散步边观察很有意思。纸书能慢慢翻，也会做小笔记。"]]),
    conversation("hiking", 6, 21, [["我", "下次要不要去走个山路？"], ["林沐", "可以，从轻松路线开始吧。我喜欢有树荫、两三个小时能走完的。"], ["我", "那先选一条交通方便的。"]]),
    conversation("pace", 7, 11, [["林沐", "我工作时会把手机静音，晚上再集中看消息。"], ["我", "好，有急事我直接说明。"], ["林沐", "嗯，平时不用因为我回得慢担心，也不必连续催。"]]),
    conversation("spicy", 7, 26, [["我", "那家川菜怎么样？"], ["林沐", "我吃不了太辣，微辣就好。"], ["我", "记住了，下单前先问问店里。"]]),
    conversation("planning", 8, 9, [["我", "出去玩你习惯提前安排，还是到时候再看？"], ["林沐", "交通和住处提前定好，其他留一点空白。我喜欢期待感，也怕把行程填太满。"], ["我", "听起来是先把关键的事落定。"]]),
    conversation("film", 8, 22, [["林沐", "那部生活纪录片比我想的好看。"], ["我", "最喜欢哪一段？"], ["林沐", "拍小店每天开门那段，没什么大事，但很有人情味。"]]),
    conversation("birthday", 9, 4, [["我", "生日想怎么过？"], ["林沐", "九月底就和几个朋友吃顿饭吧。礼物不用贵，能一起待会儿就好。"], ["我", "那找一家安静一点的店。"]]),
    conversation("picnic", 9, 20, [["我", "今天的公园野餐挺轻松。"], ["林沐", "是啊，不赶时间，带点简单的东西就很好。下次我带水果。"], ["我", "好，下次天气舒服时再约。"]]),
    conversation("money", 9, 29, [["我", "你会为了打折买很多东西吗？"], ["林沐", "以前会，现在更愿意少买一点，买能用久的。出去体验的预算倒是愿意留。"], ["我", "留给真正想做的事。"]]),
    conversation("family", 10, 3, [["林沐", "假期准备回去陪家里人吃饭。"], ["我", "很久没回去了吗？"], ["林沐", "有阵子了。我喜欢简单相处，不需要每次都安排很正式的活动。"]]),
    conversation("camera", 10, 10, [["我", "你上次拍的窗边照片挺好看。"], ["林沐", "谢谢，我最近在练习胶片摄影。还在摸索，慢慢试吧。"], ["我", "下次散步可以一起找有趣的光线。"]]),
    conversation("quiet", 10, 16, [["我", "周末去热闹的商业街还是河边？"], ["林沐", "大多数时候我更喜欢安静的地方，能好好说话。"], ["我", "那选河边这条路。"]]),
    conversation("market", 10, 30, [["林沐", "周日想去看朋友在市集上的摊位。"], ["我", "这次不怕人多啦？"], ["林沐", "这次是想支持朋友，待一会儿就走。平时约聊天还是找安静的地方。"]]),
    conversation("museum", 11, 4, [["我", "下个月那个摄影展，我先把链接存着。"], ["林沐", "好啊，等我这周缓一缓，下周再一起挑时间。"], ["我", "那先不着急定，周末好好休息。"]]),
  ];

  function messageContext(id) {
    const story = stories.find(items => items.some(item => item.id === id));
    if (story) return { status: "ok", messages: story.map(item => ({ ...item, target: item.id === id })) };
    return analysis.messageContext(id) ?? { status: "missing", messages: [] };
  }

  const memoryExamples = [
    { id: "coffee", text: "喜欢燕麦拿铁，不加糖。", tags: ["偏好与忌口"], source: "coffee-4" },
    { id: "weekend", text: "最近希望周末安排松一点，散步累了可以坐坐。", tags: ["边界"], source: "coffee-2", status: "pending" },
    { id: "design", text: "十月底刚交完一轮设计稿，想在周末休息。", tags: ["经历"], source: "design-1" },
    { id: "garden", text: "一起看过植物展，林沐很喜欢温室。", tags: ["共同事件"], source: "garden-2", eventTime: "2026-10-18" },
    { id: "occupation", text: "做品牌设计，去年秋天搬来这座城市。", tags: ["基本信息"], source: "city-2", eventTime: "2025-09" },
    { id: "birthday", text: "九月底生日，喜欢和几位朋友简单吃饭。", tags: ["基本信息"], source: "birthday-2", eventTime: "2026-09" },
    { id: "books", text: "喜欢关于植物和城市的纸书，会边翻边做小笔记。", tags: ["爱好"], source: "books-3" },
    { id: "hiking", text: "喜欢有树荫、两三个小时能走完的轻松徒步路线。", tags: ["爱好"], source: "hiking-2" },
    { id: "camera", text: "最近在练习胶片摄影，愿意慢慢尝试。", tags: ["爱好"], source: "camera-2" },
    { id: "spicy", text: "不太能吃辣，点菜时选择微辣。", tags: ["偏好与忌口"], source: "spicy-2" },
    { id: "quiet-place", text: "约见面聊天时，通常更喜欢能好好说话的安静场所。", tags: ["偏好与忌口"], source: "quiet-2", sources: ["quiet-2", "market-3"] },
    { id: "reply-pace", text: "工作时会把手机静音，晚上集中看消息，平时不需要连续催问。", tags: ["边界"], source: "pace-3", sources: ["pace-1", "pace-3"] },
    { id: "planning", text: "喜欢提前安排交通和住处，给其他活动留出余地。", tags: ["性格"], source: "planning-2" },
    { id: "documentary", text: "喜欢观察日常生活和人情味的纪录片。", tags: ["爱好"], source: "film-3" },
    { id: "picnic", text: "一起在公园野餐，下次林沐想带水果。", tags: ["共同事件"], source: "picnic-2", eventTime: "2026-09-20" },
    { id: "family", text: "十月假期回家陪家人吃饭，喜欢简单相处。", tags: ["经历"], source: "family-3", eventTime: "2026-10" },
    { id: "museum", text: "准备下周再一起商量摄影展的时间，希望这周先缓一缓。", tags: ["共同事件"], source: "museum-2", eventTime: "2026-11-04", status: "pending" },
    { id: "coffee-extra", text: "约活动时可以先散步，走累了再去咖啡店，不需要排满一天。", tags: ["偏好与忌口"], source: "coffee-2", status: "pending", relation: "supplement", relatedId: "coffee" },
    { id: "market", text: "偶尔会去热闹的市集支持朋友，但不代表平时偏好改变。", tags: ["边界"], source: "market-3", status: "pending", relation: "conflict", relatedId: "quiet-place" },
  ].map(item => ({
    origin: "analysis", version: 1, status: "confirmed", evidenceStates: [],
    firstMentionMs: messageContext(item.source).messages.find(message => message.target).timeMs,
    ...item,
    sources: item.sources || [item.source], evidenceCount: (item.sources || [item.source]).length,
  }));
  let memories = structuredClone(memoryExamples);
  let memoryVersion = 1;
  let nextMemory = 1;

  // The production trend renderer consumes synthetic per-message facts, just as it does locally.
  function makeTrend() {
    const facts = [];
    const sessions = [3, 5, 7, 6, 9, 8, 7, 9, 8, 10, 12, 11, 14, 13, 15, 18, 17, 19, 4];
    sessions.forEach((count, index) => {
      for (let session = 0; session < count; session++) {
        const day = index === sessions.length - 1 ? session + 1 : 1 + Math.floor(session * 27 / count);
        let at = new Date(2025, index + 4, day, 10 + (session % 3)).getTime();
        const turns = 6 + ((session + index) % 7);
        for (let turn = 0; turn < turns; turn++) {
          const side = (turn + session) % 2;
          const seconds = turn === 2 && session % 7 === 0 ? 4200 : 35 + 31 * ((turn * 3 + session) % 19);
          if (turn) at += seconds * 1000;
          const marker = (turn + session * 2 + index) % 29;
          const kind = marker < 5 ? marker + 1 : 0;
          facts.push({ at, side, kind, chars: kind ? 0 : 7 + ((turn * 3 + session) % 63), flags: turn ? 1 : 3, reply: turn ? seconds : null });
          if (!kind && (turn + session) % 4 === 0) {
            at += 11000;
            facts.push({ at, side, kind: 0, chars: 9, flags: 0, reply: null });
          }
        }
      }
    });
    facts.sort((left, right) => left.at - right.at);
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
      dataRevision: "fictional-friend-demo", cutoffMs, kinds: ["text", "image", "voice", "video", "sticker", "other"], messages, replies, insufficient: [],
      history: { firstMessageMs: facts[0].at, lastMessageMs: facts.at(-1).at, complete: true, readCount: facts.length },
      timezone: "本机时区", utcOffsetMinutes: -new Date(cutoffMs).getTimezoneOffset(),
      coverage: { unknownDirection: 0, untimed: 0, future: 0, ambiguousSameTime: 0 },
    };
  }

  const trend = makeTrend();
  const trendMath = window.FriendTrendMath;
  const facts = trendMath.decode(trend);
  const lastMonth = trendMath.summary(facts, time(10, 1, 0), time(11, 1, 0));
  const priorMonth = trendMath.summary(facts, time(9, 1, 0), time(10, 1, 0));
  const overview = {
    facts: { historyState: "ready", firstMs: trend.history.firstMessageMs, lastMs: trend.history.lastMessageMs,
      messages: trend.history.readCount, goal: "保持联系", goalIsDefault: true },
    summary: { summary: "林沐刚忙完一轮设计稿，想让周末轻松一点。你们聊到沿河散步和新开的咖啡店。上次一起逛植物展时，林沐最喜欢温室；最近也在练习胶片摄影。摄影展先存着链接，准备下周再一起商量时间。",
      fromMs: time(10, 18, 17), toMs: time(11, 4, 19, 6), messages: stories.flat().filter(item => item.timeMs >= time(10, 18, 17)).length,
      complete: true, createdAtMs: time(11, 5, 10),
      evidence: ["garden-2", "design-1", "coffee-2", "museum-2"].map(id => {
        const item = messageContext(id).messages.find(message => message.target);
        return { ...item, msgId: item.id };
      }) },
    next: { next: "提议先散一小段步，走累了就去喝咖啡。可以顺手记得林沐喜欢燕麦拿铁不加糖。" },
    about: [
      { kind: "memory", label: "近况", text: "刚交完设计稿，周末想休息。" },
      { kind: "memory", label: "偏好", text: "燕麦拿铁，不加糖。" },
      { kind: "memory", label: "爱好", text: "植物、轻松徒步和胶片摄影。" },
      { kind: "memory", label: "聊天节奏", text: "工作时手机静音，晚上集中看消息。" },
      { kind: "persona", label: "相处节奏", text: "喜欢提前商量，也给计划留一点余地。", confidence: "中等" },
      { kind: "persona", label: "探索兴趣", text: "对新展览和日常生活里的小细节好奇。", confidence: "中等" },
    ],
    remember: [
      { text: "一起看过植物展，林沐很喜欢温室。" },
      { text: "公园野餐时约好下次林沐带水果。" },
      { text: "点菜选微辣，约聊天找安静一点的地方。" },
    ],
    change: { label: "10 月", days: lastMonth.contactDays, previousDays: priorMonth.contactDays },
  };
  const validTags = new Set(["基本信息", "经历", "性格", "爱好", "偏好与忌口", "边界", "共同事件"]);
  const drafts = new Map();
  let observationCounter = 1;
  let ownProfile = "和林沐是朋友，希望保持轻松、互相尊重的联系。";

  function editableMemory(body) {
    const item = memories.find(memory => memory.id === body.id);
    if (!item) throw new Error("这条演示记忆已不存在。");
    if (body.expectedVersion !== undefined && body.expectedVersion !== item.version)
      throw new Error("记忆已更新，请重新打开后修改。");
    return item;
  }
  function memoryText(body) {
    const text = String(body.text || "").trim();
    if (!text || text.length > 300) throw new Error("请填写 1 至 300 字的记忆内容。");
    return { text, tags: Array.isArray(body.tags) ? body.tags.filter(tag => validTags.has(tag)) : [],
      eventTime: String(body.eventTime || "").trim() };
  }
  function confirmMemory(item, choice) {
    const related = memories.find(memory => memory.id === item.relatedId);
    if (item.relation === "conflict" && !choice) return;
    if (item.relation === "conflict" && related && choice === "keep") {
      item.status = "rejected";
    } else if (related && (item.relation === "supplement" || choice === "adopt")) {
      related.text = choice === "adopt" ? item.text : `${related.text} ${item.text}`;
      related.sources = [...new Set([...related.sources, ...item.sources])];
      related.evidenceCount = related.sources.length;
      related.version++;
      item.status = "rejected";
    } else {
      item.status = "confirmed";
      delete item.relatedId;
      delete item.relation;
    }
    item.version++;
  }
  function mutateMemory(body) {
    switch (body.action) {
      case "create":
        memories.push({ ...memoryText(body), id: `manual-${nextMemory++}`, origin: "user", status: "confirmed",
          version: 1, sources: [], evidenceCount: 0, evidenceStates: [], firstMentionMs: trend.cutoffMs });
        break;
      case "edit": {
        const item = editableMemory(body);
        Object.assign(item, memoryText(body), { userCorrected: true, version: item.version + 1 });
        break;
      }
      case "confirm": confirmMemory(editableMemory(body), body.choice); break;
      case "ignore": {
        const item = editableMemory(body);
        item.status = "rejected";
        item.version++;
        break;
      }
      case "delete": {
        const item = editableMemory(body);
        memories = memories.filter(memory => memory !== item);
        break;
      }
      case "batch-confirm":
      case "batch-ignore":
        for (const item of memories.filter(memory => memory.status === "pending" && (!body.ids || body.ids.includes(memory.id)))) {
          if (body.action === "batch-confirm") confirmMemory(item);
          else { item.status = "rejected"; item.version++; }
        }
        break;
      case "clear": memories = []; break;
      default: throw new Error("此操作在演示中不可用。");
    }
    memoryVersion++;
    overview.remember = memories.filter(item => item.status === "confirmed").slice(0, 4).map(item => ({ text: item.text }));
    return { saved: true, memoryVersion };
  }
  function saveObservation(params) {
    const observations = analysis.persona.observations;
    const index = observations.findIndex(item => item.id === params.id);
    if (params.action === "delete") {
      if (index >= 0) observations.splice(index, 1);
      return { saved: true };
    }
    const judgement = String(params.judgement || "").trim();
    if (!judgement) throw new Error("请先写下你的观察。");
    const observation = { id: params.id || `demo-observation-${observationCounter++}`, judgement,
      note: String(params.note || "").trim(), dimension: params.dimension, version: (observations[index]?.version || 0) + 1 };
    if (index >= 0) observations.splice(index, 1, observation); else observations.push(observation);
    return { saved: true };
  }

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
    context: () => ({ name: "林沐", restricted: false, theme: "light" }),
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
        case "observations.save": return saveObservation(params);
        case "drafts.list": return { drafts: [...drafts.values()], closedGenerations: {} };
        case "drafts.save": drafts.set(params.entry, structuredClone(params)); return { saved: true };
        case "drafts.close": drafts.delete(params.entry); return { closed: true };
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

  // The renderer talks only to this closed in-memory fixture adapter.
  // Its evidence view references this label lookup in the shipped renderer.
  window.roleWords = Object.freeze({ direct: "发言", quote: "引用", forward: "转发" });
  A.query = values => new URLSearchParams(values).toString();
  A.emit = () => {};
  A.api = async (request, options) => {
    const [path, query] = request.split("?");
    const params = new URLSearchParams(query);
    if (options?.method === "POST" && path === "/api/friend/memory") return mutateMemory(options.body || {});
    if (options && options.method !== "GET") throw new Error("此操作在演示中不可用。");
    if (path === "/api/friend/memories") {
      const items = memories.filter(item => item.status === params.get("status"));
      if (params.get("view") === "timeline") {
        const at = item => item.eventTime ? Date.parse(item.eventTime) || item.firstMentionMs : item.firstMentionMs;
        items.sort((left, right) => at(right) - at(left));
      }
      const offset = Number(params.get("offset")), limit = Number(params.get("limit"));
      return { items: items.slice(offset, offset + limit), total: items.length, memoryVersion };
    }
    const memory = memories.find(item => item.id === params.get("id"));
    if (memory && (path === "/api/friend/memory" || path === "/api/friend/evidence")) {
      if (path === "/api/friend/evidence") return messageContext(memory.sources[Number(params.get("ordinal"))] || memory.source);
      return { memory, evidence: memory.sources.map((id, ordinal) => {
        const message = messageContext(id).messages.find(item => item.target);
        return { ordinal, role: "direct", status: "ok", timeMs: message.timeMs, excerpt: message.text };
      }) };
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

  function analysisInfo() {
    const body = A.openSheet("分析详情", "demo-analysis");
    body.append(A.el("p", "fa-text", "演示结果依据虚构的聊天与关系资料预先生成。"));
    const details = A.el("dl", "fa-kv");
    for (const [label, value] of [["可读历史", `${trend.history.readCount} 条消息`], ["最近更新", "2026-11-05"],
      ["分析范围", "好友概述、关系记忆、性格与三观、聊天互动、关系趋势"], ["操作方式", "可以查看依据、核对上下文，也可以在本页修改演示记忆与观察。刷新页面还原。"]])
      details.append(A.el("dt", "", label), A.el("dd", "", value));
    body.append(details);
  }
  function contactSettings() {
    const body = A.openSheet("联系目标与自己的资料", "demo-settings");
    const form = A.el("div", "fa-form");
    const goal = A.el("input");
    goal.type = "text";
    goal.value = overview.facts.goal;
    goal.maxLength = 40;
    goal.setAttribute("aria-label", "联系目标");
    const profile = A.el("textarea");
    profile.value = ownProfile;
    profile.maxLength = 300;
    profile.setAttribute("aria-label", "自己的资料");
    const save = A.button("保存", "fa-primary", () => {
      overview.facts.goal = goal.value.trim() || "保持联系";
      overview.facts.goalIsDefault = false;
      ownProfile = profile.value.trim();
      A.closeSheet();
      restoreEvidenceFocus();
      void modules[selected].load();
      A.toast("已更新演示资料");
    });
    form.append(A.el("label", "fa-row-label", "联系目标"), goal,
      A.el("label", "fa-row-label", "自己的资料"), profile, save);
    body.append(form);
  }
  const more = A.byId("demoMore");
  const menu = A.byId("demoMenu");
  const menuButtons = [...menu.querySelectorAll("button")];
  function closeMenu(focus = false) {
    menu.hidden = true;
    more.setAttribute("aria-expanded", "false");
    if (focus) more.focus();
  }
  more.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
    more.setAttribute("aria-expanded", String(!menu.hidden));
    if (!menu.hidden) menuButtons[0].focus();
  });
  document.addEventListener("click", event => {
    if (event.target !== more && !menu.contains(event.target)) closeMenu();
  });
  menuButtons.forEach((item, index) => {
    item.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); closeMenu(true); return; }
      const direction = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      if (!direction) return;
      event.preventDefault();
      menuButtons[(index + direction + menuButtons.length) % menuButtons.length].focus();
    });
    item.addEventListener("click", async () => {
      closeMenu(true);
      switch (item.dataset.demoAction) {
        case "settings": contactSettings(); break;
        case "add": show("memories"); window.FriendMemory.startAdd(); break;
        case "ignored": show("memories"); await window.FriendMemory.showIgnored(); break;
        case "clear": show("memories"); await window.FriendMemory.clear(); break;
        case "rebuild":
          if (await A.confirm("载入全部预生成的演示分析？", "载入")) {
            await modules[selected].load();
            A.toast("演示分析已更新");
          }
          break;
      }
    });
  });
  A.byId("demoAnalysisInfo").textContent = `已分析 ${trend.history.readCount} 条聊天`;
  A.byId("demoAnalysisInfo").addEventListener("click", analysisInfo);
  A.byId("demoRefresh").addEventListener("click", async () => {
    await modules[selected].load();
    A.toast("已载入预生成的演示分析");
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
