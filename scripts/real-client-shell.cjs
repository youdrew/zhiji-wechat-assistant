const { app, BrowserWindow, clipboard, dialog, ipcMain, Notification, session, shell, screen } = require("electron");
// Modified by Know Your Friends, 2026-10-10: isolated native image preview windows.
// Modified by Know Your Friends, 2026-10-10: independent 知己分析 windows with draft flush on exit.
// Modified 2026-10-13: a generic docked plugin panel, host-picked files as Core file grants and plugin-page-only frames.
// Modified 2026-10-22: chat-toolbar plugin pages open in one independent window per friend (scripts/plugin-windows.cjs).
const { execFile } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { monitorBridge, pythonFlags } = require("./real-client-recovery.cjs");
const { checkForUpdates, downloadAndStageUpdate, errorStatus, RELEASES_URL } = require("./real-client-update.cjs");
const { createUpdateProxyFetch } = require("./real-client-update-proxy.cjs");
const { readControlToken } = require("./model-traces-host.cjs");
function packagedUpdatesEnabled() {
  if (app.isPackaged !== true) return false;
  try {
    // Opt in only after this product has its own verified release/signing setup.
    const metadata = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
    return metadata?.kyfUpdatesEnabled === true;
  } catch {
    return false;
  }
}
const appUpdatesEnabled = packagedUpdatesEnabled();

const ROOT = process.env.KYF_CLIENT_ROOT ?
  path.resolve(process.env.KYF_CLIENT_ROOT) : path.resolve(__dirname, "..");
const THEMES = Object.freeze({
  dark: { color: "#1b1b1b", symbolColor: "#e6e7eb", height: 36 },
  light: { color: "#edf3f7", symbolColor: "#28333d", height: 36 },
});
const DOC_URLS = new Set([
  "https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/",
  "https://www.themyersbriggs.com/en-US/Products-and-Services/Myers-Briggs",
  "https://github.com/youdrew",
  "https://github.com/youdrew/zhiji-wechat-assistant",
  "https://eugenepage.com/",
  "mailto:youdrewxuan@icloud.com",
  "https://github.com/tswawa",
  "https://github.com/tswawa/WechatVibe",
  RELEASES_URL,
]);

function clientUrl(value) {
  if (typeof value !== "string" || !/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(value)) return null;
  const url = new URL(value);
  const port = Number(url.port);
  return port >= 1 && port <= 65535 ? `http://127.0.0.1:${port}/` : null;
}

// Modified 2026-10-27: the GUI holds its own Core lease and, when MCP clients keep the
// Core alive at exit, releases only its lease and notes the background run instead of
// stopping the bridge. Only the main process holds the control token for these routes.
function controlPost(target, body) {
  const token = readControlToken(ROOT, url, instanceId);
  if (!token) return Promise.resolve(null);
  return fetch(new URL(target, url), { method: "POST", redirect: "error",
    headers: { "Content-Type": "application/json", "X-KYF-Control-Token": token },
    body: JSON.stringify(body) }).then(async response => response.ok ? response.json() : null)
    .catch(() => null);
}

function acquireGuiLease() {
  if (selfTest || updateValidation) return;
  void controlPost("api/control/lease", { action: "acquire", pid: process.pid });
}

function releaseGuiLease() {
  void controlPost("api/control/lease", { action: "release" });
}

function backgroundStatus() {
  return controlPost("api/control/background", null);
}

function notifyBackgroundRunning(connections) {
  const detail = `有 ${connections} 个 MCP 客户端连接仍在使用本地服务；主窗口已退出，` +
    "知己将在最后一个连接结束后自动关闭后台服务。";
  try {
    if (Notification && Notification.isSupported()) {
      new Notification({ title: "知己仍在后台运行", body: detail,
        icon: path.join(ROOT, "chatui", "assets", "app-icon.png"), silent: true }).show();
      return;
    }
  } catch (_) { /* fall through to the dialog */ }
  dialog.showMessageBox({ type: "info", message: "知己仍在后台运行", detail, buttons: ["好"] });
}

