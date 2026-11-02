// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Synthetic pixels only; verifies real inline bytes on every supported protocol.
// Modified 2026-10-13: the advisor's media look goes through the Core gateway with the execution token.
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { generateStructured, ModelConnectorError, type ModelConfig, type Protocol } from "../electron/model-connectors";
import { AdvisorRuntime, type EngineService } from "../plugins/kyf.advisor/engine/runtime";

const data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
const image = { mime: "image/png" as const, data };
const config = (protocol: Protocol): ModelConfig => ({ protocol, baseUrl: "http://127.0.0.1:9/v1", model: "synthetic-vision" });
const service: EngineService = { id: "svc-1", protocol: "chat_completions", model: "synthetic-vision", url: "http://127.0.0.1:9/s/svc-1" };
const token = "synthetic-execution-token";
/** The gateway endpoint and token of the media look; returns the request body. */
function gatewayBody(input: RequestInfo | URL, init?: RequestInit): any {
  const sent = new Request(input, init);
  assert.equal(sent.url, "http://127.0.0.1:9/s/svc-1~media-understanding/chat/completions");
  assert.equal(sent.headers.get("authorization"), `Bearer ${token}`);
  return JSON.parse(String(init?.body));
}
const response = (protocol: Protocol) => ({
  responses: { id: "synthetic", object: "response", status: "completed", output: [{ type: "message", role: "assistant",
    content: [{ type: "output_text", text: "白色区域", annotations: [] }] }] },
  chat_completions: { choices: [{ index: 0, message: { role: "assistant", content: "白色区域" }, finish_reason: "stop" }] },
  anthropic: { id: "synthetic", type: "message", role: "assistant", model: "synthetic-vision",
    content: [{ type: "text", text: "白色区域" }], usage: { input_tokens: 10, output_tokens: 4 }, stop_reason: "end_turn" },
  gemini: { candidates: [{ content: { role: "model", parts: [{ text: "白色区域" }] } }] },
  ollama: { model: "synthetic-vision", done: true, message: { role: "assistant", content: "白色区域" } },
}[protocol]);

test("all model protocols receive actual image bytes, not filenames or placeholders", async () => {
  const original = globalThis.fetch;
  try {
    for (const protocol of ["responses", "chat_completions", "anthropic", "gemini", "ollama"] as Protocol[]) {
      let calls = 0;
      globalThis.fetch = async (_input, init) => {
        calls++;
        const body = JSON.parse(String(init?.body));
        if (protocol === "responses") {
          assert.equal(body.input[0].content[1].image_url, `data:image/png;base64,${data}`);
          assert.equal(body.store, false);
        } else if (protocol === "chat_completions") {
          assert.equal(body.messages[1].content[1].image_url.url, `data:image/png;base64,${data}`);
        } else if (protocol === "anthropic") {
          assert.deepEqual(body.messages[0].content[1].source, { type: "base64", media_type: "image/png", data });
        } else if (protocol === "gemini") {
          assert.deepEqual(body.contents[0].parts[1].inlineData, { mimeType: "image/png", data });
        } else {
          assert.deepEqual(body.messages[1].images, [data]);
        }
        return new Response(JSON.stringify(response(protocol)), { headers: { "content-type": "application/json" } });
      };
      const result = await generateStructured(config(protocol), { system: "Describe visible pixels only", prompt: "Synthetic image",
        images: [image], maxOutputTokens: 128 });
      assert.equal(result.text, "白色区域", protocol);
      assert.equal(calls, 1, protocol);
    }
  } finally { globalThis.fetch = original; }
});

