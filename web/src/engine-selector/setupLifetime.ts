import { useCallback, useEffect, useRef } from 'react';

/** Both abort the transport and reject completions from transports that ignore it. */
export function useSetupRequests() {
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const invalidate = useCallback(() => {
    generation.current += 1;
    controller.current?.abort();
    controller.current = null;
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; invalidate(); };
  }, [invalidate]);
  const begin = useCallback(() => {
    invalidate();
    const current = generation.current;
    const request = new AbortController();
    controller.current = request;
    return {
      signal: request.signal,
      isCurrent: () => mounted.current && current === generation.current && !request.signal.aborted,
    };
  }, [invalidate]);
  return { begin, invalidate };
}

export function useSetupEditing(onEditingChange?: (editing: boolean) => void) {
  const callback = useRef(onEditingChange);
  useEffect(() => { callback.current = onEditingChange; });
  useEffect(() => () => callback.current?.(false), []);
  return (editing: boolean) => callback.current?.(editing);
}

export function safeSetupDetail(detail: string | null | undefined, secrets: string[]): string {
  return secrets.filter(Boolean).reduce(
    (message, secret) => message.split(secret).join('[redacted]'),
    detail || 'The service did not accept these credentials.',
  );
}
