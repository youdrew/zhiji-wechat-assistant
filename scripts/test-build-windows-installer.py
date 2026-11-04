"""Offline installer-boundary tests. Never run an installer or open real user data."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_windows_installer_test", ROOT / "scripts/build-windows-installer.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def generated_config() -> dict:
    return {
        "appId": "com.local.knowyourfriends.real-client",
        "productName": "知己微信助手",
        "directories": {"output": "original-output"},
        "extraMetadata": {"name": "know-your-friends", "kyfUpdatesEnabled": False},
        "win": {"target": ["dir"], "executableName": "KnowYourFriends"},
    }


class UninstallBoundaryTests(unittest.TestCase):
    def test_process_guard_never_closes_or_terminates_processes(self):
        include = installer.render_uninstall_include(["KnowYourFriends.exe"])
        guard = re.search(r"!macro customCheckAppRunning\s+([\s\S]*?)!macroend", include)
        self.assertIsNotNone(guard, "Override the builder's default process termination behavior")
        body = guard.group(1)
        checks = re.findall(r'\$\{nsProcess::FindProcess\}\s+"([^"]+)"', body)
        self.assertEqual(checks, ["${APP_EXECUTABLE_FILENAME}"], "Check only the app, without blocking unrelated Python/Node or development helpers")
        self.assertNotRegex(include.lower(), r"killprocess|closeprocess|stop-process|taskkill|terminateprocess|execshell")

    @unittest.skipUnless(os.name == "nt", "NSIS control-flow probe runs only on Windows")
    def test_compiled_process_guard_allows_only_confirmed_no_match_and_preserves_registers(self):
        # Execute the actual generated macro with a stubbed plugin return value.
        # This tiny program has no install section, registry writes or file operations.
        cache = ROOT / ".local/cache/electron-builder"
        compiler = shutil.which("makensis") or next(iter(sorted(cache.glob("nsis-*/nsis-*/makensis.exe"))), None)
        if compiler is None:
            self.skipTest("An existing offline NSIS compiler is unavailable")
        nsis_include = ROOT / "node_modules/app-builder-lib/templates/nsis/include"
        if not (nsis_include / "nsProcess.nsh").is_file():
            self.skipTest("The bundled NSIS plugin definitions are unavailable")
        with tempfile.TemporaryDirectory(prefix="kyf-process-guard-test-") as folder:
            root = Path(folder)
            include = root / "process-guard.nsh"
            include.write_text(installer.render_uninstall_include(["KnowYourFriends.exe"]), encoding="utf-8")
            executable = root / "process-guard.exe"
            script = root / "process-guard.nsi"
            script.write_text("\n".join([
                'Unicode true', 'Name "Synthetic process guard probe"',
                f'OutFile "{executable}"', 'RequestExecutionLevel user', 'SilentInstall silent',
                '!include "LogicLib.nsh"', '!include "FileFunc.nsh"',
                f'!addincludedir "{nsis_include}"',
                '!define APP_EXECUTABLE_FILENAME "Synthetic-App-For-Stub-Only.exe"',
                f'!include "{include}"', '!undef nsProcess::FindProcess',
                '!define nsProcess::FindProcess `!insertmacro fixtureFindProcess`',
                '!macro fixtureFindProcess ignored output',
                '  ${GetParameters} $R1', '  ${GetOptions} "$R1" "/PROBE=" ${output}',
                '!macroend',
                'Section "Probe"', '  StrCpy $R0 "preserved-register"',
                '  !insertmacro customCheckAppRunning',
                '  ${If} $R0 != "preserved-register"', '    SetErrorLevel 38', '    Quit', '  ${EndIf}',
                '  SetErrorLevel 37', 'SectionEnd', '',
            ]), encoding="utf-8")
            compiled = subprocess.run([str(compiler), "/V2", "/NOCD", str(script)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            for code in ["603", "0", "601", "602", *map(str, range(604, 612)), "999", "-1", "603error", "error", ""]:
                with self.subTest(plugin_return=code):
                    probe = subprocess.run([str(executable), "/S", f"/PROBE={code}"],
                                           capture_output=True, text=True, timeout=10)
                    self.assertEqual(probe.returncode, 37 if code == "603" else 2,
                                     "Only confirmed no-match may continue; all failures must exit before install/uninstall")

    def test_paths_are_exact_and_dollar_variables_stay_literal(self):
        self.assertEqual(installer.nsis_path("resources/client/$draft.js"), r"resources\client\$$draft.js")
        self.assertEqual(installer.nsis_path("目录/带 空格.txt"), "目录\\带 空格.txt")
        for name in ("", ".", "..", "../outside", "/absolute", "C:/absolute", r"a\b",
                     "a//b", "a/./b", "a/../b", "a/", "a*/b", "a?/b", "a\n/b", "a\r/b",
                     'a"/b', "a|b", "a<b", "a>b", "a./b", "a /b", ".local/private.db",
                     "resources/client/.LOCAL/settings.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                installer.nsis_path(name)

    def test_uninstall_removes_only_recorded_files_and_retains_runtime_and_unknown_files(self):
        shipped = ["KnowYourFriends.exe", "resources/app.asar", "resources/client/ui/$draft.js"]
        uninstaller_name = "Uninstall KnowYourFriends.exe"
        retained = ["resources/client/.local/accounts/synthetic.sqlite3",
                    "resources/client/.local/config/settings.json",
                    "resources/client/.local/logs/synthetic.log",
                    "resources/client/ui/user-notes.txt", "personal-notes.txt"]
        text = installer.render_uninstall_include(shipped)
        self.assertIn('SetOutPath "$TEMP"', text)
        self.assertNotIn("/REBOOTOK", text)
        self.assertNotIn("RMDir /r", text)
        self.assertNotIn(".local", text)
        self.assertIn("SetErrorLevel 2", text)
        self.assertIn("Abort ", text)
        self.assertIn('Delete "$INSTDIR\\${UNINSTALL_FILENAME}"', text)
        self.assertEqual(text, installer.render_uninstall_include(list(reversed(shipped))))
        with tempfile.TemporaryDirectory(prefix="kyf-installer-uninstall-test-") as folder:
            target = Path(folder) / "install"
            for relative in shipped + retained + [uninstaller_name]:
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic sentinel")
            # Evaluate only the generated exact Delete/RMDir effects on this temp tree.
            for line in text.splitlines():
                command, _, argument = line.strip().partition(" ")
                if command not in {"Delete", "RMDir"}:
                    continue
                self.assertTrue(argument.startswith('"$INSTDIR'))
                value = argument.strip('"').removeprefix("$INSTDIR").lstrip("\\")
                value = value.replace("${UNINSTALL_FILENAME}", uninstaller_name).replace("$$", "$")
                path = target.joinpath(*value.split("\\")) if value else target
                self.assertTrue(path.resolve().is_relative_to(target.resolve()))
                if command == "Delete":
                    if path.exists():
                        path.unlink()
                else:
                    try:
                        path.rmdir()
                    except OSError:
                        pass  # A nonempty user directory is intentionally retained.
            for relative in shipped + [uninstaller_name]:
                self.assertFalse((target / relative).exists(), relative)
            for relative in retained:
                self.assertEqual((target / relative).read_bytes(), b"synthetic sentinel", relative)

    def test_empty_duplicate_or_private_inventory_is_rejected(self):
        for names in ([], ["A.exe", "a.exe"], ["resources/client/.local/account.json"], ["../outside"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                installer.render_uninstall_include(names)


class InstallerConfigurationTests(unittest.TestCase):
    def test_actual_packaging_template_keeps_the_first_release_policy(self):
        template = json.loads((ROOT / "electron-builder.real-client.json").read_text(encoding="utf-8"))
        result = installer.installer_config(template, Path("output"), Path("preserve.nsh"))
        self.assertIs(result["extraMetadata"]["kyfUpdatesEnabled"], False)
        self.assertEqual(result["extraMetadata"]["main"], "scripts/desktop-main.cjs")
        self.assertEqual(result["appId"], "com.local.knowyourfriends.real-client")

    def test_per_user_configuration_preserves_compatibility_and_disables_updates(self):
        source = generated_config()
        result = installer.installer_config(source, Path("output"), Path("preserve.nsh"))
        self.assertEqual(source, generated_config(), "The verified generated config must not be mutated")
        self.assertEqual(result["appId"], source["appId"])
        self.assertEqual(result["win"]["executableName"], "KnowYourFriends")
        self.assertEqual(result["extraMetadata"]["name"], "know-your-friends")
        self.assertIs(result["extraMetadata"]["kyfUpdatesEnabled"], False)
        self.assertEqual(result["win"]["target"], ["nsis"])
        self.assertEqual(result["directories"]["output"], "output")
        self.assertTrue(result["nsis"]["oneClick"])
        self.assertIs(result["nsis"]["useZip"], True, "The bundled NSIS decoder must preserve ARM64 launcher bytes")
        self.assertIs(result["nsis"]["differentialPackage"], False, "Differential 7z packaging must not override ZIP/Deflate")
        for key in ("perMachine", "packElevateHelper", "deleteAppDataOnUninstall", "runAfterFinish"):
            self.assertIs(result["nsis"][key], False, key)
        self.assertEqual(result["nsis"]["include"], "preserve.nsh")

    def test_changed_identity_or_implicit_update_policy_is_rejected(self):
        for section, key, value in ((None, "appId", "other.id"), ("extraMetadata", "name", "renamed"),
                                    ("win", "executableName", "Renamed"),
                                    ("extraMetadata", "kyfUpdatesEnabled", True),
                                    ("extraMetadata", "kyfUpdatesEnabled", "false"),
                                    ("extraMetadata", "kyfUpdatesEnabled", None)):
            config = generated_config()
            (config[section] if section else config)[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                installer.installer_config(config, Path("output"), Path("include"))


class PackageInventoryTests(unittest.TestCase):
    def test_full_tree_private_paths_are_rejected_before_any_external_build(self):
        for relative in ("logs/private.log", ".local/config.json", "resources/client/accounts/account.json",
                         "private.sqlite3", "resources/private.key", "screenshots/private.png"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(prefix="kyf-installer-scan-") as temporary:
                unpacked = Path(temporary) / "win-unpacked"
                path = unpacked / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(b"synthetic private sentinel")
                with mock.patch.object(installer.release, "_require_package") as identity:
                    with self.assertRaisesRegex(ValueError, "private|unreviewed"):
                        installer.package_inventory(unpacked, "1.3.6", Path("node.exe"))
                    identity.assert_not_called()

    def test_inventory_hashes_include_files_outside_client_resources(self):
        with tempfile.TemporaryDirectory(prefix="kyf-installer-inventory-") as temporary:
            unpacked = Path(temporary) / "win-unpacked"
            unpacked.mkdir()
            (unpacked / "KnowYourFriends.exe").write_bytes(b"MZsynthetic")
            with mock.patch.object(installer.release, "_require_package") as identity:
                first = installer.package_inventory(unpacked, "1.3.6", Path("node.exe"))
                identity.assert_called_once()
                self.assertEqual(first["KnowYourFriends.exe"]["sha256"], hashlib.sha256(b"MZsynthetic").hexdigest())
                (unpacked / "KnowYourFriends.exe").write_bytes(b"MZchanged")
                second = installer.package_inventory(unpacked, "1.3.6", Path("node.exe"))
                self.assertNotEqual(first, second)


class BuildOrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kyf-installer-build-test-")
        self.addCleanup(temporary.cleanup)
        self.build = Path(temporary.name)
        self.unpacked = self.build / "release/win-unpacked"
        self.unpacked.mkdir(parents=True)
        (self.build / "electron-builder.generated.json").write_text(json.dumps(generated_config()), encoding="utf-8")
        self.before = {"KnowYourFriends.exe": {"bytes": 11, "sha256": "synthetic-source-hash"}}
        self.result = {"version": "1.3.6", "buildDirectory": str(self.build), "verifiedFiles": 1}
        self.verify = mock.patch.object(installer.clean, "verify_package", return_value=self.result).start()
        self.inventory = mock.patch.object(installer, "package_inventory", return_value=self.before).start()
        self.policy = mock.patch.object(installer.subprocess, "check_output", return_value="false").start()
        self.builder = mock.patch.object(installer.subprocess, "run", side_effect=self.fake_builder).start()
        self.addCleanup(mock.patch.stopall)
        self.payload = b"MZsynthetic installer"

    def fake_builder(self, args, **kwargs):
        self.assertEqual(args[args.index("--prepackaged") + 1], str(self.unpacked))
        self.assertEqual(args[args.index("--publish") + 1], "never")
        self.assertIn("--x64", args)
        self.assertTrue(kwargs["check"])
        config_file = Path(args[args.index("--config") + 1])
        config = json.loads(config_file.read_text(encoding="utf-8"))
        output = Path(config["directories"]["output"])
        output.mkdir()
        if self.payload is not None:
            (output / "Zhiji-WeChat-Assistant-1.3.6-Setup-x64.exe").write_bytes(self.payload)

    def test_build_revalidates_source_and_records_installer_hash_without_publishing(self):
        result = installer.build_installer(self.build, Path("node.exe"))
        self.verify.assert_called_once()
        self.assertEqual(self.inventory.call_count, 2)
        self.assertEqual(result["installerSha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertIs(result["published"], False)
        self.assertIs(result["appUpdatesEnabled"], False)
        record = Path(result["installerBuildDirectory"]) / "installer-verification.json"
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["installerSha256"], result["installerSha256"])
        bootstrap = Path(result["bootstrap"])
        content = bootstrap.read_bytes()
        self.assertEqual(bootstrap, Path(result["installer"]).with_name("install-zhiji.ps1"))
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(f"$InstallerSha256 = '{result['installerSha256']}'".encode(), content)
        self.assertIn(f"$InstallerFileName = '{Path(result['installer']).name}'".encode(), content)
        self.assertIn(b"$DownloadUrl = '__ZHIJI_DOWNLOAD_URL__'", content)
        self.assertEqual(result["bootstrapSha256"], hashlib.sha256(content).hexdigest())
        self.assertIsNone(result["bootstrapDownloadUrl"])

    def test_bootstrap_uses_only_an_explicit_https_asset_url(self):
        asset = "Zhiji-WeChat-Assistant-1.3.6-Setup-x64.exe"
        invalid = (f"http://example.test/{asset}", f"https://user@example.test/{asset}",
                   f"https://example.test/{asset}?expires=1", f"https://example.test/{asset}#fragment",
                   f"https://example.test/\n{asset}", "https://example.test/other.exe")
        for url in invalid:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "HTTPS URL"):
                installer.build_installer(self.build, Path("node.exe"), installer_url=url)
        self.builder.assert_not_called()
        url = f"https://example.test/publisher's-release/{asset}"
        result = installer.build_installer(self.build, Path("node.exe"), installer_url=url)
        self.assertEqual(result["bootstrapDownloadUrl"], url)
        escaped_url = url.replace("'", "''")
        self.assertIn(f"$DownloadUrl = '{escaped_url}'", Path(result["bootstrap"]).read_text(encoding="utf-8-sig"))

    def test_invalid_clean_stage_or_unstable_version_stops_before_builder(self):
        self.verify.side_effect = ValueError("client hash differs")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            installer.build_installer(self.build, Path("node.exe"))
        self.builder.assert_not_called()
        self.verify.side_effect = None
        self.verify.return_value = {**self.result, "version": "1.3.6-preview.1"}
        with self.assertRaisesRegex(ValueError, "stable"):
            installer.build_installer(self.build, Path("node.exe"))
        self.builder.assert_not_called()

    def test_formal_installer_reuse_passes_the_clean_requirement_to_stage_verification(self):
        self.verify.side_effect = ValueError("a formal build cannot reuse a dirty source snapshot")
        with self.assertRaisesRegex(ValueError, "cannot reuse a dirty"):
            installer.build_installer(self.build, Path("node.exe"), require_clean=True)
        self.assertIs(self.verify.call_args.kwargs["require_clean"], True)
        self.builder.assert_not_called()

    def test_packaged_update_policy_must_explicitly_be_false(self):
        for policy in ("true", "null", "undefined", ""):
            self.policy.return_value = policy
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "updates must be disabled"):
                installer.build_installer(self.build, Path("node.exe"))
        self.builder.assert_not_called()

    def test_changed_input_tree_rejects_the_installer_after_build(self):
        changed = {**self.before, "new-file.txt": {"bytes": 1, "sha256": "changed"}}
        self.inventory.side_effect = [self.before, changed]
        with self.assertRaisesRegex(ValueError, "package changed"):
            installer.build_installer(self.build, Path("node.exe"))
        self.assertEqual(len(list(self.build.glob("installer-*/installer-verification.json"))), 0)

    def test_missing_or_invalid_executable_never_gets_a_verification_record(self):
        for payload in (None, b"", b"not an exe"):
            self.payload = payload
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "output is missing|header is invalid"):
                installer.build_installer(self.build, Path("node.exe"))
        self.assertEqual(len(list(self.build.glob("installer-*/installer-verification.json"))), 0)


if __name__ == "__main__":
    unittest.main()
