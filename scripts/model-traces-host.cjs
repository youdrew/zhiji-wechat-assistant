"use strict";
// Know Your Friends, Apache-2.0. Credentials remain in the trusted main process.
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const viewers = new WeakMap();

function readControlToken(root, clientUrl, instanceId) {
  try {
    const realRoot = fs.realpathSync(root);
    const directory = path.join(realRoot, ".local", "real-client-runtime");
    for (const item of [path.join(realRoot, ".local"), directory]) {
      if (fs.lstatSync(item).isSymbolicLink() || fs.realpathSync(item) !== item) return null;
    }
    const port = Number(new URL(clientUrl).port);
    for (const name of fs.readdirSync(directory)) {
      if (!/^bridge-\d+\.json$/.test(name)) continue;
      const file = path.join(directory, name), stat = fs.lstatSync(file);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 65536) continue;
      const value = JSON.parse(fs.readFileSync(file, "utf8"));
      if (value.instance_id !== instanceId || value.port !== port || !Number.isSafeInteger(value.pid) ||
          !/^[a-f0-9]{64}$/.test(value.control_token || "")) continue;
      try { process.kill(value.pid, 0); } catch { continue; }
      return value.control_token;
    }
  } catch { /* Do not include the control file or its contents in errors. */ }
  return null;
}

