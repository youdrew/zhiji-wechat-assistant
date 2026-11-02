// Know Your Friends modification (2026-10-09): cover RFC1918 HTTP boundaries and public-host rejection.
import assert from "node:assert/strict";
import { it } from "node:test";
import { DEFAULT_MODEL_TIMEOUT_MS, MAX_MODEL_TIMEOUT_MS, modelTimeoutMs } from "../electron/model-timeout";

import {
  generateStructured, listModels, ModelConnectorError, testConnection,
  validateModelConfig, type ModelConfig, type Protocol,
} from "../electron/model-connectors";

const configs: Record<Protocol, ModelConfig> = {
  anthropic: { protocol: "anthropic", baseUrl: "https://example.invalid/v1", model: "test-model", apiKey: "secret-value" },
  responses: { protocol: "responses", baseUrl: "https://example.invalid/v1", model: "test-model", apiKey: "secret-value" },
  chat_completions: { protocol: "chat_completions", baseUrl: "https://example.invalid/v1", model: "test-model", apiKey: "secret-value" },
  gemini: { protocol: "gemini", baseUrl: "https://example.invalid/v1beta", model: "test-model", apiKey: "secret-value" },
  ollama: { protocol: "ollama", baseUrl: "http://127.0.0.1:11434", model: "test-model" },
};

type FetchHandler = (url: URL, init?: RequestInit) => Promise<Response> | Response;

async function withMockFetch<T>(handler: FetchHandler, task: () => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => handler(
    new URL(input instanceof Request ? input.url : String(input)), init,
  );
  try {
    return await task();
  } finally {
    globalThis.fetch = original;
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json" },
  });
}

function generationFixture(protocol: Protocol): unknown {
  const text = '{"ok":true}';
  switch (protocol) {
    case "responses": return {
      id: "resp_test", object: "response", status: "completed", model: "test-model",
      output: [{ type: "message", role: "assistant", content: [
        { type: "output_text", text, annotations: [] },
      ] }], usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 },
    };
    case "chat_completions": return {
      id: "chat_test", object: "chat.completion", model: "test-model",
      choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
      usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
    };
    case "anthropic": return {
      id: "msg_test", type: "message", role: "assistant", model: "test-model",
      content: [{ type: "text", text }], usage: { input_tokens: 3, output_tokens: 2 },
      stop_reason: "end_turn", stop_sequence: null,
    };
    case "gemini": return {
      candidates: [{ content: { role: "model", parts: [{ text }] }, finishReason: "STOP" }],
      usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2 },
    };
    case "ollama": return {
      model: "test-model", done: true, message: { role: "assistant", content: text },
      prompt_eval_count: 3, eval_count: 2,
    };
  }
}

it("rejects unsafe endpoints and never lets SDKs read a key from the URL", () => {
  const base = configs.responses;
  for (const url of ["file:///tmp/api", "https://key:secret@example.invalid/v1",
    "https://example.invalid/v1?key=secret", "https://example.invalid/v1#fragment",
    "http://example.invalid/v1", "https://example.invalid/v1\\redirect"]) {
    assert.throws(() => validateModelConfig({ ...base, baseUrl: url }), ModelConnectorError);
  }
  assert.equal(validateModelConfig(configs.ollama).baseUrl, "http://127.0.0.1:11434");
});

it("allows RFC1918 IPv4 HTTP boundaries while preserving existing loopback endpoints", () => {
  for (const host of ["10.0.0.0", "10.255.255.255", "172.16.0.0", "172.31.255.255",
    "192.168.0.0", "192.168.255.255", "localhost", "127.0.0.1", "[::1]"]) {
    const baseUrl = `http://${host}:8080/v1`;
    assert.equal(validateModelConfig({ ...configs.anthropic, baseUrl }).baseUrl, baseUrl, host);
  }
});

