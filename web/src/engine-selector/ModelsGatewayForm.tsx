import '../components/PanelPrimitives.css';
import { Button } from '../components/Button';
import { TextInput } from '../components/FormControls';
import { useState, type FormEvent } from 'react';
import { saveModelsGateway, validateModelsGateway, type ModelsGatewayScope } from '../api/modelsGateway';
import { safeSetupDetail } from './setupLifetime';
import { useValidateThenSave } from './useValidateThenSave';
import { ValidateThenSaveButtons } from './ValidateThenSaveButtons';

export function ModelsGatewayForm({ scope, initialOrigin = '', compact = false, onSaved, onEditingChange }: {
  scope: ModelsGatewayScope; initialOrigin?: string; compact?: boolean; onSaved(): void; onEditingChange?(editing: boolean): void;
}) {
  const [origin, setOrigin] = useState(initialOrigin);
  const [token, setToken] = useState('');
  const flow = useValidateThenSave<{ origin: string; token: string; receipt: string }>(onEditingChange);
  const { receipt, busy, message, error, editing } = flow;
  const edit = () => { flow.clear(); editing(true); };
  const test = async (recheck = false) => {
    if (busy === 'save' || (!recheck && (!origin.trim() || !token.trim()))) return;
    const candidate = { origin: origin.trim(), token: token.trim() };
    await flow.runTest(recheck ? 'recheck' : 'test', async (signal) => {
      const result = await validateModelsGateway(scope, recheck ? {} : candidate, signal);
      if (!result.probe.ok) return { error: safeSetupDetail(result.probe.detail, [candidate.token]) };
      return {
        message: recheck ? 'Models gateway is reachable' : 'Valid — models gateway accepted',
        receipt: !recheck && result.normalized_origin && result.validation_token
          ? { origin: result.normalized_origin, token: candidate.token, receipt: result.validation_token } : null,
      };
    }, 'Could not test the models gateway. Try again.');
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!receipt || busy) return;
    await flow.runSave((signal) => saveModelsGateway(scope, { origin: receipt.origin, token: receipt.token, validation_token: receipt.receipt }, signal),
      'Models gateway saved', () => { setToken(''); onSaved(); });
  };
  return <form className={`form-inline selector-setup-form${compact ? ' selector-setup-form--compact' : ''}`} autoComplete="off" onSubmit={compact ? flow.compactSubmit(submit, test) : submit} onFocus={() => editing(true)}>
    <p className="form-hint"><a href="https://github.com/frisket-dev/frisket/blob/main/sidecar/README.md" target="_blank" rel="noreferrer">Model server setup guide</a></p>
    <label className="form-field"><span className={compact ? 'sr-only' : undefined}>Gateway URL</span><TextInput aria-label="Gateway URL" type="url" placeholder="https://models.example.org" value={origin} disabled={busy === 'save'} onChange={(event) => { edit(); setOrigin(event.currentTarget.value); }} /></label>
    <label className="form-field"><span className={compact ? 'sr-only' : undefined}>Gateway token</span><TextInput aria-label="Gateway token" placeholder={compact ? 'Gateway token' : undefined} type="password" autoComplete="new-password" value={token} disabled={busy === 'save'} onChange={(event) => { edit(); setToken(event.currentTarget.value); }} /></label>
    {!compact && <p className="form-hint">Use HTTPS or a loopback address. Test this URL and token before saving.</p>}
    <ValidateThenSaveButtons compact={compact} busy={busy} hasReceipt={Boolean(receipt)} ready={Boolean(origin.trim() && token.trim())} onTest={() => void test()}>
      <Button type="button" disabled={Boolean(busy)} onClick={() => void test(true)}>{busy === 'recheck' ? 'Checking…' : 'Recheck saved gateway'}</Button>
    </ValidateThenSaveButtons>
    {(error || message) && <p className="panel-notice" data-tone={error ? 'error' : 'success'} role={error ? 'alert' : 'status'}>{error ?? message}</p>}
  </form>;
}
