import '../PanelPrimitives.css';
import '../FormControls.css';
import { Button } from '../Button';
import { useCallback, useEffect, useState } from 'react';
import { ApiError, type EmbeddingIndexSummary, type View, type ViewResolved } from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { useOpenView } from './openViewContext';
import { REFRESH_NEEDED_CODES } from './constants';
import { errorMessage } from '../../hooks/errorMessage';
import { useAction } from '../../hooks/useAction';

interface ViewListProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  reloadKey: number;
}

export function ViewList({ apiPort, index, reloadKey }: ViewListProps) {
  const sheetId = index.sheetId;
  const [views, setViews] = useState<View[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [resolved, setResolved] = useState<ViewResolved | null>(null);
  const resolveAction = useAction();
  const runResolve = resolveAction.run;
  const openViewInGrid = useOpenView();

  const load = useCallback(async () => {
    if (sheetId == null) return;
    try {
      const all = await apiPort.listViews(String(sheetId));
      setViews(all.filter((view) => {
        const query = view.spec?.selection.similarity ?? {};
        return query.embedding_index_id === index.indexId;
      }));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [apiPort, sheetId, index.indexId]);

  useEffect(() => {
    // Defer the load so the setState fires outside the synchronous effect body
    // (matches AlertsPanel; avoids react-hooks/set-state-in-effect cascades).
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, reloadKey]);

  const open = useCallback(
    async (viewId: number, name: string) => {
      openViewInGrid(viewId, name);
      setOpenId(viewId);
      setResolved(null);
      const view = await runResolve(async () => {
        try {
          return await apiPort.resolveView(viewId, { limit: 20 });
        } catch (caught) {
          if (
            caught instanceof ApiError &&
            (caught.code === 'embedding_anchor_stale' ||
              REFRESH_NEEDED_CODES.has(caught.code ?? ''))
          ) {
            throw new Error('This view is out of date — refresh the index before opening it.', {
              cause: caught,
            });
          }
          throw caught;
        }
      });
      if (view !== undefined) setResolved(view);
    },
    [apiPort, openViewInGrid, runResolve],
  );

  if (sheetId == null) return null;
  if (error) {
    return (
      <p className="panel-error" data-testid={`embedding-lens-list-error-${index.indexId}`}>
        {error}
      </p>
    );
  }
  if (!views || views.length === 0) return null;

  return (
    <div className="embedding-lenses" data-testid={`embedding-lenses-${index.indexId}`}>
      <span className="form-label">Views</span>
      <ul className="embedding-lens-list">
        {views.map((view) => (
          <li key={view.id} className="embedding-lens" data-testid={`embedding-lens-${view.id}`}>
            <div className="embedding-lens-head">
              <span className="embedding-lens-name">{view.name}</span>
              <Button
                type="button"

                data-testid={`embedding-lens-open-${view.id}`}
                disabled={resolveAction.busy && openId === view.id}
                onClick={() => void open(view.id, view.name)}
              >
                {resolveAction.busy && openId === view.id ? 'Opening…' : 'Open'}
              </Button>
            </div>
            {openId === view.id && resolveAction.error && (
              <p className="panel-error" data-testid={`embedding-lens-resolve-error-${view.id}`}>
                {resolveAction.error}
              </p>
            )}
            {openId === view.id && resolved && (
              <ul
                className="embedding-lens-results"
                data-testid={`embedding-lens-results-${view.id}`}
              >
                {resolved.refs.length === 0 && (
                  <li className="form-hint">No matching rows.</li>
                )}
                {resolved.refs.map((row) => (
                  <li
                    key={row.row_id}
                    className="embedding-lens-result-row"
                    data-testid={`embedding-lens-result-row-${row.row_id}`}
                  >
                    <span className="embedding-lens-result-rowid">row {row.row_id}</span>
                    <span className="embedding-lens-result-score">
                      {row.search_score != null ? `d ${row.search_score.toFixed(3)}` : 'd —'}
                      {' · '}
                      {row.similarity_score != null ? `score ${row.similarity_score.toFixed(3)}` : 'score —'}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
