import { useEffect, useRef } from 'react';

export interface UsePollOptions {
  /** Delay between invocations, in milliseconds. */
  intervalMs: number;
  /** Poll only while true; flipping to false tears the loop down. */
  active: boolean;
  /** Skip a tick if the previous invocation has not settled yet. The run
   *  pollers rely on this so a slow request never stacks overlapping fetches. */
  guardOverlap?: boolean;
  /** Stop after this many completed invocations (the attempt cap). */
  maxAttempts?: number;
  /** Invoke once immediately on activation instead of waiting a full interval.
   *  Preserves the queued-job poller's original leading poll. */
  immediate?: boolean;
  /** Schedule from each invocation's settlement instead of on a fixed interval. */
  mode?: 'interval' | 'settle-relative';
}

export type PollResult = 'stop' | void;

/**
 * Shared frontend poll loop. Replaces the hand-rolled `setInterval` pollers so
 * every loop gets the same three guarantees they previously lacked piecemeal:
 *
 *  - `guardOverlap` — never issue a new request while the last is in flight.
 *  - a **visibilitychange pause** — hidden tabs stop polling entirely (no
 *    background network churn) and catch up with one tick when refocused.
 *  - `maxAttempts` — an optional cap for bounded loops.
 *
 * The default `interval` mode preserves fixed-interval cadence. In
 * `settle-relative` mode, each invocation returns `void` to re-arm after it
 * settles or `'stop'` to end that chain.
 *
 * `fn` is read through a ref, so callers can pass a fresh closure each render
 * without restarting the interval; the loop only re-arms when a listed option
 * changes.
 */
export function usePoll(
  fn: () => PollResult | Promise<PollResult>,
  {
    intervalMs,
    active,
    guardOverlap = false,
    maxAttempts,
    immediate = false,
    mode = 'interval',
  }: UsePollOptions,
): void {
  const fnRef = useRef(fn);
  // Keep the latest closure without re-arming the interval; the loop reads
  // fnRef.current at call time, always after this commit.
  useEffect(() => {
    fnRef.current = fn;
  });

  useEffect(() => {
    if (!active) return undefined;

    if (mode === 'settle-relative') {
      let disposed = false;
      let inFlight = false;
      let attempts = 0;
      let timer: ReturnType<typeof setTimeout> | null = null;

      const hidden = (): boolean =>
        typeof document !== 'undefined' && document.hidden === true;

      function stop(): void {
        disposed = true;
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        if (typeof document !== 'undefined') {
          document.removeEventListener('visibilitychange', onVisibility);
        }
      }

      function arm(): void {
        timer = setTimeout(() => void tick(), intervalMs);
      }

      const tick = async (): Promise<void> => {
        timer = null;
        if (disposed) return;
        // A hidden tick pauses the chain. Refocus supplies the catch-up tick,
        // which re-arms only after its invocation settles.
        if (hidden()) return;
        // Settle-relative scheduling is sequential by construction; the
        // option-level overlap guard is therefore intentionally inert here.
        if (inFlight) return;
        inFlight = true;
        let result: PollResult;
        try {
          result = await fnRef.current();
        } finally {
          inFlight = false;
        }
        attempts += 1;
        if (maxAttempts != null && attempts >= maxAttempts) {
          stop();
          return;
        }
        if (!disposed && result !== 'stop') arm();
      };

      function onVisibility(): void {
        if (disposed || hidden()) return;
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        void tick();
      }

      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', onVisibility);
      }
      if (immediate) void tick();
      else arm();

      return stop;
    }

    let disposed = false;
    let inFlight = false;
    let attempts = 0;
    let timer: ReturnType<typeof setInterval> | null = null;

    const hidden = (): boolean =>
      typeof document !== 'undefined' && document.hidden === true;

    function stop(): void {
      disposed = true;
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    }

    const tick = async (): Promise<void> => {
      if (disposed) return;
      // Visibility pause: a hidden tab must not poll. The tick still fires but
      // does no work, and refocus triggers an immediate catch-up tick.
      if (hidden()) return;
      if (guardOverlap && inFlight) return;
      inFlight = true;
      try {
        await fnRef.current();
      } finally {
        inFlight = false;
      }
      attempts += 1;
      if (maxAttempts != null && attempts >= maxAttempts) {
        stop();
      }
    };

    function onVisibility(): void {
      if (!disposed && !hidden()) void tick();
    }

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }
    timer = setInterval(() => void tick(), intervalMs);
    if (immediate) void tick();

    return stop;
  }, [active, intervalMs, guardOverlap, maxAttempts, immediate, mode]);
}
