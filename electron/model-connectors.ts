// Network model adapters. The local Laya engine remains the application's default.
// Provider SDKs own their wire formats; this file owns validation and the network boundary.
// Know Your Friends modification (2026-10-09): allow explicit RFC1918 IPv4 HTTP endpoints.
// Modified 2026-10-10 (Apache-2.0): bounded inline images, never provider URL fetches.
// Modified 2026-10-10 (Apache-2.0): optional no-thinking structured requests on chat-template servers.
// Modified 2026-10-20 (Apache-2.0): streamed structured requests keep usage and truncation, with a bounded fallback.

import { isIP } from "node:net";
import { traceProviderFetch, modelTraceOutcome } from "./model-traces";
import { DEFAULT_MODEL_TIMEOUT_MS, MAX_MODEL_TIMEOUT_MS, modelRequestInit, modelTimeoutMs } from "./model-timeout";

export type Protocol = "anthropic" | "responses" | "chat_completions" | "gemini" | "ollama";

export interface ModelConfig {
  protocol: Protocol;
  /** Complete API prefix: e.g. /v1, /v1beta, or the Ollama server root. */
  baseUrl: string;
  model: string;
  apiKey?: string;
  contextTokens?: number;
  /** User-selected generation deadline, shared by answers and summaries. */
  timeoutMs?: number;
  /**
   * The baseUrl is Core's model gateway, which applies timeoutMs from when it forwards the request.
   * The caller then sets no deadline of its own, so time spent in the gateway's queue never counts.
   */
  gatewayDeadline?: boolean;
}

export interface ListedModel {
  id: string;
  name?: string;
  contextTokens?: number;
}

export interface ModelListResult {
  models: ListedModel[];
  /** False means the gateway cannot enumerate models; a manually entered ID can still work. */
  supported: boolean;
}

export interface ModelUsage {
  inputTokens?: number;
  outputTokens?: number;
}

export interface GenerationRequest {
  system: string;
  prompt: string;
  /** Optional provider output cap. Omit to let the selected model use its default. */
  maxOutputTokens?: number;
  /** Reuse a provider Responses session when the gateway supports it. */
  previousResponseId?: string;
  /** Persist the provider response so a later batch can reference it. */
  store?: boolean;
  /** Consume provider SSE chunks when available and expose firstBodyMs. */
  stream?: boolean;
  onTextDelta?: (delta: string) => void;
  signal?: AbortSignal;
  /** A bounded per-request timeout; larger portrait batches may need longer than a probe. */
  timeoutMs?: number;
  /** Ask compatible providers for a JSON object; callers still validate every field. */
  jsonMode?: boolean;
  /**
   * Ask self-hosted chat-template servers (vLLM, SGLang, llama.cpp) to answer without a
   * thinking phase. Servers that reject the template field are retried once without it.
   */
  disableThinking?: boolean;
  /** Verified inline image bytes; consumed transiently, never stored by this adapter. */
  images?: GenerationImage[];
}

export interface GenerationImage {
  mime: "image/jpeg" | "image/png" | "image/webp";
  data: string;
}

function validatedImages(value: GenerationImage[] | undefined): GenerationImage[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length < 1 || value.length > 6) invalid("图像请求超出允许范围");
  let total = 0;
  for (const image of value) {
    if (!image || !["image/jpeg", "image/png", "image/webp"].includes(image.mime) ||
        typeof image.data !== "string" || image.data.length > 2_796_204 ||
        image.data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(image.data)) {
      invalid("图像请求格式不正确");
    }
    const bytes = Buffer.from(image.data, "base64");
    total += bytes.length;
    const matches = image.mime === "image/jpeg" ? bytes.subarray(0, 3).equals(Buffer.from([255, 216, 255])) :
      image.mime === "image/png" ? bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) :
        bytes.subarray(0, 4).toString() === "RIFF" && bytes.subarray(8, 12).toString() === "WEBP";
    if (!matches || bytes.length > 2 * 1024 * 1024 || total > 6 * 1024 * 1024 ||
        bytes.toString("base64") !== image.data) invalid("图像请求格式不正确");
  }
  return value;
}

export interface GenerationResult {
  /** Provider text, without parsing or assigning application scores. */
  text: string;
  usage?: ModelUsage;
  responseId?: string;
  timings?: { firstBodyMs?: number; connectorMs?: number };
}

export type ConnectorErrorCode =
  | "invalid-url" | "invalid-request" | "cancelled" | "timeout"
  | "response-too-large" | "context-too-long" | "auth" | "rate-limit" | "unsupported"
  | "provider-error" | "invalid-output" | "network" | "empty-response" | "output-truncated";

