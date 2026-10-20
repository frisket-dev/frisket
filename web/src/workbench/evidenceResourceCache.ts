interface EvidenceEntry<Snapshot> {
  snapshot: Snapshot;
  listeners: Set<() => void>;
  started: boolean;
}

/** Evidence reads share requests within one scoped API, never across project APIs. */
export function createEvidenceResourceCache<Snapshot>(pending: Snapshot, error: Snapshot, capacity: number) {
  const scopes = new WeakMap<object, Map<string, EvidenceEntry<Snapshot>>>();

  function trim(cache: Map<string, EvidenceEntry<Snapshot>>) {
    for (const [key, entry] of cache) {
      if (cache.size <= capacity) break;
      if (entry.listeners.size === 0) cache.delete(key);
    }
  }

  return {
    read(scope: object, key: string): Snapshot {
      return scopes.get(scope)?.get(key)?.snapshot ?? pending;
    },
    subscribe(scope: object, key: string, listener: () => void, load: () => Promise<Snapshot>) {
      let cache = scopes.get(scope);
      if (!cache) {
        cache = new Map();
        scopes.set(scope, cache);
      }
      let entry = cache.get(key);
      if (!entry) {
        entry = { snapshot: pending, listeners: new Set(), started: false };
        cache.set(key, entry);
      }
      // Protect this subscriber before trimming: all older entries may be active.
      entry.listeners.add(listener);
      trim(cache);
      const requestEntry = entry;
      const requestCache = cache;
      const publish = (snapshot: Snapshot) => {
        // An evicted request must not write into a newer entry for the same key.
        if (requestCache.get(key) !== requestEntry) return;
        requestEntry.snapshot = snapshot;
        for (const notify of requestEntry.listeners) notify();
      };
      if (!entry.started) {
        entry.started = true;
        void load().then(publish, () => publish(error));
      }
      return () => {
        requestEntry.listeners.delete(listener);
        trim(requestCache);
      };
    },
  };
}
