"""Read a validated, independent snapshot of a plugin's saved settings.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from copy import deepcopy

from .core import CoreError


def read_settings(core, fields):
    """``fields`` maps keys to a scalar type or a tuple of allowed select values.

    Core supplies manifest defaults. Missing or invalid settings are errors, never
    an invitation to silently replace the user's saved values with local defaults.
    Call once when accepting a task/event and keep the returned snapshot for it.
    """
    snapshot = core.get("settings")
    valid = isinstance(snapshot, dict) and isinstance(snapshot.get("values"), dict)
    if not valid or type(snapshot.get("revision")) is not int or snapshot["revision"] < 0:
        raise CoreError(502, "invalid-settings", "插件设置读取失败，请重新读取后重试")
    values = snapshot["values"]
    for key, kind in fields.items():
        value = values.get(key)
        invalid = type(value) is not kind if isinstance(kind, type) else value not in kind
        if invalid:
            raise CoreError(502, "invalid-settings", "插件设置内容无效，请重新保存后重试")
    return {"values": deepcopy(values), "revision": snapshot["revision"]}
