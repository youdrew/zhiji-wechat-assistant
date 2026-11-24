// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Independent, bounded diagnostic transport: never share the business JSONL pipe.
import { configureModelTraceProducer, withModelTraceContext, modelTraceOutcome, type ModelTraceContext } from "./model-traces";
import { Agent, fetch as diagnosticFetch } from "undici";

type RemoteContext = ModelTraceContext & {
  endpoint?: string; collecting?: boolean; enabled?: boolean;
  bufferBudgetBytes?: number;
  collectionVersion: number; deleteVersion: number; producerId: string;
};
type CaptureState = { collecting: boolean; collectionVersion: number; deleteVersion: number; retentionCutoffAt?: number };
type Registration = { context: RemoteContext; users: number; lastUsed: number; polling: boolean; allowed: boolean; revoked?: boolean };
const MAX_QUEUE_BYTES = 2 * 1024 * 1024;
const MAX_ENVELOPE_BYTES = 64 * 1024;
const MAX_REGISTRATIONS = 64;
// Core accepts {"events": [...]} up to 64 events in a 64 KiB body and closes the
// connection after every response. One request per stream delta opened thousands
// of loopback connections per second, which crashes Node 24 on Windows natively
// (0xC0000409, no stderr), so events are sent in batches at a bounded rate.
const MAX_BATCH_EVENTS = 64;
const MAX_BATCH_BYTES = 60 * 1024;
const INGEST_INTERVAL_MS = 50;
// Bound concurrent control/ingest connections even for ten simultaneous tasks.
const dispatcher = new Agent({ connections: 2, pipelining: 1 });
const registrations = new Map<string, Registration>();
let state: CaptureState = { collecting: false, collectionVersion: 0, deleteVersion: 0 };
let stateRevision = -1;
let queue: { producer: string; body: string; bytes: number }[] = [];
let queuedBytes = 0;
let sending = false;
let lastIngestAt = 0;
let timer: ReturnType<typeof setInterval> | undefined;
let configured = false;
let droppedEvents = 0;
let lastTransportError = "";
export function modelTraceClientStatus() { return { queuedBytes, droppedEvents, lastTransportError }; }

class TraceProducerRevoked extends Error {}

function revoke(registration: Registration): void {
  registration.allowed = false;
  registration.revoked = true;
  // Scope deletion is intentional. Discard only this producer's pending data;
  // unrelated tasks in the same worker retain their collection state.
  queue = queue.filter(item => {
    if (item.producer !== registration.context.producerId) return true;
    queuedBytes -= item.bytes;
    return false;
  });
}

function validEndpoint(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && Boolean(url.port) &&
      !url.username && !url.password && !url.search && !url.hash && url.pathname === "/";
  } catch { return false; }
}

