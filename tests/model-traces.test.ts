import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import { generateStructured } from "../electron/model-connectors";
import { beginModelTraceAttempt, captureModelResponse, configureModelTraceProducer, modelTraceBufferStats, modelTraceOutcome,
  currentModelTraceContext, currentModelTraceRecordId, TraceRedactor, traceProviderFetch,
  withModelTraceContext, withModelTraceStep, type ModelTraceContext, type ModelTraceState } from "../electron/model-traces";
import { ModelTraceRelay } from "../electron/model-trace-relay";

const context: ModelTraceContext = { producerId: "synthetic-producer", taskId: "task-one", executionId: "execution-one",
  accountId: "synthetic-account", contactId: "synthetic-person", source: "test", endpoint: "http://should-not-overwrite.invalid" };
function capture() {
  const events: any[] = [];
  const state = { collecting: true, collectionVersion: 1, deleteVersion: 1 };
  configureModelTraceProducer({ state: () => state, emit: (_context, event) => { events.push(event); return true; } });
  return { events, state };
}
function body(events: any[], kind: string) { return events.filter(event => event.type === "chunk" && event.kind === kind).map(event => event.text).join(""); }

test("final Request payload is captured once, with media/credentials removed before the producer", async () => {
  const { events } = capture();
  const secret = "synthetic/credential+long-value";
  const requestBody = JSON.stringify({ model: "synthetic", nested: { api_key: secret }, prompt: `hello ${secret}`,
    images: ["YWJjZA=="], remote: `https://name:demo@example.test/file?signature=${secret}` });
  let forwarded = "";
  await withModelTraceContext(context, async () => {
    const response = await traceProviderFetch(new Request("http://127.0.0.1:1234/v1/chat/completions", {
      method: "POST", body: requestBody, headers: { authorization: `Bearer ${secret}`, "content-type": "application/json" },
    }), undefined, { model: "synthetic", protocol: "chat_completions", endpoint: "http://127.0.0.1:1234/v1/chat/completions", method: "POST" },
    [secret], async (input, init) => { forwarded = await new Request(input, init).text();
      return new Response(JSON.stringify({ choices: [{ message: { content: "OK" } }], usage: { prompt_tokens: 9, completion_tokens: 2 } }),
        { headers: { "content-type": "application/json" } }); });
    await response.text();
  });
  assert.equal(forwarded, requestBody);
  assert.equal(events.filter(event => event.type === "start").length, 1);
  assert.equal(events[0].meta.endpoint, "http://127.0.0.1:1234/v1/chat/completions");
  assert(!JSON.stringify(events).includes(secret));
  assert(!body(events, "request").includes("YWJjZA=="));
  assert(!body(events, "request").includes("name:demo"));
  assert.deepEqual(events.at(-1).usage, { prompt_tokens: 9, completion_tokens: 2 });
  assert.equal(modelTraceBufferStats().workBytes, 0);
  configureModelTraceProducer();
});

test("known secret split over wire chunks and separate semantic deltas never reaches producer", async () => {
  const { events } = capture();
  const secret = "sk-synthetic-CREDENTIAL";
  const fragments = ["sk-syn", "thetic-", "CRED", "ENTIAL"];
  const raw = fragments.map(content => `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\n`).join("") +
    `data: ${JSON.stringify({ choices: [{ delta: {}, finish_reason: "stop" }], usage: { completion_tokens: 4 } })}\n\ndata: [DONE]\n\n`;
  let delivered = "";
  await withModelTraceContext(context, async () => {
    const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic", endpoint: "http://127.0.0.1/v1", method: "POST" }, [secret]);
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (let at = 0; at < raw.length; at += 7) controller.enqueue(new TextEncoder().encode(raw.slice(at, at + 7)));
      controller.close();
    } });
    delivered = await captureModelResponse(new Response(stream, { headers: { "content-type": "text/event-stream" } }), attempt).text();
  });
  assert.equal(delivered, raw);
  const stored = body(events, "response");
  for (const fragment of fragments) assert(!stored.includes(fragment), fragment);
  assert(stored.includes("已脱敏"));
  assert.equal(events.at(-1).transportStatus, "completed");
  assert.equal(modelTraceBufferStats().workBytes, 0);
  configureModelTraceProducer();
});

