"""The assistant catalog: agent cards and Skills in one small JSON document.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from bridge/advisor_store.py (AgentConfigStore), 2026-10-13: the document now lives in the plugin's data
directory (``.local/plugin-data/kyf.advisor/config.json``; Core copies the old ``.local/advisor/config.json`` there
once). Writes are atomic (temp file + fsync + replace) and every mutation bumps the affected agent revision.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import uuid

from contracts import (
    MAX_AGENTS, MAX_AGENT_DESCRIPTION, MAX_AGENT_NAME, MAX_AGENT_WELCOME,
    MAX_AGENT_PROMPT, MAX_SKILLS, MAX_SKILL_CONTENT, MAX_SKILL_DESCRIPTION,
    MAX_SKILL_IDS, MAX_SKILL_NAME, AdvisorError, builtin_agents, builtin_skill_ids, builtin_skills,
    default_welcome, new_identifier, normalize_agent_payload, normalize_skill, single_line,
    text_block, valid_enabled, valid_identifier, valid_request_id, valid_sha256,
)
from store import _checked_root, _ensure_directory, _reparse, _write_bytes_atomic, _write_json_atomic

SUPPORTED_CONFIG_VERSION = 2
MAX_IMPORT_REQUESTS = 200


def _check_import_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise AdvisorError("stopping", "导入已取消")


class AgentConfigStore:
    """The owned ``config.json`` agent/skill document in the plugin's data directory."""

    def __init__(self, directory):
        self.root = _checked_root(directory)
        self.directory = self.root
        self.path = self.directory / "config.json"
        self.backup_path = self.directory / "config.v1.backup.json"
        self.lock = threading.RLock()
        self._document = None
        self._legacy_bytes = None

    # -- loading / defaults -------------------------------------------------
    def _default_document(self):
        return {"version": SUPPORTED_CONFIG_VERSION,
                "agents": builtin_agents(), "skills": builtin_skills(),
                "deletedBuiltinSkillIds": [], "deletedBuiltinAgentIds": [], "importRequests": []}

    def _validate_agent(self, item):
        if not isinstance(item, dict) or not valid_identifier(item.get("id")):
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        if type(item.get("builtin")) is not bool or type(item.get("enabled")) is not bool:
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        result = {
            "id": item["id"],
            "name": single_line(item.get("name"), "name", MAX_AGENT_NAME),
            "description": single_line(item.get("description"), "description",
                                       MAX_AGENT_DESCRIPTION, allow_empty=True),
            "prompt": text_block(item.get("prompt"), "prompt", MAX_AGENT_PROMPT),
            "welcome": single_line(item.get("welcome", default_welcome(item["name"])), "welcome",
                                   MAX_AGENT_WELCOME, allow_empty=True),
            "skillIds": list(item.get("skillIds") or []),
            "enabled": item["enabled"],
            "builtin": item["builtin"],
            "revision": int(item.get("revision", 1)),
        }
        if "importedSkillId" in item:
            if not valid_identifier(item["importedSkillId"]):
                raise AdvisorError("config-invalid", "导入助手的技能关联已损坏")
            result["importedSkillId"] = item["importedSkillId"]
        return result

    def _validate_skill(self, item):
        if not isinstance(item, dict) or not valid_identifier(item.get("id")):
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        if type(item.get("builtin")) is not bool:
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        result = {
            "id": item["id"],
            "name": single_line(item.get("name"), "name", MAX_SKILL_NAME),
            "description": single_line(item.get("description"), "description",
                                       MAX_SKILL_DESCRIPTION, allow_empty=True),
            "content": text_block(item.get("content"), "content", MAX_SKILL_CONTENT),
            "builtin": item["builtin"],
        }
        if "package" in item:
            from packages import validate_package
            try:
                package = validate_package(item["package"])
                if item["builtin"] or package["entry"]["content"] != result["content"]:
                    raise AdvisorError("config-invalid", "导入技能包的正文已变化")
            except (AdvisorError, KeyError, TypeError, ValueError) as exc:
                raise AdvisorError("config-invalid", "导入技能包已损坏") from exc
            result["package"] = copy.deepcopy(package)
        return result

    def _validate_import_requests(self, value):
        if not isinstance(value, list) or len(value) > MAX_IMPORT_REQUESTS:
            raise AdvisorError("config-invalid", "导入请求记录已损坏")
        result, seen = [], set()
        for item in value:
            if not isinstance(item, dict):
                raise AdvisorError("config-invalid", "导入请求记录已损坏")
            try:
                request_id = valid_request_id(item.get("requestId"))
            except AdvisorError as exc:
                raise AdvisorError("config-invalid", "导入请求记录已损坏") from exc
            if (request_id is None or request_id in seen or not valid_sha256(item.get("fingerprint")) or
                    not valid_identifier(item.get("agentId")) or not valid_identifier(item.get("skillId"))):
                raise AdvisorError("config-invalid", "导入请求记录已损坏")
            seen.add(request_id)
            result.append({key: item[key] for key in ("requestId", "fingerprint", "agentId", "skillId")})
        return result

    def _load(self):
        if self._document is not None:
            return self._document
        if not os.path.lexists(self.path):
            self._document = self._default_document()
            return self._document
        if _reparse(self.path) or not self.path.is_file():
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        try:
            saved_bytes = self.path.read_bytes()
            raw = json.loads(saved_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise AdvisorError("config-invalid", "顾问配置已损坏") from exc
        if (not isinstance(raw, dict) or type(raw.get("version")) is not int or
                raw.get("version") not in (1, SUPPORTED_CONFIG_VERSION) or
                not isinstance(raw.get("agents"), list) or not isinstance(raw.get("skills"), list)):
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        agents = [self._validate_agent(item) for item in raw["agents"]]
        skills = [self._validate_skill(item) for item in raw["skills"]]
        deleted_skills = raw.get("deletedBuiltinSkillIds", [])
        if (not isinstance(deleted_skills, list) or any(not isinstance(value, str) for value in deleted_skills) or
                len(set(deleted_skills)) != len(deleted_skills) or not set(deleted_skills) <= set(builtin_skill_ids())):
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        deleted_agents = raw.get("deletedBuiltinAgentIds", [])
        builtin_ids = {agent["id"] for agent in builtin_agents()}
        if (not isinstance(deleted_agents, list) or any(not isinstance(value, str) for value in deleted_agents) or
                len(set(deleted_agents)) != len(deleted_agents) or not set(deleted_agents) <= builtin_ids):
            raise AdvisorError("config-invalid", "顾问配置已损坏")
        known_skills = {skill["id"] for skill in skills}
        packaged_skills = {skill["id"] for skill in skills if "package" in skill}
        for agent in agents:
            if len(agent["skillIds"]) > MAX_SKILL_IDS or len(set(agent["skillIds"])) != len(agent["skillIds"]):
                raise AdvisorError("config-invalid", "顾问配置已损坏")
            if not set(agent["skillIds"]) <= known_skills:
                raise AdvisorError("config-invalid", "顾问配置已损坏")
            if "importedSkillId" in agent and agent["importedSkillId"] not in packaged_skills:
                raise AdvisorError("config-invalid", "导入助手的技能关联已损坏")
        # Upgrade defaults without recreating a preset the user explicitly deleted.
        present = {agent["id"] for agent in agents}
        for index, preset in enumerate(builtin_agents()):
            if preset["id"] not in present and preset["id"] not in deleted_agents:
                preset["skillIds"] = [identifier for identifier in preset["skillIds"] if identifier not in deleted_skills]
                agents.insert(min(index, len(agents)), preset)
        present_skills = {skill["id"] for skill in skills}
        for index, preset in enumerate(builtin_skills()):
            if preset["id"] not in present_skills and preset["id"] not in deleted_skills:
                skills.insert(min(index, len(skills)), preset)
        self._document = {"version": SUPPORTED_CONFIG_VERSION, "agents": agents, "skills": skills,
                          "deletedBuiltinSkillIds": list(deleted_skills),
                          "deletedBuiltinAgentIds": list(deleted_agents),
                          "importRequests": self._validate_import_requests(raw.get("importRequests", []))}
        if raw["version"] == 1:
            self._legacy_bytes = saved_bytes
        return self._document

    def _commit(self, document, cancel_event=None):
        _check_import_cancel(cancel_event)
        _ensure_directory(self.root, self.directory)
        if self._legacy_bytes is not None:
            if _reparse(self.path) or not self.path.is_file() or self.path.read_bytes() != self._legacy_bytes:
                raise AdvisorError("config-invalid", "旧版配置已在迁移前变化，请重新载入")
            _ensure_directory(self.root, self.backup_path, leaf_file=True)
            if os.path.lexists(self.backup_path):
                if not self.backup_path.is_file() or self.backup_path.read_bytes() != self._legacy_bytes:
                    raise AdvisorError("config-invalid", "旧版配置备份与待迁移配置不一致")
            else:
                _write_bytes_atomic(self.backup_path, self._legacy_bytes)
        _write_json_atomic(self.path, document,
                           (lambda: _check_import_cancel(cancel_event)) if cancel_event is not None else None)
        self._document = document
        self._legacy_bytes = None

    # -- reads ---------------------------------------------------------------
    def agents(self):
        with self.lock:
            return copy.deepcopy(self._load()["agents"])

    def skills(self):
        with self.lock:
            return copy.deepcopy(self._load()["skills"])

    def agent(self, identifier):
        if not valid_identifier(identifier):
            raise AdvisorError("invalid-request", "invalid agent id")
        with self.lock:
            for item in self._load()["agents"]:
                if item["id"] == identifier:
                    return copy.deepcopy(item)
        return None

    # -- mutations -----------------------------------------------------------
    def save_agent(self, payload):
        agent = normalize_agent_payload(payload)
        with self.lock:
            document = copy.deepcopy(self._load())
            known_skills = {item["id"] for item in document["skills"]}
            if not set(agent["skillIds"]) <= known_skills:
                raise AdvisorError("skill-unknown", "选择的技能不在技能库中")
            identifier = agent["id"]
            found = next((item for item in document["agents"] if item["id"] == identifier), None)
            if found is None:
                if identifier is not None:
                    raise AdvisorError("agent-unknown", "agent 不存在")
                if len(document["agents"]) >= MAX_AGENTS:
                    raise AdvisorError("config-full", "agent 数量已达上限")
                found = {
                    "id": new_identifier("agent", uuid.uuid4().hex),
                    "name": agent["name"], "description": agent["description"],
                    "prompt": agent["prompt"], "skillIds": agent["skillIds"],
                    "welcome": agent["welcome"] if agent["welcome"] is not None else default_welcome(agent["name"]),
                    "enabled": True, "builtin": False, "revision": 1,
                }
                document["agents"].append(found)
            else:
                welcome = agent["welcome"]
                if welcome is None:
                    previous = found.get("welcome", default_welcome(found["name"]))
                    welcome = default_welcome(agent["name"]) if previous == default_welcome(found["name"]) else previous
                found.update({"name": agent["name"], "description": agent["description"],
                              "prompt": agent["prompt"], "skillIds": agent["skillIds"], "welcome": welcome})
                if found.get("importedSkillId") not in found["skillIds"]:
                    found.pop("importedSkillId", None)
                found["revision"] = int(found.get("revision", 1)) + 1
            self._commit(document)
            return copy.deepcopy(found)

    def set_enabled(self, identifier, enabled):
        if not valid_identifier(identifier):
            raise AdvisorError("invalid-request", "invalid agent id")
        enabled = valid_enabled(enabled)
        with self.lock:
            document = copy.deepcopy(self._load())
            found = next((item for item in document["agents"] if item["id"] == identifier), None)
            if found is None:
                raise AdvisorError("agent-unknown", "agent 不存在")
            found["enabled"] = enabled
            found["revision"] = int(found.get("revision", 1)) + 1
            self._commit(document)
            return copy.deepcopy(found)

    def delete_agent(self, identifier):
        if not valid_identifier(identifier):
            raise AdvisorError("invalid-request", "invalid agent id")
        with self.lock:
            document = copy.deepcopy(self._load())
            found = next((item for item in document["agents"] if item["id"] == identifier), None)
            if found is None:
                raise AdvisorError("agent-unknown", "agent 不存在")
            if found["builtin"] and identifier not in document["deletedBuiltinAgentIds"]:
                document["deletedBuiltinAgentIds"].append(identifier)
            document["agents"] = [item for item in document["agents"] if item["id"] != identifier]
            self._commit(document)
            return {"deleted": identifier}

    def import_skill(self, name, description, content):
        skill = normalize_skill(name, description, content)
        with self.lock:
            document = copy.deepcopy(self._load())
            if any(item["name"].strip().lower() == skill["name"].strip().lower()
                   for item in document["skills"]):
                raise AdvisorError("skill-name-taken", "技能名称已存在")
            if len(document["skills"]) >= MAX_SKILLS:
                raise AdvisorError("config-full", "技能数量已达上限")
            created = {"id": new_identifier("skill", uuid.uuid4().hex),
                       "name": skill["name"], "description": skill["description"],
                       "content": skill["content"], "builtin": False}
            document["skills"].append(created)
            self._commit(document)
            return copy.deepcopy(created)

    @staticmethod
    def _unique_import_name(name, items, maximum):
        existing = {item["name"].strip().casefold() for item in items}
        if name.strip().casefold() not in existing:
            return name
        for number in range(2, len(items) + 2):
            candidate = name + " (" + str(number) + ")"
            if len(candidate) > maximum:
                raise AdvisorError("skill-name-taken", "名称已存在，请缩短名称或另选名称后导入")
            if candidate.strip().casefold() not in existing:
                return candidate
        raise AdvisorError("skill-name-taken", "导入名称已存在")

    def import_assistant(self, package, agent_overrides=None, request_id=None, *, cancel_event=None):
        """Commit one immutable package and its assistant together, without touching threads."""
        from packages import validate_package
        _check_import_cancel(cancel_event)
        package = copy.deepcopy(validate_package(package))
        request_id = valid_request_id(request_id)
        overrides = {} if agent_overrides is None else agent_overrides
        if (not isinstance(overrides, dict) or
                not set(overrides) <= {"name", "description", "prompt", "welcome"}):
            raise AdvisorError("invalid-request", "导入助手字段不正确")
        entry = package["entry"]
        agent = normalize_agent_payload({
            "name": overrides.get("name", entry["name"]),
            "description": overrides.get("description", entry["description"]),
            "prompt": overrides.get("prompt", entry["defaultPrompt"]),
            "skillIds": [],
            **({"welcome": overrides["welcome"]} if "welcome" in overrides else {}),
        })
        skill_fields = {
            "name": single_line(entry["name"], "name", MAX_SKILL_NAME),
            "description": single_line(entry["description"], "description", MAX_SKILL_DESCRIPTION, allow_empty=True),
            "content": text_block(entry["content"], "content", MAX_SKILL_CONTENT),
        }
        identity = [package["source"], package["digest"]]
        fingerprint = hashlib.sha256(json.dumps([identity, agent], ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.lock:
            _check_import_cancel(cancel_event)
            document = copy.deepcopy(self._load())
            previous = next((item for item in document["importRequests"] if item["requestId"] == request_id), None)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise AdvisorError("request-conflict", "导入请求编号已用于另一份内容")
                old_agent = next((item for item in document["agents"] if item["id"] == previous["agentId"]), None)
                old_skill = next((item for item in document["skills"] if item["id"] == previous["skillId"]), None)
                if old_agent is None:
                    raise AdvisorError("agent-unknown", "此前导入的助手已删除")
                if old_skill is None:
                    raise AdvisorError("skill-unknown", "此前导入的技能已删除")
                return {"agent": copy.deepcopy(old_agent), "skill": copy.deepcopy(old_skill),
                        "created": False, "duplicate": True}
            skill = next((item for item in document["skills"] if item.get("package") is not None and
                          [item["package"]["source"], item["package"]["digest"]] == identity), None)
            imported_agent = next((item for item in document["agents"] if skill is not None and
                                   item.get("importedSkillId") == skill["id"] and skill["id"] in item["skillIds"]), None)
            created = imported_agent is None
            if created:
                if len(document["agents"]) >= MAX_AGENTS:
                    raise AdvisorError("config-full", "助手数量已达上限")
                agent_name = self._unique_import_name(agent["name"], document["agents"], MAX_AGENT_NAME)
                if skill is None:
                    if len(document["skills"]) >= MAX_SKILLS:
                        raise AdvisorError("config-full", "技能数量已达上限")
                    skill = {"id": new_identifier("skill", uuid.uuid4().hex), **skill_fields,
                             "builtin": False, "package": package}
                    skill["name"] = self._unique_import_name(skill["name"], document["skills"], MAX_SKILL_NAME)
                    document["skills"].append(skill)
                imported_agent = {
                    "id": new_identifier("agent", uuid.uuid4().hex), "name": agent_name,
                    "description": agent["description"], "prompt": agent["prompt"],
                    "welcome": agent["welcome"] if agent["welcome"] is not None else default_welcome(agent_name),
                    "skillIds": [skill["id"]], "importedSkillId": skill["id"],
                    "enabled": True, "builtin": False, "revision": 1,
                }
                document["agents"].append(imported_agent)
            if request_id is not None:
                document["importRequests"].append({"requestId": request_id, "fingerprint": fingerprint,
                                                   "agentId": imported_agent["id"], "skillId": skill["id"]})
                document["importRequests"] = document["importRequests"][-MAX_IMPORT_REQUESTS:]
            if created or request_id is not None:
                self._commit(document, cancel_event)
            return {"agent": copy.deepcopy(imported_agent), "skill": copy.deepcopy(skill),
                    "created": created, "duplicate": not created}

    def delete_skill(self, identifier):
        if not valid_identifier(identifier):
            raise AdvisorError("invalid-request", "技能编号不正确")
        with self.lock:
            document = copy.deepcopy(self._load())
            found = next((skill for skill in document["skills"] if skill["id"] == identifier), None)
            if found is None:
                raise AdvisorError("skill-unknown", "技能不存在")
            document["skills"] = [skill for skill in document["skills"] if skill["id"] != identifier]
            if identifier in builtin_skill_ids() and identifier not in document["deletedBuiltinSkillIds"]:
                document["deletedBuiltinSkillIds"].append(identifier)
            affected = []
            for agent in document["agents"]:
                if identifier in agent["skillIds"]:
                    agent["skillIds"] = [value for value in agent["skillIds"] if value != identifier]
                    agent["revision"] += 1
                    affected.append(agent["id"])
                if agent.get("importedSkillId") == identifier:
                    agent.pop("importedSkillId")
            self._commit(document)
            return {"deleted": identifier, "affectedAgentIds": affected}
