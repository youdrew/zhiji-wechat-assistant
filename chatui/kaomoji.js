"use strict";
// Modified by Know Your Friends, 2026-10-13 (Apache-2.0): draft kaomoji only; mood labels are retired.
(function () {
  const bank = {
    happy: { category: "愉快", variants: ["(＾▽＾)", "(≧▽≦)", "(⌒‿⌒)"] },
    affectionate: { category: "亲近", variants: ["(｡♥‿♥｡)", "(♡˙︶˙♡)", "( ˘ ³˘)♥"] },
    neutral: { category: "平静", variants: ["( ˘͈ ᵕ ˘͈ )", "(￣ー￣)", "( ´ ▽ ` )"] },
    amused: { category: "有趣", variants: ["(｡•̀ᴗ-)✧", "(≧∇≦)ﾉ", "( ´艸｀)"] },
    sad: { category: "难过", variants: ["(´；ω；`)", "(╥﹏╥)", "(｡•́︿•̀｡)"] },
    anxious: { category: "不安", variants: ["(；´Д｀)", "(｡•́︿•̀｡)", "(；￣Д￣)"] },
    angry: { category: "生气", variants: ["(｀ε´)", "(╬ಠ益ಠ)", "(＃｀д´)ﾉ"] },
    excited: { category: "兴奋", variants: ["(๑•̀ㅂ•́)و✧", "(ﾉ◕ヮ◕)ﾉ", "(≧∇≦)ﾉ"] },
    surprised: { category: "惊讶", variants: ["(⊙_⊙)", "(ﾟДﾟ)", "(°o°)"] },
    shy: { category: "害羞", variants: ["(*/ω＼*)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)", "(〃▽〃)"] },
    grateful: { category: "感谢", variants: ["(人´∀｀)", "( ˘ ³˘)♥", "(｡•̀ᴗ-)✧"] },
    content: { category: "满足", variants: ["(￣▽￣)", "( ˘͈ ᵕ ˘͈ )", "(๑´ڡ`๑)"] },
    relieved: { category: "释然", variants: ["( ´ ▽ ` )", "(￣▽￣)ノ", "( ˘ω˘ )"] },
    hopeful: { category: "期待", variants: ["(☆▽☆)", "(๑•̀ㅂ•́)و✧", "( ˙꒳˙ )✧"] },
    curious: { category: "好奇", variants: ["(・_・?)", "(◎_◎;)", "(｡･ω･｡)?"] },
    proud: { category: "自豪", variants: ["(￣^￣)ゞ", "(๑˃ᴗ˂)ﻭ", "( •̀ᄇ•́)ﻭ✧"] },
    tender: { category: "温柔", variants: ["(づ｡◕‿‿◕｡)づ", "( ˘ ³˘)♥", "(｡･ω･｡)ﾉ♡"] },
    lonely: { category: "孤单", variants: ["(｡•́︿•̀｡)", "( ´•̥̥̥ω•̥̥̥` )", "(つ﹏⊂)"] },
    disappointed: { category: "失望", variants: ["(︶︹︺)", "(｡•́︿•̀｡)", "(╯︵╰,)"] },
    hurt: { category: "受伤", variants: ["(╥﹏╥)", "(｡•́︿•̀｡)", "(つ﹏⊂)"] },
    guilty: { category: "愧疚", variants: ["(；´д｀)ゞ", "(._.)", "(｡•́︿•̀｡)"] },
    ashamed: { category: "羞愧", variants: ["(〃￣ω￣〃ゞ", "(*/_＼)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)"] },
    fearful: { category: "害怕", variants: ["(ﾟДﾟ;)", "(；ﾟДﾟ)", "(⊙﹏⊙)"] },
    worried: { category: "担忧", variants: ["(；￣Д￣)", "(｡•́︿•̀｡)", "(；´Д｀)"] },
    stressed: { category: "紧绷", variants: ["(；´Д｀)", "(＞﹏＜)", "(；￣Д￣)"] },
    overwhelmed: { category: "不堪重负", variants: ["(＠_＠)", "(×_×)", "(；´Д｀)"] },
    frustrated: { category: "挫败", variants: ["(ノ_<。)", "(╯︵╰,)", "(＞﹏＜)"] },
    irritated: { category: "烦躁", variants: ["(눈_눈)", "(¬_¬)", "(｀ε´)"] },
    resentful: { category: "怨怼", variants: ["(눈_눈)", "(¬_¬)", "(｀Д´)"] },
    jealous: { category: "吃醋", variants: ["(¬_¬)", "(￣へ￣)", "(눈_눈)"] },
    suspicious: { category: "怀疑", variants: ["(¬_¬)", "(￢_￢)", "(눈_눈)"] },
    confused: { category: "困惑", variants: ["(・_・?)", "(⊙_☉)", "(・・;)ゞ"] },
    uncertain: { category: "犹豫", variants: ["(・_・;)", "(￣～￣;)", "(・・;)ゞ"] },
    bored: { category: "无聊", variants: ["(￣ー￣)", "(－_－)", "( ´△｀)"] },
    detached: { category: "淡漠", variants: ["(￣ー￣)", "(－_－)", "(￢_￢)"] },
    calm: { category: "安定", variants: ["( ˘͈ ᵕ ˘͈ )", "( ˘ω˘ )", "( ´ ▽ ` )"] },
    focused: { category: "专注", variants: ["( •̀_•́ )", "(｀・ω・´)", "(ง •̀_•́)ง"] },
    determined: { category: "坚定", variants: ["(ง •̀_•́)ง", "(๑•̀ㅂ•́)و✧", "( •̀ᄇ•́)ﻭ✧"] },
    embarrassed: { category: "尴尬", variants: ["(￣▽￣;)", "(・・;)ゞ", "(〃￣ω￣〃ゞ"] },
    tired: { category: "疲惫", variants: ["(￣o￣) . z Z", "(－_－) zzZ", "( ´△｀)"] }
  };
  const categoryGroups = {
    "愉快与期待": ["happy", "amused", "excited", "content", "relieved", "hopeful", "proud"],
    "亲近与感激": ["affectionate", "grateful", "tender", "shy"],
    "平和与专注": ["neutral", "calm", "focused", "determined"],
    "好奇与意外": ["curious", "surprised", "confused", "uncertain", "embarrassed"],
    "低落与失落": ["sad", "lonely", "disappointed", "hurt", "guilty", "ashamed"],
    "担忧与压力": ["anxious", "fearful", "worried", "stressed", "overwhelmed"],
    "冲突与不悦": ["angry", "frustrated", "irritated", "resentful", "jealous", "suspicious"],
    "疲惫与淡漠": ["bored", "tired", "detached"]
  };
  // Kaomoji the user can insert into a draft, grouped by feeling; no analysis catalog is involved.
  function panelItems() {
    return Object.entries(categoryGroups).map(([category, ids]) => ({
      category, items: ids.filter(id => bank[id]).map(id => ({ label: bank[id].category, variants: bank[id].variants })),
    })).filter(group => group.items.length);
  }
  window.Kaomoji = Object.freeze({ panelItems });
})();
