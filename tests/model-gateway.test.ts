// The model gateway: tokens, credential injection, model checks, usage metering and revocation.
import assert from "node:assert/strict";
import http from "node:http";
import { EventEmitter, getEventListeners } from "node:events";
import { Readable } from "node:stream";
import type { AddressInfo } from "node:net";
import { it } from "node:test";
import { ModelGateway, tokenHash, type AttemptProgress, type UsageRecord } from "../electron/model-gateway";
import { ServiceQueue, AGING_MS } from "../electron/model-gateway-queue";
import { configureModelTraceProducer, withModelTraceContext } from "../electron/model-traces";

for (const interruption of ["disconnect", "revoke", "closed-during-write", "socket-error"] as const) {
  it(`releases a backpressured slot after ${interruption} so another execution can run`, async () => {
    const records: UsageRecord[] = [];
    let calls = 0, wrote!: () => void;
    const blocked = new Promise<void>(resolve => { wrote = resolve; });
    const gateway = new ModelGateway(record => records.push(record), (async (_url, init) => {
      calls++;
      if (calls > 1) return new Response("{}", { headers: { "content-type": "application/json" } });
      return new Response(new ReadableStream<Uint8Array>({ start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"choices":[]}\n\n'));
        init!.signal!.addEventListener("abort", () => controller.error(init!.signal!.reason), { once: true });
      } }), { headers: { "content-type": "text/event-stream" } });
    }) as typeof fetch);
    gateway.configure([{ id: "blocked", protocol: "chat_completions", baseUrl: "http://127.0.0.1:1/v1",
      model: "synthetic", concurrency: 1 }]);
    for (const token of ["old", "new"]) gateway.addToken(tokenHash(token), { plugin: "synthetic", task: token, execution: token });
    const request = (token: string) => Object.assign(Readable.from([Buffer.from('{"model":"synthetic"}')]), {
      headers: { authorization: `Bearer ${token}` }, method: "POST", url: "/s/blocked/chat/completions",
    }) as unknown as http.IncomingMessage;
    const response = (backpressure: boolean) => Object.assign(new EventEmitter(), {
      headersSent: false, destroyed: false, writableEnded: false,
      writeHead() { this.headersSent = true; },
      write() {
        if (backpressure) {
          if (interruption === "closed-during-write") this.destroy();
          wrote();
        }
        return !backpressure;
      },
      end() { this.writableEnded = true; },
      destroy() { this.destroyed = true; this.emit("close"); },
    });
    const firstResponse = response(true), secondResponse = response(false);
    const first = gateway["handle"](request("old"), firstResponse as unknown as http.ServerResponse);
    await blocked;
    const second = gateway["handle"](request("new"), secondResponse as unknown as http.ServerResponse);
    await new Promise<void>(resolve => setImmediate(resolve));
    if (interruption !== "closed-during-write")
      assert.equal(calls, 1, "the second execution must initially wait for the sole slot");
    if (interruption === "disconnect") firstResponse.destroy();
    else if (interruption === "revoke") gateway.revokeToken(tokenHash("old"));
    else if (interruption === "socket-error") firstResponse.emit("error", new Error("synthetic socket failure"));
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([Promise.all([first, second]), new Promise<never>((_resolve, reject) => {
        timeout = setTimeout(() => reject(new Error("cancelled request retained its service slot")), 500);
      })]);
      assert.equal(calls, 2);
      assert.equal(secondResponse.writableEnded, true);
      assert.equal(records[0]!.status, interruption === "socket-error" ? "stream-error" : "cancelled");
      assert.equal(records[1]!.status, "ok");
      assert.equal(firstResponse.listenerCount("drain"), 0);
      assert.equal(firstResponse.listenerCount("error"), 0);
    } finally {
      clearTimeout(timeout);
      // Let the pre-fix implementation exit too, so a failing regression never hangs the suite.
      firstResponse.emit("drain");
      await Promise.allSettled([first, second]);
      await gateway.close();
    }
  });
}

