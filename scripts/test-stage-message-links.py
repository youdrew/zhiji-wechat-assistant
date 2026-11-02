"""Link rendering and native open dependencies must survive portable packaging."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MessageLinkStageTests(unittest.TestCase):
    def test_link_dependency_closure_is_staged_and_native_host_is_packaged_and_verified(self):
        stage = load_script("stage-real-client")
        release = load_script("build-windows-release")
        builder = json.loads((ROOT / "electron-builder.real-client.json").read_text(encoding="utf-8"))
        mapped = {relative.as_posix(): source for source, relative, _root in stage.public_mappings(ROOT)}
        for relative in ("bridge/message_links.py", "chatui/message-links.js", "scripts/chat-link-host.cjs"):
            with self.subTest(asset=relative):
                self.assertIn(relative, mapped)
                self.assertTrue(mapped[relative].is_file())
        self.assertIn("scripts/chat-link-host.cjs", builder["files"])
        self.assertIn("scripts/chat-link-host.cjs", release.ASAR_SCRIPTS)
        self.assertIn('src="message-links.js"', (ROOT / "chatui/index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