export class ModelConnectorError extends Error {
  constructor(
    readonly code: ConnectorErrorCode,
    message: string,
    readonly status?: number,
    /** The text that arrived before the cut, when a provider stopped at its output cap. */
    readonly partialText?: string,
  ) {
    super(message);
    this.name = "ModelConnectorError";
  }
}

// Inference follows the saved deadline; probes and discovery remain short.
const REQUEST_TIMEOUT_MS = DEFAULT_MODEL_TIMEOUT_MS;
// The longest timer Node keeps (about 24.8 days); used where the gateway owns the deadline.
const NO_CLIENT_DEADLINE_MS = 2_147_483_647;
const LIST_TIMEOUT_MS = 12_000;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_PROMPT_BYTES = 3 * 1024 * 1024;
const MAX_MODELS = 200;
const NO_KEY = "local-no-key";
const PROTOCOLS: readonly Protocol[] = [
  "anthropic", "responses", "chat_completions", "gemini", "ollama",
];

interface SafeConfig {
  protocol: Protocol;
  baseUrl: string;
  model: string;
  apiKey: string;
}

function invalid(message: string, code: ConnectorErrorCode = "invalid-request"): never {
  throw new ModelConnectorError(code, message);
}

function isLoopback(host: string): boolean {
  return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
}

function isPrivateIPv4(host: string): boolean {
  if (isIP(host) !== 4) return false;
  const [first, second] = host.split(".").map(Number);
  return first === 10 || (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168);
}

/** Validates a user-supplied endpoint before any SDK can send a request. */
export function validateModelConfig(config: ModelConfig, requireModel = true): SafeConfig {
  if (!config || !PROTOCOLS.includes(config.protocol)) invalid("不支持的模型接入协议");
  if (typeof config.baseUrl !== "string" || !config.baseUrl.trim() ||
      config.baseUrl.length > 2048 || /[\s\\]/u.test(config.baseUrl)) {
    invalid("Base URL 格式不正确", "invalid-url");
  }
  let url: URL;
  try {
    url = new URL(config.baseUrl);
  } catch {
    invalid("Base URL 格式不正确", "invalid-url");
  }
  if (!url! || !["http:", "https:"].includes(url.protocol) || !url.hostname ||
      url.username || url.password || url.search || url.hash) {
    invalid("Base URL 只能包含 HTTP(S) 地址和路径", "invalid-url");
  }
  if (url.protocol === "http:" && !isLoopback(url.hostname) && !isPrivateIPv4(url.hostname)) {
    invalid("HTTP 仅支持本机或 IPv4 私有地址，其他服务请使用 HTTPS", "invalid-url");
  }
  if (typeof config.model !== "string" || (requireModel && !config.model.trim()) ||
      config.model.length > 256 || /[\u0000-\u001f\u007f]/u.test(config.model)) {
    invalid("模型 ID 格式不正确");
  }
  if (config.apiKey !== undefined && (typeof config.apiKey !== "string" ||
      config.apiKey.length > 8192 || /[\r\n]/u.test(config.apiKey))) {
    invalid("API Key 格式不正确");
  }
  if (config.contextTokens !== undefined &&
      (!Number.isSafeInteger(config.contextTokens) || config.contextTokens < 4096 ||
       config.contextTokens > 1000000)) invalid("上下文大小格式不正确");
  if (config.timeoutMs !== undefined) {
    try { modelTimeoutMs(config.timeoutMs); } catch { invalid("模型超时时间格式不正确"); }
  }
  return {
    protocol: config.protocol,
    baseUrl: url.toString().replace(/\/+$/u, ""),
    model: config.model.trim(),
    apiKey: config.apiKey?.trim() ?? "",
  };
}

function checkedSignal(requestSignal?: AbortSignal, callerSignal?: AbortSignal,
                       timeoutMs?: number): AbortSignal {
  const signals: AbortSignal[] = [];
  if (typeof timeoutMs === "number" && timeoutMs > 0) signals.push(AbortSignal.timeout(timeoutMs));
  if (requestSignal) signals.push(requestSignal);
  if (callerSignal) signals.push(callerSignal);
  if (!signals.length) return new AbortController().signal;
  return AbortSignal.any(signals);
}

