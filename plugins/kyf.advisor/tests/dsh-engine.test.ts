// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// End-to-end: the advisor runtime against the installed DeepSeek Harness and a local stand-in for the Core
// model gateway. Skipped where DSH is not installed. Only synthetic text is used.
import assert from "node:assert/strict";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { AdvisorRuntime, AdvisorRuntimeError, type AdvisorRuntimeEvent, type EngineService } from "../engine/runtime";
import { resolveHarness } from "../engine/harness";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..", "..");
const ENGINE = path.resolve(HERE, "..", "engine");
const TOKEN = "exec-token-e2e";

interface Seen { url: string; authorization?: string; body: any }

/** An OpenAI-compatible endpoint mounted like a gateway service: /s/<id>~<step>/chat/completions. */
async function gateway(): Promise<{ server: Server; service: EngineService; seen: Seen[] }> {
  const seen: Seen[] = [];
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || "{}");
    seen.push({ url: req.url ?? "", authorization: req.headers.authorization, body });
    const messages: any[] = body.messages ?? [];
    const content = (message: any) => typeof message?.content === "string" ? message.content : JSON.stringify(message?.content ?? "");
    // DSH appends its own context messages after the prompt: the turn is unanswered while no tool result follows it.
    const asked = messages.findLastIndex(message => message.role === "user" && content(message).includes("[来源"));
    const answered = messages.findLastIndex(message => message.role === "tool");
    const text = content(answered > asked ? messages[answered] : messages[asked]);
    res.writeHead(200, { "content-type": "text/event-stream" });
    const send = (delta: unknown, finish: string | null = null, usage?: unknown) => res.write("data: " + JSON.stringify({ id: "c", object: "chat.completion.chunk",
      model: body.model, choices: [{ index: 0, delta, finish_reason: finish }], ...(usage ? { usage } : {}) }) + "\n\n");
    const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
    const listed = /this absolute path: ("(?:[^"\\]|\\.)*")/u.exec(text);
    if (asked > answered && content(messages[asked]).includes("WEBSEARCH")) {
      send({ role: "assistant", tool_calls: [{ index: 0, id: "call_" + seen.length, type: "function", function: { name: "mcp__web__search", arguments: JSON.stringify({ query: "synthetic query" }) } }] });
      send({}, "tool_calls", { prompt_tokens: 50, completion_tokens: 5, total_tokens: 55 });
    } else if (asked > answered && listed) {
      const target = text.includes("FORBIDDEN") ? path.join(os.tmpdir(), "not-listed.txt") : JSON.parse(listed[1]);
      send({ role: "assistant", tool_calls: [{ index: 0, id: "call_" + seen.length, type: "function", function: { name: "read", arguments: JSON.stringify({ file_path: target }) } }] });
      send({}, "tool_calls", { prompt_tokens: 50, completion_tokens: 5, total_tokens: 55 });
    } else {
      const slow = content(messages[asked]).includes("SLOW");
      send({ role: "assistant", reasoning_content: "considering" });
      const marker = /MARKER-[A-Z0-9]+/u.exec(text)?.[0] ?? "no-marker";
      const answer = `answer#${messages.filter(message => message.role === "user" && content(message).includes("[来源")).length} saw ${marker}. ` + "x".repeat(slow ? 400 : 20);
      for (const piece of answer.match(/.{1,16}/gsu) ?? []) { await wait(slow ? 300 : 5); send({ content: piece }); }
      send({}, "stop", { prompt_tokens: 200, completion_tokens: 30, total_tokens: 230, prompt_tokens_details: { cached_tokens: 20 } });
    }
    res.end("data: [DONE]\n\n");
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  return { server, seen, service: { id: "svc", protocol: "chat_completions", model: "synthetic-model", url: `http://127.0.0.1:${port}/s/svc`, contextTokens: 65_536, timeoutMs: 120_000 } };
}

const request = (extra: Record<string, unknown> = {}) => ({ account: "acct", user: "friend", agentId: "agent", threadId: "thread", sourceId: "source",
  system: "Template: answer briefly.", contextFileText: "2026-01-01 friend: hello MARKER-ALPHA7", message: "what did they say?", ...extra });

