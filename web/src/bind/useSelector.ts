// The only React-facing adapter over core/store/Store<S>. Wraps React's own
// `use-sync-external-store/with-selector` (vendored runtime dependency) — it
// owns the getSnapshot caching/bailout discipline concurrent-mode rendering
// requires; this hook's job is only to adapt the read side of Store<S>'s shape to that API. Do
// not hand-roll a cache-ref implementation here.
//
// Mandate: any call site selecting a non-primitive (object/array) MUST pass
// shallowEqual as isEqual — Object.is (the default) only works for primitive
// selections.

import { useSyncExternalStoreWithSelector } from 'use-sync-external-store/with-selector';
import type { Store, Selector, IsEqual } from '../core/store/types';

export function useSelector<S, T>(
  store: Pick<Store<S>, 'get' | 'subscribe'>,
  selector: Selector<S, T>,
  isEqual: IsEqual<T> = Object.is,
): T {
  // getServerSnapshot === getSnapshot: this app is CSR-only, no hydration fork.
  return useSyncExternalStoreWithSelector(
    store.subscribe,
    store.get,
    store.get,
    selector,
    isEqual,
  );
}
