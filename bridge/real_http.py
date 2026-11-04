"""Loopback-only HTTP transport for the real-data chat UI.

Modified by Know Your Friends, 2026-10-11: accept bounded forward history pages.
Modified by Know Your Friends, 2026-10-13: retired legacy analysis endpoints; plugin architecture.
Modified by Know Your Friends, 2026-10-13: the assistant's /api/advisor routes moved into the kyf.advisor
plugin (reached through /api/plugins/rpc); host-picked files are granted through /api/plugins/file-grant.
Modified by Know Your Friends, 2026-11-04: requests carrying X-KYF-UI-Session (Agent-opened windows) pass
core.ui.guard before routing; requests without the header are unchanged.
"""
from __future__ import annotations

import json
import io
import hmac
import mimetypes
import os
import re
import threading
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from backend_contracts import AccountUnavailableError, MessagesUnavailableError, ROOT
from backend_service import Backend
from wechat_source import WeChatSource
from account_store import AccountConflict, AccountNotFound
from instance_identity import default_port, instance_id
from model_source import ModelSourceUnavailable
from core_contracts import AdvisorError, error_status
import core_http
import core_mcp_pairing
import core_mcp_proxy
import friend_http
import plugin_api
import model_traces_http
import external_import_http
from model_traces import TraceError
from media_assets import MediaUnavailable

CHATUI = ROOT / "chatui"
PLUGIN_SDK_WEB = ROOT / "plugin-sdk" / "web"
# Plugin pages run in sandboxed frames of the host windows; they reach Core only through the host bridge.
PLUGIN_PAGE_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                      "img-src 'self' data: https: http:; "
                      "font-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
CONTROL_TOKEN_ENV = "KYF_CONTROL_TOKEN"
CONTROL_TOKEN_HEADER = "X-KYF-Control-Token"
# Agent-opened windows (core_ui_host) carry a restricted, read-only session token in this header.
UI_SESSION_HEADER = "X-KYF-UI-Session"
TRACE_INGRESS = threading.BoundedSemaphore(16)
MAX_POST_BODY = 65536
# Plugin RPC carries user-written prompts and skills (16 000 CJK characters are about 48 KB).
MAX_RPC_BODY = 1024 * 1024


class LocalHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128


def app_version():
    try:
        value = json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("version")
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, AttributeError):
        return None


APP_VERSION = app_version()
JAVASCRIPT_SUFFIXES = {".js", ".mjs"}
JAVASCRIPT_MIME = "text/javascript; charset=utf-8"


def static_content_type(name):
    """Return a stable MIME type for files served to the browser.

    On Windows, ``mimetypes.guess_type`` can inherit a registry mapping that
    incorrectly reports JavaScript as ``text/plain``.  With ``nosniff`` that
    prevents the browser from executing the application bundle, so script
    types must be selected independently of the host registry.
    """
    if Path(name).suffix.lower() in JAVASCRIPT_SUFFIXES:
        return JAVASCRIPT_MIME
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def integer(value, default, maximum):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("invalid limit")
    result = int(value)
    if not 1 <= result <= maximum:
        raise ValueError("limit out of range")
    return result


