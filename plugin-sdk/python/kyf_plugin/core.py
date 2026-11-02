"""Client for the Core plugin interface (``/plugin-api/v1``).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 60
# Core listens on loopback: its calls (with the plugin token) never go through a system or environment proxy,
# whose bypass list may not cover 127.0.0.1.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CoreError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ExecutionRevoked(CoreError):
    """The task execution was stopped or replaced; stop writing and return."""


class CoreClient:
    def __init__(self, url, token, execution_token=None):
        self.url = url.rstrip("/")
        self.token = token
        self.execution_token = execution_token

    def for_execution(self, execution_token):
        return CoreClient(self.url, self.token, execution_token)

    def _request(self, method, path, query=None, body=None, stream=False):
        url = f"{self.url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if self.execution_token:
            headers["X-KYF-Execution"] = self.execution_token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            response = _OPENER.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}")
            except ValueError:
                detail = {}
            code, message = detail.get("error", "core-error"), detail.get("message", f"Core 返回 {exc.code}")
            raise (ExecutionRevoked if code == "execution-revoked" else CoreError)(exc.code, code, message) from None
        except (urllib.error.URLError, OSError) as exc:
            raise CoreError(0, "core-unavailable", "无法连接 Core") from exc
        if stream:
            return response
        with response:
            return json.loads(response.read() or b"null")

    def get(self, path, **query):
        return self._request("GET", path, query)

    def post(self, path, body=None, **query):
        return self._request("POST", path, query, body if body is not None else {})

    def delete(self, path, body=None, **query):
        return self._request("DELETE", path, query, body)

    # Convenience wrappers --------------------------------------------------------------------------------------------
    def scope(self):
        return self.get("scope")

    def settings(self):
        """Snapshot this plugin's settings at task start; no execution token is required."""
        return self.get("settings")

    def friends(self, account):
        """The conversations the user added to the software: ``[{"user", "name", "isGroup"}]``."""
        return self.get("friends", account=account)["friends"]

    def friend(self, account, user):
        return self.get(f"friends/{urllib.parse.quote(user, safe='')}", account=account)

    def history_status(self, account, user, fresh=False, audit=False, audit_highwater=None):
        """Read import state; ``audit`` also checks numeric metadata for the saved source prefix."""
        return self.get("history/status", account=account, user=user, fresh="1" if fresh else None,
                        audit="1" if audit else None, auditHighwater=audit_highwater)

    def history_status_many(self, account, users):
        """Ordinary ``history_status`` of <=500 chats in one call, without source metadata or audits."""
        return self.post("history/status-many", {"account": account, "users": list(users)})

    def history_conversations(self, account, after=None, limit=500):
        """Page all retained chats (``isGroup`` marks groups), regardless of sidebar selection; pass ``next`` as ``after``."""
        return self.get("history/conversations", account=account, after=after, limit=limit)

    def history_prepare(self, account, user, refresh=False, rebuild=False):
        """Start or continue Core's import of the friend's history; poll ``history_status`` for ``ready``."""
        return self.post("history/prepare", {"account": account, "user": user, "refresh": refresh is True,
                                             **({"rebuild": True} if rebuild else {})})

    def history_messages(self, account, user, after=None, limit=500):
        return self.get("history/messages", account=account, user=user, after=after, limit=limit)

    def history_verify(self, account, user, items, fresh=False):
        """Verify source hashes; ``fresh`` also checks original locators for edits/deletions."""
        return self.post("history/verify", {"account": account, "user": user, "items": list(items), "fresh": fresh})

    def history_verify_many(self, account, groups):
        """Freshly verify <=500 references across <=16 contacts in one source read."""
        return self.post("history/verify-many", {"account": account, "groups": list(groups), "fresh": True})

    def history_export(self, account, user):
        """Yield every message up to the current watermark, then the trailer record."""
        with self._request("GET", "history/export", {"account": account, "user": user}, stream=True) as response:
            for line in response:
                if line.strip():
                    yield json.loads(line)

    def human(self, account, user):
        return self.get(f"human/{urllib.parse.quote(user, safe='')}", account=account)

    def self_profile(self, account):
        """Core-owned account background/style/version; plugins cannot silently overwrite it."""
        return self.get("account/self-profile", account=account)

    def self_context(self, account, user, message_id, query, budget_chars=6000, limit=5):
        """Execution-bound optional personal expression context (message-insights only)."""
        return self.post("services/self-context", {"account": account, "user": user,
            "messageId": message_id, "query": query, "budgetChars": budget_chars, "limit": limit})

    def self_context_validate(self, account, user, message_id, context):
        """Validate the precise selected sample set before committing suggestions."""
        return self.post("services/self-context", {"account": account, "user": user, "messageId": message_id,
            "validateOnly": True, "generation": context.get("generation"),
            "profileVersion": context.get("profileVersion"),
            "sources": [{"id": item.get("id"), "hash": item.get("hash")} for item in context.get("sources", [])]})

    def human_snapshot(self, account, user):
        """The verified human-data snapshot with its ``stamp`` (changes whenever the user edits it)."""
        return self.get(f"human/{urllib.parse.quote(user, safe='')}", account=account, view="snapshot")

    def media(self, account, user, message_id, frames=None):
        """Images of one image, sticker or video message; ``frames`` (2-4) spreads stills over an animated sticker."""
        return self.get("media", account=account, user=user, id=message_id, frames=frames)

    def history_stickers(self, account, user, ids):
        """``{"stickers": {message id: {"id", "pack"?, "caption"?, "packName"?}}}`` for up to 1000 sticker messages."""
        return self.post("history/stickers", {"account": account, "user": user, "ids": list(ids)})

    def stop_task(self, task_id):
        """Stop one of this plugin's own tasks: Core revokes its token and closes it as ``stopped``."""
        return self.post(f"tasks/{urllib.parse.quote(task_id, safe='')}/stop")

    def claim_file(self, token):
        """One-time: the ``{path, label, kind}`` of a file or folder the user picked in the host."""
        return self.post(f"files/{urllib.parse.quote(token, safe='')}/claim")

    def model_services(self):
        return self.get("model/services")

    def embedding_status(self, account):
        """Inspect the fixed local retrieval model without downloading or loading it."""
        return self.get("embeddings/status", account=account)

    def embedding_prepare(self, account):
        """Start/resume the opt-in domestic asset download; returns immediately."""
        return self.post("embeddings/prepare", {"account": account})

    def embed(self, account, texts, kind="passage"):
        """Local vectors: <=16 texts; passage <=2400, query <=4000 Unicode characters."""
        return self.post("embeddings/embed", {"account": account, "texts": list(texts), "kind": kind})

    def subscribe(self, account, user, event="message.new", *, replay_live=False):
        """Optionally replay this host activation's live messages on first subscription."""
        body = {"account": account, "user": user, "event": event}
        if replay_live is not False:
            body["replayLive"] = replay_live
        return self.post("subscriptions", body)

    def unsubscribe(self, account, user, event="message.new"):
        return self.delete("subscriptions", {"account": account, "user": user, "event": event})
