"""Synthetic shared evidence; summaries never reopen history or extract text.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
from kyf_plugin import Cancelled, ModelError
from self_contracts import SelfError
from self_draft import VERSION as DRAFT_VERSION, saved_view
from self_evidence_summaries import ProfileSummary, TagSummary, PersonaSummary, ValuesSummary, _Summary, _hash
from self_persona import PersonaStore
from self_values import ValuesStore
from self_store import Store
from self_tag_store import TagStore


class Context:
    def __init__(self):
        self.cancelled, self.events = threading.Event(), []
    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()
    def progress(self, **metrics):
        self.events.append(metrics)


class Corpus:
    def __init__(self):
        self.values = {"revision": 1, "source": "synthetic-source"}
    def meta(self, key, default=None):
        return self.values.get(key, default)
    def iter_messages(self, **kwargs):
        raise AssertionError("summaries must not read original corpus messages")


class Evidence:
    def __init__(self, rows=(), total=None):
        self.rows, self.cache = list(rows), {}
        self.total = total
    def identity(self):
        return _hash(self.rows)
    def coverage(self):
        own = self.total if self.total is not None else len({row["item"]["id"] for row in self.rows})
        return {"ownMessages": own, "analyzedMessages": own, "reusedMessages": own,
                "newlyAnalyzedMessages": 0}
    def records(self):
        return iter(copy.deepcopy(self.rows))
    def summary_get(self, key):
        return copy.deepcopy(self.cache.get(key))
    def summary_put(self, key, value):
        self.cache[key] = copy.deepcopy(value)


def record(index, profile=(), tags=(), persona=(), text="我喜欢先独处整理思路", values=()):
    item = {"user": "synthetic-friend", "id": "message-" + str(index), "chat": "合成聊天", "group": False,
            "time": 1700000000000 + index, "part": 1, "parts": 1, "text": text,
            "context": {"before": [], "after": [], "truncated": False, "timeUnknown": False}}
    return {"fingerprint": "fingerprint-" + str(index), "item": item,
            "profile": list(profile), "tags": list(tags), "persona": list(persona), "values": list(values)}


def profile_proof(text="明确兴趣：独处阅读", relation="interest"):
    return {"section": "background", "text": text, "quote": "我喜欢先独处整理思路", "relation": relation,
            "reason": "本人原话，保留所见时期和语境限制"}


def tag_proof(name="阅读", relation="interest", category="阅读学习"):
    return {"name": name, "relation": relation, "category": category, "quote": "我喜欢先独处整理思路", "reason": "本人明确偏好"}


def persona_proof(kind="support", value="低", key="big5_e", quote="我喜欢先独处整理思路", **extra):
    return {"key": key, "kind": kind, "value": value, "topic": "", "scope": "当前所见聊天时期",
            "quote": quote, "reason": "本人原话，仍为推测", **extra}


def values_proof(kind="support", value="重视长期稳定的关系", key="love", quote="我喜欢先独处整理思路"):
    return {"key": key, "kind": kind, "value": value, "topic": "", "scope": "当前所见聊天时期",
            "quote": quote, "reason": "本人原话，仍为推测"}


class Models:
    def __init__(self):
        self.calls, self.hook, self.mode = [], None, None
        self.context_tokens, self.model = 8192, "synthetic-model"
        self.fingerprint = "synthetic-route-1"
    def service(self):
        return {"contextTokens": self.context_tokens, "model": self.model, "fingerprint": self.fingerprint}
    def generate(self, context, request, step=None):
        payload = json.loads(request["prompt"])
        self.calls.append({"step": step, "payload": payload, "system": request["system"],
                           "maxOutputTokens": request.get("maxOutputTokens")})
        if self.hook:
            self.hook(context, step, payload)
        if step == "profile-draft-merge":
            value = {"background": "保留语义限制：" + _hash(payload)[:16], "style": "表达简短"}
            if self.mode in ("verbose", "verbose-cut", "verbose-truncated"):
                value["background"] = "合并超限：" + _hash(payload)[:16] + "，" + "叙述" * 400
            if self.mode == "deflect" and "notes" in payload:
                value = {"background": "待合并资料存在多个来源，标注方式不一致且无法确认是否同一人，以下按来源分组保留矛盾与不确定性。",
                         "style": "待合并资料存在多个来源，标注方式不一致且无法确认是否同一人，以下按来源分组保留矛盾与不确定性。"}
            nth = len([call for call in self.calls if call["step"] == step])
            if self.mode == "verbose-cut" and nth % 3 == 0:
                return {"text": "上游断流，只有半截内容而没有完整JSON" + "尾" * 60}
            if self.mode == "verbose-truncated" and nth % 3 == 0:
                raise ModelError("output-truncated", "模型输出被截断")
            if self.mode == "truncated-first" and nth % 2 == 1:
                # The provider stopped at its output cap; the cut text is delivered.
                raise ModelError("output-truncated", "模型输出被截断",
                                 partial=json.dumps(value, ensure_ascii=False)[:40])
            if self.mode == "truncated-notes" and "notes" in payload:
                raise ModelError("output-truncated", "模型输出被截断", partial='{"background":"半截')
        elif step == "profile-draft-final":
            value = {"background": payload["existing"].get("background") or "我喜欢独处阅读。", "style": "我的表达比较简短。"}
            nth = len([call for call in self.calls if call["step"] == step])
            if self.mode == "lazy-final" and nth % 2 == 1:
                value = {"background": "", "style": ""}
            if self.mode == "always-lazy-final":
                value = {"background": "", "style": ""}
            if self.mode == "tiny-final":
                value = {"background": "我", "style": "我"}
        elif step == "self-tags-merge":
            names = [row["name"] for row in payload["tags"]]
            value = {"merge": [{"into": names[0], "from": names[1:]}]}
        elif step in ("self-persona-synthesis", "self-values-synthesis"):
            support, counter = [], []
            for row in payload.get("evidence", []):
                opposite = self.mode == "persona-opposites" and row["value"] == "高"
                (support if row["kind"] == "support" and not opposite else counter).append(row["eid"])
            for note in payload.get("notes", []):
                support.extend(note["support"])
                counter.extend(note["counter"])
            value = {"text": "所见聊天可能偏内向，其余轴未知", "scope": "仅所见关系与时期，属于推测",
                     "confidence": "有一定依据", "support": list(dict.fromkeys(support))[:4], "counter": list(dict.fromkeys(counter))[:4]}
            if self.mode == "forge":
                value["support"] = ["unknown-evidence"]
            if self.mode == "counter-forge":
                value["counter"] = list(dict.fromkeys(support))[:4]
            if self.mode == "verbose":
                value["text"] = "合并超限叙述。" * 130
        elif step in ("self-persona-audit", "self-values-audit"):
            if self.mode == "garbage-audit":
                return {"text": "抱歉，我无法按JSON输出复核结论"}
            if self.mode == "long-reason-audit":
                # A 177-character acceptance was once parsed as invalid and silently
                # counted as a rejection (live 2026-10-29 core). A thorough reason
                # inside the cap must stay a valid acceptance.
                value = {"accepted": True, "reason": ("核对本人原话、反证和范围；" * 14)[:-1] + "。"}
                return {"text": json.dumps(value, ensure_ascii=False)}
            if self.mode in ("runaway-reason-audit", "runaway-reject-audit"):
                value = {"accepted": self.mode == "runaway-reason-audit", "reason": "超限" * 201}
                return {"text": json.dumps(value, ensure_ascii=False)}
            value = {"accepted": self.mode != "reject", "reason": "核对本人原话、反证和范围"}
        else:
            raise AssertionError("unexpected extraction/model step: " + str(step))
        return {"text": json.dumps(value, ensure_ascii=False)}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name), "synthetic-account")
        self.corpus, self.models, self.context = Corpus(), Models(), Context()
        self.plugin = SimpleNamespace(models=self.models)
    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()
    def profile(self, evidence, existing=None, selected_depth="deep"):
        return ProfileSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence,
                              existing or {"background": "", "style": ""}, selected_depth)
    def persona(self, evidence):
        return PersonaSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep")

    def test_empty_evidence_needs_no_model_and_all_jobs_keep_source_coverage(self):
        evidence = Evidence([record(1)], total=1)
        self.models.service = lambda: (_ for _ in ()).throw(AssertionError("empty evidence needs no model"))
        profile = self.profile(evidence, {"background": "人工资料", "style": ""})
        self.assertEqual(profile.work(self.context)["background"], "人工资料")
        tags = TagStore(self.store)
        tags.edit("add", "人工标签", "其他")
        tag_job = TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep")
        tag_job.work(self.context)
        persona_job = self.persona(evidence)
        result = persona_job.work(self.context)
        self.assertTrue(all(row["selfReport"] is None and row["inference"] is None for row in result["dimensions"]))
        self.assertEqual(tags.tags()[0]["name"], "人工标签")
        self.assertEqual(self.models.calls, [])
        for job in (profile, tag_job, persona_job):
            self.assertEqual((job.view()["done"], job.view()["total"], job.view()["unit"]), (1, 1, "条"))

    def test_profile_cache_manual_only_affects_final_and_status_is_compatible(self):
        evidence = Evidence([record(1, profile=[profile_proof()])])
        job = self.profile(evidence, {"background": "保留人工", "style": ""})
        result = job.work(self.context)
        job._set(state="done", phase="done", result=result)
        self.assertEqual(saved_view(self.store)["result"], result)
        self.assertEqual(self.store.meta("profileDraftRun")["version"], DRAFT_VERSION)
        self.assertNotIn("保留人工", json.dumps(self.models.calls[0], ensure_ascii=False))
        self.assertEqual([call["step"] for call in self.models.calls], ["profile-draft-merge", "profile-draft-final"])
        self.profile(evidence, {"background": "保留人工", "style": ""}).work(self.context)
        self.assertEqual(len(self.models.calls), 2)
        self.profile(evidence, {"background": "修改人工", "style": ""}).work(self.context)
        self.assertEqual([call["step"] for call in self.models.calls[2:]], ["profile-draft-final"])
        self.assertIn("不能因重复", self.models.calls[0]["system"])

    def test_an_over_long_merge_note_is_accepted_once_retries_are_exhausted(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        self.models.mode = "verbose"  # every merge answers over note_chars and never compresses
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        merges = [call for call in self.models.calls if call["step"] == "profile-draft-merge"]
        self.assertGreaterEqual(len(merges), 3)
        self.assertEqual(len(merges) % 3, 0, "every merge node burns all three tries before salvage")
        self.assertTrue(any("上一轮输出未通过校验" in call["system"] for call in merges))
        self.assertEqual([call["step"] for call in self.models.calls][-1], "profile-draft-final")

    def test_salvage_falls_back_to_earlier_replies_when_the_last_one_is_a_cut(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        self.models.mode = "verbose-cut"  # every node's third try arrives as a mid-stream cut
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        merges = [call for call in self.models.calls if call["step"] == "profile-draft-merge"]
        self.assertGreaterEqual(len(merges), 3)
        self.assertEqual(len(merges) % 3, 0)

    def test_salvage_covers_a_final_try_that_never_delivered_text(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        self.models.mode = "verbose-truncated"  # every node's third try raises output-truncated
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")

    def test_a_cut_reply_is_amended_and_told_to_be_shorter(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        self.models.mode = "truncated-first"  # every node's first try stops at the provider's cap
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        merges = [call for call in self.models.calls if call["step"] == "profile-draft-merge"]
        self.assertGreaterEqual(len(merges), 4)
        self.assertEqual(len(merges) % 2, 0, "every node tries again after the cut")
        for index, call in enumerate(merges):
            if index % 2 == 1:
                self.assertIn("上一轮输出达到长度上限被截断", call["system"])
                self.assertIn("压缩", call["system"])

    def test_the_cut_text_reaches_the_salvage_and_the_note_keeps_the_tree_limit(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        self.models.mode = "truncated-notes"  # every merge of notes is cut at the cap
        job = self.profile(evidence)
        result = job.work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        merges = [call for call in self.models.calls if call["step"] == "profile-draft-merge"]
        internal = [call for call in merges if "notes" in call["payload"]]
        self.assertTrue(internal)
        self.assertEqual(len(internal) % 3, 0, "every node burns all three tries before the salvage")
        salvaged = [value for value in evidence.cache.values() if "\n\n" in value.get("background", "")]
        self.assertTrue(salvaged, "the cut text reached the salvage, which kept the children side by side")
        for value in salvaged:
            for key in ("background", "style"):
                self.assertLessEqual(len(value[key]), job.note_chars,
                                     "a salvaged note may not outgrow the tree's own limit")

    def test_the_merge_uses_its_own_compact_rules(self):
        evidence = Evidence([record(1, profile=[profile_proof()])])
        self.profile(evidence).work(self.context)
        merge = next(call for call in self.models.calls if call["step"] == "profile-draft-merge")
        self.assertNotIn("前12条", merge["system"], "the extraction depth rules do not belong to a merge")
        self.assertIn("不用要点列表", merge["system"])
        self.assertIn("不保留原消息id", merge["system"])

    def test_the_final_call_budget_holds_the_legal_output_and_keeps_the_cache_signature(self):
        self.models.context_tokens = 1000000
        evidence = Evidence([record(1, profile=[profile_proof()])])
        job = self.profile(evidence)
        job.work(self.context)
        self.assertEqual(job.signature[2], 16000, "the room is unchanged, so the summary cache stays valid")
        final = next(call for call in self.models.calls if call["step"] == "profile-draft-final")
        merge = next(call for call in self.models.calls if call["step"] == "profile-draft-merge")
        self.assertEqual(final["maxOutputTokens"], job.final_output_tokens)
        self.assertGreaterEqual(final["maxOutputTokens"], 12000,
                                "two fields of 4000 characters need about 12000 tokens")
        self.assertLess(merge["maxOutputTokens"], final["maxOutputTokens"])

    def test_a_lazy_final_pass_is_retried_with_the_rejection_detail(self):
        evidence = Evidence([record(1, profile=[profile_proof()]), record(2, profile=[profile_proof()]),
                             record(3, profile=[profile_proof()]), record(4, profile=[profile_proof("明确偏好：安静的工作环境")])])
        self.models.mode = "lazy-final"  # the first final pass echoes the empty existing profile
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        finals = [call for call in self.models.calls if call["step"] == "profile-draft-final"]
        self.assertEqual(len(finals), 2)
        self.assertIn("至少4字", finals[1]["system"])

    def test_an_always_empty_final_fails_the_stage_instead_of_publishing_the_note(self):
        evidence = Evidence([record(1, profile=[profile_proof()]), record(2, profile=[profile_proof()]),
                             record(3, profile=[profile_proof()]), record(4, profile=[profile_proof("明确偏好：安静的工作环境")])])
        self.models.mode = "always-lazy-final"
        # A note may be a salvage that joined several summaries side by side; it is
        # never published as the user's own profile.
        with self.assertRaises(SelfError) as error:
            self.profile(evidence).work(self.context)
        self.assertEqual(error.exception.code, "invalid-output")

    def test_a_one_character_final_fails_the_stage_instead_of_publishing_the_note(self):
        evidence = Evidence([record(1, profile=[profile_proof()]), record(2, profile=[profile_proof()]),
                             record(3, profile=[profile_proof()]), record(4, profile=[profile_proof("明确偏好：安静的工作环境")])])
        self.models.mode = "tiny-final"  # the final answers with one character per field
        with self.assertRaises(SelfError) as error:
            self.profile(evidence).work(self.context)
        self.assertEqual(error.exception.code, "invalid-output")

    def test_a_deflecting_merge_falls_back_to_its_children(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        self.models.mode = "deflect"  # every merge puts the same disclaimer in both fields
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        final_call = next(call for call in self.models.calls if call["step"] == "profile-draft-final")
        self.assertIn("保留语义限制", json.dumps(final_call["payload"], ensure_ascii=False))
        self.assertNotIn("待合并资料", json.dumps(final_call["payload"], ensure_ascii=False))

    def test_a_poisoned_summary_cache_entry_is_regenerated_not_trusted(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        job = self.profile(evidence)
        job.work(self.context)
        merges = [call for call in self.models.calls if call["step"] == "profile-draft-merge"]
        first = merges[0]
        poison = {"background": "待合并资料存在多个来源，标注方式不一致。", "style": "待合并资料存在多个来源，标注方式不一致。"}
        evidence.summary_put(_hash([job.signature, first["system"], first["payload"]]), poison)
        result = self.profile(evidence).work(self.context)
        self.assertNotEqual(result["background"], poison["background"])
        self.assertGreater(len([call for call in self.models.calls if call["step"] == "profile-draft-merge"]),
                           len(merges), "the poisoned node was regenerated")

    def test_an_over_long_cached_merge_note_is_regenerated_not_inflated(self):
        evidence = Evidence([record(index, profile=[profile_proof(f"第{index}条：我喜欢先独处整理思路")])
                             for index in range(1, 40)])
        job = self.profile(evidence)
        job.work(self.context)
        merges = [call for call in self.models.calls if call["step"] == "profile-draft-merge"]
        first = merges[0]
        # The live run stored 36 of 67 merge notes over note_chars (901-4000字)
        # before the limit was enforced. An entry like that must not be fed back:
        # two such children made a 9401-byte parent payload that no output cap
        # could answer. It is regenerated and the stored note obeys the limit.
        poison = {"background": "合并超限：" + "叙述" * 500, "style": "表达简短。"}
        key = _hash([job.signature, first["system"], first["payload"]])
        evidence.summary_put(key, poison)
        result = self.profile(evidence).work(self.context)
        self.assertEqual(result["background"], "我喜欢独处阅读。")
        self.assertGreater(len([call for call in self.models.calls if call["step"] == "profile-draft-merge"]),
                           len(merges), "the over-long entry was regenerated")
        self.assertNotIn("合并超限：", json.dumps(self.models.calls, ensure_ascii=False),
                         "the poisoned note never reappears as a parent payload")
        healed = evidence.summary_get(key)
        for field in ("background", "style"):
            self.assertLessEqual(len(healed[field]), job.note_chars,
                                 "the replacement note keeps the tree's own limit")

    def test_model_route_fingerprint_invalidates_summaries_even_when_the_alias_is_unchanged(self):
        evidence = Evidence([record(1, profile=[profile_proof()])])
        self.profile(evidence).work(self.context)
        before = len(self.models.calls)
        self.profile(evidence).work(self.context)
        self.assertEqual(len(self.models.calls), before)
        self.models.fingerprint = "synthetic-route-2"
        self.profile(evidence).work(self.context)
        self.assertEqual(len(self.models.calls), before + 2)

    def test_model_route_switch_during_generation_cannot_cache_under_the_previous_route(self):
        evidence = Evidence([record(1, profile=[profile_proof()])])
        self.models.hook = lambda *_args: setattr(self.models, "fingerprint", "synthetic-route-2")
        with self.assertRaises(SelfError) as error:
            self.profile(evidence).work(self.context)
        self.assertEqual(error.exception.code, "model-changed")
        self.assertEqual(evidence.cache, {})

    def test_stable_hash_leaves_reuse_after_earlier_insert_and_depth_and_model_invalidate(self):
        self.models.context_tokens = 4096
        rows = [record(index, profile=[profile_proof("仅提及主题" + str(index) + "限制" * 80, "mention")]) for index in range(1, 33)]
        evidence = Evidence(rows)
        self.profile(evidence).work(self.context)
        original = len(self.models.calls)
        first_payloads = {_hash(call["payload"]) for call in self.models.calls if call["step"] == "profile-draft-merge"}
        evidence.rows.insert(0, record(0, profile=[profile_proof("新增主题", "mention")]))
        self.profile(evidence).work(self.context)
        new_calls = self.models.calls[original:]
        self.assertLess(len(new_calls), original)
        self.assertTrue(all(_hash(call["payload"]) not in first_payloads for call in new_calls if call["step"] == "profile-draft-merge"))
        before = len(self.models.calls)
        self.models.model = "another-synthetic-model"
        self.profile(evidence).work(self.context)
        self.assertGreater(len(self.models.calls) - before, len(new_calls))
        before = len(self.models.calls)
        self.profile(evidence, selected_depth="basic").work(self.context)
        self.assertGreater(len(self.models.calls), before)

    def test_cancellation_and_changed_source_cannot_cache_late_model_results(self):
        for mode in ("cancel", "corpus", "evidence"):
            evidence = Evidence([record(1, profile=[profile_proof()])])
            context = Context()
            def stop(ctx, step, payload):
                if mode == "cancel":
                    ctx.cancelled.set()
                elif mode == "corpus":
                    self.corpus.values["revision"] += 1
                else:
                    evidence.rows[0]["fingerprint"] = "changed"
            self.models.hook = stop
            with self.assertRaises((Cancelled, SelfError)):
                self.profile(evidence).work(context)
            self.assertEqual(evidence.cache, {})
        self.models.hook = None

    def test_tag_projection_removes_changed_auto_evidence_and_preserves_all_manual_choices(self):
        tags = TagStore(self.store)
        evidence = Evidence([record(1, tags=[tag_proof("阅读")]), record(2, tags=[tag_proof("看书")])])
        tags.edit("confirm", "阅读")
        tags.edit("delete", "看书")
        tags.edit("add", "自填习惯", "性格习惯")
        job = TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep")
        job.work(self.context)
        names = {row["name"]: row for row in tags.tags()}
        self.assertEqual(names["阅读"]["level"], "confirmed")
        self.assertNotIn("看书", names)
        self.assertIn("自填习惯", names)
        evidence.rows = [record(1, tags=[tag_proof("电子游戏", "mention", "游戏棋牌")])]
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        names = {row["name"]: row for row in tags.tags()}
        self.assertEqual(names["阅读"]["mentions"], 0)
        self.assertEqual(names["电子游戏"]["level"], "possible")
        self.assertEqual(tags.evidence("电子游戏")["total"], 1)

    def test_tag_cached_merge_rechecks_new_manual_confirmation_and_relation_boundaries(self):
        tags = TagStore(self.store)
        evidence = Evidence([record(1, tags=[tag_proof("阅读")]), record(2, tags=[tag_proof("看书")]),
                             record(3, tags=[tag_proof("阅读新闻", "mention")])])
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        self.assertEqual(len(self.models.calls), 1)
        self.assertTrue(all(row["relation"] == "interest" for row in self.models.calls[0]["payload"]["tags"]))
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        self.assertEqual(len(self.models.calls), 1)
        tags.edit("confirm", "看书")
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        self.assertIn("看书", {row["name"] for row in tags.tags()})

    def test_streamed_tag_staging_is_invisible_until_publish_and_cancel_keeps_manual_edits(self):
        tags = TagStore(self.store)
        original = Evidence([record(1, tags=[tag_proof("旧自动兴趣")])])
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, original, tags, "deep").work(self.context)
        for mode in ("cancel", "source"):
            context = Context()
            evidence = Evidence([record(index, tags=[tag_proof("新自动兴趣")]) for index in range(1, 131)])
            def streamed():
                for index, row in enumerate(evidence.rows):
                    if index == 70:
                        # At least one bounded staging batch is already saved.
                        self.assertEqual({row["name"] for row in tags.tags()} - {"中途人工补充"}, {"旧自动兴趣"})
                        tags.edit("add", "中途人工补充", "其他")
                        if mode == "cancel":
                            context.cancelled.set()
                        else:
                            self.corpus.values["revision"] += 1
                    yield copy.deepcopy(row)
            evidence.records = streamed
            with self.assertRaises((Cancelled, SelfError)):
                TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(context)
            self.assertEqual({row["name"] for row in tags.tags()}, {"旧自动兴趣", "中途人工补充"})
            self.assertEqual(self.store.rows("SELECT name FROM sqlite_temp_master WHERE name LIKE 'evidence_tag_%'"), [])

    def test_tag_original_parts_are_joined_before_atomic_projection(self):
        tags = TagStore(self.store)
        first, second = record(1, text="我喜欢"), record(1, text="先独处整理思路", tags=[
            {**tag_proof(), "quote": "先独处整理思路"}])
        first["item"].update(part=1, parts=2)
        second["item"].update(part=2, parts=2)
        evidence = Evidence([first, second])
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        self.assertEqual(tags.evidence("阅读")["items"][0]["text"], "我喜欢先独处整理思路")
        incomplete = Evidence([first])
        with self.assertRaises(SelfError):
            TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, incomplete, tags, "deep").work(self.context)
        self.assertEqual(tags.evidence("阅读")["total"], 1)

    def test_tag_merge_cannot_absorb_a_manual_confirmation_made_during_model_call(self):
        tags = TagStore(self.store)
        evidence = Evidence([record(1, tags=[tag_proof("阅读")]), record(2, tags=[tag_proof("看书")])])
        def confirm(ctx, step, payload):
            if step == "self-tags-merge":
                tags.edit("confirm", payload["tags"][1]["name"])
        self.models.hook = confirm
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        self.assertEqual({row["name"] for row in tags.tags()}, {"阅读", "看书"})
        tags.edit("confirm", "阅读")
        tags.edit("confirm", "看书")
        before = len(self.models.calls)
        TagSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, tags, "deep").work(self.context)
        self.assertEqual(len(self.models.calls), before)

    def test_persona_self_reports_inference_and_manual_are_separate_and_cached(self):
        evidence = Evidence([record(1, persona=[persona_proof("selfReport", "INFP", key="mbti", quote="我是INFP")], text="我是INFP"),
                             record(2, persona=[persona_proof()]), record(3, persona=[persona_proof()]),
                             record(4, persona=[persona_proof(value="高")])])
        persistence = PersonaStore(self.store)
        persistence.edit("big5_e", "人工记录不进模型", persistence.view()["version"])
        result = self.persona(evidence).work(self.context)
        self.assertEqual(result["selfReports"]["mbti"]["text"], "INFP")
        extraversion = result["dimensions"][2]
        self.assertEqual(extraversion["key"], "big5_e")
        self.assertIsNone(extraversion["selfReport"])
        self.assertEqual(extraversion["manual"]["text"], "人工记录不进模型")
        self.assertEqual(extraversion["inference"]["counts"], {"low": 2, "high": 1, "mixed": 0})
        self.assertLess(extraversion["inference"]["position"], 0)
        self.assertEqual(extraversion["inference"]["counterTotal"], 1, "the counter evidence leans to the other end")
        self.assertNotIn("人工记录不进模型", json.dumps(self.models.calls, ensure_ascii=False))
        calls = len(self.models.calls)
        evidence.rows[0]["fingerprint"] = "changed-unrelated-profile-extraction"
        self.persona(evidence).work(self.context)
        self.assertEqual(len(self.models.calls), calls)

    def test_persona_opposite_support_rows_reach_audit_and_published_proof_snapshot(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()]),
                             record(3, persona=[persona_proof(value="高")])])
        self.models.mode = "persona-opposites"
        job = self.persona(evidence)
        result = job.work(self.context)
        self.assertEqual(job.data["successfulCallCount"], 2, "synthesis and audit both pass without salvage")
        self.assertEqual(job.data["modelCalls"], 2)
        audit = next(call["payload"] for call in self.models.calls if call["step"] == "self-persona-audit")
        self.assertEqual(len(audit["draft"]["counter"]), 1)
        self.assertTrue(set(audit["draft"]["counter"]) <= {row["eid"] for row in audit["evidence"]})
        page = PersonaStore(self.store).evidence_page("big5_e", side="counter", expected_version=result["version"])
        self.assertEqual((page["total"], page["version"]), (1, result["version"]))
        self.assertEqual(page["items"][0]["id"], "message-3")

    def test_large_global_counter_set_does_not_block_a_small_cited_audit(self):
        evidence = Evidence([record(index, persona=[persona_proof(value="低" if index <= 160 else "高")])
                             for index in range(1, 301)])
        self.models.mode = "persona-opposites"
        job = self.persona(evidence)
        result = job.work(self.context)
        inference = next(row["inference"] for row in result["dimensions"] if row["key"] == "big5_e")
        self.assertIsNotNone(inference)
        self.assertEqual((inference["counts"], inference["counterTotal"]),
                         ({"low": 160, "high": 140, "mixed": 0}, 140))
        audit = next(call["payload"] for call in self.models.calls if call["step"] == "self-persona-audit")
        self.assertEqual({row["eid"] for row in audit["evidence"]},
                         set(audit["draft"]["support"] + audit["draft"]["counter"]))
        self.assertLessEqual(len(audit["evidence"]), 8)
        self.assertLessEqual(len(json.dumps(audit, ensure_ascii=False, separators=(",", ":"))), job.room)
        page = PersonaStore(self.store).evidence_page("big5_e", side="counter", limit=100,
                                                      expected_version=result["version"])
        self.assertEqual((page["total"], len(page["items"]), page["nextOffset"]), (140, 100, 100))
        rest = PersonaStore(self.store).evidence_page("big5_e", side="counter", offset=100,
                                                      expected_version=result["version"])
        self.assertEqual(len({row["id"] for row in page["items"] + rest["items"]}), 140)

    def test_draft_counter_citations_are_audited_even_when_they_match_global_position(self):
        evidence = Evidence([record(index, persona=[persona_proof(value="高" if index == 4 else "低")])
                             for index in range(1, 5)])
        job = self.persona(evidence)
        def note(context, rows, summarize):
            low = [row["eid"] for row in rows if row["value"] == "低"]
            return {"text": "合成范围观察有相反情境", "scope": "合成时期", "confidence": "有一定依据",
                    "support": low[:2], "counter": [low[2], low[0]]}
        with patch.object(job, "_tree", side_effect=note):
            result = job.work(self.context)
        audit = next(call["payload"] for call in self.models.calls if call["step"] == "self-persona-audit")
        self.assertEqual(len(audit["evidence"]), 3, "a ref used in both fields is included once")
        self.assertEqual({row["eid"] for row in audit["evidence"]},
                         set(audit["draft"]["support"] + audit["draft"]["counter"]))
        self.assertNotIn("message-4", {row["id"] for row in audit["evidence"]})
        inference = next(row["inference"] for row in result["dimensions"] if row["key"] == "big5_e")
        self.assertEqual(inference["counterTotal"], 1)
        self.assertEqual([row["id"] for row in inference["counter"]], ["message-4"])
        self.assertEqual(set(inference["citedEvidenceIds"]), {row["eid"] for row in audit["evidence"]})
        self.assertTrue(all(row["cited"] for row in inference["evidence"]))
        self.assertFalse(inference["counter"][0]["cited"], "global counter is related, but was not cited")
        support_page = PersonaStore(self.store).evidence_page("big5_e", expected_version=result["version"])
        self.assertEqual(support_page["total"], 3)
        self.assertTrue(all(row["cited"] for row in support_page["items"]), "draft counter on the global support side stays cited")
        counter_page = PersonaStore(self.store).evidence_page("big5_e", side="counter", expected_version=result["version"])
        self.assertFalse(counter_page["items"][0]["cited"])

    def test_preview_and_pages_share_public_quote_citation_identity_across_distinct_eids(self):
        evidence = Evidence([record(1, persona=[persona_proof(), persona_proof(value="高")]),
                             record(2, persona=[persona_proof()])])
        job = self.persona(evidence)
        def note(context, rows, summarize):
            return {"text": "合成范围观察", "scope": "合成时期", "confidence": "有一定依据",
                    "support": [row["eid"] for row in rows if row["value"] == "低"], "counter": []}
        with patch.object(job, "_tree", side_effect=note):
            result = job.work(self.context)
        inference = next(row["inference"] for row in result["dimensions"] if row["key"] == "big5_e")
        self.assertEqual((len(inference["citedEvidenceIds"]), inference["counterTotal"]), (2, 1))
        self.assertTrue(inference["counter"][0]["cited"], "same public quote was cited through its other internal classification")
        page = PersonaStore(self.store).evidence_page("big5_e", side="counter", expected_version=result["version"])
        self.assertEqual(page["items"], inference["counter"])

    def test_cited_audit_trims_context_without_losing_refs_or_mutating_full_proofs(self):
        records = [record(index, persona=[persona_proof(value="低" if index <= 4 else "高")]) for index in range(1, 9)]
        for row in records:
            context = [{"id": str(index), "time": 1600000000000, "side": "other", "speaker": "合成对象",
                        "text": "合成上下文" * 40, "truncated": False} for index in range(18)]
            row["item"]["context"].update(before=context[:12], after=context[12:])
        evidence = Evidence(records)
        before = copy.deepcopy(evidence.rows)
        self.models.context_tokens, self.models.mode = 32768, "persona-opposites"
        job = self.persona(evidence)
        result = job.work(self.context)
        audit = next(call["payload"] for call in self.models.calls if call["step"] == "self-persona-audit")
        self.assertEqual(len(audit["evidence"]), 8)
        self.assertEqual({row["eid"] for row in audit["evidence"]},
                         set(audit["draft"]["support"] + audit["draft"]["counter"]))
        self.assertLessEqual(len(json.dumps(audit, ensure_ascii=False, separators=(",", ":"))), job.room)
        self.assertTrue(any(row["truncated"] for row in audit["evidence"]))
        self.assertEqual(evidence.rows, before)
        inference = next(row["inference"] for row in result["dimensions"] if row["key"] == "big5_e")
        self.assertTrue(inference["limitedContext"])
        self.assertEqual(inference["confidence"], "把握较低")

    def test_values_audit_keeps_legacy_full_counter_pack(self):
        evidence = Evidence([record(index, values=[values_proof("support" if index < 3 else "counter")])
                             for index in range(1, 9)])
        result = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep").work(self.context)
        audit = next(call["payload"] for call in self.models.calls if call["step"] == "self-values-audit")
        self.assertEqual(len(audit["draft"]["counter"]), 4)
        self.assertEqual(len(audit["evidence"]), 8, "non-leaning audit payload and cache identity stay unchanged")
        self.assertEqual(result["dimensions"][0]["inference"]["counterTotal"], 6)
        self.assertNotIn("citedEvidenceIds", result["dimensions"][0]["inference"])
        self.assertTrue(all("cited" not in row for row in result["dimensions"][0]["inference"]["counter"]))

    def test_persona_leaf_and_parent_counter_refs_require_available_support_rows(self):
        job = self.persona(Evidence())
        job.persona_chars = 600
        sources = {"low": {"kind": "support"}, "high": {"kind": "support"},
                   "reported": {"kind": "selfReport"}, "legacy-counter": {"kind": "counter"}}
        value = {"text": "合成范围内存在两端表现", "scope": "合成时期", "confidence": "有一定依据",
                 "support": ["low"], "counter": ["high"]}
        def parse(payload, answer):
            with patch.object(job, "_call", side_effect=lambda ctx, system, data, step, parser, *args, **kwargs:
                              parser(json.dumps(answer, ensure_ascii=False))):
                return job._note(self.context, "big5_e", payload, sources)
        leaf = {"evidence": [{"eid": key, **row} for key, row in sources.items()]}
        self.assertEqual(parse(leaf, value)["counter"], ["high"])
        self.assertEqual(parse({"notes": [value]}, value)["counter"], ["high"])
        for role in ("support", "counter"):
            for identifier in ("unknown", "reported", "legacy-counter"):
                with self.subTest(role=role, identifier=identifier), self.assertRaises(SelfError):
                    parse(leaf, {**value, role: [identifier]})

    def test_values_note_keeps_original_kind_roles_and_filters_mismatched_refs(self):
        job = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, Evidence(), "deep")
        job.persona_chars = 600
        sources = {"yes": {"kind": "support"}, "no": {"kind": "counter"}, "reported": {"kind": "selfReport"}}
        value = {"text": "合成保守结论", "scope": "合成时期", "confidence": "有一定依据",
                 "support": ["yes", "no", "reported", "unknown"], "counter": ["no", "yes", "reported", "unknown"]}
        with patch.object(job, "_call", side_effect=lambda ctx, system, data, step, parser, *args, **kwargs:
                          parser(json.dumps(value, ensure_ascii=False))):
            note = job._note(self.context, "love", {"evidence": [{"eid": key, **row} for key, row in sources.items()]}, sources)
        self.assertEqual((note["support"], note["counter"]), (["yes"], ["no"]))

    def test_only_persona_summary_keys_change_and_old_empty_counter_notes_are_regenerated(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()]),
                             record(3, persona=[persona_proof(value="高")])])
        with patch.object(PersonaSummary, "_cache_key", _Summary._cache_key):
            self.persona(evidence).work(self.context)
        legacy_cache = copy.deepcopy(evidence.cache)
        self.assertTrue(any(value.get("counter") == [] for value in legacy_cache.values()))
        before = len(self.models.calls)
        self.models.mode = "persona-opposites"
        current = self.persona(evidence)
        current.work(self.context)
        self.assertEqual(len(self.models.calls) - before, 2, "old note and audit are both regenerated")
        self.assertTrue(all(evidence.cache[key] == value for key, value in legacy_cache.items()))
        before = len(self.models.calls)
        self.persona(evidence).work(self.context)
        self.assertEqual(len(self.models.calls), before, "corrected notes remain reusable")
        for job in (self.profile(evidence), TagSummary(self.plugin, self.store, "synthetic-account", self.corpus,
                    evidence, TagStore(self.store), "deep"),
                    ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep")):
            job._budget()
            self.assertEqual(job._cache_key("synthetic-system", {"synthetic": 1}),
                             _hash([job.signature, "synthetic-system", {"synthetic": 1}]))

    def test_persona_invalid_references_rejection_and_single_original_message_stay_conservative(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()])])
        self.models.mode = "forge"  # invented citations are dropped, leaving nothing provable
        result = self.persona(evidence).work(self.context)
        self.assertIsNone(result["dimensions"][2]["inference"])
        self.models.mode = "reject"
        result = self.persona(evidence).work(self.context)
        self.assertIsNone(result["dimensions"][2]["inference"])
        evidence = Evidence([record(1, persona=[persona_proof(), persona_proof(value="高")])])
        before = len(self.models.calls)
        result = self.persona(evidence).work(self.context)
        self.assertIsNone(result["dimensions"][2]["inference"])
        self.assertEqual(len(self.models.calls), before)

    def test_an_over_long_persona_note_is_accepted_once_retries_are_exhausted(self):
        evidence = Evidence([record(index, persona=[persona_proof()]) for index in range(1, 40)])
        self.models.mode = "verbose"  # every synthesis answers over persona_chars and never compresses
        result = self.persona(evidence).work(self.context)
        inference = result["dimensions"][2]["inference"]
        self.assertGreater(len(inference["text"]), 600, "the salvaged note is kept as written")
        self.assertEqual([call["step"] for call in self.models.calls if "synthesis" in call["step"]].count(
            "self-persona-synthesis") % 3, 0, "every node burns all three tries before salvage")

    def test_an_over_long_cached_persona_note_is_regenerated_not_inflated(self):
        evidence = Evidence([record(index, persona=[persona_proof()]) for index in range(1, 40)])
        job = self.persona(evidence)
        job.work(self.context)
        calls = [call for call in self.models.calls if call["step"] == "self-persona-synthesis"]
        first = calls[0]
        # A synthesis note stored over persona_chars (a salvage that once bypassed
        # the length check) is regenerated by the same parser gate, not fed back.
        poison = {"text": "合并超限：" + "叙述" * 400, "scope": "仅所见关系与时期，属于推测",
                  "confidence": "有一定依据",
                  "support": [row["eid"] for row in first["payload"].get("evidence", [])][:2], "counter": []}
        evidence.summary_put(job._cache_key(first["system"], first["payload"]), poison)
        self.persona(evidence).work(self.context)
        new_calls = [call for call in self.models.calls if call["step"] == "self-persona-synthesis"]
        self.assertGreater(len(new_calls), len(calls), "the over-long entry was regenerated")
        self.assertNotIn("合并超限：", json.dumps(new_calls, ensure_ascii=False))

    def test_an_unreadable_audit_remains_retryable_without_caching_a_false_rejection(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()]),
                             record(3, persona=[persona_proof()]), record(4, persona=[persona_proof(value="高")])])
        self.models.mode = "garbage-audit"
        with self.assertRaises(SelfError) as failure:
            self.persona(evidence).work(self.context)
        self.assertEqual(failure.exception.code, "invalid-output")
        self.assertGreaterEqual(len([call for call in self.models.calls if call["step"] == "self-persona-audit"]), 3)
        self.assertFalse(any("accepted" in value for value in evidence.cache.values()))
        syntheses = len([call for call in self.models.calls if call["step"] == "self-persona-synthesis"])
        self.models.mode = None
        result = self.persona(evidence).work(self.context)
        self.assertIsNotNone(result["dimensions"][2]["inference"])
        self.assertEqual(len([call for call in self.models.calls if call["step"] == "self-persona-synthesis"]), syntheses)

    def test_a_thorough_audit_reason_is_accepted_not_misread_as_unreadable(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()]),
                             record(3, persona=[persona_proof()]), record(4, persona=[persona_proof(value="高")])])
        self.models.mode = "long-reason-audit"
        result = self.persona(evidence).work(self.context)
        self.assertIsNotNone(result["dimensions"][2]["inference"])

    def test_a_verbose_valid_audit_is_normalized_once_without_flipping_the_decision(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()]),
                             record(3, persona=[persona_proof()]), record(4, persona=[persona_proof(value="高")])])
        self.models.mode = "runaway-reason-audit"
        result = self.persona(evidence).work(self.context)
        self.assertIsNotNone(result["dimensions"][2]["inference"])
        self.assertEqual(len([call for call in self.models.calls if call["step"] == "self-persona-audit"]), 1)
        audit = next(value for value in evidence.cache.values() if "accepted" in value)
        self.assertIs(audit["accepted"], True)
        self.assertEqual(len(audit["reason"]), 400)

    def test_audit_only_namespace_replaces_old_fallbacks_and_preserves_synthesis_keys(self):
        for job_type, domain, key in ((PersonaSummary, "persona", "big5_e"), (ValuesSummary, "values", "love")):
            with self.subTest(domain=domain):
                proof = persona_proof() if domain == "persona" else values_proof()
                evidence = Evidence([record(1, **{domain: [proof]}), record(2, **{domain: [proof]})])
                job = job_type(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep")
                job.work(self.context)
                audit_call = next(call for call in reversed(self.models.calls) if call["step"] == f"self-{domain}-audit")
                synthesis = next(call for call in reversed(self.models.calls) if call["step"] == f"self-{domain}-synthesis")
                old_synthesis = _hash([job.signature, synthesis["system"], synthesis["payload"]])
                old_audit = _hash([job.signature, audit_call["system"], audit_call["payload"]])
                if domain == "persona":
                    old_synthesis = _hash(["persona-counter-kind-v1", old_synthesis])
                    old_audit = _hash(["persona-counter-kind-v1", old_audit])
                self.assertEqual(job._cache_key(synthesis["system"], synthesis["payload"]), old_synthesis)
                new_audit = job._cache_key(audit_call["system"], audit_call["payload"])
                self.assertNotEqual(new_audit, old_audit)
                del evidence.cache[new_audit]
                evidence.cache[old_audit] = {"accepted": False, "reason": "复核输出无法解析"}
                before = len(self.models.calls)
                job_type(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep").work(self.context)
                self.assertEqual([call["step"] for call in self.models.calls[before:]], [f"self-{domain}-audit"])
                self.assertIs(evidence.cache[new_audit]["accepted"], True)
                self.assertIs(evidence.cache[old_audit]["accepted"], False, "old cache is isolated, not rewritten")

    def test_verbose_semantic_rejection_is_preserved_without_retries_and_can_be_cached(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()])])
        self.models.mode = "runaway-reject-audit"
        result = self.persona(evidence).work(self.context)
        self.assertIsNone(result["dimensions"][2]["inference"])
        self.assertEqual(len([call for call in self.models.calls if call["step"] == "self-persona-audit"]), 1)
        verdict = next(value for value in evidence.cache.values() if "accepted" in value)
        self.assertIs(verdict["accepted"], False)
        self.assertEqual(len(verdict["reason"]), 400)
        before = len(self.models.calls)
        self.persona(evidence).work(self.context)
        self.assertEqual(len(self.models.calls), before)

    def test_malformed_current_audit_cache_is_gated_but_real_rejections_are_reusable(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()])])
        job = self.persona(evidence)
        job.work(self.context)
        audit_call = next(call for call in self.models.calls if call["step"] == "self-persona-audit")
        key = job._cache_key(audit_call["system"], audit_call["payload"])
        evidence.cache[key] = {"accepted": "false", "reason": "合成损坏字段"}
        self.models.mode = "reject"
        before = len(self.models.calls)
        self.persona(evidence).work(self.context)
        self.assertEqual([call["step"] for call in self.models.calls[before:]], ["self-persona-audit"])
        self.assertIs(evidence.cache[key]["accepted"], False)
        before = len(self.models.calls)
        self.persona(evidence).work(self.context)
        self.assertEqual(len(self.models.calls), before, "a valid semantic rejection is a cacheable verdict")

    def test_synthesis_and_audit_prompts_carry_the_traceability_contract(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()])])
        self.persona(evidence).work(self.context)
        systems = {call["step"]: call["system"] for call in self.models.calls}
        self.assertIn("每一句断言都必须能落到", systems["self-persona-synthesis"])
        self.assertIn("不要因为单次对话或少数聊天无法证明长期稳定", systems["self-persona-audit"])
        self.assertIn("最多400字", systems["self-persona-audit"])

    def test_persona_publication_keeps_concurrent_manual_edit_and_new_source_clears_old_ai(self):
        evidence = Evidence([record(1, persona=[persona_proof()]), record(2, persona=[persona_proof()])])
        persistence = PersonaStore(self.store)
        def edit(ctx, step, payload):
            if step == "self-persona-audit":
                persistence.edit("big5_e", "生成时补充", persistence.view()["version"])
        self.models.hook = edit
        result = self.persona(evidence).work(self.context)
        self.assertEqual(result["dimensions"][2]["manual"]["text"], "生成时补充")
        evidence.rows = [record(3)]
        result = self.persona(evidence).work(self.context)
        self.assertIsNone(result["dimensions"][2]["inference"])
        self.assertEqual(result["dimensions"][2]["manual"]["text"], "生成时补充")

    def test_values_counter_citations_without_counter_rows_are_dropped_not_fatal(self):
        # Extraction never emits values counter rows; a model that fills counter with
        # support ids must not fail the whole synthesis (GLM live run 2026-10-28).
        evidence = Evidence([record(1, values=[values_proof()]), record(2, values=[values_proof()])])
        self.models.mode = "counter-forge"
        result = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep").work(self.context)
        love = result["dimensions"][0]
        self.assertEqual((love["inference"]["counterTotal"], love["inference"]["counter"]), (0, []))
        self.assertEqual(love["inference"]["evidenceTotal"], 2)

    def test_values_audit_accepts_a_thorough_reason_and_carries_the_contract(self):
        # The live core acceptance lost to the old reason cap was a values dimension.
        evidence = Evidence([record(1, values=[values_proof()]), record(2, values=[values_proof()])])
        self.models.mode = "long-reason-audit"
        result = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep").work(self.context)
        self.assertIsNotNone(result["dimensions"][0]["inference"])
        audit_systems = [call["system"] for call in self.models.calls if call["step"] == "self-values-audit"]
        self.assertIn("不要因为单次对话或少数聊天无法证明长期稳定", audit_systems[0])
        synthesis_systems = [call["system"] for call in self.models.calls if call["step"] == "self-values-synthesis"]
        self.assertIn("每一句断言都必须能落到", synthesis_systems[0])

    def test_values_use_their_own_dimensions_and_store_and_keep_manual_out_of_prompts(self):
        evidence = Evidence([record(1, values=[values_proof("selfReport", "不急着结婚", quote="我不急着结婚")], text="我不急着结婚"),
                             record(2, values=[values_proof()]), record(3, values=[values_proof()]),
                             record(4, values=[values_proof("counter", "有时想早点安定")])])
        persistence = ValuesStore(self.store)
        persistence.edit("love", "人工记录不进模型", persistence.view()["version"])
        result = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep").work(self.context)
        self.assertEqual([item["label"] for item in result["dimensions"]], ["感情观", "家庭观", "金钱观", "人生规划", "价值底色"])
        love = result["dimensions"][0]
        self.assertEqual(love["selfReport"]["text"], "不急着结婚")
        self.assertEqual((love["inference"]["evidenceTotal"], love["inference"]["counterTotal"]), (2, 1))
        self.assertEqual(love["manual"]["text"], "人工记录不进模型")
        self.assertTrue(all(item["inference"] is None for item in result["dimensions"][1:]))
        self.assertEqual({call["step"] for call in self.models.calls}, {"self-values-synthesis", "self-values-audit"})
        self.assertNotIn("人工记录不进模型", json.dumps(self.models.calls, ensure_ascii=False))
        self.assertTrue(all(item["inference"] is None for item in PersonaStore(self.store).view()["dimensions"]))
        with self.assertRaises(SelfError):
            persistence.edit("mbti", "不属于三观", persistence.view()["version"])

    def test_values_dimensions_this_run_cannot_conclude_keep_their_published_inference(self):
        evidence = Evidence([record(1, values=[values_proof()]), record(2, values=[values_proof()])])
        first = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, evidence, "deep").work(self.context)
        published = first["dimensions"][0]["inference"]
        self.assertIsNotNone(published)
        # One synced message already changes the identity, and the next run's audit
        # rejects the redone draft (live 2026-10-29: this nullified three published
        # dimensions). A dimension without a fresh conclusion keeps the stored one.
        grown = Evidence([*evidence.rows, record(9, values=[values_proof(key="money")])])
        self.models.mode = "reject"
        second = ValuesSummary(self.plugin, self.store, "synthetic-account", self.corpus, grown, "deep").work(self.context)
        self.assertEqual(second["dimensions"][0]["inference"], published)
        self.assertTrue(all(item["inference"] is None for item in second["dimensions"][1:]))
        self.assertIsNotNone(ValuesStore(self.store).meta("published")[0]["inference"])

    def test_a_profile_stage_without_any_proof_fails_instead_of_publishing_blank(self):
        # No reviewed profile proof and no saved text: the stage may not end done
        # with empty fields — it reports honestly instead (the final pass and its
        # salvage refuse blanks the same way).
        evidence = Evidence([record(1)])
        with self.assertRaises(SelfError) as error:
            self.profile(evidence).work(self.context)
        self.assertEqual(error.exception.code, "no-profile-evidence")
        # The user's own saved text is still kept without any model call.
        kept = self.profile(evidence, {"background": "人工资料", "style": ""}).work(self.context)
        self.assertEqual(kept["background"], "人工资料")
        self.assertEqual(self.models.calls, [])


if __name__ == "__main__":
    unittest.main()
