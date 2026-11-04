"""zhiji MCP install/connect CLI (G12): one command from "installed" to "client can call Zhiji".

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Runs from the installation (``<安装目录>\\resources\\client\\runtime\\python\\python.exe
<安装目录>\\resources\\client\\scripts\\zhiji-mcp-cli.py connect --client claude-code``) and
from the source tree; needs no ``zhiji`` command on PATH and only the standard library.

    connect    --client <claude-code|codex|cursor|deepseek-harness|generic> [--scope user|project]
               [--name NAME] [--project-dir DIR] [--dry-run] [--wait SECONDS] [--install-skill]
               [--install-hook]
    status     installation, Core, saved connections and client configs (starts nothing)
    doctor     status plus a real MCP round trip for every configured client
    disconnect --client ... [--name NAME] [--keep-profile]

Flow of ``connect``: discover the installation → start/reuse its headless Core → reuse a
saved credential that still handshakes, otherwise request a pairing that the user
approves in the local pairing window (or 设置 → 插件 → MCP) → save the credential with
DPAPI (zhiji_mcp_credentials) → merge the client's config (backup first, unrelated
settings kept, configs reference the profile name only) → spawn the configured command
and verify initialize, tools/list, zhiji_ping and zhiji_list_accounts.

``--install-skill`` (Claude Code only) also copies the optional Agent Skill shipped in
``plugins/kyf.mcp/skill/zhiji`` to ``~/.claude/skills/zhiji`` (``--scope user``) or
``<project>/.claude/skills/zhiji`` (``--scope project``): unchanged when identical,
otherwise the old folder is first moved to a ``zhiji-skill-backup-<time>`` folder beside
``skills`` (outside it, so Claude Code never loads the backup as a second skill).

``--install-hook`` (Claude Code only) merges a ``UserPromptSubmit`` hook into
``~/.claude/settings.json`` (``--scope user``) or ``<project>/.claude/settings.local.json``
(``--scope project``) that runs ``zhiji-events.py --once --format claude-hook --profile
<name> --consumer claude-hook`` with the installation's Python, so new WeChat messages
reach the model as context on the user's next prompt. The hook uses Claude Code's
exec-form arguments (v2.1.139 or later). Other settings and hooks are kept, changed
files are backed up, and ``disconnect`` removes the managed entry again. Run
``disconnect`` for the configured scope before uninstalling the application.

The credential is never printed, never put in argv and never written to a client config.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from zhiji_mcp_http import loopback_origin, post_json
# Filled in by the release build; a placeholder means "ask the publisher for the installer".
DOWNLOAD_URL = "__ZHIJI_DOWNLOAD_URL__"
PRODUCT_NAMES = ("知己微信助手", "Zhiji WeChat Assistant")
CLIENTS = ("claude-code", "codex", "cursor", "deepseek-harness", "generic")
CLIENT_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor",
                 "deepseek-harness": "DeepSeek Harness", "generic": "其他 MCP 客户端"}
CAPABILITY_LABELS = {"write": "修改资料与发起分析", "manage": "全局管理"}
RESTART_NOTES = {
    "claude-code": "在 /mcp 查看状态。项目 .mcp.json 已更新时可先尝试 Reconnect；仍未加载请新开会话。",
    "codex": "需要重启 Codex 才会加载新的 MCP 服务器。",
    "cursor": "如果新服务器未出现，请重启 Cursor 并在 MCP 设置中查看。",
    "deepseek-harness": "开启 HMR 的 profile 可自动重载配置；若未开启或未加载，请重启对应 profile。",
    "generic": "请按客户端说明粘贴配置并重新加载。",
}
EXIT_OK, EXIT_ERROR, EXIT_NOT_INSTALLED, EXIT_NOT_PAIRED, EXIT_CONFIG, EXIT_VERIFY, EXIT_WAITING = 0, 1, 2, 3, 4, 5, 6
HTTP_TIMEOUT = 15.0
POLL_INTERVAL = 1.0
VERIFY_TIMEOUT = 90.0
NO_DISCOVERY_ENV = "ZHIJI_CLI_NO_SYSTEM_DISCOVERY"  # tests: skip registry and known install dirs
CORE_URL_ENV = "ZHIJI_CORE_URL"                      # tests/advanced: use this Core, start nothing
HOME_ENV = "ZHIJI_CLI_HOME"                          # tests: stand-in for the user's home directory


class CliError(Exception):
    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.message, self.code = message, code


def say(message=""):
    print(message, flush=True)


def home():
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else Path.home()


def claude_config_override():
    value = None if os.environ.get(HOME_ENV) else os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else None


# -- installation discovery --------------------------------------------------------

class Installation:
    def __init__(self, root, python, source, dev):
        self.root, self.python, self.source, self.dev = Path(root), Path(python), source, dev
        self.adapter = self.root / "scripts" / "mcp-stdio-adapter.py"

    @property
    def version(self):
        try:
            return json.loads((self.root / "package.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            return None

    def command(self, profile):
        """The stable launch command written into client configs (no secret, profile name only)."""
        return str(self.python), [str(self.adapter), "--root", str(self.root), "--profile", profile]

    @property
    def events_script(self):
        return self.root / "scripts" / HOOK_SCRIPT

    def hook_command(self, profile):
        """Claude Code executes the bundled Python directly with these arguments."""
        return str(self.python), _hook_args(str(self.root), profile)


def _client_root(path):
    """Accept the install dir (``…\\resources\\client`` inside) or the client root itself."""
    path = Path(path)
    nested = path / "resources" / "client"
    return nested if nested.is_dir() else path


def _valid_root(root):
    return (root / "bridge" / "chat_server.py").is_file() and (root / "scripts" / "mcp-stdio-adapter.py").is_file()


def _bundled_python(root):
    return root / "runtime" / "python" / "python.exe"


def _registry_locations():
    try:
        import winreg
    except ImportError:
        return []
    found = []
    hives = ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"))
    for hive, base in hives:
        try:
            parent = winreg.OpenKey(hive, base)
        except OSError:
            continue
        with parent:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(parent, name) as key:
                        display = str(winreg.QueryValueEx(key, "DisplayName")[0])
                        if not any(display.startswith(product) for product in PRODUCT_NAMES):
                            continue
                        try:
                            found.append(Path(str(winreg.QueryValueEx(key, "InstallLocation")[0])))
                        except OSError:
                            uninstall = str(winreg.QueryValueEx(key, "UninstallString")[0]).strip('"')
                            found.append(Path(uninstall).parent)
                except OSError:
                    continue
    return found


def _known_locations():
    local = os.environ.get("LOCALAPPDATA")
    programs = os.environ.get("ProgramFiles")
    found = []
    if local:
        found += [Path(local) / "Programs" / name for name in ("知己微信助手", "know-your-friends", "KnowYourFriends")]
    if programs:
        found.append(Path(programs) / "知己微信助手")
    return found


def find_installation(args):
    """The installation the configs will point at; ``None`` when nothing is installed."""
    if args.dev:
        root = _client_root(args.root) if args.root else SCRIPT_DIR.parent
        if not _valid_root(root):
            raise CliError(f"--dev 指定的目录不是知己源码或安装目录：{root}")
        python = _bundled_python(root)
        return Installation(root, python if python.is_file() else sys.executable, "dev", True)
    explicit = args.root or os.environ.get("ZHIJI_ROOT")
    if explicit:
        root = _client_root(explicit)
        if not _valid_root(root):
            raise CliError(f"不是知己安装目录：{explicit}（应包含 resources\\client\\bridge 与 scripts）")
        if not _bundled_python(root).is_file():
            raise CliError(f"安装不完整：缺少 {_bundled_python(root)}；源码目录请加 --dev")
        return Installation(root, _bundled_python(root), "--root", False)
    own = SCRIPT_DIR.parent
    if _valid_root(own) and _bundled_python(own).is_file() and own.name == "client" and \
            own.parent.name == "resources":
        return Installation(own, _bundled_python(own), "本脚本所在安装", False)
    if os.environ.get(NO_DISCOVERY_ENV):
        return None
    for source, locations in (("注册表", _registry_locations()), ("默认安装目录", _known_locations())):
        for location in locations:
            root = _client_root(location)
            if _valid_root(root) and _bundled_python(root).is_file():
                return Installation(root, _bundled_python(root), source, False)
    return None


def not_installed_message():
    lines = ["安装：未安装 —— 没有在本机找到知己微信助手。"]
    if DOWNLOAD_URL.startswith("__"):
        lines.append("请向发布者获取 Windows 安装包（Zhiji-WeChat-Assistant-<版本>-Setup-x64.exe），安装后重新运行本命令。")
    else:
        lines.append(f"请下载并运行安装包：{DOWNLOAD_URL}，安装后重新运行本命令。")
    lines.append("已安装在其他位置时可加 --root <安装目录>；在源码目录开发时可加 --dev。")
    return "\n".join(lines)


# -- credentials helper (shipped beside this script) --------------------------------

def credentials(inst=None):
    """The shared DPAPI helper: beside this script, else the installation's copy."""
    for directory in (SCRIPT_DIR, inst.root / "scripts" if inst else None):
        if directory is None or not (directory / "zhiji_mcp_credentials.py").is_file():
            continue
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        import zhiji_mcp_credentials
        return zhiji_mcp_credentials
    raise CliError("缺少凭据模块 zhiji_mcp_credentials.py，安装可能不完整")


