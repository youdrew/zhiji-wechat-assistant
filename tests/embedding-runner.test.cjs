// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const test = require("node:test");
const assert = require("node:assert/strict");
const { tokenRows, clsVectors, validateRequest } = require("../scripts/embedding-runner.cjs");
const manifest = require("../scripts/embedding-model.json");

test("chat truncation preserves the latest turn and BGE special tokens", () => {
  const tokenizer = {
    token_to_id: token => token === "[CLS]" ? 101 : 102,
    encode: text => ({ ids: text === manifest.queryInstruction ? [7, 8] : Array.from({ length: 900 }, (_, i) => i + 1000) }),
  };
  const [query] = tokenRows(tokenizer, ["long conversation"], "query");
  assert.equal(query.length, 512);
  assert.deepEqual(query.slice(0, 3), [101, 7, 8]);
  assert.deepEqual(query.slice(-2), [1899, 102]);
  const [passage] = tokenRows(tokenizer, ["long conversation"], "passage");
  assert.equal(passage.length, 512);
  assert.equal(passage[1], 1390);
});

test("BGE pooling uses CLS per batch row and normalizes, never averages tokens", () => {
  const vectors = clsVectors({ dims: [2, 2, 2], data: new Float32Array([3, 4, 900, 900, 0, 2, 800, 800]) }, 2, 2);
  assert.deepEqual(vectors, [[0.6, 0.8], [0, 1]]);
  assert.throws(() => clsVectors({ dims: [1, 1, 2], data: new Float32Array([0, 0]) }, 1, 2), /output-invalid/);
  assert.throws(() => clsVectors({ dims: [1, 1, 2], data: [NaN, 2] }, 1, 2), /output-invalid/);
  assert.throws(() => clsVectors({ dims: [1, 2], data: [1, 2] }, 1, 2), /output-invalid/);
});

test("embedding accepts bounded local batches and rejects invalid inputs", () => {
  validateRequest(["最近心情很差🥹"], "query");
  validateRequest(["字".repeat(4000)], "query");
  for (const texts of [[], [" "], [null], ["字".repeat(4001)], Array(17).fill("你好")]) {
    assert.throws(() => validateRequest(texts, "passage"), /invalid-request/);
  }
  assert.throws(() => validateRequest(["你好"], "remote"), /invalid-request/);
});

test("pinned model assets use domestic HTTPS sources and complete SHA-256 identities", () => {
  assert.equal(manifest.dimensions, 512);
  assert.equal(manifest.pooling, "cls");
  assert.equal(manifest.files.find(file => file.name === "model.onnx").bytes, 94851877);
  for (const file of manifest.files) {
    const url = new URL(file.url);
    assert.equal(url.protocol, "https:");
    assert.ok(manifest.allowedHosts.includes(url.hostname));
    assert.match(file.sha256, /^[a-f0-9]{64}$/);
    assert.ok(Number.isSafeInteger(file.bytes) && file.bytes > 0);
    assert.ok(!file.name.includes("/") && !file.name.includes("\\"));
  }
});