async function request(context: RemoteContext, path: string, body?: string): Promise<any> {
  const response = await diagnosticFetch(`${context.endpoint}/api/model-traces/${path}`, {
    method: body === undefined ? "GET" : "POST", redirect: "error",
    headers: { "X-Model-Trace-Producer": context.producerId, "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body }), signal: AbortSignal.timeout(2000), dispatcher,
  });
  if (!response.ok) {
    await response.body?.cancel();
    if (response.status === 403 || response.status === 409) throw new TraceProducerRevoked();
    throw new Error("trace-channel-unavailable");
  }
  return response.json();
}

function applyState(next: any): void {
  if (!next || !Number.isSafeInteger(next.collectionVersion) || !Number.isSafeInteger(next.deleteVersion)) return;
  if (next.collectionVersion < state.collectionVersion) return;
  if (next.collectionVersion === state.collectionVersion && Number.isSafeInteger(next.revision) && next.revision < stateRevision) return;
  stateRevision = Number.isSafeInteger(next.revision) ? next.revision : stateRevision;
  state = { collecting: next.collecting === true, collectionVersion: next.collectionVersion,
    deleteVersion: next.deleteVersion,
    ...(Number.isFinite(next.retentionCutoffAt) ? { retentionCutoffAt: next.retentionCutoffAt } : {}) };
}

async function poll(registration: Registration): Promise<void> {
  if (registration.polling || registration.revoked) return;
  registration.polling = true;
  try {
    const next = await request(registration.context, "producer-state");
    if (registration.revoked) return;
    registration.allowed = next.collecting === true && next.bufferBudgetBytes !== 0;
    applyState(next);
    await request(registration.context, "ack", JSON.stringify({ collectionVersion: next.collectionVersion,
      ...(droppedEvents ? { lostEvents: droppedEvents, reason: "诊断传输中断，部分记录未保存" } : {}) }));
  } catch (error) {
    if (registration.revoked) return;
    if (error instanceof TraceProducerRevoked) revoke(registration);
    else state = { ...state, collecting: false };
  } finally { registration.polling = false; }
}

async function drain(): Promise<void> {
  if (sending) return;
  sending = true;
  try {
    while (queue.length) {
      // A full batch goes at once; a partial one waits briefly so a delta stream
      // costs at most one request per interval, or one per full batch when faster.
      const wait = lastIngestAt + INGEST_INTERVAL_MS - Date.now();
      if (wait > 0 && queue.length < MAX_BATCH_EVENTS && queuedBytes < MAX_BATCH_BYTES) {
        await new Promise(resolve => setTimeout(resolve, wait));
        if (!queue.length) break;
      }
      lastIngestAt = Date.now();
      // Consecutive events of one producer share a request; order is unchanged.
      const producer = queue[0].producer;
      const batch = [queue.shift()!];
      let batchBytes = batch[0].bytes;
      while (queue.length && queue[0].producer === producer && batch.length < MAX_BATCH_EVENTS &&
          batchBytes + queue[0].bytes + 1 <= MAX_BATCH_BYTES) {
        const next = queue.shift()!;
        batch.push(next); batchBytes += next.bytes + 1;
      }
      const sent = batch.reduce((total, item) => total + item.bytes, 0);
      const registration = registrations.get(producer);
      try {
        if (registration) await request(registration.context, "ingest",
          batch.length === 1 ? batch[0].body : `{"events":[${batch.map(item => item.body).join(",")}]}`);
      } catch (error) {
        if (registration?.revoked) continue;
        if (error instanceof TraceProducerRevoked && registration) {
          revoke(registration);
          continue;
        }
        lastTransportError = error instanceof Error ? `${error.name}:${(error.cause as { code?: string })?.code ?? "channel"}` : "channel";
        droppedEvents += queue.length + batch.length;
        state = { ...state, collecting: false };
        // A broken diagnostic link cannot replay business requests. The backend
        // independently expires incomplete records; discard this bounded queue.
        queue = []; queuedBytes = sent;
      } finally { queuedBytes -= sent; }
    }
  } finally { sending = false; }
}

function configure(): void {
  if (configured) return;
  configured = true;
  configureModelTraceProducer({
    state: context => ({ ...state, collecting: state.collecting &&
      (!context || (registrations.get(context.producerId)?.allowed === true &&
        !registrations.get(context.producerId)?.revoked)) }),
    emit: (context, event) => {
      const producer = context.producerId;
      if (!producer || !registrations.has(producer)) return false;
      // The capture module sanitizes before this first diagnostic serialization.
      const body = JSON.stringify(event);
      const bytes = Buffer.byteLength(body);
      if (bytes > MAX_ENVELOPE_BYTES || queuedBytes + bytes > MAX_QUEUE_BYTES || queue.length >= 512) {
        droppedEvents++;
        state = { ...state, collecting: false };
        return false;
      }
      queue.push({ producer, body, bytes }); queuedBytes += bytes;
      void drain();
      return true;
    },
  });
}

export async function withRemoteModelTrace<T>(value: unknown, work: () => Promise<T>): Promise<T> {
  configure();
  const context = value as RemoteContext | null;
  if (!context || !validEndpoint(context.endpoint) || typeof context.producerId !== "string" ||
      !context.taskId || !context.executionId) return work();
  let registration = registrations.get(context.producerId);
  if (!registration) {
    if (registrations.size >= MAX_REGISTRATIONS) return work();
    registration = { context, users: 0, lastUsed: Date.now(), polling: false,
      allowed: context.collecting === true && context.bufferBudgetBytes !== 0 };
    registrations.set(context.producerId, registration);
    applyState(context);
  }
  registration.users++;
  void poll(registration);
  if (!timer) {
    timer = setInterval(() => {
      for (const [key, item] of registrations) {
        if (!item.users && Date.now() - item.lastUsed > 4000 && !queue.some(entry => entry.producer === key)) {
          registrations.delete(key);
        } else void poll(item);
      }
      if (!registrations.size) { clearInterval(timer); timer = undefined; }
    }, 250);
    timer.unref();
  }
  try { return await withModelTraceContext(context, async () => {
    try { return await work(); }
    catch (error) {
      const code = (error as { code?: unknown })?.code;
      modelTraceOutcome("failed", { errorCode: typeof code === "string" && /^[a-z0-9:_-]{1,80}$/i.test(code)
        ? code : "application-processing-failed" }, "unknown");
      throw error;
    }
  }); }
  finally { registration.users--; registration.lastUsed = Date.now(); }
}

export async function flushModelTraceClient(): Promise<void> {
  const deadline = Date.now() + 1500;
  while ((queue.length || sending) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}
