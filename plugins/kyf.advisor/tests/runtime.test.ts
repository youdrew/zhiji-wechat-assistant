// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The advisor runtime against a scripted ACP transport: session records, budgets, tool auditing, summaries and
// the launch patch. No DeepSeek Harness process, model or user data; dsh-engine.test.ts covers the real engine.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test, type TestContext } from "node:test";
import type { AcpHandlers, AcpTransport } from "../engine/acp";
import { harnessCandidates, harnessEnvironment, harnessLayout, type HarnessLaunch } from "../engine/harness";
import { harnessPatch } from "../engine/harness-run";
import { patchText } from "../engine/harness-state";
import { AdvisorRuntime, AdvisorRuntimeError, advisorInputBudget, advisorOutputBudget, type AdvisorRuntimeEvent, type EngineService } from "../engine/runtime";

const SERVICE: EngineService = { id: "svc", protocol: "chat_completions", model: "synthetic-model", url: "http://127.0.0.1:9/s/svc", contextTokens: 65_536, timeoutMs: 60_000 };
const TOKEN = "exec-token";
const INSTALL = { directory: "X:/dsh", executable: "X:/dsh/DeepSeek Harness.exe", cli: "X:/dsh/resources/app.asar/cli.js" };

interface Script {
  /** What the agent does with one prompt: emit updates through `handlers`, return the stop reason. */
  prompt?: (context: { handlers: AcpHandlers; sessionId: string; text: string; launch: HarnessLaunch; cancelled: () => boolean }) => Promise<string> | string;
  resumeFails?: boolean;
}

function fixture(t: TestContext, script: Script = {}) {
  const calls: Array<{ method: string; params: any }> = [];
  const launches: HarnessLaunch[] = [];
  const patches: string[] = [];
  const summaries: any[] = [];
  let created = 0;
  const start = (launch: HarnessLaunch, handlers: AcpHandlers): AcpTransport => {
    launches.push(launch);
    let closed = false, cancelled = false;
    return {
      get closed() { return closed; },
      diagnostics: () => "",
      notify(method, params) { calls.push({ method, params }); if (method === "session/cancel") cancelled = true; },
      async close() { closed = true; },
      async request(method, params: any) {
        calls.push({ method, params });
        if (method === "initialize") { patches.push(await readFile(launch.args[2], "utf8")); return { protocolVersion: 1 }; }
        if (method === "session/new") return { sessionId: `native-${++created}` };
        if (method === "session/resume") { if (script.resumeFails) throw new Error("unknown session"); return {}; }
        if (method === "session/prompt") {
          const text = params.prompt[0].text as string;
          const stopReason = script.prompt ? await script.prompt({ handlers, sessionId: params.sessionId, text, launch, cancelled: () => cancelled }) :
            (handlers.onUpdate?.(params.sessionId, { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "synthetic answer" } }), "end_turn");
          return { stopReason: cancelled ? "cancelled" : stopReason };
        }
        return {};
      },
    };
  };
  const ready = mkdtemp(path.join(os.tmpdir(), "kyf-runtime-")).then(stateRoot => {
    t.after(() => rm(stateRoot, { recursive: true, force: true }));
    return { stateRoot, runtime: new AdvisorRuntime({ root: stateRoot, stateRoot, engineDir: path.join(stateRoot, "engine-src"), resolve: async () => INSTALL, start,
      generate: async (payload: any) => { summaries.push(payload); return { text: " SUMMARY " }; } }) };
  });
  return { calls, launches, patches, summaries, ready };
}

const request = (extra: Record<string, unknown> = {}) => ({ account: "acct", user: "friend", agentId: "agent", threadId: "thread", sourceId: "source",
  system: "模板提示词", contextFileText: "合成聊天记录", message: "合成问题", ...extra });
const code = (expected: string) => (error: unknown) => error instanceof AdvisorRuntimeError && error.code === expected;

