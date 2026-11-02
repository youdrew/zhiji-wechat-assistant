"""聊天试用: the user writes as a friend and sees how they themselves would reply.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The reply is built from Core's saved self-profile, the 自我剖析 results (self-reports,
personality and values conclusions, the user's own notes and strong tags) and the
account's own retrieved replies, and is only returned to the page: nothing is sent or
stored. The retrieved samples are returned too, so the page can show what the reply
drew on.

The default ``auto`` depth adds one cheap routing call before the reply: it picks the
concrete depth and, for personal questions (plans, feelings, recent changes), selects
timeline entries to research from ``timeline_catalog`` plus retrieval queries. Timeline
lines join the reply's facts; a failed route falls back to standard with no research.
"""
import json
import random
import time

from kyf_plugin import Cancelled

from self_contracts import MAX_QUERY_CHARS, MAX_SOURCES, SelfError, integer
from self_model import Job, json_object
from self_persona import PersonaStore
from self_retrieval import Retrieval
from self_values import ValuesStore

MAX_TURNS = 20
TURN_CHARS = 1000
QUERY_TURNS = 6
EXAMPLE_CHARS = 600
PROFILE_CHARS = 1500
FACT_CHARS = 160
# 本人真实回复 99% 不超过 6 条；没有相近样例时偏向 1 条，贴近本人过半回复只发一条的分布。
MAX_MESSAGES = 6
NO_SAMPLE_COUNTS = (1, 1, 2)
MESSAGE_CHARS = 500
PLAN_QUERIES = 4
PLAN_QUERY_CHARS = 60
# auto 路由只读结论列表的前一段（优先级有序，截前缀即可），调研条目与目录都有硬上限。
ROUTE_FACTS = 2000
MAX_RESEARCH = 12
# 回答检索深度 from the plugin settings. ``auto`` 先路由再定档。``focus`` examples
# answer the friend's last messages; ``plan`` lets the model name what to look up
# before the reply. Thinking replies spend part of the output cap on reasoning
# before the JSON.
REPLY_DEPTHS = ("basic", "standard", "deep")
REPLY_DEPTH_SETTINGS = ("auto",) + REPLY_DEPTHS
DEFAULT_REPLY_DEPTH = "auto"
LEVELS = {
    "basic": {"examples": 4, "focus": 3, "facts": 1500, "tags": 40, "tagChars": 400,
              "plan": False, "thinking": False, "outputTokens": 1024},
    "standard": {"examples": 8, "focus": 6, "facts": 3000, "tags": 80, "tagChars": 800,
                 "plan": False, "thinking": True, "outputTokens": 8192},
    "deep": {"examples": 16, "focus": 6, "facts": 6000, "tags": 160, "tagChars": 1600,
             "plan": True, "thinking": True, "outputTokens": 8192},
}

PLAN = """你帮用户本人准备回复一位微信好友。输入 JSON：conversation 是这次的对话（from=friend 是好友，from=me 是用户本人，me 的早先回复是试用中生成的，不是事实），facts 是已有的本人分析结论。
要回答好友最后发来的消息，还需要从用户本人过去的聊天里查什么？例如本人说过的关于自己的情况、相关经历、对这件事的看法，以及后来的变化。
列出最多 4 个检索短句，每个不超过 30 字，用聊天里可能出现的说法写，可以带关键词、同义说法或英文缩写。只输出 JSON：{"queries":["…"]}，不输出代码围栏。"""

