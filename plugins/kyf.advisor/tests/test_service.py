"""Synthetic service tests: runs, events, stop, restart projection and account clear."""
# Modified by Know Your Friends, 2026-10-09 (Apache-2.0): background contact
# generation, independent cancellation and persisted result recovery regressions.
import tempfile
import threading
import time
import unittest
from pathlib import Path

from contracts import AdvisorError, managed_context_cost, model_skill_text, utf8_cost
from support import build_service
from contracts import AdvisorError as AccountChangedError
from support import FakeModelStore, FakeRuntime, FakeSource, make_message, wait_until

ACCOUNT = "synthetic-account-1"
USER = "user-a"
AGENT = "builtin:advisor"


class ContactGatedRuntime(FakeRuntime):
    """Hold independent synthetic requests without a provider or native process."""

    def __init__(self, users):
        super().__init__()
        self.entered = {user: threading.Event() for user in users}
        self.release = {user: threading.Event() for user in users}

    def respond(self, config, request, on_event, cancel_event):
        self.respond_calls.append((config, request))
        user = request["user"]
        partial = "synthetic partial for " + user
        on_event({"type": "text", "text": partial})
        self.entered[user].set()
        deadline = time.monotonic() + 5
        while not self.release[user].wait(.01) and not cancel_event.is_set():
            if time.monotonic() >= deadline:
                raise RuntimeError("synthetic test gate timed out")
        final = partial + "; completed"
        if not cancel_event.is_set():
            on_event({"type": "text", "text": "; completed"})
        # A cancelled provider may still return a final result. Service must
        # ignore it while preserving only the already-published stopped partial.
        return {"text": final, "runtimeSessionId": "synthetic-session-" + user}

    def release_all(self):
        for event in self.release.values():
            event.set()


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = FakeSource(account=ACCOUNT)
        self.source.seed_pages(USER, 300)
        self.model_store = FakeModelStore(context_tokens=32768)
        self.runtime = FakeRuntime()
        self.service = self.build_service()
        self.addCleanup(self.service.shutdown)

    def build_service(self, runtime=None, runtime_factory="default"):
        if runtime_factory == "default":
            runtime_factory = (lambda: runtime or self.runtime)
        return build_service(self.source, self.model_store, self.root,
                              runtime_factory=runtime_factory)

    # -- helpers -------------------------------------------------------------
    def context(self):
        return self.source.history_status(ACCOUNT, USER)

    def wait_context_ready(self):
        self.service.prepare_context(ACCOUNT, USER)
        ready = wait_until(lambda: self.context() if self.context()["state"] == "ready" else None)
        self.assertIsNotNone(ready, "context did not become ready")
        return ready

    def wait_run(self, run_id, states=("done", "stopped", "error"), service=None):
        service = service or self.service

        def terminal():
            result = service.events(ACCOUNT, USER, run_id, 0)
            return result if result["run"]["state"] in states else None

        result = wait_until(terminal)
        self.assertIsNotNone(result, "run did not reach a terminal state")
        # Completion can be published just before the worker releases its scope.
        # A subsequent operation must wait for that documented drain boundary.
        if result["run"]["state"] == "done":
            worker = service._runs[run_id].worker
            worker.join(2)
            self.assertFalse(worker.is_alive(), "completed worker did not drain")
        return result

    def messages(self):
        return self.service.thread(ACCOUNT, USER, AGENT)["thread"]["messages"]


class CatalogTests(ServiceTestCase):
    def test_default_catalog_and_mutations_never_call_the_model(self):
        catalog = self.service.catalog()
        self.assertEqual(catalog["permissions"], {"readOnly": True})
        self.assertEqual(catalog["engine"], {"state": "available"})
        self.assertEqual(len(catalog["agents"]), 3)
        self.assertTrue(all(agent["revision"] >= 1 for agent in catalog["agents"]))
        created = self.service.save_template({"action": "save", "agent": {
            "name": "我的参谋", "description": "", "prompt": "帮我分析",
            "skillIds": ["builtin-skill:advisor"]}})
        custom = [agent for agent in created["agents"] if not agent["builtin"]]
        self.assertEqual(len(custom), 1)
        disabled = self.service.save_template({"action": "enable",
                                               "id": custom[0]["id"], "enabled": False})
        self.assertFalse([agent for agent in disabled["agents"]
                          if agent["id"] == custom[0]["id"]][0]["enabled"])
        imported = self.service.import_skill({"name": "倾听", "description": "说明",
                                              "content": "先倾听再回应。"})
        self.assertIn("倾听", [skill["name"] for skill in imported["skills"]])
        deleted = self.service.save_template({"action": "delete", "id": custom[0]["id"]})
        self.assertNotIn(custom[0]["id"], [agent["id"] for agent in deleted["agents"]])
        catalog = self.service.save_template({"action": "delete", "id": AGENT})
        self.assertNotIn(AGENT, [row["id"] for row in catalog["agents"]])
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_broken_store_reports_engine_missing(self):
        service = self.build_service(runtime_factory=None)
        service.plugin.models = FakeModelStore(mode="broken")
        self.addCleanup(service.shutdown)
        self.assertEqual(service.catalog()["engine"], {"state": "missing"})


