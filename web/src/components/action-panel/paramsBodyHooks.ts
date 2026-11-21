import { useCallback, useEffect, useRef } from 'react';

/** Returns a stable updater that merges partial edits into the latest Params
 * snapshot so back-to-back updates in one event do not overwrite each other. */
export function useMergedParams<P extends object>(
  params: P,
  setParams: (next: P) => void,
  finalize?: (next: P, patch: Partial<P>) => void,
): (patch: Partial<P>) => void {
  const paramsRef = useRef(params);
  const finalizeRef = useRef(finalize);
  useEffect(() => {
    paramsRef.current = params;
    finalizeRef.current = finalize;
  }, [params, finalize]);
  return useCallback((patch: Partial<P>) => {
    const next = { ...paramsRef.current, ...patch };
    finalizeRef.current?.(next, patch);
    paramsRef.current = next;
    setParams(next);
  }, [setParams]);
}

/** Runs `onChange` only when `key` changes after mount, leaving saved Params
 * untouched on initial render. */
export function useOnKeyChange<K>(key: K, onChange: () => void): void {
  const previous = useRef(key);
  const callback = useRef(onChange);
  useEffect(() => { callback.current = onChange; });
  useEffect(() => {
    if (previous.current === key) return;
    previous.current = key;
    callback.current();
  }, [key]);
}
