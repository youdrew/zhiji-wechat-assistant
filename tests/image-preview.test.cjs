// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const vm = require("node:vm");
const { createImagePreviews } = require("../scripts/image-preview.cjs");
const scope = { account: "test-account", user: "test-friend", id: "a".repeat(64) };

function harness() {
  const made = [];
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = handler => { this.popups = handler; };
      made.push(this);
    }
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    getBounds() { return { x: -1500, y: 80, width: 1180, height: 780 }; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    destroy() { this.destroyed = true; this.emit("closed"); }
    close() { this.destroy(); }
    loadURL(url) { this.url = url; return new Promise(resolve => { this.loaded = resolve; }); }
  }
  const main = new Window({}); main.webContents.session = {};
  const manager = createImagePreviews({ BrowserWindow: Window, mainWindow: main,
    clientUrl: "http://127.0.0.1:34567/", screen: {
      getDisplayMatching: () => ({ workArea: { x: -1600, y: 0, width: 1600, height: 900 } }),
    } });
  return { main, manager, made };
}

test("distinct images open isolated resizable native windows; repeated image restores its own window", () => {
  const h = harness();
  assert.equal(h.manager.open(scope), true);
  const first = h.made[1];
  const target = new URL(first.url), parsed = new URLSearchParams(target.hash.slice(1));
  assert.equal(target.origin, "http://127.0.0.1:34567");
  assert.equal(target.pathname, "/image-viewer.html");
  assert.equal(parsed.get("id"), scope.id);
  assert.equal(first.options.webPreferences.preload, undefined);
  assert.equal(first.options.webPreferences.nodeIntegration, false);
  assert.equal(first.options.webPreferences.sandbox, true);
  assert.equal(first.options.resizable, true);
  assert.equal(first.options.parent, undefined);
  assert.equal(first.options.webPreferences.session, h.main.webContents.session);
  assert.ok(first.options.x < 0);
  first.minimized = true;
  h.manager.open(scope);
  assert.equal(h.made.length, 2);
  assert.equal(first.minimized, false);
  assert.equal(first.focused, true);
  h.manager.open({ ...scope, id: "b".repeat(64) });
  assert.equal(h.made.length, 3);
  first.close(); h.manager.open(scope);
  assert.equal(h.made.length, 4);
});

test("untrusted identifiers never create a window or become a file/remote URL", () => {
  const h = harness();
  for (const value of [null, [], {}, { ...scope, id: "../../secrets" },
    { ...scope, id: "https://untrusted.invalid/" }, { ...scope, user: "" },
    { ...scope, account: "a\n" }, { ...scope, user: "a".repeat(4097) }])
    assert.equal(h.manager.open(value), false);
  assert.equal(h.made.length, 1);
  h.manager.open({ ...scope, user: "friend&account=another#value", url: "file:///secret" });
  assert.equal(new URLSearchParams(new URL(h.made[1].url).hash.slice(1)).get("user"), "friend&account=another#value");
});

test("viewer denies new windows, webviews and navigation and Escape closes only that viewer", () => {
  const h = harness(); h.manager.open(scope);
  const view = h.made[1];
  assert.deepEqual(view.popups({ url: "file:///secret" }), { action: "deny" });
  for (const event of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"]) {
    let blocked = false;
    view.webContents.emit(event, { preventDefault() { blocked = true; } }, "https://untrusted.invalid");
    assert.equal(blocked, true);
  }
  view.webContents.emit("before-input-event", { preventDefault() {} }, { type: "keyDown", key: "Escape" });
  assert.equal(view.destroyed, true);
  assert.equal(h.main.isDestroyed(), false);
});

test("scope reset, main close, crash and navigation close pending windows without later resurrection", async () => {
  for (const action of [h => h.manager.closeAll(), h => h.main.close(),
    h => h.main.webContents.emit("render-process-gone"),
    h => h.main.webContents.emit("did-start-navigation", {}, "http://127.0.0.1:34567/", false, true)]) {
    const h = harness(); h.manager.open(scope); h.manager.open({ ...scope, id: "b".repeat(64) });
    action(h);
    for (const view of h.made.slice(1)) {
      view.loaded(); await Promise.resolve(); view.emit("ready-to-show");
      assert.equal(view.destroyed, true);
      assert.equal(view.shown, undefined);
    }
  }
});

test("preload opens images only from an active user gesture in the main frame", async () => {
  const calls = []; let host;
  const window = {}; window.top = window;
  const navigator = { userActivation: { isActive: true } };
  vm.runInNewContext(fs.readFileSync(require.resolve("../scripts/real-client-preload.cjs"), "utf8"), {
    require: () => ({ contextBridge: { exposeInMainWorld(_name, api) { host = api; } },
      ipcRenderer: { on() {}, invoke(...args) { calls.push(args); return Promise.resolve(true); }, send(...args) { calls.push(args); } } }),
    window, navigator, process: { env: {} }, document: { addEventListener() {} },
  });
  assert.equal(await host.openImagePreview(scope), true);
  assert.deepEqual(calls[0], ["real-client:open-image-preview", scope, true]);
  navigator.userActivation.isActive = false;
  assert.equal(await host.openImagePreview(scope), false);
  host.closeImagePreviews();
  assert.deepEqual(calls[1], ["real-client:close-image-previews"]);
  window.top = {}; navigator.userActivation.isActive = true;
  assert.equal(await host.openImagePreview(scope), false);
  host.closeImagePreviews();
  assert.equal(calls.length, 2);
});
