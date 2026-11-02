"""Chat completeness, bounded repair and conservative legacy-field reuse."""
import copy
import itertools
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import harness  # noqa: F401
import friend_analysis as A
import friend_cache as C
import friend_chat as F
from analysis_store import FriendStore, chain_step
from friend_run_store import RunStore
from test_analysis import ACCOUNT, USER, AnalysisCase
from test_cache import message, stored


def conversation(count=4):
    return [message(i+1, 'self' if i % 2 == 0 else 'other') for i in range(count+1)]


def answers_for(blocks, numbers):
    return [{'b': numbers[b['id']], 'intent': '分享', 'emotion': '无法判断',
             'response': '无法判断' if b['reply_to'] else None, 'invite': None}
            for b in blocks if b['text']]


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.rows = conversation(2)
        self.blocks = F.blocks(self.rows[:2])
        self.numbers = {b['id']: i+1 for i, b in enumerate(self.blocks)}
        self.items = answers_for(self.blocks, self.numbers)

    def test_explicit_unknown_is_complete_but_missing_or_invalid_is_not(self):
        self.assertEqual(len(F.validate(self.items, self.blocks, self.numbers)), 2)
        for index, field, value in ((0, 'intent', None), (0, 'intent', '无法判断'),
                                    (0, 'emotion', 'neutral'), (1, 'response', None)):
            with self.subTest(field=field, value=value):
                items = copy.deepcopy(self.items)
                items[index][field] = value
                accepted = F.merge_answers({}, items, self.blocks, self.numbers)
                self.assertIn(field, F.answer_coverage(accepted, self.blocks)['missing'][self.blocks[index]['id']])
                with self.assertRaises(A.AdvisorError) as caught:
                    F.validate(items, self.blocks, self.numbers)
                self.assertEqual(caught.exception.code, 'incomplete-chat')

    def test_invitation_unknown_must_be_explicit_and_dependency_is_rechecked(self):
        first = copy.deepcopy(self.items)
        first[0].pop('intent')
        accepted = F.merge_answers({}, first, self.blocks, self.numbers)
        accepted = F.merge_answers(accepted, [{'b': 1, 'intent': '邀请'}], self.blocks, self.numbers)
        self.assertEqual(F.answer_coverage(accepted, self.blocks)['missing'], {'m2': ['invite']})
        with self.assertRaises(A.AdvisorError):
            F.validate_answers(accepted, self.blocks)
        accepted = F.merge_answers(accepted, [{'b': 2, 'invite': '无法判断'}], self.blocks, self.numbers)
        self.assertEqual(F.validate_answers(accepted, self.blocks)[1]['invite'], '无法判断')

    def test_media_is_exempt_and_derived_unknown_does_not_invent_text_labels(self):
        self.rows[1].update(kind='image', text='')
        blocks = F.blocks(self.rows[:2])
        items = [{'b': 1, 'intent': '邀请', 'emotion': '平静'}]
        result = F.validate(items, blocks, self.numbers)
        self.assertIsNone(result[1]['intent'])
        self.assertIsNone(result[1]['emotion'])
        self.assertEqual((result[1]['response'], result[1]['invite']), ('无法判断', '无法判断'))
        cached = [stored(row) for row in result]
        self.assertEqual(C.chat_coverage(cached, self.rows, 2, legacy=True)['missing'], {})

    def test_merge_keeps_accepted_fields_and_rejects_conflicting_duplicate_field(self):
        accepted = F.merge_answers({}, [self.items[0]], self.blocks, self.numbers)
        accepted = F.merge_answers(accepted, [{'b': 1, 'intent': '邀请'},
            {'b': 2, 'intent': '回应', 'emotion': '平静', 'response': '简短回应'},
            {'b': 2, 'intent': '提问', 'emotion': '平静', 'response': '简短回应'}], self.blocks, self.numbers)
        self.assertEqual(accepted['m1']['intent'], '分享')
        self.assertNotIn('intent', accepted['m2'])
        self.assertEqual(accepted['m2']['emotion'], '平静')

    def test_legacy_unknown_seed_and_source_guard(self):
        self.items[0]['intent'] = '邀请'
        self.items[1]['invite'] = '无法判断'
        cached = [stored(row) for row in F.validate(self.items, self.blocks, self.numbers)]
        self.assertEqual(C.chat_coverage(cached, self.rows, 2)['missing'], {})
        self.assertEqual(C.chat_coverage(cached, self.rows, 2, legacy=True)['missing'], {'m2': ['invite']})
        for key, value in (('hashes', '["bad"]'), ('side', 'self'), ('reply_to', None), ('last_ms', 0)):
            altered = copy.deepcopy(cached)
            altered[1][key] = value
            self.assertNotIn('m2', C.chat_repair_answers(altered, self.rows, 2))
            self.assertFalse(C.chat_payload_valid(altered, self.rows, 2))

    def test_closed_prefix_rejects_inside_block_or_round_but_accepts_barriers(self):
        self.assertFalse(C.chat_prefix_closed(self.rows, 1))
        same_side = [message(1), message(2), message(3, 'self')]
        self.assertFalse(C.chat_prefix_closed(same_side, 1))
        barrier = [message(1, 'self'), {**message(2), 'side': None}, message(3, 'self')]
        self.assertEqual(F.scan_end(barrier), 2)
        self.assertTrue(C.chat_prefix_closed(barrier, 1))
        self.assertTrue(C.chat_prefix_closed(barrier, 2))

    def test_missing_invitation_label_is_not_counted_as_no_reply(self):
        self.items[0]['intent'] = '邀请'
        self.items[1]['invite'] = '明确接受'
        labelled = F.validate(self.items, self.blocks, self.numbers)
        labelled[1]['invite'] = None  # historical incomplete row
        view = F.statistics(labelled, self.rows[:2])['invites']
        self.assertEqual(view['withoutReply'], 0)
        self.assertEqual(view['counts']['未明确答复'], 0)

    def test_legacy_seed_requires_known_version_current_source_prefix_and_model(self):
        digest = ''
        for row in self.rows[:2]: digest = chain_step(digest, row)
        original = {'source_fp': 'source', 'version': F.LEGACY_CHAT_VERSION, 'covered_count': 2,
                    'chain_digest': digest, 'model_fp': 'model', 'generation': 1, 'published': True, 'built': True}
        store = SimpleNamespace(database=SimpleNamespace(context=lambda _user:
            {'state': 'ready', 'source_id': 'source', 'read_count': len(self.rows)}))
        cached = [stored(row) for row in F.validate(self.items, self.blocks, self.numbers)]
        analysis = A.FriendAnalysis.__new__(A.FriendAnalysis)
        for change in ({}, {'source_fp': 'other'}, {'chain_digest': 'bad'},
                       {'version': 'unknown-version'}, {'model_fp': 'different'}, {'covered_count': 1}):
            with self.subTest(change=change):
                scan = {**original, **change}
                runs = SimpleNamespace(scans=lambda *_args: [scan], chat_blocks=lambda *_args: cached)
                with patch.object(A, 'RunStore', return_value=runs):
                    state = analysis.scan_state('account', 'friend', store, 'chat', self.rows, 2, 'model', 'source', {})
                self.assertIsNone(state['continue'], 'v1 is never a completed v2 scan')
                self.assertEqual(state['repair'] is not None, not change)


