/**
 * Debounce per key and keep at most one job in flight per key: a new job for a key
 * aborts the one still running for that key.
 */
export class KeyedScheduler {
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private running = new Map<string, AbortController>();

  constructor(
    private readonly delayMs: number,
    private readonly job: (key: string, signal: AbortSignal) => Promise<void>,
  ) {}

  request(key: string): void {
    const t = this.timers.get(key);
    if (t) clearTimeout(t);
    this.timers.set(
      key,
      setTimeout(() => {
        this.timers.delete(key);
        this.running.get(key)?.abort();
        const ac = new AbortController();
        this.running.set(key, ac);
        this.job(key, ac.signal)
          .catch(() => undefined)
          .finally(() => {
            if (this.running.get(key) === ac) this.running.delete(key);
          });
      }, this.delayMs),
    );
  }

  dispose(): void {
    for (const t of this.timers.values()) clearTimeout(t);
    for (const a of this.running.values()) a.abort();
    this.timers.clear();
    this.running.clear();
  }
}
