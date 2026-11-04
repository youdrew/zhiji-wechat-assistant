"""zhiji-events: print Zhiji new-message notifications for scripts and agent hooks.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Stdlib only. Connects to an already running Core of the installation given by
``--root`` (default: the installation this script belongs to) with the same MCP
client credentials as ``mcp-stdio-adapter.py``, in the same order of precedence:
``--profile NAME`` (the DPAPI-protected connection saved by ``zhiji-mcp-cli.py
connect``), a ``--config`` JSON file ``{"clientId", "credential"}``, then
``KYF_MCP_CLIENT_ID`` / ``KYF_MCP_CREDENTIAL``. It never starts Core: when Core is not running it prints ``知己未运行`` and exits 0.

Events are metadata only (account/conversation ids, counts, times). Nothing is
stored locally; resuming relies on the server-side consumer cursor (``resume``),
and every poll's cursor acknowledges what was printed before it.

Usage::

    zhiji-events.py [--once | --follow] [--consumer NAME] [--format jsonl|text|claude-hook]
                    [--root PATH] [--profile NAME | --config FILE] [--wait SECONDS]

``--format claude-hook`` prints a Claude Code ``UserPromptSubmit`` hook payload only
when there are new messages, and nothing otherwise.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from zhiji_mcp_http import post_json
_LAUNCHER_SPEC = importlib.util.spec_from_file_location("real_client_launcher", SCRIPTS / "start-real-client.py")
launcher = importlib.util.module_from_spec(_LAUNCHER_SPEC)
sys.modules.setdefault(_LAUNCHER_SPEC.name, launcher)
_LAUNCHER_SPEC.loader.exec_module(launcher)

CLIENT_ID_ENV = "KYF_MCP_CLIENT_ID"
CREDENTIAL_ENV = "KYF_MCP_CREDENTIAL"
ACTION = "zhiji_poll_events"
FOLLOW_WAIT = 25
HANDSHAKE_TIMEOUT = 10.0
HOOK_POLL_BUDGET = 5.0
HOOK_ACK_BUDGET = 2.0
RETRY_SECONDS = 5.0
ONCE_PAGES = 20
NOT_RUNNING = "知己未运行"
FORMATS = ("jsonl", "text", "claude-hook")
CURSOR_ERRORS = ("cursor-invalid", "cursor-stale")


class CliError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class CoreDown(Exception):
    pass


class HookBudgetExpired(Exception):
    pass


def request_timeout(timeout, deadline):
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HookBudgetExpired()
    return min(timeout, remaining)


def load_credentials(config_path, profile=None):
    """``--profile`` (DPAPI connection saved by ``connect``) first, then the config file, then
    environment -- the adapter's order; values never reach argv or output."""
    if profile is not None:
        from zhiji_mcp_credentials import CredentialError, load_adapter_credentials
        try:
            return load_adapter_credentials(profile)
        except CredentialError as exc:
            raise CliError(exc.code, exc.message) from None
    if config_path is not None:
        try:
            document = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CliError("invalid-config", f"凭据配置文件不可用：{exc}") from None
        client_id = document.get("clientId") if isinstance(document, dict) else None
        credential = document.get("credential") if isinstance(document, dict) else None
        if not isinstance(client_id, str) or not client_id or not isinstance(credential, str) or not credential:
            raise CliError("invalid-config", "配置文件需要 clientId 与 credential")
        return client_id, credential
    client_id, credential = os.environ.get(CLIENT_ID_ENV), os.environ.get(CREDENTIAL_ENV)
    if not client_id or not credential:
        raise CliError("missing-credentials",
                       f"需要 --profile 连接名、--config 配置文件或 {CLIENT_ID_ENV}/{CREDENTIAL_ENV} 环境变量")
    return client_id, credential


