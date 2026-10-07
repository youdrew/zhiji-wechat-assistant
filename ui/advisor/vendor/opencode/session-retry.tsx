// Adapted from OpenCode v1.18.34 SessionRetry; MIT: licenses/OpenCode-MIT.txt.
import { createEffect, createMemo, createSignal, on, onCleanup, Show } from "solid-js"
import type { SessionStatus } from "@opencode-ai/sdk/v2/client"

function useI18n() {
  return { t(key: string, values: Record<string, number | string> = {}) {
    if (key.endsWith("geminiHot")) return "模型服务请求受限"
    if (key.endsWith("inSeconds")) return `${values.seconds} 秒后重试`
    if (key.endsWith("retrying")) return "正在重试"
    if (key.endsWith("attempt")) return `第 ${values.attempt} 次重试`
    return `${values.line ?? ""} · 第 ${values.attempt} 次`
  } }
}

export function SessionRetry(props: { status: SessionStatus; show?: boolean }) {
  const i18n = useI18n()
  const retry = createMemo(() => {
    if (props.status.type !== "retry") return
    return props.status
  })
  const [seconds, setSeconds] = createSignal(0)
  createEffect(
    on(retry, (current) => {
      if (!current) return
      const update = () => {
        const next = retry()?.next
        if (!next) return
        setSeconds(Math.round((next - Date.now()) / 1000))
      }
      update()
      const timer = setInterval(update, 1000)
      onCleanup(() => clearInterval(timer))
    }),
  )
  const message = createMemo(() => {
    const current = retry()
    if (!current) return ""
    if (current.message.includes("exceeded your current quota") && current.message.includes("gemini")) {
      return i18n.t("ui.sessionTurn.retry.geminiHot")
    }
    if (current.message.length > 80) return current.message.slice(0, 80) + "..."
    return current.message
  })
  const truncated = createMemo(() => {
    const current = retry()
    if (!current) return false
    return current.message.length > 80
  })
  const info = createMemo(() => {
    const current = retry()
    if (!current) return ""
    const count = Math.max(0, seconds())
    const delay = count > 0 ? i18n.t("ui.sessionTurn.retry.inSeconds", { seconds: count }) : ""
    const retrying = i18n.t("ui.sessionTurn.retry.retrying")
    const line = [retrying, delay].filter(Boolean).join(" ")
    if (!line) return i18n.t("ui.sessionTurn.retry.attempt", { attempt: current.attempt })
    return i18n.t("ui.sessionTurn.retry.attemptLine", { line, attempt: current.attempt })
  })

  return (
    <Show when={retry() && (props.show ?? true)}>
      <div data-slot="session-turn-retry" class="advisor-native-retry">
        <div title={truncated() ? retry()?.message : undefined}>{message()}</div>
        <Show when={info()}>{(line) => <span>{line()}</span>}</Show>
      </div>
    </Show>
  )
}