SYSTEM = """你扮演用户本人，在微信私聊里回复一位好友。
输入 JSON：profile 是用户自己写的本人资料（background 背景与自述，style 表达偏好，可能为空）；facts 是本人分析得出的关于用户本人的结论：“自述”是本人明确说过的（如 MBTI），“本人补充”是用户亲手写的、最可信，其余性格与三观倾向只是推测；以「时间线」开头的行是本人在时间线上记录或确认过的经历与近况，同样可信，回答近况、打算、感受、变化这类问题时优先依据它；tags 是本人常聊、有证据的兴趣与特点；examples 是用户本人在相似情境下的历史回复（time=当时日期，context=当时的前文，本人/对方分别标明；reply=用户当时的回复，其中每一行是当时单独发出的一条消息），用来模仿措辞、长短、语气、回应方式和拆分消息的习惯；conversation 是这次的对话，from=friend 是好友，from=me 是用户本人；count 是这次最多发出的消息条数。
只回复 conversation 中好友最后发来的消息，像用户本人平时那样说话：长短、语气词、标点和表情贴近 examples 与 style；最多发 count 条，这是按本人相近情境的实际条数定的上限，不要超过，也不要跟着 conversation 里之前每次发了几条走。话说完就停，不必发满，更不要为凑条数添内容；上限小就只说最要紧的话，不要把几句话挤进一条。
好友问到用户本人的情况（性格、MBTI、喜好、经历、想法等）时，先通读全部 examples 和 facts 再回答：以 profile 和 facts 为准；没有写到的，examples 里用户本人亲口说过的关于自己的话可以作为线索；同一件事前后说法不一时，以 time 最近的为准，并留意本人说过的变化过程。
时间线里的内容像本人随口提起那样说，不要汇报式地罗列；时间线没有写到、或好友没有问到的细节不要展开。
conversation 里 from=me 的早先回复是这次试用中生成的，不是事实依据；与上述资料不符时以资料为准，可以像本人那样自然改口。
examples 里的人名、称呼、地点、时间、安排和承诺属于当时的对话，不是当前事实，不要照搬；以上都没有依据的事实不要编造，可以像本人那样含糊带过或反问。
不解释，不提及资料、分析、样例或本说明。只输出 JSON：{"messages":["…"]}，不输出代码围栏。"""

ROUTE = """你替用户本人决定怎么回复一位微信好友。输入 JSON：conversation 是这次的对话（from=friend 是好友，from=me 是用户本人，me 的早先回复是试用中生成的，不是事实），facts 是已有的本人分析结论，timeline 是本人的时间线目录（periods 是阶段、events 是事件，research 只能引用这里出现的 id）。
第一步选 depth：basic 适合寒暄、接梗、问候或一眼能答的简单问题；standard 适合一般聊天；deep 适合需要回忆本人经历、想法或细节才能答好的问题。拿不准就选 standard。
第二步选 research：好友的问题是否需要了解本人最近的真实经历、状态或打算（如近况、计划、感受、变化）才答得好。需要才从 timeline 里选出确实相关的条目 id（events 和 periods 合计最多 12 个，宁缺毋滥），并给出 queries：最多 3 条检索短句，用来查本人过去怎么聊这类话题。不需要调研时三项都用空数组。
只输出 JSON：{"depth":"basic|standard|deep","research":{"events":[],"periods":[],"queries":[]}}，不输出代码围栏。"""


def depth(value=None):
    value = DEFAULT_REPLY_DEPTH if value is None else value
    if value not in REPLY_DEPTH_SETTINGS:
        raise SelfError("invalid-request", "请选择自动、基础、标准或深度检索")
    return value


def conversation(params):
    turns = params.get("messages")
    if not isinstance(turns, list) or not 1 <= len(turns) <= MAX_TURNS:
        raise SelfError("invalid-request", f"对话最多 {MAX_TURNS} 条消息")
    result = []
    for turn in turns:
        if (not isinstance(turn, dict) or turn.get("from") not in ("friend", "me") or
                not isinstance(turn.get("text"), str) or not turn["text"].strip() or len(turn["text"]) > TURN_CHARS):
            raise SelfError("invalid-request", f"每条消息须在 {TURN_CHARS} 字以内")
        result.append({"from": turn["from"], "text": turn["text"].strip()})
    if result[-1]["from"] != "friend":
        raise SelfError("invalid-request", "请先以好友身份发一条消息")
    return result


def query(turns):
    """Samples keep their context as 本人/对方 lines, so the retrieval query takes the same shape."""
    lines = [("对方：" if turn["from"] == "friend" else "本人：") + turn["text"] for turn in turns[-QUERY_TURNS:]]
    return "\n".join(lines)[-MAX_QUERY_CHARS:]


def focus(turns):
    """The friend's trailing messages: what the reply answers, so retrieval starts from them
    instead of letting earlier turns of the conversation outweigh the question."""
    start = len(turns)
    while start > 0 and turns[start - 1]["from"] == "friend":
        start -= 1
    return turns[start:]


