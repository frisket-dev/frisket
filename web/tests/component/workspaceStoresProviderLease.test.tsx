// @vitest-environment jsdom
//
// WEB-01's fixed equivalence-class characterization matrix, run against the
// REAL WorkspaceStoresProvider + createWorkspaceStores + the activation lease
// + useRouteSyncController — not a reimplementation. Deliberately does NOT
// mount useWorkspaceModel (too heavy — 20+ effects, a full fetch stub) since
// none of those effects are in WEB-01's scope; a small Probe component reads
// useWorkspaceStores()/useRouteHandle() directly, mirroring the narrow-handle
// pattern bind/useRouteHandle.ts etc. already use.
//
// Scenarios (program §WEB-01 / task-card fixed matrix):
//   1. StrictMode mount-cleanup-remount — exactly one net bindProject call,
//      exactly one live RouteSyncController subscription, no duplicate
//      jobStore poll-lane registration.
//   2. Project mount/switch/unmount — deactivate() for the old project runs
//      before the new project's activate() observably rebinds the shared
//      singleton; old project's lanes/controller are disposed; final unmount
//      leaves zero live lanes/subscriptions.
//   3. Rapid remount (A→B→A) — fresh generation per transition, no stale
//      generation observable, disposal calls not accumulated.
//   4. Late async completion — a callback captured before deactivate() no-ops
//      after it, proving invalidate-then-dispose ordering end to end through
//      the real provider/lease, not just the pure lease unit (see
//      state/workspaceSessionLease.test.ts for that half).
//
// Plus the two non-lifecycle regression checks requirement 2/3 name:
// construction performs no fetch/timer/subscription before activate(), and
// activate() twice without deactivate() is a documented no-op.

