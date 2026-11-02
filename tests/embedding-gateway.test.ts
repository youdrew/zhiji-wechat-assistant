// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import test from "node:test";
import assert from "node:assert/strict";
import { ModelGateway } from "../electron/model-gateway";

test("local CPU embeddings use fair gateway admission and prioritize queued queries", async () => {
  const gateway = new ModelGateway(() => {});
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const order: string[] = [];
  const first = gateway.runLocalEmbedding(async () => { order.push("first"); await held; }, "indexer", false);
  await Promise.resolve();
  const background = gateway.runLocalEmbedding(async () => { order.push("background"); }, "indexer", false);
  const query = gateway.runLocalEmbedding(async () => { order.push("query"); }, "suggestions", true);
  assert.deepEqual(order, ["first"]);
  release();
  await Promise.all([first, background, query]);
  assert.deepEqual(order, ["first", "query", "background"]);
  await gateway.close();
});

test("cancelled embedding tickets do not run or take the next CPU slot", async () => {
  const gateway = new ModelGateway(() => {});
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const first = gateway.runLocalEmbedding(() => held, "indexer", false);
  await Promise.resolve();
  const controller = new AbortController();
  const cancelled = gateway.runLocalEmbedding(async () => { throw new Error("must-not-run"); }, "suggestions", true, controller.signal);
  controller.abort();
  await assert.rejects(cancelled, /queue-aborted/);
  release(); await first;
  assert.equal(await gateway.runLocalEmbedding(async () => "next", "indexer", false), "next");
  await gateway.close();
});