def _clip(text, limit=FACT_CHARS):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fact_lines(store):
    """自我剖析 conclusions as priority-ordered short lines: self-reports, the user's own notes, then inferences."""
    lines = []
    for noun, view in (("性格", PersonaStore(store).view()), ("三观", ValuesStore(store).view())):
        for key, label in PersonaStore.reports if noun == "性格" else ():
            report = (view.get("selfReports") or {}).get(key)
            if report and report.get("text"):
                lines.append(f"{label}（自述）：{_clip(report['text'])}")
        for row in view["dimensions"]:
            if row["manual"]:
                lines.append(f"{noun}·{row['label']}（本人补充）：{_clip(row['manual']['text'])}")
            if row["selfReport"] and row["selfReport"].get("text"):
                lines.append(f"{noun}·{row['label']}（自述）：{_clip(row['selfReport']['text'])}")
            if row["inference"] and row["inference"].get("text"):
                confidence = row["inference"].get("confidence") or "推测"
                lines.append(f"{noun}·{row['label']}（推测，{confidence}）：{_clip(row['inference']['text'])}")
    return lines


def prefix(lines, budget):
    """Priority-ordered lines cut to a character budget; a prefix of the same list, never a re-selection."""
    result, used = [], 0
    for line in lines:
        if used + len(line) > budget:
            break
        result.append(line)
        used += len(line)
    return result


def tag_lines(tags, count, budget):
    """Confirmed and strong tags by category, the most-mentioned first, within a fixed budget."""
    chosen = sorted((tag for tag in tags.tags() if tag["level"] in ("confirmed", "strong")),
                    key=lambda tag: (tag["level"] != "confirmed", -tag["mentions"], tag["name"]))[:count]
    groups = {}
    for tag in chosen:
        groups.setdefault(tag["category"], []).append(tag["name"])
    result, used = [], 0
    for category, names in groups.items():
        line = f"{category}：" + "、".join(names)
        if used + len(line) > budget:
            break
        result.append(line)
        used += len(line)
    return result


def _day(ms):
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if isinstance(ms, int) and ms > 0 else ""


def parse(text):
    messages = json_object(text).get("messages")
    if isinstance(messages, str):
        messages = [messages]
    result = [item.strip()[:MESSAGE_CHARS] for item in messages if isinstance(item, str) and item.strip()]         if isinstance(messages, list) else []
    if not result:
        raise SelfError("invalid-output", "模型返回的内容无法使用")
    return result[:MAX_MESSAGES]


def bubble_count(rows):
    """这次最多发几条：从本人相近回复里随机抽一条，以它当时发的条数为上限。

    上限不交给模型：它几乎总发 2–3 条（评测 50 例里 49 例，真实只有 18 例），写进提示词的
    区间也只被当成目标，“默认一条”又变成几乎全是一条。抽样让每次的条数跟着本人的节奏走。
    只是上限：要求发满时，抽到大数模型就会凑内容甚至编造。
    """
    if not rows:
        return random.choice(NO_SAMPLE_COUNTS)
    return min(MAX_MESSAGES, random.choice(rows)["reply"].count("\n") + 1)


def fit(messages, count):
    """模型多写的并进最后一条：不丢内容，也不超过这次的条数；写少了就按少的发。"""
    if len(messages) <= count:
        return messages
    return messages[:count - 1] + [" ".join(messages[count - 1:])[:MESSAGE_CHARS]]


def parse_plan(text):
    queries = json_object(text).get("queries")
    result = []
    for item in queries if isinstance(queries, list) else []:
        item = " ".join(item.split())[:PLAN_QUERY_CHARS] if isinstance(item, str) else ""
        if item and item not in result:
            result.append(item)
    return result[:PLAN_QUERIES]


def parse_route(text):
    """The router's answer: a concrete depth (bad values stay standard) and cleaned research lists."""
    data = json_object(text)
    research = data.get("research") if isinstance(data.get("research"), dict) else {}

    def identifiers(key):
        rows = research.get(key)
        return [item for item in rows if isinstance(item, str) and item] if isinstance(rows, list) else []

    queries = []
    for item in research.get("queries") if isinstance(research.get("queries"), list) else []:
        item = " ".join(item.split())[:PLAN_QUERY_CHARS] if isinstance(item, str) else ""
        if item and item not in queries:
            queries.append(item)
    chosen = data.get("depth")
    return {"depth": chosen if chosen in REPLY_DEPTHS else "standard",
            "events": identifiers("events"), "periods": identifiers("periods"),
            "queries": queries[:PLAN_QUERIES]}


