"""好友模拟对话回测（docs/friend-simulation-requirements.md §7；沿用 docs/loop/reply-eval 协议，视角换成好友）。

绝不发送任何微信消息。只在开发环境运行，用来调提示词并确定“资料不足”的阈值。

留出集：取这位好友最近 N 轮真实往来——“我”连发的一块文字消息为一轮，好友在 24 小时内
接上的一块是真实回复（与聊天分析、回复率的口径一致），没接上就是“没有回复”。插件镜像
（.local/plugin-data/kyf.friend-analysis/accounts/<sha256(account)>/analysis.sqlite3）
**只读打开**。

生成走生产路径：POST http://127.0.0.1:<port>/api/plugins/rpc（端口读
.local/real-client-runtime/selected-port.json），plugin=kyf.friend-analysis，
context 带账号与好友。每例：page.simulationReset → page.simulationSend
{messages, beforeTimeMs=这一轮第一条消息的时间} → 轮询 page.simulationStatus。
beforeTimeMs 让样例、档案统计与近况原文都截到这一轮之前，这一轮的样例自然被排除；
档案第 3–4 层（性格与三观、关系记忆与概况）是现在的结果、无法按时间截断，回测时整层省略
（逐例与 metrics.json 的 omittedLayers 标明），所以回测只衡量 0–2 层加样例的效果。
带 beforeTimeMs 的请求会在状态里附 debug（stance、条数上限、回复概率），界面请求没有。
注意：回测会清空运行中软件里这位好友的模拟对话会话。

串行执行；单例失败重试最多 2 次，仍失败记 failed=true。开始前与每例之前查询
scheme.status，这位好友的知己分析在跑时不启动（--force 越过）。

输出（含真实聊天，只进 .local/，绝不进 git）：
  .local/reply-eval/friend-sim/sets/<user 摘要>.jsonl     留出集（已存在则复用，--rebuild 重抽）
  .local/reply-eval/friend-sim/round-<名>/generated.jsonl 逐例结果，断点续跑
  .local/reply-eval/friend-sim/round-<名>/metrics.json   确定性指标：回不回的一致率与回复率
      校准、stance 与真实回应方式（聊天分析标签）的一致率、条数与长度误差、二元组 F1、习惯匹配
  .local/reply-eval/friend-sim/round-<名>/blind.jsonl    盲评材料：真实与模拟回复随机记为 A/B
  .local/reply-eval/friend-sim/round-<名>/blind-key.json 盲评答案（评分方评完再看）

用法：
  python friend_sim_backtest.py --user wxid_xxx --round 00a
  python friend_sim_backtest.py --user wxid_xxx --round smoke --limit 3

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import argparse
import collections
import hashlib
import json
import math
import random
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import DATA_DIR, PORT_JSON, REPO_ROOT, load_account, open_ro  # noqa: E402
from generate import bigram_f1, git_commit, habit_flags, load_jsonl  # noqa: E402

for path in (REPO_ROOT / "plugin-sdk" / "python", REPO_ROOT / "plugins" / "kyf.friend-analysis" / "backend"):
    sys.path.insert(0, str(path))
import friend_chat  # noqa: E402  (block pairing and readable text, same rules as the plugin)

PLUGIN = "kyf.friend-analysis"
OUT_DIR = DATA_DIR / "friend-sim"
POLL_SECONDS = 1.5
MAX_ATTEMPTS = 3
MAX_SEND = 6
TURN_CHARS = 1000
DEFAULT_ROUNDS = 30


def rpc(base_url, method, params, account, user, timeout=120):
    import urllib.error
    import urllib.request
    body = json.dumps({"plugin": PLUGIN, "method": method, "params": params, "account": account,
                       "user": user}).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/api/plugins/rpc", data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read() or "null")
        return value.get("result") if isinstance(value, dict) and set(value) == {"result"} else value
    except urllib.error.HTTPError as exc:
        detail = exc.read() or b"{}"
        try:
            message = json.loads(detail).get("message", str(exc.code))
        except ValueError:
            message = str(exc.code)
        raise RuntimeError(f"RPC {method} 失败：{message}") from None


def discover_plugin_db(entry, override=None):
    if override:
        return Path(override)
    digest = hashlib.sha256(entry["account"].encode("utf-8")).hexdigest()
    path = REPO_ROOT / ".local" / "plugin-data" / PLUGIN / "accounts" / digest / "analysis.sqlite3"
    if not path.is_file():
        raise SystemExit(f"找不到 {path}，请先在软件里打开这位好友的知己分析，或用 --db 指定")
    return path


def _case_id(user, block_id):
    return hashlib.sha256(json.dumps([user, block_id], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def build_set(db_path, user, rounds):
    """The latest ``rounds`` of my text blocks with the friend's real answer (or none) from the mirror."""
    connection = open_ro(db_path)
    connection.row_factory = lambda cursor, row: {column[0]: row[index] for index, column in
                                                  enumerate(cursor.description)}
    try:
        context = connection.execute("SELECT * FROM context WHERE user=?", (user,)).fetchone()
        if not context:
            raise SystemExit("插件镜像里没有这位好友的聊天，请先在软件里打开一次")
        rows = connection.execute("SELECT * FROM context_messages WHERE user=? AND seq<=? ORDER BY seq",
                                  (user, context["read_count"])).fetchall()
        labels = {}
        scan = connection.execute("SELECT generation FROM friend_scans WHERE user=? AND step='chat' AND published=1",
                                  (user,)).fetchone()
        if scan:
            for row in connection.execute("SELECT block_id,intent,response,invite FROM friend_chat_blocks "
                                          "WHERE user=? AND generation=?", (user, scan["generation"])):
                labels[row["block_id"]] = row
    finally:
        connection.close()
    found = friend_chat.blocks(rows)
    answers = {block["reply_to"]: block for block in found if block["reply_to"] is not None}
    latest, now = max((row["time_ms"] or 0 for row in rows), default=0), int(time.time() * 1000)
    cases = []
    for block in found:
        if block["side"] != "self" or block["ambiguous"]:
            continue
        if block["id"] not in answers and max(latest, now) - block["last_ms"] < friend_chat.REPLY_WINDOW_MS:
            continue  # the friend may still answer this one
        mine = [text for text in (friend_chat.readable_text(row) for row in block["rows"]) if text]
        mine = [text[:TURN_CHARS] for text in mine][-MAX_SEND:]
        if not mine:
            continue
        answer = answers.get(block["id"])
        real = [] if answer is None else [friend_chat.readable_text(row) or "[媒体或非文字消息]" for row in answer["rows"]]
        label = labels.get(answer["id"]) if answer else None
        cases.append({"caseId": _case_id(user, block["id"]), "user": user, "blockId": block["id"],
                      "messages": mine, "beforeTimeMs": block["first_ms"], "replied": answer is not None,
                      "real": real, "realResponse": label and label["response"],
                      "myIntent": (labels.get(block["id"]) or {}).get("intent"),
                      "invite": label and label["invite"]})
    return cases[-rounds:]


