// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// A minimal Agent Client Protocol (ACP v1) client over a child process' stdio: newline-delimited JSON-RPC 2.0.
// Only what the advisor uses: initialize, session/new, session/resume, session/prompt, session/cancel,
// session/close, `session/update` notifications, `session/request_permission` requests, and the advisor's own
// `_zhiji/stream` notifications (engine/dsh-stream). Anything else the agent asks for is refused.
import type { ChildProcessWithoutNullStreams } from "node:child_process";

const MAX_LINE_CHARS = 64 * 1024 * 1024;
const LS = String.fromCharCode(0x2028), PS = String.fromCharCode(0x2029);

export class AcpError extends Error {
  constructor(message: string, readonly code?: number, readonly data?: unknown) { super(message); this.name = "AcpError"; }
}

/** The transport ended before a reply: the process exited or its pipe closed. */
export class AcpClosedError extends AcpError {
  constructor() { super("acp-closed"); this.name = "AcpClosedError"; }
}

export interface AcpPermissionOption { optionId: string; kind?: string; name?: string }
export interface AcpPermissionRequest {
  sessionId?: string;
  toolCall?: { toolCallId?: string; title?: string; kind?: string; rawInput?: unknown };
  options: AcpPermissionOption[];
}

export interface AcpHandlers {
  /** `session/update` payload: `{ sessionId, update }`. */
  onUpdate?(sessionId: string, update: Record<string, unknown>): void;
  /** One frame of engine/dsh-stream. */
  onStream?(params: Record<string, unknown>): void;
  /** Return the optionId to select, or undefined to cancel the request. */
  onPermission?(request: AcpPermissionRequest): string | undefined;
  /** The process exited or its stdout closed. */
  onClose?(): void;
}

export interface AcpTransport {
  request(method: string, params?: unknown, signal?: AbortSignal): Promise<any>;
  notify(method: string, params?: unknown): void;
  /** Ends stdin, waits for exit, then kills. */
  close(graceMs?: number): Promise<void>;
  readonly closed: boolean;
  /** The last diagnostics the process wrote to stderr (bounded), for error classification only. */
  diagnostics(): string;
}

export function acpClient(child: ChildProcessWithoutNullStreams, handlers: AcpHandlers): AcpTransport {
  let buffer = "", next = 1, closed = false, stderr = "";
  const pending = new Map<number, { resolve(value: unknown): void; reject(error: Error): void }>();
  const send = (frame: unknown) => {
    if (closed || !child.stdin.writable) throw new AcpClosedError();
    // Node's readline on the other side splits on U+2028/U+2029 too; keep one frame on one line.
    child.stdin.write(JSON.stringify(frame).split(LS).join("\\u2028").split(PS).join("\\u2029") + "\n");
  };
  const finish = () => {
    if (closed) return;
    closed = true;
    for (const waiter of pending.values()) waiter.reject(new AcpClosedError());
    pending.clear();
    try { handlers.onClose?.(); } catch { /* listener errors never reach the transport */ }
  };
  const handle = (line: string) => {
    let frame: any;
    try { frame = JSON.parse(line); } catch { return; }
    if (!frame || typeof frame !== "object") return;
    if (typeof frame.method === "string" && frame.id !== undefined && frame.id !== null) {
      let result: unknown, error: { code: number; message: string } | undefined;
      if (frame.method === "session/request_permission") {
        const params = frame.params && typeof frame.params === "object" ? frame.params : {};
        const options = Array.isArray(params.options) ? params.options.filter((item: any) => item && typeof item.optionId === "string") : [];
        let selected: string | undefined;
        try { selected = handlers.onPermission?.({ sessionId: params.sessionId, toolCall: params.toolCall, options }); } catch { selected = undefined; }
        result = selected !== undefined && options.some((item: any) => item.optionId === selected) ?
          { outcome: { outcome: "selected", optionId: selected } } : { outcome: { outcome: "cancelled" } };
      } else error = { code: -32601, message: "Method not found" };
      try { send({ jsonrpc: "2.0", id: frame.id, ...(error ? { error } : { result }) }); } catch { /* closing */ }
      return;
    }
    if (typeof frame.method === "string") {
      const params = frame.params && typeof frame.params === "object" ? frame.params : {};
      try {
        if (frame.method === "session/update" && typeof params.sessionId === "string" && params.update && typeof params.update === "object") {
          handlers.onUpdate?.(params.sessionId, params.update);
        } else if (frame.method === "_zhiji/stream") handlers.onStream?.(params);
      } catch { /* a failing listener must not stop the reader */ }
      return;
    }
    const waiter = typeof frame.id === "number" ? pending.get(frame.id) : undefined;
    if (!waiter) return;
    pending.delete(frame.id);
    if (frame.error) waiter.reject(new AcpError(String(frame.error.message ?? "acp-error"), Number(frame.error.code), frame.error.data));
    else waiter.resolve(frame.result);
  };
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    if (buffer.length > MAX_LINE_CHARS && !buffer.includes("\n")) { buffer = ""; child.kill(); return; }
    let index: number;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      if (line.trim()) handle(line);
    }
  });
  child.stderr.on("data", (chunk: Buffer) => { stderr = (stderr + chunk.toString("utf8")).slice(-8192); });
  child.stdout.once("close", finish);
  child.once("exit", finish);
  child.once("error", finish);
  child.stdin.on("error", () => {});
  return {
    get closed() { return closed; },
    diagnostics: () => stderr,
    request(method, params, signal) {
      return new Promise((resolve, reject) => {
        if (signal?.aborted) { reject(new AcpError("aborted")); return; }
        const id = next++;
        const abort = () => { if (pending.delete(id)) reject(new AcpError("aborted")); };
        pending.set(id, {
          resolve: value => { signal?.removeEventListener("abort", abort); resolve(value); },
          reject: error => { signal?.removeEventListener("abort", abort); reject(error); },
        });
        signal?.addEventListener("abort", abort, { once: true });
        try { send({ jsonrpc: "2.0", id, method, ...(params !== undefined ? { params } : {}) }); }
        catch (error) { pending.delete(id); signal?.removeEventListener("abort", abort); reject(error as Error); }
      });
    },
    notify(method, params) { try { send({ jsonrpc: "2.0", method, ...(params !== undefined ? { params } : {}) }); } catch { /* closing */ } },
    async close(graceMs = 4000) {
      if (child.exitCode !== null || child.signalCode !== null) { finish(); return; }
      await new Promise<void>(resolve => {
        const timer = setTimeout(() => { child.kill(); }, graceMs);
        const hard = setTimeout(() => { child.kill("SIGKILL"); resolve(); }, graceMs + 3000);
        child.once("exit", () => { clearTimeout(timer); clearTimeout(hard); resolve(); });
        try { child.stdin.end(); } catch { child.kill(); }
      });
      finish();
    },
  };
}
