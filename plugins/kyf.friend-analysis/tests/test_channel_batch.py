"""Synthetic three-channel scheduling, cancellation and whole-window regressions.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import harness  # noqa: F401 - repository paths, no live services
from kyf_plugin import Cancelled as TaskCancelled
from kyf_plugin.models import ModelError
import friend_analysis as A
import friend_persona as P


class Task:
    token = "synthetic-token"
    execution = {"id": "synthetic-execution"}

    def __init__(self):
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise TaskCancelled()


class Runs:
    def __init__(self):
        self.owner = threading.get_ident()
        self.writes = []
        self.measures = []

    def set_run(self, *args, **kwargs):
        assert threading.get_ident() == self.owner, "worker wrote progress"
        self.writes.append(kwargs)

    def record_measurement(self, *args):
        assert threading.get_ident() == self.owner, "worker wrote measurement"
        self.measures.append(args)


class Models:
    def __init__(self, behavior=None):
        self.behavior = behavior or (lambda _index, _task: None)
        self.barrier = threading.Barrier(3)
        self.lock = threading.Lock()
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.exited = 0
        self.all_entered = threading.Event()

    def generate(self, task, request, step=None):
        task.check()
        index = [c.system for c in P.CHANNELS].index(request["system"])
        with self.lock:
            self.requests.append((index, dict(request), step, task.token))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 3:
                self.all_entered.set()
        try:
            self.barrier.wait(timeout=3)
            self.behavior(index, task)
            return {"text": str(index), "usage": {"inputTokens": index + 10, "outputTokens": index + 2}}
        finally:
            with self.lock:
                self.active -= 1
                self.exited += 1


class ChannelBatchTests(unittest.TestCase):
    def setup_batch(self, behavior=None):
        models = Models(behavior)
        analysis = A.FriendAnalysis.__new__(A.FriendAnalysis)
        analysis.plugin = SimpleNamespace(models=models)
        task = Task()
        ctx = SimpleNamespace(task=task, config={"contextTokens":32768,"modelFp":"synthetic-model"},
            entry=SimpleNamespace(cancel=threading.Event(), reason=None, run_id="synthetic-run"),
            steps=A.new_steps(), runs=Runs())
        analysis._check = lambda _ctx: (_ for _ in ()).throw(A.Cancelled()) if ctx.entry.cancel.is_set() else None
        self.part = [{"seq":1,"msg_id":"synthetic-message","side":"other","sender_name":"Synthetic",
                      "kind":"text","text":"Synthetic content","time_ms":1700000000000,"raw":"{}"}]
        return analysis, ctx, models

    def parse(self, ctx, text):
        self.assertEqual(threading.get_ident(), ctx.runs.owner)
        return int(text)

    def invoke(self, analysis, ctx, parse=None):
        with patch.object(P,"parse_evidence", side_effect=parse or (lambda text:self.parse(ctx,text))), \
                patch.object(P,"validate_evidence", side_effect=lambda result, _part, channel:[(channel,result)]):
            return analysis._persona_batch(ctx,"archive",self.part)

    def assert_joined(self, models):
        self.assertEqual(models.active,0)
        self.assertEqual(models.exited,len(models.requests))
        self.assertFalse(any(t.name.startswith("friend-evidence") for t in threading.enumerate()))

    def test_overlapping_calls_keep_original_payload_order_and_main_thread_accounting(self):
        completed=[]

        def behavior(index,_task):
            time.sleep((2-index)*.025)
            completed.append(index)

        analysis,ctx,models=self.setup_batch(behavior)
        items,processed,excluded=self.invoke(analysis,ctx)
        self.assertEqual(models.max_active,3)
        self.assertEqual(completed,[2,1,0])
        self.assertEqual(items,[(channel.key,i) for i,channel in enumerate(P.CHANNELS)])
        self.assertEqual((processed,excluded),(1,0))
        for index,request,step,token in models.requests:
            self.assertEqual(request,{"system":P.CHANNELS[index].system,"prompt":P.segment_prompt(self.part),
                "jsonMode":True,"thinking":False,"maxOutputTokens":min(analysis._output_cap(ctx),P.CHANNELS[index].out_tokens*2)})
            self.assertEqual(step,"friend-persona-evidence")
            self.assertEqual(token,Task.token)
        progress=next(s for s in ctx.steps if s['key']=='archive')
        self.assertEqual((progress['calls'],progress['inputTokens'],progress['outputTokens']),(3,33,9))
        self.assertEqual(len(ctx.runs.measures),3)
        self.assert_joined(models)

    def test_later_error_does_not_override_earlier_channel_error(self):
        second_failed=threading.Event()

        def behavior(index,task):
            if index==1:
                second_failed.set()
                raise ModelError("output-truncated","synthetic later truncation")
            if index==0:
                self.assertTrue(second_failed.wait(1))
                raise A.AdvisorError("invalid-output","synthetic first error")
            task.cancelled.wait(2)
            task.check()

        analysis,ctx,models=self.setup_batch(behavior)
        with self.assertRaises(A.AdvisorError) as failure:self.invoke(analysis,ctx)
        self.assertEqual(failure.exception.code,"invalid-output")
        self.assertFalse(ctx.task.cancelled.is_set())
        self.assertFalse(ctx.entry.cancel.is_set())
        self.assert_joined(models)

    def test_sdk_truncation_reaches_original_split_without_same_window_retries(self):
        analysis,ctx,models=self.setup_batch()
        rows=[self.part[0],dict(self.part[0],seq=2,msg_id='synthetic-second')]
        ctx.account,ctx.user,ctx.fingerprint,ctx.store='account','friend','source',None
        ctx.entry.resuming=False
        ctx.rebuild_step=lambda _key:False
        commits=[]
        ctx.runs.begin_scan=lambda *_args:1
        def commit(_user,_step,_generation,items,state,guard):
            self.assertEqual(threading.get_ident(),ctx.runs.owner)
            self.assertEqual(models.active,0)
            guard(None)
            commits.append((state['start'],state['covered'],items))
        ctx.runs.commit_scan=commit
        analysis.scan_state=lambda *_args:{'continue':None,'published':None,'start':0}
        analysis._guard=lambda *_args,**_kwargs:lambda _connection:None
        original=models.generate
        def generate(task,request,step=None):
            result=original(task,request,step)
            if request['prompt'].count('\n') and request['system']==P.CHANNELS[0].system:
                deadline=time.monotonic()+2
                while models.exited<3 and time.monotonic()<deadline:time.sleep(.005)
                raise ModelError('output-truncated',partial='{"evidence":[')
            return result
        models.generate=generate
        with patch.object(A,'RETRY_DELAYS',(0,0)), \
                patch.object(P,'parse_evidence',side_effect=lambda text:self.parse(ctx,text)), \
                patch.object(P,'validate_evidence',side_effect=lambda result,_part,channel:[(channel,result)]):
            analysis._scan(ctx,'archive','persona',rows,2,
                lambda *_args:[(rows,0,2)],analysis._persona_batch)
        self.assertEqual([(start,end) for start,end,_items in commits],[(0,1),(1,2)])
        self.assertTrue(all(items==[(c.key,i) for i,c in enumerate(P.CHANNELS)] for _,_,items in commits))
        self.assertEqual(len(models.requests),9)  # Parent once, then both full three-channel children.
        self.assertEqual(sum(request['prompt'].count('\n')>0 for _,request,_,_ in models.requests),3)
        progress=next(s for s in ctx.steps if s['key']=='archive')
        self.assertEqual((progress['calls'],progress['inputTokens'],progress['outputTokens']),(8,89,25))
        self.assertFalse(ctx.entry.cancel.is_set())
        self.assertFalse(ctx.task.cancelled.is_set())
        self.assert_joined(models)

    def test_only_sdk_truncation_changes_error_routing(self):
        for code in ('output-truncated','network','invalid-output','api-not-configured'):
            with self.subTest(code=code):
                analysis,ctx,_models=self.setup_batch()
                original=ModelError(code,'synthetic provider detail')
                calls=[]
                def action():
                    calls.append(None)
                    raise original
                with patch.object(A,'RETRY_DELAYS',(0,0)), self.assertRaises(Exception) as failure:
                    analysis._retrying(ctx,'archive',action,split_truncation=True)
                if code=='output-truncated':
                    self.assertIsInstance(failure.exception,A.AdvisorError)
                    self.assertEqual(failure.exception.code,code)
                    self.assertEqual(str(failure.exception),'模型输出过长被截断，可以继续')
                    self.assertIs(failure.exception.__cause__,original)
                    self.assertEqual(len(calls),1)
                else:
                    self.assertIs(failure.exception,original)
                    self.assertEqual(len(calls),3)

    def test_non_scan_sdk_truncation_keeps_existing_retries_and_exception(self):
        analysis,ctx,_models=self.setup_batch()
        original=ModelError('output-truncated')
        calls=[]
        def action():
            calls.append(None)
            raise original
        with patch.object(A,'RETRY_DELAYS',(0,0)), self.assertRaises(ModelError) as failure:
            analysis._retrying(ctx,'persona',action)
        self.assertIs(failure.exception,original)
        self.assertEqual(len(calls),3)

    def test_parent_and_entry_cancel_join_every_inflight_call(self):
        def behavior(_index,task):
            task.cancelled.wait(3)
            task.check()

        for source in ("task","entry"):
            with self.subTest(source=source):
                analysis,ctx,models=self.setup_batch(behavior)
                errors=[]

                def run():
                    ctx.runs.owner=threading.get_ident()
                    try:self.invoke(analysis,ctx)
                    except BaseException as exc:errors.append(exc)

                thread=threading.Thread(target=run)
                thread.start()
                self.assertTrue(models.all_entered.wait(2))
                began=time.monotonic()
                getattr(ctx,source).cancelled.set() if source=="task" else ctx.entry.cancel.set()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic()-began,1)
                self.assertIsInstance(errors[0],A.Cancelled)
                self.assert_joined(models)

    def test_successful_speculative_calls_are_counted_after_parse_failure(self):
        analysis,ctx,models=self.setup_batch()
        # All results are complete before the first parse rejects the window.
        def parse(_text):
            deadline=time.monotonic()+2
            while models.exited<3 and time.monotonic()<deadline:time.sleep(.005)
            raise A.AdvisorError("invalid-output","synthetic parse error")
        with self.assertRaises(A.AdvisorError):self.invoke(analysis,ctx,parse)
        self.assertEqual(next(s for s in ctx.steps if s['key']=='archive')['calls'],3)
        self.assertEqual(len(ctx.runs.measures),3)
        self.assertFalse(ctx.entry.cancel.is_set())
        self.assert_joined(models)

    def test_whole_window_retries_two_times_and_truncation_does_not_retry(self):
        for code,attempts in (("invalid-output",3),("output-truncated",1)):
            with self.subTest(code=code):
                analysis,ctx,models=self.setup_batch()
                def parse(_text):raise A.AdvisorError(code,"synthetic error")
                with patch.object(A,"RETRY_DELAYS",(0,0)), self.assertRaises(A.AdvisorError):
                    analysis._retrying(ctx,"archive",lambda:self.invoke(analysis,ctx,parse))
                self.assertEqual(len(models.requests),3*attempts)
                self.assertFalse(ctx.entry.cancel.is_set())
                self.assert_joined(models)

    def test_discovery_like_block_is_joined_before_failure_returns(self):
        release=threading.Event()
        discovery_done=threading.Event()
        def behavior(index,task):
            if index==0:raise A.AdvisorError("invalid-output","synthetic failure")
            if index==1:
                release.wait(2)  # Represents SDK service discovery's bounded HTTP wait.
                discovery_done.set()
                task.check()
            if index==2:
                task.cancelled.wait(2)
                task.check()
        analysis,ctx,models=self.setup_batch(behavior)
        errors=[]
        def run():
            ctx.runs.owner=threading.get_ident()
            try:self.invoke(analysis,ctx)
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        self.assertTrue(models.all_entered.wait(2))
        time.sleep(.05)
        self.assertTrue(thread.is_alive())
        release.set();thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(discovery_done.is_set())
        self.assertIsInstance(errors[0],A.AdvisorError)
        self.assertFalse(ctx.task.cancelled.is_set())
        self.assert_joined(models)

    def test_empty_completed_response_still_counts_actual_usage_without_publishing(self):
        analysis,ctx,models=self.setup_batch()
        original=models.generate
        def empty(task,request,step=None):
            result=original(task,request,step)
            result['text']=''
            return result
        models.generate=empty
        with self.assertRaises(A.AdvisorError) as failure:self.invoke(analysis,ctx)
        self.assertEqual(failure.exception.code,'invalid-output')
        progress=next(s for s in ctx.steps if s['key']=='archive')
        self.assertEqual((progress['calls'],progress['inputTokens'],progress['outputTokens']),(3,33,9))
        self.assert_joined(models)

    def test_original_scan_split_and_failed_window_commit_boundary(self):
        for code in ('invalid-output','output-truncated'):
            with self.subTest(code=code):
                analysis,ctx,models=self.setup_batch()
                rows=[self.part[0],dict(self.part[0],seq=2,msg_id='synthetic-second')]
                ctx.account,ctx.user,ctx.fingerprint,ctx.store='account','friend','source',None
                ctx.entry.resuming=False
                ctx.rebuild_step=lambda _key:False
                commits=[]
                ctx.runs.begin_scan=lambda *_args:1
                def commit(_user,_step,_generation,items,state,guard):
                    self.assertEqual(threading.get_ident(),ctx.runs.owner)
                    self.assertEqual(models.active,0)
                    guard(None)
                    commits.append((state['start'],state['covered'],items))
                ctx.runs.commit_scan=commit
                analysis.scan_state=lambda *_args:{'continue':None,'published':None,'start':0}
                analysis._guard=lambda *_args,**_kwargs:lambda _connection:None
                original=models.generate
                def generate(task,request,step=None):
                    result=original(task,request,step)
                    if request['prompt'].count('\n'):
                        raise A.AdvisorError(code,'synthetic full-window failure')
                    return result
                models.generate=generate
                with patch.object(A,'RETRY_DELAYS',(0,0)), \
                        patch.object(P,'parse_evidence',side_effect=lambda text:self.parse(ctx,text)), \
                        patch.object(P,'validate_evidence',side_effect=lambda result,_part,channel:[(channel,result)]):
                    call=lambda:analysis._scan(ctx,'archive','persona',rows,2,
                        lambda *_args:[(rows,0,2)],analysis._persona_batch)
                    if code=='invalid-output':
                        with self.assertRaises(A.AdvisorError):call()
                        self.assertEqual(commits,[])
                    else:
                        call()
                        self.assertEqual([(start,end) for start,end,_items in commits],[(0,1),(1,2)])
                        self.assertTrue(all(items==[(c.key,i) for i,c in enumerate(P.CHANNELS)] for _,_,items in commits))
                self.assertEqual(len(models.requests),9)
                self.assert_joined(models)

    def test_scope_change_after_model_result_rejects_window_and_joins(self):
        analysis,ctx,models=self.setup_batch()
        checks=[]
        def check(_ctx):
            checks.append(None)
            if len(checks)>1:
                ctx.entry.reason='interrupted'
                ctx.entry.cancel.set()
                raise A.Cancelled()
        analysis._check=check
        with self.assertRaises(A.Cancelled):self.invoke(analysis,ctx)
        self.assertEqual(ctx.entry.reason,'interrupted')
        self.assert_joined(models)


if __name__=="__main__":unittest.main()
