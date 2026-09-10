import { useEffect, useMemo, useState } from 'react';
import { FileText, Loader2 } from 'lucide-react';
import { type V1Receipt } from '../api/open';
import { projectBlobUrl } from '../api/raw/projectResources';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';

export interface ReceiptInspectorProps {
  receiptId: string;
  onCreateEntityTable?(receiptId: string): void;
}

type LoadState =
  | { status: 'loading'; receiptId: string; receipt?: undefined; error?: undefined }
  | { status: 'loaded'; receiptId: string; receipt: V1Receipt; error?: undefined }
  | { status: 'error'; receiptId: string; receipt?: undefined; error: string };

function refKind(ref: Record<string, unknown>): string {
  const kind = ref.kind;
  return typeof kind === 'string' && kind ? kind : 'ref';
}

function refDetail(ref: Record<string, unknown>): string {
  return JSON.stringify(ref);
}

function refListKey(prefix: string, ref: Record<string, unknown>, label = ''): string {
  return `${prefix}:${label}:${refDetail(ref)}`;
}

function countLabel(count: number, singular: string, plural = `${singular}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? singular : plural}`;
}

function exportDownload(projectId: string, ref: Record<string, unknown>) {
  if (ref.kind !== 'export_project_file'
    || typeof ref.blob_hash !== 'string' || ref.blob_hash.length !== 64
    || !/^[a-f0-9]{64}$/.test(ref.blob_hash)) return null;
  const path = typeof ref.filename === 'string' ? ref.filename
    : typeof ref.project_path === 'string' ? ref.project_path : '';
  const filename = path.split(/[\\/]/).pop()?.trim() || 'export';
  return { url: projectBlobUrl(projectId, ref.blob_hash), filename };
}

export function ReceiptInspector({ receiptId, onCreateEntityTable }: ReceiptInspectorProps) {
  const { projectApi: api, job } = useWorkspaceStores();
  const [state, setState] = useState<LoadState>(() => ({
    status: 'loading',
    receiptId,
  }));

  useEffect(() => {
    let alive = true;
    api
      .getReceipt(receiptId)
      .then((receipt) => {
        if (alive) setState({ status: 'loaded', receiptId, receipt });
      })
      .catch((error: Error) => {
        if (alive) setState({ status: 'error', receiptId, error: error.message });
      });
    return () => {
      alive = false;
    };
  }, [receiptId]);

  const viewState: LoadState =
    state.receiptId === receiptId ? state : { status: 'loading', receiptId };
  const receipt = viewState.status === 'loaded' ? viewState.receipt : null;
  const counts = useMemo(() => {
    if (!receipt) return [];
    return [
      countLabel(receipt.inputs.length, 'input'),
      countLabel(receipt.outputs.length, 'output'),
      countLabel(receipt.evidence.length, 'evidence ref'),
      countLabel(receipt.providerUse.length, 'provider summary', 'provider summaries'),
      countLabel(receipt.errors.length, 'error'),
    ];
  }, [receipt]);

  return (
    <section className="receipt-inspector" data-testid="receipt-inspector">
      {receipt?.status === 'completed' && receipt.actionKind === 'cluster.values'
        && onCreateEntityTable && <button type="button" className="btn btn-primary"
          onClick={() => {
            onCreateEntityTable(receipt.receiptId);
            if (job.store.get().completedClusterReceiptId === receipt.receiptId) {
              job.dismissCompletedClusterResult();
            }
          }}>
          Create entity table
        </button>}
      {/* Allowlisted, not migrated to PanelLoading —
          icon (Loader2) + flex-row layout is a different genus from the
          plain muted-text block the primitive covers. */}
      {viewState.status === 'loading' && (
        <div className="receipt-loading" data-testid="receipt-loading">
          <Loader2 size={13} aria-hidden />
          Loading receipt
        </div>
      )}

      {viewState.status === 'error' && (
        <div className="form-error" data-testid="receipt-error">{viewState.error}</div>
      )}

      {receipt && (
        <>
          <div className="receipt-heading">
            <FileText size={14} aria-hidden />
            <div>
              <div className="receipt-title-line">
                <strong>{receipt.actionKind}</strong>
                <span className={`version-status status-${receipt.status}`}>
                  {receipt.status}
                </span>
              </div>
              <div className="receipt-subline" data-testid="receipt-schema">
                {receipt.schemaVersion}
              </div>
            </div>
          </div>

          <dl className="receipt-meta">
            <div>
              <dt>receipt</dt>
              <dd data-testid="receipt-id">{receipt.receiptId}</dd>
            </div>
            {receipt.runId && (
              <div>
                <dt>run</dt>
                <dd>{receipt.runId}</dd>
              </div>
            )}
          </dl>

          <div className="receipt-counts" data-testid="receipt-counts">
            {counts.map((count) => (
              <span key={count} className="type-pill">{count}</span>
            ))}
          </div>

          <section>
            <h4 className="receipt-section-title">Outputs</h4>
            <ul className="receipt-ref-list" data-testid="receipt-outputs">
              {receipt.outputs.length === 0 ? (
                <li className="muted">none</li>
              ) : (
                receipt.outputs.map((item) => {
                  const download = exportDownload(receipt.projectId, item.ref);
                  return (
                    <li key={refListKey('output', item.ref, item.name)}>
                      <span>{item.name}</span>
                      <code>{refKind(item.ref)}</code>
                      <small>{refDetail(item.ref)}</small>
                      {download && <a className="mini-btn" href={download.url}
                        download={download.filename}>Download {download.filename}</a>}
                    </li>
                  );
                })
              )}
            </ul>
          </section>

          {receipt.value != null && (
            <section>
              <h4 className="receipt-section-title">Result</h4>
              <pre className="row-field-json" data-testid="receipt-value">
                {JSON.stringify(receipt.value, null, 2)}
              </pre>
            </section>
          )}

          <section>
            <h4 className="receipt-section-title">Evidence</h4>
            <ul className="receipt-ref-list" data-testid="receipt-evidence">
              {receipt.evidence.length === 0 ? (
                <li className="muted">none</li>
              ) : (
                receipt.evidence.map((item) => (
                  <li key={refListKey('evidence', item.ref, item.retention)}>
                    <span>{item.retention}</span>
                    <code>{refKind(item.ref)}</code>
                    <small>{refDetail(item.ref)}</small>
                  </li>
                ))
              )}
            </ul>
          </section>
        </>
      )}
    </section>
  );
}