def timeline_facts(catalog, event_ids, period_ids):
    """The routed timeline entries as fact lines, catalog order (newest first).

    ``时间线`` marks them as the user's own records in the SYSTEM prompt; ongoing
    periods say so, since “最近在做什么” often answers from those.
    """
    events, periods = set(event_ids), set(period_ids)
    lines = []
    for item in catalog["events"]:
        if item["id"] in events:
            lines.append(f"时间线·{item['when']}：{item['title']}")
    for item in catalog["periods"]:
        if item["id"] in periods:
            lines.append(f"时间线·{item['when']}{'（进行中）' if item['ongoing'] else ''}：{item['title']}")
    return lines


def eval_guard(params):
    """Optional leak guards for expression evaluation (docs/loop/reply-eval protocol 3.3).

    Absent keys keep production behaviour unchanged; present keys truncate retrieval
    and the auto route's timeline catalog to before ``beforeTimeMs`` and drop the given
    sample/message ids. ``search`` only applies ``excludeMessageIds`` for the same
    contact, so ``Trial._sources`` also filters ``excludeSampleIds`` after retrieval.
    """
    before = integer(params.get("beforeTimeMs"), None, 1, 2 ** 53)
    lists = {}
    for key in ("excludeSampleIds", "excludeMessageIds"):
        identifiers = params.get(key)
        if identifiers is None:
            continue
        if not isinstance(identifiers, list) or len(identifiers) > 500 or \
                any(not isinstance(item, str) or not item for item in identifiers):
            raise SelfError("invalid-request", f"{key} 应为不超过 500 个的标识列表")
        lists[key] = identifiers
    if before is None and not lists:
        return {}
    return {"beforeTimeMs": before, "excludeSampleIds": lists.get("excludeSampleIds"),
            "excludeMessageIds": lists.get("excludeMessageIds")}


