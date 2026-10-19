import { useCallback, useEffect, useReducer } from 'react';
import { RefreshCw } from 'lucide-react';
import type { SourceHealth } from '../../api/open';
import type { SourceApiPort } from '../../api/ports';
import { Button } from '../Button';
import { PanelEmpty, PanelError } from '../PanelPrimitives';
import { WindowCloseButton } from '../WindowCloseButton';
import { sourceKindLabel } from './sourceConfig';
import { formatCost } from './sourceFormatting';
import { SourceHealthSummary, SourceHealthRuns } from './SourceHealthContent';
import '../SourcesPanel.css';

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

type SourceHealthMainState = {
  health: SourceHealth | null;
  loading: boolean;
  error: string | null;
  busy: boolean;
};

type SourceHealthMainAction =
  | { type: 'loadStart' }
  | { type: 'loadSuccess'; health: SourceHealth }
  | { type: 'loadFailure'; message: string }
  | { type: 'fetchStart' }
  | { type: 'fetchFailure'; message: string }
  | { type: 'fetchSettled' };

const initialSourceHealthMainState = (): SourceHealthMainState => ({
  health: null,
  loading: true,
  error: null,
  busy: false,
});

function sourceHealthMainReducer(
  state: SourceHealthMainState,
  action: SourceHealthMainAction,
): SourceHealthMainState {
  switch (action.type) {
    case 'loadStart':
      return { ...state, loading: true, error: null };
    case 'loadSuccess':
      return { ...state, health: action.health, loading: false, error: null };
    case 'loadFailure':
      return { ...state, loading: false, error: action.message };
    case 'fetchStart':
      return { ...state, busy: true, error: null };
    case 'fetchFailure':
      return { ...state, error: action.message };
    case 'fetchSettled':
      return { ...state, busy: false };
    default:
      return state;
  }
}

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
  const [state, dispatch] = useReducer(
    sourceHealthMainReducer,
    undefined,
    initialSourceHealthMainState,
  );
  const { health, loading, error, busy } = state;

  const loadHealth = useCallback(async () => {
    dispatch({ type: 'loadStart' });
    try {
      dispatch({ type: 'loadSuccess', health: await sourceApi.getSourceHealth(sourceId, 0, 20) });
    } catch (e) {
      dispatch({ type: 'loadFailure', message: msg(e) });
    }
  }, [sourceApi, sourceId]);

  useEffect(() => {
    let alive = true;
    void sourceApi.getSourceHealth(sourceId, 0, 20)
      .then((next) => {
        if (!alive) return;
        dispatch({ type: 'loadSuccess', health: next });
      })
      .catch((e) => {
        if (!alive) return;
        dispatch({ type: 'loadFailure', message: msg(e) });
      });
    return () => { alive = false; };
  }, [sourceApi, sourceId]);

  const fetchNow = useCallback(async () => {
    dispatch({ type: 'fetchStart' });
    try {
      const result = await sourceApi.fetchSource(sourceId);
      onPolled?.(result.sheetId);
    } catch (e) {
      dispatch({ type: 'fetchFailure', message: `Poll failed: ${msg(e)}` });
    } finally {
      dispatch({ type: 'fetchSettled' });
      await loadHealth();
    }
  }, [loadHealth, onPolled, sourceApi, sourceId]);

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
              onClick={() => { void fetchNow(); }}
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