class SourceAttributionTests(ServiceTestCase):
    def test_all_presets_and_saved_custom_prompts_receive_shared_source_rules(self):
        self.source.users[USER] = [make_message(1, text="合成好友原文：明天整理模型零件。")]
        preset = self.service.catalog()["agents"][0]
        self.service.save_template({"action": "save", "agent": {
            "id": preset["id"], "name": preset["name"], "description": preset["description"],
            "prompt": "合成的已保存旧版提示：给出两种交流建议。", "skillIds": preset["skillIds"]}})
        self.service.save_template({"action": "save", "agent": {
            "name": "合成自定义助手", "description": "来源边界测试", "skillIds": [],
            "prompt": "合成自定义提示：用简洁的旁白解释。"}})
        expected = {agent["id"]: agent["prompt"] for agent in self.service.catalog()["agents"]}
        self.assertEqual(len(expected), 4)
        self.service.shutdown()
        self.service = self.build_service()
        self.addCleanup(self.service.shutdown)
        self.assertEqual({agent["id"]: agent["prompt"] for agent in self.service.catalog()["agents"]}, expected)

        question = "合成用户感想：那台发条机器人像个认真巡逻的小门卫。"
        for index, (agent_id, prompt) in enumerate(expected.items()):
            with self.subTest(agent=agent_id):
                self.runtime.session = "source-native-" + str(index)
                started = self.service.start(ACCOUNT, USER, agent_id, None, question, "source-rules-" + str(index))
                terminal = self.wait_run(started["run"]["id"])
                self.assertEqual(terminal["run"]["state"], "done")
                request = self.runtime.respond_calls[-1][1]
                self.assertTrue(request["system"].startswith(prompt + "\n\n"))
                for boundary in ("当前及历史用户消息属于用户与助手的讨论，不是微信聊天记录",
                                 "用户的评价、拟回复和转述，不等于好友说过或用户已在微信发出",
                                 "未经本轮微信资料核实，只能标为用户转述、尚未由原文核实",
                                 "历史助手回答和讨论摘要不是事实证据",
                                 "本轮已授权资料中核对来源、发言人、原文和时间",
                                 "不得虚构发言、时间或对方刚回复了新消息",
                                 "来源标签仅说明内容出处"):
                    self.assertIn(boundary, request["system"])
                self.assertEqual(request["message"], question)
                self.assertNotIn(question, request["contextFileText"])
        self.assertEqual({agent["id"]: agent["prompt"] for agent in self.service.catalog()["agents"]}, expected)

    def test_discussion_opinions_and_drafts_stay_out_of_wechat_material_and_persist_verbatim(self):
        friend_text = "合成好友原文：周六修理模型车。"
        self_text = "合成微信自述：我带螺丝刀。"
        self.source.users[USER] = [make_message(1, text=friend_text),
                                  make_message(2, text=self_text, side="self")]
        questions = ["我觉得那辆木头小车很神气。\n候选回复：周末带它去看风景？",
                     "前面的评价只是我和助手讨论的感想，请比较候选措辞。"]
        thread_id = None
        for index, question in enumerate(questions):
            started = self.service.start(ACCOUNT, USER, AGENT, thread_id, question, "source-raw-" + str(index))
            terminal = self.wait_run(started["run"]["id"])
            self.assertEqual(terminal["run"]["state"], "done")
            thread_id = started["threadId"]
            request = self.runtime.respond_calls[-1][1]
            self.assertEqual(request["message"], question)
            self.assertIn(friend_text, request["contextFileText"])
            self.assertIn(self_text, request["contextFileText"])
            for discussion in questions:
                self.assertNotIn(discussion, request["contextFileText"])
                self.assertNotIn(discussion, request["system"])

        self.service.shutdown()
        self.service = self.build_service()
        self.addCleanup(self.service.shutdown)
        persisted = self.service.stores.account(ACCOUNT).thread_messages(thread_id)
        self.assertEqual([message["text"] for message in persisted if message["role"] == "user"], questions)
        self.assertTrue(all("[来源：" not in message["text"] for message in persisted))


