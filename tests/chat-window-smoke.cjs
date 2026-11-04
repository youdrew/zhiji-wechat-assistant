"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Opt-in production-renderer smoke of the popped-out chat window (2026-11-04), isolated from the
// user's services and app profile: the real index.html, renderer and preload in a main window and a
// chat window on one synthetic loopback API. The test plays the main process for chat-window:open.
// Run: node_modules/electron/dist/electron.exe tests/chat-window-smoke.cjs
const { app, BrowserWindow, ipcMain } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const output = path.join(root, ".local", "tmp", "chat-window-smoke");
fs.mkdirSync(output, { recursive: true });
app.setPath("userData", fs.mkdtempSync(path.join(output, "profile-")));
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-background-timer-throttling");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
const checks = [], screenshots = [];
const audit = { requests: [], unexpected: [], rendererErrors: [], externalRequests: [], opens: [] };
const account = "synthetic-chat-account", friend = "synthetic-friend", other = "synthetic-other";
let mainWindow, chatWindow, server, origin, timeout, finished = false;
const sessions = [
  { username: other, name: "另一位好友", time: 2000, sortTimestamp: 2000, preview: "最近", unreadCount: 0 },
  { username: friend, name: "合成好友", time: 1000, sortTimestamp: 1000, preview: "在吗", unreadCount: 2, lastMessageFromSelf: false },
];
const messages = {
  [friend]: [{ id: 1, side: "other", kind: "text", text: "在吗", time: 1000 }, { id: 2, side: "self", kind: "text", text: "在的", time: 1001 }],
  [other]: [{ id: 3, side: "other", kind: "text", text: "最近", time: 2000 }],
};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(predicate, message, limit = 10000) {
  const start = Date.now();
  while (Date.now() - start < limit) { if (await predicate()) return; await delay(25); }
  throw new Error(message);
}
const evaluate = (window, expression) => window.webContents.executeJavaScript(expression, true);
function check(name, condition = true) { assert.ok(condition, name); checks.push(name); }
async function screenshot(window, name) {
  await delay(120);
  const file = path.join(output, name + ".png"); fs.writeFileSync(file, (await window.webContents.capturePage()).toPNG()); screenshots.push(file);
}
const typeInto = (window, value) => evaluate(window, `(() => { const input = document.getElementById('chatInput');
  input.value = ${JSON.stringify(value)}; input.dispatchEvent(new Event('input', { bubbles: true })); })()`);
