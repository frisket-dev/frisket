import { useEffect, useMemo, useRef, useState } from 'react';
import type { ProviderValidateResult } from '../api/open';
import { errMsg } from './settingsSupport';

type ValidationStatus = { text: string; tone: 'success' | 'error' | 'warning' | 'pending' };

function validationStatus(result: ProviderValidateResult): ValidationStatus {
  if (result.ok) return { text: 'Valid — key accepted', tone: 'success' };
  if (result.reachable) return { text: result.detail || `Reachable but rejected (HTTP ${result.status ?? '?'})`, tone: 'error' };
  return { text: `Unreachable${result.detail ? `: ${result.detail}` : ''}`, tone: 'warning' };
}

/** Status-only checks of stored credentials; tested-key save receipts belong to ProviderKeyForm. */
export function useExistingProviderValidation(
  scope: unknown,
  validate: (provider: string, signal: AbortSignal) => Promise<ProviderValidateResult>,
  allowed = true,
) {
  const session = useMemo(() => ({ scope, allowed }), [scope, allowed]);
  const active = useRef<{ session: typeof session; requests: Map<string, AbortController> } | null>(null);
  const [state, setState] = useState({ session, statuses: {} as Record<string, ValidationStatus> });
  useEffect(() => {
    const owner = { session, requests: new Map<string, AbortController>() };
    active.current = owner;
    return () => {
      for (const request of owner.requests.values()) request.abort();
      active.current = null;
    };
  }, [session]);
  const testExisting = async (provider: string, label?: string) => {
    const owner = active.current;
    if (!allowed || !owner || owner.session !== session) return;
    const requests = owner.requests;
    requests.get(provider)?.abort();
    const request = new AbortController();
    requests.set(provider, request);
    const publish = (status: ValidationStatus) => {
      if (active.current !== owner || request.signal.aborted || requests.get(provider) !== request) return;
      setState((previous) => ({
        session,
        statuses: { ...(previous.session === session ? previous.statuses : {}), [provider]: status },
      }));
    };
    publish({ text: 'Testing...', tone: 'pending' });
    try {
      publish(validationStatus(await validate(provider, request.signal)));
    } catch (error) {
      publish({ text: errMsg(error) || (label ? `Could not test ${label}` : ''), tone: 'error' });
    }
  };
  const existingTests: Record<string, ValidationStatus> = state.session === session ? state.statuses : {};
  return { existingTests, testExisting };
}
