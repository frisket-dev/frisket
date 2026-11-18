import { useEffect } from 'react';
import { RefreshCw } from 'lucide-react';
import type { FeedApiPort } from '../../api/ports';
import { Button } from '../Button';
import { PanelEmpty, PanelError } from '../PanelPrimitives';
import { WindowCloseButton } from '../WindowCloseButton';
import { feedKindLabel } from './feedConfig';
import { formatCost } from './feedFormatting';
import { FeedHealthSummary, FeedHealthChecks } from './FeedHealthContent';
import '../FeedsPanel.css';
import { useFeedHealth } from './useFeedHealth';

export function FeedHealthMainView({
  feedApi,
  sourceId,
  onClose,
  onPolled,
}: {
  feedApi: FeedApiPort;
  sourceId: number;
  onClose: () => void;
  onPolled?: (sheetId: number | null) => void;
}) {
  const { entries, load, poll } = useFeedHealth(feedApi);
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
          <div className="panel-item-title">{health?.source.name ?? 'Feed health'}</div>
          {health && <div className="panel-item-meta">{feedKindLabel(health.source.kind)}</div>}
        </div>
        <WindowCloseButton size="compact" label="Close feed health" onClick={onClose} />
      </div>

      {error && (
        <PanelError testId="source-health-mainView-error">{error}</PanelError>
      )}
      {loading && !health ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : health ? (
        <>
          <FeedHealthSummary health={health} sourceId={sourceId} />

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

          <FeedHealthChecks health={health} sourceId={sourceId} />
        </>
      ) : null}
    </section>
  );
}