class Core:
    """A running Core of one installation; never launches one."""

    def __init__(self, root, client_id, credential):
        self.root = Path(root)
        self.client_id, self.credential = client_id, credential
        self.url = None
        self.token = None

    def resolve(self):
        portable = self.root / "runtime" / "python" / "python.exe"
        python_exe = portable if portable.is_file() else Path(sys.executable)
        try:
            base = launcher.Config(self.root, python_exe, launcher.default_port(self.root))
            # Read-only: never pick or record a fallback port.
            port = launcher.selected_port(base, allow_fallback=False)
            config = launcher.Config(self.root, python_exe, port)
            state = launcher.health(config)
        except Exception:
            raise CoreDown() from None
        if state != "ready":
            raise CoreDown()
        self.url = config.url

    def _post(self, path, body, timeout, token=None):
        _, result = post_json(self.url, path, body, timeout=timeout, token=token)
        return result

    def handshake(self, *, deadline=None):
        try:
            result = self._post("/api/mcp/handshake", {"clientId": self.client_id, "credential": self.credential,
                                                       "pid": os.getpid()},
                                request_timeout(HANDSHAKE_TIMEOUT, deadline))
        except HTTPError as exc:
            raise CliError(*_http_error(exc)) from None
        except (URLError, socket.timeout, OSError):
            request_timeout(HANDSHAKE_TIMEOUT, deadline)
            raise CoreDown() from None
        self.token = result["token"]

    def call(self, params, timeout, *, budget=None):
        deadline = time.monotonic() + budget if budget is not None else None
        if self.url is None:
            self.resolve()
        if self.token is None:
            self.handshake(deadline=deadline)
        for attempt in (1, 2):
            try:
                result = self._post("/api/mcp/rpc", {"action": ACTION, "params": params},
                                    request_timeout(timeout, deadline), self.token)
                return result.get("result", result) if isinstance(result, dict) else {}
            except HTTPError as exc:
                code, message = _http_error(exc)
                if exc.code == 401 and attempt == 1:
                    self.handshake(deadline=deadline)
                    continue
                if exc.code == 403 and code == "stale-authorization" and attempt == 1:
                    # The authorization version changed (capabilities shrunk or grew): re-pair
                    # once and retry. A cursor of the old version then comes back as
                    # cursor-stale and the caller re-baselines; a revoked or disabled client
                    # fails the handshake and the error surfaces.
                    self.token = None
                    self.handshake(deadline=deadline)
                    continue
                raise CliError(code, message) from None
            except (URLError, socket.timeout, OSError):
                self.url = self.token = None
                request_timeout(timeout, deadline)
                raise CoreDown() from None


def _http_error(exc):
    try:
        with exc:
            payload = json.loads(exc.read().decode("utf-8"))
        return payload.get("error", "core-error"), payload.get("message", "本地服务返回错误")
    except Exception:
        return "core-error", f"本地服务返回 HTTP {exc.code}"


# -- output ---------------------------------------------------------------------

def _clock(ms):
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000)) if isinstance(ms, (int, float)) else "--"


def _ref(event):
    subject = event.get("subject") or {}
    return f"{event.get('accountId') or subject.get('accountId') or '?'}/{subject.get('subjectId') or '?'}"


def text_line(event):
    if event.get("type") == "message.new":
        count = event.get("newCount")
        count_text = f"{count} 条" if event.get("countExact") else f"约 {count} 条"
        kind = "群聊" if event.get("isGroup") else "会话"
        suffix = "（补报）" if event.get("catchUp") else ""
        return f"[{_clock(event.get('lastTime'))}] {kind} {_ref(event)}：{count_text}新消息{suffix}"
    if event.get("type") == "source.state":
        account = f" {event['accountId']}" if event.get("accountId") else ""
        return f"[{_clock(event.get('observedAt'))}] 来源状态{account}：{event.get('state')}"
    return json.dumps(event, ensure_ascii=False)


def hook_payload(events):
    """The UserPromptSubmit hook output for new messages, or ``None`` when there are none."""
    counts = {}
    for event in events:
        if event.get("type") == "message.new":
            counts[_ref(event)] = counts.get(_ref(event), 0) + int(event.get("newCount") or 0)
    if not counts:
        return None
    refs = "、".join(f"{ref} ×{count}" for ref, count in counts.items())
    summary = f"知己：{len(counts)} 个会话有新消息（{refs}），需要时用 zhiji 工具读取"
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": summary}}


def emit(events, fmt, out):
    if fmt == "claude-hook":
        payload = hook_payload(events)
        if payload is not None:
            out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        for event in events:
            out.write((json.dumps(event, ensure_ascii=False) if fmt == "jsonl" else text_line(event)) + "\n")
    out.flush()


