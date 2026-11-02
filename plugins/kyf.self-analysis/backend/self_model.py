"""Shared model-job plumbing for 自我剖析: one account-scoped job runs as a Core task.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The page starts a job and polls ``view()``; results stay in the plugin process.
Model calls carry the task's execution token, and failures are reported as short
public categories because provider bodies may contain private request data.
"""
import json
import re
import subprocess
import threading
import time
import uuid

from kyf_plugin import Cancelled, CoreError, ModelClient
from self_contracts import SelfError, now_ms

ATTEMPTS = 2
_CLIENTS_LOCK = threading.Lock()
# The SDK waits for a reply without a deadline; after an interrupted upstream
# stream (or a restarted gateway) that reply never comes. Past this long a call
# counts as a timeout and frees its worker thread: healthy calls finish within
# about four minutes, and evidence windows are requeued at five.
CALL_DEADLINE_SECONDS = 330
RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)


class ModelContext:
    """Cancel one local call/window without revoking the enclosing Core task."""
    def __init__(self, parent):
        self.parent = parent
        self.local_cancelled = threading.Event()
        self.cancelled = self

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def is_set(self):
        return self.local_cancelled.is_set() or self.parent.cancelled.is_set()

    def set(self):
        self.local_cancelled.set()

    def wait(self, timeout=None):
        until = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if until is None else until - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.local_cancelled.wait(.05 if remaining is None else min(.05, remaining))
        return True

    def check(self):
        self.parent.check()
        if self.local_cancelled.is_set():
            raise Cancelled()


class _JobModelClient(ModelClient):
    """An owned worker can be retired even while service discovery is returning."""
    def __init__(self, plugin):
        super().__init__(plugin)
        self.retired = False

    def _start(self):
        with self.lock:
            if self.retired:
                raise Cancelled()
            return super()._start()

    def close(self):
        # Closing stdin alone waits for Node's pending requests. A hung response
        # must release the real worker and its pipe, not just the Python caller.
        with self.lock:
            self.retired = True
            process, self.process = self.process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        for waiter in list(self.pending.values()):
            waiter["event"].set()

    def _read(self, process):
        try:
            super()._read(process)
        finally:
            process.stdout.close()


def retry_unavailable(action, attempts=7):
    """A long sweep makes thousands of loopback calls; one Core blip must not kill it.

    Core can stay unreachable for tens of seconds while it is busy, so the waits
    grow to about one minute in total before the stage reports the failure."""
    for attempt in range(attempts):
        try:
            return action()
        except CoreError as exc:
            if getattr(exc, "code", None) != "core-unavailable" or attempt + 1 >= attempts:
                raise
            time.sleep(min(15, 2 ** attempt))


# Core answers these when the unified gateway is starting, switching models or its
# ``/models`` probe misses its two-second deadline under load.
GATEWAY_CODES = ("gateway-start", "gateway-unavailable", "upstream-unavailable")


def _status(exc):
    """SDK model errors carry ``http_status``; Core API errors carry ``status``."""
    status = getattr(exc, "http_status", None)
    return status if status is not None else getattr(exc, "status", None)


def transient(exc):
    """Failures that clear by themselves: Core blips and a busy or restarting model service."""
    code, status = getattr(exc, "code", None), _status(exc)
    return (code in ("core-unavailable", "network", "network-error", "timeout", "rate-limit", "empty-response",
                     *GATEWAY_CODES) or status in RETRYABLE_HTTP)


def service_ready(plugin, attempts=3):
    """Check the model route before a job starts; a gateway blip gets a few short retries."""
    for attempt in range(attempts):
        try:
            return plugin.models.service(refresh=True)
        except Exception as exc:
            if attempt + 1 >= attempts or not transient(exc):
                log = getattr(plugin, "log", None)
                if log is not None:
                    try:
                        log(f"model service check failed: {getattr(exc, 'code', None) or type(exc).__name__}",
                            "error")
                    except Exception:
                        pass
                raise
            time.sleep(attempt + 1)


