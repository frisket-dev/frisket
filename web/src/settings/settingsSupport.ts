import { useCallback, useEffect, useReducer, useRef } from 'react';
import type { ProviderValidateResult } from '../api/open';

export function errMsg(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useLoader<T>(
  load: () => Promise<T>,
  reloadKey: string | number | boolean = '',
): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (value: T) => void;
} {
  type LS = { data: T | null; error: string | null; loading: boolean };
  type LA = { type: 'start' } | { type: 'ok'; value: T } | { type: 'fail'; error: string } | { type: 'put'; value: T };
  const loadRef = useRef(load);
  const [ls, dispatch] = useReducer(
    (s: LS, a: LA): LS => {
      if (a.type === 'start') return { ...s, loading: true };
      if (a.type === 'ok') return { data: a.value, error: null, loading: false };
      if (a.type === 'fail') return { ...s, error: a.error, loading: false };
      return { ...s, data: a.value };
    },
    { data: null, error: null, loading: true },
  );
  useEffect(() => {
    loadRef.current = load;
  });
  const reload = useCallback((): Promise<void> => {
    dispatch({ type: 'start' });
    return loadRef.current().then(
      (value) => { dispatch({ type: 'ok', value }); },
      (caught: unknown) => { dispatch({ type: 'fail', error: errMsg(caught) }); },
    );
  }, []);
  useEffect(() => {
    let alive = true;
    // Microtask deferral + alive check preserve HEAD semantics (StrictMode
    // double-invoke starts at most one live fetch), and reloadKey changes
    // show the loading state again.
    void Promise.resolve().then(() => {
      if (!alive) return;
      dispatch({ type: 'start' });
      return loadRef.current().then(
        (value) => { if (alive) dispatch({ type: 'ok', value }); },
        (caught: unknown) => { if (alive) dispatch({ type: 'fail', error: errMsg(caught) }); },
      );
    });
    return () => { alive = false; };
  }, [reloadKey]);
  return { data: ls.data, error: ls.error, loading: ls.loading, reload, setData: (value: T) => dispatch({ type: 'put', value }) };
}

export function providerValidationMessage(result: ProviderValidateResult): string {
  if (result.ok) return 'Valid — key accepted';
  if (result.reachable) return result.detail || `Reachable but rejected (HTTP ${result.status ?? '?'})`;
  return `Unreachable${result.detail ? `: ${result.detail}` : ''}`;
}

export type ProviderValidationTone = 'success' | 'error' | 'warning';

// A reachable-but-rejected key (e.g. HTTP 401) is a real error the user must
// fix; an unreachable provider is more likely transient/network, so it reads
// as a warning rather than a hard failure.
export function providerValidationTone(result: ProviderValidateResult): ProviderValidationTone {
  if (result.ok) return 'success';
  return result.reachable ? 'error' : 'warning';
}
