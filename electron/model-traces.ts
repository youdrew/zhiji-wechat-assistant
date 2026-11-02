// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Capture is optional; this module never owns or retries a business request.
import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";

export interface ModelTraceContext {
  producerId: string;
  taskId: string;
  executionId: string;
  accountId?: string;
  contactId?: string;
  sessionId?: string;
  source: string;
  trigger?: string;
  businessJobId?: string;
  stepId?: string;
  parentStepId?: string;
  resumeOfTaskId?: string;
  retryOfTaskId?: string;
  [key: string]: unknown;
}
export interface ModelTraceState {
  collecting: boolean;
  collectionVersion: number;
  deleteVersion: number;
  retentionCutoffAt?: number;
}
export interface ModelTraceProducer {
  state(context?: ModelTraceContext): ModelTraceState;
  emit(context: ModelTraceContext, event: Record<string, unknown>): boolean | void;
}
const contexts = new AsyncLocalStorage<ModelTraceContext>();
let producer: ModelTraceProducer | undefined;
export function configureModelTraceProducer(value?: ModelTraceProducer): void { producer = value; }
export function currentModelTraceContext(): ModelTraceContext | undefined { return contexts.getStore(); }
export function withModelTraceContext<T>(context: ModelTraceContext | undefined, action: () => T): T {
  return context ? contexts.run(context, action) : action();
}
export function withModelTraceStep<T>(source: string, action: () => T, extra: Record<string, unknown> = {}): T {
  const parent = contexts.getStore();
  return withModelTraceContext(parent && { ...parent, parentStepId: parent.stepId,
    stepId: randomUUID(), source, ...extra }, () => {
    try {
      const result = action();
      if (result && typeof (result as any).then === "function") return (Promise.resolve(result).catch(error => {
        modelTraceOutcome("failed", { code: typeof error?.code === "string" ? error.code : "business-processing" }, "discarded");
        throw error;
      }) as T);
      return result;
    } catch (error) {
      modelTraceOutcome("failed", { code: typeof (error as any)?.code === "string" ? (error as any).code : "business-processing" }, "discarded");
      throw error;
    }
  });
}

const SIDE_LIMIT = 16 * 1024 * 1024;
// The producer client has a separate 2 MiB queue. Diagnostic parsers share this
// 4 MiB work allowance across concurrent attempts in this Node process.
const WORK_LIMIT = 4 * 1024 * 1024;
const CHUNK_BYTES = 8 * 1024;
let workBytes = 0;
let peakWorkBytes = 0;
export function modelTraceBufferStats() { return { workBytes, peakWorkBytes, limit: WORK_LIMIT }; }
function reserve(bytes: number): boolean {
  if (bytes < 0 || workBytes + bytes > WORK_LIMIT) return false;
  workBytes += bytes; peakWorkBytes = Math.max(peakWorkBytes, workBytes); return true;
}
function release(bytes: number): void { workBytes = Math.max(0, workBytes - bytes); }
const SECRET_KEY = /^(?:authorization|proxy.authorization|cookie|set.cookie|api.?key|access.?token|refresh.?token|auth.?token|password|secret|signature|sig|credential|x.goog.api.key|x.api.key)$/iu;
const ALLOWED_HEADERS = new Set(["accept", "content-type", "anthropic-version", "anthropic-beta", "openai-beta", "x-request-id", "request-id", "retry-after"]);
const REDACTED = "[已脱敏]";
function secretKey(key: string): boolean { return SECRET_KEY.test(key) || key.toLowerCase().startsWith("x-kyf-"); }

