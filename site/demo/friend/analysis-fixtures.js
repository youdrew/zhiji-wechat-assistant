"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Fictional conversations and precomputed analysis for the public demo.
(function () {
  const time = date => Date.parse(date + "+09:00");
  const minute = 60000;
  const day = 24 * 60 * minute;
  const cutoffMs = time("2026-11-05T12:00:00");
  const scenes = [];

  function scene(id, date, intent, question, replies, options = {}) {
    const start = time(date);
    const delay = options.delay ?? 8;
    const messages = [{
      id: "analysis-" + id + "-question", speaker: "我", timeMs: start, text: question,
      intent, emotion: options.selfEmotion ?? "平静",
    }];
    for (const [index, text] of replies.entries()) messages.push({
      id: "analysis-" + id + "-reply-" + index, speaker: "林沐",
      timeMs: start + (delay + index) * minute, text,
      intent: options.otherIntent ?? "回应", emotion: options.emotion ?? "平静",
      response: options.response ?? "接住并展开", invite: options.invite ?? null,
    });
    const result = { id, messages };
    scenes.push(result);
    return result;
  }

  scene("early-coffee", "2026-10-01T16:00:00", "邀请", "周末一起散步，再喝杯咖啡？",
    ["好呀，想去安静一点的店。我还是燕麦拿铁，不加糖。",
      "上次那家熟悉的店就挺好，我有时也想让周末省点选择的力气。"],
    { invite: "明确接受", emotion: "积极" });
  scene("rest", "2026-10-02T19:00:00", "关心", "这周工作忙完了吗？周末能休息一下吗？",
    ["项目快交的时候会反复担心漏掉什么，昨晚睡前又检查了一遍。",
      "今天交完就放松多了，散步会让我恢复精神。我给自己放假，电脑也该下班啦。"], { emotion: "混合" });
  scene("career", "2026-10-03T18:00:00", "提问", "接下来一年的工作，你最想改变哪件事？",
    ["我想明年春天试着做一个完整的小展览。先每周留两小时学策展，再积累作品。",
      "机会不是只等来的，这周我会先给两个认识的策展人写信。",
      "不过找场地也要看运气，租金和档期不是我一个人能决定的。"]);
  scene("city-policy", "2026-10-04T18:00:00", "分享", "看到一篇讲城市公共空间的文章，想听听你的看法。",
    ["我政治上算中间，支持公共空间对不同的人更友好，也希望讨论时能听到不同意见。",
      "公共图书馆和基本医疗值得多投入一些钱，机会不能只由收入决定。",
      "但小店经营也需要留空间，有的事情让市场试一试更有效。",
      "老街的建筑和节庆也值得保留，改造时可以听听老居民的想法。",
      "如果财政还有空间，我会优先支持更普惠的公共服务，少一点由家庭收入决定的差距。"]);
  scene("family", "2026-10-05T19:00:00", "提问", "你怎么看陪家人和自己的生活之间的平衡？",
    ["对我来说，和家人保持联系很重要。我每周会找个不赶时间的晚上打电话。",
      "家里约好的事情我会尽量到场，答应过就不能只顾自己方便。",
      "不过工作和住哪里最后还是我自己决定，我会认真解释，但不需要替我做主。",
      "祭扫和家里的纪念物对我来说是郑重的事，不该拿来恶搞取乐。"]);
  scene("relationship", "2026-10-06T19:00:00", "提问", "你喜欢怎样的相处方式？",
    ["我觉得自己在关系里偏安全型，忙的时候不需要反复证明在意，约好再聊就可以。",
      "心情不好时我愿意直接说，也希望能放心向亲近的人求助。",
      "刚认识时我会保留一点空间，连续几天消息太密，也想有一个独处的晚上。",
      "但偶尔重要消息很久没回，我还是会担心自己是不是说错了话。"]);
  scene("exhibition-help", "2026-10-07T18:00:00", "请求帮助", "植物展的介绍太多了，你能帮我挑一条参观路线吗？",
    ["可以，我们先弄清楚展览按什么逻辑分区。温室湿度不同，是不是对应不同的植物适应方式？",
      "如果没弄懂，我宁愿先查两篇介绍再下结论，直接背结论没什么趣味。",
      "不过周末只是轻松散步，路线不用研究得像交论文，先选两个最想看的就好。"]);
  scene("umbrella-joke", "2026-10-08T18:00:00", "玩笑", "周末天气预报又变了，咱们的散步计划真考验人。",
    ["哈哈，我们不是被雨追着跑，是给咖啡店制造客流。",
      "要下雨就改室内，计划可以带伞，心情就不用淋雨啦。"],
    { emotion: "积极" });
  scene("family-invite", "2026-10-09T17:00:00", "邀请", "周末来一起看植物展吗？",
    ["这次不去了，我答应陪家人吃饭。谢谢你记得叫我，下周散步可以再问我。",
      "不用担心我是不是不想参加，只是这个周末时间排不开。"],
    { response: "明确婉拒", invite: "明确婉拒" });
  scene("money", "2026-10-10T16:00:00", "提问", "如果突然有一笔奖金，你会怎么安排？",
    ["我会先存下一部分作备用金，花钱能不能让我真的用得上，比跟风买什么更重要。",
      "不过特别喜欢的展览和旅行我也愿意多花一点，有些体验不能只看价格。",
      "我也想把收入做高一点，让自己有底气选想做的项目。"]);
  scene("new-route", "2026-10-11T17:00:00", "邀请", "周末临时去没走过的河边路线，怎么样？",
    ["好，今天不用把每一步都排满，先走走看。上次那条小路让我发现了很喜欢的咖啡店。",
      "新路线挺有意思，偶尔临时起意会有意外收获。"],
    { invite: "明确接受", emotion: "积极" });
  scene("public-rules", "2026-10-12T18:00:00", "分享", "社区在讨论周末夜市和安静街区怎么兼顾。",
    ["我能理解两边的需要，夜市带来工作和热闹，住在附近的人也需要睡觉。",
      "不必把不同意见都说成自私，先试限时开放，再听一次反馈。",
      "涉及消防和食品安全的规矩还是要统一执行，这部分不能只靠个人自觉。"]);
  scene("poster-feedback", "2026-10-13T18:00:00", "请求帮助", "这张植物展海报你能给点真实意见吗？",
    ["我会直说，标题现在太小了，先把日期和入口信息放大，装饰可以往后排。",
      "如果你想保留手写感，我们可以各留一版，小范围试试哪版更清楚。",
      "工作上做介绍时我会更正式些，跟朋友聊就不用每句话都打磨成提案。"]);
  scene("message-boundary", "2026-10-14T19:00:00", "关心", "我有时工作忙没及时回复，你会介意吗？",
    ["你说过在忙，我就不会把没回当成不想理我。有空再聊就好。",
      "但上次我提了一个很在意的建议，等了很久还是会反复想，是不是你其实不喜欢。",
      "我在聚会上也会留意别人有没有不自在，换一种说法让场面轻松点。"]);
  scene("route-disagreement", "2026-10-15T18:00:00", "请求帮助", "我想从东门进，你坚持南门，周末路线怎么定？",
    ["我们先把两条路线的步行时间和无障碍入口放一起看，再找一个双方都方便的方案。",
      "我愿意多走十分钟，如果你也能晚一点出门，这样我们各让一步。",
      "不过涉及你腿伤那一段就不要勉强，能少一点不必要的不舒服更重要。"]);
  scene("ethics", "2026-10-16T18:00:00", "分享", "社群活动有人想让新成员多承担收尾工作，我有点犹豫。",
    ["我不赞成，只因为是新人就分到更多工作不公平。应该先说明任务，再让大家按能力自愿分。",
      "让人难受还说是锻炼，我觉得不合适，先看有没有造成伤害。",
      "不愿参加的人也应该能拒绝，不能用大家都来了逼着他留下。",
      "答应帮忙后也应该守约，临时走至少要把交接讲清楚。",
      "我也不赞成为吸引眼球去破坏祭祀纪念物，别人珍视的神圣事物值得尊重。"]);
  scene("pottery", "2026-10-17T15:00:00", "邀请", "周末试试陶艺课吗？",
    ["我先看看时间，周末可能想重复走那条熟悉的河边路线，省点脑子。",
      "如果课程能自己试着做一件东西，我又有点想去。先别替我报名，晚上我给你答复。"],
    { invite: "未明确答复" });
  const walk = scene("walk", "2026-10-18T15:10:00", "邀请", "周六要不要去人多的市集转转？",
    ["这周有点累，想找个安静的地方走走。河边那条路就不错。"],
    { invite: "未明确答复", delay: 14 });
  walk.messages[0].id = "analysis-walk-question";
  walk.messages[1].id = "analysis-walk-answer";
  walk.messages.push(
    { id: "analysis-walk-followup", speaker: "我", timeMs: time("2026-10-18T15:30:00"),
      text: "那就去河边，走累了再找个地方坐坐。", intent: "回应", emotion: "积极" },
    { id: "analysis-walk-confirm", speaker: "林沐", timeMs: time("2026-10-18T15:32:00"),
      text: "好呀，今天的任务就是把步数交给河边，把休息交给咖啡店。", intent: "回应", emotion: "积极",
      response: "接住并展开", invite: null }
  );
  scene("plant-notes", "2026-10-19T18:00:00", "分享", "上次植物园的照片洗出来了，还记得温室那棵大树吗？",
    ["记得呀。每次回想那天，都觉得一起慢慢看东西的时间很珍贵。",
      "小时候常去的小公园也教会我很多植物名字，那些经历现在想起还是很开心。",
      "只是以前为了赶进度没认真学画画，这件事我还是有点后悔。"],
    { emotion: "积极" });
  scene("deadline-conflict", "2026-10-20T18:00:00", "请求帮助", "小组有人又没交资料，大家对截止时间有分歧，你会怎么处理？",
    ["这次我会坚持今晚截止，已经改过两次，再拖会让按时交的人吃亏。",
      "但可以把确实缺的部分列出来，大家一起分工补齐，先解决眼前的问题。",
      "我昨天气头上还说了一句‘这位专家的记忆力真优秀’，回头觉得那样挖苦人不太合适。"]);
  scene("community", "2026-10-21T18:00:00", "分享", "朋友在组织给流浪动物和新搬来的居民做志愿活动。",
    ["我愿意试试，陌生人和小动物的处境也值得被认真对待。",
      "不过这周我只能拿出两小时，帮别人也要留好照顾自己的精力。",
      "我还是希望先把家人需要的事情安排好，再答应新的长期任务。",
      "但吃什么和穿什么是个人选择，我不觉得衣服或食物能决定一个人纯不纯洁。"]);
  scene("past", "2026-10-22T18:00:00", "提问", "回头看过去一年，有什么经历想保留下来？",
    ["那次一起逛植物园和周末散步，我现在想起还很喜欢，有人一起分享普通日子就够好了。",
      "我也感谢当时自己真的去申请了项目，虽然紧张，但决定和努力确实改变了结果。",
      "只是有一次准备得再充分也遇上天气取消，有些事情还是得接受环境会变。"]);
  scene("time-disagreement", "2026-10-23T18:00:00", "邀请", "我周末只想早上出门，你想下午，这回怎么约？",
    ["那我们中午见吧，你能晚一点，我也少睡一会儿，各让一步。",
      "如果你这周只有早上方便，这次先按你的时间，下一次再照顾我的安排。",
      "不过路线先别争了，我今天有点累，明天有精神再把没决定的部分聊完。"],
    { invite: "明确接受" });
  scene("lost-map", "2026-10-24T16:00:00", "玩笑", "咱们又在植物园里多绕了一圈，是不是导航在休假？",
    ["哈哈，我们不是迷路，是给植物园做了一次免费深度测评。",
      "我这方向感大概只对咖啡店有效，别的地方一律需要树木指路。"],
    { emotion: "积极" });
  const plants = scene("plants", "2026-10-25T10:00:00", "邀请", "植物园周末有个小展览，要不要一起去？",
    ["好啊！我之前没看过苔藓展，想看看它们在玻璃里怎么长。",
      "我先看看开馆时间，选一个不用排队的时段吧。"],
    { invite: "明确接受", emotion: "积极", delay: 12 });
  plants.messages[0].id = "analysis-plants-question";
  plants.messages[1].id = "analysis-plants-answer";
  plants.messages[2].id = "analysis-plants-time";
  scene("late-reply", "2026-10-26T10:00:00", "邀请", "周末一起散步吗？咖啡店新开了露台。",
    ["昨天手机没电又忙到很晚，刚看见。谢谢你叫我，周末再和你确认。"],
    { invite: "未明确答复", delay: 26 * 60 });
  scene("short-reply", "2026-10-27T19:00:00", "提问", "植物园的路线图看到了吗？",
    ["看到了，谢谢你。"], { response: "简短回应" });
  scene("volunteer-plan", "2026-10-28T18:00:00", "邀请", "周末先去社区志愿活动，再一起喝咖啡？",
    ["可以，先确认现场需要什么，再把你我能做的任务写清楚，和负责人一起找合适的分工。",
      "我想让以后做的项目对别人也有一点帮助，收入之外，工作有没有意义也很重要。",
      "同时我也很想把个人作品集做好，那是我认真追求的事情。"],
    { invite: "明确接受" });
  scene("reunion", "2026-10-29T18:00:00", "邀请", "几位老朋友周末聚餐，一起来吗？",
    ["来呀，很久没和大家一起聊了，和熟悉的朋友聚一聚反而让我充电。",
      "放心，我负责吃饭时给咖啡店递交一份忠实顾客报告。"],
    { invite: "明确接受", emotion: "积极" });
  scene("self-description", "2026-10-30T18:00:00", "提问", "你平时会怎么向新朋友介绍自己？",
    ["我上次测的 MBTI 是 INFJ，我自己也更习惯先听再说。",
      "我在关系里自认偏安全型，愿意靠近，也可以各做各的事。",
      "我会先说自己喜欢植物、散步和做设计，不太想只用职位介绍自己。",
      "不过在家人面前，我也会自然地想到自己是姐姐，做安排时要照顾到弟弟的时间。",
      "无论在同事还是朋友面前，我都不想为了看上去合群而假装喜欢不喜欢的东西。"]);
  scene("stress", "2026-10-31T19:00:00", "关心", "这个月工作压力很大，周末还好吗？",
    ["有时还是会先想到做砸了怎么办，睡前也容易反复想。",
      "但总体相信事情可以一步步变好，先完成手边的事，周末出去走走就能恢复一点。",
      "在工作汇报里我会更注意语气和顺序，跟你聊天就可以直接说自己累了。",
      "但悬着没答案时我也会不踏实，有时候急着先选一个最可能的解释。"], { emotion: "混合" });
  const coffee = scene("coffee", "2026-11-01T16:00:00", "邀请", "散步之后去喝咖啡？我可以先把地方定好。",
    ["好呀。你要是临时累了，改天也完全可以，别为了赴约把自己排太满。"],
    { invite: "明确接受", delay: 8 });
  coffee.messages[0].id = "analysis-coffee-question";
  coffee.messages[1].id = "analysis-coffee-answer";
  coffee.messages.push(
    { id: "analysis-coffee-followup", speaker: "我", timeMs: time("2026-11-01T16:11:00"),
      text: "这样挺好，周六中午再互相确认一下。", intent: "回应", emotion: "积极" },
    { id: "analysis-coffee-confirm", speaker: "林沐", timeMs: time("2026-11-01T16:13:00"),
      text: "好，周六再确认，不着急。", intent: "回应", emotion: "平静", response: "简短回应", invite: null }
  );
  scene("news", "2026-11-02T18:00:00", "分享", "看到那家咖啡店的新闻，周末可能会排队。",
    ["说到这个，我的植物终于长新叶了，你要看照片吗？"], { response: "转移话题", emotion: "积极" });
  scene("thanks", "2026-11-03T18:00:00", "关心", "这两天别把工作排太满，周末散步时慢慢聊。",
    ["谢谢你惦记，收到啦。"], { response: "简短回应" });
  scene("unclear", "2026-11-03T19:00:00", "分享", "周末可能下雨，咖啡店那边我再看看。",
    ["嗯……"], { response: "无法判断", emotion: "无法判断" });

  const byScene = new Map(scenes.map(item => [item.id, item]));
  const messages = scenes.flatMap(item => item.messages).sort((left, right) => left.timeMs - right.timeMs);
  const source = ([id, index]) => byScene.get(id).messages[index];
  const evidence = new Map();
  function cite(dimension, reference, note) {
    const item = source(reference);
    const id = dimension + ":" + item.id;
    if (!evidence.has(id)) evidence.set(id, {
      id, dim: dimension, note, excerpt: item.text, timeMs: item.timeMs, messageId: item.id, status: "ok",
    });
    return id;
  }
  function point(id, label, direction, detail, supports, counters, confidence = "有一定依据") {
    const counts = { low: direction < 0 ? supports.length : counters.length,
      high: direction > 0 ? supports.length : counters.length, mixed: 0 };
    return { id, label, detail, confidence, counts,
      position: Math.round((counts.high - counts.low) / (counts.high + counts.low + 2) * 1000) / 1000,
      support: supports.map(reference => cite(id, reference, detail)),
      counter: counters.map(reference => cite(id, reference, "这段原文提供了不同情境下的相反线索，应与支持依据一起看。")),
      ...(confidence === "把握较低" ? { tier: "reference" } : {}) };
  }
  const dimensions = [
    point("big5_o", "对新体验保持好奇", 1, "会主动了解没看过的展览，也享受把原理想明白。", [["plants", 1], ["exhibition-help", 1], ["new-route", 2]], [["pottery", 1]]),
    point("big5_c", "有计划，也给安排留余地", 1, "会查开放时间、把学习拆到每周，并认真完成约好的事。", [["plants", 2], ["career", 1], ["ethics", 4]], [["new-route", 1], ["coffee", 1]]),
    point("big5_e", "偏好安静的小范围相处", -1, "忙碌后更愿意安静散步和先听再说，熟人聚会也会让自己充电。", [["walk", 1], ["early-coffee", 1], ["self-description", 1]], [["reunion", 1]]),
    point("big5_a", "会照顾彼此的感受", 1, "愿意调整安排，也愿意共同找解决办法。", [["coffee", 1], ["route-disagreement", 1], ["family-invite", 2]], [["poster-feedback", 1], ["deadline-conflict", 1]]),
    point("big5_n", "压力大时容易反复担心", 1, "在截止前和工作压力大时会反复检查、担心结果，交付后能逐渐放松。", [["rest", 1], ["stress", 1]], [["rest", 2], ["relationship", 1]]),
    point("att_anx", "相处中较少反复确认", -1, "能接受亲近的人暂时忙碌，不过重要建议久未回复时仍会担心。", [["relationship", 1], ["self-description", 2]], [["relationship", 4], ["message-boundary", 2]]),
    point("att_avo", "愿意亲近，也需要独处", -1, "愿意向亲近的人求助，也能直接表达状态，密集来往时会保留独处时间。", [["relationship", 2], ["self-description", 2], ["coffee", 1]], [["relationship", 3]]),
    point("reject_sens", "通常能坦然接受暂时没回应", -1, "能把忙碌和拒绝区分开，但在很在意的话题上更敏感。", [["message-boundary", 1], ["family-invite", 2]], [["message-boundary", 2]]),
    point("cog_need", "愿意深究原理", 1, "看展时会追问分区和植物适应原理，不只满足于现成结论。", [["exhibition-help", 1], ["exhibition-help", 2], ["plants", 1]], [["exhibition-help", 3]]),
    point("cog_close", "愿意等更多信息", -1, "会先查资料和试不同版本，也有因悬而未决而急着选一个解释的时候。", [["exhibition-help", 2], ["poster-feedback", 2], ["pottery", 2]], [["stress", 4]]),
    point("locus", "相信行动能改变结果", -1, "会把能做的事拆开推进，也承认档期和天气等外部条件。", [["career", 2], ["past", 2], ["career", 1]], [["career", 3], ["past", 3]]),
    point("optimism", "愿意期待事情逐步变好", 1, "面对变化倾向于找可行的下一步，同时也会先想到不顺利的结果。", [["stress", 2], ["umbrella-joke", 2], ["new-route", 2]], [["stress", 1]]),
    point("self_constr", "先从个人兴趣认识自己", -1, "介绍自己时会提偏好与作品，也会把家人角色纳入安排。", [["self-description", 3], ["career", 1]], [["self-description", 4], ["family", 2]]),
    point("dialect", "愿意同时考虑两面的需要", 1, "能同时承认热闹和安静、储蓄和体验各自的价值。", [["public-rules", 1], ["money", 2], ["city-policy", 3]], [["deadline-conflict", 1]]),
    point("direct", "会把想法明确说出来", 1, "给反馈和拒绝邀请时表述清楚，拿不准时也会暂缓答复。", [["poster-feedback", 1], ["family-invite", 1], ["relationship", 2]], [["pottery", 1], ["unclear", 1]]),
    point("self_monitor", "会按场合调整表达", 1, "能区分正式汇报和朋友聊天，也会留意聚会里他人的感受。", [["poster-feedback", 3], ["stress", 3], ["message-boundary", 3]], [["self-description", 5]], "把握较低"),
    point("time_focus", "把当下安排和长期计划连接起来", 1, "对明年项目有具体准备，也愿意给临时探索留一点空白。", [["career", 1], ["volunteer-plan", 3], ["money", 1]], [["new-route", 1], ["exhibition-help", 3]]),
    point("time_past", "愿意珍惜过去的普通经历", 1, "回想一起散步和童年公园时以感谢和怀念为主，也保留遗憾。", [["plant-notes", 1], ["plant-notes", 2], ["past", 1]], [["plant-notes", 3]]),
    point("pol_axis", "公共议题上愿意考虑变化", -1, "希望公共空间更包容，同时也看重老街文化和安全秩序。", [["city-policy", 1], ["public-rules", 2]], [["city-policy", 4], ["public-rules", 3]], "把握较低"),
    point("pol_ext", "谈公共议题时偏温和", -1, "愿意听不同意见，尝试用小范围试行理解分歧。", [["city-policy", 1], ["public-rules", 1], ["public-rules", 2]], [["public-rules", 3]], "把握较低"),
    point("pol_econ", "支持基础公共投入", -1, "谈分配时强调基础服务和机会，也认可经营者的自主空间。", [["city-policy", 2], ["city-policy", 5]], [["city-policy", 3]], "把握较低"),
    point("pol_lib", "重视选择权和多元讨论", -1, "反对用群体压力强迫参与，也接受必要的安全规则。", [["city-policy", 1], ["ethics", 3], ["family", 3]], [["public-rules", 3]], "把握较低"),
    point("val_change", "愿意在稳妥中尝试新体验", -1, "会尝试新路线和展览，同时也会选择熟悉的地方休息。", [["new-route", 1], ["new-route", 2], ["plants", 1]], [["early-coffee", 2], ["pottery", 1]]),
    point("val_self", "会把他人的处境纳入考虑", 1, "愿意照顾共同活动中的精力和更陌生群体的需要，也会留好个人边界。", [["coffee", 1], ["community", 1], ["ethics", 2]], [["community", 2], ["money", 3]]),
    point("val_enh", "希望作品也能对别人有用", 1, "职业目标里有意义与责任，也保留个人成绩和收入追求。", [["volunteer-plan", 2], ["community", 1]], [["volunteer-plan", 3], ["money", 3]]),
    point("moral_care", "判断时会先考虑伤害", 1, "不认同让人难受却称作锻炼，也会优先照顾身体状况。", [["ethics", 2], ["route-disagreement", 3], ["coffee", 1]], [["deadline-conflict", 1]]),
    point("moral_fair", "重视分工与付出的公平", 1, "反对新人承担更多，遇到拖延也会顾及按时完成的人。", [["ethics", 1], ["deadline-conflict", 1], ["route-disagreement", 2]], [["time-disagreement", 2]]),
    point("moral_loyal", "认真对待对家人和伙伴的承诺", 1, "会为答应家人的事情留时间，也认为帮忙后应做好交接。", [["family", 2], ["family-invite", 1], ["ethics", 4]], [["family", 3]]),
    point("moral_auth", "接受有理由的共同规则", 1, "涉及公共安全和明确约定时会尊重规则，也保留个人决定。", [["public-rules", 3], ["deadline-conflict", 1]], [["family", 3], ["ethics", 3]], "把握较低"),
    point("moral_sanct", "尊重郑重与神圣的事物", 1, "对纪念和祭扫有敬重，也把这种边界与日常个人选择区分开。", [["family", 4], ["ethics", 5]], [["community", 4]], "把握较低"),
    point("moral_liberty", "重视自愿和个人决定", 1, "在家庭和群体活动里都希望保留拒绝与选择的权利。", [["ethics", 3], ["family", 3], ["relationship", 3]], [["public-rules", 3]]),
    point("moral_scope", "关怀会延伸到陌生人与动物", 1, "愿意考虑更广的对象，也会先安排亲近的人和自己的精力。", [["community", 1], ["volunteer-plan", 2], ["city-policy", 2]], [["community", 2], ["community", 3]]),
    point("moral_bind", "兼顾个人处境与共同承诺", -1, "关爱、公平与选择权经常成为理由，家人承诺和安全规则也有分量。", [["ethics", 1], ["ethics", 2], ["ethics", 3]], [["family", 2], ["public-rules", 3]], "把握较低"),
  ];
  function typeDimension(id, label, detail, groups) {
    const types = Object.fromEntries(Object.entries(groups).map(([name, refs]) => [name, refs.length]));
    return { id, label, detail, confidence: "有一定依据", types,
      support: groups[label].map(reference => cite(id, reference, detail)),
      counter: Object.entries(groups).filter(([name]) => name !== label).flatMap(([name, refs]) =>
        refs.map(reference => cite(id, reference, "这段表达呈现了另一种方式：" + name + "。"))) };
  }
  dimensions.push(
    typeDimension("humor", "亲和型", "常用一起开怀的说法拉近距离，也出现自强、自嘲与讽刺后的反思。", {
      亲和型: [["umbrella-joke", 1], ["walk", 3], ["lost-map", 1], ["reunion", 2]],
      自强型: [["rest", 2], ["umbrella-joke", 2]],
      攻击型: [["deadline-conflict", 3]], 自嘲型: [["lost-map", 2]],
    }),
    typeDimension("conflict", "合作", "有分歧时常一起找办法，也会按情境使用妥协、坚持、暂缓或迁就。", {
      合作: [["route-disagreement", 1], ["deadline-conflict", 2], ["volunteer-plan", 1]],
      妥协: [["route-disagreement", 2], ["time-disagreement", 1]],
      竞争: [["deadline-conflict", 1]], 回避: [["time-disagreement", 3]], 迁就: [["time-disagreement", 2]],
    })
  );

  const lifeDefinitions = [
    ["love", "感情观", "重视信任，也允许彼此独处", "在亲近关系里愿意表达和求助，不要求用持续回复证明在意。",
      [["relationship", 1], ["relationship", 2]], [["relationship", 3], ["relationship", 4]]],
    ["family", "家庭观", "认真保持联系，也有自主边界", "会履行对家人的承诺，同时坚持解释自己的重要决定。",
      [["family", 1], ["family", 2]], [["family", 3]]],
    ["money", "金钱观", "先留缓冲，再为喜欢的体验付费", "会考虑备用金和实际用途，也愿意给真正喜欢的体验留预算。",
      [["money", 1], ["money", 2]], [["money", 3]]],
    ["plan", "人生规划", "把长期想法拆成能开始的小事", "想尝试完整的展览项目，用每周学习和主动联系逐步推进。",
      [["career", 1], ["career", 2]], [["career", 3], ["new-route", 1]]],
    ["core", "价值底色", "看重舒适、尊重和做事的意义", "愿意照顾共同安排中的感受，也希望自己的作品对别人有用。",
      [["coffee", 1], ["volunteer-plan", 2]], [["community", 2], ["volunteer-plan", 3]]],
  ];
  const values = lifeDefinitions.map(([id, title, label, detail, supports, counters]) => ({
    id,
    selfReport: { statements: supports.map(reference => {
      const item = source(reference);
      return { text: item.text, timeMs: item.timeMs, evidence: [cite(id, reference, title + "的明确自述。")] };
    }) },
    inference: point(id, label, 1, detail, supports, counters),
  }));
  const familyKeys = {
    personality: ["big5_o", "big5_c", "big5_e", "big5_a", "big5_n", "att_anx", "att_avo", "reject_sens"],
    mind: ["cog_need", "cog_close", "locus", "optimism"], time: ["time_focus", "time_past"],
    expression: ["self_constr", "dialect", "direct", "self_monitor"],
    politics: ["pol_axis", "pol_ext", "pol_econ", "pol_lib"], values: ["val_change", "val_self", "val_enh"],
    moral: ["moral_care", "moral_fair", "moral_loyal", "moral_auth", "moral_sanct", "moral_liberty", "moral_scope", "moral_bind"],
    humor: ["humor"], conflict: ["conflict"], life: lifeDefinitions.map(([id]) => id),
  };
  const families = { personality: "性格与依恋", mind: "认知与动机", time: "时间观", expression: "表达与思维",
    politics: "政治光谱", values: "价值观", moral: "道德基础", humor: "幽默风格", conflict: "冲突处理", life: "生活观念" };
  const sources = {
  "big5_o": [
    "Goldberg (1990) JPSP, 59, 1216–1229",
    "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"
  ],
  "big5_c": [
    "Goldberg (1990) JPSP, 59, 1216–1229",
    "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"
  ],
  "big5_e": [
    "Goldberg (1990) JPSP, 59, 1216–1229",
    "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"
  ],
  "big5_a": [
    "Goldberg (1990) JPSP, 59, 1216–1229",
    "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"
  ],
  "big5_n": [
    "Goldberg (1990) JPSP, 59, 1216–1229",
    "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"
  ],
  "att_anx": [
    "Fraley, Waller & Brennan (2000) JPSP, 78, 350–365",
    "Bartholomew & Horowitz (1991) JPSP, 61, 226–244"
  ],
  "att_avo": [
    "Fraley, Waller & Brennan (2000) JPSP, 78, 350–365",
    "Bartholomew & Horowitz (1991) JPSP, 61, 226–244"
  ],
  "pol_axis": [
    "Pan & Xu (2018) Journal of Politics, 80, 254–273"
  ],
  "pol_econ": [
    "Pan & Xu (2018) Journal of Politics, 80, 254–273"
  ],
  "pol_lib": [
    "Pan & Xu (2018) Journal of Politics, 80, 254–273"
  ],
  "pol_ext": [
    "van Prooijen & Krouwel (2019) Current Directions in Psychological Science, 28, 159–163"
  ],
  "val_change": [
    "Schwartz (1992) Advances in Experimental Social Psychology, 25, 1–65"
  ],
  "val_self": [
    "Schwartz (1992) Advances in Experimental Social Psychology, 25, 1–65"
  ],
  "val_enh": [
    "Schwartz (1992) Advances in Experimental Social Psychology, 25, 1–65"
  ],
  "moral_care": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ],
  "moral_fair": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ],
  "moral_loyal": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ],
  "moral_auth": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ],
  "moral_sanct": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ],
  "moral_liberty": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ],
  "self_mbti": [
    "McCrae & Costa (1989) JPSP, 57, 17–40"
  ],
  "self_attachment": [
    "Fraley, Waller & Brennan (2000) JPSP, 78, 350–365",
    "Bartholomew & Horowitz (1991) JPSP, 61, 226–244"
  ],
  "self_politics": [
    "Pan & Xu (2018) Journal of Politics, 80, 254–273"
  ],
  "cog_need": [
    "Cacioppo & Petty (1982) JPSP, 42, 116–131",
    "Cacioppo, Petty & Kao (1984) Journal of Personality Assessment, 48, 306–307"
  ],
  "cog_close": [
    "Kruglanski & Webster (1996) Psychological Review, 103, 263–283"
  ],
  "locus": [
    "Rotter (1966) Psychological Monographs, 80, 1–28"
  ],
  "reject_sens": [
    "Downey & Feldman (1996) JPSP, 70, 1327–1343"
  ],
  "self_constr": [
    "Markus & Kitayama (1991) Psychological Review, 98, 224–253"
  ],
  "dialect": [
    "Peng & Nisbett (1999) American Psychologist, 54, 741–754"
  ],
  "direct": [
    "Hall (1976) Beyond Culture. Anchor Books"
  ],
  "self_monitor": [
    "Snyder (1974) JPSP, 30, 526–537"
  ],
  "moral_scope": [
    "Crimston et al. (2016) JPSP, 111, 636–653"
  ],
  "optimism": [
    "Scheier & Carver (1985) Health Psychology, 4, 219–247"
  ],
  "time_focus": [
    "Zimbardo & Boyd (1999) JPSP, 77, 1271–1288"
  ],
  "time_past": [
    "Zimbardo & Boyd (1999) JPSP, 77, 1271–1288"
  ],
  "humor": [
    "Martin et al. (2003) Journal of Research in Personality, 37, 48–75"
  ],
  "conflict": [
    "Thomas & Kilmann (1974) Thomas-Kilmann Conflict MODE Instrument. Xicom",
    "Rahim (1983) Academy of Management Journal, 26, 436–448"
  ],
  "moral_bind": [
    "Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
    "Graham et al. (2009) JPSP, 96, 1029–1046",
    "Atari et al. (2023) JPSP, 125, 1157–1188"
  ]
};
  const selfReport = (value, reference) => {
    const item = source(reference);
    return { value, quote: item.text, timeMs: item.timeMs };
  };
  const fromMs = messages[0].timeMs;
  const toMs = messages.at(-1).timeMs;
  const persona = {
    createdAtMs: cutoffMs, stale: false,
    observations: [
      { id: "pace-observation", version: 1, dimension: "big5_a", judgement: "约活动时会主动留出调整空间",
        note: "上次散步约好中午再确认，双方都轻松些。" },
      { id: "planning-observation", version: 1, dimension: "big5_c", judgement: "会查路线和开放时间，也接受临时调整",
        note: "我对几次共同安排的个人观察。" },
      { id: "life-observation", version: 1, dimension: "core", judgement: "比起赶很多行程，更享受有余地的相处",
        note: "这条观察可以编辑或删除。" },
    ],
    labels: Object.fromEntries([...dimensions.map(item => [item.id, item.label]),
      ...lifeDefinitions.map(([id, title]) => [id, title])]),
    catalog: Object.entries(familyKeys).flatMap(([group, keys]) => keys.map(id => ({ id, group }))),
    sources, families,
    result: {
      dimensions,
      selfReports: {
        mbti: selfReport("INFJ", ["self-description", 1]),
        attachment: selfReport("安全型", ["self-description", 2]),
        politics: selfReport("中间", ["city-policy", 1]),
      },
      values,
      coverage: { fromMs, toMs, processed: messages.filter(item => item.speaker === "林沐").length,
        excluded: 0, evidence: evidence.size, cited: evidence.size },
      historyComplete: true, model: "演示分析", drift: [],
    },
  };

  // Use the production block and reply-window rules for the same fictional transcript.
  const blocks = [];
  for (const item of messages) {
    const previous = blocks.at(-1);
    if (previous?.speaker === item.speaker) previous.messages.push(item);
    else blocks.push({ id: item.id, speaker: item.speaker, messages: [item], intent: item.intent, emotion: item.emotion,
      response: item.response, invite: item.invite });
  }
  const first = block => block.messages[0].timeMs;
  const last = block => block.messages.at(-1).timeMs;
  const replyDelays = [];
  const reply = { eligible: 0, replied: 0, observing: 0, excluded: 0 };
  const paired = [];
  blocks.forEach((block, index) => {
    if (block.speaker !== "我") return;
    const following = blocks[index + 1];
    if (following && first(following) - last(block) <= day) {
      reply.eligible++;
      reply.replied++;
      replyDelays.push((first(following) - last(block)) / 1000);
      paired.push({ asked: block, answered: following });
    } else if (cutoffMs - last(block) >= day) reply.eligible++;
    else reply.observing++;
  });
  const sortedDelays = replyDelays.toSorted((left, right) => left - right);
  const middle = Math.floor(sortedDelays.length / 2);
  reply.rate = reply.eligible >= 5 ? reply.replied / reply.eligible : null;
  reply.medianSeconds = sortedDelays.length < 3 ? null : sortedDelays.length % 2 ?
    sortedDelays[middle] : (sortedDelays[middle - 1] + sortedDelays[middle]) / 2;
  function distribution(items, key, labels) {
    const counted = items.map(item => item[key]).filter(value => labels.includes(value));
    return { total: counted.length, items: labels.map(label => ({
      label, count: counted.filter(value => value === label).length,
      share: counted.filter(value => value === label).length / counted.length,
    })).filter(item => item.count) };
  }
  const intents = ["提问", "分享", "邀请", "关心", "玩笑", "请求帮助", "回应", "混合", "其他"];
  const emotions = ["积极", "平静", "消极", "混合", "无法判断"];
  const responseLabels = ["接住并展开", "简短回应", "转移话题", "明确婉拒", "新话题"];
  const responses = [...new Set(paired.map(pair => pair.asked.intent))].map(ask => {
    const rounds = paired.filter(pair => pair.asked.intent === ask);
    const determinable = rounds.filter(pair => responseLabels.includes(pair.answered.response)).length;
    return { ask, rounds: rounds.length, determinable, undetermined: rounds.length - determinable,
      fromMs: first(rounds[0].asked), toMs: last(rounds.at(-1).answered),
      items: responseLabels.map(label => {
        const matching = rounds.filter(pair => pair.answered.response === label);
        return { label, count: matching.length, share: determinable >= 5 ? matching.length / determinable : null,
          examples: matching.slice(0, 3).map(pair => pair.asked.id) };
      }).filter(item => item.count) };
  }).sort((left, right) => right.rounds - left.rounds);
  const invitationCounts = { 明确接受: 0, 明确婉拒: 0, 未明确答复: 0, 无法判断: 0 };
  for (const pair of paired) if (pair.asked.intent === "邀请") invitationCounts[pair.answered.invite ?? "无法判断"]++;
  const unanswered = blocks.filter(block => block.speaker === "我" && block.intent === "邀请" &&
    !paired.some(pair => pair.asked === block)).length;
  invitationCounts.未明确答复 += unanswered;
  const signals = {};
  const keywords = {};
  const vocabulary = ["周末", "咖啡", "散步", "植物园", "计划", "工作", "朋友", "一起", "家人", "时间"];
  for (const [side, speaker] of [["self", "我"], ["other", "林沐"]]) {
    const own = blocks.filter(block => block.speaker === speaker);
    signals[side] = { blocks: own.length, labelled: own.length,
      intent: distribution(own, "intent", intents), emotion: distribution(own, "emotion", emotions) };
    const text = messages.filter(item => item.speaker === speaker).map(item => item.text).join("\n");
    keywords[side] = vocabulary.map(word => ({ word, count: text.split(word).length - 1 }))
      .filter(item => item.count >= 2).sort((left, right) => right.count - left.count);
  }
  const chat = { configured: true, historyReady: true,
    coverage: { fromMs, toMs, blocks: blocks.length, pending: 0, missingTextBlocks: 0, outdated: false },
    reply, statistics: { responses, signals, keywords,
      invites: { counts: invitationCounts, total: Object.values(invitationCounts).reduce((sum, count) => sum + count, 0),
        withoutReply: unanswered } } };

  function personaEvidence(ids) {
    return { items: ids.map(id => evidence.get(id)).filter(Boolean) };
  }
  function publicMessage(item, target) {
    return { id: item.id, speaker: item.speaker, timeMs: item.timeMs, text: item.text,
      ...(target === undefined ? {} : { target: item.id === target }) };
  }
  function round(id) {
    const alias = byScene.get(id)?.messages[0].id ?? id;
    const pair = paired.find(item => item.asked.id === alias);
    if (!pair) return { blocks: [] };
    return { blocks: [pair.asked, pair.answered].map(block => ({ messages: block.messages.map(item => publicMessage(item)) })) };
  }
  function messageContext(id) {
    const thread = scenes.find(item => item.messages.some(message => message.id === id));
    return thread ? { status: "ok", messages: thread.messages.map(item => publicMessage(item, id)) } : null;
  }

  window.FriendDemoAnalysis = { persona, chat, personaEvidence, round, messageContext };
})();
