"use strict";
// Synthetic full renderer → production preload/IPC → HTTP → service/store benchmark.
// Run with node_modules/electron/dist/electron.exe (no real client is opened).
const { app, BrowserWindow, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { attachModelTraces } = require("./model-traces-host.cjs");

const root = path.resolve(__dirname, "..");
const temporary = path.join(root, ".local", "tmp");
fs.mkdirSync(temporary, { recursive: true });
const profile = fs.mkdtempSync(path.join(temporary, "trace-electron-benchmark-"));
app.setPath("userData", profile);
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-background-timer-throttling");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
const control = randomBytes(32).toString("hex");
const monitorIndex = process.argv.indexOf("--monitor");
const monitorUrl = monitorIndex >= 0 ? process.argv[monitorIndex + 1] : null;
const large = process.argv.includes("--large");
const fixtureToken = process.env.KYF_TRACE_FIXTURE_TOKEN;
delete process.env.KYF_TRACE_FIXTURE_TOKEN;
const python = process.env.KYF_PYTHON || path.join(root, ".venv", "Scripts", "python.exe");
let worker, window, host, timer, fixtureAddress;
let done = false;
const reportName = large ? "model-trace-electron-large-viewer-benchmark" : "model-trace-electron-viewer-benchmark";
const output = path.join(temporary, reportName + ".json");
const progressFile = path.join(temporary, large ? "model-trace-electron-large-viewer-progress.json" : "model-trace-electron-viewer-progress.json");
async function finish(error) {
  if (done) return;
  done = true; clearTimeout(timer);
  host?.close();
  if (window && !window.isDestroyed()) window.destroy();
  if (error) process.stderr.write(String(error.message || error) + "\n");
  if (error) fs.writeFileSync(progressFile, JSON.stringify({ error: String(error.message || error) }));
  if (worker && worker.exitCode === null) {
    try { await fetch(new URL("fixture-shutdown", fixtureAddress), { method: "POST", headers: { "X-Fixture-Control": control }, body: "{}", signal: AbortSignal.timeout(2000) }); } catch {}
    await Promise.race([new Promise(resolve => worker.once("exit", resolve)), new Promise(resolve => setTimeout(resolve, 3000))]);
  }
  app.exit(error ? 1 : 0);
}

function createWindow(address, token) {
  window = new BrowserWindow({ width: 1440, height: 900, show: false,
    webPreferences: { preload: path.join(__dirname, "real-client-preload.cjs"),
      contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false, offscreen: true } });
  window.webContents.setFrameRate(60);
  const trustedMainFrame = event => !window.isDestroyed() && event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame && event.senderFrame.url === address;
  host = attachModelTraces({ ipcMain, mainWindow: window, clientUrl: new URL("/", address).href, trustedMainFrame, getControlToken: () => token });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, target) => { if (target !== address) event.preventDefault(); });
}

async function monitor() {
  const base = new URL(monitorUrl);
  if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || base.username || base.password) throw Error("Monitor fixture must use IPv4 loopback");
  const input = fixtureToken ? { token: fixtureToken } : await new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", value => { input += value; if (input.length > 4096) reject(Error("Invalid fixture input")); else if (input.includes("\n")) resolve(JSON.parse(input.split("\n")[0])); });
    process.stdin.on("end", () => { if (!input.includes("\n")) reject(Error("Missing fixture token")); });
  });
  if (!/^[a-f0-9]{64}$/.test(input.token || "")) throw Error("Invalid synthetic fixture token");
  const address = new URL("fixture-viewer", base).href;
  createWindow(address, input.token);
  await window.loadURL(address);
  fs.writeFileSync(progressFile, JSON.stringify({ stage: "monitoring", panel: null }));
  let previous = null, reads = 0, peakHeap = 0;
  const started = Date.now();
  timer = setTimeout(() => { void finish(Error("Monitor fixture timed out")); }, 900000);
  const poll = async () => {
    if (done) return;
    const state = await fetch(new URL("fixture-viewer-state", base), { signal: AbortSignal.timeout(5000) }).then(response => response.json());
    if (state.finished) {
      const report = { mode: "actualElectronPanel", electron: process.versions.electron, elapsedMs: Date.now()-started, polls: reads,
        rendererPeakHeapBytes: peakHeap, viewport: { width: 1440, height: 900, offscreen: true, gpu: "disabled", frameRate: 60 } };
      fs.writeFileSync(path.join(temporary, "model-trace-electron-monitor.json"), JSON.stringify(report, null, 2));
      fs.writeFileSync(progressFile, JSON.stringify({ stage: "complete", mode: report.mode }));
      process.stdout.write(JSON.stringify(report) + "\n"); return finish();
    }
    if (state.panel !== previous) {
      await window.webContents.executeJavaScript(state.panel ? "ModelTraces.open()" : "ModelTraces.close({restore:false,focus:false})");
      previous = state.panel;
      fs.writeFileSync(progressFile, JSON.stringify({ stage: "monitoring", panel: previous, polls: reads }));
      await fetch(new URL("fixture-viewer-ready", base), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ panel: previous }), signal: AbortSignal.timeout(5000) });
    }
    if (state.panel) {
      const heap = await window.webContents.executeJavaScript("(()=>{const first=document.querySelector('#modelTraceList button');if(first&&first.getAttribute('aria-selected')!=='true'){first.click();document.querySelector('#traceTab_response')?.click();}return performance.memory?.usedJSHeapSize||0;})()");
      peakHeap = Math.max(peakHeap, heap);
    }
    reads++;
    setTimeout(() => { void poll().catch(finish); }, 100);
  };
  await poll();
}

