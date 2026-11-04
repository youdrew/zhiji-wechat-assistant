"""Protected local storage of MCP client credentials (G12, agent-integration §4 item 5).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

One file per named profile: ``%LOCALAPPDATA%\\Zhiji\\mcp\\<name>.json``. The client id,
installation root and client kind are plain metadata; the credential itself is
encrypted with Windows DPAPI (``CryptProtectData``, current-user scope, fixed
application entropy), so it never appears in argv, client configs, logs or chat. A
pending pairing's one-time poll secret is kept the same way (``<name>.pending.json``)
so an interrupted ``connect`` resumes instead of starting a second request.

Standard library only; shared by ``zhiji-mcp-cli.py`` and the stdio adapter.

Adapter API (for ``mcp-stdio-adapter.py --profile <name>``)::

    from zhiji_mcp_credentials import CredentialError, load_adapter_credentials
    client_id, credential = load_adapter_credentials(name)   # raises CredentialError(code, message)

``ZHIJI_MCP_HOME`` overrides the directory (tests, portable setups).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

SCHEMA = 1
ENTROPY = b"zhiji-mcp-credential-v1"
# No dots: the name becomes a TOML table key, a YAML id and a file name.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HOME_ENV = "ZHIJI_MCP_HOME"


class CredentialError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# -- DPAPI ---------------------------------------------------------------------

def dpapi_available():
    return sys.platform == "win32"


def _blob_type():
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    return DataBlob


def _call_dpapi(protect, data):
    if not dpapi_available():
        raise CredentialError("dpapi-unavailable", "本机凭据保护只支持 Windows")
    import ctypes
    from ctypes import wintypes
    DataBlob = _blob_type()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32

    def blob(value):
        buffer = ctypes.create_string_buffer(value, len(value))
        return DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    source, keep_source = blob(data)
    entropy, keep_entropy = blob(ENTROPY)
    output = DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR if protect else ctypes.c_void_p,
                         ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    ok = function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(output))
    del keep_source, keep_entropy
    if not ok:
        raise CredentialError("credential-unreadable" if not protect else "credential-protect-failed",
                              "凭据无法解密（可能来自其他 Windows 用户）" if not protect else "凭据加密失败")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def protect(data: bytes) -> bytes:
    return _call_dpapi(True, data)


def unprotect(data: bytes) -> bytes:
    return _call_dpapi(False, data)


# -- locations -------------------------------------------------------------------

def credentials_dir() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Zhiji" / "mcp"


def check_name(name):
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise CredentialError("invalid-profile-name", "连接名只能用小写字母、数字、下划线和连字符（1-64 字）")
    return name


def profile_path(name) -> Path:
    return credentials_dir() / (check_name(name) + ".json")


def pending_path(name) -> Path:
    return credentials_dir() / (check_name(name) + ".pending.json")


def _write_atomic(path: Path, document: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise CredentialError("credential-corrupt", f"凭据文件已损坏：{path}") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise CredentialError("credential-corrupt", f"凭据文件格式不正确：{path}")
    return document


def _seal(secret: str) -> str:
    return base64.b64encode(protect(secret.encode("utf-8"))).decode("ascii")


def _open(sealed) -> str:
    if not isinstance(sealed, str):
        raise CredentialError("credential-corrupt", "凭据文件缺少加密凭据")
    try:
        raw = base64.b64decode(sealed, validate=True)
    except ValueError:
        raise CredentialError("credential-corrupt", "凭据文件缺少加密凭据") from None
    return unprotect(raw).decode("utf-8")


# -- profiles ----------------------------------------------------------------------

def save_profile(name, client_id, credential, *, root=None, client=None):
    if not isinstance(client_id, str) or not client_id or not isinstance(credential, str) or not credential:
        raise CredentialError("invalid-credential", "凭据不完整")
    document = {"schema": SCHEMA, "name": check_name(name), "clientId": client_id,
                "root": None if root is None else str(root), "client": client,
                "savedAtMs": int(time.time() * 1000), "credential": _seal(credential)}
    _write_atomic(profile_path(name), document)
    return profile_path(name)


def profile_info(name):
    """Metadata only (never the credential); ``None`` when no profile is saved."""
    document = _read(profile_path(name))
    if document is None:
        return None
    return {key: document.get(key) for key in ("name", "clientId", "root", "client", "savedAtMs")}


def load_profile(name):
    """``{"clientId", "credential", "root", "client"}``; raises CredentialError when absent or unreadable."""
    document = _read(profile_path(name))
    if document is None:
        raise CredentialError("profile-not-found", f"没有名为 {name} 的知己连接配置，请先运行 connect")
    client_id = document.get("clientId")
    if not isinstance(client_id, str) or not client_id:
        raise CredentialError("credential-corrupt", "凭据文件缺少客户端标识")
    return {"clientId": client_id, "credential": _open(document.get("credential")),
            "root": document.get("root"), "client": document.get("client")}


def load_adapter_credentials(name):
    """``(client_id, credential)`` for ``mcp-stdio-adapter.py --profile NAME``."""
    profile = load_profile(name)
    return profile["clientId"], profile["credential"]


def delete_profile(name):
    path = profile_path(name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def list_profiles():
    directory = credentials_dir()
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".pending.json") or not NAME_PATTERN.fullmatch(path.stem):
            continue
        try:
            info = profile_info(path.stem)
        except CredentialError:
            info = {"name": path.stem, "corrupt": True}
        if info is not None:
            result.append(info)
    return result


# -- pending pairing (resume an interrupted connect) -----------------------------------

def save_pending(name, pairing_id, poll_secret, core_url):
    document = {"schema": SCHEMA, "name": check_name(name), "pairingId": pairing_id, "core": core_url,
                "savedAtMs": int(time.time() * 1000), "pollSecret": _seal(poll_secret)}
    _write_atomic(pending_path(name), document)


def load_pending(name):
    try:
        document = _read(pending_path(name))
    except CredentialError:
        clear_pending(name)
        return None
    if document is None:
        return None
    try:
        return {"pairingId": document["pairingId"], "core": document.get("core"),
                "pollSecret": _open(document.get("pollSecret"))}
    except (CredentialError, KeyError):
        clear_pending(name)
        return None


def clear_pending(name):
    try:
        pending_path(name).unlink()
    except FileNotFoundError:
        pass
