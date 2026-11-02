"""Shared 性格分析 / 三观分析 dimensions for 自我剖析 and 知己分析 (added 2026-10-25).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Each dimension is two-ended; a piece of chat evidence leans to one end (or both). The
chart position is computed from those leans, never asked from the model as a score.
Sources: Big Five (Goldberg 1990; Peters, Cerf & Matz 2024 OSF preprint
10.31219/osf.io/apc5g for text inference), adult attachment anxiety × avoidance
(Fraley, Waller & Brennan 2000; Bartholomew & Horowitz 1991), progressive—conservative
(Pan & Xu 2018) and ideological extremity (van Prooijen & Krouwel 2019), the political
compass's economic left—right and libertarian—authoritarian axes, Schwartz's
higher-order values (1992), moral foundations (Graham et al. 2013; Atari et al. 2023).
``plugin-sdk/web/persona-charts.js`` mirrors this catalog.

``EXTENDED_DIMENSIONS`` and ``TYPE_DIMENSIONS`` (added 2026-10-27) are the 知己分析
extension: cognition and motivation, expression and thinking, rejection sensitivity,
the scope of moral concern, time perspective and the type dimensions (humour styles,
conflict handling). They stay out of ``DIMENSIONS`` until the user decides whether
自我剖析 adopts them too, so the shared catalog and its prompts do not change under
the self-analysis workflow. ``SOURCES`` keeps one APA-style citation list per key.
"""

# key, label, low end, high end, what a piece of chat evidence must show
DIMENSIONS = (
    ("big5_o", "开放性", "低", "高", "对新想法、新体验、抽象或艺术话题的好奇（高），或偏好熟悉、具体、常规（低）"),
    ("big5_c", "尽责性", "低", "高", "守时、有计划、按约完成、有条理（高），或随性、拖延、临时变动（低）"),
    ("big5_e", "外向性", "低", "高", "主动发起聊天和聚会、热情、从社交中得到能量（高），或偏好独处、少发起、安静（低）"),
    ("big5_a", "宜人性", "低", "高", "体谅、合作、温和、信任他人（高），或直率批评、争辩、计较（低）"),
    ("big5_n", "情绪性", "低", "高", "焦虑、担忧、情绪起伏、容易受压力影响（高），或平稳、放松（低）"),
    ("att_anx", "依恋焦虑", "低", "高", "在亲近关系里担心被冷落、需要反复确认、对回应快慢敏感（高），或对关系有安全感（低）"),
    ("att_avo", "依恋回避", "低", "高", "回避亲密和依赖、不愿谈感受、主动拉开距离（高），或自在地亲近和依靠对方（低）"),
    ("pol_axis", "政治倾向", "进步", "保守", "对时事、社会政策等公共议题的立场：支持变革、平等与多元（进步），或维护传统、秩序与现状（保守）；不看个人生活选择"),
    ("pol_ext", "立场强度", "温和", "极端", "谈公共议题时的方式：非黑即白、绝对化、过度自信、贬斥不同意见（极端），或承认复杂、愿意听取异见（温和）"),
    ("pol_econ", "经济立场", "左派", "右派", "对财富与分配的明确观点：重平等、福利、国家投入或约束资本（左派），或重市场、效率、民营与低税（右派）"),
    ("pol_lib", "自由—威权", "自由", "威权", "对管制与强制的态度：支持个人选择、言论自由、多元开放（自由），或支持管制、等级、秩序与强制统一（威权）"),
    ("val_change", "求变—守成", "求变", "守成", "自己怎么生活：换城市、跳槽、尝鲜、说走就走（求变），或求稳、按部就班、不愿挪窝（守成）；不看对公共议题的立场"),
    ("val_self", "利己—利他", "利己", "利他", "看重成就、地位、享乐和个人利益（利己），或关心他人福祉、公平与自然（利他）"),
    ("moral_care", "关爱", "轻视", "重视", "评判对错时诉诸伤害与关怀"),
    ("moral_fair", "公平", "轻视", "重视", "评判对错时诉诸公平、平等、欺骗或按付出分配"),
    ("moral_loyal", "忠诚", "轻视", "重视", "评判对错时诉诸对家人、集体或群体的忠诚与背叛"),
    ("moral_auth", "权威", "轻视", "重视", "评判对错时诉诸尊重和服从长辈、权威与规矩"),
    ("moral_sanct", "圣洁", "轻视", "重视", "评判对错时诉诸纯洁、底线、恶心或神圣"),
    ("moral_liberty", "自由", "轻视", "重视", "评判对错时诉诸个人自由、反对强迫与控制"),
)
KEYS = tuple(key for key, *_rest in DIMENSIONS)
LABELS = {key: label for key, label, *_rest in DIMENSIONS}
POLES = {key: (low, high) for key, _label, low, high, _cue in DIMENSIONS}
GROUPS = {"personality": KEYS[:7], "values": KEYS[7:]}
ATTACHMENT_TYPES = ("安全型", "焦虑型", "回避型", "恐惧回避型", "焦虑回避型", "混乱型")
POLITICAL_SELF = ("进步", "保守", "中间")
# Fewer than this many leaning items keep the point near the middle.
SHRINK = 2


