"""Owned SDK workers cannot survive a timed-out/cancelled local model call.

The pipe worker below handles synthetic JSON only; it never opens a socket.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
from kyf_plugin import Cancelled, ModelClient
from self_contracts import SelfError
from self_model import Job, ModelContext, _JobModelClient


WORKER = '''
import json,sys,time
for raw in sys.stdin:
    value=json.loads(raw)
    if value['cmd']=='cancel':
        continue
    prompt=value['payload']['request']['prompt']
    if prompt=='hang':
        continue
    if prompt=='slow':
        time.sleep(.35)
    if prompt.startswith('{'):
        payload=json.loads(prompt)
        if sum(isinstance(line,dict) for chat in payload['chats'] for line in chat['lines'])>1:
            continue
        result=json.dumps({domain:[] for domain in ('profile','tags','persona','values')})
    else:
        result='synthetic-ok'
    print(json.dumps({'id':value['id'],'result':{'text':result}}),flush=True)
'''


class Context:
    def __init__(self):
        self.cancelled = threading.Event()
        self.token = "synthetic-token"

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.processes, self.clients = [], []
        original_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original_popen([sys.executable, "-u", "-c", WORKER], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.processes.append(process)
            return process
        original_init = _JobModelClient.__init__
        def initialize(client, plugin):
            original_init(client, plugin)
            self.clients.append(client)
        self.enterContext(patch("kyf_plugin.models.subprocess.Popen", side_effect=spawn))
        self.enterContext(patch.dict(os.environ, KYF_NODE="synthetic-node", KYF_TSX_LOADER="synthetic-loader"))
        self.enterContext(patch.object(_JobModelClient, "__init__", initialize))
        self.plugin = SimpleNamespace(core=SimpleNamespace(model_services=lambda: {"services": [{"model": "synthetic"}]}))
        self.plugin.models = ModelClient(self.plugin)
        self.job, self.context = Job(self.plugin, None, "synthetic-account"), Context()
        self.addCleanup(self.job.close_models)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for client in self.clients:
            client.close()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)

    def call(self, prompt, context=None):
        return self.job._model_call(context or self.context, {"prompt": prompt}, "synthetic")

    def test_repeated_deadlines_reap_waiters_and_next_call_uses_a_fresh_worker(self):
        with patch("self_model.CALL_DEADLINE_SECONDS", .12):
            for _ in range(5):
                with self.assertRaises(SelfError) as failure:
                    self.call("hang")
                self.assertEqual(failure.exception.code, "timeout")
                self.assertTrue(all(not client.pending for client in self.clients))
                self.assertTrue(all(process.poll() is not None for process in self.processes))
                self.assertFalse(self.context.cancelled.is_set())
        self.assertEqual(self.call("ok"), {"text": "synthetic-ok"})
        self.assertEqual(len(self.clients), 6)

    def test_parent_cancellation_is_prompt_and_does_not_wait_for_deadline(self):
        timer = threading.Timer(.15, self.context.cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                self.call("hang")
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(self.clients[0].pending)
        self.assertIsNotNone(self.processes[0].poll())

    def test_cancelling_one_window_leaves_parallel_worker_and_parent_alive(self):
        local = ModelContext(self.context)
        results = {}
        def call(name, prompt, context):
            try:
                results[name] = self.call(prompt, context)
            except Cancelled:
                results[name] = "cancelled"
        bad = threading.Thread(target=call, args=("bad", "hang", local))
        good = threading.Thread(target=call, args=("good", "slow", self.context))
        bad.start(); good.start()
        until = time.monotonic() + 2
        while len(self.processes) < 2 and time.monotonic() < until:
            time.sleep(.01)
        local.set()
        bad.join(2); good.join(2)
        self.assertEqual(results, {"bad": "cancelled", "good": {"text": "synthetic-ok"}})
        self.assertFalse(self.context.cancelled.is_set())
        self.assertEqual(sum(process.poll() is None for process in self.processes), 1)

    def test_discovery_returning_after_deadline_cannot_spawn_a_retired_worker(self):
        entered, release = threading.Event(), threading.Event()
        def services():
            entered.set()
            release.wait(3)
            return {"services": [{"model": "synthetic"}]}
        self.plugin.core.model_services = services
        with patch("self_model.CALL_DEADLINE_SECONDS", .1):
            with self.assertRaises(SelfError):
                self.call("hang")
        self.assertTrue(entered.is_set())
        self.assertTrue(self.clients[0].retired)
        release.set()
        # The old generate() continues after discovery, but _start is fenced.
        time.sleep(.1)
        self.assertEqual(self.processes, [])
        self.plugin.core.model_services = lambda: {"services": [{"model": "synthetic"}]}
        self.assertEqual(self.call("ok"), {"text": "synthetic-ok"})

    def test_abandoned_evidence_window_reaps_worker_before_halved_windows_finish(self):
        # Use the real dispatcher, SDK pipe and a temporary synthetic corpus.
        sys.path.insert(0, str(Path(__file__).parent))
        from self_evidence import SharedEvidence
        from self_store import Store
        from test_tag_corpus import Corpus, own
        self.plugin.core.model_services = lambda: {"services": [{"model": "synthetic", "contextTokens": 32768}]}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), "synthetic-account")
            corpus = Corpus([own(1, "synthetic first evidence line"), own(2, "synthetic second evidence line")])
            job = SharedEvidence(self.plugin, store, "synthetic-account", corpus)
            try:
                with patch("self_evidence.STALLED_CALL_SECONDS", .15), patch("self_model.CALL_DEADLINE_SECONDS", 5):
                    result = job.work(self.context)
                self.assertEqual(result["coverage"]["analyzedMessages"], 2)
                self.assertEqual(job.data["stalledCalls"], 1)
                self.assertEqual(job.data["modelCalls"], 3)
                self.assertTrue(all(not client.pending for client in self.clients))
                self.assertTrue(all(process.poll() is not None for process in self.processes))
                self.assertFalse(self.context.cancelled.is_set())
            finally:
                job.close_models()
                store.close()


if __name__ == "__main__":
    unittest.main()
