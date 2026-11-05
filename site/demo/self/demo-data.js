"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Every person, utterance, event, count, score, and interpretation below is fictional.
// Question wording mirrors the shipped IPIP and project-authored value questionnaires.
(() => {
  const at = date => Date.parse(date + "T18:30:00+09:00");
  const profile = {
    background: "我做视觉设计，喜欢把复杂的事整理清楚。下班后常去河边散步，周末偶尔看摄影展、画速写。比起赶场，更喜欢留出一点空白的安排。\n\n这几年从跟着团队做项目，到独立负责小作品，也慢慢学会说出自己需要休息。和熟悉的朋友聊天时很放松，会记住对方随口提过的小事。",
    style: "语气随和，常用短句，习惯先回应对方，再商量具体时间。喜欢说“好呀”“慢慢来”“别排太满”。\n\n想去就直接说期待，没空也会说明并提个替代时间。安慰朋友时先听具体发生了什么，少讲空泛的道理。",
    version: 1,
  };
  const samples = [
    ["苏禾", "2026-10-19", "苏禾：周日一早去逛展？", "想去呀！不过早上我想睡个懒觉，下午见？", "周末 邀请 见面 逛展"],
    ["林沐", "2026-10-27", "林沐：下午想去公园走一圈吗？", "好呀，走累了就找家店坐坐，别把行程排太满。", "周末 邀请 散步 公园 咖啡"],
    ["周屿", "2026-09-14", "周屿：那三点在公园门口碰头？", "可以呀，三点见。我带相机，咱们边走边挑路线。", "时间 确认 三点 公园 摄影"],
    ["许念", "2026-08-21", "许念：最近是不是特别忙？周末还出来吗？", "这周确实有点累。想见你，但不想安排得太赶，我们就喝杯咖啡？", "累 忙 加班 休息 咖啡 边界"],
    ["林沐", "2026-07-12", "林沐：如果你临时加班，我们改天也行。", "好呀，谢谢你留出余地。周五我再和你确认，免得你白等。", "改天 确认 加班 时间"],
    ["苏禾", "2026-06-07", "苏禾：你推荐的摄影展还有一周，要去吗？", "想看！上次聊的那组街景就在里面吧？周日下午我有空。", "看展 摄影 周末 邀请"],
    ["周屿", "2025-12-11", "周屿：做完项目终于能喘口气了？", "终于交出去啦。还有小地方想修，不过今晚先关电脑，出去走走。", "工作 项目 完成 休息 散步"],
    ["许念", "2025-10-23", "许念：我今天有点难受，也不知道怎么说。", "那先不用组织得很清楚。想说哪一点就从哪一点开始，我在听。", "难受 安慰 聊聊 陪伴"],
    ["苏禾", "2025-06-18", "苏禾：要不我们周末把两个展都看了？", "我更想慢慢看一个。还有精力就多逛一会儿，下个留到下次。", "看展 周末 节奏 安排 计划"],
    ["林沐", "2025-03-09", "林沐：这个月攒钱是不是挺辛苦的？", "我想先留一笔备用金。喜欢的东西还是会买，只是隔两天再决定。", "钱 消费 规划 存钱"],
    ["周屿", "2024-11-04", "周屿：那个速写课你报名了吗？", "报啦，先上两次试试。不要求马上画好，能每周画一点就行。", "速写 学习 兴趣 尝试"],
    ["许念", "2024-05-22", "许念：周末回家吗？你爸妈说一起吃饭。", "回呀，我周六下午到。吃完一起散个步，别忙着做一桌菜啦。", "家人 家庭 周末 散步"],
  ].map(([name, date, input, reply, keywords]) => ({
    name, timeMs: at(date), input, reply, keywords, matchKinds: ["semantic"],
  }));
  const tracks = [
    { name: "工作", color: "#07c160", visible: true, builtin: true },
    { name: "兴趣", color: "#576b95", visible: true, builtin: true },
    { name: "关系", color: "#c2841c", visible: true, builtin: true },
  ];
  const events = [
    ["2026-10-18", "和林沐去了植物展", "关系", "上次一起去植物展很开心，温室里的苔藓像小森林。", "important"],
    ["2026-09-05", "第一次带朋友看自己的小展", "兴趣", "这次把速写装进相框了，周末想带你们去看看。", "important"],
    ["2026-06-21", "开始每周留一个晚上休息", "工作", "周三晚上我想留给自己，关掉工作群去散步。", "general"],
    ["2026-03-13", "独立负责一个品牌小项目", "工作", "这回从提案到交付都由我来，紧张但也挺期待。", "important"],
    ["2025-12-11", "做完第一个完整独立项目", "工作", "终于把第一版交出去了。今晚先关电脑，出去走走。", "important"],
    ["2025-10-23", "陪许念慢慢聊过一次低谷", "关系", "不用一下想明白。今晚先吃点东西，我陪你聊一会儿。", "general"],
    ["2025-07-06", "和苏禾看了街景摄影展", "兴趣", "那张下雨的街角我站了好久，回去也想试试拍一张。", "general"],
    ["2025-03-09", "开始准备自己的备用金", "工作", "我想留一笔余地，这样做决定时就不用那么着急。", "general"],
    ["2024-11-04", "报名了速写体验课", "兴趣", "先上两次试试，不要求马上画好。", "important"],
    ["2024-08-17", "和周屿第一次走河边步道", "关系", "原来从桥头慢慢走到那家咖啡店，只要半小时。", "general"],
    ["2024-05-22", "回家陪家人过了个普通周末", "关系", "吃完饭陪他们在楼下走了一圈，觉得这样就很好。", "general"],
    ["2024-02-16", "第一次在团队里主讲设计提案", "工作", "讲之前我在楼下练了两遍，上台后反而慢慢放松了。", "important"],
  ].map(([date, title, track, phrase, importance], index) => ({
    id: "demo-life-" + index, title, track, phrase, importance,
    status: "occurred", level: "confirmed", startMs: at(date), endMs: null,
    precision: "day", whenText: date.replace("-", "年").replace("-", "月") + "日",
    evidenceCount: 1, reason: "根据虚构聊天中的本人自述整理。",
  }));
  const periods = [
    { id: "demo-work-period", title: "从协作到独立负责", track: "工作", startMs: at("2024-01-01"), endMs: null, precision: "month", whenText: "2024年1月至今", ongoing: true, evidenceCount: 3 },
    { id: "demo-art-period", title: "用速写和照片记住日常", track: "兴趣", startMs: at("2024-11-01"), endMs: null, precision: "month", whenText: "2024年11月至今", ongoing: true, evidenceCount: 3 },
  ];
  const evidence = (quote, index = 0) => ({
    id: "demo-original-" + index,
    chat: ["林沐", "苏禾", "周屿", "许念"][index % 4],
    user: ["林沐", "苏禾", "周屿", "许念"][index % 4],
    time: at("2026-10-18"), quote,
    contextBefore: [{ side: "other", text: "你现在更喜欢怎样安排？" }],
    contextAfter: [{ side: "other", text: "好呀，那就按舒服的节奏来。" }],
  });
  const dimensions = [
    ["big5_e", "更喜欢和熟悉的朋友在轻松的小场景里相处。", "比起人多的聚会，我更想找家安静的店慢慢聊。", -0.35],
    ["big5_a", "愿意听别人说完，也会主动考虑对方的方便。", "不用一次说清楚，想从哪一点开始都行，我在听。", 0.55],
    ["big5_c", "重要的事会提前准备，也给计划留出调整余地。", "我先把周五要交的东西列出来，剩下的明天再看。", 0.3],
    ["big5_n", "遇到不确定的事会紧张，整理步骤后通常能缓下来。", "刚接到任务有点慌，拆成三步以后好多了。", -0.2],
    ["big5_o", "乐于尝试新的兴趣，愿意花时间理解作品和想法。", "想试试胶片摄影，先随便拍几张，看看光会是什么样。", 0.6],
    ["att_anx", "在熟悉关系里，会直接表达需要，也能接受对方暂时忙碌。", "你先忙，晚上方便时再说就行，我今天也要整理东西。", -0.45],
    ["att_avo", "愿意向亲近的人求助，同时会说明自己需要独处的时间。", "这件事我想听听你的想法，不过今晚我先歇一会儿。", -0.3],
    ["pol_axis", "讨论公共议题时，倾向考虑不同处境的人是否方便参与。", "这个服务要是只让熟悉手机的人用，老人可能就被落下了。", -0.25],
    ["pol_ext", "表达看法时常保留条件，也愿意听不同的具体经历。", "我目前偏向这个办法，但得再听听实际用的人怎么说。", -0.55],
    ["pol_econ", "在公共服务议题里，更看重基本需要能否得到保障。", "住得远的人也需要方便的交通，不能只看哪条线最赚钱。", -0.3],
    ["pol_lib", "重视个人选择，也希望规则有清楚的边界。", "只要没伤害别人，生活方式还是让每个人自己选吧。", -0.4],
    ["val_change", "愿意尝试新机会，同时保留稳定的生活节奏。", "我想试试独立做项目，也会先留好几个月的余地。", -0.25],
    ["val_self", "会考虑自己的承受范围，也愿意为重要的人留出时间。", "今天我需要早点休息，但明晚可以认真听你说。", 0.35],
    ["moral_care", "做判断时常注意具体的人会不会难受或被忽略。", "先想想这个安排会不会让他一个人扛太多。", 0.65],
    ["moral_fair", "重视分工和机会是否清楚、公平。", "既然一起做，就把每个人负责和得到的部分说清楚。", 0.55],
    ["moral_liberty", "尊重个人边界和选择。", "你可以不去，不用因为大家都答应了就勉强自己。", 0.6],
    ["moral_loyal", "重视承诺，但愿意具体沟通能力范围。", "答应的部分我会做完，临时多出来的我们再商量。", 0.2],
    ["moral_auth", "愿意理解规则的用途，也会问不合适的地方能否调整。", "如果这条流程只是让大家重复填表，能不能简化一下？", -0.1],
    ["moral_sanct", "谈到习惯和传统时，更在意它是否适合当下的人。", "仪式可以简单一点，大家自在、愿意参与就很好。", -0.15],
  ].map(([key, text, quote, position], index) => ({
    key, label: key, manual: null, selfReport: null,
    inference: {
      text, position, confidence: "有一定依据", scope: "仅限演示聊天",
      counts: position < 0 ? { low: 2, high: 0, mixed: 1 } : { low: 0, high: 2, mixed: 1 },
      evidence: [evidence(quote, index)], counter: [],
    },
  }));
  const viewRows = [
    ["love", "感情观", "希望相处中互相支持，也能直接说出各自需要。", "我希望我们能说清楚需要，不靠猜，也各自留一点空间。"],
    ["family", "家庭观", "看重日常的陪伴，愿意关心家人又尊重彼此选择。", "不用一定做什么大事，一起吃顿饭、散个步就很好。"],
    ["money", "金钱观", "会留一笔备用金，也愿意为真正喜欢的体验花钱。", "我想先留好备用金，喜欢的展览还是会去看。"],
    ["plan", "人生规划", "先把眼前的小作品做好，再逐步争取更独立的工作。", "先认真做好这一版，下一次再试更完整的东西。"],
    ["core", "价值底色", "重视真诚、具体的照顾和能持续的生活节奏。", "不想每次都拼到耗尽，能一直做喜欢的事更重要。"],
  ];
  const values = {
    version: 1, coverage: { ownMessages: 184, analyzedMessages: 184 },
    dimensions: viewRows.map(([key, label, text, quote], index) => ({
      key, label, manual: null, selfReport: null,
      inference: { text, confidence: "有一定依据", scope: "仅限演示聊天", evidence: [evidence(quote, index)], counter: [] },
    })),
  };
  const scoreKeys = ["big5_e", "big5_a", "big5_c", "big5_n", "big5_o"];
  const periodRows = [
    ["2024H1", [3.5, 3.8, 2.8, 3.6, 3.5]],
    ["2024H2", [3.3, 3.9, 3.0, 3.4, 3.7]],
    ["2025H1", [3.0, 4.0, 3.2, 3.2, 3.9]],
    ["2025H2", [2.8, 4.1, 3.4, 3.0, 4.0]],
    ["2026H1", [2.7, 4.0, 3.6, 2.8, 4.1]],
    ["2026H2", [2.6, 4.1, 3.6, 2.7, 4.2]],
  ];
  const label = id => id.slice(0, 4) + (id.endsWith("H1") ? " 上半年" : " 下半年");
  const factors = (keys, scores, total = 10) => Object.fromEntries(keys.map((key, index) => [key, {
    score: scores[index], low: scores[index] - 0.15, high: scores[index] + 0.15, items: total, total, chats: 4,
  }]));
  const personalityPeriods = {
    months: 6, coverage: { total: 6, completed: 6, pending: 0, insufficient: 0 },
    periods: periodRows.map(([id, scores]) => ({
      id, label: label(id), status: "complete", passes: 3, items: 50,
      factors: factors(scoreKeys, scores),
      letters: [scores[0] >= 3 ? "E" : "I", scores[4] >= 3 ? "N" : "S", scores[1] >= 3 ? "F" : "T", scores[2] >= 3 ? "J" : "P"],
    })),
  };
  const worldviewPeriods = {
    months: 6, coverage: { total: 6, completed: 6, pending: 0, insufficient: 0 },
    periods: periodRows.map(([id], index) => ({
      id, label: label(id), status: "complete", passes: 3, items: 24,
      factors: factors(["pol_econ", "pol_lib", "val_change", "val_self"], [2.8 - index * .04, 2.6 - index * .03, 2.8 - index * .02, 3.2 + index * .04], 6),
      letters: ["社会民主主义"], style: "理想派",
      views: Object.fromEntries(viewRows.map(([key, , text, quote], item) => [key, {
        item: 101 + item, text: index < 2 && key === "plan" ? "先积累团队协作的经验，再找到自己喜欢的方向。" : text,
        status: "complete",
        evidence: [{ id: "demo-view-" + id + "-" + key, k: "自述", s: 1, name: "林沐", time: at(id.slice(0, 4) + "-06-10"), text: quote }],
      }])),
    })),
  };
  const tagRows = [
    ["速写", "兴趣", "interest", "周末找家安静的咖啡馆，慢慢画一张。", "主动安排时间练习速写。"],
    ["胶片摄影", "兴趣", "interest", "想试试胶片摄影，先随便拍几张。", "明确想尝试的新兴趣。"],
    ["看展", "兴趣", "interest", "那组街景我站了好久，下次还想再看一次。", "反复提到愿意花时间看展。"],
    ["河边散步", "生活方式", "interest", "关电脑以后去河边走半小时，脑子会清楚一点。", "说明了主动选择的日常活动。"],
    ["安静的小店", "生活方式", "interest", "比起赶场，我想找家安静的店慢慢聊。", "直接表达对相处环境的偏好。"],
    ["留一点空白", "性格习惯", "trait", "周末别排太满，留点时间看当天想做什么。", "在多次安排活动时表达同一种节奏偏好。"],
    ["提前准备", "性格习惯", "trait", "我先把要交的东西列出来，明天按顺序做。", "描述自己习惯的工作准备方式。"],
    ["视觉设计", "工作", "trait", "这次品牌小项目从提案到交付都由我来。", "在本人工作经历中明确出现。"],
    ["做饭", "其他", "occasional", "这个周末试着做了番茄汤，居然没有翻车。", "只有一次具体参与，保留为偶尔参与。"],
    ["马拉松", "其他", "mention", "你说的马拉松路线会经过那座桥吗？", "仅在问朋友的活动，不当成本人兴趣。"],
  ];
  const questionTexts = [
    "我是聚会上活跃气氛的人", "我不太关心别人", "我总是提前做好准备", "我很容易感到有压力", "我的词汇量很丰富",
    "我话不多", "我对别人很感兴趣", "我的东西经常到处乱放", "我大多数时候很放松", "我很难理解抽象的概念",
    "我和人相处时感到自在", "我会出口伤人", "我很注意细节", "我常常担心各种事", "我的想象力很丰富",
    "我喜欢待在不显眼的位置", "我能体谅别人的感受", "我常把事情弄得一团糟", "我很少情绪低落", "我对抽象的想法不感兴趣",
    "我会主动和人搭话", "我对别人的烦恼不感兴趣", "手头的杂事我会马上做完", "我很容易心绪不宁", "我常有很好的想法",
    "我没什么话可说", "我心肠很软", "我常忘记把东西放回原处", "我很容易心烦", "我的想象力不太好",
    "聚会上我会和很多不同的人聊天", "我其实对别人没什么兴趣", "我喜欢井井有条", "我的情绪经常变化", "我理解事情很快",
    "我不喜欢引人注意", "我愿意花时间陪别人、帮别人", "我会逃避自己该做的事", "我的情绪常常大起大落", "我喜欢用艰深的词",
    "我不介意成为众人的焦点", "我能感受到别人的情绪", "我按计划表做事", "我很容易被惹恼", "我常花时间反思",
    "在陌生人面前我很安静", "我能让别人感到放松自在", "我对工作要求很严格", "我经常情绪低落", "我脑子里有很多点子",
  ];
  const questionKeys = {
    big5_e: [1, -6, 11, -16, 21, -26, 31, -36, 41, -46],
    big5_a: [-2, 7, -12, 17, -22, 27, -32, 37, 42, 47],
    big5_c: [3, -8, 13, -18, 23, -28, 33, -38, 43, 48],
    big5_n: [4, -9, 14, -19, 24, 29, 34, 39, 44, 49],
    big5_o: [5, -10, 15, -20, 25, -30, 35, 40, 45, 50],
  };
  window.SelfDemoData = Object.freeze({
    at, profile, samples, tracks, events, periods, evidence, values,
    persona: { version: 1, coverage: { ownMessages: 184, analyzedMessages: 184 }, dimensions },
    personalityPeriods, worldviewPeriods,
    tags: {
      account: "演示账号", run: { state: "complete" }, counts: { messages: 184, pendingMessages: 0 },
      categories: ["兴趣", "生活方式", "性格习惯", "工作", "其他"],
      tags: tagRows.map(([name, category, relation], index) => ({
        key: name, name, category, relation, level: index < 8 ? "confirmed" : "possible",
        mentions: index < 8 ? 3 + index % 4 : 1, chats: index < 8 ? 2 : 1, days: index < 8 ? 3 : 1,
      })),
    },
    tagRows, questionTexts, questionKeys,
  });
})();
