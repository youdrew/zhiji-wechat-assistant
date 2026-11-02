// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Offline benchmark adapter. It never downloads, calls a provider, or reads chat stores.
const fs = require("node:fs/promises");
const { createReadStream } = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { performance } = require("node:perf_hooks");
const readline = require("node:readline");
const os = require("node:os");
const manifest = require("./models.json");
const models = Object.fromEntries(manifest.models.map(model => [model.key, model]));

function integer(value, fallback, low, high) {
  value ??= fallback;
  if (!Number.isSafeInteger(value) || value < low || value > high) throw new Error("invalid-encoder-option");
  return value;
}

async function verifyFiles(model, directory) {
  for (const file of model.files) {
    if (path.basename(file.name) !== file.name) throw new Error("invalid-model-filename");
    const filename = path.join(directory, file.name);
    const stat = await fs.lstat(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== file.bytes) throw new Error(`model-integrity:${file.name}`);
    const digest = createHash("sha256");
    for await (const block of createReadStream(filename)) digest.update(block);
    if (digest.digest("hex") !== file.sha256) throw new Error(`model-integrity:${file.name}`);
  }
}

function prefixFor(model, kind) {
  if (kind !== "query") return "";
  return model.queryTemplate.replace("{instruction}", model.queryInstruction).replace("{text}", "");
}

function tokenize(tokenizer, model, texts, kind, maxTokens) {
  const prefix = prefixFor(model, kind);
  const prefixLength = tokenizer.encode(prefix, { add_special_tokens: false }).ids.length;
  let truncatedCount = 0;
  const rows = texts.map(text => {
    // The pinned Qwen tokenizer's TemplateProcessing appends <|endoftext|>.
    // Use that processor rather than guessing from tokenizer_config.eos_token.
    const ids = tokenizer.encode(prefix + text, { add_special_tokens: true }).ids;
    const startCount = model.pooling === "cls" ? 1 + prefixLength : prefixLength;
    const finalToken = model.pooling === "cls" ? tokenizer.token_to_id("[SEP]") : tokenizer.token_to_id(model.eosToken);
    if (ids[ids.length - 1] !== finalToken || !Number.isInteger(finalToken)) throw new Error("model-special-token-mismatch");
    if (model.pooling === "cls" && ids[0] !== tokenizer.token_to_id("[CLS]")) throw new Error("model-cls-token-mismatch");
    if (ids.length <= maxTokens) return ids;
    const capacity = maxTokens - startCount - 1;
    if (capacity < 1) throw new Error("instruction-exceeds-token-budget");
    truncatedCount++;
    return [...ids.slice(0, startCount), ...ids.slice(startCount, -1).slice(-capacity), finalToken];
  });
  return { rows, tokenCounts: rows.map(row => row.length), truncatedCount };
}

function pool(output, rows, width, model) {
  if (!output || output.type !== "float32" || output.dims.length !== 3 ||
      output.dims[0] !== rows.length || output.dims[1] !== width || output.dims[2] !== model.dimensions) {
    throw new Error("encoder-output-shape-mismatch");
  }
  return rows.map((row, index) => {
    const position = model.pooling === "cls" ? 0 : row.length - 1;
    const start = (index * width + position) * model.dimensions;
    const vector = Array.from(output.data.subarray(start, start + model.dimensions));
    const norm = Math.hypot(...vector);
    if (!Number.isFinite(norm) || norm < 1e-12 || vector.some(value => !Number.isFinite(value))) {
      throw new Error("encoder-invalid-vector");
    }
    return vector.map(value => value / norm);
  });
}