test("wire traces and the always-on meter use inclusive totals after partial usage deltas", async () => {
  const { events } = capture();
  try {
    const raw = [
      { type: "message_start", message: { usage: { input_tokens: 10, cache_read_input_tokens: 20,
        cache_creation_input_tokens: 3, output_tokens: 1 } } },
      { type: "message_delta", usage: { input_tokens: null, cache_read_input_tokens: null,
        cache_creation_input_tokens: null, output_tokens: 7 } },
      { type: "message_stop" },
    ].map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("");
    await withModelTraceContext(context, async () => {
      const attempt = beginModelTraceAttempt({ protocol: "anthropic", model: "synthetic",
        endpoint: "http://127.0.0.1/v1/messages", method: "POST" }, []);
      assert.equal(await captureModelResponse(new Response(raw, { headers: { "content-type": "text/event-stream" } }), attempt).text(), raw);
    });
    const usage = events.at(-1).normalizedUsage;
    assert.equal(usage.accountingVersion, 1);
    assert.equal(usage.inputTokens, 10);
    assert.equal(usage.outputTokens, 7);
    assert.equal(usage.totalInputTokens, 33);
    assert.equal(usage.totalOutputTokens, 7);
    assert.equal(usage.totalTokens, 40);
    assert.equal(usage.totalTokensSource, "derived");
  } finally { configureModelTraceProducer(); }
});

test("equivalent JSON and percent encodings plus signed URLs are redacted", () => {
  const redact = new TraceRedactor(["secret/key"]);
  for (const value of ["secret/key", "secret%2Fkey", "%73ecret%2fkey", "\\u0073ecret/key"])
    assert(!redact.text(value).includes(value), value);
  assert(!redact.text("https://u:p@test.invalid/?token=OTHER").includes("OTHER"));
});

test("interleaved semantic streams and the derived text projection redact independently", async t => {
  const secret = "sk-synthetic-CREDENTIAL", left = "sk-synthetic-", right = "CREDENTIAL";
  const chat = (index: number, delta: object) => ({ choices: [{ index, delta }] });
  const anthropic = (index: number, partial_json: string) => ({ type: "content_block_delta", index,
    delta: { type: "input_json_delta", partial_json } });
  const responses = (output_index: number, content_index: number, delta: string) => ({
    type: "response.output_text.delta", output_index, content_index, item_id: `item_${output_index}`, delta });
  const gemini = (index: number, parts: object[]) => ({ candidates: [{ index, content: { parts } }] });
  const cases = [
    { name: "chat content and reasoning", frames: [chat(0, { content: left }), chat(0, { reasoning_content: "unrelated" }), chat(0, { content: right })] },
    { name: "chat choice indexes", frames: [chat(0, { content: left }), chat(1, { content: "unrelated" }), chat(0, { content: right })] },
    { name: "chat tool call indexes", frames: [0, 1, 0].map((index, at) => chat(0, {
      tool_calls: [{ index, function: { arguments: [left, "unrelated", right][at] } }] })) },
    { name: "Anthropic block indexes", frames: [anthropic(0, left), anthropic(1, "unrelated"), anthropic(0, right)] },
    { name: "Responses item indexes", frames: [responses(0, 0, left), responses(1, 0, "unrelated"), responses(0, 0, right)] },
    { name: "Responses content indexes", frames: [responses(0, 0, left), responses(0, 1, "unrelated"), responses(0, 0, right)] },
    { name: "Gemini candidate indexes", frames: [gemini(0, [{ text: left }]), gemini(1, [{ text: "unrelated" }]), gemini(0, [{ text: right }])] },
    { name: "Gemini parts", frames: [gemini(0, [{ text: left }, { text: "unrelated" }]), gemini(0, [{ text: right }])] },
    { name: "visible projection excluding thought text", frames: [gemini(0, [{ text: left }]), gemini(0, [{ text: "unrelated", thought: true }]), gemini(0, [{ text: right }])] },
    { name: "encoded content with interleaved reasoning", frames: [chat(0, { content: "%73k-synthetic-" }), chat(0, { reasoning_content: "unrelated" }), chat(0, { content: right })] },
  ];
  for (const item of cases) await t.test(item.name, async () => {
    const { events } = capture();
    try {
      const raw = item.frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("") + "data: [DONE]\n\n";
      await withModelTraceContext(context, async () => {
        const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
          endpoint: "http://127.0.0.1/v1", method: "POST" }, [secret]);
        const stream = new ReadableStream<Uint8Array>({ start(controller) {
          for (let offset = 0; offset < raw.length; offset += 11)
            controller.enqueue(new TextEncoder().encode(raw.slice(offset, offset + 11)));
          controller.close();
        } });
        assert.equal(await captureModelResponse(new Response(stream,
          { headers: { "content-type": "text/event-stream" } }), attempt).text(), raw);
      });
      assert(!body(events, "response").includes(left), "a retained credential prefix must be masked");
      assert(!body(events, "response").includes(right), "a credential suffix must be masked");
      assert(!body(events, "text").includes(secret), "the derived visible projection must also be safe");
      assert(body(events, "response").includes("已脱敏"));
      assert.equal(modelTraceBufferStats().workBytes, 0);
    } finally { configureModelTraceProducer(); }
  });
});

