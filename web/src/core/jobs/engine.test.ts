// Pure engine-level proof of the epoch/AbortController contract. The
// jobStore-integrated version of these same properties (epoch drop of a stale
// poll resolution, abort on cancel, no overlapping polls) lives in
// state/jobStore.test.ts, which re-asserts the engine contract in context
// rather than relying on this file alone.

import { describe, expect, it, vi } from 'vitest';
import { createJobLane } from './engine';

describe('createJobLane', () => {
  it('starts at epoch 1 for the first job and isCurrent is true for it', () => {
    const lane = createJobLane();
    const job = lane.start();
    expect(job.epoch).toBe(1);
    expect(lane.isCurrent(job)).toBe(true);
    expect(job.signal.aborted).toBe(false);
  });

  it('a later start() supersedes an earlier job — isCurrent(old) becomes false', () => {
    const lane = createJobLane();
    const jobA = lane.start();
    const jobB = lane.start();
    expect(lane.isCurrent(jobA)).toBe(false);
    expect(lane.isCurrent(jobB)).toBe(true);
    expect(jobA.epoch).not.toBe(jobB.epoch);
  });

  it('start() aborts the PREVIOUS job (real cancellation signal)', () => {
    const lane = createJobLane();
    const jobA = lane.start();
    expect(jobA.signal.aborted).toBe(false);
    lane.start();
    expect(jobA.signal.aborted).toBe(true);
  });

  it('cancel() retires the current job with no successor: isCurrent false, signal aborted', () => {
    const lane = createJobLane();
    const job = lane.start();
    lane.cancel();
    expect(lane.isCurrent(job)).toBe(false);
    expect(job.signal.aborted).toBe(true);
  });

  it('cancel() on an idle lane (no job ever started) is a harmless no-op', () => {
    const lane = createJobLane();
    expect(() => lane.cancel()).not.toThrow();
    const job = lane.start();
    expect(lane.isCurrent(job)).toBe(true);
  });

  it('retires an outer start when the displaced abort listener installs a successor', () => {
    const lane = createJobLane();
    const jobA = lane.start();
    let jobC: ReturnType<typeof lane.start> | null = null;
    jobA.signal.addEventListener('abort', () => {
      jobC = lane.start();
    }, { once: true });

    const jobB = lane.start();

    expect(jobA.signal.aborted).toBe(true);
    expect(jobB.signal.aborted).toBe(true);
    expect(lane.isCurrent(jobB)).toBe(false);
    expect(jobC).not.toBeNull();
    expect(jobC!.signal.aborted).toBe(false);
    expect(lane.isCurrent(jobC!)).toBe(true);
    lane.cancel();
    expect(jobC!.signal.aborted).toBe(true);
  });

  it('keeps a cancel-abort successor current, owned, and cancellable', () => {
    const lane = createJobLane();
    const jobA = lane.start();
    let jobC: ReturnType<typeof lane.start> | null = null;
    jobA.signal.addEventListener('abort', () => {
      jobC = lane.start();
    }, { once: true });

    lane.cancel();

    expect(jobC).not.toBeNull();
    expect(jobC!.signal.aborted).toBe(false);
    expect(lane.isCurrent(jobC!)).toBe(true);
    lane.cancel();
    expect(jobC!.signal.aborted).toBe(true);
    expect(lane.isCurrent(jobC!)).toBe(false);
  });

  it('handles recursive cancel from an abort listener and remains reusable', () => {
    const lane = createJobLane();
    const jobA = lane.start();
    jobA.signal.addEventListener('abort', () => lane.cancel(), { once: true });

    expect(() => lane.cancel()).not.toThrow();
    expect(jobA.signal.aborted).toBe(true);
    expect(lane.isCurrent(jobA)).toBe(false);

    const successor = lane.start();
    expect(successor.signal.aborted).toBe(false);
    expect(lane.isCurrent(successor)).toBe(true);
  });

  it('detaches before abort so recursive cancel cannot re-abort one controller', () => {
    const NativeAbortController = AbortController;
    let lane: ReturnType<typeof createJobLane>;
    let abortCalls = 0;
    let reentered = false;
    class RecursiveAbortController extends NativeAbortController {
      override abort(reason?: unknown): void {
        abortCalls += 1;
        if (!reentered) {
          reentered = true;
          lane.cancel();
        }
        super.abort(reason);
      }
    }
    vi.stubGlobal('AbortController', RecursiveAbortController);
    try {
      lane = createJobLane();
      lane.start();
      lane.cancel();
      expect(abortCalls).toBe(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
