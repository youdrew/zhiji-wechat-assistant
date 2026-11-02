"""Shared-corpus tag analysis; synthetic text, a temporary DB and fake models only."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]

from kyf_plugin import Cancelled, ModelError
from self_contracts import SelfError
from self_store import Store
from self_tag_store import TagStore
from self_tags import TagJob
from test_tags import Context, Models, answer, model_tag, support


def own(seq, text, *, user="friend", group=False, before=(), after=(), info=None):
    return {"rowid": seq, "id": json.dumps([user, f"m{seq}"], ensure_ascii=False), "user": user,
            "seq": seq, "time_ms": 1_700_000_000_000 + seq * 1000, "text": text, "name": "合成聊天",
            "is_group": group, "context": "\n".join(item["speaker"] + "：" + item["text"] for item in before),
            "context_before": list(before), "context_after": list(after), "context_info": info or {
                "truncated": False, "timeUnknown": False, "windowMinutes": 60, "beforeLimit": 12, "afterLimit": 6}}


def line(seq, text, *, side="other", speaker="对方"):
    return {"id": f"context-{seq}", "seq": seq, "time": 1_700_000_000_000 + seq * 1000,
            "side": side, "speaker": speaker, "text": text, "truncated": False}


class Corpus:
    def __init__(self, rows):
        self.messages = rows
        self.metadata = {"source": "shared-synthetic-source", "version": 4, "revision": 1}
        self.gaps = []

    def meta(self, key, default=None):
        return self.metadata.get(key, default)

    def counts(self):
        return {"ownMessages": len(self.messages), "catalogComplete": True, "gaps": self.gaps}

    def iter_messages(self, after_id=0, limit=200):
        return copy.deepcopy([row for row in self.messages if row["rowid"] > after_id][:limit])

    def change(self, rows):
        self.messages = rows
        self.metadata["revision"] += 1


class NoHistory:
    def __getattr__(self, name):
        raise AssertionError("shared tags must not call Core history: " + name)


class SharedTagCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "synthetic-shared-tags")
        self.tags, self.models = TagStore(self.store), Models()
        self.plugin = SimpleNamespace(core=NoHistory(), models=self.models)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_job(self, corpus, selected_depth="deep", context=None):
        job = TagJob(self.plugin, self.store, "synthetic-shared-tags", self.tags,
                     corpus=corpus, selected_depth=selected_depth)
        result = job.work(context or Context())
        self.tags.update(state="complete")
        return result

    def tag(self, name):
        return next((tag for tag in self.tags.tags() if tag["name"] == name), None)

    def observed_job(self, corpus, selected_depth="deep"):
        job = TagJob(self.plugin, self.store, "synthetic-shared-tags", self.tags,
                     corpus=corpus, selected_depth=selected_depth)
        updates, context = [], Context()
        job.on_update = lambda value: updates.append(copy.deepcopy(value))
        self.assertEqual(job.run(context), "done", job.view())
        return job, updates, context.reports

    def test_shared_progress_uses_message_units_for_extraction_review_and_batch_units_for_merge(self):
        corpus = Corpus([own(seq, ("我喜欢篮球" if seq % 2 else "我喜欢打球") + str(seq))
                         for seq in range(1, 31)])
        job, updates, reports = self.observed_job(corpus)
        extracts = [value for value in updates if value.get("activeCallStep") == "self-tags"]
        reviews = [value for value in updates if value.get("activeCallStep") == "self-tags-review"]
        merges = [value for value in updates if value.get("activeCallStep") == "self-tags-merge"]
        self.assertTrue(extracts and reviews and merges)
        for value in extracts:
            self.assertEqual((value["phase"], value["unit"], value["total"], value["ownMessages"]),
                             ("extracting", "条", 30, 30))
        source_batches = [request["prompt"]["messages"] for request in self.models.requests
                          if request["step"] == "self-tags"]
        before_batches, completed = set(), 0
        for batch in source_batches:
            before_batches.add(completed)
            completed += len(batch)
        for value in reviews:
            self.assertEqual((value["phase"], value["unit"], value["total"]), ("reviewing", "条", 30))
            self.assertIn(value["done"], before_batches)
            self.assertEqual(value["analyzedMessages"], value["done"],
                             "an in-flight review cannot claim its current messages are already saved")
        for value in merges:
            self.assertEqual((value["phase"], value["unit"], value["total"]), ("merging", "批", 1))
            self.assertEqual((value["ownMessages"], value["analyzedMessages"]), (30, 30))
        self.assertTrue(any(value["phase"] == "reviewing" and value["unit"] == "条" for value in reports))
        self.assertTrue(any(value["phase"] == "merging" and value["unit"] == "批" for value in reports))
        final = job.view()
        self.assertEqual((final["state"], final["phase"], final["done"], final["total"], final["unit"]),
                         ("done", "done", 30, 30, "条"))
        self.assertEqual((final["ownMessages"], final["analyzedMessages"], final["reusedMessages"],
                          final["newlyAnalyzedMessages"]), (30, 30, 0, 30))
        self.assertEqual(final["modelCalls"], len(self.models.requests))
        self.assertIsNone(final["activeCallStartedAt"])
        self.assertEqual((reports[-1]["done"], reports[-1]["total"], reports[-1]["unit"]), (30, 30, "条"))

    def test_shared_progress_counts_reused_and_new_whole_messages_with_one_stable_total(self):
        original = [own(seq, "我喜欢篮球" + str(seq)) for seq in range(1, 4)]
        corpus = Corpus(original)
        self.run_job(corpus, "basic")
        corpus.change([*original, own(4, "我喜欢篮球，新增的一条")])
        before = len(self.models.requests)
        job, updates, _ = self.observed_job(corpus, "basic")
        extracting = [value for value in updates if value.get("phase") == "extracting"]
        self.assertTrue(extracting)
        self.assertEqual({value["total"] for value in extracting}, {4})
        self.assertEqual((extracting[0]["done"], extracting[0]["reusedMessages"],
                          extracting[0]["newlyAnalyzedMessages"]), (3, 3, 0))
        sent = [item["id"] for request in self.models.requests[before:] if request["step"] == "self-tags"
                for item in request["prompt"]["messages"]]
        self.assertEqual(sent, [own(4, "unused")["id"]], "progress instrumentation never rereads cached model inputs")
        self.assertTrue(any(value.get("phase") == "merging" and value["done"] == value["total"] == 0
                            and value["unit"] == "批" for value in updates))
        final = job.view()
        self.assertEqual((final["done"], final["total"], final["unit"], final["ownMessages"], final["analyzedMessages"]),
                         (4, 4, "条", 4, 4), "an empty merge must not replace the final source coverage with 0/0")
        self.assertEqual((final["reusedMessages"], final["newlyAnalyzedMessages"]), (3, 1))
        calls = len(self.models.requests)
        cached, _, _ = self.observed_job(corpus, "basic")
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual((cached.view()["done"], cached.view()["total"], cached.view()["unit"],
                          cached.view()["reusedMessages"], cached.view()["newlyAnalyzedMessages"]),
                         (4, 4, "条", 4, 0))

    def test_all_shared_own_text_including_short_and_emoji_reaches_model_without_history_reads(self):
        texts = ["好", "？", "🙂"] + [f"合成本人第{seq}句" for seq in range(4, 1505)]
        corpus = Corpus([own(seq, text) for seq, text in enumerate(texts, 1)])
        self.run_job(corpus)
        sent = [item["me"] for request in self.models.requests if request["step"] == "self-tags"
                for item in request["prompt"]["messages"]]
        self.assertEqual(sent, texts)
        self.assertEqual(self.tags.counts()["messages"], len(texts))
        self.assertEqual(self.tags.counts()["pendingMessages"], 0)

    def test_full_structured_group_context_and_late_retraction_reach_both_deep_reviews(self):
        before = [line(seq, f"前文{seq}", speaker=f"群友·成员{seq}") for seq in range(1, 13)]
        before[-1]["text"] = "你喜欢篮球吗"
        after = [line(seq, f"后文{seq}", speaker=f"群友·成员{seq}") for seq in range(14, 20)]
        after[-1] = line(19, "前面是在开玩笑", side="self", speaker="本人")
        corpus = Corpus([own(13, "对啊", user="group@chatroom", group=True, before=before, after=after)])
        proposed = answer(model_tag("篮球", "运动健身", [support(0, "对啊", "interest")]))
        reviewed = answer(model_tag("篮球", "运动健身", [support(0, "对啊", "mention", "后文撤回，不能认定为本人兴趣")]))
        self.models.replies = [proposed, reviewed, reviewed]
        self.run_job(corpus)
        for request in self.models.requests:
            item = request["prompt"]["messages"][0]
            self.assertEqual((item["ctx"], item["after"]), (before, after))
            self.assertEqual(request["prompt"]["depth"], "deep")
        self.assertEqual(len([request for request in self.models.requests if request["step"] == "self-tags-review"]), 2)
        self.assertEqual((self.tag("篮球")["relation"], self.tag("篮球")["level"]), ("mention", "possible"))

    def test_unchanged_shared_snapshot_has_no_model_calls_but_depth_and_rules_invalidate_cache(self):
        corpus = Corpus([own(1, "我喜欢篮球")])
        self.run_job(corpus, "basic")
        self.assertEqual(len(self.models.requests), 1)
        self.run_job(corpus, "basic")
        self.assertEqual(len(self.models.requests), 1)
        self.run_job(corpus, "standard")
        self.assertEqual(len(self.models.requests), 3)
        self.run_job(corpus, "deep")
        self.assertEqual(len(self.models.requests), 6)
        with patch("self_tags.SHARED_RULE_VERSION", 999):
            self.run_job(corpus, "deep")
        self.assertEqual(len(self.models.requests), 9)

    def test_changed_context_invalidates_old_interest_and_rechecks_original_message(self):
        corpus = Corpus([own(1, "我喜欢篮球")])
        self.run_job(corpus)
        self.assertEqual(self.tag("篮球")["level"], "strong")
        corpus.change([own(1, "我喜欢篮球", after=[line(2, "开玩笑的，我从来不打", side="self", speaker="本人")])])
        proposed = answer(model_tag("篮球", "运动健身", [support(0, "我喜欢篮球", "interest")]))
        reviewed = answer(model_tag("篮球", "运动健身", [support(0, "我喜欢篮球", "mention")]))
        self.models.replies = [proposed, reviewed, reviewed]
        original_generate = self.models.generate

        def inspect_invalidated(task, request, step=None, timeout=None):
            if step == "self-tags":
                self.assertEqual(self.tags.evidence("篮球")["total"], 0)
            return original_generate(task, request, step, timeout)

        self.models.generate = inspect_invalidated
        self.run_job(corpus)
        self.assertEqual((self.tag("篮球")["relation"], self.tag("篮球")["level"]), ("mention", "possible"))
        self.assertIn("开玩笑", self.tags.evidence("篮球")["items"][0]["followup"])

    def test_modified_and_removed_sources_drop_ai_evidence_but_keep_edits_and_aliases(self):
        corpus = Corpus([own(1, "我喜欢篮球"), own(2, "象棋我最擅长")])
        self.run_job(corpus)
        self.tags.edit("confirm", "篮球")
        self.tags.edit("delete", "象棋")
        self.tags.edit("add", "夜猫子", "性格习惯")
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO tag_aliases VALUES(?,?)", ("打篮球", "篮球"))
        corpus.change([own(1, "我不喜欢篮球")])
        updated = answer(model_tag("不喜欢篮球", "运动健身", [support(0, "我不喜欢篮球", "trait")]))
        self.models.replies = [updated, updated, updated]
        self.run_job(corpus)
        self.assertEqual(self.tags.evidence("篮球")["total"], 0)
        self.assertEqual((self.tag("篮球")["level"], self.tag("篮球")["mentions"]), ("confirmed", 0))
        self.assertIsNone(self.tag("象棋"))
        self.assertEqual(self.tags.evidence("象棋")["total"], 0)
        self.assertEqual(self.tag("夜猫子")["level"], "confirmed")
        self.assertEqual(self.tag("不喜欢篮球")["level"], "strong")
        self.assertEqual(self.tags.rows("SELECT * FROM tag_aliases"), [{"key": "打篮球", "name": "篮球"}])

    def test_incomplete_shared_context_cannot_be_promoted_even_if_model_claims_interest(self):
        for missing in ({"truncated": True}, {"timeUnknown": True}, {"omittedUnknownTime": 1}):
            with self.subTest(missing=missing):
                corpus = Corpus([own(1, "我喜欢篮球", info=missing)])
                self.run_job(corpus)
                self.assertEqual((self.tag("篮球")["relation"], self.tag("篮球")["level"]), ("uncertain", "possible"))

    def test_long_shared_body_is_not_limited_to_1200_and_budget_elision_is_explicit_uncertain(self):
        complete = "我喜欢篮球" + "完整本人文字" * 500 + "末尾原文"
        corpus = Corpus([own(1, complete)])
        self.run_job(corpus)
        self.assertEqual(self.models.requests[0]["prompt"]["messages"][0]["me"], complete)
        large = "我喜欢篮球" + "更长的完整原文" * 9000 + "其实前面是在开玩笑"
        corpus.change([own(1, large)])
        proposed = answer(model_tag("篮球", "运动健身", [support(0, "我喜欢篮球", "interest")]))
        self.models.replies = [proposed, proposed, proposed]
        before = len(self.models.requests)
        self.run_job(corpus)
        item = self.models.requests[before]["prompt"]["messages"][0]
        self.assertTrue(item["truncated"])
        self.assertTrue(item["contextInfo"]["bodyClipped"])
        self.assertTrue(item["me"].startswith("我喜欢篮球"))
        self.assertTrue(item["me"].endswith("其实前面是在开玩笑"))
        self.assertEqual(self.tags.evidence("篮球")["items"][0]["text"], large)
        self.assertEqual((self.tag("篮球")["relation"], self.tag("篮球")["level"]), ("uncertain", "possible"))

    def test_paused_shared_analysis_reuses_finished_batches_on_resume(self):
        corpus = Corpus([own(seq, "我喜欢篮球" + str(seq)) for seq in range(1, 31)])
        context, original_generate, extracts = Context(), self.models.generate, []

        def stop_second_batch(task, request, step=None, timeout=None):
            if step == "self-tags":
                extracts.append(json.loads(request["prompt"])["messages"])
                if len(extracts) == 2:
                    task.cancelled.set()
            return original_generate(task, request, step, timeout)

        self.models.generate = stop_second_batch
        with self.assertRaises(Cancelled):
            self.run_job(corpus, context=context)
        self.assertGreater(self.tags.counts()["pendingMessages"], 0)
        self.assertLess(self.tags.counts()["pendingMessages"], 30)
        completed = {item["id"] for item in extracts[0]}
        self.models.generate = original_generate
        before = len(self.models.requests)
        self.run_job(corpus)
        resumed = {item["id"] for request in self.models.requests[before:] if request["step"] == "self-tags"
                   for item in request["prompt"]["messages"]}
        self.assertTrue(completed.isdisjoint(resumed))
        self.assertEqual(self.tags.counts()["pendingMessages"], 0)

    def test_small_context_budget_explicitly_marks_elided_context_uncertain(self):
        before = [line(seq, "前文开头" + "上下文" * 190 + "前文结尾") for seq in range(1, 13)]
        after = [line(seq, "后文开头" + "上下文" * 190 + "后文结尾") for seq in range(14, 20)]
        corpus = Corpus([own(13, "我喜欢篮球", before=before, after=after)])
        self.models.service = lambda refresh=False: {"contextTokens": 4096}
        self.run_job(corpus)
        item = self.models.requests[0]["prompt"]["messages"][0]
        self.assertTrue(item["contextInfo"]["truncated"])
        self.assertTrue(item["contextInfo"]["budgetClipped"])
        self.assertEqual((self.tag("篮球")["relation"], self.tag("篮球")["level"]), ("uncertain", "possible"))
        for request in self.models.requests:
            size = len(json.dumps(request["prompt"], ensure_ascii=False)) + len(request["system"])
            self.assertLess(size + 512, 4096)

    def test_source_gaps_are_passed_to_the_model_and_never_establish_strong_interest(self):
        corpus = Corpus([own(1, "我喜欢篮球")])
        corpus.gaps = [{"kind": "unreadable-conversations", "count": 1}]
        self.run_job(corpus)
        self.assertTrue(self.models.requests[0]["prompt"]["messages"][0]["contextInfo"]["sourceIncomplete"])
        self.assertEqual(self.tag("篮球")["level"], "possible")
        before = len(self.models.requests)
        corpus.gaps = []
        self.run_job(corpus)
        self.assertGreater(len(self.models.requests), before)
        self.assertEqual(self.tag("篮球")["level"], "strong")

    def test_corpus_changes_during_generation_cannot_commit_stale_model_evidence(self):
        corpus = Corpus([own(1, "我喜欢篮球")])
        original_generate = self.models.generate

        def change_source(task, request, step=None, timeout=None):
            result = original_generate(task, request, step, timeout)
            corpus.metadata["source"] = "different-shared-source"
            return result

        self.models.generate = change_source
        with self.assertRaises(SelfError) as caught:
            self.run_job(corpus)
        self.assertEqual(caught.exception.code, "source-changed")
        self.assertEqual(self.tags.evidence("篮球")["total"], 0)
        self.assertEqual(self.tags.counts()["pendingMessages"], 1)

    def test_model_configuration_failure_does_not_block_source_evidence_invalidation(self):
        corpus = Corpus([own(1, "我喜欢篮球"), own(2, "象棋我最擅长")])
        self.run_job(corpus)
        self.tags.edit("add", "手动保留", "其他")
        requests = len(self.models.requests)
        self.models.configured = False
        with self.assertRaises(ModelError):
            self.run_job(corpus)
        self.assertEqual(self.tag("篮球")["level"], "strong", "valid unchanged cached evidence may remain")
        corpus.change([own(1, "我从来不打篮球")])
        with self.assertRaises(ModelError):
            self.run_job(corpus)
        self.assertEqual(len(self.models.requests), requests)
        self.assertIsNone(self.tag("篮球"))
        self.assertIsNone(self.tag("象棋"))
        self.assertEqual(self.tags.evidence("篮球")["total"], 0)
        self.assertEqual(self.tags.evidence("象棋")["total"], 0)
        self.assertEqual(self.tag("手动保留")["level"], "confirmed")
        self.assertEqual((self.tags.counts()["messages"], self.tags.counts()["pendingMessages"]), (1, 1))

    def test_projection_renumbering_reuses_semantic_cache_and_updates_stored_locations(self):
        message = own(10, "我喜欢篮球", before=[line(8, "前文问题"), line(9, "前文补充")],
                      after=[line(11, "后文回应"), line(12, "后文补充")])
        corpus = Corpus([message])
        self.run_job(corpus)
        calls, evidence = len(self.models.requests), self.tags.evidence("篮球")
        renumbered = copy.deepcopy(message)
        renumbered["rowid"], renumbered["seq"] = 9, 9
        for item in renumbered["context_before"] + renumbered["context_after"]:
            item["seq"] -= 1
        corpus.change([renumbered])
        self.run_job(corpus)
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual(self.tags.evidence("篮球"), evidence)
        stored = self.tags.rows("SELECT seq,context_data,state FROM tag_messages WHERE id=?", (message["id"],))[0]
        self.assertEqual((stored["seq"], stored["state"]), (9, "done"))
        context = json.loads(stored["context_data"])
        self.assertEqual([item["seq"] for item in context["before"]], [7, 8])
        self.assertEqual([item["seq"] for item in context["after"]], [10, 11])

    def test_semantic_identity_changes_still_invalidate_a_renumbering_safe_cache(self):
        original = own(10, "我喜欢篮球", before=[line(9, "这是朋友的问题")], after=[line(11, "这是朋友的回应")])
        corpus = Corpus([original])
        self.run_job(corpus)
        changes = [(('id',), 'different-source-message'), (('time_ms',), original['time_ms'] + 1000),
                   (('text',), '我喜欢篮球，这是新的原文'),
                   (('context_before', 0, 'id'), 'different-context-message'),
                   (('context_before', 0, 'time'), original['context_before'][0]['time'] + 1000),
                   (('context_before', 0, 'speaker'), '另一个朋友'),
                   (('context_before', 0, 'side'), 'self'),
                   (('context_after', 0, 'text'), '后文改为不同含义'),
                   (('context_after', 0, 'truncated'), True)]
        for path, value in changes:
            with self.subTest(path=path):
                altered = copy.deepcopy(original)
                target = altered
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                corpus.change([altered])
                calls = len(self.models.requests)
                self.run_job(corpus)
                self.assertGreater(len(self.models.requests), calls)


if __name__ == "__main__":
    unittest.main()
