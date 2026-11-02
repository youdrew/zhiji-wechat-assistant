"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Opt-in Electron smoke for the high-impact deletion confirm window: real window,
// preload, IPC and countdown on a synthetic settings channel (no MCP, no Core).
// Run with node_modules/electron/dist/electron.exe scripts/confirm-window-smoke.cjs
const { app, BrowserWindow, ipcMain } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { createConfirmWindow } = require("./confirm-window.cjs");

const root = path.resolve(__dirname, "..");
const reportPath = path.join(root, ".local", "tmp", "confirm-window-smoke.json");
function report(value) {
  // Electron 在 app.exit 时可能截断管道 stdout；结论同时落盘。
  try {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, JSON.stringify(value, null, 2));
  } catch (_) { /* 报告写不进就只剩 stdout */ }
  console.log(JSON.stringify(value));
}
app.commandLine.appendSwitch("disable-gpu");
try {
  fs.mkdirSync(path.join(root, ".local", "tmp"), { recursive: true });
  app.setPath("userData", fs.mkdtempSync(path.join(root, ".local", "tmp", "confirm-window-smoke-")));
} catch (_) { /* 默认 userData 也可用 */ }
const checks = [];
const record = name => { checks.push(name); console.log("ok " + name); };
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function payload(overrides = {}) {
  return { confirmId: "zcf_" + "a1b2c3d4e5f60718293a4b5c6d7e8f90".slice(0, 32),
           token: "b".repeat(64), action: "zhiji_delete_account", kindLabel: "删除整个账号",
           clientName: "合成客户端", accountLabel: "合成账号甲", impact: "将删除该账号的全部本机资料，不可恢复。",
           expiresAtMs: Date.now() + 600000, ...overrides };
}

// The smoke runs across several short-lived windows; never let their closing quit the app.
app.on("window-all-closed", () => {});

async function main() {
  // Synthetic settings channel: one POST per decision, checked against the bearer token.
  const seen = [];
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", chunk => { body += chunk; });
    request.on("end", () => {
      seen.push({ url: request.url, authorization: request.headers.authorization || "",
                  body: JSON.parse(body) });
      const granted = JSON.parse(body).granted;
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ confirmId: JSON.parse(body).confirmId,
        action: "zhiji_delete_account", kind: "delete_account",
        state: granted ? "confirmed" : "denied", accountId: "acc-a", expiresAtMs: Date.now() + 300000 }));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const coreUrl = `http://127.0.0.1:${server.address().port}/`;
  const base = { BrowserWindow, ipcMain, root };

  // 1. 确认执行：回传 granted=true 且命中设置通道。
  let created = createConfirmWindow({ ...base, request: { ...payload(), core: coreUrl, adminToken: "synthetic-admin-token" } });
  await created.whenShown;
  await created.window.webContents.executeJavaScript("window.confirmHost.decide(true)");
  let outcome = await created.whenDecided;
  assert.equal(outcome.granted, true);
  assert.equal(outcome.posted, true);
  assert.equal(outcome.reason, "accept");
  await delay(200);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "/plugin-api/v1/mcp/confirmations");
  assert.equal(seen[0].authorization, "Bearer synthetic-admin-token");
  assert.equal(seen[0].body.confirmId, payload().confirmId);
  assert.equal(seen[0].body.token, "b".repeat(64));
  assert.equal(seen[0].body.granted, true);
  record("confirm path posts the one-time decision through the settings channel");

  // 2. 取消：回传 granted=false。
  const denyPayload = payload({ confirmId: "zcf_" + "f".repeat(32) });
  created = createConfirmWindow({ ...base, request: { ...denyPayload, core: coreUrl, adminToken: "synthetic-admin-token" } });
  await created.whenShown;
  await created.window.webContents.executeJavaScript("window.confirmHost.decide(false)");
  outcome = await created.whenDecided;
  assert.equal(outcome.granted, false);
  assert.equal(outcome.posted, true);
  assert.equal(outcome.reason, "cancel");
  await delay(200);
  assert.equal(seen[1].body.granted, false);
  assert.equal(seen[1].body.confirmId, denyPayload.confirmId);
  record("cancel path posts granted=false");

  // 3. 已过期载荷：按钮禁用、无用户决定也不回传；倒计时归零在显示前就能关窗。
  created = createConfirmWindow({ ...base, request: { ...payload({ confirmId: "zcf_" + "e".repeat(32), expiresAtMs: Date.now() - 1000 }),
    core: coreUrl, adminToken: "synthetic-admin-token" } });
  created.whenShown.catch(() => {});  // 过期即关窗，可能早于 ready-to-show。
  outcome = await created.whenDecided;
  assert.equal(outcome.granted, false);
  assert.equal(outcome.posted, false);
  assert.equal(outcome.reason, "expired");
  await delay(200);
  assert.equal(seen.length, 2);
  assert.equal(created.window.isDestroyed(), true);
  record("expired payload denies without a settings-channel call");

  // 4. 未决定直接关窗：按拒绝收尾，不回传。
  created = createConfirmWindow({ ...base, request: { ...payload({ confirmId: "zcf_" + "d".repeat(32) }),
    core: coreUrl, adminToken: "synthetic-admin-token" } });
  await created.whenShown;
  created.window.close();
  outcome = await created.whenDecided;
  assert.equal(outcome.granted, false);
  assert.equal(outcome.posted, false);
  assert.equal(outcome.reason, "closed");
  await delay(200);
  assert.equal(seen.length, 2);
  record("closing the window denies without posting");

  // 5. 渲染进程拿不到令牌：页面上下文里没有 adminToken/token。
  const leaked = await (async () => {
    created = createConfirmWindow({ ...base, request: { ...payload({ confirmId: "zcf_" + "c".repeat(32) }),
      core: coreUrl, adminToken: "synthetic-admin-token" } });
    await created.whenShown;
    return created.window.webContents.executeJavaScript(
      "JSON.stringify({ token: window.confirmHost.token, admin: window.confirmHost.adminToken, keys: Object.keys(window.confirmHost) })");
  })();
  const renderer = JSON.parse(leaked);
  assert.deepEqual(renderer.keys.sort(), ["decide"]);
  assert.equal(renderer.token, undefined);
  assert.equal(renderer.admin, undefined);
  record("renderer sees only decide(), never any token");

  server.close();
  report({ ok: true, checks });
  app.exit(0);
}

app.whenReady().then(() => main().catch(error => {
  report({ ok: false, error: String(error && error.stack || error) });
  app.exit(1);
}));
