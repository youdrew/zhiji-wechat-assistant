// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// One advisor turn on DeepSeek Harness: start `dsh --profile acp` with a launch patch, open or resume the
// discussion's DSH session, send one prompt, relay its live output, audit its tool calls, close the session
// and end the process. A fresh process per turn (about a second) keeps one execution token per process and
// lets every turn carry its own system prompt and model route.
import path from "node:path";
import { pathToFileURL } from "node:url";
import type { Protocol } from "../../../electron/model-connectors";
import { acpClient, AcpClosedError, AcpError, type AcpHandlers, type AcpTransport } from "./acp";
import { AdvisorRuntimeError, record, type AdvisorRuntimeEvent, type AdvisorUsage } from "./contract";
import { launchHarness, type HarnessLaunch } from "./harness";
import { patchText } from "./harness-state";

export const HARNESS_PROVIDER = "zhiji";
export const RELAY_SECRET_ENV = "ZHIJI_RELAY_SECRET";
const CONTROL_TIMEOUT_MS = 30_000;
/** DSH protocol names for the gateway protocols it can reach through a hand-declared route. */
const HARNESS_APIS: Partial<Record<Protocol, string>> = {
  chat_completions: "openai-completions", ollama: "openai-completions", responses: "openai-responses", anthropic: "anthropic-messages",
};
// Everything of the shipped base profile the advisor does not use: shells, file mutation and search, subagents,
// workflows, goals, plans, DSH's own web tools and accounts, plugin management, remote access and telemetry.
const DISABLED_ROWS = ["tool-plugin-manager", "plugin-manager", "tool-working-directory", "tool-bash", "tool-pwsh", "tool-jobs", "jobs",
  "tool-fs-search", "agent-instructions", "skill-filesystem", "tool-skill", "commands", "command-feedback", "goal", "goal-round-driver",
  "command-goal", "plan-mode", "command-compact", "subagent", "subagent-spawn-in-process", "subagent-fork-in-process",
  "tool-subagent-control", "tool-subagent", "tool-subagent-fork", "ptc-runtime", "workflow-ptc", "tool-workflow", "tool-todo", "tool-goal",
  "web", "web-search-deepseek", "web-deepseek-api", "web-fetch-http", "tool-web", "tool-result-pruner", "remote-access",
  "deepseek-account", "llm-deepseek", "llm-deepseek-account", "session-log-deepseek", "session-title-llm", "otel", "session-telemetry-otel",
  "mcp-resources"];

export function harnessApi(protocol: Protocol): string {
  const api = HARNESS_APIS[protocol];
  if (!api) throw new AdvisorRuntimeError("protocol-unsupported");
  return api;
}

export interface PatchInput {
  protocol: Protocol; model: string; baseUrl: string; contextTokens: number; outputTokens: number;
  /** The complete persona: adviser rules, the assistant template, approved skills and restored discussion. */
  persona: string;
  workspaceRoot: string;
  /** The advisor's `engine` folder, which holds the dsh-stream host plugin. */
  engineDir: string;
}

