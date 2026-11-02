// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Core model gateway: per-service loopback endpoints in each provider's own protocol.
// Plugins present an execution token; the gateway checks it, injects the real
// credential, queues fairly, meters every attempt and aborts revoked work.
import http from "node:http";
import { createHash, randomUUID } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import type { AddressInfo } from "node:net";
import { ServiceQueue, QueueAborted, modelConcurrency } from "./model-gateway-queue";
import { UsageMeter, type MeteredUsage } from "./model-gateway-meter";
import { beginModelTraceAttempt, createModelTraceResponseCapture, withModelTraceStep } from "./model-traces";
import { withRemoteModelTrace } from "./model-trace-client";
import { DEFAULT_MODEL_TIMEOUT_MS, modelRequestInit, modelTimeoutMs } from "./model-timeout";
import { LocalJudgeRuntime, type LocalJudgeConfig } from "./model-gateway-local";
import judgeSelftest from "../scripts/judge-selftest.json";

export const MAX_REQUEST_BYTES = 32 * 1024 * 1024;
// Compatibility exports: all inference phases share one configured total limit.
export const FIRST_BYTE_MS = DEFAULT_MODEL_TIMEOUT_MS;
export const IDLE_MS = DEFAULT_MODEL_TIMEOUT_MS;
export const TOTAL_MS = DEFAULT_MODEL_TIMEOUT_MS;
const TOKEN_HEADERS = ["authorization", "x-api-key", "x-goog-api-key", "api-key"];
const FORWARD_HEADERS = ["content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta", "user-agent"];
const DROP_RESPONSE = new Set(["content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"]);

export interface GatewayService {
  id: string; protocol: string; baseUrl: string; model: string; apiKey?: string; contextTokens?: number;
  concurrency?: number;
  timeoutMs?: number;
  /** Purposes this service answers; never part of the route identity. */
  roles?: string[];
  /** Trusted Core-only local process configuration; never serialized to plugins. */
  local?: LocalJudgeConfig;
}
export interface GatewayBinding {
  plugin: string; pluginVersion?: string; task: string; execution: string; generation?: number;
  account?: string; user?: string | null; kind?: string; scope?: "friend" | "multi" | "none" | "self"; trace?: unknown;
}
export interface UsageRecord extends MeteredUsage {
  attemptId: string; at: number; plugin: string; pluginVersion?: string; task: string; execution: string;
  account?: string; user?: string | null; kind?: string; service: string; protocol: string; model: string;
  step: string; status: string; httpStatus: number | null; durationMs: number; firstByteMs: number | null;
  firstTextMs: number | null; scope?: "friend" | "multi" | "none" | "self"; traceRecordId?: string;
}
type AttemptFields = Pick<UsageRecord, "attemptId" | "plugin" | "pluginVersion" | "task" | "execution" | "account" | "user" |
  "kind" | "scope" | "service" | "protocol" | "model" | "step">;
/** An attempt that has not produced its usage record yet: waiting for capacity, then forwarding. */
export type AttemptProgress = (AttemptFields & { phase: "queued" | "running"; at: number; traceRecordId?: string }) |
  { attemptId: string; phase: "ended" };

export function tokenHash(token: string): string { return createHash("sha256").update(token, "utf8").digest("hex"); }

function presentedToken(headers: http.IncomingHttpHeaders): string | undefined {
  for (const name of TOKEN_HEADERS) {
    const raw = headers[name];
    const value = Array.isArray(raw) ? raw[0] : raw;
    if (typeof value === "string" && value.trim()) return name === "authorization" ? value.replace(/^Bearer\s+/iu, "").trim() : value.trim();
  }
  return undefined;
}

function credentialHeaders(service: GatewayService): Record<string, string> {
  const key = service.apiKey ?? "";
  if (!key) return {};
  if (service.protocol === "anthropic") return { "x-api-key": key };
  if (service.protocol === "gemini") return { "x-goog-api-key": key };
  return { authorization: `Bearer ${key}` };
}