function argument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const url = clientUrl(argument("--client-url"));
const selfTest = process.argv.includes("--self-test");
const instanceId = process.env.KYF_INSTANCE_ID;
const updateValidation = appUpdatesEnabled && process.env.KYF_UPDATE_VALIDATE === "1";
const updateFinalReady = appUpdatesEnabled && !updateValidation &&
  typeof process.env.KYF_UPDATE_FINAL_READY_FILE === "string" &&
  typeof process.env.KYF_UPDATE_FINAL_READY_NONCE === "string";
const updateReadyFile = updateValidation ? process.env.KYF_UPDATE_READY_FILE :
  process.env.KYF_UPDATE_FINAL_READY_FILE;
const updateReadyNonce = updateValidation ? process.env.KYF_UPDATE_READY_NONCE :
  process.env.KYF_UPDATE_FINAL_READY_NONCE;
if (process.platform !== "win32" || !url || (!selfTest && !/^[a-f0-9]{64}$/.test(instanceId || ""))) {
  process.stderr.write("real-client shell requires Windows and a validated loopback URL\n");
  app.exit(1);
} else {
  app.setAppUserModelId("com.local.knowyourfriends.real-client");
  const userData = path.join(ROOT, ".local", selfTest ? "real-client-shell-self-test" : "real-client-shell");
  fs.mkdirSync(userData, { recursive: true });
  app.setPath("userData", userData);
  let window = null;
  let pluginWindows = null;
  let modelTracesHost = null;
  let modelTracesWindow = null;
  let imagePreviews = null;
  let friendWindows = null;
  let selfAnalysisWindow = null;
  let historyWindows = null;
  let externalImportWindows = null;
  let settingsWindow = null;
  let friendExitReady = false;
  let friendExitPending = null;
  let appExitPending = null;
  let stopBridgeMonitor = null;
  let loadRetries = 0;
  let validationTimer = null;
  let exiting = false;
  // Set by desktop-main: only the launch that created the bridge may stop it on a
  // startup failure. A reused ready bridge must survive a failed shell start.
  const bridgeCreated = process.env.KYF_BRIDGE_CREATED === "1";
  let startupFailed = false;
  let updateHandoff = "none";
  let quitRequestedDuringHandoff = false;
  let bridgeExitState = "idle";
  let updateCheckPromise = null;
  let updateController = null;
  let updateNetwork = null;
  let savedUpdateFallbackActive = false;
  const testState = { themes: [], blockedPopups: 0, themeWaiter: null };

  async function checkWithUpdateNetwork(version) {
    if (!updateNetwork) return { status: "server-error" };
    const options = { fetchImpl: updateNetwork.fetchImpl };
    const first = await checkForUpdates(version, options);
    if ((first.status === "offline" || first.status === "timeout") &&
        !savedUpdateFallbackActive && await updateNetwork.enableSavedLoopbackFallback()) {
      savedUpdateFallbackActive = true;
      return checkForUpdates(version, options);
    }
    return first;
  }

  async function stageWithUpdateNetwork(version, installRoot, onProgress) {
    const options = { fetchImpl: updateNetwork.fetchImpl };
    try {
      return await downloadAndStageUpdate(version, installRoot, onProgress, options);
    } catch (error) {
      const status = errorStatus(error);
      if (!savedUpdateFallbackActive && (status === "offline" || status === "timeout") &&
          await updateNetwork.enableSavedLoopbackFallback()) {
        savedUpdateFallbackActive = true;
        return downloadAndStageUpdate(version, installRoot, onProgress, options);
      }
      throw error;
    }
  }

  function trustedFrame(event) {
    return window && !window.isDestroyed() && event.sender === window.webContents &&
      event.senderFrame === window.webContents.mainFrame && event.senderFrame.url === url;
  }
  const trustedSettingsFrame = event => settingsWindow?.trusted(event) === true;
  const trustedAppFrame = event => trustedFrame(event) || trustedSettingsFrame(event);

  // Quitting first persists every 知己分析 window's drafts; only a failed save asks.
  function confirmFriendExit() {
    if (friendExitReady || !friendWindows || !friendWindows.count()) {
      friendExitReady = true;
      friendWindows?.destroyAll();
      return Promise.resolve(true);
    }
    if (!friendExitPending) {
      friendExitPending = friendWindows.flushAll().then(async result => {
        if (!result.ok && window && !window.isDestroyed()) {
          const choice = await dialog.showMessageBox(window, { type: "warning", buttons: ["取消退出", "仍然退出"],
            defaultId: 0, cancelId: 0, message: "部分知己分析窗口的编辑暂存失败",
            detail: "取消退出后可以在对应窗口重试；仍然退出会丢失尚未暂存的文字。" });
          if (choice.response !== 1) return false;
        }
        friendExitReady = true;
        friendWindows.destroyAll();
        return true;
      }).finally(() => { friendExitPending = null; });
    }
    return friendExitPending;
  }
  function needsExitConfirmation() {
    return (!friendExitReady && friendWindows?.count()) || externalImportWindows?.count();
  }
  function confirmAppExit() {
    if (!appExitPending) appExitPending = (async () => {
      if (externalImportWindows?.count() && !await externalImportWindows.requestCloseAll()) return false;
      return confirmFriendExit();
    })().finally(() => { appExitPending = null; });
    return appExitPending;
  }

  // Modified 2026-10-13: plugin pages run in sandboxed frames; give each its own renderer process
  // so a stuck or crashed plugin page cannot freeze the window that hosts it.
  // The default groups every plugin served by Core into one site process. Per-document
  // grouping keeps another plugin and a reloaded page responsive after one frame hangs.
  app.commandLine.appendSwitch("enable-features", "IsolateSandboxedIframes:grouping/per-document");
  if (!app.requestSingleInstanceLock()) {
    app.quit();
  } else {
    app.on("second-instance", () => {
      if (window && !window.isDestroyed()) {
        if (window.isMinimized()) window.restore();
        window.focus();
      }
    });

    ipcMain.on("real-client:set-theme", (event, theme) => {
      if (!trustedAppFrame(event) || !Object.hasOwn(THEMES, theme)) return;
      if (trustedSettingsFrame(event)) {
        settingsWindow.senderWindow(event)?.setBackgroundColor(THEMES[theme].color);
        return;
      }
      window.setTitleBarOverlay(THEMES[theme]);
      pluginWindows?.setTheme(theme);
      historyWindows?.setTheme(theme);
      externalImportWindows?.setTheme(theme);
      if (selfTest) {
        testState.themes.push({ theme, ...THEMES[theme] });
        if (testState.themes.length === 2 && testState.themeWaiter) testState.themeWaiter();
      }
    });

    // A plugin page asks for a local file or folder: the user picks it here, Core keeps the path and the
    // page gets only a one-time grant token for its own plugin.
    ipcMain.handle("real-client:choose-plugin-file", async (event, plugin, kind, activated) => {
      if (!trustedFrame(event) || activated !== true || typeof plugin !== "string" || !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(plugin) ||
          !["file", "directory"].includes(kind) || selfTest) return null;
      const selection = await dialog.showOpenDialog(window, {
        title: kind === "directory" ? "选择文件夹" : "选择文件", properties: [kind === "directory" ? "openDirectory" : "openFile"],
        ...(kind === "file" ? { filters: [{ name: "压缩包或 Markdown", extensions: ["zip", "md"] }] } : {}),
      });
      if (selection.canceled || selection.filePaths.length !== 1 || !trustedFrame(event)) return null;
      const token = readControlToken(ROOT, url, instanceId);
      if (!token) return { error: "本地服务暂时无法连接，请稍后重试" };
      try {
        const response = await fetch(new URL("api/plugins/file-grant", url), { method: "POST", redirect: "error",
          headers: { "Content-Type": "application/json", "X-KYF-Control-Token": token },
          body: JSON.stringify({ plugin, path: selection.filePaths[0] }) });
        let value = null;
        try { value = await response.json(); } catch { /* reported below */ }
        if (response.ok && typeof value?.token === "string" && /^fg_[a-f0-9]{32}$/.test(value.token))
          return { token: value.token, label: typeof value.label === "string" ? value.label.slice(0, 256) : "" };
        return { error: typeof value?.message === "string" && value.message ? value.message.slice(0, 200) : "文件读取失败，请重试" };
      } catch {
        return { error: "本地服务暂时无法连接，请稍后重试" };
      }
    });

    ipcMain.on("real-client:open-doc", (event, target, trusted, active) => {
      if (!trustedAppFrame(event) || trusted !== true || active !== true || typeof target !== "string") return;
      let parsed;
      try {
        parsed = new URL(target);
      } catch (_) {
        return;
      }
      if (!["https:", "mailto:"].includes(parsed.protocol) || parsed.username || parsed.password || !DOC_URLS.has(parsed.href)) return;
      if (!selfTest) {
        void shell.openExternal(parsed.href);
      }
    });

    ipcMain.handle("real-client:open-image-preview", (event, scope, activated) => {
      if (!trustedFrame(event) || activated !== true || selfTest || updateValidation || exiting) return false;
      return imagePreviews?.open(scope) ?? false;
    });
    require("./chat-link-host.cjs").attachChatLinks({ ipcMain, trustedMainFrame: trustedFrame, shell,
      available: () => !selfTest && !updateValidation && !exiting && updateHandoff === "none" });
    ipcMain.on("real-client:close-image-previews", event => {
      if (trustedFrame(event)) imagePreviews?.closeAll();
    });

    ipcMain.handle("real-client:copy-draft", (event, value) => {
      if (!trustedAppFrame(event) || typeof value !== "string" || !value.trim() || value.length > 1_000_000) return false;
      clipboard.writeText(value);
      return true;
    });

    ipcMain.handle("real-client:app-version", (event) => {
      if (!trustedAppFrame(event)) return null;
      return app.getVersion();
    });

    ipcMain.handle("real-client:data-root-choose-directory", async (event) => {
      if (!trustedAppFrame(event) || selfTest || updateValidation || !window) return null;
      const choice = await dialog.showOpenDialog(settingsWindow?.senderWindow(event) || window, {
        title: "选择微信聊天记录目录", defaultPath: app.getPath("documents"),
        properties: ["openDirectory"],
      });
      return choice.canceled || !trustedAppFrame(event) ? null : choice.filePaths[0] || null;
    });

    ipcMain.handle("real-client:check-updates", (event) => {
      if (!trustedAppFrame(event)) return { status: "blocked" };
      if (!appUpdatesEnabled) return { status: "disabled" };
      if (updateController) return updateController.check();
      if (!updateCheckPromise) {
        updateCheckPromise = checkWithUpdateNetwork(app.getVersion())
          .finally(() => { updateCheckPromise = null; });
      }
      return updateCheckPromise;
    });

    ipcMain.handle("real-client:update-state", (event) => {
      if (!trustedAppFrame(event)) return { phase: "blocked" };
      if (!appUpdatesEnabled) return { phase: "disabled" };
      return updateController?.getState() || { phase: "idle", currentVersion: app.getVersion() };
    });

    ipcMain.handle("real-client:begin-update", (event) => {
      if (!trustedAppFrame(event)) return { phase: "blocked" };
      if (!appUpdatesEnabled) return { phase: "disabled" };
      if (!updateController) return { phase: "blocked" };
      return updateController.begin();
    });

    ipcMain.handle("real-client:rollback-update", (event) => {
      if (!trustedAppFrame(event)) return { phase: "blocked" };
      if (!appUpdatesEnabled) return { phase: "disabled" };
      if (!updateController) return { phase: "blocked" };
      return updateController.rollback();
    });

    ipcMain.handle("real-client:update-ui-ready", async (event) => {
      if (!appUpdatesEnabled || (!updateValidation && !updateFinalReady) || !trustedFrame(event) || selfTest ||
          !window.isVisible() || window.isMinimized() ||
          typeof updateReadyFile !== "string" || typeof updateReadyNonce !== "string" ||
          !path.isAbsolute(updateReadyFile) || path.resolve(updateReadyFile) !== updateReadyFile ||
          !/^[a-f0-9]{32,64}$/.test(updateReadyNonce) ||
          !/^[a-f0-9]{64}$/.test(instanceId || "")) return false;
      try {
        const installRoot = path.resolve(ROOT, "..", "..");
        const workDir = path.dirname(path.resolve(updateReadyFile));
        const parent = path.dirname(installRoot);
        if (path.dirname(workDir).toLowerCase() !== parent.toLowerCase() ||
            !path.basename(workDir).startsWith(".kyf-update-") ||
            !/^ui-(?:final-)?ready-[a-f0-9-]{20,80}\.json$/.test(path.basename(updateReadyFile)) ||
            fs.lstatSync(workDir).isSymbolicLink() ||
            fs.realpathSync.native(workDir).toLowerCase() !== workDir.toLowerCase()) return false;
        fs.writeFileSync(updateReadyFile, JSON.stringify({
          nonce: updateReadyNonce, expectedVersion: app.getVersion(), instanceId,
        }), { flag: "wx", mode: 0o600 });
        if (updateValidation) return true;
        const journalFile = path.join(workDir, "journal.json");
        const deadline = Date.now() + 60000;
        while (Date.now() < deadline && window && !window.isDestroyed()) {
          try {
            const stat = fs.statSync(journalFile);
            if (stat.isFile() && stat.size < 32768) {
              const journal = JSON.parse(fs.readFileSync(journalFile, "utf8"));
              if (journal.phase === "succeeded" && journal.guiStarted === true &&
                  journal.expectedVersion === app.getVersion() &&
                  path.resolve(journal.installRoot).toLowerCase() === installRoot.toLowerCase()) {
                if (validationTimer) clearTimeout(validationTimer);
                validationTimer = null;
                return true;
              }
              if (["failed", "rolled_back"].includes(journal.phase)) return false;
            }
          } catch (_) { /* The helper may be replacing its atomic journal. */ }
          await new Promise(resolve => setTimeout(resolve, 200));
        }
      } catch (_) { /* A bad marker must never release chat loading. */ }
      return false;
    });

    ipcMain.handle("real-client:exit-app", (event) => {
      if (!trustedAppFrame(event)) return false;
      const proceed = () => {
        exiting = true;
        pluginWindows?.close();
        stopBridgeMonitor?.();
        setImmediate(() => app.quit());
        return true;
      };
      if (!needsExitConfirmation()) return proceed();
      return confirmAppExit().then(ok => ok && trustedAppFrame(event) ? proceed() : false);
    });

    // Register the ownership-aware quit path before any ready-time initialization
    // can throw.  A proxy/model/window setup failure must still reclaim a bridge
    // created by this launch; a reused bridge remains untouched.
    app.on("before-quit", event => {
      if (needsExitConfirmation()) {
        event.preventDefault();
        void confirmAppExit().then(ok => { if (ok) app.quit(); });
        return;
      }
      exiting = true;
      pluginWindows?.close();
      if (validationTimer) clearTimeout(validationTimer);
      const recoveryDrain = Promise.resolve(stopBridgeMonitor?.());
      if (updateHandoff === "preparing") {
        quitRequestedDuringHandoff = true;
        event.preventDefault();
        return;
      }
      if (selfTest || updateValidation || updateHandoff === "ready" || bridgeExitState === "done") return;
      if (startupFailed && !bridgeCreated) return; // A reused bridge is not ours to stop.
      event.preventDefault();
      if (bridgeExitState === "running") return;
      bridgeExitState = "running";
      const bundledPython = path.join(ROOT, "runtime", "python", "python.exe");
      const python = process.env.KYF_PYTHON ||
        (fs.existsSync(bundledPython) ? bundledPython : "python");
      const launcher = path.join(ROOT, "scripts", "start-real-client.py");
      const finish = (error, stdout) => {
        bridgeExitState = "done";
        let stopped = false;
        try {
          const result = JSON.parse(stdout);
          stopped = !error && (result.stopped === true || result.alreadyStopped === true);
        } catch (_) { /* A missing result is a shutdown failure. */ }
        if (!stopped) {
          dialog.showErrorBox("知己微信助手退出提示",
            "本地分析服务未能安全关闭，程序文件可能仍被占用。请在任务管理器中检查此安装目录的后台进程。");
        }
        app.quit();
      };
      void Promise.resolve(recoveryDrain).catch(() => {}).then(async () => {
        releaseGuiLease();
        // MCP 客户端仍在使用时只释放 GUI 租约并提示一次；后台由空闲退出收尾。
        const background = await backgroundStatus();
        if (background && background.mcpConnections > 0) {
          bridgeExitState = "done";
          notifyBackgroundRunning(background.mcpConnections);
          app.quit();
          return;
        }
        try {
          execFile(python, [...pythonFlags(ROOT, python), launcher, "--stop-owned-bridge", "--json"], {
            cwd: ROOT, windowsHide: true, timeout: 90000, maxBuffer: 65536,
            env: { ...process.env, CHATUI_PORT: String(new URL(url).port),
              KYF_CLIENT_ROOT: ROOT, KYF_PYTHON: python },
          }, finish);
        } catch (error) {
          finish(error, "");
        }
      });
    });

    app.whenReady().then(() => {
      if (appUpdatesEnabled) updateNetwork = createUpdateProxyFetch({
        session: session.defaultSession,
        ProxyAgent: require(path.join(ROOT, "node_modules", "undici")).ProxyAgent,
      });
      session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
      session.defaultSession.setPermissionCheckHandler(() => false);
      session.defaultSession.on("will-download", (event) => event.preventDefault());
      session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
        try {
          const scheme = new URL(details.url).protocol;
          callback({ cancel: scheme === "file:" || scheme === "ftp:" });
        } catch (_) {
          callback({ cancel: true });
        }
      });
      window = new BrowserWindow({
        width: 1180,
        height: 780,
        minWidth: 720,
        minHeight: 520,
        icon: path.join(ROOT, "chatui", "assets", "app-icon.ico"),
        show: !selfTest,
        resizable: true,
        titleBarStyle: "hidden",
        titleBarOverlay: THEMES.dark,
        webPreferences: {
          preload: path.join(__dirname, "real-client-preload.cjs"),
          additionalArguments: appUpdatesEnabled ? ["--kyf-app-updates-enabled"] : [],
          contextIsolation: true,
          sandbox: true,
          nodeIntegration: false,
          webSecurity: true,
          devTools: false,
        },
      });
      if (!selfTest && screen) imagePreviews = require("./image-preview.cjs").createImagePreviews({
        BrowserWindow, mainWindow: window, clientUrl: url, screen,
      });
      if (!selfTest && screen) friendWindows = require("./friend-windows.cjs").createFriendWindows({
        BrowserWindow, mainWindow: window, clientUrl: url, screen, ipcMain, dialog, userDataPath: userData,
        trustedMainFrame: trustedFrame,
      });
      if (!selfTest && screen) selfAnalysisWindow = require("./self-analysis-window.cjs").createSelfAnalysisWindow({
        BrowserWindow, mainWindow: window, clientUrl: url, screen, ipcMain, clipboard,
        trustedMainFrame: trustedFrame, root: ROOT,
      });
      if (!selfTest && screen) historyWindows = require("./history-windows.cjs").createHistoryWindows({
        BrowserWindow, mainWindow: window, clientUrl: url, screen, ipcMain, trustedMainFrame: trustedFrame,
        openImagePreview: scope => imagePreviews?.open(scope) ?? false,
      });
      if (!selfTest && screen) externalImportWindows = require("./external-import-windows.cjs").createExternalImportWindows({
        BrowserWindow, mainWindow: window, clientUrl: url, screen, ipcMain, dialog, trustedMainFrame: trustedFrame,
      });
      if (!selfTest && screen) settingsWindow = require("./settings-window.cjs").createSettingsWindow({
        BrowserWindow, mainWindow: window, clientUrl: url, screen, ipcMain, trustedMainFrame: trustedFrame,
        root: ROOT, instanceId, appUpdatesEnabled,
        openSelfAnalysis: () => selfAnalysisWindow?.openCurrent() ?? false,
      });
      if (!selfTest) modelTracesHost = require("./model-traces-host.cjs").attachModelTraces({
        ipcMain, mainWindow: window, clientUrl: url, trustedMainFrame: trustedFrame, root: ROOT, instanceId,
      });
      if (!selfTest && screen) modelTracesWindow = require("./model-traces-window.cjs").createModelTracesWindow({
        BrowserWindow, mainWindow: window, clientUrl: url, screen, ipcMain, clipboard,
        trustedMainFrame: trustedFrame, root: ROOT, instanceId,
        trustedOpenFrame: trustedAppFrame,
      });
      if (!selfTest && screen) pluginWindows = require("./plugin-windows.cjs").createPluginWindows({
        BrowserWindow, ipcMain, screen, clipboard, mainWindow: window, clientUrl: url, userDataPath: userData,
        trustedMainFrame: trustedFrame,
      });
      if (updateValidation || updateFinalReady) {
        validationTimer = setTimeout(() => app.quit(), updateValidation ? 60000 : 90000);
      }
      const contents = window.webContents;
      contents.on("will-attach-webview", (event) => event.preventDefault());
      contents.on("will-navigate", (event, target) => {
        if (target !== url) event.preventDefault();
      });
      contents.on("will-redirect", (event, target) => {
        if (target !== url) event.preventDefault();
      });
      // Plugin pages are the only frames embedded in the main window.
      const pluginPages = new URL("plugin-pages/", url).href;
      contents.on("will-frame-navigate", event => {
        if (event.isMainFrame === false && (typeof event.url !== "string" || !event.url.startsWith(pluginPages))) event.preventDefault();
      });
      contents.setWindowOpenHandler((details) => {
        if (selfTest) testState.blockedPopups += 1;
        return { action: "deny" };
      });
      contents.on("did-fail-load", (_event, code, description, validatedUrl, isMainFrame) => {
        if (!isMainFrame) return;
        if (selfTest) {
          process.stdout.write(JSON.stringify({ ok: false, code, description, url: validatedUrl }) + "\n");
          app.exit(1);
          return;
        }
        // The owned bridge may still be starting when the window first opens. Retry the
        // local page instead of leaving the user on a blank white content area.
        if (exiting || loadRetries >= 20 || !window || window.isDestroyed()) return;
        loadRetries += 1;
        setTimeout(() => {
          if (!exiting && window && !window.isDestroyed()) void window.webContents.loadURL(url);
        }, 600);
      });
      contents.on("did-finish-load", () => { loadRetries = 0; });
      if (selfTest) {
        contents.once("did-finish-load", async () => {
          try {
            const themeDone = new Promise((resolve) => { testState.themeWaiter = resolve; });
            const result = await contents.executeJavaScript(
              "(async () => ({ platform: window.desktopHost?.platform, accepted: window.desktopHost?.setTheme('light'), darkAccepted: window.desktopHost?.setTheme('dark'), rejected: window.desktopHost?.setTheme('invalid'), frozen: Object.isFrozen(window.desktopHost), startupAccessible: document.querySelector('#startupOverlay')?.hidden === true && !document.querySelector('#appWindow')?.hasAttribute('inert'), copyDraftExposed: typeof window.desktopHost?.copyDraft === 'function', emptyCopyRejected: await window.desktopHost?.copyDraft('') === false, nodeAccess: typeof require, popup: window.open('https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/') === null }))()",
            );
            await themeDone;
            process.stdout.write(JSON.stringify({ ok: true, hidden: !window.isVisible(), ...result,
              themes: testState.themes, blockedPopups: testState.blockedPopups }) + "\n");
            app.quit();
          } catch (error) {
            process.stdout.write(JSON.stringify({ ok: false, error: String(error) }) + "\n");
            app.exit(1);
          }
        });
      }
      window.on("close", event => {
        if (updateHandoff === "preparing") {
          quitRequestedDuringHandoff = true;
          event.preventDefault();
          return;
        }
        if (needsExitConfirmation()) {
          event.preventDefault();
          void confirmAppExit().then(ok => { if (ok && window && !window.isDestroyed()) window.close(); });
        }
      });
      window.on("closed", () => { pluginWindows?.close(); selfAnalysisWindow?.close(); historyWindows?.close(); externalImportWindows?.close(); settingsWindow?.close(); modelTracesWindow?.close(); modelTracesHost?.close(); window = null; stopBridgeMonitor?.(); });
      if (!selfTest && !updateValidation) {
        const startBridgeMonitor = () => {
          if (stopBridgeMonitor || !window || window.isDestroyed()) return;
          stopBridgeMonitor = monitorBridge({
            root: ROOT, url, instanceId,
            isOpen: () => !exiting && !!window && !window.isDestroyed(),
            onRecovered: () => {
              acquireGuiLease();
              if (!exiting && window && !window.isDestroyed()) {
                window.webContents.send("real-client:bridge-restored");
                settingsWindow?.broadcast("real-client:bridge-restored");
                selfAnalysisWindow?.sync({ event: { type: "service-restored" } });
                historyWindows?.broadcast({ type: "service-restored" });
                externalImportWindows?.broadcast({ type: "service-restored" });
              }
            },
          });
        };
        if (appUpdatesEnabled) {
          const { createUpdateController } = require("./real-client-update-controller.cjs");
          updateController = createUpdateController({
            app, root: ROOT, port: Number(new URL(url).port), instanceId,
            checkImpl: checkWithUpdateNetwork,
            stageImpl: stageWithUpdateNetwork,
            onState: state => {
              if (window && !window.isDestroyed()) window.webContents.send("real-client:update-state", state);
              settingsWindow?.broadcast("real-client:update-state", state);
            },
            pauseRecovery: async () => {
              updateHandoff = "preparing";
              if (!await confirmAppExit()) throw new Error("已保留外部聊天窗口，更新已取消");
              exiting = true;
              const drained = stopBridgeMonitor?.();
              stopBridgeMonitor = null;
              return drained;
            },
            resumeRecovery: () => {
              updateHandoff = "none";
              if (quitRequestedDuringHandoff) setImmediate(() => app.quit());
              else { exiting = false; startBridgeMonitor(); }
            },
            quit: () => { updateHandoff = "ready"; app.quit(); },
          });
        }
        startBridgeMonitor();
        acquireGuiLease();
      }
      void contents.loadURL(url);
    }).catch((error) => {
      process.stderr.write(String(error) + "\n");
      startupFailed = true;
      dialog.showErrorBox("知己微信助手启动失败", "客户端窗口初始化失败，请重新解压完整安装包。");
      // Use normal shutdown so the bridge created before window initialization
      // is stopped instead of becoming an invisible background process. A reused
      // bridge is left alone by the before-quit guard.
      app.quit();
    });
    app.on("window-all-closed", () => app.quit());
  }
}
