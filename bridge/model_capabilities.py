"""On-use discovery for the reserved unified gateway alias; no model payloads or disk state.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ALIAS = "local-default"
CACHE_TTL = 3.0
FAILURE_TTL = 1.0
REQUEST_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 64 * 1024
READ_CHUNK_BYTES = 4096


class ModelCapabilitiesUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("统一模型网关暂时不可用或正在切换模型")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Never forward a decrypted credential to a redirected host.


class UnifiedModelCapabilities:
    """One bounded, credential-sensitive cache. Failures never reuse the last ready route."""

    def __init__(self, *, clock=time.monotonic, opener=None):
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.lock = threading.Lock()
        self.cached = None

    def lookup(self, api, key):
        if api.get("model") != ALIAS or api.get("protocol") != "chat_completions":
            return None
        identity = (api.get("baseUrl"), hashlib.sha256((key or "").encode("utf-8")).digest())
        with self.lock:
            if self.cached is not None:
                cached_identity, expires, value = self.cached
                if cached_identity == identity and self.clock() < expires:
                    if value is None:
                        raise ModelCapabilitiesUnavailable()
                    return dict(value)
            try:
                value = self._fetch(api.get("baseUrl"), key)
            except (OSError, ValueError, TypeError, ModelCapabilitiesUnavailable):
                self.cached = (identity, self.clock() + FAILURE_TTL, None)
                raise ModelCapabilitiesUnavailable() from None
            self.cached = (identity, self.clock() + CACHE_TTL, value)
            return dict(value)

    def _fetch(self, base_url, key):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.query or parsed.fragment or not key):
            raise ModelCapabilitiesUnavailable()
        request = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET",
                                         headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        deadline = time.monotonic() + REQUEST_TIMEOUT
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                raw = self._read_response(response, deadline)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ModelCapabilitiesUnavailable() from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelCapabilitiesUnavailable()
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ModelCapabilitiesUnavailable()
        aliases = [item for item in data if isinstance(item, dict) and item.get("id") == ALIAS]
        if len(aliases) != 1:
            raise ModelCapabilitiesUnavailable()
        item = aliases[0]
        if item.get("unified_gateway") is not True or item.get("ready") is not True:
            raise ModelCapabilitiesUnavailable()
        for name in ("actual_model", "backend_id"):
            value = item.get(name)
            if (not isinstance(value, str) or not 1 <= len(value) <= 256 or
                    any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ModelCapabilitiesUnavailable()
        revision, context, output = (item.get(name) for name in
                                     ("route_revision", "context_length", "max_output_tokens"))
        if (type(revision) is not int or not 0 <= revision <= 2**53 or
                type(context) is not int or not 4096 <= context <= 2**31 or
                "max_output_tokens" not in item or
                (output is not None and (type(output) is not int or not 1 <= output <= context)) or
                type(item.get("supports_vision")) is not bool):
            raise ModelCapabilitiesUnavailable()
        return {name: item[name] for name in ("actual_model", "backend_id", "route_revision", "context_length",
                                            "max_output_tokens", "supports_vision")}

    @staticmethod
    def _read_response(response, deadline):
        # HTTPResponse.read(N) can perform many socket reads without returning.
        # read1 returns after one underlying read; reduce its idle timeout to the
        # remaining total budget before every chunk, even if the peer keeps dripping.
        raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if raw_socket is not None:
                raw_socket.settimeout(remaining)
            chunk = response.read1(min(READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - size))
            if time.monotonic() >= deadline:
                raise TimeoutError()
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ModelCapabilitiesUnavailable()
            if getattr(response, "isclosed", lambda: False)():
                break
        return b"".join(chunks)
