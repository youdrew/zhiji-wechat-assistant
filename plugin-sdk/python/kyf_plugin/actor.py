"""Who made a plugin RPC call: the user's page or an MCP Agent.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Core puts ``actor: "agent"`` and ``agent`` (the MCP client's name) into the RPC context of
every call it forwards from its MCP channel. Page calls never carry them: Core builds a
page call's context from ``account`` and ``user`` only, so a page cannot pose as an Agent
and an Agent cannot pose as the user. Read the source from the context, never from params.
"""
from __future__ import annotations

USER = "user"
AGENT = "agent"
AGENT_NAME_LIMIT = 64


def actor(context):
    """``"agent"`` for a call Core forwarded from MCP, otherwise ``"user"``."""
    return AGENT if isinstance(context, dict) and context.get("actor") == AGENT else USER


def agent_name(context):
    """The MCP client's display name of an Agent call; ``""`` for page calls."""
    if actor(context) != AGENT:
        return ""
    name = context.get("agent")
    return " ".join(name.split())[:AGENT_NAME_LIMIT] if isinstance(name, str) else ""
