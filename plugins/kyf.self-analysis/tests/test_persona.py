"""Personal tendencies: synthetic local corpus, bounded evidence and fake model only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from kyf_plugin import Cancelled
from self_contracts import SelfError
from self_persona import (DIMENSIONS, Persona, PersonaStore, evidence_record, parse_evidence,
                          parse_audit_verdict, refresh_context, scrub_display_text, source_identity)
from self_values import ValuesStore
from self_profile_corpus import ProfileCorpus, Reader
from self_store import Store
from test_profile_corpus import Context, Core, message


def proof(index, key="big5_e", kind="support", value="低", quote="我喜欢先独处", **extra):
    return {"i": index, "key": key, "kind": kind, "value": value, "topic": "", "scope": "仅限所见聊天时期",
            "quote": quote, "reason": "本人原话有对应表现，仍为推测", **extra}


class Models:
    def __init__(self):
        self.requests, self.responses = [], []
        self.hook = None
        self.accept = True
        self.audit_reason = "已核对原话和关系范围"
        self.context_tokens = 32768

    def service(self, refresh=False):
        return {"contextTokens": self.context_tokens, "model": "synthetic-model"}

    def generate(self, context, request, step=None):
        payload = json.loads(request["prompt"])
        record = {"step": step, "system": request["system"], "prompt": payload, "succeeded": False}
        self.requests.append(record)
        if self.hook:
            self.hook(context, record)
        context.check()
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            record["succeeded"] = True
            return {"text": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)}
        if step == "self-persona-evidence":
            evidence = []
            for item in payload["messages"]:
                text, index = item["text"], item["i"]
                for value in ("INFP", "INTJ"):
                    if "我是" + value in text:
                        evidence.append(proof(index, "mbti", "selfReport", value, "我是" + value))
                if "我是焦虑型依恋" in text:
                    evidence.append(proof(index, "attachment", "selfReport", "焦虑型", "我是焦虑型依恋"))
                if "我自认偏左" in text:
                    evidence.append(proof(index, "politics", "selfReport", "偏左", "我自认偏左"))
                if "累进税" in text:
                    evidence.append(proof(index, "pol_axis", "support", "进步", text[:80]))
                if "独处" in text:
                    evidence.append(proof(index, quote=text[:80]))
                if "也会主动社交" in text:
                    evidence.append(proof(index, value="高", quote=text[:80]))
                if "与伴侣" in text:
                    evidence.append(proof(index, "att_anx", "support", "高", text[:80],
                                          scope="与伴侣的关系，当前所见时期"))
            value = {"evidence": evidence}
        elif step == "self-persona-review":
            value = {"evidence": payload["evidence"]}
        elif step == "self-persona-synthesis":
            rows = payload.get("evidence", [])
            values = [row["value"] for row in rows]
            majority = max(set(values), key=values.count) if values else None
            support = [row["eid"] for row in rows if row["value"] == majority][:4]
            counter = [row["eid"] for row in rows if row["value"] != majority][:4]
            for note in payload.get("notes", []):
                support.extend(identifier for identifier in note["support"] if identifier not in support)
                counter.extend(identifier for identifier in note["counter"] if identifier not in counter)
            value = {"text": "部分聊天表现偏内向，仍有社交反例" if payload["key"] == "big5_e" else "在与伴侣的沟通中可能更需要回应确认",
                     "confidence": "有一定依据", "scope": "与伴侣或所见聊天关系，当前记录时期",
                     "support": support[:4], "counter": counter[:4]}
        elif step == "self-persona-audit":
            value = {"accepted": self.accept, "reason": self.audit_reason}
        else:
            raise AssertionError(step)
        record["succeeded"] = True
        return {"text": json.dumps(value, ensure_ascii=False)}


class PersonaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name), "synthetic-account")
        self.corpus = ProfileCorpus(self.store)
        self.core = Core({"friend-a": [message(1, "我是INFP"), message(2, "我喜欢先独处整理思路"),
                                         message(3, "我需要独处才能理清想法"), message(4, "我也会主动社交")],
                          "group@chatroom": [message(1, "我是INTJ", "other", senderName="朋友", senderId="other"),
                                              message(2, "我只是看到了朋友的自述")]})
        Reader(self.core, "synthetic-account", self.corpus, Context(), None).run()
        self.models = Models()
        self.plugin = SimpleNamespace(models=self.models)
        self.persona = PersonaStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def run_persona(self, depth="deep"):
        job = Persona(self.plugin, self.store, "synthetic-account", self.corpus, depth)
        context = Context()
        self.progress = []
        context.progress = lambda **kwargs: self.progress.append(kwargs)
        return job.work(context)

    def test_audit_parser_preserves_true_and_false_while_bounding_reason_format(self):
        for decision in (True, False):
            for length in (400, 401, 2000):
                with self.subTest(decision=decision, length=length):
                    reason = "合" * length
                    value = parse_audit_verdict(json.dumps({"accepted": decision, "reason": reason}))
                    self.assertIs(value["accepted"], decision)
                    self.assertLessEqual(len(value["reason"]), 400)
                    if length == 400:
                        self.assertEqual(value["reason"], reason)
            value = parse_audit_verdict(json.dumps({"accepted": decision,
                "reason": "核对证据sp_" + "a" * 24 + "，符合合成范围。"}))
            self.assertIs(value["accepted"], decision)
            self.assertNotIn("sp_", value["reason"])
            self.assertIn("符合合成范围", value["reason"])

    def test_invalid_audit_json_fields_or_types_are_errors_not_semantic_rejections(self):
        for raw in ("not json", '{"accepted":true}', '{"accepted":"true","reason":"合成"}',
                    '{"accepted":1,"reason":"合成"}', '{"accepted":false,"reason":null}',
                    '{"reason":"合成"}'):
            with self.subTest(raw=raw), self.assertRaises(SelfError) as failure:
                parse_audit_verdict(raw)
            self.assertEqual(failure.exception.code, "invalid-output")

    def test_legacy_persona_uses_the_same_non_flipping_audit_parser(self):
        self.models.audit_reason = "合成理由" * 150
        result = self.run_persona()
        self.assertIsNotNone(next(row["inference"] for row in result["dimensions"] if row["key"] == "big5_e"))
        self.assertEqual(len([row for row in self.models.requests if row["step"] == "self-persona-audit"]), 1)

    def test_published_evidence_pages_keep_citations_first_and_expose_the_full_set(self):
        rows = [{"eid": f"proof{index:02d}", "key": "big5_e", "kind": "support", "value": "低", "id": str(index),
                 "time": index, "quote": f"合成原话{index}", "reason": "合成理由"} for index in range(20)]
        selected = [{key: value for key, value in row.items() if key not in ("eid", "key", "kind", "value")}
                    for row in rows[:2]]
        inference = {"text": "合成描述", "position": -1, "evidence": selected, "counter": [], "evidenceTotal": 20}
        values = {key: (None, inference if key == "big5_e" else None) for key, _ in self.persona.dimensions + self.persona.reports}
        self.persona.begin("snapshot", {})
        self.persona.publish("snapshot", values, {}, evidence=rows)
        first = self.persona.evidence_page("big5_e", limit=2)
        self.assertEqual(first["items"], [{**item, "cited": True} for item in selected])
        rest = self.persona.evidence_page("big5_e", offset=2, limit=100)
        self.assertEqual((first["total"], first["nextOffset"], rest["nextOffset"]), (20, 2, None))
        self.assertEqual(len({row["id"] for row in first["items"] + rest["items"]}), 20)
        self.assertTrue(all(row["cited"] is False for row in rest["items"]))
        self.assertEqual(self.persona.evidence_page("big5_e", side="counter")["total"], 0)
        with self.assertRaises(SelfError):
            self.persona.evidence_page("big5_e", offset=-1)

    def test_proof_only_changes_advance_the_version_and_reject_a_stale_page(self):
        row = {"eid": "one", "key": "big5_e", "kind": "support", "value": "低", "id": "one", "time": 1,
               "quote": "合成原话", "reason": "合成理由"}
        inference = {"text": "合成描述", "position": -1, "evidence": [], "counter": []}
        result = {key: (None, inference if key == "big5_e" else None) for key, _ in self.persona.dimensions + self.persona.reports}
        self.persona.begin("snapshot", {})
        self.persona.publish("snapshot", result, {}, evidence=[row])
        before = self.persona.view()["version"]
        self.assertEqual(self.persona.evidence_page("big5_e", expected_version=before)["version"], before)
        more = {**row, "eid": "two", "id": "two", "time": 2}
        self.persona.publish("snapshot", result, {}, evidence=[row, more])
        after = self.persona.view()["version"]
        self.assertGreater(after, before)
        with self.assertRaises(SelfError) as failure:
            self.persona.evidence_page("big5_e", offset=1, expected_version=before)
        self.assertEqual(failure.exception.code, "stale-evidence")
        self.assertEqual(self.persona.evidence_page("big5_e", expected_version=after)["total"], 2)
        # Ordering and dictionary serialization are not a different proof set.
        self.persona.publish("snapshot", result, {}, evidence=[dict(reversed(list(more.items()))), row])
        self.assertEqual(self.persona.view()["version"], after)
        self.persona.publish("snapshot", result, {}, evidence=[])
        self.assertGreater(self.persona.view()["version"], after)

    def test_explicit_citations_mark_both_pages_beyond_preview_without_changing_order_or_totals(self):
        rows = [{"eid": f"proof{index:02d}", "key": "big5_e", "kind": "support", "value": "低" if index < 30 else "高",
                 "id": str(index), "time": index, "quote": f"合成原话{index}", "reason": "合成理由"} for index in range(50)]
        def public(row):
            return {key: value for key, value in row.items() if key not in ("eid", "key", "kind", "value")}
        inference = {"text": "合成描述", "position": -.2, "evidence": [public(row) for row in rows[:2]],
                     "counter": [public(row) for row in rows[30:42]], "evidenceTotal": 30, "counterTotal": 20}
        values = {key: (None, inference if key == "big5_e" else None) for key, _ in self.persona.dimensions + self.persona.reports}
        self.persona.begin("snapshot", {})
        self.persona.publish("snapshot", values, {}, evidence=rows)
        before = self.persona.view()["version"]
        old_counter = self.persona.evidence_page("big5_e", side="counter", limit=100)
        self.assertTrue(all("cited" not in row for row in old_counter["items"]), "legacy counter behavior is preserved")
        inference["citedEvidenceIds"] = ["proof00", "proof01", "proof29", "proof49"]
        self.persona.publish("snapshot", values, {}, evidence=rows)
        version = self.persona.view()["version"]
        self.assertGreater(version, before)
        with self.assertRaises(SelfError) as failure:
            self.persona.evidence_page("big5_e", side="counter", expected_version=before)
        self.assertEqual(failure.exception.code, "stale-evidence")
        first = self.persona.evidence_page("big5_e", side="counter", limit=12, expected_version=version)
        rest = self.persona.evidence_page("big5_e", side="counter", offset=12, expected_version=version)
        self.assertEqual((first["total"], first["nextOffset"], rest["nextOffset"]), (20, 12, None))
        self.assertFalse(any(row["cited"] for row in first["items"]))
        self.assertEqual([row["id"] for row in rest["items"] if row["cited"]], ["49"])
        self.assertEqual([row["id"] for row in first["items"] + rest["items"]],
                         [row["id"] for row in old_counter["items"]])
        support = self.persona.evidence_page("big5_e", limit=100, expected_version=version)
        self.assertEqual(support["total"], 30)
        self.assertEqual({row["id"] for row in support["items"] if row["cited"]}, {"0", "1", "29"})

    def test_values_pages_ignore_persona_citation_metadata(self):
        store = ValuesStore(self.store)
        rows = [{"eid": f"proof{index}", "key": "love", "kind": "counter" if index else "support", "value": "合成观点",
                 "id": str(index), "time": index, "quote": f"合成原话{index}", "reason": "合成理由"} for index in range(2)]
        inference = {"text": "合成描述", "evidence": [], "counter": [], "citedEvidenceIds": ["proof1"]}
        values = {key: (None, inference if key == "love" else None) for key, _ in store.dimensions + store.reports}
        store.begin("snapshot", {})
        store.publish("snapshot", values, {}, evidence=rows)
        counter = store.evidence_page("love", side="counter")
        self.assertEqual(counter["total"], 1)
        self.assertNotIn("cited", counter["items"][0])

    def test_cited_describes_the_deduplicated_public_quote_not_every_internal_classification(self):
        base = {"key": "big5_e", "kind": "support", "id": "same-message", "time": 1,
                "quote": "合成同一条原话", "reason": "合成同一理由"}
        rows = [{**base, "eid": "low-cited", "value": "低"},
                {**base, "eid": "low-other", "value": "低"},
                {**base, "eid": "high-other", "value": "高"}]
        inference = {"text": "合成描述", "position": -.5, "evidence": [], "counter": [],
                     "citedEvidenceIds": ["low-cited"]}
        values = {key: (None, inference if key == "big5_e" else None) for key, _ in self.persona.dimensions + self.persona.reports}
        self.persona.begin("snapshot", {})
        self.persona.publish("snapshot", values, {}, evidence=rows)
        for side in ("evidence", "counter"):
            page = self.persona.evidence_page("big5_e", side=side)
            self.assertEqual(page["total"], 1, "the existing public-quote deduplication stays unchanged")
            self.assertTrue(page["items"][0]["cited"], "this public quote was cited, not each internal value")

    def test_progress_counts_complete_original_messages_and_cached_sources_have_a_fixed_total(self):
        self.corpus.reset()
        self.core.live = {"friend": [message(1, "我喜欢先独处整理思路。" * 800)]}
        Reader(self.core, "synthetic-account", self.corpus, Context(), None).run()
        self.models.context_tokens = 4096
        result = self.run_persona()
        events = [row for row in self.progress if row.get("phase") in ("extracting", "reviewing")]
        self.assertGreater(sum(record["step"] == "self-persona-evidence" for record in self.models.requests), 1)
        self.assertTrue(all(row["unit"] == "条" and row["total"] == row["ownMessages"] == 1 for row in events))
        self.assertTrue(all(row["done"] == row["analyzedMessages"] in (0, 1) for row in events))
        self.assertTrue(all(row["done"] == 0 for row in events if row["phase"] == "reviewing"))
        self.assertEqual(events[-1]["done"], 1)
        self.assertEqual(result["coverage"]["analyzedMessages"], 1)
        self.assertGreater(events[-1]["batchesDone"], 1)
        before = len(self.models.requests)
        self.run_persona()
        self.assertEqual(len(self.models.requests), before)
        reused = [row for row in self.progress if row.get("unit") == "条"]
        self.assertEqual(reused[-1]["done"], 1)
        self.assertTrue(all(row["total"] == 1 for row in reused))

    def dimension(self, view, key):
        return next(item for item in view["dimensions"] if item["key"] == key)

    def replace(self, original, replacement):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE profile_corpus_messages SET text=? WHERE text=?", (replacement, original))
            self.store.db.execute("UPDATE profile_corpus_context SET text=? WHERE text=?", (replacement, original))
            self.corpus._changed()

    def test_internal_evidence_ids_are_scrubbed_from_every_displayed_conclusion(self):
        # The synthesis model sees each evidence row's id and sometimes copies it into
        # the prose (live 2026-10-29). Stored rows keep their ids; every read is clean.
        cases = {
            "主张表达自由（sp_c9ecdaf7adf06a5ce3d10729），反对思想钳制。": "主张表达自由，反对思想钳制。",
            "见sp_c9ecdaf7adf06a5ce3d10729、sp_ffffffffffffffffffffffff都支持": "都支持",
            "证据编号：sp_c9ecdaf7adf06a5ce3d10729表明倾向": "表明倾向",
            "倾向明显，sp_c9ecdaf7adf06a5ce3d10729": "倾向明显",
            "他反对管制，sp_c9ecdaf7adf06a5ce3d10729：本人多次强调。": "他反对管制，本人多次强调。",
            "普通推论正文没有任何编号": "普通推论正文没有任何编号",
        }
        for dirty, clean in cases.items():
            self.assertEqual(scrub_display_text(dirty), clean)
        dirty = json.dumps({"text": "主张表达自由（sp_c9ecdaf7adf06a5ce3d10729）。",
                            "scope": "见sp_c9ecdaf7adf06a5ce3d10729"}, ensure_ascii=False)
        values_store = ValuesStore(self.store)
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO persona_ai VALUES('big5_e', NULL, ?)", (dirty,))
            self.store.db.execute("INSERT OR REPLACE INTO values_ai VALUES('love', NULL, ?)", (dirty,))
        inference = self.dimension(self.persona.view(), "big5_e")["inference"]
        self.assertEqual((inference["text"], inference["scope"]), ("主张表达自由。", ""))
        values = values_store.view()["dimensions"][0]["inference"]
        self.assertEqual(values["text"], "主张表达自由。")
        stored = self.store.rows("SELECT inference FROM persona_ai WHERE key='big5_e'")[0]["inference"]
        self.assertIn("sp_", stored, "the stored payload keeps its ids; only reads are scrubbed")

    def test_self_report_inference_and_manual_are_separate_and_only_self_text_is_sent(self):
        manual = self.persona.edit("big5_e", "人工补充不应进入模型", self.persona.view()["version"])
        pages = list(self.core.pages)
        result = self.run_persona()
        self.assertEqual(self.core.pages, pages, "Persona must use the shared corpus rather than reread history")
        self.assertEqual([row["label"] for row in result["dimensions"]], [label for _, label in DIMENSIONS])
        self.assertEqual(result["selfReports"]["mbti"]["text"], "INFP")
        extraversion = self.dimension(result, "big5_e")
        self.assertIsNotNone(extraversion["inference"])
        self.assertEqual(extraversion["manual"]["text"], "人工补充不应进入模型")
        self.assertNotIn("人工补充不应进入模型", json.dumps(self.models.requests, ensure_ascii=False))
        self.assertEqual(extraversion["inference"]["counts"], {"low": 2, "high": 1, "mixed": 0})
        self.assertEqual(extraversion["inference"]["position"], round(-1 / 5, 3), "counted, shrunk toward the middle")
        self.assertEqual(extraversion["inference"]["counterTotal"], 1)
        self.assertTrue(extraversion["inference"]["counter"])
        sent = [item for request in self.models.requests if request["step"] == "self-persona-evidence"
                for item in request["prompt"]["messages"]]
        self.assertNotIn("我是INTJ", [item["text"] for item in sent])
        self.assertEqual(result["coverage"]["analyzedMessages"], result["coverage"]["ownMessages"])
        self.assertGreater(result["version"], manual["version"])

    def test_manual_edit_conflict_and_removal_survive_ai_publication(self):
        first = self.persona.view()
        saved = self.persona.edit("att_anx", "我自己的理解", first["version"])
        with self.assertRaises(SelfError) as caught:
            self.persona.edit("att_anx", "过期覆盖", first["version"])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.dimension(self.persona.view(), "att_anx")["manual"]["text"], "我自己的理解")
        self.models.hook = lambda context, record: self.persona.edit("att_anx", "分析期间新补充", self.persona.view()["version"])
        result = self.run_persona()
        self.assertEqual(self.dimension(result, "att_anx")["manual"]["text"], "分析期间新补充")
        deleted = self.persona.edit("att_anx", "", result["version"])
        self.assertIsNone(self.dimension(deleted, "att_anx")["manual"])
        for key, text, version in (([], "文字", 0), ("other", "文字", 0), ("mbti", "文字", deleted["version"]),
                                   ("big5_e", "长" * 2001, 0), ("big5_e", "文字", True)):
            with self.assertRaises(SelfError):
                self.persona.edit(key, text, version)

    def test_unchanged_source_reuses_all_completed_model_calls_and_restart_keeps_them(self):
        result = self.run_persona()
        calls = len(self.models.requests)
        reopened = PersonaStore(self.store)
        self.assertEqual(reopened.view(), result)
        second = self.run_persona()
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual(second["version"], result["version"])
        self.assertEqual(second["selfReports"], result["selfReports"])

    def test_changed_chats_keep_every_conclusion_until_the_next_analysis_replaces_it(self):
        result = self.run_persona()
        self.persona.edit("big5_e", "人工解释保留", result["version"])
        inference = self.dimension(self.persona.view(), "big5_e")["inference"]
        self.assertIsNotNone(inference)
        self.replace("我是INFP", "我是INTJ")
        stale = self.persona.view()
        self.assertEqual(stale["selfReports"]["mbti"]["text"], "INFP")
        self.assertEqual(self.dimension(stale, "big5_e")["inference"], inference)
        self.assertTrue(stale["coverage"]["stale"])
        self.assertEqual(self.dimension(stale, "big5_e")["manual"]["text"], "人工解释保留")
        fresh = self.run_persona()
        self.assertEqual(fresh["selfReports"]["mbti"]["text"], "INTJ")
        self.assertNotIn("我是INFP", json.dumps(fresh["selfReports"], ensure_ascii=False))

    def test_results_of_the_earlier_version_are_dropped_but_corrections_kept(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO persona_ai VALUES('politics', ?, NULL)", (json.dumps({"text": "税收：支持累进税"}),))
            self.store.db.execute("INSERT INTO persona_manual VALUES('big5_e', '保留', 1)")
            self.store.db.execute("INSERT OR REPLACE INTO persona_meta VALUES('schema', '2')")
            self.store.db.execute("INSERT OR REPLACE INTO persona_meta VALUES('resultIdentity', '\"old\"')")
        view = PersonaStore(self.store).view()
        self.assertIsNone(view["selfReports"]["politics"])
        self.assertTrue(view["coverage"]["stale"])
        self.assertEqual(self.dimension(view, "big5_e")["manual"]["text"], "保留")

    def test_source_changes_during_model_request_cannot_commit_new_results(self):
        self.models.hook = lambda context, record: self.corpus._changed()
        with self.assertRaises(SelfError) as caught:
            self.run_persona()
        self.assertEqual(caught.exception.code, "source-changed")
        view = self.persona.view()
        self.assertTrue(all(item["inference"] is None for item in view["dimensions"]))
        self.assertTrue(all(value is None for value in view["selfReports"].values()))

    def test_a_run_stopped_by_changed_chats_leaves_the_published_conclusions(self):
        inference = self.dimension(self.run_persona(), "big5_e")["inference"]
        self.replace("我是INFP", "我是INTJ")
        self.models.hook = lambda context, record: self.corpus._changed()
        with self.assertRaises(SelfError):
            self.run_persona()
        self.assertEqual(self.dimension(self.persona.view(), "big5_e")["inference"], inference)

    def test_paused_batches_are_reused_and_changing_depth_reprocesses_them(self):
        # Small batches create a committed prefix before the next request stops.
        self.models.context_tokens = 8192
        original_generate = self.models.generate
        successful = []
        def stop(context, request, step=None):
            if step == "self-persona-evidence" and successful:
                raise Cancelled()
            result = original_generate(context, request, step)
            if step == "self-persona-review":
                successful.extend(item["id"] for item in json.loads(request["prompt"])["messages"])
            return result
        self.models.generate = stop
        with self.assertRaises(Cancelled):
            self.run_persona()
        self.assertTrue(successful)
        cutoff = len(self.models.requests)
        self.models.generate = original_generate
        result = self.run_persona()
        resent = [item["id"] for record in self.models.requests[cutoff:] if record["step"] == "self-persona-evidence"
                  for item in record["prompt"]["messages"]]
        self.assertTrue(set(successful).isdisjoint(resent))
        self.assertEqual(result["coverage"]["analyzedMessages"], result["coverage"]["ownMessages"])
        cutoff = len(self.models.requests)
        self.run_persona("basic")
        self.assertTrue(any(record["step"] == "self-persona-evidence" for record in self.models.requests[cutoff:]))

    def test_missing_or_rejected_evidence_leaves_inference_empty(self):
        self.models.accept = False
        result = self.run_persona()
        self.assertIsNone(self.dimension(result, "big5_e")["inference"])
        self.assertEqual(result["selfReports"]["mbti"]["text"], "INFP")
        # A single source message, even split into fragments, cannot make a stable inference.
        self.models.accept = True
        self.replace("我需要独处才能理清想法", "今天打算吃晚饭")
        self.replace("我也会主动社交", "今天天气不错")
        result = self.run_persona()
        self.assertIsNone(self.dimension(result, "big5_e")["inference"])

    def test_political_placement_is_a_self_report_and_issue_views_place_the_point(self):
        self.replace("我是INFP", "我自认偏左")
        self.replace("我喜欢先独处整理思路", "我支持累进税")
        self.replace("我需要独处才能理清想法", "累进税对普通人更公平")
        result = self.run_persona()
        self.assertEqual(result["selfReports"]["politics"]["text"], "进步")
        axis = self.dimension(result, "pol_axis")["inference"]
        self.assertLess(axis["position"], 0, "进步 is the low end")
        item = self.item("我在上海做程序员，朋友都很外向")
        for raw in (proof(0, "pol_axis", "support", "自由派", "我在上海做程序员，朋友都很外向"),
                    proof(0, "politics", "support", "进步", "我在上海做程序员，朋友都很外向")):
            self.assertEqual(parse_evidence([item])(json.dumps({"evidence": [raw]}, ensure_ascii=False)), [])

    def test_attachment_self_report_is_separate_and_inference_has_relationship_and_time(self):
        self.replace("我是INFP", "我是焦虑型依恋")
        self.replace("我喜欢先独处整理思路", "与伴侣沟通时我会明确问对方是否愿意继续聊")
        self.replace("我需要独处才能理清想法", "与伴侣争执后我会约好时间再谈，确认彼此的想法")
        result = self.run_persona()
        self.assertEqual(result["selfReports"]["attachment"]["text"], "焦虑型")
        row = self.dimension(result, "att_anx")
        self.assertIn("关系", row["inference"]["scope"])
        self.assertIsNotNone(row["inference"]["fromMs"])
        self.assertIsNotNone(row["inference"]["toMs"])

    def test_review_must_not_invent_or_upgrade_and_quotes_must_match_own_body(self):
        item = self.item("我喜欢先独处")
        row = proof(0)
        accepted = parse_evidence([item])(json.dumps({"evidence": [row]}, ensure_ascii=False))
        upgraded = {**row, "kind": "selfReport", "value": "INFP", "quote": "我是INFP"}
        with self.assertRaises(SelfError):
            parse_evidence([item], accepted)(json.dumps({"evidence": [upgraded]}, ensure_ascii=False))
        for field, invalid in (("quote", "他人的话"), ("i", True), ("key", {}), ("reason", "长" * 161)):
            with self.assertRaises(SelfError):
                parse_evidence([item])(json.dumps({"evidence": [{**row, field: invalid}]}, ensure_ascii=False))

    def test_hearsay_jokes_denial_and_hypotheticals_never_become_explicit_self_report(self):
        for text in ("朋友说我是INFP", "如果我是INFP就好了", "我是INFP，开玩笑的", "我不是INFP", "以前我是INFP"):
            row = proof(0, "mbti", "selfReport", "INFP", text)
            self.assertEqual(parse_evidence([self.item(text)])(json.dumps({"evidence": [row]}, ensure_ascii=False)), [], text)

    def test_questionnaire_self_report_keeps_a_non_diagnosis_disclaimer(self):
        text = "我做的依恋问卷结果是焦虑型，但这只是我的自述，不是临床诊断。"
        row = proof(0, "attachment", "selfReport", "焦虑型", text, scope="问卷自述，不是诊断")
        accepted = parse_evidence([self.item(text)])(json.dumps({"evidence": [row]}, ensure_ascii=False))
        self.assertEqual(accepted[0]["value"], "焦虑型")
        self.assertEqual(accepted[0]["scope"], "问卷自述，不是诊断")

    def test_explicit_political_self_position_is_a_report_and_never_proxy_support(self):
        for text, placement in (("我是自由派", "进步"), ("我自认偏左", "进步"), ("我认为我是保守派", "保守")):
            report = proof(0, "politics", "selfReport", text, text, topic="政治自我定位")
            accepted = parse_evidence([self.item(text)])(json.dumps({"evidence": [report]}, ensure_ascii=False))
            self.assertEqual(len(accepted), 1, text)
            self.assertEqual(accepted[0]["value"], placement)
            reviewed = parse_evidence([self.item(text)], accepted)(json.dumps({"evidence": accepted}, ensure_ascii=False))
            self.assertEqual(reviewed, accepted, "review copies the normalised placement")
            support = {**report, "kind": "support"}
            self.assertEqual(parse_evidence([self.item(text)])(json.dumps({"evidence": [support]}, ensure_ascii=False)), [], text)
        for text in ("朋友说我是自由派", "我不是自由派", "我是自由派，开玩笑的", "如果我是自由派呢？"):
            report = proof(0, "politics", "selfReport", "自由派", text, topic="政治自我定位")
            self.assertEqual(parse_evidence([self.item(text)])(json.dumps({"evidence": [report]}, ensure_ascii=False)), [], text)

    def test_evidence_output_is_bounded_and_preserves_context_attribution_and_loss_markers(self):
        context = [{"id": str(index), "seq": index, "time": 1000, "side": "other", "speaker": "某群友",
                    "text": "长" * 600, "truncated": False} for index in range(12)]
        item = self.item("我喜欢先独处")
        item["context"].update(before=context, after=context[:6], truncated=False)
        record = evidence_record(proof(0), item, "fingerprint")
        self.assertEqual((len(record["contextBefore"]), len(record["contextAfter"])), (12, 6))
        self.assertTrue(record["truncated"])
        self.assertTrue(all(len(row["text"]) <= 200 and row["truncated"] for row in record["contextBefore"]))
        self.assertEqual(record["contextBefore"][0]["speaker"], "某群友")
        self.assertTrue(refresh_context([record], item)[0]["truncated"])

    def test_audit_keeps_distant_context_until_the_actual_budget_requires_trimming(self):
        self.corpus.reset()
        rows = [message(1, "关键前文：这是在讨论朋友的情况", "other")]
        rows += [message(index, "继续说吧", "other") for index in range(2, 11)]
        rows += [message(11, "我喜欢先独处整理思路"), message(12, "我需要独处才能想清楚")]
        rows += [message(index, "还有其他补充吗", "other") for index in range(13, 16)]
        rows += [message(16, "关键后文：需要结合前面的讨论对象", "other")]
        self.core.live = {"friend-a": rows}
        Reader(self.core, "synthetic-account", self.corpus, Context(), None).run()
        self.run_persona()
        audit = next(record for record in self.models.requests if record["step"] == "self-persona-audit")
        evidence = audit["prompt"]["evidence"]
        self.assertTrue(any("关键前文" in line["text"] for item in evidence for line in item["contextBefore"]))
        self.assertTrue(any("关键后文" in line["text"] for item in evidence for line in item["contextAfter"]))

    def test_context_sequence_renumbering_reuses_semantics_but_refreshes_display_locations(self):
        item = self.item("我喜欢先独处")
        item["context"]["before"] = [{"id": "stable-context", "seq": 4, "time": 900,
                                           "side": "other", "speaker": "对方", "text": "你平常怎么恢复精力", "truncated": False}]
        changed = copy.deepcopy(item)
        changed["context"]["before"][0]["seq"] = 2
        self.assertEqual(source_identity(item), source_identity(changed))
        original = evidence_record(proof(0), item, "stable-fingerprint")
        refreshed = refresh_context([original], changed)[0]
        self.assertEqual(refreshed["contextBefore"][0]["seq"], 2)
        self.assertEqual(refreshed["eid"], original["eid"])
        changed["context"]["before"][0]["text"] = "这段其实说的是别人"
        self.assertNotEqual(source_identity(item), source_identity(changed))

    def test_audit_budget_trimming_cannot_mutate_the_saved_or_displayed_context(self):
        job = Persona(self.plugin, self.store, "synthetic-account", self.corpus, "deep")
        job.revision, job.source = self.corpus.meta("revision", 0), self.corpus.meta("source")
        job.room, job.output_tokens = 5000, 1024
        job.coverage = {"ownMessages": 2, "analyzedMessages": 2}
        items = []
        for index in range(2):
            item = self.item("我喜欢先独处")
            item["id"] = "message-" + str(index)
            lines = [{"id": str(i), "time": 900, "side": "other", "speaker": "对方",
                      "text": "前后文" * 60, "truncated": False} for i in range(12)]
            item["context"].update(before=lines, after=lines[:6])
            items.append(evidence_record(proof(0), item, "fingerprint-" + str(index)))
        saved = copy.deepcopy(items)
        job._infer(Context(), "big5_e", items)
        self.assertEqual(items, saved)
        audit = next(record for record in self.models.requests if record["step"] == "self-persona-audit")
        self.assertLessEqual(len(json.dumps(audit["prompt"], ensure_ascii=False)), job.room)
        self.assertTrue(any(item["truncated"] for item in audit["prompt"]["evidence"]))

    @staticmethod
    def item(text):
        return {"text": text, "user": "friend", "id": "message", "time": 1000, "part": 1, "parts": 1,
                "context": {"before": [], "after": [], "truncated": False, "timeUnknown": False}}


if __name__ == "__main__":
    unittest.main()
