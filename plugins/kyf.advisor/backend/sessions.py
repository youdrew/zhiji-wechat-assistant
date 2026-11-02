"""Scoped discussion operations; provider state never reaches the renderer.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
Modified 2026-10-16: queue state and compaction pause/drain integration.
"""
from __future__ import annotations

import threading
import time

from contracts import AdvisorError, effective_model_budget, valid_draft_mentions, valid_identifier, valid_request_id, valid_scope_value
from kyf_plugin import Cancelled


BUSY = {"running", "unknown"}


class SessionTask:
    def __init__(self, operation, account, user, agent_id, database):
        self.id = operation["id"]
        self.thread_id = operation["thread_id"]
        self.account, self.user, self.agent_id = account, user, agent_id
        self.database = database
        self.state = "running"
        self.purpose = "session-compact"
        self.cancel = threading.Event()
        self.worker = None
        self.task = None
        self.config_stamp = None
        self.source_scope = None
        self.profile_stamp = None
        self.context_epoch = None


class SessionServiceMixin:
    def _reconcile_stopped_answers(self, account, database, user):
        """Persist terminal metadata after a stale provider worker has drained.

        This is called only through a freshly validated account operation, not
        through a late provider callback. No answer text is written here.
        """
        for row in database.active_runs_for_user(user):
            live = self._runs.get(row["id"])
            if (live and live.account == account and live.state in {"stopped", "error", "done"} and
                    (live.worker is None or not live.worker.is_alive())):
                database.finish_run(row["id"], row["thread_id"], "stopped", error="资料或配置已更新，任务已中断",
                                    status_text="资料或配置已更新，任务已中断")
                for operation in database.session_operations(row["thread_id"]):
                    if operation["kind"] == "rebuild" and operation["state"] == "running":
                        database.session_operation_update(operation["id"], state="interrupted",
                            error="资料或配置已更新，讨论整理已中断")
        for task in self._session_tasks.values():
            if (task.account == account and task.user == user and task.state != "running" and
                    task.worker is not None and not task.worker.is_alive()):
                operation = database.session_operation(task.id)
                if operation and operation["state"] == "running":
                    database.session_operation_update(task.id, state="interrupted", error="讨论压缩已中断")

    def _session_scope(self, account, user, agent_id, thread_id, writable=False):
        account = self._require_scope(account)
        user = valid_scope_value(user, "user")
        if not valid_identifier(agent_id) or not valid_identifier(thread_id):
            raise AdvisorError("invalid-request", "会话标识不正确")
        database = self.stores.account(account)
        self._reconcile_stopped_answers(account, database, user)
        row = database.session_thread(user, agent_id, thread_id)
        if writable:
            agent = self.config.agent(agent_id)
            if agent is None or not agent["enabled"]:
                raise AdvisorError("agent-disabled", "该助手已停用或删除，只能查看历史")
        return database, row

    def _session_assert_idle(self, database, row):
        if database.active_run(row["id"]) or database.session_active_operation(row["id"]):
            raise AdvisorError("run-active", "该会话正在处理，完成或停止后再试")

    def _session_assert_drained(self, account, thread_id):
        if any(run.account == account and run.thread_id == thread_id and run.worker is not None and run.worker.is_alive()
               for run in self._owned_runs()):
            raise AdvisorError("run-active", "该会话仍在处理或停止中，请稍后重试")

    @staticmethod
    def _draft_payload(draft):
        return {"text": draft.get("text", ""), "version": draft.get("version", 0),
                "scrollTop": draft.get("scroll_top", 0), "mentions": draft.get("mentions", [])}

    @staticmethod
    def _operation_payload(op):
        if op is None:
            return None
        result = op.get("result") or {}
        return {"id": op["id"], "threadId": op["thread_id"], "kind": op["kind"],
                "requestId": op["request_id"],
                "automatic": (op.get("metadata") or {}).get("automatic", False),
                "state": op["state"], "createdAtMs": result.get("startedAt") or op.get("created_at_ms"),
                "completedAtMs": (result.get("finishedAt") or op.get("updated_at_ms")) if op["state"] not in BUSY else None,
                "summary": result.get("summary"), "error": op.get("error"),
                "coverage": result.get("coverage")}

    def _session_payload(self, account, database, row, before_seq=None):
        if row is None:
            raise AdvisorError("thread-unknown", "会话已删除或不存在")
        row = database.session_thread(row["user"], row["agent_id"], row["id"])
        page = database.session_messages(row["user"], row["agent_id"], row["id"], before_seq=before_seq)
        agent = self.config.agent(row["agent_id"])
        readonly = agent is None or not agent["enabled"]
        operations = database.session_operations(row["id"])
        usage = database.session_latest_usage(row["id"])
        current_stamp = self._config_stamp(self._model_service())
        current_profile = self._profile_stamp(account, row["user"])
        current_epoch = (database.context(row["user"]) or {}).get("epoch")
        # Display-only: a recent check, never a fresh WeChat identity verification under the service lock.
        current_source = self._source_scope(account, row["user"], fresh=False)
        compacted_since_usage = False
        if usage:
            usage = usage.get("payload", usage)
            if (usage.get("configStamp") != current_stamp or
                    usage.get("sourceScope") != current_source or
                    usage.get("profileStamp") != current_profile or
                    usage.get("contextEpoch") != current_epoch or
                    usage.get("runtimeSessionId") != row.get("runtime_session_id")):
                usage = None
        for op in operations:
            metadata = op.get("metadata") or {}
            if (op["kind"] == "compact" and op["state"] == "completed" and
                    metadata.get("runtime_session_id") == row.get("runtime_session_id") and
                    metadata.get("config_stamp") == current_stamp and
                    metadata.get("source_scope") == current_source and
                    metadata.get("profile_stamp") == current_profile and
                    metadata.get("context_epoch") == current_epoch and
                    op.get("updated_at_ms", 0) > (usage or {}).get("recordedAtMs", 0)):
                compacted_since_usage = True
        if compacted_since_usage:
            usage = None
        selected = self._model_service() or {}
        try:
            window = effective_model_budget(selected.get("contextTokens"))["contextTokens"]
        except AdvisorError:
            window = None
        return {"id": row["id"], "account": account, "user": row["user"], "agentId": row["agent_id"],
                "sourceId": row["source_id"], "title": row.get("title") or "开始新讨论",
                "runtimeSessionId": row.get("runtime_session_id"),
                "agentName": row.get("agent_name") or (agent["name"] if agent else row["agent_id"]),
                "welcome": agent["welcome"] if agent else "", "readonly": readonly,
                "readonlyReason": "该助手已停用或删除，只能查看历史" if readonly else "",
                "messages": [{"id": item["id"], "seq": item["seq"], "role": item["role"],
                              "text": item["text"], "createdAtMs": item["created_at_ms"],
                              **({"runId": item["run_id"]} if item.get("run_id") else {}),
                              **({"supersededBy": item["superseded_by"]} if item.get("superseded_by") else {}),
                              **({"status": item["status"]} if item.get("status") else {})}
                              | ({"contextSnapshot": item.get("context_snapshot"),
                                  **({"mentions": item["selected_mentions"]} if "selected_mentions" in item else {})}
                                 if item["role"] == "user" else {})
                             for item in page["messages"]],
                "hasMore": page["hasMore"], "beforeSeq": page["beforeSeq"],
                "draft": self._draft_payload(database.session_draft(row["user"], row["agent_id"], row["id"])),
                "queue": database.queue_get(row["user"], row["agent_id"], row["id"]),
                "participants": database.session_participants(row["id"]),
                "contextTokens": window,
                "compactedSinceUsage": compacted_since_usage,
                "usage": usage, "compactions": [self._operation_payload(op) for op in operations if op["kind"] == "compact"],
                "activeOperation": self._operation_payload(database.session_active_operation(row["id"]))}

    def _session_read(self, account, user, agent_id, thread_id=None, before_seq=None):
        account = self._require_scope(account)
        user = valid_scope_value(user, "user")
        if not valid_identifier(agent_id):
            raise AdvisorError("invalid-request", "助手标识不正确")
        with self.condition:
            self._require_scope(account)
            database = self.stores.account(account)
            self._reconcile_stopped_answers(account, database, user)
            if thread_id:
                row = database.session_thread(user, agent_id, thread_id)
            else:
                row = database.session_selected(user, agent_id)
                if row is None:
                    agent = self.config.agent(agent_id)
                    if agent is None or not agent["enabled"]:
                        raise AdvisorError("agent-disabled", "该助手已停用或删除，可从历史列表查看")
                    row = database.create_thread(user, agent_id, self._source_id(), agent_name=agent["name"], select=True)
            run = database.latest_run(row["id"])
            return {"thread": self._session_payload(account, database, row, before_seq),
                    "context": self._context_status(self._context_snapshot(account, user)),
                    "run": self._public_run(run) if run else None}

    def threads(self, account, user, agent_id=None):
        account = self._require_scope(account)
        user = valid_scope_value(user, "user")
        if agent_id and not valid_identifier(agent_id):
            raise AdvisorError("invalid-request", "助手标识不正确")
        with self.condition:
            self._require_scope(account)
            database = self.stores.account(account)
            self._reconcile_stopped_answers(account, database, user)
        items = []
        for row in database.session_threads(user):
            agent = self.config.agent(row["agent_id"])
            readonly = agent is None or not agent["enabled"]
            if agent_id and row["agent_id"] != agent_id and not readonly:
                continue
            run = database.latest_run(row["id"])
            operation = database.session_active_operation(row["id"])
            items.append({"id": row["id"], "agentId": row["agent_id"],
                          "agentName": row.get("agent_name") or (agent["name"] if agent else row["agent_id"]),
                          "title": row.get("title") or "开始新讨论", "preview": row.get("preview", ""),
                          "updatedAtMs": row["updated_at_ms"], "readonly": readonly,
                          "readonlyReason": "该助手已停用或删除，只能查看历史" if readonly else "",
                          "deleteState": row.get("delete_state") if row.get("delete_state") != "active" else None,
                          "state": ("compacting" if operation["kind"] == "compact" and operation["state"] == "running"
                                    else operation["state"]) if operation else run["state"] if run else "idle"})
        with self.condition:
            self._require_scope(account)
            selected = database.session_selected(user, agent_id) if agent_id else None
        return {"threads": items, "selectedThreadId": selected["id"] if selected else None}

    def select_thread(self, account, user, agent_id, thread_id):
        with self.condition:
            database, row = self._session_scope(account, user, agent_id, thread_id)
            agent = self.config.agent(agent_id)
            if agent and agent["enabled"]:
                database.session_select(user, agent_id, row["id"])
            return self._session_read(account, user, agent_id, thread_id)

    def rename_thread(self, account, user, agent_id, thread_id, title, request_id):
        valid_request_id(request_id)
        if not isinstance(title, str) or not title.strip() or len(title) > 120 or "\0" in title:
            raise AdvisorError("invalid-request", "标题须为 1 到 120 个字符")
        with self.condition:
            database, row = self._session_scope(account, user, agent_id, thread_id)
            database.session_rename(user, agent_id, thread_id, title.strip(), row.get("session_version", 0))
            return self._session_read(account, user, agent_id, thread_id)

    def save_draft(self, account, user, agent_id, thread_id, text, version, scroll_top=0, migrate=False, mentions=None):
        if (not isinstance(text, str) or len(text) > 16000 or "\0" in text or type(version) is not int or version < 0 or
                type(scroll_top) not in (int, float) or not 0 <= scroll_top <= 10**9 or type(migrate) is not bool):
            raise AdvisorError("invalid-request", "草稿参数不正确")
        mentions = valid_draft_mentions(mentions)
        with self.condition:
            if migrate and thread_id is None:
                account = self._require_scope(account)
                valid_scope_value(user, "user")
                latest = self.stores.account(account).latest_thread(user, agent_id)
                if latest is None:
                    raise AdvisorError("thread-unknown", "没有可迁移草稿的历史会话")
                thread_id = latest["id"]
            database, row = self._session_scope(account, user, agent_id, thread_id, writable=True)
            self._mentioned_friends(account, user, [item["user"] for item in mentions])
            if migrate:
                latest = database.latest_thread(user, agent_id)
                if latest is None or latest["id"] != thread_id:
                    raise AdvisorError("request-conflict", "旧草稿只能迁移到升级前最新会话")
                saved = database.session_draft(user, agent_id, thread_id)
                if saved["version"] > 0:
                    return {"draft": self._draft_payload(saved)}
            draft = database.session_save_draft(user, agent_id, thread_id, text, scroll_top, version, mentions=mentions)
            return {"draft": self._draft_payload(draft)}

    def _session_new(self, account, user, agent_id, request_id=None):
        account = self._require_scope(account)
        user = valid_scope_value(user, "user")
        if not valid_identifier(agent_id):
            raise AdvisorError("invalid-request", "助手标识不正确")
        request_id = valid_request_id(request_id)
        agent = self.config.agent(agent_id)
        if agent is None or not agent["enabled"]:
            raise AdvisorError("agent-disabled", "该助手已停用或删除")
        with self.condition:
            self._require_scope(account)
            database = self.stores.account(account)
            row = database.create_thread(user, agent_id, self._source_id(), request_id=request_id,
                                         agent_name=agent["name"], select=True)
            return self._session_read(account, user, agent_id, row["id"])

    def _native_request(self, account, row, native=None, **extra):
        meta = (native or {}).get("metadata") or {}
        return {"account": account, "user": row["user"], "agentId": row["agent_id"], "threadId": row["id"],
                "runtimeSessionId": (native or {}).get("native_session_id", row.get("runtime_session_id")),
                "sourceId": meta.get("native_source_id", row.get("native_source_id") or row["source_id"]),
                **extra}

    def delete_thread(self, account, user, agent_id, thread_id, request_id):
        account = self._require_scope(account)
        valid_scope_value(user, "user")
        valid_request_id(request_id)
        if not valid_identifier(thread_id) or not valid_identifier(agent_id):
            raise AdvisorError("invalid-request", "会话标识不正确")
        with self.condition:
            self._require_scope(account)
            database = self.stores.account(account)
            self._reconcile_stopped_answers(account, database, user)
            self._session_assert_drained(account, thread_id)
            operation = database.session_delete_begin(user, agent_id, thread_id, request_id)
        op = operation.get("operation", operation)
        if op["state"] == "completed":
            return {"deleted": True, "threadId": thread_id, "state": "completed"}
        row = {"id": thread_id, "user": user, "agent_id": agent_id, "source_id": self._source_id()}
        error = None
        try:
            natives = database.session_native_sessions(thread_id)
            if natives:
                config = self._resolve_config()["service"]
                runtime = self._runtime_instance()
                for native in natives:
                    self._require_scope(account)
                    result = runtime.delete_session(config, self._native_request(account, row, native), lambda _: None, threading.Event())
                    if result.get("state") != "completed":
                        raise AdvisorError("engine-unavailable", "原生会话清理尚未确认")
                    database.session_native_deleted(thread_id, native["native_session_id"])
        except Exception:
            error = "删除未完成，可重试"
        with self.condition:
            self._require_scope(account)
            database.session_delete_finish(op["id"], error=error)
            if not error:
                for run_id, run in list(self._runs.items()):
                    if run.account == account and run.thread_id == thread_id:
                        self._runs.pop(run_id, None)
            return {"deleted": error is None, "threadId": thread_id,
                    "state": "failed" if error else "completed", **({"error": error} if error else {})}

    def compact_thread(self, account, user, agent_id, thread_id, request_id, *, _task_context=None):
        request_id = valid_request_id(request_id)
        with self.condition:
            database, row = self._session_scope(account, user, agent_id, thread_id, writable=True)
            # Repeated requests return the durable operation, even while its
            # worker is active; a new compaction cannot overtake a draining answer.
            previous = next((op for op in database.session_operations(thread_id)
                             if op["kind"] == "compact" and op["request_id"] == request_id), None)
            if previous:
                return {"operation": self._operation_payload(previous)}
            self._session_assert_drained(account, thread_id)
            if row.get("pending_replacement"):
                raise AdvisorError("rebuild-required", "修改后的回答尚未完成，请先重试或继续提问，再压缩讨论")
            op = database.session_operation_begin(user, agent_id, thread_id, "compact", request_id,
                metadata={"runtime_session_id": row.get("runtime_session_id"),
                          "config_stamp": self._config_stamp(self._model_service()),
                          "source_scope": self._source_scope(account, row["user"]),
                          "profile_stamp": self._profile_stamp(account, user, fresh=True),
                          "context_epoch": row.get("context_epoch")})
            if op["id"] in self._session_tasks or op["state"] != "running":
                return {"operation": self._operation_payload(op)}
            if not row.get("runtime_session_id") or len(database.thread_messages(thread_id)) < 2:
                op = database.session_operation_update(op["id"], state="noop", result={})
                return {"operation": self._operation_payload(op)}
            # A new profile cannot bless an old native session by summarizing it.
            # The next explicit question performs the attributed rebuild first.
            current_context = database.context(user) or {}
            current_epoch = current_context.get("epoch")
            current_profile = self._profile_stamp(account, user, fresh=True)
            native = next((item for item in database.session_native_sessions(thread_id)
                           if item["native_session_id"] == row["runtime_session_id"]), None)
            native_metadata = (native or {}).get("metadata") or {}
            if (current_context.get("source_id") != self._source_scope(account, row["user"]) or
                    row.get("context_epoch") != current_epoch or row.get("profile_stamp") != current_profile or
                    native_metadata.get("context_epoch") != current_epoch or
                    native_metadata.get("profile_stamp") != current_profile):
                op = database.session_operation_update(op["id"], state="failed",
                    error="资料版本已变化，请在原会话提问以自动恢复上下文")
                self._queue_pause_existing_locked(database, user, agent_id, thread_id, "failed")
                return {"operation": self._operation_payload(op)}
            task = SessionTask(op, account, user, agent_id, database)
            task.config_stamp = self._config_stamp(self._model_service())
            task.source_scope = self._source_scope(account, row["user"])
            task.profile_stamp = self._profile_stamp(account, user, fresh=True)
            task.context_epoch = row.get("context_epoch")
            self._session_tasks[task.id] = task
            from service import _Worker
            task.worker = _Worker()
            try:
                if _task_context is not None:
                    task.task = _task_context
                else:
                    _core_task, task.task = self.plugin.start_task(account, user, kind="interactive", title="聊天助手压缩讨论",
                        params={"operationId": task.id, "threadId": thread_id},
                        handler=lambda core_task: self._compact_task_worker(core_task, task, row))
            except Exception:
                task.worker.done.set()
                task.state = "error"
                op = database.session_operation_update(task.id, state="failed", error="压缩任务启动失败，请重试")
                self._queue_pause_existing_locked(database, user, agent_id, thread_id, "failed")
            return {"operation": self._operation_payload(op)}

    def _resume_compaction(self, core_task, request_id, operation_id):
        database = self.stores.account(core_task.account)
        op = database.session_operation(operation_id)
        if not op or op["user"] != core_task.user or op["kind"] != "compact":
            raise AdvisorError("not-found", "压缩操作不存在")
        row = database.session_thread(core_task.user, op["agent_id"], op["thread_id"])
        self._session_assert_drained(core_task.account, row["id"])
        native_id = (op.get("metadata") or {}).get("runtime_session_id")
        native = next((item for item in database.session_native_sessions(row["id"])
                       if item["native_session_id"] == native_id), None)
        result = self._runtime_instance().inspect_session(self._resolve_config()["service"],
            self._native_request(core_task.account, row, native, operationId=operation_id), lambda _: None,
            core_task.cancelled)
        self._require_scope(core_task.account)
        core_task.check()
        if result.get("state") in {"completed", "noop"}:
            database.session_operation_update(operation_id, state=result["state"], result=result)
            return "done"
        if result.get("state") not in {"idle", "missing", "interrupted", "stopped", "failed"}:
            database.session_operation_update(operation_id, state="unknown", error="压缩状态待核对")
            raise AdvisorError("run-active", "原压缩状态尚未确认，请稍后核对")
        database.session_operation_update(operation_id, state="interrupted", result=result)
        started = self.compact_thread(core_task.account, core_task.user, op["agent_id"], row["id"],
            request_id, _task_context=core_task)
        task = self._session_tasks.get(started["operation"]["id"])
        if task is None:
            return "done" if started["operation"]["state"] in {"completed", "noop"} else "failed"
        return self._compact_task_worker(core_task, task, row)

    def _check_session_task(self, task):
        self._require_scope(task.account)
        if task.cancel.is_set():
            raise AdvisorError("stopping", "压缩已停止")
        if task.task is not None:
            try:
                task.task.check()
            except Cancelled:
                raise AdvisorError("stopping", "压缩已停止") from None
        agent = self.config.agent(task.agent_id)
        history = self._history(task.account, task.user, fresh=True)
        if (self._closing or task.account in self._paused or
                agent is None or not agent["enabled"] or
                task.config_stamp != self._config_stamp(self._model_service(refresh=True)) or
                task.source_scope != history.get("sourceFingerprint") or
                task.source_scope != history.get("currentSourceFingerprint", task.source_scope) or
                task.context_epoch != history.get("epoch") or
                task.profile_stamp != self._profile_stamp(task.account, task.user, fresh=True)):
            from service import stop_task
            stop_task(task.task)
            task.cancel.set()
            raise AdvisorError("stopping", "配置或资料已更新，压缩已中断")

    def _compact_task_worker(self, core_task, task, row):
        from service import stop_task
        with self.condition:
            task.task = core_task
            if task.cancel.is_set() or self._closing or task.account in self._paused:
                stop_task(core_task)
            task.cancel = core_task.cancelled
        try:
            self._compact_worker(task, row)
            return "done" if task.state == "done" else "failed"
        except AdvisorError:
            task.state = "error"
            stop_task(core_task)
            return "failed"
        finally:
            if task.state == "running":
                task.state = "error"
            with self.condition:
                task.worker.done.set()
                self._queue_after_run_locked(task)
                self.condition.notify_all()

    def _compact_worker(self, task, row):
        result = config = None
        try:
            self._check_session_task(task)
            config = self._resolve_config(task.task.token)
            runtime = self._runtime_instance(task.cancel)
            native = next((item for item in task.database.session_native_sessions(row["id"])
                           if item["native_session_id"] == row["runtime_session_id"]), None)
            request = self._native_request(task.account, row, native, operationId=task.id,
                                           contextEpoch=task.context_epoch, profileStamp=task.profile_stamp, thinking=False)
            result = runtime.compact_session(self._generation_service(config), config["token"], request, lambda _: None, task.cancel)
            self._check_session_task(task)
            task.task.progress(label="保存讨论摘要")
        except Exception as exc:
            # Without a model nothing reached the engine, so there is no summary to reconcile.
            if config is None and getattr(exc, "code", None) == "api-not-configured":
                result = {"state": "failed", "error": exc.message}
            # A timed-out/aborted HTTP call may already have committed a summary.
            elif getattr(exc, "code", None) in {"history-too-long", "context-too-long", "native-session-incompatible"}:
                result = {"state": "failed", "error": "当前窗口或资料版本已变化，请在原会话重试提问以恢复上下文"}
            else:
                try:
                    self._check_session_task(task)
                    result = runtime.inspect_session(config["service"], request, lambda _: None, threading.Event())
                    if result.get("state") in {"idle", "missing"}:
                        result["state"] = "interrupted"
                except Exception:
                    result = {"state": "unknown"}
        with self.condition:
            if self._closing or task.account in self._paused:
                state = "unknown"
            else:
                state = result.get("state", "unknown")
            if state not in {"completed", "noop", "failed", "stopped", "unknown", "interrupted"}:
                state = "unknown"
            # Stale summaries remain native data to reconcile, never authorized recovery.
            if not self._closing and task.account not in self._paused:
                try:
                    self._check_session_task(task)
                except AdvisorError as exc:
                    result = {"state": "interrupted", "error": exc.message}
                    state = "interrupted"
            if self._closing or task.account in self._paused:
                task.state = "error"
                return
            self._require_scope(task.account)
            task.database.session_operation_update(task.id, state=state, result=result,
                                                    error="压缩状态待核对" if state == "unknown" else result.get("error"))
            task.state = "done" if state in {"completed", "noop"} else "error"
            self.condition.notify_all()

    def session_operation(self, account, user, thread_id, operation_id=None, request_id=None):
        account = self._require_scope(account)
        valid_scope_value(user, "user")
        database = self.stores.account(account)
        with self.condition:
            self._reconcile_stopped_answers(account, database, user)
        if request_id is not None:
            valid_request_id(request_id)
            op = next((item for item in database.session_operations(thread_id) if item["request_id"] == request_id and item["kind"] == "compact"), None)
            operation_id = op["id"] if op else None
        else:
            op = database.session_operation(operation_id)
        if op is None or op["thread_id"] != thread_id or op["user"] != user:
            raise AdvisorError("not-found", "操作不存在")
        task = self._session_tasks.get(operation_id)
        if op["kind"] == "compact" and op["state"] == "unknown" and not (task and task.worker and task.worker.is_alive()):
            try:
                row = database.session_thread(user, op["agent_id"], thread_id)
                native_id = (op.get("metadata") or {}).get("runtime_session_id")
                native = next((item for item in database.session_native_sessions(thread_id)
                               if item["native_session_id"] == native_id), None)
                result = self._runtime_instance().inspect_session(self._resolve_config()["service"],
                    self._native_request(account, row, native, operationId=operation_id), lambda _: None, threading.Event())
                state = result.get("state")
                if state in {"idle", "missing"}:
                    state = "interrupted"
                if state in {"completed", "stopped", "failed", "interrupted", "noop"}:
                    with self.condition:
                        self._require_scope(account)
                        current = database.session_operation(operation_id)
                        op = (database.session_operation_update(operation_id, state=state, result=result)
                              if current and current["state"] == "unknown" else current or op)
            except Exception:
                pass  # Preserve unknown; never replay an uncertain operation.
        result = {"operation": self._operation_payload(op)}
        if op["kind"] != "delete":
            result.update(self._session_read(account, user, op["agent_id"], thread_id))
        return result

    def stop_session_operation(self, account, user, thread_id, operation_id):
        snapshot = self.session_operation(account, user, thread_id, operation_id)
        with self.condition:
            database = self.stores.account(account)
            op = database.session_operation(operation_id)
            if op and database.queue_get(user, op["agent_id"], thread_id)["version"] > 0:
                database.queue_set_paused(user, op["agent_id"], thread_id, True, "stopped")
        if snapshot["operation"]["state"] not in BUSY:
            return snapshot
        with self.condition:
            task = self._session_tasks.get(operation_id)
            worker_active = bool(task and task.worker and task.worker.is_alive())
            if worker_active:
                from service import stop_task
                stop_task(task.task)
                task.cancel.set()
            else:
                # A timed-out HTTP worker can be gone while native compression
                # is still active. Cancellation must reach that exact operation.
                self._require_scope(account)
                database = self.stores.account(account)
                op = database.session_operation(operation_id)
                if not op or op["kind"] != "compact":
                    raise AdvisorError("invalid-request", "该操作不能停止")
                row = database.session_thread(user, op["agent_id"], thread_id)
                native_id = (op.get("metadata") or {}).get("runtime_session_id")
                native = next((item for item in database.session_native_sessions(thread_id)
                               if item["native_session_id"] == native_id), None)
                request = self._native_request(account, row, native, operationId=operation_id)
        if not worker_active:
            try:
                result = self._runtime_instance().stop_session_operation(self._resolve_config()["service"],
                    request, lambda _: None, threading.Event())
                state = result.get("state", "unknown")
                if state in {"idle", "missing"}:
                    state = "interrupted"
                if state in {"completed", "stopped", "failed", "interrupted", "noop"}:
                    with self.condition:
                        self._require_scope(account)
                        current = database.session_operation(operation_id)
                        if current and current["state"] in BUSY:
                            database.session_operation_update(operation_id, state=state, result=result)
            except Exception:
                pass  # Keep unknown, so the user can retry/reconcile safely.
        # Do not call this stopped until the native operation confirms its state.
        return self.session_operation(account, user, thread_id, operation_id)

    def _record_native_result(self, run, result, config, native_source_id):
        database = run.database
        metadata = {"source_id": config["sourceId"], "model_fp": config["modelFp"],
                    "template_revision": self.config.agent(run.agent_id)["revision"],
                    "context_epoch": run.context_epoch, "profile_stamp": run.profile_stamp,
                    "native_source_id": native_source_id, "config_stamp": run.config_stamp,
                    "source_scope": run.source_scope,
                    "runtime_session_id": result.get("runtimeSessionId")}
        if isinstance(result.get("userMessageId"), str) and result["userMessageId"]:
            metadata["native_message_id"] = result["userMessageId"]
        metadata["pending_revert"] = None
        native = result.get("runtimeSessionId")
        if native:
            database.session_bind_commit(run.thread_id, native, metadata)
        database.session_run_metadata(run.id, **metadata)
        recorded_compactions = {op["request_id"] for op in database.session_operations(run.thread_id)
                                if op["kind"] == "compact"}
        for event in result.get("compactions", []):
            if event.get("operationId") and database.session_operation(event["operationId"]):
                continue
            identity = event.get("id") or event.get("nativeBoundary")
            if not isinstance(identity, str):
                continue
            import hashlib
            key = "auto-" + hashlib.sha256((str(native) + identity).encode()).hexdigest()[:32]
            if key in recorded_compactions:
                continue
            op = database.session_operation_begin(run.user, run.agent_id, run.thread_id, "compact", key,
                                                  metadata={**metadata, "automatic": event.get("kind") != "manual"}, run_id=run.id)
            database.session_operation_update(op["id"], state="completed", result=event)
            recorded_compactions.add(key)
        usage = result.get("usage")
        if isinstance(usage, dict):
            count = usage.get("totalInputTokens", usage.get("inputTokens"))
            if type(count) in (int, float) and 0 < count < 10**10:
                database.session_usage_save(run.thread_id, run.id, {**usage, "inputTokens": count, "contextTokens": config["contextTokens"],
                    "recordedAtMs": int(time.time() * 1000), "configStamp": run.config_stamp,
                    "sourceScope": run.source_scope,
                    "profileStamp": run.profile_stamp, "contextEpoch": run.context_epoch, "runtimeSessionId": native})
