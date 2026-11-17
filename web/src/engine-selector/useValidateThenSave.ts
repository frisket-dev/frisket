import { useState, type FormEvent } from 'react';
import { useSetupEditing, useSetupRequests } from './setupLifetime';

export type ValidateThenSaveBusy = 'test' | 'save' | 'recheck';
type ValidationOutcome<Receipt> = { receipt: Receipt | null; message: string } | { error: string };

/** Candidate test -> receipt -> save lifecycle; only a completion from the latest request writes state. */
export function useValidateThenSave<Receipt>(onEditingChange?: (editing: boolean) => void) {
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [busy, setBusy] = useState<ValidateThenSaveBusy | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { begin, invalidate } = useSetupRequests();
  const editing = useSetupEditing(onEditingChange);
  const clear = () => { invalidate(); setReceipt(null); setBusy(null); setMessage(null); setError(null); };
  const runTest = async (kind: 'test' | 'recheck', probe: (signal: AbortSignal) => Promise<ValidationOutcome<Receipt>>, failure: string) => {
    const request = begin();
    setBusy(kind); setReceipt(null); setMessage(null); setError(null);
    try {
      const outcome = await probe(request.signal);
      if (!request.isCurrent()) return;
      if ('error' in outcome) setError(outcome.error);
      else {
        setMessage(outcome.message);
        if (outcome.receipt !== null) setReceipt(outcome.receipt);
      }
    } catch {
      if (request.isCurrent()) setError(failure);
    } finally {
      if (request.isCurrent()) setBusy(null);
    }
  };
  const runSave = async <Result>(perform: (signal: AbortSignal) => Promise<Result>, saved: string, onSaved: (result: Result) => void) => {
    const request = begin(); setBusy('save'); setError(null);
    try {
      const result = await perform(request.signal);
      if (!request.isCurrent()) return;
      setReceipt(null); setMessage(saved); editing(false); onSaved(result);
    } catch {
      if (request.isCurrent()) { setReceipt(null); setError('Could not confirm the save. Recheck before trying again.'); }
    } finally {
      if (request.isCurrent()) setBusy(null);
    }
  };
  const compactSubmit = (submit: (event: FormEvent) => Promise<void>, test: () => Promise<void>) => (event: FormEvent) => {
    if (receipt) { void submit(event); return; }
    event.preventDefault(); void test();
  };
  return { receipt, busy, message, error, setError, editing, clear, runTest, runSave, compactSubmit };
}