it("records the admitted request timeout snapshot in gateway trace parameters", async context => {
  const events: Record<string, any>[] = [];
  configureModelTraceProducer({ state: () => ({ collecting: true, collectionVersion: 1, deleteVersion: 1 }),
    emit: (_scope, event) => { events.push(event); return true; } });
  context.after(() => configureModelTraceProducer());
  const gateway = new ModelGateway(() => {}, (async () => new Response("{}", {
    headers: { "content-type": "application/json" },
  })) as typeof fetch);
  // The current route may have a new setting by the time a queued request gets a
  // slot. Telemetry must describe its captured limit, just like its actual timer.
  const service = { id: "synthetic", protocol: "chat_completions", baseUrl: "http://127.0.0.1:1/v1",
    model: "synthetic", timeoutMs: 60_000 };
  const request = { headers: {} } as http.IncomingMessage;
  const response = { writeHead() {}, write() { return true; }, end() {}, destroy() {} } as unknown as http.ServerResponse;
  await withModelTraceContext({ producerId: "synthetic", taskId: "synthetic", executionId: "synthetic", source: "test" },
    () => gateway["forward"](request, response, service,
      { attemptId: "synthetic-attempt", plugin: "synthetic", task: "synthetic", execution: "synthetic", step: "summary",
        service: service.id, protocol: service.protocol, model: service.model },
      new URL("http://127.0.0.1/s/synthetic/chat/completions"), "chat/completions", Buffer.from("{}"),
      "POST", new AbortController(), 3_600_000));
  const start = events.find(event => event.type === "start");
  assert.equal(start?.meta.parameters.timeoutMs, 3_600_000);
  assert.equal(start?.meta.timeoutMs, 3_600_000);
});

it("keeps slow prefill and stream gaps within the saved limit and applies deadline edits only to new requests", async context => {
  const forwarded: Array<{ signal: AbortSignal; respond: (response: Response) => void }> = [];
  let notify: () => void = () => {};
  const records: UsageRecord[] = [];
  const gateway = new ModelGateway(record => records.push(record), (async (_url, init) =>
    new Promise<Response>((resolve, reject) => {
      const signal = init!.signal!;
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      forwarded.push({ signal, respond: resolve });
      notify();
    })) as typeof fetch);
  const service = { id: "slow", protocol: "chat_completions", baseUrl: "http://127.0.0.1:1/v1",
    model: "synthetic", timeoutMs: 1_200_000 };
  gateway.configure([service]);
  gateway.addToken(tokenHash("synthetic"), { plugin: "test", task: "test", execution: "test" });
  const port = await gateway.listen();
  const request = () => new Promise<{ status: number; body: string }>((resolve, reject) => {
    const req = http.request(`http://127.0.0.1:${port}/s/slow/chat/completions`, {
      method: "POST", headers: { authorization: "Bearer synthetic" },
    }, res => {
      let body = "";
      res.on("data", chunk => { body += String(chunk); });
      res.on("end", () => resolve({ status: res.statusCode!, body }));
      res.on("error", reject);
    });
    req.on("error", reject);
    req.end('{"model":"synthetic"}');
  });
  context.mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const firstStarted = new Promise<void>(resolve => { notify = resolve; });
    const first = request();
    await firstStarted;
    const originalRoute = gateway.services.get("slow");
    gateway.configure([{ ...service, timeoutMs: 60_000 }]);
    assert.equal(gateway.services.get("slow"), originalRoute, "timeout changes never invalidate the route");
    context.mock.timers.tick(300_001);
    assert.equal(forwarded[0]!.signal.aborted, false, "prefill can exceed five minutes without a hidden cutoff");
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    forwarded[0]!.respond(new Response(new ReadableStream<Uint8Array>({ start(controller) {
      stream = controller;
      controller.enqueue(new TextEncoder().encode('data: {"choices":[]}\n\n'));
    } }), { headers: { "content-type": "text/event-stream" } }));
    await new Promise<void>(resolve => setImmediate(resolve));
    context.mock.timers.tick(300_001);
    assert.equal(forwarded[0]!.signal.aborted, false, "a thinking gap can also exceed five minutes");
    stream.close();
    assert.equal((await first).status, 200);

    const secondStarted = new Promise<void>(resolve => { notify = resolve; });
    const second = request();
    await secondStarted;
    context.mock.timers.tick(59_999);
    assert.equal(forwarded[1]!.signal.aborted, false);
    context.mock.timers.tick(1);
    assert.equal((await second).status, 504);
    assert.equal(records.at(-1)!.status, "timeout");
  } finally {
    context.mock.timers.reset();
    await gateway.close();
  }
});

