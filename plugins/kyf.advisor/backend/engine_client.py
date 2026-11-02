"""Private JSONL transport to the plugin's engine worker (``engine/worker.ts``), one worker per account."""
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): native session lifecycle transport.
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): transient visual inspection.
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): structured 知己分析 generation.
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): events run on the caller's thread.
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): answer deadline covers reasoning.
# Modified 2026-10-16 (Apache-2.0): every generation uses the saved model timeout, plus transport cleanup time.
# Modified 2026-10-16 (Apache-2.0): escape Unicode line separators in JSONL requests
# so Node readline cannot split one chat payload into uncorrelated fragments.
# Moved from bridge/advisor_runtime.py into the kyf.advisor plugin, 2026-10-13 (Apache-2.0): model commands carry a
# gateway service and the task's execution token instead of a provider config (no credential reaches the
# worker); the worker is started with the plugin's Node and tsx loader and keeps its state in the account folder.
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from kyf_plugin.models import model_timeout_ms

COMMAND_TIMEOUT_SECONDS = 300
# Let the engine's configured generation deadline report a clean error first.
MODEL_CLEANUP_SECONDS = 60
MODEL_COMMANDS = frozenset({"respond", "compact", "summarizeDiscussion", "compactSession", "inspectMedia"})
ENGINE_VERSION = "1.18.34"
# Only what the worker needs to start; the gateway URL and token travel per command on the private pipe.
ENVIRONMENT = ("SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP", "PATHEXT",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


class EngineError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def plugin_directory():
    return Path(os.environ.get("KYF_PLUGIN_DIR") or Path(__file__).resolve().parents[1])


def app_root():
    return Path(os.environ.get("KYF_APP_ROOT") or Path(__file__).resolve().parents[3])


class EngineClient:
    def __init__(self, plugin_dir, account=None, state_dir=None, root=None):
        self.plugin_dir = Path(plugin_dir).resolve()
        self.root = Path(root).resolve() if root is not None else app_root()
        self.account = account
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.condition = threading.Condition(threading.RLock())
        self.write_lock = threading.Lock()
        self.process = None
        self.pending = {}
        self.closed = False

    def _start(self):
        with self.condition:
            if self.closed:
                raise EngineError("advisor-closed")
            if self.process is not None and self.process.poll() is None:
                return
            node, loader = os.environ.get("KYF_NODE"), os.environ.get("KYF_TSX_LOADER")
            if not node or not loader or self.state_dir is None:
                raise EngineError("advisor-engine-missing")
            environment = {key: os.environ[key] for key in ENVIRONMENT if key in os.environ}
            environment["KYF_APP_ROOT"] = str(self.root)
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.process = subprocess.Popen(
                [node, "--import", loader, str(self.plugin_dir / "engine" / "worker.ts"),
                 "--state", str(self.state_dir), "--root", str(self.root)],
                cwd=self.plugin_dir, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, creationflags=flags,
            )
            process = self.process
            threading.Thread(target=self._read, args=(process,), daemon=True,
                             name="advisor-engine-events").start()

    def _read(self, process):
        try:
            for line in process.stdout:
                if len(line) > 2_000_000:
                    continue
                try:
                    envelope = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                with self.condition:
                    pending = self.pending.get(envelope.get("id"))
                    if pending is None or pending["process"] is not process:
                        continue
                    # Only queue here: callbacks run on each caller's thread, so a
                    # blocked run can never stall this sole reader of every run's
                    # output (or, once the pipe fills, the whole worker).
                    if isinstance(envelope.get("event"), dict):
                        pending["events"].append(envelope["event"])
                    elif "result" in envelope or "error" in envelope:
                        pending["reply"] = envelope
                    else:
                        continue
                    self.condition.notify_all()
        finally:
            with self.condition:
                for pending in self.pending.values():
                    if pending["process"] is process and pending["reply"] is None:
                        pending["reply"] = {"error": "advisor-engine-stopped"}
                self.condition.notify_all()

    def _send(self, envelope):
        with self.write_lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                raise EngineError("advisor-engine-stopped")
            try:
                # JSON permits literal U+2028/U+2029 inside strings, but Node's
                # readline treats them as line endings. Escape only the wire
                # representation; JSON.parse restores the original chat text.
                line = json.dumps(envelope, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise EngineError("advisor-engine-stopped") from exc

    def _call(self, command, payload, on_event, cancel_event):
        request = payload["request"]
        try:
            timeout = (model_timeout_ms(payload.get("service", {}).get("timeoutMs")) / 1000 + MODEL_CLEANUP_SECONDS
                       if command in MODEL_COMMANDS else COMMAND_TIMEOUT_SECONDS)
        except ValueError:
            raise EngineError("invalid-request") from None
        if self.account is not None and request.get("account") != self.account:
            raise EngineError("advisor-scope-changed")
        self._start()
        identifier = str(uuid.uuid4())
        pending = {"process": self.process, "reply": None, "events": []}
        with self.condition:
            self.pending[identifier] = pending
        started = time.monotonic()
        cancelled_at = None
        try:
            self._send({"id": identifier, "cmd": command, "payload": payload})
            while True:
                if cancel_event.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    self._send({"id": str(uuid.uuid4()), "cmd": "cancel", "payload": {"targetId": identifier}})
                with self.condition:
                    if not pending["events"] and pending["reply"] is None and not self.closed:
                        self.condition.wait(timeout=0.1)
                    # The reader queues events before the reply, so one snapshot
                    # delivers every event ahead of the result it precedes.
                    events, pending["events"] = pending["events"], []
                    reply, closed = pending["reply"], self.closed
                self._deliver(on_event, events)
                if reply is not None:
                    if cancel_event.is_set() and command != "compactSession":
                        raise EngineError("advisor-cancelled")
                    if "error" in reply:
                        code = reply["error"]
                        if isinstance(code, dict):
                            code = code.get("code")
                        safe = code if isinstance(code, str) and len(code) <= 80 else "advisor-generation-failed"
                        raise EngineError(safe)
                    result = reply.get("result")
                    if not isinstance(result, dict):
                        raise EngineError("advisor-invalid-output")
                    return result
                if closed:
                    raise EngineError("advisor-closed")
                if cancelled_at is not None and time.monotonic() - cancelled_at > (20 if command == "compactSession" else 5):
                    raise EngineError("advisor-cancelled")
                if time.monotonic() - started > timeout:
                    self._send({"id": str(uuid.uuid4()), "cmd": "cancel", "payload": {"targetId": identifier}})
                    raise EngineError("advisor-timeout")
        finally:
            with self.condition:
                self.pending.pop(identifier, None)
                events, pending["events"] = pending["events"], []
            # A late native-session ID must still be recorded after cancellation.
            self._deliver(on_event, events)

    @staticmethod
    def _deliver(on_event, events):
        for event in events:
            try:
                on_event(event)
            except Exception:
                # A failing UI callback must not abort this request's transport loop.
                pass

    # Model commands: the gateway service and the execution token of the Core task they belong to.
    def _model(self, command, service, token, request, on_event, cancel_event):
        return self._call(command, {"service": service, "token": token, "request": request}, on_event, cancel_event)

    def respond(self, service, token, request, on_event, cancel_event):
        return self._model("respond", service, token, request, on_event, cancel_event)

    def compact(self, service, token, request, on_event, cancel_event):
        return self._model("compact", service, token, request, on_event, cancel_event)

    def inspect_media(self, service, token, request, on_event, cancel_event):
        return self._model("inspectMedia", service, token, request, on_event, cancel_event)

    def compact_session(self, service, token, request, on_event, cancel_event):
        return self._model("compactSession", service, token, request, on_event, cancel_event)

    def summarize_discussion(self, service, token, request, on_event, cancel_event):
        return self._model("summarizeDiscussion", service, token, request, on_event, cancel_event)

    # Native session bookkeeping: no model call, no token.
    def inspect_session(self, service, request, on_event, cancel_event):
        return self._call("inspectSession", {"service": service, "request": request}, on_event, cancel_event)

    def stop_session_operation(self, service, request, on_event, cancel_event):
        return self._call("stopSessionOperation", {"service": service, "request": request}, on_event, cancel_event)

    def delete_session(self, service, request, on_event, cancel_event):
        return self._call("deleteSession", {"service": service, "request": request}, on_event, cancel_event)

    def close(self):
        with self.condition:
            if self.closed:
                return
            self.closed = True
            process = self.process
            self.condition.notify_all()
        if process is None:
            return
        try:
            self._send({"id": str(uuid.uuid4()), "cmd": "close", "payload": {}})
            process.wait(timeout=40)
        except (OSError, ValueError, subprocess.TimeoutExpired, EngineError):
            self._terminate_owned_children(process)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def _terminate_owned_children(self, process):
        try:
            import psutil
        except ImportError:
            return  # The plugin's process group still ends the engine with the plugin.
        allowed = {(self.root / "runtime/opencode/opencode.exe").resolve(),
                   (self.root / ".local/advisor-engine/opencode.exe").resolve()}
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for child in children:
            try:
                if Path(child.exe()).resolve() in allowed:
                    child.kill()
                    child.wait(timeout=5)
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.TimeoutExpired) as exc:
                raise EngineError("advisor-engine-stop-failed") from exc


def engine_available(plugin_dir=None, root=None):
    """Node, the tsx loader, the worker, the opencode SDK and a pinned opencode binary are all present."""
    plugin_dir = Path(plugin_dir) if plugin_dir is not None else plugin_directory()
    root = Path(root).resolve() if root is not None else app_root()
    node, loader = os.environ.get("KYF_NODE"), os.environ.get("KYF_TSX_LOADER")
    if not node or not loader or not Path(node).is_file():
        return False
    if (not (plugin_dir / "engine" / "worker.ts").is_file() or
            not (root / "node_modules/@opencode-ai/sdk/package.json").is_file()):
        return False
    for folder in (root / "runtime/opencode", root / ".local/advisor-engine"):
        manifest = folder / "manifest.json"
        executable = folder / "opencode.exe"
        try:
            if manifest.is_symlink() or executable.is_symlink() or not folder.resolve().is_relative_to(root):
                continue
            if manifest.stat().st_size > 8192:
                continue
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if (value.get("schema") == 1 and value.get("version") == ENGINE_VERSION and
                    value.get("file") == "opencode.exe" and executable.is_file() and
                    value.get("bytes") == executable.stat().st_size):
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return False


class EnginePool:
    """One engine worker per account; ``state_dir(account)`` is that account's plugin folder."""

    def __init__(self, state_dir, plugin_dir=None, root=None):
        self.state_dir = state_dir
        self.plugin_dir = Path(plugin_dir) if plugin_dir is not None else plugin_directory()
        self.root = root
        self.lock = threading.RLock()
        self.workers = {}
        self.closed = False

    def available(self):
        return engine_available(self.plugin_dir, self.root)

    def participant_root(self, account):
        """Where a run writes the files of @-mentioned friends: inside the engine workspace
        (runtime.ts: ``<state>/engine/workspace``), the only place its read tool may be allowed."""
        return Path(self.state_dir(account)) / "engine" / "workspace" / "participants"

    def _worker(self, request):
        account = request.get("account")
        if not isinstance(account, str) or not account:
            raise EngineError("advisor-scope-changed")
        with self.lock:
            if self.closed:
                raise EngineError("advisor-closed")
            worker = self.workers.get(account)
            if worker is None:
                worker = self.workers[account] = EngineClient(self.plugin_dir, account, self.state_dir(account),
                                                              self.root)
            return worker

    def respond(self, service, token, request, on_event, cancel_event):
        return self._worker(request).respond(service, token, request, on_event, cancel_event)

    def compact(self, service, token, request, on_event, cancel_event):
        return self._worker(request).compact(service, token, request, on_event, cancel_event)

    def inspect_media(self, service, token, request, on_event, cancel_event):
        return self._worker(request).inspect_media(service, token, request, on_event, cancel_event)

    def compact_session(self, service, token, request, on_event, cancel_event):
        return self._worker(request).compact_session(service, token, request, on_event, cancel_event)

    def summarize_discussion(self, service, token, request, on_event, cancel_event):
        return self._worker(request).summarize_discussion(service, token, request, on_event, cancel_event)

    def inspect_session(self, service, request, on_event, cancel_event):
        return self._worker(request).inspect_session(service, request, on_event, cancel_event)

    def stop_session_operation(self, service, request, on_event, cancel_event):
        return self._worker(request).stop_session_operation(service, request, on_event, cancel_event)

    def delete_session(self, service, request, on_event, cancel_event):
        return self._worker(request).delete_session(service, request, on_event, cancel_event)

    def shutdown_account(self, account):
        with self.lock:
            worker = self.workers.pop(account, None)
        if worker:
            worker.close()

    def close(self):
        with self.lock:
            self.closed = True
            workers, self.workers = list(self.workers.values()), {}
        for worker in workers:
            worker.close()
