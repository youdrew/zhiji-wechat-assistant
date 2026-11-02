"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Synthetic DOM tests of the MCP management page: pairing shows the credential once,
// the matrix grants through the directory's fingerprint, and the master switch stops everything.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = readFileSync(path.join(root, "ui/index.html"), "utf8");
const source = readFileSync(path.join(root, "ui/mcp.js"), "utf8");
const manifest = JSON.parse(readFileSync(path.join(root, "plugin.json"), "utf8"));
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for (let index = 0; index < 6; index++) await tick(); };

const STATUS = { coreReady: true, enabled: true, profiles: 2, enabledProfiles: 1, activeConnections: 1,
  guiAttached: true };
const PROFILE_FULL = { clientId: "mcp_full", name: "桌面客户端", enabled: true, mode: "full",
  globalManagement: false, authVersion: 1, createdAt: 1761500000000, lastUsedAt: 1761600000000, contactCount: 0 };
const PROFILE_MATRIX = { clientId: "mcp_matrix", name: "逐联系人客户端", enabled: true, mode: "per_contact",
  globalManagement: false, authVersion: 3, createdAt: 1761500000000, lastUsedAt: null, contactCount: 1 };
const DIRECTORY = { sourceFingerprint: "synthetic-install-fingerprint", accounts: [
  { accountId: "account-a", label: "账号甲", contacts: [
      { subjectId: "wxid_1", name: "好友一", isGroup: false },
      { subjectId: "wxid_2", name: "好友二", isGroup: false },
      { subjectId: "wxid_room", name: "一群", isGroup: true }] }] };
const CALLS = { calls: [
  { time: 1761600000000, clientId: "mcp_full", action: "zhiji_ping", accountId: null, subjects: [],
    resultCode: "ok", durationMs: 12 },
  { time: 1761600005000, clientId: "mcp_matrix", action: "zhiji_search_messages", accountId: "account-a",
    subjects: [], resultCode: "contact-not-authorized", durationMs: 1500 }] };

function node(tag = "div", id = "") {
  const value = { tag, id, children: [], listeners: {}, attributes: {}, className: "", textContent: "",
    value: "", hidden: false, disabled: false, checked: false, title: "", type: "", focused: false, parent: null };
  value.addEventListener = (name, handler) => { (value.listeners[name] ||= []).push(handler); };
  value.appendChild = child => { child.parent = value; value.children.push(child); return child; };
  value.replaceChildren = (...children) => { value.children = children; for (const child of children) child.parent = value; };
  value.setAttribute = (name, entry) => { value.attributes[name] = entry; };
  value.removeAttribute = name => { delete value.attributes[name]; };
  value.focus = () => { value.focused = true; };
  value.remove = () => { if (value.parent) value.parent.children = value.parent.children.filter(item => item !== value); };
  value.click = () => { for (const handler of value.listeners.click || []) handler({ preventDefault() {} }); };
  value.classList = {
    toggle(name, force) {
      const set = new Set(String(value.className).split(/\s+/).filter(Boolean));
      const active = force === undefined ? !set.has(name) : force;
      if (active) set.add(name); else set.delete(name);
      value.className = [...set].join(" ");
      return active;
    },
  };
  return value;
}

