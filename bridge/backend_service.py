"""Core application service: account scope, chat browsing and shared configuration.

Core maintains the WeChat chat data that entered this software and the model
profile that plugins reach through the model gateway. It contains no analysis
method, prompt or model-usage strategy; those belong to plugins.

Modified by Know Your Friends, 2026-10-10: prefer safe source startup diagnostics.
Modified by Know Your Friends, 2026-10-11: expose forward history scan cursors.
Modified by Know Your Friends, 2026-10-13 (Apache-2.0): plugin architecture. The
legacy Laya/API label and portrait pipeline is retired; saved portraits stay readable.
Account data (projection, 关系记忆) is Core's AccountDataService; the assistant is the kyf.advisor plugin.
Modified by Know Your Friends, 2026-10-16 (Apache-2.0): preserve source identity on deadline edits.
"""
from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from pathlib import Path

from backend_contracts import (AccountChangedError, AccountUnavailableError, LOCAL_SOURCE_ID,
                               MAX_ISSUED_IMAGES, ROOT, model_source_failure)
from conversation_selection import ConversationSelectionStore, _session_id
from core_contracts import effective_model_budget
from data_root_source import DataRootSource
from history_browser import browse as browse_history, search as search_history
from model_probe import ModelProbe
from model_source import (ModelSourceStore, ModelSourceUnavailable, _MISSING, connection_values,
                          model_concurrency, model_list_values, model_name)
from model_trace_context import traced_execution
from usage_names import UsageNameCache
from wechat_source import WeChatSource


def _legacy_max_concurrent():
    """The old global scheduling total becomes the migrated main model's concurrency."""
    try:
        doc = json.loads((ROOT / ".local" / "core" / "model" / "settings.json")
                         .read_text(encoding="utf-8"))
        value = doc.get("values", {}).get("maxConcurrentTasks") if isinstance(doc, dict) else None
        return value if type(value) is int and 1 <= value <= 8 else 4
    except Exception:
        return 4