def generate_once(base_url, account, case, timeout):
    rpc(base_url, "page.simulationReset", {}, account, case["user"])
    started = time.monotonic()
    request_id = rpc(base_url, "page.simulationSend", {"messages": case["messages"],
                                                       "beforeTimeMs": case["beforeTimeMs"]},
                     account, case["user"])["requestId"]
    deadline = started + timeout
    try:
        while True:
            status = rpc(base_url, "page.simulationStatus", {"requestId": request_id}, account, case["user"])
            if status["state"] not in ("queued", "running"):
                break
            if time.monotonic() > deadline:
                rpc(base_url, "page.simulationCancel", {"requestId": request_id}, account, case["user"])
                return None, f"超时（>{timeout}s）"
            time.sleep(POLL_SECONDS)
    finally:
        rpc(base_url, "page.simulationReset", {}, account, case["user"])
    if status["state"] != "done" or not status.get("turn"):
        return None, status.get("error") or status["state"]
    turn, debug = status["turn"], status.get("debug") or {}
    return {"generated": turn["messages"], "noReply": turn["noReply"], "stance": debug.get("stance"),
            "count": debug.get("count"), "probability": debug.get("probability"),
            "omittedLayers": debug.get("omittedLayers"),
            "sampleCount": len(turn.get("samples") or []),
            "singleMs": round((time.monotonic() - started) * 1000)}, None


def analysis_running(base_url, account, user):
    run = (rpc(base_url, "scheme.status", {}, account, user) or {}).get("run") or {}
    return run.get("state") in ("queued", "running")