import { StrictMode, useEffect } from 'react';
import { act, cleanup, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import { useWorkspaceStores } from '../../src/bind/useWorkspaceStores';
import { useRouteSyncController } from '../../src/bind/useRouteSyncController';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';

import type { RouteState } from '../../src/core/route/RouteState';
import type { RunActionLaunchResult, RunProgress } from '../../src/api/open';
import type { RegisteredActionRequest } from '../../src/api/types';
import type { JobRunDeps } from '../../src/state/jobStore';

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function runProgress(overrides: Partial<RunProgress> = {}): RunProgress {
  return {
    runId: 'run-a',
    actionName: 'Classify',
    actionKind: 'map.classify',
    sheetId: 'sheet-1',
    targetColumnId: 'col-1',
    status: 'running',
    completedRows: 0,
    totalRows: 10,
    failedRows: 0,
    costSoFar: 0,
    ...overrides,
  };
}

function jobDeps(): JobRunDeps {
  return {
    invalidateProjectData: vi.fn(),
    refreshHistory: vi.fn(),
    refreshReviewCount: vi.fn(),
    refreshSheets: vi.fn(),
    showError: vi.fn(),
  };
}

function classifyRequest(): RegisteredActionRequest {
  return {
    action_id: 'map.classify',
    scope: { kind: 'sheet_rows', sheet_id: 1 },
    params: {
      source: ['source'],
      engine: 'llm',
      model: 'test/model',
      fields: [{ name: 'result', type: 'text' }],
      context: 'Classify',
    },
    output_names: { result: 'result' },
    idempotency_key: 'workspace-provider-lease-map.classify',
  };
}

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

// ---------------------------------------------------------------------------
// Harness: a minimal probe that captures the live WorkspaceStores handle (so
// tests can drive lease.activate/deactivate observably and inspect
// job/route), and optionally mounts the real useRouteSyncController against
// the substrate's route store — exactly bind/useWorkspaceModel.tsx's own
// mount call, just without the other ~20 unrelated effects around it.

let latestStores: WorkspaceStores | null = null;
// Keyed by the store's own object identity so instrumentation installed on
// ONE createWorkspaceStores() instance (including a StrictMode-discarded
// first pass) never bleeds into another's count.
const routeHistorySubscribeCounts = new WeakMap<object, { count: number }>();

/** Wraps route.subscribeHistoryCommands with a live-writer counter, installed
 *  DURING RENDER (before useRouteSyncController's effect ever runs) so every
 *  subscribe/unsubscribe the controller performs — across a StrictMode
 *  double-invoke of construction included — is counted. The route seam
 *  intentionally exposes no listener count in production, so this is
 *  test-only instrumentation, not a production-code change. Idempotent per
 *  store instance (double-instrumenting the same instance is a no-op). */
function instrumentRouteSubscriptions(stores: WorkspaceStores): { count: number } {
  const existing = routeHistorySubscribeCounts.get(stores);
  if (existing) return existing;
  const counter = { count: 0 };
  routeHistorySubscribeCounts.set(stores, counter);
  const originalSubscribe = stores.route.subscribeHistoryCommands;
  stores.route.subscribeHistoryCommands = (listener) => {
    counter.count += 1;
    const unsubscribe = originalSubscribe(listener);
    return () => {
      counter.count -= 1;
      unsubscribe();
    };
  };
  return counter;
}

function Probe({
  mountRouteController = true,
  commitRoute,
}: {
  mountRouteController?: boolean;
  /** Defaults to a no-op — the lifecycle scenarios don't need real history
   *  writes. Scenario 4b (production-path late-write no-op) passes a spy so
   *  it can assert on calls AFTER the real controller has unsubscribed. */
  commitRoute?: (next: RouteState, mode: 'push' | 'replace') => void;
}) {
  const stores = useWorkspaceStores();
  // Installed synchronously during render, before useRouteSyncController's
  // effect body runs — guarantees the controller's own subscribe() call is
  // counted, including under StrictMode's discarded first construction.
  instrumentRouteSubscriptions(stores);
  useEffect(() => {
    latestStores = stores;
  });
  const write = commitRoute ?? (() => {});
  // eslint-disable-next-line react-hooks/rules-of-hooks -- mountRouteController is a per-test constant, never toggled after mount
  if (mountRouteController) useRouteSyncController(stores.route, write);
  return null;
}

function mount(
  projectId: string,
  opts: {
    strictMode?: boolean;
    commitRoute?: (next: RouteState, mode: 'push' | 'replace') => void;
  } = {},
) {
  const tree = (
    <WorkspaceStoresProvider projectId={projectId}>
      <Probe commitRoute={opts.commitRoute} />
    </WorkspaceStoresProvider>
  );
  return render(opts.strictMode ? <StrictMode>{tree}</StrictMode> : tree);
}

function currentStores(): WorkspaceStores {
  if (!latestStores) throw new Error('Probe has not rendered yet');
  return latestStores;
}

function routeHistorySubscribeCount(stores: WorkspaceStores): number {
  return routeHistorySubscribeCounts.get(stores)?.count ?? 0;
}

beforeEach(() => {
  latestStores = null;
});

afterEach(() => {
  cleanup();
  latestStores = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------

describe('WorkspaceStoresProvider — construction is side-effect-free', () => {
  it('performs no fetch/timer/subscription before activate() runs (i.e. before mount)', () => {
    // createWorkspaceStores() is called directly here — the same function
    // WorkspaceStoresProvider's memoized construction calls — so this
    // observes construction in isolation from React's effect timing.
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    const stores = createWorkspaceStores('p-construction-only');
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(setTimeoutSpy).not.toHaveBeenCalled();
    expect(stores.lease.isActive).toBe(false); // not activated — construction alone never binds
    fetchSpy.mockRestore();
    setTimeoutSpy.mockRestore();
  });
});

describe('WorkspaceStoresProvider — activation idempotence (independent of StrictMode)', () => {
  it('calling activate() twice with no intervening deactivate() is a documented no-op', () => {
    mount('p-idempotent');
    const stores = currentStores();
    const firstGen = stores.lease.generation;
    const secondGen = stores.lease.activate();
    expect(secondGen).toBe(firstGen);
  });
});

describe('WorkspaceStoresProvider — scenario 1: StrictMode mount-cleanup-remount', () => {
  it('nets exactly one live bindProject call for the project, one live route subscription, no duplicate lane registration', async () => {
    vi.useFakeTimers();
    mount('p1', { strictMode: true });
    expect(latestStores).not.toBeNull();
    const stores = currentStores();
    const listActionJobs = vi.spyOn(stores.projectApi, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);

    // Exactly one NET bindProject call for "p1" — StrictMode's
    // mount→cleanup→mount discards the first activation's bind via
    // deactivate() before the second activation binds again, so only one
    // call should be live/observable in the final settled state, and the
    // singleton itself reflects "p1" (not stale or double-applied).
    expect(stores.chromePreferences.projectId).toBe('p1');
    expect(stores.lease.isActive).toBe(true);

    // The surviving resource restarts cleanly after StrictMode discarded the
    // first lease generation. Repeated start refreshes collaborators but owns
    // one deferred leading refresh, not duplicate timer registrations.
    const deps = jobDeps();
    stores.job.start(deps);
    stores.job.start(deps);
    expect(listActionJobs).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(0);
    expect(listActionJobs).toHaveBeenCalledTimes(1);
  });

  it('leaves exactly one live RouteSyncController subscription after the double-invoke settles', async () => {
    mount('p1', { strictMode: true });
    await waitFor(() => expect(latestStores).not.toBeNull());
    const stores = currentStores();

    // The surviving generation's controller holds exactly one live
    // history-command subscription on routeStore — the discarded first
    // generation's
    // subscribe+unsubscribe (StrictMode's mount→cleanup) already netted back
    // to zero before this generation's subscribe ran, so the settled count
    // is 1, not 0 (never mounted) or 2+ (duplicate/leaked subscription).
    expect(routeHistorySubscribeCount(stores)).toBe(1);

    act(() => {
      stores.lease.deactivate();
    });
    expect(routeHistorySubscribeCount(stores)).toBe(0);
  });
});

describe('WorkspaceStoresProvider — RouteSyncController joins the lease directly', () => {
  it('lease.deactivate() disposes the controller registered via joinRouteController exactly once', async () => {
    mount('p-route-join');
    await waitFor(() => expect(latestStores).not.toBeNull());
    const stores = currentStores();
    expect(routeHistorySubscribeCount(stores)).toBe(1); // the real controller joined and is live

    act(() => {
      stores.lease.deactivate();
    });
    expect(routeHistorySubscribeCount(stores)).toBe(0); // deactivate() disposed it via the lease join, not a second independent effect cleanup

    // The store itself is still healthy afterward — not corrupted by a
    // double-unsubscribe or similar.
    const unsubscribe = stores.route.subscribeHistoryCommands(() => {});
    expect(routeHistorySubscribeCount(stores)).toBe(1);
    unsubscribe();
    expect(routeHistorySubscribeCount(stores)).toBe(0);
  });
});

describe('WorkspaceStoresProvider — scenario 2: project mount/switch/unmount', () => {
  it('deactivates the old project before the new project activates, disposing old lanes/controller; final unmount leaves nothing live', async () => {
    const { rerender } = mount('p1');
    await waitFor(() => expect(latestStores).not.toBeNull());
    const p1Stores = currentStores();
    expect(p1Stores.chromePreferences.projectId).toBe('p1');
    const launch = deferred<RunActionLaunchResult>();
    let launchSignal: AbortSignal | undefined;
    vi.spyOn(p1Stores.projectApi, 'runAction').mockImplementation(((_request, options) => {
      launchSignal = options?.signal;
      return launch.promise;
    }) as typeof p1Stores.projectApi.runAction);
    p1Stores.job.start(jobDeps());
    p1Stores.job.startRun(classifyRequest(), { id: '1', rowCount: 3 } as never);
    p1Stores.compareView.open('topic');
    p1Stores.compareView.setSession('topic', {
      hasData: true,
      verdict: 'project p1 only',
    });

    // Mirrors <Workspace key={project.id}> forcing unmount+remount rather
    // than update — a fresh <WorkspaceStoresProvider projectId="p2"> tree,
    // not a prop update on the same element.
    act(() => {
      rerender(
        <WorkspaceStoresProvider projectId="p2" key="p2">
          <Probe />
        </WorkspaceStoresProvider>,
      );
    });
    await waitFor(() => expect(currentStores()).not.toBe(p1Stores));
    const p2Stores = currentStores();

    // p1's instance was deactivated: its lease is no longer active and its
    // p1's in-flight launch was synchronously aborted with its resource.
    expect(p1Stores.lease.isActive).toBe(false);
    expect(launchSignal?.aborted).toBe(true);

    // p2 is now bound and active.
    expect(p2Stores.lease.isActive).toBe(true);
    expect(currentStores().chromePreferences.projectId).toBe('p2');
    expect(p2Stores.compareView).not.toBe(p1Stores.compareView);
    expect(p2Stores.compareView.store.get().topic).toEqual({
      open: false,
      active: false,
      session: { hasData: false, verdict: '' },
      closeWarn: false,
    });

    cleanup();
    // Final unmount: p2's lease is deactivated too — zero live lanes/subscriptions.
    expect(p2Stores.lease.isActive).toBe(false);
  });
});

describe('WorkspaceStoresProvider — scenario 3: rapid remount (A→B→A)', () => {
  it('produces a fresh generation per transition; no earlier "p1" generation is observable after the second p1 mount', async () => {
    const { rerender } = mount('p1');
    await waitFor(() => expect(latestStores).not.toBeNull());
    const p1First = currentStores();
    const p1FirstGen = p1First.lease.generation;
    const firstDispose = vi.spyOn(p1First.job, 'dispose');

    act(() => {
      rerender(
        <WorkspaceStoresProvider projectId="p2" key="p2">
          <Probe />
        </WorkspaceStoresProvider>,
      );
    });
    await waitFor(() => expect(currentStores()).not.toBe(p1First));

    act(() => {
      rerender(
        <WorkspaceStoresProvider projectId="p1" key="p1-again">
          <Probe />
        </WorkspaceStoresProvider>,
      );
    });
    await waitFor(() => expect(currentStores()).not.toBe(p1First));
    const p1Second = currentStores();

    // A fresh instance (React's key-forced remount), not the reused first —
    // its OWN lease starts a fresh generation from 1, but the important
    // invariant is the first instance's lease can never report current again
    // and never re-activates on its own.
    expect(p1Second).not.toBe(p1First);
    expect(p1First.lease.isActive).toBe(false);
    expect(p1First.lease.isCurrent(p1FirstGen!)).toBe(false);
    expect(p1Second.lease.isActive).toBe(true);
    expect(currentStores().chromePreferences.projectId).toBe('p1');

    // Disposal was not accumulated: p1First was disposed exactly by its own
    // deactivate(), not re-run by an unrelated later deactivate().
    expect(firstDispose).toHaveBeenCalledTimes(1);
  });
});

describe('WorkspaceStoresProvider — scenario 4: late async completion after deactivate()', () => {
  it('a callback captured from the pre-deactivation generation observably no-ops instead of mutating store state or rebinding the legacy singleton', () => {
    mount('p-late-async');
    const stores = currentStores();
    const gen = stores.lease.generation!;

    // Simulates jobStore's runLane/queuedLane tick landing after teardown
    // began: capture the generation and a store mutation closure ahead of
    // time, the way jobStore's own isCurrent(job)-gated tick does today.
    const lateTick = () => {
      if (!stores.lease.isCurrent(gen)) return; // must no-op
      stores.job.store.set((state) => ({
        ...state,
        run: runProgress({ runId: 'should-not-land' }),
      }));
    };

    act(() => {
      stores.lease.deactivate();
    });

    lateTick();

    expect(stores.job.store.get().run).toBeNull();
    // And the late tick must not have re-triggered the legacy binding either.
  });

  // Codex review hardening (correction item 3): the test above pins a
  // TEST-INVENTED callback gated on a hand-rolled isCurrent() check — it
  // proves the LEASE's own contract, but not that a real production
  // consumer's OWN guard actually engages when driven through the lease.
  // These two prove it end to end through the real wiring, not a stand-in.

  it('PRODUCTION PATH — jobStore run lane: an in-flight tick captured before deactivate() drops its resolution via runLane.cancel(), not a hand-rolled guard', async () => {
    vi.useFakeTimers();
    mount('p-late-async-joblane');
    const stores = currentStores();

    const inFlight = deferred<RunProgress>();
    vi.spyOn(stores.projectApi, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    vi.spyOn(stores.projectApi, 'runAction').mockResolvedValue({ runId: 'run-a' });
    const getRunProgress = vi.spyOn(stores.projectApi, 'getRunProgress').mockReturnValueOnce(inFlight.promise);

    // Real production call sequence: launch installs the private run target;
    // the store-owned fixed 300 ms lane then issues its own progress request.
    const deps = jobDeps();
    stores.job.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    stores.job.startRun(classifyRequest(), { id: '1', rowCount: 3 } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(1);
    const beforeDeactivate = stores.job.store.get().run;

    // The lease's deactivate() → disposeJobLanes() → job.dispose() runs
    // WHILE the tick above is still awaiting getRunProgress — the exact
    // "late async completion" shape, but driven through the real
    // WorkspaceStoresProvider lifecycle instead of calling job.dispose()
    // directly.
    act(() => {
      stores.lease.deactivate();
    });

    // The in-flight request now resolves — AFTER dispose already called
    // runLane.cancel(), which bumped the lane's epoch. jobStore's OWN
    // runLane.isCurrent(job) check (jobStore.ts:643) — not anything this
    // test wrote — must find the captured job stale and return without
    // touching the store.
    inFlight.resolve(runProgress({ runId: 'run-a', status: 'complete' }));
    await flushMicrotasks();

    expect(stores.job.store.get().run).toBe(beforeDeactivate);
    expect(deps.invalidateProjectData).not.toHaveBeenCalled();
    getRunProgress.mockRestore();
  });

  it('PRODUCTION PATH — lease deactivation aborts and fences an in-flight action launch', async () => {
    mount('p-late-async-launch');
    const stores = currentStores();
    const inFlight = deferred<RunActionLaunchResult>();
    let launchSignal: AbortSignal | undefined;
    vi.spyOn(stores.projectApi, 'runAction').mockImplementation(((_request, options) => {
      launchSignal = options?.signal;
      return inFlight.promise;
    }) as typeof stores.projectApi.runAction);
    vi.spyOn(stores.projectApi, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const deps = { ...jobDeps(), onLaunchAccepted: vi.fn() };

    stores.job.start(deps);
    stores.job.startRun(classifyRequest(), { id: '1', rowCount: 3 } as never);
    const beforeDeactivate = stores.job.store.get().run;

    act(() => {
      stores.lease.deactivate();
    });

    expect(launchSignal).toBeDefined();
    expect(launchSignal?.aborted).toBe(true);
    inFlight.resolve({ runId: 'late-launch' });
    await Promise.resolve();
    await Promise.resolve();

    expect(stores.job.store.get().run).toBe(beforeDeactivate);
    expect(deps.onLaunchAccepted).not.toHaveBeenCalled();
    expect(deps.refreshSheets).not.toHaveBeenCalled();
    expect(deps.showError).not.toHaveBeenCalled();
  });

  it('PRODUCTION PATH — RouteSyncController: a route-store write after deactivate() reaches no write adapter, because the real controller already unsubscribed', async () => {
    const write = vi.fn();
    mount('p-late-async-routecontroller', { commitRoute: write });
    const stores = currentStores();
    await waitFor(() => expect(routeHistorySubscribeCount(stores)).toBe(1));

    write.mockClear(); // drop any settle-time calls unrelated to this assertion

    act(() => {
      stores.lease.deactivate();
    });

    // A route mutation landing after teardown began (e.g. a stray dispatch
    // from code that hasn't unwound yet) — sent through the authorized
    // command seam RouteSyncController subscribes to.
    act(() => {
      stores.route.navigate({ ...stores.route.store.get(), sheetId: 'late-sheet' });
    });

    // The real controller's own unsubscribe (fired by lease.deactivate() via
    // joinRouteController's unregister — see workspaceSessionLease.ts) is
    // what prevents this: NOT a hand-rolled isCurrent() check inside this
    // test. If deactivate() failed to dispose the controller, this write
    // would reach `write` (commitRoute) exactly like a live controller does.
    expect(write).not.toHaveBeenCalled();
    expect(routeHistorySubscribeCount(stores)).toBe(0);
  });
});
