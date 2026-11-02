"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Hidden real Electron windows/preloads and production sandbox UI; synthetic loopback APIs only.
// Run: node_modules/electron/dist/electron.exe tests/self-analysis-window-smoke.cjs
const { app, BrowserWindow, ipcMain, screen, clipboard, session } = require("electron");
const assert = require("node:assert/strict"), http = require("node:http");
const fs = require("node:fs"), path = require("node:path");
const { createSelfAnalysisWindow } = require("../scripts/self-analysis-window.cjs");
const root = path.resolve(__dirname, ".."), output = path.join(root, ".local/tmp/self-analysis-window-smoke");
fs.mkdirSync(output, { recursive: true });
app.setPath("userData", fs.mkdtempSync(path.join(output, "profile-")));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-background-timer-throttling");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("enable-features", "IsolateSandboxedIframes:grouping/per-document");
const checks = [], requests = [], errors = [], external = [], children = [], screenshots = [];
let main, manager, server, timeout, finished = false, active = "synthetic-account-a", enabled = true;
const hostSource = `
window.smoke = { account:'synthetic-account-a', calls:[], profiles:{
  'synthetic-account-a':{background:'合成账号 A 背景',style:'合成偏好 A',version:1},
  'synthetic-account-b':{background:'合成账号 B 背景',style:'合成偏好 B',version:1}} };
desktopHost.onSelfAnalysisHostCall(async call => {
  smoke.calls.push(call); let result;
  try {
    if(call.scope.account!==smoke.account) throw new Error('synthetic-scope-mismatch');
    if(call.method==='model.status') result={configured:true};
    else if(call.method==='self-profile.read') result={...smoke.profiles[smoke.account]};
    else if(call.method==='self-profile.save') {
      const previous=smoke.profiles[smoke.account];
      if(call.params.account!==smoke.account || call.params.expectedVersion!==previous.version) throw new Error('synthetic-version-mismatch');
      result=smoke.profiles[smoke.account]={background:call.params.background,style:call.params.style,version:previous.version+1};
    } else if(['settings.open','toast'].includes(call.method)) result={ok:true};
    else throw new Error('synthetic-method-not-allowed');
    desktopHost.replySelfAnalysisHostCall(call.id,{result});
  } catch {desktopHost.replySelfAnalysisHostCall(call.id,{error:{code:'synthetic-error',message:'合成宿主校验失败'}});}
});`;
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };
function status(account) {
  return { account, source: { ready: true, canBuild: true, message: "合成来源" },
    build: { state: "complete", phase: "done", processedConversations: 1, totalConversations: 1, messageCount: 4, sampleCount: 2, failedCount: 0 },
    coverage: { complete: true, catalogComplete: true, sampleCount: 2, messageCount: 4, discoveredConversations: 1, indexedConversations: 1, failedConversations: 0 },
    embedding: { state: "ready", ready: true, indexState: "ready" },
    retrieval: { ready: true, mode: "hybrid", semanticReady: true, indexedSamples: 2, pendingSamples: 0, totalSamples: 2 } };
}
function json(response, value, code = 200) { response.writeHead(code, { "Content-Type": "application/json" }); response.end(JSON.stringify(value)); }
function serve(request, response) {
  const pathname = new URL(request.url, "http://127.0.0.1").pathname;
  if (pathname === "/api/plugins") return json(response, { plugins: [{ id: "kyf.self-analysis", enabled, contributes: {} }] });
  if (pathname === "/api/plugins/rpc") {
    let raw = ""; request.on("data", part => { raw += part; }); request.on("end", () => {
      const body = JSON.parse(raw); requests.push(body);
      if (!enabled || body.plugin !== "kyf.self-analysis" || body.account !== active || body.user) return json(response, { error: "scope-mismatch" }, 409);
      if (body.method === "self.status") return json(response, { result: status(body.account) });
      if (body.method === "self.analysisStatus") return json(response, { result: { state: "idle", steps: [] } });
      if (body.method === "self.analysisSettings") return json(response, { result: { outputs: ["library"], depth: "standard" } });
      if (body.method === "self.draftStatus") return json(response, { result: { state: "idle" } });
      if (body.method === "self.search") {
        const lexical = { id: "synthetic-lexical", name: "合成好友甲", input: "对方：周末来聚餐吗？", reply: "这周有点安排，这次先不了，下次再约。", matchKinds: ["lexical"] };
        const semantic = { id: "synthetic-semantic", name: "合成好友乙", input: "对方：休息日要不要出来走走？", reply: "这几天有点累，想在家歇歇，改天再一起去呀。", matchKinds: ["semantic"] };
        return json(response, { result: { sources: [semantic, lexical] } });
      }
      return json(response, { error: "unexpected-synthetic-method" }, 400);
    }); return;
  }
  if (pathname === "/") {
    response.writeHead(200, { "Content-Type": types[".html"] });
    response.end('<!doctype html><html><head><meta charset="utf-8"></head><body><p>合成主窗口</p><script src="/smoke-host.js"></script></body></html>'); return;
  }
  if (pathname === "/smoke-host.js") { response.writeHead(200, { "Content-Type": types[".js"] }); response.end(hostSource); return; }
  if (pathname === "/favicon.ico") { response.writeHead(204); response.end(); return; }
  let file;
  if (["/self-analysis.html", "/self-analysis-window.js", "/self-analysis-window.css", "/plugin-frame.js"].includes(pathname)) file = path.join(root, "chatui", pathname.slice(1));
  else if (pathname.startsWith("/plugin-sdk/web/")) file = path.join(root, pathname.slice(1));
  else if (pathname.startsWith("/plugin-pages/kyf.self-analysis/ui/")) file = path.join(root, "plugins/kyf.self-analysis/ui", pathname.slice("/plugin-pages/kyf.self-analysis/ui/".length));
  if (!file || !path.resolve(file).startsWith(root + path.sep)) { response.writeHead(404); response.end(); return; }
  try { response.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream" }); response.end(fs.readFileSync(file)); }
  catch { response.writeHead(404); response.end(); }
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, label) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) { try { if (await predicate()) return; } catch {} await delay(40); }
  throw new Error("Timed out: " + label);
}
async function evaluateFrame(frame, code) {
  let timer;
  try {
    return await Promise.race([frame.executeJavaScript(code, true), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("Synthetic frame evaluation was replaced")), 800);
    })]);
  } finally { clearTimeout(timer); }
}
const child = () => children.at(-1);
async function page(code) {
  const frame = child().webContents.mainFrame.frames.find(frame => frame.url.endsWith("/plugin-pages/kyf.self-analysis/ui/index.html"));
  if (!frame || !frame.url.endsWith("/plugin-pages/kyf.self-analysis/ui/index.html") ||
      !await evaluateFrame(frame, "document.readyState!=='loading' && !!document.getElementById('background')"))
    throw new Error("Synthetic plugin frame is loading");
  return evaluateFrame(frame, code);
}
const mainPage = code => main.webContents.executeJavaScript(code, true);
function check(label, value = true) { assert.ok(value, label); checks.push(label); }
async function capture(name) {
  await page("new Promise(resolve=>{requestAnimationFrame(()=>requestAnimationFrame(resolve));setTimeout(resolve,100)})");
  const options = { stayHidden: true, stayAwake: true };
  let image, painted = false;
  for (let attempt = 0; attempt < 4; attempt++) {
    await child().webContents.capturePage(undefined, options); await delay(120);
    image = await child().webContents.capturePage(undefined, options);
    const bitmap = image.toBitmap(), colors = new Set();
    for (let index = 0; index < bitmap.length; index += 12) colors.add(bitmap.readUInt32LE(index));
    if (colors.size > 60) { painted = true; break; }
  }
  assert.ok(painted, "hidden native window content must actually be painted: " + name);
  const file = path.join(output, name + ".png"); fs.writeFileSync(file, image.toPNG()); screenshots.push(file);
  assert.equal(child().isVisible(), false);
}
async function finish(error) {
  if (finished) return; finished = true; clearTimeout(timeout);
  fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ ok: !error, checks, requests, errors, external, screenshots,
    ...(error ? { error: error.stack || String(error) } : {}) }, null, 2));
  manager?.close(); main?.destroy(); server?.close();
  console.log(JSON.stringify({ ok: !error, checks: checks.length, output, ...(error ? { error: String(error) } : {}) }));
  app.exit(error ? 1 : 0);
}
app.whenReady().then(async () => {
  timeout = setTimeout(() => { void finish(new Error("synthetic smoke deadline")); }, 45000);
  server = http.createServer(serve); await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const clientUrl = `http://127.0.0.1:${server.address().port}/`;
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    const allowed = details.url.startsWith(clientUrl) || details.url === "about:blank";
    if (!allowed) external.push(details.url); callback({ cancel: !allowed });
  });
  main = new BrowserWindow({ show: false, webPreferences: { preload: path.join(root, "scripts/real-client-preload.cjs"),
    contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false } });
  main.show = () => {}; main.focus = () => {};
  await main.loadURL(clientUrl);
  function HiddenWindow(options) {
    const win = new BrowserWindow({ ...options, show: false, webPreferences: { ...options.webPreferences,
      paintWhenInitiallyHidden: true, backgroundThrottling: false } });
    win.show = () => {}; win.focus = () => {};
    win.webContents.on("console-message", details => { if (details.level === "error" && !details.message.includes("favicon"))
      errors.push({ message: details.message, lineNumber: details.lineNumber, sourceId: details.sourceId }); });
    children.push(win); return win;
  }
  manager = createSelfAnalysisWindow({ BrowserWindow: HiddenWindow, mainWindow: main, clientUrl, screen, ipcMain, clipboard,
    trustedMainFrame: event => event.sender === main.webContents && event.senderFrame === main.webContents.mainFrame && event.senderFrame.url === clientUrl });
  check("activated real main preload opens the dedicated native window", await mainPage("desktopHost.openSelfAnalysisWindow({account:smoke.account,theme:'light'})"));
  await until(() => page("document.getElementById('background').value==='合成账号 A 背景' && !document.getElementById('analysisRun').disabled"), "real iframe/profile/analysis readiness");
  check("real host/frame IPC loads the profile without locking analysis");
  check("native host uses fixed opaque sandbox", await child().webContents.executeJavaScript("document.querySelector('iframe').getAttribute('sandbox')==='allow-scripts'"));
  check("profile A reaches the real plugin", await page("document.getElementById('background').value==='合成账号 A 背景'"));
  await capture("light-profile-a");
  await page("document.getElementById('background').value='合成 A 保存后的资料';document.getElementById('background').dispatchEvent(new Event('input'));document.getElementById('saveProfile').click()");
  await until(() => mainPage("smoke.profiles[smoke.account].background==='合成 A 保存后的资料'"), "profile save through both preloads");
  check("profile save round trip uses the bound account");
  const id = child().id;
  check("reopening returns success", await mainPage("desktopHost.openSelfAnalysisWindow({account:smoke.account,theme:'light'})"));
  check("reopening reuses the same BrowserWindow", children.length === 1 && child().id === id);
  active = "synthetic-account-b";
  await mainPage("smoke.account='synthetic-account-b';desktopHost.syncSelfAnalysisWindow({context:{account:smoke.account,theme:'dark'}})");
  await until(() => page("document.getElementById('background').value==='合成账号 B 背景'"), "account B frame refresh");
  check("account switch removes A profile and query state", await page("!document.body.innerText.includes('合成 A 保存后的资料') && document.getElementById('query').value===''"));
  check("switch keeps the same native window", child().id === id); await capture("dark-profile-b");
  await until(() => page("!document.getElementById('query').disabled"), "account B status");
  await page("document.getElementById('tab-trial').click();document.getElementById('query').value='最近有点累，周末想在家休息，不想参加聚会';document.getElementById('query').dispatchEvent(new Event('input'));document.getElementById('search').click()");
  await until(() => page("document.querySelectorAll('#samples .sample').length===2"), "search RPC reaches the chat trial UI");
  check("native sandbox frame renders search results with synthetic sources");
  await capture("dark-search-b");
  await page("document.getElementById('modelSettings').click()");
  await until(() => mainPage("smoke.calls.some(c=>c.method==='settings.open')"), "settings host navigation");
  check("settings reach the bound main host");
  enabled = false;
  await mainPage("desktopHost.syncSelfAnalysisWindow({event:{type:'plugins-changed',enabled:false}})");
  await until(() => child().webContents.executeJavaScript("!document.querySelector('iframe') && document.body.innerText.includes('已停用')"), "disable removes frame");
  check("disable immediately removes the real plugin frame");
  enabled = true;
  await mainPage("desktopHost.syncSelfAnalysisWindow({event:{type:'plugins-changed',enabled:true}})");
  await until(() => page("document.getElementById('background').value==='合成账号 B 背景'"), "enable remounts plugin");
  await mainPage("desktopHost.syncSelfAnalysisWindow({event:{type:'account-cleared',account:smoke.account}})");
  await until(() => page("document.getElementById('background').value==='' && document.getElementById('background').disabled"), "account clear empties real UI");
  check("account clear removes old profile content");
  check("no model/build tasks or foreign contacts were requested", requests.every(value => ["self.status", "self.analysisStatus", "self.analysisSettings", "self.draftStatus", "self.search"].includes(value.method) && !value.user));
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  await finish();
}).catch(error => { void finish(error); });