def run_case(base_url, account, case, timeout, force):
    attempts, payload, error = 0, None, None
    started = time.monotonic()
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        if not force and analysis_running(base_url, account, case["user"]):
            raise SystemExit("这位好友的知己分析正在运行，按协议不启动回测；确认安全后再跑")
        payload, error = generate_once(base_url, account, case, timeout)
        if error is None:
            break
    record = {"caseId": case["caseId"], "messages": case["messages"], "realReplied": case["replied"],
              "real": case["real"], "realResponse": case["realResponse"], "myIntent": case["myIntent"],
              "commit": git_commit(), "failed": error is not None, "attempts": attempts, "generationPath": "rpc",
              "elapsedMs": round((time.monotonic() - started) * 1000), "error": error}
    record.update(payload or {"generated": None, "noReply": None, "stance": None})
    return record


def _chars(texts):
    return len(re.sub(r"\s+", "", "".join(texts)))


def compute_metrics(records):
    valid = [record for record in records if not record.get("failed")]
    if not valid:
        return {"cases": len(records), "failed": len(records), "note": "全部失败"}
    agree = [record["noReply"] == (not record["realReplied"]) for record in valid]
    probabilities = [record["probability"] for record in valid if isinstance(record.get("probability"), (int, float))]
    both = [record for record in valid if record["realReplied"] and not record["noReply"] and record["generated"]]
    labelled = [record for record in both if record.get("realResponse") and record["realResponse"] != "无法判断"]
    counts, lengths, f1s, habits, overlong = [], [], [], collections.Counter(), 0
    for record in both:
        real_chars, gen_chars = max(_chars(record["real"]), 1), max(_chars(record["generated"]), 1)
        counts.append(abs(len(record["generated"]) - len(record["real"])))
        lengths.append(abs(math.log(gen_chars / real_chars)))
        overlong += int(gen_chars > 2 * real_chars)
        f1s.append(bigram_f1("".join(record["generated"]), "".join(record["real"])))
        real_flags, gen_flags = habit_flags("\n".join(record["real"])), habit_flags("\n".join(record["generated"]))
        for key, value in real_flags.items():
            habits[key] += int(value == gen_flags[key])
    stance_pairs = collections.Counter((record["realResponse"], record.get("stance") or "-") for record in labelled)
    by_intent = collections.defaultdict(list)
    for record in labelled:
        by_intent[record.get("myIntent") or "-"].append(record.get("stance") == record["realResponse"])
    mean = lambda values: round(statistics.mean(values), 4) if values else None  # noqa: E731
    return {
        "cases": len(records), "failed": len(records) - len(valid),
        "reply": {"agreement": mean(agree), "realRate": mean([record["realReplied"] for record in valid]),
                  "simulatedRate": mean([not record["noReply"] for record in valid]),
                  "meanProbability": mean(probabilities), "rolled": len(probabilities)},
        "stance": {"cases": len(labelled),
                   "accuracy": mean([record.get("stance") == record["realResponse"] for record in labelled]),
                   "byIntent": {key: {"cases": len(values), "accuracy": mean(values)}
                                for key, values in sorted(by_intent.items())},
                   "pairs": {f"{real}→{simulated}": count for (real, simulated), count in sorted(stance_pairs.items())}},
        "wording": {"cases": len(both), "countDiffMean": mean(counts), "lengthRatioMean": mean(lengths),
                    "overlongRate": round(overlong / len(both), 4) if both else None, "bigramF1Mean": mean(f1s),
                    "habitMatch": {key: round(value / len(both), 4) for key, value in sorted(habits.items())}
                    if both else None},
        # beforeTimeMs drops profile layers 3–4 (性格与三观、关系记忆与概况): they are today's results, not as of then.
        "omittedLayers": sorted({layer for record in valid for layer in record.get("omittedLayers") or []}),
        "sampleCountMean": mean([record.get("sampleCount") or 0 for record in valid]),
        "elapsedMsMean": mean([record.get("elapsedMs") or 0 for record in valid]),
    }


