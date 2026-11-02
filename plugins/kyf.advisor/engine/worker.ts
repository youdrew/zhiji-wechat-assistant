// Modified by Know Your Friends, 2026-10-10 (Apache-2.0): bounded ephemeral vision.
// Moved from scripts/advisor-worker.ts into the kyf.advisor plugin, 2026-10-13 (Apache-2.0): model calls carry a
// gateway service and the task's execution token instead of a provider config; the `generate` command and the
// remote trace client are gone (the gateway meters and traces every request).
//
// The advisor engine worker: one process per account, started by the plugin backend as
//   KYF_NODE --import KYF_TSX_LOADER engine/worker.ts --state <plugin account dir> [--root <app root>]
// (`--root` defaults to KYF_APP_ROOT; the pinned opencode is found under it, the engine's state lives in
// `<state>/engine`). JSON lines on stdin, one command each:
//   {"id": 1, "cmd": "respond" | "compact" | "inspectMedia" | "compactSession" | "summarizeDiscussion",
//    "payload": {"service": {"id", "protocol", "model", "url", "contextTokens"?, "stamp"?}, "token": "<execution token>",
//                "request": {...AdvisorRuntimeRequest}}}
//   {"id": 2, "cmd": "inspectSession" | "deleteSession" | "stopSessionOperation",
//    "payload": {"service": {...}, "request": {...}}}                 (no model call, no token)
//   {"id": 3, "cmd": "cancel", "payload": {"targetId": 1}}            (also accepts requestId / id)
//   {"id": 4, "cmd": "doctor"} -> {"state": "available" | "missing", "version"}
//   {"id": 5, "cmd": "close"}  -> {"closed": true}, then the worker exits
// and on stdout: {"id", "event": {...AdvisorRuntimeEvent}}* then {"id", "result": {...AdvisorRuntimeResult}}
// or {"id", "error": {"code", "message"}}. `service` is an entry of `model/services`; `token` is the execution token
// of the Core task the call belongs to. Steps at the gateway: respond -> advisor, compact -> wechat-summary,
// summarizeDiscussion -> discussion-summary, compactSession -> discussion-compaction, inspectMedia -> media-understanding.
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as readline from "node:readline";
import { AdvisorRuntime, AdvisorRuntimeError, advisorSafeError, type AdvisorRuntimeRequest, type EngineService } from "./runtime";

const MAX_LINE_BYTES = 20 * 1024 * 1024;
const MODEL_COMMANDS = ["respond", "compact", "inspectMedia", "compactSession", "summarizeDiscussion"] as const;
const SESSION_COMMANDS = ["inspectSession", "deleteSession", "stopSessionOperation"] as const;
type CommandId = string | number;

function output(value: unknown): void { process.stdout.write(JSON.stringify(value) + "\n"); }

export async function runAdvisorWorker(root: string, stateRoot: string): Promise<void> {
  const runtime = new AdvisorRuntime({ root, stateRoot });
  const active = new Map<CommandId, AbortController>();
  const tasks = new Set<Promise<void>>();
  let closing = false;
  const handle = async (raw: string) => {
    let id: CommandId | null = null;
    try {
      if (Buffer.byteLength(raw, "utf8") > MAX_LINE_BYTES) throw new AdvisorRuntimeError("invalid-request");
      let command: any;
      try { command = JSON.parse(raw); } catch { throw new AdvisorRuntimeError("invalid-request"); }
      if (!command || typeof command !== "object" || Array.isArray(command)) throw new AdvisorRuntimeError("invalid-request");
      if ((typeof command.id !== "string" && !Number.isSafeInteger(command.id)) ||
          (typeof command.id === "string" && (!command.id || command.id.length > 128))) throw new AdvisorRuntimeError("invalid-request");
      id = command.id;
      if (closing) throw new AdvisorRuntimeError("engine-unavailable");
      if (command.cmd === "doctor") { output({ id, result: await runtime.doctor() }); return; }
      if (command.cmd === "cancel") {
        const target = command.payload?.targetId ?? command.payload?.requestId ?? command.payload?.id;
        active.get(target)?.abort();
        output({ id, result: { cancelled: active.has(target) } });
        return;
      }
      if (command.cmd === "close") {
        closing = true;
        for (const controller of active.values()) controller.abort();
        await runtime.close();
        output({ id, result: { closed: true } });
        lines.close();
        process.stdin.pause();
        return;
      }
      const model = (MODEL_COMMANDS as readonly string[]).includes(command.cmd);
      if ((!model && !(SESSION_COMMANDS as readonly string[]).includes(command.cmd)) || !command.payload?.request ||
          !command.payload?.service || (model && typeof command.payload.token !== "string") || active.has(id!)) {
        throw new AdvisorRuntimeError("invalid-request");
      }
      const controller = new AbortController();
      active.set(id!, controller);
      try {
        const onEvent = (event: unknown) => { if (!controller.signal.aborted) output({ id, event }); };
        const service = command.payload.service as EngineService, request = command.payload.request as AdvisorRuntimeRequest;
        const result = model ?
          await runtime[command.cmd as (typeof MODEL_COMMANDS)[number]](service, command.payload.token, request, onEvent, controller.signal) :
          await runtime[command.cmd as (typeof SESSION_COMMANDS)[number]](service, request, onEvent, controller.signal);
        output({ id, result });
      } finally { active.delete(id!); }
    } catch (error) {
      const safe = advisorSafeError(error);
      output({ id, error: { code: safe.code, message: safe.message } });
    }
  };
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
  const shutdown = () => { closing = true; for (const controller of active.values()) controller.abort(); lines.close(); process.stdin.pause(); };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
  lines.on("line", (line) => {
    if (!line.trim()) return;
    const task = handle(line).finally(() => tasks.delete(task));
    tasks.add(task);
  });
  await new Promise<void>((resolve) => lines.once("close", resolve));
  closing = true;
  for (const controller of active.values()) controller.abort();
  await runtime.close();
  await Promise.allSettled(tasks);
  process.off("SIGTERM", shutdown);
  process.off("SIGINT", shutdown);
}

function argument(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const entryPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (entryPath === fileURLToPath(import.meta.url)) {
  const root = argument("--root") ?? process.env.KYF_APP_ROOT ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
  const stateRoot = argument("--state");
  if (!root || !stateRoot) {
    output({ id: null, error: { code: "invalid-request", message: "Agent 请求格式不正确" } });
    process.exitCode = 1;
  } else runAdvisorWorker(root, stateRoot).catch(() => {
    output({ id: null, error: { code: "engine-unavailable", message: "Agent 引擎连接已断开" } });
    process.exitCode = 1;
  });
}
