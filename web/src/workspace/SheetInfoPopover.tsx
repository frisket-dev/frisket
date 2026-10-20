import './SheetInfoPopover.css';
import '../workbench/LineagePanel.css';
import { Button } from '../components/Button';
import { useCallback, useState } from 'react';
import { RotateCw } from 'lucide-react';
import { ConfirmationRequiredError, type RunEstimate, type SheetMeta } from '../api/open';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { formatUsd } from '../format';
import { StatusChip } from '../components/PanelPrimitives';
import { quotedCost } from '../actions/quotedCost';
import { CascadeConfirmDialog } from '../workbench/LineagePanel';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';

export function SheetStalePill({
  staleDeepestFirst,
  refreshSheets,
}: {
  staleDeepestFirst: SheetMeta[];
  refreshSheets: () => void | Promise<void>;
}) {
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        className="workbench-mainView-stalePill"
        data-testid="workbench-mainView-stalePill"
        onClick={() => setConfirmOpen(true)}
      >
        <RotateCw size={12} aria-hidden />
        Parent changed · re-run {staleDeepestFirst.length}
      </button>
      {confirmOpen && (
        <CascadeConfirmDialog
          staleDeepestFirst={staleDeepestFirst}
          onClose={() => setConfirmOpen(false)}
          refreshSheets={refreshSheets}
        />
      )}
    </>
  );
}

export function SheetInfoPopover({
  sheet,
  onClose,
  onNavigateParent,
  onViewLineage,
  refreshSheets,
}: {
  sheet: SheetMeta;
  onClose: () => void;
  onNavigateParent: (parentId: string) => void;
  onViewLineage: () => void;
  refreshSheets: () => void | Promise<void>;
}) {
  const { projectApi } = useWorkspaceStores();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [costGate, setCostGate] = useState<{
    message: string;
    estimate: RunEstimate;
  } | null>(null);
  const sheetRefreshQuote = quotedCost(costGate?.estimate);
  const refreshable = sheet.refreshable === true;
  const stale = sheet.syncState === 'stale';
  const parent = sheet.parent;

  useEscapeDismiss(onClose);

  const runReRun = useCallback(
    async (confirmation?: string) => {
      setBusy(true);
      setError(null);
      try {
        await projectApi.refreshSheet(sheet.id, confirmation);
        await refreshSheets();
        onClose();
      } catch (err) {
        if (err instanceof ConfirmationRequiredError) {
          setCostGate({
            message: err.message,
            estimate: err.estimate,
          });
        } else {
          setError(err instanceof Error ? err.message : 'Refresh failed');
        }
      } finally {
        setBusy(false);
      }
    },
    [sheet.id, refreshSheets, onClose],
  );

  return (

    <div
      className="sheet-info-backdrop"
      data-testid="sheet-info-backdrop"
      onClick={onClose}
    >
      <dialog
        open
        className="sheet-info-popover"
        data-testid="sheet-info-popover"
        aria-label={`${sheet.name} info`}

        style={{ position: 'static', margin: 0 }}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sheet-info-head">
          <span className="sheet-info-name">{sheet.name}</span>
          <StatusChip
            tone={refreshable ? (stale ? 'warning' : 'success') : 'neutral'}
            size="md"
            testId="sheet-info-badge"
          >
            {refreshable ? (stale ? 'Source changed' : 'Up to date') : 'Run once'}
          </StatusChip>
        </div>
        <div className="sheet-info-section">
          <div className="sheet-info-label">HOW IT&apos;S MADE</div>
          <div className="sheet-info-value" data-testid="sheet-info-transform">
            {parent?.viaAction}
          </div>
        </div>
        <div className="sheet-info-section">
          <div className="sheet-info-label">LINEAGE</div>
          <button
            type="button"
            className="sheet-info-parent-link"
            data-testid="sheet-info-parent-link"
            onClick={() => parent && onNavigateParent(parent.sheetId)}
          >
            ← {parent?.sheetName}
          </button>
        </div>

        {refreshable && (
          <p className="muted" data-testid="sheet-info-rerun-scope">
            Re-running rebuilds this sheet from{' '}
            {parent?.sheetName ?? 'its parent'} — every row is replaced, not just
            the changed ones. That makes it a new run, not a resume: every
            derived row is charged again.
          </p>
        )}
        {refreshable && error && <div className="sheet-info-error">{error}</div>}
        {refreshable && costGate && (
          <div className="sheet-info-costGate" data-testid="sheet-info-cost-confirm">
            <div className="sheet-info-costGate-message">
              This re-run calls a model
              {/* Display the exact server-hashed consent cost. */}
              {sheetRefreshQuote.usd != null
                ? ` · est. ${formatUsd(sheetRefreshQuote.usd)}`
                : sheetRefreshQuote.refused
                  ? ' · est. UNKNOWN'
                  : ''}
            </div>
            <Button
              type="button"
              className="btn-primary"
              data-testid="sheet-info-cost-confirm-run"
              disabled={busy}
              onClick={() =>
                void runReRun(costGate.estimate.promise_set_hash)
              }
            >
              {busy ? 'Re-running…' : 'Confirm re-run'}
            </Button>
          </div>
        )}
        <div className="sheet-info-footer">
          <Button
            type="button"
            className="btn-ghost"
            data-testid="sheet-info-view-lineage"
            onClick={onViewLineage}
          >
            View lineage
          </Button>
          {refreshable && (
            <Button
              type="button"
              className="btn-primary"
              data-testid="sheet-info-rerun"
              disabled={busy || costGate != null}
              onClick={() => void runReRun()}
            >
              {busy ? 'Re-running…' : 'Re-run now'}
            </Button>
          )}
        </div>
      </dialog>
    </div>
  );
}
