"""Shared timeline functions for 自我剖析·时间线 and 知己分析·时间线 (added 2026-11-07).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Pure functions moved out of ``kyf.self-analysis``'s ``self_timeline.py``: time parsing and
relative-time resolution, event ids and model-output parsers, merging, period building,
edit overlay and validation, view rows and paging, plus a small undo journal over the
plugin's own sqlite tables. Nothing here reads the plugin's tables by name, calls a model
or imports plugin code. Every default keeps 自我剖析's behaviour; 知己分析 passes its own
tracks, categories, period grouping, undo source and depth.

Errors are ``TimelineError(code, message, detail)``; a plugin wraps the calls with
``reraise_as(PluginError)`` so its own error handling stays unchanged.
"""
import calendar
import datetime
import functools
import hashlib
import json
import re
import time
import uuid

DAY_MS = 86_400_000
TITLE_CHARS = 40
PHRASE_CHARS = 40
REASON_CHARS = 80
STATUSES = ("occurred", "planned", "cancelled", "uncertain")
KINDS = ("point", "start", "end", "state")
IMPORTANCE = ("important", "general")
PRECISIONS = ("day", "month", "season", "year", "about", "unknown")
COLORS = ("#07c160", "#576b95", "#c2841c", "#b05a4a", "#7f77dd", "#3d8f8a", "#9a6fb0", "#7b8a4e")
# “时间说法不一致时采用最精确、最可靠的一条”（doc 2.3）。
PRECISION_RANK = {"unknown": 0, "about": 1, "year": 2, "season": 3, "month": 4, "day": 5}
# 非相对表达由模型推断，精度降一级（doc 2.2）。
PRECISION_LOWER = {"day": "month", "month": "season", "season": "year", "year": "about", "about": "unknown"}
# 归并只比较同一主线的候选，按时间排序后小批交给模型。一次归并请求最多给模型的候选数：几百个一组时模型只挑出零星几对（实测 471 个只合了 39 个）。
MERGE_CHUNK = 40
# 同主线、同类型、同标题且相隔不超过这么多月的候选直接视为同一件事，不问模型。
SAME_TITLE_MONTHS = 6
# 一次时间线回复里事件的字节预算；Core 丢弃超过 4 MiB 的插件输出行，
# 时期、主线和其余字段要留在余量里。
EVENT_PAGE_BYTES = 2 * 1024 * 1024
UNDO_LIMIT = 200
ECHO = "上一轮回传了输入，没有给出分析结果。本轮只输出events一个数组。"
# 用户可编辑的事件字段；编辑后的事件不参与自定义主线改归。
EDITABLE_EVENT_FIELDS = ("title", "track", "status", "importance", "startMs", "endMs", "precision",
                         "phrase", "place", "people")
EDITABLE_PERIOD_FIELDS = ("title", "track", "startMs", "endMs", "precision", "ongoing")
EDIT_VERBS = {"confirm": "确认", "unconfirm": "取消确认", "edit": "修改", "delete": "删除", "restore": "恢复",
              "add": "添加", "visible": "显示设置"}
# Fields a plugin may add to its editable list; they are validated as short strings.
_STRING_FIELDS = ("track", "status", "importance", "precision", "phrase", "place", "people", "kind", "category", "note")