async function createEncoder(key, options = {}) {
  const started = performance.now();
  const model = models[key];
  if (!model) throw new Error("unknown-benchmark-model");
  const root = path.resolve(options.root ?? path.join(__dirname, "..", ".."));
  const directory = path.resolve(root, options.cacheDir ?? model.cacheDir);
  const maxTokens = integer(options.maxTokens, manifest.defaults.maxTokens, 8, 512);
  const threads = integer(options.threads, manifest.defaults.threads, 1, 8);
  const defaultBatch = integer(options.batchSize, model.batchSize, 1, 64);
  await verifyFiles(model, directory);
  const verifyMs = performance.now() - started;
  const { Tokenizer } = require("@huggingface/tokenizers");
  const ort = require("onnxruntime-node");
  const tokenizerPackage = JSON.parse(await fs.readFile(path.resolve(
    path.dirname(require.resolve("@huggingface/tokenizers")), "..", "package.json"), "utf8"));
  const [tokenizerJson, tokenizerConfig] = await Promise.all(["tokenizer.json", "tokenizer_config.json"]
    .map(file => fs.readFile(path.join(directory, file), "utf8").then(JSON.parse)));
  const tokenizer = new Tokenizer(tokenizerJson, tokenizerConfig);
  const configFile = model.files.find(file => file.name === "config.json");
  const config = configFile ? JSON.parse(await fs.readFile(path.join(directory, "config.json"), "utf8")) : {};
  const session = await ort.InferenceSession.create(path.join(directory, model.onnx), {
    executionProviders: ["cpu"], intraOpNumThreads: threads, interOpNumThreads: 1,
    executionMode: "sequential", graphOptimizationLevel: "all", logSeverityLevel: 4,
  });
  const hiddenName = session.outputNames.find(name => name === "last_hidden_state" || name.endsWith(".last_hidden_state"));
  if (!hiddenName || !session.inputNames.includes("input_ids") || !session.inputNames.includes("attention_mask")) {
    await session.release();
    throw new Error("unsupported-embedding-graph");
  }
  const padName = typeof tokenizerConfig.pad_token === "string" ? tokenizerConfig.pad_token : tokenizerConfig.pad_token?.content;
  const pad = tokenizer.token_to_id(padName);
  if (!Number.isInteger(pad)) { await session.release(); throw new Error("missing-padding-token"); }
  let chain = Promise.resolve(), closing = false;

  async function batch(rows) {
    const width = Math.max(...rows.map(row => row.length));
    const size = rows.length * width, shape = [rows.length, width];
    const ids = new BigInt64Array(size), mask = new BigInt64Array(size), positions = new BigInt64Array(size);
    ids.fill(BigInt(pad));
    rows.forEach((row, r) => row.forEach((token, c) => {
      const index = r * width + c;
      ids[index] = BigInt(token); mask[index] = 1n; positions[index] = BigInt(c);
    }));
    const feeds = {}, owned = [];
    let outputs;
    const add = (name, type, values, dims) => {
      const tensor = new ort.Tensor(type, values, dims);
      owned.push(tensor); feeds[name] = tensor;
    };
    try {
      for (const name of session.inputNames) {
        if (name === "input_ids") add(name, "int64", ids, shape);
        else if (name === "attention_mask") add(name, "int64", mask, shape);
        else if (name === "token_type_ids") add(name, "int64", new BigInt64Array(size), shape);
        else if (name === "position_ids") add(name, "int64", positions, shape);
        else if (name === "cache_position") add(name, "int64", BigInt64Array.from({ length: width }, (_, i) => BigInt(i)), [width]);
        else if (/^past_key_values\.\d+\.(key|value)$/u.test(name)) {
          add(name, "float32", new Float32Array(0), [rows.length, config.num_key_value_heads, 0, config.head_dim]);
        } else if (name === "use_cache_branch") add(name, "bool", new Uint8Array([0]), [1]);
        else throw new Error(`unsupported-model-input:${name}`);
      }
      // Do not materialize decoder KV outputs: only hidden states are needed.
      outputs = await session.run(feeds, [hiddenName]);
      return pool(outputs[hiddenName], rows, width, model);
    } finally {
      for (const tensor of [...owned, ...Object.values(outputs ?? {})]) tensor.dispose();
    }
  }

  return {
    modelId: model.modelId, revision: model.revision, dimensions: model.dimensions,
    loadMs: performance.now() - started, verifyMs, maxTokens, threads, batchSize: defaultBatch,
    versions: { node: process.versions.node, onnxruntime: ort.env.versions.node, tokenizers: tokenizerPackage.version },
    hardware: { cpu: os.cpus()[0]?.model, logicalCpus: os.cpus().length, ramBytes: os.totalmem(),
      platform: process.platform, architecture: process.arch },
    inputNames: [...session.inputNames], outputNames: [...session.outputNames],
    encode(texts, kind = "passage", encodeOptions = {}) {
      if (closing) return Promise.reject(new Error("encoder-closed"));
      const work = async () => {
        const begin = performance.now();
        if (!["query", "passage"].includes(kind) || !Array.isArray(texts) ||
            texts.some(text => typeof text !== "string" || !text.trim() || text.length > 200000)) {
          throw new Error("invalid-benchmark-input");
        }
        if (!texts.length) return { vectors: [], tokenCounts: [], truncatedCount: 0, elapsedMs: performance.now() - begin };
        const batchSize = integer(encodeOptions.batchSize, defaultBatch, 1, 64);
        const prepared = tokenize(tokenizer, model, texts, kind, maxTokens);
        const vectors = [];
        for (let index = 0; index < prepared.rows.length; index += batchSize) {
          vectors.push(...await batch(prepared.rows.slice(index, index + batchSize)));
        }
        return { vectors, tokenCounts: prepared.tokenCounts, truncatedCount: prepared.truncatedCount,
          elapsedMs: performance.now() - begin, batchSize };
      };
      const result = chain.then(work);
      chain = result.catch(() => {});
      return result;
    },
    async close() {
      if (closing) { await chain; return; }
      closing = true;
      await chain;
      await session.release();
    },
  };
}

async function worker() {
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
  let encoder;
  try {
    for await (const line of input) {
      let request;
      try {
        request = JSON.parse(line);
        let result;
        if (request.op === "load") {
          if (encoder) await encoder.close();
          encoder = await createEncoder(request.model, request.options);
          result = Object.fromEntries(["modelId", "revision", "dimensions", "loadMs", "verifyMs", "maxTokens", "threads", "batchSize", "versions", "hardware"]
            .map(key => [key, encoder[key]]));
        } else if (request.op === "encode") {
          if (!encoder) throw new Error("encoder-not-loaded");
          result = await encoder.encode(request.texts, request.kind ?? "passage", { batchSize: request.batchSize ?? request.options?.batchSize });
        } else if (request.op === "close") {
          if (encoder) await encoder.close();
          encoder = undefined;
          process.stdout.write(JSON.stringify({ id: request.id, result: { closed: true } }) + "\n");
          break;
        } else throw new Error("unknown-worker-operation");
        process.stdout.write(JSON.stringify({ id: request.id, result }) + "\n");
      } catch (error) {
        process.stdout.write(JSON.stringify({ id: request?.id ?? null, error: { code: "encoder-error", message: error.message } }) + "\n");
      }
    }
  } finally {
    input.close();
    if (encoder) await encoder.close();
  }
}

module.exports = { createEncoder, models, tokenize, pool };
if (require.main === module) worker().catch(() => { process.exitCode = 1; });
