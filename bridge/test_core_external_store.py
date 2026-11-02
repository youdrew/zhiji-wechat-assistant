"""External chat publication, provenance and cleanup invariants (synthetic data)."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core_external_store import ExternalChatStore


class ExternalChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ExternalChatStore(self.root / 'local')
        self.friend = self.store.create_friend('Synthetic friend')
        self.file = self.root / 'synthetic.png'
        self.file.write_bytes(b'synthetic image bytes, no actual conversation')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def batch(self, **kwargs):
        return self.store.create_batch(self.friend['id'], 'synthetic-source', 'my-account', 'conversation-1', **kwargs)

    def observe(self, batch, candidates, path=None, segment_key='frame-0', order=0):
        material = self.store.add_material(batch['id'], path or self.file)
        segment = self.store.add_segment(batch['id'], material['id'], segment_key, order, path or self.file)
        self.store.submit_segment(batch['id'], segment['id'], candidates)
        return self.store.set_batch_state(batch['id'], 'review')

    def synthetic_frame(self, name, photo_top=200, photo_bottom=420):
        """A flat chat background with one textured picture and a text bubble below it."""
        import random
        from PIL import Image, ImageDraw
        random.seed(7)
        frame = Image.new('RGB', (400, 800), (242, 242, 242))
        draw = ImageDraw.Draw(frame)
        for y in range(max(0, photo_top), min(800, photo_bottom)):
            for x in range(80, 260):
                frame.putpixel((x, y), (random.randrange(40, 200), random.randrange(60, 220), random.randrange(30, 180)))
        draw.rectangle((80, 440, 300, 480), fill=(255, 255, 255))
        for x in range(92, 280, 9):
            draw.line((x, 452, x + 4, 468), fill=(30, 30, 30), width=1)
        path = self.root / name
        frame.save(path)
        return path

    def test_sent_picture_is_cut_from_its_frame_and_kept_with_the_saved_record(self):
        from PIL import Image
        frame = self.synthetic_frame('frame.png')
        # The recognizer's box is rough: it also covers the text bubble under the picture.
        batch = self.observe(self.batch(), [{'text': '[图片]', 'sender': 'other', 'kind': 'image', 'issues': ['missing_original'],
                                             'bbox': [.15, .24, .6, .38]}, {'text': 'Caption', 'sender': 'other', 'bbox': [.2, .55, .55, .05]}],
                             path=frame)
        picture, text = batch['candidates']
        self.assertIsNone(self.store.picture(candidate_id=text['id']))
        with Image.open(self.store.picture(candidate_id=picture['id'])) as crop:
            self.assertLessEqual(abs(crop.width - 180), 6)
            self.assertLessEqual(abs(crop.height - 220), 6)
        self.store.save_batch(batch['id'])
        message = next(m for m in self.store.messages(self.friend['id'])['messages'] if m['kind'] == 'image')
        self.assertRegex(message['media'], '^[0-9a-f]{64}$')
        self.store.cleanup(batch['id'], 'evidence')
        self.assertIsNotNone(self.store.picture(message_id=message['id']))

    def test_the_most_complete_view_of_a_picture_is_kept(self):
        from PIL import Image
        from core_external_media import crop_picture
        partial, whole = self.synthetic_frame('partial.png', -60, 160), self.synthetic_frame('whole.png')
        target = self.root / 'picture.png'
        self.assertTrue(crop_picture([(partial, [.15, 0, .6, .25]), (whole, [.15, .24, .6, .3])], target))
        with Image.open(target) as crop:
            self.assertGreater(crop.height, 200)
        self.assertFalse(crop_picture([(whole, [.7, .05, .2, .1])], self.root / 'nothing.png'))

    def test_source_name_is_freeform_and_reused_without_a_platform_catalog(self):
        name = '我的来源 / 自定义 42'
        first = self.store.create_batch(self.friend['id'], '  ' + name + '  ', 'self', 'peer')
        self.assertEqual(first['source']['platform'], name)
        second = self.store.create_batch(self.friend['id'], name, 'self', 'peer')
        self.assertEqual(second['source']['id'], first['source']['id'])
        first = self.observe(first, [{'text': '自定义来源中的聊天', 'sender': 'other'}])
        self.store.save_batch(first['id'])
        self.assertEqual(len(self.store.messages(self.friend['id'], ' ' + name + ' ')['messages']), 1)
        self.assertEqual(self.store.messages(self.friend['id'], '另一个来源')['messages'], [])
        moved = self.store.reassociate_batch(first['id'], self.friend['id'], '新的来源名', 'self', 'peer')
        self.assertEqual(moved['source']['platform'], '新的来源名')
        self.assertEqual(len(self.store.messages(self.friend['id'], '新的来源名')['messages']), 1)
        self.assertEqual(self.store.messages(self.friend['id'], name)['messages'], [])

    def test_existing_source_values_survive_restart_and_remain_appendable(self):
        batch = self.observe(self.batch(), [{'text': '已有记录', 'sender': 'self'}])
        self.store.save_batch(batch['id'])
        # Simulate a persisted identifier from an earlier release. It is not a catalog key.
        stored_name = 'legacy_source_id_7'
        self.store.connection.execute('UPDATE sources SET platform=? WHERE id=?',
                                      (stored_name, batch['source']['id']))
        self.store.close()
        self.store = ExternalChatStore(self.root / 'local')
        self.assertEqual(self.store.get_batch(batch['id'])['source']['platform'], stored_name)
        self.assertEqual(len(self.store.messages(self.friend['id'], stored_name)['messages']), 1)
        appended = self.store.create_batch(self.friend['id'], stored_name, 'my-account', 'conversation-1')
        self.assertEqual(appended['source']['id'], batch['source']['id'])

    def test_source_name_requires_bounded_nonempty_text_for_mutations_and_filters(self):
        batch = self.batch()
        invalid_names = ('', '  ', 'x' * 81, 'source\x00name', 7, [], {})
        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.store.create_batch(self.friend['id'], name, 'self', 'peer')
                with self.assertRaises(ValueError):
                    self.store.messages(self.friend['id'], name)
                with self.assertRaises(ValueError):
                    self.store.reassociate_batch(batch['id'], self.friend['id'], name, 'self', 'peer')
        with self.assertRaises(ValueError):
            self.store.create_batch(self.friend['id'], None, 'self', 'peer')
        self.assertEqual(len(self.store.list_batches(self.friend['id'])), 1)
        self.assertEqual(self.store.get_batch(batch['id'])['source'], batch['source'])
        longest = self.store.create_batch(self.friend['id'], '源' * 80, 'self', 'peer')
        self.assertEqual(longest['source']['platform'], '源' * 80)

    def test_partial_save_is_idempotent_unknown_time_and_later_order(self):
        batch = self.observe(self.batch(), [
            {'text': 'First', 'sender': 'other'},
            {'text': 'Middle', 'sender': 'unknown'},
            {'text': 'Last', 'sender': 'self', 'issues': ['incomplete_time'], 'dateLabel': '昨天'},
        ])
        result = self.store.save_batch(batch['id'])
        self.assertEqual((result['added'], result['pending'], result['batch']['state']), (2, 1, 'partial'))
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 0)
        history = self.store.messages(self.friend['id'])
        self.assertEqual(len(history['gaps']), 1)
        self.assertEqual([m['text'] for m in history['messages']], ['First', 'Last'])
        self.assertIsNone(history['messages'][1]['time'])
        self.store.update_candidate(batch['id'], batch['candidates'][1]['id'], {'sender': 'other'}, resolve=True)
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 1)
        history = self.store.messages(self.friend['id'])
        self.assertEqual([m['text'] for m in history['messages']], ['First', 'Middle', 'Last'])
        self.assertEqual(history['gaps'], [])

    def test_server_gate_cannot_be_bypassed_by_clearing_issues(self):
        batch = self.observe(self.batch(), [{'text': 'Truncated', 'sender': 'other', 'issues': ['bubble-truncated'], 'truncated': True}])
        candidate_id = batch['candidates'][0]['id']
        self.store.update_candidate(batch['id'], candidate_id, {'issues': [], 'truncated': False})
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 0)
        with self.assertRaises(ValueError):
            self.store.update_candidate(batch['id'], candidate_id, {'text': 'Cut □'}, resolve=True)
        # Leaving a cut-off bubble unedited is the user's explicit confirmation of its visible text.
        self.store.update_candidate(batch['id'], candidate_id, {}, resolve=True)
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 1)
        self.assertEqual(self.store.messages(self.friend['id'])['messages'][0]['text'], 'Truncated')

    def test_editing_ready_candidate_requires_confirmation(self):
        batch = self.observe(self.batch(), [{'text': 'Ready', 'sender': 'self'}])
        candidate_id = batch['candidates'][0]['id']
        self.store.update_candidate(batch['id'], candidate_id, {'text': 'Draft correction'})
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 0)
        self.store.update_candidate(batch['id'], candidate_id, {'text': 'Draft correction'}, resolve=True)
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 1)

    def test_undo_restores_deleted_and_confirmed_candidates_exactly(self):
        batch = self.observe(self.batch(), [{'text': 'Keep', 'sender': 'self'}, {'text': 'Who', 'sender': 'unknown', 'issues': ['unclear_text']}])
        kept, doubt = batch['candidates']
        deleted = self.store.update_candidate(batch['id'], kept['id'], {'ignored': True})
        restored = self.store.restore_candidate(batch['id'], kept['id'], kept['version'], deleted['version'])
        self.assertEqual((restored['ignored'], restored['ready'], restored['version']), (False, True, deleted['version'] + 1))
        confirmed = self.store.update_candidate(batch['id'], doubt['id'], {'sender': 'other', 'text': 'Who?'}, resolve=True)
        self.assertTrue(confirmed['ready'])
        restored = self.store.restore_candidate(batch['id'], doubt['id'], doubt['version'], confirmed['version'])
        self.assertEqual((restored['sender'], restored['text'], restored['issues'], restored['ready']),
                         ('unknown', 'Who', ['unclear_text'], False))
        self.assertFalse(restored.get('reviewed'))
        self.assertEqual(self.store.versions('candidate', doubt['id'])[-1]['reason'], 'user-undo')

    def test_undo_refuses_stale_saved_or_foreign_candidates(self):
        batch = self.observe(self.batch(), [{'text': 'One', 'sender': 'self'}])
        other = self.observe(self.batch(), [{'text': 'Two', 'sender': 'self'}])
        candidate = batch['candidates'][0]
        first = self.store.update_candidate(batch['id'], candidate['id'], {'text': 'One edited'})
        second = self.store.update_candidate(batch['id'], candidate['id'], {'text': 'One again'})
        for version, expected in ((candidate['version'], first['version']), (second['version'], second['version']),
                                  (True, second['version']), ('1', second['version'])):
            with self.subTest(version=version, expected=expected), self.assertRaises(ValueError):
                self.store.restore_candidate(batch['id'], candidate['id'], version, expected)
        with self.assertRaises(ValueError):
            self.store.restore_candidate(other['id'], candidate['id'], candidate['version'], second['version'])
        self.store.update_candidate(batch['id'], candidate['id'], {}, resolve=True)
        self.store.save_batch(batch['id'])
        saved = self.store.get_batch(batch['id'])['candidates'][0]
        with self.assertRaises(ValueError):
            self.store.restore_candidate(batch['id'], candidate['id'], candidate['version'], saved['version'])
        self.assertEqual(self.store.get_batch(batch['id'])['candidates'][0]['text'], 'One again')

    def test_order_keys_sort_between_their_neighbors(self):
        from core_external_store import _order_between
        keys = ['000000000000:000000000000', '000000000000:000000000001', '000000000001:000000000000']
        for low, high in [('', keys[0]), (keys[0], keys[1]), (keys[1], keys[2]), (keys[2], None)]:
            for _ in range(60):
                middle = _order_between(low, high)
                self.assertTrue(low < middle and (high is None or middle < high), (low, middle, high))
                self.assertFalse(middle.endswith('/'))
                low = middle

    def test_moved_candidate_keeps_its_place_in_review_save_and_undo(self):
        batch = self.observe(self.batch(), [{'text': text, 'sender': 'self'} for text in ('A', 'B', 'C')])
        a, b, c = batch['candidates']
        self.store.move_candidate(batch['id'], c['id'], a['id'])
        self.assertEqual([x['text'] for x in self.store.get_batch(batch['id'])['candidates']], ['A', 'C', 'B'])
        moved = self.store.move_candidate(batch['id'], b['id'], None)
        self.assertEqual([x['text'] for x in self.store.get_batch(batch['id'])['candidates']], ['B', 'A', 'C'])
        self.store.restore_candidate(batch['id'], b['id'], b['version'], moved['version'])
        self.assertEqual([x['text'] for x in self.store.get_batch(batch['id'])['candidates']], ['A', 'C', 'B'])
        with self.assertRaises(ValueError):
            self.store.move_candidate(batch['id'], a['id'], 'candidate-elsewhere')
        self.store.save_batch(batch['id'])
        self.assertEqual([m['text'] for m in self.store.messages(self.friend['id'])['messages']], ['A', 'C', 'B'])
        with self.assertRaises(ValueError):
            self.store.move_candidate(batch['id'], a['id'], None)

    def test_unsaved_moved_candidate_stays_in_place_as_a_gap(self):
        batch = self.observe(self.batch(), [{'text': 'A', 'sender': 'self'}, {'text': 'B', 'sender': 'self'}, {'text': 'C?', 'sender': 'unknown'}])
        a, _, c = batch['candidates']
        self.store.move_candidate(batch['id'], c['id'], a['id'])
        self.store.save_batch(batch['id'])
        history = self.store.messages(self.friend['id'])
        rows = sorted([('m', m['text'], m['order']) for m in history['messages']] + [('g', g['candidateId'], g['order']) for g in history['gaps']], key=lambda r: r[2])
        self.assertEqual([r[1] for r in rows], ['A', c['id'], 'B'])

    def test_real_repeated_text_is_not_auto_deduplicated(self):
        batch = self.observe(self.batch(), [{'text': 'Again', 'sender': 'self'}, {'text': 'Again', 'sender': 'self'}])
        second = batch['candidates'][1]
        self.assertIn('possible_duplicate', second['issues'])
        self.store.update_candidate(batch['id'], second['id'], {'duplicateDecision': 'new'}, resolve=True)
        self.assertEqual(self.store.save_batch(batch['id'])['added'], 2)

    def test_same_batch_explicit_overlap_reuses_first_message(self):
        batch = self.observe(self.batch(), [{'text': 'Overlap', 'sender': 'other'}, {'text': 'Overlap', 'sender': 'other'}])
        self.store.update_candidate(batch['id'], batch['candidates'][1]['id'], {'duplicateDecision': 'reuse'}, resolve=True)
        result = self.store.save_batch(batch['id'])
        self.assertEqual((result['added'], result['reused']), (1, 1))
        self.assertEqual(len(self.store.messages(self.friend['id'])['messages']), 1)

    def test_exact_material_duplicate_shares_record_and_undo_preserves_other_batch(self):
        observation = [{'text': 'Shared', 'sender': 'other', 'bbox': [0.1, 0.2, 0.5, 0.1]}]
        a = self.observe(self.batch(), observation)
        self.store.save_batch(a['id'])
        b = self.observe(self.batch(), observation)
        result = self.store.save_batch(b['id'])
        self.assertEqual((result['added'], result['reused']), (0, 1))
        undo = self.store.undo_batch(a['id'])
        self.assertEqual((undo['removed'], undo['preservedShared']), (0, 1))
        history = self.store.messages(self.friend['id'])
        self.assertEqual(len(history['messages']), 1)
        self.assertEqual(history['messages'][0]['batchIds'], [b['id']])

    def test_correction_versions_and_rerecognition_preserve_saved_content(self):
        a = self.observe(self.batch(), [{'text': 'Recognition', 'sender': 'other', 'bbox': [0.1, 0.2, 0.5, 0.1]}])
        self.store.save_batch(a['id'])
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.store.correct_message(message['id'], {'text': 'Human correction'})
        self.store.submit_segment(a['id'], a['segments'][0]['id'], [{'text': 'New recognition', 'sender': 'other', 'bbox': [0.1, 0.2, 0.5, 0.1]}])
        self.assertEqual(self.store.messages(self.friend['id'])['messages'][0]['text'], 'Human correction')
        candidate = self.store.get_batch(a['id'])['candidates'][0]
        self.assertEqual(candidate['recognitionSuggestion']['text'], 'New recognition')
        self.assertEqual(len(self.store.versions('message', message['id'])), 2)
        self.assertEqual(self.store.get_batch(a['id'])['counts']['pending'], 1)
        self.store.update_candidate(a['id'], candidate['id'], {'duplicateDecision': 'reuse'}, resolve=True)
        self.assertEqual(self.store.get_batch(a['id'])['counts']['pending'], 0)

    def test_unconfirmed_manual_edit_survives_retry(self):
        a = self.observe(self.batch(), [{'text': 'Original', 'sender': 'other'}])
        self.store.update_candidate(a['id'], a['candidates'][0]['id'], {'text': 'Unconfirmed edit'})
        self.store.submit_segment(a['id'], a['segments'][0]['id'], [{'text': 'Retry text', 'sender': 'other'}])
        candidate = self.store.get_batch(a['id'])['candidates'][0]
        self.assertEqual(candidate['text'], 'Unconfirmed edit')
        self.assertEqual(candidate['recognitionSuggestion']['text'], 'Retry text')

    def test_cross_batch_recognition_never_overwrites_saved_correction(self):
        original = [{'text': 'Original', 'sender': 'other', 'bbox': [0.1, 0.2, 0.3, 0.4]}]
        a = self.observe(self.batch(), original)
        self.store.save_batch(a['id'])
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.store.correct_message(message['id'], {'text': 'Corrected'})
        b = self.observe(self.batch(), original)
        self.assertEqual(self.store.save_batch(b['id'])['added'], 0)
        candidate = b['candidates'][0]
        self.store.update_candidate(b['id'], candidate['id'], {'duplicateDecision': 'reuse'}, resolve=True)
        self.assertEqual(self.store.save_batch(b['id'])['reused'], 1)
        self.assertEqual(self.store.messages(self.friend['id'])['messages'][0]['text'], 'Corrected')

    def test_notes_do_not_overwrite_and_search_filters(self):
        a = self.observe(self.batch(note='Batch one', source_note='Source note'), [{'text': 'Find needle', 'sender': 'self'}])
        self.store.save_batch(a['id'])
        b = self.batch(note='Batch two', source_note='Must not replace')
        self.assertEqual(b['source']['note'], 'Source note')
        self.store.update_notes(b['id'], note='Changed batch only')
        self.assertEqual(self.store.get_batch(a['id'])['note'], 'Batch one')
        self.assertEqual(len(self.store.messages(self.friend['id'], 'synthetic-source', a['id'], 'NEEDLE')['messages']), 1)
        self.assertEqual(self.store.messages(self.friend['id'], 'another-source')['messages'], [])

    def test_cleaning_evidence_and_originals_are_separate_and_never_touch_upload(self):
        a = self.observe(self.batch(), [{'text': 'Saved', 'sender': 'other'}])
        self.assertFalse(self.store.cleanup_preview(a['id'], 'originals')['allowed'])
        self.store.save_batch(a['id'])
        self.assertTrue(self.store.cleanup(a['id'], 'originals')['completed'])
        self.assertTrue(self.file.is_file())
        evidence = self.store.messages(self.friend['id'])['messages'][0]['evidence'][0]
        self.assertFalse(evidence['originalAvailable'])
        self.assertTrue(evidence['evidenceAvailable'])
        self.store.cleanup(a['id'], 'evidence')
        evidence = self.store.messages(self.friend['id'])['messages'][0]['evidence'][0]
        self.assertFalse(evidence['evidenceAvailable'])
        self.assertEqual(self.store.messages(self.friend['id'])['messages'][0]['text'], 'Saved')

    def test_shared_material_cleanup_is_blocked(self):
        a = self.observe(self.batch(), [{'text': 'Saved', 'sender': 'other'}])
        self.store.save_batch(a['id'])
        b = self.observe(self.batch(), [{'text': 'Saved', 'sender': 'other'}])
        self.store.save_batch(b['id'])
        for kind in ('originals', 'evidence'):
            preview = self.store.cleanup_preview(a['id'], kind)
            self.assertIn('shared-material', preview['blockers'])
            with self.assertRaises(ValueError):
                self.store.cleanup(a['id'], kind)

    def test_reassociation_clones_shared_message_and_retains_correction(self):
        a = self.observe(self.batch(), [{'text': 'Shared', 'sender': 'other'}])
        self.store.save_batch(a['id'])
        b = self.observe(self.batch(), [{'text': 'Shared', 'sender': 'other'}])
        self.store.save_batch(b['id'])
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.store.correct_message(message['id'], {'text': 'Corrected shared'})
        other = self.store.create_friend('Another synthetic friend')
        self.store.reassociate_batch(b['id'], other['id'], 'another-source', 'second-account', 'another-conversation')
        old = self.store.messages(self.friend['id'])['messages']
        new = self.store.messages(other['id'])['messages']
        self.assertEqual(len(old), 1)
        self.assertEqual(len(new), 1)
        self.assertNotEqual(old[0]['id'], new[0]['id'])
        self.assertEqual(new[0]['text'], 'Corrected shared')
        self.assertEqual(len(self.store.versions('message', new[0]['id'])), 2)

    def test_explicit_identity_and_restart_preserve_progress(self):
        duplicate_name = self.store.create_friend('Synthetic friend')
        self.assertNotEqual(duplicate_name['id'], self.friend['id'])
        batch = self.observe(self.batch(direction='reverse'), [{'text': 'First', 'sender': 'self'}])
        with self.assertRaises(ValueError):
            self.store.create_batch(duplicate_name['id'], 'synthetic-source', 'my-account', 'conversation-1')
        self.store.set_batch_state(batch['id'], 'running')
        self.assertEqual(self.store.mark_interrupted(), 1)
        after = self.store.get_batch(batch['id'])
        self.assertEqual((after['state'], after['direction'], after['progress']['done']), ('interrupted', 'reverse', 1))
        self.store.close()
        self.store = ExternalChatStore(self.root / 'local')
        self.assertEqual(self.store.get_batch(batch['id'])['candidates'][0]['text'], 'First')

    def test_media_visible_details_are_stored_without_fake_playable_media(self):
        a = self.observe(self.batch(), [{'text': '[语音 8秒]', 'sender': 'other', 'kind': 'voice', 'quote': '可见引用', 'issues': ['missing_original']}])
        self.assertEqual(self.store.save_batch(a['id'])['added'], 1)
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual((message['kind'], message['quote']), ('voice', '可见引用'))
        self.assertNotIn('mediaUrl', message)

    def test_concurrent_confirmation_cannot_duplicate_messages(self):
        a = self.observe(self.batch(), [{'text': 'Once', 'sender': 'self'}])
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.store.save_batch(a['id']), range(2)))
        self.assertEqual(sum(r['added'] for r in results), 1)
        self.assertEqual(len(self.store.messages(self.friend['id'])['messages']), 1)

    def test_abandon_task_then_cleanup_preserves_unsaved_gap_and_original_upload(self):
        saved = self.observe(self.batch(), [{'text': 'Kept', 'sender': 'self'}])
        self.store.save_batch(saved['id'])
        other = self.root / 'other.png'
        other.write_bytes(b'another synthetic upload')
        a = self.observe(self.batch(), [{'text': 'Unsure', 'sender': 'unknown'}], path=other)
        self.assertTrue(self.store.cleanup_preview(a['id'], 'task')['allowed'])
        self.store.cleanup(a['id'], 'task')
        self.assertEqual([b['id'] for b in self.store.list_batches()], [saved['id']])
        # Beside saved messages the lost part stays marked as a gap.
        gap = self.store.messages(self.friend['id'])['gaps'][0]
        self.assertTrue(gap['archived'])
        self.assertTrue(self.store.cleanup(a['id'], 'originals')['completed'])
        self.assertTrue(self.store.cleanup(a['id'], 'evidence')['completed'])
        self.assertTrue(other.exists())

    def test_source_with_only_deleted_unsaved_tasks_leaves_the_history(self):
        lost = self.store.create_batch(self.friend['id'], 'abandoned-source', 'me', 'peer', source_note='never saved')
        lost = self.observe(lost, [{'text': 'Unsure', 'sender': 'unknown'}])
        kept = self.observe(self.batch(), [{'text': 'Open', 'sender': 'self'}], segment_key='frame-1')
        self.assertEqual(len(self.store.list_sources(self.friend['id'])), 2)
        self.store.cleanup(lost['id'], 'task')
        self.assertEqual([s['platform'] for s in self.store.list_sources(self.friend['id'])], ['synthetic-source'])
        history = self.store.messages(self.friend['id'])
        self.assertEqual([g['batchId'] for g in history['gaps']], [kept['id']])
        self.assertEqual([s['platform'] for s in history['sources']], ['synthetic-source'])
        # Importing into the same source again brings it back with its earlier note.
        again = self.store.create_batch(self.friend['id'], 'abandoned-source', 'me', 'peer')
        self.assertEqual(again['source']['id'], lost['source']['id'])
        self.assertEqual(len(self.store.list_sources(self.friend['id'])), 2)

    def test_warnings_survive_store_reopen_and_empty_rerecognition_marks_difference(self):
        a = self.observe(self.batch(), [{'text': 'Visible', 'sender': 'self'}])
        segment = a['segments'][0]
        self.store.set_segment_warnings(segment['id'], ['vision_unconfirmed', 'no_visible_messages'])
        self.store.submit_segment(a['id'], segment['id'], [])
        after = self.store.get_batch(a['id'])
        self.assertEqual(after['candidates'][0]['recognitionSuggestion'], {'missing': True})
        self.assertEqual(self.store.save_batch(a['id'])['added'], 0)
        self.store.close()
        self.store = ExternalChatStore(self.root / 'local')
        self.assertEqual(self.store.get_batch(a['id'])['warnings'], ['vision_unconfirmed', 'no_visible_messages'])

    def test_cross_batch_overlap_anchors_new_and_later_confirmed_records(self):
        a = self.observe(self.batch(), [{'text': 'Shared anchor', 'sender': 'self', 'bbox': [0.2, 0.5, 0.4, 0.1]}])
        self.store.save_batch(a['id'])
        b = self.observe(self.batch(), [
            {'text': 'Earlier newly visible', 'sender': 'unknown', 'bbox': [0.2, 0.2, 0.4, 0.1]},
            {'text': 'Shared anchor', 'sender': 'self', 'bbox': [0.2, 0.5, 0.4, 0.1]},
            {'text': 'Later newly visible', 'sender': 'other', 'bbox': [0.2, 0.8, 0.4, 0.1]},
        ])
        result = self.store.save_batch(b['id'])
        self.assertEqual((result['added'], result['reused']), (1, 1))
        history = self.store.messages(self.friend['id'])
        self.assertLess(history['gaps'][0]['order'], history['messages'][0]['order'])
        self.store.update_candidate(b['id'], b['candidates'][0]['id'], {'sender': 'other'}, resolve=True)
        self.store.save_batch(b['id'])
        history = self.store.messages(self.friend['id'])
        self.assertEqual([m['text'] for m in history['messages']], ['Earlier newly visible', 'Shared anchor', 'Later newly visible'])
        self.assertFalse(history['coverage']['unanchoredGroups'])

    def test_nonoverlap_batches_and_contradictory_observation_are_explicit(self):
        a = self.observe(self.batch(), [
            {'text': 'A', 'sender': 'self', 'bbox': [0, 0, 0.5, 0.1]},
            {'text': 'B', 'sender': 'other', 'bbox': [0, 0.5, 0.5, 0.1]},
        ])
        self.store.save_batch(a['id'])
        other_file = self.root / 'later.png'
        other_file.write_bytes(b'new unrelated evidence')
        separate = self.observe(self.batch(), [{'text': 'C', 'sender': 'other'}], other_file)
        self.store.save_batch(separate['id'])
        self.assertTrue(self.store.messages(self.friend['id'])['coverage']['unanchoredGroups'])
        reverse = self.observe(self.batch(), [
            {'text': 'B', 'sender': 'other', 'bbox': [0, 0.5, 0.5, 0.1]},
            {'text': 'A', 'sender': 'self', 'bbox': [0, 0, 0.5, 0.1]},
        ])
        self.store.save_batch(reverse['id'])
        history = self.store.messages(self.friend['id'])
        self.assertTrue(history['coverage']['contradictoryOrder'])
        self.assertTrue(history['warnings'])
        self.assertTrue(any(m['orderUncertain'] for m in history['messages']))

    def test_reassociation_removes_out_of_scope_duplicate_target(self):
        a = self.observe(self.batch(), [{'text': 'Same', 'sender': 'other'}])
        self.store.save_batch(a['id'])
        second_file = self.root / 'different-source.png'
        second_file.write_bytes(b'separate evidence copy')
        b = self.observe(self.batch(), [{'text': 'Same', 'sender': 'other'}], second_file)
        self.store.update_candidate(b['id'], b['candidates'][0]['id'], {'duplicateDecision': 'reuse'}, resolve=True)
        other = self.store.create_friend('Different person')
        moved = self.store.reassociate_batch(b['id'], other['id'], 'another-source', 'my-account', 'different-source')
        self.assertNotIn('duplicateMessageId', moved['candidates'][0])
        self.assertNotIn('duplicateCandidateId', moved['candidates'][0])
        self.assertEqual(self.store.save_batch(b['id'])['added'], 1)
        self.assertEqual(len(self.store.messages(other['id'])['messages']), 1)
        self.assertEqual(len(self.store.messages(self.friend['id'])['messages']), 1)


if __name__ == '__main__':
    unittest.main()
