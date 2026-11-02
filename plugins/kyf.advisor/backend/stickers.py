"""Stickers in the assistant's chat record: identity, official captions and vision labels.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-16: refresh each call's timeout without counting stale configuration as a model failure.

Every sticker message is written as ``[表情#3f2a9c「晚安」·抹茶旦旦]`` or ``[表情#3f2a9c：柴犬歪头眨眼]``: the
number is the start of the sticker's content MD5 (the same number is the same picture), the caption and pack
name come from WeChat, and the label is a vision model's one-line summary of a few frames of the picture.
Identities are read from Core while a turn prepares; labels are written by a background Core task
(``识别表情``) that opening the assistant or finishing an answer starts, one sticker at a time, most used first.
"""
from __future__ import annotations

import base64
import binascii
import threading
import time

from contracts import AdvisorError, valid_request_id
from kyf_plugin import Cancelled, CoreError

TASK_TITLE = "识别表情"
TASK_PURPOSE = "sticker-labels"
RESOLVE_BATCH = 1000
RESOLVE_LIMIT = 20000
MAX_LABELS_PER_TASK = 100
MAX_FAILURES = 2
STOP_AFTER_ERRORS = 3
MAX_LABEL_CHARS = 60
FRAMES = 4
IDLE_SECONDS = 30
QUIET_SECONDS = 600
FAILURE_QUIET_SECONDS = 3600
READY_TIMEOUT_SECONDS = 300
RUN_WAIT_SECONDS = 0.5
NOTE = ("表情写作 [表情#编号…]：同一编号是同一张图；「」内是微信表情商店的官方配字，·后是表情包名，"
        "冒号后是视觉模型对画面（动图按几帧）的简短概括，不是发送者自述；“画面未识别”表示尚无概括。")


def label(info):
    """The chat-record text of one sticker message from its known identity, caption and label."""
    if not isinstance(info, dict) or not info.get("id"):
        return "[表情，内容未读取]"
    text = "表情#" + info["id"][:6]
    if info.get("caption"):
        text += "「" + info["caption"] + "」"
    if info.get("packName"):
        text += "·" + info["packName"]
    if info.get("description"):
        text += "：" + info["description"]
    elif not info.get("caption"):
        text += "，画面未识别"
    return "[" + text + "]"


def clean_label(text):
    if not isinstance(text, str):
        return None
    text = " ".join(text.split()).strip("。 ")
    return text[:MAX_LABEL_CHARS] or None


def frames_payload(payload):
    """``{"images": [...], "animated": bool}`` from Core's media view of a sticker, or None when unusable."""
    if not isinstance(payload, dict) or payload.get("error") or payload.get("kind") != "sticker":
        return None
    items = payload.get("items")
    animated = payload.get("sampling") == "animated-frames"
    if not isinstance(items, list) or not 1 <= len(items) <= (FRAMES if animated else 1):
        return None
    images, previous = [], -1
    for item in items:
        if not isinstance(item, dict) or item.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            return None
        try:
            size = len(base64.b64decode(item.get("data") or "", validate=True))
        except (binascii.Error, TypeError, ValueError):
            return None
        if not 0 < size <= 2 * 1024 * 1024:
            return None
        image = {"mime": item["mime"], "data": item["data"]}
        if animated:
            time_ms = item.get("timeMs")
            if type(time_ms) not in (int, float) or not previous <= time_ms <= 86_400_000:
                return None
            image["timeMs"] = previous = time_ms
        images.append(image)
    return {"images": images, "animated": animated}