it("rejects public, non-RFC1918 and lookalike HTTP hosts with invalid-url", () => {
  for (const host of ["9.255.255.255", "11.0.0.0", "172.15.255.255", "172.32.0.0",
    "192.167.255.255", "192.169.0.0", "8.8.8.8", "100.64.0.1", "169.254.1.1",
    "0.0.0.0", "[fc00::1]", "[::ffff:192.168.1.1]", "example.invalid",
    "10.0.0.1.example.invalid", "192.168.1.1.example.invalid", "localhost.example.invalid",
    "10.0.0.256", "0x08080808"]) {
    assert.throws(() => validateModelConfig({ ...configs.anthropic,
      baseUrl: `http://${host}:8080/v1` }), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError, host);
      assert.equal(error.code, "invalid-url", host);
      return true;
    });
  }
  assert.equal(validateModelConfig({ ...configs.anthropic,
    baseUrl: "https://example.invalid/v1" }).baseUrl, "https://example.invalid/v1");
});

it("sends an Anthropic request to a private HTTP endpoint without allowing redirects", async () => {
  let calls = 0;
  const result = await withMockFetch((url, init) => {
    calls++;
    assert.equal(url.origin, "http://10.23.45.67:8080");
    assert.equal(url.pathname, "/v1/messages");
    assert.equal(init?.redirect, "error");
    return json(generationFixture("anthropic"));
  }, () => generateStructured({ ...configs.anthropic, baseUrl: "http://10.23.45.67:8080/v1" },
    { system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64 }));
  assert.equal(calls, 1);
  assert.equal(result.text, '{"ok":true}');
});

it("accepts only integer context sizes within the supported token range", () => {
  for (const contextTokens of [4096, 1000000]) {
    assert.equal(validateModelConfig({ ...configs.responses, contextTokens }).model,
      "test-model");
  }
  for (const contextTokens of [4095, 1000001, 8192.5, "8192", true]) {
    assert.throws(() => validateModelConfig({ ...configs.responses,
      contextTokens: contextTokens as unknown as number }), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "invalid-request");
      return true;
    });
  }
});

it("enumerates models through each official SDK with a bounded first page", async () => {
  const expected: Record<Protocol, string> = {
    responses: "/v1/models", chat_completions: "/v1/models", anthropic: "/v1/models",
    gemini: "/v1beta/models", ollama: "/api/tags",
  };
  for (const protocol of Object.keys(configs) as Protocol[]) {
    let calls = 0;
    let lastUrl = "";
    const found = await withMockFetch((url, init) => {
      calls++;
      lastUrl = url.toString();
      assert.equal(url.pathname, expected[protocol]);
      assert.equal(init?.redirect, "error");
      switch (protocol) {
        case "responses":
        case "chat_completions": return json({ data: [{ id: "test-model", context_window: 128000 }] });
        case "anthropic": return json({ data: [{ id: "test-model", display_name: "Test Model" }],
          has_more: false });
        case "gemini": return json({ models: [{ name: "models/test-model", displayName: "Test Model",
          inputTokenLimit: 128000 }] });
        case "ollama": return json({ models: [{ name: "test-model", model: "test-model",
          context_length: 128000 }] });
      }
    }, () => listModels(configs[protocol])).catch((error: unknown) => {
      throw new Error(`${protocol} list failed after ${calls} calls at ${lastUrl}`, { cause: error });
    });
    assert.equal(calls, 1, protocol);
    assert.equal(found.supported, true, protocol);
    assert.equal(found.models[0]?.id, "test-model", protocol);
    assert.equal(found.models[0]?.contextTokens,
      ["responses", "chat_completions", "gemini", "ollama"].includes(protocol) ? 128000 : undefined,
      protocol);
  }
});

it("allows manual model IDs when a compatible gateway has no models endpoint", async () => {
  const result = await withMockFetch(() => json({ error: "missing endpoint" }, 404),
    () => listModels(configs.chat_completions));
  assert.deepEqual(result, { models: [], supported: false });
});

