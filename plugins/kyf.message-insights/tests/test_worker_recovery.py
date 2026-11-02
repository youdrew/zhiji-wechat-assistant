"""Synthetic model failures, partial retries, and refreshes across the real worker/store."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugin-sdk/python"))
sys.path.insert(0, str(ROOT / "plugins/kyf.message-insights/backend"))
from kyf_plugin import Cancelled
from kyf_plugin.models import ModelError
from prompts import InsightError, message_hash
from store import AnalysisStore, result_bindings
from worker import Job, Worker

ACCOUNT, USER, MESSAGE = "synthetic-account", "synthetic-friend", "synthetic-message"


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = AnalysisStore(Path(temp.name), ACCOUNT)
        self.addCleanup(self.store.close)
        self.store.set(USER, True)
        self.store.begin(USER, [MESSAGE], "generation", parts=("analysis", "suggestions"), unified=True)
        self.row = {"msg_id": MESSAGE, "seq": 1, "side": "other", "sender_id": USER,
                    "group_chat": False, "kind": "text", "text": "Synthetic hello", "raw": "{}", "time_ms": 1000}
        self.status = {"state": "ready", "sourceFingerprint": "source", "epoch": 1}
        self.requests, self.finishes, self.responses = [], [], []
        self.core = SimpleNamespace(post=lambda *_: {}, history_status=lambda *_: dict(self.status),
            history_messages=lambda *_, **__: {"messages": []},
            get=lambda *_, **__: {"stamp": "new-profile"})
        self.context = SimpleNamespace(core=self.core, cancelled=threading.Event(),
            finish=lambda *args: self.finishes.append(args))
        plugin = SimpleNamespace(core=self.core, models=SimpleNamespace(service=lambda: {"contextTokens": 100000, "stamp": "model"},
                                                                        service_for=lambda *_, **__: {"contextTokens": 100000, "stamp": "model"},
                                                                        generate=self.generate))
        self.insights = SimpleNamespace(plugin=plugin, store=lambda _: self.store,
            config_stamp=lambda **_: "model",
            check_account=lambda _: None, projected=lambda *_: [dict(self.row)],
            binding=lambda *_, **__: ("source", "model", "old-profile", 1), wake=threading.Event())
        self.worker = Worker(self.insights)
        self.worker.media = SimpleNamespace(collect=lambda *_, **__: {})
        self.job = Job(ACCOUNT, USER, [MESSAGE], {"automatic": False, "generation": "generation", "revision": 1,
            "settings": {"values": {"replyStyle": "natural"}}, "unified": True,
            "parts": ("analysis", "suggestions"), "preserved_context": "old-context"})
        self.patch_sources = patch("worker.structured_sources", return_value=[])
        self.patch_sources.start()
        self.addCleanup(self.patch_sources.stop)
        self.patch_delay = patch("worker.RETRY_DELAY", 0)
        self.patch_delay.start()
        self.addCleanup(self.patch_delay.stop)

    @staticmethod
    def analysis():
        return {"text": json.dumps({"analyses": [{"messageId": MESSAGE, "summary": "Synthetic summary",
            "meaning": "Synthetic meaning", "uncertainty": "Uncertain", "responseDirection": "Reply naturally",
            "evidenceIds": ["s1"]}]})}

    @staticmethod
    def replies():
        return {"text": json.dumps({"replies": [{"label": "Natural", "text": text}
                                               for text in ("Hello", "Good morning", "Hi there")]})}

    def missing_evidence(self):
        value = json.loads(self.analysis()["text"])
        del value["analyses"][0]["evidenceIds"]
        return {"text": json.dumps(value)}

    def generate(self, context, request, **kwargs):
        self.requests.append((dict(request), kwargs))
        response = self.responses.pop(0)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response

    def run_work(self):
        self.worker._work(self.job, self.context)
        return self.store.rows(USER, [MESSAGE])[0]

    def test_transient_502_recovers_and_profile_refresh_does_not_discard_results(self):
        self.responses = [ModelError("provider-error", "safe", http_status=502), self.analysis(), self.replies()]
        row = self.run_work()
        self.assertEqual(row["state"], "ready")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.finishes[-1][0], "done")
        self.assertEqual(result_bindings(row)["analysis"]["profile_stamp"], "new-profile")

    def personal_context(self, **overrides):
        value = {"available": True, "revision": 1, "generation": 0, "sourceFingerprint": "source",
                 "profileVersion": 1, "profile": {"style": "少用感叹号"},
                 "sources": [{"id": "past", "user": "another-friend", "sameContact": False,
                              "input": "合成历史邀约", "reply": "我想一下，晚点跟你说"}]}
        return {**value, **overrides}

    def test_cross_contact_expression_only_enters_suggestions_not_factual_analysis(self):
        calls = []
        def retrieve(*args, **kwargs):
            calls.append((args, kwargs))
            return self.personal_context()
        self.core.self_context = retrieve
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        analysis, suggestions = [json.loads(request[0]["prompt"]) for request in self.requests]
        self.assertNotIn("personalReplyStyle", analysis)
        reference = suggestions["personalReplyStyle"]
        self.assertEqual(reference["examples"][0]["ownReply"], "我想一下，晚点跟你说")
        self.assertFalse(reference["examples"][0]["sameContact"])
        self.assertNotIn("another-friend", json.dumps(reference))
        self.assertEqual(calls[0][0][:3], (ACCOUNT, USER, MESSAGE))
        self.assertEqual(len(calls), 2, "the final write checks that the knowledge has not been cleared")
        self.assertTrue(all(source["kind"] == "chat" for source in suggestions["sources"]))

    def test_contextless_targets_skip_history_but_keep_normal_material_and_own_preferences(self):
        self.patch_sources.stop()
        preference = "不要用亲昵称呼，少用感叹号"
        self.core.get = lambda *_, **__: {"stamp": "new-profile", "data": {
            "profile": {"style": preference, "background": "合成的本人背景"},
            "goal": "", "memories": [], "proofs": {}, "observations": [], "exclusions": []}}
        calls = []
        self.core.self_context = lambda *args, **kwargs: calls.append((args, kwargs)) or self.personal_context()
        transcript = "这是微信已有的合成语音转写"
        cases = [("text", " \t\n", {}), ("text", "！？……", {}), ("text", "🙂🎉1️⃣", {}),
                 ("image", "[图片]", {}), ("sticker", "[动画表情]", {}),
                 ("voice", "[语音]", {"media": {"kind": "voice", "transcript": transcript,
                                                   "transcriptSource": "wechat"}})]
        for kind, text, raw in cases:
            with self.subTest(kind=kind, text=text):
                self.requests.clear()
                self.row.update(kind=kind, text=text, raw=json.dumps(raw, ensure_ascii=False))
                observation = "一只猫躺在纸箱里。" if kind in ("image", "sticker") else None
                self.worker.media.collect = lambda *_, **__: {MESSAGE: observation} if observation else {}
                self.responses = [self.analysis(), self.replies()]
                self.assertEqual(self.run_work()["state"], "ready")
                self.assertEqual(calls, [], "no historical situation can be inferred from these targets")
                self.assertEqual([kwargs["step"] for _, kwargs in self.requests], ["analysis", "suggestions"])
                for request, _ in self.requests:
                    prompt = json.loads(request["prompt"])
                    self.assertEqual(prompt["targets"], [MESSAGE])
                    self.assertIn(MESSAGE, prompt["targetSourceIds"])
                    self.assertNotIn("personalReplyStyle", prompt)
                    material_text = "\n".join(source["text"] for source in prompt["sources"])
                    self.assertIn(preference, material_text)
                    if observation:
                        self.assertIn(observation, material_text)
                    if kind == "voice":
                        self.assertIn(transcript, material_text)
                        self.assertIn("未听原音", material_text)

    def test_emoji_or_media_target_still_uses_existing_typed_context(self):
        calls = []
        self.core.self_context = lambda *args, **kwargs: calls.append((args, kwargs)) or self.personal_context()
        earlier = {**self.row, "msg_id": "earlier", "seq": 1, "text": "周末要不要一起吃饭？"}
        self.row.update(seq=2, time_ms=2000)
        self.insights.projected = lambda account, user, ids: [dict(row) for row in (earlier, self.row)
                                                             if row["msg_id"] in ids]
        self.core.history_messages = lambda *_, **__: {"messages": [{
            "id": earlier["msg_id"], "seq": 1, "side": earlier["side"], "senderId": USER,
            "kind": "text", "text": earlier["text"], "time": earlier["time_ms"], "raw": "{}"}]}
        for kind, text in (("text", "🙂"), ("image", "[图片]")):
            with self.subTest(kind=kind):
                self.requests.clear()
                calls.clear()
                self.row.update(kind=kind, text=text)
                self.responses = [self.analysis(), self.replies()]
                self.assertEqual(self.run_work()["state"], "ready")
                self.assertEqual(len(calls), 2)
                expected = earlier["text"] + ("\n🙂" if kind == "text" else "")
                self.assertTrue(all(args[3] == expected for args, _ in calls))
                analysis, suggestions = [json.loads(request["prompt"]) for request, _ in self.requests]
                self.assertNotIn("personalReplyStyle", analysis)
                self.assertTrue(suggestions["personalReplyStyle"]["examples"])
                self.assertEqual(len(self.requests), 2, "the optional references do not add a model call")

    def test_literal_text_is_not_misclassified_as_media_by_its_placeholder_spelling(self):
        calls = []
        self.row.update(kind="text", text="[图片]")
        self.core.self_context = lambda *args, **kwargs: calls.append(args) or self.personal_context()
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertEqual([args[3] for args in calls], ["[图片]", "[图片]"])

    def test_history_applicability_guidance_is_only_in_the_existing_suggestions_request(self):
        self.core.self_context = lambda *args, **kwargs: self.personal_context()
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        analysis, suggestions = [request for request, _ in self.requests]
        for instruction in ("谁向谁提出要求", "接受与拒绝", "暂时不便与不愿继续", "许可、关系或承诺范围不兼容", "不必凑用"):
            self.assertIn(instruction, suggestions["system"])
            self.assertNotIn(instruction, analysis["system"])
        self.assertIn("当前语境、明确偏好和关系边界优先", suggestions["system"])
        self.assertEqual(len(self.requests), 2)

    def test_disabled_provider_keeps_existing_suggestions_usable(self):
        self.core.self_context = lambda *args, **kwargs: {"available": False, "reason": "disabled"}
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertNotIn("personalReplyStyle", json.loads(self.requests[-1][0]["prompt"]))

    def test_knowledge_clear_while_generating_discards_only_unfinished_suggestions(self):
        values = iter([self.personal_context(), self.personal_context(generation=1)])
        self.core.self_context = lambda *args, **kwargs: next(values)
        self.responses = [self.analysis(), self.replies()]
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("ready", "error"))
        self.assertIn("知识库", row["suggestions_error"])

    def test_new_batches_do_not_invalidate_an_in_flight_reply(self):
        values = iter([self.personal_context(), self.personal_context(revision=2)])
        self.core.self_context = lambda *args, **kwargs: next(values)
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")

    def test_validation_with_a_new_generation_cannot_accept_pre_clear_material(self):
        value = self.personal_context()
        self.core.self_context = lambda *args, **kwargs: value
        self.core.self_context_validate = lambda *args, **kwargs: {**value, "valid": True, "generation": 1}
        self.responses = [self.analysis(), self.replies()]
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("ready", "error"))

    def test_precise_validation_accepts_only_the_same_profile_source_and_generation(self):
        value = self.personal_context()
        self.core.self_context = lambda *args, **kwargs: value
        self.core.self_context_validate = lambda *args, **kwargs: {**value, "valid": True, "revision": 99}
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")

    def test_cancellation_while_validating_never_saves_the_pending_suggestions(self):
        value = self.personal_context()
        self.core.self_context = lambda *args, **kwargs: value
        def validate(*args, **kwargs):
            self.context.cancelled.set()
            return {**value, "valid": True}
        self.core.self_context_validate = validate
        self.responses = [self.analysis(), self.replies()]
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("ready", "error"))

    def test_oversized_optional_examples_leave_a_complete_target_and_bounded_prompt(self):
        value = self.personal_context(sources=[{"input": "很长的历史" * 10000, "reply": "很长的回复" * 10000}])
        self.core.self_context = lambda *args, **kwargs: value
        self.responses = [self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        request = json.loads(self.requests[-1][0]["prompt"])
        self.assertIn(MESSAGE, request["targetSourceIds"])
        self.assertLess(len(self.requests[-1][0]["prompt"]), 10000)

    def test_invalid_citations_get_one_correction_using_the_same_sources(self):
        wrong = self.analysis()
        wrong["text"] = wrong["text"].replace('"s1"', '"made-up"')
        self.responses = [wrong, self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        first, retry = self.requests[0][0], self.requests[1][0]
        self.assertEqual(first["prompt"], retry["prompt"])
        self.assertGreater(len(retry["system"]), len(first["system"]))

    def test_502_then_missing_evidence_still_gets_a_successful_format_correction(self):
        self.responses = [ModelError("provider-error", "safe", http_status=502),
                          self.missing_evidence(), self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.finishes[-1][0], "done")
        first, transport_retry, corrected = [entry[0] for entry in self.requests[:3]]
        self.assertEqual(first, transport_retry)
        self.assertEqual(first["prompt"], corrected["prompt"])
        self.assertIn("evidenceIds", corrected["system"][len(first["system"]):])

    def test_format_correction_survives_502_and_uses_the_same_corrected_request(self):
        self.responses = [self.missing_evidence(), ModelError("provider-error", "safe", http_status=502),
                          self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertEqual(len(self.requests), 4)
        self.assertNotEqual(self.requests[0][0]["system"], self.requests[1][0]["system"])
        self.assertEqual(self.requests[1][0], self.requests[2][0])

    def test_two_transport_retries_and_one_correction_allow_at_most_four_calls_per_part(self):
        failure = ModelError("provider-error", "safe", http_status=502)
        self.responses = [failure, self.missing_evidence(), failure, self.analysis(), self.replies()]
        with patch("worker.RETRY_DELAY", 1), patch.object(self.worker, "_wait_retry") as wait:
            self.assertEqual(self.run_work()["state"], "ready")
        self.assertEqual([call.args[2] for call in wait.call_args_list], [1, 2])
        self.assertEqual([entry[1]["step"] for entry in self.requests], ["analysis"] * 4 + ["suggestions"])
        self.assertEqual(self.requests[2][0], self.requests[3][0])

    def test_continuous_502_stops_after_three_calls_and_keeps_completed_sibling(self):
        self.responses = [self.analysis()] + [ModelError("provider-error", "safe", http_status=502)] * 3
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("ready", "error"))
        self.assertTrue(row["analysis_json"])
        self.assertIn("502", row["suggestions_error"])
        self.assertEqual([entry[1]["step"] for entry in self.requests], ["analysis"] + ["suggestions"] * 3)
        self.assertEqual(self.finishes[-1][0], "partial")

    def test_second_malformed_response_stops_without_spending_transport_budget(self):
        self.responses = [self.missing_evidence(), self.missing_evidence(), self.replies()]
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("error", "ready"))
        self.assertIn("证据引用", row["error"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.finishes[-1][0], "partial")

    def test_all_budgets_exhausted_stop_at_four_calls(self):
        failure = ModelError("provider-error", "safe", http_status=502)
        self.responses = [failure, self.missing_evidence(), failure, self.missing_evidence(), self.replies()]
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("error", "ready"))
        self.assertEqual([entry[1]["step"] for entry in self.requests], ["analysis"] * 4 + ["suggestions"])

    def test_transport_retry_budget_is_independent_for_each_part(self):
        failure = ModelError("provider-error", "safe", http_status=503)
        self.responses = [failure, failure, self.analysis(), failure, failure, self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertEqual(len(self.requests), 6)

    def test_auth_is_not_retried_and_both_failures_mark_task_failed(self):
        self.responses = [ModelError("auth", "safe", http_status=401), ModelError("auth", "safe", http_status=401)]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.finishes[-1][0], "failed")

    def test_permanent_error_codes_never_retry_even_with_retryable_http_metadata(self):
        self.responses = [ModelError("auth", "safe", http_status=503),
                          ModelError("context-too-long", "safe", http_status=502)]
        row = self.run_work()
        self.assertEqual(row["state"], "error")
        self.assertIn("认证失败", row["error"])
        self.assertIn("上下文超过", row["suggestions_error"])
        self.assertEqual(len(self.requests), 2)

    def test_non_transient_http_failures_do_not_retry(self):
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.responses = [ModelError("provider-error", "safe", http_status=status)] * 2
                before = len(self.requests)
                self.run_work()
                self.assertEqual(len(self.requests) - before, 2)

    def test_known_permanent_http_status_overrides_a_generic_network_error_code(self):
        self.responses = [ModelError("network", "safe", http_status=401),
                          ModelError("network-error", "safe", http_status=413)]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 2)

    def test_timeout_and_network_failures_use_transport_budget(self):
        self.responses = [ModelError("timeout"), ModelError("network-error"), self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertEqual(len(self.requests), 4)

    def test_truncated_output_gets_one_larger_bounded_correction(self):
        self.responses = [ModelError("output-truncated"), self.analysis(), self.replies()]
        self.assertEqual(self.run_work()["state"], "ready")
        self.assertGreater(self.requests[1][0]["maxOutputTokens"], self.requests[0][0]["maxOutputTokens"])
        self.assertLessEqual(self.requests[1][0]["maxOutputTokens"], 4096)

    def test_cancellation_before_retry_never_calls_model_again(self):
        def cancelled_gateway():
            self.context.cancelled.set()
            return ModelError("provider-error", "safe", http_status=502)
        self.responses = [cancelled_gateway]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 1)

    def test_cancellation_during_backoff_stops_before_the_next_call(self):
        self.responses = [ModelError("provider-error", "safe", http_status=502)]
        with patch("worker.RETRY_DELAY", 1), patch.object(self.context.cancelled, "wait",
                side_effect=lambda _: self.context.cancelled.set()) as wait:
            self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(len(self.requests), 1)

    def test_account_switch_after_502_stops_before_retry(self):
        def changed():
            def check_account(_):
                raise InsightError("account-changed", "Synthetic account changed")
            self.insights.check_account = check_account
            return ModelError("provider-error", "safe", http_status=502)
        self.responses = [changed]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 1)

    def test_source_switch_after_502_stops_before_retry(self):
        def changed():
            self.status["currentSourceFingerprint"] = "different-live-source"
            return ModelError("provider-error", "safe", http_status=502)
        self.responses = [changed]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 1)

    def test_model_switch_after_malformed_response_stops_before_correction(self):
        def changed():
            self.insights.config_stamp = lambda **_: "different-model"
            return self.missing_evidence()
        self.responses = [changed]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 1)

    def test_cancellation_after_success_retains_completed_analysis(self):
        self.responses = [self.analysis(), Cancelled()]
        row = self.run_work()
        self.assertEqual((row["analysis_state"], row["suggestions_state"]), ("ready", "error"))
        self.assertTrue(row["analysis_json"])
        self.assertIn("中断", row["suggestions_error"])

    def test_source_change_during_generation_rejects_late_result(self):
        def changed():
            self.status["sourceFingerprint"] = "another-source"
            return self.analysis()
        self.responses = [changed]
        row = self.run_work()
        self.assertEqual(row["state"], "error")
        self.assertFalse(row["analysis_json"])

    def test_model_change_cannot_mislabel_a_late_result_with_the_previous_model(self):
        def changed():
            self.insights.config_stamp = lambda **_: "different-model"
            return self.analysis()
        self.responses = [changed]
        row = self.run_work()
        self.assertEqual(row["state"], "error")
        self.assertFalse(row["analysis_json"])
        self.assertIn("模型配置", row["error"])

    def test_live_source_switch_is_detected_even_before_projection_catches_up(self):
        def changed():
            self.status["currentSourceFingerprint"] = "different-live-source"
            return self.analysis()
        self.responses = [changed]
        self.assertEqual(self.run_work()["state"], "error")
        self.assertEqual(len(self.requests), 1)

    def test_temporary_reading_waits_and_does_not_invalidate_frozen_messages(self):
        states = iter(["reading", "reading", "ready"])
        self.core.history_status = lambda *_: {**self.status, "state": next(states, "ready")}
        self.job.binding = ("source", "model", "old-profile", 1)
        self.job.hashes = {MESSAGE: message_hash(self.row)}
        with patch("worker.time.sleep"):
            self.worker._check(self.job, self.context, fresh=True)

    def test_refresh_during_message_read_does_not_look_like_a_deleted_message(self):
        states = iter(["ready", "reading", "ready", "ready"])
        self.core.history_status = lambda *_: {**self.status, "state": next(states, "ready")}
        reads = iter([[], [self.row]])
        self.insights.projected = lambda *_: next(reads)
        self.job.binding = ("source", "model", "old-profile", 1)
        self.job.hashes = {MESSAGE: message_hash(self.row)}
        self.worker._check(self.job, self.context, fresh=True)

    def test_partial_retry_accepts_new_context_preserves_original_analysis_binding(self):
        self.responses = [self.analysis(), ModelError("auth", "safe", http_status=401)]
        old = self.run_work()
        self.store.begin(USER, [MESSAGE], "retry", parts=("suggestions",), unified=True)
        self.job.spec.update(generation="retry", parts=("suggestions",))
        self.core.get = lambda *_, **__: {"stamp": "later-profile"}
        self.responses = [self.replies()]
        new = self.run_work()
        self.assertEqual(new["state"], "ready")
        self.assertEqual(new["analysis_json"], old["analysis_json"])
        self.assertEqual(result_bindings(new)["analysis"], result_bindings(old)["analysis"])
        self.assertEqual(result_bindings(new)["suggestions"]["profile_stamp"], "later-profile")

    def test_exhausted_regeneration_keeps_both_saved_results_and_original_bindings(self):
        self.responses = [self.analysis(), self.replies()]
        old = self.run_work()
        self.store.begin(USER, [MESSAGE], "retry", parts=("suggestions",), unified=True)
        self.job.spec.update(generation="retry", parts=("suggestions",))
        self.core.get = lambda *_, **__: {"stamp": "later-profile"}
        self.responses = [ModelError("provider-error", "safe", http_status=502)] * 3
        new = self.run_work()
        self.assertEqual((new["analysis_state"], new["suggestions_state"]), ("ready", "error"))
        for field in ("analysis_json", "replies_json"):
            self.assertEqual(new[field], old[field])
        for part in ("analysis", "suggestions"):
            self.assertEqual(result_bindings(new)[part], result_bindings(old)[part])


if __name__ == "__main__":
    unittest.main()
