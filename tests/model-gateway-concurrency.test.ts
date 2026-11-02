// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { it } from "node:test";
import { ModelGateway, tokenHash, type GatewayService } from "../electron/model-gateway";
import { ServiceQueue } from "../electron/model-gateway-queue";

async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 5000;
  while (!check()) {
    assert.ok(Date.now() < deadline, "expected concurrent requests to reach the provider");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

it("shares the configured capacity across plugins and applies edits without cancelling running work", async () => {
  const queue = new ServiceQueue();
  queue.configure(2);
  const releases = [await queue.acquire("analysis", false), await queue.acquire("analysis", false)];
  let thirdStarted = false;
  const third = queue.acquire("advisor", true).then(release => { thirdStarted = true; return release; });
  await Promise.resolve();
  assert.equal(thirdStarted, false);
  queue.configure(3);
  releases.push(await third);
  assert.equal(queue.stats().running, 3);

  queue.configure(1);
  let nextStarted = false;
  const next = queue.acquire("import", false).then(release => { nextStarted = true; return release; });
  releases[0]!(); releases[1]!();
  await Promise.resolve();
  assert.equal(nextStarted, false, "lowering the limit waits for all excess work to finish");
  assert.equal(queue.stats().running, 1);
  releases[2]!();
  const nextRelease = await next;
  nextRelease();
  assert.deepEqual(queue.stats(), { running: 0, waiting: 0, queuedPlugins: 0, runningPlugins: 0 });
});

it("lets one plugin use all eight shared slots without hidden plugin quotas", async () => {
  const queue = new ServiceQueue(8);
  const releases = await Promise.all(Array.from({ length: 8 }, () => queue.acquire("analysis", false)));
  assert.equal(queue.stats().running, 8);
  releases.forEach(release => release());
});

it("rejects invalid core capacity without changing the current gateway configuration", () => {
  const gateway = new ModelGateway(() => {});
  const service = { id: "synthetic", protocol: "chat_completions", model: "test-model", baseUrl: "http://127.0.0.1/v1", concurrency: 2 };
  gateway.configure([service]);
  for (const concurrency of [0, 9, 1.5, NaN, Infinity]) {
    assert.throws(() => gateway.configure([{ ...service, concurrency }]), /invalid-model-concurrency/u);
    assert.equal(gateway.services.get(service.id), service);
  }
});

it("limits actual requests across plugins and applies capacity edits without invalidating queued requests", async () => {
  const received: http.ServerResponse[] = [];
  const provider = http.createServer((req, res) => { req.resume(); received.push(res); });
  await new Promise<void>(resolve => provider.listen(0, "127.0.0.1", resolve));
  const gateway = new ModelGateway(() => {});
  const service: GatewayService = { id: "synthetic", protocol: "chat_completions", model: "test-model",
    baseUrl: `http://127.0.0.1:${(provider.address() as AddressInfo).port}/v1`, concurrency: 2 };
  gateway.configure([service]);
  const port = await gateway.listen();
  const requests: Array<Promise<number>> = [];
  const finish = (res: http.ServerResponse) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"choices":[{"message":{"content":"ok"}}]}');
  };
  const send = (index: number) => {
    const token = `synthetic-${index}`;
    gateway.addToken(tokenHash(token), { plugin: index % 2 ? "test.advisor" : "test.analysis",
      task: `task-${index}`, execution: `exec-${index}` });
    const request = fetch(`http://127.0.0.1:${port}/s/synthetic/chat/completions`, {
      method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ model: "test-model", messages: [] }),
    }).then(async response => { await response.text(); return response.status; });
    requests.push(request);
  };
  try {
    for (let index = 0; index < 8; index++) send(index);
    await until(() => received.length === 2);
    gateway.configure([{ ...service, concurrency: 8 }]);
    await until(() => received.length === 8);
    assert.ok(received.every(res => !res.writableEnded), "all eight requests overlap at the provider");

    gateway.configure([{ ...service, concurrency: 1 }]);
    send(8);
    for (const res of received.slice(0, 7)) finish(res);
    await Promise.all(requests.slice(0, 7));
    assert.equal(received.length, 8, "lowering capacity does not admit more work while a request is running");
    finish(received[7]!);
    await until(() => received.length === 9);
    finish(received[8]!);
    assert.deepEqual(await Promise.all(requests), Array(9).fill(200));
  } finally {
    for (const res of received) if (!res.writableEnded) finish(res);
    await gateway.close();
    await Promise.allSettled(requests);
    await new Promise<void>(resolve => provider.close(() => resolve()));
  }
});