it("reports a forbidden model list as an authorization error", async () => {
  await withMockFetch(() => json({ error: "forbidden" }, 403), async () => {
    await assert.rejects(() => listModels(configs.responses), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "auth");
      return true;
    });
  });
});

it("accepts the common Ollama /api URL without doubling that path", async () => {
  const result = await withMockFetch((url) => {
    assert.equal(url.pathname, "/api/tags");
    return json({ models: [{ model: "test-model", name: "test-model" }] });
  }, () => listModels({ ...configs.ollama, baseUrl: "http://127.0.0.1:11434/api" }));
  assert.equal(result.models[0]?.id, "test-model");
});

it("uses each SDK's generation format and returns only raw text and usage", async () => {
  const paths: Record<Protocol, string> = {
    responses: "/v1/responses", chat_completions: "/v1/chat/completions",
    anthropic: "/v1/messages", gemini: "/v1beta/models/test-model:generateContent",
    ollama: "/api/chat",
  };
  for (const protocol of Object.keys(configs) as Protocol[]) {
    let calls = 0;
    let lastUrl = "";
    const result = await withMockFetch(async (url, init) => {
      calls++;
      lastUrl = url.toString();
      assert.equal(url.pathname, paths[protocol]);
      assert.equal(init?.redirect, "error");
      const body = typeof init?.body === "string" ? init.body : "";
      assert.match(body, /synthetic prompt/u);
      assert.doesNotMatch(body, /private chat/u);
      return json(generationFixture(protocol));
    }, () => generateStructured(configs[protocol], {
      system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64,
    })).catch((error: unknown) => {
      throw new Error(`${protocol} generation failed after ${calls} calls at ${lastUrl}`, { cause: error });
    });
    assert.equal(calls, 1, protocol);
    assert.equal(result.text, '{"ok":true}', protocol);
    assert.deepEqual(result.usage, { inputTokens: 3, outputTokens: 2 }, protocol);
  }
});

it("sends an output cap only when the caller sets one, up to 32768", async () => {
  // The portrait classifier omits the cap so a thinking model can finish reasoning;
  // Anthropic needs max_tokens, so that caller asks for 32768.
  const capFields = ["max_output_tokens", "max_tokens", "maxOutputTokens", "num_predict"];
  const capOf = (body: any): unknown[] => [body, body.generationConfig, body.options]
    .flatMap(part => part ? capFields.filter(key => key in part).map(key => part[key]) : []);
  for (const protocol of Object.keys(configs) as Protocol[]) {
    for (const maxOutputTokens of [undefined, 32768]) {
      let caps: unknown[] = [];
      await withMockFetch((_url, init) => {
        caps = capOf(JSON.parse(String(init?.body || "{}")));
        return json(generationFixture(protocol));
      }, () => generateStructured(configs[protocol], {
        system: "Return JSON.", prompt: "synthetic prompt", jsonMode: true, maxOutputTokens,
      }));
      assert.deepEqual(caps, maxOutputTokens === undefined ? [] : [maxOutputTokens],
        `${protocol} ${maxOutputTokens}`);
    }
  }
  let calls = 0;
  await withMockFetch(() => { calls++; return json(generationFixture("responses")); }, async () => {
    await assert.rejects(() => generateStructured(configs.responses, {
      system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 32769,
    }), (error: unknown) => error instanceof ModelConnectorError && error.code === "invalid-request");
  });
  assert.equal(calls, 0);
});

it("accepts long inference limits while rejecting unbounded request timeouts", async () => {
  let calls = 0;
  await withMockFetch(() => {
    calls++;
    return json(generationFixture("responses"));
  }, async () => {
    const result = await generateStructured(configs.responses, {
      system: "Return JSON.", prompt: "synthetic prompt", timeoutMs: MAX_MODEL_TIMEOUT_MS,
    });
    assert.equal(result.text, '{"ok":true}');
    await assert.rejects(() => generateStructured(configs.responses, {
      system: "Return JSON.", prompt: "synthetic prompt", timeoutMs: MAX_MODEL_TIMEOUT_MS + 1,
    }), (error: unknown) => error instanceof ModelConnectorError && error.code === "invalid-request");
  });
  assert.equal(calls, 1);
});

