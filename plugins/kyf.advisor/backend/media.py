"""Bounded, transient visual observations for the selected WeChat conversation.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
Media bytes and base64 never enter the context projection or persisted sessions.
Moved from bridge/advisor_media.py into the kyf.advisor plugin, 2026-10-13: Core checks that a message is in
the current conversation window and returns its images (``GET /plugin-api/v1/media``); the plugin asks the
model what is visible, within the same quotas as before.
Modified 2026-10-14 (Apache-2.0): an animated sticker is read as up to four frames, and stickers that already
carry a caption or label in the chat record are not read again.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from collections import OrderedDict

RECENT_MESSAGES = 20
MAX_MEDIA_MESSAGES = 3
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 6 * 1024 * 1024
MAX_OBSERVATION_CHARS = 2400
STICKER_FRAMES = 4
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
MEDIA_BOUNDARY = ("本轮视觉读取仅限当前会话所提供范围内的最多 3 条图片/表情/视频、合计最多 6 张图像。"
                  "未标注本轮视觉观察的媒体均未读取内容；不可根据占位符猜测。"
                  "视觉观察是模型对画面的有限描述，不是发送者自述；不得根据人脸推断性格、身份或敏感属性。"
                  "视频仅抽帧、未听音轨；语音仅使用微信已有转写，未听原音。")


def media_metadata(value):
    """Only bounded, known metadata; never a local path or media bytes (same rule as Core's projection)."""
    if not isinstance(value, dict) or value.get("kind") not in ("image", "sticker", "video", "voice", "audio"):
        return None
    result = {"kind": value["kind"]}
    for key in ("durationMs", "width", "height"):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 86_400_000:
            result[key] = number
    if value.get("transcriptSource") == "wechat" and isinstance(value.get("transcript"), str):
        result.update(transcript=value["transcript"][:8000], transcriptSource="wechat")
    return result


def row_media(row):
    raw = json.loads(row.get("raw") or "{}")
    known = media_metadata(raw.get("media"))
    if known:
        return known
    kind = row.get("kind")
    aliases = {"3": "image", "34": "voice", "43": "video", "62": "video",
               "47": "sticker", "11000": "sticker", "动画表情": "sticker", "表情": "sticker"}
    inferred = aliases.get(str(raw.get("type")), kind)
    return media_metadata({"kind": inferred})


def _decoded_size(data):
    try:
        return len(base64.b64decode(data, validate=True))
    except (binascii.Error, TypeError, ValueError):
        return -1


class MediaObservations:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = OrderedDict()

    def clear_account(self, account):
        with self.lock:
            for key in list(self.cache):
                if key[0] == account:
                    self.cache.pop(key, None)

    def collect(self, rows, *, account, user, scope, model_fp, read, inspect, check,
                recent_messages=RECENT_MESSAGES, priority_ids=(), skip_ids=()):
        """``read(message_id, frames=None)`` is Core's media view; ``inspect(payload)`` asks the vision model.

        ``skip_ids`` are messages the chat record already describes (labelled stickers).
        """
        if type(recent_messages) is not int or not 1 <= recent_messages <= 100:
            raise ValueError("invalid media context range")
        priorities, skipped = set(priority_ids), set(skip_ids)
        visual = [row for row in rows[-recent_messages:] if row["msg_id"] not in skipped and
                  (row_media(row) or {}).get("kind") in ("image", "sticker", "video")]
        # Read explicitly selected target bubbles first, within the same quotas.
        selected = sorted(enumerate(visual), key=lambda pair: (pair[1]["msg_id"] in priorities, pair[0]), reverse=True)
        selected = [row for _index, row in selected[:MAX_MEDIA_MESSAGES]]
        result = {}
        remaining_bytes, remaining_images = MAX_TOTAL_BYTES, MAX_IMAGES
        for row in selected:
            check()
            message_id, kind = row["msg_id"], row_media(row)["kind"]
            result[message_id] = "[媒体未读取：本地媒体资源无法读取，不能确认画面内容。]"
            if not callable(read):
                result[message_id] = "[媒体未读取：当前数据来源未提供媒体读取能力。]"
                continue
            if not callable(inspect):
                result[message_id] = "[媒体未读取：当前模型未提供视觉读取接口。]"
                continue
            try:
                payload = read(message_id, frames=STICKER_FRAMES) if kind == "sticker" else read(message_id)
            except Exception:
                check()  # Account changes and cancellation must not become a fallback.
                continue
            check()
            if not isinstance(payload, dict):
                continue
            if payload.get("error") == "not-current":
                result[message_id] = "[媒体未读取：该消息不在本次重新核验的最近会话窗口中。]"
                continue
            if payload.get("error") == "unsupported":
                result[message_id] = "[媒体未读取：当前数据来源未提供媒体读取能力。]"
                continue
            if payload.get("error") or payload.get("kind") != kind:
                continue
            items = payload.get("items")
            frames = kind == "sticker" and payload.get("sampling") == "animated-frames"
            timed = kind == "video" or frames
            if not isinstance(items, list) or not 1 <= len(items) <= (3 if kind == "video" else STICKER_FRAMES if frames else 1):
                continue
            images, size, valid = [], 0, True
            for item in items:
                if not isinstance(item, dict):
                    valid = False
                    break
                data, mime, timestamp = item.get("data"), item.get("mime"), item.get("timeMs")
                length = _decoded_size(data) if isinstance(data, str) else -1
                if (not 0 < length <= MAX_IMAGE_BYTES or mime not in IMAGE_TYPES or
                        timed and (type(timestamp) not in (int, float) or not 0 <= timestamp <= 86_400_000 or
                                   images and timestamp < images[-1]["timeMs"])):
                    valid = False
                    break
                size += length
                images.append({"mime": mime, "data": data, **({"timeMs": timestamp} if timed else {})})
            if not valid:
                continue
            if size > remaining_bytes or len(images) > remaining_images:
                result[message_id] = "[媒体未读取：本轮媒体读取额度已用完。]"
                continue
            remaining_bytes -= size
            remaining_images -= len(images)
            sampling = ("animated-frames" if frames else "animated-image-first-frame" if kind in ("image", "sticker") and
                        payload.get("sampling") == "animated-image-first-frame" else None)
            digest = hashlib.sha256(json.dumps(images, sort_keys=True).encode()).hexdigest()
            cache_key = (account, scope, user, message_id, model_fp, kind, sampling, digest)
            with self.lock:
                observation = self.cache.get(cache_key)
                if observation:
                    self.cache.move_to_end(cache_key)
            if observation is None:
                try:
                    response = inspect({"mediaKind": kind, "images": images,
                                        **({"sampling": sampling} if sampling else {})})
                    check()
                    observation = response.get("text") if isinstance(response, dict) else None
                    if not isinstance(observation, str) or not observation.strip() or len(observation) > MAX_OBSERVATION_CHARS:
                        result[message_id] = "[媒体未读取：视觉模型未返回有效观察，不能确认画面内容。]"
                        continue
                    observation = observation.strip()
                    with self.lock:
                        self.cache[cache_key] = observation
                        while len(self.cache) > 128:
                            self.cache.popitem(last=False)
                except Exception:
                    check()
                    result[message_id] = "[媒体未读取：视觉模型读取失败，不能确认画面内容。]"
                    continue
            check()
            evidence = {"source": "model_visual_observation", "messageId": message_id,
                        "kind": kind, "observation": observation,
                        "limitations": "有限画面观察，不是发送者自述，不据此推断人格"}
            if kind == "video":
                evidence.update(frameTimesMs=[image["timeMs"] for image in images],
                                audioAnalyzed=False, fullVideoAnalyzed=False)
            if sampling:
                evidence.update(sampling=sampling, fullAnimationAnalyzed=False)
            if frames:
                evidence.update(frameTimesMs=[image["timeMs"] for image in images])
            result[message_id] = "[本轮视觉观察] " + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        return result