class TimelineError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def reraise_as(error):
    """Decorator: turn ``TimelineError`` into ``error(code, message, detail)``; returned parsers are wrapped too."""
    def wrap(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
            except TimelineError as exc:
                raise error(exc.code, exc.message, exc.detail) from None
            return wrap(result) if callable(result) else result
        return call
    return wrap


def json_object(text):
    """The first JSON object in a model reply, tolerating code fences and stray prose."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        value = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        try:
            value = json.loads(raw[start:end + 1]) if 0 <= start < end else None
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise TimelineError("invalid-output", "模型返回的内容无法使用",
                            "只输出一个完整JSON对象；若上一轮内容被截断，请压缩输出长度")
    return value


# 时间 ------------------------------------------------------------------------------------------------------------------
def _day(value):
    return time.strftime("%Y-%m-%d", time.localtime(value / 1000)) if value else None


def _midnight(day):
    """Local midnight of ``YYYY-MM-DD`` in epoch milliseconds."""
    parsed = datetime.datetime.strptime(day, "%Y-%m-%d")
    return int(time.mktime(parsed.timetuple())) * 1000


def parse_when(value):
    """The model's ``when`` string to ``(startMs, endMs, precision)`` or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace("—", "-").replace("～", "-").replace("~", "-")
    if not text:
        return None
    season = {"春": 3, "夏": 6, "秋": 9, "冬": 12}
    match = re.fullmatch(r"(\d{4}(?:-\d{2}(?:-\d{2})?)?)-(\d{4}(?:-\d{2}(?:-\d{2})?)?)", text)
    if match:  # a “起—止” range; each side is a full date on its own
        first, last = parse_when(match.group(1)), parse_when(match.group(2))
        if first and last:
            return first[0], last[1], min(first[2], last[2], key=lambda kind: PRECISION_RANK[kind])
        return None
    match = re.fullmatch(r"(\d{4})-([春夏秋冬])", text)
    if match and match.group(2) in season:
        month = season[match.group(2)]
        start = _midnight(f"{match.group(1)}-{month:02d}-01")
        end = _midnight(f"{match.group(1)}-{month + 2:02d}-01") - 1
        return start, end, "season"
    parts = text.split("-")
    try:
        if len(parts) == 1 and re.fullmatch(r"\d{4}", parts[0]):
            year = int(parts[0])
            start = _midnight(f"{year}-01-01")
            return start, _midnight(f"{year + 1}-01-01") - 1, "year"
        if len(parts) == 2 and re.fullmatch(r"\d{4}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]):
            year, month = int(parts[0]), int(parts[1])
            if not 1 <= month <= 12:
                return None
            start = _midnight(f"{year}-{month:02d}-01")
            last = calendar.monthrange(year, month)[1]
            return start, _midnight(f"{year}-{month:02d}-{last:02d}") + DAY_MS - 1, "month"
        if len(parts) == 3 and all(re.fullmatch(r"\d{1,4}", part) for part in parts):
            year, month, day = (int(part) for part in parts)
            _midnight(f"{year:04d}-{month:02d}-{day:02d}")  # raises for impossible dates
            return _midnight(f"{year:04d}-{month:02d}-{day:02d}"), _midnight(f"{year:04d}-{month:02d}-{day:02d}") + DAY_MS - 1, "day"
    except ValueError:
        return None
    return None


def parse_user_when(value):
    """A date as people type it: 2024.05, 2024/5, 2024年5月3日, 202405, 2024春, 2024.03-2024.06."""
    text = re.sub(r"[./．／。、年月\s至到]+", "-", value.strip()).replace("日", "").replace("号", "").strip("-")
    if re.fullmatch(r"\d{6}|\d{8}", text):
        text = "-".join(filter(None, (text[:4], text[4:6], text[6:])))
    if not re.match(r"(?:19[7-9]\d|20\d\d)(?!\d)", text):  # 时间存成毫秒且不为负，本地时间换算也从 1970 年起
        return None
    text = re.sub(r"(?<=\d{4})([春夏秋冬])", r"-\1", text)
    try:
        return parse_when(re.sub(r"(?<=-)(\d)(?=-|$)", r"0\1", text))
    except OverflowError:  # 区间的另一端超出本地时间能表示的范围
        return None


def _week_start(ms):
    day = datetime.datetime.fromtimestamp(ms / 1000)
    monday = day - datetime.timedelta(days=day.weekday())
    return int(time.mktime(monday.date().timetuple())) * 1000


def _month_start(ms, offset=0):
    day = datetime.datetime.fromtimestamp(ms / 1000)
    total = day.year * 12 + day.month - 1 + offset
    year, month = divmod(total, 12)
    return int(time.mktime(datetime.date(year, month + 1, 1).timetuple())) * 1000


def _week_day(said_ms, weeks, weekday):
    """Midnight of ``weekday`` (0=Monday) inside the week ``weeks`` away from ``said_ms``."""
    return _week_start(said_ms) + (7 * weeks + weekday) * DAY_MS


def _month_day(said_ms, months, day):
    """Midnight of ``day`` inside the month ``months`` away, clamped to the month's end."""
    start = _month_start(said_ms, months)
    date = datetime.datetime.fromtimestamp(start / 1000).date()
    last = calendar.monthrange(date.year, date.month)[1]
    return start + (min(day, last) - 1) * DAY_MS


def resolve_relative(phrase, said_ms):
    """Program-computed time for common relative expressions (doc 2.2), or ``None``.

    The model is bad at date arithmetic; these overrides win over its ``when``.
    Rules are ordered longest expression first, so “上上周三” resolves as a day
    instead of the whole “上周” (supervisor review C2)."""
    if not phrase or not said_ms:
        return None
    text = re.sub(r"[\s，。！？!?,.]", "", phrase)
    day = datetime.datetime.fromtimestamp(said_ms / 1000).date()
    today = int(time.mktime(day.timetuple())) * 1000
    weeks = {"上上": -2, "上": -1, "这": 0, "本": 0, "下下": 2, "下": 1}
    weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    year_words = {"前年": -2, "去年": -1, "今年": 0, "明年": 1}
    month_words = {"上上个月": -2, "上个月": -1, "这个月": 0, "本月": 0, "下个月": 1, "下下个月": 2}
    parts_in_month = {"月初": 1, "月中": 15, "月底": 99, "月末": 99, "上旬": 1, "中旬": 11, "下旬": 21,
                      "初": 1, "中": 15, "底": 99, "末": 99}
    rules = []
    # 周几：上周五 / 上上周三 / 下周五，只取那一天而不是整周。
    rules.append((rf"({'|'.join(weeks)})[个]?(?:周|星期|礼拜)([{''.join(weekdays)}])",
                  lambda match: (_week_day(said_ms, weeks[match.group(1)], weekdays[match.group(2)]),
                                 _week_day(said_ms, weeks[match.group(1)], weekdays[match.group(2)]) + DAY_MS - 1, "day")))
    # 月内位置：上个月底 / 这个月中旬，月底给日精度落最后一天（资料 C3）。
    rules.append((rf"({'|'.join(month_words)})月?({'|'.join(parts_in_month)})",
                  lambda match: _month_span(said_ms, month_words[match.group(1)], parts_in_month[match.group(2)])))
    # 年初/年底：去年底落在 12 月（月精度），去年年初落在 1 月。
    rules.append((rf"({'|'.join(year_words)})年?(年初|年底|年末|初|底)",
                  lambda match: _year_edge(said_ms, year_words[match.group(1)], match.group(2))))
    # 第X季度：去年第三季度。
    rules.append((rf"({'|'.join(year_words)})?第([一二三四1-4])季度",
                  lambda match: _quarter(said_ms, year_words.get(match.group(1) or "今年", 0), match.group(2))))
    # 学期：这学期 / 上学期 / 下学期；春季学期 2–7 月，秋季学期 9–次年 1 月。
    rules.append(("下个?学期", lambda match: _semester(said_ms, 1)))
    rules.append(("(上)?(这|本)?个?学期", lambda match: _semester(said_ms, -1 if match.group(1) else 0)))
    table = [
        ("大前天", (today - 3 * DAY_MS, today - 2 * DAY_MS - 1, "day")),
        ("前天", (today - 2 * DAY_MS, today - DAY_MS - 1, "day")),
        ("昨天", (today - DAY_MS, today - 1, "day")),
        ("今天", (today, today + DAY_MS - 1, "day")),
        ("明天", (today + DAY_MS, today + 2 * DAY_MS - 1, "day")),
        ("后天", (today + 2 * DAY_MS, today + 3 * DAY_MS - 1, "day")),
        ("上上个星期", (_week_start(said_ms) - 14 * DAY_MS, _week_start(said_ms) - 7 * DAY_MS - 1, "day")),
        ("上上周", (_week_start(said_ms) - 14 * DAY_MS, _week_start(said_ms) - 7 * DAY_MS - 1, "day")),
        ("下下个星期", (_week_start(said_ms) + 14 * DAY_MS, _week_start(said_ms) + 21 * DAY_MS - 1, "day")),
        ("下下周", (_week_start(said_ms) + 14 * DAY_MS, _week_start(said_ms) + 21 * DAY_MS - 1, "day")),
        ("上个星期", (_week_start(said_ms) - 7 * DAY_MS, _week_start(said_ms) - 1, "day")),
        ("上周", (_week_start(said_ms) - 7 * DAY_MS, _week_start(said_ms) - 1, "day")),
        ("本周", (_week_start(said_ms), _week_start(said_ms) + 7 * DAY_MS - 1, "day")),
        ("这周", (_week_start(said_ms), _week_start(said_ms) + 7 * DAY_MS - 1, "day")),
        ("下个星期", (_week_start(said_ms) + 7 * DAY_MS, _week_start(said_ms) + 14 * DAY_MS - 1, "day")),
        ("下周", (_week_start(said_ms) + 7 * DAY_MS, _week_start(said_ms) + 14 * DAY_MS - 1, "day")),
        ("上上个月", (_month_start(said_ms, -2), _month_start(said_ms, -1) - 1, "month")),
        ("上个月", (_month_start(said_ms, -1), _month_start(said_ms) - 1, "month")),
        ("这个月", (_month_start(said_ms), _month_start(said_ms, 1) - 1, "month")),
        ("本月", (_month_start(said_ms), _month_start(said_ms, 1) - 1, "month")),
        ("下下个月", (_month_start(said_ms, 2), _month_start(said_ms, 3) - 1, "month")),
        ("下个月", (_month_start(said_ms, 1), _month_start(said_ms, 2) - 1, "month")),
        ("今年", (_month_start(said_ms, -(day.month - 1)), _month_start(said_ms, 13 - day.month) - 1, "year")),
        ("去年", (_month_start(said_ms, -(day.month - 1) - 12), _month_start(said_ms, -(day.month - 1)) - 1, "year")),
        ("前年", (_month_start(said_ms, -(day.month - 1) - 24), _month_start(said_ms, -(day.month - 1) - 12) - 1, "year")),
        ("明年", (_month_start(said_ms, 13 - day.month), _month_start(said_ms, 25 - day.month) - 1, "year")),
    ]
    # 长表达式优先：先试带位置/星期的规则，再退回整单位的说法。
    for pattern, resolver in rules:
        match = re.search(pattern, text)
        if match:
            return resolver(match)
    for expression, value in table:
        if expression in text:
            return value
    return None


def _month_span(said_ms, months, part):
    """月初/月中/月底/上旬/中旬/下旬 within the month ``months`` away."""
    start = _month_start(said_ms, months)
    date = datetime.datetime.fromtimestamp(start / 1000).date()
    last = calendar.monthrange(date.year, date.month)[1]
    if part == "月初":
        return start, start + DAY_MS - 1, "day"
    if part in ("月底", "月末"):
        return start + (last - 1) * DAY_MS, start + last * DAY_MS - 1, "day"
    if part == "月中":
        return start + 14 * DAY_MS, start + 15 * DAY_MS - 1, "day"
    if part == "上旬":
        return start, start + 10 * DAY_MS - 1, "day"
    if part == "中旬":
        return start + 10 * DAY_MS, start + 20 * DAY_MS - 1, "day"
    return start + 20 * DAY_MS, start + last * DAY_MS - 1, "day"


def _year_edge(said_ms, years, edge):
    """年初/年底 of the year ``years`` away; 月精度, per 资料 C3 (“年底给月精度落在 12 月”)."""
    start = _month_start(said_ms, years * 12 - (datetime.datetime.fromtimestamp(said_ms / 1000).month - 1))
    if edge in ("年初", "初"):
        return start, start + 31 * DAY_MS - 1, "month"
    end = _month_start(said_ms, (years + 1) * 12 - (datetime.datetime.fromtimestamp(said_ms / 1000).month - 1)) - 1
    return end - 30 * DAY_MS, end, "month"


def _quarter(said_ms, years, number):
    quarter = {"一": 0, "二": 1, "三": 2, "四": 3, "1": 0, "2": 1, "3": 2, "4": 3}[number]
    month = datetime.datetime.fromtimestamp(said_ms / 1000).month
    start = _month_start(said_ms, years * 12 - (month - 1) + quarter * 3)
    return start, _month_start(said_ms, years * 12 - (month - 1) + quarter * 3 + 3) - 1, "month"


def _semester(said_ms, offset):
    """大陆学年 9 月起：春季学期 2–7 月，秋季学期 9–次年 1 月（资料 C3）。"""
    day = datetime.datetime.fromtimestamp(said_ms / 1000).date()
    year, month = day.year, day.month
    spring = 2 <= month <= 7  # 1 月属于上学年的秋季学期；8 月算即将开始的秋季学期
    if offset:
        if spring:
            year, spring = (year, False) if offset > 0 else (year - 1, False)
        else:
            year, spring = (year + 1, True) if offset > 0 else (year, True)
    if spring:
        return _midnight(f"{year}-02-01"), _midnight(f"{year}-08-01") - 1, "month"
    return _midnight(f"{year}-09-01"), _midnight(f"{year + 1}-02-01") - 1, "month"


def birth_year_of(background):
    """An explicit birth year in the user's profile, if one is stated (doc 2.3)."""
    if not isinstance(background, str):
        return None
    match = re.search(r"(19\d{2}|20\d{2})\s*年(?:出?生)", background)
    return int(match.group(1)) if match else None


def display_when(start_ms, end_ms, precision):
    """Human display per precision; never invents a day the source does not give."""
    if not start_ms:
        return "时间不明"
    def format(value, with_day):
        day = datetime.datetime.fromtimestamp(value / 1000)
        return f"{day.year}年{day.month}月{day.day}日" if with_day else f"{day.year}年{day.month}月"
    if precision == "day":
        return format(start_ms, True) if end_ms is None or end_ms - start_ms < DAY_MS else f"{format(start_ms, True)}–{format(end_ms, True)}"
    if precision == "month":
        return format(start_ms, False) if end_ms is None or end_ms - start_ms < 40 * DAY_MS else f"{format(start_ms, False)}–{format(end_ms, False)}"
    if precision == "season":
        return format(start_ms, False)
    return f"{datetime.datetime.fromtimestamp(start_ms / 1000).year}年" + ("前后" if precision == "about" else "")


def event_id(message_ids):
    raw = hashlib.sha256(json.dumps(sorted(set(message_ids))).encode()).hexdigest()
    return "ev-" + raw[:16]



# 模型输出校验 ----------------------------------------------------------------------------------------------------------
def parse_events(items, birth_year=None, *, tracks=None, categories=None, default_track=None, decode=None, accept=None):
    """Validate one window's events; the program owns time arithmetic (doc 2.2/2.3).

    ``tracks``: the model's ``track`` must be one of them (自我剖析 passes its built-in tracks).
    Without ``tracks`` every event goes on ``default_track``. ``categories``: the model's
    ``category`` must be one of them and is kept on the event. ``decode`` reads the reply
    (default ``json_object``); ``accept(row, event)`` may drop an event by returning False."""
    decode = decode or json_object

    def parse(text):
        value = decode(text)
        rows = value.get("events")
        if "messages" in value or "chats" in value:
            raise TimelineError("invalid-output", ECHO)
        if not isinstance(rows, list):
            raise TimelineError("invalid-output", "上一轮没有给出events数组。只输出events:[...]。")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            index = row.get("i")
            if type(index) is not int or not 0 <= index < len(items):
                continue
            item = items[index]
            title = row.get("title")
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_CHARS:
                continue
            if tracks is not None and row.get("track") not in tracks or row.get("kind") not in KINDS or \
                    row.get("status") not in STATUSES or row.get("importance") not in IMPORTANCE:
                continue
            if categories is not None and row.get("category") not in categories:
                continue
            precision = row.get("precision") if row.get("precision") in PRECISIONS else "unknown"
            phrase = row.get("phrase")
            phrase = phrase.strip()[:PHRASE_CHARS] if isinstance(phrase, str) else ""
            if phrase and phrase not in item["text"]:
                phrase = ""  # a time phrase must come from the quoted own line
            said = item.get("time") or 0
            inferred = False
            resolved = resolve_relative(phrase, said)
            if resolved:
                start, end, precision = resolved
            else:
                # 非相对表达是模型的推断：精度降一级并标“推断”（doc 2.2）。
                inferred = isinstance(row.get("when"), str) and bool(row.get("when").strip())
                parsed = parse_when(row.get("when"))
                if parsed:
                    start, end, precision = parsed
                    precision = PRECISION_LOWER.get(precision, precision)
                else:
                    start, end, precision = None, None, "unknown"
                    inferred = False
            # “约某年”保留模型给出的不确定性（doc 2.1）；降级后的年份精度同样是 about。
            if precision == "year" and row.get("precision") == "about":
                precision = "about"
            reason = row.get("reason")
            reason = reason.strip()[:REASON_CHARS] if isinstance(reason, str) and reason.strip() else "见原话"
            status = row.get("status")
            is_range = isinstance(row.get("when"), str) and bool(re.fullmatch(
                r"\s*\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?\s*[-—～~]\s*\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?\s*",
                row["when"]))
            if status == "occurred" and start and said and start > said:
                status = "uncertain"  # “已发生”却晚于说话时间，判为矛盾（doc 2.3）
            elif status == "occurred" and not resolved and is_range and start and said and end and end > said >= start:
                status = "uncertain"  # 显式起—止区间跨过说话时间却称“已发生”，同样矛盾
            if start and birth_year and start < _midnight(f"{birth_year}-01-01"):
                continue  # 早于出生年份，丢弃
            place = row.get("place")
            place = place.strip()[:20] if isinstance(place, str) else ""
            people = row.get("people")
            people = people.strip()[:60] if isinstance(people, str) else ""
            track = row["track"] if tracks is not None else default_track
            event = {"title": title.strip(), "track": track, "kind": row["kind"], "status": status,
                     "importance": row["importance"], "phrase": phrase, "startMs": start, "endMs": end,
                     "precision": precision, "inferred": inferred, "place": place, "people": people, "reason": reason,
                     "saidMs": said, "messageId": item["id"], "chat": item.get("chat", ""),
                     "isGroup": bool(item.get("group")), "text": item["text"], "time": said,
                     "truncated": bool(item.get("part", 1) > 1 or item.get("context", {}).get("truncated"))}
            if categories is not None:
                event["category"] = row["category"]
            if accept is not None and not accept(row, event):
                continue
            identity = (event["messageId"], event["title"], event["track"], event["kind"], start, end, status)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(event)
        return {"events": result}
    return parse


def parse_review(items, proposed, *, decode=None):
    """Review only deletes or demotes; unlisted candidates are kept (doc 3.3).

    A listed ``{"n":n}`` deletes the candidate, ``{"n":n,"importance":"general"}``
    demotes it — models often omit the kept ones, so keeping must be the default."""
    decode = decode or json_object
    allowed = set(range(len(proposed)))

    def parse(text):
        value = decode(text)
        rows = value.get("events")
        if not isinstance(rows, list):
            raise TimelineError("invalid-output", "复核只输出events:[{n,…}]。")
        result = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {"n", "importance"}:
                raise TimelineError("invalid-output", "复核只能删除或降级原候选。")
            index = row.get("n")
            if type(index) is not int or index not in allowed:
                raise TimelineError("invalid-output", "复核引用了不存在的候选n。")
            importance = row.get("importance")
            if importance is not None and importance != "general":
                raise TimelineError("invalid-output", "复核不能提高重要程度。")
            result.append({"n": index, **({"importance": "general"} if importance else {})})
        return {"events": result}
    return parse


# 归并与时期构建 -------------------------------------------------------------------------------------------------------
def _time_text(event):
    start, end = event.get("startMs"), event.get("endMs")
    if not start:
        return ""
    if event.get("precision") == "day" and end and end - start >= DAY_MS:
        return f"{_day(start)}—{_day(end)}"
    if event.get("precision") in ("day", "month"):
        return display_when(start, end, event["precision"])
    return display_when(start, end, event["precision"])


def _title_key(event):
    return re.sub(r"[\s，,。.、；;：:（）()·\-—“”\"']", "", event["title"]).lower()


def _same_title_units(candidates):
    """同主线候选按（类型, 标题）先行归组：相隔不超过 SAME_TITLE_MONTHS 的连成一件事。

    时间不明的同名候选只有在同名的有时间组恰好一个时才并入它，否则彼此成一组。"""
    buckets = {}
    for event in candidates:
        buckets.setdefault((event["kind"], _title_key(event)), []).append(event)
    units = []
    for members in buckets.values():
        dated = sorted((event for event in members if event.get("startMs")), key=lambda event: event["startMs"])
        undated = [event for event in members if not event.get("startMs")]
        runs = []
        for event in dated:
            if runs and event["startMs"] - runs[-1][-1]["startMs"] <= SAME_TITLE_MONTHS * 31 * DAY_MS:
                runs[-1].append(event)
            else:
                runs.append([event])
        if undated and len(runs) == 1:
            runs[0].extend(undated)
        elif undated:
            runs.append(undated)
        units.extend(runs)
    return units


def _unit_start(unit):
    return min((event["startMs"] for event in unit if event.get("startMs")), default=None)


def _merge_chunks(units, offset=0):
    """时间排序后每 MERGE_CHUNK 个一批；offset 错开批次边界，让上一轮落在边界两侧的同一件事相遇。"""
    dated = sorted((unit for unit in units if _unit_start(unit)), key=_unit_start)
    undated = [unit for unit in units if not _unit_start(unit)]
    chunks = []
    for part in (dated, undated):
        cuts = [0, *range(offset or MERGE_CHUNK, len(part), MERGE_CHUNK), len(part)]
        chunks.extend(part[start:end] for start, end in zip(cuts, cuts[1:]) if end - start >= 2)
    return chunks



def parse_merge_groups(count, *, decode=None):
    decode = decode or json_object

    def parse(text):
        value = decode(text)
        rows = value.get("merge")
        if not isinstance(rows, list):
            raise TimelineError("invalid-output", "归并只输出merge:[{into,from}]。")
        groups = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get("into")) is not int or not 0 <= row["into"] < count:
                continue
            sources = row.get("from")
            if not isinstance(sources, list) or any(type(index) is not int or not 0 <= index < count for index in sources):
                continue
            members = {row["into"], *sources}
            if len(members) >= 2:
                groups.append(sorted(members))
        return {"merge": groups}
    return parse


def _merge_candidates(group, keep_details=False):
    """One merged event: all evidence, the most precise time, conflicts listed (doc 2.3/3.3)."""
    def evidence_key(event):
        return (event.get("messageId"), event.get("time") or 0, event.get("text", ""))
    occurred = [event for event in group if event["status"] == "occurred"]
    pool = occurred or group
    best = max(pool, key=lambda event: (PRECISION_RANK.get(event["precision"], 0),
                                        sum(1 for other in group if other["title"] == event["title"]),
                                        -(event.get("saidMs") or 0)))
    conflicts = []
    seen_times = {_time_text(best)}
    for event in group:
        text = _time_text(event)
        if text and text not in seen_times:
            seen_times.add(text)
            conflicts.append({"when": text, "chat": event.get("chat", "")})
    evidence, seen = [], set()
    for event in sorted(group, key=lambda event: -((event.get("time") or event.get("saidMs")) or 0)):
        row = {"messageId": event["messageId"], "chat": event.get("chat", ""), "isGroup": event.get("isGroup", False),
               "time": event.get("time") or event.get("saidMs"), "text": event.get("text", ""),
               "phrase": event.get("phrase", "")}
        row["truncated"] = event.get("truncated", False)
        key = evidence_key(event)
        if key not in seen:
            seen.add(key)
            evidence.append(row)
    titles = [event["title"] for event in pool]
    title = max(set(titles), key=lambda name: (titles.count(name), -len(name)))
    status = "occurred" if occurred else group[0]["status"]
    details = [{"title": event["title"], "phrase": event.get("phrase", ""), "when": _time_text(event),
                "chat": event.get("chat", "")} for event in group] if keep_details else []
    return {"title": title, "track": group[0]["track"], "kind": group[0]["kind"], "status": status,
            "importance": "important" if any(event["importance"] == "important" for event in group) else "general",
            "startMs": best.get("startMs"), "endMs": best.get("endMs"), "precision": best["precision"],
            "inferred": best.get("inferred", False),
            "phrase": best.get("phrase", ""), "place": best.get("place", ""), "people": best.get("people", ""),
            "reason": best.get("reason", ""), "saidMs": max(event.get("saidMs") or 0 for event in group),
            "evidence": evidence, "conflicts": conflicts, "details": details,
            **({"category": group[0]["category"]} if "category" in group[0] else {})}


def _fold_same_evidence(events):
    """依据集合相同即为同一件事（doc 4.2）：折叠成一行，否则主键 id 碰撞会整轮失败。

    保留更精确的时间与更高的重要程度，证据取并集，冲突列表合并。"""
    result, index_of = [], {}
    for event in events:
        key = tuple(sorted({row["messageId"] for row in event.get("evidence", [])}))
        if key not in index_of:
            index_of[key] = len(result)
            result.append(event)
            continue
        base = result[index_of[key]]
        if PRECISION_RANK.get(event.get("precision"), 0) > PRECISION_RANK.get(base.get("precision"), 0):
            base["startMs"], base["endMs"], base["precision"] = event.get("startMs"), event.get("endMs"), event["precision"]
        if event["importance"] == "important":
            base["importance"] = "important"
        if event.get("status") == "occurred":
            base["status"] = "occurred"
        seen = {(row.get("messageId"), row.get("time"), row.get("text")) for row in base["evidence"]}
        for row in event.get("evidence", []):
            row_key = (row.get("messageId"), row.get("time"), row.get("text"))
            if row_key not in seen:
                seen.add(row_key)
                base["evidence"].append(row)
        for conflict in event.get("conflicts", []):
            if conflict not in base.setdefault("conflicts", []):
                base["conflicts"].append(conflict)
        if len(event.get("evidence", [])) > len(base.get("details", [])) and event.get("details"):
            base["details"] = event["details"]
    return result


def _apply_merges(events, groups):
    """Union merged candidate indexes; singletons keep their own evidence."""
    merged, consumed = [], set()
    for members in sorted(groups, key=len, reverse=True):
        if any(index in consumed for index in members):
            continue
        merged.append(_merge_candidates([events[index] for index in members]))
        consumed.update(members)
    merged.extend(event for index, event in enumerate(events) if index not in consumed)
    return merged



def parse_assign(count, *, decode=None):
    decode = decode or json_object

    def parse(text):
        value = decode(text)
        rows = value.get("assign")
        if not isinstance(rows, list):
            raise TimelineError("invalid-output", "改归只输出assign:[{n,track}]。")
        result = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get("n")) is not int or not 0 <= row["n"] < count:
                continue
            if isinstance(row.get("track"), str) and row["track"].strip():
                result.append({"n": row["n"], "track": row["track"].strip()[:8]})
        return {"assign": result}
    return parse