it("validates the shared saved deadline and defaults existing settings to sixty minutes", () => {
  assert.equal(modelTimeoutMs(), DEFAULT_MODEL_TIMEOUT_MS);
  for (const value of [60_000, 3_600_000, MAX_MODEL_TIMEOUT_MS]) assert.equal(modelTimeoutMs(value), value);
  for (const value of [0, 59_999, MAX_MODEL_TIMEOUT_MS + 1, 60_000.5, "3600000", true, {}, NaN, Infinity]) {
    assert.throws(() => modelTimeoutMs(value));
    assert.throws(() => validateModelConfig({ ...configs.responses, timeoutMs: value as number }),
      (error: unknown) => error instanceof ModelConnectorError && error.code === "invalid-request");
  }
});

it("uses saved inference timeouts above legacy request limits but keeps connection probes short", async context => {
  const limits: number[] = [];
  const original = AbortSignal.timeout;
  context.mock.method(AbortSignal, "timeout", (milliseconds: number) => {
    limits.push(milliseconds);
    return original(milliseconds);
  });
  let dispatchers = 0;
  await withMockFetch((_url, init) => {
    if ((init as RequestInit & { dispatcher?: unknown }).dispatcher) dispatchers++;
    return json(generationFixture("responses"));
  }, async () => {
    await generateStructured({ ...configs.responses, timeoutMs: 7_200_000 }, {
      system: "Synthetic", prompt: "Synthetic", timeoutMs: 45_000,
    });
    assert.ok(limits.includes(7_200_000));
    assert.ok(!limits.includes(45_000));
    limits.length = 0;
    await testConnection({ ...configs.responses, timeoutMs: 7_200_000 });
    assert.ok(limits.includes(15_000));
    assert.ok(!limits.includes(7_200_000));
    assert.equal(dispatchers, 2, "bounded signals own timeout instead of undici's implicit five-minute deadline");
  });
});

function truncatedFixture(protocol: Protocol): unknown {
  // Each provider's own "stopped at the output cap" marker, with the cut-off JSON.
  const text = '{"answers":{"intent_detail_flirt":[0.05,0.05';
  const fixture = generationFixture(protocol) as Record<string, any>;
  switch (protocol) {
    case "responses": return { ...fixture, status: "incomplete",
      incomplete_details: { reason: "max_output_tokens" },
      output: [{ type: "message", role: "assistant", content: [
        { type: "output_text", text, annotations: [] }] }] };
    case "chat_completions": return { ...fixture,
      choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "length" }] };
    case "anthropic": return { ...fixture, content: [{ type: "text", text }], stop_reason: "max_tokens" };
    case "gemini": return { ...fixture,
      candidates: [{ content: { role: "model", parts: [{ text }] }, finishReason: "MAX_TOKENS" }] };
    case "ollama": return { ...fixture, message: { role: "assistant", content: text },
      done_reason: "length" };
  }
}

it("reports output cut at the provider's cap as truncated, not as a format error", async () => {
  for (const protocol of Object.keys(configs) as Protocol[]) {
    let calls = 0;
    await withMockFetch(() => { calls++; return json(truncatedFixture(protocol)); }, async () => {
      await assert.rejects(() => generateStructured(configs[protocol], {
        system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
      }), (error: unknown) => {
        assert.ok(error instanceof ModelConnectorError, protocol);
        assert.equal(error.code, "output-truncated", protocol);
        // The text that did arrive travels with the error, so the caller can salvage it.
        assert.equal(error.partialText, '{"answers":{"intent_detail_flirt":[0.05,0.05', protocol);
        return true;
      });
    });
    assert.equal(calls, 1, protocol);
  }
  // A gateway that cuts the reply is not a connection failure, including Anthropic's small probe cap.
  for (const protocol of ["chat_completions", "anthropic"] as const) {
    const probe = await withMockFetch(() => json(truncatedFixture(protocol)),
      () => testConnection(configs[protocol]));
    assert.equal(probe.ok, true, protocol);
  }
});