def cue_lines():
    """One prompt line per dimension: key, ends and what counts as evidence."""
    return "".join("- %s %s（%s—%s）：%s\n" % (key, label, low, high, cue) for key, label, low, high, cue in DIMENSIONS)


def compact_cues():
    """The same catalog in one line, for prompts that extract several domains at once."""
    groups = {}
    for key, label, low, high, _cue in DIMENSIONS:
        groups.setdefault((low, high), []).append(key + ("" if low in label else label))
    return "；".join(" ".join(keys) + "(%s/%s)" % ends for ends, keys in groups.items()) + \
        "。pol_ext看谈公共议题是否非黑即白、贬斥异见，moral_*看评判对错诉诸的理由。"


def lean(key, value):
    """``"low"``, ``"high"``, ``"mixed"``, or ``None`` when the value names neither end."""
    low, high = ALL_POLES[key]
    if value == low:
        return "low"
    if value == high:
        return "high"
    if value in ("两者", "两者都有", "混合"):
        return "mixed"
    return None


def position(counts):
    """-1 (low end) … 1 (high end) from lean counts; ``None`` without evidence."""
    low, high, mixed = counts.get("low", 0), counts.get("high", 0), counts.get("mixed", 0)
    total = low + high + mixed
    return round((high - low) / (total + SHRINK), 3) if total else None


def political_self(text):
    """Normalise an explicit political self-placement to 进步 / 保守 / 中间."""
    if any(word in text for word in ("左", "自由派", "自由主义", "进步")):
        return "进步"
    if any(word in text for word in ("右", "保守", "传统派")):
        return "保守"
    if any(word in text for word in ("中间", "中立")):
        return "中间"
    return None


# -- 知己分析 extension (2026-10-27) ----------------------------------------------------------------
# Same two-ended mechanism as DIMENSIONS; the cue states the boundary against the
# existing dimension it is most easily confused with, so one piece of evidence is
# never counted twice. Type dimensions below carry a fixed value vocabulary instead
# of two ends: every matching piece of evidence names one type, and a dominant type
# is only claimed from at least three same-type items spanning different dates.
EXTENDED_DIMENSIONS = (
    ("cog_need", "认知需求", "低", "高", "愿意深究原理、琢磨复杂问题、享受想明白的过程（高），或满足于现成结论、懒得细想（低）"),
    ("cog_close", "认知闭合", "低", "高", "看日常信息处理：急着定论、受不了悬而未决（高），或愿意多等新信息再下结论（低）；公共议题表态看 pol_ext"),
    ("locus", "控制点", "内控", "外控", "把结果归于自己的选择和努力（内控），或归于运气、命、他人和环境（外控）"),
    ("reject_sens", "拒绝敏感性", "低", "高", "对被拒线索的反应：把已读不回、婉拒解读成嫌弃并耿耿于怀（高），或能坦然接受、不多想（低）；依恋焦虑是亲密关系内的担忧，此维看对具体被拒事件的反应"),
    ("self_constr", "自我建构", "独立", "互依", "描述自己时以个人偏好和特点为主（独立），或以关系和集体中的角色为主（互依）"),
    ("dialect", "辩证思维", "低", "高", "接受矛盾并存、从两面看问题、常说“但也”（高），或非此即彼、追求前后一致（低）；谈公共议题的绝对化看 pol_ext"),
    ("direct", "沟通直接性", "高语境", "低语境", "有话直说、把要求和想法讲明白（低语境），或含蓄暗示、靠对方领会（高语境）"),
    ("self_monitor", "自我监控", "低", "高", "按场合明显调整自我呈现、很在意别人怎么看（高），或言行前后一致、少看场合（低）；拿不准就不记"),
    ("moral_scope", "道德关怀范围", "窄", "宽", "道德关心的对象：只顾自己和亲近的人（窄），或延伸到陌生人、外群体、动物与后代（宽）；moral_* 看评判对错诉诸的理由，此维看对谁适用"),
    ("optimism", "气质性乐观", "悲观", "乐观", "对结果的预期：相信事情会顺利、坏事能过去（乐观），或先想到坏结果、预期不顺（悲观）；big5_n 测负性情绪的频率，此维测结果预期"),
    ("time_focus", "时间焦点", "当下", "未来", "安排生活的时间指向：做长期打算、为以后铺垫（未来），或活在当下、临到头再说（当下）"),
    ("time_past", "过去态度", "消极", "积极", "谈起过去的方式：后悔、怨怼、不愿提（消极），或怀念、感恩、坦然（积极）"),
    ("val_enh", "自我提升—自我超越", "自我提升", "自我超越", "人生追求的方向：看重成就、影响力、财富与享受（自我提升），或看重内心成长、意义与对亲近之人的责任（自我超越）；val_self 看人我之间的利害取舍，此维看自己的人生目标"),
)
# key, label, the fixed value vocabulary, what a piece of evidence must show
TYPE_DIMENSIONS = (
    ("humor", "幽默风格", ("亲和型", "自强型", "攻击型", "自嘲型"),
     "对方怎么开玩笑：拉近距离、一起乐（亲和型），用幽默化解自己的处境（自强型），讽刺或贬损他人（攻击型），拿自己开涮、贬低自己（自嘲型）；value 写类型名，note 概括原话的用法"),
    ("conflict", "冲突处理", ("竞争", "合作", "妥协", "回避", "迁就"),
     "真实分歧或冲突中的做法：坚持己见、施加压力（竞争），摆事实、共同解决（合作），各让一步（妥协），岔开或拖着不谈（回避），优先顺从对方（迁就）；只记真实分歧，玩笑和斗嘴不算"),
)
EXTENDED_KEYS = tuple(key for key, *_rest in EXTENDED_DIMENSIONS)
TYPE_KEYS = tuple(key for key, *_rest in TYPE_DIMENSIONS)
TYPES = {key: types for key, _label, types, _cue in TYPE_DIMENSIONS}
EXTENDED_POLES = {key: (low, high) for key, _label, low, high, _cue in EXTENDED_DIMENSIONS}
ALL_POLES = {**POLES, **EXTENDED_POLES}

