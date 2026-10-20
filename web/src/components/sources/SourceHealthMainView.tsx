import { useEffect } from 'react';
import { RefreshCw } from 'lucide-react';
import type { SourceApiPort } from '../../api/ports';
import { Button } from '../Button';
import { PanelEmpty, PanelError } from '../PanelPrimitives';
import { WindowCloseButton } from '../WindowCloseButton';
import { sourceKindLabel } from './sourceConfig';
import { formatCost } from './sourceFormatting';
import { SourceHealthSummary, SourceHealthRuns } from './SourceHealthContent';
import '../SourcesPanel.css';
import { useSourceHealth } from './useSourceHealth';

export function SourceHealthMainView({
  sourceApi,
  sourceId,
  onClose,
  onPolled,
}: {
  sourceApi: SourceApiPort;
  sourceId: number;
  onClose: () => void;
  onPolled?: (sheetId: number | null) => void;
}) {
  const { entries, load, poll } = useSourceHealth(sourceApi);
  const entry = entries[sourceId];
  const health = entry?.health ?? null;
  const loading = entry?.loading ?? true;
  const error = entry?.error ?? null;
  const busy = entry?.busy ?? false;

  useEffect(() => {
    void load(sourceId);
  }, [load, sourceId]);

  return (
    <section className="content-stack" data-testid="source-health-mainView">
      <div className="control-row">
        <div className="panel-item-main">
          <div className="panel-item-title">{health?.source.name ?? 'Source health'}</div>
          {health && <div className="panel-item-meta">{sourceKindLabel(health.source.kind)}</div>}
        </div>
        <WindowCloseButton size="compact" label="Close source health" onClick={onClose} />
      </div>

      {error && (
        <PanelError testId="source-health-mainView-error">{error}</PanelError>
      )}
      {loading && !health ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : health ? (
        <>
          <SourceHealthSummary health={health} sourceId={sourceId} />

          <div className="control-row panel-item-meta">
            <Button
              type="button"

              data-testid={`source-health-retry-${sourceId}`}
              disabled={busy}
              onClick={() => { void poll(sourceId, { onPolled }); }}
            >
              <RefreshCw size={12} className={busy ? 'spin' : undefined} />
              Poll now
            </Button>
            <span data-testid={`source-health-cost-${sourceId}`}>
              Cost {formatCost(health.costs.recentActualMicro)}
            </span>
          </div>

          <SourceHealthRuns health={health} sourceId={sourceId} />
        </>
      ) : null}
    </section>
  );
}