def build_periods(events, group_fields=("track",)):
    """start/end/state entries and user-anchored facts become per-track periods (doc 3.3).

    Periods are built per ``group_fields`` value (自我剖析: the track; 知己分析: track and
    category), and periods of one group never overlap. Each period carries those fields."""
    def group_of(item):
        return tuple(item.get(field) or "" for field in group_fields)

    periods = []
    for group in {group_of(event) for event in events}:
        own = sorted((event for event in events if group_of(event) == group and event.get("startMs")),
                     key=lambda event: event["startMs"])
        fields = dict(zip(group_fields, group))
        track = own[0]["track"] if own else fields.get("track", "")
        starts = [event for event in own if event["kind"] == "start"]
        ends = [event for event in own if event["kind"] == "end"]
        states = [event for event in own if event["kind"] == "state"]
        consumed = set()
        for start in starts:
            # 时期锚定用事件自己的 id（自动事件 id 即证据消息 id 的哈希，manual 事件用编辑行 id）；
            # 归并与手工事件没有顶层 messageId，这里不能直接索引它。
            following = [end for end in ends if end["startMs"] > start["startMs"] and end["id"] not in consumed]
            end = min(following, key=lambda event: event["startMs"]) if following else None
            if end:
                consumed.add(end["id"])
            periods.append({"id": "pe-" + event_id([start["id"], *(end or {}).get("id", "")])[:16],
                            "title": start["title"], "track": track, **fields, "startMs": start["startMs"],
                            "endMs": end["startMs"] if end else None, "precision": start["precision"],
                            "ongoing": end is None, "lastMs": start.get("saidMs"),
                            "evidence": start.get("evidence", [])})
        run = None
        for state in states:
            last = max((row.get("time") or 0 for row in state["evidence"]), default=state.get("saidMs") or 0) or None
            # 连续说同一状态（同地点或同标题）合成一段，中间有开始事件才另起；每说一次就开一段只会切出一串碎片。
            if run is not None and _same_state(run["state"], state) and \
                    not any(run["latest"] < start["startMs"] < state["startMs"] for start in starts):
                seen = {row.get("messageId") for row in run["period"]["evidence"]}
                run["period"]["evidence"] = run["period"]["evidence"] + [
                    row for row in state["evidence"] if row.get("messageId") not in seen]
                run["period"]["lastMs"] = max(run["period"]["lastMs"] or 0, last or 0) or None
                run["latest"] = state["startMs"]
                continue
            if any(period["startMs"] == state["startMs"] and group_of(period) == group for period in periods):
                continue
            period = {"id": "pe-" + event_id([state["id"]])[:16], "title": state["title"], "track": track, **fields,
                      "startMs": state["startMs"], "endMs": None, "precision": state["precision"],
                      "ongoing": True, "lastMs": last, "evidence": state.get("evidence", [])}
            periods.append(period)
            run = {"state": state, "period": period, "latest": state["startMs"]}
    # 同一主线上的时期不重叠：后一段开始时前一段结束；起点晚于终点的丢弃。
    valid = []
    for period in sorted(periods, key=lambda period: (group_of(period), period["startMs"])):
        previous = [item for item in valid if group_of(item) == group_of(period)]
        if previous:
            last = previous[-1]
            if period["endMs"] and period["endMs"] <= period["startMs"]:
                continue
            if last["endMs"] is None and period["startMs"] > last["startMs"]:
                # 无结束时间的时期（进行中状态、未配对的 start）在下一段开始时结束，不再永远敞开。
                last["endMs"] = period["startMs"]
                last["ongoing"] = False
            elif last["endMs"] is not None and period["startMs"] < last["endMs"]:
                last["endMs"] = period["startMs"]
        valid.append(period)
    # 与下一段同一天开始、被截成零长度的时期没有可显示的跨度。
    return [period for period in valid if period["endMs"] is None or period["endMs"] > period["startMs"]]


