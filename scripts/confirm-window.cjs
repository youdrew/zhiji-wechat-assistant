"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 独立高影响删除确认窗（阶段 5 §7）：由 Core 的 presenter 适配器
// (bridge/core_mcp_confirm_window.py) 按需拉起，不要求知己主窗口打开。
// 展示客户端、操作、账号/数据范围与不可恢复影响，带倒计时；用户决定后由
// 本进程经 MCP 设置通道（POST mcp/confirmations）回传一次性令牌。令牌只存在
// 于主进程：渲染进程是一个静态 data: 页面，只发回 granted 与否。
//
// stdout 行协议（供 core_mcp_confirm_window 读取）：
//   {"event":"shown"}                                   窗口已显示
//   {"event":"decided","granted":bool,"posted":bool,     最终结果（reason:
//    "reason":"accept"|"cancel"|"expired"|"closed"}      accept/cancel 时已回传）
//   {"event":"error","message":"..."}                    无法显示窗口（随后以 2 退出）
//
// 作为工厂被冒烟测试复用（scripts/confirm-window-smoke.cjs）；作为入口被
// Core 直接以 electron confirm-window.cjs 拉起，请求经 stdin 一行 JSON 传入。

const { app, BrowserWindow, ipcMain, nativeTheme } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const CONFIRM_ID = /^zcf_[0-9a-f]{32}$/;
const CONFIRM_TOKEN = /^[0-9a-f]{64}$/;
const ACTIONS = new Set(["zhiji_delete_account", "zhiji_clear_local_data"]);
const POST_TIMEOUT_MS = 8000;
const DECIDE_CHANNEL = "zhiji-confirm:decide";

function loopbackCore(value) {
  return typeof value === "string" && /^http:\/\/127\.0\.0\.1:\d+\/?$/.test(value) ? value : null;
}

function text(value, limit) {
  return typeof value === "string" && value.length > 0 && value.length <= limit ? value : null;
}

// stdin 一行请求 = 确认载荷（core_mcp_confirm._present）+ core/adminToken 两个回传字段。
function validateRequest(value) {
  if (!value || typeof value !== "object") return null;
  const core = loopbackCore(value.core);
  const confirmId = text(value.confirmId, 64);
  const token = text(value.token, 64);
  if (!core || !confirmId || !CONFIRM_ID.test(confirmId) || !token || !CONFIRM_TOKEN.test(token)) return null;
  if (!ACTIONS.has(value.action)) return null;
  const kindLabel = text(value.kindLabel, 64);
  const clientName = text(value.clientName, 200);
  const accountLabel = text(value.accountLabel, 200);
  const impact = text(value.impact, 2000);
  const expiresAtMs = value.expiresAtMs;
  if (!kindLabel || !clientName || !accountLabel || !impact) return null;
  if (typeof expiresAtMs !== "number" || !Number.isFinite(expiresAtMs) || expiresAtMs <= 0) return null;
  const adminToken = typeof value.adminToken === "string" && value.adminToken.length <= 128 ?
    value.adminToken : "";
  return { core, adminToken, confirmId, token, action: value.action, kindLabel, clientName,
           accountLabel, impact, expiresAtMs };
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;" }[ch]));
}

