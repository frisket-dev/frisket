import '../PanelPrimitives.css';
import '../FormControls.css';
import { Button } from '../Button';
import { useCallback, useEffect, useReducer } from 'react';
import { ApiError, type EmbeddingIndexSummary, type SavedView, type ViewResolved } from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { useOpenLens } from './openLensContext';
import { REFRESH_NEEDED_CODES } from './constants';

interface LensListProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  reloadKey: number;
}

interface LensListState {
  lenses: SavedView[] | null;
  error: string | null;
  openId: number | null;
  resolved: ViewResolved | null;
  resolveError: string | null;
  resolving: boolean;
}

type LensListAction =
  | { type: 'load_success'; lenses: SavedView[] }
  | { type: 'load_error'; message: string }
  | { type: 'open_start'; lensId: number }
  | { type: 'resolve_success'; resolved: ViewResolved }
  | { type: 'resolve_error'; message: string }
  | { type: 'resolve_done' };

const initialLensListState: LensListState = {
  lenses: null,
  error: null,
  openId: null,
  resolved: null,
  resolveError: null,
  resolving: false,
};

function lensListReducer(state: LensListState, action: LensListAction): LensListState {
  switch (action.type) {
    case 'load_success':
      return { ...state, lenses: action.lenses, error: null };
    case 'load_error':
      return { ...state, error: action.message };
    case 'open_start':
      return {
        ...state,
        openId: action.lensId,
        resolved: null,
        resolveError: null,
        resolving: true,
      };
    case 'resolve_success':
      return { ...state, resolved: action.resolved };
    case 'resolve_error':
      return { ...state, resolveError: action.message };
    case 'resolve_done':
      return { ...state, resolving: false };
    default:
      return state;
  }
}

export function LensList({ apiPort, index, reloadKey }: LensListProps) {
  const sheetId = index.sheetId;
  const [state, dispatch] = useReducer(lensListReducer, initialLensListState);
  const openLensInGrid = useOpenLens();

  const load = useCallback(async () => {
    if (sheetId == null) return;
    try {
      const all = await apiPort.listViews(String(sheetId));
      dispatch({
        type: 'load_success',
        lenses: all.filter((lens) => {
          const query = lens.spec?.selection.similarity ?? {};
          return query.embedding_index_id === index.indexId;
        }),
      });
    } catch (error) {
      dispatch({
        type: 'load_error',
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }, [apiPort, sheetId, index.indexId]);

  useEffect(() => {
    // Defer the load so the setState fires outside the synchronous effect body
    // (matches WatchesPanel; avoids react-hooks/set-state-in-effect cascades).
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, reloadKey]);

  const open = useCallback(
    async (lensId: number, name: string) => {
      openLensInGrid(lensId, name);
      dispatch({ type: 'open_start', lensId });
      try {
        dispatch({
          type: 'resolve_success',
          resolved: await apiPort.resolveView(lensId, { limit: 20 }),
        });
      } catch (error) {
        if (
          error instanceof ApiError &&
          (error.code === 'embedding_anchor_stale' ||
            REFRESH_NEEDED_CODES.has(error.code ?? ''))
        ) {
          dispatch({
            type: 'resolve_error',
            message: 'This view is out of date — refresh the index before opening it.',
          });
        } else {
          dispatch({
            type: 'resolve_error',
            message: error instanceof Error ? error.message : String(error),
          });
        }
      } finally {
        dispatch({ type: 'resolve_done' });
      }
    },
    [apiPort, openLensInGrid],
  );

  if (sheetId == null) return null;
  if (state.error) {
    return (
      <p className="panel-error" data-testid={`embedding-lens-list-error-${index.indexId}`}>
        {state.error}
      </p>
    );
  }
  if (!state.lenses || state.lenses.length === 0) return null;

  return (
    <div className="embedding-lenses" data-testid={`embedding-lenses-${index.indexId}`}>
      <span className="form-label">Saved views</span>
      <ul className="embedding-lens-list">
        {state.lenses.map((lens) => (
          <li key={lens.id} className="embedding-lens" data-testid={`embedding-lens-${lens.id}`}>
            <div className="embedding-lens-head">
              <span className="embedding-lens-name">{lens.name}</span>
              <Button
                type="button"

                data-testid={`embedding-lens-open-${lens.id}`}
                disabled={state.resolving && state.openId === lens.id}
                onClick={() => void open(lens.id, lens.name)}
              >
                {state.resolving && state.openId === lens.id ? 'Opening…' : 'Open'}
              </Button>
            </div>
            {state.openId === lens.id && state.resolveError && (
              <p className="panel-error" data-testid={`embedding-lens-resolve-error-${lens.id}`}>
                {state.resolveError}
              </p>
            )}
            {state.openId === lens.id && state.resolved && (
              <ul
                className="embedding-lens-results"
                data-testid={`embedding-lens-results-${lens.id}`}
              >
                {state.resolved.refs.length === 0 && (
                  <li className="form-hint">No matching rows.</li>
                )}
                {state.resolved.refs.map((row) => (
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
