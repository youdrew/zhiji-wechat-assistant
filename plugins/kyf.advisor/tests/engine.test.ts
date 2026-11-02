// Modified by Know Your Friends, 2026-10-10 (Apache-2.0): native session lifecycle and compaction coverage.
// Moved from tests/advisor-runtime.test.ts into the kyf.advisor plugin, 2026-10-13 (Apache-2.0): calls carry a gateway
// service and an execution token; opencode holds only the relay address and secret, the gateway sees the token.
// Modified by Know Your Friends, 2026-10-15 (Apache-2.0): web tools of an external search source.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createServer, type IncomingHttpHeaders, type ServerResponse } from "node:http";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import * as readline from "node:readline";
import { fileURLToPath } from "node:url";
import { test, type TestContext } from "node:test";
import { createOpencodeClient } from "@opencode-ai/sdk/v2/client";
import { DEFAULT_MODEL_TIMEOUT_MS, MAX_MODEL_TIMEOUT_MS } from "../../../electron/model-timeout";
import { AdvisorRuntime, AdvisorRuntimeError, ADVISOR_ENGINE_VERSION, ADVISOR_TURN_TIMEOUT_MS, advisorEngineConfig, advisorEnvironment,
  advisorSafeError, advisorOutputBudget, advisorInputBudget, advisorManagedContextCost, advisorSkillText,
  advisorUsage, NO_THINKING_VARIANT, resolveAdvisorEngine, type AdvisorEngineSpec, type AdvisorRuntimeEvent,
  type AdvisorRuntimeRequest, type EngineService } from "../engine/runtime";

const service: EngineService = { id: "svc-1", protocol: "chat_completions", model: "synthetic-chat",
  url: "http://127.0.0.1:12345/s/svc-1", contextTokens: 8192, stamp: "synthetic-stamp" };
const token = "synthetic-execution-token";
const relay = { base: "http://127.0.0.1:23456/s/svc-1", secret: "synthetic-relay-secret" };
const request: AdvisorRuntimeRequest = { account: "synthetic-account", user: "synthetic-person", agentId: "advisor",
  threadId: "synthetic-thread", system: "Be concise", message: "Draft a polite answer", context: "m1 SELF: Hello\nm2 OTHER: Hi",
  contextRevision: 1, skills: [{ id: "empathy", content: "Listen before proposing a reply." }] };

async function fixture(t: TestContext) {
  const parent = path.resolve(".local", "advisor-build", "runtime-tests");
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(path.join(parent, "case-"));
  const stateRoot = path.join(root, "plugin-data", "accounts", "synthetic-account-key");
  const engineDir = path.join(root, ".local", "advisor-engine");
  await mkdir(engineDir, { recursive: true });
  const executable = Buffer.from("Synthetic fixture, never executed.");
  await writeFile(path.join(engineDir, "opencode.exe"), executable);
  await writeFile(path.join(engineDir, "manifest.json"), JSON.stringify({ schema: 1,
    version: ADVISOR_ENGINE_VERSION, file: "opencode.exe", bytes: executable.length,
    sha256: createHash("sha256").update(executable).digest("hex") }));
  const peers = new Set<ServerResponse>();
  const sessions = new Map<string, any>();
  const calls: Array<{ method: string; pathname: string; body: any }> = [];
  const specs: AdvisorEngineSpec[] = [];
  let counter = 0;
  let held = false;
  let compactHeld = false;
  let engineClosed = 0;
  let error: unknown;
  let tool = false;
  let omitRead = false;
  let wrongRead = false;
  let readTarget: string | undefined;
  let truncatedRead = false;
  let pageRead = false;
  let gapRead = false;
  let failedRead = false;
  let loadedInstructions = false;
  let hideReadEvents = false;
  let separateReadStep = false;
  let preface = false;
  let reorderPreface = false;
  let noTextTime = false;
  let noPrefaceTime = false;
  let equalNativeTime = false;
  let streamed = "Hello";
  let finish = "stop";
  let providerCall = false;
  let webPart: { tool: string; input: Record<string, unknown> } | undefined;
  const pending = new Map<string, ServerResponse>();
  const emit = (event: unknown) => { for (const peer of peers) peer.write(`data: ${JSON.stringify(event)}\n\n`); };
  // The Core gateway as the relay reaches it; it only records what it was sent.
  const gatewaySeen: Array<{ url: string; headers: IncomingHttpHeaders; body: string }> = [];
  const gateway = createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    gatewaySeen.push({ url: req.url!, headers: req.headers, body });
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}');
  });
  await new Promise<void>((resolve) => gateway.listen(0, "127.0.0.1", resolve));
  const gatewayService: EngineService = { ...service, url: `http://127.0.0.1:${(gateway.address() as { port: number }).port}/s/svc-1` };
  // What opencode does with its provider configuration: the relay address and secret, plus its session header.
  const callProvider = async (sessionID: string) => {
    const options = Object.values(specs.at(-1)!.config.provider!)[0]!.options!;
    const reply = await fetch(`${options.baseURL}/chat/completions`, { method: "POST", headers: { "content-type": "application/json",
      authorization: `Bearer ${options.apiKey}`, "x-opencode-session-id": sessionID }, body: JSON.stringify({ model: service.model, messages: [] }) });
    return reply.status;
  };
  const providerStatuses: number[] = [];
  const server = createServer(async (req, res) => {
    const pathname = new URL(req.url!, "http://localhost").pathname;
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : null;
    calls.push({ method: req.method!, pathname, body });
    if (pathname === "/event") {
      res.writeHead(200, { "content-type": "text/event-stream" });
      peers.add(res);
      res.write('data: {"type":"server.connected","properties":{}}\n\n');
      res.once("close", () => peers.delete(res));
      return;
    }
    const json = (value: unknown, status = 200) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(value)); };
    if (pathname === "/path") {
      const directory = new URL(req.url!, "http://localhost").searchParams.get("directory");
      json({ directory, worktree: directory }); return;
    }
    if (pathname === "/session/status") {
      for (const [id, session] of sessions) if (session.completeOnNextStatus) {
        delete session.completeOnNextStatus;
        const marker = session.history.findLast((entry: any) => entry.parts.some((part: any) => part.type === "compaction"));
        session.history.push({ info: { id: "msg_race_summary", role: "assistant", parentID: marker.info.id,
          summary: true, time: { created: Date.now(), completed: Date.now() } },
        parts: [{ id: "prt_race_summary", messageID: "msg_race_summary", sessionID: id, type: "text", text: "Committed before abort." }] });
      }
      json(Object.fromEntries([...sessions].filter(([, session]) => session.nativeStatus).map(([id, session]) => [id, session.nativeStatus])));
      return;
    }
    const compactMatch = /^\/session\/([^/]+)\/summarize$/u.exec(pathname);
    if (compactMatch && req.method === "POST") {
      const id = compactMatch[1], session = sessions.get(id), now = Date.now();
      if (providerCall) providerStatuses.push(await callProvider(id));
      session.history.push({ info: { role: "user", id: "msg_compact", time: { created: now } },
        parts: [{ id: "prt_compact", sessionID: id, messageID: "msg_compact", type: "compaction", auto: body.auto }] });
      if (compactHeld) { session.nativeStatus = { type: "busy" }; pending.set(id, res); return; }
      session.history.push({ info: { role: "assistant", id: "msg_summary", parentID: "msg_compact", summary: true, time: { created: now, completed: now + 1 } },
        parts: [{ id: "prt_summary", sessionID: id, messageID: "msg_summary", type: "text", text: "Confirmed goal; unfinished question; uncertain evidence." }] });
      json(true); return;
    }
    const partMatch = /^\/session\/([^/]+)\/message\/([^/]+)\/part\/([^/]+)$/u.exec(pathname);
    if (partMatch && req.method === "PATCH") {
      const session = sessions.get(partMatch[1]);
      for (const entry of session?.history ?? []) for (const part of entry.parts) if (part.id === partMatch[3]) Object.assign(part, body);
      json(body); return;
    }
    if (pathname === "/session" && req.method === "POST") {
      const id = `ses_synthetic${++counter}`;
      const session = { id, directory: new URL(req.url!, "http://localhost").searchParams.get("directory"),
        metadata: body.metadata, permission: body.permission };
      sessions.set(id, session);
      json(session);
      return;
    }
    const updateMatch = /^\/session\/([^/]+)$/u.exec(pathname);
    if (updateMatch && req.method === "PATCH") {
      Object.assign(sessions.get(updateMatch[1]), body); json(sessions.get(updateMatch[1])); return;
    }
    const messageMatch = /^\/session\/([^/]+)\/message\/([^/]+)$/u.exec(pathname);
    if (messageMatch && req.method === "GET") {
      const session = sessions.get(messageMatch[1]);
      if (session?.userMessages?.has(messageMatch[2])) json({ info: { id: messageMatch[2], role: "user" }, parts: [] });
      else json({ name: "NotFoundError" }, 404);
      return;
    }
    const match = /^\/session\/([^/]+)(?:\/(message|abort|revert))?$/u.exec(pathname);
    if (match) {
      const id = match[1];
      if (!match[2] && req.method === "GET") { json(sessions.get(id) ?? { name: "NotFoundError" }, sessions.has(id) ? 200 : 404); return; }
      if (!match[2] && req.method === "DELETE") { sessions.delete(id); json(true); return; }
      if (match[2] === "abort") {
        const session = sessions.get(id);
        if (session) {
          delete session.nativeStatus;
          if (session.completeAfterAbortStatus) session.completeOnNextStatus = true;
          if (session.completeOnAbort) {
            const marker = session.history.findLast((entry: any) => entry.parts.some((part: any) => part.type === "compaction"));
            session.history.push({ info: { id: "msg_race_summary", role: "assistant", parentID: marker.info.id,
              summary: true, time: { created: Date.now(), completed: Date.now() } },
            parts: [{ id: "prt_race_summary", messageID: "msg_race_summary", sessionID: id, type: "text", text: "Committed before abort." }] });
          }
        }
        const prompt = pending.get(id);
        if (prompt && !prompt.writableEnded) prompt.end();
        pending.delete(id);
        json(true);
        return;
      }
      if (match[2] === "revert") { sessions.get(id).revert = { messageID: body.messageID }; json(sessions.get(id)); return; }
      if (match[2] === "message" && req.method === "GET") { json(sessions.get(id)?.history ?? []); return; }
      if (match[2] === "message") {
        const session = sessions.get(id);
        session.userMessages ??= new Set();
        session.userMessages.add(body.messageID);
        if (providerCall) providerStatuses.push(await callProvider(id));
        const nativeEvent = (type: string, props: Record<string, unknown>) => emit({ type, properties: { sessionID: id, ...props } });
        const timeline = Date.now();
        nativeEvent("session.status", { status: { type: "busy" } });
        nativeEvent("message.updated", { info: { id: `msg_${id}`, role: "assistant", sessionID: id, parentID: body.messageID,
          time: { created: timeline } } });
        const readMessageID = separateReadStep ? `msg_step_${id}` : `msg_${id}`;
        if (separateReadStep) nativeEvent("message.updated", { info: { id: readMessageID, role: "assistant", sessionID: id,
          parentID: body.messageID, time: { created: timeline } } });
        const prefacePart = { id: equalNativeTime ? "prt_00000000000100000000000001" : `preface_${id}`,
          sessionID: id, messageID: readMessageID, type: "text",
          text: "我先看看这轮聊天资料。", ...(!noPrefaceTime ? { time: { start: equalNativeTime ? timeline + 3 : timeline, end: timeline + 3 } } : {}) };
        if (preface && !reorderPreface) nativeEvent("message.part.updated", { part: prefacePart });
        const reading = session.permission?.find((rule: any) => rule.permission === "read" && path.isAbsolute(rule.pattern));
        const toolParts: any[] = [];
        if (reading && !omitRead) {
          const target = readTarget ?? reading.pattern;
          const source = await readFile(target, "utf8");
          const lines = source.split("\n");
          const actual = gapRead ? lines.slice(0, 2) : truncatedRead || pageRead ? lines.slice(0, Math.max(1, lines.length - 1)) : lines;
          const readPart = { id: equalNativeTime ? "prt_00000000000200000000000002" : `read_${id}`,
            sessionID: id, messageID: readMessageID, type: "tool", tool: "read", callID: "call_synthetic",
            state: { status: "completed", input: { filePath: wrongRead ? path.join(path.dirname(reading.pattern), "unapproved.txt") : target },
              time: { start: timeline + 2, end: timeline + 3 }, output: source, metadata: { truncated: truncatedRead, loaded: loadedInstructions ? ["unapproved-instructions.md"] : [], display: { type: "file", path: target,
                lineStart: 1, lineEnd: actual.length, totalLines: lines.length, text: actual.join("\n"), truncated: truncatedRead } } } };
          if (failedRead) Object.assign(readPart.state, { status: "error", error: "Synthetic read failure", metadata: {} });
          toolParts.push(readPart);
          if (!hideReadEvents) nativeEvent("message.part.updated", { part: readPart });
          if (pageRead || gapRead) {
            const offset = actual.length + 1 + (gapRead ? 2 : 0);
            const remainder = { ...readPart, id: readPart.id + "_page2", state: { ...readPart.state,
              input: { ...readPart.state.input, offset },
              metadata: { truncated: false, loaded: [], display: { type: "file", path: reading.pattern, lineStart: offset,
                lineEnd: lines.length, totalLines: lines.length, text: lines.slice(offset - 1).join("\n"), truncated: false } } } };
            toolParts.push(remainder); nativeEvent("message.part.updated", { part: remainder });
          }
        }
        if (webPart) {
          const part = { id: `web_${id}`, sessionID: id, messageID: readMessageID, type: "tool", tool: webPart.tool, callID: "call_web",
            state: { status: "running", input: webPart.input, time: { start: timeline + 2 } } };
          nativeEvent("message.part.updated", { part });
          const done = { ...part, state: { ...part.state, status: "completed", output: "1. 合成结果", time: { start: timeline + 2, end: timeline + 3 } } };
          nativeEvent("message.part.updated", { part: done });
          toolParts.push(done);
        }
        if (preface && reorderPreface) nativeEvent("message.part.updated", { part: prefacePart });
        const formalPart = { id: `part_${id}`, sessionID: id, messageID: `msg_${id}`, type: "text", text: streamed,
          ...(!noTextTime ? { time: { start: timeline + 4 } } : {}) };
        nativeEvent("message.part.updated", { part: { ...formalPart, text: "" } });
        nativeEvent("message.part.delta", { messageID: `msg_${id}`, partID: `part_${id}`, field: "text", delta: streamed });
        session.history = [{ info: { role: "assistant", parentID: body.messageID },
          parts: [...(preface ? [prefacePart] : []), ...toolParts] },
          { info: { role: "assistant", parentID: body.messageID }, parts: [formalPart] }];
        if (held) { pending.set(id, res); return; }
        json({ info: { id: `msg_${id}`, role: "assistant", finish, tokens: { input: 42, output: 2, reasoning: 0, cache: { read: 5, write: 3 } }, ...(error ? { error } : {}) },
          parts: [...(preface && !separateReadStep ? [prefacePart] : []), ...(!hideReadEvents && !separateReadStep ? toolParts : []), formalPart,
            ...(tool ? [{ id: `tool_${id}`, messageID: `msg_${id}`, sessionID: id, type: "tool", tool: "bash",
              state: { status: "error", error: "Denied" } }] : [])] });
        return;
      }
    }
    if (/^\/permission\/.+\/reply$/u.test(pathname) || /^\/question\/.+\/reject$/u.test(pathname)) { json(true); return; }
    json({ error: "Fixture route absent" }, 404);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as { port: number };
  const client = createOpencodeClient({ baseUrl: `http://127.0.0.1:${address.port}`, throwOnError: true });
  const runtime = new AdvisorRuntime({ root, stateRoot, startEngine: async (spec) => {
    specs.push(spec);
    return { client, close: async () => { engineClosed++; for (const peer of peers) peer.end(); } };
  } });
  t.after(async () => {
    await runtime.close();
    for (const listener of [server, gateway]) {
      listener.closeAllConnections();
      await new Promise<void>((resolve) => listener.close(() => resolve()));
    }
    await rm(root, { recursive: true, force: true });
  });
  return { root, stateRoot, runtime, calls, sessions, specs, emit, pending, gatewayService, gatewaySeen, providerStatuses, callProvider,
    callProviders: () => { providerCall = true; },
    webPart: (value?: { tool: string; input: Record<string, unknown> }) => { webPart = value; },
    setHeld: (value: boolean) => { held = value; }, setError: (value: unknown) => { error = value; },
    setCompactHeld: (value: boolean) => { compactHeld = value; },
    setTool: (value: boolean) => { tool = value; },
    omitRead: () => { omitRead = true; }, wrongRead: () => { wrongRead = true; }, truncatedRead: () => { truncatedRead = true; },
    readPath: (value: string) => { readTarget = value; },
    pageRead: () => { pageRead = true; },
    gapRead: () => { gapRead = true; }, failedRead: () => { failedRead = true; },
    loadedInstructions: (historyOnly = false) => { loadedInstructions = true; hideReadEvents = historyOnly; },
    preface: (reordered = false) => { preface = true; reorderPreface = reordered; }, noTextTime: () => { noTextTime = true; },
    noPrefaceTime: () => { noPrefaceTime = true; },
    equalNativeTime: () => { equalNativeTime = true; },
    separateReadStep: () => { separateReadStep = true; },
    setText: (value: string) => { streamed = value; }, setFinish: (value: string) => { finish = value; }, getClosed: () => engineClosed };
}

