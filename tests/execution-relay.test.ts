// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The plugin SDK execution relay against a fake gateway: secret check, fail-closed unbound
// sessions, header rewrite, step path, streaming, abort propagation and revoked tokens.
import assert from "node:assert/strict";
import { createServer, request, type IncomingHttpHeaders, type IncomingMessage, type ServerResponse } from "node:http";
import { test, type TestContext } from "node:test";
import { ExecutionRelay } from "../plugin-sdk/node/execution-relay";

interface Seen { method: string; url: string; headers: IncomingHttpHeaders; body: string }

async function gateway(t: TestContext, handler?: (req: IncomingMessage, res: ServerResponse, seen: Seen) => void) {
  const seen: Seen[] = [];
  const closed: string[] = [];
  const server = createServer(async (req, res) => {
    let body = "";
    if (!handler) for await (const chunk of req) body += chunk;
    const entry = { method: req.method!, url: req.url!, headers: req.headers, body };
    seen.push(entry);
    res.once("close", () => { if (!res.writableFinished) closed.push(req.url!); });
    if (handler) { handler(req, res, entry); return; }
    res.writeHead(200, { "content-type": "application/json", "x-kyf-attempt-id": "attempt-1" });
    res.end(JSON.stringify({ ok: true }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  t.after(async () => { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); });
  return { url: `http://127.0.0.1:${port}/s/svc-1`, seen, closed };
}

async function relayFor(t: TestContext, serviceUrl: string) {
  const relay = new ExecutionRelay(serviceUrl);
  const base = await relay.start();
  t.after(() => relay.close());
  return { relay, base };
}

function post(url: string, headers: Record<string, string>, body = '{"model":"synthetic"}', method = "POST") {
  return new Promise<{ status: number; headers: IncomingHttpHeaders; body: string }>((resolve, reject) => {
    const req = request(url, { method, headers: { "content-type": "application/json", ...headers } }, res => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", chunk => { text += chunk; });
      res.once("end", () => resolve({ status: res.statusCode!, headers: res.headers, body: text }));
      res.once("error", reject);
    });
    req.once("error", reject);
    req.end(method === "GET" ? undefined : body);
  });
}

test("only loopback POSTs carrying the relay secret are accepted", async t => {
  const g = await gateway(t);
  const { relay, base } = await relayFor(t, g.url);
  relay.bind("ses_1", { token: "exec-token", step: "advisor" });
  const session = { "x-opencode-session-id": "ses_1" };
  assert.match(base, /^http:\/\/127\.0\.0\.1:\d+\/s\/svc-1$/u);
  for (const headers of [{}, { authorization: "Bearer wrong" }, { "x-api-key": relay.secret.slice(1) },
    { authorization: `Bearer ${relay.secret}`, origin: "http://127.0.0.1" },
    { authorization: `Bearer ${relay.secret}`, "sec-fetch-site": "same-origin" }]) {
    assert.equal((await post(`${base}/chat/completions`, { ...session, ...headers })).status, 403, JSON.stringify(headers));
  }
  assert.equal((await post(`${base}/chat/completions`, { ...session, authorization: `Bearer ${relay.secret}` }, "", "GET")).status, 403);
  for (const target of [base.replace("/s/svc-1", "/s/other"), `${base}/`, `${base}/../other/chat/completions`, base + "x/chat"]) {
    assert.equal((await post(target, { ...session, authorization: `Bearer ${relay.secret}` })).status, 403, target);
  }
  assert.equal(g.seen.length, 0, "nothing reaches the gateway");
  assert.throws(() => new ExecutionRelay("http://127.0.0.1:1/s/svc~step"));
  assert.throws(() => new ExecutionRelay("http://example.com:1/s/svc"));
  assert.throws(() => relay.bind("ses_2", { token: "exec-token", step: "bad/step" }));
  assert.throws(() => relay.bind("ses_2", { token: "two words", step: "advisor" }));
});

test("an unbound or unbound-again session fails closed with execution-required", async t => {
  const g = await gateway(t);
  const { relay, base } = await relayFor(t, g.url);
  const auth = { authorization: `Bearer ${relay.secret}` };
  const unbound = await post(`${base}/chat/completions`, { ...auth, "x-opencode-session-id": "ses_1" });
  assert.equal(unbound.status, 401);
  assert.equal(JSON.parse(unbound.body).error.code, "execution-required");
  assert.equal((await post(`${base}/chat/completions`, auth)).status, 401, "no session header");
  const unbind = relay.bind("ses_1", { token: "exec-token", step: "advisor" });
  assert.equal((await post(`${base}/chat/completions`, { ...auth, "x-opencode-session-id": "ses_1" })).status, 200);
  const stale = relay.bind("ses_1", { token: "newer-token", step: "advisor" });
  unbind();
  assert.equal((await post(`${base}/chat/completions`, { ...auth, "x-opencode-session-id": "ses_1" })).status, 200,
    "an older binding's unbind never removes a newer one");
  stale();
  assert.equal((await post(`${base}/chat/completions`, { ...auth, "x-opencode-session-id": "ses_1" })).status, 401);
  assert.equal(g.seen.length, 2);
  assert.equal(g.seen[1].headers.authorization, "Bearer newer-token");
});

test("headers are rewritten: token in the secret's header, local identifiers and the secret dropped", async t => {
  const g = await gateway(t);
  const { relay, base } = await relayFor(t, g.url);
  relay.bind("ses_1", { token: "exec-token", step: "wechat-summary" });
  const common = { "x-opencode-session-id": "ses_1", "x-opencode-project": "p", "x-kyf-step": "spoofed", "x-session-affinity": "a",
    "x-parent-session-id": "parent", "anthropic-version": "2023-06-01", "user-agent": "synthetic-sdk" };
  const body = JSON.stringify({ model: "synthetic", messages: [{ role: "user", content: "你好" }] });
  const ok = await post(`${base}/chat/completions?alt=sse&key=${relay.secret}`, { ...common, authorization: `Bearer ${relay.secret}` }, body);
  assert.equal(ok.status, 200);
  assert.equal(ok.headers["x-kyf-attempt-id"], "attempt-1", "the gateway reply passes through unchanged");
  await post(`${base}/messages`, { ...common, "x-api-key": relay.secret });
  await post(`${base}/models/synthetic:streamGenerateContent?alt=sse`, { ...common, "x-goog-api-key": relay.secret,
    authorization: "Bearer not-the-secret" });
  const [first, second, third] = g.seen;
  assert.equal(first.url, "/s/svc-1~wechat-summary/chat/completions?alt=sse");
  assert.equal(first.body, body, "the body is forwarded byte for byte");
  assert.equal(first.headers.authorization, "Bearer exec-token");
  assert.equal(second.url, "/s/svc-1~wechat-summary/messages");
  assert.equal(second.headers["x-api-key"], "exec-token");
  assert.equal(second.headers.authorization, undefined);
  assert.equal(third.url, "/s/svc-1~wechat-summary/models/synthetic:streamGenerateContent?alt=sse");
  assert.equal(third.headers["x-goog-api-key"], "exec-token");
  assert.equal(third.headers.authorization, undefined, "other credential-like headers never pass");
  for (const seen of g.seen) {
    assert(!JSON.stringify(seen).includes(relay.secret), "the relay secret never reaches the gateway");
    assert(!Object.keys(seen.headers).some(key => key.startsWith("x-kyf-") || key.startsWith("x-opencode-") ||
      key.startsWith("x-session-") || key === "x-parent-session-id"));
    assert.equal(seen.headers["anthropic-version"], "2023-06-01");
    assert.equal(seen.headers["user-agent"], "synthetic-sdk");
    assert.equal(seen.headers.host, new URL(g.url).host);
  }
});

test("replies stream through and a client disconnect aborts the gateway request", async t => {
  let release!: () => void;
  const g = await gateway(t, (_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("data: first\n\n");
    release = () => res.end("data: [DONE]\n\n");
  });
  const { relay, base } = await relayFor(t, g.url);
  relay.bind("ses_1", { token: "exec-token", step: "advisor" });
  const headers = { authorization: `Bearer ${relay.secret}`, "x-opencode-session-id": "ses_1" };
  const chunks: string[] = [];
  await new Promise<void>((resolve, reject) => {
    const req = request(`${base}/chat/completions`, { method: "POST", headers }, res => {
      res.setEncoding("utf8");
      res.on("data", chunk => { chunks.push(chunk); if (chunks.length === 1) release(); });
      res.once("end", resolve);
    });
    req.once("error", reject);
    req.end("{}");
  });
  assert.deepEqual(chunks, ["data: first\n\n", "data: [DONE]\n\n"], "the first event arrives before the reply ends");

  const aborted = new Promise<void>(resolve => {
    const req = request(`${base}/chat/completions`, { method: "POST", headers }, res => {
      res.once("data", () => { req.destroy(); resolve(); });
    });
    req.once("error", () => {});
    req.end("{}");
  });
  await aborted;
  for (let n = 0; n < 100 && g.closed.length < 1; n++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(g.closed.length, 1, "the gateway sees the disconnect while it is still streaming");
});

test("unbinding ends in-flight requests of that execution, close retires the listener", async t => {
  const g = await gateway(t, (_req, res) => { res.writeHead(200, { "content-type": "text/event-stream" }); res.write("data: x\n\n"); });
  const { relay, base } = await relayFor(t, g.url);
  const unbind = relay.bind("ses_1", { token: "exec-token", step: "advisor" });
  const ended = new Promise<string>(resolve => {
    const req = request(`${base}/chat/completions`, { method: "POST", headers: { authorization: `Bearer ${relay.secret}`,
      "x-opencode-session-id": "ses_1" } }, res => {
      res.once("data", () => unbind());
      res.once("end", () => resolve("end"));
      res.once("error", () => resolve("error"));
      res.once("close", () => resolve("close"));
    });
    req.once("error", () => resolve("error"));
    req.end("{}");
  });
  assert.notEqual(await ended, "end", "the stream does not complete normally after unbind");
  for (let n = 0; n < 100 && g.closed.length < 1; n++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(g.closed.length, 1);
  await relay.close();
  await assert.rejects(post(`${base}/chat/completions`, { authorization: `Bearer ${relay.secret}` }));
});

test("a revoked token's gateway refusal reaches the engine unchanged; a missing gateway is 502", async t => {
  const g = await gateway(t, (_req, res) => {
    res.writeHead(401, { "content-type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: { code: "execution-revoked", message: "执行令牌无效或任务已停止", type: "execution-revoked" } }));
  });
  const { relay, base } = await relayFor(t, g.url);
  relay.bind("ses_1", { token: "revoked-token", step: "advisor" });
  const refused = await post(`${base}/chat/completions`, { authorization: `Bearer ${relay.secret}`, "x-opencode-session-id": "ses_1" });
  assert.equal(refused.status, 401);
  assert.equal(JSON.parse(refused.body).error.code, "execution-revoked");
  const missing = new ExecutionRelay("http://127.0.0.1:9/s/svc-1");
  const missingBase = await missing.start();
  t.after(() => missing.close());
  missing.bind("ses_1", { token: "exec-token", step: "advisor" });
  const unavailable = await post(`${missingBase}/chat/completions`, { authorization: `Bearer ${missing.secret}`, "x-opencode-session-id": "ses_1" });
  assert.equal(unavailable.status, 502);
  assert.equal(JSON.parse(unavailable.body).error.code, "gateway-unavailable");
});