function environment(options = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], node("main", match[1])]));
  const body = node("body");
  const calls = [], hostCalls = [];
  let timerId = 0;
  const timers = new Map();
  const env = { elements, body, calls, hostCalls, timers, rpc: options.rpc };
  const kyfHost = {
    rpc(method, params) {
      calls.push({ method, params: JSON.parse(JSON.stringify(params || {})) });
      return Promise.resolve().then(() => {
        const custom = env.rpc ? env.rpc(method, params || {}) : undefined;
        if (custom !== undefined) return custom;
        if (method === "mcp.status") return JSON.parse(JSON.stringify(STATUS));
        if (method === "mcp.profiles") return { profiles: JSON.parse(JSON.stringify(
          env.profiles === undefined ? [PROFILE_FULL, PROFILE_MATRIX] : env.profiles)) };
        if (method === "mcp.calls") return JSON.parse(JSON.stringify(CALLS));
        if (method === "mcp.profile") return { profile: PROFILE_MATRIX, contacts: env.grants || [],
          sourceFingerprint: DIRECTORY.sourceFingerprint };
        if (method === "mcp.directory") return JSON.parse(JSON.stringify(env.directory === undefined ? DIRECTORY : env.directory));
        if (method === "mcp.settings") return { defaultMode: "per_contact" };
        if (method === "mcp.create_profile") return { profile: { clientId: "mcp_new", name: params.name,
          mode: params.mode, enabled: true, globalManagement: params.globalManagement, contactCount: 0 },
          credential: "zmc_synthetic-credential" };
        if (method === "mcp.grant_contact" || method === "mcp.revoke_contact") return { contacts: env.grants || [] };
        return {};
      });
    },
    host(method, params) { hostCalls.push({ method, params: JSON.parse(JSON.stringify(params || {})) }); return Promise.resolve({ copied: true }); },
    on() { return () => {}; },
    ready: Promise.resolve(),
    context: () => ({ theme: "dark", surface: "nav" }),
  };
  const document = { getElementById: id => elements.get(id), createElement: tag => node(tag), body };
  vm.runInNewContext(source, { window: { document, kyfHost }, document, kyfHost, JSON, Date,
    setTimeout: handler => { const id = ++timerId; timers.set(id, handler); return id; }, clearTimeout: id => timers.delete(id) });
  const text = item => item === undefined ? "" : item.textContent + item.children.map(text).join("");
  const find = (item, predicate) => {
    if (predicate(item)) return item;
    for (const child of item.children) { const found = find(child, predicate); if (found) return found; }
    return null;
  };
  env.get = id => elements.get(id) || [...body.children, ...elements.values()]
    .map(item => find(item, candidate => candidate.id === id)).find(Boolean);
  env.click = async id => { const item = env.get(id); assert.ok(item, id); assert.equal(item.disabled, false); item.click(); await settle(); };
  env.overlay = () => body.children.filter(item => item.className === "mcp-overlay").at(-1);
  env.dialogButton = async label => {
    const overlay = env.overlay();
    assert.ok(overlay, `expected a dialog for ${label}`);
    const target = find(overlay, item => item.tag === "button" && item.textContent === label);
    assert.ok(target, `expected a ${label} button`);
    target.click();
    await settle();
    return target;
  };
  env.allText = () => text(body) + [...elements.values()].map(text).join("");
  return env;
}

test("the manifest exposes exactly one admin nav entry and a settings link to it", () => {
  assert.equal(manifest.id, "kyf.mcp");
  assert.deepEqual(manifest.contributes.nav, [{ id: "mcp-admin", label: "MCP 管理", page: "ui/index.html", icon: "generic" }]);
  assert.deepEqual(manifest.contributes.settings.links, [{ label: "打开 MCP 管理", nav: "mcp-admin" }]);
  assert.equal(manifest.contributes.settings.groups[0].fields[0].default, "full");
  assert.doesNotMatch(source, /\bfetch\(|localStorage|sessionStorage|indexedDB|\.innerHTML/);
  assert.equal([...html.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]).join(","),
    "/plugin-sdk/web/kyf-host.js,mcp.js");
  // The centralized safety page carries the three §4.1/§5 explanations in one place.
  for (const phrase of ["数据流向", "提示注入", "Windows 用户", "本机确认"]) {
    assert.ok(html.includes(phrase), phrase);
  }
  assert.ok(source.includes("我已保存，不再显示"), "the credential dialog warns it will not show again");
});

test("lists profiles, call records and the core status without any credential", async () => {
  const env = environment(); await settle();
  assert.equal(env.get("statusLine").textContent,
    "Core 就绪 · 主窗口运行中 · 1/2 个客户端启用 · 1 个活动连接");
  assert.equal(env.get("masterSwitch").attributes["aria-checked"], "true");
  assert.equal(env.get("btnCreate").disabled, false);
  const list = env.get("profileList");
  assert.ok(text(list).includes("桌面客户端") && text(list).includes("逐联系人客户端"));
  assert.ok(text(list).includes("已授权联系人 1"));
  assert.equal(env.get("callsTable").hidden, false);
  const rows = env.get("callsBody").children;
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children[4].textContent, "成功");
  assert.equal(rows[1].children[4].textContent, "contact-not-authorized");
  assert.equal(rows[1].children[4].className, "mcp-code mcp-code-error");
  assert.equal(env.get("callsBody").children[0].children[1].textContent, "桌面客户端", "calls show the paired name");
  // The credential exists nowhere in the document, not even after a poll.
  assert.ok(!env.allText().includes("zmc_"));
});

test("disabling the master switch asks once, then reports and stops everything", async () => {
  let masterEnabled = true;
  const env = environment({ rpc(method, params) {
    if (method === "mcp.set_master") { masterEnabled = params.enabled; return { enabled: params.enabled, disconnected: 1 }; }
    if (method === "mcp.status") return { ...STATUS, enabled: masterEnabled };
    return undefined;
  } });
  await settle();
  await env.click("masterSwitch");
  await env.dialogButton("关闭");
  const request = env.calls.find(call => call.method === "mcp.set_master");
  assert.deepEqual(request.params, { enabled: false });
  assert.equal(env.get("statusLine").textContent, "MCP 已关闭");
  assert.equal(env.get("masterOffBanner").hidden, false);
  assert.equal(env.get("btnCreate").disabled, true, "no pairing while the switch is off");
});

