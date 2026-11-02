// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Local files only. Core verifies the pinned ModelScope assets before admission.
const fs = require("node:fs/promises");
const path = require("node:path");
const manifest = require("./embedding-model.json");

function cancelled(signal) {
  if (signal?.aborted) throw new Error("embedding-cancelled");
}

function validateRequest(texts, kind) {
  if (!["passage", "query"].includes(kind) || !Array.isArray(texts) || !texts.length || texts.length > 16 ||
      texts.some(text => typeof text !== "string" || !text.trim() || Array.from(text).length > 4000 || text.length > 8000)) {
    throw new Error("embedding-invalid-request");
  }
}

function tokenRows(tokenizer, texts, kind) {
  const cls = tokenizer.token_to_id("[CLS]"), sep = tokenizer.token_to_id("[SEP]");
  if (!Number.isInteger(cls) || !Number.isInteger(sep)) throw new Error("embedding-tokenizer-invalid");
  const prefix = kind === "query" ? tokenizer.encode(manifest.queryInstruction, { add_special_tokens: false }).ids : [];
  const capacity = manifest.maxTokens - prefix.length - 2;
  // Retain the most recent chat turn, with query instruction and CLS/SEP intact.
  return texts.map(text => [cls, ...prefix,
    ...tokenizer.encode(text, { add_special_tokens: false }).ids.slice(-capacity), sep]);
}

function clsVectors(output, count, dimensions) {
  const shape = output?.dims;
  if (!Array.isArray(shape) || shape.length !== 3 || shape[0] !== count || shape[2] !== dimensions || shape[1] < 1) {
    throw new Error("embedding-output-invalid");
  }
  return Array.from({ length: count }, (_, row) => {
    const start = row * shape[1] * dimensions;
    const vector = Array.from(output.data.slice(start, start + dimensions));
    const norm = Math.hypot(...vector);
    if (!Number.isFinite(norm) || norm < 1e-12 || vector.some(value => !Number.isFinite(value))) {
      throw new Error("embedding-output-invalid");
    }
    return vector.map(value => value / norm);
  });
}

async function createEncoder(modelDir) {
  const { Tokenizer } = require("@huggingface/tokenizers");
  const ort = require("onnxruntime-node");
  const [json, config] = await Promise.all(["tokenizer.json", "tokenizer_config.json"]
    .map(name => fs.readFile(path.join(modelDir, name), "utf8").then(JSON.parse)));
  const tokenizer = new Tokenizer(json, config);
  const session = await ort.InferenceSession.create(path.join(modelDir, "model.onnx"), {
    executionProviders: ["cpu"], intraOpNumThreads: 2, interOpNumThreads: 1,
    executionMode: "sequential", graphOptimizationLevel: "all", logSeverityLevel: 4,
  });
  if (!session.inputNames.includes("input_ids") || !session.inputNames.includes("attention_mask")) {
    await session.release();
    throw new Error("embedding-model-invalid");
  }
  return {
    async encode(texts, kind = "passage", signal) {
      validateRequest(texts, kind);
      cancelled(signal);
      const rows = tokenRows(tokenizer, texts, kind), width = Math.max(...rows.map(row => row.length));
      const ids = new BigInt64Array(rows.length * width), mask = new BigInt64Array(ids.length);
      for (let row = 0; row < rows.length; row++) {
        for (let col = 0; col < rows[row].length; col++) {
          ids[row * width + col] = BigInt(rows[row][col]); mask[row * width + col] = 1n;
        }
      }
      const shape = [rows.length, width];
      const feeds = { input_ids: new ort.Tensor("int64", ids, shape), attention_mask: new ort.Tensor("int64", mask, shape) };
      if (session.inputNames.includes("token_type_ids")) feeds.token_type_ids = new ort.Tensor("int64", new BigInt64Array(ids.length), shape);
      const output = await session.run(feeds);
      cancelled(signal);
      // BGE v1.5 uses the last layer's CLS token, not mean or pooler_output.
      return clsVectors(output.last_hidden_state ?? output[session.outputNames[0]], rows.length, manifest.dimensions);
    },
    close: () => session.release(),
  };
}

module.exports = { createEncoder, validateRequest, tokenRows, clsVectors };
