"""Inert package compatibility and hostile-source tests with synthetic files."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import advisor_packages as packages
from advisor_contracts import AdvisorError


ENTRY = "---\nname: synthetic\ndescription: 合成技能\n---\n# 合成助手\n| 回复话术 | `references/回复.md` |\n| 冲突修复 | `references/冲突.md` |\n"
FILES = {
    "SKILL.md": ENTRY,
    "agents/openai.yaml": "interface:\n  display_name: 合成助手\n  short_description: 合成说明\n  default_prompt: 使用合成技能\n",
    "references/回复.md": "# 回复话术\n根据原话生成简短回复，说明下一步。\n",
    "references/冲突.md": "# 冲突修复\n具体观察，承认影响，落实补救。\n",
    "LICENSE": "MIT License\nSynthetic fixture only.\n",
}


def fixture(files=None):
    return packages.parse_files(files or FILES, {"kind": "local", "label": "synthetic"})


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in files.items():
            zipped.writestr(name, content)
    return output.getvalue()


class PackageTests(unittest.TestCase):
    def assert_invalid(self, call, message=None):
        with self.assertRaises(AdvisorError) as caught:
            call()
        self.assertEqual(caught.exception.code, "invalid-request")
        if message:
            self.assertIn(message, caught.exception.message)

    def test_metadata_raw_entry_license_and_independent_validation(self):
        document = fixture()
        self.assertEqual(document["entry"]["content"], ENTRY)
        self.assertEqual(document["entry"]["name"], "合成助手")
        self.assertEqual(document["entry"]["defaultPrompt"], "使用合成技能")
        self.assertEqual(document["licenses"][0]["content"], FILES["LICENSE"])
        self.assertEqual(document["compatibility"]["state"], "readonly")
        self.assertEqual(len(document["resources"]), 3)
        checked = packages.validate_package(document)
        checked["entry"]["name"] = "changed"
        self.assertEqual(document["entry"]["name"], "合成助手")
        self.assertEqual(fixture()["digest"], document["digest"])

    def test_directory_skill_file_and_zip_have_equal_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skill"
            root.mkdir()
            for name, content in FILES.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.encode("utf-8"))
            directory = packages.parse_package({"kind": "local", "path": str(root)})
            selected = packages.parse_package({"kind": "local", "path": str(root / "SKILL.md")})
            zipped_path = Path(temporary) / "fixture.zip"
            zipped_path.write_bytes(archive({"wrapper/" + key: value for key, value in FILES.items()}))
            zipped = packages.parse_package({"kind": "local", "path": str(zipped_path)})
        for result in (directory, selected, zipped):
            self.assertEqual(result["entry"]["content"], ENTRY)
            self.assertEqual(result["source"]["sha256"], directory["source"]["sha256"])
            self.assertNotIn("path", result["source"])

    def test_missing_reference_and_ambiguous_entry_fail_without_truncation(self):
        files = dict(FILES)
        del files["references/回复.md"]
        self.assert_invalid(lambda: fixture(files), "缺少引用")
        files = dict(FILES, **{"other/SKILL.md": "another"})
        self.assert_invalid(lambda: fixture(files), "唯一")
        files = dict(FILES, **{"references/large.md": "长" * 64001})
        self.assert_invalid(lambda: fixture(files), "超出限制")
        files = dict(FILES, **{"references/file-" + str(n) + ".md": "x" for n in range(128)})
        self.assert_invalid(lambda: fixture(files), "数量")

    def test_single_file_local_relative_references_and_encoded_traversal(self):
        document = fixture({"SKILL.md": "# Test\nSee [note](references/note.md).\n", "references/note.md": "# Note\n`more.md`\n", "references/more.md": "detail"})
        self.assertEqual(len(document["resources"]), 2)
        for target in ("../secret.md", "%2e%2e/secret.md", "/etc/file.md", "C:/file.md", "file://host/file.md"):
            self.assert_invalid(lambda target=target: fixture({"SKILL.md": "# Test\n[x](" + target + ")"}))
        document = fixture({"SKILL.md": "# Test\n[x](./references/note.md)", "references/note.md": "[licence](../LICENSE)", "LICENSE": "MIT"})
        self.assertEqual(document["licenses"][0]["content"], "MIT")

    def test_ancestor_license_and_development_example_are_retained(self):
        document = fixture({"skill/SKILL.md": "# Test", "LICENSE": "MIT", "skill/documentation/guide.md": "Example `emails.md` is not a runtime dependency."})
        self.assertEqual(document["licenses"][0]["content"], "MIT")
        self.assertEqual(len(document["resources"]), 1)

    def test_script_chatlab_and_memory_are_partial_without_execution(self):
        files = dict(FILES)
        files["references/回复.md"] += "\nRun `python3 scripts/memory_store.py status`.\n`chatlab manifest`\n长期记忆\n"
        files["scripts/memory_store.py"] = "raise RuntimeError('NEVER EXECUTE')"
        files["assets/image.png"] = b"synthetic image bytes"
        with patch("subprocess.run", side_effect=AssertionError("no execution")), patch("os.system", side_effect=AssertionError("no execution")):
            document = fixture(files)
            packages.validate_package(document)
        self.assertEqual(document["compatibility"]["state"], "partial")
        self.assertEqual(set(document["compatibility"]["disabledCapabilities"]),
                         {"script-execution", "chatlab-cli", "skill-persistent-memory", "binary-assets"})
        self.assertIn("不得声称", document["compatibility"]["hostInstructions"])
        self.assertNotIn("scripts/memory_store.py", [row["path"] for row in document["resources"]])

    def test_yaml_tags_aliases_and_oversize_metadata_are_rejected(self):
        for content in ("---\nname: !!python/object/apply:os.system [echo unsafe]\n---\nTest",
                        "---\nname: &name synthetic\ndescription: *name\n---\nTest",
                        "---\nname: " + "n" * 61 + "\n---\nTest",
                        "---\nname: test\ndescription: " + "d" * 241 + "\n---\nTest",
                        "---\nname: test\nTest"):
            self.assert_invalid(lambda content=content: fixture({"SKILL.md": content}))

    def test_tampered_resource_digest_or_structure_rejected(self):
        document = fixture()
        document["resources"][0]["content"] += "tampered"
        self.assert_invalid(lambda: packages.validate_package(document), "摘要")
        for mutation in (lambda p: p.update(digest="0" * 64),
                         lambda p: p["resources"][0].update(path="../escape.md"),
                         lambda p: p["compatibility"].update(state="complete"),
                         lambda p: p["compatibility"].update(disabledCapabilities=[{}]),
                         lambda p: p["source"].update(path="C:/private")):
            document = fixture()
            mutation(document)
            self.assert_invalid(lambda: packages.validate_package(document))

    def test_private_files_are_rejected_and_public_license_retained(self):
        for private in (".env", "auth.json", "keys.json", "chat-export.json", "memory.sqlite3", "id.key", "cookies.txt", "logs/private.jsonl", "id_ed25519"):
            self.assert_invalid(lambda private=private: fixture(dict(FILES, **{private: "synthetic private"})), "私密")

    def test_invalid_unicode_binary_text_and_total_budget_fail(self):
        self.assert_invalid(lambda: fixture({"SKILL.md": "# Test\n\ud800"}), "Unicode")
        self.assert_invalid(lambda: fixture({"SKILL.md": b"\xff\xfeinvalid"}), "UTF-8")
        files = {"SKILL.md": "# Test"}
        files.update({"references/" + str(n) + ".md": "字" * 60000 for n in range(12)})
        self.assert_invalid(lambda: fixture(files), "总量")

    def test_zip_traversal_ads_symlink_duplicate_and_bomb_rejected(self):
        self.assert_invalid(lambda: packages._path("dir\\SKILL.md"))
        for path in ("../SKILL.md", "/SKILL.md", "C:/SKILL.md", "dir/../SKILL.md", "file:secret", "CON.txt"):
            self.assert_invalid(lambda path=path: packages._read_zip(archive({path: "# X"})))
        self.assert_invalid(lambda: packages._read_zip(archive({"SKILL.md": "a", "skill.md": "b"})), "重名")
        self.assert_invalid(lambda: packages._read_zip(archive({"SKILL.md": "a" * 200000})), "压缩比")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            linked = zipfile.ZipInfo("SKILL.md")
            linked.create_system = 3
            linked.external_attr = (0o120777 << 16)
            zipped.writestr(linked, "elsewhere")
        self.assert_invalid(lambda: packages._read_zip(output.getvalue()), "链接")

    def test_reparse_and_file_picker_boundaries(self):
        self.assert_invalid(lambda: packages.parse_package({"kind": "local", "path": "SKILL.md"}), "选择器")
        self.assert_invalid(lambda: packages.parse_package({"kind": "local", "path": "\\\\server\\share\\SKILL.md"}), "共享")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text("# Test", encoding="utf-8")
            with patch.object(packages, "_reparse", return_value=True):
                self.assert_invalid(lambda: packages.parse_package({"kind": "local", "path": str(root)}), "链接")

    def test_references_rank_complete_documents_and_report_budget_omissions(self):
        document = fixture()
        result = packages.select_references(document, "这句怎么回", 2000)
        self.assertEqual(result["resources"][0]["path"], "references/回复.md")
        self.assertEqual(result["resources"][0]["content"], FILES["references/回复.md"])
        self.assertTrue(result["resources"][0]["matchedTerms"])
        none = packages.select_references(document, "如何回复", 1)
        self.assertEqual(none["resources"], [])
        self.assertIn("references/回复.md", none["omittedPaths"])
        self.assertTrue(none["warnings"])
        self.assertEqual(packages.select_references(document, "xyz-unrelated", 1000)["resources"], [])


class GitHubTests(unittest.TestCase):
    assert_invalid = PackageTests.assert_invalid
    def test_repo_tree_and_blob_pin_commit_and_respect_scope(self):
        sha = "a" * 40
        archive_bytes = archive({"repo-" + sha + "/skill/" + path: content for path, content in FILES.items()})
        calls = []
        def fetch(url, maximum):
            calls.append(url)
            if url.endswith("/repos/owner/repo"):
                return json.dumps({"default_branch": "main"}).encode()
            if "/commits?" in url:
                return json.dumps([{"sha": sha}]).encode()
            if url == "https://codeload.github.com/owner/repo/zip/" + sha:
                return archive_bytes
            raise AssertionError(url)
        for url in ("https://github.com/owner/repo", "https://github.com/owner/repo/tree/main/skill",
                    "https://github.com/owner/repo/blob/main/skill/SKILL.md"):
            with patch.object(packages, "_fetch", side_effect=fetch):
                document = packages.parse_package({"kind": "github", "url": url})
            self.assertEqual(document["source"]["commit"], sha)
            self.assertEqual(document["entry"]["name"], "合成助手")
            self.assertIn("/tree/" + sha, document["source"]["url"])
        self.assertTrue(any("codeload.github.com" in url for url in calls))

    def test_slash_branch_resolved_without_unpinned_download(self):
        sha = "b" * 40
        def fetch(url, maximum):
            if "sha=feature&" in url:
                raise packages.urllib.error.HTTPError(url, 404, "missing", {}, None)
            if "sha=feature%2Ftopic&" in url:
                return json.dumps([{"sha": sha}]).encode()
            if url.endswith("/zip/" + sha):
                return archive({"repo-" + sha + "/skill/" + path: content for path, content in FILES.items()})
            raise AssertionError(url)
        with patch.object(packages, "_fetch", side_effect=fetch):
            document = packages.parse_package({"kind": "github", "url": "https://github.com/owner/repo/tree/feature/topic/skill"})
        self.assertEqual(document["source"]["commit"], sha)

    def test_ssrf_non_skill_blob_and_query_urls_rejected_before_fetch(self):
        for url in ("http://github.com/owner/repo", "https://127.0.0.1/owner/repo", "https://github.com.evil/owner/repo",
                    "https://user:pass@github.com/owner/repo", "https://github.com:444/owner/repo",
                    "https://github.com/owner/repo?secret=x", "https://github.com/owner/repo/blob/main/private.txt"):
            with patch.object(packages, "_fetch", side_effect=AssertionError("must not fetch")):
                self.assert_invalid(lambda url=url: packages.parse_package({"kind": "github", "url": url}))
        handler = packages._Redirects()
        self.assert_invalid(lambda: handler.redirect_request(None, None, 302, "", {}, "https://localhost/private"))

    def test_remote_error_does_not_leave_files(self):
        with patch.object(packages, "_fetch", side_effect=AdvisorError("invalid-request", "offline")):
            self.assert_invalid(lambda: packages.parse_package({"kind": "github", "url": "https://github.com/owner/repo"}), "offline")

    def test_response_limit_redirect_and_timeout_are_enforced(self):
        class Response:
            def __init__(self, content=b"abcdefgh", url="https://api.github.com/repos/owner/repo", headers=None):
                self.stream = io.BytesIO(content)
                self.url = url
                self.headers = headers or {}
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                self.stream.close()
            def geturl(self):
                return self.url
            def read(self, size):
                return self.stream.read(size)
        for response in (Response(), Response(url="https://127.0.0.1/secret"), Response(headers={"Content-Length": "9"})):
            with patch("advisor_packages.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = response
                self.assert_invalid(lambda: packages._fetch("https://api.github.com/repos/owner/repo", 4))
        with patch("advisor_packages.urllib.request.build_opener") as opener, patch("advisor_packages.time.monotonic", side_effect=[0, 21]):
            opener.return_value.open.return_value = Response()
            self.assert_invalid(lambda: packages._fetch("https://api.github.com/repos/owner/repo", 100), "超时")


if __name__ == "__main__":
    unittest.main()
