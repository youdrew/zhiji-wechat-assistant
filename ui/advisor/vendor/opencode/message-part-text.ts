// OpenCode v1.18.34, e9f8a210b9e2b1e13d375b84906069886eb3b767. MIT: licenses/OpenCode-MIT.txt.
export function readPartText(accum: Record<string, string> | undefined, part: { id: string; text?: string }): string {
  return (accum?.[part.id] ?? part.text ?? "").trim()
}
