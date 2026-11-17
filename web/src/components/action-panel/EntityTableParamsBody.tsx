import '../PanelPrimitives.css';
import '../FormControls.css';
import '../ActionPanel.css';
import { Button } from '../Button';
import { useChromeHandle } from '../../bind/useChromeHandle';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { useLoader } from '../../hooks/useLoader';

export function EntityTableParamsBody({ params }:
  GeneratedActionParamsBodyProps<'resolve.entities'>) {
  const { projectApi } = useWorkspaceStores();
  const chrome = useChromeHandle();
  const receiptId = params.source?.receipt_id;
  const loader = useLoader(() => projectApi.getReceipt(receiptId!), [receiptId],
    { enabled: Boolean(receiptId), errorFallback: 'Could not load clustering result.' });
  const receipt = loader.data;
  return <div className="action-source-block" data-testid="entity-table-source">
    <div className="form-label">Completed clustering result</div>
    {receiptId ? <>
      <p className="form-hint">Receipt {receiptId}</p>
      {loader.loading && <p className="form-hint">Loading result…</p>}
      {loader.error && <p className="panel-error" role="alert">{loader.error}</p>}
      {receipt && (receipt.actionKind !== 'cluster.values' || receipt.status !== 'completed')
        && <p className="panel-error" role="alert">This is not a completed Cluster values result.</p>}
      <p className="form-hint">Creates a table from the committed groups and canonical names.
        No clustering is run again. The source is checked when you create the table.</p>
      {(params.cluster_keys != null || Object.keys(params.canonical_overrides ?? {}).length > 0)
        && <details><summary>Saved entity options (preserved)</summary>
          <pre>{JSON.stringify({ cluster_keys: params.cluster_keys,
            canonical_overrides: params.canonical_overrides }, null, 2)}</pre>
        </details>}
    </> : <p className="form-hint">First write your reviewed groups with Cluster values.
      Then open that completed result and choose Create entity table. A preview alone is not a result.</p>}
    <Button type="button" onClick={() => {
      if (!chrome.store.get().provenanceOpen) chrome.toggleProvenanceOpen();
    }}>Open receipt history</Button>
  </div>;
}