class Trial(Job):
    title, kind, fallback = "聊天试用", "self-trial", "回复生成未完成，请重试"

    def __init__(self, plugin, store, account, turns, tags=None, level=DEFAULT_REPLY_DEPTH,
                 before_time_ms=None, exclude_sample_ids=None, exclude_message_ids=None):
        super().__init__(plugin, store, account, phase="reading", depth=level)
        self.turns, self.tags, self.level = turns, tags, level
        # 评测防泄漏参数；默认 None 时检索行为与既有完全一致。
        self.before_time_ms = before_time_ms
        self.exclude_sample_ids = frozenset(exclude_sample_ids or ())
        self.exclude_message_ids = frozenset(exclude_message_ids or ())

    def _route(self, context, catalog, route_facts):
        """auto：一次小调用同时定深度和调研范围；路由失败就按标准档、不调研地直接回。

        ids 只保留目录里真实存在的条目，合计不超过 ``MAX_RESEARCH``——路由模型说的
        其余一切都被丢弃。
        """
        request = {"system": ROUTE, "prompt": json.dumps({"conversation": self.turns, "facts": route_facts,
                                                          "timeline": catalog}, ensure_ascii=False),
                   "jsonMode": True, "thinking": False, "maxOutputTokens": 512}
        try:
            route = self.generate(context, request, "self-trial-route", parse_route)
        except Cancelled:
            raise
        except Exception:
            context.check()
            return {"depth": "standard", "events": [], "periods": [], "queries": []}
        known = {item["id"] for item in catalog["events"]} | {item["id"] for item in catalog["periods"]}
        route["events"] = [item for item in route["events"] if item in known]
        route["periods"] = [item for item in route["periods"] if item in known]
        if len(route["events"]) + len(route["periods"]) > MAX_RESEARCH:
            route["periods"] = route["periods"][:max(0, MAX_RESEARCH - len(route["events"]))]
        return route

    def _plan(self, context, known):
        """深度: the model names what to look up first; a failed plan only narrows the search."""
        request = {"system": PLAN, "prompt": json.dumps({"conversation": self.turns, "facts": known}, ensure_ascii=False),
                   "jsonMode": True, "thinking": False, "maxOutputTokens": 512}
        try:
            return self.generate(context, request, "self-trial-plan", parse_plan)
        except Cancelled:
            raise
        except Exception:
            context.check()
            return []

    def _sources(self, context, level, queries):
        """Focus examples answer the friend's last messages; planned lookups and the whole
        exchange fill the rest, without repeating a sample."""
        limit, first = level["examples"], level["focus"]
        retrieval = Retrieval(self.plugin.core, self.store, self.account)
        modes = set()

        def search(text, count):
            context.check()
            params = {"query": text, "limit": min(MAX_SOURCES, count)}
            if self.before_time_ms is not None:
                params["beforeTimeMs"] = self.before_time_ms
            if self.exclude_message_ids:
                params["excludeMessageIds"] = sorted(self.exclude_message_ids)
            rows = retrieval.search(params, None)
            # search 的 excludeMessageIds 只在同联系人时生效，排除样例须在此再过滤一次。
            if self.exclude_sample_ids:
                rows = [row for row in rows if row["id"] not in self.exclude_sample_ids]
            modes.add(retrieval.mode)
            return rows

        asked = focus(self.turns)
        focused = search(query(asked), limit)
        groups = [search(text, PLAN_QUERIES) for text in queries]
        if len(asked) < len(self.turns):
            groups.append(search(query(self.turns), limit))
        picked, seen = [], set()
        for row in focused[:first] + [row for group in groups for row in group] + focused[first:]:
            if len(picked) < limit and row["id"] not in seen:
                picked.append(row)
                seen.add(row["id"])
        return picked, "hybrid" if "hybrid" in modes else "lexical"

    def work(self, context):
        saved = self.plugin.core.self_profile(self.account)
        profile = {key: (saved.get(key) or "")[:PROFILE_CHARS] for key in ("background", "style")}
        if self.level == "auto":
            from self_timeline import timeline_catalog
            catalog = timeline_catalog(self.store, self.before_time_ms)
            self._set(phase="routing", catalogItems=len(catalog["events"]) + len(catalog["periods"]))
            # The router sees a priority-ordered prefix of the conclusions; the reply
            # gets its depth's slice of the same list, so routing stays cheap and the
            # answer never reads more than its depth allows.
            route = self._route(context, catalog, prefix(fact_lines(self.store), ROUTE_FACTS))
        else:
            route, catalog = None, None
        level = LEVELS[route["depth"] if route else self.level]
        base = prefix(fact_lines(self.store), level["facts"])
        known = base + (timeline_facts(catalog, route["events"], route["periods"]) if route else [])
        tags = tag_lines(self.tags, level["tags"], level["tagChars"]) if self.tags is not None else []
        queries = route["queries"] if route else (self._plan(context, known) if level["plan"] else [])
        self._set(queries=queries)
        sources, mode = self._sources(context, level, queries)
        context.check()
        service = self.plugin.models.service()
        window = service.get("contextTokens") if isinstance(service, dict) else None
        window = window if type(window) is int and window >= 4096 else 32768
        output = min(level["outputTokens"], max(1024, window // 3))
        # One character per token is a conservative bound for Chinese text and JSON.
        room = window - output - len(SYSTEM) - 1000
        count = bubble_count(sources)

        def render(rows):
            return json.dumps({"profile": profile, "facts": known, "tags": tags, "conversation": self.turns,
                               "count": count,
                               "examples": [{"time": _day(row.get("timeMs")), "context": row["input"][-EXAMPLE_CHARS:],
                                             "reply": row["reply"][:EXAMPLE_CHARS]} for row in sources]},
                              ensure_ascii=False)
        while sources and len(render(sources)) > room:
            sources = sources[:-1]
        self._set(phase="writing", sources=sources, mode=mode, depth=route["depth"] if route else self.level,
                  research=len(known) - len(base))
        request = {"system": SYSTEM, "prompt": render(sources), "jsonMode": True, "maxOutputTokens": output,
                   **({} if level["thinking"] else {"thinking": False})}
        return {"messages": fit(self.generate(context, request, "self-trial", parse), count)}