test("an unresolved stream prefix cannot grow the interleaved queue beyond its shared budget", async () => {
  const { events } = capture();
  try {
    await withModelTraceContext(context, async () => {
      const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
        endpoint: "http://127.0.0.1/v1", method: "POST" }, ["sk-synthetic-CREDENTIAL"]);
      const prefix = `data: ${JSON.stringify({ choices: [{ delta: { content: "sk-synthetic-" } }] })}\n\n`;
      const unrelated = `data: ${JSON.stringify({ choices: [{ delta: { reasoning_content: "z".repeat(32768) } }] })}\n\n`;
      const raw = prefix + unrelated.repeat(80) + "data: [DONE]\n\n";
      assert.equal(await captureModelResponse(new Response(raw,
        { headers: { "content-type": "text/event-stream" } }), attempt).text(), raw);
    });
    assert(events.some(event => event.collectionStatus === "incomplete"));
    assert(!body(events, "response").includes("sk-synthetic-"));
    const stats = modelTraceBufferStats();
    assert.equal(stats.workBytes, 0);
    assert(stats.peakWorkBytes <= stats.limit);
  } finally { configureModelTraceProducer(); }
});

test("mixed credential encodings split across semantic deltas are redacted before publishing", async t => {
  t.after(() => configureModelTraceProducer());
  const secret = "sk-synthetic-CREDENTIAL";
  for (const fragments of [["%73k-syn", "thetic-", "CRED", "ENTIAL"],
    ["%", "73k-syn", "thetic-", "CRED", "ENTIAL"],
    ["\\u00", "73k-syn", "thetic-", "CRED", "ENTIAL"]]) {
    const { events } = capture();
    const raw = fragments.map(content => `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\n`).join("") +
      "data: [DONE]\n\n";
    await withModelTraceContext(context, async () => {
      const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
        endpoint: "http://127.0.0.1/v1", method: "POST" }, [secret]);
      assert.equal(await captureModelResponse(new Response(raw,
        { headers: { "content-type": "text/event-stream" } }), attempt).text(), raw);
    });
    const text = body(events, "text");
    assert(!text.includes("CREDENTIAL"), `credential suffix must be protected for ${fragments[0]}`);
    assert(text.includes("已脱敏"));
    assert.equal(modelTraceBufferStats().workBytes, 0);
  }
});

test("unexpected provider JSON shapes remain byte-for-byte available to the business parser", async t => {
  const { events } = capture();
  t.after(() => configureModelTraceProducer());
  for (const raw of ["null", "false", "17", '"provider error"', '{"content":{"filter":3}}',
    '{"output":[null,{"type":"message","content":[null]}]}']) {
    await withModelTraceContext(context, async () => {
      const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
        endpoint: "http://127.0.0.1/v1", method: "POST" })!;
      const response = captureModelResponse(new Response(raw, { headers: { "content-type": "application/json" } }), attempt);
      assert.equal(await response.text(), raw);
      assert.equal(events.at(-1).transportStatus, "completed");
    });
  }
  assert.equal(modelTraceBufferStats().workBytes, 0);
});

test("large request and response lexers redact sensitive values of every JSON type", async t => {
  const { events } = capture();
  t.after(() => configureModelTraceProducer());
  const raw = JSON.stringify({ padding: "a".repeat(140 * 1024), api_key: 123456789,
    authorization: { nested: "nested-private-value", secret: ["nested-array-secret"] },
    "x-kyf-relay-auth": [false, "relay-private-value"], safe: { text: "visible" } });
  await withModelTraceContext(context, async () => {
    const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
      endpoint: "http://127.0.0.1/v1", method: "POST" })!;
    attempt.request(raw, new Headers());
    assert.equal(await captureModelResponse(new Response(raw,
      { headers: { "content-type": "application/json" } }), attempt).text(), raw);
  });
  const request = JSON.parse(body(events, "request")).body, response = JSON.parse(body(events, "response"));
  for (const saved of [request, response]) {
    assert.equal(saved.api_key, "[已脱敏]");
    assert.equal(saved.authorization, "[已脱敏]");
    assert.equal(saved["x-kyf-relay-auth"], "[已脱敏]");
    assert.equal(saved.safe.text, "visible");
  }
  assert.equal(modelTraceBufferStats().workBytes, 0);
});

