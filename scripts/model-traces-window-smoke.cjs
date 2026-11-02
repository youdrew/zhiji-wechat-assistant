"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Opt-in Electron smoke: real preload, window host, IPC, and viewer; synthetic HTTP only.
// Run with node_modules/electron/dist/electron.exe scripts/model-traces-window-smoke.cjs.
const { app, BrowserWindow, ipcMain, screen, clipboard } = require("electron");
const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { attachModelTraces } = require("./model-traces-host.cjs");
const { createModelTracesWindow } = require("./model-traces-window.cjs");

const root = path.resolve(__dirname, "..");
const outputDirectory = path.join(root, ".local", "tmp");
fs.mkdirSync(outputDirectory, { recursive: true });
app.setPath("userData", fs.mkdtempSync(path.join(outputDirectory, "trace-window-smoke-")));
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-background-timer-throttling");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
const controlToken = randomBytes(32).toString("hex");
const sessions = new Map();
const audit = { sessions: 0, lists: 0, details: 0, bodies: 0, usage: [], rejected: 0, modelRequests: 0, rendererErrors: [] };
const checks = [];
const minimumLayouts = [];
const usageLayouts = [];
const usageScreenshots = [];
let fixtureAccount = "synthetic-account-a", liveAttempt = false;
let server, mainWindow, manager, mainHost, timeout;
let finished = false;
const reportPath = path.join(outputDirectory, "model-traces-window-smoke.json");
const screenshotPath = path.join(outputDirectory, "model-traces-window-smoke.png");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(test, message, limit = 10000) {
  const started = Date.now();
  while (Date.now() - started < limit) {
    if (await test()) return;
    await delay(25);
  }
  throw new Error(message);
}
function record(name, value = true) { assert.ok(value, name); checks.push(name); }
function metadata(account, contact) {
  return { recordId: `${account}-${contact}`, accountId: account, contactId: contact,
    taskId: `task-${account}-${contact}`, source: "advisor", kind: "http", model: "synthetic-model",
    startedAt: Date.now() / 1000 - 5, finishedAt: Date.now() / 1000 - 4,
    transportStatus: "completed", applicationStatus: "completed", adoptionStatus: "adopted",
    captureStatus: "complete", complete: true, protocol: "openai", method: "POST", httpStatus: 200 };
}
// Every aggregate and visible row comes from this isolated, in-memory dataset. Keeping
// 301 attempts while returning one visible record checks full-history aggregation.
const fixtureNow = Date.now();
const fixtureDay = new Date(fixtureNow); fixtureDay.setHours(0, 0, 0, 0);
const pastDay = offset => { const value = new Date(fixtureDay); value.setDate(value.getDate() + offset); return value.getTime(); };
const usageAttempts = Array.from({ length: 300 }, (_, index) => {
  const day = Math.floor(index / 100), at = day < 2 ? pastDay(day - 2) + (10 + day) * 3600000 + index % 100 * 1000 :
    Math.max(fixtureDay.getTime(), fixtureNow - 600000) + index % 100 * 1000;
  return { attemptId: `synthetic-attempt-${index}`, at: Math.min(at, fixtureNow - 2000), scope: "friend", user: "synthetic-contact-a",
    plugin: index < 180 ? "kyf.advisor" : "kyf.friend-analysis", step: index < 120 ? "advisor" : index < 180 ? "wechat-summary" : index < 240 ? "memory" : "friend-persona",
    model: index % 2 ? "synthetic-other" : "synthetic-model", status: "ok", durationMs: 1000,
    inputTokens: 42, outputTokens: 5, totalInputTokens: 42, totalOutputTokens: 5, totalTokens: 47, totalTokensSource: "provider",
    cacheReadTokens: 10, cacheWriteTokens: 0, reasoningTokens: 2, toolUsePromptTokens: null };
});
usageAttempts.push({ attemptId: "synthetic-attempt-unknown", at: fixtureNow - 1000, scope: "friend", user: "synthetic-contact-a",
  plugin: "kyf.advisor", step: "advisor", model: "synthetic-other", status: "cancelled", durationMs: 1000,
  inputTokens: null, outputTokens: null, totalInputTokens: null, totalOutputTokens: null, totalTokens: null, totalTokensSource: null,
  cacheReadTokens: null, cacheWriteTokens: null, reasoningTokens: null, toolUsePromptTokens: null });
