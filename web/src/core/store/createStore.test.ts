// Pure vitest coverage for the createStore primitive. `bind/useSelector` gets NO
// vitest test — it is a thin wrapper over React's own
// `use-sync-external-store/with-selector`, and its hook-rendering correctness is
// not testable in this environment: 'node' config.

import { describe, expect, it, vi } from 'vitest';
import { createStore } from './createStore';

describe('createStore', () => {
  it('returns the initial snapshot from get()', () => {
    const store = createStore({ count: 0 });
    expect(store.get()).toEqual({ count: 0 });
  });

  it('replaces state with a value and notifies listeners', () => {
    const store = createStore({ count: 0 });
    const listener = vi.fn();
    store.subscribe(listener);

    store.set({ count: 1 });

    expect(store.get()).toEqual({ count: 1 });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('replaces state with a (prev) => next updater', () => {
    const store = createStore({ count: 0 });
    store.set((prev) => ({ count: prev.count + 1 }));
    store.set((prev) => ({ count: prev.count + 1 }));
    expect(store.get()).toEqual({ count: 2 });
  });

  it('reference-equality bail: a value that is Object.is-equal to the current snapshot is a no-op', () => {
    const initial = { count: 0 };
    const store = createStore(initial);
    const listener = vi.fn();
    store.subscribe(listener);

    store.set(initial); // same reference
    expect(listener).not.toHaveBeenCalled();

    store.set((prev) => prev); // updater returns the same reference
    expect(listener).not.toHaveBeenCalled();

    // A structurally-equal but distinct object reference is NOT a bail —
    // the store compares references only (Object.is), never deep-equality.
    store.set({ count: 0 });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('subscribe returns an unsubscribe function that stops future notifications', () => {
    const store = createStore(0);
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);

    store.set(1);
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
    store.set(2);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('supports multiple independent listeners', () => {
    const store = createStore(0);
    const a = vi.fn();
    const b = vi.fn();
    store.subscribe(a);
    store.subscribe(b);

    store.set(1);

    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('a listener may unsubscribe itself during notify without breaking the other listeners (listener-copy iteration)', () => {
    const store = createStore(0);
    const a = vi.fn(() => unsubscribeA());
    const b = vi.fn();
    const unsubscribeA = store.subscribe(a);
    store.subscribe(b);

    store.set(1);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);

    store.set(2);
    expect(a).toHaveBeenCalledTimes(1); // unsubscribed after the first notify
    expect(b).toHaveBeenCalledTimes(2);
  });

  it('a listener may subscribe a new listener during notify without that new listener firing for the in-flight notification', () => {
    const store = createStore(0);
    const late = vi.fn();
    const a = vi.fn(() => store.subscribe(late));
    store.subscribe(a);

    store.set(1);
    expect(a).toHaveBeenCalledTimes(1);
    expect(late).not.toHaveBeenCalled(); // added mid-notify; the copy already excluded it

    store.set(2);
    expect(late).toHaveBeenCalledTimes(1); // fires for the next notify
  });
});