test("diagnostic result serialization failures cannot change business success", async t => {
  const { events } = capture();
  t.after(() => configureModelTraceProducer());
  await withModelTraceContext(context, async () => {
    const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
      endpoint: "http://127.0.0.1/v1", method: "POST" })!;
    await captureModelResponse(new Response('{"choices":[]}'), attempt).text();
    assert.doesNotThrow(() => attempt.outcome("success", { largeInteger: 1n }, "adopted"));
  });
  assert(body(events, "result").includes("应用结果无法安全序列化"));
  assert.equal(events.at(-1).applicationStatus, "success");
  assert.equal(events.at(-1).adoption, "adopted");
});

test("an internal diagnostic parser failure cannot interrupt response consumption or retain buffers", async t => {
  const { events } = capture();
  t.after(() => configureModelTraceProducer());
  const raw = '{"choices":[{"message":{"content":"business answer"}}]}';
  await withModelTraceContext(context, async () => {
    const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic",
      endpoint: "http://127.0.0.1/v1", method: "POST" })!;
    attempt.redactor.value = () => { throw new Error("synthetic parser fault"); };
    assert.equal(await captureModelResponse(new Response(raw), attempt).text(), raw);
  });
  assert(events.some(event => event.incomplete && event.incompleteReason.includes("诊断解析失败")));
  assert.equal(modelTraceBufferStats().workBytes, 0);
});

test("disabled capture avoids body inspection, stream interruption is not HTTP success", async () => {
  const { events, state } = capture(); state.collecting = false;
  let jsonCalls = 0;
  await withModelTraceContext(context, async () => {
    const response = await traceProviderFetch("http://127.0.0.1/v1", { method: "POST", body: "text" },
      { model: "synthetic", protocol: "chat_completions", endpoint: "http://127.0.0.1/v1", method: "POST" }, [],
      async () => { jsonCalls++; return new Response("ok"); });
    await response.text();
  });
  assert.equal(jsonCalls, 1); assert.equal(events.length, 0);
  state.collecting = true;
  await withModelTraceContext(context, async () => {
    const attempt = beginModelTraceAttempt({ model: "synthetic", protocol: "chat_completions", endpoint: "http://127.0.0.1/v1", method: "POST" });
    await captureModelResponse(new Response('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
      { headers: { "content-type": "text/event-stream" } }), attempt).text();
  });
  assert.equal(events.at(-1).transportStatus, "interrupted");
  configureModelTraceProducer();
});

test("compatibility fallback has distinct final requests and no synthetic stream attempt", async t => {
  const { events } = capture();
  const packets: any[] = [];
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    const packet = JSON.parse(raw); packets.push(packet);
    res.setHeader("content-type", "application/json");
    if (packets.length === 1) { res.statusCode = 400; res.end('{"error":{"message":"unsupported stream"}}'); }
    else res.end('{"choices":[{"message":{"content":"success"},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":1}}');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  const address = server.address() as { port: number };
  const result = await withModelTraceContext(context, () => generateStructured({ protocol: "chat_completions", model: "synthetic",
    baseUrl: `http://127.0.0.1:${address.port}/v1` }, { system: "Synthetic", prompt: "Hello", stream: true }));
  assert.equal(result.text, "success");
  const starts = events.filter(event => event.type === "start");
  assert.equal(starts.length, 2); assert.notEqual(starts[0].recordId, starts[1].recordId);
  const bodies = starts.map(start => JSON.parse(body(events.filter(event => event.recordId === start.recordId), "request")).body);
  assert.deepEqual(bodies, packets);
  assert.equal(starts[1].meta.attempt, 2);
});