# -- Core ------------------------------------------------------------------------------

def http_post(url, path, body, timeout=HTTP_TIMEOUT):
    """``(status, payload)``; network failures raise CliError, HTTP errors return their body."""
    try:
        return post_json(url, path, body, timeout=timeout)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        return exc.code, payload if isinstance(payload, dict) else {}
    except (URLError, OSError, ValueError) as exc:
        raise CliError(f"本地服务无法连接：{exc}") from None


def _launcher(inst):
    spec = importlib.util.spec_from_file_location("zhiji_cli_launcher", inst.root / "scripts" / "start-real-client.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def core_url(inst, *, start=True):
    """The installation's loopback Core URL; starts the headless Core when ``start``."""
    override = os.environ.get(CORE_URL_ENV)
    if override:
        try:
            return loopback_origin(override)
        except URLError as exc:
            raise CliError(str(exc.reason)) from None
    launcher = _launcher(inst)
    try:
        base = launcher.Config(inst.root, inst.python, launcher.default_port(inst.root))
        port = launcher.selected_port(base, allow_fallback=start)
        config = launcher.Config(inst.root, inst.python, port)
        state = launcher.health(config)
        if state != "ready" and start:
            say("正在启动知己后台服务（无界面）…")
            # Same automatic path as the stdio adapter: honours the post-clearing latch.
            launcher.ensure_service(config, recovery=True)
            state = launcher.health(config)
    except launcher.LauncherError as exc:
        raise CliError(f"知己后台服务无法启动：{exc}") from None
    if state != "ready":
        if not start:
            return None
        raise CliError(f"知己后台服务不可用（{state}）；请先打开一次知己再重试")
    return config.url


def handshake(url, client_id, credential):
    """``"ok"`` / ``"invalid"`` / an error code such as ``mcp-disabled``."""
    status, payload = http_post(url, "/api/mcp/handshake",
                                {"clientId": client_id, "credential": credential, "pid": os.getpid()})
    if status == 200 and payload.get("token"):
        return "ok"
    if status == 401:
        return "invalid"
    return payload.get("error") or f"http-{status}"


# -- client config adapters --------------------------------------------------------------

def server_key(client, name):
    return "zhiji" if name == client else "zhiji-" + name


def backup(path, *, directory=None):
    """Copy an existing config before changing it and return the backup path."""
    if not path.exists():
        return None
    directory = path.parent if directory is None else directory
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{path.name}.zhiji-backup-{stamp}"
    counter = 1
    while target.exists():
        target = directory / f"{path.name}.zhiji-backup-{stamp}-{counter}"
        counter += 1
    shutil.copy2(path, target)
    return target


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise CliError(f"无法读取配置文件 {path}：{exc}", EXIT_CONFIG) from None


class JsonServersConfig:
    """``{"mcpServers": {key: entry}}`` in one JSON file (Claude Code, Cursor)."""

    def __init__(self, path, *, stdio_type=False):
        self.path, self.stdio_type = Path(path), stdio_type

    def _load(self, text):
        if text is None or not text.strip():
            return {}
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise CliError(f"{self.path} 不是有效 JSON（{exc}），未作修改；请先修复或移走该文件",
                           EXIT_CONFIG) from None
        if not isinstance(document, dict):
            raise CliError(f"{self.path} 顶层不是对象，未作修改", EXIT_CONFIG)
        if not isinstance(document.get("mcpServers", {}), dict):
            raise CliError(f"{self.path} 的 mcpServers 不是对象，未作修改", EXIT_CONFIG)
        return document

    def entry(self, command, args):
        entry = {"command": command, "args": args}
        return {"type": "stdio", **entry, "env": {}} if self.stdio_type else entry

    def render(self, text, key, command, args):
        document = self._load(text)
        servers = document.setdefault("mcpServers", {})
        servers[key] = self.entry(command, args)
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    def remove(self, text, key):
        document = self._load(text)
        servers = document.get("mcpServers") or {}
        if key not in servers:
            return None
        del servers[key]
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    def read(self, text, key):
        entry = (self._load(text).get("mcpServers") or {}).get(key)
        if isinstance(entry, dict) and isinstance(entry.get("command"), str) and isinstance(entry.get("args"), list):
            return entry["command"], [str(item) for item in entry["args"]]
        return None


TOML_HEADER = re.compile(r"^\s*\[\[?\s*(?P<name>[^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _toml_name(raw):
    """Header path as a tuple of keys (handles bare, "basic" and 'literal' segments)."""
    parts, current, quote = [], "", None
    for char in raw.strip():
        if quote:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in ("'", '"'):
            quote = char
        elif char == ".":
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return tuple(parts)


def _toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _quoted(key)


def _tomllib():
    try:
        import tomllib
        return tomllib
    except ImportError:  # Python < 3.11: text checks only
        return None


def _quoted(value):
    """A JSON string literal: valid as a TOML basic string and as a YAML double-quoted scalar."""
    return json.dumps(value, ensure_ascii=False)


class CodexTomlConfig:
    """``[mcp_servers.<key>]`` in ``~/.codex/config.toml``, merged as text (no TOML writer)."""

    def __init__(self, path):
        self.path = Path(path)

    def _block(self, lines, key):
        table = ("mcp_servers", key)
        start = end = None
        for index, line in enumerate(lines):
            match = TOML_HEADER.match(line)
            if not match:
                continue
            name = _toml_name(match.group("name"))
            if start is None and name == table:
                start = index
            elif start is not None and name[:2] != table:
                end = index
                break
        if start is not None and end is None:
            end = len(lines)
        return start, end

    def _check(self, lines):
        # Inline or dotted top-level definitions of mcp_servers cannot be merged safely as text.
        for line in lines:
            if TOML_HEADER.match(line):
                break
            if re.match(r"^\s*mcp_servers\s*[.=]", line):
                raise CliError(f"{self.path} 用内联方式定义了 mcp_servers，无法安全合并，请手动添加", EXIT_CONFIG)

    def _parse(self, text, what):
        tomllib = _tomllib()
        if tomllib is None:
            return None
        try:
            return tomllib.loads(text or "")
        except tomllib.TOMLDecodeError as exc:
            raise CliError(f"{self.path} {what}不是有效 TOML（{exc}），未作修改", EXIT_CONFIG) from None

    @staticmethod
    def _without(document, key):
        """The parsed document minus ``mcp_servers.<key>`` (and an emptied ``mcp_servers``)."""
        result = dict(document or {})
        servers = dict(result.get("mcp_servers") or {})
        servers.pop(key, None)
        if servers:
            result["mcp_servers"] = servers
        else:
            result.pop("mcp_servers", None)
        return result

    def render(self, text, key, command, args):
        """Text merge verified with tomllib before and after; anything unsafe is refused untouched."""
        before = self._parse(text, "")
        lines = (text or "").splitlines()
        self._check(lines)
        start, _end = self._block(lines, key)
        if before is not None:
            servers = before.get("mcp_servers", {})
            if not isinstance(servers, dict):
                raise CliError(f"{self.path} 的 mcp_servers 不是表，未作修改", EXIT_CONFIG)
            if key in servers and start is None:
                raise CliError(f"{self.path} 以内联或点号形式定义了 mcp_servers.{key}，无法安全合并；"
                               "请手动删除该条目后重试", EXIT_CONFIG)
        rendered = self._render(lines, key, command, args)
        after = self._parse(rendered, "合并结果")
        if after is not None:
            entry = (after.get("mcp_servers") or {}).get(key)
            if not isinstance(entry, dict) or entry.get("command") != command or entry.get("args") != args or \
                    self._without(after, key) != self._without(before, key):
                raise CliError(f"{self.path} 的结构无法安全合并（例如多行字符串中含表头），未作修改；"
                               "请手动添加知己条目", EXIT_CONFIG)
        return rendered

    def _render(self, lines, key, command, args):
        block = [f"[mcp_servers.{_toml_key(key)}]", f"command = {_quoted(command)}",
                 "args = [" + ", ".join(_quoted(item) for item in args) + "]",
                 # Allow time for Core cold starts and slower tool calls.
                 "startup_timeout_sec = 60", "tool_timeout_sec = 300"]
        start, end = self._block(lines, key)
        if start is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines = lines + ([""] if lines else []) + block
        else:
            tail = lines[end:]
            lines = lines[:start] + block + ([""] if tail else []) + tail
        return "\n".join(lines) + "\n"

    def remove(self, text, key):
        before = self._parse(text, "")
        lines = (text or "").splitlines()
        start, end = self._block(lines, key)
        if start is None:
            if before is not None and key in (before.get("mcp_servers") or {}):
                raise CliError(f"{self.path} 以内联形式定义了 mcp_servers.{key}，请手动删除", EXIT_CONFIG)
            return None
        rest = lines[:start] + lines[end:]
        while rest and not rest[-1].strip():
            rest.pop()
        rendered = "\n".join(rest) + ("\n" if rest else "")
        after = self._parse(rendered, "删除结果")
        if after is not None and self._without(after, key) != self._without(before, key):
            raise CliError(f"{self.path} 的结构无法安全删除知己条目，未作修改", EXIT_CONFIG)
        return rendered

    def read(self, text, key):
        try:
            parsed = self._parse(text, "")
        except CliError:
            return None
        if parsed is not None:
            entry = (parsed.get("mcp_servers") or {}).get(key)
            if isinstance(entry, dict) and isinstance(entry.get("command"), str) and \
                    isinstance(entry.get("args"), list):
                return entry["command"], [str(item) for item in entry["args"]]
            return None
        lines = (text or "").splitlines()
        start, end = self._block(lines, key)
        if start is None:
            return None
        return _command_from(lines[start + 1:end], "=")


def _command_from(lines, separator):
    values = {}
    for line in lines:
        match = re.match(r"^\s*(command|args)\s*" + re.escape(separator) + r"\s*(.+?)\s*$", line)
        if match:
            try:
                values[match.group(1)] = json.loads(match.group(2))
            except ValueError:
                return None
    if isinstance(values.get("command"), str) and isinstance(values.get("args"), list):
        return values["command"], [str(item) for item in values["args"]]
    return None


class HarnessPatchConfig:
    """One managed ``insert`` patch in DSH's user patch layer (``$DSH_HOME/cordis.patch.yml``).

    Format from the official DeepSeek Harness guide (mcp-memory.zh.md); the default
    ``DSH_HOME`` location and reload behaviour are not verified on a real install.
    """

    BEGIN = "# >>> zhiji-mcp {key} (managed by zhiji-mcp-cli) >>>"
    END = "# <<< zhiji-mcp {key} <<<"

    def __init__(self, path):
        self.path = Path(path)

    def _span(self, lines, key):
        begin, end = self.BEGIN.format(key=key), self.END.format(key=key)
        start = next((index for index, line in enumerate(lines) if line.strip() == begin), None)
        if start is None:
            return None, None
        stop = next((index for index in range(start, len(lines)) if lines[index].strip() == end), None)
        if stop is None:
            raise CliError(f"{self.path} 中知己配置块不完整，未作修改；请手动删除 {begin} 起的内容", EXIT_CONFIG)
        return start, stop + 1

    def render(self, text, key, command, args):
        lines = (text or "").splitlines()
        start, stop = self._span(lines, key)
        outside = lines if start is None else lines[:start] + lines[stop:]
        first = next((line for line in outside if line.strip() and not line.lstrip().startswith("#")), None)
        if first is not None and not first.lstrip().startswith("-"):
            raise CliError(f"{self.path} 不是 patch 列表（顶层应为 '- insert:' 等条目），未作修改", EXIT_CONFIG)
        block = [self.BEGIN.format(key=key), "- insert:", f"    - id: mcp-{key}",
                 "      name: '@deepseek-ai/dsh-mcp-client'", "      config:",
                 f"        serverName: {key}", "        transport: stdio",
                 f"        command: {_quoted(command)}",
                 "        args: [" + ", ".join(_quoted(item) for item in args) + "]",
                 "        env: {}", self.END.format(key=key)]
        if start is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines = lines + block
        else:
            lines = lines[:start] + block + lines[stop:]
        return "\n".join(lines) + "\n"

    def remove(self, text, key):
        lines = (text or "").splitlines()
        start, stop = self._span(lines, key)
        if start is None:
            return None
        rest = lines[:start] + lines[stop:]
        return "\n".join(rest) + ("\n" if rest else "")

    def read(self, text, key):
        lines = (text or "").splitlines()
        start, stop = self._span(lines, key)
        if start is None:
            return None
        return _command_from(lines[start:stop], ":")


def config_target(client, scope, project_dir):
    """``(adapter, description)`` of the config this client reads; ``(None, …)`` for generic."""
    base = home()
    testing = bool(os.environ.get(HOME_ENV))
    project = Path(project_dir or os.getcwd())
    if client == "claude-code":
        if scope == "project":
            return JsonServersConfig(project / ".mcp.json", stdio_type=True), "Claude Code 项目级 .mcp.json"
        path = (claude_config_override() or base) / ".claude.json"
        return JsonServersConfig(path, stdio_type=True), "Claude Code 用户级 .claude.json"
    if client == "cursor":
        if scope == "project":
            return JsonServersConfig(project / ".cursor" / "mcp.json"), "Cursor 项目级 .cursor/mcp.json"
        return JsonServersConfig(base / ".cursor" / "mcp.json"), "Cursor 用户级 ~/.cursor/mcp.json"
    if client == "codex":
        if scope == "project":
            raise CliError("Codex 只写用户级配置（~/.codex/config.toml），请去掉 --scope project")
        codex_home = None if testing else os.environ.get("CODEX_HOME")
        path = Path(codex_home) / "config.toml" if codex_home else base / ".codex" / "config.toml"
        return CodexTomlConfig(path), "Codex config.toml"
    if client == "deepseek-harness":
        if scope == "project":
            raise CliError("DeepSeek Harness 只写用户 patch 层（$DSH_HOME/cordis.patch.yml），请去掉 --scope project")
        dsh_home = None if testing else os.environ.get("DSH_HOME")
        path = Path(dsh_home) / "cordis.patch.yml" if dsh_home else base / ".dsh" / "cordis.patch.yml"
        return HarnessPatchConfig(path), "DeepSeek Harness cordis.patch.yml（默认位置未实测）"
    return None, "通用 MCP 配置片段"


def claude_cli():
    """``claude`` only as a native executable: a .cmd shim would re-parse JSON argv through cmd.exe."""
    if os.environ.get("ZHIJI_CLI_NO_CLIENT_CLI"):
        return None
    found = shutil.which("claude")
    return found if found and found.lower().endswith(".exe") else None


def apply_config(client, scope, project_dir, key, command, args, *, dry_run):
    """Merge the entry; ``{"path", "label", "changed", "backup", "via"}`` (generic: path None)."""
    adapter, label = config_target(client, scope, project_dir)
    if adapter is None:
        return {"path": None, "label": label, "changed": False, "backup": None, "via": "snippet"}
    text = read_text(adapter.path)
    rendered = adapter.render(text, key, command, args)
    same_entry = isinstance(adapter, JsonServersConfig) and adapter.read(text, key) == (command, args)
    if rendered == text or same_entry:
        return {"path": adapter.path, "label": label, "changed": False, "backup": None, "via": "unchanged"}
    if dry_run:
        return {"path": adapter.path, "label": label, "changed": True, "backup": None, "via": "dry-run"}
    copy = backup(adapter.path)
    # Existing entries can be replaced in one file operation. Removing first
    # would leave the client unconfigured if add-json or the fallback failed.
    present = isinstance(adapter, JsonServersConfig) and key in (adapter._load(text).get("mcpServers") or {})
    executable = claude_cli() if client == "claude-code" and not present else None
    if executable:
        entry = json.dumps(adapter.entry(command, args), ensure_ascii=False)
        cwd = str(Path(project_dir or os.getcwd()))
        try:
            completed = subprocess.run([executable, "mcp", "add-json", key, entry, "-s", scope], cwd=cwd,
                                       capture_output=True, timeout=60)
            if completed.returncode == 0 and adapter.read(read_text(adapter.path), key) == (command, args):
                return {"path": adapter.path, "label": label, "changed": True, "backup": copy,
                        "via": "claude mcp add-json"}
        except (OSError, subprocess.SubprocessError):
            pass
        # Any CLI problem falls back to the direct merge.
        rendered = adapter.render(read_text(adapter.path), key, command, args)
    write_text(adapter.path, rendered)
    return {"path": adapter.path, "label": label, "changed": True, "backup": copy, "via": "file"}


def remove_config(client, scope, project_dir, key, *, dry_run):
    adapter, label = config_target(client, scope, project_dir)
    if adapter is None:
        return {"path": None, "label": label, "changed": False, "backup": None}
    text = read_text(adapter.path)
    rendered = adapter.remove(text, key) if text is not None else None
    if rendered is None:
        return {"path": adapter.path, "label": label, "changed": False, "backup": None}
    if dry_run:
        return {"path": adapter.path, "label": label, "changed": True, "backup": None}
    copy = backup(adapter.path)
    write_text(adapter.path, rendered)
    return {"path": adapter.path, "label": label, "changed": True, "backup": copy}


def configured_command(client, scope, project_dir, key):
    adapter, _label = config_target(client, scope, project_dir)
    if adapter is None:
        return None
    try:
        return adapter.read(read_text(adapter.path), key)
    except CliError:
        return None


# -- optional new-message hook (Claude Code) ------------------------------------------------------

HOOK_EVENT = "UserPromptSubmit"
HOOK_SCRIPT = "zhiji-events.py"
HOOK_CONSUMER = "claude-hook"
# The event CLI budgets five seconds to fetch and two to acknowledge. Leave
# room for interpreter startup and keep a separate outer Claude timeout.
HOOK_TIMEOUT = 15
HOOK_STATUS = "知己：检查新消息"
HOOK_CLIENTS = ("claude-code",)


def hook_settings_path(scope, project_dir):
    if scope == "project":
        return Path(project_dir or os.getcwd()) / ".claude" / "settings.local.json"
    return (claude_config_override() or home() / ".claude") / "settings.json"


def check_hook_source(inst):
    if not inst.events_script.is_file():
        raise CliError(f"安装中缺少 {inst.events_script}，安装可能不完整；可去掉 --install-hook 继续")


def _hook_args(root, name):
    return [str(Path(root) / "scripts" / HOOK_SCRIPT), "--root", root, "--once", "--format", "claude-hook",
            "--profile", name, "--consumer", HOOK_CONSUMER]


def _legacy_hook_command(command, args):
    script, _, root, *rest = args
    return f'"{command}" "{script}" --root "{root}" ' + " ".join(rest)


def _saved_hook_command(store, name):
    """Use the profile's recorded installation to recognize an old unmarked hook."""
    try:
        info = store.profile_info(name)
    except store.CredentialError:
        return None
    root = info.get("root") if info and info.get("client") == "claude-code" else None
    if isinstance(root, str) and Path(root).is_absolute():
        return str(_bundled_python(Path(root))), _hook_args(root, name)
    return None


def _hook_is_ours(hook, name, known_commands=()):
    """Match a marked exec entry or an exact command tied to a known installation."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command, args = hook.get("command"), hook.get("args")
    if not isinstance(command, str):
        return False
    if "args" not in hook:
        # Old releases wrote this literal shell string. Parsing just selected
        # options would also match wrappers and custom commands.
        return any(command == _legacy_hook_command(*known) for known in known_commands)
    if hook.get("statusMessage") != HOOK_STATUS or not isinstance(args, list):
        return False
    if (command, args) in known_commands:
        return True
    return (Path(command).is_absolute()
            and re.fullmatch(r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?", Path(command).name, re.I) is not None
            and len(args) == 10 and isinstance(args[2], str) and Path(args[2]).is_absolute()
            and args == _hook_args(args[2], name))


class ClaudeHookSettings:
    """``hooks.UserPromptSubmit`` in a Claude Code settings.json, merged as JSON."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self, text):
        if text is None or not text.strip():
            return {}
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise CliError(f"{self.path} 不是有效 JSON（{exc}），未作修改；请先修复或移走该文件",
                           EXIT_CONFIG) from None
        if not isinstance(document, dict):
            raise CliError(f"{self.path} 顶层不是对象，未作修改", EXIT_CONFIG)
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict):
            raise CliError(f"{self.path} 的 hooks 不是对象，未作修改", EXIT_CONFIG)
        groups = hooks.get(HOOK_EVENT, [])
        if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
            raise CliError(f"{self.path} 的 hooks.{HOOK_EVENT} 不是列表，未作修改", EXIT_CONFIG)
        return document

    @staticmethod
    def _strip(groups, matches, keep=None):
        """Drop this connection's hooks except ``keep`` (kept in place); prune groups this emptied."""
        result = []
        for group in groups:
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                result.append(group)
                continue
            remaining = [hook for hook in hooks if hook is keep or not matches(hook)]
            if len(remaining) == len(hooks):
                result.append(group)
            elif remaining:
                result.append({**group, "hooks": remaining})
        return result

    @staticmethod
    def first(groups, matches):
        return next((hook for group in groups if isinstance(group.get("hooks"), list)
                     for hook in group["hooks"] if matches(hook)), None)

    def render(self, text, matches, entry, inherited=None):
        document = self.load(text)
        hooks = document.setdefault("hooks", {})
        groups = hooks.setdefault(HOOK_EVENT, [])
        current = self.first(groups, matches)
        merged = {**(inherited or {}), **(current or {}), **entry}
        if current is not None:
            # Rewrite only the fields we own; keep any the user added (e.g. ``once``).
            current.clear()
            current.update(merged)
            hooks[HOOK_EVENT] = self._strip(groups, matches, keep=current)
        else:
            groups.append({"hooks": [merged]})
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    def remove(self, text, matches):
        document = self.load(text)
        hooks = document.get("hooks") or {}
        groups = hooks.get(HOOK_EVENT) or []
        stripped = self._strip(groups, matches)
        if stripped == groups:
            return None
        if stripped:
            hooks[HOOK_EVENT] = stripped
        else:
            del hooks[HOOK_EVENT]
            if not hooks:
                del document["hooks"]
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _edit_hook(scope, project_dir, name, *, entry, known_commands, dry_run):
    path = hook_settings_path(scope, project_dir)
    settings = ClaudeHookSettings(path)

    def matches(hook):
        return _hook_is_ours(hook, name, known_commands)

    text = read_text(path)
    changes = []
    inherited = None
    warning = None
    backup_dir = None
    if scope == "project":
        # Backups may contain settings that are private to this machine. Keep
        # them outside the project, including the backup of any legacy entry.
        backup_dir = home() / ".claude" / "zhiji-config-backups"
        # Older CLI versions wrote the hook into the shared settings.json.
        old = ClaudeHookSettings(path.with_name("settings.json"))
        try:
            old_text = read_text(old.path)
            old_document = old.load(old_text)
            inherited = old.first(old_document.get("hooks", {}).get(HOOK_EVENT, []), matches)
            cleaned = old.remove(old_text, matches) if inherited is not None else None
        except CliError:
            warning = f"无法检查旧钩子配置 {old.path}，已保留该文件。"
        else:
            if cleaned is not None:
                changes.append((old.path, cleaned))
    if entry is not None:
        rendered = settings.render(text, matches, entry, inherited=inherited)
        if text is not None and settings.load(text) == json.loads(rendered):
            rendered = None
    else:
        rendered = settings.remove(text, matches)
    if rendered is not None:
        changes.insert(0, (path, rendered))
    edited = []
    for changed_path, content in changes:
        copy = None
        if not dry_run:
            copy = backup(changed_path, directory=backup_dir)
            write_text(changed_path, content)
        edited.append({"path": changed_path, "backup": copy})
    return {"path": path, "changed": bool(edited), "edited": edited, "warning": warning,
            "backup": next((item["backup"] for item in edited if item["path"] == path), None)}


def install_hook(inst, scope, project_dir, name, *, dry_run, previous=None):
    """Merge the optional hook and migrate entries with verified ownership."""
    check_hook_source(inst)
    command, args = inst.hook_command(name)
    entry = {"type": "command", "command": command, "args": args, "timeout": HOOK_TIMEOUT,
             "statusMessage": HOOK_STATUS}
    known = [(command, args)] + ([previous] if previous is not None else [])
    return {**_edit_hook(scope, project_dir, name, entry=entry, known_commands=known, dry_run=dry_run),
            "command": subprocess.list2cmdline([command, *args])}


def remove_hook(scope, project_dir, name, *, dry_run, inst=None, previous=None):
    known = [inst.hook_command(name)] if inst is not None else []
    if previous is not None:
        known.append(previous)
    return _edit_hook(scope, project_dir, name, entry=None, known_commands=known, dry_run=dry_run)


# -- optional Agent Skill (Claude Code) --------------------------------------------------------

SKILL_NAME = "zhiji"
SKILL_CLIENTS = ("claude-code",)


def skill_source(inst):
    return inst.root / "plugins" / "kyf.mcp" / "skill" / SKILL_NAME


def skill_target(scope, project_dir):
    base = (Path(project_dir or os.getcwd()) / ".claude" if scope == "project"
            else claude_config_override() or home() / ".claude")
    return base / "skills" / SKILL_NAME


def _tree(directory):
    """``{relative posix path: bytes}`` of every file below ``directory``."""
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def check_skill_source(inst):
    source = skill_source(inst)
    if not (source / "SKILL.md").is_file():
        raise CliError(f"安装中缺少知己 Skill（{source / 'SKILL.md'}），安装可能不完整；可去掉 --install-skill 继续")
    return source


def install_skill(inst, scope, project_dir, *, dry_run):
    """Copy the shipped Skill folder; ``{"path", "changed", "backup"}``. Idempotent."""
    source = check_skill_source(inst)
    target = skill_target(scope, project_dir)
    try:
        if target.is_dir() and not target.is_symlink() and _tree(target) == _tree(source):
            return {"path": target, "changed": False, "backup": None}
        if dry_run:
            return {"path": target, "changed": True, "backup": None}
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{SKILL_NAME}.{os.getpid()}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            shutil.copytree(source, staging)
            copy = None
            if target.exists() or target.is_symlink():
                # The backup lives beside skills/, not in it: a second folder with the same
                # skill name inside skills/ would be loaded as a duplicate Skill.
                stamp = time.strftime("%Y%m%d-%H%M%S")
                copy = target.parent.parent / f"{SKILL_NAME}-skill-backup-{stamp}"
                counter = 1
                while copy.exists():
                    copy = target.parent.parent / f"{SKILL_NAME}-skill-backup-{stamp}-{counter}"
                    counter += 1
                os.replace(target, copy)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except OSError as exc:
        raise CliError(f"Skill 写入失败（{target}）：{exc}", EXIT_CONFIG) from None
    return {"path": target, "changed": True, "backup": copy}


def generic_snippet(key, command, args):
    return json.dumps({"mcpServers": {key: {"command": command, "args": args}}}, ensure_ascii=False, indent=2)


# -- pairing -------------------------------------------------------------------------------

def core_profile_name(client, name):
    label = CLIENT_LABELS[client]
    return label if name == client else f"{label} · {name}"[:64]


def pair(url, inst, client, name, wait, replaces=None):
    """Request (or resume) a pairing and wait for the user's decision; returns the delivery."""
    store = credentials(inst)
    pending = store.load_pending(name)
    ticket = None
    if pending and pending.get("core") == url:
        status, payload = http_post(url, "/api/mcp/pairing/poll",
                                    {"pairingId": pending["pairingId"], "pollSecret": pending["pollSecret"]})
        if status == 200 and payload.get("state") in ("pending", "approved"):
            ticket = {"pairingId": pending["pairingId"], "pollSecret": pending["pollSecret"]}
            say("继续等待上次发起的配对请求…")
            if payload.get("state") == "approved" and payload.get("credential"):
                store.clear_pending(name)
                return payload
    if ticket is None:
        store.clear_pending(name)
        body = {"name": core_profile_name(client, name), "client": client}
        if replaces:
            body["replacesClientId"] = replaces
        status, payload = http_post(url, "/api/mcp/pairing/request", body)
        if status == 404 or status == 405:
            raise CliError("当前知己版本不支持命令行配对；请升级，或在 设置 → 插件 → MCP 手动配对")
        if status != 200:
            raise CliError(f"配对请求被拒绝：{payload.get('message') or payload.get('error') or status}",
                           EXIT_NOT_PAIRED)
        ticket = {"pairingId": payload["pairingId"], "pollSecret": payload["pollSecret"]}
        store.save_pending(name, ticket["pairingId"], ticket["pollSecret"], url)
        if payload.get("replaces"):
            say("知己里已有同名客户端；确认后会就地修复它（旧凭据失效），不会新建重复档案。")
    if wait <= 0:
        raise CliError("配对请求已发出。请在知己弹出的窗口（或 设置 → 插件 → MCP）中确认，然后重新运行同一条命令继续。",
                       EXIT_WAITING)
    deadline = time.monotonic() + wait
    announced = None
    try:
        while True:
            status, payload = http_post(url, "/api/mcp/pairing/poll", ticket)
            if status == 404:
                store.clear_pending(name)
                raise CliError("配对请求已失效（知己可能已重启），请重新运行命令。", EXIT_NOT_PAIRED)
            if status != 200:
                raise CliError(f"配对查询失败：{payload.get('message') or status}", EXIT_NOT_PAIRED)
            state, ui = payload.get("state"), payload.get("ui")
            if state == "approved" and payload.get("credential"):
                store.clear_pending(name)
                return payload
            if state in ("denied", "expired", "cancelled", "delivered", "failed"):
                store.clear_pending(name)
                reason = {"denied": "你在知己中拒绝了这次配对", "expired": "配对请求已过期",
                          "cancelled": "配对请求已被取消或被更新的请求替代",
                          "delivered": "凭据已被领取过（只交付一次），请重新运行命令",
                          "failed": "知己创建客户端档案失败"}[state]
                raise CliError(f"授权：未完成 —— {reason}。", EXIT_NOT_PAIRED)
            if ui != announced:
                announced = ui
                if ui == "unavailable":
                    say("当前无法弹出配对窗口（没有可交互桌面或界面组件不可用）。")
                    say(f"请打开 知己 → 设置 → 插件 → MCP，在「待确认的配对请求」中批准「{core_profile_name(client, name)}」；"
                        f"本命令会继续等待最多 {int(max(0, deadline - time.monotonic()))} 秒。")
                elif ui == "shown":
                    say("知己的「连接 AI 客户端」窗口已打开：请选择可读取的账号（额外操作能力默认关闭），然后点「允许连接」。")
                else:
                    say("已向知己发起配对请求，正在打开确认窗口…")
            if time.monotonic() >= deadline:
                raise CliError("等待确认超时。确认后重新运行同一条命令即可继续（请求在知己里仍保留至过期）。",
                               EXIT_WAITING)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        try:
            http_post(url, "/api/mcp/pairing/cancel", ticket, timeout=5)
        except CliError:
            pass
        store.clear_pending(name)
        raise CliError("已取消配对。", EXIT_NOT_PAIRED) from None


# -- MCP verification ------------------------------------------------------------------------

class McpProbe:
    """Speak newline-delimited JSON-RPC to the configured command over stdio."""

    def __init__(self, command, args, timeout):
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=creation)
        self.deadline = time.monotonic() + timeout
        self.lines = queue.Queue()
        self.stderr = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        self.next_id = 0

    def _pump(self):
        for raw in iter(self.proc.stdout.readline, b""):
            self.lines.put(raw)
        self.lines.put(None)

    def _drain(self):
        for raw in iter(self.proc.stderr.readline, b""):
            if len(self.stderr) < 40:
                self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CliError(f"{method} 超时未应答")
            try:
                raw = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise CliError(f"{method} 超时未应答") from None
            if raw is None:
                tail = "；".join(self.stderr[-3:])
                raise CliError(f"MCP 服务器在 {method} 前退出" + (f"（{tail}）" if tail else ""))
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise CliError("MCP 服务器在 stdout 输出了非协议内容") from None
            if message.get("id") != ident:
                continue  # notifications such as tools/list_changed
            if "error" in message:
                raise CliError(f"{method} 失败：{message['error'].get('message')}")
            return message.get("result") or {}

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


