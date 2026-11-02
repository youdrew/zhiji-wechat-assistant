// Opt-in actual pinned engine verification; synthetic provider and conversation only.
// Modified 2026-10-13: opencode reaches a fake Core gateway through the plugin's ExecutionRelay; the gateway (not
// the engine) records traces, so this checks the gateway path, step and token of every request the engine makes.
// Run with KYF_TRACE_ENGINE_TEST=1 (and KYF_ENGINE_ROOT=<checkout with .local/advisor-engine> if not this one).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer, type ServerResponse } from "node:http";
import { copyFile, link, mkdir, mkdtemp, rm, readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { AdvisorRuntime, resolveAdvisorEngine, startAdvisorEngine, type AdvisorEngineSpec,
  type EngineService } from "../plugins/kyf.advisor/engine/runtime";
import type { Protocol } from "../electron/model-connectors";

function allStrings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (!value || typeof value !== "object") return [];
  return Object.values(value).flatMap(allStrings);
}
function reply(res: ServerResponse, protocol: Protocol, tool?: string, duringStream?: () => void, forceCompaction = false) {
  res.writeHead(200, { "content-type": "text/event-stream" });
  const args = tool && JSON.stringify({ filePath: tool, offset: 1, limit: 2000 });
  const data = (value: unknown, event?: string) => res.write(`${event ? `event: ${event}\n` : ""}data: ${JSON.stringify(value)}\n\n`);
  if (protocol === "chat_completions" || protocol === "ollama") {
    data({ id: "chat_synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic",
      choices: [{ index: 0, delta: tool ? { role: "assistant", tool_calls: [{ index: 0, id: "call_synthetic", type: "function",
        function: { name: "read", arguments: args } }] } : { role: "assistant", content: "Synthetic complete answer." }, finish_reason: null }] });
    data({ id: "chat_synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic", choices: [{ index: 0, delta: {}, finish_reason: tool ? "tool_calls" : "stop" }],
      usage: { prompt_tokens: forceCompaction ? 60000 : 20, completion_tokens: 5, total_tokens: forceCompaction ? 60005 : 25 } });
    if (duringStream) { duringStream(); setTimeout(() => res.end("data: [DONE]\n\n"), 50); }
    else res.end("data: [DONE]\n\n"); return;
  }
  if (protocol === "anthropic") {
    data({ type: "message_start", message: { id: "msg_synthetic", type: "message", role: "assistant", content: [], model: "synthetic",
      stop_reason: null, stop_sequence: null, usage: { input_tokens: 20, output_tokens: 0 } } }, "message_start");
    data({ type: "content_block_start", index: 0, content_block: tool ? { type: "tool_use", id: "call_synthetic", name: "read", input: {} } : { type: "text", text: "" } }, "content_block_start");
    data({ type: "content_block_delta", index: 0, delta: tool ? { type: "input_json_delta", partial_json: args } : { type: "text_delta", text: "Synthetic complete answer." } }, "content_block_delta");
    data({ type: "content_block_stop", index: 0 }, "content_block_stop");
    data({ type: "message_delta", delta: { stop_reason: tool ? "tool_use" : "end_turn", stop_sequence: null }, usage: { output_tokens: 5 } }, "message_delta");
    data({ type: "message_stop" }, "message_stop"); res.end(); return;
  }
  if (protocol === "gemini") {
    data({ candidates: [{ content: { role: "model", parts: tool ? [{ functionCall: { name: "read", args: JSON.parse(args!) } }] : [{ text: "Synthetic complete answer." }] }, finishReason: "STOP", index: 0 }],
      usageMetadata: { promptTokenCount: 20, candidatesTokenCount: 5, totalTokenCount: 25 }, modelVersion: "synthetic" });
    res.end(); return;
  }
  const response = { id: "resp_synthetic", object: "response", created_at: 1, model: "synthetic", status: "in_progress", output: [],
    usage: { input_tokens: 20, output_tokens: 5, total_tokens: 25, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } };
  data({ type: "response.created", response }, "response.created");
  const item = tool ? { id: "fc_synthetic", type: "function_call", call_id: "call_synthetic", name: "read", arguments: args, status: "completed" } :
    { id: "msg_synthetic", type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text: "Synthetic complete answer.", annotations: [] }] };
  data({ type: "response.output_item.added", output_index: 0, item: tool ? { ...item, arguments: "", status: "in_progress" } : { ...item, content: [], status: "in_progress" } }, "response.output_item.added");
  if (tool) {
    data({ type: "response.function_call_arguments.delta", item_id: item.id, output_index: 0, delta: args }, "response.function_call_arguments.delta");
    data({ type: "response.function_call_arguments.done", item_id: item.id, output_index: 0, arguments: args }, "response.function_call_arguments.done");
  } else {
    data({ type: "response.content_part.added", item_id: item.id, output_index: 0, content_index: 0, part: { type: "output_text", text: "", annotations: [] } }, "response.content_part.added");
    data({ type: "response.output_text.delta", item_id: item.id, output_index: 0, content_index: 0, delta: "Synthetic complete answer." }, "response.output_text.delta");
    data({ type: "response.output_text.done", item_id: item.id, output_index: 0, content_index: 0, text: "Synthetic complete answer." }, "response.output_text.done");
  }
  data({ type: "response.output_item.done", output_index: 0, item }, "response.output_item.done");
  data({ type: "response.completed", response: { ...response, status: "completed", output: [item] } }, "response.completed"); res.end();
}

