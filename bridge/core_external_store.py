"""Core-owned external chat records, evidence and resumable review state.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

All paths are beneath the explicitly supplied local data directory. Recognition
is deliberately absent: plugins submit observations; Core controls persistence,
identity, review validation and publication of historical records.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import mimetypes
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS friends (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, wechat_account TEXT, wechat_user TEXT,
 created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS friend_wechat ON friends(wechat_account,wechat_user)
 WHERE wechat_account IS NOT NULL AND wechat_user IS NOT NULL;
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, friend_id TEXT NOT NULL REFERENCES friends(id), platform TEXT NOT NULL,
 own_account TEXT NOT NULL, conversation_key TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 UNIQUE(platform,own_account,conversation_key)
);
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), note TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 ordinal INTEGER NOT NULL UNIQUE, archived INTEGER NOT NULL DEFAULT 0, direction TEXT NOT NULL DEFAULT 'forward'
);
CREATE TABLE IF NOT EXISTS materials (
 id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, name TEXT NOT NULL, kind TEXT NOT NULL,
 mime TEXT NOT NULL, size INTEGER NOT NULL, path TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_materials (
 batch_id TEXT NOT NULL REFERENCES batches(id), material_id TEXT NOT NULL REFERENCES materials(id),
 position INTEGER NOT NULL, PRIMARY KEY(batch_id,material_id)
);
CREATE TABLE IF NOT EXISTS segments (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id), material_id TEXT NOT NULL REFERENCES materials(id),
 segment_key TEXT NOT NULL, position INTEGER NOT NULL, video_ms INTEGER, path TEXT, state TEXT NOT NULL,
 error TEXT, warnings TEXT NOT NULL DEFAULT '[]', UNIQUE(batch_id,material_id,segment_key)
);
CREATE TABLE IF NOT EXISTS candidates (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id), segment_id TEXT NOT NULL REFERENCES segments(id),
 position INTEGER NOT NULL, identity_key TEXT, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 message_id TEXT, UNIQUE(segment_id,position)
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), origin_batch TEXT NOT NULL,
 identity_key TEXT, sort_key TEXT NOT NULL, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS messages_source ON messages(source_id,sort_key);
CREATE INDEX IF NOT EXISTS messages_identity ON messages(source_id,identity_key);
CREATE TABLE IF NOT EXISTS batch_messages (
 batch_id TEXT NOT NULL REFERENCES batches(id), candidate_id TEXT NOT NULL REFERENCES candidates(id),
 message_id TEXT NOT NULL REFERENCES messages(id), PRIMARY KEY(batch_id,candidate_id)
);
CREATE INDEX IF NOT EXISTS message_refs ON batch_messages(message_id);
CREATE TABLE IF NOT EXISTS versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target_id TEXT NOT NULL,
 version INTEGER NOT NULL, reason TEXT NOT NULL, data TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO metadata VALUES ('revision',0);
"""
STATES = frozenset(('draft', 'queued', 'running', 'stopping', 'stopped', 'failed', 'interrupted', 'review', 'partial', 'saved'))
ACTIVE = frozenset(('queued', 'running', 'stopping'))
TIME_ISSUES = frozenset(('time-unknown', 'time-incomplete', 'unknown-time', 'missing-time', 'date-incomplete', 'incomplete_time', 'missing_original', '时间不完整', '时间未知'))
AUTO_REREAD_ISSUES = frozenset(('unclear_text', 'unknown_sender', 'truncated', 'bubble-truncated',
                                'text-unclear', 'sender-unknown', 'text-empty', 'recognition-difference'))


def _id(prefix):
    return prefix + '_' + uuid.uuid4().hex


def _now():
    return int(time.time() * 1000)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _observation_text(value):
    """Match tracker line wrapping without altering any retained transcript."""
    return unicodedata.normalize('NFC', value).replace('\r\n', '\n').replace('\n', '').strip() if isinstance(value, str) else ''


def _text(value, field, limit=10000, required=False):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value or (required and not value.strip()):
        raise ValueError('invalid ' + field)
    return value.strip() if required else value