def _tool_payload(result):
    if result.get("isError"):
        content = result.get("content") or [{}]
        raise CliError(str(content[0].get("text", "工具返回错误")).splitlines()[0])
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    try:
        return json.loads((result.get("content") or [{}])[0].get("text", "{}"))
    except ValueError:
        return {}


def verify(command, args, timeout=VERIFY_TIMEOUT):
    """Real round trip: initialize, tools/list, zhiji_ping, zhiji_list_accounts."""
    report = {"ok": False, "tools": 0, "ping": None, "accounts": None, "error": None}
    try:
        probe = McpProbe(command, args, timeout)
    except OSError as exc:
        report["error"] = f"无法启动 {command}：{exc}"
        return report
    try:
        initialized = probe.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                   "clientInfo": {"name": "zhiji-mcp-cli", "version": "1"}})
        report["server"] = (initialized.get("serverInfo") or {}).get("name")
        probe.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = probe.request("tools/list").get("tools") or []
        names = {tool.get("name") for tool in tools}
        report["tools"] = len(tools)
        for required in ("zhiji_ping", "zhiji_list_accounts"):
            if required not in names:
                raise CliError(f"工具目录缺少 {required}")
        report["ping"] = _tool_payload(probe.request("tools/call", {"name": "zhiji_ping", "arguments": {}}))
        report["accounts"] = _tool_payload(probe.request("tools/call", {"name": "zhiji_list_accounts",
                                                                        "arguments": {}}))
        report["ok"] = True
    except CliError as exc:
        report["error"] = exc.message
    except OSError as exc:
        report["error"] = f"与 MCP 服务器通信失败：{exc}"
    finally:
        probe.close()
    return report


