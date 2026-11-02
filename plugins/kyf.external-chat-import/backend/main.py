"""Bundled external-chat recognizer. Core owns files, review and saved records.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from kyf_plugin import Plugin

from transcription import Transcription, match, plan

plugin = Plugin()
transcription = Transcription(plugin)


@plugin.method("transcription.plan")
def recognition_plan(params, context):
    return plan()


@plugin.method("transcription.recognize")
def recognize(params, context):
    return transcription.recognize(params, context)


@plugin.method("transcription.review")
def review(params, context):
    return transcription.review(params, context)


@plugin.method("transcription.match")
def match_candidates(params, context):
    return match(params)


if __name__ == "__main__":
    plugin.run()
