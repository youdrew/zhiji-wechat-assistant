"""Bounded visual observations for the messages around an analysed message.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (advisor_media) into kyf.message-insights, 2026-10-13. Core checks that a
message is current and returns its images; this plugin asks the model what is visible.
Modified 2026-11-09 (Apache-2.0): when the main model reads images itself, the images go to the
analysis and reply calls directly (``collect_images`` / ``attachments``), an animated sticker as
up to four frames; describing them first in a separate vision call is kept only for a separate image model.
"""
import hashlib
import json
import threading
from collections import OrderedDict

from prompts import VISION_SYSTEM, row_media, vision_prompt

MAX_MEDIA_MESSAGES = 3
MAX_IMAGES = 6
MAX_TOTAL_BYTES = 6 * 1024 * 1024
MAX_OBSERVATION_CHARS = 2400
# Direct attachments: an animated sticker can take four of these.
MAX_ATTACHED_IMAGES = 8
MAX_IMAGE_BYTES = 2 * 1024 * 1024
STICKER_FRAMES = 4
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
KIND_NAMES = {"image": "图片", "sticker": "表情", "video": "视频"}


def _size(data):
    return len(data) * 3 // 4 if isinstance(data, str) else -1


def _spread(items, count):
    """``count`` items spread evenly over ``items``, first and last kept."""
    if count >= len(items):
        return items
    if count == 1:
        return items[:1]
    return [items[round(index * (len(items) - 1) / (count - 1))] for index in range(count)]


def attachments(collected, keep=None):
    """Notes for the material and the images, numbered 图1… in the order they are sent.

    ``collected`` is ``collect_images``'s result; ``keep`` limits the images to these messages
    (the ones whose note fitted in the request). A message without images keeps its unread note.
    """
    notes, images = {}, []
    for message_id, value in collected.items():
        if isinstance(value, str):
            notes[message_id] = value
            continue
        if keep is not None and message_id not in keep:
            continue
        first = len(images) + 1
        images.extend({"mime": item["mime"], "data": item["data"]} for item in value["items"])
        numbers = f"图{first}" if len(images) == first else f"图{first}至图{len(images)}"
        evidence = {"source": "attached_images", "messageId": message_id, "kind": value["kind"], "images": numbers}
        if value.get("animated"):
            evidence["frames"] = f"动态表情按时间顺序的 {len(value['items'])} 帧，不是完整动画"
        elif value["kind"] == "video":
            evidence["frames"] = f"视频按时间顺序抽取的 {len(value['items'])} 帧，未听音轨"
        notes[message_id] = "[随附图像] " + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return notes, images


