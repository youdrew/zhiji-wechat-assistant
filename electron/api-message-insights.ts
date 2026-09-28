// API-mode per-message insight extraction. Local Laya analysis is a separate path.
// Chat text is untrusted data; no model output is used without exact structural checks.

import {
  generateStructured,
  type GenerationResult, type ModelConfig, type ModelUsage,
} from "./model-connectors";
import {
  charCount, decodeJsonOutput, inputError, outputError, validId,
} from "./api-analysis-json";

/** Complementary affect views. Any field may be omitted when it has no evidence. */
export interface ApiAffect {
  tone?: string;
  feeling?: string;
  interaction?: string;
}

export type ApiInsight = {
  id: string;
  status: "ok";
  affect?: ApiAffect;
  intents: string[];
} | {
  id: string;
  status: "routine" | "uncertain" | "insufficient";
};

export interface ApiInsightMessage {
  id: string;
  sender: "SELF" | "OTHER";
  text: string;
  portraitContext?: string;
  // Optional local-only unified-input metadata. It is accepted for the shared
  // contract but intentionally never read into a provider window or prompt.
  inputMeta?: import("../shared/message-input").MessageInputMeta;
}

export interface ApiInsightInput {
  messages: ApiInsightMessage[];
  targetIds: string[];
}

export interface ApiInsightsResult {
  insights: ApiInsight[];
  usage?: ModelUsage;
  responseId?: string;
  timings?: { firstBodyMs?: number; connectorMs?: number; parseMs?: number };
}

type Generator = typeof generateStructured;
export type ApiTextDeltaHandler = (delta: string) => void;

// Keep the bounded API experiment in one provider request. The bridge applies
// the same cap; this guard prevents accidental fragmentation at the analyzer.
const MAX_TARGETS = 500;
const MAX_MESSAGES = 512;
const MAX_CONTEXT_MESSAGES = 3;
const MAX_CHAT_CHARACTERS = 600_000;
const AFFECT_KEYS = ["tone", "feeling", "interaction"] as const;
const MAX_INTENTS = 1;
const MAX_LABEL_CHARACTERS = 4;

interface Window {
  target: { id: string; text: string; portraitContext?: string };
  context: Array<{ sender: "SELF" | "OTHER"; text: string }>;
}

function terminal(id: string, status: "routine" | "uncertain" | "insufficient"): ApiInsight {
  return { id, status };
}

function insufficient(id: string): ApiInsight {
  return terminal(id, "insufficient");
}

function prepare(input: ApiInsightInput): {
  windows: Window[];
  messages: Array<{ id: string; sender: "SELF" | "OTHER"; text: string; portraitContext?: string }>;
  eligibleIds: string[];
  emptyIds: Set<string>;
} {
  if (!input || !Array.isArray(input.messages) || !Array.isArray(input.targetIds) ||
      input.messages.length > MAX_MESSAGES || input.targetIds.length > MAX_TARGETS) inputError();
  const byId = new Map<string, number>();
  let rawCharacters = 0;
  for (const [index, message] of input.messages.entries()) {
    if (!message || !validId(message.id) || byId.has(message.id) ||
        (message.sender !== "SELF" && message.sender !== "OTHER") ||
        typeof message.text !== "string" ||
        (message.portraitContext !== undefined &&
          (message.sender !== "OTHER" || typeof message.portraitContext !== "string" ||
           charCount(message.portraitContext) > 80 ||
           /[\u0000-\u001f\u007f]/u.test(message.portraitContext)))) inputError();
    rawCharacters += charCount(message.text) + charCount(message.portraitContext || "");
    if (rawCharacters > MAX_CHAT_CHARACTERS) inputError();
    byId.set(message.id, index);
  }
  const seenTargets = new Set<string>();
  const windows: Window[] = [];
  const eligibleIds: string[] = [];
  const emptyIds = new Set<string>();
  let sentCharacters = 0;
  for (const id of input.targetIds) {
    if (!validId(id) || seenTargets.has(id)) inputError();
    seenTargets.add(id);
    const index = byId.get(id);
    if (index === undefined || input.messages[index]!.sender !== "OTHER") inputError();
    const target = input.messages[index]!;
    if (!target.text.trim()) {
      emptyIds.add(id);
      continue;
    }
    const context = input.messages.slice(Math.max(0, index - MAX_CONTEXT_MESSAGES), index)
      .map((message) => ({ sender: message.sender, text: message.text }));
    sentCharacters += charCount(target.text) + charCount(target.portraitContext || "");
    for (const message of context) sentCharacters += charCount(message.text);
    if (sentCharacters > MAX_CHAT_CHARACTERS) inputError();
    windows.push({ target: { id, text: target.text,
      ...(target.portraitContext ? { portraitContext: target.portraitContext } : {}) }, context });
    eligibleIds.push(id);
  }
  const messages = input.messages.map((message) => ({
    id: message.id, sender: message.sender, text: message.text,
    ...(message.portraitContext ? { portraitContext: message.portraitContext } : {}),
  }));
  return { windows, messages, eligibleIds, emptyIds };
}

