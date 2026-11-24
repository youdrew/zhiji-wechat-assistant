"""One 分析与建议 job: freeze the context, ask the model, and keep only results that still match.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (MessageAnalysis._work) into kyf.message-insights, 2026-10-13. Each job runs
as a Core task, so its model calls carry an execution token and stop when the task stops.
Modified 2026-11-09: a job covers one round, the friend's messages since the user's last reply,
and saves under the round's last message; the reply also sees 本人画像, 对方画像 (described, not
played) and the user's own way of talking to this friend. 回复分析深度: basic writes directly,
standard lets the model think first, deep also has 自我剖析 research how the user would answer
(their timeline and past replies) while the explanation is written.
"""
import hashlib
import json
import threading
import time

from kyf_plugin import Cancelled, CoreError
from media import MediaObservations, attachments
from prompts import SYSTEM, InsightError, budget, correction_instruction, material, message_hash, parse_reply_candidates, \
    parse_result, row_from_view, safety_margin, suggestions_system
from sources import structured_sources
from personalization import APPLICABILITY, payload as expression_payload, query_from_rows as expression_query, \
    retrieve as expression_context, still_current
from prompts import with_fields

CONTEXT_ROWS = 100
QUERY_CONTEXT = 4
# Room an attached image takes in the model context, counted in prompt characters.
IMAGE_CHARS = 1500
RESEARCH_TURNS = 20
RESEARCH_TURN_CHARS = 1000
RESEARCH_SECONDS = 240.0
RESEARCH_POLL = 1.5
RESEARCH_EXAMPLES = 4
REPLY_DEPTHS = ("basic", "standard", "deep")
DEFAULT_REPLY_DEPTH = "standard"
CORE_STOPPING = ("execution-revoked", "account-changed", "stopping", "cancelled")
ROUND_MAX = 30
PORTRAIT_CHARS = 2400
READY_WAIT = 600.0
STOPPING = ("stopping", "account-changed", "cancelled")
RETRY_DELAY = 1.0
TRANSPORT_RETRIES = 2
FORMAT_CORRECTIONS = 1
RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)


def generation_error(exc):
    """User-facing categories only; provider bodies may contain private request data."""
    code, status = getattr(exc, "code", None), getattr(exc, "http_status", None)
    if code in ("invalid-output", "invalid-suggestions", "empty-response", "output-truncated"):
        return "模型返回的格式或证据引用未通过校验，请重试"
    if code == "auth":
        return "模型服务认证失败，请检查模型设置"
    if code == "context-too-long":
        return "本次上下文超过模型容量，请检查模型的上下文长度设置"
    if status in RETRYABLE_HTTP:
        return f"模型服务暂时不可用（HTTP {status}），自动重试后仍未成功，请稍后重试"
    if code in ("network", "network-error", "timeout"):
        return "模型服务连接失败或超时，自动重试后仍未成功，请稍后重试"
    return "本条生成未完成，可以重试"


class Stop(Exception):
    pass


def round_rows(rows, user):
    """The friend's messages since the user's last reply, ending at the last row (the anchor)."""
    result = []
    for row in reversed(rows):
        if row["kind"] == "system":
            continue
        if row["side"] != "other" or row["sender_id"] != user or row["group_chat"]:
            break
        result.append(row)
        if len(result) >= ROUND_MAX:
            break
    return list(reversed(result))


def reply_depth(settings):
    """回复分析深度; a missing or unknown value (older saved settings) reads as the default."""
    value = (settings or {}).get("values", {}).get("replyDepth")
    return value if value in REPLY_DEPTHS else DEFAULT_REPLY_DEPTH


def research_turns(rows):
    """The real conversation for 回复调研: the latest turns, ending with the friend's round."""
    turns = []
    for row in rows:
        if row["kind"] == "system":
            continue
        text = " ".join((row.get("text") or "").split()) or "[%s]" % (row.get("kind") or "消息")
        turns.append({"from": "friend" if row["side"] == "other" else "me", "text": text[:RESEARCH_TURN_CHARS]})
    turns = turns[-RESEARCH_TURNS:]
    return turns if turns and turns[-1]["from"] == "friend" else []


