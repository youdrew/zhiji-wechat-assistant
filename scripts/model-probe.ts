// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// One-shot model discovery and connection test for Core settings. The request,
// including any API key, arrives only on stdin; the result is one JSON line.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { listModels, testConnection, ModelConnectorError, type ModelConfig, type Protocol } from "../electron/model-connectors";
import { withRemoteModelTrace, flushModelTraceClient } from "../electron/model-trace-client";
import { modelTraceOutcome } from "../electron/model-traces";

const MAX_INPUT_BYTES = 64 * 1024;

function config(request: Record<string, unknown>, requireModel: boolean): ModelConfig {
  if (typeof request.protocol !== "string" || typeof request.baseUrl !== "string" ||
      (request.apiKey !== undefined && request.apiKey !== null && typeof request.apiKey !== "string") ||
      (requireModel && typeof request.model !== "string")) {
    throw new ModelConnectorError("invalid-request", "模型配置格式不正确");
  }
  return { protocol: request.protocol as Protocol, baseUrl: request.baseUrl,
    apiKey: typeof request.apiKey === "string" ? request.apiKey : "",
    model: typeof request.model === "string" ? request.model : "" };
}

export async function runProbe(raw: string): Promise<Record<string, unknown>> {
  let request: Record<string, unknown>;
  try {
    if (Buffer.byteLength(raw, "utf8") > MAX_INPUT_BYTES) throw new Error("too large");
    request = JSON.parse(raw);
    if (!request || typeof request !== "object" || Array.isArray(request)) throw new Error("invalid");
  } catch { return { error: "invalid-request" }; }
  const command = request.cmd;
  if (command !== "list" && command !== "test") return { error: "invalid-request" };
  try {
    const value = config(request, command === "test");
    return await withRemoteModelTrace(request.traceContext, async () => {
      const result = command === "list" ? await listModels(value) : await testConnection(value);
      modelTraceOutcome("success", result, command === "test" ? "not-applicable" : "pending");
      return { ...result } as Record<string, unknown>;
    });
  } catch (error) {
    return { error: error instanceof ModelConnectorError ? error.code : "provider-error" };
  }
}

const entry = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (entry === fileURLToPath(import.meta.url)) {
  const chunks: Buffer[] = [];
  let size = 0;
  process.stdin.on("data", (chunk: Buffer) => { size += chunk.length; if (size <= MAX_INPUT_BYTES) chunks.push(chunk); });
  process.stdin.on("end", () => {
    void runProbe(size > MAX_INPUT_BYTES ? "" : Buffer.concat(chunks).toString("utf8"))
      .then(async result => {
        await flushModelTraceClient();
        process.stdout.write(JSON.stringify(result) + "\n");
      });
  });
}
