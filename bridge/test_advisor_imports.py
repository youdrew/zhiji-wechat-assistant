"""Picker grants, previews and atomic assistant import over synthetic packages."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from advisor_contracts import AdvisorError
from advisor_imports import PackagePreviews, issue_local_grant
from advisor_service import AdvisorService
from test_advisor_support import FakeSource, FakeRuntime, FakeModelStore


class ImportFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource()
        self.runtime = FakeRuntime()
        self.service = AdvisorService(self.source, FakeModelStore(context_tokens=8192), self.root,
                                      runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.folder = self.root / "skill-fixture"
        (self.folder / "references").mkdir(parents=True)
        (self.folder / "SKILL.md").write_text("---\nname: Reply assistant\ndescription: Reply method\n---\nUse [reply](references/reply.md).", encoding="utf-8")
        (self.folder / "references/reply.md").write_text("# 回复\n怎么回复：区分原文和推测。", encoding="utf-8")
        (self.folder / "LICENSE").write_text("Synthetic MIT fixture", encoding="utf-8")

    def preview(self):
        grant = issue_local_grant(self.root, self.folder)
        return self.service.preview_assistant_import({"kind": "local", "token": grant["token"]})["preview"]

    def test_preview_does_not_create_and_commit_retry_creates_only_once(self):
        before = len(self.service.catalog()["agents"])
        preview = self.preview()
        self.assertEqual(len(self.service.catalog()["agents"]), before)
        self.assertFalse(self.service.stores.config.path.exists())
        result = self.service.commit_assistant_import(preview["id"], "req:import-one", {})
        repeated = self.service.commit_assistant_import(preview["id"], "req:import-one", {})
        self.assertEqual(result["importedAgentId"], repeated["importedAgentId"])
        self.assertEqual(len(result["agents"]), before + 1)
        skill = next(item for item in result["skills"] if "packageSummary" in item)
        self.assertNotIn("package", skill)
        self.assertFalse(self.runtime.respond_calls)

    def test_http_style_local_path_is_not_a_file_picker_grant(self):
        with self.assertRaises(AdvisorError):
            self.service.preview_assistant_import({"kind": "local", "path": str(self.folder)})

    def test_cancelled_and_expired_previews_cannot_commit(self):
        preview = self.preview()
        self.service.cancel_assistant_import(preview["id"])
        with self.assertRaises(AdvisorError):
            self.service.commit_assistant_import(preview["id"], "req:cancelled", {})
        preview = self.preview()
        with patch("advisor_imports.time.monotonic", return_value=10**12):
            with self.assertRaises(AdvisorError):
                self.service.commit_assistant_import(preview["id"], "req:expired", {})

    def test_script_requirement_needs_explicit_readonly_acceptance_and_never_executes(self):
        with (self.folder / "SKILL.md").open("a", encoding="utf-8") as target:
            target.write("\nUse python scripts/memory.py to save memory.")
        (self.folder / "scripts").mkdir()
        (self.folder / "scripts/memory.py").write_text("raise RuntimeError('never execute')", encoding="utf-8")
        preview = self.preview()
        self.assertEqual(preview["compatibility"]["state"], "partial")
        with self.assertRaises(AdvisorError):
            self.service.commit_assistant_import(preview["id"], "req:partial", {})
        result = self.service.commit_assistant_import(preview["id"], "req:partial", {}, True)
        self.assertTrue(result["created"])
        self.assertFalse(self.runtime.respond_calls)

    def test_runtime_only_injects_budgeted_relevant_reference_text(self):
        preview = self.preview()
        result = self.service.commit_assistant_import(preview["id"], "req:refs", {})
        agent = self.service.stores.config.agent(result["importedAgentId"])
        selected = self.service._runtime_skills(self.service._skill_payloads(agent), "怎么回复", 1000)
        self.assertIn("区分原文和推测", selected[0]["content"])
        self.assertIn("宿主适配", selected[0]["content"])
        self.assertLessEqual(len(selected[0]["content"]), 24000)
        limited = self.service._runtime_skills(self.service._skill_payloads(agent), "怎么回复", 0)
        self.assertNotIn("<skill-reference", limited[0]["content"])


if __name__ == "__main__":
    unittest.main()
