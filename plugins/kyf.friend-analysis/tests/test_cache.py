"""Route-only cache reuse with synthetic model identities and histories."""
import copy
import hashlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import harness  # noqa: F401 - add the production import roots
import friend_cache
import friend_chat
import friend_persona
from analysis_store import FriendStore, chain_step
from friend_analysis import FriendAnalysis
from friend_extract import EXTRACT_VERSION
from friend_run_store import RunStore
from memory import MemoryJobs
from test_analysis import ACCOUNT, USER, AnalysisCase, msg


def service(revision=23, **changes):
    value = {'cacheFingerprint': hashlib.sha256(b'synthetic protocol endpoint alias context').hexdigest(),
             'actualModel': 'synthetic-weights', 'backendId': 'synthetic-backend', 'routeRevision': revision}
    value.update(changes)
    raw = [value['cacheFingerprint'], [value['actualModel'], value['backendId'], value['routeRevision']]]
    value['fingerprint'] = hashlib.sha256(json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return value


def config(revision=23, **changes):
    value = service(revision, **changes)
    return {'modelFp': value['fingerprint'], 'cacheIdentity': friend_cache.model_identity(value), 'contextTokens': 32768}


def message(seq, side='other', text='合成测试对话'):
    return {'seq': seq, 'msg_id': f'm{seq}', 'side': side, 'text': text, 'kind': 'text',
            'time_ms': 1700000000000 + seq * 60000, 'sort_key': json.dumps([seq]),
            'sender_id': '', 'sender_name': '', 'raw': '{}'}


def stored(row):
    return {**copy.deepcopy(row), 'user': USER, 'generation': 1,
            'msg_ids': json.dumps(row['msg_ids']), 'hashes': json.dumps(row['hashes'])}


def digest(rows):
    value = ''
    for row in rows:
        value = chain_step(value, row)
    return value


class IdentityTests(unittest.TestCase):
    def test_exact_route_proof_preserves_all_semantic_identity_fields(self):
        old = service(3)['fingerprint']
        self.assertTrue(friend_cache.route_equivalent(old, config(49)))
        for field, value in (('cacheFingerprint', 'e' * 64), ('actualModel', 'different'), ('backendId', 'different')):
            self.assertFalse(friend_cache.route_equivalent(old, config(49, **{field: value})))
        self.assertFalse(friend_cache.route_equivalent(service(10001)['fingerprint'], config(49)))
        self.assertFalse(friend_cache.route_equivalent('unknown', config(49)))

    def test_incomplete_or_inconsistent_descriptor_is_not_proof(self):
        for key in ('cacheFingerprint', 'actualModel', 'backendId', 'routeRevision', 'fingerprint'):
            value = service()
            value.pop(key)
            self.assertIsNone(friend_cache.model_identity(value))
        for revision in (True, '23', None, -1):
            self.assertIsNone(friend_cache.model_identity(service(revision)))
        self.assertIsNone(friend_cache.model_identity({**service(), 'fingerprint': 'a' * 64}))
        self.assertFalse(friend_cache.route_equivalent(service(3)['fingerprint'], {'modelFp': 'legacy'}))

    def test_bounded_proof_set_is_cached_per_model_not_per_evidence(self):
        friend_cache._legacy_fingerprints.cache_clear()
        for _ in range(10):
            self.assertTrue(friend_cache.route_equivalent(service(3)['fingerprint'], config(49)))
        self.assertEqual(friend_cache._legacy_fingerprints.cache_info().misses, 1)


class PayloadTests(unittest.TestCase):
    def test_canonical_lean_roundtrip_still_rejects_changed_fields_and_source(self):
        rows = [message(1)]
        dim = next(iter(friend_persona.POLES))
        for lean in (*friend_persona.POLES[dim], '两者'):
            evidence = friend_persona.validate_evidence(
                [{'dim': dim, 'lean': lean, 'note': '合成依据', 'msgs': [1]}], rows, channel='p')[0]
            cached = stored(evidence)
            self.assertTrue(friend_cache.persona_payload_valid([cached], rows, 1))
            for field, replacement in (('excerpt', '篡改'), ('id', 'pe_legacy'), ('hashes', '["bad"]')):
                self.assertFalse(friend_cache.persona_payload_valid([{**cached, field: replacement}], rows, 1))
            self.assertFalse(friend_cache.persona_payload_valid([cached], rows, 0))
            self.assertFalse(friend_cache.persona_payload_valid([cached], [message(1, text='内容变化')], 1))

    def test_chat_roundtrip_checks_invite_and_reply_relationships(self):
        rows = [message(1, 'self', '一起散步吗'), message(2, 'other', '好呀'), message(3, 'self', '下一轮')]
        blocks = friend_chat.blocks(rows[:2])
        numbers = {row['id']: index + 1 for index, row in enumerate(blocks)}
        items = [{'b': 1, 'intent': '邀请', 'emotion': '积极'},
                 {'b': 2, 'intent': '回应', 'emotion': '积极', 'response': '接住并展开', 'invite': '明确接受'}]
        cached = [stored(row) for row in friend_chat.validate(items, blocks, numbers)]
        self.assertTrue(friend_cache.chat_payload_valid(cached, rows, 2))
        for key, value in (('intent', '分享'), ('response', '接住并展开'), ('hashes', '["bad"]')):
            altered = copy.deepcopy(cached)
            altered[0][key] = value
            self.assertFalse(friend_cache.chat_payload_valid(altered, rows, 2))
        self.assertFalse(friend_cache.chat_payload_valid(cached[:-1], rows, 2))


class MemoryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.rows = [message(index) for index in range(1, 5)]
        self.point = {'source_fp': 'source', 'extract_version': EXTRACT_VERSION, 'covered_count': 2,
                      'chain_digest': digest(self.rows[:2]), 'clear_epoch': 0, 'built': 0}
        self.latest = {'id': 'old', 'model_fp': service(3)['fingerprint'], 'source_fp': 'source',
                       'extract_version': EXTRACT_VERSION, 'clear_epoch': 0, 'kind': 'full', 'state': 'error',
                       'done_messages': 2, 'start_count': 0}
        self.context = {'state': 'ready', 'source_id': 'source', 'epoch': 'new-epoch', 'read_count': 4}
        self.database = SimpleNamespace(account=ACCOUNT, context=lambda user: self.context,
            context_messages=lambda user, limit: self.rows if limit is None else self.rows[:limit])
        self.store = SimpleNamespace(checkpoint=lambda user: self.point, state=lambda user: {'clear_epoch': 0},
            latest_job=lambda user: self.latest, create_job=Mock(side_effect=lambda user, kind, req, fields: fields),
            restart_job=Mock())
        self.jobs = MemoryJobs()

    def compatible(self):
        coverage = self.jobs.coverage(self.database, self.store, USER, 'source')
        return self.jobs.compatible(self.database, self.store, USER, config(), 'source', self.latest, coverage)

    def test_alias_requires_current_source_algorithm_clear_and_full_prefix(self):
        self.assertTrue(self.compatible())
        for key, bad in (('source_fp', 'other'), ('extract_version', 'old'), ('clear_epoch', 1)):
            old = self.latest[key]
            self.latest[key] = bad
            self.assertFalse(self.compatible(), key)
            self.latest[key] = old
        self.point['clear_epoch'] = None
        self.assertFalse(self.compatible())
        self.point['clear_epoch'] = 0
        self.context['error'] = 'not-ready'
        self.assertFalse(self.compatible())
        self.context.pop('error')
        self.rows[0]['text'] = 'changed'
        self.assertFalse(self.compatible())

    def test_full_zero_done_does_not_reuse_older_checkpoint_and_short_positive_fails_closed(self):
        self.latest['done_messages'] = 0
        self.assertTrue(self.compatible())
        self.assertEqual(self.jobs.plan(self.database, self.store, USER, config(), 'source')['messages'], 4)
        self.latest['done_messages'] = 1
        self.assertFalse(self.compatible(), 'a shorter full-job prefix cannot use the checkpoint digest')

    def test_alias_resume_creates_new_job_without_rewriting_old_identity_or_progress(self):
        original = copy.deepcopy(self.latest)
        ctx = SimpleNamespace(database=self.database, store=self.store, user=USER, fingerprint='source', config=config())
        result = self.jobs._start(ctx, 'full', self.latest)
        self.store.create_job.assert_called_once()
        self.store.restart_job.assert_not_called()
        self.assertEqual(result['done_messages'], 2)
        self.assertEqual(result['model_fp'], config()['modelFp'])
        self.assertEqual(self.latest, original)


class RouteIntegrationTests(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.revision = 3
        base_services = self.harness.plugin.models.services
        self.harness.plugin.models.services = lambda refresh=False: [
            {**base_services(refresh)[0], **service(self.revision), 'stamp': f'route-{self.revision}'}]

    def test_route_only_update_reuses_verified_scans_but_rebuilds_final_pages(self):
        self.assertEqual(self.analyse()['state'], 'done')
        db = self.plugin_database()
        old_jobs = db._rows('SELECT id,state,model_fp,done_messages FROM friend_jobs')
        old_scans = db._rows('SELECT step,generation,model_fp FROM friend_scans')
        calls = {key: self.runtime.count(key) for key in ('extract', 'evidence', 'chat', 'summary', 'next', 'synth')}
        self.revision = 49
        self.friends.analysis_start(ACCOUNT, USER, 'route-only-update', 'update')
        self.assertEqual(self.wait_run()['state'], 'done')
        for key in ('extract', 'evidence', 'chat'):
            self.assertEqual(self.runtime.count(key), calls[key], key)
        for key in ('summary', 'next', 'synth'):
            self.assertGreater(self.runtime.count(key), calls[key], key)
        self.assertEqual(db._rows('SELECT id,state,model_fp,done_messages FROM friend_jobs'), old_jobs)
        self.assertEqual(db._rows('SELECT step,generation,model_fp FROM friend_scans'), old_scans)
        for page in db._rows('SELECT model_fp,deps FROM friend_results'):
            self.assertEqual(page['model_fp'], service(49)['fingerprint'])
            self.assertEqual(json.loads(page['deps'])['model'], service(49)['fingerprint'])

    def test_strict_current_scan_wins_and_alias_version_source_payload_guards_hold(self):
        self.assertEqual(self.analyse()['state'], 'done')
        db, analysis = self.plugin_database(), self.harness.analysis
        store = FriendStore(db)
        rows = db.context_messages(USER, None)
        source = db.context(USER)['source_id']
        self.revision = 49
        current = analysis.config()
        def state():
            return analysis.scan_state(ACCOUNT, USER, store, 'persona', rows, len(rows),
                                       current['modelFp'], source, current)
        self.assertIsNotNone(state()['continue'])
        old = RunStore(store, lambda: 1).published_scan(USER, 'persona')
        for field, value in (('version', 'friend-persona-v1'), ('source_fp', 'other'), ('chain_digest', 'bad')):
            db._connection.execute(f'UPDATE friend_scans SET {field}=? WHERE step=\'persona\'', (value,))
            db._connection.commit()
            self.assertIsNone(state()['continue'], field)
            db._connection.execute(f'UPDATE friend_scans SET {field}=? WHERE step=\'persona\'', (old[field],))
            db._connection.commit()
        db._connection.execute("UPDATE context SET error='not-ready' WHERE user=?", (USER,))
        db._connection.commit()
        self.assertIsNone(state()['continue'])
        db._connection.execute('UPDATE context SET error=NULL WHERE user=?', (USER,))
        prior = db._row('SELECT rowid,excerpt FROM friend_persona_evidence ORDER BY rowid LIMIT 1')
        db._connection.execute("UPDATE friend_persona_evidence SET excerpt='corrupt' WHERE rowid=(SELECT min(rowid) FROM friend_persona_evidence)")
        db._connection.commit()
        self.assertIsNone(state()['continue'])
        db._connection.execute('UPDATE friend_persona_evidence SET excerpt=? WHERE rowid=?', (prior['excerpt'], prior['rowid']))
        db._connection.commit()
        self.assertEqual(state()['start'], len(rows), 'the equivalent old scan is valid and longer')
        # A current-model generation is preferred even if it has less coverage.
        generation = RunStore(store, lambda: 1).begin_scan(USER, 'persona',
            {'source_fp': source, 'version': friend_persona.PERSONA_VERSION, 'model_fp': current['modelFp']})
        self.assertEqual(state()['continue']['generation'], generation)
        self.assertEqual(state()['start'], 0)

    def test_explicit_rebuild_still_reextracts_after_route_change(self):
        self.assertEqual(self.analyse()['state'], 'done')
        before = {key: self.runtime.count(key) for key in ('extract', 'evidence', 'chat')}
        self.revision = 49
        self.friends.analysis_start(ACCOUNT, USER, 'route-rebuild', 'rebuild')
        self.assertEqual(self.wait_run()['state'], 'done')
        for key in before:
            self.assertGreater(self.runtime.count(key), before[key], key)

    def test_route_equivalence_does_not_weaken_inflight_config_stamp(self):
        self.assertEqual(self.analyse()['state'], 'done')
        before = self.plugin_database()._row("SELECT payload,model_fp FROM friend_results WHERE page='persona'")
        self.runtime.entered.pop('synth', None)
        self.runtime.gate['synth'] = gate = threading.Event()
        self.revision = 49
        self.friends.analysis_start(ACCOUNT, USER, 'route-inflight', 'update')
        from test_support import wait_until
        self.assertTrue(wait_until(lambda: self.runtime.entered.get('synth') and self.runtime.entered['synth'].is_set()))
        self.revision = 50
        gate.set()
        self.assertEqual(self.wait_run()['state'], 'interrupted')
        self.assertEqual(self.plugin_database()._row("SELECT payload,model_fp FROM friend_results WHERE page='persona'"), before)


if __name__ == '__main__':
    unittest.main()