app.whenReady().then(async () => {
  if (monitorIndex >= 0) return monitor();
  timer = setTimeout(() => { void finish(Error("Synthetic fixture startup timed out")); }, 60000);
  const address = fixtureAddress = await new Promise((resolve, reject) => {
    worker = spawn(python, [path.join(__dirname, "benchmark-model-trace-viewer.py"), "--electron", ...(large ? ["--large"] : [])], {
      cwd: root, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    });
    // Synthetic capability travels only over the child's private stdin pipe.
    worker.stdin.end(JSON.stringify({ token: control, parentPid: process.pid }) + "\n");
    let text = "", errors = "";
    worker.stderr.on("data", bytes => { errors = (errors + bytes.toString()).slice(-4000); });
    worker.stdout.on("data", bytes => {
      text += bytes.toString();
      const match = /SYNTHETIC_BENCHMARK_URL=(http:\/\/127\.0\.0\.1:\d+\/)/.exec(text);
      if (match) resolve(match[1]);
    });
    worker.on("error", reject);
    worker.on("exit", code => { if (code && !done) reject(new Error(errors || "Synthetic fixture exited")); });
  });
  clearTimeout(timer); createWindow(address, control);
  timer = setTimeout(() => finish(new Error("Synthetic desktop benchmark timed out")), 120000);
  await window.loadURL(address);
  fs.writeFileSync(progressFile, JSON.stringify({ stage: "loaded" }));
  const poll = async () => {
    if (done) return;
    const state = await window.webContents.executeJavaScript("({status:document.body.dataset.benchmark,text:document.getElementById('benchmarkReport')?.textContent})");
    fs.writeFileSync(progressFile, JSON.stringify(state));
    if (state.status === "failed") return finish(new Error(state.text));
    if (state.status === "complete") {
      const result = JSON.parse(fs.readFileSync(output, "utf8"));
      result.electron = process.versions.electron;
      result.node = process.versions.node;
      result.hardware = { cpu: os.cpus()[0]?.model, logicalCpus: os.cpus().length, memoryBytes: os.totalmem() };
      result.viewport = { width: 1440, height: 900, hidden: true, offscreen: true, frameRate: 60, backgroundThrottling: false, gpu: "disabled" };
      result.rendererProcessMemory = await window.webContents.executeJavaScript("performance.memory?({usedJSHeapSize:performance.memory.usedJSHeapSize,totalJSHeapSize:performance.memory.totalJSHeapSize}):null");
      fs.writeFileSync(output, JSON.stringify(result, null, 2));
      const screenshot = await window.webContents.capturePage();
      fs.writeFileSync(path.join(temporary, reportName + ".png"), screenshot.toPNG());
      process.stdout.write(JSON.stringify({ output, passed: result.passed, coldListMs: result.coldListMs,
        coldDetailMs: result.coldDetailMs, list: result.list, detail: result.detail, sides: result.sides, electron: result.electron }) + "\n");
      return finish(result.passed ? null : new Error("Viewer benchmark thresholds failed"));
    }
    setTimeout(() => { void poll().catch(finish); }, 100);
  };
  await poll();
}).catch(finish);