test("a new discussion gets a handle, a per-thread workspace and a persona-bearing launch patch", async t => {
  const f = fixture(t);
  const { runtime, stateRoot } = await f.ready;
  const events: AdvisorRuntimeEvent[] = [];
  const result = await runtime.respond(SERVICE, TOKEN, request({ skills: [{ id: "skill-a", content: "技能正文 {{model}}" }], recoveryText: "此前讨论" }), event => events.push(event));
  assert.match(result.runtimeSessionId, /^dsh_[a-f0-9]{32}$/u);
  assert.equal(result.text, "synthetic answer");
  assert.equal(result.rebuilt, true);
  assert.match(result.userMessageId ?? "", /^msg_/u);
  assert.deepEqual(events.slice(0, 2).map(event => event.state), ["native-session-created", "loaded"]);
  const launch = f.launches[0];
  assert.deepEqual(launch.args.slice(0, 2), ["acp", "--patch"]);
  assert.equal(launch.env.DSH_HOME, path.join(stateRoot, "dsh", "home"));
  assert.equal(launch.env.DSH_TELEMETRY_MODE, "OFF");
  assert.ok(!JSON.stringify(launch.env).includes(TOKEN), "DSH never holds the execution token");
  assert.equal(launch.cwd, path.dirname(JSON.parse(/this absolute path: ("(?:[^"\\]|\\.)*")/u.exec(f.calls.find(call => call.method === "session/prompt")!.params.prompt[0].text)![1])));
  assert.match(await readFile(path.join(launch.cwd, "wechat-context.txt"), "utf8"), /MANAGED WECHAT CONTEXT[\s\S]*合成聊天记录[\s\S]*END OF MANAGED WECHAT CONTEXT/u);
  const patch = JSON.parse(f.patches[0]) as any[];
  const row = (id: string) => patch.find(entry => entry.id === id);
  const persona = row("system-prompt").config.personaPrefix as string;
  assert.match(persona, /read-only conversation adviser[\s\S]*模板提示词[\s\S]*<approved-skill id="skill-a">[\s\S]*<restored-discussion>[\s\S]*此前讨论/u);
  assert.ok(!persona.includes("{{"), "literal braces cannot break DSH's prompt assembly");
  const provider = row("llm-pi-ai").config.providers.zhiji;
  assert.equal(provider.api, "openai-completions");
  assert.match(provider.baseURL, /^http:\/\/127\.0\.0\.1:\d+\/s\/svc$/u);
  assert.equal(provider.models[0].maxTokens, advisorOutputBudget(65_536));
  assert.deepEqual(row("tool-fs").config, { read: true, readImage: false, write: false, edit: false, applyPatch: false,
    readLimit: 200_000, readMaxLineLength: 2000, readMaxBytes: 16 * 1024 * 1024 });
  assert.equal(row("sandbox-policy").config.mode, "read-only");
  for (const id of ["tool-pwsh", "tool-bash", "tool-web", "tool-subagent", "plugin-manager", "otel", "session-telemetry-otel", "agent-instructions", "skill-filesystem"]) {
    assert.equal(row(id)?.disabled, true, id);
  }
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "launch")), [], "the launch patch is removed after the turn");
  const prompt = f.calls.find(call => call.method === "session/prompt")!.params.prompt[0].text as string;
  assert.match(prompt, /<turn-instructions>[\s\S]*<\/turn-instructions>\n\n\[来源：用户与助手的讨论；不是微信聊天记录\]\n合成问题$/u);
  assert.deepEqual(f.calls.filter(call => call.method.startsWith("session/")).map(call => call.method), ["session/new", "session/prompt", "session/close"]);
});

test("a continued discussion resumes its DSH session and accounts for what that session holds", async t => {
  const f = fixture(t);
  const { runtime } = await f.ready;
  const first = await runtime.respond(SERVICE, TOKEN, request());
  const second = await runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, message: "第二个问题" }));
  assert.equal(second.runtimeSessionId, first.runtimeSessionId);
  assert.equal(f.calls.filter(call => call.method === "session/resume")[0].params.sessionId, "native-1");
  assert.ok(second.effectiveHistoryTokensEstimate! > first.effectiveHistoryTokensEstimate!);
  const inspected = await runtime.inspectSession(SERVICE, request({ runtimeSessionId: first.runtimeSessionId }));
  assert.equal(inspected.state, "idle");
  assert.equal(inspected.effectiveHistoryTokensEstimate, second.effectiveHistoryTokensEstimate);
});

