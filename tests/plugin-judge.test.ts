// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import http from "node:http";
import { test } from "node:test";
import { judge, WorkerError } from "../plugin-sdk/node/model-worker";
import { ModelGateway, tokenHash, type UsageRecord } from "../electron/model-gateway";
import { UsageMeter } from "../electron/model-gateway-meter";
import { normalizedUsage } from "../electron/model-traces";

const questions = {
  has_content: { type: "noul", instructions: "是否提及本人经历？" },
  category: { type: "choice", instructions: "属于哪类？", criteria: { event: "具体事件", greeting: null } },
  importance: { type: "score", instructions: "重要程度？", criteria: ["低", "中", "高"] },
};
const answers = {
  has_content: { type: "noul", noul: 0.8 },
  category: { type: "choice", choice: "event", probabilities: { event: 0.8, greeting: 0.2 }, confidence: 0.6 },
  importance: { type: "score", score: 1.4, legend: { "0": "低", "1": "中", "2": "高" },
    probabilities: { "0": 0.1, "1": 0.4, "2": 0.5 }, confidence: 0.3 },
};
const payload = (protocol = "systemone") => ({ service: { url: "http://127.0.0.1:1/s/synthetic", id: "synthetic",
  model: "synthetic", protocol, fingerprint: "model-fingerprint", roles: ["judge"] }, token: "execution-token",
  step: "judge-window-filter", state: "昨天我和小王一起搬家。", questions });
const signal = () => new AbortController().signal;
const fails = (code: string) => (error: unknown) => error instanceof WorkerError && error.code === code;

test("judge transports agree, use task-token gateway, and meter decision input without generated output", async () => {
  const seen: { path: string; authorization?: string; body: any }[] = [];
  const provider = http.createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    seen.push({ path: request.url!, authorization: request.headers.authorization, body: JSON.parse(body) });
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify(request.url === "/v1/systemone" ? { answers, usage: { input_tokens: 30 } } : {
      choices: [{ message: { content: JSON.stringify({ answers }) }, finish_reason: "stop" }],
      usage: { prompt_tokens: 40, completion_tokens: 20 },
    }));
  });
  await new Promise<void>(resolve => provider.listen(0, "127.0.0.1", resolve));
  const address = provider.address() as { port: number };
  const records: UsageRecord[] = [];
  const gateway = new ModelGateway(record => records.push(record));
  gateway.configure(["systemone", "chat_completions"].map((protocol, index) => ({ id: `model${index}`, protocol,
    baseUrl: `http://127.0.0.1:${address.port}${index ? "/v1" : ""}`, model: "synthetic", apiKey: "provider-key" })));
  gateway.addToken(tokenHash("execution-token"), { plugin: "test", task: "task1", execution: "run1" });
  const port = await gateway.listen();
  try {
    const results = [];
    for (const [index, protocol] of ["systemone", "chat_completions"].entries()) {
      const call = payload(protocol);
      call.service.url = `http://127.0.0.1:${port}/s/model${index}`;
      results.push(await judge(call, signal()));
    }
    assert.deepEqual(results.map(result => result.answers), [answers, answers]);
    assert.deepEqual(results.map(result => result.engine), ["systemone", "generate"]);
    assert.deepEqual(results.map(result => result.fingerprint), ["model-fingerprint", "model-fingerprint"]);
    assert.deepEqual(seen.map(request => request.path), ["/v1/systemone", "/v1/chat/completions"]);
    assert.ok(seen.every(request => request.authorization === "Bearer provider-key"));
    assert.deepEqual(seen[0]!.body.questions, questions);
    assert.equal(seen[0]!.body.state, payload().state);
    assert.equal(seen[1]!.body.stream, true);
    assert.equal(records.length, 2);
    assert.deepEqual(records.map(record => [record.protocol, record.step, record.inputTokens, record.outputTokens]),
      [["systemone", "judge-window-filter", 30, 0], ["chat_completions", "judge-window-filter", 40, 20]]);
    assert.equal(records[0]!.totalTokens, 30);
  } finally {
    await gateway.close(); provider.closeAllConnections();
    await new Promise<void>(resolve => provider.close(() => resolve()));
  }
});

test("decision usage is counted by both the live meter and diagnostic normalizer", () => {
  const meter = new UsageMeter("application/json", "systemone");
  meter.push(Buffer.from(JSON.stringify({ answers, usage: { input_tokens: 123, output_tokens: 0 } })));
  const usage = meter.finish()!;
  assert.equal(usage.inputTokens, 123); assert.equal(usage.outputTokens, 0); assert.equal(usage.totalTokens, 123);
  assert.equal(normalizedUsage({ input_tokens: 123 }, "systemone")!.totalTokens, 123);
});