test("engine pin verification checks binary hash and rejects tampering before launch", async (t) => {
  const f = await fixture(t);
  assert.equal(await resolveAdvisorEngine(f.root), path.join(f.root, ".local", "advisor-engine", "opencode.exe"));
  await writeFile(path.join(f.root, ".local", "advisor-engine", "opencode.exe"), "changed");
  await assert.rejects(f.runtime.respond(service, token, request), (error) => error instanceof AdvisorRuntimeError && error.code === "engine-integrity");
  assert.equal(f.specs.length, 0);
});

test("usage rejects missing/invalid counters and normalizes only last-step input plus cache", () => {
  for (const raw of [undefined, {}, { input: 3 }, { input: -1, cache: { read: 1, write: 2 } },
    { input: 2, cache: { read: -1, write: 2 } }, { input: 0, cache: { read: 0, write: 0 } }]) {
    assert.equal(advisorUsage(raw, "anthropic"), undefined);
  }
  const usage = advisorUsage({ input: 10, output: 999, reasoning: 500, cache: { read: 7, write: 3 } }, "anthropic");
  assert.equal(usage?.totalInputTokens, 20);
  assert.equal(usage?.estimated, false);
});

test("manual native compaction is scoped and replayable, and deletion is verified and idempotent", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const scoped = { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "compact-one", contextVersion: 7 };
  const before = f.calls.filter(call => call.method === "POST" && call.pathname.endsWith("/message")).length;
  const compact = await f.runtime.compactSession(service, token, scoped);
  assert.equal(compact.state, "completed");
  assert.equal(compact.summary, "Confirmed goal; unfinished question; uncertain evidence.");
  assert.equal(compact.compactions?.[0].contextVersion, 7);
  assert.equal(compact.compactions?.[0].boundaryVerified, false);
  assert.equal((await f.runtime.compactSession(service, token, scoped)).state, "completed");
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/summarize")).length, 1);
  assert.equal(f.calls.filter(call => call.method === "POST" && call.pathname.endsWith("/message")).length, before);
  await assert.rejects(f.runtime.deleteSession(service, { ...scoped, user: "another-contact" }), error =>
    error instanceof AdvisorRuntimeError && error.code === "scope-mismatch");
  assert.equal((await f.runtime.deleteSession(service, scoped)).state, "completed");
  assert.equal((await f.runtime.deleteSession(service, scoped)).state, "completed");
  assert.equal((await f.runtime.inspectSession(service, scoped)).state, "missing");
  await assert.rejects(f.runtime.respond(service, token, scoped), error => error instanceof AdvisorRuntimeError && error.code === "native-session-missing");
});

