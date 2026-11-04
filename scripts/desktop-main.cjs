"use strict";

const { app, dialog } = require("electron");
const { execFile } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { pythonFlags } = require("./real-client-recovery.cjs");

const root = app.isPackaged ? path.join(process.resourcesPath, "client") : path.resolve(__dirname, "..");

// The installed runtime does not carry a second Electron binary. Core can start
// only these packaged helpers, before the desktop launch or its instance lock.
// Intentional: each helper sets its own userData before any single-instance lock,
// exactly as it does when started from the source tree (dev mode).
if (app.isPackaged && process.argv[1] === "--kyf-sidecar") {
  const kind = process.argv[2];
  const args = process.argv.slice(3);
  const supported = ["ui-host", "pairing-window", "confirm-window"].includes(kind);
  const port = Number(/^http:\/\/127\.0\.0\.1:(\d{1,5})\/$/.exec(args[1])?.[1]);
  const validArgs = kind === "ui-host"
    ? args.length === 2 && args[0] === "--client-url" && port >= 1 && port <= 65535
    : args.length === 0;
  try {
    if (!supported || !validArgs) throw new Error("Invalid packaged sidecar command");
    const script = path.join(root, "scripts", `${kind}.cjs`);
    process.argv.splice(1, 2, script);
    require(script);
  } catch (_) {
    process.stdout.write(JSON.stringify({ event: "error", code: "sidecar-unavailable" }) + "\n", () => app.exit(2));
  }
  return;
}

const bundledPython = path.join(root, "runtime", "python", "python.exe");
const bundledNode = path.join(root, "runtime", "node", "node.exe");
const projectPython = path.join(root, ".venv", "Scripts", "python.exe");
const python = process.env.KYF_PYTHON || (fs.existsSync(bundledPython) ? bundledPython :
  fs.existsSync(projectPython) ? projectPython : "python");
const node = process.env.KYF_NODE || (fs.existsSync(bundledNode) ? bundledNode : "node");
const launcher = path.join(root, "scripts", "start-real-client.py");
let profileReady = false;
// True only when this launch created the bridge (not when it reused a ready one).
let bridgeCreated = false;
try {
  // Set this before app ready so Electron's single-instance lock is per installation.
  const userData = path.join(root, ".local", "real-client-shell");
  fs.mkdirSync(userData, { recursive: true });
  app.setPath("userData", userData);
  profileReady = true;
} catch (_) {
  // Report through the normal startup failure path once Electron is ready.
}

// Core opens a module in an already-running GUI (core_ui_host). Its bridge is up, so skip
// the launcher and start the shell at once: its instance lock forwards --open-module.
if (app.isPackaged && process.argv[1] === "--kyf-shell") {
  const args = process.argv.slice(2);
  const match = /^http:\/\/127\.0\.0\.1:(\d{1,5})\/$/.exec(args[1] || "");
  const port = Number(match?.[1]);
  const valid = args[0] === "--client-url" && port >= 1 && port <= 65535 &&
    (args.length === 2 || (args.length === 4 && args[2] === "--open-module" && args[3] === "settings"));
  if (!valid || !profileReady || !/^[a-f0-9]{64}$/.test(process.env.KYF_INSTANCE_ID || "")) {
    app.exit(2);
    return;
  }
  Object.assign(process.env, {
    KYF_CLIENT_ROOT: root, KYF_PYTHON: python, KYF_NODE: node, CHATUI_PORT: match[1], KYF_BRIDGE_CREATED: "0",
    PATH: (path.isAbsolute(node) ? path.dirname(node) + path.delimiter : "") + (process.env.PATH || ""),
  });
  process.argv.splice(1, 1);
  require("./real-client-shell.cjs");
  return;
}

// Ask the launcher to stop the bridge only when this launch created it. The launcher
// re-verifies the recorded PID creation time, install identity and control token, so a
// reused or foreign service is never touched. A JSON-invalid result is matched leniently
// for "created":true so a corrupt stdout cannot orphan a bridge we just started.
function stopOwnedBridge(callback) {
  const owned = bridgeCreated;
  if (!owned) {
    callback();
    return;
  }
  const environment = {
    ...process.env, KYF_CLIENT_ROOT: root, KYF_PYTHON: python,
  };
  try {
    execFile(python, [...pythonFlags(root, python), launcher, "--stop-owned-bridge", "--json"], {
      cwd: root, env: environment, windowsHide: true, timeout: 30000, maxBuffer: 65536,
    }, () => callback());
  } catch (_) {
    callback();
  }
}

function fail() {
  dialog.showErrorBox("知己微信助手启动失败", "本地服务未就绪，请检查运行文件是否完整。");
  app.quit();
}

function failAfterCleanup() {
  stopOwnedBridge(() => fail());
}

app.whenReady().then(() => {
  if (!profileReady || !fs.existsSync(launcher) ||
      (app.isPackaged && (!fs.existsSync(python) || !fs.existsSync(node)))) {
    fail();
    return;
  }
  const environment = {
    ...process.env,
    KYF_CLIENT_ROOT: root,
    KYF_PYTHON: python,
    KYF_NODE: node,
    PATH: (path.isAbsolute(node) ? path.dirname(node) + path.delimiter : "") + (process.env.PATH || ""),
  };
  // Desktop launches derive their port from this installation, even when a
  // terminal or parent process exported an older client's CHATUI_PORT.
  delete environment.CHATUI_PORT;
  execFile(python, [...pythonFlags(root, python), launcher, "--no-open", "--json"], {
    // Replacing an owned old bridge may take its bounded shutdown plus startup.
    cwd: root, env: environment, windowsHide: true, timeout: 90000, maxBuffer: 65536,
  }, (error, stdout) => {
    if (error) {
      // The launcher stops a bridge it started before failing, so do not stop one here.
      fail();
      return;
    }
    let result;
    try {
      result = JSON.parse(stdout);
      bridgeCreated = result.created === true;
      const match = /^http:\/\/127\.0\.0\.1:(\d{1,5})$/.exec(result.url);
      if (result.version !== "real-ui-1" || !match || !/^[a-f0-9]{64}$/.test(result.instanceId) ||
          Number(match[1]) < 1 || Number(match[1]) > 65535) throw new Error("Invalid launcher result");
      Object.assign(process.env, environment, {
        CHATUI_PORT: match[1], KYF_INSTANCE_ID: result.instanceId,
        KYF_BRIDGE_CREATED: bridgeCreated ? "1" : "0",
      });
    } catch (_) {
      if (!bridgeCreated) bridgeCreated = /"created"\s*:\s*true/.test(String(stdout || ""));
      failAfterCleanup();
      return;
    }
    process.argv.push("--client-url", `${result.url}/`);
    try {
      require("./real-client-shell.cjs");
    } catch (_) {
      failAfterCleanup();
    }
  });
}).catch(fail);
