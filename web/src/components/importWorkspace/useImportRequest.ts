import { useCallback, useEffect, useMemo, useRef } from 'react';

/** One replaceable browser request. Server jobs and committed uploads have
 * their own lifetimes; this request is aborted on replacement, project change,
 * or unmount. Consumers check its signal before publishing a response. */
export function useImportRequest(projectId: string) {
  const current = useRef<AbortController | null>(null);
  const abort = useCallback(() => {
    current.current?.abort();
    current.current = null;
  }, []);
  const begin = useCallback(() => {
    abort();
    const controller = new AbortController();
    current.current = controller;
    return controller;
  }, [abort]);

  useEffect(() => abort, [abort, projectId]);
  return useMemo(() => ({ begin, abort }), [begin, abort]);
}
