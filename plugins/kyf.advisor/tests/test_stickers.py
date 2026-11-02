"""Sticker identities, captions and labels in the assistant's chat record; synthetic data only."""
# Copyright 2026 Know Your Friends contributors. Apache-2.0.
import base64
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from contracts import AdvisorError
from model_context import _format_line
from stickers import NOTE, clean_label, frames_payload, label
from support import FakeModelStore, FakeRuntime, FakeSource, build_service, make_message, wait_until, wire_media

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
ACCOUNT = "synthetic-account-1"
USER = "synthetic-friend"
STORE, DOG, CAT = "a" * 32, "b" * 32, "c" * 32


def sticker(seq):
    message = make_message(seq, kind="other", text="[表情]")
    message.update(type="动画表情", media={"kind": "sticker"})
    return message


def frames(offsets=(0, 100, 200, 300)):
    return [{"mime": "image/png", "data": base64.b64encode(PNG).decode(), "timeMs": offset} for offset in offsets]


class StickerCore(FakeSource):
    """Synthetic Core that knows each sticker message's content MD5 and returns four frames."""

    def __init__(self, identities):
        super().__init__()
        self.identities = identities
        self.sticker_calls = []
        self.frame_calls = []

    def history_stickers(self, account, user, ids):
        self._check(account)
        self.sticker_calls.append(list(ids))
        return {"stickers": {item: self.identities[item] for item in ids if item in self.identities}}

    def media(self, account, user, message_id, frames=None, **kwargs):
        self._check(account)
        self.frame_calls.append((message_id, frames))
        return wire_media({"kind": "sticker", "sampling": "animated-frames", "durationMs": 400,
                           "items": [{"bytes": PNG, "mime": "image/png", "timeMs": offset} for offset in (0, 100, 200, 300)]})


class FormattingTests(unittest.TestCase):
    def test_label_writes_identity_caption_pack_and_description(self):
        self.assertEqual(label(None), "[表情，内容未读取]")
        self.assertEqual(label({"id": STORE, "caption": "晚安", "packName": "抹茶旦旦"}), "[表情#aaaaaa「晚安」·抹茶旦旦]")
        self.assertEqual(label({"id": DOG, "description": "柴犬歪头眨眼"}), "[表情#bbbbbb：柴犬歪头眨眼]")
        self.assertEqual(label({"id": CAT}), "[表情#cccccc，画面未识别]")

    def test_sticker_line_replaces_the_placeholder_and_keeps_other_media_unchanged(self):
        row = {"msg_id": "m1", "seq": 1, "side": "other", "sender_name": "朋友", "time_ms": 0, "text": "[表情]",
               "raw": '{"type":"动画表情","media":{"kind":"sticker"}}'}
        line = _format_line(row, {"m1": {"id": DOG, "description": "柴犬歪头眨眼"}})
        self.assertTrue(line.endswith("朋友: [表情#bbbbbb：柴犬歪头眨眼]"))
        self.assertNotIn("媒体来源", line)
        self.assertIn("占位", _format_line(row))  # Without identities the old placeholder stays.
        image = {**row, "text": "[图片]", "raw": '{"media":{"kind":"image"}}'}
        self.assertEqual(_format_line(image, {}), _format_line(image))

    def test_mentioned_friend_records_use_the_same_sticker_text(self):
        from participants import _chat_lines
        row = {"msg_id": "m1", "seq": 1, "side": "other", "sender_name": "朋友", "time_ms": 0, "text": "[表情]",
               "raw": '{"media":{"kind":"sticker"}}'}
        lines, _stats = _chat_lines("朋友", "wxid_friend", [row], {"m1": {"id": DOG, "description": "柴犬歪头眨眼"}})
        self.assertIn(NOTE, lines)
        self.assertTrue(any(line.endswith("[表情#bbbbbb：柴犬歪头眨眼]") for line in lines))
        self.assertNotIn(NOTE, _chat_lines("朋友", "wxid_friend", [row])[0])

    def test_label_and_frame_payload_are_bounded(self):
        self.assertEqual(clean_label(" 柴犬\n歪头。 "), "柴犬 歪头")
        self.assertEqual(len(clean_label("字" * 200)), 60)
        self.assertIsNone(clean_label("。"))
        animated = {"kind": "sticker", "sampling": "animated-frames", "items": frames()}
        self.assertEqual([image["timeMs"] for image in frames_payload(animated)["images"]], [0, 100, 200, 300])
        still = {"kind": "sticker", "items": [{"mime": "image/png", "data": base64.b64encode(PNG).decode()}]}
        self.assertFalse(frames_payload(still)["animated"])
        for bad in ({**animated, "items": frames((0, 100, 200, 300, 400))}, {**animated, "items": frames((200, 100))},
                    {**still, "items": still["items"] * 2}, {**still, "kind": "image"}, {"error": "unavailable"}):
            self.assertIsNone(frames_payload(bad))


class StickerServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        messages = [sticker(seq) for seq in range(1, 7)] + [make_message(7, text="哈哈")]
        identities = {messages[0]["id"]: {"id": STORE, "caption": "晚安", "packName": "抹茶旦旦", "pack": "pack.synthetic"}}
        identities.update({message["id"]: {"id": DOG} for message in messages[1:4]})
        identities[messages[4]["id"]] = {"id": CAT}
        # messages[5] has no readable identity.
        self.messages = messages
        self.source = StickerCore(identities)
        self.source.add_messages(USER, messages)
        self.runtime = FakeRuntime()
        self.inspections = []

        def inspect(config, request, on_event, cancel):
            self.inspections.append(request)
            return {"text": "合成小狗眨眼。" if request.get("purpose") else "合成画面。"}
        self.runtime.inspect_media = inspect
        self.models = FakeModelStore(context_tokens=32768)
        self.service = build_service(self.source, self.models, self.root, runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.database = self.service.stores.account(ACCOUNT)

    def ask(self, request_id):
        started = self.service.start(ACCOUNT, USER, "builtin:advisor", getattr(self, "thread_id", None),
                                     "他最常发什么表情", request_id)
        self.thread_id, run_id = started["threadId"], started["run"]["id"]
        self.assertEqual(wait_until(lambda: self.service._runs[run_id].state != "running" and self.service._runs[run_id].state),
                         "done")
        return self.runtime.respond_calls[-1][1]["contextFileText"]

    def labelled(self, *identifiers):
        return wait_until(lambda: all(row["description"] for row in self.database._rows(
            "SELECT description FROM stickers WHERE sticker_id IN (%s)" % ",".join("?" * len(identifiers)), identifiers)))

    def prepare_stickers(self):
        self.source.history_prepare(ACCOUNT, USER)
        self.database.sync(USER)
        self.service.stickers.resolve(ACCOUNT, USER, self.database, 10000, lambda: None)

    def test_explicit_start_is_concurrently_idempotent_and_does_not_dispatch_question_queue(self):
        self.prepare_stickers()
        thread = self.database.create_thread(USER, "builtin:advisor", "synthetic-source", select=True)
        self.database.queue_enqueue(USER, "builtin:advisor", thread["id"], "合成待发问题", "queued-question")
        before = self.database.queue_get(USER, "builtin:advisor", thread["id"])
        with patch.object(self.service, "_queue_kick_locked", side_effect=AssertionError("queue must stay idle")):
            with ThreadPoolExecutor(max_workers=5) as workers:
                results = list(workers.map(lambda _: self.service.stickers.start_requested(
                    ACCOUNT, USER, "synthetic-completion-1"), range(5)))
            self.assertEqual(len({result["taskId"] for result in results}), 1)
            self.assertTrue(wait_until(lambda: self.service.plugin.tasks[0].finished))
        self.assertEqual(len(self.service.plugin.tasks), 1)
        task = self.service.plugin.tasks[0].task
        self.assertEqual(task["kind"], "background")
        self.assertEqual(task["params"], {"purpose": "sticker-labels", "completionRequestId": "synthetic-completion-1"})
        self.assertEqual(self.database.queue_get(USER, "builtin:advisor", thread["id"]), before)
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.database.thread_messages(thread["id"]), [])

    def test_explicit_start_requires_existing_context_and_current_account(self):
        for account, user, expected in ((ACCOUNT, USER, "context-error"), ("other-account", USER, "account-changed")):
            with self.subTest(account=account), self.assertRaises(AdvisorError) as caught:
                self.service.stickers.start_requested(account, user, "synthetic-new")
            self.assertEqual(caught.exception.code, expected)
        self.assertEqual(self.service.plugin.tasks, [])

    def test_uncertain_creation_is_durable_and_never_retried(self):
        self.prepare_stickers()
        with patch.object(self.service.plugin, "start_task", side_effect=RuntimeError("synthetic transport")) as create:
            for _ in range(2):
                with self.assertRaises(AdvisorError) as caught:
                    self.service.stickers.start_requested(ACCOUNT, USER, "synthetic-uncertain")
                self.assertEqual(caught.exception.code, "sticker-request-uncertain")
                self.database.close()
                self.database = self.service.stores.account(ACCOUNT)
            create.assert_called_once()
        record = self.database.sticker_request("synthetic-uncertain")
        self.assertEqual(record["state"], "prepared")
        self.assertIsNone(record["task_id"])

    def test_request_id_cannot_move_to_another_conversation(self):
        self.prepare_stickers()
        self.database.claim_sticker_request(USER, "synthetic-owned")
        with self.assertRaises(AdvisorError) as caught:
            self.service.stickers.start_requested(ACCOUNT, "another-friend", "synthetic-owned")
        self.assertEqual(caught.exception.code, "request-conflict")
        self.assertFalse(self.service.plugin.tasks)

    def test_completed_hundred_item_batch_leaves_truthful_work_for_next_request(self):
        for seq in range(8, 109):
            message = sticker(seq)
            self.source.add_messages(USER, [message])
            self.source.identities[message["id"]] = {"id": f"{seq:032x}"}
        self.prepare_stickers()
        first = self.service.stickers.start_requested(ACCOUNT, USER, "synthetic-page-1")
        self.assertTrue(wait_until(lambda: self.service.plugin.tasks[0].finished))
        self.assertEqual(len(self.inspections), 100)
        fingerprint = self.models.service_for("image")["fingerprint"]
        self.assertEqual(len(self.database.sticker_queue(USER, 1000, 2, fingerprint)), 3)
        again = self.service.stickers.start_requested(ACCOUNT, USER, "synthetic-page-1")
        self.assertEqual(again["taskId"], first["taskId"])
        self.assertEqual(len(self.inspections), 100)
        second = self.service.stickers.start_requested(ACCOUNT, USER, "synthetic-page-2")
        self.assertNotEqual(second["taskId"], first["taskId"])
        self.assertTrue(wait_until(lambda: self.service.plugin.tasks[1].finished))
        self.assertEqual(len(self.inspections), 103)
        self.assertFalse(self.database.sticker_work_pending(USER, 2, fingerprint))
        empty = self.service.stickers.start_requested(ACCOUNT, USER, "synthetic-page-3")
        self.assertEqual(empty, {"state": "no-work", "taskId": None})
        self.assertEqual(len(self.service.plugin.tasks), 2)

    def test_failure_reasons_distinguish_missing_media_empty_and_model_error(self):
        task = Mock(account=ACCOUNT, user=USER, token="synthetic_execution_stickers", cancelled=Event())
        self.prepare_stickers()
        with patch.object(self.source, "media", return_value={"error": "not-found"}):
            self.service.stickers._label(task)
        self.assertFalse(self.inspections)
        self.assertEqual({row["failure_reason"] for row in self.database._rows(
            "SELECT failure_reason FROM stickers WHERE sticker_id IN (?,?)", (DOG, CAT))}, {"media-unavailable"})
        self.runtime.inspect_media = lambda *args: {"text": ""}
        self.service.stickers._label(task)
        self.assertEqual({row["failure_reason"] for row in self.database._rows(
            "SELECT failure_reason FROM stickers WHERE sticker_id IN (?,?)", (DOG, CAT))}, {"empty-label"})
        self.models.model = "synthetic-next-model"
        with patch.object(self.runtime, "inspect_media", side_effect=RuntimeError("synthetic private provider body")):
            self.service.stickers._label(task)
        rows = self.database._rows("SELECT failure_reason,failures,description FROM stickers WHERE sticker_id IN (?,?)", (DOG, CAT))
        self.assertEqual({row["failure_reason"] for row in rows}, {"model-error"})
        self.assertTrue(all(row["failures"] == 1 and row["description"] is None for row in rows))
        with self.assertRaises(ValueError):
            self.database.sticker_failed(DOG, "x", "private arbitrary body")

    def test_sticker_schema_migration_preserves_old_retry_counts_as_unknown(self):
        import sticker_store
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(sticker_store.SCHEMA)
            connection.execute("INSERT INTO stickers(sticker_id,failures,failed_model_fp,updated_at_ms) VALUES('legacy',2,'old',1)")
            sticker_store.ensure_schema(connection)
            self.assertEqual(connection.execute("SELECT failures,failed_model_fp,failure_reason FROM stickers").fetchone(), (2, "old", None))

    def test_turn_writes_identities_and_official_captions(self):
        context = self.ask("first-question")
        self.assertIn(NOTE, context)
        self.assertIn("[表情#aaaaaa「晚安」·抹茶旦旦]", context)
        self.assertIn("[表情#bbbbbb", context)
        self.assertIn("[表情，内容未读取]", context)
        self.assertEqual(self.source.sticker_calls[0], [message["id"] for message in self.messages[:6]])
        # The captioned store sticker is never sent to the vision model for this turn.
        read = [message_id for message_id, _frames in self.source.frame_calls]
        self.assertNotIn(self.messages[0]["id"], read)
        observed = [request for request in self.inspections if not request.get("purpose")]
        self.assertEqual(observed[0]["sampling"], "animated-frames")
        self.assertEqual(len(observed[0]["images"]), 4)

    def test_background_task_labels_most_used_first_then_the_next_turn_uses_the_labels(self):
        self.ask("first-question")
        self.assertTrue(self.labelled(DOG, CAT))
        labels = [request for request in self.inspections if request.get("purpose") == "sticker-label"]
        self.assertEqual(len(labels), 2)  # One per distinct sticker; the captioned one needs none.
        self.assertEqual(labels[0]["sampling"], "animated-frames")
        self.assertEqual(labels[0]["agentId"], "sticker-labels")
        first_read = [message_id for message_id, count in self.source.frame_calls if count == 4]
        self.assertIn(self.messages[3]["id"], first_read)  # The latest message of the most used sticker.
        tasks = [task for task in self.service.plugin.tasks if task.task["kind"] == "background"]
        self.assertEqual([task.task["title"] for task in tasks], ["识别表情"])
        self.assertTrue(wait_until(lambda: tasks[0].finished))
        before = len(self.inspections)
        context = self.ask("second-question")
        self.assertIn("[表情#bbbbbb：合成小狗眨眼]", context)
        self.assertIn("[表情#cccccc：合成小狗眨眼]", context)
        self.assertEqual(self.inspections[before:], [])  # Labelled stickers are not read again in the turn.

    def test_failures_count_per_model_and_another_model_retries(self):
        self.ask("first-question")
        self.assertTrue(self.labelled(DOG, CAT))
        self.database.sticker_failed(CAT, "model-x")
        self.database._write("UPDATE stickers SET description=NULL WHERE sticker_id=?", (CAT,))
        self.database.sticker_failed(CAT, "model-x")
        self.assertEqual([item["id"] for item in self.database.sticker_queue(USER, 10, 2, "model-x")], [])
        self.assertEqual([item["id"] for item in self.database.sticker_queue(USER, 10, 2, "model-y")], [CAT])

    def test_model_change_before_labelling_stops_without_consuming_retries(self):
        task = Mock(account=ACCOUNT, user=USER, token="synthetic_execution_stickers", cancelled=Event())
        read_media = self.source.media

        def change_model(*args, **kwargs):
            self.models.model = "synthetic-replacement-model"
            return read_media(*args, **kwargs)

        with patch.object(self.source, "media", side_effect=change_model):
            with self.assertRaises(AdvisorError) as caught:
                self.service.stickers._label(task)
        self.assertEqual(caught.exception.code, "stopping")
        self.assertEqual(self.inspections, [])
        rows = self.database._rows("SELECT failures FROM stickers WHERE sticker_id IN (?,?)", (DOG, CAT))
        self.assertEqual([row["failures"] for row in rows], [0, 0])

    def test_changing_only_image_model_retries_labels_that_exhausted_the_previous_model(self):
        task = Mock(account=ACCOUNT, user=USER, token="synthetic_execution_stickers", cancelled=Event())
        self.service.stickers._label(task)
        main = self.models.service_for("main")
        self.database._write("UPDATE stickers SET description=NULL,failures=3,failed_model_fp=? "
                             "WHERE sticker_id IN (?,?)", (main["fingerprint"], DOG, CAT))
        image = {**main, "id": "image-model", "model": "image-model", "roles": ["image"],
                 "stamp": "image-stamp", "fingerprint": "image-fingerprint"}
        self.models.service_for = lambda role, refresh=False: image if role == "image" else main
        with patch.object(self.service.plugin, "start_task") as start:
            self.service.stickers._start(ACCOUNT, USER)
            start.assert_called_once()
        self.service.stickers._label(task)
        labels = self.database._rows("SELECT description,model_fp FROM stickers WHERE sticker_id IN (?,?)",
                                      (DOG, CAT))
        self.assertTrue(all(item["description"] for item in labels))
        self.assertEqual({item["model_fp"] for item in labels}, {image["fingerprint"]})

    def test_without_core_support_the_placeholder_remains_and_no_task_starts(self):
        del StickerCore.history_stickers
        try:
            context = self.ask("unsupported")
        finally:
            StickerCore.history_stickers = _history_stickers
        self.assertNotIn(NOTE, context)
        self.assertIn("占位", context)
        self.assertFalse([task for task in self.service.plugin.tasks if task.task["kind"] == "background"])


_history_stickers = StickerCore.history_stickers


if __name__ == "__main__":
    unittest.main()
