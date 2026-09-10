import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Copy, Download, RefreshCcw, ShieldCheck } from 'lucide-react';
import { type ProvenanceManifest as ProvenanceManifestData } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { formatUsd } from '../actions/model';
import { formatUsdOrNone } from '../format';
import { Drawer } from './Drawer';
import { PanelLoading } from './PanelPrimitives';
import { ReceiptInspector } from './ReceiptInspector';
import { AttemptReceiptList } from './AttemptReceiptList';

const PROVENANCE_HISTORY_PAGE_SIZE = 25;

export interface ProvenanceManifestProps {
  projectName: string;
  onClose(): void;
  onCreateEntityTable?(receiptId: string): void;
}

type LoadState =
  | { status: 'loading'; manifest?: undefined; error?: undefined }
  | { status: 'loaded'; manifest: ProvenanceManifestData; error?: undefined }
  | { status: 'error'; manifest?: undefined; error: string };

type ProvenancePage = ProvenanceManifestData['runsPage'];

function startedLabel(iso: string | null): string {
  if (!iso) return 'unknown';
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function rowsLabel(done: number, total: number, failed: number): string {
  const base = `${done.toLocaleString()}/${total.toLocaleString()}`;
  return failed > 0 ? `${base} rows, ${failed.toLocaleString()} failed` : `${base} rows`;
}

function pageRangeLabel(page: ProvenancePage, loadedCount: number, label: string): string {
  if (page.total === 0 || loadedCount === 0) {
    return `Showing 0 of ${page.total.toLocaleString()} ${label}`;
  }
  const start = page.offset + 1;
  const end = Math.min(page.offset + loadedCount, page.total);
  return `Showing ${start.toLocaleString()}-${end.toLocaleString()} of ${page.total.toLocaleString()} ${label}`;
}

function previousPageOffset(page: ProvenancePage): number {
  return Math.max(0, page.offset - page.limit);
}

function publicManifest(manifest: ProvenanceManifestData | null) {
  if (!manifest) return null;
  return {
    project_id: manifest.projectId,
    models: manifest.models.map((model) => ({
      model: model.model,
      provider: model.provider,
      runs: model.runs,
      rows: model.rows,
      cost: model.cost,
    })),
    touched: manifest.touched,
    providers: manifest.providers,
    total_cost: manifest.totalCost,
    has_unknown_costs: manifest.hasUnknownCosts,
    unknown_cost_runs: manifest.unknownCostRuns,
    action_kinds: manifest.actionKinds.map((actionKind) => ({
      action_kind: actionKind.actionKind,
      action_name: actionKind.actionName,
      runs: actionKind.runs,
      rows: actionKind.rows,
      failed_rows: actionKind.failedRows,
      cost: actionKind.cost,
    })),
    runs: manifest.runs.map((run) => ({
      run_id: run.runId,
      sheet_id: run.sheetId,
      action_kind: run.actionKind,
      action_name: run.actionName,
      model: run.model,
      provider: run.provider,
      status: run.status,
      total_rows: run.totalRows,
      completed_rows: run.completedRows,
      failed_rows: run.failedRows,
      cost: run.cost,
      started_at: run.startedAt,
      finished_at: run.finishedAt,
    })),
    runs_page: {
      schema_version: manifest.runsPage.schemaVersion,
      order: manifest.runsPage.order,
      offset: manifest.runsPage.offset,
      limit: manifest.runsPage.limit,
      total: manifest.runsPage.total,
      has_more: manifest.runsPage.hasMore,
      next_offset: manifest.runsPage.nextOffset,
    },
    receipts: manifest.receipts.map((receipt) => ({
      receipt_id: receipt.receiptId,
      action_kind: receipt.actionKind,
      status: receipt.status,
      run_id: receipt.runId,
      created_at: receipt.createdAt,
    })),
    receipts_page: {
      schema_version: manifest.receiptsPage.schemaVersion,
      order: manifest.receiptsPage.order,
      offset: manifest.receiptsPage.offset,
      limit: manifest.receiptsPage.limit,
      total: manifest.receiptsPage.total,
      has_more: manifest.receiptsPage.hasMore,
      next_offset: manifest.receiptsPage.nextOffset,
    },
  };
}

function jsonPayload(manifest: ProvenanceManifestData | null): string {
  return JSON.stringify(
    {
      generated_at: new Date().toISOString(),
      manifest: publicManifest(manifest),
    },
    null,
    2,
  );
}

function ProvenanceActions({
  manifest,
  payload,
  projectName,
  onCopy,
  onRefresh,
}: {
  manifest: ProvenanceManifestData | null;
  payload: string;
  projectName: string;
  onCopy(): void;
  onRefresh(): void;
}) {
  const downloadHref = `data:application/json;charset=utf-8,${encodeURIComponent(payload)}`;
  return (
    <section className="provenance-actions">
      <button
        type="button"
        className="mini-btn"
        data-testid="provenance-refresh"
        onClick={onRefresh}
      >
        <RefreshCcw size={12} /> Refresh
      </button>
      <button
        type="button"
        className="mini-btn"
        data-testid="provenance-copy-json"
        disabled={!manifest}
        onClick={onCopy}
      >
        <Copy size={12} /> Copy JSON
      </button>
      <a
        className={`mini-btn${manifest ? '' : ' disabled'}`}
        data-testid="provenance-download-json"
        href={manifest ? downloadHref : undefined}
        download={`${projectName.replace(/[^A-Za-z0-9._-]+/g, '-') || 'project'}-provenance.json`}
        aria-disabled={!manifest}
      >
        <Download size={12} /> Download JSON
      </a>
    </section>
  );
}

function ProvenanceSummary({ manifest }: { manifest: ProvenanceManifestData }) {
  return (
    <>
      <section className="provenance-summary" data-testid="provenance-summary">
        <div>
          <span className="prov-key">providers</span>
          <strong>{manifest.providers.length.toLocaleString()}</strong>
        </div>
        <div>
          <span className="prov-key">models</span>
          <strong>{manifest.models.length.toLocaleString()}</strong>
        </div>
        <div>
          <span className="prov-key">runs</span>
          <strong>{manifest.runsPage.total.toLocaleString()}</strong>
        </div>
        <div>
          <span className="prov-key">cost</span>
          <strong data-testid="provenance-total-cost">{formatUsd(manifest.totalCost)}</strong>
        </div>
      </section>
      {manifest.hasUnknownCosts && (
        <div className="row-field-empty" data-testid="provenance-unknown-costs-note">
          Cost totals exclude {manifest.unknownCostRuns.toLocaleString()}{' '}
          {manifest.unknownCostRuns === 1 ? 'run' : 'runs'} with unknown cost.
        </div>
      )}
    </>
  );
}

function ProvenanceProviders({ providers }: { providers: string[] }) {
  return (
    <section>
      <h3 className="drawer-section-title">Providers</h3>
      {providers.length === 0 ? (
        <div className="row-field-empty">No model provider has touched this project yet.</div>
      ) : (
        <div className="provenance-chips" data-testid="provenance-provider-list">
          {providers.map((provider) => (
            <span key={provider} className="type-pill">{provider}</span>
          ))}
        </div>
      )}
    </section>
  );
}

function ProvenanceModels({ manifest }: { manifest: ProvenanceManifestData }) {
  return (
    <section>
      <h3 className="drawer-section-title">Models</h3>
      {manifest.models.length === 0 ? (
        <div className="row-field-empty">No model-backed runs recorded.</div>
      ) : (
        <table className="provenance-table" data-testid="provenance-model-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Provider</th>
              <th>Runs</th>
              <th>Rows</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {manifest.models.map((model) => (
              <tr key={model.model} data-testid="provenance-model-row">
                <td>{model.model}</td>
                <td>{model.provider ?? 'unknown'}</td>
                <td>{model.runs.toLocaleString()}</td>
                <td>{model.rows.toLocaleString()}</td>
                <td>{formatUsd(model.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ProvenanceActionKinds({ manifest }: { manifest: ProvenanceManifestData }) {
  return (
    <section>
      <h3 className="drawer-section-title">Actions</h3>
      {manifest.actionKinds.length === 0 ? (
        <div className="row-field-empty">No actions recorded.</div>
      ) : (
        <table className="provenance-table" data-testid="provenance-action-kind-table">
          <thead>
            <tr>
              <th>Action</th>
              <th>Runs</th>
              <th>Rows</th>
              <th>Failed</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {manifest.actionKinds.map((actionKind) => (
              <tr key={actionKind.actionKind} data-testid="provenance-action-kind-row">
                <td>
                  <div>{actionKind.actionName}</div>
                  <div className="muted">{actionKind.actionKind}</div>
                </td>
                <td>{actionKind.runs.toLocaleString()}</td>
                <td>{actionKind.rows.toLocaleString()}</td>
                <td>{actionKind.failedRows.toLocaleString()}</td>
                <td>{formatUsd(actionKind.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ProvenanceRuns({
  manifest,
  onLoadPage,
}: {
  manifest: ProvenanceManifestData;
  onLoadPage(runsOffset: number, receiptsOffset: number): void;
}) {
  return (
    <section>
      <h3 className="drawer-section-title">Recent Runs</h3>
      {manifest.runs.length === 0 ? (
        <div className="row-field-empty">No runs recorded.</div>
      ) : (
        <ol className="provenance-run-list" data-testid="provenance-run-list">
          {manifest.runs.map((run) => (
            <li key={run.runId} data-testid="provenance-run-row">
              <div className="version-line">
                <span className={`version-status status-${run.status}`}>{run.status}</span>
                <span className="col-field-name">run {run.runId}</span>
                <span>{run.actionName}</span>
                <span className="muted">{formatUsdOrNone(run.cost)}</span>
              </div>
              <div className="version-sub">
                {run.actionKind} · {run.model ?? 'no model recorded'} ·{' '}
                {run.provider ?? 'unknown provider'} ·{' '}
                {rowsLabel(run.completedRows, run.totalRows, run.failedRows)} ·{' '}
                {startedLabel(run.startedAt)}
              </div>
            </li>
          ))}
        </ol>
      )}
      <div className="row-field-empty" data-testid="provenance-runs-page-range">
        {pageRangeLabel(manifest.runsPage, manifest.runs.length, 'runs')}
        {(manifest.runsPage.hasMore || manifest.runsPage.offset > 0) && (
          <span> · bounded history window</span>
        )}
      </div>
      <div className="row-field-empty" data-testid="provenance-runs-page-note">
        Showing the current runs page; older runs not loaded until requested.
      </div>
      {(manifest.runsPage.total > manifest.runs.length || manifest.runsPage.offset > 0) && (
        <div className="provenance-actions" data-testid="provenance-runs-page-controls">
          <button
            type="button"
            className="mini-btn"
            data-testid="provenance-runs-newer"
            disabled={manifest.runsPage.offset <= 0}
            onClick={() => {
              onLoadPage(previousPageOffset(manifest.runsPage), manifest.receiptsPage.offset);
            }}
          >
            Newer runs
          </button>
          <button
            type="button"
            className="mini-btn"
            data-testid="provenance-runs-older"
            disabled={!manifest.runsPage.hasMore || manifest.runsPage.nextOffset == null}
            onClick={() => {
              onLoadPage(
                manifest.runsPage.nextOffset ?? manifest.runsPage.offset,
                manifest.receiptsPage.offset,
              );
            }}
          >
            Older runs
          </button>
        </div>
      )}
    </section>
  );
}

function ProvenanceReceipts({
  activeReceiptId,
  manifest,
  onInspectReceipt,
  onLoadPage,
  onCreateEntityTable,
}: {
  activeReceiptId: string | null;
  manifest: ProvenanceManifestData;
  onInspectReceipt(receiptId: string): void;
  onLoadPage(runsOffset: number, receiptsOffset: number): void;
  onCreateEntityTable?(receiptId: string): void;
}) {
  return (
    <section>
      <h3 className="drawer-section-title">Receipts</h3>
      {manifest.receipts.length === 0 ? (
        <div className="row-field-empty">No v1 receipts recorded.</div>
      ) : (
        <ol className="provenance-run-list" data-testid="provenance-receipt-list">
          {manifest.receipts.map((receipt) => (
            <li key={receipt.receiptId} data-testid="provenance-receipt-row">
              <div className="version-line">
                <span className={`version-status status-${receipt.status}`}>
                  {receipt.status}
                </span>
                <span className="col-field-name">{receipt.actionKind}</span>
                <span className="muted">{receipt.receiptId}</span>
                <button
                  type="button"
                  className="mini-btn"
                  data-testid="receipt-inspect-button"
                  onClick={() => onInspectReceipt(receipt.receiptId)}
                >
                  Inspect
                </button>
              </div>
              <div className="version-sub">
                {receipt.createdAt ? startedLabel(receipt.createdAt) : 'unknown time'}
              </div>
            </li>
          ))}
        </ol>
      )}
      <div className="row-field-empty" data-testid="provenance-receipts-page-range">
        {pageRangeLabel(manifest.receiptsPage, manifest.receipts.length, 'receipts')}
        {(manifest.receiptsPage.hasMore || manifest.receiptsPage.offset > 0) && (
          <span> · bounded history window</span>
        )}
      </div>
      <div className="row-field-empty" data-testid="provenance-receipts-page-note">
        Showing the current receipts page; older receipts not loaded until requested.
      </div>
      {(manifest.receiptsPage.total > manifest.receipts.length || manifest.receiptsPage.offset > 0) && (
        <div className="provenance-actions" data-testid="provenance-receipts-page-controls">
          <button
            type="button"
            className="mini-btn"
            data-testid="provenance-receipts-newer"
            disabled={manifest.receiptsPage.offset <= 0}
            onClick={() => {
              onLoadPage(manifest.runsPage.offset, previousPageOffset(manifest.receiptsPage));
            }}
          >
            Newer receipts
          </button>
          <button
            type="button"
            className="mini-btn"
            data-testid="provenance-receipts-older"
            disabled={!manifest.receiptsPage.hasMore || manifest.receiptsPage.nextOffset == null}
            onClick={() => {
              onLoadPage(
                manifest.runsPage.offset,
                manifest.receiptsPage.nextOffset ?? manifest.receiptsPage.offset,
              );
            }}
          >
            Older receipts
          </button>
        </div>
      )}
      {activeReceiptId && <ReceiptInspector receiptId={activeReceiptId}
        onCreateEntityTable={onCreateEntityTable} />}
    </section>
  );
}

function ProvenanceManifestContent({
  activeReceiptId,
  manifest,
  payload,
  onInspectReceipt,
  onLoadPage,
  onCreateEntityTable,
}: {
  activeReceiptId: string | null;
  manifest: ProvenanceManifestData;
  payload: string;
  onInspectReceipt(receiptId: string): void;
  onLoadPage(runsOffset: number, receiptsOffset: number): void;
  onCreateEntityTable?(receiptId: string): void;
}) {
  return (
    <>
      <ProvenanceSummary manifest={manifest} />
      <ProvenanceProviders providers={manifest.providers} />
      <ProvenanceModels manifest={manifest} />
      <ProvenanceActionKinds manifest={manifest} />
      <ProvenanceRuns manifest={manifest} onLoadPage={onLoadPage} />
      <ProvenanceReceipts
        activeReceiptId={activeReceiptId}
        manifest={manifest}
        onInspectReceipt={onInspectReceipt}
        onLoadPage={onLoadPage}
        onCreateEntityTable={onCreateEntityTable}
      />
      <section data-testid="provenance-charges">
        <h3 className="drawer-section-title">Charges</h3>
        {/* PROJECT-level, deliberately: an attempt whose run has been
            compacted keeps its consent + charge record with a null run_id
            (ruling 7), so no run- or job-keyed surface can open it. This is
            where such a receipt is reachable. */}
        <AttemptReceiptList testIdPrefix="provenance-charge" />
      </section>
      <section>
        <h3 className="drawer-section-title">Export Preview</h3>
        <pre className="explain-pre" data-testid="provenance-json-preview">{payload}</pre>
      </section>
    </>
  );
}

export function ProvenanceManifest({ projectName, onClose, onCreateEntityTable }: ProvenanceManifestProps) {
  const { projectApi: api } = useWorkspaceStores();
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [copyState, setCopyState] = useState<string | null>(null);
  const [activeReceiptId, setActiveReceiptId] = useState<string | null>(null);
  const runsOffsetRef = useRef(0);
  const receiptsOffsetRef = useRef(0);
  const requestSeq = useRef(0);

  const loadPage = useCallback((nextRunsOffset: number, nextReceiptsOffset: number) => {
    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    setState({ status: 'loading' });
    setActiveReceiptId(null);
    api
      .getProvenanceManifest(
        nextRunsOffset,
        PROVENANCE_HISTORY_PAGE_SIZE,
        nextReceiptsOffset,
        PROVENANCE_HISTORY_PAGE_SIZE,
      )
      .then((manifest) => {
        if (requestId !== requestSeq.current) return;
        runsOffsetRef.current = manifest.runsPage.offset;
        receiptsOffsetRef.current = manifest.receiptsPage.offset;
        setState({ status: 'loaded', manifest });
      })
      .catch((error: Error) => {
        if (requestId === requestSeq.current) {
          setState({ status: 'error', error: error.message });
        }
      });
  }, []);

  const load = () => {
    loadPage(runsOffsetRef.current, receiptsOffsetRef.current);
  };

  useEffect(() => {
    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    api
      .getProvenanceManifest(0, PROVENANCE_HISTORY_PAGE_SIZE, 0, PROVENANCE_HISTORY_PAGE_SIZE)
      .then((manifest) => {
        if (requestId !== requestSeq.current) return;
        runsOffsetRef.current = manifest.runsPage.offset;
        receiptsOffsetRef.current = manifest.receiptsPage.offset;
        setState({ status: 'loaded', manifest });
      })
      .catch((error: Error) => {
        if (requestId === requestSeq.current) {
          setState({ status: 'error', error: error.message });
        }
      });
    return () => { requestSeq.current += 1; };
  }, []);

  const manifest = state.status === 'loaded' ? state.manifest : null;
  const payload = useMemo(() => jsonPayload(manifest), [manifest]);

  const copyPayload = async () => {
    try {
      await navigator.clipboard.writeText(payload);
      setCopyState('Copied');
    } catch {
      setCopyState('Copy unavailable');
    }
  };

  return (
    <Drawer
      testId="provenance-manifest"
      title={
        <>
          <ShieldCheck size={14} />
          Provenance
          <span className="muted"> · {projectName}</span>
        </>
      }
      onClose={onClose}
    >
      <ProvenanceActions
        manifest={manifest}
        payload={payload}
        projectName={projectName}
        onCopy={() => { void copyPayload(); }}
        onRefresh={load}
      />
      {copyState && (
        <div className="column-update-result" data-testid="provenance-copy-status">
          {copyState}
        </div>
      )}

      {state.status === 'loading' && (
        <PanelLoading testId="provenance-loading" label="loading..." />
      )}
      {state.status === 'error' && (
        <div className="form-error" data-testid="provenance-error">{state.error}</div>
      )}

      {manifest && (
        <ProvenanceManifestContent
          activeReceiptId={activeReceiptId}
          manifest={manifest}
          payload={payload}
          onInspectReceipt={setActiveReceiptId}
          onLoadPage={loadPage}
          onCreateEntityTable={onCreateEntityTable}
        />
      )}
    </Drawer>
  );
}
