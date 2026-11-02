"""Build reply examples from the unified, verified local chat snapshot.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import json

from self_contracts import SelfError, now_ms
from self_samples import extract

VERSION = 1


class ReplyLibrary:
    def __init__(self, store, corpus, account):
        self.store, self.corpus, self.account = store, corpus, account
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS shared_reply_stage(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")

    def _rows(self, user):
        after = 0
        while rows := self.corpus.rows("SELECT seq,payload FROM profile_corpus_reply_rows WHERE user=? AND seq>? "
                                     "ORDER BY seq LIMIT 500", (user, after)):
            yield [json.loads(row["payload"]) for row in rows]
            after = rows[-1]["seq"]

    def _signature(self, row, check):
        value = hashlib.sha256(json.dumps([VERSION, row["source"], row["epoch"]]).encode())
        for rows in self._rows(row["user"]):
            check()
            value.update(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode())
        return value.hexdigest()

    def work(self, context, progress=None):
        store, corpus = self.store, self.corpus
        source = corpus.meta("source")
        if store.meta("source") not in (None, source):
            raise SelfError("source-changed", "聊天来源已变化，原知识库已保留，请恢复原来源后继续")
        # Reply examples come from WeChat private chats only; external chats have no reply rows.
        chats = corpus.rows("SELECT * FROM profile_corpus_conversations WHERE is_group=0 AND external=0 ORDER BY user")
        with store.lock, store.db:
            context.check()
            store._meta("source", source)
            store._meta("coverage", {"state": "partial" if corpus.counts()["gaps"] else "complete"})
            store.update_build(state="running", phase="indexing", catalogComplete=corpus.meta("catalogComplete", False))
            # A partial directory cannot prove that a previously indexed chat vanished.
            if corpus.meta("catalogComplete", False) and not corpus.meta("directoryPartial", False):
                for old in store.rows("SELECT user FROM conversations WHERE user NOT IN "
                                      "(SELECT user FROM profile_corpus_conversations WHERE is_group=0 AND external=0)"):
                    store._clear_index(old["user"])
                    store.db.execute("DELETE FROM conversations WHERE user=?", (old["user"],))
                    store.bump(generation=True)
        for index, row in enumerate(chats):
            context.check()
            user = row["user"]
            if row["state"] != "done":
                with store.lock, store.db:
                    store.db.execute("INSERT OR IGNORE INTO conversations(user,name) VALUES(?,?)", (user, row["name"]))
                    store.finish_conversation(user, "聊天读取未完成，已保留原样本")
                continue
            signature = self._signature(row, context.check)
            previous = store.meta("sharedReply:" + user)
            local = store.conversation(user)
            if previous != signature or local is None:
                carry, skipped, last_seq = {}, 0, 0
                with store.lock, store.db:
                    context.check()
                    store.db.execute("DELETE FROM shared_reply_stage")
                for messages in self._rows(user):
                    context.check()
                    # The reader already checked attribution and replaced ineligible
                    # original rows with barriers. Do not infer new provenance here.
                    samples, carry, count = extract(messages, carry, user, source, row["epoch"])
                    skipped += count
                    last_seq = messages[-1]["seq"]
                    with store.lock, store.db:
                        context.check()
                        for sample in samples:
                            if sample.get("delete"):
                                store.db.execute("DELETE FROM shared_reply_stage WHERE id=?", (sample["id"],))
                            else:
                                store.db.execute("INSERT OR REPLACE INTO shared_reply_stage VALUES(?,?)",
                                                 (sample["id"], json.dumps(sample, ensure_ascii=False)))
                # Publish a complete chat atomically. Pausing during staging keeps
                # the old chat usable; unchanged examples retain their vectors.
                with store.lock, store.db:
                    context.check()
                    removed = store.rows("SELECT id FROM samples WHERE user=? AND id NOT IN (SELECT id FROM shared_reply_stage)", (user,))
                    for sample in removed:
                        for table in ("embeddings", "terms"):
                            store.db.execute(f"DELETE FROM {table} WHERE sample_id=?", (sample["id"],))
                        store.db.execute("DELETE FROM samples WHERE id=?", (sample["id"],))
                    cursor = store.db.execute("SELECT payload FROM shared_reply_stage")
                    while batch := cursor.fetchmany(200):
                        context.check()
                        store.write_samples([json.loads(item[0]) for item in batch])
                    store.db.execute("INSERT INTO conversations(user,name,source,epoch,revision,after_seq,carry,state,skipped,"
                                     "total_count,stale,needs_rebuild,updated_ms) VALUES(?,?,?,?,?,?,?,'done',?,?,0,0,?) "
                                     "ON CONFLICT(user) DO UPDATE SET name=excluded.name,source=excluded.source,epoch=excluded.epoch,"
                                     "revision=excluded.revision,after_seq=excluded.after_seq,carry=excluded.carry,state='done',"
                                     "skipped=excluded.skipped,total_count=excluded.total_count,stale=0,needs_rebuild=0,error=NULL,"
                                     "updated_ms=excluded.updated_ms",
                                     (user, row["name"], source, row["epoch"], row["revision"], last_seq,
                                      json.dumps(carry, ensure_ascii=False), skipped, row["messages_read"], now_ms()))
                    store.db.execute("DELETE FROM proofs WHERE user=?", (user,))
                    store.db.execute("INSERT INTO proofs(user,seq,msg_id,hash) SELECT user,seq,msg_id,hash FROM "
                                     "profile_corpus_proofs WHERE user=?", (user,))
                    store._meta("sharedReply:" + user, signature)
                    store.bump(generation=bool(removed))
                store.samples_changed.set()
            else:
                with store.lock, store.db:
                    context.check()
                    store.db.execute("UPDATE conversations SET name=?,state='done',error=NULL,stale=0,updated_ms=? WHERE user=?",
                                     (row["name"], now_ms(), user))
                    if local["name"] != row["name"]:
                        store.bump()
            if progress:
                progress(index + 1, len(chats))
        counts = store.counts()
        partial = bool(counts["failed"] or corpus.counts()["gaps"])
        store.update_build(state="partial" if partial else "complete", phase="done", completedAt=now_ms(),
                           currentUser=None, error=None)
        return {"sampleCount": counts["samples"], "partial": partial}
