"""模拟对话: the user writes as themself and the model answers as this friend.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The mirror of ``kyf.self-analysis``'s 聊天试用 (``self_trial.Trial`` run as a
``self_model.Job``): one turn is one interactive Core task. Each turn reads the
friend's encoded profile and retrieved reply samples (``friend_sim_encode``), rolls
whether the friend answers at all from their real reply rate (a "no" skips the model
call), caps the bubble count from a sample, and makes one model call that picks a
stance before writing the messages.

Isolation (docs/friend-simulation-requirements.md §5): sessions live only in this
process's memory, keyed by (account, user). Nothing simulated is written to any
database, memory, analysis or advisor context, and there is no send path. The stance
stays in the backend (log and evaluation requests only).
"""
from __future__ import annotations

import collections
import importlib
import json
import random
import re
import threading
import time
import uuid

import friend_chat
from contracts import AdvisorError, friendly_error
from kyf_plugin import Cancelled, ExecutionRevoked

MAX_TURNS = 20            # simulated turns given to the model
RECENT_MESSAGES = 30      # real messages from the mirror, the "current state" of the relationship
MAX_SAMPLES = 8
MAX_SEND = 6              # messages in one of my turns
TURN_CHARS = 1000
SESSION_TURNS = 200       # a session keeps at most this many turns in memory
MAX_MESSAGES = 6
MESSAGE_CHARS = 500
EXAMPLE_CHARS = 600
RECENT_CHARS = 300
PROFILE_CHARS = 3600
OUTPUT_TOKENS = 8192
NO_SAMPLE_COUNTS = (1, 1, 2)
PROFILE_TTL = 30.0
JOB_HISTORY = 64
ATTEMPTS = 2
RETRY_CODES = ("invalid-output", "empty-response", "network", "network-error", "timeout", "rate-limit",
               "core-unavailable", "gateway-start", "gateway-unavailable", "upstream-unavailable")
# 回应方式沿用聊天分析的标签（去掉“无法判断”），回测可以直接对比。
STANCES = tuple(label for label in friend_chat.RESPONSES if label != "无法判断")
MEDIA = "[媒体或非文字消息]"

SYSTEM = """你就是用户的这位微信好友本人，在私聊里回复用户（“我”）。你不是 AI 助手，也不是在替谁写建议。
输入 JSON：
- profile：这位好友的档案，按优先级分层。第 0 层硬规则不可违背；其余各层依次是表达方式、回应倾向、ta 是什么样的人（标签）、我们的经历、性格与三观、关系记忆。其中标为“最可信”的是用户自己写的，其余性格与三观结论只是推测；自述的 MBTI 等与表达方式、回应倾向的数据冲突时，以数据为准。标签和我们的经历来自过去的聊天，近况以 recent 里最近的聊天原文为准。
- recent：你们真实私聊里最近的原文（from=me 是用户，from=friend 是你），这是你们现在的真实状态，这次对话接着它往下聊。
- examples：你过去回复“我”的真实样例（time=当时日期，input=“我”当时连发的消息，reply=你当时的回复，每一行是单独发出的一条），用来模仿你的措辞、长短、语气词、标点、表情和分条习惯。
- conversation：这次模拟里的对话（from=me 是用户，from=friend 是你；noReply=true 表示你那一轮没有回）。其中 from=friend 的早先回复是这次模拟生成的，不是事实。
- stances：可选的回应方式；count：这次最多发出的消息条数。
第 0 层硬规则：不说你现实中不会说的话；不突然变得更温柔、更包容、更会说话；没有依据时不主动道歉、表白或承诺。简短回应、敷衍、转移话题、婉拒、反问都是真实的回应，按档案里的回应倾向来，不要美化。遵守档案里的“从不”清单。
先定态度再写话：先根据回应倾向（尤其是“我”这一轮的意图对应的分布）和 recent 里的近况，从 stances 里选出这一轮的 stance；再按表达方式写出 messages，只回应 conversation 里“我”最后发的消息。
最多发 count 条，这是按你相近情境的实际条数定的上限，不要超过；话说完就停，不必发满，不要为凑条数添内容。
examples 里的人名、地点、安排和承诺属于当时，不是当前事实，不要照搬；没有依据的事不要编，可以像你平时那样含糊带过或反问。
不解释，不提资料、档案、分析、样例或本说明。只输出 JSON：{"stance":"…","messages":["…"]}，不输出代码围栏。"""


