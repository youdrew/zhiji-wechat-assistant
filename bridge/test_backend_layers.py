"""Synthetic regression tests for Core module boundaries.

Modified by Know Your Friends, 2026-10-13 (Apache-2.0): Core contains no analysis
pipeline; the retired label and portrait modules must stay absent, and 知己分析 ships as a
bundled plugin together with the plugin SDK.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import backend_contracts
import model_source
from core_mcp_confirm import ConfirmUnavailable

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
CORE_MODULES = ("backend_contracts", "backend_service", "message_input", "wechat_source",
                "legacy_portrait", "model_probe", "core_runtime", "core_services", "plugin_api", "core_http")
# Modified by Know Your Friends, 2026-10-27 (phase 6): the MCP runtime must ship as a
# complete closure -- every core_mcp_* module, the shared leases, the stdio adapter and
# the on-demand confirm window, with every bridge module they newly import.
MCP_RUNTIME_MODULES = ("core_mcp_proxy", "core_mcp_store", "core_mcp_admin", "core_mcp_offline",
                       "core_mcp_tasks", "core_mcp_assistant", "core_mcp_import",
                       "core_mcp_manage", "core_mcp_confirm", "core_mcp_confirm_window", "core_leases")
RETIRED_MODULES = ("api_portrait_ledger", "api_portrait_statistics", "api_tasks", "batch_engine", "batch_state",
                   "message_results", "node_analysis", "local_model_source", "model_bundle", "model_install",
                   "portrait_contracts", "profile_state", "real_backend", "result_store", "message_contracts")
RETIRED_ADVISOR_MODULES = ("advisor_service", "advisor_runtime", "advisor_store", "advisor_http",
                           "advisor_sessions", "advisor_thread_store", "advisor_catalog_store",
                           "advisor_companion", "reply_suggestions")
# Moved into plugins/kyf.friend-analysis/backend (2026-10-13).
PLUGIN_MODULES = ("friend_analysis", "friend_chat", "friend_extract", "friend_overview", "friend_pages",
                  "friend_persona", "friend_run_store", "friend_trends", "profile_signals")


def parsed(name):
    return ast.parse((BRIDGE / (name + ".py")).read_text(encoding="utf-8"))


def imports(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


class LayerBoundaryTests(unittest.TestCase):
    def test_retired_analysis_modules_are_absent_and_unreferenced(self):
        for name in RETIRED_MODULES + RETIRED_ADVISOR_MODULES:
            with self.subTest(module=name):
                self.assertFalse((BRIDGE / (name + ".py")).exists())
        self.assertFalse((BRIDGE / "analysis_server.ts").exists())
        for path in BRIDGE.glob("*.py"):
            with self.subTest(module=path.name):
                self.assertFalse(imports(ast.parse(path.read_text(encoding="utf-8"))) &
                                 set(RETIRED_MODULES + RETIRED_ADVISOR_MODULES))

    def test_plugin_backends_do_not_import_private_core_modules(self):
        core_modules = {path.stem for path in BRIDGE.glob("*.py") if not path.name.startswith("test_")}
        for backend in (ROOT / "plugins").glob("*/backend"):
            own_modules = {path.stem for path in backend.glob("*.py")}
            for path in backend.glob("*.py"):
                with self.subTest(plugin=backend.parent.name, module=path.name):
                    dependencies = imports(ast.parse(path.read_text(encoding="utf-8")))
                    self.assertFalse((dependencies - own_modules) & (core_modules | {"bridge"}))

    def test_contracts_have_no_application_layer_dependencies(self):
        self.assertFalse(imports(parsed("backend_contracts")) - sys.stdlib_module_names - {"__future__"})
        self.assertIs(model_source.ModelSourceUnavailable, backend_contracts.ModelSourceUnavailable)
        self.assertEqual(model_source.LOCAL_SOURCE_ID, backend_contracts.LOCAL_SOURCE_ID)

    def test_message_input_depends_only_on_the_standard_library(self):
        found = imports(parsed("message_input"))
        self.assertTrue(found <= sys.stdlib_module_names | {"__future__"})

    def test_core_service_issues_no_sql_and_defines_no_prompts(self):
        tree = parsed("backend_service")
        self.assertNotIn("sqlite3", imports(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"connect", "execute", "executemany", "executescript"})
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotRegex(node.value, r"(?i)\b(?:SELECT .+ FROM|INSERT INTO|UPDATE \w+ SET|CREATE TABLE|DELETE FROM)\b")
                self.assertNotRegex(node.value, r"(?i)you are a|system prompt|请分析")

    def test_legacy_portrait_reader_never_writes(self):
        source = (BRIDGE / "legacy_portrait.py").read_text(encoding="utf-8")
        self.assertIn("mode=ro", source)
        self.assertNotRegex(source, r"(?i)\b(?:INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b")

    def test_layer_imports_have_no_process_database_or_network_side_effects(self):
        code = """
