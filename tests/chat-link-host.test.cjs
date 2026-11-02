// Copyright 2026 Zhiji contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { chatLinkUrl, attachChatLinks } = require("../scripts/chat-link-host.cjs");

const invalidUrls = [null, {}, "", "https://", "javascript:alert(1)", "file:///C:/example.txt",
  "weixin://example", "data:text/html,example", "//example.invalid", "https://user:secret@example.invalid/",
  "https://user@example.invalid/", " https://example.invalid/", "https://example.invalid/a b",
  "https://example.invalid/\npath", "https://example.invalid/\u0000path", "https://example.invalid/\u007fpath",
  "https://example.invalid\\@other.invalid/", "https://example.invalid/" + "a".repeat(8192)];

test("chat link validation accepts only credential-free explicit web URLs", () => {
  for (const url of invalidUrls) assert.equal(chatLinkUrl(url), null, String(url));
  assert.equal(chatLinkUrl("https://example.invalid/notes?id=123&from=share#details"),
    "https://example.invalid/notes?id=123&from=share#details");
  assert.equal(chatLinkUrl("http://example.invalid:8080/"), "http://example.invalid:8080/");
  assert.equal(chatLinkUrl("HTTPS://EXAMPLE.INVALID/测试"), "https://example.invalid/%E6%B5%8B%E8%AF%95");
});

test("only active trusted main chat requests can open the external browser", async () => {
  const handlers = new Map(), trusted = {}, opened = [];
  let available = true;
  attachChatLinks({ ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
    trustedMainFrame: event => event === trusted, available: () => available,
    shell: { openExternal: async target => { opened.push(target); } } });
  const open = handlers.get("real-client:open-chat-link"), url = "https://example.invalid/notes?x=1&y=2";
  assert.equal(await open({}, url, true), false);
  assert.equal(await open(trusted, url, false), false);
  assert.equal(await open(trusted, url, "true"), false);
  for (const invalid of invalidUrls) assert.equal(await open(trusted, invalid, true), false);
  available = false;
  assert.equal(await open(trusted, url, true), false);
  assert.deepEqual(opened, []);
  available = true;
  assert.equal(await open(trusted, url, true), true);
  assert.deepEqual(opened, [url]);
});

test("external open failure returns a retryable false result", async () => {
  let open;
  attachChatLinks({ ipcMain: { handle: (_name, fn) => { open = fn; } }, trustedMainFrame: () => true,
    shell: { openExternal: async () => { throw new Error("synthetic"); } } });
  assert.equal(await open({}, "https://example.invalid/", true), false);
});

function preload({ active = true, subframe = false, settings = false } = {}) {
  let host;
  const calls = [], window = {}, navigator = { userActivation: { isActive: active } };
  window.top = subframe ? {} : window;
  vm.runInNewContext(readFileSync(path.join(__dirname, "../scripts/real-client-preload.cjs"), "utf8"), {
    window, navigator, URL, document: { addEventListener() {} },
    process: { argv: settings ? ["--kyf-settings-window"] : [], env: {} },
    require: name => { assert.equal(name, "electron"); return {
      contextBridge: { exposeInMainWorld: (_name, value) => { host = value; } },
      ipcRenderer: { on() {}, invoke: (...args) => { calls.push(args); return Promise.resolve(true); } },
    }; },
  });
  return { host, calls, navigator };
}

test("preload permits web links only from an activated main chat page", async () => {
  const h = preload();
  for (const invalid of invalidUrls) assert.equal(await h.host.openChatLink(invalid), false);
  assert.deepEqual(h.calls, []);
  assert.equal(await h.host.openChatLink("https://example.invalid/notes?x=1&y=2"), true);
  assert.deepEqual(h.calls, [["real-client:open-chat-link", "https://example.invalid/notes?x=1&y=2", true]]);
  h.navigator.userActivation.isActive = false;
  assert.equal(await h.host.openChatLink("https://example.invalid/"), false);
  assert.equal(h.calls.length, 1);
  for (const options of [{ subframe: true }, { settings: true }, { active: false }]) {
    const blocked = preload(options);
    assert.equal(await blocked.host.openChatLink("https://example.invalid/"), false);
    assert.deepEqual(blocked.calls, []);
  }
});