test("judge rejects malformed answers from either protocol instead of returning trusted decisions", async context => {
  const bad: unknown[] = [null, {}, { ...answers, extra: { noul: 1 } },
    { ...answers, has_content: { noul: 0.8 } },
    { ...answers, has_content: { noul: true } }, { ...answers, has_content: { noul: -0.1 } },
    { ...answers, has_content: { noul: 1.1 } }, { ...answers, has_content: { type: "choice", noul: 1 } },
    { ...answers, category: { ...answers.category, probabilities: { event: 1 } } },
    { ...answers, category: { ...answers.category, probabilities: { event: 0.8, greeting: 0.3 } } },
    { ...answers, category: { ...answers.category, choice: "greeting" } },
    { ...answers, category: { ...answers.category, confidence: "0.6" } },
    { ...answers, importance: { ...answers.importance, score: 2.1 } },
    { ...answers, importance: { ...answers.importance, score: 0.1 } },
    { ...answers, importance: { ...answers.importance, legend: { "0": "wrong", "1": "中", "2": "高" } } }];
  let body: unknown;
  context.mock.method(globalThis, "fetch", async (_url: unknown, init: any) => {
    const request = JSON.parse(init.body);
    return new Response(JSON.stringify(request.messages ? { choices: [{ message: { content: JSON.stringify({ answers: body }) }, finish_reason: "stop" }] } : { answers: body }),
      { headers: { "content-type": "application/json" } });
  });
  for (const protocol of ["systemone", "chat_completions"]) for (const candidate of bad) {
    body = candidate;
    await assert.rejects(judge(payload(protocol), signal()), fails("judge-invalid-output"));
  }
});

test("judge validates bounded state/questions before any model request and excludes roles from fallback identity", async context => {
  let count = 0;
  context.mock.method(globalThis, "fetch", async () => { count++; return new Response(JSON.stringify({ answers })); });
  await assert.rejects(judge({ ...payload(), state: "中".repeat(6001) }, signal()), fails("judge-state-too-long"));
  await assert.rejects(judge({ ...payload(), service: undefined }, signal()), fails("judge-not-configured"));
  for (const value of [{}, Array(17).fill(questions.has_content), { q: { type: "choice", instructions: "?", criteria: { a: null } } },
    { q: { type: "score", instructions: "?", criteria: ["one"] } }, { q: { type: "noul", instructions: "?", criteria: { true: "yes" } } }])
    await assert.rejects(judge({ ...payload(), questions: value }, signal()), fails("invalid-request"));
  assert.equal(count, 0);
  const first = payload(), second = payload();
  delete (first.service as any).fingerprint; delete (second.service as any).fingerprint;
  second.service.roles = ["judge", "image"];
  second.service.url = "http://127.0.0.1:2/s/synthetic";
  assert.equal((await judge(first, signal())).fingerprint, (await judge(second, signal())).fingerprint);
});

test("judge bounds native responses and hides provider diagnostics", async context => {
  let response = () => new Response("x".repeat(1024 * 1024 + 1));
  context.mock.method(globalThis, "fetch", async () => response());
  await assert.rejects(judge(payload(), signal()), fails("judge-invalid-output"));
  response = () => new Response(JSON.stringify({ error: { message: "PRIVATE_PROVIDER_DETAIL execution-token" } }), { status: 501 });
  await assert.rejects(judge(payload(), signal()), (error: unknown) => {
    assert.ok(error instanceof WorkerError); assert.equal(error.code, "judge-unavailable"); assert.equal(error.httpStatus, 501);
    assert.ok(!error.message.includes("PRIVATE_PROVIDER_DETAIL")); assert.ok(!error.message.includes("execution-token")); return true;
  });
  response = () => new Response(JSON.stringify({ error: { code: "execution-revoked" } }), { status: 409 });
  await assert.rejects(judge(payload(), signal()), fails("cancelled"));
  for (const detail of [{ code: 400, type: "exceed_context_size_error", message: "PRIVATE_CONTEXT" },
    { code: 500, type: "server_error", message: "input (11778 tokens) is too large to process. increase the physical batch size (current batch size: 8192)" }]) {
    response = () => new Response(JSON.stringify({ error: detail }), { status: 503 });
    await assert.rejects(judge(payload(), signal()), fails("judge-state-too-long"));
  }
});

test("aborting an active judge request preserves cancellation", async context => {
  let admitted!: () => void;
  context.mock.method(globalThis, "fetch", async (_url: unknown, init: RequestInit) => new Promise((_resolve, reject) => {
    init.signal!.addEventListener("abort", () => reject(new Error("private diagnostics")), { once: true }); admitted();
  }));
  for (const protocol of ["systemone", "chat_completions"]) {
    const controller = new AbortController();
    const started = new Promise<void>(resolve => { admitted = resolve; });
    const pending = judge(payload(protocol), controller.signal);
    await started; controller.abort();
    await assert.rejects(pending, fails("cancelled"));
    await assert.rejects(judge(payload(protocol), controller.signal), fails("cancelled"));
  }
});

test("revoking an execution token stops an active native judge call through the gateway", async () => {
  let started!: () => void;
  const admitted = new Promise<void>(resolve => { started = resolve; });
  const records: UsageRecord[] = [];
  const gateway = new ModelGateway(record => records.push(record), (async (_url: unknown, init: RequestInit) => new Promise((_resolve, reject) => {
    init.signal!.addEventListener("abort", () => reject(new Error("aborted")), { once: true }); started();
  })) as typeof fetch);
  gateway.configure([{ id: "synthetic", protocol: "systemone", model: "synthetic", baseUrl: "http://127.0.0.1:1" }]);
  gateway.addToken(tokenHash("execution-token"), { plugin: "test", task: "task", execution: "run" });
  const port = await gateway.listen();
  try {
    const call = payload(); call.service.url = `http://127.0.0.1:${port}/s/synthetic`;
    const pending = judge(call, signal());
    await admitted; gateway.revokeToken(tokenHash("execution-token"));
    await assert.rejects(pending, fails("cancelled"));
    assert.equal(records[0]!.status, "cancelled");
  } finally { await gateway.close(); }
});