const SYSTEM = [
  "人物：聊天。给聊天打上一个情感、一个意图标签，每个分别一个，限制 4 字以内。",
  "输出为：\n姓名：\n聊天内容：\n情感：\n意图：",
].join("\n");

/** Extract the first short Han phrase from a model field or a noisy text line. */
function shortLabel(value: unknown): string {
  if (typeof value !== "string") outputError();
  const text = value.trim().replace(/^(?:情绪|意图|语气|感受|互动)\s*[:：]\s*/u, "")
    .replace(/^[“"'「『【]+|[”"'」』】]+$/gu, "")
    .replace(/[。！？!？，,；;]+$/u, "").trim();
  const match = text.match(new RegExp(`\\p{Script=Han}{1,${MAX_LABEL_CHARACTERS}}`, "u"));
  if (!match) outputError();
  return match[0]!;
}

function normalizeAffect(value: unknown): ApiAffect | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "object" || Array.isArray(value)) outputError();
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!(AFFECT_KEYS as readonly string[]).includes(key)) outputError();
  }
  const affect: ApiAffect = {};
  for (const key of ["feeling", "tone", "interaction"] as const) {
    const raw = record[key];
    if (raw === undefined || raw === null) continue;
    affect.feeling = shortLabel(raw);
    break;
  }
  return Object.keys(affect).length ? affect : undefined;
}

function normalizeIntents(value: unknown): string[] {
  if (value === undefined || value === null) return [];
  const values = typeof value === "string" ? [value] : value;
  if (!Array.isArray(values)) outputError();
  const first = values.slice(0, MAX_INTENTS)
    .find((raw) => typeof raw === "string" && raw.trim());
  return first === undefined ? [] : [shortLabel(first)];
}

function normalizeItem(value: unknown): ApiInsight {
  if (!value || typeof value !== "object" || Array.isArray(value)) outputError();
  const draft = value as Record<string, unknown>;
  const id = draft.id;
  if (!validId(id)) outputError();
  const status = draft.status ?? "ok";
  if (status === "routine" || status === "uncertain" || status === "insufficient") {
    const affect = draft.affect;
    const intents = draft.intents;
    if (affect !== undefined && affect !== null &&
        (typeof affect !== "object" || Array.isArray(affect) ||
         Object.keys(affect as object).length)) outputError();
    if (intents !== undefined && intents !== null &&
        (!Array.isArray(intents) || intents.length)) outputError();
    return terminal(id, status);
  }
  if (status !== "ok") outputError();
  if (draft.affect !== undefined || draft.intents !== undefined) {
    const affect = normalizeAffect(draft.affect);
    const intents = normalizeIntents(draft.intents);
    return { id, status: "ok", ...(affect ? { affect } : {}), intents };
  }
  // The simple text contract uses scalar emotion/intent fields. Either field may be
  // absent when the program cannot find it in a noisy provider response.
  const rawEmotion = typeof draft.emotion === "string" ? draft.emotion : draft.emotionLabel;
  const rawIntent = typeof draft.intent === "string" ? draft.intent : draft.intentLabel;
  if (typeof rawEmotion === "string" || typeof rawIntent === "string") {
    const feeling = typeof rawEmotion === "string" ? shortLabel(rawEmotion) : undefined;
    const intent = typeof rawIntent === "string" ? shortLabel(rawIntent) : undefined;
    return { id, status: "ok", ...(feeling ? { affect: { feeling } } : {}),
      intents: intent ? [intent] : [] };
  }
  outputError();
}

type LooseLabels = { emotion?: string; intent?: string };

function jsonValues(raw: string): unknown[] {
  const values: unknown[] = [];
  const seen = new Set<string>();
  const add = (text: string) => {
    if (seen.has(text)) return;
    try {
      values.push(JSON.parse(text));
      seen.add(text);
    } catch { /* The surrounding prose may contain incomplete JSON. */ }
  };
  try { values.push(decodeJsonOutput(raw)); } catch { /* Fall through to fragments. */ }
  for (let start = 0; start < raw.length; start++) {
    if (raw[start] !== "{" && raw[start] !== "[") continue;
    let depth = 0;
    let quote = false;
    let escaped = false;
    for (let index = start; index < raw.length; index++) {
      const character = raw[index]!;
      if (quote) {
        if (escaped) escaped = false;
        else if (character === "\\") escaped = true;
        else if (character === '"') quote = false;
        continue;
      }
      if (character === '"') { quote = true; continue; }
      if (character === "{" || character === "[") depth++;
      else if (character === "}" || character === "]") depth--;
      if (depth === 0) { add(raw.slice(start, index + 1)); break; }
    }
  }
  return values;
}