class MediaObservations:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()
        self.cache = OrderedDict()

    @staticmethod
    def collect_images(task, rows, *, recent_messages=100, priority_ids=()):
        """The images themselves, for a main model that reads images: ``{messageId: {kind, items, animated}}``.

        A message that could not be read maps to a note saying so instead.
        """
        priorities = set(priority_ids)
        visual = [row for row in rows[-recent_messages:]
                  if (row_media(row) or {}).get("kind") in ("image", "sticker", "video")]
        selected = sorted(enumerate(visual), key=lambda pair: (pair[1]["msg_id"] in priorities, pair[0]), reverse=True)
        selected = [row for _index, row in selected[:MAX_MEDIA_MESSAGES]]
        result = {}
        remaining_bytes, remaining_images = MAX_TOTAL_BYTES, MAX_ATTACHED_IMAGES
        for row in selected:
            task.check()
            message_id, kind = row["msg_id"], row_media(row)["kind"]
            result[message_id] = "[媒体未读取：本地媒体资源无法读取，不能确认画面内容。]"
            try:
                query = {"frames": STICKER_FRAMES} if kind == "sticker" else {}
                payload = task.core.get("media", account=task.account, user=task.user, id=message_id, **query)
            except Exception:
                task.check()
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("error") == "not-current":
                result[message_id] = "[媒体未读取：该消息不在本次重新核验的最近会话窗口中。]"
                continue
            if payload.get("error") == "unsupported":
                result[message_id] = "[媒体未读取：当前数据来源未提供媒体读取能力。]"
                continue
            items = payload.get("items")
            animated = kind == "sticker" and payload.get("sampling") == "animated-frames"
            most = 3 if kind == "video" else STICKER_FRAMES if animated else 1
            if payload.get("error") or payload.get("kind") != kind or not isinstance(items, list) or                     not 1 <= len(items) <= most or any(not isinstance(item, dict) or item.get("mime") not in IMAGE_TYPES or
                                                       not 0 < _size(item.get("data")) <= MAX_IMAGE_BYTES for item in items):
                continue
            # A later animated sticker gives up frames rather than its place.
            items = _spread(items, min(len(items), remaining_images))
            size = sum(_size(item["data"]) for item in items)
            if not items or size > remaining_bytes:
                result[message_id] = "[媒体未读取：本轮媒体读取额度已用完。]"
                continue
            remaining_bytes -= size
            remaining_images -= len(items)
            result[message_id] = {"kind": kind, "animated": animated and len(items) > 1,
                                  "items": [{"mime": item["mime"], "data": item["data"]} for item in items]}
        return result

    def collect(self, task, rows, *, scope, model_fp, recent_messages=100, priority_ids=()):
        priorities = set(priority_ids)
        visual = [row for row in rows[-recent_messages:]
                  if (row_media(row) or {}).get("kind") in ("image", "sticker", "video")]
        selected = sorted(enumerate(visual), key=lambda pair: (pair[1]["msg_id"] in priorities, pair[0]), reverse=True)
        selected = [row for _index, row in selected[:MAX_MEDIA_MESSAGES]]
        result = {}
        remaining_bytes, remaining_images = MAX_TOTAL_BYTES, MAX_IMAGES
        for row in selected:
            task.check()
            message_id, kind = row["msg_id"], row_media(row)["kind"]
            result[message_id] = "[媒体未读取：本地媒体资源无法读取，不能确认画面内容。]"
            try:
                payload = task.core.get("media", account=task.account, user=task.user, id=message_id)
            except Exception:
                task.check()
                continue
            if payload.get("error") == "not-current":
                result[message_id] = "[媒体未读取：该消息不在本次重新核验的最近会话窗口中。]"
                continue
            if payload.get("error") == "unsupported":
                result[message_id] = "[媒体未读取：当前数据来源未提供媒体读取能力。]"
                continue
            items = payload.get("items")
            if payload.get("error") or payload.get("kind") != kind or not isinstance(items, list) or \
                    not 1 <= len(items) <= (3 if kind == "video" else 1):
                continue
            size = sum(len(item.get("data") or "") * 3 // 4 for item in items)
            if size > remaining_bytes or len(items) > remaining_images:
                result[message_id] = "[媒体未读取：本轮媒体读取额度已用完。]"
                continue
            remaining_bytes -= size
            remaining_images -= len(items)
            sampling = payload.get("sampling") if kind in ("image", "sticker") else None
            images = [{"mime": item["mime"], "data": item["data"]} for item in items]
            digest = hashlib.sha256(json.dumps(images, sort_keys=True).encode()).hexdigest()
            key = (task.account, scope, task.user, message_id, model_fp, kind, sampling, digest)
            with self.lock:
                observation = self.cache.get(key)
            if observation is None:
                try:
                    response = self.plugin.models.generate(task, {
                        "system": VISION_SYSTEM, "prompt": vision_prompt(kind, sampling, items), "images": images,
                        "maxOutputTokens": 1200, "timeoutMs": 45_000, "thinking": False}, step="vision",
                        role="image")
                    observation = (response.get("text") or "").strip()
                    if not observation or len(observation) > MAX_OBSERVATION_CHARS:
                        result[message_id] = "[媒体未读取：视觉模型未返回有效观察，不能确认画面内容。]"
                        continue
                    with self.lock:
                        self.cache[key] = observation
                        while len(self.cache) > 128:
                            self.cache.popitem(last=False)
                except Exception:
                    task.check()
                    result[message_id] = "[媒体未读取：视觉模型读取失败，不能确认画面内容。]"
                    continue
            evidence = {"source": "model_visual_observation", "messageId": message_id, "kind": kind,
                        "observation": observation, "limitations": "有限画面观察，不是发送者自述，不据此推断人格"}
            if kind == "video":
                evidence.update(frameTimesMs=[item.get("timeMs") for item in items], audioAnalyzed=False,
                                fullVideoAnalyzed=False)
            if sampling == "animated-image-first-frame":
                evidence.update(sampling=sampling, fullAnimationAnalyzed=False)
            result[message_id] = "[本轮视觉观察] " + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        return result
