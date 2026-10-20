import './ModelPullHistory.css';
import { useCallback, useEffect, useState } from 'react';
import type { ModelPullDto } from '../api/open';
import { ModelPullProgress } from '../components/ModelPullProgress';
import { formatNumberDisplay } from '../format';
import { errMsg } from './settingsSupport';

// Active and recent downloads share the same lifecycle in workspace and team
// settings; callers supply the scope-specific routes and keep their test IDs.
export function ModelPullHistory({
  loadPulls,
  idPrefix = '',
  fetchPull,
  cancelPull,
}: {
  loadPulls: () => Promise<{ pulls: ModelPullDto[] }>;
  idPrefix?: string;
  fetchPull?: (id: number) => Promise<ModelPullDto>;
  cancelPull?: (id: number) => Promise<unknown>;
}) {
  const [pulls, setPulls] = useState<ModelPullDto[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await loadPulls();
      setPulls(result.pulls);
      setError(null);
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setLoaded(true);
    }
  }, [loadPulls]);

  useEffect(() => {
    // Defer the load so the setState fires outside the synchronous effect
    // body (matches LensList/WatchesPanel; avoids
    // react-hooks/set-state-in-effect cascades).
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const activePulls = pulls.filter((p) => p.status === 'pending' || p.status === 'running');
  const finishedPulls = pulls
    .filter((p) => p.status !== 'pending' && p.status !== 'running')
    .slice(0, 5);

  return (
    <div className="content-stack model-pull-list" data-testid={`${idPrefix}model-pull-list`}>
      {error && (
        <p
          className="panel-notice" data-tone="error"
          data-testid={`${idPrefix}model-pull-list-error`}
        >
          {error}
        </p>
      )}
      {activePulls.map((pull) => (
        <ModelPullProgress
          key={pull.id}
          pull={pull}
          fetchPull={fetchPull}
          cancelPull={cancelPull}
          onDone={() => void load()}
        />
      ))}
      {finishedPulls.length > 0 && (
        <ul className="model-pull-history">
          {finishedPulls.map((pull) => (
            <li
              key={pull.id}
              className="model-pull-history-row"
              data-testid={`${idPrefix}model-pull-history-${pull.id}`}
            >
              <span className="model-pull-history-model">{pull.model}</span>
              <span className="model-pull-history-status">{pull.status}</span>
              <span className="model-pull-history-size">
                {pull.resolved_size != null ? formatNumberDisplay(pull.resolved_size, 'filesize') : '—'}
              </span>
            </li>
          ))}
        </ul>
      )}
      {loaded && !error && activePulls.length === 0 && finishedPulls.length === 0 && (
        <p className="form-hint">No downloads yet.</p>
      )}
    </div>
  );
}
