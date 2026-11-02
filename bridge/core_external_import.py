"""Core background orchestration of the bundled external transcription plugin.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Core owns input files, checkpoints, review and publication. Only the plugin
defines recognition prompts and preprocessing parameters.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from core_external_media import (MAX_FILE_BYTES, MAX_MATERIALS, MAX_VIDEO_SECONDS,
                                 evidence_frames, frame_alignment, inspect_material, model_image)
from core_external_store import ExternalChatStore
from core_tasks import TaskError
from model_source import MAX_MODEL_TIMEOUT_MS

PLUGIN = "kyf.external-chat-import"
# This is a control-channel fallback, not a model deadline. Keep it above the
# largest supported per-request setting so slow prefill can finish and saving a
# shorter setting cannot cut off an already accepted recognition request.
RECOGNITION_RPC_TIMEOUT_SECONDS = MAX_MODEL_TIMEOUT_MS / 1000 + 120


class ExternalImportService:
    def __init__(self, core, root):
        self.core = core
        self.root = Path(root)
        self.store = ExternalChatStore(root)
        self.store.mark_interrupted()
        self.lock = threading.RLock()
        self.capacity = threading.Condition()
        self.processing = 0
        self.runs = {}
        self.closing = False
        (self.root / "scratch").mkdir(parents=True, exist_ok=True)

    def require_idle(self, batch_id):
        if self.closing:
            raise ValueError("应用正在关闭")
        if batch_id in self.runs:
            raise ValueError("请先停止整理，再修改或保存这一批资料")

    def status(self):
        manifest = self.core.registry.get(PLUGIN)
        dependency_error = self.core.registry.dependency_error(PLUGIN) if manifest else None
        model = {"available": False, "supportsVision": None, "destination": "", "model": "",
                 "message": "请先在模型设置中配置主模型，导入素材需支持读取图片"}
        try:
            services = self.core.model_services(PLUGIN).get("services", [])
            if services:
                selected = services[0]
                vision = selected.get("supportsVision")
                model.update(available=vision is not False, supportsVision=vision,
                             model=selected.get("actualModel") or selected.get("model", ""),
                             message="当前主模型未声明支持图片，请在模型设置中核对模型能力" if vision is False else
                             "主模型的图片能力尚未确认，将以实际识别结果为准" if vision is None else "当前主模型已声明支持图片")
            source_store = getattr(self.core.backend, "model_source_store", None)
            saved = source_store.saved_selection() if source_store is not None else None
            api = saved.get("api", {}) if isinstance(saved, dict) else {}
            url = urlsplit(api.get("baseUrl") or "")
            if url.hostname:
                model["destination"] = url.scheme + "://" + url.hostname + (f":{url.port}" if url.port else "")
        except Exception:
            model["message"] = "模型暂时不可用，请检查模型设置后重试"
        return {"plugin": {"id": PLUGIN, "name": manifest["name"] if manifest else "外部聊天导入",
                           "enabled": bool(manifest and self.core.registry.enabled(PLUGIN) and not dependency_error),
                           **({"dependencyError": dependency_error} if dependency_error else {})},
                "model": model, "limits": {"maxFileBytes": MAX_FILE_BYTES, "maxMaterials": MAX_MATERIALS,
                "maxVideoSeconds": MAX_VIDEO_SECONDS, "maxVideos": 1,
                "formats": ["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm"]}}

    def upload(self, batch_id, name, reader, length):
        if not isinstance(name, str) or not 1 <= len(name) <= 200 or any(c in name for c in '/\\:\x00') or any(ord(c) < 32 for c in name) or name in (".", ".."):
            raise ValueError("素材文件名不正确")
        with self.lock:
            self.require_idle(batch_id)
            batch = self.store.get_batch(batch_id)
            if len(batch["materials"]) >= MAX_MATERIALS:
                raise ValueError("每批最多 20 个素材，请新建批次")
            if batch["segments"]:
                raise ValueError("已开始识别的批次保留原素材顺序；新素材请追加为新批次")
            with tempfile.TemporaryDirectory(dir=self.root / "scratch") as folder:
                target = Path(folder) / name
                with target.open("xb") as stream:
                    remaining = length
                    while remaining:
                        chunk = reader.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("素材上传未完成，请重试")
                        stream.write(chunk)
                        remaining -= len(chunk)
                metadata = inspect_material(target)
                if metadata["kind"] == "video" and any(m["kind"] == "video" for m in batch["materials"]):
                    raise ValueError("首版每批最多一段录屏，其余录屏请分批导入")
                self.store.add_material(batch_id, target, metadata["kind"])
            return self.store.get_batch(batch_id)

    def start(self, batch_id, *, reprocess=False):
        with self.lock:
            self.require_idle(batch_id)
            batch = self.store.get_batch(batch_id)
            if batch.get("archived"):
                raise ValueError("这批任务已移出列表，请新建批次")
            if not batch["materials"]:
                raise ValueError("请先添加截图或录屏")
            if not all(m["available"] for m in batch["materials"]):
                raise ValueError("原素材已清理，无法重新抽取画面")
            if not self.core.registry.enabled(PLUGIN):
                raise ValueError("请先在插件设置中启用外部聊天导入")
            if reprocess:
                self.store.reset_recognition(batch_id)
            created = self.core.tasks.create(PLUGIN, kind="background", account="external-chat",
                user=batch["friendId"], title="外部聊天转录", params={"batchId": batch_id}, from_plugin=True)
            if "execution" not in created:
                self.store.set_batch_state(batch_id, "interrupted", "导入插件未能启动，请检查插件设置")
                return self.store.get_batch(batch_id)
            run = {**created, "cancel": threading.Event()}
            thread = threading.Thread(target=self._run, args=(batch_id, run), daemon=True, name="external-chat-import")
            run["thread"] = thread
            self.runs[batch_id] = run
            self.store.set_batch_state(batch_id, "queued")
            thread.start()
            return self.store.get_batch(batch_id)

    def _check(self, run):
        if run["cancel"].is_set() or self.closing:
            raise TaskError("execution-revoked", "任务已停止", 409)
        self.core.tasks.verify(run["execution"]["token"], PLUGIN, run["task"]["id"])

    def _claim_capacity(self, run):
        # Keep pending batches out of the gateway's bounded request queue. The
        # main model's per-model concurrency controls batch workers; the gateway
        # still applies its final model capacity and interactive-task priority.
        with self.capacity:
            while True:
                self._check(run)
                limit = self.core.main_concurrency()
                if self.processing < limit:
                    self.processing += 1
                    return
                # Poll so raising capacity takes effect without a restart. A
                # lower setting leaves active workers alone and delays new ones.
                self.capacity.wait(.25)

    def _image(self, segment_id, plan):
        options = plan.get("image", {})
        return model_image(self.store.evidence_path(segment_id),
            max_bytes=options.get("maxBytes", 2_621_440), max_width=options.get("maxWidth", 1600),
            min_width=options.get("minWidth", 0), format=options.get("format", "jpeg"),
            max_upscale=options.get("maxUpscale", 3))

    @staticmethod
    def _message(item):
        return {key: value for key, value in {**item, "dateLabel": item.get("timeLabel") or "",
            "time": None, "timePrecision": "partial" if item.get("timeLabel") else "unknown",
            "truncated": "truncated" in item.get("issues", [])}.items()
            if key in {"text", "sender", "kind", "issues", "bbox", "quote", "dateLabel",
                       "time", "timePrecision", "truncated"}}

    def _observations(self, segment_id):
        rows = self.store.segment_candidates(segment_id)
        # Each box belongs to this observation's source frame. A canonical row
        # may now display the complete box from a later frame instead.
        observed = []
        for row in rows:
            if row.get("ignored"):
                continue
            original = row.get("original", row)
            protected = row.get("savedMessageId") or row.get("reviewed") or "edited-unconfirmed" in row.get("issues", [])
            source = original if protected else row
            observed.append({**row, **{key: source.get(key) for key in
                ("text", "sender", "kind", "quote", "issues")}, "bbox": original.get("bbox"),
                "truncated": original.get("truncated", False)})
        return observed[-20:]

    def _overlap(self, previous, current):
        if previous["materialId"] != current["materialId"]:
            return {"reliable": False}
        # Long-image tiles have exact coordinates, including a shorter last tile.
        try:
            old_kind, old_top, old_bottom = previous["key"].split(":")
            new_kind, new_top, new_bottom = current["key"].split(":")
            if old_kind == new_kind == "image":
                old_top, old_bottom, new_top, new_bottom = map(int, (old_top, old_bottom, new_top, new_bottom))
                height = new_bottom - new_top
                if height > 0 and min(old_bottom, new_bottom) > max(old_top, new_top):
                    return {"reliable": True, "scaleY": (old_bottom - old_top) / height,
                            "offsetY": (old_top - new_top) / height, "confidence": 1}
        except (ValueError, KeyError):
            pass
        return frame_alignment(self.store.evidence_path(previous["id"]),
                               self.store.evidence_path(current["id"]))

    def _link(self, batch_id, segment, previous, current, links, run):
        allowed = {row["id"] for row in previous}
        positions = {row.get("order", index) for index, row in enumerate(current)}
        for link in links if isinstance(links, list) else []:
            if (not isinstance(link, dict) or link.get("previousId") not in allowed
                    or link.get("currentIndex") not in positions):
                continue
            self._check(run)
            try:
                with self.core.tasks.submission(run["execution"]["token"], PLUGIN, run["task"]["id"]):
                    self.store.apply_auto_links(batch_id, segment["id"], [link])
            except ValueError:
                # Protected edits or unresolved identity differences remain in
                # review. A failed proposed link must never discard a message.
                continue

    def _match_segments(self, batch_id, segments, overlaps, run):
        for previous, current in zip(segments, segments[1:]):
            self._check(run)
            if previous["materialId"] != current["materialId"]:
                continue
            key = (previous["id"], current["id"])
            if key not in overlaps:
                overlaps[key] = self._overlap(previous, current)
            overlap = overlaps[key]
            if not overlap.get("reliable"):
                continue
            old, new = self._observations(previous["id"]), self._observations(current["id"])
            try:
                result = self.core.supervisor.rpc(PLUGIN, "transcription.match",
                    {"previous": old, "current": new, "overlap": overlap},
                    {"task": run["task"], "execution": run["execution"]}, timeout=30)
            except Exception:
                self._check(run)
                # Older checkpoints can contain observations the current plugin
                # classifies as UI. Keep them for review instead of losing the
                # completed transcript because optional matching failed.
                continue
            if isinstance(result, dict) and result.get("schema") == 1:
                self._link(batch_id, current, old, new, result.get("links", []), run)

    def _review(self, batch_id, batch, segments, plan, run):
        options = plan["refinement"]
        limit = max(0, min(40, int(options.get("maxReviews", 40))))
        frame_limit = max(1, min(3, int(options.get("maxFrames", 3))))
        eligible = {"truncated", "unclear_text", "unknown_sender", "recognition-difference"}
        candidates = [row for row in self.store.get_batch(batch_id)["candidates"]
            if not (row.get("savedMessageId") or row.get("reviewed") or row.get("ignored")
                    or row.get("autoReviewAttempted") or row.get("duplicateDecision")
                    or "edited-unconfirmed" in row.get("issues", [])
                    or row.get("duplicateMessageId") or row.get("duplicateCandidateId"))
            and (eligible.intersection(row.get("issues", [])) or row.get("truncated"))][:limit]
        by_id = {segment["id"]: segment for segment in segments}
        for index, target in enumerate(candidates):
            self._check(run)
            owner = by_id[target["segmentId"]]
            siblings = [segment for segment in segments if segment["materialId"] == owner["materialId"]]
            # Target and linked evidence first, then nearest neighboring frames.
            ids = [owner["id"], target.get("evidenceSegmentId", owner["id"]), *target.get("evidenceSegmentIds", [])]
            ids.extend(s["id"] for s in sorted(siblings, key=lambda s: abs(s["order"] - owner["order"])))
            selected = list(dict.fromkeys(ids))[:frame_limit]
            selected = [sid for sid in selected if sid in by_id and by_id[sid]["materialId"] == owner["materialId"]]
            self.core.tasks.progress(run["execution"]["token"], PLUGIN, run["task"]["id"],
                {"done": index, "total": len(candidates), "label": "后台补看疑点"})
            frames = [{"id": sid, "image": self._image(sid, plan), "videoMs": by_id[sid].get("videoMs")} for sid in selected]
            # The target box is expressed in its original frame, even after a
            # previous automatic correction chose a clearer evidence frame.
            target = {**target, "bbox": target.get("original", target).get("bbox")}
            applied = {"resolved": False}
            try:
                result = self.core.supervisor.rpc(PLUGIN, "transcription.review",
                    {"platform": batch["source"]["platform"], "target": target, "frames": frames},
                    {"task": run["task"], "execution": run["execution"]}, timeout=RECOGNITION_RPC_TIMEOUT_SECONDS)
                self._check(run)
                if (isinstance(result, dict) and result.get("schema") == 1 and result.get("resolved") is True
                        and isinstance(result.get("message"), dict)
                        and isinstance(result.get("evidenceSegmentIds"), list)
                        and result["evidenceSegmentIds"]
                        and set(result["evidenceSegmentIds"]).issubset(selected)
                        and result.get("preferredEvidenceSegmentId") in result["evidenceSegmentIds"]):
                    applied = {"resolved": True, "message": self._message(result["message"]),
                        "evidenceSegmentIds": result["evidenceSegmentIds"],
                        "preferredEvidenceSegmentId": result["preferredEvidenceSegmentId"]}
            except Exception:
                self._check(run)
                # Reread failure leaves the primary transcript intact for user
                # review; it must not fail already completed recognition.
            with self.core.tasks.submission(run["execution"]["token"], PLUGIN, run["task"]["id"]):
                try:
                    self.store.apply_auto_review(batch_id, target["id"], applied)
                except ValueError:
                    self.store.apply_auto_review(batch_id, target["id"], {"resolved": False})

    def _run(self, batch_id, run):
        token, task_id = run["execution"]["token"], run["task"]["id"]
        acquired = False
        active_segment = None
        try:
            self._claim_capacity(run)
            acquired = True
            self._check(run)
            self.store.set_batch_state(batch_id, "running")
            plan = self.core.supervisor.rpc(PLUGIN, "transcription.plan", {}, {}, timeout=30)
            if not isinstance(plan, dict) or plan.get("schema") != 1:
                raise ValueError("导入插件返回的处理方案不受支持")
            batch = self.store.get_batch(batch_id)
            segments = []
            for material in batch["materials"]:
                with tempfile.TemporaryDirectory(dir=self.root / "scratch") as folder:
                    for frame in evidence_frames(self.store.material_path(material["id"]), material["kind"], plan,
                                                 folder, direction=batch.get("direction", "forward"),
                                                 check=lambda: self._check(run)):
                        with self.core.tasks.submission(token, PLUGIN, task_id):
                            segment = self.store.add_segment(batch_id, material["id"], frame["key"],
                                material["order"] * 100000 + frame["order"], frame["path"], frame["videoMs"])
                        segments.append(segment)
            total = len(segments)
            if not total:
                raise ValueError("素材中没有可读取的画面")
            done = sum(s["state"] == "done" for s in segments)
            enhanced = isinstance(plan.get("refinement"), dict)
            overlaps = {}
            for index, segment in enumerate(segments):
                self._check(run)
                if segment["state"] == "done":
                    continue
                active_segment = segment["id"]
                self.store.segment_state(active_segment, "running")
                self.core.tasks.progress(token, PLUGIN, task_id, {"done": done, "total": total, "label": "识别聊天画面"})
                image = self._image(active_segment, plan)
                previous, overlap = [], {"reliable": False}
                if enhanced and index and segments[index - 1]["materialId"] == segment["materialId"]:
                    previous = self._observations(segments[index - 1]["id"])
                    overlap = self._overlap(segments[index - 1], segment)
                    overlaps[(segments[index - 1]["id"], segment["id"])] = overlap
                params = {"image": image, "platform": batch["source"]["platform"]}
                if enhanced:
                    params.update(previous=previous, overlap=overlap)
                result = self.core.supervisor.rpc(PLUGIN, "transcription.recognize",
                    params,
                    {"task": run["task"], "execution": run["execution"]},
                    timeout=RECOGNITION_RPC_TIMEOUT_SECONDS)
                if not isinstance(result, dict) or result.get("schema") != 1 or not isinstance(result.get("messages"), list):
                    raise ValueError("识别结果格式不正确，请重试这一画面")
                candidates = []
                for item in result["messages"]:
                    if not isinstance(item, dict):
                        raise ValueError("识别结果包含无效记录")
                    candidates.append(self._message(item))
                if not candidates and not (enhanced and result.get("uiOnly") is True):
                    candidates.append({"text": "", "sender": "unknown", "issues": ["no_visible_messages"], "kind": "system"})
                with self.core.tasks.submission(token, PLUGIN, task_id):
                    self.store.set_segment_warnings(active_segment, result.get("warnings", []))
                    distinct = enhanced and result.get("supportsTracking") is True and (
                        (not index or segments[index - 1]["materialId"] != segment["materialId"])
                        or overlap.get("reliable") is True and bool(previous)
                        and len(self.store.segment_candidates(segments[index - 1]["id"])) <= 20
                        and all(row.get("bbox") for row in [*previous, *candidates]))
                    self.store.submit_segment(batch_id, active_segment, candidates, evidence_distinct=bool(distinct))
                if enhanced and overlap.get("reliable"):
                    current = self.store.segment_candidates(active_segment)
                    self._link(batch_id, segment, previous, current, result.get("links", []), run)
                done += 1
                active_segment = None
            if enhanced:
                self._match_segments(batch_id, segments, overlaps, run)
                self._review(batch_id, batch, segments, plan, run)
                self._match_segments(batch_id, segments, overlaps, run)
            with self.core.tasks.submission(token, PLUGIN, task_id):
                self.store.set_batch_state(batch_id, "review")
            self.core.tasks.finish(token, PLUGIN, task_id, "done")
        except Exception as exc:
            cancelled = run["cancel"].is_set() or self.closing or isinstance(exc, TaskError) and exc.code == "execution-revoked"
            # A stop from the global task controls or plugin settings revokes the
            # token without setting this worker's local event. The gateway may
            # report that cancellation as a provider/RPC error, so check authority.
            if not cancelled:
                try:
                    self.core.tasks.verify(token, PLUGIN, task_id)
                except TaskError:
                    cancelled = True
            message = ("任务已中断，可继续未完成部分" if cancelled else
                       str(exc)[:300] if isinstance(exc, ValueError) else getattr(exc, "message", "识别未完成，请检查模型或素材后继续"))
            if active_segment:
                self.store.segment_state(active_segment, "interrupted" if cancelled else "failed", message)
            self.store.set_batch_state(batch_id, "interrupted" if cancelled else "failed", message)
            try:
                if cancelled:
                    task = self.core.tasks.task(task_id)
                    if task and task["state"] in ("running", "stopping", "queued"):
                        self.core.tasks._close(task_id, "stopped")
                else:
                    self.core.tasks.finish(token, PLUGIN, task_id, "failed", {"code": "import-failed", "message": message})
            except TaskError:
                pass
        finally:
            if acquired:
                with self.capacity:
                    self.processing -= 1
                    self.capacity.notify_all()
            with self.lock:
                self.runs.pop(batch_id, None)

    def stop(self, batch_id):
        with self.lock:
            run = self.runs.get(batch_id)
            if run is not None:
                run["cancel"].set()
                self.core.tasks.stop(run["task"]["id"])
                self.store.set_batch_state(batch_id, "stopping")
            return self.store.get_batch(batch_id)

    def continue_task(self, task_id):
        task = self.core.tasks.task(task_id)
        if task is None or task.get("plugin") != PLUGIN:
            raise TaskError("not-found", "导入任务不存在", 404)
        if task.get("state") not in ("interrupted", "stopped", "failed", "partial"):
            raise TaskError("invalid-state", "该任务当前不能继续", 409)
        batch_id = task.get("params", {}).get("batchId")
        if not isinstance(batch_id, str):
            raise TaskError("invalid-request", "该任务缺少导入批次")
        batch = self.start(batch_id)
        with self.lock:
            running = self.runs.get(batch_id)
            return {"task": running["task"] if running else task, "batch": batch}

    def close(self):
        with self.lock:
            self.closing = True
            runs = list(self.runs.values())
            for run in runs:
                run["cancel"].set()
                self.core.tasks.stop(run["task"]["id"])
        for run in runs:
            run["thread"].join(2)
        # A late RPC response cannot publish after revocation. Its daemon worker
        # still needs the connection to record interruption before process exit.
        if not any(run["thread"].is_alive() for run in runs):
            self.store.close()
