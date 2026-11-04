"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Shared 性格分析 / 三观分析 charts for 自我剖析 and 知己分析 (added 2026-10-25).
// The catalog mirrors plugin-sdk/python/kyf_plugin/persona.py, except that the Big Five and attachment are named
// as the charts show them (能量 for 外向性, 焦虑 for 依恋焦虑). Positions run from -1 (first end) to 1 (second end)
// and come from the plugin; this file only draws them. No storage, no requests.
(function (root) {
  const DIMENSIONS = {
    big5_o: ["心智", "低", "高"], big5_c: ["应对方式", "低", "高"], big5_e: ["能量", "低", "高"],
    big5_a: ["天性", "低", "高"], big5_n: ["身份特征", "低", "高"],
    att_anx: ["焦虑", "低", "高"], att_avo: ["回避", "低", "高"],
    pol_axis: ["政治倾向", "进步", "保守"], pol_ext: ["立场强度", "温和", "极端"],
    pol_econ: ["经济立场", "左派", "右派"], pol_lib: ["自由—威权", "自由", "威权"],
    val_change: ["求变—守成", "求变", "守成"], val_self: ["利己—利他", "利己", "利他"],
    moral_care: ["关爱", "轻视", "重视"], moral_fair: ["公平", "轻视", "重视"], moral_loyal: ["忠诚", "轻视", "重视"],
    moral_auth: ["权威", "轻视", "重视"], moral_sanct: ["圣洁", "轻视", "重视"], moral_liberty: ["自由", "轻视", "重视"],
    // 三观时间线 scores the pooled 道德基础 axis directly, so it has a key of its own.
    moral_bind: ["道德基础", "个人", "集体"],
    // 知己分析 extension (2026-10-27): the friend-only two-ended dimensions. The type dimensions
    // (humour, conflict) carry a fixed vocabulary instead of poles and live in TYPE_VOCAB.
    cog_need: ["认知需求", "低", "高"], cog_close: ["认知闭合", "低", "高"], locus: ["控制点", "内控", "外控"],
    optimism: ["气质性乐观", "悲观", "乐观"], time_focus: ["时间焦点", "当下", "未来"], time_past: ["过去态度", "消极", "积极"],
    self_constr: ["自我建构", "独立", "互依"], dialect: ["辩证思维", "低", "高"], direct: ["沟通直接性", "高语境", "低语境"],
    self_monitor: ["自我监控", "低", "高"], reject_sens: ["拒绝敏感性", "低", "高"], moral_scope: ["道德关怀范围", "窄", "宽"],
    val_enh: ["人生目标", "自我提升", "自我超越"],
  };
  const TYPE_VOCAB = { humor: ["亲和型", "自强型", "攻击型", "自嘲型"], conflict: ["竞争", "合作", "妥协", "回避", "迁就"] };
  // The diverging humour bars grow from the midpoint: the adaptive styles to the right, the others to the left.
  const DIVERGE_RIGHT = new Set(["亲和型", "自强型"]);
  // The Big Five drawn in MBTI order (16Personalities' five scales, McCrae & Costa 1989): each scale names its
  // letters at both ends and the share toward the nearer end. sign -1: a high Big Five score is the left letter.
  const MBTI = [
    { key: "big5_e", big5: "外向性", left: ["E", "外向"], right: ["I", "内向"], sign: -1 },
    { key: "big5_o", big5: "开放性", left: ["N", "直觉"], right: ["S", "实感"], sign: -1 },
    { key: "big5_a", big5: "宜人性", left: ["T", "理性"], right: ["F", "情感"], sign: 1 },
    { key: "big5_c", big5: "尽责性", left: ["J", "计划"], right: ["P", "随性"], sign: -1 },
    { key: "big5_n", big5: "情绪性", left: ["A", "自信"], right: ["T", "易波动"], sign: 1 },
  ];
  const CARDS = [
    { id: "big5", tab: "personality", title: "MBTI", kind: "mbti", dims: MBTI.map(scale => scale.key) },
    // Bartholomew's four styles on the anxiety × avoidance plane, drawn with low avoidance on top so 安全型 sits top-left.
    // ends: left, right, top, bottom; quadrants: top-left, top-right, bottom-left, bottom-right.
    // `extra` rows are friend-only additions drawn beneath the chart (here 拒绝敏感性).
    { id: "attachment", tab: "personality", title: "依恋类型", kind: "plane", dims: ["att_anx", "att_avo"], flipY: true,
      ends: ["低焦虑", "高焦虑", "低回避", "高回避"], quadrants: ["安全型", "焦虑型", "回避型", "恐惧型"], tinted: true,
      extra: ["reject_sens"] },
    // The politics card draws the classic compass (经济左—右 × 自由—威权); the cultural stance and the
    // extremity stay extracted and are listed in the card's detail rows. Region names are tendencies,
    // not verdicts: a point near a corner merely leans toward that side's values. The plane draws the
    // second dimension's high end at the top (no flipY), so the top end is 威权 and the quadrants follow
    // the compass's own corners (fixed 2026-10-29: the labels had been swapped against the coordinates,
    // drawing a libertarian-leaning point onto the 权威 end).
    { id: "politics", tab: "values", title: "政治光谱", kind: "plane", dims: ["pol_econ", "pol_lib", "pol_axis", "pol_ext"],
      ends: ["左派", "右派", "威权", "自由"], tinted: true, center: "中间派",
      quadrants: ["社会主义", "保守主义", "社会民主主义", "保守自由主义"] },
    { id: "values", tab: "values", title: "价值观", kind: "plane", dims: ["val_change", "val_self"], tinted: true,
      quadrants: ["理想派", "守护派", "进取派", "务实派"], extra: ["val_enh"] },
    // One pooled axis instead of six bars: 个人 (关爱、公平、自由) vs 集体 (忠诚、权威、圣洁); the six
    // foundations stay listed in the card's detail with their own evidence. side +1: the high end is 集体.
    // The friend-only 道德关怀范围 (对谁适用, beside the 理由 axes) joins as a second row.
    { id: "moral", tab: "values", title: "道德基础", kind: "axis", friend: true, ends: ["个人", "集体"], whole: "moral_bind",
      dims: ["moral_care", "moral_fair", "moral_liberty", "moral_loyal", "moral_auth", "moral_sanct"],
      pooled: { moral_care: -1, moral_fair: -1, moral_liberty: -1, moral_loyal: 1, moral_auth: 1, moral_sanct: 1 },
      extra: ["moral_scope"] },
    // 知己分析 extension (2026-10-27). `friend: true` keeps these cards out of 自我剖析, which still
    // uses the original catalog. Slider rows reuse the MBTI row style without the letter columns.
    { id: "mind", tab: "personality", title: "认知与动机", kind: "rows", friend: true,
      dims: ["cog_need", "cog_close", "locus", "optimism"] },
    { id: "expression", tab: "personality", title: "表达与思维", kind: "rows", friend: true,
      dims: ["self_constr", "dialect", "direct", "self_monitor"] },
    { id: "time", tab: "personality", title: "时间观", kind: "rows", friend: true, dims: ["time_focus", "time_past"] },
    { id: "humor", tab: "personality", title: "幽默风格", kind: "diverge", friend: true, dims: ["humor"] },
    { id: "conflict", tab: "personality", title: "冲突处理", kind: "heat", friend: true, dims: ["conflict"] },
  ];
  // Where an explicit self-report sits on its plane: [x, y] in the dimensions' own direction.
  const ATTACHMENT_SELF = { "安全型": [-0.5, -0.5], "焦虑型": [0.5, -0.5], "痴迷型": [0.5, -0.5], "回避型": [-0.5, 0.5],
    "疏离型": [-0.5, 0.5], "恐惧型": [0.5, 0.5] };
  const POLITICS_SELF = { "进步": [-0.5, 0], "中间": [0, 0], "保守": [0.5, 0] };
  const STRENGTH = { "较有把握": "firm", "有一定依据": "some", "把握较低": "weak" };
  const ORDER = ["firm", "some", "weak"];

  function el(tag, className, text) {
    const node = root.document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function add(parent, ...children) {
    for (const child of children) parent.appendChild ? parent.appendChild(child) : parent.append(child);
    return parent;
  }
  function place(node, x, y) {
    if (!node.style) return;
    if (x !== null) node.style.left = (x + 1) * 50 + "%";
    if (y !== null) node.style.top = (1 - y) * 50 + "%";
  }
  const known = item => item && typeof item.position === "number" && STRENGTH[item.confidence];
  const label = key => DIMENSIONS[key][0];

  // -1 (left letter) … 1 (right letter), or null without evidence.
  const lean = (scale, dims) => known(dims[scale.key]) ? scale.sign * dims[scale.key].position : null;
  const letter = (scale, dims) => { const x = lean(scale, dims); return x ? (x < 0 ? scale.left : scale.right)[0] : "?"; };
  // "93% 外向": the share toward the nearer letter, as the scale and its detail row both show it.
  function share(scale, dims) {
    const x = lean(scale, dims);
    if (x === null) return null;
    return Math.round(50 + Math.abs(x) * 50) + "%" + (x ? " " + (x < 0 ? scale.left : scale.right)[1] : "");
  }
  function mbti(dims, self) {
    if (self) return { text: self, title: "本人自述" };
    const text = MBTI.slice(0, 4).map(scale => letter(scale, dims)).join("");
    // A partly known scale is not a type: "?N??" must not stand in for the self-report.
    if (text.includes("?")) return null;
    const identity = letter(MBTI[4], dims);
    return { text: identity === "?" ? text : text + "-" + identity, title: "由大五人格推算（McCrae & Costa 1989）" };
  }

  function dot(item, key) {
    const node = el("i", "kpc-dot " + STRENGTH[item.confidence]);
    node.setAttribute("title", `${label(key)} · ${item.confidence}`);
    return node;
  }
  function scales(dims) {
    const list = el("div", "kpc-rows kpc-mbti");
    for (const scale of MBTI) {
      const item = dims[scale.key], x = lean(scale, dims), row = el("div", "kpc-row");
      const track = el("span", x === null ? "kpc-track unknown" : "kpc-track");
      const side = x === null || !x ? null : x < 0 ? scale.left : scale.right;
      if (x !== null) {
        const mark = dot(item, scale.key);
        place(mark, x, null);
        add(track, mark);
      }
      const name = el("span", "kpc-label", label(scale.key));
      name.setAttribute("title", "大五人格 · " + scale.big5);
      add(list, add(row, name, el("span", side === scale.left ? "kpc-letter on" : "kpc-letter", scale.left[0]), track,
        el("span", side === scale.right ? "kpc-letter on" : "kpc-letter", scale.right[0]),
        el("span", x === null ? "kpc-value unknown" : "kpc-value", share(scale, dims) || "证据不足")));
    }
    return list;
  }
  // One pooled -1 (left end) … 1 (right end) axis from several dimensions' lean counts; each key's
  // `pooled` side (+1/-1) says whether its high end counts toward the right end. Confidence is the
  // weakest of the contributing dimensions, like a plane point showing the weaker of its two axes.
  // A score for the axis as a whole (`whole`) stands in for the pooled counts.
  function axis(card, dims) {
    const [left, right] = card.ends, row = el("div", "kpc-rows kpc-axis"), whole = dims[card.whole];
    let toward = 0, total = 0, strength = -1;
    for (const [key, side] of Object.entries(card.pooled)) {
      const item = dims[key];
      if (!known(item)) continue;
      strength = Math.max(strength, ORDER.indexOf(STRENGTH[item.confidence]));
      const counts = item.counts || {};
      toward += side * ((counts.high || 0) - (counts.low || 0));
      total += (counts.low || 0) + (counts.high || 0) + (counts.mixed || 0);
    }
    if (known(whole)) strength = ORDER.indexOf(STRENGTH[whole.confidence]);
    const x = known(whole) ? whole.position : total ? toward / (total + 2) : null;
    const track = el("span", x !== null ? "kpc-track" : "kpc-track unknown");
    if (x !== null) {
      const mark = el("i", "kpc-dot " + ORDER[strength]);
      mark.setAttribute("title", `${card.title} · ${x < 0 ? left : right}侧 · ${ORDER[strength]}；` +
        `${left}＝关爱、公平、自由；${right}＝忠诚、权威、圣洁`);
      place(mark, x, null);
      add(track, mark);
    }
    const text = x !== null ? Math.round(50 + Math.abs(x) * 50) + "% " + (x < 0 ? left : right) : null;
    return add(row, add(el("div", "kpc-row"), el("span", "kpc-label", left), track, el("span", "kpc-label", right),
      el("span", text ? "kpc-value" : "kpc-value unknown", text || "证据不足")));
  }
  // The extended friend dimensions reuse the MBTI row style without the letter columns: name, track, share.
  const SLIDERS = new Set(["cog_need", "cog_close", "locus", "optimism", "time_focus", "time_past",
    "self_constr", "dialect", "direct", "self_monitor", "reject_sens", "moral_scope", "val_enh"]);
  function poleShare(key, item) {
    const x = item && typeof item.position === "number" ? item.position : null;
    if (x === null) return null;
    return Math.round(50 + Math.abs(x) * 50) + "% " + (x < 0 ? DIMENSIONS[key][1] : DIMENSIONS[key][2]);
  }
  function slider(item, key) {
    const x = known(item) ? item.position : null, row = el("div", "kpc-row kpc-slide-row");
    const track = el("span", x === null ? "kpc-track unknown" : "kpc-track");
    if (x !== null) {
      const mark = dot(item, key);
      place(mark, x, null);
      add(track, mark);
    }
    const name = el("span", "kpc-label", label(key));
    name.setAttribute("title", `${label(key)}（${DIMENSIONS[key][1]}—${DIMENSIONS[key][2]}）`);
    return add(row, name, track, el("span", x === null ? "kpc-value unknown" : "kpc-value", poleShare(key, item) || "证据不足"));
  }
  function rowsBody(card, dims) {
    const body = el("div", "kpc-rows kpc-slide");
    for (const key of card.dims) body.append(slider(dims[key], key));
    return body;
  }
  // Humour styles as four bars growing from the midpoint: adaptive styles right, the others left.
  function diverge(card, dims) {
    const counts = (dims[card.dims[0]] || {}).types || {};
    const total = TYPE_VOCAB.humor.reduce((sum, type) => sum + (counts[type] || 0), 0);
    const body = el("div", "kpc-rows kpc-diverge");
    for (const type of TYPE_VOCAB.humor) {
      const count = counts[type] || 0, row = el("div", "kpc-row");
      const track = el("span", total ? "kpc-track kpc-diverge-track" : "kpc-track unknown");
      if (count) {
        const bar = el("i", "kpc-bar");
        bar.style[DIVERGE_RIGHT.has(type) ? "left" : "right"] = "50%";
        bar.style.width = count / total * 50 + "%";
        bar.setAttribute("title", `${type} · ${count} 条 · ${Math.round(count / total * 100)}%`);
        track.append(bar);
      }
      add(body, add(row, el("span", "kpc-label", type), track,
        el("span", total ? "kpc-value" : "kpc-value unknown", total ? Math.round(count / total * 100) + "%" : "证据不足")));
    }
    return body;
  }
  // Conflict handling as a five-cell heat strip; without any recorded conflict the card stays dashed.
  function heat(card, dims) {
    const counts = (dims[card.dims[0]] || {}).types || {};
    const total = TYPE_VOCAB.conflict.reduce((sum, type) => sum + (counts[type] || 0), 0);
    const box = el("div", "kpc-heat");
    if (!total) {
      box.className += " empty";
      box.append(el("span", "kpc-heat-empty", "暂无冲突情境"));
      return box;
    }
    const top = Math.max(...TYPE_VOCAB.conflict.map(type => counts[type] || 0));
    for (const type of TYPE_VOCAB.conflict) {
      const count = counts[type] || 0, cell = el("span", "kpc-heat-cell" + (count ? "" : " none") + (count === top ? " on" : ""));
      if (count) cell.style.flexGrow = String(count);
      cell.setAttribute("title", `${type} · ${count} 条 · ${Math.round(count / total * 100)}%`);
      cell.textContent = type;
      box.append(cell);
    }
    return box;
  }
  // The distribution line of a type dimension ("亲和型 5 · 自嘲型 2"), or null for pole dimensions.
  function typeText(key, item) {
    const vocab = TYPE_VOCAB[key];
    if (!vocab || !item) return null;
    const counts = item.types || {};
    const parts = vocab.filter(type => counts[type]).map(type => `${type} ${counts[type]}`);
    return parts.length ? parts.join(" · ") : null;
  }
  function cardSources(card, sources) {
    const seen = new Set(), list = [];
    for (const key of [...card.dims, ...(card.extra || [])])
      for (const cite of sources[key] || [])
        if (!seen.has(cite)) { seen.add(cite); list.push(cite); }
    return list;
  }
  function plane(card, dims, self) {
    const [xKey, yKey] = card.dims, box = el("div", card.tinted ? "kpc-plane tinted" : "kpc-plane");
    // flipY draws the second dimension's high end at the bottom.
    const at = (node, px, py) => place(node, px, card.flipY ? -py : py);
    if (card.quadrants) for (const [index, name] of card.quadrants.entries())
      add(box, el("span", "kpc-quadrant q" + index, name));
    if (card.center) add(box, el("span", "kpc-plane-center", card.center));
    const [left, right, top, bottom] = card.ends || [DIMENSIONS[xKey][1], DIMENSIONS[xKey][2], DIMENSIONS[yKey][2], DIMENSIONS[yKey][1]];
    const frame = add(el("div", "kpc-planebox"), el("span", "kpc-end top", top), el("span", "kpc-end left", left), box,
      el("span", "kpc-end right", right), el("span", "kpc-end bottom", bottom));
    const x = dims[xKey], y = dims[yKey];
    if (known(x) && known(y)) {
      const strength = ORDER[Math.max(...[x, y].map(item => ORDER.indexOf(STRENGTH[item.confidence])))];
      const mark = el("i", "kpc-dot " + strength);
      mark.setAttribute("title", [xKey, yKey].map(key => `${label(key)} · ${known(dims[key]) ? dims[key].confidence : "证据不足"}`).join("；"));
      at(mark, x.position, y.position);
      add(box, mark);
    } else {
      box.className += " unknown";
      const available = known(x) ? xKey : known(y) ? yKey : null;
      const missing = available === xKey ? yKey : xKey;
      add(frame, el("span", "kpc-plane-status", available
        ? `仅${label(available)}轴有依据；${label(missing)}轴证据不足`
        : "两轴证据不足，暂无二维结论"));
    }
    if (self) {
      const mark = el("i", "kpc-self");
      mark.setAttribute("title", "本人自述：" + self.value);
      at(mark, self.at[0], self.at[1]);
      add(box, mark);
    }
    return frame;
  }
  function selfMark(card, reports) {
    if (card.id === "attachment" && reports.attachment)
      return { value: reports.attachment, at: ATTACHMENT_SELF[reports.attachment] || [0.5, 0.5] };
    if (card.id === "politics" && POLITICS_SELF[reports.politics])
      return { value: reports.politics, at: POLITICS_SELF[reports.politics] };
    return null;
  }

  // data: { tab, dims: { key: { position, confidence } }, selfReports: { mbti, attachment, politics }, selected,
  //   friend (draws the 知己-only cards and extra rows), sources: { key: [citation] } for the title hover,
  //   type: { text, title } when the caller already knows the letters the MBTI card should name }
  function render(container, data, onSelect) {
    const dims = data.dims || {}, reports = data.selfReports || {}, grid = el("div", "kpc-grid");
    const friend = !!data.friend;
    for (const card of CARDS.filter(item => item.tab === data.tab && (!item.friend || friend))) {
      const node = el("button", "kpc-card");
      node.type = "button";
      node.setAttribute("aria-pressed", String(data.selected === card.id));
      node.addEventListener("click", () => onSelect(card.id));
      const title = el("span", "kpc-title", card.title);
      if (data.sources) {
        const cites = cardSources(card, data.sources);
        if (cites.length) title.setAttribute("title", cites.join("；"));
      }
      const head = add(el("span", "kpc-head"), title);
      if (card.id === "big5") {
        // The type follows the scales drawn below it; a self-report differing from them is listed in the details.
        const type = data.type || mbti(dims, null) || mbti(dims, reports.mbti);
        if (type) {
          const aside = el("span", "kpc-aside", type.text);
          aside.setAttribute("title", type.title);
          add(head, aside);
        }
      }
      if ((card.kind === "diverge" || card.kind === "heat") && dims[card.dims[0]]?.label) {
        // The dominant type is decided by the local rule; the distribution itself is always drawn.
        const aside = el("span", "kpc-aside", dims[card.dims[0]].label);
        aside.setAttribute("title", dims[card.dims[0]].confidence || "");
        add(head, aside);
      }
      let body;
      if (card.kind === "mbti") body = scales(dims);
      else if (card.kind === "rows") body = rowsBody(card, dims);
      else if (card.kind === "diverge") body = diverge(card, dims);
      else if (card.kind === "heat") body = heat(card, dims);
      else if (card.kind === "axis") {
        body = axis(card, dims);
        if (friend) for (const key of card.extra || []) body.append(slider(dims[key], key));
      } else {
        const extras = friend ? card.extra || [] : [];
        if (extras.length) {
          body = el("div", "kpc-rows kpc-slide");
          add(body, plane(card, dims, selfMark(card, reports)));
          for (const key of extras) body.append(slider(dims[key], key));
        } else body = plane(card, dims, selfMark(card, reports));
      }
      add(node, head, body);
      add(grid, node);
    }
    container.replaceChildren(grid);
  }

  // 性格时间线 (added 2026-11-01): five small plots on one time axis, each scored 1–5 around a midline of 3.
  // The line is the score and the band its lowest to highest repeat; a period without a score breaks both.
  // Each scale keeps one of the eight tones in persona-charts.css (2026-11-01).
  const TONES = 8;
  const SVG_NS = "http://www.w3.org/2000/svg";
  function shape(tag, attributes) {
    const node = root.document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    node.setAttribute("vector-effect", "non-scaling-stroke");
    return node;
  }
  const plotX = (index, count) => (index + .5) / count * 100;
  const plotY = value => (5 - Math.min(5, Math.max(1, value))) / 4 * 100;
  function stretches(periods, ready) {
    const groups = [];
    let group = null;
    periods.forEach((period, index) => {
      if (!ready(period)) { group = null; return; }
      if (!group) groups.push(group = []);
      group.push(index);
    });
    return groups;
  }
  const score = (period, key) => period.factors?.[key] || {};
  const scoreText = value => typeof value.score === "number" ? value.score.toFixed(1) : "—";
  const rangeText = value => typeof value.low === "number" ? `${value.low.toFixed(1)}–${value.high.toFixed(1)}` : "";
  function periodPlot(periods, key, selected, tone) {
    const count = periods.length, plot = el("div", `kpc-p-plot kpc-tone-${tone}`);
    const chart = shape("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" });
    chart.append(shape("line", { class: "kpc-p-mid", x1: 0, x2: 100, y1: 50, y2: 50 }));
    for (const group of stretches(periods, period => typeof score(period, key).low === "number")) {
      if (group.length < 2) {
        // A lone period has no neighbour to form a band with, so its range is a short bar.
        const x = plotX(group[0], count), value = score(periods[group[0]], key);
        chart.append(shape("line", { class: "kpc-p-whisker", x1: x, x2: x, y1: plotY(value.high), y2: plotY(value.low) }));
        continue;
      }
      const top = group.map(index => `${plotX(index, count)},${plotY(score(periods[index], key).high)}`);
      const bottom = group.map(index => `${plotX(index, count)},${plotY(score(periods[index], key).low)}`).reverse();
      chart.append(shape("polygon", { class: "kpc-p-band", points: [...top, ...bottom].join(" ") }));
    }
    for (const group of stretches(periods, period => typeof score(period, key).score === "number")) {
      if (group.length > 1) chart.append(shape("polyline", { class: "kpc-p-line",
        points: group.map(index => `${plotX(index, count)},${plotY(score(periods[index], key).score)}`).join(" ") }));
    }
    plot.append(chart);
    periods.forEach((period, index) => {
      const value = score(period, key);
      if (typeof value.score !== "number") return;
      const mark = el("i", period.id === selected ? "kpc-p-dot on" : "kpc-p-dot");
      mark.style.left = plotX(index, count) + "%";
      mark.style.top = plotY(value.score) + "%";
      plot.append(mark);
    });
    return plot;
  }
  // Equal values share a tone across strips, spaced or not, so the same name keeps its color everywhere.
  // Past the eighth value a segment stays neutral rather than repeat a tone; its text still names it.
  function stripTone(tones, value) {
    const key = value.replace(/\s/g, "");
    if (!tones.seen.has(key)) tones.seen.set(key, tones.seen.size);
    const order = tones.seen.get(key);
    return order < TONES ? ` kpc-toned kpc-tone-${(tones.first + order) % TONES}` : "";
  }
  // Runs of equal text become one segment, so a long stretch of one type reads as one bar. A text
  // that is an array of names — several partners of one period — renders one chip per name, stacked;
  // each chip takes its own tone, so the same person keeps their color in every period. With
  // edit: { edited(period), ai(period), open(periodId) } every period opens, blank ones too, and
  // a click inside a run opens the period under the pointer. What the user changed never merges with the model's.
  function periodStrip(periods, text, name = "kpc-p-strip", tones = null, edit = null) {
    const strip = el("div", name), count = periods.length;
    strip.style.gridTemplateColumns = `repeat(${count}, minmax(0, 1fr))`;
    const mine = period => Boolean(edit?.edited(period)), key = value => Array.isArray(value) ? value.filter(Boolean).join("\n") : value;
    let stacked = false, start = 0;
    while (start < count) {
      const raw = text(periods[start]), value = key(raw), edited = mine(periods[start]), first = start;
      let end = start + 1;
      while (end < count && key(text(periods[end])) === value && mine(periods[end]) === edited) end++;
      if (value || edit) {
        const span = end - first, names = Array.isArray(raw) ? raw.filter(Boolean) : null;
        const classes = "kpc-p-seg" + (value ? "" : " empty") + (names?.length ? " stack" : value && tones ? stripTone(tones, value) : "") + (edited ? " edited" : "");
        const segment = el(edit ? "button" : "span", classes, names?.length ? "" : value), range = `${periods[first].label}${span > 1 ? " 至 " + periods[end - 1].label : ""}`;
        segment.style.gridColumn = `${first + 1} / ${end + 1}`;
        const record = edited ? `\n你改过，AI 判断：${edit.ai(periods[first]) || "无"}` : "";
        segment.setAttribute("title", `${range}：${names?.length ? names.join("、") : value || "无"}${record}`);
        if (names?.length) {
          stacked = true;
          for (const item of names) segment.append(el("span", `kpc-p-chip${tones ? stripTone(tones, item) : ""}`, item));
        }
        if (edit) {
          segment.type = "button";
          segment.addEventListener("click", event => {
            const offset = Math.floor(event.offsetX / segment.clientWidth * span) || 0;
            edit.open(periods[first + Math.min(span - 1, Math.max(0, offset))].id);
          });
        }
        strip.append(segment);
      }
      start = end;
    }
    if (stacked) strip.className += " stacked";
    return strip;
  }
  function periodTip(period, scales) {
    const rows = scales.map(scale => {
      const value = score(period, scale.key), range = rangeText(value);
      return `${label(scale.key)} ${scoreText(value)}${range ? `（${range}）` : ""}`;
    });
    return [period.label, ...rows, (period.letters || []).join("")].join("\n");
  }
  // The reading note opens from a ? in the corner, stays open through a redraw and closes on any click outside it.
  let helpWatched = false;
  function closeHelp(keep) {
    for (const node of root.document.querySelectorAll(".kpc-help[open]")) if (node !== keep) node.open = false;
  }
  function periodHelp(lines, title, open) {
    if (!helpWatched) {
      helpWatched = true;
      root.addEventListener("click", event => closeHelp(event.target.closest?.(".kpc-help")));
      root.addEventListener("keydown", event => { if (event.key === "Escape") closeHelp(null); });
    }
    const help = el("details", "kpc-help"), mark = el("summary", "", "?"), note = el("div", "kpc-help-note");
    mark.setAttribute("aria-label", `怎么读${title}`);
    mark.setAttribute("title", `怎么读${title}`);
    for (const line of lines) note.append(el("p", "", line));
    help.open = open;
    return add(help, mark, note);
  }
  // data: { periods: [{ id, label, factors, letters, report, ai, edited }], selected, detail, help: [line] }; a timeline of
  // other scales adds title, instrument, scales: [{ key, tone }] and strips: [[name, period => text, field, edited?]].
  // handlers: onSelect(periodId), onDetail(dimensionKey), onEdit(field, periodId). The caller owns the selection.
  function periods(container, data, handlers) {
    const list = data.periods || [], count = list.length, box = el("section", "kpc-periods"), scales = data.scales || MBTI;
    const passes = Math.max(...list.map(period => period.passes || 1)), items = Math.max(...list.map(period => period.items || 0));
    // The self-reported 你说过 row went away on 2026-11-01: the user corrects values in place, and the
    // self-report still shows on the MBTI card. A timeline that wants more rows passes its own strips.
    const strips = data.strips || [["读聊天", period => (period.letters || []).join(""), "type"]];
    const edited = (period, field) => (period.edited || []).includes(field);
    const title = data.title || "性格时间线", tone = (scale, row) => scale.tone ?? row;
    const head = add(el("div", "kpc-head"), el("span", "kpc-title", title),
      el("span", "kpc-aside", `${data.instrument || (items > 20 ? "IPIP-50" : "Mini-IPIP")} · 每期 ${passes} 次调研`));
    if (data.help) add(head, periodHelp(data.help, title, Boolean(container.querySelector(".kpc-help[open]"))));
    const body = el("div", "kpc-p-body"), chosen = list.find(period => period.id === data.selected) || list[count - 1];
    scales.forEach((scale, row) => {
      const name = el("button", `kpc-p-name kpc-toned kpc-tone-${tone(scale, row)}`, label(scale.key));
      name.type = "button";
      name.setAttribute("title", scale.big5 ? `大五人格 · ${scale.big5}（查看每道题）` : "查看每道题");
      name.setAttribute("aria-pressed", String(data.detail === scale.key));
      name.addEventListener("click", () => handlers.onDetail(scale.key));
      const value = score(chosen, scale.key), field = "score:" + scale.key, mine = edited(chosen, field), range = rangeText(value);
      const shown = el("button", (typeof value.score === "number" ? "kpc-p-value" : "kpc-p-value unknown") + (mine ? " edited" : ""), scoreText(value));
      shown.type = "button";
      shown.setAttribute("title", [range ? `${passes} 次调研 ${range}` : "", mine ? `你改过，AI 判断：${scoreText({ score: chosen.ai?.[field] })}` : "修改这个分数"].filter(Boolean).join("\n"));
      shown.addEventListener("click", () => handlers.onEdit(field, chosen.id));
      const plot = periodPlot(list, scale.key, chosen.id, tone(scale, row));
      plot.style.gridRow = String(row + 1);
      add(body, name, plot, shown);
    });
    const columns = el("div", "kpc-p-cols");
    columns.style.gridTemplateColumns = `repeat(${count}, minmax(0, 1fr))`;
    columns.style.gridRow = `1 / span ${scales.length}`;
    for (const period of list) {
      const column = el("button", period.id === chosen.id ? "kpc-p-col on" : "kpc-p-col");
      column.type = "button";
      column.setAttribute("title", periodTip(period, scales));
      column.setAttribute("aria-label", period.label);
      column.addEventListener("click", () => handlers.onSelect(period.id));
      columns.append(column);
    }
    const years = periodStrip(list, period => period.id.slice(0, period.id.includes("-") ? 9 : 4), count > 12 ? "kpc-p-strip kpc-p-years long" : "kpc-p-strip kpc-p-years");
    add(body, columns, el("span", ""), years, el("span", ""));
    // Strip values start on the tones the scales left free, so a value does not read as one of the scales.
    const tones = { seen: new Map(), first: Math.max(...scales.map(tone)) + 1 };
    for (const [name, text, field, mine] of strips) add(body, el("span", "kpc-p-name plain", name), periodStrip(list, text, undefined, tones,
      { edited: mine || (period => edited(period, field)), ai: period => period.ai?.[field], open: id => handlers.onEdit(field, id) }), el("span", ""));
    container.replaceChildren(add(box, head, body));
  }

  root.KyfPersonaCharts = {
    DIMENSIONS, TYPE_VOCAB, CARDS, MBTI, render, mbti, typeText, poleShare, periods,
    cards: tab => CARDS.filter(card => card.tab === tab),
    cardOf: key => (CARDS.find(card => card.dims.includes(key) || (card.extra || []).includes(key)) || {}).id || null,
    tabOf: key => (CARDS.find(card => card.dims.includes(key) || (card.extra || []).includes(key)) || {}).tab || null,
    label, ends: key => DIMENSIONS[key].slice(1),
    share: (key, item) => {
      const scale = MBTI.find(entry => entry.key === key);
      if (scale) return share(scale, { [key]: item });
      return SLIDERS.has(key) ? poleShare(key, item) : null;
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
