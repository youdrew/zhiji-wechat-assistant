"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Opt-in manual smoke for the Agent-openable window host (scripts/ui-host.cjs); not part of the
// automated tests. Runs under plain Node and spawns Electron with the host.
//
//   node scripts/ui-host-smoke.cjs
//       Synthetic: serves a tiny /friend.html on 127.0.0.1, opens it through the host and checks the
//       protocol (ready → opened → focused → closed → exit on stdin EOF) and that only requests to
//       that origin carry X-KYF-UI-Session.
//   node scripts/ui-host-smoke.cjs --url http://127.0.0.1:<port>/friend.html --account <a> --user <u>
//       [--name <n>] [--token <t>] [--tab persona]
//       Real Core: opens the restricted 知己分析 window and keeps it until Ctrl+C. The token must be
//       one Core accepts for a UI session, or the page masks itself.
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

const args = process.argv.slice(2);
const option = name => { const index = args.indexOf("--" + name); return index >= 0 ? args[index + 1] : undefined; };
const electron = require("electron");
const hostScript = path.join(__dirname, "ui-host.cjs");
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "kyf-ui-host-smoke-"));
const key = crypto.randomBytes(8).toString("hex");
const token = option("token") || crypto.randomBytes(24).toString("hex");

function startHost(onEvent) {
  const child = spawn(electron, [hostScript, "--user-data", userData], { stdio: ["pipe", "pipe", "inherit"] });
  readline.createInterface({ input: child.stdout }).on("line", line => {
    console.log("host →", line);
    try { onEvent(JSON.parse(line)); } catch { /* 非协议行 */ }
  });
  const send = value => { console.log("host ←", JSON.stringify(value)); child.stdin.write(JSON.stringify(value) + "\n"); };
  return { child, send };
}

async function real() {
  const { child, send } = startHost(event => {
    if (event.event === "ready") send({ op: "open", key, reqId: "smoke-1", surface: "friend", url: option("url"),
      hash: { account: option("account"), user: option("user"), ...(option("name") ? { name: option("name") } : {}) },
      token, ...(option("tab") ? { tab: option("tab") } : {}) });
    if (event.event === "closed") child.stdin.end();
  });
  process.on("SIGINT", () => child.stdin.end());
  child.on("exit", code => { console.log("host exited", code); process.exit(code || 0); });
}

async function synthetic() {
  const seen = [];
  const server = http.createServer((request, response) => {
    seen.push({ url: request.url, header: request.headers["x-kyf-ui-session"] || null });
    if (request.url === "/friend.html") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(`<!doctype html><meta charset="utf-8"><title>smoke</title><p>ui-host smoke</p>
<script>fetch("/api/probe").catch(() => {}); fetch("https://example.com/").catch(() => {});</script>`);
    } else { response.writeHead(200, { "Content-Type": "application/json" }); response.end("{}"); }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}/friend.html`;
  const events = [];
  let step = 0;
  const timeout = setTimeout(() => { console.error("FAIL: timed out", events); process.exit(1); }, 30000);
  const { child, send } = startHost(event => {
    events.push(event);
    const open = reqId => send({ op: "open", key, reqId, surface: "friend", url,
      hash: { account: "synthetic-account", user: "wxid_friend_a", name: "Smoke" }, token, theme: "light" });
    if (event.event === "ready" && step === 0) { step = 1; open("smoke-1"); }
    else if (event.event === "opened" && step === 1) {
      step = 2;
      setTimeout(() => { open("smoke-2"); send({ op: "select-tab", key, tab: "trend" }); send({ op: "bogus" }); }, 1500);
    } else if (event.event === "focused" && step === 2) { step = 3; setTimeout(() => send({ op: "close", key }), 300); }
    else if (event.event === "closed" && step === 3) { step = 4; child.stdin.end(); }
  });
  child.on("exit", code => {
    clearTimeout(timeout);
    server.close();
    const page = seen.find(item => item.url === "/friend.html"), probe = seen.find(item => item.url === "/api/probe");
    const checks = {
      protocol: JSON.stringify(events.map(event => event.event)) ===
        JSON.stringify(["ready", "opened", "focused", "error", "closed"]),
      pageHeader: page?.header === token, probeHeader: probe?.header === token, exitCode: code === 0,
    };
    console.log(JSON.stringify({ checks, seen: seen.map(item => ({ url: item.url, header: !!item.header })) }));
    try { fs.rmSync(userData, { recursive: true, force: true }); } catch { }
    process.exit(Object.values(checks).every(Boolean) ? 0 : 1);
  });
}

void (option("url") ? real() : synthetic());
