// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Optional real-model check: never downloads weights and only uses synthetic text.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createEncoder } = require("../scripts/embedding-runner.cjs");
const directory = path.resolve(__dirname, "../.local/core/embeddings/bge-small-zh-v1.5");
const available = ["model.onnx", "tokenizer.json", "tokenizer_config.json"].every(name => fs.existsSync(path.join(directory, name)));

test("local BGE retrieves paraphrased Chinese chat situations with real ONNX inference", { skip: !available, timeout: 30000 }, async () => {
  const encoder = await createEncoder(directory);
  try {
    const pairs = [
      ["我最近每天都睡不好，压力真的很大。", "连续好几晚合不上眼，事情堆着快崩溃了"],
      ["这周六晚上有空吗？一起吃顿饭吧。", "周末约个晚餐，见面聊聊？"],
      ["你能不能借我两千块钱？月底还给你。", "手头有点紧，可以先周转一点现金吗"],
      ["方案明天上午要提交，麻烦你今晚确认一下。", "文件的截止日期快到了，今天得审完"],
      ["今天是我的生日！", "又长大一岁啦，准备切蛋糕"],
      ["谢谢你昨天帮我搬家，辛苦啦。", "上次多亏有你搭把手，太感谢了"],
      ["我刚跟男朋友分手了，心里特别难受。", "恋爱结束以后整个人都很痛苦"],
      ["你是不是生我的气了？怎么一直没回复。", "一直不理我，是不是哪里惹到你了"],
      ["请问快递放在门口了吗？", "我的包裹送到了什么位置"],
      ["杭州有什么好吃的餐馆推荐？", "想去吃杭帮菜，有没有合适的店"],
      ["最近咳嗽发烧，身体很不舒服。", "体温很高还不停地咳，浑身难受"],
      ["演唱会门票已经买好了，下周一起去。", "下星期看现场音乐表演，你也一块来吧"],
    ];
    const passages = await encoder.encode(pairs.map(([text]) => "对方：" + text), "passage");
    const queries = await encoder.encode(pairs.map(([, text]) => text), "query");
    let top1 = 0;
    for (let i = 0; i < queries.length; i++) {
      assert.equal(queries[i].length, 512);
      assert.ok(Math.abs(Math.hypot(...queries[i]) - 1) < 1e-6);
      const ranked = passages.map((passage, index) => ({ index,
        similarity: passage.reduce((total, value, column) => total + value * queries[i][column], 0) }))
        .sort((a, b) => b.similarity - a.similarity);
      if (ranked[0].index === i) top1++;
      assert.ok(ranked.slice(0, 3).some(row => row.index === i), "paraphrase must remain in top 3: " + pairs[i][1]);
    }
    // This is a small regression fixture, not a benchmark of real personal-chat quality.
    assert.ok(top1 >= 10, `synthetic top-1 recall: ${top1}/${pairs.length}`);
  } finally { await encoder.close(); }
});
