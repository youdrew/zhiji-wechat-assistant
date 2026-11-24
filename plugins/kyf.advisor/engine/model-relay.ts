// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The advisor's loopback model relay for one DeepSeek Harness process.
//
// DSH is configured with this relay's address and random secret only; it never sees a credential or an
// execution token. One DSH process serves exactly one advisor run, so the relay carries one binding: every
// model request of that process is forwarded to the Core model gateway as /s/<service>~<step>/<rest> with the
// run's execution token in place of the secret. Unbound, it answers 401 (fail closed).
// Derived from plugin-sdk/node/execution-relay.ts (2026-11-14), which keys bindings by an engine session header
// that DSH does not send. Added here: SDK path normalization per protocol, an optional request-body switch that
// asks chat-template servers not to think, and the upstream status of each attempt for error reporting.
import { createServer, request as httpRequest, type ClientRequest, type IncomingHttpHeaders, type Server } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";

export type RelayProtocol = "anthropic" | "responses" | "chat_completions" | "gemini" | "ollama";
export interface RelayBinding { token: string; step: string; noThinking?: boolean }
export interface RelayAttempt { status: number; attempt: number }

const SECRET_HEADERS = ["authorization", "x-api-key", "x-goog-api-key"] as const;
const DROPPED = new Set(["api-key", "connection", "keep-alive", "proxy-authorization", "proxy-connection", "upgrade",
  "content-length", "transfer-encoding"]);
const MAX_REWRITE_BYTES = 64 * 1024 * 1024;

