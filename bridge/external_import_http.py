"""Core HTTP boundary for user-authorized external chat imports.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlsplit

from core_external_media import MAX_FILE_BYTES
from core_http import HostError
from core_plugin_process import PluginError, PluginUnavailable
from core_tasks import TaskError

PREFIX = "/api/external-import/"


def _text(value, name, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name}不正确")
    return value


def get(service, route, query):
    store = service.store
    if route == "status":
        return service.status()
    if route == "usage":
        filters = {"account": "external-chat", "plugin": "kyf.external-chat-import"}
        if query.get("friendId"):
            friend_id = _text(query["friendId"], "好友")
            if not any(friend["id"] == friend_id for friend in store.list_friends()):
                raise KeyError(friend_id)
            filters["friend"] = friend_id
        before = None
        if query.get("before"):
            stamp, separator, identifier = query["before"].partition(",")
            if not separator or not stamp.isascii() or not stamp.isdigit() or len(stamp) > 16:
                raise ValueError("分页位置不正确")
            before = (int(stamp), _text(identifier, "分页位置"))
        ledger = service.core.ledger
        return {"summary": ledger.summary(filters), **ledger.records(filters, before=before, limit=50)}
    if route == "friends":
        return {"friends": store.list_friends()}
    if route == "sources":
        return {"sources": store.list_sources(_text(query.get("friendId"), "好友"))}
    if route == "batches":
        return {"batches": store.list_batches(query.get("friendId") or None)}
    if route == "batch":
        return store.get_batch(_text(query.get("id"), "任务"))
    if route == "messages":
        return store.messages(_text(query.get("friendId"), "好友"), platform=query.get("platform") or None,
                              batch_id=query.get("batchId") or None, query=query.get("q", "")[:500])
    raise HostError(404, "not-found", "接口不存在")


def post(service, route, body):
    store = service.store
    if route == "friends":
        account, user = body.get("wechatAccount"), body.get("wechatUser")
        if account or user:
            _text(account, "微信账号")
            _text(user, "微信好友")
            scope = service.core.scope()
            if scope.get("account") != account or not any(f["user"] == user and not f.get("isGroup") for f in service.core.friends(account)):
                raise ValueError("微信账号或好友已变化，请从当前好友重新打开")
        return {"friend": store.create_friend(_text(body.get("name"), "好友名称", 128),
                                               wechat_account=account, wechat_user=user)}
    if route == "batches":
        return store.create_batch(_text(body.get("friendId"), "好友"), body.get("platform"),
                                  body.get("ownAccount"), body.get("conversationKey"),
                                  note=body.get("note", ""), source_note=body.get("sourceNote", ""),
                                  direction=body.get("direction", "forward"))
    if route == "correct-message":
        return store.correct_message(_text(body.get("messageId"), "消息"), body.get("patch", {}))
    batch_id = _text(body.get("batchId"), "任务")
    if route == "start":
        return service.start(batch_id, reprocess=body.get("reprocess") is True)
    if route == "stop":
        return service.stop(batch_id)
    if route == "cleanup-preview":
        return store.cleanup_preview(batch_id, body.get("kind"))
    # All mutations which can change recognition inputs serialize with starts.
    with service.lock:
        service.require_idle(batch_id)
        if route == "candidate":
            store.update_candidate(batch_id, _text(body.get("candidateId"), "候选"), body.get("patch", {}),
                                   resolve=body.get("resolve") is True)
        elif route == "restore-candidate":
            store.restore_candidate(batch_id, _text(body.get("candidateId"), "候选"), body.get("version"), body.get("expected"))
        elif route == "move-candidate":
            store.move_candidate(batch_id, _text(body.get("candidateId"), "候选"), body.get("after"))
        elif route == "save":
            return store.save_batch(batch_id, body.get("candidateIds"))
        elif route == "notes":
            store.update_notes(batch_id, note=body.get("note"), source_note=body.get("sourceNote"))
        elif route == "reassociate":
            store.reassociate_batch(batch_id, body.get("friendId"), body.get("platform"),
                                    body.get("ownAccount"), body.get("conversationKey"))
        elif route == "undo":
            return store.undo_batch(batch_id)
        elif route == "cleanup":
            return store.cleanup(batch_id, body.get("kind"))
        elif route == "reorder":
            store.reorder_materials(batch_id, body.get("ids"))
        elif route == "remove-material":
            store.remove_material(batch_id, body.get("materialId"))
        elif route == "direction":
            store.set_direction(batch_id, body.get("direction"))
        else:
            raise HostError(404, "not-found", "接口不存在")
        return store.get_batch(batch_id)


def handle(handler, core, method):
    """Called only after the loopback origin/host checks, without reading other bodies."""
    parsed = urlsplit(handler.path)
    route = parsed.path[len(PREFIX):]
    query = handler.query(parsed)
    try:
        service = core.external_import
        if method == "GET":
            if route == "picture":
                store = service.store
                if query.get("message"):
                    path = store.picture(message_id=_text(query["message"], "消息"))
                else:
                    path = store.picture(candidate_id=_text(query.get("candidate"), "候选"))
                if path is None:
                    raise KeyError("picture")
                return handler.send_media(SimpleNamespace(data=None, path=path, mime="image/png"))
            if route in ("evidence", "material"):
                identifier = _text(query.get("id"), "素材")
                path = service.store.evidence_path(identifier) if route == "evidence" else service.store.material_path(identifier)
                if path is None:
                    raise KeyError(identifier)
                import mimetypes
                return handler.send_media(SimpleNamespace(data=None, path=path,
                    mime="image/png" if route == "evidence" else mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
            return handler.send(200, get(service, route, query))
        if method != "POST":
            raise HostError(405, "method-not-allowed", "操作不支持")
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("请提供素材长度")
        raw_length = handler.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ValueError("请求长度不正确")
        length = int(raw_length)
        if route == "upload":
            if handler.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                raise HostError(415, "invalid-content-type", "素材上传格式不正确")
            if not 0 < length <= MAX_FILE_BYTES:
                raise ValueError("单个素材不能超过 128 MB")
            previous_timeout = handler.connection.gettimeout()
            try:
                handler.connection.settimeout(60)
                result = service.upload(_text(query.get("batchId"), "任务"), query.get("name"), handler.rfile, length)
            finally:
                handler.connection.settimeout(previous_timeout)
            return handler.send(200, result)
        if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise HostError(415, "invalid-content-type", "请求须为 JSON")
        if not 0 < length <= 256 * 1024:
            raise ValueError("请求过大或没有内容")
        import json
        body = json.loads(handler.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("请求格式不正确")
        return handler.send(200, post(service, route, body))
    except (HostError, TaskError) as exc:
        handler.close_connection = True
        return handler.send(exc.status, {"error": exc.code, "message": exc.message})
    except (PluginError, PluginUnavailable) as exc:
        return handler.send(503, {"error": exc.code, "message": exc.message})
    except KeyError:
        return handler.send(404, {"error": "not-found", "message": "资料不存在或已清理"})
    except (ValueError, UnicodeError) as exc:
        handler.close_connection = True
        return handler.send(400, {"error": "invalid-request", "message": str(exc)[:300] or "请求格式不正确"})
    except OSError:
        handler.close_connection = True
        return handler.send(503, {"error": "local-file-unavailable", "message": "本机素材无法读写，请检查文件与可用空间"})