class StickerLabeler:
    """Starts and runs the background labelling task, at most one per conversation at a time."""

    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.active = set()
        self.quiet_until = {}
        self.running = {}

    # -- identities ----------------------------------------------------------
    def resolve(self, account, user, database, upto_seq, check):
        """Ask Core for the identity of mirrored sticker messages not yet known; False when Core cannot tell."""
        read = getattr(self.service.plugin.core, "history_stickers", None)
        if not callable(read):
            return False
        pending = database.sticker_unresolved(user, upto_seq, RESOLVE_LIMIT)
        for start in range(0, len(pending), RESOLVE_BATCH):
            check()
            batch = pending[start:start + RESOLVE_BATCH]
            try:
                result = read(account, user, batch)
            except CoreError as exc:
                if exc.code in ("account-changed", "account-unavailable") or exc.status == 409:
                    raise AdvisorError("account-changed", "微信账号或数据来源已变化") from None
                return False
            if not isinstance(result, dict) or result.get("error"):
                return False
            check()
            database.save_sticker_refs(user, batch, result.get("stickers") or {})
        return True

    # -- scheduling ----------------------------------------------------------
    def start_requested(self, account, user, request_id):
        """Explicitly label an existing mirrored conversation, without opening a discussion.

        A persisted request fences Core creation. If its response is lost, the
        caller reconciles Core tasks by completionRequestId; we never resubmit
        an ambiguous request, even after a process restart.
        """
        request_id = valid_request_id(request_id)
        if request_id is None:
            raise AdvisorError("invalid-request", "缺少表情处理请求标识")
        service = self.service
        service._require_scope(account)
        database = service.stores.account(account)
        key = (account, user)
        with self.lock:
            existing = database.sticker_request(request_id)
            if existing:
                return self._request_result(existing, user)
            if database.context(user) is None:
                raise AdvisorError("context-error", "只能继续处理已经准备过的聊天表情")
            if key in self.active:
                raise AdvisorError("run-active", "该聊天的表情处理正在运行")
            if (service._model_service() is None or not service._engine_available() or
                    not callable(getattr(service.plugin.core, "history_stickers", None))):
                raise AdvisorError("engine-unavailable", "表情识别暂不可用")
            image_model = service.plugin.models.service_for("image", refresh=True)
            if not image_model or image_model.get("supportsVision") is False:
                raise AdvisorError("vision-required", "当前图片模型不支持图像")
            pending = database.sticker_work_pending(user, MAX_FAILURES, image_model.get("fingerprint"))
            existing, claimed = database.claim_sticker_request(user, request_id)
            if not claimed:
                return self._request_result(existing, user)
            if not pending:
                database.finish_sticker_request(user, request_id, None)
                return {"state": "no-work", "taskId": None}
            self.active.add(key)
            try:
                service._require_scope(account)
                task, _context = service.plugin.start_task(
                    account, user, kind="background", title=TASK_TITLE,
                    params={"purpose": TASK_PURPOSE, "completionRequestId": request_id}, handler=self.run)
                database.finish_sticker_request(user, request_id, task["id"])
                return {"state": "submitted", "taskId": task["id"]}
            except Exception:
                self.active.discard(key)
                raise AdvisorError("sticker-request-uncertain", "请在任务列表核对本次请求后继续，不能重复创建") from None

    @staticmethod
    def _request_result(record, user):
        if record["user"] != user:
            raise AdvisorError("request-conflict", "请求标识已经用于另一聊天")
        if record["state"] == "prepared":
            raise AdvisorError("sticker-request-uncertain", "请在任务列表核对本次请求后继续，不能重复创建")
        return {"state": record["state"], "taskId": record["task_id"]}

    def schedule(self, account, user):
        """Start a labelling task for the conversation when it has work; never blocks the caller."""
        key = (account, user)
        with self.lock:
            if key in self.active or time.monotonic() < self.quiet_until.get(key, 0):
                return False
            self.active.add(key)
        threading.Thread(target=self._start, args=(account, user), daemon=True, name="sticker-labels").start()
        return True

    def _start(self, account, user):
        key, started = (account, user), False
        try:
            service = self.service
            selected = service._model_service()
            if (selected is None or not service._engine_available() or
                    not callable(getattr(service.plugin.core, "history_stickers", None))):
                return
            service._require_scope(account)
            database = service.stores.account(account)
            image_model = service.plugin.models.service_for("image", refresh=True)
            if (database.context(user) is None or
                    not database.sticker_work_pending(user, MAX_FAILURES, image_model.get("fingerprint"))):
                return
            service.plugin.start_task(account, user, kind="background", title=TASK_TITLE,
                                      params={"purpose": TASK_PURPOSE}, handler=self.run)
            started = True
        except Exception:  # noqa: BLE001 - labelling is optional and never disturbs the assistant
            pass
        finally:
            if not started:
                with self.lock:
                    self.active.discard(key)
                    self.quiet_until[key] = time.monotonic() + IDLE_SECONDS

    # -- the task --------------------------------------------------------------
    def run(self, task):
        """Task handler (also for Core's "continue" of an interrupted ``识别表情``)."""
        key, done = (task.account, task.user), threading.Event()
        with self.lock:
            self.active.add(key)
            self.running[id(task)] = (task, done)
        quiet = FAILURE_QUIET_SECONDS
        try:
            outcome, quiet = self._label(task)
            return outcome
        finally:
            with self.lock:
                self.active.discard(key)
                self.running.pop(id(task), None)
                self.quiet_until[key] = time.monotonic() + quiet
            done.set()

    def stop(self, account=None, timeout=10.0):
        """Stop the labelling tasks of one account (all when None) and wait briefly for them to end."""
        with self.lock:
            stopping = [(task, done) for task, done in self.running.values() if account in (None, task.account)]
        for task, _done in stopping:
            try:
                task.stop()
            except Exception:  # noqa: BLE001 - Core may already have stopped it
                task.cancelled.set()
        deadline = time.monotonic() + timeout
        for _task, done in stopping:
            done.wait(max(0.0, deadline - time.monotonic()))

    def _label(self, task):
        service = self.service
        account, user = task.account, task.user

        def check():
            task.check()
            if service._closing or account in service._paused:
                raise Cancelled()

        check()
        service._require_scope(account)
        database = service.stores.account(account)
        mirror = self._mirror(task, database, check)
        if mirror is None:
            return "done", QUIET_SECONDS
        selected = service._model_service()
        if selected is None:
            return "done", QUIET_SECONDS
        config = service._resolve_config(task.token, selected)
        image_model = service._media_model(config)
        model_fp = image_model.get("fingerprint")
        self.resolve(account, user, database, mirror["read_count"], check)
        queue = database.sticker_queue(user, MAX_LABELS_PER_TASK, MAX_FAILURES, model_fp)
        if not queue:
            return "done", QUIET_SECONDS
        runtime = service._runtime_instance(task.cancelled)
        errors = 0
        for index, item in enumerate(queue):
            self._wait_for_answers(account, task, index, len(queue), check)
            frames = None
            for message_id in item["messages"]:
                try:
                    frames = frames_payload(service.plugin.core.media(account, user, message_id, frames=FRAMES))
                except CoreError as exc:
                    if exc.code in ("account-changed", "account-unavailable") or exc.status == 409:
                        raise AdvisorError("account-changed", "微信账号或数据来源已变化") from None
                    frames = None
                if frames:
                    break
                check()
            if frames is None:
                database.sticker_failed(item["id"], model_fp, "media-unavailable")
                continue
            check()
            selected_service = service._media_service(config, image_model.get("stamp"))
            failure_reason = "empty-label"
            try:
                result = runtime.inspect_media(selected_service, config["token"], {
                    "account": account, "user": user, "agentId": TASK_PURPOSE, "threadId": TASK_PURPOSE,
                    "sourceId": config["sourceId"], "mediaKind": "sticker", "purpose": "sticker-label",
                    "images": frames["images"], **({"sampling": "animated-frames"} if frames["animated"] else {}),
                }, lambda _event: None, task.cancelled)
                text = clean_label(result.get("text") if isinstance(result, dict) else None)
            except Exception:  # noqa: BLE001 - a model without image input fails every call
                text = None
                failure_reason = "model-error"
            check()
            if text is None:
                database.sticker_failed(item["id"], model_fp, failure_reason)
                errors += 1
                if errors >= STOP_AFTER_ERRORS:
                    return "partial", FAILURE_QUIET_SECONDS
                continue
            errors = 0
            service._require_scope(account)
            service._media_service(config, image_model.get("stamp"))
            database.save_sticker_description(item["id"], text, model_fp)
        task.progress(done=len(queue), total=len(queue), label=TASK_TITLE)
        return "done", QUIET_SECONDS

    def _wait_for_answers(self, account, task, index, total, check):
        """The user's questions go first: labelling pauses while an answer of this account is running."""
        task.progress(done=index, total=total, label=TASK_TITLE)
        waiting = False
        while self.service.has_active_runs(account):
            if not waiting:
                waiting = True
                task.progress(done=index, total=total, label="等待回答完成")
            check()
            task.cancelled.wait(RUN_WAIT_SECONDS)
        if waiting:
            task.progress(done=index, total=total, label=TASK_TITLE)

    def _mirror(self, task, database, check):
        """The conversation's mirror brought up to Core's projection, or None when it is not ready in time."""
        core = self.service.plugin.core
        account, user = task.account, task.user
        try:
            core.history_prepare(account, user)
            deadline = time.monotonic() + READY_TIMEOUT_SECONDS
            while True:
                check()
                status = core.history_status(account, user)
                if status.get("state") == "ready":
                    break
                if status.get("state") in ("error", "idle") or time.monotonic() > deadline:
                    return None
                task.cancelled.wait(0.2)
        except CoreError as exc:
            if exc.code in ("account-changed", "account-unavailable") or exc.status == 409:
                raise AdvisorError("account-changed", "微信账号或数据来源已变化") from None
            return None
        mirror = database.sync(user, check=check)
        return mirror if mirror is not None and mirror["state"] == "ready" else None
