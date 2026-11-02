"""Synthetic tests for the clean portable staging and package boundary."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = load("stage_real_client", "stage-real-client.py")
builder = load("build_portable_clean", "build-portable-clean.py")


class CleanPortableBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-clean-build-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.build = self.root / "build"
        self.client = self.build / "client"
        self.source.mkdir()
        self.build.mkdir()
        self.put(self.source / "package.json", b'{"name":"know-your-friends","version":"1.0.2-preview.2"}')
        self.put(self.source / "chatui/index.html", b"<html>reviewed</html>")
        self.put(self.source / "scripts/real-client-update-helper.cjs", b"helper")
        self.put(self.source / "scripts/real-client-update-extract.py", b"extractor")
        self.put(self.source / "scripts/update-signing.pub", b"public key")
        self.put(self.source / "bridge/chat_server.py", b"server")
        self.put(self.source / "bridge/conversation_selection.py", b"selection")
        self.put(self.source / "bridge/unreviewed.py", b"unreviewed")
        # 本机运行数据（含模型连接与真实聊天库）永远不进入干净安装包。
        self.put(self.source / ".local/core/model/settings.json", b'{"apiKey":"synthetic-secret"}')
        self.put(self.source / ".local/core/mcp/mcp.sqlite3", b"private authorization records")
        self.put(self.source / "chat.db", b"private")
        self.put(self.client / "runtime/python/python.exe", b"python")
        self.put(self.client / "node_modules/dep/index.js", b"node dependency")
        runtime_rows = [self.row(self.client, item) for item in (
            "runtime/python/python.exe", "node_modules/dep/index.js")]
        self.put(self.build / "runtime-manifest.json", json.dumps({"files": runtime_rows}).encode())
        patches = (
            mock.patch.object(stage, "PUBLIC_FILES", ("chatui/index.html",)),
            mock.patch.object(stage, "SCRIPTS", ("real-client-update-helper.cjs",
                                                    "real-client-update-extract.py",
                                                    "update-signing.pub")),
            mock.patch.object(stage, "BRIDGE", ("chat_server.py", "conversation_selection.py")),
            mock.patch.object(stage, "NATIVE_READER", ()),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def row(root: Path, relative: str) -> dict:
        path = root / relative
        return {"file": relative, "bytes": path.stat().st_size,
                "sha256": stage.digest(path)}

    def test_new_stage_copies_only_allowlist_and_rejects_reuse(self):
        result = stage.stage_public(self.source, self.client)
        self.assertEqual(result["sourceVersion"], "1.0.2-preview.2")
        self.assertTrue((self.client / "scripts/real-client-update-helper.cjs").is_file())
        self.assertTrue((self.client / "scripts/real-client-update-extract.py").is_file())
        self.assertTrue((self.client / "scripts/update-signing.pub").is_file())
        self.assertEqual((self.client / "bridge/conversation_selection.py").read_bytes(), b"selection")
        self.assertFalse((self.client / "bridge/unreviewed.py").exists())
        self.assertFalse((self.client / ".local").exists())
        self.assertFalse(any(".local" in relative for relative in
                             {Path(row["file"]).parts for row in result["files"]}))
        self.assertFalse((self.client / "chat.db").exists())
        with self.assertRaisesRegex(ValueError, "already has a manifest"):
            stage.stage_public(self.source, self.client)

    def test_unexpected_staged_file_and_directory_are_rejected(self):
        self.put(self.client / "stale.txt", b"old build")
        with self.assertRaisesRegex(ValueError, "unexpected or missing content"):
            stage.stage_public(self.source, self.client)
        (self.client / "stale.txt").unlink()
        (self.client / "unexpected-empty-dir").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected or missing directories"):
            stage.stage_public(self.source, self.client)

    def test_changed_runtime_is_rejected_before_copy(self):
        (self.client / "runtime/python/python.exe").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            stage.stage_public(self.source, self.client)

    def test_package_hash_and_exact_inventory(self):
        stage.stage_public(self.source, self.client)
        expected, _ = builder.manifest_files(self.build)
        packaged = self.build / "release/win-unpacked/resources/client"
        shutil.copytree(self.client, packaged)
        builder.verify_tree(packaged, expected)
        self.put(packaged / "logs/private.log", b"private")
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            builder.verify_tree(packaged, expected)
        (packaged / "logs/private.log").unlink()
        (packaged / "logs").rmdir()
        (packaged / "scripts/update-signing.pub").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            builder.verify_tree(packaged, expected)

    def test_packaged_version_and_app_asar_gate(self):
        stage.stage_public(self.source, self.client)
        unpacked = self.build / "release/win-unpacked"
        shutil.copytree(self.client, unpacked / "resources/client")
        self.put(unpacked / "KnowYourFriends.exe", b"MZsynthetic")
        self.put(unpacked / "resources/app.asar", b"synthetic")
        with mock.patch.object(builder, "verify_asar") as asar:
            result = builder.verify_package(self.source, self.build, self.source / "node.exe")
            self.assertEqual(result["version"], "1.0.2-preview.2")
            asar.assert_called_once()
        (unpacked / "resources/client/package.json").write_text('{"version":"0.0.0"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            builder.verify_package(self.source, self.build, self.source / "node.exe")

    def test_generated_config_uses_unique_paths_and_external_electron(self):
        self.put(self.source / "electron-builder.real-client.json", builder.TEMPLATE.read_bytes())
        electron = self.root / "external-electron"
        self.put(electron / "electron.exe", b"MZ")
        generated = builder.generate_config(self.source, self.build, electron)
        config = json.loads(generated.read_text(encoding="utf-8"))
        self.assertEqual(config["directories"]["output"], str(self.build / "release"))
        self.assertEqual(config["electronDist"], str(electron))
        self.assertEqual(config["extraResources"][0]["from"], str(self.client))
        self.assertEqual(config["extraResources"][1]["from"], str(self.client / "node_modules"))
        template = json.loads((self.source / "electron-builder.real-client.json").read_text())
        self.assertEqual(template["extraResources"][0]["from"], builder.PLACEHOLDERS["stage"])

    def test_community_build_never_invokes_a_native_sender_build(self):
        node, python = self.source / "node.exe", self.source / "python.exe"
        self.put(node, b"synthetic node")
        self.put(python, b"synthetic Python")
        self.put(self.source / "node_modules/electron-builder/cli.js", b"synthetic builder")
        self.put(self.source / "native-sender/Kyf.WeChatSender.csproj", b"not a community input")
        args = SimpleNamespace(python_exe=python, node_exe=node, node_license=None, electron_dist=self.root / "electron")

        def verified(source, build, selected_node, *, require_clean=False):
            self.assertEqual(source, self.source)
            self.assertEqual(selected_node, node.resolve())
            self.assertIs(require_clean, False)
            return {"buildDirectory": str(build), "version": "2.0.0"}

        with (mock.patch.object(builder, "ROOT", self.source),
              mock.patch.object(builder.subprocess, "run") as run,
              mock.patch.object(builder.shutil, "which") as which,
              mock.patch.object(builder, "manifest_files", return_value=({}, {})),
              mock.patch.object(builder, "verify_tree"),
              mock.patch.object(builder, "generate_config", return_value=self.root / "generated.json"),
              mock.patch.object(builder, "verify_package", side_effect=verified)):
            result = builder.build(args)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 3, "Only runtime staging, client staging and Electron packaging should run")
        self.assertEqual(Path(calls[0][1]).name, "stage-real-runtime.py")
        self.assertEqual(Path(calls[1][1]).name, "stage-real-client.py")
        self.assertEqual(Path(calls[2][1]).name, "cli.js")
        self.assertTrue(all("--sender-output" not in call and "-OutputDirectory" not in call for call in calls))
        which.assert_not_called()
        build = Path(result["buildDirectory"])
        self.assertTrue(build.is_relative_to(self.source / ".local/portable-builds"))
        self.assertEqual(json.loads((build / "build-verification.json").read_text())["version"], "2.0.0")

    def test_new_plugin_folders_and_send_sdk_are_not_selected_implicitly(self):
        self.put(self.source / "plugins/kyf.mcp/plugin.json", b'{}')
        self.put(self.source / "plugins/kyf.wechat-send/sdk/license.lic", b"synthetic private license")
        self.put(self.source / "plugins/kyf.unknown-pro/backend/main.py", b"private feature")
        selected = {relative.as_posix() for _, relative, _ in stage.public_mappings(self.source)}
        self.assertIn("plugins/kyf.mcp/plugin.json", selected)
        self.assertFalse(any("wechat-send" in name or "unknown-pro" in name for name in selected))

    def test_project_docs_and_measurements_are_not_selected_or_reintroduced_by_an_allowlist(self):
        private_paths = ("docs/local-notes.md", "benchmarks/self-analysis-chat/results/run-manifest.json")
        for name in private_paths:
            self.put(self.source / name, b"synthetic local-only material")
        selected = {relative.as_posix() for _, relative, _ in stage.public_mappings(self.source)}
        self.assertTrue(set(private_paths).isdisjoint(selected))
        for name in private_paths:
            with self.subTest(path=name), mock.patch.object(stage, "PUBLIC_FILES", ("chatui/index.html", name)):
                with self.assertRaisesRegex(ValueError, "project docs and generated benchmark results"):
                    stage.stage_public(self.source, self.client)
                self.assertFalse((self.client / "chatui/index.html").exists())

    def test_formal_build_rejects_ignored_material_that_was_force_added_to_git(self):
        checkout = self.root / "tracked-private-material"
        checkout.mkdir()
        self.put(checkout / ".gitignore", b"/docs/\n/benchmarks/self-analysis-chat/results/\n")
        self.put(checkout / "docs/local-notes.md", b"synthetic local note")
        self.put(checkout / "benchmarks/self-analysis-chat/results/run-manifest.json", b"{}")
        def git(*args):
            return subprocess.run(["git", "-C", str(checkout), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        git("init", "-q")
        git("config", "user.name", "Synthetic Test")
        git("config", "user.email", "synthetic@example.invalid")
        git("add", ".gitignore")
        git("add", "-f", "docs/local-notes.md", "benchmarks/self-analysis-chat/results/run-manifest.json")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "synthetic tracked material")
        self.assertFalse(git("status", "--porcelain"))
        with self.assertRaisesRegex(ValueError, "cannot track project docs or generated benchmark results"):
            stage.source_revision(checkout, require_clean=True)
        git("rm", "-r", "--cached", "docs")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "remove synthetic docs")
        with self.assertRaisesRegex(ValueError, "cannot track project docs or generated benchmark results"):
            stage.source_revision(checkout, require_clean=True)
        git("rm", "-r", "--cached", "benchmarks/self-analysis-chat/results")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "remove synthetic results")
        self.assertFalse(stage.source_revision(checkout, require_clean=True)["sourceDirty"])

    def test_plugin_runtime_configuration_is_rejected_before_copy(self):
        self.put(self.source / "plugins/kyf.mcp/settings.json", b'{"credential":"synthetic"}')
        with self.assertRaisesRegex(ValueError, "plugin runtime data"):
            stage.stage_public(self.source, self.client)
        self.assertFalse((self.client / "chatui/index.html").exists())

    def test_manifest_cannot_approve_a_third_party_sender_module(self):
        relative = "runtime/python/Lib/site-packages/wechatauto/guia.py"
        self.put(self.client / relative, b"synthetic GUI sending implementation")
        manifest = self.build / "runtime-manifest.json"
        data = json.loads(manifest.read_text())
        data["files"].append(self.row(self.client, relative))
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "non-reader wechatauto module"):
            stage.stage_public(self.source, self.client)

    def test_source_revision_is_independent_from_package_hash_for_synthetic_sources(self):
        result = stage.stage_public(self.source, self.client)
        self.assertIsNone(result["sourceGitSha"])
        self.assertIsNone(result["sourceDirty"])
        self.assertEqual(result["sourcePackageSha256"], stage.digest(self.source / "package.json"))
        with self.assertRaisesRegex(ValueError, "clean Git checkout"):
            stage.stage_public(self.source, self.root / "formal", require_clean=True)

    def test_formal_verification_rejects_a_dirty_stage_even_after_checkout_is_clean(self):
        stage.stage_public(self.source, self.client)
        manifest = self.build / "client-files.json"
        client = json.loads(manifest.read_text())
        client.update(sourceGitSha="a" * 40, sourceDirty=True)
        manifest.write_text(json.dumps(client), encoding="utf-8")
        clean = {"sourceGitSha": "a" * 40, "sourceDirty": False}
        with mock.patch.object(builder, "source_revision", return_value=clean):
            with self.assertRaisesRegex(ValueError, "cannot reuse a dirty"):
                builder.verify_package(self.source, self.build, self.source / "node.exe", require_clean=True)

    def assert_formal_staging_rechecks_source(self, changed):
        clean = {"sourceGitSha": "a" * 40, "sourceDirty": False}
        with mock.patch.object(stage, "source_revision", side_effect=[clean, changed]):
            with self.assertRaisesRegex(ValueError, "changed during"):
                stage.stage_public(self.source, self.client, require_clean=True)
        self.assertFalse((self.build / "client-files.json").exists())

    def test_formal_staging_rechecks_clean_state_before_recording_manifest(self):
        self.assert_formal_staging_rechecks_source({"sourceGitSha": "a" * 40, "sourceDirty": True})

    def test_formal_staging_rechecks_tip_before_recording_manifest(self):
        self.assert_formal_staging_rechecks_source({"sourceGitSha": "b" * 40, "sourceDirty": False})

    def test_root_allowlist_keeps_update_files(self):
        for name in ("real-client-update.cjs", "real-client-update-controller.cjs",
                     "real-client-update-helper.cjs", "real-client-update-extract.py",
                     "update-signing.pub"):
            self.assertIn(name, load("stage_real_client_check", "stage-real-client.py").SCRIPTS)
            self.assertTrue((builder.ROOT / "scripts" / name).is_file())

    def test_real_selection_backend_is_in_stage_allowlist(self):
        actual_stage = load("stage_selection_check", "stage-real-client.py")
        self.assertIn("conversation_selection.py", actual_stage.BRIDGE)
        self.assertTrue((builder.ROOT / "bridge/conversation_selection.py").is_file())

    def test_message_analysis_dependencies_ship_with_the_chat_page(self):
        # Modified by Know Your Friends, 2026-10-10: prevent source-only feature delivery.
        # Modified by Know Your Friends, 2026-10-13: the analysis runs in the bundled kyf.message-insights plugin.
        actual_stage = load("stage_message_analysis_check", "stage-real-client.py")
        self.assertIn("friend_context.py", actual_stage.BRIDGE)
        self.assertTrue((builder.ROOT / "bridge/friend_context.py").is_file())
        staged = {relative.as_posix() for _, relative, _ in actual_stage.public_mappings(builder.ROOT)}
        for name in ("plugin.json", "backend/main.py", "backend/insights.py"):
            self.assertIn("plugins/kyf.message-insights/" + name, staged)
        self.assertIn("plugin-sdk/python/launch.py", staged)
        page = (builder.ROOT / "chatui/index.html").read_text(encoding="utf-8")
        for name in ("message-analysis.js", "message-analysis.css"):
            self.assertIn("chatui/" + name, actual_stage.PUBLIC_FILES)
            self.assertTrue((builder.ROOT / "chatui" / name).is_file())
            self.assertIn(name, page)

    def test_self_analysis_ships_a_complete_plugin_without_project_docs_or_private_data(self):
        stage = load("stage_self_analysis_check", "stage-real-client.py")
        staged = {relative.as_posix() for _source, relative, _base in stage.public_mappings(builder.ROOT)}
        for name in ("plugin.json", "backend/main.py", "backend/self_store.py", "backend/self_worker.py",
                     "backend/self_samples.py", "ui/index.html", "ui/page.js", "ui/page.css"):
            self.assertIn("plugins/kyf.self-analysis/" + name, staged)
        self.assertIn("README.md", staged)
        self.assertFalse(any(name.casefold().startswith("docs/") for name in staged))
        for relative in ("scripts/embedding-model.json", "scripts/embedding-runner.cjs", "bridge/core_embeddings.py", "licenses/BGE-MIT.txt"):
            self.assertIn(relative, staged)
        self.assertFalse(any("/tests/" in name or "/.local/" in name or name.endswith(".sqlite3") for name in staged))

    def test_self_analysis_native_window_ships_main_preload_and_host_page(self):
        actual_stage = load("stage_self_analysis_window", "stage-real-client.py")
        release = load("release_self_analysis_window", "build-windows-release.py")
        packaged = json.loads(builder.TEMPLATE.read_text(encoding="utf-8"))["files"]
        for name in ("self-analysis-window.cjs", "self-analysis-preload.cjs"):
            self.assertIn(name, actual_stage.SCRIPTS)
            self.assertIn("scripts/" + name, packaged)
            self.assertIn("scripts/" + name, release.ASAR_SCRIPTS)
        for name in ("self-analysis.html", "self-analysis-window.js", "self-analysis-window.css"):
            self.assertIn("chatui/" + name, actual_stage.PUBLIC_FILES)
            self.assertTrue((builder.ROOT / "chatui" / name).is_file())

    def test_model_trace_window_ships_with_its_host_preload_and_page(self):
        actual_stage = load("stage_model_traces_check", "stage-real-client.py")
        release = load("release_model_traces_check", "build-windows-release.py")
        packaged = json.loads(builder.TEMPLATE.read_text(encoding="utf-8"))["files"]
        for name in ("model-traces-host.cjs", "model-traces-window.cjs", "model-traces-preload.cjs"):
            relative = "scripts/" + name
            self.assertIn(name, actual_stage.SCRIPTS)
            self.assertIn(relative, packaged)
            self.assertIn(relative, release.ASAR_SCRIPTS)
            self.assertTrue((builder.ROOT / relative).is_file())
        for name in ("model-traces-window.html", "model-traces-window.js", "model-traces.js", "model-traces.css", "model-usage.js"):
            self.assertIn("chatui/" + name, actual_stage.PUBLIC_FILES)
            self.assertTrue((builder.ROOT / "chatui" / name).is_file())
        page = (builder.ROOT / "chatui/model-traces-window.html").read_text(encoding="utf-8")
        for name in ("model-traces-window.js", "model-traces.js", "model-traces.css", "model-usage.js"):
            self.assertIn(name, page)

    def test_settings_window_ships_with_its_host_and_renderer_assets(self):
        actual_stage = load("stage_settings_window_check", "stage-real-client.py")
        release = load("release_settings_window_check", "build-windows-release.py")
        packaged = json.loads(builder.TEMPLATE.read_text(encoding="utf-8"))["files"]
        name = "settings-window.cjs"
        relative = "scripts/" + name
        self.assertIn(name, actual_stage.SCRIPTS)
        self.assertIn(relative, packaged)
        self.assertIn(relative, release.ASAR_SCRIPTS)
        self.assertTrue((builder.ROOT / relative).is_file())
        page = (builder.ROOT / "chatui/index.html").read_text(encoding="utf-8")
        for name in ("settings-window.js", "settings-window.css"):
            self.assertIn("chatui/" + name, actual_stage.PUBLIC_FILES)
            self.assertTrue((builder.ROOT / "chatui" / name).is_file())
            self.assertIn(name, page)

    def test_runtime_stage_refuses_nonempty_directory_without_replacing_it(self):
        stale = self.root / "stale-stage"
        self.put(stale / "keep.txt", b"keep")
        run = subprocess.run([sys.executable, str(builder.ROOT / "scripts/stage-real-runtime.py"),
                              "--output", str(stale)], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("runtime stage must be new and empty", run.stderr)
        self.assertEqual((stale / "keep.txt").read_bytes(), b"keep")

    def test_real_asar_api_checks_script_hashes_and_version(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        config = json.loads(builder.TEMPLATE.read_text(encoding="utf-8"))
        names = [name for name in config["files"] if name.startswith("scripts/")]
        app_dir = self.root / "synthetic-app"
        expected = {}
        for name in names:
            source = builder.ROOT / name
            self.put(app_dir / name, source.read_bytes())
            expected[name.casefold()] = self.row(app_dir, name)
        version = json.loads((builder.ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        self.put(app_dir / "package.json", json.dumps({
            "name": "know-your-friends", "version": version, "main": "scripts/desktop-main.cjs",
        }).encode())
        unpacked = self.root / "synthetic-unpacked"
        archive = unpacked / "resources/app.asar"
        archive.parent.mkdir(parents=True)
        subprocess.run([node, "-e", "require('@electron/asar').createPackage(process.argv[1], process.argv[2]).catch(e=>{console.error(e);process.exit(1)})",
                        str(app_dir), str(archive)], cwd=builder.ROOT, check=True)
        builder.verify_asar(builder.ROOT, unpacked, Path(node), version, expected)
        expected[names[0].casefold()]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "app.asar script hash differs"):
            builder.verify_asar(builder.ROOT, unpacked, Path(node), version, expected)


if __name__ == "__main__":
    unittest.main()