async function upstream(handler: http.RequestListener): Promise<{ url: string; close: () => Promise<void> }> {
  const server = http.createServer(handler);
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as AddressInfo).port;
  return { url: `http://127.0.0.1:${port}/v1`, close: () => new Promise(resolve => server.close(() => resolve())) };
}

async function setup(handler: http.RequestListener, protocol = "chat_completions", options: { concurrency?: number } = {}) {
  const fake = await upstream(handler);
  const records: UsageRecord[] = [], events: (AttemptProgress | { phase: "usage"; attemptId: string })[] = [];
  const gateway = new ModelGateway(record => { records.push(record); events.push({ phase: "usage", attemptId: record.attemptId }); },
    fetch, event => events.push(event));
  gateway.configure([{ id: "svc1", protocol, baseUrl: fake.url, model: "model-a", apiKey: "REAL_KEY", concurrency: options.concurrency }]);
  const port = await gateway.listen();
  gateway.addToken(tokenHash("exec-token"), { plugin: "kyf.test", task: "t1", execution: "e1", account: "acct",
    user: "friend", kind: "background" });
  const base = `http://127.0.0.1:${port}/s/svc1`;
  return { gateway, records, events, base, close: async () => { await gateway.close(); await fake.close(); } };
}

it("injects the real credential, strips the token and meters usage", async () => {
  let seen: http.IncomingHttpHeaders = {};
  const env = await setup((req, res) => {
    seen = req.headers;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ choices: [{ message: { content: "好" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 11, completion_tokens: 3 } }));
  });
  try {
    const response = await fetch(`${env.base}/chat/completions`, { method: "POST",
      headers: { authorization: "Bearer exec-token", "content-type": "application/json", "x-kyf-step": "persona" },
      body: JSON.stringify({ model: "model-a", messages: [] }) });
    assert.equal(response.status, 200);
    assert.ok(response.headers.get("x-kyf-attempt-id"));
    await response.json();
    assert.equal(seen.authorization, "Bearer REAL_KEY");
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(env.records.length, 1);
    const record = env.records[0]!;
    assert.equal(record.status, "ok");
    assert.equal(record.inputTokens, 11);
    assert.equal(record.outputTokens, 3);
    assert.equal(record.step, "persona");
    assert.equal(record.user, "friend");
  } finally { await env.close(); }
});

it("reports an attempt as queued and running before its usage record, then ended", async () => {
  let release: () => void = () => {};
  const held = new Promise<void>(resolve => { release = resolve; });
  const env = await setup(async (_req, res) => {
    await held;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ choices: [{ message: { content: "好" } }], usage: { prompt_tokens: 2, completion_tokens: 1 } }));
  });
  try {
    const pending = fetch(`${env.base}~advisor/chat/completions`, { method: "POST",
      headers: { authorization: "Bearer exec-token" }, body: JSON.stringify({ model: "model-a", messages: [] }) });
    while (!env.events.some(event => event.phase === "running")) await new Promise(resolve => setTimeout(resolve, 5));
    const [queued, running] = env.events as Extract<AttemptProgress, { phase: "queued" | "running" }>[];
    assert.equal(queued!.phase, "queued"); assert.equal(running!.phase, "running");
    assert.equal(queued!.attemptId, running!.attemptId);
    assert.deepEqual([running!.plugin, running!.task, running!.account, running!.user, running!.scope, running!.step, running!.model],
      ["kyf.test", "t1", "acct", "friend", "friend", "advisor", "model-a"]);
    assert.equal(env.records.length, 0);
    release();
    const response = await pending; assert.equal(response.status, 200); await response.json();
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.deepEqual(env.events.map(event => event.phase), ["queued", "running", "usage", "ended"]);
    assert.equal(env.records[0]!.attemptId, queued!.attemptId);
    assert.equal(env.records[0]!.at, running!.at);
  } finally { release(); await env.close(); }
});