/** Each attempt owns its immutable key snapshot, including after config rotation. */
export class TraceRedactor {
  readonly variants: string[];
  private readonly encoded: Array<{ whole: RegExp; tail?: RegExp }> = [];
  constructor(secrets: string[]) {
    const values = new Set<string>();
    for (const secret of secrets.filter(Boolean)) {
      values.add(secret); values.add(encodeURIComponent(secret));
      values.add(encodeURIComponent(secret).toLowerCase());
      values.add(JSON.stringify(secret).slice(1, -1));
      values.add(secret.split("").map(char => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`).join(""));
      const hexPattern = (hex: string) => hex.replace(/[a-f]/giu, letter => `[${letter.toLowerCase()}${letter.toUpperCase()}]`);
      const literal = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
      const tokens = [...secret].map(char => {
        const uri = [...Buffer.from(char)].map(byte => "%" + hexPattern(byte.toString(16).padStart(2, "0"))).join("");
        const unicode = char.split("").map(unit => "\\\\u" + hexPattern(unit.charCodeAt(0).toString(16).padStart(4, "0"))).join("");
        return `(?:${[...new Set([literal(char), literal(JSON.stringify(char).slice(1, -1)), uri, unicode])].join("|")})`;
      });
      let prefix = "";
      for (let index = tokens.length - 2; index >= 0; index--) prefix = tokens[index] + (prefix ? `(?:${prefix})?` : "");
      this.encoded.push({ whole: new RegExp(tokens.join(""), "gu"),
        ...(prefix ? { tail: new RegExp(prefix + "(?:%[\\da-fA-F]?|\\\\(?:u[\\da-fA-F]{0,3})?)?$", "u") } : {}) });
    }
    this.variants = [...values].sort((a, b) => b.length - a.length);
  }
  encodedBoundaries(value: string): { spans: Array<[number, number]>; carry: number } {
    const spans: Array<[number, number]> = [];
    let carry = 0;
    if (!value.includes("%") && !value.includes("\\")) return { spans, carry };
    for (const pattern of this.encoded) {
      pattern.whole.lastIndex = 0;
      for (let match = pattern.whole.exec(value); match; match = pattern.whole.exec(value))
        spans.push([match.index, match.index + match[0].length]);
      const tail = pattern.tail?.exec(value);
      if (tail) carry = Math.max(carry, tail[0].length);
    }
    // A percent/unicode escape can itself span separate model deltas. Retain
    // that suffix until the next event makes its credential meaning knowable.
    if (this.encoded.length) carry = Math.max(carry, /(?:%[\da-fA-F]?|\\(?:u[\da-fA-F]{0,3})?)$/u.exec(value)?.[0].length ?? 0);
    return { spans, carry };
  }
  text(value: string): string {
    let result = value;
    // Credential-equivalent encodings in nested error strings are not safe just
    // because the outer protocol JSON has already been parsed.
    const unescaped = result.replace(/\\u([\da-f]{4})/giu, (_all, hex) => String.fromCharCode(parseInt(hex, 16)));
    let decoded = unescaped;
    try { decoded = decodeURIComponent(unescaped); } catch { /* Preserve malformed text. */ }
    if (this.variants.some(secret => decoded.includes(secret))) result = decoded;
    for (const secret of this.variants) result = result.split(secret).join(REDACTED);
    result = result.replace(/data:([\w/+.-]+);base64,[a-z\d+/=\s]+/giu,
      (_all, mime) => `[媒体已省略: ${mime}]`);
    // URLs in text and errors are data too. Remove credentials and signed query values.
    return result.replace(/https?:\/\/[^\s"<>]+/giu, raw => {
      try {
        const url = new URL(raw); url.username = ""; url.password = "";
        for (const key of [...url.searchParams.keys()]) {
          if (SECRET_KEY.test(key) || /token|signature|credential|(?:^|[-_])key(?:$|[-_])/iu.test(key))
            url.searchParams.set(key, REDACTED);
        }
        return url.toString();
      } catch { return "[无法安全显示的地址]"; }
    });
  }
  value(value: unknown, key = "", depth = 0): unknown {
    if (depth > 80) return "[嵌套内容已省略]";
    if (secretKey(key)) return REDACTED;
    if (typeof value === "string") return this.text(value);
    if (!value || typeof value !== "object") return value;
    if (Array.isArray(value)) {
      if (key === "images") return value.map(() => "[媒体二进制已省略]");
      return value.map(item => this.value(item, "", depth + 1));
    }
    const object = value as Record<string, unknown>;
    if ((object.type === "base64" && typeof object.data === "string") ||
        (typeof object.mimeType === "string" && typeof object.data === "string")) {
      return { ...Object.fromEntries(Object.entries(object).filter(([name]) => name !== "data")
        .map(([name, item]) => [name, this.value(item, name, depth + 1)])),
      data: `[媒体二进制已省略: ${Math.floor(String(object.data).length * 3 / 4)} bytes]` };
    }
    return Object.fromEntries(Object.entries(object).map(([name, item]) => [this.text(name), this.value(item, name, depth + 1)]));
  }
  headers(headers: Headers): Record<string, string> {
    return Object.fromEntries([...headers].filter(([key]) => ALLOWED_HEADERS.has(key.toLowerCase()))
      .map(([key, value]) => [key, this.text(value)]));
  }
}

export interface AttemptMeta {
  protocol: string;
  transportProtocol?: string;
  model: string;
  endpoint: string;
  method: string;
  timeoutMs?: number;
  engineVersion?: string;
  retryReason?: string;
  retryOfRecordId?: string;
  stepId?: string;
  attempt?: number;
  [key: string]: unknown;
}
const taskAttempts = new WeakMap<ModelTraceContext, { count: number; last?: ModelTraceAttempt }>();
const recordIds = new Map<string, string>();
let taskEventSeq = 0;
function recordScope(context: ModelTraceContext): string {
  return JSON.stringify([context.producerId, context.taskId, context.executionId, context.stepId ?? ""]);
}
export function currentModelTraceRecordId(): string | undefined {
  const context = contexts.getStore();
  return context ? recordIds.get(recordScope(context)) : undefined;
}
export class ModelTraceAttempt {
  readonly id = randomUUID();
  readonly startedAt = Date.now();
  readonly redactor: TraceRedactor;
  private seq = 0;
  private active = true;
  private responseBytes = 0;
  private requestBytes = 0;
  private firstResponseAt?: number;
  private firstTextAt?: number;
  private outcomeDone = false;
  constructor(private readonly context: ModelTraceContext, private readonly state: ModelTraceState,
    private readonly sink: ModelTraceProducer, readonly meta: AttemptMeta, secrets: string[]) {
    this.redactor = new TraceRedactor(secrets);
    const scope = taskAttempts.get(context) ?? { count: 0 };
    scope.count++; scope.last = this; taskAttempts.set(context, scope);
    recordIds.set(recordScope(context), this.id);
    while (recordIds.size > 2048) recordIds.delete(recordIds.keys().next().value!);
    const scopeMeta = Object.fromEntries(["taskId", "executionId", "accountId", "contactId", "sessionId", "source",
      "trigger", "businessJobId", "parentStepId", "resumeOfTaskId", "retryOfTaskId"].map(key => [key, context[key]]));
    this.event({ type: "start", meta: this.redactor.value({ ...scopeMeta, ...meta,
      recordId: this.id, kind: meta.kind ?? "api", startedAt: this.startedAt,
      stepId: meta.stepId ?? context.stepId ?? context.taskId, attempt: meta.attempt ?? scope.count,
      parameters: { timeoutMs: meta.timeoutMs ?? null }, provenance: {
        promptTemplateVersion: context.promptTemplateVersion ?? null,
        contextVersion: context.contextVersion ?? null, boundary: "provider-http-client",
      } }) });
  }
  private valid(): boolean {
    if (!this.active) return false;
    try {
      const next = this.sink.state(this.context);
      if (!next.collecting || next.collectionVersion !== this.state.collectionVersion || next.deleteVersion !== this.state.deleteVersion ||
          typeof next.retentionCutoffAt === "number" && this.startedAt < next.retentionCutoffAt) {
        this.active = false; return false;
      }
      return true;
    } catch { this.active = false; return false; }
  }
  private event(payload: Record<string, unknown>): void {
    if (!this.valid()) return;
    try {
      const accepted = this.sink.emit(this.context, { ...payload, recordId: this.id,
        eventSeq: payload.eventSeq ?? this.seq++, collectionVersion: this.state.collectionVersion, deleteVersion: this.state.deleteVersion });
      if (accepted === false) this.active = false;
    } catch { this.active = false; }
  }
  incomplete(reason: string, side: "request" | "response" = "response"): void {
    this.event({ type: "end", transportStatus: "pending", collectionStatus: "incomplete",
      incomplete: true, incompleteReason: reason, side });
  }
  content(kind: "request" | "response" | "events" | "result" | "text", text: string): void {
    if (text) this.openContent(kind)(text, true);
  }
  openContent(kind: "request" | "response" | "events" | "result" | "text"): (text: string, final?: boolean) => void {
    const eventSeq = this.seq++, contentId = `${kind}-${eventSeq}`;
    let chunkIndex = 0, byteOffset = 0, ended = false;
    const request = kind === "request";
    return (text, final = false) => {
      if (!this.valid() || ended) return;
      const remaining = SIDE_LIMIT - (request ? this.requestBytes : this.responseBytes);
      if (remaining <= 0) {
        if (text) this.incomplete("正文达到 16MiB 保存上限", request ? "request" : "response");
        ended = true; return;
      }
      const bytes = Buffer.from(text);
      let limit = Math.min(remaining, bytes.length);
      while (limit > 0 && limit < bytes.length && (bytes[limit]! & 0xc0) === 0x80) limit--;
      for (let offset = 0; offset < limit && this.valid();) {
        let end = Math.min(offset + CHUNK_BYTES, limit);
        while (end < limit && (bytes[end]! & 0xc0) === 0x80) end--;
        const chunk = bytes.subarray(offset, end).toString("utf8");
        this.event({ type: "chunk", kind, contentId, eventSeq, chunkIndex: chunkIndex++, byteOffset,
          text: chunk, final: final && end === limit, ...(final && end === limit ? { totalBytes: byteOffset + end - offset } : {}) });
        byteOffset += end - offset; offset = end;
      }
      if (!limit && final) this.event({ type: "chunk", kind, contentId, eventSeq, chunkIndex: chunkIndex++, byteOffset,
        text: "", final: true, totalBytes: byteOffset });
      if (request) this.requestBytes += limit; else this.responseBytes += limit;
      if (limit < bytes.length) { this.incomplete("正文达到 16MiB 保存上限", request ? "request" : "response"); ended = true; }
      if (final) ended = true;
    };
  }
  request(body: string | undefined, headers: Headers): void {
    if (!this.valid()) return;
    if (body && Buffer.byteLength(body) > 128 * 1024) {
      const capture = createStreamingRequestCapture(this, headers);
      if (capture) { for (let offset = 0; offset < body.length; offset += 8192) capture.pushText(body.slice(offset, offset + 8192)); capture.finish(); }
      return;
    }
    let safe: unknown;
    if (body === undefined) { this.incomplete("请求体格式不支持采集", "request"); safe = "[请求体未采集]"; }
    else {
      if (Buffer.byteLength(body) > WORK_LIMIT || !reserve(Buffer.byteLength(body))) {
        this.incomplete("请求脱敏工作缓冲超出预算", "request"); return;
      }
      try {
        try { safe = this.redactor.value(JSON.parse(body)); }
        catch { safe = this.redactor.text(body); }
      } finally { release(Buffer.byteLength(body)); }
    }
    const text = JSON.stringify({ method: this.meta.method, url: this.redactor.text(this.meta.endpoint),
      headers: this.redactor.headers(headers), body: safe });
    this.content("request", text);
  }
  received(status: number, headers: Headers): void {
    this.event({ type: "response", httpStatus: status, responseHeaders: this.redactor.headers(headers), receivedAt: Date.now() });
  }
  firstByte(): void { this.firstResponseAt ??= Date.now(); }
  firstText(): void { this.firstTextAt ??= Date.now(); }
  finish(transportStatus: string, extra: Record<string, unknown> = {}): void {
    this.event({ type: "end", transportStatus, finishedAt: Date.now(), firstResponseAt: this.firstResponseAt ?? null,
      firstTextAt: this.firstTextAt ?? null, ...this.redactor.value(extra) as object });
  }
  outcome(applicationStatus: string, result?: unknown, adoption = "pending"): void {
    if (this.outcomeDone) return;
    this.outcomeDone = true;
    if (result !== undefined) {
      try { this.content("result", JSON.stringify(this.redactor.value(result))); }
      catch { this.content("result", JSON.stringify({ omitted: "应用结果无法安全序列化" })); }
    }
    this.event({ type: "outcome", applicationStatus, adoption });
  }
  isActive(): boolean { return this.valid(); }
}
export function beginModelTraceAttempt(meta: AttemptMeta, secrets: string[] = [],
  context = contexts.getStore()): ModelTraceAttempt | undefined {
  if (!producer || !context) return undefined;
  try {
    const state = producer.state(context);
    if (!state.collecting) return undefined;
    return new ModelTraceAttempt(context, { ...state }, producer, meta, secrets);
  } catch { return undefined; }
}
export function modelTraceOutcome(applicationStatus: string, result?: unknown, adoption = "pending"): void {
  const context = contexts.getStore();
  if (context) taskAttempts.get(context)?.last?.outcome(applicationStatus, result, adoption);
}
export type ModelTraceTaskEventName = "native-compaction" | "native-compaction-completed" | "native-tool";
export interface ModelTraceTaskEventDetails {
  source?: "discussion-compact";
  trigger?: "automatic" | "manual";
  nativeSessionId?: string;
  nativeMessageId?: string;
  nativePartId?: string;
  nativeCallId?: string;
  toolName?: "read" | "unknown";
  toolStatus?: "pending" | "running" | "completed" | "error" | "permission-denied";
}
export function modelTraceTaskEvent(name: ModelTraceTaskEventName, details: ModelTraceTaskEventDetails = {}): void {
  const context = contexts.getStore();
  if (!context || !producer || !["native-compaction", "native-compaction-completed", "native-tool"].includes(name)) return;
  try {
    const state = producer.state(context); if (!state.collecting) return;
    // Construct an allowlisted metadata object even when called from JavaScript.
    // Native identifiers can originate in a provider tool call, so also apply
    // the same attempt's credential snapshot before crossing the process boundary.
    const safe: Record<string, unknown> = {};
    for (const key of ["nativeSessionId", "nativeMessageId", "nativePartId", "nativeCallId"] as const)
      if (typeof details[key] === "string" && /^[\w-]{1,256}$/u.test(details[key]!)) safe[key] = details[key];
    if (details.source === "discussion-compact") safe.source = details.source;
    if (["automatic", "manual"].includes(details.trigger ?? "")) safe.trigger = details.trigger;
    if (["read", "unknown"].includes(details.toolName ?? "")) safe.toolName = details.toolName;
    if (["pending", "running", "completed", "error", "permission-denied"].includes(details.toolStatus ?? "")) safe.toolStatus = details.toolStatus;
    const redactor = taskAttempts.get(context)?.last?.redactor ?? new TraceRedactor([]);
    producer.emit(context, { type: "task", recordId: randomUUID(), eventSeq: ++taskEventSeq,
      ...state, kind: name, name, startedAt: Date.now(), ...redactor.value(safe) as object });
  } catch { /* Diagnostics cannot fail business work. */ }
}

// Large protocol events are lexed rather than parsed into an object graph. A
// string's unsafe suffix stays in this bounded work buffer until it is proven
// not to be a credential. JSON escapes are decoded before comparison.
class StreamingJsonRedactor {
  private stack: Array<{ object: boolean; key: string; expectingKey: boolean; omitted: boolean }> = [];
  private omittedDepth = 0;
  private inString = false;
  private isKey = false;
  private escaped = "";
  private value = "";
  private key = "";
  private omit = false;
  private decided = false;
  private output = "";
  private malformed = false;
  private rejected = false;
  private primitive = "";
  private readonly carry: number;
  constructor(private redactor: TraceRedactor, private write: (text: string) => void,
    private scalar?: (key: string, value: unknown) => void, private maskTail = false) {
    this.carry = Math.max(2048, ...redactor.variants.map(value => value.length));
  }
  private emit(text: string): void {
    if (this.omittedDepth) return;
    this.output += text;
    if (this.output.length >= 8192) { this.write(this.output); this.output = ""; }
  }
  private flushPrimitive(): void {
    if (!this.primitive) return;
    if (this.omittedDepth || secretKey(this.stack.at(-1)?.key ?? "")) this.emit(JSON.stringify(REDACTED));
    else if (/^(?:true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)$/u.test(this.primitive)) {
      this.scalar?.(this.stack.at(-1)?.key ?? "", JSON.parse(this.primitive));
      this.emit(this.redactor.text(this.primitive) === this.primitive ? this.primitive : JSON.stringify(REDACTED));
    } else { this.emit('"[非法 JSON 片段已省略]"'); this.malformed = true; }
    this.primitive = "";
  }
  private flushString(final: boolean): void {
    if (this.isKey) return;
    if (!this.decided && (final || this.value.length >= 128)) {
      this.decided = true;
      this.omit ||= /^(?:data:|https?:\/\/)/iu.test(this.value) && /url|uri|image|audio|video/iu.test(this.key);
      this.omit ||= /^data:[\w/+.-]+;base64,/iu.test(this.value);
    }
    if (this.omit) { this.value = ""; return; }
    if (!this.decided || !final && this.value.length < this.carry + 8192) return;
    // Redact against the entire lookahead before splitting output; a key that
    // crosses the flush boundary is therefore replaced in this operation.
    this.value = this.redactor.text(this.value);
    let length = final ? this.value.length : this.value.length - this.carry;
    if (final && this.maskTail && TEXT_FIELDS.has(this.key)) {
      const encodedCarry = this.redactor.encodedBoundaries(this.value).carry;
      if (encodedCarry) this.value = this.value.slice(0, -encodedCarry) + REDACTED;
      for (const secret of this.redactor.variants) for (let count = Math.min(secret.length - 1, this.value.length); count > 0; count--)
        if (this.value.endsWith(secret.slice(0, count))) { this.value = this.value.slice(0, -count) + REDACTED; break; }
      length = this.value.length;
    }
    // Keep a high surrogate with its low surrogate if a UTF-16 input slice split it.
    if (!final && length && /[\uD800-\uDBFF]/u.test(this.value[length - 1]!)) length--;
    this.emit(JSON.stringify(this.value.slice(0, length)).slice(1, -1));
    this.value = this.value.slice(length);
  }
  push(input: string): void {
    if (this.rejected) return;
    for (const char of input) {
      if (this.inString) {
        if (this.escaped) {
          this.escaped += char;
          if (this.escaped === "\\u") continue;
          if (this.escaped.startsWith("\\u") && this.escaped.length < 6) continue;
          try { this.value += JSON.parse('"' + this.escaped + '"'); }
          catch { this.malformed = true; this.value += "[非法转义已省略]"; }
          this.escaped = ""; this.flushString(false); continue;
        }
        if (char === "\\") { this.escaped = "\\"; continue; }
        if (char === '"') {
          if (this.isKey) {
            const top = this.stack.at(-1); if (top) top.key = this.value;
            this.emit(JSON.stringify(this.redactor.text(this.value)));
          } else {
            if (!this.omittedDepth && this.value.length < 128 && !this.omit) this.scalar?.(this.key, this.redactor.text(this.value));
            this.flushString(true);
            if (this.omit) this.emit(secretKey(this.key) ? REDACTED : "[媒体或链接内容已省略]");
            this.emit('"');
          }
          this.inString = false; this.value = ""; continue;
        }
        if (this.omit) continue;
        this.value += char;
        if (this.isKey && this.value.length > 512) { this.value = "[过长字段名已省略]"; this.omit = true; }
        this.flushString(false); continue;
      }
      const top = this.stack.at(-1);
      if (!/[\s{}\[\],:"]/u.test(char)) { this.primitive = (this.primitive + char).slice(0, 1024); continue; }
      this.flushPrimitive();
      if (char === '"') {
        this.inString = true; this.isKey = !!top?.object && top.expectingKey;
        this.key = top?.key ?? ""; this.value = "";
        this.omit = !this.isKey && (this.omittedDepth > 0 || secretKey(this.key) || /^(?:data|images|b64_json)$/u.test(this.key));
        this.decided = this.omit;
        if (!this.isKey) this.emit('"');
      } else {
        if (char === "{" || char === "[") {
          if (this.stack.length >= 128) { this.malformed = true; this.rejected = true; this.stack = []; return; }
          const omitted = this.omittedDepth > 0 || secretKey(top?.key ?? "");
          if (omitted) { this.emit(JSON.stringify(REDACTED)); this.omittedDepth++; }
          this.stack.push({ object: char === "{", key: top?.key ?? "", expectingKey: char === "{", omitted });
        }
        else if (char === "}" || char === "]") {
          this.emit(char);
          if (this.stack.pop()?.omitted) this.omittedDepth--;
          continue;
        }
        else if (char === ":" && top?.object) top.expectingKey = false;
        else if (char === "," && top?.object) { top.expectingKey = true; top.key = ""; }
        this.emit(char);
      }
    }
  }
  finish(): boolean {
    this.flushPrimitive();
    // Do not emit an unsafe partial string at a broken transport boundary.
    if (this.inString || this.escaped || this.stack.length) this.malformed = true;
    if (this.output) this.write(this.output);
    this.output = ""; this.value = ""; this.stack = [];
    return !this.malformed;
  }
  discard(): void {
    this.output = ""; this.value = ""; this.key = ""; this.escaped = ""; this.primitive = ""; this.stack = [];
    this.rejected = true;
  }
}

const STREAM_WORK_BYTES = 256 * 1024;
function createStreamingRequestCapture(attempt: ModelTraceAttempt, headers: Headers) {
  if (!reserve(STREAM_WORK_BYTES)) { attempt.incomplete("诊断脱敏缓冲预算不足", "request"); return undefined; }
  const write = attempt.openContent("request"), decoder = new TextDecoder();
  const meta = JSON.stringify({ method: attempt.meta.method, url: attempt.redactor.text(attempt.meta.endpoint), headers: attempt.redactor.headers(headers) });
  write(meta.slice(0, -1) + ',"body":');
  const lexer = new StreamingJsonRedactor(attempt.redactor, text => write(text));
  let ended = false;
  const canCollect = () => {
    if (ended) return false;
    if (attempt.isActive()) return true;
    ended = true; lexer.discard(); release(STREAM_WORK_BYTES); return false;
  };
  const failed = () => {
    if (ended) return;
    ended = true; lexer.discard(); release(STREAM_WORK_BYTES);
    attempt.incomplete("请求诊断解析失败，正文未完整采集", "request");
  };
  return {
    pushText(text: string) { try { if (canCollect()) lexer.push(text); } catch { failed(); } },
    push(bytes: Uint8Array) {
      try {
        for (let offset = 0; offset < bytes.byteLength && canCollect(); offset += 32 * 1024)
          lexer.push(decoder.decode(bytes.subarray(offset, Math.min(bytes.byteLength, offset + 32 * 1024)), { stream: true }));
      } catch { failed(); }
    },
    finish(error?: unknown) {
      if (!canCollect()) return;
      try {
        lexer.push(decoder.decode());
        if (!lexer.finish() || error) attempt.incomplete("请求体未完整结束", "request");
        write("}", true);
      } catch { attempt.incomplete("请求诊断解析失败，正文未完整采集", "request"); }
      finally { ended = true; lexer.discard(); release(STREAM_WORK_BYTES); }
    },
  };
}

interface BufferedWireEvent { value: unknown; prefix: string; suffix: string; bytes: number }
interface TextField { parent: Record<string, unknown> | unknown[]; key: string | number }
interface TextSlot extends TextField { value: string; event: number; stream: string }
const TEXT_FIELDS = new Set(["text", "content", "delta", "arguments", "partial_json", "output_text", "reasoning_content", "thinking", "reasoning", "message", "output"]);
function visibleTextFields(parsed: any): TextField[] | undefined {
  if (!parsed || typeof parsed !== "object") return undefined;
  const field = (parent: any, key: string): TextField[] | undefined => parent?.[key] == null ? undefined :
    typeof parent[key] === "string" ? [{ parent, key }] : [];
  const texts = (parts: unknown, type?: string) => Array.isArray(parts) ? parts
    .filter(part => part && typeof part.text === "string" && (!type || part.type === type) && !part.thought)
    .map(parent => ({ parent, key: "text" })) : undefined;
  return field(parsed.choices?.[0]?.delta, "content") ?? field(parsed.choices?.[0]?.message, "content") ??
    (parsed.type === "response.output_text.delta" ? field(parsed, "delta") : undefined) ??
    (parsed.delta?.type === "text_delta" ? field(parsed.delta, "text") : undefined) ?? field(parsed, "output_text") ??
    (Array.isArray(parsed.output) ? texts(parsed.output.filter((item: any) => item?.type === "message")
      .flatMap((item: any) => Array.isArray(item.content) ? item.content : []), "output_text") : undefined) ??
    field(parsed.message, "content") ?? texts(parsed.candidates?.[0]?.content?.parts) ?? texts(parsed.content, "text");
}
export function visibleText(parsed: unknown): string | undefined {
  return visibleTextFields(parsed)?.map(({ parent, key }) => (parent as Record<string | number, unknown>)[key]).join("");
}
export interface NormalizedUsage {
  accountingVersion: 1;
  inputTokens: number | null;
  outputTokens: number | null;
  reasoningTokens: number | null;
  cacheReadTokens: number | null;
  cacheWriteTokens: number | null;
  toolUsePromptTokens: number | null;
  totalInputTokens: number | null;
  totalOutputTokens: number | null;
  totalTokens: number | null;
  totalTokensSource: "provider" | "derived" | null;
  inputAccounting: string;
}

/** Streaming usage is cumulative, not additive; null delta fields carry no update. */
export function mergeUsageCounters(previous: Record<string, unknown> | undefined, next: unknown, depth = 0): Record<string, unknown> | undefined {
  if (!next || typeof next !== "object" || Array.isArray(next)) return previous;
  const merged = { ...previous };
  for (const [key, item] of Object.entries(next)) {
    if (item === null || item === undefined) continue;
    let mergedItem = item;
    if (item && typeof item === "object" && !Array.isArray(item) && depth < 8) {
      const before = merged[key];
      mergedItem = mergeUsageCounters(before && typeof before === "object" && !Array.isArray(before)
        ? before as Record<string, unknown> : undefined, item, depth + 1);
    }
    Object.defineProperty(merged, key, { value: mergedItem, enumerable: true, writable: true, configurable: true });
  }
  return merged;
}

export function normalizedUsage(raw: unknown, protocol: string): NormalizedUsage | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const value = raw as any;
  const count = (number: unknown): number | null => typeof number === "number" && Number.isSafeInteger(number) && number >= 0 ? number : null;
  const sum = (...numbers: Array<number | null>): number | null => numbers.every(number => number !== null)
    ? count(numbers.reduce<number>((total, number) => total + number!, 0)) : null;
  const known = ["anthropic", "gemini", "ollama", "responses", "chat_completions", "systemone"].includes(protocol);
  const providerTotal = protocol === "gemini" ? count(value.totalTokenCount)
    : count(value.total_tokens) ?? (known ? null : count(value.totalTokenCount));
  const result: NormalizedUsage = { accountingVersion: 1, inputTokens: null, outputTokens: null, reasoningTokens: null,
    cacheReadTokens: null, cacheWriteTokens: null, toolUsePromptTokens: null, totalInputTokens: null,
    totalOutputTokens: null, totalTokens: providerTotal, totalTokensSource: providerTotal === null ? null : "provider",
    inputAccounting: "unknown protocol; no inferred input or output accounting" };
  if (protocol === "anthropic") {
    result.inputTokens = count(value.input_tokens); result.outputTokens = count(value.output_tokens);
    result.cacheReadTokens = count(value.cache_read_input_tokens); result.cacheWriteTokens = count(value.cache_creation_input_tokens);
    result.reasoningTokens = count(value.output_tokens_details?.thinking_tokens);
    // The Messages SDK defines total input as all three counters. A missing or
    // null cache counter is unknown, including for compatible local providers.
    result.totalInputTokens = sum(result.inputTokens, result.cacheReadTokens, result.cacheWriteTokens);
    result.totalOutputTokens = result.outputTokens;
    result.inputAccounting = "Anthropic input_tokens excludes cache counters; output includes thinking";
  } else if (protocol === "gemini") {
    result.inputTokens = count(value.promptTokenCount); result.outputTokens = count(value.candidatesTokenCount);
    result.reasoningTokens = count(value.thoughtsTokenCount); result.cacheReadTokens = count(value.cachedContentTokenCount);
    result.toolUsePromptTokens = count(value.toolUsePromptTokenCount);
    // UsageMetadata defines total = prompt + tool-use prompt + candidates +
    // thoughts. Cache is already in prompt; absent optional counters stay unknown.
    result.totalInputTokens = sum(result.inputTokens, result.toolUsePromptTokens);
    result.totalOutputTokens = sum(result.outputTokens, result.reasoningTokens);
    result.inputAccounting = "Gemini prompt includes cache; tool-use prompt and thoughts are separate counters";
  } else if (protocol === "ollama") {
    result.inputTokens = count(value.prompt_eval_count); result.outputTokens = count(value.eval_count);
    result.totalInputTokens = result.inputTokens; result.totalOutputTokens = result.outputTokens;
    result.inputAccounting = "Ollama prompt_eval_count and eval_count are input and output totals";
  } else if (protocol === "systemone") {
    result.inputTokens = count(value.input_tokens);
    result.outputTokens = 0;
    result.totalInputTokens = result.inputTokens; result.totalOutputTokens = 0;
    result.inputAccounting = "System One input_tokens counts all questions; decisions generate no output tokens";
  } else if (protocol === "responses" || protocol === "chat_completions") {
    result.inputTokens = count(value.input_tokens ?? value.prompt_tokens); result.outputTokens = count(value.output_tokens ?? value.completion_tokens);
    result.reasoningTokens = count(value.output_tokens_details?.reasoning_tokens ?? value.completion_tokens_details?.reasoning_tokens);
    result.cacheReadTokens = count(value.input_tokens_details?.cached_tokens ?? value.prompt_tokens_details?.cached_tokens);
    result.cacheWriteTokens = count(value.input_tokens_details?.cache_write_tokens ?? value.prompt_tokens_details?.cache_write_tokens);
    result.totalInputTokens = result.inputTokens; result.totalOutputTokens = result.outputTokens;
    result.inputAccounting = "OpenAI input and output totals include reported cache and reasoning subsets";
  }
  if (result.totalTokens === null) {
    result.totalTokens = sum(result.totalInputTokens, result.totalOutputTokens);
    if (result.totalTokens !== null) result.totalTokensSource = "derived";
  }
  return result;
}
/** Parse before redacting: equivalent escaped strings and cross-event deltas are protected. */
class WireCollector {
  private pending = "";
  private reserved = 0;
  private queue: BufferedWireEvent[] = [];
  private stopped = false;
  private readonly decoder = new TextDecoder();
  private mode: "sse" | "ndjson" | "json";
  private usage: unknown;
  private finishReason: unknown;
  private terminal = false;
  private large?: { lexer: StreamingJsonRedactor; write: (text: string, final?: boolean) => void; suffix: string };
  constructor(private attempt: ModelTraceAttempt, contentType: string) {
    this.mode = /event-stream/iu.test(contentType) ? "sse" : /ndjson/iu.test(contentType) ? "ndjson" : "json";
  }
  push(bytes: Uint8Array): void {
    if (this.stopped || !this.attempt.isActive()) { this.stop(); return; }
    this.attempt.firstByte();
    for (let offset = 0; offset < bytes.byteLength && !this.stopped; offset += 32 * 1024)
      this.part(bytes.subarray(offset, Math.min(bytes.byteLength, offset + 32 * 1024)));
  }
  private part(bytes: Uint8Array): void {
    if (!reserve(bytes.byteLength * 2)) { this.fail("诊断脱敏缓冲预算不足"); return; }
    this.reserved += bytes.byteLength * 2;
    this.pending += this.decoder.decode(bytes, { stream: true });
    if (this.mode === "json") {
      if (!this.large && this.pending.length > 128 * 1024) this.startLarge();
      if (this.large) { this.large.lexer.push(this.pending); this.pending = ""; this.account(); }
      return;
    }
    const separator = this.mode === "sse" ? /\r?\n\r?\n/u : /\r?\n/u;
    for (;;) {
      const match = separator.exec(this.pending); if (!match) break;
      const raw = this.pending.slice(0, match.index); this.pending = this.pending.slice(match.index + match[0].length);
      if (this.large) { this.large.lexer.push(raw); this.finishLarge(); }
      else this.frame(raw);
    }
    if (!this.large && this.pending.length > 128 * 1024) this.startLarge();
    if (this.large && this.pending.length > 4) {
      this.large.lexer.push(this.pending.slice(0, -4)); this.pending = this.pending.slice(-4);
    }
    this.account();
  }
  private startLarge(): void {
    this.flush(true);
    if (!reserve(STREAM_WORK_BYTES)) { this.fail("大协议事件脱敏缓冲预算不足"); return; }
    const write = this.attempt.openContent("response");
    let prefix = "", suffix = this.mode === "sse" ? "\n\n" : "\n";
    if (this.mode === "sse") {
      const marker = /(?:^|\n)data: ?/u.exec(this.pending);
      if (!marker) { release(STREAM_WORK_BYTES); this.fail("大协议事件缺少 data 边界"); return; }
      const start = marker.index + marker[0].length;
      prefix = this.attempt.redactor.text(this.pending.slice(0, start));
      this.pending = this.pending.slice(start);
    }
    if (prefix) write(prefix);
    this.large = { write, suffix, lexer: new StreamingJsonRedactor(this.attempt.redactor, text => write(text), (key, value) => {
      if (["finish_reason", "stop_reason", "done_reason", "finishReason"].includes(key) && typeof value === "string" && value) {
        this.finishReason = value; this.terminal = true;
      }
      if (key === "done" && value === true || key === "type" && ["response.completed", "message_stop"].includes(String(value))) this.terminal = true;
    }, true) };
  }
  private finishLarge(): void {
    if (!this.large) return;
    if (!this.large.lexer.finish()) this.attempt.incomplete("大协议事件未完整结束；按已采集文本查看");
    this.large.write(this.large.suffix, true);
    this.large = undefined; release(STREAM_WORK_BYTES);
  }
  private account(): void {
    const bytes = Buffer.byteLength(this.pending) * 2 + this.queue.reduce((n, item) => n + item.bytes, 0);
    if (bytes < this.reserved) { release(this.reserved - bytes); this.reserved = bytes; }
  }
  private frame(raw: string): void {
    if (!raw.trim()) return;
    let payload = raw, prefix = "", suffix = "\n";
    if (this.mode === "sse") {
      const lines = raw.split(/\r?\n/u);
      payload = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
      prefix = lines.filter(line => !line.startsWith("data:")).map(line => this.attempt.redactor.text(line)).join("\n");
      prefix = (prefix ? prefix + "\n" : "") + "data: "; suffix = "\n\n";
      if (payload === "[DONE]") { this.terminal = true; this.flush(true); this.attempt.content("response", prefix + payload + suffix); return; }
      if (!payload) { this.attempt.content("response", this.attempt.redactor.text(raw) + "\n\n"); return; }
    }
    let parsed: any;
    try { parsed = JSON.parse(payload); } catch {
      if (this.mode === "json") {
        this.attempt.content("response", this.attempt.redactor.text(payload)); return;
      }
      // An arbitrary malformed frame can split an encoded credential. Never
      // expose a fragment whose semantic boundaries cannot be checked.
      this.attempt.incomplete("未能安全解析的协议片段已省略"); return;
    }
    const fields = parsed && typeof parsed === "object" ? parsed : {};
    const usage = fields.usage ?? fields.response?.usage ?? fields.message?.usage ?? fields.usageMetadata;
    const visible = visibleText(parsed);
    if (typeof visible === "string" && visible.length) this.attempt.firstText();
    if (usage) this.usage = mergeUsageCounters(this.usage as Record<string, unknown> | undefined, this.attempt.redactor.value(usage));
    if (fields.eval_count !== undefined || fields.prompt_eval_count !== undefined)
      this.usage = mergeUsageCounters(this.usage as Record<string, unknown> | undefined,
        { prompt_eval_count: fields.prompt_eval_count, eval_count: fields.eval_count });
    this.finishReason = fields.choices?.[0]?.finish_reason ?? fields.stop_reason ?? fields.delta?.stop_reason ??
      fields.candidates?.[0]?.finishReason ?? fields.done_reason ?? this.finishReason;
    if (this.finishReason || fields.done === true || ["response.completed", "message_stop"].includes(fields.type)) this.terminal = true;
    const value = this.attempt.redactor.value(parsed);
    this.queue.push({ value, prefix, suffix, bytes: Buffer.byteLength(raw) * 2 });
    this.flush(false);
  }
  private flush(final: boolean): void {
    const slots: TextSlot[] = [];
    const groups = new Map<string, TextSlot[]>();
    const add = (stream: string, slot: TextSlot) => {
      const group = groups.get(stream) ?? []; group.push(slot); groups.set(stream, group);
    };
    const visit = (value: unknown, event: number, path: string[]) => {
      if (!value || typeof value !== "object") return;
      if (Array.isArray(value)) {
        value.forEach((item, index) => visit(item, event, [...path,
          JSON.stringify(["index", Number.isSafeInteger(item?.index) ? item.index : index]) ]));
        return;
      }
      for (const [key, item] of Object.entries(value)) {
        const fieldPath = [...path, key];
        if (typeof item === "string" && TEXT_FIELDS.has(key)) {
          const slot = { parent: value as Record<string, unknown>, key, value: item, event, stream: JSON.stringify(fieldPath) };
          slots.push(slot); add(slot.stream, slot);
        } else if (typeof item === "object") visit(item, event, fieldPath);
      }
    };
    this.queue.forEach((item, event) => {
      const value = item.value as any;
      // Choice/tool indexes live in arrays; Anthropic blocks and Responses
      // items identify their stream on the event itself. Unrelated deltas must
      // never make a credential prefix in another stream appear safe to emit.
      const identity = value && typeof value === "object" ? [value.type ?? "", value.index ?? "",
        value.output_index ?? value.item_id ?? "", value.content_index ?? "", value.summary_index ?? ""] : [];
      const start = slots.length;
      visit(item.value, event, [JSON.stringify(identity)]);
      const eventSlots = slots.slice(start);
      for (const field of visibleTextFields(item.value) ?? []) {
        const slot = eventSlots.find(slot => slot.parent === field.parent && slot.key === field.key);
        if (slot) add("visible-text-projection", slot);
      }
    });
    const masks = new Map<TextSlot, Array<[number, number]>>();
    let retain = this.queue.length;
    for (const group of groups.values()) {
      const combined = group.map(slot => slot.value).join("");
      const encoded = this.attempt.redactor.encodedBoundaries(combined);
      const spans: Array<[number, number]> = [...encoded.spans];
      for (const secret of this.attempt.redactor.variants) {
        let index = combined.indexOf(secret);
        while (index >= 0) { spans.push([index, index + secret.length]); index = combined.indexOf(secret, index + secret.length); }
      }
      let carry = encoded.carry;
      for (const secret of this.attempt.redactor.variants) for (let length = Math.min(secret.length - 1, combined.length); length > carry; length--) {
        if (combined.endsWith(secret.slice(0, length))) { carry = length; break; }
      }
      if (final && carry) spans.push([combined.length - carry, combined.length]);
      let offset = 0;
      for (const slot of group) {
        const end = offset + slot.value.length;
        const matches = masks.get(slot) ?? [];
        for (const [left, right] of spans) if (left < end && right > offset)
          matches.push([Math.max(0, left - offset), Math.min(slot.value.length, right - offset)]);
        masks.set(slot, matches);
        if (!final && carry && end > combined.length - carry) retain = Math.min(retain, slot.event);
        offset = end;
      }
    }
    for (const slot of slots) {
      const merged: Array<[number, number]> = [];
      for (const span of (masks.get(slot) ?? []).sort((a, b) => a[0] - b[0])) {
        const last = merged.at(-1);
        if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
        else merged.push([...span]);
      }
      let value = slot.value;
      for (const [left, right] of merged.reverse()) value = value.slice(0, left) + REDACTED + value.slice(right);
      (slot.parent as Record<string | number, unknown>)[slot.key] = value;
    }
    for (const item of this.queue.splice(0, retain)) {
      this.attempt.content("response", item.prefix + JSON.stringify(item.value) + item.suffix);
      const parsed = item.value as any;
      const visible = visibleText(parsed);
      if (typeof visible === "string" && visible) this.attempt.content("text", visible);
    }
    this.account();
  }
  finish(status: number, error?: unknown): void {
    if (this.stopped) return;
    this.pending += this.decoder.decode();
    if (this.large) { this.large.lexer.push(this.pending); this.finishLarge(); }
    else if (this.pending.trim()) this.frame(this.pending);
    this.pending = ""; this.flush(true);
    // SDK readers cancel after a protocol terminal marker; that cancellation is
    // successful consumption, whereas cancelling before it is an interruption.
    const incomplete = (!!error && !this.terminal) || (this.mode !== "json" && status < 400 && !this.terminal);
    this.attempt.finish(incomplete ? "interrupted" : status >= 400 ? "failed" : "completed", {
      httpStatus: status, usage: this.usage ?? null, finishReason: this.finishReason ?? null,
      normalizedUsage: normalizedUsage(this.usage, this.attempt.meta.transportProtocol ?? this.attempt.meta.protocol), usageSource: "provider-wire",
      ...(incomplete ? { error: { code: "stream-incomplete", message: "流式连接在协议结束前中断" } } : {}),
    });
    this.stop();
  }
  private fail(reason: string): void { this.attempt.incomplete(reason); this.stop(); }
  abandon(): void { this.fail("响应诊断解析失败，正文未完整采集"); }
  private stop(): void {
    if (this.large) { this.large = undefined; release(STREAM_WORK_BYTES); }
    release(this.reserved); this.reserved = 0; this.pending = ""; this.queue = []; this.stopped = true;
  }
}

export function createModelTraceResponseCapture(attempt: ModelTraceAttempt | undefined, status: number, headers: Headers) {
  if (!attempt) return undefined;
  attempt.received(status, headers);
  const collector = new WireCollector(attempt, headers.get("content-type") ?? "");
  return {
    push(bytes: Uint8Array) { try { collector.push(bytes); } catch { collector.abandon(); } },
    finish(error?: unknown) { try { collector.finish(status, error); } catch { collector.abandon(); } },
  };
}

export function createModelTraceRequestCapture(attempt: ModelTraceAttempt | undefined, headers: Headers) {
  if (!attempt) return undefined;
  return createStreamingRequestCapture(attempt, headers);
}

/** Wrap the consumed business stream; no clone/tee queue and no disk waits. */
export function captureModelResponse(response: Response, attempt?: ModelTraceAttempt): Response {
  if (!attempt) return response;
  if (!response.body) { attempt.finish(response.ok ? "completed" : "failed", { httpStatus: response.status }); return response; }
  const collector = createModelTraceResponseCapture(attempt, response.status, response.headers)!;
  const reader = response.body.getReader();
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const next = await reader.read();
        if (next.done) { collector.finish(); controller.close(); }
        else { collector.push(next.value); controller.enqueue(next.value); }
      } catch (error) { collector.finish(error); controller.error(error); }
    },
    async cancel(reason) { collector.finish(reason ?? new Error("cancelled")); await reader.cancel(reason); },
  });
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
}

export async function traceProviderFetch(input: Parameters<typeof fetch>[0], init: RequestInit | undefined,
  meta: AttemptMeta, secrets: string[], fetcher: typeof fetch = globalThis.fetch): Promise<Response> {
  const attempt = beginModelTraceAttempt(meta, secrets);
  if (attempt) {
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    if (typeof init?.body === "string") attempt.request(init.body, headers);
    else if (input instanceof Request && !init?.body && input.body) {
      const capture = createStreamingRequestCapture(attempt, headers), reader = input.body.getReader();
      const body = new ReadableStream<Uint8Array>({
        async pull(controller) {
          try { const next = await reader.read();
            if (next.done) { capture?.finish(); controller.close(); }
            else { capture?.push(next.value); controller.enqueue(next.value); }
          } catch (error) { capture?.finish(error); controller.error(error); }
        },
        async cancel(reason) { capture?.finish(reason ?? new Error("cancelled")); await reader.cancel(reason); },
      });
      input = new Request(input, { body, duplex: "half" } as RequestInit);
    } else if (init?.body instanceof Uint8Array) attempt.request(new TextDecoder().decode(init.body), headers);
    else attempt.request(init?.body == null ? "" : undefined, headers);
  }
  try { return captureModelResponse(await fetcher(input, init), attempt); }
  catch (error) {
    attempt?.finish("failed", { error: { code: error instanceof Error ? error.name : "network", message: "模型传输未完成；未确认服务收到请求" } });
    throw error;
  }
}