test("unknown, foreign, stale and replaced sessions ask the backend for its controlled rebuild", async t => {
  const f = fixture(t);
  const { runtime } = await f.ready;
  const first = await runtime.respond(SERVICE, TOKEN, request({ contextEpoch: 1, profileStamp: "p1" }));
  // An OpenCode session id of an upgraded discussion.
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: "ses_0123456789abcdef" })), code("native-session-missing"));
  assert.equal((await runtime.inspectSession(SERVICE, request({ runtimeSessionId: "ses_0123456789abcdef" }))).state, "missing");
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, threadId: "other" })), code("scope-mismatch"));
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, contextEpoch: 2, profileStamp: "p1" })), code("native-session-incompatible"));
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, contextEpoch: 1, profileStamp: "p1",
    revertMessageId: first.userMessageId })), code("native-session-incompatible"));
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ revertMessageId: "msg_1" })), code("invalid-request"));
});

test("a DSH session that cannot be resumed is reported missing", async t => {
  const script: Script = {};
  const f = fixture(t, script);
  const { runtime } = await f.ready;
  const first = await runtime.respond(SERVICE, TOKEN, request());
  script.resumeFails = true;
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId })), code("native-session-missing"));
});

test("live stream text is published once, and committed text fills in what the stream did not carry", async t => {
  const f = fixture(t, { prompt: ({ handlers, sessionId }) => {
    handlers.onStream?.({ sessionId, attemptId: "a1", kind: "start" });
    handlers.onStream?.({ sessionId, attemptId: "a1", kind: "reasoning", text: "想" });
    handlers.onStream?.({ sessionId, attemptId: "a1", kind: "text", text: "第一" });
    handlers.onStream?.({ sessionId: "another-session", attemptId: "x", kind: "text", text: "无关" });
    handlers.onStream?.({ sessionId, attemptId: "a1", kind: "usage", usage: { inputTokens: 90, outputTokens: 7, cacheReadTokens: 10 } });
    handlers.onUpdate?.(sessionId, { sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "想" } });
    handlers.onUpdate?.(sessionId, { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "第一段" } });
    // A later step whose stream never arrived.
    handlers.onUpdate?.(sessionId, { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "第二段" } });
    return "end_turn";
  } });
  const { runtime } = await f.ready;
  const events: AdvisorRuntimeEvent[] = [];
  const result = await runtime.respond(SERVICE, TOKEN, request(), event => events.push(event));
  assert.equal(result.text, "第一段\n第二段");
  assert.deepEqual(events.filter(event => event.type === "text").map(event => event.text), ["第一", "段", "第二段"]);
  assert.deepEqual(events.filter(event => event.type === "reasoning").map(event => event.text), ["想"]);
  assert.deepEqual(events.filter(event => event.type === "status").map(event => event.state), ["native-session-created", "thinking", "answering"]);
  assert.deepEqual({ ...result.usage, measuredAt: 0 }, { inputTokens: 90, cacheReadTokens: 10, cacheWriteTokens: 0, totalInputTokens: 100, outputTokens: 7,
    estimated: false, source: "deepseek-harness-0.2.2-rc.1:chat_completions", measuredAt: 0 });
});

test("only this turn's files may be read; anything else ends the turn and marks the session for rebuild", async t => {
  const outside = path.join(os.tmpdir(), "outside.txt");
  let target = "";
  const script: Script = { prompt: ({ handlers, sessionId, text, cancelled }) => {
    const listed = JSON.parse(/this absolute path: ("(?:[^"\\]|\\.)*")/u.exec(text)![1]);
    handlers.onUpdate?.(sessionId, { sessionUpdate: "tool_call", toolCallId: "c1", title: "read", status: "in_progress", rawInput: { file_path: target || listed } });
    if (cancelled()) return "cancelled";
    handlers.onUpdate?.(sessionId, { sessionUpdate: "tool_call_update", toolCallId: "c1", status: "completed", content: [{ type: "content", content: { type: "text", text: "x".repeat(1000) } }] });
    handlers.onUpdate?.(sessionId, { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "ok" } });
    return "end_turn";
  } };
  const f = fixture(t, script);
  const { runtime } = await f.ready;
  const first = await runtime.respond(SERVICE, TOKEN, request());
  assert.ok(first.effectiveHistoryTokensEstimate! >= 1000, "what the read returned stays in the DSH session and is counted");
  target = outside;
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId })), code("permission-denied"));
  assert.ok(f.calls.some(call => call.method === "session/cancel"));
  target = "";
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId })), code("native-session-incompatible"));
});

