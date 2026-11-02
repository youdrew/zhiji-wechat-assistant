"""Large-candidate SQL equivalence and bounded fresh verification, synthetic only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_self_analysis import BASE, Core, dialogue
from self_retrieval import Retrieval, digest
from self_samples import terms
from self_service import Service
from self_store import Store


class LexicalQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "synthetic-account")
        rng = random.Random(10622)
        tokens = terms("我有点累今天不想参加聚会吃饭 fixture")
        with self.store.lock, self.store.db:
            self.store._meta("source", "synthetic-source")
            for index in range(8):
                self.store.db.execute("INSERT INTO conversations(user,name) VALUES(?,?)", (f"u{index}", "合成朋友"))
            for index in range(1200):
                identifier = f"{index:064x}"
                self.store.db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    identifier, f"u{index % 8}", "other-source" if index % 17 == 0 else "synthetic-source", "epoch",
                    index, index + 1, BASE + index, "合成前文" * 80, f"合成回复{index}", "闲聊", str(index), "[]", "digest"))
                self.store.db.executemany("INSERT INTO terms VALUES(?,?)", [(token, identifier) for token in tokens if rng.random() < 0.7])
                if index % 37 == 0:
                    self.store.db.execute("INSERT INTO exclusions VALUES(?,?)", (identifier, BASE))
        self.retrieval = Retrieval(None, self.store, "synthetic-account")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def previous_query(self, tokens, conditions, values):
        conditions, values = list(conditions), list(values)
        order = "s.time_ms DESC"
        if tokens:
            placeholders = ",".join("?" for _ in tokens)
            conditions.append(f"s.id IN (SELECT sample_id FROM terms WHERE token IN ({placeholders}))")
            values.extend(tokens)
            order = ("(SELECT sum(CASE WHEN length(token)=1 THEN 0.22 ELSE 1 END) FROM terms "
                     f"WHERE sample_id=s.id AND token IN ({placeholders})) DESC,s.time_ms DESC")
            values.extend(tokens)
        return self.store.rows("SELECT s.*,c.name FROM samples s JOIN conversations c ON c.user=s.user WHERE " +
                               " AND ".join(conditions) + " ORDER BY " + order + " LIMIT 500", values)

    def test_common_tokens_preserve_full_candidate_order_cutoffs_and_limit(self):
        rng = random.Random(10623)
        available = terms("我有点累今天不想参加聚会吃饭 fixture")
        for index in range(40):
            tokens = rng.sample(available, rng.randrange(len(available) + 1))
            conditions = ["s.source=?", "s.time_ms<?", "NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)"]
            values = ["synthetic-source", BASE + rng.randrange(600, 1500)]
            if index % 2:
                conditions.append("(s.user<>? OR s.to_seq<?)")
                values.extend(("u1", 300))
            with self.subTest(query=index):
                expected = self.previous_query(tokens, conditions, values)
                actual = self.retrieval._lexical(tokens, conditions, values)
                self.assertEqual(actual, expected)
                self.assertLessEqual(len(actual), 500)

    def test_missing_term_and_empty_query_keep_their_existing_meaning(self):
        conditions = ["s.source=?", "NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)"]
        values = ["synthetic-source"]
        self.assertEqual(self.retrieval._lexical(["absent-token"], conditions, values), [])
        recent = self.retrieval._lexical([], conditions, values)
        self.assertEqual(len(recent), 500)
        self.assertEqual([row["time_ms"] for row in recent], sorted((row["time_ms"] for row in recent), reverse=True))

    def test_equal_timestamps_keep_existing_candidate_ties_at_the_500_cutoff(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE samples SET time_ms=?", (BASE + 1,))
        conditions = ["s.source=?", "NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)"]
        values = ["synthetic-source"]
        for tokens in ([], ["我"], ["我", "有", "我有", "fixture"]):
            with self.subTest(tokens=tokens):
                self.assertEqual(self.retrieval._lexical(tokens, conditions, values),
                                 self.previous_query(tokens, conditions, values))


class VerificationBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core({"friend-a": dialogue(8)})
        self.service = Service(SimpleNamespace(core=self.core, data_dir=Path(self.temp.name)))
        self.account = self.core.account
        self.service.build(self.account)
        self.store, worker = self.service.accounts[self.account]
        worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.rows = self.store.rows("SELECT * FROM samples ORDER BY time_ms DESC LIMIT 4")
        self.core.verifies.clear()
        self.retrieval = Retrieval(self.core, self.store, self.account)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_same_contact_shares_status_and_deduplicated_fresh_proofs_per_batch(self):
        with patch.object(self.core, "history_status", wraps=self.core.history_status) as status:
            self.assertEqual(self.retrieval._verify_many(self.rows), [True] * 4)
        self.assertEqual(status.call_count, 2)
        self.assertEqual(len(self.core.verifies), 1)
        request = self.core.verifies[0]
        original_count = sum(len(json.loads(row["evidence"])) for row in self.rows)
        self.assertTrue(request["fresh"])
        self.assertLess(len(request["items"]), original_count)
        self.assertEqual(len(request["items"]), len({item["id"] for item in request["items"]}))
        # Fresh evidence is never reused across calls, even on this Retrieval.
        self.assertEqual(self.retrieval._verify_many(self.rows), [True] * 4)
        self.assertEqual(len(self.core.verifies), 2)

    def test_response_order_is_mapped_by_id_and_malformed_mappings_fail_closed(self):
        post = self.core.post
        with patch.object(self.core, "post", side_effect=lambda path, body: {"results": list(reversed(post(path, body)["results"]))}):
            self.assertEqual(self.retrieval._verify_many(self.rows), [True] * 4)
        def bad_response(kind, path, body):
            result = post(path, body)
            if kind == "unknown-id":
                result["results"][0]["id"] = "unrequested-proof"
            elif kind == "duplicate":
                result["results"][-1] = result["results"][0]
            elif kind == "unknown-state":
                result["results"][0]["state"] = "unexpected"
            else:
                result["results"].pop()
            return result
        for kind in ("unknown-id", "duplicate", "unknown-state", "missing-result"):
            with self.subTest(kind=kind), patch.object(self.core, "post", side_effect=lambda path, body: bad_response(kind, path, body)):
                self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)
        self.assertEqual(len(self.store.rows("SELECT id FROM samples")), 8)

    def test_changed_original_invalidates_every_sample_for_contact(self):
        self.core.live["friend-a"] = []
        self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)
        self.assertEqual(self.store.rows("SELECT id FROM samples"), [])

    def test_unready_original_does_not_delete_the_library(self):
        def unavailable(path, body):
            return {"results": [{"id": item["id"], "state": "unready"} for item in body["items"]]}
        with patch.object(self.core, "post", side_effect=unavailable):
            self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)
        self.assertEqual(len(self.store.rows("SELECT id FROM samples")), 8)

    def test_epoch_or_source_change_is_rejected_before_original_reads(self):
        self.core.epochs["friend-a"] += 1
        self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)
        self.assertEqual(self.core.verifies, [])
        self.assertEqual(self.store.rows("SELECT id FROM samples"), [])

    def test_moved_source_identity_rejects_rows_but_keeps_the_library(self):
        # 换一个 TEMP 启动客户端，整个账号的来源标识都会变，聊天内容却没变。
        status = self.core.history_status
        for good_calls in (0, 1):
            calls = iter(range(99))
            def moved(*args, **kwargs):
                row = status(*args, **kwargs)
                return row if next(calls) < good_calls else {**row, "currentSourceFingerprint": "moved"}
            with self.subTest(good_calls=good_calls), patch.object(self.core, "history_status", side_effect=moved):
                self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)
                self.assertEqual(len(self.store.rows("SELECT id FROM samples")), 8)
        self.assertEqual(self.retrieval._verify_many(self.rows), [True] * 4, "the old identity coming back needs no rebuild")

    def test_projection_epoch_change_during_original_reads_is_rejected(self):
        post = self.core.post
        def changed_epoch(path, body):
            result = post(path, body)
            self.core.epochs[body["user"]] += 1
            return result
        with patch.object(self.core, "post", side_effect=changed_epoch):
            self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)
        self.assertEqual(self.store.rows("SELECT id FROM samples"), [])

    def test_exclusion_and_text_change_during_fresh_reads_are_rechecked(self):
        post = self.core.post
        def changed(path, body):
            result = post(path, body)
            self.store.exclude(self.rows[0]["id"], True)
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE samples SET reply='合成编辑' WHERE id=?", (self.rows[1]["id"],))
            return result
        with patch.object(self.core, "post", side_effect=changed):
            self.assertEqual(self.retrieval._verify_many(self.rows), [False, False, True, True])

    def test_reset_during_fresh_reads_cannot_return_earlier_candidates(self):
        post = self.core.post
        def reset(path, body):
            result = post(path, body)
            self.store.reset()
            return result
        with patch.object(self.core, "post", side_effect=reset):
            self.assertEqual(self.retrieval._verify_many(self.rows), [False] * 4)

    def test_validate_batches_refs_and_search_limit_one_does_not_verify_extra_candidates(self):
        refs = [{"id": row["id"], "hash": digest(row)} for row in self.rows]
        self.assertTrue(self.retrieval.validate(refs))
        self.assertEqual(len(self.core.verifies), 1)
        self.core.verifies.clear()
        result = self.service.search(self.account, {"query": "周末一起吃饭", "limit": 1})
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(len(self.core.verifies), 1)
        selected = self.store.rows("SELECT evidence FROM samples WHERE id=?", (result["sources"][0]["id"],))[0]
        self.assertEqual(len(self.core.verifies[0]["items"]), len(json.loads(selected["evidence"])))

    def test_contact_diversity_cap_does_not_read_unselected_third_sample(self):
        result = self.service.search(self.account, {"query": "周末一起吃饭", "limit": 6})
        self.assertEqual(len(result["sources"]), 2)
        self.assertEqual(len(self.core.verifies), 1)
        proofs = set()
        for source in result["sources"]:
            row = self.store.rows("SELECT evidence FROM samples WHERE id=?", (source["id"],))[0]
            proofs.update(item["id"] for item in json.loads(row["evidence"]))
        self.assertEqual({item["id"] for item in self.core.verifies[0]["items"]}, proofs)

    def combined_rows(self):
        self.core.live["friend-b"] = dialogue(8, prefix="另一组合成回复")
        self.service.build(self.account)
        self.service.accounts[self.account][1].thread.join(5)
        self.core.verifies.clear()
        return [*self.store.rows("SELECT * FROM samples WHERE user='friend-a' ORDER BY time_ms DESC LIMIT 2"),
                *self.store.rows("SELECT * FROM samples WHERE user='friend-b' ORDER BY time_ms DESC LIMIT 2")]

    def combined_response(self, account, groups):
        return {"results": [{"user": group["user"], **self.core.post("history/verify", {
            "account": account, "fresh": True, **group})} for group in groups]}

    def test_combined_api_reads_multiple_contacts_once_without_cross_call_cache(self):
        rows = self.combined_rows()
        with patch.object(self.core, "history_verify_many", side_effect=self.combined_response, create=True) as verify:
            self.assertEqual(self.retrieval._verify_many(rows), [True] * 4)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(verify.call_args.args[0], self.account)
            groups = verify.call_args.args[1]
            self.assertEqual({group["user"] for group in groups}, {"friend-a", "friend-b"})
            for group in groups:
                self.assertEqual(len(group["items"]), len({item["id"] for item in group["items"]}))
            self.assertEqual(self.retrieval._verify_many(rows), [True] * 4)
            self.assertEqual(verify.call_count, 2)

    def test_combined_api_maps_reordered_contact_and_proof_results(self):
        rows = self.combined_rows()
        def reversed_response(account, groups):
            response = self.combined_response(account, groups)
            response["results"].reverse()
            for group in response["results"]:
                group["results"].reverse()
            return response
        with patch.object(self.core, "history_verify_many", side_effect=reversed_response, create=True):
            self.assertEqual(self.retrieval._verify_many(rows), [True] * 4)

    def test_combined_api_rejects_duplicate_missing_unknown_and_malformed_contact_groups(self):
        rows = self.combined_rows()
        def malformed(kind, account, groups):
            response = self.combined_response(account, groups)
            if kind == "duplicate":
                response["results"][1] = response["results"][0]
            elif kind == "missing":
                response["results"].pop()
            elif kind == "unknown":
                response["results"][0]["user"] = "unrequested-contact"
            elif kind == "malformed":
                response["results"][0] = {"user": []}
            else:
                response["results"] = None
            return response
        for kind in ("duplicate", "missing", "unknown", "malformed", "not-list"):
            with self.subTest(kind=kind), patch.object(self.core, "history_verify_many", create=True,
                    side_effect=lambda account, groups: malformed(kind, account, groups)):
                self.assertEqual(self.retrieval._verify_many(rows), [False] * 4)
        self.assertEqual(len(self.store.rows("SELECT id FROM samples")), 16)

    def test_combined_api_keeps_proof_ids_scoped_to_the_requested_contact(self):
        rows = self.combined_rows()
        def wrong_proofs(account, groups):
            response = self.combined_response(account, groups)
            response["results"][0]["results"][0]["id"] = "wrong-proof"
            return response
        with patch.object(self.core, "history_verify_many", side_effect=wrong_proofs, create=True):
            self.assertEqual(self.retrieval._verify_many(rows), [False, False, True, True])

    def test_combined_api_rechecks_each_contact_epoch_after_reading_all_originals(self):
        rows = self.combined_rows()
        def changed_epoch(account, groups):
            response = self.combined_response(account, groups)
            self.core.epochs["friend-b"] += 1
            return response
        with patch.object(self.core, "history_verify_many", side_effect=changed_epoch, create=True):
            self.assertEqual(self.retrieval._verify_many(rows), [True, True, False, False])
        self.assertEqual(len(self.store.rows("SELECT id FROM samples WHERE user='friend-a'")), 8)
        self.assertEqual(self.store.rows("SELECT id FROM samples WHERE user='friend-b'"), [])


if __name__ == "__main__":
    unittest.main()