def _same_state(first, second):
    place = (first.get("place") or "").strip()
    return bool(place) and place == (second.get("place") or "").strip() or _title_key(first) == _title_key(second)



# 人工修改叠加（doc 4.2）：身份由依据消息集合决定，用户字段优先。 ----------------------------------------------------
# ``match_track``: 自我剖析 also requires the edit's track to match; 知己分析 anchors by evidence only,
# because its tracks change with custom-track assignment.
def _anchors(event):
    return {row["messageId"] for row in event.get("evidence", [])}


def _winning_edit(edits, event, match_track=True):
    """The edit row that overlays ``event``: 用户的实际修改优先于裸确认，有字段的编辑赢。"""
    anchors = _anchors(event)
    matches = [row for row in edits if row["state"] in ("confirmed", "edited") and
               len(anchors & set(row["anchors"])) * 2 > max(1, len(anchors)) and
               (not match_track or row["track"] in (None, event["track"]))]
    return next((row for row in matches if row["fields"]), matches[0] if matches else None)


def _winning_period_edit(edits, period):
    anchors = _anchors(period)
    return next((row for row in edits if row["state"] in ("confirmed", "edited") and
                 anchors & set(row["anchors"])), None)


def apply_edits(events, edits, deleted, *, match_track=True, editable=EDITABLE_EVENT_FIELDS):
    """Return ``[(event, edit)]`` with the user's words winning and deleted sets hidden.

    ``edits`` are event edit rows ``{id, anchors, track, state, fields}`` in write order;
    ``deleted`` are ``{anchors: set, track}`` records. Manual events come last."""
    result = []
    for event in events:
        anchors = _anchors(event)
        if any(len(anchors & record["anchors"]) * 2 > max(1, len(anchors)) and
               (not match_track or record["track"] in (None, event["track"])) for record in deleted):
            continue
        edit = _winning_edit(edits, event, match_track)
        if edit is not None and edit["fields"]:
            # 用户的字段跟随依据集合，无论之后确认或取消确认都保留（doc 4.2）。
            result.append(({**event, **edit["fields"], "level": "confirmed"}, edit))
        elif edit is not None:
            result.append(({**event, "level": "confirmed"}, edit))
        else:
            result.append((event, None))
    for edit in edits:
        if edit["state"] == "manual":
            fields = edit["fields"]
            result.append(({**{key: fields.get(key) for key in editable},
                            "id": edit["id"], "kind": fields.get("kind", "point"), "saidMs": None,
                            "reason": fields.get("reason", ""), "evidence": [], "conflicts": [],
                            "level": "confirmed", "manual": True}, edit))
    return result


