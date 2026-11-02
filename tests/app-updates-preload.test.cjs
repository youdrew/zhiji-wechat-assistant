"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../scripts/real-client-preload.cjs"), "utf8");

function preload({ argv, environment = {}, active = true, subframe = false, clock = () => Date.now() } = {}) {
  const calls = [], sent = [], dispatched = [], listeners = new Map(), documentListeners = new Map();
  let host;
  const window = { dispatchEvent: event => dispatched.push(event) };
  window.top = subframe ? {} : window;
  const navigator = { userActivation: { isActive: active } };
  class Element {
    constructor(href) { this.href = href; }
    closest(selector) { assert.equal(selector, "a[href]"); return this; }
  }
  vm.runInNewContext(source, {
    require(name) {
      assert.equal(name, "electron");
      return {
        contextBridge: { exposeInMainWorld(name, value) { assert.equal(name, "desktopHost"); host = value; } },
        ipcRenderer: {
          on: (name, callback) => listeners.set(name, callback),
          invoke: async (...args) => { calls.push(args); return { phase: "synthetic" }; },
          send: (...args) => sent.push(args), removeListener() {},
        },
      };
    },
    process: { env: environment, ...(argv === undefined ? {} : { argv }) },
    document: { addEventListener: (name, callback) => documentListeners.set(name, callback) },
    window, navigator, Element, URL, Date: class extends Date { static now() { return clock(); } },
    Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
  });
  return {
    host, calls, sent, dispatched, listeners, navigator,
    clickLink(href, { trusted = true, button = 0 } = {}) {
      const result = { prevented: false, stopped: false };
      documentListeners.get("click")({
        target: new Element(href), isTrusted: trusted, button,
        preventDefault() { result.prevented = true; },
        stopImmediatePropagation() { result.stopped = true; },
      });
      return result;
    },
  };
}

test("About links open through desktop IPC only from activated trusted main-frame clicks", () => {
  const destinations = [
    "https://github.com/youdrew",
    "https://github.com/youdrew/zhiji-wechat-assistant",
    "https://eugenepage.com/",
    "mailto:youdrewxuan@icloud.com",
    "https://github.com/tswawa/WechatVibe",
    "https://github.com/tswawa",
  ];
  const h = preload();
  for (const href of destinations) {
    assert.deepEqual(h.clickLink(href), { prevented: true, stopped: true });
    assert.deepEqual(h.sent.at(-1), ["real-client:open-doc", href, true, true]);
  }
  assert.equal(h.sent.length, destinations.length);

  for (const href of ["https://example.com/", "https://github.com/youdrew/other-repository",
    "https://github.com/youdrew/know-your-friends",
    "https://space.bilibili.com/29291963", "http://eugenepage.com/", "https://eugenepage.com.evil.example/",
    "https://eugenepage.com/private", "https://eugenepage.com/?redirect=https://example.com/",
    "https://synthetic:secret@eugenepage.com/", "mailto:other@example.com",
    "mailto:youdrewxuan@icloud.com?subject=Hello", "mailto:youdrewxuan@icloud.com?attach=file:///C:/private.txt",
    "mailto:youdrewxuan@icloud.com%0d%0aBcc:other@example.com", "mailto://youdrewxuan@icloud.com",
    "mailto:youdrewxuan@icloud.com,other@example.com", "file:///C:/private.txt", "javascript:alert(1)",
    "https://synthetic:secret@github.com/youdrew"]) {
    assert.deepEqual(h.clickLink(href), { prevented: false, stopped: false });
  }
  for (const href of destinations) {
    assert.deepEqual(h.clickLink(href, { trusted: false }), { prevented: false, stopped: false });
    assert.deepEqual(h.clickLink(href, { button: 1 }), { prevented: false, stopped: false });
  }
  assert.equal(h.sent.length, destinations.length);

  for (const options of [{ active: false }, { subframe: true }]) {
    const blocked = preload(options);
    for (const href of destinations)
      assert.deepEqual(blocked.clickLink(href), { prevented: false, stopped: false });
    assert.equal(blocked.sent.length, 0);
  }
});

test("verified reply copy retains one text-bound click after activation expires", async () => {
  let now = 1000;
  const h = preload({ clock: () => now });
  const token = h.host.reserveCopyDraft("synthetic reply");
  h.navigator.userActivation.isActive = false;
  now += 6000;
  await h.host.copyDraft("synthetic reply", token);
  assert.deepEqual(h.calls, [["real-client:copy-draft", "synthetic reply"]]);
  assert.equal(await h.host.copyDraft("synthetic reply", token), false);
  assert.equal(await h.host.copyDraft("synthetic reply"), false);
});


