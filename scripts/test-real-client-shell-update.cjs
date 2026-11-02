"use strict";

// Exercise shell IPC registration with synthetic Electron objects; no window or bridge is opened.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function exerciseShell({ packaged, selfTest, updateEnvironment = {},
  packageJson = '{"kyfUpdatesEnabled":true}', metadataUnreadable = false, metadataExpectedEnabled = true,
  extraArgs = [] }) {
  const handles = new Map();
  const listeners = new Map(), openedLinks = [];
  const frames = { mainFrame: { url: "http://127.0.0.1:34567/" } };
  const contents = {
    mainFrame: frames.mainFrame,
    on() {}, once() {}, setWindowOpenHandler() {}, loadURL() {},
  };
  const window = { webContents: contents, isDestroyed: () => false, on() {} };
  const calls = { update: 0, stage: 0, rollback: 0, controllerCreated: 0, monitor: 0, fallback: 0, proxyCreated: 0,
    metadataRead: 0 };
  const updatesEnabled = packaged && metadataExpectedEnabled;
  const environment = selfTest ? {} : { KYF_INSTANCE_ID: "a".repeat(64), ...updateEnvironment };
  const originalEnvironment = { ...environment };
  const startupErrors = [];
  let windowOptions;
  const proxyFetch = async () => ({ ok: true });
  const app = {
    isPackaged: packaged,
    setAppUserModelId() {}, setPath() {}, requestSingleInstanceLock: () => true,
    commandLine: { appendSwitch() {} },
    on() {}, whenReady: () => Promise.resolve(), getVersion: () => "1.0.1",
    quit() {}, exit(code) { startupErrors.push(code); },
  };
  const electron = {
    app, BrowserWindow: class { constructor(options) { windowOptions = options; return window; } }, clipboard: {},
    dialog: { showErrorBox(_title, error) { startupErrors.push(error); } },
    ipcMain: { on: (name, callback) => listeners.set(name, callback), handle: (name, callback) => handles.set(name, callback) },
    session: { defaultSession: {
      setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {},
      resolveProxy: async () => "DIRECT",
      webRequest: { onBeforeRequest() {} },
    } }, shell: { openExternal: href => openedLinks.push(href) },
  };
  const source = fs.readFileSync(path.join(__dirname, "real-client-shell.cjs"), "utf8");
  vm.runInNewContext(source, {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:fs") return { mkdirSync() {}, readFileSync(file, encoding) {
        assert.equal(path.resolve(file), path.resolve(__dirname, "..", "package.json"), "read this application's package metadata");
        assert.equal(encoding, "utf8");
        calls.metadataRead++;
        if (metadataUnreadable) throw new Error("synthetic unavailable package metadata");
        return packageJson;
      } };
      if (name === "node:path") return path;
      if (name === "node:child_process") return { execFile() { throw new Error("self-test must not stop a bridge"); } };
      if (name === "./real-client-recovery.cjs") return { monitorBridge() { calls.monitor++; return () => {}; } };
      if (name === "./model-traces-host.cjs") return { attachModelTraces: () => ({ close() {} }), readControlToken: () => null };
      if (name === "./chat-link-host.cjs") return require("./chat-link-host.cjs");
      if (name === "./real-client-update-controller.cjs") return {
        createUpdateController(options) {
          calls.controllerCreated++;
          assert.equal(updatesEnabled, true, "only an explicitly enabled packaged application creates an update controller");
          return {
            check: () => options.checkImpl(app.getVersion()),
            getState: () => ({ phase: "idle" }),
            begin: () => options.stageImpl(app.getVersion(), "/synthetic-install", () => {}),
            rollback: () => { calls.rollback++; return { phase: "rolled_back" }; },
          };
        },
      };
      if (name === "./real-client-update.cjs") return {
        RELEASES_URL: "https://github.com/youdrew/zhiji-wechat-assistant/releases",
        checkForUpdates: async (version, options) => {
          calls.update++;
          assert.equal(version, "1.0.1");
          assert.equal(options.fetchImpl, proxyFetch);
          return { status: calls.update === 1 ? "offline" : "current" };
        },
        downloadAndStageUpdate: async () => { calls.stage++; return { phase: "ready" }; },
        errorStatus: () => "server-error",
      };
      if (name === "./real-client-update-proxy.cjs") return {
        createUpdateProxyFetch: ({ session, ProxyAgent }) => {
          assert.equal(session, electron.session.defaultSession);
          assert.equal(ProxyAgent.name, "FakeProxyAgent");
          calls.proxyCreated++;
          return { fetchImpl: proxyFetch, enableSavedLoopbackFallback: async () => {
            calls.fallback++;
            return true;
          } };
        },
      };
      if (name.endsWith(path.join("node_modules", "undici"))) return {
        ProxyAgent: class FakeProxyAgent {},
      };
      throw new Error(`Unexpected require: ${name}`);
    },
    __dirname, URL, process: {
      platform: "win32", env: environment,
      argv: ["electron", "shell", "--client-url", frames.mainFrame.url, ...(selfTest ? ["--self-test"] : []), ...extraArgs],
      stderr: { write() {} },
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(startupErrors, []);
  assert.deepEqual(Array.from(windowOptions.webPreferences.additionalArguments),
    updatesEnabled ? ["--kyf-app-updates-enabled"] : []);
  assert.equal(calls.metadataRead, packaged ? 1 : 0);
  const trusted = { sender: contents, senderFrame: frames.mainFrame };
  const subframe = { sender: contents, senderFrame: { url: frames.mainFrame.url } };
  const otherWindow = { sender: {}, senderFrame: frames.mainFrame };
  const openDoc = listeners.get("real-client:open-doc");
  const aboutDestinations = [
    "https://github.com/youdrew",
    "https://github.com/youdrew/zhiji-wechat-assistant",
    "https://eugenepage.com/",
    "mailto:youdrewxuan@icloud.com",
    "https://github.com/tswawa/WechatVibe",
    "https://github.com/tswawa",
  ];
  for (const href of aboutDestinations) openDoc(trusted, href, true, true);
  assert.deepEqual(openedLinks, selfTest ? [] : aboutDestinations);
  for (const href of aboutDestinations) {
    openDoc(subframe, href, true, true);
    openDoc(otherWindow, href, true, true);
    openDoc(trusted, href, false, true);
    openDoc(trusted, href, true, false);
  }
  for (const href of ["https://example.com/", "https://github.com/youdrew/other-repository",
    "https://github.com/youdrew/know-your-friends",
    "https://space.bilibili.com/29291963", "http://eugenepage.com/", "https://eugenepage.com.evil.example/",
    "https://eugenepage.com/private", "https://eugenepage.com/?redirect=https://example.com/",
    "https://synthetic:secret@eugenepage.com/", "mailto:other@example.com",
    "mailto:youdrewxuan@icloud.com?subject=Hello", "mailto:youdrewxuan@icloud.com?attach=file:///C:/private.txt",
    "mailto:youdrewxuan@icloud.com%0d%0aBcc:other@example.com", "mailto://youdrewxuan@icloud.com",
    "mailto:youdrewxuan@icloud.com,other@example.com", "file:///C:/private.txt", "javascript:alert(1)",
    "https://synthetic:secret@github.com/youdrew"])
    openDoc(trusted, href, true, true);
  assert.deepEqual(openedLinks, selfTest ? [] : aboutDestinations,
    "About links require an exact permitted URL and a trusted main-frame user action");
  const openChatLink = handles.get("real-client:open-chat-link");
  assert.equal(await openChatLink(subframe, "https://example.invalid/share", true), false);
  assert.equal(await openChatLink(otherWindow, "https://example.invalid/share", true), false);
  assert.equal(await openChatLink(trusted, "https://example.invalid/share", false), false);
  assert.equal(await openChatLink(trusted, "javascript:alert(1)", true), false);
  assert.equal(await openChatLink(trusted, "https://example.invalid/share", true), !selfTest);
  assert.deepEqual(openedLinks, selfTest ? [] : [...aboutDestinations, "https://example.invalid/share"]);
  assert.equal(handles.get("real-client:open-image-preview")(subframe, {}, true), false);
  assert.equal(handles.get("real-client:open-image-preview")(otherWindow, {}, true), false);
  assert.equal(handles.get("real-client:open-image-preview")(trusted, {}, false), false);
  assert.equal(handles.get("real-client:app-version")(trusted), "1.0.1");
  assert.equal(handles.get("real-client:app-version")(subframe), null);
  assert.equal(handles.get("real-client:app-version")(otherWindow), null);
  // Modified 2026-10-13: plugin pages ask the host for a file; only the trusted main frame with a gesture may.
  assert.equal(await handles.get("real-client:choose-plugin-file")(subframe, "kyf.advisor", "file", true), null);
  assert.equal(await handles.get("real-client:choose-plugin-file")(otherWindow, "kyf.advisor", "directory", true), null);
  assert.equal(await handles.get("real-client:choose-plugin-file")(trusted, "kyf.advisor", "file", false), null);
  assert.equal(await handles.get("real-client:choose-plugin-file")(trusted, "../x", "file", true), null);
  assert.equal((await handles.get("real-client:check-updates")(subframe)).status, "blocked");
  assert.equal(calls.update, 0);
  for (const name of ["real-client:update-state", "real-client:begin-update", "real-client:rollback-update"])
    assert.equal((await handles.get(name)(subframe)).phase, "blocked");
  if (!updatesEnabled) {
    assert.equal((await handles.get("real-client:check-updates")(trusted)).status, "disabled");
    for (const name of ["real-client:update-state", "real-client:begin-update", "real-client:rollback-update"])
      assert.equal((await handles.get(name)(trusted)).phase, "disabled");
    assert.equal(await handles.get("real-client:update-ui-ready")(trusted), false);
    assert.equal(calls.update, 0);
    assert.equal(calls.stage, 0);
    assert.equal(calls.rollback, 0);
    assert.equal(calls.controllerCreated, 0);
    assert.equal(calls.fallback, 0);
    assert.equal(calls.proxyCreated, 0, "disabled updates do not create a network transport");
    assert.equal(calls.monitor, 1, "disabled updates preserve normal startup even with inherited update flags");
    assert.deepEqual(environment, originalEnvironment);
    return;
  }
  if (!selfTest) {
    assert.equal(calls.controllerCreated, 1);
    assert.equal((await handles.get("real-client:check-updates")(trusted)).status, "current");
    assert.equal((await handles.get("real-client:begin-update")(trusted)).phase, "ready");
    assert.equal((await handles.get("real-client:rollback-update")(trusted)).phase, "rolled_back");
    assert.equal(calls.stage, 1);
    assert.equal(calls.rollback, 1);
    assert.equal(calls.update, 2);
    assert.equal(calls.fallback, 1);
    return;
  }
  const [first, second] = await Promise.all([
    handles.get("real-client:check-updates")(trusted),
    handles.get("real-client:check-updates")(trusted),
  ]);
  assert.equal(first.status, "current");
  assert.equal(second.status, "current");
  assert.equal(calls.update, 2);
  assert.equal(calls.fallback, 1);
  assert.equal(calls.proxyCreated, 1);
  assert.deepEqual(environment, {});
  frames.mainFrame.url = "http://127.0.0.1:34567/other";
  assert.equal(handles.get("real-client:app-version")(trusted), null);
  assert.equal((await handles.get("real-client:check-updates")(trusted)).status, "blocked");
}