def apply_period_edits(periods, edits, *, editable=EDITABLE_PERIOD_FIELDS):
    """Period edits: confirm/edit win, deleted rows hide the rebuilt period."""
    deleted = [row for row in edits if row["state"] == "deleted"]
    result = []
    for period in periods:
        anchors = _anchors(period)
        if any(len(anchors & set(row["anchors"])) * 2 > max(1, len(anchors)) for row in deleted):
            continue
        edit = _winning_period_edit(edits, period)
        if edit is not None:
            result.append(({**period, **edit["fields"]}, edit))
        else:
            result.append((period, None))
    for edit in edits:
        if edit["state"] == "manual":
            result.append(({**{key: edit["fields"].get(key) for key in editable},
                            "id": edit["id"], "lastMs": edit["fields"].get("lastMs"),
                            "evidence": [], "manual": True}, edit))
    return result


# 视图与分页 -------------------------------------------------------------------------------------------------------------
def event_row(event, edit, *, editable=EDITABLE_EVENT_FIELDS, extra=()):
    """One event as the page reads it; ``extra`` names further fields copied as they are (e.g. category)."""
    fields = (edit or {}).get("fields", {})
    return {
        "id": event["id"], "title": event["title"], "track": event["track"], "kind": event.get("kind", "point"),
        "status": event["status"], "importance": event["importance"], "startMs": event.get("startMs"),
        "endMs": event.get("endMs"), "precision": event.get("precision", "unknown"),
        "whenText": display_when(event.get("startMs"), event.get("endMs"), event.get("precision", "unknown")),
        "phrase": event.get("phrase", ""), "saidMs": event.get("saidMs"), "place": event.get("place", ""),
        "people": event.get("people", ""), "reason": event.get("reason", ""),
        "level": event.get("level", "possible"), "manual": bool(event.get("manual")),
        "inferred": bool(event.get("inferred")), "details": event.get("details", []),
        "edited": sorted(key for key in fields if key in editable),
        "evidenceCount": len(event.get("evidence", [])), "conflicts": event.get("conflicts", []),
        **{key: event.get(key) for key in extra}}