def failure(exc, fallback="生成未完成，请重试"):
    code, status = getattr(exc, "code", None), _status(exc)
    if code == "api-not-configured":
        return "尚未配置回复模型，请先在模型设置中配置"
    if code == "core-unavailable":
        return "与核心服务的连接曾短暂中断，请继续分析"
    if code == "auth":
        return "模型服务认证失败，请检查模型设置"
    if code == "context-too-long":
        return "内容超过模型上下文，请检查模型的上下文长度设置"
    if code == "output-truncated":
        return "模型输出达到长度上限被截断，请继续重试"
    if code in ("invalid-output", "empty-response"):
        return "模型返回的内容无法使用，请重试"
    if status in RETRYABLE_HTTP or code in ("network", "network-error", "timeout", "rate-limit", *GATEWAY_CODES):
        return "模型服务连接失败或繁忙，请稍后重试"
    if isinstance(exc, SelfError):
        return exc.message
    return fallback


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
        raise SelfError("invalid-output", "模型返回的内容无法使用",
                        "只输出一个完整JSON对象；若上一轮内容被截断，请压缩输出长度")
    return value


def format_amend(request, note):
    """Build a ``generate`` amend that appends the rejection reason to the system prompt."""
    def amend(exc):
        if getattr(exc, "code", None) == "output-truncated":
            # The answer was cut at the cap, not rejected: only a shorter one passes.
            return {**request, "system": request["system"] +
                    "\n\n上一轮输出达到长度上限被截断，本轮必须把输出压缩到要求的字数以内。"}
        detail = getattr(exc, "detail", None)
        return {**request, "system": request["system"] + "\n\n上一轮输出未通过校验，本轮必须修正。" + (detail or note)}
    return amend