async function boundedResponse(response: Response): Promise<Response> {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_RESPONSE_BYTES) {
    await response.body?.cancel();
    throw new ModelConnectorError("response-too-large", "服务响应超过大小限制");
  }
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (response.body) {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new ModelConnectorError("response-too-large", "服务响应超过大小限制");
      }
      chunks.push(value);
    }
  }
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  headers.delete("content-encoding");
  const body = chunks.length && ![204, 205, 304].includes(response.status)
    ? Buffer.concat(chunks) : null;
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function guardedFetch(config: SafeConfig, callerSignal?: AbortSignal,
                      timeoutMs = REQUEST_TIMEOUT_MS,
                      streamProtocol?: "responses" | "chat_completions"): typeof fetch {
  const base = new URL(config.baseUrl);
  const basePath = base.pathname.replace(/\/+$/u, "");
  let attempt = 0;
  return async (input, init) => {
    const target = new URL(input instanceof Request ? input.url : String(input));
    if (target.origin !== base.origin ||
        (basePath && target.pathname !== basePath && !target.pathname.startsWith(`${basePath}/`))) {
      throw new ModelConnectorError("invalid-url", "SDK 请求超出了配置的服务地址");
    }
    const signal = checkedSignal(init?.signal ?? (input instanceof Request ? input.signal : undefined),
      callerSignal, timeoutMs);
    const method = init?.method ?? (input instanceof Request ? input.method : "GET");
    // Discovery has no inference attempt. Final SDK payloads are captured here,
    // before bounded reads or compatibility conversion can change the response.
    const response = method.toUpperCase() === "POST" ? await traceProviderFetch(input,
      modelRequestInit({ ...init, redirect: "error", signal }), {
        protocol: config.protocol, transportProtocol: config.protocol, model: config.model,
        endpoint: target.toString(), method, timeoutMs, attempt: ++attempt,
        ...(attempt > 1 ? { retryReason: "连接器兼容回退：使用调整后的实际请求重新发送" } : {}),
      }, [config.apiKey, NO_KEY]) : await globalThis.fetch(input, modelRequestInit({ ...init, redirect: "error", signal }));
    const actualStream = typeof init?.body === "string" && /"stream"\s*:\s*true/u.test(init.body);
    if (actualStream && response.ok && streamProtocol &&
        !/text\/event-stream|application\/x-ndjson/iu.test(response.headers.get("content-type") || "")) {
      // Some compatible gateways ignore stream:true but still return a complete
      // JSON completion. Turn that one body into a local SSE envelope so the
      // caller observes one request and the normal incremental parser can run.
      const raw = await response.text();
      const headers = new Headers(response.headers);
      headers.set("content-type", "text/event-stream");
      headers.delete("content-length");
      let body = raw;
      try {
        const parsed = JSON.parse(raw) as Record<string, any>;
        if (streamProtocol === "chat_completions") {
          const choice = parsed.choices?.[0];
          const content = choice?.message?.content;
          if (typeof content === "string") {
            // Keep the finish reason and usage so a cut at the output cap is still reported.
            body = `data: ${JSON.stringify({
              id: parsed.id, object: "chat.completion.chunk",
              choices: [{ index: 0, delta: { content }, finish_reason: choice.finish_reason ?? "stop" }],
              ...(parsed.usage ? { usage: parsed.usage } : {}),
            })}\n\ndata: [DONE]\n\n`;
          }
        } else {
          const content = typeof parsed.output_text === "string" ? parsed.output_text : "";
          if (content) {
            const terminal = parsed.status === "incomplete" ? "response.incomplete" : "response.completed";
            body = `event: response.output_text.delta\ndata: ${JSON.stringify({
              type: "response.output_text.delta", delta: content,
            })}\n\n` +
              `event: ${terminal}\ndata: ${JSON.stringify({
                type: terminal, response: {
                  id: parsed.id, output_text: content, usage: parsed.usage,
                  status: parsed.status, incomplete_details: parsed.incomplete_details,
                },
              })}\n\n`;
          }
        }
      } catch {
        // Preserve the body. The connector will make its single compatibility
        // fallback only when the provider response is not parseable at all.
      }
      return new Response(body, { status: response.status, statusText: response.statusText, headers });
    }
    // Only a real SSE request reads incrementally; any other body, a stream fallback included, stays bounded.
    return actualStream ? response : boundedResponse(response);
  };
}

// Anthropic's SDK owns the /v1 path; most compatible-provider UIs expose the full /v1 URL.
function anthropicSdkBase(config: SafeConfig): string {
  return config.baseUrl.replace(/\/v1$/u, "");
}

// Ollama's SDK owns the /api path.
function ollamaSdkBase(config: SafeConfig): string {
  return config.baseUrl.replace(/\/api$/u, "");
}

function errorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined;
  const value = error as Record<string, unknown>;
  const status = value.status ?? value.statusCode ?? value.status_code;
  return typeof status === "number" && Number.isInteger(status) ? status : undefined;
}

// A provider that stops at the output cap returns syntactically broken JSON. Report
// that cut explicitly instead of letting callers misread it as a format error, and
// keep the text that did arrive so the caller can salvage it.
function outputTruncated(partial?: string): never {
  throw new ModelConnectorError("output-truncated", "模型输出达到长度上限被截断", undefined,
    typeof partial === "string" && partial ? partial : undefined);
}

function streamFormatError(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current && typeof current === "object"; depth++) {
    if ((current as { streamFormat?: boolean }).streamFormat) return true;
    if (current instanceof Error && /stream response is not an event stream/iu.test(current.message)) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

function streamFormatFailure(): Error {
  const error = new Error("stream response is not an event stream");
  (error as Error & { streamFormat?: boolean }).streamFormat = true;
  return error;
}

function safeError(error: unknown, signal?: AbortSignal): never {
  if (signal?.aborted) throw new ModelConnectorError("cancelled", "请求已取消");
  let current: unknown = error;
  let status: number | undefined;
  let timedOut = false;
  let contextRejected = false;
  // SDKs may wrap fetch failures. Only inspect their shape, never render raw messages.
  for (let depth = 0; depth < 5 && current && typeof current === "object"; depth++) {
    if (current instanceof ModelConnectorError) throw current;
    status ??= errorStatus(current);
    if (current instanceof Error && /Timeout|Abort/u.test(current.name)) timedOut = true;
    const record = current as Record<string, unknown>;
    const nested = record.error && typeof record.error === "object" ?
      record.error as Record<string, unknown> : null;
    const code = record.code ?? nested?.code;
    if (typeof code === "string" &&
        /^(?:context[_-]?length[_-]?exceeded|prompt[_-]?too[_-]?long|input[_-]?too[_-]?long|max[_-]?context[_-]?length[_-]?exceeded)$/iu.test(code))
      contextRejected = true;
    current = (current as { cause?: unknown }).cause;
  }
  if (status === 413 || contextRejected && [400, 413, 422].includes(status ?? -1))
    throw new ModelConnectorError("context-too-long", "模型拒绝了当前上下文长度", status);
  if (status === 401) throw new ModelConnectorError("auth", "API Key 无效或已过期", status);
  if (status === 403) throw new ModelConnectorError("auth", "服务拒绝访问", status);
  if (status === 429) throw new ModelConnectorError("rate-limit", "服务请求过于频繁", status);
  if ([404, 405, 501].includes(status ?? -1)) {
    throw new ModelConnectorError("unsupported", "服务不支持此接口", status);
  }
  if (status) throw new ModelConnectorError("provider-error", `服务返回 HTTP ${status}`, status);
  if (timedOut) throw new ModelConnectorError("timeout", "连接服务超时");
  throw new ModelConnectorError("network", "无法连接模型服务");
}

function normalizedModels(models: Array<{ id: string; name?: string; contextTokens?: number }>): ListedModel[] {
  const seen = new Set<string>();
  const result: ListedModel[] = [];
  for (const item of models) {
    const id = typeof item?.id === "string" ? item.id.trim() : "";
    if (!id || id.length > 256 || seen.has(id)) continue;
    seen.add(id);
    const name = typeof item.name === "string" ? item.name.trim().slice(0, 256) : "";
    const contextTokens = Number.isSafeInteger(item.contextTokens) &&
      item.contextTokens! >= 4096 && item.contextTokens! <= 1000000 ? item.contextTokens : undefined;
    result.push({ id, ...(name && name !== id ? { name } : {}),
      ...(contextTokens ? { contextTokens } : {}) });
    if (result.length >= MAX_MODELS) break;
  }
  return result;
}

function modelContextTokens(item: unknown): number | undefined {
  if (!item || typeof item !== "object") return undefined;
  const model = item as Record<string, unknown>;
  for (const value of [model.context_window, model.context_length, model.max_context_tokens,
    model.max_context_length, model.input_token_limit, model.inputTokenLimit]) {
    if (Number.isSafeInteger(value) && (value as number) >= 4096 &&
        (value as number) <= 1000000) return value as number;
  }
  return undefined;
}

/** Lists the first bounded page. Gateways without a list endpoint can use a manual model ID. */
export async function listModels(config: ModelConfig): Promise<ModelListResult> {
  const safe = validateModelConfig(config, false);
  try {
    let models: ListedModel[];
    switch (safe.protocol) {
      case "responses":
      case "chat_completions": {
        const { default: OpenAI } = await import("openai");
        const client = new OpenAI({ apiKey: safe.apiKey || NO_KEY, baseURL: safe.baseUrl,
          organization: null, project: null, adminAPIKey: null,
          fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS), maxRetries: 0,
          timeout: LIST_TIMEOUT_MS, logLevel: "off" });
        const page = await client.models.list();
        models = normalizedModels(page.data.map((item) => ({ id: item.id,
          contextTokens: modelContextTokens(item) })));
        break;
      }
      case "anthropic": {
        const { default: Anthropic } = await import("@anthropic-ai/sdk");
        const client = new Anthropic({ apiKey: safe.apiKey || NO_KEY, authToken: null,
          credentials: null, config: null, profile: null, baseURL: anthropicSdkBase(safe),
          fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS), maxRetries: 0,
          timeout: LIST_TIMEOUT_MS, logLevel: "off" });
        const page = await client.models.list({ limit: 100 });
        models = normalizedModels(page.data.map((item) => ({ id: item.id, name: item.display_name,
          contextTokens: modelContextTokens(item) })));
        break;
      }
      case "gemini": {
        const { GoogleGenAI } = await import("@google/genai");
        const client = new GoogleGenAI({ apiKey: safe.apiKey || NO_KEY, vertexai: false,
          httpOptions: { baseUrl: safe.baseUrl, apiVersion: "", timeout: LIST_TIMEOUT_MS,
            fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS) } });
        const page = await client.models.list({ config: { pageSize: 100 } });
        models = normalizedModels(page.page.map((item) => ({
          id: item.name?.replace(/^models\//u, "") ?? "", name: item.displayName,
          contextTokens: modelContextTokens(item),
        })));
        break;
      }
      case "ollama": {
        const { Ollama } = await import("ollama");
        const client = new Ollama({ host: ollamaSdkBase(safe),
          fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS),
          headers: safe.apiKey ? { Authorization: `Bearer ${safe.apiKey}` } : undefined });
        const page = await client.list();
        models = normalizedModels(page.models.map((item) => ({ id: item.model || item.name,
          contextTokens: modelContextTokens(item) })));
        break;
      }
    }
    return { models, supported: true };
  } catch (error) {
    // A proxy can support generation while denying or omitting its models endpoint.
    if ([404, 405, 501].includes(errorStatus(error) ?? -1)) {
      return { models: [], supported: false };
    }
    return safeError(error);
  }
}