def user_value(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid user")
    return value


def request_id_value(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid requestId")
    return value


def make_handler(backend, accounts=None, control_token=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def trusted_request(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origins = {f"http://{host}" for host in hosts}
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") in hosts and
                    (origin is None or origin in origins) and
                    self.path.startswith("/") and not self.path.startswith("//"))

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Switching chats cancels obsolete requests. There is no client left to
                # receive a second 503 response; retain the successfully read result.
                self.close_connection = True

        def query(self, path):
            return {key: values[0] for key, values in parse_qs(path.query, keep_blank_values=True).items()}

        def ui_session_allowed(self, method):
            """Agent-opened window requests pass ``core.ui.guard`` first; others are untouched.

            The body of a guarded POST/DELETE is read here and put back as ``self.rfile`` so
            the route handler reads exactly the same bytes.
            """
            token = self.headers.get(UI_SESSION_HEADER)
            if token is None:
                return True
            try:
                ui = getattr(backend.core_services(), "ui", None)
            except Exception:
                ui = None
            guard = getattr(ui, "guard", None)
            if not callable(guard):
                self.send(403, {"error": "ui-session-forbidden"})
                return False
            parsed = urlsplit(self.path)
            body = None
            if method in ("POST", "DELETE"):
                try:
                    length = integer(self.headers.get("Content-Length"), 0, MAX_RPC_BODY)
                except ValueError:
                    self.send(400, {"error": "invalid-request"})
                    return False
                raw = self.rfile.read(length) if length else b""
                self.rfile = io.BytesIO(raw)
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (ValueError, UnicodeError):
                    body = None
            try:
                guard(token, method, parsed.path, self.query(parsed), body)
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code not in ("ui-session-forbidden", "ui-session-read-only"):
                    code = "ui-session-forbidden"
                self.send(403, {"error": code})
                return False
            return True

        def send_media(self, asset):
            # Modified by Know Your Friends, 2026-10-10: bounded streaming of local
            # references. Browser seeking never loads an entire video into memory.
            try:
                stream = io.BytesIO(asset.data) if asset.data is not None else asset.path.open("rb")
            except OSError:
                return self.send(404, {"error": "media unavailable", "reason": "local-file-unavailable"})
            with stream:
                stream.seek(0, 2)
                size = stream.tell()
                start, end, status = 0, size - 1, 200
                raw_range = self.headers.get("Range")
                if raw_range is not None:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range)
                    valid = bool(match and (match[1] or match[2]) and size)
                    if valid:
                        if match[1]:
                            start = int(match[1])
                            end = min(int(match[2]), size - 1) if match[2] else size - 1
                        else:
                            suffix = int(match[2])
                            valid = suffix > 0
                            start = max(0, size - suffix)
                        valid = valid and 0 <= start <= end < size
                    if not valid:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                    status = 206
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", asset.mime)
                    self.send_header("Content-Length", str(max(0, end - start + 1)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        block = stream.read(min(256 * 1024, remaining))
                        if not block:
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True

        def _static_file(self, base, relative, policy=None):
            base = base.resolve()
            target = (base / relative).resolve() if relative else base
            if (not relative or not target.is_relative_to(base) or not target.is_file() or
                    any(part.startswith(".") for part in Path(relative).parts)):
                return self.send(404, {"error": "not found"})
            payload = target.read_bytes()
            try:
                self.send_response(200)
                self.send_header("Content-Type", static_content_type(target.name))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if policy:
                    self.send_header("Content-Security-Policy", policy)
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _plugin_page(self, path):
            plugin_id, _, relative = path[len("/plugin-pages/"):].partition("/")
            try:
                registry = backend.core_services().registry
            except RuntimeError:
                return self.send(503, {"error": "bridge-closing"})
            manifest = registry.get(plugin_id)
            if manifest is None or not registry.enabled(plugin_id):
                return self.send(404, {"error": "not found"})
            return self._static_file(Path(manifest["directory"]), relative, PLUGIN_PAGE_POLICY)

        def _plugin_api(self, method):
            parsed = urlsplit(self.path)
            body = None
            try:
                if method in ("POST", "DELETE"):
                    length = integer(self.headers.get("Content-Length"), 0, plugin_api.MAX_BODY)
                    raw = self.rfile.read(length) if length else b""
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(body, dict):
                        raise ValueError("invalid body")
                status, payload = backend.core_services().api.handle(method, parsed.path, self.query(parsed),
                                                                     self.headers, body)
            except plugin_api.ApiError as exc:
                return self.send(exc.status, {"error": exc.code, "message": exc.message})
            except AdvisorError as exc:
                return self.send(error_status(exc), {"error": exc.code, "message": exc.message})
            except (ValueError, UnicodeError):
                return self.send(400, {"error": "invalid-request", "message": "请求格式不正确"})
            except Exception as exc:
                return self.send(503, {"error": "core-unavailable", "message": str(exc)[:200]})
            if isinstance(payload, (dict, list)) or payload is None:
                return self.send(status, payload)
            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(10)
                self.send_response(status)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                for line in payload:
                    self.wfile.write(line.encode("utf-8"))
            except (OSError, AdvisorError):
                pass
            finally:
                close = getattr(payload, "close", None)
                if callable(close):
                    close()
                self.connection.settimeout(previous_timeout)
                self.close_connection = True

        def _host_core(self, method, path, request):
            try:
                core = backend.core_services()
                result = core_http.get(core, path, request) if method == "GET" else core_http.post(core, path, request)
                return self.send(200, result)
            except core_http.HostError as exc:
                return self.send(exc.status, {"error": exc.code, "message": exc.message,
                                              **({"detail": exc.detail} if exc.detail else {})})

        def do_GET(self):
            lease = getattr(backend, "request_lease", None)
            reader_scope = getattr(getattr(backend, "source", None), "request_scope", None)
            try:
                with lease() if callable(lease) else nullcontext():
                    with reader_scope() if callable(reader_scope) else nullcontext():
                        return self._do_GET()
            except RuntimeError:
                return self.send(503, {"error": "bridge-closing"})

        def _do_GET(self):
            if not self.trusted_request():
                return self.send(403, {"error": "forbidden"})
            if not self.ui_session_allowed("GET"):
                return None
            parsed = urlsplit(self.path)
            if parsed.path.startswith(core_mcp_proxy.HTTP_PREFIX):
                return self._mcp_api("GET")
            if parsed.path.startswith(plugin_api.PREFIX):
                return self._plugin_api("GET")
            if parsed.path.startswith(external_import_http.PREFIX):
                return external_import_http.handle(self, backend.core_services(), "GET")
            try:
                query = self.query(parsed)
                if parsed.path in core_http.GET_PATHS:
                    return self._host_core("GET", parsed.path, query)
                if parsed.path.startswith(model_traces_http.PREFIX):
                    backend.model_traces.transport_url = f"http://127.0.0.1:{self.server.server_port}"
                    return self.send(200, model_traces_http.get(backend.model_traces, parsed.path, query, self.headers))
                if parsed.path.startswith("/api/friend/"):
                    if parsed.path not in friend_http.GET_PATHS:
                        return self.send(404, {"error": "not found"})
                    return self.send(200, friend_http.get(backend.account_data().friends, parsed.path, query))
                if parsed.path == "/api/health":
                    return self.send(200, {**backend.health(), "instanceId": instance_id(ROOT),
                                           "appVersion": APP_VERSION})
                if parsed.path == "/api/data-root":
                    return self.send(200, backend.data_root_status())
                if parsed.path == "/api/model-source":
                    try:
                        return self.send(200, backend.model_source())
                    except Exception:
                        return self.send(503, {"error": "model source unavailable"})
                if parsed.path == "/api/legacy-portrait":
                    return self.send(200, backend.legacy_portrait(user_value(query.get("account")),
                                                                  user_value(query.get("user"))))
                if parsed.path == "/api/sessions":
                    cache = getattr(backend, "usage_names", None)
                    ticket = cache.ticket() if cache is not None else None
                    data = backend.source.sessions()
                    if cache is not None:
                        cache.observe(data, ticket)
                    if data.get("messagesReady") is False:
                        preparation = backend.preparation_status(data.get("account"))
                        if preparation:
                            data = {**data, "preparation": preparation}
                    if accounts is not None:
                        accounts.observe(data)
                    return self.send(200, data)
                if parsed.path == "/api/conversation-selection":
                    return self.send(200, backend.conversation_selection())
                if parsed.path == "/api/accounts" and accounts is not None:
                    return self.send(200, accounts.list())
                if parsed.path == "/api/messages":
                    return self.send(200, backend.messages(user_value(query.get("user")), integer(query.get("limit"), 80, 500)))
                if parsed.path == "/api/history":
                    return self.send(200, backend.history(
                        user_value(query.get("account")), user_value(query.get("user")),
                        before=query.get("before"), around=query.get("around"), after=query.get("after"),
                        limit=integer(query.get("limit"), 80, 200)))
                if parsed.path == "/api/history/search":
                    return self.send(200, backend.history_search(
                        user_value(query.get("account")), user_value(query.get("user")),
                        query=query.get("q"), day=query.get("date"), category=query.get("category", "all"),
                        before=query.get("before"), limit=integer(query.get("limit"), 50, 100)))
                if parsed.path == "/api/media/settings":
                    return self.send(200, backend.source.media_settings(user_value(query.get("account"))))
                if parsed.path == "/api/media":
                    if hasattr(backend.source, "media_asset"):
                        variant = query.get("variant", "original")
                        if variant not in ("thumbnail", "original", "playback", "frame"):
                            raise ValueError("invalid media variant")
                        account = user_value(query["account"]) if "account" in query else None
                        asset = backend.source.media_asset(user_value(query.get("user")), user_value(query.get("id")),
                            variant, expected_account=account)
                        return self.send_media(asset) if asset else self.send(404, {"error": "media unavailable",
                            "reason": getattr(getattr(backend.source, "media_reason", None), "value", None)})
                    image = backend.source.media(user_value(query.get("user")), user_value(query.get("id")))
                    return self.send(200, image[0], image[1]) if image else self.send(404, {
                        "error": "media unavailable",
                        "reason": getattr(getattr(backend.source, "media_reason", None), "value", None)})
                if parsed.path.startswith("/plugin-pages/"):
                    return self._plugin_page(parsed.path)
                if parsed.path.startswith("/plugin-sdk/web/"):
                    return self._static_file(PLUGIN_SDK_WEB, parsed.path[len("/plugin-sdk/web/"):], PLUGIN_PAGE_POLICY)
                if parsed.path.startswith("/api/"):
                    return self.send(404, {"error": "not found"})
                target = (CHATUI / (parsed.path.lstrip("/") or "index.html")).resolve()
                if not target.is_relative_to(CHATUI.resolve()) or not target.is_file():
                    return self.send(404, {"error": "not found"})
                mime = static_content_type(target.name)
                return self.send(200, target.read_bytes(), mime)
            except TraceError as exc:
                return self.send(exc.status, {"error": exc.code, "message": exc.message})
            except AdvisorError as exc:
                return self.send(error_status(exc), {"error": exc.code, "message": exc.message})
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            except Exception as exc:
                preparation = backend.preparation_status() if isinstance(exc, (AccountUnavailableError, MessagesUnavailableError)) else None
                return self.send(503, {"error": type(exc).__name__, "message": str(exc)[:200],
                                       **({"preparation": preparation} if preparation else {})})

        def _control_lease(self, endpoint):
            """The Electron main process holds the GUI lease and reads background state.

            MCP connection leases and task leases are internal; this route manages
            only the GUI's own lease and never touches MCP authorization state.
            """
            if not self.control_request():
                return self.send(403, {"error": "forbidden"})
            if endpoint == "/api/control/background":
                if self.headers.get("Content-Length") not in (None, "0"):
                    return self.send(400, {"error": "invalid control request"})
                try:
                    return self.send(200, backend.background_status())
                except Exception:
                    return self.send(503, {"error": "core-unavailable",
                                           "message": "后台状态暂时不可用"})
            try:
                length = integer(self.headers.get("Content-Length"), None, 4096)
                if length is None:
                    raise ValueError("body required")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(request, dict) or set(request) - {"action", "pid"}:
                    raise ValueError("invalid lease request")
                action = request.get("action")
                if action not in ("acquire", "release", "refresh"):
                    raise ValueError("invalid lease action")
                pid = request.get("pid")
                if pid is not None and (type(pid) is not int or not 1 <= pid <= 2 ** 31 - 1):
                    raise ValueError("invalid pid")
            except (ValueError, UnicodeError):
                return self.send(400, {"error": "invalid-request", "message": "租约请求不正确"})
            if action == "acquire":
                backend.leases.acquire("gui", "main", pid=pid)
            elif action == "refresh":
                backend.leases.refresh("gui", "main")
            else:
                backend.leases.release("gui", "main")
            return self.send(200, {"ok": True})

        def do_POST(self):
            trace_ingest = urlsplit(self.path).path == "/api/model-traces/ingest"
            if trace_ingest and not TRACE_INGRESS.acquire(blocking=False):
                return self.send(503, {"error": "trace-ingress-busy"})
            lease = getattr(backend, "request_lease", None)
            reader_scope = getattr(getattr(backend, "source", None), "request_scope", None)
            try:
                with lease() if callable(lease) else nullcontext():
                    with reader_scope() if callable(reader_scope) else nullcontext():
                        return self._do_POST()
            except RuntimeError:
                return self.send(503, {"error": "bridge-closing"})
            finally:
                if trace_ingest:
                    TRACE_INGRESS.release()

        def control_request(self):
            """Only the Electron main process holds the control token; renderers never see it."""
            supplied = self.headers.get(CONTROL_TOKEN_HEADER, "")
            return (self.client_address[0] == "127.0.0.1" and isinstance(control_token, str) and
                    re.fullmatch(r"[0-9a-f]{64}", control_token) is not None and
                    hmac.compare_digest(supplied, control_token))

        def _file_grant(self):
            """A file or folder the user picked in a host dialog, as a one-time token for one plugin."""
            if not self.control_request():
                return self.send(403, {"error": "forbidden"})
            content_type = [part.strip().lower() for part in self.headers.get("Content-Type", "").split(";")]
            if content_type[0] != "application/json" or any(part != "charset=utf-8" for part in content_type[1:]):
                return self.send(415, {"error": "application/json required"})
            try:
                length = integer(self.headers.get("Content-Length"), None, 65536)
                if length is None:
                    raise ValueError("body required")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(request, dict) or set(request) != {"plugin", "path"}:
                    raise ValueError("invalid file grant")
                return self.send(200, backend.core_services().files.grant(request["plugin"], request["path"]))
            except KeyError:
                return self.send(404, {"error": "not-found", "message": "插件不存在"})
            except (ValueError, UnicodeError):
                return self.send(400, {"error": "invalid-request", "message": "文件授权请求不正确"})

        def _mcp_api(self, method):
            """MCP 专用认证路由：只接受自己的配对凭据与连接令牌，绝不透传其他令牌或路由。"""
            content_type = [part.strip().lower() for part in self.headers.get("Content-Type", "").split(";")]
            if method == "POST" and (content_type[0] != "application/json" or
                                     any(part != "charset=utf-8" for part in content_type[1:])):
                return self.send(415, {"error": "application/json required"})
            body = {}
            if method == "POST":
                try:
                    length = integer(self.headers.get("Content-Length"), None, core_mcp_proxy.MAX_MCP_BODY)
                    if length is None:
                        raise ValueError("body required")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeError):
                    return self.send(400, {"error": "invalid-request", "message": "请求格式不正确"})
            try:
                path = urlsplit(self.path).path
                mcp = backend.core_services().mcp
                if path.startswith(core_mcp_pairing.PAIRING_PREFIX):
                    # G12 one-command pairing: request/poll/cancel/revoke only; nothing on
                    # these loopback routes can approve a pairing (see core_mcp_pairing).
                    result = core_mcp_pairing.handle_pairing_path(mcp, method, path, body)
                else:
                    result = mcp.http_handle(method, path, self.headers, body)
            except core_mcp_proxy.McpError as exc:
                return self.send(exc.status, {"error": exc.code, "message": exc.message})
            except RuntimeError:
                return self.send(503, {"error": "bridge-closing"})
            except Exception:
                return self.send(503, {"error": "core-unavailable", "message": "MCP 服务暂时不可用"})
            return self.send(200, result)

        def _do_POST(self):
            if not self.trusted_request():
                return self.send(403, {"error": "forbidden"})
            if not self.ui_session_allowed("POST"):
                return None
            endpoint = urlsplit(self.path).path
            if endpoint in ("/api/control/lease", "/api/control/background"):
                return self._control_lease(endpoint)
            if endpoint.startswith(core_mcp_proxy.HTTP_PREFIX):
                return self._mcp_api("POST")
            if endpoint.startswith(plugin_api.PREFIX):
                return self._plugin_api("POST")
            if endpoint.startswith(external_import_http.PREFIX):
                return external_import_http.handle(self, backend.core_services(), "POST")
            if endpoint == "/api/plugins/file-grant":
                return self._file_grant()
            if endpoint == "/api/control/shutdown":
                if not self.control_request():
                    return self.send(403, {"error": "forbidden"})
                if self.path != endpoint or self.headers.get("Content-Length") != "0":
                    return self.send(400, {"error": "invalid control request"})
                self.send(202, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            model_endpoints = ("/api/model-source/list", "/api/model-source/test",
                               "/api/model-source/activate", "/api/model-source/clear-key",
                               "/api/model-source/reveal-key", "/api/model-source/models/save",
                               "/api/model-source/models/delete", "/api/model-source/roles",
                               "/api/model-source/local-judge/prepare", "/api/model-source/local-judge/cancel",
                               "/api/model-source/local-judge/delete")
            if endpoint not in ("/api/messages/batch", "/api/conversation-selection",
                                 "/api/data-root", "/api/data-root/clear",
                                 "/api/media/settings", "/api/media/cache/clear", "/api/media/archive",
                                 *friend_http.POST_PATHS, *model_traces_http.POST_PATHS,
                                 *core_http.POST_PATHS,
                                 *model_endpoints):
                return self.send(404, {"error": "not found"})
            content_type = [part.strip().lower() for part in self.headers.get("Content-Type", "").split(";")]
            if content_type[0] != "application/json" or any(part != "charset=utf-8" for part in content_type[1:]):
                return self.send(415, {"error": "application/json required"})
            echo = {}
            try:
                limit = MAX_RPC_BODY if endpoint == "/api/plugins/rpc" else MAX_POST_BODY
                length = integer(self.headers.get("Content-Length"), None, limit)
                if length is None:
                    raise ValueError("body required")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(request, dict) or "texts" in request:
                    raise ValueError("invalid request")
                if endpoint in model_traces_http.POST_PATHS:
                    backend.model_traces.transport_url = f"http://127.0.0.1:{self.server.server_port}"
                    try:
                        return self.send(200, model_traces_http.post(backend.model_traces, endpoint, request,
                                                                   self.headers, control_token))
                    except TraceError as exc:
                        return self.send(exc.status, {"error": exc.code, "message": exc.message})
                if endpoint in ("/api/media/settings", "/api/media/cache/clear", "/api/media/archive"):
                    account = user_value(request.get("account"))
                    if endpoint == "/api/media/settings":
                        if set(request) - {"account", "mode", "cacheMaxBytes"}:
                            raise ValueError("invalid media settings")
                        return self.send(200, backend.source.media_settings(account,
                            {key: value for key, value in request.items() if key != "account"}))
                    if endpoint == "/api/media/cache/clear":
                        if set(request) != {"account"}:
                            raise ValueError("invalid cache request")
                        return self.send(200, backend.source.media_settings(account, clear=True))
                    if set(request) != {"account", "user", "id"}:
                        raise ValueError("invalid media archive request")
                    try:
                        return self.send(200, backend.source.media_archive(account,
                            user_value(request["user"]), user_value(request["id"])))
                    except MediaUnavailable as exc:
                        return self.send(404, {"error": "media unavailable", "reason": str(exc)})
                if endpoint in core_http.POST_PATHS:
                    return self._host_core("POST", endpoint, request)
                if endpoint in friend_http.POST_PATHS:
                    try:
                        return self.send(200, friend_http.post(backend.account_data().friends, endpoint, request))
                    except AdvisorError as exc:
                        return self.send(error_status(exc), {"error": exc.code, "message": exc.message})
                if endpoint in model_endpoints:
                    try:
                        if endpoint.startswith("/api/model-source/local-judge/"):
                            return self.send(200, backend.model_source_local_judge(endpoint.rsplit("/", 1)[1], request))
                        if endpoint == "/api/model-source/list":
                            return self.send(200, backend.model_source_list(request))
                        if endpoint == "/api/model-source/test":
                            return self.send(200, backend.model_source_test(request))
                        if endpoint == "/api/model-source/activate":
                            return self.send(200, backend.model_source_activate(request))
                        if endpoint == "/api/model-source/reveal-key":
                            return self.send(200, backend.model_source_reveal_key(request))
                        if endpoint == "/api/model-source/models/save":
                            return self.send(200, backend.model_source_models_save(request))
                        if endpoint == "/api/model-source/models/delete":
                            return self.send(200, backend.model_source_models_delete(request))
                        if endpoint == "/api/model-source/roles":
                            return self.send(200, backend.model_source_roles(request))
                        return self.send(200, backend.model_source_clear_key(request))
                    except ValueError:
                        return self.send(400, {"error": "invalid model source request"})
                    except ModelSourceUnavailable as exc:
                        return self.send(503, {"error": str(exc)})
                    except Exception:
                        return self.send(503, {"error": "model source unavailable"})
                if endpoint == "/api/conversation-selection":
                    if set(request) == {"expectedAccount", "all"} and request["all"] is True:
                        return self.send(200, backend.set_conversation_all_selected(
                            user_value(request["expectedAccount"])))
                    if set(request) == {"expectedAccount", "sessions", "selected"}:
                        if request["selected"] is not False:
                            raise ValueError("invalid conversation selection")
                        return self.send(200, backend.remove_conversations_selected(
                            user_value(request["expectedAccount"]), request["sessions"]))
                    if set(request) != {"expectedAccount", "session", "selected"} or type(request["selected"]) is not bool:
                        raise ValueError("invalid conversation selection")
                    return self.send(200, backend.set_conversation_selected(
                        user_value(request["expectedAccount"]), user_value(request["session"]),
                        request["selected"]))
                if endpoint == "/api/data-root":
                    if set(request) != {"path"}:
                        raise ValueError("invalid data root request")
                    return self.send(200, backend.configure_data_root(request["path"]))
                if endpoint == "/api/data-root/clear":
                    if request:
                        raise ValueError("invalid data root request")
                    return self.send(200, backend.clear_data_root())
                if endpoint == "/api/messages/batch":
                    account = user_value(request.get("account"))
                    users = request.get("users")
                    if not isinstance(users, list) or not 1 <= len(users) <= 64:
                        raise ValueError("invalid users")
                    users = [user_value(user) for user in users]
                    if len(set(users)) != len(users):
                        raise ValueError("duplicate users")
                    return self.send(200, backend.message_windows(account, users))
                return self.send(404, {"error": "not found"})
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                return self.send(400, {**echo, "error": str(exc)})
            except Exception as exc:
                return self.send(503, {**echo, "error": type(exc).__name__, "message": str(exc)[:200]})

        def do_DELETE(self):
            if not self.trusted_request():
                return self.send(403, {"error": "forbidden"})
            if not self.ui_session_allowed("DELETE"):
                return None
            path = urlsplit(self.path).path
            if path.startswith(plugin_api.PREFIX):
                return self._plugin_api("DELETE")
            prefix = "/api/accounts/"
            if accounts is None or not path.startswith(prefix):
                return self.send(404, {"error": "not found"})
            try:
                result = accounts.delete(path[len(prefix):])
                try:
                    return self.send(200, result)
                finally:
                    if result.get("exitApp"):
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
            except AccountConflict as exc:
                return self.send(409, {"error": "account-busy", "message": str(exc)})
            except AccountNotFound as exc:
                return self.send(404, {"error": "account-not-found", "message": str(exc)})
            except ValueError:
                return self.send(400, {"error": "invalid-account"})
            except OSError:
                return self.send(409, {"error": "account-busy", "message": "账号缓存正在使用，请稍后重试"})
            except Exception:
                return self.send(503, {"error": "account-unavailable", "message": "暂时无法清理账号数据，请稍后重试"})

    return Handler


def main(classifier):
    from account_api import AccountAPI
    from data_root_source import DataRootSource
    control_token = os.environ.pop(CONTROL_TOKEN_ENV, None)
    data_root_store = DataRootSource(ROOT)
    data_root_store.apply()
    backend = Backend(WeChatSource(classifier=classifier), data_root_store=data_root_store)
    port = integer(os.environ.get("CHATUI_PORT"), default_port(ROOT), 65535)
    backend.model_traces.transport_url = f"http://127.0.0.1:{port}"
    backend.core_url = f"http://127.0.0.1:{port}"
    backend.model_traces.snapshot()
    accounts = AccountAPI(backend, ROOT / ".local" / "real-client-data")
    # Modified 2026-10-27: MCP 高影响删除经既有账号清理链执行（core_mcp_manage）；
    # Core 持有它的只读引用，不复制清理逻辑。
    backend.account_api = accounts
    server = LocalHTTPServer(("127.0.0.1", port), make_handler(backend, accounts, control_token))
    print(f"chatui server on http://127.0.0.1:{port}", flush=True)
    # Modified 2026-10-27: the shared Core stops itself once the last lease (GUI,
    # MCP connection or task) has ended and the bounded idle period passed. A Core
    # that never held a lease keeps the previous run-until-stopped behavior.
    stop_monitor = threading.Event()

    def watch_idle():
        from core_leases import REAP_INTERVAL_SECONDS
        while not stop_monitor.wait(REAP_INTERVAL_SECONDS):
            backend.leases.reap_dead()
            if backend.leases.should_exit():
                threading.Thread(target=server.shutdown, daemon=True).start()
                return

    threading.Thread(target=watch_idle, name="core-idle-exit", daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop_monitor.set()
        server.server_close()
        backend.shutdown()
    return 0
