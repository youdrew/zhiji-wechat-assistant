// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Model gateway process. Core sends service credentials, execution-token bindings and
// connection probes over this private stdin pipe; the gateway answers with one JSON
// line per message on stdout: ready, config application, attempt progress, usage records and probe results.
import path from "node:path";
import * as readline from "node:readline";
import { fileURLToPath } from "node:url";
import { ModelGateway, type GatewayBinding, type GatewayService } from "../electron/model-gateway";
import { flushModelTraceClient } from "../electron/model-trace-client";
import { runProbe } from "./model-probe";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const embeddingManifest = require("./embedding-model.json");
const { createEncoder } = require("./embedding-runner.cjs");

const MAX_LINE_BYTES = 1024 * 1024;

function output(value: unknown): void { process.stdout.write(JSON.stringify(value) + "\n"); }

export async function runGateway(): Promise<void> {
  const gateway = new ModelGateway(record => output({ type: "usage", record }), undefined,
    attempt => output({ type: "attempt", attempt }));
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
  const port = await gateway.listen();
  output({ type: "ready", port });
  const pending = new Set<Promise<void>>();
  const embeddingCalls = new Map<string, AbortController>();
  const judgeCalls = new Map<string, AbortController>();
  let encoder: any;
  let encoderKey = "";
  for await (const line of lines) {
    if (!line.trim() || Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) continue;
    let message: any;
    try { message = JSON.parse(line); } catch { continue; }
    if (!message || typeof message !== "object") continue;
    if (message.type === "config" && Array.isArray(message.services)) {
      gateway.configure(message.services as GatewayService[]);
      if (typeof message.id === "string") output({ type: "config.applied", id: message.id });
    } else if (message.type === "token.add" && typeof message.hash === "string" && message.binding) {
      gateway.addToken(message.hash, message.binding as GatewayBinding);
    } else if (message.type === "token.revoke" && typeof message.hash === "string") {
      gateway.revokeToken(message.hash);
    } else if (message.type === "embedding.cancel" && typeof message.id === "string") {
      embeddingCalls.get(message.id)?.abort();
    } else if (message.type === "embedding" && typeof message.id === "string") {
      const request = message.request ?? {};
      const controller = new AbortController();
      embeddingCalls.set(message.id, controller);
      const job = gateway.runLocalEmbedding(async signal => {
        if (request.modelId !== embeddingManifest.modelId || request.revision !== embeddingManifest.revision ||
            request.dimensions !== embeddingManifest.dimensions || typeof request.modelDir !== "string" ||
            !path.isAbsolute(request.modelDir)) throw new Error("embedding-model-invalid");
        const key = request.modelDir + "\n" + request.revision;
        if (encoderKey !== key) {
          await encoder?.close(); encoder = undefined; encoderKey = "";
          encoder = await createEncoder(request.modelDir); encoderKey = key;
        }
        const vectors = await encoder.encode(request.texts, request.kind, signal);
        return { modelId: embeddingManifest.modelId, revision: embeddingManifest.revision,
          dimensions: embeddingManifest.dimensions, vectors };
      }, String(request.plugin ?? "kyf.self-analysis"), request.kind === "query", controller.signal)
        .then(result => output({ type: "embedding.result", id: message.id, result }))
        .catch(() => output({ type: "embedding.result", id: message.id,
          result: { error: { code: controller.signal.aborted ? "embedding-cancelled" : "embedding-unavailable" } } }))
        .finally(() => { embeddingCalls.delete(message.id); pending.delete(job); });
      pending.add(job);
    } else if (message.type === "local-judge.cancel" && typeof message.id === "string") {
      judgeCalls.get(message.id)?.abort();
    } else if (message.type === "local-judge.stop" || message.type === "local-judge.selftest") {
      const controller = new AbortController();
      const id = message.id;
      if (typeof id !== "string") continue;
      judgeCalls.set(id, controller);
      const job = (message.type === "local-judge.stop" ? gateway.stopLocalJudge().then(() => ({ stopped: true })) :
        gateway.selftestLocalJudge(message.request?.local, controller.signal))
        .then(result => output({ type: "local-judge.result", id, result }))
        .catch(() => output({ type: "local-judge.result", id,
          result: { error: { code: controller.signal.aborted ? "judge-cancelled" : "judge-unavailable" } } }))
        .finally(() => { judgeCalls.delete(id); pending.delete(job); });
      pending.add(job);
    } else if (message.type === "probe") {
      const job = runProbe(JSON.stringify(message.request ?? {}))
        .then(result => output({ type: "probe.result", id: message.id, result }))
        .finally(() => pending.delete(job));
      pending.add(job);
    } else if (message.type === "shutdown") {
      break;
    }
  }
  for (const controller of embeddingCalls.values()) controller.abort();
  for (const controller of judgeCalls.values()) controller.abort();
  await Promise.allSettled(pending);
  await encoder?.close();
  await gateway.close();
  await flushModelTraceClient();
}

const entry = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (entry === fileURLToPath(import.meta.url)) {
  runGateway().then(() => process.exit(0), () => process.exit(1));
}