function modelAllowed(service: GatewayService, rest: string, body: Buffer, method: string): boolean {
  if (method === "GET") return true;
  if (service.protocol === "gemini") {
    const match = /(?:^|\/)models\/([^/:]+):/u.exec(rest);
    return !match || decodeURIComponent(match[1]!) === service.model;
  }
  try {
    const parsed = JSON.parse(body.toString("utf8"));
    return !parsed || typeof parsed.model !== "string" || parsed.model === service.model;
  } catch { return true; }
}

function reply(res: http.ServerResponse, status: number, code: string, message: string): void {
  if (res.headersSent) { res.destroy(); return; }
  const body = JSON.stringify({ error: { code, message, type: code } });
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(body) });
  res.end(body);
}

function waitForDrain(res: http.ServerResponse, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      res.off("drain", drained);
      res.off("close", stopped);
      res.off("error", stopped);
      signal.removeEventListener("abort", stopped);
    };
    const drained = () => { cleanup(); resolve(); };
    const stopped = () => { cleanup(); reject(signal.reason ?? new Error("client-closed")); };
    res.once("drain", drained);
    res.once("close", stopped);
    res.once("error", stopped);
    signal.addEventListener("abort", stopped, { once: true });
    // A close or revoke may have happened during write(), before these listeners existed.
    if (signal.aborted || res.destroyed || res.writableEnded) stopped();
  });
}

export class ModelGateway {
  readonly services = new Map<string, GatewayService>();
  private readonly queues = new Map<string, ServiceQueue>();
  private readonly bindings = new Map<string, GatewayBinding>();
  private readonly inflight = new Map<string, Set<AbortController>>();
  private readonly localEmbeddingQueue = new ServiceQueue(1, () => Date.now());
  private readonly localEmbeddingCalls = new Set<AbortController>();
  private readonly localJudgeQueue = new ServiceQueue(1, () => Date.now());
  private readonly localJudgeCalls = new Set<AbortController>();
  private server?: http.Server;

  constructor(private readonly emit: (record: UsageRecord) => void,
    private readonly fetcher: typeof fetch = (input, init) => globalThis.fetch(input, modelRequestInit(init ?? {})),
    private readonly progress?: (event: AttemptProgress) => void,
    private readonly localJudge = new LocalJudgeRuntime()) {}

  configure(services: GatewayService[]): void {
    for (const service of services) {
      modelTimeoutMs(service.timeoutMs);
      modelConcurrency(service.concurrency);
    }
    const previous = new Map(this.services);
    this.services.clear();
    for (const service of services) {
      const prior = previous.get(service.id);
      // Capacity/deadline/role edits must not invalidate requests already waiting
      // for this route. Credentials, model identity and other route changes still
      // fence old requests. Roles never enter the route identity (约束 3/5).
      const { concurrency: _nextCapacity, timeoutMs: _nextTimeout, roles: _nextRoles, ...route } = service;
      const { concurrency: _oldCapacity, timeoutMs: _oldTimeout, roles: _oldRoles, ...oldRoute } = prior ?? {};
      if (prior && isDeepStrictEqual(oldRoute, route)) {
        prior.concurrency = service.concurrency;
        prior.timeoutMs = service.timeoutMs;
        prior.roles = service.roles;
        this.services.set(service.id, prior);
      } else this.services.set(service.id, service);
      const queue = this.queues.get(service.id) ?? new ServiceQueue(undefined, () => Date.now());
      queue.configure(modelConcurrency(service.concurrency));
      this.queues.set(service.id, queue);
    }
  }

  addToken(hash: string, binding: GatewayBinding): void { this.bindings.set(hash, binding); }