# -- helpers copied from kyf.self-analysis (plugins cannot import each other) -----------------------------------------

def fit(messages, count):
    """Copied from kyf.self-analysis/backend/self_trial.py ``fit``, 2026-11-05.

    模型多写的并进最后一条：不丢内容，也不超过这次的条数；写少了就按少的发。"""
    if len(messages) <= count:
        return messages
    return messages[:count - 1] + [" ".join(messages[count - 1:])[:MESSAGE_CHARS]]


def json_object(text):
    """Copied from kyf.self-analysis/backend/self_model.py ``json_object``, 2026-11-05.

    The first JSON object in a model reply, tolerating code fences and stray prose."""
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
        raise AdvisorError("invalid-output", "模型返回的内容无法使用")
    return value


def parse(text):
    data = json_object(text)
    messages = data.get("messages")
    if isinstance(messages, str):
        messages = [messages]
    result = [item.strip()[:MESSAGE_CHARS] for item in messages if isinstance(item, str) and item.strip()] \
        if isinstance(messages, list) else []
    if not result:
        raise AdvisorError("invalid-output", "模型返回的内容无法使用")
    stance = data.get("stance") if data.get("stance") in STANCES else None
    return {"stance": stance, "messages": result[:MAX_MESSAGES]}


def _day(ms):
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if isinstance(ms, int) and ms > 0 else ""


