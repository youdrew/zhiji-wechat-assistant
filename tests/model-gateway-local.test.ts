// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import { strict as assert } from "node:assert";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import path from "node:path";
import type http from "node:http";
import type { ChildProcess, spawn } from "node:child_process";
import { LocalJudgeRuntime, type LocalJudgeConfig } from "../electron/model-gateway-local";
import { ModelGateway, tokenHash, type GatewayService } from "../electron/model-gateway";
import { configureModelTraceProducer, withModelTraceContext } from "../electron/model-traces";

const config: LocalJudgeConfig = { modelPath: path.resolve("synthetic.gguf"),
  runtimePath: path.resolve("llama-server.exe"), threads: 2, contextTokens: 8192, idleMs: 20 };
class Child extends EventEmitter {
  exitCode: number | null = null;
  killed = false;
  kill(): boolean { this.killed = true; this.exitCode = 0; this.emit("exit", 0); return true; }
}
function harness(fetcher: typeof fetch = async () => new Response("{}")) {
  const children: Child[] = [], args: string[][] = [];
  const launcher = ((_command: string, argv: string[], options: any) => {
    assert.equal(options.windowsHide, true);
    assert.equal(options.stdio, "ignore");
    const child = new Child(); children.push(child); args.push(argv);
    return child as unknown as ChildProcess;
  }) as typeof spawn;
  return { runtime: new LocalJudgeRuntime(fetcher, launcher), children, args };
}
test("local runtime is lazy, binds loopback, protects the API, bounds batches and expires while idle", async () => {
  const h = harness();
  assert.equal(h.children.length, 0);
  const endpoint = await h.runtime.resolve(config, new AbortController().signal);
  assert.match(endpoint.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/);
  assert.equal(endpoint.apiKey.length, 64);
  const args = h.args[0]!;
  for (const [flag, value] of [["--host", "127.0.0.1"], ["--api-key", endpoint.apiKey], ["-np", "1"],
    ["-t", "2"], ["-ub", "8192"], ["-b", "8192"], ["--prio", "-1"], ["--alias", "julia-1"]]) {
    assert.equal(args[args.indexOf(flag!) + 1], value);
  }
  assert.equal((await h.runtime.resolve(config, new AbortController().signal)).apiKey, endpoint.apiKey);
  assert.equal(h.children.length, 1);
  h.runtime.release(config); await delay(60);
  assert.equal(h.children[0]!.killed, true);
  const next = await h.runtime.resolve(config, new AbortController().signal);
  assert.notEqual(next.apiKey, endpoint.apiKey);
  await h.runtime.close(); assert.equal(h.children[1]!.killed, true);
  await assert.rejects(h.runtime.resolve(config, new AbortController().signal));
});
test("revocation while loading terminates the owned child, without restarting cancelled work", async () => {
  let entered!: () => void;
  const begun = new Promise<void>(resolve => { entered = resolve; });
  const h = harness(async (_url, init) => {
    entered();
    await delay(60_000, undefined, { signal: init!.signal! });
    return new Response("{}");
  });
  const controller = new AbortController();
  const loading = h.runtime.resolve(config, controller.signal);
  await begun; controller.abort();
  await assert.rejects(loading);
  assert.equal(h.children.length, 1); assert.equal(h.children[0]!.killed, true);
  await h.runtime.close();
});
test("close and explicit stop fence in-progress startup and leave no new child", async () => {
  for (const operation of ["close", "cancel"] as const) {
    let entered!: () => void;
    const begun = new Promise<void>(resolve => { entered = resolve; });
    const h = harness(async (_url, init) => {
      entered(); await delay(60_000, undefined, { signal: init!.signal! }); return new Response("{}");
    });
    const loading = h.runtime.resolve(config, new AbortController().signal);
    const rejected = assert.rejects(loading);
    await begun;
    await h.runtime[operation](); await rejected;
    assert.equal(h.children.length, 1); assert.equal(h.children[0]!.killed, true);
    await h.runtime.close();
  }
});
test("a different configuration cannot borrow an already-starting process", async () => {
  let entered!: () => void, complete!: () => void;
  const begun = new Promise<void>(resolve => { entered = resolve; });
  const gate = new Promise<void>(resolve => { complete = resolve; });
  const h = harness(async () => { entered(); await gate; return new Response("{}"); });
  const first = h.runtime.resolve(config, new AbortController().signal);
  await begun;
  await assert.rejects(h.runtime.resolve({ ...config, modelPath: path.resolve("another.gguf") }, new AbortController().signal));
  const cancelled = new AbortController();
  const second = h.runtime.resolve(config, cancelled.signal); cancelled.abort();
  await assert.rejects(second);
  complete(); await first; await h.runtime.close();
});

