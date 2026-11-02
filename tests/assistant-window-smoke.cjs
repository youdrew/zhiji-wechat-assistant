"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Real Electron host and sandboxed assistant, using only synthetic local fixtures.
// Modified 2026-10-22: one window per friend (scripts/plugin-windows.cjs); two friends' assistants open together.
const { app, BrowserWindow, ipcMain, screen, clipboard } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), http = require("node:http");
const { createPluginWindows } = require("../scripts/plugin-windows.cjs");
const root = path.resolve(__dirname, ".."), output = path.join(root, ".local/tmp/assistant-window-smoke");
fs.mkdirSync(output, { recursive: true });
app.setPath("userData", fs.mkdtempSync(path.join(output, "profile-")));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("enable-features", "IsolateSandboxedIframes:grouping/per-document");
const A = { account: "synthetic-account", user: "friend-a", name: "合成好友甲", theme: "dark" };
const B = { ...A, user: "friend-b", name: "合成好友乙" };
const catalog = { agents: [{ id: "helper", name: "沟通助手", description: "", prompt: "合成", enabled: true, skillIds: [] }],
  skills: [], engine: { state: "available" } };
const threads = new Map(), calls = [], checks = [];
let main, manager, server, timer, finished = false, holdEnqueue = false, releaseEnqueue = null;
// Keep synthetic windows hidden while exercising their real WebContents and preloads.
class HiddenAssistant {
  constructor(options) {
    const own = new BrowserWindow({ ...options, webPreferences: { ...options.webPreferences, backgroundThrottling: false } });
    own.show = () => {}; own.focus = () => {}; own.isVisible = () => !own.isDestroyed() && !own.closedByUser;
    own.hide = () => { own.closedByUser = true; own.emit("hide"); };
    own.on("show", () => { own.closedByUser = false; });
    return own;
  }
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(read, label) {
  for (let i = 0; i < 100; i++) {
    let ready = false;
    try { ready = await Promise.race([read(), wait(500).then(() => false)]); } catch { /* frame can be replaced during a reload */ }
    if (ready) { checks.push(label); return; } await wait(40);
  }
  throw new Error("Timed out: " + label);
}
const frame = win => win.webContents.mainFrame.frames[0];
const page = (win, code) => frame(win).executeJavaScript(code, true);
async function screenshot(win, name, theme) {
  for (let i = 0; i < 6; i++) {
    win.webContents.invalidate();
    const shot = await win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
    const bytes = shot.toBitmap(), width = shot.getSize().width;
    const background = bytes[(100 * width + 2) * 4];
    if (theme !== "light" || background > 230) {
      // A second capture after the compositor wakes includes the current iframe paint.
      await wait(100); win.webContents.invalidate();
      fs.writeFileSync(path.join(output, name), (await win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG());
      return;
    }
    await wait(100);
  }
  throw new Error("Assistant screenshot did not paint the requested theme");
}
function rpc(body) {
  calls.push(body); assert.equal(body.account, A.account);
  let thread = threads.get(body.user);
  if (!thread) {
    thread = { id: "thread-" + body.user, account: body.account, user: body.user, agentId: "helper", messages: [],
      draft: { text: "", version: 0, scrollTop: 0, mentions: [] }, participants: [],
      queue: { version: 0, paused: true, reason: "manual", items: [] } };
    threads.set(body.user, thread);
  }
  const p = body.params || {};
  if (body.method === "catalog.get") return catalog;
  if (body.method === "thread.get") return { thread, run: null };
  if (body.method === "queue.get") return { queue: thread.queue };
  if (body.method === "friends.list") return { friends: [{ user: "friend-extra", name: "合成好友丙" }] };
  if (body.method === "draft.save") {
    assert.equal(p.version, thread.draft.version);
    thread.draft = { text: p.text, version: p.version + 1, scrollTop: p.scrollTop || 0, mentions: p.mentions || [] };
    return { draft: thread.draft };
  }
  if (body.method === "queue.enqueue") {
    const item = { id: "pending-a", requestId: p.requestId, message: p.message, mentions: p.mentions || [],
      thinking: p.thinking, version: 1, state: "pending" };
    thread.queue.items.push(item); thread.queue.version++;
    const accepted = { queue: thread.queue, item, threadId: thread.id };
    if (holdEnqueue) {
      holdEnqueue = false;
      return new Promise(resolve => { releaseEnqueue = () => resolve(accepted); });
    }
    return accepted;
  }
  return {};
}
async function serve(request, response) {
  const url = new URL(request.url, "http://127.0.0.1");
  if (url.pathname === "/api/plugins/rpc") {
    let raw = ""; for await (const chunk of request) raw += chunk;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ result: await rpc(JSON.parse(raw)) })); return;
  }
  let file, policy;
  if (url.pathname.startsWith("/plugin-pages/kyf.advisor/")) {
    file = path.join(root, "plugins/kyf.advisor", url.pathname.slice(26));
    policy = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; frame-ancestors 'self'";
  } else if (url.pathname.startsWith("/plugin-sdk/")) file = path.join(root, url.pathname.slice(1));
  else file = path.join(root, "chatui", url.pathname === "/main.html" ? "index.html" : url.pathname.slice(1));
  try {
    let payload = fs.readFileSync(file);
    // The actual main-chat markup/styles are enough to verify the relocated button geometry.
    if (url.pathname === "/main.html") payload = payload.toString().replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");
    response.writeHead(200, { "Content-Type": { ".html": "text/html", ".css": "text/css", ".js": "text/javascript" }[path.extname(file)] || "application/octet-stream",
      ...(policy ? { "Content-Security-Policy": policy } : {}) }); response.end(payload);
  } catch { response.writeHead(404); response.end(); }
}
async function finish(error) {
  if (finished) return; finished = true;
  clearTimeout(timer);
  fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({ passed: !error, checks,
    calls: calls.map(({ method, user, params }) => ({ method, user, ...(method === "queue.enqueue" ? { mentions: params.mentions } : {}) })),
    error: error ? String(error.stack || error) : null }, null, 2));
  process.stdout.write(JSON.stringify({ passed: !error, checks: checks.length, output }) + "\n");
  if (error) process.stderr.write(String(error.stack || error) + "\n");
  manager?.close();
  if (main && !main.isDestroyed()) main.destroy();
  server?.closeAllConnections(); server?.close();
  app.exit(error ? 1 : 0);
}
app.whenReady().then(async () => {
  timer = setTimeout(() => { void finish(new Error("Assistant smoke timed out")); }, 45000);
  server = http.createServer((req, res) => { void serve(req, res).catch(error => { res.destroy(); void finish(error); }); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}/`;
  main = new BrowserWindow({ width: 1240, height: 850, show: false,
    webPreferences: { preload: path.join(root, "scripts/real-client-preload.cjs"), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  manager = createPluginWindows({ BrowserWindow: HiddenAssistant, mainWindow: main, clientUrl: base,
    screen, clipboard, ipcMain, trustedMainFrame: e => e.sender === main.webContents && e.senderFrame === main.webContents.mainFrame });
  await main.loadURL(base + "main.html");
  const js = code => main.webContents.executeJavaScript(code, true);
  await js(`document.getElementById('databaseLoading').hidden = true;
    document.getElementById('chatTitle').textContent = '合成好友甲';
    document.getElementById('sessionList').textContent = '合成好友甲';
    document.getElementById('btnReturnLatest').hidden = false;`);
  const geometry = await js(`(() => { const b = document.getElementById('btnReturnLatest').getBoundingClientRect(),
    m = document.getElementById('chatMessages').getBoundingClientRect(), i = document.getElementById('chatInputPane').getBoundingClientRect();
    return { offset: Math.abs((b.left+b.width/2)-(m.left+m.width/2)), bottom: b.bottom, inputTop: i.top,
      width: b.width, iconOnly: !document.getElementById('btnReturnLatest').textContent.trim() }; })()`);
  assert.ok(geometry.offset <= 1 && geometry.bottom < geometry.inputTop && geometry.iconOnly && geometry.width === 40, JSON.stringify(geometry));
  checks.push("latest icon is centered above the composer");
  fs.writeFileSync(path.join(output, "latest-dark.png"), (await main.webContents.capturePage()).toPNG());
  const open = scope => js(`window.desktopHost.openPluginWindow(${JSON.stringify({ plugin: "kyf.advisor", page: "ui/panel.html",
    label: "聊天助手", ...scope })})`);
  const owned = () => BrowserWindow.getAllWindows().filter(w => w !== main);
  const before = main.getBounds(); assert.equal((await open(A)).opened, true);
  await until(() => owned().length === 1, "assistant uses a separate native window");
  const [assistant] = owned();
  await until(() => frame(assistant) && page(assistant, `document.getElementById('advisorInput').value === '@合成好友甲 '`), "default current friend mention");
  assert.deepEqual(main.getBounds(), before); checks.push("main window keeps its original bounds");
  assert.equal((await open(B)).opened, true);
  await until(() => owned().length === 2, "another friend gets an own window");
  const assistantB = owned().find(w => w !== assistant);
  await until(() => frame(assistantB) && page(assistantB, `window.AdvisorController.currentUser === 'friend-b' && document.getElementById('advisorInput').value === '@合成好友乙 '`),
    "the second window is pinned to its own friend");
  assert.equal(await page(assistant, "window.AdvisorController.currentUser"), A.user); checks.push("the first window keeps its friend");
  await page(assistant, `const input=document.getElementById('advisorInput'); input.value += '@丙';
    input.setSelectionRange(input.value.length,input.value.length); input.dispatchEvent(new Event('input',{bubbles:true}));`);
  await until(() => page(assistant, `!document.getElementById('mentionMenu').hidden && document.getElementById('mentionMenu').querySelector('button')`), "extra friend picker");
  await page(assistant, `document.getElementById('mentionMenu').querySelector('button').click();`);
  await until(() => threads.get(A.user)?.draft.mentions?.[0]?.user === "friend-extra", "exact extra friend identity is saved with draft");
  await wait(100);
  await screenshot(assistant, "assistant-dark.png", "dark");
  await new Promise(resolve => {
    assistant.webContents.once("did-finish-load", resolve); assistant.webContents.reload();
  });
  await until(() => frame(assistant) && page(assistant, `document.getElementById('advisorInput').value.includes('@合成好友丙')`), "draft mentions survive window reload");
  holdEnqueue = true;
  await page(assistant, `document.getElementById('btnAdvisorSend').click()`);
  await until(() => calls.some(c => c.method === "queue.enqueue" && c.user === A.user && c.params.mentions?.includes("friend-extra")), "submitted question includes the extra friend's identity");
  await page(assistant, `document.getElementById('advisorTimeline').dispatchEvent(new Event('scroll'));`);
  const saves = calls.filter(c => c.method === "draft.save" && c.user === A.user).length;
  await until(() => calls.filter(c => c.method === "draft.save" && c.user === A.user).length > saves,
    "scroll position saves while the question awaits acknowledgement");
  releaseEnqueue();
  await until(() => page(assistant, `document.getElementById('advisorInput').value === ''`),
    "accepted question clears after a scroll without deleting its queued item");
  assert.equal(threads.get(A.user).queue.items.length, 1);
  await until(() => threads.get(A.user).draft.text === "", "cleared composer draft is saved");
  await open({ ...A, theme: "light" });
  await until(() => page(assistant, "document.body.classList.contains('theme-light')"), "independent window theme updates");
  await wait(100);
  await screenshot(assistant, "assistant-light.png", "light");
  assert.equal(owned().length, 2); checks.push("opening a friend again reuses that friend's window");
  assistant.close(); await wait(100);
  assert.equal(assistant.isDestroyed(), false); checks.push("closing keeps the friend's page hidden");
  await open(A);
  await until(() => page(assistant, `window.AdvisorController.currentUser === 'friend-a'`), "a closed window comes back with its friend");
  assert.equal(owned().length, 2);
  const recordedContext = { version: 1, state: "recorded", capturedAtMs: Date.now(), contacts: [
    { user: B.user, name: B.name, primary: true, access: "required", status: "ready", sources: ["chat", "profile"] },
    { user: "friend-extra", name: "同名好友", primary: false, access: "available", status: "ready", sources: ["chat"] },
    { user: "friend-other", name: "同名好友", primary: false, access: "available", status: "ready", sources: ["profile"] },
    { user: "friend-long", name: "一位备注较长的合成好友".repeat(6), primary: false, access: "available", status: "unavailable", sources: [] },
  ] };
  const history = threads.get(B.user);
  history.queue = { version: 1, paused: false, reason: "", items: [] };
  history.messages = [
    { id: "legacy-question", seq: 1, role: "user", text: "这是没有记录上下文范围的旧提问。", contextSnapshot: null },
    { id: "legacy-answer", seq: 2, role: "assistant", text: "这是合成历史回答。" },
    { id: "snapshot-question", seq: 3, role: "user", runId: "snapshot-run", text: "请结合这几位朋友的交流背景，帮我整理下次可以聊的话题。", contextSnapshot: recordedContext },
    { id: "snapshot-answer", seq: 4, role: "assistant", text: "可以从彼此最近提过的共同兴趣开始，再询问近况。" },
  ];
  history.participants = [{ user: "joined-later", name: "后来加入的好友" }];
  await page(assistantB, "window.AdvisorController.loadThread()");
  await until(() => page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"]')?.textContent.includes('同名好友')`), "each question displays its recorded context");
  assert.equal(await page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"]').textContent.includes('后来加入的好友')`), false);
  assert.ok(await page(assistantB, `document.querySelector('[data-msg-id="legacy-question"]').textContent.includes('上下文未记录')`));
  checks.push("historical context never derives from current discussion participants");
  await open({ ...B, theme: "dark" });
  await until(() => page(assistantB, "document.body.classList.contains('theme-dark')"), "context chips render in dark theme");
  await screenshot(assistantB, "context-collapsed-dark.png", "dark");
  assert.equal(await page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"] .advisor-context-count').textContent`), "+2");
  assert.equal(await page(assistantB, `document.querySelectorAll('[data-msg-id="snapshot-question"] .advisor-context-name').length`), 2);
  await page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"] .advisor-context-toggle').click()`);
  await until(() => page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"] .advisor-context-toggle').getAttribute('aria-expanded') === 'true'`), "context disclosure expands all friends and source kinds");
  assert.equal(await page(assistantB, `document.querySelectorAll('[data-msg-id="snapshot-question"] .advisor-context-contact').length`), 4);
  assert.ok(await page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"] .advisor-context-details').textContent.includes('好友资料')`));
  await page(assistantB, "window.AdvisorController.loadThread()");
  assert.equal(await page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"] .advisor-context-toggle').getAttribute('aria-expanded')`), "true");
  checks.push("context disclosure survives repeated thread refreshes");
  await screenshot(assistantB, "context-expanded-dark.png", "dark");
  await open({ ...B, theme: "light" });
  await until(() => page(assistantB, "document.body.classList.contains('theme-light')"), "context details render in light theme");
  assistantB.setContentSize(420, 650); await wait(100);
  const contextGeometry = await page(assistantB, `(() => {
    const row=document.querySelector('[data-msg-id="snapshot-question"]'), box=row.getBoundingClientRect();
    const toggle=row.querySelector('.advisor-context-toggle').getBoundingClientRect();
    return { width:innerWidth, scrollWidth:document.documentElement.scrollWidth, left:box.left, right:box.right, toggleRight:toggle.right };
  })()`);
  assert.ok(contextGeometry.scrollWidth <= contextGeometry.width && contextGeometry.left >= 0 &&
    contextGeometry.right <= contextGeometry.width && contextGeometry.toggleRight <= contextGeometry.width, JSON.stringify(contextGeometry));
  checks.push("long and duplicate friend names fit a narrow native window");
  await screenshot(assistantB, "context-expanded-light-420.png", "light");
  await new Promise(resolve => {
    assistantB.webContents.once("did-finish-load", resolve); assistantB.webContents.reload();
  });
  await until(() => frame(assistantB) && page(assistantB, `document.querySelector('[data-msg-id="snapshot-question"]')?.textContent.includes('同名好友')`), "recorded message contexts survive complete window reload");
  await finish();
}).catch(finish);
