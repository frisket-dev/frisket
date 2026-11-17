import '../components/PanelPrimitives.css';
import { FormField, TextInput } from '../components/FormControls';
import { useState, type FormEvent } from 'react';
import type { ProviderValidateResult } from '../api/types';
import { PanelSelect } from '../components/PanelSelect';
import { safeSetupDetail } from './setupLifetime';
import { useValidateThenSave } from './useValidateThenSave';
import { ValidateThenSaveButtons } from './ValidateThenSaveButtons';

export interface ProviderKeyFormProps<Result = void> {
  provider?: string;
  providers?: ReadonlyArray<{ id: string; label: string; policy_fields?: readonly string[] }>;
  canMutate: boolean;
  keyLabel?: string;
  keyPlaceholder?: string;
  keyPrefix?: string;
  spendCap?: boolean;
  compact?: boolean;
  validate(provider: string, key: string, signal: AbortSignal): Promise<ProviderValidateResult>;
  save(provider: string, key: string, receipt: string, cap: number | null, signal: AbortSignal): Promise<Result>;
  onSaved(result: Result): void;
  onEditingChange?(editing: boolean): void;
}

/** Shared candidate/receipt controls for selector setup and provider Settings. */
export function ProviderKeyForm<Result = void>({ provider: fixedProvider, providers = [], canMutate,
  keyLabel = 'API key', keyPlaceholder, keyPrefix, spendCap = false, compact = false,
  validate, save, onSaved, onEditingChange,
}: ProviderKeyFormProps<Result>) {
  const [provider, setProvider] = useState(fixedProvider ?? providers[0]?.id ?? 'anthropic');
  const [key, setKey] = useState('');
  const [cap, setCap] = useState('');
  const policyFields = providers.find((item) => item.id === provider)?.policy_fields;
  const supportsSpendCap = spendCap && (policyFields === undefined || policyFields.includes('spend_cap_usd'));
  const flow = useValidateThenSave<string>(onEditingChange);
  const { receipt, busy, message, error, editing } = flow;
  const test = async () => {
    if (!canMutate || !key.trim() || busy === 'save') return;
    const candidate = key.trim();
    await flow.runTest('test', async (signal) => {
      const result = await validate(provider, candidate, signal);
      return result.ok && result.validation_token
        ? { receipt: result.validation_token, message: 'Valid — key accepted' }
        : { error: safeSetupDetail(result.detail, [candidate]) };
    }, 'Could not test the key. Try again.');
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canMutate || !receipt || !key.trim() || busy) return;
    const numericCap = supportsSpendCap && cap.trim() ? Number(cap) : null;
    if (numericCap !== null && (!Number.isFinite(numericCap) || numericCap < 0)) {
      flow.setError('Spend cap must be a nonnegative amount in US dollars.'); return;
    }
    await flow.runSave((signal) => save(provider, key.trim(), receipt, numericCap, signal),
      'Provider key saved', (result) => { setKey(''); onSaved(result); });
  };
  const prefixMismatch = Boolean(keyPrefix && key.trim() && !key.trim().startsWith(keyPrefix));
  return <form className={`form-inline settings-provider-key-form${compact ? ' settings-provider-key-form--compact' : ''}`} autoComplete="off" onSubmit={compact ? flow.compactSubmit(submit, test) : submit} onFocus={() => editing(true)}>
    {!fixedProvider && <FormField label="Provider" ><PanelSelect aria-label="Provider" value={provider} disabled={!canMutate || busy === 'save'} onChange={(event) => { flow.clear(); setProvider(event.currentTarget.value); setKey(''); editing(false); }}>
      {providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </PanelSelect></FormField>}
    <label className="form-field"><span className={compact ? 'sr-only' : undefined}>{keyLabel}</span><TextInput aria-label={keyLabel} placeholder={keyPlaceholder ?? (compact ? keyLabel : undefined)} type="password" autoComplete="new-password" value={key} disabled={!canMutate || busy === 'save'} onChange={(event) => { flow.clear(); setKey(event.currentTarget.value); editing(true); }} /></label>
    {supportsSpendCap && <FormField label="Spend cap (USD)" ><TextInput aria-label="Spend cap in US dollars" inputMode="decimal" placeholder="e.g. 5.00 — blank for no cap" value={cap} disabled={!canMutate || busy === 'save'} onChange={(event) => setCap(event.currentTarget.value)} /></FormField>}
    <ValidateThenSaveButtons compact={compact} busy={busy} hasReceipt={Boolean(receipt)} ready={canMutate && Boolean(key.trim())} onTest={() => void test()} />
    {(message || error) && <p className="panel-notice" data-tone={error ? 'error' : 'success'} data-testid="provider-validation-message" role={error ? 'alert' : 'status'}>{error ?? message}</p>}
    {prefixMismatch && !error && <p className="panel-notice" data-tone="warning">
      This key normally starts with {keyPrefix}. Check that you copied the right key.
    </p>}
  </form>;
}
