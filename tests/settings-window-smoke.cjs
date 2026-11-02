"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Opt-in production-renderer smoke, isolated from the user's services and app profile.
// Run: node_modules/electron/dist/electron.exe tests/settings-window-smoke.cjs
const { app, BrowserWindow, ipcMain } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { captureHiddenRenderer } = require("./helpers/capture-hidden-renderer.cjs");

const root = path.resolve(__dirname, "..");
const output = path.join(root, ".local", "tmp", "settings-window-smoke");
fs.mkdirSync(output, { recursive: true });
app.setPath("userData", fs.mkdtempSync(path.join(output, "profile-")));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-background-timer-throttling");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
const checks = [], layouts = [], screenshots = [];
const audit = { requests: [], unexpected: [], rendererErrors: [], externalRequests: [], closeRequests: 0, modelWindows: 0 };
let window, server, timeout, finished = false;
const account = "synthetic-settings-account";
const plugin = { id: "synthetic.settings", name: "合成测试插件", version: "1.0.0", source: "bundled", enabled: true,
  description: "仅用于设置窗口界面验证，不启动任何任务。", contributes: {} };
const source = { mode: "api", sourceId: "api:synthetic", status: "ready", api: { protocol: "chat_completions",
  baseUrl: "https://example.invalid/v1", model: "synthetic-model", contextTokens: 32768, timeoutMs: 3600000, hasKey: false },
  models: [{ id: "a".repeat(32), protocol: "chat_completions", baseUrl: "https://example.invalid/v1",
    model: "synthetic-model", contextTokens: 32768, timeoutMs: 3600000, concurrency: 4, hasKey: false }],
  roles: { main: "a".repeat(32), judge: null, image: null },
  localJudge: {supported: true, state: "missing", ready: false, totalBytes: 168166496, downloadedBytes: 0} };
const coreSettings = { revision: 1, values: { searchSource: "none", searxngUrl: "", searchOpenPages: true } };
const pluginSettings = { plugin, revision: 1, definition: "synthetic-definition", enabled: true,
  schema: { schema: 1, groups: [{ title: "常规设置", fields: [{ key: "enabled", title: "启用合成选项", type: "boolean" }] }] }, values: { enabled: true } };
