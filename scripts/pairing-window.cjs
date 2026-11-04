"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// MCP 客户端配对窗（G12）：由 Core 的 presenter 适配器 (bridge/core_mcp_pairing_window.py)
// 按需拉起，不要求知己主窗口打开。用户在这里选择要共享的账号与操作能力（默认全部
// 关闭），确认后由本进程把选择写到 stdout，只有 Core 能读到；窗口本身没有令牌，也
// 没有任何网络能力。
//
// stdout 行协议：
//   {"event":"shown"}
//   {"event":"decided","approved":bool,"accounts":[id...],"capabilities":{write,manage},
//    "reason":"accept"|"cancel"|"expired"|"closed"}
//   {"event":"error","message":"..."}          无法显示窗口（随后以 2 退出）

const { app, BrowserWindow, ipcMain, nativeTheme } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const PAIRING_ID = /^zpr_[0-9a-f]{32}$/;
const KINDS = new Set(["claude-code", "codex", "cursor", "deepseek-harness", "generic"]);
const CAPABILITIES = [
  ["write", "修改资料与发起分析"],
  ["manage", "全局管理（应用与模型设置）"],
];
const DECIDE_CHANNEL = "zhiji-pairing:decide";
const MAX_ACCOUNTS = 64;

function text(value, limit) {
  return typeof value === "string" && value.length > 0 && value.length <= limit ? value : null;
}

function validateRequest(value) {
  if (!value || typeof value !== "object") return null;
  const pairingId = text(value.pairingId, 64);
  if (!pairingId || !PAIRING_ID.test(pairingId)) return null;
  if (!KINDS.has(value.clientKind)) return null;
  const clientName = text(value.clientName, 64);
  const clientLabel = text(value.clientLabel, 64);
  if (!clientName || !clientLabel) return null;
  const replaces = value.replaces === null || value.replaces === undefined ? null : text(value.replaces, 64);
  if (value.replaces && !replaces) return null;
  if (!Array.isArray(value.accounts) || value.accounts.length > MAX_ACCOUNTS) return null;
  const accounts = [];
  for (const item of value.accounts) {
    const accountId = item && text(item.accountId, 128);
    const label = item && text(item.label, 200);
    if (!accountId || !label) return null;
    accounts.push({ accountId, label });
  }
  const expiresAtMs = value.expiresAtMs;
  if (typeof expiresAtMs !== "number" || !Number.isFinite(expiresAtMs) || expiresAtMs <= 0) return null;
  return { pairingId, clientKind: value.clientKind, clientName, clientLabel, replaces, accounts, expiresAtMs };
}

// 只接受请求里出现过的账号与三项已知能力；其他一律视为拒绝。
function validateDecision(request, value) {
  if (value === null) return { approved: false, reason: "expired" };
  if (!value || typeof value !== "object" || typeof value.approved !== "boolean") return null;
  if (!value.approved) return { approved: false, reason: "cancel" };
  const known = new Set(request.accounts.map(item => item.accountId));
  if (!Array.isArray(value.accounts) || value.accounts.some(id => !known.has(id)) ||
      new Set(value.accounts).size !== value.accounts.length) return null;
  const caps = value.capabilities;
  if (!caps || typeof caps !== "object") return null;
  const capabilities = {};
  for (const [key] of CAPABILITIES) {
    if (caps[key] !== undefined && typeof caps[key] !== "boolean") return null;
    capabilities[key] = caps[key] === true;
  }
  if (Object.keys(caps).some(key => !CAPABILITIES.some(([name]) => name === key))) return null;
  return { approved: true, accounts: value.accounts.slice(), capabilities, reason: "accept" };
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;" }[ch]));
}