test("pairing shows the credential exactly once and never renders it again", async () => {
  const env = environment(); await settle();
  await env.click("btnCreate");
  const input = find(env.overlay(), item => item.tag === "input" && item.type === "text");
  assert.ok(input, "pairing dialog has a name input");
  input.value = "新客户端";
  // The default mode comes from the plugin's pairing setting.
  const mode = find(env.overlay(), item => item.tag === "select");
  assert.equal(mode.value, "per_contact", "per_contact is preselected");
  await env.dialogButton("创建并生成凭据");
  const create = env.calls.find(call => call.method === "mcp.create_profile");
  assert.deepEqual(create.params, { name: "新客户端", mode: "per_contact", globalManagement: false });
  const secret = env.allText();
  assert.ok(secret.includes("zmc_synthetic-credential"), "the credential is shown once");
  assert.ok(secret.includes("安全与数据说明"), "the pairing flow keeps one safety-page entry");
  await env.dialogButton("我已保存，不再显示");
  assert.ok(!env.allText().includes("zmc_synthetic-credential"), "the credential never renders again");
  assert.ok(!env.overlay(), "the dialog is gone after saving");
});

test("the per-contact matrix grants through the directory fingerprint only", async () => {
  const env = environment(); await settle();
  await env.click("btnRefresh"); // no-op on an already loaded directory
  const card = env.get("profileList").children[1];
  const grantButton = find(card, item => item.tag === "button" && item.textContent === "授权联系人");
  grantButton.click(); await settle();
  assert.ok(env.calls.some(call => call.method === "mcp.directory"), "the matrix reads the directory");
  assert.ok(env.calls.some(call => call.method === "mcp.profile"), "and the profile's own grants");
  const matrix = find(env.get("profileList"), item => item.className === "mcp-matrix");
  assert.ok(text(matrix).includes("好友一") && text(matrix).includes("一群（群聊）"));
  const target = collect(matrix)[0];
  assert.equal(target.checked, false);
  target.checked = true;
  for (const handler of target.listeners.change) handler({});
  await settle();
  const grant = env.calls.filter(call => call.method === "mcp.grant_contact").at(-1);
  assert.deepEqual(grant.params, { clientId: "mcp_matrix", sourceFingerprint: "synthetic-install-fingerprint",
    accountId: "account-a", subjectType: "wechat_contact", subjectId: "wxid_1" });
  // A page-side failure keeps the checkbox honest instead of faking success.
  const failing = environment({ rpc(method) { if (method === "mcp.grant_contact") throw Object.assign(new Error("拒绝"), { message: "拒绝" });
    return undefined; } });
  await settle();
  await failing.click("btnRefresh");
  const failingCard = failing.get("profileList").children[1];
  find(failingCard, item => item.tag === "button" && item.textContent === "授权联系人").click();
  await settle();
  const failingBox = collect(find(failing.get("profileList"), item => item.className === "mcp-matrix"))[0];
  failingBox.checked = true;
  for (const handler of failingBox.listeners.change) handler({});
  await settle();
  assert.equal(failingBox.checked, false, "a refused grant reverts the checkbox");
});

function collect(rootItem) {
  const boxes = [];
  const walk = item => { if (item.tag === "input" && item.type === "checkbox") boxes.push(item);
    item.children.forEach(walk); };
  walk(rootItem);
  return boxes;
}

test("the safety page is a separate view with one entry and a way back", async () => {
  const env = environment(); await settle();
  await env.click("btnSafety");
  assert.equal(env.get("viewAdmin").hidden, true);
  assert.equal(env.get("viewSafety").hidden, false);
  await env.click("btnSafetyBack");
  assert.equal(env.get("viewAdmin").hidden, false);
  assert.equal(env.get("viewSafety").hidden, true);
});

test("revocation asks and then removes the client", async () => {
  const env = environment(); await settle();
  const card = env.get("profileList").children[0];
  find(card, item => item.tag === "button" && item.textContent === "撤销").click();
  await env.dialogButton("撤销");
  assert.ok(env.calls.some(call => call.method === "mcp.revoke" && call.params.clientId === "mcp_full"));
  assert.ok(!env.overlay(), "no dialog stays open after revoking");
});

function find(item, predicate) {
  if (predicate(item)) return item;
  for (const child of item.children) { const found = find(child, predicate); if (found) return found; }
  return null;
}
function text(item) { return item === undefined ? "" : item.textContent + item.children.map(text).join(""); }
