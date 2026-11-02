"""Semantic routes use a deterministic fake encoder, never a downloaded model.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import closing

from test_self_analysis import BASE, Core, dialogue, message
from self_service import Service
from self_vectors import DIMENSIONS, VectorStore, input_digest, model_info
from kyf_plugin import CoreError
import self_semantic


class EncoderCore(Core):
    def __init__(self, live=None):
        super().__init__(live)
        self.embedding_state = "ready"
        self.model_revision = "fixture-r1"
        self.embedding_error = False
        self.prepare_calls = 0
        self.encoded = []
        self.encoder_hook = None
        self.bad_vector = None

    def embedding_status(self, account):
        return {"state": self.embedding_state, "ready": self.embedding_state == "ready", "modelId": "fixture-bge",
                "revision": self.model_revision, "dimensions": DIMENSIONS,
                "totalBytes": 1000, "downloadedBytes": 1000 if self.embedding_state == "ready" else 300}

    def embedding_prepare(self, account):
        self.prepare_calls += 1
        return self.embedding_status(account)

    def embed(self, account, texts, kind="passage"):
        if self.embedding_error:
            raise RuntimeError("private-path must not appear in status")
        self.encoded.append((kind, copy.deepcopy(texts)))
        if self.encoder_hook:
            self.encoder_hook(kind, texts)
        encoded = []
        for text in texts:
            semantic = 0 if "腹中" in text or "共进晚餐" in text else 1 if "设备" in text or "电脑" in text else 2
            if kind == "query" and text == "螺丝刀":
                semantic = 0  # deliberately conflicting semantic result: exact keywords still matter
            vector = [0.0] * DIMENSIONS
            vector[semantic] = 1.0
            encoded.append(self.bad_vector if self.bad_vector is not None else vector)
        return {"modelId": "fixture-bge", "revision": self.model_revision, "dimensions": DIMENSIONS, "vectors": encoded}


class HybridTests(unittest.TestCase):
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

    def build(self, mode="incremental"):
        self.service.build(self.account, mode)
        store, worker = self.service.accounts[self.account]
        worker.thread.join(5)
        self.service.embeddings[self.account].thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(self.service.embeddings[self.account].thread.is_alive())
        return store, self.service.status(self.account)

    def test_unrelated_characters_synonymous_situation_recalls_other_contact(self):
        store, status = self.build()
        self.assertEqual(status["retrieval"]["mode"], "hybrid")
        self.assertEqual(status["retrieval"]["indexedSamples"], 3)
        result = self.service.search(self.account, {"query": "腹中空空"}, "current-friend")
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["sources"][0]["user"], "meal-friend")
        self.assertEqual(result["sources"][0]["matchKinds"], ["semantic"])
        self.assertFalse(result["sources"][0]["sameContact"])
        passages = [text for kind, texts in self.core.encoded if kind == "passage" for text in texts]
        self.assertTrue(all(any(word in text for word in ("共进晚餐", "电脑黑屏", "螺丝刀")) for text in passages))
        self.assertFalse(any("好呀几点" in text or "在抽屉里" in text for text in passages))

    def test_exact_keyword_route_survives_conflicting_semantic_match(self):
        self.build()
        value = self.service.search(self.account, {"query": "螺丝刀"})
        self.assertEqual(value["sources"][0]["user"], "tool-friend")
        self.assertIn("lexical", value["sources"][0]["matchKinds"])

    def test_downloading_or_failed_encoder_keeps_lexical_library_usable(self):
        self.core.embedding_state = "downloading"
        self.service.build(self.account)
        store, worker = self.service.accounts[self.account]
        worker.thread.join(5)
        status = self.service.status(self.account)
        self.assertEqual(status["build"]["state"], "complete")
        self.assertEqual(status["retrieval"]["mode"], "lexical")
        self.assertEqual(status["embedding"]["downloadedBytes"], 300)
        self.assertTrue(self.service.search(self.account, {"query": "螺丝刀"})["available"])
        self.core.embedding_state = "ready"
        self.service.embeddings[self.account].thread.join(5)
        self.assertEqual(self.service.status(self.account)["retrieval"]["mode"], "hybrid")
        self.core.embedding_error = True
        failed = self.service.search(self.account, {"query": "螺丝刀"})
        self.assertEqual(failed["mode"], "lexical")
        self.assertEqual(failed["sources"][0]["user"], "tool-friend")

    def test_legacy_schema_migrates_and_backfills_without_reprocessing_history(self):
        store, _ = self.build()
        old_pages = len(self.core.pages)
        self.service.close()
        # Recreate the previous release's schema, retaining all lexical data.
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("DROP TABLE embeddings")
            db.execute("ALTER TABLE samples DROP COLUMN input_digest")
            db.execute("PRAGMA user_version=1")
        self.service = Service(SimpleNamespace(core=self.core, data_dir=Path(self.temp.name)))
        store, status = self.build()
        self.assertEqual(len(self.core.pages), old_pages)
        self.assertEqual(status["retrieval"]["indexedSamples"], 3)
        self.assertTrue(all(row["input_digest"] == input_digest(row["input"]) for row in store.rows("SELECT * FROM samples")))
        self.assertEqual(store.rows("PRAGMA user_version")[0]["user_version"], 2)

    def test_model_revision_requires_reembedding_existing_samples(self):
        store, _ = self.build()
        calls = len(self.core.pages)
        self.core.model_revision = "fixture-r2"
        status = self.service.status(self.account)
        self.assertEqual(status["retrieval"]["pendingSamples"], 3)
        self.assertFalse(status["retrieval"]["semanticReady"])
        _, updated = self.build()
        self.assertEqual(len(self.core.pages), calls)
        self.assertEqual(updated["retrieval"]["pendingSamples"], 0)
        self.assertEqual({row["model_revision"] for row in store.rows("SELECT * FROM embeddings")}, {"fixture-r2"})

    def test_semantic_completion_only_embeds_missing_vectors_and_preserves_history(self):
        store, _ = self.build()
        history_thread = self.service.accounts[self.account][1].thread
        identifier = store.rows("SELECT id FROM samples ORDER BY id LIMIT 1")[0]["id"]
        with store.lock, store.db:
            store.db.execute("DELETE FROM embeddings WHERE sample_id=?", (identifier,))
        snapshot = {table: store.rows(f"SELECT * FROM {table}") for table in
                    ("samples", "conversations", "proofs", "terms", "exclusions")}
        build = store.meta("build")
        generation, revision = store.meta("generation"), store.meta("revision")
        self.core.encoded.clear()
        # A projection change would invalidate samples if a history rescan ran.
        self.core.epochs = {user: epoch + 1 for user, epoch in self.core.epochs.items()}
        with patch.object(self.core, "history_conversations", side_effect=AssertionError("unexpected discovery")) as discover, \
             patch.object(self.core, "history_prepare", side_effect=AssertionError("unexpected history read")) as prepare:
            self.service.build(self.account, "semantic")
            self.service.embeddings[self.account].thread.join(5)
            discover.assert_not_called()
            prepare.assert_not_called()
        self.assertIs(self.service.accounts[self.account][1].thread, history_thread)
        self.assertEqual(store.meta("build"), build)
        self.assertEqual(store.meta("generation"), generation)
        self.assertEqual(store.meta("revision"), revision + 1)  # vector coverage changed
        for table, rows in snapshot.items():
            self.assertEqual(store.rows(f"SELECT * FROM {table}"), rows)
        self.assertEqual(sum(len(texts) for kind, texts in self.core.encoded if kind == "passage"), 1)
        self.assertEqual(self.service.status(self.account)["retrieval"]["pendingSamples"], 0)

    def test_transient_embedding_and_status_failures_retry_the_missing_batch(self):
        store, _ = self.build()
        with store.lock, store.db:
            store.db.execute("DELETE FROM embeddings")
        original_embed, original_status = self.core.embed, self.core.embedding_status
        calls = {"embed": 0, "status": 0}
        def embed(*args, **kwargs):
            calls["embed"] += 1
            if calls["embed"] == 1:
                raise CoreError(503, "embedding-timeout", "PRIVATE_DIAGNOSTIC_MUST_NOT_BE_SAVED")
            return original_embed(*args, **kwargs)
        def status(*args, **kwargs):
            calls["status"] += 1
            if calls["status"] == 2:
                raise CoreError(0, "core-unavailable", "PRIVATE_DIAGNOSTIC_MUST_NOT_BE_SAVED")
            return original_status(*args, **kwargs)
        with patch("self_embedding.RETRY_DELAYS", (0, 0)), patch.object(self.core, "embed", side_effect=embed), \
             patch.object(self.core, "embedding_status", side_effect=status):
            self.service.build(self.account, "semantic")
            self.service.embeddings[self.account].thread.join(3)
        self.assertEqual(calls["embed"], 2)
        self.assertEqual(store.meta("embeddingRun")["state"], "ready")
        self.assertIsNone(store.meta("embeddingRun")["errorCode"])
        self.assertEqual(len(store.rows("SELECT sample_id FROM embeddings")), 3)

    def test_exhausted_transient_retries_are_bounded_and_save_only_the_safe_error_code(self):
        store, _ = self.build()
        with store.lock, store.db:
            store.db.execute("DELETE FROM embeddings")
        error = CoreError(0, "core-unavailable", "PRIVATE_DIAGNOSTIC_MUST_NOT_BE_SAVED")
        with patch("self_embedding.RETRY_DELAYS", (0, 0)), patch.object(self.core, "embed", side_effect=error) as embed:
            self.service.build(self.account, "semantic")
            self.service.embeddings[self.account].thread.join(3)
        saved = store.meta("embeddingRun")
        self.assertEqual(embed.call_count, 3)
        self.assertEqual((saved["state"], saved["errorCode"]), ("error", "core-unavailable"))
        self.assertNotIn("PRIVATE_DIAGNOSTIC", json.dumps(saved))
        self.assertFalse(store.rows("SELECT sample_id FROM embeddings"))
        with patch.object(self.core, "embed", side_effect=CoreError(400, "embedding-invalid", "private")) as invalid:
            self.service.build(self.account, "semantic")
            self.service.embeddings[self.account].thread.join(3)
        self.assertEqual(invalid.call_count, 1, "invalid input is not a transient connection failure")
        self.assertEqual(store.meta("embeddingRun")["errorCode"], "embedding-invalid")

    def test_embedding_retry_backoff_is_cancelled_promptly(self):
        store, _ = self.build()
        with store.lock, store.db:
            store.db.execute("DELETE FROM embeddings")
        entered = threading.Event()
        def unavailable(*args, **kwargs):
            entered.set()
            raise CoreError(0, "core-unavailable", "private")
        with patch("self_embedding.RETRY_DELAYS", (30,)), patch.object(self.core, "embed", side_effect=unavailable) as embed:
            self.service.build(self.account, "semantic")
            self.assertTrue(entered.wait(3))
            self.service.pause(self.account)
            self.service.embeddings[self.account].thread.join(1)
            self.assertFalse(self.service.embeddings[self.account].thread.is_alive())
        self.assertEqual(embed.call_count, 1)
        self.assertEqual(store.meta("embeddingRun")["state"], "paused")

    def test_semantic_pause_and_resume_do_not_restart_completed_history(self):
        self.core.live = {"meal-friend": dialogue(23)}
        self.core.embedding_error = True
        store, _ = self.build()
        build = store.meta("build")
        self.core.embedding_error = False
        entered, release = threading.Event(), threading.Event()
        calls = 0
        def hold_second(kind, texts):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                release.wait(5)
        self.core.encoder_hook = hold_second
        with patch.object(self.core, "history_conversations") as discover, patch.object(self.core, "history_prepare") as prepare:
            self.service.build(self.account, "semantic")
            try:
                self.assertTrue(entered.wait(3))
                self.service.pause(self.account)
                completed = len(store.rows("SELECT * FROM embeddings"))
                self.assertGreater(completed, 0)
            finally:
                release.set()
            self.service.embeddings[self.account].thread.join(5)
            self.assertEqual(len(store.rows("SELECT * FROM embeddings")), completed)
            self.core.encoder_hook = None
            self.service.resume(self.account)
            self.service.embeddings[self.account].thread.join(5)
            discover.assert_not_called()
            prepare.assert_not_called()
        self.assertEqual(store.meta("build"), build)
        self.assertEqual(self.service.status(self.account)["retrieval"]["indexedSamples"], 23)

    def test_late_embedding_response_cannot_repopulate_reset_or_excluded_sample(self):
        entered, release = threading.Event(), threading.Event()
        def block(kind, texts):
            if kind == "passage":
                entered.set(); release.wait(4)
        self.core.encoder_hook = block
        self.service.build(self.account)
        self.assertTrue(entered.wait(3))
        store, history = self.service.accounts[self.account]
        self.service.reset(self.account)
        release.set()
        self.service.embeddings[self.account].thread.join(5)
        history.thread.join(5)
        self.assertFalse(store.rows("SELECT * FROM embeddings"))
        self.assertFalse(store.rows("SELECT * FROM samples"))
        self.assertEqual(store.meta("build")["state"], "idle")

    def test_pause_preserves_completed_vectors_and_resume_only_backfills_missing(self):
        entered, release = threading.Event(), threading.Event()
        self.core.live["extra-friend"] = dialogue(20, topic="电脑设备", prefix="我看看")
        passage_calls = []
        def block_second(kind, texts):
            if kind == "passage":
                passage_calls.append(list(texts))
                if len(passage_calls) == 2:
                    entered.set(); release.wait(4)
        self.core.encoder_hook = block_second
        self.service.build(self.account)
        self.assertTrue(entered.wait(3))
        store, history = self.service.accounts[self.account]
        self.service.pause(self.account)
        completed = len(store.rows("SELECT * FROM embeddings"))
        self.assertGreater(completed, 0)
        release.set()
        self.service.embeddings[self.account].thread.join(5)
        history.thread.join(5)
        self.assertEqual(len(store.rows("SELECT * FROM embeddings")), completed)
        self.assertEqual(self.service.status(self.account)["embedding"]["indexState"], "paused")
        self.core.encoder_hook = None
        _, status = self.build()
        self.assertEqual(status["retrieval"]["indexedSamples"], 23)
        self.assertEqual(status["retrieval"]["pendingSamples"], 0)

    def test_backfill_waits_for_new_samples_instead_of_rescanning_while_indexing(self):
        entered, release = threading.Event(), threading.Event()
        def hold(user, after):
            if user == "work-friend":  # the last contact: indexing stays alive with nothing new stored
                entered.set()
                release.wait(5)
        self.core.hook = hold
        scans = []
        original = VectorStore.pending
        def counted(*args, **kwargs):
            scans.append(args[1:])
            return original(*args, **kwargs)
        with patch.object(VectorStore, "pending", counted):
            self.service.build(self.account)
            self.assertTrue(entered.wait(3))
            store, history = self.service.accounts[self.account]
            deadline = time.monotonic() + 3
            while len(store.rows("SELECT * FROM embeddings")) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(store.rows("SELECT * FROM embeddings")), 2)
            before = len(scans)
            time.sleep(1.2)
            idle_scans = len(scans) - before
            release.set()
            history.thread.join(5)
            self.service.embeddings[self.account].thread.join(5)
        self.assertEqual(idle_scans, 0)
        self.assertEqual(self.service.status(self.account)["retrieval"]["pendingSamples"], 0)

    def test_embedding_progress_refreshes_for_samples_added_while_encoding(self):
        self.core.embedding_error = True
        store, _ = self.build()
        self.core.embedding_error = False
        template = store.rows("SELECT * FROM samples ORDER BY id LIMIT 1")[0]

        def append_samples(start, count):
            samples = [{**template, "id": f"synthetic-{index:04d}", "input": f"新增的合成情景 {index}",
                        "evidence": json.loads(template["evidence"]), "terms": []}
                       for index in range(start, start + count)]
            with store.lock, store.db:
                store.write_samples(samples)
            store.samples_changed.set()

        append_samples(0, 100)
        worker = self.service.embeddings[self.account]
        info = model_info(self.core.embedding_status(self.account))
        entered, release = threading.Event(), threading.Event()
        updates, count_calls = [], []
        original_state, original_counts = worker._state, VectorStore.counts

        def state(value, error=None, **progress):
            original_state(value, error, **progress)
            if "done" in progress and progress["done"] is not None:
                with store.lock:
                    actual = original_counts(VectorStore(store), info)
                updates.append((value, progress, actual))

        def counts(vectors, current):
            count_calls.append(True)
            return original_counts(vectors, current)

        def block_first(kind, texts):
            if kind == "passage" and not entered.is_set():
                entered.set()
                release.wait(4)

        self.core.encoder_hook = block_first
        with patch.object(worker, "_state", side_effect=state), patch.object(VectorStore, "counts", counts):
            self.service.build(self.account, "semantic")
            try:
                self.assertTrue(entered.wait(3))
                append_samples(100, 7)
            finally:
                release.set()
            worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(store.meta("embeddingRun")["state"], "ready")
        self.assertTrue(any(value == "indexing" and progress["total"] == 110 for value, progress, _ in updates))
        for _, progress, actual in updates:
            self.assertEqual((progress["done"], progress["total"]), (actual["indexedSamples"], actual["totalSamples"]))
        self.assertEqual((updates[-1][1]["done"], updates[-1][1]["total"]), (110, 110))
        batches = sum(kind == "passage" for kind, _ in self.core.encoded)
        self.assertLess(len(count_calls), batches)

    def test_embedding_progress_does_not_double_count_reembedded_inputs(self):
        store, _ = self.build()
        template = store.rows("SELECT * FROM samples ORDER BY id LIMIT 1")[0]
        samples = [{**template, "id": f"synthetic-{index:04d}", "input": f"新增的合成情景 {index}",
                    "evidence": json.loads(template["evidence"]), "terms": []} for index in range(35)]
        with store.lock, store.db:
            store.write_samples(samples)
        store.samples_changed.set()
        worker = self.service.embeddings[self.account]
        info = model_info(self.core.embedding_status(self.account))
        entered, release = threading.Event(), threading.Event()
        updates, calls = [], []
        original_state, original_counts = worker._state, VectorStore.counts

        def state(value, error=None, **progress):
            original_state(value, error, **progress)
            if "done" in progress and progress["done"] is not None:
                with store.lock:
                    actual = original_counts(VectorStore(store), info)
                updates.append((value, progress, actual))

        def block_second(kind, texts):
            if kind == "passage":
                calls.append(list(texts))
                if len(calls) == 2:
                    entered.set()
                    release.wait(4)

        self.core.encoder_hook = block_second
        with patch.object(worker, "_state", side_effect=state):
            self.service.build(self.account, "semantic")
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(updates[-1][1]["done"], 19)
                changed = store.rows("SELECT * FROM samples WHERE id IN (?,?)", (template["id"], "synthetic-0000"))
                for sample in changed:
                    sample.update(input=sample["input"] + "，原文已更正", evidence=json.loads(sample["evidence"]), terms=[])
                with store.lock, store.db:
                    store.write_samples(changed)
                # Changed inputs advance generation even if the caller's wakeup
                # notification has not arrived yet.
            finally:
                release.set()
            worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(store.meta("embeddingRun")["state"], "ready")
        self.assertTrue(any(progress["done"] == 17 for _, progress, _ in updates))
        for _, progress, actual in updates:
            self.assertEqual((progress["done"], progress["total"]), (actual["indexedSamples"], actual["totalSamples"]))
        self.assertEqual((updates[-1][1]["done"], updates[-1][1]["total"]), (38, 38))
        self.assertEqual(sum("原文已更正" in text for texts in calls for text in texts), 2)

    def test_exclusion_while_encoding_and_source_replacement_discard_late_vectors(self):
        entered, release = threading.Event(), threading.Event()
        def block(kind, texts):
            if kind == "passage" and not entered.is_set():
                entered.set(); release.wait(4)
        self.core.encoder_hook = block
        self.service.build(self.account)
        self.assertTrue(entered.wait(3))
        store, history = self.service.accounts[self.account]
        identifier = store.rows("SELECT id FROM samples WHERE user='meal-friend'")[0]["id"]
        self.service.exclude(self.account, identifier)
        release.set()
        history.thread.join(5)
        self.service.embeddings[self.account].thread.join(5)
        self.assertFalse(store.rows("SELECT * FROM embeddings WHERE sample_id=?", (identifier,)))
        self.assertEqual(self.service.status(self.account)["retrieval"]["indexedSamples"], 2)
        self.core.source = "fixture-replaced-source"
        self.core.live = {"new-friend": dialogue(1, topic="电脑设备")}
        _, status = self.build("full")
        self.assertEqual(status["retrieval"]["indexedSamples"], 1)
        self.assertEqual(store.rows("SELECT count(*) n FROM embeddings")[0]["n"], 1)

    def test_excluded_and_changed_original_samples_never_return_semantically(self):
        store, _ = self.build()
        value = self.service.search(self.account, {"query": "腹中空空"})
        identifier = value["sources"][0]["id"]
        self.service.exclude(self.account, identifier)
        self.assertFalse(self.service.search(self.account, {"query": "腹中空空"})["available"])
        self.service.exclude(self.account, identifier, False)
        self.core.live["meal-friend"][1] = message(2, "self", "原文已变化")
        self.assertFalse(self.service.search(self.account, {"query": "腹中空空"})["available"])
        self.assertFalse(store.rows("SELECT e.* FROM embeddings e JOIN samples s ON s.id=e.sample_id WHERE s.user='meal-friend'"))

    def test_bad_vectors_and_missing_numpy_fall_back_without_persisting_invalid_values(self):
        for bad in ([0.0] * DIMENSIONS, [float("nan")] * DIMENSIONS, [1.0] * 5):
            with self.subTest(bad_size=len(bad)):
                self.core.bad_vector = bad
                store, status = self.build()
                self.assertEqual(status["embedding"]["indexState"], "error")
                self.assertFalse(store.rows("SELECT * FROM embeddings"))
                self.assertTrue(self.service.search(self.account, {"query": "螺丝刀"})["available"])
        self.core.bad_vector = None
        self.build()
        with patch("self_semantic.numerical_runtime", return_value=None):
            result = self.service.search(self.account, {"query": "螺丝刀"})
        self.assertEqual(result["mode"], "lexical")

    def test_semantic_route_has_own_full_index_candidates_and_honest_scan_deadline(self):
        self.core.live["noise-friend"] = dialogue(600, topic="腹部记录", prefix="记录收到了")
        self.build()
        value = self.service.search(self.account, {"query": "腹中空空"}, "different-friend")
        self.assertTrue(any(row["user"] == "meal-friend" for row in value["sources"]))
        self.assertGreater(value["semanticScanned"], 600)
        with patch("self_semantic.SCAN_SECONDS", 0):
            partial = self.service.search(self.account, {"query": "腹中空空"})
        self.assertTrue(partial["semanticPartial"])

    def test_semantic_cutoffs_context_limit_and_corrupt_disk_vector(self):
        store, _ = self.build()
        future = self.service.search(self.account, {"query": "腹中空空", "beforeTimeMs": BASE + 1000})
        self.assertFalse(future["available"])
        same = self.service.search(self.account, {"query": "腹中空空", "beforeSeq": 2}, "meal-friend")
        self.assertFalse(same["available"])
        with store.lock, store.db:
            store.db.execute("UPDATE embeddings SET vector=?", (b"corrupt",))
        self.assertFalse(self.service.search(self.account, {"query": "腹中空空"})["available"])

    def test_cold_numeric_import_never_blocks_status_search_or_close(self):
        self.build()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        real_numpy = self_semantic._NP
        ready = threading.Event()
        def slow_loader():
            entered.set()
            release.wait(4)
            self_semantic._NP = real_numpy
            ready.set()
            finished.set()
        with patch.multiple(self_semantic, _NP=None, _TRIED=False, _RUNTIME_READY=ready), \
                patch("self_semantic._load_runtime", side_effect=slow_loader):
            try:
                started = time.monotonic()
                status = self.service.status(self.account)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.wait(1))
                self.assertEqual(status["retrieval"]["runtimeState"], "loading")
                self.assertEqual(status["retrieval"]["mode"], "lexical")
                self.assertEqual(status["embedding"]["indexState"], "waiting")
                started = time.monotonic()
                found = self.service.search(self.account, {"query": "螺丝刀"})
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(found["mode"], "lexical")
                self.service.build(self.account)
                started = time.monotonic()
                self.service.close()
                self.assertLess(time.monotonic() - started, 1)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
        # A new plugin service can reopen the durable index after that close.
        self.service = Service(SimpleNamespace(core=self.core, data_dir=Path(self.temp.name)))
        self.assertTrue(self.service.status(self.account)["retrieval"]["semanticReady"])

    def test_missing_numeric_runtime_has_truthful_lexical_fallback(self):
        self.build()
        unavailable = threading.Event()
        unavailable.set()
        with patch.multiple(self_semantic, _NP=None, _TRIED=True, _RUNTIME_READY=unavailable):
            status = self.service.status(self.account)
            self.assertEqual(status["retrieval"]["mode"], "lexical")
            self.assertEqual(status["retrieval"]["runtimeState"], "error")
            self.assertIn("运行时不可用", status["embedding"]["indexError"])
            self.assertEqual(self.service.search(self.account, {"query": "螺丝刀"})["mode"], "lexical")


if __name__ == "__main__":
    unittest.main()
