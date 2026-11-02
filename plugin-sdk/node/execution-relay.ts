// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Plugin SDK execution relay: a loopback endpoint for an engine that must hold neither a
// credential nor an execution token (e.g. opencode). The engine is configured with
// `baseUrl` and `secret` only. While a plugin call runs it binds the engine's session id
// to {token, step}; each request of that session is forwarded to the Core model gateway as
// /s/<service>~<step>/<rest> with the execution token in place of the secret. Requests of
// unbound sessions get 401 (fail closed). Derived from electron/model-trace-relay.ts
// (2026-10-13) without trace capture: the gateway meters and traces every attempt.
import { createServer, request as httpRequest, type ClientRequest, type IncomingHttpHeaders, type Server } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";

export interface ExecutionBinding { token: string; step: string }

const SECRET_HEADERS = ["authorization", "x-api-key", "x-goog-api-key"] as const;
const DROPPED = new Set(["api-key", "x-parent-session-id", "connection", "keep-alive", "proxy-authorization",
  "proxy-connection", "upgrade"]);
const SESSION_HEADER = "x-opencode-session-id";

function same(candidate: string, expected: string): boolean {
  const a = Buffer.from(candidate), b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export class ExecutionRelay {
  /** Random per relay; the engine's provider apiKey. Never reaches the gateway. */
  readonly secret = randomBytes(32).toString("hex");
  private readonly target: URL;
  private readonly prefix: string;
  private readonly sessions = new Map<string, ExecutionBinding & { requests: Set<ClientRequest> }>();
  private readonly requests = new Set<ClientRequest>();
  private server?: Server;
  baseUrl = "";

  /** `serviceUrl` is the gateway service url, e.g. http://127.0.0.1:<gw>/s/<service>. */
  constructor(serviceUrl: string) {
    const url = new URL(serviceUrl);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.search || url.hash ||
        url.username || url.password || !/^\/s\/[A-Za-z0-9_-]{1,64}$/u.test(url.pathname)) throw new Error("invalid-service-url");
    this.target = url;
    this.prefix = url.pathname;
  }

  /** Routes the session's requests under this execution until the returned function is called. */
  bind(sessionId: string, binding: ExecutionBinding): () => void {
    if (typeof sessionId !== "string" || !sessionId || sessionId.length > 256 ||
        typeof binding?.token !== "string" || !/^[\x21-\x7e]{1,4096}$/u.test(binding.token) ||
        typeof binding.step !== "string" || !/^[A-Za-z0-9._-]{1,40}$/u.test(binding.step)) throw new Error("invalid-binding");
    const entry = { token: binding.token, step: binding.step, requests: new Set<ClientRequest>() };
    this.sessions.set(sessionId, entry);
    return () => {
      if (this.sessions.get(sessionId) === entry) this.sessions.delete(sessionId);
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
      if (!url.pathname.startsWith(this.prefix + "/") || url.pathname.length === this.prefix.length + 1) {
        deny(403, "forbidden"); return;
      }
      const sessionId = req.headers[SESSION_HEADER];
      const binding = typeof sessionId === "string" ? this.sessions.get(sessionId) : undefined;
      if (!binding) { deny(401, "execution-required"); return; }
      url.searchParams.delete("key");
      const target = new URL(`${this.prefix}~${binding.step}${url.pathname.slice(this.prefix.length)}${url.search}`, this.target.origin);
      // The gateway receives the engine's own SDK headers, minus local session identifiers
      // and the relay secret; the execution token goes in the header the secret came in.
      const headers: IncomingHttpHeaders = {};
      for (const [key, value] of Object.entries(req.headers)) {
        if (key.startsWith("x-kyf-") || key.startsWith("x-opencode-") || key.startsWith("x-session-") || DROPPED.has(key) ||
            (SECRET_HEADERS as readonly string[]).includes(key)) continue;
        headers[key] = value;
      }
      headers.host = this.target.host;
      headers[presented] = presented === "authorization" ? `Bearer ${binding.token}` : binding.token;
      const upstream = httpRequest(target, { method: "POST", headers });
      this.requests.add(upstream);
      binding.requests.add(upstream);
      upstream.once("close", () => { this.requests.delete(upstream); binding.requests.delete(upstream); });
      req.once("error", error => upstream.destroy(error));
      req.once("aborted", () => upstream.destroy());
      res.once("close", () => { if (!res.writableFinished) upstream.destroy(); });
      upstream.once("response", incoming => {
        res.writeHead(incoming.statusCode ?? 502, incoming.headers);
        incoming.once("error", error => res.destroy(error));
        incoming.pipe(res);
      });
      upstream.once("error", () => {
        if (!res.headersSent) deny(502, "gateway-unavailable");
        else res.destroy();
      });
      req.pipe(upstream);
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
    this.sessions.clear();
    const server = this.server;
    this.server = undefined;
    if (server) await new Promise<void>(resolve => { server.closeAllConnections(); server.close(() => resolve()); });
  }
}
