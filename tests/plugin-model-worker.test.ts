// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import http from "node:http";
import { test } from "node:test";
import { generate, gatewayConfig, WorkerError } from "../plugin-sdk/node/model-worker";
import { DEFAULT_MODEL_TIMEOUT_MS, MAX_MODEL_TIMEOUT_MS } from "../electron/model-timeout";

// Since 2026-10-25 the gateway applies Core's deadline once a request leaves its queue; the worker sets none,
// so a long wait for a turn never fails a call.
test("model worker leaves Core's deadline to the gateway and ignores legacy plugin limits", async context => {
  const service = { url: "http://127.0.0.1:1/v1", model: "synthetic", protocol: "chat_completions" };
  assert.equal(gatewayConfig(service, "synthetic").timeoutMs, DEFAULT_MODEL_TIMEOUT_MS);
  assert.equal(gatewayConfig(service, "synthetic").gatewayDeadline, true);
  assert.equal(gatewayConfig({ ...service, timeoutMs: MAX_MODEL_TIMEOUT_MS }, "synthetic").timeoutMs, MAX_MODEL_TIMEOUT_MS);
  assert.throws(() => gatewayConfig({ ...service, timeoutMs: 240_000.5 }, "synthetic"), WorkerError);
  const observed: number[] = [];
  const original = AbortSignal.timeout;
  context.mock.method(AbortSignal, "timeout", (milliseconds: number) => {
    observed.push(milliseconds);
    return original(milliseconds);
  });
  context.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
    choices: [{ message: { content: "synthetic reply" }, finish_reason: "stop" }],
  }), { headers: { "content-type": "application/json" } }));
  assert.equal((await generate({ service: { ...service, timeoutMs: 7_200_000 }, token: "synthetic", request: {
    system: "synthetic", prompt: "synthetic", timeoutMs: 45_000,
  } }, new AbortController().signal)).text, "synthetic reply");
  assert.ok(observed.length > 0 && observed.every(milliseconds => milliseconds === 2_147_483_647), String(observed));
});

test("model worker exposes only a valid numeric error status", () => {
  for (const status of [400, 401, 408, 429, 500, 502, 503, 504, 599])
    assert.equal(new WorkerError("provider-error", status).httpStatus, status);
  for (const status of [undefined, null, true, "502", 0, 200, 399, 600, 502.5, NaN, Infinity, {}])
    assert.equal(new WorkerError("provider-error", status).httpStatus, undefined);
});

test("upstream errors retain status and safe classification without provider diagnostics", async () => {
  let responseStatus = 502;
  const server = http.createServer((request, response) => {
    request.resume();
    response.writeHead(responseStatus, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ error: { code: "gateway_error", message: "SYNTHETIC_PRIVATE_PROVIDER_DETAIL" } }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const port = (server.address() as { port: number }).port;
    for (const [status, code] of [[502, "provider-error"], [429, "rate-limit"], [401, "auth"]] as const) {
      responseStatus = status;
      await assert.rejects(generate({ service: { url: `http://127.0.0.1:${port}/v1`, model: "synthetic",
        protocol: "chat_completions" }, token: "synthetic-token", request: {
        system: "Return synthetic test content", prompt: "synthetic", maxOutputTokens: 16,
      } }, new AbortController().signal), error => {
        assert.ok(error instanceof WorkerError);
        assert.equal(error.code, code);
        assert.equal(error.httpStatus, status);
        assert.ok(!error.message.includes("SYNTHETIC_PRIVATE_PROVIDER_DETAIL"));
        assert.ok(!error.message.includes("synthetic-token"));
        return true;
      });
    }
  } finally {
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});

test("a reply cut at the provider's cap keeps its text so the plugin can salvage it", async context => {
  const chunk = (delta: unknown, finish: string | null = null) => `data: ${JSON.stringify({
    object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
  context.mock.method(globalThis, "fetch", async () => new Response(
    chunk({ content: '{"background":"半截' }) + chunk({}, "length") + "data: [DONE]\n\n",
  { headers: { "content-type": "text/event-stream" } }));
  await assert.rejects(generate({ service: { url: "http://127.0.0.1:1/v1", model: "synthetic",
    protocol: "chat_completions" }, token: "synthetic", request: {
    system: "synthetic", prompt: "synthetic", jsonMode: true, maxOutputTokens: 16,
  } }, new AbortController().signal), error => {
    assert.ok(error instanceof WorkerError);
    assert.equal(error.code, "output-truncated");
    assert.equal(error.partial, '{"background":"半截');
    return true;
  });
});

test("model worker streams so a relay that waits for response headers cannot drop a long reply", async context => {
  const bodies: Record<string, any>[] = [];
  context.mock.method(globalThis, "fetch", async (_input: unknown, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body || "{}")));
    const chunk = (delta: unknown, finish: string | null = null, usage?: unknown) => `data: ${JSON.stringify({
      object: "chat.completion.chunk", choices: usage ? [] : [{ index: 0, delta, finish_reason: finish }],
      ...(usage ? { usage } : {}) })}\n\n`;
    return new Response(chunk({ content: '{"memories":' }) + chunk({ content: "[]}" }) + chunk({}, "stop") +
      chunk(null, null, { prompt_tokens: 7, completion_tokens: 4 }) + "data: [DONE]\n\n",
    { headers: { "content-type": "text/event-stream" } });
  });
  const result = await generate({ service: { url: "http://127.0.0.1:1/v1", model: "synthetic",
    protocol: "chat_completions" }, token: "synthetic", step: "memory", request: {
    system: "synthetic", prompt: "synthetic", jsonMode: true, thinking: false, maxOutputTokens: 4096,
  } }, new AbortController().signal);
  assert.equal(result.text, '{"memories":[]}');
  assert.deepEqual(result.usage, { inputTokens: 7, outputTokens: 4 });
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].stream, true);
  assert.deepEqual(bodies[0].stream_options, { include_usage: true });
});
