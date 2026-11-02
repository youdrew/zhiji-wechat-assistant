"""kyf.mcp plugin entry: the MCP management page's bridge to Core's settings channel.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The page (``ui/``) calls these methods through kyfHost.rpc; each one forwards to a
named ``/plugin-api/v1/mcp/*`` route that only this plugin's token may reach. No
tool call and no other plugin ever gets here.
"""
from kyf_plugin import Plugin

from mcp_admin import McpAdmin

plugin = Plugin()
admin = None


def _service():
    global admin
    if admin is None:
        admin = McpAdmin(plugin.core)
    return admin


for _name in ("status", "profiles", "profile", "directory", "calls", "create_profile", "credential",
              "set_enabled", "set_mode", "set_global_management", "revoke", "grant_contact",
              "revoke_contact", "set_master", "disconnect_all", "settings"):
    def _make(name):
        def handler(params, _context):
            return getattr(_service(), name)(params)
        return handler
    plugin.method("mcp." + _name)(_make(_name))

plugin.run()
