"""Deleting an import task archives local workflow state, never chat evidence."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from core_external_import import ExternalImportService
from core_tasks import TaskError
from external_import_http import post
from test_external_import import Supervisor, Tasks


class ExternalTaskDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = SimpleNamespace(tasks=Tasks(), supervisor=Supervisor(), backend=SimpleNamespace(),
            registry=SimpleNamespace(enabled=lambda _: True, get=lambda _: {'name': 'Synthetic import'}, dependency_error=lambda _: None),
            main_concurrency=lambda: 1)
        self.service = ExternalImportService(self.core, Path(self.temp.name) / 'data')
        self.core.external_import = self.service
        self.addCleanup(self.service.close)
        self.friend = self.service.store.create_friend('Synthetic task deletion friend')
        self.batch = self.service.store.create_batch(self.friend['id'], 'synthetic-source', 'synthetic-self', 'synthetic-peer')
        self.user_file = Path(self.temp.name) / 'synthetic-upload.png'
        Image.new('RGB', (600, 1400), 'white').save(self.user_file, 'PNG')
        with self.user_file.open('rb') as stream:
            uploaded = self.service.upload(self.batch['id'], self.user_file.name, stream, self.user_file.stat().st_size)
        self.original = self.service.store.material_path(uploaded['materials'][0]['id'])

    def wait_done(self):
        deadline = time.monotonic() + 8
        while self.service.runs and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.service.runs)
        return self.service.store.get_batch(self.batch['id'])

    def archive(self):
        return post(self.service, 'cleanup', {'batchId': self.batch['id'], 'kind': 'task'})

    def test_running_delete_waits_for_worker_and_keeps_records_and_source_evidence(self):
        self.service.start(self.batch['id'])
        batch = self.wait_done()
        result = self.service.store.save_batch(self.batch['id'])
        self.assertEqual((result['added'], result['pending']), (2, 2))
        history_before = self.service.store.messages(self.friend['id'])
        evidence = [self.service.store.evidence_path(segment['id']) for segment in batch['segments']]
        gate = self.core.supervisor.block = threading.Event()
        self.addCleanup(gate.set)
        self.core.supervisor.entered.clear()
        self.service.start(self.batch['id'], reprocess=True)
        self.assertTrue(self.core.supervisor.entered.wait(3))
        run = self.service.runs[self.batch['id']]
        self.assertFalse(self.service.store.cleanup_preview(self.batch['id'], 'task')['allowed'])
        with self.assertRaisesRegex(ValueError, '先停止'):
            self.archive()
        self.service.stop(self.batch['id'])
        with self.assertRaises(TaskError):
            self.core.tasks.verify(run['execution']['token'], 'kyf.external-chat-import', run['task']['id'])
        # The HTTP mutation guard also checks the live worker, even if its public
        # status has already changed; revocation alone does not archive a run.
        self.service.store.set_batch_state(self.batch['id'], 'interrupted')
        with self.assertRaisesRegex(ValueError, '先停止'):
            self.archive()
        self.assertFalse(self.service.store.get_batch(self.batch['id'])['archived'])
        gate.set()
        self.wait_done()
        run['thread'].join(3)
        self.assertFalse(run['thread'].is_alive())
        preview = post(self.service, 'cleanup-preview', {'batchId': self.batch['id'], 'kind': 'task'})
        self.assertTrue(preview['allowed'])
        self.assertEqual((preview['savedRecords'], preview['pendingCandidates'], preview['fileCount']), (2, 2, 0))
        self.archive()
        self.assertTrue(self.service.store.get_batch(self.batch['id'])['archived'])
        history_after = self.service.store.messages(self.friend['id'])
        self.assertEqual(history_after['messages'], history_before['messages'])
        self.assertEqual(len(history_after['gaps']), 2)
        self.assertTrue(all(gap['archived'] for gap in history_after['gaps']))
        self.assertTrue(all(path.is_file() for path in evidence))
        self.assertEqual(self.service.store.list_batches(self.friend['id']), [])
        self.assertTrue(self.original.is_file())
        self.assertTrue(self.user_file.is_file())
        with self.assertRaisesRegex(ValueError, '已移出列表'):
            self.service.start(self.batch['id'])


if __name__ == '__main__':
    unittest.main()
