"""Bounded visual observations for the messages around an analysed message.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (advisor_media) into kyf.message-insights, 2026-10-13. Core checks that a
message is current and returns its images; this plugin asks the model what is visible.
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


class MediaObservations:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()
        self.cache = OrderedDict()

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
