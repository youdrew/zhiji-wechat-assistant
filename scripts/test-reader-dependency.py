"""Synthetic reader-subset regression tests. No account or WeChat process is opened."""
from __future__ import annotations

import csv
import hashlib
import importlib.metadata as metadata
import importlib.util
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import reader_dependency as reader


class ReaderSubsetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-reader-subset-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "installed"
        self.target = self.root / "staged"
        self.metadata = "wechatauto_replica-1.2.2.6.dist-info"
        payloads = {
            "__init__.py": b"from .guia import quick_send\n",
            "db.py": b"class WeChatDB: pass\n",
            "logger.py": b"from .param import WxParam\n",
            "param.py": b"class WxParam: pass\n",
            "guia.py": b"def quick_send(): raise AssertionError('must never ship')\n",
            "media.py": b'''class MediaDownloader:
    @staticmethod
    def _find_preview_button():
        return 'UI control'
    def download_image_original(self):
        from .guia import quick_send
        return quick_send()
    def decrypt_image(self):
        return b'local image'
''',
        }
        self.hashes = {name: hashlib.sha256(payloads[name]).hexdigest() for name in reader.READER_SOURCE_HASHES}
        files = {"wechatauto/" + name: data for name, data in payloads.items()}
        files.update({self.metadata + "/METADATA": (
                          b"Metadata-Version: 2.4\nName: wechatauto-replica\nVersion: 1.2.2.6\n"
                          b"Author: Synthetic Upstream\nLicense: Apache-2.0\n\n"
                          b"# Synthetic long README description\nwxid_synthetic_reader_fixture_only\n"),
                      self.metadata + "/licenses/LICENSE": b"Synthetic upstream license\n",
                      self.metadata + "/RECORD": b"old file list includes guia.py\n",
                      "wechatauto/accounts/private.db": b"synthetic private runtime data"})
        for name, data in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.dist = SimpleNamespace(version=reader.READER_VERSION, metadata={"Name": "wechatauto-replica"},
                                    files=[Path(name) for name in files], locate_file=lambda name: self.source / name)
        patcher = mock.patch.object(reader, "READER_SOURCE_HASHES", self.hashes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy(self, source, target, _category):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def stage(self):
        return reader.stage_reader_distribution(self.dist, self.source, self.target, self.copy)

    def test_trim_preserves_local_media_and_excludes_gui_code_data_and_exports(self):
        result = self.stage()
        self.assertEqual(result["profile"], "database-media-only")
        package = self.target / "wechatauto"
        self.assertEqual({p.name for p in package.iterdir()},
                         {"__init__.py", "db.py", "media.py", "logger.py", "param.py", "KYF_READ_ONLY.md"})
        source = (package / "media.py").read_text(encoding="utf-8")
        namespace = {}
        exec(compile(source, "synthetic-media.py", "exec"), namespace)
        self.assertEqual(namespace["MediaDownloader"]().decrypt_image(), b"local image")
        self.assertFalse(hasattr(namespace["MediaDownloader"], "download_image_original"))
        self.assertFalse(hasattr(namespace["MediaDownloader"], "_find_preview_button"))
        self.assertNotIn("quick_send", (package / "__init__.py").read_text())
        self.assertEqual((self.target / self.metadata / "licenses/LICENSE").read_bytes(),
                         b"Synthetic upstream license\n")
        self.assertIn("Removed", (package / "KYF_READ_ONLY.md").read_text())
        with (self.target / self.metadata / "RECORD").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        actual = {path.relative_to(self.target).as_posix() for path in self.target.rglob("*") if path.is_file()}
        self.assertEqual({row[0] for row in rows}, actual)
        self.assertTrue(all("guia.py" not in row[0] and "private.db" not in row[0] for row in rows))
        self.assertIn("from .guia", (self.source / "wechatauto/__init__.py").read_text(),
                      "the installed dependency must remain unchanged")

    def test_unreviewed_version_or_source_change_stops_before_copy(self):
        self.dist.version = "9.9.9"
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            self.stage()
        self.dist.version = reader.READER_VERSION
        (self.source / "wechatauto/db.py").write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "differs from the reviewed"):
            self.stage()
        self.assertFalse(self.target.exists())

    def test_metadata_omits_long_description_but_preserves_queryable_identity_and_license(self):
        original = (self.source / self.metadata / "METADATA").read_bytes()
        self.stage()
        staged = (self.target / self.metadata / "METADATA").read_bytes()
        self.assertNotIn(b"wxid_synthetic_reader_fixture_only", staged)
        self.assertNotIn(b"Synthetic long README description", staged)
        self.assertEqual(staged.partition(b"\n\n")[2], b"")
        installed = list(metadata.distributions(path=[str(self.target)]))
        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0].metadata["Name"], "wechatauto-replica")
        self.assertEqual(installed[0].version, "1.2.2.6")
        self.assertEqual(installed[0].metadata["Author"], "Synthetic Upstream")
        self.assertEqual(installed[0].metadata["License"], "Apache-2.0")
        self.assertEqual((self.target / self.metadata / "licenses/LICENSE").read_bytes(),
                         b"Synthetic upstream license\n")
        self.assertEqual((self.source / self.metadata / "METADATA").read_bytes(), original)
        with (self.target / self.metadata / "RECORD").open(newline="", encoding="utf-8") as stream:
            rows = {row[0]: row for row in csv.reader(stream)}
        self.assertEqual(rows[self.metadata + "/METADATA"][2], str(len(staged)))
        self.assertIn("long description", (self.target / "wechatauto/KYF_READ_ONLY.md").read_text())

    def test_upstream_layout_change_and_remaining_gui_import_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "method layout"):
            reader.reader_media_source(b"class MediaDownloader: pass\n")
        payload = (self.source / "wechatauto/media.py").read_bytes()
        with self.assertRaisesRegex(ValueError, "still imports GUI"):
            reader.reader_media_source(b"from .guia import quick_send\n" + payload)

    def test_runtime_resolver_uses_reader_dependencies_instead_of_gui_requires_dist(self):
        spec = importlib.util.spec_from_file_location("reader_runtime_closure", Path(__file__).with_name("stage-real-runtime.py"))
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        self.dist.requires = ["winsdk", "pyautogui"]
        distributions = {"wechatauto-replica": self.dist}
        for name in reader.READER_DEPENDENCIES:
            distributions[name] = SimpleNamespace(files=[Path("module.py")], requires=[])
        with (mock.patch.object(runtime, "PYTHON_ROOT_PACKAGES", ("wechatauto-replica",)),
              mock.patch.object(runtime.metadata, "distribution", side_effect=lambda name: distributions[name])):
            chosen, omitted = runtime.python_distributions()
        self.assertEqual(len(chosen), 1 + len(reader.READER_DEPENDENCIES))
        self.assertEqual(omitted, [])


if __name__ == "__main__":
    unittest.main()
