import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import { withRemoteModelTrace, flushModelTraceClient, modelTraceClientStatus } from "../electron/model-trace-client";
import { beginModelTraceAttempt, captureModelResponse } from "../electron/model-traces";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

test("revoked producer polling and ingest preserve another account's in-flight capture", async t => {
  const events: Array<{ producer: string; event: any }> = [];
  const liveAck = deferred();
  const deniedPoll = deferred();
  const rejectedIngest = deferred();
  const releaseIngest = deferred();
  const lateIngest = deferred();
  const lateRevocation = deferred();
  const releaseLateFailure = deferred();
  const server = createServer(async (req, res) => {
    const producer = String(req.headers["x-model-trace-producer"]);
    const path = req.url?.split("/").at(-1);
    let raw = "";
    for await (const chunk of req) raw += chunk;
    res.setHeader("content-type", "application/json");
    if (producer === "deleted-late" && path === "producer-state") {
      await lateIngest.promise;
      res.statusCode = 403; res.end("{}"); lateRevocation.resolve(); return;
    }
    if (producer === "deleted-late" && path === "ingest") {
      lateIngest.resolve();
      await releaseLateFailure.promise;
      res.statusCode = 503; res.end("{}"); return;
    }
    if (producer === "deleted-poll" && path === "producer-state") {
      await liveAck.promise;
      res.statusCode = 403; res.end("{}"); deniedPoll.resolve(); return;
    }
    if (producer === "deleted-ingest" && path === "ingest") {
      rejectedIngest.resolve();
      await releaseIngest.promise;
      res.statusCode = 403; res.end("{}"); return;
    }
    if (path === "ack" && producer === "live") liveAck.resolve();
    if (path === "ingest") events.push({ producer, event: JSON.parse(raw) });
    res.end(JSON.stringify({ collecting: true, collectionVersion: 1, deleteVersion: 0,
      revision: 1, bufferBudgetBytes: 6 * 1024 * 1024 }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const endpoint = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const context = (producerId: string) => ({ producerId, endpoint, collecting: true,
    collectionVersion: 1, deleteVersion: 0, taskId: `task-${producerId}`,
    executionId: `execution-${producerId}`, accountId: producerId, source: "test" });
  const meta = { protocol: "chat_completions", model: "synthetic", method: "POST", endpoint: "http://provider.invalid/v1" };

  await Promise.all([
    withRemoteModelTrace(context("deleted-poll"), () => deniedPoll.promise),
    withRemoteModelTrace(context("live"), async () => {
      const attempt = beginModelTraceAttempt(meta);
      assert(attempt);
      await deniedPoll.promise;
      await new Promise(resolve => setTimeout(resolve, 30));
      await captureModelResponse(new Response('{"choices":[{"message":{"content":"still live"}}]}',
        { headers: { "content-type": "application/json" } }), attempt).text();
    }),
  ]);
  await flushModelTraceClient();
  assert(events.some(item => item.producer === "live" && item.event.type === "end" &&
    item.event.transportStatus === "completed"), "poll revocation must not invalidate the live attempt");

  await Promise.all([
    withRemoteModelTrace(context("deleted-ingest"), async () => {
      const attempt = beginModelTraceAttempt(meta);
      await captureModelResponse(new Response("discarded"), attempt).text();
    }),
    withRemoteModelTrace(context("live-queued"), async () => {
      await rejectedIngest.promise;
      const attempt = beginModelTraceAttempt(meta);
      assert(attempt);
      await captureModelResponse(new Response("queued survivor"), attempt).text();
      releaseIngest.resolve();
    }),
  ]);
  await flushModelTraceClient();
  assert(events.some(item => item.producer === "live-queued" && item.event.type === "end" &&
    item.event.transportStatus === "completed"), "ingest revocation must preserve another producer's queued events");

  await Promise.all([
    withRemoteModelTrace(context("deleted-late"), async () => {
      const attempt = beginModelTraceAttempt(meta);
      await captureModelResponse(new Response("deleted before its late transport failure"), attempt).text();
    }),
    withRemoteModelTrace(context("live-after-late"), async () => {
      await lateRevocation.promise;
      await new Promise(resolve => setTimeout(resolve, 30));
      const attempt = beginModelTraceAttempt(meta);
      assert(attempt);
      await captureModelResponse(new Response("late failure survivor"), attempt).text();
      releaseLateFailure.resolve();
    }),
  ]);
  await flushModelTraceClient();
  assert(events.some(item => item.producer === "live-after-late" && item.event.type === "end" &&
    item.event.transportStatus === "completed"), "a revoked producer's late 503 must not clear another producer's queue");
  assert.equal(modelTraceClientStatus().queuedBytes, 0);
  assert.equal(modelTraceClientStatus().droppedEvents, 0, "intentional deletion is not a transport gap");
});
