// Copyright 2026 Know Your Friends contributors. Apache-2.0.
import assert from "node:assert/strict";
import { test } from "node:test";
import { UsageMeter } from "../electron/model-gateway-meter";
import { normalizedUsage } from "../electron/model-traces";

function metered(protocol: string, payload: unknown) {
  const meter = new UsageMeter("application/json", protocol);
  meter.push(new TextEncoder().encode(JSON.stringify(payload)));
  return meter.finish();
}

function totals(usage: ReturnType<typeof normalizedUsage>) {
  assert.ok(usage);
  return { input: usage.totalInputTokens, output: usage.totalOutputTokens,
    total: usage.totalTokens, source: usage.totalTokensSource };
}

test("OpenAI totals already contain cached and reasoning token subsets", () => {
  for (const protocol of ["chat_completions", "responses"]) {
    const raw = protocol === "responses" ? { input_tokens: 100, output_tokens: 20, total_tokens: 120,
      input_tokens_details: { cached_tokens: 80, cache_write_tokens: 10 }, output_tokens_details: { reasoning_tokens: 15 } }
      : { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120,
        prompt_tokens_details: { cached_tokens: 80, cache_write_tokens: 10 }, completion_tokens_details: { reasoning_tokens: 15 } };
    const usage = metered(protocol, { usage: raw });
    assert.equal(usage?.accountingVersion, 1);
    assert.equal(usage?.inputTokens, 100);
    assert.equal(usage?.outputTokens, 20);
    assert.equal(usage?.cacheReadTokens, 80);
    assert.equal(usage?.cacheWriteTokens, 10);
    assert.equal(usage?.reasoningTokens, 15);
    assert.deepEqual(totals(normalizedUsage(raw, protocol)), { input: 100, output: 20, total: 120, source: "provider" });
  }
});

test("provider total is kept independently from the input/output split", () => {
  assert.deepEqual(totals(normalizedUsage({ prompt_tokens: 10, completion_tokens: 4, total_tokens: 17 }, "chat_completions")),
    { input: 10, output: 4, total: 17, source: "provider" });
  assert.deepEqual(totals(normalizedUsage({ total_tokens: 17 }, "responses")),
    { input: null, output: null, total: 17, source: "provider" });
});

test("OpenAI and Ollama derive a total only when both counters are known", () => {
  assert.deepEqual(totals(normalizedUsage({ prompt_tokens: 10, completion_tokens: 4 }, "chat_completions")),
    { input: 10, output: 4, total: 14, source: "derived" });
  const usage = metered("ollama", { prompt_eval_count: 10, eval_count: 4 });
  assert.equal(usage?.totalTokens, 14);
  assert.equal(usage?.totalTokensSource, "derived");
  assert.deepEqual(totals(normalizedUsage({ prompt_eval_count: 10 }, "ollama")),
    { input: 10, output: null, total: null, source: null });
});

test("Anthropic adds cache input counters and keeps inclusive output", () => {
  const usage = metered("anthropic", { usage: { input_tokens: 10, output_tokens: 7,
    cache_read_input_tokens: 20, cache_creation_input_tokens: 3, output_tokens_details: { thinking_tokens: 5 } } });
  assert.equal(usage?.inputTokens, 10);
  assert.equal(usage?.reasoningTokens, 5);
  assert.equal(usage?.totalInputTokens, 33);
  assert.equal(usage?.totalOutputTokens, 7);
  assert.equal(usage?.totalTokens, 40);
  assert.equal(usage?.totalTokensSource, "derived");
  for (const absent of [undefined, null]) {
    assert.deepEqual(totals(normalizedUsage({ input_tokens: 10, output_tokens: 7,
      cache_read_input_tokens: absent, cache_creation_input_tokens: 0 }, "anthropic")),
    { input: null, output: 7, total: null, source: null });
  }
  assert.deepEqual(totals(normalizedUsage({ input_tokens: 0, output_tokens: 0,
    cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }, "anthropic")),
  { input: 0, output: 0, total: 0, source: "derived" });
});

test("Gemini counts tool-use input and thinking output while retaining provider total", () => {
  const raw = { promptTokenCount: 100, cachedContentTokenCount: 80, candidatesTokenCount: 20,
    toolUsePromptTokenCount: 7, thoughtsTokenCount: 15, totalTokenCount: 144 };
  const usage = metered("gemini", { usageMetadata: raw });
  assert.equal(usage?.inputTokens, 100);
  assert.equal(usage?.outputTokens, 20);
  assert.equal(usage?.toolUsePromptTokens, 7);
  assert.deepEqual(totals(normalizedUsage(raw, "gemini")), { input: 107, output: 35, total: 144, source: "provider" });
  assert.deepEqual(totals(normalizedUsage({ ...raw, totalTokenCount: undefined }, "gemini")),
    { input: 107, output: 35, total: 142, source: "derived" });
});