def research_payload(value):
    """The finished research as prompt material: the likely reply, the timeline it read, real past replies."""
    result = value.get("result") if isinstance(value, dict) else None
    if not isinstance(result, dict) or not result.get("messages"):
        return None
    past = []
    for row in value.get("sources") or []:
        day = time.strftime("%Y-%m-%d", time.localtime(row["timeMs"] / 1000)) if type(row.get("timeMs")) is int else ""
        past.append({"time": day, "sameContact": row.get("sameContact") is True,
                     "context": row["input"][-300:], "reply": row["reply"][:300]})
    return {"likelyReply": result["messages"], "timeline": result.get("timeline") or [],
            "pastReplies": past[:RESEARCH_EXAMPLES]}


def portraits(core, account, user, anchor, budget):
    """Optional 本人画像/对方画像 and 我对 ta 的说话方式 (``mine``); any failure leaves suggestions usable."""
    if budget < 400 or not hasattr(core, "portrait_context"):
        return {}
    try:
        value = core.portrait_context(account, user, anchor, budget_chars=min(PORTRAIT_CHARS, budget // 2))
    except CoreError as exc:
        if exc.code in ("execution-revoked", "account-changed", "stopping", "cancelled"):
            raise
        return {}
    result = {}
    for side in ("self", "friend", "mine"):
        item = value.get(side) if isinstance(value, dict) else None
        if (isinstance(item, dict) and item.get("available") is True and isinstance(item.get("text"), str) and
                item["text"].strip()):
            result[side] = item["text"].strip()
    return result


class Job:
    def __init__(self, account, user, ids, spec):
        self.account, self.user, self.ids, self.spec = account, user, list(ids), spec
        self.round = list(ids)
        self.context = None
        self.cancelled = threading.Event()
        self.thread = None
        self.binding = None
        self.hashes = None
        self.model_stamp = None

    def cancel(self):
        self.cancelled.set()
        if self.context is not None:
            self.context.cancelled.set()


class Worker:
    def __init__(self, insights):
        self.insights = insights
        self.plugin = insights.plugin
        self.media = MediaObservations(self.plugin)

    def start(self, account, user, ids, spec):
        job = Job(account, user, ids, spec)
        job.thread = threading.Thread(target=self._run, args=(job,), daemon=True, name="insights-job")
        job.thread.start()
        return job

    def _run(self, job):
        store = self.insights.store(job.account)
        parts = job.spec["parts"]
        title = "分析与建议" if len(parts) == 2 else ("消息分析" if "analysis" in parts else "回复建议")
        try:
            created = self.plugin.core.post("tasks", {
                "kind": "background" if job.spec["automatic"] else "interactive", "account": job.account,
                "user": job.user, "title": title, "params": {"messages": len(job.ids)}})
        except CoreError:
            store.finish(job.user, job.ids, job.spec["generation"], "error", error="本条分析未完成，可以重试")
            return
        self.plugin.run_task(created["task"], created["execution"], lambda context: self._work(job, context))

    # Checks -----------------------------------------------------------------------------------------------------------
    def _check(self, job, context, fresh=False):
        if job.cancelled.is_set() or context.cancelled.is_set():
            raise Cancelled()
        store = self.insights.store(job.account)
        with store.lock:
            setting = store.settings(job.user)
            if setting["revision"] != job.spec["revision"] or not setting["unified_enabled"]:
                raise Stop("聊天分析设置已变化")
        if not fresh:
            return
        self.insights.check_account(job.account)
        if job.model_stamp is not None and self.insights.config_stamp(refresh=True) != job.model_stamp:
            raise Stop("模型配置已变化，请重试未完成的部分")
        # New-message refreshes temporarily change proof availability and the whole
        # profile stamp. Verify the actual frozen messages once Core is ready again.
        deadline = time.monotonic() + READY_WAIT
        while True:
            status = self._wait_ready(job, context, prepare=False, deadline=deadline)
            if job.binding is not None and ((status["sourceFingerprint"], status["epoch"]) != (job.binding[0], job.binding[3]) or
                    status.get("currentSourceFingerprint", job.binding[0]) != job.binding[0]):
                raise Stop("聊天记录来源已切换，请重试")
            if not job.hashes:
                return
            current = {row["msg_id"]: row for row in self.insights.projected(job.account, job.user, list(job.hashes))}
            after = context.core.history_status(job.account, job.user)
            if after.get("state") != "ready" or any(after.get(key) != status.get(key) for key in
                    ("sourceFingerprint", "currentSourceFingerprint", "epoch")):
                continue  # A refresh during the read is not evidence that a message was removed.
            for message_id, digest in job.hashes.items():
                row = current.get(message_id)
                if row is None or message_hash(row) != digest:
                    raise Stop("消息内容已变化")
            return

    def _wait_ready(self, job, context, prepare=True, deadline=None):
        core = context.core
        if prepare:
            core.post("history/prepare", {"account": job.account, "user": job.user, "refresh": True})
        deadline = deadline if deadline is not None else time.monotonic() + READY_WAIT
        while time.monotonic() < deadline:
            self._check(job, context)
            status = core.history_status(job.account, job.user)
            if status["state"] == "ready":
                return status
            if status["state"] == "error":
                raise InsightError("context-error", "聊天记录读取失败，请重试")
            time.sleep(0.2)
        raise InsightError("context-error", "聊天记录读取超时，请重试")

    def _generate_part(self, job, context, part, system, prompt, sources, limits, thinking=False, images=()):
        # A thinking reply spends part of the output cap on reasoning before the JSON.
        request = {"system": system, "prompt": prompt, "jsonMode": True, "thinking": thinking,
                   "maxOutputTokens": limits["outputReservedTokens"] if thinking else
                   min(limits["outputReservedTokens"], 2048), **({"images": list(images)} if images else {})}
        # A transport failure must not consume the chance to correct a response.
        # Two transport retries plus one correction bound each part to four calls.
        transport_retries = corrections = 0
        while True:
            self._check(job, context, fresh=True)
            try:
                response = self.plugin.models.generate(context, request, step=part)
                raw = response.get("text")
                return ({job.ids[0]: parse_reply_candidates(raw)} if part == "suggestions" else
                        parse_result(raw, job.round, sources, anchor=job.ids[0]))
            except (Cancelled, Stop):
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code in (*STOPPING, "auth", "context-too-long", "api-not-configured"):
                    raise
                malformed = code in ("invalid-output", "invalid-suggestions", "empty-response", "output-truncated")
                status = getattr(exc, "http_status", None)
                retryable = status in RETRYABLE_HTTP or (status is None and code in ("network", "network-error", "timeout"))
                if malformed:
                    if corrections >= FORMAT_CORRECTIONS:
                        raise
                    corrections += 1
                    request = {**request, "system": system + "\n" + correction_instruction(part, job.round, sources)}
                    if code == "output-truncated":
                        request["maxOutputTokens"] = min(limits["outputReservedTokens"], 4096)
                elif retryable:
                    if transport_retries >= TRANSPORT_RETRIES:
                        raise
                    transport_retries += 1
                    self._wait_retry(job, context, RETRY_DELAY * (2 ** (transport_retries - 1)))
                else:
                    raise

    def _start_research(self, job, context, rows):
        """Ask 自我剖析 to research this round; ``None`` (no research) whenever it cannot start."""
        turns = research_turns(rows)
        if not turns or not hasattr(context.core, "self_simulate"):
            return None
        try:
            value = context.core.self_simulate(job.account, job.user, job.ids[0], turns, "auto")
        except CoreError as exc:
            if exc.code in CORE_STOPPING:
                raise
            return None
        ok = isinstance(value, dict) and value.get("available") is True and isinstance(value.get("id"), str)
        return value["id"] if ok else None

    def _finish_research(self, job, context, identifier):
        """Wait for the research within ``RESEARCH_SECONDS``; a stopped job stops the research too."""
        deadline = time.monotonic() + RESEARCH_SECONDS
        try:
            while True:
                try:
                    value = context.core.self_simulate_status(job.account, job.user, identifier)
                except CoreError as exc:
                    if exc.code in CORE_STOPPING:
                        raise
                    return None
                state = value.get("state") if isinstance(value, dict) and value.get("available") is True else "error"
                if state != "running":
                    return research_payload(value) if state == "done" else None
                if time.monotonic() >= deadline:
                    break
                self._check(job, context)
                context.cancelled.wait(RESEARCH_POLL)
        except BaseException:
            self._stop_research(job, context, identifier)
            raise
        self._stop_research(job, context, identifier)
        return None

    @staticmethod
    def _stop_research(job, context, identifier):
        try:
            context.core.self_simulate_status(job.account, job.user, identifier, stop=True)
        except Exception:
            pass

    def _wait_retry(self, job, context, delay):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            self._check(job, context)
            context.cancelled.wait(min(0.1, max(0, deadline - time.monotonic())))

    # Work -------------------------------------------------------------------------------------------------------------
    def _work(self, job, context):
        job.context = context
        if job.cancelled.is_set():
            context.cancelled.set()
        store = self.insights.store(job.account)
        spec, user = job.spec, job.user
        research = None
        try:
            reply_system = (suggestions_system(spec["settings"]["values"]["replyStyle"]) + "\n" + APPLICABILITY
                            if "suggestions" in spec["parts"] else "")
            depth = reply_depth(spec["settings"])
            self._check(job, context)
            status = self._wait_ready(job, context)
            targets = self.insights.projected(job.account, user, job.ids)
            if len(targets) != len(job.ids) or any(row["side"] != "other" or row["sender_id"] != user or
                                                   row["group_chat"] or row["kind"] == "system" for row in targets):
                raise InsightError("not-found", "这条好友消息不在当前可核对的记录中")
            job.ids = [row["msg_id"] for row in targets]
            job.hashes = {row["msg_id"]: message_hash(row) for row in targets}
            service = self.plugin.models.service()
            job.model_stamp = service.get("stamp")
            limits = budget(service.get("contextTokens"))
            job.binding = self.insights.binding(job.account, user, unified=spec["unified"], status=status)
            upto = max(row["seq"] for row in targets)
            page = context.core.history_messages(job.account, user, after=max(0, upto - CONTEXT_ROWS), limit=CONTEXT_ROWS)
            rows = [row_from_view(view) for view in page["messages"] if view["seq"] <= upto]
            present = {row["msg_id"] for row in rows}
            rows = sorted(rows + [row for row in targets if row["msg_id"] not in present], key=lambda row: row["seq"])
            targets = round_rows(rows, user) or targets
            job.round = [row["msg_id"] for row in targets]
            if job.round[-1] != job.ids[0]:
                raise InsightError("not-found", "这条好友消息不在当前可核对的记录中")
            image_service = self.plugin.models.service_for("image")
            # The main model looks at the pictures itself; only a separate image model describes them first.
            direct = "main" in (image_service.get("roles") or []) or image_service.get("stamp") == job.model_stamp
            collected, observations = {}, {}
            if direct:
                collected = self.media.collect_images(context, rows, recent_messages=CONTEXT_ROWS, priority_ids=job.round)
            else:
                # 媒体观察缓存身份跟随图片模型。
                observations = self.media.collect(context, rows, scope=job.binding[0], model_fp=image_service.get("stamp"),
                                                  recent_messages=CONTEXT_ROWS, priority_ids=job.round)
            limit = limits["inputBudgetChars"]
            limit -= safety_margin(limit) + max(len(SYSTEM) if "analysis" in spec["parts"] else 0, len(reply_system)) + 256
            limit -= IMAGE_CHARS * len(attachments(collected)[1])
            recent = "\n".join(row["text"] or "" for row in rows)
            self._check(job, context, fresh=True)
            snapshot = context.core.get(f"human/{user}", account=job.account, view="snapshot")
            job.binding = (job.binding[0], job.binding[1], snapshot["stamp"], job.binding[3])
            friend_sources = structured_sources(snapshot, max(0, min(12000, limit // 4)), recent)
            people = portraits(context.core, job.account, user, job.ids[0], limit // 4)
            self._check(job, context)

            def fill(extra, notes):
                """Reserve room for optional material without dropping or clipping the round."""
                reserve = len(json.dumps(extra, ensure_ascii=False)) + 64 if extra else 0
                try:
                    base, base_sources = material(rows, job.round, friend_sources, notes, limit - reserve, attached=direct)
                    text = with_fields(base, extra, limit)
                    return text, base_sources, text != base or not extra
                except InsightError:
                    base, base_sources = material(rows, job.round, friend_sources, notes, limit, attached=direct)
                    return base, base_sources, False  # A large round takes precedence over optional material.

            def build(extra):
                """The prompt, its sources, whether ``extra`` fitted, and the images its notes number."""
                if not direct:
                    return (*fill(extra, observations), [])
                notes, images = attachments(collected)
                for _ in range(3):
                    text, found, fitted = fill(extra, notes)
                    # An earlier message cut for room takes its images with it; the rest are renumbered.
                    kept = {item["messageId"] for item in found.values() if item["kind"] == "observation"}
                    notes, fewer = attachments(collected, kept)
                    if len(fewer) == len(images):
                        break
                    images = fewer
                return text, found, fitted, images
            prompt, sources, _, images = build({"portraits": {"friend": people["friend"]}} if "friend" in people else {})
            personal = research = None
            reply_prompt, reply_sources, reply_images, extra = prompt, sources, images, {}
            if "suggestions" in spec["parts"]:
                # Research runs in 自我剖析 while the explanation is written; the reply waits for it.
                research = self._start_research(job, context, rows) if depth == "deep" else None
                personal_budget = min(5000, limit // 4)
                # The round and the few messages before it say what situation to look up, not the whole window.
                personal_query = expression_query(rows[-(len(job.round) + QUERY_CONTEXT):])
                personal = expression_context(context.core, job.account, user, job.ids[0], personal_query, personal_budget)
                examples = expression_payload(personal, personal_budget)
                described = {side: people[side] for side in ("self", "friend") if side in people}
                extra = {key: value for key, value in (("myStyleWithFriend", people.get("mine")),
                                                       ("personalReplyStyle", examples), ("portraits", described)) if value}
                reply_prompt, reply_sources, fitted, reply_images = build(extra)
                if examples is None or not fitted:
                    personal = None
            included = {item["messageId"] for item in [*sources.values(), *reply_sources.values()] if item["kind"] == "chat"}
            job.hashes = {row["msg_id"]: message_hash(row) for row in rows if row["msg_id"] in included}
            pictures = [hashlib.sha256(image["data"].encode()).hexdigest() for image in [*images, *reply_images]]
            context_version = hashlib.sha256(json.dumps([job.binding, job.hashes, prompt, reply_prompt, pictures],
                                                        sort_keys=True).encode()).hexdigest()
            store.bind(user, job.ids, spec["generation"], job.binding, job.hashes, context_version, round_ids=job.round)
            failures = []
            for part in spec["parts"]:
                try:
                    if part == "suggestions" and research is not None:
                        found, research = self._finish_research(job, context, research), None
                        if found:
                            researched, researched_sources, fitted, researched_images = build({"selfResearch": found, **extra})
                            if fitted:
                                reply_prompt, reply_sources, reply_images = researched, researched_sources, researched_images
                    results = self._generate_part(job, context, part, reply_system if part == "suggestions" else SYSTEM,
                                                  reply_prompt if part == "suggestions" else prompt, sources, limits,
                                                  thinking=part == "suggestions" and depth != "basic",
                                                  images=reply_images if part == "suggestions" else images)
                    self._check(job, context, fresh=True)
                    if part == "suggestions" and personal is not None:
                        if not still_current(context.core, job.account, user, job.ids[0], personal, personal_query, personal_budget):
                            raise Stop("本人的知识库或资料已变化，请重试回复建议")
                        # Validation can wait on Core/source reads; respect a stop
                        # delivered while that request was in flight.
                        self._check(job, context)
                    store.finish(user, job.ids, spec["generation"], "ready", results=results, part=part)
                except (Cancelled, Stop):
                    raise
                except Exception as exc:
                    if getattr(exc, "code", None) in STOPPING:
                        raise Stop(getattr(exc, "message", "已停止")) from None
                    error = generation_error(exc)
                    failures.append({"code": getattr(exc, "code", None) or "insights-failed", "message": error})
                    store.finish(user, job.ids, spec["generation"], "error", part=part,
                                 error=error)
            context.finish(("failed" if len(failures) == len(spec["parts"]) else "partial") if failures else "done",
                           failures[-1] if failures else None)
        except (Cancelled, Stop) as exc:
            error = str(exc) or "任务已中断，可以重试未完成的部分"
            store.finish(user, job.ids, spec["generation"], "error", error=error)
            context.finish("failed", {"code": "stopped", "message": error})
        except Exception as exc:
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", None)
            error = message if code in ("context-too-long", "invalid-output", "not-found", "api-not-configured") \
                else "本条分析未完成，可以重试"
            store.finish(user, job.ids, spec["generation"], "error", error=error)
            context.finish("failed", {"code": code or "insights-failed", "message": error})
        finally:
            if research is not None:
                self._stop_research(job, context, research)
            self.insights.wake.set()