import importlib, socket, sqlite3, subprocess, sys, threading
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch.object(sqlite3, 'connect', side_effect=AssertionError('database on import')), \
     patch.object(subprocess, 'Popen', side_effect=AssertionError('process on import')), \
     patch.object(threading.Thread, 'start', side_effect=AssertionError('thread on import')), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network on import')):
    for name in ('backend_contracts', 'backend_service', 'wechat_source', 'legacy_portrait', 'model_probe',
                 'core_runtime', 'message_input', 'real_http'):
        importlib.import_module(name)
print('IMPORTS_HAVE_NO_RUNTIME_SIDE_EFFECTS')
"""
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(BRIDGE)],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("IMPORTS_HAVE_NO_RUNTIME_SIDE_EFFECTS", completed.stdout)

    def test_packaged_bridge_includes_and_imports_core_without_source_tree(self):
        spec = importlib.util.spec_from_file_location("stage_for_layer_test", ROOT / "scripts/stage-real-client.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        self.assertTrue({name + ".py" for name in CORE_MODULES} <= set(stage.BRIDGE))
        self.assertFalse({name + ".py" for name in RETIRED_MODULES + PLUGIN_MODULES} & set(stage.BRIDGE))
        with tempfile.TemporaryDirectory(prefix="kyf-layer-stage-") as temporary:
            target = Path(temporary) / "client" / "bridge"
            target.mkdir(parents=True)
            for name in stage.BRIDGE:
                if name.endswith(".py"):
                    shutil.copy2(BRIDGE / name, target / name)
            code = """
import importlib, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import chat_server, real_http
for name in %r:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().parent == root, name
print('STAGED_BRIDGE_IMPORTS_OK')
""" % (CORE_MODULES,)
            completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(target)],
                                       cwd=temporary, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("STAGED_BRIDGE_IMPORTS_OK", completed.stdout)

    def test_mcp_runtime_stages_complete_and_imports_without_the_source_tree(self):
        spec = importlib.util.spec_from_file_location("stage_for_mcp_test", ROOT / "scripts/stage-real-client.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        # The MCP runtime modules and its entry scripts are explicit staging entries.
        self.assertTrue({name + ".py" for name in MCP_RUNTIME_MODULES} <= set(stage.BRIDGE))
        for name in ("mcp-stdio-adapter.py", "confirm-window.cjs", "confirm-window-preload.cjs"):
            self.assertIn(name, stage.SCRIPTS)
            self.assertTrue((ROOT / "scripts" / name).is_file())
        # The smoke harness is a test, never a shipped component.
        self.assertNotIn("confirm-window-smoke.cjs", stage.SCRIPTS)
        # The staged closure is closed: no staged bridge module imports a bridge module
        # that staging does not list (catches newly referenced existing modules).
        staged = {name[:-3] for name in stage.BRIDGE if name.endswith(".py")}
        for name in [entry for entry in stage.BRIDGE if entry.endswith(".py")]:
            dependencies = imports(ast.parse((BRIDGE / name).read_text(encoding="utf-8")))
            self.assertFalse((dependencies & {path.stem for path in BRIDGE.glob("*.py")}) - staged,
                             f"staged module {name} depends on unstaged bridge modules")
        # The whole kyf.mcp plugin ships (manifest, backend, UI), still without tests,
        # with the root README; project documents are local-only.
        public = {relative.as_posix() for _, relative, _ in stage.public_mappings(ROOT)}
        self.assertIn("README.md", public)
        self.assertFalse(any(name.casefold().startswith("docs/") for name in public))
        for name in ("plugins/kyf.mcp/plugin.json", "plugins/kyf.mcp/backend/main.py",
                     "plugins/kyf.mcp/backend/mcp_admin.py", "plugins/kyf.mcp/ui/index.html",
                     "plugins/kyf.mcp/ui/mcp.js", "plugins/kyf.mcp/ui/mcp.css"):
            self.assertIn(name, public)
        self.assertFalse(any(name.startswith("plugins/kyf.mcp/tests/") for name in public))
        self.assertFalse(any("__pycache__" in name for name in public))
        # The staged modules import from the staged directory alone.
        with tempfile.TemporaryDirectory(prefix="kyf-mcp-stage-") as temporary:
            target = Path(temporary) / "client" / "bridge"
            target.mkdir(parents=True)
            for name in stage.BRIDGE:
                if name.endswith(".py"):
                    shutil.copy2(BRIDGE / name, target / name)
            code = """