function pageUrl(request) {
  const accountRows = request.accounts.map((item, index) =>
    `<label class="opt"><input type="checkbox" class="acc" data-index="${index}">` +
    `<span>${escapeHtml(item.label)}</span></label>`).join("");
  const capabilityRows = CAPABILITIES.map(([key, label]) =>
    `<label class="opt"><input type="checkbox" class="cap" data-key="${key}"><span>${escapeHtml(label)}</span></label>`).join("");
  const replaces = request.replaces ?
    `<p class="warn">将重新配对已有客户端「${escapeHtml(request.replaces)}」：旧凭据立即失效，原有授权按本次选择重设。</p>` : "";
  const html = `<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>连接 AI 客户端 · 知己微信助手</title>
<style>
:root { color-scheme: dark; --bg:#1b1b1b; --panel:#222222; --line:#2f2f30; --text:#dfdfdf;
  --muted:#8c8c8c; --accent:#07c160; --warn:#fa9d3b; }
@media (prefers-color-scheme: light) { :root { --bg:#f5f5f5; --panel:#ffffff; --line:#e7e7e7;
  --text:#191919; --muted:#7f7f7f; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,
  BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
main { padding:18px 20px 16px; }
h1 { margin:0 0 6px; font-size:16px; font-weight:500; }
.sub { margin:0 0 12px; color:var(--muted); font-size:12px; }
h2 { margin:12px 0 6px; font-size:13px; font-weight:500; color:var(--muted); }
.list { max-height:150px; overflow:auto; border:1px solid var(--line); border-radius:4px; padding:4px 10px;
  background:var(--panel); }
.opt { display:flex; gap:8px; align-items:center; padding:3px 0; cursor:pointer; }
.opt span { min-width:0; word-break:break-all; }
.warn { margin:0 0 8px; color:var(--warn); font-size:12px; }
.note { margin:10px 0 12px; color:var(--muted); font-size:12px; }
footer { display:flex; align-items:center; gap:10px; border-top:1px solid var(--line); padding-top:12px; }
.count { flex:1; min-width:0; color:var(--muted); font-size:12px; }
button { font:inherit; border-radius:4px; padding:5px 16px; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
.cancel { border:1px solid var(--line); background:var(--panel); color:var(--text); }
.ok { border:0; background:var(--accent); color:#fff; }
</style></head>
<body><main>
<h1>允许「${escapeHtml(request.clientName)}」连接知己？</h1>
<p class="sub">${escapeHtml(request.clientLabel)} 请求通过 MCP 读取你选择的账号资料。</p>
${replaces}
<h2>可读取的账号</h2>
<div class="list">${accountRows}</div>
<h2>额外操作能力（默认关闭）</h2>
<div class="list">${capabilityRows}</div>
<p class="note">读取到的资料会进入该客户端及其模型服务商。授权可随时在 设置 → 插件 → MCP 修改或撤销。</p>
<footer>
  <span class="count" id="count"></span>
  <button class="cancel" id="deny">拒绝</button>
  <button class="ok" id="allow" disabled>允许连接</button>
</footer>
</main>
<script>
(() => {
  const ids = ${JSON.stringify(request.accounts.map(item => item.accountId)).replace(/</g, "\\u003c")};
  const deadline = ${Number(request.expiresAtMs)};
  const allow = document.getElementById("allow"), deny = document.getElementById("deny");
  const count = document.getElementById("count");
  const accounts = Array.from(document.querySelectorAll("input.acc"));
  const caps = Array.from(document.querySelectorAll("input.cap"));
  let decided = false;
  const update = () => { allow.disabled = decided || !accounts.some(box => box.checked); };
  accounts.forEach(box => box.addEventListener("change", update));
  const send = value => { if (decided) return; decided = true; allow.disabled = true; deny.disabled = true;
    window.pairingHost.decide(value); };
  allow.addEventListener("click", () => {
    const capabilities = {};
    caps.forEach(box => { capabilities[box.dataset.key] = box.checked; });
    send({ approved: true, accounts: accounts.filter(box => box.checked).map(box => ids[Number(box.dataset.index)]),
           capabilities });
  });
  deny.addEventListener("click", () => send({ approved: false }));
  const tick = () => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) { count.textContent = "配对请求已过期"; send(null); return; }
    const total = Math.ceil(remaining / 1000);
    count.textContent = "将在 " + Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0") + " 后失效";
  };
  tick(); update();
  const timer = setInterval(() => { if (decided) { clearInterval(timer); return; } tick(); }, 500);
})();
</script></body></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

function createPairingWindow(options) {
  const { BrowserWindow: Window, ipcMain: ipc, root, request } = options;
  const page = pageUrl(request);
  const state = { decided: null, shown: false, window: null };
  let resolveDecided, rejectDecided, resolveShown, rejectShown;
  const whenDecided = new Promise((resolve, reject) => { resolveDecided = resolve; rejectDecided = reject; });
  const whenShown = new Promise((resolve, reject) => { resolveShown = resolve; rejectShown = reject; });

  function finish(outcome) {
    if (state.decided !== null) return;
    state.decided = outcome;
    resolveDecided(outcome);
    if (state.window && !state.window.isDestroyed()) state.window.close();
  }
  const onDecide = (event, value) => {
    if (!state.window || state.window.isDestroyed() || event.sender !== state.window.webContents) return;
    const outcome = validateDecision(request, value);
    if (outcome) finish(outcome);
  };
  ipc.on(DECIDE_CHANNEL, onDecide);
  let window;
  try {
    window = new Window({
      width: 460, height: 560, useContentSize: true,
      resizable: false, minimizable: false, maximizable: false, fullscreenable: false,
      autoHideMenuBar: true, show: false, title: "连接 AI 客户端 · 知己微信助手",
      backgroundColor: nativeTheme.shouldUseDarkColors ? "#1b1b1b" : "#f5f5f5",
      ...(root ? { icon: path.join(root, "chatui", "assets", "app-icon.ico") } : {}),
      webPreferences: {
        preload: path.join(__dirname, "pairing-window-preload.cjs"),
        partition: `zhiji-pairing-${request.pairingId}`,
        contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, devTools: false,
      },
    });
  } catch (error) {
    ipc.removeListener(DECIDE_CHANNEL, onDecide);
    rejectShown(error);
    rejectDecided(error);
    return { window: null, page, whenShown, whenDecided };
  }
  state.window = window;
  window.on("closed", () => {
    ipc.removeListener(DECIDE_CHANNEL, onDecide);
    finish({ approved: false, reason: "closed" });
    if (!state.shown) rejectShown(new Error("配对窗口在显示前被关闭"));
  });
  const contents = window.webContents;
  contents.on("render-process-gone", () => finish({ approved: false, reason: "closed" }));
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
    contents.on(name, event => event.preventDefault());
  contents.session.webRequest.onBeforeRequest((details, callback) => {
    try { callback({ cancel: new URL(details.url).protocol !== "data:" }); }
    catch (_) { callback({ cancel: true }); }
  });
  window.once("ready-to-show", () => {
    if (state.decided !== null || state.shown) return;
    try {
      window.show();
      window.focus();
      state.shown = true;
      resolveShown();
    } catch (error) {
      rejectShown(error);
    }
  });
  window.loadURL(page).catch(error => { if (!state.shown && state.decided === null) rejectShown(error); });
  return { window, page, whenShown, whenDecided };
}

module.exports = { createPairingWindow, validateRequest, validateDecision, pageUrl, DECIDE_CHANNEL };

const INVOKED_DIRECTLY = process.argv.length >= 2 &&
  path.resolve(process.argv[1]) === path.resolve(__filename);

if (INVOKED_DIRECTLY) {
  const emit = value => { try { process.stdout.write(JSON.stringify(value) + "\n"); } catch (_) { /* 退出中 */ } };
  let request = null;
  try {
    const raw = fs.readFileSync(0, "utf8");
    request = validateRequest(JSON.parse(raw.split("\n").find(line => line.trim())));
  } catch (error) {
    emit({ event: "error", message: String(error && error.message || error).slice(0, 200) });
    process.exit(2);
  }
  if (!request) {
    emit({ event: "error", message: "配对请求不正确" });
    process.exit(2);
  }
  app.setAppUserModelId("com.local.knowyourfriends.real-client.pairing");
  try {
    const userData = path.join(__dirname, "..", ".local", "pairing-window");
    fs.mkdirSync(userData, { recursive: true });
    app.setPath("userData", userData);
  } catch (_) { /* 默认 userData 也可用 */ }
  const root = path.resolve(__dirname, "..");
  app.whenReady().then(async () => {
    try {
      const created = createPairingWindow({ BrowserWindow, ipcMain, root, request });
      created.whenShown.then(() => emit({ event: "shown" }), () => {});
      const outcome = await created.whenDecided;
      emit({ event: "decided", approved: outcome.approved, accounts: outcome.accounts || [],
             capabilities: outcome.capabilities || {}, reason: outcome.reason });
    } catch (error) {
      emit({ event: "error", message: String(error && error.message || error).slice(0, 200) });
      app.exit(2);
      return;
    }
    setImmediate(() => app.exit(0));
  });
}
