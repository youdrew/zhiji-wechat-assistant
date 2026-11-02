// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import { Agent } from "undici";

export const DEFAULT_MODEL_TIMEOUT_MS = 3_600_000;
export const MIN_MODEL_TIMEOUT_MS = 60_000;
export const MAX_MODEL_TIMEOUT_MS = 10_800_000;

/** Saved inference limit. Missing values keep existing installations usable. */
export function modelTimeoutMs(value?: unknown): number {
  if (value === undefined || value === null) return DEFAULT_MODEL_TIMEOUT_MS;
  if (typeof value !== "number" || !Number.isSafeInteger(value) ||
      value < MIN_MODEL_TIMEOUT_MS || value > MAX_MODEL_TIMEOUT_MS) throw new Error("invalid-model-timeout");
  return value;
}

// Node fetch otherwise applies a five-minute headers/body timeout beneath the
// caller's AbortSignal. A slow prefill or thinking gap must obey the user limit.
// Every caller supplies its own bounded signal; discovery/probes keep short ones.
const modelDispatcher = new Agent({ headersTimeout: 0, bodyTimeout: 0 });

export function modelRequestInit(init: RequestInit): RequestInit {
  return { ...init, dispatcher: modelDispatcher } as RequestInit;
}
