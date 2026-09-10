import { AlertTriangle } from 'lucide-react';
import type { RunEstimate } from '../api/open';
import { formatUsd } from '../actions/model';
import { quotedUsd } from '../actions/quotedCost';
import { countLabel, formatDuration } from '../format';

export interface CostGateModalProps {
  estimate: RunEstimate;
  message: string;
  onConfirm(): void;
  onCancel(): void;
}

/** THE cost gate, and the only one: the server's 402 needs_confirmation
 *  envelope, rendered. Show its estimate and sentence, then let the user
 *  approve with one deliberate click. The caller re-POSTs the exact
 *  server-issued confirmation token; this button is not a bare confirmation
 *  authority. Nothing opens this modal except a 402 that already came back,
 *  so there is no "waiting for a price" state to lock — the price is in hand
 *  before it mounts. */
export function CostGateModal({ estimate, message, onConfirm, onCancel }: CostGateModalProps) {
  const quoted = quotedUsd(estimate);

  return (
    <div className="modal-backdrop" data-testid="cost-gate-modal">
      <form
        className="modal-card"
        onSubmit={(e) => {
          e.preventDefault();
          onConfirm();
        }}
      >
        <div className="modal-title">
          <AlertTriangle size={15} className="modal-warn-icon" /> Cost gate
        </div>
        <p className="modal-body-text">{message}</p>
        {(estimate.claims?.length ?? 0) > 0 && (
          <ul className="modal-body-text" data-testid="cost-gate-claims">
            {estimate.claims!.map((claim) => (
              <li key={claim.field} data-testid={`cost-gate-claim-${claim.field}`}>
                {claim.display}
              </li>
            ))}
          </ul>
        )}
        <div className="cost-line cost-warn" data-testid="cost-gate-estimate">
          Estimated cost: <strong>{quoted === null ? 'UNKNOWN' : formatUsd(quoted)}</strong>
          {estimate.rows > 0 && <> · {countLabel(estimate.rows, 'row')}</>}
          {estimate.audio_seconds !== undefined && (
            <> · {formatDuration(estimate.audio_seconds * 1000)} of audio</>
          )}
          {estimate.avg_input_tokens !== undefined && (
            <> · ~{estimate.avg_input_tokens.toLocaleString()} input tok/row</>
          )}
        </div>
        <div className="form-actions">
          <button type="button" className="btn" onClick={onCancel} data-testid="cost-gate-cancel">
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            data-testid="cost-gate-confirm"
          >
            Run it
          </button>
        </div>
      </form>
    </div>
  );
}
