// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Locating and launching DeepSeek Harness (DSH) as the advisor's agent engine.
//
// The advisor drives `dsh --profile acp` (Agent Client Protocol over stdio) from the DSH desktop
// installation: its Electron binary run as Node (ELECTRON_RUN_AS_NODE) on the CLI entry inside app.asar.
// Each launch gets its own DSH_HOME inside the account's plugin folder, so sessions, attachments and
// settings of the advisor never mix with the user's own DSH profile, and are removed with the account.
// `engine_client.py` mirrors `harnessCandidates` to answer "is the engine available" without starting Node.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { stat } from "node:fs/promises";
import path from "node:path";

export const HARNESS_NAME = "DeepSeek Harness";
/** The CLI entry inside the desktop installation's app.asar. */
const CLI_ENTRY = ["dsh", "node_modules", "@deepseek-ai", "dsh-desktop-host", "lib", "cli.js"];

export interface HarnessInstall {
  /** The Electron binary, run as Node. */
  executable: string;
  /** The `dsh` CLI entry (a path inside app.asar, which only Electron's Node can open). */
  cli: string;
  /** The installation folder, for messages and process ownership checks. */
  directory: string;
}

/** Installation folders to try, in order: an explicit override, one shipped next to the app, the per-user and per-machine installs. */
export function harnessCandidates(root: string, env: NodeJS.ProcessEnv = process.env, platform: NodeJS.Platform = process.platform): string[] {
  const candidates: string[] = [];
  const add = (value: string | undefined, ...rest: string[]) => { if (value) candidates.push(path.join(value, ...rest)); };
  add(env.KYF_DSH_INSTALL);
  add(root, "runtime", "dsh");
  if (platform === "win32") {
    add(env.LOCALAPPDATA, "Programs", HARNESS_NAME);
    add(env.ProgramFiles, HARNESS_NAME);
    add(env["ProgramFiles(x86)"], HARNESS_NAME);
  } else if (platform === "darwin") {
    candidates.push(`/Applications/${HARNESS_NAME}.app`);
    add(env.HOME, "Applications", `${HARNESS_NAME}.app`);
  } else {
    candidates.push("/opt/DeepSeek Harness", "/usr/lib/deepseek-harness");
  }
  return [...new Set(candidates.map(candidate => path.resolve(candidate)))];
}

/** The binary and the resources folder of one installation folder. */
export function harnessLayout(directory: string, platform: NodeJS.Platform = process.platform): HarnessInstall {
  if (platform === "darwin") {
    return { directory, executable: path.join(directory, "Contents", "MacOS", HARNESS_NAME),
      cli: path.join(directory, "Contents", "Resources", "app.asar", ...CLI_ENTRY) };
  }
  return { directory, executable: path.join(directory, platform === "win32" ? `${HARNESS_NAME}.exe` : "deepseek-harness"),
    cli: path.join(directory, "resources", "app.asar", ...CLI_ENTRY) };
}

async function isFile(target: string): Promise<boolean> {
  try { return (await stat(target)).isFile(); } catch { return false; }
}

/** The first candidate folder holding both the binary and app.asar; undefined when DSH is not installed. */
export async function resolveHarness(root: string, env: NodeJS.ProcessEnv = process.env,
                                     platform: NodeJS.Platform = process.platform): Promise<HarnessInstall | undefined> {
  for (const directory of harnessCandidates(root, env, platform)) {
    const layout = harnessLayout(directory, platform);
    // cli.js lives inside app.asar, which plain Node cannot stat: check the archive itself.
    const archive = layout.cli.slice(0, layout.cli.indexOf("app.asar") + "app.asar".length);
    if (await isFile(layout.executable) && await isFile(archive)) return layout;
  }
  return undefined;
}

/**
 * The environment of a DSH child: system basics, its own DSH_HOME and an empty shared-agents home (so neither the
 * user's `~/.dsh` nor `~/.agents` instructions and skills reach the advisor), telemetry off, plus `extra` (the
 * relay secret). Nothing else of the parent environment is inherited: no credential can leak into the engine.
 */
export function harnessEnvironment(home: string, agentsHome: string, extra: Record<string, string>,
                                   parentEnv: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const name of ["SystemRoot", "SYSTEMROOT", "WINDIR", "SystemDrive", "ComSpec", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"]) {
    if (parentEnv[name]) env[name] = parentEnv[name];
  }
  const systemRoot = parentEnv.SystemRoot || parentEnv.WINDIR;
  Object.assign(env, {
    PATH: systemRoot ? path.join(systemRoot, "System32") : (parentEnv.PATH ?? ""),
    HOME: home, USERPROFILE: home, APPDATA: path.join(home, "AppData", "Roaming"), LOCALAPPDATA: path.join(home, "AppData", "Local"),
    ELECTRON_RUN_AS_NODE: "1", ELECTRON_NO_ATTACH_CONSOLE: "1",
    DSH_HOME: home, DSH_AGENTS_HOME: agentsHome, DSH_TELEMETRY_MODE: "OFF", DO_NOT_TRACK: "1", OTEL_SDK_DISABLED: "true", NO_COLOR: "1",
    ...extra,
  });
  return env;
}

export interface HarnessLaunch {
  install: HarnessInstall;
  /** Arguments after the CLI entry, e.g. ["acp", "--patch", file]. */
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
}

export function launchHarness(launch: HarnessLaunch): ChildProcessWithoutNullStreams {
  return spawn(launch.install.executable, ["--expose-internals", launch.install.cli, ...launch.args],
    { cwd: launch.cwd, env: launch.env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true, shell: false });
}

/** `dsh --version`, or undefined when the installation does not start. */
export async function harnessVersion(install: HarnessInstall, env: NodeJS.ProcessEnv, cwd: string, timeoutMs = 20_000): Promise<string | undefined> {
  return new Promise(resolve => {
    let output = "";
    let child: ChildProcessWithoutNullStreams;
    try { child = launchHarness({ install, args: ["--version"], cwd, env }); } catch { resolve(undefined); return; }
    const timer = setTimeout(() => { child.kill(); resolve(undefined); }, timeoutMs);
    child.stdout.on("data", chunk => { output = (output + chunk.toString("utf8")).slice(0, 256); });
    child.stderr.on("data", () => {});
    child.once("error", () => { clearTimeout(timer); resolve(undefined); });
    child.once("exit", code => {
      clearTimeout(timer);
      const version = output.trim().split(/\s+/u)[0] ?? "";
      resolve(code === 0 && /^\d+\.\d+\.\d+[A-Za-z0-9.+-]*$/u.test(version) ? version : undefined);
    });
    child.stdin.end();
  });
}
