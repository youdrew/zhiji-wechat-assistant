"""Validated API model settings and Windows user-scoped encrypted key storage.

Version 2 holds several models plus per-purpose role assignments (main, judge,
image); the analysis backend decides which source is actually active. Reading a
saved profile must never imply a successful switch. Modified by Know Your
Friends, 2026-10-16 (Apache-2.0): configurable generation deadlines.
Modified 2026-11-01: multi-model storage with roles; a version 1 document is
migrated on first write, with the original kept as ``api-model-source.v1.json``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from backend_contracts import LOCAL_SOURCE_ID, ModelSourceUnavailable


PROTOCOLS = frozenset({"anthropic", "responses", "chat_completions", "gemini", "ollama"})
BUILTIN_PROTOCOL = "systemone"
ROLES = ("main", "judge", "image")
MAX_MODELS = 16
DEFAULT_CONCURRENCY = 4
MAX_BASE_URL = 2048
MAX_MODEL_ID = 256
MAX_API_KEY = 4096
MIN_CONTEXT_TOKENS = 4096
MAX_CONTEXT_TOKENS = 1000000
DEFAULT_MODEL_TIMEOUT_MS = 3_600_000
MIN_MODEL_TIMEOUT_MS = 60_000
MAX_MODEL_TIMEOUT_MS = 10_800_000
_MISSING = object()


def _source_fingerprint(protocol, base_url, model):
    """Keep source identity stable across key changes and context budgets."""
    identity = json.dumps(["api-source-v1", protocol, base_url, model],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _text(value, name, maximum):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid " + name)
    return value


def model_timeout_ms(value=DEFAULT_MODEL_TIMEOUT_MS):
    """The per-request generation deadline shared by all model consumers."""
    if type(value) is not int or not MIN_MODEL_TIMEOUT_MS <= value <= MAX_MODEL_TIMEOUT_MS:
        raise ValueError("invalid timeoutMs")
    return value


def model_concurrency(value=DEFAULT_CONCURRENCY):
    """Per-model request concurrency; the gateway queues per service."""
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError("invalid concurrency")
    return value


def model_name(value=""):
    """A bounded display label; empty falls back to the model id in the UI."""
    if not value:
        return ""
    return _text(value, "name", 256)


def connection_values(request, require_model=False):
    """Validate user-controlled endpoint fields before handing them to an SDK."""
    if not isinstance(request, dict):
        raise ValueError("invalid model source request")
    allowed = {"protocol", "baseUrl", "apiKey", "model", "contextTokens", "timeoutMs"} if require_model else {"protocol", "baseUrl", "apiKey"}
    required = {"protocol", "baseUrl", "model"} if require_model else {"protocol", "baseUrl"}
    if not required <= request.keys() or not request.keys() <= allowed:
        raise ValueError("invalid model source request")
    protocol = request["protocol"]
    if not isinstance(protocol, str) or protocol not in PROTOCOLS:
        raise ValueError("invalid protocol")
    base_url = _text(request["baseUrl"], "baseUrl", MAX_BASE_URL).strip()
    if not base_url or any(char in base_url for char in ("\\", " ", "\t", "\r", "\n")):
        raise ValueError("invalid baseUrl")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port  # Also rejects malformed numeric ports.
    except ValueError as exc:
        raise ValueError("invalid baseUrl") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or port == 0 or
            any(part in (".", "..") for part in parsed.path.split("/"))):
        raise ValueError("invalid baseUrl")
    if parsed.hostname.endswith("."):
        raise ValueError("invalid baseUrl")
    base_url = base_url.rstrip("/")
    supplied_key = request.get("apiKey")
    if supplied_key is not None:
        if not isinstance(supplied_key, str) or len(supplied_key) > MAX_API_KEY or any(
                ord(char) < 32 or ord(char) == 127 for char in supplied_key):
            raise ValueError("invalid apiKey")
        supplied_key = supplied_key or None
    result = {"protocol": protocol, "baseUrl": base_url, "apiKey": supplied_key}
    if require_model:
        result["model"] = _text(request["model"], "model", MAX_MODEL_ID).strip()
        if not result["model"]:
            raise ValueError("invalid model")
        context_tokens = request.get("contextTokens")
        if context_tokens is not None and (type(context_tokens) is not int or
                not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
            raise ValueError("invalid contextTokens")
        result["contextTokens"] = context_tokens
        result["timeoutMs"] = model_timeout_ms(request.get("timeoutMs", DEFAULT_MODEL_TIMEOUT_MS))
    return result


def model_list_values(reply):
    """Keep only bounded, displayable catalog fields from provider responses."""
    if (not isinstance(reply, dict) or type(reply.get("supported")) is not bool or
            not isinstance(reply.get("models"), list)):
        raise ModelSourceUnavailable("model list unavailable")
    models = []
    seen = set()
    for item in reply["models"][:500]:
        if not isinstance(item, dict):
            raise ModelSourceUnavailable("model list unavailable")
        model_id = item.get("id")
        name = item.get("name")
        context_tokens = item.get("contextTokens")
        try:
            _text(model_id, "model", MAX_MODEL_ID)
            if name is not None and (not isinstance(name, str) or len(name) > 256 or any(
                    ord(char) < 32 or ord(char) == 127 for char in name)):
                raise ValueError("invalid model name")
            if context_tokens is not None and (type(context_tokens) is not int or
                    not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
                raise ValueError("invalid contextTokens")
        except ValueError as exc:
            raise ModelSourceUnavailable("model list unavailable") from exc
        if model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id, **({"name": name} if name else {}),
                       **({"contextTokens": context_tokens} if context_tokens else {})})
    return {"models": models if reply["supported"] else [], "supported": reply["supported"]}


def _dpapi_protect(value):
    import win32crypt

    return win32crypt.CryptProtectData(value.encode("utf-8"), "Know Your Friends API key", None,
                                       None, None, 0x1)


def _dpapi_unprotect(value):
    import win32crypt

    return win32crypt.CryptUnprotectData(value, None, None, None, 0x1)[1].decode("utf-8")


def _valid_id(value):
    return (isinstance(value, str) and len(value) == 32 and
            all(char in "0123456789abcdef" for char in value))


def _valid_source_ids(raw):
    """A fingerprint table mapping to distinct 32-hex ids, or an invalid marker."""
    if not isinstance(raw, dict):
        return None
    seen = set()
    for fingerprint, value in raw.items():
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                any(char not in "0123456789abcdef" for char in fingerprint) or
                not _valid_id(value) or value in seen):
            return None
        seen.add(value)
    return dict(raw)


class ModelSourceStore:
    def __init__(self, path, *, root=None, legacy_path=None, protect=None, unprotect=None,
                 initial_concurrency=DEFAULT_CONCURRENCY):
        self.path = Path(path)
        self.catalog_path = self.path.with_name("api-model-catalog.json")
        self.root = Path(root) if root is not None else self.path.parent.parent.parent
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self.protect = protect or _dpapi_protect
        self.unprotect = unprotect or _dpapi_unprotect
        self.initial_concurrency = model_concurrency(initial_concurrency)
        self.lock = threading.RLock()
        # Set when the file on disk was version 1 so the next write archives it.
        self._upgrade_pending = False

    def _check_path(self, path):
        root = self.root.resolve()
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(root):
            raise ModelSourceUnavailable("model settings unavailable")
        cursor = path
        while cursor != self.root and cursor.is_relative_to(self.root):
            try:
                stat = cursor.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ModelSourceUnavailable("model settings unavailable") from exc
            else:
                if cursor.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    raise ModelSourceUnavailable("model settings unavailable")
            cursor = cursor.parent

    def _read_json(self, path):
        self._check_path(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _MISSING
        except (OSError, ValueError, UnicodeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc

    # -- validation of stored entries --------------------------------------------------------------------------------
    @staticmethod
    def _api_entry(entry):
        if not isinstance(entry, dict) or set(entry) - {
                "id", "name", "protocol", "baseUrl", "model", "contextTokens", "timeoutMs",
                "concurrency", "encryptedKey"}:
            raise ModelSourceUnavailable("model settings unavailable")
        try:
            name = model_name(entry.get("name", ""))
            protocol = entry["protocol"]
            if not isinstance(protocol, str) or protocol not in PROTOCOLS:
                raise ValueError("invalid protocol")
            base_url = _text(entry["baseUrl"], "baseUrl", MAX_BASE_URL)
            model_id = _text(entry["model"], "model", MAX_MODEL_ID)
            context_tokens = entry.get("contextTokens")
            if context_tokens is not None and (type(context_tokens) is not int or
                    not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
                raise ValueError("invalid contextTokens")
            timeout_ms = model_timeout_ms(entry.get("timeoutMs", DEFAULT_MODEL_TIMEOUT_MS))
            concurrency = model_concurrency(entry.get("concurrency", DEFAULT_CONCURRENCY))
            encrypted = entry.get("encryptedKey")
            if encrypted is not None and (not isinstance(encrypted, str) or len(encrypted) > 16384):
                raise ValueError("invalid encryptedKey")
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        return {"name": name, "protocol": protocol, "baseUrl": base_url, "model": model_id,
                "contextTokens": context_tokens, "timeoutMs": timeout_ms,
                "concurrency": concurrency, "encryptedKey": encrypted}

    @staticmethod
    def _builtin_entry(entry):
        if not isinstance(entry, dict) or set(entry) - {
                "id", "name", "protocol", "builtin", "model", "concurrency"}:
            raise ModelSourceUnavailable("model settings unavailable")
        try:
            name = model_name(entry.get("name", ""))
            if entry.get("protocol") != BUILTIN_PROTOCOL or not isinstance(entry.get("builtin"), str) \
                    or not entry["builtin"] or len(entry["builtin"]) > 64:
                raise ValueError("invalid builtin model")
            model_id = _text(entry["model"], "model", MAX_MODEL_ID)
            # The bundled runtime serves one request at a time.
            concurrency = model_concurrency(1)
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        return {"name": name, "protocol": BUILTIN_PROTOCOL, "builtin": entry["builtin"],
                "model": model_id, "concurrency": concurrency}

    def _normalize_v2(self, raw):
        if not isinstance(raw, dict) or raw.get("version") != 2:
            raise ModelSourceUnavailable("model settings unavailable")
        models_raw = raw.get("models")
        if not isinstance(models_raw, list) or len(models_raw) > MAX_MODELS:
            raise ModelSourceUnavailable("model settings unavailable")
        models, ids = [], set()
        for item in models_raw:
            entry = self._builtin_entry(item) if isinstance(item, dict) and item.get("protocol") == BUILTIN_PROTOCOL \
                else self._api_entry(item)
            entry_id = item.get("id") if isinstance(item, dict) else None
            if not _valid_id(entry_id) or entry_id in ids:
                raise ModelSourceUnavailable("model settings unavailable")
            ids.add(entry_id)
            models.append({"id": entry_id, **entry})
        roles = raw.get("roles")
        if not isinstance(roles, dict) or set(roles) != set(ROLES):
            raise ModelSourceUnavailable("model settings unavailable")
        for role, target in roles.items():
            if target is not None and (not _valid_id(target) or target not in ids):
                raise ModelSourceUnavailable("model settings unavailable")
        main_entry = next((item for item in models if item["id"] == roles["main"]), None)
        image_entry = next((item for item in models if item["id"] == roles["image"]), None)
        if (roles["main"] is not None and (main_entry is None or "builtin" in main_entry)) or \
                (roles["main"] is None and any("builtin" not in item for item in models)) or \
                (image_entry is not None and "builtin" in image_entry):
            raise ModelSourceUnavailable("model settings unavailable")
        selected_mode = raw.get("selectedMode", "api")
        if selected_mode not in ("local", "api"):
            raise ModelSourceUnavailable("model settings unavailable")
        source_ids = _valid_source_ids(raw.get("sourceIds", {}))
        if source_ids is None:
            raise ModelSourceUnavailable("model settings unavailable")
        return {"version": 2, "selectedMode": selected_mode, "models": models,
                "roles": dict(roles), "sourceIds": source_ids}

    def _normalize_v1(self, raw):
        """Version 1 held one profile; it becomes models[0] with roles.main."""
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ModelSourceUnavailable("model settings unavailable")
        if raw.get("api") is None and raw.get("selectedMode") == "local":
            source_ids = _valid_source_ids(raw.get("sourceIds", {}))
            if source_ids is None:
                raise ModelSourceUnavailable("model settings unavailable")
            return {"version": 2, "selectedMode": "local", "models": [],
                    "roles": {"main": None, "judge": None, "image": None},
                    "sourceIds": source_ids}
        if not isinstance(raw.get("api"), dict):
            raise ModelSourceUnavailable("model settings unavailable")
        api = raw["api"]
        try:
            validated = connection_values({"protocol": api["protocol"], "baseUrl": api["baseUrl"],
                                           "model": api["model"],
                                           "contextTokens": api.get("contextTokens"),
                                           "timeoutMs": api.get("timeoutMs", DEFAULT_MODEL_TIMEOUT_MS)}, require_model=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        encrypted = api.get("encryptedKey")
        if encrypted is not None and (not isinstance(encrypted, str) or len(encrypted) > 16384):
            raise ModelSourceUnavailable("model settings unavailable")
        source_id = raw.get("sourceId")
        if not _valid_id(source_id):
            raise ModelSourceUnavailable("model settings unavailable")
        selected_mode = raw.get("selectedMode", "api")
        if selected_mode not in ("local", "api"):
            raise ModelSourceUnavailable("model settings unavailable")
        source_ids = _valid_source_ids(raw["sourceIds"]) if "sourceIds" in raw else None
        if source_ids is None and "sourceIds" in raw:
            raise ModelSourceUnavailable("model settings unavailable")
        # Version 1 accepted an absent or incomplete table. Its current profile
        # remains authoritative, as the old _source_ids() implementation did.
        source_ids = source_ids or {}
        fingerprint = _source_fingerprint(validated["protocol"], validated["baseUrl"], validated["model"])
        source_ids[fingerprint] = source_id
        if len(set(source_ids.values())) != len(source_ids):
            raise ModelSourceUnavailable("model settings unavailable")
        entry = {"id": source_id, "name": "", "protocol": validated["protocol"],
                 "baseUrl": validated["baseUrl"], "model": validated["model"],
                 "contextTokens": validated["contextTokens"], "timeoutMs": validated["timeoutMs"],
                 "concurrency": self.initial_concurrency, "encryptedKey": encrypted}
        return {"version": 2, "selectedMode": selected_mode, "models": [entry],
                "roles": {"main": source_id, "judge": None, "image": None},
                "sourceIds": source_ids}

    def _read(self):
        raw = self._read_json(self.path)
        from_legacy = False
        if raw is _MISSING and self.legacy_path is not None:
            raw = self._read_json(self.legacy_path)
            from_legacy = True
            # The old shared filename also held local model-directory settings.
            if isinstance(raw, dict) and raw.get("schema") == 1 and "version" not in raw:
                return None
        if raw is _MISSING:
            return None
        self._upgrade_pending = False
        if isinstance(raw, dict) and raw.get("version") == 1:
            normalized = self._normalize_v1(raw)
            # The original file is archived on the first version 2 write.
            self._upgrade_pending = not from_legacy
            return normalized
        return self._normalize_v2(raw)

    def _decrypted_key(self, encrypted):
        try:
            return self.unprotect(base64.b64decode(encrypted, validate=True))
        except Exception as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc

    def _main(self, saved):
        """The version 1 shape callers know: the role-assigned main profile."""
        if not saved or saved["roles"]["main"] is None:
            return None
        entry = next(item for item in saved["models"] if item["id"] == saved["roles"]["main"])
        return {"protocol": entry["protocol"], "baseUrl": entry["baseUrl"], "model": entry["model"],
                "contextTokens": entry["contextTokens"], "timeoutMs": entry["timeoutMs"],
                "encryptedKey": entry["encryptedKey"]}

    def saved_selection(self):
        """Intent for startup restore; never proof that the worker switched."""
        with self.lock:
            saved = self._read()
        if not saved:
            return {"selectedMode": "local", "api": None, "sourceId": LOCAL_SOURCE_ID}
        return {"selectedMode": saved["selectedMode"], "api": self._main(saved),
                "sourceId": saved["roles"]["main"], "sourceIds": saved["sourceIds"]}

    def models(self):
        """Public model entries without key material, plus the role assignment."""
        with self.lock:
            saved = self._read()
        if not saved:
            return {"models": [], "roles": {"main": None, "judge": None, "image": None}}
        entries = []
        for item in saved["models"]:
            entries.append({"id": item["id"], "name": item["name"], "protocol": item["protocol"],
                            "model": item["model"], "concurrency": item["concurrency"],
                            **({"builtin": item["builtin"]} if "builtin" in item else {
                                "baseUrl": item["baseUrl"], "contextTokens": item["contextTokens"],
                                "timeoutMs": item["timeoutMs"], "hasKey": bool(item["encryptedKey"])})})
        return {"models": entries, "roles": dict(saved["roles"])}

    def api_profile(self, entry_id):
        """Private gateway profile; retain ciphertext for the content-affecting stamp.

        Callers must never expose this dictionary through a public endpoint.
        """
        with self.lock:
            saved = self._read()
            entry = next((item for item in (saved or {}).get("models", [])
                          if item["id"] == entry_id and "builtin" not in item), None)
            if entry is None:
                return None
            return {key: entry[key] for key in ("protocol", "baseUrl", "model", "contextTokens",
                                                "timeoutMs", "encryptedKey")}

    def public(self, mode="local", source_id=LOCAL_SOURCE_ID, status="active"):
        with self.lock:
            saved = self._read()
        api = self._main(saved) if saved else None
        listing = self.models()
        return {"mode": mode,
                "api": ({"protocol": api["protocol"], "baseUrl": api["baseUrl"],
                         "model": api["model"], "contextTokens": api["contextTokens"],
                         "timeoutMs": api["timeoutMs"],
                         "hasKey": bool(api["encryptedKey"])} if api else None),
                "sourceId": source_id, "status": status,
                "models": listing["models"], "roles": listing["roles"],
                "localJudge": None,
                "modelList": self.model_list()}

    def resolve_key(self, protocol, base_url, supplied_key, model_id=None):
        """A blank field can reuse a key only for its original protocol and host/path."""
        if supplied_key is not None:
            return supplied_key
        with self.lock:
            saved = self._read()
            if not saved:
                return None
            if model_id is not None:
                if not _valid_id(model_id):
                    return None
                entry = next((item for item in saved["models"] if item["id"] == model_id), None)
                if (entry is None or "builtin" in entry or
                        entry["protocol"] != protocol or entry["baseUrl"] != base_url or
                        not entry["encryptedKey"]):
                    return None
                return self._decrypted_key(entry["encryptedKey"])
            for entry in saved["models"]:
                if ("builtin" not in entry and entry["protocol"] == protocol and
                        entry["baseUrl"] == base_url and entry["encryptedKey"]):
                    return self._decrypted_key(entry["encryptedKey"])
            return None

    def _upsert(self, saved, *, entry_id=None, name="", protocol=None, base_url=None, model=None,
                key=_MISSING, context_tokens=_MISSING, timeout_ms=_MISSING, concurrency=None):
        """Insert or update one model entry; returns (id, models, sourceIds).

        A blank key reuses the ciphertext of the stored model with the same
        protocol and base URL — never re-encrypt, or running work would lose
        its stamp. Explicit keys are encrypted fresh.
        """
        models = [dict(item) for item in saved["models"]] if saved else []
        source_ids = dict(saved["sourceIds"]) if saved else {}
        if entry_id is not None and not _valid_id(entry_id):
            raise ValueError("invalid sourceId")
        if (not isinstance(protocol, str) or protocol not in PROTOCOLS or
                protocol == BUILTIN_PROTOCOL):
            raise ValueError("invalid protocol")
        base_url = _text(base_url, "baseUrl", MAX_BASE_URL).strip().rstrip("/")
        if not base_url or any(char in base_url for char in ("\\", " ", "\t", "\r", "\n")):
            raise ValueError("invalid baseUrl")
        model = _text(model, "model", MAX_MODEL_ID).strip()
        if not model:
            raise ValueError("invalid model")
        name = model_name(name)
        fingerprint = _source_fingerprint(protocol, base_url, model)
        existing_id = entry_id or source_ids.get(fingerprint)
        existing = next((item for item in models if item["id"] == existing_id), None)
        if entry_id is not None and existing is None:
            raise ValueError("unknown model")
        if existing is not None and "builtin" in existing:
            raise ValueError("invalid model")
        if entry_id is not None and source_ids.get(fingerprint, entry_id) != entry_id:
            raise ValueError("duplicate model")
        if existing is None and len(models) >= MAX_MODELS:
            raise ValueError("too many models")
        if existing is not None:
            # Editing identity in place retires the old fingerprint mapping.
            old_fingerprint = _source_fingerprint(existing["protocol"], existing["baseUrl"],
                                                  existing["model"])
            if old_fingerprint != fingerprint and source_ids.get(old_fingerprint) == existing_id:
                source_ids.pop(old_fingerprint, None)
        if key is None or key is _MISSING:
            encrypted = None
            if existing is not None and existing["protocol"] == protocol and \
                    existing["baseUrl"] == base_url:
                encrypted = existing["encryptedKey"]
            else:
                # Same protocol+host stored under another entry carries its key over.
                carried = next((item for item in models if "builtin" not in item and
                                item["protocol"] == protocol and
                                item["baseUrl"] == base_url and item["encryptedKey"]), None)
                encrypted = carried["encryptedKey"] if carried else None
        else:
            try:
                encrypted = base64.b64encode(self.protect(key)).decode("ascii")
            except Exception as exc:
                raise ModelSourceUnavailable("model settings unavailable") from exc
        if context_tokens is not _MISSING and context_tokens is not None and (
                type(context_tokens) is not int or
                not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_CONTEXT_TOKENS):
            raise ValueError("invalid contextTokens")
        if timeout_ms is not _MISSING:
            timeout_ms = model_timeout_ms(timeout_ms)
        if concurrency is not None:
            concurrency = model_concurrency(concurrency)
        entry_id = existing_id or uuid.uuid4().hex
        if entry_id in source_ids.values() and source_ids.get(fingerprint) != entry_id:
            raise ValueError("duplicate sourceId")
        entry = {"id": entry_id, "name": name, "protocol": protocol, "baseUrl": base_url,
                 "model": model,
                 "contextTokens": (existing["contextTokens"] if existing is not None and
                                   context_tokens is _MISSING else context_tokens if
                                   context_tokens is not _MISSING else None),
                 "timeoutMs": (existing["timeoutMs"] if existing is not None and
                               timeout_ms is _MISSING else timeout_ms if
                               timeout_ms is not _MISSING else DEFAULT_MODEL_TIMEOUT_MS),
                 "concurrency": (existing["concurrency"] if existing is not None and
                                 concurrency is None else concurrency or DEFAULT_CONCURRENCY),
                 "encryptedKey": encrypted}
        models = [item for item in models if item["id"] != entry_id]
        models.append(entry)
        source_ids[fingerprint] = entry_id
        return entry_id, models, source_ids

    def save_api(self, protocol, base_url, model, key, source_id=None, context_tokens=None,
                 timeout_ms=_MISSING, concurrency=None):
        """Call only after the analysis worker has atomically activated this source."""
        with self.lock:
            saved = self._read()
            entry_id, models, source_ids = self._upsert(
                saved, entry_id=source_id, protocol=protocol, base_url=base_url, model=model,
                key=key, context_tokens=context_tokens, timeout_ms=timeout_ms,
                concurrency=concurrency)
            roles = dict(saved["roles"]) if saved else {"main": None, "judge": None, "image": None}
            roles["main"] = entry_id
            self._write({"version": 2, "selectedMode": "api", "models": models, "roles": roles,
                         "sourceIds": source_ids})
            return entry_id

    def save_model(self, entry_id=None, *, name="", protocol, base_url, model, key=_MISSING,
                   context_tokens=_MISSING, timeout_ms=_MISSING, concurrency=None):
        """Add or edit a model without changing role assignments (the main model
        keeps its identity through this path; /activate remains the main save)."""
        with self.lock:
            saved = self._read()
            result = self._upsert(saved, entry_id=entry_id, name=name, protocol=protocol,
                                  base_url=base_url, model=model, key=key,
                                  context_tokens=context_tokens, timeout_ms=timeout_ms,
                                  concurrency=concurrency)
            entry_id, models, source_ids = result
            roles = dict(saved["roles"]) if saved else {"main": None, "judge": None, "image": None}
            if roles["main"] is None:
                roles["main"] = entry_id
            self._write({"version": 2, "selectedMode": saved["selectedMode"] if saved else "api",
                         "models": models, "roles": roles, "sourceIds": source_ids})
            return entry_id

    def ensure_builtin(self, builtin, name, model):
        """Register the bundled local judge model once; idempotent."""
        with self.lock:
            saved = self._read()
            if saved:
                existing = next((item for item in saved["models"]
                                 if item.get("builtin") == builtin), None)
                if existing is not None:
                    return existing["id"]
                if len(saved["models"]) >= MAX_MODELS:
                    raise ValueError("too many models")
            entry_id = uuid.uuid4().hex
            models = ([dict(item) for item in saved["models"]] if saved else [])
            models.append({"id": entry_id, "name": model_name(name), "protocol": BUILTIN_PROTOCOL,
                           "builtin": builtin, "model": model, "concurrency": 1})
            roles = dict(saved["roles"]) if saved else {"main": None, "judge": None, "image": None}
            source_ids = dict(saved["sourceIds"]) if saved else {}
            self._write({"version": 2, "selectedMode": saved["selectedMode"] if saved else "api",
                         "models": models, "roles": roles, "sourceIds": source_ids})
            return entry_id

    def delete_model(self, entry_id):
        """Delete a non-main model; its roles fall back to following the main model."""
        with self.lock:
            saved = self._read()
            if not saved or not _valid_id(entry_id) or entry_id == saved["roles"]["main"]:
                raise ValueError("invalid model")
            models = [item for item in saved["models"] if item["id"] != entry_id]
            if len(models) == len(saved["models"]):
                raise ValueError("unknown model")
            roles = {role: (None if target == entry_id else target)
                     for role, target in saved["roles"].items()}
            self._write({"version": 2, "selectedMode": saved["selectedMode"], "models": models,
                         "roles": roles, "sourceIds": saved["sourceIds"]})

    def set_roles(self, **changes):
        """Assign models to purposes; ``main`` must be an existing API model."""
        if not changes or set(changes) - set(ROLES):
            raise ValueError("invalid roles")
        with self.lock:
            saved = self._read()
            if not saved:
                raise ValueError("invalid roles")
            roles = dict(saved["roles"])
            for role, target in changes.items():
                if target is None:
                    if role == "main":
                        raise ValueError("invalid roles")
                    roles[role] = None
                    continue
                if not _valid_id(target) or not any(item["id"] == target for item in saved["models"]):
                    raise ValueError("invalid roles")
                if role in ("main", "image") and any(item["id"] == target and "builtin" in item
                                          for item in saved["models"]):
                    raise ValueError("invalid roles")
                roles[role] = target
            self._write({"version": 2, "selectedMode": saved["selectedMode"],
                         "models": saved["models"], "roles": roles,
                         "sourceIds": saved["sourceIds"]})

    def save_local(self):
        """Remember local selection while retaining the encrypted API profile."""
        with self.lock:
            saved = self._read()
            if saved:
                self._write({"version": 2, "selectedMode": "local", "models": saved["models"],
                             "roles": saved["roles"], "sourceIds": saved["sourceIds"]})

    def clear_key(self):
        with self.lock:
            saved = self._read()
            if not saved:
                return
            main_id = saved["roles"]["main"]
            models = [dict(item) for item in saved["models"]]
            for item in models:
                if item["id"] == main_id:
                    if not item.get("encryptedKey") and saved["selectedMode"] == "local":
                        return
                    item["encryptedKey"] = None
            self._write({"version": 2, "selectedMode": "local", "models": models,
                         "roles": saved["roles"], "sourceIds": saved["sourceIds"]})

    def model_list(self):
        """A missing or damaged discovery cache must not disable the saved model."""
        with self.lock:
            try:
                raw = self._read_json(self.catalog_path)
                if not isinstance(raw, dict) or raw.get("version") != 1:
                    return None
                endpoint = connection_values({"protocol": raw["protocol"], "baseUrl": raw["baseUrl"]})
                return {"protocol": endpoint["protocol"], "baseUrl": endpoint["baseUrl"],
                        **model_list_values(raw)}
            except (KeyError, ValueError, TypeError, ModelSourceUnavailable):
                return None

    def save_model_list(self, protocol, base_url, reply):
        """Persist discovery independently from the active profile and its key."""
        endpoint = connection_values({"protocol": protocol, "baseUrl": base_url})
        catalog = {"protocol": endpoint["protocol"], "baseUrl": endpoint["baseUrl"],
                   **model_list_values(reply)}
        with self.lock:
            self._write({"version": 1, **catalog}, path=self.catalog_path)

    def _write(self, data, *, path=None):
        path = self.path if path is None else path
        self._check_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._check_path(path)
        if path == self.path and self._upgrade_pending:
            # First version 2 write: keep the original version 1 document.
            backup = self.path.with_name("api-model-source.v1.json")
            if not backup.exists():
                self._check_path(backup)
                backup.write_bytes(self.path.read_bytes())
            self._upgrade_pending = False
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise ModelSourceUnavailable("model settings unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)