const inputValue = window => evaluate(window, "document.getElementById('chatInput').value");
async function serve(request, response) {
  const url = new URL(request.url, "http://127.0.0.1");
  const json = (code, body) => { response.writeHead(code, { "Content-Type": "application/json; charset=utf-8" }); response.end(JSON.stringify(body)); };
  if (url.pathname.startsWith("/api/")) {
    audit.requests.push({ path: url.pathname + url.search, method: request.method });
    let raw = ""; for await (const chunk of request) raw += chunk.toString();
    const body = raw ? JSON.parse(raw) : null;
    if (url.pathname === "/api/health") return json(200, { data: { state: "ready" } });
    if (url.pathname === "/api/sessions") return json(200, { account, messagesReady: true, self: { name: "合成账号" }, sessions });
    if (url.pathname === "/api/conversation-selection") return json(200, { account, initialized: true, selectedSessions: [friend, other] });
    if (url.pathname === "/api/messages/batch") return json(200, { account,
      windows: body.users.map(user => ({ user, messages: messages[user] || [], hasMoreBefore: false })) });
    if (url.pathname === "/api/messages") {
      const user = url.searchParams.get("user");
      return json(200, { account, messages: messages[user] || [], hasMoreBefore: false });
    }
    if (url.pathname === "/api/plugins") return json(200, { plugins: [], problems: [] });
    if (url.pathname === "/api/attention") return json(200, { account, items: [] });
    if (url.pathname === "/api/model-source") return json(200, { mode: "none", status: "unset", models: [], roles: {} });
    audit.unexpected.push(url.pathname); return json(404, { message: "No synthetic route" });
  }
  const resource = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
  const base = resource.startsWith("plugin-sdk/") ? root : path.join(root, "chatui");
  const file = path.resolve(base, resource);
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return json(404, { message: "No synthetic resource" });
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png" };
  response.writeHead(200, { "Content-Type": `${mime[path.extname(file)] || "application/octet-stream"}; charset=utf-8` }); response.end(fs.readFileSync(file));
}
function createWindow(args) {
  const window = new BrowserWindow({ width: 900, height: 700, show: false, webPreferences: {
    preload: path.join(root, "scripts", "real-client-preload.cjs"), additionalArguments: args,
    contextIsolation: true, sandbox: true, nodeIntegration: false, offscreen: true, backgroundThrottling: false,
  } });
  window.webContents.on("console-message", details => {
    if (details.level === "error" && !details.message.includes("favicon")) audit.rendererErrors.push(details.message);
  });
  return window;
}
async function finish(error) {
  if (finished) return; finished = true; clearTimeout(timeout);
  const report = { passed: !error, checks, audit, screenshots, electron: process.versions.electron,
    fixture: "Production index.html, renderer and preload in a main and a chat window, isolated loopback API, hidden offscreen windows",
    ...(error ? { error: String(error.stack || error) } : {}) };
  fs.writeFileSync(path.join(output, "report.json"), JSON.stringify(report, null, 2));
  for (const window of [chatWindow, mainWindow]) if (window && !window.isDestroyed()) window.destroy();
  server?.closeAllConnections(); server?.close();
  process.stdout.write(JSON.stringify({ passed: report.passed, checks: checks.length, report: path.join(output, "report.json") }) + "\n");
  if (error) process.stderr.write(String(error.stack || error) + "\n");
  app.exit(error ? 1 : 0);
}
process.on("uncaughtException", finish); process.on("unhandledRejection", finish);
void app.whenReady().then(async () => {
  timeout = setTimeout(() => { void finish(new Error("Chat window smoke timed out")); }, 90000);
  server = http.createServer((req, res) => { void serve(req, res).catch(error => { res.destroy(); void finish(error); }); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  ipcMain.handle("real-client:app-version", () => "1.3.6-synthetic");
  ipcMain.handle("external-import:notify", () => true);
  ipcMain.handle("model-traces:request", (_event, operation, args) => ({ ok: true, scope: args?.scope, accountId: account, deleteVersion: 0,
    state: { enabled: false, collecting: false, status: "off", retentionDays: 7, recordCount: 0, storageBytes: 0, pauseHistory: [] } }));
  ipcMain.handle("chat-window:open", async (_event, scope) => {
    audit.opens.push(scope);
    if (chatWindow && !chatWindow.isDestroyed()) return true;
    chatWindow = createWindow(["--kyf-chat-window"]);
    const query = new URLSearchParams({ window: "chat", account: scope.account, user: scope.user }).toString();
    await chatWindow.loadURL(`${origin}/?${query}`);
    return true;
  });
  mainWindow = createWindow([]);
  mainWindow.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    const allowed = details.url.startsWith(origin + "/") || details.url.startsWith("devtools:");
    if (!allowed) audit.externalRequests.push(details.url);
    callback({ cancel: !allowed });
  });
  await mainWindow.loadURL(origin + "/");
  await waitFor(() => evaluate(mainWindow, `document.querySelectorAll('#sessionList .session-item').length === 2 && document.getElementById('chatTitle').textContent === '另一位好友'`), "Main window did not open its first conversation");
  const badge = () => evaluate(mainWindow, `document.querySelector('#sessionList .session-item[data-id="${friend}"] .session-unread-dot')?.textContent || ''`);
  check("main window shows the friend's unread badge", await badge() === "2");

  await evaluate(mainWindow, `document.querySelector('#sessionList .session-item[data-id="${friend}"]').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))`);
  await waitFor(() => chatWindow && !chatWindow.isDestroyed() && !chatWindow.webContents.isLoading(), "Double-click did not ask the host to open a chat window");
  check("double-click asks the host for exactly this account and conversation", JSON.stringify(audit.opens) === JSON.stringify([{ account, user: friend }]));
  await waitFor(() => evaluate(chatWindow, `document.getElementById('chatTitle').textContent === '合成好友' && document.querySelectorAll('#chatMessages .msg-item').length === 2`), "Chat window did not render its conversation");
  check("the chat window shows only the chat pane", await evaluate(chatWindow, `document.documentElement.classList.contains('chat-window') &&
    getComputedStyle(document.querySelector('.nav-rail')).display === 'none' && getComputedStyle(document.getElementById('sessionPane')).display === 'none' &&
    getComputedStyle(document.getElementById('databaseLoading')).display === 'none' && document.getElementById('startupOverlay').hidden &&
    !document.getElementById('appWindow').hasAttribute('inert')`));
  check("the chat pane fills the window and keeps its title-bar drag strip", await evaluate(chatWindow, `(() => {
    const pane = document.querySelector('.chat-pane').getBoundingClientRect(), bar = document.querySelector('.desktop-titlebar');
    return pane.left === 0 && Math.abs(pane.width - innerWidth) < 2 && getComputedStyle(bar).webkitAppRegion === 'drag' && bar.getBoundingClientRect().height > 0;
  })()`));
  check("the window title is the conversation and the URL is untouched", await evaluate(chatWindow, `document.title === '合成好友' &&
    location.search === ${JSON.stringify("?" + new URLSearchParams({ window: "chat", account, user: friend }))}`));
  check("the chat window's composer is usable", await evaluate(chatWindow, `!document.getElementById('chatInput').disabled`));
  await waitFor(async () => await badge() === "", "Reading in the chat window did not clear the main window's badge");
  check("reading in the chat window clears the main window's unread badge");
  check("the main window stays on its own conversation", await evaluate(mainWindow, `document.getElementById('chatTitle').textContent === '另一位好友'`));
  check("the chat window never remembers a last conversation or follows new ones", await evaluate(chatWindow,
    `localStorage.getItem('last-conversation:${account}') !== '${friend}'`) &&
    !audit.requests.some(item => item.method === "POST" && item.path.startsWith("/api/conversation-selection")));

  await typeInto(chatWindow, "来自独立窗口的草稿");
  await evaluate(mainWindow, `document.querySelector('#sessionList .session-item[data-id="${friend}"]').click()`);
  await waitFor(async () => await inputValue(mainWindow) === "来自独立窗口的草稿", "Main window did not receive the existing draft on opening the conversation");
  check("opening the conversation in the main window adopts the chat window's draft");
  await typeInto(mainWindow, "主窗口改了草稿");
  await waitFor(async () => await inputValue(chatWindow) === "主窗口改了草稿", "Chat window did not follow the main window's edit");
  check("an edit in the main window reaches the chat window");
  await typeInto(chatWindow, "");
  await waitFor(async () => await inputValue(mainWindow) === "", "Clearing in the chat window did not reach the main window");
  check("clearing the draft propagates");
  check("no chat text entered browser storage", !(await evaluate(chatWindow,
    `Object.keys(localStorage).some(key => /草稿/.test(localStorage.getItem(key)))`)) && !(await evaluate(mainWindow,
    `Object.keys(localStorage).some(key => /草稿/.test(localStorage.getItem(key)))`)));
  await typeInto(chatWindow, "截图用的草稿");
  await screenshot(chatWindow, "chat-window-dark");
  await evaluate(chatWindow, `(() => { const select = document.getElementById('selectThemeMode'); select.value = 'light'; select.dispatchEvent(new Event('change')); })()`);
  await waitFor(() => evaluate(mainWindow, `document.body.classList.contains('theme-light')`), "Theme did not follow across windows");
  await screenshot(chatWindow, "chat-window-light");
  await screenshot(mainWindow, "main-window-light");
  check("no unknown API requests", audit.unexpected.length === 0);
  check("no external network requests", audit.externalRequests.length === 0);
  check("production renderers have no uncaught errors", audit.rendererErrors.length === 0);
  await finish();
}).catch(finish);
