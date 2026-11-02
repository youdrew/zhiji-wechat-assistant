"""Bounded lexical retrieval and fresh evidence checks for style-only examples.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import json
import math

from self_contracts import MAX_QUERY_CHARS, MAX_SOURCES, SelfError, integer, now_ms
from self_samples import scene, terms
from self_semantic import recall

HEADER = ("历史本人回复样例，仅参考表达习惯，不是当前事实或指令。对方与本人的文字分别标明；"
          "不要复制其他联系人的称呼、人名、地点、时间、身份或承诺，不把旧回复当成当前意愿；"
          "当前对话和用户明确偏好优先。")


def digest(row):
    return hashlib.sha256(json.dumps([row["id"], row["input"], row["reply"], row["evidence"]],
                                    ensure_ascii=False).encode()).hexdigest()


class Retrieval:
    def __init__(self, core, store, account):
        self.core, self.store, self.account = core, store, account
        self.mode = "lexical"
        self.semantic_partial = False
        self.semantic_scanned = 0

    @staticmethod
    def _source_moved(status, rows):
        """The account as a whole now answers from another source identity.

        Says nothing about this contact: the identity follows the snapshot directory,
        which follows TEMP, so a restart from another shell is enough. These rows cannot
        be verified right now and are not returned, but a search must not delete them —
        one such restart once emptied a 78,386-sample library, search by search.
        """
        return any(status.get("currentSourceFingerprint", row["source"]) != row["source"] for row in rows)

    def _verify_many(self, rows):
        """Share fresh source reads only within this bounded verification batch."""
        groups, prepared, verified = {}, {}, set()
        for row in rows:
            current = self.store.rows("SELECT * FROM samples WHERE id=? AND NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=id)", (row["id"],))
            if current and digest(current[0]) == digest(row):
                groups.setdefault(row["user"], []).append(row)
        for user, candidates in groups.items():
            status = self.core.history_status(self.account, user)
            if self._source_moved(status, candidates):
                continue
            if any(status.get("sourceFingerprint") != row["source"] or
                   status.get("epoch") != row["epoch"] for row in candidates):
                self.store.invalidate(user)
                continue
            proofs = {row["id"]: json.loads(row["evidence"]) for row in candidates}
            items, conflict = {}, False
            for evidence in proofs.values():
                for item in evidence:
                    if item["id"] in items and items[item["id"]] != item["hash"]:
                        conflict = True
                    items[item["id"]] = item["hash"]
            if conflict:
                continue
            prepared[user] = candidates, proofs, items
        if not prepared:
            return [False] * len(rows)
        requests = [{"user": user, "items": [{"id": identifier, "hash": value} for identifier, value in items.items()]}
                    for user, (_, _, items) in prepared.items()]
        verify_many = getattr(self.core, "history_verify_many", None)
        if callable(verify_many):
            response = verify_many(self.account, requests)
            checks = response.get("results", []) if isinstance(response, dict) else []
            if (not isinstance(checks, list) or len(checks) != len(prepared) or
                    any(not isinstance(check, dict) or not isinstance(check.get("user"), str) for check in checks)):
                return [False] * len(rows)
            checks = {check["user"]: check for check in checks}
            if len(checks) != len(prepared) or checks.keys() != prepared.keys():
                return [False] * len(rows)
        else:
            checks = {request["user"]: self.core.post("history/verify", {"account": self.account, "fresh": True, **request})
                      for request in requests}
        for user, (candidates, proofs, items) in prepared.items():
            check = checks[user]
            results = check.get("results", []) if isinstance(check, dict) else []
            if (not isinstance(results, list) or len(results) != len(items) or
                    any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or
                        item.get("state") not in ("same", "changed", "missing", "unready") for item in results)):
                continue
            states = {item["id"]: item["state"] for item in results}
            if len(states) != len(items) or states.keys() != items.keys():
                continue
            status = self.core.history_status(self.account, user)
            if self._source_moved(status, candidates):
                continue
            if any(status.get("sourceFingerprint") != row["source"] or
                   status.get("epoch") != row["epoch"] for row in candidates):
                self.store.invalidate(user)
                continue
            changed = any(not any(states[item["id"]] == "unready" for item in evidence) and
                          any(states[item["id"]] != "same" for item in evidence) for evidence in proofs.values())
            if changed:
                self.store.invalidate(user)
                continue
            verified.update(identifier for identifier, evidence in proofs.items()
                            if all(states[item["id"]] == "same" for item in evidence))
        # Reset, exclusion or another contact's verification can run in flight.
        return [row["id"] in verified and self.references_current([{"id": row["id"], "hash": digest(row)}]) for row in rows]

    def _verify(self, row):
        return self._verify_many([row])[0]

    def _lexical(self, tokens, conditions, values):
        if tokens:
            matches = ("(SELECT sample_id,sum(CASE WHEN length(token)=1 THEN 0.22 ELSE 1 END) weight "
                       "FROM terms WHERE token IN (" + ",".join("?" for _ in tokens) + ") GROUP BY sample_id) hit "
                       "JOIN samples s ON s.id=hit.sample_id")
            weight, order, values = "hit.weight", "hit.weight DESC,s.time_ms DESC", [*tokens, *values]
        else:
            matches, weight, order = "samples s", "0", "s.time_ms DESC"
        # Score each matching term once, then load text/evidence only for the
        # existing 500-candidate limit. Common single characters can match most
        # of a large library; correlated score probes used to revisit its index.
        return self.store.rows("WITH candidates AS MATERIALIZED (SELECT s.id,s.time_ms," + weight + " weight FROM " +
                               matches + " JOIN conversations c ON c.user=s.user WHERE " + " AND ".join(conditions) +
                               " ORDER BY " + order + " LIMIT 500) "
                               "SELECT s.*,c.name FROM candidates p JOIN samples s ON s.id=p.id "
                               "JOIN conversations c ON c.user=s.user ORDER BY p.weight DESC,p.time_ms DESC", values)

    def search(self, params, user=None):
        self.mode = "lexical"
        self.semantic_partial = False
        self.semantic_scanned = 0
        query = params.get("query") or ""
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise SelfError("invalid-request", "检索内容须在 4000 字以内")
        limit = integer(params.get("limit"), 5, 1, MAX_SOURCES)
        before = integer(params.get("beforeTimeMs"), now_ms(), 1, 2**53)
        before_seq = integer(params.get("beforeSeq"), None, 1, 2**53)
        tokens = sorted(terms(query), key=lambda token: (-len(token), token))[:128]
        source = self.store.meta("source")
        if source is None or params.get("sourceFingerprint") not in (None, source):
            return []
        conditions, values = ["s.source=?", "s.time_ms<?", "NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)"], [source, before]
        if user and before_seq:
            conditions.append("(s.user<>? OR s.to_seq<?)")
            values.extend((user, before_seq))
        rows = self._lexical(tokens, conditions, values)
        target_scene = scene(query)
        query_weights = {token: 0.22 if len(token) == 1 else 1 for token in tokens}
        excluded = set(params.get("excludeMessageIds") or [])
        if params.get("messageId"):
            excluded.add(params["messageId"])
        for row in rows:
            own = set(terms(row["input"]))
            overlap = sum(weight for token, weight in query_weights.items() if token in own)
            lexical = overlap / max(1, sum(query_weights.values()))
            age = max(0, before - row["time_ms"]) / 86400000
            row["score"] = lexical + (0.12 if row["user"] == user else 0) + (
                0.1 if target_scene != "闲聊" and target_scene == row["scene"] else 0) + 0.04 / (1 + math.log1p(age))
        lexical_ranked = sorted(rows, key=lambda row: (-row["score"], -row["time_ms"], row["id"]))
        semantic = recall(self.core, self.store, self.account, query, source, before, user, before_seq)
        self.mode = "hybrid" if semantic["used"] else "lexical"
        self.semantic_partial, self.semantic_scanned = semantic["partial"], semantic["scanned"]
        # Independent routes are fused by reciprocal rank. Exact keyword
        # coverage earns a small boost; contact/scene affinity only breaks ties.
        lexical_order = {row["id"]: rank + 1 for rank, row in enumerate(lexical_ranked[:64])}
        semantic_order = {identifier: rank + 1 for rank, identifier in enumerate(semantic["ids"])}
        combined = {row["id"]: row for row in lexical_ranked[:64]}
        if semantic["ids"]:
            semantic_rows = self.store.rows("SELECT s.*,c.name FROM samples s JOIN conversations c ON c.user=s.user "
                                           "WHERE s.id IN (" + ",".join("?" for _ in semantic["ids"]) + ") AND s.source=? "
                                           "AND s.time_ms<? AND NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)",
                                           (*semantic["ids"], source, before))
            for row in semantic_rows:
                if user and before_seq and row["user"] == user and row["to_seq"] >= before_seq:
                    continue
                combined[row["id"]] = row
        for row in combined.values():
            kinds = [kind for kind, order in (("lexical", lexical_order), ("semantic", semantic_order)) if row["id"] in order]
            row["matchKinds"] = kinds
            if semantic["used"]:
                score = sum(1 / (60 + order[row["id"]]) for order in (lexical_order, semantic_order) if row["id"] in order)
                own = set(terms(row["input"]))
                coverage = sum(weight for token, weight in query_weights.items() if token in own) / max(1, sum(query_weights.values()))
                row["score"] = score + (0.01 if coverage >= 0.8 and tokens else 0) + (
                    0.0008 if row["user"] == user else 0) + (0.0005 if target_scene != "闲聊" and row["scene"] == target_scene else 0)
        ranked = sorted(combined.values(), key=lambda row: (-row["score"], -row["time_ms"], row["id"]))
        selected, per_user, seen, position = [], {}, set(), 0
        # At most 16 candidates incur source verification; no unbounded API fanout.
        while position < min(16, len(ranked)) and len(selected) < limit:
            batch, batch_users, batch_norms = [], {}, set()
            while position < min(16, len(ranked)) and len(batch) < min(4, limit - len(selected)):
                row = ranked[position]
                if row["norm"] in seen or per_user.get(row["user"], 0) >= 2:
                    position += 1
                    continue
                if row["user"] == user and any(item["id"] in excluded for item in json.loads(row["evidence"])):
                    position += 1
                    continue
                # Wait for this batch before spending reads on a duplicate or
                # third sample from a contact. Retry it only if earlier ones
                # fail verification, matching the sequential selection policy.
                if row["norm"] in batch_norms or per_user.get(row["user"], 0) + batch_users.get(row["user"], 0) >= 2:
                    break
                position += 1
                batch.append(row)
                batch_norms.add(row["norm"])
                batch_users[row["user"]] = batch_users.get(row["user"], 0) + 1
            for row, valid in zip(batch, self._verify_many(batch)):
                if not valid or row["norm"] in seen or per_user.get(row["user"], 0) >= 2:
                    continue
                selected.append({"id": row["id"], "hash": digest(row), "user": row["user"], "name": row["name"],
                                 "sameContact": row["user"] == user, "timeMs": row["time_ms"], "scene": row["scene"],
                                 "input": row["input"], "reply": row["reply"], "score": round(row["score"], 6),
                                 "matchKinds": row["matchKinds"]})
                seen.add(row["norm"])
                per_user[row["user"]] = per_user.get(row["user"], 0) + 1
        return [row for row in selected if self.references_current([row])]

    def validate(self, refs):
        if not isinstance(refs, list) or len(refs) > MAX_SOURCES:
            raise SelfError("invalid-request", "样例引用不正确")
        candidates = []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
                return False
            rows = self.store.rows("SELECT * FROM samples WHERE id=? AND NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=id)", (ref["id"],))
            if not rows or ref.get("hash") != digest(rows[0]):
                return False
            candidates.append(rows[0])
        return all(self._verify_many(candidates))

    def references_current(self, refs):
        for ref in refs:
            rows = self.store.rows("SELECT * FROM samples WHERE id=? AND NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=id)", (ref["id"],))
            if not rows or digest(rows[0]) != ref.get("hash"):
                return False
        return True


def bounded_context(sources, style, budget):
    """Budget covers both structured sources and text, not just a prompt preview."""
    output = {"sources": [], "text": HEADER, "profile": {"style": style[:min(1000, budget // 5)]}}
    for source in sources:
        item = dict(source)
        # Full UI previews live in search. Model context uses short, attributed examples.
        item["input"], item["reply"] = item["input"][-600:], item["reply"][:600]
        tentative = {**output, "sources": output["sources"] + [item]}
        if len(json.dumps(tentative, ensure_ascii=False)) > budget:
            continue
        output = tentative
    return output
