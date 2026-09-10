// The Monitor Lineage tab: the project's provenance DAG (sources → sheets → AI
// columns) read from GET /lineage.
// Stale sheet nodes and the edges into them render amber; the
// '↻ Re-run N stale · cascades' control shares the SAME cascade confirm the
// tab strip's stale pill uses (confirm-flow always — no silent spend). No
// staleness is computed client-side; every amber mark reads the API.

import { useCallback, useEffect, useState } from 'react';
import { RotateCw } from 'lucide-react';
import {
  ConfirmationRequiredError,
  type LineageDag,
  type LineageNode,
  type RunEstimate,
  type SheetMeta,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { staleSheetsDeepestFirst } from './lineageStale';
import { PanelHeader, StatusChip } from '../components/PanelPrimitives';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { CostGateModal } from '../components/CostGateModal';

const LINEAGE_TIERS: Array<{ key: string; title: string; kind: LineageNode['kind'] }> = [
  { key: 'sources', title: 'SOURCES', kind: 'source' },
  { key: 'sheets', title: 'SHEETS', kind: 'sheet' },
  { key: 'ai-columns', title: 'AI COLUMNS', kind: 'ai_column' },
];

/** The shared cascade confirm (strip pill + Lineage tab control): lists the
 *  stale sheets deepest-first; confirming enqueues the refreshes shallow→deep
 *  with confirmed=true, so model-op refreshes run only after this consent. */
export function CascadeConfirmDialog({
  staleDeepestFirst,
  onClose,
  refreshSheets,
}: {
  staleDeepestFirst: SheetMeta[];
  onClose: () => void;
  refreshSheets: () => void | Promise<unknown>;
}) {
  const { projectApi } = useWorkspaceStores();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [costGate, setCostGate] = useState<{
    estimate: RunEstimate;
    message: string;
    resolve: (approved: boolean) => void;
  } | null>(null);

  // The backdrop's own onClick already handles outside-click cancel; this
  // adds the missing Escape path. Escape must CANCEL — call onClose, never
  // runCascade — matching the Cancel button and backdrop's busy gate (no
  // dismissing away mid-run, so the Escape listener is disabled while busy).
  useEscapeDismiss(onClose, { enabled: !busy });

  const runCascade = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      for (const sheet of [...staleDeepestFirst].reverse()) {
        let confirmation: string | undefined;
        while (true) {
          try {
            await projectApi.refreshSheet(sheet.id, confirmation);
            break;
          } catch (err) {
            if (!(err instanceof ConfirmationRequiredError)) throw err;
            const approved = await new Promise<boolean>((resolve) => {
              setCostGate({ estimate: err.estimate, message: err.message, resolve });
            });
            if (!approved) return;
            confirmation = err.estimate.promise_set_hash;
          }
        }
      }
      await refreshSheets();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Refresh failed');
    } finally {
      setBusy(false);
    }
  }, [staleDeepestFirst, refreshSheets, onClose]);

  return (
    <>
      <div
        className="sheet-info-backdrop"
        data-testid="workbench-mainView-cascadeConfirm-backdrop"
        role="presentation"
        onClick={() => !busy && onClose()}
      >
        <dialog
          open
          className="sheet-cascade-confirm"
          data-testid="workbench-mainView-cascadeConfirm"
          aria-label="Re-run stale sheets"
          style={{ position: 'static', margin: 0 }}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="sheet-cascade-confirm-title">Re-run stale sheets</div>
          <ol className="sheet-cascade-confirm-list">
            {staleDeepestFirst.map((sheet) => (
              <li key={sheet.id} data-testid={`cascade-stale-${sheet.id}`}>
                {sheet.name}
              </li>
            ))}
          </ol>
          {/* Ruling 4 honesty: sheet.refresh cannot resume — it rebuilds each
              sheet from its parent, replacing every derived row. Naming the
              scope before the click was not enough: this door re-runs a whole
              CASCADE, so the copy also says it is a fresh purchase of every
              listed sheet. The cost gate then prices model steps. */}
          <p className="muted" data-testid="cascade-rerun-scope">
            Re-running rebuilds each sheet from its parent — every row is
            replaced and re-derived, not just the changed ones. That makes it
            a new run, not a resume: every derived row in every sheet listed
            above is charged again.
          </p>
          {error && <div className="sheet-cascade-confirm-error">{error}</div>}
          <div className="sheet-cascade-confirm-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="cascade-confirm-run"
              onClick={() => void runCascade()}
              disabled={busy}
            >
              {busy ? 'Re-running…' : `Re-run ${staleDeepestFirst.length}`}
            </button>
          </div>
        </dialog>
      </div>
      {costGate && (
        <CostGateModal
          estimate={costGate.estimate}
          message={costGate.message}
          onCancel={() => {
            const resolve = costGate.resolve;
            setCostGate(null);
            resolve(false);
          }}
          onConfirm={() => {
            const resolve = costGate.resolve;
            setCostGate(null);
            resolve(true);
          }}
        />
      )}
    </>
  );
}