test("unknown compaction without a live worker can be stopped only by its owned operation", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const session = f.sessions.get(first.runtimeSessionId);
  session.history.push({ info: { id: "msg_unknown_compact", role: "user", time: { created: Date.now() } },
    parts: [{ id: "prt_unknown_compact", sessionID: first.runtimeSessionId, messageID: "msg_unknown_compact", type: "compaction", auto: false }] });
  session.metadata.advisorCompactions = [{ id: "unknown-operation", state: "unknown", markerID: "msg_unknown_compact" }];
  session.nativeStatus = { type: "busy" };
  const owned = { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "unknown-operation" };
  for (const patch of [{ operationId: "not-owned" }, { user: "other-contact" }]) {
    await assert.rejects(f.runtime.stopSessionOperation(service, { ...owned, ...patch }), error =>
      error instanceof AdvisorRuntimeError && ["invalid-request", "scope-mismatch"].includes(error.code));
  }
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/abort")).length, 0);
  assert.equal((await f.runtime.stopSessionOperation(service, owned)).state, "stopped");
  assert.equal(session.metadata.advisorCompactions[0].state, "stopped");
  assert.equal((await f.runtime.stopSessionOperation(service, owned)).state, "stopped");
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/abort")).length, 1);
});

test("completed compaction replay never aborts a newer native answer", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const owned = { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "completed-operation" };
  await f.runtime.compactSession(service, token, owned);
  const session = f.sessions.get(first.runtimeSessionId);
  session.history.push({ info: { id: "msg_new_question", role: "user", time: { created: Date.now() } },
    parts: [{ type: "text", text: "New question" }] });
  session.nativeStatus = { type: "busy" };
  assert.equal((await f.runtime.stopSessionOperation(service, owned)).state, "completed");
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/abort") || call.pathname.endsWith("/revert")).length, 0);
  assert.deepEqual(session.nativeStatus, { type: "busy" });
});

test("stop reconciles a summary committed during abort and never reverts it", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const session = f.sessions.get(first.runtimeSessionId);
  session.history.push({ info: { id: "msg_racing_compact", role: "user", time: { created: Date.now() } },
    parts: [{ id: "prt_racing_compact", sessionID: first.runtimeSessionId, messageID: "msg_racing_compact", type: "compaction", auto: false }] });
  session.metadata.advisorCompactions = [{ id: "racing-operation", state: "unknown", markerID: "msg_racing_compact" }];
  session.nativeStatus = { type: "busy" };
  session.completeAfterAbortStatus = true;
  const result = await f.runtime.stopSessionOperation(service, { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "racing-operation" });
  assert.equal(result.state, "completed");
  assert.equal(result.summary, "Committed before abort.");
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/revert")).length, 0);
});

test("unknown stale compaction does not abort a different newer native operation", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const session = f.sessions.get(first.runtimeSessionId);
  session.history.push({ info: { id: "msg_old_marker", role: "user", time: { created: Date.now() } },
    parts: [{ id: "prt_old_marker", sessionID: first.runtimeSessionId, messageID: "msg_old_marker", type: "compaction", auto: false }] },
    { info: { id: "msg_newer_question", role: "user", time: { created: Date.now() } }, parts: [{ type: "text", text: "Another question" }] });
  session.metadata.advisorCompactions = [{ id: "old-operation", state: "unknown", markerID: "msg_old_marker" }];
  session.nativeStatus = { type: "busy" };
  const result = await f.runtime.stopSessionOperation(service, { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "old-operation" });
  assert.equal(result.state, "unknown");
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/abort")).length, 0);
});

test("revision changes reuse a session but profile and epoch changes require controlled reconstruction", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, { ...request, contextEpoch: "epoch1", profileStamp: "p1" });
  await f.runtime.respond({ ...service, model: "another-model" }, token, { ...request, contextEpoch: "epoch1", profileStamp: "p1",
    contextRevision: 2, runtimeSessionId: first.runtimeSessionId });
  for (const patch of [{ contextEpoch: "epoch2", profileStamp: "p1" }, { contextEpoch: "epoch1", profileStamp: "p2" }]) {
    await assert.rejects(f.runtime.respond(service, token, { ...request, ...patch, runtimeSessionId: first.runtimeSessionId }), error =>
      error instanceof AdvisorRuntimeError && error.code === "native-session-incompatible");
  }
});

test("oversized current inputs fail before provider calls and never discard history", async (t) => {
  const f = await fixture(t);
  await assert.rejects(f.runtime.respond(service, token, { ...request, context: "材料".repeat(10000) }), error =>
    error instanceof AdvisorRuntimeError && error.code === "context-too-long");
  assert.equal(f.calls.filter(call => call.method === "POST" && call.pathname.endsWith("/message")).length, 0);
  assert.equal(f.sessions.size, 0, "failed newly-created native is cleaned even before a prompt");
});

test("formal byte accounting accepts fitting Chinese and English histories without the old quarter allocation", async (t) => {
  const f = await fixture(t);
  for (const [index, context] of ["Synthetic English. ".repeat(600), "合成中文".repeat(900)].entries()) {
    const result = await f.runtime.respond({ ...service, contextTokens: 32768 }, token,
      { ...request, threadId: `byte-fit-${index}`, context });
    assert.equal(result.text, "Hello");
  }
});

test("numbered native read lines are reserved even when raw text alone fits", async (t) => {
  const f = await fixture(t);
  const context = "a\n".repeat(250);
  assert(Buffer.byteLength(context) < advisorInputBudget(8192));
  await assert.rejects(f.runtime.respond(service, token, { ...request, context }), error =>
    error instanceof AdvisorRuntimeError && error.code === "context-too-long");
  assert.equal(f.calls.filter(call => call.method === "POST" && call.pathname.endsWith("/message")).length, 0);
});

test("smallest window accepts a tiny formal request and still rejects genuine overflow", async (t) => {
  const f = await fixture(t);
  const tiny = { ...request, system: "", message: "Hi", context: "x", skills: [] };
  assert(advisorInputBudget(4096) > advisorManagedContextCost(tiny.context));
  assert.equal((await f.runtime.respond({ ...service, contextTokens: 4096 }, token, tiny)).text, "Hello");
  await assert.rejects(f.runtime.respond({ ...service, contextTokens: 4096 }, token,
    { ...tiny, threadId: "too-large", context: "界".repeat(500) }), error =>
    error instanceof AdvisorRuntimeError && error.code === "context-too-long");
});

test("backend envelope allowance covers measured prompt and file wrappers including recovery and participants", async (t) => {
  const f = await fixture(t);
  // Compare the declared backend allowance against the actual native prompt and
  // materialized file, so prompt/header changes cannot silently exceed assembly.
  const backend = await readFile(new URL("../backend/service.py", import.meta.url), "utf8");
  const fixed = Number(/hidden = (\d+) \+ quoted\(material\)/u.exec(backend)?.[1]);
  const participantExtra = Number(/hidden \+= (\d+) \+ quoted\(index\)/u.exec(backend)?.[1]);
  const webExtra = Number(/if web_search\(run\.model_settings\["values"\]\):\s*hidden \+= (\d+)/u.exec(backend)?.[1]);
  assert(Number.isFinite(fixed) && Number.isFinite(participantExtra) && Number.isFinite(webExtra));
  const folder = path.join(f.stateRoot, "engine", "workspace", "participants", "long-synthetic-folder-".repeat(3));
  await mkdir(folder, { recursive: true });
  const index = path.join(folder, "index.txt");
  await writeFile(index, "Synthetic participant index");
  for (const [number, patch] of [
    {}, { recoveryText: "Prior discussion, not facts." },
    { recoveryText: "Prior discussion, not facts.", participantIndex: index, participantFiles: [index] },
    { context: "", recoveryText: "Prior discussion, not facts." },
    { recoveryText: "Prior discussion, not facts.", participantIndex: index, participantFiles: [index],
      webSearch: { provider: "searxng" as const, url: "http://127.0.0.1:8080", fetchPages: true } },
  ].entries()) {
    const candidate = { ...request, threadId: `envelope-${number}`, ...patch };
    const result = await f.runtime.respond({ ...service, contextTokens: 16384 }, token, candidate);
    const prompt = f.calls.findLast(call => call.pathname.endsWith("/message") && call.method === "POST")!.body;
    const file = f.sessions.get(result.runtimeSessionId).permission.find((rule: any) =>
      rule.permission === "read" && path.isAbsolute(rule.pattern) && rule.pattern.endsWith("wechat-context.txt")).pattern;
    const contents = await readFile(file, "utf8");
    const context = candidate.context || "No readable WeChat records in this snapshot.";
    const visible = candidate.system! + advisorSkillText(candidate.skills!) + (candidate.recoveryText ?? "");
    const measured = Buffer.byteLength(prompt.system) - Buffer.byteLength(visible) +
      Buffer.byteLength(prompt.parts[0].text) - Buffer.byteLength(candidate.message!) +
      advisorManagedContextCost(contents) - advisorManagedContextCost(candidate.context ?? "");
    const estimated = fixed + Buffer.byteLength(JSON.stringify(file)) + Buffer.byteLength(JSON.stringify(candidate.contextRevision)) +
      (candidate.participantIndex ? participantExtra + Buffer.byteLength(JSON.stringify(candidate.participantIndex)) : 0) +
      ("webSearch" in candidate ? webExtra : 0);
    assert(estimated >= measured, `backend allowance ${estimated} must cover actual envelope ${measured}`);
    assert(contents.includes(context));
  }
});

