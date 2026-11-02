"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const { test } = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const code = fs.readFileSync(path.join(__dirname, "../chatui/self-analysis-window.js"), "utf8");
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const listing = (changes = {}) => ({ ok: true, json: async () => ({ plugins: [{ id: "kyf.self-analysis", enabled: true, ...changes }] }) });
function fixture() {
  let receive; const frames = [], requests = [], calls = [], elements = [], events = {};
  const container = { children: [], replaceChildren(...nodes) { this.children = nodes; } };
  const window = {
    addEventListener(name, callback) { events[name] = callback; },
    selfAnalysisHost: {
      onMessage(callback) { receive = callback; }, getContext: () => new Promise(() => {}),
      async hostCall(...args) { calls.push(args); return { result: { configured: true } }; },
      async copy() { return true; },
    },
    KyfFrames: {
      create(options) {
        const frame = { options, destroyed: false, post(value) { this.context = value; }, event(value) { this.latestEvent = value; }, destroy() { this.destroyed = true; } };
        frames.push(frame); return frame;
      },
      rpc: async (...args) => { requests.push(args); return { sources: [] }; },
    },
  };
  const document = { body: { dataset: {} }, getElementById: () => container,
    createElement(tag) { const item = { tag, children: [], handlers: {}, appendChild(value) { this.children.push(value); },
      addEventListener(name, callback) { this.handlers[name] = callback; } }; elements.push(item); return item; } };
  const context = { window, document, fetch: async () => listing(), AbortController, setTimeout, clearTimeout };
  vm.createContext(context); vm.runInContext(code, context);
  const send = (account, epoch, enabled = true, event) => receive({ context: { account, theme: "light" }, epoch, enabled, event });
  return { send, context, window, document, frames, requests, calls, elements, container, events };
}

test("host mounts a fixed plugin with no contact and keeps same-account theme updates in place", async () => {
  const f = fixture(); f.send("a", 1); await settle(); const frame = f.frames[0];
  assert.equal(frame.options.plugin, "kyf.self-analysis"); assert.equal(frame.options.page, "ui/index.html");
  assert.equal(frame.options.context.user, ""); assert.equal(frame.options.context.surface, "self-analysis-window");
  await frame.options.rpc("kyf.self-analysis", "self.status", {}, { account: "a", user: "forged-contact" });
  assert.equal(f.requests[0][3].user, "");
  f.send("a", 1); await settle(); assert.equal(f.frames.length, 1); assert.equal(frame.destroyed, false);
  await frame.options.host["settings.open"]({ section: "model" });
  assert.equal(f.calls[0][0], "settings.open"); assert.equal(f.calls[0][2].epoch, 1);
});

test("switches destroy old frames and reject old host calls and late Core results", async () => {
  const f = fixture(); f.send("a", 1); await settle(); const old = f.frames[0];
  let release; f.window.KyfFrames.rpc = () => new Promise(resolve => { release = resolve; });
  const pending = old.options.rpc("kyf.self-analysis", "self.search", {}, { account: "a" });
  f.send("b", 2); await settle(); assert.equal(old.destroyed, true);
  release({ sources: ["old-account"] }); await assert.rejects(pending, error => error.code === "context-changed");
  assert.throws(() => old.options.host["self-profile.save"]({ account: "b" }), error => error.code === "context-changed");
  await assert.rejects(old.options.host["clipboard.copy"]({ text: "old" }), error => error.code === "context-changed");
  f.send("a", 1); await settle(); assert.equal(f.frames.at(-1).options.context.account, "b", "late initial context cannot roll back the current account");
});

test("disabling removes content immediately and a late plugin listing cannot mount it again", async () => {
  const f = fixture(); f.send("a", 1); await settle(); const frame = f.frames[0];
  let release, pendingSignal; f.context.fetch = (_url, options) => new Promise(resolve => { release = resolve; pendingSignal = options.signal; });
  f.send("a", 2); await settle();
  f.send("a", 3, false, { type: "plugins-changed", enabled: false });
  assert.equal(pendingSignal.aborted, true, "scope invalidation cancels the pending host listing request");
  assert.equal(frame.destroyed, true); assert.equal(f.container.children[0].children[0].textContent.includes("已停用"), true);
  release(listing()); await settle(); assert.equal(f.frames.length, 1);
});

test("empty accounts retain settings/model navigation; timeout and service recovery can remount", async () => {
  const f = fixture(); f.send("", 1); await settle();
  assert.equal(f.frames.length, 1);
  await f.frames[0].options.host["model.status"]({}); assert.equal(f.calls.length, 1);
  await assert.rejects(f.frames[0].options.rpc("kyf.self-analysis", "self.status", {}, { account: "" }), error => error.code === "context-changed");
  f.frames[0].options.onTimeout(); assert.equal(f.frames[0].destroyed, true);
  const retry = f.elements.find(item => item.tag === "button"); retry.handlers.click(); await settle();
  assert.equal(f.frames.length, 2);
  f.send("a", 2, true, { type: "service-restored" }); await settle(); assert.equal(f.frames.length, 3);
  f.events.pagehide(); assert.equal(f.frames[2].destroyed, true);
});

test("an enabled plugin with unmet dependencies cannot mount until its dependencies recover", async () => {
  const f = fixture();
  f.context.fetch = async () => listing({ dependencyError: { code: "plugin-dependency-disabled", message: "依赖插件 dev.provider 已停用" } });
  f.send("a", 1); await settle();
  assert.equal(f.frames.length, 0); assert.equal(f.requests.length, 0);
  assert.match(f.container.children[0].children[0].textContent, /依赖不可用/);
  f.context.fetch = async () => listing();
  f.elements.find(item => item.tag === "button").handlers.click(); await settle();
  assert.equal(f.frames.length, 1); assert.equal(f.frames[0].options.context.account, "a");
});

test("the standalone HTML keeps the opaque plugin sandbox helper and restrictive host CSP", () => {
  const html = fs.readFileSync(path.join(__dirname, "../chatui/self-analysis.html"), "utf8");
  assert.match(html, /script-src 'self'/); assert.match(html, /frame-src 'self'/); assert.match(html, /object-src 'none'/);
  assert.match(html, /plugin-frame\.js/); assert.match(html, /self-analysis-window\.js/);
  assert.doesNotMatch(html, /allow-same-origin|nodeIntegration|<script>[^<]/);
});