# One APA short-form citation list per key; the page shows them under each card's
# “理论依据” and the method notes group them by family.
_GOLD = "Goldberg (1990) JPSP, 59, 1216–1229"
_PETERS = "Peters, Cerf & Matz (2024) OSF 预印本, 10.31219/osf.io/apc5g"
_BIG5 = (_GOLD, _PETERS)
_ATT = ("Fraley, Waller & Brennan (2000) JPSP, 78, 350–365",
        "Bartholomew & Horowitz (1991) JPSP, 61, 226–244")
_POL = ("Pan & Xu (2018) Journal of Politics, 80, 254–273",)
_MORAL = ("Graham et al. (2013) Advances in Experimental Social Psychology, 47, 55–130",
          "Graham et al. (2009) JPSP, 96, 1029–1046",
          "Atari et al. (2023) JPSP, 125, 1157–1188")
SOURCES = {
    "big5_o": _BIG5, "big5_c": _BIG5, "big5_e": _BIG5, "big5_a": _BIG5, "big5_n": _BIG5,
    "att_anx": _ATT, "att_avo": _ATT,
    "pol_axis": _POL, "pol_econ": _POL, "pol_lib": _POL,
    "pol_ext": ("van Prooijen & Krouwel (2019) Current Directions in Psychological Science, 28, 159–163",),
    "val_change": ("Schwartz (1992) Advances in Experimental Social Psychology, 25, 1–65",),
    "val_self": ("Schwartz (1992) Advances in Experimental Social Psychology, 25, 1–65",),
    "val_enh": ("Schwartz (1992) Advances in Experimental Social Psychology, 25, 1–65",),
    "moral_care": _MORAL, "moral_fair": _MORAL, "moral_loyal": _MORAL, "moral_auth": _MORAL,
    "moral_sanct": _MORAL, "moral_liberty": _MORAL,
    "self_mbti": ("McCrae & Costa (1989) JPSP, 57, 17–40",),
    "self_attachment": _ATT,
    "self_politics": _POL,
    "cog_need": ("Cacioppo & Petty (1982) JPSP, 42, 116–131",
                 "Cacioppo, Petty & Kao (1984) Journal of Personality Assessment, 48, 306–307"),
    "cog_close": ("Kruglanski & Webster (1996) Psychological Review, 103, 263–283",),
    "locus": ("Rotter (1966) Psychological Monographs, 80, 1–28",),
    "reject_sens": ("Downey & Feldman (1996) JPSP, 70, 1327–1343",),
    "self_constr": ("Markus & Kitayama (1991) Psychological Review, 98, 224–253",),
    "dialect": ("Peng & Nisbett (1999) American Psychologist, 54, 741–754",),
    "direct": ("Hall (1976) Beyond Culture. Anchor Books",),
    "self_monitor": ("Snyder (1974) JPSP, 30, 526–537",),
    "moral_scope": ("Crimston et al. (2016) JPSP, 111, 636–653",),
    "optimism": ("Scheier & Carver (1985) Health Psychology, 4, 219–247",),
    "time_focus": ("Zimbardo & Boyd (1999) JPSP, 77, 1271–1288",),
    "time_past": ("Zimbardo & Boyd (1999) JPSP, 77, 1271–1288",),
    "humor": ("Martin et al. (2003) Journal of Research in Personality, 37, 48–75",),
    "conflict": ("Thomas & Kilmann (1974) Thomas-Kilmann Conflict MODE Instrument. Xicom",
                 "Rahim (1983) Academy of Management Journal, 26, 436–448"),
}