def period_row(period, edit, *, editable=EDITABLE_PERIOD_FIELDS, extra=()):
    fields = (edit or {}).get("fields", {})
    return {
        "id": period["id"], "title": period["title"], "track": period["track"], "startMs": period.get("startMs"),
        "endMs": period.get("endMs"), "precision": period.get("precision", "unknown"),
        "whenText": display_when(period.get("startMs"), period.get("endMs"), period.get("precision", "unknown")),
        "ongoing": bool(period.get("ongoing")), "lastMs": period.get("lastMs"),
        "level": "confirmed" if edit is not None else "possible", "manual": bool(period.get("manual")),
        "edited": sorted(key for key in fields if key in editable),
        "evidenceCount": len(period.get("evidence", [])), **{key: period.get(key) for key in extra}}


def page_events(events, offset=0, page_bytes=None, budget=EVENT_PAGE_BYTES):
    """``(page, nextOffset)``: events from ``offset`` within ``min(page_bytes, budget)`` UTF-8 bytes of JSON.

    The first event of a page is always included; ``nextOffset`` is None after the last page."""
    page, size, end = [], 0, offset
    for event in events[offset:]:
        size += len(json.dumps(event, ensure_ascii=False).encode("utf-8")) + 1
        if page and size > min(page_bytes or budget, budget):
            break
        page.append(event)
        end += 1
    return page, (end if end < len(events) else None)


def monthly_counts(times_ms):
    """Message times to ``[{"month": "YYYY-MM", "count": n}]`` in month order, for the overview's chat line."""
    counts = {}
    for value in times_ms:
        if isinstance(value, (int, float)) and value > 0:
            month = time.strftime("%Y-%m", time.localtime(value / 1000))
            counts[month] = counts.get(month, 0) + 1
    return [{"month": month, "count": counts[month]} for month in sorted(counts)]


# 用户修改 ----------------------------------------------------------------------------------------------------------------
def edit_label(params, title_of=None):
    """What ``params`` will do, in words, for the undo notice. Read before the edit applies.

    ``title_of(target, identifier)`` looks up the current title of the edited item, or None."""
    target, identifier = params.get("target"), params.get("id")
    fields = params.get("fields") if isinstance(params.get("fields"), dict) else {}
    title = fields.get("title") or (identifier if target == "track" else None)
    if not title and isinstance(identifier, str) and title_of is not None:
        title = title_of(target, identifier)
    verb = EDIT_VERBS.get(params.get("action"), "修改")
    return f"{verb}“{title}”" if title else verb + {"event": "事件", "period": "时期", "track": "主线"}.get(target, "")


def apply_edit(store, params, *, default_track="其他", match_track=True, categories=(),
               event_fields=EDITABLE_EVENT_FIELDS, period_fields=EDITABLE_PERIOD_FIELDS):
    """One user edit, version-checked by the caller; returns the id of the item it touched.

    ``store`` is the plugin's timeline store: ``event(id)``, ``periods()``, ``edits(target)``,
    ``add_edit(target, action, id, anchors=(), fields=None, track=None)``, ``add_deleted(id, anchors, track)``,
    ``drop_deleted(id)``, ``track_names()``, ``set_track(name, description=, color=, visible=)`` and
    ``delete_track(name)``. A manual event or period goes on ``default_track`` unless the fields name one."""
    target, action = params.get("target"), params.get("action")
    identifier = params.get("id")
    if target not in ("event", "period", "track"):
        raise TimelineError("invalid-request", "时间线修改对象不正确")
    if target == "track":
        fields = params.get("fields") or {}
        if action == "visible":
            store.set_track(identifier, visible=bool(fields.get("visible", True)))
        elif action == "add":
            identifier = string_field(fields.get("title"))
            store.set_track(identifier, color=fields.get("color"),
                            description=string_field(fields.get("description"), 60))
        elif action == "edit":
            store.set_track(identifier, color=fields.get("color"),
                            description=string_field(fields["description"], 60) if "description" in fields else None,
                            visible=fields.get("visible") if "visible" in fields else None)
        elif action == "delete":
            store.delete_track(identifier)
        else:
            raise TimelineError("invalid-request", "不支持的主线操作")
        return identifier
    if action not in ("confirm", "unconfirm", "edit", "delete", "restore", "add"):
        raise TimelineError("invalid-request", "不支持的时间线操作")
    if action != "add" and (not isinstance(identifier, str) or not identifier):
        raise TimelineError("invalid-request", "时间线修改对象不正确")
    if target == "event":
        return _edit_event(store, action, identifier, params.get("fields") or {}, default_track, match_track,
                           categories, event_fields)
    return _edit_period(store, action, identifier, params.get("fields") or {}, default_track, period_fields)


