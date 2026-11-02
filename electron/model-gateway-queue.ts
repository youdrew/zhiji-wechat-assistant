// Copyright 2026 Know Your Friends contributors. Apache-2.0.
// Per-service admission for the model gateway: one shared concurrency limit,
// round-robin between plugins, interactive work first and
// background work promoted as it waits.
// Since 2026-10-25 waiting is unbounded: no cap on how many wait and no deadline
// while waiting. A request leaves the queue only when it runs or its caller stops;
// the model timeout starts once it is forwarded.

export const AGING_MS = 30_000;
export const DEFAULT_MODEL_CONCURRENCY = 4;

export function modelConcurrency(value = DEFAULT_MODEL_CONCURRENCY): number {
  if (!Number.isSafeInteger(value) || value < 1 || value > 8) throw new Error("invalid-model-concurrency");
  return value;
}

interface Ticket {
  plugin: string;
  interactive: boolean;
  enqueuedAt: number;
  grant: (release: () => void) => void;
  fail: (error: Error) => void;
  cleanup: () => void;
}

export class QueueAborted extends Error {
  constructor() { super("queue-aborted"); this.name = "AbortError"; }
}

export class ServiceQueue {
  private running = 0;
  private readonly runningByPlugin = new Map<string, number>();
  private readonly waiting = new Map<string, Ticket[]>();
  private turn: string[] = [];

  constructor(private concurrency = DEFAULT_MODEL_CONCURRENCY, private readonly now = () => Date.now()) {
    modelConcurrency(concurrency);
  }

  configure(concurrency: number): void {
    this.concurrency = modelConcurrency(concurrency);
    this.pump();
  }

  stats(): { running: number; waiting: number; queuedPlugins: number; runningPlugins: number } {
    let waiting = 0;
    for (const tickets of this.waiting.values()) waiting += tickets.length;
    return { running: this.running, waiting, queuedPlugins: this.waiting.size, runningPlugins: this.runningByPlugin.size };
  }

  acquire(plugin: string, interactive: boolean, signal?: AbortSignal): Promise<() => void> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new QueueAborted()); return; }
      const ticket: Ticket = { plugin, interactive, enqueuedAt: this.now(),
        grant: resolve, fail: reject, cleanup: () => {} };
      // Any plugin can use idle model capacity; the rest wait their turn.
      if (this.running < this.concurrency) {
        this.grant(ticket); return;
      }
      const list = this.waiting.get(plugin) ?? [];
      list.push(ticket);
      this.waiting.set(plugin, list);
      if (!this.turn.includes(plugin)) this.turn.push(plugin);
      const cancel = () => this.remove(ticket, new QueueAborted());
      signal?.addEventListener("abort", cancel, { once: true });
      ticket.cleanup = () => { signal?.removeEventListener("abort", cancel); };
      this.pump();
    });
  }

  private dropEmpty(plugin: string): void {
    if (this.waiting.get(plugin)?.length) return;
    this.waiting.delete(plugin);
    this.turn = this.turn.filter(value => value !== plugin);
  }

  private remove(ticket: Ticket, error: Error): void {
    const queued = this.waiting.get(ticket.plugin);
    const index = queued?.indexOf(ticket) ?? -1;
    if (index < 0) return;
    queued!.splice(index, 1);
    ticket.cleanup();
    this.dropEmpty(ticket.plugin);
    ticket.fail(error);
  }

  private grant(ticket: Ticket): void {
    ticket.cleanup();
    this.running++;
    this.runningByPlugin.set(ticket.plugin, (this.runningByPlugin.get(ticket.plugin) ?? 0) + 1);
    let released = false;
    ticket.grant(() => {
      if (released) return;
      released = true;
      this.running--;
      const remaining = (this.runningByPlugin.get(ticket.plugin) ?? 1) - 1;
      if (remaining) this.runningByPlugin.set(ticket.plugin, remaining);
      else this.runningByPlugin.delete(ticket.plugin);
      this.pump();
    });
  }

  private urgent(ticket: Ticket): boolean {
    return ticket.interactive || this.now() - ticket.enqueuedAt >= AGING_MS;
  }

  private next(): Ticket | undefined {
    const eligible = this.turn.filter(plugin => (this.waiting.get(plugin)?.length ?? 0) > 0);
    if (!eligible.length) return undefined;
    // Interactive or aged work goes first; plugins take turns within the same priority.
    const pick = eligible.find(plugin => this.waiting.get(plugin)!.some(ticket => this.urgent(ticket))) ?? eligible[0]!;
    const list = this.waiting.get(pick)!;
    const index = Math.max(0, list.findIndex(ticket => this.urgent(ticket)));
    const [ticket] = list.splice(index, 1);
    this.turn = [...this.turn.filter(plugin => plugin !== pick), pick];
    this.dropEmpty(pick);
    return ticket;
  }

  private pump(): void {
    while (this.running < this.concurrency) {
      const ticket = this.next();
      if (!ticket) return;
      this.grant(ticket);
    }
  }
}
