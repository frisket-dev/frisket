import { useCallback, useReducer } from 'react';
import { Bell, Bookmark, Search } from 'lucide-react';
import {
  ApiError,
  MAX_LENS_VIEW_ROWS,
  type EmbeddingIndexSummary,
  type EmbeddingSimilarityHit,
} from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { isPlainPhrase, parseComposedQuery } from '../../embeddings/composedQuery';
import { REFRESH_NEEDED_CODES } from './constants';

interface ShowSimilarProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  onRefresh(indexId: string, mode: 'incremental' | 'full'): void;
  busy: boolean;
  onLensSaved(): void;
}

interface ShowSimilarState {
  text: string;
  hits: EmbeddingSimilarityHit[] | null;
  stale: boolean;
  error: string | null;
  searching: boolean;
  watched: Set<number>;
  watchError: string | null;
  saved: Set<number>;
  saveError: string | null;
  searchSaved: boolean;
  hybrid: boolean;
}

type ShowSimilarAction =
  | { type: 'set_text'; text: string }
  | { type: 'set_hybrid'; hybrid: boolean }
  | { type: 'search_start' }
  | { type: 'search_success'; hits: EmbeddingSimilarityHit[] }
  | { type: 'search_stale' }
  | { type: 'search_error'; message: string }
  | { type: 'watch_start' }
  | { type: 'watch_success'; rowId: number }
  | { type: 'watch_error'; message: string }
  | { type: 'save_start' }
  | { type: 'save_lens_success'; rowId: number }
  | { type: 'save_search_success' }
  | { type: 'save_error'; message: string };

const initialShowSimilarState: ShowSimilarState = {
  text: '',
  hits: null,
  stale: false,
  error: null,
  searching: false,
  watched: new Set(),
  watchError: null,
  saved: new Set(),
  saveError: null,
  searchSaved: false,
  hybrid: false,
};

function showSimilarReducer(
  state: ShowSimilarState,
  action: ShowSimilarAction,
): ShowSimilarState {
  switch (action.type) {
    case 'set_text':
      return { ...state, text: action.text, searchSaved: false };
    case 'set_hybrid':
      return { ...state, hybrid: action.hybrid, searchSaved: false };
    case 'search_start':
      return { ...state, searching: true, stale: false, error: null };
    case 'search_success':
      return { ...state, searching: false, hits: action.hits };
    case 'search_stale':
      return { ...state, searching: false, stale: true, hits: null };
    case 'search_error':
      return { ...state, searching: false, error: action.message };
    case 'watch_start':
      return { ...state, watchError: null };
    case 'watch_success': {
      const watched = new Set(state.watched);
      watched.add(action.rowId);
      return { ...state, watched };
    }
    case 'watch_error':
      return { ...state, watchError: action.message };
    case 'save_start':
      return { ...state, saveError: null };
    case 'save_lens_success': {
      const saved = new Set(state.saved);
      saved.add(action.rowId);
      return { ...state, saved };
    }
    case 'save_search_success':
      return { ...state, searchSaved: true };
    case 'save_error':
      return { ...state, saveError: action.message };
    default:
      return state;
  }
}

