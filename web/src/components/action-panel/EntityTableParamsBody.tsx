import { useEffect, useState } from 'react';
import { useChromeHandle } from '../../bind/useChromeHandle';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { V1Receipt } from '../../api/types';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function EntityTableParamsBody({ params }:
  GeneratedActionParamsBodyProps<'resolve.entities'>) {
  const { projectApi } = useWorkspaceStores();
  const chrome = useChromeHandle();
  const receiptId = params.source?.receipt_id;
  const [result, setResult] = useState<{
    id: string; receipt?: V1Receipt; error?: string;
  } | null>(null);
  useEffect(() => {
    if (!receiptId) return;
    let current = true;
    projectApi.getReceipt(receiptId).then((receipt) => {
      if (current) setResult({ id: receiptId, receipt });
    }).catch((error: unknown) => {
      if (current) setResult({ id: receiptId,
        error: error instanceof Error ? error.message : 'Could not load clustering result.' });
    });
    return () => { current = false; };
  }, [projectApi, receiptId]);
  const loaded = result?.id === receiptId ? result : null;
  const receipt = loaded?.receipt;
  return <div className="action-source-block" data-testid="entity-table-source">
    <div className="form-label">Completed clustering result</div>
    {receiptId ? <>
      <p className="form-hint">Receipt {receiptId}</p>
      {!loaded && <p className="form-hint">Loading result…</p>}
      {loaded?.error && <p className="form-error" role="alert">{loaded.error}</p>}
      {receipt && (receipt.actionKind !== 'cluster.values' || receipt.status !== 'completed')
        && <p className="form-error" role="alert">This is not a completed Cluster values result.</p>}
      <p className="form-hint">Creates a table from the committed groups and canonical names.
        No clustering is run again. The source is checked when you create the table.</p>
      {(params.cluster_keys != null || Object.keys(params.canonical_overrides ?? {}).length > 0)
        && <details><summary>Saved entity options (preserved)</summary>
          <pre>{JSON.stringify({ cluster_keys: params.cluster_keys,
            canonical_overrides: params.canonical_overrides }, null, 2)}</pre>
        </details>}
    </> : <p className="form-hint">First write your reviewed groups with Cluster values.
      Then open that completed result and choose Create entity table. A preview alone is not a result.</p>}
    <button type="button" className="mini-btn" onClick={() => {
      if (!chrome.store.get().provenanceOpen) chrome.toggleProvenanceOpen();
    }}>Open receipt history</button>
  </div>;
}
