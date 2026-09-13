import { useState, type FormEvent } from 'react';
import type { ProviderValidateResult } from '../api/types';
import { PanelSelect } from '../components/PanelSelect';
import { safeSetupDetail, useSetupEditing, useSetupRequests } from './setupLifetime';

export interface ProviderKeyFormProps<Result = void> {
  provider?: string;
  providers?: ReadonlyArray<{ id: string; label: string }>;
  canMutate: boolean;
  keyLabel?: string;
  spendCap?: boolean;
  compact?: boolean;
  validate(provider: string, key: string, signal: AbortSignal): Promise<ProviderValidateResult>;
  save(provider: string, key: string, receipt: string, cap: number | null, signal: AbortSignal): Promise<Result>;
  onSaved(result: Result): void;
  onEditingChange?(editing: boolean): void;
}

/** Shared candidate/receipt controls for selector setup and provider Settings. */
export function ProviderKeyForm<Result = void>({ provider: fixedProvider, providers = [], canMutate,
  keyLabel = 'API key', spendCap = false, compact = false, validate, save, onSaved, onEditingChange,
}: ProviderKeyFormProps<Result>) {
  const [provider, setProvider] = useState(fixedProvider ?? providers[0]?.id ?? 'anthropic');
  const [key, setKey] = useState('');
  const [cap, setCap] = useState('');
  const [receipt, setReceipt] = useState<string | null>(null);
  const [busy, setBusy] = useState<'test' | 'save' | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { begin, invalidate } = useSetupRequests();
  const editing = useSetupEditing(onEditingChange);
  const clearValidation = () => {
    invalidate(); setReceipt(null); setMessage(null); setError(null); setBusy(null);
  };
  const test = async () => {
    if (!canMutate || !key.trim() || busy === 'save') return;
    const request = begin();
    const candidate = key.trim();
    setBusy('test'); setReceipt(null); setMessage(null); setError(null);
    try {
      const result = await validate(provider, candidate, request.signal);
      if (!request.isCurrent()) return;
      if (result.ok && result.validation_token) {
        setReceipt(result.validation_token); setMessage('Valid — key accepted');
      } else {
        setError(safeSetupDetail(result.detail, [candidate]));
      }
    } catch {
      if (request.isCurrent()) setError('Could not test the key. Try again.');
    } finally {
      if (request.isCurrent()) setBusy(null);
    }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canMutate || !receipt || !key.trim() || busy) return;
    const numericCap = cap.trim() ? Number(cap) : null;
    if (numericCap !== null && (!Number.isFinite(numericCap) || numericCap < 0)) {
      setError('Spend cap must be a nonnegative amount in US dollars.'); return;
    }
    const request = begin(); setBusy('save'); setError(null);
    try {
      const result = await save(provider, key.trim(), receipt, numericCap, request.signal);
      if (!request.isCurrent()) return;
      setKey(''); setReceipt(null); setMessage('Provider key saved'); editing(false); onSaved(result);
    } catch {
      if (request.isCurrent()) { setReceipt(null); setError('Could not confirm the save. Recheck before trying again.'); }
    } finally {
      if (request.isCurrent()) setBusy(null);
    }
  };
  const compactSubmit = (event: FormEvent) => {
    if (receipt) { void submit(event); return; }
    event.preventDefault(); void test();
  };
  const actionLabel = busy === 'test' ? 'Testing…' : busy === 'save' ? 'Saving…' : receipt ? 'Save' : 'Test';
  return <form className={`settings-form settings-inline-form settings-provider-key-form${compact ? ' settings-provider-key-form--compact' : ''}`} autoComplete="off" onSubmit={compact ? compactSubmit : submit} onFocus={() => editing(true)}>
    {!fixedProvider && <label><span>Provider</span><PanelSelect aria-label="Provider" value={provider} disabled={!canMutate || busy === 'save'} onChange={(event) => { clearValidation(); setProvider(event.currentTarget.value); setKey(''); editing(false); }}>
      {providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </PanelSelect></label>}
    <label><span className={compact ? 'sr-only' : undefined}>{keyLabel}</span><input aria-label={keyLabel} placeholder={compact ? keyLabel : undefined} type="password" autoComplete="new-password" value={key} disabled={!canMutate || busy === 'save'} onChange={(event) => { clearValidation(); setKey(event.currentTarget.value); editing(true); }} /></label>
    {spendCap && <label><span>Spend cap (USD)</span><input aria-label="Spend cap in US dollars" inputMode="decimal" placeholder="e.g. 5.00 — blank for no cap" value={cap} disabled={!canMutate || busy === 'save'} onChange={(event) => setCap(event.currentTarget.value)} /></label>}
    {compact ? <button className="btn btn-primary" type="submit" disabled={!canMutate || Boolean(busy) || !key.trim()}>{actionLabel}</button>
      : <><button className="btn" type="button" disabled={!canMutate || Boolean(busy) || !key.trim()} onClick={() => void test()}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        <button className="btn btn-primary" type="submit" disabled={!canMutate || Boolean(busy) || !receipt || !key.trim()}>{busy === 'save' ? 'Saving…' : 'Save'}</button></>}
    {(message || error) && <p className={`settings-inline-status settings-validation-message is-${error ? 'error' : 'success'}`} data-testid="provider-validation-message" role={error ? 'alert' : 'status'}>{error ?? message}</p>}
  </form>;
}