function jsonItems(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [];
  const root = value as Record<string, unknown>;
  if (Array.isArray(root.items)) return root.items;
  if (Array.isArray(root.results)) return root.results;
  return root.id !== undefined ? [root] : [];
}

function looseLabels(raw: string): LooseLabels[] {
  // Ignore the common trailing overall-summary block shown by the provider. The
  // parser deliberately keeps the leading thought/formatting text harmless.
  const summary = raw.search(/(?:^|\n)\s*(?:整体|总体)?总结\s*[:：]?/u);
  const body = (summary >= 0 ? raw.slice(0, summary) : raw).replace(/\r/g, "");
  const emotions = [...body.matchAll(/(?:情感|情绪|emotion)\s*[:：]\s*([^\n\r,，;}]+)/giu)]
    .map((match) => match[1]).map((value) => {
      try { return shortLabel(value); } catch { return undefined; }
    });
  const intents = [...body.matchAll(/(?:意图|intent)\s*[:：]\s*([^\n\r,，;}]+)/giu)]
    .map((match) => match[1]).map((value) => {
      try { return shortLabel(value); } catch { return undefined; }
    });
  const count = Math.max(emotions.length, intents.length);
  return Array.from({ length: count }, (_, index) => ({
    ...(emotions[index] ? { emotion: emotions[index] } : {}),
    ...(intents[index] ? { intent: intents[index] } : {}),
  }));
}

/**
 * Provider output is intentionally treated as text. JSON, Markdown fences,
 * leading thoughts, and trailing summaries are all accepted; only the first
 * short Chinese phrase following each 情感/意图 marker is retained.
 */
function parseOutput(
  raw: GenerationResult, eligibleIds: readonly string[],
): Map<string, ApiInsight> {
  const text = typeof raw.text === "string" ? raw.text : "";
  const allowed = new Set(eligibleIds);
  const result = new Map<string, ApiInsight>();
  const unnamed: ApiInsight[] = [];
  for (const value of jsonValues(text).flatMap(jsonItems)) {
    try {
      const insight = normalizeItem(value);
      if (allowed.has(insight.id) && !result.has(insight.id)) result.set(insight.id, insight);
      else if (!allowed.has(insight.id)) unnamed.push(insight);
    } catch { /* Keep scanning other JSON fragments and the text markers. */ }
  }
  for (const labels of looseLabels(text)) {
    try {
      const insight = normalizeItem({ id: "__loose__", status: "ok", ...labels });
      unnamed.push(insight);
    } catch { /* A malformed line is simply ignored. */ }
  }
  const missing = () => eligibleIds.find((id) => !result.has(id));
  for (const insight of unnamed) {
    const id = missing();
    if (!id) break;
    result.set(id, { ...insight, id });
  }
  for (const id of eligibleIds) {
    if (!result.has(id)) result.set(id, { id, status: "ok", intents: [] });
  }
  return result;
}

/** Returns one strictly validated insight per OTHER target, in requested order. */
export async function analyzeApiInsights(
  config: ModelConfig,
  input: ApiInsightInput,
  generate: Generator = generateStructured,
  onTextDelta?: ApiTextDeltaHandler,
): Promise<ApiInsightsResult> {
  const { messages, eligibleIds, emptyIds } = prepare(input);
  if (input.targetIds.length === 0) return { insights: [] };
  if (eligibleIds.length === 0) {
    return { insights: input.targetIds.map((id) => insufficient(id)) };
  }
  const response = await generate(config, {
    system: SYSTEM,
    prompt: `CHAT_BATCH_JSON:\n${JSON.stringify({ messages, targetIds: eligibleIds })}`,
    jsonMode: false,
    stream: true,
    ...(onTextDelta ? { onTextDelta } : {}),
  });
  const parseStarted = performance.now();
  const parsed = parseOutput(response, eligibleIds);
  const insights = input.targetIds.map((id) => emptyIds.has(id) ? insufficient(id) : parsed.get(id)!);
  const timings = response.timings ? { ...response.timings, parseMs: performance.now() - parseStarted } : undefined;
  return { insights, ...(response.usage ? { usage: response.usage } : {}),
    ...(response.responseId ? { responseId: response.responseId } : {}),
    ...(timings ? { timings } : {}) };
}
