"""Business execution provenance, carried through private worker pipes only.

Diagnostics never decide whether work runs, retries, or commits. No prompt or
credential is copied into this context, and no viewer selection is consulted.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import json
import re
import uuid
from urllib.parse import quote, unquote

_execution = ContextVar("model_trace_execution", default=None)


def current_trace_context(process_id=None):
    value = _execution.get()
    if not value:
        return None
    context = dict(value[1])
    if process_id is not None:
        binder = getattr(value[0], "bind_transport", None)
        if callable(binder):
            try:
                context.update(binder(context["producerId"], process_id))
            except Exception:
                context["collecting"] = False
    return context


def trace_task_event(kind, **metadata):
    value = _execution.get()
    if value:
        try:
            value[0].task_event(value[1], {"recordId": value[1]["taskId"] + "-" + uuid.uuid4().hex,
                                          "kind": kind, **metadata})
        except Exception:
            pass  # Diagnostic faults cannot change business processing.


def trace_task_outcome(status):
    value = _execution.get()
    if value:
        value[1]["terminalStatus"] = status


def trace_bind_result(response):
    value = _execution.get()
    if value:
        value[1]["lastRecordId"] = response.get("traceRecordId") if isinstance(response, dict) else None


def trace_record_outcome(status, adoption="pending", *, result=None, config=None, error_code=None):
    """Record a Python parser decision using the worker's exact attempt ID.

    Sanitization precedes the diagnostic queue. Parsed business objects stay in
    business ownership; only a bounded, sanitized projection is copied here.
    """
    value = _execution.get()
    if not value or not value[1].get("lastRecordId"):
        return
    try:
        available = getattr(value[0], "capture_available", None)
        if callable(available) and not available(value[1]):
            return
        from model_traces import clean_url, sanitize
        secrets = [value[1]["producerId"], (config or {}).get("apiKey")]
        variants = set()
        for secret in secrets:
            if isinstance(secret, str) and secret:
                variants.update((secret, quote(secret, safe=""), json.dumps(secret)[1:-1]))
        remaining = 48 * 1024
        def safe(item, depth=0):
            nonlocal remaining
            if depth > 12 or remaining <= 0:
                return "[应用结果字段已省略]"
            if isinstance(item, str):
                if len(item) > 8192 or len(item) * 4 > remaining:
                    return "[应用结果字段超限，已省略]"
                remaining -= len(item) * 4 + 32
                decoded = unquote(re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), item))
                if any(secret in decoded for secret in variants):
                    item = decoded
                for secret in sorted(variants, key=len, reverse=True):
                    item = item.replace(secret, "[已脱敏]")
                return re.sub(r'https?://[^\s"<>]+', lambda match: clean_url(match[0]), item)
            if isinstance(item, dict):
                return {safe(str(key), depth+1): safe(child, depth+1) for key, child in list(item.items())[:128]}
            if isinstance(item, (list, tuple)):
                return [safe(child, depth+1) for child in item[:256]]
            remaining -= 32
            return item if item is None or isinstance(item, (bool, int, float)) else "[字段已省略]"
        projection = sanitize(safe(result)) if result is not None else None
        if projection is not None and len(json.dumps(projection, ensure_ascii=False).encode("utf-8")) > 48 * 1024:
            projection = {"omitted": "应用处理结果超过诊断信封预算；处理状态仍已保留"}
        value[0].record_outcome(value[1], value[1]["lastRecordId"], status, adoption,
                                projection, error_code)
    except Exception:
        pass


def _service(owner):
    backend = getattr(owner, "backend", owner)
    service = getattr(backend, "_model_traces", None)
    if service is not None:
        return service
    advisor = getattr(owner, "advisor", getattr(owner, "service", owner))
    factory = getattr(advisor, "trace_service", None)
    return factory() if callable(factory) else None


def run_scope(run):
    return {"accountId": run.account, "contactId": run.user,
            "businessJobId": run.id, "sessionId": getattr(run, "thread_id", None),
            "trigger": getattr(run, "trigger", "background")}


def job_scope(key, job, contact_index=1):
    return {"accountId": key[0], "contactId": key[contact_index],
            "businessJobId": str(job.get("id") or job.get("jobId") or ""),
            "trigger": "background"}


def traced_execution(source, scope, outcome=None):
    """Create a fresh diagnostic execution at each business worker entry.

    Nested helpers reuse their parent. Worker retries stay inside the decorated
    execution; a resumed business worker invocation receives a new task ID.
    """
    def decorate(function):
        @wraps(function)
        def wrapped(owner, *args, **kwargs):
            if _execution.get() is not None:
                return function(owner, *args, **kwargs)
            service, context, token = None, None, None
            try:
                service = _service(owner)
                if service is not None:
                    context = service.register_execution({"source": source, "trigger": "manual",
                                                          **scope(owner, *args, **kwargs)})
                    context["endpoint"] = getattr(service, "transport_url", None)
                    token = _execution.set((service, context))
                    service.task_event(context, {"kind": "preparing"})
            except Exception:
                if token is not None:
                    _execution.reset(token)
                service, context, token = None, None, None
            failed = False
            try:
                return function(owner, *args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                if context is not None:
                    try:
                        status = outcome(owner, *args, **kwargs) if outcome else context.get("terminalStatus")
                        if failed:
                            status = "failed"
                        # A swallowed business error is read from its authoritative
                        # run/job state; an unknown state must never claim adoption.
                        application, adoption = {
                            "done": ("success", "adopted"),
                            "completed": ("success", "adopted"),
                            "success": ("success", "adopted"),
                            "not-applicable": ("success", "not-applicable"),
                            "error": ("failed", "unknown"),
                            "failed": ("failed", "unknown"),
                            "cancelled": ("cancelled", "discarded"),
                            "stopped": ("cancelled", "discarded"),
                            "stale": ("success", "discarded"),
                        }.get(status, ("unknown", "unknown"))
                        service.outcome(context, application, adoption)
                        service.unregister(context["producerId"])
                    except Exception:
                        pass
                    finally:
                        if token is not None:
                            _execution.reset(token)
        return wrapped
    return decorate