test("other tools are refused, web tools only with a search source, and permission prompts follow the same rule", async t => {
  const answers: Array<string | undefined> = [];
  const script: Script = { prompt: ({ handlers, sessionId }) => {
    const options = [{ optionId: "allow", kind: "allow_once" }, { optionId: "reject", kind: "reject_once" }];
    answers.push(handlers.onPermission?.({ sessionId, toolCall: { title: "mcp__web__search", rawInput: { query: "天气" } }, options }));
    answers.push(handlers.onPermission?.({ sessionId, toolCall: { title: "mcp__web__fetch", rawInput: { url: "https://example.com" } }, options }));
    answers.push(handlers.onPermission?.({ sessionId, toolCall: { title: "pwsh", rawInput: { command: "dir" } }, options }));
    handlers.onUpdate?.(sessionId, { sessionUpdate: "tool_call", toolCallId: "w", title: "mcp__web__search", status: "in_progress", rawInput: { query: "天气" } });
    handlers.onUpdate?.(sessionId, { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "ok" } });
    return "end_turn";
  } };
  const f = fixture(t, script);
  const { runtime } = await f.ready;
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request()), code("permission-denied"));
  assert.deepEqual(answers, [undefined, undefined, undefined]);
  answers.length = 0;
  const events: AdvisorRuntimeEvent[] = [];
  await runtime.respond(SERVICE, TOKEN, request({ threadId: "web", webSearch: { provider: "searxng", url: "http://127.0.0.1:9", fetchPages: false } }), event => events.push(event));
  assert.deepEqual(answers, ["allow", undefined, undefined]);
  assert.ok(events.some(event => event.state === "searching" && event.text === "正在搜索：天气"));
  const created = f.calls.filter(call => call.method === "session/new").at(-1)!.params;
  assert.equal(created.mcpServers[0].name, "web");
  assert.match(created.mcpServers[0].headers[0].value, /^Bearer [a-f0-9]{48}$/u);
  assert.match(JSON.parse(f.patches.at(-1)!).find((entry: any) => entry.id === "system-prompt").config.personaPrefix, /web search tool/u);
});

test("stop cancels the DSH prompt, removes a session created for it, and leaves an accepted one for rebuild", async t => {
  const controller = new AbortController();
  const script: Script = { prompt: async ({ handlers, sessionId, cancelled }) => {
    handlers.onStream?.({ sessionId, attemptId: "a", kind: "text", text: "partial" });
    controller.abort();
    for (let index = 0; index < 50 && !cancelled(); index++) await new Promise(resolve => setTimeout(resolve, 5));
    return "cancelled";
  } };
  const f = fixture(t, script);
  const { runtime, stateRoot } = await f.ready;
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request(), () => {}, controller.signal), code("cancelled"));
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "sessions")), []);
  await assert.rejects(runtime.respond(SERVICE, TOKEN, request(), () => {}, controller.signal), code("cancelled"));
  assert.equal(f.launches.length, 1, "an already stopped request starts nothing");
});

test("oversized input fails before DSH starts and a long history asks for compaction", async t => {
  const f = fixture(t);
  const { runtime } = await f.ready;
  const small = { ...SERVICE, contextTokens: 8192 };
  await assert.rejects(runtime.respond(small, TOKEN, request({ contextFileText: "长".repeat(4000) })), code("context-too-long"));
  assert.equal(f.launches.length, 0);
  const first = await runtime.respond(small, TOKEN, request({ contextFileText: "短" }));
  const budget = advisorInputBudget(8192);
  await assert.rejects(runtime.respond(small, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, contextFileText: "字".repeat(Math.floor(budget / 3) - 300) })), code("history-too-long"));
});

