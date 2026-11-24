// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// A DeepSeek Harness host plugin, mounted by the advisor's launch patch (never installed into a user profile).
//
// The ACP surface of DSH carries committed assistant messages only. This plugin forwards the live assistant
// stream of each agent as an extra JSON-RPC notification on the same stdout pipe, so the advisor panel can show
// the answer while it is written. One line per frame, in order with the ACP frames of the same process:
//   {"jsonrpc":"2.0","method":"_zhiji/stream","params":{"sessionId","attemptId","kind":"start"|"text"|"reasoning"|"usage"|"end","text"?,"usage"?}}
// Unknown frame shapes are dropped: when DSH changes its stream, answers still arrive as committed messages.
export const name = "zhiji-advisor-stream";

export function apply(ctx) {
  const write = (params) => {
    try { process.stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "_zhiji/stream", params }) + "\n"); } catch { /* closing */ }
  };
  ctx.on("agent/assistant-stream", (event) => {
    try {
      const sessionId = event?.agent?.id;
      const frame = event?.frame;
      if (typeof sessionId !== "string" || !frame || typeof frame !== "object") return;
      const attemptId = typeof frame.attemptId === "string" ? frame.attemptId : "";
      if (frame.type === "start" || frame.type === "end") { write({ sessionId, attemptId, kind: frame.type }); return; }
      const chunk = frame.chunk;
      if (frame.type !== "chunk" || !chunk || typeof chunk !== "object") return;
      if ((chunk.type === "text-delta" || chunk.type === "reasoning-delta") && typeof chunk.text === "string" && chunk.text) {
        write({ sessionId, attemptId, kind: chunk.type === "text-delta" ? "text" : "reasoning", text: chunk.text });
      } else if (chunk.type === "usage" && chunk.usage && typeof chunk.usage === "object") {
        write({ sessionId, attemptId, kind: "usage", usage: chunk.usage });
      }
    } catch { /* never disturb the agent */ }
  });
}