const tokenFields = ["inputTokens", "outputTokens", "totalInputTokens", "totalOutputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "toolUsePromptTokens"];
const coverageFields = { knownInputRequests: "totalInputTokens", knownOutputRequests: "totalOutputTokens", knownTotalRequests: "totalTokens",
  knownCacheReadRequests: "cacheReadTokens", knownCacheWriteRequests: "cacheWriteTokens", knownReasoningRequests: "reasoningTokens", knownToolUsePromptRequests: "toolUsePromptTokens" };
const moduleNames = { "kyf.advisor": "聊天助手", "kyf.friend-analysis": "知己分析" };
function usageAggregate(rows) {
  const value = { requests: rows.length, failures: rows.filter(row => ["http-error", "network-error", "timeout", "stream-error"].includes(row.status)).length,
    missingUsage: rows.filter(row => row.inputTokens === null || row.outputTokens === null).length, lastAt: rows.length ? Math.max(...rows.map(row => row.at)) : null };
  for (const field of tokenFields) value[field] = rows.reduce((sum, row) => sum + (row[field] ?? 0), 0);
  for (const [key, field] of Object.entries(coverageFields)) value[key] = rows.filter(row => Number.isFinite(row[field])).length;
  value.missingInputUsage = rows.length - value.knownInputRequests; value.missingOutputUsage = rows.length - value.knownOutputRequests;
  value.missingTotalUsage = rows.length - value.knownTotalRequests;
  return value;
}
function usageMatching(args, { time = true, model = true } = {}) {
  return usageAttempts.filter(row => (!time || ((!args.since || row.at >= Number(args.since)) && (!args.until || row.at < Number(args.until)))) &&
    (!model || !args.model || args.model === row.model) && (!args.plugin || args.plugin === row.plugin) &&
    (!args.step || args.step === row.step) && (!args.status || args.status === row.status) && (!args.friend || args.friend === row.user));
}
function usageModules(rows, steps = true) {
  return [...new Set(rows.map(row => row.plugin))].map(plugin => {
    const own = rows.filter(row => row.plugin === plugin);
    return { plugin, name: moduleNames[plugin], ...usageAggregate(own), ...(steps ? { steps: [...new Set(own.map(row => row.step))].map(step =>
      ({ step, ...usageAggregate(own.filter(row => row.step === step)) })) } : {}) };
  }).sort((left, right) => right.totalTokens - left.totalTokens);
}
const mainHtml = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Synthetic main page</title>
<body class="theme-light"><button id="btnModelTraces">模型调用</button><button id="btnViewModelTraces">查看模型调用</button>
<button id="btnDeveloperMode"></button><select id="modelTraceRetention"><option value="7">7</option></select>
<p id="modelTraceStorage"></p><button id="btnRetryModelTraces"></button><textarea id="mainDraft">Synthetic unsent draft</textarea>
<script src="/model-traces.js"></script><script>ModelTraces.init({account:"synthetic-account-a",user:"synthetic-contact-a",view:"chat"});
document.getElementById("mainDraft").focus();</script></body></html>`;
async function serve(request, response) {
  const address = new URL(request.url, "http://127.0.0.1");
  const json = (status, value) => { response.writeHead(status, { "Content-Type": "application/json" }); response.end(JSON.stringify(value)); };
  if (address.pathname === "/fixture-main") {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }); response.end(mainHtml); return;
  }
  if (address.pathname.startsWith("/api/usage/")) {
    const route = address.pathname.slice("/api/usage/".length);
    const args = Object.fromEntries(address.searchParams);
    audit.usage.push({ route, ...args });
    if (args.account !== fixtureAccount) return json(409, { error: "account-changed" });
    const rows = usageMatching(args), extent = usageMatching(args, { time: false });
    const history = { firstAt: extent.length ? Math.min(...extent.map(row => row.at)) : null,
      lastAt: extent.length ? Math.max(...extent.map(row => row.at)) : null, cutoffMs: fixtureNow, droppedRecords: 0 };
    if (route === "summary") return json(200, { ...usageAggregate(rows), ...history,
      models: [...new Set(usageMatching(args, { model: false }).map(row => row.model))], plugins: usageModules(rows, false) });
    if (route === "friends") return json(200, { friends: rows.length ? [{ scope: "friend", user: "synthetic-contact-a", name: "合成好友", ...usageAggregate(rows) }] : [] });
    if (route === "modules") return json(200, { modules: usageModules(rows) });
    if (route === "timeline") {
      const bounds = args.boundaries.split(",").map(Number);
      return json(200, { bounds, buckets: bounds.slice(0, -1).map((since, index) => {
        const own = rows.filter(row => row.at >= since && row.at < bounds[index + 1]);
        return { since, until: bounds[index + 1], ...usageAggregate(own), modules: usageModules(own, false) };
      }), ...history });
    }
    // Core lists an in-flight attempt ahead of the first stored page until its row is written.
    const live = liveAttempt && !args.before && !args.since && Object.keys(args).length === 2 ? [{ attemptId: "synthetic-live", at: fixtureNow,
      scope: "friend", user: "synthetic-contact-a", plugin: "kyf.advisor", step: "advisor", model: "synthetic-model", status: "running",
      totalTokens: null, traceRecordId: `${fixtureAccount}-synthetic-contact-a` }] : [];
    if (route === "records") return json(200, { records: [...live, ...rows.slice(0, 15).map(row => ({ ...row, account: fixtureAccount,
      traceRecordId: `${fixtureAccount}-synthetic-contact-a` }))], next: null });
    return json(404, { error: "fixture-route-not-found" });
  }
  if (!address.pathname.startsWith("/api/model-traces/")) {
    if (address.pathname === "/plugin-sdk/web/time-trend.js") {
      response.writeHead(200, { "Content-Type": "text/javascript; charset=utf-8" });
      response.end(fs.readFileSync(path.join(root, "plugin-sdk", "web", "time-trend.js"))); return;
    }
    const allowed = new Set(["model-traces-window.html", "model-traces-window.js", "model-traces.js", "model-usage.js", "model-traces.css", "style.css"]);
    const name = address.pathname.slice(1);
    if (!allowed.has(name)) return json(404, { error: "fixture-resource-not-found" });
    const file = path.join(root, "chatui", name);
    const type = name.endsWith(".html") ? "text/html" : name.endsWith(".js") ? "text/javascript" : "text/css";
    response.writeHead(200, { "Content-Type": `${type}; charset=utf-8` }); response.end(fs.readFileSync(file)); return;
  }
  const route = address.pathname.slice("/api/model-traces/".length);
  if (route === "session" && request.method === "POST") {
    if (request.headers["x-kyf-control-token"] !== controlToken) { audit.rejected++; return json(403, { error: "forbidden" }); }
    let raw = "";
    for await (const part of request) { raw += part.toString(); if (raw.length > 4096) return json(400, { error: "too-large" }); }
    const input = JSON.parse(raw), token = randomBytes(32).toString("hex");
    const session = { accountId: fixtureAccount, scope: input.scope, token, deleteVersion: 0 };
    sessions.set(token, session); audit.sessions++; return json(200, session);
  }
  const session = sessions.get(request.headers["x-model-trace-session"]);
  if (!session) { audit.rejected++; return json(403, { error: "forbidden" }); }
  const state = { enabled: true, collecting: true, status: "on", retentionDays: 7, recordCount: 2, storageBytes: 1024 };
  if (route === "state") return json(200, { state, deleteVersion: 0 });
  if (route === "events") return json(200, { state, revision: 1, changes: [], reset: false, deleteVersion: 0 });
  if (route === "records") {
    audit.lists++;
    const contact = address.searchParams.get("user") || "synthetic-contact-a";
    return json(200, { records: [metadata(session.accountId, contact)], total: 1, nextOffset: null, deleteVersion: 0 });
  }
  const match = /^records\/([A-Za-z0-9_-]+)(\/body)?$/.exec(route);
  if (match) {
    const id = match[1];
    if (!id.startsWith(session.accountId + "-")) { audit.rejected++; return json(403, { error: "wrong-account" }); }
    const contact = id.slice(session.accountId.length + 1);
    if (!match[2]) { audit.details++; return json(200, { record: metadata(session.accountId, contact), deleteVersion: 0 }); }
    audit.bodies++;
    const kind = address.searchParams.get("kind");
    const text = kind === "request" ? JSON.stringify({ model: "synthetic-model", messages: [{ role: "user", content: `SYNTHETIC BODY ${id}` }] }) : `SYNTHETIC RESPONSE ${id}`;
    return json(200, { text, offset: 0, nextOffset: null, totalBytes: Buffer.byteLength(text), complete: true, truncated: false, deleteVersion: 0 });
  }
  return json(404, { error: "fixture-route-not-found" });
}
// Use real native windows and renderers while suppressing presentation so the
// opt-in test never takes the user's foreground window or displays test data.
function HiddenBrowserWindow(options) {
  const window = new BrowserWindow({ ...options, show: false,
    webPreferences: { ...options.webPreferences, offscreen: true, backgroundThrottling: false } });
  window.show = () => {};
  window.focus = () => {};
  return window;
}
async function finish(error) {
  if (finished) return;
  finished = true; clearTimeout(timeout);
  const report = { passed: !error, checks, audit, minimumLayouts, usageLayouts, usageScreenshots, electron: process.versions.electron,
    fixture: "Synthetic authenticated loopback HTTP; production renderer, preloads, IPC, and native window manager",
    presentation: "Hidden offscreen native windows; show/focus calls suppressed; main DOM focus checked",
    screenshot: fs.existsSync(screenshotPath) ? screenshotPath : null,
    ...(error ? { error: String(error.stack || error) } : {}) };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  manager?.close(); mainHost?.close();
  for (const window of BrowserWindow.getAllWindows()) if (!window.isDestroyed()) window.destroy();
  server?.closeAllConnections(); server?.close();
  process.stdout.write(JSON.stringify({ passed: report.passed, checks: checks.length, report: reportPath, screenshot: report.screenshot }) + "\n");
  if (error) process.stderr.write(String(error.stack || error) + "\n");
  app.exit(error ? 1 : 0);
}
app.whenReady().then(async () => {
  timeout = setTimeout(() => { void finish(new Error("Standalone model trace smoke timed out")); }, 60000);
  server = http.createServer((request, response) => { void serve(request, response).catch(error => { response.writeHead(500); response.end("Synthetic fixture error"); void finish(error); }); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const clientUrl = `http://127.0.0.1:${server.address().port}/`;
  const mainUrl = new URL("fixture-main", clientUrl).href;
  mainWindow = new HiddenBrowserWindow({ width: 1200, height: 800,
    webPreferences: { preload: path.join(__dirname, "real-client-preload.cjs"), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  const trustedMainFrame = event => !mainWindow.isDestroyed() && event.sender === mainWindow.webContents &&
    event.senderFrame === mainWindow.webContents.mainFrame && event.senderFrame.url === mainUrl;
  const options = { BrowserWindow: HiddenBrowserWindow, mainWindow, clientUrl, screen, ipcMain, clipboard, trustedMainFrame, getControlToken: () => controlToken };
  mainHost = attachModelTraces(options);
  manager = createModelTracesWindow(options);
  await mainWindow.loadURL(mainUrl);
  const mainEval = script => mainWindow.webContents.executeJavaScript(script);
  await waitFor(() => mainEval("document.getElementById('btnDeveloperMode').textContent === '已开启'"), "Main developer settings did not load");
  record("main initialization does not build an embedded trace panel", await mainEval("!document.getElementById('modelTracesPanel')"));
  record("opening without user activation is rejected", await mainEval("desktopHost.openModelTracesWindow({account:'synthetic-account-a',user:'synthetic-contact-a',theme:'light'})") === false);
  const mainBounds = mainWindow.getBounds();
  const context = { account: fixtureAccount, user: "synthetic-contact-a", view: "chat", theme: "light" };
  record("window manager accepts a valid context", manager.open(context));
  let viewer;
  await waitFor(() => { viewer = BrowserWindow.getAllWindows().find(window => window !== mainWindow); return viewer; }, "Native viewer not created");
  viewer.webContents.on("console-message", event => {
    if (event.level === "error") audit.rendererErrors.push(event.message);
  });
  const viewerEval = async script => {
    try { return await viewer.webContents.executeJavaScript(script); }
    catch (error) { audit.failedRendererScript = script; throw error; }
  };
  await waitFor(() => viewerEval("!!document.querySelector('#modelUsageRecords .model-usage-record')"), "Usage ledger not loaded over production IPC");
  await waitFor(() => viewerEval("document.querySelector('.usage-chart canvas')?.width > 0"), "Shared timeline canvas did not paint");
  record("global usage opens first and uses whole-ledger aggregates", await viewerEval("!document.getElementById('modelUsagePanel').hidden && document.getElementById('modelUsageSummary').textContent.includes('301') && document.getElementById('modelUsageSummary').textContent.includes('14,100') && document.getElementById('modelUsageFriend').value === '' && document.getElementById('modelCallsStatsTab').getAttribute('aria-selected') === 'true'"));
  record("one navigation row contains statistics and call records without duplicate title or close", await viewerEval(`(() => {
    const tabs = Array.from(document.querySelectorAll('#modelCallsNavigation [role=tab]'));
    return tabs.map(tab => tab.textContent).join('|') === '用量统计|调用记录' &&
      tabs.filter(tab => tab.getAttribute('aria-selected') === 'true').length === 1 &&
      !['modelUsageClose','modelUsageStatsTab','modelUsageRecordsTab','modelUsageTraces'].some(id => document.getElementById(id)) &&
      !Array.from(document.querySelectorAll('#modelCallsContent h2')).some(node => node.textContent === '模型调用');
  })()`));
  record("opening usage never filters by the main chat", audit.usage.length >= 5 && audit.usage.every(call => !call.friend));
  record("a compact record page does not limit aggregate history", await viewerEval("document.querySelectorAll('#modelUsageRecords .model-usage-record').length === 15 && document.getElementById('modelUsageAccounting').textContent.includes('300 / 301') && document.getElementById('modelUsageSummary').textContent.includes('14,100')"));
  const captureUsage = async name => {
    await waitFor(() => viewerEval("document.getElementById('modelUsagePanel').getAttribute('aria-busy') !== 'true' && (document.getElementById('modelUsageStatsView').hidden || document.querySelector('.usage-chart canvas')?.width > 0)"), "Usage capture did not reach a painted, settled renderer");
    await delay(50);
    const file = path.join(outputDirectory, name); fs.writeFileSync(file, (await viewer.webContents.capturePage()).toPNG()); usageScreenshots.push(file);
  };
  liveAttempt = true;
  await viewerEval("document.getElementById('modelCallsRecordsTab').click(); window.dispatchEvent(new Event('focus'))");
  await waitFor(() => viewerEval("(() => { const row = document.querySelector('#modelUsageRecords .model-usage-record'); return row?.querySelector('.usage-record-status').textContent === '进行中' && row.querySelector('.usage-record-total').textContent === '—'; })()"), "In-flight attempt was not listed first");
  record("an in-flight attempt is listed first with unknown usage and counted apart", await viewerEval("document.getElementById('modelUsageCompactSummary').textContent.includes('1 次 进行中') && document.querySelectorAll('#modelUsageRecords .model-usage-record').length === 16"));
  await captureUsage("model-usage-window-smoke-live.png");
  liveAttempt = false;
  await waitFor(() => viewerEval("document.querySelector('#modelUsageRecords .model-usage-record .usage-record-status').textContent !== '进行中' && !document.getElementById('modelUsageCompactSummary').textContent.includes('进行中')"), "A finished in-flight attempt did not leave the list", 6000);
  record("a finished attempt leaves through the quick refresh without reopening the page");
  await viewerEval("document.getElementById('modelCallsStatsTab').click()");
  await viewerEval(`(() => {
    window.smokeUnknownMarks = 0;
    const original = CanvasRenderingContext2D.prototype.strokeRect;
    window.smokeOriginalStrokeRect = original;
    CanvasRenderingContext2D.prototype.strokeRect = function (...args) { window.smokeUnknownMarks++; return original.apply(this, args); };
    document.body.style.setProperty('--smoke-repaint', '1');
  })()`);
  await waitFor(() => viewerEval("window.smokeUnknownMarks > 0"), "Incomplete-use bucket did not draw its dashed marker");
  record("unknown tokens are labeled and marked on the real canvas", await viewerEval("document.querySelector('.model-usage-legend').textContent.includes('虚线周期有 1 次用量不完整') && document.getElementById('modelUsageAccounting').textContent.includes('1 次调用总量未知') && window.smokeUnknownMarks > 0"));
  await viewerEval("CanvasRenderingContext2D.prototype.strokeRect = window.smokeOriginalStrokeRect; true");
  await captureUsage("model-usage-window-smoke-unknown.png");
  await viewerEval("document.querySelector('#modelUsageModules .model-usage-module-name').click()");
  record("module expands to real step totals", await viewerEval("document.querySelector('#modelUsageModules .model-usage-module-name').getAttribute('aria-expanded') === 'true' && Array.from(document.querySelectorAll('#modelUsageModules .model-usage-step')).some(node => node.textContent.includes('助手回答')) && Array.from(document.querySelectorAll('#modelUsageModules .model-usage-step')).some(node => node.textContent.includes('聊天历史摘要'))"));
  await viewerEval("document.querySelectorAll('#modelUsageModules .model-usage-module-name')[1].click()");
  viewer.setContentSize(960, 1040); await delay(100);
  record("default overview shows all modules and their steps with every filter cleared", await viewerEval("document.getElementById('modelCallsStatsTab').getAttribute('aria-selected') === 'true' && ['modelUsageFriend','modelUsagePlugin','modelUsageModel','modelUsageStatus'].every(id => document.getElementById(id).value === '') && document.getElementById('modelUsageRange').value === 'all' && document.getElementById('modelUsageStep').hidden && document.querySelectorAll('.model-usage-module-name[aria-expanded=true]').length === 2 && document.querySelectorAll('.model-usage-step').length === 4"));
  await captureUsage("model-usage-window-smoke-overview.png");
  viewer.setContentSize(960, 760);
  await viewerEval("document.getElementById('modelUsageModel').value = 'synthetic-other'; document.getElementById('modelUsageModel').dispatchEvent(new Event('change'))");
  await waitFor(() => audit.usage.filter(call => call.model === "synthetic-other").length >= 5, "Model filter did not reach all usage endpoints");
  await waitFor(() => viewerEval("document.getElementById('modelUsageSummary').textContent.includes('151 次')"), "Filtered ledger aggregates did not finish rendering");
  record("model filtering reaches ledger totals, modules, friends, records and timeline", ["summary", "modules", "friends", "records", "timeline"].every(route => audit.usage.some(call => call.route === route && call.model === "synthetic-other")));
  const beforeSelect = audit.usage.length;
  await viewerEval("document.querySelector('.usage-chart-scroll').focus(); document.querySelector('.usage-chart-scroll').dispatchEvent(new KeyboardEvent('keydown', {key:'Home', bubbles:true}))");
  await waitFor(() => viewerEval("Array.from(document.querySelectorAll('#modelUsageSelection button')).some(node => node.textContent === '按分钟查看这一天') && document.getElementById('modelUsageSummary').textContent.includes('50 次')"), "Shared chart Home key did not select a day and update summary");
  record("real renderer date selection updates totals without rebuilding timeline", audit.usage.slice(beforeSelect).some(call => call.route === "summary" && call.since && call.until) && !audit.usage.slice(beforeSelect).some(call => call.route === "timeline"));
  const dayTitle = await viewerEval("document.querySelector('#modelUsageSelection strong').textContent");
  await viewerEval("Array.from(document.querySelectorAll('#modelUsageSelection button')).find(node => node.textContent === '按分钟查看这一天').click()");
  await waitFor(() => viewerEval("Array.from(document.querySelectorAll('#modelUsageTrendControls button')).some(node => node.textContent === '返回') && document.querySelector('.usage-chart-scroll')?.getAttribute('aria-label').includes('1440 个周期')"), "Minute drill-down did not render a complete day");
  record("minute drill-down queries every calendar bucket through chunked timeline endpoints", audit.usage.some(call => call.route === "timeline" && call.boundaries.split(",").length === 1001) && audit.usage.some(call => call.route === "timeline" && call.boundaries.split(",").length === 441));
  await captureUsage("model-usage-window-smoke-minutes.png");
  record("minute view initially reveals reported activity while retaining full-day totals", await viewerEval(`(() => { const scroller = document.querySelector('.usage-chart-scroll'), track = document.querySelector('.usage-chart-track'); const slot = (parseFloat(track.style.width) - 56) / 1440, at = 44 + 600 * slot - scroller.scrollLeft; return at >= 44 && at + slot <= scroller.clientWidth && document.getElementById('modelUsageSummary').textContent.includes('50 次') && document.querySelector('#modelUsageSelection strong').textContent === KyfTimeTrend.calendar.title(${pastDay(-2)}, ${pastDay(-1)}, 'minute'); })()`));
  await viewerEval("Array.from(document.querySelectorAll('#modelUsageTrendControls button')).find(node => node.textContent === '返回').click()");
  await waitFor(() => viewerEval("Array.from(document.querySelectorAll('#modelUsageTrendControls button')).some(node => node.textContent === '天' && node.getAttribute('aria-pressed') === 'true') && document.querySelector('#modelUsageSelection strong')?.textContent === " + JSON.stringify(dayTitle)), "Returning from minutes lost the selected day or granularity");
  record("minute return retains the selected day and preferred granularity");
  await viewerEval("Array.from(document.querySelectorAll('#modelUsageModules .model-usage-step')).find(node => node.textContent.includes('助手回答')).click()");
  await waitFor(() => audit.usage.some(call => call.route === "records" && call.plugin === "kyf.advisor" && call.step === "advisor" && call.model === "synthetic-other" && call.since && call.until), "Module step filter did not reach call records");
  await waitFor(() => viewerEval("!document.getElementById('modelUsageStep').hidden && document.querySelector('#modelUsageRecords .model-usage-record')?.textContent.includes('助手回答')"), "Step-filtered renderer did not finish updating");
  await viewerEval("document.getElementById('modelCallsRecordsTab').click()");
  record("records tab retains time, model, module and step filters", await viewerEval("document.getElementById('modelUsageStatsView').hidden && !document.getElementById('modelUsageRecordsView').hidden && document.getElementById('modelUsageModel').value === 'synthetic-other' && document.getElementById('modelUsagePlugin').value === 'kyf.advisor' && document.getElementById('modelUsageStep').textContent.includes('助手回答')"));
  record("records use compact summary while accounting remains in the footer", await viewerEval(`(() => {
    const summary = document.getElementById('modelUsageCompactSummary'), accounting = document.getElementById('modelUsageAccounting');
    return !summary.hidden && document.getElementById('modelUsageSummary').hidden && summary.textContent.includes('50') &&
      !!accounting.closest('footer') && accounting.textContent.includes('失败、取消及每次重试');
  })()`));
  record("dense grid highlights friend and total tokens in 34-pixel rows", await viewerEval(`(() => {
    const row = document.querySelector('#modelUsageRecords .model-usage-record');
    const friend = row.querySelector('.usage-record-friend'), total = row.querySelector('.usage-record-total');
    return row.tagName === 'TR' && row.children.length === 8 && row.getBoundingClientRect().height <= 36 &&
      friend.textContent.includes('合成好友') && total.textContent.includes('47') &&
      Number(getComputedStyle(friend).fontWeight) >= 600 && Number(getComputedStyle(total).fontWeight) >= 600 &&
      Number(getComputedStyle(row.querySelector('.usage-record-time')).fontWeight) < Number(getComputedStyle(friend).fontWeight);
  })()`));
  await captureUsage("model-usage-window-smoke-records.png");
  const readsBeforeDisclosure = audit.usage.length;
  await viewerEval("document.querySelector('#modelUsageRecords .model-usage-record').click()");
  record("clicking a record expands details without reloading usage", await viewerEval(`(() => {
    const row = document.querySelector('#modelUsageRecords .model-usage-record'), detail = row.nextElementSibling;
    return row.querySelector('.model-usage-record-toggle').getAttribute('aria-expanded') === 'true' && detail.classList.contains('model-usage-record-detail') &&
      !detail.hidden && detail.textContent.includes('输入') && detail.textContent.includes('输出') && detail.textContent.includes('synthetic-other');
  })()`) && audit.usage.length === readsBeforeDisclosure);
  await captureUsage("model-usage-window-smoke-record-expanded.png");
  await viewerEval("Array.from(document.querySelectorAll('#modelUsageRecords .model-usage-record-tab')).find(node => node.textContent === '过程与错误').click()");
  await waitFor(() => viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('诊断元数据')"), "Process tab did not load the captured attempt");
  record("process tab combines the ledger timeline with captured metadata", await viewerEval("(() => { const detail = document.querySelector('#modelUsageRecords .model-usage-record-detail:not([hidden])'); return detail.textContent.includes('请求发出') && detail.textContent.includes('1.0 秒后 · 成功') && !!detail.querySelector('.model-traces-viewer details'); })()"));
  await captureUsage("model-usage-window-smoke-record-process.png");
  await viewerEval("Array.from(document.querySelectorAll('#modelUsageRecords .model-usage-record-tab')).find(node => node.textContent === '请求').click()");
  await waitFor(() => viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('SYNTHETIC BODY synthetic-account-a-synthetic-contact-a')"), "Record detail did not show its captured request");
  record("record detail shows its captured request in place without a separate diagnostics page", await viewerEval("!document.getElementById('modelUsagePanel').hidden && !document.getElementById('modelTracesPanel') && !document.getElementById('modelCallsDiagnosticsTab') && document.getElementById('modelCallsRecordsTab').getAttribute('aria-selected') === 'true'"));
  await captureUsage("model-usage-window-smoke-record-request.png");
  await viewerEval("document.getElementById('modelCallsStatsTab').click()");
  await waitFor(() => viewerEval("document.getElementById('modelUsagePanel').getAttribute('aria-busy') !== 'true' && document.querySelector('.usage-chart canvas')?.width > 0"), "Returning to statistics did not settle usage");
  await viewerEval("document.getElementById('modelUsagePanel').scrollTop = 0");
  await captureUsage("model-usage-window-smoke.png");
  viewer.setContentSize(600, 480);
  await delay(100);
  await captureUsage("model-usage-window-smoke-minimum.png");
  const minimumControls = await viewerEval(`(() => {
    const ids = ['modelUsageFriend', 'modelUsagePlugin', 'modelUsageModel', 'modelUsageStatus', 'modelCallsNavigation'];
    return { width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth, controls: ids.map(id => {
      const rect = document.getElementById(id).getBoundingClientRect();
      return { id, width: rect.width, left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
    }) };
  })()`);
  audit.minimumControls = minimumControls;
  record("minimum-size usage window keeps filters and unified navigation within its viewport", minimumControls.scrollWidth <= minimumControls.width + 1 && minimumControls.controls.every(rect => rect.width > 0 && rect.left >= 0 && rect.right <= minimumControls.width + 1 && rect.top >= 0 && rect.bottom <= minimumControls.height));
  usageLayouts.push(await viewerEval("({mode:'light-content-600x480',width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,panelScrollWidth:document.getElementById('modelUsagePanel').scrollWidth,panelWidth:document.getElementById('modelUsagePanel').clientWidth})"));
  viewer.setSize(...viewer.getMinimumSize()); await delay(100);
  const minimumUsage = await viewerEval(`(() => {
    const panel = document.getElementById('modelUsagePanel'), navigation = document.getElementById('modelCallsNavigation').getBoundingClientRect();
    return {mode:'light-native-minimum',width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,
      panelScrollWidth:panel.scrollWidth,panelWidth:panel.clientWidth,navigationVisible:navigation.top >= 0 && navigation.bottom <= innerHeight && navigation.right <= innerWidth};
  })()`);
  usageLayouts.push(minimumUsage);
  record("native minimum usage size has no horizontal overflow and keeps navigation visible", minimumUsage.scrollWidth <= minimumUsage.width + 1 && minimumUsage.panelScrollWidth <= minimumUsage.panelWidth + 1 && minimumUsage.navigationVisible);
  await captureUsage("model-usage-window-smoke-native-minimum.png");
  await viewerEval("document.getElementById('modelCallsRecordsTab').click(); document.getElementById('modelUsagePanel').scrollTop = 0");
  record("minimum records grid keeps friend and total visible while compacting secondary columns", await viewerEval(`(() => {
    const row = document.querySelector('#modelUsageRecords .model-usage-record'), friend = row.querySelector('.usage-record-friend'), total = row.querySelector('.usage-record-total');
    const inside = element => { const rect = element.getBoundingClientRect(); return rect.width > 0 && rect.left >= 0 && rect.right <= innerWidth; };
    return inside(friend) && inside(total) && row.getBoundingClientRect().height <= 36 &&
      getComputedStyle(row.querySelector('.usage-record-module')).display === 'none' &&
      getComputedStyle(row.querySelector('.usage-record-duration')).display === 'none' && document.getElementById('modelUsagePanel').scrollWidth <= innerWidth;
  })()`));
  await captureUsage("model-usage-window-smoke-records-minimum.png");
  await viewerEval("document.getElementById('modelCallsStatsTab').click()");
  await viewerEval(`(() => { const panel = document.getElementById('modelUsagePanel'), chart = document.querySelector('.usage-chart'); panel.scrollTop += chart.getBoundingClientRect().top - 70; })()`);
  record("minimum usage size keeps the real chart reachable through vertical scrolling", await viewerEval("(() => { const rect = document.querySelector('.usage-chart-scroll').getBoundingClientRect(); return rect.top >= 0 && rect.bottom <= innerHeight && rect.left >= 0 && rect.right <= innerWidth && rect.height >= 170; })()"));
  record("navigation stays fixed while usage scrolls", await viewerEval("(() => { const nav = document.getElementById('modelCallsNavigation').getBoundingClientRect(); return nav.top >= 0 && nav.bottom <= 60 && document.getElementById('modelUsagePanel').scrollTop > 0; })()"));
  await captureUsage("model-usage-window-smoke-minimum-chart.png");
  await viewerEval("document.getElementById('modelUsagePanel').scrollTop = 0");
  viewer.setContentSize(960, 760);
  await viewerEval("document.getElementById('modelCallsRecordsTab').click()");
  await waitFor(() => viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('SYNTHETIC BODY synthetic-account-a-synthetic-contact-a')"), "Expanded record lost its captured request across pages");
  record("viewer is an independent resizable native window", viewer.isResizable() && !viewer.getParentWindow() && viewer !== mainWindow);
  record("viewer received the light theme", await viewerEval("document.body.classList.contains('theme-light')"));
  record("main draft and DOM focus survive window opening", await mainEval("document.getElementById('mainDraft').value === 'Synthetic unsent draft' && document.activeElement.id === 'mainDraft'"));
  record("main bounds and content remain unchanged", JSON.stringify(mainWindow.getBounds()) === JSON.stringify(mainBounds) && await mainEval("!document.getElementById('modelTracesPanel')"));
  record("detail and body read through authenticated HTTP and real IPC without a separate list", audit.lists === 0 && audit.details > 0 && audit.bodies > 0);
  const firstViewerId = viewer.id;
  record("repeated opening reuses the same viewer", manager.open(context) && BrowserWindow.getAllWindows().length === 2 && viewer.id === firstViewerId);
  await mainEval("ModelTraces.setScope('synthetic-account-a','synthetic-contact-a','dashboard')");
  await waitFor(() => viewerEval("modelTracesWindow.getContext().then(context => context.view === 'dashboard')"), "Main view context did not synchronize");
  record("changing the main view keeps the viewer and opened record body", !viewer.isDestroyed() && await viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('SYNTHETIC BODY synthetic-account-a-synthetic-contact-a')"));
  viewer.setContentSize(720, 700);
  // Windows display scaling can round native DIPs to a neighboring renderer pixel.
  await waitFor(() => viewerEval("Math.abs(window.innerWidth - 720) <= 2"), "Viewer did not resize independently");
  record("resized viewer has no document horizontal overflow", await viewerEval("document.documentElement.scrollWidth <= innerWidth + 1"));
  record("resizing viewer leaves main bounds unchanged", JSON.stringify(mainWindow.getBounds()) === JSON.stringify(mainBounds));
  for (const mode of ["content-600x480", "native-minimum"]) {
    if (mode === "content-600x480") viewer.setContentSize(600, 480);
    else viewer.setSize(...viewer.getMinimumSize());
    await delay(100);
    await viewerEval("document.getElementById('modelUsagePanel').scrollTop = 0");
    const layout = await viewerEval(`(() => {
      const bounds = id => { const element = document.getElementById(id); const rect = element.getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, height: rect.height, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight, overflowY: getComputedStyle(element).overflowY }; };
      return { width: innerWidth, height: innerHeight, documentScrollHeight: document.documentElement.scrollHeight, documentScrollWidth: document.documentElement.scrollWidth,
        bodyOverflowY: getComputedStyle(document.body).overflowY, panel: bounds('modelUsagePanel'), navigation: bounds('modelCallsNavigation') };
    })()`);
    const reachedDetail = await viewerEval(`(() => {
      const panel = document.getElementById('modelUsagePanel'), body = document.querySelector('#modelUsageRecords .model-traces-viewer pre');
      body.scrollIntoView({ block: 'nearest' }); const rect = body.getBoundingClientRect();
      return { panelScrollTop: panel.scrollTop, top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, height: rect.height,
        bodyVisible: rect.top >= 0 && rect.bottom <= innerHeight + 1 && rect.left >= 0 && rect.right <= innerWidth + 1 && panel.scrollWidth <= panel.clientWidth + 1 };
    })()`);
    minimumLayouts.push({ mode, nativeBounds: viewer.getBounds(), nativeContentSize: viewer.getContentSize(), ...layout, reachedDetail });
    await delay(75);
    fs.writeFileSync(path.join(outputDirectory, `model-traces-window-smoke-${mode}.png`), (await viewer.webContents.capturePage()).toPNG());
    await viewerEval("document.getElementById('modelUsagePanel').scrollTop = 0");
  }
  for (const layout of minimumLayouts) {
    record(`${layout.mode} keeps unified navigation visible`, layout.navigation.top >= 0 && layout.navigation.bottom <= layout.height && layout.navigation.right <= layout.width);
    record(`${layout.mode} keeps the expanded record body reachable by scrolling`, layout.reachedDetail.bodyVisible && layout.reachedDetail.height > 0);
    record(`${layout.mode} has no inaccessible page overflow`, layout.documentScrollWidth <= layout.width + 1 && layout.documentScrollHeight <= layout.height + 1);
  }
  viewer.setContentSize(960, 760);
  await mainEval("document.body.classList.remove('theme-light'); ModelTraces.setScope('synthetic-account-a','synthetic-contact-b','chat');");
  await waitFor(() => viewerEval("!document.body.classList.contains('theme-light')"), "Theme context did not synchronize");
  record("main contact changes preserve the opened record body", await viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('SYNTHETIC BODY synthetic-account-a-synthetic-contact-a')"));
  await viewerEval("document.getElementById('modelCallsStatsTab').click()");
  await waitFor(() => viewerEval("!document.getElementById('modelUsagePanel').hidden && document.querySelector('.usage-chart canvas')?.width > 0"), "Dark usage view did not reopen");
  await captureUsage("model-usage-window-smoke-dark.png");
  viewer.setContentSize(600, 480); await delay(100);
  const darkLayout = await viewerEval("({mode:'dark-content-600x480',width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,panelScrollWidth:document.getElementById('modelUsagePanel').scrollWidth,panelWidth:document.getElementById('modelUsagePanel').clientWidth})");
  usageLayouts.push(darkLayout);
  record("dark minimum usage size has no horizontal overflow", darkLayout.scrollWidth <= darkLayout.width + 1 && darkLayout.panelScrollWidth <= darkLayout.panelWidth + 1);
  await captureUsage("model-usage-window-smoke-dark-minimum.png");
  viewer.setContentSize(960, 760);
  await viewerEval("document.getElementById('modelCallsRecordsTab').click(); document.getElementById('modelUsagePanel').scrollTop = 0; document.querySelector('#modelUsageRecords .model-usage-record-toggle[aria-expanded=true]')?.click()");
  await captureUsage("model-usage-window-smoke-dark-records.png");
  const bodiesBeforeReopen = audit.bodies;
  await viewerEval("document.querySelector('#modelUsageRecords .model-usage-record').click()");
  await waitFor(() => viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('SYNTHETIC BODY synthetic-account-a-synthetic-contact-a')"), "Reopened record did not reload its request");
  record("a collapsed record releases its body and reopens on the tab it showed", audit.bodies > bodiesBeforeReopen);
  fixtureAccount = "synthetic-account-b";
  await mainEval("ModelTraces.setScope('synthetic-account-b','synthetic-contact-c','chat');");
  await waitFor(() => viewerEval("!!document.querySelector('#modelUsageRecords .model-usage-record') && !document.body.textContent.includes('SYNTHETIC BODY')"), "Account context did not synchronize");
  record("account change clears prior account content", await viewerEval("!document.body.textContent.includes('synthetic-account-a') && !document.querySelector('#modelUsageRecords .model-usage-record-detail:not([hidden])')"));
  await viewerEval("document.querySelector('#modelUsageRecords .model-usage-record').click()");
  await viewerEval("Array.from(document.querySelectorAll('#modelUsageRecords .model-usage-record-tab')).find(node => node.textContent === '响应').click()");
  await waitFor(() => viewerEval("document.querySelector('#modelUsageRecords .model-traces-viewer')?.textContent.includes('SYNTHETIC RESPONSE synthetic-account-b-synthetic-contact-a')"), "New account response body not loaded");
  record("account change remints the viewer session and reads only the new scope", audit.sessions >= 3 && audit.rejected === 0);
  await delay(100);
  fs.writeFileSync(screenshotPath, (await viewer.webContents.capturePage()).toPNG());
  await viewerEval("document.getElementById('modelCallsStatsTab').click()");
  await waitFor(() => viewerEval("!document.getElementById('modelUsagePanel').hidden"), "Records did not return to statistics");
  viewer.close();
  await waitFor(() => viewer.isDestroyed(), "Native close did not close the viewer");
  record("user close leaves main draft intact", await mainEval("document.getElementById('mainDraft').value === 'Synthetic unsent draft' && !document.getElementById('modelTracesPanel')"));
  // Electron's userGesture option exercises the production preload activation gate.
  await mainWindow.webContents.executeJavaScript("document.getElementById('btnModelTraces').click()", true);
  await waitFor(() => { viewer = BrowserWindow.getAllWindows().find(window => window !== mainWindow); return viewer && viewer.id !== firstViewerId; }, "Toolbar did not reopen the standalone native window");
  await waitFor(() => viewerEval("!!document.querySelector('#modelUsageRecords .model-usage-record')"), "Reopened viewer usage did not load");
  record("main toolbar reopens a closed viewer through the real preload", viewer.id !== firstViewerId);
  const beforeReload = viewer;
  fixtureAccount = "synthetic-account-a";
  await mainWindow.loadURL(mainUrl);
  record("main reload closes the old viewer", beforeReload.isDestroyed() && BrowserWindow.getAllWindows().length === 1);
  await waitFor(() => mainEval("document.getElementById('btnDeveloperMode').textContent === '已开启'"), "Main settings did not recover after reload");
  await mainWindow.webContents.executeJavaScript("document.getElementById('btnModelTraces').click()", true);
  await waitFor(() => { viewer = BrowserWindow.getAllWindows().find(window => window !== mainWindow); return !!viewer; }, "Viewer did not reopen after main reload");
  await waitFor(() => viewerEval("!!document.querySelector('#modelUsageRecords .model-usage-record')"), "Reloaded viewer usage did not load");
  record("main reload keeps the independent viewer feature usable", viewer.id !== beforeReload.id);
  mainWindow.destroy();
  await waitFor(() => viewer.isDestroyed(), "Main teardown left the viewer alive");
  record("main teardown closes the standalone viewer and IPC ownership", manager.open(context) === false);
  record("smoke performs no model calls", audit.modelRequests === 0);
  record("production renderer completes without console errors", audit.rendererErrors.length === 0);
  await finish();
}).catch(finish);