test("Gemini missing thoughts or tool counters remain unknown without losing provider total", () => {
  for (const absent of [undefined, null]) {
    assert.deepEqual(totals(normalizedUsage({ promptTokenCount: 10, candidatesTokenCount: 4,
      thoughtsTokenCount: absent, toolUsePromptTokenCount: 0, totalTokenCount: 17 }, "gemini")),
    { input: 10, output: null, total: 17, source: "provider" });
    assert.deepEqual(totals(normalizedUsage({ promptTokenCount: 10, candidatesTokenCount: 4,
      thoughtsTokenCount: 3, toolUsePromptTokenCount: absent, totalTokenCount: 17 }, "gemini")),
    { input: null, output: 7, total: 17, source: "provider" });
  }
});

test("unrecognized protocols keep only explicit provider totals", () => {
  for (const raw of [{ prompt_tokens: 10, completion_tokens: 4, total_tokens: 14 },
    { promptTokenCount: 10, candidatesTokenCount: 4, totalTokenCount: 14 }]) {
    assert.deepEqual(totals(normalizedUsage(raw, "custom")), { input: null, output: null, total: 14, source: "provider" });
  }
  assert.deepEqual(totals(normalizedUsage({ input_tokens: 10, output_tokens: 4 }, "custom")),
    { input: null, output: null, total: null, source: null });
});

test("negative, fractional, nonnumeric and unsafe counters never become usage", () => {
  for (const invalid of [-1, 1.25, "10", true, Number.MAX_SAFE_INTEGER + 1, NaN, Infinity, null]) {
    assert.deepEqual(totals(normalizedUsage({ prompt_tokens: invalid, completion_tokens: invalid, total_tokens: invalid }, "chat_completions")),
      { input: null, output: null, total: null, source: null });
  }
  assert.deepEqual(totals(normalizedUsage({ prompt_tokens: Number.MAX_SAFE_INTEGER, completion_tokens: 1 }, "chat_completions")),
    { input: Number.MAX_SAFE_INTEGER, output: 1, total: null, source: null });
  assert.deepEqual(totals(normalizedUsage({ input_tokens: Number.MAX_SAFE_INTEGER, output_tokens: 0,
    cache_read_input_tokens: 1, cache_creation_input_tokens: 0 }, "anthropic")),
    { input: null, output: 0, total: null, source: null });
});

test("Anthropic SSE usage merges cumulative deltas and ignores null counter updates", () => {
  const meter = new UsageMeter("text/event-stream", "anthropic");
  const raw = [
    { type: "message_start", message: { usage: { input_tokens: 10, cache_read_input_tokens: 20,
      cache_creation_input_tokens: 3, output_tokens: 1, output_tokens_details: { thinking_tokens: 1 } } } },
    { type: "message_delta", usage: { input_tokens: null, cache_read_input_tokens: null,
      cache_creation_input_tokens: null, output_tokens: 7, output_tokens_details: null } },
    { type: "message_stop" },
  ].map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("");
  for (let offset = 0; offset < raw.length; offset += 13) meter.push(new TextEncoder().encode(raw.slice(offset, offset + 13)));
  const usage = meter.finish();
  assert.equal(usage?.inputTokens, 10);
  assert.equal(usage?.outputTokens, 7);
  assert.equal(usage?.reasoningTokens, 1);
  assert.equal(usage?.totalTokens, 40);
  assert.equal(meter.terminal, true);
});

test("SSE nested usage details are merged without replacing earlier counters", () => {
  const meter = new UsageMeter("text/event-stream", "chat_completions");
  const raw = [
    { usage: { prompt_tokens: 10, prompt_tokens_details: { cached_tokens: 8 } } },
    { usage: null },
    { usage: { completion_tokens: 7, prompt_tokens_details: { cache_write_tokens: 2 } } },
  ].map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("") + "data: [DONE]\n\n";
  meter.push(new TextEncoder().encode(raw));
  const usage = meter.finish();
  assert.equal(usage?.cacheReadTokens, 8);
  assert.equal(usage?.cacheWriteTokens, 2);
  assert.equal(usage?.totalTokens, 17);
  assert.equal(meter.terminal, true);
});

test("Ollama NDJSON input and output counters merge across distinct frames", () => {
  const meter = new UsageMeter("application/x-ndjson", "ollama");
  meter.push(new TextEncoder().encode('{"prompt_eval_count":10}\n{"eval_count":4,"done":true}\n'));
  assert.equal(meter.finish()?.totalTokens, 14);
  assert.equal(meter.terminal, true);
});

test("framed streams need a terminal frame; a JSON body is its own end", () => {
  const sse = new UsageMeter("text/event-stream", "chat_completions");
  sse.push(new TextEncoder().encode('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'));
  assert.equal(sse.streaming, true);
  assert.equal(sse.terminal, false);
  sse.push(new TextEncoder().encode('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'));
  assert.equal(sse.terminal, true);
  assert.equal(new UsageMeter("application/json", "chat_completions").streaming, false);
});

test("missing usage or malformed usage never fabricates token counters", () => {
  assert.equal(metered("responses", { output: [] }), null);
  assert.equal(metered("responses", { usage: [] }), null);
  assert.equal(normalizedUsage([], "responses"), null);
});
