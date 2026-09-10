// Action-catalog invalidation bus. Clearing the client's cached
// catalog (ActionCatalogCache, invoked by RealApi after a provider-secret
// save/delete) is only half the job — a MOUNTED surface (the Translate form)
// keeps rendering its stale snapshot until something re-fetches. This tiny
// pub/sub lets the api signal "the catalog may have changed" WITHOUT the api
// depending on React or the FrisketApi interface growing a subscription method:
// real.ts emits, useWorkspaceModel subscribes and re-runs its catalog fetch, so
// a saved DEEPL_API_KEY flips the engine available without a remount.

type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe to catalog-invalidation signals. Returns an unsubscribe fn. */
export function onActionCatalogInvalidated(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Notify subscribers that the cached action catalog was invalidated. */
export function emitActionCatalogInvalidated(): void {
  for (const listener of [...listeners]) {
    listener();
  }
}