class Job:
    """Subclasses set ``title``/``kind``/``fallback`` and implement ``work(context)`` returning the result."""
    title = kind = ""
    fallback = "生成未完成，请重试"
    task_kind = "interactive"
    needs_samples = True

    def __init__(self, plugin, store, account, **data):
        self.plugin, self.store, self.account = plugin, store, account
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.context = None
        self.data = {"id": self.id, "state": "running", "startedAt": now_ms(), **data}

    @property
    def running(self):
        return self.data["state"] == "running"

    def view(self):
        with self.lock:
            return {**self.data, **({"result": dict(self.data["result"])} if "result" in self.data else {})}

    def _set(self, **changes):
        with self.lock:
            self.data.update(changes)
        observer = getattr(self, "on_update", None)
        if observer is not None:
            observer(self.view())

    def start(self):
        _task, self.context = self.plugin.start_task(self.account, None, kind=self.task_kind, title=self.title,
                                                     params={"kind": self.kind}, handler=self.run)

    def cancel(self):
        """Stop the job and close its Core task as ``stopped``, not ``failed``."""
        if self.context is not None:
            try:
                self.context.stop()
            except Exception:
                self.context.cancelled.set()

    def _report(self, exc):
        """Keep unexpected failures diagnosable without persisting prompt material."""
        try:
            import traceback
            self.plugin.log(f"{self.kind}: " + "".join(
                traceback.format_exception_only(type(exc), exc)).strip(), "error")
        except Exception:
            pass

    def run(self, context):
        self.context = context
        try:
            result = self.work(context)
            context.check()
            self._set(state="done", phase="done", result=result, finishedAt=now_ms())
            return "done"
        except Cancelled:
            self._set(state="cancelled", finishedAt=now_ms())
            raise
        except Exception as exc:
            if context.cancelled.is_set():
                self._set(state="cancelled", finishedAt=now_ms())
                raise Cancelled() from None
            self._report(exc)
            message = failure(exc, self.fallback)
            self._set(state="error", error=message, finishedAt=now_ms())
            context.finish("failed", {"code": getattr(exc, "code", self.kind + "-failed"), "message": message})
            return "failed"

    def _models(self):
        """This thread's own SDK client: one SDK model worker process carried only two
        requests at a time on the live run, so parallel stages give each thread its own."""
        models = self.plugin.models
        if not isinstance(models, ModelClient):
            return models
        with _CLIENTS_LOCK:
            local = self.__dict__.setdefault("_thread_models", threading.local())
            client = getattr(local, "client", None)
            if client is None:
                client = local.client = _JobModelClient(self.plugin)
                self.__dict__.setdefault("_clients", []).append(client)
        return client

    def _discard_models(self, client):
        if not isinstance(client, _JobModelClient):
            return
        with _CLIENTS_LOCK:
            local = self.__dict__.get("_thread_models")
            if local is not None and getattr(local, "client", None) is client:
                del local.client
            clients = self.__dict__.get("_clients", [])
            if client in clients:
                clients.remove(client)
        client.close()

    def close_models(self):
        """Stop the per-thread model worker processes once the stage is over."""
        with _CLIENTS_LOCK:
            clients = self.__dict__.pop("_clients", [])
            self.__dict__.pop("_thread_models", None)
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    def _model_call(self, context, request, step):
        """The SDK call on a helper thread, so a reply that never arrives cannot hold this worker."""
        box, done, models = {}, threading.Event(), self._models()
        call = ModelContext(context)

        def target():
            try:
                box["value"] = models.generate(call, request, step=step)
            except BaseException as exc:
                box["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=target, daemon=True, name="model-call")
        thread.start()
        deadline = time.monotonic() + CALL_DEADLINE_SECONDS
        try:
            while not done.wait(.05):
                call.check()
                if time.monotonic() >= deadline:
                    raise SelfError("timeout", "模型长时间没有返回结果")
            call.check()
        except BaseException:
            call.set()
            self._discard_models(models)
            # A killed worker wakes the SDK pipe waiter. Discovery is separately
            # bounded by CoreClient; a retired client cannot spawn a late worker.
            thread.join(timeout=1)
            raise
        if "error" in box:
            raise box["error"]
        return box["value"]

    def generate(self, context, request, step, parse, amend=None, salvage=None,
                 refocus=("invalid-output", "empty-response")):
        """Retry on retryable failures; ``amend(exc)`` may refocus a rejected-format retry.

        Unusable output gets one more try than transport blips: an interrupted
        upstream stream arrives as truncated text rather than an error, so the
        format path also absorbs random mid-stream cuts (live GLM 2026-10-28).
        ``refocus`` names the codes that earn that extra try and the amended
        request: a reply the provider cut at its output cap belongs to it only
        where the caller cannot do better (the evidence stage halves its batch
        instead), and the text it did deliver stays available for the salvage.
        ``salvage(raw)`` is the last resort once every try was rejected: it
        turns the final raw reply into a result or raises the honest error."""
        attempts, attempt, extra = ATTEMPTS, 0, True
        raws = []
        while True:
            context.check()
            started = now_ms()
            succeeded = False
            self._set(modelCalls=self.data.get("modelCalls", 0) + 1, activeCallStartedAt=started,
                      activeCallStep=step)
            try:
                raws.append(self._model_call(context, request, step).get("text"))
                result = parse(raws[-1])
                succeeded = True
                return result
            except Cancelled:
                raise
            except Exception as exc:
                code, status = getattr(exc, "code", None), getattr(exc, "http_status", None)
                if code == "output-truncated":
                    # The cut text is still the model's own answer: keep it so the
                    # salvage fallback can use it instead of losing the whole reply.
                    partial = getattr(exc, "partial", None)
                    if isinstance(partial, str) and partial.strip():
                        raws.append(partial)
                retry = code in ("invalid-output", "empty-response", "output-truncated", "network", "timeout") or \
                    status in RETRYABLE_HTTP
                last = attempt + 1 >= attempts
                if last and extra and code in refocus:
                    attempts += 1  # one extra fresh call plus the amend reason beats a second cut
                    extra = False
                    last = False
                if last or not retry:
                    if last and salvage is not None and raws:
                        # The last try may itself be a mid-stream cut that never even
                        # delivered text, so salvage prefers the newest usable reply
                        # and falls back to earlier ones from this call.
                        for candidate in reversed(raws):
                            if not candidate:
                                continue
                            try:
                                return salvage(candidate)
                            except Exception:
                                pass
                    raise
                if amend is not None and code in refocus:
                    request = amend(exc) or request
            finally:
                elapsed = max(0, now_ms() - started)
                self._set(activeCallStartedAt=None, activeCallStep=None,
                          completedCallCount=self.data.get("completedCallCount", 0) + 1,
                          successfulCallCount=self.data.get("successfulCallCount", 0) + int(succeeded),
                          cumulativeCallMs=self.data.get("cumulativeCallMs", 0) + elapsed)
            attempt += 1
