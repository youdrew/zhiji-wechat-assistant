"""The analysis window's run line, button and details, worded by this scheme for the host to show.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Ported from the window's former run panel (chatui/friend-run.js) into kyf.friend-analysis,
2026-10-13. The host owns start, stop and continue through Core tasks; it shows these texts.
"""
from __future__ import annotations

import time

STATE_TEXT = {"done": "已完成", "skipped": "已跳过", "failed": "失败", "stopped": "已停止", "running": "进行中",
              "pending": "等待"}
OPEN = ("queued", "running")


def tokens(value):
    if value >= 10000:
        return f"{value / 10000:.{0 if value >= 100000 else 1}f} 万"
    return str(round(value))


def duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时" + (f" {rest} 分钟" if rest else "")


def token_range(value):
    return f"约 {tokens(value[0])}–{tokens(value[1])} token" if value else "—"


def time_range(value):
    if not value:
        return "耗时暂无法估算"
    if value[1] <= 0:
        return "无需调用模型"
    return f"约 {duration(value[0])}" if value[0] == value[1] else f"约 {duration(value[0])}–{duration(value[1])}"


def estimate_line(estimate):
    if not estimate:
        return ""
    if not estimate.get("configured"):
        return estimate.get("error") or "需要配置 API 模型"
    recent = estimate.get("recent") if estimate.get("recent") and estimate["recent"]["tokens"][1] > 0 else None
    if not estimate.get("full"):
        return ("近期概述" + time_range(recent["seconds"]) + " · " if recent else "") + "完整分析待历史读取完成"
    if not estimate["full"]["seconds"] and (not recent or not recent["seconds"]):
        return "耗时暂无法估算"
    parts = ["近期概述" + time_range(recent["seconds"]) if recent else "", "完整分析" + time_range(estimate["full"]["seconds"])]
    return " · ".join(part for part in parts if part)


def progress_line(run):
    if run["state"] == "queued":
        return f"排队中 · 前面还有 {run['ahead']} 位" if run.get("ahead") else "排队中"
    step = next((item for item in run["steps"] if item["state"] == "running"), None)
    recent_done = next((item for item in run["steps"] if item["key"] == "recent"), {}).get("state") == "done"
    if step is None:
        return "分析中"
    parts = ["分析中" if step["key"] in ("history", "recent") else "历史分析中", step["title"]]
    if step.get("detail"):
        detail = step["detail"]
        for prefix in ("聊天分析 · ", "性格与三观证据 · "):
            if detail.startswith(prefix):
                detail = detail[len(prefix):]
        parts.append(detail)
    if recent_done and step["key"] not in ("history", "recent"):
        parts.append("近期概述已就绪")
    return " · ".join(parts)


def _date(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else ""


def view(status, estimate, history=None):
    """``{line, action, disabled, analyzed, details}`` for the host's run controls."""
    run = status.get("run")
    analyzed = bool(status.get("analyzed"))
    action, disabled = None, False
    if run and run["state"] in OPEN:
        line = progress_line(run)
    elif run and run["state"] in ("stopped", "interrupted", "error", "partial"):
        failed = [step["title"] for step in run["steps"] if step["state"] == "failed"]
        line = (f"部分完成：{'、'.join(failed)}未完成" if run["state"] == "partial" else
                "分析失败" + ("：" + run["error"] if run.get("error") else "") if run["state"] == "error" else
                "已取消，已保存的进度会保留" if run["state"] == "stopped" else "已中断，可继续")
        action = {"label": "重试" if run["state"] in ("partial", "error") else "继续", "continue": True}
    elif not analyzed:
        line, action = estimate_line(estimate), {"label": "开始分析", "mode": "update"}
    elif estimate and (estimate.get("needs") or {}).get("rebuild"):
        line = "分析规则或模型已变化，需要重新分析 · " + estimate_line(estimate)
        action = {"label": "重新分析", "mode": "update"}
    else:
        if history and (history.get("importing") or history.get("state") == "reading"):
            line = f"读取聊天历史 · 已读取 {history.get('readCount', 0)} 条"
        elif estimate and estimate.get("full") and estimate["full"]["tokens"][1] > 0:
            line = "有新内容 · " + estimate_line(estimate)
        elif run and run.get("finishedAtMs"):
            line = "已是最新 · 上次分析 " + _date(run["finishedAtMs"])
        else:
            line = "已是最新"
        action = {"label": "更新分析", "mode": "update"}
    if estimate is not None and estimate.get("configured") is False and not (run and run["state"] in OPEN):
        disabled, line = True, estimate_line(estimate)
    return {"line": line, "action": action, "disabled": disabled, "analyzed": analyzed,
            "active": bool(run and run["state"] in OPEN), "details": details(run, estimate)}


def details(run, estimate):
    sections = []
    if run:
        rows = [["状态", {"queued": "排队中", "running": "进行中", "done": "已完成", "partial": "部分完成", "stopped": "已取消",
                          "interrupted": "已中断，可继续", "error": "失败"}.get(run["state"], run["state"])],
                ["触发", "打开窗口时自动更新" if run.get("trigger") == "auto" else "手动开始"],
                ["本次消耗", f"输入 {tokens(run['usage']['inputTokens'])} · 输出 {tokens(run['usage']['outputTokens'])} token" +
                 ("（部分为估算）" if run["usage"]["estimated"] else "")]]
        if run.get("ahead"):
            rows.append(["排队", f"前面还有 {run['ahead']} 位联系人"])
        if run.get("startedAtMs"):
            rows.append(["开始时间", _date(run["startedAtMs"])])
        sections.append({"kind": "pairs", "rows": rows})
        steps = []
        for step in run["steps"]:
            meta = []
            if step.get("calls"):
                meta.append(f"模型调用 {step['calls']} 次")
                meta.append(f"输入 {tokens(step['inputTokens'])} · 输出 {tokens(step['outputTokens'])} token" +
                            ("（估算）" if step.get("estimated") else ""))
            if step.get("startedAtMs"):
                meta.append("耗时 " + duration(((step.get("finishedAtMs") or time.time() * 1000) - step["startedAtMs"]) / 1000))
            if step.get("usedOld"):
                meta.append("使用旧资料")
            steps.append({"title": step["title"], "state": STATE_TEXT.get(step["state"], step["state"]),
                          "warn": step["state"] == "failed", "detail": step.get("detail") or "",
                          "error": step.get("error") or "", "meta": meta})
        sections.append({"kind": "steps", "title": "步骤", "items": steps})
    if estimate and estimate.get("configured") and not (run and run["state"] in OPEN):
        rows = [[step["title"], f"{token_range(step['tokens'])} · {time_range(step['seconds'])}"
                 if step["tokens"][1] > 0 else "没有新内容，不调用模型"] for step in estimate.get("steps", [])]
        if not estimate.get("historyReady"):
            rows.append(["完整分析", "待历史读取完成后估算"])
        sections.append({"kind": "pairs", "title": "下次分析的预估", "collapsed": bool(run), "rows": rows,
                         "note": "按待处理的字数、分段数和固定提示词估算，不为预估调用模型；耗时依据同一模型的实测速度。"})
    sections.append({"kind": "note", "text": "展示处理对象和步骤，不包含模型内部的思考过程。取消后已完成的部分会保留，需要手动继续。"})
    return {"title": "分析详情", "sections": sections}