it("keeps account-scoped self-analysis attempts and final usage associated with self", async () => {
  const env = await setup((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ choices: [{ message: { content: "好" } }], usage: { prompt_tokens: 2, completion_tokens: 1 } }));
  });
  try {
    env.gateway.addToken(tokenHash("self-token"), { plugin: "kyf.self-analysis", task: "self-task", execution: "self-exec",
      account: "acct", user: null, scope: "self" });
    const response = await fetch(`${env.base}~profile-draft/chat/completions`, { method: "POST",
      headers: { authorization: "Bearer self-token" }, body: JSON.stringify({ model: "model-a", messages: [] }) });
    assert.equal(response.status, 200); await response.json();
    await new Promise(resolve => setTimeout(resolve, 20));
    const progress = env.events.filter(event => event.phase === "queued" || event.phase === "running") as
      Extract<AttemptProgress, { phase: "queued" | "running" }>[];
    assert.deepEqual(progress.map(event => [event.scope, event.user]), [["self", null], ["self", null]]);
    assert.deepEqual([env.records[0]!.scope, env.records[0]!.user, env.records[0]!.step], ["self", null, "profile-draft"]);
  } finally { await env.close(); }
});

it("a queued attempt fenced by a route change ends without running or a usage record", async () => {
  let release: () => void = () => {};
  const held = new Promise<void>(resolve => { release = resolve; });
  const env = await setup(async (_req, res) => { await held; res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); },
    "chat_completions", { concurrency: 1 });
  const send = () => fetch(`${env.base}/chat/completions`, { method: "POST",
    headers: { authorization: "Bearer exec-token" }, body: JSON.stringify({ model: "model-a" }) });
  try {
    const first = send();
    while (!env.events.some(event => event.phase === "running")) await new Promise(resolve => setTimeout(resolve, 5));
    const second = send();
    while (env.events.filter(event => event.phase === "queued").length < 2) await new Promise(resolve => setTimeout(resolve, 5));
    const fenced = env.events.filter(event => event.phase === "queued")[1]!.attemptId;
    const route = (env.gateway.services.get("svc1")!);
    env.gateway.configure([{ ...route, baseUrl: route.baseUrl + "/moved" }]);
    release();
    assert.equal((await second).status, 409); await (await first).text();
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.deepEqual(env.events.filter(event => event.attemptId === fenced).map(event => event.phase), ["queued", "ended"]);
    assert.equal(env.records.some(record => record.attemptId === fenced), false);
  } finally { release(); await env.close(); }
});

it("rejects unknown tokens, other models and unconfigured services", async () => {
  const env = await setup((_req, res) => { res.writeHead(200); res.end("{}"); });
  try {
    const noToken = await fetch(`${env.base}/chat/completions`, { method: "POST", body: "{}" });
    assert.equal(noToken.status, 401);
    const wrongModel = await fetch(`${env.base}/chat/completions`, { method: "POST",
      headers: { "x-api-key": "exec-token" }, body: JSON.stringify({ model: "other" }) });
    assert.equal(wrongModel.status, 403);
    const missing = await fetch(env.base.replace("svc1", "nope") + "/chat/completions", { method: "POST",
      headers: { authorization: "Bearer exec-token" }, body: "{}" });
    assert.equal(missing.status, 403);
  } finally { await env.close(); }
});

it("meters streamed Anthropic usage across message_start and message_delta", async () => {
  const env = await setup((_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write('event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":20,"output_tokens":1}}}\n\n');
    res.write('event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"嗨"}}\n\n');
    res.write('event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n');
    res.end('event: message_stop\ndata: {"type":"message_stop"}\n\n');
  }, "anthropic");
  try {
    const response = await fetch(`${env.base}/messages`, { method: "POST", headers: { "x-api-key": "exec-token" },
      body: JSON.stringify({ model: "model-a", stream: true }) });
    await response.text();
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(env.records[0]!.inputTokens, 20);
    assert.equal(env.records[0]!.outputTokens, 7);
    assert.ok(env.records[0]!.firstTextMs !== null);
  } finally { await env.close(); }
});