test("advisor turns run on DeepSeek Harness through the gateway relay", { timeout: 240_000 }, async (t) => {
  if (!await resolveHarness(ROOT)) { t.skip("DeepSeek Harness is not installed"); return; }
  const stateRoot = await mkdtemp(path.join(os.tmpdir(), "kyf-dsh-"));
  const { server, service, seen } = await gateway();
  const summaries: any[] = [];
  const runtime = new AdvisorRuntime({ root: ROOT, stateRoot, engineDir: ENGINE,
    generate: async (payload: any) => { summaries.push(payload); return { text: "SUMMARY of the discussion" }; } });
  t.after(async () => { await runtime.close(); server.close(); await rm(stateRoot, { recursive: true, force: true }); });

  const doctor = await runtime.doctor();
  assert.equal(doctor.state, "available");
  assert.match(doctor.version, /^\d+\.\d+\.\d+/u);

  // A new discussion: created event, live text, the context file read, usage, the execution token at the gateway.
  const events: AdvisorRuntimeEvent[] = [];
  const first = await runtime.respond(service, TOKEN, request(), event => events.push(event));
  assert.match(first.runtimeSessionId, /^dsh_[a-f0-9]{32}$/u);
  assert.match(first.text, /answer#1 saw MARKER-ALPHA7/u);
  assert.equal(events.find(event => event.state === "native-session-created")?.runtimeSessionId, first.runtimeSessionId);
  assert.ok(events.filter(event => event.type === "text").length > 1, "the answer streams in pieces");
  assert.equal(events.filter(event => event.type === "text").map(event => event.text).join(""), first.text);
  assert.ok(events.some(event => event.type === "reasoning"));
  assert.ok(!events.some(event => event.state === "retry"), "the request after a tool round is not a retry");
  assert.equal(first.usage?.cacheReadTokens, 20);
  assert.equal(first.usage?.totalInputTokens, 200);
  assert.match(first.usage?.source ?? "", /^deepseek-harness-/u);
  assert.ok(seen.every(item => item.url === "/s/svc~advisor/chat/completions" && item.authorization === `Bearer ${TOKEN}`));
  const system = seen[0].body.messages[0].content as string;
  assert.match(system, /read-only conversation adviser/u);
  assert.match(system, /Template: answer briefly\./u);
  assert.deepEqual((seen[0].body.tools as any[]).map(tool => tool.function.name), ["read"]);
  assert.ok((first.effectiveHistoryTokensEstimate ?? 0) > 0);

  // The same session continues in a new DSH process and still holds the first turn.
  const second = await runtime.respond(service, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, message: "and then?" }));
  assert.equal(second.runtimeSessionId, first.runtimeSessionId);
  assert.match(second.text, /answer#2/u);
  assert.ok(seen.at(-1)!.body.messages.some((message: any) => message.role === "assistant" && String(message.content).includes("answer#1")));

  // Without thinking: chat-template switches are added on the way to the gateway.
  await runtime.respond(service, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, message: "quick one", thinking: false }));
  assert.deepEqual(seen.at(-1)!.body.chat_template_kwargs, { thinking: false, enable_thinking: false });

  // Inspect, then compact manually: the summary replaces the turns and the next turn starts a fresh DSH session from it.
  const inspected = await runtime.inspectSession(service, request({ runtimeSessionId: first.runtimeSessionId }));
  assert.equal(inspected.state, "idle");
  const compacted = await runtime.compactSession(service, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, operationId: "op-1" }));
  assert.equal(compacted.state, "completed");
  assert.equal(compacted.summary, "SUMMARY of the discussion");
  assert.equal(summaries[0].step, "discussion-compaction");
  assert.match(summaries[0].request.prompt, /answer#1 saw MARKER-ALPHA7/u);
  const afterCompaction = await runtime.respond(service, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, message: "continue" }));
  assert.match(afterCompaction.text, /answer#1/u, "a fresh DSH session");
  assert.match(seen.at(-1)!.body.messages[0].content, /<restored-discussion>[\s\S]*SUMMARY of the discussion/u);
  assert.equal((await runtime.inspectSession(service, request({ runtimeSessionId: first.runtimeSessionId, operationId: "op-1" }))).state, "completed");

  // A read outside this turn's files ends the turn; the unfinished turn then asks for a rebuild.
  await assert.rejects(runtime.respond(service, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, message: "FORBIDDEN read" })),
    (error: unknown) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
  await assert.rejects(runtime.respond(service, TOKEN, request({ runtimeSessionId: first.runtimeSessionId })),
    (error: unknown) => error instanceof AdvisorRuntimeError && error.code === "native-session-incompatible");

  // Stop while the answer streams.
  const controller = new AbortController();
  const stopped = runtime.respond(service, TOKEN, request({ threadId: "thread-2", message: "SLOW answer please" }), event => {
    if (event.type === "text") controller.abort();
  }, controller.signal);
  await assert.rejects(stopped, (error: unknown) => error instanceof AdvisorRuntimeError && error.code === "cancelled");

  // With a search source, the turn gets the advisor's own web tools and nothing else of DSH's web access.
  const searches: string[] = [];
  const searx = createServer((req, res) => {
    searches.push(new URL(req.url ?? "/", "http://127.0.0.1").searchParams.get("q") ?? "");
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ results: [{ title: "Synthetic result", url: "https://example.com/a", content: "MARKER-WEB9 snippet" }] }));
  });
  await new Promise<void>(resolve => searx.listen(0, "127.0.0.1", resolve));
  t.after(() => { searx.close(); });
  const webEvents: AdvisorRuntimeEvent[] = [];
  const searched = await runtime.respond(service, TOKEN, request({ threadId: "thread-web", message: "WEBSEARCH please",
    webSearch: { provider: "searxng", url: `http://127.0.0.1:${(searx.address() as { port: number }).port}`, fetchPages: false } }), event => webEvents.push(event));
  assert.deepEqual(searches, ["synthetic query"]);
  assert.match(searched.text, /saw MARKER-WEB9/u, "the search result reached the model");
  const offered = (seen.at(-1)!.body.tools as any[]).map(tool => tool.function.name).sort();
  assert.deepEqual(offered, ["mcp__web__search", "read"]);
  await runtime.deleteSession(service, request({ threadId: "thread-web", runtimeSessionId: searched.runtimeSessionId }));

  // Sessions of an earlier engine are unknown: the backend rebuilds from its transcript.
  assert.equal((await runtime.inspectSession(service, request({ runtimeSessionId: "ses_legacyOpenCode123" }))).state, "missing");
  await assert.rejects(runtime.respond(service, TOKEN, request({ runtimeSessionId: "ses_legacyOpenCode123" })),
    (error: unknown) => error instanceof AdvisorRuntimeError && error.code === "native-session-missing");

  // Deleting removes the advisor's record and DSH's own session files.
  await runtime.deleteSession(service, request({ runtimeSessionId: first.runtimeSessionId }));
  assert.equal((await runtime.inspectSession(service, request({ runtimeSessionId: first.runtimeSessionId }))).state, "missing");
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "sessions")), []);
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "launch")), []);
});
