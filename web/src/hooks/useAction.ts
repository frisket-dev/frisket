import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from './errorMessage';

export interface ActionOptions {
  /** Message for a rejected value that is not an `Error`. */
  errorFallback?: string;
}

export interface Action {
  /** True while the latest `run` is in flight. */
  busy: boolean;
  /** The latest failed run's message; cleared when the next run starts. */
  error: string | null;
  setError: (error: string | null) => void;
  /**
   * Run `task`, owning busy and error. Resolves to the task's value, or to
   * `undefined` when it failed (the failure is in `error`). Only the latest
   * run updates state, and nothing updates after unmount.
   */
  run: <R>(task: () => Promise<R>) => Promise<R | undefined>;
}

export function useAction({ errorFallback }: ActionOptions = {}): Action {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(0);
  const fallbackRef = useRef(errorFallback);
  useEffect(() => {
    fallbackRef.current = errorFallback;
  });
  useEffect(() => () => { latest.current += 1; }, []);

  const run = useCallback(async <R,>(task: () => Promise<R>): Promise<R | undefined> => {
    const id = ++latest.current;
    setBusy(true);
    setError(null);
    try {
      return await task();
    } catch (caught) {
      if (latest.current === id) setError(errorMessage(caught, fallbackRef.current));
      return undefined;
    } finally {
      if (latest.current === id) setBusy(false);
    }
  }, []);

  return { busy, error, setError, run };
}