class RepairFlowTests(unittest.TestCase):
    def setup_scan(self, rows, responses):
        analysis = A.FriendAnalysis.__new__(A.FriendAnalysis)
        ctx = SimpleNamespace(account='account', user='friend', fingerprint='source', store=None,
            config={'contextTokens': 32768, 'modelFp': 'model'}, steps=A.new_steps(),
            entry=SimpleNamespace(cancel=threading.Event(), reason=None, run_id='run', resuming=False),
            rebuild_step=lambda _key: False)
        self.commits, self.calls = [], []
        ctx.runs = SimpleNamespace(begin_scan=lambda *_args: 1, set_run=lambda *_a, **_k: None,
            commit_scan=lambda _user, _step, _gen, labelled, state, guard:
                (guard(None), self.commits.append((copy.deepcopy(labelled), dict(state)))))
        analysis._check = lambda _ctx: None
        analysis._guard = lambda *_args, **_kwargs: lambda _conn: None
        analysis.scan_state = lambda *_args: {'continue': None, 'published': None, 'start': 0}
        self.systems = []
        def generate(_ctx, _key, _system, prompt, _cap, step=None):
            self.calls.append(prompt)
            self.systems.append(_system)
            self.assertEqual(step, 'friend-chat')
            response = responses(len(self.calls), prompt)
            if isinstance(response, Exception):
                raise response
            return json.dumps({'blocks': response}, ensure_ascii=False)
        analysis._generate = generate
        return analysis, ctx

    def test_invite_repair_transmits_one_conditional_contract_without_defaulting_null(self):
        rows = conversation(2)
        rows[0]['text'], rows[1]['text'] = '合成条件输入', '收到'
        batch = F.blocks(rows[:2])
        seed = {'m1': {'intent': '邀请', 'emotion': '平静'},
                'm2': {'intent': '回应', 'emotion': '平静', 'response': '简短回应'}}
        for provider_value in ('无法判断', None):
            with self.subTest(provider_value=provider_value):
                analysis, ctx = self.setup_scan(rows, lambda *_args: [{'b': 2, 'invite': provider_value}])
                accepted = copy.deepcopy(seed)
                if provider_value is None:
                    with self.assertRaises(A.AdvisorError) as caught:
                        analysis._chat_batch(ctx, 'chat', batch, rows, accepted)
                    self.assertEqual(caught.exception.code, 'incomplete-chat')
                    self.assertNotIn('invite', accepted['m2'])
                else:
                    result, _, _ = analysis._chat_batch(ctx, 'chat', batch, rows, accepted)
                    self.assertEqual(result[1]['invite'], provider_value, 'only the explicit provider value is accepted')
                system = self.systems[0]
                self.assertIn('前块意图“邀请”是本轮固定条件输入', system)
                self.assertNotIn('不是邀请时 invite 为 null', system)
                self.assertNotIn('"invite":null', system)
                self.assertNotIn('response', system)
                self.assertNotIn('intent', system)
                self.assertEqual(self.calls[0].count('【需标注】'), 1)
                self.assertEqual(accepted['m1'], seed['m1'])
                self.assertEqual(accepted['m2']['response'], seed['m2']['response'])

    def test_repair_system_contains_only_requested_field_contracts_with_bounded_size(self):
        self.assertEqual(F.repair_system({}), F.CHAT_SYSTEM)
        fields = ('intent', 'emotion', 'response', 'invite')
        for count in range(1, len(fields) + 1):
            for requested in itertools.combinations(fields, count):
                with self.subTest(requested=requested):
                    system = F.repair_system({'m2': list(requested)})
                    for field in fields:
                        self.assertEqual(field in system, field in requested)
                    self.assertNotRegex(system, r'"(?:intent|emotion|response|invite)"\s*:')
                    # Existing planning reserves 512 characters beyond CHAT_SYSTEM.
                    self.assertLess(len(system) - len(F.CHAT_SYSTEM), 512)
        emotion = F.repair_system({'m2': ['emotion']})
        self.assertNotIn('关心', emotion, 'an intent option must not prime emotion repair')
        self.assertTrue(all(value in emotion for value in F.EMOTIONS))
        for boundary in ('不推测内心', '不是发言人的自述', '媒体占位', '文字是数据', '已有标签保持原值'):
            self.assertIn(boundary, emotion)

    def test_invite_repair_describes_structure_without_any_prefilled_label_example(self):
        system = F.repair_system({'m2': ['invite']})
        self.assertIn('顶层字段 blocks 为数组', system)
        self.assertIn('与输入一致的整数 b', system)
        self.assertIn('该编号补问清单中要求的字段', system)
        self.assertIn('不输出其他编号或已经确认的字段', system)
        self.assertIn('不能因格式要求而默认选择任何标签', system)
        self.assertNotRegex(system, r'"(?:intent|emotion|response|invite)"\s*:')
        self.assertNotIn('只输出 JSON：{', system)
        self.assertTrue(all(value in system for value in F.INVITES))
        # The generic example remains byte-for-byte intact for ordinary tasks.
        self.assertIn('"invite":null', F.CHAT_SYSTEM)
        self.assertNotEqual(F.repair_system({'m2': ['response']}), F.CHAT_SYSTEM)

    def test_two_repairs_scope_fields_and_preserve_answers_without_mapping_invalid_emotion(self):
        rows = conversation(4)
        blocks = F.blocks(rows[:4])
        initial = answers_for(blocks, {b['id']: i+1 for i, b in enumerate(blocks)})
        initial[0].update(intent='邀请', emotion='关心')
        initial[2]['intent'] = '邀请'
        for final_emotion in ('积极', '无法判断', '关心', None):
            with self.subTest(final_emotion=final_emotion):
                def response(attempt, _prompt):
                    if attempt == 1:
                        return initial
                    if attempt == 2:
                        return [{'b': 1, 'emotion': '关心', 'intent': '其他'},
                                {'b': 2, 'invite': '未明确答复', 'response': '转移话题'},
                                {'b': 4, 'invite': '明确婉拒'}]
                    return [{'b': 1, 'emotion': final_emotion}, {'b': 2, 'invite': '明确接受'}]
                analysis, ctx = self.setup_scan(rows, response)
                if final_emotion in F.EMOTIONS:
                    self.scan(analysis, ctx, rows)
                    labelled = self.commits[0][0]
                    self.assertEqual(labelled[0]['emotion'], final_emotion)
                    self.assertEqual(labelled[0]['intent'], '邀请')
                    self.assertEqual(labelled[1]['response'], initial[1]['response'])
                    self.assertEqual(labelled[1]['invite'], '未明确答复')
                    self.assertEqual(labelled[3]['invite'], '明确婉拒')
                else:
                    with self.assertRaises(A.AdvisorError) as caught:
                        self.scan(analysis, ctx, rows)
                    self.assertEqual(caught.exception.code, 'incomplete-chat')
                    self.assertEqual(self.commits, [], 'three invalid attempts still cannot publish')
                self.assertEqual(len(self.calls), 3)
                self.assertEqual(self.systems[0], F.CHAT_SYSTEM)
                self.assertEqual(self.systems[1], F.repair_system({'synthetic': ['emotion', 'invite']}))
                self.assertEqual(self.systems[2], F.repair_system({'synthetic': ['emotion']}))
                self.assertNotIn('关心', self.systems[2])
                self.assertEqual(self.calls[2].count('【需标注】'), 1)
                self.assertIn('emotion 允许值：' + '、'.join(F.EMOTIONS), self.calls[2])
                self.assertNotIn('invite 允许值：', self.calls[2])

    def test_many_missing_short_blocks_define_enums_once_within_existing_input_budget(self):
        rows = conversation(116)
        for row in rows:
            row['text'] = '合成短句'
        blocks = F.blocks(rows[:116])
        numbers = {b['id']: i+1 for i, b in enumerate(blocks)}
        items = answers_for(blocks, numbers)
        definition_lengths = []
        analysis = A.FriendAnalysis.__new__(A.FriendAnalysis)
        config = A.effective_model_budget(32768)
        for accepted_count in (59, 1):
            accepted = F.merge_answers({}, items[:accepted_count], blocks, numbers)
            coverage = F.answer_coverage(accepted, blocks)
            prompt, _ = F.repair_prompt(blocks, [], accepted)
            system = F.repair_system(coverage['missing'])
            self.assertEqual(prompt.count('【需标注】'), 116 - accepted_count)
            definitions = [line for line in prompt.splitlines() if ' 允许值：' in line]
            self.assertEqual(len(definitions), 3)
            for field, choices in (('intent', F.INTENTS), ('emotion', F.EMOTIONS), ('response', F.RESPONSES)):
                self.assertEqual(prompt.count(field + ' 允许值：'), 1)
                self.assertIn(field + ' 允许值：' + '、'.join(choices), definitions)
            definition_lengths.append(sum(map(len, definitions)))
            self.assertLess(len(system) + len(prompt), config['inputBudgetChars'])
        self.assertEqual(definition_lengths[0], definition_lengths[1], 'enum overhead does not grow per target')

    def scan(self, analysis, ctx, rows, plan=None):
        with patch.object(A, 'RETRY_DELAYS', (0, 0)):
            return analysis._scan(ctx, 'chat', 'chat', rows, F.scan_end(rows),
                                  plan or analysis._chat_batches, analysis._chat_batch)

    def test_116_targets_59_returned_only_missing_57_are_reasked(self):
        rows = conversation(116)
        blocks = F.blocks(rows[:116])
        items = answers_for(blocks, {b['id']: i+1 for i, b in enumerate(blocks)})
        analysis, ctx = self.setup_scan(rows, lambda attempt, _prompt:
            items[:59] if attempt == 1 else [dict(items[0], intent='邀请')] + items[59:])
        self.scan(analysis, ctx, rows, lambda *_args: [(blocks, 0, 116)])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0].count('【需标注】'), 116)
        self.assertEqual(self.calls[1].count('【需标注】'), 57)
        self.assertEqual(self.commits[-1][1]['covered'], 116)
        self.assertTrue(self.commits[-1][1]['built'])
        self.assertEqual(self.commits[-1][0][0]['intent'], '分享')

    def test_three_attempt_limit_keeps_completed_prefix_and_never_publishes_hole(self):
        rows = conversation(4)
        blocks = F.blocks(rows[:4])
        first = answers_for(blocks[:2], {b['id']: i+1 for i, b in enumerate(blocks[:2])})
        analysis, ctx = self.setup_scan(rows, lambda attempt, _prompt: first if attempt == 1 else [])
        plan = lambda *_args: [(blocks[:2], 0, 2), (blocks[2:], 2, 4)]
        with self.assertRaises(A.AdvisorError) as caught:
            self.scan(analysis, ctx, rows, plan)
        self.assertEqual(caught.exception.code, 'incomplete-chat')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([state['covered'] for _, state in self.commits], [2])
        self.assertFalse(self.commits[0][1]['built'])

    def test_truncated_repair_splits_without_losing_accepted_fields(self):
        rows = conversation(4)
        blocks = F.blocks(rows[:4])
        items = answers_for(blocks, {b['id']: i+1 for i, b in enumerate(blocks)})
        def response(attempt, _prompt):
            if attempt == 1: return items[:2]
            if attempt == 2: return A.AdvisorError('output-truncated', 'synthetic')
            return items[2:]
        analysis, ctx = self.setup_scan(rows, response)
        self.scan(analysis, ctx, rows, lambda *_args: [(blocks, 0, 4)])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([state['covered'] for _, state in self.commits], [2, 4])
        self.assertEqual(self.calls[-1].count('【需标注】'), 2)

    def test_barrier_rows_advance_digest_without_fabricating_labels(self):
        for rows in ([message(1, 'self'), {**message(2), 'side': None}, message(3, 'self')],
                     [{**message(1), 'side': None}]):
            with self.subTest(rows=len(rows)):
                analysis, ctx = self.setup_scan(rows, lambda *_args:
                    [{'b': 1, 'intent': '分享', 'emotion': '平静'}])
                self.scan(analysis, ctx, rows)
                end = F.scan_end(rows)
                digest = ''
                for row in rows[:end]: digest = chain_step(digest, row)
                self.assertEqual(self.commits[-1][1]['covered'], end)
                self.assertEqual(self.commits[-1][1]['digest'], digest)
                self.assertTrue(self.commits[-1][1]['built'])
                self.assertEqual(sum(len(labelled) for labelled, _ in self.commits), len(F.blocks(rows[:end])))


