// Reference-equality bail on write; a plain Set of listeners; snapshot is the
// current state object (immutable-by-convention — callers replace, never
// mutate).

import type { Store, Listener } from './types';

export function createStore<S>(initial: S): Store<S> {
  let state = initial;
  const listeners = new Set<Listener>();
  return {
    get: () => state,
    set: (next) => {
      const value =
        typeof next === 'function' ? (next as (prev: S) => S)(state) : next;
      if (Object.is(value, state)) return; // reference-equality bail
      state = value;
      // Copy before iterating: a listener may unsubscribe during notify.
      for (const l of [...listeners]) l();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
