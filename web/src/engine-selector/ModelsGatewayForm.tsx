import { useState, type FormEvent } from 'react';
import { saveModelsGateway, validateModelsGateway, type ModelsGatewayScope } from '../api/modelsGateway';
import { safeSetupDetail, useSetupEditing, useSetupRequests } from './setupLifetime';

export function ModelsGatewayForm({ scope, initialOrigin = '', onSaved, onEditingChange }: {
  scope: ModelsGatewayScope; initialOrigin?: string; onSaved(): void; onEditingChange?(editing: boolean): void;
}) {
  const [origin, setOrigin] = useState(initialOrigin);
  const [token, setToken] = useState('');
  const [receipt, setReceipt] = useState<{ origin: string; token: string; receipt: string } | null>(null);
  const [busy, setBusy] = useState<'test' | 'save' | 'recheck' | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { begin, invalidate } = useSetupRequests();
  const editing = useSetupEditing(onEditingChange);
  const edit = () => { invalidate(); setReceipt(null); setBusy(null); setMessage(null); setError(null); editing(true); };
  const test = async (recheck = false) => {
    if (busy === 'save' || (!recheck && (!origin.trim() || !token.trim()))) return;
    const request = begin();
    const candidate = { origin: origin.trim(), token: token.trim() };
    setBusy(recheck ? 'recheck' : 'test'); setReceipt(null); setMessage(null); setError(null);
    try {
      const result = await validateModelsGateway(scope, recheck ? {} : candidate, request.signal);
      if (!request.isCurrent()) return;
      if (result.probe.ok) {
        setMessage(recheck ? 'Models gateway is reachable' : 'Valid — models gateway accepted');
        if (!recheck && result.normalized_origin && result.validation_token) {
          setReceipt({ origin: result.normalized_origin, token: candidate.token, receipt: result.validation_token });
        }
      } else setError(safeSetupDetail(result.probe.detail, [candidate.token]));
    } catch {
      if (request.isCurrent()) setError('Could not test the models gateway. Try again.');
    } finally { if (request.isCurrent()) setBusy(null); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!receipt || busy) return;
    const request = begin(); setBusy('save'); setError(null);
    try {
      await saveModelsGateway(scope, { origin: receipt.origin, token: receipt.token, validation_token: receipt.receipt }, request.signal);
      if (!request.isCurrent()) return;
      setToken(''); setReceipt(null); setMessage('Models gateway saved'); editing(false); onSaved();
    } catch {
      if (request.isCurrent()) { setReceipt(null); setError('Could not confirm the save. Recheck before trying again.'); }
    } finally { if (request.isCurrent()) setBusy(null); }
  };
  return <form className="settings-form settings-inline-form selector-setup-form" autoComplete="off" onSubmit={submit} onFocus={() => editing(true)}>
    <label><span>Gateway URL</span><input aria-label="Gateway URL" type="url" placeholder="https://models.example.org" value={origin} disabled={busy === 'save'} onChange={(event) => { edit(); setOrigin(event.currentTarget.value); }} /></label>
    <label><span>Gateway token</span><input aria-label="Gateway token" type="password" autoComplete="new-password" value={token} disabled={busy === 'save'} onChange={(event) => { edit(); setToken(event.currentTarget.value); }} /></label>
    <p className="settings-help">Use HTTPS or a loopback address. Test this URL and token before saving.</p>
    <button className="btn" type="button" disabled={Boolean(busy) || !origin.trim() || !token.trim()} onClick={() => void test()}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
    <button className="btn btn-primary" type="submit" disabled={Boolean(busy) || !receipt}>{busy === 'save' ? 'Saving…' : 'Save'}</button>
    <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void test(true)}>{busy === 'recheck' ? 'Checking…' : 'Recheck saved gateway'}</button>
    {(error || message) && <p className={`settings-inline-status settings-validation-message is-${error ? 'error' : 'success'}`} role={error ? 'alert' : 'status'}>{error ?? message}</p>}
  </form>;
}
