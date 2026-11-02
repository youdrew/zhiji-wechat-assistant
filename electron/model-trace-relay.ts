// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// A single configured provider, not an open proxy. Lifetime follows the engine,
// not the diagnostic setting. Stream forwarding never waits for trace storage.
import { createServer, request as httpRequest, type IncomingHttpHeaders, type Server } from "node:http";
import { request as httpsRequest } from "node:https";
import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { createBrotliDecompress, createGunzip, createInflate } from "node:zlib";
import { validateModelConfig, type ModelConfig } from "./model-connectors";
import { beginModelTraceAttempt, createModelTraceRequestCapture, createModelTraceResponseCapture,
  type ModelTraceContext } from "./model-traces";

function headersFrom(raw: IncomingHttpHeaders): Headers {
  const result = new Headers();
  for (const [key, value] of Object.entries(raw)) {
    if (value !== undefined) result.set(key, Array.isArray(value) ? value.join(", ") : value);
  }
  return result;
}
function authenticated(value: string | string[] | undefined, token: string): boolean {
  if (typeof value !== "string") return false;
  const candidate = Buffer.from(value), expected = Buffer.from(token);
  return candidate.length === expected.length && timingSafeEqual(candidate, expected);
}
interface Binding { context?: ModelTraceContext; messageId?: string; stepId: string; attempt: number;
  previous?: { stepId: string; attempt: number }; retryReason?: string }
