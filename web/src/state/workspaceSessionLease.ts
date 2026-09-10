// Generation-scoped workspace teardown. Deactivation invalidates the current
// generation before disposing jobs and route synchronization.

export type WorkspaceSessionGeneration = number;

export interface WorkspaceSessionLeaseDeps {
  /** Disposes jobStore's poll lanes (WorkspaceStores.dispose(), which today
   *  wraps job.dispose() — see createWorkspaceStores.ts). Called every
   *  deactivation, after the generation has already been invalidated. */
  disposeJobLanes(): void;
}

export interface WorkspaceSessionLease {
  /** Current generation, or null if never activated / currently inactive. */
  readonly generation: WorkspaceSessionGeneration | null;
  /** True while a live (non-deactivated) generation exists. */
  readonly isActive: boolean;
  /** Activates or returns the current generation. */
  activate(): WorkspaceSessionGeneration;
  /** True while `gen` is the lease's current live generation. A generation
   *  captured before deactivate() reports false immediately once
   *  deactivate() begins — before any disposal runs — so a joiner can use
   *  this to make a late-arriving callback no-op instead of racing the
   *  abort. */
  isCurrent(gen: WorkspaceSessionGeneration): boolean;
  /** Registers the RouteSyncController's dispose callback for the CURRENT
   *  generation and returns a lease-owned `unregister()` — the caller
   *  (bind/useRouteSyncController.ts) returns it as its OWN effect's
   *  cleanup, replacing the controller's former independent mount-cleanup
   *  lifecycle. `unregister()` DISPOSES the registered controller (idempotent
   *  — a no-op if already disposed, e.g. by a deactivate() that ran first),
   *  so it is safe from either direction: a real unmount/deps-change calls it
   *  directly and tears the controller down; a deactivate() that runs first
   *  disposes it via its own ordered teardown and clears the registration, so
   *  a LATER unregister() call (React's effect cleanup still runs once more)
   *  finds nothing left to do.
   *
   *  Generation-scoped: if the lease is INACTIVE when this is called,
   *  `dispose` is invoked IMMEDIATELY (not stored) and the returned
   *  `unregister()` is a no-op — a join can never outlive or outrun the
   *  generation it belongs to. If a registration is already live for the
   *  current generation (the caller's effect re-ran without an intervening
   *  deactivate()), the PREVIOUS registration is disposed before the new one
   *  is stored, so a dependency rerun never leaks the outgoing controller. */
  joinRouteController(dispose: () => void): () => void;
  /** Invalidates the current generation FIRST (isCurrent(gen) starts
   *  returning false for every generation up to and including this one),
   *  THEN disposes in fixed order: jobStore's poll lanes, then the route
   *  controller (if one joined this generation). Safe to call when already
   *  inactive (idempotent no-op). A deactivated lease can still be
   *  reactivated later via activate(). */
  deactivate(): void;
}

export function createWorkspaceSessionLease(
  deps: WorkspaceSessionLeaseDeps,
): WorkspaceSessionLease {
  let generation: WorkspaceSessionGeneration | null = null;
  let nextGeneration = 1;
  // Tracks BOTH the registered dispose callback and the generation it was
  // registered under, so unregister() (returned to the caller as its effect
  // cleanup) can tell whether it is still the CURRENT registration — a stale
  // unregister from an earlier generation must never clear a later
  // generation's live registration.
  let routeController: { gen: WorkspaceSessionGeneration; dispose: () => void } | null = null;

  /** Disposes and clears the current route-controller registration, if any.
   *  Idempotent (a second call finds nothing to do). Shared by deactivate()
   *  and joinRouteController()'s replace-previous path so both go through
   *  the identical dispose-then-clear sequence. */
  function disposeRouteController(): void {
    if (!routeController) return;
    const { dispose } = routeController;
    routeController = null;
    dispose();
  }

  return {
    get generation() {
      return generation;
    },
    get isActive() {
      return generation !== null;
    },
    activate() {
      if (generation !== null) return generation;
      generation = nextGeneration;
      nextGeneration += 1;
      return generation;
    },
    isCurrent(gen) {
      return generation !== null && gen === generation;
    },
    joinRouteController(dispose) {
      if (generation === null) {
        // Inactive: this join can never be reached by a future deactivate()
        // (there is no live generation for it to belong to), so storing it
        // would either leak or fire against an unrelated later generation.
        // Dispose immediately instead.
        dispose();
        return () => {};
      }
      const gen = generation;
      // A registration already live for THIS generation (the caller's effect
      // re-ran without an intervening deactivate()) is disposed before the
      // new one is stored — otherwise the outgoing controller's subscription
      // leaks.
      disposeRouteController();
      routeController = { gen, dispose };
      return () => {
        // Only clears/disposes if this exact registration is still current —
        // a deactivate() that already ran (clearing routeController itself)
        // makes this a no-op, and a NEWER registration for a later
        // generation is never touched by an older join's unregister.
        if (routeController && routeController.gen === gen && routeController.dispose === dispose) {
          disposeRouteController();
        }
      };
    },
    deactivate() {
      if (generation === null) return; // idempotent: nothing live to tear down
      // Invalidate FIRST: isCurrent() for this generation (and thus any
      // late-arriving callback gated on it) starts returning false before
      // either disposal below runs.
      generation = null;
      deps.disposeJobLanes();
      disposeRouteController();
    },
  };
}