function same(candidate: string, expected: string): boolean {
  const a = Buffer.from(candidate), b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

/**
 * The gateway service url is a complete API prefix (…/v1, …/v1beta, or the Ollama root). The Anthropic and Gemini
 * SDKs inside DSH append their own version segment, and DSH speaks Ollama through its OpenAI-compatible /v1.
 */
export function relayBase(protocol: RelayProtocol, base: string): string {
  return protocol === "ollama" ? base + "/v1" : base;
}

function normalizedRest(protocol: RelayProtocol, rest: string): string {
  if (protocol === "anthropic") return rest.replace(/^\/v1(?=\/)/u, "");
  if (protocol === "gemini") return rest.replace(/^\/v1(?:beta|alpha)?(?=\/)/u, "");
  return rest;
}

export class ModelRelay {
  /** Random per relay; DSH's provider key. Never reaches the gateway. */
  readonly secret = randomBytes(32).toString("hex");
  private readonly target: URL;
  private readonly prefix: string;
  private binding?: RelayBinding & { requests: Set<ClientRequest> };
  private readonly requests = new Set<ClientRequest>();
  private server?: Server;
  private attempts = 0;
  baseUrl = "";
  /** Called with the gateway's status of every forwarded request (0 when the gateway was unreachable). */
  onAttempt?: (attempt: RelayAttempt) => void;

  /** `serviceUrl` is the gateway service url, e.g. http://127.0.0.1:<gw>/s/<service>. */
  constructor(serviceUrl: string, private readonly protocol: RelayProtocol) {
    const url = new URL(serviceUrl);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.search || url.hash ||
        url.username || url.password || !/^\/s\/[A-Za-z0-9_-]{1,64}$/u.test(url.pathname)) throw new Error("invalid-service-url");
    this.target = url;
    this.prefix = url.pathname;
  }

  /** Routes every request under this execution until the returned function is called. */
  bind(binding: RelayBinding): () => void {
    if (typeof binding?.token !== "string" || !/^[\x21-\x7e]{1,4096}$/u.test(binding.token) ||
        typeof binding.step !== "string" || !/^[A-Za-z0-9._-]{1,40}$/u.test(binding.step)) throw new Error("invalid-binding");
    const entry = { ...binding, requests: new Set<ClientRequest>() };
    this.binding = entry;
    return () => {
      if (this.binding === entry) this.binding = undefined;
      for (const request of entry.requests) request.destroy();
      entry.requests.clear();
    };
  }

  async start(): Promise<string> {
    if (this.server) return this.baseUrl;
    this.server = createServer((req, res) => {
      const deny = (status: number, code: string) => {
        res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
        res.end(JSON.stringify({ error: { code, type: code, message: code } }));
      };
      const presented = SECRET_HEADERS.find(name => {
        const raw = req.headers[name];
        return typeof raw === "string" && same(name === "authorization" ? raw.replace(/^Bearer\s+/iu, "") : raw, this.secret);
      });
      if (!presented || req.method !== "POST" || req.headers.origin !== undefined || req.headers["sec-fetch-site"] !== undefined ||
          !/^127\.0\.0\.1:\d+$/u.test(req.headers.host ?? "")) { deny(403, "forbidden"); return; }
      let url: URL;
      try { url = new URL(req.url ?? "/", "http://127.0.0.1"); } catch { deny(403, "forbidden"); return; }
      if (!url.pathname.startsWith(this.prefix + "/") || url.pathname.length === this.prefix.length + 1) { deny(403, "forbidden"); return; }
      const binding = this.binding;
      if (!binding) { deny(401, "execution-required"); return; }
      url.searchParams.delete("key");
      const rest = normalizedRest(this.protocol, url.pathname.slice(this.prefix.length));
      const target = new URL(`${this.prefix}~${binding.step}${rest}${url.search}`, this.target.origin);
      // The gateway receives DSH's own SDK headers, minus hop headers and the relay secret; the execution
      // token goes in the header the secret came in.
      const headers: IncomingHttpHeaders = {};
      for (const [key, value] of Object.entries(req.headers)) {
        if (key.startsWith("x-kyf-") || DROPPED.has(key) || (SECRET_HEADERS as readonly string[]).includes(key)) continue;
        headers[key] = value;
      }
      headers.host = this.target.host;
      headers[presented] = presented === "authorization" ? `Bearer ${binding.token}` : binding.token;
      const attempt = ++this.attempts;
      const forward = (body?: Buffer) => {
        if (body) headers["content-length"] = String(body.length);
        else if (req.headers["content-length"] !== undefined) headers["content-length"] = req.headers["content-length"];
        const upstream = httpRequest(target, { method: "POST", headers });
        this.requests.add(upstream);
        binding.requests.add(upstream);
        upstream.once("close", () => { this.requests.delete(upstream); binding.requests.delete(upstream); });
        req.once("error", error => upstream.destroy(error));
        req.once("aborted", () => upstream.destroy());
        res.once("close", () => { if (!res.writableFinished) upstream.destroy(); });
        upstream.once("response", incoming => {
          try { this.onAttempt?.({ status: incoming.statusCode ?? 502, attempt }); } catch { /* observers never break the relay */ }
          res.writeHead(incoming.statusCode ?? 502, incoming.headers);
          incoming.once("error", error => res.destroy(error));
          incoming.pipe(res);
        });
        upstream.once("error", () => {
          try { this.onAttempt?.({ status: 0, attempt }); } catch { /* as above */ }
          if (!res.headersSent) deny(502, "gateway-unavailable");
          else res.destroy();
        });
        if (body) upstream.end(body); else req.pipe(upstream);
      };
      // Chat-template servers (DeepSeek, Qwen, llama.cpp) read these switches; hosted APIs reject them, which
      // the runtime reports as "thinking-unsupported".
      const rewrite = binding.noThinking && (this.protocol === "chat_completions" || this.protocol === "ollama") &&
        /\/chat\/completions$/u.test(rest);
      if (!rewrite) { forward(); return; }
      const chunks: Buffer[] = [];
      let size = 0;
      req.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > MAX_REWRITE_BYTES) { req.destroy(); deny(413, "request-too-large"); return; }
        chunks.push(chunk);
      });
      req.once("end", () => {
        if (res.writableEnded) return;
        let body = Buffer.concat(chunks);
        try {
          const parsed = JSON.parse(body.toString("utf8"));
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            parsed.chat_template_kwargs = { ...(parsed.chat_template_kwargs ?? {}), thinking: false, enable_thinking: false };
            body = Buffer.from(JSON.stringify(parsed), "utf8");
          }
        } catch { /* forward what DSH sent */ }
        forward(body);
      });
    });
    this.server.requestTimeout = 0;
    await new Promise<void>((resolve, reject) => { this.server!.once("error", reject); this.server!.listen(0, "127.0.0.1", resolve); });
    const address = this.server.address();
    if (!address || typeof address === "string") throw new Error("invalid-listener");
    this.baseUrl = `http://127.0.0.1:${address.port}${this.prefix}`;
    return this.baseUrl;
  }

  async close(): Promise<void> {
    for (const request of this.requests) request.destroy();
    this.requests.clear();
    this.binding = undefined;
    const server = this.server;
    this.server = undefined;
    if (server) await new Promise<void>(resolve => { server.closeAllConnections(); server.close(() => resolve()); });
  }
}
