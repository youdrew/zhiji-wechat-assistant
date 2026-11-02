"""Synthetic accounting, pagination and backpressure regressions for the gateway ledger."""
import queue
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from usage_ledger import ACCOUNTING_COLUMNS, MAX_SAFE_INTEGER, SCHEMA, UsageLedger
from core_contracts import AdvisorError


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(Path(self.temp.name) / "usage.sqlite3")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def record(self, number, **fields):
        self.assertTrue(self.ledger.record({"attemptId": str(number), "at": number,
            "plugin": "test.plugin", "account": "a", "user": "friend", "model": "model-a",
            "status": "done", "inputTokens": 10, "outputTokens": 2, **fields}))

    def test_totals_and_model_choices_cover_all_pages_and_stay_account_scoped(self):
        for number in range(125):
            self.record(number, model="model-b" if number == 0 else "model-a")
        self.record(126, account="other", model="private-model")
        self.assertTrue(self.ledger.flush())
        filters = {"account": "a"}
        page = self.ledger.records(filters)
        self.assertEqual(len(page["records"]), 50)
        self.assertIsNotNone(page["next"])
        self.assertEqual(self.ledger.summary(filters)["requests"], 125)
        self.assertEqual(self.ledger.summary(filters)["inputTokens"], 1250)
        self.assertEqual(self.ledger.models({**filters, "model": "model-a"}), ["model-a", "model-b"])

    def test_self_scope_covers_new_legacy_and_active_calls_without_rewriting_history(self):
        self.record(1, plugin="kyf.self-analysis", user=None, protocol="responses", status="ok")
        self.record(2, plugin="kyf.self-analysis", user=None, protocol="responses", status="ok")
        self.record(3, plugin="kyf.self-analysis", user="friend", protocol="responses", status="ok")
        self.record(4, plugin="kyf.self-analysis", user=None, scope="multi", protocol="responses", status="ok")
        self.record(5, user=None, protocol="responses", status="ok")
        self.record(6, plugin="kyf.self-analysis", user=None, account="other", protocol="responses", status="ok")
        self.assertTrue(self.ledger.flush())
        with self.ledger.lock:
            self.ledger.connection.execute("UPDATE attempts SET scope='none',user='' WHERE id='2'")
        filters = {"account": "a", "friend": "self"}
        rows = self.ledger.records(filters)["records"]
        self.assertEqual([(row["attemptId"], row["scope"]) for row in rows], [("2", "self"), ("1", "self")])
        self.assertTrue(all(not row["user"] for row in rows))
        self.assertEqual(self.ledger.summary(filters)["requests"], 2)
        friends = self.ledger.by_friend(filters)
        self.assertEqual([(row["scope"], row["user"], row["requests"]) for row in friends], [("self", None, 2)])
        self.assertEqual(self.ledger.modules(filters)[0]["requests"], 2)
        self.assertEqual(self.ledger.models(filters), ["model-a"])
        buckets = self.ledger.timeline(filters, [0, 2, 4, 8])["buckets"]
        self.assertEqual([bucket["requests"] for bucket in buckets], [1, 1, 0])
        self.assertEqual(sum(bucket["totalTokens"] for bucket in buckets), self.ledger.summary(filters)["totalTokens"])
        for scope, expected in (("none", ["5"]), ("multi", ["4"]), ("friend", ["3"])):
            result = self.ledger.records({"account": "a", "friend": scope})["records"]
            self.assertEqual([row["attemptId"] for row in result], expected)
        self.assertEqual(self.ledger.summary({"account": "a"})["requests"], 5)
        self.assertTrue(self.attempt("self-live", "running", plugin="kyf.self-analysis", user=None, scope="none"))
        live = self.ledger.records({**filters, "status": "active"})["records"]
        self.assertEqual([(row["attemptId"], row["scope"], row["user"]) for row in live], [("self-live", "self", None)])
        self.assertEqual(self.ledger.records({"account": "a", "friend": "none", "status": "active"})["records"], [])
        self.assertEqual(self.ledger.summary(filters)["requests"], 2, "in-flight rows must not enter totals")
        with self.ledger.lock:
            self.assertEqual(self.ledger.connection.execute("SELECT scope FROM attempts WHERE id='2'").fetchone()[0], "none")

    def test_repeated_attempt_updates_do_not_double_count_or_erase_outcome(self):
        self.record(1)
        self.ledger.flush()
        self.ledger.set_outcome("1", "test.plugin", "adopted")
        self.record(1, inputTokens=15, outputTokens=4)
        self.record(2, inputTokens=None, outputTokens=3)
        self.ledger.flush()
        summary = self.ledger.summary({"account": "a"})
        self.assertEqual((summary["requests"], summary["inputTokens"], summary["outputTokens"]), (2, 15, 7))
        self.assertEqual(summary["missingUsage"], 1)
        rows = self.ledger.records({"account": "a"})["records"]
        self.assertEqual(next(row for row in rows if row["attemptId"] == "1")["outcome"], "adopted")

    def attempt(self, attempt_id, phase, owner=None, **fields):
        return self.ledger.track({"attemptId": attempt_id, "phase": phase, "at": 5_000, "plugin": "test.plugin",
                                  "account": "a", "user": "friend", "model": "model-a", "step": "advisor",
                                  "task": "task-1", **fields}, owner=owner)

    def test_in_flight_attempts_lead_the_first_page_without_entering_totals(self):
        self.record(1_000)
        self.ledger.flush()
        self.assertTrue(self.attempt("live", "queued"))
        rows = self.ledger.records({"account": "a"})["records"]
        self.assertEqual([(row["attemptId"], row["status"]) for row in rows], [("live", "queued"), ("1000", "done")])
        self.assertEqual((rows[0]["scope"], rows[0]["totalTokens"]), ("friend", None))
        self.assertTrue(self.attempt("live", "running", traceRecordId="trace-live"))
        self.assertEqual(self.ledger.records({"account": "a"})["records"][0]["traceRecordId"], "trace-live")
        self.assertEqual(self.ledger.summary({"account": "a"})["requests"], 1)
        self.assertEqual(self.ledger.summary({"account": "a", "status": "active"})["requests"], 0)
        for filters, visible in (({"status": "active"}, True), ({"status": "running"}, True), ({"status": "done"}, False),
                                 ({"friend": "friend"}, True), ({"friend": "none"}, False), ({"plugin": "other"}, False),
                                 ({"step": "advisor"}, True), ({"step": "__none__"}, False), ({"task": "task-1"}, True),
                                 ({"model": "model-b"}, False), ({"since": 5_001}, False), ({"until": 5_000}, False)):
            ids = [row["attemptId"] for row in self.ledger.records({"account": "a", **filters})["records"]]
            self.assertEqual("live" in ids, visible, filters)
        self.assertEqual(self.ledger.records({"account": "other"})["records"], [])
        self.assertNotIn("live", [row["attemptId"] for row in self.ledger.records({"account": "a"}, before=(2_000, "z"))["records"]])
        self.assertFalse(self.attempt("bad", "running", account=None))

    def test_a_finished_attempt_is_shown_once_while_its_row_is_written(self):
        self.attempt("live", "running")
        gate = threading.Event()
        original = self.ledger._write
        def slow(rows):
            if rows:
                gate.wait(5)
            original(rows)
        with patch.object(self.ledger, "_write", side_effect=slow):
            self.record(5_000, attemptId="live", status="ok")
            self.attempt("live", "ended")  # The gateway reports the end after the usage record.
            rows = self.ledger.records({"account": "a"})["records"]
            self.assertEqual([(row["attemptId"], row["status"]) for row in rows], [("live", "running")])
            gate.set()
            self.assertTrue(self.ledger.flush())
        rows = self.ledger.records({"account": "a"})["records"]
        self.assertEqual([(row["attemptId"], row["status"]) for row in rows], [("live", "ok")])
        self.assertEqual(self.ledger.active, {})

    def test_ended_or_orphaned_attempts_leave_the_list(self):
        first, second = object(), object()
        self.attempt("queued-only", "queued", owner=first)
        self.attempt("ended", "running", owner=first)
        self.attempt("other-generation", "running", owner=second)
        self.attempt("ended", "ended")
        self.ledger.forget(first)
        self.assertEqual([row["attemptId"] for row in self.ledger.records({"account": "a"})["records"]], ["other-generation"])

    def test_normalized_totals_preserve_provider_semantics_and_unknown_coverage(self):
        self.record(1, protocol="anthropic", inputTokens=10, outputTokens=5, cacheReadTokens=30,
                    cacheWriteTokens=20, totalInputTokens=60, totalOutputTokens=5, totalTokens=65,
                    totalTokensSource="derived", accountingVersion=1)
        # The provider total remains authoritative even when a component is unknown.
        self.record(2, protocol="gemini", inputTokens=40, outputTokens=7, reasoningTokens=3,
                    toolUsePromptTokens=None, totalInputTokens=None, totalOutputTokens=10,
                    totalTokens=50, totalTokensSource="provider", accountingVersion=1)
        self.record(3, protocol="chat_completions", inputTokens=100, outputTokens=12,
                    reasoningTokens=4, cacheReadTokens=80, totalInputTokens=100, totalOutputTokens=12,
                    totalTokens=112, totalTokensSource="provider", accountingVersion=1, status="stream-error")
        # New normalized unknown fields cannot fall back to a tempting input+output sum.
        self.record(4, protocol="chat_completions", accountingVersion=1,
                    totalInputTokens=None, totalOutputTokens=None, totalTokens=None)
        self.record(5, protocol="future-provider", accountingVersion=1, inputTokens=None, outputTokens=None,
                    totalTokens=9, totalTokensSource="provider", status="aborted")
        self.assertTrue(self.ledger.flush())
        summary = self.ledger.summary({"account": "a"})
        self.assertEqual((summary["requests"], summary["totalTokens"], summary["knownTotalRequests"]), (5, 236, 4))
        self.assertEqual((summary["totalInputTokens"], summary["totalOutputTokens"]), (160, 27))
        self.assertEqual((summary["missingInputUsage"], summary["missingOutputUsage"], summary["missingTotalUsage"]),
                         (3, 2, 1))
        self.assertEqual((summary["reasoningTokens"], summary["cacheReadTokens"], summary["cacheWriteTokens"]),
                         (7, 110, 20))
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["missingUsage"], 1)
        rows = self.ledger.records({"account": "a"})["records"]
        self.assertIsNone(next(item for item in rows if item["attemptId"] == "4")["totalTokens"])
        self.assertEqual(next(item for item in rows if item["attemptId"] == "2")["totalTokensSource"], "provider")

    def test_modules_steps_and_task_filters_use_all_attempts_including_failed_retries(self):
        for number in range(130):
            self.record(number, protocol="chat_completions", step="summary" if number % 2 else "answer",
                        task="task-1", status="http-error" if number == 0 else "done")
        self.record(130, protocol="ollama", plugin="other.plugin", step=None, task="task-2", inputTokens=2)
        self.record(131, account="other", protocol="ollama", plugin="other.plugin", inputTokens=999)
        self.assertTrue(self.ledger.flush())
        modules = self.ledger.modules({"account": "a"})
        self.assertEqual([item["plugin"] for item in modules], ["test.plugin", "other.plugin"])
        self.assertEqual((modules[0]["requests"], modules[0]["totalTokens"], modules[0]["lastAt"]), (130, 1560, 129))
        self.assertEqual([(item["step"], item["requests"], item["totalTokens"]) for item in modules[0]["steps"]],
                         [("answer", 65, 780), ("summary", 65, 780)])
        scoped = {"account": "a", "task": "task-1", "step": "answer", "since": 0, "until": 100}
        self.assertEqual(self.ledger.summary(scoped)["requests"], 50)
        self.assertEqual(self.ledger.summary(scoped)["failures"], 1)
        self.assertEqual(self.ledger.by_friend(scoped)[0]["totalTokens"], 600)
        self.assertEqual(self.ledger.models({**scoped, "model": "irrelevant"}), ["model-a"])
        self.assertEqual(self.ledger.summary({"account": "a", "step": "__none__"})["requests"], 1)

    def test_timeline_respects_calendar_boundaries_filters_and_zero_buckets_without_pagination(self):
        for number in range(200):
            self.record(number, protocol="chat_completions", plugin="a.plugin" if number % 2 else "b.plugin")
        self.record(201, account="other", protocol="chat_completions")
        self.assertTrue(self.ledger.flush())
        timeline = self.ledger.timeline({"account": "a", "since": 25, "until": 175}, [0, 50, 100, 150, 200, 250])
        self.assertEqual([item["requests"] for item in timeline["buckets"]], [25, 50, 50, 25, 0])
        self.assertEqual(sum(item["totalTokens"] for item in timeline["buckets"]), 1800)
        self.assertEqual((timeline["firstAt"], timeline["lastAt"]), (0, 199))
        self.assertEqual(timeline["bounds"], [0, 50, 100, 150, 200, 250])
        empty = timeline["buckets"][-1]
        self.assertEqual((empty["totalTokens"], empty["missingTotalUsage"], empty["knownTotalRequests"]), (0, 0, 0))
        self.assertEqual(empty["modules"], [])
        self.assertEqual(empty["lastAt"], None)
        for bucket in timeline["buckets"]:
            self.assertEqual(sum(module["requests"] for module in bucket["modules"]), bucket["requests"])
            self.assertEqual(sum(module["totalTokens"] for module in bucket["modules"]), bucket["totalTokens"])
        summary = self.ledger.summary({"account": "a", "since": 25, "until": 175})
        self.assertEqual((summary["firstAt"], summary["lastAt"]), (0, 199))

    def test_variable_calendar_intervals_and_large_timeline_use_constant_query_count(self):
        # DST fall-back can create a 25-hour day; the ledger uses the supplied
        # elapsed boundaries rather than making its own timezone assumptions.
        hour = 3_600_000
        self.record(1, at=24 * hour, protocol="ollama")
        self.record(2, at=25 * hour, protocol="ollama")
        self.assertTrue(self.ledger.flush())
        self.assertEqual([item["requests"] for item in self.ledger.timeline(
            {"account": "a"}, [0, 25 * hour, 49 * hour])["buckets"]], [1, 1])
        queries = []
        with self.ledger.lock:
            self.ledger.connection.set_trace_callback(queries.append)
            try:
                timeline = self.ledger.timeline({"account": "a"}, list(range(1001)))
            finally:
                self.ledger.connection.set_trace_callback(None)
        self.assertEqual(len(timeline["buckets"]), 1000)
        self.assertEqual(len(queries), 3, "timeline queries must not grow with the number of buckets")
        for bounds in ([1], [1, 1], [2, 1], [-1, 2], [0, MAX_SAFE_INTEGER + 1], [False, 1]):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                self.ledger.timeline({"account": "a"}, bounds)

    def test_legacy_schema_migrates_conservatively_and_preserves_normalized_unknowns(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        schema = SCHEMA
        for column, kind in ACCOUNTING_COLUMNS.items():
            schema = schema.replace(f"  {column} {kind},\n", "")
        with closing(sqlite3.connect(path)) as database:
            database.executescript(schema)
            for number, protocol, cache_write, reasoning in (
                    (1, "chat_completions", 20, 8), (2, "anthropic", 20, None),
                    (3, "anthropic", None, None), (4, "gemini", None, 8),
                    (5, "ollama", None, None), (6, "future-provider", None, None)):
                database.execute(
                    "INSERT INTO attempts(id,at_ms,plugin,account,scope,status,protocol,input_tokens,output_tokens,"
                    "cache_read_tokens,cache_write_tokens,reasoning_tokens,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(number), number, "test.plugin", "a", "none", "done", protocol, 10, 5, 30,
                     cache_write, reasoning, "adopted"))
            database.commit()
        migrated = UsageLedger(path)
        try:
            rows = {item["attemptId"]: item for item in migrated.records({"account": "a"})["records"]}
            self.assertEqual([rows[str(number)]["totalTokens"] for number in range(1, 7)], [15, 65, None, None, 15, None])
            self.assertEqual(rows["4"]["totalOutputTokens"], 13)
            self.assertIsNone(rows["4"]["totalInputTokens"])
            self.assertTrue(all(item["outcome"] == "adopted" for item in rows.values()))
            self.assertTrue(all(item["accountingVersion"] == 0 for item in rows.values()))
            migrated.record({"attemptId": "new", "at": 7, "plugin": "test.plugin", "account": "a", "scope": "none",
                             "status": "done", "protocol": "chat_completions", "inputTokens": 10, "outputTokens": 5,
                             "totalTokens": None, "accountingVersion": 1})
            self.assertTrue(migrated.flush())
        finally:
            migrated.close()
        reopened = UsageLedger(path)
        try:
            new = next(item for item in reopened.records({"account": "a"})["records"] if item["attemptId"] == "new")
            self.assertIsNone(new["totalTokens"])
            self.assertEqual(new["accountingVersion"], 1)
        finally:
            reopened.close()

    def test_failed_migration_rolls_back_schema_and_usage_and_closes_connection(self):
        path = Path(self.temp.name) / "failed-migration.sqlite3"
        schema = SCHEMA
        for column, kind in ACCOUNTING_COLUMNS.items():
            schema = schema.replace(f"  {column} {kind},\n", "")
        with closing(sqlite3.connect(path)) as database:
            database.executescript(schema)
            database.execute("INSERT INTO attempts(id,at_ms,plugin,account,scope,status,protocol,input_tokens,output_tokens) "
                             "VALUES('legacy',1,'test.plugin','a','none','done','responses',10,5)")
            database.commit()
        opened = []

        class FailedMigrationConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("UPDATE attempts SET total_tokens="):
                    raise sqlite3.OperationalError("synthetic migration failure")
                return super().execute(sql, *args, **kwargs)

        connect = sqlite3.connect

        def failing_connection(*args, **kwargs):
            connection = connect(*args, **kwargs, factory=FailedMigrationConnection)
            opened.append(connection)
            return connection

        with patch("usage_ledger.sqlite3.connect", side_effect=failing_connection):
            with self.assertRaises(sqlite3.OperationalError):
                UsageLedger(path)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        with closing(sqlite3.connect(path)) as database:
            columns = {row[1] for row in database.execute("PRAGMA table_info(attempts)")}
            self.assertFalse(columns.intersection(ACCOUNTING_COLUMNS))
            self.assertEqual(database.execute("SELECT input_tokens,output_tokens FROM attempts").fetchone(), (10, 5))
        recovered = UsageLedger(path)
        try:
            self.assertEqual(recovered.summary({"account": "a"})["totalTokens"], 15)
        finally:
            recovered.close()

    def test_overflow_never_waits_for_database_lock_and_records_the_gap(self):
        returned = threading.Event()
        result = []
        with patch.object(self.ledger.queue, "put_nowait", side_effect=queue.Full):
            with self.ledger.lock:
                worker = threading.Thread(target=lambda: (result.append(self.ledger.record(
                    {"attemptId": "overflow", "plugin": "test.plugin"})), returned.set()))
                worker.start()
                finished = returned.wait(1)
            worker.join(2)
        self.assertTrue(finished, "record() waited for the ledger's database lock")
        self.assertEqual(result, [False])
        self.ledger.flush()
        self.assertEqual(self.ledger.summary({"account": "a"})["droppedRecords"], 1)

    def test_flush_waits_for_a_dequeued_batch_to_commit(self):
        entered, release, flushed = threading.Event(), threading.Event(), threading.Event()
        original = self.ledger._write

        def blocked(rows):
            if rows:
                entered.set()
                release.wait(3)
            original(rows)

        with patch.object(self.ledger, "_write", side_effect=blocked):
            self.record(1)
            self.assertTrue(entered.wait(2))
            thread = threading.Thread(target=lambda: (self.ledger.flush(), flushed.set()))
            thread.start()
            try:
                self.assertFalse(flushed.wait(.1), "flush returned before the in-flight batch committed")
            finally:
                release.set()
                thread.join(3)
        self.assertTrue(flushed.is_set())
        self.assertEqual(self.ledger.summary({"account": "a"})["requests"], 1)

    def test_clear_reports_busy_instead_of_allowing_old_records_to_reappear(self):
        entered, release = threading.Event(), threading.Event()
        original = self.ledger._write

        def blocked(rows):
            if rows:
                entered.set()
                release.wait(5)
            original(rows)

        with patch.object(self.ledger, "_write", side_effect=blocked):
            self.record(1)
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(AdvisorError) as busy:
                    self.ledger.clear("a")
                self.assertEqual(busy.exception.code, "usage-busy")
            finally:
                release.set()
        self.assertTrue(self.ledger.flush())
        self.ledger.clear("a")
        self.assertEqual(self.ledger.summary({"account": "a"})["requests"], 0)

    def test_flush_timeout_includes_database_lock_and_close_rejects_new_records(self):
        with self.ledger.lock:
            self.assertFalse(self.ledger.flush(.05))
        self.assertTrue(self.ledger.flush())
        self.ledger.close()
        self.assertFalse(self.ledger.record({"attemptId": "late", "plugin": "test.plugin"}))
        self.assertFalse(self.ledger.writer.is_alive())
        self.assertEqual(self.ledger.queue.unfinished_tasks, 0)

    def test_close_drains_a_record_accepted_after_the_writer_observed_an_empty_queue(self):
        empty_observed, release, closing_started = threading.Event(), threading.Event(), threading.Event()
        original_get = self.ledger.queue.get
        original_join = self.ledger.writer.join

        def paused_get(*args, **kwargs):
            try:
                return original_get(*args, **kwargs)
            except queue.Empty:
                empty_observed.set()
                release.wait(3)
                raise

        def observed_join(timeout):
            closing_started.set()  # close() has already rejected further record() calls.
            return original_join(timeout)

        closer = threading.Thread(target=self.ledger.close)
        with patch.object(self.ledger.queue, "get", side_effect=paused_get), \
                patch.object(self.ledger.writer, "join", side_effect=observed_join):
            try:
                self.assertTrue(empty_observed.wait(2))
                self.record(1)
                closer.start()
                self.assertTrue(closing_started.wait(1))
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(self.ledger.writer.is_alive())
        self.assertEqual(self.ledger.queue.unfinished_tasks, 0)
        with closing(sqlite3.connect(self.ledger.path)) as database:
            persisted = database.execute("SELECT id FROM attempts").fetchall()
        self.assertEqual(persisted, [("1",)])


if __name__ == "__main__":
    unittest.main()
