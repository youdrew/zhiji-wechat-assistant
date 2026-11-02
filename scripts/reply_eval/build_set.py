"""表达评测集抽样（docs/loop/reply-eval 协议 3.1–3.3）。所有数据库只读打开（mode=ro）。

用法：
  python build_set.py --set dev                 # 输出 .local/reply-eval/sets/dev.jsonl
  python build_set.py --set holdout --seed 20261031
                                                # 读取已有 dev.jsonl 排除其 sampleId 后抽样，
                                                # 输出 holdout.jsonl（由评分方执行，执行方不读内容）

固定默认种子：dev=20261030，holdout=20261031。分层（真实回复去空白字数）：≤5 / 6–30 / >30
约 3:4:3；单个好友 ≤3 例（覆盖不足 20 位好友时收紧为 2、1 重抽）；≥8 例为「好友问到本人
自身情况」（启发式判定，见 asks_about_me，抽样后需人工核对）；场景尽量多样（按 scene
轮转取例，问句配额先取）。

firstReplyMs 取法（写入每例）：把样本 evidence 的消息 id 到 Core 消息库
（advisor.sqlite3 的 context_messages，只读）反查 side/time_ms；回复行 = evidence 中
最后一条「对方」消息之后的本人消息行，firstReplyMs = 其中最早的时间（并与该样本
time_ms=最后回复时间做一致性校验）。侧别缺失时按回复行数从尾部取；消息查不到时用
保守值——最后一条「对方」消息的时间（不晚于任何回复内容的时间）；完全反查不到的
样本不入选。采用的取法与各类计数打印在 stdout 统计里。

输出每例字段：caseId、sampleId、user、input（原文）、reply（原文）、timeMs、firstReplyMs、
evidence（消息 id/seq，generate.py 防泄漏截断用），另附 scene / asksAboutMe / lengthClass
便于统计。样本原文只进 .local/reply-eval/。

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import argparse
import collections
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA_DIR, discover_core_db, discover_self_db, load_account, open_ro, parse_context  # noqa: E402

DEFAULT_SEED = {"dev": 20261030, "holdout": 20261031}
MAX_PER_USER = 3
MIN_USERS = 20
MIN_ASKS = 8
# 媒体占位行（_plain 已过滤非文本行，这里防御性兜底）；[表情名] 是真实表达，不算占位。
MEDIA_PLACEHOLDER = re.compile(
    r"^\s*\[(?:图片|图像|照片|语音|文件|视频|链接|名片|转账|红包|位置|音乐|小程序|视频号|动画表情|"
    r"表情包|拍一拍|系统|系统消息|引用|合并转发|聊天记录|Image|Voice|File|Video|Link|Sticker|"
    r"Location|RedEnvelope|Transfer)\]\s*$", re.I)
# 「好友问到本人自身情况/看法」的启发式：对方行里的常见问法，或含“你”且带问号。
ASK_ABOUT_ME = re.compile(
    r"你(觉得|认为|是不是|有没有|会不会|喜欢|最近|想不想|想|平时|现在|还|在|去|的|呢|怎么样|好吗)|^你$")


def length_class(reply):
    size = len(re.sub(r"\s+", "", reply))
    return "short" if size <= 5 else "mid" if size <= 30 else "long"


def is_media_heavy(input_text, reply):
    """上下文主要是占位符：任一回复行是占位符，或占位行占 input+reply 总行数过半。"""
    reply_lines = reply.splitlines()
    if any(MEDIA_PLACEHOLDER.match(line) for line in reply_lines):
        return True
    all_lines = input_text.splitlines() + reply_lines
    placeholders = sum(1 for line in all_lines if MEDIA_PLACEHOLDER.match(line))
    return placeholders * 2 >= len(all_lines) and placeholders >= 2


def asks_about_me(input_text):
    """「好友问到本人自身情况/看法」：只看结尾连续的好友消息（即该例回复所回应的内容），
    与 Trial.focus 一致；对方行里的常见问法，或含“你”且带问号。"""
    turns = parse_context(input_text)
    if turns is None:
        return False
    start = len(turns)
    while start > 0 and turns[start - 1]["from"] == "friend":
        start -= 1
    return any(ASK_ABOUT_ME.search(turn["text"]) or ("你" in turn["text"] and ("？" in turn["text"] or "?" in turn["text"]))
               for turn in turns[start:])


def first_reply_ms(core, user, evidence, reply, time_ms):
    """按模块注释的取法反查第一条真实回复的时间。返回 (毫秒, 取法)；反查不到返回 (None, 原因)。

    取法："sided" = 消息与侧别齐全，取最后一条对方消息之后行中的最早时间；"tail" = 侧别
    缺失，按回复行数从尾部取；"conservative" = 部分消息缺失，退化为最后一条对方消息的时间。
    """
    rows = {}
    evidence = list(evidence)
    for start in range(0, len(evidence), 400):
        chunk = evidence[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in core.execute(
                f"SELECT seq, msg_id, side, time_ms FROM context_messages WHERE user=? AND seq IN ({marks})",
                (user, *[item["seq"] for item in chunk])):
            rows[row["seq"]] = row
    resolved = [rows[item["seq"]] for item in evidence
                if rows.get(item["seq"]) is not None and rows[item["seq"]]["msg_id"] == item["id"]]
    other_times = [row["time_ms"] for row in resolved if row["side"] == "other"]
    if len(resolved) == len(evidence) and other_times:
        index = max(position for position, row in enumerate(resolved) if row["side"] == "other")
        reply_rows = resolved[index + 1:]
        if reply_rows and max(row["time_ms"] for row in reply_rows) == time_ms:
            return min(row["time_ms"] for row in reply_rows), "sided"
        count = reply.count("\n") + 1
        if len(resolved) > count:
            return min(row["time_ms"] for row in resolved[-count:]), "tail"
    if other_times:
        return max(other_times), "conservative"
    return None, "missing"


def stratified_sample(candidates, size, rng, max_per_user):
    """按 3:4:3 分层；层内先取「问到本人情况」的配额，再按场景轮转取满；单好友 ≤max_per_user。"""
    targets = {"short": round(size * 0.3), "mid": round(size * 0.4)}
    targets["long"] = size - targets["short"] - targets["mid"]
    # ≥8 例问到本人情况：按层大小分摊配额，余数从大到小补。
    ask_targets = {key: value * MIN_ASKS // size for key, value in targets.items()}
    for key in sorted(targets, key=lambda key: -targets[key])[:MIN_ASKS - sum(ask_targets.values())]:
        ask_targets[key] += 1
    picked, user_counts = [], collections.Counter()

    def take(groups, quota):
        """场景轮转：每轮各场景取一例，直到配额满足或耗尽；避开超配额的好友。"""
        order = [scene for scene in groups if groups[scene]]
        rng.shuffle(order)
        progress = True
        while quota > 0 and progress:
            progress = False
            for scene in order:
                while quota > 0 and groups.get(scene):
                    candidate = groups[scene][0]
                    if user_counts[candidate["user"]] >= max_per_user:
                        groups[scene].pop(0)
                        continue
                    picked.append(candidate)
                    user_counts[candidate["user"]] += 1
                    groups[scene].pop(0)
                    quota -= 1
                    progress = True
                    break
        return quota

    for key in targets:
        pools = {"asks": collections.defaultdict(list), "rest": collections.defaultdict(list)}
        for candidate in candidates:
            if candidate["lengthClass"] != key:
                continue
            pools["asks" if candidate["asksAboutMe"] else "rest"][candidate["scene"]].append(candidate)
        for pool in pools.values():
            for bucket in pool.values():
                rng.shuffle(bucket)
        ask_quota = min(ask_targets[key], sum(map(len, pools["asks"].values())))
        taken_asks = ask_quota - take(pools["asks"], ask_quota)
        take(pools["rest"], targets[key] - taken_asks)
    return picked


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="抽样构建表达评测集（只读；样本原文只进 .local/reply-eval/）。"
                                                     "firstReplyMs 取法见本脚本 docstring。")
    parser.add_argument("--set", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子；默认 dev=20261030、holdout=20261031")
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--account", default=None, help="Core 账号名；默认 accounts.json 的唯一账号")
    parser.add_argument("--db", default=None, help="self-analysis.sqlite3 路径覆盖")
    parser.add_argument("--core-db", default=None, help="advisor.sqlite3（Core 消息库）路径覆盖")
    args = parser.parse_args()
    seed = args.seed if args.seed is not None else DEFAULT_SEED[args.set]

    entry = load_account(args.account)
    self_db = Path(args.db) if args.db else discover_self_db(entry)
    core_db = Path(args.core_db) if args.core_db else discover_core_db(entry)
    db, core = open_ro(self_db), open_ro(core_db)
    db.row_factory = core.row_factory = sqlite3.Row

    excluded_ids = {row[0] for row in db.execute("SELECT sample_id FROM exclusions")}
    if args.set == "holdout":
        dev_path = DATA_DIR / "sets" / "dev.jsonl"
        if not dev_path.is_file():
            raise SystemExit("holdout 需要已有的 .local/reply-eval/sets/dev.jsonl")
        excluded_ids |= {json.loads(line)["sampleId"] for line in dev_path.read_text(encoding="utf-8").splitlines()
                         if line.strip()}

    rows = db.execute("SELECT s.id, s.user, s.input, s.reply, s.time_ms, s.scene, s.evidence FROM samples s "
                      "WHERE NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)").fetchall()
    candidates, dropped = [], collections.Counter()
    for row in rows:
        if row["id"] in excluded_ids:
            dropped["overlapping-or-excluded"] += 1
            continue
        if "@chatroom" in row["user"]:
            dropped["group"] += 1
            continue
        if is_media_heavy(row["input"], row["reply"]):
            dropped["media"] += 1
            continue
        if parse_context(row["input"]) is None:
            dropped["context"] += 1
            continue
        candidates.append({"sampleId": row["id"], "user": row["user"], "input": row["input"],
                           "reply": row["reply"], "timeMs": row["time_ms"], "scene": row["scene"],
                           "evidence": json.loads(row["evidence"]), "lengthClass": length_class(row["reply"]),
                           "asksAboutMe": asks_about_me(row["input"])})

    picked = []
    for max_per_user in (MAX_PER_USER, 2, 1):  # 好友覆盖不足时收紧单好友上限重抽（候选列表每次重建）
        rng = random.Random(seed)
        picked = stratified_sample(candidates, args.size, rng, max_per_user)
        if len(picked) == args.size and len({row["user"] for row in picked}) >= MIN_USERS:
            break
    if len(picked) != args.size:
        raise SystemExit(f"候选不足：只选出 {len(picked)} 例（需 {args.size}），请检查过滤条件")

    selected, first_reply_sources = [], collections.Counter()
    for row in sorted(picked, key=lambda row: (row["timeMs"], row["sampleId"])):
        milliseconds, source = first_reply_ms(core, row["user"], row["evidence"], row["reply"], row["timeMs"])
        if milliseconds is None:
            first_reply_sources["dropped-missing-evidence"] += 1
            continue
        first_reply_sources[source] += 1
        selected.append({"caseId": "", "sampleId": row["sampleId"], "user": row["user"], "input": row["input"],
                         "reply": row["reply"], "timeMs": row["timeMs"], "firstReplyMs": milliseconds,
                         "evidence": row["evidence"], "scene": row["scene"], "asksAboutMe": row["asksAboutMe"],
                         "lengthClass": row["lengthClass"]})
    asks = sum(1 for case in selected if case["asksAboutMe"])
    if asks < MIN_ASKS:
        raise SystemExit(f"问到本人情况的例数不足：{asks}（需 ≥{MIN_ASKS}），请调整启发式或种子")
    for index, case in enumerate(selected, 1):
        case["caseId"] = f"{args.set}-{index:03d}"

    out_dir = DATA_DIR / "sets"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.set}.jsonl"
    out_path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in selected), encoding="utf-8")

    print(f"输出 {out_path}（{len(selected)} 例，seed={seed}）")
    print("分层计数:", dict(collections.Counter(case["lengthClass"] for case in selected)))
    print("好友数:", len({case['user'] for case in selected}),
          "单好友最多例数:", max(collections.Counter(case['user'] for case in selected).values()))
    print("问到本人情况的例数:", asks)
    print("场景分布:", dict(collections.Counter(case["scene"] for case in selected)))
    print("firstReplyMs 取法:", dict(first_reply_sources))
    print("过滤统计:", dict(dropped))


if __name__ == "__main__":
    main()
