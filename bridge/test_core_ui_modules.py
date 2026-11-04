"""The Agent-openable window module registry (B1)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_ui_modules
from core_ui_modules import FRIEND_ANALYSIS, UiError, openable_modules, resolve

ROOT = Path(__file__).resolve().parent.parent


class ModuleRegistryTests(unittest.TestCase):
    def code(self, module_id, context=None):
        with self.assertRaises(UiError) as caught:
            resolve(module_id, context)
        return caught.exception.code

    def test_contact_module_resolves_to_friend_surface(self):
        spec = resolve("contact.memory", {"accountId": "acc-1", "subjectId": "wxid_friend"})
        self.assertEqual(spec["surface"], "friend")
        self.assertEqual(spec["tab"], "memories")
        self.assertEqual(spec["plugins"], [FRIEND_ANALYSIS])
        self.assertEqual(spec["capability"], "read")
        self.assertEqual((spec["accountId"], spec["subjectId"]), ("acc-1", "wxid_friend"))
        self.assertNotIn("agentOpenable", spec)

    def test_progress_module_flags_run_status(self):
        spec = resolve("analysis.progress", {"accountId": "a", "subjectId": "b"})
        self.assertEqual(spec["tab"], "overview")
        self.assertTrue(spec["runStatus"])

    def test_conversation_module_opens_the_chat_surface_for_one_contact(self):
        spec = resolve("contact.conversation", {"accountId": "acc-1", "subjectId": "wxid_friend"})
        self.assertEqual((spec["surface"], spec["context"], spec["capability"]), ("chat", "contact", "read"))
        self.assertEqual(spec["plugins"], [])
        self.assertEqual((spec["accountId"], spec["subjectId"]), ("acc-1", "wxid_friend"))
        self.assertIn("contact.conversation", openable_modules())
        self.assertEqual(self.code("contact.conversation"), "context-invalid")
        self.assertEqual(self.code("contact.conversation", {"accountId": "a", "subjectId": "1@chatroom"}),
                         "context-invalid")
        for subject in ("", "a\nb", " x", "a" * 257):
            self.assertEqual(self.code("contact.conversation", {"accountId": "a", "subjectId": subject}),
                             "context-invalid", subject)
        for subject in ("wxid_abc123", "gh_0a1b", "abc-def.x", "123@openim", "名字"):
            self.assertEqual(resolve("contact.conversation", {"accountId": "a", "subjectId": subject})["subjectId"],
                             subject)
        self.assertEqual(resolve("contact.overview", {"accountId": "a", "subjectId": "名字"})["subjectId"], "名字")

    def test_main_modules_take_no_context(self):
        self.assertEqual(resolve("app.main")["surface"], "main")
        self.assertEqual(resolve("app.settings")["openModule"], "settings")
        self.assertEqual(self.code("app.main", {"accountId": "a"}), "context-invalid")

    def test_unknown_and_url_like_modules_are_rejected(self):
        for value in ("contact.unknown", "http://127.0.0.1/friend.html", "../friend.html", "friend.html",
                      "CONTACT.OVERVIEW", "", None, 42, "contact.overview ", "a" * 200):
            self.assertEqual(self.code(value, {"accountId": "a", "subjectId": "b"}), "module-unknown", value)

    def test_registered_but_not_openable_module_is_rejected(self):
        self.assertEqual(self.code("app.settings.models"), "module-not-openable")
        self.assertNotIn("app.settings.models", openable_modules())

    def test_contact_context_is_validated(self):
        self.assertEqual(self.code("contact.overview"), "context-invalid")
        self.assertEqual(self.code("contact.overview", {"accountId": "a"}), "context-invalid")
        self.assertEqual(self.code("contact.overview", {"accountId": "a", "subjectId": "123@chatroom"}),
                         "context-invalid")
        self.assertEqual(self.code("contact.overview", {"accountId": "a", "subjectId": "x\ny"}), "context-invalid")
        self.assertEqual(self.code("contact.overview", {"accountId": "a", "subjectId": "b", "url": "x"}),
                         "context-invalid")
        self.assertEqual(self.code("contact.overview", "acc"), "context-invalid")

    def test_contact_tabs_exist_in_the_friend_analysis_scheme(self):
        import json
        manifest = json.loads((ROOT / "plugins" / FRIEND_ANALYSIS / "plugin.json").read_text(encoding="utf-8"))
        tabs = {tab.get("id") or tab.get("host") for tab in manifest["contributes"]["schemes"][0]["tabs"]}
        for name, entry in core_ui_modules.MODULES.items():
            if entry["surface"] == "friend":
                self.assertIn(entry["tab"], tabs, name)

    def test_error_statuses(self):
        self.assertEqual(UiError("module-unknown").status, 404)
        self.assertEqual(UiError("contact-not-authorized").status, 403)


if __name__ == "__main__":
    unittest.main()
