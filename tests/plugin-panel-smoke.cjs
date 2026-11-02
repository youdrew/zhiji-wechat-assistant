"use strict";
// Know Your Friends, 2026-10-10. Apache-2.0. Synthetic, offline companion rendering.
// Moved from tests/advisor-session-frame-smoke.cjs to tests/plugin-panel-smoke.cjs on 2026-10-13: the real panel host
// (chatui/plugin-panel.html + its preload) loads the kyf.advisor panel page in a sandboxed frame from a synthetic
// local server that answers /api/plugins/rpc. Run with the project Electron executable:
//   node_modules/electron/dist/electron.exe tests/plugin-panel-smoke.cjs
// It never attaches to the user's running application.
const { app, BrowserWindow } = require("electron");
const assert = require("node:assert/strict");
const http = require("node:http");
const { mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const output = path.resolve(root, ".local/tmp/plugin-panel-smoke");
mkdirSync(output, { recursive: true });
app.setPath("userData", path.join(output, "electron-profile"));
app.disableHardwareAcceleration();
// Match the production shell: opaque sandbox origins must use a separate renderer.
app.commandLine.appendSwitch("enable-features", "IsolateSandboxedIframes:grouping/per-document");
const PAGE_POLICY = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
  "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'";
const scope = { account: "synthetic-account", user: "synthetic-contact" };
const thread = {
  id: "discussion-a", account: scope.account, user: scope.user, agentId: "helper",
  title: "周末活动：先问时间，再确认地点", contextTokens: 128000,
  draft: { text: "能否帮我整理下一步？", version: 2, scrollTop: 0 },
  usage: { inputTokens: 24100, contextTokens: 128000, estimated: true, recordedAtMs: 4000 },
  compactions: [{ id: "compact-a", state: "completed", kind: "manual", completedAtMs: 3000,
    summary: "用户目标：协调一次周末活动。\n已确认：先询问时间，之后再商量地点。\n待办：确认大家是否有空。" }],
  messages: [{ id: "u1", seq: 1, role: "user", text: "我想和朋友组织周末活动，可以怎么开口？", createdAtMs: 1000 },
    { id: "a1", seq: 2, role: "assistant", text: "可以先问对方周末是否有空，再讨论具体活动。\n\n先保留选择空间。", createdAtMs: 2000 }],
};
const catalog = { agents: [{ id: "helper", name: "沟通助手", description: "", prompt: "合成", enabled: true, skillIds: [] }],
  skills: [], engine: { state: "available" } };
const history = [
  { id: "discussion-a", agentId: "helper", title: thread.title, preview: "等待确认时间", updatedAtMs: 4000, state: "running", readonly: false },
  { id: "discussion-b", agentId: "helper", title: "最近的共同经历", preview: "合成历史", updatedAtMs: 2000, state: "done", readonly: false },
  { id: "retired", agentId: "retired-helper", agentName: "已停用助手", title: "旧讨论", preview: "只读历史", updatedAtMs: 1000, readonly: true },
];
const calls = [];
// Added by Know Your Friends, 2026-10-16 (Apache-2.0): durable queue UI exercised
// through the same isolated plugin frame, backed only by these synthetic RPCs.
let queueRun = null, queueSerial = 0;
thread.queue = { paused: false, reason: "", version: 0, items: [] };
function rpc(body) {
  calls.push(body);
  const params = body.params || {};
  if (body.method === "catalog.get") return catalog;
  if (body.method === "thread.get") return { thread, run: queueRun };
  if (body.method === "queue.get") return { queue: thread.queue };
  if (body.method === "queue.enqueue") {
    let item = thread.queue.items.find(row => row.requestId === params.requestId);
    if (!item) {
      item = { id: `queue-${++queueSerial}`, requestId: params.requestId, message: params.message,
        thinking: params.thinking, mentions: params.mentions || [], state: "pending", version: 1, createdAtMs: Date.now() };
      thread.queue.items.push(item); thread.queue.version++;
    }
    return { queue: thread.queue, item, threadId: thread.id };
  }
  if (body.method === "queue.edit" || body.method === "queue.remove") {
    const item = thread.queue.items.find(row => row.id === params.itemId);
    assert.ok(item); assert.equal(params.version, item.version);
    if (body.method === "queue.edit") { item.message = params.message; item.version++; }
    else thread.queue.items = thread.queue.items.filter(row => row !== item);
    thread.queue.version++;
    return { queue: thread.queue };
  }
  if (body.method === "queue.pause" || body.method === "queue.resume") {
    thread.queue.paused = body.method === "queue.pause"; thread.queue.reason = thread.queue.paused ? "manual" : "";
    thread.queue.version++;
    return { queue: thread.queue };
  }
  if (body.method === "run.events") return { run: queueRun, thread, events: [], cursor: 0, terminal: false };
  if (body.method === "run.stop") {
    queueRun = { ...queueRun, state: "stopped" }; thread.queue.paused = true; thread.queue.reason = "stopped";
    thread.queue.version++;
    return { run: queueRun, queue: thread.queue };
  }
  if (body.method === "threads.list") return { threads: history };
  if (body.method === "thread.rename") return { thread: { ...thread, id: params.threadId, title: params.title }, run: null };
  if (body.method === "thread.delete") return { deleted: true, state: "completed", threadId: params.threadId };
  if (body.method === "draft.save") return { draft: { text: params.text, version: params.version + 1 } };
  return {};
}
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  if (process.env.SMOKE_DEBUG) process.stderr.write(`${request.method} ${url.pathname}
`);
  if (url.pathname === "/api/plugins/rpc" && request.method === "POST") {
    let data = "";
    request.on("data", chunk => { data += chunk; });
    request.on("end", () => {
      const body = JSON.parse(data);
      if (process.env.SMOKE_DEBUG) process.stderr.write(`rpc ${data}
`);
      assert.equal(body.plugin, "kyf.advisor"); assert.equal(body.account, scope.account); assert.equal(body.user, scope.user);
      response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify({ result: rpc(body) }));
    });
    return;
  }
  let file, policy = null;
  if (url.pathname.startsWith("/plugin-pages/kyf.advisor/")) { file = path.join(root, "plugins/kyf.advisor", url.pathname.slice(26)); policy = PAGE_POLICY; }
  else if (url.pathname.startsWith("/plugin-sdk/web/")) { file = path.join(root, "plugin-sdk/web", url.pathname.slice(16)); policy = PAGE_POLICY; }
  else file = path.join(root, "chatui", url.pathname.slice(1));
  try {
    const payload = readFileSync(file);
    response.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream", ...(policy ? { "Content-Security-Policy": policy } : {}) });
    response.end(payload);
  } catch { response.writeHead(404); response.end(); }
});
let win;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function page(expression) {
  for (let attempt = 0; attempt < 50; attempt++) {
    const frame = win.webContents.mainFrame.frames[0];
    if (frame) try { return await frame.executeJavaScript(expression); } catch { /* still loading */ }
    await wait(100);
  }
  throw new Error("panel page did not load");
}
async function until(expression) {
  for (let attempt = 0; attempt < 50; attempt++) { if (await page(expression)) return; await wait(100); }
  const state = await page(`JSON.stringify((({ catalogReady, activeAgentId, currentAccount, catalogError, lastProblem }) => ({ catalogReady, activeAgentId, currentAccount, catalogError, lastProblem }))(window.AdvisorController))`).catch(String);
  throw new Error("timed out: " + expression + " " + state);
}
app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}/`;
  win = new BrowserWindow({ width: 440, height: 760, show: false, useContentSize: true,
    webPreferences: { preload: path.join(root, "scripts/plugin-panel-preload.cjs"), contextIsolation: true, sandbox: true,
      nodeIntegration: false, backgroundThrottling: false } });
  const rendererErrors = [];
  win.webContents.on("console-message", details => {
    const level = details.level, message = details.message;
    if (process.env.SMOKE_DEBUG) process.stderr.write(`console ${level}: ${message}
