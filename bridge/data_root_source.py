"""Persist a user-configured WeChat data root directory across desktop updates."""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


_MISSING = object()

CUSTOM_ROOT_ENV = "WECHATVIBE_DATA_ROOT"


class DataRootError(ValueError):
    pass


class DataRootSource:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        runtime = self.root / ".local" / "real-client-runtime"
        self.config = runtime / "wechat-data-root.json"

    def _check_config(self, path: Path) -> None:
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            raise DataRootError("数据目录配置不可用")
        cursor = path
        while cursor != self.root:
            try:
                stat = cursor.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DataRootError("数据目录配置不可用") from exc
            else:
                if cursor.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    raise DataRootError("数据目录配置不可用")
            cursor = cursor.parent

    def _read_config(self):
        self._check_config(self.config)
        try:
            return json.loads(self.config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _MISSING

    def _stored_path(self):
        setting = self._read_config()
        if setting is _MISSING:
            return None
        if not isinstance(setting, dict) or setting.get("schema") != 1:
            return None
        path = setting.get("path")
        return path if isinstance(path, str) and path else None

    def _write_config(self, value: str | None) -> None:
        self._check_config(self.config)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self._check_config(self.config)
        temporary = self.config.with_name(self.config.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"schema": 1, "path": value}, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config)
        finally:
            temporary.unlink(missing_ok=True)

    def _count_accounts(self, path: str) -> int:
        project_root = Path(__file__).resolve().parents[1]
        native_reader = str(project_root / "native-reader")
        if native_reader not in sys.path:
            sys.path.insert(0, native_reader)
        from wr import discovery
        boundary = os.path.normcase(os.path.realpath(path))
        count = 0
        for account in discovery.discover_account_dirs():
            normalized = os.path.normcase(os.path.realpath(account.path))
            if normalized == boundary or normalized.startswith(boundary + os.sep):
                count += 1
        return count

    def status(self) -> dict:
        try:
            stored = self._stored_path()
        except (OSError, ValueError, DataRootError):
            stored = None
        if not stored:
            return {"state": "unset", "path": "", "exists": False, "accounts": 0}
        exists = Path(stored).is_dir()
        accounts = self._count_accounts(stored) if exists else 0
        return {"state": "ready" if exists and accounts > 0 else "missing",
                "path": stored, "exists": exists, "accounts": accounts}

    def select(self, value: str) -> dict:
        if not isinstance(value, str) or not value:
            raise DataRootError("请输入有效的目录路径")
        path = Path(value)
        if not path.is_absolute():
            raise DataRootError("请输入绝对路径")
        if not path.is_dir():
            raise DataRootError("目录不存在，请选择已存在的目录")
        path = path.resolve()
        if path.name.lower() == "db_storage":
            path = path.parent
        self._write_config(str(path))
        os.environ[CUSTOM_ROOT_ENV] = str(path)
        return self.status()

    def clear(self) -> dict:
        self._write_config(None)
        os.environ.pop(CUSTOM_ROOT_ENV, None)
        return self.status()

    def apply(self) -> None:
        """Restore env from persisted config at process start."""
        try:
            stored = self._stored_path()
        except (OSError, ValueError, DataRootError):
            return
        if stored:
            os.environ[CUSTOM_ROOT_ENV] = stored
        else:
            os.environ.pop(CUSTOM_ROOT_ENV, None)
