// Synthetic consumer latency benchmark. Invoked by the matching Python harness.
import { createServer } from "node:http";
import { once } from "node:events";
import { performance } from "node:perf_hooks";
import os from "node:os";
import { withRemoteModelTrace, flushModelTraceClient, modelTraceClientStatus } from "../electron/model-trace-client";
import { modelTraceOutcome, modelTraceBufferStats } from "../electron/model-traces";
import { ModelTraceRelay } from "../electron/model-trace-relay";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const fixture = JSON.parse(input);
const responseEvents = Array.from({ length: 32 }, (_, index) => {
  const prefix = 'data: {"choices":[{"delta":{"content":"', suffix = '"}}]}\n\n';
  return prefix + "x".repeat(8192 - prefix.length - suffix.length) + suffix;
});
// Exactly 256 KiB wire text, 32 equal protocol events, 2 ms between events.
const bodyPrefix = '{"model":"synthetic","stream":true,"messages":[{"role":"user","content":"';
const bodySuffix = '"}]}';
const requestBody = bodyPrefix + "q".repeat(65536 - bodyPrefix.length - bodySuffix.length) + bodySuffix;
let businessRequests = 0;
const provider = createServer(async (request, response) => {
  let size = 0; for await (const chunk of request) size += chunk.length;
  if (size !== 65536) throw new Error("fixture request size changed");
  businessRequests++;
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  // Schedule against one origin so Windows timer granularity cannot accumulate
  // one rounding delay per chunk differently between the four groups.
  responseEvents.forEach((event, index) => setTimeout(() => response.write(event), index * 2));
  setTimeout(() => response.end(), responseEvents.length * 2);
});
provider.listen(0, "127.0.0.1"); await once(provider, "listening");
const direct = `http://127.0.0.1:${(provider.address() as { port: number }).port}/v1`;
const relay = await new ModelTraceRelay({ protocol: "chat_completions", baseUrl: direct,
  apiKey: "synthetic-benchmark-key", model: "synthetic" } as any, "1.18.34").start();
const samples: Record<string, { first: number; complete: number }[]> = { off: [], closed: [], open: [], direct: [] };
let peakHeap = process.memoryUsage().heapUsed, initialHeap = peakHeap;
async function fixtureRequest(path: string, value?: unknown) {
  const response = await fetch(`${fixture.endpoint}/${path}`, { method: value === undefined ? "GET" : "POST",
    ...(value === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }) });
  if (!response.ok) throw new Error(`fixture ${path} failed`);
  return response.json();
}
async function trial(mode: string) {
  const context = await fixtureRequest("fixture-prepare", { enabled: mode === "closed" || mode === "open", panel: mode === "open" });
  const unbind = relay.bind("synthetic-session", context);
  const endpoint = (mode === "direct" ? direct : relay.baseUrl) + "/chat/completions";
  let first = 0, total = 0;
  const start = performance.now();
  await withRemoteModelTrace(context, async () => {
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json",
      ...(mode === "direct" ? {} : { "x-kyf-relay-auth": relay.auth, "x-opencode-session-id": "synthetic-session" }) }, body: requestBody });
    if (!response.ok || !response.body) throw new Error("fixture business response failed");
    for await (const chunk of response.body) {
      if (!first) first = performance.now() - start;
      total += chunk.length;
    }
    if (total !== 262144) throw new Error("fixture response changed");
    modelTraceOutcome("success", undefined, "adopted");
  });
  const complete = performance.now() - start;
  unbind();
  peakHeap = Math.max(peakHeap, process.memoryUsage().heapUsed);
  // Drain only after measuring business completion; verify the normal group
  // never includes missing records, capacity cleanup, or a health pause.
  await flushModelTraceClient();
  const state = await fixtureRequest("fixture-settle");
  if ((mode === "closed" || mode === "open") && (!state.collecting || state.pending || state.incomplete))
    throw new Error(`normal sample lost diagnostics: ${JSON.stringify(state)}`);
  return { first, complete };
}
const stats = (values: number[]) => {
  const sorted = [...values].sort((a,b) => a-b);
  return { samples: values.length, medianMs: sorted[Math.floor(sorted.length/2)],
    p95Ms: sorted[Math.ceil(.95*sorted.length)-1], maxMs: sorted.at(-1) };
};
try {
  for (let index = 0; index < 110; index++) {
    const order = ["off", "closed", "open", "direct"];
    for (let offset = 0; offset < 4; offset++) {
      const mode = order[(index + offset) % order.length];
      const result = await trial(mode);
      if (index >= 10) samples[mode].push(result);
    }
  }
  const groups = Object.fromEntries(Object.entries(samples).map(([mode, rows]) => [mode, {
    first: stats(rows.map(row => row.first)), complete: stats(rows.map(row => row.complete)) }]));
  const deltas = Object.fromEntries(["closed", "open"].map(mode => [mode, {
    first: stats(samples[mode].map((row,index) => row.first - samples.off[index].first)),
    complete: stats(samples[mode].map((row,index) => row.complete - samples.off[index].complete)) }]));
  process.stdout.write(JSON.stringify({ groups, deltas, relayOverhead: {
    first: stats(samples.off.map((row,index) => row.first - samples.direct[index].first)),
    complete: stats(samples.off.map((row,index) => row.complete - samples.direct[index].complete)) },
    passed: Object.values(deltas).every(value => value.first.p95Ms <= 50 && value.complete.p95Ms <= 50),
    dataset: { requestBytes:65536,responseBytes:262144,messages:1,responseEvents:32,intervalMs:2,
      warmupsPerGroup:10,samplesPerGroup:100,rotation:true },
    conditions: { sameRelayForThreeStates:true, panel:fixture.desktop ? "production Electron panel, preload, IPC and 300 ms updates" : "authenticated list/detail/body queries every 300 ms; renderer measured separately",
      cache:"OS cache retained", engine:"provider boundary relay; full engine matrix is separate" },
    businessRequests, diagnostics:modelTraceClientStatus(), buffers:modelTraceBufferStats(),
    peakHeapDeltaBytes:peakHeap-initialHeap, environment:{node:process.version,os:os.release(),
      cpu:os.cpus()[0]?.model,memoryBytes:os.totalmem()} }) + "\n");
} finally { await relay.close(); provider.closeAllConnections(); provider.close(); }
