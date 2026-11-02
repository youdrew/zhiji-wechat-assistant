// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Always-on usage metering for the model gateway: reads provider usage counters
// from JSON, SSE and NDJSON responses without keeping the body.
import { mergeUsageCounters, normalizedUsage, visibleText, type NormalizedUsage } from "./model-traces";

const JSON_LIMIT = 8 * 1024 * 1024;
const FRAME_LIMIT = 4 * 1024 * 1024;

export type MeteredUsage = Omit<NormalizedUsage, "inputAccounting">;

export class UsageMeter {
  private pending = "";
  private readonly decoder = new TextDecoder();
  private readonly mode: "sse" | "ndjson" | "json";
  private usage: Record<string, unknown> | undefined;
  private overflow = false;
  firstTextAt?: number;
  terminal = false;

  constructor(contentType: string, private readonly protocol: string) {
    this.mode = /event-stream/iu.test(contentType) ? "sse" : /ndjson/iu.test(contentType) ? "ndjson" : "json";
  }

  /** Only framed streams carry a terminal frame; a complete JSON body is its own end. */
  get streaming(): boolean { return this.mode !== "json"; }

  push(bytes: Uint8Array): void {
    if (this.overflow) return;
    this.pending += this.decoder.decode(bytes, { stream: true });
    if (this.mode === "json") {
      if (this.pending.length > JSON_LIMIT) { this.overflow = true; this.pending = ""; }
      return;
    }
    const separator = this.mode === "sse" ? /\r?\n\r?\n/u : /\r?\n/u;
    for (;;) {
      const match = separator.exec(this.pending);
      if (!match) break;
      const raw = this.pending.slice(0, match.index);
      this.pending = this.pending.slice(match.index + match[0].length);
      this.frame(raw);
    }
    if (this.pending.length > FRAME_LIMIT) this.pending = "";
  }

  finish(): MeteredUsage | null {
    if (!this.overflow) {
      this.pending += this.decoder.decode();
      if (this.pending.trim()) this.frame(this.pending);
    }
    this.pending = "";
    const normalized = normalizedUsage(this.usage, this.protocol);
    if (!normalized) return null;
    const { inputAccounting: _accounting, ...usage } = normalized;
    return usage;
  }

  private frame(raw: string): void {
    if (!raw.trim()) return;
    let payload = raw;
    if (this.mode === "sse") {
      payload = raw.split(/\r?\n/u).filter(line => line.startsWith("data:"))
        .map(line => line.slice(5).trimStart()).join("\n");
      if (payload === "[DONE]") { this.terminal = true; return; }
      if (!payload) return;
    }
    let parsed: any;
    try { parsed = JSON.parse(payload); } catch { return; }
    if (!parsed || typeof parsed !== "object") return;
    const usage = parsed.usage ?? parsed.response?.usage ?? parsed.message?.usage ?? parsed.usageMetadata;
    if (usage) this.usage = mergeUsageCounters(this.usage, usage);
    if (parsed.eval_count !== undefined || parsed.prompt_eval_count !== undefined)
      this.usage = mergeUsageCounters(this.usage,
        { prompt_eval_count: parsed.prompt_eval_count, eval_count: parsed.eval_count });
    if (this.firstTextAt === undefined) {
      const text = visibleText(parsed);
      if (typeof text === "string" && text.length) this.firstTextAt = Date.now();
    }
    if (parsed.done === true || ["response.completed", "message_stop"].includes(parsed.type) ||
        parsed.choices?.[0]?.finish_reason || parsed.candidates?.[0]?.finishReason) this.terminal = true;
  }
}