export class ModelTraceRelay {
  readonly auth = randomBytes(32).toString("hex");
  private server?: Server;
  private base?: URL;
  private sessions = new Map<string, Binding>();
  private messages = new Map<string, Binding>();
  private attempts = new Map<string, number>();
  private requests = new Set<import("node:http").ClientRequest>();
  baseUrl = "";
  constructor(private config: ModelConfig, private engineVersion: string) {}
  bind(sessionId: string, context?: ModelTraceContext, messageId?: string): () => void {
    // Preserve the explicit scope identity so later business validation can
    // attach its result to the same attempt without a global "latest" lookup.
    const binding: Binding = { context, messageId, stepId: randomUUID(), attempt: 0 };
    this.sessions.set(sessionId, binding);
    if (messageId) this.messages.set(`${sessionId}:${messageId}`, binding);
    while (this.messages.size > 4096) this.messages.delete(this.messages.keys().next().value!);
    return () => {
      if (this.sessions.get(sessionId) === binding) this.sessions.delete(sessionId);
      if (messageId && this.messages.get(`${sessionId}:${messageId}`) === binding) this.messages.delete(`${sessionId}:${messageId}`);
    };
  }
  retry(sessionId: string): void {
    const binding = this.sessions.get(sessionId);
    if (binding?.previous) Object.assign(binding, binding.previous, { retryReason: "OpenCode provider 重试" });
  }
  async start(): Promise<this> {
    const safe = validateModelConfig(this.config);
    this.base = new URL(safe.protocol === "ollama" ? safe.baseUrl.replace(/\/v1$/u, "") + "/v1" : safe.baseUrl);
    this.server = createServer((req, res) => {
      if (!authenticated(req.headers["x-kyf-relay-auth"], this.auth) || req.method !== "POST" ||
          req.headers.origin || req.headers["sec-fetch-site"] || !/^127\.0\.0\.1:\d+$/u.test(req.headers.host ?? "")) {
        res.writeHead(403); res.end(); return;
      }
      let url: URL;
      try {
        url = new URL(req.url!, this.base);
        const prefix = this.base!.pathname.replace(/\/+$/u, "");
        const suffix = url.pathname.slice(prefix.length);
        const allowed = safe.protocol === "responses" ? suffix === "/responses" :
          safe.protocol === "anthropic" ? suffix === "/messages" :
          safe.protocol === "gemini" ? /^\/models\/[^/]+:(?:streamGenerateContent|generateContent)$/u.test(suffix) : suffix === "/chat/completions";
        if (url.origin !== this.base!.origin || !url.pathname.startsWith(prefix + "/") || !allowed) throw new Error();
      } catch { res.writeHead(403); res.end(); return; }
      const sessionId = String(req.headers["x-kyf-session-id"] ?? req.headers["x-opencode-session-id"] ?? "");
      const messageId = String(req.headers["x-kyf-message-id"] ?? "");
      // An explicit message identity must never fall back to a newer execution
      // of the same native session after the original binding was retired.
      const binding = messageId ? this.messages.get(`${sessionId}:${messageId}`) : this.sessions.get(sessionId);
      const stepId = String(req.headers["x-kyf-step-id"] ?? binding?.stepId ?? randomUUID());
      const headers: IncomingHttpHeaders = { ...req.headers, host: url.host };
      // The provider receives its original SDK headers, except identifiers that
      // only exist to associate local execution. Never forward the process token.
      for (const key of Object.keys(headers)) if (key.startsWith("x-kyf-") || key.startsWith("x-opencode-") ||
          ["x-session-id", "x-parent-session-id", "x-session-affinity"].includes(key)) delete headers[key];
      const count = binding ? ++binding.attempt : (this.attempts.get(stepId) ?? 0) + 1;
      if (binding) binding.previous = { stepId, attempt: count };
      this.attempts.set(stepId, count);
      while (this.attempts.size > 4096) this.attempts.delete(this.attempts.keys().next().value!);
      const attempt = binding?.context ? beginModelTraceAttempt({ protocol: safe.protocol,
        transportProtocol: safe.protocol === "ollama" ? "chat_completions" : safe.protocol,
        model: safe.model, endpoint: url.toString(), method: "POST", engineVersion: this.engineVersion,
        stepId: stepId || undefined, sessionId, nativeMessageId: binding.messageId,
        attempt: count, timeoutMs: 1_800_000,
        ...(count > 1 ? { retryReason: binding?.retryReason ?? "OpenCode provider 重试" } : {}),
      }, [safe.apiKey, this.auth], binding.context) : undefined;
      const capture = createModelTraceRequestCapture(attempt, headersFrom(headers));
      const upstream = (url.protocol === "https:" ? httpsRequest : httpRequest)(url, { method: "POST", headers });
      this.requests.add(upstream);
      upstream.once("close", () => this.requests.delete(upstream));
      req.on("data", (bytes: Buffer) => capture?.push(bytes));
      req.once("end", () => capture?.finish());
      req.once("error", error => { capture?.finish(error); upstream.destroy(error); });
      req.once("aborted", () => { capture?.finish(new Error("aborted")); upstream.destroy(); });
      res.once("close", () => { if (!res.writableFinished) upstream.destroy(); });
      upstream.once("response", incoming => {
        res.writeHead(incoming.statusCode ?? 502, incoming.headers);
        const responseCapture = createModelTraceResponseCapture(attempt, incoming.statusCode ?? 502, headersFrom(incoming.headers));
        const encoding = incoming.headers["content-encoding"];
        let decoder: ReturnType<typeof createGunzip> | undefined;
        if (responseCapture && encoding && encoding !== "identity") {
          decoder = encoding === "gzip" ? createGunzip() : encoding === "deflate" ? createInflate() :
            encoding === "br" ? createBrotliDecompress() : undefined;
          if (!decoder) attempt?.incomplete("未知压缩格式，响应正文未采集");
          decoder?.on("data", (bytes: Buffer) => responseCapture.push(bytes));
          decoder?.once("end", () => responseCapture.finish());
          decoder?.once("error", error => responseCapture.finish(error));
        }
        incoming.on("data", (bytes: Buffer) => {
          if (!responseCapture) return;
          if (decoder) {
            if (!decoder.write(bytes)) { attempt?.incomplete("压缩响应脱敏缓冲拥塞"); decoder.destroy(); decoder = undefined; }
          } else if (!encoding || encoding === "identity") responseCapture.push(bytes);
        });
        incoming.once("end", () => {
          decoder ? decoder.end() : responseCapture?.finish();
          if (binding && (incoming.statusCode ?? 500) < 400) { binding.stepId = randomUUID(); binding.attempt = 0; }
        });
        incoming.once("error", error => { decoder?.destroy(); responseCapture?.finish(error); res.destroy(error); });
        incoming.pipe(res);
      });
      upstream.once("error", error => {
        attempt?.finish("failed", { error: { code: (error as NodeJS.ErrnoException).code ?? "network",
          message: "引擎 provider 传输未完成；是否收到请求未知" } });
        if (!res.headersSent) { res.writeHead(502); res.end('{"error":{"message":"Local provider transport unavailable"}}'); }
        else res.destroy();
      });
      req.pipe(upstream);
    });
    this.server.requestTimeout = 0;
    await new Promise<void>((resolve, reject) => { this.server!.once("error", reject); this.server!.listen(0, "127.0.0.1", resolve); });
    const address = this.server.address();
    if (!address || typeof address === "string") throw new Error("Invalid local provider listener");
    this.baseUrl = `http://127.0.0.1:${address.port}${this.base.pathname.replace(/\/+$/u, "")}`;
    return this;
  }
  async close(): Promise<void> {
    for (const request of this.requests) request.destroy();
    this.requests.clear(); this.sessions.clear(); this.messages.clear(); this.attempts.clear();
    if (this.server) await new Promise<void>(resolve => { this.server!.closeAllConnections(); this.server!.close(() => resolve()); });
    this.server = undefined;
  }
}