def _edit_event(timeline, action, identifier, fields, default_track, match_track, categories, editable):
    def validate(values, manual=False):
        return validate_event_fields(values, manual=manual, track_names=timeline.track_names(),
                                     categories=categories, editable=editable)
    if action == "delete":
        event = timeline.event(identifier)
        if event is None:
            timeline.add_edit("event", "delete", identifier)  # a manual event's edit row
        else:
            timeline.add_deleted(identifier, [row["messageId"] for row in event["evidence"]], event["track"])
        return identifier
    if action == "restore":
        timeline.drop_deleted(identifier)
        timeline.add_edit("event", "restore", identifier)
        return identifier
    if action == "add":
        identifier = "manual-" + uuid.uuid4().hex[:12]
        fields = validate(fields, manual=True)
        if "title" not in fields:
            raise TimelineError("invalid-request", "标题不能为空")
        timeline.add_edit("event", "add", identifier, fields={"track": default_track, "status": "occurred",
                                                               "importance": "general", "startMs": None, "endMs": None,
                                                               "precision": "unknown", **fields})
        return identifier
    event = timeline.event(identifier)
    if event is not None:
        anchors = [row["messageId"] for row in event["evidence"]]
        if action == "edit":
            fields = validate(fields)
            # 逐项修改只带改动的字段：已有修改要保留，并写回正在生效的那一行，否则旧行仍然优先。
            edit = _winning_edit(timeline.edits("event"), event, match_track)
            merged = {**(edit["fields"] if edit else {}), **fields}
            _check_order({**event, **merged})
            timeline.add_edit("event", "edit", edit["id"] if edit else identifier,
                              anchors=edit["anchors"] if edit else anchors, track=event["track"], fields=merged)
        else:
            timeline.add_edit("event", action, identifier, anchors=anchors, track=event["track"])
        return identifier
    # A manual event: update or remove its edit row directly.
    if action == "edit":
        fields = validate(fields, manual=True)
        row = next((row for row in timeline.edits("event") if row["id"] == identifier and row["state"] == "manual"), None)
        if row is None:
            raise TimelineError("not-found", "事件不存在")
        merged = {**row["fields"], **fields}
        _check_order(merged)
        timeline.add_edit("event", "add", identifier, fields=merged)
    else:
        timeline.add_edit("event", action, identifier)
    return identifier


def _edit_period(timeline, action, identifier, fields, default_track, editable):
    def validate(values, manual=False):
        return validate_period_fields(values, manual=manual, track_names=timeline.track_names(), editable=editable)
    if action == "add":
        identifier = "manual-" + uuid.uuid4().hex[:12]
        fields = validate(fields, manual=True)
        if "title" not in fields:
            raise TimelineError("invalid-request", "名称不能为空")
        timeline.add_edit("period", "add", identifier, fields={"track": default_track, "precision": "unknown", **fields})
        return identifier
    period = next((period for period in timeline.periods() if period["id"] == identifier), None)
    anchors = [row["messageId"] for row in period["evidence"]] if period else []
    if action != "edit":
        timeline.add_edit("period", action, identifier, anchors=anchors)
        return identifier
    fields = validate(fields)
    edits = timeline.edits("period")
    if period is None:
        row = next((row for row in edits if row["id"] == identifier and row["state"] == "manual"), None)
        if row is None:
            raise TimelineError("not-found", "时期不存在")
        merged = {**row["fields"], **fields}
        _check_order(merged)
        timeline.add_edit("period", "add", identifier, fields=merged)
        return identifier
    edit = _winning_period_edit(edits, period)
    merged = {**(edit["fields"] if edit else {}), **fields}
    _check_order({**period, **merged})
    timeline.add_edit("period", "edit", edit["id"] if edit else identifier,
                      anchors=edit["anchors"] if edit else anchors, fields=merged)
    return identifier


def string_field(value, limit=40):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise TimelineError("invalid-request", "字段长度超出限制")
    return value.strip()


def _with_when(fields):
    """Expand a typed ``when`` into the stored time fields; empty clears the time."""
    if "when" not in fields:
        return fields
    if not {"startMs", "endMs", "precision"}.isdisjoint(fields):
        raise TimelineError("invalid-request", "when 不能和 startMs、endMs、precision 同时使用")
    text, fields = fields["when"], {key: value for key, value in fields.items() if key != "when"}
    if text is not None and not isinstance(text, str):
        raise TimelineError("invalid-request", "时间不正确")
    if not (text or "").strip():
        return {**fields, "startMs": None, "endMs": None, "precision": "unknown"}
    parsed = parse_user_when(text)
    if parsed is None:
        raise TimelineError("invalid-request", "时间可写成 2024、2024.05、2024-05-03 或 2024.03-2024.06")
    return {**fields, "startMs": parsed[0], "endMs": parsed[1], "precision": parsed[2]}


def _check_order(item):
    if item.get("startMs") and item.get("endMs") and item["endMs"] < item["startMs"]:
        raise TimelineError("invalid-request", "结束时间不能早于开始时间")


def validate_event_fields(fields, manual=False, track_names=(), *, categories=(), editable=EDITABLE_EVENT_FIELDS):
    """``editable`` may add ``category`` or ``note``; a ``category`` must be one of ``categories`` when given."""
    if not isinstance(fields, dict) or not fields:
        raise TimelineError("invalid-request", "没有要修改的字段")
    fields = _with_when(fields)
    unknown = set(fields) - set(editable) - ({"kind"} if manual else set())
    if unknown:
        raise TimelineError("invalid-request", "字段不正确")
    result = {}
    for key, value in fields.items():
        if key == "title":
            result[key] = string_field(value)
            if not result[key]:
                raise TimelineError("invalid-request", "标题不能为空")
        elif key in _STRING_FIELDS:
            result[key] = string_field(value, 60)
        elif key in ("startMs", "endMs"):
            if value is not None and (type(value) is not int or value < 0):
                raise TimelineError("invalid-request", "时间不正确")
            result[key] = value
        else:
            raise TimelineError("invalid-request", "字段不正确")
    if result.get("track") and track_names and result["track"] not in track_names:
        raise TimelineError("invalid-request", "主线不存在")
    if result.get("status") and result["status"] not in STATUSES:
        raise TimelineError("invalid-request", "状态不正确")
    if result.get("importance") and result["importance"] not in IMPORTANCE:
        raise TimelineError("invalid-request", "重要程度不正确")
    if result.get("precision") and result["precision"] not in PRECISIONS:
        raise TimelineError("invalid-request", "精度不正确")
    if result.get("category") and categories and result["category"] not in categories:
        raise TimelineError("invalid-request", "分类不正确")
    _check_order(result)
    return result