class StoredCompletionTests(AnalysisCase):
    def corrupt_chat(self, *, legacy):
        db = self.plugin_database()
        scan = RunStore(FriendStore(db), lambda: 1).published_scan(USER, 'chat')
        block = db._row('SELECT block_id FROM friend_chat_blocks WHERE user=? AND generation=? AND emotion IS NOT NULL LIMIT 1',
                        (USER, scan['generation']))['block_id']
        db._connection.execute('UPDATE friend_chat_blocks SET emotion=NULL WHERE user=? AND generation=? AND block_id=?',
                               (USER, scan['generation'], block))
        if legacy:
            db._connection.execute("UPDATE friend_scans SET version=? WHERE user=? AND step='chat'",
                                   (F.LEGACY_CHAT_VERSION, USER))
        db._connection.commit()
        return scan

    def test_old_done_repair_reuses_good_fields_and_leaves_other_scans_untouched(self):
        self.assertEqual(self.analyse()['state'], 'done')
        db = self.plugin_database()
        old = self.corrupt_chat(legacy=True)
        persona = db._rows("SELECT * FROM friend_scans WHERE step='persona'")
        memories = db._rows('SELECT * FROM friend_jobs')
        old_labels = db._rows('SELECT * FROM friend_chat_blocks WHERE generation=?', (old['generation'],))
        calls = self.runtime.count('chat')
        self.friends.analysis_start(ACCOUNT, USER, 'legacy-completion-repair', 'update')
        self.assertEqual(self.wait_run()['state'], 'done')
        current = RunStore(FriendStore(db), lambda: 1).published_scan(USER, 'chat')
        self.assertEqual(current['version'], F.CHAT_VERSION)
        self.assertGreater(current['generation'], old['generation'])
        self.assertEqual(self.runtime.count('chat') - calls, 1)
        rows = db.context_messages(USER, None)
        new_labels = RunStore(FriendStore(db), lambda: 1).chat_blocks(USER, current['generation'])
        self.assertTrue(C.chat_payload_valid(new_labels, rows, current['covered_count']))
        new_by_id = {item['block_id']: item for item in new_labels}
        for item in old_labels:
            for field in ('intent', 'emotion', 'response', 'invite'):
                if item[field] is not None:
                    self.assertEqual(new_by_id[item['block_id']][field], item[field])
        self.assertEqual(db._rows("SELECT * FROM friend_scans WHERE step='persona'"), persona)
        self.assertEqual(db._rows('SELECT * FROM friend_jobs'), memories)

    def test_current_fingerprint_incomplete_done_is_not_reused_and_failure_is_partial(self):
        self.assertEqual(self.analyse()['state'], 'done')
        db = self.plugin_database()
        old = self.corrupt_chat(legacy=False)
        self.runtime.answer_chat = lambda _prompt: {'blocks': []}
        calls = self.runtime.count('chat')
        with patch.object(A, 'RETRY_DELAYS', (0, 0)):
            self.friends.analysis_start(ACCOUNT, USER, 'incomplete-v2-repair', 'update')
            run = self.wait_run()
        self.assertEqual(run['state'], 'partial')
        self.assertEqual(next(step for step in run['steps'] if step['key'] == 'chat')['state'], 'failed')
        self.assertEqual(self.runtime.count('chat') - calls, 3)
        self.assertEqual(RunStore(FriendStore(db), lambda: 1).published_scan(USER, 'chat')['generation'], old['generation'])
        page = self.friends.page(ACCOUNT, USER, 'chat')
        self.assertGreater(page['coverage']['missingTextBlocks'], 0)
        self.assertTrue(page['coverage']['outdated'])

    def test_complete_legacy_fields_publish_v2_without_model_relabelling(self):
        self.assertEqual(self.analyse()['state'], 'done')
        db = self.plugin_database()
        db._connection.execute("UPDATE friend_scans SET version=? WHERE user=? AND step='chat'",
                               (F.LEGACY_CHAT_VERSION, USER))
        db._connection.commit()
        calls = self.runtime.count('chat')
        self.friends.analysis_start(ACCOUNT, USER, 'copy-complete-legacy-fields', 'update')
        self.assertEqual(self.wait_run()['state'], 'done')
        self.assertEqual(self.runtime.count('chat'), calls)
        self.assertEqual(RunStore(FriendStore(db), lambda: 1).published_scan(USER, 'chat')['version'], F.CHAT_VERSION)


if __name__ == '__main__':
    unittest.main()