export function ShowSimilar({
  apiPort,
  index,
  onRefresh,
  busy,
  onLensSaved,
}: ShowSimilarProps) {
  const [state, dispatch] = useReducer(
    showSimilarReducer,
    initialShowSimilarState,
  );

  const saveLens = useCallback(
    async (rowId: number, label: string) => {
      dispatch({ type: 'save_start' });
      try {
        // A row-anchor embedding_similarity lens persisted through the contract
        // spine: opening it later resolves to the ordered row-set WITH numeric
        // distance/score. NO provider call - the row anchor reuses the vector.
        await apiPort.saveLens({
          name: `Similar to ${label || `row ${rowId}`}`,
          query: {
            kind: 'embedding_similarity',
            embedding_index_id: index.indexId,
            sheet_id: index.sheetId,
            anchor: { kind: 'row', row_id: rowId },
            limit: MAX_LENS_VIEW_ROWS,
          },
        });
        dispatch({ type: 'save_lens_success', rowId });
        onLensSaved();
      } catch (error) {
        dispatch({
          type: 'save_error',
          message: error instanceof Error ? error.message : String(error),
        });
      }
    },
    [apiPort, index.indexId, index.sheetId, onLensSaved],
  );

  const watchRow = useCallback(
    async (rowId: number, label: string) => {
      dispatch({ type: 'watch_start' });
      try {
        await apiPort.createWatch({
          name: `Similar to ${label || `row ${rowId}`}`,
          scope: { kind: 'sheet', sheet_id: index.sheetId },
          query: {
            kind: 'embedding_similarity',
            embedding_index_id: index.indexId,
            anchor: { kind: 'row', row_id: rowId },
          },
        });
        dispatch({ type: 'watch_success', rowId });
      } catch (error) {
        dispatch({
          type: 'watch_error',
          message: error instanceof Error ? error.message : String(error),
        });
      }
    },
    [apiPort, index.indexId, index.sheetId],
  );

  const search = useCallback(async () => {
    if (!state.text.trim()) return;
    dispatch({ type: 'search_start' });
    try {
      let result;
      if (state.hybrid) {
        result = await apiPort.embeddingHybridPreview(
          index.indexId,
          index.sheetId,
          state.text.trim(),
          { limit: 5 },
        );
      } else {
        const parsed = parseComposedQuery(state.text);
        const query = isPlainPhrase(state.text, parsed)
          ? state.text.trim()
          : { terms: parsed.terms, exclude: parsed.exclude };
        result = await apiPort.embeddingSimilarityPreview(index.indexId, query, {
          limit: 5,
        });
      }
      dispatch({ type: 'search_success', hits: result.hits });
    } catch (error) {
      if (error instanceof ApiError && REFRESH_NEEDED_CODES.has(error.code ?? '')) {
        dispatch({ type: 'search_stale' });
      } else {
        dispatch({
          type: 'search_error',
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }, [apiPort, index.indexId, index.sheetId, state.text, state.hybrid]);

  const saveSearch = useCallback(async () => {
    if (!state.text.trim()) return;
    const { terms, exclude } = parseComposedQuery(state.text);
    if (!state.hybrid && !terms.length) return;
    dispatch({ type: 'save_start' });
    const query = state.hybrid
      ? {
          kind: 'embedding_hybrid',
          embedding_index_id: index.indexId,
          sheet_id: index.sheetId,
          text: state.text.trim(),
        }
      : {
          kind: 'embedding_similarity',
          embedding_index_id: index.indexId,
          sheet_id: index.sheetId,
          anchor: {
            kind: 'manual_text_query',
            terms,
            ...(exclude.length ? { exclude } : {}),
          },
        };
    try {
      await apiPort.saveLens({ name: `Search: ${state.text.trim()}`, query });
      dispatch({ type: 'save_search_success' });
      onLensSaved();
    } catch (error) {
      dispatch({
        type: 'save_error',
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }, [apiPort, state.text, state.hybrid, index.indexId, index.sheetId, onLensSaved]);

  return (
    <div className="embedding-similar" data-testid={`embedding-similar-${index.indexId}`}>
      <div className="embedding-similar-search">
        <input
          className="form-input"
          placeholder="Find similar to…"
          aria-label={`Find similar in ${index.name}`}
          data-testid={`embedding-similar-input-${index.indexId}`}
          value={state.text}
          onChange={(event) =>
            dispatch({ type: 'set_text', text: event.target.value })
          }
          onKeyDown={(event) => {
            if (event.key === 'Enter') void search();
          }}
        />
        <button
          type="button"
          className="mini-btn"
          data-testid={`embedding-similar-search-${index.indexId}`}
          disabled={state.searching || !state.text.trim()}
          onClick={() => void search()}
        >
          <Search size={11} /> Similar
        </button>
        <button
          type="button"
          className="mini-btn embedding-search-save"
          data-testid={`embedding-similar-save-search-${index.indexId}`}
          disabled={!state.text.trim() || state.searchSaved}
          title="Save this search as a view (opens in the grid)"
          onClick={() => void saveSearch()}
        >
          <Bookmark size={11} /> {state.searchSaved ? 'Saved' : 'Save search'}
        </button>
        <label
          className="embedding-hybrid-toggle"
          title="Hybrid: fuse keyword (BM25) + vector search by RRF"
        >
          <input
            type="checkbox"
            data-testid={`embedding-hybrid-toggle-${index.indexId}`}
            checked={state.hybrid}
            onChange={(event) =>
              dispatch({ type: 'set_hybrid', hybrid: event.target.checked })
            }
          />
          + keyword
        </label>
      </div>

      {state.stale && (
        <div
          className="embedding-stale-banner"
          data-testid={`embedding-similar-stale-${index.indexId}`}
          role="alert"
        >
          Embeddings are out of date — refresh needed.
          <button
            type="button"
            className="mini-btn"
            data-testid={`embedding-similar-refresh-${index.indexId}`}
            disabled={busy}
            onClick={() => onRefresh(index.indexId, 'incremental')}
          >
            Refresh
          </button>
        </div>
      )}

      {state.error && <p className="form-error">{state.error}</p>}
      {state.watchError && <p className="form-error">{state.watchError}</p>}
      {state.saveError && (
        <p
          className="form-error"
          data-testid={`embedding-lens-save-error-${index.indexId}`}
        >
          {state.saveError}
        </p>
      )}

      {state.hits && (
        <ul
          className="embedding-similar-hits"
          data-testid={`embedding-similar-hits-${index.indexId}`}
        >
          {state.hits.length === 0 && <li className="form-hint">No matches.</li>}
          {state.hits.map((hit) => {
            const label =
              Object.values(hit.values)
                .map((value) => String(value))
                .join(' · ') || `Row ${hit.rowId}`;
            return (
              <li
                key={hit.rowId}
                className="embedding-similar-hit"
                data-testid="embedding-similar-hit"
              >
                <span className="embedding-hit-values">{label}</span>
                {hit.score != null && (
                  <span className="embedding-hit-score">{hit.score.toFixed(2)}</span>
                )}
                <button
                  type="button"
                  className="mini-btn embedding-watch-hit"
                  data-testid={`embedding-watch-hit-${hit.rowId}`}
                  disabled={state.watched.has(hit.rowId)}
                  title="Watch rows similar to this row"
                  onClick={() => void watchRow(hit.rowId, label)}
                >
                  <Bell size={11} />{' '}
                  {state.watched.has(hit.rowId) ? 'Watching' : 'Watch'}
                </button>
                <button
                  type="button"
                  className="mini-btn embedding-lens-save-hit"
                  data-testid={`embedding-lens-save-hit-${hit.rowId}`}
                  disabled={state.saved.has(hit.rowId)}
                  title="Save a 'rows similar to this row' view"
                  onClick={() => void saveLens(hit.rowId, label)}
                >
                  <Bookmark size={11} />{' '}
                  {state.saved.has(hit.rowId) ? 'Saved' : 'Save as view'}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