def _number(value, field, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid ' + field)
    return value


def _issues(value):
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError('invalid issues')
    return list(dict.fromkeys(_text(x, 'issue', 120, True) for x in value))


def _blocking(data):
    result = [issue for issue in data.get('issues', []) if issue not in TIME_ISSUES]
    if data.get('sender') not in ('self', 'other') and not (data.get('kind') == 'system' and data.get('sender') == 'unknown'):
        result.append('sender-unknown')
    if not data.get('text', '').strip():
        result.append('text-empty')
    if '\u25a1' in data.get('text', '') or '\ufffd' in data.get('text', ''):
        result.append('text-unclear')
    if data.get('truncated'):
        result.append('bubble-truncated')
    return list(dict.fromkeys(result))


def _order_key(segment_position, position, data):
    """Position of a bubble within its batch: recognition order unless the user moved it."""
    return data.get('orderKey') or '%012d:%012d' % (segment_position, position)


def _in_order(rows):
    """Candidate rows (with segment_position) of one batch in review order."""
    return sorted(rows, key=lambda r: (_order_key(r['segment_position'], r['position'], json.loads(r['data'])), r['id']))


# Contiguous ASCII from '/' (the implicit trailing digit) up to 'z'; recognition keys use
# only digits and ':', so a moved bubble's key always sorts between its two neighbors.
ORDER_DIGITS = ''.join(map(chr, range(0x2f, 0x7b)))


def _order_between(low, high):
    """Shortest key sorting strictly after ``low`` and before ``high`` (None: no bound)."""
    if high is not None:
        n = 0
        while n < len(high) and (low[n] if n < len(low) else ORDER_DIGITS[0]) == high[n]:
            n += 1
        if n:
            return high[:n] + _order_between(low[n:], high[n:])
    a = ORDER_DIGITS.index(low[0]) if low else 0
    b = ORDER_DIGITS.index(high[0]) if high is not None else len(ORDER_DIGITS)
    if b - a > 1:
        return ORDER_DIGITS[(a + b + 1) // 2]
    if high is not None and len(high) > 1:
        return high[:1]
    return ORDER_DIGITS[a] + _order_between(low[1:], None)


class ExternalChatStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'originals').mkdir(exist_ok=True)
        (self.root / 'evidence').mkdir(exist_ok=True)
        (self.root / 'media').mkdir(exist_ok=True)
        self.path = self.root / 'external-chat.sqlite3'
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA busy_timeout=5000')
        self.connection.executescript(SCHEMA)
        if 'direction' not in {r[1] for r in self.connection.execute('PRAGMA table_info(batches)')}:
            self.connection.execute("ALTER TABLE batches ADD COLUMN direction TEXT NOT NULL DEFAULT 'forward'")
        if 'warnings' not in {r[1] for r in self.connection.execute('PRAGMA table_info(segments)')}:
            self.connection.execute("ALTER TABLE segments ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]'")

    def close(self):
        with self.lock:
            self.connection.close()

    @contextmanager
    def _write(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise

    def _one(self, table, item_id):
        row = self.connection.execute('SELECT * FROM ' + table + ' WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise KeyError(table + ': ' + str(item_id))
        return row

    def _bump(self):
        self.connection.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")

    def revision(self):
        with self.lock:
            return self.connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]

    def _version(self, kind, item_id, version, data, reason):
        self.connection.execute('INSERT INTO versions(kind,target_id,version,reason,data,created_at) VALUES(?,?,?,?,?,?)',
                                (kind, item_id, version, reason, _dump(data), _now()))

    def versions(self, kind, item_id):
        if kind not in ('candidate', 'message'):
            raise ValueError('invalid version kind')
        with self.lock:
            return [{'version': r['version'], 'reason': r['reason'], 'data': json.loads(r['data']), 'createdAt': r['created_at']}
                    for r in self.connection.execute('SELECT * FROM versions WHERE kind=? AND target_id=? ORDER BY id', (kind, item_id))]

    @staticmethod
    def _friend(row):
        return {'id': row['id'], 'name': row['name'], 'wechatAccount': row['wechat_account'],
                'wechatUser': row['wechat_user'], 'createdAt': row['created_at']}

    @staticmethod
    def _source(row):
        return {'id': row['id'], 'friendId': row['friend_id'], 'platform': row['platform'],
                'ownAccount': row['own_account'], 'conversationKey': row['conversation_key'], 'note': row['note']}

    def create_friend(self, name, wechat_account=None, wechat_user=None):
        name = _text(name, 'name', 200, True)
        if (wechat_account is None) != (wechat_user is None):
            raise ValueError('explicit WeChat account and user are both required')
        if wechat_account is not None:
            wechat_account = _text(wechat_account, 'wechatAccount', 256, True)
            wechat_user = _text(wechat_user, 'wechatUser', 256, True)
        with self._write() as db:
            existing = db.execute('SELECT * FROM friends WHERE wechat_account=? AND wechat_user=?', (wechat_account, wechat_user)).fetchone()
            if existing:
                return self._friend(existing)
            friend_id = _id('friend')
            db.execute('INSERT INTO friends VALUES(?,?,?,?,?)', (friend_id, name, wechat_account, wechat_user, _now()))
            return self._friend(self._one('friends', friend_id))

    def list_friends(self):
        with self.lock:
            return [self._friend(r) for r in self.connection.execute('SELECT * FROM friends ORDER BY created_at,id')]

    def list_sources(self, friend_id):
        """Sources still in use. One whose tasks were all deleted before anything was saved
        has nothing left to show or act on, so it leaves the history, tabs and import choices."""
        with self.lock:
            self._one('friends', friend_id)
            return [self._source(r) for r in self.connection.execute(
                'SELECT * FROM sources s WHERE friend_id=? AND (EXISTS(SELECT 1 FROM messages m WHERE m.source_id=s.id)'
                ' OR EXISTS(SELECT 1 FROM batches b WHERE b.source_id=s.id AND b.archived=0)) ORDER BY rowid', (friend_id,))]

    def _ensure_source(self, friend_id, platform, own_account, conversation_key, note=''):
        self._one('friends', friend_id)
        platform = _text(platform, 'platform', 80, True)
        own_account = _text(own_account, 'ownAccount', 256, True)
        conversation_key = _text(conversation_key, 'conversationKey', 256, True)
        note = _text(note, 'sourceNote')
        row = self.connection.execute('SELECT * FROM sources WHERE platform=? AND own_account=? AND conversation_key=?',
                                      (platform, own_account, conversation_key)).fetchone()
        if row:
            if row['friend_id'] != friend_id:
                raise ValueError('这个来源会话已关联另一位好友；请选择不同的会话标识，或先修正已有来源关联。')
            return row['id']
        source_id = _id('source')
        self.connection.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)', (source_id, friend_id, platform, own_account, conversation_key, note))
        return source_id

    def create_batch(self, friend_id, platform, own_account, conversation_key, note='', source_note='', direction='forward'):
        note = _text(note, 'note')
        if direction not in ('forward', 'reverse'):
            raise ValueError('invalid direction')
        with self._write() as db:
            source_id = self._ensure_source(friend_id, platform, own_account, conversation_key, source_note)
            batch_id, stamp = _id('batch'), _now()
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM batches').fetchone()[0]
            db.execute('INSERT INTO batches(id,source_id,note,state,created_at,updated_at,ordinal,direction) VALUES(?,?,?,?,?,?,?,?)',
                       (batch_id, source_id, note, 'draft', stamp, stamp, ordinal, direction))
            return self.get_batch(batch_id)

    def update_notes(self, batch_id, note=None, source_note=None):
        with self._write() as db:
            batch = self._one('batches', batch_id)
            if note is not None:
                db.execute('UPDATE batches SET note=?,updated_at=? WHERE id=?', (_text(note, 'note'), _now(), batch_id))
            if source_note is not None:
                db.execute('UPDATE sources SET note=? WHERE id=?', (_text(source_note, 'sourceNote'), batch['source_id']))
            return self.get_batch(batch_id)

    def _managed(self, relative):
        if not relative:
            return None
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError('file is outside local evidence storage')
        return path

    def _copy(self, path, category):
        origin = Path(path).resolve(strict=True)
        if not origin.is_file():
            raise ValueError('material must be a file')
        digest = hashlib.sha256()
        size = 0
        # Hash the actual copy so later changes to the user's file cannot alter its identity.
        temporary = self.root / category / (uuid.uuid4().hex + '.upload')
        try:
            with origin.open('rb') as reader, temporary.open('xb') as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                    digest.update(chunk)
                    size += len(chunk)
                    writer.write(chunk)
            checksum = digest.hexdigest()
            target = self.root / category / (checksum + origin.suffix.lower()[:12])
            if target.exists():
                temporary.unlink()
            else:
                temporary.replace(target)
            return str(target.relative_to(self.root)), checksum, size
        finally:
            temporary.unlink(missing_ok=True)

    def add_material(self, batch_id, path, kind=None):
        origin = Path(path)
        mime = mimetypes.guess_type(origin.name)[0] or 'application/octet-stream'
        kind = kind or ('video' if mime.startswith('video/') else 'image')
        if kind not in ('image', 'video'):
            raise ValueError('material kind must be image or video')
        with self._write() as db:
            batch = self._one('batches', batch_id)
            if batch['state'] in ACTIVE:
                raise ValueError('请先停止识别，再修改素材。')
            relative, checksum, size = self._copy(path, 'originals')
            row = db.execute('SELECT * FROM materials WHERE sha256=?', (checksum,)).fetchone()
            if row:
                material_id = row['id']
                db.execute('UPDATE materials SET path=? WHERE id=?', (relative, material_id))
            else:
                material_id = _id('material')
                db.execute('INSERT INTO materials VALUES(?,?,?,?,?,?,?,?)',
                           (material_id, checksum, origin.name, kind, mime, size, relative, _now()))
            position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM batch_materials WHERE batch_id=?', (batch_id,)).fetchone()[0]
            db.execute('INSERT OR IGNORE INTO batch_materials VALUES(?,?,?)', (batch_id, material_id, position))
            return next(m for m in self.get_batch(batch_id)['materials'] if m['id'] == material_id)

    def material_path(self, material_id):
        with self.lock:
            path = self._managed(self._one('materials', material_id)['path'])
            return path if path and path.is_file() else None

    def evidence_path(self, segment_id):
        with self.lock:
            path = self._managed(self._one('segments', segment_id)['path'])
            return path if path and path.is_file() else None

    @staticmethod
    def _unit_box(value):
        return (isinstance(value, list) and len(value) == 4 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in value)
                and value[2] > 0 and value[3] > 0 and value[0] + value[2] <= 1.001 and value[1] + value[3] <= 1.001)

    def _picture(self, row):
        """Crop a sent picture from the frames showing it; (key, path) or (None, None).

        The crop is cached by the observations it came from, so it outlives a
        later evidence cleanup and a saved record keeps its own copy.
        """
        data = json.loads(row['data'])
        if data.get('kind') != 'image':
            return None, None
        own = data.get('original', data).get('bbox')
        seen = [(data.get('preferredEvidenceSegmentId', row['segment_id']), data.get('bbox')), (row['segment_id'], own)]
        for sibling in self.connection.execute('SELECT segment_id,data FROM candidates WHERE batch_id=? AND id<>?', (row['batch_id'], row['id'])):
            sibling_data = json.loads(sibling['data'])
            if sibling_data.get('autoMergedInto') == row['id']:
                seen.append((sibling['segment_id'], sibling_data.get('original', sibling_data).get('bbox')))
        observations = list(dict.fromkeys((segment, tuple(box)) for segment, box in seen if self._unit_box(box)))
        if not observations:
            return None, None
        key = hashlib.sha256(_dump([1, observations]).encode()).hexdigest()
        target = self.root / 'media' / (key + '.png')
        if target.is_file():
            return key, target
        missing = target.with_suffix('.none')
        frames = [(self.evidence_path(segment), list(box)) for segment, box in observations]
        frames = [(path, box) for path, box in frames if path]
        if missing.is_file() or not frames:
            return None, None
        from core_external_media import crop_picture
        if crop_picture(frames, target):
            return key, target
        missing.touch()
        return None, None

    def picture(self, candidate_id=None, message_id=None):
        """Return the local picture shown by an image candidate or saved record, if any."""
        with self.lock:
            if message_id:
                row = self._one('messages', message_id)
                path = self._media(json.loads(row['data']).get('media'))
                if path:
                    return path
                ref = self.connection.execute('SELECT candidate_id FROM batch_messages WHERE message_id=? ORDER BY rowid LIMIT 1', (message_id,)).fetchone()
                if not ref:
                    return None
                candidate_id = ref[0]
            return self._picture(self._one('candidates', candidate_id))[1]

    def _media(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            return None
        path = self.root / 'media' / (key + '.png')
        return path if path.is_file() else None

    def reorder_materials(self, batch_id, ids):
        with self._write() as db:
            batch = self.get_batch(batch_id)
            if batch['state'] in ACTIVE or batch['segments']:
                raise ValueError('只能在首次识别前调整素材顺序。')
            existing = {m['id'] for m in batch['materials']}
            if not isinstance(ids, list) or len(ids) != len(existing) or set(ids) != existing:
                raise ValueError('include every material exactly once')
            for index, material_id in enumerate(ids):
                db.execute('UPDATE batch_materials SET position=? WHERE batch_id=? AND material_id=?', (index, batch_id, material_id))
            return self.get_batch(batch_id)

    def remove_material(self, batch_id, material_id):
        with self._write() as db:
            batch = self._one('batches', batch_id)
            if batch['state'] in ACTIVE or db.execute('SELECT 1 FROM segments WHERE batch_id=? AND material_id=?', (batch_id, material_id)).fetchone():
                raise ValueError('这份素材已用于识别，当前任务仍需要它。请使用分项清理查看影响。')
            db.execute('DELETE FROM batch_materials WHERE batch_id=? AND material_id=?', (batch_id, material_id))
            return self.get_batch(batch_id)

    def add_segment(self, batch_id, material_id, key, order, path=None, video_ms=None):
        key = _text(key, 'segment key', 256, True)
        order = int(_number(order, 'order', False))
        video_ms = _number(video_ms, 'videoMs')
        with self._write() as db:
            self._one('batches', batch_id)
            if not db.execute('SELECT 1 FROM batch_materials WHERE batch_id=? AND material_id=?', (batch_id, material_id)).fetchone():
                raise ValueError('material does not belong to this batch')
            existing = db.execute('SELECT * FROM segments WHERE batch_id=? AND material_id=? AND segment_key=?', (batch_id, material_id, key)).fetchone()
            if existing:
                return self._segment(existing)
            relative = self._copy(path, 'evidence')[0] if path else None
            segment_id = _id('segment')
            db.execute('INSERT INTO segments(id,batch_id,material_id,segment_key,position,video_ms,path,state,error) VALUES(?,?,?,?,?,?,?,?,?)',
                       (segment_id, batch_id, material_id, key, order, video_ms, relative, 'pending', None))
            return self._segment(self._one('segments', segment_id))

    def _segment(self, row):
        return {'id': row['id'], 'batchId': row['batch_id'], 'materialId': row['material_id'],
                'key': row['segment_key'], 'order': row['position'], 'videoMs': row['video_ms'],
                'state': row['state'], 'error': row['error'], 'warnings': json.loads(row['warnings']),
                'evidenceAvailable': bool(self.evidence_path(row['id']))}

    def set_segment_warnings(self, segment_id, warnings):
        if not isinstance(warnings, list) or len(warnings) > 100:
            raise ValueError('invalid warnings')
        warnings = list(dict.fromkeys(_text(w, 'warning', 2000, True) for w in warnings))
        with self._write() as db:
            self._one('segments', segment_id)
            db.execute('UPDATE segments SET warnings=? WHERE id=?', (_dump(warnings), segment_id))
            return self._segment(self._one('segments', segment_id))

    @staticmethod
    def _normalize_candidate(value):
        if not isinstance(value, dict):
            raise ValueError('candidate must be an object')
        sender = value.get('sender', 'unknown')
        if sender not in ('self', 'other', 'unknown'):
            raise ValueError('invalid sender')
        bbox = value.get('bbox')
        if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4 or any(_number(v, 'bbox', False) > 100000 for v in bbox)):
            raise ValueError('invalid bbox')
        precision = value.get('timePrecision') or ('exact' if value.get('time') is not None else 'unknown')
        if precision not in ('unknown', 'exact', 'date', 'partial', 'relative'):
            raise ValueError('invalid time precision')
        if value.get('time') is None and precision == 'exact':
            precision = 'partial' if value.get('dateLabel') else 'unknown'
        return {'sender': sender, 'text': _text(value.get('text', ''), 'text', 100000),
                'time': _number(value.get('time'), 'time'), 'dateLabel': _text(value.get('dateLabel') or '', 'dateLabel', 200),
                'timePrecision': precision, 'issues': _issues(value.get('issues', [])),
                'bbox': bbox, 'truncated': bool(value.get('truncated', False)), 'reviewed': False, 'ignored': False,
                'kind': value.get('kind') if value.get('kind') in ('text', 'image', 'voice', 'video', 'system') else 'text',
                'quote': _text(value.get('quote') or '', 'quote', 10000)}

    def _candidate(self, row):
        data = json.loads(row['data'])
        return {**data, 'id': row['id'], 'batchId': row['batch_id'], 'segmentId': row['segment_id'],
                'evidenceSegmentId': data.get('preferredEvidenceSegmentId', row['segment_id']),
                'order': row['position'], 'version': row['version'], 'savedMessageId': row['message_id'],
                'ready': not data.get('ignored') and not data.get('autoMergedInto') and not _blocking(data)}

    def segment_candidates(self, segment_id, include_merged=True):
        """Return auditable observations, including hidden overlap siblings."""
        with self.lock:
            self._one('segments', segment_id)
            rows = self.connection.execute('SELECT * FROM candidates WHERE segment_id=? ORDER BY position,id', (segment_id,))
            return [self._candidate(row) for row in rows
                    if include_merged or not json.loads(row['data']).get('autoMergedInto')]

    @staticmethod
    def _auto_protected(row, data):
        return bool(row['message_id'] or data.get('reviewed') or data.get('ignored')
                    or 'edited-unconfirmed' in data.get('issues', []) or data.get('duplicateDecision'))

    def _auto_evidence(self, batch_id, material_id, values):
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise ValueError('invalid auto review evidence')
        result = []
        for segment_id in values:
            segment_id = _text(segment_id, 'evidence segment', 100, True)
            segment = self._one('segments', segment_id)
            if segment['batch_id'] != batch_id or segment['material_id'] != material_id:
                raise ValueError('evidence must belong to the same batch and material')
            if segment_id not in result:
                result.append(segment_id)
        return result

    def apply_auto_links(self, batch_id, segment_id, links):
        """Persist links proven by the import plugin's image overlap tracker.

        Text equality is deliberately insufficient to create links here: callers
        supply the observation pair after visual alignment. Earlier observations
        remain the canonical positions and all source observations are retained.
        """
        if not isinstance(links, list) or len(links) > 1000:
            raise ValueError('invalid auto links')
        with self._write() as db:
            self._one('batches', batch_id)
            segment = self._one('segments', segment_id)
            if segment['batch_id'] != batch_id:
                raise ValueError('segment belongs to another batch')
            seen = set()
            for link in links:
                if not isinstance(link, dict) or set(link) - {'currentIndex', 'previousId', 'action', 'reason'}:
                    raise ValueError('invalid auto link')
                position = _number(link.get('currentIndex'), 'currentIndex', False)
                if not isinstance(position, int) or position in seen:
                    raise ValueError('invalid auto link position')
                seen.add(position)
                action = link.get('action')
                if action not in ('reuse', 'replace', 'conflict'):
                    raise ValueError('invalid auto link action')
                reason = _text(link.get('reason', 'visual-overlap'), 'auto link reason', 500, True)
                current = db.execute('SELECT * FROM candidates WHERE segment_id=? AND position=?', (segment_id, position)).fetchone()
                if current is None:
                    raise ValueError('auto link observation not found')
                previous_id = _text(link.get('previousId'), 'previousId', 100, True)
                previous = self._one('candidates', previous_id)
                previous_segment = self._one('segments', previous['segment_id'])
                if (previous['batch_id'] != batch_id or previous_segment['material_id'] != segment['material_id']
                        or previous_segment['position'] >= segment['position']):
                    raise ValueError('auto link must reference an earlier observation of the same material')
                # Follow existing links defensively; malformed persisted cycles
                # must never let a plugin attach evidence across conversations.
                trail = {current['id']}
                canonical = previous
                while True:
                    if canonical['id'] in trail:
                        raise ValueError('cyclic auto link')
                    trail.add(canonical['id'])
                    data = json.loads(canonical['data'])
                    next_id = data.get('autoMergedInto')
                    if not next_id:
                        break
                    canonical = self._one('candidates', next_id)
                    target_segment = self._one('segments', canonical['segment_id'])
                    if canonical['batch_id'] != batch_id or target_segment['material_id'] != segment['material_id']:
                        raise ValueError('auto link target belongs to another material')
                current_data = json.loads(current['data'])
                if current_data.get('autoMergedInto'):
                    if current_data['autoMergedInto'] != canonical['id']:
                        raise ValueError('observation is already linked elsewhere')
                    continue
                if self._auto_protected(current, current_data):
                    raise ValueError('cannot automatically merge a reviewed or saved observation')
                canonical_protected = self._auto_protected(canonical, data)
                if action == 'conflict':
                    if canonical_protected:
                        raise ValueError('cannot automatically dispute a reviewed or saved observation')
                    evidence = self._auto_evidence(batch_id, segment['material_id'], list(dict.fromkeys([
                        canonical['segment_id'], segment_id, *data.get('evidenceSegmentIds', []),
                        *current_data.get('evidenceSegmentIds', [])])))
                    for candidate_row, updated, other_id in ((canonical, data, current['id']),
                                                              (current, current_data, canonical['id'])):
                        references = updated.get('autoConflictWith', [])
                        if other_id in references:
                            continue
                        updated['autoConflictWith'] = list(dict.fromkeys([*references, other_id]))
                        if 'recognition-difference' not in updated['issues']:
                            updated['autoConflictIssue'] = True
                            updated['issues'].append('recognition-difference')
                        updated['evidenceSegmentIds'] = evidence
                        version = candidate_row['version'] + 1
                        db.execute('UPDATE candidates SET data=?,version=? WHERE id=?', (_dump(updated), version, candidate_row['id']))
                        self._version('candidate', candidate_row['id'], version, updated, 'auto-overlap-conflict')
                    continue
                # A human correction changes the published interpretation, not
                # the source pixels. Protected reuse is checked against the
                # referenced frame's original observation, including aliases.
                reference = data
                if canonical_protected and action == 'reuse':
                    previous_data = json.loads(previous['data'])
                    reference = previous_data.get('original', previous_data)
                system = reference.get('kind') == current_data.get('kind') == 'system'
                if reference['sender'] != current_data['sender'] or (
                        reference['sender'] == 'unknown' and not system and not (canonical_protected and action == 'reuse')):
                    raise ValueError('auto link sender mismatch')
                if (reference.get('kind', 'text') != current_data.get('kind', 'text')
                        or _observation_text(reference.get('quote')) != _observation_text(current_data.get('quote'))):
                    raise ValueError('auto link kind or quote mismatch')
                reference_text, current_text = _observation_text(reference['text']), _observation_text(current_data['text'])
                if canonical_protected and action == 'reuse' and reference_text != current_text:
                    raise ValueError('protected reuse must match the original observation')
                partial = data.get('truncated') or any(i in data.get('issues', []) for i in
                    ('truncated', 'bubble-truncated', 'unclear_text', 'text-unclear')) or '□' in data['text'] or '\ufffd' in data['text']
                current_partial = current_data.get('truncated') or any(i in current_data.get('issues', []) for i in
                    ('truncated', 'bubble-truncated', 'unclear_text', 'text-unclear')) or '□' in current_data['text'] or '\ufffd' in current_data['text']
                if action == 'replace':
                    if canonical_protected or not partial or current_partial:
                        raise ValueError('only unreviewed partial text can be automatically completed')
                    if _blocking({**current_data, 'issues': [i for i in current_data['issues'] if i != 'possible_duplicate']}):
                        raise ValueError('replacement observation is still uncertain')
                    if any(i not in AUTO_REREAD_ISSUES and i not in TIME_ISSUES for i in data['issues']):
                        raise ValueError('replacement cannot resolve conversation or identity uncertainty')
                    fields = ('sender', 'text', 'bbox', 'truncated', 'kind', 'quote')
                    data.update({field: current_data[field] for field in fields})
                    if current_data.get('dateLabel') or current_data.get('time') is not None:
                        data.update({field: current_data[field] for field in ('time', 'dateLabel', 'timePrecision')})
                    data['issues'] = [i for i in current_data['issues'] if i != 'possible_duplicate']
                    data['preferredEvidenceSegmentId'] = segment_id
                    data['autoReviewed'] = True
                    data['reviewed'] = False
                elif reference_text != current_text and not (
                        current_partial and current_text and current_text.rstrip('….') in reference_text):
                    raise ValueError('different complete observations cannot be automatically merged')
                evidence = list(dict.fromkeys([canonical['segment_id'], *data.get('evidenceSegmentIds', []),
                                               previous['segment_id'], segment_id]))
                data['evidenceSegmentIds'] = self._auto_evidence(batch_id, segment['material_id'], evidence)
                data.setdefault('preferredEvidenceSegmentId', canonical['segment_id'])
                for updated, other_id in ((data, current['id']), (current_data, canonical['id'])):
                    if updated is data and canonical_protected:
                        continue
                    if other_id in updated.get('autoConflictWith', []):
                        updated['autoConflictWith'] = [item for item in updated['autoConflictWith'] if item != other_id]
                        if not updated['autoConflictWith'] and updated.pop('autoConflictIssue', False):
                            if not updated.get('recognitionSuggestion') and not updated.get('savedVersion'):
                                updated['issues'] = [i for i in updated['issues'] if i != 'recognition-difference']
                        if not updated['autoConflictWith']:
                            updated.pop('autoConflictWith')
                current_data['autoMergedInto'] = canonical['id']
                current_data['autoMergeReason'] = reason
                current_data['evidenceSegmentIds'] = [segment_id]
                for row, updated, version_reason in ((canonical, data, 'auto-overlap-' + action),
                                                     (current, current_data, 'auto-overlap-link')):
                    version = row['version'] + 1
                    db.execute('UPDATE candidates SET data=?,version=? WHERE id=?', (_dump(updated), version, row['id']))
                    self._version('candidate', row['id'], version, updated, version_reason)
            self._review_state(batch_id)
            return self.get_batch(batch_id)

    def apply_auto_review(self, batch_id, candidate_id, result):
        """Apply one bounded visual reread without claiming a human review."""
        allowed = {'resolved', 'message', 'evidenceSegmentIds', 'preferredEvidenceSegmentId'}
        if not isinstance(result, dict) or set(result) - allowed or not isinstance(result.get('resolved'), bool):
            raise ValueError('invalid auto review result')
        with self._write() as db:
            self._one('batches', batch_id)
            row = self._one('candidates', candidate_id)
            if row['batch_id'] != batch_id:
                raise ValueError('candidate belongs to another batch')
            data = json.loads(row['data'])
            if self._auto_protected(row, data) or data.get('autoMergedInto'):
                raise ValueError('cannot automatically review a reviewed, edited, linked or saved observation')
            segment = self._one('segments', row['segment_id'])
            # An overlap replacement can already prefer a different source
            # frame. An unresolved reread without a new proposal must keep
            # that stored provenance, including its preferred frame.
            evidence = result.get('evidenceSegmentIds', list(dict.fromkeys(
                [row['segment_id'], *data.get('evidenceSegmentIds', [])])))
            evidence = self._auto_evidence(batch_id, segment['material_id'], evidence)
            preferred = result.get('preferredEvidenceSegmentId', data.get('preferredEvidenceSegmentId', row['segment_id']))
            if preferred not in evidence:
                raise ValueError('preferred evidence must be included')
            if result['resolved']:
                message = result.get('message')
                fields = {'sender', 'text', 'time', 'dateLabel', 'timePrecision', 'issues', 'bbox', 'truncated', 'kind', 'quote'}
                if not isinstance(message, dict) or set(message) - fields or not {'sender', 'text', 'issues'} <= set(message):
                    raise ValueError('invalid auto review message')
                # Cross-batch identity, chronology, and conversation doubts cannot
                # be cleared by rereading one bubble. They keep the review gate.
                preserved = [i for i in data['issues'] if i not in AUTO_REREAD_ISSUES and i not in TIME_ISSUES]
                if (data.get('duplicateMessageId') or data.get('duplicateCandidateId')) and 'recognition-difference' in data['issues']:
                    preserved.append('recognition-difference')
                normalized = self._normalize_candidate({**data, **message})
                normalized['issues'] = list(dict.fromkeys(normalized['issues'] + preserved))
                if _blocking(normalized):
                    raise ValueError('auto review cannot declare an uncertain observation clear')
                conflicts = []
                for other_id in data.get('autoConflictWith', []):
                    other = self._one('candidates', other_id)
                    other_data = json.loads(other['data'])
                    if (other_data.get('sender'), other_data.get('text')) != (normalized['sender'], normalized['text']):
                        conflicts.append(other_id)
                if conflicts and 'recognition-difference' not in normalized['issues']:
                    normalized['issues'].append('recognition-difference')
                data.update({field: normalized[field] for field in fields})
                data['reviewed'] = False
                data['autoReviewed'] = True
                data['preferredEvidenceSegmentId'] = preferred
                data.pop('recognitionSuggestion', None)
            data['autoReviewAttempted'] = True
            data['evidenceSegmentIds'] = list(dict.fromkeys([row['segment_id'], *data.get('evidenceSegmentIds', []), *evidence]))
            version = row['version'] + 1
            db.execute('UPDATE candidates SET data=?,version=? WHERE id=?', (_dump(data), version, candidate_id))
            self._version('candidate', candidate_id, version, data, 'auto-review-resolved' if result['resolved'] else 'auto-review-unresolved')
            self._review_state(batch_id)
            return self._candidate(self._one('candidates', candidate_id))

    def submit_segment(self, batch_id, segment_id, candidates, *, evidence_distinct=False):
        if not isinstance(candidates, list) or len(candidates) > 1000:
            raise ValueError('invalid candidates')
        normalized = [self._normalize_candidate(c) for c in candidates]
        positions = [int(_number(c.get('order', index), 'order', False)) for index, c in enumerate(candidates)]
        if len(set(positions)) != len(positions):
            raise ValueError('candidate order must be unique within a segment')
        with self._write() as db:
            batch, segment = self._one('batches', batch_id), self._one('segments', segment_id)
            if segment['batch_id'] != batch_id:
                raise ValueError('segment belongs to another batch')
            material = self._one('materials', segment['material_id'])
            preceding = db.execute('SELECT id FROM segments WHERE batch_id=? AND material_id=? AND position<? ORDER BY position DESC,id DESC LIMIT 1',
                                   (batch_id, segment['material_id'], segment['position'])).fetchone()
            distinct_segments = {segment_id}
            if preceding:
                distinct_segments.add(preceding['id'])
            for ordinal, (incoming, data) in enumerate(zip(candidates, normalized)):
                position = int(_number(incoming.get('order', ordinal), 'order', False))
                # Exact same bytes, same frame/crop and bubble location are evidence of
                # identity. Text alone is never an identity, within or across batches.
                locator = data['bbox'] if data['bbox'] is not None else {'ordinal': position, 'text': data['text'], 'sender': data['sender']}
                identity = hashlib.sha256(_dump([material['sha256'], segment['segment_key'], locator]).encode()).hexdigest()
                previous = db.execute('SELECT * FROM candidates WHERE segment_id=? AND position=?', (segment_id, position)).fetchone()
                data['original'] = dict(data)
                if previous:
                    old = json.loads(previous['data'])
                    observed = {k: data[k] for k in ('sender', 'text', 'time', 'dateLabel', 'timePrecision', 'issues', 'bbox', 'truncated', 'kind', 'quote')}
                    old_observed = {k: old.get('original', old).get(k) for k in observed}
                    if observed == old_observed:
                        continue
                    # Recognition retries must never erase review edits or saved text.
                    if (self._auto_protected(previous, old) or old.get('autoReviewed') or old.get('autoMergedInto')
                            or old.get('evidenceSegmentIds')):
                        old['recognitionSuggestion'] = observed
                        old['issues'] = list(dict.fromkeys(old['issues'] + ['recognition-difference']))
                        data = old
                        identity = previous['identity_key']
                    elif old.get('orderKey'):
                        # A fresh reading of the bubble keeps the place the user moved it to.
                        data['orderKey'] = old['orderKey']
                    candidate_id, version = previous['id'], previous['version'] + 1
                    db.execute('UPDATE candidates SET data=?,version=?,identity_key=? WHERE id=?', (_dump(data), version, identity, candidate_id))
                else:
                    existing = db.execute('SELECT * FROM messages WHERE source_id=? AND identity_key=? ORDER BY rowid LIMIT 1',
                                          (batch['source_id'], identity)).fetchone()
                    if existing:
                        saved = json.loads(existing['data'])
                        data['duplicateMessageId'] = existing['id']
                        if (saved['text'], saved['sender']) != (data['text'], data['sender']):
                            data['issues'] = list(dict.fromkeys(data['issues'] + ['recognition-difference']))
                            data['savedVersion'] = saved
                    elif data['text'].strip() and data['sender'] != 'unknown':
                        # Equal content is only a reason to ask; it never deletes a
                        # message. Repeated actual utterances can be kept explicitly.
                        possible = db.execute('SELECT c.*,s.material_id FROM candidates c JOIN batches b ON b.id=c.batch_id JOIN segments s ON s.id=c.segment_id WHERE b.source_id=? AND c.identity_key<>? ORDER BY c.rowid DESC',
                                              (batch['source_id'], identity))
                        for match in possible:
                            match_data = json.loads(match['data'])
                            # Alignment proves separation only in this frame and
                            # its immediate neighbor, never across an unseen gap.
                            if (evidence_distinct and match['batch_id'] == batch_id
                                    and match['material_id'] == segment['material_id']
                                    and match['segment_id'] in distinct_segments):
                                continue
                            if not match_data.get('ignored') and not match_data.get('autoMergedInto') and (match_data.get('text'), match_data.get('sender')) == (data['text'], data['sender']):
                                data['duplicateCandidateId'] = match['id']
                                if match['message_id']:
                                    data['duplicateMessageId'] = match['message_id']
                                data['issues'] = list(dict.fromkeys(data['issues'] + ['possible_duplicate']))
                                break
                    candidate_id, version = _id('candidate'), 1
                    db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,NULL)',
                               (candidate_id, batch_id, segment_id, position, identity, _dump(data), version))
                self._version('candidate', candidate_id, version, data, 'recognition')
            for previous in list(db.execute('SELECT * FROM candidates WHERE segment_id=?', (segment_id,))):
                if previous['position'] in positions:
                    continue
                data = json.loads(previous['data'])
                data['recognitionSuggestion'] = {'missing': True}
                data['issues'] = list(dict.fromkeys(data['issues'] + ['recognition-difference']))
                db.execute('UPDATE candidates SET data=?,version=version+1 WHERE id=?', (_dump(data), previous['id']))
                self._version('candidate', previous['id'], previous['version'] + 1, data, 'recognition-missing')
            db.execute("UPDATE segments SET state='done',error=NULL WHERE id=?", (segment_id,))
            db.execute('UPDATE batches SET updated_at=? WHERE id=?', (_now(), batch_id))
            return self.get_batch(batch_id)

    def update_candidate(self, batch_id, candidate_id, patch, resolve=False):
        allowed = {'sender', 'text', 'time', 'dateLabel', 'timePrecision', 'issues', 'truncated', 'ignored', 'duplicateDecision', 'duplicateOf'}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError('unsupported candidate field')
        with self._write() as db:
            row = self._one('candidates', candidate_id)
            if row['batch_id'] != batch_id:
                raise ValueError('candidate belongs to another batch')
            old = json.loads(row['data'])
            if row['message_id']:
                if not resolve or patch.get('duplicateDecision') != 'reuse':
                    raise ValueError('这条记录已经保存；请在聊天记录中纠错，或选择保留已保存版本。')
                # Dismissing a recognition difference explicitly preserves the
                # existing saved record and its correction history.
                old['issues'] = [i for i in old['issues'] if i in TIME_ISSUES]
                old.pop('recognitionSuggestion', None)
                db.execute('UPDATE candidates SET data=?,version=version+1 WHERE id=?', (_dump(old), candidate_id))
                self._version('candidate', candidate_id, row['version'] + 1, old, 'retain-saved-version')
                self._review_state(batch_id)
                return self._candidate(self._one('candidates', candidate_id))
            data = {**old, **patch}
            validated = self._normalize_candidate(data)
            data.update({k: validated[k] for k in ('sender', 'text', 'time', 'dateLabel', 'timePrecision', 'issues', 'bbox', 'truncated')})
            data['ignored'] = bool(data.get('ignored'))
            decision = data.get('duplicateDecision')
            if decision not in (None, 'reuse', 'new'):
                raise ValueError('invalid duplicateDecision')
            duplicate_of = data.get('duplicateOf')
            if duplicate_of:
                duplicate_of = _text(duplicate_of, 'duplicateOf', 100, True)
                batch = self._one('batches', batch_id)
                target_message = db.execute('SELECT * FROM messages WHERE id=? AND source_id=?', (duplicate_of, batch['source_id'])).fetchone()
                target_candidate = db.execute('SELECT c.* FROM candidates c JOIN batches b ON b.id=c.batch_id WHERE c.id=? AND b.source_id=?', (duplicate_of, batch['source_id'])).fetchone()
                if target_message:
                    data['duplicateMessageId'] = duplicate_of
                elif target_candidate and duplicate_of != candidate_id:
                    data['duplicateCandidateId'] = duplicate_of
                else:
                    raise ValueError('duplicate target must belong to the same source')
            if resolve:
                if ((data['sender'] == 'unknown' and data.get('kind') != 'system')
                        or not data['text'].strip() or '\u25a1' in data['text'] or '\ufffd' in data['text']):
                    raise ValueError('请先确认发言人并补全可辨认的正文。')
                # An explicit confirmation accepts a cut-off bubble's visible text as it is.
                if (any(i in old['issues'] for i in ('recognition-difference', 'possible_duplicate', 'possible-duplicate', 'overlap'))
                        and (data.get('duplicateMessageId') or data.get('duplicateCandidateId')) and decision is None):
                    raise ValueError('请明确选择复用已有记录，或作为另一条真实发言保留。')
                data['issues'] = [issue for issue in data['issues'] if issue in TIME_ISSUES]
                data['truncated'] = False
                data['reviewed'] = True
            elif any(k in patch for k in ('text', 'sender', 'time', 'dateLabel', 'issues', 'truncated', 'duplicateDecision', 'duplicateOf')):
                data['reviewed'] = False
                data['issues'] = list(dict.fromkeys(old.get('issues', []) + data['issues'] + ['edited-unconfirmed']))
                data['truncated'] = old.get('truncated', False) or data['truncated']
            version = row['version'] + 1
            db.execute('UPDATE candidates SET data=?,version=? WHERE id=?', (_dump(data), version, candidate_id))
            self._version('candidate', candidate_id, version, data, 'user-review')
            self._review_state(batch_id)
            return self._candidate(self._one('candidates', candidate_id))

    def restore_candidate(self, batch_id, candidate_id, version, expected):
        """Undo review: return an unsaved candidate to a recorded earlier version.

        ``expected`` is the version the undo was taken against; any later change,
        by the user or by recognition, makes the undo stale instead of clobbering it.
        """
        for value in (version, expected):
            if type(value) is not int or value < 1:
                raise ValueError('invalid candidate version')
        with self._write() as db:
            row = self._one('candidates', candidate_id)
            if row['batch_id'] != batch_id:
                raise ValueError('candidate belongs to another batch')
            if row['message_id']:
                raise ValueError('这条记录已经保存，不能撤销；请在聊天记录中更正。')
            if row['version'] != expected or version >= expected:
                raise ValueError('这条记录之后又有改动，不能撤销。')
            previous = db.execute("SELECT data FROM versions WHERE kind='candidate' AND target_id=? AND version=? ORDER BY id DESC LIMIT 1",
                                  (candidate_id, version)).fetchone()
            if previous is None:
                raise ValueError('找不到可以恢复的版本。')
            data, version = json.loads(previous['data']), row['version'] + 1
            db.execute('UPDATE candidates SET data=?,version=? WHERE id=?', (_dump(data), version, candidate_id))
            self._version('candidate', candidate_id, version, data, 'user-undo')
            self._review_state(batch_id)
            return self._candidate(self._one('candidates', candidate_id))

    def move_candidate(self, batch_id, candidate_id, after_id=None):
        """Place an unsaved bubble right after another one of its batch (None: first)."""
        if after_id is not None:
            after_id = _text(after_id, 'after', 100, True)
        with self._write() as db:
            row = self._one('candidates', candidate_id)
            if row['batch_id'] != batch_id:
                raise ValueError('candidate belongs to another batch')
            if row['message_id']:
                raise ValueError('这条记录已经保存，不能再调整位置；请在聊天记录中更正。')
            others = sorted((_order_key(r['segment_position'], r['position'], json.loads(r['data'])), r['id'])
                            for r in db.execute('SELECT c.*,s.position AS segment_position FROM candidates c JOIN segments s ON s.id=c.segment_id WHERE c.batch_id=? AND c.id<>?',
                                                (batch_id, candidate_id)))
            index = 0
            if after_id is not None:
                index = next((i + 1 for i, (_, item) in enumerate(others) if item == after_id), None)
                if index is None:
                    raise ValueError('target belongs to another batch')
            low = others[index - 1][0] if index else ''
            high = others[index][0] if index < len(others) else None
            if high is not None and low >= high:
                raise ValueError('这两条消息之间无法再插入，请换一个位置。')
            data = json.loads(row['data'])
            data['orderKey'] = _order_between(low, high)
            version = row['version'] + 1
            db.execute('UPDATE candidates SET data=?,version=? WHERE id=?', (_dump(data), version, candidate_id))
            self._version('candidate', candidate_id, version, data, 'user-move')
            return self._candidate(self._one('candidates', candidate_id))

    def _counts(self, candidates):
        candidates = [c for c in candidates if not c.get('autoMergedInto')]
        return {'total': len(candidates), 'saved': sum(bool(c['savedMessageId']) for c in candidates),
                'ready': sum(c['ready'] and not c['savedMessageId'] for c in candidates),
                'pending': sum(not c['ready'] and not c['ignored'] and (not c['savedMessageId'] or bool(c.get('recognitionSuggestion'))) for c in candidates),
                'ignored': sum(c['ignored'] for c in candidates)}

    def _review_state(self, batch_id):
        batch = self._one('batches', batch_id)
        if batch['state'] in ACTIVE:
            return
        candidates = [self._candidate(r) for r in self.connection.execute('SELECT * FROM candidates WHERE batch_id=?', (batch_id,))]
        counts = self._counts(candidates)
        remaining = counts['ready'] + counts['pending']
        state = 'partial' if counts['saved'] and remaining else 'saved' if counts['saved'] else 'review'
        self.connection.execute('UPDATE batches SET state=?,updated_at=? WHERE id=?', (state, _now(), batch_id))

    def save_batch(self, batch_id, candidate_ids=None):
        with self._write() as db:
            batch = self._one('batches', batch_id)
            if batch['state'] in ACTIVE:
                raise ValueError('请等待识别完成或先停止任务，再确认保存。')
            rows = _in_order(db.execute('SELECT c.*,s.position AS segment_position FROM candidates c JOIN segments s ON s.id=c.segment_id WHERE c.batch_id=?', (batch_id,)))
            if candidate_ids is not None:
                if not isinstance(candidate_ids, list) or set(candidate_ids) - {r['id'] for r in rows}:
                    raise ValueError('invalid candidate selection')
                selected = set(candidate_ids)
            else:
                selected = {r['id'] for r in rows}
            added = reused = 0
            for row in rows:
                data = json.loads(row['data'])
                if row['id'] not in selected or row['message_id'] or data.get('ignored') or data.get('autoMergedInto') or _blocking(data):
                    continue
                existing = None
                if data.get('duplicateDecision') == 'reuse' and (data.get('duplicateMessageId') or data.get('duplicateCandidateId')):
                    target_id = data.get('duplicateMessageId')
                    if target_id:
                        existing = db.execute('SELECT * FROM messages WHERE source_id=? AND id=?', (batch['source_id'], target_id)).fetchone()
                    if not existing and data.get('duplicateCandidateId'):
                        target = self._one('candidates', data['duplicateCandidateId'])
                        target_id = target['message_id']
                        if target_id:
                            existing = db.execute('SELECT * FROM messages WHERE source_id=? AND id=?', (batch['source_id'], target_id)).fetchone()
                    if not existing:
                        # A duplicate cannot be published until its chosen original
                        # was itself confirmed. It remains an explicit gap.
                        continue
                elif data.get('duplicateDecision') != 'new':
                    existing = db.execute('SELECT * FROM messages WHERE source_id=? AND identity_key=? ORDER BY rowid LIMIT 1', (batch['source_id'], row['identity_key'])).fetchone()
                if existing:
                    saved = json.loads(existing['data'])
                    if (saved['sender'], saved['text']) != (data['sender'], data['text']) and data.get('duplicateDecision') != 'reuse':
                        data['issues'] = list(dict.fromkeys(data['issues'] + ['recognition-difference']))
                        data['duplicateMessageId'], data['savedVersion'] = existing['id'], saved
                        db.execute('UPDATE candidates SET data=?,version=version+1 WHERE id=?', (_dump(data), row['id']))
                        self._version('candidate', row['id'], row['version'] + 1, data, 'saved-version-difference')
                        continue
                    message_id = existing['id']
                    reused += 1
                else:
                    message_id = _id('message')
                    record = {k: data[k] for k in ('sender', 'text', 'time', 'dateLabel', 'timePrecision', 'reviewed', 'kind', 'quote')}
                    if data.get('autoReviewed'):
                        record['autoReviewed'] = True
                    record['corrected'] = False
                    media = self._picture(row)[0]
                    if media:
                        record['media'] = media
                    sort_key = '%012d:%s' % (batch['ordinal'], _order_key(row['segment_position'], row['position'], data))
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,1)', (message_id, batch['source_id'], batch_id, row['identity_key'], sort_key, _dump(record)))
                    self._version('message', message_id, 1, record, 'batch-confirmation')
                    added += 1
                db.execute('INSERT INTO batch_messages VALUES(?,?,?)', (batch_id, row['id'], message_id))
                db.execute('UPDATE candidates SET message_id=? WHERE id=?', (message_id, row['id']))
            self._review_state(batch_id)
            if added or reused:
                self._bump()
            view = self.get_batch(batch_id)
            return {'added': added, 'reused': reused, 'saved': view['counts']['saved'], 'pending': view['counts']['pending'], 'batch': view}

    def set_batch_state(self, batch_id, state, error=None):
        if state not in STATES:
            raise ValueError('invalid batch state')
        with self._write() as db:
            self._one('batches', batch_id)
            db.execute('UPDATE batches SET state=?,error=?,updated_at=? WHERE id=?', (state, None if error is None else str(error)[:2000], _now(), batch_id))
            if state in ('review', 'saved', 'partial'):
                self._review_state(batch_id)
            return self.get_batch(batch_id)

    def segment_state(self, segment_id, state, error=None):
        if state not in ('pending', 'running', 'done', 'failed', 'stopped', 'interrupted'):
            raise ValueError('invalid segment state')
        with self._write() as db:
            self._one('segments', segment_id)
            db.execute('UPDATE segments SET state=?,error=? WHERE id=?', (state, None if error is None else str(error)[:2000], segment_id))
            return self._segment(self._one('segments', segment_id))

    def mark_interrupted(self):
        with self._write() as db:
            db.execute("UPDATE segments SET state='interrupted' WHERE state='running'")
            return db.execute("UPDATE batches SET state='interrupted',updated_at=? WHERE state IN ('queued','running','stopping')", (_now(),)).rowcount

    def set_direction(self, batch_id, direction):
        if direction not in ('forward', 'reverse'):
            raise ValueError('invalid direction')
        with self._write() as db:
            batch = self.get_batch(batch_id)
            if batch['state'] in ACTIVE or batch['segments']:
                raise ValueError('只能在首次识别前调整录屏滚动方向。')
            db.execute('UPDATE batches SET direction=?,updated_at=? WHERE id=?', (direction, _now(), batch_id))
            return self.get_batch(batch_id)

    def reset_recognition(self, batch_id):
        with self._write() as db:
            batch = self._one('batches', batch_id)
            if batch['state'] in ACTIVE:
                raise ValueError('请先停止当前任务，再重新识别。')
            if any(not s['evidenceAvailable'] for s in self.get_batch(batch_id)['segments']):
                raise ValueError('部分证据画面已清理，无法重新识别；请重新添加素材。')
            db.execute("UPDATE segments SET state='pending',error=NULL WHERE batch_id=?", (batch_id,))
            db.execute("UPDATE batches SET state='draft',error=NULL,updated_at=? WHERE id=?", (_now(), batch_id))
            return self.get_batch(batch_id)

    def get_batch(self, batch_id):
        with self.lock:
            row = self._one('batches', batch_id)
            source = self._one('sources', row['source_id'])
            materials = []
            for material in self.connection.execute('SELECT m.*,bm.position FROM materials m JOIN batch_materials bm ON bm.material_id=m.id WHERE bm.batch_id=? ORDER BY bm.position', (batch_id,)):
                materials.append({'id': material['id'], 'name': material['name'], 'kind': material['kind'], 'mime': material['mime'],
                                  'size': material['size'], 'sha256': material['sha256'], 'order': material['position'], 'available': bool(self.material_path(material['id']))})
            segments = [self._segment(r) for r in self.connection.execute('SELECT * FROM segments WHERE batch_id=? ORDER BY position,id', (batch_id,))]
            candidates = [self._candidate(r) for r in _in_order(self.connection.execute('SELECT c.*,s.position AS segment_position FROM candidates c JOIN segments s ON s.id=c.segment_id WHERE c.batch_id=?', (batch_id,)))]
            candidates = [c for c in candidates if not c.get('autoMergedInto')]
            return {'id': row['id'], 'friendId': source['friend_id'], 'sourceId': row['source_id'], 'source': self._source(source),
                    'friend': self._friend(self._one('friends', source['friend_id'])), 'note': row['note'], 'state': row['state'], 'error': row['error'],
                    'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'archived': bool(row['archived']), 'direction': row['direction'],
                    'progress': {'done': sum(s['state'] == 'done' for s in segments), 'total': len(segments), 'label': row['state']},
                    'warnings': list(dict.fromkeys(w for s in segments for w in s['warnings'])),
                    'materials': materials, 'segments': segments, 'candidates': candidates, 'counts': self._counts(candidates)}

    def list_batches(self, friend_id=None):
        with self.lock:
            sql = 'SELECT b.id FROM batches b JOIN sources s ON s.id=b.source_id WHERE b.archived=0'
            params = []
            if friend_id is not None:
                sql += ' AND s.friend_id=?'
                params.append(friend_id)
            return [self.get_batch(r[0]) for r in self.connection.execute(sql + ' ORDER BY b.ordinal DESC', params)]

    def _message(self, row):
        source = self._one('sources', row['source_id'])
        evidence = []
        batch_ids = []
        seen_evidence = set()
        for ref in self.connection.execute('SELECT bm.batch_id,c.id,c.segment_id,c.data FROM batch_messages bm JOIN candidates c ON c.id=bm.candidate_id WHERE bm.message_id=? ORDER BY bm.rowid', (row['id'],)):
            candidate = json.loads(ref['data'])
            batch_ids.append(ref['batch_id'])
            preferred = candidate.get('preferredEvidenceSegmentId', ref['segment_id'])
            segment_ids = list(dict.fromkeys([preferred, ref['segment_id'], *candidate.get('evidenceSegmentIds', [])]))
            for segment_id in segment_ids:
                # Explicitly retained observations provide the box as it appeared
                # in each frame, rather than reusing a corrected box everywhere.
                observed = candidate.get('original', candidate) if segment_id == ref['segment_id'] else None
                if segment_id == preferred:
                    observed = candidate
                if observed is None:
                    for sibling in self.connection.execute('SELECT data FROM candidates WHERE segment_id=? AND batch_id=?', (segment_id, ref['batch_id'])):
                        sibling_data = json.loads(sibling['data'])
                        if sibling_data.get('autoMergedInto') == ref['id']:
                            observed = sibling_data.get('original', sibling_data)
                            break
                bbox = observed.get('bbox') if observed else None
                evidence_key = (segment_id, _dump(bbox))
                if evidence_key in seen_evidence:
                    continue
                seen_evidence.add(evidence_key)
                segment = self._one('segments', segment_id)
                evidence.append({'segmentId': segment['id'], 'materialId': segment['material_id'], 'videoMs': segment['video_ms'],
                                 'bbox': bbox, 'preferred': segment_id == preferred,
                                 'evidenceAvailable': bool(self.evidence_path(segment['id'])), 'originalAvailable': bool(self.material_path(segment['material_id']))})
        return {**json.loads(row['data']), 'id': row['id'], 'friendId': source['friend_id'], 'sourceId': source['id'],
                'platform': source['platform'], 'order': row['sort_key'], 'version': row['version'],
                'batchIds': list(dict.fromkeys(batch_ids)), 'evidence': evidence}

    def _timeline(self, source_id):
        """Merge observed batch sequences at shared record identities.

        No timestamp is invented. Disconnected evidence keeps import order; a
        contradictory sequence remains visibly uncertain instead of picking one
        claimed chronology. The mapping also places still-unsaved gaps between
        their saved neighbors when partial confirmation happens later.
        """
        ranks, edges, incoming = {}, {}, {}
        for row in self.connection.execute('SELECT id,sort_key FROM messages WHERE source_id=?', (source_id,)):
            key = 'm:' + row['id']
            ranks[key] = row['sort_key']
            edges[key], incoming[key] = set(), 0
        batches = {}
        rows = self.connection.execute('SELECT c.*,b.ordinal,s.position AS segment_position FROM candidates c JOIN batches b ON b.id=c.batch_id JOIN segments s ON s.id=c.segment_id WHERE b.source_id=?', (source_id,))
        for row in sorted(rows, key=lambda r: (r['ordinal'], _order_key(r['segment_position'], r['position'], json.loads(r['data'])), r['id'])):
            candidate_data = json.loads(row['data'])
            if candidate_data.get('ignored') or candidate_data.get('autoMergedInto'):
                continue
            key = ('m:' + row['message_id']) if row['message_id'] else ('c:' + row['id'])
            if key not in ranks:
                ranks[key] = '%012d:%s' % (row['ordinal'], _order_key(row['segment_position'], row['position'], candidate_data))
                edges[key], incoming[key] = set(), 0
            sequence = batches.setdefault(row['batch_id'], [])
            if not sequence or sequence[-1] != key:
                sequence.append(key)
        undirected = {key: set() for key in ranks}
        for sequence in batches.values():
            for left, right in zip(sequence, sequence[1:]):
                if right not in edges[left]:
                    edges[left].add(right)
                    incoming[right] += 1
                    undirected[left].add(right)
                    undirected[right].add(left)
        queue = [(ranks[key], key) for key in ranks if incoming[key] == 0]
        heapq.heapify(queue)
        ordered = []
        while queue:
            _, key = heapq.heappop(queue)
            ordered.append(key)
            for successor in edges[key]:
                incoming[successor] -= 1
                if incoming[successor] == 0:
                    heapq.heappush(queue, (ranks[successor], successor))
        contradictory = {key for key in ranks if incoming[key] > 0}
        if contradictory:
            # Retain the established import order for this inconsistent source.
            # Callers expose the conflict instead of presenting it as chronology.
            ordered = sorted(ranks, key=lambda key: (ranks[key], key))
        remaining, components = set(ranks), 0
        while remaining:
            components += 1
            stack = [remaining.pop()]
            while stack:
                for neighbor in undirected[stack.pop()]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        stack.append(neighbor)
        return {key: index for index, key in enumerate(ordered)}, contradictory, components > 1

    def messages(self, friend_id, platform=None, batch_id=None, query=''):
        query = _text(query, 'query', 1000)
        if platform is not None:
            platform = _text(platform, 'platform', 80, True)
        with self.lock:
            self._one('friends', friend_id)
            source_ids = [s['id'] for s in self.list_sources(friend_id) if platform is None or s['platform'] == platform]
            rows, gaps, warnings = [], [], []
            unanchored = conflict = False
            for source_index, source_id in enumerate(source_ids):
                timeline, contradictory, disconnected = self._timeline(source_id)
                unanchored |= disconnected
                conflict |= bool(contradictory)
                for row in self.connection.execute('SELECT * FROM messages WHERE source_id=? ORDER BY sort_key,id', (source_id,)):
                    message = self._message(row)
                    key = 'm:' + row['id']
                    message['order'] = '%012d:%012d' % (source_index, timeline[key])
                    message['orderUncertain'] = key in contradictory
                    if (batch_id is None or batch_id in message['batchIds']) and (not query or query.casefold() in message['text'].casefold()):
                        rows.append(message)
                for row in self.connection.execute('SELECT c.*,b.ordinal,b.archived,s.position AS segment_position FROM candidates c JOIN batches b ON b.id=c.batch_id JOIN segments s ON s.id=c.segment_id WHERE b.source_id=? AND c.message_id IS NULL', (source_id,)):
                    data = json.loads(row['data'])
                    if not data.get('ignored') and not data.get('autoMergedInto') and (batch_id is None or row['batch_id'] == batch_id):
                        key = 'c:' + row['id']
                        gap_issues = _blocking(data) or ['not-saved']
                        if key in contradictory:
                            gap_issues.append('order-conflict')
                        gaps.append({'candidateId': row['id'], 'batchId': row['batch_id'], 'sourceId': source_id,
                                     'order': '%012d:%012d' % (source_index, timeline[key]),
                                     'issues': gap_issues, 'archived': bool(row['archived'])})
            if unanchored:
                warnings.append('部分批次缺少相互重叠的证据，按导入顺序分组展示；分组先后不代表确定的消息时间。')
            if conflict:
                warnings.append('不同批次提供的消息顺序存在冲突，暂按已有导入顺序展示；请对照来源核对，当前顺序不表示已确认的时间先后。')
            return {'messages': sorted(rows, key=lambda x: (x['order'], x['id'])), 'gaps': sorted(gaps, key=lambda x: x['order']),
                    'sources': self.list_sources(friend_id), 'revision': self.revision(), 'warnings': warnings,
                    'coverage': {'ordering': 'evidence-sequence', 'contradictoryOrder': conflict, 'unanchoredGroups': unanchored,
                                 'hasUnknownTimes': any(m['time'] is None for m in rows), 'includesArchivedGaps': any(g['archived'] for g in gaps)}}

    def correct_message(self, message_id, patch):
        if not isinstance(patch, dict) or set(patch) - {'sender', 'text', 'time', 'dateLabel', 'timePrecision'}:
            raise ValueError('unsupported correction field')
        with self._write() as db:
            row = self._one('messages', message_id)
            data = {**json.loads(row['data']), **patch}
            validated = self._normalize_candidate(data)
            if _blocking(validated):
                raise ValueError('保存的记录需要可辨认的正文和明确的发言人。')
            data.update({k: validated[k] for k in ('sender', 'text', 'time', 'dateLabel', 'timePrecision')})
            data['corrected'] = data['reviewed'] = True
            version = row['version'] + 1
            db.execute('UPDATE messages SET data=?,version=? WHERE id=?', (_dump(data), version, message_id))
            self._version('message', message_id, version, data, 'user-correction')
            self._bump()
            return self._message(self._one('messages', message_id))

    def reassociate_batch(self, batch_id, friend_id, platform, own_account, conversation_key):
        platform = _text(platform, 'platform', 80, True)
        with self._write() as db:
            batch = self._one('batches', batch_id)
            if batch['state'] in ACTIVE:
                raise ValueError('请先停止识别，再修改所属好友或来源会话。')
            old_source = self._one('sources', batch['source_id'])
            same_identity = (platform, own_account, conversation_key) == (old_source['platform'], old_source['own_account'], old_source['conversation_key'])
            if same_identity and old_source['friend_id'] != friend_id:
                self._one('friends', friend_id)
                if db.execute('SELECT COUNT(*) FROM batches WHERE source_id=?', (old_source['id'],)).fetchone()[0] > 1:
                    raise ValueError('这个来源还包含其他批次；请先为本批选择不同的会话标识，再调整关联。')
                db.execute('UPDATE sources SET friend_id=? WHERE id=?', (friend_id, old_source['id']))
                source_id = old_source['id']
            else:
                source_id = self._ensure_source(friend_id, platform, own_account, conversation_key)
            if source_id != batch['source_id']:
                replacements = {}
                for message_id in [r[0] for r in db.execute('SELECT DISTINCT message_id FROM batch_messages WHERE batch_id=?', (batch_id,))]:
                    message = self._one('messages', message_id)
                    others = db.execute('SELECT 1 FROM batch_messages WHERE message_id=? AND batch_id<>?', (message_id, batch_id)).fetchone()
                    if others:
                        replacement = _id('message')
                        replacements[message_id] = replacement
                        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (replacement, source_id, batch_id, message['identity_key'], message['sort_key'], message['data'], message['version']))
                        db.execute('INSERT INTO versions(kind,target_id,version,reason,data,created_at) SELECT kind,?,version,reason,data,created_at FROM versions WHERE kind=\'message\' AND target_id=?', (replacement, message_id))
                        db.execute('UPDATE batch_messages SET message_id=? WHERE batch_id=? AND message_id=?', (replacement, batch_id, message_id))
                        db.execute('UPDATE candidates SET message_id=? WHERE batch_id=? AND message_id=?', (replacement, batch_id, message_id))
                    else:
                        db.execute('UPDATE messages SET source_id=? WHERE id=?', (source_id, message_id))
                db.execute('UPDATE batches SET source_id=?,updated_at=? WHERE id=?', (source_id, _now(), batch_id))
                for candidate in list(db.execute('SELECT * FROM candidates WHERE batch_id=?', (batch_id,))):
                    data = json.loads(candidate['data'])
                    before = _dump(data)
                    if data.get('duplicateMessageId') in replacements:
                        data['duplicateMessageId'] = replacements[data['duplicateMessageId']]
                    if data.get('duplicateMessageId') and not db.execute('SELECT 1 FROM messages WHERE id=? AND source_id=?', (data['duplicateMessageId'], source_id)).fetchone():
                        data.pop('duplicateMessageId')
                    if data.get('duplicateCandidateId') and not db.execute('SELECT 1 FROM candidates c JOIN batches b ON b.id=c.batch_id WHERE c.id=? AND b.source_id=?', (data['duplicateCandidateId'], source_id)).fetchone():
                        data.pop('duplicateCandidateId')
                    if not data.get('duplicateMessageId') and not data.get('duplicateCandidateId'):
                        for field in ('duplicateDecision', 'duplicateOf', 'savedVersion'):
                            data.pop(field, None)
                        data['issues'] = [issue for issue in data['issues'] if issue != 'possible_duplicate' and (issue != 'recognition-difference' or data.get('recognitionSuggestion'))]
                    if before != _dump(data):
                        db.execute('UPDATE candidates SET data=?,version=version+1 WHERE id=?', (_dump(data), candidate['id']))
                        self._version('candidate', candidate['id'], candidate['version'] + 1, data, 'association-correction')
            self._bump()
            return self.get_batch(batch_id)

    def _undo(self, batch_id):
        db = self.connection
        refs = [r[0] for r in db.execute('SELECT DISTINCT message_id FROM batch_messages WHERE batch_id=?', (batch_id,))]
        db.execute('DELETE FROM batch_messages WHERE batch_id=?', (batch_id,))
        db.execute('UPDATE candidates SET message_id=NULL WHERE batch_id=?', (batch_id,))
        removed = shared = 0
        for message_id in refs:
            if db.execute('SELECT 1 FROM batch_messages WHERE message_id=?', (message_id,)).fetchone():
                shared += 1
            else:
                db.execute('DELETE FROM messages WHERE id=?', (message_id,))
                removed += 1
        self._review_state(batch_id)
        if refs:
            self._bump()
        return {'removed': removed, 'preservedShared': shared}

    def undo_batch(self, batch_id):
        with self._write():
            batch = self._one('batches', batch_id)
            if batch['state'] in ACTIVE:
                raise ValueError('请先停止识别，再撤销本批保存。')
            result = self._undo(batch_id)
            return {**result, 'batch': self.get_batch(batch_id)}

    def cleanup_preview(self, batch_id, kind):
        if kind not in ('originals', 'evidence', 'task', 'records'):
            raise ValueError('invalid cleanup kind')
        with self.lock:
            batch = self.get_batch(batch_id)
            blockers = []
            unfinished = batch['state'] in ACTIVE or batch['counts']['pending'] + batch['counts']['ready'] > 0 or any(s['state'] != 'done' for s in batch['segments']) or (bool(batch['materials']) and not batch['segments'])
            if kind in ('originals', 'evidence') and unfinished and not batch['archived']:
                blockers.append('unfinished-task')
            if kind in ('records', 'task') and batch['state'] in ACTIVE:
                blockers.append('active-task')
            files = []
            affected_batches = set()
            if kind == 'originals':
                for material in batch['materials']:
                    path = self.material_path(material['id'])
                    if path:
                        files.append(str(path.relative_to(self.root)))
                    others = self.connection.execute('SELECT batch_id FROM batch_materials WHERE material_id=? AND batch_id<>?', (material['id'], batch_id))
                    affected_batches.update(r[0] for r in others)
            elif kind == 'evidence':
                for segment in batch['segments']:
                    row = self._one('segments', segment['id'])
                    if row['path']:
                        files.append(row['path'])
                        affected_batches.update(r[0] for r in self.connection.execute('SELECT batch_id FROM segments WHERE path=? AND batch_id<>?', (row['path'], batch_id)))
            if affected_batches:
                blockers.append('shared-material')
            unique = list(dict.fromkeys(files))
            size = sum(self._managed(p).stat().st_size for p in unique if self._managed(p).is_file())
            return {'batchId': batch_id, 'kind': kind, 'allowed': not blockers, 'blockers': blockers,
                    'affectedBatchIds': sorted(affected_batches), 'savedRecords': batch['counts']['saved'],
                    'pendingCandidates': batch['counts']['pending'] + batch['counts']['ready'],
                    'fileCount': len(unique), 'bytes': size,
                    'effects': {'originals': '完整素材回放及重新抽取画面将不可用；保留的证据和聊天正文仍可查看。',
                                'evidence': '证据画面将不可查看；聊天正文和仍保留的原素材不受影响。',
                                'task': f"从任务列表移除；{batch['counts']['pending'] + batch['counts']['ready']} 条未保存候选将停止核对，聊天中的未保存缺口标记保留。正式记录及其来源证据继续保留。之后可单独清理素材。",
                                'records': '撤销本批保存关联；其他批次引用的记录保留。'}[kind]}

    def cleanup(self, batch_id, kind):
        # Keep the store lock through physical removal so a simultaneous re-upload
        # cannot restore a referenced path immediately before this call unlinks it.
        with self.lock:
            return self._cleanup_locked(batch_id, kind)

    def _cleanup_locked(self, batch_id, kind):
        with self._write() as db:
            preview = self.cleanup_preview(batch_id, kind)
            if not preview['allowed']:
                labels = {'unfinished-task': '还有未完成识别或核对的内容', 'shared-material': '其他批次仍依赖这些共享素材', 'active-task': '任务正在运行'}
                raise ValueError('暂时无法清理：' + '；'.join(labels[b] for b in preview['blockers']) + '。')
            paths = []
            if kind == 'originals':
                rows = list(db.execute('SELECT m.id,m.path FROM materials m JOIN batch_materials bm ON bm.material_id=m.id WHERE bm.batch_id=?', (batch_id,)))
                paths = [r['path'] for r in rows if r['path']]
                for row in rows:
                    db.execute('UPDATE materials SET path=NULL WHERE id=?', (row['id'],))
            elif kind == 'evidence':
                paths = [r[0] for r in db.execute('SELECT path FROM segments WHERE batch_id=? AND path IS NOT NULL', (batch_id,))]
                db.execute('UPDATE segments SET path=NULL WHERE batch_id=?', (batch_id,))
            elif kind == 'task':
                db.execute('UPDATE batches SET archived=1,updated_at=? WHERE id=?', (_now(), batch_id))
            elif kind == 'records':
                preview.update(self._undo(batch_id))
            # Metadata first; if a file is locked, report it and leave an unreferenced
            # local copy rather than falsely promising evidence remains available.
        failed = []
        for relative in set(paths):
            try:
                self._managed(relative).unlink(missing_ok=True)
            except OSError:
                failed.append(relative)
        return {**preview, 'completed': not failed, 'filesNotRemoved': failed}