def _account_count(accounts):
    items = accounts.get("accounts") if isinstance(accounts, dict) else None
    return len(items) if isinstance(items, list) else None


def describe_scope(delivery, report):
    """One line for 授权范围, from the pairing delivery and/or the live ping."""
    profile = (report.get("ping") or {}).get("profile") or {}
    caps = (delivery or {}).get("capabilities") or profile.get("capabilities") or {}
    mode = (delivery or {}).get("mode") or profile.get("mode")
    opened = [label for key, label in CAPABILITY_LABELS.items() if caps.get(key)]
    if delivery and delivery.get("accounts") is not None:
        accounts = "、".join(item["label"] for item in delivery["accounts"]) or "未选择账号"
    else:
        count = _account_count(report.get("accounts"))
        accounts = f"{count} 个账号" if count is not None else "未知"
    mode_label = {"accounts": "按账号", "per_contact": "逐联系人", "full": "全部账号（旧）"}.get(mode, mode or "未知")
    return f"{mode_label}读取：{accounts}；额外能力：{'、'.join(opened) if opened else '无（只读）'}"


# -- commands ----------------------------------------------------------------------------------

def _installation_or_exit(args):
    inst = find_installation(args)
    if inst is None:
        raise CliError(not_installed_message(), EXIT_NOT_INSTALLED)
    return inst