`);
    if (level === "error") rendererErrors.push(message);
  });
  await win.loadURL(base + "plugin-panel.html?plugin=kyf.advisor&page=ui%2Fpanel.html");
  win.webContents.send("plugin-panel:to-panel", { context: { ...scope, name: "合成好友", theme: "light" } });
  await until(`document.getElementById('sessionTitle').textContent === ${JSON.stringify(thread.title)}`);
  const hostProcess = win.webContents.mainFrame.osProcessId;
  const pageProcess = win.webContents.mainFrame.frames[0].osProcessId;
  assert.ok(hostProcess > 0 && pageProcess > 0 && hostProcess !== pageProcess,
    `plugin page must have its own renderer (${hostProcess}, ${pageProcess})`);
  assert.equal(await page(`document.getElementById('usageButton').textContent`), "约 24k / 128k");
  assert.equal(await page(`document.querySelectorAll('.advisor-compaction').length`), 1);
  assert.equal(await page(`document.getElementById('advisorInput').value`), thread.draft.text);
  assert.equal(await page(`document.body.classList.contains('theme-light')`), true);
  writeFileSync(path.join(output, "light-440.png"), (await win.webContents.capturePage()).toPNG());
  win.webContents.send("plugin-panel:to-panel", { context: { ...scope, name: "合成好友", theme: "dark" } });
  await until(`document.body.classList.contains('theme-dark')`);
  writeFileSync(path.join(output, "dark-440.png"), (await win.webContents.capturePage()).toPNG());
  await page(`document.getElementById('btnHistory').click()`);
  await until(`document.querySelectorAll('.advisor-history-row').length === 3`);
  assert.equal(await page(`document.getElementById('otherHistory').hidden`), false);
  writeFileSync(path.join(output, "history-440.png"), (await win.webContents.capturePage()).toPNG());
  await page(`document.querySelector('.advisor-history-row').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 150, clientY: 150 }))`);
  assert.equal(await page(`document.getElementById('btnDeleteThread').disabled`), true);
  await page(`document.getElementById('btnRenameThread').click(); document.getElementById('sessionTitleInput').value = '合成改名'; document.getElementById('btnConfirmDialog').click();`);
  await wait(300);
  assert.equal(calls.filter(call => call.method === "thread.rename").at(-1)?.params.title, "合成改名");
  await page(`document.getElementById('btnHistory').click()`);
  await until(`document.querySelectorAll('.advisor-history-row').length === 3`);
  await page(`document.querySelectorAll('.advisor-history-row')[1].dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 150, clientY: 200 })); document.getElementById('btnDeleteThread').click(); document.getElementById('btnConfirmDialog').click();`);
  await wait(300);
  assert.equal(calls.filter(call => call.method === "thread.delete").at(-1)?.params.threadId, "discussion-b");
  // Return to the conversation before measuring the composer: hidden nodes have a zero rectangle.
  await page(`document.getElementById('btnCloseHistory').click()`);
  await until(`document.querySelector('.advisor-input-area').getBoundingClientRect().height > 0`);
  win.setContentSize(340, 400);
  await wait(200);
  const geometry = await page(`({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
    inputBottom: document.querySelector('.advisor-input-area').getBoundingClientRect().bottom,
    height: innerHeight, moreRight: document.getElementById('btnMore').getBoundingClientRect().right })`);
  assert.ok(geometry.scrollWidth <= geometry.width, JSON.stringify(geometry));
  assert.ok(geometry.inputBottom > 0 && geometry.inputBottom <= geometry.height && geometry.moreRight <= geometry.width, JSON.stringify(geometry));
  writeFileSync(path.join(output, "small-340.png"), (await win.webContents.capturePage()).toPNG());
  // A waiting list has its own editor; sending while a turn is running must
  // leave Stop available and preserve the separate composer draft during edits.
  thread.queue = { paused: true, reason: "manual", version: 1, items: [
    { id: "pending-a", requestId: "synthetic-pending-a", message: "分析完成后，请列出三个下一步。",
      thinking: false, mentions: [], state: "pending", version: 1, createdAtMs: 5000 },
    { id: "pending-b", requestId: "synthetic-pending-b", message: "再给我一条轻松的开场白。",
      thinking: true, mentions: [], state: "pending", version: 1, createdAtMs: 6000 },
  ] };
  await page(`window.AdvisorController.loadThread()`);
  await until(`!document.getElementById('queuePanel').hidden`);
  await page(`document.getElementById('btnQueueToggle').click()`);
  await until(`!document.getElementById('queueList').hidden`);
  await page(`document.querySelector('.queue-edit').click()`);
  await until(`!document.getElementById('queueEditor').hidden`);
  const composerBeforeEdit = await page(`document.getElementById('advisorInput').value`);
  await page(`document.getElementById('queueEditInput').value = '编辑后的后续问题';
    document.getElementById('queueEditInput').dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btnQueueSave').click()`);
  await until(`document.getElementById('queueEditor').hidden`);
  assert.equal(thread.queue.items[0].message, "编辑后的后续问题");
  assert.equal(await page(`document.getElementById('advisorInput').value`), composerBeforeEdit);
  await page(`document.querySelectorAll('.queue-remove')[1].click()`);
  await until(`document.querySelectorAll('.advisor-queue-item').length === 1`);
  await page(`document.getElementById('btnQueuePause').click()`);
  await until(`document.getElementById('btnQueuePause').textContent.includes('暂停')`);
  assert.equal(thread.queue.paused, false);
  await page(`document.getElementById('btnQueuePause').click()`);
  await until(`document.getElementById('btnQueuePause').textContent.includes('继续')`);
  queueRun = { id: "synthetic-running-queue", threadId: thread.id, state: "running", message: "正在处理的合成问题" };
  await page(`window.AdvisorController.loadThread()`);
  await until(`!document.getElementById('btnStopRun').hidden && !document.getElementById('btnAdvisorSend').hidden`);
  await page(`document.getElementById('advisorInput').value = '生成期间追加的合成问题';
    document.getElementById('advisorInput').dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btnAdvisorSend').click();`);
  await until(`document.getElementById('advisorInput').value === ''`);
  assert.equal(thread.queue.items.at(-1).message, "生成期间追加的合成问题");
  assert.ok(calls.some(call => call.method === "queue.enqueue"));
  for (const theme of ["dark", "light"]) {
    win.webContents.send("plugin-panel:to-panel", { context: { ...scope, name: "合成好友", theme } });
    await until(`document.body.classList.contains('theme-${theme}')`);
    for (const width of [340, 440]) {
      win.setContentSize(width, 660); await wait(150);
      const boxes = await page(`({ width: innerWidth, scroll: document.documentElement.scrollWidth,
        bottom: document.querySelector('.advisor-input-area').getBoundingClientRect().bottom, height: innerHeight,
        send: document.getElementById('btnAdvisorSend').getBoundingClientRect().toJSON(),
        stop: document.getElementById('btnStopRun').getBoundingClientRect().toJSON() })`);
      assert.ok(boxes.scroll <= boxes.width && boxes.bottom <= boxes.height, JSON.stringify(boxes));
      assert.ok(boxes.send.width > 0 && boxes.stop.width > 0 &&
        (boxes.send.right <= boxes.stop.left || boxes.stop.right <= boxes.send.left), JSON.stringify(boxes));
      writeFileSync(path.join(output, `queue-${theme}-${width}.png`), (await win.webContents.capturePage()).toPNG());
    }
  }
  win.setContentSize(340, 400);
  await wait(150);
  const editorOpened = await page(`(() => { document.querySelector('.queue-edit').click();
    const q = document.getElementById('queuePanel'); return { top: q.scrollTop, height: q.scrollHeight, client: q.clientHeight,
      overflow: getComputedStyle(q).overflowY }; })()`);
  await wait(150);
  const compactQueue = await page(`({ width: innerWidth, scroll: document.documentElement.scrollWidth,
    queueScroll: document.getElementById('queuePanel').scrollTop,
    queueScrollHeight: document.getElementById('queuePanel').scrollHeight,
    input: document.querySelector('.advisor-input-area').getBoundingClientRect().toJSON(),
    queue: document.getElementById('queuePanel').getBoundingClientRect().toJSON(),
    save: document.getElementById('btnQueueSave').getBoundingClientRect().toJSON(),
    send: document.getElementById('btnAdvisorSend').getBoundingClientRect().toJSON(), height: innerHeight })`);
  assert.ok(compactQueue.scroll <= compactQueue.width && compactQueue.input.top >= 0 &&
    compactQueue.save.top >= compactQueue.queue.top && compactQueue.save.bottom <= compactQueue.queue.bottom &&
    compactQueue.send.bottom <= compactQueue.height, JSON.stringify({ editorOpened, compactQueue }));
  writeFileSync(path.join(output, "queue-editor-small-340.png"), (await win.webContents.capturePage()).toPNG());
  await page(`document.getElementById('btnQueueCancel').click()`);
  await page(`document.getElementById('btnStopRun').click()`);
  await until(`document.getElementById('btnStopRun').hidden`);
  assert.equal(thread.queue.paused, true);
  queueRun = null;
  assert.deepEqual(rendererErrors, []);
  // Both pages come from the same Core origin, so default per-site grouping would join them.
  await win.webContents.executeJavaScript(`(() => {
    const container = document.createElement('div'); document.body.appendChild(container);
    window.isolationPeer = KyfFrames.create({ container, plugin: 'kyf.advisor', page: 'ui/repository.html',
      context: ${JSON.stringify({ ...scope, surface: "nav", theme: "dark" })} });
  })()`);
  let peer;
  for (let attempt = 0; attempt < 50; attempt++) {
    peer = win.webContents.mainFrame.frames.find(frame => frame.url.includes('/repository.html'));
    if (peer && await peer.executeJavaScript("typeof window.kyfHost === 'object'").catch(() => false)) break;
    await wait(100);
  }
  assert.ok(peer);
  const peerProcess = peer.osProcessId;
  assert.notEqual(peerProcess, pageProcess, "sibling plugin documents must not share a renderer");
  assert.notEqual(peerProcess, hostProcess);
  // A stuck plugin script must not block its host's controls. Removing its frame also reclaims that process.
  const stuckFrame = win.webContents.mainFrame.frames[0];
  void stuckFrame.executeJavaScript("while (true) {}").catch(() => {});
  await wait(150);
  const responsive = await Promise.race([win.webContents.executeJavaScript("document.querySelector('iframe').remove(); true"),
    wait(3000).then(() => { throw new Error("host froze with its plugin page"); })]);
  assert.equal(responsive, true);
  assert.equal(win.webContents.mainFrame.osProcessId, hostProcess);
  assert.equal(await Promise.race([peer.executeJavaScript("typeof window.kyfHost === 'object'"),
    wait(3000).then(() => { throw new Error("a sibling plugin froze with the stuck page"); })]), true);
  await win.loadURL(base + "plugin-panel.html?plugin=kyf.advisor&page=ui%2Fpanel.html");
  win.webContents.send("plugin-panel:to-panel", { context: { ...scope, name: "合成好友", theme: "dark" } });
  // Added 2026-10-14 (review): 继续聊这句 opening the panel replays its quote with the first context, while the
  // page still loads its later scripts; the quote must reach the draft.
  win.webContents.send("plugin-panel:to-panel", { event: { type: "quote", ...scope, messageId: "m-quote", text: "周末一起吃饭吗" } });
  await until(`document.getElementById('sessionTitle').textContent === ${JSON.stringify(thread.title)}`);
  await until(`document.getElementById('advisorInput').value.includes('周末一起吃饭吗')`);
  writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: true, geometry,
    isolation: { hostProcess, pageProcess, peerProcess, hostResponsiveDuringStall: responsive,
      siblingResponsiveDuringStall: true, reloaded: true }, calls: calls.map(call => call.method) }, null, 2));
  process.stdout.write("Synthetic plugin panel smoke passed (440px light/dark/history, 340px).\n");
  win.destroy(); server.close(); app.quit();
}).catch(error => {
  process.stderr.write(`${error.stack}\ncalls: ${JSON.stringify(calls.map(call => call.method))}\n`); if (win && !win.isDestroyed()) win.destroy(); server.close(); app.exit(1);
});
