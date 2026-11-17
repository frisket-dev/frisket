import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';
import { errorMessage } from './errorMessage';

export interface LoaderOptions {
  /** Keep the previous `data` and `error` visible while a changed input loads.
   *  By default a changed input reads as loading with no data in the same render. */
  keepPreviousData?: boolean;
  /** Message for a rejected value that is not an `Error`. */
  errorFallback?: string;
  /** When false nothing is fetched: `loading` is false and any in-flight
   *  request is discarded. */
  enabled?: boolean;
}

interface Loader<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Refetch the current inputs, keeping `data` (but not `error`) while it loads. */
  reload: () => Promise<void>;
  /** Replace `data` locally, e.g. with a mutation's returned value. */
  setData: (value: T) => void;
}

interface Settled<T> {
  deps: DependencyList;
  data: T | null;
  error: string | null;
  reloading: boolean;
}

function sameDeps(a: DependencyList, b: DependencyList): boolean {
  return a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
}

/**
 * Fetch `load()` whenever `deps` change, the way a `useEffect` would.
 *
 * Only the latest request settles state: a response for superseded inputs,
 * for a superseded `reload()`, or after unmount is dropped, and its signal is
 * aborted. `load` is read at call time, so it may close over fresh values
 * without being listed in `deps`.
 */
export function useLoader<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  { keepPreviousData = false, errorFallback, enabled = true }: LoaderOptions = {},
): Loader<T> {
  const loadRef = useRef(load);
  const optionsRef = useRef({ errorFallback, keepPreviousData });
  // The inputs of the latest started request; null when none is live.
  const started = useRef<{ deps: DependencyList; enabled: boolean } | null>(null);
  const request = useRef<{ id: number; controller: AbortController | null }>({ id: 0, controller: null });
  const [settled, setSettled] = useState<Settled<T> | null>(null);

  const discard = useCallback(() => {
    request.current.id += 1;
    request.current.controller?.abort();
    request.current.controller = null;
  }, []);

  const start = useCallback((forDeps: DependencyList): Promise<void> => {
    discard();
    const id = request.current.id;
    const controller = new AbortController();
    request.current.controller = controller;
    const current = () => request.current.id === id;
    let pending: Promise<T>;
    try {
      pending = loadRef.current(controller.signal);
    } catch (caught) {
      pending = Promise.reject(caught);
    }
    return pending.then(
      (data) => {
        if (current()) setSettled({ deps: forDeps, data, error: null, reloading: false });
      },
      (caught: unknown) => {
        if (!current()) return;
        const { errorFallback: fallback, keepPreviousData: keep } = optionsRef.current;
        const error = errorMessage(caught, fallback);
        setSettled((previous) => ({
          deps: forDeps,
          data: previous !== null && (keep || sameDeps(previous.deps, forDeps)) ? previous.data : null,
          error,
          reloading: false,
        }));
      },
    );
  }, [discard]);

  // Runs after every render and starts a request only when the inputs
  // changed, so `deps` can be compared element-wise like useEffect's own.
  useEffect(() => {
    loadRef.current = load;
    optionsRef.current = { errorFallback, keepPreviousData };
    const previous = started.current;
    if (previous !== null && previous.enabled === enabled && sameDeps(previous.deps, deps)) return;
    started.current = { deps, enabled };
    if (enabled) void start(deps);
    else discard();
  });
  useEffect(() => () => {
    started.current = null;
    discard();
  }, [discard]);

  const reload = useCallback((): Promise<void> => {
    const live = started.current;
    if (live === null || !live.enabled) return Promise.resolve();
    setSettled((previous) => (previous !== null && sameDeps(previous.deps, live.deps)
      ? { ...previous, error: null, reloading: true }
      : previous));
    return start(live.deps);
  }, [start]);

  const setData = useCallback((value: T) => {
    const live = started.current;
    setSettled((previous) => ({
      deps: live?.deps ?? previous?.deps ?? [],
      data: value,
      error: previous?.error ?? null,
      reloading: previous?.reloading ?? false,
    }));
  }, []);

  const fresh = settled !== null && sameDeps(settled.deps, deps);
  const kept = keepPreviousData && settled !== null;
  return {
    data: fresh || kept ? settled.data : null,
    error: fresh || kept ? settled.error : null,
    loading: enabled && (fresh ? settled.reloading : true),
    reload,
    setData,
  };
}