def _print_install(inst):
    version = inst.version
    say(f"安装：就绪 —— {inst.root}（{inst.source}{'，版本 ' + version if version else ''}）")
    if inst.dev:
        say("  注意：--dev 模式下客户端配置指向源码目录与当前 Python，升级或移动后会失效。")


def cmd_connect(args):
    client, name = args.client, args.name or args.client
    scope = args.scope
    if scope == "project" and client not in ("claude-code", "cursor"):
        raise CliError(f"{CLIENT_LABELS[client]} 不支持 --scope project")
    if args.install_skill and client not in SKILL_CLIENTS:
        raise CliError(f"--install-skill 目前只支持 Claude Code；{CLIENT_LABELS[client]} 请按其文档手动放置 Skill")
    if args.install_hook and client not in HOOK_CLIENTS:
        raise CliError("--install-hook 目前只提供 Claude Code 的安装适配；"
                       "可另开终端运行 zhiji-events.py --follow --format text")
    inst = _installation_or_exit(args)
    store = credentials(inst)
    store.check_name(name)
    if args.install_skill:
        check_skill_source(inst)  # fail before pairing or writing anything
    previous_hook = _saved_hook_command(store, name) if args.install_hook else None
    hook_plan = (install_hook(inst, scope, args.project_dir, name, dry_run=True, previous=previous_hook)
                 if args.install_hook else None)
    _print_install(inst)
    key = server_key(client, name)
    command, command_args = inst.command(name)
    if args.dry_run:
        result = apply_config(client, scope, args.project_dir, key, command, command_args, dry_run=True)
        say("（演练）不发起配对、不写文件、不启动服务。")
        say(f"（演练）凭据位置：{store.profile_path(name)}（DPAPI 加密）")
        if result["path"] is None:
            say(generic_snippet(key, command, command_args))
        else:
            say(f"（演练）{result['label']}：{result['path']} —— {'将合并知己条目（先备份）' if result['changed'] else '已是最新，无需修改'}")
        say(f"（演练）启动命令：{command} {' '.join(command_args)}")
        if args.install_skill:
            skill = install_skill(inst, scope, args.project_dir, dry_run=True)
            say(f"（演练）Skill：{skill['path']} —— {'将复制（已有不同内容时先移到备份）' if skill['changed'] else '已是最新，无需修改'}")
        if hook_plan is not None:
            say(f"（演练）新消息钩子：{hook_plan['path']} —— "
                f"{'将更新配置（先备份）' if hook_plan['changed'] else '已是最新，无需修改'}")
            for item in hook_plan["edited"]:
                if item["path"] != hook_plan["path"]:
                    say(f"（演练）将从 {item['path']} 移除旧版知己钩子。")
            if hook_plan["warning"]:
                say(hook_plan["warning"])
            say(f"（演练）钩子命令：{hook_plan['command']}")
        return EXIT_OK

    url = core_url(inst)
    delivery = None
    existing = None
    replaces = None
    try:
        existing = store.load_profile(name)
    except store.CredentialError as exc:
        if exc.code != "profile-not-found":
            say(f"本机已保存的凭据不可用（{exc.message}），将重新配对。")
    if existing:
        state = handshake(url, existing["clientId"], existing["credential"])
        if state == "ok":
            say("授权：沿用已有配对（凭据校验通过，不新建档案）。")
        elif state == "invalid":
            say("已保存的凭据已失效（被撤销或重新生成），将重新配对并修复同一档案。")
            replaces = existing["clientId"]
            existing = None
        elif state in ("mcp-disabled", "mcp-plugin-disabled"):
            raise CliError("知己的 MCP 已关闭或 MCP 插件已停用；请在 设置 → 插件 → MCP 打开后重试。", EXIT_NOT_PAIRED)
        else:
            raise CliError(f"凭据校验失败：{state}")
    if not existing:
        delivery = pair(url, inst, client, name, args.wait, replaces=replaces)
        store.save_profile(name, delivery["clientId"], delivery["credential"], root=inst.root, client=client)
        delivery.pop("credential", None)
        say(f"授权：已确认 —— {'已修复原有档案' if delivery.get('repaired') else '已创建客户端档案'}，"
            f"凭据已加密保存到 {store.profile_path(name)}")

    result = apply_config(client, scope, args.project_dir, key, command, command_args, dry_run=False)
    if result["path"] is None:
        say("接入：未完成客户端配置 —— 通用客户端请把下面的片段加入其 MCP 配置：")
        say(generic_snippet(key, command, command_args))
        target = (command, command_args)
    else:
        if result["changed"]:
            say(f"已写入 {result['label']}：{result['path']}（方式：{result['via']}"
                f"{'，原文件已备份为 ' + str(result['backup']) if result['backup'] else ''}）")
        else:
            say(f"{result['label']} 已包含正确的知己条目：{result['path']}")
        target = configured_command(client, scope, args.project_dir, key)
        if target is None:
            raise CliError("接入：失败 —— 写入后读不回知己条目，请检查配置文件。", EXIT_CONFIG)
    if args.install_skill:
        skill = install_skill(inst, scope, args.project_dir, dry_run=False)
        if skill["changed"]:
            say(f"Skill：已安装到 {skill['path']}"
                f"{'（原有内容已移到 ' + str(skill['backup']) + '）' if skill['backup'] else ''}")
        else:
            say(f"Skill：{skill['path']} 已是最新")
    if args.install_hook:
        hook = install_hook(inst, scope, args.project_dir, name, dry_run=False, previous=previous_hook)
        if any(item["path"] == hook["path"] for item in hook["edited"]):
            say(f"新消息钩子：已写入 {hook['path']}（UserPromptSubmit"
                f"{'，原文件已备份为 ' + str(hook['backup']) if hook['backup'] else ''}）。")
            say("Claude Code 2.1.139 或更新版本会在你下次提问时显示新消息提示（有新消息时）。")
        else:
            say(f"新消息钩子：{hook['path']} 已是最新")
        for item in hook["edited"]:
            if item["path"] != hook["path"]:
                say(f"已从 {item['path']} 移除旧版知己钩子（原文件备份在 {item['backup']}）。")
        if hook["warning"]:
            say(hook["warning"])
        if scope == "project":
            say("项目 .mcp.json 与 .claude/settings.local.json 含本机路径，请勿提交这次写入的知己配置。")

    report = verify(*target)
    if not report["ok"]:
        say(f"接入：验证失败 —— {report['error']}")
        say(f"  已配置的命令：{target[0]} {' '.join(target[1])}")
        say("  可运行 doctor 查看详情；凭据和配置已保留，修复后重新运行 connect 即可。")
        return EXIT_VERIFY
    count = _account_count(report["accounts"])
    if result["path"] is not None:
        say(f"接入：成功 —— 已实际启动配置的命令并完成 initialize、tools/list（{report['tools']} 个工具）、"
            f"zhiji_ping 与 zhiji_list_accounts（{count if count is not None else '?'} 个账号）")
    else:
        say(f"连通：已验证（{report['tools']} 个工具），但客户端配置需手动完成。")
    say(f"授权范围：{describe_scope(delivery, report)}")
    ready = ((delivery or {}).get("models") or {}).get("ready")
    say("专项模型：" + {True: "已配置（好友分析、自我分析等可用）",
                       False: "未配置 —— 基础读取可用；需要好友分析等专项能力时请在知己 设置 → 模型 中配置",
                       None: "未知（沿用已有配对时不检查；可在知己 设置 → 模型 查看）"}[ready])
    say("重启：" + RESTART_NOTES[client])
    return EXIT_OK