  /** Local CPU inference is admitted by Core's gateway, with the same fair queue policy. */
  async runLocalEmbedding<T>(work: (signal: AbortSignal) => Promise<T>, plugin: string, interactive: boolean,
    signal?: AbortSignal): Promise<T> {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener("abort", cancel, { once: true });
    if (signal?.aborted) cancel();
    this.localEmbeddingCalls.add(controller);
    let release: (() => void) | undefined;
    try {
      release = await this.localEmbeddingQueue.acquire(plugin, interactive, controller.signal);
      if (controller.signal.aborted) throw new QueueAborted();
      return await work(controller.signal);
    } finally {
      release?.(); this.localEmbeddingCalls.delete(controller);
      signal?.removeEventListener("abort", cancel);
    }
  }

  revokeToken(hash: string): void {
    this.bindings.delete(hash);
    for (const controller of this.inflight.get(hash) ?? []) controller.abort(new Error("execution-revoked"));
    this.inflight.delete(hash);
  }

  async listen(port = 0): Promise<number> {
    this.server = http.createServer((req, res) => { void this.handle(req, res); });
    this.server.requestTimeout = 0;
    this.server.headersTimeout = 60_000;
    await new Promise<void>((resolve, reject) => {
      this.server!.once("error", reject);
      this.server!.listen(port, "127.0.0.1", () => resolve());
    });
    return (this.server.address() as AddressInfo).port;
  }

  async close(): Promise<void> {
    for (const controller of this.localEmbeddingCalls) controller.abort();
    for (const controller of this.localJudgeCalls) controller.abort();
    for (const hash of [...this.inflight.keys()]) this.revokeToken(hash);
    await this.localJudge.close();
    await new Promise<void>(resolve => this.server ? this.server.close(() => resolve()) : resolve());
  }

  async stopLocalJudge(): Promise<void> {
    for (const controller of this.localJudgeCalls) controller.abort();
    await this.localJudge.cancel();
  }

