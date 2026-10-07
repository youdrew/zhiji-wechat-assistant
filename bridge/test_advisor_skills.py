"""Skill mutation tests with only temporary configuration and synthetic runtimes."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import advisor_http
from advisor_contracts import AdvisorError
from advisor_store import AdvisorStoreRoot
from advisor_service import AdvisorService
from test_advisor_support import FakeModelStore, FakeRuntime, FakeSource, wait_until


class SkillCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stores = AdvisorStoreRoot(self.root)
        self.addCleanup(self.stores.close)
        self.config = self.stores.config

    def test_duplicate_then_rename_import_recovers_without_config_or_lock_damage(self):
        self.config.import_skill("技能一", "说明", "合成内容")
        before = self.config.path.read_bytes()
        with self.assertRaises(AdvisorError) as caught:
            self.config.import_skill("技能一", "新说明", "另一个合成内容")
        self.assertEqual(caught.exception.code, "skill-name-taken")
        self.assertEqual(self.config.path.read_bytes(), before)
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(self.config.import_skill("技能二", "", "改名内容")))
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "duplicate import left the configuration locked")
        self.assertEqual(outcomes[0]["name"], "技能二")
        self.assertTrue(self.config.agents())
        self.assertTrue(self.config.skills())

    def test_delete_skill_atomically_clears_all_agent_references(self):
        skill = self.config.import_skill("自定义", "", "合成技能")
        first = self.config.save_agent({"name": "一", "description": "", "prompt": "", "skillIds": [skill["id"]]})
        second = self.config.save_agent({"name": "二", "description": "", "prompt": "",
                                          "skillIds": ["builtin-skill:advisor", skill["id"]]})
        result = self.config.delete_skill(skill["id"])
        self.assertEqual(set(result["affectedAgentIds"]), {first["id"], second["id"]})
        self.assertNotIn(skill["id"], [row["id"] for row in self.config.skills()])
        for original in (first, second):
            current = self.config.agent(original["id"])
            self.assertNotIn(skill["id"], current["skillIds"])
            self.assertEqual(current["revision"], original["revision"] + 1)
        restored = AdvisorStoreRoot(self.root).config
        self.assertEqual(restored.agents(), self.config.agents())
        self.assertEqual(restored.skills(), self.config.skills())

    def test_builtin_skill_delete_stays_deleted_after_restart(self):
        result = self.config.delete_skill("builtin-skill:advisor")
        self.assertIn("builtin:advisor", result["affectedAgentIds"])
        restored = AdvisorStoreRoot(self.root).config
        self.assertNotIn("builtin-skill:advisor", [row["id"] for row in restored.skills()])
        self.assertEqual(restored.agent("builtin:advisor")["skillIds"], [])
        self.assertTrue(restored.agent("builtin:advisor")["enabled"])

    def test_failed_atomic_write_keeps_skill_and_all_references(self):
        skill = self.config.import_skill("删除失败", "", "合成内容")
        agent = self.config.save_agent({"name": "军师", "description": "", "prompt": "", "skillIds": [skill["id"]]})
        before = self.config.path.read_bytes()
        with patch("advisor_store.os.replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.config.delete_skill(skill["id"])
        self.assertEqual(self.config.path.read_bytes(), before)
        self.assertEqual(self.config.agent(agent["id"]), agent)
        self.assertIn(skill["id"], [row["id"] for row in self.config.skills()])
        self.assertEqual(list(self.config.directory.glob("*.tmp")), [])

    def test_unknown_skill_failure_leaves_catalog_usable(self):
        before = self.config.agents(), self.config.skills()
        with self.assertRaises(AdvisorError):
            self.config.delete_skill("skill:unknown")
        self.assertEqual((self.config.agents(), self.config.skills()), before)
        self.assertIsNotNone(self.config.import_skill("后续仍可导入", "", "合成内容"))


class SkillServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = FakeSource()
        self.source.seed_pages("user-a", 2)
        self.runtime = FakeRuntime()
        self.service = AdvisorService(self.source, FakeModelStore(context_tokens=32768), temporary.name,
                                      runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)

    def test_http_duplicate_then_import_again_and_delete_returns_valid_catalog(self):
        first = advisor_http.post(self.service, "/api/advisor/skills", {"name": "test", "description": "", "content": "synthetic"})
        with self.assertRaises(AdvisorError) as caught:
            advisor_http.post(self.service, "/api/advisor/skills", {"name": "test", "description": "", "content": "synthetic2"})
        self.assertEqual(caught.exception.code, "skill-name-taken")
        next_catalog = advisor_http.post(self.service, "/api/advisor/skills", {"action": "import", "name": "renamed", "description": "", "content": "synthetic2"})
        self.assertEqual(len(next_catalog["skills"]), len(first["skills"]) + 1)
        identifier = next(row["id"] for row in first["skills"] if row["name"] == "test")
        deleted = advisor_http.post(self.service, "/api/advisor/skills", {"action": "delete", "id": identifier})
        self.assertNotIn(identifier, [row["id"] for row in deleted["skills"]])
        self.assertTrue(deleted["agents"])
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_delete_skill_stops_affected_run_and_blocks_late_reply(self):
        entered, release = threading.Event(), threading.Event()
        def respond(config, request, on_event, cancel):
            entered.set()
            release.wait(3)
            on_event({"type": "text", "text": "late synthetic reply"})
            return {"text": "late synthetic reply", "runtimeSessionId": "synthetic-session"}
        self.runtime.respond = respond
        run_payload = self.service.start("synthetic-account-1", "user-a", "builtin:advisor", None,
                                         "synthetic request", "delete-in-run")
        self.assertTrue(entered.wait(2))
        run = self.service._runs[run_payload["run"]["id"]]
        catalog = self.service.import_skill({"action": "delete", "id": "builtin-skill:advisor"})
        self.assertEqual(run.state, "stopped")
        self.assertNotIn("builtin-skill:advisor", next(agent["skillIds"] for agent in catalog["agents"] if agent["id"] == "builtin:advisor"))
        before = run.database.thread_messages(run.thread_id)
        release.set()
        run.worker.join(2)
        self.assertEqual(run.database.thread_messages(run.thread_id), before)

    def test_delete_unselected_skill_does_not_stop_unaffected_run(self):
        catalog = self.service.import_skill({"name": "unused", "description": "", "content": "synthetic"})
        identifier = next(row["id"] for row in catalog["skills"] if row["name"] == "unused")
        self.runtime.pause_after_first_chunk = True
        pending = self.service.start("synthetic-account-1", "user-a", "builtin:advisor", None,
                                     "synthetic request", "unaffected-run")
        self.assertTrue(wait_until(self.runtime.paused.is_set))
        run = self.service._runs[pending["run"]["id"]]
        self.service.import_skill({"action": "delete", "id": identifier})
        self.assertEqual(run.state, "running")
        self.service.stop(run.account, run.user, run.id)


if __name__ == "__main__":
    unittest.main()