# -- modes ------------------------------------------------------------------------

def poll(core, consumer, cursor, wait, *, budget=None):
    params = {"waitSeconds": wait, "consumer": consumer, "resume": cursor is None}
    if cursor is not None:
        params["cursor"] = cursor
    return core.call(params, timeout=wait + 15, budget=budget)


def run_once(core, consumer, fmt, wait, out):
    """Everything pending since this consumer's acknowledged position, then acknowledge it."""
    hook = fmt == "claude-hook"
    result = poll(core, consumer, None, wait, budget=HOOK_POLL_BUDGET + wait if hook else None)
    # Each page is printed before its cursor goes back as the acknowledgement, so an
    # acknowledged event has always been printed -- including the last page, whose
    # cursor is sent once more; that final poll's page is neither printed nor
    # acknowledged and comes first in the next run. A hook prints one summary of one page.
    pages = 1 if hook else ONCE_PAGES
    for _ in range(pages):
        events, cursor = list(result.get("events") or []), result.get("cursor")
        emit(events, fmt, out)
        if not events or not cursor:
            return 0
        try:
            # Give the acknowledgement its own short budget after flushing the
            # output. If it cannot reach Core, resume still sees the last ACK.
            result = poll(core, consumer, cursor, 0, budget=HOOK_ACK_BUDGET if hook else None)
        except HookBudgetExpired:
            return 0
        except CliError as exc:
            if exc.code in CURSOR_ERRORS:
                return 0  # authorization changed mid-run: the next run starts a fresh baseline
            raise
    if result.get("events") and not hook:
        sys.stderr.write(f"还有未读完的事件（单次最多 {ONCE_PAGES} 页），再次运行继续\n")
    return 0


def run_follow(core, consumer, fmt, out, stop=None):
    cursor = None
    while stop is None or not stop():
        try:
            result = poll(core, consumer, cursor, FOLLOW_WAIT)
        except CliError as exc:
            if exc.code in CURSOR_ERRORS:
                cursor = None  # authorization changed: start again from a fresh baseline
                continue
            raise
        except CoreDown:
            # Core stopped: wait for it to come back (never start it), keep the cursor.
            time.sleep(RETRY_SECONDS)
            continue
        emit(list(result.get("events") or []), fmt, out)
        cursor = result.get("cursor") or cursor
    return 0


def parse_args(argv):
    options = {"mode": "once", "consumer": "cli", "format": "jsonl", "root": None, "config": None, "profile": None,
               "wait": 0}
    index = 0
    while index < len(argv):
        option = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else None
        if option == "--once":
            options["mode"] = "once"
        elif option == "--follow":
            options["mode"] = "follow"
        elif option in ("--consumer", "--format", "--root", "--config", "--profile", "--wait") and value is not None:
            options[option[2:]] = value
            index += 1
        else:
            raise CliError("invalid-args", f"未知参数：{option}")
        index += 1
    if options["format"] not in FORMATS:
        raise CliError("invalid-args", f"--format 只能是 {'/'.join(FORMATS)}")
    try:
        options["wait"] = int(options["wait"])
    except ValueError:
        raise CliError("invalid-args", "--wait 须为整数秒") from None
    if not 0 <= options["wait"] <= FOLLOW_WAIT:
        raise CliError("invalid-args", f"--wait 须在 0-{FOLLOW_WAIT} 之间")
    return options


def main(argv=None, out=None):
    out = out or sys.stdout
    argv = list(sys.argv[1:] if argv is None else argv)
    fmt = "jsonl"
    try:
        options = parse_args(argv)
        fmt = options["format"]
        client_id, credential = load_credentials(options["config"], options["profile"])
        core = Core(Path(options["root"]) if options["root"] else SCRIPTS.parent, client_id, credential)
        if options["mode"] == "follow":
            return run_follow(core, options["consumer"], fmt, out)
        return run_once(core, options["consumer"], fmt, options["wait"], out)
    except HookBudgetExpired:
        return 0
    except CoreDown:
        # A hook must stay silent in the prompt; humans get the notice on stdout.
        (sys.stderr if fmt == "claude-hook" else out).write(NOT_RUNNING + "\n")
        return 0
    except CliError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