class Backend:
    def __init__(self, source, model_source_store=None, selection_store=None, data_root_store=None,
                 model_probe=None):
        self.source = source
        self.usage_names = UsageNameCache()
        self._model_traces = None
        self._model_traces_lock = threading.Lock()
        self.data_root_store = data_root_store or DataRootSource(ROOT)
        self.model_source_store = model_source_store or ModelSourceStore(
            ROOT / ".local" / "real-client-runtime" / "api-model-source.json", root=ROOT,
            legacy_path=ROOT / ".local" / "real-client-runtime" / "model-source.json",
            initial_concurrency=_legacy_max_concurrent())
        self.model_probe = model_probe or ModelProbe(ROOT, gateway=lambda: self.core_services().gateway)
        self.model_lock = threading.RLock()
        self.model_source_revision = 0
        self.model_list_revision = 0
        self.model_listeners = []
        self.selection_store = selection_store or ConversationSelectionStore(
            ROOT / ".local" / "real-client-data")
        self.jobs_lock = threading.Lock()
        self.request_condition = threading.Condition()
        self.active_requests = 0
        self.closing = False
        self.account_clear_paused = False
        self.core_url = None
        self._core = None
        # The shared-Core leases live on the backend so the idle-exit monitor needs
        # no eager plugin-host construction; CoreServices reuses this instance.
        from core_leases import LeaseService
        self.leases = LeaseService()

    def background_status(self):
        """Whether MCP clients still use this Core; the GUI reads it before stopping the bridge.

        Uses only the already-constructed plugin host; nothing here builds one.
        """
        mcp = getattr(self._core, "mcp", None)
        return {"mcpConnections": mcp.active_connections() if mcp is not None else 0,
                "guiLeases": self.leases.count("gui"),
                "holders": self.leases.holders()}

    # ------------------------------------------------------------------ services
    @property
    def model_traces(self):
        with self._model_traces_lock:
            if self._model_traces is None:
                from model_traces import ModelTraceService
                def current_account():
                    try:
                        return self._status_identity()[0]
                    except Exception:
                        return None
                self._model_traces = ModelTraceService(ROOT, current_account=current_account,
                    current_account_version=lambda: getattr(self.source, "advisor_binding_generation", lambda: 0)())
            return self._model_traces

    def account_data(self):
        """Per-account databases, the chat projection and 关系记忆 (created on first use)."""
        with self.jobs_lock:
            service = getattr(self, "_accounts", None)
            if service is None:
                if self.closing:
                    raise RuntimeError("bridge is closing")
                from core_accounts import AccountDataService
                service = self._accounts = AccountDataService(self.source, ROOT)
                # Human-data changes and opened analysis windows are announced to plugins.
                service.friends.on_changed = lambda account, user, version: self._core_event(
                    "human_changed", account, user, version)
                service.friends.on_opened = lambda account, user: self._core_event("friend_opened", account, user)
            return service

    def core_services(self):
        """The plugin host: registry, supervisor, tasks, events and the plugin interface."""
        with self.jobs_lock:
            if self._core is None:
                if self.closing:
                    raise RuntimeError("bridge is closing")
                from core_services import CoreServices
                self._core = CoreServices(self, ROOT, self.core_url or "http://127.0.0.1:0")
            return self._core

    def _core_event(self, name, *args):
        core = self._core
        if core is None or self.closing:
            return
        try:
            if name == "human_changed":
                core.events.human_changed(*args)
            else:
                core.friend_opened(*args)
        except Exception:
            pass

    def _observe_window(self, account, workdir, user, messages, has_more_before):
        accounts = getattr(self, "_accounts", None)
        if accounts is None or self.closing or type(has_more_before) is not bool:
            return
        try:
            accounts.observe_message_window(account, workdir, user, messages, has_more_before)
        except Exception:
            # Chat-data maintenance for plugins must not break the existing chat display.
            return

    def _invalidate_source(self):
        # Plugins notice the changed source through the history status fingerprint.
        invalidate = getattr(self.source, "invalidate_advisor_identity", None)
        if callable(invalidate):
            invalidate()

    # ------------------------------------------------------------- lifecycle
    @contextmanager
    def request_lease(self):
        """Track HTTP requests so an account clear can drain them."""
        with self.request_condition:
            if self.closing:
                raise RuntimeError("bridge is closing")
            self.active_requests += 1
        try:
            yield
        finally:
            with self.request_condition:
                self.active_requests -= 1
                self.request_condition.notify_all()

    def pause_for_account_clear(self, account):
        """Drain this bridge's work while retaining a way to resume after a failed clear."""
        with self.request_condition:
            if self.closing:
                raise RuntimeError("bridge is closing")
            self.closing = True
        self.usage_names.clear(account)
        accounts = getattr(self, "_accounts", None)
        if accounts is not None:
            accounts.pause_for_account_clear(account)
        with self.request_condition:
            if not self.request_condition.wait_for(lambda: self.active_requests == 0, timeout=200):
                raise RuntimeError("bridge requests did not finish")
        self.account_clear_paused = True
        try:
            forget = getattr(self.source, "forget_account", None)
            if callable(forget):
                forget(account)
        except Exception:
            self.resume_after_failed_account_clear()
            raise

    def resume_after_failed_account_clear(self):
        """Restore this bridge if scoped file removal failed after it was drained."""
        with self.request_condition:
            if not self.account_clear_paused or self.active_requests:
                raise RuntimeError("bridge cannot safely resume")
        accounts = getattr(self, "_accounts", None)
        if accounts is not None:
            accounts.resume_after_failed_account_clear()
        with self.request_condition:
            self.account_clear_paused = False
            self.closing = False

    def shutdown(self, account=None):
        """Stop this bridge on normal exit or after an active-account clear."""
        with self.request_condition:
            first = not self.closing
            paused = self.account_clear_paused
            self.closing = True
            self.account_clear_paused = False
        self.usage_names.close()
        accounts = getattr(self, "_accounts", None)
        if accounts is not None:
            accounts.shutdown(account)
        if first or paused:
            if account is not None:
                forget = getattr(self.source, "forget_account", None)
                if callable(forget):
                    forget(account)
            close_source = getattr(self.source, "close", None)
            if callable(close_source):
                close_source()
        with self.request_condition:
            if not self.request_condition.wait_for(lambda: self.active_requests == 0, timeout=200):
                raise RuntimeError("bridge requests did not finish")
        core = self._core
        if core is not None:
            core.close()
        if self._model_traces is not None:
            self._model_traces.close()

    # ----------------------------------------------------------------- scope
    def _status_identity(self):
        """Polled status views reuse a strong check from the last few seconds (the advisor lease).

        Each fresh check is a Windows ownership query of about a second under the source lock;
        health and trace polling must not keep chat reads waiting behind it.
        """
        leased = getattr(self.source, "advisor_identity", None)
        return leased(messages=False) if callable(leased) else self.source.identity()

    def _selection_account(self):
        """Resolve the real login without requiring message shards."""
        if self.closing:
            raise AccountUnavailableError()
        verified = getattr(self.source, "verified_identity", None)
        if callable(verified):
            account, _workdir = verified(messages=False)
        else:
            account, _workdir = self.source.identity()
        if not isinstance(account, str) or not account:
            raise AccountUnavailableError()
        return account

    def _scoped_identity(self):
        if self.closing:
            raise AccountUnavailableError()
        verified = getattr(self.source, "verified_identity", None)
        if callable(verified):
            account, workdir = verified(messages=True)
        else:
            ready = getattr(self.source, "require_messages_ready", None)
            if callable(ready):
                ready()
            account, workdir = self.source.identity()
        return str(account), str(Path(workdir).resolve())

    def _assert_scope(self, scope):
        if self.closing:
            raise AccountChangedError()
        if self._scoped_identity() != tuple(scope):
            raise AccountChangedError()

    # ----------------------------------------------------- conversation list
    def conversation_selection(self):
        account = self._selection_account()
        state = self.selection_store.get(account)
        if self._selection_account() != account:
            raise AccountChangedError()
        return state

    def set_conversation_selected(self, expected_account, session, selected):
        if not isinstance(expected_account, str) or not expected_account:
            raise ValueError("invalid expected account")
        _session_id(session)
        if type(selected) is not bool:
            raise ValueError("invalid selected")
        account = self._selection_account()
        if account != expected_account:
            raise AccountChangedError()
        if selected:
            metadata = self.source.sessions()
            if metadata.get("account") != account:
                raise AccountChangedError()
            if session not in {item.get("username") for item in metadata.get("sessions", [])}:
                raise ValueError("unknown session")
        elif session not in self.selection_store.get(account)["selectedSessions"]:
            raise ValueError("session not selected")
        if self._selection_account() != account:
            raise AccountChangedError()
        state = self.selection_store.set_selected(account, session, selected)
        if self._selection_account() != account:
            raise AccountChangedError()
        return state

    def set_conversation_all_selected(self, expected_account):
        """Put every conversation of the current WeChat account into the sidebar at once."""
        if not isinstance(expected_account, str) or not expected_account:
            raise ValueError("invalid expected account")
        account = self._selection_account()
        if account != expected_account:
            raise AccountChangedError()
        metadata = self.source.sessions()
        if metadata.get("account") != account:
            raise AccountChangedError()
        sessions = [item.get("username") for item in metadata.get("sessions", [])]
        if self._selection_account() != account:
            raise AccountChangedError()
        state = self.selection_store.set_all_selected(account, sessions)
        if self._selection_account() != account:
            raise AccountChangedError()
        return state

    def remove_conversations_selected(self, expected_account, sessions):
        if (not isinstance(sessions, list) or not 1 <= len(sessions) <= 1000 or
                len(set(_session_id(session) for session in sessions)) != len(sessions)):
            raise ValueError("invalid session selection")
        account = self._selection_account()
        if account != expected_account:
            raise AccountChangedError()
        state = self.selection_store.remove_selected(account, sessions)
        if self._selection_account() != account:
            raise AccountChangedError()
        return state

    # ---------------------------------------------------------------- status
    def health(self):
        state = "idle"
        error = None
        account = None
        if isinstance(self.source, WeChatSource) or getattr(self.source, "db", None) is not None:
            try:
                account, _ = self._status_identity()
                state = "ready"
            except AccountUnavailableError as exc:
                state, error = "account-unavailable", str(exc)
            except Exception as exc:
                state, error = "error", str(exc)
        data = {"state": state}
        if account:
            data["account"] = account
        if error:
            data["error"] = error
        preparation = self.preparation_status(account)
        if preparation is not None and state != "ready":
            data["preparation"] = preparation
        return {"ok": state not in ("error", "account-unavailable"), "data": data, "version": "real-ui-1"}

    def preparation_status(self, account=None):
        status = getattr(self.source, "preparation_status", None)
        if not callable(status):
            status = getattr(getattr(self.source, "factory", None), "preparation_status", None)
        if not callable(status):
            return None
        result = status(account)
        if not isinstance(result, dict):
            return None
        safe = {key: result[key] for key in ("reason", "state", "message", "retryAfterSeconds") if key in result}
        return safe or None

    def data_root_status(self):
        return self.data_root_store.status()

    def configure_data_root(self, value):
        result = self.data_root_store.select(value)
        self._invalidate_source()
        return result

    def clear_data_root(self):
        result = self.data_root_store.clear()
        self._invalidate_source()
        return result

    # ---------------------------------------------------------- model profile
    def model_source(self):
        """The saved generation model all plugins reach through Core's model routing."""
        with self.model_lock:
            selected = self.model_source_store.saved_selection()
            if isinstance(selected.get("api"), dict):
                result = self.model_source_store.public("api", selected["sourceId"], "active")
            else:
                result = self.model_source_store.public("none", LOCAL_SOURCE_ID, "active")
            core = getattr(self, "_core", None)
            if core is not None:
                result["localJudge"] = core.local_judge.status()
            else:
                from core_judge import JudgeService
                result["localJudge"] = JudgeService.unprepared_status(ROOT, self.model_source_store)
            return result

    def model_source_local_judge(self, operation, request):
        if request != {} or operation not in ("prepare", "cancel", "delete"):
            raise ValueError("invalid model source request")
        service = self.core_services().local_judge
        getattr(service, operation)()
        return self.model_source()

    def add_model_listener(self, callback):
        self.model_listeners.append(callback)

    def _model_changed(self):
        # Plugins re-check the model service stamp and stop their own stale runs.
        self.model_source_revision += 1
        for callback in list(self.model_listeners):
            try:
                callback()
            except Exception:
                pass

    def model_source_list(self, request):
        if not isinstance(request, dict):
            raise ValueError("invalid model source request")
        model_id = self._model_entry_id(request["id"]) if "id" in request else None
        values = connection_values({key: value for key, value in request.items() if key != "id"})
        with self.model_lock:
            self.model_list_revision += 1
            revision = self.model_list_revision
        key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                  values["apiKey"], model_id)
        try:
            reply = self.model_probe.model_list(values["protocol"], values["baseUrl"], key)
        except Exception as exc:
            raise model_source_failure(exc, "model list unavailable") from exc
        result = model_list_values(reply)
        with self.model_lock:
            # A slow response from an older discovery must not replace the list
            # requested later, even if that newer request failed or was cancelled.
            if not self.closing and revision == self.model_list_revision:
                self.model_source_store.save_model_list(values["protocol"], values["baseUrl"], result)
        return result

    def model_source_reveal_key(self, request):
        if not isinstance(request, dict) or not set(request) <= {"protocol", "baseUrl", "id"}:
            raise ValueError("invalid model source request")
        values = connection_values({key: value for key, value in request.items() if key != "id"})
        model_id = self._model_entry_id(request["id"]) if "id" in request else None
        with self.model_lock:
            key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                      None, model_id)
            if key is None:
                raise ModelSourceUnavailable("saved API key unavailable")
            return {"apiKey": key}

    def _test_connection(self, values, key):
        try:
            reply = self.model_probe.model_test(values["protocol"], values["baseUrl"], key, values["model"])
        except Exception as exc:
            raise model_source_failure(exc, "model connection failed") from exc
        latency = reply.get("latencyMs") if isinstance(reply, dict) else None
        if (not isinstance(reply, dict) or reply.get("ok") is not True or
                type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0):
            raise ModelSourceUnavailable("model connection failed")
        return latency

    @traced_execution("connection-test", lambda self, request: {"accountId": None}, lambda *args: "not-applicable")
    def model_source_test(self, request):
        if not isinstance(request, dict) or "id" in request and "model" not in request:
            raise ValueError("invalid model source request")
        model_id = self._model_entry_id(request["id"]) if "id" in request else None
        values = connection_values({key: value for key, value in request.items() if key != "id"},
                                   require_model=True)
        key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                  values["apiKey"], model_id)
        return {"ok": True, "latencyMs": self._test_connection(values, key)}

    @traced_execution("connection-test", lambda self, request: {"accountId": None}, lambda *args: "not-applicable")
    def model_source_activate(self, request):
        if not isinstance(request, dict) or request.get("mode") != "api":
            raise ValueError("invalid model source request")
        values = connection_values({key: value for key, value in request.items() if key != "mode"},
                                   require_model=True)
        values["contextTokens"] = effective_model_budget(values["contextTokens"])["contextTokens"]
        with self.model_lock:
            key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"], values["apiKey"])
            revision = self.model_source_revision
            saved_profile = self.model_source_store.saved_selection()["api"]
            # Older clients omit the new field; preserve the user's chosen deadline.
            if "timeoutMs" not in request and saved_profile is not None:
                values["timeoutMs"] = saved_profile["timeoutMs"]
            reuse_validated = (values["apiKey"] is None and key is not None and saved_profile is not None and
                               (saved_profile["protocol"], saved_profile["baseUrl"], saved_profile["model"]) ==
                               (values["protocol"], values["baseUrl"], values["model"]))
        if not reuse_validated:
            self._test_connection(values, key)
        with self.model_lock:
            if self.closing or revision != self.model_source_revision:
                raise ModelSourceUnavailable("model source changed during connection test")
            # Reusing a key must reuse its ciphertext too: re-encryption would change
            # the service stamp and cancel live work after a timeout-only edit.
            self.model_source_store.save_api(values["protocol"], values["baseUrl"], values["model"], values["apiKey"],
                                             context_tokens=values["contextTokens"], timeout_ms=values["timeoutMs"])
            self._model_changed()
            return self.model_source()

    def model_source_clear_key(self, request):
        if request != {}:
            raise ValueError("invalid model source request")
        with self.model_lock:
            self.model_source_store.clear_key()
            self._model_changed()
            return self.model_source()

    def _model_entry_id(self, value):
        if (not isinstance(value, str) or len(value) != 32 or
                any(char not in "0123456789abcdef" for char in value)):
            raise ValueError("invalid model source request")
        return value

    def model_source_models_save(self, request):
        """Add or edit one model; the connection is tested before anything is written."""
        if not isinstance(request, dict):
            raise ValueError("invalid model source request")
        if not request.keys() <= {"id", "name", "protocol", "baseUrl", "apiKey", "model",
                                  "contextTokens", "timeoutMs", "concurrency"} or \
                not {"protocol", "baseUrl", "model"} <= request.keys():
            raise ValueError("invalid model source request")
        entry_id = self._model_entry_id(request["id"]) if "id" in request else None
        fields = {key: request[key] for key in ("protocol", "baseUrl", "model", "apiKey") if key in request}
        if "contextTokens" in request:
            fields["contextTokens"] = request["contextTokens"]
        if "timeoutMs" in request:
            fields["timeoutMs"] = request["timeoutMs"]
        values = connection_values(fields, require_model=True)
        name = model_name(request.get("name", ""))
        concurrency = model_concurrency(request["concurrency"]) if "concurrency" in request else None
        context_tokens = values["contextTokens"] if "contextTokens" in fields else _MISSING
        timeout_ms = values["timeoutMs"] if "timeoutMs" in fields else _MISSING
        with self.model_lock:
            revision = self.model_source_revision
            key = self.model_source_store.resolve_key(values["protocol"], values["baseUrl"],
                                                      values["apiKey"], entry_id)
            unchanged = False
            if entry_id is not None and values["apiKey"] is None:
                entry = next((item for item in self.model_source_store.models()["models"]
                              if item["id"] == entry_id), None)
                if entry is None or entry.get("builtin"):
                    raise ValueError("unknown model")
                unchanged = ((key is not None or values["protocol"] == "ollama") and
                             (entry["protocol"], entry["baseUrl"], entry["model"]) ==
                             (values["protocol"], values["baseUrl"], values["model"]))
        if not unchanged:
            self._test_connection(values, key)
        with self.model_lock:
            if self.closing or revision != self.model_source_revision:
                raise ModelSourceUnavailable("model source changed during connection test")
            self.model_source_store.save_model(
                entry_id, name=name, protocol=values["protocol"], base_url=values["baseUrl"],
                model=values["model"], key=values["apiKey"], context_tokens=context_tokens,
                timeout_ms=timeout_ms, concurrency=concurrency)
            self._model_changed()
            return self.model_source()

    def model_source_models_delete(self, request):
        if not isinstance(request, dict) or set(request) != {"id"}:
            raise ValueError("invalid model source request")
        entry_id = self._model_entry_id(request["id"])
        with self.model_lock:
            entry = next((item for item in self.model_source_store.models()["models"]
                          if item["id"] == entry_id), None)
            if entry is not None and entry.get("builtin") == "julia-1":
                self.core_services().local_judge.delete()
                return self.model_source()
            self.model_source_store.delete_model(entry_id)
            self._model_changed()
            return self.model_source()

    def model_source_roles(self, request):
        if not isinstance(request, dict) or not request or set(request) - {"main", "judge", "image"}:
            raise ValueError("invalid model source request")
        parsed = {}
        for role, target in request.items():
            parsed[role] = None if target is None else self._model_entry_id(target)
        with self.model_lock:
            self.model_source_store.set_roles(**parsed)
            self._model_changed()
            return self.model_source()

    # ---------------------------------------------------------- chat browsing
    def messages(self, user, limit):
        account, workdir = self._scoped_identity()
        messages = self.source.messages(user, limit)
        self._assert_scope((account, workdir))
        self._observe_window(account, workdir, user, messages, getattr(messages, "has_more_before", None))
        return {"messages": [{key: value for key, value in item.items() if not key.startswith("_")} for item in messages],
                "account": account, "total": None,
                **({"hasMoreBefore": messages.has_more_before}
                   if type(getattr(messages, "has_more_before", None)) is bool else {})}

    def message_windows(self, requested_account, users):
        if not isinstance(self.source, WeChatSource):
            ready = getattr(self.source, "require_messages_ready", None)
            if callable(ready):
                ready()
        windows = self.source.message_windows(users, 80, expected_account=requested_account)
        if getattr(self, "_accounts", None) is not None:
            verified = getattr(windows, "verified_scope", None)
            if isinstance(verified, tuple) and len(verified) == 2:
                account, workdir = verified
            else:
                account, workdir = self._scoped_identity()
            if account != requested_account:
                raise AccountChangedError()
            for user in users:
                self._observe_window(account, workdir, user, windows[user],
                                             getattr(windows, "has_more_before", {}).get(user))
        return {"account": requested_account, "windows": [
            {"user": user, "messages": [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in windows[user]],
             **({"hasMoreBefore": windows.has_more_before[user]}
                if user in getattr(windows, "has_more_before", {}) else {})}
            for user in users]}

    def history(self, requested_account, user, *, before=None, around=None, after=None, limit=80):
        account, workdir = self._scoped_identity()
        if requested_account != account:
            raise AccountChangedError()
        page = browse_history(self.source, account, user, before=before, around=around,
                              after=after, limit=limit, max_issued_images=MAX_ISSUED_IMAGES)
        self._assert_scope((account, workdir))
        return {"account": account, "user": user,
                "messages": [{key: value for key, value in item.items() if not key.startswith("_")}
                             for item in page["messages"]],
                "hasMoreBefore": page["hasMoreBefore"],
                "hasMoreAfter": page["hasMoreAfter"], "nextCursor": page["nextCursor"],
                "oldestCursor": page["oldestCursor"], "newestCursor": page["newestCursor"],
                "focusId": page["focusId"]}

    def history_search(self, requested_account, user, *, query=None, day=None,
                       category="all", before=None, limit=50):
        account, workdir = self._scoped_identity()
        if requested_account != account:
            raise AccountChangedError()
        page = search_history(self.source, account, user, query=query, day=day,
                              category=category, before=before, limit=limit,
                              max_issued_images=MAX_ISSUED_IMAGES)
        self._assert_scope((account, workdir))
        return {"account": account, "user": user,
                "messages": [{key: value for key, value in item.items() if not key.startswith("_")}
                             for item in page["messages"]],
                "hasMore": page["hasMore"], "nextCursor": page["nextCursor"]}

    # --------------------------------------------------- retired analysis data
    def legacy_portrait(self, requested_account, user):
        """Read-only saved portraits of the retired WechatVibe analysis; no model is called."""
        account, workdir = self._scoped_identity()
        if requested_account != account:
            raise AccountChangedError()
        from legacy_portrait import read_legacy_portrait
        result = read_legacy_portrait(account, user)
        self._assert_scope((account, workdir))
        return {"account": account, "user": user, **result}
