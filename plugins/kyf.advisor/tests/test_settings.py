"""Synthetic discussion settings snapshots; all history and model calls use injected fakes."""
import threading
from unittest.mock import patch

from contracts import AdvisorError
from kyf_plugin import CoreError
from support import wait_until
from test_service import ACCOUNT, USER, AGENT, ServiceTestCase


class SettingsTests(ServiceTestCase):
    def test_turn_freezes_settings_and_following_question_uses_new_values(self):
        self.source.snapshot["data"]["observations"] = [{
            "id": "synthetic-observation", "judgement": "合成观察标记", "note": "仅凭感觉"}]
        self.source.saved_settings["values"]["includeObservations"] = False
        self.runtime.finish_gate = threading.Event()
        self.addCleanup(self.runtime.finish_gate.set)
        first = self.service.start(ACCOUNT, USER, AGENT, None, "合成问题")
        self.assertTrue(wait_until(lambda: self.runtime.respond_calls))
        first_run = self.service._runs[first["run"]["id"]]
        self.assertNotIn("合成观察标记", self.runtime.respond_calls[0][1]["contextFileText"])
        self.source.saved_settings["values"]["includeObservations"] = True
        self.source.saved_settings["revision"] = 1
        self.assertFalse(first_run.settings["values"]["includeObservations"])
        self.runtime.finish_gate.set()
        self.assertEqual(self.wait_run(first_run.id)["run"]["state"], "done")
        self.runtime.session = "sess-2"
        second = self.service.start(ACCOUNT, USER, AGENT, None, "新的合成问题")
        self.assertEqual(self.wait_run(second["run"]["id"])["run"]["state"], "done")
        self.assertIn("合成观察标记", self.runtime.respond_calls[-1][1]["contextFileText"])

    def test_external_search_source_reaches_only_answers_asked_while_it_is_configured(self):
        first = self.service.start(ACCOUNT, USER, AGENT, None, "合成问题")
        self.assertEqual(self.wait_run(first["run"]["id"])["run"]["state"], "done")
        self.assertNotIn("webSearch", self.runtime.respond_calls[-1][1])
        self.source.model_settings["values"].update(
            searchSource="searxng", searxngUrl=" http://127.0.0.1:8888/ ", searchOpenPages=False)
        self.source.model_settings["revision"] = 1
        self.runtime.session = "sess-2"
        second = self.service.start(ACCOUNT, USER, AGENT, None, "需要联网的合成问题")
        self.assertEqual(self.wait_run(second["run"]["id"])["run"]["state"], "done")
        self.assertEqual(self.runtime.respond_calls[-1][1]["webSearch"],
                         {"provider": "searxng", "url": "http://127.0.0.1:8888/", "fetchPages": False})

    def test_unusable_search_source_address_fails_before_the_question_starts(self):
        for url in ("", "   ", "ftp://127.0.0.1", "http://user:secret@127.0.0.1:8080", "127.0.0.1:8080",
                    "http://127.0.0.1:99999", "http://127.0.0.1:8080/#top", "http://"):
            self.source.model_settings["values"].update(searchSource="searxng", searxngUrl=url)
            with self.assertRaises(AdvisorError, msg=url) as raised:
                self.service.start(ACCOUNT, USER, AGENT, None, "合成问题")
            self.assertEqual(raised.exception.code, "invalid-settings")
            self.assertIn("外部搜索源", raised.exception.message)
            self.assertIn("模型设置", raised.exception.message)
        self.source.model_settings["values"]["searchSource"] = "bing"
        with self.assertRaises(AdvisorError) as raised:
            self.service.start(ACCOUNT, USER, AGENT, None, "合成问题")
        self.assertEqual(raised.exception.code, "invalid-settings")
        self.assertFalse(self.service._runs)
        self.assertEqual(self.runtime.respond_calls, [])

    def test_legacy_plugin_search_configuration_cannot_override_core_settings(self):
        self.source.saved_settings["values"].update(
            searchSource="searxng", searxngUrl="http://legacy.invalid", searchOpenPages=True)
        first = self.service.start(ACCOUNT, USER, AGENT, None, "只使用资料")
        self.assertEqual(self.wait_run(first["run"]["id"])["run"]["state"], "done")
        self.assertNotIn("webSearch", self.runtime.respond_calls[-1][1])
        self.source.saved_settings["values"].update(searchSource="none", searchOpenPages=False)
        self.source.model_settings["values"].update(searchSource="searxng", searxngUrl="http://core.invalid")
        self.runtime.session = "sess-2"
        second = self.service.start(ACCOUNT, USER, AGENT, None, "需要联网")
        self.assertEqual(self.wait_run(second["run"]["id"])["run"]["state"], "done")
        self.assertEqual(self.runtime.respond_calls[-1][1]["webSearch"],
                         {"provider": "searxng", "url": "http://core.invalid", "fetchPages": True})

    def test_queued_answer_reads_core_search_settings_when_it_starts(self):
        self.runtime.finish_gate = threading.Event()
        self.addCleanup(self.runtime.finish_gate.set)
        first = self.service.start(ACCOUNT, USER, AGENT, None, "先回答资料问题")
        self.assertTrue(wait_until(lambda: self.runtime.respond_calls))
        first_run = self.service._runs[first["run"]["id"]]
        self.service.queue_enqueue(ACCOUNT, USER, AGENT, first["threadId"], "随后联网查询", "queued-search")
        self.source.model_settings["values"].update(
            searchSource="searxng", searxngUrl="http://changed.invalid", searchOpenPages=False)
        self.source.model_settings["revision"] = 1
        self.assertEqual(first_run.model_settings["values"]["searchSource"], "none")
        self.runtime.finish_gate.set()
        self.assertEqual(self.wait_run(first_run.id)["run"]["state"], "done")
        self.assertTrue(wait_until(lambda: len(self.runtime.respond_calls) == 2))
        self.assertNotIn("webSearch", self.runtime.respond_calls[0][1])
        self.assertEqual(self.runtime.respond_calls[1][1]["webSearch"],
                         {"provider": "searxng", "url": "http://changed.invalid", "fetchPages": False})

    def test_unavailable_core_model_settings_do_not_fall_back_to_plugin_search(self):
        original = self.source.get
        def get(route, **query):
            if route == "models/settings":
                raise CoreError(503, "unavailable", "synthetic unavailable")
            return original(route, **query)
        with patch.object(self.source, "get", side_effect=get), self.assertRaises(AdvisorError) as raised:
            self.service.start(ACCOUNT, USER, AGENT, None, "合成问题")
        self.assertEqual(raised.exception.code, "settings-unavailable")
        self.assertIn("模型设置", raised.exception.message)
        self.assertFalse(self.service._runs)
        self.assertEqual(self.runtime.respond_calls, [])

    def test_invalid_settings_fail_before_creating_discussion_or_model_call(self):
        self.source.saved_settings["values"]["includeMemories"] = "false"
        with self.assertRaises(AdvisorError) as raised:
            self.service.start(ACCOUNT, USER, AGENT, None, "合成问题")
        self.assertEqual(raised.exception.code, "invalid-settings")
        self.assertFalse(self.service._runs)
        self.assertEqual(self.runtime.respond_calls, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
