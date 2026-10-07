// OpenCode v1.18.34, e9f8a210b9e2b1e13d375b84906069886eb3b767. MIT: licenses/OpenCode-MIT.txt.
import type { Block, Projection } from "./markdown-stream"

export function completedProjection(text: string): Projection {
  return { text, blocks: [{ raw: text, src: text, mode: "full" }] }
}

export function canReusePendingBlock(current: Pick<Block, "mode" | "raw"> | undefined, next: Block) {
  if (!current || current.mode !== next.mode) return false
  if (next.mode === "code" || next.mode === "live") return next.raw.startsWith(current.raw)
  return current.raw === next.raw
}