import importlib, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
for name in %r:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().parent == root, name
print('STAGED_MCP_IMPORTS_OK')
""" % (list(MCP_RUNTIME_MODULES),)
            completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(target)],
                                       cwd=temporary, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("STAGED_MCP_IMPORTS_OK", completed.stdout)

    def test_on_demand_component_paths_resolve_in_the_install_layout(self):
        """确认窗等按需组件在安装布局（scripts/、node_modules/）下按安装根解析。"""
        from core_runtime import electron_executable
        from core_mcp_confirm_window import ElectronConfirmPresenter
        with tempfile.TemporaryDirectory(prefix="kyf-install-layout-") as temporary:
            root = Path(temporary)
            script = root / "scripts" / "confirm-window.cjs"
            script.parent.mkdir(parents=True)
            script.write_text("// staged marker", encoding="utf-8")
            electron = root / "node_modules" / "electron" / "dist" / "electron.exe"
            electron.parent.mkdir(parents=True)
            electron.write_bytes(b"MZsynthetic")
            self.assertEqual(electron_executable(root), str(electron))
            # The presenter defaults to exactly these two staged locations.
            presenter = ElectronConfirmPresenter(root, "http://127.0.0.1:9")
            self.assertEqual(presenter.root, root)
            self.assertIsNone(presenter._script)  # falls back to root/scripts/confirm-window.cjs
            self.assertIsNone(presenter._electron)  # falls back to electron_executable(root)
            # Without the bundled shell the presenter fails closed instead of guessing.
            self.assertEqual(electron_executable(root / "nowhere"), None)
            empty = ElectronConfirmPresenter(root / "nowhere", "http://127.0.0.1:9")
            with self.assertRaises(ConfirmUnavailable):
                empty._spawn({"confirmId": "c", "expiresAtMs": 0})

    def test_analysis_modules_live_in_their_plugin_and_ship_with_the_sdk(self):
        for name in PLUGIN_MODULES:
            with self.subTest(module=name):
                self.assertFalse((BRIDGE / (name + ".py")).exists())
                self.assertTrue((ROOT / "plugins/kyf.friend-analysis/backend" / (name + ".py")).is_file())
        for path in BRIDGE.glob("*.py"):
            with self.subTest(module=path.name):
                self.assertFalse(imports(ast.parse(path.read_text(encoding="utf-8"))) & set(PLUGIN_MODULES))
        spec = importlib.util.spec_from_file_location("stage_for_plugin_test", ROOT / "scripts/stage-real-client.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        staged = {relative.as_posix() for _, relative, _ in stage.public_mappings(ROOT)}
        self.assertTrue({"plugin-sdk/python/launch.py", "plugin-sdk/python/kyf_plugin/__init__.py",
                         "plugin-sdk/web/kyf-host.js"} <= staged)
        self.assertFalse({name for name in staged if "/tests/" in name or "__pycache__" in name})
        manifests = sorted((ROOT / "plugins").glob("*/plugin.json"))
        self.assertTrue(manifests)
        for manifest in manifests:
            folder, data = manifest.parent.relative_to(ROOT).as_posix(), json.loads(manifest.read_text(encoding="utf-8"))
            with self.subTest(plugin=data["id"]):
                needed = {"plugin.json", data["runtime"]["entry"]}
                for slot in ("schemes", "chatToolbar", "nav"):
                    needed |= {item["page"] for item in data.get("contributes", {}).get(slot, []) if item.get("page")}
                self.assertTrue({folder + "/" + name for name in needed} <= staged)


if __name__ == "__main__":
    unittest.main()