const TOKEN_HEADER: Record<Protocol, string> = { chat_completions: "authorization", responses: "authorization", ollama: "authorization",
  anthropic: "x-api-key", gemini: "x-goog-api-key" };
const PROTOCOL_PATH: Record<Protocol, RegExp> = { chat_completions: /^\/chat\/completions$/u, ollama: /^\/v1\/chat\/completions$/u,
  responses: /^\/responses$/u, anthropic: /^\/messages$/u, gemini: /^\/models\/synthetic:streamGenerateContent\?alt=sse$/u };

test("actual OpenCode 1.18.34 reaches the gateway only through the execution relay across all five protocols", {
  skip: process.env.KYF_TRACE_ENGINE_TEST !== "1", timeout: 180_000,
}, async t => {
  const repository = path.resolve(process.env.KYF_ENGINE_ROOT ?? ".");
  const executable = await resolveAdvisorEngine(repository);
  const parent = path.resolve(".local", "model-trace-tests");
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(path.join(parent, "engine-"));
  await mkdir(path.join(root, ".local", "advisor-engine"), { recursive: true });
  await link(executable, path.join(root, ".local", "advisor-engine", "opencode.exe"));
  await copyFile(path.join(path.dirname(executable), "manifest.json"), path.join(root, ".local", "advisor-engine", "manifest.json"));
  t.after(async () => { await rm(root, { recursive: true, force: true }); });
  for (const protocol of ["chat_completions", "responses", "anthropic", "gemini", "ollama"] as const) {
    const packets: any[] = [], seen: Array<{ url: string; headers: Record<string, unknown> }> = [];
    const token = `synthetic-execution-token-${protocol}`;
    const server = createServer(async (req, res) => {
      let raw = ""; for await (const chunk of req) raw += chunk;
      const packet = JSON.parse(raw); packets.push(packet);
      seen.push({ url: req.url!, headers: req.headers });
      if (protocol === "chat_completions" && packets.length === 1) {
        res.writeHead(429, { "content-type": "application/json", "retry-after": "0" });
        res.end('{"error":{"message":"synthetic gateway retry once"}}'); return;
      }
      const strings = allStrings(packet);
      const file = strings.map(text => /absolute path: ("(?:[^"\\]|\\.)*")/u.exec(text)?.[1]).find(Boolean);
      const tool = packets.length === (protocol === "chat_completions" ? 2 : 1) && file ? JSON.parse(file) : undefined;
      reply(res, protocol, tool, undefined, protocol === "chat_completions" && packets.length === 2);
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    let launches = 0;
    const specs: AdvisorEngineSpec[] = [];
    const observedStatus: string[] = [];
    const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state", protocol),
      startEngine: spec => { launches++; specs.push(spec); return startAdvisorEngine(spec); } });
    try {
      const service: EngineService = { id: "svc-1", protocol, model: "synthetic", contextTokens: 65536,
        url: `http://127.0.0.1:${(server.address() as { port: number }).port}/s/svc-1` };
      const request = { account: `synthetic-${protocol}`, user: "synthetic-person", agentId: "advisor", threadId: "synthetic-thread",
        message: "Offer a concise synthetic answer.", system: "Synthetic test", context: "m1 OTHER: synthetic-evidence-canary.", contextRevision: 1 };
      const response = await runtime.respond(service, token, request, event => { if (event.state) observedStatus.push(event.state); });
      assert.match(response.text, /Synthetic complete answer/u, protocol);
      assert.equal(packets.length, protocol === "chat_completions" ? 4 : 2, `${protocol} read tool and final request`);
      assert(packets.slice(1).some(packet => JSON.stringify(packet).includes("synthetic-evidence-canary")), `${protocol} actual read result`);
      assert(JSON.stringify(packets[0]).includes('"read"'), `${protocol} actual tools`);
      const secret = String(Object.values(specs[0].config.provider!)[0]!.options!.apiKey);
      for (const entry of seen) {
        const step = /^\/s\/svc-1~([a-z-]+)(\/.*)$/u.exec(entry.url);
        assert(step, `${protocol} ${entry.url} goes to the service with a step`);
        assert.equal(step[1], "advisor", entry.url);
        assert.match(step[2], PROTOCOL_PATH[protocol], entry.url);
        const presented = String(entry.headers[TOKEN_HEADER[protocol]]).replace(/^Bearer /u, "");
        assert.equal(presented, token, `${protocol} token in ${TOKEN_HEADER[protocol]}`);
        assert(!JSON.stringify(entry).includes(secret), "the relay secret never leaves the relay");
        assert(!Object.keys(entry.headers).some(key => key.startsWith("x-kyf-") || key.startsWith("x-opencode-")));
      }
      assert(!JSON.stringify([specs[0].env, specs[0].config]).includes(token), "no token in opencode's config or environment");
      if (protocol === "chat_completions") {
        assert(observedStatus.includes("compacting") || observedStatus.includes("context-compacted"),
          `native automatic compaction ran inside the bound turn; states=${observedStatus.join(",")}`);
        assert.equal(launches, 1);
        const before = seen.length;
        const compact = await runtime.compactSession(service, "synthetic-compaction-token",
          { ...request, runtimeSessionId: response.runtimeSessionId, operationId: "synthetic-operation" });
        assert.equal(compact.runtimeSessionId, response.runtimeSessionId);
        assert.equal(compact.state, "completed");
        assert.equal(launches, 1, "manual native compaction reuses the same engine");
        assert(seen.length > before);
        for (const entry of seen.slice(before)) {
          assert.match(entry.url, /^\/s\/svc-1~discussion-compaction\/chat\/completions$/u);
          assert.equal(entry.headers.authorization, "Bearer synthetic-compaction-token");
        }
      }
      for (const entry of await readdir(root, { recursive: true, withFileTypes: true })) if (entry.isFile() && /\.log$/u.test(entry.name)) {
        const log = await readFile(path.join(entry.parentPath, entry.name), "utf8");
        assert(!log.includes(token), "execution token absent from ordinary engine logs");
        assert(!log.includes("synthetic-evidence-canary"), "conversation content absent from ordinary engine logs");
      }
      t.diagnostic(`${protocol}: ${packets.length} gateway requests, each with the execution token under its step path`);
    } finally { await runtime.close(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
  }
});
