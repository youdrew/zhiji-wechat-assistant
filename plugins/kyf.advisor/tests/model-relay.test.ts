// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The advisor's loopback model relay against a synthetic gateway: no engine, model or user data.
import assert from "node:assert/strict";
import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import { test } from "node:test";
import { ModelRelay, relayBase, type RelayProtocol } from "../engine/model-relay";

interface Seen { url: string; headers: IncomingHttpHeaders; body: string }

async function gateway(status = 200): Promise<{ server: Server; url: string; seen: Seen[] }> {
  const seen: Seen[] = [];
  const server = createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    seen.push({ url: req.url ?? "", headers: req.headers, body });
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: status === 200 }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  return { server, seen, url: `http://127.0.0.1:${(server.address() as { port: number }).port}/s/svc-1` };
}

async function started(protocol: RelayProtocol, status = 200) {
  const upstream = await gateway(status);
  const relay = new ModelRelay(upstream.url, protocol);
  const base = await relay.start();
  return { ...upstream, relay, base, close: async () => { await relay.close(); upstream.server.close(); } };
}

const post = (url: string, headers: Record<string, string>, body = "{}") =>
  fetch(url, { method: "POST", headers: { "content-type": "application/json", ...headers }, body });

test("only a loopback gateway service url is accepted", () => {
  assert.throws(() => new ModelRelay("http://127.0.0.1:1/s/svc~step", "chat_completions"));
  assert.throws(() => new ModelRelay("http://example.com:1/s/svc", "chat_completions"));
  assert.throws(() => new ModelRelay("http://127.0.0.1:1/s/svc?x=1", "chat_completions"));
});

test("requests are refused without the relay secret, and fail closed while unbound", async t => {
  const f = await started("chat_completions");
  t.after(f.close);
  assert.equal((await post(`${f.base}/chat/completions`, { authorization: "Bearer wrong" })).status, 403);
  assert.equal((await post(`${f.base}/chat/completions`, { authorization: `Bearer ${f.relay.secret}` })).status, 401);
  assert.equal((await fetch(`${f.base}/chat/completions`, { headers: { authorization: `Bearer ${f.relay.secret}` } })).status, 403);
  assert.equal((await post(`${f.base.replace("/s/svc-1", "/s/other")}/chat/completions`, { authorization: `Bearer ${f.relay.secret}` })).status, 403);
  assert.equal(f.seen.length, 0);
});

test("a bound request reaches the gateway under the execution token and step, without the secret", async t => {
  const f = await started("chat_completions");
  t.after(f.close);
  const attempts: number[] = [];
  f.relay.onAttempt = attempt => attempts.push(attempt.status);
  const unbind = f.relay.bind({ token: "exec-token", step: "advisor" });
  const response = await post(`${f.base}/chat/completions?key=leak`, { authorization: `Bearer ${f.relay.secret}`, "x-kyf-step": "spoofed" }, '{"model":"m"}');
  assert.equal(response.status, 200);
  assert.equal(f.seen[0].url, "/s/svc-1~advisor/chat/completions");
  assert.equal(f.seen[0].headers.authorization, "Bearer exec-token");
  assert.equal(f.seen[0].headers["x-kyf-step"], undefined);
  assert.equal(f.seen[0].body, '{"model":"m"}');
  assert.ok(!JSON.stringify(f.seen[0]).includes(f.relay.secret));
  assert.deepEqual(attempts, [200]);
  unbind();
  assert.equal((await post(`${f.base}/chat/completions`, { authorization: `Bearer ${f.relay.secret}` })).status, 401);
});

test("SDK version segments are folded into the gateway's API prefix per protocol", async t => {
  const anthropic = await started("anthropic");
  const gemini = await started("gemini");
  const responses = await started("responses");
  t.after(async () => { await anthropic.close(); await gemini.close(); await responses.close(); });
  for (const f of [anthropic, gemini, responses]) f.relay.bind({ token: "t", step: "advisor" });
  await post(`${anthropic.base}/v1/messages`, { "x-api-key": anthropic.relay.secret });
  assert.equal(anthropic.seen[0].url, "/s/svc-1~advisor/messages");
  assert.equal(anthropic.seen[0].headers["x-api-key"], "t");
  await post(`${gemini.base}/v1beta/models/m:streamGenerateContent?alt=sse`, { "x-goog-api-key": gemini.relay.secret });
  assert.equal(gemini.seen[0].url, "/s/svc-1~advisor/models/m:streamGenerateContent?alt=sse");
  await post(`${responses.base}/responses`, { authorization: `Bearer ${responses.relay.secret}` });
  assert.equal(responses.seen[0].url, "/s/svc-1~advisor/responses");
  assert.equal(relayBase("ollama", "http://127.0.0.1:1/s/x"), "http://127.0.0.1:1/s/x/v1");
  assert.equal(relayBase("anthropic", "http://127.0.0.1:1/s/x"), "http://127.0.0.1:1/s/x");
});

test("answers without thinking add chat-template switches to chat requests only", async t => {
  const chat = await started("chat_completions");
  const anthropic = await started("anthropic");
  t.after(async () => { await chat.close(); await anthropic.close(); });
  chat.relay.bind({ token: "t", step: "advisor", noThinking: true });
  anthropic.relay.bind({ token: "t", step: "advisor", noThinking: true });
  const text = "合成正文😀";
  await post(`${chat.base}/chat/completions`, { authorization: `Bearer ${chat.relay.secret}` }, JSON.stringify({ model: "m", messages: [{ role: "user", content: text }] }));
  const sent = JSON.parse(chat.seen[0].body);
  assert.deepEqual(sent.chat_template_kwargs, { thinking: false, enable_thinking: false });
  assert.equal(sent.messages[0].content, text);
  assert.equal(Number(chat.seen[0].headers["content-length"]), Buffer.byteLength(chat.seen[0].body));
  await post(`${anthropic.base}/v1/messages`, { "x-api-key": anthropic.relay.secret }, '{"model":"m"}');
  assert.equal(anthropic.seen[0].body, '{"model":"m"}');
});

test("the gateway's failure status is reported for every attempt", async t => {
  const f = await started("chat_completions", 429);
  t.after(f.close);
  const attempts: Array<{ status: number; attempt: number }> = [];
  f.relay.onAttempt = attempt => attempts.push(attempt);
  f.relay.bind({ token: "t", step: "advisor" });
  for (let index = 0; index < 2; index++) {
    assert.equal((await post(`${f.base}/chat/completions`, { authorization: `Bearer ${f.relay.secret}` })).status, 429);
  }
  assert.deepEqual(attempts, [{ status: 429, attempt: 1 }, { status: 429, attempt: 2 }]);
});
