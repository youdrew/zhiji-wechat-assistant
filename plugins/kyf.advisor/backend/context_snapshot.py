"""Frozen friend scope and material availability for one admitted assistant turn.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
This is a record of supplied material, never evidence that the model read a file.
"""
from __future__ import annotations

import copy
import time

from contracts import valid_scope_value


def public(value):
    """Only the documented, path-free fields may reach a question bubble."""
    if (not isinstance(value, dict) or value.get("version") != 1 or
            value.get("state") not in {"pending", "recorded"} or
            type(value.get("capturedAtMs")) is not int or value["capturedAtMs"] < 0 or
            not isinstance(value.get("contacts"), list) or not value["contacts"]):
        return None
    contacts, seen = [], set()
    for index, item in enumerate(value["contacts"]):
        if not isinstance(item, dict):
            return None
        user, name, sources = item.get("user"), item.get("name"), item.get("sources")
        if (not isinstance(user, str) or not user or len(user) > 256 or user in seen or
                not isinstance(name, str) or not name.strip() or len(name) > 240 or
                any(ord(char) < 32 or ord(char) == 127 for char in user + name) or
                type(item.get("primary")) is not bool or item["primary"] != (index == 0) or
                item.get("access") != ("required" if index == 0 else "available") or
                item.get("status") not in {"pending", "ready", "empty", "unavailable"} or
                not isinstance(sources, list) or any(not isinstance(source, str) or source not in {"chat", "profile"} for source in sources) or
                len(set(sources)) != len(sources)):
            return None
        seen.add(user)
        contacts.append({"user": user, "name": name, "primary": item["primary"], "access": item["access"],
                         "sources": list(sources), "status": item["status"]})
    return {"version": 1, "state": value["state"], "capturedAtMs": value["capturedAtMs"], "contacts": contacts}


def pending(user, name, participants, inherited=None):
    """Retries keep the original question's identities; each attempt prepares its own materials."""
    if inherited is not None:
        snapshot = copy.deepcopy(inherited)
        snapshot["state"] = "pending"
        snapshot["capturedAtMs"] = int(time.time() * 1000)
        for item in snapshot["contacts"]:
            item.update(sources=[], status="pending")
        return snapshot
    contacts, seen = [], set()
    for primary, item in [(True, {"user": user, "name": name}), *[(False, item) for item in participants]]:
        identity = valid_scope_value(item["user"], "context friend")
        if identity in seen:
            continue
        seen.add(identity)
        label = " ".join(str(item.get("name") or identity).split())[:240] or identity[:240]
        contacts.append({"user": identity, "name": label, "primary": primary,
                         "access": "required" if primary else "available", "sources": [], "status": "pending"})
    return {"version": 1, "state": "pending", "capturedAtMs": int(time.time() * 1000), "contacts": contacts}


def recorded(snapshot, material, supplied=True):
    result = copy.deepcopy(snapshot)
    result["state"] = "recorded"
    for item in result["contacts"]:
        sources = material.get(item["user"]) if supplied else None
        item["sources"] = list(sources or [])
        item["status"] = "unavailable" if sources is None else "ready" if sources else "empty"
    return result


def for_run(snapshot, state):
    """A stopped preparation cannot remain visually pending after an aborted account clear."""
    value = public(snapshot)
    if value is not None and value["state"] == "pending" and state != "running":
        return recorded(value, {}, supplied=False)
    return value


def same_scope(left, right):
    return (left["capturedAtMs"] == right["capturedAtMs"] and
            [(item["user"], item["name"], item["primary"], item["access"]) for item in left["contacts"]] ==
            [(item["user"], item["name"], item["primary"], item["access"]) for item in right["contacts"]])
