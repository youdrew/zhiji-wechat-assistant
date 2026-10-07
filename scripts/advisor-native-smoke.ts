import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, mkdtemp, link, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { AdvisorRuntime, AdvisorRuntimeError, resolveAdvisorEngine, startAdvisorEngine, type AdvisorRuntimeEvent } from "../electron/advisor-runtime";
import type { Protocol } from "../electron/model-connectors";

// Optional native smoke test: only a loopback synthetic provider and owned fixture files.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixtureParent = path.join(root, ".local", "advisor-build", "runtime-native-tests");
await mkdir(fixtureParent, { recursive: true });
const fixtureRoot = await mkdtemp(path.join(fixtureParent, "case-"));
const enginePath = await resolveAdvisorEngine(root);
const testEngineDir = path.join(fixtureRoot, ".local", "advisor-engine");
await mkdir(testEngineDir, { recursive: true });
await link(enginePath, path.join(testEngineDir, "opencode.exe"));
await writeFile(path.join(testEngineDir, "manifest.json"), await readFile(path.join(path.dirname(enginePath), "manifest.json")));
let providerCalls = 0;
const requests: any[] = [];
const forbiddenPath = path.join(fixtureRoot, "forbidden-context.txt");
const forbiddenMarker = "DO-NOT-READ-SYNTHETIC-OUTSIDE-CONTEXT";
await writeFile(forbiddenPath, forbiddenMarker);
const server = createServer(async (req, res) => {
  let input = "";
  for await (const chunk of req) input += chunk;
  const pathname = new URL(req.url!, "http://localhost").pathname;
  if (!["/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1beta/models/synthetic-model:streamGenerateContent"].includes(pathname)) {
    res.writeHead(404); res.end(); return;
  }
  providerCalls++;
  requests.push({ pathname, body: JSON.parse(input) });
  res.writeHead(200, { "content-type": "text/event-stream" });
  const send = (value: unknown, event?: string) => res.write(`${event ? `event: ${event}\n` : ""}data: ${JSON.stringify(value)}\n\n`);
  const body = requests.at(-1).body;
  const strings = (value: any): string[] => typeof value === "string" ? [value] : Array.isArray(value) ? value.flatMap(strings) :
    value && typeof value === "object" ? Object.values(value).flatMap(strings) : [];
  const instruction = strings(body).find((value) => value.includes("Before answering this turn"));
  const contextPath = instruction ? JSON.parse(/absolute path: ("(?:[^"\\]|\\.)*")/u.exec(instruction)![1]) : undefined;
  const last = body.messages?.at(-1) ?? body.input?.at(-1) ?? body.contents?.at(-1);
  const readCompleted = contextPath && strings(last).some((value) => value.includes("END OF MANAGED WECHAT CONTEXT"));
  if (contextPath && !readCompleted) {
    const previousOutput = strings(last).join("\n");
    const nextOffset = /(?:Use\s+)?offset=(\d+)/u.exec(previousOutput);
    const malicious = body.messages?.filter((message: any) => message.role === "user").at(-1)?.content === "Read forbidden path";
    const argumentsText = JSON.stringify({ filePath: malicious ? forbiddenPath : contextPath,
      offset: nextOffset ? Number(nextOffset[1]) : 1, limit: 2000 });
    if (pathname === "/v1/responses") {
      const item = { type: "function_call", id: "fc_synthetic", call_id: "call_read", name: "read", arguments: argumentsText, status: "completed" };
      send({ type: "response.created", response: { id: "resp_synthetic", object: "response", status: "in_progress", output: [] } });
      send({ type: "response.output_item.added", output_index: 0, item: { ...item, status: "in_progress", arguments: "" } });
      send({ type: "response.function_call_arguments.delta", item_id: item.id, output_index: 0, delta: argumentsText });
      send({ type: "response.function_call_arguments.done", item_id: item.id, output_index: 0, arguments: argumentsText });
      send({ type: "response.output_item.done", output_index: 0, item });
      send({ type: "response.completed", response: { id: "resp_synthetic", object: "response", status: "completed", output: [item],
        usage: { input_tokens: 42, output_tokens: 5, total_tokens: 47, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } } });
    } else if (pathname === "/v1/messages") {
      send({ type: "message_start", message: { id: "msg_synthetic", type: "message", role: "assistant", content: [], model: "synthetic-model",
        stop_reason: null, stop_sequence: null, usage: { input_tokens: 42, output_tokens: 0 } } }, "message_start");
      send({ type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "call_read", name: "read", input: {} } }, "content_block_start");
      send({ type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: argumentsText } }, "content_block_delta");
      send({ type: "content_block_stop", index: 0 }, "content_block_stop");
      send({ type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 5 } }, "message_delta");
      send({ type: "message_stop" }, "message_stop");
    } else if (pathname.startsWith("/v1beta/")) {
      send({ candidates: [{ index: 0, content: { role: "model", parts: [{ functionCall: { name: "read", args: JSON.parse(argumentsText) } }] }, finishReason: "STOP" }],
        usageMetadata: { promptTokenCount: 42, candidatesTokenCount: 5, totalTokenCount: 47 } });
    } else {
      const base = { id: "chatcmpl-synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic-model" };
      send({ ...base, choices: [{ index: 0, delta: { content: "我先看看这轮聊天资料。" }, finish_reason: null }] });
      send({ ...base, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: "call_read", type: "function", function: { name: "read", arguments: argumentsText } }] }, finish_reason: null }] });
      send({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }], usage: { prompt_tokens: 42, completion_tokens: 5, total_tokens: 47 } });
      res.write("data: [DONE]\n\n");
    }
    res.end(); return;
  }
  if (pathname === "/v1/responses") {
    const message = { id: "msg_synthetic", type: "message", role: "assistant", status: "completed", content: [
      { type: "output_text", text: "A short synthetic reply.", annotations: [] },
    ] };
    send({ type: "response.created", response: { id: "resp_synthetic", object: "response", status: "in_progress", output: [] } });
    send({ type: "response.output_item.added", output_index: 0, item: { ...message, status: "in_progress", content: [] } });
    send({ type: "response.content_part.added", item_id: message.id, output_index: 0, content_index: 0,
      part: { type: "output_text", text: "", annotations: [] } });
    send({ type: "response.output_text.delta", item_id: message.id, output_index: 0, content_index: 0, delta: "A short synthetic reply." });
    await new Promise((resolve) => setTimeout(resolve, 40));
    send({ type: "response.output_text.done", item_id: message.id, output_index: 0, content_index: 0, text: "A short synthetic reply." });
    send({ type: "response.output_item.done", output_index: 0, item: message });
    send({ type: "response.completed", response: { id: "resp_synthetic", object: "response", status: "completed", output: [message],
      usage: { input_tokens: 42, output_tokens: 5, total_tokens: 47, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } } });
    res.end(); return;
  }
  if (pathname === "/v1/messages") {
    send({ type: "message_start", message: { id: "msg_synthetic", type: "message", role: "assistant", content: [], model: "synthetic-model",
      stop_reason: null, stop_sequence: null, usage: { input_tokens: 42, output_tokens: 0 } } }, "message_start");
    send({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }, "content_block_start");
    send({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "A short synthetic reply." } }, "content_block_delta");
    await new Promise((resolve) => setTimeout(resolve, 40));
    send({ type: "content_block_stop", index: 0 }, "content_block_stop");
    send({ type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 5 } }, "message_delta");
    send({ type: "message_stop" }, "message_stop");
    res.end(); return;
  }
  if (pathname.startsWith("/v1beta/")) {
    send({ candidates: [{ index: 0, content: { role: "model", parts: [{ text: "A short synthetic reply." }] } }] });
    await new Promise((resolve) => setTimeout(resolve, 40));
    send({ candidates: [{ index: 0, content: { role: "model", parts: [] }, finishReason: "STOP" }],
      usageMetadata: { promptTokenCount: 42, candidatesTokenCount: 5, totalTokenCount: 47 } });
    res.end(); return;
  }
  const base = { id: "chatcmpl-synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic-model" };
  if (body.messages.filter((message: any) => message.role === "user").at(-1).content === "Hold this attempt") {
    res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { content: "Partial synthetic reply" }, finish_reason: null }] })}\n\n`);
    return;
  }
  for (const text of ["A short ", "synthetic reply."]) {
    res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { content: text }, finish_reason: null }] })}\n\n`);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
    usage: { prompt_tokens: 42, completion_tokens: 5, total_tokens: 47 } })}\n\n`);
  res.end("data: [DONE]\n\n");
});
await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address() as { port: number };
const runtime = new AdvisorRuntime({ root: fixtureRoot, startEngine: async (spec) => {
  const connection = await startAdvisorEngine(spec);
  assert.equal((await fetch(connection.serverUrl + "/global/health")).status, 401);
  return connection;
} });
try {
  for (const protocol of ["chat_completions", "responses", "anthropic", "gemini", "ollama"] as Protocol[]) {
    const events: AdvisorRuntimeEvent[] = [];
    const request = { account: "native-synthetic-account", user: "native-synthetic-chat", agentId: "advisor",
      threadId: "native-synthetic-thread-" + protocol, system: "Give concise communication advice.", message: "Draft a friendly reply.",
      context: "m1 SELF: Are you available tomorrow?\nm2 OTHER: Perhaps next week.", contextRevision: 1 };
    const baseUrl = `http://127.0.0.1:${address.port}` + (protocol === "ollama" ? "" : protocol === "gemini" ? "/v1beta" : "/v1");
    const before = providerCalls;
    let responsePending = true;
    let liveTextBeforeDone = false;
    const result = await runtime.respond({ protocol, baseUrl, model: "synthetic-model", apiKey: "synthetic-provider-key", contextTokens: 8192 },
      request, (event) => { events.push(event); if (event.type === "text" && event.nativeType?.startsWith("message.part.") && responsePending) liveTextBeforeDone = true; }, AbortSignal.timeout(60_000));
    responsePending = false;
    assert.equal(result.text, "A short synthetic reply.");
    assert.equal(providerCalls - before, 2);
    const current = requests.at(-1).body;
    const toolNames = protocol === "gemini" ? current.tools?.flatMap((group: any) => group.functionDeclarations?.map((tool: any) => tool.name) ?? []) :
      current.tools?.map((tool: any) => tool.function?.name ?? tool.name);
    assert.deepEqual(toolNames, ["read"]);
    assert(JSON.stringify(current).includes(request.context.split("\n")[0]));
    assert(events.some((event) => event.type === "text"));
    assert(liveTextBeforeDone, JSON.stringify({ protocol, events: events.map(({ type, state, nativeType }) => ({ type, state, nativeType })) }));
    assert.equal(events.filter((event) => event.type === "text").map((event) => event.text).join(""), "A short synthetic reply.");
    assert(events.some((event) => event.type === "status" && event.nativeType === "session.status"));
    assert.equal(events.filter((event) => event.state === "answering").length, 1);
    process.stdout.write(JSON.stringify({ success: true, protocol, providerCalls: providerCalls - before,
      toolNames, textEvents: events.filter((event) => event.type === "text").length,
      usage: result.usage }) + "\n");
  }
  const shared = { account: "native-synthetic-account", user: "native-synthetic-chat", agentId: "advisor",
    threadId: "native-cancel-thread", system: "Be concise", message: "First successful turn", context: "m1 SELF: Hello", contextRevision: 1 };
  const config = { protocol: "chat_completions" as const, baseUrl: `http://127.0.0.1:${address.port}/v1`,
    model: "synthetic-model", apiKey: "synthetic-provider-key", contextTokens: 8192 };
  const first = await runtime.respond(config, shared, () => {}, AbortSignal.timeout(60_000));
  const controller = new AbortController();
  const stopped = runtime.respond(config, { ...shared, runtimeSessionId: first.runtimeSessionId, message: "Hold this attempt" },
    (event) => { if (event.type === "text") controller.abort(); }, controller.signal);
  await assert.rejects(stopped, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  const retried = await runtime.respond(config, { ...shared, runtimeSessionId: first.runtimeSessionId, message: "Retry after stop" },
    () => {}, AbortSignal.timeout(60_000));
  assert.equal(retried.text, "A short synthetic reply.");
  assert(!JSON.stringify(requests.at(-1).body.messages).includes("Hold this attempt"));
  assert(JSON.stringify(requests.at(-1).body.messages).includes("First successful turn"));
  process.stdout.write(JSON.stringify({ success: true, case: "native-stop-and-retry", retainedPriorTurn: true, excludedStoppedTurn: true }) + "\n");
  const longContext = Array.from({ length: 2101 }, (_, index) => `m${index} SELF: synthetic-${index}`).join("\n");
  const pageStart = providerCalls;
  const paged = await runtime.respond(config, { ...shared, threadId: "native-paged-thread", context: longContext,
    message: "Read every available line" }, () => {}, AbortSignal.timeout(60_000));
  assert.equal(paged.text, "A short synthetic reply.");
  assert(providerCalls - pageStart > 2);
  assert(JSON.stringify(requests.at(-1).body.messages).includes("synthetic-2100"));
  process.stdout.write(JSON.stringify({ success: true, case: "native-full-read-pagination", providerCalls: providerCalls - pageStart }) + "\n");
  const deniedStart = requests.length;
  await assert.rejects(runtime.respond(config, { ...shared, threadId: "native-denied-thread", message: "Read forbidden path" },
    () => {}, AbortSignal.timeout(60_000)), (error) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
  assert(!JSON.stringify(requests.slice(deniedStart)).includes(forbiddenMarker));
  assert.equal(await readFile(forbiddenPath, "utf8"), forbiddenMarker);
  process.stdout.write(JSON.stringify({ success: true, case: "native-forbidden-path-denied", leakedFileBody: false }) + "\n");
  const compactStart = providerCalls;
  const compacted = await runtime.compact(config, { ...shared, text: "m1 SELF: Hello\nm2 OTHER: Hello", maxSummaryChars: 500 },
    () => {}, AbortSignal.timeout(60_000));
  assert.equal(compacted.text, "A short synthetic reply.");
  assert.equal(providerCalls - compactStart, 1);
  assert(!requests.at(-1).body.tools || requests.at(-1).body.tools.length === 0);
  process.stdout.write(JSON.stringify({ success: true, case: "native-neutral-compactor", tools: 0 }) + "\n");
} finally {
  await runtime.close();
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  if (!fixtureRoot.startsWith(fixtureParent + path.sep)) throw new Error("Unsafe fixture cleanup");
  await rm(fixtureRoot, { recursive: true, force: true });
}