/** The launch patch over DSH's shipped `acp` profile. */
export function harnessPatch(input: PatchInput): string {
  const streamPlugin = pathToFileURL(path.join(input.engineDir, "dsh-stream", "index.js")).href;
  return patchText([
    { id: "llm-pi-ai", config: { providers: { [HARNESS_PROVIDER]: { displayName: "知己模型网关", api: harnessApi(input.protocol),
      baseURL: input.baseUrl, apiKeyEnv: RELAY_SECRET_ENV,
      models: [{ id: input.model, name: input.model, contextWindow: input.contextTokens, maxTokens: input.outputTokens }] } } } },
    { id: "acp", config: { provider: HARNESS_PROVIDER, model: input.model } },
    { id: "agent-default-model", config: { provider: HARNESS_PROVIDER, model: input.model } },
    // Prompt assembly fails on unknown {{variables}}; persona text is literal.
    { id: "system-prompt", config: { personaPrefix: input.persona.replace(/\{\{/gu, "{ {") } },
    ...DISABLED_ROWS.map(id => ({ id, disabled: true })),
    // One read returns the whole managed context file; nothing can be written.
    { id: "tool-fs", config: { read: true, readImage: false, write: false, edit: false, applyPatch: false,
      readLimit: 200_000, readMaxLineLength: 2000, readMaxBytes: 16 * 1024 * 1024 } },
    { id: "sandbox-policy", config: { mode: "read-only", workspaceRoot: input.workspaceRoot } },
    // The backend rebuilds a long discussion well before this; DSH condenses only as a last resort.
    { id: "compaction-basic", config: { thresholdRatio: 0.92, headroomTokens: 512 } },
    { insert: [{ id: "zhiji-stream", name: streamPlugin }] },
  ]);
}

export interface TurnTools {
  /** Absolute paths the read tool may open this turn. */
  readable: Set<string>;
  /** The loopback web MCP server of this turn, when a search source is configured. */
  web?: { url: string; secret: string; fetchPages: boolean };
}

export interface TurnInput {
  launch: HarnessLaunch;
  cwd: string;
  /** Resume this DSH session; absent creates one. */
  nativeId?: string;
  prompt: string;
  tools: TurnTools;
  onEvent(event: AdvisorRuntimeEvent): void;
  /** Called once with the DSH session id, before the prompt is sent. */
  onSession(nativeId: string): Promise<void> | void;
  signal: AbortSignal;
  usageSource: string;
  start?: (launch: HarnessLaunch, handlers: AcpHandlers) => AcpTransport;
}

export interface TurnOutput { text: string; nativeId: string; stopReason: string; usage?: AdvisorUsage; readBytes: number }

function toolName(update: Record<string, unknown>): string {
  return typeof update.title === "string" ? update.title : "";
}

function usageOf(raw: unknown, source: string): AdvisorUsage | undefined {
  const value = record(raw);
  const valid = (n: unknown): n is number => Number.isSafeInteger(n) && Number(n) >= 0;
  if (!valid(value.inputTokens)) return undefined;
  const read = valid(value.cacheReadTokens) ? value.cacheReadTokens : 0, write = valid(value.cacheWriteTokens) ? value.cacheWriteTokens : 0;
  const total = value.inputTokens + read + write;
  if (total <= 0) return undefined;
  return { inputTokens: value.inputTokens, cacheReadTokens: read, cacheWriteTokens: write, totalInputTokens: total,
    ...(valid(value.outputTokens) ? { outputTokens: value.outputTokens } : {}),
    ...(valid(value.reasoningTokens) ? { reasoningTokens: value.reasoningTokens } : {}),
    estimated: false, source, measuredAt: Date.now() };
}

export async function runTurn(input: TurnInput): Promise<TurnOutput> {
  const { tools, onEvent, signal } = input;
  let nativeId = input.nativeId ?? "";
  let failed: AdvisorRuntimeError | undefined;
  let usage: AdvisorUsage | undefined;
  let readBytes = 0, lastStatus = "";
  // Live text per attempt and kind; a committed message publishes only what its stream did not.
  const live = { attempt: "", text: "", reasoning: "" };
  const messages: string[] = [];
  const pendingReads = new Set<string>();
  const status = (event: AdvisorRuntimeEvent) => {
    const signature = JSON.stringify([event.state, event.text]);
    if (signature === lastStatus || signal.aborted) return;
    lastStatus = signature;
    onEvent(event);
  };
  const publish = (kind: "text" | "reasoning", text: string, nativeType: string) => {
    if (!text || signal.aborted) return;
    status({ type: "status", state: kind === "reasoning" ? "thinking" : "answering", nativeType });
    onEvent({ type: kind, text, nativeType });
  };
  let transport!: AcpTransport;
  const fail = (code: AdvisorRuntimeError["code"]) => {
    failed ??= new AdvisorRuntimeError(code);
    if (nativeId) transport.notify("session/cancel", { sessionId: nativeId });
  };
  const allowedTool = (name: string, rawInput: unknown): boolean => {
    if (name === "read") {
      const args = record(rawInput);
      const target = typeof args.file_path === "string" ? args.file_path : typeof args.path === "string" ? args.path : undefined;
      // Arguments stream in; an empty input is checked again once it is complete.
      if (target === undefined) return true;
      return tools.readable.has(path.resolve(input.cwd, target));
    }
    if (/^mcp__web__search$/u.test(name)) return !!tools.web;
    if (/^mcp__web__fetch$/u.test(name)) return !!tools.web?.fetchPages;
    return false;
  };
  const handlers: AcpHandlers = {
    onStream(params) {
      if (params.sessionId !== nativeId) return;
      if (params.kind === "start") { live.attempt = String(params.attemptId ?? ""); live.text = ""; live.reasoning = ""; return; }
      if (params.kind === "usage") { usage = usageOf(params.usage, input.usageSource) ?? usage; return; }
      if ((params.kind === "text" || params.kind === "reasoning") && typeof params.text === "string") {
        live[params.kind] += params.text;
        publish(params.kind, params.text, "assistant-stream");
      }
    },
    onUpdate(sessionId, update) {
      if (sessionId !== nativeId) return;
      const kind = update.sessionUpdate;
      if (kind === "agent_message_chunk" || kind === "agent_thought_chunk") {
        const text = record(update.content).text;
        if (typeof text !== "string") return;
        const field = kind === "agent_message_chunk" ? "text" : "reasoning";
        if (field === "text") messages.push(text);
        // Without the stream plugin (or after a discarded attempt) the committed message is the only source.
        if (text.startsWith(live[field])) publish(field, text.slice(live[field].length), "session/update");
        live[field] = "";
      } else if (kind === "tool_call" || kind === "tool_call_update") {
        const id = String(update.toolCallId ?? "");
        const name = toolName(update);
        if (name && !allowedTool(name, update.rawInput)) { fail("permission-denied"); return; }
        if (name === "read" && update.rawInput !== undefined) pendingReads.add(id);
        const args = record(update.rawInput);
        if (name === "mcp__web__search" && typeof args.query === "string" && args.query.trim()) {
          status({ type: "status", state: "searching", nativeType: String(kind), text: `正在搜索：${args.query.trim().slice(0, 80)}` });
        } else if (name === "mcp__web__fetch" && typeof args.url === "string") {
          let host = "";
          try { host = new URL(args.url).hostname; } catch { /* the tool reports the bad address */ }
          status({ type: "status", state: "searching", nativeType: String(kind), text: host ? `正在打开网页：${host}` : "正在打开网页" });
        }
        if (update.status === "completed" && pendingReads.delete(id)) {
          for (const item of Array.isArray(update.content) ? update.content : []) {
            const text = record(record(item).content).text;
            if (typeof text === "string") readBytes += Buffer.byteLength(text, "utf8");
          }
        }
      }
    },
    onPermission(request) {
      const name = typeof request.toolCall?.title === "string" ? request.toolCall.title : "";
      if (!name || !allowedTool(name, request.toolCall?.rawInput)) return undefined;
      return request.options.find(option => option.kind === "allow_once")?.optionId;
    },
  };
  transport = input.start ? input.start(input.launch, handlers) : acpClient(launchHarness(input.launch), handlers);
  const control = () => AbortSignal.any([signal, AbortSignal.timeout(CONTROL_TIMEOUT_MS)]);
  // DSH settles a cancelled prompt itself; a process that does not is ended so Stop never hangs.
  const cancel = () => {
    if (nativeId) transport.notify("session/cancel", { sessionId: nativeId });
    setTimeout(() => { if (!transport.closed) void transport.close(500); }, 5000).unref();
  };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    try { await transport.request("initialize", { protocolVersion: 1, clientCapabilities: {} }, control()); }
    catch (error) { throw signal.aborted ? error : new AdvisorRuntimeError(error instanceof AcpClosedError ? "engine-start" : "engine-unavailable"); }
    const mcpServers = tools.web ? [{ type: "http", name: "web", url: tools.web.url,
      headers: [{ name: "authorization", value: `Bearer ${tools.web.secret}` }] }] : [];
    if (nativeId) {
      try { await transport.request("session/resume", { sessionId: nativeId, cwd: input.cwd, mcpServers }, control()); }
      catch (error) {
        if (signal.aborted || error instanceof AcpClosedError) throw error;
        throw new AdvisorRuntimeError("native-session-missing");
      }
    } else {
      const created = record(await transport.request("session/new", { cwd: input.cwd, mcpServers }, control()));
      if (typeof created.sessionId !== "string" || !created.sessionId) throw new AdvisorRuntimeError("engine-unavailable");
      nativeId = created.sessionId;
    }
    await input.onSession(nativeId);
    if (signal.aborted) throw new AcpError("aborted");
    const reply = record(await transport.request("session/prompt", { sessionId: nativeId, prompt: [{ type: "text", text: input.prompt }] }));
    if (failed) throw failed;
    const stopReason = typeof reply.stopReason === "string" ? reply.stopReason : "";
    return { text: messages.filter(Boolean).join("\n"), nativeId, stopReason, readBytes, ...(usage ? { usage } : {}) };
  } catch (error) {
    if (failed) throw failed;
    if (error instanceof AdvisorRuntimeError) throw error;
    if (error instanceof AcpClosedError && !signal.aborted) throw new AdvisorRuntimeError("engine-unavailable");
    throw error;
  } finally {
    signal.removeEventListener("abort", cancel);
    if (!transport.closed && nativeId) {
      await transport.request("session/close", { sessionId: nativeId }, AbortSignal.timeout(8000)).catch(() => {});
    }
    await transport.close().catch(() => {});
  }
}