it("connection test sends a fixed synthetic prompt, and only Anthropic sets a small output cap", async () => {
  const capFields = ["max_output_tokens", "max_tokens", "maxOutputTokens", "num_predict"];
  const capsOf = (body: any): unknown[] => [body, body.generationConfig, body.options]
    .flatMap(part => part ? capFields.filter(key => key in part).map(key => part[key]) : []);
  for (const protocol of Object.keys(configs) as Protocol[]) {
    const result = await withMockFetch((_url, init) => {
      const body = typeof init?.body === "string" ? init.body : "";
      assert.match(body, /Reply with JSON/u, protocol);
      assert.doesNotMatch(body, /private chat/u, protocol);
      assert.deepEqual(capsOf(JSON.parse(body)), protocol === "anthropic" ? [128] : [], protocol);
      return json(generationFixture(protocol));
    }, () => testConnection(configs[protocol]));
    assert.equal(result.ok, true, protocol);
    assert.equal(result.model, "test-model", protocol);
    assert.ok(result.latencyMs >= 0);
  }
});

it("keeps official DeepSeek structured requests out of default thinking mode", async () => {
  const config = { ...configs.responses, baseUrl: "https://api.deepseek.com/" };
  const result = await withMockFetch((_url, init) => {
    const body = JSON.parse(String(init?.body || "{}"));
    assert.equal(body.text?.format?.type, "json_object");
    assert.equal(body.reasoning?.effort, "none");
    return json(generationFixture("responses"));
  }, () => generateStructured(config, {
    system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
  }));
  assert.equal(result.text, '{"ok":true}');
});

it("requests JSON mode and falls back once when a compatible gateway rejects it", async () => {
  const bodies: Record<string, unknown>[] = [];
  const result = await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
    return bodies.length === 1
      ? json({ error: { message: "format unsupported" } }, 400)
      : json(generationFixture("responses"));
  }, () => generateStructured(configs.responses, {
    system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
  }));
  assert.equal((bodies[0].text as { format: { type: string } }).format.type, "json_object");
  assert.equal(bodies[1].text, undefined);
  assert.equal(result.text, '{"ok":true}');
});

it("keeps official DeepSeek thinking disabled when JSON mode falls back", async () => {
  const config = { ...configs.responses, baseUrl: "https://api.deepseek.com/v1" };
  const bodies: Record<string, unknown>[] = [];
  await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
    return bodies.length === 1
      ? json({ error: { message: "synthetic format rejection" } }, 422)
      : json(generationFixture("responses"));
  }, () => generateStructured(config, {
    system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
  }));
  assert.equal(bodies.length, 2);
  assert.equal((bodies[0].reasoning as { effort: string }).effort, "none");
  assert.equal(bodies[1].text, undefined);
  assert.equal((bodies[1].reasoning as { effort: string }).effort, "none");
});

it("keeps provider error bodies and keys out of application errors", async () => {
  await withMockFetch(() => json({ error: { message: "secret-value" } }, 401), async () => {
    await assert.rejects(() => generateStructured(configs.responses, {
      system: "", prompt: "synthetic prompt", maxOutputTokens: 64,
    }), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "auth");
      assert.doesNotMatch(error.message, /secret-value/u);
      return true;
    });
  });
});

it("stops before buffering a response larger than the connector limit", async () => {
  await withMockFetch(() => new Response(null, {
    status: 200, headers: { "content-length": String(3 * 1024 * 1024) },
  }), async () => {
    await assert.rejects(() => listModels(configs.responses), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "response-too-large");
      return true;
    });
  });
});

