import { createMemo, createSignal, For, Show } from "solid-js";
import { render } from "solid-js/web";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { project, type Projection } from "./vendor/opencode/markdown-stream";
import { readPartText } from "./vendor/opencode/message-part-text";
import { TextShimmer } from "./vendor/opencode/text-shimmer";
import { SessionRetry } from "./vendor/opencode/session-retry";
import type { SessionStatus } from "@opencode-ai/sdk/v2/client";

type TextState = { text: string; live: boolean };
type EventState = { state?: string; text?: string; next?: number; attempt?: number };
type TextMount = { update(value: TextState): void; dispose(): void };
type StatusMount = { update(value: EventState): void; dispose(): void };
const textMounts = new WeakMap<HTMLElement, TextMount>();
const statusMounts = new WeakMap<HTMLElement, StatusMount>();

function Markdown(props: TextState) {
  const projection = createMemo<Projection>((previous) => {
    const next = project(previous, readPartText(undefined, { id: "answer", text: props.text }), props.live);
    return { ...next, blocks: next.blocks.map((block, index) => {
      const cached = previous?.blocks[index];
      return cached && cached.raw === block.raw && cached.src === block.src && cached.mode === block.mode &&
        cached.language === block.language && cached.complete === block.complete ? cached : block;
    }) };
  });
  return <For each={projection().blocks}>{(block) => {
    const node = document.createElement("div");
    node.className = "advisor-native-markdown";
    if (block.mode === "code") {
      const pre = document.createElement("pre"), code = document.createElement("code");
      code.textContent = block.src;
      pre.append(code); node.append(pre);
    } else {
      const html = marked.parse(block.src, { async: false, breaks: true });
      node.append(DOMPurify.sanitize(html, {
        RETURN_DOM_FRAGMENT: true,
        ALLOWED_TAGS: ["p", "br", "strong", "em", "s", "blockquote", "ul", "ol", "li", "pre", "code", "h1", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td", "hr"],
        ALLOWED_ATTR: [], ALLOW_DATA_ATTR: false,
      }));
    }
    return node;
  }}</For>;
}

const STATUS: Record<string, string> = {
  reading: "正在读取聊天记录", compressing: "正在压缩上下文", compacting: "正在压缩上下文",
  preparing: "正在准备", starting: "正在准备", awaiting: "正在等待", stopping: "正在停止",
  thinking: "正在思考", busy: "正在回答", generating: "正在回答", running: "正在回答",
  answering: "正在回答",
  stopped: "已停止", done: "已完成", error: "本轮失败", "context-compacted": "上下文已压缩",
};

function NativeStatus(props: EventState) {
  const retry = createMemo<SessionStatus>(() => props.state === "retry" && Number.isFinite(props.next)
    ? { type: "retry", next: props.next!, attempt: props.attempt || 1, message: props.text || "模型服务请求受限" }
    : { type: "idle" });
  const text = createMemo(() => props.text || STATUS[props.state || ""] || "");
  const active = createMemo(() => ["preparing", "starting", "awaiting", "stopping", "reading", "compressing", "compacting", "thinking", "busy", "generating", "running", "answering"].includes(props.state || ""));
  const waiting = createMemo(() => ["preparing", "starting", "awaiting"].includes(props.state || ""));
  return <Show when={props.state === "retry"} fallback={<Show when={waiting()} fallback={<TextShimmer text={text()} active={active()} />}>
    <span class="advisor-waiting-dots" role="status" aria-label={text()}><span /><span /><span /></span>
  </Show>}>
    <SessionRetry status={retry()} />
  </Show>;
}

function renderInto(node: HTMLElement, text: string, live = false) {
  if (!(node instanceof HTMLElement) || typeof text !== "string") return;
  let mount = textMounts.get(node);
  if (!mount) {
    const [state, update] = createSignal<TextState>({ text, live });
    const dispose = render(() => <Markdown text={state().text} live={state().live} />, node);
    mount = { update, dispose }; textMounts.set(node, mount);
  } else mount.update({ text, live });
}

function statusInto(node: HTMLElement, event: EventState) {
  if (!(node instanceof HTMLElement) || !event || typeof event !== "object") return;
  let mount = statusMounts.get(node);
  if (!mount) {
    const [state, update] = createSignal<EventState>(event);
    const dispose = render(() => <NativeStatus state={state().state} text={state().text} next={state().next} attempt={state().attempt} />, node);
    mount = { update, dispose }; statusMounts.set(node, mount);
  } else mount.update(event);
}

function disposeInto(node: HTMLElement) {
  textMounts.get(node)?.dispose(); statusMounts.get(node)?.dispose();
  textMounts.delete(node); statusMounts.delete(node);
}

Object.defineProperty(window, "AdvisorTimeline", {
  value: Object.freeze({ renderInto, statusInto, disposeInto }), writable: false, configurable: false,
});
