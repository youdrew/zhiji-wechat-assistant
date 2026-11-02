"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Hidden Electron + real settings HTML/CSS. Every API response uses synthetic data in this file.
// Run: node_modules/electron/dist/electron.exe tests/plugin-configuration-smoke.cjs
const { app, BrowserWindow, nativeImage } = require("electron");
const assert = require("node:assert/strict");
const http = require("node:http");
const { mkdirSync, readFileSync, writeFileSync, writeSync } = require("node:fs");
const { createHash } = require("node:crypto");
const path = require("node:path");
const { captureHiddenRenderer } = require("./helpers/capture-hidden-renderer.cjs");
const root = path.resolve(__dirname, ".."), output = path.join(root, ".local/tmp/plugin-settings-smoke");
// Modified 2026-10-17 (Apache-2.0): a detached GUI test may outlive its terminal's output pipe.
// Keep logging best-effort; result.json and the exit code carry the test result.
function report(fd, text) {
  try { writeSync(fd, text); } catch { /* A closed output pipe must not prevent cleanup. */ }
}
const captureRun = Date.now().toString(36);
mkdirSync(output, { recursive: true });
app.setPath("userData", path.join(output, "electron-profile"));
app.disableHardwareAcceleration();
// Geometry and raster assertions share CSS-pixel coordinates on scaled displays.
if (!app.commandLine.hasSwitch("force-device-scale-factor")) app.commandLine.appendSwitch("force-device-scale-factor", "1");
const plugins = ["kyf.advisor", "kyf.friend-analysis", "kyf.message-insights"].map((id, index) => ({
  ...JSON.parse(readFileSync(path.join(root, "plugins", id, "plugin.json"), "utf8")),
  source: "bundled", enabled: true,
  process: { state: index === 0 ? "ready" : "idle", reason: null, log: [] },
}));
const snapshots = new Map(plugins.map(plugin => {
  const schema = plugin.contributes.settings, values = Object.fromEntries(schema.groups.flatMap(group => group.fields.map(field => [field.key, field.default])));
  return [plugin.id, { plugin, schema, values, revision: 0, definition: createHash("sha256").update(JSON.stringify(schema)).digest("hex"), enabled: true, effective: "next-task" }];
}));
const calls = [], errors = [], geometry = [], captures = [];
let nextSaveConflict = false;
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".png": "image/png" };
const listing = () => ({ coreApi: 1, plugins, problems: [] });
const appSource = readFileSync(path.join(root, "chatui/app.js"), "utf8");
function appSection(first, last) {
  const start = appSource.indexOf(first), end = appSource.indexOf(last, start);
  assert.ok(start >= 0 && end > start, "Missing production settings handler: " + first);
  return appSource.slice(start, end);
}
const initSource = `
const byId = id => document.getElementById(id);
const text = (id, value) => { byId(id).textContent = value; };
const settingsState = { dataRootRequest: 0, modelSourceReadRequest: 0, modelListRequest: 0, modelTestRequest: 0 };
let accountManagementOpen = false, pendingDeleteAccountId = null;
function closeConversationManager() {} function renderManagedAccounts() {} function loadAboutVersion() {} function hideApiKey() {}
${appSection("function closeSettingsModal()", "// Forget this window's per-account conveniences")}
${appSection('byId("btnCloseSettings").addEventListener', 'byId("btnManageAccounts").addEventListener')}
${appSection('document.querySelectorAll(".settings-tab-btn").forEach', 'byId("btnEmoji").addEventListener')}
document.getElementById('settingsModal').classList.add('show');
document.querySelectorAll('.settings-panel').forEach(value => value.classList.toggle('active', value.id === 'panelPlugins'));
document.querySelectorAll('.settings-tab-btn').forEach(value => value.classList.toggle('active', value.dataset.tab === 'plugins'));
window.smokeNav = [];
PluginSettings.init({ ${appSection('  onModelSettings() {', '  onOpenNav(')} onOpenNav: (id, nav) => window.smokeNav.push(id + ':' + nav) });
`;
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  const json = (value, status = 200) => { response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" }); response.end(JSON.stringify(value)); };
  if (url.pathname === "/api/plugins" && request.method === "GET") { calls.push({ path: url.pathname, method: "GET" }); json(listing()); return; }
  if (url.pathname === "/api/plugins/settings" && request.method === "GET") {
    calls.push({ path: url.pathname, method: "GET", id: url.searchParams.get("id") });
    json(snapshots.get(url.searchParams.get("id"))); return;
  }
  if (url.pathname === "/api/plugins/settings" && request.method === "POST") {
    let raw = ""; request.on("data", chunk => { raw += chunk; }); request.on("end", () => {
      const body = JSON.parse(raw); calls.push({ path: url.pathname, method: "POST", body });
      const current = snapshots.get(body.id); assert.equal(body.definition, current.definition);
      if (nextSaveConflict) { nextSaveConflict = false; json({ error: "plugin-settings-conflict", message: "设置已在其他窗口更新，请重新读取" }, 409); return; }
      assert.equal(body.revision, current.revision); current.revision++; current.values = body.values; json(current);
    }); return;
  }
  if (url.pathname.startsWith("/api/")) { errors.push("Unexpected API " + url.pathname); json({ message: "合成验收禁止此调用" }, 500); return; }
  if (url.pathname === "/smoke-init.js") { response.writeHead(200, { "Content-Type": types[".js"] }); response.end(initSource); return; }
  try {
    let data, extension;
    if (url.pathname === "/" || url.pathname === "/index.html") {
      const html = readFileSync(path.join(root, "chatui/index.html"), "utf8");
      const head = html.slice(0, html.indexOf("<body>"));
      // Keep the real settings markup; the unrelated startup overlay needs the production app controller.
      const settings = html.slice(html.indexOf('<div class="settings-modal-overlay"'), html.indexOf('<div class="session-context-menu"'));
      data = head + '<body>' + settings + '<script src="plugin-configuration.js"></script><script src="plugin-settings.js"></script><script src="smoke-init.js"></script></body></html>'; extension = ".html";
    } else {
      const file = path.resolve(root, "chatui", url.pathname.slice(1));
      assert.ok(file.startsWith(path.join(root, "chatui") + path.sep));
      data = readFileSync(file); extension = path.extname(file);
    }
    response.writeHead(200, { "Content-Type": types[extension] || "application/octet-stream" }); response.end(data);
  } catch { response.writeHead(404); response.end(); }
});
let win;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const page = expression => win.webContents.executeJavaScript(expression);
async function until(expression) {
  for (let attempt = 0; attempt < 50; attempt++) { if (await page(expression)) return; await wait(60); }
  throw new Error("Timed out: " + expression + "\n" + await page("document.getElementById('panelPlugins').innerText"));
}
async function screenshot(name) {
  assert.equal(win.isVisible(), false, "The synthetic renderer must never show a window");
  await page("Promise.all(document.getAnimations().filter(value => value.effect.getComputedTiming().iterations !== Infinity).map(value => value.finished.catch(() => {})))");
  await page("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const rect = await page(`(() => { const r = document.querySelector('.settings-modal-card').getBoundingClientRect(); return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }; })()`);
  const viewport = await captureHiddenRenderer(win);
  // Persist the complete viewport, including the margins around the 650px settings card.
  const viewportPng = Buffer.from(viewport.toPNG()), image = nativeImage.createFromBuffer(viewportPng);
  const size = viewport.getSize(), viewportSize = await page("({width:innerWidth,height:innerHeight})");
  assert.deepEqual(size, viewportSize, "Full renderer capture must use viewport coordinates");
  assert.deepEqual(image.getSize(), viewportSize);
  const pixels = image.toBitmap(), colors = new Set();
  for (let offset = 0; offset < pixels.length; offset += 4) colors.add(pixels.readUInt32LE(offset));
  assert.ok(colors.size > 100, `${name}: blank or unpainted capture (${colors.size} colors)`);
  const sidebar = await page("(() => { const r = document.querySelector('.settings-sidebar').getBoundingClientRect(); return {x:Math.ceil(r.left),y:Math.ceil(r.top),width:Math.floor(r.width),height:Math.min(180,Math.floor(r.height))}; })()");
  const sidebarColors = new Set();
  for (let y = sidebar.y; y < sidebar.y + sidebar.height; y++) for (let x = sidebar.x; x < sidebar.x + sidebar.width; x++)
    sidebarColors.add(pixels.readUInt32LE((y * size.width + x) * 4));
  assert.ok(sidebarColors.size > 30, `${name}: sidebar was not painted (${sidebarColors.size} colors)`);
  const landmarks = await page(`(() => {
    const selectors = ['.settings-sidebar', '#pluginConfigBack', '#pluginConfigTitle', '#pluginLeaveConfirm'];
    return selectors.flatMap(selector => { const element = document.querySelector(selector), r = element?.getBoundingClientRect();
      return r?.width > 0 && r?.height > 0 ? [{ selector, top:r.top, bottom:r.bottom, left:r.left, right:r.right }] : []; });
  })()`);
  for (const landmark of landmarks) assert.ok(landmark.top >= rect.y && landmark.bottom <= rect.y + rect.height,
    `${name}: visible landmark was clipped ${JSON.stringify(landmark)}`);
  assert.equal(win.isVisible(), false, "Capturing must keep the window hidden");
  const filename = `${captureRun}-${name}.png`;
  writeFileSync(path.join(output, name + "-viewport.png"), viewportPng);
  writeFileSync(path.join(output, name + ".png"), viewportPng);
  writeFileSync(path.join(output, filename), viewportPng);
  captures.push({ name, image: filename, card: rect, viewport: size, landmarks, colors: colors.size, sidebarColors: sidebarColors.size, hidden: true });
}
async function measure(name) {
  const result = await page(`(() => {
    const box = element => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
    const content = document.querySelector('.settings-content'), card = document.querySelector('.settings-modal-card');
    return { viewport: innerWidth, documentWidth: document.documentElement.scrollWidth, contentWidth: content.clientWidth,
      contentScrollWidth: content.scrollWidth, card: box(card), controls: [...document.querySelectorAll('#panelPlugins input, #panelPlugins select, #panelPlugins button, #panelPlugins summary')]
        .filter(value => value.getBoundingClientRect().width > 0).map(value => ({ label: value.getAttribute('aria-label') || value.innerText || value.name, ...box(value) })) };
  })()`);
  assert.ok(result.documentWidth <= result.viewport, name + ": document overflow " + JSON.stringify(result));
  assert.ok(result.contentScrollWidth <= result.contentWidth + 1, name + ": content overflow " + JSON.stringify(result));
  for (const control of result.controls) assert.ok(control.left >= result.card.left - 1 && control.right <= result.card.right + 1,
    name + ": control escapes card " + JSON.stringify(control));
  geometry.push({ name, ...result });
}
app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  win = new BrowserWindow({ width: 682, height: 740, show: false, useContentSize: true,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, offscreen: true, backgroundThrottling: false } });
  win.webContents.on("console-message", details => { if (details.level === "error" && !details.message.includes("favicon")) errors.push(details.message); });
  win.webContents.on("render-process-gone", (_event, details) => { errors.push("Renderer exited: " + details.reason); });
  await win.loadURL(`http://127.0.0.1:${server.address().port}/`);
  await until(`document.querySelectorAll('.plugin-row').length === ${plugins.length}`);
  await wait(250); // Let the production settings overlay's entrance animation complete before capturing pixels.
  assert.equal(await page("document.querySelectorAll('.plugin-settings-entry').length"), plugins.length);
  // Shared model settings are a sibling panel, with drafts retained across navigation.
  await page("PluginSettings.openSettings('kyf.advisor')"); await until("!!document.querySelector('.plugin-config-link')");
  await page("[...document.querySelectorAll('.plugin-config-link')].find(node => node.textContent.includes('模型设置')).click()");
  assert.equal(await page("document.querySelector('.settings-panel.active').id"), "panelModels");
  assert.equal(await page("document.querySelectorAll('.settings-tab-btn').length"), 4);
  for (const id of ["apiModelSettings", "inputApiTimeoutMinutes", "modelTraceRetention", "btnDeveloperMode"]) {
    assert.equal(await page(`document.getElementById('${id}').closest('.settings-panel').id`), "panelModels");
  }
  for (const theme of ["dark", "light"]) {
    await page(`document.body.classList.toggle('theme-light', ${theme === "light"})`);
    await screenshot(theme + "-models-650");
  }
  await page("document.getElementById('inputApiTimeoutMinutes').value = '90'; document.querySelector('.settings-tab-btn[data-tab=general]').click()");
  assert.equal(await page("document.querySelector('.settings-panel.active').id"), "panelGeneral");
  await page("document.querySelector('.settings-tab-btn[data-tab=models]').click()");
  assert.equal(await page("document.getElementById('inputApiTimeoutMinutes').value"), "90");
  await page("document.querySelector('.settings-tab-btn[data-tab=plugins]').click()");
  for (const theme of ["dark", "light"]) {
    await page(`document.body.classList.toggle('theme-light', ${theme === "light"})`); await measure(theme + "-list-650"); await screenshot(theme + "-list-650");
    await page("PluginSettings.openSettings('kyf.advisor')"); await until("!!document.querySelector('[name=includeMemories]')");
    await measure(theme + "-detail-650"); await screenshot(theme + "-detail-650");
    await page("PluginConfiguration.back()");
  }
  // Execute the production app.js close/sidebar/Escape handlers above, with only unrelated account cleanup stubbed.
  await page("PluginSettings.openSettings('kyf.message-insights')"); await until("!!document.querySelector('[name=replyStyle]')");
  await page("document.querySelector('[name=replyStyle]').value = 'brief'; document.querySelector('[name=replyStyle]').dispatchEvent(new Event('change')); document.getElementById('btnCloseSettings').click()");
  await until("!document.getElementById('pluginLeaveConfirm').hidden");
  assert.equal(await page("document.getElementById('settingsModal').classList.contains('show')"), true);
  await page("document.getElementById('pluginLeaveStay').click(); document.querySelector('.settings-tab-btn[data-tab=models]').click()");
  await until("!document.getElementById('pluginLeaveConfirm').hidden");
  assert.equal(await page("document.getElementById('panelPlugins').classList.contains('active')"), true);
  await page("document.getElementById('pluginLeaveDiscard').click()");
  assert.equal(await page("document.getElementById('panelModels').classList.contains('active')"), true);
  assert.equal(await page("PluginConfiguration.isEditing()"), false);
  await page("document.querySelector('.settings-tab-btn[data-tab=plugins]').click(); PluginSettings.openSettings('kyf.message-insights')");
  await until("!!document.querySelector('[name=replyStyle]') && !document.getElementById('pluginDetail').hidden");
  await page("document.querySelector('[name=replyStyle]').value = 'brief'; document.querySelector('[name=replyStyle]').dispatchEvent(new Event('change')); const handled = new KeyboardEvent('keydown', {key:'Escape', bubbles:true, cancelable:true}); handled.preventDefault(); document.dispatchEvent(handled)");
  assert.equal(await page("document.getElementById('pluginLeaveConfirm').hidden"), true);
  await page("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true, cancelable:true}))");
  await until("!document.getElementById('pluginLeaveConfirm').hidden");
  await page("document.getElementById('pluginLeaveSave').click()");
  await until("!document.getElementById('settingsModal').classList.contains('show')");
  assert.equal(snapshots.get("kyf.message-insights").values.replyStyle, "brief");
  await page("document.getElementById('settingsModal').classList.add('show'); PluginSettings.openSettings('kyf.message-insights')");
  await until("!!document.querySelector('[name=replyStyle]') && !document.getElementById('pluginDetail').hidden");
  await page("document.querySelector('[name=replyStyle]').value = 'polite'; document.querySelector('[name=replyStyle]').dispatchEvent(new Event('change')); document.getElementById('settingsModal').dispatchEvent(new MouseEvent('click', {bubbles:true}))");
  await until("!document.getElementById('pluginLeaveConfirm').hidden");
  await page("document.getElementById('pluginLeaveDiscard').click()");
  assert.equal(await page("document.getElementById('settingsModal').classList.contains('show')"), false);
  await page("document.getElementById('settingsModal').classList.add('show')");
  await page("PluginSettings.openSettings('kyf.message-insights')"); await until("!!document.querySelector('[name=replyStyle]')");
  await page("document.querySelector('[name=replyStyle]').value = 'natural'; document.querySelector('[name=replyStyle]').dispatchEvent(new Event('change', {bubbles:true}))");
  await page("PluginSettings.refresh()");
  assert.equal(await page("document.querySelector('[name=replyStyle]').value"), "natural");
  assert.equal(await page("PluginConfiguration.isDirty()"), true);
  nextSaveConflict = true; await page("document.getElementById('pluginConfigSave').click()");
  await until("!document.getElementById('pluginConfigReload').hidden");
  assert.equal(await page("document.querySelector('[name=replyStyle]').value"), "natural");
  await page("document.getElementById('pluginConfigSave').click()"); await until("!PluginConfiguration.isDirty()");
  assert.equal(snapshots.get("kyf.message-insights").values.replyStyle, "natural");
  assert.equal(await page("document.getElementById('pluginConfigReload').hidden"), true);
  await page("document.querySelector('[name=replyStyle]').value = 'polite'; document.querySelector('[name=replyStyle]').dispatchEvent(new Event('change', {bubbles:true})); PluginConfiguration.back()");
  await until("!document.getElementById('pluginLeaveConfirm').hidden");
  await screenshot("light-confirm-650");
  await page("document.getElementById('pluginLeaveStay').click()");
  assert.equal(await page("document.querySelector('[name=replyStyle]').value"), "polite");
  win.setContentSize(360, 520); await wait(100); await measure("light-detail-360"); await screenshot("light-detail-360");
  await page("PluginConfiguration.back()"); await measure("light-confirm-360"); await screenshot("light-confirm-360");
  await page("document.getElementById('pluginLeaveDiscard').click()");
  assert.equal(await page("document.getElementById('pluginOverview').hidden"), false);
  await measure("light-list-360"); await screenshot("light-list-360");
  assert.ok(calls.every(call => ["/api/plugins", "/api/plugins/settings"].includes(call.path)));
  assert.deepEqual(errors, []);
  writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: true, geometry, captures, calls }, null, 2));
  report(1, "Synthetic plugin settings smoke passed (650px card light/dark, 360px, revision conflict, draft protection).\n");
  win.destroy(); server.close(); app.quit();
}).catch(error => {
  try {
    writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: false, error: String(error.stack || error) }, null, 2));
    report(2, String(error.stack || error) + "\n");
  } finally {
    if (win && !win.isDestroyed()) win.destroy(); server.close(); app.exit(1);
  }
});
