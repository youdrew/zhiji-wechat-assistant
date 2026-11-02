"""Live comparison contracts over synthetic chat and a deterministic fake encoder.

These tests check route isolation, shared eligibility and current evidence, not
the semantic quality of an actual model. No chat database or download is used.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_hybrid import EncoderCore
from test_self_analysis import BASE, dialogue, message
from kyf_plugin import CoreError
from self_comparison import Comparison, LexicalOnlyCore
from self_contracts import SelfError
from self_retrieval import Retrieval
from self_service import Service


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = EncoderCore({
            "meal-friend": [message(1, "other", "共进晚餐"), message(2, "self", "好呀几点")],
            "work-friend": [message(1, "other", "电脑黑屏"), message(2, "self", "先重启看看")],
            "tool-friend": [message(1, "other", "螺丝刀"), message(2, "self", "在抽屉里")],
        })
        self.account = self.core.account
        self.service = Service(SimpleNamespace(core=self.core, data_dir=Path(self.temp.name)))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def build(self):
        self.service.build(self.account)
        store, history = self.service.accounts[self.account]
        history.thread.join(5)
        self.service.embeddings[self.account].thread.join(5)
        self.assertFalse(history.thread.is_alive())
        self.assertFalse(self.service.embeddings[self.account].thread.is_alive())
        return store

    def test_same_query_production_routes_differ_without_shared_core_mutation_or_extra_encoding(self):
        store = self.build()
        params = {"query": "腹中空空", "beforeTimeMs": BASE + 100000, "limit": 4}
        before = list(self.core.encoded)
        prepares = self.core.prepare_calls
        value = self.service.compare(self.account, params, "current-friend")
        self.assertTrue(value["consistent"])
        self.assertEqual(value["results"]["lexical"]["sources"], [])
        sources = value["results"]["hybrid"]["sources"]
        self.assertEqual([row["user"] for row in sources], ["meal-friend"])
        self.assertEqual(sources[0]["matchKinds"], ["semantic"])
        self.assertEqual(value["overlap"]["hybridOnlyIds"], [sources[0]["id"]])
        self.assertEqual(value["overlap"]["sharedIds"], [])
        self.assertEqual(self.core.encoded[len(before):], [("query", [params["query"]])])
        self.assertEqual(self.core.prepare_calls, prepares, "comparison must not start a download/build")
        self.assertTrue(value["semantic"]["ready"])
        self.assertTrue(value["semantic"]["used"])
        self.assertEqual(value["semantic"]["scanned"], 3)
        self.assertEqual(value["semantic"]["indexedSamples"], 3)
        self.assertEqual(value["results"]["hybrid"]["mode"], "hybrid")
        self.assertEqual(sources, Retrieval(self.core, store, self.account).search(params, "current-friend"))
        self.assertTrue(all(call["fresh"] for call in self.core.verifies))
        self.assertLessEqual(value["timings"]["lexicalMs"] + value["timings"]["hybridMs"],
                             value["timings"]["totalMs"] + 0.002)

    def test_exact_keywords_and_overlap_match_the_real_lexical_fallback(self):
        store = self.build()
        params = {"query": "螺丝刀", "beforeTimeMs": BASE + 100000}
        value = self.service.compare(self.account, params)
        keyword_sources = value["results"]["lexical"]["sources"]
        self.assertEqual(keyword_sources[0]["user"], "tool-friend")
        self.assertEqual(keyword_sources, Retrieval(LexicalOnlyCore(self.core), store, self.account).search({**params, "limit": 4}))
        self.assertEqual(value["overlap"]["sharedIds"], [keyword_sources[0]["id"]])
        self.assertTrue(self.core.embedding_status(self.account)["ready"], "the adapter must remain local to one request")

    def test_unready_failed_or_missing_numeric_service_is_truthful_keyword_fallback(self):
        self.build()
        self.core.embedding_state = "downloading"
        unready = self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertFalse(unready["semantic"]["ready"])
        self.assertFalse(unready["semantic"]["used"])
        self.assertEqual(unready["semantic"]["state"], "downloading")
        self.assertEqual(unready["results"]["lexical"]["sources"], unready["results"]["hybrid"]["sources"])
        self.core.embedding_state = "ready"
        self.core.embedding_error = True
        failed = self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertEqual(failed["semantic"]["state"], "fallback")
        self.assertEqual(failed["results"]["hybrid"]["mode"], "lexical")
        self.assertNotIn("private-path", json.dumps(failed))
        self.core.embedding_error = False
        with patch("self_semantic.numerical_runtime", return_value=None), \
                patch("self_comparison.numerical_runtime", return_value=None):
            missing = self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertFalse(missing["semantic"]["used"])
        self.assertFalse(missing["semantic"]["ready"])
        self.assertTrue(missing["results"]["hybrid"]["available"])

    def test_partial_scan_is_reported_and_each_branch_keeps_its_own_verification_window(self):
        self.core.live["extra-friend"] = dialogue(520, topic="腹中空空", prefix="合成回复")
        self.build()
        with patch("self_semantic.SCAN_SECONDS", 0):
            value = self.service.compare(self.account, {"query": "腹中空空", "limit": 8})
        self.assertTrue(value["semantic"]["partial"])
        self.assertEqual(value["semantic"]["state"], "partial")
        for result in value["results"].values():
            self.assertLessEqual(len(result["sources"]), 8)
            self.assertLessEqual(sum(row["user"] == "extra-friend" for row in result["sources"]), 2)
            self.assertEqual(len({row["reply"] for row in result["sources"]}), len(result["sources"]))

    def test_shared_cutoffs_exclusions_and_source_scope_apply_to_both_columns(self):
        store = self.build()
        value = self.service.compare(self.account, {"query": "腹中空空", "beforeTimeMs": BASE + 1000})
        self.assertFalse(value["available"])
        same = self.service.compare(self.account, {"query": "腹中空空", "beforeSeq": 2}, "meal-friend")
        self.assertFalse(same["available"])
        target = self.service.compare(self.account, {"query": "腹中空空", "messageId": "m2"}, "meal-friend")
        self.assertFalse(target["available"])
        identifier = store.rows("SELECT id FROM samples WHERE user='meal-friend'")[0]["id"]
        self.service.exclude(self.account, identifier)
        self.assertFalse(self.service.compare(self.account, {"query": "腹中空空"})["available"])
        mismatch = self.service.compare(self.account, {"query": "螺丝刀", "sourceFingerprint": "another-source"})
        self.assertFalse(mismatch["consistent"])
        self.assertEqual(mismatch["reason"], "source-changed")
        self.assertFalse(mismatch["available"])

    def test_changed_original_between_branches_clears_both_columns(self):
        self.build()
        def change(kind, texts):
            if kind == "query":
                self.core.live["tool-friend"][1] = message(2, "self", "原文已经改过")
        self.core.encoder_hook = change
        value = self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertFalse(value["consistent"])
        self.assertEqual(value["reason"], "library-changed")
        self.assertTrue(all(not result["sources"] for result in value["results"].values()))

    def test_union_verification_rejects_an_original_deleted_after_both_searches(self):
        self.build()
        original = Comparison._semantic_status
        def deleted_after_searches(comparison, retrieval):
            metadata = original(comparison, retrieval)
            self.core.live["tool-friend"] = []
            return metadata
        with patch.object(Comparison, "_semantic_status", autospec=True, side_effect=deleted_after_searches):
            value = self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertFalse(value["consistent"])
        self.assertFalse(value["available"])
        self.assertEqual(value["overlap"]["sharedIds"], [])

    def test_live_source_change_and_revoked_plugin_do_not_return_old_results(self):
        self.build()
        original_source = self.core.source
        self.core.encoder_hook = lambda kind, texts: setattr(self.core, "source", "replaced-source") if kind == "query" else None
        changed = self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertFalse(changed["consistent"])
        self.assertFalse(changed["available"])
        self.core.source = original_source
        self.core.encoder_hook = None
        self.build()
        # Like the actual Core client, a disabled plugin rejects later API reads.
        def disabled_scope():
            raise CoreError(403, "plugin-disabled", "插件已停用")
        def revoked(kind, texts):
            if kind == "query":
                self.core.scope = disabled_scope
        self.core.encoder_hook = revoked
        with self.assertRaises(CoreError) as error:
            self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertEqual(error.exception.code, "plugin-disabled")

    def test_reset_or_new_index_revision_in_flight_never_mixes_library_snapshots(self):
        store = self.build()
        for mutation in (lambda: self.service.reset(self.account), lambda: self._bump(store)):
            with self.subTest(mutation=mutation):
                if not store.counts()["samples"]:
                    self.build()
                self.core.encoder_hook = lambda kind, texts: mutation() if kind == "query" else None
                value = self.service.compare(self.account, {"query": "螺丝刀"})
                self.assertFalse(value["consistent"])
                self.assertFalse(value["available"])
        self.core.encoder_hook = None

    @staticmethod
    def _bump(store):
        with store.lock, store.db:
            store.bump()

    def test_no_account_empty_query_limit_and_account_switch_are_rejected(self):
        for account in (None, ""):
            with self.subTest(account=account), self.assertRaises(SelfError):
                self.service.compare(account, {"query": "查找场景"})
        for query in (None, "", " \t\n", "！？", "🙂", "字" * 4001):
            with self.subTest(query=query), self.assertRaises(SelfError):
                self.service.compare(self.account, {"query": query})
        for limit in (0, 9, True):
            with self.subTest(limit=limit), self.assertRaises(SelfError):
                self.service.compare(self.account, {"query": "查找场景", "limit": limit})
        self.assertEqual(self.service.accounts, {}, "invalid requests do not create an account database")
        self.build()
        self.core.encoder_hook = lambda kind, texts: setattr(self.core, "account", "another-account") if kind == "query" else None
        with self.assertRaises(SelfError) as error:
            self.service.compare(self.account, {"query": "螺丝刀"})
        self.assertEqual(error.exception.code, "account-changed")

    def test_empty_library_is_readable_without_implicitly_building_or_downloading(self):
        started = time.monotonic()
        value = self.service.compare(self.account, {"query": "周末一起吃饭"})
        self.assertTrue(value["consistent"])
        self.assertFalse(value["available"])
        self.assertEqual(value["semantic"]["state"], "empty")
        self.assertEqual(value["semantic"]["indexedSamples"], 0)
        self.assertEqual(self.core.prepare_calls, 0)
        self.assertEqual(self.core.encoded, [])
        self.assertEqual(self.core.pages, [])
        self.assertGreaterEqual(value["beforeTimeMs"], BASE)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