// 极简、贴微信原生的静态页面；动态值全部转义后内联，页面自身没有任何网络能力。
function pageUrl(request) {
  const html = `<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>删除确认 · 知己微信助手</title>
<style>
:root { color-scheme: dark; --bg:#1b1b1b; --panel:#222222; --line:#2f2f30; --text:#dfdfdf;
  --muted:#8c8c8c; --danger:#fa5151; }
@media (prefers-color-scheme: light) { :root { --bg:#f5f5f5; --panel:#ffffff; --line:#e7e7e7;
  --text:#191919; --muted:#7f7f7f; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,
  BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
main { padding:18px 20px 16px; }
h1 { margin:0 0 14px; font-size:16px; font-weight:500; }
.rows { display:flex; flex-direction:column; gap:8px; margin-bottom:12px; }
.row { display:flex; gap:12px; }
.row .k { flex:none; width:56px; color:var(--muted); }
.row .v { flex:1; min-width:0; word-break:break-all; }
.kind { color:var(--danger); font-weight:600; }
.impact { margin:0 0 12px; border:1px solid var(--line); border-left:3px solid var(--danger);
  border-radius:4px; padding:8px 12px; color:var(--muted); font-size:12px; }
.note { margin:0 0 14px; color:var(--muted); font-size:12px; }
footer { display:flex; align-items:center; gap:10px; border-top:1px solid var(--line);
  padding-top:12px; }
.count { flex:1; min-width:0; color:var(--muted); font-size:12px; }
button { font:inherit; border-radius:4px; padding:5px 16px; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
.cancel { border:1px solid var(--line); background:var(--panel); color:var(--text); }
.cancel:hover:not(:disabled) { filter:brightness(1.1); }
.confirm { border:0; background:var(--danger); color:#fff; }
.confirm:hover:not(:disabled) { filter:brightness(1.05); }
</style></head>
<body><main>
<h1>高影响删除确认</h1>
<div class="rows">
  <div class="row"><span class="k">操作</span><span class="v kind">${escapeHtml(request.kindLabel)}</span></div>
  <div class="row"><span class="k">客户端</span><span class="v">${escapeHtml(request.clientName)}</span></div>
  <div class="row"><span class="k">范围</span><span class="v">${escapeHtml(request.accountLabel)}</span></div>
</div>
<p class="impact">${escapeHtml(request.impact)}</p>
<p class="note">本确认只对这次请求有效且只能使用一次；确认后请让该客户端在 5 分钟内执行。</p>
<footer>
  <span class="count" id="count"></span>
  <button class="cancel" id="cancel">取消</button>
  <button class="confirm" id="confirm">确认执行</button>
</footer>
</main>
<script>
(() => {
  const deadline = ${Number(request.expiresAtMs)};
  const confirmBtn = document.getElementById("confirm");
  const cancelBtn = document.getElementById("cancel");
  const count = document.getElementById("count");
  let decided = false;
  const send = value => { if (decided) return; decided = true; window.confirmHost.decide(value); };
  confirmBtn.addEventListener("click", () => send(true));
  cancelBtn.addEventListener("click", () => send(false));
  const tick = () => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      count.textContent = "确认已过期";
      confirmBtn.disabled = true; cancelBtn.disabled = true;
      send(null); return;
    }
    const total = Math.ceil(remaining / 1000);
    count.textContent = "本确认将在 " + Math.floor(total / 60) + ":" +
      String(total % 60).padStart(2, "0") + " 后失效";
  };
  tick();
  const timer = setInterval(() => { if (decided) { clearInterval(timer); return; } tick(); }, 500);
})();
</script></body></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

// 经 MCP 设置通道回传一次性决定；失败只意味着这条通道没走通，决定仍会经
// presenter 返回值生效（core_mcp_confirm 对迟到/重复决定幂等）。
async function postDecision(request, granted) {
  if (!request.adminToken) return false;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), POST_TIMEOUT_MS);
  try {
    const response = await fetch(new URL("plugin-api/v1/mcp/confirmations", request.core), {
      method: "POST", redirect: "error", signal: controller.signal,
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${request.adminToken}` },
      body: JSON.stringify({ confirmId: request.confirmId, token: request.token, granted }),
    });
    if (!response.ok) return false;
    const body = await response.json().catch(() => null);
    return !!body && body.confirmId === request.confirmId &&
      body.state === (granted ? "confirmed" : "denied");
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function createConfirmWindow(options) {
  const { BrowserWindow: Window, ipcMain, root, request } = options;
  const page = pageUrl(request);
  const state = { decided: null, shown: false, window: null };
  let resolveDecided, rejectDecided, resolveShown, rejectShown;
  const whenDecided = new Promise((resolve, reject) => { resolveDecided = resolve; rejectDecided = reject; });
  const whenShown = new Promise((resolve, reject) => { resolveShown = resolve; rejectShown = reject; });

  function finish(granted, posted, reason) {
    if (state.decided !== null) return state.decided;
    state.decided = { granted, posted, reason };
    resolveDecided(state.decided);
    if (state.window && !state.window.isDestroyed()) state.window.close();
    return state.decided;
  }
  async function decide(value) {
    if (value === null) { finish(false, false, "expired"); return; }
    const posted = await postDecision(request, value);
    finish(value, posted, value ? "accept" : "cancel");
  }
  const onDecide = (event, value) => {
    if (!state.window || state.window.isDestroyed() || event.sender !== state.window.webContents) return;
    if (value !== true && value !== false && value !== null) return;
    void decide(value);
  };
  ipcMain.on(DECIDE_CHANNEL, onDecide);

  let window;
  try {
    window = new Window({
      width: 460, height: 400, useContentSize: true,
      resizable: false, minimizable: false, maximizable: false, fullscreenable: false,
      autoHideMenuBar: true, show: false, title: "删除确认 · 知己微信助手",
      backgroundColor: nativeTheme.shouldUseDarkColors ? "#1b1b1b" : "#f5f5f5",
      ...(root ? { icon: path.join(root, "chatui", "assets", "app-icon.ico") } : {}),
      webPreferences: {
        preload: path.join(__dirname, "confirm-window-preload.cjs"),
        // 每个确认一个独立内存会话：页面发出的任何非 data: 请求都拦下，也不与
        // 其他窗口共享缓存或 Cookie。
        partition: `zhiji-confirm-${request.confirmId}`,
        contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true,
        devTools: false,
      },
    });
  } catch (error) {
    ipcMain.removeListener(DECIDE_CHANNEL, onDecide);
    rejectShown(error);
    rejectDecided(error);
    return { window: null, page, whenShown, whenDecided };
  }
  state.window = window;
  window.on("closed", () => {
    finish(false, false, "closed");
    if (!state.shown) rejectShown(new Error("确认窗口在显示前被关闭"));
  });
  const contents = window.webContents;
  contents.on("render-process-gone", () => { finish(false, false, "closed"); });
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  for (const name of ["will-navigate", "will-redirect", "will-frame-navigate", "will-attach-webview"])
    contents.on(name, event => event.preventDefault());
  // 静态页面：数据地址之外的任何请求（含 favicon）一律取消。
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
  window.loadURL(page).catch(error => {
    if (!state.shown && state.decided === null) rejectShown(error);
  });
  return { window, page, whenShown, whenDecided };
}

