// Modified 2026-10-13: the conversation components belong to the kyf.advisor plugin and build into its pages.
import { build } from "esbuild";
import { solidPlugin } from "esbuild-plugin-solid";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
await build({
  absWorkingDir: root, entryPoints: ["plugins/kyf.advisor/ui-src/entry.tsx"],
  outfile: "plugins/kyf.advisor/ui/advisor-ui-bundle.js",
  bundle: true, minify: true, platform: "browser", format: "iife", target: "chrome120",
  conditions: ["browser"], define: { "process.env.NODE_ENV": '"production"' },
  plugins: [solidPlugin({ solid: { generate: "dom", hydratable: false, dev: false } })],
  legalComments: "inline", sourcemap: false,
});
console.log("Advisor conversation components built.");