def _client_configs(names):
    """Every (client, scope, name) whose config file references one of our profiles."""
    found = []
    for client in CLIENTS:
        for scope in ("user", "project") if client in ("claude-code", "cursor") else ("user",):
            for name in names:
                try:
                    target = configured_command(client, scope, None, server_key(client, name))
                except CliError:
                    target = None
                if target is not None:
                    found.append((client, scope, name, target))
    return found


def cmd_status(args, *, deep=False):
    inst = find_installation(args)
    if inst is None:
        say(not_installed_message())
        return EXIT_NOT_INSTALLED
    _print_install(inst)
    store = credentials(inst)
    url = core_url(inst, start=deep)
    say(f"知己后台服务：{'运行中 ' + url if url else '未运行（客户端调用时会自动按需启动）'}")
    profiles = store.list_profiles()
    if not profiles:
        say("本机连接：无（运行 connect --client <客户端> 完成配对）")
    problems = 0
    for info in profiles:
        line = f"本机连接 {info['name']}：客户端标识 {info.get('clientId') or '?'}"
        if info.get("corrupt"):
            say(line + " —— 凭据文件损坏，请重新 connect")
            problems += 1
            continue
        if url:
            try:
                saved = store.load_profile(info["name"])
                state = handshake(url, saved["clientId"], saved["credential"])
            except store.CredentialError as exc:
                state = exc.message
            line += " —— " + {"ok": "凭据有效", "invalid": "凭据已失效，请重新 connect"}.get(state, state)
            problems += state != "ok"
        say(line)
    configs = _client_configs([info["name"] for info in profiles if not info.get("corrupt")])
    for client, scope, name, target in configs:
        say(f"客户端配置：{CLIENT_LABELS[client]}（{scope}）→ 连接 {name}")
        if not Path(target[0]).is_file() or (len(target[1]) > 0 and not Path(target[1][0]).is_file()):
            say("  ⚠ 配置中的命令路径不存在（安装已移动或升级？），请重新运行 connect 修复")
            problems += 1
        if deep:
            report = verify(*target)
            if report["ok"]:
                say(f"  验证通过：{report['tools']} 个工具，{describe_scope(None, report)}")
            else:
                say(f"  验证失败：{report['error']}")
                problems += 1
    return EXIT_OK if not problems else (EXIT_VERIFY if deep else EXIT_ERROR)