def _clock(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if isinstance(ms, int) and ms > 0 else ""


def my_messages(params):
    messages = params.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_SEND:
        raise AdvisorError("invalid-request", f"每轮可以发 1 到 {MAX_SEND} 条消息")
    result = []
    for item in messages:
        if not isinstance(item, str) or not item.strip() or len(item) > TURN_CHARS:
            raise AdvisorError("invalid-request", f"每条消息须在 {TURN_CHARS} 字以内且不能为空")
        result.append(item.strip())
    return result


def before_time(params):
    """Evaluation-only leak guard (§7): samples, profile and recent messages stop before this time."""
    value = params.get("beforeTimeMs")
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2 ** 53:
        raise AdvisorError("invalid-request", "beforeTimeMs 不正确")
    return value


def recent_context(database, user, before_ms=None, limit=RECENT_MESSAGES):
    """The latest real messages of this private chat, text only (media as a placeholder)."""
    context = database.context(user)
    if not context:
        return []
    sql = ("SELECT msg_id,side,kind,text,time_ms,raw FROM context_messages WHERE user=? AND seq<=? "
           "AND side IN ('self','other')")
    args = [user, context["read_count"]]
    if before_ms is not None:
        sql += " AND time_ms<?"
        args.append(before_ms)
    rows = database._rows(sql + " ORDER BY seq DESC LIMIT ?", (*args, limit))
    return [{"side": "me" if row["side"] == "self" else "friend",
             "text": (friend_chat.readable_text(row) or MEDIA)[:RECENT_CHARS], "timeMs": row["time_ms"]}
            for row in reversed(rows)]


def trailing_count(turns):
    """How many messages "I" sent in my last run of turns (one-to-many / many-to-many)."""
    count = 0
    for turn in reversed(turns):
        if turn["side"] != "me":
            break
        count += len(turn["messages"])
    return max(1, count)


def _turn_id():
    return uuid.uuid4().hex[:12]


def public_turn(turn):
    if turn["side"] == "me":
        return {"id": turn["id"], "side": "me", "messages": list(turn["messages"])}
    return {"id": turn["id"], "side": "friend", "messages": list(turn["messages"]), "noReply": turn["noReply"],
            "samples": [dict(sample) for sample in turn["samples"]]}


class Session:
    def __init__(self):
        self.turns = []
        self.pending = None
        self.epoch = 0
        self.profile = None  # (key, monotonic stamp, profile)


class Turn:
    """One simulated friend reply, run as an interactive Core task (mirror of self_trial.Trial)."""

    def __init__(self, service, account, user, database, store, session, base, before_ms):
        self.service, self.plugin = service, service.plugin
        self.account, self.user = account, user
        self.database, self.store = database, store
        self.session, self.epoch = session, session.epoch
        self.base = base            # session turns this reply answers (a rerolled friend turn excluded)
        self.before_ms = before_ms
        self.replaces = None        # the friend turn a reroll replaces
        self.mine = None            # my turn a send added; dropped again if no reply comes
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.context = None
        self.data = {"state": "queued", "turn": None, "error": None}
        self.debug = {}

    # -- lifecycle ------------------------------------------------------------------------------------------------
    def start(self):
        _task, context = self.plugin.start_task(self.account, None, kind="interactive", title="模拟对话",
                                                params={"kind": "friend-sim"}, handler=self.run)
        with self.lock:
            self.context = self.context or context
        if self.stopped.is_set():
            self._stop_task()

    def cancel(self):
        self.stopped.set()
        self._stop_task()
        with self.lock:
            if self.data["state"] in ("queued", "running"):
                self.data["state"] = "cancelled"

    def _stop_task(self):
        context = self.context
        if context is None:
            return
        try:
            context.stop()
        except Exception:
            context.cancelled.set()

    def check(self):
        if self.stopped.is_set():
            raise Cancelled()
        if self.context is not None:
            self.context.check()

    @property
    def open(self):
        return self.data["state"] in ("queued", "running")

    def view(self, evaluation=False):
        with self.lock:
            result = {"state": self.data["state"], "turn": self.data["turn"] and public_turn(self.data["turn"]),
                      "error": self.data["error"]}
        if evaluation:
            # Evaluation requests (beforeTimeMs) also get what the page never shows.
            result["debug"] = dict(self.debug)
        return result

    def _set(self, **changes):
        with self.lock:
            if self.data["state"] == "cancelled" and changes.get("state") not in (None, "cancelled"):
                return False
            self.data.update(changes)
            return True

    def run(self, context):
        with self.lock:
            self.context = context
        try:
            self.check()
            self._set(state="running")
            turn = self.work()
            self.check()
            if not self.service.finish(self, turn):
                raise Cancelled()
            return "done"
        except (Cancelled, ExecutionRevoked):
            self._set(state="cancelled")
            self.service.release(self)
            raise Cancelled() from None
        except Exception as exc:
            if self.stopped.is_set() or context.cancelled.is_set():
                self._set(state="cancelled")
                self.service.release(self)
                raise Cancelled() from None
            message = friendly_error(exc)
            self._set(state="failed", error=message)
            self.service.release(self)
            self._log(f"friend-sim failed: {getattr(exc, 'code', None) or type(exc).__name__}", "error")
            context.finish("failed", {"code": getattr(exc, "code", None) or "friend-sim-failed", "message": message})
            return "failed"

    def _log(self, message, level="info"):
        log = getattr(self.plugin, "log", None)
        if log is not None:
            try:
                log(message, level)
            except Exception:
                pass

    # -- one turn -------------------------------------------------------------------------------------------------
    def work(self):
        encoder = self.service.encoder()
        turns = [{"side": turn["side"], "messages": list(turn["messages"])} for turn in self.base[-MAX_TURNS:]]
        recent = recent_context(self.database, self.user, self.before_ms)
        self.check()
        profile = self.service.profile(self.session, self.database, self.store, self.user, self.before_ms)
        self.check()
        samples = encoder.samples(self.database, self.user, turns, limit=MAX_SAMPLES, before_ms=self.before_ms)
        samples = list(samples or [])[:MAX_SAMPLES]
        self.check()
        probability = encoder.reply_probability(profile)
        rng = self.service.rng
        replied = probability is None or rng.random() < probability
        self.debug.update(probability=probability, replied=replied, samples=len(samples),
                          omittedLayers=list(profile.get("omittedLayers") or []))
        public_samples = [self.service.public_sample(self.database, self.user, sample) for sample in samples]
        if not replied:
            self._log("friend-sim turn: no reply")
            return {"id": _turn_id(), "side": "friend", "messages": [], "noReply": True, "samples": public_samples}
        if samples:
            # 从相近样例里抽一条，以它当时的条数为上限（self_trial.bubble_count 的做法）。
            count = min(MAX_MESSAGES, max(1, len(rng.choice(samples).get("reply") or [""])))
        else:
            count = encoder.bubble_count(profile, trailing_count(turns), rng)
            count = min(MAX_MESSAGES, max(1, count if type(count) is int else rng.choice(NO_SAMPLE_COUNTS)))
        self.debug["count"] = count
        result = self._generate(encoder, profile, recent, samples, turns, count)
        self.debug["stance"] = result["stance"]
        self._log(f"friend-sim turn: stance={result['stance'] or '-'} count={count}")
        return {"id": _turn_id(), "side": "friend", "messages": fit(result["messages"], count), "noReply": False,
                "samples": public_samples}

    def _generate(self, encoder, profile, recent, samples, turns, count):
        service = self.plugin.models.service()
        window = service.get("contextTokens") if isinstance(service, dict) else None
        window = window if type(window) is int and window >= 4096 else 32768
        output = min(OUTPUT_TOKENS, max(1024, window // 3))
        # One character per token is a conservative bound for Chinese text and JSON.
        room = window - output - len(SYSTEM) - 1000
        conversation = [{"from": turn["side"], **({"noReply": True} if turn["side"] == "friend" and not turn["messages"]
                                                   else {"messages": turn["messages"]})} for turn in turns]
        history = [{"from": item["side"], "time": _clock(item["timeMs"]), "text": item["text"]} for item in recent]
        text = encoder.prefix(profile, budget=PROFILE_CHARS)

        def render(rows):
            return json.dumps({"profile": text, "recent": history, "conversation": conversation,
                               "stances": list(STANCES), "count": count,
                               "examples": [{"time": _day(row.get("timeMs")),
                                             "input": "\n".join(row.get("input") or [])[-EXAMPLE_CHARS:],
                                             "reply": "\n".join(row.get("reply") or [])[:EXAMPLE_CHARS]}
                                            for row in rows]}, ensure_ascii=False)
        rows = list(samples)
        while rows and len(render(rows)) > room:
            rows.pop()
        while history and len(render(rows)) > room:
            history.pop(0)
        request = {"system": SYSTEM, "prompt": render(rows), "jsonMode": True, "maxOutputTokens": output}
        for attempt in range(ATTEMPTS):
            self.check()
            try:
                generated = self.plugin.models.generate(self.context, request, step="friend-sim")
                return parse(generated.get("text") if isinstance(generated, dict) else None)
            except (Cancelled, ExecutionRevoked):
                raise
            except Exception as exc:
                if self.stopped.is_set() or attempt + 1 >= ATTEMPTS or getattr(exc, "code", None) not in RETRY_CODES:
                    raise


class Simulations:
    """In-memory 模拟对话 sessions keyed by (account, user); nothing here touches a database write."""

    def __init__(self, plugin, scope, encoder=None, rng=None):
        self.plugin = plugin
        self.scope = scope            # main.pages._scope: (account, user, fingerprint, database, store)
        self._encoder = encoder
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.sessions = {}
        self.jobs = collections.OrderedDict()
        self.closed = False

    def encoder(self):
        if self._encoder is None:
            self._encoder = importlib.import_module("friend_sim_encode")
        return self._encoder

    def _session(self, account, user):
        with self.lock:
            return self.sessions.setdefault((account, user), Session())

    def profile(self, session, database, store, user, before_ms=None):
        """The encoded profile, reused for a short while within one session (encoding reads several stores)."""
        context = database.context(user) or {}
        key = (context.get("source_id"), context.get("read_count"), before_ms)
        cached = session.profile
        if cached and cached[0] == key and time.monotonic() - cached[1] < PROFILE_TTL:
            return cached[2]
        encoder = self.encoder()
        encoder.refresh(database, user)
        profile = encoder.encode(database, store, user, before_ms=before_ms)
        session.profile = (key, time.monotonic(), profile)
        return profile

    def reply_context(self, account, user, params):
        """对方画像 text for 分析与建议 (kyf.message-insights); the same encoded profile, read-only, no model call.

        Added 2026-11-09: reply suggestions for a round of the friend's messages also see who the friend is.
        """
        budget = params.get("budgetChars")
        budget = budget if type(budget) is int and 200 <= budget <= 6000 else 2400
        _account, _user, _fp, database, store = self.scope(account, user)
        profile = self.profile(self._session(account, user), database, store, user)
        encoder = self.encoder()
        if params.get("audience") == "advisor":
            # Modified 2026-11-09: suggestions write as the user. ta is described, not played, and the
            # user's own way of talking to ta comes along as ``mine``.
            text = encoder.advisor_prefix(profile, budget) if profile.get("layers") else ""
            context = database.context(user)
            rows = database.context_messages(user, context["read_count"]) if context else []
            mine = encoder.advisor_mine(rows, user, min(1200, budget))
            return {"available": bool(text.strip() or mine), "text": text, "mine": mine,
                    "analysed": bool(profile.get("analysed")), "warning": profile.get("warning")}
        text = encoder.prefix(profile, budget) if profile.get("layers") else ""
        return {"available": bool(text.strip()), "text": text, "analysed": bool(profile.get("analysed")),
                "warning": profile.get("warning")}

    def forget_profile(self, account, user):
        """Drop the cached profile after a tag or timeline edit (by the user or an Agent)."""
        with self.lock:
            session = self.sessions.get((account, user))
            if session is not None:
                session.profile = None

    def public_sample(self, database, user, sample):
        result = {"input": list(sample.get("input") or []), "reply": list(sample.get("reply") or []),
                  "timeMs": sample.get("timeMs")}
        seq = sample.get("seq")
        if isinstance(sample.get("messageId"), str) and sample["messageId"]:
            result["id"] = sample["messageId"]
        elif type(seq) is int:
            # page.message opens a message by its id; the sample's reply starts at ``seq``.
            row = database._row("SELECT msg_id FROM context_messages WHERE user=? AND seq=?", (user, seq))
            if row is not None:
                result["id"] = row["msg_id"]
        return result

    # -- RPC ------------------------------------------------------------------------------------------------------
    def view(self, account, user):
        _account, _user, _fp, database, store = self.scope(account, user)
        session = self._session(account, user)
        profile = self.profile(session, database, store, user)
        context = database.context(user)
        with self.lock:
            turns = [public_turn(turn) for turn in session.turns]
            pending = session.pending.id if session.pending is not None and session.pending.open else None
        # Only an empty service list means no model is saved. A gateway that is down or erroring raises here;
        # that is "unavailable" (Core's own status bar explains it), never "not configured".
        try:
            model = "ready" if self.plugin.models.services() else "missing"
        except Exception:
            model = "unavailable"
        return {"account": account, "user": user, "analysed": bool(profile.get("analysed")),
                "warning": profile.get("warning"),
                "profile": {"layers": profile.get("layers") or [],
                            **({"range": profile["range"]} if isinstance(profile.get("range"), dict) else {})},
                "context": recent_context(database, user), "turns": turns, "pending": pending,
                "model": model, "configured": model != "missing", "historyReady": bool(context and context["state"] == "ready")}

    def send(self, account, user, params):
        messages = my_messages(params)
        before_ms = before_time(params)
        _account, _user, _fp, database, store = self.scope(account, user)
        session = self._session(account, user)
        with self.lock:
            self._idle(session)
            mine = {"id": _turn_id(), "side": "me", "messages": messages}
            session.turns.append(mine)
            del session.turns[:-SESSION_TURNS]
            job = self._job(account, user, database, store, session, list(session.turns), before_ms)
            job.mine = mine["id"]
        return self._start(session, job)

    def reroll(self, account, user, params):
        before_ms = before_time(params)
        _account, _user, _fp, database, store = self.scope(account, user)
        session = self._session(account, user)
        with self.lock:
            self._idle(session)
            base = list(session.turns)
            if base and base[-1]["side"] == "friend":
                base.pop()
            if not base or base[-1]["side"] != "me":
                raise AdvisorError("invalid-request", "还没有可以重新生成的回复")
            job = self._job(account, user, database, store, session, base, before_ms)
            job.replaces = session.turns[-1]["id"] if session.turns[-1]["side"] == "friend" else None
        return self._start(session, job)

    def status(self, account, user, request_id):
        with self.lock:
            job = self.jobs.get(request_id) if isinstance(request_id, str) else None
        if job is None or (job.account, job.user) != (account, user):
            raise AdvisorError("not-found", "这一轮模拟已不存在，请重新发送")
        return job.view(evaluation=job.before_ms is not None)

    def cancel(self, account, user, request_id):
        with self.lock:
            job = self.jobs.get(request_id) if isinstance(request_id, str) else None
        if job is not None and (job.account, job.user) == (account, user):
            job.cancel()
            self.release(job)
        return {"ok": True}

    def reset(self, account, user):
        with self.lock:
            session = self.sessions.pop((account, user), None)
            job = session.pending if session is not None else None
            if session is not None:
                session.epoch += 1
                session.turns = []
                session.pending = None
        if job is not None:
            job.cancel()
        return {"ok": True}

    def close(self):
        with self.lock:
            self.closed = True
            jobs = [job for job in self.jobs.values() if job.open]
            self.sessions.clear()
        for job in jobs:
            job.cancel()

    # -- internals ------------------------------------------------------------------------------------------------
    def _idle(self, session):
        if self.closed:
            raise AdvisorError("stopping", "插件正在退出")
        if session.pending is not None and session.pending.open:
            raise AdvisorError("request-conflict", "上一轮还在生成，请稍候或先停止")

    def _job(self, account, user, database, store, session, base, before_ms):
        job = Turn(self, account, user, database, store, session, base, before_ms)
        session.pending = job
        self.jobs[job.id] = job
        while len(self.jobs) > JOB_HISTORY:
            oldest = next(iter(self.jobs))
            if self.jobs[oldest].open:
                break
            self.jobs.pop(oldest)
        return job

    def _start(self, session, job):
        try:
            job.start()
        except Exception as exc:
            job.stopped.set()
            self.release(job)
            with self.lock:
                self.jobs.pop(job.id, None)
            raise AdvisorError(getattr(exc, "code", None) or "friend-sim-failed", friendly_error(exc)) from None
        return {"requestId": job.id}

    def finish(self, job, turn):
        """Put a finished reply into its session, unless the session was reset or the turn cancelled meanwhile.

        The job turns "done" under its own lock here, so a concurrent cancel either wins (nothing is stored)
        or finds the reply already stored and leaves it."""
        with self.lock:
            session = job.session
            if session.epoch != job.epoch or self.sessions.get((job.account, job.user)) is not session:
                return False
            with job.lock:
                if job.stopped.is_set() or job.data["state"] not in ("queued", "running"):
                    return False
                job.data.update(state="done", turn=turn)
            if job.replaces is not None:
                session.turns = [item for item in session.turns if item["id"] != job.replaces]
            # A reply answers the turns it was given; nothing else can be appended while it is pending.
            session.turns.append(turn)
            del session.turns[:-SESSION_TURNS]
            if session.pending is job:
                session.pending = None
            return True

    def release(self, job):
        """A job that ends without a reply: free the session and drop the turn of mine it was answering, so the
        page gives those lines back to the input (a reroll keeps the friend turn it would have replaced)."""
        with self.lock:
            session = job.session
            if session.pending is job:
                session.pending = None
            with job.lock:
                done = job.data["state"] == "done"
            if job.mine is not None and not done and session.epoch == job.epoch:
                session.turns = [item for item in session.turns if item["id"] != job.mine]
