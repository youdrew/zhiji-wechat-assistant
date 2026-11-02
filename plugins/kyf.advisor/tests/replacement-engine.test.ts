// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Opt-in regression using the pinned native engine and a synthetic local model, with no user data.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import { copyFile, link, mkdir, mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import { AdvisorRuntime, resolveAdvisorEngine, type EngineService } from "../engine/runtime";

test("actual engine replaces the latest turn repeatedly while keeping earlier discussion", {
  skip: process.env.KYF_TRACE_ENGINE_TEST !== "1", timeout: 180_000,
}, async t => {
  const executable = await resolveAdvisorEngine(path.resolve(process.env.KYF_ENGINE_ROOT ?? "."));
  const parent = path.resolve(".local/advisor-build/replacement-engine-tests");
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(path.join(parent, "engine-"));
  await mkdir(path.join(root, ".local/advisor-engine"), { recursive: true });
  const nativeCopy = path.join(root, ".local/advisor-engine/opencode.exe");
  try { await link(executable, nativeCopy); } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EXDEV") throw error;
    await copyFile(executable, nativeCopy);
  }
  await copyFile(path.join(path.dirname(executable), "manifest.json"), path.join(root, ".local/advisor-engine/manifest.json"));
  const packets: string[] = [];
  const gateway = createServer(async (req, res) => {
    let raw = ""; for await (const part of req) raw += part;
    packets.push(raw);
    res.writeHead(200, { "content-type": "text/event-stream" });
    const data = (value: unknown) => res.write(`data: ${JSON.stringify(value)}\n\n`);
    const base = { id: "chat_synthetic", object: "chat.completion.chunk", created: 1, model: "synthetic" };
    data({ ...base, choices: [{ index: 0, delta: { role: "assistant", content: `SYNTHETIC_ANSWER_${packets.length}` }, finish_reason: null }] });
    data({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      usage: { prompt_tokens: 30, completion_tokens: 5, total_tokens: 35 } });
    res.end("data: [DONE]\n\n");
  });
  await new Promise<void>(resolve => gateway.listen(0, "127.0.0.1", resolve));
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state") });
  t.after(async () => {
    await runtime.close();
    gateway.closeAllConnections();
    await new Promise<void>(resolve => gateway.close(() => resolve()));
    await rm(root, { recursive: true, force: true });
  });
  const service: EngineService = { id: "synthetic", protocol: "chat_completions", model: "synthetic", contextTokens: 65536,
    url: `http://127.0.0.1:${(gateway.address() as { port: number }).port}/s/synthetic` };
  const request = { account: "synthetic-account", user: "synthetic-friend", agentId: "advisor", threadId: "synthetic-thread",
    message: "SYNTHETIC_INITIAL_QUESTION", system: "Synthetic test", context: "m1 OTHER: synthetic record", contextRevision: 1 };
  const first = await runtime.respond(service, "synthetic-token-1", request);
  assert.equal(first.text, "SYNTHETIC_ANSWER_1");
  assert.match(first.userMessageId!, /^msg_/);
  const second = await runtime.respond(service, "synthetic-token-2", { ...request,
    runtimeSessionId: first.runtimeSessionId, message: "SYNTHETIC_SECOND_QUESTION" });
  const regenerated = await runtime.respond(service, "synthetic-token-3", { ...request,
    runtimeSessionId: second.runtimeSessionId, message: "SYNTHETIC_SECOND_QUESTION", revertMessageId: second.userMessageId });
  assert.equal(regenerated.text, "SYNTHETIC_ANSWER_3");
  assert.equal(regenerated.runtimeSessionId, first.runtimeSessionId);
  assert.match(packets[2], /SYNTHETIC_ANSWER_1/);
  assert.doesNotMatch(packets[2], /SYNTHETIC_ANSWER_2/);
  const edited = await runtime.respond(service, "synthetic-token-4", { ...request,
    runtimeSessionId: regenerated.runtimeSessionId, message: "SYNTHETIC_EDITED_QUESTION", revertMessageId: regenerated.userMessageId });
  assert.equal(edited.text, "SYNTHETIC_ANSWER_4");
  assert.match(packets[3], /SYNTHETIC_INITIAL_QUESTION/);
  assert.match(packets[3], /SYNTHETIC_EDITED_QUESTION/);
  assert.doesNotMatch(packets[3], /SYNTHETIC_SECOND_QUESTION|SYNTHETIC_ANSWER_[23]/);
  await runtime.respond(service, "synthetic-token-5", { ...request,
    runtimeSessionId: edited.runtimeSessionId, message: "SYNTHETIC_FOLLOWUP" });
  assert.match(packets[4], /SYNTHETIC_ANSWER_1/);
  assert.match(packets[4], /SYNTHETIC_ANSWER_4/);
  assert.doesNotMatch(packets[4], /SYNTHETIC_SECOND_QUESTION|SYNTHETIC_ANSWER_[23]/);
  assert.equal(packets.length, 5);
});
