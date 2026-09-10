// @vitest-environment jsdom
//
// Unit coverage for the shared poll loop (src/hooks/usePoll.ts), the inventory's
// named target for the usepoll surface. NOTE: usepoll-stale-target.spec.ts
// itself stays KEEP-NON-CORE — its bug is cross-surface choreography (a Copilot
// dual-proposal launch replacing jobStore's private run target while a stale
// run-A tick is in flight), which belongs to the project job resource, not
// usePoll. Here we pin usePoll's own guarantees: immediate lead tick,
// interval cadence, the maxAttempts cap, guardOverlap, and teardown on inactive.

import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePoll } from '../../src/hooks/usePoll';

beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
});
afterEach(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.useRealTimers();
});

describe('usePoll', () => {
  it('does nothing while inactive', () => {
    const fn = vi.fn();
    renderHook(() => usePoll(fn, { intervalMs: 100, active: false }));
    vi.advanceTimersByTime(500);
    expect(fn).not.toHaveBeenCalled();
  });

  it('fires an immediate lead tick then on each interval', () => {
    const fn = vi.fn();
    renderHook(() => usePoll(fn, { intervalMs: 100, active: true, immediate: true }));
    expect(fn).toHaveBeenCalledTimes(1); // lead tick
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(200);
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it('stops after maxAttempts completed invocations', async () => {
    const fn = vi.fn(() => Promise.resolve());
    renderHook(() => usePoll(fn, { intervalMs: 100, active: true, maxAttempts: 2 }));
    // async advance flushes each tick's microtask (attempts++/stop) before the
    // next interval — the cap engages once an invocation actually settles.
    await vi.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('tears the loop down when active flips to false', () => {
    const fn = vi.fn();
    const { rerender } = renderHook(
      ({ active }: { active: boolean }) => usePoll(fn, { intervalMs: 100, active }),
      { initialProps: { active: true } },
    );
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(1);
    rerender({ active: false });
    vi.advanceTimersByTime(500);
    expect(fn).toHaveBeenCalledTimes(1); // no further ticks after teardown
  });

  it('guardOverlap skips a tick while the previous invocation is still in flight', async () => {
    let resolveInFlight: () => void = () => {};
    const fn = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveInFlight = resolve;
        }),
    );
    renderHook(() => usePoll(fn, { intervalMs: 100, active: true, guardOverlap: true }));

    vi.advanceTimersByTime(100); // tick 1 starts, stays in flight
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(100); // tick 2 is guarded away — tick 1 not settled
    expect(fn).toHaveBeenCalledTimes(1);

    resolveInFlight();
    await Promise.resolve();
    vi.advanceTimersByTime(100); // now free to run again
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('pauses interval ticks while hidden and catches up exactly once on refocus', () => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    const fn = vi.fn();
    renderHook(() => usePoll(fn, { intervalMs: 100, active: true, immediate: true }));

    vi.advanceTimersByTime(500);
    expect(fn).not.toHaveBeenCalled();

    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('cleans up the interval exactly on unmount', () => {
    const fn = vi.fn();
    const { unmount } = renderHook(() =>
      usePoll(fn, { intervalMs: 100, active: true }),
    );
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(1);

    unmount();
    vi.advanceTimersByTime(500);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("ignores a 'stop' return in default interval mode", async () => {
    const fn = vi.fn(() => 'stop' as const);
    renderHook(() => usePoll(fn, { intervalMs: 100, active: true, immediate: true }));

    await vi.advanceTimersByTimeAsync(300);
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it('settle-relative mode re-arms only after the invocation settles', async () => {
    let resolveInFlight: () => void = () => {};
    const fn = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveInFlight = resolve;
        }),
    );
    const { unmount } = renderHook(() =>
      usePoll(fn, {
        mode: 'settle-relative',
        intervalMs: 100,
        active: true,
        immediate: true,
      }),
    );

    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(500);
    expect(fn).toHaveBeenCalledTimes(1);

    resolveInFlight();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(99);
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fn).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("settle-relative mode does not re-arm the branch that returns 'stop'", async () => {
    const fn = vi.fn(() => 'stop' as const);
    renderHook(() =>
      usePoll(fn, {
        mode: 'settle-relative',
        intervalMs: 100,
        active: true,
        immediate: true,
      }),
    );

    await vi.advanceTimersByTimeAsync(500);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('keeps the completed-attempt cap in settle-relative mode', async () => {
    const fn = vi.fn(
      () => new Promise<void>((resolve) => setTimeout(resolve, 150)),
    );
    renderHook(() =>
      usePoll(fn, {
        mode: 'settle-relative',
        intervalMs: 100,
        active: true,
        immediate: true,
        maxAttempts: 2,
      }),
    );

    // Tick 1 settles at 150ms, so settle-relative mode cannot issue tick 2
    // until 250ms. Interval mode would already have called at 100ms and
    // 200ms, making this assertion fail with three calls.
    await vi.advanceTimersByTimeAsync(249);
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fn).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('settle-relative hidden ticks pause the chain until one refocus catch-up', async () => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    const fn = vi.fn(
      () => new Promise<void>((resolve) => setTimeout(resolve, 60)),
    );
    const { unmount } = renderHook(() =>
      usePoll(fn, {
        mode: 'settle-relative',
        intervalMs: 100,
        active: true,
        immediate: true,
      }),
    );

    await vi.advanceTimersByTimeAsync(500);
    expect(fn).not.toHaveBeenCalled();

    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
    await Promise.resolve();
    expect(fn).toHaveBeenCalledTimes(1);
    // The catch-up settles 60ms after refocus and only then starts the 100ms
    // delay. Interval mode would call again at the next fixed 100ms boundary.
    await vi.advanceTimersByTimeAsync(159);
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fn).toHaveBeenCalledTimes(2);
    unmount();
  });
});
