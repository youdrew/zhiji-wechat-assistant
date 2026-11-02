// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The settings probe lists models and tests a connection from one stdin request.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import { runProbe } from "../scripts/model-probe";

test("model list and connection test reach the configured endpoint with the supplied key", async t => {
  const seen: { url?: string; auth?: string }[] = [];
  const server = createServer((request, response) => {
    seen.push({ url: request.url, auth: request.headers.authorization });
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/v1/models") response.end(JSON.stringify({ data: [{ id: "synthetic" }] }));
    else if (request.url === "/v1/chat/completions") {
      request.resume();
      response.end(JSON.stringify({ choices: [{ message: { content: '{"ok":true}' } }],
        usage: { prompt_tokens: 3, completion_tokens: 2 } }));
    } else { response.statusCode = 404; response.end("{}"); }
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const baseUrl = `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`;
  const common = { protocol: "chat_completions", baseUrl, apiKey: "synthetic-key" };
  const listed = await runProbe(JSON.stringify({ cmd: "list", ...common }));
  assert.equal(listed.supported, true);
  assert.deepEqual((listed.models as { id: string }[]).map(model => model.id), ["synthetic"]);
  const tested = await runProbe(JSON.stringify({ cmd: "test", ...common, model: "synthetic" }));
  assert.equal(tested.ok, true);
  assert.equal(typeof tested.latencyMs, "number");
  assert.ok(seen.every(item => item.auth === "Bearer synthetic-key"));
});

test("invalid requests and provider failures return short error codes", async t => {
  assert.deepEqual(await runProbe("not json"), { error: "invalid-request" });
  assert.deepEqual(await runProbe(JSON.stringify({ cmd: "remove" })), { error: "invalid-request" });
  assert.deepEqual(await runProbe(JSON.stringify({ cmd: "test", protocol: "chat_completions",
    baseUrl: "https://example.invalid/v1" })), { error: "invalid-request" });
  const server = createServer((_request, response) => { response.statusCode = 401; response.end('{"error":"denied"}'); });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const baseUrl = `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`;
  const failed = await runProbe(JSON.stringify({ cmd: "test", protocol: "chat_completions", baseUrl,
    apiKey: "synthetic-key", model: "synthetic" }));
  assert.equal(failed.error, "auth");
});
