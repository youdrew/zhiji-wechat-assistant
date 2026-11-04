"""Every local resource a staged page loads must be staged with it.

A page script missing from the package fails as a 404 at startup, and the
chat window then never leaves its static loading state.
"""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = re.compile(r"""<(?:script|link|img)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def packaged_path(page: str, reference: str) -> str | None:
    """The staged file the bridge serves for one page reference, or None when it is not local."""
    parts = urlsplit(reference)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    if parts.path.startswith("/plugin-sdk/web/"):
        return "plugin-sdk/web/" + parts.path[len("/plugin-sdk/web/"):]
    if parts.path.startswith("/"):
        return "chatui/" + parts.path.lstrip("/")
    return (Path(page).parent / parts.path).as_posix()


class ChatUiReferenceStageTests(unittest.TestCase):
    def test_staged_pages_only_reference_staged_files(self):
        stage = load_script("stage-real-client")
        mapped = {relative.as_posix() for _source, relative, _root in stage.public_mappings(ROOT)}
        pages = sorted(path for path in mapped if path.startswith("chatui/") and path.endswith(".html"))
        self.assertIn("chatui/index.html", pages)
        for page in pages:
            text = (ROOT / page).read_text(encoding="utf-8")
            for reference in REFERENCE.findall(text):
                target = packaged_path(page, reference)
                if target is None:
                    continue
                with self.subTest(page=page, reference=reference):
                    self.assertTrue(target in mapped, f"{target} is not staged")

    def test_reference_mapping_follows_bridge_routes(self):
        self.assertEqual(packaged_path("chatui/index.html", "app.js"), "chatui/app.js")
        self.assertEqual(packaged_path("chatui/friend.html", "/friend.js?v=1"), "chatui/friend.js")
        self.assertEqual(packaged_path("chatui/friend.html", "/plugin-sdk/web/time-trend.js"),
                         "plugin-sdk/web/time-trend.js")
        self.assertIsNone(packaged_path("chatui/index.html", "https://example.com/a.js"))
        self.assertIsNone(packaged_path("chatui/index.html", "mailto:someone@example.com"))


class PrivateDocsStageTests(unittest.TestCase):
    """Root docs/ is private development documentation and never enters the client stage."""

    def test_public_mappings_contain_no_root_docs(self):
        stage = load_script("stage-real-client")
        mapped = [relative for _source, relative, _root in stage.public_mappings(ROOT)]
        self.assertEqual([], [path.as_posix() for path in mapped if path.parts[0].casefold() == "docs"])
        self.assertFalse(any(name.startswith("docs/") for name in stage.PUBLIC_FILES))
        self.assertIn(Path("README.md"), mapped)


if __name__ == "__main__":
    unittest.main()
