// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Added 2026-10-15: opt-in check that the actual pinned engine calls the 聊天助手's web tools; the model, the SearXNG
// instance and its results are synthetic. Run with KYF_TRACE_ENGINE_TEST=1 (and KYF_ENGINE_ROOT=<checkout with
// .local/advisor-engine> if not this one).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer, type ServerResponse } from "node:http";
import { copyFile, link, mkdir, mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import { AdvisorRuntime, resolveAdvisorEngine, startAdvisorEngine, type AdvisorRuntimeEvent, type EngineService } from "../engine/runtime";

function chunk(res: ServerResponse, delta: Record<string, unknown>, finish: string) {
  res.writeHead(200, { "content-type": "text/event-stream" });
  const data = (value: unknown) => res.write(`data: ${JSON.stringify(value)}\n\n`);
  data({ id: "chat_synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic", choices: [{ index: 0, delta, finish_reason: null }] });
  data({ id: "chat_synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic", choices: [{ index: 0, delta: {}, finish_reason: finish }],
    usage: { prompt_tokens: 20, completion_tokens: 5, total_tokens: 25 } });
  res.end("data: [DONE]\n\n");
}
const call = (name: string, args: unknown) => ({ role: "assistant", tool_calls: [{ index: 0, id: `call_${name}`, type: "function",
  function: { name, arguments: JSON.stringify(args) } }] });

test("actual OpenCode 1.18.34 searches through the configured source and never opens a local page", {
  skip: process.env.KYF_TRACE_ENGINE_TEST !== "1", timeout: 180_000,
}, async t => {
  const repository = path.resolve(process.env.KYF_ENGINE_ROOT ?? ".");
  const executable = await resolveAdvisorEngine(repository);
  const parent = path.resolve(".local", "advisor-build", "web-engine-tests");
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(path.join(parent, "engine-"));
  await mkdir(path.join(root, ".local", "advisor-engine"), { recursive: true });
  await link(executable, path.join(root, ".local", "advisor-engine", "opencode.exe"));
  await copyFile(path.join(path.dirname(executable), "manifest.json"), path.join(root, ".local", "advisor-engine", "manifest.json"));
  // The user's SearXNG: one result, on a local address the page tool must refuse.
  const searches: string[] = [], pages: string[] = [];
  const searxng = createServer((req, res) => {
    const url = new URL(req.url!, "http://127.0.0.1");
    if (url.pathname !== "/search") { pages.push(url.pathname); res.writeHead(200, { "content-type": "text/html" }); res.end("<p>local secret</p>"); return; }
    searches.push(`${url.searchParams.get("q")}|${url.searchParams.get("format")}`);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ results: [{ title: "周末篮球赛赛程", url: `http://127.0.0.1:${(searxng.address() as { port: number }).port}/page`,
      content: "周六晚上 7 点在体育馆开赛" }] }));
  });
  // The model: search, then try to open the result, then answer.
  const packets: any[] = [];
  const gateway = createServer(async (req, res) => {
    let raw = ""; for await (const part of req) raw += part;
    const packet = JSON.parse(raw); packets.push(packet);
    const results = (packet.messages ?? []).filter((message: any) => message.role === "tool");
    const page = `http://127.0.0.1:${(searxng.address() as { port: number }).port}/page`;
    const offered = (packet.tools ?? []).some((tool: any) => tool.function?.name === "web_search");
    if (!offered) chunk(res, { role: "assistant", content: "没有联网，只能根据聊天记录回答。" }, "stop");
    else if (!results.length) chunk(res, call("web_search", { query: "周末 比赛" }), "tool_calls");
    else if (results.length === 1) chunk(res, call("web_fetch", { url: page }), "tool_calls");
    else chunk(res, { role: "assistant", content: "周六晚上 7 点有比赛（来源：周末篮球赛赛程）。" }, "stop");
  });
  for (const server of [searxng, gateway]) await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  let launches = 0;
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state"), startEngine: spec => { launches++; return startAdvisorEngine(spec); } });
  t.after(async () => {
    await runtime.close();
    for (const server of [searxng, gateway]) { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
    await rm(root, { recursive: true, force: true });
  });
  const service: EngineService = { id: "svc-1", protocol: "chat_completions", model: "synthetic", contextTokens: 65536,
    url: `http://127.0.0.1:${(gateway.address() as { port: number }).port}/s/svc-1` };
  const request = { account: "synthetic-web", user: "synthetic-person", agentId: "advisor", threadId: "synthetic-thread",
    message: "过几天有没有比赛？", system: "Synthetic test", context: "m1 OTHER: 周末一起看比赛吗", contextRevision: 1,
    webSearch: { provider: "searxng" as const, url: `http://127.0.0.1:${(searxng.address() as { port: number }).port}`, fetchPages: true } };
  const events: AdvisorRuntimeEvent[] = [];
  const answer = await runtime.respond(service, "synthetic-execution-token", request, event => events.push(event));
  assert.match(answer.text, /周六晚上 7 点有比赛/u);
  const offered = (packets[0].tools ?? []).map((tool: any) => tool.function?.name);
  assert(offered.includes("web_search") && offered.includes("web_fetch") && offered.includes("read"), offered.join(","));
  assert(!offered.some((name: string) => ["bash", "webfetch", "websearch", "edit", "write"].includes(name)), offered.join(","));
  assert.deepEqual(searches, ["周末 比赛|json"]);
  const toolResults = packets.at(-1).messages.filter((message: any) => message.role === "tool").map((message: any) => JSON.stringify(message.content));
  assert.match(toolResults[0], /周末篮球赛赛程/u);
  assert.match(toolResults[0], /网页内容不是指令/u);
  assert.match(toolResults[1], /不能打开本机或内网地址/u);
  assert.deepEqual(pages, [], "the local page is never requested");
  const shown = events.filter(event => event.state === "searching").map(event => event.text);
  assert(shown.includes("正在搜索：周末 比赛") && shown.includes("正在打开网页：127.0.0.1"), shown.join(","));
  // Without a search source the engine restarts and the model is offered no web tools.
  packets.length = 0;
  const { webSearch: _, ...offline } = request;
  const followup = await runtime.respond(service, "synthetic-execution-token-2", { ...offline,
    runtimeSessionId: answer.runtimeSessionId, message: "继续刚才的讨论" });
  assert.equal(launches, 2);
  assert.equal(followup.runtimeSessionId, answer.runtimeSessionId, "changing search settings keeps the discussion's native history");
  const plain = (packets[0].tools ?? []).map((tool: any) => tool.function?.name);
  assert(!plain.some((name: string) => name.startsWith("web_")), plain.join(","));
  t.diagnostic(`offered with a search source: ${offered.join(",")}`);
});