function sheetNameOf(dag: LineageDag, nodeId: string): string {
  return dag.nodes.find((n) => n.id === nodeId)?.name ?? nodeId;
}

function LineageNodeChip({
  node,
  dag,
}: {
  node: LineageNode;
  dag: LineageDag;
}) {
  const stale = node.syncState === 'stale';
  // Inbound derive edges give a derived sheet its "← parent" line; an amber
  // edge means the downstream sheet is stale relative to that parent.
  const inbound = dag.edges.filter((e) => e.to === node.id && e.kind !== 'ai_column');
  return (
    <div
      className="lineage-node"
      data-testid={`lineage-node-${node.id.replace(':', '-')}`}
      data-node-kind={node.kind}
      data-stale={stale ? 'true' : undefined}
    >
      <div className="lineage-node-name">
        {node.name}
        {node.kind === 'sheet' && node.syncState && (
          <StatusChip tone={stale ? 'warning' : 'success'} size="sm">
            {stale ? 'stale' : 'synced'}
          </StatusChip>
        )}
      </div>
      {node.kind === 'ai_column' && node.model && (
        <div className="lineage-node-sub">{node.model}</div>
      )}
      {node.kind === 'sheet' && node.op_kind && (
        <div className="lineage-node-sub">{node.op_label || node.op_kind}</div>
      )}
      {inbound.map((edge) => (
        <div
          key={`${edge.from}-${edge.to}`}
          className="lineage-edge"
          data-testid={`lineage-edge-${edge.from.replace(':', '-')}-${edge.to.replace(':', '-')}`}
          data-stale={edge.stale ? 'true' : undefined}
        >
          ← {sheetNameOf(dag, edge.from)}
        </div>
      ))}
    </div>
  );
}

export function LineagePanel({
  sheets,
  refreshSheets,
}: {
  sheets: SheetMeta[];
  refreshSheets: () => void | Promise<unknown>;
}) {
  const { projectApi } = useWorkspaceStores();
  const [dag, setDag] = useState<LineageDag | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Refetch whenever the sheets list changes identity — refreshSheets() after a
  // re-run produces a new array, which re-syncs the DAG with the same read.
  useEffect(() => {
    let alive = true;
    projectApi
      .getLineage()
      .then((next) => {
        if (alive) {
          setDag(next);
          setError(null);
        }
      })
      .catch((err) => {
        if (alive) setError(err instanceof Error ? err.message : 'Lineage unavailable');
      });
    return () => {
      alive = false;
    };
  }, [sheets]);

  const staleDeepestFirst = staleSheetsDeepestFirst(sheets);

  if (error) {
    return (
      <div className="bottom-dock-empty" data-testid="lineage-panel-error">
        {error}
      </div>
    );
  }
  if (!dag) {
    return <div className="bottom-dock-empty">Loading lineage…</div>;
  }

  return (
    <div className="lineage-panel" data-testid="lineage-panel">
      <PanelHeader
        className="panel-frame-header lineage-panel-header"
        title={<span className="lineage-panel-caption">sources → sheets → AI columns</span>}
        actions={
          staleDeepestFirst.length > 0 && (
            <button
              type="button"
              className="lineage-rerun-stale"
              data-testid="lineage-rerun-stale"
              onClick={() => setConfirmOpen(true)}
            >
              <RotateCw size={12} aria-hidden />
              Re-run {staleDeepestFirst.length} stale · cascades
            </button>
          )
        }
      />
      <div className="lineage-tiers">
        {LINEAGE_TIERS.map((tier) => {
          const nodes = dag.nodes.filter((n) => n.kind === tier.kind);
          return (
            <div className="lineage-tier" key={tier.key} data-testid={`lineage-tier-${tier.key}`}>
              <div className="lineage-tier-title">{tier.title}</div>
              {nodes.length === 0 ? (
                <div className="lineage-tier-empty">none</div>
              ) : (
                nodes.map((node) => <LineageNodeChip key={node.id} node={node} dag={dag} />)
              )}
            </div>
          );
        })}
      </div>
      {confirmOpen && (
        <CascadeConfirmDialog
          staleDeepestFirst={staleDeepestFirst}
          onClose={() => setConfirmOpen(false)}
          refreshSheets={refreshSheets}
        />
      )}
    </div>
  );
}