test("relay auth, route bounds, concurrent scope and capture toggle preserve the same provider connection", async t => {
  const { events, state } = capture();
  const packets: Array<{ headers: any; body: any }> = [];
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    packets.push({ headers: req.headers, body: JSON.parse(raw) });
    res.setHeader("content-type", "text/event-stream");
    res.write('data: {"choices":[{"delta":{"content":"first"}}]}\n\n');
    setTimeout(() => res.end('data: {"choices":[{"delta":{"content":"last"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'), 30);
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const relay = await new ModelTraceRelay({ protocol: "chat_completions", model: "synthetic",
    baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`, apiKey: "provider-key" }, "1.18.34").start();
  t.after(async () => { await relay.close(); server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  assert.equal((await fetch(relay.baseUrl + "/chat/completions", { method: "POST", body: "{}" })).status, 403);
  assert.equal((await fetch(relay.baseUrl + "/chat/completions", { method: "POST", body: "{}",
    headers: { "x-kyf-relay-auth": "é".repeat(relay.auth.length) } })).status, 403);
  assert.equal((await fetch(relay.baseUrl + "/other", { method: "POST", body: "{}", headers: { "x-kyf-relay-auth": relay.auth } })).status, 403);
  await Promise.all(Array.from({ length: 10 }, async (_, i) => {
    relay.bind(`session-${i}`, { ...context, taskId: `task-${i}`, executionId: `execution-${i}`, contactId: `contact-${i}` }, `message-${i}`);
    const response = await fetch(relay.baseUrl + "/chat/completions", { method: "POST", body: JSON.stringify({ model: "synthetic", index: i }),
      headers: { "x-kyf-relay-auth": relay.auth, "x-kyf-session-id": `session-${i}`, "x-kyf-message-id": `message-${i}`, "x-kyf-step-id": `step-${i}` } });
    assert((await response.text()).includes("last"));
  }));
  assert.equal(packets.length, 10);
  assert(packets.every(packet => !Object.keys(packet.headers).some(key => key.startsWith("x-kyf-"))));
  const starts = events.filter(event => event.type === "start");
  assert.equal(new Set(starts.map(event => event.meta.contactId)).size, 10);
  for (const start of starts) {
    const request = JSON.parse(body(events.filter(event => event.recordId === start.recordId), "request"));
    assert.equal(start.meta.contactId, `contact-${request.body.index}`);
  }
  state.collecting = false; state.collectionVersion++;
  const oldEvents = events.length;
  const response = await fetch(relay.baseUrl + "/chat/completions", { method: "POST", body: '{"model":"synthetic"}',
    headers: { "x-kyf-relay-auth": relay.auth, "x-kyf-session-id": "session-0" } });
  assert((await response.text()).includes("last")); assert.equal(events.length, oldEvents);
});

test("relay business outcome and returned record ID stay within the explicitly bound child scope", async t => {
  const { events } = capture();
  const server = createServer(async (req, res) => {
    for await (const _chunk of req) { /* Consume the synthetic request. */ }
    res.setHeader("content-type", "application/json");
    res.end('{"choices":[{"message":{"content":"synthetic"},"finish_reason":"stop"}]}');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const relay = await new ModelTraceRelay({ protocol: "chat_completions", model: "synthetic",
    baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1` }, "1.18.34").start();
  t.after(async () => { await relay.close(); server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  let recordId: string | undefined;
  await assert.rejects(withModelTraceContext({ ...context, stepId: "parent-step" }, () =>
    withModelTraceStep("synthetic-child", async () => {
      const unbind = relay.bind("native-child", currentModelTraceContext());
      try {
        const response = await fetch(relay.baseUrl + "/chat/completions", { method: "POST", body: "{}",
          headers: { "x-kyf-relay-auth": relay.auth, "x-opencode-session-id": "native-child" } });
        await response.text();
        recordId = currentModelTraceRecordId();
        throw new Error("synthetic validation rejection");
      } finally { unbind(); }
    })));
  const start = events.find(event => event.type === "start");
  assert.equal(recordId, start.recordId);
  assert.equal(start.meta.source, "synthetic-child");
  const outcome = events.find(event => event.type === "outcome");
  assert.equal(outcome.recordId, recordId);
  assert.equal(outcome.applicationStatus, "failed");
  assert.equal(outcome.adoption, "discarded");
});

test("a retired explicit message identity is never associated with a newer session execution", async t => {
  const { events } = capture();
  const server = createServer(async (req, res) => {
    for await (const _chunk of req) { /* Consume the synthetic request. */ }
    res.end('{"choices":[]}');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const relay = await new ModelTraceRelay({ protocol: "chat_completions", model: "synthetic",
    baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1` }, "1.18.34").start();
  t.after(async () => { await relay.close(); server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  relay.bind("reused-session", { ...context, taskId: "old-execution" }, "old-message")();
  relay.bind("reused-session", { ...context, taskId: "new-execution" }, "new-message");
  const request = async (message: string) => (await fetch(relay.baseUrl + "/chat/completions", {
    method: "POST", body: "{}", headers: { "x-kyf-relay-auth": relay.auth,
      "x-kyf-session-id": "reused-session", "x-kyf-message-id": message },
  })).text();
  await request("old-message");
  assert.equal(events.length, 0, "an old explicit message cannot fall back to the live binding");
  await request("new-message");
  assert.equal(events.find(event => event.type === "start").meta.taskId, "new-execution");
});

test("16 MiB single protocol events and large requests stream under the shared work budget", async () => {
  const state = { collecting: true, collectionVersion: 1, deleteVersion: 1 };
  const stats = new Map<string, { bytes: number; first: string; last: string; final: boolean; indexes: number[] }>();
  const secret = "sk-large-synthetic-secret";
  let incomplete = false;
  configureModelTraceProducer({ state: () => state, emit: (_ctx, event: any) => {
    assert(Buffer.byteLength(JSON.stringify(event)) < 64 * 1024);
    if (event.type === "chunk") {
      assert(!event.text.includes(secret));
      const item = stats.get(event.kind) ?? { bytes: 0, first: "", last: "", final: false, indexes: [] };
      item.bytes += Buffer.byteLength(event.text); item.first ||= event.text;
      item.last = event.text; item.final ||= !!event.final; item.indexes.push(event.chunkIndex);
      stats.set(event.kind, item);
    }
    if (event.incomplete) incomplete = true;
    return true;
  } });
  await withModelTraceContext(context, async () => {
    const attempt = beginModelTraceAttempt({ model: "synthetic", protocol: "chat_completions", endpoint: "http://127.0.0.1/v1", method: "POST" }, [secret])!;
    const huge = "a".repeat(16 * 1024 * 1024 - 2048) + secret;
    attempt.request(JSON.stringify({ messages: [{ role: "user", content: huge }] }), new Headers());
    const response = captureModelResponse(new Response(JSON.stringify({ choices: [{ message: { content: huge }, finish_reason: "stop" }] }),
      { headers: { "content-type": "application/json" } }), attempt);
    assert.equal((await response.text()).length > 15 * 1024 * 1024, true);
  });
  assert.equal(incomplete, false);
  for (const kind of ["request", "response"]) {
    const item = stats.get(kind)!;
    assert(item.bytes > 15 * 1024 * 1024 && item.bytes <= 16 * 1024 * 1024);
    assert(item.final); assert(item.last.includes("已脱敏") || item.last.length < 10);
    assert.deepEqual(item.indexes, Array.from({ length: item.indexes.length }, (_, index) => index));
  }
  assert.equal(modelTraceBufferStats().workBytes, 0);
  assert(modelTraceBufferStats().peakWorkBytes <= modelTraceBufferStats().limit);
  configureModelTraceProducer();
});

test("all direct SDK protocols capture the provider packet before any adapter conversion", async t => {
  const { events } = capture();
  let selected = "", captured: any;
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    captured = JSON.parse(raw);
    res.setHeader("content-type", "application/json");
    const response = selected === "responses" ? { id: "response_synthetic", object: "response", status: "completed", model: "synthetic", output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "ok", annotations: [] }] }], usage: { input_tokens: 7, output_tokens: 1 } } :
      selected === "anthropic" ? { id: "msg_synthetic", type: "message", role: "assistant", content: [{ type: "text", text: "ok" }], stop_reason: "end_turn", usage: { input_tokens: 7, output_tokens: 1 } } :
      selected === "gemini" ? { candidates: [{ content: { parts: [{ text: "ok" }] }, finishReason: "STOP" }], usageMetadata: { promptTokenCount: 7, candidatesTokenCount: 1 } } :
      selected === "ollama" ? { message: { role: "assistant", content: "ok" }, done: true, prompt_eval_count: 7, eval_count: 1 } :
      { choices: [{ message: { content: "ok" }, finish_reason: "stop" }], usage: { prompt_tokens: 7, completion_tokens: 1 } };
    res.end(JSON.stringify(response));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  for (const protocol of ["responses", "chat_completions", "anthropic", "gemini", "ollama"] as const) {
    selected = protocol;
    const start = events.length;
    await withModelTraceContext({ ...context, taskId: `direct-${protocol}`, executionId: `execution-${protocol}` }, () => generateStructured({ protocol, model: "synthetic",
      baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}${protocol === "ollama" ? "" : "/v1"}`,
      apiKey: "protocol-secret" }, { system: "Synthetic system", prompt: "Synthetic prompt", maxOutputTokens: 64 }));
    const trace = events.slice(start);
    assert.equal(trace.filter(event => event.type === "start").length, 1, protocol);
    assert.deepEqual(JSON.parse(body(trace, "request")).body, captured, protocol);
    assert.equal(trace.findLast(event => event.type === "end").transportStatus, "completed");
    assert.equal(body(trace, "text"), "ok", `${protocol} accumulated visible text`);
    assert(!JSON.stringify(trace).includes("protocol-secret"));
  }
});

test("HTTP success plus invalid business JSON keeps distinct transfer and validation results", async t => {
  const { events } = capture();
  const server = createServer(async (req, res) => {
    for await (const _chunk of req) { /* Consume the synthetic request. */ }
    res.setHeader("content-type", "text/event-stream");
    res.end('data: {"choices":[{"delta":{"content":"invalid business JSON"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  // A plugin-side business parser rejects text the provider delivered successfully.
  const parseBusinessJson = async () => {
    const response = await generateStructured({ protocol: "chat_completions", model: "synthetic",
      baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1` },
    { system: "Return JSON.", prompt: "Synthetic chat", stream: true });
    try { return JSON.parse(response.text); } catch (error) {
      modelTraceOutcome("failed", { stage: "JSON-validation", code: "invalid-output" }, "discarded");
      throw error;
    }
  };
  await assert.rejects(withModelTraceContext(context, parseBusinessJson));
  assert.equal(events.find(event => event.type === "end").transportStatus, "completed");
  assert.equal(events.find(event => event.type === "outcome").applicationStatus, "failed");
  assert(body(events, "result").includes("JSON-validation"));
  assert(body(events, "response").includes("invalid business JSON"));
});

test("direct Responses streaming captures the actual request and original SSE before SDK processing", async t => {
  const { events } = capture();
  let captured: any;
  const frames = [
    { type: "response.created", response: { id: "resp_synthetic_stream", status: "in_progress" } },
    { type: "response.output_text.delta", output_index: 0, content_index: 0, delta: "Synthetic " },
    { type: "response.output_text.delta", output_index: 0, content_index: 0, delta: "stream" },
    { type: "response.completed", response: { id: "resp_synthetic_stream", status: "completed", output_text: "Synthetic stream",
      usage: { input_tokens: 9, output_tokens: 3, total_tokens: 12 } } },
  ].map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    captured = JSON.parse(raw);
    assert.equal(req.url, "/v1/responses");
    res.setHeader("content-type", "text/event-stream");
    for (const frame of frames) {
      res.write(frame);
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    res.end();
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); configureModelTraceProducer(); });
  let projected = "";
  const result = await withModelTraceContext({ ...context, taskId: "direct-responses-stream", executionId: "execution-responses-stream" }, () =>
    generateStructured({ protocol: "responses", model: "synthetic", apiKey: "synthetic-stream-key",
      baseUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1` },
    { system: "Synthetic system", prompt: "Synthetic prompt", stream: true, onTextDelta: text => { projected += text; } }));
  assert.equal(events.filter(event => event.type === "start").length, 1);
  assert.deepEqual(JSON.parse(body(events, "request")).body, captured);
  assert.equal(captured.stream, true);
  assert.equal(body(events, "response"), frames.join(""));
  assert.equal(body(events, "text"), "Synthetic stream");
  assert.equal(result.text, "Synthetic stream");
  assert.equal(projected, result.text);
  const end = events.findLast(event => event.type === "end");
  assert.equal(end.transportStatus, "completed");
  assert.deepEqual(end.usage, { input_tokens: 9, output_tokens: 3, total_tokens: 12 });
  assert(!JSON.stringify(events).includes("synthetic-stream-key"));
});

test("retention stops an expired attempt while a newer attempt continues in the same capture generation", t => {
  const events: any[] = [];
  const state: ModelTraceState = { collecting: true, collectionVersion: 3, deleteVersion: 7 };
  let now = 1_800_000_000_000;
  t.mock.method(Date, "now", () => now);
  configureModelTraceProducer({ state: () => state, emit: (_context, event) => { events.push(event); return true; } });
  t.after(() => configureModelTraceProducer());
  const meta = { model: "synthetic", protocol: "responses", endpoint: "http://127.0.0.1/v1/responses", method: "POST" };
  const old = withModelTraceContext(context, () => beginModelTraceAttempt(meta))!;
  now += 1_000;
  const current = withModelTraceContext({ ...context, taskId: "newer-task", executionId: "newer-execution" }, () => beginModelTraceAttempt(meta))!;
  state.retentionCutoffAt = now;
  const oldCount = events.filter(event => event.recordId === old.id).length;
  old.content("response", "expired diagnostic fragment");
  old.finish("completed");
  current.content("response", "current diagnostic fragment");
  current.finish("completed");
  assert.equal(old.isActive(), false);
  assert.equal(events.filter(event => event.recordId === old.id).length, oldCount);
  assert.equal(current.isActive(), true);
  const retained = events.filter(event => event.recordId === current.id);
  assert.equal(body(retained, "response"), "current diagnostic fragment");
  assert.equal(retained.at(-1).transportStatus, "completed");
  assert(retained.every(event => event.collectionVersion === 3 && event.deleteVersion === 7));
  state.deleteVersion++;
  assert.equal(current.isActive(), false, "explicit deletion still revokes every older capture version");
});

test("ten large concurrent responses preserve business bytes and scope with a bounded slow diagnostic sink", async t => {
  const state: ModelTraceState = { collecting: true, collectionVersion: 1, deleteVersion: 1 };
  const queueLimit = 128 * 1024;
  const queue: Array<{ bytes: number; event: Record<string, any> }> = [];
  const owners = new Map<string, { contactId: string; sessionId: string }>();
  const responseOwners = new Set<string>();
  const rejected = new Set<string>();
  let queuedBytes = 0, peakQueuedBytes = 0, drainedEvents = 0;
  const drain = () => { drainedEvents += queue.length; queue.length = 0; queuedBytes = 0; };
  // Diagnostics deliberately consume in batches, while business readers remain
  // independent. Refusal models the producer client's bounded overload policy.
  const consumer = setInterval(drain, 25);
  configureModelTraceProducer({ state: () => state, emit: (scope, event: Record<string, any>) => {
    assert(modelTraceBufferStats().workBytes <= 4 * 1024 * 1024);
    if (event.type === "start") {
      assert.equal(event.meta.contactId, scope.contactId);
      assert.equal(event.meta.sessionId, scope.sessionId);
      owners.set(event.recordId, { contactId: scope.contactId!, sessionId: scope.sessionId! });
    }
    const owner = owners.get(event.recordId)!;
    assert(owner, "each observed diagnostic event has an explicit start");
    assert.equal(owner.contactId, scope.contactId);
    assert.equal(owner.sessionId, scope.sessionId);
    if (event.type === "chunk" && event.kind === "response" && event.text.includes("contact-")) {
      const matches = event.text.match(/contact-\d+\|session-\d+/gu);
      assert.deepEqual(matches, [`${owner.contactId}|${owner.sessionId}`]);
      responseOwners.add(event.recordId);
    }
    const bytes = Buffer.byteLength(JSON.stringify(event));
    if (queuedBytes + bytes > queueLimit) { rejected.add(event.recordId); return false; }
    queue.push({ bytes, event }); queuedBytes += bytes;
    peakQueuedBytes = Math.max(peakQueuedBytes, queuedBytes);
    return true;
  } });
  t.after(() => { clearInterval(consumer); drain(); configureModelTraceProducer(); });
  const sharedBody = Buffer.alloc(64 * 1024, "x");
  const blocks = 256; // 16 MiB per response, with one shared payload allocation.
  const suffix = Buffer.from('"},"finish_reason":"stop"}]}');
  const byteCounts = await Promise.all(Array.from({ length: 10 }, (_, index) =>
    withModelTraceContext({ ...context, taskId: `large-task-${index}`, executionId: `large-execution-${index}`,
      contactId: `contact-${index}`, sessionId: `session-${index}` }, async () => {
      const prefix = Buffer.from(`{"choices":[{"message":{"content":"contact-${index}|session-${index}|`);
      let sent = 0;
      const attempt = beginModelTraceAttempt({ model: "synthetic", protocol: "chat_completions",
        endpoint: "http://127.0.0.1/v1/chat/completions", method: "POST" })!;
      const input = new ReadableStream<Uint8Array>({ async pull(controller) {
        await new Promise<void>(resolve => setImmediate(resolve));
        if (sent > blocks + 1) { controller.close(); return; }
        controller.enqueue(sent === 0 ? prefix : sent === blocks + 1 ? suffix : sharedBody);
        sent++;
      } });
      const reader = captureModelResponse(new Response(input, { headers: { "content-type": "application/json" } }), attempt).body!.getReader();
      let received = 0, bytes = 0;
      for (;;) {
        const next = await reader.read(); if (next.done) break;
        const expected = received === 0 ? prefix : received === blocks + 1 ? suffix : sharedBody;
        assert(Buffer.from(next.value.buffer, next.value.byteOffset, next.value.byteLength).equals(expected),
          `business response ${index}, fragment ${received} is unchanged`);
        bytes += next.value.byteLength; received++;
      }
      assert.equal(received, blocks + 2);
      assert.equal(bytes, prefix.length + blocks * sharedBody.length + suffix.length);
      return bytes;
    })));
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(owners.size, 10);
  assert.equal(responseOwners.size, 10, "every captured response prefix belongs to its own explicit scope");
  assert.equal(byteCounts.length, 10);
  assert(byteCounts.every(bytes => bytes > 16 * 1024 * 1024));
  assert(rejected.size > 0, "slow diagnostics exercise overload refusal without stopping business reads");
  assert(drainedEvents > 0);
  assert(peakQueuedBytes <= queueLimit);
  assert(modelTraceBufferStats().peakWorkBytes <= 4 * 1024 * 1024);
  assert.equal(modelTraceBufferStats().workBytes, 0, "all parsers release buffers after completion or overload");
});
