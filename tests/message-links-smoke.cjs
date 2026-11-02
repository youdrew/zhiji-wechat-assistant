"use strict";
// Copyright 2026 Zhiji WeChat Assistant contributors. SPDX-License-Identifier: Apache-2.0
// Full production renderer/preload with synthetic data; no running Core or WeChat access.
// Run: node_modules/electron/dist/electron.exe tests/message-links-smoke.cjs
const { app, BrowserWindow, ipcMain } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), http = require("node:http");
const { attachChatLinks } = require("../scripts/chat-link-host.cjs");
const root = path.resolve(__dirname, ".."), output = path.join(root, ".local", "tmp", "message-links-smoke");
fs.mkdirSync(output, { recursive: true });
app.setPath("userData", fs.mkdtempSync(path.join(output, "profile-")));
app.commandLine.appendSwitch("disable-gpu");
const account = "synthetic-account", user = "synthetic-friend";
const messages = [
  { id: "a".repeat(64), side: "other", kind: "other", text: "合成分享：杭州散步路线", link: {
    title: "合成分享：杭州散步路线", description: "走进山间，看看秋天。\n分享内容仅用于界面验证。", source: "小红书", url: "https://example.com/note?from=wechat&item=1" } },
  { id: "b".repeat(64), side: "self", kind: "text", text: "周末可以看看 https://example.org/walk_(autumn)。\n这是一条合成消息。" },
  { id: "c".repeat(64), side: "other", kind: "other", text: "合成小程序分享", link: {
    title: "合成小程序分享", description: "这条分享没有网页地址。", source: "小程序", url: null } },
  { id: "d".repeat(64), side: "other", kind: "other", text: "无效链接", link: {
    title: "无效链接 <img src=x onerror=alert(1)>", source: "合成来源", url: "javascript:alert(1)" } },
  { id: "e".repeat(64), side: "other", kind: "text", text: "普通文字仍然正常显示。" },
  { id: "f".repeat(64), side: "other", kind: "image", text: "[图片]", media: { kind: "image", width: 24, height: 24 } },
].map((message, index) => ({ time: 1792707600000 + index * 1000, ...message }));
const audit = { checks: [], opened: [], unexpected: [], errors: [], screenshots: [] };
let window, server, timer, finished = false;
const evaluate = code => window.webContents.executeJavaScript(code, true);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(predicate, message) {
  for (let n = 0; n < 200; n++) { if (await predicate()) return; await delay(25); }
  throw new Error(message);
}
function check(name, condition) { assert.ok(condition, name); audit.checks.push(name); }
async function serve(request, response) {
  const url = new URL(request.url, "http://127.0.0.1");
  const json = value => { response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify(value)); };
  if (url.pathname.startsWith("/api/")) {
    let raw = ""; for await (const chunk of request) raw += chunk.toString();
    const body = raw ? JSON.parse(raw) : {};
    if (url.pathname === "/api/health") return json({ data: { state: "ready" } });
    if (url.pathname === "/api/sessions") return json({ account, messagesReady: true, sessions: [{ username: user, name: "合成好友" }], self: { name: "合成账号" } });
    if (url.pathname === "/api/conversation-selection") return json({ account, initialized: true, selectedSessions: [user] });
    if (url.pathname === "/api/messages/batch") return json({ account, windows: body.users.map(user => ({ user, messages, hasMoreBefore: false })) });
    if (url.pathname === "/api/messages") return json({ account, messages, hasMoreBefore: false });
    if (url.pathname === "/api/plugins") return json({ plugins: [], problems: [] });
    if (url.pathname === "/api/model-source") return json({ mode: "api", sourceId: null, status: "not-configured", api: {} });
    if (url.pathname === "/api/media/settings") return json({ account, mode: "reference", cacheMaxBytes: 536870912, cacheBytes: 0, archiveBytes: 0 });
    if (url.pathname === "/api/media") { response.writeHead(200, { "Content-Type": "image/png" }); response.end(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ1kAAAAASUVORK5CYII=", "base64")); return; }
    audit.unexpected.push([request.method, url.pathname]); return json({});
  }
  const resource = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
  const base = resource.startsWith("plugin-sdk/") ? root : path.join(root, "chatui"), file = path.resolve(base, resource);
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { response.writeHead(404); response.end(); return; }
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png" };
  response.writeHead(200, { "Content-Type": `${mime[path.extname(file)] || "application/octet-stream"}; charset=utf-8` }); response.end(fs.readFileSync(file));
}
async function finish(error) {
  if (finished) return; finished = true; clearTimeout(timer);
  fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({ passed: !error, ...audit, error: error ? String(error.stack || error) : null }, null, 2));
  if (window && !window.isDestroyed()) window.destroy();
  server?.closeAllConnections(); server?.close();
  process.stdout.write(JSON.stringify({ passed: !error, checks: audit.checks.length, report: path.join(output, "report.json") }) + "\n");
  if (error) process.stderr.write(String(error.stack || error) + "\n");
  app.exit(error ? 1 : 0);
}
process.on("uncaughtException", finish); process.on("unhandledRejection", finish);
void app.whenReady().then(async () => {
  timer = setTimeout(() => { void finish(new Error("Links smoke timed out")); }, 45000);
  server = http.createServer((req, res) => { void serve(req, res).catch(error => { res.destroy(); void finish(error); }); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  attachChatLinks({ ipcMain, trustedMainFrame: event => event.sender === window.webContents && event.senderFrame === window.webContents.mainFrame,
    shell: { openExternal: async target => { audit.opened.push(target); } } });
  ipcMain.handle("real-client:app-version", () => "1.3.6");
  ipcMain.handle("model-traces:request", () => ({ ok: false, error: "synthetic-unavailable", statusCode: 503 }));
  ipcMain.handle("external-import:notify", () => true);
  window = new BrowserWindow({ width: 1100, height: 940, show: false, webPreferences: {
    preload: path.join(root, "scripts/real-client-preload.cjs"), contextIsolation: true, sandbox: true,
    nodeIntegration: false, offscreen: true, backgroundThrottling: false,
  } });
  window.webContents.on("console-message", details => { if (details.level === "error" && !details.message.includes("favicon")) audit.errors.push(details.message); });
  window.webContents.setWindowOpenHandler(() => { audit.unexpected.push(["popup"]); return { action: "deny" }; });
  window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    const allowed = details.url.startsWith(origin + "/"); if (!allowed) audit.unexpected.push(["external", details.url]); callback({ cancel: !allowed });
  });
  await window.loadURL(origin + "/");
  await waitFor(() => evaluate(`document.querySelector('#startupOverlay').hidden && document.querySelectorAll('#chatMessages .msg-item').length === 6`), "Synthetic chat did not initialize");
  check("cards retain title, summary and source", await evaluate(`(() => { const card = document.querySelector('a.msg-link-card'); return card.querySelector('.msg-link-title').textContent === '合成分享：杭州散步路线' && card.querySelector('.msg-link-description').textContent.includes('走进山间') && card.querySelector('.msg-link-source').textContent.includes('小红书') && card.href === 'https://example.com/note?from=wechat&item=1'; })()`));
  check("ordinary text URLs become links without changing original text", await evaluate(`(() => { const item = document.querySelector('[data-message-id="${"b".repeat(64)}"]'); return item.querySelector('.msg-text-link').href === 'https://example.org/walk_(autumn)' && item.querySelector('.msg-bubble').textContent === ${JSON.stringify(messages[1].text)}; })()`));
  check("missing and unsafe URLs never become clickable cards", await evaluate(`document.querySelectorAll('div.msg-link-card').length === 2 && [...document.querySelectorAll('div.msg-link-card .msg-link-address')].every(node => node.textContent === '链接地址不可用') && !document.querySelector('.msg-link-card img')`));
  check("ordinary text and image retain existing renderers", await evaluate(`document.querySelector('[data-message-id="${"e".repeat(64)}"] .msg-bubble').textContent === '普通文字仍然正常显示。' && Boolean(document.querySelector('[data-message-id="${"f".repeat(64)}"] .chat-media-image'))`));
  check("rendering performs no external opening", audit.opened.length === 0);
  const initialUrl = window.webContents.getURL();
  await evaluate(`document.querySelector('a.msg-link-card').click()`);
  await waitFor(() => audit.opened.length === 1, "Share card did not reach the production opener");
  await evaluate(`document.querySelector('.msg-text-link').click()`);
  await waitFor(() => audit.opened.length === 2, "Text link did not reach the production opener");
  check("only the two explicitly clicked URLs reach browser opening", audit.opened[0] === "https://example.com/note?from=wechat&item=1" && audit.opened[1] === "https://example.org/walk_(autumn)");
  check("chat retains its local URL and messages after clicking", window.webContents.getURL() === initialUrl && await evaluate(`document.querySelectorAll('#chatMessages .msg-item').length === 6`));
  for (const [width, theme] of [[1100, "dark"], [760, "light"]]) {
    window.setContentSize(width, 940);
    await evaluate(`settingsState.settings.zoom = '1'; settingsState.settings.theme = '${theme}'; applySettings(); document.querySelector('#chatMessages').scrollTop = 0;`);
    await delay(100);
    const layout = await evaluate(`(() => { const pane = document.querySelector('#chatMessages').getBoundingClientRect(); return [...document.querySelectorAll('.msg-link-card')].every(node => { const b = node.getBoundingClientRect(); return b.left >= pane.left - 1 && b.right <= pane.right + 1; }); })()`);
    check(`cards fit ${width}px ${theme} theme`, layout);
    const screenshot = path.join(output, `links-${theme}-${width}.png`); fs.writeFileSync(screenshot, (await window.webContents.capturePage()).toPNG()); audit.screenshots.push(screenshot);
  }
  check("no unexpected API, remote preview, navigation or popup", audit.unexpected.length === 0);
  check("production renderer has no uncaught errors", audit.errors.length === 0);
  await finish();
}).catch(finish);