test("copy reservations reject expiry, substitution, reuse, subframes and background creation", async () => {
  let now = 1000;
  const h = preload({ clock: () => now });
  let token = h.host.reserveCopyDraft("original");
  assert.equal(await h.host.copyDraft("substitution", token), false);
  assert.equal(await h.host.copyDraft("original", token), false);
  token = h.host.reserveCopyDraft("original");
  now += 60000;
  assert.equal(await h.host.copyDraft("original", token), false);
  token = h.host.reserveCopyDraft("original");
  h.host.releaseCopyDraft(token);
  assert.equal(await h.host.copyDraft("original", token), false);
  const superseded = h.host.reserveCopyDraft("original");
  token = h.host.reserveCopyDraft("new reply");
  h.host.releaseCopyDraft(superseded);
  await h.host.copyDraft("new reply", token);
  assert.deepEqual(h.calls, [["real-client:copy-draft", "new reply"]]);
  for (const options of [{ active: false }, { subframe: true }]) {
    const blocked = preload(options);
    assert.equal(blocked.host.reserveCopyDraft("reply"), null);
    assert.equal(await blocked.host.copyDraft("reply", 1), false);
    assert.equal(blocked.calls.length, 0);
  }
});

test("development preload disables every update action without IPC", async () => {
  for (const argv of [undefined, [], ["--unrelated-flag"]]) {
    const h = preload({ argv, environment: { KYF_UPDATE_VALIDATE: "1",
      KYF_UPDATE_FINAL_READY_FILE: "/synthetic-ready", KYF_UPDATE_FINAL_READY_NONCE: "synthetic-nonce" } });
    assert.equal(h.host.appUpdatesEnabled, false);
    assert.equal(h.host.updateValidationMode, false);
    assert.equal(h.host.updateFinalReadyMode, false);
    assert.equal((await h.host.checkForUpdates()).status, "disabled");
    for (const name of ["getUpdateState", "beginUpdate", "rollbackUpdate"])
      assert.equal((await h.host[name]()).phase, "disabled");
    assert.equal(await h.host.reportUiReady(), false);
    assert.equal(h.calls.length, 0);
    h.listeners.get("real-client:update-state")({}, { phase: "available" });
    assert.equal(h.dispatched.length, 0);
    assert.equal(Object.isFrozen(h.host), true);
    assert.throws(() => { h.host.appUpdatesEnabled = true; }, TypeError);
    await h.host.getAppVersion();

    assert.equal(h.host.downloadLayaModel, undefined);
    assert.deepEqual(h.calls.map(call => call[0]), ["real-client:app-version"]);
  }
});

test("packaged preload routes updates and keeps user activation checks", async () => {
  const h = preload({ argv: ["--kyf-app-updates-enabled"] });
  assert.equal(h.host.appUpdatesEnabled, true);
  for (const name of ["checkForUpdates", "getUpdateState", "beginUpdate", "rollbackUpdate"])
    await h.host[name]();
  assert.deepEqual(h.calls.map(call => call[0]), ["real-client:check-updates", "real-client:update-state",
    "real-client:begin-update", "real-client:rollback-update"]);
  h.listeners.get("real-client:update-state")({}, { phase: "available" });
  assert.equal(h.dispatched.length, 1);
  assert.equal(h.dispatched[0].type, "kyf-update-state");
  h.navigator.userActivation.isActive = false;
  assert.equal((await h.host.beginUpdate()).phase, "blocked");
  assert.equal((await h.host.rollbackUpdate()).phase, "blocked");
  assert.equal(h.calls.length, 4);
});

test("packaged subframes cannot invoke update actions", async () => {
  const h = preload({ argv: ["--kyf-app-updates-enabled"], subframe: true });
  assert.equal((await h.host.checkForUpdates()).status, "blocked");
  for (const name of ["getUpdateState", "beginUpdate", "rollbackUpdate"])
    assert.equal((await h.host[name]()).phase, "blocked");
  assert.equal(h.calls.length, 0);
});

test("packaged update validation still reports UI readiness", async () => {
  for (const environment of [{ KYF_UPDATE_VALIDATE: "1" },
    { KYF_UPDATE_FINAL_READY_FILE: "/synthetic-ready", KYF_UPDATE_FINAL_READY_NONCE: "synthetic-nonce" }]) {
    const h = preload({ argv: ["--kyf-app-updates-enabled"], environment });
    assert.equal(h.host.updateValidationMode || h.host.updateFinalReadyMode, true);
    await h.host.reportUiReady();
    assert.deepEqual(h.calls.map(call => call[0]), ["real-client:update-ui-ready"]);
  }
});
