"""zhiji-mcp-cli (G12): config merges, DPAPI storage, not-installed path and end-to-end connect.

The end-to-end tests run the real CLI as a subprocess against an in-process fake Core
(real McpServices + McpPairingService behind a loopback HTTP server, with a scripted
pairing presenter) and a fake stdio adapter that loads the credential by profile name.
No real client config, credential directory or Core installation is touched: the home,
credential and Core locations are redirected through the CLI's test environment variables.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(SCRIPTS))

from core_mcp_pairing import McpPairingService, handle_pairing_path  # noqa: E402
from core_mcp_proxy import McpError, McpServices  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("zhiji_mcp_cli", SCRIPTS / "zhiji-mcp-cli.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

COMMAND = r"C:\Zhiji\resources\client\runtime\python\python.exe"
ARGS = [r"C:\Zhiji\resources\client\scripts\mcp-stdio-adapter.py", "--root", r"C:\Zhiji\resources\client",
        "--profile", "codex"]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zhiji-cli-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {cli.HOME_ENV: str(self.home), "ZHIJI_CLI_NO_CLIENT_CLI": "1",
                    "ZHIJI_MCP_HOME": str(self.tmp / "creds"), cli.NO_DISCOVERY_ENV: "1"}
        self.saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backups(self, path):
        return sorted(path.parent.glob(path.name + ".zhiji-backup-*"))


class JsonConfigTests(Sandbox):
    def test_claude_user_files_honor_native_config_directory(self):
        custom = self.tmp / "separate claude config"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom), cli.HOME_ENV: ""}):
            config, _ = cli.config_target("claude-code", "user", None)
            self.assertEqual(config.path, custom / ".claude.json")
            self.assertEqual(cli.hook_settings_path("user", None), custom / "settings.json")
            self.assertEqual(cli.skill_target("user", None), custom / "skills" / "zhiji")

    def test_claude_user_and_project_and_cursor_preserve_unrelated_settings(self):
        project = self.tmp / "proj"
        project.mkdir()
        cases = (("claude-code", "user", self.home / ".claude.json"),
                 ("claude-code", "project", project / ".mcp.json"),
                 ("cursor", "user", self.home / ".cursor" / "mcp.json"),
                 ("cursor", "project", project / ".cursor" / "mcp.json"))
        for client, scope, path in cases:
            with self.subTest(client=client, scope=scope):
                path.parent.mkdir(parents=True, exist_ok=True)
                original = {"theme": "dark", "projects": {"x": {"allowedTools": ["a"]}},
                            "mcpServers": {"other": {"command": "other.exe", "args": ["--x"]}}}
                path.write_text(json.dumps(original), encoding="utf-8")
                result = cli.apply_config(client, scope, project, "zhiji", COMMAND, ARGS, dry_run=False)
                self.assertTrue(result["changed"])
                written = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(written["theme"], "dark")
                self.assertEqual(written["projects"], original["projects"])
                self.assertEqual(written["mcpServers"]["other"], original["mcpServers"]["other"])
                self.assertEqual(written["mcpServers"]["zhiji"]["args"], ARGS)
                self.assertEqual(len(self.backups(path)), 1)
                self.assertEqual(json.loads(self.backups(path)[0].read_text(encoding="utf-8")), original)
                # Idempotent: a second run changes nothing and makes no new backup.
                again = cli.apply_config(client, scope, project, "zhiji", COMMAND, ARGS, dry_run=False)
                self.assertFalse(again["changed"])
                self.assertEqual(len(self.backups(path)), 1)
                self.assertEqual(cli.configured_command(client, scope, project, "zhiji"), (COMMAND, ARGS))
                removed = cli.remove_config(client, scope, project, "zhiji", dry_run=False)
                self.assertTrue(removed["changed"])
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mcpServers"],
                                 original["mcpServers"])

    def test_invalid_json_is_refused_untouched_and_dry_run_writes_nothing(self):
        path = self.home / ".cursor" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(cli.CliError) as caught:
            cli.apply_config("cursor", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        self.assertEqual(caught.exception.code, cli.EXIT_CONFIG)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
        path.write_text("{}", encoding="utf-8")
        result = cli.apply_config("cursor", "user", None, "zhiji", COMMAND, ARGS, dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{}")
        self.assertEqual(self.backups(path), [])

    def test_no_secret_ever_reaches_a_config(self):
        rendered = cli.JsonServersConfig(self.tmp / "x.json", stdio_type=True).render(None, "zhiji", COMMAND, ARGS)
        self.assertNotIn("zmc_", rendered)
        self.assertIn("--profile", rendered)

    def test_claude_update_does_not_remove_the_existing_server_first(self):
        path = self.home / ".claude.json"
        original = {"mcpServers": {"zhiji": {"type": "stdio", "command": "old.exe", "args": ["--old"]},
                                   "other": {"type": "stdio", "command": "other.exe"}}}
        path.write_text(json.dumps(original), encoding="utf-8")
        with mock.patch.object(cli, "claude_cli", return_value="claude.exe"), \
                mock.patch.object(cli.subprocess, "run") as run:
            result = cli.apply_config("claude-code", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        run.assert_not_called()
        self.assertEqual(cli.configured_command("claude-code", "user", None, "zhiji"), (COMMAND, ARGS))
        self.assertEqual(json.loads(result["backup"].read_text(encoding="utf-8")), original)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["other"],
                         original["mcpServers"]["other"])


class CodexTomlTests(Sandbox):
    path = property(lambda self: self.home / ".codex" / "config.toml")

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_merge_preserves_unrelated_content_and_replaces_in_place(self):
        original = ('model = "o3"\n# keep me\n[profiles.fast]\nmodel = "mini"\n\n'
                    '[mcp_servers.other]\ncommand = "other"\nargs = []\n[mcp_servers.other.env]\nA = "1"\n')
        self.write(original)
        cli.apply_config("codex", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        parsed = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"], "o3")
        self.assertEqual(parsed["profiles"], {"fast": {"model": "mini"}})
        self.assertEqual(parsed["mcp_servers"]["other"], {"command": "other", "args": [], "env": {"A": "1"}})
        self.assertEqual(parsed["mcp_servers"]["zhiji"]["command"], COMMAND)
        self.assertEqual(parsed["mcp_servers"]["zhiji"]["args"], ARGS)
        self.assertIn("# keep me", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.backups(self.path)[0].read_text(encoding="utf-8"), original)
        # Re-running with a new path replaces the one block (no duplicate table).
        cli.apply_config("codex", "user", None, "zhiji", COMMAND + "2", ARGS, dry_run=False)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("[mcp_servers.zhiji]"), 1)
        self.assertEqual(tomllib.loads(text)["mcp_servers"]["zhiji"]["command"], COMMAND + "2")
        cli.remove_config("codex", "user", None, "zhiji", dry_run=False)
        after = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("zhiji", after["mcp_servers"])
        self.assertEqual(after["mcp_servers"]["other"]["env"], {"A": "1"})

    def test_inline_parent_table_entry_is_refused_untouched(self):
        original = '[mcp_servers]\nzhiji = { command = "old.exe", args = [] }\n'
        self.write(original)
        tomllib.loads(original)
        with self.assertRaises(cli.CliError) as caught:
            cli.apply_config("codex", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        self.assertEqual(caught.exception.code, cli.EXIT_CONFIG)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.backups(self.path), [])
        # The same inline form for another server is merged fine and stays valid TOML.
        self.write('[mcp_servers]\nother = { command = "o", args = [] }\n')
        cli.apply_config("codex", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        parsed = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(parsed["mcp_servers"]), {"other", "zhiji"})

    def test_multiline_string_with_a_fake_header_is_preserved_or_refused(self):
        original = ('instructions = """\nline\n[mcp_servers.zhiji]\ncommand = "fake"\n"""\n'
                    '[mcp_servers.other]\ncommand = "o"\nargs = []\n')
        parsed_before = tomllib.loads(original)
        self.write(original)
        try:
            cli.apply_config("codex", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        except cli.CliError as exc:
            self.assertEqual(exc.code, cli.EXIT_CONFIG)
            self.assertEqual(self.path.read_text(encoding="utf-8"), original)
            return
        parsed = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["instructions"], parsed_before["instructions"])
        self.assertEqual(parsed["mcp_servers"]["zhiji"]["args"], ARGS)

    def test_dotted_profile_names_are_rejected_and_odd_keys_are_quoted(self):
        store = cli.credentials()
        with self.assertRaises(store.CredentialError):
            store.check_name("work.personal")
        rendered = cli.CodexTomlConfig(self.path).render("", "zhiji-a.b", COMMAND, ARGS)
        self.assertIn('[mcp_servers."zhiji-a.b"]', rendered)
        self.assertEqual(tomllib.loads(rendered)["mcp_servers"]["zhiji-a.b"]["args"], ARGS)

    def test_invalid_toml_is_refused(self):
        self.write("model = \n")
        with self.assertRaises(cli.CliError):
            cli.apply_config("codex", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "model = \n")


class HarnessPatchTests(Sandbox):
    def test_managed_block_is_appended_replaced_and_removed(self):
        path = self.home / ".dsh" / "cordis.patch.yml"
        path.parent.mkdir(parents=True)
        original = "# user patches\n- insert:\n    - id: memory-memorix\n      name: x\n"
        path.write_text(original, encoding="utf-8")
        cli.apply_config("deepseek-harness", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(original.rstrip("\n")))
        self.assertIn("serverName: zhiji", text)
        self.assertEqual(cli.configured_command("deepseek-harness", "user", None, "zhiji"), (COMMAND, ARGS))
        cli.apply_config("deepseek-harness", "user", None, "zhiji", COMMAND + "2", ARGS, dry_run=False)
        self.assertEqual(path.read_text(encoding="utf-8").count("id: mcp-zhiji"), 1)
        cli.remove_config("deepseek-harness", "user", None, "zhiji", dry_run=False)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_a_mapping_document_is_refused(self):
        path = self.home / ".dsh" / "cordis.patch.yml"
        path.parent.mkdir(parents=True)
        path.write_text("plugins:\n  a: 1\n", encoding="utf-8")
        with self.assertRaises(cli.CliError):
            cli.apply_config("deepseek-harness", "user", None, "zhiji", COMMAND, ARGS, dry_run=False)


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class DpapiTests(Sandbox):
    def test_round_trip_and_ciphertext_on_disk(self):
        store = cli.credentials()
        path = store.save_profile("codex", "mcp_abc", "zmc_" + "9" * 64, root="C:/x", client="codex")
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("zmc_", raw)
        self.assertEqual(store.load_adapter_credentials("codex"), ("mcp_abc", "zmc_" + "9" * 64))
        self.assertEqual(store.profile_info("codex")["clientId"], "mcp_abc")
        self.assertNotEqual(store.unprotect(store.protect(b"x")), b"y")
        document = json.loads(raw)
        document["credential"] = document["credential"][:-8] + "AAAAAAA="
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(store.CredentialError):
            store.load_profile("codex")


class NotInstalledTests(Sandbox):
    def test_standalone_cli_reports_not_installed_with_guidance(self):
        lonely = self.tmp / "download"
        lonely.mkdir()
        for name in ("zhiji-mcp-cli.py", "zhiji_mcp_http.py"):
            shutil.copy2(SCRIPTS / name, lonely / name)
        for command in (["connect", "--client", "codex"], ["status"]):
            completed = subprocess.run([sys.executable, str(lonely / "zhiji-mcp-cli.py"), *command],
                                       capture_output=True, text=True, encoding="utf-8", env={**os.environ},
                                       timeout=60)
            self.assertEqual(completed.returncode, cli.EXIT_NOT_INSTALLED, completed.stdout + completed.stderr)
            self.assertIn("未安装", completed.stdout)
            self.assertIn("安装包", completed.stdout)

    def test_root_that_is_not_an_installation_is_rejected(self):
        args = cli.build_parser().parse_args(["status", "--root", str(self.tmp)])
        with self.assertRaises(cli.CliError):
            cli.find_installation(args)


class SkillInstallTests(Sandbox):
    def setUp(self):
        super().setUp()
        root = self.tmp / "install"
        self.source = root / "plugins" / "kyf.mcp" / "skill" / "zhiji"
        self.source.mkdir(parents=True)
        shutil.copy2(ROOT / "plugins" / "kyf.mcp" / "skill" / "zhiji" / "SKILL.md", self.source / "SKILL.md")
        self.inst = cli.Installation(root, sys.executable, "test", True)

    def test_user_scope_copies_then_is_idempotent_and_backs_up_a_differing_copy_outside_skills(self):
        target = self.home / ".claude" / "skills" / "zhiji"
        dry = cli.install_skill(self.inst, "user", None, dry_run=True)
        self.assertEqual(dry["path"], target)
        self.assertTrue(dry["changed"])
        self.assertFalse(target.exists())
        first = cli.install_skill(self.inst, "user", None, dry_run=False)
        self.assertTrue(first["changed"])
        self.assertIsNone(first["backup"])
        self.assertEqual((target / "SKILL.md").read_bytes(), (self.source / "SKILL.md").read_bytes())
        again = cli.install_skill(self.inst, "user", None, dry_run=False)
        self.assertFalse(again["changed"])
        # A user-edited copy (changed file plus an extra file) is moved aside, not merged.
        (target / "SKILL.md").write_text("---\nname: zhiji\ndescription: mine\n---\n", encoding="utf-8")
        (target / "notes.md").write_text("mine", encoding="utf-8")
        replaced = cli.install_skill(self.inst, "user", None, dry_run=False)
        self.assertTrue(replaced["changed"])
        backup = replaced["backup"]
        self.assertEqual(backup.parent, self.home / ".claude")
        self.assertEqual((backup / "notes.md").read_text(encoding="utf-8"), "mine")
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["SKILL.md"])
        self.assertEqual([path.name for path in (self.home / ".claude" / "skills").iterdir()], ["zhiji"])

    def test_project_scope_and_missing_source(self):
        project = self.tmp / "proj"
        project.mkdir()
        result = cli.install_skill(self.inst, "project", project, dry_run=False)
        self.assertEqual(result["path"], project / ".claude" / "skills" / "zhiji")
        self.assertTrue((result["path"] / "SKILL.md").is_file())
        shutil.rmtree(self.source)
        with self.assertRaises(cli.CliError):
            cli.install_skill(self.inst, "user", None, dry_run=False)
        self.assertFalse((self.home / ".claude").exists())

    def test_shipped_skill_has_agent_skills_frontmatter(self):
        text = (ROOT / "plugins" / "kyf.mcp" / "skill" / "zhiji" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        header = text.split("---\n", 2)[1]
        fields = dict(line.split(": ", 1) for line in header.strip().splitlines())
        self.assertEqual(fields["name"], "zhiji")  # must equal the folder name
        self.assertTrue(0 < len(fields["description"]) <= 1024)


class HookInstallTests(Sandbox):
    """--install-hook: merge-preserving, idempotent, backed-up UserPromptSubmit hook."""

    def setUp(self):
        super().setUp()
        root = self.tmp / "install"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "zhiji-events.py").write_text("# synthetic\n", encoding="utf-8")
        self.inst = cli.Installation(root, root / "runtime" / "python" / "python.exe", "test", False)
        self.path = self.home / ".claude" / "settings.json"
        self.original = {
            "model": "opus", "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-hook.exe"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}

    @staticmethod
    def legacy(inst, name):
        """The single-string entry older CLI versions wrote."""
        command, (script, _, root, *rest) = inst.hook_command(name)
        return {"type": "command", "command": f'"{command}" "{script}" --root "{root}" ' + " ".join(rest),
                "timeout": 30}

    def ours(self, document, name="claude-code"):
        return [hook for group in document["hooks"]["UserPromptSubmit"] for hook in group["hooks"]
                if cli._hook_is_ours(hook, name)]

    def test_merge_is_idempotent_backed_up_and_preserves_other_settings(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        dry = cli.install_hook(self.inst, "user", None, "claude-code", dry_run=True)
        self.assertTrue(dry["changed"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)
        first = cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False)
        self.assertTrue(first["changed"])
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({key: written[key] for key in ("model", "permissions")},
                         {key: self.original[key] for key in ("model", "permissions")})
        self.assertEqual(written["hooks"]["PreToolUse"], self.original["hooks"]["PreToolUse"])
        self.assertEqual(written["hooks"]["UserPromptSubmit"][0], self.original["hooks"]["UserPromptSubmit"][0])
        [hook] = self.ours(written)
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["timeout"], cli.HOOK_TIMEOUT)
        self.assertEqual(hook["command"], str(self.inst.python))
        self.assertEqual(hook["args"], [str(self.inst.events_script), "--root", str(self.inst.root),
                                        "--once", "--format", "claude-hook", "--profile", "claude-code",
                                        "--consumer", "claude-hook"])
        self.assertNotIn("zmc_", json.dumps(hook))
        self.assertEqual(len(self.backups(self.path)), 1)
        self.assertEqual(json.loads(self.backups(self.path)[0].read_text(encoding="utf-8")), self.original)
        again = cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False)
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.backups(self.path)), 1)
        # A moved installation replaces the one entry in place; a second connection gets its own.
        moved_root = self.tmp / "搬迁 $目录's copy"
        moved = cli.Installation(moved_root, moved_root / "python.exe", "test", False)
        (moved.root / "scripts").mkdir(parents=True)
        (moved.root / "scripts" / "zhiji-events.py").write_text("#\n", encoding="utf-8")
        cli.install_hook(moved, "user", None, "claude-code", dry_run=False)
        cli.install_hook(moved, "user", None, "work", dry_run=False)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        [hook] = self.ours(written)
        self.assertEqual(hook["args"][0], str(moved.events_script))
        self.assertEqual(len(self.ours(written, "work")), 1)
        self.assertEqual(len(written["hooks"]["UserPromptSubmit"]), 3)
        # Removal takes out exactly this connection's entry.
        self.assertTrue(cli.remove_hook("user", None, "claude-code", dry_run=True)["changed"])
        cli.remove_hook("user", None, "claude-code", dry_run=False)
        cli.remove_hook("user", None, "work", dry_run=False)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)
        self.assertFalse(cli.remove_hook("user", None, "work", dry_run=False)["changed"])

    def test_fresh_project_scope_and_full_removal(self):
        project = self.tmp / "proj"
        project.mkdir()
        result = cli.install_hook(self.inst, "project", project, "claude-code", dry_run=False)
        self.assertEqual(result["path"], project / ".claude" / "settings.local.json")
        self.assertFalse(result["path"].with_name("settings.json").exists())
        self.assertIsNone(result["backup"])
        self.assertEqual(len(self.ours(json.loads(result["path"].read_text(encoding="utf-8")))), 1)
        cli.remove_hook("project", project, "claude-code", dry_run=False)
        self.assertEqual(json.loads(result["path"].read_text(encoding="utf-8")), {})
        self.assertFalse(self.path.exists())

    def test_project_migrates_only_verified_legacy_hooks_and_keeps_added_fields(self):
        project = self.tmp / "proj"
        local = project / ".claude" / "settings.local.json"
        shared = local.with_name("settings.json")
        shared.parent.mkdir(parents=True)
        legacy = {**self.legacy(self.inst, "claude-code"), "once": True}
        customized = {**legacy, "timeout": 31}
        other_root = self.tmp / "other-install"
        moved = self.legacy(cli.Installation(other_root, other_root / "python.exe", "test", False), "claude-code")
        shell_wrapper = {**legacy, "command": "echo " + legacy["command"]}
        other_profile = self.legacy(self.inst, "work")
        other_format = {**legacy, "command": legacy["command"].replace("--format claude-hook", "--format json")}
        preserved = [shell_wrapper, other_profile, other_format, moved]
        original = {"theme": "dark", "hooks": {"UserPromptSubmit": [
            {"hooks": [legacy, *preserved]}, {"hooks": [customized]}]}}
        shared.write_text(json.dumps(original), encoding="utf-8")
        local.write_text('{"model": "opus"}', encoding="utf-8")
        dry = cli.install_hook(self.inst, "project", project, "claude-code", dry_run=True)
        self.assertEqual([item["path"] for item in dry["edited"]], [local, shared])
        self.assertEqual(json.loads(shared.read_text(encoding="utf-8")), original)
        self.assertEqual(json.loads(local.read_text(encoding="utf-8")), {"model": "opus"})
        result = cli.install_hook(self.inst, "project", project, "claude-code", dry_run=False)
        self.assertEqual([item["path"] for item in result["edited"]], [local, shared])
        migrated = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual(migrated["model"], "opus")
        [hook] = self.ours(migrated)
        self.assertIs(hook["once"], True)
        expected = {"theme": "dark", "hooks": {"UserPromptSubmit": [{"hooks": preserved}]}}
        self.assertEqual(json.loads(shared.read_text(encoding="utf-8")), expected)
        for item in result["edited"]:
            self.assertTrue(item["backup"].is_relative_to(self.home))
        self.assertFalse(cli.install_hook(self.inst, "project", project, "claude-code", dry_run=False)["changed"])
        cli.remove_hook("project", project, "claude-code", dry_run=False, inst=self.inst)
        self.assertEqual(json.loads(local.read_text(encoding="utf-8")), {"model": "opus"})
        self.assertEqual(json.loads(shared.read_text(encoding="utf-8")), expected)

    def test_stale_entry_from_moved_install_is_replaced_in_place_and_removed(self):
        # Legacy shell hooks need a recorded install. Marked exec hooks can move independently.
        old_root = self.tmp / "旧 安装"
        old = cli.Installation(old_root, old_root / "runtime" / "python" / "python.exe", "test", False)
        command, args = old.hook_command("claude-code")
        store = mock.Mock()
        store.profile_info.return_value = {"client": "claude-code", "root": str(old_root)}
        previous = cli._saved_hook_command(store, "claude-code")
        self.assertEqual(previous, (command, args))
        forms = {
            "string": (self.legacy(old, "claude-code"), previous),
            "args": ({"type": "command", "command": command, "args": args, "timeout": 99,
                      "statusMessage": cli.HOOK_STATUS}, None),
        }
        other = {"type": "command", "command": "other-hook.exe"}
        for form, (stale, known) in forms.items():
            with self.subTest(form=form):
                document = {"hooks": {"UserPromptSubmit": [{"hooks": [other, stale]}]}}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(document), encoding="utf-8")
                self.assertTrue(cli._hook_is_ours(stale, "claude-code", [known] if known else []))
                self.assertFalse(cli._hook_is_ours(stale, "work"))
                cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False, previous=known)
                [group] = json.loads(self.path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
                self.assertEqual(len(group["hooks"]), 2)
                self.assertEqual(group["hooks"][0], other)
                self.assertEqual(group["hooks"][1]["command"], str(self.inst.python))
                self.assertEqual(group["hooks"][1]["args"][0], str(self.inst.events_script))
                self.assertEqual(group["hooks"][1]["timeout"], cli.HOOK_TIMEOUT)
                self.path.write_text(json.dumps(document), encoding="utf-8")
                self.assertTrue(cli.remove_hook("user", None, "claude-code", dry_run=False,
                                                previous=known)["changed"])
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                                 {"hooks": {"UserPromptSubmit": [{"hooks": [other]}]}})

    def test_unrelated_hooks_are_not_ours(self):
        command, args = self.inst.hook_command("claude-code")
        base = {"type": "command", "command": command, "args": args, "statusMessage": cli.HOOK_STATUS}
        for hook in ({**base, "type": "prompt"},
                     {**base, "args": [arg.replace("zhiji-events.py", "other.py") for arg in args]},
                     {**base, "args": [arg if arg != "claude-hook" else "json" for arg in args[:6]] + args[6:]},
                     {**base, "args": args[:-2] + ["--consumer", "someone-else"]},
                     {**base, "args": args[:-2]},
                     {**base, "args": [*args[:3], "--follow", *args[4:]]},
                     {**base, "args": [*args, "--profile", "work"]},
                     {**base, "command": str(self.tmp / "echo.exe")},
                     {**base, "args": "not-a-list"},
                     {"type": "command", "command": "wrapper.exe " + subprocess.list2cmdline([command, *args])},
                     {"type": "command", "command": 'unbalanced "quote'}, "string-hook", None):
            with self.subTest(hook=hook):
                self.assertFalse(cli._hook_is_ours(hook, "claude-code", [(command, args)]))

    def test_custom_commands_survive_connect_and_disconnect_in_same_group(self):
        command, args = self.inst.hook_command("claude-code")
        legacy = self.legacy(self.inst, "claude-code")
        custom = [
            {"type": "command", "command": f'echo "{args[0]}" ' + " ".join(args[3:])},
            {"type": "command", "command": command, "args": args},
            {"type": "command", "command": command, "args": [*args[:3], "--follow", *args[4:]]},
            {**legacy, "command": legacy["command"] + " && echo done"},
            {"type": "command", "command": command, "args": [*args, "--profile", "other"]},
        ]
        original = {"hooks": {"UserPromptSubmit": [{"hooks": custom}]}}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(original), encoding="utf-8")
        cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["hooks"]["UserPromptSubmit"][0]["hooks"], custom)
        self.assertEqual(len(self.ours(written)), 1)
        cli.remove_hook("user", None, "claude-code", dry_run=False, inst=self.inst)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)

    def test_user_added_fields_are_recognized_and_preserved(self):
        # P2-2: an extra field must not hide our entry from connect or disconnect.
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        [hook] = self.ours(written)
        hook["once"] = True
        hook["timeout"] = 5
        self.path.write_text(json.dumps(written), encoding="utf-8")
        moved_root = self.tmp / "moved"
        moved = cli.Installation(moved_root, moved_root / "python.exe", "test", False)
        (moved.root / "scripts").mkdir(parents=True)
        (moved.root / "scripts" / "zhiji-events.py").write_text("#\n", encoding="utf-8")
        cli.install_hook(moved, "user", None, "claude-code", dry_run=False)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        [hook] = self.ours(written)
        self.assertIs(hook["once"], True)
        self.assertEqual(hook["timeout"], cli.HOOK_TIMEOUT)
        self.assertEqual(hook["args"][0], str(moved.events_script))
        self.assertEqual(len(written["hooks"]["UserPromptSubmit"]), 2)
        self.assertFalse(cli.install_hook(moved, "user", None, "claude-code", dry_run=False)["changed"])
        self.assertTrue(cli.remove_hook("user", None, "claude-code", dry_run=False)["changed"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)

    def test_unmergeable_settings_are_refused_untouched(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{broken", "[]", '{"hooks": []}', '{"hooks": null}', '{"hooks": {"UserPromptSubmit": {}}}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(cli.CliError) as caught:
                    cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False)
                self.assertEqual(caught.exception.code, cli.EXIT_CONFIG)
                self.assertEqual(self.path.read_text(encoding="utf-8"), text)
        self.assertEqual(self.backups(self.path), [])

    def test_missing_events_script_is_reported(self):
        (self.inst.root / "scripts" / "zhiji-events.py").unlink()
        with self.assertRaises(cli.CliError):
            cli.install_hook(self.inst, "user", None, "claude-code", dry_run=False)
        self.assertFalse(self.path.exists())


class StagingTests(unittest.TestCase):
    def test_install_ships_the_cli_helper_pairing_window_and_its_bridge_modules(self):
        spec = importlib.util.spec_from_file_location("stage_for_cli_test", SCRIPTS / "stage-real-client.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        for name in ("zhiji-mcp-cli.py", "zhiji_mcp_credentials.py", "zhiji_mcp_http.py", "pairing-window.cjs",
                     "pairing-window-preload.cjs", "ui-host.cjs", "zhiji-events.py"):
            self.assertIn(name, stage.SCRIPTS)
            self.assertTrue((SCRIPTS / name).is_file(), name)
        for name in ("core_mcp_pairing.py", "core_mcp_pairing_window.py", "core_mcp_events.py"):
            self.assertIn(name, stage.BRIDGE)
            self.assertTrue((ROOT / "bridge" / name).is_file(), name)
        # The optional Agent Skill ships inside the kyf.mcp plugin tree (where --install-skill reads it).
        shipped = {relative.as_posix() for _source, relative, _base in stage.public_mappings(ROOT)}
        self.assertIn("plugins/kyf.mcp/skill/zhiji/SKILL.md", shipped)


# -- end to end ---------------------------------------------------------------------------

FAKE_ADAPTER = r'''
import json, os, sys
from pathlib import Path
from urllib import request
sys.path.insert(0, str(Path(__file__).resolve().parent))
from zhiji_mcp_credentials import load_adapter_credentials
argv = sys.argv[1:]
profile = argv[argv.index("--profile") + 1]
client_id, credential = load_adapter_credentials(profile)
url = os.environ["ZHIJI_CORE_URL"]
body = json.dumps({"clientId": client_id, "credential": credential, "pid": os.getpid()}).encode()
req = request.Request(url + "/api/mcp/handshake", data=body, headers={"Content-Type": "application/json"})
request.urlopen(req, timeout=10).read()
results = {"zhiji_ping": {"service": "zhiji-mcp", "profile": {"mode": "accounts", "capabilities": {}}},
           "zhiji_list_accounts": {"accounts": [{"accountId": "acct-a"}]}}
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "zhiji-mcp"}, "capabilities": {}}
    elif method == "tools/list":
        result = {"tools": [{"name": "zhiji_ping"}, {"name": "zhiji_list_accounts"}]}
    else:
        payload = results[message["params"]["name"]]
        result = {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


class FakeCore:
    """Real McpServices + pairing service behind a loopback HTTP server."""

    def __init__(self, directory, presenter):
        self.mcp = McpServices(directory, source_fingerprint=lambda: "fp")
        self.mcp.directory = lambda: {"accounts": [{"accountId": "acct-a", "label": "合成账号甲"}]}
        self.mcp.pairing = McpPairingService(self.mcp, presenter)
        self.requests = []
        core = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                core.requests.append(self.path)
                try:
                    if self.path == "/api/mcp/handshake":
                        result = core.mcp.handshake(body)
                    else:
                        result = handle_pairing_path(core.mcp, "POST", self.path, body)
                    status = 200
                except McpError as exc:
                    status, result = exc.status, {"error": exc.code, "message": exc.message}
                data = json.dumps(result).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.mcp.close()


def approve(payload, on_shown):
    on_shown()
    time.sleep(0.3)
    return {"approved": True, "accounts": ["acct-a"], "capabilities": {}}


class EndToEndTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.install = self.tmp / "install"
        (self.install / "bridge").mkdir(parents=True)
        (self.install / "bridge" / "chat_server.py").write_text("# synthetic\n", encoding="utf-8")
        (self.install / "scripts").mkdir()
        (self.install / "scripts" / "mcp-stdio-adapter.py").write_text(FAKE_ADAPTER, encoding="utf-8")
        shutil.copy2(SCRIPTS / "zhiji_mcp_credentials.py", self.install / "scripts" / "zhiji_mcp_credentials.py")
        for name in ("zhiji-events.py", "start-real-client.py", "zhiji_mcp_http.py"):
            shutil.copy2(SCRIPTS / name, self.install / "scripts" / name)
        for name in ("core_runtime.py", "instance_identity.py"):
            shutil.copy2(ROOT / "bridge" / name, self.install / "bridge" / name)
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        super().tearDown()

    def core(self, presenter):
        core = FakeCore(self.tmp / f"core{len(self.cores)}", presenter)
        self.cores.append(core)
        os.environ[cli.CORE_URL_ENV] = core.url
        self.addCleanup(os.environ.pop, cli.CORE_URL_ENV, None)
        return core

    def run_cli(self, *command):
        completed = subprocess.run([sys.executable, str(SCRIPTS / "zhiji-mcp-cli.py"), *command, "--root",
                                    str(self.install), "--dev"], capture_output=True, text=True,
                                   encoding="utf-8", env={**os.environ}, timeout=120)
        return completed.returncode, completed.stdout + completed.stderr

    def test_connect_pairs_writes_verifies_then_reuses_repairs_and_disconnects(self):
        core = self.core(approve)
        code, output = self.run_cli("connect", "--client", "cursor", "--wait", "30")
        self.assertEqual(code, 0, output)
        for marker in ("安装：就绪", "授权：已确认", "接入：成功", "授权范围：按账号读取：合成账号甲", "专项模型", "重启："):
            self.assertIn(marker, output)
        config = json.loads((self.home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(config["mcpServers"]["zhiji"]["args"][-2:], ["--profile", "cursor"])
        profiles = core.mcp.store.list_profiles()
        self.assertEqual(len(profiles), 1)
        creds = (self.tmp / "creds" / "cursor.json").read_text(encoding="utf-8")
        self.assertNotIn("zmc_", creds + output + json.dumps(config))

        # Re-run: the saved credential still handshakes, so no new pairing and no new profile.
        before = len([path for path in core.requests if path.endswith("/request")])
        code, output = self.run_cli("connect", "--client", "cursor")
        self.assertEqual(code, 0, output)
        self.assertIn("沿用已有配对", output)
        self.assertEqual(len([path for path in core.requests if path.endswith("/request")]), before)
        self.assertEqual(len(core.mcp.store.list_profiles()), 1)

        # The credential was regenerated in Zhiji: connect repairs the same profile.
        client_id = profiles[0]["clientId"]
        core.mcp.store.regenerate_credential(client_id)
        code, output = self.run_cli("connect", "--client", "cursor", "--wait", "30")
        self.assertEqual(code, 0, output)
        self.assertIn("已修复原有档案", output)
        self.assertEqual([item["clientId"] for item in core.mcp.store.list_profiles()], [client_id])

        code, output = self.run_cli("status")
        self.assertEqual(code, 0, output)
        self.assertIn("凭据有效", output)
        code, output = self.run_cli("doctor")
        self.assertEqual(code, 0, output)
        self.assertIn("验证通过", output)

        code, output = self.run_cli("disconnect", "--client", "cursor")
        self.assertEqual(code, 0, output)
        self.assertNotIn("zhiji", json.loads((self.home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
                         ["mcpServers"])
        self.assertEqual(core.mcp.store.list_profiles(), [])
        self.assertFalse((self.tmp / "creds" / "cursor.json").exists())

    def test_denied_pairing_writes_nothing(self):
        self.core(lambda payload, on_shown: {"approved": False})
        code, output = self.run_cli("connect", "--client", "codex", "--wait", "30")
        self.assertEqual(code, cli.EXIT_NOT_PAIRED, output)
        self.assertIn("拒绝", output)
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        self.assertFalse((self.tmp / "creds" / "codex.json").exists())

    def test_no_desktop_guides_to_settings_and_completes_after_approval_there(self):
        from core_mcp_pairing import PairingUnavailable

        def unavailable(payload, on_shown):
            raise PairingUnavailable("no desktop")
        core = self.core(unavailable)
        # Approve in the settings page only after the CLI has printed its guidance: approving
        # earlier is legitimate (the CLI then never needs to guide) but would not test the guide.
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "zhiji-mcp-cli.py"), "connect", "--client",
                                    "deepseek-harness", "--wait", "40", "--root", str(self.install), "--dev"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={**os.environ})
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        lines = []
        for raw in iter(process.stdout.readline, b""):
            lines.append(raw.decode("utf-8"))
            if "设置 → 插件 → MCP" in lines[-1]:
                [pending] = [item for item in core.mcp.pairing.pending() if item["ui"] == "unavailable"]
                core.mcp.pairing.decide(pending["pairingId"], True, ["acct-a"], {"write": True})
                break
        lines.append(process.stdout.read().decode("utf-8"))
        code, output = process.wait(timeout=120), "".join(lines)
        self.assertEqual(code, 0, output)
        self.assertIn("设置 → 插件 → MCP", output)
        self.assertIn("修改资料与发起分析", output)
        self.assertIn("serverName: zhiji", (self.home / ".dsh" / "cordis.patch.yml").read_text(encoding="utf-8"))

    def test_generic_prints_snippet_and_marks_config_incomplete(self):
        self.core(approve)
        code, output = self.run_cli("connect", "--client", "generic", "--wait", "30")
        self.assertEqual(code, 0, output)
        self.assertIn("未完成客户端配置", output)
        self.assertIn('"mcpServers"', output)

    def test_install_skill_for_claude_code_and_refused_for_other_clients(self):
        skill = self.install / "plugins" / "kyf.mcp" / "skill" / "zhiji"
        skill.mkdir(parents=True)
        shutil.copy2(ROOT / "plugins" / "kyf.mcp" / "skill" / "zhiji" / "SKILL.md", skill / "SKILL.md")
        core = self.core(approve)
        code, output = self.run_cli("connect", "--client", "codex", "--install-skill")
        self.assertEqual(code, cli.EXIT_ERROR, output)
        self.assertEqual(core.requests, [])
        code, output = self.run_cli("connect", "--client", "claude-code", "--install-skill", "--wait", "30")
        self.assertEqual(code, 0, output)
        self.assertIn("Skill：已安装", output)
        installed = self.home / ".claude" / "skills" / "zhiji" / "SKILL.md"
        self.assertEqual(installed.read_bytes(), (skill / "SKILL.md").read_bytes())
        code, output = self.run_cli("connect", "--client", "claude-code", "--install-skill")
        self.assertEqual(code, 0, output)
        self.assertIn("已是最新", output)

    def test_install_hook_for_claude_code_runs_and_disconnect_removes_it(self):
        core = self.core(approve)
        code, output = self.run_cli("connect", "--client", "codex", "--install-hook")
        self.assertEqual(code, cli.EXIT_ERROR, output)
        self.assertEqual(core.requests, [])
        code, output = self.run_cli("connect", "--client", "claude-code", "--install-hook", "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("（演练）钩子命令：", output)
        settings = self.home / ".claude" / "settings.json"
        self.assertFalse(settings.exists())
        code, output = self.run_cli("connect", "--client", "claude-code", "--install-hook", "--wait", "30")
        self.assertEqual(code, 0, output)
        self.assertIn("新消息钩子：已写入", output)
        [group] = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
        entry = group["hooks"][0]
        self.assertEqual(entry["args"][-4:], ["--profile", "claude-code", "--consumer", "claude-hook"])
        # Execute the registered argv, as Claude Code does. The synthetic install's Core is down.
        hook = subprocess.run([entry["command"], *entry["args"]], capture_output=True, timeout=60,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual((hook.returncode, hook.stdout), (0, b""), hook.stderr.decode("utf-8", "replace"))
        self.assertIn("知己未运行", hook.stderr.decode("utf-8"))
        code, output = self.run_cli("connect", "--client", "claude-code", "--install-hook")
        self.assertEqual(code, 0, output)
        self.assertIn("已是最新", output)
        code, output = self.run_cli("disconnect", "--client", "claude-code")
        self.assertEqual(code, 0, output)
        self.assertIn("移除知己新消息钩子", output)
        self.assertNotIn("hooks", json.loads(settings.read_text(encoding="utf-8")))

    def test_dry_run_starts_and_writes_nothing(self):
        core = self.core(approve)
        code, output = self.run_cli("connect", "--client", "claude-code", "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("演练", output)
        self.assertEqual(core.requests, [])
        self.assertFalse((self.home / ".claude.json").exists())


if __name__ == "__main__":
    unittest.main()