module.exports = { createConfirmWindow, validateRequest, DECIDE_CHANNEL };

// Electron 不把入口脚本设为 require.main（那是 electron 模块），按 argv 判断直启。
const INVOKED_DIRECTLY = process.argv.length >= 2 &&
  path.resolve(process.argv[1]) === path.resolve(__filename);

function readRequest() {
  const raw = fs.readFileSync(0, "utf8");
  const line = raw.split("\n").find(candidate => candidate.trim());
  return validateRequest(JSON.parse(line));
}

if (INVOKED_DIRECTLY) {
  const emit = value => { try { process.stdout.write(JSON.stringify(value) + "\n"); } catch (_) { /* 退出中 */ } };
  let request = null;
  try {
    request = readRequest();
  } catch (error) {
    emit({ event: "error", message: String(error && error.message || error).slice(0, 200) });
    process.exit(2);
  }
  if (!request) {
    emit({ event: "error", message: "确认请求不正确" });
    process.exit(2);
  }
  app.setAppUserModelId("com.local.knowyourfriends.real-client.confirm");
  try {
    const userData = path.join(__dirname, "..", ".local", "confirm-window");
    fs.mkdirSync(userData, { recursive: true });
    app.setPath("userData", userData);
  } catch (_) { /* 默认 userData 也可用 */ }
  const root = path.resolve(__dirname, "..");
  app.whenReady().then(async () => {
    try {
      const created = createConfirmWindow({ BrowserWindow, ipcMain, root, request });
      // 显示前就被关掉（例如载荷已过期）也算一次明确结论，不算显示失败。
      created.whenShown.then(() => { emit({ event: "shown" }); }, () => {});
      const outcome = await created.whenDecided;
      emit({ event: "decided", granted: outcome.granted, posted: outcome.posted, reason: outcome.reason });
    } catch (error) {
      // 无可交互桌面、窗口创建或显示失败：如实上报，由确认服务按不可用拒绝。
      emit({ event: "error", message: String(error && error.message || error).slice(0, 200) });
      app.exit(2);
      return;
    }
    setImmediate(() => app.exit(0));
  });
}
