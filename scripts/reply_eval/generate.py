"""表达评测生成脚本（docs/loop/reply-eval 协议 4.1–4.2）。绝不发送任何微信消息。

走生产路径：POST http://127.0.0.1:<port>/api/plugins/rpc（端口读
.local/real-client-runtime/selected-port.json），method=self.reply，账号用 accounts.json
的 Core 账号名；随后轮询 self.replyStatus 直到完成/失败（单次尝试上限 --timeout 300 秒）。
每例请求带防泄漏截断（协议 3.3）：beforeTimeMs=该例 firstReplyMs、
excludeSampleIds=[该例 sampleId]、excludeMessageIds=该例 evidence 的全部消息 id。
不传 depth，生产代码自动使用插件设置里用户当前的 replyDepth；实际值从 job 状态里读取
（读不到记 null）——插件没有单独暴露设置读取 RPC。

串行执行（服务端每账号只有一个 reply job 槽，并发会互相盯错 job；协议要求并发 ≤2，
串行合规）；单例失败（超时/错误/invalid）重试最多 2 次，仍失败记 failed=true；任何单例
异常都不中断整个批次，转为 failed 记录继续下一例。开始前查询 self.analysisStatus，
自我剖析运行中拒绝生成（--force 可越过，仅供确认安全后使用）；生成中每约 30 秒抽查
一次，发现分析开始运行立即中止整个脚本。

逐例落盘：每完成一例立即 append 写入 generated.jsonl 并 flush；启动时若该文件已存在，
读出其中 caseId 集合并跳过（断点续跑，追加模式）；metrics.json 在结束时**从
generated.jsonl 全量重读**所有记录重新计算（新跑/续跑/部分失败均一致；空行与损坏行
跳过并计数），保证与实际落盘记录相符。若 round 目录存在评分方产物 scores.jsonl
（每行 {caseId, A..D, total?, failed, unpredictable, failure_mode, note}），另附
failure_mode 分布与各维度均分（均分排除 failed 与 unpredictable；分布单独统计）；
不存在时该字段为 null 并注明原因。

用法：
  python generate.py --set dev --round 00a              # 全量 50 例
  python generate.py --set dev --round smoke --limit 3  # 冒烟：只跑前 3 例
  python generate.py --set holdout --round 3 --seed 20261031  # seed 仅记录；抽样在 build_set.py

输出 .local/reply-eval/round-NN/generated.jsonl（caseId、context、real、generated、
retrievedSampleIds、depth、commit、elapsedMs、failed、generationPath="rpc"，另附
attempts/error）与 metrics.json（协议 4.2 的确定性指标，纯 stdlib）。生成内容只进
.local/reply-eval/，绝不写入 git。

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import argparse
import collections
import json
import math
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA_DIR, PORT_JSON, load_account, parse_context  # noqa: E402

POLL_SECONDS = 2.0
ANALYSIS_CHECK_POLLS = 15  # 轮询 replyStatus 时每 N 次抽查一次 self.analysisStatus（约 30 秒）
MAX_ATTEMPTS = 3  # 首次 + 重试 2 次（协议 4.1）
HABIT_TONE = re.compile(r"哈哈+|嗯+|啊+|吧|呢|哦|噢|呗|啦|呀|哎|诶|唉|嘛|咯|嘿|emm+|hhh+")
HABIT_ENDING = "。！？…，、～~？！.,;；"
HABIT_STICKER = re.compile(r"\[[^\[\]]{1,12}\]")


def rpc(base_url, method, params, account, timeout=60):
    body = json.dumps({"plugin": "kyf.self-analysis", "method": method, "params": params,
                       "account": account}).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/api/plugins/rpc", data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read() or "null")
        # /api/plugins/rpc 成功响应统一为 {"result": ...}（bridge/core_http.py），解包后才是方法返回值。
        return body.get("result") if isinstance(body, dict) and set(body) == {"result"} else body
    except urllib.error.HTTPError as exc:
        detail = exc.read() or b"{}"
        try:
            message = json.loads(detail).get("message", str(exc.code))
        except ValueError:
            message = str(exc.code)
        raise RuntimeError(f"RPC {method} 失败：{message}") from None


def generate_once(base_url, account, case, timeout):
    """一次尝试：启动 self.reply 并轮询到终态。返回 (payload, error)；error 为 None 表示成功。"""
    started = time.monotonic()
    job = rpc(base_url, "self.reply", {"messages": case["context"],
                                       "beforeTimeMs": case["firstReplyMs"],
                                       "excludeSampleIds": [case["sampleId"]],
                                       "excludeMessageIds": [item["id"] for item in case["evidence"]]},
              account)
    job_id = job.get("id")
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        polls += 1
        status = rpc(base_url, "self.replyStatus", {}, account)
        state = status.get("state")
        if state == "idle":
            return None, f"job {job_id} 丢失（状态 idle）"
        if job_id and status.get("id") != job_id:
            # replyStatus 返回最近一个 job：提交后可能先轮到上一个 job 的终态，等本例 job 出现。
            if time.monotonic() > deadline:
                return None, f"超时（>{timeout}s）"
            time.sleep(POLL_SECONDS)
            continue
        if state in ("done", "error", "cancelled"):
            break
        if polls % ANALYSIS_CHECK_POLLS == 0 and (rpc(base_url, "self.analysisStatus", {}, account)
                                                  or {}).get("state") == "running":
            raise SystemExit("自我剖析开始运行，按协议 4.1 立即中止生成；确认安全后可重跑")
        if time.monotonic() > deadline:
            return None, f"超时（>{timeout}s）"
        time.sleep(POLL_SECONDS)
    if status.get("state") != "done":
        return None, status.get("error") or status.get("state") or "unknown"
    messages = (status.get("result") or {}).get("messages")
    if not isinstance(messages, list) or not messages:
        return None, "invalid-output"
    return {"generated": messages, "retrievedSampleIds": [row["id"] for row in status.get("sources") or []],
            "depth": status.get("depth"), "singleMs": round((time.monotonic() - started) * 1000)}, None


def run_case(base_url, account, case, timeout):
    attempts, payload, error = 0, None, None
    started = time.monotonic()
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        if (rpc(base_url, "self.analysisStatus", {}, account) or {}).get("state") == "running":
            raise SystemExit("自我剖析开始运行，按协议 4.1 立即中止生成；确认安全后可重跑")
        payload, error = generate_once(base_url, account, case, timeout)
        if error is None:
            break
    record = {"caseId": case["caseId"], "context": case["context"], "real": case["reply"],
              "commit": git_commit(), "failed": error is not None, "attempts": attempts,
              "generationPath": "rpc", "elapsedMs": round((time.monotonic() - started) * 1000)}
    if payload is None:
        record.update(generated=None, retrievedSampleIds=[], depth=None, error=error)
    else:
        record.update(payload, error=None)
    return record


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parents[2]),
                              capture_output=True, text=True, check=True, encoding="utf-8").stdout.strip()
    except Exception:
        return None


def _chars(text):
    return len(re.sub(r"\s+", "", text))


def _bigrams(text):
    return {text[index:index + 2] for index in range(len(text) - 1)}


def bigram_f1(generated, real):
    gen, reference = _bigrams(generated), _bigrams(real)
    if not reference:
        return 1.0 if not gen else 0.0
    if not gen:
        return 0.0
    overlap = len(gen & reference)
    precision, recall = overlap / len(gen), overlap / len(reference)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def habit_flags(text):
    stripped = text.rstrip()
    return {"tone": bool(HABIT_TONE.search(text)),
            "ending": bool(stripped) and stripped[-1] in HABIT_ENDING,
            "sticker": bool(HABIT_STICKER.search(text))}


def load_jsonl(path):
    """逐行解析 JSONL：跳过空行、行首 BOM 与损坏行（返回记录列表和损坏行数）。
    历史上 append 落盘可能出现编码/BOM 问题（如 round-holdout-00a），这里容错而不是崩溃。"""
    records, damaged = [], 0
    if not path.is_file():
        return records, damaged
    text = path.read_bytes().decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            damaged += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            damaged += 1
    return records, damaged


def compute_metrics(records):
    valid = [record for record in records if isinstance(record, dict) and "failed" in record]
    done = [record for record in valid if not record["failed"]
            and isinstance(record.get("generated"), list) and isinstance(record.get("real"), str)]
    if not valid:
        return {"cases": len(records), "failed": len(records), "failureRate": 1.0, "note": "无有效记录"}
    if not done:
        return {"cases": len(valid), "failed": len(valid), "failureRate": 1.0, "note": "全部失败"}
    lengths, count_diffs, f1s, habit_hits = [], [], [], collections.Counter()
    overlong, undershoot = 0, 0
    for record in done:
        joined = "".join(record["generated"])
        real_chars, gen_chars = max(_chars(record["real"]), 1), max(_chars(joined), 1)
        lengths.append(abs(math.log(gen_chars / real_chars)))
        count_diffs.append(abs(len(record["generated"]) - len(record["real"].splitlines())))
        overlong += int(gen_chars > 2 * real_chars)
        undershoot += int(gen_chars < real_chars / 2)
        f1s.append(bigram_f1(re.sub(r"\s+", "", joined), re.sub(r"\s+", "", record["real"])))
        real_habits, gen_habits = habit_flags(record["real"]), habit_flags(joined)
        for key, matches in real_habits.items():
            habit_hits[key] += int(matches == gen_habits[key])
    return {"cases": len(records), "failed": len(records) - len(done),
            "failureRate": round((len(records) - len(done)) / len(records), 4),
            "countDiffMean": round(statistics.mean(count_diffs), 3),
            "lengthRatioMean": round(statistics.mean(lengths), 4),
            "overlongRate": round(overlong / len(done), 4),
            "undershootRate": round(undershoot / len(done), 4),
            "bigramF1Mean": round(statistics.mean(f1s), 4),
            "habitMatch": {key: round(value / len(done), 4) for key, value in sorted(habit_hits.items())},
            "habitMatchMean": round(statistics.mean([habit_hits[key] / len(done) for key in sorted(habit_hits)]), 4),
            "elapsedMsMean": round(statistics.mean(record.get("elapsedMs") or 0 for record in valid), 1),
            "depthCounts": dict(collections.Counter(str(record.get("depth")) for record in valid))}


SCORE_DIMS = ("A", "B", "C", "D", "total")


def summarize_scores(score_rows):
    """评分方 scores.jsonl 汇总：failure_mode 分布（单独统计，含 unpredictable 行）
    与各维度均分（排除 failed 与 unpredictable 的行）。total 缺失时按 A+B+C+D 合成。"""
    def total_of(row):
        if isinstance(row.get("total"), (int, float)):
            return row["total"]
        parts = [row[dim] for dim in ("A", "B", "C", "D") if isinstance(row.get(dim), (int, float))]
        return sum(parts) if len(parts) == 4 else None

    usable = [row for row in score_rows if not row.get("failed") and not row.get("unpredictable")]
    means = {}
    for dim in SCORE_DIMS:
        if dim == "total":
            values = [total for total in (total_of(row) for row in usable) if total is not None]
        else:
            values = [row[dim] for row in usable if isinstance(row.get(dim), (int, float))]
        means[dim] = round(statistics.mean(values), 4) if values else None
    modes = collections.Counter(str(row["failure_mode"]) for row in score_rows
                                if row.get("failure_mode") not in (None, ""))
    return {"scoredCases": len(score_rows),
            "unpredictableCount": sum(1 for row in score_rows if row.get("unpredictable")),
            "failedCount": sum(1 for row in score_rows if row.get("failed")),
            "dimensionMeans": means,
            "failureModes": dict(sorted(modes.items()))}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="表达评测生成（走生产 RPC；绝不发送微信消息）。"
                                                     "自我剖析运行中会拒绝启动；样本原文只进 .local/reply-eval/。")
    parser.add_argument("--set", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--round", required=True, help="轮次名（如 00a、00b、smoke），输出 round-<名>/")
    parser.add_argument("--seed", type=int, default=None, help="仅记录进 metrics（抽样在 build_set.py）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 例（冒烟用）")
    parser.add_argument("--base-url", default=None, help="覆盖 http://127.0.0.1:<selected-port>")
    parser.add_argument("--account", default=None)
    parser.add_argument("--timeout", type=int, default=300, help="单次尝试轮询上限秒数")
    parser.add_argument("--force", action="store_true", help="self.analysisStatus 显示运行中时仍继续")
    args = parser.parse_args()

    set_path = DATA_DIR / "sets" / f"{args.set}.jsonl"
    if not set_path.is_file():
        raise SystemExit(f"缺少 {set_path}，请先用 build_set.py 抽样")
    cases = [json.loads(line) for line in set_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    round_dir = DATA_DIR / f"round-{args.round}"
    round_dir.mkdir(parents=True, exist_ok=True)
    out_path = round_dir / "generated.jsonl"

    def append_record(record):
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    records, damaged_resume = [], 0
    if out_path.is_file():  # 断点续跑：读出已落盘的记录，跳过这些 caseId（追加模式）
        records, damaged_resume = load_jsonl(out_path)
    if damaged_resume:
        print(f"警告：{out_path.name} 有 {damaged_resume} 行损坏，已跳过", flush=True)
    done_ids = {record.get("caseId") for record in records}

    prepared = []
    invalid_this_run = 0
    for case in cases:
        if case["caseId"] in done_ids:
            continue
        context = parse_context(case["input"])
        if context is None:
            record = {"caseId": case["caseId"], "context": None, "real": case["reply"], "commit": git_commit(),
                      "failed": True, "attempts": 0, "generationPath": "rpc", "elapsedMs": 0,
                      "generated": None, "retrievedSampleIds": [], "depth": None, "error": "context-invalid"}
            records.append(record)
            append_record(record)
            invalid_this_run += 1
            continue
        prepared.append({**case, "context": context})
    if args.limit:
        prepared = prepared[:max(args.limit - invalid_this_run, 0)]
    entry = load_account(args.account)
    base_url = args.base_url or f"http://127.0.0.1:{json.loads(PORT_JSON.read_text(encoding='utf-8'))['port']}"

    status = rpc(base_url, "self.analysisStatus", {}, entry["account"])
    if not args.force and (status or {}).get("state") == "running":
        raise SystemExit("自我剖析正在运行，按协议 4.1 不启动评测生成；确认安全后再跑")

    for case in prepared:  # 串行：服务端每账号单 reply job 槽，并发会互相盯错 job
        started = time.monotonic()
        try:
            record = run_case(base_url, entry["account"], case, args.timeout)
        except SystemExit:
            raise  # 分析守卫等主动中止仍然整体退出
        except Exception as exc:  # 单例异常不中断：转 failed 记录，继续下一例
            record = {"caseId": case["caseId"], "context": case["context"], "real": case["reply"],
                      "commit": git_commit(), "failed": True, "attempts": 0, "generationPath": "rpc",
                      "elapsedMs": round((time.monotonic() - started) * 1000),
                      "generated": None, "retrievedSampleIds": [], "depth": None, "error": f"exception: {exc}"}
        records.append(record)
        append_record(record)  # 逐例落盘：立即 append 并 flush
        failed = sum(1 for item in records if item["failed"])
        print(f"[{len(records)}/{len(cases)}] {record['caseId']} "
              + (f"失败: {record.get('error')}" if record["failed"] else "完成")
              + f"（累计失败 {failed}）", flush=True)

    # metrics 从落盘记录全量重读重算：无论新跑、续跑还是部分失败，metrics 都与
    # generated.jsonl 的实际内容一致（续跑补例后不再出现 metrics 缺例）。
    final_records, damaged_final = load_jsonl(out_path)
    metrics = compute_metrics(final_records)
    if damaged_final:
        metrics["damagedLines"] = damaged_final
    metrics.update({"round": args.round, "set": args.set, "seed": args.seed, "generationPath": "rpc",
                    "commit": git_commit(), "account": entry["account"], "baseUrl": base_url})

    scores_path = round_dir / "scores.jsonl"  # 评分方产物；存在才汇总 failure_mode
    if scores_path.is_file():
        score_rows, damaged_scores = load_jsonl(scores_path)
        metrics["scores"] = summarize_scores(score_rows) if score_rows else None
        if damaged_scores:
            metrics["scoresDamagedLines"] = damaged_scores
    else:
        metrics["scores"] = None
        metrics["scoresNote"] = "评分方 scores.jsonl 尚不存在，跳过 failure_mode 汇总"
    (round_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