class SyntheticRuntime extends LocalJudgeRuntime {
  starts = 0; stops = 0;
  override async resolve(): Promise<{ baseUrl: string; apiKey: string }> { this.starts++; return { baseUrl: "http://local.synthetic", apiKey: "local-only" }; }
  override release(): void {}
  override async cancel(): Promise<void> { this.stops++; }
  override async close(): Promise<void> { this.stops++; }
}
test("native calls and synthetic selftest share one fair queue, and stop cancels local work only", async () => {
  const runtime = new SyntheticRuntime();
  let active = 0, maximum = 0;
  const localFetcher: typeof fetch = async (_url, init) => {
    active++; maximum = Math.max(maximum, active);
    try { await delay(20, undefined, { signal: init!.signal! }); }
    finally { active--; }
    return new Response(JSON.stringify({ answers: { has_content: { type: "noul", noul: 0.5 } },
      usage: { input_tokens: 25, output_tokens: 0 } }), { headers: { "content-type": "application/json" } });
  };
  const gateway = new ModelGateway(() => undefined, localFetcher, undefined, runtime);
  const service: GatewayService = { id: "judge", protocol: "systemone", baseUrl: "http://127.0.0.1", model: "julia-1",
    concurrency: 8, roles: ["judge"], local: config };
  gateway.configure([service]); gateway.addToken(tokenHash("synthetic-token"), { plugin: "test", task: "t", execution: "e" });
  const port = await gateway.listen();
  const send = (rest = "v1/systemone") => fetch(`http://127.0.0.1:${port}/s/judge/${rest}`, { method: "POST",
    headers: { authorization: "Bearer synthetic-token", "content-type": "application/json" },
    body: JSON.stringify({ model: "julia-1", state: "synthetic", questions: {} }) });
  try {
    const selftest = gateway.selftestLocalJudge(config, new AbortController().signal);
    const [a, b, result] = await Promise.all([send(), send(), selftest]);
    assert.equal(a.status, 200); assert.equal(b.status, 200); assert.equal(maximum, 1);
    assert.equal((result as any).qualityVerified, false);
    assert.equal((await send("props")).status, 403);
    const main = { id: "main", protocol: "chat_completions", model: "m", baseUrl: "http://main.synthetic" };
    gateway.configure([main, service]);
    const mainCall = fetch(`http://127.0.0.1:${port}/s/main/v1/chat/completions`, { method: "POST",
      headers: { authorization: "Bearer synthetic-token", "content-type": "application/json" }, body: "{}" });
    const localCalls = [send(), send()];
    await delay(5); await gateway.stopLocalJudge();
    assert.equal((await mainCall).status, 200);
    const responses = await Promise.all(localCalls);
    assert.ok(responses.every(response => response.status >= 400));
  } finally { await gateway.close(); }
});
test("a roles-only edit refreshes metadata without invalidating a route reference", () => {
  const gateway = new ModelGateway(() => undefined);
  const service = { id: "main", protocol: "chat_completions", model: "m", baseUrl: "http://test", roles: ["main"] };
  gateway.configure([service]); const prior = gateway.services.get("main");
  gateway.configure([{ ...service, roles: ["main", "judge"] }]);
  assert.equal(gateway.services.get("main"), prior);
  assert.deepEqual(prior!.roles, ["main", "judge"]);
});
test("a native startup failure produces a failed call record without reporting token usage", async () => {
  class BrokenRuntime extends SyntheticRuntime {
    override async resolve(): Promise<never> { throw new Error("synthetic broken model"); }
  }
  const records: any[] = [];
  const gateway = new ModelGateway(record => records.push(record), undefined, undefined, new BrokenRuntime());
  gateway.configure([{ id: "judge", protocol: "systemone", baseUrl: "http://127.0.0.1", model: "julia-1", local: config }]);
  gateway.addToken(tokenHash("synthetic"), { plugin: "test", task: "t", execution: "e" });
  const port = await gateway.listen();
  try {
    const response = await fetch(`http://127.0.0.1:${port}/s/judge/v1/systemone`, { method: "POST",
      headers: { authorization: "Bearer synthetic", "content-type": "application/json" }, body: "{}" });
    assert.equal(response.status, 502); assert.equal((await response.json() as any).error.code, "judge-unavailable");
    assert.equal(records.length, 1); assert.equal(records[0].status, "judge-unavailable");
    assert.equal(records[0].inputTokens, null); assert.equal(records[0].outputTokens, null);
    assert.equal(JSON.stringify(records).includes("synthetic broken model"), false);
  } finally { await gateway.close(); }
});
test("a crash retry starts a new trace with the new private endpoint and redacts the new credential", async context => {
  const events: Record<string, any>[] = [];
  configureModelTraceProducer({ state: () => ({ collecting: true, collectionVersion: 1, deleteVersion: 1 }),
    emit: (_scope, event) => { events.push(event); return true; } });
  context.after(() => configureModelTraceProducer());
  class RestartedRuntime extends SyntheticRuntime {
    override alive(): boolean { return false; }
    override async resolve(): Promise<{ baseUrl: string; apiKey: string }> {
      return { baseUrl: "http://127.0.0.1:54321", apiKey: "synthetic-new-local-key" };
    }
  }
  let calls = 0;
  const gateway = new ModelGateway(() => undefined, async (url, init) => {
    calls++;
    if (calls === 1) throw new Error("synthetic local process exited");
    assert.equal(String(url), "http://127.0.0.1:54321/v1/systemone");
    assert.equal((init!.headers as any).authorization, "Bearer synthetic-new-local-key");
    return new Response(JSON.stringify({ echo: "synthetic-new-local-key", input: "synthetic-task-token",
      usage: { input_tokens: 12, output_tokens: 0 } }), { headers: { "content-type": "application/json" } });
  }, undefined, new RestartedRuntime());
  const service = { id: "judge", protocol: "systemone", baseUrl: "http://127.0.0.1:54320", model: "julia-1",
    apiKey: "synthetic-old-local-key", local: config };
  const req = { headers: { authorization: "Bearer synthetic-task-token" } } as http.IncomingMessage;
  const res = { writeHead() {}, write() { return true; }, end() {}, destroy() {} } as unknown as http.ServerResponse;
  await withModelTraceContext({ producerId: "synthetic", taskId: "synthetic-retry", executionId: "synthetic", source: "test" },
    () => gateway["forward"](req, res, service,
      { attemptId: "synthetic-retry", plugin: "test", task: "t", execution: "e", service: "judge", protocol: "systemone", model: "julia-1", step: "judge" },
      new URL("http://127.0.0.1/s/judge/v1/systemone"), "v1/systemone", Buffer.from("{}"), "POST", new AbortController(), 60_000));
  const starts = events.filter(event => event.type === "start");
  assert.equal(starts.length, 2);
  assert.equal(starts[1]!.meta.retryOfRecordId, starts[0]!.recordId);
  assert.equal(starts[1]!.meta.retryReason, "judge-process-exited");
  assert.equal(starts[1]!.meta.endpoint, "http://127.0.0.1:54321/v1/systemone");
  for (const secret of ["synthetic-new-local-key", "synthetic-old-local-key", "synthetic-task-token"])
    assert.equal(JSON.stringify(events).includes(secret), false);
  assert.equal(calls, 2);
  await gateway.close();
});