test("cancel or rejected scope callback immediately after native creation leaves no orphan", async (t) => {
  const f = await fixture(t);
  const controller = new AbortController();
  await assert.rejects(f.runtime.respond(service, token, request, event => {
    if (event.state === "native-session-created") controller.abort();
  }, controller.signal), error => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  assert.equal(f.sessions.size, 0);
  await assert.rejects(f.runtime.respond(service, token, request, event => {
    if (event.state === "native-session-created") throw new AdvisorRuntimeError("scope-mismatch");
  }), error => error instanceof AdvisorRuntimeError && error.code === "scope-mismatch");
  assert.equal(f.sessions.size, 0);
});

test("all five protocols use bundled provider adapters and immutable deny permissions", () => {
  for (const protocol of ["chat_completions", "responses", "anthropic", "gemini", "ollama"] as const) {
    const mapped = advisorEngineConfig({ ...service, protocol }, relay);
    assert.equal(mapped.config.permission, "deny");
    assert.deepEqual(mapped.config.agent?.advisor?.permission, { "*": "deny", read: { "*": "deny" } });
    assert.equal(mapped.config.agent?.compaction?.permission, "deny");
    assert.equal(mapped.config.agent?.compactor?.permission, "deny");
    assert.deepEqual(mapped.config.plugin, []);
    assert.deepEqual(mapped.config.mcp, {});
    assert.equal(mapped.config.agent?.build?.disable, true);
    assert.equal(mapped.config.agent?.title?.disable, true);
    assert.equal(mapped.config.compaction?.prune, true);
    const provider = mapped.config.provider![mapped.providerID];
    assert.equal(provider.models![service.model].tool_call, true);
    assert.equal(provider.options!.baseURL, protocol === "ollama" ? relay.base + "/v1" : relay.base);
    assert.equal(provider.options!.apiKey, relay.secret);
    assert.equal(provider.models![service.model].headers, undefined);
    if (protocol === "responses") { assert.equal(mapped.providerID, "openai"); assert.equal(provider.npm, "@ai-sdk/openai"); }
    const stable = advisorEngineConfig({ ...service, protocol }).config.provider![mapped.providerID].options!;
    assert.equal(stable.apiKey, undefined, "the fingerprinted part holds no secret");
    assert.equal(stable.baseURL, undefined, "and no relay port");
  }
  for (const bad of [{ url: "https://api.example.com/v1" }, { url: "http://127.0.0.1:1/s/other" }, { url: "http://127.0.0.1:1/s/svc-1~advisor" },
    { protocol: "unknown" }, { model: "" }]) {
    assert.throws(() => advisorEngineConfig({ ...service, ...bad } as EngineService, relay),
      (error: unknown) => error instanceof AdvisorRuntimeError && error.code === "invalid-request", JSON.stringify(bad));
  }
});

test("answers without thinking use a chat-template variant only where one exists", async (t) => {
  for (const protocol of ["chat_completions", "responses", "anthropic", "gemini", "ollama"] as const) {
    const mapped = advisorEngineConfig({ ...service, protocol }, relay);
    const variants = mapped.config.provider![mapped.providerID].models![service.model].variants;
    if (protocol === "chat_completions" || protocol === "ollama") {
      assert.equal(mapped.noThinkingVariant, NO_THINKING_VARIANT);
      assert.deepEqual(variants, { [NO_THINKING_VARIANT]: { chat_template_kwargs: { thinking: false, enable_thinking: false } } });
    } else {
      assert.equal(mapped.noThinkingVariant, undefined, protocol);
      assert.equal(variants, undefined, protocol);
    }
  }
  const f = await fixture(t);
  await f.runtime.respond(service, token, request);
  await f.runtime.respond(service, token, { ...request, threadId: "synthetic-quick", thinking: false });
  const prompts = f.calls.filter((call) => /^\/session\/[^/]+\/message$/u.test(call.pathname) && call.method === "POST");
  assert.deepEqual(prompts.map((call) => call.body.variant), [undefined, NO_THINKING_VARIANT]);
  await assert.rejects(f.runtime.respond(service, token, { ...request, thinking: "no" as unknown as boolean }),
    (error) => error instanceof AdvisorRuntimeError && error.code === "invalid-request");
});

test("a server that rejects the no-thinking field gets a specific error", async (t) => {
  const f = await fixture(t);
  f.setError({ name: "APIError", data: { statusCode: 400, message: "Unrecognized request argument: chat_template_kwargs" } });
  await assert.rejects(f.runtime.respond(service, token, { ...request, thinking: false }),
    (error) => error instanceof AdvisorRuntimeError && error.code === "thinking-unsupported");
  await assert.rejects(f.runtime.respond(service, token, { ...request, threadId: "synthetic-default" }),
    (error) => error instanceof AdvisorRuntimeError && error.code === "provider-error");
});

test("summary agents' only call is never OpenCode's last step", () => {
  // On step `steps` OpenCode appends an assistant "maximum steps reached" notice,
  // after which a model returns nothing or nonsense instead of the summary.
  const agents = advisorEngineConfig(service).config.agent!;
  for (const name of ["compactor", "discussion_compactor", "compaction"] as const) {
    assert(agents[name]!.steps! > 1, name);
    assert.deepEqual(agents[name]!.tools, agents.compactor!.tools, name);
  }
  assert(Object.values(agents.compactor!.tools!).every(enabled => enabled === false));
});

test("all summary paths disable thinking while formal answers retain the user's selection", async (t) => {
  const agents = advisorEngineConfig(service).config.agent!;
  for (const name of ["compactor", "discussion_compactor", "compaction"] as const) {
    assert.deepEqual(agents[name]!.options, { chat_template_kwargs: { thinking: false, enable_thinking: false } }, name);
  }
  assert.equal(agents.advisor.options, undefined);
  const f = await fixture(t);
  await f.runtime.compact(service, token, { ...request, text: "Synthetic WeChat evidence", thinking: true });
  await f.runtime.summarizeDiscussion(service, token, { ...request, text: "Synthetic adviser discussion", targetSummaryChars: 500, thinking: true });
  await f.runtime.respond(service, token, { ...request, thinking: true });
  await f.runtime.respond(service, token, { ...request, thinking: false });
  const prompts = f.calls.filter(call => /^\/session\/[^/]+\/message$/u.test(call.pathname) && call.method === "POST");
  assert.deepEqual(prompts.map(call => call.body.variant), [NO_THINKING_VARIANT, NO_THINKING_VARIANT, undefined, NO_THINKING_VARIANT]);
});

test("summary, discussion summary, native compaction and answers use the configured deadline", async (t) => {
  const f = await fixture(t);
  const timeouts: number[] = [];
  const timeout = AbortSignal.timeout.bind(AbortSignal);
  t.mock.method(AbortSignal, "timeout", (duration: number) => { timeouts.push(duration); return timeout(duration); });
  const first = await f.runtime.respond({ ...service, timeoutMs: 120_000 }, token, request);
  await f.runtime.compactSession({ ...service, timeoutMs: 180_000 }, token,
    { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "synthetic-manual" });
  await f.runtime.summarizeDiscussion({ ...service, timeoutMs: 240_000 }, token,
    { ...request, text: "Synthetic adviser history", targetSummaryChars: 500 });
  await f.runtime.compact({ ...service, timeoutMs: 300_000 }, token, { ...request, text: "Synthetic WeChat history" });
  await f.runtime.compact(service, token, { ...request, text: "Synthetic default setting" });
  assert.deepEqual(timeouts.filter(duration => duration !== 5_000), [120_000, 180_000, 240_000, 300_000, DEFAULT_MODEL_TIMEOUT_MS]);
  assert.equal(f.specs.length, 1, "timeout changes do not restart the engine or replace native history");
});

test("a timeout edit leaves an active discussion running and its original deadline aborts the native task", async (t) => {
  const f = await fixture(t);
  const deadline = new AbortController();
  const timeout = AbortSignal.timeout.bind(AbortSignal);
  t.mock.method(AbortSignal, "timeout", (duration: number) => duration === 60_000 ? deadline.signal : timeout(duration));
  f.setHeld(true);
  const running = f.runtime.respond({ ...service, timeoutMs: 60_000 }, token, request);
  const rejected = assert.rejects(running, (error) => error instanceof AdvisorRuntimeError && error.code === "timeout");
  for (let tries = 0; !f.pending.size && tries < 100; tries++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(f.pending.size, 1);
  f.setHeld(false);
  await f.runtime.respond({ ...service, timeoutMs: 120_000 }, token, { ...request, threadId: "synthetic-second" });
  assert.equal(f.getClosed(), 0);
  assert.equal(f.pending.size, 1, "the prior task remains active under its original timeout");
  deadline.abort(new DOMException("Synthetic deadline", "TimeoutError"));
  await rejected;
  assert(f.calls.some(call => call.pathname.endsWith("/abort")), "timeout cancels native generation");
});

test("native compaction also cancels the engine when its own deadline expires", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const deadline = new AbortController();
  const timeout = AbortSignal.timeout.bind(AbortSignal);
  t.mock.method(AbortSignal, "timeout", (duration: number) => duration === 60_000 ? deadline.signal : timeout(duration));
  f.setCompactHeld(true);
  const compact = f.runtime.compactSession({ ...service, timeoutMs: 60_000 }, token,
    { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "synthetic-timeout" });
  for (let tries = 0; !f.pending.size && tries < 100; tries++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(f.pending.size, 1);
  deadline.abort(new DOMException("Synthetic deadline", "TimeoutError"));
  assert.equal((await compact).state, "failed");
  assert(f.calls.some(call => call.pathname.endsWith("/abort")));
  assert.equal(f.sessions.get(first.runtimeSessionId)?.revert.messageID, "msg_compact", "only the unfinished compaction is reverted");
});

test("output and compaction budgets leave room in the smallest allowed window", () => {
  assert.equal(advisorOutputBudget(4096), 1024);
  assert.equal(advisorOutputBudget(8192), 2048);
  const mapped = advisorEngineConfig({ ...service, contextTokens: 4096 });
  assert.equal(mapped.config.provider![mapped.providerID].models![service.model].limit?.output, 1024);
  assert.equal(mapped.config.compaction?.reserved, 1280);
});

test("reasoning gets room on top of the answer's share, within OpenCode's 32 000 cap", () => {
  assert.equal(advisorOutputBudget(32768), 8192);
  assert.equal(advisorOutputBudget(131072), 28672);
  assert.equal(advisorOutputBudget(1_000_000), 28672);
  assert.ok(advisorOutputBudget(1_000_000) - 4096 >= 24576);
  const mapped = advisorEngineConfig({ ...service, contextTokens: 1_000_000 });
  const provider = mapped.config.provider![mapped.providerID];
  assert.equal(provider.models![service.model].limit?.output, 28672);
  // The stable transport ceiling must not preempt any user-selectable deadline.
  assert.equal(provider.options?.timeout, MAX_MODEL_TIMEOUT_MS);
  assert.ok(ADVISOR_TURN_TIMEOUT_MS * 16 / 1000 >= 28672);
  assert.equal(provider.options?.headerTimeout, MAX_MODEL_TIMEOUT_MS);
  assert.equal(provider.options?.chunkTimeout, MAX_MODEL_TIMEOUT_MS);
});

test("engine environment drops parent auth, hooks, proxy and external config", () => {
  const env = advisorEnvironment("C:\\owned", "C:\\runtime\\opencode.exe", advisorEngineConfig(service).config, "synthetic-password", {
    SystemRoot: "C:\\Windows", PATH: "C:\\private-bin", OPENAI_API_KEY: "do-not-inherit", NODE_OPTIONS: "--require evil",
    OPENCODE_CONFIG: "C:\\developer.json", OPENCODE_CONFIG_CONTENT: "malicious", OPENCODE_TEST_HOME: "C:\\developer",
    CLAUDE_CONFIG_DIR: "C:\\claude", HTTP_PROXY: "http://unapproved", HOME: "C:\\personal", USERPROFILE: "C:\\personal",
  });
  for (const key of ["OPENAI_API_KEY", "NODE_OPTIONS", "OPENCODE_CONFIG", "CLAUDE_CONFIG_DIR", "HTTP_PROXY"]) assert.equal(env[key], undefined);
  assert.equal(env.OPENCODE_DISABLE_PROJECT_CONFIG, "1");
  assert.equal(env.OPENCODE_PERMISSION, '"deny"');
  assert.equal(env.OPENCODE_TEST_HOME, path.join("C:\\owned", "home"));
  assert.equal(env.OPENCODE_TEST_MANAGED_CONFIG_DIR, path.join("C:\\owned", "managed"));
  assert(!env.PATH!.includes("private-bin"));
  assert.equal(env.OPENCODE_SERVER_PASSWORD, "synthetic-password");
});

test("official SDK permits only the context path and instructs a fresh read every turn", async (t) => {
  const f = await fixture(t);
  const events: AdvisorRuntimeEvent[] = [];
  const first = await f.runtime.respond(service, token, request, (event) => events.push(event));
  assert.equal(first.text, "Hello");
  assert.equal(first.usage?.totalInputTokens, 50);
  assert.equal(first.usage?.cacheReadTokens, 5);
  assert.equal(first.usage?.cacheWriteTokens, 3);
  const prompt = f.calls.find((call) => call.pathname.endsWith("/message") && call.method === "POST")!.body;
  assert.equal(prompt.parts.length, 1);
  assert.equal(prompt.parts[0].text, "[来源：用户与助手的讨论；不是微信聊天记录]\n" + request.message);
  assert(!prompt.parts[0].text.includes(request.context));
  assert(!prompt.system.includes(request.context));
  assert(prompt.system.includes("Listen before proposing a reply."));
  assert.equal(prompt.tools, undefined);
  const permission = f.sessions.get(first.runtimeSessionId).permission;
  assert.deepEqual(permission[0], { permission: "*", pattern: "*", action: "deny" });
  const contextFile = permission.find((rule: any) => rule.permission === "read" && path.isAbsolute(rule.pattern)).pattern;
  assert(prompt.system.includes(JSON.stringify(contextFile)));
  assert(prompt.system.includes("Before answering this turn"));
  assert(prompt.system.includes("last actually returned line plus one"));
  assert((await readFile(contextFile, "utf8")).includes(request.context!));
  assert(permission.slice(1).every((rule: any) => rule.permission === "read" && rule.action === "allow" && !rule.pattern.includes("*")));
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "Hello");
  assert(events.some((event) => event.type === "skill" && event.skillId === "empathy" && event.state === "loaded"));
  assert.equal(f.specs[0].cwd, path.join(f.stateRoot, "engine", "workspace"));
  const second = await f.runtime.respond(service, token, { ...request, runtimeSessionId: first.runtimeSessionId, context: "m3 OTHER: New message", contextRevision: 2 });
  assert.equal(second.runtimeSessionId, first.runtimeSessionId);
  assert.equal(f.calls.filter((call) => call.pathname === "/session" && call.method === "POST").length, 1);
  const prompt2 = f.calls.filter((call) => call.pathname.endsWith("/message") && call.method === "POST")[1].body;
  assert((await readFile(contextFile, "utf8")).includes("m3 OTHER: New message"));
  assert(!prompt2.system.includes(request.context));
  assert(prompt2.system.includes("Do not reuse an earlier turn's read"));
  assert.equal(f.calls.filter((call) => call.pathname.endsWith("/revert")).length, 0);
});

test("user comments stay labelled as adviser discussion on first, continued and restored turns", async (t) => {
  const f = await fixture(t);
  const prefix = "[来源：用户与助手的讨论；不是微信聊天记录]\n";
  const context = "[2026-01-03 10:00] 合成好友: 周六图书馆门口见。";
  const firstInput = "这个安排真周到。\n我只是和你讨论一下。";
  const initial = { ...request, agentId: "synthetic-custom-adviser", context, message: firstInput };
  const first = await f.runtime.respond(service, token, initial);
  const continued = { ...initial, runtimeSessionId: first.runtimeSessionId, message: "可以怎样回应？先给我草稿。" };
  const second = await f.runtime.respond(service, token, continued);
  assert.equal(second.runtimeSessionId, first.runtimeSessionId);
  const restored = { ...initial, threadId: "synthetic-restored-discussion", message: "我觉得这样的安排很贴心。",
    recoveryText: "[历史用户问题] 用户觉得安排周到；这是用户的评价，并非好友原话。" };
  await f.runtime.respond(service, token, restored);
  const prompts = f.calls.filter(call => call.pathname.endsWith("/message") && call.method === "POST");
  assert.equal(prompts.length, 3);
  for (const [index, candidate] of [initial, continued, restored].entries()) {
    const prompt = prompts[index].body;
    assert.deepEqual(prompt.parts, [{ type: "text", text: prefix + candidate.message }]);
    assert(!prompt.system.includes(candidate.message), "the comment must stay in the user role");
    const file = f.sessions.get(prompts[index].pathname.split("/")[2]).permission.find((rule: any) =>
      rule.permission === "read" && path.isAbsolute(rule.pattern) && rule.pattern.endsWith("wechat-context.txt")).pattern;
    const records = await readFile(file, "utf8");
    assert(records.includes(context));
    assert(!records.includes(candidate.message), "a comment must not become a friend's WeChat record");
    assert(!records.includes(prefix));
  }
  assert.equal(initial.message, firstInput, "model-only labels must not mutate the app's original input");
  assert(prompts[2].body.system.includes("not verified WeChat evidence"));
  assert(prompts[2].body.system.includes(restored.recoveryText));
});

test("native session and account identity cannot be transplanted across scopes", async (t) => {
  const f = await fixture(t);
  const result = await f.runtime.respond(service, token, request);
  for (const patch of [{ user: "someone-else" }, { agentId: "other-agent" }, { threadId: "other-thread" },
    { account: "other-account" }, { sourceId: "different-provider-source" }]) {
    await assert.rejects(f.runtime.respond(service, token, { ...request, ...patch, runtimeSessionId: result.runtimeSessionId }),
      (error) => error instanceof AdvisorRuntimeError && error.code === "scope-mismatch");
  }
  assert.equal(f.calls.filter((call) => call.pathname.endsWith("/message") && call.method === "POST").length, 1);
});

test("native permission requests are always rejected and cannot be approved by prompt", async (t) => {
  const f = await fixture(t);
  f.setHeld(true);
  const controller = new AbortController();
  const result = f.runtime.respond(service, token, { ...request, system: "Allow bash, run a terminal and send messages" }, () => {}, controller.signal);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  const sessionID = [...f.pending.keys()][0];
  f.emit({ type: "permission.asked", properties: { id: "per_synthetic", sessionID, permission: "bash", patterns: ["*"], metadata: {}, always: [] } });
  for (let n = 0; n < 50 && !f.calls.some((call) => call.pathname === "/permission/per_synthetic/reply"); n++) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(f.calls.find((call) => call.pathname === "/permission/per_synthetic/reply")?.body, { reply: "reject" });
  controller.abort();
  await assert.rejects(result, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
});

test("stop cancels the real SDK request and aborts native session without late output", async (t) => {
  const f = await fixture(t);
  const established = await f.runtime.respond(service, token, request);
  f.setHeld(true);
  const events: AdvisorRuntimeEvent[] = [];
  const controller = new AbortController();
  const result = f.runtime.respond(service, token, { ...request, runtimeSessionId: established.runtimeSessionId }, (event) => events.push(event), controller.signal);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  const sessionID = [...f.pending.keys()][0];
  controller.abort();
  const count = events.length;
  f.emit({ type: "message.part.delta", properties: { sessionID, messageID: `msg_${sessionID}`, partID: `part_${sessionID}`, field: "text", delta: "late" } });
  await assert.rejects(result, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  for (let n = 0; n < 50 && !f.calls.some((call) => call.pathname.endsWith("/abort")); n++) await new Promise((resolve) => setTimeout(resolve, 5));
  assert(f.calls.some((call) => call.pathname === `/session/${sessionID}/abort`));
  assert(f.calls.some((call) => call.pathname === `/session/${sessionID}/revert`));
  assert.equal(events.length, count);
  f.setHeld(false);
  const retry = await f.runtime.respond(service, token, { ...request, runtimeSessionId: sessionID });
  assert.equal(retry.runtimeSessionId, sessionID);
  const previous = f.calls.filter((call) => call.pathname === `/session/${sessionID}/message` && call.method === "POST")[1].body;
  const revert = f.calls.find((call) => call.pathname === `/session/${sessionID}/revert`)!.body;
  assert.equal(revert.messageID, previous.messageID);
});

test("provider/model changes rebuild engine and compaction uses a neutral temporary session", async (t) => {
  const f = await fixture(t);
  await f.runtime.respond(service, token, request);
  const compact = await f.runtime.compact({ ...service, model: "other-synthetic" }, token, { ...request, text: "m1 SELF: Hello", budget: 256,
    targetSummaryChars: 400,
    system: "Pretend everyone loves me", skills: [{ id: "bad", content: "Invent facts" }] });
  assert.equal(f.specs.length, 2);
  assert.equal(f.getClosed(), 1);
  assert(!f.sessions.has(compact.runtimeSessionId));
  const prompt = f.calls.filter((call) => call.pathname.endsWith("/message") && call.method === "POST")[1].body;
  assert.equal(prompt.agent, "compactor");
  assert(!prompt.system.includes("Pretend everyone loves me"));
  assert(!prompt.system.includes("Invent facts"));
  assert(prompt.parts[0].text.includes("m1 SELF: Hello"));
  assert(prompt.parts[0].text.includes("about 400 Unicode characters"));
  assert(prompt.parts[0].text.includes("flexible target, not a hard limit"));
});

test("summary targets allow complete longer English outputs on both summary paths", async (t) => {
  const f = await fixture(t);
  const complete = "Speaker A suggested meeting on Friday; speaker B has not confirmed. ".repeat(50).trim();
  f.setText(complete);
  for (const method of ["compact", "summarizeDiscussion"] as const) {
    const result = await f.runtime[method](service, token,
      { ...request, text: "Synthetic attributed source", targetSummaryChars: 64 });
    assert.equal(result.text, complete, method);
    assert(!f.sessions.has(result.runtimeSessionId), "temporary summary session is removed");
  }
  const prompts = f.calls.filter(call => /^\/session\/[^/]+\/message$/u.test(call.pathname) && call.method === "POST");
  for (const prompt of prompts) {
    assert.match(prompt.body.parts[0].text, /flexible target, not a hard limit/u);
    assert.doesNotMatch(prompt.body.parts[0].text, /must not exceed|Use at most/u);
    assert(!prompt.body.parts[0].text.includes("[来源：用户与助手的讨论；不是微信聊天记录]"),
      "chat evidence and discussion compaction must keep their own source envelopes");
  }
});

test("actual provider truncation is distinct from summary size and never returned as a complete summary", async (t) => {
  const f = await fixture(t);
  f.setText("Synthetic incomplete summary ending in");
  f.setFinish("length");
  for (const method of ["compact", "summarizeDiscussion"] as const) {
    await assert.rejects(f.runtime[method](service, token, { ...request, text: "Synthetic source", targetSummaryChars: 500 }),
      (error) => error instanceof AdvisorRuntimeError && error.code === "output-truncated");
    assert.equal(f.sessions.size, 0, "partial summary session is removed");
  }
});

test("provider error response body and API secrets never become public errors", async (t) => {
  const f = await fixture(t);
  f.setError({ name: "APIError", data: { statusCode: 401, message: "secret-key private-body", responseBody: "secret-key full-chat" } });
  await assert.rejects(f.runtime.respond(service, token, request), (error) => {
    assert(error instanceof AdvisorRuntimeError);
    assert.equal(error.code, "auth");
    assert(!JSON.stringify(error).includes("secret-key"));
    assert(!error.message.includes("private-body"));
    return true;
  });
  assert.equal(advisorSafeError(new Error("raw-private-response")).message, "模型服务响应失败");
});

test("a native tool attempt is a terminal permission error even when it also contains text", async (t) => {
  const f = await fixture(t);
  f.setTool(true);
  await assert.rejects(f.runtime.respond(service, token, request), (error) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
  assert(f.calls.some((call) => call.pathname.endsWith("/revert")));
});

test("persisted incomplete-turn checkpoint is reverted before a restored request", async (t) => {
  const f = await fixture(t);
  const result = await f.runtime.respond(service, token, request);
  const prior = f.calls.find((call) => call.pathname.endsWith("/message") && call.method === "POST")!.body;
  const scope = f.sessions.get(result.runtimeSessionId).metadata.advisorScope;
  const directory = path.join(f.specs[0].cwd, ".advisor-checkpoints");
  const target = path.join(directory, createHash("sha256").update(result.runtimeSessionId).digest("hex") + ".json");
  await writeFile(target, JSON.stringify({ schema: 1, scope, sessionID: result.runtimeSessionId, messageID: prior.messageID }));
  await f.runtime.respond(service, token, { ...request, runtimeSessionId: result.runtimeSessionId });
  const rollbackIndex = f.calls.findIndex((call) => call.pathname.endsWith("/revert"));
  const promptIndices = f.calls.map((call, index) => call.pathname.endsWith("/message") && call.method === "POST" ? index : -1).filter((index) => index >= 0);
  assert(rollbackIndex > promptIndices[0] && rollbackIndex < promptIndices[1]);
  await assert.rejects(readFile(target), (error: any) => error.code === "ENOENT");
});

// Added 2026-10-15: regenerate / edit of the latest turn.
test("an answer names its native question, and a later request can drop that turn before asking again", async (t) => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const prompts = () => f.calls.filter((call) => call.pathname.endsWith("/message") && call.method === "POST");
  assert.equal(first.userMessageId, prompts()[0].body.messageID);
  const again = await f.runtime.respond(service, token, { ...request, runtimeSessionId: first.runtimeSessionId,
    revertMessageId: first.userMessageId });
  const revertIndex = f.calls.findIndex((call) => call.pathname === `/session/${first.runtimeSessionId}/revert`);
  const promptIndices = f.calls.map((call, index) => call.pathname.endsWith("/message") && call.method === "POST" ? index : -1).filter((index) => index >= 0);
  assert(revertIndex > promptIndices[0] && revertIndex < promptIndices[1], "the turn is dropped before the new prompt");
  assert.equal(f.calls[revertIndex].body.messageID, first.userMessageId);
  assert.equal(again.runtimeSessionId, first.runtimeSessionId);
  assert.notEqual(again.userMessageId, first.userMessageId);
  // A question the session no longer holds asks for a controlled rebuild; a malformed ID is refused.
  await assert.rejects(f.runtime.respond(service, token, { ...request, runtimeSessionId: first.runtimeSessionId,
    revertMessageId: "msg_missing" }), (error: any) => error instanceof AdvisorRuntimeError && error.code === "native-session-incompatible");
  await assert.rejects(f.runtime.respond(service, token, { ...request, runtimeSessionId: first.runtimeSessionId,
    revertMessageId: "../etc" }), (error: any) => error instanceof AdvisorRuntimeError && error.code === "invalid-request");
  assert.equal(prompts().length, 2, "a refused revert sends no prompt");
});

test("cancellation after reverting a turn but before its prompt leaves a recoverable native session", async t => {
  const f = await fixture(t);
  const first = await f.runtime.respond(service, token, request);
  const controller = new AbortController();
  const replacement = { ...request, runtimeSessionId: first.runtimeSessionId, revertMessageId: first.userMessageId };
  await assert.rejects(f.runtime.respond(service, token, { ...replacement, skills: [{ id: "synthetic", content: "Synthetic guidance" }] },
    event => { if (event.type === "skill") controller.abort(); }, controller.signal),
    error => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  const prompts = () => f.calls.filter(call => call.method === "POST" && call.pathname.endsWith("/message"));
  assert.equal(prompts().length, 1, "a cancelled replacement sent no new model prompt");
  const result = await f.runtime.respond(service, token, replacement);
  assert.equal(result.runtimeSessionId, first.runtimeSessionId);
  assert.equal(prompts().length, 2);
  await assert.rejects(f.runtime.respond(service, token, { ...request, revertMessageId: first.userMessageId }),
    error => error instanceof AdvisorRuntimeError && error.code === "invalid-request");
});

const webSearch = { provider: "searxng" as const, url: "http://127.0.0.1:8080", fetchPages: true };
const permissionDenied = (error: unknown) => error instanceof AdvisorRuntimeError && error.code === "permission-denied";
const mcpCall = (url: string, authorization: string | undefined, body: unknown) => fetch(url, { method: "POST",
  headers: { "content-type": "application/json", ...(authorization ? { authorization } : {}) }, body: JSON.stringify(body) });
// Status of a web tool while it runs: the answer is held open until that status arrives, then stopped.
async function webProgress(f: Awaited<ReturnType<typeof fixture>>, input: AdvisorRuntimeRequest, text: string) {
  f.setHeld(true);
  const events: AdvisorRuntimeEvent[] = [];
  const controller = new AbortController();
  const held = f.runtime.respond(service, token, input, (event) => events.push(event), controller.signal);
  const shown = () => events.some((event) => event.type === "status" && event.state === "searching" && event.text === text);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  for (let n = 0; n < 100 && !shown(); n++) await new Promise((resolve) => setTimeout(resolve, 5));
  controller.abort();
  await assert.rejects(held, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  f.setHeld(false);
  assert(shown(), text);
}

test("an external search source gives only the advisor web tools, over a loopback MCP server with its own secret", async (t) => {
  const f = await fixture(t);
  f.webPart({ tool: "web_search", input: { query: "周末 篮球赛" } });
  await webProgress(f, { ...request, webSearch }, "正在搜索：周末 篮球赛");
  const result = await f.runtime.respond(service, token, { ...request, webSearch });
  assert.equal(result.text, "Hello");
  assert.equal(f.specs.length, 1);
  const config = f.specs[0].config;
  const mcp = (config.mcp as any).web;
  assert.deepEqual(Object.keys(config.mcp!), ["web"]);
  assert.equal(mcp.type, "remote");
  assert.equal(mcp.oauth, false);
  assert.match(mcp.url, /^http:\/\/127\.0\.0\.1:\d+\/mcp$/u);
  assert.match(mcp.headers.authorization, /^Bearer [0-9a-f]{48}$/u);
  const advisor = config.agent!.advisor!;
  assert.deepEqual(advisor.permission, { "*": "deny", read: { "*": "deny" }, web_search: "allow", web_fetch: "allow" });
  assert.equal((advisor.tools as any).web_search, true);
  assert.equal((advisor.tools as any).web_fetch, true);
  assert(String(advisor.prompt).includes("web_search") && String(advisor.prompt).includes("Cite the web sources"));
  const onlinePrompt = f.calls.findLast(call => call.method === "POST" && call.pathname.endsWith("/message"))!.body.system;
  assert.match(onlinePrompt, /Use public-topic keywords only/u);
  assert(!onlinePrompt.includes("change files, browse the web"), "the per-turn prompt agrees with the enabled web tools");
  for (const name of ["compactor", "discussion_compactor", "compaction"] as const) {
    assert.equal((config.agent![name]!.tools as any).web_search, false, name);
    assert.equal(config.agent![name]!.permission, "deny", name);
  }
  assert.equal((config.tools as any).web_search, false);
  const permission = f.sessions.get(result.runtimeSessionId).permission;
  assert.deepEqual(permission[0], { permission: "*", pattern: "*", action: "deny" });
  assert.deepEqual(permission.filter((rule: any) => rule.permission.startsWith("web_")), [
    { permission: "web_search", pattern: "*", action: "allow" }, { permission: "web_fetch", pattern: "*", action: "allow" }]);
  const listed = await (await mcpCall(mcp.url, mcp.headers.authorization, { jsonrpc: "2.0", id: 1, method: "tools/list" })).json();
  assert.deepEqual(listed.result.tools.map((tool: any) => tool.name), ["search", "fetch"]);
  assert.equal((await mcpCall(mcp.url, undefined, { jsonrpc: "2.0", id: 1, method: "tools/list" })).status, 401);
  assert.equal((await mcpCall(mcp.url, "Bearer wrong", { jsonrpc: "2.0", id: 1, method: "tools/list" })).status, 401);
  // A summary never gets web tools and keeps the engine as it is; turning the source off restarts it without them.
  const summary = { ...request, text: "m1 SELF: Hello", budget: 256, maxSummaryChars: 400 };
  await assert.rejects(f.runtime.compact(service, token, summary), permissionDenied);
  f.webPart(undefined);
  await f.runtime.compact(service, token, summary);
  assert.equal(f.specs.length, 1);
  await f.runtime.compactSession(service, token, { ...request, runtimeSessionId: result.runtimeSessionId, operationId: "compact-web" });
  assert.deepEqual(f.sessions.get(result.runtimeSessionId).permission, [{ permission: "*", pattern: "*", action: "deny" }]);
  const offline = await f.runtime.respond(service, token, { ...request, runtimeSessionId: result.runtimeSessionId });
  assert.equal(offline.runtimeSessionId, result.runtimeSessionId, "changing search settings keeps the discussion's session");
  assert.equal(f.specs.length, 2);
  assert.deepEqual(f.specs[1].config.mcp, {});
  assert.deepEqual(f.specs[1].config.agent!.advisor!.permission, { "*": "deny", read: { "*": "deny" } });
  assert.equal((f.specs[1].config.agent!.advisor!.tools as any).web_search, false);
  await assert.rejects(mcpCall(mcp.url, mcp.headers.authorization, { jsonrpc: "2.0", id: 1, method: "tools/list" }));
});

test("web tools are refused when no search source is configured, and opening pages only when allowed", async (t) => {
  const f = await fixture(t);
  f.webPart({ tool: "web_search", input: { query: "合成搜索" } });
  await assert.rejects(f.runtime.respond(service, token, request), permissionDenied);
  f.webPart({ tool: "web_fetch", input: { url: "https://example.com/page" } });
  await assert.rejects(f.runtime.respond(service, token, { ...request, webSearch: { ...webSearch, fetchPages: false } }), permissionDenied);
  const config = f.specs.at(-1)!.config;
  assert.deepEqual(config.agent!.advisor!.permission, { "*": "deny", read: { "*": "deny" }, web_search: "allow" });
  assert.equal((config.agent!.advisor!.tools as any).web_fetch, false);
  await webProgress(f, { ...request, webSearch }, "正在打开网页：example.com");
  assert.equal((await f.runtime.respond(service, token, { ...request, webSearch })).text, "Hello");
  // A search source that is not a usable address means no web access, not a broken engine.
  f.webPart({ tool: "web_search", input: { query: "合成搜索" } });
  await assert.rejects(f.runtime.respond(service, token, { ...request, webSearch: { ...webSearch, url: "file:///etc/passwd" } }), permissionDenied);
  assert.deepEqual(f.specs.at(-1)!.config.mcp, {});
});

test("native retry timing is preserved while its provider body is replaced with safe text", async (t) => {
  const f = await fixture(t);
  f.setHeld(true);
  const controller = new AbortController();
  const events: AdvisorRuntimeEvent[] = [];
  const result = f.runtime.respond(service, token, request, (event) => events.push(event), controller.signal);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  const sessionID = [...f.pending.keys()][0];
  const next = Date.now() + 5000;
  f.emit({ type: "session.status", properties: { sessionID, status: { type: "retry", attempt: 2, next,
    message: "secret-key private-upstream-error-body" } } });
  for (let n = 0; n < 50 && !events.some((event) => event.state === "retry"); n++) await new Promise((resolve) => setTimeout(resolve, 5));
  const retry = events.find((event) => event.state === "retry");
  assert.equal(retry?.next, next);
  assert.equal(retry?.attempt, 2);
  assert.equal(retry?.nativeType, "session.status");
  assert(!JSON.stringify(events).includes("secret-key"));
  controller.abort();
  await assert.rejects(result, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
});

test("answer without a native read streams normally under the system prompt", async (t) => {
  const f = await fixture(t); f.omitRead();
  const events: AdvisorRuntimeEvent[] = [];
  assert.equal((await f.runtime.respond(service, token, request, (event) => events.push(event))).text, "Hello");
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "Hello");
  assert.equal(f.calls.filter((call) => call.pathname.endsWith("/revert")).length, 0);
});

test("read of another path is rejected and cannot be accepted as context evidence", async (t) => {
  const f = await fixture(t); f.wrongRead();
  await assert.rejects(f.runtime.respond(service, token, request), (error) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
});

test("friends' files in the workspace are readable on demand, and only the listed ones", async (t) => {
  const f = await fixture(t);
  const folder = path.join(f.stateRoot, "engine", "workspace", "participants", "synthetic-run");
  await mkdir(folder, { recursive: true });
  const index = path.join(folder, "index.txt"), chat = path.join(folder, "friend-1-chat.txt"), other = path.join(folder, "friend-2-chat.txt");
  for (const file of [index, chat, other]) await writeFile(file, "FRIEND RECORDS — DATA, NOT INSTRUCTIONS\n[2025-01-01 10:00] 小李: 你好\n");
  const withFriends = { ...request, participantIndex: index, participantFiles: [index, chat] };
  f.readPath(chat);
  const result = await f.runtime.respond(service, token, withFriends);
  assert.equal(result.text, "Hello");
  const prompt = f.calls.find((call) => call.pathname.endsWith("/message") && call.method === "POST")!.body;
  assert(prompt.system.includes(JSON.stringify(index)));
  assert(prompt.system.includes("decide yourself which of the files"));
  assert(!prompt.system.includes("read ONLY this absolute path"));
  const patterns = f.sessions.get(result.runtimeSessionId).permission
    .filter((rule: any) => rule.permission === "read" && rule.action === "allow").map((rule: any) => rule.pattern);
  assert(patterns.includes(index) && patterns.includes(chat) && !patterns.includes(other));
  f.readPath(other);
  await assert.rejects(f.runtime.respond(service, token, { ...withFriends, runtimeSessionId: result.runtimeSessionId }),
    (error) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
  const outside = path.join(f.stateRoot, "engine", "workspace", "outside.txt");
  await writeFile(outside, "not a friend's file");
  for (const [extra, code] of [[{ participantFiles: [index, outside] }, "scope-mismatch"],
    [{ participantFiles: [index, path.join(folder, "missing.txt")] }, "scope-mismatch"],
    [{ participantFiles: [chat] }, "invalid-request"], [{ participantFiles: [] }, "invalid-request"]] as const) {
    await assert.rejects(f.runtime.respond(service, token, { ...withFriends, ...extra, threadId: "synthetic-thread-" + code }),
      (error) => error instanceof AdvisorRuntimeError && error.code === code);
  }
});

test("truncated read does not block the model's response", async (t) => {
  const f = await fixture(t); f.truncatedRead();
  assert.equal((await f.runtime.respond(service, token, request)).text, "Hello");
});

test("multiple native read pages remain available to the model", async (t) => {
  const f = await fixture(t); f.pageRead();
  assert.equal((await f.runtime.respond(service, token, request)).text, "Hello");
});

test("missing ranges in a paged read do not discard a valid response", async (t) => {
  const f = await fixture(t); f.gapRead();
  assert.equal((await f.runtime.respond(service, token, request)).text, "Hello");
  assert.equal(f.calls.filter((call) => call.pathname.endsWith("/revert")).length, 0);
});

test("read errors on the allowed file are handled by the model", async (t) => {
  const f = await fixture(t); f.failedRead(); f.setText("The conversation file is unavailable.");
  assert.equal((await f.runtime.respond(service, token, request)).text, "The conversation file is unavailable.");
});

test("external instructions loaded by a read remain forbidden, including delayed tool events", async (t) => {
  for (const historyOnly of [false, true]) {
    const f = await fixture(t); f.loadedInstructions(historyOnly);
    await assert.rejects(f.runtime.respond(service, token, request),
      (error) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
  }
});

test("one frozen scope cannot start two native sessions concurrently", async (t) => {
  const f = await fixture(t); f.setHeld(true);
  const controller = new AbortController();
  const first = f.runtime.respond(service, token, request, () => {}, controller.signal);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  await assert.rejects(f.runtime.respond(service, token, request), (error) => error instanceof AdvisorRuntimeError && error.code === "busy");
  controller.abort();
  await assert.rejects(first, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  assert.equal(f.calls.filter((call) => call.pathname === "/session" && call.method === "POST").length, 1);
});

test("ordinary model narration streams before a tool read without being filtered", async (t) => {
  const f = await fixture(t); f.preface();
  const events: AdvisorRuntimeEvent[] = [];
  const result = await f.runtime.respond(service, token, request, (event) => events.push(event));
  assert.equal(result.text, "我先看看这轮聊天资料。\nHello");
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "我先看看这轮聊天资料。Hello");
});

test("text from earlier native assistant steps survives finalization without duplicates", async (t) => {
  const f = await fixture(t); f.preface(); f.separateReadStep();
  const events: AdvisorRuntimeEvent[] = [];
  const result = await f.runtime.respond(service, token, request, event => events.push(event));
  assert.equal(result.text, "我先看看这轮聊天资料。\nHello");
  assert.equal(events.filter(event => event.type === "text").map(event => event.text).join(""), "我先看看这轮聊天资料。Hello");
  const previousParts = f.sessions.get(result.runtimeSessionId).history[0].parts;
  assert(previousParts.some((part: any) => part.type === "text"));
  assert.equal(f.calls.filter(call => call.pathname.endsWith("/revert")).length, 0);
});

test("late SSE text is not filtered by read completion time", async (t) => {
  const f = await fixture(t); f.preface(true);
  const events: AdvisorRuntimeEvent[] = [];
  assert.equal((await f.runtime.respond(service, token, request, (event) => events.push(event))).text, "我先看看这轮聊天资料。\nHello");
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "我先看看这轮聊天资料。Hello");
});

test("timestamp-free text uses normal incremental output", async (t) => {
  const f = await fixture(t); f.preface(true); f.noPrefaceTime(); f.noTextTime();
  const events: AdvisorRuntimeEvent[] = [];
  assert.equal((await f.runtime.respond(service, token, request, (event) => events.push(event))).text, "我先看看这轮聊天资料。\nHello");
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "我先看看这轮聊天资料。Hello");
});

test("equal timestamps do not change which model text is accepted", async (t) => {
  const f = await fixture(t); f.preface(true); f.equalNativeTime();
  const events: AdvisorRuntimeEvent[] = [];
  assert.equal((await f.runtime.respond(service, token, request, (event) => events.push(event))).text, "我先看看这轮聊天资料。\nHello");
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "我先看看这轮聊天资料。Hello");
});

test("text without a read or timestamp streams before HTTP completes", async (t) => {
  const f = await fixture(t); f.omitRead(); f.noTextTime(); f.setHeld(true);
  const events: AdvisorRuntimeEvent[] = [];
  const controller = new AbortController();
  const result = f.runtime.respond(service, token, request, (event) => events.push(event), controller.signal);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  for (let n = 0; n < 50 && !events.some((event) => event.type === "text"); n++) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "Hello");
  controller.abort(); await assert.rejects(result, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
});

test("repeated busy is coalesced, reasoning changes phase once, retry timing changes are retained", async (t) => {
  const f = await fixture(t); f.setHeld(true);
  const events: AdvisorRuntimeEvent[] = [];
  const controller = new AbortController();
  const result = f.runtime.respond(service, token, request, (event) => events.push(event), controller.signal);
  while (!f.pending.size) await new Promise((resolve) => setTimeout(resolve, 5));
  const sessionID = [...f.pending.keys()][0];
  const nativeEvent = (type: string, properties: any) => f.emit({ type, properties: { sessionID, ...properties } });
  for (let n = 0; n < 4; n++) nativeEvent("session.status", { status: { type: "busy" } });
  nativeEvent("message.part.updated", { part: { id: "reasoning_part", sessionID, messageID: `msg_${sessionID}`, type: "reasoning", text: "" } });
  for (const delta of ["A", "B", "C"]) {
    nativeEvent("message.part.delta", { messageID: `msg_${sessionID}`, partID: "reasoning_part", field: "text", delta });
    nativeEvent("session.status", { status: { type: "busy" } });
  }
  const next = Date.now() + 5000;
  nativeEvent("session.status", { status: { type: "retry", attempt: 1, next } });
  nativeEvent("session.status", { status: { type: "retry", attempt: 1, next } });
  nativeEvent("session.status", { status: { type: "retry", attempt: 2, next: next + 5000 } });
  for (let n = 0; n < 50 && !events.some((event) => event.attempt === 2); n++) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(events.filter((event) => event.type === "status" && event.state === "answering").length, 1);
  assert.equal(events.filter((event) => event.type === "status" && event.state === "thinking").length, 1);
  assert.equal(events.filter((event) => event.type === "reasoning").map((event) => event.text).join(""), "ABC");
  assert.deepEqual(events.filter((event) => event.state === "retry").map((event) => [event.attempt, event.next]), [[1, next], [2, next + 5000]]);
  controller.abort(); await assert.rejects(result, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
});

test("opencode's configuration and environment hold the relay address and secret only, never a credential or token", async (t) => {
  const f = await fixture(t);
  await f.runtime.respond(f.gatewayService, token, request);
  const spec = f.specs[0];
  const provider = Object.values(spec.config.provider!)[0]!;
  assert.match(String(provider.options!.baseURL), /^http:\/\/127\.0\.0\.1:\d+\/s\/svc-1$/u);
  assert.notEqual(provider.options!.baseURL, f.gatewayService.url, "opencode talks to the plugin's relay, not to the gateway");
  assert.match(String(provider.options!.apiKey), /^[a-f0-9]{64}$/u);
  assert.equal(JSON.stringify(spec.config).split('"apiKey"').length, 2, "the relay secret is the only key");
  assert.deepEqual(JSON.parse(spec.env.OPENCODE_CONFIG_CONTENT!), spec.config);
  assert(!JSON.stringify([spec.env, spec.config]).includes(token), "no execution token in opencode's config or environment");
  assert(!JSON.stringify(spec.config).includes("x-kyf"));
  assert(!Object.keys(spec.env).some(key => /API_KEY|TOKEN/u.test(key)));
});

test("each call reaches the gateway as Bearer <token> under its step, and only while it runs", async (t) => {
  const f = await fixture(t); f.callProviders();
  const svc = f.gatewayService;
  const first = await f.runtime.respond(svc, token, request);
  await f.runtime.compact(svc, "wechat-summary-token", { ...request, text: "m1 SELF: Hello", budget: 256 });
  await f.runtime.summarizeDiscussion(svc, "discussion-token", { ...request, text: "Q: synthetic goal", targetSummaryChars: 400 });
  await f.runtime.compactSession({ ...svc, stamp: "another-stamp" }, "compaction-token",
    { ...request, runtimeSessionId: first.runtimeSessionId, operationId: "operation-1" });
  assert.deepEqual(f.providerStatuses, [200, 200, 200, 200]);
  assert.deepEqual(f.gatewaySeen.map(seen => [seen.url, seen.headers.authorization]), [
    ["/s/svc-1~advisor/chat/completions", `Bearer ${token}`],
    ["/s/svc-1~wechat-summary/chat/completions", "Bearer wechat-summary-token"],
    ["/s/svc-1~discussion-summary/chat/completions", "Bearer discussion-token"],
    ["/s/svc-1~discussion-compaction/chat/completions", "Bearer compaction-token"]]);
  const secret = String(Object.values(f.specs[0].config.provider!)[0]!.options!.apiKey);
  for (const seen of f.gatewaySeen) {
    assert(!JSON.stringify(seen).includes(secret), "the relay secret stays between opencode and the relay");
    assert(!Object.keys(seen.headers).some(key => key.startsWith("x-opencode-") || key.startsWith("x-kyf-")));
  }
  assert.equal(await f.callProvider(first.runtimeSessionId), 401, "once the call settles its native session has no execution");
  assert.equal(f.gatewaySeen.length, 4);
  assert.equal(f.specs.length, 1, "one engine per account; a new stamp alone does not restart it");
  await assert.rejects(f.runtime.respond(svc, "", request), (error) => error instanceof AdvisorRuntimeError && error.code === "invalid-request");
  await assert.rejects(f.runtime.compact(svc, "two words", { ...request, text: "m1" }),
    (error) => error instanceof AdvisorRuntimeError && error.code === "invalid-request");
});

test("a later answer reuses its native session with only its own fresh execution token", async (t) => {
  const f = await fixture(t); f.callProviders();
  const first = await f.runtime.respond(f.gatewayService, "execution-first", request);
  assert.equal(await f.callProvider(first.runtimeSessionId), 401);
  const next = await f.runtime.respond(f.gatewayService, "execution-second", {
    ...request, runtimeSessionId: first.runtimeSessionId, message: "A second synthetic question",
  });
  assert.equal(next.runtimeSessionId, first.runtimeSessionId, "the task token is not part of native session identity");
  assert.deepEqual(f.gatewaySeen.map(seen => seen.headers.authorization), ["Bearer execution-first", "Bearer execution-second"]);
  assert.equal(f.specs.length, 1);
  assert.equal(await f.callProvider(next.runtimeSessionId), 401, "the second binding ends with the second answer");
  assert(!JSON.stringify(f.specs).includes("execution-second"));
});

test("concurrent calls after a service change restart the engine once and each binds on that engine's relay", async (t) => {
  const f = await fixture(t); f.callProviders();
  await f.runtime.respond(f.gatewayService, "execution-before", request);
  const changed = { ...f.gatewayService, model: "another-synthetic-model" };
  const results = await Promise.allSettled([
    f.runtime.respond(changed, "execution-a", { ...request, threadId: "synthetic-thread-a" }),
    f.runtime.respond(changed, "execution-b", { ...request, threadId: "synthetic-thread-b" }),
    f.runtime.inspectSession(changed, { ...request, threadId: "synthetic-thread-c", runtimeSessionId: "ses_absent" }),
  ]);
  assert.deepEqual(results.map(result => result.status), ["fulfilled", "fulfilled", "fulfilled"], JSON.stringify(results));
  assert.equal(f.specs.length, 2, "one restart for the new service, not one per waiting caller");
  assert.equal(f.getClosed(), 1, "no engine is left running unowned");
  assert.deepEqual(f.providerStatuses, [200, 200, 200]);
  assert.deepEqual(f.gatewaySeen.map(seen => seen.headers.authorization).slice(1).sort(), ["Bearer execution-a", "Bearer execution-b"]);
});

test("the worker speaks JSON lines with {service, token, request}; generate and token-less model calls are refused", async (t) => {
  const parent = path.resolve(".local", "advisor-build", "runtime-tests");
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(path.join(parent, "worker-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const worker = fileURLToPath(new URL("../engine/worker.ts", import.meta.url));
  const child = spawn(process.execPath, ["--import", "tsx", worker, "--root", root, "--state", path.join(root, "state")],
    { cwd: path.resolve("."), stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
  const exited = new Promise<number | null>(resolve => child.once("exit", resolve));
  t.after(() => { if (child.exitCode === null) child.kill(); });
  const waiting = new Map<unknown, (reply: any) => void>();
  readline.createInterface({ input: child.stdout }).on("line", line => {
    const reply = JSON.parse(line);
    if ("result" in reply || "error" in reply) waiting.get(reply.id)?.(reply);
  });
  const call = (command: Record<string, unknown>) => new Promise<any>(resolve => {
    waiting.set(command.id, resolve);
    child.stdin.write(JSON.stringify(command) + "\n");
  });
  assert.deepEqual((await call({ id: 1, cmd: "doctor" })).result, { state: "missing", version: ADVISOR_ENGINE_VERSION });
  const refused: Array<[string, Record<string, unknown>]> = [["generate", { service, token, request }], ["respond", { service, request }],
    ["respond", { config: { protocol: "chat_completions", baseUrl: "http://127.0.0.1:1/v1", model: "m", apiKey: "k" }, request }],
    ["inspectSession", { request }], ["respond", { service: { ...service, url: "https://api.example.com/v1" }, token, request }]];
  for (const [index, [cmd, payload]] of refused.entries()) {
    assert.equal((await call({ id: 10 + index, cmd, payload })).error.code, "invalid-request", `${cmd} ${JSON.stringify(payload)}`);
  }
  assert.equal((await call({ id: 20, cmd: "respond", payload: { service, token, request } })).error.code, "engine-missing");
  assert.equal((await call({ id: 21, cmd: "inspectSession", payload: { service, request: { ...request, runtimeSessionId: "ses_1" } } }))
    .error.code, "engine-missing", "session commands need no token");
  assert.deepEqual((await call({ id: 22, cmd: "close" })).result, { closed: true });
  assert.equal(await exited, 0);
});
