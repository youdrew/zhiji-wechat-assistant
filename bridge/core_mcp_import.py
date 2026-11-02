"""Core business methods behind the MCP external-chat-import tools (阶段 3b 第三批).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

External imports are Core-owned (files, checkpoints, review and saved records),
so the capability proxy reaches the same ``ExternalImportService`` and store the
GUI uses -- the transcription plugin answers only inside Core's background run
and never sees an MCP caller. 未绑定微信的外部联系人按 ``external_contact``
主体类型独立授权（键为导入素材库的稳定好友 ID，绝不按显示名碰撞到微信联系
人）；本机文件输入没有 MCP 路径：上传永远经 GUI 的显式选择，MCP 端只管理
已有素材，任何路径参数都会被代理以稳定码拒绝。
"""
from __future__ import annotations

from core_mcp_proxy import McpError
from core_tasks import TaskError

# 导入素材库不是已保存微信账号：授权表用这个固定伪账号区分它的主体命名空间。
EXTERNAL_ACCOUNT = "external-chat"
PAGE_MAX = 50


def _summary(batch):
    """The bounded batch view: state, source and counts -- never the raw segments."""
    return {"id": batch["id"], "friendId": batch["friendId"], "friend": batch["friend"],
            "source": batch["source"], "note": batch["note"], "state": batch["state"],
            "error": batch["error"], "createdAt": batch["createdAt"], "updatedAt": batch["updatedAt"],
            "archived": batch["archived"], "direction": batch["direction"],
            "progress": batch["progress"], "counts": batch["counts"],
            "materialCount": len(batch["materials"])}


class McpImportBridge:
    """The one external-import surface the capability proxy may call."""

    def __init__(self, services):
        self.services = services

    def _store(self):
        service = getattr(self.services, "external_import", None)
        if service is None:
            raise McpError(501, "capability-not-open", "外部聊天导入服务尚未接入")
        return service.store

    def _call(self, run):
        """Map Core-owned failures onto the stable codes the MCP route reports."""
        try:
            return run()
        except McpError:
            raise
        except TaskError as exc:
            raise McpError(exc.status, exc.code, exc.message) from None
        except KeyError:
            raise McpError(404, "not-found", "资料不存在或已清理") from None
        except ValueError as exc:
            raise McpError(400, "invalid-request", str(exc)[:300] or "请求参数不正确") from None
        except OSError:
            raise McpError(503, "local-file-unavailable",
                           "本机素材无法读写，请检查文件与可用空间") from None

    def _batch(self, store, batch_id, subject_id):
        """One batch of exactly the authorized external friend, or it does not exist here."""
        batch = self._call(lambda: store.get_batch(batch_id))
        if batch["friendId"] != subject_id:
            raise McpError(404, "not-found", "导入批次不存在或不属于该外部联系人")
        return batch

    def _require_friend(self, store, friend_id):
        """The subject must be a real external-contact record; names are never identifiers."""
        if not any(friend["id"] == friend_id for friend in self._call(store.list_friends)):
            raise McpError(404, "not-found", "外部联系人不存在")

    # -- contacts -------------------------------------------------------------
    def friends(self):
        """All external contacts; the proxy filters per-contact clients to their grants."""
        store = self._store()
        friends = self._call(store.list_friends)
        return [{"id": friend["id"], "name": friend["name"],
                 "wechatBound": friend["wechatAccount"] is not None} for friend in friends]

    # -- batches and materials ------------------------------------------------
    def batches(self, friend_id):
        store = self._store()
        self._require_friend(store, friend_id)
        batches = self._call(lambda: store.list_batches(friend_id))
        return [_summary(batch) for batch in batches]

    def batch(self, batch_id, subject_id):
        """One batch with its materials; transcript and review records go through candidates."""
        store = self._store()
        batch = self._batch(store, batch_id, subject_id)
        return {**_summary(batch), "materials": batch["materials"]}

    def remove_material(self, batch_id, subject_id, material_id):
        store = self._store()
        batch = self._batch(store, batch_id, subject_id)
        if not any(material["id"] == material_id for material in batch["materials"]):
            raise McpError(404, "not-found", "该批次没有这份素材")
        batch = self._call(lambda: store.remove_material(batch_id, material_id))
        return _summary(batch)

    # -- transcript and review records ----------------------------------------
    def candidates(self, batch_id, subject_id, limit, offset):
        """Paged candidates of one batch: the transcript and its review states."""
        store = self._store()
        batch = self._batch(store, batch_id, subject_id)
        items = batch["candidates"]
        return {"batchId": batch_id, "state": batch["state"], "total": len(items),
                "items": items[offset:offset + limit]}

    # -- saved results ----------------------------------------------------------
    def messages(self, friend_id, *, platform=None, batch_id=None, query="", limit=PAGE_MAX, offset=0):
        store = self._store()
        self._require_friend(store, friend_id)
        result = self._call(lambda: store.messages(friend_id, platform=platform, batch_id=batch_id,
                                                   query=query))
        messages = result["messages"]
        return {"friendId": friend_id, "total": len(messages), "items": messages[offset:offset + limit],
                "gapCount": len(result["gaps"]), "sources": result["sources"],
                "revision": result["revision"], "warnings": result["warnings"],
                "coverage": result["coverage"]}

    # -- controlled operations --------------------------------------------------
    def start(self, batch_id, subject_id, reprocess=False):
        service = getattr(self.services, "external_import", None)
        if service is None:
            raise McpError(501, "capability-not-open", "外部聊天导入服务尚未接入")
        self._batch(service.store, batch_id, subject_id)
        batch = self._call(lambda: service.start(batch_id, reprocess=reprocess))
        return _summary(batch)

    def stop(self, batch_id, subject_id):
        service = getattr(self.services, "external_import", None)
        if service is None:
            raise McpError(501, "capability-not-open", "外部聊天导入服务尚未接入")
        self._batch(service.store, batch_id, subject_id)
        return self._stop_unchecked(service, batch_id)

    def stop_client_batch(self, batch_id):
        """Revocation listener hook: stop without a subject, the client binding is already gone."""
        service = getattr(self.services, "external_import", None)
        if service is None:
            return None
        return self._stop_unchecked(service, batch_id)

    @staticmethod
    def _stop_unchecked(service, batch_id):
        try:
            batch = service.stop(batch_id)
        except TaskError as exc:
            raise McpError(exc.status, exc.code, exc.message) from None
        except KeyError:
            raise McpError(404, "not-found", "导入批次不存在") from None
        except ValueError as exc:
            raise McpError(400, "invalid-request", str(exc)[:300]) from None
        return _summary(batch)

    def save(self, batch_id, subject_id, candidate_ids=None):
        """Publish reviewed records; the store refuses active or unreviewable batches."""
        store = self._store()
        self._batch(store, batch_id, subject_id)
        batch = self._call(lambda: store.save_batch(batch_id, candidate_ids))
        return _summary(batch)