test("invalid image bytes, MIME, count and capacity fail before network", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("should not fetch"); };
  try {
    for (const images of [[], Array(7).fill(image), [{ ...image, data: "file:///private/image.png" }],
      [{ ...image, mime: "image/jpeg" }], [{ ...image, data: "eHl6" }],
      [{ ...image, data: "A".repeat(2_796_208) }]]) {
      await assert.rejects(generateStructured(config("chat_completions"), { system: "", prompt: "synthetic",
        images: images as any }), (error: unknown) => error instanceof ModelConnectorError && error.code === "invalid-request");
    }
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test("video inspection is transient, scoped, and explicitly excludes audio/full-video claims", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "synthetic-vision-"));
  const original = globalThis.fetch;
  let engines = 0;
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state"),
    startEngine: async () => { engines++; throw new Error("must not start"); } });
  try {
    globalThis.fetch = async (input, init) => {
      const body = gatewayBody(input, init);
      assert.match(body.messages[1].content[0].text, /0、1500、3000/);
      assert.match(body.messages[1].content[0].text, /未提供音轨/);
      assert.match(body.messages[0].content, /不得根据人脸推断身份、性格/);
      assert.equal(body.messages[1].content.filter((part: any) => part.type === "image_url").length, 3);
      return new Response(JSON.stringify(response("chat_completions")), { headers: { "content-type": "application/json" } });
    };
    const request = { account: "synthetic-account", user: "synthetic-user", agentId: "synthetic-agent", threadId: "synthetic-thread",
      mediaKind: "video" as const, images: [0, 1500, 3000].map(timeMs => ({ ...image, timeMs })) };
    const result = await runtime.inspectMedia(service, token, request);
    assert.equal(result.text, "白色区域");
    assert.equal(result.runtimeSessionId, "");
    assert.equal(engines, 0);
    assert.deepEqual(await readdir(root), []);
    await assert.rejects(runtime.inspectMedia(service, token, { ...request, images: [image] }), /格式/);
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(runtime.inspectMedia(service, token, request, () => {}, controller.signal), /停止/);
  } finally {
    globalThis.fetch = original;
    await runtime.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("animated images send only their explicit first-frame scope to the visual model", async () => {
  const original = globalThis.fetch;
  const root = path.join(os.tmpdir(), "synthetic-no-write-vision");
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state") });
  try {
    globalThis.fetch = async (input, init) => {
      const body = gatewayBody(input, init);
      assert.match(body.messages[1].content[0].text, /动态图片消息的首帧/);
      assert.match(body.messages[1].content[0].text, /未提供后续动画/);
      return new Response(JSON.stringify(response("chat_completions")), { headers: { "content-type": "application/json" } });
    };
    const request = { account: "synthetic-account", user: "synthetic-user", agentId: "synthetic-agent", threadId: "synthetic-thread",
      mediaKind: "image" as const, images: [image], sampling: "animated-image-first-frame" as const };
    assert.equal((await runtime.inspectMedia(service, token, request)).text, "白色区域");
    await assert.rejects(runtime.inspectMedia(service, token, { ...request,
      sampling: "arbitrary-untrusted-text" as any }), /格式/);
  } finally {
    globalThis.fetch = original;
    await runtime.close();
  }
});

test("animated stickers reach the provider as real pixels with first-frame limits", async () => {
  const original = globalThis.fetch;
  const root = path.join(os.tmpdir(), "synthetic-no-write-sticker");
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state") });
  try {
    globalThis.fetch = async (input, init) => {
      const body = gatewayBody(input, init);
      assert.match(body.messages[1].content[0].text, /动态表情\/贴纸的首帧/);
      assert.match(body.messages[1].content[0].text, /未提供后续动画/);
      assert.equal(body.messages[1].content[1].image_url.url, `data:image/png;base64,${data}`);
      return new Response(JSON.stringify(response("chat_completions")), { headers: { "content-type": "application/json" } });
    };
    const request = { account: "synthetic-account", user: "synthetic-user", agentId: "synthetic-agent", threadId: "synthetic-thread",
      mediaKind: "sticker" as const, images: [image], sampling: "animated-image-first-frame" as const };
    assert.equal((await runtime.inspectMedia(service, token, request)).text, "白色区域");
    globalThis.fetch = async () => new Response('{"error":{"code":"execution-revoked"}}', { status: 401,
      headers: { "content-type": "application/json" } });
    await assert.rejects(runtime.inspectMedia(service, token, request), (error: any) => error.code === "auth",
      "a revoked execution surfaces as the advisor's auth failure");
    await assert.rejects(runtime.inspectMedia(service, "", request), (error: any) => error.code === "invalid-request");
  } finally {
    globalThis.fetch = original;
    await runtime.close();
  }
});

test("sticker frames arrive in order and a label request asks for one short line", async () => {
  const original = globalThis.fetch;
  const root = path.join(os.tmpdir(), "synthetic-no-write-sticker-frames");
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state") });
  const bodies: any[] = [];
  try {
    globalThis.fetch = async (input, init) => {
      bodies.push(gatewayBody(input, init));
      return new Response(JSON.stringify(response("chat_completions")), { headers: { "content-type": "application/json" } });
    };
    const request = { account: "synthetic-account", user: "synthetic-user", agentId: "sticker-labels", threadId: "sticker-labels",
      mediaKind: "sticker" as const, sampling: "animated-frames" as const, purpose: "sticker-label" as const,
      images: [0, 120, 240, 360].map(timeMs => ({ ...image, timeMs })) };
    assert.equal((await runtime.inspectMedia(service, token, request)).text, "白色区域");
    assert.match(bodies[0].messages[1].content[0].text, /按播放顺序抽取的 4 帧/);
    assert.match(bodies[0].messages[1].content[0].text, /0、120、240、360/);
    assert.match(bodies[0].messages[0].content, /不超过 40 个字/);
    assert.equal(bodies[0].messages[1].content.filter((part: any) => part.type === "image_url").length, 4);
    // Without the label purpose the frames are an ordinary observation.
    await runtime.inspectMedia(service, token, { ...request, purpose: undefined });
    assert.match(bodies[1].messages[0].content, /最多 600 字/);
    for (const invalid of [
      { ...request, images: [...request.images, { ...image, timeMs: 480 }] },
      { ...request, images: [{ ...image, timeMs: 240 }, { ...image, timeMs: 120 }] },
      { ...request, images: [image, image] },
      { ...request, mediaKind: "image" as const },
      { ...request, sampling: undefined, images: [image, image] },
      { ...request, purpose: "anything" as any },
    ]) await assert.rejects(runtime.inspectMedia(service, token, invalid), /格式/);
    assert.equal(bodies.length, 2);
  } finally {
    globalThis.fetch = original;
    await runtime.close();
  }
});

test("sticker labels reserve their small output budget for text without changing other media thinking", async () => {
  const original = globalThis.fetch;
  const root = path.join(os.tmpdir(), "synthetic-no-write-sticker-label-thinking");
  const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state") });
  const bodies: any[] = [];
  try {
    globalThis.fetch = async (input, init) => {
      const body = gatewayBody(input, init);
      bodies.push(body);
      // Reproduce a successful HTTP response that spends the whole label
      // budget on reasoning and never produces the text the caller requires.
      const exhausted = body.max_tokens === 300 && body.chat_template_kwargs?.enable_thinking !== false;
      const value = exhausted ? {
        choices: [{ index: 0, message: { role: "assistant", content: "", reasoning_content: "synthetic reasoning" },
          finish_reason: "length" }],
        usage: { completion_tokens: 300, completion_tokens_details: { reasoning_tokens: 300 } },
      } : response("chat_completions");
      return new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
    };
    const request = { account: "synthetic-account", user: "synthetic-user", agentId: "sticker-labels", threadId: "sticker-labels",
      mediaKind: "sticker" as const, purpose: "sticker-label" as const, images: [image] };
    for (const thinking of [undefined, true, false]) {
      assert.equal((await runtime.inspectMedia(service, token, { ...request, thinking })).text, "白色区域");
      const body = bodies.at(-1);
      assert.equal(body.max_tokens, 300);
      assert.deepEqual(body.chat_template_kwargs, { thinking: false, enable_thinking: false });
    }
    for (const mediaKind of ["image", "sticker", "video"] as const) {
      for (const thinking of [undefined, true, false]) {
        await runtime.inspectMedia(service, token, { ...request, purpose: undefined, mediaKind, thinking,
          images: [{ ...image, timeMs: 0 }] });
        const body = bodies.at(-1);
        assert.equal(body.max_tokens, 1200);
        assert.deepEqual(body.chat_template_kwargs, thinking === false ? { thinking: false, enable_thinking: false } : undefined);
      }
    }
    assert.equal(bodies.length, 12);
  } finally {
    globalThis.fetch = original;
    await runtime.close();
  }
});
