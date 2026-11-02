"""Shared evidence contract/regressions; synthetic messages and temporary DBs only.

Fake responses test validation and caching, not the model's semantic accuracy.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from kyf_plugin import Cancelled, CoreError, ModelError
from self_contracts import SelfError
from self_model import retry_unavailable
from self_evidence import (EvidenceStore, SharedEvidence, encoded, pack_messages, parse_decisions, parse_evidence,
                           unpack_messages, window_messages, WINDOW_ITEMS, digest, legacy_cache_compatible,
                           valid_cache_payload)
from self_profile_analysis import message_base
from self_store import Store
from test_profile_corpus import Context
from test_tag_corpus import Corpus, NoHistory, line, own


def profile(index, quote="我喜欢篮球", relation="interest"):
    return {"i": index, "section": "background", "text": "本人明确表达喜欢篮球", "relation": relation,
            "quote": quote, "reason": "合成原话中的明确自述"}


def tag(index, quote="我喜欢篮球", relation="interest"):
    return {"i": index, "name": "篮球", "category": "运动健身", "relation": relation,
            "quote": quote, "reason": "合成原话中的明确自述"}


def values(index, quote="我喜欢篮球", kind="support"):
    return {"i": index, "key": "core", "kind": kind, "value": "看重自己真正喜欢的事",
            "quote": quote, "reason": "合成原话中的明确态度", "scope": "所见聊天时期", "topic": ""}


def persona(index, quote="我是INFP", kind="selfReport"):
    # MBTI is only a self-report; behaviour evidence leans to one end of a catalog dimension.
    return {"i": index, "key": "mbti" if kind == "selfReport" else "big5_e", "kind": kind,
            "value": "INFP" if kind == "selfReport" else "低",
            "quote": quote, "reason": "合成本人自述，尚非客观人格证明", "scope": "所见聊天时期", "topic": ""}


class Models:
    def __init__(self):
        self.requests, self.responses = [], []
        self.context_tokens, self.model, self.hook = 32768, "synthetic-model-1", None
        self.empty, self.unavailable = False, False
        self.relation = "mention"
        self.first_only = False
        self.model_fields = {}
        self.call_spans = []

    def service(self):
        if self.unavailable:
            raise ModelError("api-not-configured", "synthetic unavailable")
        return {"contextTokens": self.context_tokens, "model": self.model, **self.model_fields}

    def generate(self, context, request, step=None):
        started = time.monotonic()
        time.sleep(.05)  # wide enough for the dispatcher to overlap workers
        try:
            return self._generate(context, request, step)
        finally:
            self.call_spans.append((started, time.monotonic()))

    def _generate(self, context, request, step=None):
        payload = json.loads(request["prompt"])
        record = {"step": step, "prompt": payload, "system": request["system"], "maxOutputTokens": request["maxOutputTokens"]}
        self.requests.append(record)
        if self.hook:
            self.hook(context, record)
        if self.responses:
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
        elif step == "self-evidence-review":
            value = {domain: [{"n": proof["n"]}
                              for proof in payload["proposed"][domain]] for domain in ("profile", "tags", "persona", "values")}
        elif step == "self-evidence-extract":
            value = {"profile": [], "tags": [], "persona": [], "values": []}
            if not self.empty:
                for item in window_messages(payload):
                    if self.first_only and item["i"]:
                        continue
                    index, quote = item["i"], item["text"][:80]
                    value["profile"].append(profile(index, quote, self.relation))
                    value["tags"].append(tag(index, quote, self.relation))
                    value["persona"].append(persona(index, quote, "support"))
                    value["values"].append(values(index, quote))
        else:
            raise AssertionError(step)
        return {"text": value if isinstance(value, str) else encoded(value)}


class SharedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "synthetic-evidence")
        self.models = Models()
        self.plugin = SimpleNamespace(core=NoHistory(), models=self.models)
        self.corpus = Corpus([own(1, "我喜欢篮球"), own(2, "我喜欢先独处整理思路")])
        self.evidence = EvidenceStore(self.store)
        self.reports = []

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_job(self, selected_depth="deep", context=None):
        context = context or Context()
        context.progress = lambda **values: self.reports.append(values)
        job = SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus, selected_depth)
        return job.work(context)

    def test_one_joint_extract_and_review_covers_three_domains_without_manual_data_or_history_access(self):
        with self.store.lock, self.store.db:
            self.store._meta("manual-profile", "PRIVATE_MANUAL_MUST_NOT_ENTER_PROMPT")
        def observe(context, request):
            self.assertTrue(self.evidence.meta("prepared"))
            self.assertEqual(self.evidence.coverage()["totalParts"], 2)
        self.models.hook = observe
        result = self.run_job()
        self.assertEqual([row["step"] for row in self.models.requests], ["self-evidence-extract"])
        self.assertEqual(set(self.models.requests[0]["prompt"]), {"chats"})
        records = list(self.evidence.records())
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(set(record), {"fingerprint", "item", "profile", "tags", "persona", "values"})
            for domain in ("profile", "tags", "persona", "values"):
                self.assertTrue(record[domain])
                self.assertNotIn("i", record[domain][0])
                self.assertNotIn("n", record[domain][0])
                self.assertIn(record[domain][0]["quote"], record["item"]["text"])
        self.assertEqual(result["coverage"]["newlyAnalyzedMessages"], 2)
        self.assertNotIn("PRIVATE_MANUAL", encoded(self.models.requests))
        self.assertEqual(self.store.meta("manual-profile"), "PRIVATE_MANUAL_MUST_NOT_ENTER_PROMPT")

    def test_one_call_per_window_keeps_the_extracted_relations_without_a_review_round(self):
        self.models.relation = "trait"
        self.run_job()
        self.assertEqual([row["step"] for row in self.models.requests], ["self-evidence-extract"])
        for record in self.evidence.records():
            self.assertEqual([proof["relation"] for proof in record["tags"]], ["trait"])
            self.assertEqual(len(record["values"]), 1)

    def test_filler_lines_are_context_only_and_analyzed_without_a_model_call(self):
        self.corpus.change([own(1, "嗯嗯"), own(2, "哈哈哈[捂脸]"), own(3, "好的！")])
        result = self.run_job()
        self.assertEqual(self.models.requests, [])
        self.assertEqual(result["coverage"]["analyzedMessages"], 3)
        self.corpus.change([own(1, "嗯嗯嗯"), own(2, "我喜欢篮球")])
        self.run_job()
        prompt = self.models.requests[-1]["prompt"]
        self.assertEqual([line["text"] for line in window_messages(prompt)], ["我喜欢篮球"])
        self.assertIn("我：嗯嗯嗯", prompt["chats"][0]["lines"])

    def test_a_loose_or_missing_quote_becomes_the_cited_line(self):
        self.models.responses = [{"profile": [], "tags": [{"i": 0, "name": "篮球", "category": "运动健身", "relation": "interest"},
                                                         {**tag(1), "quote": "我喜欢……打篮球"}],
                                  "persona": [], "values": []}]
        self.run_job()
        quotes = [proof["quote"] for record in self.evidence.records() for proof in record["tags"]]
        self.assertEqual(quotes, ["我喜欢篮球", "我喜欢先独处整理思路"])

    def test_empty_evidence_is_cached_across_reopening_and_summary_cache_is_content_addressed(self):
        self.models.empty = True
        first = self.run_job()
        self.evidence.summary_put("content-key", {"text": "synthetic cached summary"})
        calls = len(self.models.requests)
        self.store.close()
        self.store = Store(Path(self.temp.name), "synthetic-evidence")
        self.evidence = EvidenceStore(self.store)
        with patch.object(self.corpus, "iter_messages", side_effect=AssertionError("prepared snapshot was reread")):
            second = self.run_job()
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(second["coverage"]["reusedMessages"], 2)
        self.assertEqual(second["coverage"]["newlyAnalyzedMessages"], 0)
        self.assertTrue(all(not record[domain] for record in self.evidence.records() for domain in ("profile", "tags", "persona", "values")))
        self.assertEqual(self.evidence.summary_get("content-key"), {"text": "synthetic cached summary"})
        self.assertEqual(calls, 1)
        self.assertEqual(self.evidence.meta("settings")["quality"], "extracted")

    def test_library_only_construction_never_loads_model_settings(self):
        self.models.unavailable = True
        SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus)
        self.assertEqual(EvidenceStore(self.store).coverage()["ownMessages"], 0)
        self.assertEqual(self.models.requests, [])

    def test_append_then_changed_followup_only_extracts_the_affected_source_parts(self):
        self.run_job()
        original = {record["item"]["id"]: record["fingerprint"] for record in self.evidence.records()}
        self.corpus.change(self.corpus.messages + [own(3, "新的一句本人文字")])
        before = len(self.models.requests)
        self.run_job()
        self.assertEqual(len(self.models.requests) - before, 1)
        self.assertEqual([item["text"] for item in window_messages(self.models.requests[before]["prompt"])], ["新的一句本人文字"])
        updated = copy.deepcopy(self.corpus.messages)
        updated[0]["context_after"] = [line(4, "开玩笑的，我从来不打", side="self", speaker="本人")]
        self.corpus.change(updated)
        before = len(self.models.requests)
        result = self.run_job()
        self.assertEqual(len(self.models.requests) - before, 1)
        self.assertEqual(len(window_messages(self.models.requests[before]["prompt"])), 1)
        records = list(self.evidence.records())
        self.assertNotEqual(records[0]["fingerprint"], original[records[0]["item"]["id"]])
        self.assertEqual(records[1]["fingerprint"], original[records[1]["item"]["id"]])
        self.assertEqual((result["coverage"]["reusedMessages"], result["coverage"]["newlyAnalyzedMessages"]), (2, 1))

    def test_projection_renumbering_reuses_semantically_identical_inputs(self):
        self.corpus.messages[0]["context_before"] = [line(10, "你喜欢什么？")]
        first = self.run_job()
        changed = copy.deepcopy(self.corpus.messages)
        changed[0]["seq"] += 20
        changed[0]["context_before"][0]["seq"] += 20
        self.corpus.change(changed)
        calls = len(self.models.requests)
        second = self.run_job()
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual(first["identity"], second["identity"])

    def test_deleted_message_loses_active_reference_but_unchanged_cached_content_survives(self):
        first = self.run_job()
        original = copy.deepcopy(self.corpus.messages)
        self.corpus.change(original[:1])
        calls = len(self.models.requests)
        second = self.run_job()
        self.assertNotEqual(first["identity"], second["identity"])
        self.assertEqual(len(list(self.evidence.records())), 1)
        self.corpus.change(original)
        self.run_job()
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual(len(list(self.evidence.records())), 2)

    def test_source_quality_model_and_budget_are_cache_dependencies_even_with_empty_evidence(self):
        self.models.empty = True
        identities = [self.run_job()["identity"]]
        self.evidence.summary_put("old-summary", {"text": "keep content cache"})
        self.corpus.metadata["source"] = "another-synthetic-source"
        identities.append(self.run_job()["identity"])
        identities.append(self.run_job("basic")["identity"])
        self.models.model = "synthetic-model-2"
        identities.append(self.run_job("basic")["identity"])
        self.models.context_tokens = 16384
        identities.append(self.run_job("basic")["identity"])
        self.assertEqual(len(set(identities)), 4)  # basic and deep share one extraction
        self.assertEqual(len(self.models.requests), 4)
        self.assertEqual(self.evidence.summary_get("old-summary"), {"text": "keep content cache"})

    def test_standard_and_deep_share_the_same_reviewed_raw_evidence(self):
        first = self.run_job("standard")
        before = len(self.models.requests)
        with patch.object(self.corpus, "iter_messages", side_effect=AssertionError("output selection reread source messages")):
            second = self.run_job("deep")
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(len(self.models.requests), before)
        self.assertEqual(second["coverage"]["depth"], "deep")

    def test_service_without_cache_fingerprint_keeps_strict_legacy_route_identity(self):
        identities = [self.run_job()["identity"]]
        alias = self.models.model
        for field in ("fingerprint", "routeRevision", "actualModel", "backendId", "id"):
            self.models.model_fields[field] = "synthetic-change-" + field
            self.models.model_fields.update(apiKey="PRIVATE_KEY_MUST_NOT_PERSIST", baseUrl="PRIVATE_ENDPOINT_MUST_NOT_PERSIST")
            before = len(self.models.requests)
            result = self.run_job()
            self.assertGreater(len(self.models.requests), before)
            identities.append(result["identity"])
            self.assertEqual(self.models.model, alias)
        self.assertEqual(len(set(identities)), 6)
        self.assertNotIn("PRIVATE_", encoded(self.evidence.meta("settings")))

    def route(self, revision, *, stable=True, endpoint="synthetic-endpoint"):
        base = digest(["chat_completions", endpoint, self.models.model, self.models.context_tokens])
        route = ["synthetic-actual", "synthetic-backend", revision]
        self.models.model_fields = {"id": "synthetic-service", "actualModel": route[0], "backendId": route[1],
                                    "routeRevision": revision,
                                    "fingerprint": hashlib.sha256(json.dumps([base, route], ensure_ascii=False,
                                                   separators=(",", ":")).encode("utf-8")).hexdigest()}
        if stable:
            self.models.model_fields["cacheFingerprint"] = base

    def test_stable_cache_reuses_route_changes_but_preserves_model_and_endpoint_identity(self):
        self.route(1)
        first = self.run_job()
        before = len(self.models.requests)
        self.route(2)
        reused = self.run_job()
        self.assertEqual(first["identity"], reused["identity"])
        self.assertEqual(len(self.models.requests), before)
        self.assertEqual(reused["coverage"]["reusedMessages"], 2)
        for field in ("actualModel", "backendId", "version", "revision"):
            self.models.model_fields[field] = "synthetic-new-" + field
            self.run_job()
            self.assertGreater(len(self.models.requests), before)
            before = len(self.models.requests)
        self.route(3, endpoint="synthetic-other-endpoint")
        self.run_job()
        self.assertGreater(len(self.models.requests), before)

    def test_stable_cache_still_rejects_route_changes_during_inference(self):
        self.route(1)
        self.models.hook = lambda *_args: self.route(2)
        with self.assertRaises(SelfError) as caught:
            self.run_job()
        self.assertEqual(caught.exception.code, "model-changed")
        self.assertEqual(list(self.evidence.records()), [])

    def test_legacy_cache_migration_verifies_endpoint_hash_and_reuses_only_valid_payloads(self):
        self.route(1, stable=False)
        self.run_job()
        old = self.evidence.meta("settings")
        before = len(self.models.requests)
        self.route(2)
        result = self.run_job()
        self.assertTrue(legacy_cache_compatible(old, self.evidence.meta("settings")))
        self.assertEqual(len(self.models.requests), before)
        self.assertEqual(result["coverage"]["reusedMessages"], 2)
        self.assertEqual(self.store.rows("SELECT count(*) n FROM self_evidence_cache")[0]["n"], 4)

    def test_legacy_migration_rejects_endpoint_model_source_rules_and_payload_changes(self):
        self.route(1, stable=False)
        self.run_job()
        old = self.evidence.meta("settings")
        self.route(2)
        job = SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus)
        stable = job._settings(self.models.service())
        for field, value in (("cacheFingerprint", "a" * 64), ("actualModel", "different"),
                             ("backendId", "different"), ("version", "different")):
            changed = copy.deepcopy(stable)
            changed["model"][field] = value
            self.assertFalse(legacy_cache_compatible(old, changed))
        self.assertFalse(legacy_cache_compatible(old, {**stable, "rules": "different"}))
        self.assertEqual(self.evidence.migrate_legacy_cache(stable, "different-source", lambda: None), 0)
        rows = self.store.rows("SELECT * FROM self_evidence_active ORDER BY position")
        item = json.loads(rows[0]["item"])
        invalid = {domain: [] for domain in ("profile", "tags", "persona", "values")}
        invalid["tags"] = [{key: value for key, value in tag(0, "not in the source").items() if key != "i"}]
        self.assertFalse(valid_cache_payload(item, invalid))
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE self_evidence_cache SET payload=? WHERE fingerprint=?",
                                  (encoded(invalid), rows[0]["fingerprint"]))
        result = self.run_job()
        self.assertEqual(result["coverage"]["reusedMessages"], 1)
        self.assertEqual(result["coverage"]["newlyAnalyzedMessages"], 1)

    def test_legacy_migration_does_not_overwrite_existing_cache_or_invent_done(self):
        self.route(1, stable=False)
        self.run_job()
        self.route(2)
        job = SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus)
        stable, source = job._settings(self.models.service()), self.corpus.meta("source")
        rows = self.store.rows("SELECT * FROM self_evidence_active ORDER BY position")
        existing = encoded({domain: [] for domain in ("profile", "tags", "persona", "values")})
        target = digest([stable, source, json.loads(rows[0]["item"])])
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO self_evidence_cache VALUES(?,?)", (target, existing))
            self.store.db.execute("DELETE FROM self_evidence_cache WHERE fingerprint=?", (rows[1]["fingerprint"],))
        self.assertEqual(self.evidence.migrate_legacy_cache(stable, source, lambda: None), 0)
        self.assertEqual(self.store.rows("SELECT payload FROM self_evidence_cache WHERE fingerprint=?", (target,))[0]["payload"], existing)
        self.assertEqual(self.store.rows("SELECT * FROM self_evidence_active ORDER BY position"), rows)
        before = len(self.models.requests)
        result = self.run_job()
        self.assertEqual(result["coverage"]["reusedMessages"], 1)
        self.assertEqual(result["coverage"]["newlyAnalyzedMessages"], 1)
        self.assertGreater(len(self.models.requests), before)

    def test_legacy_migration_rejects_mismatched_item_hash_and_checks_cancellation_before_writing(self):
        self.route(1, stable=False)
        self.run_job()
        self.route(2)
        job = SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus)
        stable, source = job._settings(self.models.service()), self.corpus.meta("source")
        rows = self.store.rows("SELECT * FROM self_evidence_active ORDER BY position")
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE self_evidence_active SET fingerprint=? WHERE position=?",
                                  (rows[1]["fingerprint"], rows[0]["position"]))
        before = self.store.rows("SELECT * FROM self_evidence_cache ORDER BY fingerprint")
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            self.evidence.migrate_legacy_cache(stable, source, cancel)
        self.assertEqual(self.store.rows("SELECT * FROM self_evidence_cache ORDER BY fingerprint"), before)
        self.assertEqual(self.evidence.migrate_legacy_cache(stable, source, lambda: None), 1)
        invalid_target = digest([stable, source, json.loads(rows[0]["item"])])
        self.assertEqual(self.store.rows("SELECT 1 FROM self_evidence_cache WHERE fingerprint=?", (invalid_target,)), [])

    def test_legacy_migration_cancelled_between_batches_is_retryable_and_idempotent(self):
        self.route(1, stable=False)
        self.run_job()
        old, source = self.evidence.meta("settings"), self.corpus.meta("source")
        original = list(self.evidence.records())[0]
        payload = encoded({domain: original[domain] for domain in ("profile", "tags", "persona", "values")})
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM self_evidence_active")
            self.store.db.execute("DELETE FROM self_evidence_cache")
            for position in range(201):
                item = {**original["item"], "id": f"synthetic-batch-{position}"}
                fingerprint = digest([old, source, item])
                self.store.db.execute("INSERT INTO self_evidence_cache VALUES(?,?)", (fingerprint, payload))
                self.store.db.execute("INSERT INTO self_evidence_active VALUES(?,?,?,?,1,0)",
                                      (position, item["id"], fingerprint, encoded(item)))
        active = self.store.rows("SELECT * FROM self_evidence_active ORDER BY position")
        self.route(2)
        stable = SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus)._settings(self.models.service())
        checks = []
        def cancel_second_batch():
            checks.append(True)
            if len(checks) == 2:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            self.evidence.migrate_legacy_cache(stable, source, cancel_second_batch)
        self.assertEqual(self.store.rows("SELECT count(*) n FROM self_evidence_cache")[0]["n"], 401)
        self.assertEqual(self.evidence.migrate_legacy_cache(stable, source, lambda: None), 1)
        self.assertEqual(self.evidence.migrate_legacy_cache(stable, source, lambda: None), 0)
        self.assertEqual(self.store.rows("SELECT * FROM self_evidence_active ORDER BY position"), active)
        self.assertEqual(self.evidence.meta("settings"), old)

    def test_model_route_or_context_switch_during_a_response_is_not_cached_under_the_old_identity(self):
        for step, field in (("self-evidence-extract", "fingerprint"), ("self-evidence-extract", "routeRevision"),
                            ("self-evidence-extract", "contextTokens")):
            with self.subTest(step=step, field=field):
                self.models.model += "-fresh"
                def switch(context, request):
                    if request["step"] == step:
                        if field == "contextTokens":
                            self.models.context_tokens += 4096
                        else:
                            self.models.model_fields[field] = "new-synthetic-route"
                self.models.hook = switch
                with self.assertRaises(SelfError) as caught:
                    self.run_job()
                self.assertEqual(caught.exception.code, "model-changed")
                self.assertEqual(self.evidence.coverage()["analyzedParts"], 0)
                self.assertEqual(list(self.evidence.records()), [])
        self.models.hook = None
        self.assertEqual(self.run_job()["coverage"]["analyzedMessages"], 2)

    def test_coverage_incompleteness_changes_rebuild_the_prepared_snapshot_without_body_revision(self):
        original_counts = self.corpus.counts
        revision = self.corpus.meta("revision")
        for field, value in (("gaps", ["synthetic-gap"]), ("failedConversations", 1), ("unreadMessages", 1),
                             ("directoryPartial", True), ("catalogComplete", False)):
            with self.subTest(field=field):
                self.corpus.counts = original_counts
                self.run_job()
                self.corpus.counts = lambda: {**original_counts(), field: value}
                with patch.object(self.corpus, "iter_messages", wraps=self.corpus.iter_messages) as reads:
                    self.run_job()
                self.assertGreater(reads.call_count, 0)
                self.assertTrue(self.evidence.meta("sourceIncomplete"))
                self.assertEqual(self.corpus.meta("revision"), revision)
                self.assertTrue(all(proof["relation"] == "uncertain" for record in self.evidence.records() for proof in record["tags"]))

    def test_late_extract_or_review_response_after_cancel_is_not_committed(self):
        for step in ("self-evidence-extract",):
            with self.subTest(step=step):
                self.models.model += "-fresh"
                def cancel(context, request):
                    if request["step"] == step:
                        context.cancelled.set()
                self.models.hook = cancel
                with self.assertRaises(Cancelled):
                    self.run_job()
                self.assertEqual(self.evidence.coverage()["analyzedParts"], 0)
                self.assertEqual(list(self.evidence.records()), [])
        self.models.hook = None
        self.assertEqual(self.run_job()["coverage"]["analyzedMessages"], 2)

    def test_scope_or_corpus_change_during_a_response_cannot_commit_it(self):
        def change(context, request):
            self.corpus.metadata["revision"] += 1
        self.models.hook = change
        with self.assertRaises(SelfError) as caught:
            self.run_job()
        self.assertEqual(caught.exception.code, "source-changed")
        self.assertEqual(self.evidence.coverage()["analyzedParts"], 0)

    def test_dynamic_batches_cover_more_than_twenty_four_short_items(self):
        self.models.empty = True
        self.models.context_tokens = 65536
        self.corpus.change([own(index, f"合成短句{index}") for index in range(1, 151)])
        result = self.run_job()
        extracts = [row for row in self.models.requests if row["step"] == "self-evidence-extract"]
        self.assertTrue(any(len(window_messages(row["prompt"])) > 24 for row in extracts))
        self.assertTrue(all(len(window_messages(row["prompt"])) <= WINDOW_ITEMS for row in extracts))
        self.assertEqual(sum(len(window_messages(row["prompt"])) for row in extracts), 150)
        self.assertEqual(result["coverage"]["analyzedMessages"], 150)

    def test_dense_neighbor_windows_share_context_once_and_fit_the_batch_budget(self):
        self.models.first_only, self.models.context_tokens = True, 65536
        lines = [line(index, f"合成相邻原话 {index}", side="self" if index % 2 else "other",
                      speaker="本人" if index % 2 else "群友·合成人（wxid_synthetic）") for index in range(1, 91)]
        self.corpus.change([own(index, lines[index - 1]["text"], before=lines[max(0, index - 13):index - 1],
                                after=lines[index:index + 6]) for index in range(1, 91)])
        self.run_job()
        extracts = [request for request in self.models.requests if request["step"] == "self-evidence-extract"]
        self.assertTrue(any(len(window_messages(request["prompt"])) > 24 for request in extracts))
        room = self.evidence.meta("settings")["room"]
        for request in extracts:
            self.assertLessEqual(len(encoded(request["prompt"])), room)
            context = [text for chat in request["prompt"]["chats"] for text in chat["lines"] if isinstance(text, str)]
            self.assertEqual(len(context), len(set(context)))
            self.assertFalse(any("wxid_" in text for text in context))
        self.assertEqual(sum(len(window_messages(request["prompt"])) for request in extracts), 90)

    def test_capacity_errors_halve_only_uncommitted_batches(self):
        self.models.empty = True
        self.corpus.change([own(index, "合成短句") for index in range(1, 11)])
        self.models.responses = [ModelError("output-truncated", "synthetic capacity"),
                                 ModelError("output-truncated", "synthetic capacity")]
        result = self.run_job()
        requests = self.models.requests
        self.assertTrue(all(len(window_messages(row["prompt"])) == 10 for row in requests[:2]))
        self.assertTrue(all(len(window_messages(row["prompt"])) <= 5 for row in requests[2:]))
        self.assertEqual(result["coverage"]["analyzedMessages"], 10)
        self.assertEqual(len(list(self.evidence.records())), 10)

    def test_one_item_capacity_failure_remains_pending_and_can_resume(self):
        self.corpus.change([own(1, "我喜欢篮球")])
        self.models.responses = [ModelError("output-truncated", "synthetic capacity"),
                                 ModelError("output-truncated", "synthetic capacity")]
        with self.assertRaises(ModelError):
            self.run_job()
        self.assertEqual(self.evidence.coverage()["analyzedMessages"], 0)
        self.assertEqual(list(self.evidence.records()), [])
        self.assertEqual(self.run_job()["coverage"]["analyzedMessages"], 1)

    def test_an_unusable_batch_finishes_partial_without_stalling_and_retries_next_pass(self):
        invalid = encoded({"messages": [], "contextRows": []})  # the model echoed its input
        self.models.responses = [invalid] * 9  # both items fail at every size, three tries each
        with self.assertRaises(SelfError) as caught:
            self.run_job()
        self.assertEqual(caught.exception.code, "evidence-incomplete")
        self.assertEqual(self.evidence.coverage()["skippedParts"], 2)
        self.assertEqual(self.evidence.coverage()["analyzedParts"], 0)
        self.assertEqual(list(self.evidence.records()), [])
        self.assertEqual(len(self.evidence.pending()), 2)
        self.assertEqual([row["step"] for row in self.models.requests],
                         ["self-evidence-extract"] * 9)
        self.assertEqual(self.run_job()["coverage"]["analyzedParts"], 2)
        self.assertEqual(self.evidence.coverage()["skippedParts"], 0)

    def test_a_legacy_uncached_done_row_is_retried_without_reextracting_cached_empty_results(self):
        self.models.empty = True
        self.run_job()
        rows = self.store.rows("SELECT fingerprint FROM self_evidence_active ORDER BY position")
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM self_evidence_cache WHERE fingerprint=?", (rows[0]["fingerprint"],))
        self.models.requests.clear()
        with patch.object(self.corpus, "iter_messages", side_effect=AssertionError("prepared snapshot was reread")):
            result = self.run_job()
        self.assertEqual(len(self.models.requests), 1)
        self.assertEqual((result["coverage"]["reusedParts"], result["coverage"]["newlyAnalyzedParts"]), (1, 1))
        self.assertEqual(len(list(self.evidence.records())), 2)

    def test_an_unusable_batch_is_bisected_and_then_extracts_normally(self):
        invalid = "这不是JSON"
        self.models.responses = [invalid] * 3  # the two-item batch fails all three tries, then halves succeed
        with patch("self_evidence.EXTRACTION_WORKERS", 1):  # the call sequence below is sequential
            result = self.run_job()
        self.assertEqual(result["coverage"]["analyzedParts"], 2)
        self.assertEqual(result["coverage"].get("skippedParts", 0), 0)
        self.assertEqual(len(list(self.evidence.records())), 2)
        self.assertEqual([row["step"] for row in self.models.requests], ["self-evidence-extract"] * 5)

    def test_an_echoed_extract_is_retried_with_the_rejection_in_the_system(self):
        self.models.responses = [encoded({"messages": [{"i": 0, "text": "我喜欢篮球"}], "contextRows": []}),
                                 encoded({"profile": [profile(0, "我喜欢篮球")], "tags": [tag(0)],
                                          "persona": [persona(0, "我喜欢篮球", "support")], "values": [values(0, "我喜欢篮球")]})]
        self.run_job()
        extracts = [row for row in self.models.requests if row["step"] == "self-evidence-extract"]
        self.assertEqual(len(extracts), 2)
        self.assertNotIn("上一轮输出未通过校验", extracts[0]["system"])
        self.assertIn("上一轮输出未通过校验", extracts[1]["system"])
        self.assertIn("回传了输入", extracts[1]["system"])

    def stalled_first_call(self):
        release, calls = threading.Event(), []
        def stall(context, request):
            calls.append(request["step"])
            if len(calls) == 1:
                release.wait(10)  # this reply arrives far too late
        self.models.hook = stall
        self.addCleanup(release.set)
        return calls

    def test_a_call_that_never_returns_is_abandoned_and_its_lines_extracted_again(self):
        calls = self.stalled_first_call()
        with patch("self_evidence.STALLED_CALL_SECONDS", 0.3):
            result = self.run_job()
        self.assertEqual(result["coverage"]["analyzedMessages"], 2)
        self.assertEqual(len(calls), 3, "the abandoned window comes back halved")
        self.assertEqual(len(list(self.evidence.records())), 2)

    def test_a_window_that_always_stalls_is_halved_instead_of_resent_forever(self):
        release, sizes = threading.Event(), []
        def stall(context, request):
            sizes.append(len(window_messages(request["prompt"])))
            if sizes[-1] > 1:
                release.wait(10)  # the model loops on the whole window every time
        self.models.hook = stall
        self.addCleanup(release.set)
        with patch("self_evidence.STALLED_CALL_SECONDS", 0.3):
            result = self.run_job()
        self.assertEqual(result["coverage"]["analyzedMessages"], 2)
        self.assertEqual(sizes, [2, 1, 1])

    def test_a_model_call_past_its_deadline_is_retried(self):
        calls = self.stalled_first_call()
        with patch("self_model.CALL_DEADLINE_SECONDS", 0.3):
            result = self.run_job()
        self.assertEqual(result["coverage"]["analyzedMessages"], 2)
        self.assertEqual(len(calls), 2)

    def test_extraction_runs_batches_concurrently(self):
        # Long-enough messages keep batches small, so several batches are in
        # flight at once; overlapping call spans prove more than one worker ran.
        self.corpus.change([own(index, "很长的一句合成内容，让单条消息接近批预算的三分之一。" * 400) for index in range(1, 10)])
        result = self.run_job()
        self.assertGreaterEqual(result["coverage"]["analyzedParts"], 9)
        spans = self.models.call_spans
        overlap = max(sum(1 for start, end in spans if start < moment < end) for moment, _ in spans)
        self.assertGreaterEqual(overlap, 2)

    def test_batch_capacity_recovers_after_early_failures_without_missing_or_duplicate_commits(self):
        self.models.empty = True
        self.corpus.change([own(index, "EARLY_DENSE" if index == 1 else f"稀疏合成短句 {index}") for index in range(1, 81)])
        accepted, failed = [], []

        def localized_capacity(context, request):
            if request["step"] != "self-evidence-extract":
                return
            items = window_messages(request["prompt"])
            if len(items) > 1 and any(item["text"] == "EARLY_DENSE" for item in items):
                failed.append(len(items))
                raise ModelError("output-truncated", "synthetic early capacity limit")
            accepted.append([item["text"] for item in items])

        self.models.hook = localized_capacity
        with patch("self_evidence.EXTRACTION_WORKERS", 1):  # sequential halving semantics
            result = self.run_job()
        sizes = [len(batch) for batch in accepted]
        self.assertGreaterEqual(len(set(failed)), 3)
        self.assertEqual(sizes[:3], [1, 1, 1])
        self.assertGreaterEqual(max(sizes[3:]), 8)
        self.assertEqual([text for batch in accepted for text in batch], [row["text"] for row in self.corpus.messages])
        self.assertEqual((result["coverage"]["analyzedMessages"], result["coverage"]["newlyAnalyzedMessages"]), (80, 80))
        self.assertEqual(len(list(self.evidence.records())), 80)

    def test_fragments_preserve_full_body_and_advance_only_after_the_whole_message(self):
        self.models.context_tokens = 4096
        self.corpus.change([own(1, "我喜欢先独处整理思路。" * 300)])
        extracts = [0]
        def pause(context, request):
            if request["step"] == "self-evidence-extract":
                extracts[0] += 1
                if extracts[0] == 2:
                    context.cancelled.set()
        self.models.hook = pause
        with patch("self_evidence.EXTRACTION_WORKERS", 1):  # commit ordering is sequential here
            with self.assertRaises(Cancelled):
                self.run_job("basic")
        partial = self.evidence.coverage()
        self.assertEqual(partial["analyzedMessages"], 0)
        self.assertTrue(0 < partial["analyzedParts"] < partial["totalParts"])  # the first window committed
        self.models.hook = None
        with patch.object(self.corpus, "iter_messages", side_effect=AssertionError("resume reread prepared context")):
            result = self.run_job("basic")
        records = list(self.evidence.records())
        self.assertEqual("".join(record["item"]["text"] for record in records), self.corpus.messages[0]["text"])
        self.assertEqual([record["item"]["part"] for record in records], list(range(1, len(records) + 1)))
        self.assertTrue(all(record["tags"][0]["relation"] == "uncertain" for record in records))
        self.assertEqual((result["coverage"]["analyzedMessages"], result["coverage"]["reusedMessages"],
                          result["coverage"]["newlyAnalyzedMessages"], result["coverage"]["reusedParts"]),
                         (1, 0, 1, partial["analyzedParts"]))

    def test_actual_fragmentation_with_large_context_always_adds_the_original_message_frame(self):
        before = [line(index, "合成前文" * 20) for index in range(1, 13)]
        after = [line(index, "合成后文" * 20) for index in range(14, 20)]
        text = "这是本人观点和经历。" * 200 + "以上都是转述朋友说的，不是我"
        row = own(13, text, before=before, after=after)
        roomy_base = {**message_base(row, 100000), "user": row["user"]}
        self.assertNotIn("messageFrame", roomy_base)
        job = SharedEvidence(self.plugin, self.store, "synthetic-evidence", self.corpus)
        job.room = len(encoded(pack_messages([roomy_base]))) + 512
        parts = job._parts(row, False)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(item["text"] for item in parts), text)
        for part in parts:
            self.assertTrue(text.startswith(part["messageFrame"]["head"]))
            self.assertTrue(part["messageFrame"]["tail"].endswith("以上都是转述朋友说的，不是我"))
            self.assertEqual((len(part["context"]["before"]), len(part["context"]["after"])), (12, 6))
            self.assertLessEqual(len(encoded(pack_messages([part]))), job.room)

    def test_unavailable_model_still_removes_stale_active_source_evidence(self):
        self.run_job()
        self.models.unavailable = True
        self.corpus.change([own(1, "已经更正的正文")])
        with self.assertRaises(ModelError):
            self.run_job()
        self.assertEqual(list(self.evidence.records()), [])
        self.assertEqual(self.evidence.coverage()["ownMessages"], 1)

    def test_interrupted_preparation_exposes_no_mixed_active_snapshot_and_sends_no_model_calls(self):
        self.run_job()
        self.evidence.summary_put("preserved-summary", {"text": "cached"})
        self.corpus.change([own(index, "合成消息") for index in range(1, 151)])
        context, original = Context(), self.corpus.iter_messages

        def interrupted(after_id=0, limit=200):
            rows = original(after_id, limit)
            if after_id:
                context.cancelled.set()
            return rows

        self.corpus.iter_messages = interrupted
        before = len(self.models.requests)
        with self.assertRaises(Cancelled):
            self.run_job(context=context)
        self.assertEqual(len(self.models.requests), before)
        self.assertFalse(self.evidence.meta("prepared"))
        self.assertEqual(list(self.evidence.records()), [])
        self.assertIsNone(self.evidence.identity())
        self.assertEqual(self.evidence.summary_get("preserved-summary"), {"text": "cached"})
        self.corpus.iter_messages = original
        self.assertEqual(self.run_job()["coverage"]["analyzedMessages"], 150)

    def test_edit_in_the_middle_of_a_long_message_only_invalidates_the_changed_part(self):
        self.models.context_tokens, self.models.empty = 4096, True
        text = "甲" * 2500
        self.corpus.change([own(1, text)])
        self.run_job("basic")
        records = list(self.evidence.records())
        self.assertGreater(len(records), 2)
        self.corpus.change([own(1, text[:1250] + "乙" + text[1251:])])
        before = len(self.models.requests)
        result = self.run_job("basic")
        self.assertEqual(len(self.models.requests) - before, 1)
        self.assertEqual(len(window_messages(self.models.requests[-1]["prompt"])), 1)
        self.assertEqual(result["coverage"]["reusedParts"], len(records) - 1)
        self.assertEqual(result["coverage"]["newlyAnalyzedParts"], 1)


class EvidenceParserTests(unittest.TestCase):
    def setUp(self):
        self.items = [{"i": 0, "text": "我喜欢篮球，我是INFP", "part": 1, "parts": 1,
                       "context": {"before": [], "after": []}}]
        self.value = {"profile": [profile(0)], "tags": [tag(0)], "persona": [persona(0)], "values": [values(0)]}

    def test_elided_quotes_pass_when_segments_match_in_order(self):
        value = copy.deepcopy(self.value)
        value["tags"][0]["quote"] = "我喜欢篮球……我是INFP"
        value["persona"] = [persona(0, "喜欢篮球……我是INFP", "support")]
        parsed = parse_evidence(self.items)(encoded(value))
        self.assertEqual(parsed["tags"][0]["quote"], "我喜欢篮球……我是INFP")
        for bad in ("篮球……不喜欢", "……", "我喜欢篮球……"):
            with self.subTest(quote=bad):
                invalid, stats = copy.deepcopy(value), {}
                invalid["tags"][0]["quote"] = bad
                self.assertEqual(parse_evidence(self.items, stats=stats)(encoded(invalid))["tags"], [])
                self.assertEqual(stats, {"dropped": 1})

    def test_an_invalid_proof_is_dropped_alone_and_never_discards_the_batch(self):
        valid = parse_evidence(self.items)(encoded(self.value))
        for domain, field, value in (("profile", "quote", "他人的话"), ("tags", "relation", "explicit"),
                                     ("persona", "quote", "他人的话"), ("profile", "relation", None),
                                     ("tags", "category", "不存在的类别"), ("profile", "text", "x" * 241),
                                     ("values", "quote", "他人的话"), ("values", "key", "mbti"),
                                     ("tags", "reason", "")):
            with self.subTest(domain=domain, field=field):
                invalid, stats = copy.deepcopy(self.value), {}
                invalid[domain][0][field] = value
                result = parse_evidence(self.items, stats=stats)(encoded(invalid))
                self.assertEqual(result, {**valid, domain: []})
                self.assertEqual(stats, {"dropped": 1})
        invalid = copy.deepcopy(self.value)
        invalid["tags"].insert(0, "不是对象")
        self.assertEqual(parse_evidence(self.items)(encoded(invalid)), valid)
        # Omitted domains mean no evidence there; only an answer without any domain is unusable.
        self.assertEqual(parse_evidence(self.items)(encoded({"tags": []})),
                         {"profile": [], "tags": [], "persona": [], "values": []})
        for unusable in ({"messages": []}, {"evidence": []}, "不是JSON"):
            with self.subTest(unusable=unusable):
                with self.assertRaises(SelfError) as caught:
                    parse_evidence(self.items)(unusable if isinstance(unusable, str) else encoded(unusable))
                self.assertEqual(caught.exception.code, "invalid-output")
        with self.assertRaises(SelfError) as caught:
            parse_evidence(self.items)(encoded({"messages": [], "contextRows": []}))
        self.assertIn("回传了输入", caught.exception.detail)

    def test_a_quote_of_another_batch_message_is_anchored_to_that_message(self):
        items = [{**self.items[0], "i": 0, "text": "周末去哪"}, {**self.items[0], "i": 1, "text": "我喜欢篮球"},
                 {**self.items[0], "i": 2, "text": "好的"}, {**self.items[0], "i": 3, "text": "我喜欢篮球"}]
        value = {"profile": [], "tags": [tag(2)], "persona": [], "values": [values(0)]}
        result = parse_evidence(items)(encoded(value))
        self.assertEqual([row["i"] for row in result["tags"]], [1])  # the nearest of two identical lines
        self.assertEqual([row["i"] for row in result["values"]], [1])
        for cited, anchored in ((3, 3), (9, 3), (True, 1), (None, 1)):
            with self.subTest(cited=cited):
                tags = parse_evidence(items)(encoded({**value, "tags": [{**tag(0), "i": cited}]}))["tags"]
                self.assertEqual(tags[0]["i"], anchored)

    def test_a_quote_differing_only_in_whitespace_keeps_the_original_characters(self):
        items = [{**self.items[0], "text": "@合成群友 我看看能不能和你调到一个宿舍。"}]
        value = {"profile": [], "tags": [tag(0, "@合成群友 我看看能不能和你调到一个宿舍。", "mention")], "persona": [],
                 "values": [values(0, "@合成群友 我看看能不能")]}
        result = parse_evidence(items)(encoded(value))
        self.assertEqual(result["tags"][0]["quote"], "@合成群友 我看看能不能和你调到一个宿舍。")
        self.assertEqual(result["values"][0]["quote"], "@合成群友 我看看能不能")
        for unrelated in ("@合成群友 我不想调宿舍", "@合成群友 ……宿舍。"):
            with self.subTest(quote=unrelated):
                self.assertEqual(parse_evidence(items)(encoded({**value, "tags": [tag(0, unrelated, "mention")]}))["tags"], [])

    def test_values_self_reports_must_be_first_person_statements(self):
        value = copy.deepcopy(self.value)
        value["values"] = [values(0, "我喜欢篮球", "selfReport"), values(0, "篮球", "selfReport")]
        self.assertEqual([row["quote"] for row in parse_evidence(self.items)(encoded(value))["values"]], ["我喜欢篮球"])

    def test_one_message_can_supply_multiple_tags_with_the_same_message_index(self):
        value = copy.deepcopy(self.value)
        value["tags"].append({**tag(0, "我是INFP", "trait"), "name": "INFP自述", "category": "身份生活"})
        self.assertEqual([row["i"] for row in parse_evidence(self.items)(encoded(value))["tags"]], [0, 0])
        value["tags"][1]["i"] = 1  # the quote still names message 0
        self.assertEqual([row["i"] for row in parse_evidence(self.items)(encoded(value))["tags"]], [0, 0])

    def test_index_only_review_preserves_every_original_identity_and_quote(self):
        proposed = parse_evidence(self.items)(encoded(self.value))
        decisions = {"profile": [{"n": 0, "relation": "interest"}],
                     "tags": [{"n": 0, "relation": "mention", "reason": "复核后仅保留话题线索"}],
                     "persona": [{"n": 0}], "values": [{"n": 0}]}
        result = parse_decisions(self.items, proposed)(encoded(decisions))
        self.assertEqual(result["profile"], proposed["profile"])
        self.assertEqual(result["persona"], proposed["persona"])
        self.assertEqual(result["values"], proposed["values"])
        self.assertEqual(result["tags"][0], {**proposed["tags"][0], "relation": "mention", "reason": "复核后仅保留话题线索"})
        self.assertEqual(proposed["tags"][0]["relation"], "interest")
        self.assertTrue(all("n" not in row for domain in result.values() for row in domain))
        self.assertEqual(parse_decisions(self.items, proposed)(encoded({"profile": [], "tags": [], "persona": [], "values": []})),
                         {"profile": [], "tags": [], "persona": [], "values": []})

    def test_n_only_review_preserves_relations_and_distinct_candidates_with_identical_identity(self):
        proposed = parse_evidence(self.items)(encoded(self.value))
        result = parse_decisions(self.items, proposed)(encoded({domain: [{"n": 0}] for domain in proposed}))
        self.assertEqual(result, proposed)
        proposed["tags"].append({**proposed["tags"][0], "relation": "mention"})
        result = parse_decisions(self.items, proposed)(encoded({"profile": [], "tags": [{"n": 0}, {"n": 1}], "persona": [], "values": []}))
        self.assertEqual([row["relation"] for row in result["tags"]], ["interest", "mention"])
        self.assertTrue(all(row["quote"] == proposed["tags"][0]["quote"] for row in result["tags"]))

    def test_index_only_review_rejects_out_of_range_duplicate_or_forged_fields(self):
        proposed = parse_evidence(self.items)(encoded(self.value))
        for decision in ({"n": 1, "relation": "mention"}, {"n": -1, "relation": "mention"},
                         {"n": True, "relation": "mention"}, {"n": 0, "relation": "mention", "quote": "改写"},
                         {"n": 0, "relation": "mention", "i": 0}, {"n": 0, "relation": "mention", "name": "假造"},
                         {"n": 0, "relation": "trait"}, {"n": 0, "relation": "mention", "reason": "x" * 201}):
            with self.subTest(decision=decision):
                with self.assertRaises(SelfError):
                    parse_decisions(self.items, proposed)(encoded({"profile": [], "tags": [decision], "persona": [], "values": []}))
        with self.assertRaises(SelfError):
            parse_decisions(self.items, proposed)(encoded({"profile": [], "tags": [{"n": 0, "relation": "mention"}] * 2, "persona": [], "values": []}))
        with self.assertRaises(SelfError):
            parse_decisions(self.items, proposed)(encoded({"profile": [], "tags": [], "persona": [{"n": 0, "kind": "support"}], "values": []}))
        with self.assertRaises(SelfError):
            parse_decisions(self.items, proposed)(encoded({"profile": [], "tags": [], "persona": [], "values": [{"n": 0, "relation": "trait"}]}))
        weakened = copy.deepcopy(proposed)
        weakened["tags"][0]["relation"] = "mention"
        with self.assertRaises(SelfError):
            parse_decisions(self.items, weakened)(encoded({"profile": [], "tags": [{"n": 0, "relation": "interest"}], "persona": [], "values": []}))

    def test_context_packing_never_merges_distinct_chats_or_group_speakers(self):
        alice = {**line(1, "相同原话", speaker="群友·甲"), "id": "same-local-id"}
        bob = {**alice, "speaker": "群友·乙"}
        items = [{**self.items[0], "user": "group-a", "context": {"before": [alice, bob], "after": [alice], "truncated": True}},
                 {**self.items[0], "i": 1, "user": "group-b", "context": {"before": [alice], "after": []}},
                 {**self.items[0], "i": 2, "user": "group-a", "context": {"before": [bob, alice], "after": []}}]
        original = copy.deepcopy(items)
        packed = pack_messages(items)
        self.assertEqual(len(packed["contextRows"]), 3)
        self.assertEqual(packed["messages"][0]["context"]["before"], [0, 1])
        self.assertEqual(packed["messages"][2]["context"]["before"], [1, 0])
        self.assertEqual(unpack_messages(packed), original)
        self.assertEqual(items, original)

    def test_joint_review_can_remove_or_downgrade_but_cannot_rewrite_or_add_evidence(self):
        proposed = parse_evidence(self.items)(encoded(self.value))
        weaker = copy.deepcopy(self.value)
        weaker["tags"][0]["relation"] = "mention"
        weaker["persona"] = []
        self.assertEqual(parse_evidence(self.items, proposed)(encoded(weaker))["tags"][0]["relation"], "mention")
        for domain, field, value in (("tags", "name", "打篮球"), ("tags", "quote", "喜欢篮球"),
                                     ("profile", "text", "另一种事实"), ("persona", "kind", "support"),
                                     ("persona", "value", "INTJ"), ("tags", "relation", "trait"),
                                     ("values", "value", "改写后的观点")):
            with self.subTest(domain=domain, field=field):
                invalid = copy.deepcopy(self.value)
                invalid[domain][0][field] = value
                with self.assertRaises(SelfError):
                    parse_evidence(self.items, proposed)(encoded(invalid))
        mention = copy.deepcopy(self.value)
        mention["tags"][0]["relation"] = "mention"
        with self.assertRaises(SelfError):
            parse_evidence(self.items, parse_evidence(self.items)(encoded(mention)))(encoded(self.value))

    def test_incomplete_context_and_message_fragments_do_not_establish_stable_traits(self):
        for flag in ("truncated", "timeUnknown", "sourceIncomplete", "omittedBefore"):
            with self.subTest(flag=flag):
                items = copy.deepcopy(self.items)
                items[0]["context"][flag] = True
                result = parse_evidence(items)(encoded(self.value))
                self.assertEqual(result["tags"][0]["relation"], "uncertain")
                self.assertEqual(result["profile"][0]["relation"], "uncertain")
                self.assertEqual(result["persona"], [])
        items = copy.deepcopy(self.items)
        items[0].update(parts=2, messageFrame={"head": "我喜欢篮球", "tail": "以上是转述朋友说的，不是我"})
        result = parse_evidence(items)(encoded(self.value))
        self.assertEqual(result["tags"][0]["relation"], "uncertain")
        self.assertEqual(result["persona"], [])


class RetryTests(unittest.TestCase):
    def test_transient_core_blips_are_retried(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise CoreError(0, "core-unavailable", "无法连接 Core")
            return "ok"

        self.assertEqual(retry_unavailable(flaky), "ok")
        self.assertEqual(len(calls), 3)
        with self.assertRaises(CoreError):
            retry_unavailable(lambda: (_ for _ in ()).throw(CoreError(500, "account-changed", "变化")))


if __name__ == "__main__":
    unittest.main()
