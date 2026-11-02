"""Deterministic selective-analysis races; synthetic models and temporary stores only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import threading
import unittest
from unittest.mock import patch

import test_unified_analysis as fixture
from self_analysis_run import AnalysisRun, StageCancellation
from self_contracts import SelfError
from self_evidence import EvidenceStore


class ControlledEmbedding:
    """Keep one real synthetic worker alive until its cancellation is released."""

    def __init__(self, store):
        self.store, self.thread, self.starts = store, None, 0
        self.cancel = threading.Event()
        self.release_first = threading.Event()

    def state(self, state):
        with self.store.lock, self.store.db:
            self.store._meta("embeddingRun", {"state": state, "done": 0 if state != "ready" else 3, "total": 3})

    def start(self):
        if self.thread and self.thread.is_alive():
            return False
        self.starts += 1
        first = self.starts == 1
        self.cancel.clear()
        self.state("indexing")

        def run():
            if first:
                self.release_first.wait(5)
            if not self.cancel.is_set():
                self.state("ready")

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return True

    def pause(self):
        self.cancel.set()
        if self.store.meta("embeddingRun", {}).get("state") in ("waiting", "indexing"):
            self.state("paused")

    def close(self):
        self.pause()
        self.release_first.set()
        if self.thread:
            self.thread.join(5)
        return not self.thread or not self.thread.is_alive()


class SelectiveRaceTests(unittest.TestCase):
    setup_unified = fixture.UnifiedTests.setup_unified
    finish = fixture.UnifiedTests.finish

    def setUp(self):
        fixture.UnifiedTests.setUp(self)
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for job in self.service.analyses.values():
            job.cancel()
        for thread in self.plugin.threads:
            thread.join(5)
        fixture.UnifiedTests.tearDown(self)

    def hold_model(self, step):
        reached, release = threading.Event(), threading.Event()

        def hook(_context, request):
            if request["step"] == step:
                reached.set()
                release.wait(5)

        self.models.hook = hook
        self.release_events.append(release)
        return reached, release

    def assert_resume_busy(self, output):
        with self.assertRaises(SelfError) as error:
            self.service.analysis_resume(self.account, {"output": output})
        self.assertEqual(error.exception.code, "analysis-busy")

    def test_semantic_pause_rejects_early_resume_then_restarts_without_an_orphan_worker(self):
        store = self.setup_unified()
        embedding = ControlledEmbedding(store)
        self.service.embeddings[self.account] = embedding
        self.release_events.append(embedding.release_first)
        reached, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = StageCancellation.wait

        def hold_semantic_wait(stage_cancel, timeout=None):
            if stage_cancel.stage.key == "semantic":
                reached.set()
                release.wait(5)
            return original(stage_cancel, timeout)

        with patch.object(StageCancellation, "wait", hold_semantic_wait):
            self.service.analyze(self.account, {"depth": "basic", "outputs": ["library"]})
            self.assertTrue(reached.wait(5), "the semantic action did not reach its cancellation checkpoint")
            paused = self.service.analysis_pause(self.account, {"output": "library"})
            steps = {step["key"]: step for step in paused["steps"]}
            self.assertTrue(steps["semantic"]["stopping"])
            self.assertTrue(steps["library"]["stopping"])
            self.assert_resume_busy("library")
            self.assertEqual(embedding.starts, 1)
            release.set()
            stopped = self.finish()
        self.assertEqual(stopped["state"], "paused", stopped)
        self.assertFalse(any(step.get("stopping") for step in stopped["steps"]))
        self.assertEqual(next(step["state"] for step in stopped["steps"] if step["key"] == "semantic"), "paused")
        embedding.release_first.set()
        embedding.thread.join(5)
        self.assertFalse(embedding.thread.is_alive())
        self.service.analysis_resume(self.account, {"output": "library"})
        completed = self.finish()
        self.assertEqual(completed["state"], "done", completed)
        self.assertEqual(next(step["state"] for step in completed["steps"] if step["key"] == "semantic"), "done")
        self.assertEqual((embedding.starts, store.meta("embeddingRun", {})["state"]), (2, "ready"))
        self.assertFalse(embedding.thread.is_alive())

    def test_resume_after_scheduler_exit_is_processed_before_the_same_task_finishes(self):
        self.setup_unified()
        reached, release = self.hold_model("self-evidence-extract")
        work_returned, finish_release = threading.Event(), threading.Event()
        self.release_events.append(finish_release)
        original = AnalysisRun.work
        work_calls = []

        def hold_after_work(job, context):
            result = original(job, context)
            work_calls.append(job.id)
            if len(work_calls) == 1:
                work_returned.set()
                finish_release.wait(5)
            return result

        with patch.object(AnalysisRun, "work", hold_after_work):
            first = self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile", "tags"]})
            self.assertTrue(reached.wait(5))
            self.service.analysis_pause(self.account, {"output": "tags"})
            release.set()
            self.assertTrue(work_returned.wait(5))
            resumed = self.service.analysis_resume(self.account, {"output": "tags"})
            self.assertEqual(resumed["id"], first["id"])
            finish_release.set()
            completed = self.finish()
        self.assertEqual(completed["state"], "done", completed)
        self.assertEqual(next(step["state"] for step in completed["steps"] if step["key"] == "tags"), "done")
        self.assertEqual(completed["pausedOutputs"], [])
        self.assertEqual(len(self.plugin.tasks), 1)
        self.assertEqual(work_calls, [first["id"], first["id"]])

    def test_resume_waiting_on_terminal_lock_starts_a_new_task_instead_of_losing_the_output(self):
        self.setup_unified()
        reached, release = self.hold_model("self-evidence-extract")
        terminal_reached, terminal_release = threading.Event(), threading.Event()
        resume_started = threading.Event()
        self.release_events.append(terminal_release)
        original_set, original_resume = AnalysisRun._set, AnalysisRun.resume_output
        results, errors = [], []

        def hold_terminal(job, **changes):
            if changes.get("state") == "paused":
                terminal_reached.set()
                terminal_release.wait(5)
            return original_set(job, **changes)

        def observe_resume(job, key):
            resume_started.set()
            return original_resume(job, key)

        def resume():
            try:
                results.append(self.service.analysis_resume(self.account, {"output": "tags"}))
            except Exception as exc:
                errors.append(exc)

        with patch.object(AnalysisRun, "_set", hold_terminal), patch.object(AnalysisRun, "resume_output", observe_resume):
            first = self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile", "tags"]})
            self.assertTrue(reached.wait(5))
            self.service.analysis_pause(self.account, {"output": "tags"})
            release.set()
            self.assertTrue(terminal_reached.wait(5))
            requester = threading.Thread(target=resume, daemon=True)
            requester.start()
            self.assertTrue(resume_started.wait(5))
            terminal_release.set()
            requester.join(5)
            self.assertFalse(requester.is_alive())
            self.assertFalse(errors, errors)
            completed = self.finish()
        self.assertEqual(completed["state"], "done", completed)
        self.assertNotEqual(results[0]["id"], first["id"])
        self.assertEqual(completed["outputs"], ["tags"])
        self.assertEqual(next(step["state"] for step in completed["steps"] if step["key"] == "tags"), "done")
        self.assertEqual(len(self.plugin.tasks), 2)

    def test_current_summary_cannot_resume_until_its_late_request_is_acknowledged(self):
        self.setup_unified()
        reached, release = self.hold_model("profile-draft-final")
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile", "tags"]})
        self.assertTrue(reached.wait(5))
        paused = self.service.analysis_pause(self.account, {"output": "profile"})
        self.assertTrue(next(step["stopping"] for step in paused["steps"] if step["key"] == "profile"))
        self.assert_resume_busy("profile")
        release.set()
        stopped = self.finish()
        self.assertEqual(stopped["state"], "paused", stopped)
        self.assertFalse(any(step.get("stopping") for step in stopped["steps"]))
        self.assertEqual(self.service.draft_status(self.account)["state"], "cancelled")
        self.models.hook = None
        self.service.analysis_resume(self.account, {"output": "profile"})
        self.assertEqual(self.finish()["state"], "done")
        self.assertEqual(sum(request["step"] == "profile-draft-final" for request in self.models.requests), 2)

    def test_last_ai_output_cannot_resume_until_shared_extraction_acknowledges_pause(self):
        store = self.setup_unified()
        reached, release = self.hold_model("self-evidence-extract")
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile"]})
        self.assertTrue(reached.wait(5))
        paused = self.service.analysis_pause(self.account, {"output": "profile"})
        for key in ("evidence", "profile"):
            self.assertTrue(next(step["stopping"] for step in paused["steps"] if step["key"] == key))
        self.assert_resume_busy("profile")
        release.set()
        stopped = self.finish()
        self.assertEqual(stopped["state"], "paused", stopped)
        self.assertFalse(any(step.get("stopping") for step in stopped["steps"]))
        self.assertEqual(EvidenceStore(store).coverage()["analyzedMessages"], 0)
        self.models.hook = None
        self.service.analysis_resume(self.account, {"output": "profile"})
        self.assertEqual(self.finish()["state"], "done")

    def test_continuing_one_item_keeps_the_saved_output_selection(self):
        self.setup_unified()
        self.service.analysis_settings(self.account, {"outputs": ["profile", "tags"]})
        self.service.analysis_resume(self.account, {"depth": "basic", "output": "profile"})
        self.assertEqual(self.finish()["outputs"], ["profile"])
        self.assertEqual(self.service.analysis_settings(self.account, {})["outputs"], ["profile", "tags"])
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["tags"]})
        self.finish()
        self.assertEqual(self.service.analysis_settings(self.account, {})["outputs"], ["tags"])

    def test_global_pause_clears_linked_stopping_flags_and_discards_late_shared_evidence(self):
        store = self.setup_unified()
        reached, release = self.hold_model("self-evidence-extract")
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile", "tags"]})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account, {"output": "profile"})
        self.service.analysis_pause(self.account, {"output": "tags"})
        self.service.analysis_pause(self.account)
        release.set()
        stopped = self.finish()
        self.assertEqual(stopped["state"], "paused", stopped)
        self.assertFalse(stopped["stopping"])
        self.assertFalse(any(step.get("stopping") for step in stopped["steps"]))
        self.assertEqual(EvidenceStore(store).coverage()["analyzedMessages"], 0)


if __name__ == "__main__":
    unittest.main()