it("asks chat-template servers to answer structured requests without thinking", async () => {
  const bodies: Record<string, unknown>[] = [];
  await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
    return json(generationFixture("chat_completions"));
  }, async () => {
    await generateStructured(configs.chat_completions, { system: "Return JSON.", prompt: "synthetic prompt",
      maxOutputTokens: 8192, jsonMode: true, disableThinking: true });
    await generateStructured(configs.chat_completions, { system: "Return JSON.", prompt: "synthetic prompt",
      maxOutputTokens: 8192, jsonMode: true });
  });
  assert.deepEqual(bodies[0].chat_template_kwargs, { thinking: false, enable_thinking: false });
  assert.equal((bodies[0].response_format as { type: string }).type, "json_object");
  assert.equal(bodies[0].max_tokens, 8192);
  assert.equal(bodies[1].chat_template_kwargs, undefined, "thinking is left alone unless asked");
});

it("drops only the template field when a hosted API rejects it, then plain JSON", async () => {
  for (const rejections of [1, 2]) {
    const bodies: Record<string, unknown>[] = [];
    const result = await withMockFetch((_url, init) => {
      bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
      return bodies.length <= rejections
        ? json({ error: { message: "Unrecognized request argument supplied: chat_template_kwargs" } }, 400)
        : json(generationFixture("chat_completions"));
    }, () => generateStructured(configs.chat_completions, { system: "Return JSON.", prompt: "synthetic prompt",
      maxOutputTokens: 64, jsonMode: true, disableThinking: true }));
    assert.equal(result.text, '{"ok":true}');
    assert.equal(bodies.length, rejections + 1);
    assert.equal(bodies[1].chat_template_kwargs, undefined);
    assert.equal((bodies[1].response_format as { type: string }).type, "json_object");
    if (rejections === 2) assert.equal(bodies[2].response_format, undefined);
  }
});

it("rejects a non-boolean thinking switch", async () => {
  await assert.rejects(generateStructured(configs.chat_completions, { system: "s", prompt: "p",
    disableThinking: "yes" as unknown as boolean }), (error: unknown) =>
    error instanceof ModelConnectorError && error.code === "invalid-request");
});

function sse(events: Array<Record<string, unknown>>, done = false): Response {
  const frames = events.map(event => `${typeof event.type === "string" ? `event: ${event.type}\n` : ""}` +
    `data: ${JSON.stringify(event)}\n\n`).join("") + (done ? "data: [DONE]\n\n" : "");
  return new Response(frames, { headers: { "content-type": "text/event-stream" } });
}

function chatChunks(finishReason: string): Array<Record<string, unknown>> {
  const chunk = (delta: unknown, finish: string | null = null) => ({ id: "chat_test", object: "chat.completion.chunk",
    model: "test-model", choices: [{ index: 0, delta, finish_reason: finish }] });
  return [chunk({ role: "assistant", content: '{"ok":' }), chunk({ content: "true}" }), chunk({}, finishReason),
    { id: "chat_test", object: "chat.completion.chunk", model: "test-model", choices: [],
      usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } }];
}

function responseEvents(terminal: "response.completed" | "response.incomplete"): Array<Record<string, unknown>> {
  const response = { id: "resp_test", object: "response", model: "test-model", output: [] };
  return [{ type: "response.created", response: { ...response, status: "in_progress" } },
    { type: "response.output_text.delta", delta: '{"ok":' }, { type: "response.output_text.delta", delta: "true}" },
    { type: terminal, response: { ...response, usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 },
      ...(terminal === "response.incomplete" ?
        { status: "incomplete", incomplete_details: { reason: "max_output_tokens" } } : { status: "completed" }) } }];
}

