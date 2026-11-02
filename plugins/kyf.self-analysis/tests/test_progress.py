"""Measured, bounded ETA and model-call counters using synthetic work only."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_draft as fixture
from self_model import Job
from self_progress import progress_step, timed_view


class ProgressTests(unittest.TestCase):
    def step(self):
        return {"key": "profile", "title": "本人资料", "state": "running", "unit": "条", "phase": "extracting",
                "done": 0, "total": 100, "successfulCallCount": 0}

    def measured(self):
        step = self.step()
        for calls, (at, done) in enumerate(((1000, 10), (5000, 20), (9000, 30)), 1):
            step = progress_step(step, {"successfulCallCount": calls * 2}, at)
            step = progress_step(step, {"done": done}, at + 10)
        return step

    def test_eta_needs_repeated_actual_progress_and_describes_only_current_phase(self):
        step = self.measured()
        run = timed_view({"state": "running", "startedAt": 1000, "steps": [step]}, 10000)
        self.assertEqual(run["elapsedMs"], 9000)
        self.assertEqual(run["eta"]["scope"], "phase")
        self.assertEqual(run["eta"]["basis"], "observed")
        self.assertLess(run["eta"]["lowMs"], run["eta"]["highMs"])
        self.assertLess(run["eta"]["highMs"], 60000)

    def test_cached_bursts_unknown_totals_and_conversation_counts_never_give_eta(self):
        for changes in ({"successfulCallCount": 0}, {"total": None}, {"unit": "个聊天"}):
            step = {**self.step(), **changes}
            for at, done in ((1000, 0), (5000, 10), (9000, 20)):
                step = progress_step(step, {"done": done}, at)
            self.assertNotIn("eta", timed_view({"state": "running", "steps": [step]}, 10000))
        step = self.step()
        for at, done in ((1000, 0), (1001, 10), (1002, 20)):
            step = progress_step(step, {"done": done}, at)
        self.assertNotIn("eta", timed_view({"state": "running", "steps": [step]}, 1003))

    def test_phase_unit_or_denominator_change_invalidates_eta_and_stale_work_does_not_count_down(self):
        step = self.measured()
        for changes in ({"unit": "批", "phase": "merging", "done": 0, "total": 3}, {"total": 200}, {"phase": "writing"}, {"startedAt": 10000}):
            updated = progress_step(step, changes, 10000)
            self.assertNotIn("eta", timed_view({"state": "running", "steps": [updated]}, 10001))
        self.assertNotIn("eta", timed_view({"state": "running", "steps": [step]}, 200000))

    def test_model_completion_does_not_create_a_baseline_before_persisted_progress(self):
        step = self.step()
        step = progress_step(step, {"successfulCallCount": 2}, 1000)
        self.assertEqual(step["rateSamples"], [])
        step = progress_step(step, {"done": 10}, 1010)
        self.assertEqual(step["rateSamples"], [[1010, 10]])
        step = progress_step(step, {"successfulCallCount": 4}, 11010)
        step = progress_step(step, {"done": 20}, 11020)
        self.assertNotIn("eta", timed_view({"state": "running", "steps": [step]}, 11021))
        step = progress_step(step, {"successfulCallCount": 6}, 21020)
        step = progress_step(step, {"done": 30}, 21030)
        eta = timed_view({"state": "running", "steps": [step]}, 21031)["eta"]
        self.assertGreater(eta["lowMs"], 10000, "save latency is not model throughput")

    def test_cached_jumps_between_real_batches_clear_the_rate_window(self):
        step = self.measured()
        step = progress_step(step, {"done": 80}, 10000)
        self.assertEqual(step["rateSamples"], [])
        step = progress_step(step, {"successfulCallCount": 8}, 20000)
        step = progress_step(step, {"done": 90}, 20010)
        self.assertEqual(step["rateSamples"], [[20010, 90]])
        self.assertNotIn("eta", timed_view({"state": "running", "steps": [step]}, 20011))

    def test_old_samples_without_call_baseline_and_rewound_progress_are_discarded(self):
        step = {**self.step(), "successfulCallCount": 6, "rateSamples": [[1000, 0], [1010, 10], [11020, 20]]}
        step = progress_step(step, {"done": 20}, 12000)
        self.assertEqual(step["rateSamples"], [])
        step = self.measured()
        step = progress_step(step, {"done": 0}, 12000)
        self.assertEqual(step["rateSamples"], [])

    def test_paused_and_interrupted_elapsed_freezes_at_last_observed_time(self):
        run = {"state": "paused", "startedAt": 1000, "finishedAt": 5000, "steps": []}
        self.assertEqual(timed_view(run, 99999)["elapsedMs"], 4000)
        run = {"state": "interrupted", "startedAt": 1000, "steps": [{"updatedAt": 7000}]}
        self.assertEqual(timed_view(run, 99999)["elapsedMs"], 6000)
        self.assertNotIn("elapsedMs", timed_view({"state": "interrupted", "startedAt": 1000, "steps": []}, 99999))

    def test_a_stalled_interval_cannot_promise_thousands_of_hours(self):
        # One slow gap between two persisted batches (live 2026-10-29: "3875 hours")
        # must not stretch the band; the spread stays within a bounded factor.
        step = self.measured()
        step = progress_step(step, {"successfulCallCount": 10}, 1000000)
        step = progress_step(step, {"done": 41}, 1000010)
        eta = timed_view({"state": "running", "steps": [step]}, 1000011)["eta"]
        self.assertLessEqual(eta["highMs"], eta["lowMs"] * 4)
        self.assertGreater(eta["lowMs"], 0)

    def test_model_call_observation_retains_only_counts_and_timing(self):
        job = Job(SimpleNamespace(models=fixture.Models()), None, "synthetic-account")
        seen = []
        job.on_update = seen.append
        with patch("self_model.now_ms", side_effect=[1000, 4000]):
            result = job.generate(fixture.Context(), {"system": "synthetic", "prompt": "{}", "jsonMode": True}, "synthetic-step", lambda text: {"valid": True})
        self.assertTrue(result["valid"])
        self.assertEqual(seen[0]["activeCallStartedAt"], 1000)
        self.assertIsNone(seen[-1]["activeCallStartedAt"])
        self.assertEqual((seen[-1]["modelCalls"], seen[-1]["successfulCallCount"], seen[-1]["cumulativeCallMs"]), (1, 1, 3000))
        self.assertNotIn("prompt", seen[-1])


if __name__ == "__main__":
    unittest.main()