def cmd_disconnect(args):
    client, name = args.client, args.name or args.client
    inst = find_installation(args)
    store = credentials(inst)
    store.check_name(name)
    result = remove_config(client, args.scope, args.project_dir, server_key(client, name), dry_run=args.dry_run)
    if result["path"] is None:
        say("通用客户端：请自行从其配置中删除知己条目。")
    elif result["changed"]:
        say(f"{'（演练）将' if args.dry_run else '已'}从 {result['label']} 移除知己条目：{result['path']}"
            f"{'（原文件已备份为 ' + str(result['backup']) + '）' if result['backup'] else ''}")
    else:
        say(f"{result['label']} 中没有知己条目。")
    if client in HOOK_CLIENTS:
        hook = remove_hook(args.scope, args.project_dir, name, dry_run=args.dry_run, inst=inst,
                           previous=_saved_hook_command(store, name))
        for item in hook["edited"]:
            say(f"{'（演练）将' if args.dry_run else '已'}从 {item['path']} 移除知己新消息钩子"
                f"{'（原文件已备份为 ' + str(item['backup']) + '）' if item['backup'] else ''}")
        if hook["warning"]:
            say(hook["warning"])
    if args.keep_profile or args.dry_run:
        return EXIT_OK
    try:
        saved = store.load_profile(name)
    except store.CredentialError:
        saved = None
    if saved and inst is not None:
        try:
            url = core_url(inst)
            status, payload = http_post(url, "/api/mcp/pairing/revoke",
                                        {"clientId": saved["clientId"], "credential": saved["credential"]})
            say("已在知己中撤销该客户端档案。" if status == 200 else
                f"未能在知己中撤销档案（{payload.get('message') or status}），可在 设置 → 插件 → MCP 手动删除。")
        except CliError as exc:
            say(f"未能连接知己撤销档案（{exc.message}），可稍后在 设置 → 插件 → MCP 手动删除。")
    if store.delete_profile(name):
        say(f"已删除本机凭据：{store.profile_path(name)}")
    store.clear_pending(name)
    return EXIT_OK


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="知己安装目录（含 resources\\client 的目录或 client 目录本身）")
    common.add_argument("--dev", action="store_true", help="使用源码目录与当前 Python（仅开发）")
    parser = argparse.ArgumentParser(prog="zhiji-mcp-cli", description="把本机 AI 客户端接入知己微信助手（MCP）")
    commands = parser.add_subparsers(dest="command", required=True)
    connect = commands.add_parser("connect", parents=[common], help="配对、写入客户端配置并验证")
    disconnect = commands.add_parser("disconnect", parents=[common], help="移除客户端配置与本机凭据")
    for sub in (connect, disconnect):
        sub.add_argument("--client", required=True, choices=CLIENTS)
        sub.add_argument("--scope", choices=("user", "project"), default="user")
        sub.add_argument("--name", help="连接名（默认等于客户端类型）")
        sub.add_argument("--project-dir", help="项目级配置所在目录（默认当前目录）")
        sub.add_argument("--dry-run", action="store_true", help="只显示将要做的修改")
    connect.add_argument("--wait", type=int, default=600, help="等待用户确认配对的秒数（0 表示不等待）")
    connect.add_argument("--install-skill", action="store_true",
                         help="同时安装可选的知己 Agent Skill（仅 Claude Code；按 --scope 放到用户或项目 .claude/skills）")
    connect.add_argument("--install-hook", action="store_true",
                         help="安装新消息提醒钩子（Claude Code 2.1.139+；项目级使用 .claude/settings.local.json）")
    disconnect.add_argument("--keep-profile", action="store_true", help="保留知己档案与本机凭据，只删客户端配置")
    commands.add_parser("status", parents=[common], help="查看安装、服务、连接与客户端配置（不启动服务）")
    commands.add_parser("doctor", parents=[common], help="status 加上对每个已配置客户端的真实 MCP 验证")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.command == "connect":
            return cmd_connect(args)
        if args.command == "disconnect":
            return cmd_disconnect(args)
        return cmd_status(args, deep=args.command == "doctor")
    except CliError as exc:
        say(exc.message)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
