// Modified 2026-10-13: the engine's provider endpoint is the plugin's ExecutionRelay (plugin-sdk/node/execution-relay.ts).
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { AdvisorRuntime, ADVISOR_ENGINE_VERSION, type AdvisorRuntimeRequest, type EngineService } from "../plugins/kyf.advisor/engine/runtime";

test("failed engine initialization and native engine exit retire the owned execution relay", async t => {
  const parent = path.resolve(".local/tmp/model-trace-lifecycle-tests");
  await mkdir(parent, { recursive: true });
  const root = await mkdtemp(path.join(parent, "synthetic-"));
  const engineDirectory = path.join(root, ".local/advisor-engine");
  await mkdir(engineDirectory, { recursive: true });
  const executable = Buffer.from("Synthetic engine fixture; never executed.");
  await writeFile(path.join(engineDirectory, "opencode.exe"), executable);
  await writeFile(path.join(engineDirectory, "manifest.json"), JSON.stringify({ schema: 1,
    version: ADVISOR_ENGINE_VERSION, file: "opencode.exe", bytes: executable.length,
    sha256: createHash("sha256").update(executable).digest("hex") }));
  t.after(async () => { assert(root.startsWith(parent + path.sep)); await rm(root, { recursive: true, force: true }); });
  const request: AdvisorRuntimeRequest = { account: "synthetic-account", user: "synthetic-person", agentId: "advisor",
    threadId: "synthetic-thread", runtimeSessionId: "ses_synthetic", system: "Synthetic system", message: "Synthetic message" };
  const service: EngineService = { id: "svc-1", protocol: "chat_completions", model: "synthetic", url: "http://127.0.0.1:12345/s/svc-1" };

  for (const failure of ["launch", "path", "scope", "subscribe", "exit"] as const) {
    let relayUrl = "", closed = 0, exited: (() => void) | undefined;
    let resolveStream!: () => void;
    const streamEnded = new Promise<void>(resolve => { resolveStream = resolve; });
    const runtime = new AdvisorRuntime({ root, stateRoot: path.join(root, "state"), startEngine: async spec => {
      relayUrl = String(Object.values(spec.config.provider!)[0]!.options!.baseURL);
      if (failure === "launch") throw new Error("Synthetic launch failure");
      return {
        client: {
          path: { get: async () => {
            if (failure === "path") throw new Error("Synthetic path lookup failure");
            return { data: { directory: failure === "scope" ? parent : spec.cwd, worktree: spec.cwd } };
          } },
          event: { subscribe: async () => {
            if (failure === "subscribe") throw new Error("Synthetic subscription failure");
            return { stream: (async function* () { await streamEnded; })() };
          } },
          session: { get: async () => { throw new Error("Synthetic session lookup failure"); } },
        } as any,
        close: async () => { closed++; resolveStream(); },
        onExit: callback => { exited = callback; },
      };
    } });
    try {
      await assert.rejects(runtime.inspectSession(service, request));
      assert.match(relayUrl, /^http:\/\/127\.0\.0\.1:\d+\/s\/svc-1$/u, "the relay was created before the injected failure");
      if (failure === "exit") {
        assert.equal((await fetch(relayUrl)).status, 403);
        assert(exited); exited(); resolveStream();
        await new Promise(resolve => setImmediate(resolve));
      }
      await assert.rejects(fetch(relayUrl, { signal: AbortSignal.timeout(1000) }),
        `${failure}: the retired loopback listener must not remain reachable`);
      if (failure !== "exit") assert.equal(closed, failure === "launch" ? 0 : 1);
    } finally { await runtime.close(); resolveStream(); }
  }
});
