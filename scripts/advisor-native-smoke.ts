// Modified by Know Your Friends, 2026-10-10 (Apache-2.0): synthetic native usage, history, and compaction verification.
// Modified by Know Your Friends, 2026-10-11 (Apache-2.0): answers without thinking.
// Modified by Know Your Friends, 2026-10-13 (Apache-2.0): the engine moved into plugins/kyf.advisor; the synthetic
// provider now stands in for the Core gateway (/s/<service>~<step>/...) and checks the execution token of every request.
// Modified by Know Your Friends, 2026-10-16 (Apache-2.0): flexible summary targets and summary-only no-thinking policy.
// KYF_ENGINE_ROOT names another checkout whose .local/advisor-engine holds the pinned engine.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, mkdtemp, link, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { AdvisorRuntime, AdvisorRuntimeError, advisorOutputBudget, resolveAdvisorEngine, startAdvisorEngine, type AdvisorEngineConnection,
  type AdvisorRuntimeEvent, type EngineService } from "../plugins/kyf.advisor/engine/runtime";
import type { Protocol } from "../electron/model-connectors";

// Optional native smoke test: only a loopback synthetic provider and owned fixture files.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixtureParent = path.join(root, ".local", "advisor-build", "runtime-native-tests");
await mkdir(fixtureParent, { recursive: true });
const fixtureRoot = await mkdtemp(path.join(fixtureParent, "case-"));
const enginePath = await resolveAdvisorEngine(path.resolve(process.env.KYF_ENGINE_ROOT ?? root));
const testEngineDir = path.join(fixtureRoot, ".local", "advisor-engine");
await mkdir(testEngineDir, { recursive: true });
await link(enginePath, path.join(testEngineDir, "opencode.exe"));
await writeFile(path.join(testEngineDir, "manifest.json"), await readFile(path.join(path.dirname(enginePath), "manifest.json")));
let providerCalls = 0;
let inputUsage = 42;
let failSummary = false;
let rejectTemplateField = false;
let holdSummary: (() => void) | undefined;
const requests: any[] = [];
const token = "synthetic-execution-token";
const steps = new Set<string>();
const forbiddenPath = path.join(fixtureRoot, "forbidden-context.txt");
const forbiddenMarker = "DO-NOT-READ-SYNTHETIC-OUTSIDE-CONTEXT";
await writeFile(forbiddenPath, forbiddenMarker);
const server = createServer(async (req, res) => {
  let input = "";
  for await (const chunk of req) input += chunk;
  // The gateway's view: /s/<service>~<step>/<protocol path>, the execution token in the protocol's key header.
  const routed = /^\/s\/svc~([a-z-]+)(\/.*)$/u.exec(new URL(req.url!, "http://localhost").pathname);
  const presented = String(req.headers.authorization ?? req.headers["x-api-key"] ?? req.headers["x-goog-api-key"] ?? "").replace(/^Bearer /u, "");
  if (!routed || presented !== token || Object.keys(req.headers).some(key => key.startsWith("x-opencode-") || key.startsWith("x-kyf-"))) {
    res.writeHead(401); res.end(); return;
  }
  steps.add(routed[1]);
  const pathname = routed[2].startsWith("/v1/") ? routed[2] : routed[2].startsWith("/models/") ? "/v1beta" + routed[2] : "/v1" + routed[2];
  if (!["/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1beta/models/synthetic-model:streamGenerateContent"].includes(pathname)) {
    res.writeHead(404); res.end(); return;
  }
  providerCalls++;
  requests.push({ pathname, step: routed[1], body: JSON.parse(input) });
  const body = requests.at(-1).body;
  const strings = (value: any): string[] => typeof value === "string" ? [value] : Array.isArray(value) ? value.flatMap(strings) :
    value && typeof value === "object" ? Object.values(value).flatMap(strings) : [];
  const discussionSummary = strings(body).some(value => value.includes("Summarize this adviser discussion for faithful continuation."));
  if (rejectTemplateField && body.chat_template_kwargs) {
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "Unrecognized request argument supplied: chat_template_kwargs", type: "invalid_request_error" } })); return;
  }
  if (discussionSummary && failSummary) {
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "Synthetic summary failure", type: "invalid_request_error" } })); return;
  }
  res.writeHead(200, { "content-type": "text/event-stream" });
  const send = (value: unknown, event?: string) => res.write(`${event ? `event: ${event}\n` : ""}data: ${JSON.stringify(value)}\n\n`);
  if (discussionSummary && holdSummary) { res.write(": keep-alive\n\n"); holdSummary(); return; }
  const instruction = strings(body).find((value) => value.includes("Before answering this turn"));
  const contextPath = instruction ? JSON.parse(/absolute path: ("(?:[^"\\]|\\.)*")/u.exec(instruction)![1]) : undefined;
  // Tool results can also have role=user (Anthropic/Gemini); select only ordinary text parts.
  const userTexts = (body.messages ?? body.input ?? body.contents ?? []).filter((message: any) => message.role === "user")
    .flatMap((message: any) => typeof message.content === "string" ? [message.content] :
      Array.isArray(message.content) ? message.content.filter((part: any) => ["text", "input_text"].includes(part.type)).map((part: any) => part.text) :
      (message.parts ?? []).filter((part: any) => typeof part.text === "string").map((part: any) => part.text));
  const userText = userTexts.at(-1) ?? "";
  if (contextPath && userText) assert(userText.startsWith("[来源：用户与助手的讨论；不是微信聊天记录]\n"),
    "the native provider must receive the adviser discussion source label");
  const last = body.messages?.at(-1) ?? body.input?.at(-1) ?? body.contents?.at(-1);
  const readCompleted = contextPath && strings(last).some((value) => value.includes("END OF MANAGED WECHAT CONTEXT"));
  if (contextPath && !readCompleted) {
    const previousOutput = strings(last).join("\n");
    const nextOffset = /(?:Use\s+)?offset=(\d+)/u.exec(previousOutput);
    const malicious = userText.endsWith("\nRead forbidden path");
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
        usage: { input_tokens: 42, output_tokens: 5, total_tokens: 47, input_tokens_details: { cached_tokens: 7 }, output_tokens_details: { reasoning_tokens: 0 } } } });
    } else if (pathname === "/v1/messages") {
      send({ type: "message_start", message: { id: "msg_synthetic", type: "message", role: "assistant", content: [], model: "synthetic-model",
        stop_reason: null, stop_sequence: null, usage: { input_tokens: 42, output_tokens: 0, cache_read_input_tokens: 7, cache_creation_input_tokens: 3 } } }, "message_start");
      send({ type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "call_read", name: "read", input: {} } }, "content_block_start");
      send({ type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: argumentsText } }, "content_block_delta");
      send({ type: "content_block_stop", index: 0 }, "content_block_stop");
      send({ type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 5 } }, "message_delta");
      send({ type: "message_stop" }, "message_stop");
    } else if (pathname.startsWith("/v1beta/")) {
      send({ candidates: [{ index: 0, content: { role: "model", parts: [{ functionCall: { name: "read", args: JSON.parse(argumentsText) } }] }, finishReason: "STOP" }],
        usageMetadata: { promptTokenCount: 42, cachedContentTokenCount: 7, candidatesTokenCount: 5, totalTokenCount: 47 } });
    } else {
      const base = { id: "chatcmpl-synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic-model" };
      send({ ...base, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: "call_read", type: "function", function: { name: "read", arguments: argumentsText } }] }, finish_reason: null }] });
      send({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }], usage: { prompt_tokens: 42, completion_tokens: 5, total_tokens: 47, prompt_tokens_details: { cached_tokens: 7 } } });
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
      usage: { input_tokens: 42, output_tokens: 5, total_tokens: 47, input_tokens_details: { cached_tokens: 7 }, output_tokens_details: { reasoning_tokens: 0 } } } });
    res.end(); return;
  }
  if (pathname === "/v1/messages") {
    send({ type: "message_start", message: { id: "msg_synthetic", type: "message", role: "assistant", content: [], model: "synthetic-model",
      stop_reason: null, stop_sequence: null, usage: { input_tokens: 42, output_tokens: 0, cache_read_input_tokens: 7, cache_creation_input_tokens: 3 } } }, "message_start");
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
      usageMetadata: { promptTokenCount: 42, cachedContentTokenCount: 7, candidatesTokenCount: 5, totalTokenCount: 47 } });
    res.end(); return;
  }
  const base = { id: "chatcmpl-synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic-model" };
  if (userText.endsWith("\nHold this attempt")) {
    res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { content: "Partial synthetic reply" }, finish_reason: null }] })}\n\n`);
    return;
  }
  for (const text of ["A short ", "synthetic reply."]) {
    res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { content: text }, finish_reason: null }] })}\n\n`);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  const reportedInput = inputUsage;
  inputUsage = 42;
  res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
    usage: { prompt_tokens: reportedInput, completion_tokens: 5, total_tokens: reportedInput + 5, prompt_tokens_details: { cached_tokens: 7 } } })}\n\n`);
  res.end("data: [DONE]\n\n");
});
await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address() as { port: number };
let nativeConnection: AdvisorEngineConnection | undefined;
let nativeDirectory: string | undefined;
const gatewayUrl = `http://127.0.0.1:${address.port}/s/svc`;
const runtime = new AdvisorRuntime({ root: fixtureRoot, stateRoot: path.join(fixtureRoot, "state"), startEngine: async (spec) => {
  assert(!JSON.stringify([spec.env, spec.config]).includes(token), "opencode never holds the execution token");
  const connection = await startAdvisorEngine(spec);
  nativeConnection = connection;
  nativeDirectory = spec.cwd;
  assert.equal((await fetch(connection.serverUrl + "/global/health")).status, 401);
  return connection;
} });
try {
  for (const protocol of ["chat_completions", "responses", "anthropic", "gemini", "ollama"] as Protocol[]) {
    const events: AdvisorRuntimeEvent[] = [];
    const request = { account: "native-synthetic-account", user: "native-synthetic-chat", agentId: "advisor",
      threadId: "native-synthetic-thread-" + protocol, system: "Give concise communication advice.", message: "Draft a friendly reply.",
      context: "m1 SELF: Are you available tomorrow?\nm2 OTHER: Perhaps next week.", contextRevision: 1 };
    const before = providerCalls;
    let responsePending = true;
    let liveTextBeforeDone = false;
    const result = await runtime.respond({ id: "svc", protocol, url: gatewayUrl, model: "synthetic-model", contextTokens: 8192 }, token,
      request, (event) => { events.push(event); if (event.type === "text" && event.nativeType?.startsWith("message.part.") && responsePending) liveTextBeforeDone = true; }, AbortSignal.timeout(60_000));
    responsePending = false;
    assert.equal(result.text, "A short synthetic reply.");
    assert.equal(result.usage?.totalInputTokens, protocol === "anthropic" ? 52 : 42, protocol + " normalized input");
    assert.equal(result.usage?.cacheReadTokens, 7, protocol + " cache read");
    assert.equal(result.usage?.cacheWriteTokens, protocol === "anthropic" ? 3 : 0, protocol + " cache write");
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
  const config: EngineService = { id: "svc", protocol: "chat_completions", url: gatewayUrl, model: "synthetic-model", contextTokens: 8192 };
  const first = await runtime.respond(config, token, shared, () => {}, AbortSignal.timeout(60_000));
  const controller = new AbortController();
  const stopped = runtime.respond(config, token, { ...shared, runtimeSessionId: first.runtimeSessionId, message: "Hold this attempt" },
    (event) => { if (event.type === "text") controller.abort(); }, controller.signal);
  await assert.rejects(stopped, (error) => error instanceof AdvisorRuntimeError && error.code === "cancelled");
  const retried = await runtime.respond(config, token, { ...shared, runtimeSessionId: first.runtimeSessionId, message: "Retry after stop" },
    () => {}, AbortSignal.timeout(60_000));
  assert.equal(retried.text, "A short synthetic reply.");
  assert(!JSON.stringify(requests.at(-1).body.messages).includes("Hold this attempt"));
  assert(JSON.stringify(requests.at(-1).body.messages).includes("First successful turn"));
  process.stdout.write(JSON.stringify({ success: true, case: "native-stop-and-retry", retainedPriorTurn: true, excludedStoppedTurn: true }) + "\n");
  const longContext = Array.from({ length: 2101 }, (_, index) => `m${index} SELF: synthetic-${index}`).join("\n");
  const pageStart = providerCalls;
  const paged = await runtime.respond({ ...config, contextTokens: 262144 }, token, { ...shared, threadId: "native-paged-thread", context: longContext,
    message: "Read every available line" }, () => {}, AbortSignal.timeout(60_000));
  assert.equal(paged.text, "A short synthetic reply.");
  assert(providerCalls - pageStart > 2);
  assert(JSON.stringify(requests.at(-1).body.messages).includes("synthetic-2100"));
  process.stdout.write(JSON.stringify({ success: true, case: "native-full-read-pagination", providerCalls: providerCalls - pageStart }) + "\n");
  const deniedStart = requests.length;
  await assert.rejects(runtime.respond(config, token, { ...shared, threadId: "native-denied-thread", message: "Read forbidden path" },
    () => {}, AbortSignal.timeout(60_000)), (error) => error instanceof AdvisorRuntimeError && error.code === "permission-denied");
  assert(!JSON.stringify(requests.slice(deniedStart)).includes(forbiddenMarker));
  assert.equal(await readFile(forbiddenPath, "utf8"), forbiddenMarker);
  process.stdout.write(JSON.stringify({ success: true, case: "native-forbidden-path-denied", leakedFileBody: false }) + "\n");
  const compactStart = providerCalls;
  const compacted = await runtime.compact(config, token, { ...shared, text: "m1 SELF: Hello\nm2 OTHER: Hello", targetSummaryChars: 500 },
    () => {}, AbortSignal.timeout(60_000));
  assert.equal(compacted.text, "A short synthetic reply.");
  assert.equal(providerCalls - compactStart, 1);
  assert(!requests.at(-1).body.tools || requests.at(-1).body.tools.length === 0);
  // OpenCode appends an assistant "maximum steps" notice on an agent's last step;
  // after it, models stop at once or summarize nothing. The records must come last.
  assert.equal(requests.at(-1).body.messages.at(-1).role, "user", "compactor must not get the last-step notice");
  const discussed = await runtime.summarizeDiscussion(config, token, { ...shared, text: "Q: synthetic goal\nA: synthetic suggestion",
    targetSummaryChars: 500 }, () => {}, AbortSignal.timeout(60_000));
  assert.equal(discussed.text, "A short synthetic reply.");
  assert.equal(requests.at(-1).body.messages.at(-1).role, "user", "discussion compactor must not get the last-step notice");
  process.stdout.write(JSON.stringify({ success: true, case: "native-neutral-compactor", tools: 0 }) + "\n");
  assert(requests.filter(item => item.step === "advisor").every(item => item.body.chat_template_kwargs === undefined),
    "formal answers keep the server default unless turned off");
  for (const item of requests.filter(item => ["wechat-summary", "discussion-summary"].includes(item.step))) {
    assert.deepEqual(item.body.chat_template_kwargs, { thinking: false, enable_thinking: false }, "summaries disable thinking");
  }
  const quickStart = requests.length;
  const quick = await runtime.respond(config, token, { ...shared, threadId: "native-no-thinking-thread", thinking: false }, () => {}, AbortSignal.timeout(60_000));
  assert.equal(quick.text, "A short synthetic reply.");
  await runtime.compact(config, token, { ...shared, text: "m1 SELF: Hello", targetSummaryChars: 500, thinking: false }, () => {}, AbortSignal.timeout(60_000));
  assert.equal(requests.length - quickStart, 3);
  for (const item of requests.slice(quickStart)) assert.deepEqual(item.body.chat_template_kwargs, { thinking: false, enable_thinking: false });
  rejectTemplateField = true;
  await assert.rejects(runtime.respond(config, token, { ...shared, threadId: "native-no-template-thread", thinking: false }, () => {}, AbortSignal.timeout(60_000)),
    (error) => error instanceof AdvisorRuntimeError && error.code === "thinking-unsupported");
  rejectTemplateField = false;
  process.stdout.write(JSON.stringify({ success: true, case: "native-no-thinking-variant", requests: 3 }) + "\n");
  let sessionId: string | undefined;
  const tenTurn = { ...shared, threadId: "native-dedup-thread", context: "UNIQUE-SYNTHETIC-MATERIAL-ONCE: keep the shared appointment uncertain.", contextEpoch: 1, profileStamp: "p1" };
  const roomy: EngineService = { ...config, contextTokens: 32768 };
  for (let turn = 0; turn < 10; turn++) {
    const result = await runtime.respond(roomy, token, { ...tenTurn, runtimeSessionId: sessionId, message: `Synthetic question ${turn}` }, () => {}, AbortSignal.timeout(60000));
    sessionId = result.runtimeSessionId;
    const effective = JSON.stringify(requests.at(-1).body);
    assert.equal(effective.split("UNIQUE-SYNTHETIC-MATERIAL-ONCE").length - 1, 1, `turn ${turn}: repeated evidence`);
    assert(Buffer.byteLength(effective, "utf8") < roomy.contextTokens! - advisorOutputBudget(roomy.contextTokens!),
      `turn ${turn}: captured complete provider request byte bound exceeds input budget`);
    assert.equal(result.compactions?.length, 0, "roomy synthetic history should not auto compact");
  }
  process.stdout.write(JSON.stringify({ success: true, case: "native-ten-turn-effective-input-dedup", turns: 10 }) + "\n");
  const manualRequest = { ...tenTurn, runtimeSessionId: sessionId, operationId: "synthetic-manual-1", contextVersion: 10 };
  const manualBefore = providerCalls;
  const manual = await runtime.compactSession(roomy, token, manualRequest, () => {}, AbortSignal.timeout(60000));
  assert.equal(manual.state, "completed", JSON.stringify(manual));
  assert.equal(manual.summary, "A short synthetic reply.");
  assert.equal(providerCalls - manualBefore, 1, "manual compaction must not continue answering");
  const duplicate = await runtime.compactSession(roomy, token, manualRequest);
  assert.equal(duplicate.state, "completed");
  assert.equal(providerCalls - manualBefore, 1, "operation replay must not call model");
  const resumed = await runtime.respond(roomy, token, { ...tenTurn, runtimeSessionId: sessionId, message: "Continue after compaction" });
  assert.equal(resumed.runtimeSessionId, sessionId);
  assert(!JSON.stringify(requests.at(-1).body).includes("Synthetic question 0"));
  const modelChanged = await runtime.respond({ ...roomy, model: "synthetic-other" }, token, { ...tenTurn, runtimeSessionId: sessionId, message: "Continue on another model" });
  assert.equal(modelChanged.runtimeSessionId, sessionId);
  const protocolChanged = await runtime.respond({ ...roomy, protocol: "anthropic" }, token, { ...tenTurn, runtimeSessionId: sessionId, message: "Continue via another protocol" });
  assert.equal(protocolChanged.runtimeSessionId, sessionId);
  const deleted = await runtime.deleteSession(roomy, manualRequest);
  assert.equal(deleted.state, "completed");
  assert.equal((await runtime.deleteSession(roomy, manualRequest)).state, "completed");
  assert.equal((await runtime.inspectSession(roomy, manualRequest)).state, "missing");
  process.stdout.write(JSON.stringify({ success: true, case: "native-manual-compaction-replay-model-reuse-delete", noAutomaticAnswer: true }) + "\n");
  const autoRequest = { ...shared, threadId: "native-auto-thread", contextVersion: "auto-version-1" };
  inputUsage = 7000;
  const beforeAuto = await runtime.respond(config, token, autoRequest, () => {}, AbortSignal.timeout(60000));
  inputUsage = 42;
  const auto = await runtime.respond(config, token, { ...autoRequest, runtimeSessionId: beforeAuto.runtimeSessionId, message: "A later synthetic question" }, () => {}, AbortSignal.timeout(60000));
  assert(auto.compactions?.some(item => item.kind === "automatic" && item.state === "completed" && item.contextVersion === "auto-version-1"));
  assert(requests.some(item => JSON.stringify(item.body).includes("unfinished tasks, constraints, uncertain assumptions")));
  const autoReadback = await runtime.inspectSession(config, { ...autoRequest, runtimeSessionId: auto.runtimeSessionId, contextVersion: "new-version" });
  assert.equal(autoReadback.compactions?.[0].contextVersion, "auto-version-1", "historical compaction version must remain fixed");
  process.stdout.write(JSON.stringify({ success: true, case: "native-automatic-discussion-compaction-record" }) + "\n");
  const failureRequest = { ...shared, threadId: "native-summary-failure", message: "Keep this original question" };
  const beforeFailure = await runtime.respond(roomy, token, failureRequest);
  failSummary = true;
  const failed = await runtime.compactSession(roomy, token, { ...failureRequest, runtimeSessionId: beforeFailure.runtimeSessionId, operationId: "fail-1" });
  failSummary = false;
  assert.equal(failed.state, "failed", JSON.stringify(failed));
  const afterFailure = await runtime.respond(roomy, token, { ...failureRequest, runtimeSessionId: beforeFailure.runtimeSessionId, message: "Continue after a failed summary" });
  assert.equal(afterFailure.text, "A short synthetic reply.");
  assert(JSON.stringify(requests.at(-1).body).includes("Keep this original question"), "failed compaction retains original discussion");
  const stopController = new AbortController();
  holdSummary = () => stopController.abort();
  const stoppedCompact = await runtime.compactSession(roomy, token, { ...failureRequest, runtimeSessionId: beforeFailure.runtimeSessionId, operationId: "stop-1" }, () => {}, stopController.signal);
  holdSummary = undefined;
  assert.equal(stoppedCompact.state, "stopped", JSON.stringify(stoppedCompact));
  assert(!stoppedCompact.compactions?.some(item => item.operationId === "stop-1" && item.state === "completed"));
  process.stdout.write(JSON.stringify({ success: true, case: "native-compaction-failure-and-stop-preserve-history" }) + "\n");
  // Model a lost bridge/HTTP waiter while the native summary continues. The
  // fixture owns this private engine; no production session API is exposed.
  const detachedScope = { ...shared, threadId: "native-detached-summary", message: "A detached synthetic compaction" };
  const detachedSeed = await runtime.respond(roomy, token, detachedScope);
  const detachedID = detachedSeed.runtimeSessionId;
  const native = nativeConnection!.client;
  const nativeSession = await native.session.get({ sessionID: detachedID, directory: nativeDirectory });
  const nativeHistory = await native.session.messages({ sessionID: detachedID, directory: nativeDirectory });
  await native.session.update({ sessionID: detachedID, directory: nativeDirectory,
    metadata: { ...nativeSession.data!.metadata, advisorCompactions: [{ id: "detached-operation", state: "unknown",
      beforeMessageId: nativeHistory.data!.at(-1)!.info.id, startedAt: Date.now() }] } });
  // A raw native call has no execution of its own: bind it as the lost compaction's would have been.
  (runtime as any).relay.bind(detachedID, { token, step: "discussion-compaction" });
  let summaryEntered!: () => void;
  const entered = new Promise<void>(resolve => { summaryEntered = resolve; });
  holdSummary = () => summaryEntered();
  const detachedController = new AbortController();
  const detachedHTTP = native.session.summarize({ sessionID: detachedID, directory: nativeDirectory,
    providerID: "wechatvibe-advisor", modelID: roomy.model, auto: false }, { signal: detachedController.signal }).catch(() => undefined);
  await entered;
  detachedController.abort();
  await detachedHTTP;
  const detachedRequest = { ...detachedScope, runtimeSessionId: detachedID, operationId: "detached-operation" };
  assert.equal((await runtime.inspectSession(roomy, detachedRequest)).state, "running");
  const detachedStop = await runtime.stopSessionOperation(roomy, detachedRequest);
  holdSummary = undefined;
  assert.equal(detachedStop.state, "stopped", JSON.stringify(detachedStop));
  assert.equal((await runtime.stopSessionOperation(roomy, detachedRequest)).state, "stopped");
  const afterDetachedStop = await runtime.respond(roomy, token, { ...detachedScope, runtimeSessionId: detachedID, message: "Continue after detached stop" });
  assert.equal(afterDetachedStop.runtimeSessionId, detachedID);
  assert(JSON.stringify(requests.at(-1).body).includes("A detached synthetic compaction"));
  process.stdout.write(JSON.stringify({ success: true, case: "native-detached-unknown-compaction-stop", retainedPriorDiscussion: true }) + "\n");
  assert(!requests.some(item => JSON.stringify(item.body).includes("MAXIMUM STEPS REACHED")), "no request may carry the last-step notice");
  assert.deepEqual([...steps].sort(), ["advisor", "discussion-compaction", "discussion-summary", "wechat-summary"]);
  process.stdout.write(JSON.stringify({ success: true, case: "gateway-steps-and-token", steps: [...steps].sort() }) + "\n");
} finally {
  await runtime.close();
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  if (!fixtureRoot.startsWith(fixtureParent + path.sep)) throw new Error("Unsafe fixture cleanup");
  await rm(fixtureRoot, { recursive: true, force: true });
}