def validate_period_fields(fields, manual=False, track_names=(), *, editable=EDITABLE_PERIOD_FIELDS):
    if not isinstance(fields, dict) or not fields:
        raise TimelineError("invalid-request", "没有要修改的字段")
    fields = _with_when(fields)
    unknown = set(fields) - set(editable) - {"lastMs"}
    if unknown:
        raise TimelineError("invalid-request", "字段不正确")
    result = {}
    for key, value in fields.items():
        if key == "title":
            result[key] = string_field(value)
            if not result[key]:
                raise TimelineError("invalid-request", "名称不能为空")
        elif key in ("track", "precision", "category"):
            result[key] = string_field(value, 60)
        elif key in ("startMs", "endMs", "lastMs"):
            if value is not None and (type(value) is not int or value < 0):
                raise TimelineError("invalid-request", "时间不正确")
            result[key] = value
        elif key == "ongoing":
            result[key] = bool(value)
        else:
            raise TimelineError("invalid-request", "字段不正确")
    if result.get("track") and track_names and result["track"] not in track_names:
        raise TimelineError("invalid-request", "主线不存在")
    if result.get("precision") and result["precision"] not in PRECISIONS:
        raise TimelineError("invalid-request", "精度不正确")
    _check_order(result)
    return result


# 撤销日志 ----------------------------------------------------------------------------------------------------------------
class UndoJournal:
    """Before-images of the user-edit tables, kept in the plugin's own undo table.

    ``tracked`` maps each edit table to its key column. ``scope`` (e.g. ``{"user": friend}``)
    filters the undo table and every tracked table and is written into new undo rows. With
    ``actors`` the undo table has ``actor`` and ``agent`` columns and each source keeps its own
    stack of ``limit`` steps. With ``seq_column`` each tracked row records the seq of the last
    entry that changed it, and undoing an entry whose rows another source changed since is
    skipped and dropped instead. ``on_change`` runs inside the undo transaction (a version bump).

    Undo table columns: ``seq INTEGER PRIMARY KEY AUTOINCREMENT, label, changes, updated_ms``,
    plus the scope columns, plus ``actor, agent`` with ``actors``. Without scope and actors the
    journal behaves exactly like 自我剖析's original shared journal."""

    def __init__(self, db, lock, table, tracked, *, scope=None, actors=False, seq_column=None, on_change=None):
        self.db, self.lock, self.table, self.tracked = db, lock, table, dict(tracked)
        self.scope, self.actors, self.seq_column, self.on_change = dict(scope or {}), actors, seq_column, on_change

    def _rows(self, query, values=()):
        with self.lock:
            cursor = self.db.execute(query, values)
            names = [column[0] for column in cursor.description or ()]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _where(self, extra=()):
        parts = [f"{column}=?" for column in self.scope] + [f"{column}=?" for column, _ in extra]
        values = [*self.scope.values(), *(value for _, value in extra)]
        return (" WHERE " + " AND ".join(parts) if parts else ""), values

    def snapshot(self):
        """Every row a user edit can touch, keyed so ``push`` can diff before and after."""
        where, values = self._where()
        return {table: {row[key]: row for row in self._rows(f"SELECT * FROM {table}{where}", values)}
                for table, key in self.tracked.items()}

    @staticmethod
    def changes(before, after):
        """Rows that differ, as ``{table: [[key, before_row_or_None], ...]}``; None when nothing changed."""
        changes = {table: [[key, before[table].get(key)] for key in before[table].keys() | after[table].keys()
                           if before[table].get(key) != after[table].get(key)] for table in before}
        return changes if any(changes.values()) else None

    def push(self, label, before, *, actor="user", agent="", limit=UNDO_LIMIT, now_ms=None):
        """Record the before-image of whatever changed since ``before``; returns the new seq, or None for a no-op."""
        after = self.snapshot()
        changes = self.changes(before, after)
        if changes is None:
            return None
        if self.seq_column:
            changes = {table: [[key, row, key in after[table]] for key, row in rows] for table, rows in changes.items()}
        now = int(time.time() * 1000) if now_ms is None else now_ms
        columns = {**self.scope, **({"actor": actor, "agent": agent or ""} if self.actors else {}),
                   "label": label, "changes": json.dumps(changes, ensure_ascii=False), "updated_ms": now}
        with self.lock, self.db:
            seq = self.db.execute(f"INSERT INTO {self.table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                                  tuple(columns.values())).lastrowid
            if not self.scope and not self.actors:
                self.db.execute(f"DELETE FROM {self.table} WHERE seq<=(SELECT max(seq) FROM {self.table})-?", (limit,))
            else:
                where, values = self._where((("actor", actor),) if self.actors else ())
                self.db.execute(f"DELETE FROM {self.table}{where} AND seq NOT IN (SELECT seq FROM {self.table}{where} "
                                "ORDER BY seq DESC LIMIT ?)", (*values, *values, limit))
            if self.seq_column:
                for table, rows in changes.items():
                    for key, _row, exists in rows:
                        if exists:
                            where, values = self._where(((self.tracked[table], key),))
                            self.db.execute(f"UPDATE {table} SET {self.seq_column}=?{where}", (seq, *values))
        return seq

    def _current(self, table, key):
        where, values = self._where(((self.tracked[table], key),))
        rows = self._rows(f"SELECT * FROM {table}{where}", values)
        return rows[0] if rows else None

    def undo(self, actor=None):
        """Restore the newest entry (of ``actor``'s stack with ``actors``).

        Returns None when there is nothing to undo, else ``{seq, label, actor, agent, undone}``;
        ``undone`` is False when a later change by another source made the entry stale (it is dropped)."""
        with self.lock, self.db:
            where, values = self._where((("actor", actor),) if self.actors and actor is not None else ())
            rows = self._rows(f"SELECT * FROM {self.table}{where} ORDER BY seq DESC LIMIT 1", values)
            if not rows:
                return None
            entry, changes = rows[0], json.loads(rows[0]["changes"])
            result = {"seq": entry["seq"], "label": entry["label"], "actor": entry.get("actor", "user"),
                      "agent": entry.get("agent", ""), "undone": True}
            if self.seq_column:
                for table, items in changes.items():
                    for key, _row, existed in items:
                        current = self._current(table, key)
                        if (current is None) == existed or current is not None and current.get(self.seq_column) != entry["seq"]:
                            result["undone"] = False
            if result["undone"]:
                for table, items in changes.items():
                    for item in items:
                        identifier, row = item[0], item[1]
                        where, values = self._where(((self.tracked[table], identifier),))
                        self.db.execute(f"DELETE FROM {table}{where}", values)
                        if row is not None:
                            marks = ",".join("?" for _ in row)
                            self.db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({marks})", tuple(row.values()))
            self.db.execute(f"DELETE FROM {self.table} WHERE seq=?", (entry["seq"],))
            if result["undone"] and self.on_change is not None:
                self.on_change()
            return result

    def recent(self, actor=None, limit=5):
        """``(count, [{seq, label, agent, updatedMs}])`` of the newest entries, for the page's undo notice."""
        where, values = self._where((("actor", actor),) if self.actors and actor is not None else ())
        total = self._rows(f"SELECT count(*) total FROM {self.table}{where}", values)[0]["total"]
        rows = self._rows(f"SELECT * FROM {self.table}{where} ORDER BY seq DESC LIMIT ?", (*values, limit))
        return total, [{"seq": row["seq"], "label": row["label"], "agent": row.get("agent", ""),
                        "updatedMs": row["updated_ms"]} for row in rows]