it("aborts in-flight requests when the execution token is revoked", async () => {
  let release: () => void = () => {};
  const env = await setup((_req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write('data: {"choices":[{"delta":{"content":"a"}}]}\n\n');
    release = () => res.end();
  });
  try {
    env.gateway.addToken(tokenHash("exec-token"), { plugin: "kyf.test", task: "t1", execution: "e1",
      account: "acct", user: null, kind: "background", scope: "multi" });
    const response = await fetch(`${env.base}/chat/completions`, { method: "POST",
      headers: { authorization: "Bearer exec-token" }, body: JSON.stringify({ model: "model-a", stream: true }) });
    const reader = response.body!.getReader();
    await reader.read();
    env.gateway.revokeToken(tokenHash("exec-token"));
    await assert.rejects(async () => { for (;;) { const next = await reader.read(); if (next.done) break; } });
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(env.records[0]!.status, "cancelled");
    assert.equal(env.records[0]!.scope, "multi");
    assert.equal(env.records[0]!.execution, "e1");
    const after = await fetch(`${env.base}/chat/completions`, { method: "POST",
      headers: { authorization: "Bearer exec-token" }, body: JSON.stringify({ model: "model-a" }) });
    assert.equal(after.status, 401);
  } finally { release(); await env.close(); }
});

it("shares a service fairly between plugins and promotes waiting background work", async () => {
  let now = 0;
  const queue = new ServiceQueue(1, () => now);
  const order: string[] = [];
  const first = await queue.acquire("a", false);
  const waits = [queue.acquire("a", false).then(release => { order.push("a2"); return release; }),
    queue.acquire("b", false).then(release => { order.push("b1"); return release; }),
    queue.acquire("c", true).then(release => { order.push("c-interactive"); return release; })];
  first();
  (await waits[2])();
  (await Promise.race([waits[0], waits[1]]))();
  now += AGING_MS;
  const rest = await Promise.all(waits);
  rest.forEach(release => release());
  assert.equal(order[0], "c-interactive");
  assert.deepEqual(new Set(order.slice(1)), new Set(["a2", "b1"]));
});

it("lets any number of requests wait, cleaning cancelled tickets and abort listeners", async () => {
  const queue = new ServiceQueue(1);
  const active = await queue.acquire("active", false);
  const cancelled: Array<{ controller: AbortController; settled: Promise<void> }> = [];
  for (let index = 0; index < 200; index++) {
    const controller = new AbortController();
    const settled = assert.rejects(queue.acquire(`plugin-${index % 3}`, false, controller.signal), { name: "AbortError" });
    cancelled.push({ controller, settled });
    assert.equal(getEventListeners(controller.signal, "abort").length, 1);
  }
  assert.equal(queue.stats().waiting, 200, "no plugin or service cap refuses a waiting request");
  for (const item of cancelled) item.controller.abort();
  await Promise.all(cancelled.map(item => item.settled));
  for (const item of cancelled) assert.equal(getEventListeners(item.controller.signal, "abort").length, 0);
  assert.equal(queue.stats().queuedPlugins, 0);
  const freshSignal = new AbortController().signal;
  const fresh = queue.acquire("healthy-plugin", true, freshSignal);
  active();
  const release = await fresh;
  assert.equal(getEventListeners(freshSignal, "abort").length, 0, "a granted ticket drops its queue abort listener");
  release(); release();
  assert.deepEqual(queue.stats(), { running: 0, waiting: 0, queuedPlugins: 0, runningPlugins: 0 });
});

it("never expires a waiting request, however long it waits", async () => {
  let now = 0;
  const queue = new ServiceQueue(1, () => now);
  const active = await queue.acquire("busy", false);
  const waiting = queue.acquire("waiting", false);
  now = 30 * 24 * 3_600_000;
  active();
  const release = await waiting;
  assert.equal(queue.stats().running, 1);
  release();
  assert.deepEqual(queue.stats(), { running: 0, waiting: 0, queuedPlugins: 0, runningPlugins: 0 });
});

it("another plugin gets a turn while a noisy plugin has many waiting", async () => {
  const queue = new ServiceQueue(2, () => Date.now());
  const first = await queue.acquire("noisy", false);
  const busy = await queue.acquire("busy", false);
  const order: string[] = [];
  const noisy = [0, 1, 2, 3].map(index => queue.acquire("noisy", false).then(release => { order.push(`n${index}`); return release; }));
  const healthy = queue.acquire("healthy", true);
  first();
  const healthyRelease = await healthy;
  assert.equal(queue.stats().running, 2, "the interactive plugin gets the next shared slot");
  busy(); healthyRelease();
  for (const wait of noisy) (await wait)();
  assert.deepEqual(order, ["n0", "n1", "n2", "n3"]);
  assert.deepEqual(queue.stats(), { running: 0, waiting: 0, queuedPlugins: 0, runningPlugins: 0 });
});

