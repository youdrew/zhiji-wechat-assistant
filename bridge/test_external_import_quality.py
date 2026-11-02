"""Generated-image integration checks for automatic import refinement."""
from __future__ import annotations

import copy
import importlib.util
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_external_import import ExternalImportService
from core_tasks import TaskError
from test_external_import import Tasks

_tracking_spec = importlib.util.spec_from_file_location("external_import_quality_tracking",
    Path(__file__).resolve().parents[1] / "plugins" / "kyf.external-chat-import" / "backend" / "tracking.py")
_tracking = importlib.util.module_from_spec(_tracking_spec)
_tracking_spec.loader.exec_module(_tracking)


def message(text, *, y=.3, sender="other", issues=None, height=.08):
    return {"text": text, "sender": sender, "kind": "text", "issues": issues or [],
            "bbox": [.1, y, .6, height], "timeLabel": None}


class QualitySupervisor:
    def __init__(self):
        self.plan = {"schema": 1, "screenshot": {"tileHeight": 800, "overlap": 200},
            "image": {"minWidth": 660, "format": "png", "maxUpscale": 3},
            "refinement": {"maxFrames": 3, "maxReviews": 40, "maxAttempts": 1}}
        self.primary = [[message("unclear □", issues=["unclear_text"])], [message("Clear later message")]]
        self.calls, self.reviews = [], []
        self.recognized = 0
        self.review_entered = threading.Event()
        self.review_gate = None
        self.resolved = False
        self.match_error = None
        self.recognition_gate = None
        self.gated_recognition = None
        self.recognition_entered = threading.Event()

    def rpc(self, plugin, method, params, context, timeout=None):
        self.calls.append(method)
        if method == "transcription.plan":
            return copy.deepcopy(self.plan)
        if method == "transcription.recognize":
            rows = copy.deepcopy(self.primary[self.recognized % len(self.primary)])
            self.recognized += 1
            if self.recognition_gate and self.recognized == self.gated_recognition:
                self.recognition_entered.set()
                self.recognition_gate.wait(5)
            return {"schema": 1, "messages": rows, "warnings": [], "supportsTracking": True,
                    "links": _tracking.match_observations(params.get("previous", []), rows, params.get("overlap", {}))}
        if method == "transcription.match":
            if self.match_error is not None:
                raise self.match_error
            return {"schema": 1, "links": _tracking.match_observations(
                params["previous"], params["current"], params["overlap"])}
        if method == "transcription.review":
            self.reviews.append(copy.deepcopy(params))
            self.review_entered.set()
            if self.review_gate:
                self.review_gate.wait(5)
            if self.resolved:
                evidence = [frame["id"] for frame in params["frames"]]
                return {"schema": 1, "resolved": True, "message": message("Recovered clear text"),
                    "evidenceSegmentIds": evidence, "preferredEvidenceSegmentId": evidence[0]}
            return {"schema": 1, "resolved": False}
        raise AssertionError("Unexpected RPC: " + method)


class ExternalImportQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.supervisor = QualitySupervisor()
        self.core = SimpleNamespace(tasks=Tasks(), supervisor=self.supervisor, backend=SimpleNamespace(),
            registry=SimpleNamespace(enabled=lambda _: True, get=lambda _: {"name": "导入"}, dependency_error=lambda _: None),
            main_concurrency=lambda: 1)
        self.service = ExternalImportService(self.core, Path(self.temp.name) / "data")
        self.addCleanup(self.service.close)
        friend = self.service.store.create_friend("Synthetic refinement friend")
        self.batch = self.service.store.create_batch(friend["id"], "synthetic-source", "Synthetic self", "Synthetic conversation")

    def upload(self, height=1400):
        content = io.BytesIO()
        Image.new("RGB", (600, height), "white").save(content, "PNG")
        data = content.getvalue()
        self.service.upload(self.batch["id"], "synthetic.png", io.BytesIO(data), len(data))

    def wait_done(self):
        deadline = time.monotonic() + 8
        while self.service.runs and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.service.runs)
        batch = self.service.store.get_batch(self.batch["id"])
        self.assertIn(batch["state"], ("review", "partial", "saved", "interrupted"), batch.get("error"))
        return batch

    def run_batch(self, **kwargs):
        self.service.start(self.batch["id"], **kwargs)
        return self.wait_done()

    def test_tile_overlap_completes_partial_and_preserves_real_repeated_bubbles(self):
        self.supervisor.primary = [
            [message("哈哈", y=.2), message("Independent overlap anchor", y=.77, height=.06),
             message("This is the partial", y=.93, height=.07, issues=["truncated"])],
            [message("Independent overlap anchor", y=.02, height=.06),
             message("This is the partial message complete", y=.18, height=.14), message("哈哈", y=.5)],
        ]
        self.upload()
        batch = self.run_batch()
        self.assertEqual(batch["counts"]["total"], 4, batch["candidates"])
        repeated = [row for row in batch["candidates"] if row["text"] == "哈哈"]
        self.assertEqual(len(repeated), 2)
        self.assertTrue(all(row["ready"] for row in repeated), repeated)
        completed = next(row for row in batch["candidates"] if row["text"].startswith("This is"))
        self.assertEqual(completed["text"], "This is the partial message complete")
        self.assertFalse(completed["truncated"])
        self.assertTrue(completed["autoReviewed"])
        self.assertFalse(completed["reviewed"])
        self.assertEqual(len(completed["evidenceSegmentIds"]), 2)
        self.assertEqual(self.supervisor.reviews, [])
        saved = self.service.store.save_batch(self.batch["id"])
        self.assertEqual(saved["added"], 4)
        history = self.service.store.messages(self.batch["friendId"])["messages"]
        self.assertEqual(sum(row["text"] == "哈哈" for row in history), 2)

    def test_primary_finishes_before_bounded_multiframe_review(self):
        self.supervisor.primary = [[message(f"unclear {index} □", issues=["unclear_text"])] for index in range(4)]
        self.supervisor.plan["refinement"] = {"maxFrames": 99, "maxReviews": 2}
        self.upload(height=2600)
        batch = self.run_batch()
        self.assertEqual(self.supervisor.recognized, 4)
        first_review = self.supervisor.calls.index("transcription.review")
        self.assertEqual(self.supervisor.calls[:first_review].count("transcription.recognize"), 4)
        self.assertEqual(len(self.supervisor.reviews), 2)
        self.assertTrue(all(1 <= len(call["frames"]) <= 3 for call in self.supervisor.reviews))
        self.assertEqual(sum(bool(row.get("autoReviewAttempted")) for row in batch["candidates"]), 2)
        self.assertEqual(batch["counts"]["pending"], 4)

    def test_unresolved_review_is_checkpointed_and_not_repeated_on_resume(self):
        self.upload()
        first = self.run_batch()
        uncertain = next(row for row in first["candidates"] if "□" in row["text"])
        self.assertTrue(uncertain["autoReviewAttempted"])
        self.assertFalse(uncertain["ready"])
        self.assertEqual(len(self.supervisor.reviews), 1)
        primary_count = self.supervisor.recognized
        self.run_batch()
        self.assertEqual(len(self.supervisor.reviews), 1)
        self.assertEqual(self.supervisor.recognized, primary_count)

    def test_invalid_reread_after_cross_frame_replacement_falls_back_without_reprocessing(self):
        self.supervisor.primary = [[message('Partial', issues=['truncated'])],
                                   [message('Partial completed')], [message('Conflicting observation')]]
        self.upload(height=2000)
        original_rpc = self.supervisor.rpc
        linked = False
        before = None

        def rpc(plugin, method, params, context, timeout=None):
            result = original_rpc(plugin, method, params, context, timeout)
            if method == 'transcription.recognize':
                result['links'] = []
            elif method == 'transcription.review':
                evidence = [frame['id'] for frame in params['frames']]
                result = {'schema': 1, 'resolved': True,
                          'message': message('Still unclear □', issues=[]),
                          'evidenceSegmentIds': evidence, 'preferredEvidenceSegmentId': evidence[0]}
            return result

        def match(batch_id, segments, overlaps, run):
            nonlocal linked, before
            if linked:
                return
            linked = True
            first = self.service.store.segment_candidates(segments[0]['id'])[0]
            for segment, action in ((segments[1], 'replace'), (segments[2], 'conflict')):
                self.service.store.apply_auto_links(batch_id, segment['id'], [
                    {'currentIndex': 0, 'previousId': first['id'], 'action': action}])
            before = self.service.store.segment_candidates(segments[0]['id'])[0]

        with patch.object(self.supervisor, 'rpc', side_effect=rpc), patch.object(self.service, '_match_segments', side_effect=match):
            batch = self.run_batch()
            self.assertEqual(batch['state'], 'review')
            candidate = next(row for row in batch['candidates'] if row['text'] == 'Partial completed')
            segments = batch['segments']
            self.assertEqual(candidate['evidenceSegmentId'], segments[1]['id'])
            self.assertEqual(candidate['evidenceSegmentIds'], before['evidenceSegmentIds'])
            self.assertEqual(set(candidate['evidenceSegmentIds']), {row['id'] for row in segments})
            self.assertTrue(candidate['autoReviewAttempted'])
            self.assertFalse(candidate['reviewed'])
            self.assertFalse(candidate['ready'])
            self.assertEqual(candidate['original']['text'], 'Partial')
            self.assertTrue(all(row['state'] == 'done' for row in segments))
            self.assertEqual(self.service.store.messages(self.batch['friendId'])['messages'], [])
            raw_count = sum(len(self.service.store.segment_candidates(row['id'])) for row in segments)
            self.assertEqual(raw_count, 3)
            calls = (self.supervisor.recognized, len(self.supervisor.reviews))
            resumed = self.run_batch()
            self.assertEqual((self.supervisor.recognized, len(self.supervisor.reviews)), calls)
            self.assertEqual(resumed['candidates'], batch['candidates'])

    def test_stop_during_review_discards_late_result_then_resumes(self):
        self.upload()
        self.supervisor.resolved = True
        gate = self.supervisor.review_gate = threading.Event()
        self.addCleanup(gate.set)
        self.service.start(self.batch["id"])
        self.assertTrue(self.supervisor.review_entered.wait(3))
        primary_count = self.supervisor.recognized
        self.service.stop(self.batch["id"])
        gate.set()
        stopped = self.wait_done()
        self.assertEqual(stopped["state"], "interrupted")
        uncertain = next(row for row in stopped["candidates"] if "□" in row["text"])
        self.assertFalse(uncertain.get("autoReviewAttempted", False))
        self.assertFalse(any(row["text"] == "Recovered clear text" for row in stopped["candidates"]))
        self.supervisor.review_gate = None
        resumed = self.run_batch()
        self.assertEqual(self.supervisor.recognized, primary_count)
        recovered = next(row for row in resumed["candidates"] if row["text"] == "Recovered clear text")
        self.assertTrue(recovered["ready"])
        self.assertTrue(recovered["autoReviewAttempted"])
        self.assertFalse(recovered["reviewed"])

    def test_reprocessing_preserves_saved_and_unconfirmed_human_text(self):
        self.supervisor.primary = [[message("First unclear □", issues=["unclear_text"])],
                                   [message("Second unclear □", issues=["unclear_text"])]]
        self.upload()
        batch = self.run_batch()
        first, second = batch["candidates"]
        self.service.store.update_candidate(batch["id"], first["id"], {"text": "Human confirmed text"}, resolve=True)
        self.service.store.update_candidate(batch["id"], second["id"], {"text": "Human unfinished draft"})
        self.assertEqual(self.service.store.save_batch(batch["id"])["added"], 1)
        review_count = len(self.supervisor.reviews)
        self.supervisor.primary = [[message("Model changed first", issues=["unclear_text"])],
                                   [message("Model changed second", issues=["unclear_text"])]]
        after = self.run_batch(reprocess=True)
        by_id = {row["id"]: row for row in after["candidates"]}
        self.assertEqual(by_id[first["id"]]["text"], "Human confirmed text")
        self.assertEqual(by_id[second["id"]]["text"], "Human unfinished draft")
        self.assertIsNotNone(by_id[first["id"]]["savedMessageId"])
        self.assertIn("edited-unconfirmed", by_id[second["id"]]["issues"])
        self.assertEqual(len(self.supervisor.reviews), review_count)
        self.assertEqual([row["text"] for row in self.service.store.messages(batch["friendId"])["messages"]],
                         ["Human confirmed text"])

    def test_legacy_plan_has_no_automatic_match_or_review(self):
        self.supervisor.plan.pop("refinement")
        self.upload()
        batch = self.run_batch()
        self.assertEqual(batch["counts"]["pending"], 1)
        self.assertNotIn("transcription.match", self.supervisor.calls)
        self.assertNotIn("transcription.review", self.supervisor.calls)
        self.assertFalse(any(row.get("autoReviewAttempted") for row in batch["candidates"]))

    def test_optional_match_failure_preserves_primary_candidates_and_review(self):
        self.upload()
        for error in (ValueError("legacy UI candidate"), TaskError("invalid-request", "legacy date separator", 400)):
            with self.subTest(error=type(error).__name__):
                self.supervisor.match_error = error
                batch = self.run_batch()
                self.assertEqual(batch["state"], "review")
                self.assertEqual([row["text"] for row in batch["candidates"]], ["unclear □", "Clear later message"])
                self.assertEqual(batch["counts"]["pending"], 1)
                self.assertTrue(batch["candidates"][0]["autoReviewAttempted"])
        self.assertEqual(self.supervisor.recognized, 2)
        self.assertEqual(len(self.supervisor.reviews), 1)

    def test_overlapping_distinct_screenshots_keep_duplicate_review_gate(self):
        self.supervisor.primary = [[message("Same actual overlapping bubble")],
                                   [message("Same actual overlapping bubble")]]
        for index in range(2):
            image = Image.new("RGB", (600, 700), "white")
            image.putpixel((0, 0), (index, 0, 0))
            output = io.BytesIO()
            image.save(output, "PNG")
            data = output.getvalue()
            self.service.upload(self.batch["id"], f"synthetic-{index}.png", io.BytesIO(data), len(data))
        batch = self.run_batch()
        self.assertEqual(len(batch["materials"]), 2)
        self.assertEqual(batch["counts"]["total"], 2)
        self.assertEqual(batch["counts"]["ready"], 1)
        self.assertEqual(batch["counts"]["pending"], 1)
        self.assertIn("possible_duplicate", batch["candidates"][1]["issues"])
        saved = self.service.store.save_batch(batch["id"])
        self.assertEqual(saved["added"], 1)
        self.assertEqual(saved["pending"], 1)
        self.assertEqual(len(self.service.store.messages(batch["friendId"])["messages"]), 1)

    def test_resumed_overlap_uses_source_identity_and_keeps_saved_human_correction(self):
        source = "Source transcription value A"
        self.supervisor.primary = [[message(source, y=.8)], [message(source, y=.05)]]
        gate = self.supervisor.recognition_gate = threading.Event()
        self.supervisor.gated_recognition = 2
        self.addCleanup(gate.set)
        self.upload()
        self.service.start(self.batch["id"])
        self.assertTrue(self.supervisor.recognition_entered.wait(3))
        self.service.stop(self.batch["id"])
        gate.set()
        stopped = self.wait_done()
        self.assertEqual(len(stopped["candidates"]), 1)
        original = stopped["candidates"][0]
        self.service.store.update_candidate(self.batch["id"], original["id"],
            {"text": "Human explicitly corrected B", "sender": "self"}, resolve=True)
        self.assertEqual(self.service.store.save_batch(self.batch["id"])["added"], 1)
        self.supervisor.recognition_gate = None
        self.supervisor.primary = [[message(source, y=.05)]]
        resumed = self.run_batch()
        self.assertEqual(len(resumed["candidates"]), 1, resumed["candidates"])
        canonical = resumed["candidates"][0]
        self.assertEqual(canonical["text"], "Human explicitly corrected B")
        self.assertEqual(canonical["sender"], "self")
        self.assertTrue(canonical["reviewed"])
        self.assertEqual(len(canonical["evidenceSegmentIds"]), 2)
        self.assertEqual(self.service.store.save_batch(self.batch["id"])["added"], 0)
        history = self.service.store.messages(self.batch["friendId"])["messages"]
        self.assertEqual([(row["text"], row["sender"]) for row in history], [("Human explicitly corrected B", "self")])

    def test_same_bubble_normalization_links_without_rewriting_source_text(self):
        original = "First cafe\u0301 line\nsecond line"
        normalized = "First café linesecond line"
        self.supervisor.primary = [[message(original, y=.8)], [message(normalized, y=.05)]]
        self.upload()
        batch = self.run_batch()
        self.assertEqual(len(batch["candidates"]), 1, batch["candidates"])
        self.assertEqual(batch["candidates"][0]["text"], original)
        self.assertEqual(len(batch["candidates"][0]["evidenceSegmentIds"]), 2)
        self.assertEqual(self.service.store.save_batch(self.batch["id"])["added"], 1)


if __name__ == "__main__":
    unittest.main()