  async selftestLocalJudge(config: LocalJudgeConfig, signal: AbortSignal): Promise<unknown> {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
    this.localJudgeCalls.add(controller);
    let release: (() => void) | undefined;
    try {
      release = await this.localJudgeQueue.acquire("core.judge-selftest", false, controller.signal);
      const endpoint = await this.localJudge.resolve(config, controller.signal);
      const run = async (state: string): Promise<{ probability: number; ms: number }> => {
        const start = performance.now();
        const response = await this.fetcher(`${endpoint.baseUrl}/v1/systemone`, {
          method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${endpoint.apiKey}` },
          body: JSON.stringify({ state, questions: judgeSelftest.questions }),
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)]), redirect: "error" });
        if (!response.ok) { await response.body?.cancel(); throw new Error("judge-unavailable"); }
        const value = await response.json() as any;
        const p = value?.answers?.has_content?.noul;
        if (typeof p !== "number" || !Number.isFinite(p) || p < 0 || p > 1) throw new Error("judge-invalid-output");
        return { probability: p, ms: performance.now() - start };
      };
      await run(judgeSelftest.samples[0]!.state); // Warm-up is excluded from the latency result.
      let total = 0, correct = 0;
      for (const sample of judgeSelftest.samples) {
        const result = await run(sample.state); total += result.ms;
        if ((result.probability >= 0.5) === sample.expected) correct++;
      }
      const latencyMs = total / judgeSelftest.samples.length;
      return { formatValid: true, latencyMs, backgroundOnly: latencyMs > 500,
        qualityVerified: false, correct, samples: judgeSelftest.samples.length };
    } finally {
      if (release) { this.localJudge.release(config); release(); }
      this.localJudgeCalls.delete(controller);
      signal.removeEventListener("abort", cancel);
    }
  }

  private async readBody(req: http.IncomingMessage): Promise<Buffer> {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of req) {
      size += (chunk as Buffer).length;
      if (size > MAX_REQUEST_BYTES) throw new Error("request-too-large");
      chunks.push(chunk as Buffer);
    }
    return Buffer.concat(chunks);
  }

  private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/health") { res.writeHead(200, { "content-type": "application/json" }); res.end("{\"ok\":true}"); return; }
    // /s/<service>[~<step>]/<protocol path>: the optional step labels usage records.
    const match = /^\/s\/([A-Za-z0-9_-]{1,64})(?:~([A-Za-z0-9._-]{1,40}))?\/(.*)$/u.exec(url.pathname);
    if (!match) return reply(res, 404, "not-found", "未知的网关地址");
    const token = presentedToken(req.headers);
    const hash = token ? tokenHash(token) : "";
    const binding = token ? this.bindings.get(hash) : undefined;
    if (!binding) return reply(res, 401, "execution-revoked", "执行令牌无效或任务已停止");
    const service = this.services.get(match[1]!);
    if (!service) return reply(res, 403, "service-not-configured", "模型服务未配置");
    let body: Buffer;
    try { body = await this.readBody(req); } catch { return reply(res, 413, "request-too-large", "请求过大"); }
    const method = (req.method ?? "GET").toUpperCase();
    // Capture at admission: saving a new limit affects future requests, never the
    // already-running request or its queued ticket. Model discovery stays short.
    const timeoutMs = method === "GET" || method === "HEAD" ? 15_000 : modelTimeoutMs(service.timeoutMs);
    const rest = match[3]!;
    if (service.local && (method !== "POST" || rest !== "v1/systemone")) {
      return reply(res, 403, "model-not-allowed", "本地判断模型只接受判断请求");
    }
    if (!modelAllowed(service, rest, body, method)) return reply(res, 403, "model-not-allowed", "请求的模型与设置不一致");
    const step = String(match[2] ?? req.headers["x-kyf-step"] ?? "plugin").replace(/[^a-z0-9._-]/giu, "").slice(0, 40) || "plugin";
    // In-flight attempts are reported before their usage record so the call list can show them.
    const attempt: AttemptFields = { attemptId: randomUUID(), plugin: binding.plugin, pluginVersion: binding.pluginVersion,
      task: binding.task, execution: binding.execution, account: binding.account, user: binding.user ?? null,
      kind: binding.kind, scope: binding.scope ?? (binding.user ? "friend" : binding.plugin === "kyf.self-analysis" ? "self" : "none"), service: service.id,
      protocol: service.protocol, model: service.model, step };
    this.progress?.({ ...attempt, phase: "queued", at: Date.now() });
    const controller = new AbortController();
    if (service.local) this.localJudgeCalls.add(controller);
    const controllers = this.inflight.get(hash) ?? new Set<AbortController>();
    controllers.add(controller);
    this.inflight.set(hash, controllers);
    res.on("close", () => { if (!res.writableEnded) controller.abort(new Error("client-closed")); });
    const queue = service.local ? this.localJudgeQueue : this.queues.get(service.id)!;
    let release: (() => void) | undefined;
    try {
      // Waiting has no cap and no deadline; timeoutMs counts from the forward below.
      release = await queue.acquire(binding.plugin, binding.kind === "interactive", controller.signal);
      if (controller.signal.aborted || this.bindings.get(hash) !== binding) throw new QueueAborted();
      // Admission captured this configuration before waiting. A config.applied
      // acknowledgment must also fence queued requests from the removed/replaced route.
      const currentService = this.services.get(service.id);
      if (!currentService) return reply(res, 503, "service-unavailable", "模型服务正在切换或暂时不可用");
      if (currentService !== service) return reply(res, 409, "service-changed", "模型服务已切换，请重新发起任务");
      if (service.local) {
        // Resolve only after admission. The process and random key never enter
        // public service metadata or a user-controlled endpoint.
        const loadingAt = Date.now();
        let endpoint: { baseUrl: string; apiKey: string };
        try { endpoint = await this.localJudge.resolve(service.local, controller.signal); }
        catch {
          const cancelled = controller.signal.aborted || this.bindings.get(hash) !== binding;
          this.emit({ ...attempt, at: loadingAt, status: cancelled ? "cancelled" : "judge-unavailable",
            httpStatus: null, durationMs: Date.now() - loadingAt, firstByteMs: null, firstTextMs: null,
            accountingVersion: 1, inputTokens: null, outputTokens: null, reasoningTokens: null,
            cacheReadTokens: null, cacheWriteTokens: null, totalInputTokens: null, totalOutputTokens: null,
            totalTokens: null, totalTokensSource: null, toolUsePromptTokens: null });
          return reply(res, cancelled ? 409 : 502, cancelled ? "execution-revoked" : "judge-unavailable",
            cancelled ? "任务已停止" : "本地判断模型暂不可用");
        }
        const localService = { ...service, baseUrl: endpoint.baseUrl, apiKey: endpoint.apiKey };
        try {
          await withRemoteModelTrace(binding.trace, () => withModelTraceStep(`plugin:${binding.plugin}:${step}`,
            () => this.forward(req, res, localService, attempt, url, rest, body, method, controller, timeoutMs)));
        } finally { this.localJudge.release(service.local); }
        return;
      }
      await withRemoteModelTrace(binding.trace, () => withModelTraceStep(`plugin:${binding.plugin}:${step}`,
        () => this.forward(req, res, service, attempt, url, rest, body, method, controller, timeoutMs)));
    } catch (error) {
      if (error instanceof QueueAborted || controller.signal.aborted || this.bindings.get(hash) !== binding) {
        reply(res, 409, "execution-revoked", "任务已停止");
      }
      else if (!res.headersSent) reply(res, 502, "gateway-error", "模型网关转发失败");
    } finally {
      release?.();
      if (service.local) this.localJudgeCalls.delete(controller);
      controllers.delete(controller);
      if (!controllers.size) this.inflight.delete(hash);
      this.progress?.({ attemptId: attempt.attemptId, phase: "ended" });
    }
  }

  private async forward(req: http.IncomingMessage, res: http.ServerResponse, service: GatewayService,
    attempt: AttemptFields, url: URL, rest: string, body: Buffer, method: string,
    controller: AbortController, timeoutMs: number): Promise<void> {
    const started = Date.now();
    const attemptId = attempt.attemptId;
    let target = new URL(`${service.baseUrl.replace(/\/+$/u, "")}/${rest}`);
    for (const [key, value] of url.searchParams) if (key !== "key") target.searchParams.append(key, value);
    const headers: Record<string, string> = {};
    for (const name of FORWARD_HEADERS) {
      const value = req.headers[name];
      if (typeof value === "string") headers[name] = value;
    }
    Object.assign(headers, credentialHeaders(service));
    let trace = beginModelTraceAttempt({ protocol: service.protocol, transportProtocol: service.protocol,
      model: service.model, endpoint: target.toString(), method, timeoutMs, kind: "api", gateway: true },
      [service.apiKey ?? "", presentedToken(req.headers) ?? ""].filter(Boolean));
    if (trace && body.length) trace.request(body.toString("utf8"), new Headers(headers));
    this.progress?.({ ...attempt, phase: "running", at: started, ...(trace ? { traceRecordId: trace.id } : {}) });
    const record = (status: string, httpStatus: number | null, usage: MeteredUsage | null, firstByte?: number,
      firstText?: number) => this.emit({ ...attempt, at: started, status, httpStatus,
      durationMs: Date.now() - started, firstByteMs: firstByte ? firstByte - started : null,
      firstTextMs: firstText ? firstText - started : null, inputTokens: usage?.inputTokens ?? null,
      outputTokens: usage?.outputTokens ?? null, reasoningTokens: usage?.reasoningTokens ?? null,
      cacheReadTokens: usage?.cacheReadTokens ?? null, cacheWriteTokens: usage?.cacheWriteTokens ?? null,
      accountingVersion: 1, totalInputTokens: usage?.totalInputTokens ?? null,
      totalOutputTokens: usage?.totalOutputTokens ?? null, totalTokens: usage?.totalTokens ?? null,
      totalTokensSource: usage?.totalTokensSource ?? null, toolUsePromptTokens: usage?.toolUsePromptTokens ?? null,
      ...(trace ? { traceRecordId: trace.id } : {}) });
    // Do not add shorter first-byte/idle deadlines: local prefill and reasoning
    // gaps are legitimate parts of the same configured inference budget.
    const total = setTimeout(() => controller.abort(new Error("total-timeout")), timeoutMs);
    let upstream: Response;
    try {
      const send = () => this.fetcher(target, { method, headers,
        body: method === "GET" || method === "HEAD" ? undefined : body,
        signal: controller.signal, redirect: "error" });
      try { upstream = await send(); }
      catch (error) {
        if (!service.local || controller.signal.aborted || this.localJudge.alive()) throw error;
        // A dead local process is restarted once before any response bytes have
        // reached the client. HTTP errors and invalid answers are never retried.
        const retryOfRecordId = trace?.id;
        trace?.finish("failed", { error: { code: "judge-process-exited", message: "本地判断模型进程已退出" } });
        const endpoint = await this.localJudge.resolve(service.local, controller.signal);
        target = new URL(`${endpoint.baseUrl}/${rest}`);
        for (const [key, value] of url.searchParams) if (key !== "key") target.searchParams.append(key, value);
        headers.authorization = `Bearer ${endpoint.apiKey}`;
        trace = beginModelTraceAttempt({ protocol: service.protocol, transportProtocol: service.protocol,
          model: service.model, endpoint: target.toString(), method, timeoutMs, kind: "api", gateway: true,
          retryOfRecordId, retryReason: "judge-process-exited" },
          [endpoint.apiKey, presentedToken(req.headers) ?? ""].filter(Boolean));
        if (trace && body.length) trace.request(body.toString("utf8"), new Headers(headers));
        this.progress?.({ ...attempt, phase: "running", at: started, ...(trace ? { traceRecordId: trace.id } : {}) });
        upstream = await send();
      }
    } catch (error) {
      clearTimeout(total);
      const reason = String((controller.signal.reason as Error | undefined)?.message ?? "");
      const status = reason === "execution-revoked" ? "cancelled" : reason.endsWith("timeout") ? "timeout" : "network-error";
      trace?.finish("failed", { error: { code: status, message: "模型传输未完成" } });
      record(status, null, null);
      if (status === "cancelled") return reply(res, 409, "execution-revoked", "任务已停止");
      return reply(res, status === "timeout" ? 504 : 502, status, "模型服务连接失败");
    }
    const firstByte = Date.now();
    const responseHeaders: Record<string, string> = { "x-kyf-attempt-id": attemptId };
    upstream.headers.forEach((value, key) => { if (!DROP_RESPONSE.has(key.toLowerCase())) responseHeaders[key] = value; });
    res.writeHead(upstream.status, responseHeaders);
    const meter = new UsageMeter(upstream.headers.get("content-type") ?? "", service.protocol);
    const capture = createModelTraceResponseCapture(trace, upstream.status, upstream.headers);
    let outcome = upstream.status >= 400 ? "http-error" : "ok";
    try {
      if (upstream.body) {
        const reader = upstream.body.getReader();
        for (;;) {
          const next = await reader.read();
          if (next.done) break;
          meter.push(next.value);
          capture?.push(next.value);
          // A closed consumer never emits drain; cancellation must release the service slot too.
          if (!res.write(next.value)) await waitForDrain(res, controller.signal);
        }
      }
      // A framed stream that ends without its terminal frame is a mid-stream cut,
      // not a success: surface it in telemetry while still delivering what arrived.
      if (outcome === "ok" && meter.streaming && !meter.terminal) outcome = "stream-error";
      capture?.finish();
      res.end();
    } catch (error) {
      const reason = String((controller.signal.reason as Error | undefined)?.message ?? "");
      outcome = reason === "execution-revoked" || reason === "client-closed" ? "cancelled" :
        reason.endsWith("timeout") ? "timeout" : "stream-error";
      capture?.finish(error);
      res.destroy();
    } finally {
      clearTimeout(total);
    }
    record(outcome, upstream.status, meter.finish(), firstByte, meter.firstTextAt);
  }
}
