"""Synthetic business provenance and isolation; no model or private data used."""
import concurrent.futures
import threading
import unittest
import uuid
from types import SimpleNamespace

from model_trace_context import current_trace_context, traced_execution, trace_bind_result, trace_record_outcome


class Service:
    transport_url = "http://127.0.0.1:12345"

    def __init__(self):
        self.registered, self.finished = [], []

    def register_execution(self, scope):
        value = {**scope, "taskId": str(uuid.uuid4()), "executionId": str(uuid.uuid4()),
                 "producerId": str(uuid.uuid4())}
        self.registered.append(value)
        return value

    def task_event(self, *args):
        pass

    def outcome(self, context, status, adoption):
        self.finished.append((context["taskId"], status, adoption))

    def unregister(self, producer):
        pass


class Worker:
    def __init__(self, service):
        self._model_traces = service

    @traced_execution("synthetic", lambda self, account, contact, gate=None:
                      {"accountId": account, "contactId": contact, "businessJobId": "durable-job"},
                      lambda *args, **kwargs: "done")
    def run(self, account, contact, gate=None):
        if gate:
            gate.wait(3)
        return current_trace_context()


class TraceContextTests(unittest.TestCase):
    def test_concurrent_scopes_and_new_execution_on_resume(self):
        service, barrier = Service(), threading.Barrier(10)
        worker = Worker(service)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda i: worker.run("a" + str(i), "c" + str(i), barrier), range(10)))
        self.assertEqual(len({item["taskId"] for item in results}), 10)
        self.assertEqual([item["accountId"] for item in results], ["a" + str(i) for i in range(10)])
        self.assertIsNone(current_trace_context())
        first, resumed = worker.run("a", "c"), worker.run("a", "c")
        self.assertNotEqual(first["executionId"], resumed["executionId"])
        self.assertEqual(first["businessJobId"], resumed["businessJobId"])
        self.assertTrue(all(row[1:] == ("success", "adopted") for row in service.finished))

    def test_diagnostic_failure_does_not_abort_business(self):
        service = SimpleNamespace(register_execution=lambda scope: (_ for _ in ()).throw(OSError("disk full")))
        self.assertIsNone(Worker(service).run("a", "c"))
        self.assertIsNone(current_trace_context())

    def test_context_snapshot_cannot_rebind_active_execution(self):
        class Scoped(Worker):
            @traced_execution("test", lambda self: {"accountId": "a", "contactId": "c"})
            def inspect(self):
                value = current_trace_context()
                value["accountId"] = "other"
                return current_trace_context()["accountId"]
        self.assertEqual(Scoped(Service()).inspect(), "a")

    def test_parser_result_is_bound_sanitized_and_bounded_before_queue(self):
        import json
        service = Service()
        captured = []
        service.record_outcome = lambda *args: captured.append(args)
        class Parser(Worker):
            @traced_execution("test", lambda self: {"accountId": "a", "contactId": "c"})
            def parse(self):
                trace_bind_result({"traceRecordId": "exact-attempt"})
                trace_record_outcome("success", result={"text": "token synthetic/key %73ynthetic%2Fkey",
                    "authorization": "hidden", "large": "x" * 100000,
                    "url": "https://example.invalid/?signature=private"}, config={"apiKey": "synthetic/key"})
        Parser(service).parse()
        self.assertEqual(captured[0][1:4], ("exact-attempt", "success", "pending"))
        serialized = json.dumps(captured[0][4])
        self.assertNotIn("synthetic/key", serialized)
        self.assertNotIn("%73ynthetic%2Fkey", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertNotIn("private", serialized)
        self.assertLess(len(serialized), 65536)

    def test_disabled_collection_does_not_inspect_or_serialize_parser_result(self):
        service = Service()
        service.capture_available = lambda context: False
        class BusinessResult(dict):
            pass
        result = BusinessResult({"body": "synthetic content"})
        class Parser(Worker):
            @traced_execution("test", lambda self: {"accountId": "a", "contactId": "c"})
            def parse(self):
                trace_bind_result({"traceRecordId": "exact-attempt"})
                trace_record_outcome("success", result=result)
        # Exceptions inside tracing fail open; count accesses rather than relying
        # on the tracing helper to propagate a sentinel exception.
        from unittest.mock import patch
        with patch.object(BusinessResult, "items", side_effect=AssertionError("inspected")) as items:
            Parser(service).parse()
        items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
