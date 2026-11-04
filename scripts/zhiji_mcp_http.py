"""Direct HTTP transport for credential-bearing requests to the local Core.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import json
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib reports the redirect as an HTTPError instead of forwarding the token.
        return None


_DIRECT = build_opener(ProxyHandler({}), _NoRedirect())


def loopback_origin(value):
    """Accept only the explicit IPv4 loopback origin the launcher binds."""
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme == "http" and port and parts.netloc == f"127.0.0.1:{port}"
                and parts.path in ("", "/") and not parts.query and not parts.fragment
                and value in (f"http://{parts.netloc}", f"http://{parts.netloc}/")):
            return f"http://127.0.0.1:{port}"
    except (ValueError, TypeError, AttributeError):
        pass
    raise URLError("知己服务地址必须是 http://127.0.0.1:<端口>")


def post_json(origin, path, body, *, timeout, token=None):
    """Return ``(status, payload)``; keep HTTPError bodies available to callers."""
    if not isinstance(path, str) or not path.startswith("/api/mcp/"):
        raise URLError("知己 MCP 请求路径不正确")
    url = loopback_origin(origin) + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    with _DIRECT.open(Request(url, data=data, headers=headers, method="POST"), timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))