it("forwards every queued request in turn instead of refusing it; another service keeps running", async () => {
  let endFirst = () => {};
  let upstreamRequests = 0;
  const env = await setup((req, res) => {
    upstreamRequests++;
    res.writeHead(200, { "content-type": "application/json" });
    if (req.url?.endsWith("/fast") || upstreamRequests > 2) { res.end('{"usage":{"prompt_tokens":2,"completion_tokens":1}}'); return; }
    res.write('{"usage":{"prompt_tokens":3,');
    endFirst = () => res.end('"completion_tokens":1}}');
  }, "chat_completions", { concurrency: 1 });
  const post = (base: string, token = "exec-token", route = "chat/completions") => fetch(`${base}/${route}`, {
    method: "POST", headers: { authorization: `Bearer ${token}` }, body: '{"model":"model-a"}' });
  try {
    const service = env.gateway.services.get("svc1")!;
    env.gateway.configure([service, { ...service, id: "svc2" }]);
    env.gateway.addToken(tokenHash("queued-token"), { plugin: "kyf.test", task: "queued-task", execution: "queued-execution" });
    const first = await post(env.base);
    const pending = Array.from({ length: 10 }, () => post(env.base, "queued-token"));
    const independent = await post(env.base.replace("svc1", "svc2"), "exec-token", "fast");
    assert.equal(independent.status, 200); await independent.text();
    assert.equal(upstreamRequests, 2, "queued requests wait for the busy service's only slot");
    endFirst(); await first.text();
    const served = await Promise.all(pending);
    assert.deepEqual(served.map(response => response.status), served.map(() => 200));
    await Promise.all(served.map(response => response.text()));
    assert.equal(upstreamRequests, 12);
    assert.equal(env.records.length, 12);
  } finally { endFirst(); await env.close(); }
});

for (const routeState of ["removed", "replaced"] as const) {
  it(`never forwards a queued request after its service is ${routeState}`, async context => {
    let queued!: () => void;
    const admittedToQueue = new Promise<void>(resolve => { queued = resolve; });
    let grant!: (release: () => void) => void;
    let released = 0;
    context.mock.method(ServiceQueue.prototype, "acquire", () => {
      queued();
      return new Promise<() => void>(resolve => { grant = resolve; });
    });
    let forwarded = 0;
    const records: UsageRecord[] = [];
    const gateway = new ModelGateway(record => records.push(record), (async () => {
      forwarded++;
      return new Response("{}");
    }) as typeof fetch);
    const service = { id: "queued-fixture", protocol: "chat_completions", model: "local-default",
      baseUrl: "http://127.0.0.1:1/v1", apiKey: "synthetic-key", stamp: "route-one" };
    gateway.configure([service]);
    gateway.addToken(tokenHash("queued-fixture-token"), { plugin: "fixture", task: "queued", execution: "queued" });
    const port = await gateway.listen();
    try {
      const pending = fetch(`http://127.0.0.1:${port}/s/queued-fixture/chat/completions`, {
        method: "POST", headers: { authorization: "Bearer queued-fixture-token" }, body: '{"model":"local-default"}' });
      await admittedToQueue;
      gateway.configure(routeState === "removed" ? [] : [{ ...service, stamp: "route-two" }]);
      grant(() => { released++; });
      const response = await pending;
      assert.equal(response.status, routeState === "removed" ? 503 : 409);
      assert.equal(response.headers.has("x-kyf-attempt-id"), false);
      const body = await response.json() as { error: { code: string } };
      assert.equal(body.error.code, routeState === "removed" ? "service-unavailable" : "service-changed");
      assert.equal(forwarded, 0);
      assert.equal(records.length, 0, "an obsolete queued request is not a billed attempt");
      assert.equal(released, 1, "reconfiguration still releases the granted queue slot");
    } finally { await gateway.close(); }
  });
}
