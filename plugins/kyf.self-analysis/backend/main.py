"""RPC entry for 自我剖析. All runtime data stays in the host-assigned local directory.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import threading

from kyf_plugin import CoreError, Plugin, RpcError
from self_contracts import SelfError, account_scope
from self_service import Service

plugin = Plugin()
service = None
lock = threading.Lock()


def application():
    global service
    with lock:
        if service is None:
            service = Service(plugin)
            plugin.on_shutdown.append(service.close)
        return service


def register(name, action):
    @plugin.method(name)
    def handle(params, context):
        try:
            account = account_scope(params, context)
            return action(application(), account, params, context)
        except (SelfError, CoreError) as exc:
            raise RpcError(exc.code, exc.message) from None


register("self.status", lambda app, account, params, ctx: app.status(account))
register("self.analyze", lambda app, account, params, ctx: app.analyze(account, params))
register("self.analysisStatus", lambda app, account, params, ctx: app.analysis_status(account))
register("self.analysisPause", lambda app, account, params, ctx: app.analysis_pause(account, params))
register("self.analysisResume", lambda app, account, params, ctx: app.analysis_resume(account, params))
register("self.analysisSettings", lambda app, account, params, ctx: app.analysis_settings(account, params))
register("self.judgeShadow", lambda app, account, params, ctx: app.judge_shadow(account))
register("self.persona", lambda app, account, params, ctx: app.persona(account))
register("self.personaEvidence", lambda app, account, params, ctx: app.opinion_evidence(account, params, "persona"))
register("self.personaEdit", lambda app, account, params, ctx: app.persona_edit(account, params))
register("self.values", lambda app, account, params, ctx: app.values(account))
register("self.valuesEvidence", lambda app, account, params, ctx: app.opinion_evidence(account, params, "values"))
register("self.valuesEdit", lambda app, account, params, ctx: app.values_edit(account, params))
register("self.periods", lambda app, account, params, ctx: app.periods(account))
register("self.periodDetail", lambda app, account, params, ctx: app.period_detail(account, params))
register("self.periodEvidence", lambda app, account, params, ctx: app.period_evidence(account, params))
register("self.periodEdit", lambda app, account, params, ctx: app.period_edit(account, params))
register("self.worldview", lambda app, account, params, ctx: app.worldview(account))
register("self.worldviewDetail", lambda app, account, params, ctx: app.worldview_detail(account, params))
register("self.worldviewEvidence", lambda app, account, params, ctx: app.period_evidence(account, params, "worldview8"))
register("self.worldviewEdit", lambda app, account, params, ctx: app.worldview_edit(account, params))
register("self.timeline", lambda app, account, params, ctx: app.timeline(account, params))
register("self.timelineEvidence", lambda app, account, params, ctx: app.timeline_evidence(account, params))
register("self.timelineEdit", lambda app, account, params, ctx: app.timeline_edit(account, params))
register("self.timelineUndo", lambda app, account, params, ctx: app.timeline_undo(account, params))
register("self.build", lambda app, account, params, ctx: app.build(account, params.get("mode", "incremental")))
register("self.start", lambda app, account, params, ctx: app.build(account, params.get("mode", "incremental")))
register("self.pause", lambda app, account, params, ctx: app.pause(account))
register("self.resume", lambda app, account, params, ctx: app.resume(account))
register("self.reset", lambda app, account, params, ctx: app.reset(account))
register("self.search", lambda app, account, params, ctx: app.search(account, params, ctx.get("user")))
register("self.compare", lambda app, account, params, ctx: app.compare(account, params, ctx.get("user")))
register("self.draft", lambda app, account, params, ctx: app.draft(account, params))
register("self.draftStatus", lambda app, account, params, ctx: app.draft_status(account))
register("self.draftPause", lambda app, account, params, ctx: app.draft_pause(account))
register("self.reply", lambda app, account, params, ctx: app.reply(account, params))
register("self.replyStatus", lambda app, account, params, ctx: app.reply_status(account))
register("self.tags", lambda app, account, params, ctx: app.tags(account))
register("self.tagsAnalyze", lambda app, account, params, ctx: app.tags_analyze(account))
register("self.tagsPause", lambda app, account, params, ctx: app.tags_pause(account))
register("self.tagEvidence", lambda app, account, params, ctx: app.tag_evidence(account, params))
register("self.tagEdit", lambda app, account, params, ctx: app.tag_edit(account, params))
register("self.context", lambda app, account, params, ctx: app.context(account, params, ctx.get("user")))
register("self.exclude", lambda app, account, params, ctx: app.exclude(account, params.get("id"), params.get("excluded", True)))
register("self.exclusions", lambda app, account, params, ctx: app.exclusions(account))


@plugin.event("message.new")
def changed(event):
    application().changed(event.get("account"), event.get("user"))


if __name__ == "__main__":
    plugin.run()