async function main() {
  await exerciseShell({ packaged: true, selfTest: true });
  await exerciseShell({ packaged: true, selfTest: false });
  await exerciseShell({ packaged: false, selfTest: false });
  await exerciseShell({ packaged: false, selfTest: false, updateEnvironment: {
    KYF_UPDATE_VALIDATE: "1", KYF_UPDATE_FINAL_READY_FILE: "/synthetic-ready",
    KYF_UPDATE_FINAL_READY_NONCE: "b".repeat(64),
  } });
  for (const packageJson of ['{}', '{"kyfUpdatesEnabled":false}', '{"kyfUpdatesEnabled":"true"}',
    '{"kyfUpdatesEnabled":1}', 'null', '{invalid-json']) {
    await exerciseShell({ packaged: true, selfTest: false, packageJson, metadataExpectedEnabled: false,
      extraArgs: ["--kyf-app-updates-enabled"], updateEnvironment: {
        KYF_UPDATES_ENABLED: "1", KYF_UPDATE_VALIDATE: "1", KYF_UPDATE_FINAL_READY_FILE: "/synthetic-ready",
        KYF_UPDATE_FINAL_READY_NONCE: "b".repeat(64),
      } });
  }
  await exerciseShell({ packaged: true, selfTest: false, metadataUnreadable: true, metadataExpectedEnabled: false });
  process.stdout.write("real-client shell update IPC checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
