import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import { withRemoteModelTrace, flushModelTraceClient, modelTraceClientStatus } from "../electron/model-trace-client";
import { beginModelTraceAttempt, captureModelResponse } from "../electron/model-traces";

// Its own file, so its own process: a finished producer keeps polling for a few
// seconds, and a closed server there would pause collection for other tests.
test("streamed deltas are uploaded in a few ordered batches, not one connection per event", async t => {
  const received: any[] = [];
  let ingests = 0;
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    if (req.url?.endsWith("/ingest")) {
      ingests++;
      const body = JSON.parse(raw);
      assert(Buffer.byteLength(raw) <= 64 * 1024 && (body.events?.length ?? 1) <= 64, "batch exceeds Core's ingest limits");
      received.push(...(body.events ?? [body]));
    }
    res.setHeader("content-type", "application/json");
    res.setHeader("connection", "close");
    res.end(JSON.stringify({ collecting: true, collectionVersion: 1, deleteVersion: 0, revision: 1,
      bufferBudgetBytes: 6 * 1024 * 1024 }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const endpoint = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  // Deltas arrive over time, as from a model, so the queue never reaches its own bound.
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({ async start(controller) {
    for (let index = 0; index < 200; index++) {
      controller.enqueue(encoder.encode(`data: {"choices":[{"delta":{"content":"第${index}段"}}]}\n\n`));
      if (index % 10 === 9) await new Promise(resolve => setTimeout(resolve, 5));
    }
    controller.enqueue(encoder.encode("data: [DONE]\n\n"));
    controller.close();
  } });
  await withRemoteModelTrace({ producerId: "burst", endpoint, collecting: true, collectionVersion: 1, deleteVersion: 0,
    taskId: "task-burst", executionId: "execution-burst", accountId: "burst", source: "test" }, async () => {
    const attempt = beginModelTraceAttempt({ protocol: "chat_completions", model: "synthetic", method: "POST",
      endpoint: "http://provider.invalid/v1" });
    assert(attempt);
    await captureModelResponse(new Response(stream, { headers: { "content-type": "text/event-stream" } }), attempt).text();
  });
  await flushModelTraceClient();
  assert.equal(received.at(-1)?.type, "end");
  assert.equal(received.at(-1)?.transportStatus, "completed");
  assert(received.length > 20 && ingests * 4 <= received.length, `${received.length} events used ${ingests} requests`);
  assert.equal(modelTraceClientStatus().queuedBytes, 0);
});