it("streams structured requests with usage and still reports a cut at the output cap", async () => {
  const request = { system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
    disableThinking: true, stream: true };
  const bodies: Record<string, any>[] = [];
  const chat = await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")));
    return sse(chatChunks("stop"), true);
  }, () => generateStructured(configs.chat_completions, request));
  assert.equal(chat.text, '{"ok":true}');
  assert.deepEqual(chat.usage, { inputTokens: 3, outputTokens: 2 });
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].stream, true);
  assert.deepEqual(bodies[0].stream_options, { include_usage: true });
  assert.equal(bodies[0].response_format.type, "json_object");
  assert.deepEqual(bodies[0].chat_template_kwargs, { thinking: false, enable_thinking: false });

  const responses = await withMockFetch(() => sse(responseEvents("response.completed")),
    () => generateStructured(configs.responses, request));
  assert.equal(responses.text, '{"ok":true}');
  assert.deepEqual(responses.usage, { inputTokens: 3, outputTokens: 2 });

  for (const [protocol, reply] of [["chat_completions", () => sse(chatChunks("length"), true)],
    ["responses", () => sse(responseEvents("response.incomplete"))]] as const) {
    let calls = 0;
    await assert.rejects(withMockFetch(() => { calls++; return reply(); },
      () => generateStructured(configs[protocol], request)), (error: unknown) =>
      error instanceof ModelConnectorError && error.code === "output-truncated");
    assert.equal(calls, 1, protocol);
  }
});

it("reads a gateway's whole reply to a stream request once, keeping usage and truncation", async () => {
  const request = { system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
    stream: true };
  let calls = 0;
  const result = await withMockFetch(() => { calls++; return json(generationFixture("chat_completions")); },
    () => generateStructured(configs.chat_completions, request));
  assert.equal(result.text, '{"ok":true}');
  assert.deepEqual(result.usage, { inputTokens: 3, outputTokens: 2 });
  assert.equal(calls, 1);
  calls = 0;
  await assert.rejects(withMockFetch(() => { calls++; return json(truncatedFixture("chat_completions")); },
    () => generateStructured(configs.chat_completions, request)), (error: unknown) =>
    error instanceof ModelConnectorError && error.code === "output-truncated");
  assert.equal(calls, 1);
});

it("sends the same structured request once without SSE when a server rejects streaming", async () => {
  const bodies: Record<string, any>[] = [];
  const result = await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")));
    return bodies.length === 1 ? json({ error: { message: "stream unsupported" } }, 400)
      : json(generationFixture("chat_completions"));
  }, () => generateStructured(configs.chat_completions, { system: "Return JSON.", prompt: "synthetic prompt",
    maxOutputTokens: 64, jsonMode: true, disableThinking: true, stream: true }));
  assert.equal(result.text, '{"ok":true}');
  assert.equal(bodies.length, 2);
  assert.equal(bodies[1].stream, undefined);
  assert.equal(bodies[1].stream_options, undefined);
  assert.equal(bodies[1].response_format.type, "json_object");
  assert.deepEqual(bodies[1].chat_template_kwargs, { thinking: false, enable_thinking: false });
  // A provider failure is not a stream rejection; a long generation is never sent twice.
  let calls = 0;
  await assert.rejects(withMockFetch(() => { calls++; return json({ error: { message: "upstream" } }, 502); },
    () => generateStructured(configs.chat_completions, { system: "s", prompt: "p", stream: true })),
  (error: unknown) => error instanceof ModelConnectorError && error.status === 502);
  assert.equal(calls, 1);
});

it("keeps protocols without streaming bounded when a caller asks to stream", async () => {
  for (const protocol of ["anthropic", "gemini", "ollama"] as const) {
    await withMockFetch(() => new Response(null, {
      status: 200, headers: { "content-length": String(3 * 1024 * 1024) },
    }), async () => {
      await assert.rejects(generateStructured(configs[protocol], { system: "s", prompt: "p", maxOutputTokens: 64,
        stream: true }), (error: unknown) => error instanceof ModelConnectorError && error.code === "response-too-large");
    });
  }
});
