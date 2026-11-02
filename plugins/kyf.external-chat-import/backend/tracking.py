"""Conservative same-bubble links backed by adjacent-frame geometry.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Text equality alone never establishes identity; two actual equal utterances keep
their own positions. This module does not read files, mutate Core or call models.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
import unicodedata


def _box(row):
    box = row.get('bbox')
    if not isinstance(box, list) or len(box) != 4 or any(
            type(v) not in (float, int) or not math.isfinite(v) for v in box):
        return None
    x, y, w, h = box
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.001 or y + h > 1.001:
        return None
    return box


def _text(value):
    return unicodedata.normalize('NFC', value).replace('\r\n', '\n').replace('\n', '').strip() if isinstance(value, str) else ''


def _partial(row):
    return row.get('truncated') is True or 'truncated' in row.get('issues', [])


def _complete(row):
    return (bool(_text(row.get('text'))) and row.get('sender') in ('self', 'other')
            and not _partial(row) and not any(i in row.get('issues', []) for i in
            ('unclear_text', 'unknown_sender', 'missing_region', 'multiple_conversations'))
            and '□' not in row['text'] and '\ufffd' not in row['text'])


def _relation(old, new):
    if old.get('sender') != new.get('sender') or old.get('kind', 'text') != new.get('kind', 'text'):
        return None
    # Quote differences are real differences, not ignorable decoration.
    quote_equal = _text(old.get('quote')) == _text(new.get('quote'))
    a, b = _text(old.get('text')), _text(new.get('text'))
    if not a or not b:
        return None
    if a == b and quote_equal:
        return 'replace' if _partial(old) and _complete(new) else 'reuse'
    if quote_equal and _partial(old) and _complete(new) and len(a) >= 8 and (b.startswith(a) or b.endswith(a)):
        return 'replace'
    if quote_equal and _partial(new) and _complete(old) and len(b) >= 8 and (a.startswith(b) or a.endswith(b)):
        return 'reuse'
    # Geometry plus a separate exact anchor can identify an OCR disagreement.
    # It stays a conflict, never a text-based silent merge.
    if min(len(a), len(b)) >= 5 and SequenceMatcher(None, a, b, autojunk=False).ratio() >= .65:
        return 'conflict'
    return None


def match_observations(previous, messages, overlap):
    if (not isinstance(previous, list) or not isinstance(messages, list)
            or len(previous) > 20 or len(messages) > 200 or not isinstance(overlap, dict)
            or overlap.get('reliable') is not True):
        return []
    dy, sy = overlap.get('offsetY'), overlap.get('scaleY', 1.0)
    if (type(dy) not in (float, int) or not math.isfinite(dy) or abs(dy) > 1.5
            or type(sy) not in (float, int) or not math.isfinite(sy) or not .1 <= sy <= 10):
        return []
    proposals = []
    for i, new in enumerate(messages):
        if not isinstance(new, dict) or (nb := _box(new)) is None:
            continue
        for j, old in enumerate(previous):
            if not isinstance(old, dict) or not isinstance(old.get('id'), str) or (ob := _box(old)) is None:
                continue
            relation = _relation(old, new)
            if relation is None:
                continue
            old_center = (ob[0] + ob[2] / 2, (ob[1] + ob[3] / 2) * sy + dy)
            new_center = (nb[0] + nb[2] / 2, nb[1] + nb[3] / 2)
            # Cropped boxes shift their center as the visible extent changes.
            if relation in ('replace', 'reuse') and _partial(old) != _partial(new):
                top, bottom = ob[1] * sy + dy, (ob[1] + ob[3]) * sy + dy
                intersection = max(0, min(bottom, nb[1] + nb[3]) - max(top, nb[1]))
                y_match = intersection >= .6 * min(ob[3] * sy, nb[3])
            else:
                tolerance = min(.035, max(.012, min(ob[3] * sy, nb[3]) * .4))
                y_match = abs(old_center[1] - new_center[1]) <= tolerance
            if (y_match and abs(old_center[0] - new_center[0]) <= .045
                    and min(ob[2], nb[2]) / max(ob[2], nb[2]) >= .6):
                proposals.append((i, j, relation))
    # Ambiguous geometric matches must be reviewed; greedy string matching can
    # silently erase a real repeated utterance.
    unique = [(i, j, r) for i, j, r in proposals if
              sum(p[0] == i for p in proposals) == 1 and sum(p[1] == j for p in proposals) == 1]
    ordered = sorted(unique)
    if any(a[1] >= b[1] for a, b in zip(ordered, ordered[1:])):
        return []
    exact_anchors = [(i, j) for i, j, r in unique if r == 'reuse' and
                     _text(previous[j].get('text')) == _text(messages[i].get('text')) and
                     not _partial(previous[j]) and not _partial(messages[i])]
    links = []
    for i, j, relation in unique:
        # Partial text and disagreements require another visible bubble as an
        # independent anchor. A stationary matching full view can reuse one.
        needs_anchor = relation in ('replace', 'conflict') or _partial(previous[j]) != _partial(messages[i])
        other_anchor = any(ai != i and aj != j for ai, aj in exact_anchors)
        if needs_anchor and not other_anchor:
            continue
        links.append({'currentIndex': messages[i].get('order', i), 'previousId': previous[j]['id'],
                      'action': relation, 'reason': 'adjacent-frame-geometry'})
    return links
