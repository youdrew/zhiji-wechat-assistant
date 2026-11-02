// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The gateway owns the process and its random credential. Neither is given to plugins.
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createServer, type AddressInfo } from "node:net";
import { constants, setPriority } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export interface LocalJudgeConfig {
  modelPath: string;
  runtimePath: string;
  contextTokens?: number;
  threads?: number;
  idleMs?: number;
}
export interface LocalJudgeEndpoint { baseUrl: string; apiKey: string; }

interface OwnedProcess extends LocalJudgeEndpoint {
  child: ChildProcess;
  key: string;
  exited: Promise<void>;
}

export class LocalJudgeRuntime {
  private owned?: OwnedProcess;
  private starting?: Promise<LocalJudgeEndpoint>;
  private startingKey?: string;
  private startup?: AbortController;
  private idle?: NodeJS.Timeout;
  private closed = false;

  constructor(private readonly fetcher: typeof fetch = globalThis.fetch,
    private readonly launcher: typeof spawn = spawn) {}

  alive(): boolean { return !!this.owned && this.owned.child.exitCode === null && !this.owned.child.killed; }

  async resolve(config: LocalJudgeConfig, signal: AbortSignal): Promise<LocalJudgeEndpoint> {
    if (this.closed || signal.aborted) throw new Error("judge-unavailable");
    if (!path.isAbsolute(config.modelPath) || !path.isAbsolute(config.runtimePath) ||
        path.basename(config.runtimePath).toLowerCase() !== "llama-server.exe") throw new Error("judge-unavailable");
    this.touch();
    const key = JSON.stringify(config);
    if (this.starting) {
      if (this.startingKey !== key) throw new Error("judge-unavailable");
      // Gateway admission serializes production calls. A second direct caller
      // can cancel its own wait without cancelling the owner of shared startup.
      let cancelWait: () => void = () => undefined;
      const cancelled = new Promise<never>((_resolve, reject) => {
        cancelWait = () => reject(new Error("judge-unavailable"));
        signal.addEventListener("abort", cancelWait, { once: true });
      });
      try { return await Promise.race([this.starting, cancelled]); }
      finally { signal.removeEventListener("abort", cancelWait); }
    }
    if (this.alive() && this.owned!.key === key) return this.owned!;
    const controller = new AbortController();
    this.startup = controller;
    this.startingKey = key;
    const cancel = () => controller.abort();
    signal.addEventListener("abort", cancel, { once: true });
    this.starting = (async () => {
      await this.stop();
      // Only process startup/crash is retried; cancellation and malformed outputs are not.
      for (let attempt = 0; attempt < 2; attempt++) {
        try { return await this.start(config, key, controller.signal); }
        catch {
          await this.stop();
          if (controller.signal.aborted || this.closed || attempt) throw new Error("judge-unavailable");
        }
      }
      throw new Error("judge-unavailable");
    })();
    try { return await this.starting; }
    finally {
      signal.removeEventListener("abort", cancel);
      this.starting = undefined;
      this.startingKey = undefined;
      if (this.startup === controller) this.startup = undefined;
    }
  }

  private async start(config: LocalJudgeConfig, key: string, signal: AbortSignal): Promise<LocalJudgeEndpoint> {
    if (signal.aborted || this.closed) throw new Error("judge-unavailable");
    const server = createServer();
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject); server.listen(0, "127.0.0.1", resolve);
    });
    const port = (server.address() as AddressInfo).port;
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    if (signal.aborted || this.closed) throw new Error("judge-unavailable");
    const context = config.contextTokens ?? 8192;
    const threads = config.threads ?? 1; // Core supplies half the physical core count.
    if (!Number.isInteger(context) || context < 512 || context > 8192 ||
        !Number.isInteger(threads) || threads < 1 || threads > 128) throw new Error("judge-unavailable");
    const apiKey = randomBytes(32).toString("hex");
    const child = this.launcher(config.runtimePath, ["-m", config.modelPath, "--host", "127.0.0.1",
      "--port", String(port), "--api-key", apiKey, "-c", String(context), "-ub", String(context),
      "-b", String(context), "-np", "1", "-t", String(threads), "--prio", "-1", "--alias", "julia-1"],
      { windowsHide: true, stdio: "ignore", cwd: path.dirname(config.runtimePath) });
    let failed = false;
    child.once("error", () => { failed = true; });
    const exited = new Promise<void>(resolve => { child.once("exit", () => resolve()); child.once("error", () => resolve()); });
    this.owned = { child, apiKey, baseUrl: `http://127.0.0.1:${port}`, key, exited };
    try { if (child.pid) setPriority(child.pid, constants.priority.PRIORITY_BELOW_NORMAL); } catch { /* --prio remains in force. */ }
    const deadline = Date.now() + 30_000;
    while (!signal.aborted && !this.closed && Date.now() < deadline) {
      if (failed || child.exitCode !== null) throw new Error("judge-unavailable");
      try {
        const response = await this.fetcher(`${this.owned.baseUrl}/health`, {
          signal: AbortSignal.any([signal, AbortSignal.timeout(1000)]), redirect: "error",
          headers: { authorization: `Bearer ${apiKey}` } });
        if (response.ok) { await response.body?.cancel(); return this.owned; }
        await response.body?.cancel();
      } catch { /* A newly spawned server can be loading or not listening yet. */ }
      await delay(50, undefined, { signal }).catch(() => undefined);
    }
    throw new Error("judge-unavailable");
  }

  /** Admission clears the idle deadline. Arm it only after the complete response finishes. */
  touch(): void { if (this.idle) clearTimeout(this.idle); this.idle = undefined; }

  release(config: LocalJudgeConfig): void {
    this.touch();
    if (this.closed) return;
    this.idle = setTimeout(() => { void this.stop(); }, config.idleMs ?? 300_000);
    this.idle.unref();
  }

  async stop(): Promise<void> {
    this.touch();
    const owned = this.owned; this.owned = undefined;
    if (!owned) return;
    if (owned.child.exitCode === null) owned.child.kill();
    await Promise.race([owned.exited, delay(2000, undefined, { ref: false })]);
    if (owned.child.exitCode === null) owned.child.kill("SIGKILL");
    await Promise.race([owned.exited, delay(1000, undefined, { ref: false })]);
  }

  async close(): Promise<void> {
    this.closed = true;
    await this.cancel();
  }

  async cancel(): Promise<void> {
    this.startup?.abort();
    await this.stop();
    await this.starting?.catch(() => undefined);
    await this.stop();
  }
}