class RunTests(ServiceTestCase):
    def test_switching_contact_keeps_both_runs_and_restores_their_saved_results(self):
        other = "synthetic-user-b"
        self.source.seed_pages(other, 2)
        self.source.users[USER][-1]["text"] = "SYNTHETIC_CONTACT_A_ONLY"
        self.source.users[other][0]["text"] = "SYNTHETIC_CONTACT_B_ONLY"
        self.runtime = ContactGatedRuntime((USER, other))
        self.addCleanup(self.runtime.release_all)
        first = self.service.start(ACCOUNT, USER, AGENT, None, "synthetic question A", "background-a")
        self.assertTrue(self.runtime.entered[USER].wait(2))
        first_run = self.service._runs[first["run"]["id"]]
        epoch = self.service.stores.account(ACCOUNT).context(USER)["epoch"]

        # These are the backend reads a contact switch performs. They neither
        # stop A nor turn B's context into A's source identity.
        destination = self.service.thread(ACCOUNT, other, AGENT)
        self.service.prepare_context(ACCOUNT, other)
        second = self.service.start(ACCOUNT, other, AGENT, destination["thread"]["id"],
                                    "synthetic question B", "background-b")
        self.assertTrue(self.runtime.entered[other].wait(2))
        self.assertFalse(first_run.cancel.is_set())
        self.assertEqual(first_run.state, "running")
        self.runtime.release[other].set()
        second_done = wait_until(lambda: (result if result["run"]["state"] == "done" else None)
                                 if (result := self.service.events(ACCOUNT, other, second["run"]["id"])) else None)
        self.assertIsNotNone(second_done)
        returned = self.service.thread(ACCOUNT, USER, AGENT)
        self.assertEqual(returned["run"]["id"], first["run"]["id"])
        self.assertEqual(returned["run"]["state"], "running")
        self.assertEqual(self.service.stores.account(ACCOUNT).context(USER)["epoch"], epoch)
        self.runtime.release[USER].set()
        first_done = self.wait_run(first["run"]["id"])
        self.assertEqual(first_done["run"]["state"], "done")
        payloads = {request["user"]: request for _config, request in self.runtime.respond_calls}
        self.assertIn("SYNTHETIC_CONTACT_A_ONLY", payloads[USER]["contextFileText"])
        self.assertNotIn("SYNTHETIC_CONTACT_B_ONLY", payloads[USER]["contextFileText"])
        self.assertIn("SYNTHETIC_CONTACT_B_ONLY", payloads[other]["contextFileText"])
        self.assertNotIn("SYNTHETIC_CONTACT_A_ONLY", payloads[other]["contextFileText"])
        self.assertNotEqual(payloads[USER]["threadId"], payloads[other]["threadId"])

        saved = {user: self.service.thread(ACCOUNT, user, AGENT) for user in (USER, other)}
        self.assertEqual([row["text"] for row in saved[USER]["thread"]["messages"]],
                         ["synthetic question A", "synthetic partial for " + USER + "; completed"])
        self.assertEqual([row["text"] for row in saved[other]["thread"]["messages"]],
                         ["synthetic question B", "synthetic partial for " + other + "; completed"])
        self.service.shutdown()
        recovered_runtime = FakeRuntime()
        recovered = self.build_service(runtime=recovered_runtime)
        self.addCleanup(recovered.shutdown)
        for user in (USER, other):
            resumed = recovered.thread(ACCOUNT, user, AGENT)
            self.assertEqual(resumed["thread"], saved[user]["thread"])
            self.assertEqual(resumed["run"]["id"], saved[user]["run"]["id"])
            self.assertEqual(resumed["run"]["state"], "done")
        self.assertEqual(recovered_runtime.respond_calls, [])

    def test_stop_targets_one_contact_and_rejects_cross_contact_run_ids(self):
        other = "synthetic-user-b"
        self.source.seed_pages(other, 2)
        self.runtime = ContactGatedRuntime((USER, other))
        self.addCleanup(self.runtime.release_all)
        first = self.service.start(ACCOUNT, USER, AGENT, None, "synthetic question A", "stop-isolated-a")
        second = self.service.start(ACCOUNT, other, AGENT, None, "synthetic question B", "stop-isolated-b")
        self.assertTrue(self.runtime.entered[USER].wait(2))
        self.assertTrue(self.runtime.entered[other].wait(2))
        with self.assertRaises(AdvisorError) as wrong_contact:
            self.service.stop(ACCOUNT, other, first["run"]["id"])
        self.assertEqual(wrong_contact.exception.code, "run-unknown")
        self.assertFalse(self.service._runs[first["run"]["id"]].cancel.is_set())
        self.service.stop(ACCOUNT, USER, first["run"]["id"])
        self.assertTrue(wait_until(lambda: not self.service._runs[first["run"]["id"]].worker.is_alive()))
        second_live = self.service._runs[second["run"]["id"]]
        self.assertEqual(second_live.state, "running")
        self.assertFalse(second_live.cancel.is_set())
        self.runtime.release[other].set()
        self.assertTrue(wait_until(lambda: self.service.events(ACCOUNT, other, second["run"]["id"])["run"]["state"] == "done"))
        stopped = self.service.thread(ACCOUNT, USER, AGENT)["thread"]["messages"][-1]
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["text"], "synthetic partial for " + USER)
        completed = self.service.thread(ACCOUNT, other, AGENT)["thread"]["messages"][-1]
        self.assertNotIn("status", completed)
        self.assertEqual(completed["text"], "synthetic partial for " + other + "; completed")

    def test_run_streams_events_and_persists_projection(self):
        ready = self.wait_context_ready()
        self.runtime.finish_gate = threading.Event()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "我该怎么回复？", "req-1")
        run_id = result["run"]["id"]
        self.assertEqual(result["run"]["state"], "running")
        # The transcript shows the user message while the run is still generating.
        during = self.service.thread(ACCOUNT, USER, AGENT)
        self.assertEqual([message["role"] for message in during["thread"]["messages"]], ["user"])
        self.assertEqual(during["run"]["state"], "running")
        self.runtime.finish_gate.set()
        terminal = self.wait_run(run_id)
        self.assertEqual(terminal["run"]["state"], "done")
        self.assertIn("thread", terminal)
        kinds = [event["type"] for event in terminal["events"]]
        self.assertIn("reasoning", kinds)
        self.assertIn("text", kinds)
        self.assertIn("done", kinds)
        sequences = [event["seq"] for event in terminal["events"]]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        messages = terminal["thread"]["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["text"], self.runtime.text)
        self.assertNotIn("status", messages[1])
        self.assertEqual(terminal["thread"]["runtimeSessionId"], "sess-1")
        self.assertEqual(self.context()["readCount"], 300)
        self.assertEqual(self.context()["revision"], ready["revision"])

        config, request = self.runtime.respond_calls[0]
        self.assertNotIn("apiKey", config)
        self.assertNotIn("stateDir", config)
        self.assertEqual(config["id"], self.model_store.source_id)
        self.assertEqual(self.service.engine.calls[-1][1], self.service.plugin.tasks[-1].token)
        self.assertIn("狗头军师", request["system"])
        self.assertNotIn("参谋建议框架", request["system"])
        self.assertEqual(request["agentId"], AGENT)
        self.assertIn("参谋建议框架", request["skills"][0]["content"])
        self.assertEqual(request["message"], "我该怎么回复？")
        self.assertEqual(request["selectedSkillIds"], ["builtin-skill:advisor"])
        self.assertEqual(request["contextRevision"], ready["revision"])
        self.assertNotIn("contextMessageCount", request)
        self.assertNotIn("context", request)
        self.assertIn("[最近聊天记录]", request["contextFileText"])
        self.assertIn("消息内容 1", request["contextFileText"])
        self.assertIn("消息内容 300", request["contextFileText"])
        self.assertNotIn("我该怎么回复？", request["contextFileText"])

        # The terminal event can precede Core task cleanup; direct start, like queue dispatch, waits for drain.
        self.assertTrue(wait_until(lambda: not self.service._runs[run_id].worker.is_alive()))
        second = self.service.start(ACCOUNT, USER, AGENT, result["threadId"], "再想想", "req-2")
        self.wait_run(second["run"]["id"])
        self.assertEqual(self.runtime.respond_calls[1][1]["runtimeSessionId"], "sess-1")
        self.assertEqual([message["role"] for message in self.messages()],
                         ["user", "assistant", "user", "assistant"])

    def test_prepare_context_alone_never_calls_the_model(self):
        self.wait_context_ready()
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_long_chat_summary_disables_thinking_but_answer_preserves_it_and_timeout(self):
        self.source.seed_pages(USER, 400, text_size=400)
        services = self.model_store.services
        configured_timeout, accepted = [3_600_000], []
        self.model_store.services = lambda refresh=False: [
            {**service, "timeoutMs": configured_timeout[0]} for service in services(refresh=refresh)]
        compact = self.runtime.compact

        def change_timeout_after_first_summary(config, request, on_event, cancel):
            accepted.append(config)
            configured_timeout[0] = 7_200_000
            return compact(config, request, on_event, cancel)

        self.runtime.compact = change_timeout_after_first_summary
        first = self.service.start(ACCOUNT, USER, AGENT, None, "合成讨论", "summary-thinking-1", thinking=True)
        self.assertEqual(self.wait_run(first["run"]["id"])["run"]["state"], "done")
        self.assertTrue(self.runtime.compact_calls, "synthetic history should require summarization")
        self.assertTrue(all(call["thinking"] is False for call in self.runtime.compact_calls))
        self.assertEqual(accepted[0]["timeoutMs"], 3_600_000, "an accepted call keeps its original deadline")
        self.assertTrue(all(config["timeoutMs"] == 7_200_000 for config in accepted[1:]))
        config, request = self.runtime.respond_calls[-1]
        self.assertTrue(request["thinking"])
        self.assertEqual(config["timeoutMs"], 7_200_000)
        second = self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "继续合成讨论",
                                    "summary-thinking-2", thinking=False)
        self.assertEqual(self.wait_run(second["run"]["id"])["run"]["state"], "done")
        self.assertFalse(self.runtime.respond_calls[-1][1]["thinking"])

    def test_verbose_english_summaries_still_reach_the_answer(self):
        self.source.seed_pages(USER, 400, text_size=400)
        self.model_store.context_tokens = 131072
        outputs = []

        def verbose_summary(config, request, on_event, cancel):
            self.runtime.compact_calls.append(dict(request))
            # A verbose first packet must not abort all of the useful packets.
            # Later rounds can consolidate them to the real context budget.
            text = ("Synthetic English evidence. " * 230 if not outputs else
                    "Source facts and dates. " * 130 if request["level"] == 1 else
                    "A complete neutral account of the synthetic conversation.")
            outputs.append(text)
            return {"text": text}

        self.runtime.compact = verbose_summary
        started = self.service.start(ACCOUNT, USER, AGENT, None,
                                     "Synthetic question", "verbose-summary", thinking=True)
        result = self.wait_run(started["run"]["id"])
        self.assertEqual(result["run"]["state"], "done", result["run"])
        self.assertTrue(any(len(text) > 6000 for text in outputs))
        self.assertTrue(any(2000 < len(text) < 6000 for text in outputs))
        self.assertTrue(all(call["targetSummaryChars"] > 0 and
                            "maxSummaryChars" not in call and call["thinking"] is False
                            for call in self.runtime.compact_calls))
        self.assertEqual(len(self.runtime.respond_calls), 1)
        self.assertTrue(self.runtime.respond_calls[0][1]["thinking"])

    def assert_fitting_history_stays_original(self, content):
        self.source.users[USER] = [make_message(index, text=content + str(index)) for index in range(1, 13)]
        started = self.service.start(ACCOUNT, USER, AGENT, None, "Synthetic question", "byte-fit")
        result = self.wait_run(started["run"]["id"])
        self.assertEqual(result["run"]["state"], "done", result["run"])
        self.assertFalse(self.runtime.compact_calls, "history fits the byte budget without dividing it by four")
        request = self.runtime.respond_calls[-1][1]
        self.assertIn(content + "1", request["contextFileText"])
        self.assertIn(content + "12", request["contextFileText"])
        return started, request

    def test_english_history_uses_available_utf8_capacity_without_premature_compaction(self):
        self.assert_fitting_history_stays_original("Synthetic English content. " * 35)

    def test_chinese_history_uses_actual_utf8_cost_without_premature_compaction(self):
        self.assert_fitting_history_stays_original("合成中文" * 70)

    def test_current_question_that_truly_exceeds_utf8_window_is_rejected(self):
        started = self.service.start(ACCOUNT, USER, AGENT, None, "界" * 10000, "byte-overflow")
        result = self.wait_run(started["run"]["id"])
        self.assertEqual(result["run"]["state"], "error")
        self.assertFalse(self.runtime.respond_calls)
        self.assertFalse(self.runtime.compact_calls)
        self.assertEqual(self.messages()[0]["text"], "界" * 10000)

    def test_controlled_native_rebuild_uses_same_material_byte_budget(self):
        from engine_client import EngineError
        first, _ = self.assert_fitting_history_stays_original("Synthetic English content. " * 35)
        respond, rejected = self.runtime.respond, []

        def missing_once(config, request, events, cancel):
            if request.get("runtimeSessionId") and not rejected:
                rejected.append(dict(request))
                raise EngineError("native-session-missing")
            return respond(config, request, events, cancel)

        self.runtime.respond = missing_once
        started = self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "Continue", "byte-rebuild")
        result = self.wait_run(started["run"]["id"])
        self.assertEqual(result["run"]["state"], "done", result["run"])
        request = self.runtime.respond_calls[-1][1]
        self.assertEqual(len(rejected), 1)
        self.assertIsNone(request["runtimeSessionId"])
        self.assertIn("Synthetic question", request["recoveryText"])
        self.assertFalse(self.runtime.compact_calls, "rebuild must not reintroduce the old /4 material limit")
        self.assertEqual(request["contextFileText"], rejected[0]["contextFileText"])

    def test_native_history_is_debited_before_allocating_current_material(self):
        first, _ = self.assert_fitting_history_stays_original("Synthetic English content. " * 45)
        self.runtime.inspect_session = lambda *_args: {"state": "idle", "effectiveHistoryTokensEstimate": 6000}
        started = self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "Continue", "byte-history")
        result = self.wait_run(started["run"]["id"])
        self.assertEqual(result["run"]["state"], "done", result["run"])
        request = self.runtime.respond_calls[-1][1]
        self.assertIsNotNone(request["runtimeSessionId"], "small native history need not be rebuilt")
        cost = utf8_cost(request["system"] + request["message"] + model_skill_text(request["skills"]))
        budget = self.service._formal_budget(self.service._runs[started["run"]["id"]],
                                             {"contextTokens": self.model_store.context_tokens}, request["contextRevision"])
        self.assertLessEqual(cost + managed_context_cost(request["contextFileText"]) + 6000, budget)
        self.assertTrue(self.runtime.compact_calls, "history reserve leaves less space for the new material")

    def test_actual_provider_output_truncation_is_not_reported_as_context_overflow(self):
        from engine_client import EngineError

        def truncated(*_args):
            raise EngineError("output-truncated")

        self.runtime.respond = truncated
        started = self.service.start(ACCOUNT, USER, AGENT, None,
                                     "Synthetic question", "provider-truncation")
        result = self.wait_run(started["run"]["id"])
        self.assertEqual(result["run"]["state"], "error")
        self.assertIn("输出达到生成上限", result["run"]["error"])
        self.assertNotIn("上下文", result["run"]["error"])
        self.assertEqual(self.messages()[0]["text"], "Synthetic question")

    def test_early_run_waits_without_adding_a_reading_flow_to_the_timeline(self):
        self.source.page_delay = 0.05
        self.service.prepare_context(ACCOUNT, USER)
        result = self.service.start(ACCOUNT, USER, AGENT, None, "先看一点", "req-early")
        run_id = result["run"]["id"]
        thread = self.service.thread(ACCOUNT, USER, AGENT)["thread"]
        roles = [(message["role"], message.get("status")) for message in thread["messages"]]
        self.assertEqual(roles[0], ("user", None))
        self.assertEqual(roles, [("user", None)])
        self.assertNotIn("state", self.service.thread(ACCOUNT, USER, AGENT)["context"])
        self.source.page_delay = 0
        terminal = self.wait_run(run_id)
        self.assertEqual(terminal["run"]["state"], "done")
        after_all = self.service.events(ACCOUNT, USER, run_id, 10 ** 6)
        self.assertEqual(after_all["events"], [])

    def test_stop_preserves_partial_output_and_blocks_late_save(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        result = self.service.start(ACCOUNT, USER, AGENT, None, "停一下", "req-stop")
        run_id = result["run"]["id"]
        self.assertTrue(wait_until(self.runtime.paused.is_set))
        stopped = self.service.stop(ACCOUNT, USER, run_id)
        self.assertEqual(stopped["run"]["state"], "stopped")
        assistant = [message for message in self.messages() if message["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["text"], "这是合")
        self.assertEqual(assistant[0]["status"], "stopped")
        # Late runtime output after the stop must not create another transcript row.
        time.sleep(0.2)
        self.assertEqual(len([message for message in self.messages()
                              if message["role"] == "assistant"]), 1)
        terminal = self.service.events(ACCOUNT, USER, run_id, 0)
        self.assertEqual(terminal["run"]["state"], "stopped")
        self.assertEqual(terminal["events"][-1]["type"], "done")
        self.assertEqual(terminal["events"][-1]["state"], "stopped")

    def test_new_thread_keeps_background_run_and_context(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        started = self.service.start(ACCOUNT, USER, AGENT, None, "第一线程", "req-new-1")
        self.assertTrue(wait_until(self.runtime.paused.is_set))
        created = self.service.new_thread(ACCOUNT, USER, AGENT)
        self.assertNotEqual(created["thread"]["id"], started["threadId"])
        self.assertEqual(created["thread"]["messages"], [])
        self.assertEqual(set(created["context"]), {"revision"})
        old = self.service.events(ACCOUNT, USER, started["run"]["id"], 0)
        self.assertEqual(old["run"]["state"], "running")
        self.assertFalse(self.service._runs[started["run"]["id"]].cancel.is_set())
        self.runtime.pause_after_first_chunk = False
        second = self.service.start(ACCOUNT, USER, AGENT, created["thread"]["id"], "新的开始", "req-new-2")
        self.wait_run(second["run"]["id"])
        self.assertEqual(len(self.messages()), 2)
        self.service.stop(ACCOUNT, USER, started["run"]["id"])

    def test_new_thread_keeps_old_thread_writable_and_selection_stable(self):
        original = self.service.thread(ACCOUNT, USER, AGENT)["thread"]
        self.service.save_draft(ACCOUNT, USER, AGENT, original["id"], "unfinished", 0)
        current = self.service.new_thread(ACCOUNT, USER, AGENT)["thread"]
        self.assertNotEqual(original["id"], current["id"])
        started = self.service.start(ACCOUNT, USER, AGENT, original["id"], "synthetic delayed question", "delayed")
        self.assertEqual(self.wait_run(started["run"]["id"])["run"]["state"], "done")
        self.assertEqual(self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"], current["id"])
        self.assertEqual(self.service.stores.account(ACCOUNT).thread_messages(current["id"]), [])

    def test_start_before_new_remains_running_and_retry_is_idempotent(self):
        self.runtime = ContactGatedRuntime((USER,))
        self.addCleanup(self.runtime.release_all)
        original = self.service.thread(ACCOUNT, USER, AGENT)["thread"]
        first = self.service.start(ACCOUNT, USER, AGENT, original["id"], "synthetic accepted question", "accepted-before-new")
        self.assertTrue(self.runtime.entered[USER].wait(2))
        current = self.service.new_thread(ACCOUNT, USER, AGENT)["thread"]
        retried = self.service.start(ACCOUNT, USER, AGENT, original["id"], "synthetic accepted question", "accepted-before-new")
        self.assertEqual(retried["run"]["id"], first["run"]["id"])
        self.assertEqual(retried["run"]["state"], "running")
        self.assertEqual(len(self.runtime.respond_calls), 1)
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, original["id"], "another question", "still-busy")
        self.assertEqual(caught.exception.code, "run-active")
        self.runtime.release[USER].set()
        self.assertEqual(self.wait_run(first["run"]["id"])["run"]["state"], "done")
        self.assertEqual(self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"], current["id"])

    def test_request_id_is_idempotent_and_one_run_per_thread(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        first = self.service.start(ACCOUNT, USER, AGENT, None, "重复请求", "req-same")
        again = self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "重复请求", "req-same")
        self.assertEqual(first["run"]["id"], again["run"]["id"])
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "另一个", "req-other")
        self.assertEqual(caught.exception.code, "run-active")
        self.service.stop(ACCOUNT, USER, first["run"]["id"])
        self.assertEqual(len([message for message in self.messages()
                              if message["role"] == "user"]), 1)

    def test_disabled_agent_cannot_run(self):
        self.wait_context_ready()
        self.service.save_template({"action": "enable", "id": AGENT, "enabled": False})
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, None, "你好", "req-disabled")
        self.assertEqual(caught.exception.code, "agent-disabled")

    def test_scope_validation_rejects_other_accounts_and_users(self):
        with self.assertRaises(AccountChangedError):
            self.service.start("someone-else", USER, AGENT, None, "你好", "req-scope")
        with self.assertRaises(AdvisorError) as caught:
            self.service.events(ACCOUNT, "other-user", "run:missing", 0)
        self.assertEqual(caught.exception.code, "run-unknown")
        with self.assertRaises(AdvisorError) as caught:
            self.service.stop(ACCOUNT, USER, "run:missing")
        self.assertEqual(caught.exception.code, "run-unknown")

    def test_message_size_limit_uses_the_contract_boundary(self):
        self.wait_context_ready()
        previous = self.service.stores.account(ACCOUNT).latest_thread(USER, AGENT)
        with self.assertRaises(AdvisorError):
            self.service.start(ACCOUNT, USER, AGENT, None, "x" * 16001, "req-big")
        self.assertEqual(self.service.stores.account(ACCOUNT).latest_thread(USER, AGENT), previous)
        self.assertFalse(self.runtime.respond_calls)


class ConfigurationTests(ServiceTestCase):
    def test_missing_api_configuration_returns_a_clear_error(self):
        self.model_store.mode = "local"
        self.assertEqual(self.service.catalog()["engine"], {"state": "missing"})
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, None, "你好", "req-local")
        self.assertEqual(caught.exception.code, "api-not-configured")
        self.assertIsNone(self.service.stores.account(ACCOUNT).latest_thread(USER, AGENT))
        self.model_store.mode = "api"
        self.runtime.respond = lambda *_: (_ for _ in ()).throw(AdvisorError("api-not-configured", "模型密钥不可用"))
        started = self.service.start(ACCOUNT, USER, AGENT, None, "你好", "req-nokey")
        finished = self.wait_run(started["run"]["id"])
        self.assertEqual(finished["run"]["state"], "error")
        self.assertIn("密钥不可用", finished["run"]["error"])
        self.assertFalse(self.runtime.respond_calls)

    def test_missing_runtime_engine_is_a_synchronous_error(self):
        service = self.build_service(runtime_factory=None)
        self.addCleanup(service.shutdown)
        with self.assertRaises(AdvisorError) as caught:
            service.start(ACCOUNT, USER, AGENT, None, "你好", "req-engine")
        self.assertEqual(caught.exception.code, "engine-unavailable")

    def test_restart_restores_the_thread_projection(self):
        self.wait_context_ready()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "持久化问题", "req-persist")
        self.wait_run(result["run"]["id"])
        self.service.shutdown()
        restarted_runtime = FakeRuntime(text="重启后的回复。")
        restarted = self.build_service(runtime=restarted_runtime)
        self.addCleanup(restarted.shutdown)
        payload = restarted.thread(ACCOUNT, USER, AGENT)
        self.assertEqual(payload["run"]["state"], "done")
        messages = payload["thread"]["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["text"], "持久化问题")
        second = restarted.start(ACCOUNT, USER, AGENT, payload["thread"]["id"], "重启后继续",
                                 "req-restart")
        self.wait_run(second["run"]["id"], service=restarted)
        self.assertEqual(restarted_runtime.respond_calls[0][1]["runtimeSessionId"], "sess-1")


class AccountLifecycleTests(ServiceTestCase):
    def test_pause_and_resume_keep_the_projection(self):
        self.wait_context_ready()
        self.service.pause_for_account_clear(ACCOUNT)
        with self.assertRaises(AdvisorError) as caught:
            self.service.thread(ACCOUNT, USER, AGENT)
        self.assertEqual(caught.exception.code, "account-paused")
        self.service.resume_after_failed_account_clear()
        payload = self.service.thread(ACCOUNT, USER, AGENT)
        self.assertNotIn("readCount", payload["context"])
        self.assertEqual(self.context()["readCount"], 300)

    def test_clear_account_drains_the_run_then_removes_owned_data(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        started = self.service.start(ACCOUNT, USER, AGENT, None, "清理中", "req-clear")
        run = self.service._runs[started["run"]["id"]]
        self.assertTrue(wait_until(self.runtime.paused.is_set))
        result = self.service.clear_account(ACCOUNT)
        self.assertTrue(result["deleted"])
        self.assertFalse(run.worker.is_alive())
        self.assertEqual(run.state, "stopped")
        digest = __import__("hashlib").sha256(ACCOUNT.encode()).hexdigest()
        self.assertFalse((self.root / "accounts" / digest).exists())
        self.assertEqual(self.runtime.account_shutdowns, [ACCOUNT])
        # A cleared service cannot recreate data through polling or late callbacks.
        self.service.resume_after_failed_account_clear()
        with self.assertRaises(AdvisorError):
            self.service.prepare_context(ACCOUNT, USER)
        with self.assertRaises(AdvisorError):
            self.service.events(ACCOUNT, USER, started["run"]["id"], 0)
        self.assertFalse((self.root / "accounts" / digest).exists())

    def test_resume_after_shutdown_is_rejected(self):
        self.service.shutdown()
        with self.assertRaises(RuntimeError):
            self.service.resume_after_failed_account_clear()


class SendPerformanceTests(ServiceTestCase):
    def test_deleting_active_builtin_stops_only_its_run_and_terminal_projection_remains_readable(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        started = self.service.start(ACCOUNT, USER, AGENT, None, "待删除的助手", "req-delete-active")
        self.assertTrue(self.runtime.paused.wait(2))
        run = self.service._runs[started["run"]["id"]]
        catalog = self.service.save_template({"action": "delete", "id": AGENT})
        self.assertNotIn(AGENT, [item["id"] for item in catalog["agents"]])
        self.assertEqual(run.state, "stopped")
        payload = self.service.events(ACCOUNT, USER, run.id, 0)
        self.assertEqual(payload["run"]["state"], "stopped")
        self.assertEqual(payload["thread"]["welcome"], "")
        run.worker.join(2)
        self.assertFalse(run.worker.is_alive())
        self.assertEqual(self.service.stores.account(ACCOUNT).run(run.id)["state"], "stopped")
        self.assertEqual(len(self.service.stores.config.agents()), 2)

    def running_fixture(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        result = self.service.start(ACCOUNT, USER, AGENT, None, "合成性能问题", "req-poll-performance")
        self.assertTrue(wait_until(self.runtime.paused.is_set))
        run = self.service._runs[result["run"]["id"]]
        return run

    def test_active_event_polls_reuse_the_recent_fresh_scope_check(self):
        run = self.running_fixture()
        calls = []

        original_scope = self.source.scope
        def slow_verified():
            calls.append(True)
            time.sleep(0.06)
            return original_scope()

        self.source.scope = slow_verified
        for _ in range(10):
            self.assertEqual(self.service.events(ACCOUNT, USER, run.id)["run"]["id"], run.id)
        self.assertEqual(calls, [])
        run.last_scope_check = 0
        self.service.events(ACCOUNT, USER, run.id)
        self.assertEqual(calls, [True])
        for _ in range(10):
            self.service.events(ACCOUNT, USER, run.id)
        self.assertEqual(calls, [True])

    def test_reading_a_discussion_reuses_the_recent_source_check(self):
        self.wait_context_ready()
        fresh = []
        original = self.source.history_status
        def counted(account, user, fresh_check=False, **kwargs):
            fresh.append(kwargs.get("fresh", fresh_check))
            return original(account, user)
        self.source.history_status = counted
        for _ in range(3):
            self.assertTrue(self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"])
        # Opening a discussion or polling an operation must not verify WeChat identity afresh under the service lock.
        self.assertNotIn(True, fresh)

    def test_due_poll_detects_account_switch_and_refuses_cross_scope_requests(self):
        run = self.running_fixture()
        with self.assertRaises(AdvisorError):
            self.service.events("other-account", USER, run.id)
        with self.assertRaises(AdvisorError):
            self.service.events(ACCOUNT, "other-user", run.id)
        self.source.account = "other-account"
        run.last_scope_check = 0
        with self.assertRaises((AdvisorError, AccountChangedError)):
            self.service.events(ACCOUNT, USER, run.id)
        self.assertEqual(run.state, "stopped")
        self.assertTrue(run.cancel.is_set())
        self.assertEqual([row["role"] for row in run.database.thread_messages(run.thread_id)], ["user"])

    def test_slow_due_poll_does_not_hold_the_service_condition(self):
        run = self.running_fixture()
        entered, release = threading.Event(), threading.Event()
        errors = []

        original_scope = self.source.scope
        def blocked_verified():
            entered.set()
            release.wait(3)
            return original_scope()

        self.source.scope = blocked_verified
        run.last_scope_check = 0

        def poll():
            try:
                self.service.events(ACCOUNT, USER, run.id)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=poll)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            acquired = self.service.condition.acquire(timeout=0.1)
            self.assertTrue(acquired, "fresh account discovery held the service condition")
            if acquired:
                self.service.condition.release()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_terminal_event_recovery_keeps_a_fresh_identity_check(self):
        self.wait_context_ready()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "结束后读取", "req-terminal-scope")
        self.wait_run(result["run"]["id"])
        before = self.source.identity_calls
        self.service.events(ACCOUNT, USER, result["run"]["id"])
        self.assertGreater(self.source.identity_calls, before)
        self.source.account = "other-account"
        with self.assertRaises(AccountChangedError):
            self.service.events(ACCOUNT, USER, result["run"]["id"])

    def test_run_finishing_during_a_due_check_returns_the_terminal_projection(self):
        run = self.running_fixture()
        run.last_scope_check = 0
        original = self.service._check_run

        def complete_during_check(target, thorough=True):
            if target is run and thorough:
                with self.service.condition:
                    target.state = "done"
                raise AdvisorError("stopping", "已停止")
            return original(target, thorough)

        self.service._check_run = complete_during_check
        result = self.service.events(ACCOUNT, USER, run.id)
        self.assertEqual(result["run"]["state"], "done")
        self.assertIn("thread", result)
        run.cancel.set()

    def block_highwater(self):
        entered, release = threading.Event(), threading.Event()
        original = self.source.history_prepare
        self.source.lock = threading.RLock()

        def highwater(account, user):
            with self.source.lock:
                entered.set()
                release.wait(4)
                return original(account, user)

        self.source.history_prepare = highwater
        self.addCleanup(release.set)
        return entered, release

    def test_send_ack_and_cancel_do_not_wait_for_locked_history_discovery(self):
        entered, release = self.block_highwater()
        started_at = time.monotonic()
        started = self.service.start(ACCOUNT, USER, AGENT, None, "立即显示的问题", "req-fast")
        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertTrue(entered.wait(2))
        self.assertEqual(started["run"]["phase"], "preparing")
        self.assertEqual(started["run"]["requestId"], "req-fast")
        saved = self.service.stores.account(ACCOUNT).thread_messages(started["threadId"])
        self.assertEqual(saved[0]["id"], started["run"]["messageId"])
        stopped_at = time.monotonic()
        stopped = self.service.stop(ACCOUNT, USER, started["run"]["id"])
        self.assertLess(time.monotonic() - stopped_at, 0.5)
        self.assertEqual(stopped["run"]["state"], "stopped")
        release.set()
        self.service._runs[started["run"]["id"]].worker.join(2)
        self.assertFalse(self.runtime.respond_calls)

    def test_model_change_while_preparing_returns_the_stopped_run_to_its_poll(self):
        entered, release = self.block_highwater()
        started = self.service.start(ACCOUNT, USER, AGENT, None, "准备中更换模型", "req-preparing-stale")
        self.assertTrue(entered.wait(2))
        run = self.service._runs[started["run"]["id"]]
        self.assertIsNone(run.source_scope)
        self.model_store.context_tokens = 16384
        run.last_scope_check = 0
        polled = self.service.events(ACCOUNT, USER, run.id)
        self.assertEqual(polled["run"]["state"], "stopped")
        release.set()
        run.worker.join(2)
        self.assertFalse(self.runtime.respond_calls)

    def test_source_change_during_preparation_never_reaches_model(self):
        entered, release = self.block_highwater()
        started = self.service.start(ACCOUNT, USER, AGENT, None, "旧账号的问题", "req-source-switch")
        self.assertTrue(entered.wait(2))
        self.source.account = "different-account"
        release.set()
        run = self.service._runs[started["run"]["id"]]
        run.worker.join(2)
        self.assertFalse(run.worker.is_alive())
        self.assertEqual(run.state, "stopped")
        self.assertFalse(self.runtime.respond_calls)
        self.assertFalse(self.runtime.compact_calls)

    def test_idempotent_ack_returns_same_saved_message_identity(self):
        started = self.service.start(ACCOUNT, USER, AGENT, None, "幂等问题", "req-same")
        self.wait_run(started["run"]["id"])
        repeated = self.service.start(ACCOUNT, USER, AGENT, started["threadId"], "幂等问题", "req-same")
        self.assertEqual(repeated["run"]["id"], started["run"]["id"])
        self.assertEqual(repeated["run"]["messageId"], started["run"]["messageId"])
        self.assertEqual(repeated["run"]["requestId"], "req-same")
        self.assertEqual(len([item for item in self.messages() if item["role"] == "user"]), 1)

    def test_token_burst_does_not_verify_wechat_source_for_every_delta(self):
        self.wait_context_ready()
        self.runtime.pause_after_first_chunk = True
        started = self.service.start(ACCOUNT, USER, AGENT, None, "流式问题", "req-burst")
        self.assertTrue(self.runtime.paused.wait(2))
        run = self.service._runs[started["run"]["id"]]
        before = self.source.identity_calls
        for _ in range(200):
            self.service._on_event(run, {"type": "text", "text": "x"})
        self.assertLessEqual(self.source.identity_calls - before, 4)
        self.assertEqual(run.phase, "answering")
        self.service.stop(ACCOUNT, USER, run.id)

    def test_waiting_for_core_import_does_not_verify_wechat_source_on_every_poll(self):
        # A fresh status or scope makes Core run a full WeChat account discovery.
        original_status, original_scope = self.source.history_status, self.source.scope
        importing_until, fresh, scopes = time.monotonic() + 0.6, [], []
        def importing(account, user, fresh_check=False, **kwargs):
            fresh.append(kwargs.get("fresh", fresh_check))
            status = original_status(account, user)
            return {**status, "state": "reading"} if time.monotonic() < importing_until else status
        def scope():
            scopes.append(time.monotonic())
            return original_scope()
        self.source.history_status, self.source.scope = importing, scope
        started = self.service.start(ACCOUNT, USER, AGENT, None, "导入中的问题", "req-importing")
        self.assertEqual(self.wait_run(started["run"]["id"])["run"]["state"], "done")
        self.assertGreater(len(fresh), 5)
        self.assertLessEqual(fresh.count(True), 2)
        self.assertLessEqual(len([stamp for stamp in scopes if stamp < importing_until]), 4)


if __name__ == "__main__":
    unittest.main()
