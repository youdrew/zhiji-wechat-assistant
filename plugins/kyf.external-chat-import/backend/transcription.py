"""One bounded image recognition through Core's task-scoped model gateway.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import threading

from kyf_plugin import Cancelled, CoreError, ExecutionRevoked, ModelError, RpcError, TaskContext

from prompts import FORMAT_RETRY, ISSUES, KINDS, REVIEW_SYSTEM, SYSTEM, prompt, review_prompt
from tracking import match_observations

PLUGIN_ID = "kyf.external-chat-import"
MAX_IMAGE_BYTES = 2_621_440
MAX_OUTPUT_CHARS = 256_000
MAX_MESSAGES = 200
DATA_URL = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})\Z")
NON_TEXT_LABEL = {"image": "[图片]", "voice": "[语音]", "video": "[视频]"}
BLOCKING_ISSUES = ISSUES - {"incomplete_time", "missing_original"}
DATE_ONLY = re.compile(
    r"(?:(?:\d{4}[年/.-])?(?:0?[1-9]|1[0-2])[月/.-](?:0?[1-9]|[12]\d|3[01])日?(?:\s*(?:星期|周)[一二三四五六日天])?"
    r"|(?:今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]))?"
    r"\s*(?:(?:上午|下午|晚上|早上|凌晨|中午)?\s*(?:[01]?\d|2[0-3])[:：][0-5]\d(?:[:：][0-5]\d)?)?\Z")


def plan():
    """Core performs mechanical slicing; this plugin supplies the recognition policy."""
    return {"schema": 1, "screenshot": {"tileHeight": 1600, "overlap": 400},
            "video": {"intervalMs": 750, "sampleWindowMs": 100, "maxFrames": 240,
                      "maxDurationMs": 180_000},
            "image": {"maxBytes": MAX_IMAGE_BYTES, "maxWidth": 1600, "minWidth": 660,
                      "format": "png", "maxUpscale": 3},
            "refinement": {"maxFrames": 3, "maxReviews": 40, "maxAttempts": 1}}


def fail(code="invalid-output", message="识别结果格式不正确，当前画面未保存，请重试"):
    raise RpcError(code, message)


def bounded_text(value, limit, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        fail()
    return value.strip()


def image_payload(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 100:
        fail("invalid-request", "识别画面过大或格式不正确")
    match = DATA_URL.fullmatch(value)
    if match is None:
        fail("invalid-request", "识别画面必须为 PNG、JPEG 或 WebP 内嵌图像")
    try:
        raw = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        fail("invalid-request", "识别画面编码不正确")
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        fail("invalid-request", "识别画面为空或超过大小限制")
    # Core decodes and re-encodes inputs before calling us. Also reject a MIME mismatch here.
    valid = {"image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
             "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
             "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"}
    if not valid[match[1]]:
        fail("invalid-request", "识别画面类型与文件内容不一致")
    return {"mime": match[1], "data": match[2]}


def previous_rows(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        fail("invalid-request", "相邻画面上下文不正确")
    rows = []
    for row in value:
        if not isinstance(row, dict) or row.get("sender") not in ("self", "other", "unknown"):
            fail("invalid-request", "相邻画面上下文不正确")
        text = row.get("text")
        if not isinstance(text, str) or len(text) > 20_000 or "\0" in text:
            fail("invalid-request", "相邻画面上下文过长或格式不正确")
        candidate = {"text": text, "sender": row["sender"]}
        for field in ("id", "segmentId", "quote"):
            if field in row and row[field] is not None:
                item = row[field]
                if not isinstance(item, str) or len(item) > (5000 if field == "quote" else 1024) or "\0" in item:
                    fail("invalid-request", "相邻画面上下文不正确")
                candidate[field] = item
        if "kind" in row:
            if not isinstance(row["kind"], str) or row["kind"] not in KINDS:
                fail("invalid-request", "相邻画面上下文不正确")
            candidate["kind"] = row["kind"]
        if "issues" in row:
            candidate["issues"] = context_issues(row["issues"])
        if row.get("bbox") is not None:
            candidate["bbox"] = bbox(row["bbox"], request=True)
        if "version" in row:
            if type(row["version"]) is not int or row["version"] < 0:
                fail("invalid-request", "相邻画面上下文不正确")
            candidate["version"] = row["version"]
        context_metadata(row, candidate)
        rows.append(candidate)
    return rows


def json_object(raw):
    if not isinstance(raw, str) or len(raw) > MAX_OUTPUT_CHARS:
        fail()
    raw = raw.strip()
    # Accept a single JSON fence from providers that ignore JSON mode, never scrape partial JSON.
    if raw.startswith("```json\n") and raw.endswith("\n```"):
        raw = raw[8:-4]
    elif raw.startswith("```\n") and raw.endswith("\n```"):
        raw = raw[4:-4]
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        fail()
    if not isinstance(parsed, dict):
        fail()
    return parsed


def issue_list(value, *, request=False):
    if not isinstance(value, list) or len(value) > len(ISSUES) or any(
            not isinstance(item, str) or item not in ISSUES for item in value):
        fail("invalid-request" if request else "invalid-output")
    return list(dict.fromkeys(value))


def context_issues(value):
    """Core adds lifecycle/relationship issues; only recognition issues go to the model."""
    if not isinstance(value, list) or len(value) > 64 or any(
            not isinstance(item, str) or len(item) > 200 or "\0" in item for item in value):
        fail("invalid-request", "候选疑点格式不正确")
    return list(dict.fromkeys(item for item in value if item in ISSUES))


def context_metadata(row, normalized):
    if "order" in row:
        if type(row["order"]) is not int or not 0 <= row["order"] <= 1_000_000:
            fail("invalid-request", "候选顺序不正确")
        normalized["order"] = row["order"]
    if "truncated" in row:
        if type(row["truncated"]) is not bool:
            fail("invalid-request", "候选截断标记不正确")
        normalized["truncated"] = row["truncated"]


def context_message(row):
    if not isinstance(row, dict):
        fail("invalid-request", "候选格式不正确")
    row = {**row, "issues": context_issues(row.get("issues", []))}
    # Legacy Core candidates may carry this field independently of issues.
    if row.get("truncated") is True and "truncated" not in row["issues"]:
        row["issues"].append("truncated")
    result = normalize_message(row)
    if result is not None:
        context_metadata(row, result)
    return result


def bbox(value, *, request=False):
    if not isinstance(value, list) or len(value) != 4 or any(
            type(item) not in (int, float) or not math.isfinite(item) for item in value) or \
            not all(0 <= item <= 1 for item in value) or min(value[2:]) <= 0 or \
            value[0] + value[2] > 1.001 or value[1] + value[3] > 1.001:
        fail("invalid-request" if request else "invalid-output")
    return value


def normalize_message(row):
    if not isinstance(row, dict) or not isinstance(row.get("kind", "text"), str) or \
            row.get("kind", "text") not in KINDS:
        fail()
    kind = row.get("kind", "text")
    sender = row.get("sender")
    if sender not in ("self", "other", "unknown") and not (sender == "system" and kind == "system"):
        fail()
    text = bounded_text(row.get("text"), 20_000)
    # UI classifications never override a known human text bubble. Date-like human messages
    # such as “10:20” remain messages; only non-human/date separator observations are removed.
    classification = row.get("classification")
    if sender not in ("self", "other") and classification in ("ui", "ui_chrome"):
        return None
    date_only = bool(text and DATE_ONLY.fullmatch(text))
    if sender not in ("self", "other") and date_only and (
            kind == "system" or classification == "date_separator"):
        return None
    issues = issue_list(row.get("issues", []))
    if kind == "system":
        sender = "unknown"
        issues = [item for item in issues if item != "unknown_sender"]
    elif sender == "unknown" and "unknown_sender" not in issues:
        issues.append("unknown_sender")
    if kind in NON_TEXT_LABEL:
        text = text or NON_TEXT_LABEL[kind]
        if "missing_original" not in issues:
            issues.append("missing_original")
    if (not text or "□" in text or "\ufffd" in text) and "unclear_text" not in issues:
        issues.append("unclear_text")
    message = {"text": text, "sender": sender, "kind": kind,
               "timeLabel": bounded_text(row.get("timeLabel"), 200, nullable=True) or None,
               "timestamp": None, "issues": issues}
    if "quote" in row and row["quote"] is not None:
        message["quote"] = bounded_text(row["quote"], 5000)
    if row.get("bbox") is not None:
        message["bbox"] = bbox(row["bbox"])
    return message


def parse_result(raw):
    parsed = json_object(raw)
    if not isinstance(parsed.get("messages"), list) or len(parsed["messages"]) > MAX_MESSAGES:
        fail()
    warnings = parsed.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 30:
        fail()
    warnings = [bounded_text(item, 500) for item in warnings]
    messages = []
    for row in parsed["messages"]:
        message = normalize_message(row)
        if message is None:
            continue
        if "multiple_conversations" in warnings and "multiple_conversations" not in message["issues"]:
            message["issues"].append("multiple_conversations")
        messages.append(message)
    if not messages and "no_visible_messages" not in warnings:
        warnings.append("no_visible_messages")
    result = {"schema": 1, "messages": messages, "warnings": list(dict.fromkeys(warnings))}
    if parsed["messages"] and not messages:
        result["uiOnly"] = True
    return result


def unresolved(warning="review_unresolved"):
    return {"schema": 1, "resolved": False, "evidenceSegmentIds": [], "warnings": [warning]}


def parse_review(raw, provided_ids):
    parsed = json_object(raw)
    if type(parsed.get("resolved")) is not bool:
        fail()
    if parsed["resolved"] is False:
        return unresolved()
    if parsed.get("matchesTarget") is not True or parsed.get("fullyVisible") is not True:
        return unresolved()
    warnings = parsed.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 30 or any(
            not isinstance(item, str) or len(item) > 500 or "\0" in item for item in warnings):
        fail()
    if warnings:
        # A successful rewrite with an accompanying uncertainty is still uncertain.
        # Never clear a candidate by ignoring a contradictory top-level limitation.
        return unresolved()
    evidence = parsed.get("evidenceSegmentIds")
    preferred = parsed.get("preferredEvidenceSegmentId")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 3 or any(
            not isinstance(item, str) or item not in provided_ids for item in evidence) or \
            len(set(evidence)) != len(evidence) or preferred not in evidence:
        fail()
    message = normalize_message(parsed.get("message"))
    if message is None or not message["text"] or any(item in BLOCKING_ISSUES for item in message["issues"]) or \
            (message["sender"] == "unknown" and message["kind"] != "system"):
        return unresolved()
    return {"schema": 1, "resolved": True, "message": message, "evidenceSegmentIds": evidence,
            "preferredEvidenceSegmentId": preferred, "warnings": []}


class Transcription:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()

    def _validate(self, params, context):
        if not isinstance(params, dict) or not isinstance(context, dict):
            fail("invalid-request", "缺少识别参数或后台任务")
        task, execution = context.get("task"), context.get("execution")
        if not isinstance(task, dict) or not isinstance(execution, dict) or task.get("kind") != "background" or \
                task.get("plugin", PLUGIN_ID) != PLUGIN_ID or any(
                    not isinstance(value, str) or not value or len(value) > 1024 for value in
                    (task.get("id"), task.get("account"), execution.get("id"), execution.get("token"))):
            fail("invalid-request", "识别需要有效的后台任务执行凭据")
        platform = params.get("platform")
        if not isinstance(platform, str) or not platform.strip() or len(platform) > 80 or "\0" in platform:
            fail("invalid-request", "缺少聊天来源平台")
        return platform.strip(), task, execution

    def _call(self, context, system, user_prompt, images, parser, *, step, invalid_fallback=None):
        _, task, execution = context
        ctx = TaskContext(self.plugin, task, execution)
        with self.lock:
            if execution["id"] in self.plugin.contexts:
                fail("busy", "当前批次正在识别另一张画面")
            self.plugin.contexts[execution["id"]] = ctx
        try:
            if self.plugin.stopping.is_set():
                raise Cancelled()
            ctx.check()
            service = self.plugin.models.service_for("image")
            if service.get("supportsVision") is False:
                fail("vision-required", "当前图片模型未声明支持图像，请先在模型设置中核对模型能力")
            cap = service.get("maxOutputTokens")
            max_output = min(cap, 8192) if type(cap) is int and cap >= 16 else 8192
            # A format-only retry is bounded and never resends provider/transport failures.
            # Do not feed the rejected provider response back into the prompt or diagnostics.
            for attempt in range(2):
                if self.plugin.stopping.is_set():
                    raise Cancelled()
                ctx.check()
                response = self.plugin.models.generate(ctx, {
                    "system": system + (FORMAT_RETRY if attempt else ""), "prompt": user_prompt,
                    "images": images, "jsonMode": True, "thinking": False, "maxOutputTokens": max_output,
                }, step=step if not attempt else f"{step}.format-retry", role="image")
                if self.plugin.stopping.is_set():
                    raise Cancelled()
                ctx.check()
                try:
                    result = parser(response.get("text") if isinstance(response, dict) else None)
                    break
                except RpcError as exc:
                    if exc.code != "invalid-output":
                        raise
                    if attempt:
                        if invalid_fallback is None:
                            raise
                        result = invalid_fallback()
            if service.get("supportsVision") is not True:
                result["warnings"].append("vision_unconfirmed")
            return result
        except (Cancelled, ExecutionRevoked):
            fail("cancelled", "识别已停止，已完成画面仍保留")
        except (CoreError, ModelError) as exc:
            # Avoid logging provider output or carrying image/text content into diagnostics.
            messages = {"api-not-configured": "尚未配置主模型，请先在模型设置中保存连接，导入素材需支持读取图片",
                        "timeout": "当前画面识别超时，可以重试未完成部分",
                        "output-truncated": "当前画面的识别结果过长，可以缩小画面后重试",
                        "cancelled": "识别已停止，已完成画面仍保留"}
            fail(exc.code, messages.get(exc.code, "当前画面识别失败，可以检查模型设置后重试"))
        finally:
            with self.lock:
                if self.plugin.contexts.get(execution["id"]) is ctx:
                    self.plugin.contexts.pop(execution["id"], None)

    def recognize(self, params, context):
        validated = self._validate(params, context)
        image = image_payload(params.get("image"))
        previous = previous_rows(params.get("previous"))
        overlap = overlap_data(params.get("overlap"))
        result = self._call(validated, SYSTEM, prompt(validated[0], previous, overlap), [image],
                            parse_result, step="transcription")
        result["supportsTracking"] = True
        result["links"] = match_observations(previous, result["messages"], overlap)
        return result

    def review(self, params, context):
        validated = self._validate(params, context)
        frames = params.get("frames")
        target = params.get("target")
        if not isinstance(frames, list) or not 1 <= len(frames) <= 3 or not isinstance(target, dict):
            fail("invalid-request", "局部复核需要目标气泡和一至三张证据画面")
        metadata, images, ids = [], [], set()
        for frame in frames:
            if not isinstance(frame, dict):
                fail("invalid-request", "局部复核证据格式不正确")
            frame_id = frame.get("id")
            if not valid_id(frame_id) or frame_id in ids:
                fail("invalid-request", "局部复核证据编号不正确")
            info = {"id": frame_id}
            if frame.get("videoMs") is not None:
                video_ms = frame["videoMs"]
                if type(video_ms) not in (int, float) or not math.isfinite(video_ms) or not 0 <= video_ms <= 180_000:
                    fail("invalid-request", "局部复核证据时间不正确")
                info["videoMs"] = video_ms
            ids.add(frame_id)
            metadata.append(info)
            images.append(image_payload(frame.get("image")))
        if not valid_id(target.get("id")) or not valid_id(target.get("segmentId")):
            fail("invalid-request", "局部复核目标编号不正确")
        try:
            normalized = context_message(target)
        except RpcError:
            fail("invalid-request", "局部复核目标格式不正确")
        if normalized is None:
            fail("invalid-request", "局部复核目标不能是界面或日期条")
        normalized.update({"id": target["id"], "segmentId": target["segmentId"]})
        return self._call(validated, REVIEW_SYSTEM, review_prompt(validated[0], metadata, normalized), images,
                          lambda raw: parse_review(raw, ids), step="transcription.review",
                          invalid_fallback=lambda: unresolved("review_invalid_output"))


def valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and "\0" not in value


def overlap_data(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        fail("invalid-request", "相邻画面位移信息不正确")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        fail("invalid-request", "相邻画面位移信息不正确")
    if len(encoded) > 16_000:
        fail("invalid-request", "相邻画面位移信息过大")
    return value


def match(params):
    """Pure cross-frame matching RPC; model requests remain in recognize/review only."""
    if not isinstance(params, dict):
        fail("invalid-request", "缺少气泡关联参数")
    previous = previous_rows(params.get("previous"))
    current = params.get("current")
    if not isinstance(current, list) or len(current) > MAX_MESSAGES:
        fail("invalid-request", "当前画面候选格式不正确")
    rows = []
    try:
        for row in current:
            normalized = context_message(row)
            if normalized is None:
                # Keep indices aligned with the caller; a discarded UI observation cannot
                # become a link to another actual message.
                fail("invalid-request", "关联候选中包含界面或日期条")
            rows.append(normalized)
    except RpcError:
        fail("invalid-request", "当前画面候选格式不正确")
    return {"schema": 1, "links": match_observations(previous, rows, overlap_data(params.get("overlap")))}