def write_blind(round_dir, records, seed):
    """Real and simulated replies as A/B in random order, the answer key in a separate file."""
    rng = random.Random(seed)
    blind, key = [], {}
    for record in records:
        if record.get("failed") or not record["realReplied"] or record["noReply"] or not record["generated"]:
            continue
        real_first = rng.random() < .5
        pair = (record["real"], record["generated"]) if real_first else (record["generated"], record["real"])
        blind.append({"caseId": record["caseId"], "context": record["messages"], "A": pair[0], "B": pair[1]})
        key[record["caseId"]] = "A" if real_first else "B"
    with (round_dir / "blind.jsonl").open("w", encoding="utf-8") as handle:
        for item in blind:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    (round_dir / "blind-key.json").write_text(json.dumps({"real": key}, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="好友模拟对话回测（走生产 RPC；绝不发送微信消息；"
                                                 "真实聊天只进 .local/reply-eval/friend-sim/）。")
    parser.add_argument("--user", required=True, help="好友的微信 id（Core 的 user）")
    parser.add_argument("--round", required=True, help="轮次名，输出 round-<名>/")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="留出最近多少轮真实往来")
    parser.add_argument("--rebuild", action="store_true", help="重新抽取留出集")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 例（冒烟用）")
    parser.add_argument("--account", default=None)
    parser.add_argument("--db", default=None, help="覆盖插件 analysis.sqlite3 路径（只读）")
    parser.add_argument("--base-url", default=None, help="覆盖 http://127.0.0.1:<selected-port>")
    parser.add_argument("--timeout", type=int, default=300, help="单次尝试轮询上限秒数")
    parser.add_argument("--seed", type=int, default=20261105, help="盲评 A/B 顺序的种子")
    parser.add_argument("--force", action="store_true", help="这位好友的知己分析在跑时仍继续")
    args = parser.parse_args()

    entry = load_account(args.account)
    set_dir = OUT_DIR / "sets"
    set_dir.mkdir(parents=True, exist_ok=True)
    set_path = set_dir / (hashlib.sha256(args.user.encode("utf-8")).hexdigest()[:16] + ".jsonl")
    if args.rebuild or not set_path.is_file():
        cases = build_set(discover_plugin_db(entry, args.db), args.user, args.rounds)
        set_path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
        print(f"留出集 {len(cases)} 例（其中好友未回复 {sum(not case['replied'] for case in cases)} 例）", flush=True)
    cases, _damaged = load_jsonl(set_path)

    round_dir = OUT_DIR / f"round-{args.round}"
    round_dir.mkdir(parents=True, exist_ok=True)
    out_path = round_dir / "generated.jsonl"
    records, damaged = load_jsonl(out_path)
    if damaged:
        print(f"警告：{out_path.name} 有 {damaged} 行损坏，已跳过", flush=True)
    done = {record.get("caseId") for record in records}
    pending = [case for case in cases if case["caseId"] not in done]
    if args.limit:
        pending = pending[:args.limit]
    base_url = args.base_url or f"http://127.0.0.1:{json.loads(PORT_JSON.read_text(encoding='utf-8'))['port']}"
    if not args.force and analysis_running(base_url, entry["account"], args.user):
        raise SystemExit("这位好友的知己分析正在运行，按协议不启动回测；确认安全后再跑")

    for case in pending:
        started = time.monotonic()
        try:
            record = run_case(base_url, entry["account"], case, args.timeout, args.force)
        except SystemExit:
            raise
        except Exception as exc:  # 单例异常不中断：记 failed，继续下一例
            record = {"caseId": case["caseId"], "messages": case["messages"], "realReplied": case["replied"],
                      "real": case["real"], "realResponse": case["realResponse"], "myIntent": case["myIntent"],
                      "commit": git_commit(), "failed": True, "attempts": 0, "generationPath": "rpc",
                      "elapsedMs": round((time.monotonic() - started) * 1000), "error": f"exception: {exc}",
                      "generated": None, "noReply": None, "stance": None}
        records.append(record)
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{len(records)}/{len(cases)}] {record['caseId']} "
              + (f"失败: {record['error']}" if record["failed"] else
                 "没有回复" if record["noReply"] else f"{len(record['generated'])} 条"), flush=True)

    final, _damaged = load_jsonl(out_path)
    metrics = compute_metrics(final)
    metrics.update({"round": args.round, "rounds": args.rounds, "generationPath": "rpc", "commit": git_commit(),
                    "account": entry["account"], "baseUrl": base_url})
    write_blind(round_dir, final, args.seed)
    (round_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