function usage(input?: number, output?: number): ModelUsage | undefined {
  const inputTokens = Number.isFinite(input) && input! >= 0 ? input : undefined;
  const outputTokens = Number.isFinite(output) && output! >= 0 ? output : undefined;
  return inputTokens === undefined && outputTokens === undefined
    ? undefined : { inputTokens, outputTokens };
}

/** Sends a bounded prompt and returns raw model text for the caller to validate. */
export async function generateStructured(
  config: ModelConfig, request: GenerationRequest,
): Promise<GenerationResult> {
  const safe = validateModelConfig(config);
  const outputLimit = request?.maxOutputTokens;
  if (!request || typeof request.system !== "string" || typeof request.prompt !== "string" ||
      !request.prompt.trim() ||
      (outputLimit !== undefined && (!Number.isInteger(outputLimit) ||
        outputLimit < 1 || outputLimit > 32768)) ||
      (request.jsonMode !== undefined && typeof request.jsonMode !== "boolean") ||
      (request.disableThinking !== undefined && typeof request.disableThinking !== "boolean") ||
      (request.stream !== undefined && typeof request.stream !== "boolean") ||
      (request.timeoutMs !== undefined && (!Number.isInteger(request.timeoutMs) ||
        request.timeoutMs < 1000 || request.timeoutMs > MAX_MODEL_TIMEOUT_MS)) ||
      Buffer.byteLength(request.system, "utf8") + Buffer.byteLength(request.prompt, "utf8") > MAX_PROMPT_BYTES) {
    invalid("生成请求超出允许范围");
  }
  if (request.signal?.aborted) throw new ModelConnectorError("cancelled", "请求已取消");
  const images = validatedImages(request.images);
  const timeoutMs = config.gatewayDeadline ? NO_CLIENT_DEADLINE_MS : config.timeoutMs ?? request.timeoutMs ?? REQUEST_TIMEOUT_MS;
  const connectorStarted = performance.now();
  let firstBodyMs: number | undefined;
  const markFirstBody = () => { firstBodyMs ??= performance.now() - connectorStarted; };
  try {
    let result: GenerationResult;
    switch (safe.protocol) {
      case "responses": {
        const { default: OpenAI } = await import("openai");
        const client = new OpenAI({ apiKey: safe.apiKey || NO_KEY, baseURL: safe.baseUrl,
          organization: null, project: null, adminAPIKey: null,
          fetch: guardedFetch(safe, request.signal, timeoutMs,
            safe.protocol === "responses" ? "responses" : undefined), maxRetries: 0,
          timeout: timeoutMs, logLevel: "off" });
        const input = images.length ? [{ role: "user" as const, content: [
          { type: "input_text" as const, text: request.prompt }, ...images.map(image => ({
            type: "input_image" as const, image_url: `data:${image.mime};base64,${image.data}`, detail: "auto" as const,
          })),
        ] }] : request.prompt;
        const params = { model: safe.model, input,
          instructions: request.system, store: request.store ?? false,
          ...(request.previousResponseId ?
            { previous_response_id: request.previousResponseId } : {}),
          ...(request.maxOutputTokens !== undefined ?
            { max_output_tokens: request.maxOutputTokens } : {}) };
        // DeepSeek's official Responses API enables thinking by default, and its
        // output cap includes reasoning tokens. Structured analysis needs room
        // for the final JSON rather than spending the cap before any text appears.
        const officialDeepSeekJson = request.jsonMode &&
          new URL(safe.baseUrl).hostname.toLowerCase() === "api.deepseek.com";
        const requestBody = { ...params,
          ...(request.jsonMode ? { text: { format: { type: "json_object" as const } } } : {}),
          ...(officialDeepSeekJson ? { reasoning: { effort: "none" as const } } : {}) };
        let response: any;
        if (request.stream) {
          try {
            const stream = await client.responses.create({ ...requestBody, stream: true },
              { signal: request.signal });
            let text = "";
            let responseId: string | undefined;
            let responseUsage: any;
            let incomplete: unknown;
            let terminal = false;
            for await (const event of stream as AsyncIterable<any>) {
              if (event?.type === "response.output_text.delta" && typeof event.delta === "string") {
                markFirstBody();
                text += event.delta;
                request.onTextDelta?.(event.delta);
              }
              if (["response.created", "response.completed", "response.incomplete"].includes(event?.type)) {
                responseId = typeof event.response?.id === "string" ? event.response.id : responseId;
                responseUsage = event.response?.usage ?? responseUsage;
                if (!text && typeof event.response?.output_text === "string") {
                  text = event.response.output_text;
                }
                if (event.type !== "response.created") terminal = true;
                if (event.type === "response.incomplete") incomplete = event.response?.incomplete_details;
              }
              if (event?.type === "error" || event?.type === "response.failed")
                throw new Error(String(event.message || "provider stream error"));
            }
            if (!text.trim() && !terminal) throw streamFormatFailure();
            response = { output_text: text, id: responseId, usage: responseUsage,
              ...(incomplete ? { status: "incomplete", incomplete_details: incomplete } : {}) };
          } catch (error) {
            // Compatible gateways may support the non-stream Responses endpoint but
            // reject SSE. Send the same request once without the stream flag; do not loop it.
            if (!streamFormatError(error) && ![400, 404, 405, 422].includes(errorStatus(error) ?? -1)) throw error;
          }
        }
        if (!response) {
          try {
            response = await client.responses.create(requestBody, { signal: request.signal });
          } catch (error) {
            if (!request.jsonMode || ![400, 422].includes(errorStatus(error) ?? -1)) throw error;
            response = await client.responses.create({ ...params,
              ...(officialDeepSeekJson ? { reasoning: { effort: "none" as const } } : {}) },
            { signal: request.signal });
          }
        }
        if ((response as any).status === "incomplete" &&
            (response as any).incomplete_details?.reason === "max_output_tokens")
          outputTruncated(typeof response.output_text === "string" ? response.output_text : undefined);
        if (response.output_text) markFirstBody();
        result = { text: response.output_text || "",
          responseId: typeof response.id === "string" ? response.id : undefined,
          usage: usage(response.usage?.input_tokens, response.usage?.output_tokens) };
        break;
      }
      case "chat_completions": {
        const { default: OpenAI } = await import("openai");
        const client = new OpenAI({ apiKey: safe.apiKey || NO_KEY, baseURL: safe.baseUrl,
          organization: null, project: null, adminAPIKey: null,
          fetch: guardedFetch(safe, request.signal, timeoutMs,
            safe.protocol === "chat_completions" ? "chat_completions" : undefined), maxRetries: 0,
          timeout: timeoutMs, logLevel: "off" });
        const params = { model: safe.model,
          messages: [{ role: "system" as const, content: request.system },
            { role: "user" as const, content: images.length ? [
              { type: "text" as const, text: request.prompt }, ...images.map(image => ({
                type: "image_url" as const, image_url: { url: `data:${image.mime};base64,${image.data}` },
              })),
            ] : request.prompt }],
          ...(request.maxOutputTokens !== undefined ?
            { max_tokens: request.maxOutputTokens } : {}) };
        const json = request.jsonMode ? { response_format: { type: "json_object" as const } } : {};
        // A thinking model spends the output cap on reasoning before any JSON appears.
        // DeepSeek templates read `thinking`; Qwen and llama.cpp read `enable_thinking`.
        // Chat-template servers ignore the variable a template does not use.
        const noThinking = request.disableThinking ?
          { chat_template_kwargs: { thinking: false, enable_thinking: false } } : {};
        const requestBody = { ...params, ...json, ...noThinking };
        let response: any;
        if (request.stream) {
          try {
            // Usage arrives in a final chunk; the finish reason still reports a cut at the output cap.
            const stream = await client.chat.completions.create({ ...requestBody, stream: true,
              stream_options: { include_usage: true } }, { signal: request.signal });
            let text = "";
            let finishReason: string | undefined;
            let streamUsage: any;
            for await (const chunk of stream as AsyncIterable<any>) {
              const choice = chunk?.choices?.[0];
              const delta = choice?.delta?.content;
              if (typeof delta === "string") {
                markFirstBody(); text += delta; request.onTextDelta?.(delta);
              }
              finishReason = choice?.finish_reason ?? finishReason;
              streamUsage = chunk?.usage ?? streamUsage;
            }
            if (!text.trim() && !finishReason) throw streamFormatFailure();
            response = { choices: [{ message: { content: text }, finish_reason: finishReason }], usage: streamUsage };
          } catch (error) {
            // A server that rejects SSE gets the same request once without the stream flag.
            if (!streamFormatError(error) && ![400, 404, 405, 422].includes(errorStatus(error) ?? -1)) throw error;
          }
        }
        if (!response) {
          try {
            response = await client.chat.completions.create(requestBody, { signal: request.signal });
          } catch (error) {
            const status = errorStatus(error);
            const jsonRejected = request.jsonMode && [400, 422].includes(status ?? -1);
            const thinkingRejected = request.disableThinking && [400, 422].includes(status ?? -1);
            if (!jsonRejected && !thinkingRejected) throw error;
            let retried;
            if (thinkingRejected && request.jsonMode) {
              // Hosted APIs without chat templates reject the field; keep JSON mode once more.
              try {
                retried = await client.chat.completions.create({ ...params, ...json }, { signal: request.signal });
              } catch (retryError) {
                if (![400, 422].includes(errorStatus(retryError) ?? -1)) throw retryError;
              }
            }
            response = retried ?? await client.chat.completions.create(params, { signal: request.signal });
          }
        }
        if ((response.choices[0] as any)?.finish_reason === "length")
          outputTruncated(typeof response.choices[0]?.message?.content === "string"
            ? response.choices[0].message.content : undefined);
        if (response.choices[0]?.message.content) markFirstBody();
        result = { text: response.choices[0]?.message.content ?? "",
          usage: usage(response.usage?.prompt_tokens, response.usage?.completion_tokens) };
        break;
      }
      case "anthropic": {
        const { default: Anthropic } = await import("@anthropic-ai/sdk");
        const client = new Anthropic({ apiKey: safe.apiKey || NO_KEY, authToken: null,
          credentials: null, config: null, profile: null, baseURL: anthropicSdkBase(safe),
          fetch: guardedFetch(safe, request.signal, timeoutMs), maxRetries: 0,
          timeout: timeoutMs, logLevel: "off" });
        const params = { model: safe.model,
          system: request.system,
          ...(request.maxOutputTokens !== undefined ?
            { max_tokens: request.maxOutputTokens } : {}),
          messages: [{ role: "user", content: images.length ? [
            { type: "text", text: request.prompt }, ...images.map(image => ({ type: "image",
              source: { type: "base64", media_type: image.mime, data: image.data } })),
          ] : request.prompt }] };
        // Anthropic's SDK type requires max_tokens even when a compatible gateway
        // accepts the provider default. Keep the field absent on the wire.
        // An explicit per-request timeout stops the SDK refusing large non-streaming
        // caps (over ~21K tokens) by its own duration estimate. 600000 ms is the
        // SDK's own default when no timeout is configured.
        const response = await client.messages.create(params as any,
          { signal: request.signal, timeout: timeoutMs ?? 600_000 });
        const text = response.content.filter((item) => item.type === "text")
          .map((item) => item.text).join("\n");
        if (response.stop_reason === "max_tokens") outputTruncated(text);
        result = { text,
          usage: usage(response.usage.input_tokens, response.usage.output_tokens) };
        break;
      }
      case "gemini": {
        const { GoogleGenAI } = await import("@google/genai");
        const client = new GoogleGenAI({ apiKey: safe.apiKey || NO_KEY, vertexai: false,
          httpOptions: { baseUrl: safe.baseUrl, apiVersion: "", timeout: timeoutMs,
            fetch: guardedFetch(safe, request.signal, timeoutMs) } });
        const response = await client.models.generateContent({ model: safe.model,
          contents: images.length ? [{ role: "user", parts: [{ text: request.prompt },
            ...images.map(image => ({ inlineData: { mimeType: image.mime, data: image.data } }))] }] : request.prompt,
          config: { systemInstruction: request.system,
            ...(request.jsonMode ? { responseMimeType: "application/json" } : {}),
            ...(request.maxOutputTokens !== undefined ?
              { maxOutputTokens: request.maxOutputTokens } : {}),
            abortSignal: request.signal } });
        if (response.candidates?.[0]?.finishReason === "MAX_TOKENS")
          outputTruncated(typeof response.text === "string" ? response.text : undefined);
        result = { text: response.text ?? "",
          usage: usage(response.usageMetadata?.promptTokenCount,
            response.usageMetadata?.candidatesTokenCount) };
        break;
      }
      case "ollama": {
        const { Ollama } = await import("ollama");
        const client = new Ollama({ host: ollamaSdkBase(safe),
          fetch: guardedFetch(safe, request.signal, timeoutMs),
          headers: safe.apiKey ? { Authorization: `Bearer ${safe.apiKey}` } : undefined });
        const response = await client.chat({ model: safe.model, stream: false,
          ...(request.jsonMode ? { format: "json" } : {}),
          messages: [{ role: "system", content: request.system },
            { role: "user", content: request.prompt, ...(images.length ? { images: images.map(image => image.data) } : {}) }],
          options: { ...(request.maxOutputTokens !== undefined ?
            { num_predict: request.maxOutputTokens } : {}) } });
        if (response.done_reason === "length")
          outputTruncated(typeof response.message?.content === "string" ? response.message.content : undefined);
        result = { text: response.message?.content ?? "",
          usage: usage(response.prompt_eval_count, response.eval_count) };
        break;
      }
    }
    if (!result.text.trim()) throw new ModelConnectorError("empty-response", "模型没有返回文本");
    return { ...result, timings: { firstBodyMs, connectorMs: performance.now() - connectorStarted } };
  } catch (error) {
    modelTraceOutcome("failed", { stage: "connector-response", code: error instanceof ModelConnectorError ? error.code : "provider-response" }, "not-applicable");
    return safeError(error, request.signal);
  }
}

/** The only connection probe contains fixed synthetic text, never conversation content. */
export async function testConnection(config: ModelConfig): Promise<{
  ok: true; latencyMs: number; model: string;
}> {
  const start = performance.now();
  try {
    // A connection check never inherits the potentially hours-long generation limit.
    await generateStructured({ ...config, timeoutMs: undefined }, {
      system: "This is a connection test. Return only a JSON object.",
      prompt: 'Reply with JSON {"ok":true}.',
      jsonMode: true,
      // Anthropic requires max_tokens. 128 is enough for this probe's JSON.
      // Other protocols send no cap: a small cap is spent by reasoning before
      // any JSON appears. The timeout keeps the settings buttons from staying busy.
      ...(config.protocol === "anthropic" ? { maxOutputTokens: 128 } : {}),
      timeoutMs: 15_000,
    });
  } catch (error) {
    // A gateway can still cut the reply at its own default cap. The endpoint answered.
    if (!(error instanceof ModelConnectorError && error.code === "output-truncated")) throw error;
  }
  return { ok: true, latencyMs: Math.round(performance.now() - start), model: config.model.trim() };
}
