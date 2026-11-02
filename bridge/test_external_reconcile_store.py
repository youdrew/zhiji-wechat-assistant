"""Synthetic visual-reconciliation publication and provenance invariants."""
import tempfile
import unittest
from pathlib import Path

from core_external_store import ExternalChatStore


class ExternalReconcileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ExternalChatStore(self.root / 'local')
        self.friend = self.store.create_friend('Synthetic reconciliation friend')
        self.batch = self.store.create_batch(self.friend['id'], 'another-source', 'synthetic-self', 'synthetic-peer')
        self.input = self.root / 'synthetic.mp4'
        self.input.write_bytes(b'synthetic source, not a real conversation')
        self.material = self.store.add_material(self.batch['id'], self.input)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def frame(self, position, rows, *, distinct=True, batch=None, material=None):
        batch = batch or self.batch
        material = material or self.material
        evidence = self.root / ('synthetic-frame-%s.png' % position)
        evidence.write_bytes(('synthetic frame %s' % position).encode())
        segment = self.store.add_segment(batch['id'], material['id'], 'frame-%s' % position,
                                         position, evidence, video_ms=position * 1000)
        self.store.submit_segment(batch['id'], segment['id'], rows, evidence_distinct=distinct)
        return segment, self.store.segment_candidates(segment['id'])

    @staticmethod
    def bubble(text='Synthetic bubble', **extra):
        return {'text': text, 'sender': 'other', 'bbox': [0.1, 0.2, 0.5, 0.1], **extra}

    def link(self, frame, previous, action='reuse', current_index=0):
        return self.store.apply_auto_links(self.batch['id'], frame['id'], [
            {'currentIndex': current_index, 'previousId': previous['id'], 'action': action}])

    def test_overlap_is_one_record_with_all_raw_observations_and_evidence(self):
        first_frame, first = self.frame(0, [self.bubble()])
        second_frame, second = self.frame(1, [self.bubble(bbox=[0.1, 0.4, 0.5, 0.1])])
        batch = self.link(second_frame, first[0])
        self.assertEqual(batch['counts'], {'total': 1, 'saved': 0, 'ready': 1, 'pending': 0, 'ignored': 0})
        self.assertEqual(len(self.store.segment_candidates(second_frame['id'], include_merged=False)), 0)
        hidden = self.store.segment_candidates(second_frame['id'])[0]
        self.assertEqual(hidden['autoMergedInto'], first[0]['id'])
        self.assertFalse(hidden['ready'])
        self.assertEqual(hidden['original']['bbox'], second[0]['bbox'])
        self.assertEqual(self.link(second_frame, first[0])['counts']['total'], 1)
        result = self.store.save_batch(self.batch['id'])
        self.assertEqual((result['added'], result['reused'], result['saved']), (1, 0, 1))
        history = self.store.messages(self.friend['id'])
        self.assertEqual(history['gaps'], [])
        self.assertEqual(len(history['messages']), 1)
        evidence = history['messages'][0]['evidence']
        self.assertEqual([e['segmentId'] for e in evidence], [first_frame['id'], second_frame['id']])
        self.assertEqual([e['bbox'] for e in evidence], [first[0]['bbox'], second[0]['bbox']])
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 0)

    def test_complete_observation_replaces_partial_and_preferred_box_without_losing_original(self):
        first_frame, first = self.frame(0, [self.bubble('Synthetic partial', truncated=True, issues=['truncated'])])
        second_frame, second = self.frame(1, [self.bubble('Synthetic partial completed', bbox=[0.1, 0.6, 0.5, 0.2])])
        batch = self.link(second_frame, first[0], 'replace')
        candidate = batch['candidates'][0]
        self.assertEqual(candidate['id'], first[0]['id'])
        self.assertEqual(candidate['text'], 'Synthetic partial completed')
        self.assertEqual(candidate['original']['text'], 'Synthetic partial')
        self.assertTrue(candidate['autoReviewed'])
        self.assertFalse(candidate['reviewed'])
        self.assertEqual(candidate['evidenceSegmentId'], second_frame['id'])
        self.assertEqual(candidate['bbox'], second[0]['bbox'])
        self.store.save_batch(self.batch['id'])
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.assertFalse(message['reviewed'])
        self.assertTrue(message['autoReviewed'])
        self.assertEqual(message['evidence'][0]['segmentId'], second_frame['id'])
        self.assertEqual(message['evidence'][0]['bbox'], second[0]['bbox'])
        self.assertEqual(message['evidence'][1]['bbox'], first[0]['bbox'])
        self.assertEqual(self.store.versions('candidate', first[0]['id'])[0]['data']['text'], 'Synthetic partial')

    def test_equal_text_in_distinct_bubbles_is_preserved_but_cross_batch_remains_a_question(self):
        _, first = self.frame(0, [self.bubble('Again'), self.bubble('Again', bbox=[0.1, 0.6, 0.5, 0.1])])
        self.assertTrue(all('possible_duplicate' not in candidate['issues'] for candidate in first))
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 2)
        other_batch = self.store.create_batch(self.friend['id'], 'another-source', 'synthetic-self', 'synthetic-peer')
        other_input = self.root / 'different-synthetic.mp4'
        other_input.write_bytes(b'a different synthetic source')
        material = self.store.add_material(other_batch['id'], other_input)
        _, other = self.frame(2, [self.bubble('Again')], batch=other_batch, material=material)
        self.assertIn('possible_duplicate', other[0]['issues'])
        self.assertEqual(self.store.save_batch(other_batch['id'])['added'], 0)

    def test_chained_observations_link_to_earliest_position_and_keep_all_frames(self):
        _, first = self.frame(0, [self.bubble()])
        second_frame, second = self.frame(1, [self.bubble()])
        self.link(second_frame, first[0])
        third_frame, _ = self.frame(2, [self.bubble()])
        result = self.link(third_frame, second[0])
        self.assertEqual(result['counts']['total'], 1)
        self.assertEqual(len(result['candidates'][0]['evidenceSegmentIds']), 3)
        self.assertEqual(self.store.segment_candidates(third_frame['id'])[0]['autoMergedInto'], first[0]['id'])

    def test_distinct_geometry_never_exempts_other_materials_from_duplicate_review(self):
        _, first = self.frame(0, [self.bubble('Shared screenshot bubble')])
        other_input = self.root / 'another-screenshot.png'
        other_input.write_bytes(b'another synthetic screenshot with an overlapping bubble')
        material = self.store.add_material(self.batch['id'], other_input)
        _, second = self.frame(1, [self.bubble('Shared screenshot bubble'),
                                   self.bubble('Shared screenshot bubble', bbox=[0.1, 0.6, 0.5, 0.1])],
                               material=material, distinct=True)
        self.assertTrue(all('possible_duplicate' in row['issues'] for row in second))
        self.assertTrue(all(row['duplicateCandidateId'] == first[0]['id'] for row in second))
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 1)
        history = self.store.messages(self.friend['id'])
        self.assertEqual(len(history['messages']), 1)
        self.assertEqual(len(history['gaps']), 2)

    def test_neighbor_alignment_cannot_exempt_a_reappearing_bubble_after_a_missed_frame(self):
        _, first = self.frame(0, [self.bubble('Synthetic A')])
        self.frame(1, [self.bubble('Synthetic B')])
        _, reappeared = self.frame(2, [self.bubble('Synthetic A')], distinct=True)
        self.assertIn('possible_duplicate', reappeared[0]['issues'])
        self.assertEqual(reappeared[0]['duplicateCandidateId'], first[0]['id'])
        self.assertFalse(reappeared[0]['ready'])
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 2)
        self.assertEqual(len(self.store.messages(self.friend['id'])['gaps']), 1)

    def test_scope_future_and_inconsistent_complete_links_are_rejected(self):
        first_frame, first = self.frame(0, [self.bubble('First complete')])
        second_frame, second = self.frame(1, [self.bubble('Different complete')])
        for action in ('reuse', 'replace'):
            with self.assertRaises(ValueError):
                self.link(second_frame, first[0], action)
        with self.assertRaises(ValueError):
            self.link(first_frame, second[0])
        other_input = self.root / 'other.mp4'
        other_input.write_bytes(b'different synthetic material')
        other_material = self.store.add_material(self.batch['id'], other_input)
        other_frame, _ = self.frame(2, [self.bubble('First complete')], material=other_material)
        with self.assertRaises(ValueError):
            self.link(other_frame, first[0])
        self.assertEqual(len(self.store.get_batch(self.batch['id'])['candidates']), 3)
        other_friend = self.store.create_friend('Another synthetic friend')
        foreign_batch = self.store.create_batch(other_friend['id'], 'synthetic-source', 'synthetic-self', 'different-peer')
        foreign_material = self.store.add_material(foreign_batch['id'], self.input)
        _, foreign = self.frame(0, [self.bubble('Different complete')], batch=foreign_batch, material=foreign_material)
        with self.assertRaises(ValueError):
            self.link(second_frame, foreign[0])

    def test_invalid_link_rolls_back_other_links_in_the_same_submission(self):
        _, first = self.frame(0, [self.bubble('One'), self.bubble('Two', bbox=[0.1, 0.5, 0.5, 0.1])])
        second_frame, _ = self.frame(1, [self.bubble('One'), self.bubble('Mismatch', bbox=[0.1, 0.5, 0.5, 0.1])])
        with self.assertRaises(ValueError):
            self.store.apply_auto_links(self.batch['id'], second_frame['id'], [
                {'currentIndex': 0, 'previousId': first[0]['id'], 'action': 'reuse'},
                {'currentIndex': 1, 'previousId': first[1]['id'], 'action': 'reuse'},
            ])
        self.assertEqual(len(self.store.get_batch(self.batch['id'])['candidates']), 4)
        self.assertFalse(any(c.get('autoMergedInto') for c in self.store.segment_candidates(second_frame['id'])))

    def test_human_edit_and_saved_record_survive_auto_completion_and_retry(self):
        first_frame, first = self.frame(0, [self.bubble('Partial', truncated=True, issues=['truncated'])])
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'text': 'Human draft'})
        second_frame, _ = self.frame(1, [self.bubble('Partial complete')])
        with self.assertRaises(ValueError):
            self.link(second_frame, first[0], 'replace')
        with self.assertRaises(ValueError):
            self.store.apply_auto_review(self.batch['id'], first[0]['id'], {'resolved': False})
        self.assertEqual(self.store.segment_candidates(first_frame['id'])[0]['text'], 'Human draft')
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'text': 'Human complete', 'truncated': False}, resolve=True)
        self.store.save_batch(self.batch['id'], [first[0]['id']])
        with self.assertRaises(ValueError):
            self.link(second_frame, first[0], 'replace')
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual(message['text'], 'Human complete')

    def test_reuse_adds_evidence_without_overwriting_saved_human_correction(self):
        _, first = self.frame(0, [self.bubble()])
        self.store.save_batch(self.batch['id'])
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.store.correct_message(message['id'], {'text': 'Human saved correction'})
        second_frame, _ = self.frame(1, [self.bubble()])
        self.link(second_frame, first[0])
        self.assertEqual(self.store.get_batch(self.batch['id'])['counts']['pending'], 0)
        corrected = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual(corrected['text'], 'Human saved correction')
        self.assertEqual(len(corrected['evidence']), 2)
        self.assertEqual(len(self.store.versions('message', message['id'])), 2)

    def test_protected_human_text_and_sender_keep_source_identity_across_hidden_aliases(self):
        _, first = self.frame(0, [self.bubble('Source A', quote='Source quote')])
        alias_frame, alias = self.frame(1, [self.bubble('Source A', quote='Source quote')])
        self.link(alias_frame, first[0])
        self.store.update_candidate(self.batch['id'], first[0]['id'],
                                    {'text': 'Human B', 'sender': 'self'}, resolve=True)
        self.store.save_batch(self.batch['id'])
        saved_before = self.store.messages(self.friend['id'])['messages'][0]
        third_frame, _ = self.frame(2, [self.bubble('Source A', quote='Source quote')])
        batch = self.link(third_frame, alias[0])
        canonical = batch['candidates'][0]
        self.assertEqual((canonical['text'], canonical['sender'], canonical['reviewed']), ('Human B', 'self', True))
        self.assertEqual((batch['counts']['total'], batch['counts']['saved'], batch['counts']['pending']), (1, 1, 0))
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 0)
        saved_after = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual((saved_after['text'], saved_after['sender'], saved_after['version']),
                         (saved_before['text'], saved_before['sender'], saved_before['version']))
        self.assertEqual(len(saved_after['evidence']), 3)
        self.assertEqual(self.store.messages(self.friend['id'])['gaps'], [])

    def test_protected_reuse_rejects_changed_source_quote_and_conflict(self):
        _, first = self.frame(0, [self.bubble('Source A', quote='Original quote')])
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'text': 'Human B'}, resolve=True)
        frame, _ = self.frame(1, [self.bubble('Source A', quote='Different quote')])
        for action in ('reuse', 'replace', 'conflict'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.link(frame, first[0], action)
        self.assertEqual(self.store.get_batch(self.batch['id'])['candidates'][0]['text'], 'Human B')

    def test_source_reuse_preserves_unconfirmed_human_draft_and_its_review_gate(self):
        _, first = self.frame(0, [self.bubble('Source A')])
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'text': 'Unconfirmed human B', 'sender': 'self'})
        second_frame, _ = self.frame(1, [self.bubble('Source A')])
        batch = self.link(second_frame, first[0])
        self.assertEqual((batch['counts']['total'], batch['counts']['ready'], batch['counts']['pending']), (1, 0, 1))
        self.assertEqual((batch['candidates'][0]['text'], batch['candidates'][0]['sender']), ('Unconfirmed human B', 'self'))
        self.assertIn('edited-unconfirmed', batch['candidates'][0]['issues'])
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 0)

    def test_source_unknown_sender_can_reuse_a_human_corrected_saved_canonical(self):
        _, first = self.frame(0, [self.bubble('Source A', sender='unknown', issues=['unknown_sender'])])
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'sender': 'other'}, resolve=True)
        self.store.save_batch(self.batch['id'])
        second_frame, _ = self.frame(1, [self.bubble('Source A', sender='unknown', issues=['unknown_sender'])])
        batch = self.link(second_frame, first[0])
        self.assertEqual((batch['counts']['total'], batch['counts']['saved'], batch['counts']['pending']), (1, 1, 0))
        self.assertEqual(batch['candidates'][0]['sender'], 'other')
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual((message['text'], message['sender']), ('Source A', 'other'))
        self.assertEqual(len(message['evidence']), 2)
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 0)
        _, third = self.frame(2, [self.bubble('Other source', sender='unknown', issues=['unknown_sender'])])
        fourth_frame, _ = self.frame(3, [self.bubble('Other source', sender='unknown', issues=['unknown_sender'])])
        with self.assertRaises(ValueError):
            self.link(fourth_frame, third[0])

    def test_nfc_and_visual_line_wrap_changes_reuse_without_changing_original_transcript(self):
        original = 'Cafe\u0301\nSynthetic line'
        original_quote = 'Quoted\r\nsource'
        _, first = self.frame(0, [self.bubble(original, quote=original_quote)])
        second_frame, _ = self.frame(1, [self.bubble('CaféSynthetic line', quote='Quotedsource')])
        batch = self.link(second_frame, first[0])
        self.assertEqual((batch['counts']['total'], batch['counts']['ready']), (1, 1))
        self.assertEqual(batch['candidates'][0]['text'], original)
        self.assertEqual(batch['candidates'][0]['quote'], original_quote)
        self.store.save_batch(self.batch['id'])
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual((message['text'], message['quote']), (original, original_quote))
        self.assertEqual(len(message['evidence']), 2)

    def test_local_reread_preserves_machine_vs_human_review_and_is_retry_safe(self):
        first_frame, first = self.frame(0, [self.bubble('Synthetic □', issues=['unclear_text'])])
        second_frame, _ = self.frame(1, [])
        evidence = [first_frame['id'], second_frame['id']]
        candidate = self.store.apply_auto_review(self.batch['id'], first[0]['id'], {
            'resolved': True, 'message': self.bubble('Synthetic clear', issues=[]),
            'evidenceSegmentIds': evidence, 'preferredEvidenceSegmentId': second_frame['id']})
        self.assertTrue(candidate['ready'])
        self.assertTrue(candidate['autoReviewAttempted'])
        self.assertFalse(candidate['reviewed'])
        self.assertEqual(candidate['original']['text'], 'Synthetic □')
        self.store.submit_segment(self.batch['id'], first_frame['id'], [self.bubble('Later OCR', issues=[])], evidence_distinct=True)
        retried = self.store.segment_candidates(first_frame['id'])[0]
        self.assertEqual(retried['text'], 'Synthetic clear')
        self.assertEqual(retried['recognitionSuggestion']['text'], 'Later OCR')
        self.assertFalse(retried['ready'])

    def test_unresolved_attempt_keeps_doubts_and_does_not_publish(self):
        frame, first = self.frame(0, [self.bubble('Still □', issues=['unclear_text'])])
        candidate = self.store.apply_auto_review(self.batch['id'], first[0]['id'], {'resolved': False})
        self.assertTrue(candidate['autoReviewAttempted'])
        self.assertEqual(candidate['text'], 'Still □')
        self.assertFalse(candidate['ready'])
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 0)
        self.assertEqual(candidate['evidenceSegmentIds'], [frame['id']])

    def test_unresolved_review_keeps_cross_frame_preference_and_complete_provenance(self):
        first_frame, first = self.frame(0, [self.bubble('Synthetic partial', truncated=True, issues=['truncated'])])
        second_frame, _ = self.frame(1, [self.bubble('Synthetic partial completed', bbox=[.1, .6, .5, .2])])
        before = self.link(second_frame, first[0], 'replace')['candidates'][0]
        self.assertEqual(before['evidenceSegmentId'], second_frame['id'])
        after = self.store.apply_auto_review(self.batch['id'], first[0]['id'], {'resolved': False})
        self.assertTrue(after['autoReviewAttempted'])
        self.assertEqual(after['version'], before['version'] + 1)
        self.assertEqual({key: value for key, value in after.items() if key not in ('version', 'autoReviewAttempted')},
                         {key: value for key, value in before.items() if key not in ('version', 'autoReviewAttempted')})
        self.assertEqual(after['evidenceSegmentIds'], [first_frame['id'], second_frame['id']])
        self.assertEqual(self.store.messages(self.friend['id'])['messages'], [])
        self.assertTrue(all(row['state'] == 'done' for row in self.store.get_batch(self.batch['id'])['segments']))

    def test_cross_frame_review_rejects_explicit_incomplete_or_foreign_evidence(self):
        first_frame, first = self.frame(0, [self.bubble('Partial', truncated=True, issues=['truncated'])])
        second_frame, _ = self.frame(1, [self.bubble('Partial completed')])
        before = self.link(second_frame, first[0], 'replace')['candidates'][0]
        other_input = self.root / 'different-review-material.png'
        other_input.write_bytes(b'a distinct synthetic review material')
        other_material = self.store.add_material(self.batch['id'], other_input)
        foreign, _ = self.frame(2, [], material=other_material)
        invalid = [
            {'resolved': False, 'evidenceSegmentIds': [first_frame['id']]},
            {'resolved': False, 'evidenceSegmentIds': []},
            {'resolved': False, 'evidenceSegmentIds': None},
            {'resolved': False, 'evidenceSegmentIds': [first_frame['id'], foreign['id']],
             'preferredEvidenceSegmentId': foreign['id']},
            {'resolved': True, 'message': self.bubble('Model proposal', issues=[]),
             'evidenceSegmentIds': [first_frame['id']]},
        ]
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.store.apply_auto_review(self.batch['id'], first[0]['id'], result)
            self.assertEqual(self.store.segment_candidates(first_frame['id'])[0], before)
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'text': 'Unconfirmed human draft'})
        protected = self.store.segment_candidates(first_frame['id'])[0]
        with self.assertRaises(ValueError):
            self.store.apply_auto_review(self.batch['id'], first[0]['id'], {'resolved': False})
        self.assertEqual(self.store.segment_candidates(first_frame['id'])[0], protected)

    def test_invalid_reread_cannot_clear_uncertainty_or_inject_metadata(self):
        frame, first = self.frame(0, [self.bubble('Partial', truncated=True, issues=['truncated'])])
        candidate_id = first[0]['id']
        invalid = [
            {'resolved': True, 'message': self.bubble('Still □', issues=[])},
            {'resolved': True, 'message': self.bubble('Clear', issues=[], reviewed=True)},
            {'resolved': True, 'message': self.bubble('Clear', issues=[], sender='unknown')},
            {'resolved': True, 'message': self.bubble('Clear', issues=[]), 'preferredEvidenceSegmentId': 'missing'},
            {'resolved': 'yes'},
        ]
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.store.apply_auto_review(self.batch['id'], candidate_id, result)
        self.assertFalse(self.store.segment_candidates(frame['id'])[0].get('autoReviewAttempted', False))
        other_batch = self.store.create_batch(self.friend['id'], 'synthetic-source', 'synthetic-self', 'other-peer')
        material = self.store.add_material(other_batch['id'], self.input)
        foreign, _ = self.frame(9, [], batch=other_batch, material=material)
        with self.assertRaises(ValueError):
            self.store.apply_auto_review(self.batch['id'], candidate_id, {'resolved': False, 'evidenceSegmentIds': [foreign['id']]})

    def test_bubble_reread_cannot_clear_cross_conversation_or_duplicate_doubts(self):
        for issue in ('multiple_conversations', 'possible_duplicate', 'uncertain_order'):
            frame, first = self.frame(10 + len(self.store.get_batch(self.batch['id'])['segments']),
                                      [self.bubble('Synthetic □', issues=['unclear_text', issue])])
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                self.store.apply_auto_review(self.batch['id'], first[0]['id'], {
                    'resolved': True, 'message': self.bubble('Synthetic clear', issues=[])})
            self.assertIn(issue, self.store.segment_candidates(frame['id'])[0]['issues'])

    def test_system_notices_can_save_with_unknown_sender_but_human_bubbles_cannot(self):
        _, rows = self.frame(0, [self.bubble('Synthetic system notice', kind='system', sender='unknown'),
                                 self.bubble('Synthetic unknown speaker', sender='unknown')])
        self.assertTrue(rows[0]['ready'])
        self.assertFalse(rows[1]['ready'])
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 1)
        message = self.store.messages(self.friend['id'])['messages'][0]
        self.assertEqual((message['kind'], message['sender']), ('system', 'unknown'))

    def test_conflicting_reads_stay_pending_until_reread_converges_then_merge(self):
        first_frame, first = self.frame(0, [self.bubble('Synthetic first read')])
        second_frame, second = self.frame(1, [self.bubble('Synthetic second read')])
        batch = self.link(second_frame, first[0], 'conflict')
        self.assertEqual((batch['counts']['total'], batch['counts']['pending']), (2, 2))
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 0)
        self.assertEqual(self.store.segment_candidates(first_frame['id'])[0]['autoConflictWith'], [second[0]['id']])
        evidence = [first_frame['id'], second_frame['id']]
        updated = self.store.apply_auto_review(self.batch['id'], first[0]['id'], {
            'resolved': True, 'message': self.bubble('Synthetic resolved read', issues=[]),
            'evidenceSegmentIds': evidence})
        self.assertEqual(updated['text'], 'Synthetic resolved read')
        self.assertFalse(updated['ready'])
        self.assertIn('recognition-difference', updated['issues'])
        self.store.apply_auto_review(self.batch['id'], second[0]['id'], {
            'resolved': True, 'message': self.bubble('Synthetic resolved read', issues=[]),
            'evidenceSegmentIds': evidence})
        batch = self.link(second_frame, first[0])
        self.assertEqual((batch['counts']['total'], batch['counts']['ready'], batch['counts']['pending']), (1, 1, 0))
        self.assertNotIn('autoConflictWith', batch['candidates'][0])
        self.assertEqual(self.store.save_batch(self.batch['id'])['added'], 1)
        self.assertEqual(len(self.store.messages(self.friend['id'])['messages']), 1)

    def test_conflict_cannot_turn_saved_or_manually_edited_records_into_doubts(self):
        _, first = self.frame(0, [self.bubble('Synthetic known read')])
        self.store.update_candidate(self.batch['id'], first[0]['id'], {'text': 'Human draft'})
        second_frame, _ = self.frame(1, [self.bubble('Synthetic conflicting read')])
        with self.assertRaises(ValueError):
            self.link(second_frame, first[0], 'conflict')
        self.assertEqual(self.store.get_batch(self.batch['id'])['candidates'][0]['text'], 'Human draft')


if __name__ == '__main__':
    unittest.main()
