// Synthetic integration worker: stdin carries the private producer capability.
import { createServer } from "node:http";
import { once } from "node:events";
import { withRemoteModelTrace, flushModelTraceClient, modelTraceClientStatus } from "../../electron/model-trace-client";
import { traceProviderFetch, modelTraceOutcome } from "../../electron/model-traces";

let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const contexts = JSON.parse(raw);
let requests = 0;
const server = createServer(async (request, response) => {
  let body = "";
  for await (const chunk of request) body += chunk;
  const value = JSON.parse(body);
  requests++;
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  for (const text of ["synthetic ", value.messages[0].content]) {
    response.write(`data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`);
    await new Promise(resolve => setTimeout(resolve, 8));
  }
  response.end('data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\ndata: [DONE]\n\n');
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
const address = server.address() as { port: number };
try {
  await Promise.all(contexts.map((context: any, index: number) => withRemoteModelTrace(context, async () => {
    const endpoint = `http://127.0.0.1:${address.port}/v1/chat/completions`;
    const response = await traceProviderFetch(endpoint, { method: "POST", headers: {
      "Content-Type": "application/json", Authorization: "Bearer synthetic-secret-key" },
      body: JSON.stringify({ model: "synthetic", stream: true,
        messages: [{ role: "user", content: `contact-${index}` }] }) },
      { protocol: "openai-chat", transportProtocol: "openai-chat", model: "synthetic", endpoint, method: "POST" },
      ["synthetic-secret-key"]);
    const result = await response.text();
    if (!result.includes(`contact-${index}`)) throw new Error("business output changed");
    modelTraceOutcome("success", { synthetic: true, contact: index }, "adopted");
  })));
  await flushModelTraceClient();
  process.stdout.write(JSON.stringify({ requests, diagnostics: modelTraceClientStatus() }) + "\n");
} finally { server.closeAllConnections(); server.close(); }