const traceState = { enabled: true, collecting: true, status: "recording", retentionDays: 7, recordCount: 3218,
  storageBytes: 328000000, pauseHistory: Array.from({ length: 30 }, (_, i) => ({
    startedAt: new Date(2026, 9, 16, 12, i, 0).getTime() / 1000,
    finishedAt: new Date(2026, 9, 16, 12, i, 30).getTime() / 1000,
    reason: "诊断传输中断，部分记录未保存",
  })) };
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(predicate, message, limit = 10000) {
  const start = Date.now();
  while (Date.now() - start < limit) { if (await predicate()) return; await delay(25); }
  throw new Error(message);
}
const evaluate = expression => window.webContents.executeJavaScript(expression, true);
function check(name, condition = true) { assert.ok(condition, name); checks.push(name); }
async function click(selector) { await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`); }
async function change(id, value, event = "change") {
  await evaluate(`(() => { const input = document.getElementById(${JSON.stringify(id)}); if (input.type === 'checkbox') input.checked = ${JSON.stringify(value)}; else input.value = ${JSON.stringify(value)}; input.dispatchEvent(new Event(${JSON.stringify(event)}, { bubbles: true })); })()`);
}
async function tab(name) {
  await click(`.settings-tab-btn[data-tab="${name}"]`);
  await waitFor(() => evaluate(`document.querySelector('.settings-tab-btn.active').dataset.tab === ${JSON.stringify(name)}`), `${name} tab did not activate`);
}
async function screenshot(name) {
  await delay(80);
  const image = await captureHiddenRenderer(window), pixels = image.toBitmap(), colors = new Set();
  for (let offset = 0; offset < pixels.length; offset += 4) colors.add(pixels.readUInt32LE(offset));
  assert.ok(colors.size > 100, `${name}: blank or unpainted capture (${colors.size} colors)`);
  const file = path.join(output, name + ".png"); fs.writeFileSync(file, image.toPNG()); screenshots.push(file);
}
async function serve(request, response) {
  const url = new URL(request.url, "http://127.0.0.1");
  const json = (code, body) => { response.writeHead(code, { "Content-Type": "application/json; charset=utf-8" }); response.end(JSON.stringify(body)); };
  if (url.pathname.startsWith("/api/")) {
    audit.requests.push({ path: url.pathname, method: request.method });
    let raw = ""; for await (const chunk of request) raw += chunk.toString();
    const body = raw ? JSON.parse(raw) : null;
    if (url.pathname === "/api/sessions") return json(200, { account, messagesReady: true, self: { name: "合成账号" },
      sessions: [{ username: "synthetic-friend", name: "合成好友", lastMessageTime: 1 }] });
    if (url.pathname === "/api/conversation-selection") return json(200, { account, initialized: true, selectedSessions: ["synthetic-friend"] });
    if (url.pathname === "/api/accounts") return json(200, { currentAccountId: account, accounts: [{ accountId: account, wechatId: "synthetic-wechat", current: true }] });
    if (url.pathname === "/api/model-source") return json(200, source);
    if (url.pathname === "/api/model-source/models/save") {
      const id = body.id || "b".repeat(32);
      const previous = source.models.find(item => item.id === id);
      const entry = { ...previous, ...body, id, name: "", hasKey: !!body.apiKey || !!previous?.hasKey };
      delete entry.apiKey;
      source.models = [...source.models.filter(item => item.id !== id), entry];
      if (id === source.roles.main) Object.assign(source.api, entry);
      return json(200, source);
    }
    if (url.pathname === "/api/model-source/roles") { Object.assign(source.roles, body); return json(200, source); }
    if (url.pathname === "/api/model-source/models/delete") {
      assert.notEqual(body.id, source.roles.main);
      source.models = source.models.filter(item => item.id !== body.id);
      for (const role of ["judge", "image"]) if (source.roles[role] === body.id) source.roles[role] = null;
      return json(200, source);
    }
    if (url.pathname === "/api/model-source/test") return json(200, { ok: true, latencyMs: 1 });
    if (url.pathname === "/api/model-source/list") return json(200, { supported: true, models: [{id: "synthetic-judge"}] });
    if (url.pathname === "/api/model-source/reveal-key") {
      assert.equal(body.id, "b".repeat(32));
      return json(200, {apiKey: "synthetic-key"});
    }
    if (url.pathname.startsWith("/api/model-source/local-judge/")) {
      assert.deepEqual(body, {});
      if (url.pathname.endsWith("/prepare")) {
        source.localJudge = {...source.localJudge, state: "ready", ready: true, downloadedBytes: 168166496,
          latencyMs: 25, qualityVerified: false};
        source.models.push({id: "c".repeat(32), builtin: "julia-1", protocol: "systemone", model: "julia-1", concurrency: 1});
      } else if (url.pathname.endsWith("/delete")) {
        source.models = source.models.filter(item => !item.builtin);
        source.roles.judge = null;
        source.localJudge = {...source.localJudge, state: "missing", ready: false, downloadedBytes: 0};
      }
      return json(200, source);
    }
    if (url.pathname === "/api/data-root") return json(200, { state: "unset", path: "", accounts: 1 });
    if (url.pathname === "/api/media/settings") return json(200, { account, mode: "reference", cacheMaxBytes: 536870912, cacheBytes: 0, archiveBytes: 0 });
    if (["/api/plugins", "/api/plugins/refresh"].includes(url.pathname)) return json(200, { plugins: [plugin], problems: [] });
    if (url.pathname === "/api/plugins/settings") {
      if (body) { pluginSettings.values = body.values; pluginSettings.revision++; }
      return json(200, pluginSettings);
    }
    if (url.pathname === "/api/model-settings") {
      if (body) { assert.equal(body.revision, coreSettings.revision); coreSettings.values = body.values; coreSettings.revision++; }
      return json(200, coreSettings);
    }
    audit.unexpected.push(url.pathname); return json(404, { message: "No synthetic route" });
  }
  const resource = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
  const base = resource.startsWith("plugin-sdk/") ? root : path.join(root, "chatui");
  const file = path.resolve(base, resource);
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return json(404, { message: "No synthetic resource" });
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png" };
  response.writeHead(200, { "Content-Type": `${mime[path.extname(file)] || "application/octet-stream"}; charset=utf-8` }); response.end(fs.readFileSync(file));
}
async function finish(error) {
  if (finished) return; finished = true; clearTimeout(timeout);
  const report = { passed: !error, checks, layouts, audit, screenshots, electron: process.versions.electron,
    fixture: "Production index.html, renderer and preload with isolated loopback API and IPC fixtures, hidden offscreen Electron window",
    ...(error ? { error: String(error.stack || error) } : {}) };
  fs.writeFileSync(path.join(output, "report.json"), JSON.stringify(report, null, 2));
  if (window && !window.isDestroyed()) window.destroy();
  server?.closeAllConnections(); server?.close();
  process.stdout.write(JSON.stringify({ passed: report.passed, checks: checks.length, report: path.join(output, "report.json") }) + "\n");
  if (error) process.stderr.write(String(error.stack || error) + "\n");
  app.exit(error ? 1 : 0);
}
process.on("uncaughtException", finish); process.on("unhandledRejection", finish);
void app.whenReady().then(async () => {
  timeout = setTimeout(() => { void finish(new Error("Settings smoke timed out")); }, 90000);
  server = http.createServer((req, res) => { void serve(req, res).catch(error => { res.destroy(); void finish(error); }); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  ipcMain.handle("settings-window:target", () => ({ tab: "models" }));
  ipcMain.handle("settings-window:close", () => { audit.closeRequests++; return true; });
  ipcMain.handle("real-client:app-version", () => "1.3.6-synthetic");
  ipcMain.handle("model-traces-window:open", () => { audit.modelWindows++; return true; });
  ipcMain.handle("model-traces:request", (_event, operation, args) => {
    if (operation === "scope") return { ok: true, scope: args.scope, accountId: account, deleteVersion: 0 };
    if (operation === "configure") Object.assign(traceState, args);
    return { ok: true, state: traceState, deleteVersion: 0 };
  });
  window = new BrowserWindow({ width: 900, height: 780, show: false, webPreferences: {
    preload: path.join(root, "scripts", "real-client-preload.cjs"), additionalArguments: ["--kyf-settings-window"],
    contextIsolation: true, sandbox: true, nodeIntegration: false, offscreen: true, backgroundThrottling: false,
  } });
  window.webContents.on("console-message", details => {
    if (details.level === "error" && !details.message.includes("favicon")) audit.rendererErrors.push(details.message);
  });
  window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    const allowed = details.url.startsWith(origin + "/"); if (!allowed) audit.externalRequests.push(details.url);
    callback({ cancel: !allowed });
  });
  window.setContentSize(900, 780);
  await window.loadURL(origin + "/?window=settings");
  await waitFor(() => evaluate(`document.querySelector('#panelModels').classList.contains('active') && !document.querySelector('#selectModelSearchSource').disabled && document.querySelector('#modelTraceHistoryCount').textContent === '30 次'`), "Production model settings did not initialize");
  check("standalone settings shell is visible without a chat overlay", await evaluate(`document.documentElement.classList.contains('settings-window') && getComputedStyle(document.querySelector('#appWindow')).display === 'none' && document.querySelector('#settingsModal').parentElement === document.body && document.querySelector('#settingsModal').classList.contains('show')`));
  check("history is collapsed and summary stays compact", await evaluate(`!document.querySelector('#modelTraceHistory').open && document.querySelector('#modelTraceHistoryList').children.length === 30 && !document.querySelector('#modelTraceStorage').textContent.includes('中断')`));
  check("home keeps developer controls in a separate page", await evaluate(`!document.querySelector('#modelHomeView').hidden && document.querySelector('#developerSettingsView').hidden`));
  await screenshot("models-home");
  await click("#btnAddModel");
  await change("inputApiBaseUrl", "https://judge.invalid/v1", "input");
  await change("inputApiKey", "synthetic-key", "input");
  await change("inputApiModelId", "synthetic-judge", "input");
  await click("#btnSaveApiModel");
  await waitFor(() => evaluate(`!document.querySelector('#modelHomeView').hidden && document.querySelector('#modelListRows').children.length === 2`), "Added model did not return to home");
  await change("selectRoleJudge", "b".repeat(32));
  await waitFor(() => source.roles.judge === "b".repeat(32), "Judge role was not saved");
  await waitFor(() => evaluate(`!document.querySelector('#selectRoleImage').disabled`), "Role save did not settle");
  await change("selectRoleImage", "b".repeat(32));
  await waitFor(() => source.roles.image === "b".repeat(32), "Image role was not saved");
  await waitFor(() => evaluate(`!document.querySelector('#btnAddModel').disabled`), "Role save did not settle");
  await click("#modelListRows button:last-child");
  await click("#btnToggleApiKeyVisibility");
  await waitFor(() => evaluate(`document.querySelector('#inputApiKey').value === 'synthetic-key'`), "Optional model key did not reveal");
  await click("#btnToggleApiKeyVisibility");
  await click("#btnTestApiModel");
  await waitFor(() => evaluate(`document.querySelector('#apiModelTestStatus').textContent.includes('连接成功')`), "Optional model test did not finish");
  await click("#btnFetchApiModels");
  await waitFor(() => evaluate(`document.querySelector('#apiModelCount').textContent.includes('1 个模型')`), "Optional model list did not load");
  await screenshot("models-detail");
  await click("#btnDeleteApiModel");
  await waitFor(() => source.models.length === 1 && !source.roles.judge && !source.roles.image, "Delete did not clear optional roles");
  await waitFor(() => evaluate(`!document.querySelector('#modelHomeView').hidden`), "Delete did not return home");
  check("add, assign, reveal, test, list and delete work in the production renderer");
  await change("selectRoleJudge", "download-local");
  await waitFor(() => evaluate(`!document.querySelector('#localJudgeView').hidden && document.querySelector('#localJudgeStatus').textContent.includes('影子评估')`), "Local judge did not prepare");
  check("preparing the local candidate does not automatically enable a role", source.roles.judge === null);
  await screenshot("models-local-judge");
  await click("#btnBackLocalJudge");
  await change("selectRoleJudge", "c".repeat(32));
  await waitFor(() => source.roles.judge === "c".repeat(32), "Local judge role was not saved");
  await waitFor(() => evaluate(`!document.querySelector('#btnAddModel').disabled`), "Local role save did not settle");
  await click("#modelListRows button:last-child");
  await click("#btnDeleteLocalJudge");
  await waitFor(() => source.models.length === 1 && source.roles.judge === null, "Local judge delete did not restore follow-main");
  await click("#btnBackLocalJudge");
  check("local candidate prepare, manual selection and delete work in the production renderer");
  await click("#btnOpenSearchSettings");
  await change("inputModelSearchOpenPages", false, "change"); await click("#btnSaveCoreModelSettings");
  await waitFor(() => evaluate(`document.querySelector('#coreModelSettingsStatus').textContent.includes('已保存')`), "Core settings save did not finish");
  check("production core settings save updates the synthetic API", coreSettings.values.searchOpenPages === false && coreSettings.revision === 2);
  await click("#btnBackFromSearch");
  await click("#btnOpenDeveloperSettings");
  await click("#btnViewModelTraces");
  check("model-call viewer leaves settings visible", audit.modelWindows === 1 && await evaluate(`document.querySelector('#settingsModal').classList.contains('show')`));
  await tab("general"); await click("#btnManageAccounts");
  await waitFor(() => evaluate(`document.querySelector('#accountList').textContent.includes('synthetic-wechat')`), "Account manager did not render");
  await click("#btnManageConversations");
  check("general settings renders the synthetic conversation directory", await evaluate(`!document.querySelector('#conversationManager').hidden && document.querySelector('#conversationManagerList').textContent.includes('合成好友')`));
  await click("#btnManageConversations");
  await tab("plugins");
  await waitFor(() => evaluate(`document.querySelector('#pluginList').textContent.includes('合成测试插件')`), "Plugin tab did not render");
  await tab("about");
  check("about tab shows the app version", await evaluate(`document.querySelector('#panelAbout').textContent.includes('1.3.6-synthetic')`));
  for (const [width, height, zoom] of [[900, 780, "1.0"], [680, 500, "1.0"], [680, 500, "1.5"], [900, 780, "1.5"]]) {
    window.setContentSize(width, height); await change("selectZoomLevel", zoom); await delay(50);
    for (const name of ["general", "models", "plugins", "about"]) {
      await tab(name);
      if (name === "models") {
        await click("#btnOpenDeveloperSettings");
        await evaluate(`document.querySelector('#modelTraceHistory').open = true`);
      }
      const layout = await evaluate(`(() => {
        const content = document.querySelector('.settings-content'), panel = document.querySelector('.settings-panel.active');
        const visible = element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden';
        return { viewport: [innerWidth, innerHeight], content: [content.clientWidth, content.scrollWidth],
          body: [document.body.clientWidth, document.body.scrollWidth], panel: [panel.clientWidth, panel.scrollWidth],
          escapedControls: [...panel.querySelectorAll('button, input, select')].filter(visible).filter(element => element.getBoundingClientRect().right > innerWidth + 2).map(element => element.id || element.textContent),
          sidebarVisible: [...document.querySelectorAll('.settings-tab-btn')].every(element => element.getBoundingClientRect().right <= innerWidth),
        };
      })()`);
      layouts.push({ width, height, zoom, tab: name, ...layout });
      check(`no horizontal overflow at ${width}x${height}, ${zoom} zoom, ${name}`, layout.content[1] <= layout.content[0] + 2 && layout.panel[1] <= layout.panel[0] + 2 && !layout.escapedControls.length && layout.sidebarVisible);
      if (name === "models") {
        check(`expanded history stays bounded at ${width}x${height}, ${zoom} zoom`, await evaluate(`(() => { const list = document.querySelector('#modelTraceHistoryList'); return list.clientHeight <= 220 && list.scrollHeight > list.clientHeight; })()`));
        if (width === 680 && zoom === "1.5") {
          await evaluate(`document.querySelector('.settings-content').scrollTo({ top: 99999, behavior: 'instant' })`);
          await screenshot("settings-dark-minimum-150-expanded");
        }
        await evaluate(`document.querySelector('#modelTraceHistory').open = false`);
        await click("#btnBackDeveloperSettings");
      }
    }
  }
  window.setContentSize(900, 780); await change("selectZoomLevel", "1.0"); await tab("models");
  await waitFor(() => evaluate(`!document.querySelector('#selectRoleMain').disabled`), "Model settings refresh did not settle");
  await click("#btnOpenDeveloperSettings");
  await evaluate(`document.querySelector('.settings-content').scrollTo({ top: 99999, behavior: 'instant' })`);
  for (const theme of ["dark", "light"]) { await change("selectThemeMode", theme); await screenshot(`settings-${theme}-collapsed`); }
  await click("#modelTraceHistory summary");
  await evaluate(`document.querySelector('.settings-content').scrollTo({ top: 99999, behavior: 'instant' })`);
  check("expanded history has an internal scroll limit", await evaluate(`(() => { const list = document.querySelector('#modelTraceHistoryList'); return document.querySelector('#modelTraceHistory').open && list.clientHeight <= 220 && list.scrollHeight > list.clientHeight; })()`));
  await screenshot("settings-light-expanded");
  await tab("plugins"); await click("[id='pluginSettings-synthetic.settings']");
  await waitFor(() => evaluate(`!!document.querySelector('#pluginConfig-enabled')`), "Plugin configuration did not load");
  await click("#pluginConfig-enabled");
  window.webContents.send("settings-window:request-close");
  await waitFor(() => evaluate(`!document.querySelector('#pluginLeaveConfirm').hidden`), "Native close request bypassed the unsaved plugin confirmation");
  check("native close request preserves an unsaved plugin draft", audit.closeRequests === 0);
  await click("#pluginLeaveStay");
  check("keep editing retains the plugin draft", await evaluate(`!document.querySelector('#pluginConfig-enabled').checked && document.querySelector('#settingsModal').classList.contains('show')`));
  window.webContents.send("settings-window:request-close");
  await waitFor(() => evaluate(`!document.querySelector('#pluginLeaveConfirm').hidden`), "Second close confirmation did not appear");
  await click("#pluginLeaveDiscard");
  await waitFor(() => audit.closeRequests === 1, "Confirmed close did not reach the desktop host");
  check("confirmed discard closes through the desktop bridge");
  check("no messages, attention, model inference, or unknown API requests", audit.unexpected.length === 0);
  check("no external network requests", audit.externalRequests.length === 0);
  check("production renderer has no uncaught errors", audit.rendererErrors.length === 0);
  await finish();
}).catch(finish);