function attachModelTraces(options) {
  const { ipcMain, mainWindow, trustedMainFrame, clientUrl, root, instanceId } = options;
  const webContents = mainWindow.webContents;
  const fetchImpl = options.fetchImpl || fetch;
  const getControlToken = options.getControlToken || (() => readControlToken(root, clientUrl, instanceId));
  const clientId = `desktop-${webContents.id || "main"}-${randomUUID()}`;
  let session = null, requestedScope = "application", epoch = 0, closed = false, mint = null;
  const requests = new Set();
  let registry = viewers.get(ipcMain);
  if (!registry) {
    registry = new Set(); viewers.set(ipcMain, registry);
    ipcMain.handle("model-traces:request", (event, operation, args) => {
      const viewer = [...registry].find(item => item.trusted(event));
      return viewer ? viewer.handle(event, operation, args) : { ok: false, error: "trace-forbidden", statusCode: 403 };
    });
  }
  const origin = new URL(clientUrl).origin;
  let deleteVersion = null;
  const registration = { trusted: trustedMainFrame, origin, reset, handle: null,
    invalidate(version) {
      if (closed) return;
      if (Number.isSafeInteger(version)) deleteVersion = version;
      reset();
      if (!webContents.isDestroyed?.()) webContents.send?.("model-traces:invalidated", { deleteVersion: version });
    } };
  registry.add(registration);
  function invalidatePeers(version) {
    for (const viewer of registry) if (viewer !== registration && viewer.origin === origin) viewer.invalidate(version);
  }
  function reset() {
    epoch++; session = null; mint = null;
    for (const controller of requests) controller.abort();
    requests.clear();
  }
  async function transport(route, method, payload, header, signal) {
    const response = await fetchImpl(new URL(`api/model-traces/${route}`, clientUrl), {
      method, redirect: "error", headers: { ...header, ...(payload ? { "Content-Type": "application/json" } : {}) },
      ...(payload ? { body: JSON.stringify(payload) } : {}), signal,
    });
    const data = await response.json();
    return { ...data, ok: response.ok, statusCode: response.status };
  }
  async function getSession(signal) {
    if (session) return session;
    if (!mint) {
      const ticket = epoch, token = getControlToken();
      if (!token) throw new Error("trace-unavailable");
      mint = transport("session", "POST", { clientId, scope: requestedScope }, { "X-KYF-Control-Token": token }, signal)
        .then(value => {
          if (ticket !== epoch || closed) throw new Error("trace-stale");
          if (!value.ok || typeof value.token !== "string") throw new Error("trace-unavailable");
          session = value; return value;
        }).finally(() => { if (ticket === epoch) mint = null; });
    }
    return mint;
  }
  const allowed = new Set(["scope", "state", "configure", "records", "detail", "body", "events", "clear"]);
  // Modified 2026-10-13: the always-on usage ledger (Core /api/usage/*) needs no trace session.
  const usageRoutes = { "usage-summary": "summary", "usage-friends": "friends", "usage-records": "records",
    "usage-timeline": "timeline", "usage-modules": "modules" };
  const usageKeys = ["since", "until", "friend", "plugin", "step", "task", "model", "status", "order", "before", "limit"];
  async function usage(event, operation, args) {
    const text = value => typeof value === "string" && value.length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f]/u.test(value);
    if (!text(args.account) || args.account.length > 128) return { ok: false, error: "usage-invalid-account", statusCode: 400 };
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 7000);
    try {
      let response;
      if (operation === "usage-clear") {
        if (args.confirm !== true) return { ok: false, error: "usage-confirmation-required" };
        response = await fetchImpl(new URL("api/usage/clear", clientUrl), { method: "POST", redirect: "error", signal: controller.signal,
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account: args.account }) });
      } else {
        const query = new URLSearchParams({ account: args.account });
        for (const key of usageKeys) if (["string", "number"].includes(typeof args[key]) && text(String(args[key]))) query.set(key, String(args[key]));
        if (operation === "usage-timeline") {
          if (typeof args.boundaries !== "string" || args.boundaries.length > 32000 || !/^\d+(,\d+)+$/u.test(args.boundaries))
            return { ok: false, error: "usage-invalid-boundaries", statusCode: 400 };
          query.set("boundaries", args.boundaries);
        }
        response = await fetchImpl(new URL(`api/usage/${usageRoutes[operation]}?${query}`, clientUrl), { redirect: "error", signal: controller.signal });
      }
      const data = await response.json();
      if (closed || !trustedMainFrame(event)) return { ok: false, error: "usage-stale", statusCode: 409 };
      return { ...data, ok: response.ok, statusCode: response.status };
    } catch {
      return { ok: false, error: "usage-unavailable", statusCode: 503 };
    } finally { clearTimeout(timeout); }
  }
  registration.handle = async (event, operation, args = {}) => {
    if (closed || !trustedMainFrame(event) || !(allowed.has(operation) || Object.hasOwn(usageRoutes, operation) || operation === "usage-clear") ||
        !args || typeof args !== "object" || Array.isArray(args))
      return { ok: false, error: "trace-forbidden", statusCode: 403 };
    if (operation.startsWith("usage-")) return usage(event, operation, args);
    if (operation === "scope") {
      if (!["application", "account"].includes(args.scope)) return { ok: false, error: "trace-invalid-scope" };
      reset(); requestedScope = args.scope;
    }
    if (operation === "clear") {
      if (args.confirmAllAccounts !== true) return { ok: false, error: "trace-confirmation-required" };
      // Revoke local in-flight body deliveries before issuing the durable deletion.
      reset();
      invalidatePeers();
    }
    const ticket = epoch, controller = new AbortController(); requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 7000);
    try {
      const current = await getSession(controller.signal);
      if (operation === "scope") {
        deleteVersion = current.deleteVersion;
        return { ok: true, accountId: current.accountId, scope: current.scope, deleteVersion: current.deleteVersion };
      }
      let route = "", method = "GET", payload;
      if (operation === "state") route = "state";
      if (operation === "configure") {
        route = "state"; method = "POST"; payload = {};
        if (typeof args.enabled === "boolean") payload.enabled = args.enabled;
        if ([1, 7, 30].includes(args.retentionDays)) payload.retentionDays = args.retentionDays;
      }
      if (operation === "records") {
        const query = new URLSearchParams();
        for (const key of ["user", "source", "status", "model", "search", "from", "to", "includeActive", "offset", "taskId", "incomplete"])
          if (["string", "number", "boolean"].includes(typeof args[key]) && String(args[key]).length <= 1024) query.set(key, String(args[key]));
        query.set("limit", "50"); route = `records?${query}`;
      }
      if (["detail", "body"].includes(operation)) {
        if (typeof args.id !== "string" || !/^[A-Za-z0-9_-]{1,160}$/.test(args.id)) return { ok: false, error: "trace-invalid-id" };
        route = `records/${encodeURIComponent(args.id)}`;
        if (operation === "body") {
          if (!["request", "response", "events", "result", "text"].includes(args.kind)) return { ok: false, error: "trace-invalid-kind" };
          route += `/body?${new URLSearchParams({ kind: args.kind, offset: String(Math.max(0, Number(args.offset) || 0)), limit: "65536" })}`;
        }
      }
      if (operation === "events") route = `events?cursor=${encodeURIComponent(String(args.cursor || 0))}`;
      if (operation === "clear") {
        if (args.confirmAllAccounts !== true) return { ok: false, error: "trace-confirmation-required" };
        route = "clear"; method = "POST"; payload = { confirmAllAccounts: true };
      }
      const result = await transport(route, method, payload, { "X-Model-Trace-Session": current.token }, controller.signal);
      if (ticket !== epoch || !trustedMainFrame(event)) return { ok: false, error: "trace-stale", statusCode: 409 };
      if (Number.isSafeInteger(result.deleteVersion)) {
        if (deleteVersion !== null && result.deleteVersion < deleteVersion) return { ok: false, error: "trace-stale", statusCode: 409 };
        if (deleteVersion !== null && result.deleteVersion > deleteVersion) invalidatePeers(result.deleteVersion);
        deleteVersion = result.deleteVersion;
      }
      if (result.statusCode === 409 || result.statusCode === 401 || result.statusCode === 403) reset();
      return result;
    } catch {
      return { ok: false, error: ticket !== epoch ? "trace-stale" : "trace-unavailable", statusCode: ticket !== epoch ? 409 : 503 };
    } finally { clearTimeout(timeout); requests.delete(controller); }
  };
  webContents.on("did-start-navigation", reset);
  return { invalidate: registration.invalidate, close() { if (closed) return; closed = true; reset(); registry.delete(registration);
    if (!registry.size) { ipcMain.removeHandler("model-traces:request"); viewers.delete(ipcMain); }
    webContents.off("did-start-navigation", reset); } };
}

module.exports = { attachModelTraces, readControlToken };
