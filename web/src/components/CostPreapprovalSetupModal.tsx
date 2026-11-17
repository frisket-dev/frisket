import './PanelPrimitives.css';
import { Button } from './Button';
import { TextInput } from './FormControls';
import { useState } from 'react';
import { Modal } from './Modal';

import { updateProfile } from '../api/open';
import { useAction } from '../hooks/useAction';

/** First signed-in use records a person's standing amount. The server keeps
 * the null marker until this form succeeds, so a reload cannot skip it. */
export function CostPreapprovalSetupModal({ onComplete }: { onComplete?: () => void }) {
  const [amount, setAmount] = useState('2');
  const { busy: saving, error, run } = useAction();
  const [complete, setComplete] = useState(false);

  if (complete) return null;

  const save = () => run(async () => {
    await updateProfile({ cost_preapproval_usd: amount });
    setComplete(true);
    onComplete?.();
  });

  return (
    <Modal title="Set your allowance" testId="cost-preapproval-setup">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!saving) void save();
        }}
      >
        <p className="modal-body-text">
          Frisket always shows a price estimate. Runs at or below your allowance
          continue without another prompt, including sending their inputs to the
          selected provider. Runs over it, or with no price, ask for one click.
        </p>
        <label className="form-label" htmlFor="cost-preapproval-setup-input">
          Allowance per run (USD)
        </label>
        <TextInput
          id="cost-preapproval-setup-input"

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
        {error && <p className="panel-error" role="alert">{error}</p>}
        <div className="form-actions">
          <Button
            type="submit"
            className="btn-primary"
            data-testid="cost-preapproval-setup-save"
            disabled={saving || amount.trim() === ''}
          >
            {saving ? 'Saving…' : 'Continue'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
