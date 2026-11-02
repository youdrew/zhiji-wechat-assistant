"""Model discovery and connection tests for the settings page.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Probes run in the model gateway when Core has one, otherwise in a short-lived
Node process. The API key travels only on a private stdin pipe and is never
placed in an environment or argument list.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from core_runtime import base_environment, node_executable
from model_trace_context import current_trace_context

PROBE_TIMEOUT_SECONDS = 45
MAX_CONCURRENT_PROBES = 2


class ModelProbe:
    def __init__(self, root, gateway=None):
        self.root = Path(root).resolve()
        self.gateway = gateway
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT_PROBES)

    def _run(self, request):
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("rate-limit")
        try:
            if self.gateway is not None:
                return self._run_in_gateway(request)
            node = node_executable(self.root)
            if not node:
                raise RuntimeError("unsupported")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                [node, "--import", "tsx", str(self.root / "scripts" / "model-probe.ts")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", cwd=self.root, env=base_environment(), creationflags=flags)
            # The diagnostic grant is bound to this exact process before it receives work.
            payload = {**request, "traceContext": current_trace_context(process.pid)}
            try:
                stdout, _stderr = process.communicate(json.dumps(payload, ensure_ascii=False),
                                                      timeout=PROBE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise RuntimeError("timeout") from exc
            lines = [line for line in (stdout or "").splitlines() if line.strip()]
            try:
                result = json.loads(lines[-1]) if lines else None
            except ValueError:
                result = None
            if not isinstance(result, dict):
                raise RuntimeError("provider-error")
            if isinstance(result.get("error"), str):
                raise RuntimeError(result["error"])
            return result
        finally:
            self.slots.release()

    def _run_in_gateway(self, request):
        from core_gateway import GatewayUnavailable
        try:
            result = self.gateway().probe(request, trace=current_trace_context)
        except GatewayUnavailable as exc:
            raise RuntimeError("unsupported") from exc
        except RuntimeError as exc:
            raise RuntimeError("timeout") from exc
        if not isinstance(result, dict):
            raise RuntimeError("provider-error")
        if isinstance(result.get("error"), str):
            raise RuntimeError(result["error"])
        return result

    def model_list(self, protocol, base_url, api_key):
        result = self._run({"cmd": "list", "protocol": protocol, "baseUrl": base_url, "apiKey": api_key})
        return {"models": result.get("models"), "supported": result.get("supported")}

    def model_test(self, protocol, base_url, api_key, model):
        result = self._run({"cmd": "test", "protocol": protocol, "baseUrl": base_url,
                            "apiKey": api_key, "model": model})
        return {"ok": result.get("ok"), "latencyMs": result.get("latencyMs")}