test("manual compaction summarizes the saved turns once, retires the DSH session and continues from the summary", async t => {
  const f = fixture(t);
  const { runtime } = await f.ready;
  assert.equal((await runtime.compactSession(SERVICE, TOKEN, request({ operationId: "op" }))).state, "noop");
  const first = await runtime.respond(SERVICE, TOKEN, request({ recoveryText: "更早的摘要" }));
  const handle = first.runtimeSessionId;
  const compacted = await runtime.compactSession(SERVICE, TOKEN, request({ runtimeSessionId: handle, operationId: "op-1", contextVersion: "v1" }));
  assert.equal(compacted.state, "completed");
  assert.equal(compacted.summary, "SUMMARY");
  assert.deepEqual(compacted.compactions?.map(item => [item.operationId, item.kind, item.state, item.contextVersion]), [["op-1", "manual", "completed", "v1"]]);
  assert.equal(f.summaries[0].step, "discussion-compaction");
  assert.equal(f.summaries[0].token, TOKEN);
  assert.equal(f.summaries[0].request.thinking, false);
  assert.match(f.summaries[0].request.prompt, /<earlier-summary>\n更早的摘要[\s\S]*\[user\]\n合成问题\n\[adviser\]\nsynthetic answer/u);
  assert.equal((await runtime.compactSession(SERVICE, TOKEN, request({ runtimeSessionId: handle, operationId: "op-1" }))).state, "completed");
  assert.equal(f.summaries.length, 1, "a repeated operation is replayed, not run again");
  await runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: handle }));
  assert.equal(f.calls.filter(call => call.method === "session/new").length, 2);
  assert.match(JSON.parse(f.patches.at(-1)!).find((entry: any) => entry.id === "system-prompt").config.personaPrefix, /<restored-discussion>[\s\S]*SUMMARY/u);
  assert.equal((await runtime.stopSessionOperation(SERVICE, request({ runtimeSessionId: handle, operationId: "op-1" }))).state, "completed");
  await assert.rejects(runtime.stopSessionOperation(SERVICE, request({ runtimeSessionId: handle, operationId: "unknown" })), code("invalid-request"));
});

test("a failed or stopped compaction keeps the discussion as it was", async t => {
  const f = fixture(t);
  const { stateRoot } = await f.ready;
  let mode: "fail" | "stop" = "fail";
  const controller = new AbortController();
  const runtime = new AdvisorRuntime({ root: stateRoot, stateRoot, engineDir: stateRoot, resolve: async () => INSTALL,
    start: (await f.ready).runtime["starter" as keyof AdvisorRuntime] as any,
    generate: async () => { if (mode === "stop") controller.abort(); throw Object.assign(new Error("x"), { status: 500 }); } });
  const first = await runtime.respond(SERVICE, TOKEN, request());
  const failed = await runtime.compactSession(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, operationId: "op-f" }));
  assert.equal(failed.state, "failed");
  mode = "stop";
  const stopped = await runtime.compactSession(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId, operationId: "op-s" }), () => {}, controller.signal);
  assert.equal(stopped.state, "stopped");
  assert.equal((await runtime.inspectSession(SERVICE, request({ runtimeSessionId: first.runtimeSessionId, operationId: "op-s" }))).state, "stopped");
  const next = await runtime.respond(SERVICE, TOKEN, request({ runtimeSessionId: first.runtimeSessionId }));
  assert.equal(f.calls.filter(call => call.method === "session/resume").length, 1, "the same DSH session continues");
  assert.equal(next.compactions?.length, 0);
});

test("record and discussion summaries are single gateway requests with their own step labels", async t => {
  const f = fixture(t);
  const { runtime } = await f.ready;
  assert.equal((await runtime.compact(SERVICE, TOKEN, request({ text: "聊天证据", targetSummaryChars: 300 }))).text, "SUMMARY");
  assert.equal((await runtime.summarizeDiscussion(SERVICE, TOKEN, request({ text: "讨论记录" }))).text, "SUMMARY");
  assert.deepEqual(f.summaries.map(item => item.step), ["wechat-summary", "discussion-summary"]);
  assert.match(f.summaries[0].request.prompt, /about 300 Unicode characters[\s\S]*聊天证据/u);
  assert.equal(f.launches.length, 0, "no agent is started for a summary");
  await assert.rejects(runtime.compact({ ...SERVICE, contextTokens: 8192 }, TOKEN, request({ text: "长".repeat(5000) })), code("context-too-long"));
  await assert.rejects(runtime.compact(SERVICE, TOKEN, request({ text: "x", budget: 1 })), code("invalid-request"));
});

