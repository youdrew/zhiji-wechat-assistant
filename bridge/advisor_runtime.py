"""Private JSONL worker transport for read-only advisor sessions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid


class AdvisorRuntimeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class AdvisorRuntime:
    def __init__(self, root, account=None):
        self.root = Path(root).resolve()
        self.account = account
        self.condition = threading.Condition(threading.RLock())
        self.write_lock = threading.Lock()
        self.process = None
        self.pending = {}
        self.closed = False

    def _start(self):
        with self.condition:
            if self.closed:
                raise AdvisorRuntimeError("advisor-closed")
            if self.process is not None and self.process.poll() is None:
                return
            bundled = self.root / "runtime/node/node.exe"
            node = str(bundled) if bundled.is_file() else shutil.which("node")
            if not node:
                raise AdvisorRuntimeError("advisor-engine-missing")
            # The worker receives provider credentials only through its private pipe.
            environment = {key: os.environ[key] for key in (
                "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP",
            ) if key in os.environ}
            environment["WECHATVIBE_ADVISOR_ROOT"] = str(self.root)
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                [node, "--import", "tsx", str(self.root / "scripts/advisor-worker.ts")],
                cwd=self.root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                creationflags=flags,
            )
            process = self.process
            threading.Thread(target=self._read, args=(process,), daemon=True,
                             name="advisor-runtime-events").start()

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
                callback = None
                event = None
                with self.condition:
                    pending = self.pending.get(envelope.get("id"))
                    if pending is None or pending["process"] is not process:
                        continue
                    if isinstance(envelope.get("event"), dict):
                        callback, event = pending["on_event"], envelope["event"]
                    elif "result" in envelope or "error" in envelope:
                        pending["reply"] = envelope
                        self.condition.notify_all()
                if callback is not None:
                    try:
                        callback(event)
                    except Exception:
                        # UI cancellation must not tear down another request's reader.
                        pass
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
                raise AdvisorRuntimeError("advisor-engine-stopped")
            try:
                process.stdin.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AdvisorRuntimeError("advisor-engine-stopped") from exc

    def _call(self, command, config, request, on_event, cancel_event):
        if self.account is not None and request.get("account") != self.account:
            raise AdvisorRuntimeError("advisor-scope-changed")
        self._start()
        identifier = str(uuid.uuid4())
        pending = {"process": self.process, "reply": None, "on_event": on_event}
        with self.condition:
            self.pending[identifier] = pending
        started = time.monotonic()
        cancelled_at = None
        try:
            self._send({"id": identifier, "cmd": command,
                        "payload": {"config": config, "request": request}})
            while True:
                if cancel_event.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    self._send({"id": str(uuid.uuid4()), "cmd": "cancel",
                                "payload": {"requestId": identifier}})
                with self.condition:
                    reply = pending["reply"]
                    if reply is not None:
                        if cancel_event.is_set():
                            raise AdvisorRuntimeError("advisor-cancelled")
                        if "error" in reply:
                            code = reply["error"]
                            if isinstance(code, dict):
                                code = code.get("code")
                            safe = code if isinstance(code, str) and len(code) <= 80 else "advisor-generation-failed"
                            raise AdvisorRuntimeError(safe)
                        result = reply.get("result")
                        if not isinstance(result, dict):
                            raise AdvisorRuntimeError("advisor-invalid-output")
                        return result
                    if self.closed:
                        raise AdvisorRuntimeError("advisor-closed")
                    if cancelled_at is not None and time.monotonic() - cancelled_at > 5:
                        raise AdvisorRuntimeError("advisor-cancelled")
                    if time.monotonic() - started > 300:
                        self._send({"id": str(uuid.uuid4()), "cmd": "cancel",
                                    "payload": {"requestId": identifier}})
                        raise AdvisorRuntimeError("advisor-timeout")
                    self.condition.wait(timeout=0.1)
        finally:
            with self.condition:
                self.pending.pop(identifier, None)

    def respond(self, config, request, on_event, cancel_event):
        return self._call("respond", config, request, on_event, cancel_event)

    def compact(self, config, request, on_event, cancel_event):
        return self._call("compact", config, request, on_event, cancel_event)

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
        except (OSError, ValueError, subprocess.TimeoutExpired, AdvisorRuntimeError):
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
        import psutil
        allowed = {(self.root / "runtime/opencode/opencode.exe").resolve(),
                   (self.root / ".local/advisor-engine/opencode.exe").resolve()}
        try:
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
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
                raise AdvisorRuntimeError("advisor-engine-stop-failed") from exc


def runtime_factory(root):
    factory = lambda: AdvisorRuntimePool(root)
    factory.available = lambda: engine_available(root)
    return factory


def engine_available(root):
    root = Path(root).resolve()
    if not (root / "scripts/advisor-worker.ts").is_file() or not (root / "node_modules/@opencode-ai/sdk/package.json").is_file():
        return False
    if not (root / "runtime/node/node.exe").is_file() and not shutil.which("node"):
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
            if (value.get("schema") == 1 and value.get("version") == "1.18.34" and
                    value.get("file") == "opencode.exe" and executable.is_file() and
                    value.get("bytes") == executable.stat().st_size):
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return False


class AdvisorRuntimePool:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.workers = {}
        self.closed = False

    def _worker(self, request):
        account = request.get("account")
        if not isinstance(account, str) or not account:
            raise AdvisorRuntimeError("advisor-scope-changed")
        with self.lock:
            if self.closed:
                raise AdvisorRuntimeError("advisor-closed")
            worker = self.workers.get(account)
            if worker is None:
                worker = self.workers[account] = AdvisorRuntime(self.root, account)
            return worker

    def respond(self, config, request, on_event, cancel_event):
        return self._worker(request).respond(config, request, on_event, cancel_event)

    def compact(self, config, request, on_event, cancel_event):
        return self._worker(request).compact(config, request, on_event, cancel_event)

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
