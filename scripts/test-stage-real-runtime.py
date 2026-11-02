"""Offline staging gates with synthetic packages; no model weights or private data."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SPEC = importlib.util.spec_from_file_location("stage_real_runtime", Path(__file__).with_name("stage-real-runtime.py"))
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


class RetrievalRuntimeGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-runtime-probe-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "client"
        self.client.mkdir()
        patcher = mock.patch.object(stage, "STAGE", self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_probes_use_bundled_interpreters_and_isolate_the_build_environment(self):
        with mock.patch.dict(os.environ, {"NODE_OPTIONS": "PRIVATE_NODE_OPTION", "NODE_PATH": "PRIVATE_PATH",
                                         "PYTHONPATH": "PRIVATE_PYTHON_PATH", "ANTHROPIC_AUTH_TOKEN": "PRIVATE_TOKEN"}), \
                mock.patch.object(stage.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            stage.verify_staged_retrieval_imports()
        self.assertEqual(run.call_count, 3)
        for call, name in zip(run.call_args_list, ("onnxruntime-node", "@huggingface/tokenizers", "numpy")):
            command, = call.args
            expected = self.client / "runtime" / ("python/python.exe" if name == "numpy" else "node/node.exe")
            self.assertEqual(command[0], str(expected))
            self.assertEqual(call.kwargs["cwd"], self.client)
            self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertNotIn("PRIVATE", repr(call))
            self.assertNotIn("NODE_PATH", call.kwargs["env"])
            self.assertNotIn("PYTHONPATH", call.kwargs["env"])
        self.assertEqual(run.call_args_list[-1].args[0][1:4], ["-I", "-B", "-c"])

    def test_missing_native_binding_or_dependency_fails_at_the_specific_runtime(self):
        names = ("onnxruntime-node", "@huggingface/tokenizers", "numpy")
        for index, name in enumerate(names):
            with self.subTest(name=name), mock.patch.object(stage.subprocess, "run", side_effect=[
                    *[SimpleNamespace(returncode=0) for _ in range(index)], SimpleNamespace(returncode=1)]) as run:
                with self.assertRaisesRegex(RuntimeError, "^staged retrieval runtime cannot load: " + name + "$"):
                    stage.verify_staged_retrieval_imports()
                self.assertEqual(run.call_count, index + 1)

    def test_launch_and_timeout_errors_do_not_expose_provider_or_machine_details(self):
        for error in (OSError("PRIVATE_MACHINE_DETAIL"), subprocess.TimeoutExpired("PRIVATE_COMMAND", 30)):
            with self.subTest(error=type(error).__name__), mock.patch.object(stage.subprocess, "run", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    stage.verify_staged_retrieval_imports()
                self.assertEqual(str(caught.exception), "staged retrieval runtime cannot load: onnxruntime-node")
                self.assertTrue(caught.exception.__suppress_context__)

    def prepare_main(self, gate):
        calls = []
        fixtures = {
            "stage_python_stdlib": {"version": "3.14"}, "stage_python_packages": {"packages": []},
            "stage_node_runtime": {"version": "v24.11.1", "license": "fixture"},
            "stage_node_packages": {"packages": []}, "verify_staged_sdk_imports": None,
            "verify_staged_reader_imports": None,
            "verify_staged_ffmpeg_imports": None,
            "verify_staged_user_site_disabled": None,
            "stage_advisor_engine": {}, "staged_inventory": {}, "staged_hashes": [],
        }
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in fixtures.items():
            def invoke(*_args, _name=name, _value=value):
                calls.append(_name)
                return dict(_value) if isinstance(_value, dict) else _value
            stack.enter_context(mock.patch.object(stage, name, side_effect=invoke))

        def verify():
            calls.append("verify_staged_retrieval_imports")
            gate()
        stack.enter_context(mock.patch.object(stage, "verify_staged_retrieval_imports", side_effect=verify))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return calls

    def test_failed_gate_prevents_manifest_locks_and_further_packaging(self):
        def fail():
            raise RuntimeError("staged retrieval runtime cannot load: numpy")
        calls = self.prepare_main(fail)
        with self.assertRaisesRegex(RuntimeError, "numpy"):
            stage.main(["--output", str(self.client)])
        self.assertEqual(calls[-1], "verify_staged_retrieval_imports")
        for name in ("runtime-manifest.json", "python-requirements.lock.txt", "node-dependencies.lock.json"):
            self.assertFalse((self.root / name).exists())

    def test_successful_gate_runs_after_dependency_copy_and_before_manifest(self):
        calls = self.prepare_main(lambda: None)
        self.assertEqual(stage.main(["--output", str(self.client)]), 0)
        self.assertEqual(calls[:10], ["stage_python_stdlib", "stage_python_packages", "stage_node_runtime",
                                     "stage_node_packages", "verify_staged_sdk_imports",
                                     "verify_staged_retrieval_imports", "verify_staged_reader_imports", "verify_staged_ffmpeg_imports", "verify_staged_user_site_disabled",
                                     "stage_advisor_engine"])
        self.assertTrue((self.root / "runtime-manifest.json").is_file())


class ProbeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-runtime-isolation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "client"
        (self.client / "node_modules").mkdir(parents=True)
        self.node = shutil.which("node")

    @staticmethod
    def package(directory, name, code):
        target = directory / "node_modules" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.js").write_text(code, encoding="utf-8")
        return target

    def node_probe(self, name):
        if not self.node:
            self.skipTest("Node.js is unavailable")
        return subprocess.run([self.node, "-e", stage.RETRIEVAL_NODE_PROBE, name], cwd=self.client,
                              capture_output=True, timeout=30)

    def test_node_probe_loads_staged_cjs_exports_and_builtins(self):
        self.package(self.client, "@huggingface/tokenizers",
                     "require('node:path'); exports.Tokenizer = class {}; ")
        self.package(self.client, "onnxruntime-node", "exports.InferenceSession = {create(){}}; "
                     "exports.listSupportedBackends = () => [{name:'cpu'}];")
        for name in ("@huggingface/tokenizers", "onnxruntime-node"):
            with self.subTest(name=name):
                self.assertEqual(self.node_probe(name).returncode, 0)

    def test_node_probe_rejects_missing_dependency_hidden_by_parent_node_modules(self):
        self.package(self.root, "@huggingface/tokenizers", "exports.Tokenizer = class {};")
        self.assertNotEqual(self.node_probe("@huggingface/tokenizers").returncode, 0)

    def test_node_probe_rejects_transitive_dependency_outside_stage(self):
        self.package(self.root, "fake-native-helper", "exports.Tokenizer = class {};")
        self.package(self.client, "@huggingface/tokenizers", "module.exports = require('fake-native-helper');")
        self.assertNotEqual(self.node_probe("@huggingface/tokenizers").returncode, 0)

    def test_node_probe_rejects_native_load_failure_and_missing_cpu_backend(self):
        target = self.package(self.client, "onnxruntime-node", "module.exports = require('./broken.node');")
        (target / "broken.node").write_bytes(b"synthetic-invalid-native-binding")
        self.assertNotEqual(self.node_probe("onnxruntime-node").returncode, 0)
        (target / "index.js").write_text("exports.InferenceSession = {create(){}}; "
                                        "exports.listSupportedBackends = () => [];", encoding="utf-8")
        self.assertNotEqual(self.node_probe("onnxruntime-node").returncode, 0)

    def test_python_probe_checks_native_numpy_and_rejects_an_unbundled_import(self):
        for expected, success in ((stage.PYTHON_SITE, True), (self.client, False)):
            with self.subTest(success=success):
                probe = subprocess.run([sys.executable, "-I", "-B", "-c", stage.RETRIEVAL_PYTHON_PROBE, str(expected)],
                                       cwd=self.client, capture_output=True, timeout=30,
                                       env={**os.environ, "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2"})
                self.assertEqual(probe.returncode == 0, success)


class UserSiteGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-user-site-gate-")
        self.addCleanup(self.temporary.cleanup)
        self.client = Path(self.temporary.name) / "client"
        self.runtime = self.client / "runtime" / "python"
        patcher = mock.patch.object(stage, "STAGE", self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_runtime(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / "python.exe").write_bytes(b"fixture")
        (self.runtime / "python314._pth").write_text(".\nLib\nimport site\n", encoding="utf-8")

    def real_runtime(self):
        """A python.exe pinned by a ._pth file like the shipped one, reusing this Python's library."""
        base = Path(sys.base_prefix)
        dll = f"python{sys.version_info.major}{sys.version_info.minor}"
        names = ("python.exe", f"{dll}.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll")
        if os.name != "nt" or not all((base / name).is_file() for name in names[:2]):
            self.skipTest("needs a Windows Python installation to copy")
        self.runtime.mkdir(parents=True)
        for name in names:
            if (base / name).is_file():
                shutil.copy2(base / name, self.runtime / name)
        (self.runtime / f"{dll}._pth").write_text(f"{base / 'DLLs'}\n{base / 'Lib'}\nimport site\n",
                                                  encoding="utf-8")

    def test_probe_runs_the_client_flags_against_existing_user_sites(self):
        self.fake_runtime()
        seen = {}

        def run(command, **kwargs):
            environment = kwargs["env"]
            seen.update(command=command, environment=environment)
            version = f"Python{sys.version_info.major}{sys.version_info.minor}"
            self.assertTrue((Path(environment["APPDATA"]) / "Python" / version / "site-packages").is_dir())
            self.assertTrue((Path(environment["PYTHONUSERBASE"]) / version / "site-packages").is_dir())
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"PYTHONPATH": "PRIVATE_PYTHON_PATH", "ANTHROPIC_AUTH_TOKEN": "PRIVATE_TOKEN"}), \
                mock.patch.object(stage.subprocess, "run", side_effect=run):
            stage.verify_staged_user_site_disabled()
        user = seen["command"][-1]
        self.assertEqual(seen["command"], [str(self.runtime / "python.exe"), "-s", "-B", "-c",
                                           stage.USER_SITE_PROBE, user])
        self.assertTrue(Path(seen["environment"]["APPDATA"]).is_relative_to(user))
        self.assertTrue(Path(seen["environment"]["PYTHONUSERBASE"]).is_relative_to(user))
        self.assertNotIn("PRIVATE", repr(seen["environment"]))
        self.assertFalse(Path(user).exists(), "the fake user folders are removed afterwards")

    def test_probe_failure_or_launch_error_stops_the_build(self):
        self.fake_runtime()
        for outcome, message in ((SimpleNamespace(returncode=1), "loads packages from the user's own Python"),
                                 (OSError("PRIVATE_MACHINE_DETAIL"), "cannot run the user site probe")):
            with self.subTest(message=message), mock.patch.object(stage.subprocess, "run", side_effect=[outcome]):
                with self.assertRaisesRegex(RuntimeError, message) as caught:
                    stage.verify_staged_user_site_disabled()
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_real_runtime_ignores_user_sites_only_with_the_client_flags(self):
        self.real_runtime()
        stage.verify_staged_user_site_disabled()
        # A ._pth file alone leaves the user site on, so the probe must catch a launch without -s.
        with mock.patch.object(stage, "python_flags", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "loads packages from the user's own Python"):
                stage.verify_staged_user_site_disabled()


if __name__ == "__main__":
    unittest.main()