test("deleting a discussion removes its record, its workspace and DSH's own session files; the old engine's state is retired", async t => {
  const f = fixture(t);
  const { runtime, stateRoot } = await f.ready;
  const legacy = path.join(stateRoot, "workspace", "contexts");
  for (const folder of [legacy, path.join(stateRoot, "data", "opencode"), path.join(stateRoot, "home", "AppData"), path.join(stateRoot, "unrelated")]) {
    await mkdir(folder, { recursive: true });
  }
  await writeFile(path.join(legacy, "wechat-context.txt"), "old copy");
  const first = await runtime.respond(SERVICE, TOKEN, request());
  assert.deepEqual((await readdir(stateRoot)).sort(), ["dsh", "unrelated"], "OpenCode's state is removed on first use, nothing else");
  const native = path.join(stateRoot, "dsh", "home", "sessions", "--slug--", "native-1");
  await mkdir(native, { recursive: true });
  await writeFile(path.join(native, "log.jsonl"), "{}");
  await runtime.deleteSession(SERVICE, request({ runtimeSessionId: first.runtimeSessionId }));
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "sessions")), []);
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "home", "sessions", "--slug--")), []);
  assert.deepEqual(await readdir(path.join(stateRoot, "dsh", "workspace", "threads")), []);
  assert.equal((await runtime.deleteSession(SERVICE, request({ runtimeSessionId: first.runtimeSessionId }))).state, "completed");
});

test("protocols DSH cannot reach are refused with their own message", async t => {
  const f = fixture(t);
  const { runtime } = await f.ready;
  await assert.rejects(runtime.respond({ ...SERVICE, protocol: "gemini" }, TOKEN, request()), code("protocol-unsupported"));
  for (const [protocol, api] of [["anthropic", "anthropic-messages"], ["responses", "openai-responses"], ["ollama", "openai-completions"]] as const) {
    const patch = JSON.parse(harnessPatch({ protocol, model: "m", baseUrl: "http://127.0.0.1:1/s/x", contextTokens: 1000, outputTokens: 100, persona: "p", workspaceRoot: "w", engineDir: "e" }));
    assert.equal(patch[0].config.providers.zhiji.api, api);
  }
});

test("the launch patch is JSON that YAML reads as written", () => {
  const text = patchText([{ id: "x", config: { text: "a\u2028b\u0085c\u007fd\ufeff中文😀\"quoted\"" } }]);
  for (const forbidden of ["\u2028", "\u0085", "\u007f", "\ufeff"]) assert.ok(!text.includes(forbidden));
  assert.equal(JSON.parse(text)[0].config.text, "a\u2028b\u0085c\u007fd\ufeff中文😀\"quoted\"");
});

test("DSH is looked for in the override, next to the app, then the user's installation; its environment is isolated", () => {
  const env = { KYF_DSH_INSTALL: "D:\\custom", LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local", ProgramFiles: "C:\\Program Files" };
  const folders = harnessCandidates("D:\\app", env, "win32");
  assert.deepEqual(folders.slice(0, 4).map(folder => folder.replace(/\\/gu, "/")), ["D:/custom", "D:/app/runtime/dsh",
    "C:/Users/u/AppData/Local/Programs/DeepSeek Harness", "C:/Program Files/DeepSeek Harness"]);
  const mac = harnessLayout("/Applications/DeepSeek Harness.app", "darwin");
  assert.match(mac.executable.replace(/\\/gu, "/"), /Contents\/MacOS\/DeepSeek Harness$/u);
  assert.match(mac.cli.replace(/\\/gu, "/"), /Contents\/Resources\/app\.asar\/dsh\/node_modules\/@deepseek-ai\/dsh-desktop-host\/lib\/cli\.js$/u);
  const child = harnessEnvironment("H", "A", { ZHIJI_RELAY_SECRET: "s" }, { SystemRoot: "C:\\Windows", OPENAI_API_KEY: "secret", HTTPS_PROXY: "p", PATH: "x" });
  assert.equal(child.OPENAI_API_KEY, undefined);
  assert.equal(child.HTTPS_PROXY, undefined);
  assert.equal(child.DSH_HOME, "H");
  assert.equal(child.DSH_AGENTS_HOME, "A");
  assert.equal(child.ELECTRON_RUN_AS_NODE, "1");
  assert.equal(child.ZHIJI_RELAY_SECRET, "s");
});
