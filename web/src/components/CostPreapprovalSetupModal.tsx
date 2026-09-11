import { useState } from 'react';

import { updateProfile } from '../api/open';

/** First signed-in use records a person's standing amount. The server keeps
 * the null marker until this form succeeds, so a reload cannot skip it. */
export function CostPreapprovalSetupModal({ onComplete }: { onComplete?: () => void }) {
  const [amount, setAmount] = useState('2');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [complete, setComplete] = useState(false);

  if (complete) return null;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateProfile({ cost_preapproval_usd: amount });
      setComplete(true);
      onComplete?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" data-testid="cost-preapproval-setup">
      <form
        className="modal-card"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div className="modal-title">Choose your cost pre-approval amount</div>
        <p className="modal-body-text">
          Frisket always shows a price estimate. Runs at or below this amount can
          continue without another prompt, including sending their inputs to the
          selected provider. Higher or unpriced runs ask for one click.
        </p>
        <label className="form-label" htmlFor="cost-preapproval-setup-input">
          Amount in USD per run
        </label>
        <input
          id="cost-preapproval-setup-input"
          className="form-input"
          data-testid="cost-preapproval-setup-input"
          type="number"
          min="0"
          step="0.000001"
          inputMode="decimal"
          value={amount}
          onChange={(event) => setAmount(event.currentTarget.value)}
          disabled={saving}
          autoFocus
        />
        {error && <p className="settings-inline-error" role="alert">{error}</p>}
        <div className="form-actions">
          <button
            type="submit"
            className="btn btn-primary"
            data-testid="cost-preapproval-setup-save"
            disabled={saving || amount.trim() === ''}
          >
            {saving ? 'Saving…' : 'Continue'}
          </button>
        </div>
      </form>
    </div>
  );
}
