// Characterizes state/workspaceSessionLease.ts's generation/activation
// protocol directly (framework-free — no React, no StrictMode harness needed
// here; the StrictMode replay itself is characterized against the real
// WorkspaceStoresProvider component in
// tests/component/workspaceSessionLease.strictMode.test.tsx).
//
// WEB-01's fixed equivalence-class matrix, the parts this pure-logic layer
// owns: activation idempotence, restart-after-deactivate, ordered dispose
// (jobStore lanes THEN route controller), and "late async completion" —
// invalidate-generation-first semantics via isCurrent().

import { describe, expect, it, vi } from 'vitest';
import { createWorkspaceSessionLease } from './workspaceSessionLease';

function makeDeps() {
  return {
    disposeJobLanes: vi.fn(),
  };
}

describe('createWorkspaceSessionLease — activate()', () => {
  it('returns one live generation', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const gen = lease.activate();
    expect(gen).toBe(1);
    expect(lease.isActive).toBe(true);
    expect(lease.generation).toBe(1);
  });

  it('is idempotent without an intervening deactivate()', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const first = lease.activate();
    const second = lease.activate();
    expect(second).toBe(first);
  });

  it('restarts after deactivate() with a fresh generation', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const first = lease.activate();
    lease.deactivate();
    const second = lease.activate();
    expect(second).toBeGreaterThan(first);
  });
});

describe('createWorkspaceSessionLease — StrictMode mount-cleanup-remount replay', () => {
  it('nets exactly one live generation for mount→cleanup→mount', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    // Simulates React StrictMode's synchronous double-invoke of one effect:
    // mount, cleanup, mount again — driven at the lease level since this is
    // exactly the sequence WorkspaceStoresProvider's effect performs.
    lease.activate(); // first mount
    lease.deactivate(); // cleanup
    const finalGen = lease.activate(); // second (real) mount
    expect(lease.isActive).toBe(true);
    expect(lease.generation).toBe(finalGen);
    expect(deps.disposeJobLanes).toHaveBeenCalledTimes(1); // only the discarded first generation was torn down
  });
});

describe('createWorkspaceSessionLease — deactivate()', () => {
  it('is idempotent: calling it when never activated is a safe no-op', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    expect(() => lease.deactivate()).not.toThrow();
    expect(deps.disposeJobLanes).not.toHaveBeenCalled();
  });

  it('is idempotent: calling it twice in a row disposes job lanes only once', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    lease.activate();
    lease.deactivate();
    lease.deactivate();
    expect(deps.disposeJobLanes).toHaveBeenCalledTimes(1);
  });

  it('disposes in fixed order: jobStore lanes THEN the joined route controller', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const order: string[] = [];
    deps.disposeJobLanes.mockImplementation(() => order.push('jobLanes'));
    const routeDispose = vi.fn(() => order.push('routeController'));

    lease.activate();
    lease.joinRouteController(routeDispose);
    lease.deactivate();

    expect(order).toEqual(['jobLanes', 'routeController']);
    expect(routeDispose).toHaveBeenCalledTimes(1);
  });

  it('disposes job lanes even when no route controller ever joined', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    lease.activate();
    expect(() => lease.deactivate()).not.toThrow();
    expect(deps.disposeJobLanes).toHaveBeenCalledTimes(1);
  });

  it('invalidates the generation before either disposal runs (invalidate-first ordering)', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const gen = lease.activate();
    let isCurrentDuringJobDispose: boolean | null = null;
    let isCurrentDuringRouteDispose: boolean | null = null;
    deps.disposeJobLanes.mockImplementation(() => {
      isCurrentDuringJobDispose = lease.isCurrent(gen);
    });
    const routeDispose = vi.fn(() => {
      isCurrentDuringRouteDispose = lease.isCurrent(gen);
    });
    lease.joinRouteController(routeDispose);

    lease.deactivate();

    expect(isCurrentDuringJobDispose).toBe(false);
    expect(isCurrentDuringRouteDispose).toBe(false);
  });
});

describe('createWorkspaceSessionLease — late async completion after deactivate()', () => {
  it('a callback captured from a stale generation observes isCurrent()=false and can no-op instead of racing the abort', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const gen = lease.activate();

    // Simulates a poll tick / route-store notification captured BEFORE
    // deactivate() began (e.g. jobStore's runLane/queuedLane tick, or a
    // RouteSyncController write queued ahead of teardown).
    const staleCommit = vi.fn((mutateStore: () => void) => {
      if (!lease.isCurrent(gen)) return; // must no-op, not mutate
      mutateStore();
    });

    lease.deactivate();

    const mutateStore = vi.fn();
    staleCommit(mutateStore);
    expect(mutateStore).not.toHaveBeenCalled();
  });

  it('a callback captured from the CURRENT generation still commits', () => {
    const deps = makeDeps();
    const lease = createWorkspaceSessionLease(deps);
    const gen = lease.activate();

    const commit = vi.fn((mutateStore: () => void) => {
      if (!lease.isCurrent(gen)) return;
      mutateStore();
    });

    const mutateStore = vi.fn();
    commit(mutateStore);
    expect(mutateStore).toHaveBeenCalledTimes(1);
  });
});

describe('createWorkspaceSessionLease — rapid remount (A→B→A generation isolation)', () => {
  it('produces a fresh generation per activation; an earlier generation never reports current again', () => {
    const deps = makeDeps();
    const leaseA1 = createWorkspaceSessionLease(deps);
    const genA1 = leaseA1.activate(); // "A" mounts
    leaseA1.deactivate(); // "A" unmounts (projectId flap to "B")

    const leaseB = createWorkspaceSessionLease(makeDeps());
    leaseB.activate(); // "B" mounts
    leaseB.deactivate(); // "B" unmounts (flap back to "A")

    const genA2 = leaseA1.activate(); // "A" remounts — SAME underlying lease instance in this test's harness

    expect(genA2).toBeGreaterThan(genA1);
    expect(leaseA1.isCurrent(genA1)).toBe(false);
    expect(leaseA1.isCurrent(genA2)).toBe(true);
  });
});
